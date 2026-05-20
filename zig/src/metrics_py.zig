// Runtime-dynamic metric registry for Python FFI.
//
// The existing `zig/src/metrics/` module uses comptime-generic types
// (`Counter(u32)`, `CounterVec(u64, struct { method: []const u8 })`)
// which is perfect for Zig callers that know their metric types at
// compile time — but Python is a runtime caller and can't interact
// with comptime-generic types directly.
//
// This module provides a parallel runtime-dynamic registry:
//
//   - `DynCounter` / `DynGauge` / `DynHistogram` / `DynCounterVec` /
//     `DynHistogramVec` — each holds atomics + a preamble string
//   - A global registry: a fixed, pointer-stable slot array of atomic
//     `*MetricEntry` published by an atomic count, indexed by u32 handle
//   - Python calls pass handles (indices) to inc/observe/set
//   - Hot path is atomic RMW only — zero locks after registration
//   - Registration takes a mutex but happens once at process startup
//
// Design notes:
//
//   - Counters and histogram buckets use u64 atomics (monotonic RMW)
//   - Gauges use i64 (can go up and down) plus a separate f64 store
//     for floating-point values
//   - Histogram sum uses a CAS loop on the bits of f64 (no native f64
//     atomic RMW in std.atomic, but load+CAS is correct and fast
//     enough for the histogram observe path)
//   - Labels in Vec variants are joined into a single string key for
//     the hash map, wrapped in a fine-grained RwLock for the per-metric
//     map but NEVER the global registry
//   - Prometheus text output streams to an std.io.Writer, one counter
//     at a time; no intermediate list-of-strings allocation
//
// No protobuf. No OTLP. Everything is Prometheus text or Python tuples
// returned by `_metric_registry_snapshot()` for in-memory test sinks.

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;

// Debug tracing for metric registry operations.
// Enabled in Debug builds, compiled out in Release. Matches the
// pattern used by zig/src/db.zig so the whole native extension
// shares a single tracing discipline — no ad-hoc stderr prints.
const TRACE = @import("builtin").mode == .Debug;

fn trace(comptime fmt: []const u8, args: anytype) void {
    if (TRACE) {
        std.debug.print("[METRIC] " ++ fmt ++ "\n", args);
    }
}

// ── Global state ────────────────────────────────────────────────────────────

const MetricKind = enum(u8) {
    counter = 0,
    gauge = 1,
    histogram = 2,
    counter_vec = 3,
    histogram_vec = 4,
};

pub const MetricEntry = struct {
    kind: MetricKind,
    name: []const u8, // owned, lives for process lifetime
    help: []const u8, // owned, may be empty
    impl: *anyopaque, // points to one of Dyn{Counter,Gauge,...}
};

// Registry storage — a FIXED, pointer-stable slot array + atomic publish.
// This mirrors the lock-free query registry in db.zig so the read path
// (getEntry, on EVERY counter/gauge/histogram inc) is a pair of atomic
// loads with zero locking, and concurrent registration is race-free.
//
// Invariants (see db.zig "Lock-free query registry"):
//   * The slot array never reallocates → indexing is race-free; a slot is
//     only reachable after registerEntry publishes the new count.
//   * Registration serializes on `_registry_mutex` (cold — process startup).
//     It writes the slot (release) BEFORE publishing the incremented count
//     (release). A reader loads count (acquire) — which gates every use —
//     then the slot (acquire); the acquire/release pairing guarantees a
//     reader that observes handle < count also observes the fully-written
//     MetricEntry pointer (never a half-published slot).
//   * MetricEntry values are heap-allocated and never freed, so an entry
//     pointer, once published, stays valid for process lifetime.
const MAX_METRICS: usize = 4096;
var _registry: [MAX_METRICS]std.atomic.Value(?*MetricEntry) =
    [_]std.atomic.Value(?*MetricEntry){std.atomic.Value(?*MetricEntry).init(null)} ** MAX_METRICS;
var _registry_count: std.atomic.Value(u32) = std.atomic.Value(u32).init(0);
var _registry_mutex: py.Mutex = .{};

// Use the C allocator (malloc/free) for metric storage. Page
// allocator calls `mmap` for every small allocation which is ~1μs
// per call — unacceptable for labeled-metric lookup paths where we
// build a joined key per inc(). Malloc is ~20ns for small sizes.
// Metrics live for process lifetime, so we never call free on the
// registry itself, but the Vec label map needs dynamic allocation
// for new slot inserts + key buffers.
const _alloc = std.heap.c_allocator;

// ── Sharded counting cells (mechanical sympathy for hot counters) ───────────
//
// A single atomic counter bumped by every worker on every request is one
// cache line ping-ponging between every core that serves traffic: at ~580k
// responses/sec across 64 workers that is >1M contended RMWs per second on
// one line — the same pattern whose removal from the in-flight gauge was the
// dominant threaded-mode win. The distributed shape: each THREAD claims one
// fixed cache-line-isolated cell (round-robin at first touch, one threadlocal
// read afterwards); `add()` RMWs only the caller's own cell (atomic because
// round-robin can map two threads to one cell once thread count exceeds
// COUNT_CELLS — uncontended-exclusive in the common case, which is the whole
// point); `total()` gathers the sum and runs only on scrape/stats reads
// (~1/sec). Scatter on write, gather on read.

const CACHE_LINE = 64;
// Power of two (cheap masking) and >= the worker counts that matter: with the
// 512-worker auto ceiling, at most 4 threads share a cell. 8 KiB per counter.
const COUNT_CELLS = 128;

const PaddedCell = struct {
    v: std.atomic.Value(u64) align(CACHE_LINE) = std.atomic.Value(u64).init(0),
};

comptime {
    // The padding IS the mechanism: one cell per cache line, no false sharing.
    if (@sizeOf(PaddedCell) != CACHE_LINE) @compileError("PaddedCell must be exactly one cache line");
}

const NO_CELL_SLOT: u32 = 0xFFFF_FFFF;
var _cell_seq = std.atomic.Value(u32).init(0);
threadlocal var _cell_slot: u32 = NO_CELL_SLOT;

inline fn cellSlot() u32 {
    if (_cell_slot == NO_CELL_SLOT) {
        _cell_slot = _cell_seq.fetchAdd(1, .monotonic) & (COUNT_CELLS - 1);
    }
    return _cell_slot;
}

pub const ShardedCount = struct {
    cells: [COUNT_CELLS]PaddedCell align(CACHE_LINE) =
        [_]PaddedCell{.{}} ** COUNT_CELLS,

    pub inline fn add(self: *ShardedCount, amount: u64) void {
        _ = self.cells[cellSlot()].v.fetchAdd(amount, .monotonic);
    }

    pub fn total(self: *const ShardedCount) u64 {
        var t: u64 = 0;
        for (&self.cells) |*cell| t += cell.v.load(.monotonic);
        return t;
    }

    /// Zero every cell. Stats-reset paths only; concurrent add()s during a
    /// reset land before or after it non-deterministically — identical
    /// semantics to resetting the old single shared atomic.
    pub fn reset(self: *ShardedCount) void {
        for (&self.cells) |*cell| cell.v.store(0, .monotonic);
    }
};

// ── DynCounter ──────────────────────────────────────────────────────────────

