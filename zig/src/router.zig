// Optimized radix trie router with parameterized path matching.
//
// Supports static segments, parameterized segments ({id}), and wildcard (*path).
// Zero-allocation lookups (except wildcard join).
//
// Optimizations over previous HashMap-based implementation:
//   1. Sorted StaticChild array replaces StringHashMap for children (no hash overhead)
//   2. Fixed [METHOD_COUNT] array replaces StringHashMap for handlers (O(1) method lookup)
//   3. Path compression: single-child static chains merged into one node
//   4. @prefetch on child node during walk to hide memory latency
//   5. Linear scan for small fan-out (≤6), binary search for larger

const std = @import("std");

const Allocator = std.mem.Allocator;

// ── Public types ────────────────────────────────────────────────────────────

pub const MAX_ROUTE_PARAMS = 16;

pub const RouteParam = struct {
    key: []const u8,
    value: []const u8,
};

/// Zero-alloc route params — fixed-size stack array instead of HashMap.
pub const RouteParams = struct {
    items_buf: [MAX_ROUTE_PARAMS]RouteParam = undefined,
    len: usize = 0,

    pub fn get(self: *const RouteParams, key: []const u8) ?[]const u8 {
        for (self.items_buf[0..self.len]) |p| {
            if (std.mem.eql(u8, p.key, key)) return p.value;
        }
        return null;
    }

    pub fn put(self: *RouteParams, key: []const u8, value: []const u8) void {
        if (self.len < MAX_ROUTE_PARAMS) {
            self.items_buf[self.len] = .{ .key = key, .value = value };
            self.len += 1;
        } else {
            std.debug.print("[WARN] Route has >{d} params — excess dropped: {s}\n", .{ MAX_ROUTE_PARAMS, key });
        }
    }

    pub fn removeLast(self: *RouteParams) void {
        if (self.len > 0) self.len -= 1;
    }

    pub fn entries(self: *const RouteParams) []const RouteParam {
        return self.items_buf[0..self.len];
    }
};

pub const RouteMatch = struct {
    handler_key: []const u8,
    params: RouteParams = .{},
    /// Heap-allocated values that this match owns (e.g. joined wildcard paths)
    owned_values: std.ArrayListUnmanaged([]const u8) = .empty,
    alloc: Allocator,
    /// Opaque dispatch pointer embedded in the matched trie node (Part 3): the
    /// integer value of a `*DispatchEntry` that the server eagerly stamps into
    /// the node at startup (see `forEachHandler`). 0 when unpopulated, in which
    /// case the caller falls back to its dispatch map. Lets the hot path resolve
    /// the handler kind WITHOUT re-hashing `handler_key` through a second map.
    data: usize = 0,

    pub fn deinit(self: *RouteMatch) void {
        for (self.owned_values.items) |v| {
            self.alloc.free(v);
        }
        self.owned_values.deinit(self.alloc);
    }
};

/// Result of a successful handler lookup: the registered handler key plus the
/// opaque dispatch pointer embedded in the owning trie node (Part 3).
pub const HandlerHit = struct {
    key: []const u8,
    data: usize,
};

// ── HTTP Method enum for O(1) handler lookup ─────────────────────────────

pub const Method = enum(u4) {
    GET = 0,
    HEAD = 1,
    POST = 2,
    PUT = 3,
    DELETE = 4,
    PATCH = 5,
    OPTIONS = 6,
    CONNECT = 7,
    TRACE = 8,

    pub const COUNT = 9;

    pub fn fromString(s: []const u8) ?Method {
        return switch (s.len) {
            3 => switch (s[0]) {
                'G' => if (s[1] == 'E' and s[2] == 'T') .GET else null,
                'P' => if (s[1] == 'U' and s[2] == 'T') .PUT else null,
                else => null,
            },
            4 => switch (s[0]) {
                'H' => if (std.mem.eql(u8, s, "HEAD")) .HEAD else null,
                'P' => if (std.mem.eql(u8, s, "POST")) .POST else null,
                else => null,
            },
            5 => switch (s[0]) {
                'P' => if (std.mem.eql(u8, s, "PATCH")) .PATCH else null,
                'T' => if (std.mem.eql(u8, s, "TRACE")) .TRACE else null,
                else => null,
            },
            6 => if (std.mem.eql(u8, s, "DELETE")) .DELETE else null,
            7 => switch (s[0]) {
                'O' => if (std.mem.eql(u8, s, "OPTIONS")) .OPTIONS else null,
                'C' => if (std.mem.eql(u8, s, "CONNECT")) .CONNECT else null,
                else => null,
            },
            else => null,
        };
    }
};

// ── Route Node ──────────────────────────────────────────────────────────────

const StaticChild = struct {
    segment: []const u8,
    node: *RouteNode,
};

