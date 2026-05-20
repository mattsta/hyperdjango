// db.zig — Zig-native Postgres via pg.zig
// Zero-Python CRUD: HTTP request → dhi validate → pg.zig query → JSON response
// No GIL acquired at any point.

const std = @import("std");
const pg = @import("pg");
const py = @import("py.zig");
const c = py.c;
const router_mod = @import("router.zig");
const dhi = @import("dhi_validator.zig");
const json_parser = @import("json_parser.zig");
// Pure PG-binary → canonical-string renderers (NUMERIC/TIMESTAMP/DATE/TIME/
// UUID + JSON compaction), shared by the Python-object converters and the
// query_json writer so both stay byte-identical. Unit-tested standalone.
const pg_render = @import("pg_render.zig");
const metrics = @import("metrics_py.zig");
const server = @import("server.zig");

// Shared allocator authority: c_allocator in production, or the safety-checking
// DebugAllocator under -Dheap-safety=true. The pool created here passes this
// allocator into pg.Pool.init, so every connection/pool/result allocation flows
// through it — giving the safety build double-free / UAF / OOB detection over
// the whole pg path.
const allocator = @import("heap.zig").gpa;

// Debug tracing for pool/connection lifecycle.
// Enabled in Debug builds, compiled out in Release.
const TRACE = @import("builtin").mode == .Debug;

fn trace(comptime fmt: []const u8, args: anytype) void {
    if (TRACE) {
        std.debug.print("[HYPER] " ++ fmt ++ "\n", args);
    }
}

// ── Types ────────────────────────────────────────────────────────────────────

pub const DbOp = enum(u8) { select_one, select_list, insert, delete, custom_query, custom_query_single };

pub const DbRouteEntry = struct {
    op: DbOp,
    table: []const u8,
    columns: []const []const u8,
    pk_column: ?[]const u8,
    pk_param: ?[]const u8,
    select_sql: []const u8,
    insert_sql: []const u8,
    delete_sql: []const u8,
    custom_sql: []const u8,
    param_names: []const []const u8,
    cache_name: ?[]const u8, // prepared statement cache name (skips Parse on repeat queries)
    schema: ?dhi.ModelSchema,
};

// ── Pool registry: multiple independent pools, each owned by a connection ────
// Each PgZigConnection gets its own pool via _db_configure → handle.
// This prevents one connection's lifecycle from destroying another's pool.

// Dynamic pool registry — grows as needed, no arbitrary limit.
var pool_registry: std.ArrayListUnmanaged(?*pg.Pool) = .empty;
var pool_registry_mutex: py.Mutex = .{};

// Active pool handle — set by Python side before each operation.
// This tells acquireConn() which pool to use.
// A2#2: read/written cross-thread (Python C API writers vs native worker-thread
// readers under GIL-off). Always accessed via @atomicLoad/@atomicStore with
// acquire/release so the pool selection is at least coherent (no torn/stale i64).
var active_pool_handle: i64 = -1;

const PoolLookup = enum {
    exact, // only the given handle
    fallback_to_active, // given handle, else active handle, else first pool
};

/// SINGLE AUTHORITY for turning a pool handle into a usable *Pool.
/// Resolves under pool_registry_mutex and returns the pool with a strong
/// reference already taken — a concurrent _db_close_pool can detach it from
/// the registry but cannot free it until the caller drops the reference with
/// pool.unref(). Every native op that dereferences a registry pool MUST go
/// through here: a raw registry read escaping the mutex is exactly the
/// close-vs-in-flight-op use-after-free this closes.
fn retainPoolByHandle(handle: i64, lookup: PoolLookup) ?*pg.Pool {
    pool_registry_mutex.lock();
    defer pool_registry_mutex.unlock();
    if (handle >= 0 and handle < pool_registry.items.len) {
        if (pool_registry.items[@intCast(handle)]) |pool| {
            pool.retain();
            return pool;
        }
    }
    if (lookup == .exact) return null;
    const active = @atomicLoad(i64, &active_pool_handle, .acquire);
    if (active >= 0 and active < pool_registry.items.len) {
        if (pool_registry.items[@intCast(active)]) |pool| {
            pool.retain();
            return pool;
        }
    }
    // Fallback: first registered pool.
    for (pool_registry.items) |entry| {
        if (entry) |pool| {
            pool.retain();
            return pool;
        }
    }
    return null;
}

/// Registry slot count for diagnostics — the only safe way to read
/// pool_registry.items.len outside the mutex (append may reallocate it).
fn registeredPoolCount() usize {
    pool_registry_mutex.lock();
    defer pool_registry_mutex.unlock();
    return pool_registry.items.len;
}

var db_routes_map: ?std.StringHashMap(DbRouteEntry) = null;

// ── Production-ready DB cache: TTL, per-table invalidation, thread-safe, LRU ─

const CacheEntry = struct {
    body: []const u8,
    table: []const u8, // which table this entry belongs to (for targeted invalidation)
    created_at: i64, // timestamp in seconds
};

const DB_CACHE_MAX: usize = 10_000;
const DB_CACHE_MAX_BYTES: usize = 128 * 1024 * 1024; // 128 MB total size limit
var db_cache_enabled: bool = true;
var db_cache_ttl: i64 = 30; // default 30 second TTL
var db_cache: ?std.StringHashMap(CacheEntry) = null;
var db_cache_count: usize = 0;
var db_cache_total_bytes: usize = 0; // total body bytes across all entries
var db_cache_mutex: py.Mutex = .{};

// Per-thread connections
const MAX_WORKERS: usize = 24;
var thread_conns: [MAX_WORKERS]?*pg.Conn = [_]?*pg.Conn{null} ** MAX_WORKERS;
var thread_conn_count: usize = 0;
// A2#2: never written after init (the thread_conns branch below is currently
// inert), but read on native worker threads. Accessed via @atomicLoad/@atomicStore
// with acquire/release so a future writer stays coherent with readers under GIL-off.
var use_thread_conns: bool = false;

// Last query column metadata — per-thread storage.
// Each thread has its own last_columns to avoid data races under free-threading.
// _db_query returns rows directly; columns retrieved via _db_get_last_columns().
threadlocal var last_columns: ?*c.PyObject = null;

// Set during module finalization to prevent Py_DecRef on already-freed objects.
// During interpreter shutdown, Python may free objects before Zig threads
// clean up their threadlocal references. Once this flag is set, all code
// paths that would decref threadlocal Python objects skip the decref.
var module_shutting_down: std.atomic.Value(bool) = std.atomic.Value(bool).init(false);

/// Called from module_free (main.zig) during interpreter finalization.
/// Abandons all Python object references without decref — the process is
/// exiting and Python's object allocator may already be torn down.
/// This prevents SIGABRT from Py_DecRef on freed objects.
pub fn module_cleanup() void {
    module_shutting_down.store(true, .release);

    // Stop all multiplexed LISTEN/NOTIFY listener threads before tearing down
    // Python state. Each thread checks `running` and exits within one read
    // timeout (MUX_READ_TIMEOUT_MS). We do NOT join here — a thread may be in
    // a reconnect backoff or a network read, and blocking module_free() would
    // hang Python finalization. The Py_DecRef guard (module_shutting_down
    // check) in the listener cleanup path prevents use-after-free regardless.
    mux_mutex.lock();
    var mux_it = mux_listeners.valueIterator();
    while (mux_it.next()) |mlp| {
        @atomicStore(bool, &mlp.*.running, false, .release);
    }
    mux_mutex.unlock();

    // Null out this thread's last_columns without decref
    last_columns = null;

    // Clear column cache entries without decref (objects may be freed already)
    column_cache_mutex.lock();
    if (column_cache) |*cc| {
        cc.clearAndFree();
    }
    column_cache_mutex.unlock();

    // Clear interned key cache without decref
    if (interned_key_cache) |*ikc| {
        var it = ikc.iterator();
        while (it.next()) |entry| {
            allocator.free(entry.value_ptr.keys[0..entry.value_ptr.count]);
        }
        ikc.clearAndFree();
    }
}

// Column metadata cache: SQL text hash → Python list of column names.
// Most ORM queries repeat the same SQL — this avoids rebuilding the column
// list after the first execution. Uses FNV-1a hash of SQL text as key.
// THREAD SAFETY: protected by column_cache_mutex. Required because
// Python 3.14t free-threaded mode has real OS threads competing concurrently.
// SIZE BOUND: evicts all entries when exceeding COLUMN_CACHE_MAX to prevent
// unbounded memory growth. Column lists are cheap to rebuild from query results.
const COLUMN_CACHE_MAX: usize = 4096;
var column_cache: ?std.AutoHashMap(u64, *c.PyObject) = null;
var column_cache_mutex: py.Mutex = .{};

/// Interned column key cache: SQL hash → array of interned PyUnicode* column name objects.
/// Pre-interned strings are identity-comparable and immortal — used as dict keys for
/// zero-alloc per-row dict construction in db_query_dicts.
const InternedKeys = struct {
    keys: []*c.PyObject, // allocator-owned array of interned PyUnicode*
    count: usize,
};
var interned_key_cache: ?std.AutoHashMap(u64, InternedKeys) = null;

fn getInternedKeyCache() *std.AutoHashMap(u64, InternedKeys) {
    if (interned_key_cache) |*ikc| return ikc;
    interned_key_cache = std.AutoHashMap(u64, InternedKeys).init(allocator);
    return &interned_key_cache.?;
}

/// Get or create interned column key objects for a query result.
/// Caller MUST hold column_cache_mutex.
fn getOrCreateInternedKeys(sql_hash: u64, col_names: [][]const u8) ?InternedKeys {
    const ikc = getInternedKeyCache();
    if (ikc.get(sql_hash)) |existing| {
        // Stale-shape guard: verify the cached entry matches the result's
        // column set EXACTLY — both count and names. Catches:
        //   * ALTER TABLE ADD/DROP COLUMN (count changes)
        //   * DROP TABLE + CREATE TABLE with different columns (same count,
        //     different names — same SQL hash because the query text is
        //     identical, e.g. "SELECT * FROM t WHERE id = $1")
        if (existing.count == col_names.len) {
            var all_match = true;
            for (col_names, 0..) |name, i| {
                // Compare the interned PyUnicode against the Zig column name bytes.
                var py_len: c.Py_ssize_t = 0;
                const py_ptr = c.PyUnicode_AsUTF8AndSize(existing.keys[i], &py_len);
                if (py_ptr == null or @as(usize, @intCast(py_len)) != name.len or
                    !std.mem.eql(u8, py_ptr[0..@intCast(py_len)], name))
                {
                    all_match = false;
                    break;
                }
            }
            if (all_match) return existing;
        }
        // Stale — evict
        for (existing.keys) |k| c.Py_DecRef(k);
        allocator.free(existing.keys);
        _ = ikc.remove(sql_hash);
    }

    const col_count = col_names.len;
    if (col_count == 0) return null;

    const keys = allocator.alloc(*c.PyObject, col_count) catch return null;
    for (col_names, 0..) |name, i| {
        // Create PyUnicode from Zig slice (not null-terminated), then intern it
        var str_obj = c.PyUnicode_FromStringAndSize(name.ptr, @intCast(name.len)) orelse {
            // Cleanup already-created keys
            for (keys[0..i]) |k| c.Py_DecRef(k);
            allocator.free(keys);
            return null;
        };
        // InternInPlace replaces str_obj with an interned version (may be same object)
        c.PyUnicode_InternInPlace(&str_obj);
        keys[i] = str_obj;
    }

    const entry = InternedKeys{ .keys = keys, .count = col_count };
    // Evict all when full (same policy as column_cache)
    if (ikc.count() >= COLUMN_CACHE_MAX) {
        var evict_it = ikc.iterator();
        while (evict_it.next()) |e| {
            for (e.value_ptr.keys) |k| c.Py_DecRef(k);
            allocator.free(e.value_ptr.keys);
        }
        ikc.clearAndFree();
    }
    ikc.put(sql_hash, entry) catch {
        for (keys) |k| c.Py_DecRef(k);
        allocator.free(keys);
        return null;
    };
    return entry;
}

/// Get the column metadata cache. Caller MUST hold column_cache_mutex.
fn getColumnCache() *std.AutoHashMap(u64, *c.PyObject) {
    if (column_cache) |*cc| return cc;
    column_cache = std.AutoHashMap(u64, *c.PyObject).init(allocator);
    return &column_cache.?;
}

fn hashSql(sql: []const u8) u64 {
    return std.hash.Fnv1a_64.hash(sql);
}

// ── Lock-free query registry (Workstream 4, #1) ────────────────────────────
// The thread-owned-connection design (acquireConnByHandle fast path) means
// parallel queries never contend on the pool — yet db_query_dicts still took
// prep_stmt_mutex (getPreparedName: hash SQL + HashMap lookup) AND
// column_cache_mutex (getOrCreateInternedKeys) on EVERY call. Under true
// free-threaded parallelism (no GIL) those two process-global mutexes
// reserialized the entire hot path.
//
// The registry gives each distinct compiled SQL an integer handle, allocated
// once by _db_register_query and cached on the Python side (per compiled
// query). A handle indexes a FIXED, pointer-stable slot array, so the steady
// state is fully lock-free AND hash-free: the prepared-statement name and the
// interned dict-key array are each published once (release store) and
// thereafter loaded (acquire) with no mutex and no SQL re-hash.
//
// Correctness under free-threading:
//   * The slot array never reallocates (fixed size) → indexing is race-free;
//     a slot is only reachable after _db_register_query returns its index.
//   * prep_name bytes are registry-OWNED (never shared with / freed by the
//     prep_stmt_cache eviction), written before prep_ready is published
//     (release); a reader observing prep_ready==true (acquire) sees the name.
//   * RegKeys is heap-allocated, fully initialized (own ref per interned
//     key), then its pointer is published (release). Readers load (acquire)
//     an immutable, never-mutated RegKeys. A schema change bumps registry_gen
//     (via invalidateColumnCaches); a reader whose RegKeys.gen != registry_gen
//     (or whose column count differs) falls back to the locked populate path,
//     which republishes. Superseded RegKeys are intentionally leaked — bounded
//     by (distinct query shapes × rare schema changes) — to avoid unsound
//     cross-thread reclamation; they are never freed while a reader may hold
//     the pointer.
const MAX_REGISTERED_QUERIES: usize = 4096;

const RegKeys = struct {
    keys: []*c.PyObject, // registry-owned interned PyUnicode* array (own ref each)
    count: usize,
    gen: u64, // registry_gen snapshot at publish time
};

const QuerySlot = struct {
    // Atomic so the SQL hash a slot was registered under can be read lock-free
    // by regGetKeys / the handle-verification in db_query_dicts. Written once
    // (under registry_mutex) before the slot's index is published to Python,
    // so a monotonic load pairs with the acquire load of registry_count that
    // gates every use — this atomic just removes any doubt about the field.
    sql_hash: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
    prep_ready: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
    prep_name: []const u8 = "",
    keys: std.atomic.Value(?*RegKeys) = std.atomic.Value(?*RegKeys).init(null),
};

var query_registry: [MAX_REGISTERED_QUERIES]QuerySlot = [_]QuerySlot{.{}} ** MAX_REGISTERED_QUERIES;
var registry_count: std.atomic.Value(usize) = std.atomic.Value(usize).init(0);
var registry_mutex: py.Mutex = .{};
var registry_index: ?std.AutoHashMap(u64, usize) = null; // sql_hash → slot index (dedup)
var registry_gen: std.atomic.Value(u64) = std.atomic.Value(u64).init(1);

fn getRegistryIndex() *std.AutoHashMap(u64, usize) {
    if (registry_index) |*m| return m;
    registry_index = std.AutoHashMap(u64, usize).init(allocator);
    return &registry_index.?;
}

/// Allocate (or return existing) a registry handle for a SQL string.
/// One-time, mutex-guarded — Python caches the returned handle per query so
/// this is not on the hot path. Returns -1 if the registry is full (caller
/// then falls back to the locked getPreparedName/getOrCreateInternedKeys path).
fn registerQuery(sql: []const u8) i64 {
    const hash = hashSql(sql);
    registry_mutex.lock();
    defer registry_mutex.unlock();
    const idx_map = getRegistryIndex();
    if (idx_map.get(hash)) |existing| return @intCast(existing);
    const idx = registry_count.load(.monotonic);
    if (idx >= MAX_REGISTERED_QUERIES) return -1;
    query_registry[idx].sql_hash.store(hash, .monotonic);
    idx_map.put(hash, idx) catch return -1;
    // Publish the new count last — the slot is fully set up above.
    registry_count.store(idx + 1, .release);
    return @intCast(idx);
}

/// Prepared-statement name for a registered query. Lock-free once published.
/// The name is registry-owned (unique, stable, never evicted) so pinning it
/// is safe even though the shared prep_stmt_cache may evict its own entries.
fn regGetPrepName(idx: usize) ?[]const u8 {
    const slot = &query_registry[idx];
    if (slot.prep_ready.load(.acquire)) return slot.prep_name; // steady state (lock-free)
    // First execution for this handle: mint a dedicated, stable name. Guarded
    // by double-checked locking so the non-atomic `prep_name` slice (ptr+len)
    // is written by exactly ONE thread — two threads first-executing the same
    // new query concurrently must not race on that two-word write.
    registry_mutex.lock();
    defer registry_mutex.unlock();
    if (slot.prep_ready.load(.acquire)) return slot.prep_name; // recheck under lock
    const counter = prep_stmt_counter.fetchAdd(1, .monotonic);
    const owned = std.fmt.allocPrint(allocator, "hd_{d}", .{counter}) catch return null;
    slot.prep_name = owned;
    slot.prep_ready.store(true, .release); // publish after the name is written
    return owned;
}

/// True iff every interned key already matches the corresponding result column
/// name. Lock-free byte comparison of each interned PyUnicode against the Zig
/// column-name slice — same equality test the locked getOrCreateInternedKeys
/// stale-shape guard performs, hoisted so the registry fast path can reject a
/// same-count/different-names cache entry without taking column_cache_mutex.
fn regKeysMatch(keys: []*c.PyObject, col_names: [][]const u8) bool {
    for (col_names, 0..) |name, i| {
        var py_len: c.Py_ssize_t = 0;
        const py_ptr = c.PyUnicode_AsUTF8AndSize(keys[i], &py_len);
        if (py_ptr == null or @as(usize, @intCast(py_len)) != name.len or
            !std.mem.eql(u8, py_ptr[0..@intCast(py_len)], name))
            return false;
    }
    return true;
}

/// Interned dict-key array for a registered query. Lock-free once published
/// and current (gen + column-count + column-name match). Returns a slice into
/// registry-owned (never-freed) memory, safe to read from the row loop without a lock.
fn regGetKeys(idx: usize, col_names: [][]const u8) ?[]*c.PyObject {
    const slot = &query_registry[idx];
    const gen = registry_gen.load(.acquire);
    if (slot.keys.load(.acquire)) |rk| {
        // Steady state stays lock-free (no mutex, no SQL re-hash) but MUST still
        // verify the cached keys match the result's column NAMES, not just the
        // count. A cross-connection DROP TABLE + CREATE TABLE with the same column
        // COUNT but different NAMES (identical SQL text → identical registry
        // handle) does NOT raise a server cached-plan error, so registry_gen is
        // never bumped — a count-only check would hand back stale keys (e.g.
        // ["id","val"] for a table now shaped ["id","a"]). This mirrors the
        // stale-shape guard in the locked getOrCreateInternedKeys path.
        if (rk.gen == gen and rk.count == col_names.len and regKeysMatch(rk.keys, col_names))
            return rk.keys; // steady state
    }
    // Populate: build a registry-owned interned-key array under the shared
    // lock (reusing getOrCreateInternedKeys for the stale-shape check), then
    // publish. Old RegKeys (if any) is intentionally leaked.
    if (col_names.len == 0) return null;
    const sql_hash = slot.sql_hash.load(.monotonic);
    column_cache_mutex.lock();
    const interned = getOrCreateInternedKeys(sql_hash, col_names) orelse {
        column_cache_mutex.unlock();
        return null;
    };
    const new_keys = allocator.alloc(*c.PyObject, interned.count) catch {
        column_cache_mutex.unlock();
        return null;
    };
    for (0..interned.count) |i| {
        new_keys[i] = interned.keys[i];
        c.Py_IncRef(interned.keys[i]); // registry holds its own ref → never dangles
    }
    column_cache_mutex.unlock();
    const rk = allocator.create(RegKeys) catch {
        for (new_keys) |k| c.Py_DecRef(k);
        allocator.free(new_keys);
        return null;
    };
    rk.* = .{ .keys = new_keys, .count = interned.count, .gen = gen };
    slot.keys.store(rk, .release); // publish; any superseded RegKeys is leaked
    return rk.keys;
}

/// _db_register_query(sql) -> int — allocate/return a lock-free query handle.
pub fn db_register_query(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var sql_c: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "s", &sql_c) == 0) return null;
    return py.newInt(registerQuery(std.mem.span(sql_c)));
}

/// Evict stale column metadata for a specific SQL hash. Called during
/// cached-plan error recovery — after ALTER TABLE the column set has
/// changed, so the interned key array / column tuple / JSON key
/// fragments are all stale and would cause index-out-of-bounds panics
/// if reused with the new result shape.
///
/// Caller does NOT need to hold column_cache_mutex — this function
/// acquires it internally.
fn invalidateColumnCaches(sql_hash: u64) void {
    // Bump the registry generation so every lock-free RegKeys reader falls
    // back to the locked populate path and rebuilds against the new shape.
    _ = registry_gen.fetchAdd(1, .acq_rel);

    column_cache_mutex.lock();
    defer column_cache_mutex.unlock();

    // 1. Interned PyUnicode key array (db_query_dicts fast path)
    if (interned_key_cache) |*ikc| {
        if (ikc.fetchRemove(sql_hash)) |removed| {
            for (removed.value.keys) |k| c.Py_DecRef(k);
            allocator.free(removed.value.keys);
        }
    }

    // 2. Column name Python tuple (db_query tuples path)
    if (column_cache) |*cc| {
        if (cc.fetchRemove(sql_hash)) |removed| {
            c.Py_DecRef(removed.value);
        }
    }

    // 3. JSON key fragments (db_query_json path)
    if (json_key_cache) |*jkc| {
        if (jkc.fetchRemove(sql_hash)) |removed| {
            allocator.free(removed.value.fragments);
        }
    }
}

// Cached Python module references for type conversion (imported lazily, survive forever)
var py_datetime_mod: ?*c.PyObject = null;
var py_datetime_cls: ?*c.PyObject = null;
var py_date_cls: ?*c.PyObject = null;
var py_time_cls: ?*c.PyObject = null;
var py_decimal_mod: ?*c.PyObject = null;
var py_decimal_cls: ?*c.PyObject = null;
var py_uuid_mod: ?*c.PyObject = null;
var py_uuid_cls: ?*c.PyObject = null;

// Lazy Python-type interning is shared mutable state read by row-decode on
// PARALLEL worker threads (no GIL). Each group uses double-checked locking with
// a done-flag published LAST (release) after ALL its pointers are stored — so a
// concurrent reader can never observe the guard set while a sibling pointer is
// still null (the round-13 where_compiler.zig class). Publishing plain guard-
// first would let a thread decode a timestamp/decimal/uuid as None and double-
// import (leaking the first refs). On a partial-import failure we decref what we
// obtained and leave the flag unpublished so a later call retries cleanly.
var _pytypes_mutex: py.Mutex = .{};
var _datetime_ready: bool = false;
var _decimal_ready: bool = false;
var _uuid_ready: bool = false;

fn ensureDatetime() void {
    if (@atomicLoad(bool, &_datetime_ready, .acquire)) return;
    _pytypes_mutex.lock();
    defer _pytypes_mutex.unlock();
    if (_datetime_ready) return;
    const m = c.PyImport_ImportModule("datetime") orelse return;
    const dt = c.PyObject_GetAttrString(m, "datetime");
    const dcls = c.PyObject_GetAttrString(m, "date");
    const tcls = c.PyObject_GetAttrString(m, "time");
    if (dt == null or dcls == null or tcls == null) {
        if (dt) |p| c.Py_DecRef(p);
        if (dcls) |p| c.Py_DecRef(p);
        if (tcls) |p| c.Py_DecRef(p);
        c.Py_DecRef(m);
        return; // unpublished → retried next call
    }
    py_datetime_mod = m;
    py_datetime_cls = dt;
    py_date_cls = dcls;
    py_time_cls = tcls;
    @atomicStore(bool, &_datetime_ready, true, .release);
}

fn ensureDecimal() void {
    if (@atomicLoad(bool, &_decimal_ready, .acquire)) return;
    _pytypes_mutex.lock();
    defer _pytypes_mutex.unlock();
    if (_decimal_ready) return;
    const m = c.PyImport_ImportModule("decimal") orelse return;
    const cls = c.PyObject_GetAttrString(m, "Decimal");
    if (cls == null) {
        c.Py_DecRef(m);
        return;
    }
    py_decimal_mod = m;
    py_decimal_cls = cls;
    @atomicStore(bool, &_decimal_ready, true, .release);
}

fn ensureUuid() void {
    if (@atomicLoad(bool, &_uuid_ready, .acquire)) return;
    _pytypes_mutex.lock();
    defer _pytypes_mutex.unlock();
    if (_uuid_ready) return;
    const m = c.PyImport_ImportModule("uuid") orelse return;
    const cls = c.PyObject_GetAttrString(m, "UUID");
    if (cls == null) {
        c.Py_DecRef(m);
        return;
    }
    py_uuid_mod = m;
    py_uuid_cls = cls;
    @atomicStore(bool, &_uuid_ready, true, .release);
}

/// Convert PostgreSQL timestamp (microseconds since 2000-01-01) to Python datetime.
fn pgTimestampToPyDatetime(usec: i64) ?*c.PyObject {
    ensureDatetime();
    const cls = py_datetime_cls orelse return null;

    // PostgreSQL stores timestamps as microseconds since 2000-01-01 00:00:00.
    // Decompose the calendar fields in Zig (same civil-from-days arithmetic as
    // the query_json render path) and construct datetime directly — no timezone
    // conversion (matches psycopg's behavior for TIMESTAMP). The previous
    // implementation went through time.gmtime + PySequence_GetItem, which leaked
    // six NEW PyObject refs per call (a real +MB/300k-row heap leak on the hot
    // timestamp path); computing in Zig avoids the Python round-trip entirely.
    const pg_epoch_offset: i64 = 946684800; // seconds from Unix to PG epoch
    // @divFloor (not @divTrunc) so total_sec pairs with @mod (which floors:
    // remaining_usec ∈ [0, 1e6)). For a negative usec with a nonzero fraction
    // @divTrunc rounds toward zero and leaves total_sec one second too high —
    // this MUST stay identical to pg_render.writeIsoTimestamp so the object and
    // query_json paths agree. See pg_render.zig for the worked example.
    const total_sec = @divFloor(usec, 1_000_000) + pg_epoch_offset;
    const remaining_usec = @mod(usec, 1_000_000);
    const days = @divFloor(total_sec, 86400);
    const sod = total_sec - days * 86400; // seconds of day [0, 86399]
    const civ = pg_render.civilFromDays(days);
    // datetime only spans years 1..9999 — outside that, fall back to None rather
    // than trap the unsigned cast / raise deep in Py_BuildValue.
    if (civ.y < 1 or civ.y > 9999) return null;

    const dt_args = c.Py_BuildValue(
        "(lllllll)",
        @as(c_long, @intCast(civ.y)), // year
        @as(c_long, @intCast(civ.m)), // month
        @as(c_long, @intCast(civ.d)), // day
        @as(c_long, @intCast(@divTrunc(sod, 3600))), // hour
        @as(c_long, @intCast(@divTrunc(@mod(sod, 3600), 60))), // minute
        @as(c_long, @intCast(@mod(sod, 60))), // second
        @as(c_long, @intCast(remaining_usec)), // microsecond
    ) orelse return null;
    defer c.Py_DecRef(dt_args);
    return c.PyObject_Call(cls, dt_args, null);
}

/// Convert PostgreSQL date (days since 2000-01-01) to Python date.
fn pgDateToPyDate(days: i32) ?*c.PyObject {
    ensureDatetime();
    const cls = py_date_cls orelse return null;
    // 2000-01-01 as Python ordinal = 730120
    const pg_epoch_ordinal: i64 = 730120;
    const ordinal = @as(i64, days) + pg_epoch_ordinal;
    // Python date.fromordinal accepts 1..3652059 (0001-01-01 .. 9999-12-31). A
    // BC date (PG stores them) or a far-future overflow lands outside that and
    // raises ValueError deep in the call — return None instead (consistent with
    // pgTimestampToPyDatetime and the query_json path, which already emits null).
    if (ordinal < 1 or ordinal > 3652059) return py.pyNone();
    const py_ord = py.newInt(ordinal);
    defer c.Py_DecRef(py_ord);

    const method = c.PyObject_GetAttrString(cls, "fromordinal") orelse return null;
    defer c.Py_DecRef(method);
    const call_args = c.PyTuple_Pack(1, py_ord) orelse return null;
    defer c.Py_DecRef(call_args);
    return c.PyObject_Call(method, call_args, null);
}

/// Convert PostgreSQL time (microseconds since midnight) to Python time.
fn pgTimeToPyTime(usec: i64) ?*c.PyObject {
    ensureDatetime();
    const cls = py_time_cls orelse return null;
    const total_sec = @divTrunc(usec, 1_000_000);
    const remaining_usec = @mod(usec, 1_000_000);
    const hour = @divTrunc(total_sec, 3600);
    const minute = @divTrunc(@mod(total_sec, 3600), 60);
    const second = @mod(total_sec, 60);

    // PostgreSQL's TIME domain includes '24:00:00', which Python's time (hour
    // 0..23) rejects — calling time(24,...) raises ValueError and poisons the
    // interpreter error state. Return the ISO string instead (identical to what
    // the query_json path emits: writeIsoTime happily renders "24:00:00").
    if (hour >= 24 or hour < 0) {
        var tbuf: [20]u8 = undefined;
        const s = pg_render.writeIsoTime(&tbuf, usec) orelse return py.pyNone();
        return py.newString(s) orelse py.pyNone();
    }

    const args = c.Py_BuildValue("llll", hour, minute, second, remaining_usec) orelse return null;
    defer c.Py_DecRef(args);
    return c.PyObject_Call(cls, args, null);
}

/// Convert PostgreSQL NUMERIC binary to Python Decimal.
fn pgNumericToPyDecimal(data: []const u8) ?*c.PyObject {
    ensureDecimal();
    const cls = py_decimal_cls orelse return null;
    // 64 base-10000 groups = 256 decimal digits + sign + '.' = 258 worst case;
    // 288 adds scientific-exponent slack. A 256-digit value truncated its last
    // digit at 256.
    var buf: [288]u8 = undefined;
    const str_val = pg_render.pgNumericToStr(data, &buf) orelse return null;
    const py_str = py.newString(str_val) orelse return null;
    defer c.Py_DecRef(py_str);
    const args = c.PyTuple_Pack(1, py_str) orelse return null;
    defer c.Py_DecRef(args);
    return c.PyObject_Call(cls, args, null);
}

/// Convert PostgreSQL UUID (16 bytes) to Python uuid.UUID.
fn pgUuidToPyUuid(data: []const u8) ?*c.PyObject {
    ensureUuid();
    const cls = py_uuid_cls orelse return null;
    var buf: [36]u8 = undefined;
    const s = pg_render.pgUuidToStr(data, &buf) orelse return null;
    const py_str = py.newString(s) orelse return null;
    defer c.Py_DecRef(py_str);
    const args = c.PyTuple_Pack(1, py_str) orelse return null;
    defer c.Py_DecRef(args);
    return c.PyObject_Call(cls, args, null);
}

// ── Binary array decoding (multi-dimensional aware) ─────────────────────────
// Binary array layout: int32 ndim, int32 flags, int32 elem_oid, then per dim
// [int32 length, int32 lower_bound], then elements in row-major order, each
// [int32 len (-1 = null) + len bytes]. The previous per-type converters read
// nelems from data[12..16] (dim-0 length) and flattened everything after the
// first dimension header — silently truncating a 2-D array to its first row.
// These helpers honor ndim and reconstruct nested Python lists.

const MAX_ARRAY_DIMS = 6; // PostgreSQL's MAXDIM

const ArrayHeader = struct {
    ndim: i32,
    dims: [MAX_ARRAY_DIMS]i32,
    data_off: usize, // byte offset where element payload begins
};

fn readArrayHeader(data: []const u8) ?ArrayHeader {
    if (data.len < 12) return null;
    const ndim = std.mem.readInt(i32, data[0..4], .big);
    if (ndim < 0 or ndim > MAX_ARRAY_DIMS) return null;
    var hdr = ArrayHeader{ .ndim = ndim, .dims = undefined, .data_off = 12 };
    var off: usize = 12; // skip ndim + flags + elem_oid
    var d: usize = 0;
    while (d < @as(usize, @intCast(ndim))) : (d += 1) {
        if (off + 8 > data.len) return null;
        const len = std.mem.readInt(i32, data[off..][0..4], .big);
        if (len < 0) return null;
        hdr.dims[d] = len;
        off += 8; // dim length + lower bound
    }
    hdr.data_off = off;
    return hdr;
}

/// Decode a single leaf array element (already sliced to its payload bytes) into
/// a native Python object, dispatching on the ARRAY type OID.
fn decodeArrayElem(elem: []const u8, array_oid: i32) ?*c.PyObject {
    return switch (array_oid) {
        1005 => if (elem.len >= 2) py.newInt(@as(i64, std.mem.readInt(i16, elem[0..2], .big))) else py.pyNone(), // int2[]
        1007, 1028 => if (elem.len >= 4) py.newInt(@as(i64, std.mem.readInt(i32, elem[0..4], .big))) else py.pyNone(), // int4[] / oid[]
        1016 => if (elem.len >= 8) py.newInt(std.mem.readInt(i64, elem[0..8], .big)) else py.pyNone(), // int8[]
        1000 => if (elem.len >= 1) (if (elem[0] != 0) py.pyTrue() else py.pyFalse()) else py.pyNone(), // bool[]
        1021 => if (elem.len >= 4) c.PyFloat_FromDouble(@as(f64, @as(f32, @bitCast(std.mem.readInt(u32, elem[0..4], .big))))) else py.pyNone(), // float4[]
        1022 => if (elem.len >= 8) c.PyFloat_FromDouble(@bitCast(std.mem.readInt(u64, elem[0..8], .big))) else py.pyNone(), // float8[]
        1003, 1009, 1015 => py.newString(elem) orelse py.pyNone(), // name[]/text[]/varchar[]
        1115, 1185 => if (elem.len >= 8) pgTimestampToPyDatetime(std.mem.readInt(i64, elem[0..8], .big)) else py.pyNone(), // timestamp[]
        1182 => if (elem.len >= 4) pgDateToPyDate(std.mem.readInt(i32, elem[0..4], .big)) else py.pyNone(), // date[]
        1183 => if (elem.len >= 8) pgTimeToPyTime(std.mem.readInt(i64, elem[0..8], .big)) else py.pyNone(), // time[]
        1231 => pgNumericToPyDecimal(elem), // numeric[]
        2951 => pgUuidToPyUuid(elem), // uuid[]
        1001 => py.newBytes(elem), // bytea[]
        3807 => blk: { // jsonb[]
            const j = if (elem.len > 0 and elem[0] == 0x01) elem[1..] else elem;
            break :blk json_parser.jsonToPython(j);
        },
        199 => json_parser.jsonToPython(elem), // json[]
        else => py.newString(elem) orelse py.pyNone(),
    };
}

/// Recursively build the Python list for dimension `dim_idx`, advancing
/// `offset` through the row-major element stream.
fn buildArrayDim(data: []const u8, offset: *usize, dims: []const i32, dim_idx: usize, array_oid: i32) ?*c.PyObject {
    const count = dims[dim_idx];
    const list = c.PyList_New(0) orelse return null;
    const is_leaf = dim_idx + 1 >= dims.len;
    var i: i32 = 0;
    while (i < count) : (i += 1) {
        var child: ?*c.PyObject = undefined;
        if (is_leaf) {
            if (offset.* + 4 > data.len) break;
            const elem_len = std.mem.readInt(i32, data[offset.*..][0..4], .big);
            offset.* += 4;
            if (elem_len < 0) {
                child = py.pyNone();
            } else {
                const elen: usize = @intCast(elem_len);
                if (offset.* + elen > data.len) break;
                child = decodeArrayElem(data[offset.*..][0..elen], array_oid);
                offset.* += elen;
            }
        } else {
            child = buildArrayDim(data, offset, dims, dim_idx + 1, array_oid);
        }
        const v = child orelse py.pyNone();
        _ = c.PyList_Append(list, v);
        c.Py_DecRef(v);
    }
    return list;
}

