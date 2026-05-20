// Lock-free MPSC span ring buffer for native span recording.
//
// Design (see docs/TelemetryArchitecturePlan.md §6):
//
//   - Fixed-capacity ring of 16384 × 256-byte slots = 4 MB static RSS.
//   - Multiple producer threads claim slots via an atomic
//     round-robin `next_slot` counter + per-slot CAS on the state
//     byte. If the CAS fails (slot still in use from an earlier
//     wrap), the producer increments `dropped_count` and returns
//     the sentinel handle 0 — the span's Python wrapper then
//     becomes a no-op for the rest of its lifetime.
//   - A single consumer (the background drain thread in Python)
//     walks the ring on each drain interval, picks up slots whose
//     state is `complete`, extracts their data into OpenTelemetry
//     JSON shape, then CASes them back to `free` for reuse.
//   - Span handle encoding: `(slot_idx << 48) | (generation & 0x0000FFFFFFFFFFFF)`.
//     16 bits of slot index (65536 max), 48 bits of generation.
//     Every successful claim bumps a monotonic 64-bit counter; we
//     store the low 48 bits in the slot and in the returned
//     handle, so `set_attr` / `set_status` / `end` can cheaply
//     verify the caller isn't holding a stale reference to a
//     reused slot. Generation wraps after ~10^14 spans — multiple
//     centuries at 100k spans/sec.
//
// Hot path costs (target for Phase 3, verified in bench_span_primitives.py):
//
//   start  ≤ 600 ns  — atomic fetchAdd + CAS + slot write + monotonic timestamp
//   set_attr ≤ 200 ns — slot load + generation check + append to packed KV buffer
//   end    ≤ 400 ns  — slot load + generation check + state store + end_ns write
//
// Zero-cost when unsampled: `start(..., sampled=false)` returns
// handle 0 without claiming any slot. Subsequent set_attr/end calls
// on handle 0 short-circuit in ≤5 ns each.

const std = @import("std");

// ── Zig 0.16 compat (no py.zig in metrics module) ──
const Mutex = struct {
    inner: std.c.pthread_mutex_t = std.c.PTHREAD_MUTEX_INITIALIZER,
    pub fn lock(self: *Mutex) void {
        _ = std.c.pthread_mutex_lock(&self.inner);
    }
    pub fn unlock(self: *Mutex) void {
        _ = std.c.pthread_mutex_unlock(&self.inner);
    }
};

fn nanoTimestamp() i128 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
    return @as(i128, ts.sec) * std.time.ns_per_s + ts.nsec;
}

// ── Public constants ────────────────────────────────────────────────────────

/// Default ring capacity when no explicit `configure(...)` call is made.
/// Override at runtime via `_span_configure(capacity)` from Python or via
/// `HYPER_TELEMETRY_SPAN_RING_CAPACITY` in the application settings. Must
/// be a power of 2 so the slot-index wraparound stays a single AND
/// instruction.
///
/// 16384 slots × 256 bytes = 4 MB static RSS. Larger rings give more
/// headroom against burst traffic at the cost of memory; smaller rings
/// fit edge / embedded deployments. Production tuning guidance lives in
/// `docs/telemetry.md`.
pub const DEFAULT_RING_CAPACITY: usize = 16384;

/// Hard cap on configured capacity. 1 << 24 = 16M slots × 256 B = 4 GB —
/// well past anything sensible, prevents accidental OOM via a typo'd env
/// var. Same lower bound (256) prevents pathological micro-rings that
/// would drop spans constantly under any real load.
pub const MIN_RING_CAPACITY: usize = 256;
pub const MAX_RING_CAPACITY: usize = 1 << 24;

/// Max inline length for span `name`. Names longer than this are truncated.
/// Compile-time constant — changing it changes the SpanSlot layout, so
/// re-tuning requires a `hyper-build --release`. The 256-byte slot is
/// sized to hold (header + 64-byte name + 128-byte attrs) — see the
/// `comptime` size assertion below.
pub const NAME_MAX: usize = 64;

