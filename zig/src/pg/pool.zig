const std = @import("std");
const lib = @import("lib.zig");

const log = lib.log;
const Conn = lib.Conn;
const Result = lib.Result;
const SSLCtx = lib.SSLCtx;
const QueryRow = lib.QueryRow;
const QueryRowUnsafe = lib.QueryRowUnsafe;
const Listener = @import("listener.zig").Listener;

const pg_compat = @import("pg.zig");
const Allocator = std.mem.Allocator;

pub const Pool = struct {
    _opts: Opts,
    _timeout: u64,
    _conns: []*Conn,
    _available: usize,
    _missing: usize,
    _allocator: Allocator,
    _mutex: pg_compat.Mutex,
    _cond: pg_compat.Condition,
    _ssl_ctx: ?*lib.SSLCtx,
    _reconnector: Reconnector,
    _arena: std.heap.ArenaAllocator,
    // Lifetime reference count. Starts at 1 for the pool's owner (the handle
    // registry, or the direct creator in embedded use). Every successful
    // acquire() adds one reference that the matching release() drops, and any
    // code that resolves a shared *Pool from a registry must retain() under
    // that registry's lock before dereferencing. deinit() runs when the LAST
    // reference drops — so closing a pool while other threads still hold
    // checked-out connections or are blocked inside acquire() defers the
    // teardown instead of freeing memory under them (use-after-free SIGSEGV).
    _refs: std.atomic.Value(usize) = .init(1),
    // Set once when the owner starts closing the pool. Threads holding an
    // idle checked-out connection (thread-owned slots) poll this on their
    // next use and release the connection themselves — their checkout
    // reference guarantees this memory is still valid to read.
    _closing: std.atomic.Value(bool) = .init(false),
    // Contention instrumentation (task #193). All reads/writes happen
    // under _mutex, so no atomics are needed. These are cumulative
    // counters reset only on pool init; callers compute deltas.
    _waiters: usize = 0, // number of threads currently blocked in acquire's timedWait
    _max_waiters: usize = 0, // peak _waiters observed since init
    _wait_count: u64 = 0, // cumulative acquires that had to wait at least once
    _wait_total_ns: u64 = 0, // cumulative nanoseconds spent waiting
    _wait_max_ns: u64 = 0, // single longest wait observed
    _acquire_count: u64 = 0, // total successful acquires
    _timeout_count: u64 = 0, // acquires that returned error.Timeout

    pub const Opts = struct {
        size: u16 = 10,
        auth: Conn.AuthOpts = .{},
        connect: Conn.Opts = .{},
        timeout: u32 = 10 * std.time.ms_per_s,
        connect_on_init_count: ?u16 = null,
        max_queries_per_conn: u64 = 0, // 0 = unlimited
        max_conn_lifetime: i64 = 0, // 0 = unlimited (seconds)
    };

    pub const Stats = struct {
        size: usize,
        available: usize,
        missing: usize,
        in_use: usize,
        // Contention instrumentation (task #193)
        waiters: usize,
        max_waiters: usize,
        wait_count: u64,
        wait_total_ns: u64,
        wait_max_ns: u64,
        acquire_count: u64,
        timeout_count: u64,
    };

    pub fn initUri(allocator: Allocator, uri: std.Uri, opts: Opts) !*Pool {
        var po = try lib.parseOpts(uri, allocator);
        defer po.deinit();
        po.opts.size = opts.size;
        po.opts.timeout = opts.timeout;
        return Pool.init(allocator, po.opts);
    }

    pub fn init(allocator: Allocator, opts: Opts) !*Pool {
        var arena = std.heap.ArenaAllocator.init(allocator);
        const aa = arena.allocator();
        errdefer arena.deinit();

        const pool = try aa.create(Pool);
        const size = opts.size;
        const conns = try aa.alloc(*Conn, size);

        var opts_copy = opts;
        var ssl_ctx: ?*SSLCtx = null;
        if (comptime lib.has_openssl) {
            switch (opts.connect.tls) {
                .off => {},
                else => |tls_config| {
                    if (opts.connect.host) |h| {
                        opts_copy.connect._hostz = try aa.dupeZ(u8, h);
                    }
                    ssl_ctx = try lib.initializeSSLContext(tls_config);
                },
            }
        }
        errdefer lib.freeSSLContext(ssl_ctx);
        const connect_on_init_count = opts.connect_on_init_count orelse size;

        pool.* = .{
            ._cond = .{},
            ._mutex = .{},
            ._conns = conns,
            ._arena = arena,
            ._opts = opts_copy,
            ._ssl_ctx = ssl_ctx,
            ._missing = 0,
            ._allocator = allocator,
            ._available = connect_on_init_count,
            ._reconnector = Reconnector.init(pool),
            ._timeout = @as(u64, @intCast(opts.timeout)) * std.time.ns_per_ms,
        };

        var opened_connections: usize = 0;
        errdefer {
            // Stop the reconnector first: if a lazy-start reconnect() spawned the
            // background thread, it holds a *Pool that arena.deinit() is about to
            // free (later reconnect → UAF). stop() joins it (no-op if never
            // spawned). Then mirror deinit(): each Conn is heap-allocated via
            // newConnection(pool._allocator), so it must be destroy()'d, not just
            // deinit()'d, or the Conn struct leaks.
            pool._reconnector.stop();
            for (0..opened_connections) |i| {
                conns[i].deinit();
                allocator.destroy(conns[i]);
            }
        }

        for (0..connect_on_init_count) |i| {
            conns[i] = try newConnection(pool, true);
            conns[i]._in_pool.store(true, .release); // resident in the free-list
            opened_connections += 1;
        }

        const lazy_start_count = size - connect_on_init_count;
        pool._missing = lazy_start_count;
        for (0..lazy_start_count) |_| {
            try pool._reconnector.reconnect();
        }

        return pool;
    }

    pub fn deinit(self: *Pool) void {
        self._reconnector.stop();
        const allocator = self._allocator;
        // Destroy ONLY the connections resident in the free-list —
        // conns[0..available]. `acquire` advances the stack pointer WITHOUT
        // clearing the vacated slot, so a checked-out connection's pointer
        // lingers at its old index. If it was leaked (never returned — e.g. a
        // cancelled pinned transaction) that stale copy duplicates a connection
        // a later return shifted down into the live range, and freeing every
        // slot would double-free it ("double free or corruption"). The live
        // range holds each resident connection exactly once (`_in_pool` keeps a
        // duplicate from entering it; unfilled lazy-init slots sit beyond
        // `available` holding undefined pointers). Checked-out connections are
        // their holder's responsibility — leaked here rather than double-freed.
        // When teardown runs via unref() the point is moot: every checkout
        // holds a pool reference, so the last-reference deinit runs only once
        // every connection has come home.
        for (self._conns[0..self._available]) |conn| {
            conn.deinit();
            allocator.destroy(conn);
        }
        lib.freeSSLContext(self._ssl_ctx);
        self._arena.deinit();
    }

    /// Mark the pool as closing. Purely advisory for holders of checked-out
    /// connections (lazy release); acquire/release themselves stay valid until
    /// the last reference drops.
    pub fn markClosing(self: *Pool) void {
        self._closing.store(true, .release);
    }

    pub fn isClosing(self: *const Pool) bool {
        return self._closing.load(.acquire);
    }

    /// Take a strong reference. The caller must either already hold a
    /// reference, or hold the lock of the registry the pool is published in
    /// (so the owner's reference provably still exists).
    pub fn retain(self: *Pool) void {
        const prev = self._refs.fetchAdd(1, .acq_rel);
        std.debug.assert(prev > 0);
    }

    /// Drop a reference; the last one tears the pool down. Callers must not
    /// touch the pool after this returns. Safe from any thread EXCEPT the
    /// reconnector's own (deinit joins that thread — see releaseInsert, which
    /// is why the reconnector's refill path never drops a reference).
    pub fn unref(self: *Pool) void {
        const prev = self._refs.fetchSub(1, .acq_rel);
        std.debug.assert(prev > 0);
        if (prev == 1) self.deinit();
    }

    pub fn acquire(self: *Pool) !*Conn {
        const conns = self._conns;
        const deadline = @import("pg.zig").nanoTimestamp() + @as(i64, @intCast(self._timeout));

        self._mutex.lock();
        errdefer self._mutex.unlock();

        // Track whether this acquire had to wait at all — used to bump
        // _wait_count exactly once per acquire, even if timedWait returns
        // spuriously and we re-enter the loop.
        var waited: bool = false;
        var wait_start_ns: i128 = 0;

        while (true) {
            const available = self._available;
            const missing = self._missing;

            if (available == 0) {
                // Check if pool is completely exhausted
                const total_alive = self._conns.len - missing;
                if (total_alive == 0) {
                    if (waited) {
                        self._waiters -= 1;
                    }
                    return error.PoolExhausted;
                }

                lib.metrics.poolEmpty();

                // Calculate remaining timeout
                const now = @import("pg.zig").nanoTimestamp();
                if (now >= deadline) {
                    if (waited) {
                        self._waiters -= 1;
                        const elapsed: u64 = @intCast(now - wait_start_ns);
                        self._wait_total_ns += elapsed;
                        if (elapsed > self._wait_max_ns) self._wait_max_ns = elapsed;
                    }
                    self._timeout_count += 1;
                    return error.Timeout;
                }
                const remaining_ns: u64 = @intCast(deadline - now);

                if (!waited) {
                    waited = true;
                    wait_start_ns = now;
                    self._waiters += 1;
                    if (self._waiters > self._max_waiters) {
                        self._max_waiters = self._waiters;
                    }
                    self._wait_count += 1;
                }

                try self._cond.timedWait(&self._mutex, remaining_ns);
                continue;
            }

            if (waited) {
                self._waiters -= 1;
                const elapsed: u64 = @intCast(@import("pg.zig").nanoTimestamp() - wait_start_ns);
                self._wait_total_ns += elapsed;
                if (elapsed > self._wait_max_ns) self._wait_max_ns = elapsed;
            }

            const index = available - 1;
            const conn = conns[index];
            self._available = index;
            self._acquire_count += 1;
            conn._in_pool.store(false, .release); // handed out — no longer resident
            // The checkout itself holds a pool reference: the pool stays alive
            // until this connection comes back, even if the owner closes the
            // pool while the connection is out working on another thread.
            self.retain();
            self._mutex.unlock();
            return conn;
        }
    }

    pub fn release(self: *Pool, conn: *Conn) void {
        // Structural release-at-most-once. Atomically claim the connection for
        // return: if it is ALREADY resident in the free-list, this is a
        // duplicate release (two cleanup paths raced to return the same pinned
        // connection). Drop it — the connection is already owned by the pool, so
        // returning it again would place its pointer in a second free-list slot
        // and double-free it at teardown. This enforces the invariant the
        // stack free-list depends on, instead of trusting every caller to
        // release exactly once across the async-cancellation + thread-offload
        // boundary (which is where it kept going wrong).
        //
        // Reference accounting: exactly one release per checkout drops the
        // checkout's pool reference (taken in acquire). The duplicate-release
        // exits below are ref-neutral — their checkout's reference was already
        // dropped by the release that won.
        if (conn._in_pool.swap(true, .acq_rel)) return;

        var conn_to_add = conn;
        var needs_replace = conn._state != .idle;

        // A closing pool's residents only get destroyed at deinit — never
        // replace or session-reset for it (spawning a fresh PG connection for
        // a pool that is being torn down is pure teardown churn). Parking the
        // connection as-is, even dirty, is safe: nothing reuses it.
        const closing = self.isClosing();

        // Check if connection should be rotated (query count or age)
        if (closing) {
            needs_replace = false;
        } else if (!needs_replace) {
            const opts = self._opts;
            if (opts.max_queries_per_conn > 0 and conn.queryCount() >= opts.max_queries_per_conn) {
                needs_replace = true;
            } else if (opts.max_conn_lifetime > 0 and conn.age() >= opts.max_conn_lifetime) {
                needs_replace = true;
            }
        }

        // Session reset: clear SET variables, cursors, and LISTEN
        // subscriptions before returning the connection to the pool.
        // This prevents cross-request state pollution (e.g., one
        // handler's SET search_path leaking to the next handler that
        // gets the same connection). Does NOT touch prepared
        // statements — those are managed by the LRU stmt cache.
        //
        // If the reset fails, the connection is dirty → replace it.
        //
        // NOTE: session reset is skipped for thread-owned connections
        // (the common case for the Zig HTTP server) because thread-
        // owned slots never return to the pool — they're pinned for
        // the lifetime of the worker thread. Reset only fires on the
        // explicit pool acquire/release path used by Django integration,
        // manual `conn_acquire()`/`conn_release()`, and pool rotation.
        // Session reset: clear SET variables, cursors, and LISTEN
        // subscriptions before returning the connection to the pool.
        // Prevents cross-request state pollution. Pointless for a closing
        // pool (nothing reuses the parked connection).
        if (!closing and !needs_replace) {
            conn.resetSession() catch {
                needs_replace = true;
            };
        }

        if (needs_replace) {
            if (conn._state != .idle) {
                lib.metrics.poolDirty();
            }
            conn.deinit();
            self._allocator.destroy(conn);

            conn_to_add = newConnection(self, true) catch {
                self._mutex.lock();
                self._missing += 1;
                self._mutex.unlock();

                self._reconnector.reconnect() catch {};
                // The checkout still ended (its conn was destroyed above) —
                // drop its pool reference. Last touch of self.
                self.unref();
                return;
            };
        }

        var conns = self._conns;
        self._mutex.lock();
        const available = self._available;
        if (available >= conns.len) {
            // Over-release: the pool already holds its full capacity of idle
            // connections, so this conn is being returned more times than it
            // was acquired. Writing conns[available] would be an out-of-bounds
            // store that smashes the adjacent heap chunk (a "double free or
            // corruption" abort on a later, unrelated free — and only under
            // ReleaseFast, where this index is unchecked). Refuse the write.
            // A fresh replacement conn is ours to destroy; an original conn is
            // already parked in the pool, so a duplicate release is a no-op we
            // must NOT destroy (that would dangle the copy still in `conns`).
            self._mutex.unlock();
            lib.metrics.poolDirty();
            if (needs_replace) {
                conn_to_add.deinit();
                self._allocator.destroy(conn_to_add);
            }
            return;
        }
        // The connection now becomes resident. For a rotated connection this is
        // a fresh replacement whose flag is still false; for a normal return the
        // swap above already set it. Store unconditionally so `conn_to_add`
        // (whichever it is) reflects the free-list invariant.
        conn_to_add._in_pool.store(true, .release);
        conns[available] = conn_to_add;
        self._available = available + 1;
        self._mutex.unlock();
        self._cond.signal();
        // Drop the checkout's pool reference LAST: if the pool was closed
        // while this connection was out, this is what finally runs deinit —
        // after the conn is parked and nothing here touches self again. The
        // signal above is safe: we still held this reference through it.
        self.unref();
    }

    /// Park a connection that was created OUTSIDE any checkout (the
    /// reconnector's refill). Identical residency handling to release(), but
    /// ends no checkout, so it drops no pool reference — which also guarantees
    /// the reconnector thread can never be the one that triggers deinit
    /// (deinit joins the reconnector thread; tearing down from it would
    /// self-join). A fresh connection needs no rotation or session reset.
    fn releaseInsert(self: *Pool, conn: *Conn) void {
        const conns = self._conns;
        self._mutex.lock();
        const available = self._available;
        if (available >= conns.len) {
            // Pool already at capacity (a racing duplicate release overfilled
            // it) — this fresh connection has no slot; destroy it.
            self._mutex.unlock();
            conn.deinit();
            self._allocator.destroy(conn);
            return;
        }
        conn._in_pool.store(true, .release);
        conns[available] = conn;
        self._available = available + 1;
        self._mutex.unlock();
        self._cond.signal();
    }

    pub fn newListener(self: *Pool) !Listener {
        var listener = try Listener.open(self._allocator, self._opts.connect);
        try listener.auth(self._opts.auth);
        return listener;
    }

    pub fn stats(self: *Pool) Stats {
        self._mutex.lock();
        defer self._mutex.unlock();

        const available = self._available;
        const missing = self._missing;
        const size = self._conns.len;

        return .{
            .size = size,
            .available = available,
            .missing = missing,
            .in_use = size - available - missing,
            .waiters = self._waiters,
            .max_waiters = self._max_waiters,
            .wait_count = self._wait_count,
            .wait_total_ns = self._wait_total_ns,
            .wait_max_ns = self._wait_max_ns,
            .acquire_count = self._acquire_count,
            .timeout_count = self._timeout_count,
        };
    }

    pub fn exec(self: *Pool, sql: []const u8, values: anytype) !?i64 {
        return self.execOpts(sql, values, .{});
    }

    pub fn execOpts(self: *Pool, sql: []const u8, values: anytype, opts: Conn.QueryOpts) !?i64 {
        var conn = try self.acquire();
        defer self.release(conn);
        return conn.execOpts(sql, values, opts);
    }

    pub fn query(self: *Pool, sql: []const u8, values: anytype) !*Result {
        return self.queryOpts(sql, values, .{});
    }

    pub fn queryOpts(self: *Pool, sql: []const u8, values: anytype, opts_: Conn.QueryOpts) !*Result {
        var opts = opts_;
        opts.release_conn = true;
        var conn = try self.acquire();
        // F4: NO `errdefer self.release(conn)` here. With release_conn=true,
        // conn.queryOpts owns the release on BOTH exits — it releases on every
        // error path (F5), and on success the returned Result releases via
        // result.deinit(). An errdefer here would push the same conn to the
        // free list a SECOND time (double-release corruption).
        return conn.queryOpts(sql, values, opts);
    }

    pub fn row(self: *Pool, sql: []const u8, values: anytype) !?QueryRow {
        return self.rowOpts(sql, values, .{});
    }

    pub fn rowUnsafe(self: *Pool, sql: []const u8, values: anytype) !?QueryRowUnsafe {
        return self.rowUnsafeOpts(sql, values, .{});
    }

    pub fn rowOpts(self: *Pool, sql: []const u8, values: anytype, opts_: Conn.QueryOpts) !?QueryRow {
        var opts = opts_;
        opts.release_conn = true;
        var conn = try self.acquire();
        return conn.rowOpts(sql, values, opts);
    }

    pub fn rowUnsafeOpts(self: *Pool, sql: []const u8, values: anytype, opts_: Conn.QueryOpts) !?QueryRowUnsafe {
        var opts = opts_;
        opts.release_conn = true;
        var conn = try self.acquire();
        return conn.rowUnsafeOpts(sql, values, opts);
    }
};

