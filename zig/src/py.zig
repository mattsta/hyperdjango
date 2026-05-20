// Thin wrappers around the Python C-API, imported via @cImport.
// Everything the rest of the Zig codebase needs goes through here.

pub const c = @cImport({
    @cDefine("PY_SSIZE_T_CLEAN", {});
    // Under ThreadSanitizer, Zig's translate-C stops selecting CPython's
    // GCC-builtin atomics header and falls into the C11 <stdatomic.h> path,
    // whose memory_order_* symbols aren't available in this translation unit
    // (74 compile errors). Forcing the GCC-builtin path — identical to the
    // non-TSan baseline — fixes it. Only under -Dsanitize-thread.
    if (@import("build_options").sanitize_thread) {
        @cDefine("_Py_USE_GCC_BUILTIN_ATOMICS", "1");
    }
    @cInclude("Python.h");
});

// Re-export the types we use everywhere
pub const PyObject = c.PyObject;
pub const PyMethodDef = c.PyMethodDef;
pub const PyModuleDef = c.PyModuleDef;
pub const PyModuleDef_Base = c.PyModuleDef_Base;
pub const Py_ssize_t = c.Py_ssize_t;

// ── Constants ──
pub const METH_VARARGS = c.METH_VARARGS;
pub const METH_KEYWORDS = c.METH_VARARGS | c.METH_KEYWORDS;
pub const METH_NOARGS = c.METH_NOARGS;

// ── Helpers ──

pub fn incref(obj: *PyObject) *PyObject {
    c.Py_IncRef(obj);
    return obj;
}

pub fn decref(obj: *PyObject) void {
    c.Py_DecRef(obj);
}

pub fn none() *PyObject {
    return incref(@as(*PyObject, @ptrCast(c._Py_NoneStruct[0..])));
}

pub fn pyNone() *PyObject {
    return incref(&c._Py_NoneStruct);
}

pub fn pyTrue() *PyObject {
    return incref(@ptrCast(&c._Py_TrueStruct));
}

pub fn pyFalse() *PyObject {
    return incref(@ptrCast(&c._Py_FalseStruct));
}

pub fn isNone(obj: *PyObject) bool {
    return obj == @as(*PyObject, @ptrCast(&c._Py_NoneStruct));
}

pub fn setError(comptime fmt: []const u8, args: anytype) void {
    var buf: [1024]u8 = undefined;
    const msg = std.fmt.bufPrintZ(&buf, fmt, args) catch {
        c.PyErr_SetString(c.PyExc_RuntimeError, "internal error");
        return;
    };
    c.PyErr_SetString(c.PyExc_RuntimeError, msg.ptr);
}

/// Raise a Python `ValueError` instead of `RuntimeError`. Use for
/// input-validation failures so callers can write idiomatic
/// `except ValueError:` instead of catching RuntimeError.
pub fn setValueError(comptime fmt: []const u8, args: anytype) void {
    var buf: [1024]u8 = undefined;
    const msg = std.fmt.bufPrintZ(&buf, fmt, args) catch {
        c.PyErr_SetString(c.PyExc_ValueError, "internal error");
        return;
    };
    c.PyErr_SetString(c.PyExc_ValueError, msg.ptr);
}

pub fn newString(s: []const u8) ?*PyObject {
    return c.PyUnicode_FromStringAndSize(@ptrCast(s.ptr), @intCast(s.len));
}

pub fn newBytes(data: []const u8) ?*PyObject {
    return c.PyBytes_FromStringAndSize(@ptrCast(data.ptr), @intCast(data.len));
}

pub fn newInt(val: i64) ?*PyObject {
    return c.PyLong_FromLongLong(val);
}

pub fn newDict() ?*PyObject {
    return c.PyDict_New();
}

pub fn dictSetItemString(dict: *PyObject, key: [*:0]const u8, val: *PyObject) bool {
    return c.PyDict_SetItemString(dict, key, val) == 0;
}

pub fn newList(size: usize) ?*PyObject {
    return c.PyList_New(@intCast(size));
}

pub fn createModule(def: *PyModuleDef) ?*PyObject {
    return c.PyModule_Create(def);
}