/// Bytes of packed KV attribute storage per slot. Overflow drops extra attrs.
/// Compile-time for the same reason as NAME_MAX.
pub const ATTRS_MAX: usize = 128;

/// Bytes of packed event storage per slot. Each event is:
///   [timestamp_ns i64 (8 bytes)][name_len u8 (1 byte)][name bytes (name_len)]
/// So one event with a 22-char name uses 31 bytes. A 128-byte arena holds
/// 4 events of typical size (22-char names) or up to 14 events with single-
/// char names. Overflow drops extra events silently (same discipline as attrs).
pub const EVENTS_MAX: usize = 128;

/// Sentinel handle returned for unsampled or dropped spans. All subsequent
/// operations on this handle are no-ops.
pub const SENTINEL_HANDLE: u64 = 0;

// ── Slot layout ─────────────────────────────────────────────────────────────

pub const SlotState = enum(u8) {
    free = 0,
    recording = 1,
    complete = 2,
};

pub const StatusCode = enum(u8) {
    unset = 0,
    ok = 1,
    error_ = 2,
};

/// 384-byte span slot. `extern struct` to guarantee a stable C layout so
/// `@sizeOf(SpanSlot)` is a comptime assertion the compiler can verify.
///
/// Layout (v0.15.2):
///   offset   0:  header         (64 bytes)
///   offset  64:  name           (64 bytes)
///   offset 128:  attrs          (128 bytes)
///   offset 256:  events         (128 bytes) — NEW in v0.15.2
///   total:                       384 bytes
///
/// The event arena follows the same discipline as attrs: packed binary
/// format, overflow drops silently, never throws on the request path.
/// Default ring uses 384 × 16384 = 6 MB (was 4 MB with 256-byte slots).
///
/// The state byte MUST be first in the trailing byte group so atomic CAS
/// on it is cheap (no field-offset math at runtime).
pub const SpanSlot = extern struct {
    // Header (64 bytes): 8-byte fields first, then the small fields.
    generation: u64, // low 48 bits of global counter — handle check
    trace_id_high: u64,
    trace_id_low: u64,
    span_id: u64, // opaque monotonic; used for trace export
    parent_id: u64,
    start_ns: i64,
    end_ns: i64,
    state: u8, // SlotState — FIRST in the trailing byte group
    status_code: u8, // StatusCode
    sampled: u8, // 0 or 1
    name_len: u8, // 0..NAME_MAX
    attrs_used: u16, // bytes of `attrs` populated
    events_used: u8, // bytes of `events` populated (v0.15.2)
    event_count: u8, // number of events stored (v0.15.2)
    name: [NAME_MAX]u8, // 64 — second cache line
    attrs: [ATTRS_MAX]u8, // 128 — packed: [key_len u8][val_len u8][key][val]
    events: [EVENTS_MAX]u8, // 128 — packed: [ts i64][name_len u8][name bytes]
};

comptime {
    // Enforce 384-byte slot layout at compile time. Changing this requires
    // a rebuild + capacity re-tuning (memory impact is proportional).
    if (@sizeOf(SpanSlot) != 384) {
        @compileError(std.fmt.comptimePrint(
            "SpanSlot must be 384 bytes, got {d}",
            .{@sizeOf(SpanSlot)},
        ));
    }
}

// ── Global ring state ───────────────────────────────────────────────────────
//
// `ring` is heap-allocated at first use so the capacity can be tuned at
// runtime via `configure(...)`. The slice + capacity-mask are stored as
// module globals; we never reallocate after init() returns, so producers
// can read `ring_slice` and `ring_mask` without atomic loads — they
// observe a single transition from null/zero to populated under the
// init double-check below.
//
// `ring_slice` is opaque-pointer-shaped (`[*]SpanSlot`) instead of a
// `[]SpanSlot` so the producer hot path doesn't pay for the slice
// length re-load on every `start()` call. The length lives in
// `ring_capacity` and the AND mask in `ring_mask`.

