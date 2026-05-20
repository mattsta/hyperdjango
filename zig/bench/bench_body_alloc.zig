//! Component microbench for the response-body ownership transfer in
//! `server.zig:callPythonHandler` (`allocator.dupe(u8, body_slice)` +
//! `PythonResponse.deinit`'s `allocator.free(body)`).
//!
//! WHY THIS EXISTS
//! ---------------
//! The wire benchmark's 64 KiB body cell has a ±4 percentage-point run-to-run
//! noise floor. A change worth single-digit percent cannot be judged there: an
//! A/B would need an impractical number of interleaved rounds to separate the
//! effect from the floor. So instead of trying to measure a DIFFERENCE OF TWO
//! NOISY TOTALS, this bench measures the COMPONENT directly, with error bars
//! small enough to state an upper bound on any end-to-end win.
//!
//! The pattern under test, per request, for a body of N bytes:
//!     dupe   = malloc(N) + memcpy(N) + free(N)     <- what the server does now
//!     retain = memcpy(N) into a per-thread buffer  <- the proposed fix
//! and the two isolated halves, so the report can attribute the cost:
//!     alloc  = malloc(N) + touch + free(N)         <- allocator only
//!     memcpy = memcpy(N) only                      <- copy only
//!
//! RESOLVING-POWER SELF-CHECK
//! --------------------------
//! `alloc2` performs exactly two malloc/free pairs per iteration instead of one:
//! a KNOWN synthetic +100% on the allocator component. If the harness cannot
//! separate `alloc` from `alloc2`, it cannot judge anything, and the run says so.
//! `dupe_delay` adds a fixed spin of `--delay-ns` per iteration: a known additive
//! cost, used to show the harness resolves an injected effect of the same
//! magnitude as the one under investigation.
//!
//! Thread scaling is swept because the prior allocator win in this codebase
//! (jsonWriteString's 6x over-reservation, which crossed glibc's 128 KiB mmap
//! threshold) was a CONTENTION effect that only appeared at high worker counts —
//! throughput per thread got WORSE as threads were added. A 64 KiB block stays
//! under the mmap threshold and lands in a per-thread arena's bins, so the
//! hypothesis is that it does NOT contend. That is a measurable claim; measure it.
//!
//! Build:  zig build-exe zig/bench/bench_body_alloc.zig -OReleaseFast -lc -femit-bin=<out>
//! Run:    <out> --sizes 65536 --threads 1,8,24 --iters 200000 --reps 5 --json <path>

const std = @import("std");

const alloc = std.heap.c_allocator;

// Zig 0.16 removed std.time.Timer / std.posix.{write,open}; this repo's own
// compat shim (zig/src/py.zig) goes straight to libc, so do the same here.
fn monoNs() u64 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
    return @as(u64, @intCast(ts.sec)) * std.time.ns_per_s + @as(u64, @intCast(ts.nsec));
}

extern "c" fn fopen(path: [*:0]const u8, mode: [*:0]const u8) ?*anyopaque;
extern "c" fn fwrite(ptr: [*]const u8, size: usize, n: usize, stream: *anyopaque) usize;
extern "c" fn fclose(stream: *anyopaque) c_int;
extern "c" fn fputs(s: [*:0]const u8, stream: *anyopaque) c_int;
extern "c" fn fflush(stream: ?*anyopaque) c_int;

const Mode = enum {
    dupe, // malloc + memcpy + free   (current server behaviour)
    retain, // memcpy into retained buf  (proposed fix)
    alloc_only, // malloc + touch + free     (allocator component)
    memcpy_only, // memcpy only               (copy component)
    alloc2, // 2x malloc/free            (known +100% self-check)
    dupe_delay, // dupe + fixed spin         (known additive-cost self-check)

    fn name(self: Mode) []const u8 {
        return switch (self) {
            .dupe => "dupe",
            .retain => "retain",
            .alloc_only => "alloc",
            .memcpy_only => "memcpy",
            .alloc2 => "alloc2",
            .dupe_delay => "dupe_delay",
        };
    }
};

const ALL_MODES = [_]Mode{ .dupe, .retain, .alloc_only, .memcpy_only, .alloc2, .dupe_delay };

var g_start: std.atomic.Value(u32) = std.atomic.Value(u32).init(0);
var g_ready: std.atomic.Value(u32) = std.atomic.Value(u32).init(0);
var g_sink: std.atomic.Value(u64) = std.atomic.Value(u64).init(0);

