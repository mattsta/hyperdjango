// TurboServer – Zig HTTP server core.
// Placeholder that registers routes and runs an event loop.
// The actual HTTP serving uses Zig's std.net / std.http.

const std = @import("std");
const builtin = @import("builtin");
const py = @import("py.zig");
const c = py.c;
const router_mod = @import("router.zig");
const dhi = @import("dhi_validator.zig");
const multipart = @import("multipart.zig");
const db = @import("db.zig");
const ws = @import("websocket_server.zig");
const metrics = @import("metrics_py.zig");
const reactor_mod = @import("reactor.zig");

const allocator = std.heap.c_allocator;

// ── Route storage ───────────────────────────────────────────────────────────

const MAX_PARAMS: usize = 32;

const ParamType = enum(u8) { str, int, float, bool_val };

const ParamMeta = struct {
    name: []const u8,
    type_tag: ParamType,
    has_default: bool, // true → skip if missing (let Python use its own default)
};

fn parseParamType(s: []const u8) ParamType {
    if (std.mem.eql(u8, s, "int")) return .int;
    if (std.mem.eql(u8, s, "float")) return .float;
    if (std.mem.eql(u8, s, "bool")) return .bool_val;
    return .str;
}

/// Parse "name:type|name:type|..." into out[]. Returns count of parsed params.
/// Slices point into meta_str, so meta_str must outlive the result.
/// Parse "name:type[?]|name:type[?]|..." into out[]. Returns count of parsed params.
/// '?' suffix on type means the param has a Python default — skip if missing.
/// Slices point into meta_str, so meta_str must outlive the result.
fn parseParamMeta(meta_str: []const u8, out: *[MAX_PARAMS]ParamMeta) usize {
    if (meta_str.len == 0) return 0;
    var count: usize = 0;
    var it = std.mem.splitScalar(u8, meta_str, '|');
    while (it.next()) |pair| {
        if (pair.len == 0 or count >= MAX_PARAMS) break;
        const colon = std.mem.indexOfScalar(u8, pair, ':') orelse continue;
        var type_str = pair[colon + 1 ..];
        const has_default = type_str.len > 0 and type_str[type_str.len - 1] == '?';
        if (has_default) type_str = type_str[0 .. type_str.len - 1];
        out[count] = .{
            .name = pair[0..colon],
            .type_tag = parseParamType(type_str),
            .has_default = has_default,
        };
        count += 1;
    }
    return count;
}

/// Fast query-string value lookup. Format: "k1=v1&k2=v2&...".
/// No percent-decoding (fine for int/float/simple str params in hot path).
/// Fast query-string value lookup. Format: "k1=v1&k2=v2&...".
fn queryStringGet(qs: []const u8, key: []const u8) ?[]const u8 {
    var it = std.mem.splitScalar(u8, qs, '&');
    while (it.next()) |pair| {
        const eq = std.mem.indexOfScalar(u8, pair, '=') orelse continue;
        if (std.mem.eql(u8, pair[0..eq], key)) return pair[eq + 1 ..];
    }
    return null;
}

fn hexNibble(ch: u8) ?u8 {
    return switch (ch) {
        '0'...'9' => ch - '0',
        'a'...'f' => ch - 'a' + 10,
        'A'...'F' => ch - 'A' + 10,
        else => null,
    };
}

/// Percent-decode src into buf. '+' → space, '%XX' → byte. Returns decoded slice.
/// If buf is too small, copies as many bytes as fit (safe truncation).
///
/// Fast path: most query strings and form values contain few or no '%' / '+'
/// bytes. We use indexOfAny to find the next decode-trigger byte and bulk
/// @memcpy the clean run, falling back to per-byte handling only at the
/// trigger. Long unencoded values (search terms, JSON, URLs) are O(chunks)
/// rather than O(bytes).
fn percentDecode(src: []const u8, buf: []u8, plus_space: bool) []u8 {
    var out: usize = 0;
    var i: usize = 0;
    // Only the QUERY string uses `+` as an alias for space (application/x-www-
    // form-urlencoded). In a URL PATH `+` is a literal plus, so path-sourced
    // values must NOT be `+`-decoded (F3). Callers pass plus_space accordingly.
    const triggers: []const u8 = if (plus_space) "%+" else "%";
    while (i < src.len and out < buf.len) {
        // Find the next byte that needs special handling.
        const rest = src[i..];
        const trigger_off = std.mem.indexOfAny(u8, rest, triggers);
        if (trigger_off == null) {
            // Clean tail — bulk-copy and finish.
            const remaining = src.len - i;
            const space = buf.len - out;
            const copy_n = if (remaining < space) remaining else space;
            @memcpy(buf[out .. out + copy_n], src[i .. i + copy_n]);
            out += copy_n;
            return buf[0..out];
        }
        const off = trigger_off.?;
        if (off > 0) {
            // Bulk-copy the literal prefix before the trigger byte.
            const space = buf.len - out;
            const copy_n = if (off < space) off else space;
            @memcpy(buf[out .. out + copy_n], src[i .. i + copy_n]);
            out += copy_n;
            i += copy_n;
            if (out >= buf.len) return buf[0..out];
        }
        // i now points at '%' or '+'.
        if (src[i] == '+') {
            buf[out] = ' ';
            out += 1;
            i += 1;
        } else if (i + 2 < src.len) {
            // src[i] == '%'
            const hi = hexNibble(src[i + 1]);
            const lo = hexNibble(src[i + 2]);
            if (hi != null and lo != null) {
                buf[out] = (hi.? << 4) | lo.?;
                out += 1;
                i += 3;
            } else {
                buf[out] = src[i];
                out += 1;
                i += 1;
            }
        } else {
            // Truncated '%' near end of input — copy literally.
            buf[out] = src[i];
            out += 1;
            i += 1;
        }
    }
    return buf[0..out];
}

const HandlerType = enum(u8) {
    simple_sync_noargs,
    simple_sync,
    model_sync,
    body_sync,
    enhanced,
};

fn parseHandlerType(s: []const u8) HandlerType {
    if (std.mem.eql(u8, s, "simple_sync_noargs")) return .simple_sync_noargs;
    if (std.mem.eql(u8, s, "simple_sync")) return .simple_sync;
    if (std.mem.eql(u8, s, "model_sync")) return .model_sync;
    if (std.mem.eql(u8, s, "body_sync")) return .body_sync;
    return .enhanced;
}

const HandlerEntry = struct {
    handler: *c.PyObject,
    handler_type: []const u8,
    handler_tag: HandlerType = .enhanced,
    param_types_json: []const u8,
    original_handler: ?*c.PyObject,
    model_param_name: ?[]const u8,
    model_class: ?*c.PyObject,
    // Vectorcall dispatch: ordered param metadata parsed at registration time
    param_meta: [MAX_PARAMS]ParamMeta = undefined,
    param_count: usize = 0,
};

const HeaderPair = struct {
    name: []const u8,
    value: []const u8,
};

const PythonResponse = struct {
    status_code: u16,
    content_type: []const u8,
    body: []const u8,
    extra_headers: []const u8 = "", // Pre-formatted "\r\nKey: Value" string for Set-Cookie, etc.
    ct_owned: bool = true,
    // When non-null the response is a CHUNKED STREAM: an OWNED reference to a
    // Python no-arg callable that yields the next body chunk (bytes) or None at
    // end. The dispatch site drives it via sendChunkedResponse (which CONSUMES /
    // decrefs it). `body` is empty in this case. Requires the GIL to decref, so
    // deinit deliberately does NOT touch it — sendChunkedResponse owns cleanup.
    stream_pull: ?*c.PyObject = null,

    fn deinit(self: PythonResponse) void {
        if (self.ct_owned and self.content_type.len > 0) allocator.free(self.content_type);
        if (self.body.len > 0) allocator.free(self.body);
        if (self.extra_headers.len > 0) allocator.free(self.extra_headers);
    }
};

// ── FFI native handler types (matching turboapi_ffi.h) ──────────────────────

const FfiRequest = extern struct {
    method: [*c]const u8,
    method_len: usize,
    path: [*c]const u8,
    path_len: usize,
    query_string: [*c]const u8,
    query_len: usize,
    body: [*c]const u8,
    body_len: usize,
    header_names: [*c]const [*c]const u8,
    header_name_lens: [*c]const usize,
    header_values: [*c]const [*c]const u8,
    header_value_lens: [*c]const usize,
    header_count: usize,
    param_names: [*c]const [*c]const u8,
    param_name_lens: [*c]const usize,
    param_values: [*c]const [*c]const u8,
    param_value_lens: [*c]const usize,
    param_count: usize,
};

const FfiResponse = extern struct {
    status_code: u16,
    content_type: [*c]const u8,
    content_type_len: usize,
    body: [*c]const u8,
    body_len: usize,
};

const NativeHandlerFn = *const fn (*const FfiRequest) callconv(.c) FfiResponse;
const NativeInitFn = *const fn () callconv(.c) c_int;

const NativeHandlerEntry = struct {
    handler_fn: NativeHandlerFn,
    lib_handle: *anyopaque,
};
// ── Static route entry — pre-rendered head + body, with a fresh Date per send ──
// The status line + fixed headers are rendered once at registration and stored
// WITHOUT a Date (which must be current at send time, not frozen at registration).
// The send path writev-splices a fresh Date between `head` and `body` — still one
// syscall, and the Date now matches every other response path.
const StaticRouteEntry = struct {
    head: []const u8, // "HTTP/1.1 <s> <t>\r\nContent-Type: ...\r\nContent-Length: N" (Connection + Date spliced per-send)
    body: []const u8, // response body bytes
};

var routes: ?std.StringHashMap(HandlerEntry) = null;
var native_routes: ?std.StringHashMap(NativeHandlerEntry) = null;
var static_routes: ?std.StringHashMap(StaticRouteEntry) = null;
const MAX_CACHE_ENTRIES: usize = 10_000; // bounded to prevent OOM via unique paths
var model_schemas: ?std.StringHashMap(dhi.ModelSchema) = null;
var router: ?router_mod.Router = null;

// ── Unified dispatch map (single-probe route resolution) ─────────────────────
// The router returns a `handler_key` ("METHOD path"); the dispatch path used to
// probe up to SIX StringHashMaps sequentially with that same key
// (static/file/native/db/python/model_schema). Those maps are still the
// registration source of truth (registration APIs unchanged), but at server
// start we fold them into ONE map whose value is a tagged union carrying the
// resolved handler kind + a pointer into the owning map's value (+ the optional
// model-schema pointer for the python body path). Hot-path lookup is then a
// single probe. The pointers stay valid because the source maps are frozen
// (read-only) once serving begins — buildDispatchMap runs after all
// registrations complete and before any worker starts.
const PyDispatch = struct {
    entry: *const HandlerEntry,
    schema: ?*const dhi.ModelSchema = null,
};
const DispatchEntry = union(enum) {
    static: *const StaticRouteEntry,
    file: *const FileRouteEntry,
    native: *const NativeHandlerEntry,
    db: *const db.DbRouteEntry,
    python: PyDispatch,
};
var dispatch_map: ?std.StringHashMap(DispatchEntry) = null;
var server_host: []const u8 = "127.0.0.1";
var server_port: u16 = 8000;
var server_max_body_size: usize = 10 * 1024 * 1024; // 10MB default, wired from MAX_BODY_SIZE setting
var cache_noargs_responses: bool = false;

// ── Resolved-once server configuration (Part 1) ──────────────────────────────
// The per-connection path used to re-read HYPER_SEND_TIMEOUT_MS /
// HYPER_IDLE_TIMEOUT_MS from the environment (getenv + parse) on EVERY accepted
// connection and every reactor arm. The env can't change after the server is
// up, so we resolve every server knob ONCE in server_run — before any worker or
// the accept loop starts — into this struct and have the hot paths read fields.
// Written exactly once (server_run, single-threaded, pre-workers) then read-only
// for the serving lifetime: no lock, no data race.
//
// py.zig owns HYPER_TCP_NODELAY and the BSD-only setBlocking; resolveServerConfig
// calls py.resolveNetConfig() so that knob is resolved on the same single-threaded
// pass and the accept path pays no getenv either.
const ServerConfig = struct {
    idle_timeout_ms: u64 = DEFAULT_IDLE_TIMEOUT_MS,
    send_timeout_ms: u64 = DEFAULT_SEND_TIMEOUT_MS,
};
var server_config: ServerConfig = .{};

/// Resolve all server.zig-owned env knobs into `server_config`. MUST be called
/// from server_run (single-threaded) before any worker/accept loop starts.
fn resolveServerConfig() void {
    server_config = .{
        .idle_timeout_ms = getIdleTimeoutMs(),
        .send_timeout_ms = getSendTimeoutMs(),
    };
    py.resolveNetConfig();
}

// ── Streaming body reader (per-worker-thread state) ────────────────────────
// When a request body exceeds server_max_body_size AND the handler supports
// streaming, we don't buffer the full body. Instead we store the socket +
// remaining byte count here, and Python pulls chunks via _read_body_chunk().
const StreamBodyState = struct {
    stream: ?py.NetStream = null,
    content_length: usize = 0,
    bytes_read: usize = 0,
    already_read: []const u8 = "",
    already_read_offset: usize = 0,
};
threadlocal var _stream_body: StreamBodyState = .{};

/// Python-callable: read the next chunk of body bytes from the TCP socket.
/// Called by request.stream() on the Python side. Returns bytes or empty
/// bytes when done. Runs on the same Zig worker thread that owns the socket.
pub fn read_body_chunk(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var max_bytes: c.Py_ssize_t = 262144; // 256KB default
    if (args) |a| {
        if (c.PyTuple_Size(a) >= 1) {
            const arg = c.PyTuple_GetItem(a, 0);
            if (arg) |item| {
                if (c.PyLong_Check(item) != 0) {
                    const v = c.PyLong_AsSsize_t(item);
                    // Only accept a positive size. A negative chunk_size would make
                    // the `@intCast(max_bytes)` below wrap (ReleaseFast) or panic
                    // (ReleaseSafe) — F6. On overflow PyLong_AsSsize_t returns -1
                    // and sets an exception; clear it and keep the default so no
                    // pending error rides back into Python.
                    if (v == -1 and c.PyErr_Occurred() != null) {
                        c.PyErr_Clear();
                    } else if (v > 0) {
                        max_bytes = v;
                    }
                }
            }
        }
    }

    var state = &_stream_body;
    const remaining = state.content_length - state.bytes_read;
    if (remaining == 0 or state.stream == null) {
        // Done — return empty bytes
        return c.PyBytes_FromStringAndSize("", 0);
    }

    const to_read = @min(@as(usize, @intCast(max_bytes)), remaining);
    var buf: [262144]u8 = undefined;
    const read_buf = if (to_read <= buf.len) buf[0..to_read] else blk: {
        break :blk allocator.alloc(u8, to_read) catch {
            return c.PyBytes_FromStringAndSize("", 0);
        };
    };
    defer if (to_read > buf.len) allocator.free(read_buf);

    // First: drain any already-read bytes from the header buffer
    var filled: usize = 0;
    if (state.already_read_offset < state.already_read.len) {
        const avail = state.already_read.len - state.already_read_offset;
        const copy_len = @min(avail, to_read);
        @memcpy(read_buf[0..copy_len], state.already_read[state.already_read_offset..][0..copy_len]);
        state.already_read_offset += copy_len;
        filled = copy_len;
        state.bytes_read += copy_len;
    }

    // Then: read from socket for the rest
    while (filled < to_read) {
        const n = state.stream.?.read(read_buf[filled..to_read]) catch break;
        if (n == 0) break; // Connection closed
        filled += n;
        state.bytes_read += n;
    }

    return c.PyBytes_FromStringAndSize(@ptrCast(read_buf.ptr), @intCast(filled));
}

// Graceful shutdown state
var shutdown_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false);
var shutdown_pipe: [2]std.posix.fd_t = .{ -1, -1 };
const DRAIN_TIMEOUT_S: u64 = 30;

// ── In-flight request gauge: per-worker cells, no shared hot-path atomic ─────
//
// A single global atomic incremented AND decremented on every request is one
// cache line written by every core twice per request — at high worker/core
// counts that ping-pong caps throughput (it dominated the 128-core scaling
// collapse). Instead each worker owns ONE cache-line-isolated cell that only
// IT writes (its worker id is unique), so the per-request update has zero
// cross-core contention. Readers (the shutdown drain, and a periodic gauge
// reconcile) sum the cells; the sum is exact once writes settle and never
// races (single writer per cell, relaxed loads elsewhere). The `/metrics`
// gauge is published from a worker at most once per GAUGE_RECONCILE_MS — the
// buffered-reconcile pattern, so the hot path never touches the shared gauge.
const CACHE_LINE: usize = 64;
const InflightCell = struct {
    value: std.atomic.Value(i64) align(CACHE_LINE) = std.atomic.Value(i64).init(0),
    _pad: [CACHE_LINE - @sizeOf(std.atomic.Value(i64))]u8 = undefined,
};
var inflight_cells: []InflightCell = &.{};

/// Adjust THIS worker's in-flight cell. Single writer per cell → a plain
/// load+store (relaxed) is race-free and contention-free; no RMW, no fence.
inline fn inflightAdd(worker_id: usize, delta: i64) void {
    const cell = &inflight_cells[worker_id].value;
    cell.store(cell.load(.monotonic) +% delta, .monotonic);
}

/// Sum every worker's cell — the authoritative in-flight count. Used by the
/// drain loop (not hot) and the periodic gauge reconcile. Clamped at 0: a
/// concurrent reconcile can momentarily observe a decrement before its paired
/// increment across cells.
fn activeRequests() u64 {
    var sum: i64 = 0;
    for (inflight_cells) |*cell| sum += cell.value.load(.monotonic);
    return if (sum < 0) 0 else @intCast(sum);
}

// Periodic gauge reconcile: one worker publishes sum(cells) to the shared
// `/metrics` gauge per interval, so the shared gauge is never touched on the
// per-request path. Losers of the interval CAS skip — the per-request cost is
// a single relaxed load and a branch.
const GAUGE_RECONCILE_MS: i64 = 1000;
var last_gauge_reconcile_ms = std.atomic.Value(i64).init(0);

inline fn maybeReconcileInflightGauge() void {
    const now = nowMonoMs();
    const last = last_gauge_reconcile_ms.load(.monotonic);
    if (now - last < GAUGE_RECONCILE_MS) return;
    // Only the worker that wins the timestamp CAS publishes this interval.
    if (last_gauge_reconcile_ms.cmpxchgStrong(last, now, .monotonic, .monotonic) != null) return;
    if (loadGauge(&_srv_active_gauge)) |g| g.set(@intCast(activeRequests()));
}

// ── Native server metrics (v0.15.2, task #261) ────────────────────────────
//
// Registered in the shared metric registry (metrics_py.zig) so they show
// up in `/metrics` alongside the Python-side telemetry series. Initialized
// lazily on first request so the cost is zero when the server is used as
// a WSGI bridge (runziserver) without telemetry.
//
// Each counter is one atomic increment per response (~35 ns) — less than
// 0.1% overhead on a typical 50 μs Zig-native response.

var _srv_metrics_init_flag: std.atomic.Value(u8) = std.atomic.Value(u8).init(0);
var _srv_responses_counter: ?*metrics.DynCounter = null;
var _srv_static_counter: ?*metrics.DynCounter = null;
var _srv_active_gauge: ?*metrics.DynGauge = null;
var _srv_idle_reaped_counter: ?*metrics.DynCounter = null;

// Per-status-class response tally (task #3). A labeled counter_vec would be
// ideal (native_responses_total{status_class="2xx"}) but DynCounterVec is not
// part of metrics_py's pub surface, so we register four distinct plain counters
// — a valid, unambiguous Prometheus representation of the same breakdown.
var _srv_resp_2xx_counter: ?*metrics.DynCounter = null;
var _srv_resp_3xx_counter: ?*metrics.DynCounter = null;
var _srv_resp_4xx_counter: ?*metrics.DynCounter = null;
var _srv_resp_5xx_counter: ?*metrics.DynCounter = null;
// Bumped at every site that sets _write_failed (partial/failed response write).
var _srv_write_failures_counter: ?*metrics.DynCounter = null;
// Listener (accept-loop) health: incremented on a fatal accept() error.
var _srv_accept_errors_counter: ?*metrics.DynCounter = null;
// Accepted connections, and the high-water number drained from the kernel accept
// queue in a SINGLE poll wakeup. The burst gauge is the only server-side signal
// for listen-queue overflow: the kernel drops an over-capacity connection
// silently (no errno here, no error at the peer), so a burst approaching the
// configured backlog is the fingerprint of connections that were never accepted
// and will report zero responses with zero errors.
var _srv_accepted_counter: ?*metrics.DynCounter = null;
var _srv_accept_burst_gauge: ?*metrics.DynGauge = null;
var accept_burst_max = std.atomic.Value(i64).init(0);

// ── Reactor connection-state observability (bounded, no labels) ─────────────
// Throughput and latency describe the connections that ARE being served and say
// nothing about the ones that aren't. These are the series that make a starved
// keep-alive set visible from the server side: how many connections are parked
// on a reactor, how many of those have never completed a single response, and
// how deep the dispatch queues are. All are plain gauges/counters recomputed
// once per sweep tick from the fd-indexed tables — no per-connection series.
var _srv_dispatched_counter: ?*metrics.DynCounter = null;
var _srv_rearm_counter: ?*metrics.DynCounter = null;
var _srv_rearm_fail_counter: ?*metrics.DynCounter = null;
var _srv_requeue_counter: ?*metrics.DynCounter = null;
var _srv_parked_gauge: ?*metrics.DynGauge = null;
var _srv_parked_unserved_gauge: ?*metrics.DynGauge = null;
var _srv_queue_depth_gauge: ?*metrics.DynGauge = null;

// ── Metric-pointer publication (A2#4) ───────────────────────────────────────
// initServerMetrics runs once (CAS-guarded) but every worker thread calls it at
// startup; the losers return early and then READ these pointers on the serving
// path. Publish each pointer with a RELEASE store after its counter/gauge is
// fully constructed, and have every reader ACQUIRE-load it — the same
// acquire/release discipline used for cached_coro_runner. A reader that observes
// a non-null pointer is then guaranteed to see the fully-initialised object; a
// reader that races ahead sees null and skips (already null-guarded).
inline fn storeCounter(p: *?*metrics.DynCounter, v: ?*metrics.DynCounter) void {
    @atomicStore(?*metrics.DynCounter, p, v, .release);
}
inline fn storeGauge(p: *?*metrics.DynGauge, v: ?*metrics.DynGauge) void {
    @atomicStore(?*metrics.DynGauge, p, v, .release);
}
inline fn loadCounter(p: *?*metrics.DynCounter) ?*metrics.DynCounter {
    return @atomicLoad(?*metrics.DynCounter, p, .acquire);
}
inline fn loadGauge(p: *?*metrics.DynGauge) ?*metrics.DynGauge {
    return @atomicLoad(?*metrics.DynGauge, p, .acquire);
}

/// Register one plain DynCounter in the shared registry. Returns null (leaving
/// the slot unregistered) on any allocation failure — metrics are best-effort.
/// Name/help are duped so callers can pass string literals.
fn registerDynCounter(name_lit: []const u8, help_lit: []const u8) ?*metrics.DynCounter {
    const alloc = std.heap.c_allocator;
    const counter = metrics.DynCounter.init() catch return null;
    const name = alloc.dupe(u8, name_lit) catch return null;
    const help = alloc.dupe(u8, help_lit) catch return null;
    const entry = alloc.create(metrics.MetricEntry) catch return null;
    entry.* = .{ .kind = .counter, .name = name, .help = help, .impl = counter };
    _ = metrics.registerEntry(entry) catch return null;
    return counter;
}

/// Register one plain DynGauge in the shared registry (gauge twin of
/// registerDynCounter). Returns null on any allocation failure.
fn registerDynGauge(name_lit: []const u8, help_lit: []const u8) ?*metrics.DynGauge {
    const alloc = std.heap.c_allocator;
    const gauge = metrics.DynGauge.init() catch return null;
    const name = alloc.dupe(u8, name_lit) catch return null;
    const help = alloc.dupe(u8, help_lit) catch return null;
    const entry = alloc.create(metrics.MetricEntry) catch return null;
    entry.* = .{ .kind = .gauge, .name = name, .help = help, .impl = gauge };
    _ = metrics.registerEntry(entry) catch return null;
    return gauge;
}

/// Classify a response status into its {2xx,3xx,4xx,5xx} bucket and bump the
/// matching counter. Called from every response-emitting funnel (sendResponse,
/// sendFullResponse, static path). 1xx and unknown classes are not tallied.
inline fn bumpStatusClass(status: u16) void {
    const cnt = switch (status / 100) {
        2 => loadCounter(&_srv_resp_2xx_counter),
        3 => loadCounter(&_srv_resp_3xx_counter),
        4 => loadCounter(&_srv_resp_4xx_counter),
        5 => loadCounter(&_srv_resp_5xx_counter),
        else => null,
    };
    if (cnt) |c2| c2.inc(1);
}

/// Tally `n` connections drained from the kernel accept queue in one wakeup and
/// raise the burst high-water mark. Runs only on the acceptor thread, so the
/// high-water update needs no CAS loop.
fn noteAcceptBurst(n: usize) void {
    if (loadCounter(&_srv_accepted_counter)) |cnt| cnt.inc(n);
    const v: i64 = @intCast(n);
    if (v > accept_burst_max.load(.monotonic)) {
        accept_burst_max.store(v, .monotonic);
        if (loadGauge(&_srv_accept_burst_gauge)) |g| g.set(v);
    }
}

/// Mark the current response write as failed AND tally it. Replaces bare
/// `_write_failed = true` assignments so every failure is observable.
inline fn noteWriteFailure() void {
    _write_failed = @as(bool, true);
    if (loadCounter(&_srv_write_failures_counter)) |c2| c2.inc(1);
}

// Always-valid tally of connections closed by the reactor idle sweep. The
// DynCounter mirror above may momentarily be null (registration is lazy on the
// first worker/reactor start), so the sweep bumps this atomic unconditionally
// and the counter opportunistically.
var idle_reaped_total = std.atomic.Value(u64).init(0);

fn initServerMetrics() void {
    // Atomic CAS — only one thread wins the 0→1 transition.
    // All other threads see 1 and return immediately. This prevents
    // duplicate metric registrations under 24-thread free-threading.
    if (@cmpxchgStrong(u8, &_srv_metrics_init_flag.raw, 0, 1, .acquire, .monotonic) != null) return;
    const alloc = std.heap.c_allocator;

    // Counter: total responses from the native server (all paths)
    if (metrics.DynCounter.init()) |counter| {
        const name = alloc.dupe(u8, "hyperdjango_native_responses_total") catch return;
        const help = alloc.dupe(u8, "HTTP responses from the native Zig server (all paths).") catch return;
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .counter, .name = name, .help = help, .impl = counter };
        _ = metrics.registerEntry(entry) catch return;
        storeCounter(&_srv_responses_counter, counter);
    } else |_| {}

    // Counter: static route hits (pre-rendered bytes, bypass Python entirely)
    if (metrics.DynCounter.init()) |counter| {
        const name = alloc.dupe(u8, "hyperdjango_native_static_responses_total") catch return;
        const help = alloc.dupe(u8, "Static route responses served from pre-rendered cache.") catch return;
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .counter, .name = name, .help = help, .impl = counter };
        _ = metrics.registerEntry(entry) catch return;
        storeCounter(&_srv_static_counter, counter);
    } else |_| {}

    // Gauge: active in-flight requests (mirrors the existing atomic)
    if (metrics.DynGauge.init()) |gauge| {
        const name = alloc.dupe(u8, "hyperdjango_native_connections_active") catch return;
        const help = alloc.dupe(u8, "In-flight requests currently being processed by the Zig server.") catch return;
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .gauge, .name = name, .help = help, .impl = gauge };
        _ = metrics.registerEntry(entry) catch return;
        storeGauge(&_srv_active_gauge, gauge);
    } else |_| {}

    // Counter: connections reaped by the reactor idle sweep (zero-byte/slowloris
    // parked fds the SO_RCVTIMEO guard can't catch — see sweepIdle).
    if (metrics.DynCounter.init()) |counter| {
        const name = alloc.dupe(u8, "hyperdjango_native_idle_connections_reaped_total") catch return;
        const help = alloc.dupe(u8, "Idle connections closed by the reactor idle sweep.") catch return;
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .counter, .name = name, .help = help, .impl = counter };
        _ = metrics.registerEntry(entry) catch return;
        storeCounter(&_srv_idle_reaped_counter, counter);
    } else |_| {}

    // Per-status-class response breakdown (task #3). Release-publish each pointer
    // after registration so acquire-loading readers see a fully-built counter.
    storeCounter(&_srv_resp_2xx_counter, registerDynCounter("hyperdjango_native_responses_2xx_total", "Native server responses with a 2xx status."));
    storeCounter(&_srv_resp_3xx_counter, registerDynCounter("hyperdjango_native_responses_3xx_total", "Native server responses with a 3xx status."));
    storeCounter(&_srv_resp_4xx_counter, registerDynCounter("hyperdjango_native_responses_4xx_total", "Native server responses with a 4xx status."));
    storeCounter(&_srv_resp_5xx_counter, registerDynCounter("hyperdjango_native_responses_5xx_total", "Native server responses with a 5xx status."));
    // Failed/partial response writes (desynced connections).
    storeCounter(&_srv_write_failures_counter, registerDynCounter("hyperdjango_native_write_failures_total", "Response writes that failed or were truncated (connection closed)."));
    // Listener/accept-loop health: fatal accept() errors.
    storeCounter(&_srv_accept_errors_counter, registerDynCounter("hyperdjango_native_accept_errors_total", "Fatal accept() errors on the listen socket (accept-loop health)."));
    storeCounter(&_srv_accepted_counter, registerDynCounter("hyperdjango_native_accepted_connections_total", "Connections accepted from the listen socket."));
    storeGauge(&_srv_accept_burst_gauge, registerDynGauge("hyperdjango_native_accept_burst_max", "Largest number of connections drained from the kernel accept queue in one wakeup; approaching the listen backlog means connections were silently dropped."));
    // Reactor connection-state series — the starved-keep-alive signal.
    storeCounter(&_srv_dispatched_counter, registerDynCounter("hyperdjango_native_reactor_dispatched_total", "Readable connections dispatched from a reactor to a worker."));
    storeCounter(&_srv_rearm_counter, registerDynCounter("hyperdjango_native_reactor_rearm_total", "Connections re-armed on a reactor after being served."));
    storeCounter(&_srv_rearm_fail_counter, registerDynCounter("hyperdjango_native_reactor_rearm_failures_total", "Re-arm registrations that failed (connection dropped)."));
    storeCounter(&_srv_requeue_counter, registerDynCounter("hyperdjango_native_reactor_requeue_total", "Connections handed back to a shard queue at the pipelining fairness cap."));
    storeGauge(&_srv_parked_gauge, registerDynGauge("hyperdjango_native_reactor_parked_connections", "Keep-alive connections currently parked on a reactor (armed, owned by nobody)."));
    storeGauge(&_srv_parked_unserved_gauge, registerDynGauge("hyperdjango_native_reactor_parked_unserved", "Parked connections that have never completed a response — a non-zero steady value is a starved keep-alive set."));
    storeGauge(&_srv_queue_depth_gauge, registerDynGauge("hyperdjango_native_reactor_queue_depth", "Connections waiting in the shard dispatch queues for a worker."));
}

// Interpreter reference captured before releasing the GIL at server start.
// Workers use this to create their own PyThreadState rather than calling
// PyGILState_Ensure (which pays a per-call thread-state lookup cost).
var py_interp: ?*anyopaque = null;

fn getRoutes() *std.StringHashMap(HandlerEntry) {
    if (routes == null) {
        routes = std.StringHashMap(HandlerEntry).init(allocator);
    }
    return &routes.?;
}

fn getNativeRoutes() *std.StringHashMap(NativeHandlerEntry) {
    if (native_routes == null) {
        native_routes = std.StringHashMap(NativeHandlerEntry).init(allocator);
    }
    return &native_routes.?;
}

