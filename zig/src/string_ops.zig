// string_ops.zig — SIMD-accelerated string operations for Python.
//
// html_escape: 16-byte SIMD scan for <, >, &, ", '
// url_encode: 16-byte SIMD scan for unreserved chars (passthrough) vs encode
// url_decode: percent-decode with hex digit SIMD
// parse_query_string: SIMD & and = delimiter scan
// xor_bytes: 32-byte SIMD XOR with cyclically repeating mask

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

const allocator = std.heap.c_allocator;

// ── html_escape ──────────────────────────────────────────────────────────────

const html_replacements = [5]struct { char: u8, repl: []const u8 }{
    .{ .char = '&', .repl = "&amp;" },
    .{ .char = '<', .repl = "&lt;" },
    .{ .char = '>', .repl = "&gt;" },
    .{ .char = '"', .repl = "&quot;" },
    .{ .char = '\'', .repl = "&#x27;" },
};

pub fn html_escape(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var str_ptr: [*c]const u8 = null;
    var str_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#", &str_ptr, &str_len) == 0) return null;

    if (str_len <= 0) return py.newString("");

    const input: []const u8 = str_ptr[0..@intCast(str_len)];

    // Worst case: every char becomes &#x27; (6 bytes)
    const buf = allocator.alloc(u8, input.len * 6) catch return null;
    defer allocator.free(buf);
    var out: usize = 0;

    const Block16 = @Vector(16, u8);
    const amp: Block16 = @splat('&');
    const lt: Block16 = @splat('<');
    const gt: Block16 = @splat('>');
    const quot: Block16 = @splat('"');
    const apos: Block16 = @splat('\'');

    var i: usize = 0;

    // SIMD: 16 bytes at a time
    while (i + 16 <= input.len) {
        const chunk: Block16 = input[i..][0..16].*;
        const need_escape = (chunk == amp) | (chunk == lt) | (chunk == gt) | (chunk == quot) | (chunk == apos);
        if (!@reduce(.Or, need_escape)) {
            // No special chars — bulk copy
            @memcpy(buf[out..][0..16], input[i..][0..16]);
            out += 16;
            i += 16;
        } else {
            // Has special chars — scalar this chunk
            for (input[i..][0..16]) |ch| {
                switch (ch) {
                    '&' => {
                        @memcpy(buf[out..][0..5], "&amp;");
                        out += 5;
                    },
                    '<' => {
                        @memcpy(buf[out..][0..4], "&lt;");
                        out += 4;
                    },
                    '>' => {
                        @memcpy(buf[out..][0..4], "&gt;");
                        out += 4;
                    },
                    '"' => {
                        @memcpy(buf[out..][0..6], "&quot;");
                        out += 6;
                    },
                    '\'' => {
                        @memcpy(buf[out..][0..6], "&#x27;");
                        out += 6;
                    },
                    else => {
                        buf[out] = ch;
                        out += 1;
                    },
                }
            }
            i += 16;
        }
    }

    // Scalar tail
    while (i < input.len) {
        const ch = input[i];
        switch (ch) {
            '&' => {
                @memcpy(buf[out..][0..5], "&amp;");
                out += 5;
            },
            '<' => {
                @memcpy(buf[out..][0..4], "&lt;");
                out += 4;
            },
            '>' => {
                @memcpy(buf[out..][0..4], "&gt;");
                out += 4;
            },
            '"' => {
                @memcpy(buf[out..][0..6], "&quot;");
                out += 6;
            },
            '\'' => {
                @memcpy(buf[out..][0..6], "&#x27;");
                out += 6;
            },
            else => {
                buf[out] = ch;
                out += 1;
            },
        }
        i += 1;
    }

    return py.newString(buf[0..out]);
}

// ── url_encode ───────────────────────────────────────────────────────────────

fn isUnreserved(ch: u8) bool {
    return (ch >= 'A' and ch <= 'Z') or (ch >= 'a' and ch <= 'z') or
        (ch >= '0' and ch <= '9') or ch == '-' or ch == '_' or ch == '.' or ch == '~';
}

