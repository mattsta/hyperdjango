# Database

pg.zig native PostgreSQL driver -- 2-5x faster than psycopg3. Binary protocol, prepared statement caching, connection pooling, COPY protocol, pipelined queries.

HyperDjango uses a single database backend: the pg.zig native Zig extension compiled into `_hyperdjango_native.so`. There are no fallbacks to asyncpg, psycopg2, or any other Python driver. If the native extension is not built, database operations fail immediately with a clear build instruction.

## Connection

### Database URL Format

Connections use standard PostgreSQL connection URLs:

```
postgres://user:password@host:port/dbname
```

All components are optional with sensible defaults:

| Component  | Default               | Description           |
| ---------- | --------------------- | --------------------- |
| `user`     | OS username (`$USER`) | PostgreSQL role       |
| `password` | (none)                | Role password         |
| `host`     | `localhost`           | Server hostname or IP |
| `port`     | `5432`                | Server port           |
| `dbname`   | (required)            | Database name         |

Examples:

```python
# Full URL
db = Database("postgres://myuser:secret@db.example.com:5432/myapp")

# Minimal — uses OS username, localhost, default port
db = Database("postgres://localhost/myapp")

# With password, default host
db = Database("postgres://admin:hunter2@localhost/myapp")
```

If the URL omits the username, password, or host, HyperDjango fills them from the standard libpq variables (`PGUSER`/`PGPASSWORD`/`PGHOST`) and then from `$USER` (or `$USERNAME` on Windows) / `localhost`, falling back to the role `postgres`. A URL that names **no database** is rejected rather than silently connecting to a role-named default.

### Choosing the database — one resolver

"Which database" is resolved by a single authority, `hyperdjango.conf.resolve_database_url()`, so you set **one** variable in whichever convention you prefer and every component agrees — the server / `get_db()`, the production-config guard, the native driver, and every `hyper` CLI command (`setup`, `seed`, `migrate`, `makemigrations`, `shell`, `dbshell`, `inspectdb`, `dumpdata`, `loaddata`). The precedence, first non-empty wins:

| #   | Source                                                                            | Convention                                   |
| --- | --------------------------------------------------------------------------------- | -------------------------------------------- |
| 1   | `HyperApp(database=...)` constructor / Django `HYPERDJANGO_DATABASE_URL`          | explicit override (beats every env var)      |
| 2   | `HYPER_DATABASE_URL` env var / `.env`                                             | framework prefix                             |
| 3   | `DATABASE_URL` env var / `.env`                                                   | 12-factor / libpq URI                        |
| 4   | libpq `PG*` set (`PGDATABASE` + optional `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`) | assembled when `PGDATABASE` is set           |
| 5   | (none)                                                                            | not configured — callers raise a clear error |

`get_setting("DATABASE_URL")` delegates to this resolver, so setting a single `DATABASE_URL` (or `HYPER_DATABASE_URL`, or the `PG*` set) is enough for both the CLI (`hyper setup`/`seed`) and the running server to use the exact same database — no need to set several variables to the same value.

### Creating a Connection

```python
from hyperdjango.database import Database

db = Database("postgres://user:pass@localhost/mydb", max_size=32)
await db.connect()
```

### Automatic Connection (Recommended)

When using `HyperApp`, database connections are managed automatically. Pass `database=` to the constructor and the pool is created lazily on first query:

```python
from hyperdjango import HyperApp

app = HyperApp(
    title="My App",
    database="postgres://localhost/mydb",  # Auto-connect on first query
)


@app.get("/users")
async def list_users(request):
    # get_db() is called automatically — pool created on first use
    users = await User.objects.all()
    return [u.model_dump() for u in users]
```

The `DATABASE_URL` is stored in the settings system. `get_db()` lazily creates the pool the first time any model query runs. There is no need to manually call `connect()` or `set_db()`.

### Unified Connection Pool

HyperDjango uses a **single shared connection pool** for both the Zig HTTP server and the Python ORM. When `app.run()` starts the Zig server:

1. `get_db()` creates the pool (pg.zig native pool, 32 connections default)
2. The pool handle is shared with the Zig server via `configure_db_handle()`
3. All database operations — Zig-native model routes and Python ORM queries — use the same pool

This eliminates connection competition and maximizes pool utilization across the 24-thread Zig worker pool.

### Database Class API

```python
class Database:
    def __init__(self, url: str, min_size: int = 2, max_size: int = 32): ...
```

**Constructor parameters:**