var ring_slice: ?[*]SpanSlot = null;
var ring_capacity: usize = 0;
var ring_mask: usize = 0;
var configured_capacity: usize = DEFAULT_RING_CAPACITY;
var ring_initialized: std.atomic.Value(u8) = std.atomic.Value(u8).init(0);
var init_lock: Mutex = .{};
var next_slot_counter: std.atomic.Value(u64) = std.atomic.Value(u64).init(0);
var generation_counter: std.atomic.Value(u64) = std.atomic.Value(u64).init(1);

/// Incremented every time a span claim fails (ring full) or an unsampled
/// span is created. Exposed to Python via `span_dropped_count()`.
pub var dropped_count: std.atomic.Value(u64) = std.atomic.Value(u64).init(0);

// Debug tracing — matches the pattern in db.zig / metrics_py.zig.
const TRACE = @import("builtin").mode == .Debug;
fn trace(comptime fmt: []const u8, args: anytype) void {
    if (TRACE) std.debug.print("[SPAN] " ++ fmt ++ "\n", args);
}

// ── UTF-8 safe truncation ───────────────────────────────────────────────────
//
// Return the largest byte-count N ≤ max such that `s[0..N]` ends on
// a valid UTF-8 codepoint boundary. Rolls back from `max` past any
// continuation bytes until it hits a lead byte. This prevents
// storing partial codepoints in the ring, which would fail
// `PyUnicode_FromStringAndSize` at drain time.
//
// Cost: at most 3 byte loads + branches — UTF-8 codepoints are ≤ 4
// bytes, so we never walk further than 3 back from the truncation
// point.
fn utf8SafeLen(s: []const u8, max: usize) usize {
    if (s.len <= max) return s.len;
    var cut: usize = max;
    // Walk back past any continuation bytes (0x80..0xBF). At most
    // 3 iterations because the longest UTF-8 sequence is 4 bytes.
    var steps: usize = 0;
    while (cut > 0 and (s[cut] & 0xC0) == 0x80 and steps < 3) {
        cut -= 1;
        steps += 1;
    }
    // If we landed on a lead byte, check whether the codepoint
    // starting there actually fits in `max`. If not, cut BEFORE it.
    if (cut < s.len) {
        const lead = s[cut];
        const codepoint_len: usize = if (lead < 0x80)
            1
        else if (lead < 0xC0)
            1 // invalid but we can't do better; take 1 byte
        else if (lead < 0xE0)
            2
        else if (lead < 0xF0)
            3
        else
            4;
        if (cut + codepoint_len > max) {
            // Can't fit — exclude this codepoint entirely.
            return cut;
        }
    }
    return @min(cut + 1, max);
}

// ── Handle encoding ─────────────────────────────────────────────────────────

const GEN_MASK: u64 = 0x0000_FFFF_FFFF_FFFF;
const SLOT_SHIFT: u6 = 48;

fn encodeHandle(slot_idx: usize, gen: u64) u64 {
    return (@as(u64, @intCast(slot_idx)) << SLOT_SHIFT) | (gen & GEN_MASK);
}

fn decodeSlotIdx(handle: u64) usize {
    return @intCast(handle >> SLOT_SHIFT);
}

fn decodeGen(handle: u64) u64 {
    return handle & GEN_MASK;
}

// ── Initialization + configuration ──────────────────────────────────────────

pub const ConfigureError = error{
    AlreadyInitialized,
    CapacityNotPowerOfTwo,
    CapacityOutOfRange,
    OutOfMemory,
};