const Reconnector = struct {
    // number of connections that the pool is missing, i.e. how many need to be
    // reconnected
    count: usize,

    // when stop is called, this is set to true
    stopped: bool,

    pool: *Pool,
    mutex: pg_compat.Mutex,

    // the thread, if any, that the monitor is running in
    thread: ?std.Thread,

    fn init(pool: *Pool) Reconnector {
        return .{
            .pool = pool,
            .count = 0,
            .mutex = .{},
            .stopped = false,
            .thread = null,
        };
    }

    fn run(self: *Reconnector) void {
        const pool = self.pool;
        const retry_delay = 2 * std.time.ns_per_s;

        // F6: NO `defer self.mutex.unlock()`. Each iteration unlocks the mutex
        // before the `stopped` check and re-locks only on the paths that loop
        // back (transient-connect retry, successful reconnect). The stopped
        // early-return exits with the mutex already UNLOCKED — a deferred
        // unlock would then run on an unowned mutex (double-unlock UB, racing
        // Pool.deinit -> stop). So every exit path unlocks explicitly, exactly
        // once, matching the lock state it is actually in.
        self.mutex.lock();
        loop: while (self.count > 0) {
            const stopped = self.stopped;
            self.mutex.unlock();
            if (stopped == true) {
                // mutex already unlocked above — nothing to release.
                return;
            }

            const conn = newConnection(pool, false) catch {
                pg_compat.sleep(retry_delay);
                self.mutex.lock();
                continue :loop;
            };

            // Decrement missing count when successfully recreated
            pool._mutex.lock();
            std.debug.assert(pool._missing > 0);
            pool._missing -= 1;
            pool._mutex.unlock();

            // Ref-neutral insert (NOT conn.release(): that would drop a
            // checkout reference this fresh connection never took, and could
            // make THIS thread the last-unref → deinit → self-join).
            pool.releaseInsert(conn);
            self.mutex.lock();
            self.count -= 1;
        }

        // Loop condition (count == 0) was re-checked with the mutex held, so we
        // still own it here. Release it explicitly on this exit path.
        self.thread.?.detach();
        self.thread = null;
        self.mutex.unlock();
    }

    fn stop(self: *Reconnector) void {
        self.mutex.lock();
        self.stopped = true;
        self.mutex.unlock();
        if (self.thread) |thrd| {
            thrd.join();
        }
    }

    fn reconnect(self: *Reconnector) !void {
        self.mutex.lock();
        defer self.mutex.unlock();
        self.count += 1;
        if (self.thread == null) {
            self.thread = try std.Thread.spawn(.{ .stack_size = 1024 * 1024 }, Reconnector.run, .{self});
        }
    }
};