const hex_digits = "0123456789ABCDEF";

pub fn url_encode(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var str_ptr: [*c]const u8 = null;
    var str_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#", &str_ptr, &str_len) == 0) return null;

    if (str_len <= 0) return py.newString("");

    const input: []const u8 = str_ptr[0..@intCast(str_len)];

    // Worst case: every byte becomes %XX (3x)
    const buf = allocator.alloc(u8, input.len * 3) catch return null;
    defer allocator.free(buf);
    var out: usize = 0;

    const Block16 = @Vector(16, u8);

    var i: usize = 0;

    // SIMD fast path: check 16 bytes at a time for all-unreserved
    while (i + 16 <= input.len) {
        const chunk: Block16 = input[i..][0..16].*;

        // Check if all chars are unreserved (alphanumeric or -_.~)
        const ge_A = chunk >= @as(Block16, @splat('A'));
        const le_Z = chunk <= @as(Block16, @splat('Z'));
        const ge_a = chunk >= @as(Block16, @splat('a'));
        const le_z = chunk <= @as(Block16, @splat('z'));
        const ge_0 = chunk >= @as(Block16, @splat('0'));
        const le_9 = chunk <= @as(Block16, @splat('9'));
        const is_dash = chunk == @as(Block16, @splat('-'));
        const is_under = chunk == @as(Block16, @splat('_'));
        const is_dot = chunk == @as(Block16, @splat('.'));
        const is_tilde = chunk == @as(Block16, @splat('~'));

        const is_upper = ge_A & le_Z;
        const is_lower = ge_a & le_z;
        const is_digit = ge_0 & le_9;
        const is_unreserved = is_upper | is_lower | is_digit | is_dash | is_under | is_dot | is_tilde;

        if (@reduce(.And, is_unreserved)) {
            // All unreserved — bulk copy
            @memcpy(buf[out..][0..16], input[i..][0..16]);
            out += 16;
            i += 16;
        } else {
            // Has reserved chars — scalar this chunk
            for (input[i..][0..16]) |ch| {
                if (isUnreserved(ch)) {
                    buf[out] = ch;
                    out += 1;
                } else {
                    buf[out] = '%';
                    buf[out + 1] = hex_digits[ch >> 4];
                    buf[out + 2] = hex_digits[ch & 0x0F];
                    out += 3;
                }
            }
            i += 16;
        }
    }

    // Scalar tail
    while (i < input.len) {
        const ch = input[i];
        if (isUnreserved(ch)) {
            buf[out] = ch;
            out += 1;
        } else {
            buf[out] = '%';
            buf[out + 1] = hex_digits[ch >> 4];
            buf[out + 2] = hex_digits[ch & 0x0F];
            out += 3;
        }
        i += 1;
    }

    return py.newString(buf[0..out]);
}

// ── url_decode ───────────────────────────────────────────────────────────────

fn hexVal(ch: u8) ?u8 {
    if (ch >= '0' and ch <= '9') return ch - '0';
    if (ch >= 'A' and ch <= 'F') return ch - 'A' + 10;
    if (ch >= 'a' and ch <= 'f') return ch - 'a' + 10;
    return null;
}

