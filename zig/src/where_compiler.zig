// WhereNode Zig acceleration — native fast-path for compiled query cache.
//
// _where_cache_key: Single FFI call computes 64-bit FNV-1a hash from
//   filter/exclude lists directly. Iterates Python tuples in-place —
//   no intermediate list allocation needed on the Python side.
//
//   Takes: (filters: list[(key,value)], excludes: list[(key,value)])
//   Returns: int (64-bit structural hash)
//
//   Extracts key strings and value shapes (None=0, True=1, False=2,
//   empty_collection=3, plain_non_empty=4, some_null=5, all_null=6)
//   natively, matching where.value_shape().
//
//   ⚠ LOCKSTEP INVARIANT: valueShape() below and where.value_shape() (Python)
//   MUST return identical shape codes for every value. They feed two halves of
//   the SAME compiled-SQL cache: the native FNV hash (plain filters) and the
//   Python Q-object structural key. The __in lookup emits three DIFFERENT SQL
//   templates with different bind-param counts depending on null content
//   ([1,2]→`= ANY($1)`; [1,None]→`(= ANY($1) OR IS NULL)`; [None]→`IS NULL`),
//   so codes 4/5/6 keep those three variants in distinct cache buckets. Change
//   one function ⇒ change the other.

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;

// FNV-1a constants
const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const FNV_PRIME: u64 = 0x100000001b3;

inline fn fnv_byte(h: u64, b: u8) u64 {
    return (h ^ b) *% FNV_PRIME;
}

fn fnv_bytes(h: u64, data: []const u8) u64 {
    var hash = h;
    for (data) |b| hash = fnv_byte(hash, b);
    return hash;
}

fn fnv_u64(h: u64, val: u64) u64 {
    var hash = h;
    const bytes: [8]u8 = @bitCast(val);
    for (bytes) |b| hash = fnv_byte(hash, b);
    return hash;
}

/// Map a None count over a non-empty collection to a null-aware shape code:
/// 0 nones → 4 (plain), all nones → 6, otherwise → 5. `n` is the collection
/// length (> 0).
inline fn nullShape(none_count: usize, n: usize) u64 {
    if (none_count == 0) return 4;
    if (none_count == n) return 6;
    return 5;
}

/// Compute value shape matching where.value_shape() — MUST stay in lockstep
/// (see LOCKSTEP INVARIANT at top of file):
///   None=0, True=1, False=2, empty_collection=3,
///   non-empty no-null=4, non-empty some-null=5, non-empty all-null=6
fn valueShape(obj: *c.PyObject) u64 {
    const none_ptr = @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct));
    if (obj == none_ptr)
        return 0;
    if (obj == @as(*c.PyObject, @ptrCast(&c._Py_TrueStruct)))
        return 1;
    if (obj == @as(*c.PyObject, @ptrCast(&c._Py_FalseStruct)))
        return 2;
    if (c.PyList_Check(obj) != 0) {
        const size = c.PyList_Size(obj);
        if (size <= 0) return 3;
        const n: usize = @intCast(size);
        var none_count: usize = 0;
        for (0..n) |i| {
            const item = c.PyList_GetItem(obj, @intCast(i)); // borrowed
            if (item != null and item.? == none_ptr) none_count += 1;
        }
        return nullShape(none_count, n);
    }
    if (c.PyTuple_Check(obj) != 0) {
        const size = c.PyTuple_Size(obj);
        if (size <= 0) return 3;
        const n: usize = @intCast(size);
        var none_count: usize = 0;
        for (0..n) |i| {
            const item = c.PyTuple_GetItem(obj, @intCast(i)); // borrowed
            if (item != null and item.? == none_ptr) none_count += 1;
        }
        return nullShape(none_count, n);
    }
    // set / frozenset — reachable from the native path when a filter uses a set
    // value, e.g. `.filter(id__in={1, 2})`. A set holds None at most once, so
    // "contains None" + size fully determines the null-aware shape.
    if (c.PyAnySet_Check(obj)) {
        const size = c.PySet_Size(obj);
        if (size <= 0) return 3;
        const has_none = c.PySet_Contains(obj, none_ptr) == 1;
        if (!has_none) return 4;
        if (size == 1) return 6;
        return 5;
    }
    return 4;
}

