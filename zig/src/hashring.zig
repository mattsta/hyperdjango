// hashring.zig — Native consistent hash ring with ketama-compatible hashing.
//
// High-performance consistent hash ring for distributed cache routing.
// Ketama-compatible MD5-based 32-bit hashing with weighted virtual nodes.
//
// Performance optimizations vs uhashring (Python):
//   - Split arrays: hashes[] separate from node_indices[] for cache-friendly binary search
//   - SIMD linear scan for small rings (≤64 points) using @Vector(8, u32)
//   - Batch sort O(N log N) instead of insort per point O(N²)
//   - Prefetch on binary search for large rings
//   - Zero Python dict lookups on hot path — direct array index
//
// Architecture:
//   - hashes[]: sorted u32 array (THE hot path data — tight cache lines)
//   - node_indices[]: parallel u16 array (only touched after search completes)
//   - MD5 from Zig stdlib — extracts 4x 32-bit hashes per digest (ketama replicas)
//   - @byteSwap for little-endian extraction (single instruction on ARM/x86)

const std = @import("std");
const py = @import("py.zig");
const c = py.c;
const Md5 = std.crypto.hash.Md5;

// ---------------------------------------------------------------------------
// Core data structures — split arrays for cache-friendly access
// ---------------------------------------------------------------------------

const NodeInfo = struct {
    name_ptr: [*]const u8,
    name_len: u16,
    weight: u16,
    vnodes: u16,
    instance: ?*c.PyObject,
};

const MAX_NODES = 1024;
const MAX_POINTS = 1024 * 1024;
const DEFAULT_VNODES: u16 = 40;
const DEFAULT_REPLICAS: u8 = 4;