pub const DynCounter = struct {
    // Sharded interior (see ShardedCount above): inc() writes only the
    // calling thread's cache-line-isolated cell, read() gathers on scrape.
    // Every counter in the registry gets this shape — the hot always-on
    // server counters (responses_total, per-status-class, static hits) are
    // bumped by every worker on every response, and a single shared value
    // there is a guaranteed cross-core cache-line ping-pong.
    count: ShardedCount,

    pub fn init() !*DynCounter {
        const p = try _alloc.create(DynCounter);
        p.* = .{ .count = .{} };
        return p;
    }

    pub fn inc(self: *DynCounter, amount: u64) void {
        self.count.add(amount);
    }

    fn read(self: *const DynCounter) u64 {
        return self.count.total();
    }
};

// ── DynGauge ────────────────────────────────────────────────────────────────

pub const DynGauge = struct {
    // Gauges use two fields: an atomic i64 for set/add, and an f64 bits
    // encoded via u64 CAS for set_float. Only one is "live" at a time —
    // the int_mode flag says which (true = int64, false = f64 bits).
    // For simplicity v0.15 only exposes i64 gauges to Python; float
    // support can come later if needed. Round to nearest i64 for
    // floating inputs.
    value: std.atomic.Value(i64),

    pub fn init() !*DynGauge {
        const p = try _alloc.create(DynGauge);
        p.* = .{ .value = std.atomic.Value(i64).init(0) };
        return p;
    }

    pub fn set(self: *DynGauge, v: i64) void {
        self.value.store(v, .monotonic);
    }

    pub fn add(self: *DynGauge, delta: i64) void {
        _ = self.value.fetchAdd(delta, .monotonic);
    }

    fn read(self: *const DynGauge) i64 {
        return self.value.load(.monotonic);
    }
};

// ── DynHistogram ────────────────────────────────────────────────────────────

const DynHistogram = struct {
    // Prometheus-style histogram: fixed upper_bounds, one atomic counter
    // per bucket, plus atomic sum (bits of f64) and atomic count.
    // Observation is: find the first bucket with upper_bound >= value,
    // increment that bucket's counter + count + sum. Buckets are
    // NON-cumulative in storage; cumulative output is computed at
    // scrape time.
    upper_bounds: []const f64, // owned
    bucket_counts: []std.atomic.Value(u64), // owned, len == upper_bounds.len
    sum_bits: std.atomic.Value(u64), // bits of f64
    count: std.atomic.Value(u64),

    fn init(buckets: []const f64) !*DynHistogram {
        const p = try _alloc.create(DynHistogram);
        const owned_bounds = try _alloc.alloc(f64, buckets.len);
        @memcpy(owned_bounds, buckets);
        const counters = try _alloc.alloc(std.atomic.Value(u64), buckets.len);
        for (counters) |*cnt| cnt.* = std.atomic.Value(u64).init(0);
        p.* = .{
            .upper_bounds = owned_bounds,
            .bucket_counts = counters,
            .sum_bits = std.atomic.Value(u64).init(0),
            .count = std.atomic.Value(u64).init(0),
        };
        return p;
    }

    fn observe(self: *DynHistogram, value: f64) void {
        _ = self.count.fetchAdd(1, .monotonic);
        // CAS loop to add `value` to sum stored as bits of f64.
        var old_bits = self.sum_bits.load(.monotonic);
        while (true) {
            const old_f: f64 = @bitCast(old_bits);
            const new_f = old_f + value;
            const new_bits: u64 = @bitCast(new_f);
            const result = self.sum_bits.cmpxchgWeak(
                old_bits,
                new_bits,
                .monotonic,
                .monotonic,
            );
            if (result == null) break;
            old_bits = result.?;
        }
        // Find target bucket — first bucket whose upper_bound >= value.
        for (self.upper_bounds, 0..) |upper, idx| {
            if (value <= upper) {
                _ = self.bucket_counts[idx].fetchAdd(1, .monotonic);
                return;
            }
        }
        // Value is above all explicit buckets — the +Inf bucket is
        // implicit (derived from count at scrape time), so no per-bucket
        // increment needed.
    }

    fn readBuckets(self: *const DynHistogram, out: []u64) void {
        std.debug.assert(out.len == self.bucket_counts.len);
        for (self.bucket_counts, 0..) |*cnt, i| {
            out[i] = cnt.load(.monotonic);
        }
    }

    fn readCount(self: *const DynHistogram) u64 {
        return self.count.load(.monotonic);
    }

    fn readSum(self: *const DynHistogram) f64 {
        return @bitCast(self.sum_bits.load(.monotonic));
    }
};

// ── DynCounterVec ───────────────────────────────────────────────────────────
//
// Labeled counter family. Label values are joined with a NUL byte into
// a single lookup key. Per-metric RwLock guards the HashMap; individual
// counters are atomic once the slot exists.

const DynCounterVec = struct {
    label_names: [][]const u8, // owned
    values: std.StringHashMapUnmanaged(*DynCounter), // key: joined label string
    lock: py.RwLock,

    fn init(label_names: []const []const u8) !*DynCounterVec {
        const p = try _alloc.create(DynCounterVec);
        const owned_names = try _alloc.alloc([]const u8, label_names.len);
        for (label_names, 0..) |n, i| {
            owned_names[i] = try _alloc.dupe(u8, n);
        }
        p.* = .{
            .label_names = owned_names,
            .values = .{},
            .lock = .{},
        };
        return p;
    }

    /// Join label values into a single NUL-separated key, writing to
    /// the provided buffer. Returns the used slice. Caller must
    /// provide at least `sum(len+1)` bytes; in practice labels are
    /// short (<64 bytes each) so a 256-byte stack buffer covers the
    /// common case with zero allocation.
    fn joinLabelsInto(values: []const []const u8, buf: []u8) ![]u8 {
        var pos: usize = 0;
        for (values) |v| {
            if (pos + v.len + 1 > buf.len) return error.BufferTooSmall;
            @memcpy(buf[pos .. pos + v.len], v);
            pos += v.len;
            buf[pos] = 0;
            pos += 1;
        }
        return buf[0..pos];
    }

    fn inc(self: *DynCounterVec, label_values: []const []const u8, amount: u64) !void {
        if (label_values.len != self.label_names.len) return error.LabelMismatch;

        // Fast path: build the lookup key in a stack-local buffer.
        // Zero allocation on the inc path when the slot already
        // exists in the map (the common case by far).
        var stack_buf: [256]u8 = undefined;
        const key = try joinLabelsInto(label_values, &stack_buf);

        // Shared-lock read path — stack_buf is stable for the
        // duration of the lookup so the slice is valid.
        {
            self.lock.lockShared();
            defer self.lock.unlockShared();
            if (self.values.get(key)) |counter| {
                counter.inc(amount);
                return;
            }
        }

        // Slot doesn't exist — allocate a persistent copy of the key
        // for the map, create a new counter, upgrade to exclusive
        // lock, and insert. This path runs once per distinct label
        // combo — rare after warmup.
        const persistent_key = try _alloc.dupe(u8, key);
        errdefer _alloc.free(persistent_key);
        const new_counter = try DynCounter.init();
        new_counter.inc(amount);
        errdefer _alloc.destroy(new_counter);

        self.lock.lock();
        defer self.lock.unlock();
        const gop = try self.values.getOrPut(_alloc, persistent_key);
        if (gop.found_existing) {
            _alloc.free(persistent_key);
            _alloc.destroy(new_counter);
            gop.value_ptr.*.inc(amount);
            return;
        }
        gop.value_ptr.* = new_counter;
    }
};

// ── DynHistogramVec ─────────────────────────────────────────────────────────

