const std = @import("std");
const lib = @import("lib.zig");
const Buffer = @import("buffer").Buffer;

const proto = lib.proto;
const Conn = lib.Conn;
const Reader = lib.Reader;
const NotificationResponse = lib.proto.NotificationResponse;

const Stream = lib.Stream;
const Allocator = std.mem.Allocator;

// errno access for distinguishing a read timeout from a dead connection in the
// multiplexed listener (see nextRaw). Bind the platform's real TLS errno
// function — std.c._errno proved unreliable under Zig 0.16 in this codebase.
extern fn __errno_location() *c_int; // Linux/glibc
extern fn __error() *c_int; // macOS/BSD
inline fn currentErrno() c_int {
    return switch (@import("builtin").os.tag) {
        .macos, .ios, .tvos, .watchos => __error().*,
        else => __errno_location().*,
    };
}
const is_darwin = switch (@import("builtin").os.tag) {
    .macos, .ios, .tvos, .watchos => true,
    else => false,
};
const EAGAIN: c_int = if (is_darwin) 35 else 11;
const EWOULDBLOCK: c_int = EAGAIN; // same value on Linux and macOS
const EINTR: c_int = 4;

const ListenError = union(enum) {
    err: anyerror,
    pg: lib.proto.Error,
};

pub const Listener = struct {
    err: ?ListenError = null,
    closed: bool = false,

    _stream: Stream,

    // A buffer used for writing to PG. This can grow dynamically as needed.
    _buf: Buffer,

    // Used to read data from PG. Has its own buffer which can grow dynamically
    _reader: Reader,

    // If we get a PG error, we'll return a LIstenError.pg, and we'll own its
    // memory.
    _err_data: ?[]const u8 = null,

    _allocator: Allocator,

    pub fn open(allocator: Allocator, opts: Conn.Opts) !Listener {
        var stream = try Stream.connect(allocator, opts, null);
        errdefer stream.close();

        const buf = try Buffer.init(allocator, opts.write_buffer orelse 2048);
        errdefer buf.deinit();

        const reader = try Reader.init(allocator, opts.read_buffer orelse 4096, stream);
        errdefer reader.deinit();

        return .{
            ._buf = buf,
            ._stream = stream,
            ._reader = reader,
            ._allocator = allocator,
        };
    }

    pub fn deinit(self: *Listener) void {
        if (self._err_data) |err_data| {
            self._allocator.free(err_data);
        }
        self._buf.deinit();
        self._reader.deinit();

        self.stop();
    }

    pub fn stop(self: *Listener) void {
        if (@atomicRmw(bool, &self.closed, .Xchg, true, .monotonic) == true) {
            return;
        }

        // try to send a Terminate to the DB
        self._stream.writeAll(&.{ 'X', 0, 0, 0, 4 }) catch {};
        self._stream.close();
    }

    pub fn auth(self: *Listener, opts: Conn.AuthOpts) !void {
        if (try lib.auth.auth(&self._stream, &self._buf, &self._reader, opts)) |raw_pg_err| {
            return self.setErr(raw_pg_err);
        }

        while (true) {
            const msg = try self.read();
            switch (msg.type) {
                'Z' => return,
                'K' => {}, // BackendKeyData: pid + secret key — informational for cancel support
                'S' => {}, // ParameterStatus: server config params — informational
                else => return error.UnexpectedDBMessage,
            }
        }
    }

    const ListenOpts = struct {
        timeout: u32 = 0,
    };
    // Build + write a `LISTEN "channel"` simple-query message. LISTEN doesn't
    // support parameterized queries, so we quote the identifier by hand. This
    // does NOT read the response — callers either read C/Z synchronously
    // (`listen`) or consume it generically from the read loop (`sendListen`,
    // used by the multiplexed listener).
    fn writeListen(self: *Listener, channel: []const u8) !void {
        const buf = &self._buf;
        buf.reset();

        // "LISTEN " = 7, identifier up to 63 (126 if every char is quote-doubled)
        // + 2 quotes + 1 null terminator; 136 is a safe upper bound.
        try buf.ensureTotalCapacity(136);
        buf.writeByteAssumeCapacity('Q');

        var len_view = try buf.skip(4);

        buf.writeAssumeCapacity("LISTEN \"");

        // + 4 for the length itself + 7 for LISTEN + 2 quotes + 1 null terminator
        var len = 11 + channel.len + 3;
        for (channel) |c| {
            if (c == '"') {
                len += 1;
                buf.writeAssumeCapacity("\"\"");
            } else {
                buf.writeByteAssumeCapacity(c);
            }
        }
        buf.writeByteAssumeCapacity('"');
        buf.writeByteAssumeCapacity(0);

        // fill in the length
        len_view.writeIntBig(u32, @intCast(len));

        try self._stream.writeAll(buf.string());
    }

    pub fn listen(self: *Listener, channel: []const u8, opts: ListenOpts) !void {
        try self.writeListen(channel);

        {
            // we expect a command complete ('C')
            const msg = try self.read();
            switch (msg.type) {
                'C' => {},
                else => return error.UnexpectedDBMessage,
            }
        }

        {
            // followed by a ReadyForQuery ('Z')
            const msg = try self.read();
            switch (msg.type) {
                'Z' => {},
                else => return error.UnexpectedDBMessage,
            }
        }

        try self._reader.startFlow(null, opts.timeout);
    }

    // ── Multiplexed-listener API ─────────────────────────────────────────
    //
    // A single Listener can LISTEN on many channels and demultiplex the
    // notifications by name (PostgreSQL tags every NotificationResponse with
    // its channel). db.zig uses this to run ONE connection+thread per database
    // instead of one per channel. The read loop consumes ALL message types, so
    // a LISTEN's C/Z acknowledgement is issued fire-and-forget and swallowed
    // generically — no synchronous request/response pairing to race against
    // asynchronous notifications.

    /// Arm a read timeout so the demux loop wakes periodically to add new
    /// channels and observe shutdown even when no notification arrives.
    pub fn setReadTimeout(self: *Listener, timeout_ms: u32) !void {
        try self._reader.startFlow(null, timeout_ms);
    }

    /// Issue `LISTEN "channel"` without reading its response (the demux loop
    /// consumes the C/Z generically). Safe to interleave with notifications.
    pub fn sendListen(self: *Listener, channel: []const u8) !void {
        try self.writeListen(channel);
    }

    pub const DemuxResult = union(enum) {
        notification: NotificationResponse, // 'A' — demux by .channel
        other, // C/Z/N/S/K — LISTEN acks / notices / status; ignore
        pg_error: []const u8, // 'E' — server error payload
        timed_out, // read timeout elapsed (SO_RCVTIMEO) — benign, retry
        closed, // connection dropped / fatal read error — caller reconnects
    };

    /// Read + classify the next protocol message for the multiplexed loop,
    /// distinguishing a benign read timeout from a dead connection. Buffered
    /// messages are returned before the socket is touched (so back-to-back
    /// notifications drain without blocking); a socket read then blocks up to
    /// the configured timeout.
    pub fn nextDemux(self: *Listener) DemuxResult {
        const msg = self._reader.next() catch {
            // readSocket collapses every read failure to error.ReadFailed, so
            // consult errno directly (still valid — no syscalls intervene) to
            // tell a SO_RCVTIMEO timeout (EAGAIN/EWOULDBLOCK/EINTR) apart from
            // a genuinely broken connection.
            const e = currentErrno();
            if (e == EAGAIN or e == EWOULDBLOCK or e == EINTR) return .timed_out;
            return .closed;
        };
        return switch (msg.type) {
            'A' => .{ .notification = NotificationResponse.parse(msg.data) catch return .other },
            'E' => .{ .pg_error = msg.data },
            else => .other,
        };
    }

    pub fn next(self: *Listener) ?NotificationResponse {
        const msg = self.read() catch |err| {
            self.err = .{ .err = err };
            return null;
        };

        switch (msg.type) {
            'A' => return NotificationResponse.parse(msg.data) catch |err| {
                self.err = .{ .err = err };
                return null;
            },
            else => {
                self.err = .{ .err = error.UnexpectedDBMessage };
                return null;
            },
        }
    }

    fn read(self: *Listener) !lib.Message {
        var reader = &self._reader;
        while (true) {
            const msg = try reader.next();
            switch (msg.type) {
                'N' => {}, // NoticeResponse: warnings/info — informational, no action needed
                'E' => return self.setErr(msg.data),
                else => return msg,
            }
        }
    }

    fn setErr(self: *Listener, data: []const u8) error{ PG, OutOfMemory } {
        const allocator = self._allocator;

        // The proto.Error that we're about to create is going to reference data.
        // But data is owned by our Reader and its lifetime doesn't necessarily match
        // what we want here. So we're going to dupe it and make the connection own
        // the data so it can tie its lifecycle to the error.

        // That means clearing out any previous duped error data we had
        if (self._err_data) |err_data| {
            allocator.free(err_data);
        }

        const owned = try allocator.dupe(u8, data);
        self._err_data = owned;
        self.err = .{ .pg = proto.Error.parse(owned) };
        return error.PG;
    }
};

