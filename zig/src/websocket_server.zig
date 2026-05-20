// websocket_server.zig — RFC 6455 WebSocket protocol for the Zig HTTP server.
//
// Handles:
//   - HTTP upgrade handshake (101 Switching Protocols)
//   - Frame parsing (text, binary, ping, pong, close)
//   - Frame writing (no masking for server→client)
//   - Client frame unmasking (XOR with 4-byte key)
//   - Python handler bridge via C API

const std = @import("std");
const py = @import("py.zig");
const metrics = @import("metrics_py.zig");
const c = py.c;

const Sha1 = std.crypto.hash.Sha1;

const allocator = std.heap.c_allocator;

// ── Native observability ────────────────────────────────────────────────────
// A always-on counter (visible in the Prometheus scrape) for frames the
// server rejected as malformed/non-conformant — unmasked client frames,
// oversized/fragmented control frames, over-limit payloads. Lets operators
// see hostile or broken clients; a rising rate is a signal, not noise.
var _ws_metrics_init_flag: std.atomic.Value(u8) = std.atomic.Value(u8).init(0);
var _ws_rejected_counter: ?*metrics.DynCounter = null;

fn initWsMetrics() void {
    if (@cmpxchgStrong(u8, &_ws_metrics_init_flag.raw, 0, 1, .acquire, .monotonic) != null) return;
    if (metrics.DynCounter.init()) |counter| {
        const name = allocator.dupe(u8, "hyperdjango_ws_frames_rejected_total") catch return;
        const help = allocator.dupe(u8, "WebSocket frames rejected as malformed/non-conformant (RFC 6455 violations or over-limit).") catch return;
        const entry = allocator.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .counter, .name = name, .help = help, .impl = counter };
        _ = metrics.registerEntry(entry) catch return;
        _ws_rejected_counter = counter;
    } else |_| {}
}

/// Disable Nagle's algorithm on a connection's socket (IPPROTO_TCP=6,
/// TCP_NODELAY=1 — same numeric values on Linux and macOS). Best-effort:
/// a WebSocket connection is still fully functional without it, just
/// with slightly higher latency on small frames, so a failure here isn't
/// fatal to the connection.
fn setTcpNoDelay(stream: py.NetStream) void {
    var optval: c_int = 1;
    _ = std.c.setsockopt(stream.handle, @as(c_int, std.posix.IPPROTO.TCP), @as(c_int, std.posix.TCP.NODELAY), &optval, @sizeOf(c_int));
}

// ── WebSocket Frame Opcodes ──────────────────────────────────────────────────

pub const Opcode = enum(u4) {
    continuation = 0x0,
    text = 0x1,
    binary = 0x2,
    close = 0x8,
    ping = 0x9,
    pong = 0xA,
    _,
};

pub const Frame = struct {
    fin: bool,
    rsv1: bool = false, // Used by permessage-deflate
    opcode: Opcode,
    payload: []const u8,
    owned: bool = false,

    pub fn deinit(self: *Frame) void {
        if (self.owned) {
            allocator.free(self.payload);
        }
    }
};

// ── WebSocket Configuration ──────────────────────────────────────────────────

/// Maximum message size (bytes). Configurable via _server_set_ws_config.
pub var max_message_size: u64 = 16 * 1024 * 1024; // 16 MB default

/// Server-initiated ping interval (nanoseconds). 0 = disabled.
/// Default 30s — standard WebSocket keepalive interval.
pub var ping_interval_ns: i128 = 30 * std.time.ns_per_s;

/// Pong timeout — close connection if no pong received within this time.
/// Default 120s — generous for mobile/wireless/laptop lid-close scenarios.
/// Most clients reconnect automatically after brief network interruptions.
pub var pong_timeout_ns: i128 = 120 * std.time.ns_per_s;

// ── WebSocket Handshake ──────────────────────────────────────────────────────

const WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// Check if an HTTP request is a WebSocket upgrade request.
/// Scans headers for Connection: Upgrade and Upgrade: websocket.
pub fn isWebSocketUpgrade(headers: []const u8) bool {
    var has_upgrade = false;
    var has_websocket = false;

    var line_it = std.mem.splitSequence(u8, headers, "\r\n");
    while (line_it.next()) |line| {
        if (line.len == 0) break;
        if (std.mem.indexOf(u8, line, ":")) |colon| {
            const name = std.mem.trim(u8, line[0..colon], " ");
            const value = std.mem.trim(u8, line[colon + 1 ..], " ");

            if (std.ascii.eqlIgnoreCase(name, "connection")) {
                // Connection header can be comma-separated: "keep-alive, Upgrade"
                var val_it = std.mem.splitScalar(u8, value, ',');
                while (val_it.next()) |part| {
                    if (std.ascii.eqlIgnoreCase(std.mem.trim(u8, part, " "), "upgrade")) {
                        has_upgrade = true;
                    }
                }
            }
            if (std.ascii.eqlIgnoreCase(name, "upgrade")) {
                if (std.ascii.eqlIgnoreCase(value, "websocket")) {
                    has_websocket = true;
                }
            }
        }
    }

    return has_upgrade and has_websocket;
}

/// Extract Sec-WebSocket-Key from headers.
pub fn getWebSocketKey(headers: []const u8) ?[]const u8 {
    var line_it = std.mem.splitSequence(u8, headers, "\r\n");
    while (line_it.next()) |line| {
        if (line.len == 0) break;
        if (std.mem.indexOf(u8, line, ":")) |colon| {
            const name = std.mem.trim(u8, line[0..colon], " ");
            if (std.ascii.eqlIgnoreCase(name, "sec-websocket-key")) {
                return std.mem.trim(u8, line[colon + 1 ..], " ");
            }
        }
    }
    return null;
}

/// Compute the Sec-WebSocket-Accept value from the client's key.
pub fn computeAcceptKey(client_key: []const u8) [28]u8 {
    // SHA-1(client_key + magic)
    var hasher = Sha1.init(.{});
    hasher.update(client_key);
    hasher.update(WS_MAGIC);
    const hash = hasher.finalResult();

    // Base64 encode the 20-byte hash → 28 chars
    var result: [28]u8 = undefined;
    _ = std.base64.standard.Encoder.encode(&result, &hash);
    return result;
}

/// Select the WebSocket subprotocol to echo in the handshake.
/// RFC 6455 §4.2.2: the server MUST select AT MOST ONE of the client's offered
/// subprotocols. The client sends a comma-separated preference list; we pick the
/// first offered token (a reasonable default preference order) and return only
/// that single value, so the handshake never echoes the whole list back (which
/// would be a conformance violation). Returns null if the header is absent or
/// carries no non-empty token.
pub fn getWebSocketProtocol(headers: []const u8) ?[]const u8 {
    var line_it = std.mem.splitSequence(u8, headers, "\r\n");
    while (line_it.next()) |line| {
        if (line.len == 0) break;
        if (std.mem.indexOf(u8, line, ":")) |colon| {
            const name = std.mem.trim(u8, line[0..colon], " ");
            if (std.ascii.eqlIgnoreCase(name, "sec-websocket-protocol")) {
                const raw = std.mem.trim(u8, line[colon + 1 ..], " ");
                // Take only the first comma-separated token — exactly one.
                var proto_it = std.mem.splitScalar(u8, raw, ',');
                const first = std.mem.trim(u8, proto_it.next() orelse raw, " ");
                if (first.len == 0) return null;
                return first;
            }
        }
    }
    return null;
}

/// Extract Sec-WebSocket-Extensions header.
pub fn getWebSocketExtensions(headers: []const u8) ?[]const u8 {
    var line_it = std.mem.splitSequence(u8, headers, "\r\n");
    while (line_it.next()) |line| {
        if (line.len == 0) break;
        if (std.mem.indexOf(u8, line, ":")) |colon| {
            const name = std.mem.trim(u8, line[0..colon], " ");
            if (std.ascii.eqlIgnoreCase(name, "sec-websocket-extensions")) {
                return std.mem.trim(u8, line[colon + 1 ..], " ");
            }
        }
    }
    return null;
}

/// Send the 101 Switching Protocols handshake response.
/// Optionally includes Sec-WebSocket-Protocol and Sec-WebSocket-Extensions.
pub fn sendHandshake(stream: py.NetStream, client_key: []const u8) !void {
    try sendHandshakeEx(stream, client_key, null, false);
}

/// Extended handshake with subprotocol and extension negotiation.
pub fn sendHandshakeEx(
    stream: py.NetStream,
    client_key: []const u8,
    subprotocol: ?[]const u8,
    deflate: bool,
) !void {
    const accept_key = computeAcceptKey(client_key);

    var buf: [512]u8 = undefined;
    var pos: usize = 0;

    // Base response
    const base = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ";
    @memcpy(buf[pos..][0..base.len], base);
    pos += base.len;
    @memcpy(buf[pos..][0..28], &accept_key);
    pos += 28;
    @memcpy(buf[pos..][0..2], "\r\n");
    pos += 2;

    // Optional subprotocol
    if (subprotocol) |proto| {
        const prefix = "Sec-WebSocket-Protocol: ";
        @memcpy(buf[pos..][0..prefix.len], prefix);
        pos += prefix.len;
        const plen = @min(proto.len, buf.len - pos - 4);
        @memcpy(buf[pos..][0..plen], proto[0..plen]);
        pos += plen;
        @memcpy(buf[pos..][0..2], "\r\n");
        pos += 2;
    }

    // Optional permessage-deflate
    if (deflate) {
        const ext = "Sec-WebSocket-Extensions: permessage-deflate; server_no_context_takeover; client_no_context_takeover\r\n";
        @memcpy(buf[pos..][0..ext.len], ext);
        pos += ext.len;
    }

    // Final CRLF
    @memcpy(buf[pos..][0..2], "\r\n");
    pos += 2;

    try stream.writeAll(buf[0..pos]);
}