pub fn moduleAddObject(module: *PyObject, name: [*:0]const u8, obj: *PyObject) bool {
    return c.PyModule_AddObject(module, name, obj) == 0;
}

pub fn parseArgs(args: ?*PyObject, fmt: [*:0]const u8, ptrs: anytype) bool {
    return @call(.auto, c.PyArg_ParseTuple, .{ args, fmt } ++ ptrs) != 0;
}

const std = @import("std");
const builtin = @import("builtin");

// ── Zig 0.16 compat: std.Thread.Mutex → pthread_mutex (C library context, no Io) ──
pub const Mutex = struct {
    inner: std.c.pthread_mutex_t = std.c.PTHREAD_MUTEX_INITIALIZER,
    pub fn lock(self: *Mutex) void {
        _ = std.c.pthread_mutex_lock(&self.inner);
    }
    pub fn unlock(self: *Mutex) void {
        _ = std.c.pthread_mutex_unlock(&self.inner);
    }
};

pub const RwLock = struct {
    // Zig's `pthread_rwlock_t` carries per-platform DEFAULT field values that
    // form a valid static initializer (on Darwin: `sig = 0x2DA8B3B4`, the
    // `_PTHREAD_RWLOCK_SIG_init` magic; on Linux: all-zero, i.e.
    // PTHREAD_RWLOCK_INITIALIZER). `std.mem.zeroes` DESTROYS that default —
    // on Darwin it left `sig = 0`, an invalid signature, so every
    // `pthread_rwlock_rdlock`/`wrlock` returned EINVAL and silently acquired
    // NOTHING. That turned every RwLock into a no-op → real data races under
    // free-threading (metric label maps, response cache) → SIGSEGV. Use the
    // type's own default initializer instead, which is correct on all targets.
    inner: std.c.pthread_rwlock_t = .{},
    pub fn lock(self: *RwLock) void {
        _ = std.c.pthread_rwlock_wrlock(&self.inner);
    }
    pub fn unlock(self: *RwLock) void {
        _ = std.c.pthread_rwlock_unlock(&self.inner);
    }
    pub fn lockShared(self: *RwLock) void {
        _ = std.c.pthread_rwlock_rdlock(&self.inner);
    }
    pub fn unlockShared(self: *RwLock) void {
        _ = std.c.pthread_rwlock_unlock(&self.inner);
    }
};

pub const Condition = struct {
    inner: std.c.pthread_cond_t = std.c.PTHREAD_COND_INITIALIZER,
    pub fn signal(self: *Condition) void {
        _ = std.c.pthread_cond_signal(&self.inner);
    }
    pub fn broadcast(self: *Condition) void {
        _ = std.c.pthread_cond_broadcast(&self.inner);
    }
    pub fn wait(self: *Condition, mutex: *Mutex) void {
        _ = std.c.pthread_cond_wait(&self.inner, &mutex.inner);
    }
};

// ── Zig 0.16 compat: std.time.nanoTimestamp/timestamp → clock_gettime via libc ──
pub fn nanoTimestamp() i128 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
    return @as(i128, ts.sec) * std.time.ns_per_s + ts.nsec;
}

pub fn timestamp() i64 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
    return @intCast(ts.sec);
}

// ── Zig 0.16 compat: std.Thread.sleep removed → libc nanosleep ──
pub fn sleep(ns: u64) void {
    var ts: std.c.timespec = .{
        .sec = @intCast(ns / std.time.ns_per_s),
        .nsec = @intCast(ns % std.time.ns_per_s),
    };
    _ = std.c.nanosleep(&ts, &ts);
}