/// Convert any PostgreSQL binary array to a (possibly nested) Python list,
/// honoring the true dimensionality. `array_oid` is the array type OID.
fn pgArrayToPyList(data: []const u8, array_oid: i32) ?*c.PyObject {
    const hdr = readArrayHeader(data) orelse return c.PyList_New(0);
    if (hdr.ndim == 0) return c.PyList_New(0);
    var offset = hdr.data_off;
    return buildArrayDim(data, &offset, hdr.dims[0..@intCast(hdr.ndim)], 0, array_oid);
}

/// Convert PostgreSQL INTERVAL (16 bytes) to Python timedelta.
/// Binary format: 8 bytes microseconds + 4 bytes days + 4 bytes months
fn pgIntervalToPyTimedelta(data: []const u8) ?*c.PyObject {
    const usec = std.mem.readInt(i64, data[0..8], .big);
    const days = std.mem.readInt(i32, data[8..12], .big);
    const months = std.mem.readInt(i32, data[12..16], .big);

    // Python's timedelta only supports days + seconds + microseconds.
    // Months are approximated as 30 days each (standard PostgreSQL approach).
    const total_days: c_long = @as(c_long, days) + @as(c_long, months) * 30;
    // @divFloor (not @divTrunc) so seconds pair correctly with @mod, which
    // always floors: a negative sub-second interval (e.g. usec −500000) must
    // yield seconds=−1, usec=500000 (= −0.5s), matching Python timedelta —
    // @divTrunc gave seconds=0, usec=500000 (= +0.5s), off by one second.
    const total_seconds: c_long = @intCast(@divFloor(usec, 1_000_000));
    const remaining_usec: c_long = @intCast(@mod(usec, 1_000_000));

    // Import datetime.timedelta
    ensureDatetime();
    const td_cls = c.PyObject_GetAttrString(py_datetime_mod orelse return null, "timedelta") orelse return null;
    defer c.Py_DecRef(td_cls);

    // timedelta(days=N, seconds=S, microseconds=U)
    const kwargs = c.PyDict_New() orelse return null;
    defer c.Py_DecRef(kwargs);
    const py_days = py.newInt(total_days);
    defer c.Py_DecRef(py_days);
    const py_secs = py.newInt(total_seconds);
    defer c.Py_DecRef(py_secs);
    const py_usec = py.newInt(remaining_usec);
    defer c.Py_DecRef(py_usec);
    _ = c.PyDict_SetItemString(kwargs, "days", py_days);
    _ = c.PyDict_SetItemString(kwargs, "seconds", py_secs);
    _ = c.PyDict_SetItemString(kwargs, "microseconds", py_usec);

    const empty_args = c.PyTuple_New(0) orelse return null;
    defer c.Py_DecRef(empty_args);
    return c.PyObject_Call(td_cls, empty_args, kwargs);
}

/// Convert PostgreSQL INET/CIDR binary to Python ipaddress object.
/// Binary: family(1) + mask_bits(1) + is_cidr(1) + addr_len(1) + addr(4|16)
fn pgInetToPython(data: []const u8) ?*c.PyObject {
    if (data.len < 4) return py.pyNone();

    const family = data[0]; // 2=IPv4, 3=IPv6
    const mask_bits = data[1];
    const is_cidr = data[2];
    const addr_len = data[3];

    if (data.len < 4 + @as(usize, addr_len)) return py.pyNone();

    var addr_str_buf: [64]u8 = undefined;
    var addr_str: []const u8 = undefined;

    if (family == 2 and addr_len == 4) {
        // IPv4
        const a = data[4..8];
        if (is_cidr != 0) {
            addr_str = std.fmt.bufPrint(&addr_str_buf, "{d}.{d}.{d}.{d}/{d}", .{
                a[0], a[1], a[2], a[3], mask_bits,
            }) catch return py.pyNone();
        } else {
            addr_str = std.fmt.bufPrint(&addr_str_buf, "{d}.{d}.{d}.{d}", .{
                a[0], a[1], a[2], a[3],
            }) catch return py.pyNone();
        }
    } else if (family == 3 and addr_len == 16) {
        // IPv6 — build hex representation
        const a = data[4..20];
        if (is_cidr != 0) {
            addr_str = std.fmt.bufPrint(&addr_str_buf, "{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}/{d}", .{
                a[0],      a[1], a[2],  a[3],  a[4],  a[5],  a[6],  a[7],
                a[8],      a[9], a[10], a[11], a[12], a[13], a[14], a[15],
                mask_bits,
            }) catch return py.pyNone();
        } else {
            addr_str = std.fmt.bufPrint(&addr_str_buf, "{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}:{x:0>2}{x:0>2}", .{
                a[0], a[1], a[2],  a[3],  a[4],  a[5],  a[6],  a[7],
                a[8], a[9], a[10], a[11], a[12], a[13], a[14], a[15],
            }) catch return py.pyNone();
        }
    } else {
        return py.pyNone();
    }

    // Use Python's ipaddress module
    const ipaddr_mod = c.PyImport_ImportModule("ipaddress") orelse return null;
    defer c.Py_DecRef(ipaddr_mod);

    const py_str = py.newString(addr_str) orelse return null;
    defer c.Py_DecRef(py_str);

    const func_name = if (is_cidr != 0) "ip_network" else "ip_address";
    const func = c.PyObject_GetAttrString(ipaddr_mod, func_name) orelse return null;
    defer c.Py_DecRef(func);

    if (is_cidr != 0) {
        // ip_network(addr, strict=False)
        const args = c.PyTuple_Pack(1, py_str) orelse return null;
        defer c.Py_DecRef(args);
        const kwargs = c.PyDict_New() orelse return null;
        defer c.Py_DecRef(kwargs);
        _ = c.PyDict_SetItemString(kwargs, "strict", py.pyFalse());
        return c.PyObject_Call(func, args, kwargs);
    } else {
        return c.PyObject_CallOneArg(func, py_str);
    }
}

/// Convert PostgreSQL TIMETZ (time with timezone) to Python time object.
/// Binary: 8 bytes microseconds + 4 bytes tz offset (seconds west of UTC, negated)
fn pgTimetzToPython(usec: i64, tz_offset: i32) ?*c.PyObject {
    const dt_mod = c.PyImport_ImportModule("datetime") orelse return null;
    defer c.Py_DecRef(dt_mod);

    const total_secs = @divTrunc(usec, 1_000_000);
    const hour: c_int = @intCast(@divTrunc(total_secs, 3600));
    const minute: c_int = @intCast(@divTrunc(@rem(total_secs, 3600), 60));
    const second: c_int = @intCast(@rem(total_secs, 60));
    const micro: c_int = @intCast(@rem(usec, 1_000_000));

    // PostgreSQL's TIMETZ domain includes '24:00:00' (SELECT '24:00:00'::timetz),
    // which Python's time (hour 0..23) rejects — time(24,...) raises ValueError and
    // leaves the interpreter error indicator set. Both callers swallow the null
    // return via `orelse py.pyNone()` WITHOUT clearing that indicator, so the stale
    // ValueError would surface on the next unrelated C-API call. Mirror
    // pgTimeToPyTime: for an out-of-range hour, return the canonical ISO string with
    // the zone suffix (never touching Python time()) so no error state is set.
    if (hour >= 24 or hour < 0) {
        var sbuf: [40]u8 = undefined;
        const t = pg_render.writeIsoTime(&sbuf, usec) orelse return py.pyNone();
        var pos = t.len;
        const east: i64 = -@as(i64, tz_offset); // seconds east of UTC
        const off_sign: u8 = if (east < 0) '-' else '+';
        const abs_off: u64 = @abs(east);
        const oh: u64 = abs_off / 3600;
        const om: u64 = (abs_off % 3600) / 60;
        const os: u64 = abs_off % 60;
        const tail = if (os != 0)
            std.fmt.bufPrint(sbuf[pos..], "{c}{d:0>2}:{d:0>2}:{d:0>2}", .{ off_sign, oh, om, os }) catch return py.pyNone()
        else
            std.fmt.bufPrint(sbuf[pos..], "{c}{d:0>2}:{d:0>2}", .{ off_sign, oh, om }) catch return py.pyNone();
        pos += tail.len;
        return py.newString(sbuf[0..pos]) orelse py.pyNone();
    }

    // Create timezone from offset (PostgreSQL stores seconds west, Python wants east)
    const tz_cls = c.PyObject_GetAttrString(dt_mod, "timezone") orelse return null;
    defer c.Py_DecRef(tz_cls);
    const td_cls = c.PyObject_GetAttrString(dt_mod, "timedelta") orelse return null;
    defer c.Py_DecRef(td_cls);

    const py_offset_secs = py.newInt(@as(i64, -tz_offset));
    defer c.Py_DecRef(py_offset_secs);
    const kwargs = c.PyDict_New() orelse return null;
    defer c.Py_DecRef(kwargs);
    _ = c.PyDict_SetItemString(kwargs, "seconds", py_offset_secs);
    const empty = c.PyTuple_New(0) orelse return null;
    defer c.Py_DecRef(empty);
    const td = c.PyObject_Call(td_cls, empty, kwargs) orelse return null;
    defer c.Py_DecRef(td);
    const tz = c.PyObject_CallOneArg(tz_cls, td) orelse return null;
    defer c.Py_DecRef(tz);

    // time(hour, minute, second, microsecond, tzinfo=tz)
    const time_cls = c.PyObject_GetAttrString(dt_mod, "time") orelse return null;
    defer c.Py_DecRef(time_cls);
    // Py_BuildValue builds the args tuple with fresh refs it owns; PyTuple_Pack
    // would INCREF four newInt() results we never decref (a proven per-call leak).
    const time_args = c.Py_BuildValue("iiii", hour, minute, second, micro) orelse return null;
    defer c.Py_DecRef(time_args);
    const time_kwargs = c.PyDict_New() orelse return null;
    defer c.Py_DecRef(time_kwargs);
    _ = c.PyDict_SetItemString(time_kwargs, "tzinfo", tz);
    return c.PyObject_Call(time_cls, time_args, time_kwargs);
}

/// Convert PostgreSQL MONEY binary (int64 cents) to Python Decimal.
fn pgMoneyToDecimal(data: []const u8) ?*c.PyObject {
    ensureDecimal();
    const cls = py_decimal_cls orelse return null;
    if (data.len != 8) return null;
    const cents = std.mem.readInt(i64, data[0..8], .big);
    var buf: [32]u8 = undefined;
    // Shared renderer derives the sign from cents<0 so -1..-99 cents keep it.
    const s = pg_render.pgMoneyToStr(cents, &buf) orelse return null;
    const py_str = py.newString(s) orelse return null;
    defer c.Py_DecRef(py_str);
    const args = c.PyTuple_Pack(1, py_str) orelse return null;
    defer c.Py_DecRef(args);
    return c.PyObject_Call(cls, args, null);
}

/// Convert PostgreSQL BIT/VARBIT binary to Python int.
/// Binary: 4 bytes bit count + ceil(bits/8) bytes data.
/// Bit string "10110011" → int 0b10110011 = 179.
fn pgBitToPython(data: []const u8) ?*c.PyObject {
    if (data.len < 4) return c.PyLong_FromLong(0);
    const nbits: usize = @intCast(std.mem.readInt(i32, data[0..4], .big));
    if (nbits == 0) return c.PyLong_FromLong(0);

    // Build null-terminated binary digit string for PyLong_FromString base 2
    var buf = allocator.alloc(u8, nbits + 1) catch return null;
    defer allocator.free(buf);

    for (0..nbits) |i| {
        const byte_idx = 4 + i / 8;
        const bit_idx: u3 = @intCast(7 - (i % 8));
        if (byte_idx < data.len) {
            buf[i] = if ((data[byte_idx] >> bit_idx) & 1 == 1) '1' else '0';
        } else {
            buf[i] = '0';
        }
    }
    buf[nbits] = 0; // null terminate
    return c.PyLong_FromString(buf.ptr, null, 2);
}

/// Convert PostgreSQL TSVECTOR binary to Python list[tuple[str, list[int]]].
/// Binary: 4 bytes num_lexemes, then per lexeme: null-terminated string + 2 bytes num_positions + 2*num_positions bytes.
/// Each position word: bits 0-13 = position (1-based), bits 14-15 = weight (0=D, 1=C, 2=B, 3=A).
/// Returns: [("lexeme", [pos1, pos2, ...]), ...]
fn pgTsvectorToPython(data: []const u8) ?*c.PyObject {
    if (data.len < 4) return c.PyList_New(0);
    const num_lexemes: usize = @intCast(std.mem.readInt(i32, data[0..4], .big));
    if (num_lexemes == 0) return c.PyList_New(0);

    const py_list = c.PyList_New(@intCast(num_lexemes)) orelse return null;
    var offset: usize = 4;

    for (0..num_lexemes) |li| {
        // Find null-terminated lexeme string
        const str_start = offset;
        while (offset < data.len and data[offset] != 0) : (offset += 1) {}
        if (offset >= data.len) break;
        const lexeme = data[str_start..offset];
        offset += 1; // skip null terminator

        // Read position count
        if (offset + 2 > data.len) break;
        const npos: usize = @intCast(std.mem.readInt(u16, data[offset..][0..2], .big));
        offset += 2;

        // Build position list (just the position numbers, weights are rarely used)
        const pos_list = c.PyList_New(@intCast(npos)) orelse break;
        for (0..npos) |pi| {
            if (offset + 2 > data.len) break;
            const pos_word = std.mem.readInt(u16, data[offset..][0..2], .big);
            offset += 2;
            const position = pos_word & 0x3FFF; // lower 14 bits
            _ = c.PyList_SetItem(pos_list, @intCast(pi), c.PyLong_FromLong(@intCast(position)));
        }

        // Build (lexeme, positions) tuple
        const py_lexeme = py.newString(lexeme) orelse break;
        const tup = c.PyTuple_Pack(2, py_lexeme, pos_list) orelse {
            c.Py_DecRef(py_lexeme);
            c.Py_DecRef(pos_list);
            break;
        };
        c.Py_DecRef(py_lexeme);
        c.Py_DecRef(pos_list);
        _ = c.PyList_SetItem(py_list, @intCast(li), tup);
    }
    return py_list;
}

/// Convert PostgreSQL TSQUERY binary to Python str.
/// Binary format: 4 bytes num_nodes, then nodes in prefix (pre-order) traversal.
/// Each node: 1 byte type. VAL(1): weight(1) + prefix(1) + null-terminated lexeme.
/// OPER(2): op_kind(1) [+ distance(2) for phrase]. Operands follow recursively.
fn pgTsqueryToPython(data: []const u8) ?*c.PyObject {
    if (data.len < 4) return py.newString("");
    const num_nodes: usize = @intCast(std.mem.readInt(i32, data[0..4], .big));
    if (num_nodes == 0) return py.newString("");

    // Use a growable buffer for the output string
    var out_buf: [2048]u8 = undefined;
    var out_pos: usize = 0;
    var offset: usize = 4;

    tsqueryNode(data, &offset, &out_buf, &out_pos);

    return py.newString(out_buf[0..out_pos]);
}

/// Recursively parse one tsquery node from prefix-order binary, appending to output buffer.
fn tsqueryNode(data: []const u8, offset: *usize, buf: *[2048]u8, pos: *usize) void {
    if (offset.* >= data.len) return;
    const node_type = data[offset.*];
    offset.* += 1;

    if (node_type == 1) {
        // VAL: weight(1) + prefix(1) + null-terminated lexeme
        if (offset.* + 2 > data.len) return;
        offset.* += 1; // skip weight
        const prefix = data[offset.*];
        offset.* += 1;
        const str_start = offset.*;
        while (offset.* < data.len and data[offset.*] != 0) : (offset.* += 1) {}
        const lexeme = data[str_start..offset.*];
        if (offset.* < data.len) offset.* += 1; // skip null

        // Write 'lexeme' to buffer
        if (pos.* + lexeme.len + 4 < buf.len) {
            buf.*[pos.*] = '\'';
            pos.* += 1;
            @memcpy(buf.*[pos.*..][0..lexeme.len], lexeme);
            pos.* += lexeme.len;
            buf.*[pos.*] = '\'';
            pos.* += 1;
            if (prefix != 0) {
                buf.*[pos.*] = ':';
                pos.* += 1;
                buf.*[pos.*] = '*';
                pos.* += 1;
            }
        }
    } else if (node_type == 2) {
        // OPER: op_kind(1)
        if (offset.* >= data.len) return;
        const op_kind = data[offset.*];
        offset.* += 1;

        var dist: u16 = 0;
        if (op_kind == 4) {
            // PHRASE: additional 2-byte distance
            if (offset.* + 2 <= data.len) {
                dist = std.mem.readInt(u16, data[offset.*..][0..2], .big);
                offset.* += 2;
            }
        }

        if (op_kind == 1) {
            // NOT (unary): !operand
            if (pos.* + 1 < buf.len) {
                buf.*[pos.*] = '!';
                pos.* += 1;
            }
            tsqueryNode(data, offset, buf, pos);
        } else {
            // Binary: left OP right
            tsqueryNode(data, offset, buf, pos);
            const op_str: []const u8 = switch (op_kind) {
                2 => " & ",
                3 => " | ",
                4 => if (dist == 1) " <-> " else " <-> ", // simplified
                else => " & ",
            };
            if (pos.* + op_str.len < buf.len) {
                @memcpy(buf.*[pos.*..][0..op_str.len], op_str);
                pos.* += op_str.len;
            }
            tsqueryNode(data, offset, buf, pos);
        }
    }
}

/// Convert PostgreSQL MACADDR (6 bytes) / MACADDR8 (8 bytes) to a Python str
/// in canonical colon-hex form (matches PG ::text and psycopg's str form).
fn pgMacaddrToPython(data: []const u8) ?*c.PyObject {
    var buf: [24]u8 = undefined;
    const s = pg_render.pgMacaddrToStr(data, &buf) orelse return py.pyNone();
    return py.newString(s) orelse py.pyNone();
}

/// py.newString, but if the bytes are not valid UTF-8 (PyUnicode raises a
/// UnicodeDecodeError and returns null) the pending Python error is CLEARED —
/// otherwise it lingers and poisons the very next native call — and None is
/// returned. Used by the object-path fallback for otherwise-unhandled binary
/// OIDs so a non-textual value degrades to None instead of crashing.
fn newStringOrNone(data: []const u8) *c.PyObject {
    if (py.newString(data)) |s| return s;
    c.PyErr_Clear();
    return py.pyNone();
}

/// Convert PostgreSQL HSTORE binary to Python dict.
/// Binary format: int32 num_pairs, then per pair: int32 key_len + key + int32 val_len + val
fn pgHstoreToPyDict(data: []const u8) ?*c.PyObject {
    if (data.len < 4) return py.newDict();

    const num_pairs: usize = @intCast(std.mem.readInt(i32, data[0..4], .big));
    const dict = py.newDict() orelse return null;

    var offset: usize = 4;
    for (0..num_pairs) |_| {
        // Key
        if (offset + 4 > data.len) break;
        const key_len: usize = @intCast(std.mem.readInt(i32, data[offset..][0..4], .big));
        offset += 4;
        if (offset + key_len > data.len) break;
        const key = py.newString(data[offset..][0..key_len]) orelse {
            c.Py_DecRef(dict);
            return null;
        };
        offset += key_len;

        // Value
        if (offset + 4 > data.len) {
            c.Py_DecRef(key);
            break;
        }
        const val_len_raw = std.mem.readInt(i32, data[offset..][0..4], .big);
        offset += 4;

        const val = if (val_len_raw < 0)
            // NULL value
            py.pyNone()
        else blk: {
            const val_len: usize = @intCast(val_len_raw);
            if (offset + val_len > data.len) break :blk py.pyNone();
            const v = py.newString(data[offset..][0..val_len]) orelse py.pyNone();
            offset += val_len;
            break :blk v;
        };

        if (c.PyDict_SetItem(dict, key, val) != 0) {
            c.Py_DecRef(key);
            c.Py_DecRef(val);
            c.Py_DecRef(dict);
            return null;
        }
        c.Py_DecRef(key);
        c.Py_DecRef(val);
    }

    return dict;
}

// Dynamic OID registry for extension types (hstore, enums, etc.)
// Populated at connection time by querying pg_type.
var hstore_oid: i32 = 0;

// Query timeout — stored globally so new pool connections get SET statement_timeout.
// 0 = no timeout (PostgreSQL default). Set via _db_configure(conn_str, pool_size, connect_timeout, query_timeout).
var stored_query_timeout_ms: c_int = 0;

// Custom enum type registry — maps PostgreSQL enum OIDs to their label lists.
// Dynamically growable. Each registered enum stores its OID and owned label strings.
// On query result, if an OID matches a registered enum, we return the string label.

const EnumTypeEntry = struct {
    oid: i32,
    array_oid: i32, // OID for the array type (e.g. _mood), 0 if not found
    labels: std.ArrayListUnmanaged([]const u8), // owned duped strings

    fn deinit(self: *EnumTypeEntry) void {
        for (self.labels.items) |label| {
            allocator.free(label);
        }
        self.labels.deinit(allocator);
    }

    fn findLabel(self: *const EnumTypeEntry, data: []const u8) ?[]const u8 {
        // PostgreSQL sends enum values as text — match against stored labels
        for (self.labels.items) |label| {
            if (std.mem.eql(u8, label, data)) return label;
        }
        return null;
    }
};

var enum_registry: std.ArrayListUnmanaged(EnumTypeEntry) = .empty;

// Guards `enum_registry` against concurrent mutation while decode-hot-path
// readers iterate it. Under free-threaded 3.14t (no GIL) `findEnumByOid` runs
// concurrently on many OS threads while `db_register_enum` / `db_list_enums`
// can append to (reallocating + FREEing the old backing buffer) or update
// (freeing old label strings) the registry — a lock-free reader would then see
// a torn `.items` slice or freed memory (use-after-free). Readers take
// `lockShared` and COPY OUT the scalar result before releasing; mutators take
// the exclusive `lock`. Same idiom as the hashring / metrics / server RwLocks.
var enum_registry_lock: py.RwLock = .{};

/// Scalar result of an enum OID lookup, copied out under the registry lock so
/// it stays valid after the lock is released — no pointer/slice into registry
/// memory (which a writer may realloc/free) ever escapes to the caller.
const EnumLookup = struct {
    array_oid: i32,
};

/// Look up an enum entry by OID (scalar or array). Returns the matched entry's
/// scalar fields by value; null if not registered. Holds the shared lock only
/// for the scan.
fn findEnumByOid(oid: i32) ?EnumLookup {
    enum_registry_lock.lockShared();
    defer enum_registry_lock.unlockShared();
    for (enum_registry.items) |*entry| {
        if (entry.oid == oid or entry.array_oid == oid) return .{ .array_oid = entry.array_oid };
    }
    return null;
}

/// Free every duped label string plus the backing list. Caller must not hold
/// the registry lock unless it owns `labels` exclusively.
fn freeLabelList(labels: *std.ArrayListUnmanaged([]const u8)) void {
    for (labels.items) |l| allocator.free(l);
    labels.deinit(allocator);
}

const EnumPublishResult = enum { appended, updated, oom };

/// Publish (or update) an enum entry into the registry under the EXCLUSIVE
/// lock. Takes ownership of `labels_in`: on the append/update path the list is
/// stored (and any old labels freed); on OOM it is freed. Because readers hold
/// the shared lock for the whole scan, no reader can observe a torn `.items`
/// slice, a reallocated buffer, or freed label bytes.
fn publishEnumEntry(oid: i32, array_oid: i32, labels_in: std.ArrayListUnmanaged([]const u8)) EnumPublishResult {
    var labels = labels_in;
    enum_registry_lock.lock();
    defer enum_registry_lock.unlock();
    for (enum_registry.items) |*entry| {
        if (entry.oid == oid) {
            // Re-registration (e.g. ALTER TYPE ADD VALUE) — free old labels,
            // swap in the fresh list. Safe: exclusive lock excludes readers.
            freeLabelList(&entry.labels);
            entry.labels = labels;
            entry.array_oid = array_oid;
            return .updated;
        }
    }
    enum_registry.append(allocator, .{ .oid = oid, .array_oid = array_oid, .labels = labels }) catch {
        freeLabelList(&labels);
        return .oom;
    };
    return .appended;
}

/// _db_register_hstore(pool_handle) — query pg_type for hstore OID and register it
pub fn db_register_hstore(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &pool_handle) == 0) return null;

    // Query the hstore OID from pg_type using our existing db_query path
    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("register_hstore: no connection available", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    const sql = "SELECT oid::integer FROM pg_type WHERE typname = 'hstore'";
    var result = conn.query(sql, .{}) catch {
        // hstore extension not installed — not an error
        return py.newInt(0);
    };
    defer result.deinit();

    var oid_val: i32 = 0;
    if (result.next() catch null) |row| {
        oid_val = row.get(i32, 0) catch 0;
        hstore_oid = oid_val;
        trace("db_register_hstore: oid={d}", .{oid_val});
    }
    // Drain remaining rows so connection returns to idle state
    result.drain() catch {};

    return py.newInt(@as(i64, oid_val));
}

/// _db_register_vector(pool_handle) — query pg_type for pgvector OID and register it.
/// Sets pg.types.Vector.oid_decimal so binary vector results decode via SIMD.
/// Returns OID on success, 0 if pgvector extension not installed.
pub fn db_register_vector(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &pool_handle) == 0) return null;

    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("register_vector: no connection available", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    const sql = "SELECT oid::integer FROM pg_type WHERE typname = 'vector'";
    var result = conn.query(sql, .{}) catch {
        // pgvector extension not installed — not an error
        return py.newInt(0);
    };
    defer result.deinit();

    var oid_val: i32 = 0;
    if (result.next() catch null) |row| {
        oid_val = row.get(i32, 0) catch 0;
        pg.types.Vector.oid_decimal = oid_val;
        trace("db_register_vector: oid={d}", .{oid_val});
    }
    result.drain() catch {};

    return py.newInt(@as(i64, oid_val));
}

/// _db_register_enum(pool_handle, type_name) — query pg_type + pg_enum for a custom enum type.
/// Returns the OID on success, 0 if type not found.
/// Re-registering an existing type updates its labels (safe for ALTER TYPE ADD VALUE).
pub fn db_register_enum(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var type_name: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ls", &pool_handle, &type_name) == 0) return null;

    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("register_enum: no connection available", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    const tname = std.mem.span(type_name);
    trace("db_register_enum: type='{s}' pool_handle={d} conn_state={}", .{ tname, pool_handle, conn._state });

    // 1. Get the enum's OID and its array type OID from pg_type
    const oid_sql = "SELECT t.oid::integer, COALESCE(t.typarray::integer, 0) FROM pg_type t WHERE t.typname = $1 AND t.typtype = 'e'";
    var enum_oid: i32 = 0;
    var array_oid: i32 = 0;
    {
        var oid_result = conn.query(oid_sql, .{tname}) catch |err| {
            trace("db_register_enum: pg_type query failed: {}", .{err});
            return py.newInt(0);
        };

        const oid_row = (oid_result.next() catch |err| {
            trace("db_register_enum: next() failed: {}", .{err});
            oid_result.deinit();
            return py.newInt(0);
        }) orelse {
            trace("db_register_enum: type '{s}' not found in pg_type", .{tname});
            oid_result.deinit();
            return py.newInt(0);
        };
        enum_oid = oid_row.get(i32, 0) catch {
            oid_result.deinit();
            return py.newInt(0);
        };
        array_oid = oid_row.get(i32, 1) catch 0;

        // Drain remaining rows to ensure result is fully consumed
        while (oid_result.next() catch null) |_| {}
        oid_result.deinit();
    }

    trace("db_register_enum: enum_oid={d} array_oid={d} conn_state={}", .{ enum_oid, array_oid, conn._state });

    // 2. Get all enum labels in sort order
    // Build query with OID inline (simple query — no parameter binding type issues)
    var label_sql_buf: [128]u8 = undefined;
    const label_sql = std.fmt.bufPrint(&label_sql_buf, "SELECT enumlabel FROM pg_enum WHERE enumtypid = {d} ORDER BY enumsortorder", .{enum_oid}) catch return py.newInt(0);

    var label_result = conn.query(label_sql, .{}) catch |err| {
        trace("db_register_enum: pg_enum query failed: {} conn_state={}", .{ err, conn._state });
        py.setError("register_enum: failed to query pg_enum for type '{s}'", .{tname});
        return null;
    };
    defer label_result.deinit();

    // Build label list
    var labels: std.ArrayListUnmanaged([]const u8) = .empty;

    while (label_result.next() catch null) |row| {
        const label = row.get([]const u8, 0) catch continue;
        const duped = allocator.dupe(u8, label) catch continue;
        labels.append(allocator, duped) catch {
            allocator.free(duped);
            continue;
        };
    }
    const label_count = labels.items.len;

    // 3 + 4. Publish under the exclusive registry lock. publishEnumEntry takes
    // ownership of `labels` (stored on success, freed on OOM), and its
    // check-then-update/append is atomic w.r.t. concurrent readers/writers.
    switch (publishEnumEntry(enum_oid, array_oid, labels)) {
        .oom => {
            py.setError("register_enum: out of memory", .{});
            return null;
        },
        .updated => {
            trace("db_register_enum: updated type='{s}' oid={d} array_oid={d} labels={d}", .{ tname, enum_oid, array_oid, label_count });
            return py.newInt(@as(i64, enum_oid));
        },
        .appended => {
            trace("db_register_enum: registered type='{s}' oid={d} array_oid={d} labels={d}", .{ tname, enum_oid, array_oid, label_count });
            return py.newInt(@as(i64, enum_oid));
        },
    }
}

/// _db_list_enums(pool_handle) — discover and auto-register ALL enum types in the database.
/// Returns a Python dict: {type_name: [label1, label2, ...]}
pub fn db_list_enums(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &pool_handle) == 0) return null;

    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("list_enums: no connection available", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Query all enum types with their labels in one shot
    const sql =
        \\SELECT t.oid::integer, t.typname, COALESCE(t.typarray::integer, 0),
        \\       array_agg(e.enumlabel ORDER BY e.enumsortorder)::text
        \\FROM pg_type t
        \\JOIN pg_enum e ON e.enumtypid = t.oid
        \\WHERE t.typtype = 'e'
        \\GROUP BY t.oid, t.typname
        \\ORDER BY t.typname
    ;

    var result = conn.query(sql, .{}) catch {
        py.setError("list_enums: failed to query enum types", .{});
        return null;
    };
    defer result.deinit();

    const py_dict = c.PyDict_New() orelse return null;

    while (result.next() catch null) |row| {
        const enum_oid = row.get(i32, 0) catch continue;
        const type_name = row.get([]const u8, 1) catch continue;
        const array_oid = row.get(i32, 2) catch 0;
        const labels_text = row.get([]const u8, 3) catch continue;

        // Parse PostgreSQL array text format: {label1,label2,label3}
        const py_labels = parseEnumArrayText(labels_text) orelse continue;

        const py_key = py.newString(type_name) orelse {
            c.Py_DecRef(py_labels);
            continue;
        };
        _ = c.PyDict_SetItem(py_dict, py_key, py_labels);
        c.Py_DecRef(py_key);
        c.Py_DecRef(py_labels);

        // Auto-register each discovered enum
        autoRegisterEnum(enum_oid, array_oid, labels_text);
    }

    return py_dict;
}

/// Parse PostgreSQL text array format {a,b,c} into Python list of strings.
fn parseEnumArrayText(text: []const u8) ?*c.PyObject {
    if (text.len < 2 or text[0] != '{' or text[text.len - 1] != '}') return null;
    const inner = text[1 .. text.len - 1];

    // Count labels first
    if (inner.len == 0) return c.PyList_New(0);

    var count: c.Py_ssize_t = 1;
    for (inner) |ch| {
        if (ch == ',') count += 1;
    }

    const py_list = c.PyList_New(count) orelse return null;
    var idx: c.Py_ssize_t = 0;
    var start: usize = 0;

    for (inner, 0..) |ch, i| {
        if (ch == ',') {
            const label = inner[start..i];
            const py_str = py.newString(label) orelse py.pyNone();
            _ = c.PyList_SetItem(py_list, idx, py_str);
            idx += 1;
            start = i + 1;
        }
    }
    // Last label
    const label = inner[start..];
    const py_str = py.newString(label) orelse py.pyNone();
    _ = c.PyList_SetItem(py_list, idx, py_str);

    return py_list;
}

/// Auto-register an enum OID from list_enums discovery. Idempotent: a repeat
/// discovery of an already-registered OID refreshes its labels (via
/// publishEnumEntry's update path) rather than duplicating the entry.
fn autoRegisterEnum(enum_oid: i32, array_oid: i32, labels_text: []const u8) void {
    if (labels_text.len < 2 or labels_text[0] != '{' or labels_text[labels_text.len - 1] != '}') return;
    const inner = labels_text[1 .. labels_text.len - 1];
    if (inner.len == 0) return;

    // Build the label list first — allocation/parse happens WITHOUT the
    // registry lock held; only the publish step below takes the lock.
    var labels: std.ArrayListUnmanaged([]const u8) = .empty;
    var start: usize = 0;
    for (inner, 0..) |ch, i| {
        if (ch == ',') {
            const duped = allocator.dupe(u8, inner[start..i]) catch {
                freeLabelList(&labels);
                return;
            };
            labels.append(allocator, duped) catch {
                allocator.free(duped);
                freeLabelList(&labels);
                return;
            };
            start = i + 1;
        }
    }
    const duped = allocator.dupe(u8, inner[start..]) catch {
        freeLabelList(&labels);
        return;
    };
    labels.append(allocator, duped) catch {
        allocator.free(duped);
        freeLabelList(&labels);
        return;
    };

    // Publish under the exclusive lock (consumes `labels`).
    _ = publishEnumEntry(enum_oid, array_oid, labels);
}

// Prepared statement name cache: SQL text hash → "hd_N" name string.
// pg.zig caches prepared statements per connection by name — subsequent calls
// with the same name skip Parse (query planning) and go straight to Bind+Execute.
// THREAD SAFETY: protected by prep_stmt_mutex. Required because
// Python 3.14t free-threaded mode has real OS threads competing concurrently.
// SIZE BOUND: evicts oldest entries when exceeding PREP_STMT_CACHE_MAX to prevent
// unbounded memory growth from dynamic SQL patterns.
const PREP_STMT_CACHE_MAX: usize = 4096;
var prep_stmt_cache: ?std.AutoHashMap(u64, []const u8) = null;
var prep_stmt_counter: std.atomic.Value(u32) = std.atomic.Value(u32).init(0);
var prep_stmt_mutex: py.Mutex = .{};

// Prepared statement cache statistics (atomic — readable without lock)
// Sharded (see metrics_py.ShardedCount): hits/misses are bumped by EVERY
// worker on EVERY statement — at high qps a single shared atomic here is a
// cross-core cache-line ping-pong on the query hot path. Scatter on write,
// gather on the stats read.
var prep_stmt_hits: metrics.ShardedCount = .{};
var prep_stmt_misses: metrics.ShardedCount = .{};
var prep_stmt_evictions: metrics.ShardedCount = .{};

/// Get the prepared statement cache. Caller MUST hold prep_stmt_mutex.
fn getPrepStmtCache() *std.AutoHashMap(u64, []const u8) {
    if (prep_stmt_cache) |*pc| return pc;
    prep_stmt_cache = std.AutoHashMap(u64, []const u8).init(allocator);
    return &prep_stmt_cache.?;
}

/// Clear the prepared statement name cache.
/// Must be called after DDL operations (ALTER, CREATE, DROP) that change
/// table schemas, because cached prepared statements have stale result types.
pub fn db_clear_stmt_cache(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    // Lock both mutexes to safely clear caches under concurrent access.
    // NOTE: Do NOT reset prep_stmt_counter. The counter must be monotonically
    // increasing because prepared statements with old names still exist on the
    // PostgreSQL connection. If we restart at 0, getPreparedName generates "hd_0"
    // again, pg.zig sees "hd_0" already exists on the connection, skips Parse,
    // and executes the WRONG cached query.
    prep_stmt_mutex.lock();
    defer prep_stmt_mutex.unlock();

    if (prep_stmt_cache) |*cache| {
        var it = cache.iterator();
        while (it.next()) |entry| {
            allocator.free(entry.value_ptr.*);
        }
        cache.clearAndFree();
    }

    // Also clear column metadata cache since column types/names may have changed
    column_cache_mutex.lock();
    defer column_cache_mutex.unlock();

    if (column_cache) |*cc| {
        var cit = cc.iterator();
        while (cit.next()) |entry| {
            c.Py_DecRef(entry.value_ptr.*);
        }
        cc.clearAndFree();
    }
    // last_columns is threadlocal — only clear this thread's copy
    if (last_columns) |old| {
        c.Py_DecRef(old);
        last_columns = null;
    }

    return py.pyNone();
}

