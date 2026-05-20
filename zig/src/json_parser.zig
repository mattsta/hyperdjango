// json_parser.zig — SIMD-accelerated JSON parser → Python objects.
//
// Converts JSON text from PostgreSQL JSONB columns into Python dict/list/str/
// int/float/bool/None — no intermediate Zig data structures, no Python
// json.loads() call. Goes straight from bytes to PyObject*.
//
// Uses SIMD primitives from simd_json_parser.zig:
//   - skipWhitespaceSIMD: 16-byte SIMD whitespace scanning
//   - extractString: 32-byte SIMD quote/backslash scanning
//   - parseInteger: 8-digit SIMD parallel digit processing
//   - processEscapes: escape sequence handling

const std = @import("std");
const py = @import("py.zig");
const c = py.c;
const simd = @import("dhi/simd_json_parser.zig");

const allocator = std.heap.c_allocator;

// ── Nesting depth policy (DoS bound, not a stack crutch) ─────────────────────
//
// The value parser below is ITERATIVE: container nesting is tracked on an
// explicit heap-backed stack (see FrameStack), NOT on the native call stack, so
// parse depth is bounded by heap memory rather than by the worker's C stack.
// A 50k-deep array therefore parses without ever overrunning the stack.
//
// We STILL enforce a depth bound, but as a deliberate resource/DoS policy:
// an over-deep document builds a correspondingly deep *Python* object graph,
// and later operations on it (traversal, serialization, deallocation, GC) walk
// CPython's own C stack. Bounding depth keeps that downstream object graph
// within what CPython safely handles and turns a pathological body into an
// ordinary parse ERROR (→ HTTP 400 / WS error) instead of a crash anywhere in
// the pipeline.
//
// Default is aligned with CPython's default recursion limit (1000): deep enough
// for any realistic payload, shallow enough that downstream Python recursion
// stays safe. Override via HYPER_JSON_MAX_DEPTH, clamped to a sane ceiling.
const default_max_depth: u32 = 1000;
const max_depth_ceiling: u32 = 10000;

// Cached, lazily-resolved effective limit. 0 = not yet resolved. The resolved
// value is always non-zero, so a benign race under free-threading just has
// multiple threads compute the same value.
var max_depth_cache: std.atomic.Value(u32) = std.atomic.Value(u32).init(0);

fn maxDepth() u32 {
    const cached = max_depth_cache.load(.monotonic);
    if (cached != 0) return cached;

    var result: u32 = default_max_depth;
    if (std.c.getenv("HYPER_JSON_MAX_DEPTH")) |env_ptr| {
        const env_val = std.mem.sliceTo(env_ptr, 0);
        if (std.fmt.parseInt(u32, env_val, 10)) |n| {
            if (n > 0) result = @min(n, max_depth_ceiling);
        } else |_| {}
    }
    max_depth_cache.store(result, .monotonic);
    return result;
}

/// Parse a JSON string and return a Python object (dict, list, str, int, float, bool, None).
/// Returns null on parse error (sets Python exception).
pub fn jsonToPython(json: []const u8) ?*c.PyObject {
    var pos: usize = 0;
    return parseValue(json, &pos);
}

// ── Iterative value parser (SIMD-accelerated) ────────────────────────────────
//
// An in-progress container: a Python list (array) or a Python dict (object)
// being built. For objects, `pending_key` holds the parsed key awaiting its
// value (an owned reference).
const Frame = struct {
    container: *c.PyObject,
    is_object: bool,
    pending_key: ?*c.PyObject,
};

// Explicit nesting stack. Small documents (nesting ≤ inline_cap) never touch the
// heap — the common shallow case stays allocation-free, matching or beating the
// old recursive parser. Deeper nesting spills to a heap slice that grows by
// doubling, so depth is bounded by heap, not by the native call stack.
const FrameStack = struct {
    inline_buf: [inline_cap]Frame = undefined,
    heap: ?[]Frame = null,
    len: usize = 0,

    const inline_cap = 32;

    fn active(self: *FrameStack) []Frame {
        return if (self.heap) |h| h else self.inline_buf[0..];
    }

    fn push(self: *FrameStack, f: Frame) bool {
        const cap = if (self.heap) |h| h.len else inline_cap;
        if (self.len == cap) {
            const new_heap = allocator.alloc(Frame, cap * 2) catch return false;
            @memcpy(new_heap[0..self.len], self.active()[0..self.len]);
            if (self.heap) |old| allocator.free(old);
            self.heap = new_heap;
        }
        self.active()[self.len] = f;
        self.len += 1;
        return true;
    }

    fn top(self: *FrameStack) *Frame {
        return &self.active()[self.len - 1];
    }

    fn pop(self: *FrameStack) void {
        self.len -= 1;
    }

    fn deinit(self: *FrameStack) void {
        if (self.heap) |h| allocator.free(h);
    }

    // Release every still-live container + pending key. Called on the error path
    // so a mid-parse failure never leaks the partially-built object graph.
    fn releaseAll(self: *FrameStack) void {
        for (self.active()[0..self.len]) |f| {
            c.Py_DecRef(f.container);
            if (f.pending_key) |k| c.Py_DecRef(k);
        }
    }
};