/// Overflow handler for non-standard HTTP methods (WebDAV, etc.)
const CustomHandler = struct {
    method: []const u8,
    handler_key: []const u8,
    /// Embedded dispatch pointer (see RouteNode.handler_data) for this custom
    /// method's handler. Stamped once at startup; read-only while serving.
    data: usize = 0,
};

const RouteNode = struct {
    // Handlers: fixed array indexed by Method enum — O(1) lookup
    handlers: [Method.COUNT]?[]const u8 = .{null} ** Method.COUNT,
    // Embedded dispatch pointers, parallel to `handlers` (Part 3). Each slot
    // holds the integer value of a `*DispatchEntry` stamped once at server
    // startup (forEachHandler) so the hot path resolves the handler kind from
    // the trie node directly, WITHOUT a second hash-map probe. 0 = unpopulated
    // (caller falls back to its dispatch map). Written once before any worker
    // runs, then read-only for the serving lifetime — no per-request mutation,
    // no data race under free-threading.
    handler_data: [Method.COUNT]usize = .{0} ** Method.COUNT,
    // Overflow for non-standard methods
    custom_handlers: ?[]CustomHandler = null,

    // Static children: sorted by segment for binary/linear search
    static_children: []StaticChild = &.{},

    // Dynamic children
    param_child: ?*RouteNode = null,
    wildcard_child: ?*RouteNode = null,
    param_name: ?[]const u8 = null,

    // Path compression: chains of single static children with no handlers/param/wildcard
    // Stores slash-separated compressed segments (e.g., "api/v1/users")
    compressed_prefix: ?[]const u8 = null,
    compressed_depth: u8 = 0, // number of segments in compressed prefix
    compressed_child: ?*RouteNode = null, // child at end of compressed path

    fn getHandler(self: *const RouteNode, method: []const u8) ?[]const u8 {
        if (Method.fromString(method)) |m| {
            return self.handlers[@intFromEnum(m)];
        }
        // Non-standard method: linear scan overflow
        if (self.custom_handlers) |customs| {
            for (customs) |ch| {
                if (std.mem.eql(u8, ch.method, method)) return ch.handler_key;
            }
        }
        return null;
    }

    /// Like getHandler but also surfaces the node's embedded dispatch pointer
    /// (Part 3), so a lookup returns both the handler key and the resolved
    /// `*DispatchEntry` value in one shot.
    fn getHandlerHit(self: *const RouteNode, method: []const u8) ?HandlerHit {
        if (Method.fromString(method)) |m| {
            const idx = @intFromEnum(m);
            if (self.handlers[idx]) |k| return .{ .key = k, .data = self.handler_data[idx] };
            return null;
        }
        if (self.custom_handlers) |customs| {
            for (customs) |ch| {
                if (std.mem.eql(u8, ch.method, method)) return .{ .key = ch.handler_key, .data = ch.data };
            }
        }
        return null;
    }

    fn setHandler(self: *RouteNode, alloc: Allocator, method: []const u8, handler_key: []const u8) !void {
        const owned_key = try alloc.dupe(u8, handler_key);
        if (Method.fromString(method)) |m| {
            const idx = @intFromEnum(m);
            if (self.handlers[idx]) |old| alloc.free(old);
            self.handlers[idx] = owned_key;
        } else {
            // Non-standard method: append to custom_handlers
            const owned_method = try alloc.dupe(u8, method);
            if (self.custom_handlers) |old| {
                // Check if exists
                for (old) |*ch| {
                    if (std.mem.eql(u8, ch.method, method)) {
                        alloc.free(ch.handler_key);
                        ch.handler_key = owned_key;
                        alloc.free(owned_method);
                        return;
                    }
                }
                const new = try alloc.alloc(CustomHandler, old.len + 1);
                @memcpy(new[0..old.len], old);
                new[old.len] = .{ .method = owned_method, .handler_key = owned_key };
                alloc.free(old);
                self.custom_handlers = new;
            } else {
                const new = try alloc.alloc(CustomHandler, 1);
                new[0] = .{ .method = owned_method, .handler_key = owned_key };
                self.custom_handlers = new;
            }
        }
    }

    fn hasAnyHandler(self: *const RouteNode) bool {
        for (self.handlers) |h| {
            if (h != null) return true;
        }
        return self.custom_handlers != null;
    }

    /// Find a static child by segment name.
    /// Uses linear scan for ≤6 children, binary search for >6.
    fn findStaticChild(self: *const RouteNode, segment: []const u8) ?*RouteNode {
        const children = self.static_children;
        if (children.len == 0) return null;

        if (children.len <= 6) {
            // Linear scan — fits in 1-2 cache lines, no branch misprediction
            for (children) |child| {
                if (std.mem.eql(u8, child.segment, segment)) return child.node;
            }
            return null;
        }

        // Binary search on sorted children
        var lo: usize = 0;
        var hi: usize = children.len;
        while (lo < hi) {
            const mid = lo + (hi - lo) / 2;
            const cmp = std.mem.order(u8, children[mid].segment, segment);
            switch (cmp) {
                .eq => return children[mid].node,
                .lt => lo = mid + 1,
                .gt => hi = mid,
            }
        }
        return null;
    }

    /// Insert a static child in sorted position.
    fn addStaticChild(self: *RouteNode, alloc: Allocator, segment: []const u8, child: *RouteNode) !void {
        const owned_seg = try alloc.dupe(u8, segment);
        const old = self.static_children;
        const new = try alloc.alloc(StaticChild, old.len + 1);

        // Find insertion position (maintain sorted order)
        var insert_pos: usize = 0;
        for (old, 0..) |c, i| {
            if (std.mem.order(u8, c.segment, owned_seg) != .lt) break;
            new[i] = c;
            insert_pos = i + 1;
        }
        new[insert_pos] = .{ .segment = owned_seg, .node = child };
        if (insert_pos < old.len) {
            @memcpy(new[insert_pos + 1 ..][0 .. old.len - insert_pos], old[insert_pos..]);
        }

        if (old.len > 0) alloc.free(old);
        self.static_children = new;
    }

    fn deinit(self: *RouteNode, alloc: Allocator) void {
        // Free static children
        for (self.static_children) |child| {
            child.node.deinit(alloc);
            alloc.destroy(child.node);
            alloc.free(child.segment);
        }
        if (self.static_children.len > 0) alloc.free(self.static_children);

        // Free param child
        if (self.param_child) |pc| {
            pc.deinit(alloc);
            alloc.destroy(pc);
        }

        // Free wildcard child
        if (self.wildcard_child) |wc| {
            wc.deinit(alloc);
            alloc.destroy(wc);
        }

        // Free compressed child
        if (self.compressed_child) |cc| {
            cc.deinit(alloc);
            alloc.destroy(cc);
        }

        // Free owned strings
        if (self.param_name) |pn| alloc.free(pn);
        if (self.compressed_prefix) |cp| alloc.free(cp);
        for (self.handlers) |h| {
            if (h) |hk| alloc.free(hk);
        }
        if (self.custom_handlers) |customs| {
            for (customs) |ch| {
                alloc.free(ch.method);
                alloc.free(ch.handler_key);
            }
            alloc.free(customs);
        }
    }
};