fn getStaticRoutes() *std.StringHashMap(StaticRouteEntry) {
    if (static_routes == null) {
        static_routes = std.StringHashMap(StaticRouteEntry).init(allocator);
    }
    return &static_routes.?;
}

// ── Sharded response cache ──────────────────────────────────────────────────
// The response cache was a single process-wide StringHashMap behind ONE rwlock:
// every cached request took a shared read-lock on that lock, so the read path
// serialised on one cache line's reader count across all workers. Shard it into
// N independent (map + rwlock) buckets picked by hash(key) & (N-1). Reads and
// writes for different shards never touch the same lock, so the reader-count
// contention on the hot path is cut by a factor of N. Each shard keeps its own
// entry count against a per-shard slice of MAX_CACHE_ENTRIES so the global cap
// is preserved. The cache is populated once per key (first miss) then read
// forever, so writes are rare and only ever contend within a single shard.
const CACHE_SHARDS: usize = 16; // power of two → mask with (N-1)
const CacheShard = struct {
    map: ?std.StringHashMap([]const u8) = null,
    count: usize = 0,
    // Sum of cached VALUE bytes in this shard, capped independently of the entry
    // count: 10k small entries and a handful of huge ones are both bounded, so a
    // future enable of this cache can't OOM via large values (mirrors db.zig's
    // byte-capped response cache). Entries are insert-once/read-forever, so this
    // only grows on a successful first insert.
    bytes: usize = 0,
    rwlock: py.RwLock = .{},
};
var cache_shards: [CACHE_SHARDS]CacheShard = [_]CacheShard{.{}} ** CACHE_SHARDS;
const MAX_CACHE_ENTRIES_PER_SHARD: usize = MAX_CACHE_ENTRIES / CACHE_SHARDS;
// 128 MB total value ceiling (matches db.zig), split evenly across shards.
const MAX_CACHE_BYTES_PER_SHARD: usize = (128 * 1024 * 1024) / CACHE_SHARDS;

inline fn cacheShardFor(key: []const u8) *CacheShard {
    return &cache_shards[std.hash.Wyhash.hash(0, key) & (CACHE_SHARDS - 1)];
}

/// Cache a pre-rendered response, respecting the per-shard cap to prevent OOM.
/// Only the owning shard's write lock is taken — writes to other shards proceed
/// concurrently. getOrPut prevents a race where two threads cache the same key.
fn cacheResponse(key: []const u8, rendered: []const u8) void {
    const shard = cacheShardFor(key);
    shard.rwlock.lock();
    defer shard.rwlock.unlock();

    // Refuse insertion past EITHER the entry cap or the byte ceiling — both keep
    // the cache bounded so unique paths (entries) or large values (bytes) can't
    // grow it without limit.
    if (shard.count >= MAX_CACHE_ENTRIES_PER_SHARD or shard.bytes + rendered.len > MAX_CACHE_BYTES_PER_SHARD) {
        allocator.free(rendered);
        return;
    }
    if (shard.map == null) shard.map = std.StringHashMap([]const u8).init(allocator);
    const new_key = allocator.dupe(u8, key) catch {
        allocator.free(rendered);
        return;
    };
    const gop = shard.map.?.getOrPut(new_key) catch {
        allocator.free(new_key);
        allocator.free(rendered);
        return;
    };
    if (gop.found_existing) {
        // Another thread already cached this. StringHashMap keeps the EXISTING
        // stored key, not our `new_key` — so free `new_key` (our fresh dup), NOT
        // gop.key_ptr.* (the live stored key, which would be a UAF/double-free).
        allocator.free(rendered);
        allocator.free(new_key);
    } else {
        gop.value_ptr.* = rendered;
        shard.count += 1;
        shard.bytes += rendered.len;
    }
}

/// Thread-safe response cache lookup — takes only the owning shard's read lock,
/// so concurrent reads across different keys almost never contend.
fn getCachedResponse(key: []const u8) ?[]const u8 {
    const shard = cacheShardFor(key);
    shard.rwlock.lockShared();
    defer shard.rwlock.unlockShared();
    const m = shard.map orelse return null;
    return m.get(key);
}

fn getModelSchemas() *std.StringHashMap(dhi.ModelSchema) {
    if (model_schemas == null) {
        model_schemas = std.StringHashMap(dhi.ModelSchema).init(allocator);
    }
    return &model_schemas.?;
}

pub fn getRouter() *router_mod.Router {
    if (router == null) {
        router = router_mod.Router.init(allocator);
    }
    return &router.?;
}

// ── Per-worker request arena ────────────────────────────────────────────────
// Request-transient allocations (currently the parseHeaders header list) used
// to churn the process-wide c_allocator (malloc/free) once per request AND
// contend the global allocator across parallel workers. Each worker thread now
// owns a private ArenaAllocator, reset with retain_capacity at the start of each
// request. ONLY genuinely request-transient allocations use it — anything that
// outlives the request (response-cache dupes, PythonResponse owned buffers, body
// copies that may be up to MAX_BODY_SIZE) stays on the global allocator so the
// arena never pins large or long-lived memory.
threadlocal var req_arena: ?std.heap.ArenaAllocator = null;

// Set by the response writers (sendResponse/sendFullResponse/static) when a
// write to the client fails or only partially completes. Those writers return
// void (their errors are otherwise swallowed), so this flag is how the
// per-request loop learns a response was NOT fully delivered and must close the
// connection instead of continuing a now-desynced keep-alive. Reset at the top
// of each handleOneRequest.
threadlocal var _write_failed: bool = false;

// Set per-request in handleOneRequest from the client's `Connection: close`
// token (or HTTP/1.0 default-close semantics). Threadlocal, so it is safe under
// free-threading — each worker sees only its own current request. The response
// builders read it to emit `Connection: close` instead of `keep-alive`, and the
// per-connection loops (threaded handleConnection, reactor serveConnectionBurst)
// read it after handleOneRequest to close the socket instead of re-arming/
// keeping it alive — so a close request never hangs until SO_RCVTIMEO. Reset at
// the top of each handleOneRequest.
threadlocal var _conn_close: bool = false;

// Set per-request in handleOneRequest from the request method. A HEAD request
// must receive the IDENTICAL status line + headers a GET would (including the
// Content-Length the GET body would have) but ZERO body bytes (RFC 7230 §3.3.3)
// — sending the body desyncs the next keep-alive/pipelined request. Threadlocal
// so each worker sees only its own current request; the response writers read it
// to suppress the body. Reset at the top of each handleOneRequest so a stale
// HEAD from a prior request can't strip the body of a following GET's error.
threadlocal var _req_is_head: bool = false;

/// The `Connection:` header token for the current request's response.
inline fn connectionHeaderValue() []const u8 {
    return if (_conn_close) "close" else "keep-alive";
}

/// RFC 7230 §3.3.{1,2}: 1xx, 204 and 304 responses MUST NOT carry a message
/// body, and MUST NOT frame one with Content-Length either (304 may keep a
/// Content-Length reflecting the would-be GET, but we simply omit it — never a
/// body). Writing a body for these desyncs the connection just like a HEAD body.
inline fn statusForbidsBody(status: u16) bool {
    return (status >= 100 and status < 200) or status == 204 or status == 304;
}

/// APPEND_SLASH parity with the ASGI dispatch path. The framework default is
/// True (see conf.py APPEND_SLASH); the native server honours the setting via
/// HYPER_APPEND_SLASH — set to "0"/"false"/"no"/"off" to disable. The Python
/// side should export this env from get_setting("APPEND_SLASH") so native and
/// ASGI agree (orchestrator/app.py counterpart). Default: enabled.
fn appendSlashEnabled() bool {
    const env_ptr = std.c.getenv("HYPER_APPEND_SLASH") orelse return true;
    const v = std.mem.sliceTo(env_ptr, 0);
    if (std.ascii.eqlIgnoreCase(v, "0") or std.ascii.eqlIgnoreCase(v, "false") or
        std.ascii.eqlIgnoreCase(v, "no") or std.ascii.eqlIgnoreCase(v, "off")) return false;
    return true;
}

/// Pure APPEND_SLASH decision (env gate applied separately at the call site):
/// true when the request `path` lacks a trailing slash but the matched route's
/// registered pattern has one. The pattern is the tail of `handler_key`
/// ("METHOD /pattern"), so a handler_key ending in '/' means the pattern did.
/// Mirrors the ASGI `Router.resolve` check `not path.endswith("/") and
/// route.pattern.endswith("/")`.
fn needsAppendSlashRedirect(path: []const u8, handler_key: []const u8) bool {
    return path.len > 0 and path[path.len - 1] != '/' and
        handler_key.len > 0 and handler_key[handler_key.len - 1] == '/';
}

/// Strip CR/LF from a handler-controlled header VALUE to prevent response
/// splitting / header injection (RFC 7230 §3.2.4 forbids CR/LF in field values).
/// The common case (no CR/LF) returns the original slice with zero copy; a value
/// containing CR/LF is compacted into `buf` (mirrors the CORS-config guard, but
/// strips rather than rejecting since this runs per-response). Over-long values
/// are truncated to `buf`.
fn sanitizeHeaderValue(value: []const u8, buf: []u8) []const u8 {
    if (std.mem.indexOfAny(u8, value, "\r\n") == null) return value;
    var n: usize = 0;
    for (value) |ch| {
        if (ch == '\r' or ch == '\n') continue;
        if (n >= buf.len) break;
        buf[n] = ch;
        n += 1;
    }
    return buf[0..n];
}

/// Guard a PRE-FORMATTED header block ("\r\nKey: Value\r\n...") against response
/// splitting: a blank line ("\r\n\r\n") embedded in it would prematurely
/// terminate the headers and inject an attacker-controlled body. Legit blocks are
/// runs of "\r\nKey: Value" and never contain a blank line, so truncate at the
/// first one. (Individual values from Django are already newline-rejected at the
/// source via BadHeaderError; this is defense in depth for every full-header path.)
fn sanitizeHeaderBlock(headers: []const u8) []const u8 {
    if (std.mem.indexOf(u8, headers, "\r\n\r\n")) |i| return headers[0..i];
    return headers;
}

inline fn reqArenaReset() void {
    if (req_arena) |*a| {
        // Bounded retain: reclaim the request's transient allocations but cap the
        // capacity a worker holds between requests, so a single huge request can't
        // pin an oversized arena for the worker's whole life. 64 KB comfortably
        // covers a normal header list; anything larger is released.
        _ = a.reset(.{ .retain_with_limit = 64 * 1024 });
    } else {
        req_arena = std.heap.ArenaAllocator.init(allocator);
    }
}

inline fn reqAllocator() std.mem.Allocator {
    return (req_arena orelse blk: {
        req_arena = std.heap.ArenaAllocator.init(allocator);
        break :blk req_arena.?;
    }).allocator();
}

/// Fold the per-kind registration maps into the single dispatch map. Called from
/// server_run after every route is registered and before any worker starts, so
/// the pointers into the (now-frozen) source maps stay valid for the serving
/// lifetime. Python entries are inserted first, then model schemas are merged in
/// so the body path gets its schema pointer without a second probe.
fn buildDispatchMap() void {
    if (dispatch_map == null) dispatch_map = std.StringHashMap(DispatchEntry).init(allocator);
    const dm = &dispatch_map.?;

    // Insert in REVERSE of the old sequential-probe priority (static > file >
    // native > db > python) so that if a key ever appeared in two kind-maps the
    // higher-priority kind still wins the single lookup — exactly preserving the
    // old first-match-wins behaviour. (In practice each route registers into
    // exactly one kind-map, so no key collides.)
    {
        var it = getRoutes().iterator();
        while (it.next()) |kv| dm.put(kv.key_ptr.*, .{ .python = .{ .entry = kv.value_ptr } }) catch {};
    }
    {
        var it = db.getDbRoutes().iterator();
        while (it.next()) |kv| dm.put(kv.key_ptr.*, .{ .db = kv.value_ptr }) catch {};
    }
    {
        var it = getNativeRoutes().iterator();
        while (it.next()) |kv| dm.put(kv.key_ptr.*, .{ .native = kv.value_ptr }) catch {};
    }
    {
        var it = getFileRoutes().iterator();
        while (it.next()) |kv| dm.put(kv.key_ptr.*, .{ .file = kv.value_ptr }) catch {};
    }
    {
        var it = getStaticRoutes().iterator();
        while (it.next()) |kv| dm.put(kv.key_ptr.*, .{ .static = kv.value_ptr }) catch {};
    }
    {
        // Merge model schemas into the python entries registered above. A key
        // overwritten by a higher-priority kind is skipped (no python entry),
        // matching the old behaviour where the higher-priority path never
        // consulted the schema.
        var it = getModelSchemas().iterator();
        while (it.next()) |kv| {
            if (dm.getPtr(kv.key_ptr.*)) |de| {
                if (de.* == .python) de.python.schema = kv.value_ptr;
            }
        }
    }

    // Part 3: stamp each trie node's embedded dispatch slot with the integer
    // value of its resolved *DispatchEntry. Runs once here (single-threaded,
    // before any worker) so findRouteInto surfaces the entry pointer directly
    // and the hot path skips the second `dispatch_map.get(handler_key)` probe.
    // The pointers stay valid because dispatch_map is frozen after this function.
    getRouter().forEachHandler(stampDispatchSlot);
}

/// forEachHandler callback: resolve `key` in the frozen dispatch map and write
/// the entry pointer's integer value into the trie node's slot (0 stays 0 for a
/// key absent from the map — the hot path then falls back to a map probe).
fn stampDispatchSlot(key: []const u8, slot: *usize) void {
    if (dispatch_map) |*dm| {
        if (dm.getPtr(key)) |entry| slot.* = @intFromPtr(entry);
    }
}

// ── server_new(host, port) -> state dict ────────────────────────────────────

pub fn server_new(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var host: [*c]const u8 = "127.0.0.1";
    var port: c_long = 8000;

    if (args) |a| {
        const n = c.PyTuple_Size(a);
        if (n >= 1) {
            const h = c.PyTuple_GetItem(a, 0);
            if (h) |item| {
                if (c.PyUnicode_Check(item) != 0) {
                    host = c.PyUnicode_AsUTF8(item) orelse "127.0.0.1";
                }
            }
        }
        if (n >= 2) {
            const p = c.PyTuple_GetItem(a, 1);
            if (p) |item| {
                if (c.PyLong_Check(item) != 0) {
                    port = c.PyLong_AsLong(item);
                }
            }
        }
        if (n >= 3) {
            const mbs = c.PyTuple_GetItem(a, 2);
            if (mbs) |item| {
                if (c.PyLong_Check(item) != 0) {
                    const val = c.PyLong_AsLong(item);
                    if (val > 0) {
                        server_max_body_size = @intCast(val);
                    }
                }
            }
        }
    }

    // Validate port range before truncating to u16
    if (port < 1 or port > 65535) {
        py.setError("port must be in range 1-65535, got {d}", .{port});
        return null;
    }

    // Dupe the host string — the Python string's internal buffer may be freed
    // by the GC once the Python object is collected.
    server_host = allocator.dupe(u8, std.mem.span(host)) catch "127.0.0.1";
    server_port = @intCast(port);

    // Eagerly initialize all globals — workers must never hit the lazy-init
    // path, which has a check-then-act race condition.
    _ = getRoutes();
    _ = getNativeRoutes();
    _ = getStaticRoutes();
    _ = getModelSchemas();
    _ = getRouter();
    // Return a state dict
    const d = c.PyDict_New() orelse return null;
    const h_obj = c.PyUnicode_FromString(host) orelse return null;
    _ = c.PyDict_SetItemString(d, "host", h_obj);
    c.Py_DecRef(h_obj);
    const p_obj = c.PyLong_FromLong(@intCast(port)) orelse return null;
    _ = c.PyDict_SetItemString(d, "port", p_obj);
    c.Py_DecRef(p_obj);
    return d;
}

// ── add_route(method, path, handler) ────────────────────────────────────────

pub fn server_add_route(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var handler: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "ssO", &method, &path, &handler) == 0) return null;

    c.Py_IncRef(handler.?);
    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);
    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch return null;
    getRoutes().put(key, .{
        .handler = handler.?,
        .handler_type = "enhanced",
        .handler_tag = .enhanced,
        .param_types_json = "{}",
        .original_handler = null,
        .model_param_name = null,
        .model_class = null,
    }) catch return null;
    getRouter().addRoute(method_s, path_s, key) catch return null;

    return py.pyNone();
}

// ── add_route_typed(method, path, handler, param_types_json) ────────────────
// Enhanced handler with typed path parameters. Zig creates typed Python objects
// (PyLong, PyFloat, PyBool) directly in the path_params dict instead of strings.
// Eliminates the Python-side string→type conversion round-trip.

pub fn server_add_route_typed(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var handler: ?*c.PyObject = null;
    var ptj: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ssOs", &method, &path, &handler, &ptj) == 0) return null;

    c.Py_IncRef(handler.?);
    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);

    // Dupe param_types_json — Python string buffer may be collected.
    const ptj_s = allocator.dupe(u8, std.mem.span(ptj)) catch return null;
    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch {
        allocator.free(ptj_s);
        return null;
    };

    var entry = HandlerEntry{
        .handler = handler.?,
        .handler_type = "enhanced",
        .handler_tag = .enhanced,
        .param_types_json = ptj_s,
        .original_handler = null,
        .model_param_name = null,
        .model_class = null,
    };

    // Parse "name:type|..." metadata into ordered ParamMeta array.
    if (ptj_s.len > 0) {
        entry.param_count = parseParamMeta(ptj_s, &entry.param_meta);
    }

    getRoutes().put(key, entry) catch return null;
    getRouter().addRoute(method_s, path_s, key) catch return null;

    return py.pyNone();
}

// ── add_route_fast(method, path, handler, handler_type, param_types_json, original) ──

pub fn server_add_route_fast(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var handler: ?*c.PyObject = null;
    var ht: [*c]const u8 = null;
    var ptj: [*c]const u8 = null;
    var orig: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "ssOssO", &method, &path, &handler, &ht, &ptj, &orig) == 0) return null;

    c.Py_IncRef(handler.?);
    c.Py_IncRef(orig.?);
    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);

    // Dupe handler_type and param_types_json — the Python string's internal buffer
    // becomes a dangling pointer once the Python object is collected.
    const ht_s = allocator.dupe(u8, std.mem.span(ht)) catch return null;
    const ptj_s = allocator.dupe(u8, std.mem.span(ptj)) catch {
        allocator.free(ht_s);
        return null;
    };
    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch {
        allocator.free(ht_s);
        allocator.free(ptj_s);
        return null;
    };

    // For simple_sync: parse "name:type|..." metadata into ordered ParamMeta array.
    // Slices in param_meta point into ptj_s which we own.
    var entry = HandlerEntry{
        .handler = handler.?,
        .handler_type = ht_s,
        .handler_tag = parseHandlerType(ht_s),
        .param_types_json = ptj_s,
        .original_handler = orig,
        .model_param_name = null,
        .model_class = null,
    };

    if (std.mem.eql(u8, ht_s, "simple_sync")) {
        entry.param_count = parseParamMeta(ptj_s, &entry.param_meta);
    }

    getRoutes().put(key, entry) catch return null;
    getRouter().addRoute(method_s, path_s, key) catch return null;

    return py.pyNone();
}

// ── add_route_model(method, path, handler, param_name, model_class, original) ──

pub fn server_add_route_model(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var handler: ?*c.PyObject = null;
    var param_name: [*c]const u8 = null;
    var model_class: ?*c.PyObject = null;
    var orig: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "ssOsOO", &method, &path, &handler, &param_name, &model_class, &orig) == 0) return null;

    c.Py_IncRef(handler.?);
    c.Py_IncRef(model_class.?);
    c.Py_IncRef(orig.?);
    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);
    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch return null;
    getRoutes().put(key, .{
        .handler = handler.?,
        .handler_type = "model_sync",
        .handler_tag = .model_sync,
        .param_types_json = "{}",
        .original_handler = orig,
        .model_param_name = std.mem.span(param_name),
        .model_class = model_class,
    }) catch return null;
    getRouter().addRoute(method_s, path_s, key) catch return null;

    return py.pyNone();
}

// ── add_route_model_validated(method, path, handler, param_name, model_class, original, schema_json) ──
// Like add_route_model but also registers a JSON schema for Zig-native validation

pub fn server_add_route_model_validated(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var handler: ?*c.PyObject = null;
    var param_name: [*c]const u8 = null;
    var model_class: ?*c.PyObject = null;
    var orig: ?*c.PyObject = null;
    var schema_json: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ssOsOOs", &method, &path, &handler, &param_name, &model_class, &orig, &schema_json) == 0) return null;

    c.Py_IncRef(handler.?);
    c.Py_IncRef(model_class.?);
    c.Py_IncRef(orig.?);
    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);
    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch return null;
    getRoutes().put(key, .{
        .handler = handler.?,
        .handler_type = "model_sync",
        .handler_tag = .model_sync,
        .param_types_json = "{}",
        .original_handler = orig,
        .model_param_name = std.mem.span(param_name),
        .model_class = model_class,
    }) catch return null;
    getRouter().addRoute(method_s, path_s, key) catch return null;

    // Parse and register the schema for Zig-native validation
    const schema_s = std.mem.span(schema_json);
    if (dhi.parseSchema(schema_s)) |schema| {
        getModelSchemas().put(key, schema) catch {};
        std.debug.print("[DHI] Registered schema for {s}: {d} fields\n", .{ key, schema.fields.len });
    }

    return py.pyNone();
}

// ── add_route_async_fast(method, path, handler, handler_type, param_types_json, original) ──

pub fn server_add_route_async_fast(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    // Same signature as add_route_fast
    return server_add_route_fast(null, args);
}

// ── add_native_route(method, path, lib_path, symbol_name) ───────────────────

pub fn server_add_native_route(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var lib_path: [*c]const u8 = null;
    var symbol_name: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ssss", &method, &path, &lib_path, &symbol_name) == 0) return null;

    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);
    const lib_path_s = std.mem.span(lib_path);
    const symbol_name_s = std.mem.span(symbol_name);

    // dlopen the shared library
    const lib_path_z = allocator.dupeZ(u8, lib_path_s) catch {
        py.setError("OOM for lib path", .{});
        return null;
    };
    defer allocator.free(lib_path_z);

    const handle = std.c.dlopen(lib_path_z, .{}) orelse {
        py.setError("dlopen failed for {s}", .{lib_path_s});
        return null;
    };

    // Try to call turboapi_init if it exists
    const init_sym = std.c.dlsym(handle, "turboapi_init");
    if (init_sym) |sym| {
        const init_fn: NativeInitFn = @ptrCast(@alignCast(sym));
        const rc = init_fn();
        if (rc != 0) {
            py.setError("turboapi_init returned {d}", .{rc});
            _ = std.c.dlclose(handle);
            return null;
        }
    }

    // Resolve the handler symbol
    const sym_z = allocator.dupeZ(u8, symbol_name_s) catch {
        py.setError("OOM for symbol name", .{});
        _ = std.c.dlclose(handle);
        return null;
    };
    defer allocator.free(sym_z);

    const handler_sym = std.c.dlsym(handle, sym_z) orelse {
        py.setError("dlsym failed for {s} in {s}", .{ symbol_name_s, lib_path_s });
        _ = std.c.dlclose(handle);
        return null;
    };
    const handler_fn: NativeHandlerFn = @ptrCast(@alignCast(handler_sym));

    // Register in router + native_routes
    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch {
        _ = std.c.dlclose(handle);
        return null;
    };
    getNativeRoutes().put(key, .{
        .handler_fn = handler_fn,
        .lib_handle = handle,
    }) catch {
        _ = std.c.dlclose(handle);
        return null;
    };
    getRouter().addRoute(method_s, path_s, key) catch {
        _ = std.c.dlclose(handle);
        return null;
    };

    std.debug.print("[FFI] Registered native handler: {s} {s} -> {s}:{s}\n", .{ method_s, path_s, lib_path_s, symbol_name_s });
    return py.pyNone();
}

// ── add_static_route(method, path, status, content_type, body) ──────────────
// Pre-renders the complete HTTP response at registration time.
// At dispatch time: single writeAll, zero parsing, zero allocation.

pub fn server_add_static_route(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var path: [*c]const u8 = null;
    var status: c_int = 200;
    var content_type: [*c]const u8 = null;
    var body: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ssiss", &method, &path, &status, &content_type, &body) == 0) return null;

    const method_s = std.mem.span(method);
    const path_s = std.mem.span(path);
    const ct_s = std.mem.span(content_type);
    const body_s = std.mem.span(body);
    const st: u16 = if (status >= 100 and status <= 599) @intCast(status) else 200;

    const status_text = statusText(st);
    // Head only — Date is spliced in fresh at send time (see the static dispatch
    // arm), so it is deliberately NOT part of the pre-rendered bytes.
    // Connection is intentionally NOT baked in — it is spliced per-send along
    // with Date (see the static dispatch arm) so a `Connection: close` request
    // gets the correct token on this shared, pre-rendered head.
    const head = std.fmt.allocPrint(
        allocator,
        "HTTP/1.1 {d} {s}\r\nContent-Type: {s}\r\nContent-Length: {d}",
        .{ st, status_text, ct_s, body_s.len },
    ) catch return null;
    const body_owned = allocator.dupe(u8, body_s) catch {
        allocator.free(head);
        return null;
    };

    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch {
        allocator.free(head);
        allocator.free(body_owned);
        return null;
    };

    getStaticRoutes().put(key, .{ .head = head, .body = body_owned }) catch {
        allocator.free(head);
        allocator.free(body_owned);
        return null;
    };
    getRouter().addRoute(method_s, path_s, key) catch return null;

    std.debug.print("[STATIC] Registered: {s} {s} -> {d} ({d} body bytes pre-rendered)\n", .{ method_s, path_s, st, body_owned.len });
    return py.pyNone();
}

// ── Zig-native CORS — zero per-request overhead ─────────────────────────────
// CORS headers are pre-rendered once at configure_cors() time.  sendResponse
// injects them via a single memcpy into the stack buffer.  OPTIONS preflight
// is handled in handleOneRequest before touching Python.

var cors_headers: []const u8 = ""; // "" = disabled; otherwise pre-rendered CORS header block
// Pre-rendered security-header block ("\r\nX-Frame-Options: ...\r\n...") applied
// to FRAMEWORK-GENERATED responses (sendResponse: 404/400/500/503/preflight) so
// they carry the same X-Frame-Options/nosniff/HSTS/CSP the Python
// SecurityHeadersMiddleware sets on ROUTED responses. Routed responses use a
// separate writer + the middleware, so this never double-applies. "" = unset.
var security_headers: []const u8 = "";
var cors_enabled: bool = false;

/// Serve a file with zero-copy transfer and Range request support.
/// Called from the request handling path when a file route is matched.
/// Uses kernel sendfile on Linux, chunked read+write on macOS.
pub fn serveFile(stream: py.NetStream, file_path: []const u8, content_type: []const u8, range_header: ?[]const u8) void {
    const file = py.NativeFile.open(file_path) catch {
        sendResponse(stream, 404, "text/plain", "File not found");
        return;
    };
    defer file.close();

    const file_size: u64 = file.size() catch {
        sendResponse(stream, 500, "text/plain", "Cannot stat file");
        return;
    };

    // Parse a single HTTP byte range (RFC 7233 §2.1) — mirrors the Python
    // reference `staticfiles._parse_byte_range`. Three forms are accepted:
    //   bytes=A-B  → bytes A..B          bytes=A-  → bytes A..EOF
    //   bytes=-N   → the final N bytes (start computed from the END, not 0)
    // Three outcomes, distinguished so we never mislabel bytes:
    //   .full  — no Range, a malformed/unparseable spec, or a multi-range
    //            request we decline → serve the whole file as 200.
    //   .range — a valid, satisfiable range → 206 slice.
    //   .unsat — a syntactically valid but unsatisfiable range (start past
    //            EOF, start>end, or a `-0` suffix) → 416 with no body.
    // `file_size - 1` underflows for a 0-byte file (safety-panic in Debug,
    // wrap in ReleaseFast); Range handling is meaningless for an empty file,
    // so a 0-byte file is always .full (served as an empty 200).
    const RangeParse = struct { start: u64, end: u64, kind: enum { full, range, unsat } };
    const parsed: RangeParse = blk: {
        const full: RangeParse = .{ .start = 0, .end = if (file_size > 0) file_size - 1 else 0, .kind = .full };
        if (file_size == 0) break :blk full;
        const rh = range_header orelse break :blk full;
        if (!std.mem.startsWith(u8, rh, "bytes=")) break :blk full;
        const spec = std.mem.trim(u8, rh[6..], " ");
        // Multi-range ("," present) or a spec with no dash → decline (full body).
        if (std.mem.indexOfScalar(u8, spec, ',') != null) break :blk full;
        const dash = std.mem.indexOfScalar(u8, spec, '-') orelse break :blk full;
        const start_str = spec[0..dash];
        const end_str = spec[dash + 1 ..];

        var s: u64 = 0;
        var e: u64 = file_size - 1;
        if (start_str.len == 0) {
            // Suffix range: the final N bytes. An empty or unparseable N is a
            // malformed spec → ignore (full body). `-0` is valid-but-unsatisfiable.
            const n = std.fmt.parseInt(u64, end_str, 10) catch break :blk full;
            if (n == 0) break :blk .{ .start = 0, .end = 0, .kind = .unsat };
            s = if (n >= file_size) 0 else file_size - n; // clamp to start-of-file
            e = file_size - 1;
        } else {
            // `bytes=A-` / `bytes=A-B`. An unparseable A or B is malformed → full body.
            s = std.fmt.parseInt(u64, start_str, 10) catch break :blk full;
            if (end_str.len == 0) {
                e = file_size - 1;
            } else {
                const pe = std.fmt.parseInt(u64, end_str, 10) catch break :blk full;
                // An end past EOF clamps to the last byte and still 206 (RFC 7233).
                e = if (pe >= file_size) file_size - 1 else pe;
            }
        }
        // Unsatisfiable: start beyond EOF, or start>end after clamping → 416.
        if (s >= file_size or s > e) break :blk .{ .start = 0, .end = 0, .kind = .unsat };
        break :blk .{ .start = s, .end = e, .kind = .range };
    };

    const range_start: u64 = parsed.start;
    const range_end: u64 = parsed.end;
    const is_range = parsed.kind == .range;
    const is_unsat = parsed.kind == .unsat;

    const content_length = if (is_range) range_end - range_start + 1 else file_size;
    const status: u16 = if (is_range) 206 else 200;

    // Reject CR/LF in the content_type (header injection defense in depth).
    var ct_buf: [256]u8 = undefined;
    const safe_ct = sanitizeHeaderValue(content_type, &ct_buf);

    // Server + Date on every branch — serveFile was the only writer that omitted
    // them, diverging from every other native response (dual-path drift).
    var sf_date: [HTTP_DATE_LEN]u8 = undefined;
    httpDate(&sf_date);

    // A valid-but-unsatisfiable range: 416 with `Content-Range: bytes */<size>`
    // and no body (RFC 7233 §4.4). `nosniff` mirrors the Python 416 branch.
    // Returns here — no seek, no body transfer.
    if (is_unsat) {
        var un_buf: [512]u8 = undefined;
        const uh = std.fmt.bufPrint(
            &un_buf,
            "HTTP/1.1 416 Range Not Satisfiable\r\n" ++
                "Server: HyperDjango\r\nDate: {s}\r\n" ++
                "Content-Type: {s}\r\n" ++
                "Content-Length: 0\r\n" ++
                "Content-Range: bytes */{d}\r\n" ++
                "Accept-Ranges: bytes\r\n" ++
                "X-Content-Type-Options: nosniff\r\n" ++
                "Connection: {s}\r\n\r\n",
            .{ sf_date[0..], safe_ct, file_size, connectionHeaderValue() },
        ) catch return;
        stream.writeAll(uh) catch noteWriteFailure();
        return;
    }

    // Build response headers
    var header_buf: [512]u8 = undefined;
    var header_len: usize = 0;

    if (is_range) {
        const h = std.fmt.bufPrint(
            &header_buf,
            "HTTP/1.1 206 Partial Content\r\n" ++
                "Server: HyperDjango\r\nDate: {s}\r\n" ++
                "Content-Type: {s}\r\n" ++
                "Content-Length: {d}\r\n" ++
                "Content-Range: bytes {d}-{d}/{d}\r\n" ++
                "Accept-Ranges: bytes\r\n" ++
                "X-Content-Type-Options: nosniff\r\n" ++
                "Connection: {s}\r\n\r\n",
            .{ sf_date[0..], safe_ct, content_length, range_start, range_end, file_size, connectionHeaderValue() },
        ) catch return;
        header_len = h.len;
    } else {
        const h = std.fmt.bufPrint(
            &header_buf,
            "HTTP/1.1 200 OK\r\n" ++
                "Server: HyperDjango\r\nDate: {s}\r\n" ++
                "Content-Type: {s}\r\n" ++
                "Content-Length: {d}\r\n" ++
                "Accept-Ranges: bytes\r\n" ++
                "Cache-Control: public, max-age=86400\r\n" ++
                "X-Content-Type-Options: nosniff\r\n" ++
                "Connection: {s}\r\n\r\n",
            .{ sf_date[0..], safe_ct, content_length, connectionHeaderValue() },
        ) catch return;
        header_len = h.len;
    }

    // Send headers. Any write failure here (or below, mid-body) leaves the
    // response short of the Content-Length we already committed to, so the
    // connection is desynced — flag _write_failed so the request loop closes it
    // instead of re-arming keep-alive and pinning the worker to the idle timeout.
    stream.writeAll(header_buf[0..header_len]) catch {
        noteWriteFailure();
        return;
    };

    // HEAD: the headers above (with the full Content-Length a GET would produce)
    // are the complete response — never send the file body (RFC 7230 §3.3.3).
    if (_req_is_head) return;

    // Seek to range start. A seek failure after headers were sent also desyncs
    // the stream (headers promised a body we can't deliver) → force-close.
    if (range_start > 0) {
        file.seekTo(range_start) catch {
            noteWriteFailure();
            return;
        };
    }

    // Zero-copy file transfer
    if (builtin.os.tag == .linux) {
        // Linux: use sendfile(2) — true zero-copy kernel→socket. Bind to libc
        // directly because std.posix.sendfile was removed in Zig 0.16.
        const c_sendfile = struct {
            extern fn sendfile(out_fd: c_int, in_fd: c_int, offset: ?*i64, count: usize) isize;
        }.sendfile;
        var offset: i64 = @intCast(range_start);
        var remaining: u64 = content_length;
        while (remaining > 0) {
            const chunk = @min(remaining, 1024 * 1024); // 1MB chunks
            const sent = c_sendfile(stream.handle, file.fd, &offset, chunk);
            if (sent <= 0) {
                // sent < 0 is a socket error; sent == 0 before draining the full
                // Content-Length is a truncated body. Either way the stream is
                // desynced — close rather than keep-alive.
                noteWriteFailure();
                break;
            }
            remaining -= @intCast(sent);
        }
    } else {
        // macOS / other: chunked read + write (64KB chunks for good throughput)
        var buf: [65536]u8 = undefined;
        var remaining: u64 = content_length;
        while (remaining > 0) {
            const to_read = @min(remaining, buf.len);
            const n = file.read(buf[0..to_read]) catch {
                noteWriteFailure();
                break;
            };
            if (n == 0) {
                noteWriteFailure();
                break;
            }
            stream.writeAll(buf[0..n]) catch {
                noteWriteFailure();
                break;
            };
            remaining -= n;
        }
    }
    _ = status;
}