pub fn url_decode(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var str_ptr: [*c]const u8 = null;
    var str_len: c.Py_ssize_t = 0;
    // "s#" is fine here because input is percent-encoded ASCII
    if (c.PyArg_ParseTuple(args, "s#", &str_ptr, &str_len) == 0) return null;

    if (str_len <= 0) return py.newString("");

    const input: []const u8 = str_ptr[0..@intCast(str_len)];

    // Fast path: if no % at all, return input directly (zero-copy)
    if (std.mem.indexOfScalar(u8, input, '%') == null) {
        return py.newString(input);
    }

    const buf = allocator.alloc(u8, input.len) catch return null;
    defer allocator.free(buf);
    var out: usize = 0;
    var i: usize = 0;

    // SIMD fast path: scan 16 bytes for %
    const Block16 = @Vector(16, u8);
    const pct: Block16 = @splat('%');

    while (i + 16 <= input.len) {
        const chunk: Block16 = input[i..][0..16].*;
        const has_pct = @reduce(.Or, chunk == pct);

        if (!has_pct) {
            // No % — bulk copy
            @memcpy(buf[out..][0..16], input[i..][0..16]);
            out += 16;
            i += 16;
        } else {
            // Has encoded chars — go scalar for this section
            break;
        }
    }

    // Scalar for remainder (or from where SIMD stopped)
    // url_decode does NOT convert + to space (that's unquote_plus / form decoding)
    while (i < input.len) {
        if (input[i] == '%' and i + 2 < input.len) {
            const hi = hexVal(input[i + 1]);
            const lo = hexVal(input[i + 2]);
            if (hi != null and lo != null) {
                buf[out] = (hi.? << 4) | lo.?;
                out += 1;
                i += 3;
                continue;
            }
        }
        buf[out] = input[i];
        out += 1;
        i += 1;
    }

    // Decoded bytes may contain non-UTF-8 (e.g., %A0 → 0xa0).
    // Try UTF-8 first, fall back to latin-1 for arbitrary byte values.
    return py.newString(buf[0..out]) orelse blk: {
        c.PyErr_Clear();
        break :blk c.PyUnicode_DecodeLatin1(@ptrCast(buf[0..out].ptr), @intCast(out), null);
    };
}

// ── parse_query_string ───────────────────────────────────────────────────────

pub fn parse_query_string(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    // Accept any string — ASGI passes query_string as bytes decoded via latin-1,
    // which can contain byte values 0x80-0xFF that aren't valid UTF-8.
    // Use "O" and try UTF-8 first, fall back to latin-1 encoding.
    var obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O", &obj) == 0) return null;

    var str_ptr: [*c]const u8 = null;
    var str_len: c.Py_ssize_t = 0;
    var latin1_encoded: ?*c.PyObject = null;

    // Try UTF-8 first (fast path — covers 99%+ of real query strings)
    str_ptr = c.PyUnicode_AsUTF8AndSize(obj, &str_len);
    if (str_ptr == null) {
        // Non-UTF-8 — encode to latin-1 bytes (lossless for codepoints 0-255)
        c.PyErr_Clear();
        latin1_encoded = c.PyUnicode_AsLatin1String(obj) orelse return null;
        var enc_len: c.Py_ssize_t = 0;
        var enc_ptr: [*c]u8 = undefined;
        if (c.PyBytes_AsStringAndSize(latin1_encoded.?, @ptrCast(&enc_ptr), &enc_len) < 0) {
            c.Py_DecRef(latin1_encoded.?);
            return null;
        }
        str_ptr = enc_ptr;
        str_len = enc_len;
    }
    defer if (latin1_encoded) |e| c.Py_DecRef(e);

    const dict = py.newDict() orelse return null;

    if (str_len <= 0) return dict;

    const input: []const u8 = str_ptr[0..@intCast(str_len)];

    // Split on & then split each part on =
    var pair_it = std.mem.splitScalar(u8, input, '&');
    while (pair_it.next()) |pair| {
        if (pair.len == 0) continue;

        var key: []const u8 = undefined;
        var value: []const u8 = "";

        if (std.mem.indexOfScalar(u8, pair, '=')) |eq_pos| {
            key = pair[0..eq_pos];
            value = pair[eq_pos + 1 ..];
        } else {
            key = pair;
        }

        // URL-decode key and value
        const decoded_key = decodeInPlace(key) catch continue;
        defer allocator.free(decoded_key);
        const decoded_val = decodeInPlace(value) catch continue;
        defer allocator.free(decoded_val);

        // Use UTF-8 decoding first, fall back to latin-1 for non-UTF-8 bytes
        const py_key = py.newString(decoded_key) orelse blk: {
            c.PyErr_Clear();
            break :blk c.PyUnicode_DecodeLatin1(@ptrCast(decoded_key.ptr), @intCast(decoded_key.len), null);
        } orelse {
            c.Py_DecRef(dict);
            return null;
        };
        const py_val = py.newString(decoded_val) orelse blk: {
            c.PyErr_Clear();
            break :blk c.PyUnicode_DecodeLatin1(@ptrCast(decoded_val.ptr), @intCast(decoded_val.len), null);
        } orelse {
            c.Py_DecRef(py_key);
            c.Py_DecRef(dict);
            return null;
        };

        // Get or create list for this key
        const existing = c.PyDict_GetItem(dict, py_key);
        if (existing) |list| {
            _ = c.PyList_Append(list, py_val);
        } else {
            const list = c.PyList_New(1) orelse {
                c.Py_DecRef(py_key);
                c.Py_DecRef(py_val);
                c.Py_DecRef(dict);
                return null;
            };
            // PyList_SET_ITEM steals the reference
            _ = c.PyList_SetItem(list, 0, py_val);
            _ = c.PyDict_SetItem(dict, py_key, list);
            c.Py_DecRef(list);
            c.Py_DecRef(py_key);
            continue; // py_val ownership transferred to list
        }
        c.Py_DecRef(py_key);
        c.Py_DecRef(py_val);
    }

    return dict;
}