/// One-time runtime configuration. Sets the desired capacity for the ring
/// before the first `init()` call. Calling `configure(N)` after the ring
/// has already been **successfully** initialized returns
/// `error.AlreadyInitialized` — reconfiguring a live ring would dangle
/// in-flight span handles.
///
/// If init() previously ran but failed (e.g. OOM on a huge capacity),
/// configure() is allowed to retry with a smaller value. The
/// `ring_initialized` flag is rolled back so the next start() can
/// re-attempt allocation.
///
/// `new_capacity` MUST be a power of 2 in
/// `[MIN_RING_CAPACITY, MAX_RING_CAPACITY]`. The power-of-2 requirement
/// is what makes the slot-index modulo a single AND instruction.
///
/// **Validation order**: range + power-of-2 are checked FIRST,
/// regardless of init state. This guarantees the user always gets
/// the most useful error type — `CapacityOutOfRange` /
/// `CapacityNotPowerOfTwo` (→ ValueError) if their input is bad,
/// `AlreadyInitialized` (→ RuntimeError) only if input was valid
/// but the ring is locked-in. Without this order, a typo'd setting
/// against a live ring would surface as a confusing
/// "AlreadyInitialized" message.
///
/// Concurrent configure() calls are last-write-wins under `init_lock`.
/// Configure-twice is a misuse pattern — call exactly once at startup.
pub fn configure(new_capacity: usize) ConfigureError!void {
    // Validate input FIRST so bad values always raise the
    // input-validation error, regardless of init state.
    if (new_capacity < MIN_RING_CAPACITY or new_capacity > MAX_RING_CAPACITY) {
        return error.CapacityOutOfRange;
    }
    // Power-of-2 check: x > 0 and (x & (x-1)) == 0.
    if ((new_capacity & (new_capacity - 1)) != 0) {
        return error.CapacityNotPowerOfTwo;
    }
    // Now check operational state. Allow configure if init never ran
    // OR ran-but-failed (ring_slice null after a failed init).
    if (ring_initialized.load(.acquire) != 0 and ring_slice != null) {
        return error.AlreadyInitialized;
    }
    init_lock.lock();
    defer init_lock.unlock();
    // Re-check under lock — another thread may have succeeded init
    // between the unlocked check and now.
    if (ring_initialized.load(.acquire) != 0 and ring_slice != null) {
        return error.AlreadyInitialized;
    }
    configured_capacity = new_capacity;
    // Roll back the failed-init flag so the next start() retries
    // allocation at the new (presumably smaller) capacity.
    ring_initialized.store(0, .release);
    trace("span ring configure: capacity={d}", .{new_capacity});
}

/// Read the currently-configured ring capacity. Reflects the most recent
/// successful `configure(...)` call, OR `DEFAULT_RING_CAPACITY` if none
/// has happened. Once `init()` runs successfully this is the live ring's
/// actual size and cannot change for the process lifetime.
///
/// Edge case — failed init (e.g. OOM): returns the requested
/// `configured_capacity` (the user's intent) rather than 0, so
/// monitoring code observing this value sees a sensible answer.
/// Distinguish "ring not allocated" by checking that
/// `_span_dropped_count()` is rising while `_span_drain()` returns
/// nothing — every span becomes a sentinel after a failed init.
pub fn capacity() usize {
    // ring_slice is the source of truth for "is the ring operational?".
    // It's only non-null after a successful allocation in init().
    if (ring_slice != null) {
        return ring_capacity;
    }
    return configured_capacity;
}

/// True if init() has run AND successfully allocated the ring. False
/// before first use OR after a failed init (OOM). Producers fall back
/// to dropping every span when this is false.
pub fn isOperational() bool {
    return ring_slice != null;
}

/// Allocate + zero-initialize the ring at the configured capacity. Safe
/// to call multiple times — subsequent calls are no-ops thanks to the
/// `ring_initialized` atomic flag.
///
/// Allocation goes through `c_allocator` (the same allocator used for
/// the metric registry) so it shows up as a normal heap allocation in
/// memory profilers and is freed by the OS at process exit.
pub fn init() void {
    // Double-check pattern: most calls hit the "already initialized"
    // fast path and return without touching anything.
    if (ring_initialized.load(.acquire) != 0) return;
    init_lock.lock();
    defer init_lock.unlock();
    if (ring_initialized.load(.acquire) != 0) return;

    const cap = configured_capacity;
    // Allocator can fail; on OOM we fall back to NO ring (every span
    // becomes a sentinel) so the rest of the process keeps working.
    const slots = std.heap.c_allocator.alloc(SpanSlot, cap) catch {
        trace("span ring init: OOM trying to allocate {d} slots", .{cap});
        ring_initialized.store(1, .release);
        return;
    };
    for (slots) |*slot| {
        slot.state = @intFromEnum(SlotState.free);
        slot.status_code = @intFromEnum(StatusCode.unset);
        slot.sampled = 0;
        slot.name_len = 0;
        slot.attrs_used = 0;
        slot.events_used = 0;
        slot.event_count = 0;
        slot.generation = 0;
    }
    ring_slice = slots.ptr;
    ring_capacity = cap;
    ring_mask = cap - 1;
    ring_initialized.store(1, .release);
    trace("span ring initialized: capacity={d}", .{cap});
}