// ── Router ──────────────────────────────────────────────────────────────────

pub const Router = struct {
    root: *RouteNode,
    alloc: Allocator,

    pub fn init(alloc: Allocator) Router {
        const root = alloc.create(RouteNode) catch @panic("OOM");
        root.* = .{};
        return .{ .root = root, .alloc = alloc };
    }

    pub fn deinit(self: *Router) void {
        self.root.deinit(self.alloc);
        self.alloc.destroy(self.root);
    }

    /// Add a route pattern. `handler_key` is stored as-is.
    pub fn addRoute(self: *Router, method: []const u8, path: []const u8, handler_key: []const u8) !void {
        if (path.len == 0 or path[0] != '/') return error.InvalidPath;

        const segments = try parsePath(self.alloc, path);
        defer self.alloc.free(segments);

        try self.insertRoute(self.root, segments, method, handler_key);
    }

    /// Fill a caller-provided `*RouteMatch` in place (Part 4) and return whether
    /// a route matched. Zero-alloc for static and parameterized routes (only a
    /// wildcard join allocates). Filling `out` in place — writing only
    /// `out.params.items_buf[0..len]` — eliminates the unconditional ~512-byte
    /// by-value copy of the fixed `RouteParams` buffer that returning a
    /// `RouteMatch` by value incurred on EVERY request.
    ///
    /// `out` is fully (re)initialized here: on a miss it is left with an empty,
    /// safe-to-`deinit` `owned_values` and touches no other field the caller
    /// reads; on a hit every field is set. The caller owns `out.deinit()`.
    pub fn findRouteInto(self: *const Router, method: []const u8, path: []const u8, out: *RouteMatch) bool {
        out.params.len = 0;
        out.owned_values = .empty;
        out.alloc = self.alloc;
        out.data = 0;

        const trimmed = if (path.len > 0 and path[0] == '/') path[1..] else path;

        var segments_buf: [64][]const u8 = undefined;
        var seg_count: usize = 0;

        if (trimmed.len > 0) {
            var it = std.mem.splitScalar(u8, trimmed, '/');
            while (it.next()) |seg| {
                if (seg.len == 0) continue; // collapse sequential slashes
                if (seg_count >= segments_buf.len) return false;
                segments_buf[seg_count] = seg;
                seg_count += 1;
            }
        }
        const segments = segments_buf[0..seg_count];

        if (self.findHandler(self.root, segments, 0, method, &out.params, &out.owned_values)) |hit| {
            out.handler_key = hit.key;
            out.data = hit.data;
            return true;
        }
        // No wildcard join can have been appended on a miss (findHandler only
        // appends immediately before returning a hit), so owned_values is empty.
        return false;
    }

    /// Value-returning convenience wrapper over `findRouteInto`. Retained for
    /// tests and non-hot-path callers; the per-request server hot path uses
    /// `findRouteInto` to avoid the by-value `RouteMatch` copy.
    pub fn findRoute(self: *const Router, method: []const u8, path: []const u8) ?RouteMatch {
        var m: RouteMatch = undefined;
        m.owned_values = .empty;
        if (self.findRouteInto(method, path, &m)) return m;
        return null;
    }

    // ── Internal ────────────────────────────────────────────────────────

    fn insertRoute(self: *Router, node: *RouteNode, segments: []const Segment, method: []const u8, handler_key: []const u8) Allocator.Error!void {
        if (segments.len == 0) {
            try node.setHandler(self.alloc, method, handler_key);
            return;
        }

        const seg = segments[0];
        const rest = segments[1..];

        switch (seg) {
            .static => |name| {
                // Check if this node has a compressed prefix
                if (node.compressed_prefix != null) {
                    try self.splitCompressed(node, name, rest, method, handler_key);
                    return;
                }

                if (node.findStaticChild(name)) |child| {
                    try self.insertRoute(child, rest, method, handler_key);
                } else {
                    const child = try self.alloc.create(RouteNode);
                    child.* = .{};
                    try node.addStaticChild(self.alloc, name, child);
                    try self.insertRoute(child, rest, method, handler_key);
                }
            },
            .param => |param_name| {
                if (node.param_child == null) {
                    const child = try self.alloc.create(RouteNode);
                    child.* = .{};
                    child.param_name = try self.alloc.dupe(u8, param_name);
                    node.param_child = child;
                }
                try self.insertRoute(node.param_child.?, rest, method, handler_key);
            },
            .wildcard => |param_name| {
                const child = if (node.wildcard_child) |wc| wc else blk: {
                    const c = try self.alloc.create(RouteNode);
                    c.* = .{};
                    c.param_name = try self.alloc.dupe(u8, param_name);
                    node.wildcard_child = c;
                    break :blk c;
                };
                try child.setHandler(self.alloc, method, handler_key);
            },
        }
    }

    /// Handle insertion when a node has a compressed prefix that may need splitting.
    fn splitCompressed(self: *Router, node: *RouteNode, first_seg: []const u8, rest: []const Segment, method: []const u8, handler_key: []const u8) Allocator.Error!void {
        const prefix = node.compressed_prefix.?;
        var prefix_it = std.mem.splitScalar(u8, prefix, '/');

        // Compare the first compressed segment with the new segment
        const first_compressed = prefix_it.next().?;

        if (std.mem.eql(u8, first_compressed, first_seg)) {
            // First segment matches — need to check deeper
            // Collect remaining compressed segments
            var remaining_parts: [64][]const u8 = undefined;
            var remaining_count: usize = 0;
            while (prefix_it.next()) |part| {
                // Defensive bound: compressed_depth is a u8 and routes are
                // inserted with at most segments_buf.len segments, so this
                // never truncates in valid operation, but guard the fixed
                // buffer so a malformed/oversized prefix can't overflow it.
                if (remaining_count >= remaining_parts.len) break;
                remaining_parts[remaining_count] = part;
                remaining_count += 1;
            }

            if (remaining_count == 0) {
                // Entire compressed prefix matched — continue into compressed child
                if (node.compressed_child) |cc| {
                    // Build full segment list: rest
                    try self.insertRoute(cc, rest, method, handler_key);
                } else {
                    // Create compressed child if needed
                    const cc = try self.alloc.create(RouteNode);
                    cc.* = .{};
                    node.compressed_child = cc;
                    try self.insertRoute(cc, rest, method, handler_key);
                }
            } else {
                // Partial match — need to split at divergence point
                // Create intermediate node for the matched portion
                const old_child = node.compressed_child;

                // Build new compressed prefix for the remaining part
                var remain_len: usize = 0;
                for (remaining_parts[0..remaining_count], 0..) |p, i| {
                    if (i > 0) remain_len += 1;
                    remain_len += p.len;
                }

                const new_mid = try self.alloc.create(RouteNode);
                new_mid.* = .{};

                if (remaining_count == 1) {
                    // Single remaining segment — just add as static child
                    const final_node = old_child orelse blk: {
                        const n = try self.alloc.create(RouteNode);
                        n.* = .{};
                        break :blk n;
                    };
                    try new_mid.addStaticChild(self.alloc, remaining_parts[0], final_node);
                } else {
                    // Multiple remaining — create compressed child
                    const remain_str = try self.alloc.alloc(u8, remain_len);
                    var pos: usize = 0;
                    for (remaining_parts[0..remaining_count], 0..) |p, i| {
                        if (i > 0) {
                            remain_str[pos] = '/';
                            pos += 1;
                        }
                        @memcpy(remain_str[pos..][0..p.len], p);
                        pos += p.len;
                    }
                    new_mid.compressed_prefix = remain_str;
                    new_mid.compressed_depth = @intCast(remaining_count);
                    new_mid.compressed_child = old_child;
                }

                // Update current node: remove compression, add new_mid as static child
                self.alloc.free(node.compressed_prefix.?);
                node.compressed_prefix = null;
                node.compressed_depth = 0;
                node.compressed_child = null;
                try node.addStaticChild(self.alloc, first_seg, new_mid);

                // Now insert the new route into new_mid
                try self.insertRoute(new_mid, rest, method, handler_key);
            }
        } else {
            // First segment doesn't match — decompress entirely
            // Create a node for the compressed path
            const compressed_node = try self.alloc.create(RouteNode);
            compressed_node.* = .{};

            // Collect remaining compressed segments for the old path
            var old_parts: [64][]const u8 = undefined;
            var old_count: usize = 0;
            while (prefix_it.next()) |part| {
                // Defensive bound (see splitCompressed's other fill): guard the
                // fixed buffer so an oversized compressed prefix cannot overflow.
                if (old_count >= old_parts.len) break;
                old_parts[old_count] = part;
                old_count += 1;
            }

            if (old_count == 0) {
                // first_compressed was the only segment — compressed_child is the endpoint
                if (node.compressed_child) |cc| {
                    compressed_node.* = cc.*;
                    // Don't recursively deinit cc since we moved its data
                    self.alloc.destroy(cc);
                }
            } else {
                // Rebuild remaining compressed path
                var remain_len: usize = 0;
                for (old_parts[0..old_count], 0..) |p, i| {
                    if (i > 0) remain_len += 1;
                    remain_len += p.len;
                }
                const remain_str = try self.alloc.alloc(u8, remain_len);
                var pos: usize = 0;
                for (old_parts[0..old_count], 0..) |p, i| {
                    if (i > 0) {
                        remain_str[pos] = '/';
                        pos += 1;
                    }
                    @memcpy(remain_str[pos..][0..p.len], p);
                    pos += p.len;
                }
                compressed_node.compressed_prefix = remain_str;
                compressed_node.compressed_depth = @intCast(old_count);
                compressed_node.compressed_child = node.compressed_child;
            }

            // Clear compression from current node
            self.alloc.free(node.compressed_prefix.?);
            node.compressed_prefix = null;
            node.compressed_depth = 0;
            node.compressed_child = null;

            // Add old compressed path as static child
            try node.addStaticChild(self.alloc, first_compressed, compressed_node);

            // Now insert the new route normally
            const new_child = try self.alloc.create(RouteNode);
            new_child.* = .{};
            try node.addStaticChild(self.alloc, first_seg, new_child);
            try self.insertRoute(new_child, rest, method, handler_key);
        }
    }

    fn findHandler(
        self: *const Router,
        node: *const RouteNode,
        segments: []const []const u8,
        index: usize,
        method: []const u8,
        params: *RouteParams,
        owned: *std.ArrayListUnmanaged([]const u8),
    ) ?HandlerHit {
        // Handle compressed prefix: match multiple segments at once
        if (node.compressed_prefix) |prefix| {
            var prefix_it = std.mem.splitScalar(u8, prefix, '/');
            var idx = index;
            while (prefix_it.next()) |expected| {
                if (idx >= segments.len) return null;
                if (!std.mem.eql(u8, segments[idx], expected)) return null;
                idx += 1;
            }
            // All compressed segments matched — continue into compressed child
            if (node.compressed_child) |cc| {
                // Prefetch the child node
                @prefetch(cc, .{ .rw = .read, .locality = 3 });
                return self.findHandler(cc, segments, idx, method, params, owned);
            }
            // No child but all segments consumed — check handlers on this node
            if (idx >= segments.len) {
                return node.getHandlerHit(method);
            }
            return null;
        }

        if (index >= segments.len) {
            return node.getHandlerHit(method);
        }

        const segment = segments[index];

        // 1. Try static match first (highest priority)
        if (node.findStaticChild(segment)) |child| {
            // Prefetch child's static_children array for next iteration
            if (child.static_children.len > 0) {
                @prefetch(child.static_children.ptr, .{ .rw = .read, .locality = 3 });
            }
            if (self.findHandler(child, segments, index + 1, method, params, owned)) |h| {
                return h;
            }
        }

        // 2. Try parameter match
        if (node.param_child) |param_child| {
            if (param_child.param_name) |pname| {
                params.put(pname, segment);
                if (self.findHandler(param_child, segments, index + 1, method, params, owned)) |h| {
                    return h;
                }
                params.removeLast();
            }
        }

        // 3. Try wildcard match (matches rest of path)
        if (node.wildcard_child) |wc| {
            if (wc.param_name) |pname| {
                if (wc.getHandlerHit(method)) |wc_hit| {
                    // Reject path traversal — literal and percent-encoded variants
                    for (segments[index..]) |s| {
                        if (s.len == 0) return null; // empty segment (double slash)
                        if (std.mem.eql(u8, s, "..") or std.mem.eql(u8, s, ".")) return null;
                        // Percent-encoded: %2e = '.', %2E = '.'
                        if (std.mem.eql(u8, s, "%2e%2e") or std.mem.eql(u8, s, "%2E%2E") or
                            std.mem.eql(u8, s, "%2e%2E") or std.mem.eql(u8, s, "%2E%2e")) return null;
                        if (std.mem.eql(u8, s, "%2e") or std.mem.eql(u8, s, "%2E")) return null;
                        // Mixed: .%2e, %2e., .%2E, %2E.
                        if (std.mem.eql(u8, s, ".%2e") or std.mem.eql(u8, s, ".%2E") or
                            std.mem.eql(u8, s, "%2e.") or std.mem.eql(u8, s, "%2E.")) return null;
                        // Reject null bytes
                        if (std.mem.indexOfScalar(u8, s, 0) != null) return null;
                    }
                    // Join remaining segments with '/'
                    var total_len: usize = 0;
                    for (segments[index..]) |s| {
                        if (total_len > 0) total_len += 1;
                        total_len += s.len;
                    }
                    const joined = self.alloc.alloc(u8, total_len) catch return null;
                    var pos: usize = 0;
                    for (segments[index..]) |s| {
                        if (pos > 0) {
                            joined[pos] = '/';
                            pos += 1;
                        }
                        @memcpy(joined[pos..][0..s.len], s);
                        pos += s.len;
                    }
                    params.put(pname, joined);
                    // NB: findHandler returns ?HandlerHit (not an error union), so
                    // an `errdefer` would NOT fire on `return null` — free `joined`
                    // explicitly if it can't be tracked in `owned`. On this miss
                    // `params`/`owned` are never read (see findRouteInto), so the
                    // now-dangling params entry is harmless.
                    owned.append(self.alloc, joined) catch {
                        self.alloc.free(joined);
                        return null;
                    };
                    return wc_hit;
                }
            }
        }

        return null;
    }

    /// Visit every registered handler in the trie exactly once, invoking `cb`
    /// with the handler key and a pointer to that handler's embedded dispatch
    /// slot (Part 3). Called once at server startup so the owner can stamp each
    /// slot with its resolved `*DispatchEntry`; the slots are then read-only for
    /// the serving lifetime.
    pub fn forEachHandler(self: *Router, cb: *const fn (key: []const u8, slot: *usize) void) void {
        walkHandlers(self.root, cb);
    }

    fn walkHandlers(node: *RouteNode, cb: *const fn (key: []const u8, slot: *usize) void) void {
        for (0..Method.COUNT) |i| {
            if (node.handlers[i]) |k| cb(k, &node.handler_data[i]);
        }
        if (node.custom_handlers) |customs| {
            for (customs) |*ch| cb(ch.handler_key, &ch.data);
        }
        for (node.static_children) |child| walkHandlers(child.node, cb);
        if (node.param_child) |pc| walkHandlers(pc, cb);
        if (node.wildcard_child) |wc| walkHandlers(wc, cb);
        if (node.compressed_child) |cc| walkHandlers(cc, cb);
    }

    /// Post-registration optimization: compress chains of single static children.
    pub fn finalize(self: *Router) void {
        self.compressNode(self.root);
    }

    fn compressNode(self: *Router, node: *RouteNode) void {
        // Recursively finalize children first
        for (node.static_children) |child| {
            self.compressNode(child.node);
        }
        if (node.param_child) |pc| self.compressNode(pc);
        if (node.wildcard_child) |wc| self.compressNode(wc);
        if (node.compressed_child) |cc| self.compressNode(cc);

        // Try to compress: if this node has exactly 1 static child,
        // no handlers, no param_child, no wildcard_child, and no compressed_prefix already
        if (node.static_children.len == 1 and
            !node.hasAnyHandler() and
            node.param_child == null and
            node.wildcard_child == null and
            node.compressed_prefix == null)
        {
            const child_entry = node.static_children[0];
            const child = child_entry.node;

            // Check if child can also be absorbed (chain compression)
            if (child.compressed_prefix) |child_prefix| {
                // Child already compressed — merge: "seg/child_prefix"
                const seg = child_entry.segment;
                const total = seg.len + 1 + child_prefix.len;
                const merged = self.alloc.alloc(u8, total) catch return;
                @memcpy(merged[0..seg.len], seg);
                merged[seg.len] = '/';
                @memcpy(merged[seg.len + 1 ..][0..child_prefix.len], child_prefix);

                node.compressed_prefix = merged;
                node.compressed_depth = child.compressed_depth + 1;
                node.compressed_child = child.compressed_child;

                // Move child's non-compressed state up if needed
                // (child shouldn't have handlers/param/wildcard since it was compressible)

                // Free child's compressed prefix and the child node itself
                self.alloc.free(child_prefix);
                child.compressed_prefix = null;
                child.compressed_child = null;
                self.alloc.free(child_entry.segment);
                self.alloc.destroy(child);
            } else {
                // Simple compression: single segment
                node.compressed_prefix = child_entry.segment; // take ownership
                node.compressed_depth = 1;
                node.compressed_child = child;
                // Don't free child_entry.segment — transferred to compressed_prefix
            }

            // Free the static_children array (now empty)
            self.alloc.free(node.static_children);
            node.static_children = &.{};
        }
    }
};