/// _db_stmt_cache_stats() -> dict with {hits, misses, evictions, entries, max_entries}
/// Returns current prepared statement cache statistics. All counters are cumulative.
pub fn db_stmt_cache_stats(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const hits = prep_stmt_hits.total();
    const misses = prep_stmt_misses.total();
    const evictions = prep_stmt_evictions.total();

    // Get current entry count under lock
    prep_stmt_mutex.lock();
    const entries: u64 = if (prep_stmt_cache) |*cache_| cache_.count() else 0;
    prep_stmt_mutex.unlock();

    const dict = c.PyDict_New() orelse return null;
    inline for (.{
        .{ "hits", hits },
        .{ "misses", misses },
        .{ "evictions", evictions },
        .{ "entries", entries },
        .{ "max_entries", @as(u64, PREP_STMT_CACHE_MAX) },
    }) |item| {
        const val = c.PyLong_FromUnsignedLongLong(item[1]) orelse {
            c.Py_DecRef(dict);
            return null;
        };
        if (c.PyDict_SetItemString(dict, item[0], val) < 0) {
            c.Py_DecRef(val);
            c.Py_DecRef(dict);
            return null;
        }
        c.Py_DecRef(val);
    }
    return dict;
}

/// _db_reset_stmt_cache_stats() -> None — reset hit/miss/eviction counters to zero.
pub fn db_reset_stmt_cache_stats(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    prep_stmt_hits.reset();
    prep_stmt_misses.reset();
    prep_stmt_evictions.reset();
    return py.pyNone();
}

fn getPreparedName(sql: []const u8) []const u8 {
    const hash = hashSql(sql);

    // Lock protects both cache lookup and insert — prevents concurrent
    // HashMap access that causes SafetyLock panic under free-threading.
    prep_stmt_mutex.lock();
    defer prep_stmt_mutex.unlock();

    const cache = getPrepStmtCache();
    if (cache.get(hash)) |name| {
        prep_stmt_hits.add(1);
        return name;
    }
    prep_stmt_misses.add(1);

    // Evict ~25% of entries when cache is full to prevent unbounded growth.
    // Uses clearAndFree which is O(1) — simpler and faster than selective eviction.
    // The prep_stmt_counter is NOT reset (monotonic), so new names don't collide
    // with prepared statements still cached on PostgreSQL connections.
    if (cache.count() >= PREP_STMT_CACHE_MAX) {
        trace("getPreparedName: cache full ({d} entries), evicting all", .{cache.count()});
        const evicted_count = cache.count();
        var it = cache.iterator();
        while (it.next()) |entry| {
            allocator.free(entry.value_ptr.*);
        }
        cache.clearAndFree();
        prep_stmt_evictions.add(evicted_count);
    }

    // Generate new name "hd_0", "hd_1", etc.
    // Atomic fetch_add ensures unique names even if lock is released between calls.
    const counter = prep_stmt_counter.fetchAdd(1, .monotonic);
    const name = std.fmt.allocPrint(allocator, "hd_{d}", .{counter}) catch return "";
    cache.put(hash, name) catch return "";
    return name;
}

/// _db_warmup_statements(pool_handle, sql_list) — pre-parse SQL statements to prime the cache.
/// First execution of each SQL does Parse+Describe round-trip with PostgreSQL.
/// Subsequent queries using these SQL strings skip Parse entirely (33% faster).
pub fn db_warmup_statements(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var sql_list: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lO!", &pool_handle, &c.PyList_Type, &sql_list) == 0) return null;

    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("warmup_statements: no connection available", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    const list = sql_list.?;
    const n: usize = @intCast(c.PyList_Size(list));
    var warmed: i64 = 0;

    for (0..n) |i| {
        const item = c.PyList_GetItem(list, @as(c.Py_ssize_t, @intCast(i))) orelse continue;
        if (c.PyUnicode_Check(item) == 0) continue;
        var slen: c.Py_ssize_t = 0;
        const sptr = c.PyUnicode_AsUTF8AndSize(item, &slen) orelse continue;
        const sql = sptr[0..@intCast(slen)];

        // Generate a cache name for this SQL — registers in our name map
        const cache_name = getPreparedName(sql);
        if (cache_name.len == 0) continue;

        // Execute a lightweight query to trigger Parse+Describe caching on the connection.
        // Use queryOpts with cache_name to prime pg.zig's internal prepared statement cache.
        var result = conn.queryOpts(sql, .{}, .{ .cache_name = cache_name }) catch continue;
        // Drain the result (we don't care about the data, just the Parse+Describe).
        // On a mid-stream error, recover the connection so it isn't left wedged
        // in .query for the next warmup/query (F10).
        while (true) {
            _ = result.next() catch {
                recoverAfterRowError(result, conn);
                break;
            } orelse break;
        }
        result.deinit();
        warmed += 1;
    }

    trace("db_warmup_statements: warmed {d}/{d} statements", .{ warmed, n });
    return py.newInt(warmed);
}

fn getDbCacheMap() *std.StringHashMap(CacheEntry) {
    if (db_cache) |*dc| return dc;
    db_cache = std.StringHashMap(CacheEntry).init(allocator);
    return &db_cache.?;
}

// A2#5: db_routes_map is a lazily-initialized optional-of-struct — it cannot be
// atomically loaded, so a check-then-act (`if (map) |m|` then assign) racing with
// itself would double-init (leaking one map + tearing the optional) under GIL-off.
// getDbCacheMap above is safe only because every caller already holds
// db_cache_mutex; getDbRoutes has no such umbrella, so guard the init here. The
// lock is startup-only (route registration + one-time buildDispatchMap freeze),
// so it never touches the per-request serving path (that uses the frozen
// dispatch_map, not getDbRoutes). Unconditional lock, not DCL: the fast-path
// check would itself be a data race on the non-atomic optional. Once created the
// map is never reset to null, so the returned pointer stays stable after unlock.
var db_routes_mutex: py.Mutex = .{};
pub fn getDbRoutes() *std.StringHashMap(DbRouteEntry) {
    db_routes_mutex.lock();
    defer db_routes_mutex.unlock();
    if (db_routes_map) |*m| return m;
    db_routes_map = std.StringHashMap(DbRouteEntry).init(allocator);
    return &db_routes_map.?;
}

fn now() i64 {
    return py.timestamp();
}

/// Thread-safe cache lookup. Returns an OWNED copy of the cached body (the
/// caller must free it) or null on miss/expired. The copy is made while the
/// lock is held: returning the interior `entry.body` slice and using it after
/// the lock drops is a use-after-free, because another thread can evict/
/// invalidate + free that entry before the caller finishes sending it (F12).
fn cacheGet(key: []const u8) ?[]u8 {
    if (!db_cache_enabled) return null;
    db_cache_mutex.lock();
    defer db_cache_mutex.unlock();

    const cache = getDbCacheMap();
    if (cache.get(key)) |entry| {
        // TTL check
        if (db_cache_ttl > 0 and (now() - entry.created_at) > db_cache_ttl) {
            // Expired — remove it, freeing key, body AND table (the table dupe
            // was leaked on this path before) and correcting the byte accounting.
            if (cache.fetchRemove(key)) |removed| {
                db_cache_total_bytes -|= removed.value.body.len;
                allocator.free(@constCast(removed.key));
                allocator.free(@constCast(removed.value.body));
                allocator.free(@constCast(removed.value.table));
                db_cache_count -|= 1;
            }
            return null;
        }
        return allocator.dupe(u8, entry.body) catch null;
    }
    return null;
}

/// Thread-safe cache put. Evicts oldest entries if full.
fn cachePut(key: []const u8, body: []const u8, table: []const u8) void {
    if (!db_cache_enabled) return;
    db_cache_mutex.lock();
    defer db_cache_mutex.unlock();

    // Reject entries that are individually too large (>16MB single entry)
    if (body.len > 16 * 1024 * 1024) return;

    // LRU eviction: if full by count or total bytes, remove ~10% oldest entries
    if (db_cache_count >= DB_CACHE_MAX or db_cache_total_bytes >= DB_CACHE_MAX_BYTES) {
        evictOldest(DB_CACHE_MAX / 10);
    }

    const key_dupe = allocator.dupe(u8, key) catch return;
    const body_dupe = allocator.dupe(u8, body) catch {
        allocator.free(key_dupe);
        return;
    };
    const table_dupe = allocator.dupe(u8, table) catch {
        allocator.free(key_dupe);
        allocator.free(body_dupe);
        return;
    };

    const cache = getDbCacheMap();
    // If key already exists, free old value and subtract its size
    if (cache.fetchRemove(key_dupe)) |old| {
        db_cache_total_bytes -|= old.value.body.len;
        allocator.free(@constCast(old.key));
        allocator.free(@constCast(old.value.body));
        allocator.free(@constCast(old.value.table));
        db_cache_count -|= 1;
    }

    cache.put(key_dupe, .{
        .body = body_dupe,
        .table = table_dupe,
        .created_at = now(),
    }) catch {
        allocator.free(key_dupe);
        allocator.free(body_dupe);
        allocator.free(table_dupe);
        return;
    };
    db_cache_count += 1;
    db_cache_total_bytes += body_dupe.len;
}

/// Per-table invalidation — clears ALL entries belonging to the specified table.
/// Batches in groups of 256 to avoid unbounded stack allocation while handling
/// tables with any number of cached entries.
fn invalidateTableCache(table: []const u8) void {
    db_cache_mutex.lock();
    defer db_cache_mutex.unlock();

    const cache = getDbCacheMap();

    // Repeat until all matching entries are removed
    while (true) {
        var keys_to_remove: [256][]const u8 = undefined;
        var remove_count: usize = 0;

        var it = cache.iterator();
        while (it.next()) |entry| {
            if (std.mem.eql(u8, entry.value_ptr.table, table) or table.len == 0) {
                keys_to_remove[remove_count] = entry.key_ptr.*;
                remove_count += 1;
                if (remove_count >= 256) break; // batch full
            }
        }

        if (remove_count == 0) break; // done — no more matches

        for (keys_to_remove[0..remove_count]) |key| {
            if (cache.fetchRemove(key)) |removed| {
                db_cache_total_bytes -|= removed.value.body.len;
                allocator.free(@constCast(removed.key));
                allocator.free(@constCast(removed.value.body));
                allocator.free(@constCast(removed.value.table));
                db_cache_count -|= 1;
            }
        }
    }
}

/// Evict N oldest entries (approximate LRU)
fn evictOldest(count: usize) void {
    // Already holding mutex from caller
    const cache = getDbCacheMap();
    var oldest_keys: [256][]const u8 = undefined;
    var oldest_times: [256]i64 = undefined;
    var oldest_count: usize = 0;
    const max_evict = @min(count, 256);

    var it = cache.iterator();
    while (it.next()) |entry| {
        const age = entry.value_ptr.created_at;
        if (oldest_count < max_evict) {
            oldest_keys[oldest_count] = entry.key_ptr.*;
            oldest_times[oldest_count] = age;
            oldest_count += 1;
        } else {
            // Replace the newest in our evict list if this one is older
            var newest_idx: usize = 0;
            for (0..oldest_count) |j| {
                if (oldest_times[j] > oldest_times[newest_idx]) newest_idx = j;
            }
            if (age < oldest_times[newest_idx]) {
                oldest_keys[newest_idx] = entry.key_ptr.*;
                oldest_times[newest_idx] = age;
            }
        }
    }

    for (oldest_keys[0..oldest_count]) |key| {
        if (cache.fetchRemove(key)) |removed| {
            db_cache_total_bytes -|= removed.value.body.len;
            allocator.free(@constCast(removed.key));
            allocator.free(@constCast(removed.value.body));
            allocator.free(@constCast(removed.value.table));
            db_cache_count -|= 1;
        }
    }
}

/// Set Python error with full PostgreSQL error details from connection
/// Recover a connection after Result.next() fails mid-stream (F10). A bare
/// `next() catch null` is indistinguishable from clean end-of-rows, so callers
/// used to return a SILENTLY TRUNCATED row set as success — and worse, left
/// _state=.query with a trailing ReadyForQuery undrained, wedging pinned/
/// thread-owned connections (every later query → ConnectionBusy). Drain the
/// remaining protocol traffic back to ReadyForQuery so the connection is
/// reusable; if the wire is too broken to drain, mark it failed so the pool
/// discards it on release instead of handing back a desynced connection.
/// Callers must SURFACE the error (setPgError + return) rather than continue.
fn recoverAfterRowError(result: *pg.Result, conn: *pg.Conn) void {
    result.drain() catch {
        conn._state = .fail;
    };
}

fn setPgError(conn: *pg.Conn, prefix: []const u8, sql: []const u8) void {
    if (conn.err) |err| {
        py.setError("{s}: {s} [SQL: {s}]", .{
            prefix,
            if (err.message.len > 0) err.message else "unknown error",
            sql[0..@min(sql.len, 200)],
        });
    } else {
        py.setError("{s} [SQL: {s}]", .{ prefix, sql[0..@min(sql.len, 200)] });
    }
}

/// Acquire a Postgres connection.
/// If a pinned connection is active (transaction mode), uses that.
/// Otherwise: per-thread conn → pool.
/// Acquire a connection by handle.
///
/// Handle encoding:
///   >= 0: pool registry index — acquires from that pool
///   -1:   legacy fallback — uses first non-null pool
///   < -1: pinned connection — handle = -(pinned_slot + 2), returns pinned conn
///         e.g. -2 = pinned slot 0, -3 = pinned slot 1
///
/// This is the correct architecture: each call specifies which pool/connection
/// to use. No global mutable state needed for Django's connection management.
const AcquireResult = struct {
    conn: *pg.Conn,
    should_release: bool, // false for pinned connections
    // Set when the connection is thread-owned: the slot is BUSY for the
    // duration of the operation and must be idled again via releaseAcquired.
    slot: ?*ThreadOwnedSlot = null,
};

/// End an acquireConn/acquireConnByHandle operation. EVERY caller must defer
/// this: it either returns a pool-path connection, or marks a thread-owned
/// slot idle again so db_close_pool's reaper can safely reclaim it (a busy
/// slot is mid-op and is left for the owner's lazy cleanup instead). Pinned
/// connections pass through untouched — their release is the explicit
/// _db_conn_release call.
fn releaseAcquired(acq: AcquireResult) void {
    if (acq.should_release) {
        acq.conn.release();
    } else if (acq.slot) |slot| {
        slot.busy.store(false, .release);
    }
}

fn acquireConnByHandle(handle: i64) ?AcquireResult {
    if (handle < -1) {
        // Pinned connection: handle = -(slot + 2). Per-query hot path — a single
        // atomic load off a non-reallocating array (see pinned_slots).
        const slot: usize = @intCast(-(handle + 2));
        if (pinnedGet(slot)) |conn| {
            trace("acquireConnByHandle: pinned slot={d} conn={x}", .{ slot, @intFromPtr(conn) });
            return .{ .conn = conn, .should_release = false };
        }
        trace("acquireConnByHandle: pinned slot={d} is NULL", .{slot});
        return null;
    }

    // ── FAST PATH: Thread-owned connection (NO MUTEX) ──
    // Check if this thread already owns a connection for this pool.
    // This is the zero-contention path for repeat queries under free-threading.
    // Skipped entirely for DB-offload worker threads (see `offload_worker`): they
    // must acquire/release per op, never pin a connection.
    if (!offload_worker) {
        if (tryThreadOwned(handle)) |slot| {
            const conn = slot.conn.?;
            trace("acquireConnByHandle: thread-owned conn={x}", .{@intFromPtr(conn)});
            return .{ .conn = conn, .should_release = false, .slot = slot };
        }
    }

    // ── SLOW PATH: Acquire from pool (MUTEX) ──
    const pool = retainPoolByHandle(handle, .fallback_to_active) orelse {
        trace("acquireConnByHandle: NO POOL for handle={d}", .{handle});
        return null;
    };
    // Transient resolution reference: keeps the pool alive across the checks
    // and the (possibly waiting) pool.acquire below. A successful checkout
    // holds its own reference, so dropping this at every exit is correct.
    defer pool.unref();

    // Task #200: fast-fail undersized-pool detection.
    //
    // If every connection in the pool is already owned by a thread_slot
    // that belongs to this pool_handle, and NONE of those slots is the
    // current thread (we already checked tryThreadOwned above), then
    // pool.acquire would block in timedWait until the pool's 10 s
    // timeout — with NO wakeup path, because slot-holding threads never
    // release their conn. This is the pathological regime documented
    // in logs/profile_pool_queue_depth_report.md. Return fast with an
    // actionable error so the user sees the real problem instead of
    // an opaque "Query failed" after 10 s of apparent hang.
    const thread_owned_here = countThreadOwnedForPool(handle);
    if (thread_owned_here >= pool._conns.len) {
        trace("acquireConnByHandle: UNDERSIZED pool handle={d} thread_owned={d} pool_size={d}", .{
            handle, thread_owned_here, pool._conns.len,
        });
        py.setError(
            "Pool undersized: all {d} connections are permanently owned by other worker threads (pool_size={d}, {d} threads are holding all slots). Raise HYPER_POOL_SIZE to at least thread_pool_size + 8 (see docs/patterns.md 'Lazy-init per-instance primitives' and the thread-owned slot architecture notes in logs/profile_pool_queue_depth_report.md).",
            .{ pool._conns.len, pool._conns.len, thread_owned_here },
        );
        return null;
    }

    const conn = pool.acquire() catch |err| {
        trace("acquireConnByHandle: acquire FAILED for handle={d}: {} pool_avail={d} pool_missing={d} pool_size={d}", .{
            handle,
            err,
            pool._available,
            pool._missing,
            pool._conns.len,
        });
        return null;
    };

    // Try to claim a thread-owned slot for next time (avoid mutex on repeat
    // queries). Offload worker threads never claim: they release per op so the
    // connection returns to the pool (with session reset) after every query.
    if (!offload_worker) {
        if (claimThreadSlot(handle, conn)) |slot| {
            trace("acquireConnByHandle: claimed thread slot for handle={d} conn={x}", .{ handle, @intFromPtr(conn) });
            return .{ .conn = conn, .should_release = false, .slot = slot }; // Owned by thread now
        }
    }

    // All slots taken — use regular pool acquire/release path
    trace("acquireConnByHandle: pool handle={d} conn={x}", .{ handle, @intFromPtr(conn) });
    return .{ .conn = conn, .should_release = true };
}

// ── Thread-owned connection slots ────────────────────────────────────────────
// Eliminates pool mutex contention for repeat queries on the same thread.
//
// Design: Fixed array of 64 slots. Each slot has an atomic owner_tid and an
// atomic `busy` flag marking an operation in progress on the slot's conn.
// Fast path (NO MUTEX): scan for owner_tid == my_tid → win `busy` → reuse.
// Slow path (first query): atomic CAS to claim empty slot → acquire from pool.
// Cleanup: _db_release_thread_conn() releases the slot back explicitly;
// db_close_pool reaps IDLE slots (busy-CAS win = provably not mid-op) and
// leaves busy ones to the owner, which sees the pool marked closing on its
// next use and releases its own connection (lazy, steal-free — reclaiming a
// mid-op conn would hand one PG socket to two threads).
//
// This is the critical optimization for Python 3.14t free-threaded mode where
// real OS threads compete for the pool mutex on every query.

// The Python DB offload executor (serving a MULTIPLEXING loop — shared WS pool /
// reactor) marks its worker threads via `_db_mark_offload_worker`. Such a thread
// must NOT retain a thread-owned connection. Unlike an HTTP worker thread (one
// logical flow for its whole lifetime, so pinning one connection to it is a
// bounded, intentional working set), an offload worker runs arbitrary UNRELATED
// tasks' queries serially. Pinning a connection to it would (a) never return that
// connection to the pool while the loop is idle — permanently inflating pool
// `in_use` and starving every other pool consumer of a connection that is doing
// no work — and (b) reuse one connection across unrelated tasks WITHOUT the pool's
// per-release session reset, leaking SET / search_path / cursor / LISTEN state
// between them (a cross-task, potentially cross-tenant, session-state hazard).
// An offload worker therefore acquires a pool connection per op and releases it
// right back (`should_release = true`, honored by every acquireConnByHandle
// caller's `defer`). Defaults false, so HTTP worker threads and every other
// caller keep the zero-mutex thread-owned fast path unchanged.
threadlocal var offload_worker: bool = false;

const MAX_THREAD_SLOTS = 64;

const ThreadOwnedSlot = struct {
    owner_tid: std.atomic.Value(u64), // 0 = unowned
    // Operation-in-progress flag. Won by CAS(false→true) from exactly one of:
    // the owner starting an op (tryThreadOwned / claimThreadSlot), the owner's
    // explicit release (releaseThreadSlot), or the close reaper. Whoever holds
    // it has exclusive access to `conn` and the slot's lifecycle, which is
    // what makes the plain `conn` field safe to touch cross-thread.
    busy: std.atomic.Value(bool),
    // Only ever accessed under a won `busy` claim (see above). pool_handle is
    // additionally read cross-thread WITHOUT the claim by
    // countThreadOwnedForPool, so it must be atomic. (A2#3)
    conn: ?*pg.Conn,
    pool_handle: std.atomic.Value(i64),
};

var thread_slots: [MAX_THREAD_SLOTS]ThreadOwnedSlot = init: {
    var slots: [MAX_THREAD_SLOTS]ThreadOwnedSlot = undefined;
    for (&slots) |*s| {
        s.* = .{
            .owner_tid = std.atomic.Value(u64).init(0),
            .busy = std.atomic.Value(bool).init(false),
            .conn = null,
            .pool_handle = std.atomic.Value(i64).init(-1),
        };
    }
    break :init slots;
};

/// Try to get this thread's owned connection for a pool handle (atomics only,
/// no mutex). On success the slot is returned BUSY (op in progress) — the
/// caller MUST end the op via releaseAcquired so the slot goes idle again.
/// If the slot's pool is closing, the owner releases its own connection here
/// (lazy cleanup: the close path never steals a possibly-mid-op connection)
/// and returns null so the caller acquires from the current pool instead.
fn tryThreadOwned(pool_handle: i64) ?*ThreadOwnedSlot {
    const tid = std.Thread.getCurrentId();
    for (&thread_slots) |*slot| {
        if (slot.owner_tid.load(.acquire) != tid or slot.pool_handle.load(.acquire) != pool_handle) continue;
        // Win the op window; losing means the close reaper (or a transient
        // claim probe) holds the slot right now — treat as no slot.
        if (slot.busy.cmpxchgStrong(false, true, .acq_rel, .acquire) != null) continue;
        // Revalidate under the claim: the reaper may have recycled the slot
        // (and another thread re-claimed it) between the scan and the CAS.
        if (slot.owner_tid.load(.acquire) != tid or slot.pool_handle.load(.acquire) != pool_handle or slot.conn == null) {
            slot.busy.store(false, .release);
            continue;
        }
        const conn = slot.conn.?;
        // Safe deref even after db_close_pool: this slot's checkout holds a
        // pool reference, so the Pool memory outlives the slot's connection.
        if (conn._pool.?.isClosing()) {
            conn.release();
            slot.conn = null;
            slot.pool_handle.store(-1, .release);
            slot.owner_tid.store(0, .release);
            slot.busy.store(false, .release);
            return null;
        }
        return slot;
    }
    return null;
}

/// Count thread_slots currently owned by a specific pool_handle.
/// Used by acquireConnByHandle's undersized-pool fast-fail check
/// (task #200) — if every connection in the pool is owned by a
/// permanent thread slot AND the current thread isn't one of them,
/// calling pool.acquire would block forever in timedWait.
fn countThreadOwnedForPool(pool_handle: i64) usize {
    var count: usize = 0;
    for (&thread_slots) |*slot| {
        if (slot.owner_tid.load(.acquire) != 0 and slot.pool_handle.load(.acquire) == pool_handle) {
            count += 1;
        }
    }
    return count;
}

/// Claim an empty slot for the current thread's freshly-acquired connection.
/// Returns the slot BUSY (the claiming op is in progress — releaseAcquired
/// idles it), or null if all slots are taken (caller uses the pool path).
fn claimThreadSlot(pool_handle: i64, conn: *pg.Conn) ?*ThreadOwnedSlot {
    const tid = std.Thread.getCurrentId();
    for (&thread_slots) |*slot| {
        if (slot.owner_tid.load(.acquire) != 0) continue; // owned — skip fast
        // Win the op window FIRST: once we hold `busy`, neither the close
        // reaper nor a laggard owner-side probe can touch the slot while we
        // publish ownership and start the op.
        if (slot.busy.cmpxchgStrong(false, true, .acq_rel, .acquire) != null) continue;
        if (slot.owner_tid.cmpxchgStrong(0, tid, .acq_rel, .acquire)) |_| {
            // Someone owns it after all (raced between load and claim) —
            // give the window back and keep scanning.
            slot.busy.store(false, .release);
            continue;
        }
        slot.conn = conn;
        slot.pool_handle.store(pool_handle, .release);
        return slot;
    }
    return null; // All slots taken
}

/// Release this thread's owned connection back to the pool (explicit
/// _db_release_thread_conn). If the close reaper holds the slot right now,
/// the cleanup is already happening — nothing to do.
fn releaseThreadSlot(tid: u64, pool_handle: i64) void {
    for (&thread_slots) |*slot| {
        if (slot.owner_tid.load(.acquire) != tid or slot.pool_handle.load(.acquire) != pool_handle) continue;
        if (slot.busy.cmpxchgStrong(false, true, .acq_rel, .acquire) != null) continue;
        if (slot.owner_tid.load(.acquire) == tid and slot.pool_handle.load(.acquire) == pool_handle) {
            if (slot.conn) |conn| {
                conn.release();
                slot.conn = null;
            }
            slot.pool_handle.store(-1, .release);
            slot.owner_tid.store(0, .release); // Make slot available
        }
        slot.busy.store(false, .release);
        return;
    }
}

/// db_close_pool: reclaim the closing pool's IDLE thread-owned connections.
/// Winning the busy CAS proves the owner is not mid-op, so returning the
/// connection is safe. A busy slot is left alone — its owner finds the pool
/// marked closing on its next use and releases the connection itself
/// (tryThreadOwned's lazy cleanup). Never steal a busy slot's connection:
/// parking it while its owner is mid-query hands one PG socket to two
/// threads (protocol corruption, observed as indefinite read hangs).
fn reapIdleThreadSlotsForPool(pool_handle: i64) void {
    for (&thread_slots) |*slot| {
        if (slot.pool_handle.load(.acquire) != pool_handle) continue;
        if (slot.busy.cmpxchgStrong(false, true, .acq_rel, .acquire) != null) continue; // mid-op → lazy cleanup
        // Revalidate under the claim (the owner may have released or lazily
        // cleaned the slot between the scan and the CAS).
        if (slot.pool_handle.load(.acquire) == pool_handle) {
            if (slot.conn) |conn| {
                conn.release();
                slot.conn = null;
            }
            slot.pool_handle.store(-1, .release);
            slot.owner_tid.store(0, .release);
        }
        slot.busy.store(false, .release);
    }
}

/// Legacy acquireConn for HTTP server routes (uses global active_pool_handle).
/// Returns the full AcquireResult so callers honor should_release: a pinned,
/// legacy-thread-owned, OR thread_slot-claimed connection must NOT be released
/// back to the pool (should_release=false). Previously this returned a bare
/// *pg.Conn, discarding the flag, and releaseConn() then unconditionally
/// released even thread_slot-owned connections — a request would hand a
/// still-referenced wire connection back to the pool, letting another thread
/// reuse it concurrently (protocol corruption + inflated _available). (F3)
fn acquireConn() ?AcquireResult {
    // A2#2: load each selection global exactly once via acquire so the branch
    // taken is coherent (no re-read tearing a >=0 check apart from the @intCast).
    const pinned = @atomicLoad(i64, &active_pinned_handle, .acquire);
    if (pinned >= 0) {
        const conn = pinnedGet(@intCast(pinned)) orelse return null;
        return .{ .conn = conn, .should_release = false };
    }
    if (@atomicLoad(bool, &use_thread_conns, .acquire)) {
        const tid = std.Thread.getCurrentId();
        const idx = tid % MAX_WORKERS;
        if (thread_conns[idx]) |conn| return .{ .conn = conn, .should_release = false };
    }
    return acquireConnByHandle(@atomicLoad(i64, &active_pool_handle, .acquire));
}

fn releaseConn(conn: *pg.Conn) void {
    if (@atomicLoad(i64, &active_pinned_handle, .acquire) >= 0) return;
    if (@atomicLoad(bool, &use_thread_conns, .acquire)) return;
    trace("releaseConn: conn={x} conn._pool={x}", .{ @intFromPtr(conn), @intFromPtr(if (conn._pool) |p| p else @as(*pg.Pool, undefined)) });
    conn.release();
}

// ── Pinned connections for transactions ─────────────────────────────────────
// Django's atomic() needs to hold a connection across multiple execute/query calls.
// Pinned connections are acquired once and released when the transaction ends.
//
// THREAD SAFETY (free-threaded 3.14t, GIL genuinely off):
// This is a fixed-capacity, NON-REALLOCATING atomic slot array — the same model
// as `thread_slots` above. The previous ArrayListUnmanaged was a real data race:
// concurrent `_transaction_multiplexed` (offloaded to the DB executor → truly
// parallel), `server_cursor` (inline per WS-loop thread), copy_from/to and
// migrations all mutated/read it under no lock, so an `.append` realloc could
// tear the `items` slice out from under a concurrent reader (UAF), and two
// threads scanning for a null slot could both claim it (lost slot / double
// handle). A fixed array never reallocates, so the per-query hot-path read
// (`pinnedGet` / the pinned branch of `acquireConnByHandle`) is a single atomic
// load that can never observe a moved backing store. Slot allocation is made
// atomic by CAS-claiming the `in_use` flag, so scan-then-assign is race-free.
//
// Capacity: a pinned connection holds a real pool connection for its whole
// lifetime, so the number of simultaneously-pinned connections is bounded by the
// sum of all pool sizes; pool.acquire() blocks/fails long before this cap is
// hit in any sane configuration. 1024 static slots (~16 KiB) is comfortably
// above any realistic pool sizing.
const MAX_PINNED_SLOTS = 1024;

const PinnedSlot = struct {
    // false = free. Claimed atomically via CAS(false→true); this is the sole
    // synchronization point that makes "find a free slot + assign" atomic.
    in_use: std.atomic.Value(bool),
    // Published with .release after claiming, read with .acquire on the hot
    // path, cleared with .release on release/close. Optional pointer is
    // pointer-sized so it is a valid atomic type.
    conn: std.atomic.Value(?*pg.Conn),
};

var pinned_slots: [MAX_PINNED_SLOTS]PinnedSlot = init: {
    // The comptime init loop runs MAX_PINNED_SLOTS iterations; raise the
    // backwards-branch quota above Zig's default 1000 so it evaluates.
    @setEvalBranchQuota(MAX_PINNED_SLOTS * 8);
    var slots: [MAX_PINNED_SLOTS]PinnedSlot = undefined;
    for (&slots) |*s| {
        s.* = .{
            .in_use = std.atomic.Value(bool).init(false),
            .conn = std.atomic.Value(?*pg.Conn).init(null),
        };
    }
    break :init slots;
};

/// Atomically claim the first free slot and publish `conn` into it. Returns the
/// slot index, or null if all slots are in use. Pure array mechanics — never
/// dereferences `conn`, never touches the pool (so it is unit-testable with a
/// fake pointer). CAS(false→true) on `in_use` is the sole synchronization point
/// that makes scan-then-assign atomic: only one thread can win a given slot.
fn pinnedClaimSlot(conn: *pg.Conn) ?usize {
    for (&pinned_slots, 0..) |*slot, i| {
        if (slot.in_use.cmpxchgStrong(false, true, .acq_rel, .acquire)) |_| {
            continue; // CAS failed — slot already claimed by another thread
        }
        // Won the slot. Publish the connection with release so a reader that
        // later loads it with acquire sees a fully-constructed *Conn.
        slot.conn.store(conn, .release);
        return i;
    }
    return null;
}

/// Atomically vacate a slot: null its connection, then free the slot for reuse,
/// and return the connection that occupied it — WITHOUT touching the pool. The
/// caller decides whether to release the returned connection. Pure array
/// mechanics; never dereferences the connection. Nulling before freeing `in_use`
/// ensures no hot-path reader observes a connection the caller is about to hand
/// back and that another transaction may immediately reuse.
fn pinnedFreeSlot(handle: usize) ?*pg.Conn {
    if (handle >= pinned_slots.len) return null;
    const slot = &pinned_slots[handle];
    // Atomically TAKE the connection out of the slot. A load-then-store here
    // let two threads that race to free the SAME slot both observe the same
    // *Conn and both return it — so both callers would `conn.release()` it
    // back into the pool. That double-release overflows the pool's idle-array
    // (Pool.release writes conns[_available] past the end) and corrupts the
    // heap, surfacing as glibc "double free or corruption" only on multicore
    // Linux under ReleaseFast (where the bounds check is elided). swap gives
    // the connection to exactly one caller; the loser gets null and releases
    // nothing.
    const conn = slot.conn.swap(null, .acq_rel) orelse return null;
    slot.in_use.store(false, .release);
    return conn;
}

fn pinnedAcquire() ?usize {
    return pinnedAcquireFromPool(-1);
}

fn pinnedAcquireFromPool(pool_handle: i64) ?usize {
    // Acquire directly from a specific pool — NOT through acquireConn()
    // which would return an already-pinned connection.
    // If pool_handle >= 0, use that specific pool. Otherwise use active pool.
    const pool = retainPoolByHandle(pool_handle, .fallback_to_active) orelse return null;
    // Transient resolution reference (the pinned checkout holds its own).
    defer pool.unref();
    const conn = pool.acquire() catch return null;
    trace("pinnedAcquireFromPool: pool_handle={d} pool={x} conn={x}", .{ pool_handle, @intFromPtr(pool), @intFromPtr(conn) });
    if (pinnedClaimSlot(conn)) |i| return i;
    // All slots taken — hand the connection back rather than leak it.
    conn.release();
    return null;
}

fn pinnedGet(handle: usize) ?*pg.Conn {
    if (handle >= pinned_slots.len) return null;
    return pinned_slots[handle].conn.load(.acquire);
}

fn pinnedRelease(handle: usize) void {
    // Release this slot's own connection UNCONDITIONALLY. Do NOT route through
    // releaseConn(): that legacy helper early-returns when the
    // active_pinned_handle / use_thread_conns globals are set, which would null
    // the slot without ever returning the connection to the pool (orphan / pool
    // drain). A pinned connection is always pool-owned.
    if (pinnedFreeSlot(handle)) |conn| {
        conn.release();
    }
}

// Python C API: _db_conn_acquire(pool_handle) → pinned handle (int)
pub fn db_conn_acquire(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_h: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &pool_h) == 0) return null;
    const handle = pinnedAcquireFromPool(pool_h) orelse {
        py.setError("Failed to acquire pinned connection", .{});
        return null;
    };
    return py.newInt(@intCast(handle));
}

// Python C API: _db_conn_release(handle) → None
pub fn db_conn_release(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;
    pinnedRelease(@intCast(handle));
    return py.pyNone();
}