fn decodeInPlace(input: []const u8) ![]u8 {
    const buf = try allocator.alloc(u8, input.len);
    var out: usize = 0;
    var i: usize = 0;

    while (i < input.len) {
        if (input[i] == '%' and i + 2 < input.len) {
            const hi = hexVal(input[i + 1]);
            const lo = hexVal(input[i + 2]);
            if (hi != null and lo != null) {
                buf[out] = (hi.? << 4) | lo.?;
                out += 1;
                i += 3;
                continue;
            }
        }
        if (input[i] == '+') {
            buf[out] = ' ';
            out += 1;
            i += 1;
            continue;
        }
        buf[out] = input[i];
        out += 1;
        i += 1;
    }

    // Shrink to actual size
    if (out < buf.len) {
        const result = try allocator.alloc(u8, out);
        @memcpy(result, buf[0..out]);
        allocator.free(buf);
        return result;
    }
    return buf;
}

// ── Cookie Parsing ──────────────────────────────────────────────────────

/// Trim leading/trailing ASCII whitespace from a byte slice.
fn trimWhitespace(s: []const u8) []const u8 {
    var start: usize = 0;
    while (start < s.len and (s[start] == ' ' or s[start] == '\t')) {
        start += 1;
    }
    var end: usize = s.len;
    while (end > start and (s[end - 1] == ' ' or s[end - 1] == '\t')) {
        end -= 1;
    }
    return s[start..end];
}

/// Parse a Cookie header string into a Python dict.
/// Split on ';', then split each pair on '='. Keys and values are trimmed.
/// Values are URL percent-decoded (but '+' is NOT decoded to space for cookies,
/// unlike query strings — cookies use literal '+').
pub fn parse_cookies(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var str_ptr: [*c]const u8 = null;
    var str_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#", &str_ptr, &str_len) == 0) return null;

    const dict = py.newDict() orelse return null;

    if (str_len <= 0) return dict;

    const input: []const u8 = str_ptr[0..@intCast(str_len)];

    // Split on ';' then each pair on '='
    var pair_it = std.mem.splitScalar(u8, input, ';');
    while (pair_it.next()) |raw_pair| {
        const pair = trimWhitespace(raw_pair);
        if (pair.len == 0) continue;

        var key: []const u8 = undefined;
        var value: []const u8 = "";

        if (std.mem.indexOfScalar(u8, pair, '=')) |eq_pos| {
            key = trimWhitespace(pair[0..eq_pos]);
            value = trimWhitespace(pair[eq_pos + 1 ..]);
        } else {
            key = pair;
        }

        if (key.len == 0) continue;

        // Percent-decode value (cookies use %XX encoding, but '+' stays literal)
        const decoded_val = decodeCookieValue(value) catch continue;
        defer allocator.free(decoded_val);

        const py_key = py.newString(key) orelse {
            c.Py_DecRef(dict);
            return null;
        };
        const py_val = py.newString(decoded_val) orelse {
            c.Py_DecRef(py_key);
            c.Py_DecRef(dict);
            return null;
        };

        // Last cookie with same name wins (standard behavior)
        _ = c.PyDict_SetItem(dict, py_key, py_val);
        c.Py_DecRef(py_key);
        c.Py_DecRef(py_val);
    }

    return dict;
}