// ── Path parsing ────────────────────────────────────────────────────────────

const Segment = union(enum) {
    static: []const u8,
    param: []const u8,
    wildcard: []const u8,
};

fn parsePath(alloc: Allocator, path: []const u8) ![]const Segment {
    const trimmed = if (path.len > 0 and path[0] == '/') path[1..] else path;

    if (trimmed.len == 0) {
        return try alloc.alloc(Segment, 0);
    }

    // Count non-empty segments (consistent with findRoute which skips empties)
    var count: usize = 0;
    var count_it = std.mem.splitScalar(u8, trimmed, '/');
    while (count_it.next()) |seg| {
        if (seg.len > 0) count += 1;
    }

    const segs = try alloc.alloc(Segment, count);
    var i: usize = 0;
    var it = std.mem.splitScalar(u8, trimmed, '/');
    while (it.next()) |seg| {
        if (seg.len == 0) continue; // collapse sequential/trailing slashes
        if (seg.len >= 2 and seg[0] == '{' and seg[seg.len - 1] == '}') {
            // Strip :type annotation — {id:int} → param name "id"
            const inner = seg[1 .. seg.len - 1];
            const param_name = if (std.mem.indexOfScalar(u8, inner, ':')) |colon_pos|
                inner[0..colon_pos]
            else
                inner;
            segs[i] = .{ .param = param_name };
        } else if (seg.len >= 1 and seg[0] == '*') {
            const name = if (seg.len > 1) seg[1..] else "wildcard";
            segs[i] = .{ .wildcard = name };
        } else {
            segs[i] = .{ .static = seg };
        }
        i += 1;
    }

    return segs;
}

