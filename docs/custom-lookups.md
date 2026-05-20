# Custom Lookups & Transforms

Extend the ORM with custom field lookups and query transforms. Lookups convert Django-style keyword arguments into parameterized SQL WHERE clauses. Transforms modify column references before a lookup is applied.

## Built-in Lookups

HyperDjango includes 17 built-in lookups:

### String Lookups

```python
# Exact match (default when no lookup specified)
filter(name__exact="Alice")  # name = 'Alice'
filter(name="Alice")  # same as above

# Case-insensitive exact match
filter(name__iexact="alice")  # UPPER(name) = UPPER('alice')

# Substring match
filter(name__contains="li")  # name LIKE '%li%' ESCAPE '\'
filter(name__icontains="li")  # name ILIKE '%li%' ESCAPE '\'

# Prefix/suffix match
filter(name__startswith="Al")  # name LIKE 'Al%' ESCAPE '\'
filter(name__istartswith="al")  # name ILIKE 'al%' ESCAPE '\'
filter(name__endswith="ce")  # name LIKE '%ce' ESCAPE '\'
filter(name__iendswith="ce")  # name ILIKE '%ce' ESCAPE '\'

# Regular expression (PostgreSQL ~ operator)
filter(name__regex=r"^A\w+")  # name ~ '^A\w+'
filter(name__iregex=r"^a\w+")  # name ~* '^a\w+'
```

All LIKE/ILIKE lookups automatically escape `%` and `_` metacharacters in the value, so you can safely filter on user input.

### Comparison Lookups

```python
filter(age__gt=18)  # age > 18
filter(age__gte=18)  # age >= 18
filter(age__lt=65)  # age < 65
filter(age__lte=65)  # age <= 65
```

### Collection Lookups

```python
# IN: uses PostgreSQL = ANY() for single-parameter binding
filter(id__in=[1, 2, 3])  # id = ANY($1)  -- $1 is {1,2,3}

# Empty list produces FALSE (no results)
filter(id__in=[])  # FALSE

# Range: BETWEEN inclusive on both ends
filter(age__range=(18, 65))  # age BETWEEN 18 AND 65
```

### Null Check

```python
filter(email__isnull=True)  # email IS NULL
filter(email__isnull=False)  # email IS NOT NULL
```

### Full Lookup Reference

| Lookup        | SQL Output                | Description                |
| ------------- | ------------------------- | -------------------------- |
| `exact`       | `= $N`                    | Exact match (default)      |
| `iexact`      | `UPPER(col) = UPPER($N)`  | Case-insensitive exact     |
| `contains`    | `LIKE $N ESCAPE '\'`      | Case-sensitive substring   |
| `icontains`   | `ILIKE $N ESCAPE '\'`     | Case-insensitive substring |
| `startswith`  | `LIKE $N ESCAPE '\'`      | Case-sensitive prefix      |
| `istartswith` | `ILIKE $N ESCAPE '\'`     | Case-insensitive prefix    |
| `endswith`    | `LIKE $N ESCAPE '\'`      | Case-sensitive suffix      |
| `iendswith`   | `ILIKE $N ESCAPE '\'`     | Case-insensitive suffix    |
| `gt`          | `> $N`                    | Greater than               |
| `gte`         | `>= $N`                   | Greater than or equal      |
| `lt`          | `< $N`                    | Less than                  |
| `lte`         | `<= $N`                   | Less than or equal         |
| `in`          | `= ANY($N)`               | Membership test            |
| `range`       | `BETWEEN $N AND $M`       | Inclusive range            |
| `isnull`      | `IS NULL` / `IS NOT NULL` | Null check                 |
| `regex`       | `~ $N`                    | PostgreSQL regex           |
| `iregex`      | `~* $N`                   | Case-insensitive regex     |

## Built-in Transforms

12 transforms for date/time decomposition and text operations:

### Date/Time Transforms

