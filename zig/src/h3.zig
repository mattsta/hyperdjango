// H3 geospatial primitives — a minimal Python surface over the H3 v4 C library.
//
// The H3 source (the mattsta/h3 fork, github.com/mattsta/h3, v4.4.1) is compiled
// into this same native extension by build.zig, with per-ISA SIMD kernels
// selected for the target CPU (NEON on aarch64, AVX2/AVX-512 on x86_64; see
// `addH3`). `@cImport("h3api.h")` sees the v4 API directly because `H3_EXPORT`
// resolves to unprefixed symbols (h3api.h.in:38).
//
// Surface (mirrors the `_db_*` block in main.zig; registered in `methods`):
//   _h3_lat_lng_to_cell(lat_deg, lng_deg, res) -> int          write-path + viewer cell
//   _h3_grid_disk(origin_cell, k)             -> list[int]      the radius recall fan-out
//   _h3_grid_disk_distances(origin_cell, k)   -> list[(cell,d)] adaptive ring-expansion
//   _h3_grid_distance(a, b)                   -> int | None     diagnostics
//   _h3_cell_to_parent(cell, parent_res)      -> int            the shard key
//   _h3_get_resolution(cell)                  -> int            validation
//
// HONESTY / SAFETY discipline (ADR-0026, S3-RECALL-GEO-DESIGN §2.2):
//   * Every wrapper checks `H3Error != E_SUCCESS` and RAISES ValueError (or
//     returns None where None is a meaningful "no answer", e.g. gridDistance
//     across a pentagon) — it NEVER fabricates a cell index. This is the same
//     discipline as `coarse_distance_meters` returning None on a missing bucket.
//   * uint64 -> int64 BIGINT GUARD: H3Index is uint64; Postgres BIGINT is int64.
//     A valid H3 cell has bit 63 (the reserved high bit) = 0, so it fits a
//     positive int64. `cellToI64` checks bit 63 and raises if set, so a
//     malformed value can never silently wrap negative in the h3_cell column.
//   * Python ints arriving as the uint64 cell are read back through the same
//     guard (`i64ToCell`) so a negative / out-of-range int is rejected, never
//     reinterpreted.

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

// ── H3 v4 C API (compiled into this ext; symbols are unprefixed) ─────────────
// Declared here rather than via @cImport("h3api.h") on the whole module so the
// rest of the codebase is unaffected; the symbols resolve at link time against
// the compiled H3 object files. Signatures verified against
// build_native/src/h3lib/include/h3api.h (the version-filled generated header).

const LatLng = extern struct {
    lat: f64, // latitude in radians
    lng: f64, // longitude in radians
};

const H3Index = u64;
const H3Error = u32;
const E_SUCCESS: H3Error = 0;

extern fn latLngToCell(g: *const LatLng, res: c_int, out: *H3Index) H3Error;
extern fn maxGridDiskSize(k: c_int, out: *i64) H3Error;
extern fn gridDisk(origin: H3Index, k: c_int, out: [*]H3Index) H3Error;
extern fn gridDiskDistances(origin: H3Index, k: c_int, out: [*]H3Index, distances: [*]c_int) H3Error;
extern fn gridDistance(origin: H3Index, h3: H3Index, distance: *i64) H3Error;
extern fn cellToParent(h: H3Index, parentRes: c_int, parent: *H3Index) H3Error;
extern fn getResolution(h: H3Index) c_int;
extern fn degsToRads(degrees: f64) f64;

// ── uint64 <-> int64 BIGINT boundary guards (risk #1) ────────────────────────

/// Convert an H3 cell (uint64) to a Python int via i64, guarding bit 63.
/// A valid H3 index reserves bit 63 = 0; if it is set the value is malformed
/// and we raise rather than let it wrap negative in a BIGINT column.
fn cellToPyInt(cell: H3Index) ?*c.PyObject {
    if ((cell >> 63) & 1 == 1) {
        py.setValueError("H3 cell index has reserved bit 63 set (0x{x}) — refusing to wrap negative in BIGINT", .{cell});
        return null;
    }
    return c.PyLong_FromLongLong(@intCast(cell));
}