const Ctx = struct {
    mode: Mode,
    size: usize,
    iters: usize,
    src: []const u8,
    delay_ns: u64,
    churn: bool,
    ns: u64 = 0,
};

/// Force the allocation to ESCAPE. Without this, LLVM proves a malloc/free pair
/// whose contents are never observed is dead and deletes it outright — the first
/// draft of this bench measured 1.2 ns/op for `malloc(64KiB) + free`, which is
/// the signature of an elided allocation, not a fast one. A per-thread volatile
/// store (NOT a shared atomic — that would add cross-core cache-line ping-pong
/// and corrupt the thread-scaling sweep) makes the address observable at ~0 cost.
inline fn escape(slot: *usize, p: [*]const u8) void {
    @as(*volatile usize, slot).* = @intFromPtr(p);
}

/// Defeat dead-code elimination without perturbing the measured work: read one
/// byte from each end of the produced buffer into a per-thread accumulator that
/// is published once, after the timed loop.
inline fn consume(acc: *u64, buf: []const u8) void {
    if (buf.len == 0) return;
    acc.* +%= buf[0];
    acc.* +%= buf[buf.len - 1];
}

fn spinNs(ns: u64) void {
    if (ns == 0) return;
    const start = monoNs();
    while (monoNs() - start < ns) {
        std.atomic.spinLoopHint();
    }
}

fn worker(ctx: *Ctx) void {
    // Per-thread retained buffer for the `retain`/`memcpy` modes — this is the
    // exact shape of the proposed fix (grow once, reuse for the thread's life).
    var retained: []u8 = &.{};
    defer if (retained.len > 0) alloc.free(retained);

    if (ctx.mode == .retain or ctx.mode == .memcpy_only) {
        retained = alloc.alloc(u8, @max(ctx.size, 1)) catch @panic("OOM retained");
        // Pre-fault it: the fix's steady state has the buffer already resident,
        // so charging first-touch page faults to it would be a lie.
        @memset(retained, 0);
    }

    var acc: u64 = 0;
    var esc: usize = 0;
    // Small churn allocations, when enabled, keep the allocator's fast-path
    // state from being unrealistically hot for the one size under test.
    var churn_bufs: [4][]u8 = undefined;

    _ = g_ready.fetchAdd(1, .acq_rel);
    while (g_start.load(.acquire) == 0) std.atomic.spinLoopHint();

    const t0 = monoNs();
    var i: usize = 0;
    while (i < ctx.iters) : (i += 1) {
        if (ctx.churn) {
            inline for (0..4) |k| {
                churn_bufs[k] = alloc.alloc(u8, 64 + k * 96) catch &.{};
                if (churn_bufs[k].len > 0) churn_bufs[k][0] = @truncate(i);
            }
        }
        switch (ctx.mode) {
            .dupe, .dupe_delay => {
                const b = alloc.dupe(u8, ctx.src) catch @panic("OOM dupe");
                escape(&esc, b.ptr);
                consume(&acc, b);
                alloc.free(b);
                if (ctx.mode == .dupe_delay) spinNs(ctx.delay_ns);
            },
            .retain => {
                @memcpy(retained[0..ctx.size], ctx.src);
                escape(&esc, retained.ptr);
                consume(&acc, retained[0..ctx.size]);
            },
            .alloc_only => {
                const b = alloc.alloc(u8, @max(ctx.size, 1)) catch @panic("OOM alloc");
                escape(&esc, b.ptr);
                b[0] = @truncate(i);
                acc +%= b[0];
                alloc.free(b);
            },
            .alloc2 => {
                const b = alloc.alloc(u8, @max(ctx.size, 1)) catch @panic("OOM alloc");
                escape(&esc, b.ptr);
                b[0] = @truncate(i);
                acc +%= b[0];
                alloc.free(b);
                const b2 = alloc.alloc(u8, @max(ctx.size, 1)) catch @panic("OOM alloc");
                escape(&esc, b2.ptr);
                b2[0] = @truncate(i);
                acc +%= b2[0];
                alloc.free(b2);
            },
            .memcpy_only => {
                @memcpy(retained[0..ctx.size], ctx.src);
                escape(&esc, retained.ptr);
                consume(&acc, retained[0..ctx.size]);
            },
        }
        if (ctx.churn) {
            inline for (0..4) |k| {
                if (churn_bufs[k].len > 0) alloc.free(churn_bufs[k]);
            }
        }
    }
    ctx.ns = monoNs() - t0;
    acc +%= @as(u64, @truncate(esc));
    _ = g_sink.fetchAdd(acc, .monotonic);
}

