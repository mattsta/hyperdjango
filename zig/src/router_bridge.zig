// router_bridge.zig — Expose the Zig radix trie router to Python.
//
// Provides a C API for Python to use the native radix trie router
// for URL resolution, bypassing regex-based routing entirely.
//
// API:
//   _router_new() → handle (int)
//   _router_add(handle, method, pattern, handler_key) → None
//   _router_resolve(handle, method, path) → (handler_key, {params}) or None
//   _router_free(handle) → None

const std = @import("std");
const py = @import("py.zig");
const c = py.c;
const router_mod = @import("router.zig");

const allocator = std.heap.c_allocator;

// ── Router Registry ──────────────────────────────────────────────────────────
// Dynamic array of router instances, indexed by handle.

var routers: std.ArrayListUnmanaged(?*router_mod.Router) = .empty;

// A2#5: the registry is normally register-at-startup-then-resolve, but nothing
// enforced that discipline. A concurrent `router_new` append can realloc the
// backing store while a `router_resolve`/`router_free` indexes `routers.items[h]`
// (UAF), and two `router_new` calls scanning for a null slot could both claim it.
// Guard every registry access: writers (new/add/finalize/free) take the write
// lock; the per-request hot path (resolve) takes a shared read lock so concurrent
// resolves still run in parallel while being safe against a concurrent free/realloc.
var routers_lock: py.RwLock = .{};

// ── Python API ───────────────────────────────────────────────────────────────

/// _router_new() → int handle
pub fn router_new(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const r = allocator.create(router_mod.Router) catch {
        py.setError("router: allocation failed", .{});
        return null;
    };
    r.* = router_mod.Router.init(allocator);

    routers_lock.lock();
    defer routers_lock.unlock();

    // Find a free slot or append
    for (routers.items, 0..) |slot, i| {
        if (slot == null) {
            routers.items[i] = r;
            return py.newInt(@intCast(i));
        }
    }
    routers.append(allocator, r) catch {
        r.deinit();
        allocator.destroy(r);
        py.setError("router: registry full", .{});
        return null;
    };
    return py.newInt(@intCast(routers.items.len - 1));
}

/// _router_add(handle, method, pattern, handler_key) → None
pub fn router_add(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = -1;
    var method_c: [*c]const u8 = null;
    var pattern_c: [*c]const u8 = null;
    var key_c: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "lsss", &handle, &method_c, &pattern_c, &key_c) == 0) return null;

    routers_lock.lock();
    defer routers_lock.unlock();

    const h = std.math.cast(usize, handle) orelse {
        py.setError("router: invalid handle {d}", .{handle});
        return null;
    };
    if (h >= routers.items.len) {
        py.setError("router: invalid handle {d}", .{handle});
        return null;
    }
    const r = routers.items[h] orelse {
        py.setError("router: handle {d} is freed", .{handle});
        return null;
    };

    r.addRoute(std.mem.span(method_c), std.mem.span(pattern_c), std.mem.span(key_c)) catch {
        py.setError("router: failed to add route", .{});
        return null;
    };

    return py.pyNone();
}

/// _router_resolve(handle, method, path) → (handler_key, {param: value, ...}) or None
pub fn router_resolve(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = -1;
    var method_c: [*c]const u8 = null;
    var path_c: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "lss", &handle, &method_c, &path_c) == 0) return null;

    // Shared lock: concurrent resolves run in parallel, but a concurrent
    // router_free/router_new (write lock) cannot deinit or realloc under us.
    // Held across findRoute + result building since match slices may reference
    // router-internal memory. (A2#5)
    routers_lock.lockShared();
    defer routers_lock.unlockShared();

    const h = std.math.cast(usize, handle) orelse {
        py.setError("router: invalid handle {d}", .{handle});
        return null;
    };
    if (h >= routers.items.len) {
        py.setError("router: invalid handle {d}", .{handle});
        return null;
    }
    const r = routers.items[h] orelse {
        py.setError("router: handle {d} is freed", .{handle});
        return null;
    };

    var match = r.findRoute(std.mem.span(method_c), std.mem.span(path_c)) orelse {
        return py.pyNone();
    };
    defer match.deinit();

    // Build result: (handler_key, {param_name: param_value, ...})
    const key_obj = py.newString(match.handler_key) orelse return null;

    const params_dict = py.newDict() orelse {
        c.Py_DecRef(key_obj);
        return null;
    };

    for (match.params.entries()) |param| {
        const k = py.newString(param.key) orelse {
            c.Py_DecRef(key_obj);
            c.Py_DecRef(params_dict);
            return null;
        };
        const v = py.newString(param.value) orelse {
            c.Py_DecRef(k);
            c.Py_DecRef(key_obj);
            c.Py_DecRef(params_dict);
            return null;
        };
        if (c.PyDict_SetItem(params_dict, k, v) != 0) {
            c.Py_DecRef(k);
            c.Py_DecRef(v);
            c.Py_DecRef(key_obj);
            c.Py_DecRef(params_dict);
            return null;
        }
        c.Py_DecRef(k);
        c.Py_DecRef(v);
    }

    // Return tuple (handler_key, params_dict)
    const result = c.PyTuple_Pack(2, key_obj, params_dict);
    c.Py_DecRef(key_obj);
    c.Py_DecRef(params_dict);
    return result;
}

/// _router_finalize(handle) → None
/// Optimize the router: sort children, compress single-child chains.
/// Call after all routes are registered, before serving requests.
pub fn router_finalize(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;

    routers_lock.lock();
    defer routers_lock.unlock();

    const h = std.math.cast(usize, handle) orelse return py.pyNone();
    if (h < routers.items.len) {
        if (routers.items[h]) |r| {
            r.finalize();
        }
    }

    return py.pyNone();
}

/// _router_free(handle) → None
pub fn router_free(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;

    routers_lock.lock();
    defer routers_lock.unlock();

    const h = std.math.cast(usize, handle) orelse return py.pyNone();
    if (h < routers.items.len) {
        if (routers.items[h]) |r| {
            r.deinit();
            allocator.destroy(r);
            routers.items[h] = null;
        }
    }

    return py.pyNone();
}