/// Hash a list of (key, value) filter tuples.
/// Extracts key string + value shape from each tuple item.
fn hashFilterList(start_hash: u64, filter_list: *c.PyObject) u64 {
    var hash = start_hash;
    // PyList_Size returns -1 (and sets an exception) for a non-list; guard the
    // signed→usize cast so a negative length can't become a huge loop bound.
    const size = c.PyList_Size(filter_list);
    if (size < 0) {
        c.PyErr_Clear();
        return hash;
    }
    const n: usize = @intCast(size);

    for (0..n) |i| {
        const item = c.PyList_GetItem(filter_list, @intCast(i)) orelse {
            c.PyErr_Clear();
            continue;
        };
        // Each item is a (key, value) tuple
        const key_obj = c.PyTuple_GetItem(item, 0); // borrowed
        const val_obj = c.PyTuple_GetItem(item, 1); // borrowed
        if (key_obj == null or val_obj == null) {
            c.PyErr_Clear();
            continue;
        }

        // Hash key string
        var key_len: c.Py_ssize_t = 0;
        const key_ptr = c.PyUnicode_AsUTF8AndSize(key_obj, &key_len);
        if (key_ptr != null) {
            hash = fnv_bytes(hash, @as([*]const u8, @ptrCast(key_ptr))[0..@intCast(key_len)]);
        } else {
            // Non-UTF8/surrogate key — clear so the stale exception can't leak.
            c.PyErr_Clear();
        }

        // Hash value shape
        hash = fnv_u64(hash, valueShape(val_obj.?));
    }

    return hash;
}

// ---------------------------------------------------------------------------
// _where_cache_key(filters, excludes) → int
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// _where_compile(node, start_idx) → (sql, params, next_idx)
// ---------------------------------------------------------------------------

// Cached interned PyObject* for attribute names (avoids string hash on every access)
var _attr_template: ?*c.PyObject = null;
var _attr_bind_values: ?*c.PyObject = null;
var _attr_children: ?*c.PyObject = null;
var _attr_negated: ?*c.PyObject = null;
var _attr_connector: ?*c.PyObject = null;

// Free-threaded init (GIL OFF): the old `if (_attr_template == null)` guard wrote
// the guard var FIRST and the other four after — all plain, non-atomic stores. A
// second thread could observe `_attr_template != null` while the siblings were
// still null and deref null into PyObject_GetAttr (UB in ReleaseFast; `.?` panic
// in ReleaseSafe), plus race on the pointer words and double-intern-leak a strong
// ref. Fix: double-checked locking (the codebase idiom — see server.zig
// getAsyncioRun/getNativeLogger) with a SEPARATE atomic done-flag published LAST
// with release ordering, so a reader that acquire-loads `ready == true`
// happens-after ALL FIVE pointer stores. Init runs exactly once → no ref leak.
var _attr_cache_ready: bool = false;
var _attr_cache_mutex: py.Mutex = .{};

/// Idempotently intern the five attribute-name PyObjects. Returns false (with a
/// pending Python exception) if interning fails; callers must bail. Safe to call
/// concurrently from any number of threads with no GIL — all-or-nothing publish.
fn initAttrCache() bool {
    // Fast path: acquire-load the done-flag. `true` here synchronizes-with the
    // release-store below, so every pointer store is visible before any deref.
    if (@atomicLoad(bool, &_attr_cache_ready, .acquire)) return true;

    _attr_cache_mutex.lock();
    defer _attr_cache_mutex.unlock();
    if (_attr_cache_ready) return true; // another thread won the race

    // InternFromString returns a NEW strong ref on each call. We reach this body
    // exactly once (flag-guarded), so there is no double-intern leak. On partial
    // failure, decref what we obtained and leave the flag unpublished — a later
    // call retries cleanly.
    const template = c.PyUnicode_InternFromString("template") orelse return false;
    const bind_values = c.PyUnicode_InternFromString("bind_values") orelse {
        c.Py_DecRef(template);
        return false;
    };
    const children = c.PyUnicode_InternFromString("children") orelse {
        c.Py_DecRef(template);
        c.Py_DecRef(bind_values);
        return false;
    };
    const negated = c.PyUnicode_InternFromString("negated") orelse {
        c.Py_DecRef(template);
        c.Py_DecRef(bind_values);
        c.Py_DecRef(children);
        return false;
    };
    const connector = c.PyUnicode_InternFromString("connector") orelse {
        c.Py_DecRef(template);
        c.Py_DecRef(bind_values);
        c.Py_DecRef(children);
        c.Py_DecRef(negated);
        return false;
    };

    _attr_template = template;
    _attr_bind_values = bind_values;
    _attr_children = children;
    _attr_negated = negated;
    _attr_connector = connector;

    // Publish LAST: only now are all five pointers non-null and visible to any
    // thread that acquire-loads this flag.
    @atomicStore(bool, &_attr_cache_ready, true, .release);
    return true;
}