const RepResult = struct {
    ns_per_op: f64,
    ops_per_sec: f64,
};

fn runOnce(mode: Mode, size: usize, threads: usize, iters: usize, src: []const u8, delay_ns: u64, churn: bool) RepResult {
    const ctxs = alloc.alloc(Ctx, threads) catch @panic("OOM ctxs");
    defer alloc.free(ctxs);
    const ths = alloc.alloc(std.Thread, threads) catch @panic("OOM threads");
    defer alloc.free(ths);

    g_start.store(0, .release);
    g_ready.store(0, .release);

    for (ctxs, 0..) |*ctx, i| {
        ctx.* = .{ .mode = mode, .size = size, .iters = iters, .src = src, .delay_ns = delay_ns, .churn = churn };
        ths[i] = std.Thread.spawn(.{}, worker, .{ctx}) catch @panic("spawn");
    }
    while (g_ready.load(.acquire) < threads) std.atomic.spinLoopHint();
    g_start.store(1, .release);
    for (ths) |t| t.join();

    // Per-thread ns/op averaged across threads (this is the per-request cost a
    // single worker pays); ops/sec is the aggregate the whole pool sustains.
    var sum_ns: u64 = 0;
    var max_ns: u64 = 0;
    for (ctxs) |ctx| {
        sum_ns += ctx.ns;
        if (ctx.ns > max_ns) max_ns = ctx.ns;
    }
    const mean_ns_per_op = @as(f64, @floatFromInt(sum_ns)) / @as(f64, @floatFromInt(threads * iters));
    const aggregate = @as(f64, @floatFromInt(threads * iters)) / (@as(f64, @floatFromInt(max_ns)) / 1e9);
    return .{ .ns_per_op = mean_ns_per_op, .ops_per_sec = aggregate };
}

fn out(bytes: []const u8) void {
    var buf: [4096]u8 = undefined;
    const n = @min(bytes.len, buf.len - 1);
    @memcpy(buf[0..n], bytes[0..n]);
    buf[n] = 0;
    const so = stdoutStream() orelse return;
    _ = fputs(@ptrCast(&buf), so);
    _ = fflush(so);
}

/// libc's `stdout` FILE* is exposed differently per platform; open /dev/stdout
/// once instead of chasing the symbol.
var _stdout_stream: ?*anyopaque = null;
fn stdoutStream() ?*anyopaque {
    if (_stdout_stream == null) _stdout_stream = fopen("/dev/stdout", "w");
    return _stdout_stream;
}

/// Zig 0.16's ArrayList has no writer(); format into a stack buffer and append.
fn jprint(list: *std.ArrayList(u8), comptime fmt: []const u8, args: anytype) !void {
    var buf: [1024]u8 = undefined;
    const s = try std.fmt.bufPrint(&buf, fmt, args);
    try list.appendSlice(alloc, s);
}

fn median(vals: []f64) f64 {
    std.mem.sort(f64, vals, {}, std.sort.asc(f64));
    const n = vals.len;
    if (n == 0) return 0;
    if (n % 2 == 1) return vals[n / 2];
    return (vals[n / 2 - 1] + vals[n / 2]) / 2.0;
}

fn parseList(comptime T: type, s: []const u8, dst: *std.ArrayList(T)) !void {
    var it = std.mem.splitScalar(u8, s, ',');
    while (it.next()) |tok| {
        const t = std.mem.trim(u8, tok, " ");
        if (t.len == 0) continue;
        try dst.append(alloc, try std.fmt.parseInt(T, t, 10));
    }
}