// ── Tests ───────────────────────────────────────────────────────────────────

test "static routes" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/users", "GET /users");

    var m1 = r.findRoute("GET", "/users").?;
    defer m1.deinit();
    try std.testing.expectEqualStrings("GET /users", m1.handler_key);
}

test "multiple methods on same path" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/items", "GET /items");
    try r.addRoute("POST", "/items", "POST /items");

    var m1 = r.findRoute("GET", "/items").?;
    defer m1.deinit();
    try std.testing.expectEqualStrings("GET /items", m1.handler_key);

    var m2 = r.findRoute("POST", "/items").?;
    defer m2.deinit();
    try std.testing.expectEqualStrings("POST /items", m2.handler_key);

    const m3 = r.findRoute("DELETE", "/items");
    try std.testing.expect(m3 == null);
}

test "parameterized routes" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/users/{id}", "GET /users/{id}");

    var m = r.findRoute("GET", "/users/123").?;
    defer m.deinit();
    try std.testing.expectEqualStrings("GET /users/{id}", m.handler_key);
    try std.testing.expectEqualStrings("123", m.params.get("id").?);
}

test "multi-param routes" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/api/v1/users/{id}/posts/{post_id}", "GET /api/v1/users/{id}/posts/{post_id}");

    var m = r.findRoute("GET", "/api/v1/users/42/posts/7").?;
    defer m.deinit();
    try std.testing.expectEqualStrings("42", m.params.get("id").?);
    try std.testing.expectEqualStrings("7", m.params.get("post_id").?);
}

