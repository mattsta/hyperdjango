# Fixtures

Database seeding with JSON fixture files. Serialize models to JSON with `dumpdata`
and import them back with `loaddata`. Replaces Django's `dumpdata`/`loaddata`
management commands.

---

## Overview

The fixtures module provides async functions for exporting model instances to JSON
fixture files and importing them back. Supports foreign key dependency ordering,
natural keys, upsert semantics (INSERT ... ON CONFLICT), and path traversal
protection.

```python
from hyperdjango.fixtures import dumpdata, loaddata, dumpdata_natural
```

---

## dumpdata

Serialize all instances of one or more model classes to a JSON string.

```python
json_str = await dumpdata([User, Article])
```

### Parameters

| Parameter       | Type          | Default | Description                |
| --------------- | ------------- | ------- | -------------------------- |
| `model_classes` | `list[type]`  | --      | Model classes to dump      |
| `output_path`   | `str \| None` | `None`  | File path to write JSON to |
| `indent`        | `int`         | `2`     | JSON indentation level     |

### Write to file

```python
await dumpdata([User, Article], output_path="fixtures/seed.json")
```

### FK dependency ordering

Models are automatically sorted so that FK targets come before dependents.
For example, if `Article` has a FK to `User`, users are dumped first.
Uses Kahn's algorithm (topological sort). Cycles are handled by appending
remaining models in their original order.

### Path traversal protection

Paths containing `..` are rejected with a `ValueError`:

```python
await dumpdata([User], output_path="../../etc/passwd")
# ValueError: Path traversal not allowed: ../../etc/passwd
```

---

## loaddata

Import fixture data from a JSON string, file path, or list of dicts.

```python
# From file
result = await loaddata("fixtures/seed.json")

# From JSON string
result = await loaddata('[{"model": "users", "pk": 1, "fields": {"name": "Alice"}}]')

# From list of dicts
result = await loaddata(
    [
        {"model": "users", "pk": 1, "fields": {"name": "Alice"}},
        {"model": "users", "pk": 2, "fields": {"name": "Bob"}},
    ]
)
```

### Parameters

| Parameter | Type                             | Default | Description                      |
| --------- | -------------------------------- | ------- | -------------------------------- |
| `source`  | `str \| list[dict[str, object]]` | --      | JSON string, file path, or dicts |
| `db`      | database instance or None        | `None`  | Database (uses global if None)   |

### LoadResult

```python
result = await loaddata("fixtures/seed.json")
print(result.created)  # number of new records inserted
print(result.updated)  # number of existing records updated
print(result.skipped)  # number of records skipped due to errors
print(result.errors)  # list[str] of error messages
```

### Upsert Semantics (ON CONFLICT)

Records with a `pk` field use `INSERT ... ON CONFLICT DO UPDATE` for atomic
upsert operations. This avoids TOCTOU (time-of-check/time-of-use) race conditions
that would occur with separate SELECT + INSERT/UPDATE queries:

```sql
INSERT INTO "users" ("id", "name", "email")
VALUES ($1, $2, $3)
ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name", "email" = EXCLUDED."email"
```

- Records with a `pk` are upserted atomically -- existing rows are updated, new rows
  are inserted, all in a single statement.
- Records without a `pk` are inserted with auto-generated primary keys.
- Records with `natural_key` use a filter-based lookup (see Natural Keys below).

### SQL Injection Protection

All table names and column names in generated SQL are quoted with PostgreSQL
double-quote identifiers (`"column_name"`). This prevents SQL injection through
crafted field names or model table names in fixture data:

```python
# Table and column identifiers are always quoted:
# INSERT INTO "users" ("id", "name") VALUES ($1, $2)
# ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name"
```

Values are always passed as parameterized query arguments (`$1`, `$2`, ...),
never interpolated into the SQL string.

### FK dependency handling

Records are sorted by FK count (fewer FKs first). Records that fail on the first
pass are retried after all other records have been processed, allowing FK targets
to be created first.

---

## JSON Format

Each fixture record is a JSON object with three keys:

