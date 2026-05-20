// multipart.zig — SIMD-accelerated multipart/form-data parser.
//
// Parses multipart form data (file uploads) by scanning for boundary
// strings using SIMD. Returns a Python list of (name, filename, content_type, data) tuples.

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

const allocator = std.heap.c_allocator;

/// parse_multipart(body_bytes, boundary_str) → list of (name, filename|None, content_type, data)
/// Python FFI entry point — unpacks PyArg_ParseTuple then delegates to parseMultipartFromBuffer.
pub fn parse_multipart(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var body_ptr: [*c]const u8 = null;
    var body_len: c.Py_ssize_t = 0;
    var boundary_ptr: [*c]const u8 = null;
    var boundary_len: c.Py_ssize_t = 0;

    // Accept both bytes (y#) and str (s#) — try bytes first
    if (c.PyArg_ParseTuple(args, "y#s#", &body_ptr, &body_len, &boundary_ptr, &boundary_len) == 0) {
        c.PyErr_Clear();
        if (c.PyArg_ParseTuple(args, "s#s#", &body_ptr, &body_len, &boundary_ptr, &boundary_len) == 0) return null;
    }

    if (body_len <= 0 or boundary_len <= 0) {
        return c.PyList_New(0);
    }

    const body: []const u8 = body_ptr[0..@intCast(body_len)];
    const boundary: []const u8 = boundary_ptr[0..@intCast(boundary_len)];
    return parseMultipartFromBuffer(body, boundary);
}

/// Parse multipart body from raw Zig slices.
/// Called from both the Python FFI entry point and server.zig pre-dispatch.
/// Returns a Python list of (name, filename|None, content_type, data) tuples.
/// Caller owns the returned reference. Returns null on allocation failure.
pub fn parseMultipartFromBuffer(body: []const u8, boundary: []const u8) ?*c.PyObject {
    if (body.len == 0 or boundary.len == 0) {
        return c.PyList_New(0);
    }

    // Full delimiter: \r\n--boundary
    var delim_buf: [256]u8 = undefined;
    if (boundary.len + 4 > delim_buf.len) {
        py.setError("multipart: boundary too long", .{});
        return null;
    }
    delim_buf[0] = '\r';
    delim_buf[1] = '\n';
    delim_buf[2] = '-';
    delim_buf[3] = '-';
    @memcpy(delim_buf[4..][0..boundary.len], boundary);
    const delim = delim_buf[0 .. 4 + boundary.len];

    // Also handle first boundary without leading \r\n
    var first_delim_buf: [256]u8 = undefined;
    first_delim_buf[0] = '-';
    first_delim_buf[1] = '-';
    @memcpy(first_delim_buf[2..][0..boundary.len], boundary);
    const first_delim = first_delim_buf[0 .. 2 + boundary.len];

    const result_list = c.PyList_New(0) orelse return null;

    // Find first boundary (SIMD-accelerated)
    var pos: usize = 0;
    if (findBoundarySIMD(body, first_delim)) |first_pos| {
        pos = first_pos + first_delim.len;
        // Skip trailing -- or \r\n after boundary
        if (pos + 2 <= body.len and body[pos] == '-' and body[pos + 1] == '-') {
            return result_list; // Final boundary, empty form
        }
        if (pos + 2 <= body.len and body[pos] == '\r' and body[pos + 1] == '\n') {
            pos += 2;
        }
    } else {
        return result_list; // No boundary found
    }

    // Parse each part
    while (pos < body.len) {
        // Find next boundary — SIMD-accelerated for large bodies
        const next_boundary = findBoundarySIMD(body[pos..], delim);
        const part_end = if (next_boundary) |nb| pos + nb else body.len;
        const part = body[pos..part_end];

        // Parse part: headers \r\n\r\n body
        if (std.mem.indexOf(u8, part, "\r\n\r\n")) |header_end| {
            const headers = part[0..header_end];
            const part_body = part[header_end + 4 ..];

            // Extract Content-Disposition
            var name: ?[]const u8 = null;
            var filename: ?[]const u8 = null;
            var content_type: []const u8 = "application/octet-stream";

            var line_it = std.mem.splitSequence(u8, headers, "\r\n");
            while (line_it.next()) |line| {
                if (std.ascii.startsWithIgnoreCase(line, "content-disposition:")) {
                    name = extractParam(line, "name");
                    // Try standard filename="..." first, then RFC 5987 filename*=
                    filename = extractParam(line, "filename") orelse
                        extractParamEncoded(line, "filename");
                } else if (std.ascii.startsWithIgnoreCase(line, "content-type:")) {
                    const ct = std.mem.trimStart(u8, line["content-type:".len..], " ");
                    if (ct.len > 0) content_type = ct;
                }
            }

            if (name) |n| {
                const py_name = py.newString(n) orelse {
                    c.Py_DecRef(result_list);
                    return null;
                };
                const py_filename = if (filename) |f| py.newString(f) orelse {
                    c.Py_DecRef(py_name);
                    c.Py_DecRef(result_list);
                    return null;
                } else py.pyNone();
                const py_ct = py.newString(content_type) orelse {
                    c.Py_DecRef(py_name);
                    c.Py_DecRef(py_filename);
                    c.Py_DecRef(result_list);
                    return null;
                };
                const py_data = py.newBytes(part_body) orelse {
                    c.Py_DecRef(py_name);
                    c.Py_DecRef(py_filename);
                    c.Py_DecRef(py_ct);
                    c.Py_DecRef(result_list);
                    return null;
                };

                const tuple = c.PyTuple_Pack(4, py_name, py_filename, py_ct, py_data);
                c.Py_DecRef(py_name);
                c.Py_DecRef(py_filename);
                c.Py_DecRef(py_ct);
                c.Py_DecRef(py_data);

                if (tuple) |t| {
                    _ = c.PyList_Append(result_list, t);
                    c.Py_DecRef(t);
                }
            }
        }

        // Move past this boundary
        if (next_boundary) |nb| {
            pos = pos + nb + delim.len;
            // Check for final --
            if (pos + 2 <= body.len and body[pos] == '-' and body[pos + 1] == '-') {
                break;
            }
            // Skip \r\n after boundary
            if (pos + 2 <= body.len and body[pos] == '\r' and body[pos + 1] == '\n') {
                pos += 2;
            }
        } else {
            break;
        }
    }

    return result_list;
}