| Parameter  | Type  | Default    | Description                                                 |
| ---------- | ----- | ---------- | ----------------------------------------------------------- |
| `url`      | `str` | (required) | PostgreSQL connection URL                                   |
| `min_size` | `int` | `2`        | Minimum connections in pool                                 |
| `max_size` | `int` | `32`       | Maximum connections in pool (should be >= Zig thread count) |

**Connection methods:**

| Method       | Signature                            | Description                                                                                        |
| ------------ | ------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `connect`    | `async def connect(self) -> None`    | Create the connection pool. Calls `_db_configure` in pg.zig. Idempotent -- second call is a no-op. |
| `disconnect` | `async def disconnect(self) -> None` | Close all connections and release the pool. Safe to call multiple times.                           |

**Properties:**

| Property       | Type   | Description                                          |
| -------------- | ------ | ---------------------------------------------------- |
| `is_connected` | `bool` | `True` if pool is active                             |
| `backend`      | `str`  | Returns `"pgzig"` when connected, `"none"` otherwise |
| `url`          | `str`  | Connection URL passed to constructor                 |
| `min_size`     | `int`  | Minimum pool size                                    |
| `max_size`     | `int`  | Maximum pool size                                    |

### Global Database Instance

HyperDjango maintains a single global database instance via `get_db()`:

```python
from hyperdjango.database import get_db

db = get_db()  # Auto-creates from DATABASE_URL on first call
```

`get_db()` behavior:

- **First call**: reads `DATABASE_URL` from settings, creates a `Database` instance, connects the pool, returns it
- **Subsequent calls**: returns the cached instance (thread-safe, double-check locking)
- **No DATABASE_URL**: raises `RuntimeError` with a clear message

The `HyperApp` class pushes `database=` into `DATABASE_URL` automatically:

```python
from hyperdjango import HyperApp

app = HyperApp(database_url="postgres://localhost/myapp")
# db is connected and set_db() called during app startup
```

## Queries

### query -- Multiple Rows

```python
async def query(self, sql: str, *args) -> list[dict[str, Any]]
```

Execute a SQL query and return all rows as a list of dictionaries. Column names come from `_db_get_last_columns()` which returns `list[tuple[str, int]]` (name, OID pairs).

```python
# All users over 18
rows = await db.query("SELECT * FROM users WHERE age > $1", 18)
# [{"id": 1, "name": "Alice", "age": 30}, {"id": 2, "name": "Bob", "age": 25}]

# With multiple parameters
rows = await db.query(
    "SELECT * FROM orders WHERE user_id = $1 AND status = $2", user_id, "shipped"
)

# Empty result returns empty list
rows = await db.query("SELECT * FROM users WHERE id = $1", 999999)
# []
```

**Parameters use `$1`, `$2`, ... positional placeholders** (PostgreSQL native protocol), not `%s` or `?`.

### query_one -- Single Row

```python
async def query_one(self, sql: str, *args) -> dict[str, Any] | None
```

Execute a query and return the first row as a dictionary, or `None` if no rows match.

```python
user = await db.query_one("SELECT * FROM users WHERE id = $1", 1)
if user:
    print(user["name"])  # "Alice"

# Returns None for no match
missing = await db.query_one("SELECT * FROM users WHERE id = $1", 999999)
assert missing is None
```

### query_val -- Single Scalar

```python
async def query_val(self, sql: str, *args) -> Any | None
```

Execute a query and return the first column of the first row. Ideal for aggregates and existence checks.

```python
count = await db.query_val("SELECT COUNT(*) FROM users")
# 42

exists = await db.query_val(
    "SELECT EXISTS(SELECT 1 FROM users WHERE email = $1)", email
)
# True or False

max_age = await db.query_val("SELECT MAX(age) FROM users")
# 98
```

### execute -- INSERT/UPDATE/DELETE

```python
async def execute(self, sql: str, *args) -> int
```

Execute a statement that modifies data and return the **affected-row count** as a
plain `int` — `1` for a single-row insert, `5` for an update that touched five
rows, `0` when nothing matched.

```python
# Insert
rows = await db.execute(
    "INSERT INTO users (name, email) VALUES ($1, $2)", "Alice", "alice@example.com"
)
# 1

# Update — how many rows changed?
changed = await db.execute(
    "UPDATE users SET active = $1 WHERE last_login < $2", False, cutoff_date
)
# 15

# Delete — act on the count directly, no parsing
deleted = await db.execute("DELETE FROM sessions WHERE expires_at < NOW()")
if deleted:
    logger.info("purged %d expired sessions", deleted)
# 203
```

The count comes straight from PostgreSQL's command tag. Use it directly — there
is no status string to `.split()` or pattern-match. `QuerySet.update()` and
`QuerySet.delete()` return this same integer.

