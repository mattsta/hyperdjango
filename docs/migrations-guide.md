# Database Migrations Guide

This guide covers HyperDjango's migration system: auto-detecting schema changes,
generating migration files, applying and rolling back migrations, squashing,
reverse-engineering schemas, and writing data migrations.

For the API reference, see [migrations.md](migrations.md).

---

## Table of Contents

- [How It Works](#how-it-works)
- [Auto-Detecting Schema Changes](#auto-detecting-schema-changes)
- [Migration Operations](#migration-operations)
- [Running Migrations](#running-migrations)
- [Rollback](#rollback)
- [Migration Squashing](#migration-squashing)
- [Reverse Engineering with inspectdb](#reverse-engineering-with-inspectdb)
- [Dry-Run Mode](#dry-run-mode)
- [Data Migrations](#data-migrations)
- [Schema Snapshots](#schema-snapshots)
- [Deployment Safety](#deployment-safety)
- [Migration from Django Migrations](#migration-from-django-migrations)

---

## How It Works

HyperDjango's migration system diffs your Python model definitions against
the **live database schema** (not a migration history file). It introspects
PostgreSQL system catalogs directly via pg.zig to determine what has changed.

Key principles:

- **Live introspection**: Compares models to the actual database, not to
  stored migration state. This catches drift from manual SQL changes.
- **Mandatory reversibility**: Every migration operation generates both
  forward and reverse SQL. Rollbacks are always possible.
- **Post-apply verification**: After applying a migration, the system
  introspects the database again to confirm the DDL succeeded.
- **Deployment safety analysis**: Flags operations that acquire table locks
  and suggests `CONCURRENTLY` alternatives.

---

## Auto-Detecting Schema Changes

Generate a migration by comparing your models to the live database:

```bash
uv run hyper makemigrations
```

The system detects:

- New models (tables that need to be created)
- Removed models (tables that exist in DB but not in code)
- New fields (columns to add)
- Removed fields (columns to drop)
- Field type changes (e.g., `int` to `bigint`, `varchar(100)` to `varchar(200)`)
- Nullable changes (`NOT NULL` added or removed)
- Default value changes
- New indexes and unique constraints
- Foreign key additions and removals
- Many-to-many junction table changes

Example output:

```
Detected changes:
  products:
    + Add column "description" TEXT DEFAULT ''
    + Add column "sku" VARCHAR(20) NOT NULL
    ~ Alter column "price" from NUMERIC to NUMERIC(10,2)
    + Add index "idx_products_sku" ON products(sku) UNIQUE

Generated: migrations/0003_add_product_fields.py
```

### Model-to-SQL Type Mapping

| Python Type        | PostgreSQL Type    |
| ------------------ | ------------------ |
| `int`              | `INTEGER`          |
| `str`              | `TEXT`             |
| `str` (max_length) | `VARCHAR(n)`       |
| `float`            | `DOUBLE PRECISION` |
| `Decimal`          | `NUMERIC(p, s)`    |
| `bool`             | `BOOLEAN`          |
| `date`             | `DATE`             |
| `datetime`         | `TIMESTAMPTZ`      |
| `time`             | `TIME`             |
| `bytes`            | `BYTEA`            |
| `uuid.UUID`        | `UUID`             |
| `dict`/`list`      | `JSONB`            |

---

## Migration Operations

Each generated migration file contains a sequence of operations:

### CreateModel

```python
from hyperdjango.migrations import Migration, CreateModel, ModelColumn


class Migration0003(Migration):
    dependencies = ["0002_add_users"]

    operations = [
        CreateModel(
            table="products",
            columns={
                "id": ModelColumn(
                    name="id",
                    type_sql="SERIAL",
                    nullable=False,
                    is_pk=True,
                    is_auto=True,
                    is_unique=False,
                    has_index=False,
                    default_sql=None,
                    foreign_key=None,
                ),
                "name": ModelColumn(
                    name="name",
                    type_sql="VARCHAR(200)",
                    nullable=False,
                    is_pk=False,
                    is_auto=False,
                    is_unique=False,
                    has_index=False,
                    default_sql=None,
                    foreign_key=None,
                ),
                "price": ModelColumn(
                    name="price",
                    type_sql="NUMERIC(10,2)",
                    nullable=False,
                    is_pk=False,
                    is_auto=False,
                    is_unique=False,
                    has_index=False,
                    default_sql="0",
                    foreign_key=None,
                ),
            },
        ),
    ]
```

### AddField

```python
from hyperdjango.migrations import AddField

AddField(
    table="products",
    column=ModelColumn(
        name="description",
        type_sql="TEXT",
        nullable=False,
        is_pk=False,
        is_auto=False,
        is_unique=False,
        has_index=False,
        default_sql="''",
        foreign_key=None,
    ),
)
```

### AlterField

```python
from hyperdjango.migrations import AlterField

AlterField(
    table="products",
    column_name="price",
    old_type="NUMERIC",
    new_type="NUMERIC(10,2)",
)
```

### RemoveField

```python
from hyperdjango.migrations import RemoveField

RemoveField(table="products", column_name="legacy_sku")
```

### AddIndex

```python
from hyperdjango.migrations import AddIndex

AddIndex(
    table="products",
    index_name="idx_products_name",
    columns=["name"],
    unique=False,
)
```

### AddForeignKey

```python
from hyperdjango.migrations import AddForeignKey

AddForeignKey(
    table="order_items",
    column="product_id",
    target_table="products",
    target_column="id",
)
```

### DeleteModel

```python
from hyperdjango.migrations import DeleteModel

DeleteModel(table="legacy_products")
```

---

## Running Migrations

Apply all pending migrations:

```bash
uv run hyper migrate
```

Output:

```
Applying migrations:
  0001_initial ............ OK (12ms)
  0002_add_users .......... OK (8ms)
  0003_add_product_fields . OK (15ms)

Verifying schema... OK
All migrations applied successfully.
```

### Show Migration Status

```bash
uv run hyper showmigrations
```

Output:

```
[X] 0001_initial
[X] 0002_add_users
[ ] 0003_add_product_fields    (pending)
```

### Verify Schema

Check that models match the live database without applying any changes:

```bash
uv run hyper db verify
```

If there is drift (someone ran manual SQL), you will see the differences:

```
Schema drift detected:
  products:
    Column "temp_flag" exists in DB but not in models
    Column "price" type mismatch: DB=NUMERIC, Model=NUMERIC(10,2)
```

---

## Rollback

Every operation has a reverse. Roll back the most recent migration:

```bash
uv run hyper migrate --rollback
```

Roll back to a specific migration:

```bash
uv run hyper migrate --target 0002_add_users
```

This rolls back `0003_add_product_fields` (and any later migrations) in
reverse order.

### How Reversibility Works

Each operation stores both forward and reverse SQL:

| Operation     | Forward SQL                        | Reverse SQL                                   |
| ------------- | ---------------------------------- | --------------------------------------------- |
| `CreateModel` | `CREATE TABLE ...`                 | `DROP TABLE ...`                              |
| `AddField`    | `ALTER TABLE ... ADD COLUMN ...`   | `ALTER TABLE ... DROP COLUMN ...`             |
| `AlterField`  | `ALTER TABLE ... ALTER COLUMN ...` | `ALTER TABLE ... ALTER COLUMN ...` (old type) |
| `RemoveField` | `ALTER TABLE ... DROP COLUMN ...`  | `ALTER TABLE ... ADD COLUMN ...`              |
| `AddIndex`    | `CREATE INDEX ...`                 | `DROP INDEX ...`                              |
| `DeleteModel` | `DROP TABLE ...`                   | `CREATE TABLE ...`                            |

---

## Migration Squashing

Over time, many small migrations accumulate. Squash them into a single
migration that produces the same final schema:

```bash
uv run hyper migrate --squash
```

This:

1. Takes a snapshot of the current schema
2. Replaces all existing migrations with a single migration that creates
   the schema from scratch
3. Preserves data migration steps (`RunPython`) that cannot be squashed

The squashed migration is a fresh `CreateModel` for each table, which is
much faster to apply on a new database than replaying dozens of `AddField`
and `AlterField` operations.

---

## Reverse Engineering with inspectdb

Generate HyperDjango model definitions from an existing database:

```bash
uv run hyper inspectdb
```

Output:

```python
from hyperdjango import Model, Field
from decimal import Decimal
from datetime import datetime


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    price: Decimal = Field()
    stock: int = Field(default=0)
    is_active: bool = Field(default=True)
    created_at: datetime = Field()
```

### Inspecting Specific Tables

```bash
uv run hyper inspectdb --table products --table orders
```

The generated code includes:

- Correct Python type annotations from PostgreSQL types
- `primary_key=True` for PK columns
- `auto=True` for `SERIAL`/`BIGSERIAL` columns
- `max_length` for `VARCHAR(n)` columns
- `default` values where detected
- `foreign_key` references
- `nullable=True` where `NOT NULL` is absent
- Unique constraints noted in comments

---

## Dry-Run Mode

Preview the exact SQL that would be executed without applying anything:

```bash
uv run hyper migrate --dry-run
```

Output:

```sql
-- Migration: 0003_add_product_fields

ALTER TABLE products ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN sku VARCHAR(20) NOT NULL;
ALTER TABLE products ALTER COLUMN price TYPE NUMERIC(10,2);
CREATE UNIQUE INDEX idx_products_sku ON products (sku);

-- 4 operations, estimated lock time: <1s
```

This is useful for:

- Reviewing changes before applying in production
- Generating SQL scripts for DBA review
- CI/CD pipelines that need to validate migrations without a database

---

## Data Migrations

Sometimes you need to transform data alongside schema changes. Use
`RunPython` for custom Python code in migrations:

```python
from hyperdjango.migrations import Migration, RunPython, AddField, ModelColumn


async def populate_slugs(db):
    """Generate slugs from existing product names."""
    rows = await db.query("SELECT id, name FROM products WHERE slug IS NULL")
    for row in rows:
        slug = row["name"].lower().replace(" ", "-").replace("'", "")
        await db.execute(
            "UPDATE products SET slug = $1 WHERE id = $2",
            slug,
            row["id"],
        )


async def clear_slugs(db):
    """Reverse: set all slugs back to NULL."""
    await db.execute("UPDATE products SET slug = NULL")


class Migration0004(Migration):
    dependencies = ["0003_add_product_fields"]

    operations = [
        # 1. Add the column as nullable first
        AddField(
            table="products",
            column=ModelColumn(
                name="slug",
                type_sql="VARCHAR(200)",
                nullable=True,
                is_pk=False,
                is_auto=False,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key=None,
            ),
        ),
        # 2. Populate the data
        RunPython(forward=populate_slugs, reverse=clear_slugs),
        # 3. Now make it NOT NULL + UNIQUE
        AlterField(
            table="products",
            column_name="slug",
            old_type="VARCHAR(200)",
            new_type="VARCHAR(200) NOT NULL",
        ),
        AddIndex(
            table="products",
            index_name="idx_products_slug",
            columns=["slug"],
            unique=True,
        ),
    ]
```

The `RunPython` operation receives a `Database` instance for executing
queries. Both `forward` and `reverse` functions must be provided for
reversibility.

---

## Schema Snapshots

Save periodic snapshots of the database schema for fast state reconstruction
and drift detection:

```bash
# Save current schema as a checkpoint
uv run hyper db snapshot

# Detect drift from the last snapshot
uv run hyper db drift
```

Snapshots are stored as JSON files and include:

- All tables, columns, types, constraints, and indexes
- A checksum for quick comparison
- The migration ID at the time of the snapshot

Snapshots are useful for:

- Fast schema reconstruction without replaying all migrations
- Detecting unauthorized schema changes in production
- Comparing schemas across environments (staging vs production)

---

## Deployment Safety

The migration system analyzes operations for production safety:

- **Table locks**: `ALTER TABLE` operations that acquire `ACCESS EXCLUSIVE`
  locks are flagged with estimated lock duration
- **CONCURRENTLY hints**: Where possible, the system suggests using
  `CREATE INDEX CONCURRENTLY` instead of `CREATE INDEX`
- **Large table warnings**: Operations on tables with many rows are flagged
  with estimated execution time
- **NOT NULL additions**: Adding `NOT NULL` to an existing column requires
  a default value; the system validates this at generation time

These warnings appear in both `--dry-run` output and during `hyper migrate`.

---

## Migration from Django Migrations

| Django                              | HyperDjango                       |
| ----------------------------------- | --------------------------------- |
| `python manage.py makemigrations`   | `uv run hyper makemigrations`     |
| `python manage.py migrate`          | `uv run hyper migrate`            |
| `python manage.py showmigrations`   | `uv run hyper showmigrations`     |
| `python manage.py sqlmigrate`       | `uv run hyper migrate --dry-run`  |
| `python manage.py inspectdb`        | `uv run hyper inspectdb`          |
| `python manage.py squashmigrations` | `uv run hyper migrate --squash`   |
| Migration dependency graph          | Linear dependency chain           |
| `RunPython(forward, reverse)`       | Same pattern (async functions)    |
| `migrations.CreateModel`            | `CreateModel` (same concept)      |
| `migrations.AddField`               | `AddField` (same concept)         |
| Stored migration state              | Live DB introspection + snapshots |

The key architectural difference is that HyperDjango diffs against the live
database rather than maintaining a separate migration state table. This means
migrations always reflect reality, and manual schema changes are detected
automatically.

Both forward and reverse functions in `RunPython` are `async` since all
database operations use pg.zig.