/// SIMD-accelerated boundary scanning.
/// Searches for the first byte of the delimiter using 16-byte SIMD vectors,
/// then verifies the full match. Falls back to byte-by-byte for short data.
fn findBoundarySIMD(data: []const u8, delim: []const u8) ?usize {
    if (delim.len == 0) return null;
    if (data.len < delim.len) return null;

    const first_byte = delim[0];
    const search_len = data.len - delim.len + 1;

    // For small data (< 64 bytes), use std.mem.indexOf directly — no SIMD overhead
    if (search_len < 64) {
        return std.mem.indexOf(u8, data, delim);
    }

    // SIMD: scan 16 bytes at a time for the first byte of the delimiter
    const V = @Vector(16, u8);
    const needle: V = @splat(first_byte);

    var i: usize = 0;
    // Process 16-byte chunks
    while (i + 16 <= search_len) {
        const chunk: V = data[i..][0..16].*;
        const matches = chunk == needle;
        const mask: u16 = @bitCast(matches);

        if (mask != 0) {
            // Found at least one match — check each position
            var m = mask;
            while (m != 0) {
                const bit_pos: u4 = @intCast(@ctz(m));
                const candidate = i + bit_pos;
                // Verify full delimiter match
                if (candidate + delim.len <= data.len) {
                    if (std.mem.eql(u8, data[candidate..][0..delim.len], delim)) {
                        return candidate;
                    }
                }
                m &= m - 1; // clear lowest set bit
            }
        }
        i += 16;
    }

    // Handle remaining bytes
    while (i < search_len) {
        if (data[i] == first_byte) {
            if (i + delim.len <= data.len and std.mem.eql(u8, data[i..][0..delim.len], delim)) {
                return i;
            }
        }
        i += 1;
    }

    return null;
}

fn extractParam(header: []const u8, param_name: []const u8) ?[]const u8 {
    // Case-insensitive search for param_name="value" — no heap allocation.
    // Slides a window across the header comparing char-by-char with toLower.
    // Handles escaped quotes (\" inside the value) by skipping them.
    const needle_len = param_name.len + 2; // name="
    if (header.len < needle_len) return null;

    var pos: usize = 0;
    outer: while (pos + needle_len <= header.len) {
        // The param name must sit at a token boundary: the preceding char must
        // be a separator (start-of-string, ';', or whitespace). Without this,
        // searching "name" matches the "name" inside "filename" and steals the
        // filename's value whenever filename= precedes name= (valid per RFC
        // 7578, which does not fix param order).
        if (pos > 0) {
            const prev = header[pos - 1];
            if (prev != ';' and prev != ' ' and prev != '\t') {
                pos += 1;
                continue :outer;
            }
        }
        // Check if param_name matches at pos (case-insensitive)
        for (param_name, 0..) |ch, j| {
            if (std.ascii.toLower(header[pos + j]) != std.ascii.toLower(ch)) {
                pos += 1;
                continue :outer;
            }
        }
        // Check for =" after the name
        if (header[pos + param_name.len] == '=' and header[pos + param_name.len + 1] == '"') {
            const val_start = pos + needle_len;
            // Find closing quote, skipping escaped quotes (\")
            var scan = val_start;
            while (scan < header.len) {
                if (header[scan] == '"' and (scan == val_start or header[scan - 1] != '\\')) {
                    return header[val_start..scan];
                }
                scan += 1;
            }
            return null;
        }
        pos += 1;
    }
    return null;
}

/// Extract RFC 5987 encoded parameter: param_name*=charset'language'value
/// Example: filename*=UTF-8''%E4%B8%AD%E6%96%87.txt
/// Returns the percent-decoded value. Only UTF-8 charset is supported.
/// Uses a stack buffer for decoding — no heap allocation.
fn extractParamEncoded(header: []const u8, param_name: []const u8) ?[]const u8 {
    // Search for param_name*= (case-insensitive)
    const needle_len = param_name.len + 2; // name*=
    if (header.len < needle_len) return null;

    var pos: usize = 0;
    outer: while (pos + needle_len <= header.len) {
        for (param_name, 0..) |ch, j| {
            if (std.ascii.toLower(header[pos + j]) != std.ascii.toLower(ch)) {
                pos += 1;
                continue :outer;
            }
        }
        if (header[pos + param_name.len] == '*' and header[pos + param_name.len + 1] == '=') {
            const val_start = pos + needle_len;
            // Find the end of the value (next ; or end of header)
            const val_end = std.mem.indexOfScalarPos(u8, header, val_start, ';') orelse header.len;
            const raw = std.mem.trim(u8, header[val_start..val_end], " \t");
            // Skip charset'language' prefix — find the second single quote
            var quote_count: usize = 0;
            var data_start: usize = 0;
            for (raw, 0..) |ch, i| {
                if (ch == '\'') {
                    quote_count += 1;
                    if (quote_count == 2) {
                        data_start = i + 1;
                        break;
                    }
                }
            }
            if (quote_count < 2) return null;
            // Return the raw percent-encoded value — Python side will decode
            return raw[data_start..];
        }
        pos += 1;
    }
    return null;
}
