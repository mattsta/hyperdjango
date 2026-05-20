# Lookups & Transforms

Filter QuerySets with Django-style double-underscore lookups. HyperDjango includes 17 built-in lookups, 12 built-in transforms, and a registration API for custom extensions.

## Quick Start

```python
# Exact match (default lookup when no __ suffix)
users = await User.objects.filter(name="Alice").all()

# Comparison operators
users = await User.objects.filter(age__gte=18, age__lte=65).all()

# String matching (case-insensitive)
users = await User.objects.filter(name__icontains="ali").all()

# Membership test
users = await User.objects.filter(id__in=[1, 2, 3]).all()

# Null check
users = await User.objects.filter(bio__isnull=True).all()

# Transforms + lookups chained
events = await Event.objects.filter(created_at__year__gte=2024).all()
```

## How Lookup Resolution Works

When you write `filter(name__icontains="ali")`, the lookup system:

1. Splits the key on `__` into parts: `["name", "icontains"]`
2. Walks the parts left to right, classifying each as a column name, FK path segment, transform, or lookup
3. The last part is checked against the lookup registry -- if it matches, it becomes the lookup
4. Middle parts are checked against the transform registry -- if they match, they are applied as column-wrapping SQL functions
5. Remaining parts form the column reference (possibly spanning FK joins)

The entry point is `resolve_lookup()` from `hyperdjango.lookups`:

```python
from hyperdjango.lookups import resolve_lookup

# Returns (sql_condition, params_list)
sql, params = resolve_lookup("name__icontains", "ali")
# -> ("name ILIKE $1 ESCAPE '\\'", ["%ali%"])

sql, params = resolve_lookup("created_at__year", 2024)
# -> ("EXTRACT(YEAR FROM created_at) = $1", [2024])

sql, params = resolve_lookup("created_at__year__gte", 2020)
# -> ("EXTRACT(YEAR FROM created_at) >= $1", [2020])
```

## All 17 Built-in Lookups

### exact

Default lookup when no suffix is specified. Uses parameterized `= $N`. Handles `None` values by emitting `IS NULL`.

```python
User.objects.filter(name="Alice")
User.objects.filter(name__exact="Alice")
```

| Input          | SQL Generated                       |
| -------------- | ----------------------------------- |
| `name="Alice"` | `name = $1` with params `["Alice"]` |
| `name=None`    | `name IS NULL` with params `[]`     |

### iexact

Case-insensitive exact match. Wraps both sides in `UPPER()`.

```python
User.objects.filter(name__iexact="alice")
```

| SQL | `UPPER(name) = UPPER($1)` |

This works for all Unicode text because PostgreSQL `UPPER()` is locale-aware. Note: for ASCII-only data, `iexact` is equivalent to `LOWER()` comparison.

### contains

Case-sensitive substring match using `LIKE`. LIKE metacharacters (`%` and `_`) in the search value are escaped automatically.

```python
User.objects.filter(name__contains="li")
```

| SQL | `name LIKE $1 ESCAPE '\\'` |
| Param | `"%li%"` |

**Edge case:** Searching for literal percent signs or underscores works correctly:

```python
# Finds names containing literal "%"
User.objects.filter(name__contains="%")
# SQL: name LIKE $1 ESCAPE '\\'  with param "%\%%"
```

### icontains

Case-insensitive substring match using `ILIKE` (PostgreSQL extension).

```python
User.objects.filter(name__icontains="ALI")
```

| SQL | `name ILIKE $1 ESCAPE '\\'` |
| Param | `"%ALI%"` |

### startswith

Case-sensitive prefix match.

```python
User.objects.filter(name__startswith="Al")
```

| SQL | `name LIKE $1 ESCAPE '\\'` |
| Param | `"Al%"` |

### istartswith

Case-insensitive prefix match.

```python
User.objects.filter(name__istartswith="al")
```

| SQL | `name ILIKE $1 ESCAPE '\\'` |
| Param | `"al%"` |

### endswith

Case-sensitive suffix match.

```python
User.objects.filter(email__endswith="@example.com")
```

| SQL | `email LIKE $1 ESCAPE '\\'` |
| Param | `"%@example.com"` |