// ── Frame Reading ────────────────────────────────────────────────────────────

/// Read a single WebSocket frame from the stream.
/// Client frames MUST be masked (RFC 6455 Section 5.1).
pub fn readFrame(stream: py.NetStream) !Frame {
    // Read first 2 bytes: FIN/opcode + mask/length
    var header: [2]u8 = undefined;
    try readExact(stream, &header);

    const fin = (header[0] & 0x80) != 0;
    const rsv1 = (header[0] & 0x40) != 0;
    const opcode: Opcode = @enumFromInt(@as(u4, @truncate(header[0] & 0x0F)));
    const masked = (header[1] & 0x80) != 0;
    var payload_len: u64 = header[1] & 0x7F;

    // RFC 6455 §5.2: RSV1/2/3 MUST be zero unless an extension negotiated their
    // use. The server no longer advertises permessage-deflate (it never
    // decompressed the payload), so any reserved bit set is a protocol error.
    if ((header[0] & 0x70) != 0) return error.ProtocolError;

    // RFC 6455 §5.1: client→server frames MUST be masked.
    if (!masked) return error.UnmaskedClientFrame;
    // RFC 6455 §5.5: control frames MUST be ≤125 bytes and unfragmented.
    const is_control = (@intFromEnum(opcode) & 0x8) != 0;
    if (is_control and (!fin or payload_len > 125)) return error.InvalidControlFrame;

    // Extended payload length
    if (payload_len == 126) {
        var ext: [2]u8 = undefined;
        try readExact(stream, &ext);
        payload_len = std.mem.readInt(u16, &ext, .big);
        // RFC 6455 §5.2: minimal length encoding required (mirrors
        // parseFrameFromBuffer) — a length <126 must not use the 16-bit form.
        if (payload_len < 126) return error.ProtocolError;
    } else if (payload_len == 127) {
        var ext: [8]u8 = undefined;
        try readExact(stream, &ext);
        payload_len = std.mem.readInt(u64, &ext, .big);
        // §5.2: the 64-bit form is valid only for lengths >65535.
        if (payload_len <= 65535) return error.ProtocolError;
    }

    // Limit frame size (configurable, default 16MB)
    if (payload_len > max_message_size) {
        return error.FrameTooLarge;
    }

    // Masked frame (enforced above): read the 4-byte masking key.
    var mask_key: [4]u8 = undefined;
    try readExact(stream, &mask_key);

    // Read payload
    const len: usize = @intCast(payload_len);
    const payload = try allocator.alloc(u8, len);
    errdefer allocator.free(payload);

    if (len > 0) {
        try readExact(stream, payload);
        unmaskPayload(payload, mask_key); // always masked (enforced above)
    }

    return Frame{
        .fin = fin,
        .rsv1 = rsv1,
        .opcode = opcode,
        .payload = payload,
        .owned = true,
    };
}

// ── Non-blocking frame reading (buffer + MSG_DONTWAIT) ──────────────────────
//
// Bridges the receive path into asyncio without a thread-hop: parse
// whatever's already buffered, and if a full frame isn't available yet,
// let the caller wait for readability (via asyncio's add_reader) and try
// again, rather than blocking a thread on `readExact`. See ws_try_recv
// and hyperdjango/websocket.py's ZigWebSocket._recv_one.

const BufferParseResult = union(enum) {
    frame: struct { frame: Frame, consumed: usize },
    incomplete,
    fatal, // protocol violation, frame too large, or allocation failure
};

/// Upper bound on recv-buffer capacity retained between frames. Small frames
/// keep their (small) buffer for reuse; a connection that received one large
/// frame releases the oversized allocation once drained instead of pinning it
/// for its whole lifetime. Comfortably above the 16 KB read chunk so typical
/// traffic never triggers a realloc.
const RECV_BUF_RETAIN_CAP: usize = 64 * 1024;

/// Parse one frame from an in-memory buffer, WITHOUT consuming/copying out
/// of `buf` — returns how many bytes were consumed so the caller can slide
/// its buffer. Mirrors readFrame's wire format exactly, operating on
/// already-available bytes instead of blocking reads.
fn parseFrameFromBuffer(buf: []const u8) BufferParseResult {
    if (buf.len < 2) return .incomplete;

    const fin = (buf[0] & 0x80) != 0;
    const rsv1 = (buf[0] & 0x40) != 0;
    const opcode: Opcode = @enumFromInt(@as(u4, @truncate(buf[0] & 0x0F)));
    const masked = (buf[1] & 0x80) != 0;
    var payload_len: u64 = buf[1] & 0x7F;
    var pos: usize = 2;

    // RFC 6455 §5.2: any set reserved bit is a protocol violation (no extension
    // is negotiated — permessage-deflate is no longer advertised). Fatal → close.
    if ((buf[0] & 0x70) != 0) return .fatal;

    // RFC 6455 §5.1: every client→server frame MUST be masked. Reject
    // unmasked frames (fatal → close) rather than processing bytes a
    // conformant client would never send.
    if (!masked) return .fatal;

    // RFC 6455 §5.5: control frames (close/ping/pong) MUST have a payload
    // ≤125 bytes and MUST NOT be fragmented. Enforce before trusting the
    // length — a 16 MB "ping" would otherwise be buffered and echoed back.
    const is_control = (@intFromEnum(opcode) & 0x8) != 0;
    if (is_control and (!fin or payload_len > 125)) return .fatal;

    if (payload_len == 126) {
        if (buf.len < pos + 2) return .incomplete;
        payload_len = std.mem.readInt(u16, buf[pos..][0..2], .big);
        pos += 2;
        // RFC 6455 §5.2: minimal length encoding is required — a length <126
        // MUST use the 7-bit form, not the 16-bit form. Reject (Autobahn strict).
        if (payload_len < 126) return .fatal;
    } else if (payload_len == 127) {
        if (buf.len < pos + 8) return .incomplete;
        payload_len = std.mem.readInt(u64, buf[pos..][0..8], .big);
        pos += 8;
        // §5.2: the 64-bit form is valid only for lengths >65535.
        if (payload_len <= 65535) return .fatal;
    }

    if (payload_len > max_message_size) return .fatal;

    // Masked frame: read the 4-byte masking key.
    if (buf.len < pos + 4) return .incomplete;
    const mask_key: [4]u8 = buf[pos..][0..4].*;
    pos += 4;

    const len: usize = @intCast(payload_len);
    if (buf.len < pos + len) return .incomplete;

    const payload = allocator.alloc(u8, len) catch return .fatal;
    if (len > 0) {
        @memcpy(payload, buf[pos..][0..len]);
        unmaskPayload(payload, mask_key); // always masked (enforced above)
    }
    pos += len;

    return .{ .frame = .{
        .frame = .{ .fin = fin, .rsv1 = rsv1, .opcode = opcode, .payload = payload, .owned = true },
        .consumed = pos,
    } };
}

const TryRecvResult = union(enum) {
    frame: Frame,
    would_block,
    closed, // clean EOF / peer went away
    rejected, // malformed/non-conformant frame (protocol violation or over-limit)
};

/// Get the next complete frame if one is already parseable from
/// `conn.recv_buf`; otherwise attempt non-blocking reads (MSG_DONTWAIT) to
/// pull in more bytes, looping until a full frame is available, the socket
/// would block, or the connection is closed. A single readable notification
/// can contain several frames' worth of bytes, so this drains everything
/// currently available before reporting would_block.
fn tryRecvFrame(conn: *WsConn) TryRecvResult {
    var read_buf: [16384]u8 = undefined;
    while (true) {
        // conn.recv_pos is a read cursor: bytes [0, recv_pos) in recv_buf
        // are already-consumed frames we haven't reclaimed yet. Extracting
        // a frame just advances the cursor — no copy — so a single read
        // that contains a batch of several small frames costs zero memmoves
        // to drain. We only ever copy the (small) unconsumed remainder, and
        // only when we're about to block on needing more bytes from the
        // socket, not on every frame.
        switch (parseFrameFromBuffer(conn.recv_buf.items[conn.recv_pos..])) {
            .frame => |r| {
                conn.recv_pos += r.consumed;
                if (conn.recv_pos == conn.recv_buf.items.len) {
                    // Fully drained. Normally an O(1) reset that keeps the
                    // capacity for the next frame — but if a large frame grew
                    // this buffer past the retention cap, free that memory
                    // rather than pin it for the connection's whole life
                    // (matters under the shared model: many long-lived
                    // connections that each saw one big frame). The buffer
                    // manages its own footprint.
                    if (conn.recv_buf.capacity > RECV_BUF_RETAIN_CAP) {
                        conn.recv_buf.clearAndFree(allocator);
                    } else {
                        conn.recv_buf.clearRetainingCapacity();
                    }
                    conn.recv_pos = 0;
                }
                return .{ .frame = r.frame };
            },
            .fatal => return .rejected,
            .incomplete => {},
        }

        // Not enough buffered to complete a frame — reclaim the consumed
        // prefix (if any) before growing the buffer with a new read.
        if (conn.recv_pos > 0) {
            const remaining = conn.recv_buf.items[conn.recv_pos..];
            std.mem.copyForwards(u8, conn.recv_buf.items[0..remaining.len], remaining);
            conn.recv_buf.shrinkRetainingCapacity(remaining.len);
            conn.recv_pos = 0;
        }

        switch (conn.stream.tryRead(&read_buf)) {
            .would_block => return .would_block,
            .closed => return .closed,
            .n => |n| {
                conn.recv_buf.appendSlice(allocator, read_buf[0..n]) catch return .closed;
                // Loop back — either drain more of a large already-available
                // payload, or attempt to parse the now-larger buffer.
            },
        }
    }
}