const t = lib.testing;
test "Listener" {
    var l = try Listener.open(t.allocator, .{ .host = "localhost" });
    defer l.deinit();
    try l.auth(t.authOpts(.{}));
    try testListener(&l);
}

test "Listener: from Pool" {
    var pool = try lib.Pool.init(t.allocator, .{
        .size = 1,
        .auth = t.authOpts(.{}),
    });
    defer pool.deinit();

    var l = try pool.newListener();
    defer l.deinit();

    try testListener(&l);
}

fn testListener(l: *Listener) !void {
    // std.Thread.{ResetEvent,Semaphore} were removed in Zig 0.16; a shared
    // atomic bool the shutdown thread spins on gives the same "block until
    // signalled once" behavior for this test.
    var reset = std.atomic.Value(bool).init(false);
    var tt = try std.Thread.spawn(.{}, struct {
        fn shutdown(ll: *Listener, r: *std.atomic.Value(bool)) void {
            while (!r.load(.acquire)) std.atomic.spinLoopHint();
            ll.stop();
        }
    }.shutdown, .{ l, &reset });
    tt.detach();

    try l.listen("chan-1", .{});
    try l.listen("chan_2", .{});

    const thrd = try std.Thread.spawn(.{}, testNotifier, .{});
    {
        const notification = l.next().?;
        try t.expectString("chan-1", notification.channel);
        try t.expectString("pl-1", notification.payload);
    }

    {
        const notification = l.next().?;
        try t.expectString("chan_2", notification.channel);
        try t.expectString("pl-2", notification.payload);
    }

    {
        const notification = l.next().?;
        try t.expectString("chan-1", notification.channel);
        try t.expectString("", notification.payload);
    }

    reset.store(true, .release);
    try t.expectEqual(null, l.next());
    thrd.join();
}

fn testNotifier() void {
    var c = t.connect(.{});
    defer c.deinit();
    _ = c.exec("select pg_notify($1, $2)", .{ "chan_x", "pl-x" }) catch unreachable;
    _ = c.exec("select pg_notify($1, $2)", .{ "chan-1", "pl-1" }) catch unreachable;
    _ = c.exec("select pg_notify($1, $2)", .{ "chan_2", "pl-2" }) catch unreachable;
    _ = c.exec("select pg_notify($1, null)", .{"chan-1"}) catch unreachable;
}