### execute_many -- Batch Execution

```python
async def execute_many(self, sql: str, args_list: list[tuple]) -> None
```

Execute the same statement with multiple parameter sets. Each set is executed sequentially.

```python
await db.execute_many(
    "INSERT INTO tags (name) VALUES ($1)", [("python",), ("zig",), ("postgresql",)]
)
```

### Parameter Types

Parameters are converted to PostgreSQL wire types automatically:

| Python Type | PostgreSQL Type | Example            |
| ----------- | --------------- | ------------------ |
| `int`       | `int4` / `int8` | `42`               |
| `float`     | `float8`        | `3.14`             |
| `str`       | `text`          | `"hello"`          |
| `bool`      | `bool`          | `True`             |
| `None`      | `NULL`          | `None`             |
| `bytes`     | `bytea`         | `b"\x00\x01"`      |
| `datetime`  | `timestamptz`   | `datetime.now()`   |
| `date`      | `date`          | `date.today()`     |
| `UUID`      | `uuid`          | `uuid.uuid4()`     |
| `Decimal`   | `numeric`       | `Decimal("19.99")` |
| `list[int]` | `int4[]`        | `[1, 2, 3]`        |
| `list[str]` | `text[]`        | `["a", "b"]`       |
| `dict`      | `jsonb`         | `{"key": "val"}`   |

#### Lists and dicts bind to both JSONB and native array columns

A Python `list` or `dict` binds correctly whether the target column is `JSONB`
**or** a native PostgreSQL array (`int[]`, `text[]`, `timestamptz[]`, …) — the
driver coerces to whichever the column expects:

- Into a **`JSONB`** column, values are stored as canonical JSON. An empty list
  becomes `[]` and an empty dict becomes `{}` (not the ambiguous `{}` that a
  text-only encoding produced for both).
- Into a **native array** column, a `list` is encoded as a PostgreSQL array
  literal — including the empty list as an empty array (`{}`) — for the
  `int`/`text`/`bool`/`float`/`numeric`/`timestamp[]` element types.

```python
# JSONB column — stored as JSON, empty containers disambiguated
await db.execute("INSERT INTO docs (meta) VALUES ($1)", {"k": "v"})
await db.execute("INSERT INTO docs (meta) VALUES ($1)", [])  # -> []
await db.execute("INSERT INTO docs (meta) VALUES ($1)", {})  # -> {}

# Native int[] column — same Python list, encoded as a PG array
await db.execute("INSERT INTO points (coords) VALUES ($1)", [10, 20, 30])
```

You can bind a Python list or dict directly; there is no need to pre-encode it to a JSON string.

### Result serialization to JSON

The native fast path (`db.query_json`, and REST list/retrieve when
`use_native_json=True`) renders PostgreSQL rows straight from the binary wire
protocol into JSON bytes — no intermediate Python objects. To stay lossless and
always emit **valid** JSON, a few types map to a JSON form that is not the
"obvious" one. These transformations are stable and intentional:

| PostgreSQL type              | JSON form           | Notes                                                                                                                                                                                                         |
| ---------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `int2/4/8`                   | number              | —                                                                                                                                                                                                             |
| `float4/8` (finite)          | number              | —                                                                                                                                                                                                             |
| `float4/8` (`NaN`/`±Inf`)    | **string**          | `"NaN"` / `"Infinity"` / `"-Infinity"` — JSON has no such literal. Parse with `float()`.                                                                                                                      |
| `numeric` / `decimal`        | **string**          | Exact decimal, e.g. `"19.99"`. A JSON _number_ is an IEEE-754 double and would silently lose precision past ~15–17 digits — so NUMERIC is a string. `NaN`/`Infinity`/`-Infinity` too. Parse with `Decimal()`. |
| `money`                      | **string**          | Sign-correct decimal, e.g. `"-0.50"` (matches `str(Decimal)`).                                                                                                                                                |
| `bool`                       | `true` / `false`    | —                                                                                                                                                                                                             |
| `text`/`varchar`/`char`/enum | string              | —                                                                                                                                                                                                             |
| `json` / `jsonb`             | embedded JSON       | Passed through as-is (compact).                                                                                                                                                                               |
| `timestamp`/`timestamptz`    | string              | Naive ISO-8601 `YYYY-MM-DDTHH:MM:SS[.ffffff]` (no `Z`); sub-second preserved.                                                                                                                                 |
| `date`                       | string              | `YYYY-MM-DD`. A BC/out-of-range date → `null` (not representable).                                                                                                                                            |
| `time`                       | string              | `HH:MM:SS[.ffffff]`; PostgreSQL's `24:00:00` is preserved verbatim.                                                                                                                                           |
| `uuid`                       | string              | Canonical lowercase hyphenated.                                                                                                                                                                               |
| `bytea`                      | **string**          | PostgreSQL `\xDEADBEEF` hex (never raw bytes — those aren't valid JSON/UTF-8).                                                                                                                                |
| `macaddr`/`macaddr8`         | string              | Colon-hex, e.g. `"08:00:2b:01:02:03"`.                                                                                                                                                                        |
| arrays (incl. multi-dim)     | JSON array          | Element type mapped per this table; nesting preserved.                                                                                                                                                        |
| range types                  | `null`              | Not yet binary-decoded — emitted as `null` rather than a mangled value.                                                                                                                                       |
| other binary types           | **`\x`-hex string** | Any type without a dedicated decoder is emitted as a lossless hex string, never raw.                                                                                                                          |
| `NULL`                       | `null`              | —                                                                                                                                                                                                             |

The key rule: **a value is never silently dropped**. Types with no JSON literal
(non-finite floats, arbitrary-precision decimals, binary blobs) become strings
that round-trip, not `null`. Only genuinely undecodable inputs (a BC date, an
unsupported range) become `null`, and never poison the connection.

The object path (`db.query` / `db.query_one`) instead returns native Python
objects — `Decimal`, `datetime`, `bytes`, `float('inf')`, `uuid.UUID` — so use
it when you want typed values rather than JSON text.

### Native auto-CRUD routes

`app.add_db_route(...)` registers a route the Zig server answers **entirely in
native code** — HTTP request → native router → row → JSON bytes → socket, never
entering Python. It reuses the lossless serialization above.

```python
# GET /articles/{id}  →  SELECT * FROM articles WHERE id = $1, served natively
app.add_db_route(
    "GET",
    "/articles/{id}",
    table="articles",
    op="select_one",
    pk_column="id",
    pk_param="id",
)
```

> ⚠️ **Security — this bypasses the entire Python request cycle.** No middleware
> runs, which means **no authentication, no tenancy scoping, no rate limiting, and
> no per-object permission checks**. A `select_one` route is a raw
> `SELECT … WHERE {pk} = $1` — anyone who can reach the URL can read any row by
> primary key. Use it **only** for data that is already fully public and
> non-tenant-scoped (a published article, a public product catalog). For anything
> user-owned, tenant-scoped, or access-controlled, use a normal `@app.get` view or
> a `ModelViewSet` so auth/tenancy middleware applies. `table` must be a fixed
> string — never interpolate request input into it.

Ops: `select_one`, `select_list`, `insert`, `delete`, `custom_query`,
`custom_query_single`.

## Query Plan Analysis (EXPLAIN)

Analyze query performance with the native `db.explain()` API. Returns structured results with plan tree parsing, index usage detection, and execution timing.

### Basic Usage

```python
# Plan without executing
result = await db.explain("SELECT * FROM users WHERE id = $1", 1)
print(result.plan.node_type)  # "Index Scan"
print(result.text)  # Full text plan

# With execution timing
result = await db.explain("SELECT * FROM users", analyze=True)
print(result.execution_time)  # 0.042 ms

# With buffer stats
result = await db.explain(
    "SELECT * FROM posts ORDER BY hot_score DESC LIMIT 30", analyze=True, buffers=True
)
print(result.execution_time)  # 0.07 ms
```

### ExplainResult API

```python
result = await db.explain(sql, *args, analyze=True)

result.text  # Full text plan output
result.plan  # Root ExplainNode (structured tree)
result.execution_time  # Execution time in ms (analyze only)
result.planning_time  # Planning time in ms (analyze only)
result.analyzed  # Whether ANALYZE was used

# Convenience properties
result.has_seq_scan  # True if any sequential scan on tables
result.seq_scan_tables  # ["users", "posts"] — tables using seq scan
result.index_scans  # [ExplainNode(...)] — all index scan nodes
result.all_nodes  # Flat list of all plan tree nodes
```

### ExplainNode Properties

```python
node = result.plan
node.node_type  # "Index Scan", "Seq Scan", "Limit", etc.
node.relation  # Table name ("users")
node.index_name  # Index used ("idx_users_email")
node.actual_rows  # Rows returned (analyze only)
node.actual_total_time  # Time in ms (analyze only)
node.shared_hit_blocks  # Buffer cache hits
node.shared_read_blocks  # Disk reads
node.children  # Child nodes in plan tree
node.is_seq_scan  # True if Seq Scan
node.is_index_scan  # True if Index Scan / Index Only Scan
```

### Performance Testing

```python
# Assert index usage in tests
result = await db.explain(
    "SELECT * FROM posts WHERE is_deleted = false ORDER BY hot_score DESC LIMIT 30",
    analyze=True,
)
assert not result.has_seq_scan, f"Seq scan on: {result.seq_scan_tables}"
assert result.execution_time < 5.0, f"Too slow: {result.execution_time}ms"
```

## Transactions

### Basic Transaction

```python
async with db.transaction():
    await db.execute("INSERT INTO orders (user_id) VALUES ($1)", user_id)
    await db.execute("UPDATE inventory SET stock = stock - 1 WHERE id = $1", item_id)
    # Auto-commits on success, rolls back on exception
```

### Nested Transactions (Savepoints)

Nested `transaction()` calls use PostgreSQL savepoints. Inner failures only roll back to the savepoint, not the outer transaction:

```python
async with db.transaction():  # BEGIN
    await db.execute("INSERT INTO users ...")
    try:
        async with db.transaction():  # SAVEPOINT sp_2
            await db.execute("INSERT INTO audit ...")
            raise ValueError("oops")
    except ValueError:
        pass  # ROLLBACK TO SAVEPOINT sp_2
    # Outer transaction continues
    await db.execute("INSERT INTO logs ...")
# COMMIT (users + logs committed, audit rolled back)
```

### Named Savepoints

```python
async with db.transaction(savepoint_name="my_save"):
    await db.execute("INSERT INTO users ...")
    # Uses SAVEPOINT my_save / RELEASE SAVEPOINT my_save
```

### atomic() Alias

`db.atomic()` is a Django-compatible alias for `db.transaction()`:

```python
async with db.atomic():
    await db.execute("INSERT INTO orders ...")
```

### Transaction Nesting Depth

Transaction depth is tracked per-thread via `threading.local()`. The outermost call issues `BEGIN`/`COMMIT`, inner calls issue `SAVEPOINT`/`RELEASE SAVEPOINT`. This is fully safe under free-threaded Python 3.14t.

## Connection Pooling

pg.zig manages a connection pool internally with the following behavior:

- **Pool sizing**: configurable via `min_size` and `max_size` (default 2-10)
- **Thread-owned connections**: each worker thread acquires its own connection, eliminating contention on the hot path
- **Automatic health checks**: connections are validated before use
- **Graceful shutdown**: `disconnect()` drains all active connections

### Pool Statistics

```python
stats = db.pool_stats()
# Returns dict[str, int]:
# {
#     "total": 10,        # Total connections in pool
#     "idle": 8,           # Available connections
#     "busy": 2,           # In-use connections
#     "stmt_cache_size": 42  # Cached prepared statements
# }
```

### Recommended Pool Sizing

| Deployment                | `max_size` | Rationale                                    |
| ------------------------- | ---------- | -------------------------------------------- |
| Development               | `5`        | Single developer, low concurrency            |
| Small app (2-4 workers)   | `10`       | Default, good for most apps                  |
| Medium app (8-16 workers) | `20-30`    | `max_size` per worker, shared across threads |
| Large app (32+ workers)   | `50-100`   | Watch `max_connections` in postgresql.conf   |

PostgreSQL's default `max_connections` is 100. Across all application instances, your total `max_size` should stay well below this limit.

## Prepared Statement Caching

pg.zig automatically caches prepared statements. When you execute the same SQL string repeatedly, the Parse phase is skipped on subsequent calls:

```python
# First call: Parse + Bind + Execute
user = await db.query_one("SELECT * FROM users WHERE id = $1", 1)

# Second call: Bind + Execute only (Parse skipped)
user = await db.query_one("SELECT * FROM users WHERE id = $1", 2)
```

**Performance impact**: 33% faster on repeated queries. First-query warmup is 7.7x faster (494us to 65us) when the statement is already cached.

The cache is thread-safe, protected by mutexes in the Zig layer. Cache size is reported in `pool_stats()["stmt_cache_size"]`.

## Connection Pipelining

Execute multiple independent queries in a single network round-trip:

```python
async def pipeline(self, queries: list[str]) -> list[list[tuple]]
```

```python
results = await db.pipeline(
    [
        "SELECT COUNT(*) FROM users",
        "SELECT COUNT(*) FROM orders",
        "SELECT COUNT(*) FROM products",
    ]
)
# [[( 1542,)], [(8923,)], [(456,)]]
```

**Performance**: 20 queries in 0.24ms vs 1.40ms sequential (5.74x faster). Pipeline sends all queries in one TCP write and reads all responses in one TCP read.

**Note**: Pipeline returns raw tuples, not dicts. Each result is a `list[tuple]` matching the query's columns.

## COPY Protocol

PostgreSQL's COPY protocol for bulk data loading. 42.8x faster than row-by-row INSERT:

```python
# Bulk import from CSV data
csv_data = "Alice,30\nBob,25\nCharlie,35\n"
await db.execute("COPY users (name, age) FROM STDIN WITH (FORMAT csv)", csv_data)
```

**Benchmark**: 536K rows/sec via COPY vs 12K rows/sec via INSERT. Use COPY for any bulk import of 1,000+ rows.

## Supported Types

30+ PostgreSQL types with native binary decoding in the Zig layer. No Python-side type conversion overhead.

| Category    | PostgreSQL Types                  | Python Type              |
| ----------- | --------------------------------- | ------------------------ |
| Integer     | `int2` (smallint)                 | `int`                    |
|             | `int4` (integer)                  | `int`                    |
|             | `int8` (bigint)                   | `int`                    |
| Float       | `float4` (real)                   | `float`                  |
|             | `float8` (double)                 | `float`                  |
| Text        | `text`, `varchar`, `char`, `name` | `str`                    |
| Boolean     | `bool`                            | `bool`                   |
| Date/Time   | `timestamp`                       | `datetime` (naive)       |
|             | `timestamptz`                     | `datetime` (aware, UTC)  |
|             | `date`                            | `date`                   |
|             | `time`                            | `time`                   |
|             | `timetz`                          | `time` (with tzinfo)     |
|             | `interval`                        | `timedelta`              |
| Binary      | `bytea`                           | `bytes`                  |
| JSON        | `json`, `jsonb`                   | `dict` / `list` / scalar |
| UUID        | `uuid`                            | `UUID`                   |
| Numeric     | `numeric` / `decimal`             | `Decimal`                |
| Money       | `money`                           | `str` (formatted)        |
| Network     | `inet`                            | `str`                    |
|             | `cidr`                            | `str`                    |
| Bit         | `bit`, `varbit`                   | `str`                    |
| XML         | `xml`                             | `str`                    |
| Replication | `pg_lsn`                          | `str`                    |
| Key-Value   | `hstore`                          | `dict[str, str]`         |
| Arrays      | `int4[]`, `int8[]`                | `list[int]`              |
|             | `text[]`, `varchar[]`, `name[]`   | `list[str]`              |
|             | `bool[]`                          | `list[bool]`             |
|             | `float4[]`, `float8[]`            | `list[float]`            |
|             | `timestamp[]`, `timestamptz[]`    | `list[datetime]`         |
| Custom      | enum types                        | `str`                    |

### Custom Enum Types

PostgreSQL custom enum types are supported via dynamic OID registration:

```python
# Auto-discover all enum types in the database
await db.execute("CREATE TYPE mood AS ENUM ('happy', 'sad', 'neutral')")

# pg.zig registers the OID automatically when it encounters the type
row = await db.query_one("SELECT 'happy'::mood AS current_mood")
# {"current_mood": "happy"}  -- returned as str
```

The `discover_enums()` mechanism scans `pg_type` for all custom enum OIDs and registers them in the type decoder map. This happens automatically on first encounter.

## Connection Lifecycle

```
Database("postgres://...")      # 1. Create instance (no connection)
    |
await db.connect()              # 2. Call _db_configure() in pg.zig
    |                           #    - Parse URL, resolve host
    |                           #    - TCP connect (non-blocking + poll)
    |                           #    - PostgreSQL startup handshake
    |                           #    - Allocate connection pool
    |                           #    - Returns pool handle (int)
    |
await db.query(...)             # 3. Acquire connection from pool
    |                           #    - Check prepared statement cache
    |                           #    - Send Parse/Bind/Execute (binary protocol)
    |                           #    - Decode response tuples
    |                           #    - Return connection to pool
    |
await db.disconnect()           # 4. Call _db_close_pool()
                                #    - Drain active connections
                                #    - Free pool memory
```

### Connection Timeouts

pg.zig uses non-blocking TCP connect with poll for connection establishment. The timeout is set to 10,000ms by default (the third parameter to `_db_configure`). PostgreSQL's `statement_timeout` can be set via startup parameters for query-level timeouts.

## Error Handling

### Typed exception hierarchy — one contract for both paths

Every database failure — from the direct-SQL interface (`query`, `query_one`,
`query_val`, `execute`, `execute_many`, `pipeline`, `copy_*`, `explain`) **and**
from the ORM (`Model` / `QuerySet`) built on top of it — raises **one** typed
exception hierarchy. A given PostgreSQL error surfaces as the **identical** class
whichever path reached it, because both classify at the same native FFI boundary
(the same classifier the psycopg-compat cursor path uses). A raw `RuntimeError`
carrying PostgreSQL error text never reaches a caller.

The classes are importable from `hyperdjango.db`:

```python
from hyperdjango.db import IntegrityError, is_unique_violation
```

The full hierarchy lives in `hyperdjango.db.pgzig_connection` and subclasses
psycopg's error types (so Django's `DatabaseErrorWrapper` interop holds):

