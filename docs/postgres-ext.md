# PostgreSQL Extensions

PostgreSQL-specific field types, search, aggregates, range types, indexes, and
constraints. Replaces `django.contrib.postgres`.

---

## Overview

This module provides dataclass-based representations of PostgreSQL-specific features.
Each class generates SQL fragments via `.as_sql()` for use in queries, annotations,
and DDL. ORM lookups are registered automatically on import.

```python
from hyperdjango.postgres import (
    # Fields
    ArrayField,
    HStoreField,
    JSONBField,
    # Full-text search
    SearchVector,
    SearchQuery,
    SearchRank,
    SearchHeadline,
    # Trigram similarity
    TrigramSimilarity,
    TrigramDistance,
    TrigramWordSimilarity,
    TrigramWordDistance,
    # Array operations
    ArrayContains,
    ArrayContainedBy,
    ArrayOverlap,
    ArrayLength,
    ArrayIndex,
    ArrayRemove,
    ArrayAppend,
    ArrayPrepend,
    ArrayCat,
    ArrayPosition,
    Unnest,
    # Aggregates
    ArrayAgg,
    JSONBAgg,
    StringAgg,
    BitAnd,
    BitOr,
    BoolAnd,
    BoolOr,
    # Range types
    IntegerRange,
    BigIntegerRange,
    DecimalRange,
    DateRange,
    DateTimeRange,
    # Range lookups
    RangeContains,
    RangeContainedBy,
    RangeOverlap,
    RangeFullyLessThan,
    RangeFullyGreaterThan,
    RangeAdjacentTo,
    # Constraints
    ExclusionConstraint,
    # Indexes
    GinIndex,
    GistIndex,
    BrinIndex,
    HashIndex,
    SpGistIndex,
    BTreeIndex,
)
```

---

## Field Types

### ArrayField

PostgreSQL array column (`int[]`, `text[]`, etc.).

```python
tags = ArrayField(base_type="text")
scores = ArrayField(base_type="int", default=[0, 0, 0])

tags.db_type  # "text[]"
scores.db_type  # "integer[]"
scores.create_sql  # "integer[] DEFAULT '{0,0,0}'"
```

| Parameter   | Type           | Default  | Description                 |
| ----------- | -------------- | -------- | --------------------------- |
| `base_type` | `str`          | `"text"` | Element type name           |
| `size`      | `int \| None`  | `None`   | Fixed array size (optional) |
| `default`   | `list \| None` | `None`   | Default value               |

Supported base types: `int`, `integer`, `bigint`, `smallint`, `text`, `varchar`,
`uuid`, `float`, `double precision`, `bool`, `boolean`, `date`, `timestamp`,
`timestamptz`, `numeric`, `inet`, `cidr`, `jsonb`.

### HStoreField

PostgreSQL `hstore` key-value text pairs. Requires the `hstore` extension.

```python
metadata = HStoreField()
metadata.db_type  # "hstore"
```

### JSONBField

PostgreSQL `jsonb` with GIN indexing support.

```python
data = JSONBField()
data.db_type  # "jsonb"
```

---

## Full-Text Search

### SearchVector

Build a `to_tsvector()` expression over one or more fields.

```python
sv = SearchVector(fields=["title", "body"], config="english", weight="A")
sv.as_sql()
# "setweight(to_tsvector('english', COALESCE(\"title\", '')), 'A') || setweight(to_tsvector('english', COALESCE(\"body\", '')), 'A')"
```

| Parameter | Type          | Default     | Description                  |
| --------- | ------------- | ----------- | ---------------------------- |
| `fields`  | `list[str]`   | --          | Column names to vectorize    |
| `config`  | `str`         | `"english"` | Text search configuration    |
| `weight`  | `str \| None` | `None`      | Weight category (A, B, C, D) |

### SearchQuery

Build a tsquery expression.

```python
sq = SearchQuery(query="web framework", config="english", search_type="plain")
sq.as_sql()  # "plainto_tsquery('english', ${param})"
```

| Parameter     | Type  | Default     | Description                           |
| ------------- | ----- | ----------- | ------------------------------------- |
| `query`       | `str` | --          | Search text                           |
| `config`      | `str` | `"english"` | Text search configuration             |
| `search_type` | `str` | `"plain"`   | `plain`, `phrase`, `raw`, `websearch` |

Search type mapping:

- `plain` -> `plainto_tsquery` (splits on spaces, AND)
- `phrase` -> `phraseto_tsquery` (proximity search)
- `raw` -> `to_tsquery` (raw tsquery syntax with `&`, `|`, `!`)
- `websearch` -> `websearch_to_tsquery` (Google-style: quotes, `-`, `OR`)