const DynHistogramVec = struct {
    label_names: [][]const u8, // owned
    upper_bounds: []const f64, // owned
    values: std.StringHashMapUnmanaged(*DynHistogram),
    lock: py.RwLock,

    fn init(label_names: []const []const u8, buckets: []const f64) !*DynHistogramVec {
        const p = try _alloc.create(DynHistogramVec);
        const owned_names = try _alloc.alloc([]const u8, label_names.len);
        for (label_names, 0..) |n, i| {
            owned_names[i] = try _alloc.dupe(u8, n);
        }
        const owned_bounds = try _alloc.alloc(f64, buckets.len);
        @memcpy(owned_bounds, buckets);
        p.* = .{
            .label_names = owned_names,
            .upper_bounds = owned_bounds,
            .values = .{},
            .lock = .{},
        };
        return p;
    }

    fn observe(self: *DynHistogramVec, label_values: []const []const u8, value: f64) !void {
        if (label_values.len != self.label_names.len) return error.LabelMismatch;

        // Stack-buffer lookup (same fast path as DynCounterVec.inc).
        var stack_buf: [256]u8 = undefined;
        const key = try DynCounterVec.joinLabelsInto(label_values, &stack_buf);

        {
            self.lock.lockShared();
            defer self.lock.unlockShared();
            if (self.values.get(key)) |hist| {
                hist.observe(value);
                return;
            }
        }

        const persistent_key = try _alloc.dupe(u8, key);
        errdefer _alloc.free(persistent_key);
        const new_hist = try DynHistogram.init(self.upper_bounds);
        new_hist.observe(value);

        self.lock.lock();
        defer self.lock.unlock();
        const gop = try self.values.getOrPut(_alloc, persistent_key);
        if (gop.found_existing) {
            _alloc.free(persistent_key);
            _alloc.free(new_hist.upper_bounds);
            _alloc.free(new_hist.bucket_counts);
            _alloc.destroy(new_hist);
            gop.value_ptr.*.observe(value);
            return;
        }
        gop.value_ptr.* = new_hist;
    }
};

// ── Registry helpers ────────────────────────────────────────────────────────

pub fn registerEntry(entry: *MetricEntry) !u32 {
    _registry_mutex.lock();
    defer _registry_mutex.unlock();
    const idx = _registry_count.load(.monotonic);
    if (idx >= MAX_METRICS) return error.RegistryFull;
    // Write the slot first, then publish the incremented count with a
    // release store — a reader that observes the new count (acquire) is
    // guaranteed to see this fully-written slot.
    _registry[idx].store(entry, .release);
    _registry_count.store(idx + 1, .release);
    return @intCast(idx);
}

fn getEntry(handle: u32) ?*MetricEntry {
    // Lock-free: load the published count (acquire), bounds-check the
    // handle against it, then load the pointer-stable slot (acquire).
    // The slot array never reallocates and entries never free, so this
    // is race-free without any lock — the hot path for every inc/observe.
    const count = _registry_count.load(.acquire);
    if (handle >= count) return null;
    return _registry[handle].load(.acquire);
}

// ── Python FFI — Counter ────────────────────────────────────────────────────

/// _metric_counter_register(name: str, help: str) -> int (handle)
pub fn py_metric_counter_register(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    var help_ptr: [*c]const u8 = undefined;
    var help_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "s#s#", &name_ptr, &name_len, &help_ptr, &help_len) == 0) {
        return null;
    }
    const name_slice = name_ptr[0..@as(usize, @intCast(name_len))];
    const help_slice = help_ptr[0..@as(usize, @intCast(help_len))];

    const counter = DynCounter.init() catch {
        py.setError("metric_counter_register: out of memory", .{});
        return null;
    };
    const entry = _alloc.create(MetricEntry) catch {
        py.setError("metric_counter_register: out of memory", .{});
        return null;
    };
    entry.* = .{
        .kind = .counter,
        .name = _alloc.dupe(u8, name_slice) catch {
            py.setError("metric_counter_register: out of memory", .{});
            return null;
        },
        .help = _alloc.dupe(u8, help_slice) catch {
            py.setError("metric_counter_register: out of memory", .{});
            return null;
        },
        .impl = counter,
    };
    const handle = registerEntry(entry) catch {
        py.setError("metric_counter_register: registry append failed", .{});
        return null;
    };
    return c.PyLong_FromUnsignedLong(handle);
}

/// _metric_counter_inc(handle: int, amount: int) -> None
pub fn py_metric_counter_inc(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    var amount: c_ulonglong = undefined;
    if (c.PyArg_ParseTuple(args, "IK", &handle, &amount) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("metric_counter_inc: invalid handle {d}", .{handle});
        return null;
    };
    if (entry.kind != .counter) {
        py.setError("metric_counter_inc: handle {d} is not a counter", .{handle});
        return null;
    }
    const counter: *DynCounter = @ptrCast(@alignCast(entry.impl));
    counter.inc(@intCast(amount));
    return py.pyNone();
}

/// _metric_counter_read(handle: int) -> int  (test helper)
pub fn py_metric_counter_read(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    if (c.PyArg_ParseTuple(args, "I", &handle) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("metric_counter_read: invalid handle", .{});
        return null;
    };
    if (entry.kind != .counter) {
        py.setError("metric_counter_read: wrong kind", .{});
        return null;
    }
    const counter: *const DynCounter = @ptrCast(@alignCast(entry.impl));
    return c.PyLong_FromUnsignedLongLong(counter.read());
}

// ── Python FFI — Gauge ──────────────────────────────────────────────────────

/// _metric_gauge_register(name: str, help: str) -> int (handle)
pub fn py_metric_gauge_register(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    var help_ptr: [*c]const u8 = undefined;
    var help_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "s#s#", &name_ptr, &name_len, &help_ptr, &help_len) == 0) {
        return null;
    }
    const gauge = DynGauge.init() catch {
        py.setError("gauge_register: oom", .{});
        return null;
    };
    const entry = _alloc.create(MetricEntry) catch {
        py.setError("gauge_register: oom", .{});
        return null;
    };
    entry.* = .{
        .kind = .gauge,
        .name = _alloc.dupe(u8, name_ptr[0..@as(usize, @intCast(name_len))]) catch {
            py.setError("gauge_register: oom", .{});
            return null;
        },
        .help = _alloc.dupe(u8, help_ptr[0..@as(usize, @intCast(help_len))]) catch {
            py.setError("gauge_register: oom", .{});
            return null;
        },
        .impl = gauge,
    };
    const handle = registerEntry(entry) catch {
        py.setError("gauge_register: registry append failed", .{});
        return null;
    };
    return c.PyLong_FromUnsignedLong(handle);
}

/// _metric_gauge_set(handle: int, value: int) -> None
pub fn py_metric_gauge_set(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    var value: c_longlong = undefined;
    if (c.PyArg_ParseTuple(args, "IL", &handle, &value) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("gauge_set: invalid handle", .{});
        return null;
    };
    if (entry.kind != .gauge) {
        py.setError("gauge_set: wrong kind", .{});
        return null;
    }
    const gauge: *DynGauge = @ptrCast(@alignCast(entry.impl));
    gauge.set(@intCast(value));
    return py.pyNone();
}

/// _metric_gauge_add(handle: int, delta: int) -> None
pub fn py_metric_gauge_add(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    var delta: c_longlong = undefined;
    if (c.PyArg_ParseTuple(args, "IL", &handle, &delta) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("gauge_add: invalid handle", .{});
        return null;
    };
    if (entry.kind != .gauge) {
        py.setError("gauge_add: wrong kind", .{});
        return null;
    }
    const gauge: *DynGauge = @ptrCast(@alignCast(entry.impl));
    gauge.add(@intCast(delta));
    return py.pyNone();
}