```python
filter(created_at__year=2024)  # EXTRACT(YEAR FROM created_at) = 2024
filter(created_at__month=12)  # EXTRACT(MONTH FROM created_at) = 12
filter(created_at__day=25)  # EXTRACT(DAY FROM created_at) = 25
filter(created_at__hour=14)  # EXTRACT(HOUR FROM created_at) = 14
filter(created_at__minute=30)  # EXTRACT(MINUTE FROM created_at) = 30
filter(created_at__second=0)  # EXTRACT(SECOND FROM created_at) = 0
filter(created_at__week_day=1)  # EXTRACT(DOW FROM created_at) = 1
filter(created_at__date="2024-12-25")  # created_at::date = '2024-12-25'
```

### Text Transforms

```python
filter(name__lower="alice")  # LOWER(name) = 'alice'
filter(name__upper="ALICE")  # UPPER(name) = 'ALICE'
filter(name__length=5)  # LENGTH(name) = 5
filter(name__trim="alice")  # TRIM(name) = 'alice'
```

### Full Transform Reference

| Transform  | SQL Output                 | Description                |
| ---------- | -------------------------- | -------------------------- |
| `year`     | `EXTRACT(YEAR FROM col)`   | Extract year               |
| `month`    | `EXTRACT(MONTH FROM col)`  | Extract month (1-12)       |
| `day`      | `EXTRACT(DAY FROM col)`    | Extract day (1-31)         |
| `hour`     | `EXTRACT(HOUR FROM col)`   | Extract hour (0-23)        |
| `minute`   | `EXTRACT(MINUTE FROM col)` | Extract minute (0-59)      |
| `second`   | `EXTRACT(SECOND FROM col)` | Extract second (0-59)      |
| `week_day` | `EXTRACT(DOW FROM col)`    | Day of week (0=Sun, 6=Sat) |
| `date`     | `col::date`                | Cast timestamp to date     |
| `lower`    | `LOWER(col)`               | Lowercase text             |
| `upper`    | `UPPER(col)`               | Uppercase text             |
| `length`   | `LENGTH(col)`              | String length              |
| `trim`     | `TRIM(col)`                | Strip whitespace           |

## Chaining Transforms with Lookups

Transforms can be chained with lookups. The transform modifies the column expression, then the lookup applies to the transformed result:

```python
# EXTRACT(YEAR FROM created_at) >= 2024
filter(created_at__year__gte=2024)

# LOWER(name) LIKE 'al%'
filter(name__lower__startswith="al")

# LENGTH(name) > 10
filter(name__length__gt=10)

# EXTRACT(MONTH FROM created_at) IN (6, 7, 8)
filter(created_at__month__in=[6, 7, 8])

# UPPER(TRIM(name)) = 'ALICE'
filter(name__trim__upper="ALICE")
```

The resolution order is: column parts first, then transforms left-to-right, then the final lookup.

## Registering Custom Lookups

Register a custom lookup by subclassing `Lookup` or using the functional API:

### Class-Based Registration

```python
from hyperdjango.lookups import Lookup, register_lookup


class TrigramSimilarityLookup(Lookup):
    """PostgreSQL trigram similarity: col % $N"""

    def as_sql(self, col, param_idx, value):
        return f"{col} %% ${param_idx}", [value]


register_lookup("similar", TrigramSimilarityLookup())
```

### The as_sql Method

Every lookup must implement `as_sql(col, param_idx, value)` which returns a tuple of `(sql_condition, params_list)`:

| Parameter   | Type  | Description                                                              |
| ----------- | ----- | ------------------------------------------------------------------------ |
| `col`       | `str` | The qualified column reference (e.g., `"users.name"` or `"LOWER(name)"`) |
| `param_idx` | `int` | Next available parameter index (1-based, for `$1`, `$2`, etc.)           |
| `value`     | `Any` | The filter value provided by the user                                    |

The method returns:

- `sql_condition` -- a SQL fragment like `"col > $1"` or `"col IS NULL"`
- `params_list` -- list of parameter values to bind (can be empty for `IS NULL` etc.)

### Practical Custom Lookups

**String length filter:**

```python
class LengthLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"LENGTH({col}) = ${param_idx}", [value]


register_lookup("length_eq", LengthLookup())

# Usage
await User.objects.filter(name__length_eq=5).all()
# SQL: WHERE LENGTH(name) = $1  (params: [5])
```

**Trigram similarity (PostgreSQL pg_trgm):**

