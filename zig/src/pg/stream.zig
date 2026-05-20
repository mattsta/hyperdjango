const std = @import("std");
const lib = @import("lib.zig");

const openssl = lib.openssl;

const posix = std.posix;

const Conn = lib.Conn;
const Allocator = std.mem.Allocator;

const DEFAULT_HOST = "127.0.0.1";

pub const Stream = if (lib.has_openssl) TLSStream else PlainStream;

const TLSStream = struct {
    valid: bool,
    ssl: ?*openssl.SSL,
    socket: posix.socket_t,

    pub fn connect(allocator: Allocator, opts: Conn.Opts, ctx_: ?*openssl.SSL_CTX) !Stream {
        const plain = try PlainStream.connect(allocator, opts, null);
        errdefer plain.close();

        const socket = plain.socket;

        var ssl: ?*openssl.SSL = null;
        if (ctx_) |ctx| {
            // PostgreSQL TLS starts off as a plain connection which we upgrade
            try writeSocket(socket, &.{ 0, 0, 0, 8, 4, 210, 22, 47 });
            var buf = [1]u8{0};
            _ = try readSocket(socket, &buf);
            if (buf[0] != 'S') {
                return error.SSLNotSupportedByServer;
            }

            ssl = openssl.SSL_new(ctx) orelse return error.SSLNewFailed;
            errdefer openssl.SSL_free(ssl);

            if (opts.host) |host| {
                if (isHostName(host)) {
                    // don't send this for an ip address
                    var owned = false;
                    const h = opts._hostz orelse blk: {
                        owned = true;
                        break :blk try allocator.dupeZ(u8, host);
                    };

                    defer if (owned) {
                        allocator.free(h);
                    };

                    if (openssl.SSL_set_tlsext_host_name(ssl, h.ptr) != 1) {
                        return error.SSLHostNameFailed;
                    }
                }
                switch (opts.tls) {
                    .verify_full => openssl.SSL_set_verify(ssl, openssl.SSL_VERIFY_PEER, null),
                    else => {},
                }
            }

            if (openssl.SSL_set_fd(ssl, if (@import("builtin").os.tag == .windows) @intCast(@intFromPtr(socket)) else socket) != 1) {
                return error.SSLSetFdFailed;
            }

            {
                const ret = openssl.SSL_connect(ssl);
                if (ret != 1) {
                    const verification_code = openssl.SSL_get_verify_result(ssl);
                    if (comptime lib._stderr_tls) {
                        lib.printSSLError();
                    }
                    if (verification_code != openssl.X509_V_OK) {
                        if (comptime lib._stderr_tls) {
                            std.debug.print("ssl verification error: {s}\n", .{openssl.X509_verify_cert_error_string(verification_code)});
                        }
                        return error.SSLCertificationVerificationError;
                    }
                    return error.SSLConnectFailed;
                }
            }
        }

        return .{
            .ssl = ssl,
            .valid = true,
            .socket = socket,
        };
    }

    pub fn close(self: *Stream) void {
        if (self.ssl) |ssl| {
            if (self.valid) {
                _ = openssl.SSL_shutdown(ssl);
                self.valid = false;
            }
            openssl.SSL_free(ssl);
        }
        _ = std.posix.system.close(self.socket);
    }

    pub fn writeAll(self: *Stream, data: []const u8) !void {
        if (self.ssl) |ssl| {
            const result = openssl.SSL_write(ssl, data.ptr, @intCast(data.len));
            if (result <= 0) {
                self.valid = false;
                return error.SSLWriteFailed;
            }
            return;
        }
        return writeSocket(self.socket, data);
    }

    pub fn read(self: *Stream, buf: []u8) !usize {
        if (self.ssl) |ssl| {
            var read_len: usize = undefined;
            const result = openssl.SSL_read_ex(ssl, buf.ptr, @intCast(buf.len), &read_len);
            if (result <= 0) {
                self.valid = false;
                return error.SSLReadFailed;
            }
            return read_len;
        }

        return readSocket(self.socket, buf);
    }
};