// File route registry: url_path → (file_path, content_type)
const FileRouteEntry = struct {
    file_path: []const u8,
    content_type: []const u8,
};
var file_routes: ?std.StringHashMap(FileRouteEntry) = null;

fn getFileRoutes() *std.StringHashMap(FileRouteEntry) {
    if (file_routes) |*fr| return fr;
    file_routes = std.StringHashMap(FileRouteEntry).init(allocator);
    return &file_routes.?;
}

/// _server_add_file_route(method, url_path, file_path, content_type)
/// Register a file to be served with zero-copy transfer + Range support.
pub fn server_add_file_route(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method: [*c]const u8 = null;
    var url_path_c: [*c]const u8 = null;
    var file_path_c: [*c]const u8 = null;
    var content_type_c: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ssss", &method, &url_path_c, &file_path_c, &content_type_c) == 0) return null;

    const method_s = std.mem.span(method);
    const url_s = allocator.dupe(u8, std.mem.span(url_path_c)) catch return null;
    const fp = allocator.dupe(u8, std.mem.span(file_path_c)) catch return null;
    const ct = allocator.dupe(u8, std.mem.span(content_type_c)) catch return null;

    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, url_s }) catch return null;

    getFileRoutes().put(key, .{ .file_path = fp, .content_type = ct }) catch return null;

    // Register in router
    const rt = getRouter();
    rt.addRoute(method_s, url_s, key) catch return null;

    return py.pyNone();
}

pub fn server_configure_cors(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var origins: [*c]const u8 = "*";
    var methods: [*c]const u8 = "GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD";
    var hdrs: [*c]const u8 = "*";
    var max_age: c_int = 600;
    var credentials: c_int = 0;
    if (c.PyArg_ParseTuple(args, "|sssii", &origins, &methods, &hdrs, &max_age, &credentials) == 0) return null;

    const origins_s = std.mem.span(origins);
    const methods_s = std.mem.span(methods);
    const hdrs_s = std.mem.span(hdrs);

    // Reject CRLF in CORS values — prevents header injection
    for ([_][]const u8{ origins_s, methods_s, hdrs_s }) |val| {
        if (std.mem.indexOfAny(u8, val, "\r\n") != null) {
            py.setError("CORS values must not contain CR or LF", .{});
            return null;
        }
    }

    // Pre-render the CORS header block (injected into every response)
    const cred_hdr: []const u8 = if (credentials != 0) "\r\nAccess-Control-Allow-Credentials: true" else "";
    var age_buf: [16]u8 = undefined;
    const age_str = std.fmt.bufPrint(&age_buf, "{d}", .{max_age}) catch "600";

    cors_headers = std.fmt.allocPrint(
        allocator,
        "\r\nAccess-Control-Allow-Origin: {s}" ++
            "\r\nAccess-Control-Allow-Methods: {s}" ++
            "\r\nAccess-Control-Allow-Headers: {s}" ++
            "{s}" ++
            "\r\nAccess-Control-Max-Age: {s}",
        .{ origins_s, methods_s, hdrs_s, cred_hdr, age_str },
    ) catch return null;
    cors_enabled = true;

    std.debug.print("[CORS] Zig-native CORS enabled: origin={s} methods={s}\n", .{ origins_s, methods_s });
    return py.pyNone();
}

/// Configure the security-header block spliced into framework-generated
/// (short-circuit) responses. `block` is a pre-rendered "\r\nKey: Value..."
/// string built by the Python SecurityHeadersMiddleware. Guarded against a
/// blank line (which would terminate the header section early / inject a body).
pub fn server_configure_security_headers(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var block: [*c]const u8 = "";
    if (c.PyArg_ParseTuple(args, "s", &block) == 0) return null;
    const block_s = std.mem.span(block);
    if (std.mem.indexOf(u8, block_s, "\r\n\r\n") != null) {
        py.setError("security-header block must not contain a blank line", .{});
        return null;
    }
    // Own a stable copy (the Python str may be freed after the call returns).
    security_headers = allocator.dupe(u8, block_s) catch return null;
    return py.pyNone();
}

// ── add_middleware(middleware_obj) – currently a no-op ──

pub fn server_add_middleware(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return py.pyNone();
}

// ── Response cache for noargs handlers ──────────────────────────────────────
// After the first Python call, the pre-rendered response bytes are cached.
// Subsequent calls serve from cache — zero Python, zero GIL, single writeAll.

pub fn server_enable_response_cache(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    cache_noargs_responses = true;
    std.debug.print("[CACHE] Response caching enabled for noargs handlers\n", .{});
    return py.pyNone();
}

/// Pre-render a full HTTP response into a heap-allocated buffer.
// ── Cached RFC-2822 Date header ──────────────────────────────────────────────
// HTTP Date has 1-second resolution, so computing the full civil-time breakdown
// and bufPrint on every response is pure waste. Cache the formatted 29-byte
// string keyed by epoch second. The fast path is one timestamp() + a seqlock
// read + memcpy. Torn-read-safe under free-threading via a seqlock: readers
// retry when the sequence is odd (writer active) or changes across the copy; a
// mutex serialises the (at most once-per-second) writers.
const HTTP_DATE_LEN = 29; // "Wed, 19 Mar 2026 11:30:27 GMT"
var date_cache_seq = std.atomic.Value(u32).init(0);
var date_cache_epoch: i64 = 0;
var date_cache_buf: [HTTP_DATE_LEN]u8 = "Thu, 01 Jan 2026 00:00:00 GMT".*;
var date_write_mutex: py.Mutex = .{};

fn formatHttpDate(dst: *[HTTP_DATE_LEN]u8, epoch: i64) void {
    const es: std.time.epoch.EpochSeconds = .{ .secs = @intCast(epoch) };
    const ds = es.getDaySeconds();
    const ed = es.getEpochDay();
    const yd = ed.calculateYearDay();
    const md = yd.calculateMonthDay();
    const di: usize = @intCast(@mod(@as(i32, @intCast(ed.day)) + 3, 7)); // 0=Mon
    const dw = [7][]const u8{ "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun" };
    const mn = [12][]const u8{ "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" };
    _ = std.fmt.bufPrint(dst, "{s}, {d:0>2} {s} {d} {d:0>2}:{d:0>2}:{d:0>2} GMT", .{
        dw[di],               md.day_index + 1,        mn[@intFromEnum(md.month) - 1], yd.year,
        ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute(),
    }) catch {};
}

/// Fill `dst` with the current RFC-2822 Date header (exactly 29 bytes).
/// Fast path: one timestamp() + atomic loads + memcpy. Recomputes only when the
/// epoch second advances. Safe under true parallelism (free-threaded builds).
fn httpDate(dst: *[HTTP_DATE_LEN]u8) void {
    const now = py.timestamp();

    // Seqlock read fast path. Bounded: a preempted writer holding the odd seq
    // must not spin readers forever, so after MAX_SEQ_SPINS we fall back to
    // formatting the date locally (correct, just not cache-shared for this call).
    const MAX_SEQ_SPINS: usize = 1024;
    var spins: usize = 0;
    while (true) {
        const s1 = date_cache_seq.load(.acquire);
        if (s1 & 1 != 0) {
            spins += 1;
            if (spins >= MAX_SEQ_SPINS) return formatHttpDate(dst, now); // writer stalled
            std.atomic.spinLoopHint(); // writer in progress
            continue;
        }
        const epoch = @atomicLoad(i64, &date_cache_epoch, .acquire);
        @memcpy(dst, &date_cache_buf);
        const s2 = date_cache_seq.load(.acquire);
        if (s1 != s2) {
            spins += 1;
            if (spins >= MAX_SEQ_SPINS) return formatHttpDate(dst, now); // writer stalled
            std.atomic.spinLoopHint(); // torn read
            continue;
        }
        if (epoch == now) return; // clean read, still current
        break; // clean read but stale → refresh below
    }

    // Slow path (≤ once per second): recompute and publish under the seqlock.
    date_write_mutex.lock();
    defer date_write_mutex.unlock();
    // Re-check: another writer may have refreshed while we waited on the mutex.
    if (@atomicLoad(i64, &date_cache_epoch, .acquire) != now) {
        const seq0 = date_cache_seq.load(.monotonic);
        // Publish the odd (write-in-progress) marker with an acq_rel RMW rather
        // than a plain release store: a release store only orders PRIOR writes
        // before it, which on a weak (ARM) memory model would let the buffer
        // stores below hoist ABOVE the odd marker — a reader could then observe
        // an even-matched seq over a torn buffer. The acq_rel RMW is a full
        // barrier for this location, so the buffer writes cannot be reordered
        // before the marker becomes visible.
        _ = date_cache_seq.fetchAdd(1, .acq_rel); // → seq0+1, odd
        formatHttpDate(&date_cache_buf, now);
        @atomicStore(i64, &date_cache_epoch, now, .release);
        date_cache_seq.store(seq0 +% 2, .release); // even → done
    }
    // Safe: serialised against other writers by the mutex.
    @memcpy(dst, &date_cache_buf);
}

fn renderResponse(status: u16, content_type: []const u8, body: []const u8) ?[]const u8 {
    const cors = cors_headers;
    var date_buf: [HTTP_DATE_LEN]u8 = undefined;
    httpDate(&date_buf);
    const dt = date_buf[0..];
    return std.fmt.allocPrint(
        allocator,
        "HTTP/1.1 {d} {s}\r\nServer: HyperDjango\r\nDate: {s}\r\nContent-Type: {s}\r\nContent-Length: {d}\r\nConnection: {s}{s}\r\n\r\n{s}",
        .{ status, statusText(status), dt, content_type, body.len, connectionHeaderValue(), cors, body },
    ) catch null;
}

// ── Graceful shutdown signal handler ─────────────────────────────────────────

fn shutdown_signal_handler(_: std.c.SIG) callconv(.c) void {
    shutdown_flag.store(true, .release);
    // Write one byte to self-pipe to wake the poll() in the accept loop.
    // This is async-signal-safe (write to a pipe is guaranteed safe).
    _ = std.c.write(shutdown_pipe[1], &[_]u8{1}, 1);
}

fn install_signal_handlers() void {
    // Create self-pipe for waking the accept loop from signal context
    if (std.c.pipe(&shutdown_pipe) != 0) {
        @panic("shutdown pipe creation failed — cannot handle signals safely");
    }

    const action = std.posix.Sigaction{
        .handler = .{ .handler = shutdown_signal_handler },
        .mask = std.posix.sigemptyset(),
        .flags = 0,
    };

    // Handle both SIGTERM (systemd/docker stop) and SIGINT (Ctrl-C)
    // posix-safe: one-time, single-threaded startup install with static valid
    // args; not a socket path, so the unreachable errnos cannot arise here.
    std.posix.sigaction(std.posix.SIG.TERM, &action, null);
    // posix-safe: see above — static signal install, no fd/socket involved.
    std.posix.sigaction(std.posix.SIG.INT, &action, null);
}

/// Python-callable: trigger graceful shutdown programmatically.
pub fn server_shutdown(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    shutdown_flag.store(true, .release);
    _ = std.c.write(shutdown_pipe[1], &[_]u8{1}, 1);
    return py.pyNone();
}

// ── run() – start the HTTP server ──

// ── Thread pool for connection handling ─────────────────────────────────────

// ── Capacity detection & self-scaling defaults ──────────────────────────────
//
// Static defaults ("24 workers, 1 reactor") were tuned on a ~12-core laptop.
// On a big machine they leave it idle (24 workers on 512 cores), and on a
// hand-scaled big machine the single reactor/queue becomes the bottleneck. So
// when the operator has NOT pinned a value, the server sizes itself from the
// machine's usable capacity — and workers and reactors scale TOGETHER, so the
// per-shard work queue never has to serve more than WORKERS_PER_REACTOR
// workers. Any explicit HYPER_* override always wins verbatim (clamped only to
// a sane hard ceiling); the equations only fill the unset case.
const DEFAULT_POOL_SIZE = 24; // floor for auto-sizing — never size BELOW the historically-tuned default
const WORKER_AUTO_CEILING: usize = 512; // auto never exceeds this; override to go higher
const WORKER_HARD_MAX: usize = 4096; // clamp for an explicit override (guards a fat-finger)

/// Usable core count. `std.Thread.getCpuCount` reads sched_getaffinity on
/// Linux, so inside a cpuset/cgroup (a container pinned to 4 of 128 cores) it
/// correctly reports 4 — the server scales to what it may actually use, not
/// the host's raw core count.
fn detectCores() usize {
    return std.Thread.getCpuCount() catch DEFAULT_POOL_SIZE;
}

/// Operator budget knob: how much of the detected machine to use. Unset = all
/// cores. A fraction ("0.5") or percent ("50%") scales the core count; a bare
/// integer ("8") is an absolute core budget. Lets one big box be shared
/// without hand-sizing every pool.
fn cpuBudget() usize {
    const cores = detectCores();
    const env_ptr = std.c.getenv("HYPER_CPU_BUDGET") orelse return cores;
    const s = std.mem.sliceTo(env_ptr, 0);
    if (s.len == 0) return cores;
    // '%' → percent of cores; '.' → fraction of cores; else absolute count.
    if (s[s.len - 1] == '%') {
        const pct = std.fmt.parseFloat(f64, s[0 .. s.len - 1]) catch return cores;
        return scaleCores(cores, pct / 100.0);
    }
    if (std.mem.indexOfScalar(u8, s, '.') != null) {
        const frac = std.fmt.parseFloat(f64, s) catch return cores;
        return scaleCores(cores, frac);
    }
    const n = std.fmt.parseInt(usize, s, 10) catch return cores;
    return if (n == 0) cores else @min(n, cores);
}

/// Scale `cores` by `frac` (guarded: only finite fractions in (0,1] scale;
/// anything else falls back to all cores). Result floored at 1.
fn scaleCores(cores: usize, frac: f64) usize {
    if (!std.math.isFinite(frac) or frac <= 0 or frac > 1.0) return cores;
    const scaled: usize = @intFromFloat(@ceil(@as(f64, @floatFromInt(cores)) * frac));
    return @max(scaled, 1);
}

/// Auto worker count from the budget: at least the historic default (so small
/// machines are byte-for-byte unchanged), at most WORKER_AUTO_CEILING.
fn autoWorkers(budget: usize) usize {
    return std.math.clamp(budget, DEFAULT_POOL_SIZE, WORKER_AUTO_CEILING);
}

fn getPoolSize() usize {
    if (std.c.getenv("HYPER_THREAD_POOL_SIZE")) |env_ptr| {
        const env_val = std.mem.sliceTo(env_ptr, 0);
        if (std.fmt.parseInt(usize, env_val, 10)) |parsed| {
            // 0 = "auto" opt-in; any positive value is an explicit override.
            if (parsed != 0) return @min(parsed, WORKER_HARD_MAX);
        } else |_| {}
    }
    return autoWorkers(cpuBudget());
}

// Zig's default worker thread stack is 16 MB (std.Thread.SpawnConfig).
// That's a safe, conservative default for arbitrary deep call chains
// (nested ORM queries, template includes, serializer recursion) — left
// unchanged unless explicitly overridden. Each pool thread reserves this
// much address space (only touched pages become resident), and with
// long-lived WebSocket connections tying up one thread each, that's a
// real, tunable trade-off: a workload of many lightweight handlers (e.g.
// a pure echo/proxy endpoint with shallow call depth) can safely run a
// smaller stack per thread and reclaim memory; a workload with deep
// synchronous call chains should keep the default. Set via
// HYPER_THREAD_STACK_SIZE (bytes) — mirrors HYPER_THREAD_POOL_SIZE.
const DEFAULT_STACK_SIZE: usize = std.Thread.SpawnConfig.default_stack_size;
const MIN_STACK_SIZE: usize = 256 * 1024; // floor — below this risks overflow for any real Python call chain

fn getStackSize() usize {
    const env_ptr = std.c.getenv("HYPER_THREAD_STACK_SIZE") orelse return DEFAULT_STACK_SIZE;
    const env_val = std.mem.sliceTo(env_ptr, 0);
    const parsed = std.fmt.parseInt(usize, env_val, 10) catch return DEFAULT_STACK_SIZE;
    if (parsed == 0) return DEFAULT_STACK_SIZE;
    return @max(parsed, MIN_STACK_SIZE);
}

const ConnectionPool = struct {
    queue: Queue,
    threads: []std.Thread = &.{},
    thread_count: usize = 0,

    /// Growable ring buffer for accepted connections.
    /// Starts at 4096 capacity, doubles when full instead of dropping connections.
    const Queue = struct {
        items: []py.NetStream,
        capacity: usize,
        head: usize = 0,
        tail: usize = 0,
        count: usize = 0,
        mutex: py.Mutex = .{},
        not_empty: py.Condition = .{},
        // Workers currently parked in pop() on `not_empty`. Maintained under
        // `mutex`, so pushBatch can wake EXACTLY the number of sleeping workers
        // (never more) — zero futex wakes when all workers are busy.
        waiters: usize = 0,
        fn init() Queue {
            const initial_cap: usize = 4096;
            const items = allocator.alloc(py.NetStream, initial_cap) catch @panic("OOM: connection queue");
            return .{ .items = items, .capacity = initial_cap };
        }

        /// Enqueue a connection. Returns false if the queue was full AND could
        /// not grow (OOM) — in that case the stream is closed here and the caller
        /// must clean up any per-fd state it staged (e.g. a carry stash) so a
        /// later accept() reusing the fd number can't inherit it.
        fn push(self: *Queue, stream: py.NetStream) bool {
            self.mutex.lock();
            defer self.mutex.unlock();
            if (self.count >= self.capacity) {
                self.grow() catch {
                    // Truly out of memory — drop connection as last resort
                    stream.close();
                    return false;
                };
            }
            self.items[self.tail] = stream;
            self.tail = (self.tail + 1) % self.capacity;
            self.count += 1;
            self.not_empty.signal();
            return true;
        }

        fn pop(self: *Queue) ?py.NetStream {
            self.mutex.lock();
            defer self.mutex.unlock();
            while (self.count == 0) {
                if (shutdown_flag.load(.acquire)) return null;
                self.waiters += 1;
                self.not_empty.wait(&self.mutex);
                self.waiters -= 1;
            }
            const stream = self.items[self.head];
            self.head = (self.head + 1) % self.capacity;
            self.count -= 1;
            return stream;
        }

        /// Current backlog depth (accepted-but-not-yet-picked-up connections).
        /// Used by the accept loop for load-shedding in threaded mode.
        fn depth(self: *Queue) usize {
            self.mutex.lock();
            defer self.mutex.unlock();
            return self.count;
        }

        /// Non-blocking pop — returns null immediately if empty. Used by the
        /// reactor thread, which must not block on this queue (it also waits on
        /// the reactor for socket readiness).
        fn tryPop(self: *Queue) ?py.NetStream {
            self.mutex.lock();
            defer self.mutex.unlock();
            if (self.count == 0) return null;
            const stream = self.items[self.head];
            self.head = (self.head + 1) % self.capacity;
            self.count -= 1;
            return stream;
        }

        /// Push a batch of items under a SINGLE lock and wake up to N waiting
        /// workers. The reactor dispatches a whole kevent batch of readable
        /// connections at once, so this replaces N lock/signal pairs with one —
        /// the work-queue lock was the dominant contention point at high worker
        /// counts (profiled).
        fn pushBatch(self: *Queue, streams: []const py.NetStream) void {
            if (streams.len == 0) return;
            self.mutex.lock();
            defer self.mutex.unlock();
            for (streams) |stream| {
                if (self.count >= self.capacity) {
                    self.grow() catch {
                        stream.close();
                        continue;
                    };
                }
                self.items[self.tail] = stream;
                self.tail = (self.tail + 1) % self.capacity;
                self.count += 1;
            }
            // Wake exactly as many sleeping workers as we have new items, capped
            // by the number actually parked in pop(). Under steady load every
            // worker is busy (`waiters == 0`) so this issues ZERO futex wakes —
            // the per-item signal storm is gone. Never a broadcast: no thundering
            // herd of workers waking only to find nothing.
            var to_wake = @min(streams.len, self.waiters);
            while (to_wake > 0) : (to_wake -= 1) self.not_empty.signal();
        }

        /// Wake all waiting workers so they can check shutdown_flag and exit.
        fn wakeAll(self: *Queue) void {
            self.mutex.lock();
            defer self.mutex.unlock();
            self.not_empty.broadcast();
        }

        fn grow(self: *Queue) !void {
            const new_cap = self.capacity * 2;
            const new_items = try allocator.alloc(py.NetStream, new_cap);
            // Linearize ring buffer into new array
            for (0..self.count) |i| {
                new_items[i] = self.items[(self.head + i) % self.capacity];
            }
            allocator.free(self.items);
            self.items = new_items;
            self.head = 0;
            self.tail = self.count;
            self.capacity = new_cap;
        }
    };

    fn init(self: *ConnectionPool) void {
        self.thread_count = getPoolSize();
        self.queue = Queue.init();
        self.threads = std.heap.page_allocator.alloc(std.Thread, self.thread_count) catch @panic("thread alloc");
        // One in-flight cell per worker (each worker writes only its own),
        // allocated before any worker starts.
        inflight_cells = std.heap.page_allocator.alloc(InflightCell, self.thread_count) catch @panic("inflight cell alloc");
        for (inflight_cells) |*cell| cell.* = .{};
        if (httpServerModel() == .reactor) {
            // Create the reactor groups (+ their reactor threads), then bind each
            // worker to a group's work queue (round-robin), so a group's reactor
            // feeds only its own workers via its own queue — no shared queue.
            setupReactorShards(self.thread_count);
            for (0..self.thread_count) |i| {
                const wq = &shards[i % shards.len].work;
                self.threads[i] = std.Thread.spawn(.{ .stack_size = getStackSize() }, workerLoopReactor, .{ wq, i }) catch @panic("thread spawn");
            }
        } else {
            for (0..self.thread_count) |i| {
                self.threads[i] = std.Thread.spawn(.{ .stack_size = getStackSize() }, workerLoop, .{ &self.queue, i }) catch @panic("thread spawn");
            }
        }
    }

    // Each worker creates its own PyThreadState once and reuses it for every
    // request. This replaces PyGILState_Ensure/Release (which re-does a
    // thread-state lookup on every call) with the cheaper AcquireThread path.
    fn workerLoop(queue: *Queue, worker_id: usize) void {
        const tstate = py.PyThreadState_New(py_interp) orelse @panic("PyThreadState_New failed");
        defer {
            py.PyEval_AcquireThread(tstate);
            py.PyThreadState_Clear(tstate);
            py.PyThreadState_DeleteCurrent();
        }

        // Lazy init server metrics on first worker start — safe because
        // every worker thread runs this path exactly once (the outer
        // `while !shutdown` is the long-lived loop). The init function
        // has its own `_srv_metrics_initialized` guard so only the
        // first thread does the actual registration.
        initServerMetrics();

        while (!shutdown_flag.load(.acquire)) {
            const stream = queue.pop() orelse break; // null = shutdown
            // In-flight tracked in THIS worker's own cell — no shared atomic on
            // the hot path. The /metrics gauge is reconciled from the summed
            // cells at most once per second (see maybeReconcileInflightGauge).
            inflightAdd(worker_id, 1);
            handleConnection(stream, tstate);
            inflightAdd(worker_id, -1);
            maybeReconcileInflightGauge();
        }
    }
};

var pool: ConnectionPool = undefined;

// ── Connection-dispatch reactor (HTTP_SERVER_MODEL=reactor — the DEFAULT) ────
//
// Removes the "one worker thread per keep-alive connection" ceiling: IDLE
// keep-alive connections wait in a kqueue/epoll reactor (zero worker threads),
// armed EV_ONESHOT/EPOLLONESHOT; when one becomes readable the kernel disarms
// its filter and the reactor hands the WHOLE connection to a worker for exactly
// ONE request, then the worker re-arms it directly on the shard's poll fd.
// Strict single-owner baton — a disarmed ONESHOT fd is owned by exactly one
// worker until that worker's re-arm call returns, and an armed fd is owned by
// the kernel — so there is no concurrent access to a socket. Registration is a
// thread-safe kevent()/epoll_ctl() issued off the reactor thread (acceptor arms
// new fds, workers re-arm served fds), keeping the reactor thread out of the
// per-request path entirely. The active-request path is the unchanged,
// proven `handleOneRequest`. This is the SAFE default: it holds thousands of
// concurrent connections and degrades gracefully, where the threaded model
// starves connections past THREAD_POOL_SIZE (see the threaded-ceiling note at
// `handleConnection`). 'threaded' is the opt-in max-throughput mode for
// known-bounded connection counts. See docs/design/http-connection-reactor.md.

const HttpModel = enum { threaded, reactor };

fn httpServerModel() HttpModel {
    const env_ptr = std.c.getenv("HYPER_HTTP_SERVER_MODEL") orelse return .threaded;
    const v = std.mem.sliceTo(env_ptr, 0);
    if (std.ascii.eqlIgnoreCase(v, "reactor")) return .reactor;
    return .threaded;
}

// SHARDED reactor GROUPS: R independent (reactor + work-queue + worker subset)
// groups. A connection is pinned to a group by fd (fd % R) for its whole life,
// so within a group there is a single reactor producing to one work queue drained
// by that group's workers — no global shared queue, so dispatch parallelizes
// across groups with no cross-group contention.
//
// Registration is done DIRECTLY on the shard's poll fd via thread-safe
// kevent()/epoll_ctl(): the acceptor arms a new connection (addOneshotRead) and
// each worker re-arms the connection it just served (rearmOneshotRead). No
// register queue, no self-pipe wake per request, and the reactor thread is out
// of the re-arm path entirely. The single-owner-per-fd invariant still holds:
// a ONESHOT fd is disarmed the instant its event fires, so it is owned by the
// one worker draining it until that worker's re-arm call returns.
const Shard = struct {
    reactor: reactor_mod.Reactor,
    // Readable connections dispatched by this group's reactor to its workers.
    work: ConnectionPool.Queue,
    // Carry-over stash for fairness requeues (see the carry-stash section):
    // fd → buffered-bytes copy. Shard-local because a fd is pinned to its
    // shard for the connection's whole life, so no cross-shard lookup can
    // ever be needed — and a single global map would put one mutex on EVERY
    // request dispatch across all workers. `carry_count` mirrors the map's
    // size so the (overwhelmingly common) empty case is a single shared
    // atomic read with no lock and no cache-line invalidation traffic.
    carry_mutex: py.Mutex = .{},
    carry_map: std.AutoHashMapUnmanaged(std.posix.fd_t, []u8) = .{},
    carry_count: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
    // Reactor-thread-only: monotonic ms of the last idle sweep.
    last_sweep_ms: i64 = 0,
};

// Monotonic milliseconds — used for the idle sweep so a wall-clock adjustment
// can't make a connection look spuriously idle (or immortal).
fn nowMonoMs() i64 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
    return @as(i64, @intCast(ts.sec)) * 1000 + @divTrunc(@as(i64, @intCast(ts.nsec)), std.time.ns_per_ms);
}

// ── Idle-reaping registry (lock-free, fd-indexed) ───────────────────────────
// A connection is PARKED when it is armed ONESHOT on its shard's reactor and
// owned by nobody; `parked_at[fd]` then holds the monotonic ms of its last
// activity, and 0 means "not parked" (worker-owned, closed, or never seen). The
// entry is written BEFORE the arm/re-arm — so a connection that becomes readable
// immediately can't be dispatched-then-lost — and cleared the instant the
// reactor dispatches it, which is what keeps the sweep from ever closing an fd a
// worker holds.
//
// This was a per-shard `AutoHashMapUnmanaged` behind a mutex. That mutex sat on
// the ACCEPT path (once per new connection), on every worker re-arm (once per
// request) and on every reactor dispatch batch — one lock shared by the
// acceptor, the reactor thread and every worker in the shard. The acceptor is a
// single thread that must place an entire connection burst before the kernel
// listen queue overflows, so making it queue behind W workers' per-request
// traffic turned a scaling knob into a correctness cliff: overflowed
// connections are dropped by the kernel with no error visible to either side.
//
// fds are small dense integers, so a flat array indexed by fd is both smaller
// and faster than a hash map and needs no lock at all: mark/unmark are single
// atomic stores and the sweep claims an entry with a compare-exchange.
var parked_at: []std.atomic.Value(i64) = &.{};
// One past the highest fd ever parked — bounds the sweep scan so a table sized
// for RLIMIT_NOFILE costs nothing when only a few hundred fds are in use.
var parked_high_water = std.atomic.Value(usize).init(0);