/// Parse a single JSON value at the given position, advancing pos past it.
/// Public so model_validator can use it for single-pass JSON → model. The parse
/// is iterative (no self-recursion), so arbitrarily-nested input is bounded by
/// the depth policy (maxDepth) and never by the native stack — an over-deep
/// document yields a normal parse error, not a crash.
pub fn parseValue(json: []const u8, pos: *usize) ?*c.PyObject {
    // Scalar fast path: a value that is not a container needs no nesting stack.
    // Covers JSONB scalar cells (numbers, strings, bools, null) with zero
    // stack setup — as cheap as the old direct-dispatch path.
    pos.* = simd.skipWhitespaceSIMD(json, pos.*);
    if (pos.* >= json.len) {
        py.setError("JSON: unexpected end of input", .{});
        return null;
    }
    switch (json[pos.*]) {
        '{', '[' => {}, // containers fall through to the iterative parser
        '"' => return parseString(json, pos),
        't' => return parseTrue(json, pos),
        'f' => return parseFalse(json, pos),
        'n' => return parseNull(json, pos),
        '-', '0'...'9' => return parseNumber(json, pos),
        else => {
            py.setError("JSON: unexpected character at position {d}", .{pos.*});
            return null;
        },
    }

    var stack: FrameStack = .{};
    defer stack.deinit();

    return parseValueImpl(json, pos, &stack) catch {
        stack.releaseAll();
        // Every throw site sets a Python exception; guarantee one regardless.
        if (c.PyErr_Occurred() == null) py.setError("JSON: parse error", .{});
        return null;
    };
}

const ParseError = error{ Parse, OutOfMemory };