// Python C API: _db_conn_execute(handle, sql, params) → rowcount
pub fn db_conn_execute(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    var sql_c: [*c]const u8 = null;
    var params_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lsO", &handle, &sql_c, &params_obj) == 0) return null;

    const sql_span = std.mem.span(sql_c);
    trace("db_conn_execute: handle={d} sql={s}", .{ handle, sql_span[0..@min(sql_span.len, 120)] });

    const conn = pinnedGet(@intCast(handle)) orelse {
        py.setError("Invalid connection handle", .{});
        return null;
    };
    const sql = std.mem.span(sql_c);

    // Extract params (nullable — Python None → SQL NULL)
    // Stack-first: 64 params on stack, heap for more
    var stack_strs: [64]?[]const u8 = undefined;
    var stack_bufs: [64][64]u8 = undefined;
    var pbufs = initParamBufs(params_obj, &stack_strs, &stack_bufs) orelse return null;
    defer pbufs.deinit();
    var overflow_stack: [1024]u8 = undefined;
    var overflow_buf: []u8 = &overflow_stack;
    var overflow_pos: usize = 0;
    var overflow_heap: ?[]u8 = null;
    defer if (overflow_heap) |hb| allocator.free(hb);
    var param_count: usize = pbufs.count;
    extractParams(params_obj, pbufs.strs, pbufs.num_bufs, &param_count, &overflow_buf, &overflow_pos, &overflow_heap) orelse return null;

    const values = pbufs.strs[0..param_count];
    const rowcount: i64 = switch (values.len) {
        0 => (conn.exec(sql, .{}) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        1 => (conn.exec(sql, .{values[0]}) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        2 => (conn.exec(sql, .{ values[0], values[1] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        3 => (conn.exec(sql, .{ values[0], values[1], values[2] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        4 => (conn.exec(sql, .{ values[0], values[1], values[2], values[3] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        5 => (conn.exec(sql, .{ values[0], values[1], values[2], values[3], values[4] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        6 => (conn.exec(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        7 => (conn.exec(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5], values[6] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        8 => (conn.exec(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7] }) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        else => blk: {
            const opts = pg.Conn.QueryOpts{ .column_names = false };
            break :blk execDmlDynamic(conn, sql, values, opts) orelse {
                setPgError(conn, "Execute failed", sql);
                return null;
            };
        },
    };
    return py.newInt(rowcount);
}

// Global: active pinned connection handle for transaction-aware queries.
// When set (>= 0), db_query uses this pinned connection instead of the pool,
// so SELECTs can see uncommitted DML within the same transaction.
// A2#2: read on native worker threads (acquireConn) while written by these
// Python C API setters under GIL-off — accessed via @atomicLoad/@atomicStore
// with acquire/release everywhere so the selection stays coherent.
var active_pinned_handle: i64 = -1;

// Python C API: _db_set_active_pinned(handle) — routes all queries through pinned conn
pub fn db_set_active_pinned(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;
    @atomicStore(i64, &active_pinned_handle, handle, .release);
    return py.pyNone();
}

// Python C API: _db_clear_active_pinned() — routes queries back to pool
pub fn db_clear_active_pinned(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    @atomicStore(i64, &active_pinned_handle, -1, .release);
    return py.pyNone();
}

// ── Growable write buffer — stack first, spills to heap transparently ─────────
// Same pattern as jsonEnsureSpace/jsonWrite in main.zig, kept local to db.zig
// to avoid cross-module coupling.

fn bufEnsureSpace(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, needed: usize) bool {
    if (pos.* + needed <= buf.*.len) return true;
    const new_size = @max(buf.*.len * 2, pos.* + needed + 256);
    const new_buf = allocator.alloc(u8, new_size) catch return false;
    @memcpy(new_buf[0..pos.*], buf.*[0..pos.*]);
    if (heap_buf.*) |old| allocator.free(old);
    heap_buf.* = new_buf;
    buf.* = new_buf;
    return true;
}

fn bufWrite(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, data: []const u8) bool {
    if (!bufEnsureSpace(buf, pos, heap_buf, data.len)) return false;
    @memcpy(buf.*[pos.*..][0..data.len], data);
    pos.* += data.len;
    return true;
}

fn bufWriteByte(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, byte: u8) bool {
    if (!bufEnsureSpace(buf, pos, heap_buf, 1)) return false;
    buf.*[pos.*] = byte;
    pos.* += 1;
    return true;
}

// Helper: write str(item)'s UTF-8 into a growable buffer. Returns false on failure.
fn writePyStr(item: *c.PyObject, buf: *[]u8, pos: *usize, heap_buf: *?[]u8) bool {
    const str_obj = c.PyObject_Str(item) orelse return false;
    defer c.Py_DecRef(str_obj);
    if (c.PyUnicode_AsUTF8(str_obj)) |s| {
        return bufWrite(buf, pos, heap_buf, std.mem.span(s));
    }
    return false;
}

// Helper: write a Python object's text representation into a growable buffer.
// Returns the slice written, or null on failure.
fn pyObjToText(item: *c.PyObject, buf: *[]u8, pos: *usize, heap_buf: *?[]u8) ?[]const u8 {
    const start = pos.*;
    if (c.PyUnicode_Check(item) != 0) {
        if (c.PyUnicode_AsUTF8(item)) |s| {
            const span = std.mem.span(s);
            if (!bufWrite(buf, pos, heap_buf, span)) return null;
        }
    } else if (c.PyBool_Check(item) != 0) {
        const val = if (item == py.pyTrue()) "true" else "false";
        if (!bufWrite(buf, pos, heap_buf, val)) return null;
    } else if (c.PyLong_Check(item) != 0) {
        // Use LongLong + overflow check (mirrors the scalar extractParams path):
        // PyLong_AsLong silently truncates a >int64 value to -1 AND leaves an
        // OverflowError set, poisoning the next native call. On overflow, clear
        // it and fall through to the exact decimal string via str().
        const val = c.PyLong_AsLongLong(item);
        if (c.PyErr_Occurred() != null) {
            c.PyErr_Clear();
            if (!writePyStr(item, buf, pos, heap_buf)) return null;
        } else {
            var num_buf: [32]u8 = undefined;
            const n = std.fmt.bufPrint(&num_buf, "{d}", .{val}) catch return null;
            if (!bufWrite(buf, pos, heap_buf, n)) return null;
        }
    } else if (c.PyFloat_Check(item) != 0) {
        const val = c.PyFloat_AsDouble(item);
        if (std.math.isNan(val)) {
            if (!bufWrite(buf, pos, heap_buf, "NaN")) return null;
        } else if (val == std.math.inf(f64)) {
            if (!bufWrite(buf, pos, heap_buf, "Infinity")) return null;
        } else if (val == -std.math.inf(f64)) {
            if (!bufWrite(buf, pos, heap_buf, "-Infinity")) return null;
        } else {
            // {d} is shortest-round-trip decimal; large magnitudes (≳1e63)
            // need a wide temp before copying into the growable buffer.
            var fbuf: [512]u8 = undefined;
            const n = std.fmt.bufPrint(&fbuf, "{d}", .{val}) catch
                (std.fmt.bufPrint(&fbuf, "{e}", .{val}) catch return null);
            if (!bufWrite(buf, pos, heap_buf, n)) return null;
        }
    } else {
        // Fallback: PyObject_Str → UTF-8
        if (!writePyStr(item, buf, pos, heap_buf)) return null;
    }
    return buf.*[start..pos.*];
}

// True if any element of the sequence is itself a dict/list/tuple — i.e. the value is a
// JSON structure (bound to JSONB), not a flat SQL array. Used to pick json.dumps over the
// PG-array-literal serializer for list/tuple params (mirrors the ORM heuristic).
fn seqHasContainerElem(seq: *c.PyObject) bool {
    const size = c.PySequence_Size(seq);
    if (size <= 0) return false;
    for (0..@as(usize, @intCast(size))) |j| {
        const elem = c.PySequence_GetItem(seq, @intCast(j)) orelse continue;
        defer c.Py_DecRef(elem);
        if (c.PyDict_Check(elem) != 0 or c.PyList_Check(elem) != 0 or c.PyTuple_Check(elem) != 0) {
            return true;
        }
    }
    return false;
}

// Helper: convert a Python list/tuple to a PostgreSQL array literal: {val1,val2,...}
// Strings are quoted and escaped per PG array literal rules.
fn pyListToPgArray(seq: *c.PyObject, buf: *[]u8, pos: *usize, heap_buf: *?[]u8) ?[]const u8 {
    const start = pos.*;
    const size = c.PySequence_Size(seq);
    if (size < 0) return null;

    if (!bufWriteByte(buf, pos, heap_buf, '{')) return null;

    for (0..@as(usize, @intCast(size))) |j| {
        if (j > 0) {
            if (!bufWriteByte(buf, pos, heap_buf, ',')) return null;
        }
        const elem_owned = c.PySequence_GetItem(seq, @intCast(j)) orelse return null;
        defer c.Py_DecRef(elem_owned);
        const elem = elem_owned;

        if (elem == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct))) {
            if (!bufWrite(buf, pos, heap_buf, "NULL")) return null;
        } else if (c.PyUnicode_Check(elem) != 0) {
            // Strings must be double-quoted in PG array literals
            // and internal quotes/backslashes escaped
            if (!bufWriteByte(buf, pos, heap_buf, '"')) return null;
            if (c.PyUnicode_AsUTF8(elem)) |s| {
                const span = std.mem.span(s);
                for (span) |ch| {
                    if (ch == '"' or ch == '\\') {
                        if (!bufWriteByte(buf, pos, heap_buf, '\\')) return null;
                    }
                    if (!bufWriteByte(buf, pos, heap_buf, ch)) return null;
                }
            }
            if (!bufWriteByte(buf, pos, heap_buf, '"')) return null;
        } else if (c.PyList_Check(elem) != 0 or c.PyTuple_Check(elem) != 0) {
            // Nested array — recurse
            _ = pyListToPgArray(elem, buf, pos, heap_buf) orelse return null;
        } else {
            // Numeric/bool/other — write unquoted text representation
            _ = pyObjToText(elem, buf, pos, heap_buf) orelse return null;
        }
    }

    if (!bufWriteByte(buf, pos, heap_buf, '}')) return null;
    return buf.*[start..pos.*];
}

// Helper: extract params from Python list into nullable string array.
// Python None → SQL NULL (wire protocol NULL), everything else → text string.
// Uses a growable buffer for values that exceed the 64-byte per-slot static buffers
// (long strings, array literals, etc.).
/// Extract query parameters from a Python sequence into string slices.
/// Uses stack buffers for <=64 params (common case), heap-allocates for more.
/// Caller provides slices sized to the actual param count.
/// Store a byte span as a bound param value: into the 64-byte per-param stack
/// buffer when it fits, else appended to the growable overflow buffer. Returns
/// the stored slice (valid for the query's lifetime) or "" on alloc failure.
fn storeParamStr(span: []const u8, str_buf: *[64]u8, overflow_buf: *[]u8, overflow_pos: *usize, overflow_heap: *?[]u8) []const u8 {
    if (span.len <= str_buf.len) {
        @memcpy(str_buf[0..span.len], span);
        return str_buf[0..span.len];
    }
    const start = overflow_pos.*;
    if (bufWrite(overflow_buf, overflow_pos, overflow_heap, span)) {
        return overflow_buf.*[start..overflow_pos.*];
    }
    return "";
}

fn extractParams(params_obj: ?*c.PyObject, param_strs: []?[]const u8, str_bufs: [][64]u8, param_count: *usize, overflow_buf: *[]u8, overflow_pos: *usize, overflow_heap: *?[]u8) ?void {
    if (params_obj) |plist| {
        // PySequence_Size/GetItem work on list, tuple, and any sequence — no branching
        const size = c.PySequence_Size(plist);
        if (size < 0) return {}; // Not a sequence — treat as no params
        param_count.* = @intCast(size);
        if (param_count.* > param_strs.len) {
            py.setError("Too many parameters ({d}, buffer {d})", .{ param_count.*, param_strs.len });
            return null;
        }
        if (param_count.* > 0) {
            for (0..param_count.*) |i| {
                const item_owned = c.PySequence_GetItem(plist, @intCast(i)) orelse continue;
                defer c.Py_DecRef(item_owned); // GetItem returns new ref
                const item = item_owned;
                if (item == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct))) {
                    // Python None → SQL NULL (wire protocol -1 length)
                    param_strs[i] = null;
                } else if (c.PyUnicode_Check(item) != 0) {
                    // AsUTF8AndSize (not AsUTF8 + span) so an embedded NUL isn't
                    // silently truncated — PG rejects \x00 in text itself, so we
                    // surface its error instead of dropping the tail. The pointer
                    // stays valid because the params list keeps `item` alive.
                    var slen: c.Py_ssize_t = 0;
                    if (c.PyUnicode_AsUTF8AndSize(item, &slen)) |s| {
                        param_strs[i] = @as([*c]const u8, s)[0..@intCast(slen)];
                    } else {
                        param_strs[i] = "";
                    }
                } else if (c.PyBytes_Check(item) != 0) {
                    // bytes → raw byte slice. The pg layer's bindSlice sends this
                    // verbatim via the binary Bytea encoder once Describe resolves
                    // the param OID to bytea (17), so binary values round-trip
                    // losslessly (was: fell through to PyObject_Str → the ASCII of
                    // the Python repr b'\x00\x01\xff', corrupting the value).
                    // The pointer stays valid because the params list keeps `item`
                    // alive for the query; a slice length carries embedded NULs.
                    const n = c.PyBytes_Size(item);
                    const p = c.PyBytes_AsString(item);
                    if (p != null and n >= 0) {
                        param_strs[i] = @as([*c]const u8, @ptrCast(p))[0..@intCast(n)];
                    } else {
                        param_strs[i] = "";
                    }
                } else if (c.PyByteArray_Check(item) != 0) {
                    // Mutable buffer — copy the bytes so a later resize can't
                    // dangle the slice; PG binds it verbatim as bytea (see above).
                    const n = c.PyByteArray_Size(item);
                    const p = c.PyByteArray_AsString(item);
                    if (p != null and n >= 0) {
                        const raw = @as([*c]const u8, @ptrCast(p))[0..@intCast(n)];
                        param_strs[i] = storeParamStr(raw, &str_bufs[i], overflow_buf, overflow_pos, overflow_heap);
                    } else {
                        param_strs[i] = "";
                    }
                } else if (c.PyDict_Check(item) != 0) {
                    // Python dict → JSON string for JSONB columns.
                    // Uses Python json.dumps via C API — correct JSON output,
                    // reuses the same json module the platform uses everywhere.
                    const json_mod = c.PyImport_ImportModule("json") orelse continue;
                    defer c.Py_DecRef(json_mod);
                    const json_obj = c.PyObject_CallMethod(json_mod, "dumps", "(O)", item) orelse continue;
                    defer c.Py_DecRef(json_obj);
                    if (c.PyUnicode_AsUTF8(json_obj)) |js| {
                        const span = std.mem.span(js);
                        if (span.len <= 64) {
                            @memcpy(str_bufs[i][0..span.len], span);
                            param_strs[i] = str_bufs[i][0..span.len];
                        } else {
                            const start = overflow_pos.*;
                            if (bufWrite(overflow_buf, overflow_pos, overflow_heap, span)) {
                                param_strs[i] = overflow_buf.*[start..overflow_pos.*];
                            } else {
                                param_strs[i] = "{}";
                            }
                        }
                    } else {
                        param_strs[i] = "{}";
                    }
                } else if (c.PyList_Check(item) != 0 or c.PyTuple_Check(item) != 0) {
                    // A list/tuple whose elements are themselves containers (dict/list) is a
                    // JSON structure, never a flat SQL array — serialize it via json.dumps so
                    // it binds correctly to a JSONB column (mirrors the ORM's
                    // _pg_quote_literal heuristic in db/pgzig_connection.py). An EMPTY list is
                    // ALSO json.dumps'd (→ `[]`): its PG-array form `{}` is INDISTINGUISHABLE from
                    // an empty JSON object `{}` at the text-only bind path, so emitting `[]` here at
                    // the SOURCE (where the Python type is known to be a list) is the only way an
                    // empty list round-trips through a JSONB column as a JSON array (vs `{}` an
                    // object). A FLAT non-empty scalar list stays a PG array literal `{a,b}` (correct
                    // for a native array column); when that flat list targets a JSONB column the bind
                    // path coerces the literal to a JSON array (see types.zig encodeBytesCoerced).
                    // (Native array columns are bound via SQL `ARRAY[...]` literals, not list params,
                    // so an empty list never needs the PG-array `{}` form here.)
                    if (seqHasContainerElem(item) or c.PySequence_Size(item) == 0) {
                        const json_mod = c.PyImport_ImportModule("json") orelse continue;
                        defer c.Py_DecRef(json_mod);
                        const json_obj = c.PyObject_CallMethod(json_mod, "dumps", "(O)", item) orelse continue;
                        defer c.Py_DecRef(json_obj);
                        if (c.PyUnicode_AsUTF8(json_obj)) |js| {
                            const span = std.mem.span(js);
                            if (span.len <= 64) {
                                @memcpy(str_bufs[i][0..span.len], span);
                                param_strs[i] = str_bufs[i][0..span.len];
                            } else {
                                const start = overflow_pos.*;
                                if (bufWrite(overflow_buf, overflow_pos, overflow_heap, span)) {
                                    param_strs[i] = overflow_buf.*[start..overflow_pos.*];
                                } else {
                                    param_strs[i] = "[]";
                                }
                            }
                        } else {
                            param_strs[i] = "[]";
                        }
                    } else {
                        // Flat list/tuple → PostgreSQL array literal: {val1,val2,...}
                        param_strs[i] = pyListToPgArray(item, overflow_buf, overflow_pos, overflow_heap) orelse "";
                    }
                } else if (c.PyBool_Check(item) != 0) {
                    // Bool check MUST come before Long check (Python bools are ints)
                    param_strs[i] = if (item == py.pyTrue()) "true" else "false";
                } else if (c.PyLong_Check(item) != 0) {
                    const val = c.PyLong_AsLongLong(item);
                    if (c.PyErr_Occurred() != null) {
                        // Value exceeds int64 (e.g. a NUMERIC-bound big int).
                        // PyLong_AsLongLong returned -1 and set OverflowError —
                        // clear it (else it poisons the next native call) and
                        // fall back to the exact decimal string via str().
                        c.PyErr_Clear();
                        const str_obj = c.PyObject_Str(item) orelse {
                            param_strs[i] = "";
                            continue;
                        };
                        defer c.Py_DecRef(str_obj);
                        if (c.PyUnicode_AsUTF8(str_obj)) |s| {
                            param_strs[i] = storeParamStr(std.mem.span(s), &str_bufs[i], overflow_buf, overflow_pos, overflow_heap);
                        } else {
                            param_strs[i] = "";
                        }
                    } else {
                        const n = std.fmt.bufPrint(&str_bufs[i], "{d}", .{val}) catch "";
                        param_strs[i] = n;
                    }
                } else if (c.PyFloat_Check(item) != 0) {
                    const val = c.PyFloat_AsDouble(item);
                    if (std.math.isNan(val)) {
                        param_strs[i] = "NaN";
                    } else if (val == std.math.inf(f64)) {
                        param_strs[i] = "Infinity";
                    } else if (val == -std.math.inf(f64)) {
                        param_strs[i] = "-Infinity";
                    } else {
                        // {d} for a finite float is decimal; a large magnitude
                        // (≳1e63) overflows the 64-byte per-param buffer, so
                        // format into a wide temp and store via the overflow path
                        // (was: bufPrint error → "" → PG syntax error).
                        var fbuf: [512]u8 = undefined;
                        const n = std.fmt.bufPrint(&fbuf, "{d}", .{val}) catch
                            (std.fmt.bufPrint(&fbuf, "{e}", .{val}) catch "");
                        param_strs[i] = storeParamStr(n, &str_bufs[i], overflow_buf, overflow_pos, overflow_heap);
                    }
                } else {
                    // Fallback: PyObject_Str → copy into str_bufs if fits, overflow buffer otherwise
                    const str_obj = c.PyObject_Str(item) orelse continue;
                    defer c.Py_DecRef(str_obj);
                    if (c.PyUnicode_AsUTF8(str_obj)) |s| {
                        const span = std.mem.span(s);
                        if (span.len <= 64) {
                            @memcpy(str_bufs[i][0..span.len], span);
                            param_strs[i] = str_bufs[i][0..span.len];
                        } else {
                            // Overflow: write into growable buffer
                            const start = overflow_pos.*;
                            if (bufWrite(overflow_buf, overflow_pos, overflow_heap, span)) {
                                param_strs[i] = overflow_buf.*[start..overflow_pos.*];
                            } else {
                                param_strs[i] = "";
                            }
                        }
                    } else {
                        param_strs[i] = "";
                    }
                }
            }
        }
    }
    return {};
}

// ── Inferred Parse-time parameter type OIDs ─────────────────────────────────
// db_query* serialize every param as TEXT with no declared type, so the Parse
// message historically declared ZERO param types and let PostgreSQL infer them
// from context. That inference resolves the WRONG type for
// `col = ANY(ARRAY[$1,$2,...])`: the ARRAY constructor pins its element type
// from the (unknown) params to `text` BEFORE the outer `col = ANY(...)` can
// propagate the column type, producing `integer = text` (no such operator).
//
// Fix: declare a concrete type OID for the params whose Python runtime type
// maps unambiguously to a PostgreSQL scalar — int→int8, float→float8,
// bool→bool. int8 coerces to int2/int4/int8/numeric/float in every normal
// comparison and is what asyncpg/psycopg send for a Python int, so scalar
// shapes (`id = $1`, INSERTs, etc.) keep working AND the ARRAY element type is
// now `int8[]`, giving `int4 = ANY(int8[])`. Everything else — str, None,
// bytes, dict, list, Decimal, datetime, uuid-as-str — stays 0 (inferred) so
// its text→target coercion is preserved unchanged.
const PG_OID_BOOL: i32 = 16;
const PG_OID_INT8: i32 = 20;
const PG_OID_FLOAT8: i32 = 701;

const ParamOids = struct {
    // null → declare nothing (byte-for-byte the historical zero-type Parse).
    oids: ?[]const i32 = null,
    heap: ?[]i32 = null, // non-null when spilled to the heap; caller frees

    fn deinit(self: *ParamOids) void {
        if (self.heap) |h| allocator.free(h);
    }
};

/// Scan `sql` and mark, in `mask`, the 1-based positions of every `$N`
/// placeholder that appears lexically INSIDE an `ARRAY[...]` constructor.
/// `mask[k]` corresponds to `$(k+1)`; positions past the param range
/// (`k >= mask.len`) are ignored. Returns true if any position was marked.
///
/// A parameter only needs a declared type OID when it sits inside an
/// `ARRAY[...]` constructor — that is the ONLY shape where PostgreSQL resolves
/// an unknown param to `text` (via the array element type) before the outer
/// comparison can supply the column's type, yielding `integer = text`. Params
/// ANYWHERE ELSE — including a `$N` on a text column in the SAME query that
/// merely also contains an unrelated `ARRAY[...]` literal — keep their
/// historical unknown type so their text→target coercion is preserved.
///
/// String literals (`'...'`, `''` escaping) and quoted identifiers (`"..."`)
/// are skipped, so an `ARRAY[` or `$1` appearing inside a literal is ignored.
fn markArrayParams(sql: []const u8, mask: []bool) bool {
    // One entry per currently-open '[' bracket: true if that bracket opened an
    // ARRAY[...] constructor. "Inside an array" == array_nesting > 0. Depth is
    // bounded; arrays realistically never nest past a handful of levels.
    var bracket_is_array: [64]bool = undefined;
    var depth: usize = 0;
    var array_nesting: usize = 0; // count of true entries in bracket_is_array
    var array_pending = false; // the ARRAY keyword was just seen; next '[' opens a ctor
    var any = false;

    var i: usize = 0;
    while (i < sql.len) {
        const ch = sql[i];
        // Whitespace never clears a pending ARRAY keyword (`ARRAY  [` is valid).
        if (ch == ' ' or ch == '\t' or ch == '\n' or ch == '\r') {
            i += 1;
            continue;
        }
        switch (ch) {
            '\'' => {
                // single-quoted string literal ('' escapes an embedded quote)
                i += 1;
                while (i < sql.len) {
                    if (sql[i] == '\'') {
                        if (i + 1 < sql.len and sql[i + 1] == '\'') {
                            i += 2;
                            continue;
                        }
                        i += 1;
                        break;
                    }
                    i += 1;
                }
                array_pending = false;
            },
            '"' => {
                // double-quoted identifier ("" escapes an embedded quote)
                i += 1;
                while (i < sql.len) {
                    if (sql[i] == '"') {
                        if (i + 1 < sql.len and sql[i + 1] == '"') {
                            i += 2;
                            continue;
                        }
                        i += 1;
                        break;
                    }
                    i += 1;
                }
                array_pending = false;
            },
            '[' => {
                if (depth < bracket_is_array.len) {
                    bracket_is_array[depth] = array_pending;
                    if (array_pending) array_nesting += 1;
                }
                depth += 1;
                array_pending = false;
                i += 1;
            },
            ']' => {
                if (depth > 0) {
                    depth -= 1;
                    if (depth < bracket_is_array.len and bracket_is_array[depth]) {
                        array_nesting -= 1;
                    }
                }
                array_pending = false;
                i += 1;
            },
            '$' => {
                var j = i + 1;
                var n: usize = 0;
                var has_digit = false;
                while (j < sql.len and sql[j] >= '0' and sql[j] <= '9') {
                    n = n * 10 + (sql[j] - '0');
                    has_digit = true;
                    j += 1;
                }
                if (has_digit and array_nesting > 0 and n >= 1 and (n - 1) < mask.len) {
                    mask[n - 1] = true;
                    any = true;
                }
                array_pending = false;
                i = j;
            },
            else => {
                if (std.ascii.isAlphabetic(ch) or ch == '_') {
                    const start = i;
                    var j = i + 1;
                    while (j < sql.len and (std.ascii.isAlphanumeric(sql[j]) or sql[j] == '_')) j += 1;
                    // Case-insensitive "ARRAY" keyword arms the next '['.
                    array_pending = std.ascii.eqlIgnoreCase(sql[start..j], "ARRAY");
                    i = j;
                } else {
                    array_pending = false;
                    i += 1;
                }
            },
        }
    }
    return any;
}

/// Infer per-param Parse-time type OIDs from the Python values, but ONLY for
/// the params that sit lexically inside an `ARRAY[...]` constructor (see
/// markArrayParams) — every other position keeps the historical
/// zero-declaration Parse (0 in the returned slice). Returns ``.oids == null``
/// when nothing needs declaring; otherwise a slice of ``count`` OIDs.
fn computeParamOids(sql: []const u8, params_obj: ?*c.PyObject, stack_buf: []i32) ParamOids {
    var out = ParamOids{};
    const plist = params_obj orelse return out;
    const size = c.PySequence_Size(plist);
    if (size <= 0) return out;
    const count: usize = @intCast(size);

    // Mask of which param positions are inside an ARRAY[...] constructor.
    var mask_stack: [64]bool = [_]bool{false} ** 64;
    var mask: []bool = mask_stack[0..];
    var mask_heap: ?[]bool = null;
    defer if (mask_heap) |m| allocator.free(m);
    if (count > mask_stack.len) {
        mask_heap = allocator.alloc(bool, count) catch return out; // OOM → historical path
        @memset(mask_heap.?, false);
        mask = mask_heap.?;
    }
    if (!markArrayParams(sql, mask[0..count])) return out; // no $N inside ARRAY[]: untouched

    var buf: []i32 = stack_buf;
    if (count > stack_buf.len) {
        buf = allocator.alloc(i32, count) catch return out; // OOM → historical path
        out.heap = buf;
    }

    var any = false;
    for (0..count) |i| {
        // Only params inside an ARRAY[...] get a declared type; the rest stay 0.
        if (!mask[i]) {
            buf[i] = 0;
            continue;
        }
        const item = c.PySequence_GetItem(plist, @intCast(i)) orelse {
            buf[i] = 0;
            continue;
        };
        defer c.Py_DecRef(item);
        // Bool MUST be checked before Long — Python bool is an int subclass.
        if (c.PyBool_Check(item) != 0) {
            buf[i] = PG_OID_BOOL;
            any = true;
        } else if (c.PyLong_Check(item) != 0) {
            buf[i] = PG_OID_INT8;
            any = true;
        } else if (c.PyFloat_Check(item) != 0) {
            buf[i] = PG_OID_FLOAT8;
            any = true;
        } else {
            buf[i] = 0; // str/None/bytes/dict/list/Decimal/datetime → PG infers
        }
    }

    if (!any) {
        // No concrete OID needed — preserve the exact historical Parse bytes.
        if (out.heap) |h| {
            allocator.free(h);
            out.heap = null;
        }
        return out; // .oids stays null
    }
    out.oids = buf[0..count];
    return out;
}

// Stack-first param extraction: 64 on stack, heap for more.
// Returns the param count. Caller must defer cleanup of heap_* if used.
const ParamBufs = struct {
    strs: []?[]const u8,
    num_bufs: [][64]u8,
    count: usize,
    heap_strs: ?[]?[]const u8,
    heap_num: ?[][64]u8,

    fn deinit(self: *ParamBufs) void {
        if (self.heap_strs) |h| allocator.free(h);
        if (self.heap_num) |h| allocator.free(h);
    }
};

fn initParamBufs(params_obj: ?*c.PyObject, stack_strs: *[64]?[]const u8, stack_bufs: *[64][64]u8) ?ParamBufs {
    var result = ParamBufs{
        .strs = stack_strs,
        .num_bufs = stack_bufs,
        .count = 0,
        .heap_strs = null,
        .heap_num = null,
    };
    if (params_obj) |plist| {
        const size = c.PySequence_Size(plist);
        if (size < 0) return result; // Not a sequence
        const count: usize = @intCast(size);
        if (count > 64) {
            result.heap_strs = allocator.alloc(?[]const u8, count) catch {
                py.setError("OOM allocating param buffers for {d} params", .{count});
                return null;
            };
            result.heap_num = allocator.alloc([64]u8, count) catch {
                allocator.free(result.heap_strs.?);
                result.heap_strs = null;
                py.setError("OOM allocating param buffers for {d} params", .{count});
                return null;
            };
            result.strs = result.heap_strs.?;
            result.num_bufs = result.heap_num.?;
        }
        result.count = count;
    }
    return result;
}

// ── SQL builders (all pre-built at registration time, not per-request) ───────

fn isValidIdentifier(name: []const u8) bool {
    if (name.len == 0 or name.len > 64) return false;
    for (name, 0..) |ch, i| {
        if (i == 0) {
            if (!std.ascii.isAlphabetic(ch) and ch != '_') return false;
        } else {
            if (!std.ascii.isAlphanumeric(ch) and ch != '_') return false;
        }
    }
    return true;
}

fn buildSelectOneSql(table: []const u8, pk_column: []const u8) []const u8 {
    return std.fmt.allocPrint(allocator, "SELECT * FROM {s} WHERE {s} = $1", .{ table, pk_column }) catch "";
}

fn buildSelectListSql(table: []const u8) []const u8 {
    return std.fmt.allocPrint(allocator, "SELECT * FROM {s} LIMIT $1 OFFSET $2", .{table}) catch "";
}

fn buildInsertSql(table: []const u8, columns: []const []const u8) []const u8 {
    // INSERT INTO users (name, email, age) VALUES ($1, $2, $3) RETURNING *
    // Uses heap buffers — registration-time only, no fixed buffer limits
    var col_list: std.ArrayListUnmanaged(u8) = .empty;
    defer col_list.deinit(allocator);
    var val_list: std.ArrayListUnmanaged(u8) = .empty;
    defer val_list.deinit(allocator);

    for (columns, 0..) |col, i| {
        if (i > 0) {
            col_list.appendSlice(allocator, ", ") catch return "";
            val_list.appendSlice(allocator, ", ") catch return "";
        }
        col_list.appendSlice(allocator, col) catch return "";
        {
            var param_buf: [16]u8 = undefined;
            const param_str = std.fmt.bufPrint(&param_buf, "${d}", .{i + 1}) catch return "";
            val_list.appendSlice(allocator, param_str) catch return "";
        }
    }

    return std.fmt.allocPrint(allocator, "INSERT INTO {s} ({s}) VALUES ({s}) RETURNING *", .{
        table,
        col_list.items,
        val_list.items,
    }) catch "";
}

fn buildDeleteSql(table: []const u8, pk_column: []const u8) []const u8 {
    return std.fmt.allocPrint(allocator, "DELETE FROM {s} WHERE {s} = $1", .{ table, pk_column }) catch "";
}
// ── JSON serialization — delegates to pg.zig's writeJsonRow ──────────────────

fn serializeRow(row: anytype, col_names: []const []const u8, buf: []u8) ![]const u8 {
    const len = row.writeJsonRow(col_names, buf);
    if (len == 0) return error.SerializationFailed;
    return buf[0..len];
}

/// Serialize a row to JSON. Tries 8KB stack buffer first, then grows via heap.
/// Returns (json_slice, heap_buf_to_free). Caller must free heap_buf if non-null.
const SerResult = struct { json: []const u8, heap_buf: ?[]u8 };

fn serializeRowAlloc(row: anytype, col_names: []const []const u8, stack_buf: []u8) SerResult {
    if (serializeRow(row, col_names, stack_buf)) |json| {
        return .{ .json = json, .heap_buf = null };
    } else |_| {}
    // Stack buffer too small — compute needed size from actual row data.
    // Sum raw data + column names + JSON structural overhead (quotes, colons, commas).
    var raw_size: usize = 2; // {}
    const ncols = @min(col_names.len, row.values.len);
    for (0..ncols) |i| {
        raw_size += col_names[i].len + 4; // "name":,
        if (!row.values[i].is_null) {
            raw_size += row.values[i].data.len;
        }
        raw_size += 8; // overhead per column (quotes, null literal, etc.)
    }
    // Try 2x first (handles typical text), then 6x (handles pathological escaping)
    const multipliers = [_]usize{ 2, 6 };
    for (multipliers) |mult| {
        const needed = raw_size * mult;
        const heap = allocator.alloc(u8, needed) catch continue;
        if (serializeRow(row, col_names, heap)) |json| {
            return .{ .json = json, .heap_buf = heap };
        } else |_| {
            allocator.free(heap);
        }
    }
    return .{ .json = "", .heap_buf = null };
}
// ── Request dispatch (called from server.zig fast-exit path) ─────────────────

pub fn handleDbRoute(
    stream: py.NetStream,
    entry: *const DbRouteEntry,
    body: []const u8,
    params: *const router_mod.RouteParams,
    query_string: []const u8,
    sendResponseFn: *const fn (py.NetStream, u16, []const u8, []const u8) void,
) void {
    switch (entry.op) {
        .select_one => {
            const pk_param = entry.pk_param orelse "id";
            const pk_val = params.get(pk_param) orelse {
                sendResponseFn(stream, 400, "application/json", "{\"error\": \"Missing primary key\"}");
                return;
            };

            // Cache check — build cache key from table + pk value
            var cache_key_buf: [256]u8 = undefined;
            const cache_key = std.fmt.bufPrint(&cache_key_buf, "GET:{s}:{s}", .{ entry.table, pk_val }) catch "";
            if (cache_key.len > 0) {
                if (cacheGet(cache_key)) |cached_body| {
                    defer allocator.free(cached_body); // owned copy (F12)
                    sendResponseFn(stream, 200, "application/json", cached_body);
                    return;
                }
            }

            const acq = acquireConn() orelse {
                sendResponseFn(stream, 503, "application/json", "{\"error\": \"Database connection unavailable\"}");
                return;
            };
            const conn = acq.conn;
            // Release ONLY a pool-owned connection; a pinned/thread-owned one is
            // still referenced elsewhere and releasing it would alias it (F3).
            defer releaseAcquired(acq);

            var result = conn.queryOpts(entry.select_sql, .{pk_val}, .{ .column_names = true, .cache_name = entry.cache_name }) catch {
                sendResponseFn(stream, 500, "application/json", "{\"error\": \"Query failed\"}");
                return;
            };
            defer result.deinit();

            if (result.next() catch null) |row| {
                var json_stack: [8192]u8 = undefined;
                const ser = serializeRowAlloc(row, result.column_names, &json_stack);
                defer if (ser.heap_buf) |h| allocator.free(h);
                if (ser.json.len == 0) {
                    sendResponseFn(stream, 500, "application/json", "{\"error\": \"Row too large to serialize\"}");
                    return;
                }
                if (cache_key.len > 0) {
                    cachePut(cache_key, ser.json, entry.table);
                }
                sendResponseFn(stream, 200, "application/json", ser.json);
            } else {
                sendResponseFn(stream, 404, "application/json", "{\"error\": \"Not found\"}");
            }
        },

        .select_list => {
            var limit: []const u8 = "50";
            var offset: []const u8 = "0";

            if (query_string.len > 0) {
                var qs_iter = std.mem.splitScalar(u8, query_string, '&');
                while (qs_iter.next()) |pair| {
                    if (std.mem.indexOf(u8, pair, "limit=")) |idx| {
                        limit = pair[idx + 6 ..];
                    } else if (std.mem.indexOf(u8, pair, "offset=")) |idx| {
                        offset = pair[idx + 7 ..];
                    }
                }
            }

            // Cache check for list queries
            var cache_key_buf: [256]u8 = undefined;
            const cache_key = std.fmt.bufPrint(&cache_key_buf, "LIST:{s}:{s}:{s}", .{ entry.table, limit, offset }) catch "";
            if (cache_key.len > 0) {
                if (cacheGet(cache_key)) |cached_body| {
                    defer allocator.free(cached_body); // owned copy (F12)
                    sendResponseFn(stream, 200, "application/json", cached_body);
                    return;
                }
            }

            const acq = acquireConn() orelse {
                sendResponseFn(stream, 503, "application/json", "{\"error\": \"Database connection unavailable\"}");
                return;
            };
            const conn = acq.conn;
            // Release ONLY a pool-owned connection; a pinned/thread-owned one is
            // still referenced elsewhere and releasing it would alias it (F3).
            defer releaseAcquired(acq);

            var result = conn.queryOpts(entry.select_sql, .{ limit, offset }, .{ .column_names = true, .cache_name = entry.cache_name }) catch {
                sendResponseFn(stream, 500, "application/json", "{\"error\": \"Query failed\"}");
                return;
            };
            defer result.deinit();

            var out_buf = allocator.alloc(u8, 65536) catch {
                sendResponseFn(stream, 500, "application/json", "{\"error\": \"Out of memory\"}");
                return;
            };
            defer allocator.free(out_buf);

            var out_pos: usize = 0;
            out_buf[out_pos] = '[';
            out_pos += 1;

            var row_count: usize = 0;
            while (true) {
                const row = result.next() catch {
                    // Mid-stream failure — recover the connection and return 500
                    // rather than a truncated 200 (and never cache a partial body).
                    recoverAfterRowError(result, conn);
                    sendResponseFn(stream, 500, "application/json", "{\"error\": \"Query failed mid-result\"}");
                    return;
                } orelse break;
                if (row_count > 0) {
                    out_buf[out_pos] = ',';
                    out_pos += 1;
                }
                var row_stack: [8192]u8 = undefined;
                const row_ser = serializeRowAlloc(row, result.column_names, &row_stack);
                defer if (row_ser.heap_buf) |h| allocator.free(h);
                if (row_ser.json.len == 0) break;
                if (out_pos + row_ser.json.len + 2 > out_buf.len) break;
                @memcpy(out_buf[out_pos..][0..row_ser.json.len], row_ser.json);
                out_pos += row_ser.json.len;
                row_count += 1;
            }

            out_buf[out_pos] = ']';
            out_pos += 1;

            const response_body = out_buf[0..out_pos];
            if (cache_key.len > 0) {
                cachePut(cache_key, response_body, entry.table);
            }
            sendResponseFn(stream, 200, "application/json", response_body);
        },

        .insert => {
            if (body.len == 0) {
                sendResponseFn(stream, 400, "application/json", "{\"error\": \"Request body required\"}");
                return;
            }

            if (entry.schema) |schema| {
                const vr = dhi.validateJson(body, &schema);
                switch (vr) {
                    .ok => {},
                    .err => |ve| {
                        defer ve.deinit();
                        sendResponseFn(stream, ve.status_code, "application/json", ve.body);
                        return;
                    },
                }
            }

            const parsed = std.json.parseFromSlice(std.json.Value, allocator, body, .{}) catch {
                sendResponseFn(stream, 400, "application/json", "{\"error\": \"Invalid JSON\"}");
                return;
            };
            defer parsed.deinit();

            const obj = switch (parsed.value) {
                .object => |o| o,
                else => {
                    sendResponseFn(stream, 400, "application/json", "{\"error\": \"Expected JSON object\"}");
                    return;
                },
            };

            // Stack for <=64 columns, heap for more
            var stack_values: [64]?[]const u8 = undefined;
            const ncols = entry.columns.len;
            var heap_values: ?[]?[]const u8 = null;
            const values: []?[]const u8 = if (ncols <= 64)
                stack_values[0..ncols]
            else blk: {
                heap_values = allocator.alloc(?[]const u8, ncols) catch {
                    sendResponseFn(stream, 500, "application/json", "{\"error\": \"Too many columns\"}");
                    return;
                };
                break :blk heap_values.?;
            };
            defer if (heap_values) |h| allocator.free(h);

            for (entry.columns[0..ncols], 0..) |col, i| {
                if (obj.get(col)) |val| {
                    values[i] = switch (val) {
                        .string => |s| s,
                        .integer => |n| std.fmt.allocPrint(allocator, "{d}", .{n}) catch "",
                        .float => |f| std.fmt.allocPrint(allocator, "{d}", .{f}) catch "",
                        .bool => |b| if (b) "true" else "false",
                        .null => null,
                        else => "",
                    };
                } else {
                    values[i] = null;
                }
            }

            const acq = acquireConn() orelse {
                sendResponseFn(stream, 503, "application/json", "{\"error\": \"Database connection unavailable\"}");
                return;
            };
            const conn = acq.conn;
            // Release ONLY a pool-owned connection; a pinned/thread-owned one is
            // still referenced elsewhere and releasing it would alias it (F3).
            defer releaseAcquired(acq);

            const insert_result = execWithParams(conn, entry.insert_sql, values[0..ncols], entry.cache_name, null);
            if (insert_result) |result| {
                defer result.deinit();
                // Invalidate cache on write
                invalidateTableCache(entry.table);
                if (result.next() catch null) |row| {
                    var json_stack2: [8192]u8 = undefined;
                    const ser2 = serializeRowAlloc(row, result.column_names, &json_stack2);
                    defer if (ser2.heap_buf) |h| allocator.free(h);
                    if (ser2.json.len == 0) {
                        sendResponseFn(stream, 201, "application/json", "{\"created\": true}");
                        return;
                    }
                    sendResponseFn(stream, 201, "application/json", ser2.json);
                } else {
                    sendResponseFn(stream, 201, "application/json", "{\"created\": true}");
                }
            } else {
                sendResponseFn(stream, 500, "application/json", "{\"error\": \"Insert failed\"}");
            }
        },

        .delete => {
            const pk_param = entry.pk_param orelse "id";
            const pk_val = params.get(pk_param) orelse {
                sendResponseFn(stream, 400, "application/json", "{\"error\": \"Missing primary key\"}");
                return;
            };

            const acq = acquireConn() orelse {
                sendResponseFn(stream, 503, "application/json", "{\"error\": \"Database connection unavailable\"}");
                return;
            };
            const conn = acq.conn;
            // Release ONLY a pool-owned connection; a pinned/thread-owned one is
            // still referenced elsewhere and releasing it would alias it (F3).
            defer releaseAcquired(acq);

            const affected = conn.exec(entry.delete_sql, .{pk_val}) catch {
                sendResponseFn(stream, 500, "application/json", "{\"error\": \"Delete failed\"}");
                return;
            };

            // Invalidate cache on write
            invalidateTableCache(entry.table);

            if (affected) |n| {
                if (n > 0) {
                    sendResponseFn(stream, 204, "application/json", "");
                } else {
                    sendResponseFn(stream, 404, "application/json", "{\"error\": \"Not found\"}");
                }
            } else {
                sendResponseFn(stream, 404, "application/json", "{\"error\": \"Not found\"}");
            }
        },

        .custom_query, .custom_query_single => {
            // Collect params: path params first, then query string params
            var param_values: [16]?[]const u8 = undefined;
            var param_count: usize = 0;

            for (entry.param_names) |pname| {
                if (param_count >= 16) break;
                if (params.get(pname)) |v| {
                    param_values[param_count] = v;
                    param_count += 1;
                } else {
                    // Try query string
                    var found = false;
                    if (query_string.len > 0) {
                        var qs_iter = std.mem.splitScalar(u8, query_string, '&');
                        while (qs_iter.next()) |pair| {
                            const eq = std.mem.indexOf(u8, pair, "=") orelse continue;
                            if (std.mem.eql(u8, pair[0..eq], pname)) {
                                param_values[param_count] = pair[eq + 1 ..];
                                param_count += 1;
                                found = true;
                                break;
                            }
                        }
                    }
                    if (!found) {
                        param_values[param_count] = "";
                        param_count += 1;
                    }
                }
            }

            // Cache check
            var cache_key_buf: [512]u8 = undefined;
            var ck_pos: usize = 0;
            const prefix = "Q:";
            @memcpy(cache_key_buf[ck_pos..][0..prefix.len], prefix);
            ck_pos += prefix.len;
            const sql_key_len = @min(entry.custom_sql.len, 64);
            @memcpy(cache_key_buf[ck_pos..][0..sql_key_len], entry.custom_sql[0..sql_key_len]);
            ck_pos += sql_key_len;
            for (param_values[0..param_count]) |v_opt| {
                cache_key_buf[ck_pos] = ':';
                ck_pos += 1;
                if (v_opt) |v| {
                    const vlen = @min(v.len, 32);
                    @memcpy(cache_key_buf[ck_pos..][0..vlen], v[0..vlen]);
                    ck_pos += vlen;
                }
            }
            const cache_key = cache_key_buf[0..ck_pos];

            if (db_cache_enabled) {
                if (cacheGet(cache_key)) |cached_body| {
                    defer allocator.free(cached_body); // owned copy (F12)
                    sendResponseFn(stream, 200, "application/json", cached_body);
                    return;
                }
            }

            const acq = acquireConn() orelse {
                sendResponseFn(stream, 503, "application/json", "{\"error\": \"Database connection unavailable\"}");
                return;
            };
            const conn = acq.conn;
            // Release ONLY a pool-owned connection; a pinned/thread-owned one is
            // still referenced elsewhere and releasing it would alias it (F3).
            defer releaseAcquired(acq);

            const result_opt = execWithParams(conn, entry.custom_sql, param_values[0..param_count], entry.cache_name, null);
            if (result_opt) |result| {
                defer result.deinit();

                if (entry.op == .custom_query_single) {
                    // Single row
                    if (result.next() catch null) |row| {
                        var json_stack3: [8192]u8 = undefined;
                        const ser3 = serializeRowAlloc(row, result.column_names, &json_stack3);
                        defer if (ser3.heap_buf) |h| allocator.free(h);
                        if (ser3.json.len == 0) {
                            sendResponseFn(stream, 500, "application/json", "{\"error\": \"Row too large to serialize\"}");
                            return;
                        }
                        cachePut(cache_key, ser3.json, entry.table);
                        sendResponseFn(stream, 200, "application/json", ser3.json);
                    } else {
                        sendResponseFn(stream, 404, "application/json", "{\"error\": \"Not found\"}");
                    }
                } else {
                    // Multi-row — JSON array
                    var out_buf = allocator.alloc(u8, 65536) catch {
                        sendResponseFn(stream, 500, "application/json", "{\"error\": \"Out of memory\"}");
                        return;
                    };
                    defer allocator.free(out_buf);

                    var out_pos: usize = 0;
                    out_buf[out_pos] = '[';
                    out_pos += 1;

                    var row_count: usize = 0;
                    while (result.next() catch null) |row| {
                        if (row_count > 0) {
                            out_buf[out_pos] = ',';
                            out_pos += 1;
                        }
                        var row_stack2: [8192]u8 = undefined;
                        const rser = serializeRowAlloc(row, result.column_names, &row_stack2);
                        defer if (rser.heap_buf) |h| allocator.free(h);
                        if (rser.json.len == 0) break;
                        if (out_pos + rser.json.len + 2 > out_buf.len) break;
                        @memcpy(out_buf[out_pos..][0..rser.json.len], rser.json);
                        out_pos += rser.json.len;
                        row_count += 1;
                    }

                    out_buf[out_pos] = ']';
                    out_pos += 1;

                    const resp = out_buf[0..out_pos];
                    cachePut(cache_key, resp, entry.table);
                    sendResponseFn(stream, 200, "application/json", resp);
                }
            } else {
                sendResponseFn(stream, 500, "application/json", "{\"error\": \"Query failed\"}");
            }
        },
    }
}
fn execWithParams(conn: *pg.Conn, sql: []const u8, values: []const ?[]const u8, cache_name: ?[]const u8, param_oids: ?[]const i32) ?*pg.Result {
    const opts = pg.Conn.QueryOpts{ .column_names = true, .cache_name = cache_name, .param_oids = param_oids };
    // Fast path: 0-8 params use compile-time tuples (no overhead)
    // Each value is ?[]const u8 — null = SQL NULL, non-null = text param
    return switch (values.len) {
        0 => conn.queryOpts(sql, .{}, opts) catch return null,
        1 => conn.queryOpts(sql, .{values[0]}, opts) catch return null,
        2 => conn.queryOpts(sql, .{ values[0], values[1] }, opts) catch return null,
        3 => conn.queryOpts(sql, .{ values[0], values[1], values[2] }, opts) catch return null,
        4 => conn.queryOpts(sql, .{ values[0], values[1], values[2], values[3] }, opts) catch return null,
        5 => conn.queryOpts(sql, .{ values[0], values[1], values[2], values[3], values[4] }, opts) catch return null,
        6 => conn.queryOpts(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5] }, opts) catch return null,
        7 => conn.queryOpts(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5], values[6] }, opts) catch return null,
        8 => conn.queryOpts(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7] }, opts) catch return null,
        // Slow path: 9+ params use Stmt API with runtime bind loop
        else => execWithParamsDynamic(conn, sql, values, opts),
    };
}

/// Execute a query with >8 parameters using pg.zig's Stmt API directly.
/// Uses runtime bind loop instead of compile-time tuple expansion.
/// Respects the per-connection prepared statement cache (cache_name in opts).
fn execWithParamsDynamic(conn: *pg.Conn, sql: []const u8, values: []const ?[]const u8, opts: pg.Conn.QueryOpts) ?*pg.Result {
    const name = opts.cache_name;

    // Check per-connection prepared statement cache
    if (name) |n| {
        if (conn._prepared_statements.getPtr(n)) |describe| {
            // CACHE HIT: skip PARSE, use cached statement metadata
            trace("execWithParamsDynamic: CACHE HIT name={s} conn={x}", .{ n, @intFromPtr(conn) });
            var stmt = pg.Stmt.fromDescribe(conn, describe, opts) catch return null;
            errdefer stmt.deinit();

            conn.stmtCacheMoveToEnd(n);

            // stmt.deinit() runs endFlow() + frees stmt's arena on every failure,
            // so a mid-flow pooled connection is not returned to the pool desynced
            // (the errdefer above never fires — this fn returns ?*Result, not !).
            conn._reader.startFlow(stmt.arena.allocator(), opts.timeout) catch {
                stmt.deinit();
                return null;
            };
            conn.write(&.{ 'S', 0, 0, 0, 4 }) catch {
                stmt.deinit();
                return null;
            };
            stmt.buf.reset();
            stmt.prepareForBind(@intCast(describe.param_oids.len)) catch {
                stmt.deinit();
                return null;
            };

            for (values) |val| {
                stmt.bind(val) catch {
                    stmt.deinit();
                    return null;
                };
            }
            return stmt.execute() catch {
                stmt.deinit();
                return null;
            };
        }
    }

    // CACHE MISS: prepare the statement
    trace("execWithParamsDynamic: CACHE MISS name={s} conn={x} sql={s}", .{
        if (name) |n| n else "(none)",
        @intFromPtr(conn),
        sql[0..@min(sql.len, 80)],
    });

    if (name) |n| {
        // Named statement: prepare with describe_allocator so we can cache
        var stmt = pg.Stmt.init(conn, opts) catch return null;
        errdefer stmt.deinit();

        // Evict LRU if cache is full
        if (conn._stmt_cache_order.items.len >= conn._stmt_cache_max) {
            conn.stmtCacheEvictOldest();
        }

        var describe_arena = std.heap.ArenaAllocator.init(conn._allocator);
        errdefer describe_arena.deinit();
        // BEFORE the put: describe_arena is still ours, so free both stmt (which
        // also runs endFlow()) and describe_arena on failure. The errdefers above
        // never fire (this fn returns ?*Result, not !), so do it explicitly.
        stmt.prepare(sql, describe_arena.allocator()) catch {
            stmt.deinit();
            describe_arena.deinit();
            return null;
        };

        const owned_name = describe_arena.allocator().dupe(u8, n) catch {
            stmt.deinit();
            describe_arena.deinit();
            return null;
        };
        conn._prepared_statements.put(conn._allocator, owned_name, .{
            .arena = describe_arena,
            .param_oids = stmt.param_oids,
            .result_state = stmt.result_state,
        }) catch {
            stmt.deinit();
            describe_arena.deinit();
            return null;
        };
        // From here on describe_arena is MOVED into the map — free only stmt, never
        // describe_arena (the map owns it; a local deinit would double-free).
        conn._stmt_cache_order.append(conn._allocator, owned_name) catch {};

        for (values) |val| {
            stmt.bind(val) catch {
                stmt.deinit();
                return null;
            };
        }
        return stmt.execute() catch {
            stmt.deinit();
            return null;
        };
    } else {
        // Unnamed statement: no caching needed
        var stmt = pg.Stmt.init(conn, opts) catch return null;
        errdefer stmt.deinit();

        // stmt.deinit() runs endFlow() so the pooled conn isn't left mid-flow.
        stmt.prepare(sql, null) catch {
            stmt.deinit();
            return null;
        };

        for (values) |val| {
            stmt.bind(val) catch {
                stmt.deinit();
                return null;
            };
        }
        return stmt.execute() catch {
            stmt.deinit();
            return null;
        };
    }
}

/// Execute a DML statement (INSERT/UPDATE/DELETE) with >8 parameters.
/// Uses conn.exec via queryOpts internally, then drains the result.
fn execDmlDynamic(conn: *pg.Conn, sql: []const u8, values: []const ?[]const u8, opts: pg.Conn.QueryOpts) ?i64 {
    var result = execWithParamsDynamic(conn, sql, values, opts) orelse return null;
    defer result.deinit();
    // Drain the result to complete the protocol exchange
    while (result.next() catch null) |_| {}
    return 0;
}

// ── Python C API functions ───────────────────────────────────────────────────

pub fn db_configure(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_str: [*c]const u8 = null;
    var pool_size: c_int = 16;
    var connect_timeout_ms: c_int = 10000; // 10s default
    var query_timeout_ms: c_int = 0; // 0 = no query timeout
    var max_queries_per_conn: c_longlong = 0; // 0 = unlimited (no rotation)
    var max_conn_lifetime_s: c_longlong = 0; // 0 = unlimited (seconds)
    if (c.PyArg_ParseTuple(args, "si|iiLL", &conn_str, &pool_size, &connect_timeout_ms, &query_timeout_ms, &max_queries_per_conn, &max_conn_lifetime_s) == 0) return null;

    const uri_str = std.mem.span(conn_str);
    // Pool size rules (task #186, informed by task #193):
    //
    // 1. Explicit pool_size (1..1024) is honored as-is. The old limit of
    //    128 silently fell through to auto-tune for any caller passing a
    //    larger value, which was surprising; 1024 matches conf.py's
    //    POOL_SIZE max_value.
    //
    // 2. pool_size = 0 (or out of range) triggers auto-tune. The formula
    //    floor is 32, NOT cpu_count * 2, because pg.zig pins one
    //    connection per Zig HTTP worker thread via tryThreadOwned. The
    //    default thread count is 24 (see conf.py DEFAULT_THREAD_POOL_SIZE)
    //    so a pool below 24 creates the pathological regime where excess
    //    worker threads block forever in pool.acquire with no wakeup path
    //    (slot-holding threads never release). A floor of 32 gives 8
    //    slots of headroom above the default 24-thread server for
    //    debug endpoints, pool heartbeat, auto-tuner, and background
    //    tasks. On big machines, cpu_count * 2 takes over above 32.
    //    Cap at 128 to keep PostgreSQL max_connections bounded.
    const size: u16 = if (pool_size > 0 and pool_size <= 1024)
        @intCast(pool_size)
    else blk: {
        const cpu_count = std.Thread.getCpuCount() catch 4;
        break :blk @intCast(@min(@max(cpu_count * 2, 32), 128));
    };

    // Parse postgres://user:pass@host:port/database
    const uri = std.Uri.parse(uri_str) catch {
        py.setError("Invalid connection string: {s}", .{uri_str});
        return null;
    };

    // Extract and DUPLICATE strings — the URI slices point into the Python
    // string buffer which may be garbage collected after this function returns.
    // The pool holds these strings for the lifetime of the pool (reconnects etc.)
    const host_str: []const u8 = if (uri.host) |h|
        allocator.dupe(u8, h.percent_encoded) catch "127.0.0.1"
    else
        "127.0.0.1";
    const user_str: []const u8 = if (uri.user) |u|
        allocator.dupe(u8, u.percent_encoded) catch "postgres"
    else
        allocator.dupe(u8, defaultPgUser()) catch "postgres";
    const db_name: []const u8 = if (uri.path.percent_encoded.len > 1)
        allocator.dupe(u8, uri.path.percent_encoded[1..]) catch "postgres"
    else
        "postgres";
    const pw_str: ?[]const u8 = if (uri.password) |p|
        allocator.dupe(u8, p.percent_encoded) catch null
    else
        null;

    // Connect timeout: clamp to sane range (100ms..300s), default 10s
    const conn_timeout: u32 = if (connect_timeout_ms > 0)
        @intCast(@min(@max(connect_timeout_ms, 100), 300_000))
    else
        10_000;

    // Build startup parameters for session-level settings.
    // These are sent during PostgreSQL connection startup, so they apply to
    // ALL connections including pool reconnects — no per-query overhead.
    var startup_params: ?std.StringHashMap([]const u8) = null;
    if (query_timeout_ms > 0) {
        var params = std.StringHashMap([]const u8).init(allocator);
        // Format timeout value as string (PostgreSQL expects text in startup message)
        const timeout_str = std.fmt.allocPrint(allocator, "{d}", .{query_timeout_ms}) catch {
            py.setError("Failed to format query timeout", .{});
            return null;
        };
        params.put("statement_timeout", timeout_str) catch {
            allocator.free(timeout_str);
            py.setError("Failed to set startup parameters", .{});
            return null;
        };
        startup_params = params;
    }

    // Allocate a pool slot in the registry.
    const new_pool = pg.Pool.init(allocator, .{
        .size = size,
        .timeout = conn_timeout,
        .max_queries_per_conn = if (max_queries_per_conn > 0) @intCast(max_queries_per_conn) else 0,
        .max_conn_lifetime = if (max_conn_lifetime_s > 0) @intCast(max_conn_lifetime_s) else 0,
        .connect = .{
            .port = uri.port,
            .host = host_str,
            .connect_timeout_ms = conn_timeout,
        },
        .auth = .{
            .username = user_str,
            .database = db_name,
            .password = pw_str,
            .timeout = conn_timeout,
            .startup_parameters = startup_params,
        },
    }) catch |err| {
        trace("db_configure: FAILED to create pool for db={s} host={s} user={s} err={}", .{ db_name, host_str, user_str, err });
        // Surface the specific Zig error variant in the Python message
        // (was: every cause collapsed into "Failed to connect"). Now you
        // see e.g. "...err=ConnectionRefused" vs "...err=DnsResolveFailed"
        // vs "...err=SocketCreateFailed" vs "...err=AuthenticationFailed".
        py.setError("Failed to connect to database: {s} (err={s})", .{ uri_str, @errorName(err) });
        return null;
    };

    // Find a free slot in the pool registry, or grow it
    pool_registry_mutex.lock();
    defer pool_registry_mutex.unlock();

    var handle: i64 = -1;
    for (pool_registry.items, 0..) |slot, i| {
        if (slot == null) {
            pool_registry.items[i] = new_pool;
            handle = @intCast(i);
            break;
        }
    }

    if (handle < 0) {
        // No free slots — append a new one
        pool_registry.append(allocator, new_pool) catch {
            py.setError("Failed to register pool (out of memory)", .{});
            new_pool.deinit();
            return null;
        };
        handle = @intCast(pool_registry.items.len - 1);
    }

    @atomicStore(i64, &active_pool_handle, handle, .release);

    trace("db_configure: handle={d} pool={x} db={s} pool_count={d}", .{ handle, @intFromPtr(new_pool), db_name, pool_registry.items.len });
    return py.newInt(handle);
}

// Python C API: _db_set_active_handle(handle) — set which pool handle the Zig server uses.
// Allows Python to pre-create a pool via _db_configure and share it with the Zig server,
// instead of the server creating its own separate pool.
pub fn db_set_active_handle(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;

    if (retainPoolByHandle(@intCast(handle), .exact)) |pool| {
        defer pool.unref();
        @atomicStore(i64, &active_pool_handle, @intCast(handle), .release);
        trace("db_set_active_handle: handle={d} pool={x} db={s}", .{ handle, @intFromPtr(pool), if (pool._opts.auth.database) |d| d else "null" });
    }

    return py.pyNone();
}

// Python C API: _db_close_pool(handle) — close a specific pool without affecting others
pub fn db_close_pool(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;

    // Detach the pool from the registry under the mutex, then drop the
    // registry's strong reference OUTSIDE it. Teardown (pool.deinit) runs
    // when the LAST reference drops — which may be right here (nothing in
    // flight), or later on whichever thread returns the final checked-out
    // connection. In-flight operations on other threads are safe either way:
    //   * ops that already resolved the pool hold a reference (transient or
    //     checkout), so the memory stays alive until they finish;
    //   * ops that resolve AFTER this detach find no registry entry and fail
    //     with a clean "no pool" error.
    // Pinned slots are NOT force-cleared here: their owners drop them through
    // the normal release path, which now outlives the close safely. (The old
    // force-clear raced with a concurrent acquire claiming a slot between the
    // scan and deinit — that release then touched freed pool memory.)
    var closing: ?*pg.Pool = null;
    if (handle >= 0) {
        pool_registry_mutex.lock();
        if (handle < pool_registry.items.len) {
            const idx: usize = @intCast(handle);
            if (pool_registry.items[idx]) |pool| {
                pool.markClosing();
                pool_registry.items[idx] = null;
                if (@atomicLoad(i64, &active_pool_handle, .acquire) == handle) @atomicStore(i64, &active_pool_handle, -1, .release);
                closing = pool;
            }
        }
        pool_registry_mutex.unlock();
    }
    if (closing) |pool| {
        trace("db_close_pool: handle={d} pool={x} db={s} pool_count={d}", .{ handle, @intFromPtr(pool), if (pool._opts.auth.database) |d| d else "null", registeredPoolCount() });
        // Reclaim idle thread-owned connections so they don't pin the pool
        // alive; busy ones are released lazily by their owners (see
        // reapIdleThreadSlotsForPool).
        reapIdleThreadSlotsForPool(handle);
        pool.unref();
    }
    return py.pyNone();
}

/// _db_pipeline(pool_handle, queries_list) → list[list[tuple]]
/// Execute N queries in a single pipeline (one network round-trip for all).
/// queries_list = [sql1, sql2, ...] (simple queries with params already bound)
/// Returns: list of result lists, each result list contains tuples of column values.
pub fn db_pipeline(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var queries_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lO", &pool_handle, &queries_obj) == 0) return null;

    const queries_list = queries_obj orelse return null;
    const n_queries = c.PySequence_Size(queries_list);
    if (n_queries < 0) {
        py.setError("Pipeline queries must be a list", .{});
        return null;
    }
    if (n_queries == 0) {
        return c.PyList_New(0);
    }

    // Extract SQL strings from Python list
    const n: usize = @intCast(n_queries);
    const pipeline_queries = allocator.alloc(pg.Conn.PipelineQuery, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer allocator.free(pipeline_queries);

    // Keep Python string references alive during the call
    const py_strs = allocator.alloc(?*c.PyObject, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer {
        for (py_strs) |ps| if (ps) |p| c.Py_DecRef(p);
        allocator.free(py_strs);
    }

    for (0..n) |i| {
        const item = c.PySequence_GetItem(queries_list, @intCast(i)) orelse {
            py.setError("Failed to get query at index {d}", .{i});
            return null;
        };
        py_strs[i] = item;

        if (c.PyUnicode_Check(item) != 0) {
            if (c.PyUnicode_AsUTF8(item)) |s| {
                pipeline_queries[i] = .{ .sql = std.mem.span(s) };
            } else {
                py.setError("Failed to encode query {d} as UTF-8", .{i});
                return null;
            }
        } else {
            py.setError("Pipeline query {d} must be a string", .{i});
            return null;
        }
    }

    // Acquire connection
    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("Database connection unavailable for pipeline", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Execute pipeline
    var pipeline_result = conn.pipeline(pipeline_queries) catch {
        py.setError("Pipeline execution failed", .{});
        return null;
    };
    defer pipeline_result.deinit();

    // Build Python result: list of lists of tuples
    const py_results = c.PyList_New(@intCast(n)) orelse return null;

    for (pipeline_result.results, 0..) |*entry, qi| {
        if (entry.err != null) {
            // Error for this query — return empty list
            const empty = c.PyList_New(0) orelse {
                c.Py_DecRef(py_results);
                return null;
            };
            _ = c.PyList_SetItem(py_results, @intCast(qi), empty);
            continue;
        }

        const n_rows = entry.rows.len;
        const py_query_result = c.PyList_New(@intCast(n_rows)) orelse {
            c.Py_DecRef(py_results);
            return null;
        };

        for (entry.rows, 0..) |row_vals, ri| {
            const n_cols = row_vals.len;
            const py_tuple = c.PyTuple_New(@intCast(n_cols)) orelse continue;

            for (row_vals, 0..) |val, ci| {
                const py_val: *c.PyObject = if (val.is_null)
                    py.pyNone()
                else blk: {
                    // Simple query returns text format — convert to Python string
                    // For OID-aware conversion, check entry.column_oids[ci]
                    if (ci < entry.column_oids.len) {
                        const oid = entry.column_oids[ci];
                        break :blk convertTextValue(oid, val.data) orelse py.newString(val.data) orelse py.pyNone();
                    }
                    break :blk py.newString(val.data) orelse py.pyNone();
                };
                _ = c.PyTuple_SetItem(py_tuple, @intCast(ci), py_val);
            }

            _ = c.PyList_SetItem(py_query_result, @intCast(ri), py_tuple);
        }

        _ = c.PyList_SetItem(py_results, @intCast(qi), py_query_result);
    }

    return py_results;
}

/// Convert a text-format PostgreSQL value to Python object based on OID.
fn convertTextValue(oid: i32, data: []const u8) ?*c.PyObject {
    return switch (oid) {
        // int2, int4, int8 — parse text integer
        21, 23, 20 => blk: {
            const val = std.fmt.parseInt(i64, data, 10) catch break :blk null;
            break :blk py.newInt(val);
        },
        // float4, float8
        700, 701 => blk: {
            const val = std.fmt.parseFloat(f64, data) catch break :blk null;
            break :blk c.PyFloat_FromDouble(val);
        },
        // bool
        16 => if (data.len > 0 and data[0] == 't') py.pyTrue() else py.pyFalse(),
        // text, varchar, name, char, bpchar — already text
        25, 1043, 19, 18, 1042 => py.newString(data),
        // json, jsonb — parse JSON
        114, 3802 => json_parser.jsonToPython(data),
        // Everything else — return as string
        else => null,
    };
}

/// _db_pool_stats(pool_handle) → dict with pool statistics.
/// Returns: {total, available, in_use, missing, thread_owned, pools_registered, active_handle}
pub fn db_pool_stats(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "l", &pool_handle) == 0) return null;

    const py_dict = c.PyDict_New() orelse return null;

    // Pool-specific stats
    {
        if (retainPoolByHandle(@intCast(pool_handle), .exact)) |pool| {
            defer pool.unref();
            const stats = pool.stats();
            _ = c.PyDict_SetItemString(py_dict, "total", py.newInt(@intCast(stats.size)));
            _ = c.PyDict_SetItemString(py_dict, "available", py.newInt(@intCast(stats.available)));
            _ = c.PyDict_SetItemString(py_dict, "in_use", py.newInt(@intCast(stats.in_use)));
            _ = c.PyDict_SetItemString(py_dict, "missing", py.newInt(@intCast(stats.missing)));
            // Contention instrumentation (task #193) — expose pg.zig
            // pool acquire/wait counters so Python can sample them
            // during wrk runs to build queue-depth histograms.
            _ = c.PyDict_SetItemString(py_dict, "waiters", py.newInt(@intCast(stats.waiters)));
            _ = c.PyDict_SetItemString(py_dict, "max_waiters", py.newInt(@intCast(stats.max_waiters)));
            _ = c.PyDict_SetItemString(py_dict, "wait_count", py.newInt(@intCast(stats.wait_count)));
            _ = c.PyDict_SetItemString(py_dict, "wait_total_ns", py.newInt(@intCast(stats.wait_total_ns)));
            _ = c.PyDict_SetItemString(py_dict, "wait_max_ns", py.newInt(@intCast(stats.wait_max_ns)));
            _ = c.PyDict_SetItemString(py_dict, "acquire_count", py.newInt(@intCast(stats.acquire_count)));
            _ = c.PyDict_SetItemString(py_dict, "timeout_count", py.newInt(@intCast(stats.timeout_count)));

            // Count thread-owned slots for this pool
            var thread_owned: i64 = 0;
            for (&thread_slots) |*slot| {
                if (slot.owner_tid.load(.acquire) != 0 and slot.pool_handle.load(.acquire) == pool_handle) {
                    thread_owned += 1;
                }
            }
            _ = c.PyDict_SetItemString(py_dict, "thread_owned", py.newInt(thread_owned));

            // Database name
            if (pool._opts.auth.database) |db_name| {
                if (py.newString(db_name)) |s| {
                    _ = c.PyDict_SetItemString(py_dict, "database", s);
                    c.Py_DecRef(s);
                }
            }
        }
    }

    // Global stats
    _ = c.PyDict_SetItemString(py_dict, "pools_registered", py.newInt(@intCast(registeredPoolCount())));
    _ = c.PyDict_SetItemString(py_dict, "active_handle", py.newInt(@atomicLoad(i64, &active_pool_handle, .acquire)));

    return py_dict;
}

// Python C API: _db_release_thread_conn(pool_handle)
// Release the current thread's owned connection back to the pool.
// Called when a Django connection is closed or when switching pools.
pub fn db_release_thread_conn(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;
    const tid = std.Thread.getCurrentId();
    releaseThreadSlot(tid, handle);
    return py.pyNone();
}

// Python C API: _db_mark_offload_worker() — mark THIS thread as a DB-offload
// worker so it acquires/releases a pool connection per op instead of pinning one
// (see `offload_worker`). Called once per worker thread from the offload
// ThreadPoolExecutor's `initializer`.
pub fn db_mark_offload_worker(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    offload_worker = true;
    return py.pyNone();
}

// Python C API: _db_set_active_pool(handle) — switch which pool acquireConn uses
pub fn db_set_active_pool(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = 0;
    if (c.PyArg_ParseTuple(args, "l", &handle) == 0) return null;
    trace("db_set_active_pool: handle={d} pool_count={d}", .{ handle, registeredPoolCount() });
    @atomicStore(i64, &active_pool_handle, @intCast(handle), .release);
    // Clear any pinned connection that belongs to a DIFFERENT pool.
    // When switching pools, stale pinned connections from the old pool
    // must not be used — they point to a different database context.
    const ap = @atomicLoad(i64, &active_pinned_handle, .acquire);
    if (ap >= 0) {
        const pconn = pinnedGet(@intCast(ap));
        if (pconn) |conn| {
            if (retainPoolByHandle(@intCast(handle), .exact)) |target| {
                defer target.unref();
                if (conn._pool != target) {
                    // Pinned conn is from a different pool — clear it
                    trace("db_set_active_pool: clearing stale pinned handle={d} (wrong pool)", .{ap});
                    @atomicStore(i64, &active_pinned_handle, -1, .release);
                }
            }
        }
    }
    return py.pyNone();
}

pub fn db_add_route(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var method_c: [*c]const u8 = null;
    var path_c: [*c]const u8 = null;
    var op_c: [*c]const u8 = null;
    var table_c: [*c]const u8 = null;
    var pk_col_c: [*c]const u8 = null;
    var pk_param_c: [*c]const u8 = null;
    var columns_c: [*c]const u8 = null; // comma-separated column names

    if (c.PyArg_ParseTuple(args, "sssssss", &method_c, &path_c, &op_c, &table_c, &pk_col_c, &pk_param_c, &columns_c) == 0) return null;

    const method_s = std.mem.span(method_c);
    const path_s = std.mem.span(path_c);
    const op_s = std.mem.span(op_c);
    const table_s = std.mem.span(table_c);
    const pk_col_s = std.mem.span(pk_col_c);
    const pk_param_s = std.mem.span(pk_param_c);
    const columns_s = std.mem.span(columns_c);

    const op: DbOp = if (std.mem.eql(u8, op_s, "select_one"))
        .select_one
    else if (std.mem.eql(u8, op_s, "select_list"))
        .select_list
    else if (std.mem.eql(u8, op_s, "insert"))
        .insert
    else if (std.mem.eql(u8, op_s, "delete"))
        .delete
    else if (std.mem.eql(u8, op_s, "custom_query"))
        .custom_query
    else if (std.mem.eql(u8, op_s, "custom_query_single"))
        .custom_query_single
    else {
        py.setError("Invalid db op: {s}", .{op_s});
        return null;
    };

    // Validate table name for CRUD ops (custom queries pass SQL as table)
    if (op != .custom_query and op != .custom_query_single) {
        if (!isValidIdentifier(table_s)) {
            py.setError("Invalid table name: {s}", .{table_s});
            return null;
        }
    }

    // Parse column names — stack for <=64, heap for more (registration-time only)
    var stack_cols: [64][]const u8 = undefined;
    var ncols: usize = 0;
    var heap_cols: ?[][]const u8 = null;
    if (columns_s.len > 0) {
        // Count columns first
        var count_iter = std.mem.splitScalar(u8, columns_s, ',');
        var total_cols: usize = 0;
        while (count_iter.next()) |_| total_cols += 1;

        const cols_buf: [][]const u8 = if (total_cols <= 64)
            &stack_cols
        else blk: {
            heap_cols = allocator.alloc([]const u8, total_cols) catch return null;
            break :blk heap_cols.?;
        };

        var col_iter = std.mem.splitScalar(u8, columns_s, ',');
        while (col_iter.next()) |col| {
            const trimmed = std.mem.trim(u8, col, " ");
            cols_buf[ncols] = allocator.dupe(u8, trimmed) catch return null;
            ncols += 1;
        }
    }

    const cols_src = if (heap_cols) |h| h[0..ncols] else stack_cols[0..ncols];
    const columns_owned = allocator.dupe([]const u8, cols_src) catch return null;
    if (heap_cols) |h| allocator.free(h);
    const pk_col = if (pk_col_s.len > 0) allocator.dupe(u8, pk_col_s) catch return null else null;
    const pk_param = if (pk_param_s.len > 0) allocator.dupe(u8, pk_param_s) catch return null else null;
    const table = allocator.dupe(u8, table_s) catch return null;

    // For custom queries, columns_s contains the raw SQL (passed via the columns arg)
    // and pk_col_s contains comma-separated param names
    const custom_sql = if (op == .custom_query or op == .custom_query_single)
        allocator.dupe(u8, table_s) catch return null // table_s carries the SQL for custom queries
    else
        "";

    // For custom queries, parse param names from pk_col_s — stack for <=64
    var stack_pnames: [64][]const u8 = undefined;
    var npnames: usize = 0;
    var heap_pnames: ?[][]const u8 = null;
    if ((op == .custom_query or op == .custom_query_single) and pk_col_s.len > 0) {
        var count_pn_iter = std.mem.splitScalar(u8, pk_col_s, ',');
        var total_pnames: usize = 0;
        while (count_pn_iter.next()) |_| total_pnames += 1;

        const pn_buf: [][]const u8 = if (total_pnames <= 64)
            &stack_pnames
        else blk: {
            heap_pnames = allocator.alloc([]const u8, total_pnames) catch return null;
            break :blk heap_pnames.?;
        };

        var pn_iter = std.mem.splitScalar(u8, pk_col_s, ',');
        while (pn_iter.next()) |pn| {
            const trimmed = std.mem.trim(u8, pn, " ");
            pn_buf[npnames] = allocator.dupe(u8, trimmed) catch return null;
            npnames += 1;
        }
    }
    const pn_src = if (heap_pnames) |h| h[0..npnames] else stack_pnames[0..npnames];
    const param_names_owned = allocator.dupe([]const u8, pn_src) catch return null;
    if (heap_pnames) |h| allocator.free(h);
    // Generate prepared statement cache name: "db_METHOD_path"
    var cache_name_counter: usize = 0;
    _ = @atomicRmw(usize, &cache_name_counter, .Add, 1, .seq_cst);
    const cache_name = std.fmt.allocPrint(allocator, "db_{s}_{s}", .{ method_s, path_s }) catch null;

    const entry = DbRouteEntry{
        .op = op,
        .table = table,
        .columns = columns_owned,
        .pk_column = pk_col,
        .pk_param = pk_param,
        .select_sql = if (pk_col) |pk| buildSelectOneSql(table, pk) else buildSelectListSql(table),
        .insert_sql = if (ncols > 0 and op == .insert) buildInsertSql(table, columns_owned) else "",
        .delete_sql = if (pk_col) |pk| buildDeleteSql(table, pk) else "",
        .custom_sql = custom_sql,
        .param_names = param_names_owned,
        .cache_name = cache_name,
        .schema = null,
    };

    const key = std.fmt.allocPrint(allocator, "{s} {s}", .{ method_s, path_s }) catch return null;
    getDbRoutes().put(key, entry) catch return null;

    // Register in router
    const rt = @import("server.zig").getRouter();
    rt.addRoute(method_s, path_s, key) catch return null;

    std.debug.print("[DB] Registered: {s} {s} -> {s}.{s} ({s})\n", .{ method_s, path_s, table_s, if (pk_col) |pk| pk else "*", op_s });
    return py.pyNone();
}

// ── _db_query(pool_handle, sql, params_list) → list of tuples ───────────────
// General-purpose SQL query execution for Django's ORM cursor.
// pool_handle selects which pool to acquire a connection from.
// Use -1 to use the legacy global pool (for backward compat with scripts/benchmarks).

// ── Cached plan error detection ────────────────────────────────────────────
//
// When a prepared statement's cached plan becomes stale (DDL changes the
// schema after the statement was prepared), PostgreSQL returns an error
// with one of these SQLSTATE codes. We evict the broken entry from the
// statement cache and retry ONCE with a fresh parse.
//
// Using SQLSTATE codes (precise, locale-independent) instead of substring
// matching on the error message text (fragile, could false-positive on
// unrelated errors containing "cached plan" or miss errors that don't).
//
// Codes checked (from PostgreSQL errcodes-appendix):
//   42P18 — cached plan must not change result type
//   42P01 — undefined_table (table dropped/renamed after prepare)
//   42703 — undefined_column (column dropped/renamed after prepare)
//   0A000 — feature_not_supported (plan cache stale in edge cases)

fn isCachedPlanError(err: pg.Error) bool {
    const code = err.code;
    if (code.len < 5) return false;
    return std.mem.eql(u8, code, "42P18") or
        std.mem.eql(u8, code, "42P01") or
        std.mem.eql(u8, code, "42703") or
        std.mem.eql(u8, code, "0A000");
}

pub fn db_query(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    var params_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lsO", &pool_handle, &sql_c, &params_obj) == 0) return null;

    const sql = std.mem.span(sql_c);
    trace("db_query: handle={d} sql={s}", .{ pool_handle, sql[0..@min(sql.len, 120)] });

    // Extract params from Python list — stack-first, heap for >64
    var stack_strs: [64]?[]const u8 = undefined;
    var stack_bufs: [64][64]u8 = undefined;
    var pbufs = initParamBufs(params_obj, &stack_strs, &stack_bufs) orelse return null;
    defer pbufs.deinit();
    var overflow_stack: [1024]u8 = undefined;
    var overflow_buf: []u8 = &overflow_stack;
    var overflow_pos: usize = 0;
    var overflow_heap: ?[]u8 = null;
    defer if (overflow_heap) |hb| allocator.free(hb);
    var param_count: usize = pbufs.count;
    extractParams(params_obj, pbufs.strs, pbufs.num_bufs, &param_count, &overflow_buf, &overflow_pos, &overflow_heap) orelse return null;

    // Acquire connection — pool or pinned, based on handle
    const acq = acquireConnByHandle(pool_handle) orelse {
        trace("db_query: FAILED acquireConnByHandle handle={d} pool_count={d} sql={s}", .{ pool_handle, registeredPoolCount(), sql[0..@min(sql.len, 60)] });
        py.setError("Database connection unavailable. Call _db_configure first.", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Execute query with prepared statement caching.
    // First call: Parse→Describe→Bind→Execute (full round trip).
    // Subsequent calls with same SQL: Bind→Execute only (skips query planning).
    //
    // Self-healing: if query fails with "cached plan must not change result type"
    // (happens after schema changes on other nodes), clear the cache and retry once.
    const values = pbufs.strs[0..param_count];
    const prep_name = getPreparedName(sql);
    const cache_name: ?[]const u8 = if (prep_name.len > 0) prep_name else null;
    // Inferred Parse-time param type OIDs (fixes `col = ANY(ARRAY[$1,...])`).
    var oid_stack: [64]i32 = undefined;
    var poids = computeParamOids(sql, params_obj, &oid_stack);
    defer poids.deinit();
    trace("db_query: handle={d} cache_name={s} conn={x} sql={s}", .{
        pool_handle,
        if (cache_name) |cn| cn else "(none)",
        @intFromPtr(conn),
        sql[0..@min(sql.len, 80)],
    });
    var result = execWithParams(conn, sql, values, cache_name, poids.oids) orelse retry: {
        if (conn.err) |err| {
            trace("db_query: ERROR code={s} msg={s}", .{ err.code, err.message[0..@min(err.message.len, 120)] });
            if (isCachedPlanError(err)) {
                trace("db_query: cached plan error (SQLSTATE {s}), evicting + retrying", .{err.code});
                conn.clearErr();
                if (cache_name) |cn| conn.stmtCacheEvict(cn);
                _ = db_clear_stmt_cache(null, null);
                // Invalidate column metadata caches — the column set may
                // have changed due to ALTER TABLE ADD/DROP COLUMN.
                invalidateColumnCaches(hashSql(sql));
                break :retry execWithParams(conn, sql, values, null, poids.oids);
            }
        }
        setPgError(conn, "Query failed", sql);
        return null;
    } orelse {
        setPgError(conn, "Query failed (retry)", sql);
        return null;
    };
    defer result.deinit();

    // Build Python list of tuples from result rows.
    // Two-phase: first collect all tuples into a Zig array, then build
    // a pre-sized PyList with PyList_SET_ITEM (no realloc, no bounds check).
    const col_count = result.column_names.len;

    // Phase 1: Collect tuples into a growable array
    var tuple_buf: std.ArrayListUnmanaged(*c.PyObject) = .empty;
    defer tuple_buf.deinit(allocator);
    // Pre-allocate for 64 rows — covers most queries without realloc
    tuple_buf.ensureTotalCapacity(allocator, 64) catch {};

    // Static stack buffer handles the common case (ints, bools, short strings).
    // Only heap-allocates if a value overflows it — once, reused for the rest.
    const STACK_SIZE = 4096;
    var stack_buf: [STACK_SIZE]u8 = undefined;
    var heap_buf: ?[]u8 = null;
    defer if (heap_buf) |hb| allocator.free(hb);

    var val_buf: []u8 = &stack_buf;

    while (true) {
        const row = result.next() catch {
            // Mid-stream failure — recover the connection and surface the error
            // instead of returning the partial rows collected so far as success.
            recoverAfterRowError(result, conn);
            for (tuple_buf.items) |t| c.Py_DecRef(t);
            setPgError(conn, "Query failed mid-result", sql);
            return null;
        } orelse break;
        const py_tuple = c.PyTuple_New(@intCast(col_count)) orelse continue;
        for (0..col_count) |ci| {
            // Check null first — avoids all conversion work
            if (row.values[@intCast(ci)].is_null) {
                _ = c.PyTuple_SetItem(py_tuple, @intCast(ci), py.pyNone());
                continue;
            }

            const oid = row.oids[@intCast(ci)];
            const py_val: ?*c.PyObject = switch (oid) {
                // Integers → Python int (direct binary read, no string intermediary)
                21 => blk: { // int2
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk py.newInt(@as(i64, std.mem.readInt(i16, data[0..2], .big)));
                },
                23 => blk: { // int4
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk py.newInt(@as(i64, std.mem.readInt(i32, data[0..4], .big)));
                },
                20 => blk: { // int8
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk py.newInt(std.mem.readInt(i64, data[0..8], .big));
                },
                // Bool → Python bool
                16 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk if (data[0] != 0) py.pyTrue() else py.pyFalse();
                },
                // Float4/8 → Python float
                700 => blk: { // float4
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    const n = std.mem.readInt(i32, data[0..4], .big);
                    const v: f64 = @floatCast(@as(f32, @bitCast(n)));
                    break :blk c.PyFloat_FromDouble(v) orelse py.pyNone();
                },
                701 => blk: { // float8
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    const n = std.mem.readInt(i64, data[0..8], .big);
                    const v: f64 = @bitCast(n);
                    break :blk c.PyFloat_FromDouble(v) orelse py.pyNone();
                },
                // Text/varchar/char/name/bpchar → Python str (raw binary IS UTF-8, no conversion)
                18, 19, 25, 1042, 1043 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk py.newString(data) orelse py.pyNone();
                },
                // JSON (text format) → Python dict/list/str/int/float/bool/None
                114 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk json_parser.jsonToPython(data) orelse py.pyNone();
                },
                // JSONB → Python dict/list/str/int/float/bool/None
                // pg.zig returns JSONB as text-format JSON (no binary version byte).
                3802 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len > 0) {
                        // Check for binary format version byte (0x01) vs text format
                        const json_text = if (data[0] == 0x01) data[1..] else data;
                        break :blk json_parser.jsonToPython(json_text) orelse py.pyNone();
                    }
                    break :blk py.pyNone();
                },
                // Timestamp/timestamptz → Python datetime
                1114, 1184 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len >= 8) {
                        const usec = std.mem.readInt(i64, data[0..8], .big);
                        break :blk pgTimestampToPyDatetime(usec) orelse py.pyNone();
                    }
                    break :blk py.pyNone();
                },
                // Date → Python date
                1082 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len >= 4) {
                        const days = std.mem.readInt(i32, data[0..4], .big);
                        break :blk pgDateToPyDate(days) orelse py.pyNone();
                    }
                    break :blk py.pyNone();
                },
                // Time → Python time
                1083 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len >= 8) {
                        const usec = std.mem.readInt(i64, data[0..8], .big);
                        break :blk pgTimeToPyTime(usec) orelse py.pyNone();
                    }
                    break :blk py.pyNone();
                },
                // Numeric/Decimal → Python Decimal
                1700 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgNumericToPyDecimal(data) orelse py.pyNone();
                },
                // UUID → Python uuid.UUID
                2950 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgUuidToPyUuid(data) orelse py.pyNone();
                },
                // BYTEA → Python bytes
                17 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk py.newBytes(data) orelse py.pyNone();
                },
                // Integer arrays → Python list of ints
                1005, 1007, 1016 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // Text/varchar/name arrays → Python list of strings
                1003, 1009, 1015 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // INTERVAL → Python timedelta
                // Binary: 8 bytes microseconds + 4 bytes days + 4 bytes months
                1186 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len >= 16) {
                        break :blk pgIntervalToPyTimedelta(data) orelse py.pyNone();
                    }
                    break :blk py.pyNone();
                },
                // INET (869) / CIDR (650) → Python ipaddress
                // Binary: family(1) + mask_bits(1) + is_cidr(1) + addr_len(1) + addr(4|16)
                869, 650 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgInetToPython(data) orelse py.pyNone();
                },
                // MONEY (790) → Python Decimal (exact currency arithmetic)
                790 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgMoneyToDecimal(data) orelse py.pyNone();
                },
                // MACADDR (829) / MACADDR8 (774) → Python str (colon-hex)
                829, 774 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgMacaddrToPython(data) orelse py.pyNone();
                },
                // RANGE types (int4/int8/num/ts/tstz/date range) — no native
                // decoder yet. Return None (no crash / no error-state poisoning)
                // rather than mangling the binary bound-structure into a string.
                3904, 3926, 3906, 3908, 3910, 3912 => py.pyNone(),
                // TIMETZ (1266) → Python str (time with timezone)
                // Binary: 8 bytes microseconds + 4 bytes tz offset
                1266 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len >= 12) {
                        const usec = std.mem.readInt(i64, data[0..8], .big);
                        const tz_offset = std.mem.readInt(i32, data[8..12], .big);
                        break :blk pgTimetzToPython(usec, tz_offset) orelse py.pyNone();
                    }
                    break :blk py.pyNone();
                },
                // XML (142) → Python str
                142 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk py.newString(data) orelse py.pyNone();
                },
                // BIT (1560), VARBIT (1562) → Python int
                1560, 1562 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgBitToPython(data) orelse py.pyNone();
                },
                // TSVECTOR (3614) → Python list[tuple[str, list[int]]]
                3614 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgTsvectorToPython(data) orelse py.pyNone();
                },
                // TSQUERY (3615) → Python str (reconstructed from binary tree)
                3615 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgTsqueryToPython(data) orelse py.pyNone();
                },
                // PG_LSN (3220) → Python str ("16/B374D848")
                3220 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    if (data.len == 8) {
                        const lsn = std.mem.readInt(u64, data[0..8], .big);
                        var buf: [24]u8 = undefined;
                        const s = std.fmt.bufPrint(&buf, "{X}/{X}", .{ lsn >> 32, lsn & 0xFFFFFFFF }) catch break :blk py.pyNone();
                        break :blk py.newString(s) orelse py.pyNone();
                    }
                    break :blk py.newString(data) orelse py.pyNone();
                },
                // Bool array (1000) → Python list of bools
                1000 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // Float arrays (1021=float4[], 1022=float8[]) → Python list of floats
                1021, 1022 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // Typed arrays — parse binary array format with native element conversion
                // Timestamp arrays
                1115, 1185 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // Date/time arrays
                1182, 1183 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // Numeric array (1231), UUID array (2951)
                1231, 2951 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // Bytea array (1001)
                1001 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // JSONB array (3807), JSON array (199)
                3807, 199 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
                },
                // OID array (1028) → list of ints
                1028 => blk: {
                    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                    break :blk pgArrayToPyList(data, 1028) orelse py.pyNone(); // oid[] → int4 elements
                },
                // Everything else: check dynamic OIDs, then writeJsonValue fallback
                else => blk: {
                    // Check for hstore (dynamic OID from extension)
                    if (hstore_oid != 0 and oid == hstore_oid) {
                        const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                        break :blk pgHstoreToPyDict(data) orelse py.pyNone();
                    }
                    // Check for pgvector (dynamic OID from extension)
                    if (pg.types.Vector.oid_decimal != 0 and oid == pg.types.Vector.oid_decimal) {
                        const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                        // Decode binary vector and return as Python list of floats
                        const vec = pg.types.Vector.decode(data);
                        if (vec.dim > 0) {
                            const floats = vec.toFloats(allocator) catch break :blk py.pyNone();
                            defer allocator.free(floats);
                            const py_list = c.PyList_New(@intCast(vec.dim)) orelse break :blk py.pyNone();
                            for (floats, 0..) |f, fi| {
                                _ = c.PyList_SetItem(py_list, @intCast(fi), c.PyFloat_FromDouble(@floatCast(f)));
                            }
                            break :blk py_list;
                        }
                        break :blk py.pyNone();
                    }
                    // Check for registered custom enum types
                    if (findEnumByOid(oid)) |enum_entry| {
                        const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                        if (enum_entry.array_oid != 0 and oid == enum_entry.array_oid) {
                            // Enum array — parse text array format
                            break :blk parseEnumArrayText(data) orelse py.pyNone();
                        }
                        // Scalar enum — return as Python string
                        break :blk py.newString(data) orelse py.pyNone();
                    }
                    trace("db_query: FALLBACK oid={d} col={d}", .{ oid, ci });
                    var val_len = row.writeJsonValue(@intCast(ci), val_buf);
                    if (val_len == 0) {
                        // Buffer overflow — grow once, reuse for all remaining
                        const new_size = if (heap_buf) |hb| hb.len * 2 else STACK_SIZE * 16;
                        const new_buf = allocator.alloc(u8, new_size) catch break :blk py.pyNone();
                        if (heap_buf) |hb| allocator.free(hb);
                        heap_buf = new_buf;
                        val_buf = new_buf;
                        val_len = row.writeJsonValue(@intCast(ci), val_buf);
                        if (val_len == 0) break :blk py.pyNone();
                    }
                    const raw = val_buf[0..val_len];
                    // Strip JSON quotes from string values
                    if (val_len >= 2 and raw[0] == '"' and raw[val_len - 1] == '"') {
                        break :blk py.newString(raw[1 .. val_len - 1]) orelse py.pyNone();
                    }
                    break :blk py.newString(raw) orelse py.pyNone();
                },
            };
            _ = c.PyTuple_SetItem(py_tuple, @intCast(ci), py_val);
        }
        tuple_buf.append(allocator, py_tuple) catch {
            c.Py_DecRef(py_tuple);
            continue;
        };
    }

    // Phase 2: Build pre-sized PyList with SET_ITEM (no realloc, no bounds check)
    const row_count = tuple_buf.items.len;
    const py_rows = c.PyList_New(@intCast(row_count)) orelse {
        // Cleanup all collected tuples on allocation failure
        for (tuple_buf.items) |t| c.Py_DecRef(t);
        return null;
    };
    for (tuple_buf.items, 0..) |py_tuple, ri| {
        // SET_ITEM steals the reference — no Py_DecRef needed
        c.PyList_SET_ITEM(py_rows, @intCast(ri), py_tuple);
    }

    // Cache column metadata per SQL text hash — repeated queries skip column
    // list construction entirely. Only build once per unique SQL.
    // THREAD SAFETY: column_cache_mutex protects the shared HashMap.
    // last_columns is threadlocal so no lock needed for it.
    const sql_hash = hashSql(sql);
    {
        column_cache_mutex.lock();
        defer column_cache_mutex.unlock();
        const cache = getColumnCache();
        if (cache.get(sql_hash)) |cached_cols| {
            // Stale-shape guard: check count AND names match.
            // Catches ALTER TABLE ADD/DROP COLUMN AND DROP+CREATE with
            // different columns (same SQL hash, same column count,
            // different column names).
            const cached_len: usize = @intCast(c.PyList_Size(cached_cols));
            const names_match = blk: {
                if (cached_len != col_count) break :blk false;
                for (result.column_names, 0..) |name, i| {
                    const py_tuple = c.PyList_GetItem(cached_cols, @intCast(i));
                    if (py_tuple == null) break :blk false;
                    const py_name = c.PyTuple_GetItem(py_tuple, 0);
                    if (py_name == null) break :blk false;
                    var py_len: c.Py_ssize_t = 0;
                    const py_ptr = c.PyUnicode_AsUTF8AndSize(py_name, &py_len);
                    if (py_ptr == null or @as(usize, @intCast(py_len)) != name.len or
                        !std.mem.eql(u8, py_ptr[0..@intCast(py_len)], name))
                        break :blk false;
                }
                break :blk true;
            };
            if (names_match) {
                // Cache hit — just update this thread's last_columns pointer
                if (last_columns) |old| {
                    if (!module_shutting_down.load(.acquire)) c.Py_DecRef(old);
                }
                c.Py_IncRef(cached_cols);
                last_columns = cached_cols;
            } else {
                // Stale — evict and fall through to rebuild
                c.Py_DecRef(cached_cols);
                _ = cache.remove(sql_hash);
            }
        }
        if (cache.get(sql_hash) == null) {
            // Cache miss — build column list of (name, oid) tuples for cursor.description
            const py_cols = c.PyList_New(@intCast(col_count)) orelse {
                if (last_columns) |old| {
                    if (!module_shutting_down.load(.acquire)) c.Py_DecRef(old);
                }
                last_columns = null;
                return py_rows;
            };
            for (result.column_names, 0..) |name, i| {
                const py_name = py.newString(name) orelse py.pyNone();
                const oid: i64 = if (i < result._oids.len) @as(i64, result._oids[i]) else 0;
                const py_oid = py.newInt(oid);
                const py_pair = c.PyTuple_Pack(2, py_name, py_oid) orelse py.pyNone();
                c.Py_DecRef(py_name);
                c.Py_DecRef(py_oid);
                _ = c.PyList_SetItem(py_cols, @intCast(i), py_pair);
            }
            // Evict all entries when cache is full — column lists are cheap to rebuild.
            if (cache.count() >= COLUMN_CACHE_MAX) {
                trace("column_cache: full ({d} entries), evicting all", .{cache.count()});
                var evict_it = cache.iterator();
                while (evict_it.next()) |entry| {
                    c.Py_DecRef(entry.value_ptr.*);
                }
                cache.clearAndFree();
            }
            // Store in cache (cache owns one ref, last_columns owns another)
            c.Py_IncRef(py_cols);
            cache.put(sql_hash, py_cols) catch {};
            if (last_columns) |old| {
                if (!module_shutting_down.load(.acquire)) c.Py_DecRef(old);
            }
            last_columns = py_cols;
        }
    }

    // If a Python exception was set during type conversion (e.g., importing
    // datetime module failed), we must return NULL — otherwise Python raises
    // SystemError: "returned a result with an exception set".
    if (c.PyErr_Occurred() != null) {
        c.Py_DecRef(py_rows);
        return null;
    }

    return py_rows;
}