/// _metric_gauge_read(handle: int) -> int  (test helper)
pub fn py_metric_gauge_read(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    if (c.PyArg_ParseTuple(args, "I", &handle) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("gauge_read: invalid handle", .{});
        return null;
    };
    if (entry.kind != .gauge) {
        py.setError("gauge_read: wrong kind", .{});
        return null;
    }
    const gauge: *const DynGauge = @ptrCast(@alignCast(entry.impl));
    return c.PyLong_FromLongLong(gauge.read());
}

// ── Python FFI — Histogram ──────────────────────────────────────────────────

/// _metric_histogram_register(name: str, help: str, buckets: tuple[float, ...]) -> int
pub fn py_metric_histogram_register(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    var help_ptr: [*c]const u8 = undefined;
    var help_len: c.Py_ssize_t = undefined;
    var buckets_obj: ?*c.PyObject = undefined;
    if (c.PyArg_ParseTuple(args, "s#s#O", &name_ptr, &name_len, &help_ptr, &help_len, &buckets_obj) == 0) {
        return null;
    }
    const bseq = buckets_obj orelse return null;
    const blen = c.PySequence_Size(bseq);
    if (blen < 0) {
        py.setError("histogram_register: buckets must be a sequence", .{});
        return null;
    }
    var buckets_buf: [64]f64 = undefined;
    if (blen > 64) {
        py.setError("histogram_register: at most 64 buckets supported", .{});
        return null;
    }
    const buckets = buckets_buf[0..@as(usize, @intCast(blen))];
    var i: c.Py_ssize_t = 0;
    while (i < blen) : (i += 1) {
        const item = c.PySequence_GetItem(bseq, i) orelse return null;
        defer c.Py_DecRef(item);
        const f = c.PyFloat_AsDouble(item);
        if (f == -1.0 and c.PyErr_Occurred() != null) return null;
        buckets[@intCast(i)] = f;
    }

    const hist = DynHistogram.init(buckets) catch {
        py.setError("histogram_register: oom", .{});
        return null;
    };
    const entry = _alloc.create(MetricEntry) catch {
        py.setError("histogram_register: oom", .{});
        return null;
    };
    entry.* = .{
        .kind = .histogram,
        .name = _alloc.dupe(u8, name_ptr[0..@as(usize, @intCast(name_len))]) catch {
            py.setError("oom", .{});
            return null;
        },
        .help = _alloc.dupe(u8, help_ptr[0..@as(usize, @intCast(help_len))]) catch {
            py.setError("oom", .{});
            return null;
        },
        .impl = hist,
    };
    const handle = registerEntry(entry) catch {
        py.setError("histogram_register: registry append failed", .{});
        return null;
    };
    return c.PyLong_FromUnsignedLong(handle);
}

/// _metric_histogram_observe(handle: int, value: float) -> None
pub fn py_metric_histogram_observe(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    var value: f64 = undefined;
    if (c.PyArg_ParseTuple(args, "Id", &handle, &value) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("histogram_observe: invalid handle", .{});
        return null;
    };
    if (entry.kind != .histogram) {
        py.setError("histogram_observe: wrong kind", .{});
        return null;
    }
    const hist: *DynHistogram = @ptrCast(@alignCast(entry.impl));
    hist.observe(value);
    return py.pyNone();
}

// ── Python FFI — CounterVec ─────────────────────────────────────────────────

fn parseLabelList(obj: *c.PyObject, buf: []?[]const u8) ![]const []const u8 {
    // Require a list or tuple and use BORROWED item references (PyList/PyTuple
    // GetItem). The returned UTF-8 slices point into each item's internal buffer
    // and stay valid only while the item is alive — the container keeps them
    // alive for the duration of this call. A generic PySequence whose __getitem__
    // returns a fresh str per call (which we'd then Py_DecRef) would leave those
    // slices dangling, so we deliberately do not accept arbitrary sequences.
    const is_list = c.PyList_Check(obj) != 0;
    const is_tuple = c.PyTuple_Check(obj) != 0;
    if (!is_list and !is_tuple) return error.NotASequence;
    const len = if (is_list) c.PyList_Size(obj) else c.PyTuple_Size(obj);
    if (len < 0) return error.NotASequence;
    if (len > buf.len) return error.TooManyLabels;
    var i: c.Py_ssize_t = 0;
    while (i < len) : (i += 1) {
        const item = (if (is_list) c.PyList_GetItem(obj, i) else c.PyTuple_GetItem(obj, i)) orelse return error.ItemGetFailed;
        if (c.PyUnicode_Check(item) == 0) return error.NotAString;
        var item_len: c.Py_ssize_t = undefined;
        const s = c.PyUnicode_AsUTF8AndSize(item, &item_len) orelse return error.Utf8Failed;
        buf[@intCast(i)] = s[0..@as(usize, @intCast(item_len))];
    }
    // Build a contiguous slice from the populated prefix of the buffer.
    // The caller provides `buf` large enough and we write only the
    // first `len` entries. Cast back to non-optional slice.
    var out_buf: [16][]const u8 = undefined;
    for (0..@as(usize, @intCast(len))) |k| {
        out_buf[k] = buf[k].?;
    }
    _ = &out_buf;
    // NOTE: the real ownership stays in the PyUnicode buffer. These
    // slices are valid for the duration of this call — the caller
    // must copy them into a hash key before returning.
    // Since Zig doesn't let us return a slice into a stack-local
    // `out_buf`, we reuse the optional slice buffer by casting.
    const result_ptr: [*]const []const u8 = @ptrCast(buf.ptr);
    return result_ptr[0..@as(usize, @intCast(len))];
}

/// _metric_counter_vec_register(name: str, help: str, label_names: list[str]) -> int
pub fn py_metric_counter_vec_register(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    var help_ptr: [*c]const u8 = undefined;
    var help_len: c.Py_ssize_t = undefined;
    var label_obj: ?*c.PyObject = undefined;
    if (c.PyArg_ParseTuple(args, "s#s#O", &name_ptr, &name_len, &help_ptr, &help_len, &label_obj) == 0) {
        return null;
    }
    const lseq = label_obj orelse return null;
    var label_buf: [16]?[]const u8 = undefined;
    const labels = parseLabelList(lseq, &label_buf) catch {
        py.setError("counter_vec_register: label_names must be list[str] (max 16)", .{});
        return null;
    };

    const vec = DynCounterVec.init(labels) catch {
        py.setError("counter_vec_register: oom", .{});
        return null;
    };
    const entry = _alloc.create(MetricEntry) catch {
        py.setError("oom", .{});
        return null;
    };
    entry.* = .{
        .kind = .counter_vec,
        .name = _alloc.dupe(u8, name_ptr[0..@as(usize, @intCast(name_len))]) catch {
            py.setError("oom", .{});
            return null;
        },
        .help = _alloc.dupe(u8, help_ptr[0..@as(usize, @intCast(help_len))]) catch {
            py.setError("oom", .{});
            return null;
        },
        .impl = vec,
    };
    const handle = registerEntry(entry) catch {
        py.setError("counter_vec_register: registry append failed", .{});
        return null;
    };
    return c.PyLong_FromUnsignedLong(handle);
}