const HashRing = struct {
    nodes: [MAX_NODES]NodeInfo = undefined,
    node_count: u16 = 0,
    name_buf: [64 * MAX_NODES]u8 = undefined,
    name_buf_pos: usize = 0,

    // Split arrays: hashes separate from indices for cache-friendly binary search
    hashes: []u32 = &.{},
    node_indices: []u16 = &.{},
    point_count: u32 = 0,
    hashes_heap: ?[]u32 = null,
    indices_heap: ?[]u16 = null,

    replicas: u8 = DEFAULT_REPLICAS,
    default_vnodes: u16 = DEFAULT_VNODES,

    // Per-ring reader/writer lock. Lookups (getNode*/get_stats) read hashes[]/
    // node_indices[]/nodes[] under lockShared(); mutators (addNode/removeNode/
    // buildRing) take lock() while they mutate node state and free+realloc the
    // split arrays. Without this, a serving thread's get_node racing a
    // rebalancing thread's add_node/remove_node is a UAF/OOB under free-threaded
    // CPython (no GIL to serialize the two C calls). The ring OBJECT itself
    // cannot be freed mid-lookup: freeRing runs only from __del__ at refcount 0,
    // which Python's refcounting withholds while a caller holds it to look up.
    lock: py.RwLock = .{},

    allocator: std.mem.Allocator,

    fn init(allocator: std.mem.Allocator, replicas: u8, default_vnodes: u16) HashRing {
        return HashRing{
            .allocator = allocator,
            .replicas = replicas,
            .default_vnodes = default_vnodes,
        };
    }

    fn deinit(self: *HashRing) void {
        if (self.hashes_heap) |h| self.allocator.free(h);
        if (self.indices_heap) |h| self.allocator.free(h);
        self.hashes_heap = null;
        self.indices_heap = null;
        for (self.nodes[0..self.node_count]) |*node| {
            if (node.instance) |inst| {
                c.Py_DECREF(inst);
                node.instance = null;
            }
        }
        self.node_count = 0;
        self.point_count = 0;
    }

    // --- Node management ---

    fn addNode(self: *HashRing, name: []const u8, weight: u16, vnodes: u16, instance: ?*c.PyObject) !u16 {
        if (self.node_count >= MAX_NODES) return error.TooManyNodes;
        if (name.len > 63) return error.NameTooLong;

        const idx = self.node_count;
        const name_start = self.name_buf_pos;
        @memcpy(self.name_buf[name_start .. name_start + name.len], name);
        self.name_buf_pos += name.len;

        if (instance) |inst| c.Py_INCREF(inst);

        self.nodes[idx] = NodeInfo{
            .name_ptr = self.name_buf[name_start..].ptr,
            .name_len = @intCast(name.len),
            .weight = weight,
            .vnodes = vnodes,
            .instance = instance,
        };
        self.node_count += 1;
        return idx;
    }

    fn removeNode(self: *HashRing, name: []const u8) bool {
        var found_idx: ?u16 = null;
        for (self.nodes[0..self.node_count], 0..) |node, i| {
            if (std.mem.eql(u8, node.name_ptr[0..node.name_len], name)) {
                found_idx = @intCast(i);
                break;
            }
        }
        if (found_idx == null) return false;
        const idx = found_idx.?;

        if (self.nodes[idx].instance) |inst| c.Py_DECREF(inst);

        if (idx < self.node_count - 1) {
            for (idx..self.node_count - 1) |i| {
                self.nodes[i] = self.nodes[i + 1];
            }
        }
        self.node_count -= 1;
        return true;
    }

    // --- Ring building ---

    fn buildRing(self: *HashRing) !void {
        if (self.node_count == 0) {
            self.point_count = 0;
            return;
        }

        // Calculate total points
        var weight_sum: u32 = 0;
        for (self.nodes[0..self.node_count]) |node| weight_sum += node.weight;
        if (weight_sum == 0) weight_sum = 1;

        var total_points: u32 = 0;
        for (self.nodes[0..self.node_count]) |node| {
            const ks: u32 = (@as(u32, node.vnodes) * self.node_count * node.weight) / weight_sum;
            total_points += ks * self.replicas;
        }
        if (total_points > MAX_POINTS) return error.TooManyPoints;

        // Free old allocations
        if (self.hashes_heap) |old| self.allocator.free(old);
        if (self.indices_heap) |old| self.allocator.free(old);

        // Allocate split arrays
        const hash_buf = try self.allocator.alloc(u32, total_points);
        const idx_buf = try self.allocator.alloc(u16, total_points);
        self.hashes_heap = hash_buf;
        self.indices_heap = idx_buf;

        // Generate all points
        var pos: u32 = 0;
        var name_buf: [128]u8 = undefined;
        for (self.nodes[0..self.node_count], 0..) |node, node_idx| {
            const node_name = node.name_ptr[0..node.name_len];
            const ks: u32 = (@as(u32, node.vnodes) * self.node_count * node.weight) / weight_sum;

            var w: u32 = 0;
            while (w < ks) : (w += 1) {
                const key = formatKey(&name_buf, node_name, w);
                var digest: [16]u8 = undefined;
                Md5.hash(key, &digest, .{});

                // Extract 4 ketama replicas from 16-byte MD5 digest
                // Use @as + shifts (compiler optimizes to single load on LE architectures)
                var r: u8 = 0;
                while (r < self.replicas and pos < total_points) : (r += 1) {
                    const rd: usize = @as(usize, r) * 4;
                    const h: u32 = @as(u32, digest[3 + rd]) << 24 |
                        @as(u32, digest[2 + rd]) << 16 |
                        @as(u32, digest[1 + rd]) << 8 |
                        @as(u32, digest[0 + rd]);

                    hash_buf[pos] = h;
                    idx_buf[pos] = @intCast(node_idx);
                    pos += 1;
                }
            }
        }

        self.point_count = pos;
        self.hashes = hash_buf[0..pos];
        self.node_indices = idx_buf[0..pos];

        // Co-sort: sort hashes and permute node_indices in parallel
        coSort(self.hashes, self.node_indices);
    }

    // --- Lookup (THE hot path) ---

    fn getNodeIndex(self: *const HashRing, key_hash: u32) ?u16 {
        const count = self.point_count;
        if (count == 0) return null;

        // SIMD linear scan for small rings (≤64 points)
        // 8 hashes fit in a 256-bit vector — scan in chunks
        if (count <= 64) {
            return self.simdScan(key_hash);
        }

        // Binary search with prefetch for large rings
        return self.binarySearch(key_hash);
    }

    fn simdScan(self: *const HashRing, key_hash: u32) ?u16 {
        const count = self.point_count;
        const hashes_ptr = self.hashes.ptr;
        const splat: @Vector(8, u32) = @splat(key_hash);

        var i: u32 = 0;
        while (i + 8 <= count) : (i += 8) {
            const chunk: @Vector(8, u32) = hashes_ptr[i..][0..8].*;
            // Compare: which hashes >= key_hash?
            const ge = chunk >= splat;
            // Find first true (ge bit set)
            const mask: u8 = @bitCast(ge);
            if (mask != 0) {
                const first_set = @ctz(mask);
                return self.node_indices[i + first_set];
            }
        }
        // Remainder
        while (i < count) : (i += 1) {
            if (hashes_ptr[i] >= key_hash) {
                return self.node_indices[i];
            }
        }
        // Wrap around
        return self.node_indices[0];
    }

    fn binarySearch(self: *const HashRing, key_hash: u32) ?u16 {
        const count = self.point_count;
        var lo: u32 = 0;
        var hi: u32 = count;

        while (lo < hi) {
            const mid = lo + (hi - lo) / 2;
            // Prefetch next likely access points for binary search
            if (hi - lo > 16) {
                const next_lo = mid + 1 + (hi - mid - 1) / 2;
                const next_hi = lo + (mid - lo) / 2;
                if (next_lo < count) @prefetch(self.hashes.ptr + next_lo, .{ .rw = .read, .locality = 1 });
                if (next_hi < count) @prefetch(self.hashes.ptr + next_hi, .{ .rw = .read, .locality = 1 });
            }
            if (self.hashes[mid] < key_hash) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        const idx = if (lo < count) lo else 0;
        return self.node_indices[idx];
    }

    fn getNodeName(self: *const HashRing, key_hash: u32) ?[]const u8 {
        const node_idx = self.getNodeIndex(key_hash) orelse return null;
        const node = &self.nodes[node_idx];
        return node.name_ptr[0..node.name_len];
    }

    fn getNodeInstance(self: *const HashRing, key_hash: u32) ?*c.PyObject {
        const node_idx = self.getNodeIndex(key_hash) orelse return null;
        return self.nodes[node_idx].instance;
    }

    // --- Hash key ---

    fn hashKey(key: []const u8) u32 {
        var digest: [16]u8 = undefined;
        Md5.hash(key, &digest, .{});
        return @as(u32, digest[3]) << 24 |
            @as(u32, digest[2]) << 16 |
            @as(u32, digest[1]) << 8 |
            @as(u32, digest[0]);
    }
};

// ---------------------------------------------------------------------------
// Co-sort: sort hashes[] and permute node_indices[] in parallel
// Uses insertion sort for small N, std sort for large
// ---------------------------------------------------------------------------

fn coSort(hashes: []u32, indices: []u16) void {
    const n = hashes.len;
    if (n <= 1) return;

    if (n <= 64) {
        // Insertion sort for small arrays (cache-friendly, low overhead)
        var i: usize = 1;
        while (i < n) : (i += 1) {
            const kh = hashes[i];
            const ki = indices[i];
            var j: usize = i;
            while (j > 0 and hashes[j - 1] > kh) : (j -= 1) {
                hashes[j] = hashes[j - 1];
                indices[j] = indices[j - 1];
            }
            hashes[j] = kh;
            indices[j] = ki;
        }
        return;
    }

    // For large arrays: build permutation index, sort, apply
    // Use std.mem.sort on a paired struct view
    const PairContext = struct {
        h: []u32,
        idx: []u16,
    };
    _ = PairContext;

    // Pack into sortable pairs, sort, unpack
    // (Direct in-place co-sort using temp buffer)
    const allocator = std.heap.c_allocator;
    const tmp_h = allocator.alloc(u32, n) catch {
        // Fallback: simple insertion sort
        var i: usize = 1;
        while (i < n) : (i += 1) {
            const kh = hashes[i];
            const ki = indices[i];
            var j: usize = i;
            while (j > 0 and hashes[j - 1] > kh) : (j -= 1) {
                hashes[j] = hashes[j - 1];
                indices[j] = indices[j - 1];
            }
            hashes[j] = kh;
            indices[j] = ki;
        }
        return;
    };
    defer allocator.free(tmp_h);
    const tmp_i = allocator.alloc(u16, n) catch return;
    defer allocator.free(tmp_i);

    // Build index array
    const perm = allocator.alloc(u32, n) catch return;
    defer allocator.free(perm);
    for (perm, 0..) |*p, idx| p.* = @intCast(idx);

    // Sort perm by hashes[perm[i]]
    const SortCtx = struct {
        h: []const u32,
        fn lessThan(ctx: @This(), a: u32, b: u32) bool {
            return ctx.h[a] < ctx.h[b];
        }
    };
    std.mem.sort(u32, perm, SortCtx{ .h = hashes }, SortCtx.lessThan);

    // Apply permutation
    for (perm, 0..) |p, i| {
        tmp_h[i] = hashes[p];
        tmp_i[i] = indices[p];
    }
    @memcpy(hashes, tmp_h);
    @memcpy(indices, tmp_i);
}

fn formatKey(buf: *[128]u8, name: []const u8, w: u32) []const u8 {
    @memcpy(buf[0..name.len], name);
    buf[name.len] = '-';
    const num_len = formatU32(buf[name.len + 1 ..], w);
    return buf[0 .. name.len + 1 + num_len];
}

fn formatU32(buf: []u8, val: u32) usize {
    if (val == 0) {
        buf[0] = '0';
        return 1;
    }
    var v = val;
    var len: usize = 0;
    var tmp: [10]u8 = undefined;
    while (v > 0) {
        tmp[len] = @intCast(v % 10 + '0');
        v /= 10;
        len += 1;
    }
    for (0..len) |i| buf[i] = tmp[len - 1 - i];
    return len;
}

// ---------------------------------------------------------------------------
// Global ring storage
// ---------------------------------------------------------------------------

const MAX_RINGS = 64;
var rings: [MAX_RINGS]?*HashRing = .{null} ** MAX_RINGS;
var ring_mutex: py.Mutex = .{};

fn allocRing(replicas: u8, vnodes: u16) ?usize {
    ring_mutex.lock();
    defer ring_mutex.unlock();
    for (&rings, 0..) |*slot, i| {
        if (slot.* == null) {
            const allocator = std.heap.c_allocator;
            const ring = allocator.create(HashRing) catch return null;
            ring.* = HashRing.init(allocator, replicas, vnodes);
            slot.* = ring;
            return i;
        }
    }
    return null;
}

fn getRing(handle: usize) ?*HashRing {
    if (handle >= MAX_RINGS) return null;
    return rings[handle];
}

fn freeRing(handle: usize) void {
    ring_mutex.lock();
    defer ring_mutex.unlock();
    if (handle < MAX_RINGS) {
        if (rings[handle]) |ring| {
            ring.deinit();
            ring.allocator.destroy(ring);
            rings[handle] = null;
        }
    }
}

// ---------------------------------------------------------------------------
// Python C API bridge
// ---------------------------------------------------------------------------

pub fn hashring_new(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var replicas: c_int = DEFAULT_REPLICAS;
    var vnodes: c_int = DEFAULT_VNODES;
    if (c.PyArg_ParseTuple(args, "|ii", &replicas, &vnodes) == 0) return null;

    const handle = allocRing(@intCast(replicas), @intCast(vnodes)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Too many hash rings");
        return null;
    };
    return c.PyLong_FromLong(@intCast(handle));
}

pub fn hashring_free(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;
    freeRing(@intCast(handle));
    return py.pyNone();
}

pub fn hashring_add_node(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = 0;
    var weight: c_int = 1;
    var vnodes: c_int = 0;
    var instance: ?*c.PyObject = null;

    if (c.PyArg_ParseTuple(args, "ls#|iiO", &handle, &name_ptr, &name_len, &weight, &vnodes, &instance) == 0) return null;

    const ring = getRing(@intCast(handle)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Invalid hash ring handle");
        return null;
    };

    const actual_vnodes: u16 = if (vnodes > 0) @intCast(vnodes) else ring.default_vnodes;
    const name = name_ptr[0..@intCast(name_len)];

    ring.lock.lock();
    defer ring.lock.unlock();

    _ = ring.addNode(name, @intCast(weight), actual_vnodes, instance) catch |err| {
        const msg = switch (err) {
            error.TooManyNodes => "Too many nodes (max 1024)",
            error.NameTooLong => "Node name too long (max 63)",
        };
        c.PyErr_SetString(c.PyExc_RuntimeError, msg);
        return null;
    };

    return py.pyNone();
}

pub fn hashring_remove_node(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "ls#", &handle, &name_ptr, &name_len) == 0) return null;

    const ring = getRing(@intCast(handle)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Invalid hash ring handle");
        return null;
    };

    ring.lock.lock();
    defer ring.lock.unlock();

    const removed = ring.removeNode(name_ptr[0..@intCast(name_len)]);
    if (removed) {
        ring.buildRing() catch {
            c.PyErr_SetString(c.PyExc_RuntimeError, "Failed to rebuild ring");
            return null;
        };
    }

    return c.PyBool_FromLong(if (removed) 1 else 0);
}

