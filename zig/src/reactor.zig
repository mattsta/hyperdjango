// HTTP connection reactor — kqueue (macOS/BSD) / epoll (Linux).
//
// Stage 1 of the reactor + worker-pool HTTP design
// (docs/design/http-connection-reactor.md). This is a thin, self-contained
// readiness multiplexer: register many socket fds with read/write interest,
// wait for readiness events, and wake the loop from another thread via a
// self-pipe. It runs NO Python and owns NO connection state — it only answers
// "which fds are ready?" so a reactor thread can drive non-blocking I/O over
// thousands of connections without a thread per connection.
//
// Deliberately isolated and independently unit-tested: wiring it into the HTTP
// serving path (framing, worker exchange, backpressure) is a later stage, so
// landing this cannot regress the live server.

const std = @import("std");
const builtin = @import("builtin");
const posix = std.posix;

const is_bsd = switch (builtin.os.tag) {
    .macos, .ios, .tvos, .watchos, .freebsd, .netbsd, .openbsd, .dragonfly => true,
    else => false,
};

pub const Interest = struct {
    read: bool = false,
    write: bool = false,

    pub const read_only: Interest = .{ .read = true };
    pub const write_only: Interest = .{ .write = true };
    pub const read_write: Interest = .{ .read = true, .write = true };
};

/// A readiness event for one fd. `wake` marks the self-pipe wakeup (its fd is
/// internal; callers just observe `wake == true` and re-check their work queue).
pub const Event = struct {
    fd: posix.fd_t,
    readable: bool = false,
    writable: bool = false,
    // Peer closed / error — the connection is done; the owner should close it.
    hangup: bool = false,
    wake: bool = false,
};

pub const ReactorError = error{
    CreateFailed,
    PipeFailed,
    RegisterFailed,
};

