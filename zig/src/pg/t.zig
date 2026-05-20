//! pg-module test harness (reconstructed).
//!
//! The vendored pg.zig driver ships an extensive unit-test suite (conn.zig,
//! result.zig, reader.zig, pool.zig, types.zig, …) that all reference this `t`
//! helper. It had been reduced to a stub, so `zig build test` never compiled or
//! ran any of those ~110 tests. This file re-implements the harness so they run
//! again against a live PostgreSQL (env-driven) plus an in-memory mock stream
//! for the DB-free reader tests.
//!
//! Live-DB tests connect via env: PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD
//! (falling back to localhost / $USER / hyperdjango_test). `setup()` seeds the
//! fixture schema (all_types, simple_table, pool_test, dummy_enum) once, lazily,
//! before the first connection — so test order does not matter.

const std = @import("std");
const lib = @import("lib.zig");
const compat = @import("pg.zig");
const Conn = lib.Conn;

pub const allocator = std.testing.allocator;

// Zig 0.16 removed std.Thread.{Mutex,Condition,Semaphore}; re-export the pg
// module's pthread-backed shims so tests referencing t.Mutex/t.Condition work.
pub const Mutex = compat.Mutex;
pub const Condition = compat.Condition;
pub const sleep = compat.sleep;
pub const nanoTimestamp = compat.nanoTimestamp;

/// Shared scratch arena for tests that allocate row/array copies. Backed by the
/// page allocator (NOT the leak-checked testing allocator): it is a global that
/// is never deinit'd, so leak-checked backing would false-positive on any test
/// that fills it without calling `reset()`. Reset with `reset()` between tests.
pub var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);

pub fn reset() void {
    _ = arena.reset(.retain_capacity);
}

/// Skip a test that needs the upstream pgz CI database fixture — special roles
/// (pgz_user_clear/scram/nopass/ssl), a `postgres` superuser database, SSL certs,
/// or the postgis extension. A plain dev/CI PostgreSQL has none of these, so
/// these tests run only when PGZ_FIXTURE=1 signals that provisioned server.
pub fn requireFixture() !void {
    if (getenvZ("PGZ_FIXTURE") == null) return error.SkipZigTest;
}

// ── assertion wrappers (coerce literal `expected` to the actual's type) ──────

pub fn expectEqual(expected: anytype, actual: anytype) !void {
    try std.testing.expectEqual(@as(@TypeOf(actual), expected), actual);
}

pub fn expectString(expected: []const u8, actual: []const u8) !void {
    try std.testing.expectEqualStrings(expected, actual);
}

pub fn expectSlice(comptime T: type, expected: []const T, actual: []const T) !void {
    try std.testing.expectEqualSlices(T, expected, actual);
}

pub fn expectStringSlice(expected: []const []const u8, actual: []const []const u8) !void {
    try std.testing.expectEqual(expected.len, actual.len);
    for (expected, actual) |e, a| try std.testing.expectEqualStrings(e, a);
}

pub const expectError = std.testing.expectError;

pub fn expectDelta(expected: anytype, actual: anytype, delta: anytype) !void {
    const T = @TypeOf(actual, expected, delta);
    try std.testing.expectApproxEqAbs(@as(T, expected), @as(T, actual), @as(T, delta));
}

/// Fail a test, printing the connection's PG error context first (used in the
/// `insert into all_types` blocks where a schema mismatch would otherwise be an
/// opaque error code).
pub fn fail(c: anytype, err: anyerror) anyerror!void {
    if (c.err) |pg_err| {
        std.debug.print("\n[t.fail] PG error: {s} ({s})\n", .{ pg_err.message, pg_err.code });
    }
    return err;
}

// ── deterministic PRNG (reader fuzz test) ────────────────────────────────────

pub fn getRandom() std.Random.DefaultPrng {
    // Fixed seed: the reader fuzz test just needs varied-but-reproducible chunk
    // boundaries, not cryptographic randomness (and Date/random are unavailable).
    return std.Random.DefaultPrng.init(0xC0FFEE);
}