/// Size the fd-indexed parked table from RLIMIT_NOFILE. Called once at reactor
/// setup (startup, single-threaded). An fd beyond the table simply isn't
/// idle-tracked — only reachable if the fd limit is raised after startup.
fn initParkedTable() void {
    var rl: std.c.rlimit = undefined;
    const soft: usize = if (std.c.getrlimit(.NOFILE, &rl) == 0 and rl.cur != std.c.RLIM.INFINITY)
        @intCast(rl.cur)
    else
        1 << 20;
    const n = std.math.clamp(soft, 4096, 1 << 20);
    const table = std.heap.page_allocator.alloc(std.atomic.Value(i64), n) catch @panic("reactor: parked table alloc");
    for (table) |*slot| slot.* = std.atomic.Value(i64).init(0);
    parked_at = table;
    const served = std.heap.page_allocator.alloc(std.atomic.Value(u32), n) catch @panic("reactor: served table alloc");
    for (served) |*slot| slot.* = std.atomic.Value(u32).init(0);
    served_n = served;
}

// Completed responses per fd, reset when the fd is (re-)accepted. Paired with
// `parked_at` it answers the only question a rps/latency number can't: is a
// connection the client considers live actually being SERVED? A connection that
// is parked (armed on the reactor, so the server is waiting on the client) but
// has never completed a response is one the reactor is not delivering readiness
// for — invisible in throughput, invisible as an error, and the exact shape of a
// starved keep-alive set. Summarised into bounded gauges once per sweep tick, so
// nothing per-fd ever reaches the metric registry.
var served_n: []std.atomic.Value(u32) = &.{};

inline fn noteServed(fd: std.posix.fd_t) void {
    const i: usize = @intCast(fd);
    if (i >= served_n.len) return;
    _ = served_n[i].fetchAdd(1, .monotonic);
}

inline fn resetServed(fd: std.posix.fd_t) void {
    const i: usize = @intCast(fd);
    if (i >= served_n.len) return;
    served_n[i].store(0, .monotonic);
}

/// Record `fd` as parked with a fresh activity timestamp. Called before
/// arming/re-arming so the sweep can find it.
fn markParked(fd: std.posix.fd_t) void {
    const i: usize = @intCast(fd);
    if (i >= parked_at.len) return;
    parked_at[i].store(nowMonoMs(), .release);
    var hw = parked_high_water.load(.monotonic);
    while (hw <= i) {
        hw = parked_high_water.cmpxchgWeak(hw, i + 1, .monotonic, .monotonic) orelse break;
    }
}

/// Drop `fd` from the parked registry (arm failed, dispatched, or closed).
fn unmarkParked(fd: std.posix.fd_t) void {
    const i: usize = @intCast(fd);
    if (i >= parked_at.len) return;
    parked_at[i].store(0, .release);
}

// Per-shard census cells. Each reactor thread writes only its own cell during
// its sweep tick; the gauges are published as the sum, so N reactors don't
// overwrite each other's view. Sized to the shard-count ceiling so the cells are
// a fixed array with no allocation and no lock.
const CensusCell = struct {
    parked: std.atomic.Value(i64) = std.atomic.Value(i64).init(0),
    unserved: std.atomic.Value(i64) = std.atomic.Value(i64).init(0),
    queued: std.atomic.Value(i64) = std.atomic.Value(i64).init(0),
    // Pad to a cache line: adjacent cells are written by different reactor
    // threads once per tick, and false sharing here would be free contention.
    _pad: [40]u8 = undefined,
};

var census_cells: [MAX_REACTOR_COUNT]CensusCell = [_]CensusCell{.{}} ** MAX_REACTOR_COUNT;

/// Recompute this shard's slice of the connection census and publish the summed
/// gauges. Runs on the reactor thread once per sweep tick (default 1 s) over the
/// fds this shard owns — the scan is already bounded by the sweep's high-water
/// mark, so this rides along on a walk the sweep does anyway.
fn censusParked(shard_index: usize, nshards: usize, scan_end: usize) void {
    var parked: i64 = 0;
    var unserved: i64 = 0;
    var i: usize = shard_index;
    while (i < scan_end) : (i += nshards) {
        if (parked_at[i].load(.monotonic) == 0) continue;
        parked += 1;
        if (i < served_n.len and served_n[i].load(.monotonic) == 0) unserved += 1;
    }
    const cell = &census_cells[shard_index];
    cell.parked.store(parked, .monotonic);
    cell.unserved.store(unserved, .monotonic);
    cell.queued.store(@intCast(shards[shard_index].work.depth()), .monotonic);

    var tot_parked: i64 = 0;
    var tot_unserved: i64 = 0;
    var tot_queued: i64 = 0;
    for (census_cells[0..nshards]) |*c2| {
        tot_parked += c2.parked.load(.monotonic);
        tot_unserved += c2.unserved.load(.monotonic);
        tot_queued += c2.queued.load(.monotonic);
    }
    if (loadGauge(&_srv_parked_gauge)) |g| g.set(tot_parked);
    if (loadGauge(&_srv_parked_unserved_gauge)) |g| g.set(tot_unserved);
    if (loadGauge(&_srv_queue_depth_gauge)) |g| g.set(tot_queued);
}

/// Close connections parked idle longer than `timeout_ms`. Runs on the reactor
/// thread off its wait() tick, over the fds this shard owns (fd-pinned by
/// `shardFor`). An entry is claimed with a compare-exchange against the exact
/// timestamp that was read, so a connection dispatched or re-armed in the
/// meantime (value changed to 0 or to a newer stamp) is never reaped out from
/// under its owner — strictly tighter than the old lock, which only held across
/// the scan. Bounded per call; any overflow is caught by the next sweep.
fn sweepIdle(shard: *Shard, shard_index: usize, timeout_ms: u64) void {
    if (timeout_ms == 0) return;
    const cutoff = nowMonoMs() - @as(i64, @intCast(timeout_ms));
    const nshards = shards.len;
    const scan_end = @min(parked_high_water.load(.monotonic), parked_at.len);
    var reap: [256]std.posix.fd_t = undefined;
    var rn: usize = 0;
    var i: usize = shard_index;
    while (i < scan_end) : (i += nshards) {
        const stamp = parked_at[i].load(.acquire);
        if (stamp == 0 or stamp > cutoff) continue;
        if (parked_at[i].cmpxchgStrong(stamp, 0, .acq_rel, .monotonic) != null) continue;
        reap[rn] = @intCast(i);
        rn += 1;
        if (rn == reap.len) break;
    }

    censusParked(shard_index, nshards, scan_end);

    for (reap[0..rn]) |fd| {
        // Unregister from the reactor BEFORE close so the fd number can't be
        // reused by a concurrent accept() while a filter still references it.
        shard.reactor.remove(fd);
        (py.NetStream{ .handle = fd }).close();
        // A parked fd never carries a stash (a stash implies queued, not parked),
        // but free defensively so no bytes are ever left owned for this fd number.
        if (takeCarry(fd)) |orphan| allocator.free(orphan);
        _ = idle_reaped_total.fetchAdd(1, .monotonic);
        if (loadCounter(&_srv_idle_reaped_counter)) |ctr| ctr.inc(1);
    }
}

var shards: []Shard = &.{};
var reactor_threads: []std.Thread = &.{};

// Reactor/shard count scales WITH the worker count. Each reactor owns one
// shard = one work queue, and workers bind round-robin across shards, so this
// bounds how many workers convoy on any single queue mutex. One reactor
// dispatches fine for a modest worker set (a single reactor core is not the
// bottleneck there — the historic finding), but once workers exceed
// WORKERS_PER_REACTOR the single queue's lock/cache-line contention is what
// collapses throughput at high core counts. Sharding at exactly that knee
// keeps small machines on the old 1-reactor path (workers ≤ 32 → 1 reactor,
// byte-for-byte unchanged) while a big machine gets one queue per ~32 workers.
// WORKERS_PER_REACTOR sits at the observed single-queue knee and is the
// primary knob the matrix benchmark calibrates.
const MAX_REACTOR_COUNT: usize = 64;
const WORKERS_PER_REACTOR: usize = 32;

/// Auto reactor/shard count for `workers`: one queue per WORKERS_PER_REACTOR
/// workers, at least 1, capped at MAX_REACTOR_COUNT.
fn autoReactors(workers: usize) usize {
    return std.math.clamp(workers / WORKERS_PER_REACTOR, 1, MAX_REACTOR_COUNT);
}

fn getReactorCount(workers: usize) usize {
    if (std.c.getenv("HYPER_HTTP_REACTOR_COUNT")) |e| {
        const v = std.mem.sliceTo(e, 0);
        const p = std.fmt.parseInt(usize, v, 10) catch 0;
        if (p > 0) return @min(p, MAX_REACTOR_COUNT);
    }
    return autoReactors(workers);
}

// ── Load-shedding (threaded mode) ───────────────────────────────────────────
// When all THREAD_POOL_SIZE workers are pinned to keep-alive connections, more
// connections queue unserved. Instead of accept-and-starve (a "fairness collapse"
// where the excess get one response then hang — see the threaded-ceiling note),
// once the accept backlog exceeds this cap we fail new connections FAST with
// 503 Service Unavailable + Connection: close. The client gets a clear
// retry/failover signal instead of a hung request — the threaded mode "expands
// gracefully to capacity, then sheds cleanly" rather than exploding at the margin.
// Cap defaults to THREAD_POOL_SIZE × 8; set HYPER_HTTP_MAX_PENDING=0 to disable
// (unbounded queue), or to a specific backlog size. Reactor mode never sheds.
const DEFAULT_MAX_PENDING_FACTOR: usize = 8;
var shed_total = std.atomic.Value(u64).init(0);

// How many connections the acceptor pulls off the kernel queue before pausing to
// set them up. Large enough that a normal connection storm is drained in one or
// two passes, small enough to stay a stack array and to bound how long a
// freshly-accepted connection waits for its reactor registration.
const ACCEPT_BATCH: usize = 512;

fn getMaxPending(thread_count: usize) usize {
    if (std.c.getenv("HYPER_HTTP_MAX_PENDING")) |e| {
        const v = std.mem.sliceTo(e, 0);
        return std.fmt.parseInt(usize, v, 10) catch (thread_count * DEFAULT_MAX_PENDING_FACTOR);
    }
    return thread_count * DEFAULT_MAX_PENDING_FACTOR;
}

/// Allocate the reactor groups and spawn one reactor thread per group. Called
/// from pool.init in reactor mode (startup — allocation failure panics).
fn setupReactorShards(workers: usize) void {
    const rc = getReactorCount(workers);
    initParkedTable();
    shards = std.heap.page_allocator.alloc(Shard, rc) catch @panic("reactor: shard alloc");
    reactor_threads = std.heap.page_allocator.alloc(std.Thread, rc) catch @panic("reactor: thread alloc");
    for (0..rc) |i| {
        shards[i] = .{
            .reactor = reactor_mod.Reactor.init() catch @panic("reactor: kqueue/epoll init"),
            .work = ConnectionPool.Queue.init(),
        };
    }
    for (0..rc) |i| {
        reactor_threads[i] = std.Thread.spawn(.{ .stack_size = getStackSize() }, reactorLoop, .{i}) catch @panic("reactor: thread spawn");
    }
}

/// Return the shard that owns `fd` (fd-pinned for the connection's whole life).
inline fn shardFor(fd: std.posix.fd_t) *Shard {
    return &shards[@as(usize, @intCast(fd)) % shards.len];
}

// Bound how long a blocking writev to a client may stall a worker (or, on the
// shed path, the accept thread). Without SO_SNDTIMEO a zero-window / slow-reading
// client can pin the writing thread indefinitely. Default 30s, override via
// HYPER_SEND_TIMEOUT_MS (0 = leave the kernel default / unbounded).
const DEFAULT_SEND_TIMEOUT_MS: u64 = 30_000;

fn getSendTimeoutMs() u64 {
    const env_ptr = std.c.getenv("HYPER_SEND_TIMEOUT_MS") orelse return DEFAULT_SEND_TIMEOUT_MS;
    const env_val = std.mem.sliceTo(env_ptr, 0);
    const parsed = std.fmt.parseInt(u64, env_val, 10) catch return DEFAULT_SEND_TIMEOUT_MS;
    // Clamp so the timeval @intCast in setSendTimeout can't overflow (see
    // MAX_IDLE_TIMEOUT_MS). 0 stays 0 (leave kernel default).
    return if (parsed == 0) 0 else @min(parsed, MAX_IDLE_TIMEOUT_MS);
}

/// Set SO_SNDTIMEO on `handle` to `ms` milliseconds (no-op when ms == 0).
fn setSendTimeout(handle: std.posix.fd_t, ms: u64) void {
    if (ms == 0) return;
    const tv = std.posix.timeval{
        .sec = @intCast(ms / 1000),
        .usec = @intCast((ms % 1000) * 1000),
    };
    // Direct libc call, NOT std.posix.setsockopt: same reason as
    // py.setTcpNoDelay — the wrapper asserts `unreachable` on EBADF/ENOTSOCK/
    // EINVAL, which a peer that closed this connection produces, panicking the
    // server thread under ReleaseSafe (silent UB under ReleaseFast). Setting a
    // send timeout is best-effort; ignore the result.
    _ = std.c.setsockopt(handle, @as(c_int, std.posix.SOL.SOCKET), @as(c_int, std.posix.SO.SNDTIMEO), std.mem.asBytes(&tv), @sizeOf(std.posix.timeval));
}

// Idle-connection reaping. SO_RCVTIMEO only fires on an IN-PROGRESS blocking
// read, so a connection parked on the reactor (armed ONESHOT, waiting for
// readability) that has sent ZERO bytes never trips it and would hold an fd
// forever. The reactor's periodic sweep (sweepIdle) closes parked connections
// idle beyond this bound; the SAME value drives the SO_RCVTIMEO slowloris guard
// in both modes so threaded and reactor agree on when a silent client is dropped.
// Default 30s; HYPER_IDLE_TIMEOUT_MS overrides (0 disables idle reaping AND
// leaves RCVTIMEO unset — connections may then idle unbounded).
const DEFAULT_IDLE_TIMEOUT_MS: u64 = 30_000;
// Clamp ceiling (7 days). Keeps every downstream cast to i64 (sweep cutoff) and
// to timeval.sec/usec well within range — an out-of-range env value would make
// those @intCasts undefined behavior under ReleaseFast, so bound at the source.
const MAX_IDLE_TIMEOUT_MS: u64 = 7 * 24 * 60 * 60 * 1000;

fn getIdleTimeoutMs() u64 {
    const env_ptr = std.c.getenv("HYPER_IDLE_TIMEOUT_MS") orelse return DEFAULT_IDLE_TIMEOUT_MS;
    const env_val = std.mem.sliceTo(env_ptr, 0);
    const parsed = std.fmt.parseInt(u64, env_val, 10) catch return DEFAULT_IDLE_TIMEOUT_MS;
    // 0 stays 0 (disables reaping); any other value is clamped to the ceiling.
    return if (parsed == 0) 0 else @min(parsed, MAX_IDLE_TIMEOUT_MS);
}

/// Set SO_RCVTIMEO on `handle` to `ms` milliseconds (no-op when ms == 0).
fn setRecvTimeout(handle: std.posix.fd_t, ms: u64) void {
    if (ms == 0) return;
    const tv = std.posix.timeval{
        .sec = @intCast(ms / 1000),
        .usec = @intCast((ms % 1000) * 1000),
    };
    // Direct libc call (see setSendTimeout): std.posix.setsockopt asserts
    // `unreachable` on EBADF/ENOTSOCK/EINVAL from a peer-closed socket, which
    // panics the server thread under ReleaseSafe. Best-effort; ignore result.
    _ = std.c.setsockopt(handle, @as(c_int, std.posix.SOL.SOCKET), @as(c_int, std.posix.SO.RCVTIMEO), std.mem.asBytes(&tv), @sizeOf(std.posix.timeval));
}

/// Arm a freshly-accepted connection: apply the socket options and the slowloris
/// read + send timeouts ONCE (they persist on the fd for the connection's life,
/// so the reactor path never re-sets them per request), then register it ONESHOT
/// directly on its owning shard's poll fd. Called from the acceptor thread —
/// kevent()/epoll_ctl() is thread-safe, so no reactor round trip is needed.
/// Drops the connection if it can't be watched.
fn armNewConnection(stream: py.NetStream) void {
    py.TcpServer.prepareAccepted(stream.handle);
    resetServed(stream.handle);
    setRecvTimeout(stream.handle, server_config.idle_timeout_ms);
    setSendTimeout(stream.handle, server_config.send_timeout_ms);
    // Mark parked BEFORE arming: once armed the connection can be found readable
    // and dispatched (which removes it) at any instant, so the registry entry
    // must already exist or the dispatch-side remove would be a lost no-op and
    // leave the fd untracked. On arm failure, drop the entry and close.
    markParked(stream.handle);
    shardFor(stream.handle).reactor.addOneshotRead(stream.handle) catch {
        unmarkParked(stream.handle);
        stream.close();
    };
}

// ── Per-connection read carry-over ──────────────────────────────────────────
// A single read() can return the end of request N AND the start of request N+1
// (HTTP pipelining, or a client that sends the next request before reading the
// previous response). handleOneRequest consumes EXACTLY one request; the excess
// bytes live in `st.buf[0..st.len]` and seed the next call. Dropping them (the
// old behaviour) silently loses requests, and in reactor mode stalls forever —
// buffered userspace bytes never fire a fresh kevent, so a re-armed connection
// with a buffered request would hang until the client happened to send more.
const CONN_BUF_SIZE: usize = 8192;
const ConnState = struct {
    buf: [CONN_BUF_SIZE]u8 = undefined,
    len: usize = 0, // valid carry-over bytes in buf[0..len]
};

// ── Reactor carry stash (fairness overflow) ─────────────────────────────────
// A worker serves at most REACTOR_BURST_MAX pipelined requests per hand-off so
// one hot pipelining connection can't monopolise a worker. On overflow with
// bytes still buffered, the connection is pushed back onto its shard's work
// queue WITH its carry stashed here (keyed by fd) rather than re-armed — the fd
// is single-owner while stashed/queued, so another worker resumes it in order.
const REACTOR_BURST_MAX: usize = 32;

/// Stash `bytes` (a copy) as the carry-over for `fd` on its shard. Returns
/// false on OOM. The fd is single-owner (the calling worker) while stashed,
/// so no entry for it can already exist.
fn stashCarry(fd: std.posix.fd_t, bytes: []const u8) bool {
    const dup = allocator.dupe(u8, bytes) catch return false;
    const shard = shardFor(fd);
    shard.carry_mutex.lock();
    defer shard.carry_mutex.unlock();
    shard.carry_map.put(allocator, fd, dup) catch {
        allocator.free(dup);
        return false;
    };
    _ = shard.carry_count.fetchAdd(1, .release);
    return true;
}

/// Take (and remove) any stashed carry-over for `fd`. Caller owns the slice.
/// Called on EVERY dispatch, so the empty-map common case (carry only exists
/// after a >REACTOR_BURST_MAX pipelined burst) must stay lock-free: one
/// shared atomic read, no mutex, no cache-line ping-pong across workers.
fn takeCarry(fd: std.posix.fd_t) ?[]u8 {
    const shard = shardFor(fd);
    if (shard.carry_count.load(.acquire) == 0) return null;
    shard.carry_mutex.lock();
    defer shard.carry_mutex.unlock();
    const kv = shard.carry_map.fetchRemove(fd) orelse return null;
    _ = shard.carry_count.fetchSub(1, .release);
    return kv.value;
}

const BurstOutcome = enum {
    done, // fd already closed (error / WebSocket handoff)
    rearm, // buffer fully drained — park on the reactor for the next request
    requeue, // burst cap hit with bytes still buffered — hand back to the queue
};

/// Serve a burst of requests on a connection the reactor found readable,
/// draining any pipelined/eagerly-sent follow-up requests already in the buffer
/// before yielding. Bounded by REACTOR_BURST_MAX for fairness.
fn serveConnectionBurst(stream: py.NetStream, tstate: ?*anyopaque, st: *ConnState) BurstOutcome {
    var count: usize = 0;
    while (true) {
        handleOneRequest(stream, tstate, st) catch |e| {
            // WebSocketReleased: the WS layer owns the fd now — never touch it.
            if (e != error.WebSocketReleased) stream.close();
            return .done;
        };
        count += 1;
        noteServed(stream.handle);
        // Client requested close (Connection: close, or HTTP/1.0 default) — the
        // response already carried `Connection: close`, so send the FIN now and
        // never re-arm or process carry-over on this connection.
        if (_conn_close) {
            stream.close();
            return .done;
        }
        // Fully drained: nothing buffered. Re-arm and wait for the kernel to
        // signal the next request (no speculative read — we only ever act on
        // bytes we already hold).
        if (st.len == 0) return .rearm;
        // Bytes remain (a buffered follow-up request). Serve it inline until the
        // fairness cap, then hand the connection (and its carry) back.
        if (count >= REACTOR_BURST_MAX) return .requeue;
    }
}

fn workerLoopReactor(queue: *ConnectionPool.Queue, worker_id: usize) void {
    const tstate = py.PyThreadState_New(py_interp) orelse @panic("PyThreadState_New failed");
    defer {
        py.PyEval_AcquireThread(tstate);
        py.PyThreadState_Clear(tstate);
        py.PyThreadState_DeleteCurrent();
    }
    initServerMetrics();

    while (!shutdown_flag.load(.acquire)) {
        const stream = queue.pop() orelse break; // null = shutdown
        // In-flight tracked in this worker's own cell (no shared hot-path
        // atomic); the /metrics gauge reconciles from summed cells ~1×/s.
        inflightAdd(worker_id, 1);

        // Seed carry-over from a prior burst-overflow re-queue, if any.
        var st: ConnState = .{};
        if (takeCarry(stream.handle)) |carried| {
            @memcpy(st.buf[0..carried.len], carried);
            st.len = carried.len;
            allocator.free(carried);
        }

        const outcome = serveConnectionBurst(stream, tstate, &st);
        inflightAdd(worker_id, -1);
        maybeReconcileInflightGauge();

        switch (outcome) {
            .done => {},
            .rearm => {
                // Buffer drained — re-arm ONESHOT DIRECTLY on the shard's reactor
                // (thread-safe kevent), holding no worker thread meanwhile. Mark
                // parked (fresh activity timestamp) BEFORE re-arming, same ordering
                // as armNewConnection, so an immediately-readable connection can't
                // be dispatched before it is tracked. If the re-arm fails (e.g.
                // reactor torn down during shutdown), drop the entry and close.
                markParked(stream.handle);
                if (shardFor(stream.handle).reactor.rearmOneshotRead(stream.handle)) |_| {
                    if (loadCounter(&_srv_rearm_counter)) |cnt| cnt.inc(1);
                } else |_| {
                    unmarkParked(stream.handle);
                    stream.close();
                    if (loadCounter(&_srv_rearm_fail_counter)) |cnt| cnt.inc(1);
                }
            },
            .requeue => {
                // Fairness cap hit with bytes still buffered — stash the carry and
                // hand the connection back to this shard's work queue so other fds
                // get a turn. Do NOT re-arm: buffered bytes would never fire a
                // kevent. On stash OOM, drop the connection.
                if (loadCounter(&_srv_requeue_counter)) |cnt| cnt.inc(1);
                if (stashCarry(stream.handle, st.buf[0..st.len])) {
                    // push() closes the stream on OOM and returns false; if it
                    // dropped the connection we MUST remove the carry we just
                    // stashed, or a future accept() reusing this fd number would
                    // inherit another client's buffered bytes (cross-client leak).
                    if (!shardFor(stream.handle).work.push(stream)) {
                        if (takeCarry(stream.handle)) |orphan| allocator.free(orphan);
                    }
                } else {
                    stream.close();
                }
            },
        }
    }
}

fn reactorLoop(shard_index: usize) void {
    const shard = &shards[shard_index];
    // Ensure the idle-reaped metric counter is registered even if this reactor
    // thread bumps it before any worker has run initServerMetrics.
    initServerMetrics();
    const idle_ms = server_config.idle_timeout_ms;
    // Sweep granularity: coarse enough that idle bookkeeping is negligible, fine
    // enough that a reaped connection is closed within ~1s of its deadline.
    const sweep_interval_ms: i64 = 1000;
    // With idle reaping disabled there is no periodic work, so block indefinitely
    // (the shutdown wake pipe still returns us promptly). Otherwise wake at the
    // sweep granularity — NOT the old redundant 100ms, since the wake pipe (not a
    // timeout) is what serves shutdown.
    const wait_timeout: i32 = if (idle_ms == 0) -1 else @intCast(sweep_interval_ms);
    shard.last_sweep_ms = nowMonoMs();
    var events: [256]reactor_mod.Event = undefined;
    var work_batch: [256]py.NetStream = undefined;
    var hup: [256]std.posix.fd_t = undefined;
    while (!shutdown_flag.load(.acquire)) {
        const n = shard.reactor.wait(&events, wait_timeout);
        // Every connection was armed ONESHOT, so the kernel already disarmed the
        // filter the instant this event fired — NO explicit remove() syscall per
        // request. Collect the whole readable batch and hand it to this group's
        // workers in ONE locked push (pushBatch); the fd is single-owner (the
        // dispatched worker) until that worker re-arms it via rearmOneshotRead.
        // Dispatching REMOVES the fd from the parked registry (it becomes
        // worker-owned), so the idle sweep can never close a fd a worker holds.
        var wb: usize = 0;
        var hn: usize = 0;
        for (events[0..n]) |e| {
            if (e.wake) continue; // shutdown nudge — loop re-checks shutdown_flag
            if (e.readable) {
                work_batch[wb] = py.NetStream{ .handle = e.fd };
                wb += 1;
                unmarkParked(e.fd);
            } else if (e.hangup) {
                // Peer closed while parked. ONESHOT already disarmed the filter;
                // close() also removes the fd from the poll set.
                hup[hn] = e.fd;
                hn += 1;
                unmarkParked(e.fd);
            }
        }
        for (hup[0..hn]) |fd| (py.NetStream{ .handle = fd }).close();
        if (wb > 0) {
            if (loadCounter(&_srv_dispatched_counter)) |cnt| cnt.inc(wb);
            shard.work.pushBatch(work_batch[0..wb]);
        }

        // Periodic idle sweep — reaps zero-byte/slowloris connections parked past
        // their deadline that SO_RCVTIMEO can't catch (no blocking read is ever in
        // progress on a parked fd).
        if (idle_ms != 0) {
            const now = nowMonoMs();
            if (now - shard.last_sweep_ms >= sweep_interval_ms) {
                shard.last_sweep_ms = now;
                sweepIdle(shard, shard_index, idle_ms);
            }
        }
    }
}