/// Decode cookie value: percent-decode %XX sequences, but leave '+' as literal.
fn decodeCookieValue(input: []const u8) ![]u8 {
    const buf = try allocator.alloc(u8, input.len);
    var out: usize = 0;
    var i: usize = 0;

    while (i < input.len) {
        if (input[i] == '%' and i + 2 < input.len) {
            const hi = hexVal(input[i + 1]);
            const lo = hexVal(input[i + 2]);
            if (hi != null and lo != null) {
                buf[out] = (hi.? << 4) | lo.?;
                out += 1;
                i += 3;
                continue;
            }
        }
        // '+' stays literal in cookies (NOT converted to space)
        buf[out] = input[i];
        out += 1;
        i += 1;
    }

    if (out < buf.len) {
        const result = try allocator.alloc(u8, out);
        @memcpy(result, buf[0..out]);
        allocator.free(buf);
        return result;
    }
    return buf;
}

// ── Base Encode/Decode ──────────────────────────────────────────────────────
//
// Arbitrary-base encoder with tiered fast paths:
//   1. Values ≤ u64:  pure native integer math (no Python objects in the loop)
//   2. Values ≤ u128: pure native 128-bit math (single division instruction on aarch64)
//   3. Larger values: fallback to Python PyLong arithmetic
//
// This means typical application IDs (database PKs, random tokens up to 128 bits)
// never touch Python's object system during the encode/decode loop.
//
// base_encode(value: int, alphabet: str) -> str
// base_decode(code: str, alphabet: str) -> int

/// Pure native encode for values that fit in u128.
/// Returns number of characters written to buf (in reverse order).
fn encodeNative128(value: u128, alphabet: []const u8, buf: []u8) usize {
    const base: u128 = @intCast(alphabet.len);
    var v = value;
    var pos: usize = 0;
    while (v > 0) {
        buf[pos] = alphabet[@intCast(v % base)];
        v /= base;
        pos += 1;
    }
    return pos;
}

/// Pure native decode for encoded strings that will fit in u128.
/// Returns null if overflow would occur, signaling caller to use PyLong fallback.
fn decodeNative128(code: []const u8, lookup: *const [256]u8, base_val: u128) ?u128 {
    var result: u128 = 0;
    for (code) |ch| {
        const idx = lookup[ch];
        if (idx == 0xFF) return null; // invalid char — caller handles error

        // Check for overflow before multiply
        if (result > std.math.maxInt(u128) / base_val) return null;
        const mul = result *% base_val;
        const add = mul +% @as(u128, idx);
        if (add < mul) return null; // overflow on add
        result = add;
    }
    return result;
}