// ── in-memory mock stream (reader.zig, no socket) ────────────────────────────

pub const Stream = struct {
    // Never touched without a read timeout, which the reader tests do not set.
    socket: std.posix.socket_t = -1,
    buf: std.ArrayListUnmanaged(u8) = .empty,
    pos: usize = 0,

    /// Heap-allocated so callers hold a `*Stream` (matching `ReaderT(*t.Stream)`).
    pub fn init() *Stream {
        const s = allocator.create(Stream) catch unreachable;
        s.* = .{};
        return s;
    }

    pub fn deinit(self: *Stream) void {
        self.buf.deinit(allocator);
        allocator.destroy(self);
    }

    pub fn reset(self: *Stream) void {
        self.buf.clearRetainingCapacity();
        self.pos = 0;
    }

    /// Queue bytes for the reader to consume.
    pub fn add(self: *Stream, data: []const u8) void {
        self.buf.appendSlice(allocator, data) catch unreachable;
    }

    /// Drain queued bytes; 0 signals EOF, which the reader maps to error.Closed.
    pub fn read(self: *Stream, into: []u8) !usize {
        const remaining = self.buf.items[self.pos..];
        if (remaining.len == 0) return 0;
        const n = @min(into.len, remaining.len);
        @memcpy(into[0..n], remaining[0..n]);
        self.pos += n;
        return n;
    }

    pub fn writeAll(self: *Stream, data: []const u8) !void {
        _ = self;
        _ = data;
    }

    pub fn close(self: *Stream) void {
        _ = self;
    }
};

// ── live-DB helpers ──────────────────────────────────────────────────────────

// std.posix.getenv was removed in Zig 0.16; go through libc (we link libc).
fn getenvZ(key: [*:0]const u8) ?[]const u8 {
    const v = std.c.getenv(key) orelse return null;
    return std.mem.span(v);
}

fn envOr(key: [*:0]const u8, default: []const u8) []const u8 {
    return getenvZ(key) orelse default;
}

fn envPort() u16 {
    const s = getenvZ("PGPORT") orelse return 5432;
    return std.fmt.parseInt(u16, s, 10) catch 5432;
}

pub fn authOpts(base: Conn.AuthOpts) Conn.AuthOpts {
    var ao = base;
    ao.username = envOr("PGUSER", envOr("USER", "postgres"));
    ao.database = base.database orelse envOr("PGDATABASE", "hyperdjango_test");
    if (base.password == null) {
        if (getenvZ("PGPASSWORD")) |p| ao.password = p;
    }
    return ao;
}

/// Combined connect options: connection fields (host/port/tls/read_buffer) plus
/// per-call auth overrides (username/password/database). Tests pass a subset,
/// e.g. `.{}` or `.{ .tls = .require, .username = "u", .password = "p" }`.
pub const ConnectOpts = struct {
    host: ?[]const u8 = null,
    port: ?u16 = null,
    tls: Conn.Opts.TLS = .off,
    read_buffer: ?u16 = null,
    connect_timeout_ms: u32 = 0,
    username: ?[]const u8 = null,
    password: ?[]const u8 = null,
    database: ?[]const u8 = null,
};

fn openRaw(opts: Conn.Opts) !Conn {
    var o = opts;
    if (o.host == null) o.host = envOr("PGHOST", "localhost");
    if (o.port == null) o.port = envPort();
    return Conn.openAndAuth(allocator, o, authOpts(.{}));
}