fn parseValueImpl(json: []const u8, pos: *usize, stack: *FrameStack) ParseError!*c.PyObject {
    const limit = maxDepth();

    outer: while (true) {
        // ── Read one value into the current context ──────────────────────────
        pos.* = simd.skipWhitespaceSIMD(json, pos.*);
        if (pos.* >= json.len) {
            py.setError("JSON: unexpected end of input", .{});
            return error.Parse;
        }

        var val: *c.PyObject = switch (json[pos.*]) {
            '{' => blk: {
                // DoS policy: bound how deep the resulting Python object graph goes.
                if (stack.len >= limit) {
                    py.setError("JSON: maximum nesting depth ({d}) exceeded", .{limit});
                    return error.Parse;
                }
                pos.* += 1; // skip '{'
                const dict = py.newDict() orelse return error.Parse;
                const after = simd.skipWhitespaceSIMD(json, pos.*);
                if (after < json.len and json[after] == '}') {
                    pos.* = after + 1; // empty object — a complete value
                    break :blk dict;
                }
                if (!stack.push(.{ .container = dict, .is_object = true, .pending_key = null })) {
                    c.Py_DecRef(dict);
                    return error.OutOfMemory;
                }
                try readObjectKey(json, pos, stack); // sets pending_key on the new top
                continue :outer; // now read this key's value
            },
            '[' => blk: {
                if (stack.len >= limit) {
                    py.setError("JSON: maximum nesting depth ({d}) exceeded", .{limit});
                    return error.Parse;
                }
                pos.* += 1; // skip '['
                const list = c.PyList_New(0) orelse return error.Parse;
                const after = simd.skipWhitespaceSIMD(json, pos.*);
                if (after < json.len and json[after] == ']') {
                    pos.* = after + 1; // empty array — a complete value
                    break :blk list;
                }
                if (!stack.push(.{ .container = list, .is_object = false, .pending_key = null })) {
                    c.Py_DecRef(list);
                    return error.OutOfMemory;
                }
                continue :outer; // read the first element as a value
            },
            '"' => parseString(json, pos) orelse return error.Parse,
            't' => parseTrue(json, pos) orelse return error.Parse,
            'f' => parseFalse(json, pos) orelse return error.Parse,
            'n' => parseNull(json, pos) orelse return error.Parse,
            '-', '0'...'9' => parseNumber(json, pos) orelse return error.Parse,
            else => {
                py.setError("JSON: unexpected character at position {d}", .{pos.*});
                return error.Parse;
            },
        };

        // ── Attach the completed value, then settle separators, popping any
        //    containers that just closed (their completed value bubbles up). ──
        while (true) {
            if (stack.len == 0) return val; // root value complete

            const frame = stack.top();
            if (frame.is_object) {
                const key = frame.pending_key.?;
                // PyDict_SetItem does NOT steal refs.
                if (c.PyDict_SetItem(frame.container, key, val) != 0) {
                    c.Py_DecRef(val);
                    return error.Parse;
                }
                c.Py_DecRef(key);
                frame.pending_key = null;
                c.Py_DecRef(val);
            } else {
                if (c.PyList_Append(frame.container, val) != 0) {
                    c.Py_DecRef(val);
                    return error.Parse;
                }
                c.Py_DecRef(val);
            }
            // val is now owned by the container; do not touch it again.

            pos.* = simd.skipWhitespaceSIMD(json, pos.*);
            if (pos.* >= json.len) {
                py.setError("JSON: unterminated container", .{});
                return error.Parse;
            }
            const sep = json[pos.*];
            const close: u8 = if (frame.is_object) '}' else ']';
            if (sep == ',') {
                pos.* += 1;
                if (frame.is_object) try readObjectKey(json, pos, stack);
                continue :outer; // read the next value
            } else if (sep == close) {
                pos.* += 1;
                val = frame.container; // completed container bubbles up
                stack.pop();
                continue; // settle it into the parent (or return as root)
            } else {
                py.setError("JSON: expected ',' or '{c}' in container", .{close});
                return error.Parse;
            }
        }
    }
}

// Read an object key + ':' at the current position and store the key as the top
// frame's pending value key. Advances pos past the colon.
fn readObjectKey(json: []const u8, pos: *usize, stack: *FrameStack) ParseError!void {
    pos.* = simd.skipWhitespaceSIMD(json, pos.*);
    if (pos.* >= json.len or json[pos.*] != '"') {
        py.setError("JSON: expected string key in object", .{});
        return error.Parse;
    }
    const key = parseString(json, pos) orelse return error.Parse;
    pos.* = simd.skipWhitespaceSIMD(json, pos.*);
    if (pos.* >= json.len or json[pos.*] != ':') {
        c.Py_DecRef(key);
        py.setError("JSON: expected ':' after key", .{});
        return error.Parse;
    }
    pos.* += 1; // skip ':'
    stack.top().pending_key = key;
}

fn parseString(json: []const u8, pos: *usize) ?*c.PyObject {
    pos.* += 1; // skip opening '"'

    // SIMD string extraction: 32-byte blocks scanning for quote + backslash
    const result = simd.extractString(json, pos.*) catch {
        py.setError("JSON: unterminated string", .{});
        return null;
    };

    pos.* = result.end;

    if (!result.has_escapes) {
        // Fast path: zero-copy, no escapes — direct to Python string
        return py.newString(result.slice);
    }

    // Slow path: has escape sequences — use simd_json_parser's processEscapes
    const unescaped = simd.processEscapes(result.slice, allocator) catch {
        py.setError("JSON: escape processing failed", .{});
        return null;
    };
    defer allocator.free(unescaped);

    return py.newString(unescaped);
}

fn parseNumber(json: []const u8, pos: *usize) ?*c.PyObject {
    const start = pos.*;

    // Check if this is a float by scanning ahead
    var i = start;
    if (i < json.len and json[i] == '-') i += 1;
    while (i < json.len and json[i] >= '0' and json[i] <= '9') i += 1;
    const is_float = i < json.len and (json[i] == '.' or json[i] == 'e' or json[i] == 'E');

    if (!is_float) {
        // SIMD integer parsing: 8-digit SIMD parallel digit processing
        const int_result = simd.parseInteger(json, start) catch {
            // Overflow — fall through to Python arbitrary precision
            return parseNumberFallback(json, pos);
        };
        pos.* = int_result.end;
        return py.newInt(int_result.value);
    }

    // Float parsing with SIMD boundary detection
    const float_result = simd.parseFloat(json, start) catch {
        py.setError("JSON: invalid number", .{});
        return null;
    };
    pos.* = float_result.end;
    return c.PyFloat_FromDouble(float_result.value);
}

