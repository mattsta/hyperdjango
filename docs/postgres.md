# PostgreSQL Features

HyperDjango is PostgreSQL-native. The pg.zig driver provides first-class support for PostgreSQL-specific types and features.

## Native Type Support

pg.zig handles 30+ PostgreSQL type OIDs natively — no Python-side parsing needed:

| PostgreSQL Type                        | Python Type   | Notes                      |
| -------------------------------------- | ------------- | -------------------------- |
| `int2`, `int4`, `int8`                 | `int`         | 16/32/64-bit integers      |
| `float4`, `float8`                     | `float`       | Single/double precision    |
| `bool`                                 | `bool`        | Boolean                    |
| `text`, `varchar`, `char`              | `str`         | String types               |
| `timestamp`, `timestamptz`             | `datetime`    | With/without timezone      |
| `date`                                 | `date`        | Date only                  |
| `time`, `timetz`                       | `time`        | Time with/without timezone |
| `interval`                             | `timedelta`   | Time intervals             |
| `numeric`                              | `Decimal`     | Arbitrary precision        |
| `uuid`                                 | `UUID`        | UUID type                  |
| `bytea`                                | `bytes`       | Binary data                |
| `json`, `jsonb`                        | `dict`/`list` | Auto-parsed JSON           |
| `inet`, `cidr`                         | `str`         | Network addresses          |
| `money`                                | `Decimal`     | Currency                   |
| `bit`, `varbit`                        | `str`         | Bit strings                |
| `int[]`, `text[]`, `bool[]`, `float[]` | `list`        | Array types                |

## JSONB

JSONB fields are auto-parsed by pg.zig — no manual `json.loads()`:

```python
class Config(Model):
    class Meta:
        table = "configs"

    id: int = Field(primary_key=True, auto=True)
    settings: dict[str, str] = Field(default_factory=dict)  # JSONB column


# Insert
config = Config(settings={"theme": "dark", "lang": "en"})
await config.save()

# Query JSONB fields
rows = await db.query("SELECT * FROM configs WHERE settings->>'theme' = $1", "dark")

# JSONB operators in raw SQL
await db.query(
    "SELECT * FROM configs WHERE settings ? $1",
    "theme",  # has key
)
await db.query("SELECT * FROM configs WHERE settings @> $1::jsonb", '{"theme": "dark"}')
```

## Arrays

```python
# PostgreSQL array columns
await db.execute(
    "CREATE TABLE IF NOT EXISTS articles (id SERIAL PRIMARY KEY, tags TEXT[])"
)

# Insert with array
await db.execute("INSERT INTO articles (tags) VALUES ($1)", ["python", "web", "async"])

# Query arrays
rows = await db.query("SELECT * FROM articles WHERE $1 = ANY(tags)", "python")

# Array operators
await db.query(
    "SELECT * FROM articles WHERE tags @> ARRAY[$1]",
    "python",  # contains
)
```

## Full-Text Search

PostgreSQL's built-in full-text search via raw SQL:

```python
# Create search index
await db.execute("""
    ALTER TABLE articles ADD COLUMN IF NOT EXISTS
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED
""")
await db.execute(
    "CREATE INDEX IF NOT EXISTS idx_articles_search ON articles USING gin(search_vector)"
)

# Search
results = await db.query(
    "SELECT * FROM articles WHERE search_vector @@ plainto_tsquery('english', $1) "
    "ORDER BY ts_rank(search_vector, plainto_tsquery('english', $1)) DESC",
    "python async web",
)

# Headline (highlighted results)
results = await db.query(
    "SELECT *, ts_headline('english', content, plainto_tsquery('english', $1)) as headline "
    "FROM articles WHERE search_vector @@ plainto_tsquery('english', $1)",
    "python",
)
```

## UNLOGGED Tables

HyperDjango uses UNLOGGED tables for cache, sessions, and rate limits — 2-3x faster writes:

```sql
CREATE UNLOGGED TABLE hyper_cache (
    key VARCHAR(255) PRIMARY KEY,
    value BYTEA,
    expires_at TIMESTAMPTZ
);
```

UNLOGGED tables skip WAL (Write-Ahead Log). Data survives normal operation but is lost on crash — acceptable for ephemeral data like sessions and cache.

## LISTEN/NOTIFY

Real-time PostgreSQL notifications for the Channel layer:

```python
from hyperdjango.channels import PgChannelLayer

layer = PgChannelLayer(db)

# Publish
await layer.publish("chat:room1", {"user": "Alice", "text": "Hello!"})

# Subscribe (in WebSocket handler)
async for message in layer.subscribe("chat:room1"):
    await ws.send_json(message)
```