### iendswith

Case-insensitive suffix match.

```python
User.objects.filter(email__iendswith="@EXAMPLE.COM")
```

| SQL | `email ILIKE $1 ESCAPE '\\'` |
| Param | `"%@EXAMPLE.COM"` |

### gt

Greater than comparison.

```python
User.objects.filter(age__gt=18)
```

| SQL | `age > $1` |

Works with any comparable type: integers, floats, strings, dates, timestamps.

### gte

Greater than or equal.

```python
User.objects.filter(age__gte=18)
```

| SQL | `age >= $1` |

### lt

Less than.

```python
User.objects.filter(age__lt=65)
```

| SQL | `age < $1` |

### lte

Less than or equal.

```python
User.objects.filter(age__lte=65)
```

| SQL | `age <= $1` |

### in

Membership test. Accepts a list, tuple, set, or frozenset. Uses PostgreSQL `= ANY($N)` which accepts an array parameter -- pg.zig converts Python lists to PostgreSQL array literals natively.

```python
User.objects.filter(id__in=[1, 2, 3])
User.objects.filter(status__in={"active", "pending"})
```

| SQL | `id = ANY($1)` |
| Param | `[[1, 2, 3]]` (single array parameter) |

**Edge cases:**

```python
# Empty list: always returns no results
User.objects.filter(id__in=[])
# SQL: FALSE (short-circuits, no query parameter)

# Type error if not iterable
User.objects.filter(id__in=42)
# Raises TypeError: "__in lookup requires a list/tuple/set, got int"
```

### range

Inclusive range test using `BETWEEN`. Requires a 2-element tuple or list.

```python
User.objects.filter(age__range=(18, 65))
```

| SQL | `age BETWEEN $1 AND $2` |
| Params | `[18, 65]` |

**Note:** `BETWEEN` is inclusive on both ends. `range=(18, 65)` matches 18, 19, ..., 64, 65.

```python
# Date ranges
Event.objects.filter(date__range=("2024-01-01", "2024-12-31"))

# TypeError if wrong shape
User.objects.filter(age__range=(18,))
# Raises TypeError: "__range lookup requires a 2-element tuple/list"
```

### isnull

Null check. Pass `True` for `IS NULL`, `False` for `IS NOT NULL`.

```python
User.objects.filter(bio__isnull=True)  # WHERE bio IS NULL
User.objects.filter(bio__isnull=False)  # WHERE bio IS NOT NULL
```

No query parameters are used -- the SQL is generated directly.

### regex

PostgreSQL regular expression match (case-sensitive). Uses the `~` operator.

```python
User.objects.filter(name__regex=r"^[A-Z][a-z]+$")
```

| SQL | `name ~ $1` |

PostgreSQL regex syntax supports POSIX extended regular expressions. Common patterns:

```python
# Names starting with uppercase
User.objects.filter(name__regex=r"^[A-Z]")

# Email format validation
User.objects.filter(email__regex=r"^[^@]+@[^@]+\.[^@]+$")

# Phone number pattern
User.objects.filter(phone__regex=r"^\+\d{1,3}-\d{3}-\d{4}$")
```

### iregex

Case-insensitive regex match. Uses the `~*` operator.

```python
User.objects.filter(name__iregex=r"alice|bob")
```

| SQL | `name ~* $1` |

## All 12 Built-in Transforms

Transforms modify the column reference before the lookup is applied. They wrap the column in a SQL function or expression.

### year

Extract the year from a date or timestamp column.

```python
Event.objects.filter(created_at__year=2024)
```

| SQL | `EXTRACT(YEAR FROM created_at) = $1` |

### month

Extract the month (1-12).

```python
Event.objects.filter(created_at__month=3)
```

| SQL | `EXTRACT(MONTH FROM created_at) = $1` |

### day

Extract the day of month (1-31).

```python
Event.objects.filter(created_at__day=15)
```

| SQL | `EXTRACT(DAY FROM created_at) = $1` |

### hour

Extract the hour (0-23) from a timestamp.

```python
Event.objects.filter(created_at__hour=14)
```