// ── Zig 0.16 compat: std.net.Stream removed → libc-backed NetStream ──
pub const NetStream = struct {
    handle: std.posix.fd_t,

    pub fn read(self: NetStream, buf: []u8) !usize {
        const rc = std.c.read(self.handle, buf.ptr, buf.len);
        if (rc < 0) return error.ReadFailed;
        if (rc == 0) return error.EndOfStream;
        return @intCast(rc);
    }

    pub fn write(self: NetStream, data: []const u8) !usize {
        while (true) {
            const rc = std.c.write(self.handle, data.ptr, data.len);
            if (rc < 0) {
                // Signals are installed without SA_RESTART, so a benign EINTR must
                // be retried rather than reported as a write failure (which would
                // desync a keep-alive connection). Any other errno is a genuine
                // failure — the caller must close the connection.
                if (std.c.errno(rc) == .INTR) continue;
                return error.WriteFailed;
            }
            // A 0-byte return makes no forward progress; treat it as a failure so
            // writeAll can't spin forever on it (mirrors writevAll's rc==0 guard).
            if (rc == 0) return error.WriteFailed;
            return @intCast(rc);
        }
    }

    pub fn writeAll(self: NetStream, data: []const u8) !void {
        var written: usize = 0;
        while (written < data.len) {
            const n = try self.write(data[written..]);
            written += n;
        }
    }

    // Max non-empty segments gathered into a single `writev`. Response paths use
    // ≤4 (header, cors/extra_headers, trailer, body); frames use 2.
    pub const MAX_IOV = 8;

    /// Write N byte segments as a single `writev` syscall instead of N separate
    /// `write` calls — no copy of any segment (notably the response body).
    /// Empty segments are skipped. Partial writes are handled correctly across
    /// segment boundaries by advancing an intra-segment offset and re-issuing
    /// `writev` for the unwritten tail. The common case is exactly one syscall.
    pub fn writeAllVectored(self: NetStream, segments: []const []const u8) !void {
        var iov: [MAX_IOV]std.posix.iovec_const = undefined;
        var count: usize = 0;
        for (segments) |seg| {
            if (seg.len == 0) continue; // writev on a 0-length iovec is wasteful
            if (count == MAX_IOV) {
                // More non-empty segments than one writev batch can hold — flush
                // what we've gathered so far, preserving order, then continue.
                try self.writevAll(iov[0..count]);
                count = 0;
            }
            iov[count] = .{ .base = seg.ptr, .len = seg.len };
            count += 1;
        }
        if (count == 0) return;
        try self.writevAll(iov[0..count]);
    }

    /// Flush a prepared iovec array, tolerating partial writes.
    fn writevAll(self: NetStream, iov: []std.posix.iovec_const) !void {
        var start: usize = 0;
        while (start < iov.len) {
            const rc = std.c.writev(self.handle, iov[start..].ptr, @intCast(iov.len - start));
            if (rc < 0) {
                // EINTR (no SA_RESTART) → retry without advancing. Any other errno
                // — including EAGAIN from an SO_SNDTIMEO timeout on a stalled
                // (zero-window) client — is a genuine failure: return so the caller
                // closes the connection instead of continuing a desynced keep-alive.
                if (std.c.errno(rc) == .INTR) continue;
                return error.WriteFailed;
            }
            var advanced: usize = @intCast(rc);
            if (advanced == 0) return error.WriteFailed; // no progress → avoid spin
            while (advanced > 0 and start < iov.len) {
                const seg = &iov[start];
                if (advanced >= seg.len) {
                    advanced -= seg.len;
                    start += 1;
                } else {
                    seg.base += advanced;
                    seg.len -= advanced;
                    advanced = 0;
                }
            }
        }
    }

    /// Two-segment convenience wrapper (e.g. a frame header + its payload).
    pub fn writeAllVectored2(self: NetStream, a: []const u8, b: []const u8) !void {
        return self.writeAllVectored(&.{ a, b });
    }

    pub fn close(self: NetStream) void {
        _ = std.c.close(self.handle);
    }

    pub const TryReadResult = union(enum) {
        n: usize,
        would_block,
        closed,
    };

    /// Single-attempt, non-blocking read via a per-call MSG_DONTWAIT flag —
    /// unlike toggling O_NONBLOCK on the fd, this leaves the socket's
    /// blocking mode (and therefore `write`/`writeAll`) completely
    /// unaffected. Used by the WebSocket receive path to avoid handing a
    /// blocking read off to a thread pool when no thread-hop is needed
    /// (see websocket_server.zig's tryRecvFrame).
    pub fn tryRead(self: NetStream, buf: []u8) TryReadResult {
        const rc = std.c.recv(self.handle, buf.ptr, buf.len, std.c.MSG.DONTWAIT);
        if (rc == 0) return .closed;
        if (rc < 0) {
            return switch (std.c.errno(rc)) {
                .AGAIN, .INTR => .would_block,
                else => .closed,
            };
        }
        return .{ .n = @intCast(rc) };
    }

    pub const TryWriteResult = union(enum) {
        n: usize,
        would_block,
        closed,
    };

    /// Single-attempt, non-blocking send of one contiguous buffer via a per-call
    /// MSG_DONTWAIT flag — the send-side mirror of tryRead. Leaves the socket's
    /// blocking mode (and therefore the blocking write/recv fallbacks) untouched,
    /// unlike toggling O_NONBLOCK on the fd. EAGAIN → .would_block (caller buffers
    /// the unsent tail and waits for writability via asyncio add_writer); EINTR is
    /// retried. This is what keeps one slow/zero-window WebSocket consumer from
    /// stalling the shared event-loop thread (head-of-line blocking).
    pub fn trySend(self: NetStream, data: []const u8) TryWriteResult {
        while (true) {
            const rc = std.c.send(self.handle, data.ptr, data.len, std.c.MSG.DONTWAIT);
            if (rc >= 0) return .{ .n = @intCast(rc) };
            switch (std.c.errno(rc)) {
                .INTR => continue,
                // EWOULDBLOCK == EAGAIN on darwin and Linux, so one arm covers both.
                .AGAIN => return .would_block,
                else => return .closed,
            }
        }
    }

    /// Vectored form of trySend: a single non-blocking `sendmsg` over several
    /// segments, so the common fully-sent case costs one syscall and zero copies
    /// (the caller copies only the unsent remainder into its outbound buffer).
    /// Used for the send fast path — [frame header, payload] in one call.
    pub fn trySendv(self: NetStream, iov: []const std.posix.iovec_const) TryWriteResult {
        while (true) {
            const msg: std.c.msghdr_const = .{
                .name = null,
                .namelen = 0,
                .iov = iov.ptr,
                .iovlen = @intCast(iov.len),
                .control = null,
                .controllen = 0,
                .flags = 0,
            };
            const rc = std.c.sendmsg(self.handle, &msg, std.c.MSG.DONTWAIT);
            if (rc >= 0) return .{ .n = @intCast(rc) };
            switch (std.c.errno(rc)) {
                .INTR => continue,
                .AGAIN => return .would_block,
                else => return .closed,
            }
        }
    }
};