/// Unmask a WebSocket payload using SIMD XOR when possible.
fn unmaskPayload(data: []u8, mask: [4]u8) void {
    // Expand mask to 16 bytes for SIMD
    const Block16 = @Vector(16, u8);
    const mask16: Block16 = .{
        mask[0], mask[1], mask[2], mask[3],
        mask[0], mask[1], mask[2], mask[3],
        mask[0], mask[1], mask[2], mask[3],
        mask[0], mask[1], mask[2], mask[3],
    };

    var i: usize = 0;

    // SIMD: 16 bytes at a time
    while (i + 16 <= data.len) {
        const chunk: Block16 = data[i..][0..16].*;
        const unmasked = chunk ^ mask16;
        data[i..][0..16].* = unmasked;
        i += 16;
    }

    // Scalar remainder
    while (i < data.len) {
        data[i] ^= mask[i % 4];
        i += 1;
    }
}

// ── Frame Writing ────────────────────────────────────────────────────────────

/// Serialize a server→client frame header (never masked) into `buf`, returning
/// its length (2, 4, or 10 bytes). Shared by the blocking writeFrame path and
/// the non-blocking send path (buildFramedRemainder / sendFramedNonblocking).
fn buildFrameHeader(buf: *[10]u8, opcode: Opcode, payload_len: usize, fin: bool) usize {
    buf[0] = @as(u8, if (fin) 0x80 else 0x00) | @as(u8, @intFromEnum(opcode));
    if (payload_len < 126) {
        buf[1] = @intCast(payload_len);
        return 2;
    } else if (payload_len <= 65535) {
        buf[1] = 126;
        std.mem.writeInt(u16, buf[2..4], @intCast(payload_len), .big);
        return 4;
    } else {
        buf[1] = 127;
        std.mem.writeInt(u64, buf[2..10], @intCast(payload_len), .big);
        return 10;
    }
}

/// Write a WebSocket frame to the stream (blocking).
/// Server frames are NOT masked. Used by the control-frame helpers
/// (sendClose/sendPong) and the blocking send fallback.
pub fn writeFrame(stream: py.NetStream, opcode: Opcode, payload: []const u8, fin: bool) !void {
    var header_buf: [10]u8 = undefined;
    const header_len = buildFrameHeader(&header_buf, opcode, payload.len, fin);
    // Single syscall for header + payload (was two separate writes).
    try stream.writeAllVectored2(header_buf[0..header_len], payload);
}

/// Send a text frame.
pub fn sendText(stream: py.NetStream, text: []const u8) !void {
    try writeFrame(stream, .text, text, true);
}

/// Send a binary frame.
pub fn sendBinary(stream: py.NetStream, data: []const u8) !void {
    try writeFrame(stream, .binary, data, true);
}

/// Send a close frame with optional status code and reason.
pub fn sendClose(stream: py.NetStream, code: ?u16, reason: []const u8) !void {
    if (code) |status_code| {
        var buf: [128]u8 = undefined;
        std.mem.writeInt(u16, buf[0..2], status_code, .big);
        const rlen = @min(reason.len, buf.len - 2);
        @memcpy(buf[2..][0..rlen], reason[0..rlen]);
        try writeFrame(stream, .close, buf[0 .. 2 + rlen], true);
    } else {
        try writeFrame(stream, .close, "", true);
    }
}

/// Send a pong frame (in response to a ping).
pub fn sendPong(stream: py.NetStream, payload: []const u8) !void {
    try writeFrame(stream, .pong, payload, true);
}

// ── Non-blocking send path (HOL-blocking fix) ───────────────────────────────
//
// The blocking ws_send* primitives (sendText/sendBinary under writevAll) treat
// EAGAIN — from an SO_SNDTIMEO timeout on a stalled zero-window consumer — as a
// write failure only after up to 30s. On the shared event-loop model that stalls
// EVERY connection multiplexed on that loop for the whole timeout. tryTakeSend
// mirrors the receive path (tryRecvFrame): a SINGLE MSG_DONTWAIT send, with any
// unsent remainder held in a per-connection outbound buffer + offset (symmetric
// to recv_buf/recv_pos). Frame ordering is preserved by the single buffer under
// write_mutex; the Python layer re-arms the write via asyncio add_writer and
// flushes when the socket is writable again — no blocking syscall on the loop
// thread, no extra threads.

/// Result of a non-blocking send/flush. Returned to Python as a small int.
const SendResult = enum(c_int) {
    sent = 0, // fully drained — nothing buffered
    would_block = 1, // partial/buffered — caller must add_writer and flush later
    shed = 2, // outbound buffer past the high-water mark — drop the connection (1013)
    closed = 3, // fd write error / connection already dead — disconnect
};

/// Free an oversized outbound buffer once drained instead of pinning it for the
/// connection's lifetime (matches RECV_BUF_RETAIN_CAP on the receive side).
const SEND_BUF_RETAIN_CAP: usize = 64 * 1024;

/// Outbound high-water mark. Once this many un-sent bytes are queued for a slow
/// consumer, further whole messages are refused (shed) rather than buffered
/// without bound — so one stuck consumer is dropped, never allowed to grow
/// memory without limit or stall its peers. Overridable via env for tests.
var ws_send_high_water_cached: std.atomic.Value(usize) = std.atomic.Value(usize).init(0);
fn sendHighWater() usize {
    const cached = ws_send_high_water_cached.load(.acquire);
    if (cached != 0) return cached;
    var hw: usize = 8 * 1024 * 1024; // 8 MB of queued backlog per connection
    if (std.c.getenv("HYPER_WS_SEND_HIGH_WATER")) |env_ptr| {
        const s = std.mem.sliceTo(env_ptr, 0);
        if (std.fmt.parseInt(usize, s, 10)) |v| {
            if (v > 0) hw = v;
        } else |_| {}
    }
    ws_send_high_water_cached.store(hw, .release);
    return hw;
}

/// Reclaim the already-sent prefix of the outbound buffer, then append the
/// framed message at the true end (preserving order). Assumes write_mutex held.
fn appendFramed(conn: *WsConn, header: []const u8, payload: []const u8) !void {
    if (conn.out_pos > 0) {
        const remaining = conn.out_buf.items[conn.out_pos..];
        std.mem.copyForwards(u8, conn.out_buf.items[0..remaining.len], remaining);
        conn.out_buf.shrinkRetainingCapacity(remaining.len);
        conn.out_pos = 0;
    }
    try conn.out_buf.appendSlice(allocator, header);
    try conn.out_buf.appendSlice(allocator, payload);
}

/// Flush the buffered outbound remainder with non-blocking sends. Assumes
/// write_mutex held (and the caller has released the GIL). Advances out_pos and,
/// on full drain, resets the buffer (freeing it if it grew past the retain cap).
fn flushOutboundLocked(conn: *WsConn) SendResult {
    while (conn.out_pos < conn.out_buf.items.len) {
        switch (conn.stream.trySend(conn.out_buf.items[conn.out_pos..])) {
            .n => |n| {
                if (n == 0) return .closed; // no progress → broken transport
                conn.out_pos += n;
            },
            .would_block => return .would_block,
            .closed => return .closed,
        }
    }
    if (conn.out_buf.capacity > SEND_BUF_RETAIN_CAP) {
        conn.out_buf.clearAndFree(allocator);
    } else {
        conn.out_buf.clearRetainingCapacity();
    }
    conn.out_pos = 0;
    return .sent;
}