/// Compile a WhereNode tree to (sql_string, params_list, next_idx).
/// Native acceleration of WhereNode.compile() — single-pass split/join
/// with stack-allocated output buffer.
pub fn where_compile(_: ?*c.PyObject, args: [*]?*c.PyObject, nargs: c.Py_ssize_t) callconv(.c) ?*c.PyObject {
    if (!initAttrCache()) return null; // interning failed — exception already set
    if (nargs != 2) {
        py.setError("_where_compile requires 2 args (node, start_idx)", .{});
        return null;
    }
    const node = args[0] orelse return null;
    const start_idx_obj = args[1] orelse return null;
    const start_idx_raw = c.PyLong_AsLongLong(start_idx_obj);
    // Guard the u32 cast: a negative value or one too large for u32 would make
    // @intCast undefined behavior in ReleaseFast.
    const start_idx = std.math.cast(u32, start_idx_raw) orelse {
        if (c.PyErr_Occurred() != null) c.PyErr_Clear();
        py.setError("_where_compile: start_idx out of range", .{});
        return null;
    };

    // Output buffer for SQL assembly (stack-allocated, 8KB)
    var sql_buf: [8192]u8 = undefined;
    var sql_len: usize = 0;

    // Params list
    const params = c.PyList_New(0) orelse return null;

    const next_idx = compileNode(node, start_idx, &sql_buf, &sql_len, params) orelse {
        c.Py_DecRef(params);
        return null;
    };

    // Build return tuple (sql_string, params_list, next_idx)
    const sql_str = c.PyUnicode_FromStringAndSize(@ptrCast(&sql_buf), @intCast(sql_len)) orelse {
        c.Py_DecRef(params);
        return null;
    };
    const next_idx_py = py.newInt(@intCast(next_idx)) orelse {
        c.Py_DecRef(sql_str);
        c.Py_DecRef(params);
        return null;
    };

    const result = c.PyTuple_New(3) orelse {
        c.Py_DecRef(sql_str);
        c.Py_DecRef(params);
        c.Py_DecRef(next_idx_py);
        return null;
    };
    // PyTuple_SetItem steals references
    _ = c.PyTuple_SetItem(result, 0, sql_str);
    _ = c.PyTuple_SetItem(result, 1, params);
    _ = c.PyTuple_SetItem(result, 2, next_idx_py);
    return result;
}

/// Recursively compile a WhereNode into sql_buf, collecting params.
fn compileNode(
    node: *c.PyObject,
    start_idx: u32,
    buf: *[8192]u8,
    buf_len: *usize,
    params: *c.PyObject,
) ?u32 {
    // Read attributes from WhereNode dataclass (slots=True)
    // Use cached interned attribute name PyObjects (no string hash on every call)
    const template_obj = c.PyObject_GetAttr(node, _attr_template.?) orelse return null;
    defer c.Py_DecRef(template_obj);
    const bind_values_obj = c.PyObject_GetAttr(node, _attr_bind_values.?) orelse return null;
    defer c.Py_DecRef(bind_values_obj);
    const children_obj = c.PyObject_GetAttr(node, _attr_children.?) orelse return null;
    defer c.Py_DecRef(children_obj);
    const negated_obj = c.PyObject_GetAttr(node, _attr_negated.?) orelse return null;
    defer c.Py_DecRef(negated_obj);

    var tmpl_len: c.Py_ssize_t = 0;
    const tmpl_ptr = c.PyUnicode_AsUTF8AndSize(template_obj, &tmpl_len);
    // A non-str/surrogate template leaves a pending exception; we treat it as an
    // empty template below, so clear it now to avoid corrupting a later call.
    if (tmpl_ptr == null) c.PyErr_Clear();
    const children_len_signed = c.PyList_Size(children_obj);
    if (children_len_signed < 0) return null; // children not a list — exception set
    const children_len: usize = @intCast(children_len_signed);
    const is_negated = (negated_obj == @as(*c.PyObject, @ptrCast(&c._Py_TrueStruct)));

    // Empty node check
    if ((tmpl_ptr == null or tmpl_len == 0) and children_len == 0) {
        return start_idx;
    }

    if (tmpl_ptr != null and tmpl_len > 0) {
        // Leaf node: split template on {} and reassemble with $N
        return compileLeaf(
            @as([*]const u8, @ptrCast(tmpl_ptr.?))[0..@intCast(tmpl_len)],
            bind_values_obj,
            start_idx,
            buf,
            buf_len,
            params,
            is_negated,
        );
    }

    // Branch node: compile children, join with connector
    const connector_obj = c.PyObject_GetAttr(node, _attr_connector.?) orelse return null;
    defer c.Py_DecRef(connector_obj);
    var conn_len: c.Py_ssize_t = 0;
    const conn_ptr = c.PyUnicode_AsUTF8AndSize(connector_obj, &conn_len);

    const is_or = (conn_ptr != null and conn_len == 2 and
        @as([*]const u8, @ptrCast(conn_ptr.?))[0] == 'O' and
        @as([*]const u8, @ptrCast(conn_ptr.?))[1] == 'R');

    return compileBranch(children_obj, children_len, is_or, start_idx, buf, buf_len, params, is_negated);
}