/// _metric_counter_vec_inc(handle: int, label_values: list[str], amount: int) -> None
pub fn py_metric_counter_vec_inc(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    var label_obj: ?*c.PyObject = undefined;
    var amount: c_ulonglong = undefined;
    if (c.PyArg_ParseTuple(args, "IOK", &handle, &label_obj, &amount) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("counter_vec_inc: invalid handle", .{});
        return null;
    };
    if (entry.kind != .counter_vec) {
        py.setError("counter_vec_inc: wrong kind", .{});
        return null;
    }
    const lseq = label_obj orelse return null;
    var label_buf: [16]?[]const u8 = undefined;
    const label_values = parseLabelList(lseq, &label_buf) catch {
        py.setError("counter_vec_inc: label_values must be list[str]", .{});
        return null;
    };
    const vec: *DynCounterVec = @ptrCast(@alignCast(entry.impl));
    vec.inc(label_values, @intCast(amount)) catch {
        py.setError("counter_vec_inc: inc failed (label count mismatch?)", .{});
        return null;
    };
    return py.pyNone();
}

// ── Python FFI — HistogramVec ───────────────────────────────────────────────

/// _metric_histogram_vec_register(name, help, label_names, buckets) -> int
pub fn py_metric_histogram_vec_register(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    var help_ptr: [*c]const u8 = undefined;
    var help_len: c.Py_ssize_t = undefined;
    var label_obj: ?*c.PyObject = undefined;
    var buckets_obj: ?*c.PyObject = undefined;
    if (c.PyArg_ParseTuple(args, "s#s#OO", &name_ptr, &name_len, &help_ptr, &help_len, &label_obj, &buckets_obj) == 0) {
        return null;
    }
    const lseq = label_obj orelse return null;
    var label_buf: [16]?[]const u8 = undefined;
    const labels = parseLabelList(lseq, &label_buf) catch {
        py.setError("histogram_vec_register: label_names must be list[str]", .{});
        return null;
    };

    const bseq = buckets_obj orelse return null;
    const blen = c.PySequence_Size(bseq);
    if (blen < 0 or blen > 64) {
        py.setError("histogram_vec_register: buckets must be a sequence (max 64)", .{});
        return null;
    }
    var buckets_buf: [64]f64 = undefined;
    const buckets = buckets_buf[0..@as(usize, @intCast(blen))];
    var i: c.Py_ssize_t = 0;
    while (i < blen) : (i += 1) {
        const item = c.PySequence_GetItem(bseq, i) orelse return null;
        defer c.Py_DecRef(item);
        const f = c.PyFloat_AsDouble(item);
        // Surface a non-float bucket as an error instead of leaving the pending
        // exception to corrupt a later C-API call (mirrors the non-vec path).
        if (f == -1.0 and c.PyErr_Occurred() != null) return null;
        buckets[@intCast(i)] = f;
    }

    const vec = DynHistogramVec.init(labels, buckets) catch {
        py.setError("histogram_vec_register: oom", .{});
        return null;
    };
    const entry = _alloc.create(MetricEntry) catch {
        py.setError("oom", .{});
        return null;
    };
    entry.* = .{
        .kind = .histogram_vec,
        .name = _alloc.dupe(u8, name_ptr[0..@as(usize, @intCast(name_len))]) catch {
            py.setError("oom", .{});
            return null;
        },
        .help = _alloc.dupe(u8, help_ptr[0..@as(usize, @intCast(help_len))]) catch {
            py.setError("oom", .{});
            return null;
        },
        .impl = vec,
    };
    const handle = registerEntry(entry) catch {
        py.setError("histogram_vec_register: registry append failed", .{});
        return null;
    };
    return c.PyLong_FromUnsignedLong(handle);
}

/// _metric_histogram_vec_observe(handle, label_values, value) -> None
pub fn py_metric_histogram_vec_observe(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_uint = undefined;
    var label_obj: ?*c.PyObject = undefined;
    var value: f64 = undefined;
    if (c.PyArg_ParseTuple(args, "IOd", &handle, &label_obj, &value) == 0) return null;
    const entry = getEntry(@intCast(handle)) orelse {
        py.setError("histogram_vec_observe: invalid handle", .{});
        return null;
    };
    if (entry.kind != .histogram_vec) {
        py.setError("histogram_vec_observe: wrong kind", .{});
        return null;
    }
    const lseq = label_obj orelse return null;
    var label_buf: [16]?[]const u8 = undefined;
    const label_values = parseLabelList(lseq, &label_buf) catch {
        py.setError("histogram_vec_observe: label_values must be list[str]", .{});
        return null;
    };
    const vec: *DynHistogramVec = @ptrCast(@alignCast(entry.impl));
    vec.observe(label_values, value) catch {
        py.setError("histogram_vec_observe: observe failed", .{});
        return null;
    };
    return py.pyNone();
}

// ── Python FFI — Prometheus text export ─────────────────────────────────────

/// _metric_registry_write_prometheus() -> bytes
///
/// Streams all registered metrics into a single Prometheus text
/// exposition blob. Called once per scrape — typical rate is 1 per
/// 15 seconds — so this path can allocate freely.
pub fn py_metric_registry_write_prometheus(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var buf: std.ArrayListUnmanaged(u8) = .empty;
    defer buf.deinit(_alloc);

    // Snapshot the published count (acquire) and iterate the pointer-stable
    // slots lock-free. A registration racing this scrape simply lands beyond
    // `count` and is picked up by the next scrape — never a torn read.
    const count = _registry_count.load(.acquire);
    var i: u32 = 0;
    while (i < count) : (i += 1) {
        const entry = _registry[i].load(.acquire) orelse continue;
        writeEntry(&buf, entry) catch |err| {
            // Surface the real Zig error name so callers can
            // distinguish genuine OOM from formatting/encoding bugs.
            py.setError(
                "write_prometheus: failed on metric '{s}': {s}",
                .{ entry.name, @errorName(err) },
            );
            return null;
        };
    }

    return c.PyBytes_FromStringAndSize(
        @ptrCast(buf.items.ptr),
        @intCast(buf.items.len),
    );
}

fn writeEntry(buf: *std.ArrayListUnmanaged(u8), entry: *MetricEntry) !void {
    // HELP line (if help provided)
    if (entry.help.len > 0) {
        try buf.appendSlice(_alloc, "# HELP ");
        try buf.appendSlice(_alloc, entry.name);
        try buf.append(_alloc, ' ');
        try buf.appendSlice(_alloc, entry.help);
        try buf.append(_alloc, '\n');
    }
    // TYPE line
    try buf.appendSlice(_alloc, "# TYPE ");
    try buf.appendSlice(_alloc, entry.name);
    try buf.append(_alloc, ' ');
    switch (entry.kind) {
        .counter, .counter_vec => try buf.appendSlice(_alloc, "counter\n"),
        .gauge => try buf.appendSlice(_alloc, "gauge\n"),
        .histogram, .histogram_vec => try buf.appendSlice(_alloc, "histogram\n"),
    }

    switch (entry.kind) {
        .counter => {
            const cnt: *const DynCounter = @ptrCast(@alignCast(entry.impl));
            try buf.appendSlice(_alloc, entry.name);
            try buf.append(_alloc, ' ');
            try formatUint(buf, cnt.read());
            try buf.append(_alloc, '\n');
        },
        .gauge => {
            const g: *const DynGauge = @ptrCast(@alignCast(entry.impl));
            try buf.appendSlice(_alloc, entry.name);
            try buf.append(_alloc, ' ');
            try formatInt(buf, g.read());
            try buf.append(_alloc, '\n');
        },
        .histogram => {
            const h: *const DynHistogram = @ptrCast(@alignCast(entry.impl));
            try writeHistogram(buf, entry.name, "", h);
        },
        .counter_vec => {
            const vec: *DynCounterVec = @ptrCast(@alignCast(entry.impl));
            vec.lock.lockShared();
            defer vec.lock.unlockShared();
            var it = vec.values.iterator();
            while (it.next()) |kv| {
                try buf.appendSlice(_alloc, entry.name);
                try writeLabelSuffix(buf, vec.label_names, kv.key_ptr.*);
                try buf.append(_alloc, ' ');
                try formatUint(buf, kv.value_ptr.*.read());
                try buf.append(_alloc, '\n');
            }
        },
        .histogram_vec => {
            const vec: *DynHistogramVec = @ptrCast(@alignCast(entry.impl));
            vec.lock.lockShared();
            defer vec.lock.unlockShared();
            var it = vec.values.iterator();
            while (it.next()) |kv| {
                var label_suffix_buf: std.ArrayListUnmanaged(u8) = .empty;
                defer label_suffix_buf.deinit(_alloc);
                try writeLabelSuffix(&label_suffix_buf, vec.label_names, kv.key_ptr.*);
                try writeHistogram(buf, entry.name, label_suffix_buf.items, kv.value_ptr.*);
            }
        },
    }
}