pub fn connect(opts: ConnectOpts) Conn {
    ensureSetup();
    const o = Conn.Opts{
        .host = opts.host orelse envOr("PGHOST", "localhost"),
        .port = opts.port orelse envPort(),
        .tls = opts.tls,
        .read_buffer = opts.read_buffer,
        .connect_timeout_ms = opts.connect_timeout_ms,
    };
    var ao = authOpts(.{});
    if (opts.username) |u| ao.username = u;
    if (opts.password) |p| ao.password = p;
    if (opts.database) |d| ao.database = d;
    var conn = Conn.openAndAuth(allocator, o, ao) catch |err| {
        std.debug.print("\n[t.connect] could not reach PostgreSQL ({any}). " ++
            "Set PGHOST/PGPORT/PGUSER/PGDATABASE (defaults localhost/5432/$USER/hyperdjango_test).\n", .{err});
        unreachable;
    };
    // Pin the session timezone so TIMESTAMPTZ decode is deterministic regardless
    // of the host's local TZ — the fixture's expected µs values assume UTC.
    _ = conn.exec("SET TIME ZONE 'UTC'", .{}) catch {};
    return conn;
}

/// Run a single-column, single-row query and return column 0 as i32.
pub fn scalar(c: *Conn, sql: []const u8) i32 {
    var row = (c.row(sql, .{}) catch unreachable) orelse unreachable;
    defer row.deinit() catch {};
    return row.get(i32, 0) catch unreachable;
}

// ── fixture schema ───────────────────────────────────────────────────────────

var setup_done = false;
var setup_mutex: Mutex = .{};

const SCHEMA_SQL =
    \\ drop table if exists all_types;
    \\ drop table if exists simple_table;
    \\ drop table if exists pool_test;
    \\ drop type if exists dummy_enum;
    \\ create type dummy_enum as enum ('val1', 'val2');
    \\ create table simple_table (value text);
    \\ create table pool_test (id int not null);
    \\ create table all_types (
    \\   id int primary key,
    \\   col_int2 int2,               col_int2_arr int2[],
    \\   col_int4 int4,               col_int4_arr int4[],
    \\   col_int8 int8,               col_int8_arr int8[],
    \\   col_float4 float4,           col_float4_arr float4[],
    \\   col_float8 float8,           col_float8_arr float8[],
    \\   col_bool bool,               col_bool_arr bool[],
    \\   col_text text,               col_text_arr text[],
    \\   col_bytea bytea,             col_bytea_arr bytea[],
    \\   col_enum dummy_enum,         col_enum_arr dummy_enum[],
    \\   col_uuid uuid,               col_uuid_arr uuid[],
    \\   col_numeric numeric,         col_numeric_arr numeric[],
    \\   col_timestamp timestamp,     col_timestamp_arr timestamp[],
    \\   col_timestamptz timestamptz, col_timestamptz_arr timestamptz[],
    \\   col_json json,               col_json_arr json[],
    \\   col_jsonb jsonb,             col_jsonb_arr jsonb[],
    \\   col_char char(1),            col_char_arr char(1)[],
    \\   col_charn char(3),           col_charn_arr char(3)[],
    \\   col_cidr cidr,               col_cidr_arr cidr[],
    \\   col_inet inet,               col_inet_arr inet[],
    \\   col_macaddr macaddr,         col_macaddr_arr macaddr[],
    \\   col_macaddr8 macaddr8,       col_macaddr8_arr macaddr8[]
    \\ );
;

/// Create the fixture schema. Idempotent and thread-safe; runs once, lazily,
/// before the first `connect()` so test declaration order does not matter.
pub fn setup() !void {
    ensureSetup();
}

fn ensureSetup() void {
    setup_mutex.lock();
    defer setup_mutex.unlock();
    if (setup_done) return;
    setup_done = true;

    var c = openRaw(.{}) catch |err| {
        std.debug.print("\n[t.setup] could not reach PostgreSQL ({any}).\n", .{err});
        return;
    };
    defer c.deinit();
    _ = c.exec(SCHEMA_SQL, .{}) catch |err| {
        std.debug.print("\n[t.setup] schema creation failed: {any}", .{err});
        if (c.err) |pg_err| std.debug.print(" — {s}", .{pg_err.message});
        std.debug.print("\n", .{});
    };
}