pub const Reactor = struct {
    // kqueue fd on BSD, epoll fd on Linux.
    poll_fd: posix.fd_t,
    // Self-pipe for cross-thread wakeup: write a byte to `wake_w` to make the
    // next wait() return promptly with a `wake` event.
    wake_r: posix.fd_t,
    wake_w: posix.fd_t,

    pub fn init() ReactorError!Reactor {
        const poll_fd = createPollFd() orelse return error.CreateFailed;
        errdefer _ = posix.system.close(poll_fd);

        // Self-pipe (read end non-blocking so drain() never blocks).
        var fds: [2]posix.fd_t = undefined;
        if (std.c.pipe(&fds) != 0) return error.PipeFailed;
        // Close both pipe ends if registration fails — they used to leak on the
        // RegisterFailed path (only poll_fd was covered by an errdefer).
        errdefer {
            _ = posix.system.close(fds[0]);
            _ = posix.system.close(fds[1]);
        }
        setNonblock(fds[0]);
        setNonblock(fds[1]);

        var self = Reactor{ .poll_fd = poll_fd, .wake_r = fds[0], .wake_w = fds[1] };
        // Register the read end so wait() returns when wake() is called.
        self.add(fds[0], Interest.read_only) catch return error.RegisterFailed;
        return self;
    }

    pub fn deinit(self: *Reactor) void {
        _ = posix.system.close(self.wake_r);
        _ = posix.system.close(self.wake_w);
        _ = posix.system.close(self.poll_fd);
    }

    /// Register a fd with the given interest (idempotent-ish: use modify() to
    /// change interest on an already-registered fd).
    pub fn add(self: *Reactor, fd: posix.fd_t, interest: Interest) ReactorError!void {
        if (is_bsd) {
            self.kqueueApply(fd, interest) catch return error.RegisterFailed;
        } else {
            self.epollApply(fd, interest, EPOLL_CTL_ADD) catch return error.RegisterFailed;
        }
    }

    /// Change the interest set for an already-registered fd.
    pub fn modify(self: *Reactor, fd: posix.fd_t, interest: Interest) ReactorError!void {
        if (is_bsd) {
            self.kqueueApply(fd, interest) catch return error.RegisterFailed;
        } else {
            self.epollApply(fd, interest, EPOLL_CTL_MOD) catch return error.RegisterFailed;
        }
    }

    /// Register a fd for a SINGLE read-readiness delivery (EV_ONESHOT on kqueue,
    /// EPOLLONESHOT on epoll): the filter auto-disables the instant it fires, so
    /// wait() delivers the event with the fd ALREADY disarmed — no explicit
    /// remove() syscall is ever needed for the readiness baton. Use for the
    /// first arm of a freshly-accepted connection. kevent()/epoll_ctl() on a
    /// shared poll fd is thread-safe, so the acceptor thread may call this
    /// directly without involving the reactor thread.
    pub fn addOneshotRead(self: *Reactor, fd: posix.fd_t) ReactorError!void {
        if (is_bsd) {
            self.kqueueOneshotRead(fd) catch return error.RegisterFailed;
        } else {
            self.epollOneshotRead(fd, EPOLL_CTL_ADD) catch return error.RegisterFailed;
        }
    }

    /// Re-arm a fd for its NEXT single read-readiness after a ONESHOT event
    /// fired. Thread-safe: the worker that just served a request on `fd` calls
    /// this directly on the owning shard's poll fd — no register queue, no
    /// self-pipe wake, no reactor-thread round trip. The single-owner invariant
    /// holds because the fd is disarmed (owned by this worker) until the call
    /// returns. On kqueue this re-adds the auto-deleted filter (identical to the
    /// first arm); on epoll the fd is still present but disabled, so it must be
    /// MODified rather than ADDed.
    pub fn rearmOneshotRead(self: *Reactor, fd: posix.fd_t) ReactorError!void {
        if (is_bsd) {
            self.kqueueOneshotRead(fd) catch return error.RegisterFailed;
        } else {
            self.epollOneshotRead(fd, EPOLL_CTL_MOD) catch return error.RegisterFailed;
        }
    }

    /// Stop watching a fd. Best-effort (the fd may already be closed).
    pub fn remove(self: *Reactor, fd: posix.fd_t) void {
        if (is_bsd) {
            // Delete both filters; ignore ENOENT for a filter that wasn't set.
            // EV_RECEIPT makes kevent() process BOTH deletes even when the
            // first returns ENOENT — without it a benign ENOENT on the
            // READ-DELETE would abort the call and leak a still-registered
            // WRITE filter. Best-effort: per-change receipt errors are ignored.
            const del: u16 = posix.system.EV.DELETE | posix.system.EV.RECEIPT;
            var changes: [2]posix.Kevent = .{
                kev(fd, posix.system.EVFILT.READ, del),
                kev(fd, posix.system.EVFILT.WRITE, del),
            };
            var receipts: [2]posix.Kevent = undefined;
            _ = std.c.kevent(self.poll_fd, &changes, 2, &receipts, 2, null);
        } else {
            _ = epoll_ctl(self.poll_fd, EPOLL_CTL_DEL, fd, null);
        }
    }

    /// Wake a blocked wait() from another thread.
    pub fn wake(self: *Reactor) void {
        const one = [_]u8{1};
        _ = std.c.write(self.wake_w, &one, 1);
    }

    /// Wait for readiness. Fills `out` with up to out.len events; returns the
    /// count. `timeout_ms < 0` blocks indefinitely. A wakeup surfaces as an
    /// event with `wake == true` (and the self-pipe is drained).
    pub fn wait(self: *Reactor, out: []Event, timeout_ms: i32) usize {
        if (is_bsd) return self.kqueueWait(out, timeout_ms);
        return self.epollWait(out, timeout_ms);
    }

    // ── kqueue (BSD/macOS) ──────────────────────────────────────────────────

    fn kqueueApply(self: *Reactor, fd: posix.fd_t, interest: Interest) !void {
        // EV_RECEIPT forces kevent() to PROCESS EVERY change and post a
        // per-change status event (EV_ERROR with data = errno, 0 = success)
        // into the eventlist, instead of returning -1 and ABORTING the whole
        // changelist at the first EV_ERROR. Without it, switching an fd to
        // write-only submits [READ-DELETE, WRITE-ADD]; if the READ filter was
        // never registered the leading DELETE returns ENOENT, kevent() stops,
        // and the WRITE-ADD is silently dropped — write readiness would never
        // arm. EV_RECEIPT also lets us surface a GENUINE registration failure
        // (EBADF, …) that the old fire-and-forget `_ = kevent(...)` swallowed.
        const add_enable: u16 = posix.system.EV.ADD | posix.system.EV.ENABLE | posix.system.EV.RECEIPT;
        const del: u16 = posix.system.EV.DELETE | posix.system.EV.RECEIPT;
        var changes: [2]posix.Kevent = .{
            kev(fd, posix.system.EVFILT.READ, if (interest.read) add_enable else del),
            kev(fd, posix.system.EVFILT.WRITE, if (interest.write) add_enable else del),
        };
        var receipts: [2]posix.Kevent = undefined;
        const rc = std.c.kevent(self.poll_fd, &changes, 2, &receipts, 2, null);
        if (rc < 0) return error.RegisterFailed; // kevent() itself failed
        for (receipts[0..@intCast(rc)]) |r| {
            if ((r.flags & posix.system.EV.ERROR) == 0) continue;
            const err: i64 = @intCast(r.data); // this change's errno; 0 = success
            if (err == 0) continue;
            // A DELETE of a filter that was never registered returns ENOENT —
            // benign (we only wanted it gone). Anything else is a real failure.
            const was_delete = if (r.filter == posix.system.EVFILT.READ) !interest.read else !interest.write;
            if (err == @as(i64, @intFromEnum(posix.E.NOENT)) and was_delete) continue;
            return error.RegisterFailed;
        }
    }

    fn kqueueOneshotRead(self: *Reactor, fd: posix.fd_t) !void {
        // ADD|ENABLE|ONESHOT: deliver read readiness exactly once, then the
        // kernel auto-deletes the filter. A subsequent kqueueOneshotRead re-adds
        // it for the next request.
        const flags: u16 = posix.system.EV.ADD | posix.system.EV.ENABLE | posix.system.EV.ONESHOT;
        var changes: [1]posix.Kevent = .{kev(fd, posix.system.EVFILT.READ, flags)};
        var none: [0]posix.Kevent = .{};
        // nevents=0 → returns 0 on success, -1 (errno set to the first failing
        // change, e.g. EBADF for a closed fd) on failure.
        if (std.c.kevent(self.poll_fd, &changes, 1, &none, 0, null) < 0) return error.RegisterFailed;
    }

    fn kqueueWait(self: *Reactor, out: []Event, timeout_ms: i32) usize {
        var evs: [128]posix.Kevent = undefined;
        const cap = @min(out.len, evs.len);
        if (cap == 0) return 0;
        var ts: posix.timespec = undefined;
        const ts_ptr: ?*const posix.timespec = if (timeout_ms < 0) null else blk: {
            ts = .{
                .sec = @intCast(@divTrunc(timeout_ms, 1000)),
                .nsec = @intCast(@mod(timeout_ms, 1000) * std.time.ns_per_ms),
            };
            break :blk &ts;
        };
        var none: [0]posix.Kevent = .{};
        const rc = std.c.kevent(self.poll_fd, &none, 0, &evs, @intCast(cap), ts_ptr);
        if (rc <= 0) return 0;
        const count: usize = @intCast(rc);
        var out_n: usize = 0;
        for (evs[0..count]) |e| {
            const fd: posix.fd_t = @intCast(e.ident);
            if (fd == self.wake_r) {
                self.drainWake();
                out[out_n] = .{ .fd = fd, .wake = true };
                out_n += 1;
                continue;
            }
            const hup = (e.flags & posix.system.EV.EOF) != 0;
            out[out_n] = .{
                .fd = fd,
                .readable = e.filter == posix.system.EVFILT.READ,
                .writable = e.filter == posix.system.EVFILT.WRITE,
                .hangup = hup,
            };
            out_n += 1;
        }
        return out_n;
    }

    // ── epoll (Linux) ───────────────────────────────────────────────────────

    fn epollApply(self: *Reactor, fd: posix.fd_t, interest: Interest, op: c_int) !void {
        var ev = EpollEvent{ .events = 0, .data = .{ .fd = fd } };
        if (interest.read) ev.events |= EPOLLIN;
        if (interest.write) ev.events |= EPOLLOUT;
        ev.events |= EPOLLRDHUP;
        if (epoll_ctl(self.poll_fd, op, fd, &ev) != 0) return error.RegisterFailed;
    }

    fn epollOneshotRead(self: *Reactor, fd: posix.fd_t, op: c_int) !void {
        // EPOLLONESHOT: after one delivery the fd is disabled (but stays in the
        // set), so re-arming uses EPOLL_CTL_MOD, not ADD. RDHUP so a peer close
        // still surfaces as a hangup even while armed one-shot.
        var ev = EpollEvent{ .events = EPOLLIN | EPOLLONESHOT | EPOLLRDHUP, .data = .{ .fd = fd } };
        if (epoll_ctl(self.poll_fd, op, fd, &ev) != 0) return error.RegisterFailed;
    }

    fn epollWait(self: *Reactor, out: []Event, timeout_ms: i32) usize {
        var evs: [128]EpollEvent = undefined;
        const cap = @min(out.len, evs.len);
        if (cap == 0) return 0;
        const rc = epoll_wait(self.poll_fd, &evs, @intCast(cap), timeout_ms);
        if (rc <= 0) return 0;
        const count: usize = @intCast(rc);
        var out_n: usize = 0;
        for (evs[0..count]) |e| {
            const fd = e.data.fd;
            if (fd == self.wake_r) {
                self.drainWake();
                out[out_n] = .{ .fd = fd, .wake = true };
                out_n += 1;
                continue;
            }
            const hup = (e.events & (EPOLLHUP | EPOLLERR | EPOLLRDHUP)) != 0;
            out[out_n] = .{
                .fd = fd,
                .readable = (e.events & EPOLLIN) != 0,
                .writable = (e.events & EPOLLOUT) != 0,
                .hangup = hup,
            };
            out_n += 1;
        }
        return out_n;
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    fn drainWake(self: *Reactor) void {
        var buf: [256]u8 = undefined;
        while (std.c.read(self.wake_r, &buf, buf.len) > 0) {}
    }
};

fn createPollFd() ?posix.fd_t {
    if (is_bsd) {
        const rc = posix.system.kqueue();
        if (rc < 0) return null;
        return @intCast(rc);
    } else {
        const rc = epoll_create1(0);
        if (rc < 0) return null;
        return @intCast(rc);
    }
}

fn setNonblock(fd: posix.fd_t) void {
    const flags = std.c.fcntl(fd, F_GETFL, @as(c_int, 0));
    if (flags >= 0) _ = std.c.fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

fn kev(fd: posix.fd_t, filter: i16, flags: u16) posix.Kevent {
    return .{
        .ident = @intCast(fd),
        .filter = filter,
        .flags = flags,
        .fflags = 0,
        .data = 0,
        .udata = 0,
    };
}

// fcntl constants (stable across Linux/macOS for these).
const F_GETFL: c_int = 3;
const F_SETFL: c_int = 4;
const O_NONBLOCK: c_int = if (is_bsd) 0x0004 else 0o4000;

extern "c" fn fcntl(fd: c_int, cmd: c_int, ...) c_int;

// ── epoll externs (Linux) ───────────────────────────────────────────────────
const EPOLL_CTL_ADD: c_int = 1;
const EPOLL_CTL_DEL: c_int = 2;
const EPOLL_CTL_MOD: c_int = 3;
const EPOLLIN: u32 = 0x001;
const EPOLLOUT: u32 = 0x004;
const EPOLLERR: u32 = 0x008;
const EPOLLHUP: u32 = 0x010;
const EPOLLRDHUP: u32 = 0x2000;
const EPOLLONESHOT: u32 = 0x40000000;

const EpollData = extern union {
    ptr: ?*anyopaque,
    fd: c_int,
    u32_: u32,
    u64_: u64,
};
const EpollEvent = switch (builtin.cpu.arch) {
    // x86_64 packs epoll_event; other arches are naturally aligned.
    .x86_64 => extern struct { events: u32, data: EpollData align(4) },
    else => extern struct { events: u32, data: EpollData },
};

extern "c" fn epoll_create1(flags: c_int) c_int;
extern "c" fn epoll_ctl(epfd: c_int, op: c_int, fd: c_int, event: ?*EpollEvent) c_int;
extern "c" fn epoll_wait(epfd: c_int, events: [*]EpollEvent, maxevents: c_int, timeout: c_int) c_int;

// ── tests ────────────────────────────────────────────────────────────────────

test "reactor: wake surfaces a wake event" {
    var r = try Reactor.init();
    defer r.deinit();

    r.wake();
    var events: [8]Event = undefined;
    const n = r.wait(&events, 1000);
    try std.testing.expect(n >= 1);
    var saw_wake = false;
    for (events[0..n]) |e| {
        if (e.wake) saw_wake = true;
    }
    try std.testing.expect(saw_wake);
}

test "reactor: socketpair read readiness" {
    var r = try Reactor.init();
    defer r.deinit();

    var sv: [2]c_int = undefined;
    // AF_UNIX=1 (linux) / 1 (mac); SOCK_STREAM=1.
    const af_unix: c_int = 1;
    const sock_stream: c_int = 1;
    try std.testing.expect(std.c.socketpair(af_unix, sock_stream, 0, &sv) == 0);
    defer _ = posix.system.close(sv[0]);
    defer _ = posix.system.close(sv[1]);

    try r.add(sv[0], Interest.read_only);

    // Before any write: no readiness (short timeout).
    var events: [8]Event = undefined;
    try std.testing.expect(r.wait(&events, 50) == 0);

    // Write to the peer → sv[0] becomes readable.
    const msg = [_]u8{ 'h', 'i' };
    _ = std.c.write(sv[1], &msg, msg.len);
    const n = r.wait(&events, 1000);
    try std.testing.expect(n == 1);
    try std.testing.expect(events[0].fd == sv[0]);
    try std.testing.expect(events[0].readable);
}

test "reactor: remove stops readiness" {
    var r = try Reactor.init();
    defer r.deinit();

    var sv: [2]c_int = undefined;
    try std.testing.expect(std.c.socketpair(1, 1, 0, &sv) == 0);
    defer _ = posix.system.close(sv[0]);
    defer _ = posix.system.close(sv[1]);

    try r.add(sv[0], Interest.read_only);
    r.remove(sv[0]);

    const msg = [_]u8{'x'};
    _ = std.c.write(sv[1], &msg, 1);
    var events: [8]Event = undefined;
    // Only the (unrelated) wake fd is registered now; sv[0] must not surface.
    const n = r.wait(&events, 50);
    for (events[0..n]) |e| try std.testing.expect(e.fd != sv[0]);
}

test "reactor: oneshot read auto-disarms, re-arm restores readiness" {
    var r = try Reactor.init();
    defer r.deinit();

    var sv: [2]c_int = undefined;
    try std.testing.expect(std.c.socketpair(1, 1, 0, &sv) == 0);
    defer _ = posix.system.close(sv[0]);
    defer _ = posix.system.close(sv[1]);

    try r.addOneshotRead(sv[0]);

    // First byte → sv[0] surfaces readable exactly once.
    _ = std.c.write(sv[1], &[_]u8{'a'}, 1);
    var events: [8]Event = undefined;
    var n = r.wait(&events, 1000);
    try std.testing.expect(n == 1 and events[0].fd == sv[0] and events[0].readable);

    // We never drained the byte, so it's still readable — but the oneshot
    // filter auto-disarmed, so wait() must NOT surface sv[0] again.
    _ = std.c.write(sv[1], &[_]u8{'b'}, 1);
    n = r.wait(&events, 50);
    for (events[0..n]) |e| try std.testing.expect(e.fd != sv[0]);

    // Re-arm → the still-pending data surfaces readiness once more.
    try r.rearmOneshotRead(sv[0]);
    n = r.wait(&events, 1000);
    try std.testing.expect(n == 1 and events[0].fd == sv[0] and events[0].readable);
}

// Regression for the kqueueApply "abort on first EV_ERROR" bug: arming WRITE
// interest submits [READ-DELETE, WRITE-ADD]; when the READ filter was never
// registered the leading DELETE returns ENOENT. The OLD code let that ENOENT
// abort the whole changelist so the WRITE-ADD never applied and write
// readiness never armed. A fresh connected socket is immediately writable, so
// the WRITE filter must fire once armed. (Latent on Linux/epoll, which applies
// changes independently; this asserts the kqueue path matches that behavior.)
test "reactor: write interest arms even when read filter was never registered" {
    var r = try Reactor.init();
    defer r.deinit();

    var sv: [2]c_int = undefined;
    try std.testing.expect(std.c.socketpair(1, 1, 0, &sv) == 0);
    defer _ = posix.system.close(sv[0]);
    defer _ = posix.system.close(sv[1]);

    // WRITE-only on an fd whose READ filter was never added.
    try r.add(sv[0], Interest.write_only);

    var events: [8]Event = undefined;
    const n = r.wait(&events, 1000);
    var saw_writable = false;
    for (events[0..n]) |e| {
        if (e.fd == sv[0] and e.writable) saw_writable = true;
    }
    try std.testing.expect(saw_writable);

    // Flip back to READ-only (submits WRITE-DELETE + READ-ADD): the WRITE
    // filter must go away and READ must arm, proving both changes applied.
    try r.modify(sv[0], Interest.read_only);
    _ = std.c.write(sv[1], &[_]u8{'q'}, 1);
    const n2 = r.wait(&events, 1000);
    var saw_readable = false;
    var saw_writable2 = false;
    for (events[0..n2]) |e| {
        if (e.fd == sv[0] and e.readable) saw_readable = true;
        if (e.fd == sv[0] and e.writable) saw_writable2 = true;
    }
    try std.testing.expect(saw_readable);
    try std.testing.expect(!saw_writable2);
}