/// Test helper — reset all ring state in place at the current capacity.
/// DO NOT call from production code; concurrent producers during reset
/// will corrupt their in-flight spans.
pub fn resetForTests() void {
    // Force init if we're being reset before any spans have been
    // started — gives tests a deterministic empty ring.
    init();
    if (ring_slice) |ptr| {
        var i: usize = 0;
        while (i < ring_capacity) : (i += 1) {
            const slot = &ptr[i];
            slot.state = @intFromEnum(SlotState.free);
            slot.status_code = @intFromEnum(StatusCode.unset);
            slot.sampled = 0;
            slot.name_len = 0;
            slot.attrs_used = 0;
            slot.events_used = 0;
            slot.event_count = 0;
            slot.generation = 0;
        }
    }
    next_slot_counter.store(0, .release);
    generation_counter.store(1, .release);
    dropped_count.store(0, .release);
}

// ── Core operations ─────────────────────────────────────────────────────────

/// Start a new span. Returns an opaque handle; pass it to subsequent
/// set_attr/set_status/end calls. Returns `SENTINEL_HANDLE` (0) for
/// unsampled spans and on ring overflow — in both cases, subsequent
/// operations no-op safely.
///
/// `name` is truncated to NAME_MAX bytes. `trace_id_high`/`trace_id_low`
/// form the 128-bit W3C trace ID; pass zeros for untraced spans.
pub fn start(
    trace_id_high: u64,
    trace_id_low: u64,
    parent_id: u64,
    name: []const u8,
    sampled: bool,
) u64 {
    init();

    if (!sampled) {
        _ = dropped_count.fetchAdd(1, .monotonic);
        return SENTINEL_HANDLE;
    }

    // If init() failed (OOM), there is no ring — every span is dropped.
    const ring_ptr = ring_slice orelse {
        _ = dropped_count.fetchAdd(1, .monotonic);
        return SENTINEL_HANDLE;
    };

    // Round-robin slot selection via atomic counter. Modulo via AND
    // because `ring_mask = capacity - 1` and capacity is power of 2.
    const slot_raw = next_slot_counter.fetchAdd(1, .monotonic);
    const slot_idx: usize = @intCast(slot_raw & ring_mask);
    const slot = &ring_ptr[slot_idx];

    // CAS slot state free → recording. If another thread beat us (ring
    // wrap race) or the slot is still held by a not-yet-drained span,
    // we drop this span.
    const cas_result = @cmpxchgStrong(
        u8,
        &slot.state,
        @intFromEnum(SlotState.free),
        @intFromEnum(SlotState.recording),
        .acquire,
        .monotonic,
    );
    if (cas_result != null) {
        _ = dropped_count.fetchAdd(1, .monotonic);
        return SENTINEL_HANDLE;
    }

    // We own the slot. Populate it. Note: generation is NOT atomic
    // because we just exclusively claimed the slot via the CAS above,
    // so there's no observer racing against us on this slot until we
    // store state=complete at end().
    const gen_full = generation_counter.fetchAdd(1, .monotonic);
    const gen = gen_full & GEN_MASK;
    slot.generation = gen;
    slot.trace_id_high = trace_id_high;
    slot.trace_id_low = trace_id_low;
    slot.span_id = gen_full;
    slot.parent_id = parent_id;
    slot.start_ns = @intCast(nanoTimestamp());
    slot.end_ns = 0;
    slot.status_code = @intFromEnum(StatusCode.unset);
    slot.sampled = 1;
    slot.attrs_used = 0;
    slot.events_used = 0;
    slot.event_count = 0;

    // UTF-8 safe truncation: never split a codepoint mid-sequence.
    // The drain path hands the bytes to `PyUnicode_FromStringAndSize`
    // which strict-checks UTF-8, so partial sequences would error.
    const copy_len = utf8SafeLen(name, NAME_MAX);
    @memcpy(slot.name[0..copy_len], name[0..copy_len]);
    slot.name_len = @intCast(copy_len);

    return encodeHandle(slot_idx, gen);
}