pub fn main(init: std.process.Init.Minimal) !void {
    var args = init.args.iterate();
    _ = args.next();

    var sizes: std.ArrayList(usize) = .empty;
    defer sizes.deinit(alloc);
    var threads: std.ArrayList(usize) = .empty;
    defer threads.deinit(alloc);
    var iters: usize = 100_000;
    var reps: usize = 5;
    var delay_ns: u64 = 200;
    var churn = false;
    var json_path: ?[]const u8 = null;
    var label: []const u8 = "run";

    while (args.next()) |a| {
        if (std.mem.eql(u8, a, "--sizes")) {
            try parseList(usize, args.next() orelse "", &sizes);
        } else if (std.mem.eql(u8, a, "--threads")) {
            try parseList(usize, args.next() orelse "", &threads);
        } else if (std.mem.eql(u8, a, "--iters")) {
            iters = try std.fmt.parseInt(usize, args.next() orelse "0", 10);
        } else if (std.mem.eql(u8, a, "--reps")) {
            reps = try std.fmt.parseInt(usize, args.next() orelse "0", 10);
        } else if (std.mem.eql(u8, a, "--delay-ns")) {
            delay_ns = try std.fmt.parseInt(u64, args.next() orelse "0", 10);
        } else if (std.mem.eql(u8, a, "--churn")) {
            churn = true;
        } else if (std.mem.eql(u8, a, "--json")) {
            json_path = args.next();
        } else if (std.mem.eql(u8, a, "--label")) {
            label = args.next() orelse "run";
        }
    }
    if (sizes.items.len == 0) try sizes.append(alloc, 65536);
    if (threads.items.len == 0) try threads.append(alloc, 1);

    var json: std.ArrayList(u8) = .empty;
    defer json.deinit(alloc);
    try jprint(&json, "{{\"label\":\"{s}\",\"iters\":{d},\"reps\":{d},\"churn\":{},\"delay_ns\":{d},\"rows\":[", .{ label, iters, reps, churn, delay_ns });
    var first = true;

    var obuf: [4096]u8 = undefined;

    for (sizes.items) |size| {
        const src = try alloc.alloc(u8, @max(size, 1));
        defer alloc.free(src);
        for (src, 0..) |*b, i| b.* = @truncate(i);

        for (threads.items) |nt| {
            const line = try std.fmt.bufPrint(&obuf, "\n== size={d}B threads={d} iters={d} reps={d} churn={} ==\n", .{ size, nt, iters, reps, churn });
            out(line);

            for (ALL_MODES) |mode| {
                var ns_samples: std.ArrayList(f64) = .empty;
                defer ns_samples.deinit(alloc);
                var ops_samples: std.ArrayList(f64) = .empty;
                defer ops_samples.deinit(alloc);

                // One untimed warm rep so page tables / arena state are steady.
                _ = runOnce(mode, size, nt, @max(iters / 10, 1000), src, delay_ns, churn);

                var r: usize = 0;
                while (r < reps) : (r += 1) {
                    const res = runOnce(mode, size, nt, iters, src, delay_ns, churn);
                    try ns_samples.append(alloc, res.ns_per_op);
                    try ops_samples.append(alloc, res.ops_per_sec);
                }
                const lo = std.mem.min(f64, ns_samples.items);
                const hi = std.mem.max(f64, ns_samples.items);
                const med = median(ns_samples.items);
                const ops_med = median(ops_samples.items);
                const spread_pct = if (med > 0) (hi - lo) / med * 100.0 else 0.0;

                const l2 = try std.fmt.bufPrint(&obuf, "  {s:<11} {d:>10.1} ns/op  (min {d:.1} max {d:.1}, spread {d:.2}%)  agg {d:.0} ops/s\n", .{ mode.name(), med, lo, hi, spread_pct, ops_med });
                out(l2);

                if (!first) try json.appendSlice(alloc, ",");
                first = false;
                try jprint(&json, "{{\"mode\":\"{s}\",\"size\":{d},\"threads\":{d},\"ns_per_op_median\":{d:.4},\"ns_per_op_min\":{d:.4},\"ns_per_op_max\":{d:.4},\"spread_pct\":{d:.4},\"ops_per_sec_median\":{d:.2}}}", .{ mode.name(), size, nt, med, lo, hi, spread_pct, ops_med });
            }
        }
    }
    try json.appendSlice(alloc, "]}\n");

    if (json_path) |p| {
        var pbuf: [512]u8 = undefined;
        const pz = try std.fmt.bufPrintZ(&pbuf, "{s}", .{p});
        const f = fopen(pz, "w") orelse return error.CannotOpenOutput;
        _ = fwrite(json.items.ptr, 1, json.items.len, f);
        _ = fclose(f);
        const l = try std.fmt.bufPrint(&obuf, "\n-> json: {s}\n", .{p});
        out(l);
    }
    // Publish the sink so nothing above is optimised away.
    const l = try std.fmt.bufPrint(&obuf, "sink={d}\n", .{g_sink.load(.monotonic)});
    out(l);
}
