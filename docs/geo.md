# Geospatial (H3)

Hexagonal hierarchical geospatial indexing, built in. HyperDjango compiles the
[mattsta/h3 fork](https://github.com/mattsta/h3) of the H3 hexagonal-indexing
library directly into its native extension, so you get fast, dependency-free
spatial bucketing and proximity search — no PostGIS, no `pip install h3`.

---

## Overview

H3 divides the globe into a grid of hexagonal **cells** at 16 nested
**resolutions** (0 = coarsest, 15 = finest). Each cell is a 64-bit index. The two
things that make it useful for application code:

- **Bucketing** — every latitude/longitude maps to exactly one cell at a chosen
  resolution. Rows sharing a cell are "near" each other, so a plain `WHERE cell = ?`
  becomes a spatial lookup with an ordinary B-tree index.
- **Proximity** — from any cell you can enumerate the surrounding cells (a "disk"),
  turning "find everything near here" into an `IN (...)` over indexed integers.

Cells are plain Python `int`s and fit a Postgres `BIGINT`, so no special column
type is required.

```python
from hyperdjango import geo

# Index a point (degrees) to a resolution-9 cell (~170 m edge)
cell = geo.lat_lng_to_cell(37.7749, -122.4194, 9)  # 617700169957507071

# The cell plus its ring of neighbours — a proximity fan-out
nearby = geo.grid_disk(cell, 1)  # 7 cells (center + 6)
```

Import `geo` explicitly — it is not pulled in at package init. It requires the
native extension (build with `uv run hyper-build`); calling any function on an
older extension without H3 raises a `RuntimeError` with a rebuild hint.

---

## Resolutions

Pick the resolution whose cell size matches your bucket. Common choices:

| Resolution | Average edge length | Typical use                     |
| ---------- | ------------------- | ------------------------------- |
| 4          | ~22 km              | Region / metro-area bucketing   |
| 6          | ~3.2 km             | City-district recall            |
| 7          | ~1.2 km             | Neighbourhood                   |
| 8          | ~460 m              | "Nearby" search in a dense city |
| 9          | ~170 m              | Block-level proximity           |
| 11         | ~25 m               | Fine-grained venue clustering   |
| 15         | ~0.5 m              | Maximum precision               |

A finer resolution means more, smaller cells (more precise, larger disks needed
to cover the same real-world radius). A coarser resolution means fewer cells
(cheaper to index and shard, less precise). See the
[mattsta/h3 fork](https://github.com/mattsta/h3) for the full resolution table.

---

## API

All functions live in `hyperdjango.geo`.

### `lat_lng_to_cell(lat_deg, lng_deg, resolution) -> int`

Index a latitude/longitude given in **degrees** to the H3 cell at `resolution`
(`0..15`). This is the write path: compute a cell when a row is created and store
it in a `BIGINT` column.

```python
cell = geo.lat_lng_to_cell(37.7749, -122.4194, 9)
# 617700169957507071
```

Raises `ValueError` on a resolution outside `0..15` or a non-finite coordinate.
It never returns a fabricated cell.

### `grid_disk(origin_cell, k) -> list[int]`

Every cell within grid distance `k` of `origin_cell` — a filled hexagonal disk.
`k=0` is just `[origin_cell]`; `k=1` adds the six immediate neighbours; each
larger `k` adds another ring.

```python
geo.grid_disk(cell, 0)  # [cell]
len(geo.grid_disk(cell, 1))  # 7
len(geo.grid_disk(cell, 2))  # 19
```

Order is not significant. Raises `ValueError` on a negative `k` or a malformed
cell. Near one of H3's twelve pentagons a disk can be slightly smaller than the
ideal `1 + 3·k·(k+1)`; the result only ever contains real cells.

### `grid_disk_distances(origin_cell, k) -> list[tuple[int, int]]`

Like `grid_disk`, but each cell is paired with its ring distance
(`0..k`) from the origin. Use it for **adaptive ring expansion** — widen the
search outward, cheapest ring first, until you have enough candidates.

```python
for cell_id, distance in geo.grid_disk_distances(cell, 2):
    ...  # distance 0 = origin, 1 = first ring, 2 = second ring
```

### `grid_distance(cell_a, cell_b) -> int | None`

The number of grid steps between two cells, or `None` when no honest integer
distance exists — the cells are at different resolutions, or the shortest path
would cross a pentagon. Diagnostics only; it returns `None` rather than guessing.

```python
geo.grid_distance(cell, cell)  # 0
geo.grid_distance(cell_res9, cell_res7)  # None (resolution mismatch)
```

### `cell_to_parent(cell, parent_resolution) -> int`

The coarser ancestor cell at `parent_resolution` (which must be `0..15` and no
finer than the cell's own resolution). Every cell beneath one parent shares its
parent index, which makes the parent a natural **coarse shard or bucket key**.

```python
fine = geo.lat_lng_to_cell(37.7749, -122.4194, 9)
shard = geo.cell_to_parent(fine, 5)  # 599685771850416127
```

Raises `ValueError` if `parent_resolution` is finer than the cell's resolution.

### `get_resolution(cell) -> int`

The resolution (`0..15`) of a cell. Handy to validate a value read back from
storage before trusting it.

```python
geo.get_resolution(cell)  # 9
```

Raises `ValueError` if the value is not a well-formed H3 cell.

---

## Storing cells on a model

A cell is an ordinary integer. Store it in a `BIGINT` column and index it — no
spatial extension needed. Use `Field(big=True)` so the column is `BIGINT` rather
than the default 32-bit `INTEGER` (H3 indexes are 64-bit).

```python
from hyperdjango import Model, Field
from hyperdjango import geo


class Venue(Model):
    name: str
    lat: float
    lng: float
    # H3 res-9 cell — 64-bit, so big=True → BIGINT, and index it for lookups
    h3_cell: int = Field(big=True, index=True)

    class Meta:
        table = "venues"


async def create_venue(name: str, lat: float, lng: float) -> Venue:
    return await Venue.objects.acreate(
        name=name,
        lat=lat,
        lng=lng,
        h3_cell=geo.lat_lng_to_cell(lat, lng, 9),
    )
```

See [Models & ORM](models.md) for `Field(big=True)` and the other column options.

---

## Example: "venues near me"

Combine `lat_lng_to_cell` (bucket the query point) with `grid_disk` (expand to a
neighbourhood) and a single indexed `IN` query:

```python
from hyperdjango import geo


async def venues_near(lat: float, lng: float, k: int = 1) -> list[Venue]:
    """Venues in the caller's cell and the surrounding k rings."""
    origin = geo.lat_lng_to_cell(lat, lng, 9)
    cells = geo.grid_disk(origin, k)  # center + neighbours
    return await Venue.objects.filter(h3_cell__in=cells).all()
```

Because `h3_cell` is an indexed `BIGINT`, this is a plain index range scan — no
distance math in the database. Widen `k` to search a larger radius, or drop to a
coarser resolution (e.g. 8) for bigger cells and smaller disks.

For sparse data, expand adaptively instead of guessing `k` up front:

```python
async def venues_until(lat: float, lng: float, want: int = 20) -> list[Venue]:
    origin = geo.lat_lng_to_cell(lat, lng, 9)
    found: list[Venue] = []
    for k in range(0, 6):  # widen ring by ring
        cells = geo.grid_disk(origin, k)
        found = await Venue.objects.filter(h3_cell__in=cells).all()
        if len(found) >= want:
            break
    return found
```

---

## Notes

- **Degrees in, integers out.** Coordinates are degrees; cells are `BIGINT`-safe
  `int`s. A valid H3 index reserves its high bit, so it always fits a positive
  signed 64-bit integer.
- **Honest failures.** Every function raises `ValueError` on bad input rather
  than returning a wrong cell. `grid_distance` returns `None` when there is no
  defined answer.
- **SIMD-accelerated.** H3 is compiled with the per-ISA SIMD kernels the build
  target supports (NEON on ARM64, AVX2/AVX-512 on x86-64), selected at compile
  time from the target CPU's feature set. Results are bit-identical across
  kernels — the acceleration is transparent.