/// Frame `payload` under `opcode` and send it non-blocking, buffering any
/// remainder. Assumes write_mutex held, GIL released, conn.alive checked.
fn sendFramedLocked(conn: *WsConn, opcode: Opcode, payload: []const u8) SendResult {
    var header_buf: [10]u8 = undefined;
    const hlen = buildFrameHeader(&header_buf, opcode, payload.len, true);

    // Nothing pending: attempt a direct vectored send; only the unsent tail is
    // copied into the buffer (zero copy in the common fully-sent case).
    if (conn.out_pos == conn.out_buf.items.len) {
        const total = hlen + payload.len;
        const iov = [2]std.posix.iovec_const{
            .{ .base = &header_buf, .len = hlen },
            .{ .base = if (payload.len == 0) &header_buf else payload.ptr, .len = payload.len },
        };
        switch (conn.stream.trySendv(iov[0 .. if (payload.len == 0) 1 else 2])) {
            .n => |n| {
                if (n == total) return .sent;
                // Partial write — buffer exactly the unsent remainder, spanning
                // the header/payload boundary. (out_buf is empty here.)
                if (n < hlen) {
                    conn.out_buf.appendSlice(allocator, header_buf[n..hlen]) catch return .closed;
                    conn.out_buf.appendSlice(allocator, payload) catch return .closed;
                } else {
                    conn.out_buf.appendSlice(allocator, payload[n - hlen ..]) catch return .closed;
                }
                conn.out_pos = 0;
                return .would_block;
            },
            .would_block => {
                appendFramed(conn, header_buf[0..hlen], payload) catch return .closed;
                return .would_block;
            },
            .closed => return .closed,
        }
    }

    // Data already queued for a slow consumer: preserve order by appending after
    // it. Enforce the high-water mark on the un-sent backlog before piling on —
    // we never shed mid-frame (that would desync the stream), only refuse whole
    // new messages once the backlog is too deep.
    const pending = conn.out_buf.items.len - conn.out_pos;
    if (pending + hlen + payload.len > sendHighWater()) return .shed;
    appendFramed(conn, header_buf[0..hlen], payload) catch return .closed;
    return flushOutboundLocked(conn);
}

/// Shared body for _ws_try_send / _ws_send_ping: look up the connection, frame
/// and non-blocking-send under the write mutex, and return the SendResult as an
/// int. On a shed/closed outcome the connection is marked dead so no further
/// send is attempted. Never blocks the calling (event-loop) thread.
fn wsTrySendImpl(conn_id: u64, opcode: Opcode, data: []const u8) ?*c.PyObject {
    const conn = lookupConnection(conn_id) orelse return py.newInt(@intFromEnum(SendResult.closed));
    defer unref(conn);

    conn.write_mutex.lock();
    if (!conn.alive.load(.acquire)) {
        conn.write_mutex.unlock();
        return py.newInt(@intFromEnum(SendResult.closed));
    }
    const saved = py.PyEval_SaveThread();
    const res = sendFramedLocked(conn, opcode, data);
    py.PyEval_RestoreThread(saved);
    if (res == .shed or res == .closed) conn.alive.store(false, .release);
    conn.write_mutex.unlock();

    return py.newInt(@intFromEnum(res));
}

/// Enqueue a reply frame (auto-pong / close-echo) through the SAME non-blocking
/// outbound path the data send uses, so a control-frame reply can NEVER
/// head-of-line-block the (shared) event-loop thread. Assumes the GIL is held;
/// takes the write mutex and releases the GIL around the single MSG_DONTWAIT
/// send. A slow/zero-window consumer buffers the frame (up to the high-water
/// mark) and drains later via the writer selector — it does not block here.
/// Returns true if the connection is still usable (sent or buffered), false if
/// the send shed at the high-water mark or the transport died (in which case the
/// connection has been marked dead). Mirrors the shed/closed handling in
/// wsTrySendImpl; the payload is fully sent or copied out before returning, so
/// the caller may free/`deinit` the frame immediately.
fn enqueueReplyNonblocking(conn: *WsConn, opcode: Opcode, payload: []const u8) bool {
    conn.write_mutex.lock();
    if (!conn.alive.load(.acquire)) {
        conn.write_mutex.unlock();
        return false;
    }
    const saved = py.PyEval_SaveThread();
    const res = sendFramedLocked(conn, opcode, payload);
    py.PyEval_RestoreThread(saved);
    const dead = (res == .shed or res == .closed);
    if (dead) conn.alive.store(false, .release);
    conn.write_mutex.unlock();
    return !dead;
}

/// _ws_try_send(conn_id, opcode, data) -> int (SendResult)
/// Non-blocking single send attempt. opcode is the RFC 6455 opcode (0x1 text,
/// 0x2 binary). Buffers any remainder and returns would_block so the caller can
/// register add_writer; returns shed if the per-connection backlog is too deep.
pub fn ws_try_send(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    var opcode_val: c_int = 0;
    var data_c: [*c]const u8 = null;
    var data_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Kiy#", &conn_id_val, &opcode_val, &data_c, &data_len) == 0) return null;
    const opcode: Opcode = @enumFromInt(@as(u4, @truncate(@as(u32, @bitCast(opcode_val)))));
    return wsTrySendImpl(conn_id_val, opcode, data_c[0..@intCast(data_len)]);
}