/// Read a Python int back into an H3 cell (uint64), guarding the int64 range.
/// Rejects negatives (a stored BIGINT can never be a valid cell with bit 63
/// set) and any value PyLong_AsLongLong cannot represent.
fn pyIntToCell(obj: ?*c.PyObject) ?H3Index {
    const v: i64 = c.PyLong_AsLongLong(obj);
    if (v == -1 and c.PyErr_Occurred() != null) {
        // The OverflowError/TypeError is already set by CPython.
        return null;
    }
    if (v < 0) {
        py.setValueError("H3 cell index must be a non-negative BIGINT (got {d})", .{v});
        return null;
    }
    return @intCast(v);
}

/// Validate an H3 resolution argument (0..15) before passing it to C, so a
/// bad res raises a clean ValueError instead of an opaque E_RES_DOMAIN.
fn validRes(res: c_long) bool {
    if (res < 0 or res > 15) {
        py.setValueError("H3 resolution must be in 0..15 (got {d})", .{res});
        return false;
    }
    return true;
}

// ── Python-facing wrappers ───────────────────────────────────────────────────

/// _h3_lat_lng_to_cell(lat_deg, lng_deg, res) -> int
/// Latitude/longitude are accepted in DEGREES (the coarse bucket center) and
/// converted to radians via degsToRads before latLngToCell. Raises ValueError
/// on any H3Error (e.g. NaN coords) — NEVER returns a fabricated cell.
pub fn h3_lat_lng_to_cell(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var lat_deg: f64 = undefined;
    var lng_deg: f64 = undefined;
    var res: c_long = undefined;
    if (c.PyArg_ParseTuple(args, "ddl", &lat_deg, &lng_deg, &res) == 0) return null;
    if (!validRes(res)) return null;

    const g = LatLng{ .lat = degsToRads(lat_deg), .lng = degsToRads(lng_deg) };
    var cell: H3Index = 0;
    const err = latLngToCell(&g, @intCast(res), &cell);
    if (err != E_SUCCESS) {
        py.setValueError("latLngToCell failed (H3Error {d}) for ({d}, {d}) res {d}", .{ err, lat_deg, lng_deg, res });
        return null;
    }
    return cellToPyInt(cell);
}