/// Append a (key, value) attribute pair to the span. Values are stored
/// as string bytes — callers can encode int/float/bool as their own
/// string representation. Silently drops attrs that would overflow
/// ATTRS_MAX; callers interested in overflow check the slot's
/// `attrs_used` field via export.
pub fn setAttr(handle: u64, key: []const u8, value: []const u8) void {
    if (handle == SENTINEL_HANDLE) return;
    const ring_ptr = ring_slice orelse return;
    const slot_idx = decodeSlotIdx(handle);
    if (slot_idx >= ring_capacity) return;
    const slot = &ring_ptr[slot_idx];

    // Stale-handle check: if the slot has been recycled since this
    // handle was issued, the generation field won't match.
    if (slot.generation != decodeGen(handle)) return;
    if (slot.state != @intFromEnum(SlotState.recording)) return;

    // Pack: [key_len u8][val_len u8][key bytes][val bytes]
    // Truncate at a UTF-8 codepoint boundary so the drain path
    // never emits invalid bytes. 255 is the u8 storage cap on the
    // length prefix; the real cap is ATTRS_MAX on the total.
    const key_len: usize = utf8SafeLen(key, @min(key.len, 255));
    const val_len: usize = utf8SafeLen(value, @min(value.len, 255));
    const total: usize = 2 + key_len + val_len;
    const used: usize = @intCast(slot.attrs_used);
    if (used + total > ATTRS_MAX) return; // drop silently

    slot.attrs[used] = @intCast(key_len);
    slot.attrs[used + 1] = @intCast(val_len);
    @memcpy(slot.attrs[used + 2 .. used + 2 + key_len], key[0..key_len]);
    @memcpy(
        slot.attrs[used + 2 + key_len .. used + 2 + key_len + val_len],
        value[0..val_len],
    );
    slot.attrs_used = @intCast(used + total);
}

/// Set span status. `code` is a `StatusCode` value cast to u8.
pub fn setStatus(handle: u64, code: u8) void {
    if (handle == SENTINEL_HANDLE) return;
    const ring_ptr = ring_slice orelse return;
    const slot_idx = decodeSlotIdx(handle);
    if (slot_idx >= ring_capacity) return;
    const slot = &ring_ptr[slot_idx];
    if (slot.generation != decodeGen(handle)) return;
    if (slot.state != @intFromEnum(SlotState.recording)) return;
    slot.status_code = code;
}

/// Add a timestamped event to a recording span. Events are packed as:
///   [timestamp_ns: i64 (8 bytes)][name_len: u8 (1 byte)][name bytes]
/// Overflow drops silently (same discipline as setAttr). Max 255 events
/// per span (u8 counter), but in practice the 128-byte arena limits it
/// to 4-14 events depending on name lengths.
///
/// This is the Zig-side implementation; Python calls through
/// `_span_add_event(handle, name)` in metrics_py.zig.
pub fn addEvent(handle: u64, name: []const u8) void {
    if (handle == SENTINEL_HANDLE) return;
    const ring_ptr = ring_slice orelse return;
    const slot_idx = decodeSlotIdx(handle);
    if (slot_idx >= ring_capacity) return;
    const slot = &ring_ptr[slot_idx];
    if (slot.generation != decodeGen(handle)) return;
    if (slot.state != @intFromEnum(SlotState.recording)) return;

    const name_len: usize = utf8SafeLen(name, @min(name.len, 255));
    const total: usize = 8 + 1 + name_len; // i64 timestamp + u8 name_len + name
    const used: usize = @intCast(slot.events_used);
    if (used + total > EVENTS_MAX) return; // Drop silently

    // Pack: [timestamp_ns i64 LE][name_len u8][name bytes]
    const ts: i64 = @intCast(nanoTimestamp());
    const ts_bytes: [8]u8 = @bitCast(ts);
    @memcpy(slot.events[used .. used + 8], &ts_bytes);
    slot.events[used + 8] = @intCast(name_len);
    @memcpy(slot.events[used + 9 .. used + 9 + name_len], name[0..name_len]);
    slot.events_used = @intCast(used + total);
    slot.event_count += 1;
}