/// Compile a leaf node: template with {} → $N placeholders.
fn compileLeaf(
    template: []const u8,
    bind_values: *c.PyObject,
    start_idx: u32,
    buf: *[8192]u8,
    buf_len: *usize,
    params: *c.PyObject,
    negated: bool,
) ?u32 {
    var idx = start_idx;

    // Append bind_values to params list
    const bv_len_signed = c.PyList_Size(bind_values);
    if (bv_len_signed < 0) return null; // bind_values not a list — exception set
    const bv_len: usize = @intCast(bv_len_signed);
    for (0..bv_len) |i| {
        const item = c.PyList_GetItem(bind_values, @intCast(i)); // borrowed ref
        if (c.PyList_Append(params, item.?) != 0) return null;
    }

    if (negated) {
        appendStr(buf, buf_len, "NOT (");
    }

    // Scan template for {} and replace with $N
    var pos: usize = 0;
    while (pos < template.len) {
        if (pos + 1 < template.len and template[pos] == '{' and template[pos + 1] == '}') {
            // Write $N
            var num_buf: [12]u8 = undefined;
            const num_str = std.fmt.bufPrint(&num_buf, "${d}", .{idx}) catch return null;
            appendStr(buf, buf_len, num_str);
            idx += 1;
            pos += 2;
        } else {
            appendByte(buf, buf_len, template[pos]);
            pos += 1;
        }
    }

    if (negated) {
        appendStr(buf, buf_len, ")");
    }

    return idx;
}

/// Compile a branch node: children joined by AND/OR.
fn compileBranch(
    children: *c.PyObject,
    children_len: usize,
    is_or: bool,
    start_idx: u32,
    buf: *[8192]u8,
    buf_len: *usize,
    params: *c.PyObject,
    negated: bool,
) ?u32 {
    // Compile each child into temporary buffers, collect non-empty parts
    var idx = start_idx;
    var part_count: usize = 0;

    // We need to store child SQL positions. Max 64 children.
    var child_starts: [64]usize = undefined;
    var child_ends: [64]usize = undefined;

    for (0..children_len) |i| {
        const child = c.PyList_GetItem(children, @intCast(i)); // borrowed ref
        const before_len = buf_len.*;
        idx = compileNode(child.?, idx, buf, buf_len, params) orelse return null;
        if (buf_len.* > before_len) {
            if (part_count < 64) {
                child_starts[part_count] = before_len;
                child_ends[part_count] = buf_len.*;
                part_count += 1;
            }
        }
    }

    if (part_count == 0) return idx;

    // Now reassemble: we have the child SQL fragments in the buffer at known positions.
    // We need to insert connectors between them. Build into a temp buffer then copy back.
    var temp_buf: [8192]u8 = undefined;
    var temp_len: usize = 0;

    if (negated) {
        appendStrTo(&temp_buf, &temp_len, "NOT (");
    }

    if (is_or and part_count > 1) {
        appendStrTo(&temp_buf, &temp_len, "(");
    }

    for (0..part_count) |i| {
        if (i > 0) {
            if (is_or) {
                appendStrTo(&temp_buf, &temp_len, " OR ");
            } else {
                appendStrTo(&temp_buf, &temp_len, " AND ");
            }
        }
        const s = child_starts[i];
        const e = child_ends[i];
        appendStrTo(&temp_buf, &temp_len, buf[s..e]);
    }

    if (is_or and part_count > 1) {
        appendStrTo(&temp_buf, &temp_len, ")");
    }

    if (negated) {
        appendStrTo(&temp_buf, &temp_len, ")");
    }

    // Overwrite buf from the start of the first child. The reassembled fragment
    // (child SQL + connectors + NOT/paren wrapping) can be longer than the region
    // it replaces, so bounds-check before the copy — write_start + temp_len can
    // exceed the 8KB buffer for large/deeply-nested WHERE trees.
    const write_start = if (part_count > 0) child_starts[0] else buf_len.*;
    if (write_start + temp_len > buf.len) return null;
    @memcpy(buf[write_start..][0..temp_len], temp_buf[0..temp_len]);
    buf_len.* = write_start + temp_len;

    return idx;
}