| SQL | `EXTRACT(HOUR FROM created_at) = $1` |

### minute

Extract the minute (0-59).

```python
Event.objects.filter(created_at__minute=30)
```

| SQL | `EXTRACT(MINUTE FROM created_at) = $1` |

### second

Extract the second (0-59).

```python
Event.objects.filter(created_at__second=0)
```

| SQL | `EXTRACT(SECOND FROM created_at) = $1` |

### week_day

Extract the day of week (0=Sunday, 1=Monday, ..., 6=Saturday) using PostgreSQL `DOW`.

```python
Event.objects.filter(created_at__week_day=1)  # Monday
```

| SQL | `EXTRACT(DOW FROM created_at) = $1` |

### date

Cast a timestamp to a date (strips the time component).

```python
Event.objects.filter(created_at__date="2024-03-15")
```

| SQL | `created_at::date = $1` |

### lower

Lowercase a text column.

```python
User.objects.filter(name__lower="alice")
```

| SQL | `LOWER(name) = $1` |

### upper

Uppercase a text column.

```python
User.objects.filter(name__upper="ALICE")
```

| SQL | `UPPER(name) = $1` |

### length

Get the character length of a text column.

```python
User.objects.filter(name__length=5)
```

| SQL | `LENGTH(name) = $1` |

### trim

Strip leading and trailing whitespace.

```python
User.objects.filter(name__trim="Alice")
```

| SQL | `TRIM(name) = $1` |

## Chaining Transforms with Lookups

Transforms and lookups can be chained together with double underscores. Transforms are applied from left to right, wrapping the column in nested SQL functions. The final segment is the lookup.

```python
# Transform (year) + Lookup (gte)
Event.objects.filter(created_at__year__gte=2020)
# SQL: EXTRACT(YEAR FROM created_at) >= $1

# Transform (month) + Lookup (in)
Event.objects.filter(created_at__month__in=[1, 2, 3])
# SQL: EXTRACT(MONTH FROM created_at) = ANY($1)

# Transform (lower) + Lookup (contains)
User.objects.filter(name__lower__contains="alice")
# SQL: LOWER(name) LIKE $1 ESCAPE '\\'  with param "%alice%"

# Transform (length) + Lookup (gte)
User.objects.filter(name__length__gte=5)
# SQL: LENGTH(name) >= $1

# Multiple transforms chained
User.objects.filter(name__trim__length__gte=3)
# SQL: LENGTH(TRIM(name)) >= $1

# Transform (date) + Transform (year) — not typical but valid
Event.objects.filter(created_at__date__year=2024)
# SQL: EXTRACT(YEAR FROM created_at::date) = $1
```

## Cross-Table Lookups (FK Traversal)

Filter across foreign key relationships using double-underscore path syntax. The QuerySet generates the necessary JOINs automatically.

```python
class Author(Model):
    class Meta:
        table = "authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class Book(Model):
    class Meta:
        table = "books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    author_id: int = Field(foreign_key=Author)
    pub_date: date = Field()
```

```python
# Filter books by author name — generates a JOIN
books = await Book.objects.filter(author__name="Alice").all()
# SQL: SELECT books.* FROM books
#      JOIN authors t1 ON books.author_id = t1.id
#      WHERE t1.name = $1

# FK traversal + lookup
books = await Book.objects.filter(author__name__icontains="ali").all()
# SQL: ... WHERE t1.name ILIKE $1 ESCAPE '\\'

# FK traversal + transform + lookup
books = await Book.objects.filter(author__name__length__gte=5).all()
# SQL: ... WHERE LENGTH(t1.name) >= $1
```

The lookup resolver checks each `__`-separated part against the `join_aliases` dict. If a prefix matches a known FK path, the column is qualified with the join table alias.

## Lookup Negation with exclude()

Negate any lookup using `exclude()` instead of `filter()`. This wraps the condition in `NOT(...)`.