### SearchRank

Rank search results using `ts_rank()`.

```python
rank = SearchRank(vector=sv, query=sq, weights=[0.1, 0.2, 0.4, 1.0])
rank.as_sql()  # "ts_rank('{0.1,0.2,0.4,1.0}', ..., {query})"
```

| Parameter       | Type                  | Default | Description              |
| --------------- | --------------------- | ------- | ------------------------ |
| `vector`        | `SearchVector`        | --      | The search vector        |
| `query`         | `SearchQuery`         | --      | The search query         |
| `weights`       | `list[float] \| None` | `None`  | D, C, B, A weight values |
| `normalization` | `int`                 | `0`     | Normalization flags      |

### SearchHeadline

Generate highlighted search result snippets using `ts_headline()`.

```python
hl = SearchHeadline(
    field="body",
    query=sq,
    config="english",
    start_sel="<mark>",
    stop_sel="</mark>",
    max_words=35,
    min_words=15,
    max_fragments=3,
)
hl.as_sql()
# "ts_headline('english', \"body\", {query}, 'StartSel=<mark>, StopSel=</mark>, MaxWords=35, MinWords=15, MaxFragments=3')"
```

---

## Trigram Similarity

Requires the `pg_trgm` extension (`CREATE EXTENSION IF NOT EXISTS pg_trgm`).

### TrigramSimilarity

Score from 0.0 to 1.0 based on trigram overlap.

```python
ts = TrigramSimilarity(field="name", value="example")
ts.as_sql()  # "similarity(name, ${param})"
```

### TrigramDistance

Distance = 1 - similarity. Lower is more similar.

```python
td = TrigramDistance(field="name", value="example")
td.as_sql()  # "(name <-> ${param})"
```

### TrigramWordSimilarity

Best match similarity within longer text.

```python
tws = TrigramWordSimilarity(field="description", value="search term")
tws.as_sql()  # "word_similarity(${param}, description)"
```

### TrigramWordDistance

```python
twd = TrigramWordDistance(field="description", value="search term")
twd.as_sql()  # "(${param} <<-> description)"
```

---

## Array Operations and Lookups

### Query-Building Classes

| Class              | Operator/Function | Example SQL                      |
| ------------------ | ----------------- | -------------------------------- |
| `ArrayContains`    | `@>`              | `tags @> ${param}`               |
| `ArrayContainedBy` | `<@`              | `tags <@ ${param}`               |
| `ArrayOverlap`     | `&&`              | `tags && ${param}`               |
| `ArrayLength`      | `array_length`    | `array_length(tags, 1)`          |
| `ArrayIndex`       | `[]`              | `tags[1]` (1-indexed)            |
| `ArrayRemove`      | `array_remove`    | `array_remove(tags, ${param})`   |
| `ArrayAppend`      | `array_append`    | `array_append(tags, ${param})`   |
| `ArrayPrepend`     | `array_prepend`   | `array_prepend(${param}, tags)`  |
| `ArrayCat`         | `array_cat`       | `array_cat(tags, other_tags)`    |
| `ArrayPosition`    | `array_position`  | `array_position(tags, ${param})` |
| `Unnest`           | `unnest`          | `unnest(tags)`                   |

### Usage

```python
# Check if tags array contains specific values
ac = ArrayContains(column="tags", values=["python", "web"])
ac.as_sql()  # "tags @> ${param}"

# Get array length
al = ArrayLength(column="scores", dimension=1)
al.as_sql()  # "array_length(scores, 1)"

# Access specific index (PostgreSQL arrays are 1-based)
ai = ArrayIndex(column="scores", index=1)
ai.as_sql()  # "scores[1]"

# Append element
aa = ArrayAppend(column="tags", value="new-tag")
aa.as_sql()  # "array_append(tags, ${param})"

# Expand array to rows
u = Unnest(column="tags")
u.as_sql()  # "unnest(tags)"
```

---

## Aggregates

All aggregates support `filter_condition` for conditional aggregation (`FILTER (WHERE ...)`).

### ArrayAgg

```python
agg = ArrayAgg(
    field="name",
    distinct=True,
    ordering="name ASC",
    filter_condition="active = true",
    default=["none"],
)
agg.as_sql()
# "COALESCE(array_agg(DISTINCT name ORDER BY name ASC) FILTER (WHERE active = true), '{none}')"
```

### JSONBAgg