/// Fallback for numbers too large for i64 — use Python's arbitrary precision
fn parseNumberFallback(json: []const u8, pos: *usize) ?*c.PyObject {
    const start = pos.*;
    if (pos.* < json.len and json[pos.*] == '-') pos.* += 1;
    while (pos.* < json.len and json[pos.*] >= '0' and json[pos.*] <= '9') pos.* += 1;
    if (pos.* < json.len and json[pos.*] == '.') {
        pos.* += 1;
        while (pos.* < json.len and json[pos.*] >= '0' and json[pos.*] <= '9') pos.* += 1;
    }
    if (pos.* < json.len and (json[pos.*] == 'e' or json[pos.*] == 'E')) {
        pos.* += 1;
        if (pos.* < json.len and (json[pos.*] == '+' or json[pos.*] == '-')) pos.* += 1;
        while (pos.* < json.len and json[pos.*] >= '0' and json[pos.*] <= '9') pos.* += 1;
    }
    const num_str = json[start..pos.*];
    if (num_str.len == 0) {
        py.setError("JSON: empty number", .{});
        return null;
    }
    // Use Python's parser for arbitrary precision
    const py_str = py.newString(num_str) orelse return null;
    defer c.Py_DecRef(py_str);
    // Try int first, then float
    if (std.mem.indexOfAny(u8, num_str, ".eE")) |_| {
        return c.PyFloat_FromString(py_str);
    }
    // Need null-terminated string for PyLong_FromString
    var buf: [128]u8 = undefined;
    if (num_str.len < buf.len) {
        @memcpy(buf[0..num_str.len], num_str);
        buf[num_str.len] = 0;
        return c.PyLong_FromString(&buf, null, 10);
    }
    const heap = allocator.alloc(u8, num_str.len + 1) catch return null;
    defer allocator.free(heap);
    @memcpy(heap[0..num_str.len], num_str);
    heap[num_str.len] = 0;
    return c.PyLong_FromString(heap.ptr, null, 10);
}

fn parseTrue(json: []const u8, pos: *usize) ?*c.PyObject {
    if (pos.* + 4 <= json.len and std.mem.eql(u8, json[pos.* .. pos.* + 4], "true")) {
        pos.* += 4;
        return py.pyTrue();
    }
    py.setError("JSON: invalid literal", .{});
    return null;
}

fn parseFalse(json: []const u8, pos: *usize) ?*c.PyObject {
    if (pos.* + 5 <= json.len and std.mem.eql(u8, json[pos.* .. pos.* + 5], "false")) {
        pos.* += 5;
        return py.pyFalse();
    }
    py.setError("JSON: invalid literal", .{});
    return null;
}

fn parseNull(json: []const u8, pos: *usize) ?*c.PyObject {
    if (pos.* + 4 <= json.len and std.mem.eql(u8, json[pos.* .. pos.* + 4], "null")) {
        pos.* += 4;
        return py.pyNone();
    }
    py.setError("JSON: invalid literal", .{});
    return null;
}

// ── Exported C function for Python ───────────────────────────────────────────

/// Python-callable: json_loads_native(json_str) → Python object
/// SIMD-accelerated JSON parsing directly into Python dict/list/str/int/float/bool/None.
pub fn json_loads_native(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var json_ptr: [*c]const u8 = null;
    var json_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#", &json_ptr, &json_len) == 0) return null;

    if (json_len <= 0) {
        py.setError("JSON: empty input", .{});
        return null;
    }

    const json: []const u8 = json_ptr[0..@intCast(json_len)];
    return jsonToPython(json);
}

// ── Tests ────────────────────────────────────────────────────────────────────

const testing = std.testing;

fn ensurePython() void {
    if (c.Py_IsInitialized() == 0) c.Py_Initialize();
}