pub fn hashring_build(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;

    const ring = getRing(@intCast(handle)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Invalid hash ring handle");
        return null;
    };

    ring.lock.lock();
    defer ring.lock.unlock();

    ring.buildRing() catch {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Failed to build ring");
        return null;
    };

    return c.PyLong_FromLong(@intCast(ring.point_count));
}

pub fn hashring_get_node(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    var key_ptr: [*c]const u8 = undefined;
    var key_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "ls#", &handle, &key_ptr, &key_len) == 0) return null;

    const ring = getRing(@intCast(handle)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Invalid hash ring handle");
        return null;
    };

    const key_hash = HashRing.hashKey(key_ptr[0..@intCast(key_len)]);

    // Read lock spans the lookup AND the copy out of name_buf, so a concurrent
    // removeNode/buildRing can't shift/free the data mid-read.
    ring.lock.lockShared();
    defer ring.lock.unlockShared();

    const name = ring.getNodeName(key_hash) orelse return py.pyNone();
    return c.PyUnicode_FromStringAndSize(name.ptr, @intCast(name.len));
}

pub fn hashring_get_node_instance(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    var key_ptr: [*c]const u8 = undefined;
    var key_len: c.Py_ssize_t = 0;

    if (c.PyArg_ParseTuple(args, "ls#", &handle, &key_ptr, &key_len) == 0) return null;

    const ring = getRing(@intCast(handle)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Invalid hash ring handle");
        return null;
    };

    const key_hash = HashRing.hashKey(key_ptr[0..@intCast(key_len)]);

    // Read lock spans the lookup AND the Py_INCREF so a concurrent removeNode
    // can't Py_DECREF the instance to zero between our read and our incref.
    ring.lock.lockShared();
    defer ring.lock.unlockShared();

    const instance = ring.getNodeInstance(key_hash) orelse return py.pyNone();
    c.Py_INCREF(instance);
    return instance;
}