| Exception                               | Raised for                                                                                    |
| --------------------------------------- | --------------------------------------------------------------------------------------------- |
| `IntegrityError`                        | constraint violations — unique / duplicate-key, foreign-key, not-null, check                  |
| `DuplicateTable`                        | `CREATE TABLE` / relation `... already exists`                                                |
| `DuplicateDatabase`                     | `CREATE DATABASE ... already exists`                                                          |
| `ProgrammingError`                      | syntax error; undefined table/column/function (`... does not exist`)                          |
| `InvalidParameterValue` (a `DataError`) | bad parameter/value, or a nonexistent role                                                    |
| `OperationalError`                      | connection loss, permission denied, pool/resource exhaustion, "being accessed by other users" |
| `DatabaseError`                         | base / catch-all for anything unmatched                                                       |

`IntegrityError` is the base class for `DuplicateTable` / `DuplicateDatabase`;
`DataError` is the base for `InvalidParameterValue`; `DatabaseError` is the root
of the whole tree — so a broad `except DatabaseError:` catches every classified
server error while narrower classes catch a specific cause.

```python
from hyperdjango.db import IntegrityError

try:
    await User.objects.create(email="alice@example.com")
except IntegrityError:
    # Any constraint violation — unique, FK, not-null, or check.
    ...
```