/// _ws_flush_send(conn_id) -> int (SendResult)
/// Flush the buffered outbound remainder (the asyncio add_writer callback).
/// Returns sent when fully drained, would_block if more remains, closed on a
/// transport error.
pub fn ws_flush_send(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    if (c.PyArg_ParseTuple(args, "K", &conn_id_val) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse return py.newInt(@intFromEnum(SendResult.closed));
    defer unref(conn);

    conn.write_mutex.lock();
    if (!conn.alive.load(.acquire)) {
        conn.write_mutex.unlock();
        return py.newInt(@intFromEnum(SendResult.closed));
    }
    const saved = py.PyEval_SaveThread();
    const res = flushOutboundLocked(conn);
    py.PyEval_RestoreThread(saved);
    if (res == .closed) conn.alive.store(false, .release);
    conn.write_mutex.unlock();

    return py.newInt(@intFromEnum(res));
}

/// _ws_send_ping(conn_id, payload) -> int (SendResult)
/// Server-initiated keepalive ping, routed through the SAME non-blocking send
/// path so a ping can never head-of-line-block the loop. (Replaces the old inert
/// sendPing, which had zero callers.)
pub fn ws_send_ping(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    var data_c: [*c]const u8 = null;
    var data_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Ky#", &conn_id_val, &data_c, &data_len) == 0) return null;
    return wsTrySendImpl(conn_id_val, .ping, data_c[0..@intCast(data_len)]);
}

/// _ws_pong_age(conn_id) -> float | None
/// Seconds since the last inbound frame of ANY kind (data/ping/pong/close) was
/// received — i.e. how long since we last heard the peer is alive. The keepalive
/// layer compares this against pong_timeout. None if the connection is unknown.
pub fn ws_pong_age(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    if (c.PyArg_ParseTuple(args, "K", &conn_id_val) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse return py.pyNone();
    defer unref(conn);
    const last = conn.last_recv_ns.load(.acquire);
    const age_ns = py.nanoTimestamp() - @as(i128, last);
    const age_s: f64 = @as(f64, @floatFromInt(age_ns)) / @as(f64, std.time.ns_per_s);
    return c.PyFloat_FromDouble(if (age_s < 0) 0 else age_s);
}

// ── WebSocket Connection Loop ────────────────────────────────────────────────

/// Handle a WebSocket connection after the handshake is complete.
/// Handle a WebSocket connection after handshake.
///
/// Calls the Python handler ONCE with (conn_id, headers_dict, path, query_string).
/// The handler drives I/O via the non-blocking _ws_try_send / _ws_try_recv
/// primitives (and their blocking _ws_send / _ws_recv fallbacks). Keepalive
/// pings are driven from the asyncio layer (ZigWebSocket) on top of the
/// non-blocking send path — there is NO background keepalive thread.
/// Returns true if connection ownership was transferred to the registry
/// (i.e. the fd/WsConn will be closed+freed by _ws_release, and server.zig
/// must NOT close the socket). Returns false on an early error before
/// handoff, in which case the caller closes the socket normally.
pub fn handleWebSocket(stream: py.NetStream, path: []const u8, query_string: []const u8, headers_section: []const u8, tstate: ?*anyopaque) bool {
    initWsMetrics(); // idempotent (CAS-guarded); registers the rejected-frames counter once

    // Look up the Python WebSocket handler for this path
    const handler = getWsHandler(path) orelse {
        sendClose(stream, 1008, "No handler") catch {};
        return false;
    };

    // Register this connection so Python can drive I/O
    const reg = registerConnection(stream);
    if (reg.id == 0) {
        sendClose(stream, 1011, "connection registry full") catch {};
        return false;
    }
    const conn_id = reg.id;

    // Disable Nagle's algorithm — without this, small frames (a 2-10 byte
    // header, or a short control/data frame) can sit buffered waiting for
    // more data or an ACK before the kernel sends them, adding latency to
    // every message. The reference `websockets`/asyncio stack sets this by
    // default; the native server didn't, which was a real (if usually
    // small, on loopback) disadvantage on every send.
    setTcpNoDelay(stream);

    // Acquire Python GIL to build args and invoke the dispatch callback.
    py.PyEval_RestoreThread(tstate);

    var handoff_ok = false;
    build: {
        const py_conn_id = c.PyLong_FromUnsignedLongLong(conn_id) orelse break :build;
        defer c.Py_DecRef(py_conn_id);
        const py_headers = buildHeadersDict(headers_section) orelse break :build;
        defer c.Py_DecRef(py_headers);
        const py_path = py.newString(path) orelse break :build;
        defer c.Py_DecRef(py_path);
        const py_qs = py.newString(query_string) orelse break :build;
        defer c.Py_DecRef(py_qs);
        const py_args = c.PyTuple_Pack(4, py_conn_id, py_headers, py_path, py_qs) orelse break :build;
        defer c.Py_DecRef(py_args);

        // Call the Python dispatch callback. In the default (synchronous)
        // mode it runs the handler to completion and calls _ws_release
        // before returning; in shared-loop mode it schedules the handler on
        // an event-loop pool and returns immediately, with _ws_release
        // invoked later when the coroutine finishes. Either way, ownership
        // of the connection now belongs to the registry, not this thread.
        const py_result = c.PyObject_Call(handler, py_args, null);
        if (py_result) |result| {
            c.Py_DecRef(result);
            handoff_ok = true;
        } else {
            c.PyErr_Print();
        }
    }

    // Release the GIL before any blocking socket I/O below.
    _ = py.PyEval_SaveThread();

    if (!handoff_ok) {
        // The Python dispatch never took ownership (couldn't build args or
        // schedule the handler). Send a server-error close frame, then release
        // synchronously so we don't leak the WsConn. This is the only path
        // where the native side sends a close frame — on every normal path the
        // Python ZigWebSocket owns the protocol close (see finalize()).
        sendClose(stream, 1011, "") catch {};
        releaseConnection(conn_id);
    }
    return true;
}

/// Release a connection's transport resources exactly once: close the fd and
/// free the WsConn + its buffers. Idempotent via the `released` atomic. Safe
/// to call from any thread (the accepting worker in thread mode, or an
/// event-loop thread in shared mode). Pure resource release — the protocol
/// close frame is the Python layer's concern (ZigWebSocket.finalize), so this
/// stays a clean single-responsibility teardown. Assumes the GIL is not held
/// (or has been released by the caller — see ws_release).
fn releaseConnection(conn_id: u64) void {
    // Atomically remove-and-take so exactly one caller owns the map's reference,
    // even if _ws_release races with a handoff-failure release.
    conn_mutex.lock();
    const removed = ws_connections.fetchRemove(conn_id);
    conn_mutex.unlock();

    const c_ptr = (removed orelse return).value;
    // Claim the one-time transport close (idempotent belt-and-suspenders).
    if (!c_ptr.released.swap(true, .acq_rel)) {
        c_ptr.alive.store(false, .release);
        // Serialize with any in-flight write, then close the fd.
        c_ptr.write_mutex.lock();
        c_ptr.stream.close();
        c_ptr.write_mutex.unlock();
    }
    // Drop the map's reference. The WsConn is freed here only if no I/O thread
    // still holds a reference (see unref) — otherwise the last such thread frees.
    unref(c_ptr);
}

/// Drop a reference to a WsConn (from lookupConnection, or the map's own ref in
/// releaseConnection). Frees the connection + its buffers when the final
/// reference is released, so an in-flight I/O call can never dereference freed
/// memory (F7).
fn unref(conn: *WsConn) void {
    if (conn.refcount.fetchSub(1, .acq_rel) != 1) return;
    conn.recv_buf.deinit(allocator);
    conn.out_buf.deinit(allocator);
    conn.frag_buf.deinit(allocator);
    allocator.destroy(conn);
}

/// Build a Python dict from raw HTTP headers.
fn buildHeadersDict(headers_section: []const u8) ?*c.PyObject {
    const dict = c.PyDict_New() orelse return null;
    var line_it = std.mem.splitSequence(u8, headers_section, "\r\n");
    while (line_it.next()) |line| {
        if (line.len == 0) continue;
        if (std.mem.indexOf(u8, line, ": ")) |colon_idx| {
            const name = line[0..colon_idx];
            const value = line[colon_idx + 2 ..];
            // Lowercase header name for consistency
            var lower_buf: [256]u8 = undefined;
            const lower_len = @min(name.len, lower_buf.len);
            for (0..lower_len) |i| {
                lower_buf[i] = std.ascii.toLower(name[i]);
            }
            // Clear the pending exception on EVERY failure branch so no
            // exception (e.g. a py.newString / PyDict_SetItem OOM) rides into the
            // handler's PyObject_Call in handleWebSocket. The dict may end up
            // partial, but it is always exception-clean (R13 F1 discipline).
            const py_key = py.newString(lower_buf[0..lower_len]) orelse {
                c.PyErr_Clear();
                continue;
            };
            const py_val = py.newString(value) orelse {
                c.PyErr_Clear();
                c.Py_DecRef(py_key);
                continue;
            };
            if (c.PyDict_SetItem(dict, py_key, py_val) != 0) c.PyErr_Clear();
            c.Py_DecRef(py_key);
            c.Py_DecRef(py_val);
        }
    }
    return dict;
}

// ── Python C API: WebSocket I/O ─────────────────────────────────────────────

/// _ws_send(conn_id, text) — send a text frame to the client.
/// Thread-safe: per-connection write mutex prevents frame interleaving.
pub fn ws_send(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    var text_c: [*c]const u8 = null;
    var text_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Ks#", &conn_id_val, &text_c, &text_len) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse {
        py.setError("ws_send: invalid connection id", .{});
        return null;
    };
    defer unref(conn);

    const text = text_c[0..@intCast(text_len)];

    // Serialize writes with per-connection mutex, release GIL for I/O
    conn.write_mutex.lock();
    // Re-check liveness under the write mutex. releaseConnection sets alive=false
    // BEFORE it acquires this same mutex to close the fd, so once we hold the lock
    // an alive load fully decides the race: if false, the fd is closed/reused and
    // we must not write to it (fd-reuse cross-talk); if true, the fd stays open
    // until we release the lock (releaseConnection is blocked waiting on it).
    if (!conn.alive.load(.acquire)) {
        conn.write_mutex.unlock();
        return py.pyNone();
    }
    const saved = py.PyEval_SaveThread();
    sendText(conn.stream, text) catch {
        py.PyEval_RestoreThread(saved);
        conn.write_mutex.unlock();
        conn.alive.store(false, .release);
        py.setError("ws_send: write failed", .{});
        return null;
    };
    py.PyEval_RestoreThread(saved);
    conn.write_mutex.unlock();

    return py.pyNone();
}

/// _ws_send_bytes(conn_id, data) — send a binary frame to the client.
pub fn ws_send_bytes(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    var data_c: [*c]const u8 = null;
    var data_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Ky#", &conn_id_val, &data_c, &data_len) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse {
        py.setError("ws_send_bytes: invalid connection id", .{});
        return null;
    };
    defer unref(conn);

    const data = data_c[0..@intCast(data_len)];

    conn.write_mutex.lock();
    // Re-check liveness under the write mutex — closes the fd-reuse window (see
    // ws_send). releaseConnection clears alive before taking this lock to close.
    if (!conn.alive.load(.acquire)) {
        conn.write_mutex.unlock();
        return py.pyNone();
    }
    const saved = py.PyEval_SaveThread();
    sendBinary(conn.stream, data) catch {
        py.PyEval_RestoreThread(saved);
        conn.write_mutex.unlock();
        conn.alive.store(false, .release);
        py.setError("ws_send_bytes: write failed", .{});
        return null;
    };
    py.PyEval_RestoreThread(saved);
    conn.write_mutex.unlock();

    return py.pyNone();
}

/// _ws_send_text_bytes(conn_id, data) — send a TEXT frame from already-UTF-8 bytes.
/// Fast path for send_json: the payload comes from our own JSON encoder, so it is
/// guaranteed valid UTF-8 — we write a text-opcode frame directly, skipping the
/// decode-to-str / re-encode round-trip. Mirrors ws_send_bytes but uses .text.
pub fn ws_send_text_bytes(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    var data_c: [*c]const u8 = null;
    var data_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Ky#", &conn_id_val, &data_c, &data_len) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse {
        py.setError("ws_send_text_bytes: invalid connection id", .{});
        return null;
    };
    defer unref(conn);

    const data = data_c[0..@intCast(data_len)];

    conn.write_mutex.lock();
    // Re-check liveness under the write mutex — closes the fd-reuse window (see
    // ws_send). releaseConnection clears alive before taking this lock to close.
    if (!conn.alive.load(.acquire)) {
        conn.write_mutex.unlock();
        return py.pyNone();
    }
    const saved = py.PyEval_SaveThread();
    sendText(conn.stream, data) catch {
        py.PyEval_RestoreThread(saved);
        conn.write_mutex.unlock();
        conn.alive.store(false, .release);
        py.setError("ws_send_text_bytes: write failed", .{});
        return null;
    };
    py.PyEval_RestoreThread(saved);
    conn.write_mutex.unlock();

    return py.pyNone();
}

// ── RFC 6455 §5.4 fragmentation + §8.1 validation ───────────────────────────