pub fn server_run(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    // Signal handlers FIRST — before the socket exists. The kernel completes
    // TCP handshakes the moment listen() returns, so anything watching the
    // port for readiness can deliver SIGINT/SIGTERM while this function is
    // still starting workers. Until sigaction lands, SIGINT hits CPython's
    // default handler, which only sets a pending KeyboardInterrupt that can
    // never fire once this thread parks in the GIL-released accept poll —
    // the signal is swallowed and the server shuts down never. Installing
    // before bind closes the window: port reachable ⇒ handlers live.
    install_signal_handlers();

    var tcp_server = py.TcpServer.init(server_host, server_port) catch {
        py.setError("Failed to bind to {s}:{d}", .{ server_host, server_port });
        return null;
    };
    defer tcp_server.deinit();

    // Capture interpreter state before releasing the GIL.
    // Workers need this to create their own PyThreadState.
    py_interp = py.PyInterpreterState_Get();

    // Resolve every server env knob ONCE (Part 1) before workers/accept start.
    resolveServerConfig();

    // Freeze route registration into the single-probe dispatch map. Must run
    // after all add_route* calls and before any worker starts.
    buildDispatchMap();

    // Start thread pool. In reactor mode, pool.init also stands up the reactor
    // groups (their reactor threads + per-group work queues) and binds each
    // worker to a group's queue. The acceptor hands new connections to a group
    // via armNewConnection().
    pool.init();
    const reactor_mode = httpServerModel() == .reactor;
    // Threaded-mode load-shedding cap (0 = reactor mode or disabled).
    const shed_cap: usize = if (reactor_mode) 0 else getMaxPending(pool.thread_count);

    std.debug.print("🚀 HyperDjango server listening on {s}:{d}\n", .{ server_host, server_port });
    std.debug.print("🎯 Zig HTTP core active – {d}-thread pool ({s}), per-worker tstate!\n", .{
        pool.thread_count,
        if (reactor_mode) "reactor keep-alive" else "threaded",
    });
    // Self-report the capacity decision so an operator can see what was
    // auto-detected vs pinned, and why the pool is the size it is. "auto"
    // means the value came from the machine's usable capacity; "pinned" means
    // a HYPER_* override supplied it.
    const workers_pinned = std.c.getenv("HYPER_THREAD_POOL_SIZE") != null;
    std.debug.print(
        "⚙️  capacity: {d} usable core(s), budget {d} → workers={d} ({s})\n",
        .{
            detectCores(),
            cpuBudget(),
            pool.thread_count,
            if (workers_pinned) "pinned" else "auto",
        },
    );
    if (reactor_mode) {
        const reactors_pinned = std.c.getenv("HYPER_HTTP_REACTOR_COUNT") != null;
        std.debug.print("⚙️  reactor shards={d} ({s}) — ~{d} worker(s) per queue\n", .{
            shards.len,
            if (reactors_pinned) "pinned" else "auto",
            (pool.thread_count + shards.len - 1) / shards.len,
        });
    }

    // Release the GIL — workers acquire it per-request via AcquireThread.
    const save = py.PyEval_SaveThread();

    // Accept loop: poll on both listen socket and shutdown pipe.
    // When shutdown_pipe becomes readable, the loop breaks.
    //
    // The listen socket is non-blocking so each POLL.IN wakeup drains the whole
    // kernel accept queue (accept until EWOULDBLOCK) instead of one connection
    // per poll() — under connection storms poll() coalesces many pending SYNs
    // into a single wakeup, and the old one-per-wakeup accept let the queue back
    // up and drop SYNs before load-shedding could fire. Each accepted connection
    // still runs the full per-connection dispatch below (reactor arm, or threaded
    // load-shed vs enqueue).
    tcp_server.setNonBlocking();
    const listen_fd = tcp_server.stream.handle;
    var poll_fds = [2]std.posix.pollfd{
        .{ .fd = listen_fd, .events = std.posix.POLL.IN, .revents = 0 },
        .{ .fd = shutdown_pipe[0], .events = std.posix.POLL.IN, .revents = 0 },
    };

    while (!shutdown_flag.load(.acquire)) {
        // Block indefinitely: the shutdown self-pipe is IN this poll set and BOTH
        // shutdown paths (signal handler, server_shutdown) write it, so a fully
        // quiescent acceptor sleeps instead of waking 10×/s just to re-check the
        // flag. (-1 = infinite; EINTR surfaces as an error and re-polls.)
        // posix-safe: poll's unreachable errnos are EFAULT/EINVAL; poll_fds is
        // a stack array (no EFAULT) and nfds is fixed (no EINVAL). The polled
        // fds (listen socket + shutdown pipe) are server-owned and never
        // peer-closed. EINTR surfaces as a returned error and re-polls.
        const ready = std.posix.poll(&poll_fds, -1) catch continue;
        if (ready == 0) continue; // (only reachable if a timeout is ever set)

        // Shutdown signal received via self-pipe
        if (poll_fds[1].revents & std.posix.POLL.IN != 0) break;

        // New connection(s) ready — drain the accept queue in one wakeup.
        //
        // TWO PHASES PER PASS, and the split is the point: accept() until the
        // queue is empty (or the batch is full) FIRST, then do per-connection
        // setup. Setup is ~6 syscalls plus a reactor registration — several times
        // the cost of accept() itself — and interleaving it drops the drain rate
        // to the accept+setup rate. A client that opens N keep-alive connections
        // at once only has to outrun that rate for the kernel to overflow the
        // listen queue, and an overflowed connection is dropped with NO error on
        // either side: the client waits out an exponential SYN-ACK backoff having
        // already sent its request, so it reports zero responses, zero errors,
        // and stays dead for the rest of a short benchmark window. Draining first
        // makes the burst the listener can absorb a function of accept() alone.
        if (poll_fds[0].revents & std.posix.POLL.IN != 0) {
            drain: while (true) {
                var batch: [ACCEPT_BATCH]py.NetStream = undefined;
                var bn: usize = 0;
                var queue_empty = false;
                while (bn < batch.len) {
                    // Re-check shutdown inside the drain loop so a connection storm
                    // (a full accept queue keeping tryAccept perpetually non-empty)
                    // can't delay shutdown indefinitely.
                    if (shutdown_flag.load(.acquire)) {
                        queue_empty = true;
                        break;
                    }
                    switch (tcp_server.tryAccept()) {
                        .conn => |c2| {
                            batch[bn] = c2.stream;
                            bn += 1;
                        },
                        .retry => {}, // EINTR — retry same wakeup
                        .drained => {
                            queue_empty = true;
                            break;
                        },
                        .fatal => {
                            // ECONNABORTED/EMFILE/… — a listener-health event.
                            // Tally it and stop draining this wakeup; the loop
                            // retries on the next poll.
                            if (loadCounter(&_srv_accept_errors_counter)) |cnt| cnt.inc(1);
                            queue_empty = true;
                            break;
                        },
                    }
                }
                if (bn > 0) noteAcceptBurst(bn);
                for (batch[0..bn]) |stream| {
                    if (reactor_mode) {
                        // Socket options + the read timeout once, then hand the new
                        // connection to its group's reactor: it parks there until
                        // the first request arrives, then dispatches to one of that
                        // group's workers.
                        armNewConnection(stream);
                    } else if (shed_cap != 0 and pool.queue.depth() >= shed_cap) {
                        // Load-shed: workers saturated and the backlog is full. Fail
                        // fast with 503 rather than accept-and-starve, so the client
                        // can retry or fail over instead of hanging. (Threaded mode
                        // only; reactor never sheds — it holds all connections.)
                        // This write happens ON THE ACCEPT THREAD, so bound it with
                        // a short send timeout: a slow-reading shed client must
                        // never be able to stall the accept loop for everyone else.
                        py.TcpServer.prepareAccepted(stream.handle);
                        setSendTimeout(stream.handle, 1000);
                        sendResponse(stream, 503, "text/plain", "Service Unavailable: server at connection capacity, retry shortly");
                        stream.close();
                        _ = shed_total.fetchAdd(1, .monotonic);
                    } else {
                        py.TcpServer.prepareAccepted(stream.handle);
                        _ = pool.queue.push(stream);
                    }
                }
                if (queue_empty) break :drain;
            }
        }
    }

    std.debug.print("\n[SHUTDOWN] Draining {d} active requests (timeout {d}s)...\n", .{
        activeRequests(), DRAIN_TIMEOUT_S,
    });

    // Accept loop ended — tcp_server.deinit() will close the socket via defer.

    // Stop the reactor threads first (they hold no GIL, so joining is always
    // safe). Each wakes from its 100ms wait on shutdown_flag; wake() nudges it
    // to return promptly. Idle parked connections close on process teardown.
    for (shards) |*s| s.reactor.wake();
    for (reactor_threads) |t| t.join();
    for (shards) |*s| s.reactor.deinit();

    // Wake all worker threads so they can exit. Reactor-mode workers wait on
    // their group's work queue; threaded workers wait on pool.queue. Do NOT
    // clear `shards` here — a worker finishing its final request may still call
    // rearmOneshotRead() (which indexes `shards`); the slice stays valid until
    // process exit. The reactor is already deinited above, so that re-arm just
    // fails and the worker closes the fd — no leak, no crash.
    for (shards) |*s| s.work.wakeAll();
    pool.queue.wakeAll();

    // Wait for in-flight requests to complete
    const drain_deadline = py.nanoTimestamp() + @as(i128, DRAIN_TIMEOUT_S) * std.time.ns_per_s;
    var drained = true;
    while (activeRequests() > 0) {
        if (py.nanoTimestamp() > drain_deadline) {
            std.debug.print("[SHUTDOWN] Drain timeout — {d} requests still active, forcing exit\n", .{
                activeRequests(),
            });
            drained = false;
            break;
        }
        py.sleep(10 * std.time.ns_per_ms);
    }

    // Join all worker threads BEFORE restoring the GIL.
    // Workers' defer blocks call PyEval_AcquireThread → PyThreadState_DeleteCurrent.
    // We must let them complete before main thread takes the GIL back via
    // PyEval_RestoreThread, otherwise workers and main race for the GIL during
    // Python finalization, causing use-after-free (SIGSEGV).
    //
    // If drain timed out, workers may still be in Python code and join() would
    // block indefinitely. In that case, skip joining — the Py_DecRef guards in
    // db.zig (module_shutting_down checks) protect against use-after-free, and
    // process exit will reclaim all resources.
    if (drained) {
        for (pool.threads) |thread| {
            thread.join();
        }
    } else {
        std.debug.print("[SHUTDOWN] Skipping thread join (drain timed out) — Py_DecRef guards active\n", .{});
    }

    // Shutdown hygiene — only when workers are truly joined, so nothing else can
    // touch the queues or the carry map concurrently. Close any connections left
    // sitting in the accept queue or the shard work queues (e.g. reactor requeues
    // that were never picked up) so their fds don't leak, and free every carry
    // stash entry so no buffered bytes are left owned.
    if (drained) {
        while (pool.queue.tryPop()) |leftover| leftover.close();
        // Close every still-parked connection — the reactor threads are joined
        // and the workers are drained, so nothing else touches the registry.
        const parked_end = @min(parked_high_water.load(.monotonic), parked_at.len);
        for (parked_at[0..parked_end], 0..) |*slot, fd| {
            if (slot.swap(0, .acq_rel) != 0) (py.NetStream{ .handle = @intCast(fd) }).close();
        }
        for (shards) |*s| {
            while (s.work.tryPop()) |leftover| leftover.close();
            s.carry_mutex.lock();
            var cit = s.carry_map.iterator();
            while (cit.next()) |kv| allocator.free(kv.value_ptr.*);
            s.carry_map.clearAndFree(allocator);
            s.carry_count.store(0, .release);
            s.carry_mutex.unlock();
        }
    }

    // Clean up self-pipe
    _ = std.posix.system.close(shutdown_pipe[0]);
    _ = std.posix.system.close(shutdown_pipe[1]);

    std.debug.print("[SHUTDOWN] Server stopped cleanly.\n", .{});

    py.PyEval_RestoreThread(save);
    return py.pyNone();
}

const HeaderList = std.ArrayListUnmanaged(HeaderPair);

fn parseHeaders(alloc: std.mem.Allocator, request_data: []const u8, first_line_end: usize, header_end_pos: usize) HeaderList {
    var headers: HeaderList = .empty;

    var pos = first_line_end + 2; // skip past first \r\n
    while (pos < header_end_pos) {
        const line_end = std.mem.indexOfPos(u8, request_data, pos, "\r\n") orelse header_end_pos;
        const line = request_data[pos..line_end];
        pos = line_end + 2;

        if (line.len == 0) break;

        // Consistent with scanRequestHead's smuggling rejects (which run first
        // and 400 the whole request): never surface an obs-fold continuation or
        // a "name <ws>: value" header as a real header pair.
        if (line[0] == ' ' or line[0] == '\t') continue; // obs-fold continuation
        const colon = std.mem.indexOfScalar(u8, line, ':') orelse continue;
        if (colon > 0 and (line[colon - 1] == ' ' or line[colon - 1] == '\t')) continue; // ws before colon
        const name = std.mem.trim(u8, line[0..colon], " \t");
        const value = std.mem.trim(u8, line[colon + 1 ..], " \t");

        if (name.len == 0) continue;
        headers.append(alloc, .{ .name = name, .value = value }) catch continue;
    }

    return headers;
}

// ── HeaderView capture (Part 2) ──────────────────────────────────────────────
// scanRequestHead already walks every header line once for framing/WS/CT/Range.
// It now ALSO fills this fixed stack array with the SAME {name, value} pairs
// parseHeaders would produce (identical skips: obs-fold, ws-before-colon,
// missing colon, empty name) — so the header-consuming dispatch paths (enhanced
// Python, native FFI, Django catch-all) reuse the pre-filled views instead of
// re-walking the block. The views are slices into the read buffer (st.buf),
// which outlives dispatch — zero allocation. Requests with more than
// MAX_HEADER_VIEWS headers set `truncated` and fall back to parseHeaders (arena)
// so behavior is byte-identical even in that rare case.
const MAX_HEADER_VIEWS: usize = 64;
const HeaderViews = struct {
    items: [MAX_HEADER_VIEWS]HeaderPair = undefined,
    count: usize = 0,
    truncated: bool = false,

    fn slice(self: *const HeaderViews) []const HeaderPair {
        return self.items[0..self.count];
    }
};

/// Resolve the full header list for a header-consuming dispatch path: the
/// pre-filled HeaderView slice on the common path, or a one-off parseHeaders
/// re-walk into the per-request arena when the header count overflowed the fixed
/// array (rare). Arena memory is reclaimed at the next reqArenaReset, so the
/// fallback needs no explicit deinit.
fn headerItems(hviews: *const HeaderViews, request_head: []const u8, first_line_end: usize, header_end: usize) []const HeaderPair {
    if (!hviews.truncated) return hviews.slice();
    const list = parseHeaders(reqAllocator(), request_head, first_line_end, header_end);
    return list.items;
}

/// ASCII-lowercase `name` into freshly-arena-allocated bytes (Part 6). HTTP
/// header field-names are ASCII tokens, so this equals Python's str.lower() for
/// every valid header name. On allocation failure the original slice is returned
/// (degrades to the pre-existing mixed-case behavior rather than failing).
fn asciiLowerName(arena: std.mem.Allocator, name: []const u8) []const u8 {
    const buf = arena.alloc(u8, name.len) catch return name;
    for (name, 0..) |ch, i| buf[i] = std.ascii.toLower(ch);
    return buf;
}

/// Unified, request-smuggling-safe framing scan. Determines the exact body
/// length (needed for byte-exact carry-over) and rejects the ambiguous cases —
/// applied to EVERY route, not just the Python path, so no handler can be fed a
/// mis-framed body. Chunked (Transfer-Encoding) is not implemented, so it is
/// rejected rather than silently mis-parsed.
const FramingResult = union(enum) {
    ok: usize, // content_length
    reject: struct { status: u16, body: []const u8 },
};

/// Single-pass header capture. The header block is walked EXACTLY once, folding
/// what used to be three separate scans — scanFraming (Content-Length /
/// Transfer-Encoding), isWebSocketUpgrade (Connection / Upgrade), and the
/// per-route Content-Type / Range re-scans — into one traversal that records the
/// handful of headers-of-interest in this fixed stack struct. The full arbitrary
/// header list (parseHeaders) is still built ONLY for the paths that genuinely
/// need it (Python request object, Django catch-all, native FFI), from the
/// per-worker arena. WebSocket detection stays complete regardless of a framing
/// reject (the caller checks is_ws_upgrade first, matching the old ordering where
/// the upgrade check ran before framing).
const RequestScan = struct {
    framing: FramingResult,
    is_ws_upgrade: bool = false,
    content_type: []const u8 = "",
    range: []const u8 = "",
    // Connection header tokens (comma-list, case-insensitive) — drive keep-alive
    // vs close for the response and whether the connection is re-armed afterwards.
    conn_close: bool = false,
    conn_keepalive: bool = false,
};

/// RFC 7230 1*DIGIT check: non-empty and every byte in '0'..'9'. Rejects the
/// signs and '_' separators Zig's parseInt would otherwise silently accept.
fn isAllAsciiDigits(s: []const u8) bool {
    if (s.len == 0) return false;
    for (s) |ch| {
        if (ch < '0' or ch > '9') return false;
    }
    return true;
}

fn scanRequestHead(request_head: []const u8, first_line_end: usize, header_end: usize, views: *HeaderViews) RequestScan {
    var has_cl = false;
    var has_te = false;
    var content_length: usize = 0;
    var conn_upgrade = false;
    var conn_close = false;
    var conn_keepalive = false;
    var upgrade_ws = false;
    var content_type: []const u8 = "";
    var range: []const u8 = "";
    // A framing reject is recorded but does NOT stop the scan, so WebSocket
    // detection (and the other captures) stays complete — the caller branches on
    // is_ws_upgrade before ever acting on a framing reject.
    var reject: ?FramingResult = null;

    var pos = first_line_end + 2;
    while (pos < header_end) {
        const line_end = std.mem.indexOfPos(u8, request_head, pos, "\r\n") orelse header_end;
        const line = request_head[pos..line_end];
        pos = line_end + 2;
        if (line.len == 0) break;
        // RFC 7230 §3.2.4: a line beginning with SP/HT is an obs-fold
        // continuation. We don't unfold, and silently dropping it would let a
        // proxy and this server disagree on the header set (a smuggling vector),
        // so reject the message.
        if (line[0] == ' ' or line[0] == '\t') {
            if (reject == null) reject = .{ .reject = .{ .status = 400, .body = "{\"detail\":\"Obsolete line folding not allowed\",\"status\":400}" } };
            continue;
        }
        const colon = std.mem.indexOfScalar(u8, line, ':') orelse continue;
        // RFC 7230 §3.2.4: no whitespace is allowed between field-name and the
        // colon. "Content-Length : 5" is a classic smuggling trick — proxies
        // disagree on whether it names Content-Length. Reject.
        if (colon > 0 and (line[colon - 1] == ' ' or line[colon - 1] == '\t')) {
            if (reject == null) reject = .{ .reject = .{ .status = 400, .body = "{\"detail\":\"Whitespace before header colon\",\"status\":400}" } };
            continue;
        }
        const name = std.mem.trim(u8, line[0..colon], " \t");
        const value = std.mem.trim(u8, line[colon + 1 ..], " \t");

        // Part 2: capture the header view (identical filter to parseHeaders —
        // this point is reached only after the same obs-fold / ws-before-colon /
        // colon-present skips). Overflow past the fixed array flags a fallback.
        if (name.len != 0) {
            if (views.count < MAX_HEADER_VIEWS) {
                views.items[views.count] = .{ .name = name, .value = value };
                views.count += 1;
            } else {
                views.truncated = true;
            }
        }

        if (std.ascii.eqlIgnoreCase(name, "content-length")) {
            if (has_cl) {
                // Duplicate Content-Length — a smuggling vector. Reject.
                if (reject == null) reject = .{ .reject = .{ .status = 400, .body = "{\"detail\":\"Duplicate Content-Length\",\"status\":400}" } };
            } else {
                has_cl = true;
                // RFC 7230 §3.3.2: Content-Length = 1*DIGIT. Zig's parseInt also
                // accepts '_' separators and a leading '+'/'-' sign, so "1_0",
                // "+5", "-0" would sneak through and desync framing. Require every
                // byte to be an ASCII digit before trusting the value.
                if (!isAllAsciiDigits(value)) {
                    if (reject == null) reject = .{ .reject = .{ .status = 400, .body = "{\"detail\":\"Invalid Content-Length\",\"status\":400}" } };
                } else content_length = std.fmt.parseInt(usize, value, 10) catch blk: {
                    if (reject == null) reject = .{ .reject = .{ .status = 400, .body = "{\"detail\":\"Invalid Content-Length\",\"status\":400}" } };
                    break :blk 0;
                };
            }
        } else if (std.ascii.eqlIgnoreCase(name, "transfer-encoding")) {
            has_te = true;
        } else if (std.ascii.eqlIgnoreCase(name, "content-type")) {
            content_type = value;
        } else if (std.ascii.eqlIgnoreCase(name, "range")) {
            range = value;
        } else if (std.ascii.eqlIgnoreCase(name, "connection")) {
            // Connection may be comma-separated: "keep-alive, Upgrade" / "close".
            var vit = std.mem.splitScalar(u8, value, ',');
            while (vit.next()) |part| {
                const tok = std.mem.trim(u8, part, " \t");
                if (std.ascii.eqlIgnoreCase(tok, "upgrade")) {
                    conn_upgrade = true;
                } else if (std.ascii.eqlIgnoreCase(tok, "close")) {
                    conn_close = true;
                } else if (std.ascii.eqlIgnoreCase(tok, "keep-alive")) {
                    conn_keepalive = true;
                }
            }
        } else if (std.ascii.eqlIgnoreCase(name, "upgrade")) {
            if (std.ascii.eqlIgnoreCase(value, "websocket")) upgrade_ws = true;
        }
    }

    const framing: FramingResult = blk: {
        if (has_te) {
            if (has_cl) {
                // TE + CL = smuggling attack (RFC 7230 §3.3.3).
                break :blk .{ .reject = .{ .status = 400, .body = "{\"detail\":\"Conflicting Transfer-Encoding and Content-Length\",\"status\":400}" } };
            }
            break :blk .{ .reject = .{ .status = 501, .body = "{\"detail\":\"Transfer-Encoding not supported\",\"status\":501}" } };
        }
        if (reject) |r| break :blk r;
        break :blk .{ .ok = content_length };
    };

    return .{
        .framing = framing,
        .is_ws_upgrade = conn_upgrade and upgrade_ws,
        .content_type = content_type,
        .range = range,
        .conn_close = conn_close,
        .conn_keepalive = conn_keepalive,
    };
}

fn handleConnection(stream: py.NetStream, tstate: ?*anyopaque) void {
    // Slowloris protection: if the client sends nothing for the idle timeout,
    // read() times out and the worker is freed. No kqueue needed — just a socket
    // option. Same knob (HYPER_IDLE_TIMEOUT_MS) as the reactor idle sweep so both
    // modes drop a silent client on the same deadline; 0 leaves it unbounded.
    setRecvTimeout(stream.handle, server_config.idle_timeout_ms);
    // Bound blocking writes too: a zero-window / slow-reading client must not be
    // able to pin this worker forever on a writev.
    setSendTimeout(stream.handle, server_config.send_timeout_ms);

    // Per-connection carry-over: a pipelined/eagerly-sent request N+1 read
    // alongside request N seeds the next iteration instead of being dropped.
    var st: ConnState = .{};
    while (true) {
        handleOneRequest(stream, tstate, &st) catch |e| {
            // A WebSocket that transferred ownership owns its own fd close
            // (via _ws_release) — closing here would double-close a possibly
            // reused fd. Every other exit closes the socket normally.
            if (e != error.WebSocketReleased) stream.close();
            return;
        };
        // Honor Connection: close (and HTTP/1.0 default-close): FIN promptly
        // instead of looping into a blocking read that hangs until SO_RCVTIMEO —
        // which would pin this worker for the whole idle timeout.
        if (_conn_close) {
            stream.close();
            return;
        }
    }
}

fn handleOneRequest(stream: py.NetStream, tstate: ?*anyopaque, st: *ConnState) !void {
    // Reclaim the previous request's transient arena allocations (header list)
    // with retain_capacity, so this request's parseHeaders reuses that memory
    // instead of hitting the global allocator. Everything the arena backs lives
    // and dies within this one call.
    reqArenaReset();
    _write_failed = false;
    _conn_close = false;
    _req_is_head = false;

    // Phase 1: ensure a full header block is buffered. st.buf[0..st.len] holds
    // carry-over from a prior read that pulled in (part of) this request — reuse
    // it and only read() when more bytes are actually needed. In reactor mode
    // this is also what lets a re-armed connection whose next request was already
    // buffered make progress WITHOUT waiting for a fresh kevent.
    var total_read: usize = st.len;
    // Bytes of st.buf belonging to THIS request; the remainder (buf[consumed..])
    // is carry-over for the next request. maxInt until the boundary is known, so
    // any early error exit (all of which close the connection) carries nothing.
    var consumed: usize = std.math.maxInt(usize);
    defer {
        const cs = @min(consumed, total_read);
        if (cs < total_read) {
            std.mem.copyForwards(u8, st.buf[0 .. total_read - cs], st.buf[cs..total_read]);
            st.len = total_read - cs;
        } else {
            st.len = 0;
        }
    }

    var header_end_pos: ?usize = std.mem.indexOf(u8, st.buf[0..total_read], "\r\n\r\n");
    while (header_end_pos == null and total_read < st.buf.len) {
        const n = stream.read(st.buf[total_read..]) catch return error.ReadError;
        if (n == 0) return error.ConnectionClosed;
        // Re-scan from just before the freshly appended bytes so a \r\n\r\n split
        // across two reads is still found.
        const scan_from = if (total_read >= 3) total_read - 3 else 0;
        total_read += n;
        header_end_pos = std.mem.indexOfPos(u8, st.buf[0..total_read], scan_from, "\r\n\r\n");
    }
    if (total_read == 0) return error.ConnectionClosed;

    const he = header_end_pos orelse {
        sendResponse(stream, 431, "text/plain", "Request Header Fields Too Large");
        return error.HeadersTooLarge;
    };

    const request_head = st.buf[0..total_read];

    // Phase 2: Parse the first line to get method + path (cheap — no allocs)
    const first_line_end = std.mem.indexOf(u8, request_head, "\r\n") orelse return error.ConnectionClosed;
    const first_line = request_head[0..first_line_end];

    var parts = std.mem.splitScalar(u8, first_line, ' ');
    const method = parts.next() orelse return error.ConnectionClosed;
    const raw_path = parts.next() orelse return error.ConnectionClosed;

    const q_idx = std.mem.indexOf(u8, raw_path, "?");
    const path = if (q_idx) |i| raw_path[0..i] else raw_path;
    const query_string = if (q_idx) |i| raw_path[i + 1 ..] else "";

    // HEAD gets the identical headers a GET would but zero body bytes (RFC 7230
    // §3.3.3). The response writers read this threadlocal to suppress the body on
    // every dispatch path (static/file/native/db/python/Django) at once.
    _req_is_head = std.mem.eql(u8, method, "HEAD");

    // Phase 2.5: ONE header-block traversal captures everything the downstream
    // paths need — framing (Content-Length / Transfer-Encoding), WebSocket
    // upgrade (Connection / Upgrade), Content-Type (multipart) and Range — so no
    // later phase re-scans the header bytes.
    // Clamp the start: when the request line is terminated by the SAME CRLF that
    // begins the CRLF-CRLF header terminator (e.g. "GET /\r\n\r\n" — no version,
    // no headers), first_line_end == he, so first_line_end+2 > he and the naive
    // slice [first_line_end+2 .. he] is INVERTED → an out-of-bounds panic that
    // crashed the worker on a 9-byte unauthenticated request (remote DoS). @min
    // yields an empty header section for that degenerate case instead.
    const headers_section = request_head[@min(first_line_end + 2, he)..he];
    // Part 2: the single header walk also fills the HeaderView array (slices into
    // request_head/st.buf, alive through dispatch) for the consuming paths.
    var hviews: HeaderViews = .{};
    const scan = scanRequestHead(request_head, first_line_end, he, &hviews);

    // WebSocket upgrade is checked BEFORE framing (a WS upgrade is bodyless and
    // hands the fd off, so it never carries over — and the old code ran this
    // check ahead of framing). The handshake sub-scans only run on real upgrades.
    if (scan.is_ws_upgrade) {
        // RFC 6455 §4.2.1: the client MUST offer version 13. If it names any
        // other version, reject with 426 and advertise the version we speak so
        // the client can retry — the upgrade is NOT completed. An absent header
        // is tolerated (lenient): a compliant client always sends 13, and
        // rejecting its absence would regress clients/tests that omit it, while
        // the conformance-critical case is an explicit non-13 offer.
        {
            var vit = std.mem.splitSequence(u8, headers_section, "\r\n");
            while (vit.next()) |line| {
                if (line.len == 0) break;
                const colon = std.mem.indexOfScalar(u8, line, ':') orelse continue;
                if (std.ascii.eqlIgnoreCase(std.mem.trim(u8, line[0..colon], " "), "sec-websocket-version")) {
                    const ver = std.mem.trim(u8, line[colon + 1 ..], " ");
                    if (!std.mem.eql(u8, ver, "13")) {
                        stream.writeAll(
                            "HTTP/1.1 426 Upgrade Required\r\n" ++
                                "Sec-WebSocket-Version: 13\r\n" ++
                                "Content-Length: 0\r\n" ++
                                "Connection: close\r\n\r\n",
                        ) catch {};
                        return error.ConnectionClosed;
                    }
                    break;
                }
            }
        }
        const ws_key = ws.getWebSocketKey(headers_section) orelse {
            sendResponse(stream, 400, "text/plain", "Missing Sec-WebSocket-Key");
            return error.ConnectionClosed;
        };
        const subprotocol = ws.getWebSocketProtocol(headers_section);
        // permessage-deflate is intentionally NOT negotiated: the receive path
        // never decompressed RSV1 frames (it returned the compressed bytes as-is),
        // so advertising it produced garbage. Declining the extension keeps us
        // honest and lets the frame parser reject any reserved bit (RFC 6455 §5.2).
        ws.sendHandshakeEx(stream, ws_key, subprotocol, false) catch {
            sendResponse(stream, 500, "text/plain", "WebSocket handshake failed");
            return error.ConnectionClosed;
        };
        // Handshake complete — hand the connection to the WebSocket layer.
        // WebSocketReleased means _ws_release owns the fd close; skip our close.
        const transferred = ws.handleWebSocket(stream, path, query_string, headers_section, tstate);
        return if (transferred) error.WebSocketReleased else error.ConnectionClosed;
    }

    // Keep-alive decision (RFC 7230 §6.1). HTTP/1.1 keeps the connection alive
    // unless the client sent `Connection: close`; HTTP/1.0 closes by default
    // unless it sent `Connection: keep-alive`. The response builders emit the
    // matching Connection token, and the per-connection loop closes (rather than
    // re-arming) when this is set, so a `Connection: close` request gets a prompt
    // FIN instead of hanging until SO_RCVTIMEO — which in threaded mode would pin
    // a worker for the whole idle timeout.
    const is_http10 = std.mem.endsWith(u8, first_line, "HTTP/1.0");
    _conn_close = scan.conn_close or (is_http10 and !scan.conn_keepalive);

    // Phase 3: unified, smuggling-safe framing for EVERY route — the exact body
    // length pins the request boundary, which is what makes carry-over exact.
    var content_length: usize = 0;
    switch (scan.framing) {
        .ok => |cl| content_length = cl,
        .reject => |rj| {
            sendResponse(stream, rj.status, "application/json", rj.body);
            return error.BadFraming; // ambiguous framing — never keep-alive
        },
    }

    // Phase 4: read the body ONCE, up front, for every downstream path. This
    // pins the request boundary and drains the body even for handlers that
    // ignore it (static, 404, …), keeping the stream byte-aligned for the next
    // keep-alive request. A body larger than the already-buffered prefix is read
    // into its own allocation (never over-reads past the body); an over-large
    // body streams (Python pulls chunks) and forces a close afterwards.
    const already_read_body = request_head[he + 4 .. total_read];
    const use_streaming = content_length > server_max_body_size;
    var body: []const u8 = "";
    var body_owned: ?[]u8 = null;
    defer if (body_owned) |b| allocator.free(b);
    defer _stream_body = .{};

    if (content_length == 0) {
        body = ""; // No body — any excess bytes are the NEXT pipelined request.
    } else if (use_streaming) {
        _stream_body = .{
            .stream = stream,
            .content_length = content_length,
            .bytes_read = 0,
            .already_read = already_read_body,
            .already_read_offset = 0,
        };
        body = ""; // Python pulls chunks via _read_body_chunk().
    } else if (already_read_body.len >= content_length) {
        body = already_read_body[0..content_length];
    } else {
        const full_body = allocator.alloc(u8, content_length) catch {
            sendResponse(stream, 500, "application/json", "{\"detail\":\"Out of memory\",\"status\":500}");
            return error.ConnectionClosed;
        };
        body_owned = full_body;
        @memcpy(full_body[0..already_read_body.len], already_read_body);
        var body_read: usize = already_read_body.len;
        while (body_read < content_length) {
            const n = stream.read(full_body[body_read..content_length]) catch return error.ReadError;
            if (n == 0) return error.ConnectionClosed; // truncated body — close
            body_read += n;
        }
        body = full_body;
    }
    // The request occupies buf[0 .. he+4+content_length] when the body is fully
    // buffered; when it isn't (large body read separately, or streaming), the min
    // clamps to total_read so no next-request bytes are mis-attributed and no
    // partial body is mis-carried.
    consumed = @min(he + 4 + content_length, total_read);

    // Phase 5: route match + dispatch. Every arm funnels through `break :dispatch`
    // to the single tail so an over-large (streamed) body can force a close —
    // its exact drain can't be guaranteed across all handler types.
    dispatch: {
        const rt = getRouter();
        // HEAD is an implicit alias for GET: HyperApp.run registers only the
        // GET route with the Zig router (the GET→HEAD twin lives in the Python
        // Router._route_map, which never reaches native), so HEAD on a GET-only
        // endpoint used to 404 in production while it 200s under ASGI. Mirror
        // ASGI: when there is no explicit HEAD route, dispatch the GET handler —
        // the response funnels already strip the body for HEAD (_req_is_head).
        //
        // Part 4: fill `match` in place (no by-value RouteMatch copy). `match` is
        // left with an empty, safe-to-deinit owned_values on a miss.
        var match: router_mod.RouteMatch = undefined;
        match.owned_values = .empty;
        const matched = rt.findRouteInto(method, path, &match) or
            (_req_is_head and rt.findRouteInto("GET", path, &match));
        if (!matched) {
            // No matching route — Django catch-all (WSGI) if registered.
            if (django_handler != null) {
                dispatchDjango(stream, tstate, method, path, query_string, body, headerItems(&hviews, request_head, first_line_end, he));
                break :dispatch;
            }
            // Unified error contract with the ASGI path (HTTPException → JSON
            // {"detail","status"}); previously a bare {"error":...} shape.
            sendResponse(stream, 404, "application/json", "{\"detail\":\"Not Found\",\"status\":404}");
            break :dispatch;
        }
        defer match.deinit();

        // ── APPEND_SLASH parity with ASGI _dispatch ──
        // The Zig radix trie collapses trailing slashes, so `GET /posts` matches
        // a route registered as `/posts/` directly. The ASGI path emits a 301 to
        // the slash-terminated URL in that case (Router.resolve →
        // _APPEND_SLASH_REDIRECT); mirror it here so native == ASGI. The matched
        // route's registered pattern is the tail of handler_key ("METHOD /pattern").
        // Redirects for any method, matching ASGI (which redirects regardless of
        // method). The query string is preserved on the redirect target.
        if (appendSlashEnabled() and needsAppendSlashRedirect(path, match.handler_key)) {
            var loc_buf: [2048]u8 = undefined;
            const location = if (query_string.len > 0)
                std.fmt.bufPrint(&loc_buf, "\r\nLocation: {s}/?{s}", .{ path, query_string }) catch null
            else
                std.fmt.bufPrint(&loc_buf, "\r\nLocation: {s}/", .{path}) catch null;
            if (location) |loc| {
                sendFullResponse(stream, 301, loc, "");
                break :dispatch;
            }
            // Location too long to frame — fall through and serve normally rather
            // than emit a redirect with no target.
        }

        // CORS preflight — immediate 204, no Python
        if (cors_enabled and std.mem.eql(u8, method, "OPTIONS")) {
            sendResponse(stream, 204, "", "");
            break :dispatch;
        }

        // ── Trie-embedded dispatch (Part 3): findRouteInto surfaced the resolved
        // *DispatchEntry that was stamped into the matched node at startup, so we
        // dereference it directly — NO second hash-map probe on `handler_key`. The
        // 0 case (slot never stamped) falls back to a map probe defensively. ──
        const de: DispatchEntry = if (match.data != 0)
            @as(*const DispatchEntry, @ptrFromInt(match.data)).*
        else
            (dispatch_map.?.get(match.handler_key) orelse {
                std.debug.print("[ZIG] handler entry missing for key: {s}\n", .{match.handler_key});
                sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
                break :dispatch;
            });

        const entry, const schema_ptr = switch (de) {
            // Static routes — single writeAll of pre-rendered bytes
            .static => |static_entry| {
                if (loadCounter(&_srv_static_counter)) |cnt| cnt.inc(1);
                if (loadCounter(&_srv_responses_counter)) |cnt| cnt.inc(1);
                bumpStatusClass(200);
                // Splice a fresh Date (cheap seqlock read) between the pre-rendered
                // head and body — one writev, still zero body copy.
                var date_buf: [HTTP_DATE_LEN]u8 = undefined;
                httpDate(&date_buf);
                var dh_buf: [96]u8 = undefined;
                // Connection is spliced here (not baked into head) so a close
                // request gets `Connection: close` on the shared static bytes.
                const date_hdr = std.fmt.bufPrint(&dh_buf, "\r\nConnection: {s}\r\nServer: HyperDjango\r\nDate: {s}\r\n\r\n", .{ connectionHeaderValue(), date_buf[0..] }) catch "\r\nConnection: close\r\n\r\n";
                // HEAD: identical head (Content-Length is baked into static_entry.head)
                // but zero body bytes (RFC 7230 §3.3.3).
                const static_body: []const u8 = if (_req_is_head) "" else static_entry.body;
                stream.writeAllVectored(&.{ static_entry.head, date_hdr, static_body }) catch {
                    noteWriteFailure();
                };
                break :dispatch;
            },
            // File routes — zero-copy file serving with Range support. The Range
            // header was captured in the single header scan above.
            .file => |file_entry| {
                const range_header: ?[]const u8 = if (scan.range.len > 0) scan.range else null;
                serveFile(stream, file_entry.file_path, file_entry.content_type, range_header);
                break :dispatch;
            },
            // Native FFI routes — no GIL, no Python
            .native => |native_entry| {
                const ffi_resp = callNativeHandler(native_entry.*, method, path, query_string, body, headerItems(&hviews, request_head, first_line_end, he), &match.params);
                const resp_ct = ffi_resp.content_type[0..ffi_resp.content_type_len];
                const resp_body = ffi_resp.body[0..ffi_resp.body_len];
                sendResponse(stream, ffi_resp.status_code, resp_ct, resp_body);
                break :dispatch;
            },
            // DB routes — full Zig request cycle, no Python, no GIL
            .db => |db_entry| {
                db.handleDbRoute(stream, db_entry, body, &match.params, query_string, &sendResponse);
                break :dispatch;
            },
            // Python handler — fall through to the shared Python dispatch below.
            .python => |pyd| .{ pyd.entry.*, pyd.schema },
        };

        // ── Ultra-fast path: simple handlers that don't need headers or body ──
        switch (entry.handler_tag) {
            .simple_sync_noargs => {
                if (cache_noargs_responses) {
                    if (getCachedResponse(match.handler_key)) |cached| {
                        sendResponse(stream, 200, "application/json", cached);
                        break :dispatch;
                    }
                    callPythonNoArgsCaching(tstate, entry, stream, match.handler_key);
                } else {
                    callPythonNoArgs(tstate, entry, stream);
                }
                break :dispatch;
            },
            .simple_sync => {
                if (cache_noargs_responses) {
                    var cache_key_buf: [512]u8 = undefined;
                    const cache_key = if (query_string.len > 0)
                        std.fmt.bufPrint(&cache_key_buf, "{s} {s}?{s}", .{ method, path, query_string }) catch path
                    else
                        std.fmt.bufPrint(&cache_key_buf, "{s} {s}", .{ method, path }) catch path;
                    if (getCachedResponse(cache_key)) |cached| {
                        sendResponse(stream, 200, "application/json", cached);
                        break :dispatch;
                    }
                    callPythonVectorcallCaching(tstate, entry, query_string, &match.params, stream, cache_key);
                } else {
                    callPythonVectorcall(tstate, entry, query_string, &match.params, stream);
                }
                break :dispatch;
            },
            else => {},
        }

        // ── Full path: body already read in Phase 4. The full header list is
        // NOT re-walked here (Part 2) — only the .enhanced arm needs it, and it
        // consumes the HeaderView array the single scanRequestHead pass filled. ──

        // Pre-GIL multipart detection — Content-Type came from the single header
        // scan; boundary points into st.buf (alive through dispatch).
        var multipart_boundary: ?[]const u8 = null;
        if (body.len > 0 and scan.content_type.len >= "multipart/form-data".len and
            std.ascii.startsWithIgnoreCase(scan.content_type, "multipart/form-data"))
        {
            multipart_boundary = extractBoundaryFromContentType(scan.content_type);
        }

        // DHI validation for model_sync — single parse, retain tree. The schema
        // pointer came from the unified dispatch entry (no second map probe).
        var cached_parse: ?std.json.Parsed(std.json.Value) = null;
        defer if (cached_parse) |*cp| cp.deinit();

        if (body.len > 0) {
            if (schema_ptr) |sp| {
                const vr = dhi.validateJsonRetainParsed(body, sp);
                switch (vr) {
                    .ok => |parsed| {
                        cached_parse = parsed;
                    },
                    .err => |ve| {
                        defer ve.deinit();
                        std.debug.print("[DHI] validation failed for {s}\n", .{match.handler_key});
                        sendResponse(stream, ve.status_code, "application/json", ve.body);
                        break :dispatch;
                    },
                }
            }
        }

        switch (entry.handler_tag) {
            .simple_sync_noargs, .simple_sync => unreachable, // handled above
            .model_sync => {
                if (body.len > 0) {
                    if (cached_parse) |cp| {
                        callPythonModelHandlerParsed(tstate, entry, cp.value, &match.params, stream);
                    } else {
                        callPythonModelHandlerDirect(tstate, entry, body, &match.params, stream);
                    }
                    break :dispatch;
                }
                callPythonHandlerDirect(tstate, entry, query_string, body, &match.params, stream);
            },
            .body_sync => {
                callPythonHandlerDirect(tstate, entry, query_string, body, &match.params, stream);
            },
            .enhanced => {
                const stream_cl: usize = if (use_streaming) content_length else 0;
                const resp = callPythonHandler(tstate, entry, method, path, query_string, body, headerItems(&hviews, request_head, first_line_end, he), &match.params, multipart_boundary, stream_cl, stream.handle);
                defer resp.deinit();
                if (resp.stream_pull) |pull_fn| {
                    // Chunked/streaming response: drive the Python pull callable
                    // one chunk at a time, writing Transfer-Encoding: chunked
                    // frames. sendChunkedResponse CONSUMES (decrefs) pull_fn and
                    // requires the GIL released (callPythonHandler already did).
                    sendChunkedResponse(stream, tstate, resp.status_code, resp.content_type, resp.extra_headers, pull_fn);
                } else if (resp.extra_headers.len > 0) {
                    // Reject CR/LF in the handler-controlled content_type (injection).
                    var ct_buf: [256]u8 = undefined;
                    const safe_ct = sanitizeHeaderValue(resp.content_type, &ct_buf);
                    var eh_buf: [4096]u8 = undefined;
                    // On overflow, fall back to a heap build so Content-Type is never
                    // dropped (the old `catch resp.extra_headers` mislabeled the body
                    // by losing the Content-Type prefix). If even that fails, send via
                    // sendResponse which still carries Content-Type (extra headers lost).
                    const full_eh = std.fmt.bufPrint(&eh_buf, "\r\nContent-Type: {s}{s}", .{ safe_ct, resp.extra_headers }) catch
                        (std.fmt.allocPrint(reqAllocator(), "\r\nContent-Type: {s}{s}", .{ safe_ct, resp.extra_headers }) catch {
                            sendResponse(stream, resp.status_code, safe_ct, resp.body);
                            break :dispatch;
                        });
                    sendFullResponse(stream, resp.status_code, full_eh, resp.body);
                } else {
                    sendResponse(stream, resp.status_code, resp.content_type, resp.body);
                }
            },
        }
    }

    // A failed/partial response write means the client never got the full
    // response bytes — the connection is desynced, so close instead of serving
    // the next keep-alive request on a stream at an unknown offset.
    if (_write_failed) return error.ConnectionClosed;

    // An over-large (streamed) body can't be guaranteed byte-exactly drained by
    // every handler type, so never keep-alive after one — close instead of
    // risking a mis-framed follow-up request.
    if (use_streaming) return error.ConnectionClosed;
}