// ── Zig 0.16 compat: std.net.Address/Server removed → libc TCP server ──
extern "c" fn socket(domain: c_uint, sock_type: c_uint, protocol: c_uint) c_int;
extern "c" fn inet_pton(af: c_int, src: [*:0]const u8, dst: *anyopaque) c_int;

/// Listen backlog. Env HYPER_LISTEN_BACKLOG. The kernel clamps this to somaxconn
/// (kern.ipc.somaxconn on darwin, net.core.somaxconn on Linux) — raise the
/// sysctl too if you need a deeper accept queue. The old hardcoded 128 was
/// exactly macOS's default somaxconn, so the accept queue filled and dropped
/// SYNs under connection storms before userspace could shed.
///
/// 4096 (Linux's own default somaxconn), not 1024: a load generator that opens
/// N keep-alive connections at once needs an accept queue DEEPER than N, because
/// the queue must absorb the whole burst while the acceptor drains it. At
/// backlog == burst size the listener sits on a knife edge, and on overflow
/// Linux (tcp_abort_on_overflow=0) DROPS the client's final ACK rather than
/// resetting: the client believes it is connected, sends its request, and waits
/// through an exponential SYN-ACK backoff — zero responses, zero errors on
/// either side. Headroom here is the difference between "served" and "silently
/// invisible" for those connections.
const DEFAULT_LISTEN_BACKLOG: c_uint = 4096;