// A connect error is TRANSIENT if it stems from momentary resource pressure
// rather than a permanent misconfiguration: a full listen backlog
// (ConnectionRefused), ephemeral-port/address contention (AddressInUse),
// fd/socket exhaustion (SocketCreateFailed), a slow SYN/ACK
// (ConnectionTimedOut), or the driver's catch-all (UnknownConnectError).
// These clear on their own in milliseconds, so a bounded retry-with-backoff
// absorbs connection storms and brief network/DB blips instead of failing the
// whole pool init or request. Permanent errors (auth/PG errors, permission
// denied, missing unix socket, unreachable host) are NOT retried — they would
// just waste time.
fn isTransientConnectError(err: anyerror) bool {
    return switch (err) {
        error.ConnectionRefused,
        error.ConnectionTimedOut,
        error.AddressInUse,
        error.AddressNotAvailable, // ephemeral port exhaustion — ports recycle
        error.SocketCreateFailed,
        error.UnknownConnectError,
        => true,
        else => false,
    };
}

// Capped exponential backoff for transient connect failures, bounded by the
// caller's connect timeout as an OVERALL deadline. This makes the retry
// self-correcting by failure speed, not just count:
//   * A fast-failing transient error (ephemeral-port exhaustion returns
//     immediately) gets many retries within the budget, riding out a
//     connection storm as ports/queue slots free up.
//   * A slow failure (unreachable host that consumes the whole connect
//     timeout on the first attempt) leaves no budget for a retry, so it
//     fails fast — honoring the timeout the caller asked for.
// Permanent errors (auth, bad host type) are never retried (see
// isTransientConnectError). Per-attempt backoff is capped so one sleep never
// dominates the budget; a hard attempt cap backstops instant-fail spins.
const CONNECT_MAX_ATTEMPTS: u16 = 64;
const CONNECT_BACKOFF_BASE_NS: u64 = 25 * std.time.ns_per_ms;
const CONNECT_BACKOFF_CAP_NS: u64 = 500 * std.time.ns_per_ms;
// Retry budget when no connect timeout is configured (connect_timeout_ms == 0,
// i.e. OS-default blocking connect): enough to self-heal a transient storm
// without hanging a startup indefinitely.
const CONNECT_DEFAULT_BUDGET_NS: u64 = 5 * std.time.ns_per_s;