// ── _db_query_dicts(pool_handle, sql, params) → list[dict] ──────────────────
// Same as db_query but returns list[dict] directly instead of list[tuple].
// Uses pre-interned column name PyObjects as dict keys — zero per-row string
// allocation. Eliminates the Python-side dict(zip(col_names, row)) overhead.

pub fn db_query_dicts(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    var params_obj: ?*c.PyObject = null;
    // Optional 4th arg: a lock-free query registry handle (from
    // _db_register_query). >=0 selects the hash-free, mutex-free fast path
    // for prepared-statement name + interned dict keys. Omitted / -1 falls
    // back to the shared locked caches.
    var query_handle: c_long = -1;
    if (c.PyArg_ParseTuple(args, "lsO|l", &pool_handle, &sql_c, &params_obj, &query_handle) == 0) return null;

    const sql = std.mem.span(sql_c);
    // Registry handle validation. A handle is only usable if it is (1) within
    // the range that has actually been REGISTERED — not merely < the fixed
    // array size, or a bogus handle would index an uninitialized slot and run
    // the WRONG cached statement — AND (2) tagged with the same SQL hash it was
    // registered under (belt-and-braces against any Python-side handle/SQL
    // desync). On any mismatch we fall back to the shared locked path, which is
    // always correct. The extra hash is lock-free (no mutex, no HashMap) and
    // dwarfed by the query round-trip.
    const use_reg = blk: {
        if (query_handle < 0) break :blk false;
        const idx: usize = @intCast(query_handle);
        if (idx >= registry_count.load(.acquire)) break :blk false;
        break :blk query_registry[idx].sql_hash.load(.monotonic) == hashSql(sql);
    };
    const reg_idx: usize = if (use_reg) @intCast(query_handle) else 0;

    // Extract params (same as db_query)
    var stack_strs: [64]?[]const u8 = undefined;
    var stack_bufs: [64][64]u8 = undefined;
    var pbufs = initParamBufs(params_obj, &stack_strs, &stack_bufs) orelse return null;
    defer pbufs.deinit();
    var overflow_stack: [1024]u8 = undefined;
    var overflow_buf: []u8 = &overflow_stack;
    var overflow_pos: usize = 0;
    var overflow_heap: ?[]u8 = null;
    defer if (overflow_heap) |hb| allocator.free(hb);
    var param_count: usize = pbufs.count;
    extractParams(params_obj, pbufs.strs, pbufs.num_bufs, &param_count, &overflow_buf, &overflow_pos, &overflow_heap) orelse return null;

    // Acquire connection
    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("Database connection unavailable", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Execute query (same as db_query — with retry on cached plan error).
    // Registry handle → lock-free, hash-free prepared-name lookup.
    const values = pbufs.strs[0..param_count];
    const prep_name: []const u8 = if (use_reg) (regGetPrepName(reg_idx) orelse "") else getPreparedName(sql);
    const cache_name: ?[]const u8 = if (prep_name.len > 0) prep_name else null;
    // Inferred Parse-time param type OIDs (fixes `col = ANY(ARRAY[$1,...])`).
    var oid_stack: [64]i32 = undefined;
    var poids = computeParamOids(sql, params_obj, &oid_stack);
    defer poids.deinit();
    var result = execWithParams(conn, sql, values, cache_name, poids.oids) orelse retry: {
        if (conn.err) |err| {
            if (isCachedPlanError(err)) {
                conn.clearErr();
                if (cache_name) |name| conn.stmtCacheEvict(name);
                _ = db_clear_stmt_cache(null, null);
                invalidateColumnCaches(hashSql(sql));
                break :retry execWithParams(conn, sql, values, null, poids.oids);
            }
        }
        setPgError(conn, "Query failed", sql);
        return null;
    } orelse {
        setPgError(conn, "Query failed (retry)", sql);
        return null;
    };
    defer result.deinit();

    const col_count = result.column_names.len;

    // Interned dict-key array.
    //   * Registry handle → regGetKeys: lock-free & hash-free in steady state,
    //     returning a slice into registry-owned (never-freed) memory.
    //   * No handle → the shared locked interned-key cache (unchanged path).
    // The dict path NO LONGER calls updateColumnCache: that maintained the
    // process-global `last_columns` (Python list building + ref juggling under
    // column_cache_mutex on every call) purely for the tuple path's
    // _db_get_last_columns() compat, which the dict path never uses.
    var interned_keys: []*c.PyObject = undefined;
    if (use_reg) {
        interned_keys = regGetKeys(reg_idx, result.column_names) orelse {
            py.setError("Failed to intern column keys", .{});
            return null;
        };
    } else {
        const sql_hash = hashSql(sql);
        column_cache_mutex.lock();
        defer column_cache_mutex.unlock();
        const interned = getOrCreateInternedKeys(sql_hash, result.column_names) orelse {
            py.setError("Failed to intern column keys", .{});
            return null;
        };
        interned_keys = interned.keys;
    }

    // Build list of dicts — same value conversion as db_query but into dicts
    var dict_buf: std.ArrayListUnmanaged(*c.PyObject) = .empty;
    defer dict_buf.deinit(allocator);
    dict_buf.ensureTotalCapacity(allocator, 64) catch {};

    const STACK_SIZE = 4096;
    var stack_buf: [STACK_SIZE]u8 = undefined;
    var heap_buf: ?[]u8 = null;
    defer if (heap_buf) |hb| allocator.free(hb);
    var val_buf: []u8 = &stack_buf;

    const presize: c.Py_ssize_t = @intCast(col_count);
    while (true) {
        const row = result.next() catch {
            recoverAfterRowError(result, conn);
            for (dict_buf.items) |d| c.Py_DecRef(d);
            setPgError(conn, "Query failed mid-result", sql);
            return null;
        } orelse break;
        // Pre-size the dict to exactly col_count entries — avoids the
        // resize-during-population step that PyDict_New forces when more
        // than ~5 items are inserted into the default-sized hash table.
        // Measured: ~0.35μs/row dict overhead in bench_db_query_dicts,
        // of which the resize accounts for a meaningful fraction on
        // 10-200 row queries.
        const py_dict = py._PyDict_NewPresized(presize) orelse continue;
        for (0..col_count) |ci| {
            const py_val = convertColumnValue(&row, ci, col_count, &val_buf, &heap_buf) orelse py.pyNone();
            // PyDict_SetItem does NOT steal refs — IncRef key is handled by interning
            _ = c.PyDict_SetItem(py_dict, interned_keys[ci], py_val);
            // We own the value ref from conversion; SetItem increfs it, so we decref
            c.Py_DecRef(py_val);
        }
        dict_buf.append(allocator, py_dict) catch {
            c.Py_DecRef(py_dict);
            continue;
        };
    }

    // Build pre-sized PyList
    const row_count = dict_buf.items.len;
    const py_rows = c.PyList_New(@intCast(row_count)) orelse {
        for (dict_buf.items) |d| c.Py_DecRef(d);
        return null;
    };
    for (dict_buf.items, 0..) |py_dict, ri| {
        c.PyList_SET_ITEM(py_rows, @intCast(ri), py_dict);
    }

    if (c.PyErr_Occurred() != null) {
        c.Py_DecRef(py_rows);
        return null;
    }

    return py_rows;
}

/// Update column metadata cache (extracted from db_query for reuse).
/// Caller MUST hold column_cache_mutex.
fn updateColumnCache(sql_hash: u64, col_count: usize, result: *pg.Result) void {
    const cache = getColumnCache();
    if (cache.get(sql_hash)) |cached_cols| {
        if (last_columns) |old| {
            if (!module_shutting_down.load(.acquire)) c.Py_DecRef(old);
        }
        c.Py_IncRef(cached_cols);
        last_columns = cached_cols;
    } else {
        const py_cols = c.PyList_New(@intCast(col_count)) orelse {
            if (last_columns) |old| {
                if (!module_shutting_down.load(.acquire)) c.Py_DecRef(old);
            }
            last_columns = null;
            return;
        };
        for (result.column_names, 0..) |name, i| {
            const py_name = py.newString(name) orelse py.pyNone();
            const oid: i64 = if (i < result._oids.len) @as(i64, result._oids[i]) else 0;
            const py_oid = py.newInt(oid);
            const py_pair = c.PyTuple_Pack(2, py_name, py_oid) orelse py.pyNone();
            c.Py_DecRef(py_name);
            c.Py_DecRef(py_oid);
            _ = c.PyList_SetItem(py_cols, @intCast(i), py_pair);
        }
        if (cache.count() >= COLUMN_CACHE_MAX) {
            var evict_it = cache.iterator();
            if (!module_shutting_down.load(.acquire)) {
                while (evict_it.next()) |entry| c.Py_DecRef(entry.value_ptr.*);
            }
            cache.clearAndFree();
        }
        c.Py_IncRef(py_cols);
        cache.put(sql_hash, py_cols) catch {};
        if (last_columns) |old| {
            if (!module_shutting_down.load(.acquire)) c.Py_DecRef(old);
        }
        last_columns = py_cols;
    }
}

/// Convert a single column value from PostgreSQL binary to Python object.
/// Extracted from db_query's OID switch for reuse by db_query_dicts.
fn convertColumnValue(row: anytype, ci: usize, col_count: usize, val_buf: *[]u8, heap_buf: *?[]u8) ?*c.PyObject {
    _ = col_count;

    if (row.values[@intCast(ci)].is_null) return py.pyNone();

    const oid = row.oids[@intCast(ci)];
    return switch (oid) {
        // Integers
        21 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk py.newInt(@as(i64, std.mem.readInt(i16, data[0..2], .big)));
        },
        23 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk py.newInt(@as(i64, std.mem.readInt(i32, data[0..4], .big)));
        },
        20 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk py.newInt(std.mem.readInt(i64, data[0..8], .big));
        },
        16 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk if (data[0] != 0) py.pyTrue() else py.pyFalse();
        },
        700 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            const n = std.mem.readInt(i32, data[0..4], .big);
            const v: f64 = @floatCast(@as(f32, @bitCast(n)));
            break :blk c.PyFloat_FromDouble(v) orelse py.pyNone();
        },
        701 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            const n = std.mem.readInt(i64, data[0..8], .big);
            const v: f64 = @bitCast(n);
            break :blk c.PyFloat_FromDouble(v) orelse py.pyNone();
        },
        18, 19, 25, 1042, 1043 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk py.newString(data) orelse py.pyNone();
        },
        114 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk json_parser.jsonToPython(data) orelse py.pyNone();
        },
        3802 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len > 0) {
                const json_text = if (data[0] == 0x01) data[1..] else data;
                break :blk json_parser.jsonToPython(json_text) orelse py.pyNone();
            }
            break :blk py.pyNone();
        },
        1114, 1184 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len >= 8) {
                const usec = std.mem.readInt(i64, data[0..8], .big);
                break :blk pgTimestampToPyDatetime(usec) orelse py.pyNone();
            }
            break :blk py.pyNone();
        },
        1082 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len >= 4) {
                const days = std.mem.readInt(i32, data[0..4], .big);
                break :blk pgDateToPyDate(days) orelse py.pyNone();
            }
            break :blk py.pyNone();
        },
        1083 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len >= 8) {
                const usec = std.mem.readInt(i64, data[0..8], .big);
                break :blk pgTimeToPyTime(usec) orelse py.pyNone();
            }
            break :blk py.pyNone();
        },
        1700 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgNumericToPyDecimal(data) orelse py.pyNone();
        },
        2950 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgUuidToPyUuid(data) orelse py.pyNone();
        },
        17 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk py.newBytes(data) orelse py.pyNone();
        },
        1005, 1007, 1016 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
        },
        1003, 1009, 1015 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
        },
        1186 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len >= 16) break :blk pgIntervalToPyTimedelta(data) orelse py.pyNone();
            break :blk py.pyNone();
        },
        869, 650 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgInetToPython(data) orelse py.pyNone();
        },
        790 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgMoneyToDecimal(data) orelse py.pyNone();
        },
        // MACADDR (829) / MACADDR8 (774) → Python str (colon-hex)
        829, 774 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgMacaddrToPython(data) orelse py.pyNone();
        },
        // RANGE types — unsupported binary decode; return None (no crash).
        3904, 3926, 3906, 3908, 3910, 3912 => py.pyNone(),
        1266 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len >= 12) {
                const usec = std.mem.readInt(i64, data[0..8], .big);
                const tz_offset = std.mem.readInt(i32, data[8..12], .big);
                break :blk pgTimetzToPython(usec, tz_offset) orelse py.pyNone();
            }
            break :blk py.pyNone();
        },
        142 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk py.newString(data) orelse py.pyNone();
        },
        1560, 1562 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgBitToPython(data) orelse py.pyNone();
        },
        3614 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgTsvectorToPython(data) orelse py.pyNone();
        },
        3615 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgTsqueryToPython(data) orelse py.pyNone();
        },
        3220 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            if (data.len == 8) {
                const lsn = std.mem.readInt(u64, data[0..8], .big);
                var buf: [24]u8 = undefined;
                const s = std.fmt.bufPrint(&buf, "{X}/{X}", .{ lsn >> 32, lsn & 0xFFFFFFFF }) catch break :blk py.pyNone();
                break :blk py.newString(s) orelse py.pyNone();
            }
            break :blk py.newString(data) orelse py.pyNone();
        },
        1000 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
        },
        1021, 1022 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
        },
        1115, 1185, 1182, 1183, 1231, 2951, 1001, 3807, 199 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgArrayToPyList(data, oid) orelse py.pyNone();
        },
        1028 => blk: {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
            break :blk pgArrayToPyList(data, 1028) orelse py.pyNone();
        },
        else => blk: {
            if (hstore_oid != 0 and oid == hstore_oid) {
                const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                break :blk pgHstoreToPyDict(data) orelse py.pyNone();
            }
            if (pg.types.Vector.oid_decimal != 0 and oid == pg.types.Vector.oid_decimal) {
                const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                const vec = pg.types.Vector.decode(data);
                if (vec.dim > 0) {
                    const floats = vec.toFloats(allocator) catch break :blk py.pyNone();
                    defer allocator.free(floats);
                    const py_list = c.PyList_New(@intCast(vec.dim)) orelse break :blk py.pyNone();
                    for (floats, 0..) |f, fi| {
                        _ = c.PyList_SetItem(py_list, @intCast(fi), c.PyFloat_FromDouble(@floatCast(f)));
                    }
                    break :blk py_list;
                }
                break :blk py.pyNone();
            }
            if (findEnumByOid(oid)) |_| {
                const data = (row.get([]const u8, @intCast(ci)) catch null) orelse break :blk py.pyNone();
                break :blk py.newString(data) orelse py.pyNone();
            }
            // Fallback: writeJsonValue
            const data = row.writeJsonValue(@intCast(ci), val_buf.*);
            if (data == 0) {
                const new_size = if (heap_buf.*) |hb| hb.len * 2 else 4096 * 16;
                const new_buf = allocator.alloc(u8, new_size) catch break :blk py.pyNone();
                if (heap_buf.*) |hb| allocator.free(hb);
                heap_buf.* = new_buf;
                val_buf.* = new_buf;
                const retry = row.writeJsonValue(@intCast(ci), val_buf.*);
                if (retry == 0) break :blk py.pyNone();
                const raw = val_buf.*[0..retry];
                if (retry >= 2 and raw[0] == '"' and raw[retry - 1] == '"')
                    break :blk newStringOrNone(raw[1 .. retry - 1]);
                break :blk newStringOrNone(raw);
            }
            const raw = val_buf.*[0..data];
            if (data >= 2 and raw[0] == '"' and raw[data - 1] == '"')
                break :blk newStringOrNone(raw[1 .. data - 1]);
            break :blk newStringOrNone(raw);
        },
    };
}