const PlainStream = struct {
    socket: posix.socket_t,

    pub fn connect(allocator: Allocator, opts: Conn.Opts, _: anytype) !PlainStream {
        const socket = blk: {
            const host = opts.host orelse DEFAULT_HOST;
            if (host.len > 0 and host[0] == '/') {
                break :blk try connectUnix(host, opts.port orelse 5432);
            }
            const port = opts.port orelse 5432;
            if (opts.connect_timeout_ms > 0) {
                break :blk try tcpConnectWithTimeout(allocator, host, port, opts.connect_timeout_ms);
            }
            break :blk try tcpConnect(allocator, host, port);
        };
        errdefer _ = std.posix.system.close(socket);

        return .{
            .socket = socket,
        };
    }

    pub fn close(self: *const PlainStream) void {
        _ = std.posix.system.close(self.socket);
    }

    pub fn writeAll(self: *const PlainStream, data: []const u8) !void {
        return writeSocket(self.socket, data);
    }

    pub fn read(self: *const PlainStream, buf: []u8) !usize {
        return readSocket(self.socket, buf);
    }
};

// ── Zig 0.16 compat: libc-based TCP/Unix connect (std.net removed) ──
const libc_socket = @extern(*const fn (c_uint, c_uint, c_uint) callconv(.c) c_int, .{ .name = "socket" });
const libc_connect = @extern(*const fn (c_int, *const std.c.sockaddr, std.c.socklen_t) callconv(.c) c_int, .{ .name = "connect" });

// Direct errno access — std.c._errno() proved unreliable on Linux in
// Zig 0.16 (returned EFAULT=14 in CI even when getsockopt/connect set
// ECONNREFUSED=111). Bind to the platform's actual TLS errno function.
extern fn __errno_location() *c_int; // Linux/glibc
extern fn __error() *c_int; // macOS

inline fn currentErrno() c_int {
    return switch (@import("builtin").os.tag) {
        .macos, .ios, .tvos, .watchos => __error().*,
        else => __errno_location().*,
    };
}

// ── Specific connect errors (don't collapse different failure modes
//    into a single ConnectionRefused — that hides root causes) ──
//
// Errors are mapped from POSIX errno via mapConnectErrno below. The
// db.zig wrapper turns the variant name into a Python error message so
// users see (e.g.) "DnsResolveFailed" or "SocketCreateFailed" instead
// of a generic "ConnectionRefused" that sends them chasing the wrong
// hypothesis (as happened in CI — auth failures and FD exhaustion both
// surfaced as ConnectionRefused in the original code).
pub const ConnectError = error{
    HostTooLong,
    PortFormatFailed,
    DnsResolveFailed,
    SocketCreateFailed, // EMFILE / ENOMEM / EAFNOSUPPORT
    ConnectionRefused, // ECONNREFUSED specifically
    ConnectionTimedOut, // ETIMEDOUT or our connect_timeout_ms expired
    NetworkUnreachable, // ENETUNREACH
    HostUnreachable, // EHOSTUNREACH
    AddressInUse, // EADDRINUSE
    AddressNotAvailable, // EADDRNOTAVAIL — ephemeral port exhaustion (TIME_WAIT churn)
    PermissionDenied, // EACCES / EPERM
    PathTooLong, // Unix socket sun_path overflow
    UnixSocketNotFound, // ENOENT
    UnixSocketBadType, // ENOTSOCK
    UnknownConnectError, // catch-all (the actual errno is logged)
};

// errno values — POSIX values are stable across Linux/macOS for these.
const E = struct {
    const PERM = 1; // EPERM
    const NOENT = 2; // ENOENT
    const ACCES = 13; // EACCES
    const EXIST = 17; // EEXIST
    const NOTSOCK = 38; // ENOTSOCK (Linux); macOS uses 38 too
    const ADDRINUSE_LINUX = 98;
    const ADDRINUSE_MACOS = 48;
    const ADDRNOTAVAIL_LINUX = 99;
    const ADDRNOTAVAIL_MACOS = 49;
    const NETUNREACH_LINUX = 101;
    const NETUNREACH_MACOS = 51;
    const CONNREFUSED_LINUX = 111;
    const CONNREFUSED_MACOS = 61;
    const HOSTUNREACH_LINUX = 113;
    const HOSTUNREACH_MACOS = 65;
    const TIMEDOUT_LINUX = 110;
    const TIMEDOUT_MACOS = 60;
    const MFILE = 24; // EMFILE
    const NFILE = 23; // ENFILE
    const NOMEM = 12; // ENOMEM
    const AFNOSUPPORT_LINUX = 97;
    const AFNOSUPPORT_MACOS = 47;
};

