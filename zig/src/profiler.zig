// Built-in profiler — nanosecond precision timing
//
// Uses py.nanoTimestamp() for sub-microsecond accuracy.
// Provides timing primitives that Python profiling wraps call.
//
// API:
//   _profiler_nanos() -> int  (current nanosecond timestamp)
//   _profiler_diff_nanos(start) -> int  (elapsed nanoseconds since start)

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;

// ── _profiler_nanos ──────────────────────────────────────────────────────────
// Returns current nanosecond timestamp as Python int.
// Uses monotonic clock — suitable for elapsed time measurement.

pub fn py_profiler_nanos(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const nanos = py.nanoTimestamp();
    return c.PyLong_FromLongLong(@intCast(nanos));
}

// ── _profiler_diff_nanos ─────────────────────────────────────────────────────
// Returns elapsed nanoseconds since start timestamp.
// Args: (start_nanos: int) -> int

pub fn py_profiler_diff_nanos(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var start: c_longlong = undefined;
    if (c.PyArg_ParseTuple(args, "L", &start) == 0) return null;

    const now = py.nanoTimestamp();
    const elapsed: c_longlong = @intCast(now - @as(i128, start));
    return c.PyLong_FromLongLong(elapsed);
}