// ── _db_query_json(pool_handle, sql, params) → bytes ────────────────────────
// Returns query results as a JSON array bytes object, built entirely in Zig.
// No Python dict/list creation, no json.dumps. PostgreSQL binary → JSON bytes.

/// Pre-built JSON key fragments for each column position.
/// Column 0: '{"key":' , Column N: ',"key":'
const JsonKeyFragments = struct {
    fragments: [][]const u8,
    count: usize,
};
var json_key_cache: ?std.AutoHashMap(u64, JsonKeyFragments) = null;

fn getJsonKeyCache() *std.AutoHashMap(u64, JsonKeyFragments) {
    if (json_key_cache) |*jkc| return jkc;
    json_key_cache = std.AutoHashMap(u64, JsonKeyFragments).init(allocator);
    return &json_key_cache.?;
}

fn getOrCreateJsonKeys(sql_hash: u64, col_names: [][]const u8) ?JsonKeyFragments {
    const jkc = getJsonKeyCache();
    if (jkc.get(sql_hash)) |existing| return existing;

    const col_count = col_names.len;
    if (col_count == 0) return null;

    const fragments = allocator.alloc([]const u8, col_count) catch return null;
    for (col_names, 0..) |name, i| {
        // Column 0: '{"name":' , others: ',"name":'
        const prefix: []const u8 = if (i == 0) "{\"" else ",\"";
        const suffix: []const u8 = "\":";
        const frag = allocator.alloc(u8, prefix.len + name.len + suffix.len) catch {
            for (fragments[0..i]) |f| allocator.free(f);
            allocator.free(fragments);
            return null;
        };
        @memcpy(frag[0..prefix.len], prefix);
        @memcpy(frag[prefix.len .. prefix.len + name.len], name);
        @memcpy(frag[prefix.len + name.len ..], suffix);
        fragments[i] = frag;
    }

    const entry = JsonKeyFragments{ .fragments = fragments, .count = col_count };
    if (jkc.count() >= COLUMN_CACHE_MAX) {
        var evict_it = jkc.iterator();
        while (evict_it.next()) |e| {
            for (e.value_ptr.fragments) |f| allocator.free(f);
            allocator.free(e.value_ptr.fragments);
        }
        jkc.clearAndFree();
    }
    jkc.put(sql_hash, entry) catch {
        for (fragments) |f| allocator.free(f);
        allocator.free(fragments);
        return null;
    };
    return entry;
}