fn mapConnectErrno(errno: c_int) ConnectError {
    return switch (errno) {
        E.CONNREFUSED_LINUX, E.CONNREFUSED_MACOS => error.ConnectionRefused,
        E.TIMEDOUT_LINUX, E.TIMEDOUT_MACOS => error.ConnectionTimedOut,
        E.NETUNREACH_LINUX, E.NETUNREACH_MACOS => error.NetworkUnreachable,
        E.HOSTUNREACH_LINUX, E.HOSTUNREACH_MACOS => error.HostUnreachable,
        E.ADDRINUSE_LINUX, E.ADDRINUSE_MACOS => error.AddressInUse,
        E.ADDRNOTAVAIL_LINUX, E.ADDRNOTAVAIL_MACOS => error.AddressNotAvailable,
        E.ACCES, E.PERM => error.PermissionDenied,
        E.NOENT => error.UnixSocketNotFound,
        E.NOTSOCK => error.UnixSocketBadType,
        E.MFILE, E.NFILE, E.NOMEM, E.AFNOSUPPORT_LINUX, E.AFNOSUPPORT_MACOS => error.SocketCreateFailed,
        else => {
            // Self-reporting: an unmapped errno must never vanish into an
            // opaque "UnknownConnectError". Surface the raw number so the
            // next occurrence is diagnosable (and can be given its own case).
            std.debug.print("[HYPER] connect: unmapped errno={d} → UnknownConnectError\n", .{errno});
            return error.UnknownConnectError;
        },
    };
}

// addrinfo field order is NOT portable: glibc/Linux orders ai_addr BEFORE
// ai_canonname; macOS/BSD orders ai_canonname BEFORE ai_addr. Getting this
// wrong means connect() reads a junk pointer and returns EFAULT — which is
// exactly the bug CI surfaced (every test failed with errno=14 on Linux
// with the macOS-shaped struct in place).
const addrinfo = if (@import("builtin").os.tag == .linux) extern struct {
    flags: c_int = 0,
    family: c_int = 0,
    socktype: c_int = 0,
    protocol: c_int = 0,
    addrlen: std.c.socklen_t = 0,
    addr: ?*std.c.sockaddr = null,
    canonname: ?[*:0]u8 = null,
    next: ?*addrinfo = null,
} else extern struct {
    flags: c_int = 0,
    family: c_int = 0,
    socktype: c_int = 0,
    protocol: c_int = 0,
    addrlen: std.c.socklen_t = 0,
    canonname: ?[*:0]u8 = null,
    addr: ?*std.c.sockaddr = null,
    next: ?*addrinfo = null,
};
extern "c" fn getaddrinfo(node: [*:0]const u8, service: [*:0]const u8, hints: ?*const addrinfo, res: *?*addrinfo) c_int;
extern "c" fn freeaddrinfo(res: *addrinfo) void;

fn tcpConnect(allocator: Allocator, host: []const u8, port: u16) !posix.socket_t {
    return tcpConnectWithTimeout(allocator, host, port, 10000);
}

extern "c" fn fcntl(fd: c_int, cmd: c_int, ...) c_int;
extern "c" fn getsockopt(fd: c_int, level: c_int, optname: c_int, optval: *anyopaque, optlen: *std.c.socklen_t) c_int;