```json
[
  {
    "model": "articles",
    "pk": 1,
    "fields": {
      "title": "My Article",
      "author_id": 1,
      "published_at": "2026-03-28T12:00:00",
      "tags": ["python", "web"]
    }
  }
]
```

| Key      | Type                 | Description                  |
| -------- | -------------------- | ---------------------------- |
| `model`  | `str`                | Database table name          |
| `pk`     | `int \| str \| null` | Primary key value (optional) |
| `fields` | `dict`               | Field name to value mapping  |

### Supported value types

Serialization handles these Python types automatically:

| Python Type | JSON Representation   | Example                 |
| ----------- | --------------------- | ----------------------- |
| `datetime`  | ISO 8601 string       | `"2026-03-28T12:00:00"` |
| `date`      | ISO 8601 string       | `"2026-03-28"`          |
| `time`      | ISO 8601 string       | `"14:30:00"`            |
| `timedelta` | Total seconds (float) | `3600.0`                |
| `UUID`      | String                | `"550e8400-..."`        |
| `Decimal`   | String                | `"99.99"`               |
| `bytes`     | Base64 string         | `"SGVsbG8="`            |
| `str`       | String                | `"hello"`               |
| `int`       | Number                | `42`                    |
| `float`     | Number                | `3.14`                  |
| `bool`      | Boolean               | `true`                  |
| `list`      | Array (recursive)     | `[1, 2, 3]`             |
| `dict`      | Object (recursive)    | `{"key": "value"}`      |
| `None`      | `null`                | `null`                  |

---

## Natural Keys

Natural keys identify records by a unique field combination instead of the
database primary key. Useful for fixtures that need to be portable across
databases where PKs may differ.

### Dumping with natural keys

```python
json_str = await dumpdata_natural(
    model_class=User,
    natural_key_fields=["email"],
    indent=2,
)
```

Output:

```json
[
  {
    "model": "users",
    "natural_key": ["alice@example.com"],
    "natural_key_fields": ["email"],
    "fields": {
      "name": "Alice",
      "is_active": true
    }
  }
]
```

Natural key fields are excluded from the `fields` dict (they are in
`natural_key`).

### Loading natural key fixtures

`loaddata` detects `natural_key` and `natural_key_fields` in each record and
uses a filter-based lookup instead of PK-based upsert:

```python
result = await loaddata(
    [
        {
            "model": "users",
            "natural_key": ["alice@example.com"],
            "natural_key_fields": ["email"],
            "fields": {"name": "Alice Updated"},
        }
    ]
)
```

If a matching record exists, its fields are updated. Otherwise, a new record is
inserted.

---

## CLI Integration

The fixtures module integrates with the `hyper` CLI via management commands:

```bash
# Dump all users to stdout
hyper dumpdata users

# Dump to file
hyper dumpdata users articles --output fixtures/seed.json

# Load from file
hyper loaddata fixtures/seed.json
```

---

## Error Handling

`loaddata` collects errors per record rather than failing on the first error:

```python
result = await loaddata(
    [
        {"model": "users", "pk": 1, "fields": {"name": "Alice"}},
        {"model": "nonexistent", "pk": 1, "fields": {}},
    ]
)
print(result.created)  # 1
print(result.errors)  # ["Unknown model table: nonexistent"]
print(result.skipped)  # 1
```

Errors include:

- Missing `model` key in record
- Unknown model table name
- Unknown field name on model
- Database constraint violations (FK, unique, etc.)
- Invalid JSON format
- Path traversal attempts

---

## Django Migration Guide

| Django                       | HyperDjango                          |
| ---------------------------- | ------------------------------------ |
| `manage.py dumpdata`         | `await dumpdata([Model])`            |
| `manage.py loaddata`         | `await loaddata("file.json")`        |
| `--natural-foreign`          | `dumpdata_natural(Model, ["field"])` |
| XML/YAML fixture formats     | JSON only                            |
| Synchronous                  | Async (native pg.zig)                |
| `model` field: `"app.Model"` | `model` field: table name            |