/// _h3_grid_disk(origin_cell, k) -> list[int]
/// All cells within grid distance k of origin (the recall fan-out). Raises
/// ValueError on a negative k or any H3Error; returns a Python list of cell
/// ints (E_SUCCESS guarantees no fabricated members — H3 zero-fills empties).
pub fn h3_grid_disk(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var origin_obj: ?*c.PyObject = null;
    var k: c_long = undefined;
    if (c.PyArg_ParseTuple(args, "Ol", &origin_obj, &k) == 0) return null;
    const origin = pyIntToCell(origin_obj) orelse return null;
    if (k < 0) {
        py.setValueError("grid_disk k must be >= 0 (got {d})", .{k});
        return null;
    }

    var max_size: i64 = 0;
    if (maxGridDiskSize(@intCast(k), &max_size) != E_SUCCESS or max_size <= 0) {
        py.setValueError("maxGridDiskSize failed for k {d}", .{k});
        return null;
    }
    const n: usize = @intCast(max_size);

    const out = std.heap.c_allocator.alloc(H3Index, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(out);
    @memset(out, 0);

    const err = gridDisk(origin, @intCast(k), out.ptr);
    if (err != E_SUCCESS) {
        py.setValueError("gridDisk failed (H3Error {d}) for cell 0x{x} k {d}", .{ err, origin, k });
        return null;
    }

    const list = c.PyList_New(0) orelse return null;
    for (out) |cell| {
        if (cell == 0) continue; // gridDisk zero-fills slots that fall off pentagons
        const py_cell = cellToPyInt(cell) orelse {
            c.Py_DecRef(list);
            return null;
        };
        if (c.PyList_Append(list, py_cell) != 0) {
            c.Py_DecRef(py_cell);
            c.Py_DecRef(list);
            return null;
        }
        c.Py_DecRef(py_cell);
    }
    return list;
}

/// _h3_grid_disk_distances(origin_cell, k) -> list[(cell, distance)]
/// Like grid_disk but each survivor is paired with its ring distance from
/// origin — the substrate for adaptive ring-expansion (sparse-cell recall).
pub fn h3_grid_disk_distances(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var origin_obj: ?*c.PyObject = null;
    var k: c_long = undefined;
    if (c.PyArg_ParseTuple(args, "Ol", &origin_obj, &k) == 0) return null;
    const origin = pyIntToCell(origin_obj) orelse return null;
    if (k < 0) {
        py.setValueError("grid_disk_distances k must be >= 0 (got {d})", .{k});
        return null;
    }

    var max_size: i64 = 0;
    if (maxGridDiskSize(@intCast(k), &max_size) != E_SUCCESS or max_size <= 0) {
        py.setValueError("maxGridDiskSize failed for k {d}", .{k});
        return null;
    }
    const n: usize = @intCast(max_size);

    const cells = std.heap.c_allocator.alloc(H3Index, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(cells);
    const dists = std.heap.c_allocator.alloc(c_int, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(dists);
    @memset(cells, 0);
    @memset(dists, 0);

    const err = gridDiskDistances(origin, @intCast(k), cells.ptr, dists.ptr);
    if (err != E_SUCCESS) {
        py.setValueError("gridDiskDistances failed (H3Error {d}) for cell 0x{x} k {d}", .{ err, origin, k });
        return null;
    }

    const list = c.PyList_New(0) orelse return null;
    for (cells, dists) |cell, dist| {
        if (cell == 0) continue;
        const py_cell = cellToPyInt(cell) orelse {
            c.Py_DecRef(list);
            return null;
        };
        const py_dist = c.PyLong_FromLong(@intCast(dist)) orelse {
            c.Py_DecRef(py_cell);
            c.Py_DecRef(list);
            return null;
        };
        // PyTuple_Pack steals nothing; it INCREFs, so we own our refs still.
        const tup = c.PyTuple_Pack(2, py_cell, py_dist) orelse {
            c.Py_DecRef(py_cell);
            c.Py_DecRef(py_dist);
            c.Py_DecRef(list);
            return null;
        };
        c.Py_DecRef(py_cell);
        c.Py_DecRef(py_dist);
        if (c.PyList_Append(list, tup) != 0) {
            c.Py_DecRef(tup);
            c.Py_DecRef(list);
            return null;
        }
        c.Py_DecRef(tup);
    }
    return list;
}

/// _h3_grid_distance(a, b) -> int | None
/// Grid distance between two cells. Returns None (NOT a raise) on E_FAILED /
/// pentagon / resolution-mismatch — an honest "no answer", never a fabricated
/// number. This is diagnostics only; recall does not depend on it.
pub fn h3_grid_distance(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var a_obj: ?*c.PyObject = null;
    var b_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "OO", &a_obj, &b_obj) == 0) return null;
    const a = pyIntToCell(a_obj) orelse return null;
    const b = pyIntToCell(b_obj) orelse return null;

    var dist: i64 = 0;
    const err = gridDistance(a, b, &dist);
    if (err != E_SUCCESS) {
        // No clean integer distance exists (different res / pentagon span).
        return py.pyNone();
    }
    return c.PyLong_FromLongLong(dist);
}

/// _h3_cell_to_parent(cell, parent_res) -> int
/// The coarser parent cell (the shard key at RES_SHARD). Raises ValueError on
/// any H3Error (e.g. parent_res finer than the cell's own resolution).
pub fn h3_cell_to_parent(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var cell_obj: ?*c.PyObject = null;
    var parent_res: c_long = undefined;
    if (c.PyArg_ParseTuple(args, "Ol", &cell_obj, &parent_res) == 0) return null;
    const cell = pyIntToCell(cell_obj) orelse return null;
    if (!validRes(parent_res)) return null;

    var parent: H3Index = 0;
    const err = cellToParent(cell, @intCast(parent_res), &parent);
    if (err != E_SUCCESS) {
        py.setValueError("cellToParent failed (H3Error {d}) for cell 0x{x} parent_res {d}", .{ err, cell, parent_res });
        return null;
    }
    return cellToPyInt(parent);
}

/// _h3_get_resolution(cell) -> int
/// The resolution (0..15) of a cell. Used to validate stored cells. H3's
/// getResolution returns an int directly (no H3Error); a value outside 0..15
/// indicates a malformed cell and raises.
pub fn h3_get_resolution(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var cell_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O", &cell_obj) == 0) return null;
    const cell = pyIntToCell(cell_obj) orelse return null;

    const res = getResolution(cell);
    if (res < 0 or res > 15) {
        py.setValueError("getResolution returned {d} for cell 0x{x} — malformed cell", .{ res, cell });
        return null;
    }
    return c.PyLong_FromLong(@intCast(res));
}