pub fn base_encode(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var value_obj: ?*c.PyObject = null;
    var alpha_ptr: [*c]const u8 = null;
    var alpha_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "Os#", &value_obj, &alpha_ptr, &alpha_len) == 0) return null;

    const alphabet: []const u8 = alpha_ptr[0..@intCast(alpha_len)];
    const base: usize = alphabet.len;
    if (base < 2) {
        c.PyErr_SetString(c.PyExc_ValueError, "Alphabet must have at least 2 characters");
        return null;
    }

    // Try to extract as u64 first (covers ~99% of real-world IDs)
    const as_u64 = c.PyLong_AsUnsignedLongLong(value_obj);
    if (c.PyErr_Occurred() == null) {
        // Fast path: value fits in u64 — pure native math
        if (as_u64 == 0) return py.newString(alphabet[0..1]);

        var buf: [64]u8 = undefined;
        const n = encodeNative128(@intCast(as_u64), alphabet, &buf);

        // Reverse
        var lo: usize = 0;
        var hi: usize = n - 1;
        while (lo < hi) {
            const tmp = buf[lo];
            buf[lo] = buf[hi];
            buf[hi] = tmp;
            lo += 1;
            hi -= 1;
        }
        return py.newString(buf[0..n]);
    }

    // Clear the OverflowError from PyLong_AsUnsignedLongLong
    c.PyErr_Clear();

    // Check for negative (PyLong_AsUnsignedLongLong fails for negative too)
    const py_zero = c.PyLong_FromLong(0) orelse return null;
    defer c.Py_DECREF(py_zero);
    const neg_cmp = c.PyObject_RichCompareBool(value_obj, py_zero, c.Py_LT);
    if (neg_cmp < 0) return null;
    if (neg_cmp == 1) {
        c.PyErr_SetString(c.PyExc_ValueError, "Cannot encode negative value");
        return null;
    }

    // Try u128 path: extract as two 64-bit halves
    // Python int to bytes, then reinterpret as u128
    const nbits_obj = c.PyObject_CallMethod(value_obj, "bit_length", null) orelse return null;
    const nbits: c_long = c.PyLong_AsLong(nbits_obj);
    c.Py_DECREF(nbits_obj);
    if (nbits <= 128) {
        // Extract via to_bytes
        const nbytes = @as(c_long, @intCast((@as(u64, @intCast(nbits)) + 7) / 8));
        const bytes_obj = c.PyObject_CallMethod(value_obj, "to_bytes", "lz", nbytes, "big") orelse return null;
        defer c.Py_DECREF(bytes_obj);

        var buf_ptr: [*c]u8 = null;
        var buf_len: c.Py_ssize_t = 0;
        if (c.PyBytes_AsStringAndSize(bytes_obj, @ptrCast(&buf_ptr), &buf_len) < 0) return null;

        const bytes_slice: []const u8 = buf_ptr[0..@intCast(buf_len)];
        var val128: u128 = 0;
        for (bytes_slice) |b| {
            val128 = (val128 << 8) | @as(u128, b);
        }

        // Native u128 encode
        var out: [64]u8 = undefined;
        const n = encodeNative128(val128, alphabet, &out);

        var lo: usize = 0;
        var hi: usize = n - 1;
        while (lo < hi) {
            const tmp = out[lo];
            out[lo] = out[hi];
            out[hi] = tmp;
            lo += 1;
            hi -= 1;
        }
        return py.newString(out[0..n]);
    }

    // Slow path: value > 128 bits — use Python PyLong divmod
    var stack_buf: [128]u8 = undefined;
    var heap_buf: ?[]u8 = null;
    defer if (heap_buf) |hb| allocator.free(hb);

    var out_buf: []u8 = &stack_buf;
    var pos: usize = 0;

    const py_base = c.PyLong_FromUnsignedLongLong(@intCast(base)) orelse return null;
    defer c.Py_DECREF(py_base);

    var current = value_obj;
    c.Py_INCREF(current);

    while (true) {
        const dm = c.PyNumber_Divmod(current, py_base) orelse {
            c.Py_DECREF(current);
            return null;
        };

        const py_quot = c.PyTuple_GetItem(dm, 0);
        const py_rem = c.PyTuple_GetItem(dm, 1);

        const rem_val: usize = @intCast(c.PyLong_AsUnsignedLongLong(py_rem));
        if (c.PyErr_Occurred() != null) {
            c.Py_DECREF(dm);
            c.Py_DECREF(current);
            return null;
        }

        if (pos >= out_buf.len) {
            const new_size = out_buf.len * 2;
            const new_buf = allocator.alloc(u8, new_size) catch {
                c.Py_DECREF(dm);
                c.Py_DECREF(current);
                return null;
            };
            @memcpy(new_buf[0..pos], out_buf[0..pos]);
            if (heap_buf) |hb| allocator.free(hb);
            heap_buf = new_buf;
            out_buf = new_buf;
        }

        out_buf[pos] = alphabet[rem_val];
        pos += 1;

        c.Py_DECREF(current);
        current = py_quot;
        c.Py_INCREF(current);
        c.Py_DECREF(dm);

        const is_zero = c.PyObject_RichCompareBool(current, py_zero, c.Py_EQ);
        if (is_zero < 0) {
            c.Py_DECREF(current);
            return null;
        }
        if (is_zero == 1) break;
    }
    c.Py_DECREF(current);

    // Reverse
    var lo: usize = 0;
    var hi: usize = pos - 1;
    while (lo < hi) {
        const tmp = out_buf[lo];
        out_buf[lo] = out_buf[hi];
        out_buf[hi] = tmp;
        lo += 1;
        hi -= 1;
    }

    return py.newString(out_buf[0..pos]);
}

