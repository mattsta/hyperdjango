// log_helpers.zig — Native helpers for logging hot path acceleration.
//
// Provides:
//   _log_timestamp_iso() → ISO 8601 UTC timestamp bytes (no Python datetime overhead)
//   _log_basename(path) → basename without os.path overhead
//   _log_splitext(basename) → module name (strip .py extension)

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

/// Return ISO 8601 UTC timestamp as bytes: "2026-03-23T14:30:45.123456+00:00"
/// Much faster than datetime.now(timezone.utc).isoformat() — avoids Python
/// datetime object creation entirely.
pub fn log_timestamp_iso(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const ts = py.timestamp();
    const nanos: u64 = @intCast(py.nanoTimestamp());
    const micros = @divFloor(nanos, 1000) % 1_000_000;

    // Convert epoch seconds to date/time components
    const epoch_secs: u64 = @intCast(ts);
    const days = epoch_secs / 86400;
    const day_secs = epoch_secs % 86400;
    const hours = day_secs / 3600;
    const minutes = (day_secs % 3600) / 60;
    const seconds = day_secs % 60;

    // Civil date from days since epoch (algorithm from Howard Hinnant)
    const z = days + 719468;
    const era = z / 146097;
    const doe = z - era * 146097;
    const yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const y = yoe + era * 400;
    const doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const mp = (5 * doy + 2) / 153;
    const d = doy - (153 * mp + 2) / 5 + 1;
    const m = if (mp < 10) mp + 3 else mp - 9;
    const year = if (m <= 2) y + 1 else y;

    // Format: "YYYY-MM-DDTHH:MM:SS.ffffff+00:00" (32 chars)
    var buf: [32]u8 = undefined;
    _ = formatDecimal(&buf, 0, 4, @intCast(year));
    buf[4] = '-';
    _ = formatDecimal(&buf, 5, 2, @intCast(m));
    buf[7] = '-';
    _ = formatDecimal(&buf, 8, 2, @intCast(d));
    buf[10] = 'T';
    _ = formatDecimal(&buf, 11, 2, @intCast(hours));
    buf[13] = ':';
    _ = formatDecimal(&buf, 14, 2, @intCast(minutes));
    buf[16] = ':';
    _ = formatDecimal(&buf, 17, 2, @intCast(seconds));
    buf[19] = '.';
    _ = formatDecimal(&buf, 20, 6, @intCast(micros));
    buf[26] = '+';
    buf[27] = '0';
    buf[28] = '0';
    buf[29] = ':';
    buf[30] = '0';
    buf[31] = '0';

    return c.PyBytes_FromStringAndSize(&buf, 32);
}

/// Extract basename from a path (last component after / or \).
/// Avoids os.path.basename() overhead in the logging hot path.
pub fn log_basename(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var path_ptr: [*c]const u8 = undefined;
    var path_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "s#", &path_ptr, &path_len) == 0) return null;

    const path = path_ptr[0..@intCast(path_len)];

    // Find last separator
    var i: usize = path.len;
    while (i > 0) : (i -= 1) {
        if (path[i - 1] == '/' or path[i - 1] == '\\') break;
    }

    const basename = path[i..];
    return c.PyUnicode_FromStringAndSize(basename.ptr, @intCast(basename.len));
}

/// Extract module name from basename (strip .py extension).
/// "app.py" → "app", "foo.bar.py" → "foo.bar", "no_ext" → "no_ext"
pub fn log_module_name(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "s#", &name_ptr, &name_len) == 0) return null;

    const name = name_ptr[0..@intCast(name_len)];

    // Find last dot
    var dot: usize = name.len;
    var i: usize = name.len;
    while (i > 0) : (i -= 1) {
        if (name[i - 1] == '.') {
            dot = i - 1;
            break;
        }
    }

    return c.PyUnicode_FromStringAndSize(name.ptr, @intCast(dot));
}

/// Format a decimal number into a fixed-width field with leading zeros.
fn formatDecimal(buf: []u8, offset: usize, width: usize, val: u64) usize {
    var v = val;
    var i: usize = width;
    while (i > 0) : (i -= 1) {
        buf[offset + i - 1] = @intCast(v % 10 + '0');
        v /= 10;
    }
    return width;
}