fn newConnection(pool: *Pool, log_failure: bool) !*Conn {
    const opts = &pool._opts;
    const allocator = pool._allocator;
    _ = log_failure;

    const conn = allocator.create(Conn) catch |err| {
        std.debug.print("[HYPER] newConnection: alloc FAILED err={}\n", .{err});
        return err;
    };
    errdefer allocator.destroy(conn);

    // Open the TCP connection, retrying transient failures with backoff so a
    // connection storm (many pools/tests connecting at once) or a brief blip
    // doesn't fail pool init outright — bounded by the caller's connect
    // timeout as an overall deadline (see the constants above).
    const budget_ns: u64 = if (opts.connect.connect_timeout_ms > 0)
        @as(u64, opts.connect.connect_timeout_ms) * std.time.ns_per_ms
    else
        CONNECT_DEFAULT_BUDGET_NS;
    const start_ns = pg_compat.nanoTimestamp();
    const deadline_ns = start_ns + @as(i128, @intCast(budget_ns));
    var attempt: u16 = 0;
    conn.* = while (true) {
        if (Conn.open(allocator, opts.connect)) |opened| {
            break opened;
        } else |err| {
            attempt += 1;
            const now_ns = pg_compat.nanoTimestamp();
            const out_of_budget = now_ns >= deadline_ns;
            if (attempt >= CONNECT_MAX_ATTEMPTS or out_of_budget or !isTransientConnectError(err)) {
                const host = if (opts.connect.host) |h| h else "(null)";
                const db = if (opts.auth.database) |d| d else "(null)";
                std.debug.print(
                    "[HYPER] newConnection: open FAILED host={s} db={s} err={} (after {d} attempt(s))\n",
                    .{ host, db, err, attempt },
                );
                return err;
            }
            // Exponential backoff, capped, and clamped to the remaining budget
            // so the last sleep never overshoots the deadline.
            const shift: u6 = @intCast(@min(attempt - 1, 20));
            var backoff = @min(CONNECT_BACKOFF_BASE_NS << shift, CONNECT_BACKOFF_CAP_NS);
            const remaining_ns: u64 = @intCast(@max(deadline_ns - now_ns, 0));
            backoff = @min(backoff, remaining_ns);
            pg_compat.sleep(backoff);
        }
    };
    errdefer conn.deinit();

    conn.auth(opts.auth) catch |err| {
        const db = if (opts.auth.database) |d| d else "(null)";
        std.debug.print("[HYPER] newConnection: auth FAILED user={s} db={s} err={}\n", .{
            opts.auth.username,
            db,
            err,
        });
        if (conn.err) |pg_err| {
            std.debug.print("[HYPER] newConnection: pg error: {s}\n", .{pg_err.message});
        }
        return err;
    };
    conn._pool = pool;
    return conn;
}