pub fn db_query_json(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    var params_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lsO", &pool_handle, &sql_c, &params_obj) == 0) return null;

    const sql = std.mem.span(sql_c);

    // Extract params
    var stack_strs: [64]?[]const u8 = undefined;
    var stack_bufs: [64][64]u8 = undefined;
    var pbufs = initParamBufs(params_obj, &stack_strs, &stack_bufs) orelse return null;
    defer pbufs.deinit();
    var overflow_stack: [1024]u8 = undefined;
    var overflow_buf: []u8 = &overflow_stack;
    var overflow_pos: usize = 0;
    var overflow_heap: ?[]u8 = null;
    defer if (overflow_heap) |hb| allocator.free(hb);
    var param_count: usize = pbufs.count;
    extractParams(params_obj, pbufs.strs, pbufs.num_bufs, &param_count, &overflow_buf, &overflow_pos, &overflow_heap) orelse return null;

    // Acquire connection
    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("Database connection unavailable", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Execute query
    const values = pbufs.strs[0..param_count];
    const prep_name = getPreparedName(sql);
    const cache_name: ?[]const u8 = if (prep_name.len > 0) prep_name else null;
    // Inferred Parse-time param type OIDs (fixes `col = ANY(ARRAY[$1,...])`).
    var oid_stack: [64]i32 = undefined;
    var poids = computeParamOids(sql, params_obj, &oid_stack);
    defer poids.deinit();
    var result = execWithParams(conn, sql, values, cache_name, poids.oids) orelse retry: {
        if (conn.err) |err| {
            if (isCachedPlanError(err)) {
                conn.clearErr();
                if (cache_name) |name| conn.stmtCacheEvict(name);
                _ = db_clear_stmt_cache(null, null);
                invalidateColumnCaches(hashSql(sql));
                break :retry execWithParams(conn, sql, values, null, poids.oids);
            }
        }
        setPgError(conn, "Query failed", sql);
        return null;
    } orelse {
        setPgError(conn, "Query failed (retry)", sql);
        return null;
    };
    defer result.deinit();

    const col_count = result.column_names.len;
    const sql_hash = hashSql(sql);

    // Get pre-built JSON key fragments
    var json_keys: JsonKeyFragments = undefined;
    {
        column_cache_mutex.lock();
        defer column_cache_mutex.unlock();
        json_keys = getOrCreateJsonKeys(sql_hash, result.column_names) orelse {
            py.setError("Failed to create JSON keys", .{});
            return null;
        };
        updateColumnCache(sql_hash, col_count, result);
    }

    // Build JSON output into a growable buffer
    var json_buf: std.ArrayListUnmanaged(u8) = .empty;
    defer json_buf.deinit(allocator);
    json_buf.ensureTotalCapacity(allocator, 4096) catch {};
    json_buf.appendAssumeCapacity('[');

    var row_idx: usize = 0;
    while (true) {
        const row = result.next() catch {
            // Mid-stream failure — recover the connection and surface the error
            // rather than emitting a silently truncated JSON array as success.
            recoverAfterRowError(result, conn);
            setPgError(conn, "Query failed mid-result", sql);
            return null;
        } orelse break;
        if (row_idx > 0) json_buf.append(allocator, ',') catch {};

        for (0..col_count) |ci| {
            json_buf.appendSlice(allocator, json_keys.fragments[ci]) catch {};

            if (row.values[@intCast(ci)].is_null) {
                json_buf.appendSlice(allocator, "null") catch {};
                continue;
            }
            const oid = row.oids[@intCast(ci)];
            writeJsonColumnValue(allocator, &json_buf, &row, ci, oid);
        }
        json_buf.append(allocator, '}') catch {};
        row_idx += 1;
    }

    json_buf.append(allocator, ']') catch {};

    // Return as Python bytes
    return c.PyBytes_FromStringAndSize(json_buf.items.ptr, @intCast(json_buf.items.len));
}

/// Write a column value as JSON into the output buffer.
fn writeJsonColumnValue(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), row: anytype, ci: usize, oid: i32) void {
    // Arrays carry a multi-dimensional structure, handled before the scalar
    // switch so the output is a (possibly nested) JSON array — the else-branch
    // used to splice raw binary array bytes as a string (broken JSON).
    switch (oid) {
        1005, 1007, 1016, 1028, 1000, 1021, 1022, 1003, 1009, 1015, 1115, 1185, 1182, 1183, 1231, 2951, 1001, 3807, 199 => {
            const data = (row.get([]const u8, @intCast(ci)) catch null) orelse {
                buf.appendSlice(alloc, "null") catch {};
                return;
            };
            writeJsonArray(alloc, buf, data, oid);
            return;
        },
        else => {},
    }
    const data = (row.get([]const u8, @intCast(ci)) catch null) orelse {
        buf.appendSlice(alloc, "null") catch {};
        return;
    };
    writeJsonScalar(alloc, buf, data, oid);
}