/// Outcome of feeding one data/continuation frame through the reassembly state
/// machine. Control frames (ping/pong/close) are handled by the caller and must
/// NOT be passed to handleDataFrame.
const DataFrameResult = union(enum) {
    /// A complete message is ready. `data` is valid until conn.frag_buf is next
    /// mutated (the caller converts it to a Python object immediately).
    message: struct { is_text: bool, data: []const u8 },
    buffered, // fragment stored; keep reading for the rest of the message
    protocol_error, // §5.4 / §5.2 violation → close 1002
    invalid_utf8, // §8.1: text payload is not valid UTF-8 → close 1007
    oom, // reassembly allocation failed → close 1011
};

/// Apply fragmentation reassembly (§5.4) and text UTF-8 validation (§8.1) to a
/// text/binary/continuation frame. Enforces: a data frame is illegal while a
/// message is in progress; a continuation with no start is illegal; the running
/// reassembled size must stay within max_message_size; unknown opcodes fail.
fn handleDataFrame(conn: *WsConn, frame: *const Frame) DataFrameResult {
    switch (frame.opcode) {
        .text, .binary => {
            // §5.4: only continuation frames may follow a non-FIN data frame.
            if (conn.frag_active) return .protocol_error;
            if (frame.fin) {
                if (frame.opcode == .text and !std.unicode.utf8ValidateSlice(frame.payload))
                    return .invalid_utf8;
                return .{ .message = .{ .is_text = frame.opcode == .text, .data = frame.payload } };
            }
            // Start a fragmented message.
            conn.frag_buf.clearRetainingCapacity();
            conn.frag_buf.appendSlice(allocator, frame.payload) catch return .oom;
            conn.frag_active = true;
            conn.frag_is_text = frame.opcode == .text;
            return .buffered;
        },
        .continuation => {
            if (!conn.frag_active) return .protocol_error; // continuation with no start
            if (conn.frag_buf.items.len + frame.payload.len > max_message_size) return .protocol_error;
            conn.frag_buf.appendSlice(allocator, frame.payload) catch return .oom;
            if (!frame.fin) return .buffered;
            // FIN — message complete.
            conn.frag_active = false;
            const is_text = conn.frag_is_text;
            const data = conn.frag_buf.items;
            if (is_text and !std.unicode.utf8ValidateSlice(data)) return .invalid_utf8;
            return .{ .message = .{ .is_text = is_text, .data = data } };
        },
        // Reserved data opcodes (0x3-0x7) and reserved control opcodes reaching
        // here (0xB-0xF) are protocol violations (§5.2). The old `else => {}`
        // silently dropped them.
        else => return .protocol_error,
    }
}

/// Choose the close code to echo when the peer sends a close frame (§5.5.1 /
/// §7.4): a body must be either empty or a valid 2-byte status code followed by
/// a UTF-8 reason. 1-byte body or invalid code → 1002; non-UTF-8 reason → 1007.
fn closeResponseCode(payload: []const u8) u16 {
    if (payload.len == 0) return 1000;
    if (payload.len == 1) return 1002; // truncated status code
    const code = std.mem.readInt(u16, payload[0..2], .big);
    if (!validCloseCode(code)) return 1002;
    if (!std.unicode.utf8ValidateSlice(payload[2..])) return 1007;
    return 1000;
}

/// RFC 6455 §7.4.1 valid close codes a client may send.
fn validCloseCode(code: u16) bool {
    return switch (code) {
        1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011 => true,
        else => code >= 3000 and code <= 4999,
    };
}

/// Fail the connection: send `code` in a close frame, mark it dead, count a
/// rejected frame. Assumes the GIL is held (releases it around the send).
fn failWs(conn: *WsConn, code: u16) void {
    conn.write_mutex.lock();
    const saved = py.PyEval_SaveThread();
    sendClose(conn.stream, code, "") catch {};
    py.PyEval_RestoreThread(saved);
    conn.write_mutex.unlock();
    conn.alive.store(false, .release);
    if (_ws_rejected_counter) |cnt| cnt.inc(1);
}