// ── FFI native handler dispatch (no GIL, no Python) ─────────────────────────

fn callNativeHandler(
    entry: NativeHandlerEntry,
    method: []const u8,
    path: []const u8,
    query_string: []const u8,
    body: []const u8,
    headers: []const HeaderPair,
    params: *const router_mod.RouteParams,
) FfiResponse {
    // Header + param parallel arrays now live in fixed stack buffers (mirroring
    // the vectorcall path), so the common case does ZERO per-request heap allocs.
    // A request with more than MAX_FFI_HEADERS headers falls back to a single
    // heap block for the header arrays only; path params are bounded by the
    // router's MAX_ROUTE_PARAMS and always fit on the stack.
    const pentries = params.entries();
    const pcount = @min(pentries.len, MAX_FFI_PARAMS);

    var h_names_stack: [MAX_FFI_HEADERS][*c]const u8 = undefined;
    var h_name_lens_stack: [MAX_FFI_HEADERS]usize = undefined;
    var h_values_stack: [MAX_FFI_HEADERS][*c]const u8 = undefined;
    var h_value_lens_stack: [MAX_FFI_HEADERS]usize = undefined;

    const hcount = headers.len;
    const over_cap = hcount > MAX_FFI_HEADERS;
    // Heap fallback for the rare over-cap case only; freed on return.
    const heap_names: ?[][*c]const u8 = if (over_cap) allocator.alloc([*c]const u8, hcount) catch return ffiError() else null;
    defer if (heap_names) |s| allocator.free(s);
    const heap_name_lens: ?[]usize = if (over_cap) allocator.alloc(usize, hcount) catch return ffiError() else null;
    defer if (heap_name_lens) |s| allocator.free(s);
    const heap_values: ?[][*c]const u8 = if (over_cap) allocator.alloc([*c]const u8, hcount) catch return ffiError() else null;
    defer if (heap_values) |s| allocator.free(s);
    const heap_value_lens: ?[]usize = if (over_cap) allocator.alloc(usize, hcount) catch return ffiError() else null;
    defer if (heap_value_lens) |s| allocator.free(s);

    const h_names: [][*c]const u8 = heap_names orelse h_names_stack[0..hcount];
    const h_name_lens: []usize = heap_name_lens orelse h_name_lens_stack[0..hcount];
    const h_values: [][*c]const u8 = heap_values orelse h_values_stack[0..hcount];
    const h_value_lens: []usize = heap_value_lens orelse h_value_lens_stack[0..hcount];

    for (headers, 0..) |h, i| {
        h_names[i] = h.name.ptr;
        h_name_lens[i] = h.name.len;
        h_values[i] = h.value.ptr;
        h_value_lens[i] = h.value.len;
    }

    var p_names: [MAX_FFI_PARAMS][*c]const u8 = undefined;
    var p_name_lens: [MAX_FFI_PARAMS]usize = undefined;
    var p_values: [MAX_FFI_PARAMS][*c]const u8 = undefined;
    var p_value_lens: [MAX_FFI_PARAMS]usize = undefined;
    for (pentries[0..pcount], 0..) |pe, i| {
        p_names[i] = pe.key.ptr;
        p_name_lens[i] = pe.key.len;
        p_values[i] = pe.value.ptr;
        p_value_lens[i] = pe.value.len;
    }

    const ffi_req = FfiRequest{
        .method = method.ptr,
        .method_len = method.len,
        .path = path.ptr,
        .path_len = path.len,
        .query_string = query_string.ptr,
        .query_len = query_string.len,
        .body = body.ptr,
        .body_len = body.len,
        .header_names = h_names.ptr,
        .header_name_lens = h_name_lens.ptr,
        .header_values = h_values.ptr,
        .header_value_lens = h_value_lens.ptr,
        .header_count = hcount,
        .param_names = &p_names,
        .param_name_lens = &p_name_lens,
        .param_values = &p_values,
        .param_value_lens = &p_value_lens,
        .param_count = pcount,
    };

    return entry.handler_fn(&ffi_req);
}

const MAX_FFI_HEADERS: usize = 64;
const MAX_FFI_PARAMS: usize = router_mod.MAX_ROUTE_PARAMS;

fn ffiError() FfiResponse {
    const body = "{\"detail\":\"Internal Server Error\",\"status\":500}";
    return .{
        .status_code = 500,
        .content_type = "application/json",
        .content_type_len = 16,
        .body = body,
        .body_len = body.len,
    };
}

// ── Tuple ABI helper ─────────────────────────────────────────────────────────
// Python fast handlers return (status_code, content_type, body_str).
// Unpack and send — no dict key lookups, no hash computation.

/// Convert a Python status object into a validated u16. `PyLong_AsLong` returns
/// -1 with a pending exception on a non-int (and any out-of-range int would be
/// UB through a bare @intCast under ReleaseFast). Clear any pending exception and
/// clamp to the valid HTTP range, falling back to 500 — mirrors the enhanced
/// path's validation so the fast paths can never crash or leak an exception.
/// PyUnicode_AsUTF8 that clears the exception it sets on failure. A non-str
/// content_type/body/header makes AsUTF8 return NULL and set TypeError; a str
/// with lone surrogates sets UnicodeEncodeError. Left uncleared, that stale
/// exception rides the reused per-worker tstate into the NEXT request and
/// corrupts its Vectorcall/Call (same class as the status-code fix). Callers
/// use the returned optional and fall back safely.
fn asUtf8OrClear(obj: ?*c.PyObject) [*c]const u8 {
    const s = c.PyUnicode_AsUTF8(obj);
    if (s == null) c.PyErr_Clear();
    return s;
}

/// Build a Python `str` from raw peer bytes LOSSLESSLY via Latin-1, matching the
/// ASGI path (request.py `from_asgi` decodes header names/values and the query
/// string with `latin-1`). Latin-1 maps every byte 0..=255 to a code point, so
/// this NEVER fails and therefore NEVER leaves a pending exception on the reused
/// per-worker tstate — unlike `py.newString` (PyUnicode_FromStringAndSize),
/// which raises UnicodeDecodeError on any non-UTF-8 byte and, left uncleared,
/// rides into the current Vectorcall and poisons the NEXT request (F1). Use at
/// every wire-bytes→str site so the native path is both leak-free and byte-for-
/// byte ASGI-equivalent. Only returns null on genuine allocation failure.
fn newStrLossless(s: []const u8) ?*c.PyObject {
    return c.PyUnicode_DecodeLatin1(@ptrCast(s.ptr), @intCast(s.len), null);
}

/// Decode already-percent-decoded PATH bytes as UTF-8 with the `surrogateescape`
/// error handler. A real ASGI server (uvicorn/hypercorn) exposes `scope["path"]`
/// UTF-8-decoded (ASGI spec: "percent-encoded sequences and UTF-8 byte sequences
/// decoded into characters"), so `request.path` and `path_params` must be UTF-8,
/// NOT Latin-1 — otherwise a non-ASCII path (e.g. `/caf%C3%A9`) would decode to
/// mojibake on the native path only. `surrogateescape` maps any invalid byte to a
/// lone surrogate, so like `newStrLossless` it NEVER raises and never leaves a
/// pending exception (F1). Headers/method/query stay Latin-1 (matching
/// request.py `from_asgi`, which Latin-1-decodes those). Null only on OOM.
fn newStrPathUtf8(s: []const u8) ?*c.PyObject {
    return c.PyUnicode_DecodeUTF8(@ptrCast(s.ptr), @intCast(s.len), "surrogateescape");
}

fn parseStatusCode(sc_obj: *c.PyObject) u16 {
    const code = c.PyLong_AsLong(sc_obj);
    if (code == -1 and c.PyErr_Occurred() != null) {
        c.PyErr_Clear();
        return 500;
    }
    if (code >= 100 and code <= 599) return @intCast(code);
    return 500;
}

fn sendTupleResponse(stream: py.NetStream, result: *c.PyObject) void {
    const sc_obj = py.PyTuple_GetItem(result, 0) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    const ct_obj = py.PyTuple_GetItem(result, 1) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    const body_obj = py.PyTuple_GetItem(result, 2) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };

    const status_code: u16 = parseStatusCode(sc_obj);
    const ct_cstr: [*c]const u8 = asUtf8OrClear(ct_obj) orelse "application/json";
    const content_type = std.mem.span(ct_cstr);

    if (c.PyUnicode_Check(body_obj) != 0) {
        if (asUtf8OrClear(body_obj)) |cs| {
            sendResponse(stream, status_code, content_type, std.mem.span(cs));
            return;
        }
    } else if (c.PyBytes_Check(body_obj) != 0) {
        var size: c.Py_ssize_t = 0;
        var buf: [*c]u8 = undefined;
        if (c.PyBytes_AsStringAndSize(body_obj, @ptrCast(&buf), &size) == 0) {
            sendResponse(stream, status_code, content_type, buf[0..@intCast(size)]);
            return;
        }
    }
    sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
}

// ── simple_sync_noargs: PyObject_CallNoArgs — no tuple/dict construction ─────

fn callPythonNoArgs(tstate: ?*anyopaque, entry: HandlerEntry, stream: py.NetStream) void {
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const result = py.PyObject_CallNoArgs(entry.handler) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);
    sendTupleResponse(stream, result);
}

/// Like callPythonNoArgs but caches the pre-rendered response for subsequent calls.
fn callPythonNoArgsCaching(tstate: ?*anyopaque, entry: HandlerEntry, stream: py.NetStream, handler_key: []const u8) void {
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const result = py.PyObject_CallNoArgs(entry.handler) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);

    // Extract tuple (status, content_type, body) and cache pre-rendered response.
    // A malformed tuple must still produce a prompt 500 — returning silently here
    // sends NOTHING, wedging the client until the 30s SO_RCVTIMEO and pinning a
    // worker the whole time (mirror the non-caching sendTupleResponse twin).
    const sc_obj = py.PyTuple_GetItem(result, 0) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    const ct_obj = py.PyTuple_GetItem(result, 1) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    const body_obj = py.PyTuple_GetItem(result, 2) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };

    const status_code: u16 = parseStatusCode(sc_obj);
    const ct_cstr: [*c]const u8 = asUtf8OrClear(ct_obj) orelse "application/json";
    const content_type = std.mem.span(ct_cstr);

    var body_slice: []const u8 = "";
    if (c.PyUnicode_Check(body_obj) != 0) {
        if (asUtf8OrClear(body_obj)) |cs| body_slice = std.mem.span(cs);
    } else if (c.PyBytes_Check(body_obj) != 0) {
        var size: c.Py_ssize_t = 0;
        var buf: [*c]u8 = undefined;
        if (c.PyBytes_AsStringAndSize(body_obj, @ptrCast(&buf), &size) == 0) {
            body_slice = buf[0..@intCast(size)];
        }
    }

    // Send response now
    sendResponse(stream, status_code, content_type, body_slice);

    // Cache body only (sendResponse adds fresh Date headers on each hit)
    const body_dupe = allocator.dupe(u8, body_slice) catch return;
    cacheResponse(handler_key, body_dupe);
}

/// Fast path for simple_sync handlers with 1+ params.
/// Zig assembles the positional arg vector from path/query params — no Python
/// dict allocation, no parse_qs, no call_kwargs. Calls via PyObject_Vectorcall.
/// Fast path for simple_sync handlers with 1+ params.
/// Zig assembles the positional arg vector from path/query params — no Python
/// dict allocation, no parse_qs, no call_kwargs. Calls via PyObject_Vectorcall.
/// Params with has_default=true that are missing from the request are omitted
/// from the tail of the arg vector, letting Python apply its own defaults.
/// Fast path for simple_sync handlers with 1+ params.
/// Zig assembles the positional arg vector from path/query params — no Python
/// dict allocation, no parse_qs, no call_kwargs. Calls via PyObject_Vectorcall.
/// Params with has_default=true that are missing from the request are omitted
/// from the tail of the arg vector, letting Python apply its own defaults.
fn callPythonVectorcall(
    tstate: ?*anyopaque,
    entry: HandlerEntry,
    query_string: []const u8,
    params: *const router_mod.RouteParams,
    stream: py.NetStream,
) void {
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const argc = entry.param_count;
    var argv: [MAX_PARAMS]?*c.PyObject = undefined;
    // Track created objects for Py_DecRef after the call.
    var created: [MAX_PARAMS]?*c.PyObject = [_]?*c.PyObject{null} ** MAX_PARAMS;
    defer for (created[0..argc]) |obj| {
        if (obj) |o| c.Py_DecRef(o);
    };

    // Per-param decode buffer for percent-decoding str query values.
    var decode_buf: [8192]u8 = undefined;

    // last_filled: highest index+1 where we have a real value.
    // Trailing optional params with no value are excluded from the vectorcall
    // so Python uses its own default — never passes None for missing optionals.
    var last_filled: usize = 0;

    for (entry.param_meta[0..argc], 0..) |pm, i| {
        // Path params take priority; fall back to query string. Track the source
        // so `+` is decoded to space ONLY for query values, never path (F3).
        const from_path: ?[]const u8 = params.get(pm.name);
        const val_str: ?[]const u8 = from_path orelse queryStringGet(query_string, pm.name);

        if (val_str) |vs| {
            const py_obj: ?*c.PyObject = switch (pm.type_tag) {
                .int => blk: {
                    const n = std.fmt.parseInt(i64, vs, 10) catch 0;
                    break :blk c.PyLong_FromLongLong(n);
                },
                .float => blk: {
                    const f = std.fmt.parseFloat(f64, vs) catch 0.0;
                    break :blk c.PyFloat_FromDouble(f);
                },
                .bool_val => blk: {
                    const b: c_long = if (std.mem.eql(u8, vs, "true") or std.mem.eql(u8, vs, "1")) 1 else 0;
                    break :blk c.PyBool_FromLong(b);
                },
                .str => blk: {
                    // Size the decode target to the raw length so a long value is
                    // never silently truncated (decoded len ≤ raw len) — F5. Only
                    // spill to the request arena when it exceeds the stack buffer.
                    const dbuf: []u8 = if (vs.len <= decode_buf.len)
                        decode_buf[0..]
                    else
                        (reqAllocator().alloc(u8, vs.len) catch decode_buf[0..]);
                    // Query values `+`→space; path values keep literal `+` (F3).
                    const decoded = percentDecode(vs, dbuf, from_path == null);
                    // Path values decode as UTF-8 (ASGI parity), query values as
                    // Latin-1 (request.py from_asgi). Both never raise / never
                    // poison the vectorcall or next request (F1).
                    break :blk if (from_path != null)
                        newStrPathUtf8(decoded)
                    else
                        newStrLossless(decoded);
                },
            };
            if (py_obj) |obj| {
                argv[i] = obj;
                created[i] = obj;
                last_filled = i + 1;
            } else {
                // Object creation failed (e.g. OOM). Clear any pending exception
                // so it can't ride into the vectorcall / poison the next request
                // (F1), then substitute None.
                c.PyErr_Clear();
                argv[i] = @ptrCast(&c._Py_NoneStruct);
                if (!pm.has_default) last_filled = i + 1;
            }
        } else {
            // Missing param: if required, pass None; if optional, skip (Python uses default)
            argv[i] = @ptrCast(&c._Py_NoneStruct);
            if (!pm.has_default) last_filled = i + 1;
        }
    }

    const result = py.PyObject_Vectorcall(
        entry.handler,
        @as([*]const ?*c.PyObject, @ptrCast(&argv)),
        last_filled, // excludes trailing missing optionals
        null,
    ) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);
    sendTupleResponse(stream, result);
}

/// Like callPythonVectorcall but caches the pre-rendered response keyed by full path.
fn callPythonVectorcallCaching(
    tstate: ?*anyopaque,
    entry: HandlerEntry,
    query_string: []const u8,
    params: *const router_mod.RouteParams,
    stream: py.NetStream,
    cache_key: []const u8,
) void {
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const argc = entry.param_count;
    var args: [MAX_PARAMS + 1]*c.PyObject = undefined;
    args[0] = entry.handler;
    var decode_buf: [2048]u8 = undefined;
    var last_filled: usize = 0;

    for (entry.param_meta[0..argc], 0..) |pm, i| {
        const from_path: ?[]const u8 = params.get(pm.name);
        const val_str: ?[]const u8 = from_path orelse queryStringGet(query_string, pm.name);
        if (val_str) |vs| {
            const py_obj: ?*c.PyObject = switch (pm.type_tag) {
                .int => blk: {
                    const n = std.fmt.parseInt(i64, vs, 10) catch 0;
                    break :blk c.PyLong_FromLongLong(n);
                },
                .float => blk: {
                    const f = std.fmt.parseFloat(f64, vs) catch 0;
                    break :blk c.PyFloat_FromDouble(f);
                },
                .bool_val => blk: {
                    const is_true = std.mem.eql(u8, vs, "true") or std.mem.eql(u8, vs, "1");
                    break :blk if (is_true) py.pyTrue() else py.pyFalse();
                },
                .str => blk: {
                    // Size to raw length — no silent truncation (F5); spill to the
                    // request arena only past the stack buffer.
                    const dbuf: []u8 = if (vs.len <= decode_buf.len)
                        decode_buf[0..]
                    else
                        (reqAllocator().alloc(u8, vs.len) catch decode_buf[0..]);
                    // `+`→space for query only, never path (F3). Path decodes as
                    // UTF-8 (ASGI parity), query as Latin-1 (request.py); neither
                    // raises / leaks (F1).
                    const decoded = percentDecode(vs, dbuf, from_path == null);
                    break :blk if (from_path != null)
                        newStrPathUtf8(decoded)
                    else
                        newStrLossless(decoded);
                },
            };
            if (py_obj) |obj| {
                args[i + 1] = obj;
                last_filled = i + 1;
            } else {
                // Clear any pending exception before sending — a live error here
                // would otherwise ride the reused tstate into the next request (F1).
                c.PyErr_Clear();
                sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
                for (1..i + 1) |j| c.Py_DecRef(args[j]);
                return;
            }
        } else {
            if (pm.has_default) break;
            sendResponse(stream, 422, "application/json", "{\"detail\":\"missing required param\",\"status\":422}");
            for (1..i + 1) |j| c.Py_DecRef(args[j]);
            return;
        }
    }
    defer for (1..last_filled + 1) |j| c.Py_DecRef(args[j]);

    const nargs = last_filled;
    const result = py.PyObject_Vectorcall(entry.handler, @ptrCast(&args[1]), nargs, null) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);

    // Extract tuple and send + cache. A malformed tuple must still send a prompt
    // 500 — a silent return sends nothing and pins the worker until the client's
    // 30s SO_RCVTIMEO fires (mirror the non-caching sendTupleResponse twin).
    const sc_obj = py.PyTuple_GetItem(result, 0) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    const ct_obj = py.PyTuple_GetItem(result, 1) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    const body_obj = py.PyTuple_GetItem(result, 2) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };

    const status_code: u16 = parseStatusCode(sc_obj);
    const ct_cstr: [*c]const u8 = asUtf8OrClear(ct_obj) orelse "application/json";
    const content_type = std.mem.span(ct_cstr);

    var body_slice: []const u8 = "";
    if (c.PyUnicode_Check(body_obj) != 0) {
        if (asUtf8OrClear(body_obj)) |cs| body_slice = std.mem.span(cs);
    } else if (c.PyBytes_Check(body_obj) != 0) {
        var size: c.Py_ssize_t = 0;
        var buf: [*c]u8 = undefined;
        if (c.PyBytes_AsStringAndSize(body_obj, @ptrCast(&buf), &size) == 0) {
            body_slice = buf[0..@intCast(size)];
        }
    }

    sendResponse(stream, status_code, content_type, body_slice);

    // Cache body only (sendResponse adds fresh Date headers on each hit)
    const body_dupe = allocator.dupe(u8, body_slice) catch return;
    cacheResponse(cache_key, body_dupe);
}

// ── Fast Python handler dispatch (simple_sync/body_sync) ─────────────────────
// Calls Python with kwargs dict, unpacks 3-tuple response — zero extra allocs.

fn callPythonHandlerDirect(tstate: ?*anyopaque, entry: HandlerEntry, query_string: []const u8, body: []const u8, params: *const router_mod.RouteParams, stream: py.NetStream) void {
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const kwargs = c.PyDict_New() orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(kwargs);

    // ── path_params dict — typed when param metadata is available ──
    const py_path_params = buildTypedPathParams(&entry, params) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(py_path_params);
    _ = c.PyDict_SetItemString(kwargs, "path_params", py_path_params);

    if (query_string.len > 0) {
        // Lossless Latin-1 (F1 + ASGI parity): a non-UTF-8 query string must not
        // fail-and-leak a pending exception into the PyObject_Call below.
        if (newStrLossless(query_string)) |v| {
            _ = c.PyDict_SetItemString(kwargs, "query_string", v);
            c.Py_DecRef(v);
        } else c.PyErr_Clear();
    }

    if (body.len > 0) {
        const py_body = c.PyBytes_FromStringAndSize(@ptrCast(body.ptr), @intCast(body.len)) orelse {
            sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
            return;
        };
        _ = c.PyDict_SetItemString(kwargs, "body", py_body);
        c.Py_DecRef(py_body);
    }

    const empty_tuple = c.PyTuple_New(0) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(empty_tuple);

    const result = c.PyObject_Call(entry.handler, empty_tuple, kwargs) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);

    // Unpack (status_code, content_type, body_str) 3-tuple
    sendTupleResponse(stream, result);
}

// ── JSON-to-Python conversion (eliminates Python json.loads round-trip) ──────

fn jsonValueToPyObject(val: std.json.Value) ?*c.PyObject {
    return switch (val) {
        .null => py.pyNone(),
        .bool => |b| if (b) py.pyTrue() else py.pyFalse(),
        .integer => |i| py.newInt(i),
        .float => |f| c.PyFloat_FromDouble(f),
        .string => |s| py.newString(s),
        .array => |arr| blk: {
            const list = c.PyList_New(@intCast(arr.items.len)) orelse break :blk null;
            for (arr.items, 0..) |item, idx| {
                const py_item = jsonValueToPyObject(item) orelse {
                    c.Py_DecRef(list);
                    break :blk null;
                };
                // PyList_SetItem steals the reference
                _ = c.PyList_SetItem(list, @intCast(idx), py_item);
            }
            break :blk list;
        },
        .object => |obj| blk: {
            const dict = c.PyDict_New() orelse break :blk null;
            var it = obj.iterator();
            while (it.next()) |entry| {
                const py_key = py.newString(entry.key_ptr.*) orelse {
                    c.Py_DecRef(dict);
                    break :blk null;
                };
                const py_val = jsonValueToPyObject(entry.value_ptr.*) orelse {
                    c.Py_DecRef(py_key);
                    c.Py_DecRef(dict);
                    break :blk null;
                };
                _ = c.PyDict_SetItem(dict, py_key, py_val);
                c.Py_DecRef(py_key);
                c.Py_DecRef(py_val);
            }
            break :blk dict;
        },
        .number_string => |s| blk: {
            // Fallback: try to parse as Python int/float from string
            break :blk py.newString(s);
        },
    };
}

// ── model_sync fast dispatch: Zig-parsed JSON → Python dict (no json.loads) ──

fn callPythonModelHandlerDirect(tstate: ?*anyopaque, entry: HandlerEntry, body: []const u8, params: *const router_mod.RouteParams, stream: py.NetStream) void {
    const parsed = std.json.parseFromSlice(std.json.Value, allocator, body, .{}) catch {
        sendResponse(stream, 400, "application/json", "{\"detail\":\"Invalid JSON\",\"status\":400}");
        return;
    };
    defer parsed.deinit();

    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const py_body_dict = jsonValueToPyObject(parsed.value) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(py_body_dict);

    const kwargs = c.PyDict_New() orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(kwargs);

    _ = c.PyDict_SetItemString(kwargs, "body_dict", py_body_dict);

    // ── path_params dict — typed when param metadata is available ──
    const py_path_params = buildTypedPathParams(&entry, params) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(py_path_params);
    _ = c.PyDict_SetItemString(kwargs, "path_params", py_path_params);

    const empty_tuple = c.PyTuple_New(0) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(empty_tuple);

    const result = c.PyObject_Call(entry.handler, empty_tuple, kwargs) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);

    sendTupleResponse(stream, result);
}

