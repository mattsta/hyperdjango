"""H3 geospatial primitives — hexagonal hierarchical spatial indexing.

HyperDjango compiles the `mattsta/h3 fork <https://github.com/mattsta/h3>`_ of
the H3 hexagonal-indexing library directly into its native extension (no external
dependency, no ``pip install h3``).
This module is the public Python surface over that native code — a thin, clean
namespace around the low-level ``_h3_*`` functions in ``_hyperdjango_native``.

Import it explicitly (it is not pulled in at package init):

    from hyperdjango import geo

    cell = geo.lat_lng_to_cell(37.7749, -122.4194, 9)   # a res-9 cell over SF
    neighbours = geo.grid_disk(cell, 1)                 # the cell + its 6 neighbours

Design notes / guarantees:

* **Coordinates are DEGREES.** ``lat_lng_to_cell`` takes latitude/longitude in
  degrees (the everyday form) and converts to radians internally.
* **Cells are plain Python ``int``s**, safe to store in a Postgres ``BIGINT``
  column. A valid H3 index reserves the high bit, so it always fits a positive
  signed 64-bit integer; a malformed value raises ``ValueError`` rather than
  wrapping negative.
* **Never fabricates an answer.** Every function raises ``ValueError`` on an H3
  error (bad resolution, NaN coordinate, a parent finer than the cell, …).
  :func:`grid_distance` is the one exception: it returns ``None`` when no honest
  integer distance exists (cells at different resolutions, or a path crossing a
  pentagon) — an explicit "no answer", never a made-up number.
* **Resolutions run 0 (coarsest, ~1000 km hexes) to 15 (finest, ~0.5 m).**

Requires the native extension. If H3 is not compiled into the loaded extension
(an older build), calling any function here raises ``RuntimeError`` with a
rebuild hint — importing this module never fails on its own.
"""

from __future__ import annotations

try:  # The native extension bundles H3; import errors are surfaced on first use.
    import hyperdjango._hyperdjango_native as _native
except ImportError:  # pragma: no cover - build tools run without the .so
    _native = None

__all__ = [
    "lat_lng_to_cell",
    "grid_disk",
    "grid_disk_distances",
    "grid_distance",
    "cell_to_parent",
    "get_resolution",
]


def _fn(name: str):
    """Resolve a native ``_h3_*`` callable or raise a clear rebuild hint."""
    # dynamic-attr: name is a runtime _h3_* symbol string; the native module exposes it only when built with H3 support
    fn = getattr(_native, name, None) if _native is not None else None
    if fn is None:
        raise RuntimeError(
            "H3 geospatial support is not available in the loaded native "
            "extension. Rebuild it with: uv run hyper-build"
        )
    return fn


def lat_lng_to_cell(lat_deg: float, lng_deg: float, resolution: int) -> int:
    """Index a latitude/longitude (in **degrees**) to the H3 cell at ``resolution``.

    ``resolution`` must be in ``0..15``. Returns the cell as a ``BIGINT``-safe
    ``int``. Raises ``ValueError`` on a bad resolution or non-finite coordinate.

        >>> geo.lat_lng_to_cell(37.7749, -122.4194, 9)
        617700169957507071
    """
    return _fn("_h3_lat_lng_to_cell")(lat_deg, lng_deg, resolution)


def grid_disk(origin_cell: int, k: int) -> list[int]:
    """All cells within grid distance ``k`` of ``origin_cell`` (a filled hex disk).

    ``k=0`` returns ``[origin_cell]``; ``k=1`` returns the origin plus its (up to)
    six neighbours; and so on. Order is not significant. Raises ``ValueError`` on
    a negative ``k`` or a malformed cell.

        >>> len(geo.grid_disk(cell, 1))
        7
    """
    return _fn("_h3_grid_disk")(origin_cell, k)


def grid_disk_distances(origin_cell: int, k: int) -> list[tuple[int, int]]:
    """Like :func:`grid_disk`, but pair each cell with its ring distance from origin.

    Returns a list of ``(cell, distance)`` tuples where ``0 <= distance <= k`` —
    the substrate for adaptive ring-expansion (widen the search ring until enough
    candidates are found). Raises ``ValueError`` on a negative ``k``.

        >>> geo.grid_disk_distances(cell, 1)
        [(617700169957507071, 0), (617700169959079935, 1), ...]
    """
    return _fn("_h3_grid_disk_distances")(origin_cell, k)


def grid_distance(cell_a: int, cell_b: int) -> int | None:
    """Grid distance (number of steps) between two cells, or ``None``.

    Returns ``None`` — not a raised error — when no integer distance is defined:
    the cells are at different resolutions, or the path would cross a pentagon.
    Intended for diagnostics.

        >>> geo.grid_distance(cell, cell)
        0
    """
    return _fn("_h3_grid_distance")(cell_a, cell_b)


def cell_to_parent(cell: int, parent_resolution: int) -> int:
    """The coarser ancestor of ``cell`` at ``parent_resolution``.

    ``parent_resolution`` must be ``0..15`` and no finer than the cell's own
    resolution (raises ``ValueError`` otherwise). Handy as a coarse shard/bucket
    key: every cell under one parent shares the same parent index.

        >>> geo.cell_to_parent(geo.lat_lng_to_cell(37.77, -122.41, 9), 5)
        599685771850416127
    """
    return _fn("_h3_cell_to_parent")(cell, parent_resolution)


def get_resolution(cell: int) -> int:
    """The resolution (``0..15``) of ``cell``. Raises ``ValueError`` if malformed.

    Useful for validating a cell read back from storage before trusting it.

        >>> geo.get_resolution(geo.lat_lng_to_cell(37.77, -122.41, 9))
        9
    """
    return _fn("_h3_get_resolution")(cell)