fn writeHistogram(buf: *std.ArrayListUnmanaged(u8), name: []const u8, label_suffix: []const u8, h: *const DynHistogram) !void {
    var cumulative: u64 = 0;
    var bucket_counts_buf: [64]u64 = undefined;
    const counts = bucket_counts_buf[0..h.upper_bounds.len];
    h.readBuckets(counts);
    for (h.upper_bounds, 0..) |upper, idx| {
        cumulative += counts[idx];
        try buf.appendSlice(_alloc, name);
        try buf.appendSlice(_alloc, "_bucket{le=\"");
        try formatFloat(buf, upper);
        try buf.append(_alloc, '"');
        if (label_suffix.len > 0) {
            try buf.append(_alloc, ',');
            try buf.appendSlice(_alloc, label_suffix[1 .. label_suffix.len - 1]);
        }
        try buf.appendSlice(_alloc, "} ");
        try formatUint(buf, cumulative);
        try buf.append(_alloc, '\n');
    }
    // +Inf bucket
    const total = h.readCount();
    try buf.appendSlice(_alloc, name);
    try buf.appendSlice(_alloc, "_bucket{le=\"+Inf\"");
    if (label_suffix.len > 0) {
        try buf.append(_alloc, ',');
        try buf.appendSlice(_alloc, label_suffix[1 .. label_suffix.len - 1]);
    }
    try buf.appendSlice(_alloc, "} ");
    try formatUint(buf, total);
    try buf.append(_alloc, '\n');
    // sum
    try buf.appendSlice(_alloc, name);
    try buf.appendSlice(_alloc, "_sum");
    try buf.appendSlice(_alloc, label_suffix);
    try buf.append(_alloc, ' ');
    try formatFloat(buf, h.readSum());
    try buf.append(_alloc, '\n');
    // count
    try buf.appendSlice(_alloc, name);
    try buf.appendSlice(_alloc, "_count");
    try buf.appendSlice(_alloc, label_suffix);
    try buf.append(_alloc, ' ');
    try formatUint(buf, total);
    try buf.append(_alloc, '\n');
}

fn writeLabelSuffix(buf: *std.ArrayListUnmanaged(u8), label_names: []const []const u8, joined_key: []const u8) !void {
    try buf.append(_alloc, '{');
    // Split joined_key on NUL bytes to recover individual label values
    var start: usize = 0;
    var label_idx: usize = 0;
    for (joined_key, 0..) |b, i| {
        if (b == 0) {
            if (label_idx > 0) try buf.append(_alloc, ',');
            try buf.appendSlice(_alloc, label_names[label_idx]);
            try buf.appendSlice(_alloc, "=\"");
            try buf.appendSlice(_alloc, joined_key[start..i]);
            try buf.append(_alloc, '"');
            label_idx += 1;
            start = i + 1;
        }
    }
    try buf.append(_alloc, '}');
}

fn formatUint(buf: *std.ArrayListUnmanaged(u8), n: u64) !void {
    var tmp: [32]u8 = undefined;
    const s = try std.fmt.bufPrint(&tmp, "{d}", .{n});
    try buf.appendSlice(_alloc, s);
}

fn formatInt(buf: *std.ArrayListUnmanaged(u8), n: i64) !void {
    var tmp: [32]u8 = undefined;
    const s = try std.fmt.bufPrint(&tmp, "{d}", .{n});
    try buf.appendSlice(_alloc, s);
}

fn formatFloat(buf: *std.ArrayListUnmanaged(u8), f: f64) !void {
    // Use explicit precision (6 decimal places) — this matches the
    // Prometheus text exposition convention and gives deterministic
    // bounded output. The bare `{d}` specifier on f64 in Zig 0.15
    // dumps FULL precision which can produce hundreds of chars for
    // subnormal or full-mantissa values, blowing any fixed buffer.
    //
    // After bufPrint we strip trailing zeros + a dangling decimal
    // point so `0.100000` renders as `0.1` and `1.000000` as `1` —
    // the canonical Prometheus convention for bucket upper bounds.
    var tmp: [64]u8 = undefined;
    const raw = std.fmt.bufPrint(&tmp, "{d:.6}", .{f}) catch |err| {
        trace("formatFloat failed: bits=0x{x} err={s}", .{ @as(u64, @bitCast(f)), @errorName(err) });
        return err;
    };
    const trimmed = trimFloatString(raw);
    try buf.appendSlice(_alloc, trimmed);
}

/// Trim trailing zeros after a decimal point, plus a dangling `.`.
/// Examples: "0.100000" → "0.1", "1.000000" → "1", "0" → "0".
/// Doesn't touch strings without a decimal point.
fn trimFloatString(s: []const u8) []const u8 {
    const dot = std.mem.indexOfScalar(u8, s, '.') orelse return s;
    var end: usize = s.len;
    while (end > dot + 1 and s[end - 1] == '0') : (end -= 1) {}
    if (end == dot + 1) end = dot; // strip trailing '.'
    return s[0..end];
}

// ── Python FFI — test helpers ───────────────────────────────────────────────

/// _metric_registry_reset() -> None  (test helper)
///
/// WARNING: leaks all existing metric storage. Only safe to call at
/// the very end of a test run where the process will exit soon. We
/// cannot safely reclaim metric memory while handles are held by
/// Python — breaking the index-based read invariant is not worth the
/// complexity for a test helper.
pub fn py_metric_registry_reset(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    // Publishing count=0 hides all existing slots (their MetricEntry storage
    // is intentionally leaked, as documented). Take the registration mutex so
    // this can't interleave with a concurrent registerEntry.
    _registry_mutex.lock();
    defer _registry_mutex.unlock();
    _registry_count.store(0, .release);
    return py.pyNone();
}

/// _metric_registry_size() -> int  (test helper)
pub fn py_metric_registry_size(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return c.PyLong_FromUnsignedLong(@intCast(_registry_count.load(.acquire)));
}

// ═══════════════════════════════════════════════════════════════════════════
// Span ring FFI — Phase 3 (task #226)
// ═══════════════════════════════════════════════════════════════════════════

const span_ring = @import("metrics/span_ring.zig");