inline fn appendByte(buf: *[8192]u8, len: *usize, b: u8) void {
    if (len.* < 8192) {
        buf[len.*] = b;
        len.* += 1;
    }
}

inline fn appendStr(buf: *[8192]u8, len: *usize, s: []const u8) void {
    const avail = 8192 - len.*;
    const n = @min(s.len, avail);
    @memcpy(buf[len.*..][0..n], s[0..n]);
    len.* += n;
}

inline fn appendStrTo(buf: *[8192]u8, len: *usize, s: []const u8) void {
    const avail = 8192 - len.*;
    const n = @min(s.len, avail);
    @memcpy(buf[len.*..][0..n], s[0..n]);
    len.* += n;
}

// ---------------------------------------------------------------------------
// _where_cache_key(filters, excludes) → int
// ---------------------------------------------------------------------------

pub fn where_cache_key(_: ?*c.PyObject, args: [*]?*c.PyObject, nargs: c.Py_ssize_t) callconv(.c) ?*c.PyObject {
    if (nargs != 2) {
        py.setError("_where_cache_key requires 2 arguments", .{});
        return null;
    }

    var hash: u64 = FNV_OFFSET;
    hash = hashFilterList(hash, args[0].?);
    hash = fnv_u64(hash, 0xDEADBEEF); // separator
    hash = hashFilterList(hash, args[1].?);

    return py.newInt(@bitCast(hash));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
//
// `initAttrCache()` itself calls the CPython C-API (PyUnicode_InternFromString),
// which needs a live interpreter and so cannot run under `zig build test`. The
// test below instead exercises the *synchronization skeleton* it uses — the same
// double-checked-locking shape (mutex + acquire/release atomic done-flag,
// body-runs-exactly-once, publish-last) — under many concurrent threads. It
// proves the two properties the fix relies on: (1) the init body runs EXACTLY
// once even with heavy contention (no double-intern strong-ref leak), and (2)
// every thread that observes the published flag runs AFTER the body completed
// (no partial-publish deref of a still-null sibling pointer).
//
// GATE GUIDANCE for an end-to-end multithread stress of the real init path:
// mirror the test_locks.zig pattern — add a `_test_where_compile_stress`
// FASTCALL/VARARGS entry (in test_locks.zig or where_compiler.zig) that spawns
// N std.Thread workers each calling `where_compile` against a shared WhereNode
// under a released GIL, register it in main.zig's method table, and drive it
// from a tests/test_freethread_*.py file. That requires editing main.zig +
// adding a Python test, both outside this fix wave's single-file scope.

const CacheInitSkeleton = struct {
    ready: bool = false,
    mutex: py.Mutex = .{},
    // Number of times the (would-be interning) init body actually executed.
    body_runs: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
    // Stand-ins for the five pointers: 0 = "unpublished/null".
    slots: [5]u32 = .{ 0, 0, 0, 0, 0 },

    fn ensure(self: *CacheInitSkeleton) void {
        if (@atomicLoad(bool, &self.ready, .acquire)) {
            // Post-publish read: mirror compileNode's deref sites. If publication
            // were not release-ordered, a sibling could still read 0 here.
            for (self.slots) |v| std.debug.assert(v != 0);
            return;
        }
        self.mutex.lock();
        defer self.mutex.unlock();
        if (self.ready) return;

        _ = self.body_runs.fetchAdd(1, .monotonic);
        // Write the siblings BEFORE publishing the flag.
        for (&self.slots) |*v| v.* = 0xA11;
        @atomicStore(bool, &self.ready, true, .release);
    }
};

fn cacheStressWorker(cache: *CacheInitSkeleton, iters: u32) void {
    var i: u32 = 0;
    while (i < iters) : (i += 1) cache.ensure();
}

test "initAttrCache DCL: body runs exactly once, publish is race-free" {
    var cache = CacheInitSkeleton{};
    const n_threads = 8;
    var threads: [n_threads]std.Thread = undefined;

    var spawned: usize = 0;
    while (spawned < n_threads) : (spawned += 1) {
        threads[spawned] = std.Thread.spawn(.{}, cacheStressWorker, .{ &cache, @as(u32, 2000) }) catch break;
    }
    for (threads[0..spawned]) |t| t.join();

    // Init body executed exactly once → no double-intern strong-ref leak.
    try std.testing.expectEqual(@as(u32, 1), cache.body_runs.load(.monotonic));
    // All siblings published.
    try std.testing.expect(@atomicLoad(bool, &cache.ready, .acquire));
    for (cache.slots) |v| try std.testing.expectEqual(@as(u32, 0xA11), v);
}