fn getListenBacklog() c_uint {
    const env_ptr = std.c.getenv("HYPER_LISTEN_BACKLOG") orelse return DEFAULT_LISTEN_BACKLOG;
    const env_val = std.mem.sliceTo(env_ptr, 0);
    const parsed = std.fmt.parseInt(c_uint, env_val, 10) catch return DEFAULT_LISTEN_BACKLOG;
    if (parsed == 0) return DEFAULT_LISTEN_BACKLOG;
    return parsed;
}

/// TCP_NODELAY (disable Nagle) is on by default; HYPER_TCP_NODELAY=0 opts out.
fn tcpNoDelayEnabled() bool {
    const env_ptr = std.c.getenv("HYPER_TCP_NODELAY") orelse return true;
    const env_val = std.mem.sliceTo(env_ptr, 0);
    return !std.mem.eql(u8, env_val, "0");
}

// Resolved once before any thread starts (server_run → resolveNetConfig), then
// read-only for the serving lifetime — no lock, no race. `getenv` is a linear
// scan of `environ`, and setTcpNoDelay used to call it once per ACCEPTED
// connection, i.e. inside the accept-queue drain loop where every microsecond of
// per-connection setup shrinks the burst the listener can absorb before the
// kernel silently drops connections.
var tcp_nodelay_resolved: bool = true;

/// Resolve the py.zig-owned socket knobs once. Call from server startup, before
/// the accept loop and any worker thread exists.
pub fn resolveNetConfig() void {
    tcp_nodelay_resolved = tcpNoDelayEnabled();
}

pub const TcpServer = struct {
    stream: NetStream,

    pub const Connection = struct {
        stream: NetStream,
    };

    pub fn init(host: []const u8, port: u16) !TcpServer {
        const sock = socket(std.c.AF.INET, std.c.SOCK.STREAM, 0);
        if (sock < 0) return error.SocketFailed;
        // Close the socket fd on any error before we hand it off in the return
        // value (HostTooLong / InvalidAddress / BindFailed / ListenFailed all
        // used to leak it). On success no error fires, so the fd survives.
        errdefer _ = std.c.close(sock);

        // SO_REUSEADDR — use platform-correct constants. The old call passed
        // Linux's SOL_SOCKET=1/SO_REUSEADDR=2, which are wrong on darwin
        // (SOL_SOCKET=0xffff, SO_REUSEADDR=4) so it failed silently there.
        // Direct libc call (see py.setTcpNoDelay): std.posix.setsockopt asserts
        // `unreachable` on unexpected errnos, which panics under ReleaseSafe;
        // best-effort here, so ignore the result.
        var optval: c_int = 1;
        _ = std.c.setsockopt(sock, @as(c_int, std.posix.SOL.SOCKET), @as(c_int, std.posix.SO.REUSEADDR), std.mem.asBytes(&optval), @sizeOf(c_int));

        // Build sockaddr_in
        var addr: std.c.sockaddr.in = std.mem.zeroes(std.c.sockaddr.in);
        addr.family = std.c.AF.INET;
        addr.port = std.mem.nativeToBig(u16, port);

        // Parse host
        var host_buf: [256]u8 = undefined;
        if (host.len >= host_buf.len) return error.HostTooLong;
        @memcpy(host_buf[0..host.len], host);
        host_buf[host.len] = 0;
        if (inet_pton(std.c.AF.INET, @ptrCast(&host_buf), &addr.addr) != 1) return error.InvalidAddress;

        if (std.c.bind(sock, @ptrCast(&addr), @sizeOf(std.c.sockaddr.in)) < 0) return error.BindFailed;
        if (std.c.listen(sock, getListenBacklog()) < 0) return error.ListenFailed;

        return .{ .stream = .{ .handle = sock } };
    }

    pub fn accept(self: *TcpServer) !Connection {
        const fd = std.c.accept(self.stream.handle, null, null);
        if (fd < 0) return error.AcceptFailed;
        setTcpNoDelay(fd);
        return .{ .stream = .{ .handle = fd } };
    }

    /// Non-throwing accept for the non-blocking drain loop. Distinguishes an
    /// empty accept queue (.drained) from a transient interrupt (.retry) and
    /// real failures (.fatal), so the loop can drain the queue in one wakeup
    /// without spinning on errors.
    pub const AcceptResult = union(enum) {
        conn: Connection,
        drained, // EWOULDBLOCK/EAGAIN — accept queue empty
        retry, // EINTR — interrupted before a connection was accepted
        fatal, // ECONNABORTED/EMFILE/… — stop draining this wakeup
    };

    /// Per-connection socket setup for a freshly accepted fd. Deliberately NOT
    /// done inside tryAccept: the caller drains the whole kernel accept queue
    /// first and applies this afterwards, so the queue empties at the accept()
    /// syscall rate instead of the (much slower) accept+setup rate. A connection
    /// storm that outruns the drain is dropped by the kernel with NO error
    /// visible to either side, so drain speed is a correctness property.
    pub fn prepareAccepted(fd: std.posix.fd_t) void {
        // On BSD/macOS accept() inherits O_NONBLOCK from the (now non-blocking)
        // listen socket, but the request path does blocking reads with
        // SO_RCVTIMEO (both threaded and reactor modes) — so force the accepted
        // socket back to blocking. Harmless on Linux, where it isn't inherited.
        setBlocking(fd);
        setTcpNoDelay(fd);
    }

    pub fn tryAccept(self: *TcpServer) AcceptResult {
        const fd = std.c.accept(self.stream.handle, null, null);
        if (fd >= 0) {
            return .{ .conn = .{ .stream = .{ .handle = fd } } };
        }
        // EWOULDBLOCK == EAGAIN on both darwin and Linux, so one check covers both.
        return switch (std.c._errno().*) {
            @intFromEnum(std.c.E.AGAIN) => .drained,
            @intFromEnum(std.c.E.INTR) => .retry,
            else => .fatal,
        };
    }

    /// Make the listen socket non-blocking so the accept loop can drain the
    /// kernel accept queue in one wakeup (accept until EWOULDBLOCK) instead of
    /// one connection per poll().
    pub fn setNonBlocking(self: *TcpServer) void {
        const flags = std.c.fcntl(self.stream.handle, std.c.F.GETFL, @as(c_int, 0));
        if (flags < 0) return;
        _ = std.c.fcntl(self.stream.handle, std.c.F.SETFL, flags | @as(c_int, @bitCast(std.posix.O{ .NONBLOCK = true })));
    }

    pub fn deinit(self: *TcpServer) void {
        self.stream.close();
    }
};