fn tcpConnectWithTimeout(_: Allocator, host: []const u8, port: u16, timeout_ms: u32) !posix.socket_t {
    var host_buf: [256]u8 = undefined;
    if (host.len >= host_buf.len) return error.HostTooLong;
    @memcpy(host_buf[0..host.len], host);
    host_buf[host.len] = 0;

    var port_buf: [8]u8 = undefined;
    const port_str = std.fmt.bufPrint(&port_buf, "{d}", .{port}) catch return error.PortFormatFailed;
    port_buf[port_str.len] = 0;

    var hints: addrinfo = .{};
    hints.family = std.c.AF.INET;
    hints.socktype = std.c.SOCK.STREAM;

    var result: ?*addrinfo = null;
    if (getaddrinfo(@ptrCast(&host_buf), @ptrCast(&port_buf), &hints, &result) != 0) return error.DnsResolveFailed;
    if (result == null) return error.DnsResolveFailed;
    defer freeaddrinfo(result.?);

    // Track the most informative error across address attempts. socket()
    // failures (EMFILE etc.) take priority over per-address connect()
    // failures because they're an environmental issue, not a server issue.
    var last_socket_errno: c_int = 0;
    var last_connect_errno: c_int = 0;

    var it: ?*addrinfo = result;
    while (it) |ai| {
        const sockfd = libc_socket(@intCast(ai.family), @intCast(ai.socktype), @intCast(ai.protocol));
        if (sockfd < 0) {
            last_socket_errno = currentErrno();
            it = ai.next;
            continue;
        }

        // Set non-blocking for connect with timeout. O_NONBLOCK is NOT
        // portable: macOS/BSD uses 0o0004 (= 0x004); Linux uses 0o4000
        // (= 0x800). Hardcoding 0x004 silently leaves the Linux socket
        // BLOCKING, so the subsequent connect() to an unreachable host
        // sits in the kernel for ~127s of SYN backoff before returning
        // ETIMEDOUT — the exact 134s symptom in the connection_timeout
        // test, with poll() never running. Trace evidence:
        //   [HYPER trace] tcpConnect iter=1 connect() immediate errno=110
        //   [HYPER trace] tcpConnect loop done: iters=1 total_elapsed=136322ms requested_timeout=1000ms
        const O_NONBLOCK: c_int = if (@import("builtin").os.tag == .linux) 0o4000 else 0o0004;
        const flags = fcntl(sockfd, 3, @as(c_int, 0)); // F_GETFL = 3
        _ = fcntl(sockfd, 4, flags | O_NONBLOCK); // F_SETFL = 4

        const rc = libc_connect(sockfd, ai.addr.?, ai.addrlen);
        if (rc == 0) {
            // Connected immediately — restore blocking mode
            _ = fcntl(sockfd, 4, flags); // F_SETFL = 4
            return sockfd;
        }

        // Check if EINPROGRESS (note: macOS = 36, Linux = 115)
        const err = currentErrno();
        const EINPROGRESS_LINUX: c_int = 115;
        const EINPROGRESS_MACOS: c_int = 36;
        if (err != EINPROGRESS_LINUX and err != EINPROGRESS_MACOS) {
            last_connect_errno = err;
            _ = std.posix.system.close(sockfd);
            it = ai.next;
            continue;
        }

        // Poll for writability with timeout
        var pollfds = [1]std.posix.pollfd{.{
            .fd = sockfd,
            .events = std.posix.POLL.OUT,
            .revents = 0,
        }};

        // posix-safe: poll's only `unreachable` errnos are EFAULT/EINVAL; the
        // pollfds are a stack array (no EFAULT) and nfds=1 (no EINVAL). A dead
        // peer surfaces as POLLHUP/POLLERR/POLLNVAL in revents, never an errno.
        const poll_result = std.posix.poll(&pollfds, @intCast(timeout_ms)) catch {
            _ = std.posix.system.close(sockfd);
            return error.ConnectionTimedOut;
        };

        if (poll_result == 0) {
            // Timeout
            _ = std.posix.system.close(sockfd);
            return error.ConnectionTimedOut;
        }

        // POLLHUP / POLLERR / POLLNVAL in revents = the connect() failed
        // asynchronously; the kernel signals this independently of SO_ERROR.
        // (Pre-existing bug: we only checked SO_ERROR, missed POLLHUP, and
        // returned a dead fd up to auth where setsockopt blew up with EINVAL.
        // Proven by tracing connect to a closed port: poll returned POLLHUP
        // 0x10, never POLLOUT 0x4, and getsockopt failed with rc=-1.)
        const HUP: i16 = 0x010;
        const ERR_REV: i16 = 0x008;
        const NVAL: i16 = 0x020;
        // SOL_SOCKET is platform-dependent: Linux=1, macOS/BSD=0xffff.
        // Hardcoding 1 makes getsockopt() fail with EINVAL on macOS, which
        // surfaced as UnknownConnectError on every connect after the
        // gso_rc check landed.
        const SOL_SOCKET_C: c_int = if (@import("builtin").os.tag == .linux) 1 else 0xffff;
        const SO_ERROR_C: c_int = 4;
        if (pollfds[0].revents & (HUP | ERR_REV | NVAL) != 0) {
            // Pull the actual errno via SO_ERROR so we can map it; if
            // getsockopt fails too, fall back to ConnectionRefused (the
            // most common cause of POLLHUP on a freshly-connecting socket).
            var hup_err: c_int = 0;
            var hup_len: std.c.socklen_t = @sizeOf(c_int);
            if (getsockopt(sockfd, SOL_SOCKET_C, SO_ERROR_C, &hup_err, &hup_len) == 0 and hup_err != 0) {
                last_connect_errno = hup_err;
            } else {
                last_connect_errno = E.CONNREFUSED_LINUX; // mapped to ConnectionRefused
            }
            _ = std.posix.system.close(sockfd);
            it = ai.next;
            continue;
        }

        // Check SO_ERROR. CRITICAL: check getsockopt's return code too —
        // pre-existing bug discarded it via `_ = getsockopt(...)`, so a
        // failed getsockopt left so_err=0 and the dead socket leaked through.
        var so_err: c_int = 0;
        var so_len: std.c.socklen_t = @sizeOf(c_int);
        const gso_rc = getsockopt(sockfd, SOL_SOCKET_C, SO_ERROR_C, &so_err, &so_len);
        if (gso_rc != 0) {
            // getsockopt itself failed — treat as a connect failure rather
            // than assuming connect succeeded.
            last_connect_errno = currentErrno();
            _ = std.posix.system.close(sockfd);
            it = ai.next;
            continue;
        }
        if (so_err != 0) {
            last_connect_errno = so_err;
            _ = std.posix.system.close(sockfd);
            it = ai.next;
            continue;
        }

        // Connected — restore blocking mode
        _ = fcntl(sockfd, 4, flags);
        return sockfd;
    }

    // Exhausted all addrinfo entries. socket() failure (env-level) wins
    // over connect() failure (server-level) because it's the more
    // actionable diagnostic.
    if (last_socket_errno != 0) return mapConnectErrno(last_socket_errno);
    if (last_connect_errno != 0) return mapConnectErrno(last_connect_errno);
    return error.UnknownConnectError;
}