/// _span_start(trace_id_high, trace_id_low, parent_id, name, sampled) -> int
///
/// Returns an opaque u64 handle. Pass it back to `_span_set_attr_*`,
/// `_span_set_status`, and `_span_end`. Handle 0 is the sentinel for
/// unsampled or dropped spans — all subsequent ops on it no-op.
pub fn py_span_start(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var trace_high: c_ulonglong = undefined;
    var trace_low: c_ulonglong = undefined;
    var parent_id: c_ulonglong = undefined;
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    var sampled_int: c_int = undefined;
    if (c.PyArg_ParseTuple(
        args,
        "KKKs#p",
        &trace_high,
        &trace_low,
        &parent_id,
        &name_ptr,
        &name_len,
        &sampled_int,
    ) == 0) return null;
    const name_slice = name_ptr[0..@as(usize, @intCast(name_len))];
    const handle = span_ring.start(
        @intCast(trace_high),
        @intCast(trace_low),
        @intCast(parent_id),
        name_slice,
        sampled_int != 0,
    );
    return c.PyLong_FromUnsignedLongLong(handle);
}

/// _span_set_attr_str(handle, key, value) -> None
pub fn py_span_set_attr_str(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_ulonglong = undefined;
    var key_ptr: [*c]const u8 = undefined;
    var key_len: c.Py_ssize_t = undefined;
    var val_ptr: [*c]const u8 = undefined;
    var val_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "Ks#s#", &handle, &key_ptr, &key_len, &val_ptr, &val_len) == 0) {
        return null;
    }
    span_ring.setAttr(
        @intCast(handle),
        key_ptr[0..@as(usize, @intCast(key_len))],
        val_ptr[0..@as(usize, @intCast(val_len))],
    );
    return py.pyNone();
}

/// _span_set_attr_int(handle, key, value) -> None  (value formatted as decimal)
pub fn py_span_set_attr_int(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_ulonglong = undefined;
    var key_ptr: [*c]const u8 = undefined;
    var key_len: c.Py_ssize_t = undefined;
    var value: c_longlong = undefined;
    if (c.PyArg_ParseTuple(args, "Ks#L", &handle, &key_ptr, &key_len, &value) == 0) return null;
    // Format value into a local buffer then push through the same
    // byte-level setAttr path. 24 bytes covers any int64.
    var tmp: [24]u8 = undefined;
    const s = std.fmt.bufPrint(&tmp, "{d}", .{value}) catch return py.pyNone();
    span_ring.setAttr(
        @intCast(handle),
        key_ptr[0..@as(usize, @intCast(key_len))],
        s,
    );
    return py.pyNone();
}

/// _span_set_attr_float(handle, key, value) -> None
pub fn py_span_set_attr_float(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_ulonglong = undefined;
    var key_ptr: [*c]const u8 = undefined;
    var key_len: c.Py_ssize_t = undefined;
    var value: f64 = undefined;
    if (c.PyArg_ParseTuple(args, "Ks#d", &handle, &key_ptr, &key_len, &value) == 0) return null;
    var tmp: [64]u8 = undefined;
    const raw = std.fmt.bufPrint(&tmp, "{d:.6}", .{value}) catch return py.pyNone();
    const trimmed = trimFloatString(raw);
    span_ring.setAttr(
        @intCast(handle),
        key_ptr[0..@as(usize, @intCast(key_len))],
        trimmed,
    );
    return py.pyNone();
}

/// _span_set_status(handle, code) -> None
pub fn py_span_set_status(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_ulonglong = undefined;
    var code: c_int = undefined;
    if (c.PyArg_ParseTuple(args, "Ki", &handle, &code) == 0) return null;
    span_ring.setStatus(@intCast(handle), @intCast(@max(0, @min(code, 255))));
    return py.pyNone();
}

/// _span_end(handle) -> None
pub fn py_span_end(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_ulonglong = undefined;
    if (c.PyArg_ParseTuple(args, "K", &handle) == 0) return null;
    span_ring.end(@intCast(handle));
    return py.pyNone();
}

/// _span_add_event(handle, name) -> None
/// Adds a timestamped event to the recording span. The timestamp is
/// captured at call time (not when the event is drained). Events are
/// packed into the slot's 128-byte event arena; overflow is silent.
pub fn py_span_add_event(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_ulonglong = undefined;
    var name_ptr: [*c]const u8 = undefined;
    var name_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "Ks#", &handle, &name_ptr, &name_len) == 0) return null;
    const name = name_ptr[0..@as(usize, @intCast(name_len))];
    span_ring.addEvent(@intCast(handle), name);
    return py.pyNone();
}

/// Context struct threaded through drain()'s emit callback so we can
/// build a Python list from inside the Zig loop.
const DrainCtx = extern struct {
    py_list: ?*c.PyObject,
    failed: u8,
};