/// _ws_recv(conn_id) -> str | bytes | None — read the next frame (blocking).
/// Returns None on close/disconnect. Handles ping/pong transparently.
/// Text frames return str, binary frames return bytes.
pub fn ws_recv(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    if (c.PyArg_ParseTuple(args, "K", &conn_id_val) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse {
        return py.pyNone();
    };
    defer unref(conn);

    while (true) {
        if (!conn.alive.load(.acquire)) return py.pyNone();

        // Release GIL while waiting for I/O
        const saved = py.PyEval_SaveThread();
        var frame = readFrame(conn.stream) catch |err| {
            py.PyEval_RestoreThread(saved);
            // Count malformed/non-conformant frames (vs a clean disconnect) and,
            // for protocol violations, send the matching close code (§5.x).
            switch (err) {
                error.UnmaskedClientFrame, error.InvalidControlFrame, error.ProtocolError => failWs(conn, 1002),
                error.FrameTooLarge => failWs(conn, 1009),
                else => conn.alive.store(false, .release),
            }
            return py.pyNone();
        };
        py.PyEval_RestoreThread(saved);
        defer frame.deinit();
        // Any inbound frame proves the peer is alive — refresh the keepalive clock.
        conn.last_recv_ns.store(@intCast(py.nanoTimestamp()), .release);

        switch (frame.opcode) {
            .ping => {
                // Auto-pong through the non-blocking outbound path — a
                // ping-flooding peer with a full receive window can no longer
                // block this thread for up to SO_SNDTIMEO. On shed/transport
                // death the connection is marked dead.
                if (!enqueueReplyNonblocking(conn, .pong, frame.payload)) return py.pyNone();
            },
            .pong => {},
            .close => {
                // Echo the close through the non-blocking path (best-effort per
                // §5.5.1), then tear down. The fd close in release delivers the
                // FIN/RST even if the echo could only buffer or shed.
                var code_buf: [2]u8 = undefined;
                std.mem.writeInt(u16, &code_buf, closeResponseCode(frame.payload), .big);
                _ = enqueueReplyNonblocking(conn, .close, &code_buf);
                conn.alive.store(false, .release);
                return py.pyNone();
            },
            // text / binary / continuation / reserved → fragmentation state machine.
            else => switch (handleDataFrame(conn, &frame)) {
                // On a valid message, newString/newBytes only fail on OOM (text
                // is already UTF-8-validated). Propagate that (return null) rather
                // than masking it as None with a dangling exception set.
                .message => |m| return if (m.is_text)
                    (py.newString(m.data) orelse return null)
                else
                    (py.newBytes(m.data) orelse return null),
                .buffered => {}, // keep reading for the rest of the fragmented message
                .protocol_error => {
                    failWs(conn, 1002);
                    return py.pyNone();
                },
                .invalid_utf8 => {
                    failWs(conn, 1007);
                    return py.pyNone();
                },
                .oom => {
                    failWs(conn, 1011);
                    return py.pyNone();
                },
            },
        }
    }
}

/// _ws_try_recv(conn_id) -> str | bytes | False | None
///
/// Non-blocking single attempt: returns the next frame if one is already
/// available (buffered or via a MSG_DONTWAIT read that doesn't block),
/// `False` if none is available yet (caller should wait for readability —
/// e.g. via asyncio's loop.add_reader() on the fd from _ws_get_fd — and
/// call again), or `None` on disconnect. Never blocks, so it's safe to call
/// directly from a coroutine without an executor. Handles ping/pong
/// transparently, same as the blocking _ws_recv.
pub fn ws_try_recv(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    if (c.PyArg_ParseTuple(args, "K", &conn_id_val) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse return py.pyNone();
    defer unref(conn);

    while (true) {
        if (!conn.alive.load(.acquire)) return py.pyNone();

        conn.recv_mutex.lock();
        const result = tryRecvFrame(conn);
        conn.recv_mutex.unlock();

        switch (result) {
            .would_block => return py.pyFalse(),
            .closed => {
                conn.alive.store(false, .release);
                return py.pyNone();
            },
            .rejected => {
                if (_ws_rejected_counter) |cnt| cnt.inc(1);
                conn.alive.store(false, .release);
                return py.pyNone();
            },
            .frame => |f| {
                var frame = f;
                defer frame.deinit();
                // Inbound frame → peer is alive; refresh the keepalive clock.
                conn.last_recv_ns.store(@intCast(py.nanoTimestamp()), .release);
                switch (frame.opcode) {
                    .ping => {
                        // Auto-pong through the non-blocking outbound path. This
                        // runs ON the shared event-loop thread, so a blocking
                        // writev here (the old behavior) would stall EVERY
                        // connection multiplexed on the loop for up to
                        // SO_SNDTIMEO when a ping-flooding peer holds its receive
                        // window full — the HOL DoS. Buffer/shed instead of block.
                        if (!enqueueReplyNonblocking(conn, .pong, frame.payload)) return py.pyNone();
                        // Not a data frame — keep looking for one (or would_block/closed).
                    },
                    .pong => {},
                    .close => {
                        // Echo the close non-blocking (best-effort per §5.5.1),
                        // then tear down; the fd close in release delivers the
                        // FIN/RST even if the echo could only buffer or shed.
                        var code_buf: [2]u8 = undefined;
                        std.mem.writeInt(u16, &code_buf, closeResponseCode(frame.payload), .big);
                        _ = enqueueReplyNonblocking(conn, .close, &code_buf);
                        conn.alive.store(false, .release);
                        return py.pyNone();
                    },
                    // text / binary / continuation / reserved → §5.4 reassembly.
                    else => switch (handleDataFrame(conn, &frame)) {
                        .message => |m| return if (m.is_text)
                            (py.newString(m.data) orelse return null)
                        else
                            (py.newBytes(m.data) orelse return null),
                        .buffered => {}, // keep reading for the rest of the message
                        .protocol_error => {
                            failWs(conn, 1002);
                            return py.pyNone();
                        },
                        .invalid_utf8 => {
                            failWs(conn, 1007);
                            return py.pyNone();
                        },
                        .oom => {
                            failWs(conn, 1011);
                            return py.pyNone();
                        },
                    },
                }
            },
        }
    }
}

/// _ws_get_fd(conn_id) -> int | None — raw socket fd, for asyncio's
/// loop.add_reader(). None if the connection is unknown/already closed.
pub fn ws_get_fd(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    if (c.PyArg_ParseTuple(args, "K", &conn_id_val) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse return py.pyNone();
    defer unref(conn);
    return c.PyLong_FromLong(@intCast(conn.stream.handle));
}

/// _ws_close(conn_id, code, reason) — send close frame.
pub fn ws_close(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    var code_val: c_int = 1000;
    var reason_c: [*c]const u8 = null;
    var reason_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Kis#", &conn_id_val, &code_val, &reason_c, &reason_len) == 0) return null;

    const conn = lookupConnection(conn_id_val) orelse {
        return py.pyNone();
    };
    defer unref(conn);

    const reason = reason_c[0..@intCast(reason_len)];
    conn.write_mutex.lock();
    const saved = py.PyEval_SaveThread();
    sendClose(conn.stream, @intCast(code_val), reason) catch {};
    py.PyEval_RestoreThread(saved);
    conn.write_mutex.unlock();
    conn.alive.store(false, .release);

    return py.pyNone();
}

/// _ws_release(conn_id) — close the socket and free the connection, exactly
/// once. Called by Python when a handler finishes (synchronously in the
/// default model, or from an event-loop thread in shared-loop mode). This is
/// the sole owner of the fd close: server.zig never closes a WS socket.
pub fn ws_release(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_id_val: c_ulonglong = 0;
    if (c.PyArg_ParseTuple(args, "K", &conn_id_val) == 0) return null;

    // Release the GIL around the blocking close-frame write + fd close.
    const saved = py.PyEval_SaveThread();
    releaseConnection(@intCast(conn_id_val));
    py.PyEval_RestoreThread(saved);

    return py.pyNone();
}

// ── Active Connection Registry ──────────────────────────────────────────────
//
// Maps connection IDs to active WebSocket streams so Python can drive I/O
// via _ws_send / _ws_recv / _ws_close.

/// WebSocket connection state.
///
/// Lifetime (explicit-release model):
///
///   1. handleWebSocket registers the conn, hands (conn_id, ...) to the Python
///      dispatch callback, and RETURNS without closing the socket or freeing
///      the WsConn — ownership has passed to the connection registry.
///   2. The Python handler drives I/O via _ws_send / _ws_try_recv / _ws_recv,
///      referencing the conn by id (all guarded by the registry mutex + the
///      `alive` / `released` atomics).
///   3. When the handler finishes (client disconnect, error, or graceful
///      close), Python calls _ws_release(conn_id) EXACTLY once.
///   4. ws_release atomically claims the connection (via `released`), sends a
///      best-effort close frame, closes the fd, unregisters, and frees.
///
/// This decouples a connection's lifetime from any single OS thread, which is
/// what lets the handler run either synchronously on the accepting worker
/// thread (default) OR multiplexed on a shared event-loop pool (opt-in
/// HYPER_WS_SHARED_LOOPS) without changing any of the Zig I/O primitives.
///
/// `released` makes _ws_release idempotent and is the single authority for
/// who closes the fd — server.zig never closes a WebSocket socket, so there is
/// exactly one close per connection regardless of which thread triggers it.
const WsConn = struct {
    stream: py.NetStream,
    alive: std.atomic.Value(bool), // false = no more I/O should be attempted
    released: std.atomic.Value(bool) = std.atomic.Value(bool).init(false), // true once close+free has been claimed
    // Reference count keeps the allocation alive while a thread is mid-I/O on it
    // (F7). The registry map holds the initial ref; lookupConnection retains one
    // for the caller; unref frees when the last ref drops. Without this, one
    // thread's releaseConnection could destroy the WsConn while another derefs
    // conn.stream / write_mutex (reachable in shared-loop mode).
    refcount: std.atomic.Value(u32) = std.atomic.Value(u32).init(1),
    write_mutex: py.Mutex, // Serializes frame writes AND guards out_buf/out_pos
    recv_buf: std.ArrayListUnmanaged(u8) = .empty, // Non-blocking recv reassembly (see tryRecvFrame)
    recv_pos: usize = 0, // Read cursor into recv_buf — avoids memmove per frame extracted
    recv_mutex: py.Mutex = .{}, // Defensive: protects recv_buf if misused concurrently
    // Non-blocking send backlog (symmetric to recv_buf/recv_pos): framed bytes
    // not yet accepted by the kernel for a slow consumer. out_pos marks the
    // already-sent prefix. Guarded by write_mutex; drained via add_writer +
    // _ws_flush_send. See sendFramedLocked / flushOutboundLocked.
    out_buf: std.ArrayListUnmanaged(u8) = .empty,
    out_pos: usize = 0,
    // Wall-clock (ns) of the last inbound frame of any kind — the keepalive
    // layer reads it via _ws_pong_age to reap peers that have gone silent.
    // Initialized to registration time.
    last_recv_ns: std.atomic.Value(i64) = std.atomic.Value(i64).init(0),
    // RFC 6455 §5.4 fragmentation reassembly. A data frame with FIN=0 starts a
    // message; continuation frames (opcode 0) append until FIN=1. frag_active
    // marks an in-progress message; frag_is_text records whether to UTF-8-validate
    // the reassembled result.
    frag_active: bool = false,
    frag_is_text: bool = false,
    frag_buf: std.ArrayListUnmanaged(u8) = .empty,
};

var next_conn_id: u64 = 1;
var ws_connections: std.AutoHashMapUnmanaged(u64, *WsConn) = .empty;
var conn_mutex: py.Mutex = .{};

fn registerConnection(stream: py.NetStream) struct { id: u64, conn: *WsConn } {
    conn_mutex.lock();
    defer conn_mutex.unlock();
    const id = next_conn_id;
    next_conn_id +%= 1;
    if (next_conn_id == 0) next_conn_id = 1;
    const conn = allocator.create(WsConn) catch return .{ .id = 0, .conn = undefined };
    conn.* = .{
        .stream = stream,
        .alive = std.atomic.Value(bool).init(true),
        .write_mutex = .{},
        .last_recv_ns = std.atomic.Value(i64).init(@intCast(py.nanoTimestamp())),
    };
    ws_connections.put(allocator, id, conn) catch {
        allocator.destroy(conn);
        return .{ .id = 0, .conn = undefined };
    };
    return .{ .id = id, .conn = conn };
}

/// Look up a live connection. Returns null if not found or dead. The
/// returned pointer stays valid because releaseConnection removes the entry
/// from the map (under conn_mutex) before freeing, and every I/O primitive
/// re-looks-up by id under the same mutex rather than caching the pointer.
fn lookupConnection(id: u64) ?*WsConn {
    conn_mutex.lock();
    defer conn_mutex.unlock();
    const conn = ws_connections.get(id) orelse return null;
    if (!conn.alive.load(.acquire)) return null;
    // Retain a reference for the caller while still holding conn_mutex, so a
    // concurrent releaseConnection can't remove+free between the get and the
    // caller's use. The caller MUST pair this with unref(conn) (F7).
    _ = conn.refcount.fetchAdd(1, .acq_rel);
    return conn;
}

// ── WebSocket Handler Registry ───────────────────────────────────────────────

var ws_handlers: std.StringHashMapUnmanaged(?*c.PyObject) = .{};
var ws_mutex: py.Mutex = .{};

pub fn registerWsHandler(path: []const u8, handler: *c.PyObject) !void {
    ws_mutex.lock();
    defer ws_mutex.unlock();

    const owned_path = try allocator.dupe(u8, path);
    c.Py_IncRef(handler);

    if (ws_handlers.fetchPut(allocator, owned_path, handler) catch null) |old| {
        if (old.value) |v| c.Py_DecRef(v);
        allocator.free(old.key);
    }
}

fn getWsHandler(path: []const u8) ?*c.PyObject {
    ws_mutex.lock();
    defer ws_mutex.unlock();
    return ws_handlers.get(path) orelse null;
}

// ── Python API ───────────────────────────────────────────────────────────────

/// _server_add_ws_route(path, handler) — register a WebSocket handler
pub fn server_add_ws_route(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var path_c: [*c]const u8 = null;
    var handler: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "sO", &path_c, &handler) == 0) return null;

    registerWsHandler(std.mem.span(path_c), handler.?) catch {
        py.setError("websocket: failed to register handler", .{});
        return null;
    };

    return py.pyNone();
}

// ── Python C API: WebSocket Configuration ────────────────────────────────────

/// _server_set_ws_config(max_msg_size, ping_interval_s, pong_timeout_s)
/// Configure WebSocket protocol parameters.
pub fn server_set_ws_config(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var msg_size: c_long = 0;
    var ping_s: c_long = 0;
    var pong_s: c_long = 0;
    if (c.PyArg_ParseTuple(args, "lll", &msg_size, &ping_s, &pong_s) == 0) return null;

    if (msg_size > 0) max_message_size = @intCast(msg_size);
    ping_interval_ns = @as(i128, ping_s) * std.time.ns_per_s;
    pong_timeout_ns = @as(i128, pong_s) * std.time.ns_per_s;

    return py.pyNone();
}

/// _server_get_ws_config() -> (max_msg_size, ping_interval_s, pong_timeout_s)
pub fn server_get_ws_config(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const msg = c.PyLong_FromUnsignedLongLong(max_message_size) orelse return null;
    defer c.Py_DecRef(msg);
    const ping_s: i64 = @intCast(@divTrunc(ping_interval_ns, std.time.ns_per_s));
    const pong_s: i64 = @intCast(@divTrunc(pong_timeout_ns, std.time.ns_per_s));
    const ping = c.PyLong_FromLongLong(ping_s) orelse return null;
    defer c.Py_DecRef(ping);
    const pong = c.PyLong_FromLongLong(pong_s) orelse return null;
    defer c.Py_DecRef(pong);
    // PyTuple_Pack INCREFs each item; without these defers the three fresh
    // PyLongs would leak on every _server_get_ws_config call.
    return c.PyTuple_Pack(3, msg, ping, pong);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn readExact(stream: py.NetStream, buf: []u8) !void {
    var total: usize = 0;
    while (total < buf.len) {
        const n = try stream.read(buf[total..]);
        if (n == 0) return error.ConnectionClosed;
        total += n;
    }
}

// ── Unit tests (fixtures) ───────────────────────────────────────────────────

/// Build a minimal WsConn for exercising the pure fragmentation state machine.
/// handleDataFrame only touches the frag_* fields, so stream can be undefined.
fn testConn() WsConn {
    return .{
        .stream = undefined,
        .alive = std.atomic.Value(bool).init(true),
        .write_mutex = .{},
    };
}

test "handleDataFrame: unfragmented text and binary" {
    var conn = testConn();
    defer conn.frag_buf.deinit(allocator);

    const t = Frame{ .fin = true, .opcode = .text, .payload = "hi" };
    switch (handleDataFrame(&conn, &t)) {
        .message => |m| {
            try std.testing.expect(m.is_text);
            try std.testing.expectEqualStrings("hi", m.data);
        },
        else => return error.TestUnexpectedResult,
    }
    const b = Frame{ .fin = true, .opcode = .binary, .payload = &[_]u8{ 0xFF, 0x00 } };
    switch (handleDataFrame(&conn, &b)) {
        .message => |m| try std.testing.expect(!m.is_text),
        else => return error.TestUnexpectedResult,
    }
}

test "handleDataFrame: RFC 6455 §5.4 fragmentation reassembly" {
    var conn = testConn();
    defer conn.frag_buf.deinit(allocator);

    // "He" (start, FIN=0) + "ll" (continuation) + "o" (continuation, FIN=1).
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = false, .opcode = .text, .payload = "He" }) == .buffered);
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = false, .opcode = .continuation, .payload = "ll" }) == .buffered);
    switch (handleDataFrame(&conn, &Frame{ .fin = true, .opcode = .continuation, .payload = "o" })) {
        .message => |m| {
            try std.testing.expect(m.is_text);
            try std.testing.expectEqualStrings("Hello", m.data);
        },
        else => return error.TestUnexpectedResult,
    }
}