pub fn base_decode(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var code_ptr: [*c]const u8 = null;
    var code_len: c.Py_ssize_t = 0;
    var alpha_ptr: [*c]const u8 = null;
    var alpha_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "s#s#", &code_ptr, &code_len, &alpha_ptr, &alpha_len) == 0) return null;

    const code: []const u8 = code_ptr[0..@intCast(code_len)];
    const alphabet: []const u8 = alpha_ptr[0..@intCast(alpha_len)];
    const base_val: usize = alphabet.len;

    if (code.len == 0) {
        c.PyErr_SetString(c.PyExc_ValueError, "Cannot decode empty string");
        return null;
    }

    // Build lookup table: char -> index (256 entries, 0xFF = not found)
    var lookup: [256]u8 = [_]u8{0xFF} ** 256;
    for (alphabet, 0..) |ch, idx| {
        lookup[ch] = @intCast(idx);
    }

    // Fast path: try native u128 decode (covers up to ~22 base-62 chars or ~25 base-32 chars)
    const native_result = decodeNative128(code, &lookup, @intCast(base_val));
    if (native_result) |val| {
        if (val <= std.math.maxInt(u64)) {
            // Fits in u64 — direct PyLong creation, no arithmetic
            return c.PyLong_FromUnsignedLongLong(@intCast(val));
        }
        // u128 but > u64: construct from high and low 64-bit halves
        // result = (high << 64) | low
        const high: u64 = @intCast(val >> 64);
        const low: u64 = @intCast(val & 0xFFFFFFFFFFFFFFFF);
        const py_high = c.PyLong_FromUnsignedLongLong(high) orelse return null;
        const py_64 = c.PyLong_FromLong(64) orelse {
            c.Py_DECREF(py_high);
            return null;
        };
        const shifted = c.PyNumber_Lshift(py_high, py_64) orelse {
            c.Py_DECREF(py_high);
            c.Py_DECREF(py_64);
            return null;
        };
        c.Py_DECREF(py_high);
        c.Py_DECREF(py_64);
        const py_low = c.PyLong_FromUnsignedLongLong(low) orelse {
            c.Py_DECREF(shifted);
            return null;
        };
        const result_obj = c.PyNumber_Or(shifted, py_low);
        c.Py_DECREF(shifted);
        c.Py_DECREF(py_low);
        return result_obj;
    }

    // Slow path: use Python PyLong arithmetic for values > 128 bits
    var result: ?*c.PyObject = c.PyLong_FromLong(0) orelse return null;
    const py_base_val = c.PyLong_FromUnsignedLongLong(@intCast(base_val)) orelse {
        c.Py_DECREF(result);
        return null;
    };
    defer c.Py_DECREF(py_base_val);

    for (code) |ch| {
        const idx = lookup[ch];
        if (idx == 0xFF) {
            c.Py_DECREF(result);
            c.PyErr_SetString(c.PyExc_ValueError, "Invalid character in encoded string");
            return null;
        }

        const mul_result = c.PyNumber_Multiply(result, py_base_val) orelse {
            c.Py_DECREF(result);
            return null;
        };
        c.Py_DECREF(result);

        const py_idx = c.PyLong_FromUnsignedLongLong(@intCast(idx)) orelse {
            c.Py_DECREF(mul_result);
            return null;
        };
        result = c.PyNumber_Add(mul_result, py_idx);
        c.Py_DECREF(mul_result);
        c.Py_DECREF(py_idx);
        if (result == null) return null;
    }

    return result;
}