#### Narrowing a unique violation — `is_unique_violation`

Because a single `IntegrityError` covers unique, foreign-key, not-null, and
check violations, `isinstance(exc, IntegrityError)` does not single out a
duplicate key. `is_unique_violation(exc)` narrows to exactly the
unique/duplicate-key case (the distinction `get_or_create` /
`update_or_create` rely on to turn a lost insert race into a clean re-read):

```python
from hyperdjango.db import IntegrityError, is_unique_violation

try:
    await Account.objects.create(email=email)
except IntegrityError as exc:
    if is_unique_violation(exc):
        account = await Account.objects.filter(email=email).get()  # already exists
    else:
        raise  # FK/not-null/check — real error
```

#### `RuntimeError` is reserved for framework preconditions

A failure that is **not** a PostgreSQL result — calling a query method before
`connect()`, or the native extension not being built — is a framework
precondition and keeps raising a plain `RuntimeError`. It has no place in the
Postgres error hierarchy, so `except DatabaseError:` never masks a "not
connected" bug.

### Connection Errors

```python
from hyperdjango.database import Database

db = Database("postgres://localhost/nonexistent_db")
try:
    await db.connect()
except RuntimeError as e:
    # Native extension not available
    print(e)
    # "Native extension not available. Build it:
    #   uv run hyper-build"

# Native-extension-not-built is a framework precondition -> RuntimeError.
# Connection loss during a query instead surfaces as OperationalError.
```