const t = lib.testing;
test "Pool" {
    var pool = try Pool.init(t.allocator, .{
        .size = 2,
        .auth = t.authOpts(.{}),
        .connect_on_init_count = 1,
    });
    defer pool.deinit();

    {
        const c1 = try pool.acquire();
        defer pool.release(c1);
        _ = try c1.exec(
            \\ drop table if exists pool_test;
            \\ create table pool_test (id int not null)
        , .{});
    }

    const t1 = try std.Thread.spawn(.{}, testPool, .{pool});
    const t2 = try std.Thread.spawn(.{}, testPool, .{pool});
    const t3 = try std.Thread.spawn(.{}, testPool, .{pool});

    t1.join();
    t2.join();
    t3.join();

    {
        const c1 = try pool.acquire();
        defer c1.release();

        const affected = try c1.exec("delete from pool_test", .{});
        try t.expectEqual(1500, affected.?);
    }
}

test "Pool: Release" {
    var pool = try Pool.init(t.allocator, .{
        .size = 2,
        .auth = t.authOpts(.{}),
    });
    defer pool.deinit();

    const c1 = try pool.acquire();
    c1._state = .query;
    pool.release(c1);
}

test "Pool: stats" {
    var pool = try Pool.init(t.allocator, .{
        .size = 3,
        .auth = t.authOpts(.{}),
    });
    defer pool.deinit();

    // Initial state: all connections available
    {
        const s = pool.stats();
        try t.expectEqual(3, s.size);
        try t.expectEqual(3, s.available);
        try t.expectEqual(0, s.missing);
        try t.expectEqual(0, s.in_use);
    }

    // Acquire one connection
    const c1 = try pool.acquire();
    {
        const s = pool.stats();
        try t.expectEqual(3, s.size);
        try t.expectEqual(2, s.available);
        try t.expectEqual(0, s.missing);
        try t.expectEqual(1, s.in_use);
    }

    // Acquire another
    const c2 = try pool.acquire();
    {
        const s = pool.stats();
        try t.expectEqual(3, s.size);
        try t.expectEqual(1, s.available);
        try t.expectEqual(0, s.missing);
        try t.expectEqual(2, s.in_use);
    }

    // Release one
    pool.release(c1);
    {
        const s = pool.stats();
        try t.expectEqual(3, s.size);
        try t.expectEqual(2, s.available);
        try t.expectEqual(0, s.missing);
        try t.expectEqual(1, s.in_use);
    }

    // Release the other
    pool.release(c2);
    {
        const s = pool.stats();
        try t.expectEqual(3, s.size);
        try t.expectEqual(3, s.available);
        try t.expectEqual(0, s.missing);
        try t.expectEqual(0, s.in_use);
    }
}