test "handleDataFrame: §5.4 violations fail the connection" {
    var conn = testConn();
    defer conn.frag_buf.deinit(allocator);

    // Continuation with no in-progress message.
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = true, .opcode = .continuation, .payload = "x" }) == .protocol_error);

    // A new data frame while a fragmented message is in progress.
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = false, .opcode = .text, .payload = "a" }) == .buffered);
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = true, .opcode = .text, .payload = "b" }) == .protocol_error);

    // Reserved (unknown) opcode.
    var c2 = testConn();
    defer c2.frag_buf.deinit(allocator);
    try std.testing.expect(handleDataFrame(&c2, &Frame{ .fin = true, .opcode = @enumFromInt(0x3), .payload = "" }) == .protocol_error);
}

test "handleDataFrame: §8.1 text UTF-8 validation, binary exempt" {
    var conn = testConn();
    defer conn.frag_buf.deinit(allocator);

    // Lone 0xFF is invalid UTF-8 → 1007 for text.
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = true, .opcode = .text, .payload = &[_]u8{0xFF} }) == .invalid_utf8);
    // Same bytes in a binary frame are fine.
    switch (handleDataFrame(&conn, &Frame{ .fin = true, .opcode = .binary, .payload = &[_]u8{0xFF} })) {
        .message => |m| try std.testing.expect(!m.is_text),
        else => return error.TestUnexpectedResult,
    }
    // Invalid UTF-8 split across fragments is caught on reassembly.
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = false, .opcode = .text, .payload = &[_]u8{0xE2} }) == .buffered);
    try std.testing.expect(handleDataFrame(&conn, &Frame{ .fin = true, .opcode = .continuation, .payload = &[_]u8{0x28} }) == .invalid_utf8);
}

test "parseFrameFromBuffer: reserved bits are a protocol violation (§5.2)" {
    // FIN + <reserved> + text opcode, masked, len 0. Reserved bit must be fatal.
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0xC1, 0x80 }) == .fatal); // RSV1
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0xA1, 0x80 }) == .fatal); // RSV2
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0x91, 0x80 }) == .fatal); // RSV3
    // No reserved bits, unmasked → still fatal (must be masked), but not for RSV.
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0x81, 0x00 }) == .fatal); // unmasked
}

test "parseFrameFromBuffer: non-minimal length encoding rejected (§5.2)" {
    // len 5 (fits the 7-bit form) sent in the 16-bit form → fatal.
    // 0x81 = FIN+text, 0xFE = masked + 126 marker, then u16 length 5.
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0x81, 0xFE, 0x00, 0x05 }) == .fatal);
    // len 5 sent in the 64-bit form → fatal.
    // 0x81, 0xFF = masked + 127 marker, then u64 length 5.
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0x81, 0xFF, 0, 0, 0, 0, 0, 0, 0, 5 }) == .fatal);
    // Boundary: 126 IS the minimal use of the 16-bit form — not a length error
    // (incomplete here only because no mask key / payload follows).
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0x81, 0xFE, 0x00, 0x7E }) == .incomplete);
    // Boundary: 65536 IS the minimal use of the 64-bit form — not a length error.
    try std.testing.expect(parseFrameFromBuffer(&[_]u8{ 0x81, 0xFF, 0, 0, 0, 0, 0, 1, 0, 0 }) == .incomplete);
}

test "getWebSocketProtocol: selects exactly one subprotocol (§4.2.2)" {
    const h1 = "GET / HTTP/1.1\r\nSec-WebSocket-Protocol: a, b, c\r\n\r\n";
    try std.testing.expectEqualStrings("a", getWebSocketProtocol(h1).?);
    const h2 = "GET / HTTP/1.1\r\nSec-WebSocket-Protocol: chat\r\n\r\n";
    try std.testing.expectEqualStrings("chat", getWebSocketProtocol(h2).?);
    const h3 = "GET / HTTP/1.1\r\nSec-WebSocket-Protocol:   x  ,y\r\n\r\n";
    try std.testing.expectEqualStrings("x", getWebSocketProtocol(h3).?);
    // Absent header → null.
    try std.testing.expect(getWebSocketProtocol("GET / HTTP/1.1\r\nHost: x\r\n\r\n") == null);
}

test "closeResponseCode: §5.5.1/§7.4 validation" {
    try std.testing.expectEqual(@as(u16, 1000), closeResponseCode(""));
    try std.testing.expectEqual(@as(u16, 1002), closeResponseCode(&[_]u8{0x03})); // 1-byte
    try std.testing.expectEqual(@as(u16, 1000), closeResponseCode(&[_]u8{ 0x03, 0xE8 })); // 1000, empty reason
    try std.testing.expectEqual(@as(u16, 1000), closeResponseCode(&[_]u8{ 0x03, 0xE8, 'o', 'k' })); // 1000 + "ok"
    try std.testing.expectEqual(@as(u16, 1002), closeResponseCode(&[_]u8{ 0x03, 0xEC })); // 1004 (invalid)
    try std.testing.expectEqual(@as(u16, 1007), closeResponseCode(&[_]u8{ 0x03, 0xE8, 0xFF })); // bad UTF-8 reason
}
