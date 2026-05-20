// Native acceleration for static file serving.
//
// Provides:
// - _hash_file_md5(path) -> hex_digest: Read file + MD5 hash in one pass
// - _gzip_compress(data, level) -> compressed: Native gzip compression
// - _file_read_with_hash(path) -> (content, hex_digest): Read + hash in one call
//
// These eliminate multiple Python-side operations (open + read + hashlib.md5 + gzip.compress)
// into single native calls with zero-copy where possible.

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

const Md5 = std.crypto.hash.Md5;
const allocator = std.heap.c_allocator;

const hex_digits = "0123456789abcdef";

fn md5_to_hex(digest: [16]u8) [32]u8 {
    var hex: [32]u8 = undefined;
    for (digest, 0..) |byte, i| {
        hex[i * 2] = hex_digits[byte >> 4];
        hex[i * 2 + 1] = hex_digits[byte & 0x0f];
    }
    return hex;
}

/// _hash_file_md5(path: str) -> str
/// Read file and compute MD5 hash in a single native call.
/// Returns 32-char lowercase hex digest (first 12 chars used for content-hash filenames).
pub fn py_hash_file_md5(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var path_ptr: [*c]const u8 = undefined;
    var path_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "s#", &path_ptr, &path_len) == 0) return null;

    const path = path_ptr[0..@intCast(path_len)];

    // Open and read the file
    const file = py.NativeFile.open(path) catch {
        _ = c.PyErr_Format(c.PyExc_FileNotFoundError, "Cannot open file: %.*s", @as(c_int, @intCast(path_len)), path_ptr);
        return null;
    };
    defer file.close();

    const size = file.size() catch {
        _ = c.PyErr_Format(c.PyExc_IOError, "Cannot get file size: %.*s", @as(c_int, @intCast(path_len)), path_ptr);
        return null;
    };

    if (size > 512 * 1024 * 1024) { // 512MB limit
        _ = c.PyErr_Format(c.PyExc_ValueError, "File too large for hashing: %.*s", @as(c_int, @intCast(path_len)), path_ptr);
        return null;
    }

    const buf = allocator.alloc(u8, @intCast(size)) catch {
        _ = c.PyErr_SetNone(c.PyExc_MemoryError);
        return null;
    };
    defer allocator.free(buf);

    const bytes_read = file.readAll(buf) catch {
        _ = c.PyErr_Format(c.PyExc_IOError, "Cannot read file: %.*s", @as(c_int, @intCast(path_len)), path_ptr);
        return null;
    };

    // Compute MD5
    var digest: [16]u8 = undefined;
    Md5.hash(buf[0..bytes_read], &digest, .{});
    const hex = md5_to_hex(digest);

    return py.newString(hex[0..32]);
}

/// _file_read_with_hash(path: str) -> tuple[bytes, str]
/// Read file content and compute MD5 hash in a single native call.
/// Returns (content_bytes, hex_digest_str).
/// This eliminates the Python-side pattern of read_bytes() + hashlib.md5().
pub fn py_file_read_with_hash(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var path_ptr: [*c]const u8 = undefined;
    var path_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "s#", &path_ptr, &path_len) == 0) return null;

    const path = path_ptr[0..@intCast(path_len)];

    const file = py.NativeFile.open(path) catch {
        _ = c.PyErr_Format(c.PyExc_FileNotFoundError, "Cannot open file: %.*s", @as(c_int, @intCast(path_len)), path_ptr);
        return null;
    };
    defer file.close();

    const size = file.size() catch {
        _ = c.PyErr_Format(c.PyExc_IOError, "Cannot get file size", @as(c_int, 0));
        return null;
    };

    if (size > 512 * 1024 * 1024) {
        _ = c.PyErr_Format(c.PyExc_ValueError, "File too large", @as(c_int, 0));
        return null;
    }

    const buf = allocator.alloc(u8, @intCast(size)) catch {
        _ = c.PyErr_SetNone(c.PyExc_MemoryError);
        return null;
    };
    // Don't free buf — we pass it to Python as bytes (PyBytes_FromStringAndSize copies)

    const bytes_read = file.readAll(buf) catch {
        allocator.free(buf);
        _ = c.PyErr_Format(c.PyExc_IOError, "Cannot read file", @as(c_int, 0));
        return null;
    };

    // Compute MD5
    var digest: [16]u8 = undefined;
    Md5.hash(buf[0..bytes_read], &digest, .{});
    const hex = md5_to_hex(digest);

    // Build Python tuple (bytes, str)
    const py_bytes = c.PyBytes_FromStringAndSize(@ptrCast(buf.ptr), @intCast(bytes_read));
    allocator.free(buf); // Safe — PyBytes_FromStringAndSize copies the data
    if (py_bytes == null) return null;

    const py_hex = py.newString(hex[0..32]);
    if (py_hex == null) {
        c.Py_DECREF(py_bytes);
        return null;
    }

    const tuple = c.PyTuple_New(2);
    if (tuple == null) {
        c.Py_DECREF(py_bytes);
        c.Py_DECREF(py_hex);
        return null;
    }

    // PyTuple_SetItem steals references
    _ = c.PyTuple_SetItem(tuple, 0, py_bytes);
    _ = c.PyTuple_SetItem(tuple, 1, py_hex);

    return tuple;
}

// Note: gzip compression not provided natively because Zig 0.15 does not include
// a gzip/zlib module (only raw DEFLATE via std.compress.flate). Python's gzip module
// is already C-accelerated (via zlib), so the overhead is minimal. The real performance
// win is in _file_read_with_hash which eliminates the Python read_bytes + hashlib.md5
// double-pass pattern.