test "Pool: exec" {
    var pool = try Pool.init(t.allocator, .{ .size = 1, .auth = t.authOpts(.{}) });
    defer pool.deinit();

    {
        const n = try pool.exec("insert into simple_table values ($1), ($2), ($3)", .{ "pool_insert_args_a", "pool_insert_args_b", "pool_insert_args_c" });
        try t.expectEqual(3, n.?);
    }

    {
        // this makes sure the connection was returned to the pool
        const n = try pool.exec("insert into simple_table values ($1)", .{"pool_insert_args_a"});
        try t.expectEqual(1, n.?);
    }
}

test "Pool: Query/Row" {
    var pool = try Pool.init(t.allocator, .{ .size = 1, .auth = t.authOpts(.{}) });
    defer pool.deinit();

    {
        _ = try pool.exec("insert into all_types (id, col_int8, col_text) values ($1, $2, $3)", .{ 100, 1, "val-1" });
        _ = try pool.exec("insert into all_types (id, col_int8, col_text) values ($1, $2, $3)", .{ 101, 2, "val-2" });
    }

    for (0..3) |_| {
        var result = try pool.query("select col_int8, col_text from all_types where id = any($1)", .{[2]i32{ 100, 101 }});
        defer result.deinit();

        const row1 = (try result.nextUnsafe()) orelse unreachable;
        try t.expectEqual(1, row1.get(i64, 0));
        try t.expectString("val-1", row1.get([]u8, 1));

        const row2 = (try result.nextUnsafe()) orelse unreachable;
        try t.expectEqual(2, row2.get(i64, 0));
        try t.expectString("val-2", row2.get([]u8, 1));

        try t.expectEqual(null, result.nextUnsafe());
    }

    for (0..3) |_| {
        var row = try pool.rowUnsafe("select col_int8, col_text from all_types where id = $1", .{101}) orelse unreachable;
        defer row.deinit() catch {};

        try t.expectEqual(2, row.get(i64, 0));
        try t.expectString("val-2", row.get([]u8, 1));
    }
}

test "Pool: Row error" {
    var pool = try Pool.init(t.allocator, .{ .size = 1, .auth = t.authOpts(.{}) });
    defer pool.deinit();

    _ = try pool.rowUnsafe("insert into all_types (id) values ($1)", .{200});

    // This would segfault:
    // https://github.com/karlseguin/pg.zig/issues/34
    try t.expectError(error.PG, pool.rowUnsafe("insert into all_types (id) values ($1)", .{200}));

    try t.expectEqual(1, pool._available);
}

fn testPool(p: *Pool) void {
    for (0..500) |i| {
        const conn = p.acquire() catch unreachable;
        _ = conn.exec("insert into pool_test (id) values ($1)", .{i}) catch unreachable;
        conn.release();
    }
}