/// Render one PostgreSQL scalar binary value (already fetched into `data`) as
/// JSON. Shared by the top-level column writer and the array element writer.
fn writeJsonScalar(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), data: []const u8, oid: i32) void {
    switch (oid) {
        // Integers → digits
        21 => {
            if (data.len < 2) return buf.appendSlice(alloc, "null") catch {};
            const v = std.mem.readInt(i16, data[0..2], .big);
            var num_buf: [8]u8 = undefined;
            const s = std.fmt.bufPrint(&num_buf, "{d}", .{v}) catch return buf.appendSlice(alloc, "null") catch {};
            buf.appendSlice(alloc, s) catch {};
        },
        23 => {
            if (data.len < 4) return buf.appendSlice(alloc, "null") catch {};
            const v = std.mem.readInt(i32, data[0..4], .big);
            var num_buf: [12]u8 = undefined;
            const s = std.fmt.bufPrint(&num_buf, "{d}", .{v}) catch return buf.appendSlice(alloc, "null") catch {};
            buf.appendSlice(alloc, s) catch {};
        },
        20 => {
            if (data.len < 8) return buf.appendSlice(alloc, "null") catch {};
            const v = std.mem.readInt(i64, data[0..8], .big);
            var num_buf: [24]u8 = undefined;
            const s = std.fmt.bufPrint(&num_buf, "{d}", .{v}) catch return buf.appendSlice(alloc, "null") catch {};
            buf.appendSlice(alloc, s) catch {};
        },
        // Bool → true/false
        16 => {
            if (data.len < 1) return buf.appendSlice(alloc, "null") catch {};
            buf.appendSlice(alloc, if (data[0] != 0) "true" else "false") catch {};
        },
        // Float → number (ensure decimal point so JSON parsers see float, not int)
        700 => {
            if (data.len < 4) return buf.appendSlice(alloc, "null") catch {};
            const n = std.mem.readInt(i32, data[0..4], .big);
            const v: f64 = @floatCast(@as(f32, @bitCast(n)));
            writeJsonFloat(alloc, buf, v);
        },
        701 => {
            if (data.len < 8) return buf.appendSlice(alloc, "null") catch {};
            const n = std.mem.readInt(i64, data[0..8], .big);
            const v: f64 = @bitCast(n);
            writeJsonFloat(alloc, buf, v);
        },
        // Text/varchar → quoted string with JSON escaping
        18, 19, 25, 1042, 1043 => writeJsonString(alloc, buf, data),
        // JSON (114) → raw text as stored.
        114 => appendCompactJson(alloc, buf, data),
        // JSONB (3802) → strip 0x01 version byte, emit COMPACT JSON.
        3802 => {
            if (data.len > 0) {
                const json_text = if (data[0] == 0x01) data[1..] else data;
                appendCompactJson(alloc, buf, json_text);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // TIMESTAMP / TIMESTAMPTZ → naive ISO 8601 string.
        1114, 1184 => {
            if (data.len < 8) return buf.appendSlice(alloc, "null") catch {};
            const usec = std.mem.readInt(i64, data[0..8], .big);
            var tbuf: [40]u8 = undefined;
            if (pg_render.writeIsoTimestamp(&tbuf, usec)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // DATE → "YYYY-MM-DD" string
        1082 => {
            if (data.len < 4) return buf.appendSlice(alloc, "null") catch {};
            const days = std.mem.readInt(i32, data[0..4], .big);
            var tbuf: [16]u8 = undefined;
            if (pg_render.writeIsoDate(&tbuf, days)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // TIME → "HH:MM:SS[.ffffff]" string
        1083 => {
            if (data.len < 8) return buf.appendSlice(alloc, "null") catch {};
            const usec = std.mem.readInt(i64, data[0..8], .big);
            var tbuf: [20]u8 = undefined;
            if (pg_render.writeIsoTime(&tbuf, usec)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // NUMERIC → canonical decimal string (also NaN/Infinity/-Infinity, which
        // pgNumericToStr now emits — as JSON strings, matching str(Decimal)).
        1700 => {
            var nbuf: [288]u8 = undefined;
            if (pg_render.pgNumericToStr(data, &nbuf)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // MONEY → decimal string (sign-correct; matches str(Decimal)).
        790 => {
            if (data.len < 8) return buf.appendSlice(alloc, "null") catch {};
            const cents = std.mem.readInt(i64, data[0..8], .big);
            var mbuf: [32]u8 = undefined;
            if (pg_render.pgMoneyToStr(cents, &mbuf)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // MACADDR / MACADDR8 → colon-hex string.
        829, 774 => {
            var macbuf: [24]u8 = undefined;
            if (pg_render.pgMacaddrToStr(data, &macbuf)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // UUID → canonical lowercase hyphenated string
        2950 => {
            var ubuf: [36]u8 = undefined;
            if (pg_render.pgUuidToStr(data, &ubuf)) |s| {
                writeJsonString(alloc, buf, s);
            } else buf.appendSlice(alloc, "null") catch {};
        },
        // BYTEA → `\xDEADBEEF` hex string (matches PG ::text; never raw binary).
        17 => writeJsonHexString(alloc, buf, data),
        // RANGE types — no native decode; emit null (matches object-path None).
        3904, 3926, 3906, 3908, 3910, 3912 => buf.appendSlice(alloc, "null") catch {},
        // Everything else: enums and text-like types send valid UTF-8 in binary
        // mode → quote as a JSON string. Genuinely binary/unhandled types (their
        // wire form is not UTF-8) are hex-encoded rather than emitted as raw
        // bytes, which would produce invalid JSON.
        else => {
            if (std.unicode.utf8ValidateSlice(data)) {
                writeJsonString(alloc, buf, data);
            } else {
                writeJsonHexString(alloc, buf, data);
            }
        },
    }
}

/// Map an array type OID to its element (scalar) type OID for JSON rendering.
fn arrayElemOid(array_oid: i32) i32 {
    return switch (array_oid) {
        1005 => 21,
        1007 => 23,
        1016 => 20,
        1028 => 23, // oid[] elements are 4-byte, decode like int4
        1000 => 16,
        1021 => 700,
        1022 => 701,
        1003, 1009, 1015 => 25,
        1115 => 1114,
        1185 => 1184,
        1182 => 1082,
        1183 => 1083,
        1231 => 1700,
        2951 => 2950,
        1001 => 17,
        3807 => 3802,
        199 => 114,
        else => 25,
    };
}

/// Recursively write dimension `dim_idx` of a binary array as a JSON array,
/// advancing `offset` through the row-major element stream.
fn writeJsonArrayDim(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), data: []const u8, offset: *usize, dims: []const i32, dim_idx: usize, elem_oid: i32) void {
    buf.append(alloc, '[') catch {};
    const count = dims[dim_idx];
    const is_leaf = dim_idx + 1 >= dims.len;
    var i: i32 = 0;
    while (i < count) : (i += 1) {
        if (i > 0) buf.append(alloc, ',') catch {};
        if (is_leaf) {
            if (offset.* + 4 > data.len) {
                buf.appendSlice(alloc, "null") catch {};
                continue;
            }
            const elem_len = std.mem.readInt(i32, data[offset.*..][0..4], .big);
            offset.* += 4;
            if (elem_len < 0) {
                buf.appendSlice(alloc, "null") catch {};
                continue;
            }
            const elen: usize = @intCast(elem_len);
            if (offset.* + elen > data.len) {
                buf.appendSlice(alloc, "null") catch {};
                continue;
            }
            writeJsonScalar(alloc, buf, data[offset.*..][0..elen], elem_oid);
            offset.* += elen;
        } else {
            writeJsonArrayDim(alloc, buf, data, offset, dims, dim_idx + 1, elem_oid);
        }
    }
    buf.append(alloc, ']') catch {};
}

/// Write a PostgreSQL binary array as a (possibly nested) JSON array.
fn writeJsonArray(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), data: []const u8, array_oid: i32) void {
    const hdr = readArrayHeader(data) orelse return buf.appendSlice(alloc, "[]") catch {};
    if (hdr.ndim == 0) return buf.appendSlice(alloc, "[]") catch {};
    var offset = hdr.data_off;
    writeJsonArrayDim(alloc, buf, data, &offset, hdr.dims[0..@intCast(hdr.ndim)], 0, arrayElemOid(array_oid));
}

/// Write a float as JSON. Non-finite (nan/±inf) has no JSON numeric literal, so
/// it is emitted as a LOSSLESS quoted string — "NaN" / "Infinity" / "-Infinity" —
/// NOT `null`: turning an infinite/NaN measurement into null silently deletes the
/// value and is indistinguishable downstream from a missing field. These spellings
/// round-trip through Python float()/Decimal() and match the NUMERIC path
/// (pgNumericToStr) and result.zig's writeJsonValue. Finite values format as
/// decimal via {d} into a wide buffer so a large magnitude never overflows; if
/// that ever fails, fall back to {e}.
fn writeJsonFloat(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), v: f64) void {
    if (std.math.isNan(v)) {
        buf.appendSlice(alloc, "\"NaN\"") catch {};
        return;
    }
    if (std.math.isInf(v)) {
        buf.appendSlice(alloc, if (v < 0) "\"-Infinity\"" else "\"Infinity\"") catch {};
        return;
    }
    // Widest {d} for an f64 is ~325 chars (largest magnitude / smallest denormal);
    // 512 covers it. The old [32] buffer overflowed for ~1e39+ → silent null.
    var num_buf: [512]u8 = undefined;
    const s = std.fmt.bufPrint(&num_buf, "{d}", .{v}) catch {
        const s2 = std.fmt.bufPrint(&num_buf, "{e}", .{v}) catch {
            buf.appendSlice(alloc, "null") catch {};
            return;
        };
        buf.appendSlice(alloc, s2) catch {};
        return;
    };
    buf.appendSlice(alloc, s) catch {};
    // If no '.'/'e'/'E' in output, append .0 so JSON preserves float type.
    if (std.mem.indexOfScalar(u8, s, '.') == null and
        std.mem.indexOfScalar(u8, s, 'e') == null and
        std.mem.indexOfScalar(u8, s, 'E') == null)
    {
        buf.appendSlice(alloc, ".0") catch {};
    }
}

/// Write raw bytes as a JSON string in PostgreSQL bytea `\xDEADBEEF` hex form.
/// The leading backslash is JSON-escaped (`\\x`), so the decoded string is
/// exactly what a `bytea::text` read returns.
fn writeJsonHexString(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), data: []const u8) void {
    const hex = "0123456789abcdef";
    buf.append(alloc, '"') catch {};
    buf.appendSlice(alloc, "\\\\x") catch {}; // JSON `\\x` → decoded `\x`
    for (data) |b| {
        buf.append(alloc, hex[b >> 4]) catch {};
        buf.append(alloc, hex[b & 0x0f]) catch {};
    }
    buf.append(alloc, '"') catch {};
}

/// Append a JSON document with insignificant whitespace removed, via the
/// shared (unit-tested) pg_render.compactJson. Compaction only shrinks, so we
/// reserve `data.len` bytes and write in place. On OOM we splice the raw JSON
/// (still valid, just not byte-identical to the compact Python path).
fn appendCompactJson(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), data: []const u8) void {
    buf.ensureUnusedCapacity(alloc, data.len) catch {
        buf.appendSlice(alloc, data) catch {};
        return;
    };
    const start = buf.items.len;
    const dst = buf.items.ptr[start .. start + data.len];
    const n = pg_render.compactJson(data, dst);
    buf.items.len = start + n;
}

/// Write a JSON-escaped string value: "text" with \n, \t, \\, \", etc.
fn writeJsonString(alloc: std.mem.Allocator, buf: *std.ArrayListUnmanaged(u8), data: []const u8) void {
    buf.append(alloc, '"') catch {};
    for (data) |byte| {
        switch (byte) {
            '"' => buf.appendSlice(alloc, "\\\"") catch {},
            '\\' => buf.appendSlice(alloc, "\\\\") catch {},
            '\n' => buf.appendSlice(alloc, "\\n") catch {},
            '\r' => buf.appendSlice(alloc, "\\r") catch {},
            '\t' => buf.appendSlice(alloc, "\\t") catch {},
            0x08 => buf.appendSlice(alloc, "\\b") catch {},
            0x0c => buf.appendSlice(alloc, "\\f") catch {},
            else => {
                if (byte < 0x20) {
                    // Control character → \u00XX
                    var esc_buf: [6]u8 = undefined;
                    _ = std.fmt.bufPrint(&esc_buf, "\\u{X:0>4}", .{byte}) catch {};
                    buf.appendSlice(alloc, &esc_buf) catch {};
                } else {
                    buf.append(alloc, byte) catch {};
                }
            },
        }
    }
    buf.append(alloc, '"') catch {};
}

// ── _db_execute(sql, params_list) → affected-row count (int) ────────────────
// For INSERT/UPDATE/DELETE — returns the affected row count as a Python int
// (parsed from the CommandComplete tag at the wire; never a status string).

pub fn db_execute(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    var params_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lsO", &pool_handle, &sql_c, &params_obj) == 0) return null;

    const sql = std.mem.span(sql_c);
    trace("db_execute: handle={d} sql={s}", .{ pool_handle, sql[0..@min(sql.len, 120)] });

    // Stack-first param extraction: 64 on stack, heap for more
    var stack_strs: [64]?[]const u8 = undefined;
    var stack_bufs: [64][64]u8 = undefined;
    var pbufs = initParamBufs(params_obj, &stack_strs, &stack_bufs) orelse return null;
    defer pbufs.deinit();
    var overflow_stack: [1024]u8 = undefined;
    var overflow_buf: []u8 = &overflow_stack;
    var overflow_pos: usize = 0;
    var overflow_heap: ?[]u8 = null;
    defer if (overflow_heap) |hb| allocator.free(hb);
    var param_count: usize = pbufs.count;
    extractParams(params_obj, pbufs.strs, pbufs.num_bufs, &param_count, &overflow_buf, &overflow_pos, &overflow_heap) orelse return null;

    const acq = acquireConnByHandle(pool_handle) orelse {
        trace("db_execute: FAILED acquireConnByHandle handle={d} pool_count={d} sql={s}", .{ pool_handle, registeredPoolCount(), sql[0..@min(sql.len, 60)] });
        py.setError("Database connection unavailable", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Execute with prepared statement caching — same benefit as queries.
    // DDL statements (CREATE, DROP, ALTER) should NOT be cached since they
    // change schema. Only cache parameterized DML (INSERT, UPDATE, DELETE).
    const values = pbufs.strs[0..param_count];
    const stripped = std.mem.trimStart(u8, sql, " \t\n\r");
    const is_dml = stripped.len >= 6 and (std.ascii.startsWithIgnoreCase(stripped, "INSERT") or
        std.ascii.startsWithIgnoreCase(stripped, "UPDATE") or
        std.ascii.startsWithIgnoreCase(stripped, "DELETE"));
    const prep_name_exec = if (is_dml) getPreparedName(sql) else "";
    const cache_name_exec: ?[]const u8 = if (prep_name_exec.len > 0) prep_name_exec else null;
    // Inferred Parse-time param type OIDs so DML like
    // `DELETE ... WHERE id = ANY(ARRAY[$1,$2])` types its ints correctly.
    var oid_stack: [64]i32 = undefined;
    var poids = computeParamOids(sql, params_obj, &oid_stack);
    defer poids.deinit();
    const opts = pg.Conn.QueryOpts{ .cache_name = cache_name_exec, .param_oids = poids.oids };

    const rowcount: i64 = switch (values.len) {
        0 => (conn.execOpts(sql, .{}, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        1 => (conn.execOpts(sql, .{values[0]}, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        2 => (conn.execOpts(sql, .{ values[0], values[1] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        3 => (conn.execOpts(sql, .{ values[0], values[1], values[2] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        4 => (conn.execOpts(sql, .{ values[0], values[1], values[2], values[3] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        5 => (conn.execOpts(sql, .{ values[0], values[1], values[2], values[3], values[4] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        6 => (conn.execOpts(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        7 => (conn.execOpts(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5], values[6] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        8 => (conn.execOpts(sql, .{ values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7] }, opts) catch {
            setPgError(conn, "Execute failed", sql);
            return null;
        }) orelse 0,
        // 9+ params: use dynamic path with proper DML handling
        else => blk: {
            break :blk execDmlDynamic(conn, sql, values, opts) orelse {
                setPgError(conn, "Execute failed", sql);
                return null;
            };
        },
    };

    return py.newInt(rowcount);
}

// ── _db_get_last_columns() → list of column name strings ────────────────────
// Returns the column names from the most recent _db_query call.
// Called by PgZigCursor only when cursor.description is accessed.

pub fn db_get_last_columns(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    if (last_columns) |cols| {
        c.Py_IncRef(cols);
        return cols;
    }
    // No columns available — return empty list
    return c.PyList_New(0);
}

// ── Batch execute ────────────────────────────────────────────────────────────
// Execute the same SQL with many parameter sets in batched wire protocol.
// Parse+Describe once, then N×(Bind+Execute) with Sync at batch boundaries.

const BATCH_FLUSH_SIZE: usize = 256 * 1024; // 256KB — flush batch threshold

/// _db_exec_many(pool_handle, sql, list_of_param_lists) → total_rowcount
/// Batched execute: single prepared statement, N parameter sets.
/// 10-100x faster than N individual execute() calls for bulk INSERT/UPDATE.
pub fn db_exec_many(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var pool_handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    var rows_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lsO", &pool_handle, &sql_c, &rows_obj) == 0) return null;

    const sql = std.mem.span(sql_c);
    const rows_list = rows_obj orelse {
        py.setError("rows_list is null", .{});
        return null;
    };

    const row_count_total: c.Py_ssize_t = c.PyList_Size(rows_list);
    if (row_count_total <= 0) return py.newInt(0);

    // Acquire connection
    const acq = acquireConnByHandle(pool_handle) orelse {
        py.setError("Database connection unavailable", .{});
        return null;
    };
    const conn = acq.conn;
    defer releaseAcquired(acq);

    // Deallocate any previous _batch statement, then prepare fresh.
    // Named statements persist across Sync boundaries (unnamed are discarded).
    conn.deallocate("_batch") catch {};
    var stmt = pg.Stmt.init(conn, .{ .cache_name = "_batch" }) catch {
        py.setError("Failed to init statement", .{});
        return null;
    };
    // Free the statement's arena (and end the reader flow) on EVERY exit. The
    // old code only deinit'd on prepare failure, leaking the arena on the
    // success path and on every mid-batch error return — a per-call heap leak.
    defer stmt.deinit();
    stmt.prepare(sql, null) catch {
        setPgError(conn, "Batch prepare failed", sql);
        return null;
    };

    // Begin transaction if not already in one
    const was_idle = conn._state == .idle;
    if (was_idle) {
        conn.begin() catch {
            setPgError(conn, "BEGIN failed", sql);
            return null;
        };
    }

    var total_affected: i64 = 0;
    var batch_start: c.Py_ssize_t = 0;

    // Process rows in batches
    while (batch_start < row_count_total) {
        conn._buf.resetRetainingCapacity();

        var batch_count: c.Py_ssize_t = 0;

        // Fill batch up to BATCH_FLUSH_SIZE
        var row_idx = batch_start;
        while (row_idx < row_count_total) : (row_idx += 1) {
            const param_list = c.PyList_GetItem(rows_list, row_idx) orelse {
                py.setError("Invalid row at index {d}", .{row_idx});
                if (was_idle) conn.rollback() catch {};
                return null;
            };
            const param_count: usize = @intCast(c.PyList_Size(param_list));

            // Start new Bind message for this row
            stmt.startNewBind() catch {
                if (was_idle) conn.rollback() catch {};
                return null;
            };

            // Bind each parameter as text (nullable)
            for (0..param_count) |pi| {
                const param_obj = c.PyList_GetItem(param_list, @intCast(pi));
                if (param_obj == null or param_obj == py.pyNone()) {
                    // NULL parameter
                    stmt.bind(@as(?[]const u8, null)) catch {
                        if (was_idle) conn.rollback() catch {};
                        return null;
                    };
                } else {
                    // Convert Python object to string
                    var str_buf: [64]u8 = undefined;
                    const val_str = pyObjToStr(param_obj.?, &str_buf) orelse {
                        if (was_idle) conn.rollback() catch {};
                        return null;
                    };
                    stmt.bind(val_str) catch {
                        if (was_idle) conn.rollback() catch {};
                        return null;
                    };
                }
            }

            // Write Bind+Execute (no Sync)
            const buf_size = stmt.finishBindExecuteNoSync() catch {
                if (was_idle) conn.rollback() catch {};
                return null;
            };
            batch_count += 1;

            // Flush batch if buffer exceeds threshold
            if (buf_size >= BATCH_FLUSH_SIZE) break;
        }

        // Write Sync and flush
        conn._buf.write(&.{ 'S', 0, 0, 0, 4 }) catch {
            if (was_idle) conn.rollback() catch {};
            return null;
        };
        conn.write(conn._buf.string()) catch {
            if (was_idle) conn.rollback() catch {};
            setPgError(conn, "Batch flush failed", sql);
            return null;
        };

        // Read responses: batch_count × (BindComplete + CommandComplete)
        var bi: c.Py_ssize_t = 0;
        while (bi < batch_count) : (bi += 1) {
            // BindComplete ('2')
            const bind_msg = conn.read() catch {
                if (was_idle) conn.rollback() catch {};
                setPgError(conn, "Batch read BindComplete failed", sql);
                return null;
            };
            if (bind_msg.type != '2') {
                if (was_idle) conn.rollback() catch {};
                py.setError("Expected BindComplete, got '{c}'", .{bind_msg.type});
                return null;
            }

            // CommandComplete ('C') — fast path for single-row inserts
            const cc_msg = conn.read() catch {
                if (was_idle) conn.rollback() catch {};
                setPgError(conn, "Batch read CommandComplete failed", sql);
                return null;
            };
            if (cc_msg.type == 'C') {
                if (cc_msg.data.len >= 3 and cc_msg.data[cc_msg.data.len - 3] == ' ' and cc_msg.data[cc_msg.data.len - 2] == '1') {
                    total_affected += 1; // Fast path: "INSERT 0 1" or "UPDATE 1"
                } else {
                    // Parse row count from CommandComplete
                    total_affected += parseCommandComplete(cc_msg.data);
                }
            }
        }

        // ReadyForQuery ('Z')
        conn.readyForQuery() catch {
            if (was_idle) conn.rollback() catch {};
            return null;
        };

        batch_start = row_idx + 1;
    }

    // Commit if we started the transaction
    if (was_idle) {
        conn.commit() catch {
            conn.rollback() catch {};
            setPgError(conn, "COMMIT failed after batch", sql);
            return null;
        };
    }

    return py.newInt(total_affected);
}

/// Parse affected row count from CommandComplete data (e.g. "INSERT 0 5" → 5)
fn parseCommandComplete(data: []const u8) i64 {
    // Find last space, parse number after it
    var i = data.len;
    while (i > 0) : (i -= 1) {
        if (data[i - 1] == ' ') {
            const num_str = data[i..];
            // Strip trailing null
            const end = std.mem.indexOfScalar(u8, num_str, 0) orelse num_str.len;
            return std.fmt.parseInt(i64, num_str[0..end], 10) catch 0;
        }
    }
    return 0;
}

/// Convert a Python object to a string slice for parameter binding.
/// Returns null if conversion fails.
fn pyObjToStr(obj: *c.PyObject, buf: *[64]u8) ?[]const u8 {
    // Fast path: already a string
    if (c.PyUnicode_Check(obj) != 0) {
        const cstr: [*c]const u8 = c.PyUnicode_AsUTF8(obj) orelse return null;
        return std.mem.span(cstr);
    }
    // Integer
    if (c.PyLong_Check(obj) != 0) {
        const val = c.PyLong_AsLongLong(obj);
        const written = std.fmt.bufPrint(buf, "{d}", .{val}) catch return null;
        return written;
    }
    // Float
    if (c.PyFloat_Check(obj) != 0) {
        const val = c.PyFloat_AsDouble(obj);
        const written = std.fmt.bufPrint(buf, "{d}", .{val}) catch return null;
        return written;
    }
    // Bool (must check before int since bool is subclass of int in Python)
    if (c.PyBool_Check(obj) != 0) {
        return if (obj == py.pyTrue()) "t" else "f";
    }
    // Fallback: call str() on the object
    const str_obj = c.PyObject_Str(obj) orelse return null;
    defer c.Py_DecRef(str_obj);
    const cstr: [*c]const u8 = c.PyUnicode_AsUTF8(str_obj) orelse return null;
    return std.mem.span(cstr);
}

// ── COPY protocol ────────────────────────────────────────────────────────────
// COPY TO STDOUT: returns all rows as a Python list of strings
// COPY FROM STDIN: accepts a Python list of strings (rows)

/// After a COPY error/abort, consume the server's remaining messages up to
/// ReadyForQuery ('Z') so the pinned connection is left in the same clean state
/// a successful COPY leaves it — reusable, not desynced with an undrained error
/// response + ReadyForQuery still in the socket (F11). A read failure means the
/// wire is dead, so mark the connection failed instead.
fn drainCopyToReady(conn: *pg.Conn) void {
    while (true) {
        const msg = conn.read() catch {
            conn._state = .fail;
            return;
        };
        if (msg.type == 'Z') return;
    }
}

/// _db_copy_to(pinned_handle, sql) → list of row strings
pub fn db_copy_to(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    if (c.PyArg_ParseTuple(args, "ls", &handle, &sql_c) == 0) return null;

    const sql = std.mem.span(sql_c);
    trace("db_copy_to: handle={d} sql={s}", .{ handle, sql[0..@min(sql.len, 80)] });

    const conn = pinnedGet(@intCast(handle)) orelse {
        py.setError("COPY: invalid pinned handle {d}", .{handle});
        return null;
    };

    // Send the COPY SQL via simple query protocol
    // Query message: 'Q' + int32(len+4+1) + sql + \0
    sendSimpleQuery(conn, sql) catch {
        py.setError("COPY: failed to send query", .{});
        return null;
    };

    // Read response: expect CopyOutResponse ('H'), then CopyData ('d') messages, then CopyDone ('c')
    const result_list = c.PyList_New(0) orelse return null;

    while (true) {
        const msg = conn.read() catch {
            c.Py_DecRef(result_list);
            conn._state = .fail; // wire is broken — don't hand it back reusable
            py.setError("COPY: read error", .{});
            return null;
        };

        switch (msg.type) {
            'H' => {
                // CopyOutResponse — server is ready to send data
                // data[0] = format (0=text, 1=binary), then column count + format codes
                continue;
            },
            'd' => {
                // CopyData — one row of data
                const py_row = py.newString(msg.data) orelse {
                    // Aborting mid-stream — drain the rest so the pinned conn
                    // isn't left desynced with pending CopyData/ReadyForQuery.
                    drainCopyToReady(conn);
                    c.Py_DecRef(result_list);
                    return null;
                };
                if (c.PyList_Append(result_list, py_row) != 0) {
                    c.Py_DecRef(py_row);
                    drainCopyToReady(conn);
                    c.Py_DecRef(result_list);
                    return null;
                }
                c.Py_DecRef(py_row);
            },
            'c' => {
                // CopyDone — all data sent
                continue;
            },
            'C' => {
                // CommandComplete — "COPY N"
                continue;
            },
            'Z' => {
                // ReadyForQuery — done
                break;
            },
            'E' => {
                // ErrorResponse — a ReadyForQuery still follows; drain it so the
                // connection is reusable rather than stuck with a pending 'Z'.
                drainCopyToReady(conn);
                c.Py_DecRef(result_list);
                py.setError("COPY TO failed: server error", .{});
                return null;
            },
            else => continue,
        }
    }

    return result_list;
}

/// _db_copy_from(pinned_handle, sql, rows_list) → row count
pub fn db_copy_from(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c_long = -1;
    var sql_c: [*c]const u8 = null;
    var rows_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "lsO", &handle, &sql_c, &rows_obj) == 0) return null;

    const sql = std.mem.span(sql_c);
    trace("db_copy_from: handle={d} sql={s}", .{ handle, sql[0..@min(sql.len, 80)] });

    const conn = pinnedGet(@intCast(handle)) orelse {
        py.setError("COPY: invalid pinned handle {d}", .{handle});
        return null;
    };

    // Send the COPY SQL via simple query protocol
    sendSimpleQuery(conn, sql) catch {
        py.setError("COPY: failed to send query", .{});
        return null;
    };

    // Read CopyInResponse ('G')
    while (true) {
        const msg = conn.read() catch {
            conn._state = .fail;
            py.setError("COPY: read error", .{});
            return null;
        };
        switch (msg.type) {
            'G' => break, // CopyInResponse — ready for data
            'E' => {
                // ErrorResponse — a ReadyForQuery follows; drain it so the pinned
                // connection stays reusable (F11).
                drainCopyToReady(conn);
                py.setError("COPY FROM failed: server error", .{});
                return null;
            },
            else => continue,
        }
    }

    // Send CopyData messages for each row
    const rows_list = rows_obj orelse {
        py.setError("COPY: rows argument is null", .{});
        return null;
    };
    const row_count = c.PySequence_Size(rows_list);
    if (row_count < 0) {
        py.setError("COPY: rows must be a sequence", .{});
        return null;
    }

    // Batch CopyData rows into a 256 KiB buffer to amortize per-row syscalls.
    // Each row would otherwise cost 2 conn.write calls (header + body); at
    // hundreds of thousands of rows/sec that dominates throughput.
    const COPY_BATCH_BYTES: usize = 256 * 1024;
    var batch_buf: std.ArrayListUnmanaged(u8) = .empty;
    defer batch_buf.deinit(allocator);
    batch_buf.ensureTotalCapacity(allocator, COPY_BATCH_BYTES + 8192) catch {
        sendCopyFail(conn, "Out of memory");
        py.setError("COPY: out of memory for batch buffer", .{});
        return null;
    };

    var i: c.Py_ssize_t = 0;
    while (i < row_count) : (i += 1) {
        const py_row = c.PySequence_GetItem(rows_list, i) orelse {
            sendCopyFail(conn, "Failed to get row from Python list");
            py.setError("COPY: failed to get row {d}", .{i});
            return null;
        };
        defer c.Py_DecRef(py_row);

        var row_len: c.Py_ssize_t = 0;
        const row_ptr = c.PyUnicode_AsUTF8AndSize(py_row, &row_len) orelse {
            sendCopyFail(conn, "Row is not a string");
            py.setError("COPY: row {d} is not a string", .{i});
            return null;
        };
        const row_data = row_ptr[0..@intCast(row_len)];

        // Append CopyData frame: 'd' + int32(len+4) + data
        var header: [5]u8 = undefined;
        header[0] = 'd';
        std.mem.writeInt(u32, header[1..5], @intCast(row_data.len + 4), .big);
        batch_buf.appendSlice(allocator, &header) catch {
            sendCopyFail(conn, "Out of memory");
            py.setError("COPY: append header failed", .{});
            return null;
        };
        batch_buf.appendSlice(allocator, row_data) catch {
            sendCopyFail(conn, "Out of memory");
            py.setError("COPY: append body failed", .{});
            return null;
        };

        if (batch_buf.items.len >= COPY_BATCH_BYTES) {
            conn.write(batch_buf.items) catch {
                conn._state = .fail; // write failed mid-COPY — wire is unusable
                py.setError("COPY: failed to flush batch", .{});
                return null;
            };
            batch_buf.clearRetainingCapacity();
        }
    }

    // Flush any remaining batched rows.
    if (batch_buf.items.len > 0) {
        conn.write(batch_buf.items) catch {
            conn._state = .fail;
            py.setError("COPY: failed to flush final batch", .{});
            return null;
        };
        batch_buf.clearRetainingCapacity();
    }

    // Send CopyDone: 'c' + int32(4)
    sendCopyDone(conn) catch {
        conn._state = .fail;
        py.setError("COPY: failed to send CopyDone", .{});
        return null;
    };

    // Read CommandComplete + ReadyForQuery
    var copied: i64 = 0;
    while (true) {
        const msg = conn.read() catch {
            conn._state = .fail;
            py.setError("COPY: read error after CopyDone", .{});
            return null;
        };
        switch (msg.type) {
            'C' => {
                // CommandComplete: "COPY N\0"
                if (std.mem.startsWith(u8, msg.data, "COPY ")) {
                    const num_str = std.mem.trimEnd(u8, msg.data[5..], &[_]u8{0});
                    copied = std.fmt.parseInt(i64, num_str, 10) catch 0;
                }
            },
            'Z' => break,
            'E' => {
                // ErrorResponse after data — drain the trailing ReadyForQuery so
                // the connection is reusable (F11).
                drainCopyToReady(conn);
                py.setError("COPY FROM failed: server error after data", .{});
                return null;
            },
            else => continue,
        }
    }

    return py.newInt(copied);
}

// ── LISTEN/NOTIFY ────────────────────────────────────────────────────────────

/// Parsed connection target for a dedicated listener connection.
///
/// The listener thread cannot use the shared pool (a LISTEN connection is
/// long-lived and dedicated), so it opens its own connection from a DSN.
/// We mirror db_configure's std.Uri.parse handling EXACTLY (same fields,
/// same fallbacks) so a listener connects to the same database the app's
/// pool does. Previously listenerThread hardcoded host=localhost/port=5432/
/// database=hyperdjango_test and DROPPED the passed DSN, so the subscribe
/// leg silently ran against hyperdjango's own test DB instead of the app's.
const ListenerDsn = struct {
    host: []const u8,
    port: ?u16,
    database: []const u8,
    username: []const u8,
    password: ?[]const u8,
};

/// libpq-compatible default PostgreSQL user when a DSN omits it: $PGUSER, then
/// the OS login name ($USER), then "postgres". Hardcoding "postgres" broke
/// local/dev setups whose role is the OS user (e.g. Homebrew PostgreSQL has no
/// "postgres" role) — connections silently failed with
/// `role "postgres" does not exist`. Matches how psql/libpq resolve the user.
fn defaultPgUser() []const u8 {
    if (std.c.getenv("PGUSER")) |p| return std.mem.span(p);
    if (std.c.getenv("USER")) |p| return std.mem.span(p);
    return "postgres";
}

/// Parse a `postgres://user:pass@host:port/database` DSN into the fields a
/// dedicated listener connection needs. All returned slices are `dupe`d with
/// `alloc` (they must outlive the Python string the DSN came from, since the
/// listener thread holds them for its whole lifetime). Mirrors db_configure's
/// fallbacks: host→127.0.0.1, user→postgres, db→postgres, password→null.
/// Returns null on an unparseable DSN (caller raises a Python error).
fn parseListenerDsn(alloc: std.mem.Allocator, dsn: []const u8) ?ListenerDsn {
    const uri = std.Uri.parse(dsn) catch return null;

    const host_str: []const u8 = if (uri.host) |h|
        alloc.dupe(u8, h.percent_encoded) catch return null
    else
        alloc.dupe(u8, "127.0.0.1") catch return null;

    const user_str: []const u8 = if (uri.user) |u|
        alloc.dupe(u8, u.percent_encoded) catch return null
    else
        alloc.dupe(u8, defaultPgUser()) catch return null;

    const db_name: []const u8 = if (uri.path.percent_encoded.len > 1)
        alloc.dupe(u8, uri.path.percent_encoded[1..]) catch return null
    else
        alloc.dupe(u8, "postgres") catch return null;

    const pw_str: ?[]const u8 = if (uri.password) |p|
        alloc.dupe(u8, p.percent_encoded) catch return null
    else
        null;

    return .{
        .host = host_str,
        .port = uri.port,
        .database = db_name,
        .username = user_str,
        .password = pw_str,
    };
}

// ── Multiplexed LISTEN/NOTIFY ─────────────────────────────────────────────
//
// One listener CONNECTION + THREAD per DATABASE (keyed by connection string),
// LISTENing on many channels and demultiplexing NOTIFYs by channel name —
// replacing the old one-connection-and-thread-PER-CHANNEL model that scaled
// O(channels) (a chat app with 10k rooms → 10k threads + 10k PG connections).
// PostgreSQL tags every NotificationResponse with its channel, so a single
// connection can serve them all. This is now O(distinct databases).
//
// The listener thread owns the connection: it issues every LISTEN itself
// (fire-and-forget; the C/Z ack is consumed generically by the read loop, so
// there is no synchronous request/response to race against async NOTIFYs), a
// short read timeout lets it pick up newly-subscribed channels and observe
// shutdown, and a dropped connection self-heals by reconnecting and
// re-subscribing every registered channel.

// ── Listener observability (task #4) ────────────────────────────────────────
//
// The mux LISTEN thread runs detached with its own PyThreadState. Failures used
// to go to std.debug.print (raw stderr) with no path to Python. We now:
//   * bump lock-free metric counters (safe from the detached thread, no GIL);
//   * route the human-readable message through the framework logger via the
//     server.zig native-log bridge, acquiring the GIL through the thread's own
//     tstate. Every log site is reached with `ml.mutex` UNLOCKED — the
//     registration path (db_listen) holds the GIL then takes ml.mutex, so
//     taking the GIL while holding ml.mutex here would invert that order and
//     deadlock. muxConnect's connect/auth/timeout sites hold no lock; the
//     re-LISTEN-under-lock failure is counter-only for that reason.
var _dbl_metrics_init_flag: std.atomic.Value(u8) = std.atomic.Value(u8).init(0);
var _dbl_reconnects_counter: ?*metrics.DynCounter = null;
var _dbl_errors_counter: ?*metrics.DynCounter = null;
var _dbl_connected_gauge: ?*metrics.DynGauge = null;

fn initDbListenerMetrics() void {
    if (@cmpxchgStrong(u8, &_dbl_metrics_init_flag.raw, 0, 1, .acquire, .monotonic) != null) return;
    const alloc = std.heap.c_allocator;
    if (metrics.DynCounter.init()) |cnt| {
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .counter, .name = alloc.dupe(u8, "hyperdjango_db_listener_reconnects_total") catch return, .help = alloc.dupe(u8, "PG LISTEN mux connection (re)connects.") catch return, .impl = cnt };
        _ = metrics.registerEntry(entry) catch return;
        _dbl_reconnects_counter = cnt;
    } else |_| {}
    if (metrics.DynCounter.init()) |cnt| {
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .counter, .name = alloc.dupe(u8, "hyperdjango_db_listen_errors_total") catch return, .help = alloc.dupe(u8, "PG LISTEN mux errors (connect/auth/re-LISTEN/read/dropped notifications).") catch return, .impl = cnt };
        _ = metrics.registerEntry(entry) catch return;
        _dbl_errors_counter = cnt;
    } else |_| {}
    if (metrics.DynGauge.init()) |g| {
        const entry = alloc.create(metrics.MetricEntry) catch return;
        entry.* = .{ .kind = .gauge, .name = alloc.dupe(u8, "hyperdjango_db_listener_connected") catch return, .help = alloc.dupe(u8, "1 when the PG LISTEN mux connection is up, 0 when down.") catch return, .impl = g };
        _ = metrics.registerEntry(entry) catch return;
        _dbl_connected_gauge = g;
    } else |_| {}
}

/// Log a listener event through the framework logger. Bumps the error counter
/// when `is_error`. MUST be called with `ml.mutex` UNLOCKED (see the ordering
/// note above) and with `tstate` being this thread's PyThreadState. The GIL is
/// acquired only for the duration of the Python call.
fn dbListenerLog(tstate: ?*anyopaque, is_error: bool, comptime fmt: []const u8, args: anytype) void {
    if (is_error) {
        if (_dbl_errors_counter) |cnt| cnt.inc(1);
    }
    var buf: [512]u8 = undefined;
    const msg = std.fmt.bufPrint(&buf, "db listener: " ++ fmt, args) catch "db listener: (message too long)";
    py.PyEval_AcquireThread(tstate);
    defer py.PyEval_ReleaseThread(tstate);
    server.emitNativeLog(if (is_error) .@"error" else .warning, msg);
}

const MuxListener = struct {
    conn_str: []const u8, // registry key (owned)
    dsn: ListenerDsn, // owned
    mutex: py.Mutex = .{},
    running: bool = true,
    thread: ?std.Thread = null,
    // channel name (owned) -> Python callback (owned ref). Channels are never
    // removed for a process's lifetime (matches the old model), so a callback
    // pointer read under `mutex` stays valid without extra refcounting.
    channels: std.StringHashMapUnmanaged(*c.PyObject) = .empty,
    // channels awaiting a LISTEN on the wire (slices alias `channels` keys).
    pending: std.ArrayListUnmanaged([]const u8) = .empty,
};

var mux_listeners: std.StringHashMapUnmanaged(*MuxListener) = .empty;
var mux_mutex: py.Mutex = .{};

const MUX_READ_TIMEOUT_MS: u32 = 250;
const MUX_RECONNECT_DELAY_NS: u64 = 1 * std.time.ns_per_s;

/// _db_listen(conn_string, channel, callback) → 0
/// Registers `channel`→`callback` on the shared (per-database) multiplexed
/// listener, creating and starting it on first use for that connection string.
/// The int return is legacy/unused (the Python layer tracks channels itself).
pub fn db_listen(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var conn_str_c: [*c]const u8 = null;
    var channel_c: [*c]const u8 = null;
    var callback: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "ssO", &conn_str_c, &channel_c, &callback) == 0) return null;

    const conn_str = std.mem.span(conn_str_c);
    const channel = std.mem.span(channel_c);

    mux_mutex.lock();
    defer mux_mutex.unlock();

    const ml: *MuxListener = mux_listeners.get(conn_str) orelse blk: {
        // Parse the DSN up front, while we hold the GIL + the Python string,
        // so a bad DSN raises cleanly (the listener thread has no path back
        // to Python).
        const dsn = parseListenerDsn(allocator, conn_str) orelse {
            py.setError("LISTEN: invalid connection string: {s}", .{conn_str});
            return null;
        };
        const new_ml = allocator.create(MuxListener) catch {
            freeListenerDsn(allocator, dsn);
            py.setError("LISTEN: allocation failed", .{});
            return null;
        };
        const conn_owned = allocator.dupe(u8, conn_str) catch {
            allocator.destroy(new_ml);
            freeListenerDsn(allocator, dsn);
            py.setError("LISTEN: allocation failed", .{});
            return null;
        };
        new_ml.* = .{ .conn_str = conn_owned, .dsn = dsn };
        mux_listeners.put(allocator, conn_owned, new_ml) catch {
            allocator.free(conn_owned);
            allocator.destroy(new_ml);
            freeListenerDsn(allocator, dsn);
            py.setError("LISTEN: registry allocation failed", .{});
            return null;
        };
        new_ml.thread = std.Thread.spawn(.{}, muxListenerThread, .{new_ml}) catch {
            _ = mux_listeners.remove(conn_owned);
            allocator.free(conn_owned);
            allocator.destroy(new_ml);
            freeListenerDsn(allocator, dsn);
            py.setError("LISTEN: failed to spawn listener thread", .{});
            return null;
        };
        break :blk new_ml;
    };

    // Register the channel + callback (idempotent per channel).
    ml.mutex.lock();
    defer ml.mutex.unlock();
    if (!ml.channels.contains(channel)) {
        const chan_owned = allocator.dupe(u8, channel) catch {
            py.setError("LISTEN: allocation failed", .{});
            return null;
        };
        c.Py_IncRef(callback.?);
        ml.channels.put(allocator, chan_owned, callback.?) catch {
            allocator.free(chan_owned);
            c.Py_DecRef(callback.?);
            py.setError("LISTEN: channel registration failed", .{});
            return null;
        };
        // Queue the LISTEN. If the append fails we don't error: the channel is
        // in the map, so the next (re)connect re-subscribes it anyway.
        ml.pending.append(allocator, chan_owned) catch {};
    }

    return py.newInt(0);
}

/// Open + authenticate the listener connection, arm the read timeout, and
/// (re)subscribe every registered channel. Returns null on any failure so the
/// caller can back off and retry (self-healing reconnect).
fn muxConnect(ml: *MuxListener, tstate: ?*anyopaque) ?pg.Listener {
    var listener = pg.Listener.open(allocator, .{
        .host = ml.dsn.host,
        .port = ml.dsn.port,
    }) catch |err| {
        dbListenerLog(tstate, true, "open failed host={s} err={}", .{ ml.dsn.host, err });
        return null;
    };
    listener.auth(.{
        .username = ml.dsn.username,
        .database = ml.dsn.database,
        .password = ml.dsn.password,
    }) catch |err| {
        if (listener.err) |le| switch (le) {
            .pg => |pg_err| dbListenerLog(tstate, true, "auth failed db={s} user={s} pg={s}", .{ ml.dsn.database, ml.dsn.username, pg_err.message }),
            .err => |e| dbListenerLog(tstate, true, "auth failed db={s} user={s} err={}", .{ ml.dsn.database, ml.dsn.username, e }),
        } else dbListenerLog(tstate, true, "auth failed db={s} user={s} err={}", .{ ml.dsn.database, ml.dsn.username, err });
        listener.deinit();
        return null;
    };
    listener.setReadTimeout(MUX_READ_TIMEOUT_MS) catch |err| {
        dbListenerLog(tstate, true, "setReadTimeout failed err={}", .{err});
        listener.deinit();
        return null;
    };
    // (Re)subscribe every registered channel on this fresh connection. NOTE:
    // the re-LISTEN failure below runs under ml.mutex, so it is COUNTER-ONLY —
    // acquiring the GIL to log here would invert the db_listen lock order and
    // deadlock (see the ordering note on dbListenerLog).
    ml.mutex.lock();
    defer ml.mutex.unlock();
    var it = ml.channels.keyIterator();
    while (it.next()) |k| {
        listener.sendListen(k.*) catch {
            if (_dbl_errors_counter) |cnt| cnt.inc(1);
            listener.deinit();
            return null;
        };
    }
    // Everything mapped is now subscribed — clear the pending add-queue.
    ml.pending.clearRetainingCapacity();
    return listener;
}

fn muxListenerThread(ml: *MuxListener) void {
    // Python thread state for callback calls. PyInterpreterState_Main (NOT
    // _Get) because this std.Thread has no tstate yet.
    const interp = py.PyInterpreterState_Main();
    const tstate = py.PyThreadState_New(interp);

    initDbListenerMetrics();

    var listener: ?pg.Listener = null;
    defer {
        if (listener) |*l| l.deinit();
        if (_dbl_connected_gauge) |g| g.set(0); // thread exiting → not connected
    }

    while (@atomicLoad(bool, &ml.running, .monotonic)) {
        if (listener == null) {
            listener = muxConnect(ml, tstate) orelse {
                if (!@atomicLoad(bool, &ml.running, .monotonic)) break;
                py.sleep(MUX_RECONNECT_DELAY_NS);
                continue;
            };
            // Fresh connection established (initial connect or self-healing
            // reconnect): tally it and mark the listener up.
            if (_dbl_reconnects_counter) |cnt| cnt.inc(1);
            if (_dbl_connected_gauge) |g| g.set(1);
        }
        const l = &listener.?;

        // Issue any newly-queued LISTENs (no network I/O under the lock).
        var listen_failed = false;
        while (true) {
            ml.mutex.lock();
            if (ml.pending.items.len == 0) {
                ml.mutex.unlock();
                break;
            }
            const chan = ml.pending.items[ml.pending.items.len - 1];
            ml.pending.items.len -= 1;
            ml.mutex.unlock();
            l.sendListen(chan) catch {
                listen_failed = true;
                break;
            };
        }
        if (listen_failed) {
            // Mutex is unlocked here (released above before sendListen), so
            // GIL-logging is safe.
            dbListenerLog(tstate, true, "re-LISTEN on pending channel failed; reconnecting", .{});
            if (_dbl_connected_gauge) |g| g.set(0);
            l.deinit();
            listener = null;
            continue; // reconnect + re-subscribe
        }

        switch (l.nextDemux()) {
            .notification => |notif| {
                // Look up the callback under the lock (no GIL held), then call
                // it with the GIL. Callbacks are never freed while running, so
                // the pointer stays valid after we drop the lock.
                ml.mutex.lock();
                const cb = ml.channels.get(notif.channel);
                ml.mutex.unlock();
                if (cb == null) {
                    // A NOTIFY arrived for a channel we're LISTENing on but have
                    // no callback for — a dropped notification (invariant
                    // violation worth surfacing). Mutex already unlocked above.
                    dbListenerLog(tstate, true, "dropped notification: no callback for channel {s}", .{notif.channel});
                }
                if (cb) |callback| {
                    py.PyEval_AcquireThread(tstate);
                    const py_channel = py.newString(notif.channel);
                    const py_payload = py.newString(notif.payload);
                    if (py_channel != null and py_payload != null) {
                        const result = c.PyObject_CallFunctionObjArgs(callback, py_channel, py_payload, @as(?*c.PyObject, null));
                        // GIL is held here → route the callback's traceback through
                        // the framework logger (same bridge as the HTTP path)
                        // instead of dumping to raw fd 2.
                        if (result) |r| c.Py_DecRef(r) else server.reportNativeError("db listener callback");
                    }
                    if (py_channel) |p| c.Py_DecRef(p);
                    if (py_payload) |p| c.Py_DecRef(p);
                    py.PyEval_ReleaseThread(tstate);
                }
            },
            .other => {},
            .pg_error => |data| {
                dbListenerLog(tstate, true, "PG error: {s}", .{data});
            },
            .timed_out => {}, // benign — loop to check pending + running
            .closed => {
                dbListenerLog(tstate, true, "connection closed; reconnecting", .{});
                if (_dbl_connected_gauge) |g| g.set(0);
                l.deinit();
                listener = null;
                if (@atomicLoad(bool, &ml.running, .monotonic)) {
                    py.sleep(MUX_RECONNECT_DELAY_NS);
                }
            },
        }
    }

    // Clean up thread state — guard Py_DecRef against Python finalization.
    py.PyEval_AcquireThread(tstate);
    if (!module_shutting_down.load(.acquire)) {
        var it = ml.channels.valueIterator();
        while (it.next()) |cbp| c.Py_DecRef(cbp.*);
    }
    py.PyThreadState_Clear(tstate);
    py.PyThreadState_DeleteCurrent();
}

fn sendSimpleQuery(conn: *pg.Conn, sql: []const u8) !void {
    // Simple query protocol: 'Q' + int32(len) + sql + \0
    const payload_len: u32 = @intCast(sql.len + 5); // 4 (length field) + sql.len + 1 (null terminator)
    var header: [5]u8 = undefined;
    header[0] = 'Q';
    std.mem.writeInt(u32, header[1..5], payload_len, .big);
    try conn.write(&header);
    try conn.write(sql);
    try conn.write(&[_]u8{0});
}

fn sendCopyData(conn: *pg.Conn, data: []const u8) !void {
    // CopyData message: 'd' + int32(len+4) + data
    var header: [5]u8 = undefined;
    header[0] = 'd';
    std.mem.writeInt(u32, header[1..5], @intCast(data.len + 4), .big);
    try conn.write(&header);
    try conn.write(data);
}

fn sendCopyDone(conn: *pg.Conn) !void {
    // CopyDone: 'c' + int32(4)
    var msg: [5]u8 = undefined;
    msg[0] = 'c';
    std.mem.writeInt(u32, msg[1..5], 4, .big);
    try conn.write(&msg);
}

fn sendCopyFail(conn: *pg.Conn, reason: []const u8) void {
    // CopyFail: 'f' + int32(len+5) + reason + \0
    var header: [5]u8 = undefined;
    header[0] = 'f';
    std.mem.writeInt(u32, header[1..5], @intCast(reason.len + 5), .big);
    conn.write(&header) catch {
        conn._state = .fail;
        return;
    };
    conn.write(reason) catch {
        conn._state = .fail;
        return;
    };
    conn.write(&[_]u8{0}) catch {
        conn._state = .fail;
        return;
    };
    // The server answers CopyFail with ErrorResponse + ReadyForQuery. Drain both
    // so the pinned connection is left reusable rather than desynced (F11).
    drainCopyToReady(conn);
}

// ── Tests ──────────────────────────────────────────────────────────────────

fn freeListenerDsn(alloc: std.mem.Allocator, dsn: ListenerDsn) void {
    alloc.free(dsn.host);
    alloc.free(dsn.database);
    alloc.free(dsn.username);
    if (dsn.password) |p| alloc.free(p);
}

test "parseListenerDsn: full DSN uses passed host/port/db/user/password (regression for hardcoded hyperdjango_test)" {
    const a = std.testing.allocator;
    const dsn = parseListenerDsn(a, "postgres://meshuser:s3cret@db.internal:6543/mesh_dev").?;
    defer freeListenerDsn(a, dsn);

    try std.testing.expectEqualStrings("db.internal", dsn.host);
    try std.testing.expectEqual(@as(?u16, 6543), dsn.port);
    try std.testing.expectEqualStrings("mesh_dev", dsn.database);
    try std.testing.expectEqualStrings("meshuser", dsn.username);
    try std.testing.expectEqualStrings("s3cret", dsn.password.?);
}

test "parseListenerDsn: MESH dev DSN (localhost, no auth) parses database, not hyperdjango_test" {
    const a = std.testing.allocator;
    const dsn = parseListenerDsn(a, "postgres://localhost/mesh_dev").?;
    defer freeListenerDsn(a, dsn);

    try std.testing.expectEqualStrings("localhost", dsn.host);
    try std.testing.expectEqual(@as(?u16, null), dsn.port);
    try std.testing.expectEqualStrings("mesh_dev", dsn.database);
    // db_configure fallbacks: no user → defaultPgUser() (PGUSER/USER/"postgres"),
    // no password → null. Compare against defaultPgUser() itself so the
    // assertion is hermetic regardless of the environment's $USER/$PGUSER.
    try std.testing.expectEqualStrings(defaultPgUser(), dsn.username);
    try std.testing.expect(dsn.password == null);
}

test "parseListenerDsn: mesh_test DSN parses through" {
    const a = std.testing.allocator;
    const dsn = parseListenerDsn(a, "postgres://localhost:5432/mesh_test").?;
    defer freeListenerDsn(a, dsn);

    try std.testing.expectEqualStrings("mesh_test", dsn.database);
    try std.testing.expectEqual(@as(?u16, 5432), dsn.port);
}

test "parseListenerDsn: missing host/db fall back to 127.0.0.1/postgres (mirrors db_configure)" {
    const a = std.testing.allocator;
    // No path component → database defaults to "postgres".
    const dsn = parseListenerDsn(a, "postgres://example.com").?;
    defer freeListenerDsn(a, dsn);

    try std.testing.expectEqualStrings("example.com", dsn.host);
    try std.testing.expectEqualStrings("postgres", dsn.database);
}

test "parseListenerDsn: unparseable DSN returns null (caller raises Python error)" {
    const a = std.testing.allocator;
    try std.testing.expect(parseListenerDsn(a, "not a valid uri at all") == null);
}

test "initDbListenerMetrics: registers counters/gauge once, idempotent" {
    // First call wins the CAS and wires the metric handles; a second call is a
    // no-op (the CAS flag blocks re-registration). Best-effort: if the shared
    // registry is full, the handles stay null and there is nothing to assert.
    initDbListenerMetrics();
    const r1 = _dbl_reconnects_counter;
    const e1 = _dbl_errors_counter;
    const g1 = _dbl_connected_gauge;
    initDbListenerMetrics(); // must not re-register or swap the pointers
    try std.testing.expectEqual(r1, _dbl_reconnects_counter);
    try std.testing.expectEqual(e1, _dbl_errors_counter);
    try std.testing.expectEqual(g1, _dbl_connected_gauge);

    // The gauge set/counter inc paths are pointer-safe when wired.
    if (_dbl_connected_gauge) |gg| {
        gg.set(1);
        gg.set(0);
    }
    if (_dbl_reconnects_counter) |cc| cc.inc(1);
}

// ── Pinned-slot concurrency regression (R9) ──────────────────────────────────
// Stresses the free-threaded pinned_slots array: many OS threads concurrently
// claim / read / free slots. Exercises the atomic CAS-claim + publish + free
// mechanics directly (pinnedClaimSlot / pinnedGet / pinnedFreeSlot never
// dereference the connection, so fake, never-dereferenced pointers stand in for
// real *pg.Conn). Guards against the three hazards the fix targets:
//   (a) realloc UAF — impossible now (fixed, non-reallocating array);
//   (b) TOCTOU lost-slot / double-handle — two threads claiming one index;
//   (c) release-vs-claim leaving a leaked in_use flag or orphaned conn.
test "pinned_slots: concurrent claim/get/free is race-free (no lost slot, no double handle)" {
    // Reset the shared global array — earlier tests share this process.
    for (&pinned_slots) |*s| {
        s.in_use.store(false, .monotonic);
        s.conn.store(null, .monotonic);
    }

    const Worker = struct {
        fn run(id: usize, mismatches: *std.atomic.Value(usize)) void {
            var i: usize = 0;
            while (i < 5000) : (i += 1) {
                // Unique, non-null, correctly-aligned fake pointer per (id, i).
                const tag: usize = ((id + 1) << 24) | (i + 1);
                const fake: *pg.Conn = @ptrFromInt(tag * @alignOf(pg.Conn));
                const handle = pinnedClaimSlot(fake) orelse continue; // full → retry
                // While we hold the slot (in_use=true), no other thread can win
                // it, so a read MUST return our own pointer. A double-handle bug
                // would let another thread overwrite it → mismatch.
                if (pinnedGet(handle) != fake) {
                    _ = mismatches.fetchAdd(1, .monotonic);
                }
                if (pinnedFreeSlot(handle) != fake) {
                    _ = mismatches.fetchAdd(1, .monotonic);
                }
            }
        }
    };

    var mismatches = std.atomic.Value(usize).init(0);
    var threads: [8]std.Thread = undefined;
    for (&threads, 0..) |*t, id| {
        t.* = try std.Thread.spawn(.{}, Worker.run, .{ id, &mismatches });
    }
    for (&threads) |t| t.join();

    try std.testing.expectEqual(@as(usize, 0), mismatches.load(.monotonic));
    // Every slot must be free and null again — no leaked in_use, no orphan.
    for (&pinned_slots) |*s| {
        try std.testing.expect(!s.in_use.load(.monotonic));
        try std.testing.expect(s.conn.load(.monotonic) == null);
    }
}

// Resets the process-global enum_registry (earlier tests share this process).
fn testResetEnumRegistry() void {
    enum_registry_lock.lock();
    defer enum_registry_lock.unlock();
    for (enum_registry.items) |*e| freeLabelList(&e.labels);
    enum_registry.clearAndFree(allocator);
}

// Stresses the free-threaded enum_registry: writer threads publish/update enum
// entries — which realloc the backing ArrayList and FREE old label strings —
// while reader threads hammer findEnumByOid on the query-decode hot path. This
// is the exact shape of the fixed use-after-free / torn-slice bug: a lock-free
// reader iterating enum_registry.items while a writer reallocs+frees the buffer
// (or replaces an entry's labels) would crash or read stale memory. With the
// RwLock (readers lockShared + copy the scalar result out, writers exclusive)
// there must be no crash and no torn read. Fake OIDs + duped labels — this
// exercises the pure registry mechanics with no real connection.
test "enum_registry: concurrent register/update vs findEnumByOid is race-free" {
    testResetEnumRegistry();
    defer testResetEnumRegistry();

    // Invariant every writer upholds: for enum OID `o`, array_oid == o + 10000.
    // (n_oids is redeclared inside each fn — nested struct fns can't capture an
    // enclosing function's locals in Zig.)
    const Writer = struct {
        fn run(seed: usize) void {
            const n_oids: usize = 32;
            var i: usize = 0;
            while (i < 4000) : (i += 1) {
                const oid: i32 = 1000 + @as(i32, @intCast((seed + i) % n_oids));
                // Fresh label list each time: the update path frees the previous
                // list's strings while readers may be mid-scan (exclusive lock
                // must serialize this against lockShared readers).
                var labels: std.ArrayListUnmanaged([]const u8) = .empty;
                var k: usize = 0;
                while (k < 3) : (k += 1) {
                    const dup = allocator.dupe(u8, "mood_label") catch break;
                    labels.append(allocator, dup) catch {
                        allocator.free(dup);
                        break;
                    };
                }
                _ = publishEnumEntry(oid, oid + 10000, labels);
            }
        }
    };
    const Reader = struct {
        fn run(torn: *std.atomic.Value(usize)) void {
            const n_oids: usize = 32;
            var i: usize = 0;
            while (i < 20000) : (i += 1) {
                const oid: i32 = 1000 + @as(i32, @intCast(i % n_oids));
                if (findEnumByOid(oid)) |look| {
                    // Scalar OID match must carry the writer's invariant; any
                    // other value means a torn / freed-buffer read slipped through.
                    if (look.array_oid != oid + 10000) _ = torn.fetchAdd(1, .monotonic);
                }
                _ = findEnumByOid(oid + 10000); // array-OID match path
                _ = findEnumByOid(999_999); // guaranteed miss (full scan)
            }
        }
    };

    var torn = std.atomic.Value(usize).init(0);
    var writers: [4]std.Thread = undefined;
    var readers: [4]std.Thread = undefined;
    for (&writers, 0..) |*t, s| t.* = try std.Thread.spawn(.{}, Writer.run, .{s});
    for (&readers) |*t| t.* = try std.Thread.spawn(.{}, Reader.run, .{&torn});
    for (&writers) |t| t.join();
    for (&readers) |t| t.join();

    try std.testing.expectEqual(@as(usize, 0), torn.load(.monotonic));
}