// ── xor_bytes ───────────────────────────────────────────────────────────────
//
// XOR data bytes with a cyclically repeating mask.
// SIMD: processes 32 bytes/cycle when mask is exactly 32 bytes (common case:
// HMAC-SHA256 produces 32-byte masks). Falls back to 16-byte and scalar
// for other mask sizes and tails.
//
// Python signature: xor_bytes(data: bytes, mask: bytes) -> bytes
// The mask repeats cyclically to cover the full data length.

pub fn xor_bytes(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var data_ptr: [*c]const u8 = null;
    var data_len: c.Py_ssize_t = 0;
    var mask_ptr: [*c]const u8 = null;
    var mask_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "y#y#", &data_ptr, &data_len, &mask_ptr, &mask_len) == 0)
        return null;

    const dlen: usize = @intCast(data_len);
    const mlen: usize = @intCast(mask_len);

    if (mlen == 0) {
        c.PyErr_SetString(c.PyExc_ValueError.?, "mask must not be empty");
        return null;
    }

    // Empty data → empty bytes
    if (dlen == 0)
        return py.newBytes(&.{});

    const data: [*]const u8 = @ptrCast(data_ptr);
    const mask: [*]const u8 = @ptrCast(mask_ptr);

    const buf = allocator.alloc(u8, dlen) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer allocator.free(buf);

    // Fast path: 32-byte mask (HMAC-SHA256 output) — no modulo needed
    if (mlen == 32) {
        xor_mask32(buf.ptr, data, mask, dlen);
    } else {
        xor_generic(buf.ptr, data, mask, dlen, mlen);
    }

    return py.newBytes(buf[0..dlen]);
}

/// XOR with exactly 32-byte mask — SIMD 32 bytes/cycle, no modulo.
fn xor_mask32(out: [*]u8, data: [*]const u8, mask: [*]const u8, len: usize) void {
    const Block32 = @Vector(32, u8);
    const mask_v: Block32 = mask[0..32].*;
    var i: usize = 0;

    // SIMD: 32 bytes at a time, mask repeats perfectly
    while (i + 32 <= len) : (i += 32) {
        const chunk: Block32 = data[i..][0..32].*;
        const result = chunk ^ mask_v;
        out[i..][0..32].* = result;
    }

    // Scalar tail (< 32 bytes)
    while (i < len) : (i += 1) {
        out[i] = data[i] ^ mask[i % 32];
    }
}

/// XOR with arbitrary-length mask — 16-byte SIMD where possible.
fn xor_generic(out: [*]u8, data: [*]const u8, mask: [*]const u8, dlen: usize, mlen: usize) void {
    const Block16 = @Vector(16, u8);
    var i: usize = 0;

    // If mask >= 16 bytes, use SIMD for aligned mask chunks
    if (mlen >= 16) {
        while (i + 16 <= dlen) {
            // Build 16-byte mask chunk with cyclic wrap
            var mask_chunk: [16]u8 = undefined;
            const base = i % mlen;
            if (base + 16 <= mlen) {
                // Contiguous: fast copy
                mask_chunk = mask[base..][0..16].*;
            } else {
                // Wraps around: byte-by-byte
                for (&mask_chunk, 0..) |*b, j| {
                    b.* = mask[(base + j) % mlen];
                }
            }

            const data_v: Block16 = data[i..][0..16].*;
            const mask_v: Block16 = mask_chunk;
            const result = data_v ^ mask_v;
            out[i..][0..16].* = result;
            i += 16;
        }
    }

    // Scalar tail
    while (i < dlen) : (i += 1) {
        out[i] = data[i] ^ mask[i % mlen];
    }
}