test "wildcard routes" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/files/*path", "GET /files/*path");

    var m = r.findRoute("GET", "/files/docs/readme.txt").?;
    defer m.deinit();
    try std.testing.expectEqualStrings("GET /files/*path", m.handler_key);
    try std.testing.expectEqualStrings("docs/readme.txt", m.params.get("path").?);
}

test "static takes priority over param" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/users/me", "GET /users/me");
    try r.addRoute("GET", "/users/{id}", "GET /users/{id}");

    var m1 = r.findRoute("GET", "/users/me").?;
    defer m1.deinit();
    try std.testing.expectEqualStrings("GET /users/me", m1.handler_key);

    var m2 = r.findRoute("GET", "/users/123").?;
    defer m2.deinit();
    try std.testing.expectEqualStrings("GET /users/{id}", m2.handler_key);
}

test "method mismatch returns null" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/users", "GET /users");

    const m = r.findRoute("DELETE", "/users");
    try std.testing.expect(m == null);
}

test "no match returns null" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/users", "GET /users");

    const m = r.findRoute("GET", "/posts");
    try std.testing.expect(m == null);
}

test "root route" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/", "GET /");

    var m = r.findRoute("GET", "/").?;
    defer m.deinit();
    try std.testing.expectEqualStrings("GET /", m.handler_key);
}

test "path compression via finalize" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/api/v1/users", "GET /api/v1/users");
    r.finalize();

    var m = r.findRoute("GET", "/api/v1/users").?;
    defer m.deinit();
    try std.testing.expectEqualStrings("GET /api/v1/users", m.handler_key);

    // Verify compression happened: root should have compressed_prefix
    try std.testing.expect(r.root.compressed_prefix != null);
}

test "compression with branching" {
    const alloc = std.testing.allocator;
    var r = Router.init(alloc);
    defer r.deinit();

    try r.addRoute("GET", "/api/v1/users", "GET /api/v1/users");
    try r.addRoute("GET", "/api/v1/posts", "GET /api/v1/posts");
    r.finalize();

    var m1 = r.findRoute("GET", "/api/v1/users").?;
    defer m1.deinit();
    try std.testing.expectEqualStrings("GET /api/v1/users", m1.handler_key);

    var m2 = r.findRoute("GET", "/api/v1/posts").?;
    defer m2.deinit();
    try std.testing.expectEqualStrings("GET /api/v1/posts", m2.handler_key);
}

test "method enum parsing" {
    try std.testing.expect(Method.fromString("GET") == .GET);
    try std.testing.expect(Method.fromString("POST") == .POST);
    try std.testing.expect(Method.fromString("PUT") == .PUT);
    try std.testing.expect(Method.fromString("DELETE") == .DELETE);
    try std.testing.expect(Method.fromString("PATCH") == .PATCH);
    try std.testing.expect(Method.fromString("HEAD") == .HEAD);
    try std.testing.expect(Method.fromString("OPTIONS") == .OPTIONS);
    try std.testing.expect(Method.fromString("CONNECT") == .CONNECT);
    try std.testing.expect(Method.fromString("TRACE") == .TRACE);
    try std.testing.expect(Method.fromString("CUSTOM") == null);
    try std.testing.expect(Method.fromString("") == null);
}

// ── Fuzz tests ───────────────────────────────────────────────────────────────

/// Bridge the Zig 0.16 `std.testing.fuzz` `*Smith` callback to a byte slice:
/// replay a concrete corpus entry verbatim (`smith.in`), or draw an
/// arbitrary-length byte string when actively fuzzing (`in == null`). `buf`
/// backs the active-fuzz draw and must outlive the returned slice.
fn fuzzInput(smith: *std.testing.Smith, buf: []u8) []const u8 {
    if (smith.in) |in| return in;
    return buf[0..smith.sliceWithHash(buf, 0)];
}

fn fuzz_findRoute(_: void, smith: *std.testing.Smith) anyerror!void {
    var in_buf: [4096]u8 = undefined;
    const input = fuzzInput(smith, &in_buf);
    if (input.len == 0) return;

    const methods = [_][]const u8{ "GET", "POST", "PUT", "DELETE", "PATCH", "" };
    const method = methods[input[0] % methods.len];
    const path = if (input.len > 1) input[1..] else "/";

    var r = Router.init(std.heap.c_allocator);
    defer r.deinit();

    r.addRoute("GET", "/", "GET /") catch return;
    r.addRoute("GET", "/users", "GET /users") catch return;
    r.addRoute("GET", "/users/{id}", "GET /users/{id}") catch return;
    r.addRoute("POST", "/users", "POST /users") catch return;
    r.addRoute("PUT", "/users/{id}", "PUT /users/{id}") catch return;
    r.addRoute("DELETE", "/users/{id}", "DELETE /users/{id}") catch return;
    r.addRoute("GET", "/items/{cat}/{id}", "GET /items/{cat}/{id}") catch return;
    r.addRoute("GET", "/files/*", "GET /files/*") catch return;
    r.addRoute("GET", "/health", "GET /health") catch return;
    r.finalize();

    if (r.findRoute(method, path)) |match_c| {
        var match = match_c;
        defer match.deinit();
        try std.testing.expect(match.handler_key.len > 0);
    }
}

test "fuzz: router findRoute — never panics, no OOB on any path" {
    try std.testing.fuzz({}, fuzz_findRoute, .{ .corpus = &.{
        "\x00/",
        "\x00/users/42",
        "\x01/users",
        "\x00/users/",
        "\x00/items/books/99",
        "\x00/health",
        "\x00/files/deep/nested/path",
        "\x00" ++ "/" ++ ("a/" ** 70),
        "\x00/\x00secret",
        "\x00/" ++ ("a" ** 4096),
        "\x00/%2F%2F/../admin",
        "\x00/users/%00/profile",
        "\x00//double//slash//path",
        "\x00/users/{injected}",
        "\x00/\xFF\xFE\xFD",
        "\x05/anything",
        "\x00",
    } });
}