fn drainEmit(ctx_raw: *anyopaque, ds: *const span_ring.DrainedSpan) void {
    const ctx: *DrainCtx = @ptrCast(@alignCast(ctx_raw));
    if (ctx.failed != 0) return;
    const list = ctx.py_list orelse return;

    // Build a dict per span — OpenTelemetry JSON schema shape
    // (trace_id/span_id as hex strings, times in unix nanos,
    // attributes as a dict of string → string, status as a small
    // sub-dict). No protobuf.
    const span_dict = c.PyDict_New() orelse {
        ctx.failed = 1;
        return;
    };
    defer c.Py_DecRef(span_dict);

    // trace_id as 32-char hex
    var trace_hex_buf: [33]u8 = undefined;
    const trace_hex = std.fmt.bufPrintZ(
        &trace_hex_buf,
        "{x:0>16}{x:0>16}",
        .{ ds.trace_id_high, ds.trace_id_low },
    ) catch {
        ctx.failed = 1;
        return;
    };
    _ = dictSetStrSteal(span_dict, "trace_id", c.PyUnicode_FromStringAndSize(trace_hex.ptr, @intCast(trace_hex.len)));

    // span_id as 16-char hex
    var span_hex_buf: [17]u8 = undefined;
    const span_hex = std.fmt.bufPrintZ(&span_hex_buf, "{x:0>16}", .{ds.span_id}) catch {
        ctx.failed = 1;
        return;
    };
    _ = dictSetStrSteal(span_dict, "span_id", c.PyUnicode_FromStringAndSize(span_hex.ptr, @intCast(span_hex.len)));

    // parent_id — empty string for root spans
    if (ds.parent_id != 0) {
        var parent_hex_buf: [17]u8 = undefined;
        const parent_hex = std.fmt.bufPrintZ(&parent_hex_buf, "{x:0>16}", .{ds.parent_id}) catch {
            ctx.failed = 1;
            return;
        };
        _ = dictSetStrSteal(span_dict, "parent_id", c.PyUnicode_FromStringAndSize(parent_hex.ptr, @intCast(parent_hex.len)));
    } else {
        _ = dictSetStrSteal(span_dict, "parent_id", c.PyUnicode_FromStringAndSize("", 0));
    }

    // name
    _ = dictSetStrSteal(
        span_dict,
        "name",
        c.PyUnicode_FromStringAndSize(@ptrCast(ds.name.ptr), @intCast(ds.name.len)),
    );

    // times in unix nanos
    _ = dictSetStrSteal(span_dict, "start_time_unix_nano", c.PyLong_FromLongLong(ds.start_ns));
    _ = dictSetStrSteal(span_dict, "end_time_unix_nano", c.PyLong_FromLongLong(ds.end_ns));

    // status as {"code": int, "message": ""}
    const status_dict = c.PyDict_New() orelse {
        ctx.failed = 1;
        return;
    };
    _ = dictSetStrSteal(status_dict, "code", c.PyLong_FromLong(@intCast(ds.status_code)));
    _ = dictSetStrSteal(status_dict, "message", c.PyUnicode_FromStringAndSize("", 0));
    _ = dictSetStrSteal(span_dict, "status", status_dict);

    // attributes — unpack the packed KV buffer into a Python dict
    const attrs_dict = c.PyDict_New() orelse {
        ctx.failed = 1;
        return;
    };
    var off: usize = 0;
    while (off + 2 <= ds.attrs_raw.len) {
        const key_len: usize = ds.attrs_raw[off];
        const val_len: usize = ds.attrs_raw[off + 1];
        off += 2;
        if (off + key_len + val_len > ds.attrs_raw.len) break;
        const key = ds.attrs_raw[off .. off + key_len];
        off += key_len;
        const val = ds.attrs_raw[off .. off + val_len];
        off += val_len;
        const py_val = c.PyUnicode_FromStringAndSize(@ptrCast(val.ptr), @intCast(val.len)) orelse {
            // Invalid UTF-8 attribute value — clear the pending exception so the
            // rest of the drain (and the caller) isn't corrupted by it.
            c.PyErr_Clear();
            continue;
        };
        defer c.Py_DecRef(py_val);
        var key_z: [256]u8 = undefined;
        if (key_len >= key_z.len) continue;
        @memcpy(key_z[0..key_len], key);
        key_z[key_len] = 0;
        _ = c.PyDict_SetItemString(attrs_dict, @ptrCast(&key_z), py_val);
    }
    _ = dictSetStrSteal(span_dict, "attributes", attrs_dict);

    // events — unpack the packed event buffer into a Python list of dicts.
    // Each event is: [timestamp_ns i64 LE (8 bytes)][name_len u8 (1 byte)][name bytes]
    // The output format matches the OpenTelemetry JSON event schema:
    //   {"name": str, "time_unix_nano": int}
    if (ds.event_count > 0) {
        const events_list = c.PyList_New(0) orelse {
            ctx.failed = 1;
            return;
        };
        var ev_off: usize = 0;
        while (ev_off + 9 <= ds.events_raw.len) {
            // Read timestamp (i64, little-endian packed)
            const ts_bytes: *const [8]u8 = @ptrCast(ds.events_raw[ev_off .. ev_off + 8]);
            const ts: i64 = @bitCast(ts_bytes.*);
            ev_off += 8;
            const ev_name_len: usize = ds.events_raw[ev_off];
            ev_off += 1;
            if (ev_off + ev_name_len > ds.events_raw.len) break;
            const ev_name = ds.events_raw[ev_off .. ev_off + ev_name_len];
            ev_off += ev_name_len;

            const ev_dict = c.PyDict_New() orelse continue;
            _ = dictSetStrSteal(ev_dict, "name", c.PyUnicode_FromStringAndSize(@ptrCast(ev_name.ptr), @intCast(ev_name.len)));
            _ = dictSetStrSteal(ev_dict, "time_unix_nano", c.PyLong_FromLongLong(ts));
            // PyDict_New gave us the only reference (rc=1). PyList_Append takes
            // its own reference, so release ours afterwards — net: owned by list.
            _ = c.PyList_Append(events_list, ev_dict);
            c.Py_DecRef(ev_dict);
        }
        _ = dictSetStrSteal(span_dict, "events", events_list);
    }

    // Append span_dict to the output list. The create reference (rc=1) is
    // released by the `defer c.Py_DecRef(span_dict)` above; PyList_Append takes
    // its own reference on success, so we must NOT add an extra one here (doing
    // so leaked one reference per drained span).
    if (c.PyList_Append(list, span_dict) != 0) {
        ctx.failed = 1;
    }
}

/// PyDict_SetItemString that takes ownership (steals) the new reference
/// passed in, matching the pattern used across the drainEmit function.
/// Returns true on success.
fn dictSetStrSteal(dict: ?*c.PyObject, key: [*:0]const u8, value: ?*c.PyObject) bool {
    const v = value orelse return false;
    defer c.Py_DecRef(v);
    return c.PyDict_SetItemString(dict, key, v) == 0;
}

/// _span_drain() -> list[dict]
pub fn py_span_drain(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const list = c.PyList_New(0) orelse return null;
    var ctx = DrainCtx{ .py_list = list, .failed = 0 };
    _ = span_ring.drain(&ctx, drainEmit);
    if (ctx.failed != 0) {
        c.Py_DecRef(list);
        py.setError("span_drain: failed to build span records", .{});
        return null;
    }
    return list;
}

/// _span_dropped_count() -> int
pub fn py_span_dropped_count(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return c.PyLong_FromUnsignedLongLong(span_ring.droppedCount());
}

/// _span_configure(capacity: int) -> None
///
/// Set the span ring capacity BEFORE the first span is recorded. Must
/// be a power of 2 between MIN_RING_CAPACITY (256) and MAX_RING_CAPACITY
/// (16777216). Raises ValueError on bad input or RuntimeError if the
/// ring has already been initialized (no live reconfiguration — that
/// would dangle in-flight span handles).
///
/// Default capacity is 16384 slots × 256 bytes = 4 MB. Production apps
/// can tune via this FFI or via the higher-level
/// `HYPER_TELEMETRY_SPAN_RING_CAPACITY` setting.
pub fn py_span_configure(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var capacity_raw: c_ulonglong = undefined;
    if (c.PyArg_ParseTuple(args, "K", &capacity_raw) == 0) return null;
    const capacity: usize = @intCast(capacity_raw);
    span_ring.configure(capacity) catch |err| {
        switch (err) {
            error.AlreadyInitialized => {
                // State-machine error → RuntimeError (the ring exists,
                // we just can't change its size).
                py.setError("span ring already initialized; configure must be called before first span", .{});
                return null;
            },
            error.CapacityNotPowerOfTwo => {
                // Input-validation error → ValueError so callers can
                // `except ValueError:` idiomatically.
                py.setValueError("span ring capacity {d} must be a power of 2", .{capacity});
                return null;
            },
            error.CapacityOutOfRange => {
                py.setValueError(
                    "span ring capacity {d} out of range [{d}, {d}]",
                    .{ capacity, span_ring.MIN_RING_CAPACITY, span_ring.MAX_RING_CAPACITY },
                );
                return null;
            },
            error.OutOfMemory => {
                py.setError("span ring configure: out of memory", .{});
                return null;
            },
        }
    };
    return py.pyNone();
}

/// _span_capacity() -> int
///
/// Return the live ring capacity (after successful init) or the
/// configured/intended capacity (before init OR after failed init).
/// Use `_span_is_operational()` to disambiguate the two.
pub fn py_span_capacity(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return c.PyLong_FromUnsignedLongLong(@intCast(span_ring.capacity()));
}

/// _span_is_operational() -> bool
///
/// True if the ring has been allocated and is recording spans. False
/// before first use OR after a failed init (e.g. OOM on a huge
/// configured capacity). Producers fall back to dropping every span
/// when this is False.
pub fn py_span_is_operational(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    if (span_ring.isOperational()) {
        return py.pyTrue();
    }
    return py.pyFalse();
}

/// _span_reset_for_tests() -> None
pub fn py_span_reset_for_tests(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    span_ring.resetForTests();
    return py.pyNone();
}