```python
class TrigramLookup(Lookup):
    """Requires CREATE EXTENSION pg_trgm;"""

    def as_sql(self, col, param_idx, value):
        return f"{col} %% ${param_idx}", [value]


register_lookup("trigram", TrigramLookup())

# Usage: fuzzy name search
await User.objects.filter(name__trigram="Alic").all()
# SQL: WHERE name % $1
```

**Array contains (PostgreSQL @> operator):**

```python
class ArrayContainsLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} @> ${param_idx}", [value]


register_lookup("array_contains", ArrayContainsLookup())

# Usage
await Product.objects.filter(tags__array_contains=["sale", "featured"]).all()
# SQL: WHERE tags @> $1
```

**JSONB key exists (PostgreSQL ? operator):**

```python
class JSONBKeyExistsLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} ? ${param_idx}", [value]


register_lookup("has_key", JSONBKeyExistsLookup())

# Usage
await Product.objects.filter(metadata__has_key="color").all()
# SQL: WHERE metadata ? $1
```

**JSONB path value:**

```python
class JSONBPathLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        path, expected = value
        return f"{col} ->> ${param_idx} = ${param_idx + 1}", [path, expected]


register_lookup("json_path_eq", JSONBPathLookup())

# Usage
await Product.objects.filter(metadata__json_path_eq=("color", "red")).all()
# SQL: WHERE metadata ->> $1 = $2
```

**Case-insensitive regex (already built-in as `iregex`, but as example):**

```python
class CIRegexLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} ~* ${param_idx}", [value]


register_lookup("ci_regex", CIRegexLookup())
```

**Full-text search:**

```python
class FullTextLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return (
            f"to_tsvector('english', {col}) @@ plainto_tsquery('english', ${param_idx})",
            [value],
        )


register_lookup("search", FullTextLookup())

# Usage
await Article.objects.filter(body__search="django performance").all()
# SQL: WHERE to_tsvector('english', body) @@ plainto_tsquery('english', $1)
```

## Registering Custom Transforms

Register a custom transform by subclassing `Transform`:

```python
from hyperdjango.lookups import Transform, register_transform


class AbsTransform(Transform):
    """ABS(col) -- absolute value."""

    def as_sql(self, col):
        return f"ABS({col})"


register_transform("abs", AbsTransform())
```

### The as_sql Method

Transforms implement `as_sql(col)` which takes a column reference string and returns a transformed column expression string.

### Practical Custom Transforms

**Absolute value:**

```python
class AbsTransform(Transform):
    def as_sql(self, col):
        return f"ABS({col})"


register_transform("abs", AbsTransform())

# Usage: filter by absolute value of balance
await Account.objects.filter(balance__abs__gt=1000).all()
# SQL: WHERE ABS(balance) > $1
```

**Date truncation:**

```python
class DateTruncTransform(Transform):
    def __init__(self, precision):
        self.precision = precision

    def as_sql(self, col):
        return f"date_trunc('{self.precision}', {col})"


register_transform("trunc_month", DateTruncTransform("month"))
register_transform("trunc_week", DateTruncTransform("week"))

# Usage
await Order.objects.filter(created_at__trunc_month="2024-06-01").all()
# SQL: WHERE date_trunc('month', created_at) = $1
```

**Coalesce (default value for NULL):**

```python
class CoalesceTransform(Transform):
    def __init__(self, default):
        self.default = default

    def as_sql(self, col):
        return f"COALESCE({col}, '{self.default}')"


register_transform("coalesce_empty", CoalesceTransform(""))

# Usage: treat NULL as empty string for comparison
await User.objects.filter(bio__coalesce_empty__length__gt=0).all()
# SQL: WHERE LENGTH(COALESCE(bio, '')) > $1
```

**Reverse string:**

```python
class ReverseTransform(Transform):
    def as_sql(self, col):
        return f"REVERSE({col})"


register_transform("reverse", ReverseTransform())

# Usage: find palindromes
await Word.objects.filter(text__reverse="madam").all()
# SQL: WHERE REVERSE(text) = $1
```

## Cross-Table Lookups

Lookups work across foreign key relations. The ORM automatically generates JOIN clauses:

```python
# Filter articles by author name
await Article.objects.filter(author__name__startswith="Al").all()
# SQL: SELECT articles.* FROM articles
#      JOIN authors ON articles.author_id = authors.id
#      WHERE authors.name LIKE 'Al%' ESCAPE '\'

# Filter by related object's date field with transform
await Article.objects.filter(author__created_at__year=2024).all()
# SQL: ... JOIN authors ON ... WHERE EXTRACT(YEAR FROM authors.created_at) = $1

# Chain through multiple relations
await Comment.objects.filter(article__author__name="Alice").all()
# SQL: ... JOIN articles ON ... JOIN authors ON ... WHERE authors.name = $1
```

Custom lookups and transforms work with cross-table relations:

```python
# Custom lookup on a related field
await Article.objects.filter(author__name__trigram="Alic").all()
# SQL: ... JOIN authors ON ... WHERE authors.name % $1
```

## Vendor-Specific Lookups (PostgreSQL)

HyperDjango targets PostgreSQL exclusively, so all lookups can use PostgreSQL-specific syntax. Some useful PostgreSQL-only lookups to register:

```python
# Array overlap (@> any common elements)
class ArrayOverlapLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} && ${param_idx}", [value]


register_lookup("overlap", ArrayOverlapLookup())


# Network containment (inet/cidr)
class NetworkContainsLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} >>= ${param_idx}", [value]


register_lookup("net_contains", NetworkContainsLookup())


# JSONB containment
class JSONBContainsLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} @> ${param_idx}::jsonb", [value]


register_lookup("jsonb_contains", JSONBContainsLookup())
```

## Listing Available Lookups and Transforms

```python
from hyperdjango.lookups import list_lookups, list_transforms, get_lookup, get_transform

# List all registered lookup names
print(list_lookups())
# ['contains', 'endswith', 'exact', 'gt', 'gte', 'icontains', 'iendswith',
#  'iexact', 'in', 'iregex', 'isnull', 'istartswith', 'lt', 'lte',
#  'range', 'regex', 'startswith']

# List all registered transform names
print(list_transforms())
# ['date', 'day', 'hour', 'length', 'lower', 'minute', 'month',
#  'second', 'trim', 'upper', 'week_day', 'year']

# Get a specific lookup/transform instance
exact = get_lookup("exact")
year = get_transform("year")
```

## Testing Custom Lookups

Test custom lookups using `resolve_lookup` directly:

```python
from hyperdjango.lookups import resolve_lookup, register_lookup, Lookup


class MyLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} @@ ${param_idx}", [value]


register_lookup("custom", MyLookup())

# Test the SQL generation
sql, params = resolve_lookup("name__custom", "test_value", param_idx=1)
assert sql == "name @@ $1"
assert params == ["test_value"]

# Test with transforms
sql, params = resolve_lookup("name__lower__custom", "test_value", param_idx=1)
assert sql == "LOWER(name) @@ $1"

# Test with table alias
sql, params = resolve_lookup(
    "name__custom", "test_value", param_idx=1, table_alias="users"
)
assert sql == "users.name @@ $1"
```

For integration tests against a real database:

```python
async def test_custom_lookup():
    await User.objects.create(name="Alice", bio="Loves Python")
    await User.objects.create(name="Bob", bio="Loves Zig")

    # Test the custom lookup produces correct results
    results = await User.objects.filter(bio__search="Python").all()
    assert len(results) == 1
    assert results[0].name == "Alice"
```

## How Lookup Resolution Works

When you write `filter(name__lower__startswith="al")`, the resolver:

1. Splits the key into parts: `["name", "lower", "startswith"]`
2. Walks the parts left to right:
   - `"name"` -- not a transform or lookup, so it is a column name
   - `"lower"` -- registered transform, added to transform chain
   - `"startswith"` -- registered lookup (and is the last part), used as the final lookup
3. Builds the column reference: `"name"`
4. Applies transforms: `LOWER(name)`
5. Applies the lookup: `LOWER(name) LIKE $1 ESCAPE '\'` with params `["al%"]`

If the last part is not a registered lookup, `exact` is used by default. If a part matches a transform, it is consumed as a transform. Otherwise it is treated as part of the column path (supporting FK spanning like `author__name`).