test "depth policy: over-deep array nesting errors instead of crashing" {
    ensurePython();
    const limit = maxDepth();

    // `[` * N + `]` * N with N well beyond the limit. The iterative parser must
    // return a clean error (null + Python exception set), never a crash — and
    // must NOT have built a limit-deep Python object graph before erroring.
    const n: usize = @as(usize, limit) + 50;
    const buf = try testing.allocator.alloc(u8, n * 2);
    defer testing.allocator.free(buf);
    @memset(buf[0..n], '[');
    @memset(buf[n..], ']');

    var pos: usize = 0;
    const result = parseValue(buf, &pos);
    try testing.expect(result == null);
    try testing.expect(c.PyErr_Occurred() != null);
    c.PyErr_Clear();
}

test "depth policy: over-deep object nesting errors instead of crashing" {
    ensurePython();
    const limit = maxDepth();

    // `{"a":` * N + `}` * N — the object path must count depth too.
    const n: usize = @as(usize, limit) + 50;
    const open = "{\"a\":";
    const buf = try testing.allocator.alloc(u8, n * open.len + n);
    defer testing.allocator.free(buf);
    var i: usize = 0;
    while (i < n) : (i += 1) @memcpy(buf[i * open.len ..][0..open.len], open);
    @memset(buf[n * open.len ..], '}');

    var pos: usize = 0;
    const result = parseValue(buf, &pos);
    try testing.expect(result == null);
    try testing.expect(c.PyErr_Occurred() != null);
    c.PyErr_Clear();
}

test "iterative parser: extreme nesting is heap-bounded, not a stack crash" {
    ensurePython();

    // 100k-deep array — the value that SIGSEGV'd the recursive parser. The
    // iterative parser tracks nesting on the heap, so this must be an ordinary
    // parse error (depth policy), never a native stack overflow.
    const n: usize = 100_000;
    const buf = try testing.allocator.alloc(u8, n * 2);
    defer testing.allocator.free(buf);
    @memset(buf[0..n], '[');
    @memset(buf[n..], ']');

    var pos: usize = 0;
    const result = parseValue(buf, &pos);
    try testing.expect(result == null);
    try testing.expect(c.PyErr_Occurred() != null);
    c.PyErr_Clear();
}

test "depth policy: legitimately deep-but-under-limit nesting parses correctly" {
    ensurePython();

    // 500 levels of array nesting, innermost value 42:  [[[…[42]…]]]
    // Well under the default limit — must parse to the correct nested structure.
    const depth: usize = 500;
    const buf = try testing.allocator.alloc(u8, depth * 2 + 2);
    defer testing.allocator.free(buf);
    @memset(buf[0..depth], '[');
    buf[depth] = '4';
    buf[depth + 1] = '2';
    @memset(buf[depth + 2 ..], ']');

    var pos: usize = 0;
    const result = parseValue(buf, &pos);
    try testing.expect(result != null);
    const root = result.?;
    defer c.Py_DecRef(root);

    // Walk down all 500 levels and confirm the innermost scalar is 42.
    var cur = root;
    var d: usize = 0;
    while (d < depth) : (d += 1) {
        try testing.expect(c.PyList_Check(cur) != 0);
        try testing.expect(c.PyList_Size(cur) == 1);
        cur = c.PyList_GetItem(cur, 0).?; // borrowed
    }
    try testing.expect(c.PyLong_AsLong(cur) == 42);
}

test "iterative parser: shallow payload materializes the correct structure" {
    ensurePython();
    var pos: usize = 0;
    const result = parseValue("{\"a\": [1, 2, {\"b\": true}], \"c\": null}", &pos);
    try testing.expect(result != null);
    const obj = result.?;
    defer c.Py_DecRef(obj);

    try testing.expect(c.PyDict_Check(obj) != 0);
    // obj["a"] is a 3-element list
    const a = c.PyDict_GetItemString(obj, "a").?; // borrowed
    try testing.expect(c.PyList_Check(a) != 0);
    try testing.expect(c.PyList_Size(a) == 3);
    // a[2]["b"] is True
    const inner = c.PyList_GetItem(a, 2).?; // borrowed
    try testing.expect(c.PyDict_Check(inner) != 0);
    const b = c.PyDict_GetItemString(inner, "b").?; // borrowed
    try testing.expect(c.PyObject_IsTrue(b) == 1);
    // obj["c"] is None
    const cval = c.PyDict_GetItemString(obj, "c").?; // borrowed
    try testing.expect(cval == py.pyNone());
}