The PgChannelLayer uses PostgreSQL's native LISTEN/NOTIFY protocol.

## Custom Enum Types

Register PostgreSQL custom enums for automatic type conversion:

```python
await db.execute("CREATE TYPE mood AS ENUM ('happy', 'sad', 'neutral')")

# Discover and register
await db.discover_enums()

# Now enum values are returned as Python strings automatically
row = await db.query_one("SELECT current_mood FROM users WHERE id = $1", 1)
# row["current_mood"] = "happy" (str, not raw bytes)
```

## COPY Protocol

Bulk import at 536K rows/sec (43x faster than INSERT):

```python
# COPY IN — bulk import
data = [
    (1, "Alice", "alice@example.com"),
    (2, "Bob", "bob@example.com"),
    # ... thousands of rows ...
]
await db.copy_in("users", ["id", "name", "email"], data)
```

## Connection Pipelining

Send multiple queries in a single round-trip:

```python
# 20 queries in 0.24ms vs 1.40ms sequential (5.74x faster)
results = await db.pipeline(
    [
        ("SELECT * FROM users WHERE id = $1", [1]),
        ("SELECT * FROM users WHERE id = $1", [2]),
        ("SELECT COUNT(*) FROM orders", []),
    ]
)
```

## Prepared Statement Caching

pg.zig automatically caches prepared statements. Repeated queries skip the Parse phase, reducing latency by ~33%.

```python
# First call: Parse + Bind + Execute
user = await db.query_one("SELECT * FROM users WHERE id = $1", 1)

# Subsequent calls: Bind + Execute only (Parse cached)
user = await db.query_one("SELECT * FROM users WHERE id = $1", 2)
```

## Statement Timeout

Prevent runaway queries:

```python
db = Database("postgres://localhost/mydb?statement_timeout=30000")
# Queries exceeding 30 seconds are automatically cancelled
```

## Performance

| Operation           | pg.zig        | psycopg3     | Speedup   |
| ------------------- | ------------- | ------------ | --------- |
| SELECT by PK        | 21K ops/sec   | 10K ops/sec  | **2.06x** |
| SELECT range        | 4.18x faster  | baseline     | **4.18x** |
| UPDATE              | 1.52x faster  | baseline     | **1.52x** |
| COPY bulk import    | 536K rows/sec | 12K rows/sec | **42.8x** |
| Micro-bench 50 rows | 69μs          | 25ms         | **365x**  |

## JSONB Lookups

PostgreSQL JSONB operators via raw SQL:

```python
# Key exists
await db.query("SELECT * FROM configs WHERE settings ? $1", "theme")

# Key-value match
await db.query("SELECT * FROM configs WHERE settings->>'theme' = $1", "dark")

# Contains (superset check)
await db.query("SELECT * FROM configs WHERE settings @> $1::jsonb", '{"theme":"dark"}')

# Contained by (subset check)
await db.query(
    "SELECT * FROM configs WHERE settings <@ $1::jsonb", '{"theme":"dark","lang":"en"}'
)

# Nested key access
await db.query("SELECT settings->'display'->>'font_size' FROM configs WHERE id = $1", 1)

# Array of keys exists (any)
await db.query("SELECT * FROM configs WHERE settings ?| ARRAY['theme', 'lang']")

# Array of keys exists (all)
await db.query("SELECT * FROM configs WHERE settings ?& ARRAY['theme', 'lang']")
```

## Array Lookups

```python
# Contains element
await db.query("SELECT * FROM articles WHERE $1 = ANY(tags)", "python")

# Array contains (superset)
await db.query("SELECT * FROM articles WHERE tags @> ARRAY[$1, $2]", "python", "web")

# Array overlap (any shared elements)
await db.query("SELECT * FROM articles WHERE tags && ARRAY[$1, $2]", "python", "rust")

# Array length
await db.query("SELECT * FROM articles WHERE array_length(tags, 1) > $1", 3)

# Unnest (expand array to rows)
await db.query("SELECT DISTINCT unnest(tags) as tag FROM articles ORDER BY tag")
```

## Range Types

```python
# Integer range
await db.execute("CREATE TABLE events (id SERIAL PRIMARY KEY, during INT4RANGE)")
await db.execute("INSERT INTO events (during) VALUES ('[1, 10)')")

# Contains value
await db.query("SELECT * FROM events WHERE during @> $1", 5)

# Date range
await db.execute("CREATE TABLE bookings (id SERIAL PRIMARY KEY, dates DATERANGE)")
await db.query("SELECT * FROM bookings WHERE dates && daterange($1, $2)", start, end)
```