/// Best-effort TCP_NODELAY on an accepted connection. Nagle + the peer's
/// delayed-ACK can add up to ~40ms to small keep-alive responses; disabling it
/// removes the keep-alive/reactor latency floor. Failures are ignored — a slow
/// socket is better than a refused connection. Toggle off via HYPER_TCP_NODELAY=0.
/// (Matches websocket_server.zig's per-upgrade setTcpNoDelay, extended to every
/// HTTP connection with an env opt-out.)
pub fn setTcpNoDelay(fd: std.posix.fd_t) void {
    if (!tcp_nodelay_resolved) return;
    var one: c_int = 1;
    // Direct libc call, NOT std.posix.setsockopt. That wrapper asserts
    // `unreachable` on EBADF/ENOTSOCK/EINVAL — exactly the errnos a peer that
    // has already closed/reset the just-accepted socket produces — and the
    // `catch {}` cannot catch a panic raised INSIDE the wrapper. Under
    // ReleaseFast the unreachable is silent UB (NODELAY quietly not set);
    // under ReleaseSafe it panics and takes down the server thread (observed
    // as 55 malformed-request fuzz failures). This is best-effort by design,
    // so the result is ignored.
    _ = std.c.setsockopt(fd, @as(c_int, std.posix.IPPROTO.TCP), @as(c_int, std.posix.TCP.NODELAY), &one, @sizeOf(c_int));
}

/// Clear O_NONBLOCK on a socket (force blocking). Best-effort.
pub fn setBlocking(fd: std.posix.fd_t) void {
    const flags = std.c.fcntl(fd, std.c.F.GETFL, @as(c_int, 0));
    if (flags < 0) return;
    const nonblock: c_int = @bitCast(std.posix.O{ .NONBLOCK = true });
    _ = std.c.fcntl(fd, std.c.F.SETFL, flags & ~nonblock);
}