```python
# All users except Bob
users = await User.objects.exclude(name="Bob").all()
# SQL: WHERE NOT (name = $1)

# Users not in the given IDs
users = await User.objects.exclude(id__in=[1, 2, 3]).all()
# SQL: WHERE NOT (id = ANY($1))

# Users whose age is NOT less than 18
users = await User.objects.exclude(age__lt=18).all()
# SQL: WHERE NOT (age < $1)

# Combine filter and exclude
users = await User.objects.filter(status="active").exclude(role="admin").all()
# SQL: WHERE status = $1 AND NOT (role = $2)
```

The `resolve_exclude()` function wraps the resolved condition in `NOT (...)`:

```python
from hyperdjango.lookups import resolve_exclude

sql, params = resolve_exclude("name", "Bob")
# -> ("NOT (name = $1)", ["Bob"])
```

## Custom Lookup Registration

Register new lookups to extend the filter system with custom SQL operators.

### The Lookup Base Class

```python
from hyperdjango.lookups import Lookup


class Lookup:
    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        """Generate SQL condition.

        Args:
            col: Qualified column reference (e.g., "users.name" or "LOWER(name)")
            param_idx: Next available parameter index (1-based for PostgreSQL)
            value: The filter value from the user

        Returns:
            (sql_fragment, params_list)
        """
        raise NotImplementedError
```

### Registering a Custom Lookup

```python
from hyperdjango.lookups import Lookup, register_lookup


class NotEqualLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} != ${param_idx}", [value]


register_lookup("ne", NotEqualLookup())

# Now available in all QuerySets:
users = await User.objects.filter(status__ne="deleted").all()
# SQL: WHERE status != $1
```

### More Custom Lookup Examples

```python
# Array contains lookup (PostgreSQL @> operator)
class ArrayContainsLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} @> ${param_idx}", [value]


register_lookup("array_contains", ArrayContainsLookup())


# JSONB key exists
class HasKeyLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} ? ${param_idx}", [value]


register_lookup("has_key", HasKeyLookup())


# SIMILAR TO (SQL standard regex)
class SimilarToLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} SIMILAR TO ${param_idx}", [value]


register_lookup("similar_to", SimilarToLookup())


# Overlap (PostgreSQL && for arrays)
class OverlapLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} && ${param_idx}", [list(value)]


register_lookup("overlap", OverlapLookup())
```

## Custom Transform Registration

Register new transforms to add column-wrapping SQL functions.

### The Transform Base Class

```python
from hyperdjango.lookups import Transform


class Transform:
    def as_sql(self, col: str) -> str:
        """Transform the column reference.

        Args:
            col: The column reference to transform

        Returns:
            Transformed SQL column expression
        """
        raise NotImplementedError
```

### Registering a Custom Transform

```python
from hyperdjango.lookups import Transform, register_transform


class AbsTransform(Transform):
    def as_sql(self, col):
        return f"ABS({col})"


register_transform("abs", AbsTransform())

# Now available in QuerySets:
users = await User.objects.filter(balance__abs__gte=100).all()
# SQL: WHERE ABS(balance) >= $1
```

### More Custom Transform Examples

```python
# Ceiling function
class CeilTransform(Transform):
    def as_sql(self, col):
        return f"CEIL({col})"


register_transform("ceil", CeilTransform())


# Floor function
class FloorTransform(Transform):
    def as_sql(self, col):
        return f"FLOOR({col})"


register_transform("floor", FloorTransform())


# MD5 hash
class MD5Transform(Transform):
    def as_sql(self, col):
        return f"MD5({col})"


register_transform("md5", MD5Transform())


# Reverse a string
class ReverseTransform(Transform):
    def as_sql(self, col):
        return f"REVERSE({col})"


register_transform("reverse", ReverseTransform())


# Extract epoch from timestamp
class EpochTransform(Transform):
    def as_sql(self, col):
        return f"EXTRACT(EPOCH FROM {col})"


register_transform("epoch", EpochTransform())

# Chaining: created_at__epoch__gte=1700000000
events = await Event.objects.filter(created_at__epoch__gte=1700000000).all()
# SQL: WHERE EXTRACT(EPOCH FROM created_at) >= $1
```

## Introspection API

List all registered lookups and transforms at runtime:

```python
from hyperdjango.lookups import list_lookups, list_transforms, get_lookup, get_transform

# List all registered lookup names
names = list_lookups()
# ['contains', 'endswith', 'exact', 'gt', 'gte', 'icontains', 'iendswith',
#  'iexact', 'in', 'iregex', 'isnull', 'istartswith', 'lt', 'lte',
#  'range', 'regex', 'startswith']

# List all registered transform names
names = list_transforms()
# ['date', 'day', 'hour', 'length', 'lower', 'minute', 'month',
#  'second', 'trim', 'upper', 'week_day', 'year']

# Get a specific lookup instance
lookup = get_lookup("icontains")  # IContainsLookup instance or None

# Get a specific transform instance
transform = get_transform("year")  # ExtractTransform instance or None
```

## Lookup Quick Reference

| Lookup        | SQL                       | Example                  | Notes                   |
| ------------- | ------------------------- | ------------------------ | ----------------------- |
| `exact`       | `= $1` / `IS NULL`        | `name="Alice"`           | Default; handles None   |
| `iexact`      | `UPPER(col) = UPPER($1)`  | `name__iexact="alice"`   | Unicode-safe            |
| `contains`    | `LIKE $1 ESCAPE '\\'`     | `name__contains="li"`    | Escapes % and \_        |
| `icontains`   | `ILIKE $1 ESCAPE '\\'`    | `name__icontains="LI"`   | PostgreSQL ILIKE        |
| `startswith`  | `LIKE $1 ESCAPE '\\'`     | `name__startswith="Al"`  | Prefix match            |
| `istartswith` | `ILIKE $1 ESCAPE '\\'`    | `name__istartswith="al"` | Case-insensitive prefix |
| `endswith`    | `LIKE $1 ESCAPE '\\'`     | `name__endswith="ce"`    | Suffix match            |
| `iendswith`   | `ILIKE $1 ESCAPE '\\'`    | `name__iendswith="CE"`   | Case-insensitive suffix |
| `gt`          | `> $1`                    | `age__gt=18`             | Greater than            |
| `gte`         | `>= $1`                   | `age__gte=18`            | Greater or equal        |
| `lt`          | `< $1`                    | `age__lt=65`             | Less than               |
| `lte`         | `<= $1`                   | `age__lte=65`            | Less or equal           |
| `in`          | `= ANY($1)`               | `id__in=[1,2,3]`         | Empty list -> FALSE     |
| `range`       | `BETWEEN $1 AND $2`       | `age__range=(18,65)`     | Inclusive both ends     |
| `isnull`      | `IS NULL` / `IS NOT NULL` | `bio__isnull=True`       | No params               |
| `regex`       | `~ $1`                    | `name__regex=r"^[A-Z]"`  | POSIX regex             |
| `iregex`      | `~* $1`                   | `name__iregex=r"alice"`  | Case-insensitive regex  |

## Transform Quick Reference

| Transform  | SQL                        | Example                      | Output Type    |
| ---------- | -------------------------- | ---------------------------- | -------------- |
| `year`     | `EXTRACT(YEAR FROM col)`   | `created__year=2024`         | numeric        |
| `month`    | `EXTRACT(MONTH FROM col)`  | `created__month=3`           | numeric (1-12) |
| `day`      | `EXTRACT(DAY FROM col)`    | `created__day=15`            | numeric (1-31) |
| `hour`     | `EXTRACT(HOUR FROM col)`   | `created__hour=14`           | numeric (0-23) |
| `minute`   | `EXTRACT(MINUTE FROM col)` | `created__minute=30`         | numeric (0-59) |
| `second`   | `EXTRACT(SECOND FROM col)` | `created__second=0`          | numeric (0-59) |
| `week_day` | `EXTRACT(DOW FROM col)`    | `created__week_day=1`        | numeric (0-6)  |
| `date`     | `col::date`                | `created__date="2024-03-15"` | date           |
| `lower`    | `LOWER(col)`               | `name__lower="alice"`        | text           |
| `upper`    | `UPPER(col)`               | `name__upper="ALICE"`        | text           |
| `length`   | `LENGTH(col)`              | `name__length=5`             | integer        |
| `trim`     | `TRIM(col)`                | `name__trim="Alice"`         | text           |