pub fn hashring_get_stats(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;

    const ring = getRing(@intCast(handle)) orelse {
        c.PyErr_SetString(c.PyExc_RuntimeError, "Invalid hash ring handle");
        return null;
    };

    // Read lock: get_stats reads node_indices[]/nodes[]/point_count, which a
    // concurrent add_node/remove_node/build mutates and frees.
    ring.lock.lockShared();
    defer ring.lock.unlockShared();

    const dict = c.PyDict_New() orelse return null;

    _ = c.PyDict_SetItemString(dict, "node_count", c.PyLong_FromLong(@intCast(ring.node_count)));
    _ = c.PyDict_SetItemString(dict, "point_count", c.PyLong_FromLong(@intCast(ring.point_count)));
    _ = c.PyDict_SetItemString(dict, "replicas", c.PyLong_FromLong(@intCast(ring.replicas)));
    _ = c.PyDict_SetItemString(dict, "default_vnodes", c.PyLong_FromLong(@intCast(ring.default_vnodes)));

    const dist = c.PyDict_New() orelse return dict;
    var counts: [MAX_NODES]u32 = .{0} ** MAX_NODES;
    for (ring.node_indices[0..ring.point_count]) |idx| counts[idx] += 1;
    for (ring.nodes[0..ring.node_count], 0..) |node, i| {
        const name = c.PyUnicode_FromStringAndSize(node.name_ptr, @intCast(node.name_len)) orelse continue;
        const cnt = c.PyLong_FromLong(@intCast(counts[i]));
        _ = c.PyDict_SetItem(dist, name, cnt);
        c.Py_DECREF(name);
        c.Py_DECREF(cnt);
    }
    _ = c.PyDict_SetItemString(dict, "distribution", dist);
    c.Py_DECREF(dist);

    return dict;
}

pub fn hashring_hash_key(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var key_ptr: [*c]const u8 = undefined;
    var key_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#", &key_ptr, &key_len) == 0) return null;
    const h = HashRing.hashKey(key_ptr[0..@intCast(key_len)]);
    return c.PyLong_FromUnsignedLong(h);
}