// ── Zig 0.16 compat: std.fs.openFileAbsolute removed → libc-backed file helpers ──
// In Zig 0.16, std.c.Stat / std.c.fstat evaluate to `void` on Linux because the
// platform module routes through std.posix.system.* instead. To stay portable
// across both macOS and Linux without reaching for either of those broken
// abstractions, we use lseek(fd, 0, SEEK_END) for size() — the lseek API is
// stable in libc on every POSIX platform.
const SEEK_SET: c_int = 0;
const SEEK_END: c_int = 2;

pub const NativeFile = struct {
    fd: std.posix.fd_t,

    pub fn open(path: []const u8) !NativeFile {
        // Need null-terminated path for libc
        var path_buf: [4096]u8 = undefined;
        if (path.len >= path_buf.len) return error.PathTooLong;
        @memcpy(path_buf[0..path.len], path);
        path_buf[path.len] = 0;
        const fd = std.c.open(@ptrCast(&path_buf), std.c.O{}, @as(std.c.mode_t, 0));
        if (fd < 0) return error.FileNotFound;
        return .{ .fd = fd };
    }

    pub fn close(self: NativeFile) void {
        _ = std.c.close(self.fd);
    }

    /// Returns file size in bytes.
    ///
    /// SIDE EFFECT: leaves the file position at 0 (start). Original
    /// implementation used fstat which didn't move position, but
    /// fstat is broken on Linux Zig 0.16. Current callers (server.zig
    /// serveFile, static_helpers hashing) always seekTo(range_start)
    /// or readAll-from-start AFTER calling size(), so the reset to 0
    /// is harmless or actively desired. If you add a caller that
    /// relies on the file's current position being preserved, save
    /// it via seek-CUR before calling size() and restore after.
    pub fn size(self: NativeFile) !u64 {
        const end = std.c.lseek(self.fd, 0, SEEK_END);
        if (end < 0) return error.StatFailed;
        _ = std.c.lseek(self.fd, 0, SEEK_SET); // restore position to start
        return @intCast(end);
    }

    pub fn seekTo(self: NativeFile, pos: u64) !void {
        _ = std.c.lseek(self.fd, @intCast(pos), 0); // SEEK_SET = 0
    }

    pub fn read(self: NativeFile, buf: []u8) !usize {
        const rc = std.c.read(self.fd, buf.ptr, buf.len);
        if (rc < 0) return error.ReadFailed;
        return @intCast(rc);
    }

    pub fn readAll(self: NativeFile, buf: []u8) !usize {
        var total: usize = 0;
        while (total < buf.len) {
            const n = try self.read(buf[total..]);
            if (n == 0) break;
            total += n;
        }
        return total;
    }
};

// ── Zig 0.16 compat: std.fs.openDirAbsolute removed → libc-backed dir iterator ──
pub const max_path_bytes = 4096;

// The libc `struct dirent` layout is PLATFORM-SPECIFIC — Linux has no
// d_namlen and a 256-byte d_name; macOS/BSD have d_namlen and a 1024-byte
// d_name at a different offset. Declaring one hardcoded layout (the macOS one)
// made `entry.d_namlen` read garbage on Linux and slice d_name out of bounds.
// Match each target exactly; the name length is derived from the NUL
// terminator below, never from a field that may not exist.
const DirEnt = switch (builtin.os.tag) {
    .linux => extern struct {
        d_ino: u64,
        d_off: u64,
        d_reclen: u16,
        d_type: u8,
        d_name: [256]u8,
    },
    .macos, .ios, .tvos, .watchos, .freebsd, .openbsd, .netbsd, .dragonfly => extern struct {
        d_ino: u64,
        d_seekoff: u64,
        d_reclen: u16,
        d_namlen: u16,
        d_type: u8,
        d_name: [1024]u8,
    },
    else => @compileError("DirEnt: unsupported target OS — add its struct dirent layout"),
};
extern "c" fn opendir(path: [*:0]const u8) ?*anyopaque;
extern "c" fn readdir(dir: *anyopaque) ?*DirEnt;
extern "c" fn closedir(dir: *anyopaque) c_int;