```python
agg = JSONBAgg(field="data", ordering="id ASC", default="[]")
agg.as_sql()  # "COALESCE(jsonb_agg(data ORDER BY id ASC), '[]'::jsonb)"
```

### StringAgg

```python
agg = StringAgg(field="name", delimiter=", ", distinct=True, default="")
agg.as_sql()  # "COALESCE(string_agg(DISTINCT name, ', '), '')"
```

### BitAnd / BitOr

```python
BitAnd(field="flags").as_sql()  # "bit_and(flags)"
BitOr(field="flags", filter_condition="active").as_sql()
# "bit_or(flags) FILTER (WHERE active)"
```

### BoolAnd / BoolOr

```python
BoolAnd(field="is_valid").as_sql()  # "bool_and(is_valid)"
BoolOr(field="has_error").as_sql()  # "bool_or(has_error)"
```

---

## Range Types

All range types have `lower`, `upper`, and `bounds` (default `"[)"` -- inclusive lower,
exclusive upper).

| Class             | PostgreSQL Type | Python lower/upper types |
| ----------------- | --------------- | ------------------------ |
| `IntegerRange`    | `int4range`     | `int`                    |
| `BigIntegerRange` | `int8range`     | `int`                    |
| `DecimalRange`    | `numrange`      | `float`                  |
| `DateRange`       | `daterange`     | `date`                   |
| `DateTimeRange`   | `tstzrange`     | `datetime`               |

```python
r = IntegerRange(lower=1, upper=10, bounds="[)")
r.as_sql()  # "int4range(1, 10, '[)')"
r.db_type  # "int4range"

# Check containment
r.contains(5)  # "int4range(1, 10, '[)') @> 5"
```

### Range Lookups

| Class                   | Operator | Description              |
| ----------------------- | -------- | ------------------------ |
| `RangeContains`         | `@>`     | Range contains element   |
| `RangeContainedBy`      | `<@`     | Range contained by range |
| `RangeOverlap`          | `&&`     | Ranges overlap           |
| `RangeFullyLessThan`    | `<<`     | Strictly left of         |
| `RangeFullyGreaterThan` | `>>`     | Strictly right of        |
| `RangeAdjacentTo`       | `-\|-`   | Adjacent to              |

```python
rc = RangeContains(column="age_range", value=25)
rc.as_sql()  # "age_range @> ${param}"

ro = RangeOverlap(column="booking_period", other="requested_period")
ro.as_sql()  # "booking_period && ${param}"
```

---

## Indexes

All index classes take `name`, `fields`, optional `opclass`, and optional `condition`
(for partial indexes). Call `.as_sql(table)` to generate the `CREATE INDEX` statement.

### GinIndex

For arrays, JSONB, and full-text search vectors.

```python
idx = GinIndex(
    name="idx_article_tags",
    fields=["tags"],
    opclass="gin_trgm_ops",
)
idx.as_sql("articles")
# 'CREATE INDEX "idx_article_tags" ON "articles" USING GIN ("tags" gin_trgm_ops)'
```

### GistIndex

For geometric types, full-text search, and range types.

```python
idx = GistIndex(name="idx_booking_period", fields=["period"])
idx.as_sql("bookings")
# "CREATE INDEX idx_booking_period ON bookings USING GiST (period)"
```

### BrinIndex

For large, naturally-ordered tables (timestamps, sequential IDs).

```python
idx = BrinIndex(
    name="idx_events_created",
    fields=["created_at"],
    pages_per_range=64,
)
idx.as_sql("events")
# "CREATE INDEX idx_events_created ON events USING BRIN (created_at) WITH (pages_per_range = 64)"
```

### HashIndex

For exact-match equality lookups (faster than B-Tree for pure equality).

```python
idx = HashIndex(name="idx_users_email", fields=["email"])
idx.as_sql("users")
# "CREATE INDEX idx_users_email ON users USING HASH (email)"
```

### SpGistIndex

For partitioned search trees (inet, text with prefix ops).

```python
idx = SpGistIndex(
    name="idx_routes_path",
    fields=["path"],
    opclass="text_ops",
)
idx.as_sql("routes")
```

### BTreeIndex

Standard B-Tree with optional operator class support.

```python
idx = BTreeIndex(
    name="idx_articles_slug",
    fields=["slug"],
    opclass="varchar_pattern_ops",
)
idx.as_sql("articles")
```

### Partial Indexes

All index types support a `condition` parameter for partial indexes:

```python
idx = GinIndex(
    name="idx_active_tags",
    fields=["tags"],
    condition="is_active = true",
)
idx.as_sql("articles")
# 'CREATE INDEX "idx_active_tags" ON "articles" USING GIN ("tags") WHERE (is_active = true)'
```

---

## ExclusionConstraint

PostgreSQL exclusion constraint using GiST (or other) index.

```python
constraint = ExclusionConstraint(
    name="no_overlap_rooms",
    expressions=[("room", "="), ("period", "&&")],
    index_type="GIST",
    condition="cancelled = false",
)
constraint.as_sql()
# 'CONSTRAINT "no_overlap_rooms" EXCLUDE USING GIST ("room" WITH =, "period" WITH &&) WHERE (cancelled = false)'
```

| Parameter     | Type                    | Default  | Description              |
| ------------- | ----------------------- | -------- | ------------------------ |
| `name`        | `str`                   | --       | Constraint name          |
| `expressions` | `list[tuple[str, str]]` | --       | (column, operator) pairs |
| `index_type`  | `str`                   | `"GIST"` | Index method             |
| `condition`   | `str \| None`           | `None`   | Partial constraint WHERE |

---

## ORM Lookup Registration

On import, the module registers 10 custom lookups with `hyperdjango.lookups`:

| Lookup Name            | Operator              | Example                                    |
| ---------------------- | --------------------- | ------------------------------------------ |
| `array_contains`       | `@>`                  | `filter(tags__array_contains=[1, 2])`      |
| `array_contained_by`   | `<@`                  | `filter(tags__array_contained_by=[1,2,3])` |
| `array_overlap`        | `&&`                  | `filter(tags__array_overlap=[1, 2])`       |
| `array_len`            | `array_length(...) =` | `filter(tags__array_len=3)`                |
| `trigram_similar`      | `%`                   | `filter(name__trigram_similar="val")`      |
| `trigram_word_similar` | `%>`                  | `filter(name__trigram_word_similar="val")` |
| `search`               | `@@ plainto_tsquery`  | `filter(body__search="query")`             |
| `has_key`              | `?`                   | `filter(data__has_key="name")`             |
| `has_keys`             | `?&`                  | `filter(data__has_keys=["a", "b"])`        |
| `has_any_keys`         | `?\|`                 | `filter(data__has_any_keys=["a", "b"])`    |

These lookups are available in QuerySet `.filter()` calls after importing
`hyperdjango.postgres`.

---

## Identifier Quoting in SQL Generation

All `as_sql()` methods quote table names, column names, index names, and constraint
names with PostgreSQL double-quote identifiers. This prevents SQL injection through
crafted names and ensures reserved words are handled correctly:

```python
idx = GinIndex(name="idx_article_tags", fields=["tags"])
idx.as_sql("articles")
# 'CREATE INDEX "idx_article_tags" ON "articles" USING GIN ("tags")'

constraint = ExclusionConstraint(
    name="no_overlap",
    expressions=[("room", "="), ("period", "&&")],
)
constraint.as_sql()
# 'CONSTRAINT "no_overlap" EXCLUDE USING GIST ("room" WITH =, "period" WITH &&)'
```

`SearchVector` and `SearchHeadline` also quote field names in their SQL output:

```python
sv = SearchVector(fields=["title", "body"], config="english")
sv.as_sql()
# to_tsvector('english', COALESCE("title", '')) || to_tsvector('english', COALESCE("body", ''))
```

Text search configuration names (e.g., `'english'`) are escaped by doubling single
quotes to prevent injection through config parameters.

---

## Django Migration Guide

| Django                                  | HyperDjango                         |
| --------------------------------------- | ----------------------------------- |
| `django.contrib.postgres.fields`        | `hyperdjango.postgres`              |
| `ArrayField(base_field=IntegerField())` | `ArrayField(base_type="int")`       |
| `HStoreField()`                         | `HStoreField()`                     |
| `JSONField()`                           | `JSONBField()`                      |
| `SearchVector`, `SearchQuery`           | Same names, same API                |
| `TrigramSimilarity`                     | Same name                           |
| `ArrayAgg`, `StringAgg`, etc.           | Same names                          |
| `GinIndex`, `GistIndex`, etc.           | Same names, `.as_sql(table)` method |
| `ExclusionConstraint`                   | Same name                           |
| `__contains` (array lookup)             | `__array_contains`                  |
| `__overlap` (array lookup)              | `__array_overlap`                   |
| `__search` (full-text)                  | `__search`                          |
| `__has_key` (JSONB)                     | `__has_key`                         |