fn connectUnix(path: []const u8, port: u16) !posix.socket_t {
    _ = port;
    const sockfd = libc_socket(std.c.AF.UNIX, std.c.SOCK.STREAM, 0);
    if (sockfd < 0) return mapConnectErrno(currentErrno());
    errdefer _ = std.posix.system.close(sockfd);

    // Build Unix socket path: /path/.s.PGSQL.5432
    var addr = std.mem.zeroes(std.c.sockaddr.un);
    addr.family = std.c.AF.UNIX;
    if (path.len >= addr.path.len) return error.PathTooLong;
    @memcpy(addr.path[0..path.len], path);

    if (libc_connect(sockfd, @ptrCast(&addr), @sizeOf(std.c.sockaddr.un)) != 0) {
        return mapConnectErrno(currentErrno());
    }
    return sockfd;
}

fn readSocket(socket: posix.socket_t, buf: []u8) !usize {
    const rc = std.c.read(socket, buf.ptr, buf.len);
    if (rc < 0) return error.ReadFailed;
    if (rc == 0) return error.EndOfStream;
    return @intCast(rc);
}

fn writeSocket(socket: posix.socket_t, data: []const u8) !void {
    var written: usize = 0;
    while (written < data.len) {
        const rc = std.c.write(socket, data[written..].ptr, data[written..].len);
        if (rc < 0) return error.WriteFailed;
        written += @as(usize, @intCast(rc));
    }
}

fn isHostName(host: []const u8) bool {
    if (std.mem.indexOfScalar(u8, host, ':') != null) {
        // IPv6
        return false;
    }
    return std.mem.indexOfNone(u8, host, "0123456789.") != null;
}