pub const DirEntry = struct {
    name: []const u8,
    kind: Kind,
    pub const Kind = enum { file, directory, other };
};

pub const DirIterator = struct {
    handle: *anyopaque,

    pub fn next(self: *DirIterator) ?DirEntry {
        while (true) {
            const entry = readdir(self.handle) orelse return null;
            // d_name is always NUL-terminated; derive the length from that,
            // NOT from d_namlen (absent on Linux). Bounded by the array size,
            // so it can never read past the buffer.
            const name = std.mem.sliceTo(&entry.d_name, 0);
            // Skip . and ..
            if (std.mem.eql(u8, name, ".") or std.mem.eql(u8, name, "..")) continue;
            const kind: DirEntry.Kind = switch (entry.d_type) {
                4 => .directory, // DT_DIR
                8 => .file, // DT_REG
                else => .other,
            };
            return .{ .name = name, .kind = kind };
        }
    }

    pub fn close(self: *DirIterator) void {
        _ = closedir(self.handle);
    }
};

pub fn openDirAbsolute(path: []const u8) !DirIterator {
    var path_buf: [max_path_bytes]u8 = undefined;
    if (path.len >= path_buf.len) return error.PathTooLong;
    @memcpy(path_buf[0..path.len], path);
    path_buf[path.len] = 0;
    const handle = opendir(@ptrCast(&path_buf)) orelse return error.OpenDirFailed;
    return .{ .handle = handle };
}

// GIL management — PyEval_SaveThread/RestoreThread return/take PyThreadState*
// which Zig's @cImport can't translate. We declare them manually.
pub extern fn PyEval_SaveThread() ?*anyopaque;
pub extern fn PyEval_RestoreThread(state: ?*anyopaque) void;

// Per-worker thread state — cheaper than PyGILState_Ensure/Release on every call.
// Create one PyThreadState per OS thread at startup; reuse for every request.
pub extern fn PyEval_AcquireThread(tstate: ?*anyopaque) void;
pub extern fn PyEval_ReleaseThread(tstate: ?*anyopaque) void;
pub extern fn PyThreadState_New(interp: ?*anyopaque) ?*anyopaque;
pub extern fn PyThreadState_Clear(tstate: ?*anyopaque) void;
pub extern fn PyThreadState_DeleteCurrent() void;
pub extern fn PyInterpreterState_Get() ?*anyopaque;
// PyInterpreterState_Main reads the interpreter state from the runtime
// singleton. Unlike PyInterpreterState_Get, it does NOT require the
// calling OS thread to already have a PyThreadState attached — which
// is exactly the situation a freshly-`std.Thread.spawn`ed worker is in
// before it bootstraps its first tstate. (PyInterpreterState_Get aborts
// the process with "Fatal Python error: ... no active thread state" in
// that case — proven by the channels test on free-threaded 3.14t in
// CI run 24963756525, exit -6.) Available since CPython 3.13.
pub extern fn PyInterpreterState_Main() ?*anyopaque;

// Fast call API — avoids arg tuple/dict construction for simple cases.
pub extern fn PyObject_CallNoArgs(callable: *c.PyObject) ?*c.PyObject;
pub extern fn PyObject_Vectorcall(
    callable: *c.PyObject,
    args: [*]const ?*c.PyObject,
    nargsf: usize,
    kwnames: ?*c.PyObject,
) ?*c.PyObject;

// Tuple access — used to unpack (status, content_type, body) response tuples.
pub extern fn PyTuple_GetItem(op: *c.PyObject, i: c.Py_ssize_t) ?*c.PyObject;

// Pre-sized dict construction — avoids the resize-during-population penalty
// when we already know the final column count. Used by db_query_dicts to
// allocate an exactly-sized hash table up front. This is a private CPython
// API but has been stable since 3.6 and is widely used internally.
pub extern fn _PyDict_NewPresized(minused: c.Py_ssize_t) ?*c.PyObject;
