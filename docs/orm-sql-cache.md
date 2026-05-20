# Compiled SQL Cache

HyperDjango's ORM caches the **SQL string itself**, not just the result rows.
Once a query shape has been compiled once, every subsequent call with the same
structure skips WhereNode tree construction entirely — it just collects bind
values and returns the cached template.

**Result:** 520,000 qps on cache hits, ~2× faster than re-compiling.

---

## Request lifecycle

```
                       User.objects.filter(active=True).order_by("-id")[:20]
                                                │
                                                ▼
                                ┌───────────────────────────────┐
                                │   _build_select() entry       │
                                │   in hyperdjango/query.py     │
                                └───────────────┬───────────────┘
                                                │
                                                ▼
                          ┌────────────────────────────────────────┐
                          │  Cacheable?                            │
                          │   ✗ Expression annotations             │── NO ──┐
                          │   ✗ Exists / NotExists subqueries      │        │
                          │   ✗ with_cte() clauses                 │        │
                          └────────────────┬───────────────────────┘        │
                                           │ YES                            │
                                           ▼                                │
                          ┌────────────────────────────────────────┐        │
                          │  _fast_cache_key(meta)                 │        │
                          │  ┌──────────────────────────────────┐  │        │
                          │  │ id(meta)                         │  │        │
                          │  │ _fast_where_key()  ──► Zig FNV-1a│  │        │
                          │  │ ordering tuple                   │  │        │
                          │  │ limit                            │  │        │
                          │  └──────────────────────────────────┘  │        │
                          │  4-tuple for ~90% of queries,          │        │
                          │  11-tuple for complex shapes           │        │
                          └────────────────┬───────────────────────┘        │
                                           │                                │
                                           ▼                                │
                          ┌────────────────────────────────────────┐        │
                          │  _compiled_sql_cache.get(key)          │        │
                          │  (lock-free dict.get)                  │        │
                          └─────────┬────────────────┬─────────────┘        │
                                    │                │                      │
                              HIT   │                │   MISS               │
                                    │                │                      │
                                    ▼                ▼                      ▼
                  ┌─────────────────────────┐  ┌────────────────────────────────┐
                  │ _collect_where_params() │  │ _build_where_tree()            │
                  │                         │  │   → WhereNode tree             │
                  │ Walks filters/excludes  │  │ .compile(start_idx)            │
                  │ tuple list directly.    │  │   → SQL fragment + params      │
                  │ Zero tree alloc.        │  │                                │
                  │ Inlined passthrough     │  │ Assemble parts:                │
                  │ for exact/gt/gte/lt/    │  │   SELECT … FROM … JOIN …       │
                  │ lte/iexact/regex.       │  │   WHERE … GROUP BY …           │
                  │                         │  │   ORDER BY … LIMIT … OFFSET …  │
                  └────────────┬────────────┘  └────────────────┬───────────────┘
                               │                                │
                               │                                ▼
                               │              ┌──────────────────────────────────┐
                               │              │ with _compiled_cache_lock:       │
                               │              │   _compiled_sql_cache[key] = sql │
                               │              └────────────────┬─────────────────┘
                               │                               │
                               └────────────────┬──────────────┘
                                                │
                                                ▼
                                ┌──────────────────────────────┐
                                │  return (sql_template,       │
                                │          bind_params)        │
                                └──────────────┬───────────────┘
                                               │
                                               ▼
                                ┌──────────────────────────────┐
                                │  pg.zig prepared statement   │
                                │  cache + execute             │
                                └──────────────────────────────┘
```

The HIT path is the hot path. After the first call to any given query shape,
every subsequent call short-circuits at the `dict.get()` and feeds straight
into the pg.zig prepared-statement cache. No tree, no string assembly, no
allocator churn.

---

## Two caches, two purposes

HyperDjango ships **two independent caches** along the query path. They look
related but live in different files and solve different problems:

```
                                  Query execution
                                          │
            ┌─────────────────────────────┴─────────────────────────────┐
            │                                                           │
            ▼                                                           ▼
┌───────────────────────────┐                           ┌───────────────────────────┐
│  Compiled SQL Cache       │                           │  Query Result Cache       │
│  hyperdjango/query.py     │                           │  hyperdjango/query_cache. │
│                           │                           │  py                       │
│  Stores: SQL template     │                           │                           │
│          strings          │                           │  Stores: row results      │
│                           │                           │                           │
│  Keyed by:                │                           │  Keyed by:                │
│  • Model identity         │                           │  • Table name             │
│  • Query structure        │                           │  • Full SQL text          │
│  • FNV-1a where hash      │                           │  • Bind values            │
│                           │                           │                           │
│  Invalidated:             │                           │  Invalidated:             │
│  • Never (templates are   │                           │  • post_save / post_delete│
│    pure functions of      │                           │    signals                │
│    query structure)       │                           │  • FK dependency cascade  │
│  • Process restart only   │                           │  • Meta.cache_ttl expiry  │
│                           │                           │                           │
│  Eviction:                │                           │  Eviction:                │
│  • None — bounded by      │                           │  • LRU + TTL              │
│    schema cardinality     │                           │                           │
└───────────────────────────┘                           └───────────────────────────┘
            │                                                           │
            └─────────────────────────────┬─────────────────────────────┘
                                          ▼
                              ┌──────────────────────────┐
                              │  pg.zig prepared stmts   │
                              │  Cached parse plans      │
                              └──────────────────────────┘
```

The compiled SQL cache is essentially free to keep forever — the same query
shape always produces the same SQL text, regardless of model data. The result
cache needs invalidation; the template cache never does.

---

## Cache key fingerprint

`_fast_cache_key(meta)` produces one of two tuple shapes. Different lengths
never collide in dict lookup, so the dispatcher is implicit:

```
                            ┌────────────────────────────────────┐
                            │  Common case?                      │
                            │  no values/only/defer/annotations/ │
                            │  group_by/select_related/distinct/ │
                            │  for_update + no offset            │
                            └────────────┬───────────────────────┘
                                         │
                              ┌──────────┴──────────┐
                              │ YES                 │ NO
                              ▼                     ▼
              ┌─────────────────────────┐   ┌────────────────────────────┐
              │  Compact 4-tuple        │   │  Full 11-tuple             │
              │ ┌─────────────────────┐ │   │ ┌────────────────────────┐ │
              │ │ id(meta)            │ │   │ │ id(meta)               │ │
              │ │ _fast_where_key()   │ │   │ │ col_key (v/o/d)        │ │
              │ │ ordering            │ │   │ │ select_related         │ │
              │ │ limit               │ │   │ │ _fast_where_key()      │ │
              │ └─────────────────────┘ │   │ │ annotation key         │ │
              │                         │   │ │ ordering               │ │
              │  ~90% of queries        │   │ │ limit                  │ │
              │  in production hit      │   │ │ offset                 │ │
              │  this path              │   │ │ distinct               │ │
              │                         │   │ │ for_update             │ │
              └─────────────────────────┘   │ │ group_by key           │ │
                                            │ └────────────────────────┘ │
                                            └────────────────────────────┘
```

### `_fast_where_key()` — the inner fingerprint

The WHERE clause is the most structurally complex part. Hashing it well is
what makes the fast path fast:

```
                            ┌────────────────────────────────────┐
                            │  Filter tree contains Q objects    │
                            │  or raw WHERE fragments?           │
                            └────────────┬───────────────────────┘
                                         │
                              ┌──────────┴──────────┐
                              │ NO                  │ YES
                              ▼                     ▼
              ┌─────────────────────────┐   ┌────────────────────────────┐
              │ Zig native FNV-1a hash  │   │ Python tuple traversal     │
              │                         │   │                            │
              │ _where_cache_key(       │   │ Recursive Q._structural_   │
              │   filters, excludes)    │   │ key() walk + value shape   │
              │                         │   │ extraction                 │
              │ • METH_FASTCALL         │   │                            │
              │ • Iterates tuples       │   │ Slower path, but correct   │
              │   directly              │   │ for arbitrarily nested     │
              │ • Zero list alloc       │   │ AND/OR/NOT compositions    │
              │ • Returns u64           │   │                            │
              └─────────────────────────┘   └────────────────────────────┘
```

The native hash is one FFI call. The Python path is only taken when Q-objects
or raw SQL fragments are in play.

---

## Skip conditions — when the cache is intentionally bypassed

Three query shapes cannot be cached because they embed per-call SQL fragments
or parameter positions that vary between invocations:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. Expression annotations                                                │
│    .annotate(rank=SearchRank(...))                                       │
│    .annotate(avg=Avg("score"))                                           │
│                                                                          │
│    Why skip: expressions emit their own SQL fragments with their         │
│              own bind params. Template would not be reusable.            │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│ 2. Exists / NotExists correlated subqueries                              │
│    .filter(Exists(Other.objects.filter(parent_id=OuterRef("id"))))       │
│                                                                          │
│    Why skip: OuterRef substitution uses per-call nonce sentinels         │
│              (128 bits of entropy) so the same logical query produces    │
│              different SQL on every call.                                │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│ 3. with_cte() clauses                                                    │
│    .with_cte("ancestors", recursive_body_sql, *params, recursive=True)   │
│                                                                          │
│    Why skip: CTE params interleave into the WITH prefix and {idx}        │
│              placeholders renumber on every call. Template wouldn't      │
│              match the param positions.                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