/// End a span. Writes end_ns and transitions slot state to `complete`
/// so the drain thread can pick it up. Safe to call on the sentinel
/// handle (no-op) or a stale handle (no-op via generation check).
pub fn end(handle: u64) void {
    if (handle == SENTINEL_HANDLE) return;
    const ring_ptr = ring_slice orelse return;
    const slot_idx = decodeSlotIdx(handle);
    if (slot_idx >= ring_capacity) return;
    const slot = &ring_ptr[slot_idx];
    if (slot.generation != decodeGen(handle)) return;
    if (slot.state != @intFromEnum(SlotState.recording)) return;
    slot.end_ns = @intCast(nanoTimestamp());
    // Release store — pair with the acquire load in drain() so the
    // drain thread sees all prior writes to this slot.
    @atomicStore(u8, &slot.state, @intFromEnum(SlotState.complete), .release);
}

// ── Drain ───────────────────────────────────────────────────────────────────

/// Exported record shape — plain struct the FFI layer can walk and
/// convert into Python dicts without going through the slot's raw
/// bytes directly.
pub const DrainedSpan = struct {
    trace_id_high: u64,
    trace_id_low: u64,
    span_id: u64,
    parent_id: u64,
    start_ns: i64,
    end_ns: i64,
    status_code: u8,
    name: []const u8, // borrowed from ring slot — valid until next reclaim
    attrs_raw: []const u8, // packed KV bytes — caller unpacks
    events_raw: []const u8, // packed event bytes — caller unpacks (v0.15.2)
    event_count: u8, // number of events stored (v0.15.2)
};

/// Scan the ring for slots in `complete` state. For each one, call
/// `emit(ctx, span)`; the slot is then returned to `free` state for
/// reuse. Called by the drain thread. NOT thread-safe against
/// multiple concurrent callers — there is exactly one drain thread
/// by construction.
pub fn drain(
    ctx: *anyopaque,
    emit: *const fn (ctx: *anyopaque, span: *const DrainedSpan) void,
) usize {
    const ring_ptr = ring_slice orelse return 0;
    var processed: usize = 0;
    var idx: usize = 0;
    while (idx < ring_capacity) : (idx += 1) {
        const slot = &ring_ptr[idx];
        // Acquire load — pairs with the release store in end().
        const state = @atomicLoad(u8, &slot.state, .acquire);
        if (state != @intFromEnum(SlotState.complete)) continue;

        const ds = DrainedSpan{
            .trace_id_high = slot.trace_id_high,
            .trace_id_low = slot.trace_id_low,
            .span_id = slot.span_id,
            .parent_id = slot.parent_id,
            .start_ns = slot.start_ns,
            .end_ns = slot.end_ns,
            .status_code = slot.status_code,
            .name = slot.name[0..slot.name_len],
            .attrs_raw = slot.attrs[0..slot.attrs_used],
            .events_raw = slot.events[0..slot.events_used],
            .event_count = slot.event_count,
        };
        emit(ctx, &ds);
        processed += 1;

        // Mark the slot free again. Use release so any producer that
        // later claims it sees our writes as happens-before.
        @atomicStore(u8, &slot.state, @intFromEnum(SlotState.free), .release);
    }
    return processed;
}

/// Read the global dropped counter.
pub fn droppedCount() u64 {
    return dropped_count.load(.monotonic);
}
