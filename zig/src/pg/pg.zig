const std = @import("std");
const lib = @import("lib.zig");

// ── Zig 0.16 compat (no std.time.nanoTimestamp/timestamp, no std.Thread.Mutex/Condition) ──
pub const Mutex = struct {
    inner: std.c.pthread_mutex_t = std.c.PTHREAD_MUTEX_INITIALIZER,
    pub fn lock(self: *Mutex) void {
        _ = std.c.pthread_mutex_lock(&self.inner);
    }
    pub fn unlock(self: *Mutex) void {
        _ = std.c.pthread_mutex_unlock(&self.inner);
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
    pub fn timedWait(self: *Condition, mutex: *Mutex, timeout_ns: u64) error{Timeout}!void {
        var ts: std.c.timespec = undefined;
        _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
        ts.sec += @intCast(timeout_ns / std.time.ns_per_s);
        ts.nsec += @intCast(timeout_ns % std.time.ns_per_s);
        if (ts.nsec >= std.time.ns_per_s) {
            ts.sec += 1;
            ts.nsec -= std.time.ns_per_s;
        }
        const rc = std.c.pthread_cond_timedwait(&self.inner, &mutex.inner, &ts);
        if (rc != .SUCCESS) return error.Timeout;
    }
};
pub fn nanoTimestamp() i128 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
    return @as(i128, ts.sec) * std.time.ns_per_s + ts.nsec;
}

pub fn sleep(ns: u64) void {
    var ts: std.c.timespec = .{
        .sec = @intCast(ns / std.time.ns_per_s),
        .nsec = @intCast(ns % std.time.ns_per_s),
    };
    _ = std.c.nanosleep(&ts, &ts);
}

pub fn timestamp() i64 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
    return @intCast(ts.sec);
}

pub const Row = lib.Row;
pub const Conn = lib.Conn;
pub const Pool = lib.Pool;
pub const Stmt = lib.Stmt;
pub const Result = lib.Result;
pub const Iterator = lib.Iterator;
pub const QueryRow = lib.QueryRow;
pub const Mapper = lib.Mapper;
pub const Binary = lib.Binary;

pub const Listener = @import("listener.zig").Listener;

pub const types = lib.types;
pub const Cidr = types.Cidr;
pub const Numeric = types.Numeric;
pub const Vector = types.Vector;
pub const Error = lib.proto.Error;
pub const printSSLError = lib.printSSLError;

pub fn uuidToHex(uuid: []const u8) ![36]u8 {
    return lib.types.UUID.toString(uuid);
}

pub fn writeMetrics(writer: anytype) !void {
    return @import("metrics.zig").write(writer);
}

const t = lib.testing;
test {
    try t.setup();
    std.testing.refAllDecls(@This());
}

test "pg: uuidToHex" {
    try t.expectError(error.InvalidUUID, uuidToHex(&.{ 73, 190, 142, 9, 170, 250, 176, 16, 73, 21 }));

    const s = try uuidToHex(&.{ 183, 204, 40, 47, 236, 67, 73, 190, 142, 9, 170, 250, 176, 16, 73, 21 });
    try t.expectString("b7cc282f-ec43-49be-8e09-aafab0104915", &s);
}