These all fall back to the full WhereNode tree compile path, which still
works correctly — just without the cache speedup. Anything that doesn't hit
one of these three conditions is automatically cached.

---

## Coverage across builders

All four SQL builders share the same `_compiled_sql_cache` storage (COUNT
gets its own `_compiled_count_cache` keyspace to avoid key collisions):

```
┌──────────────────┬────────────────────────┬────────────────────────┐
│ Builder          │ Cache key              │ Skip condition         │
├──────────────────┼────────────────────────┼────────────────────────┤
│ _build_select    │ _fast_cache_key(meta)  │ Expressions / Exists / │
│                  │                        │ with_cte               │
├──────────────────┼────────────────────────┼────────────────────────┤
│ _build_count     │ (id(meta),             │ (same as select)       │
│                  │  _fast_where_key())    │                        │
├──────────────────┼────────────────────────┼────────────────────────┤
│ _build_update    │ (id(meta), "U",        │ F() in SET values      │
│                  │  set_cols,             │                        │
│                  │  where_key,            │                        │
│                  │  returning_cols)       │                        │
├──────────────────┼────────────────────────┼────────────────────────┤
│ _build_delete    │ (id(meta), "D",        │ (none — always cached) │
│                  │  where_key)            │                        │
└──────────────────┴────────────────────────┴────────────────────────┘
```

The discriminator strings `"U"` and `"D"` ensure UPDATE/DELETE keys never
collide with SELECT keys for the same model and WHERE shape.

---

## Free-threading and concurrency

The cache is built for Python 3.14t (GIL disabled). Three properties make
this safe under contention:

```
              ┌─────────────────────────────────────────────────┐
              │  Reads (cache HIT path)                         │
              │  ┌───────────────────────────────────────────┐  │
              │  │  _compiled_sql_cache.get(key)             │  │
              │  │  Lock-free. dict.get is atomic in CPython │  │
              │  │  even under free-threading.               │  │
              │  └───────────────────────────────────────────┘  │
              └─────────────────────────────────────────────────┘

              ┌─────────────────────────────────────────────────┐
              │  Writes (cache MISS → store)                    │
              │  ┌───────────────────────────────────────────┐  │
              │  │  with _compiled_cache_lock:               │  │
              │  │    _compiled_sql_cache[key] = sql         │  │
              │  │                                           │  │
              │  │  Two threads computing the same MISS in   │  │
              │  │  parallel will both write the SAME sql    │  │
              │  │  string. Last write wins. Idempotent.     │  │
              │  └───────────────────────────────────────────┘  │
              └─────────────────────────────────────────────────┘

              ┌─────────────────────────────────────────────────┐
              │  Key construction                               │
              │  ┌───────────────────────────────────────────┐  │
              │  │  _fast_cache_key is a pure function of    │  │
              │  │  the QuerySet's frozen state at the       │  │
              │  │  moment _build_select is called.          │  │
              │  │                                           │  │
              │  │  QuerySet is cloned on every chain step,  │  │
              │  │  so no shared mutable state.              │  │
              │  └───────────────────────────────────────────┘  │
              └─────────────────────────────────────────────────┘
```

Validated by an 8-thread × 1000-iteration concurrent stress test in
`scripts/test_free_threading_stress.py`.

---

## Measured performance

| Path          | Throughput | Notes                                         |
| ------------- | ---------- | --------------------------------------------- |
| Cache HIT     | 520K qps   | dict.get + `_collect_where_params` walk       |
| Cache MISS    | ~260K qps  | Full WhereNode tree compile + SQL assembly    |
| Speedup       | ~2.0×      | Per-query, end-to-end SQL build cost          |
| FFI hash call | ~280 ns    | `_where_cache_key` Zig FNV-1a (METH_FASTCALL) |

Numbers from `scripts/bench_where_compile.py` (multi-run median, jitter < 5%,
ReleaseFast Zig build).

In production, the HIT path takes the per-call SQL-build cost from
microseconds to nanoseconds, leaving the database round-trip as the
dominant remaining cost. That's where you want it.