### Query Errors

Query failures raise the [typed hierarchy](#typed-exception-hierarchy-one-contract-for-both-paths)
above — catch the specific class rather than a bare `Exception`:

```python
from hyperdjango.db import IntegrityError, is_unique_violation
from hyperdjango.db.pgzig_connection import ProgrammingError, DatabaseError

# SQL syntax / undefined object -> ProgrammingError
try:
    await db.query("SELEC * FROM users")  # typo
except ProgrammingError as e:
    print(e)  # syntax error at or near "SELEC"

# Constraint violation -> IntegrityError (narrow with is_unique_violation)
try:
    await db.execute("INSERT INTO users (email) VALUES ($1)", "duplicate@example.com")
except IntegrityError as e:
    if is_unique_violation(e):
        print("email already exists")

# Any other server error falls to the DatabaseError catch-all
try:
    await db.query("SELECT * FROM users WHERE id = $1", "not_an_int")
except DatabaseError as e:
    print(e)  # invalid input syntax for type integer
```

### Pool Exhaustion

If all connections are busy, `query`/`execute` blocks until a connection is available. Design your `max_size` to accommodate peak concurrency.

```python
# Check pool state before heavy operations
stats = db.pool_stats()
if stats["idle"] == 0:
    logger.warning("All connections busy, queries may queue")
```

### Disconnected State

All query methods check pool state and raise immediately if not connected:

```python
db = Database("postgres://localhost/mydb")
# Forgot to call connect()
await db.query("SELECT 1")
# RuntimeError: Database not connected. Call await db.connect() first.
```

## DataLoader -- N+1 Prevention

The `DataLoader` batches and deduplicates async database lookups within a single event loop tick:

```python
from hyperdjango.dataloader import DataLoader


async def batch_load_users(keys: list[int]) -> list[dict | None]:
    """Load users by ID in a single query."""
    rows = await db.query("SELECT * FROM users WHERE id = ANY($1)", list(keys))
    by_id = {r["id"]: r for r in rows}
    return [by_id.get(k) for k in keys]


loader = DataLoader(batch_fn=batch_load_users, max_batch_size=100)

# These three calls are batched into ONE query:
user1, user2, user3 = await asyncio.gather(
    loader.load(1),
    loader.load(2),
    loader.load(3),
)
```

### DataLoader API

```python
@dataclass
class DataLoader:
    batch_fn: Callable[[list[K]], Awaitable[list[V]]]
    max_batch_size: int = 100
    cache_enabled: bool = True
```

| Method      | Signature                                             | Description                                              |
| ----------- | ----------------------------------------------------- | -------------------------------------------------------- |
| `load`      | `async def load(self, key: K) -> V`                   | Load single value, batched with other calls in same tick |
| `load_many` | `async def load_many(self, keys: list[K]) -> list[V]` | Load multiple values, all batched together               |
| `prime`     | `def prime(self, key: K, value: V) -> None`           | Pre-populate cache with a known value                    |
| `clear`     | `def clear(self, key: K = None) -> None`              | Clear cache (single key or all)                          |

### DataLoader with Pipeline

For maximum performance, combine DataLoader with connection pipelining:

```python
async def batch_load_with_pipeline(keys: list[int]) -> list[dict | None]:
    queries = [f"SELECT * FROM users WHERE id = {k}" for k in keys]
    results = await db.pipeline(queries)
    return [r[0] if r else None for r in results]


loader = DataLoader(batch_fn=batch_load_with_pipeline)
```

## Multi-Database

### ConnectionManager

Manage multiple named database connections with automatic routing:

```python
from hyperdjango.multi_db import ConnectionManager, PrimaryReplicaRouter

connections = ConnectionManager()
await connections.configure(
    {
        "default": "postgres://primary/myapp",
        "replica": "postgres://replica/myapp",
        "analytics": {
            "url": "postgres://analytics-host/warehouse",
            "min_size": 2,
            "max_size": 20,
        },
    }
)
```

### Database Routing

Route reads to replicas and writes to the primary:

```python
from hyperdjango.multi_db import DatabaseRouter, PrimaryReplicaRouter

# Built-in router: reads to "replica", writes to "default"
connections.router = PrimaryReplicaRouter()


# Custom router
class MyRouter(DatabaseRouter):
    def db_for_read(self, model):
        if model.__name__ == "AnalyticsEvent":
            return "analytics"
        return "replica"

    def db_for_write(self, model):
        return "default"


connections.router = MyRouter()
```

### Explicit Database Selection

Override routing for specific queries:

```python
# Reads go to replica by default
users = await User.objects.all()  # replica

# Writes go to primary by default
await User.objects.create(name="Alice")  # primary

# Explicit selection
users = await User.objects.using("replica").all()  # forced replica
events = await Event.objects.using("analytics").all()  # analytics DB
```

### Per-Model Database Binding

Bind a model to a specific database in its `Meta` class:

```python
class AnalyticsEvent(Model):
    class Meta:
        table = "events"
        database = "analytics"  # Always uses "analytics" connection
```

## Performance Reference

| Operation              | pg.zig       | psycopg3            | Speedup |
| ---------------------- | ------------ | ------------------- | ------- |
| SELECT by PK           | 21K ops/s    | 10K ops/s           | 2.06x   |
| SELECT range           | 4.18x faster | baseline            | 4.18x   |
| UPDATE                 | 1.52x faster | baseline            | 1.52x   |
| COPY bulk import       | 536K rows/s  | 12K rows/s (INSERT) | 42.8x   |
| SELECT 50 rows (micro) | 69us         | 25ms                | 365x    |
| Pipeline 20 queries    | 0.24ms       | 1.40ms (sequential) | 5.74x   |
| Prepared stmt warmup   | 65us         | 494us               | 7.7x    |
| Prepared stmt repeat   | 33% faster   | baseline            | 1.33x   |

All benchmarks on PostgreSQL 16, Apple M-series, single connection.