/// Single-parse variant: takes a pre-parsed std.json.Value from validateJsonRetainParsed.
/// Eliminates the second JSON parse that callPythonModelHandlerDirect does.
fn callPythonModelHandlerParsed(tstate: ?*anyopaque, entry: HandlerEntry, json_value: std.json.Value, params: *const router_mod.RouteParams, stream: py.NetStream) void {
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    const py_body_dict = jsonValueToPyObject(json_value) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(py_body_dict);

    const kwargs = c.PyDict_New() orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(kwargs);

    _ = c.PyDict_SetItemString(kwargs, "body_dict", py_body_dict);

    // ── path_params dict — typed when param metadata is available ──
    const py_path_params = buildTypedPathParams(&entry, params) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(py_path_params);
    _ = c.PyDict_SetItemString(kwargs, "path_params", py_path_params);

    const empty_tuple = c.PyTuple_New(0) orelse {
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(empty_tuple);

    const result = c.PyObject_Call(entry.handler, empty_tuple, kwargs) orelse {
        reportNativeError("native handler");
        sendResponse(stream, 500, "application/json", "{\"detail\":\"Internal Server Error\",\"status\":500}");
        return;
    };
    defer c.Py_DecRef(result);

    sendTupleResponse(stream, result);
}

// ── Shared helper: build typed path_params dict ─────────────────────────────
// When entry.param_count > 0, creates typed Python objects (PyLong, PyFloat,
// PyBool) directly from the raw URL byte slices. Otherwise falls back to
// all-string values. Eliminates the Python-side int()/float() round-trip.

/// Percent-decode a URL path-segment value into a request-arena buffer, WITHOUT
/// `+`→space (a path uses `+` literally — F3). Decoded length ≤ raw length. On a
/// (tiny) allocation failure returns the raw slice unchanged — degraded, never
/// wrong-length. The returned bytes are decoded into a fresh str by the caller.
fn percentDecodePathValue(raw: []const u8) []const u8 {
    const buf = reqAllocator().alloc(u8, raw.len) catch return raw;
    return percentDecode(raw, buf, false);
}

fn buildTypedPathParams(entry: *const HandlerEntry, params: *const router_mod.RouteParams) ?*c.PyObject {
    const py_path_params = c.PyDict_New() orelse return null;

    for (params.entries()) |pe| {
        // Lossless Latin-1 everywhere (F1): a non-UTF-8 key/value must never raise
        // or leave a pending exception. String values are also percent-decoded
        // (no `+`→space) so path_params match the now-decoded request.path and the
        // ASGI scope["path_params"] (F3).
        const pk = newStrLossless(pe.key) orelse continue;

        // Look up type metadata for this param name
        const pv: ?*c.PyObject = if (entry.param_count > 0) blk: {
            for (entry.param_meta[0..entry.param_count]) |pm| {
                if (std.mem.eql(u8, pm.name, pe.key)) {
                    break :blk switch (pm.type_tag) {
                        .int => c.PyLong_FromLongLong(std.fmt.parseInt(i64, pe.value, 10) catch 0),
                        .float => c.PyFloat_FromDouble(std.fmt.parseFloat(f64, pe.value) catch 0.0),
                        .bool_val => c.PyBool_FromLong(if (std.mem.eql(u8, pe.value, "true") or std.mem.eql(u8, pe.value, "1")) @as(c_long, 1) else 0),
                        .str => newStrPathUtf8(percentDecodePathValue(pe.value)),
                    };
                }
            }
            // No metadata match: fall back to string
            break :blk newStrPathUtf8(percentDecodePathValue(pe.value));
        } else newStrPathUtf8(percentDecodePathValue(pe.value));

        if (pv) |v| {
            _ = c.PyDict_SetItem(py_path_params, pk, v);
            c.Py_DecRef(v);
        } else {
            // Value/key construction failed (OOM). Clear so no exception leaks.
            c.PyErr_Clear();
        }
        c.Py_DecRef(pk);
    }

    return py_path_params;
}

// ── Python handler dispatch (full kwargs — enhanced/model handlers) ──────────

// The handler-coroutine runner (hyperdjango.native._coro.run_handler_coro)
// resolved once and cached for the process lifetime. It replaces the old
// per-request `asyncio.run(coro)` call, which constructed and tore down a
// whole event loop (epoll fd + socketpair + close, ~111 us measured) for
// EVERY async request — and whose per-request fd alloc/free serialized all
// workers on the kernel's process-wide fd-table lock, an accidental scaling
// limiter. The runner drives never-suspending coroutines to completion in one
// send() (~0.4 us) and finishes genuinely-suspending ones on a persistent
// per-worker-thread loop. Callers must hold this thread's Python thread
// state (they do).
var cached_coro_runner: ?*c.PyObject = null;
var coro_runner_mutex: py.Mutex = .{};

fn getCoroRunner() ?*c.PyObject {
    if (@atomicLoad(?*c.PyObject, &cached_coro_runner, .acquire)) |f| return f;
    coro_runner_mutex.lock();
    defer coro_runner_mutex.unlock();
    if (cached_coro_runner) |f| return f; // another thread resolved it first
    const mod = c.PyImport_ImportModule("hyperdjango.native._coro") orelse return null;
    defer c.Py_DecRef(mod);
    const run_fn = c.PyObject_GetAttrString(mod, "run_handler_coro") orelse return null;
    // Keep a strong reference for the lifetime of the process (intentional cache).
    @atomicStore(?*c.PyObject, &cached_coro_runner, run_fn, .release);
    return run_fn;
}

// ── Native error / log bridge (task #2) ─────────────────────────────────────
//
// Handler failures on the native path used to `PyErr_Print()` to raw fd 2 and
// then clear the error — no request context, never through Python logging.
// Instead we route them through the framework's own `logging` (the same channel
// the ASGI path uses via `logging.getLogger("hyperdjango.*")`), so the traceback
// reaches configured handlers/formatters with context. No new module-level
// registration is needed: we lazily import stdlib `logging` (mirroring
// getCoroRunner) and cache `logging.getLogger("hyperdjango.native")`. The 500
// response behaviour at every call site is unchanged.
//
// All entry points require the GIL to be held by the caller (every call site is
// already inside a PyEval_AcquireThread region; db.zig's listener thread wraps
// its calls in its own AcquireThread).

var cached_native_logger: ?*c.PyObject = null;
var native_logger_mutex: py.Mutex = .{};

pub const LogLevel = enum {
    info,
    warning,
    @"error",

    fn methodName(self: LogLevel) [*:0]const u8 {
        return switch (self) {
            .info => "info",
            .warning => "warning",
            .@"error" => "error",
        };
    }
};

/// Resolve (and cache for process lifetime) `logging.getLogger("hyperdjango.native")`.
/// MUST be called with the GIL held and NO pending exception (import/getattr
/// consult the error indicator). Returns null — clearing any error it raised —
/// if the logger can't be resolved, so callers fall back cleanly.
fn getNativeLogger() ?*c.PyObject {
    if (@atomicLoad(?*c.PyObject, &cached_native_logger, .acquire)) |l| return l;
    native_logger_mutex.lock();
    defer native_logger_mutex.unlock();
    if (cached_native_logger) |l| return l; // resolved by another thread

    const logging = c.PyImport_ImportModule("logging") orelse {
        c.PyErr_Clear();
        return null;
    };
    defer c.Py_DecRef(logging);
    const get_logger = c.PyObject_GetAttrString(logging, "getLogger") orelse {
        c.PyErr_Clear();
        return null;
    };
    defer c.Py_DecRef(get_logger);
    const name = py.newString("hyperdjango.native") orelse return null;
    defer c.Py_DecRef(name);
    const logger = c.PyObject_CallFunctionObjArgs(get_logger, name, @as(?*c.PyObject, null)) orelse {
        c.PyErr_Clear();
        return null;
    };
    // Intentional process-lifetime cache (strong ref, never freed).
    @atomicStore(?*c.PyObject, &cached_native_logger, logger, .release);
    return logger;
}

/// Emit a plain log record (no exception) through the framework logger.
/// GIL MUST be held. Best-effort: any failure inside logging is swallowed
/// (error indicator cleared) so it can never poison the caller's request.
pub fn emitNativeLog(level: LogLevel, message: []const u8) void {
    const logger = getNativeLogger() orelse return;
    const meth = c.PyObject_GetAttrString(logger, level.methodName()) orelse {
        c.PyErr_Clear();
        return;
    };
    defer c.Py_DecRef(meth);
    const py_msg = py.newString(message) orelse {
        c.PyErr_Clear();
        return;
    };
    defer c.Py_DecRef(py_msg);
    if (c.PyObject_CallOneArg(meth, py_msg)) |r| {
        c.Py_DecRef(r);
    } else {
        c.PyErr_Clear(); // logging itself raised — never propagate
    }
}

/// Report the CURRENT Python exception (if any) through the framework logger
/// with request `context`, INSTEAD of PyErr_Print. GIL MUST be held. The error
/// indicator is consumed (cleared) exactly as PyErr_Print would leave it. If no
/// logger can be resolved, falls back to the original PyErr_Print behaviour so
/// nothing is ever silently dropped. Pub so other GIL-holding native sites
/// (e.g. db.zig's LISTEN callback dispatch) can reuse the same bridge.
pub fn reportNativeError(context: []const u8) void {
    // Take ownership of the pending exception (NEW refs; clears the indicator so
    // getNativeLogger's import/getattr run on a clean error state).
    var ptype: ?*c.PyObject = null;
    var pvalue: ?*c.PyObject = null;
    var ptb: ?*c.PyObject = null;
    c.PyErr_Fetch(&ptype, &pvalue, &ptb);
    c.PyErr_NormalizeException(&ptype, &pvalue, &ptb);
    if (pvalue) |ev| {
        if (ptb) |tb| _ = c.PyException_SetTraceback(ev, tb); // does not steal tb
    }

    const logger = getNativeLogger() orelse {
        // No framework logger — restore the exception and print it (unchanged
        // legacy behaviour). PyErr_Restore steals our three refs; PyErr_Print
        // then clears the indicator.
        if (ptype != null or pvalue != null or ptb != null) {
            c.PyErr_Restore(ptype, pvalue, ptb);
            c.PyErr_Print();
        }
        return;
    };
    defer {
        if (ptype) |o| c.Py_DecRef(o);
        if (pvalue) |o| c.Py_DecRef(o);
        if (ptb) |o| c.Py_DecRef(o);
    }

    const meth = c.PyObject_GetAttrString(logger, "error") orelse {
        c.PyErr_Clear();
        return;
    };
    defer c.Py_DecRef(meth);

    var msg_buf: [512]u8 = undefined;
    const msg = std.fmt.bufPrint(&msg_buf, "native handler error: {s}", .{context}) catch context;
    const py_msg = py.newString(msg) orelse {
        c.PyErr_Clear();
        return;
    };
    defer c.Py_DecRef(py_msg);

    const args = c.PyTuple_Pack(1, py_msg) orelse {
        c.PyErr_Clear();
        return;
    };
    defer c.Py_DecRef(args);

    // logger.error(msg, exc_info=<exc>) — logging accepts an exception instance
    // for exc_info and renders its traceback. When there is no live exception
    // (e.g. a handler returned a non-tuple), omit exc_info and log the message.
    var call_kwargs: ?*c.PyObject = null;
    if (pvalue) |ev| {
        if (c.PyDict_New()) |kw| {
            call_kwargs = kw;
            _ = c.PyDict_SetItemString(kw, "exc_info", ev); // increfs ev
        }
    }
    defer if (call_kwargs) |kw| c.Py_DecRef(kw);

    if (c.PyObject_Call(meth, args, call_kwargs)) |r| {
        c.Py_DecRef(r);
    } else {
        c.PyErr_Clear(); // logging raised — swallow, keep the 500 path clean
    }
}

/// Build a `(ip: str, port: int)` tuple from the connected socket's peer
/// address for the Python `_peer` hook (app.py `_build_native_scope`), so
/// `client_ip`/`peer_ip` resolve to the real remote address instead of
/// collapsing every production client to 127.0.0.1. GIL MUST be held. Returns a
/// NEW reference (caller owns) or null when the address can't be determined
/// (getpeername failure, or a non-IPv4 family — the listener is AF_INET only).
fn buildPeerTuple(fd: std.posix.fd_t) ?*c.PyObject {
    var addr: std.c.sockaddr.in = std.mem.zeroes(std.c.sockaddr.in);
    var addr_len: std.posix.socklen_t = @sizeOf(std.c.sockaddr.in);
    if (std.c.getpeername(fd, @ptrCast(&addr), &addr_len) != 0) return null;
    if (addr.family != std.c.AF.INET) return null;

    // addr.addr is the in_addr (network byte order); its 4 bytes are the dotted
    // quad in order. Port is network-order too.
    const octets = std.mem.asBytes(&addr.addr);
    var ip_buf: [16]u8 = undefined;
    const ip = std.fmt.bufPrint(&ip_buf, "{d}.{d}.{d}.{d}", .{ octets[0], octets[1], octets[2], octets[3] }) catch return null;
    const port = std.mem.bigToNative(u16, addr.port);

    const py_ip = py.newString(ip) orelse return null;
    const py_port = c.PyLong_FromLong(@intCast(port)) orelse {
        c.Py_DecRef(py_ip);
        return null;
    };
    const tup = c.PyTuple_New(2) orelse {
        c.Py_DecRef(py_ip);
        c.Py_DecRef(py_port);
        return null;
    };
    // PyTuple_SetItem STEALS both references — no decref of py_ip/py_port after.
    _ = c.PyTuple_SetItem(tup, 0, py_ip);
    _ = c.PyTuple_SetItem(tup, 1, py_port);
    return tup;
}

fn callPythonHandler(tstate: ?*anyopaque, entry: HandlerEntry, method: []const u8, path: []const u8, query_string: []const u8, body: []const u8, headers: []const HeaderPair, params: *const router_mod.RouteParams, multipart_boundary: ?[]const u8, stream_content_length: usize, peer_fd: std.posix.fd_t) PythonResponse {
    const err_body = "{\"detail\":\"Internal Server Error\",\"status\":500}";
    const err_ct = "application/json";

    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    // Request context for the native error bridge — "METHOD /path". Built once
    // and reused at every failure site below (see reportNativeError).
    var rctx_buf: [256]u8 = undefined;
    const rctx = std.fmt.bufPrint(&rctx_buf, "{s} {s}", .{ method, path }) catch path;

    // Pre-parse multipart if boundary was detected (post-GIL, needs Python API).
    // A null return means the parser raised (boundary >252B, or a part
    // name/filename/content-type that isn't valid str, or OOM). Left uncleared,
    // that pending exception would ride into the vectorcall below and poison the
    // reused tstate for the NEXT request (F2). Clear it and answer a clean 400
    // (invalid multipart payload) instead of dispatching with a live error.
    var mp_parts: ?*c.PyObject = null;
    if (multipart_boundary) |boundary| {
        mp_parts = multipart.parseMultipartFromBuffer(body, boundary);
        if (mp_parts == null) {
            c.PyErr_Clear();
            return badRequestResponse("{\"detail\":\"Invalid multipart payload\",\"status\":400}");
        }
    }
    defer if (mp_parts) |mp| c.Py_DecRef(mp);

    // ── Build the POSITIONAL argument vector for PyObject_Vectorcall (Part 5) ──
    // The enhanced wrapper (app.py `_wrap_handler_for_zig.wrapper`) now takes a
    // fixed positional signature instead of **kwargs, so we skip the per-request
    // PyDict_New + ~9 PyDict_SetItemString + empty-tuple, and Python skips the
    // matching `.get()` re-reads. Argument order MUST match the wrapper:
    //   (method, path, body, query_string, headers, path_params,
    //    multipart_parts, stream_content_length, peer, headers_lowercased)
    // We create each object (owned ref) or use immortal None/True as a borrowed
    // placeholder; Vectorcall BORROWS the args (does not steal), so every owned
    // ref is released via its defer after the call.
    // Peer-supplied bytes never raise UnicodeDecodeError and never leave a pending
    // exception on the reused tstate (F1). Method/query/headers use Latin-1
    // (matching request.py from_asgi); the path uses UTF-8+surrogateescape
    // (matching a real ASGI server's already-decoded scope["path"], F3).
    const py_method = newStrLossless(method) orelse return errorResponse(err_ct, err_body);
    defer c.Py_DecRef(py_method);
    // Percent-decode the request path to match ASGI's already-decoded scope["path"]
    // (F3). A URL path uses `+` literally, so `+`→space is disabled here. Decoded
    // length ≤ raw length; decode into a request-arena buffer, falling back to the
    // raw path only if the (tiny) allocation fails.
    const decoded_path: []const u8 = blk: {
        const pbuf = reqAllocator().alloc(u8, path.len) catch break :blk path;
        break :blk percentDecode(path, pbuf, false);
    };
    const py_path = newStrPathUtf8(decoded_path) orelse return errorResponse(err_ct, err_body);
    defer c.Py_DecRef(py_path);
    const py_body = c.PyBytes_FromStringAndSize(@ptrCast(body.ptr), @intCast(body.len)) orelse return errorResponse(err_ct, err_body);
    defer c.Py_DecRef(py_body);
    const py_qs = newStrLossless(query_string) orelse return errorResponse(err_ct, err_body);
    defer c.Py_DecRef(py_qs);

    // ── headers dict — keys LOWERCASED here (Part 6) via an ASCII byte op in the
    // request arena, so the Python side adopts the dict directly
    // (CaseInsensitiveDict._adopt_lowercased) instead of re-lowering every key.
    // HTTP field-names are ASCII tokens, so ASCII lowering == Python str.lower().
    const py_headers = c.PyDict_New() orelse return errorResponse(err_ct, err_body);
    defer c.Py_DecRef(py_headers);
    const hdr_arena = reqAllocator();
    for (headers) |h| {
        const lname = asciiLowerName(hdr_arena, h.name);
        // Lossless Latin-1 (F1 + ASGI parity): a non-UTF-8 header name/value must
        // never raise / leak a pending exception into the vectorcall below.
        const hk = newStrLossless(lname) orelse continue;
        const hv = newStrLossless(h.value) orelse {
            c.Py_DecRef(hk);
            continue;
        };
        _ = c.PyDict_SetItem(py_headers, hk, hv);
        c.Py_DecRef(hk);
        c.Py_DecRef(hv);
    }

    // ── path_params dict — typed when param metadata is available ──
    const py_path_params = buildTypedPathParams(&entry, params) orelse return errorResponse(err_ct, err_body);
    defer c.Py_DecRef(py_path_params);

    // ── streaming content length (int) or None ──
    var py_scl: ?*c.PyObject = null;
    if (stream_content_length > 0) {
        py_scl = c.PyLong_FromLongLong(@intCast(stream_content_length)) orelse return errorResponse(err_ct, err_body);
    }
    defer if (py_scl) |o| c.Py_DecRef(o);

    // ── _peer = (ip, port) or None — the socket peer address for client_ip
    // parity. Best-effort: on any getpeername failure we pass None (Python falls
    // back to 127.0.0.1 exactly as before), so a hiccup never fails the request.
    const py_peer: ?*c.PyObject = buildPeerTuple(peer_fd); // NEW ref or null
    defer if (py_peer) |o| c.Py_DecRef(o);

    var args = [10]?*c.PyObject{
        py_method,
        py_path,
        py_body,
        py_qs,
        py_headers,
        py_path_params,
        mp_parts orelse @as(?*c.PyObject, @ptrCast(&c._Py_NoneStruct)),
        py_scl orelse @as(?*c.PyObject, @ptrCast(&c._Py_NoneStruct)),
        py_peer orelse @as(?*c.PyObject, @ptrCast(&c._Py_NoneStruct)),
        @as(?*c.PyObject, @ptrCast(&c._Py_TrueStruct)), // headers_lowercased = True (trusted native dict)
    };

    var result = py.PyObject_Vectorcall(entry.handler, @as([*]const ?*c.PyObject, @ptrCast(&args)), args.len, null) orelse {
        reportNativeError(rctx);
        return errorResponse(err_ct, err_body);
    };
    defer c.Py_DecRef(result);

    // ── Async handler support: resolve coroutine via the cached runner ──
    if (c.PyCoro_CheckExact(result) != 0) {
        const run_fn = getCoroRunner() orelse {
            reportNativeError(rctx);
            return errorResponse(err_ct, err_body);
        };
        const run_args = c.PyTuple_Pack(1, result) orelse return errorResponse(err_ct, err_body);
        defer c.Py_DecRef(run_args);
        const awaited = c.PyObject_CallObject(run_fn, run_args) orelse {
            reportNativeError(rctx);
            return errorResponse(err_ct, err_body);
        };
        // Replace result with the awaited value
        c.Py_DecRef(result);
        result = awaited;
    }

    // ── Extract response fields from returned tuple ──
    // Contract: (status:int, content_type:str, body:bytes, extra_headers:str|None).
    // Mirrors sendTupleResponse: index access + PyBytes_AsStringAndSize, so the
    // body is read as raw bytes — no decode in Python, no re-encode here.
    if (c.PyTuple_Check(result) == 0) {
        reportNativeError(rctx);
        return errorResponse(err_ct, err_body);
    }
    const tuple_len = c.PyTuple_Size(result);

    // status_code (default 200). Clear a pending exception if the object was not
    // a valid int (PyLong_AsLong → -1 + exception) so it can't poison the next
    // Python call on this thread; out-of-range stays at the 200 default.
    var status_code: u16 = 200;
    if (tuple_len > 0) {
        if (c.PyTuple_GetItem(result, 0)) |sc| {
            const code = c.PyLong_AsLong(sc);
            if (code == -1 and c.PyErr_Occurred() != null) {
                c.PyErr_Clear();
            } else if (code >= 100 and code <= 599) {
                status_code = @intCast(code);
            }
        }
    }

    // content_type (default "application/json")
    var ct_slice: []const u8 = "application/json";
    if (tuple_len > 1) {
        if (c.PyTuple_GetItem(result, 1)) |ct_obj| {
            if (asUtf8OrClear(ct_obj)) |cs| ct_slice = std.mem.span(cs);
        }
    }

    // body — bytes (preferred); str tolerated defensively.
    var body_slice: []const u8 = "";
    if (tuple_len > 2) {
        if (c.PyTuple_GetItem(result, 2)) |body_obj| {
            if (c.PyBytes_Check(body_obj) != 0) {
                var size: c.Py_ssize_t = 0;
                var buf: [*c]u8 = undefined;
                if (c.PyBytes_AsStringAndSize(body_obj, @ptrCast(&buf), &size) == 0) {
                    body_slice = buf[0..@intCast(size)];
                }
            } else if (c.PyUnicode_Check(body_obj) != 0) {
                if (asUtf8OrClear(body_obj)) |cs| body_slice = std.mem.span(cs);
            }
        }
    }

    // extra_headers — pre-formatted "\r\nKey: Value" string, or None.
    var eh_slice: []const u8 = "";
    if (tuple_len > 3) {
        if (c.PyTuple_GetItem(result, 3)) |eh_obj| {
            if (!py.isNone(eh_obj) and c.PyUnicode_Check(eh_obj) != 0) {
                if (asUtf8OrClear(eh_obj)) |cs| eh_slice = std.mem.span(cs);
            }
        }
    }

    // ── Streaming detection ──
    // The wrapper returns a 5-tuple (status, ct, b"", extra_headers, pull) for a
    // chunked/streaming Response (Response.stream / .sse / large file). The 5th
    // slot is a no-arg callable that yields the next chunk one step at a time.
    // We take an OWNED reference to it (survives the `result` decref below) and
    // hand it to the dispatch site, which drives sendChunkedResponse. body stays
    // empty; Content-Length is never framed for a stream (Transfer-Encoding:
    // chunked is emitted instead).
    var stream_pull: ?*c.PyObject = null;
    if (tuple_len >= 5) {
        if (c.PyTuple_GetItem(result, 4)) |pull_obj| {
            if (!py.isNone(pull_obj) and c.PyCallable_Check(pull_obj) != 0) {
                c.Py_IncRef(pull_obj);
                stream_pull = pull_obj;
            }
        }
    }

    // ── Return PythonResponse with owned copies ──
    const owned_ct = allocator.dupe(u8, ct_slice) catch {
        if (stream_pull) |sp| c.Py_DecRef(sp);
        return errorResponse(err_ct, err_body);
    };
    // A streaming response carries no buffered body — skip the (empty) dupe.
    //
    // ── Why the body is COPIED here, and why that is not worth "fixing" ──────
    // `body_slice` points INTO the PyBytes owned by `result`, and `result` is
    // decref'd by this function's `defer` — which runs BEFORE the deferred
    // `PyEval_ReleaseThread`, i.e. while the GIL is still held. The caller then
    // sends and calls `resp.deinit()` with the GIL RELEASED, so the response
    // bytes must outlive every Python object in this frame. Hence an owned copy.
    // Keeping the PyObject alive instead would mean either re-acquiring the GIL
    // after the send purely to decref (a second attach/detach per request), or
    // staying attached across the socket write (serialising all Python work in
    // the process behind one thread's write) — both cost more than the copy.
    //
    // MEASURED (bench box: 2x EPYC 7702, glibc 2.43, performance governor,
    // node0-pinned, reactor W=16 — reproduce with `zig/bench/bench_body_alloc.zig`
    // for the component and `scripts/bench_body_dupe_budget.py` for the budget):
    //   * the malloc/free pair alone (what a per-thread retained buffer would
    //     remove, mirroring main.zig's `json_scratch`) costs 27 ns/request at
    //     64 KiB, 57 ns/request with realistic allocator churn — FLAT in body
    //     size (32 ns at 256 KiB, 27 ns at 1 MiB) and NOT contended: it holds
    //     ~30 ns from 1 to 16 threads. Unlike jsonWriteString's 6x
    //     over-reservation, this block never crosses glibc's mmap threshold, so
    //     there is no mmap/munmap-per-request signature to recover.
    //   * a whole 64 KiB request costs 140.7 us of server CPU (128.3 us on
    //     /jsoncached; 618.8 us at 256 KiB), measured as utime+stime per served
    //     request, not inferred from rps.
    //   => removing the allocator half is worth <=0.04% of a request. Removing
    //      the copy ENTIRELY (the memcpy is 1.26 us at 64 KiB) would be worth
    //      ~0.9% — still under the +-4 percentage-point noise floor of the
    //      64 KiB wire cell, and only reachable via the GIL round-trip above.
    // Separating 0.04% from that floor would take ~10^5 interleaved wire rounds
    // per arm (weeks of machine time). This is closed: do not re-open it without
    // a NEW load shape in which the per-request budget is orders of magnitude
    // smaller than 140 us, which is where a flat ~30 ns could ever matter.
    const owned_body = if (stream_pull != null) @as([]const u8, "") else allocator.dupe(u8, body_slice) catch {
        allocator.free(owned_ct);
        return errorResponse(err_ct, err_body);
    };
    const owned_eh = if (eh_slice.len > 0) allocator.dupe(u8, eh_slice) catch "" else @as([]const u8, "");

    return PythonResponse{
        .status_code = status_code,
        .content_type = owned_ct,
        .body = owned_body,
        .extra_headers = owned_eh,
        .stream_pull = stream_pull,
    };
}

fn errorResponse(ct: []const u8, body_str: []const u8) PythonResponse {
    const owned_ct = allocator.dupe(u8, ct) catch return PythonResponse{
        .status_code = 500,
        .content_type = &.{},
        .body = &.{},
    };
    const owned_body = allocator.dupe(u8, body_str) catch {
        allocator.free(owned_ct);
        return PythonResponse{
            .status_code = 500,
            .content_type = &.{},
            .body = &.{},
        };
    };
    return PythonResponse{
        .status_code = 500,
        .content_type = owned_ct,
        .body = owned_body,
    };
}

/// Owned 400 response with a JSON body and application/json content-type. Mirrors
/// errorResponse's allocation discipline (dupe both, free on partial failure) but
/// with a 400 status — used for peer-invalid payloads (e.g. bad multipart, F2).
fn badRequestResponse(body_str: []const u8) PythonResponse {
    const owned_ct = allocator.dupe(u8, "application/json") catch return PythonResponse{
        .status_code = 400,
        .content_type = &.{},
        .body = &.{},
    };
    const owned_body = allocator.dupe(u8, body_str) catch {
        allocator.free(owned_ct);
        return PythonResponse{
            .status_code = 400,
            .content_type = &.{},
            .body = &.{},
        };
    };
    return PythonResponse{
        .status_code = 400,
        .content_type = owned_ct,
        .body = owned_body,
    };
}

/// Extract boundary value from Content-Type header.
/// Handles both `boundary=value` and `boundary="value"` (quoted).
/// Returns a slice into the input — no allocation.
fn extractBoundaryFromContentType(ct: []const u8) ?[]const u8 {
    // Scan for "boundary=" (case-insensitive)
    var pos: usize = 0;
    while (pos + 9 <= ct.len) : (pos += 1) {
        if (std.ascii.eqlIgnoreCase(ct[pos..][0..9], "boundary=")) {
            var start = pos + 9;
            // Quoted: boundary="..."
            if (start < ct.len and ct[start] == '"') {
                start += 1;
                const end = std.mem.indexOfScalarPos(u8, ct, start, '"') orelse ct.len;
                return ct[start..end];
            }
            // Unquoted: boundary ends at ; or end of string
            const end = std.mem.indexOfScalarPos(u8, ct, start, ';') orelse ct.len;
            return std.mem.trim(u8, ct[start..end], " \t");
        }
    }
    return null;
}

fn statusText(status: u16) []const u8 {
    return switch (status) {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        301 => "Moved Permanently",
        302 => "Found",
        304 => "Not Modified",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        422 => "Unprocessable Entity",
        429 => "Too Many Requests",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        else => "Unknown",
    };
}

/// Zero-alloc response writer.  Header + body are concatenated into a stack
/// buffer for a single write syscall (most API responses are <4KB).
/// Falls back to two writes only for large responses.
pub fn sendResponse(stream: py.NetStream, status: u16, content_type: []const u8, body: []const u8) void {
    // Native server metrics — one atomic increment per response (~35 ns).
    // The counter is null when metrics haven't been initialized (WSGI mode
    // or before the first request triggers initServerMetrics).
    if (loadCounter(&_srv_responses_counter)) |cnt| cnt.inc(1);
    bumpStatusClass(status);

    // TFB requires Server + Date headers. Date is served from the 1-second cache.
    var date_buf: [HTTP_DATE_LEN]u8 = undefined;
    httpDate(&date_buf);

    // Reject CR/LF in the handler-controlled content_type (header injection).
    var ct_buf: [256]u8 = undefined;
    const safe_ct = sanitizeHeaderValue(content_type, &ct_buf);

    // 1xx/204/304 carry no body and no Content-Length; HEAD keeps the
    // Content-Length a GET would send but no body bytes.
    const bodyless = statusForbidsBody(status);
    const suppress_body = bodyless or _req_is_head;

    // Built segment-by-segment so an empty content_type omits the header entirely
    // (a bare "Content-Type:" line is malformed — hit by the CORS 204 preflight)
    // and bodyless statuses omit Content-Length.
    var header_buf: [512]u8 = undefined;
    var hlen: usize = 0;
    hlen += (std.fmt.bufPrint(
        header_buf[hlen..],
        "HTTP/1.1 {d} {s}\r\nServer: HyperDjango\r\nDate: {s}",
        .{ status, statusText(status), date_buf[0..] },
    ) catch return).len;
    if (safe_ct.len > 0) {
        hlen += (std.fmt.bufPrint(header_buf[hlen..], "\r\nContent-Type: {s}", .{safe_ct}) catch return).len;
    }
    if (!bodyless) {
        hlen += (std.fmt.bufPrint(header_buf[hlen..], "\r\nContent-Length: {d}", .{body.len}) catch return).len;
    }
    hlen += (std.fmt.bufPrint(header_buf[hlen..], "\r\nConnection: {s}", .{connectionHeaderValue()}) catch return).len;
    const header = header_buf[0..hlen];

    // Single writev: [header][cors][trailer][body] — no body copy, one syscall.
    // cors_headers is "" when disabled and is skipped by writeAllVectored.
    const out_body: []const u8 = if (suppress_body) "" else body;
    stream.writeAllVectored(&.{ header, cors_headers, security_headers, "\r\n\r\n", out_body }) catch {
        noteWriteFailure();
    };
}

/// Send an HTTP response with arbitrary headers (for Django full middleware responses).
/// headers_str is pre-formatted "\r\nKey: Value\r\nKey: Value" string.
pub fn sendFullResponse(stream: py.NetStream, status: u16, extra_headers: []const u8, body: []const u8) void {
    // Count full responses too (previously only sendResponse + the static path
    // bumped the total, undercounting Django/extra-header responses).
    if (loadCounter(&_srv_responses_counter)) |cnt| cnt.inc(1);
    bumpStatusClass(status);

    var date_buf: [HTTP_DATE_LEN]u8 = undefined;
    httpDate(&date_buf);

    // Guard the handler-provided header block against response splitting (a blank
    // line embedded in it would terminate the headers early and inject a body).
    const safe_headers = sanitizeHeaderBlock(extra_headers);

    // 1xx/204/304 carry no body and no Content-Length; HEAD keeps the
    // Content-Length a GET would send but no body bytes.
    const bodyless = statusForbidsBody(status);
    const suppress_body = bodyless or _req_is_head;

    // Status line + Server + Date + (Content-Length) + Connection. Zig owns
    // framing — handler.py strips Django's own Content-Length/Connection so these
    // are the only copies on the wire. Connection is emitted here too (previously
    // omitted), so HTTP/1.0 keep-alive is echoed and `close` is advertised.
    var status_buf: [256]u8 = undefined;
    var slen: usize = 0;
    slen += (std.fmt.bufPrint(
        status_buf[slen..],
        "HTTP/1.1 {d} {s}\r\nServer: HyperDjango\r\nDate: {s}",
        .{ status, statusText(status), date_buf[0..] },
    ) catch return).len;
    if (!bodyless) {
        slen += (std.fmt.bufPrint(status_buf[slen..], "\r\nContent-Length: {d}", .{body.len}) catch return).len;
    }
    slen += (std.fmt.bufPrint(status_buf[slen..], "\r\nConnection: {s}", .{connectionHeaderValue()}) catch return).len;
    const status_line = status_buf[0..slen];

    // Single writev: [status_line][extra_headers][trailer][body] — no body copy,
    // no heap allocation, one syscall regardless of size. Empty extra_headers is
    // skipped by writeAllVectored.
    const out_body: []const u8 = if (suppress_body) "" else body;
    stream.writeAllVectored(&.{ status_line, safe_headers, "\r\n\r\n", out_body }) catch {
        noteWriteFailure();
    };
}

/// Send a `Transfer-Encoding: chunked` streaming response, driving a Python pull
/// callable ONE CHUNK AT A TIME. This is the real chunked-send path that replaced
/// the old "materialize the whole stream into a buffer" hack — an infinite SSE /
/// heartbeat stream is now memory-bounded and incremental (first bytes reach the
/// client immediately) instead of draining a worker forever or OOMing on a large
/// finite stream.
///
/// GIL discipline (the crux): the caller MUST NOT hold the GIL. The GIL is
/// acquired ONLY to step the generator (`pull_fn()`), and RELEASED across every
/// socket write — so a slow/stalled client back-pressures the generator (the
/// write blocks, or times out via SO_SNDTIMEO → WriteFailed → we close) and other
/// workers keep running Python. A Python bytes chunk's buffer is used directly
/// while the GIL is released by KEEPING its reference alive across the write
/// (bytes are immutable and never relocate), avoiding a per-chunk copy.
///
/// `content_type` "" means Content-Type is already carried inside `extra_headers`
/// (the Django path). `pull_fn` is an OWNED reference that this function CONSUMES
/// (decrefs before returning). `pull_fn()` returns the next chunk as `bytes`
/// (empty bytes are skipped — NOT a stream terminator), or None (StopAsyncIteration
/// → end of stream); a raised exception aborts the response and closes.
fn sendChunkedResponse(
    stream: py.NetStream,
    tstate: ?*anyopaque,
    status: u16,
    content_type: []const u8,
    extra_headers: []const u8,
    pull_fn: *c.PyObject,
) void {
    if (loadCounter(&_srv_responses_counter)) |cnt| cnt.inc(1);
    bumpStatusClass(status);

    var date_buf: [HTTP_DATE_LEN]u8 = undefined;
    httpDate(&date_buf);

    const safe_headers = sanitizeHeaderBlock(extra_headers);
    var ct_buf: [256]u8 = undefined;
    const safe_ct = sanitizeHeaderValue(content_type, &ct_buf);

    // ── Header block: status line + Server + Date + [Content-Type] ──
    var head_buf: [512]u8 = undefined;
    var hlen: usize = 0;
    hlen += (std.fmt.bufPrint(
        head_buf[hlen..],
        "HTTP/1.1 {d} {s}\r\nServer: HyperDjango\r\nDate: {s}",
        .{ status, statusText(status), date_buf[0..] },
    ) catch {
        // Even the fixed prefix didn't fit (impossible) — release pull + bail.
        py.PyEval_AcquireThread(tstate);
        c.Py_DecRef(pull_fn);
        py.PyEval_ReleaseThread(tstate);
        noteWriteFailure();
        return;
    }).len;
    if (safe_ct.len > 0) {
        if (std.fmt.bufPrint(head_buf[hlen..], "\r\nContent-Type: {s}", .{safe_ct})) |w| {
            hlen += w.len;
        } else |_| {}
    }
    const head_prefix = head_buf[0..hlen];
    // Trailer carries the framing headers Zig owns for a stream.
    var trailer_buf: [128]u8 = undefined;
    const trailer = std.fmt.bufPrint(
        trailer_buf[0..],
        "\r\nTransfer-Encoding: chunked\r\nConnection: {s}\r\n\r\n",
        .{connectionHeaderValue()},
    ) catch "\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n";

    stream.writeAllVectored(&.{ head_prefix, safe_headers, trailer }) catch {
        noteWriteFailure();
        py.PyEval_AcquireThread(tstate);
        c.Py_DecRef(pull_fn);
        py.PyEval_ReleaseThread(tstate);
        return;
    };

    // HEAD: identical headers, ZERO body — never advance the generator.
    if (_req_is_head) {
        py.PyEval_AcquireThread(tstate);
        c.Py_DecRef(pull_fn);
        py.PyEval_ReleaseThread(tstate);
        return;
    }

    // ── Chunk loop ──  (GIL held only during pull; released across writes)
    var prev_chunk: ?*c.PyObject = null;
    while (true) {
        py.PyEval_AcquireThread(tstate);
        // Release the previous chunk (its buffer was used by the prior write,
        // now complete) before pulling the next.
        if (prev_chunk) |pc| {
            c.Py_DecRef(pc);
            prev_chunk = null;
        }
        const chunk = c.PyObject_CallNoArgs(pull_fn);
        if (chunk == null) {
            // Generator raised mid-stream — report and abort WITHOUT a 0-length
            // terminator so the client sees a truncated (closed) response, not a
            // clean end. noteWriteFailure forces the connection closed.
            reportNativeError("native stream");
            py.PyEval_ReleaseThread(tstate);
            noteWriteFailure();
            break;
        }
        if (py.isNone(chunk.?)) {
            c.Py_DecRef(chunk.?);
            py.PyEval_ReleaseThread(tstate);
            // Normal end of stream → terminating zero-length chunk.
            stream.writeAll("0\r\n\r\n") catch noteWriteFailure();
            break;
        }
        // Extract the bytes buffer. Keep the reference across the write.
        var size: c.Py_ssize_t = 0;
        var bufp: [*c]u8 = undefined;
        if (c.PyBytes_Check(chunk.?) == 0 or
            c.PyBytes_AsStringAndSize(chunk.?, @ptrCast(&bufp), &size) != 0)
        {
            // Not bytes (pull contract violated) — clear any error, abort.
            c.PyErr_Clear();
            c.Py_DecRef(chunk.?);
            py.PyEval_ReleaseThread(tstate);
            noteWriteFailure();
            break;
        }
        prev_chunk = chunk; // hold the ref alive while the GIL is released
        py.PyEval_ReleaseThread(tstate);

        const dlen: usize = @intCast(size);
        if (dlen == 0) continue; // empty chunk: skip (a 0-frame would end the stream)

        var size_line: [24]u8 = undefined;
        const sl = std.fmt.bufPrint(&size_line, "{x}\r\n", .{dlen}) catch {
            noteWriteFailure();
            break;
        };
        stream.writeAllVectored(&.{ sl, bufp[0..dlen], "\r\n" }) catch {
            noteWriteFailure();
            break;
        };
    }

    // Cleanup under the GIL: release the last held chunk (if any) + the pull ref.
    py.PyEval_AcquireThread(tstate);
    if (prev_chunk) |pc| c.Py_DecRef(pc);
    c.Py_DecRef(pull_fn);
    py.PyEval_ReleaseThread(tstate);
}

// ── Django handler: receive full request, call Python WSGI handler, send full response ──

/// _server_set_django_handler(handler) — register a Python callable as the Django WSGI dispatcher.
/// The handler receives (method, path, headers_dict, body_bytes, query_string)
/// and returns (status_code, headers_str, body_bytes).
/// headers_str is pre-formatted: "\r\nContent-Type: text/html\r\nSet-Cookie: ..."
var django_handler: ?*c.PyObject = null;

pub fn server_set_django_handler(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handler_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O", &handler_obj) == 0) return null;

    // Release old handler if any
    if (django_handler) |old| {
        c.Py_DecRef(old);
    }
    c.Py_IncRef(handler_obj.?);
    django_handler = handler_obj;

    return py.pyNone();
}

/// Called from handleOneRequest when no route matches and django_handler is set.
/// This is the Django catch-all: every unmatched request goes through Django's middleware chain.
fn dispatchDjango(stream: py.NetStream, tstate: ?*anyopaque, method: []const u8, path: []const u8, query_string: []const u8, body: []const u8, headers: []const HeaderPair) void {
    const handler = django_handler orelse {
        // Unified {"detail","status"} contract (see the no-route fast path).
        sendResponse(stream, 404, "application/json", "{\"detail\":\"Not Found\",\"status\":404}");
        return;
    };

    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);

    // Build args: (method, path, headers_dict, body_bytes, query_string).
    // Every wire-derived string uses lossless Latin-1 (newStrLossless): a
    // non-UTF-8 method/path/header/query must never raise UnicodeDecodeError and
    // leave a pending exception riding into the handler call — the same F1 leak
    // fixed on the native handler path — and Latin-1 matches the Django/WSGI/ASGI
    // header decoding convention.
    const py_method = newStrLossless(method) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(py_method);

    const py_path = newStrLossless(path) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(py_path);

    const py_headers = c.PyDict_New() orelse {
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(py_headers);
    for (headers) |h| {
        const hk = newStrLossless(h.name) orelse {
            c.PyErr_Clear();
            continue;
        };
        const hv = newStrLossless(h.value) orelse {
            c.Py_DecRef(hk);
            c.PyErr_Clear();
            continue;
        };
        _ = c.PyDict_SetItem(py_headers, hk, hv);
        c.Py_DecRef(hk);
        c.Py_DecRef(hv);
    }

    const py_body = c.PyBytes_FromStringAndSize(@ptrCast(body.ptr), @intCast(body.len)) orelse {
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(py_body);

    const py_qs = newStrLossless(query_string) orelse {
        c.PyErr_Clear();
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(py_qs);

    // Call handler(method, path, headers, body, query_string)
    const call_args = c.PyTuple_Pack(5, py_method, py_path, py_headers, py_body, py_qs) orelse {
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(call_args);

    const result = c.PyObject_CallObject(handler, call_args) orelse {
        var dctx_buf: [256]u8 = undefined;
        const dctx = std.fmt.bufPrint(&dctx_buf, "django {s} {s}", .{ method, path }) catch "django handler";
        reportNativeError(dctx);
        sendResponse(stream, 500, "text/plain", "Internal Server Error");
        return;
    };
    defer c.Py_DecRef(result);

    // Unpack (status_code, headers_str, body_bytes) OR the streaming variant
    // (status_code, headers_str, b"", pull) — a 4-tuple whose 4th slot is a no-arg
    // callable yielding the next chunk (StreamingHttpResponse / FileResponse).
    const tsize = if (c.PyTuple_Check(result) != 0) c.PyTuple_Size(result) else -1;
    if (tsize != 3 and tsize != 4) {
        sendResponse(stream, 500, "text/plain", "Django handler must return (status, headers_str, body_bytes)");
        return;
    }

    const py_status = c.PyTuple_GetItem(result, 0);
    const py_resp_headers = c.PyTuple_GetItem(result, 1);

    // headers_str is pre-formatted "\r\nKey: Value\r\nKey: Value"
    var dj_headers_slice: []const u8 = "";
    if (c.PyUnicode_Check(py_resp_headers) != 0) {
        if (asUtf8OrClear(py_resp_headers)) |cs| dj_headers_slice = std.mem.span(cs);
    }
    const dj_status: u16 = blk: {
        const code = c.PyLong_AsLong(py_status);
        if (code >= 100 and code <= 599) break :blk @intCast(code);
        break :blk 200;
    };

    // ── Streaming Django response → chunked send ──
    if (tsize == 4) {
        const py_pull = c.PyTuple_GetItem(result, 3);
        if (py_pull != null and !py.isNone(py_pull.?) and c.PyCallable_Check(py_pull.?) != 0) {
            c.Py_IncRef(py_pull.?);
            // Content-Type is already inside dj_headers_slice (format_response
            // includes it), so pass "" for content_type. sendChunkedResponse
            // self-manages the GIL per pull and requires it RELEASED on entry, so
            // release here and reacquire after so the top-level defer balances.
            // dj_headers_slice stays valid: we hold `result` (deferred decref).
            py.PyEval_ReleaseThread(tstate);
            sendChunkedResponse(stream, tstate, dj_status, "", dj_headers_slice, py_pull.?);
            py.PyEval_AcquireThread(tstate);
            return;
        }
        sendResponse(stream, 500, "text/plain", "Django streaming tuple: 4th element must be callable");
        return;
    }

    const py_resp_body = c.PyTuple_GetItem(result, 2);

    // body is bytes
    var resp_body_slice: []const u8 = "";
    if (c.PyBytes_Check(py_resp_body) != 0) {
        const bptr = c.PyBytes_AsString(py_resp_body);
        const blen: usize = @intCast(c.PyBytes_Size(py_resp_body));
        if (bptr != null) {
            resp_body_slice = @as([*]const u8, @ptrCast(bptr))[0..blen];
        }
    } else if (c.PyUnicode_Check(py_resp_body) != 0) {
        if (asUtf8OrClear(py_resp_body)) |cs| {
            resp_body_slice = std.mem.span(cs);
        }
    }

    sendFullResponse(stream, dj_status, dj_headers_slice, resp_body_slice);
}

// ── Fuzz tests ───────────────────────────────────────────────────────────────
// Run: zig build fuzz-http  (then execute the binary with --fuzz)
//
// These tests exercise the parsing functions used by handleOneRequest.
// The invariants are: no panics, no out-of-bounds access, bounded output.

/// Bridge the Zig 0.16 `std.testing.fuzz` `*Smith` callback to a byte slice:
/// replay a concrete corpus entry verbatim (`smith.in`), or draw an
/// arbitrary-length byte string when actively fuzzing (`in == null`). `buf`
/// backs the active-fuzz draw and must outlive the returned slice.
fn fuzzInput(smith: *std.testing.Smith, buf: []u8) []const u8 {
    if (smith.in) |in| return in;
    return buf[0..smith.sliceWithHash(buf, 0)];
}

fn fuzz_percentDecode(_: void, smith: *std.testing.Smith) anyerror!void {
    var in_buf: [4096]u8 = undefined;
    const input = fuzzInput(smith, &in_buf);
    var buf: [4096]u8 = undefined;
    const out = percentDecode(input, &buf, true);
    // Decoded output is never longer than percent-encoded input
    try std.testing.expect(out.len <= input.len);
    // Output must fit in buffer
    try std.testing.expect(out.len <= buf.len);
    // Output must be a subslice of buf
    const buf_start = @intFromPtr(&buf);
    const buf_end = buf_start + buf.len;
    const out_start = @intFromPtr(out.ptr);
    try std.testing.expect(out_start >= buf_start and out_start <= buf_end);
}

test "fuzz: percentDecode — output bounded, no OOB" {
    try std.testing.fuzz({}, fuzz_percentDecode, .{
        .corpus = &.{
            "%00", // null byte
            "%GG", // invalid hex digits
            "%", // bare percent at end of input
            "%2", // truncated percent sequence
            "hello+world", // plus → space
            "a%20b%20c", // spaces
            "%FF%FE%FD", // high bytes
            &([_]u8{'%'} ** 200), // 200 bare percents
            "%2F%2F..%2F..%2Fetc%2Fpasswd", // path traversal
            "%00%00%00", // three null bytes
        },
    });
}

fn fuzz_queryStringGet(_: void, smith: *std.testing.Smith) anyerror!void {
    var in_buf: [4096]u8 = undefined;
    const input = fuzzInput(smith, &in_buf);
    // Split: first 16 bytes = key, remainder = query string
    const split = @min(input.len, 16);
    const key = input[0..split];
    const qs = if (split < input.len) input[split..] else "";

    const result = queryStringGet(qs, key);
    if (result) |v| {
        // Returned slice must be within the query string buffer
        const qs_start = @intFromPtr(qs.ptr);
        const qs_end = qs_start + qs.len;
        const v_start = @intFromPtr(v.ptr);
        try std.testing.expect(v_start >= qs_start and v_start <= qs_end);
    }
}

test "fuzz: queryStringGet — result is within input, no panic" {
    try std.testing.fuzz({}, fuzz_queryStringGet, .{
        .corpus = &.{
            "key" ++ "key=value",
            "x" ++ "x=1&y=2&z=3",
            "a" ++ "a=&b=c",
            "k" ++ "k",
            "" ++ "=value",
            "foo" ++ "foo=bar&foo=baz", // duplicate key
            "q" ++ "q=" ++ ("A" ** 2000), // very long value
            "k" ++ "k=\x00\xFF", // binary values
            "k" ++ "&&&&&", // no values, only separators
        },
    });
}

fn fuzz_requestLineParsing(_: void, smith: *std.testing.Smith) anyerror!void {
    var in_buf: [8192]u8 = undefined;
    const input = fuzzInput(smith, &in_buf);
    if (input.len == 0) return;

    // The parser searches for \r\n\r\n to delimit headers from body.
    // If absent → server returns 431 and stops. We mirror that.
    const he = std.mem.indexOf(u8, input, "\r\n\r\n") orelse return;

    // Parse the first line (request line).
    const first_line_end = std.mem.indexOf(u8, input[0..he], "\r\n") orelse return;
    const first_line = input[0..first_line_end];

    var parts = std.mem.splitScalar(u8, first_line, ' ');
    const method = parts.next() orelse return;
    const raw_path = parts.next() orelse return;
    _ = method;

    // Split path from query string at '?'
    const q_idx = std.mem.indexOf(u8, raw_path, "?");
    const path = if (q_idx) |i| raw_path[0..i] else raw_path;
    const query_string = if (q_idx) |i| raw_path[i + 1 ..] else "";
    _ = path;
    _ = query_string;

    // Parse headers — real function, same file
    const request_head = input[0 .. he + 4];
    var headers = parseHeaders(allocator, request_head, first_line_end, he);
    defer headers.deinit(allocator);

    // Validate Content-Length parsing on adversarial values
    for (headers.items) |h| {
        if (std.ascii.eqlIgnoreCase(h.name, "content-length")) {
            const cl = std.fmt.parseInt(usize, h.value, 10) catch 0;
            _ = @min(cl, server_max_body_size);
        }
    }
}

test "fuzz: HTTP request-line and header parsing — no panic on malformed input" {
    try std.testing.fuzz({}, fuzz_requestLineParsing, .{
        .corpus = &.{
            // Minimal valid GET
            "GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
            // Valid POST with body
            "POST /items HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
            // Missing HTTP version token
            "GET /\r\n\r\n",
            // Empty method
            " / HTTP/1.1\r\n\r\n",
            // Huge Content-Length (parser must cap it)
            "POST / HTTP/1.1\r\nContent-Length: 99999999999999999999\r\n\r\n",
            // Negative Content-Length (parseInt → error → 0)
            "POST / HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
            // CRLF injection attempt in header value
            "GET / HTTP/1.1\r\nX-Header: value\r\nInjected: header\r\n\r\n",
            // Header with no colon (should be skipped)
            "GET / HTTP/1.1\r\nMalformedHeaderLine\r\n\r\n",
            // Null byte in path
            "GET /\x00secret HTTP/1.1\r\n\r\n",
            // Very long path (> 8KB header buffer)
            "GET /" ++ ("a" ** 7000) ++ " HTTP/1.1\r\n\r\n",
            // Very long header value
            "GET / HTTP/1.1\r\nX-Custom: " ++ ("B" ** 7000) ++ "\r\n\r\n",
            // Bare \n instead of \r\n
            "GET / HTTP/1.1\nHost: x\n\n",
            // No path at all
            "GET HTTP/1.1\r\n\r\n",
            // Method with no space
            "GETHTTP/1.1\r\n\r\n",
            // Percent-encoded path
            "GET /users%2F42 HTTP/1.1\r\n\r\n",
            // Query string with adversarial chars
            "GET /search?q=%00&limit=-1&page=\xFF HTTP/1.1\r\n\r\n",
        },
    });
}

// ── Unit tests: single-pass request-head scan ───────────────────────────────
// These lock in the folded scan (framing + WebSocket + content-type + range)
// that replaced scanFraming + isWebSocketUpgrade + the per-route re-scans.

fn scanFixture(comptime head: []const u8) RequestScan {
    const fle = std.mem.indexOf(u8, head, "\r\n").?;
    const he = std.mem.indexOf(u8, head, "\r\n\r\n").?;
    // scanRequestHead fills a caller-owned HeaderViews (added in the #37 redesign);
    // the fixture only asserts on framing/upgrade/content-type/range, so a throwaway
    // local suffices.
    var views: HeaderViews = .{};
    return scanRequestHead(head, fle, he, &views);
}

test "scanRequestHead: plain GET has no body, no upgrade" {
    const s = scanFixture("GET /x HTTP/1.1\r\nHost: a\r\n\r\n");
    try std.testing.expect(s.framing == .ok and s.framing.ok == 0);
    try std.testing.expect(!s.is_ws_upgrade);
    try std.testing.expectEqualStrings("", s.range);
}

test "scanRequestHead: Content-Length parsed, Content-Type + Range captured" {
    const s = scanFixture("POST /x HTTP/1.1\r\nContent-Length: 5\r\nContent-Type: application/json\r\nRange: bytes=0-9\r\n\r\n");
    try std.testing.expect(s.framing == .ok and s.framing.ok == 5);
    try std.testing.expectEqualStrings("application/json", s.content_type);
    try std.testing.expectEqualStrings("bytes=0-9", s.range);
}

test "scanRequestHead: duplicate Content-Length rejected 400" {
    const s = scanFixture("POST /x HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n");
    try std.testing.expect(s.framing == .reject and s.framing.reject.status == 400);
}

test "scanRequestHead: Transfer-Encoding rejected 501" {
    const s = scanFixture("POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n");
    try std.testing.expect(s.framing == .reject and s.framing.reject.status == 501);
}

test "scanRequestHead: TE + CL rejected 400 (smuggling)" {
    const s = scanFixture("POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 3\r\n\r\n");
    try std.testing.expect(s.framing == .reject and s.framing.reject.status == 400);
}

test "scanRequestHead: WebSocket upgrade detected (comma-separated Connection)" {
    const s = scanFixture("GET /ws HTTP/1.1\r\nConnection: keep-alive, Upgrade\r\nUpgrade: websocket\r\n\r\n");
    try std.testing.expect(s.is_ws_upgrade);
}

test "scanRequestHead: upgrade detection is complete even past a framing reject" {
    // Duplicate CL comes before the upgrade headers; upgrade must still be seen
    // (the caller branches on is_ws_upgrade before acting on the framing reject).
    const s = scanFixture("GET /ws HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n");
    try std.testing.expect(s.is_ws_upgrade);
    try std.testing.expect(s.framing == .reject);
}

test "scanRequestHead: Content-Length must be 1*DIGIT — rejects _, +, - (smuggling)" {
    // Zig parseInt accepts '_' separators and a leading sign; RFC 7230 does not.
    inline for (.{ "1_0", "+5", "-0", "5 5" }) |bad| {
        const s = scanFixture("POST /x HTTP/1.1\r\nContent-Length: " ++ bad ++ "\r\n\r\n");
        try std.testing.expect(s.framing == .reject and s.framing.reject.status == 400);
    }
    // Plain digits (incl. leading zeros, and OWS the parser trims) stay valid.
    const ok = scanFixture("POST /x HTTP/1.1\r\nContent-Length: 007\r\n\r\n");
    try std.testing.expect(ok.framing == .ok and ok.framing.ok == 7);
    const ows = scanFixture("POST /x HTTP/1.1\r\nContent-Length:  5 \r\n\r\n");
    try std.testing.expect(ows.framing == .ok and ows.framing.ok == 5);
}

test "scanRequestHead: whitespace before colon rejected 400 (smuggling)" {
    const s = scanFixture("POST /x HTTP/1.1\r\nContent-Length : 5\r\n\r\n");
    try std.testing.expect(s.framing == .reject and s.framing.reject.status == 400);
}

test "scanRequestHead: obs-fold continuation line rejected 400" {
    // A header line beginning with SP/HT is an obs-fold; we reject rather than
    // silently drop it.
    const s = scanFixture("POST /x HTTP/1.1\r\nX-Test: a\r\n\tfolded\r\nContent-Length: 0\r\n\r\n");
    try std.testing.expect(s.framing == .reject and s.framing.reject.status == 400);
}

// ── Native-serving-path parity helpers (native-wave) ────────────────────────

test "needsAppendSlashRedirect: request without slash, pattern with slash → redirect" {
    // handler_key is "METHOD /pattern"; a trailing '/' means the route pattern had one.
    try std.testing.expect(needsAppendSlashRedirect("/posts", "GET /posts/"));
    try std.testing.expect(needsAppendSlashRedirect("/a/b", "GET /a/b/"));
}

test "needsAppendSlashRedirect: no redirect when path already ends in slash" {
    // Zig collapses the trailing slash, so /posts/ matches "GET /posts/" — but the
    // request path already has the slash, so ASGI serves directly (no 301).
    try std.testing.expect(!needsAppendSlashRedirect("/posts/", "GET /posts/"));
}

test "needsAppendSlashRedirect: no redirect when pattern has no trailing slash" {
    try std.testing.expect(!needsAppendSlashRedirect("/posts", "GET /posts"));
    try std.testing.expect(!needsAppendSlashRedirect("/posts/", "GET /posts"));
}

test "needsAppendSlashRedirect: HEAD-aliased match uses the GET pattern key" {
    // A HEAD request that fell through to the GET route carries handler_key
    // "GET /posts/", so the append-slash decision is identical to a GET.
    try std.testing.expect(needsAppendSlashRedirect("/posts", "GET /posts/"));
}

test "needsAppendSlashRedirect: root and empty edge cases never redirect" {
    try std.testing.expect(!needsAppendSlashRedirect("/", "GET /"));
    try std.testing.expect(!needsAppendSlashRedirect("", "GET /"));
    try std.testing.expect(!needsAppendSlashRedirect("/x", ""));
}

test "bumpStatusClass: increments only the matching class counter" {
    // Route each class through a freshly-registered set of counters and assert
    // exactly one bumped. Uses the real registration + inc path.
    _srv_resp_2xx_counter = registerDynCounter("test_native_responses_2xx_total", "");
    _srv_resp_3xx_counter = registerDynCounter("test_native_responses_3xx_total", "");
    _srv_resp_4xx_counter = registerDynCounter("test_native_responses_4xx_total", "");
    _srv_resp_5xx_counter = registerDynCounter("test_native_responses_5xx_total", "");
    // Skip if the registry is full (registration returns null) — nothing to assert.
    if (_srv_resp_2xx_counter == null or _srv_resp_4xx_counter == null) return;

    const c2 = _srv_resp_2xx_counter.?;
    const c4 = _srv_resp_4xx_counter.?;
    const before2 = c2.value.load(.monotonic);
    const before4 = c4.value.load(.monotonic);
    bumpStatusClass(200);
    bumpStatusClass(201);
    bumpStatusClass(404);
    try std.testing.expectEqual(before2 + 2, c2.value.load(.monotonic));
    try std.testing.expectEqual(before4 + 1, c4.value.load(.monotonic));
    // A 1xx status is not tallied in any class bucket.
    bumpStatusClass(100);
    try std.testing.expectEqual(before2 + 2, c2.value.load(.monotonic));
}

test "autoWorkers: preserves the historic default on small machines, scales up on big ones" {
    // At or below the historic default (24), auto never sizes DOWN — small
    // machines are byte-for-byte unchanged.
    try std.testing.expectEqual(@as(usize, 24), autoWorkers(1));
    try std.testing.expectEqual(@as(usize, 24), autoWorkers(12));
    try std.testing.expectEqual(@as(usize, 24), autoWorkers(24));
    // Above the default, workers track the budget…
    try std.testing.expectEqual(@as(usize, 64), autoWorkers(64));
    try std.testing.expectEqual(@as(usize, 128), autoWorkers(128));
    try std.testing.expectEqual(@as(usize, 512), autoWorkers(512));
    // …up to the auto ceiling (override to exceed).
    try std.testing.expectEqual(@as(usize, WORKER_AUTO_CEILING), autoWorkers(1024));
}

test "autoReactors: 1 shard through the single-queue knee, then one queue per WORKERS_PER_REACTOR" {
    // Every currently-validated config (workers ≤ 32) stays on ONE reactor —
    // the historic default — so this change cannot regress them.
    try std.testing.expectEqual(@as(usize, 1), autoReactors(1));
    try std.testing.expectEqual(@as(usize, 1), autoReactors(24));
    try std.testing.expectEqual(@as(usize, 1), autoReactors(32));
    // Past the knee, shards scale with workers so no single queue serves more
    // than WORKERS_PER_REACTOR workers.
    try std.testing.expectEqual(@as(usize, 2), autoReactors(64));
    try std.testing.expectEqual(@as(usize, 4), autoReactors(128));
    try std.testing.expectEqual(@as(usize, 16), autoReactors(512));
    // Capped at MAX_REACTOR_COUNT.
    try std.testing.expectEqual(@as(usize, MAX_REACTOR_COUNT), autoReactors(WORKERS_PER_REACTOR * MAX_REACTOR_COUNT * 4));
}

test "scaleCores: only finite fractions in (0,1] scale; garbage falls back to all cores" {
    try std.testing.expectEqual(@as(usize, 64), scaleCores(128, 0.5));
    try std.testing.expectEqual(@as(usize, 128), scaleCores(128, 1.0));
    try std.testing.expectEqual(@as(usize, 1), scaleCores(128, 0.001)); // floored at 1
    // Out-of-range / nonsense → all cores (never 0, never NaN via @intFromFloat).
    try std.testing.expectEqual(@as(usize, 128), scaleCores(128, 0));
    try std.testing.expectEqual(@as(usize, 128), scaleCores(128, 2.0));
    try std.testing.expectEqual(@as(usize, 128), scaleCores(128, -0.5));
}

test "in-flight cells: per-worker deltas sum to the total, and never report negative" {
    var cells = [_]InflightCell{.{}} ** 4;
    inflight_cells = &cells;
    defer inflight_cells = &.{};

    inflightAdd(0, 1);
    inflightAdd(2, 1);
    inflightAdd(3, 1);
    try std.testing.expectEqual(@as(u64, 3), activeRequests());
    inflightAdd(0, -1);
    inflightAdd(2, -1);
    try std.testing.expectEqual(@as(u64, 1), activeRequests());
    inflightAdd(3, -1);
    try std.testing.expectEqual(@as(u64, 0), activeRequests());
    // A transient cross-cell negative (a decrement observed before its paired
    // increment) clamps to 0 rather than underflowing.
    inflightAdd(1, -1);
    try std.testing.expectEqual(@as(u64, 0), activeRequests());
}

test "InflightCell is cache-line sized so per-worker cells never false-share" {
    try std.testing.expectEqual(CACHE_LINE, @sizeOf(InflightCell));
    try std.testing.expectEqual(CACHE_LINE, @alignOf(InflightCell));
}
