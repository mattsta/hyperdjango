"""
PostgreSQL-specific extensions for HyperDjango.

Replaces django.contrib.postgres with native HyperDjango implementations.

Provides:
- Field types: ArrayField, HStoreField, JSONBField
- Full-text search: SearchVector, SearchQuery, SearchRank, SearchHeadline
- Trigram similarity: TrigramSimilarity, TrigramDistance, TrigramWordSimilarity, TrigramWordDistance
- Array lookups: ArrayContains, ArrayContainedBy, ArrayOverlap, ArrayLength, ArrayIndex
- Aggregates: ArrayAgg, JSONBAgg, StringAgg, BitAnd, BitOr, BoolAnd, BoolOr
- Range types: IntegerRange, BigIntegerRange, DecimalRange, DateRange, DateTimeRange
- Constraints: ExclusionConstraint
- Indexes: GinIndex, GistIndex, BrinIndex, HashIndex, SpGistIndex, BTreeIndex

Usage:
    from hyperdjango.postgres import (
        ArrayField, SearchVector, SearchQuery, SearchRank,
        TrigramSimilarity, ArrayAgg, StringAgg, IntegerRange,
        ArrayContains, ArrayOverlap, GinIndex, GistIndex,
    )
"""

from dataclasses import dataclass
from typing import Any

from hyperdjango.expressions import Expression
from hyperdjango.lookups import Lookup, register_lookup
from hyperdjango.sqlident import escape_sql_literal, validate_identifier

# ---------------------------------------------------------------------------
# Field Types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ArrayField:
    """PostgreSQL array field -- stores typed arrays (int[], text[], etc.)."""

    base_type: str = "text"
    size: int | None = None
    default: list | None = None

    @property
    def db_type(self) -> str:
        type_map: dict[str, str] = {
            "int": "integer[]",
            "integer": "integer[]",
            "bigint": "bigint[]",
            "smallint": "smallint[]",
            "text": "text[]",
            "varchar": "varchar[]",
            "uuid": "uuid[]",
            "float": "double precision[]",
            "double precision": "double precision[]",
            "bool": "boolean[]",
            "boolean": "boolean[]",
            "date": "date[]",
            "timestamp": "timestamp[]",
            "timestamptz": "timestamptz[]",
            "numeric": "numeric[]",
            "inet": "inet[]",
            "cidr": "cidr[]",
            "jsonb": "jsonb[]",
        }
        return type_map.get(self.base_type, f"{self.base_type}[]")

    @property
    def create_sql(self) -> str:
        """SQL fragment for CREATE TABLE column definition."""
        sql = self.db_type
        if self.default is not None:
            escaped_values = [str(v).replace("'", "''") for v in self.default]
            sql += f" DEFAULT '{{{','.join(escaped_values)}}}'"
        return sql


@dataclass(slots=True)
class HStoreField:
    """PostgreSQL hstore field -- key/value text pairs."""

    default: dict[str, str] | None = None

    @property
    def db_type(self) -> str:
        return "hstore"


@dataclass(slots=True)
class JSONBField:
    """PostgreSQL jsonb field with GIN indexing support."""

    default: object = None

    @property
    def db_type(self) -> str:
        return "jsonb"


# ---------------------------------------------------------------------------
# Full-Text Search
# ---------------------------------------------------------------------------


def _validate_field_name(name: str) -> None:
    """Reject field names with dangerous characters — prevents SQL injection.

    Thin adapter over the shared identifier authority (kept because it has many
    call sites in this module).
    """
    validate_identifier(name, kind="column", source="postgres")


@dataclass(slots=True)
class SearchVector(Expression):
    """PostgreSQL full-text search vector — ORM Expression.

    Generates: to_tsvector('config', COALESCE("field", '')) || ...
    No bind parameters (field names + config are structural).

    Usage:
        vector = SearchVector(["title", "body"], config="english")
        qs.annotate(rank=SearchRank(vector, query))
    """

    fields: list[str]
    config: str = "english"
    weight: str | None = None

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        if not self.fields:
            raise ValueError("SearchVector requires at least one field")
        escaped_config = self.config.replace("'", "''")
        parts: list[str] = []
        for f in self.fields:
            _validate_field_name(f)
            expr = f"to_tsvector('{escaped_config}', COALESCE(\"{f}\", ''))"
            if self.weight:
                # Weight must be A, B, C, or D
                if self.weight not in ("A", "B", "C", "D"):
                    raise ValueError(
                        f"Invalid weight: {self.weight!r} (must be A/B/C/D)"
                    )
                expr = f"setweight({expr}, '{self.weight}')"
            parts.append(expr)
        return " || ".join(parts), []

    @property
    def default_alias(self) -> str:
        return "search_vector"

    @property
    def contains_aggregate(self) -> bool:
        return False


_TSQUERY_FUNC_MAP: dict[str, str] = {
    "plain": "plainto_tsquery",
    "phrase": "phraseto_tsquery",
    "raw": "to_tsquery",
    "websearch": "websearch_to_tsquery",
}


@dataclass(slots=True)
class SearchQuery(Expression):
    """PostgreSQL full-text search query — ORM Expression.

    Generates: plainto_tsquery('config', $N)
    The query text is a bind parameter (safe from injection).

    Usage:
        query = SearchQuery("python tutorial", search_type="websearch")
    """

    query: str
    config: str = "english"
    search_type: str = "plain"

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        escaped_config = self.config.replace("'", "''")
        func = _TSQUERY_FUNC_MAP.get(self.search_type, "plainto_tsquery")
        return f"{func}('{escaped_config}', ${param_offset + 1})", [self.query]

    @property
    def default_alias(self) -> str:
        return "search_query"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class SearchRank(Expression):
    """PostgreSQL ts_rank — ORM Expression.

    Composes SearchVector + SearchQuery with proper parameter tracking.

    Usage:
        rank = SearchRank(
            SearchVector(["title", "body"]),
            SearchQuery("python"),
        )
        results = await Article.objects.annotate(rank=rank).order_by("-rank").all()
    """

    vector: SearchVector
    query: SearchQuery
    weights: list[float] | None = None
    normalization: int = 0

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        vector_sql, vector_params = self.vector.as_sql(param_offset)
        query_sql, query_params = self.query.as_sql(param_offset + len(vector_params))
        all_params = vector_params + query_params

        rank_args = []
        if self.weights:
            # Coerce to float before interpolation. The type hint is list[float]
            # but Python does not enforce it; a str element would otherwise embed
            # raw into the SQL array literal (injection). float() rejects anything
            # non-numeric with a clear ValueError.
            weights_sql = "'{" + ",".join(str(float(w)) for w in self.weights) + "}'"
            rank_args.append(weights_sql)
        rank_args.append(vector_sql)
        rank_args.append(query_sql)
        if self.normalization:
            # Coerce to int for the same reason (declared int, not enforced).
            rank_args.append(str(int(self.normalization)))

        return f"ts_rank({', '.join(rank_args)})", all_params

    @property
    def default_alias(self) -> str:
        return "search_rank"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class SearchHeadline(Expression):
    """PostgreSQL ts_headline — highlighted search results, ORM Expression.

    Usage:
        headline = SearchHeadline("body", query, start_sel="<mark>", stop_sel="</mark>")
        results = await Article.objects.annotate(snippet=headline).all()
    """

    field: str
    query: SearchQuery
    config: str = "english"
    start_sel: str = "<b>"
    stop_sel: str = "</b>"
    max_words: int = 35
    min_words: int = 15
    max_fragments: int = 0

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        _validate_field_name(self.field)
        escaped_config = self.config.replace("'", "''")
        escaped_start = self.start_sel.replace("'", "''")
        escaped_stop = self.stop_sel.replace("'", "''")
        # Enforce int types to prevent injection via options string
        max_w = int(self.max_words)
        min_w = int(self.min_words)
        max_f = int(self.max_fragments)
        options = (
            f"StartSel={escaped_start}, StopSel={escaped_stop}, "
            f"MaxWords={max_w}, MinWords={min_w}, "
            f"MaxFragments={max_f}"
        )
        query_sql, query_params = self.query.as_sql(param_offset)
        return (
            f"ts_headline('{escaped_config}', \"{self.field}\", {query_sql}, '{options}')",
            query_params,
        )

    @property
    def default_alias(self) -> str:
        return "search_headline"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class SearchMatch(Expression):
    """PostgreSQL full-text match: vector @@ query — ORM Expression.

    Use in WHERE clauses via where_raw() or as a filter expression.

    Usage:
        match = SearchMatch(
            SearchVector(["title", "body"]),
            SearchQuery("python web"),
        )
        # Get SQL for where_raw:
        sql, params = match.as_sql(param_offset=0)
    """

    vector: SearchVector
    query: SearchQuery

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        vector_sql, vector_params = self.vector.as_sql(param_offset)
        query_sql, query_params = self.query.as_sql(param_offset + len(vector_params))
        return f"({vector_sql}) @@ ({query_sql})", vector_params + query_params

    @property
    def default_alias(self) -> str:
        return "search_match"

    @property
    def contains_aggregate(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Trigram Similarity (pg_trgm extension)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TrigramSimilarity(Expression):
    """PostgreSQL pg_trgm similarity score (0.0 to 1.0) �� ORM Expression.

    Usage:
        results = await Article.objects.annotate(
            sim=TrigramSimilarity("title", "pythn"),
        ).filter(sim__gt=0.15).order_by("-sim").all()
    """

    field: str
    value: str

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        _validate_field_name(self.field)
        return f'similarity("{self.field}", ${param_offset + 1})', [self.value]

    @property
    def default_alias(self) -> str:
        return f"{self.field}_similarity"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class TrigramDistance(Expression):
    """PostgreSQL pg_trgm distance (1 - similarity) — ORM Expression."""

    field: str
    value: str

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        _validate_field_name(self.field)
        return f'("{self.field}" <-> ${param_offset + 1})', [self.value]

    @property
    def default_alias(self) -> str:
        return f"{self.field}_distance"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class TrigramWordSimilarity(Expression):
    """PostgreSQL pg_trgm word_similarity — best match within text, ORM Expression."""

    field: str
    value: str

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        _validate_field_name(self.field)
        return f'word_similarity(${param_offset + 1}, "{self.field}")', [self.value]

    @property
    def default_alias(self) -> str:
        return f"{self.field}_word_similarity"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class TrigramWordDistance(Expression):
    """PostgreSQL pg_trgm word distance (1 - word_similarity) — ORM Expression."""

    field: str
    value: str

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[object]]:
        _validate_field_name(self.field)
        return f'(${param_offset + 1} <<-> "{self.field}")', [self.value]

    @property
    def default_alias(self) -> str:
        return f"{self.field}_word_distance"

    @property
    def contains_aggregate(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Array Lookups
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ArrayContains:
    """Array contains lookup: @> operator.

    Usage: filter(tags__contains=[1, 2])  ->  tags @> ARRAY[1, 2]
    """

    column: str
    values: list

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column} @> ${{param}}"


@dataclass(slots=True)
class ArrayContainedBy:
    """Array contained-by lookup: <@ operator.

    Usage: filter(tags__contained_by=[1, 2, 3])  ->  tags <@ ARRAY[1, 2, 3]
    """

    column: str
    values: list

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column} <@ ${{param}}"


@dataclass(slots=True)
class ArrayOverlap:
    """Array overlap lookup: && operator.

    Usage: filter(tags__overlap=[1, 2])  ->  tags && ARRAY[1, 2]
    """

    column: str
    values: list

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column} && ${{param}}"


@dataclass(slots=True)
class ArrayLength:
    """Array length function.

    Usage: annotate(tag_count=ArrayLength("tags"))  ->  array_length(tags, 1)
    """

    column: str
    dimension: int = 1

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"array_length({self.column}, {self.dimension})"


@dataclass(slots=True)
class ArrayIndex:
    """Array index access.

    Usage: annotate(first_tag=ArrayIndex("tags", 1))  ->  tags[1]
    Note: PostgreSQL arrays are 1-indexed.
    """

    column: str
    index: int

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column}[{self.index}]"


@dataclass(slots=True)
class ArrayRemove:
    """PostgreSQL array_remove -- remove all occurrences of a value."""

    column: str
    value: object

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"array_remove({self.column}, ${{param}})"


@dataclass(slots=True)
class ArrayAppend:
    """PostgreSQL array_append -- add element to end."""

    column: str
    value: object

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"array_append({self.column}, ${{param}})"


@dataclass(slots=True)
class ArrayPrepend:
    """PostgreSQL array_prepend -- add element to beginning."""

    column: str
    value: object

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"array_prepend(${{param}}, {self.column})"


@dataclass(slots=True)
class ArrayCat:
    """PostgreSQL array_cat -- concatenate two arrays."""

    column: str
    other_column: str

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        _validate_field_name(self.other_column)
        return f'array_cat("{self.column}", "{self.other_column}")'


@dataclass(slots=True)
class ArrayPosition:
    """PostgreSQL array_position -- find index of value in array."""

    column: str
    value: object

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"array_position({self.column}, ${{param}})"


@dataclass(slots=True)
class Unnest:
    """PostgreSQL unnest -- expand array to rows."""

    column: str

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"unnest({self.column})"


# ---------------------------------------------------------------------------
# Aggregate Functions
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ArrayAgg:
    """PostgreSQL array_agg() aggregate."""

    field: str
    distinct: bool = False
    ordering: str | None = None
    filter_condition: str | None = None
    default: list | None = None

    def as_sql(self) -> str:
        _validate_field_name(self.field)
        expr = f"DISTINCT {self.field}" if self.distinct else self.field
        if self.ordering:
            expr = f"{expr} ORDER BY {self.ordering}"  # sql-raw: developer-supplied ORDER BY expression
        sql = f"array_agg({expr})"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"  # sql-raw: developer-supplied WHERE expression
        if self.default is not None:
            # Render the array literal, then escape it as a single SQL string
            # literal so a value carrying a quote can't break out (mirrors
            # StringAgg's escape_sql_literal discipline — ArrayAgg had drifted).
            inner = ",".join(str(v) for v in self.default)
            default_str = "'" + escape_sql_literal("{" + inner + "}") + "'"
            sql = f"COALESCE({sql}, {default_str})"
        return sql


@dataclass(slots=True)
class JSONBAgg:
    """PostgreSQL jsonb_agg() aggregate."""

    field: str
    distinct: bool = False
    ordering: str | None = None
    filter_condition: str | None = None
    default: str | None = None

    def as_sql(self) -> str:
        _validate_field_name(self.field)
        expr = f"DISTINCT {self.field}" if self.distinct else self.field
        if self.ordering:
            expr = f"{expr} ORDER BY {self.ordering}"  # sql-raw: developer-supplied ORDER BY expression
        sql = f"jsonb_agg({expr})"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"  # sql-raw: developer-supplied WHERE expression
        if self.default is not None:
            # Escape the default JSON literal so a quote can't break out of the
            # string (mirrors StringAgg; JSONBAgg had drifted to a raw embed).
            sql = f"COALESCE({sql}, '{escape_sql_literal(self.default)}'::jsonb)"
        return sql


@dataclass(slots=True)
class StringAgg:
    """PostgreSQL string_agg() aggregate."""

    field: str
    delimiter: str = ", "
    distinct: bool = False
    ordering: str | None = None
    filter_condition: str | None = None
    default: str | None = None

    def as_sql(self) -> str:

        _validate_field_name(self.field)
        expr = f"DISTINCT {self.field}" if self.distinct else self.field
        # ordering/filter_condition are raw SQL expression fragments by contract
        # (e.g. "created ASC", "active = true") — the sanctioned developer hatch.
        if self.ordering:
            expr = f"{expr} ORDER BY {self.ordering}"  # sql-raw: developer-supplied ORDER BY expression
        # delimiter/default are quoted string LITERALS — escape ' so they can't
        # break out of the literal.
        sql = f"string_agg({expr}, '{escape_sql_literal(self.delimiter)}')"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"  # sql-raw: developer-supplied WHERE expression
        if self.default is not None:
            sql = f"COALESCE({sql}, '{escape_sql_literal(self.default)}')"
        return sql


@dataclass(slots=True)
class BitAnd:
    """PostgreSQL bit_and() aggregate."""

    field: str
    filter_condition: str | None = None

    def as_sql(self) -> str:
        _validate_field_name(self.field)
        sql = f"bit_and({self.field})"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"
        return sql


@dataclass(slots=True)
class BitOr:
    """PostgreSQL bit_or() aggregate."""

    field: str
    filter_condition: str | None = None

    def as_sql(self) -> str:
        _validate_field_name(self.field)
        sql = f"bit_or({self.field})"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"
        return sql


@dataclass(slots=True)
class BoolAnd:
    """PostgreSQL bool_and() aggregate."""

    field: str
    filter_condition: str | None = None

    def as_sql(self) -> str:
        _validate_field_name(self.field)
        sql = f"bool_and({self.field})"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"
        return sql


@dataclass(slots=True)
class BoolOr:
    """PostgreSQL bool_or() aggregate."""

    field: str
    filter_condition: str | None = None

    def as_sql(self) -> str:
        _validate_field_name(self.field)
        sql = f"bool_or({self.field})"
        if self.filter_condition:
            sql = f"{sql} FILTER (WHERE {self.filter_condition})"
        return sql


# ---------------------------------------------------------------------------
# Range Types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IntegerRange:
    """PostgreSQL int4range."""

    lower: int | None = None
    upper: int | None = None
    bounds: str = "[)"

    def as_sql(self) -> str:
        lower_str = str(self.lower) if self.lower is not None else "NULL"
        upper_str = str(self.upper) if self.upper is not None else "NULL"
        return f"int4range({lower_str}, {upper_str}, '{self.bounds}')"

    @property
    def db_type(self) -> str:
        return "int4range"

    def contains(self, value: int) -> str:
        return f"int4range({self.lower}, {self.upper}, '{self.bounds}') @> {value}"


@dataclass(slots=True)
class BigIntegerRange:
    """PostgreSQL int8range."""

    lower: int | None = None
    upper: int | None = None
    bounds: str = "[)"

    def as_sql(self) -> str:
        lower_str = str(self.lower) if self.lower is not None else "NULL"
        upper_str = str(self.upper) if self.upper is not None else "NULL"
        return f"int8range({lower_str}, {upper_str}, '{self.bounds}')"

    @property
    def db_type(self) -> str:
        return "int8range"


@dataclass(slots=True)
class DecimalRange:
    """PostgreSQL numrange."""

    lower: float | None = None
    upper: float | None = None
    bounds: str = "[)"

    def as_sql(self) -> str:
        lower_str = str(self.lower) if self.lower is not None else "NULL"
        upper_str = str(self.upper) if self.upper is not None else "NULL"
        return f"numrange({lower_str}, {upper_str}, '{self.bounds}')"

    @property
    def db_type(self) -> str:
        return "numrange"


@dataclass(slots=True)
class DateRange:
    """PostgreSQL daterange."""

    lower: object = None
    upper: object = None
    bounds: str = "[)"

    def as_sql(self) -> str:
        lower_str = f"'{self.lower}'" if self.lower is not None else "NULL"
        upper_str = f"'{self.upper}'" if self.upper is not None else "NULL"
        return f"daterange({lower_str}, {upper_str}, '{self.bounds}')"

    @property
    def db_type(self) -> str:
        return "daterange"


@dataclass(slots=True)
class DateTimeRange:
    """PostgreSQL tstzrange (timestamp with time zone range)."""

    lower: object = None
    upper: object = None
    bounds: str = "[)"

    def as_sql(self) -> str:
        lower_str = f"'{self.lower}'" if self.lower is not None else "NULL"
        upper_str = f"'{self.upper}'" if self.upper is not None else "NULL"
        return f"tstzrange({lower_str}, {upper_str}, '{self.bounds}')"

    @property
    def db_type(self) -> str:
        return "tstzrange"


# ---------------------------------------------------------------------------
# Range Lookups
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RangeContains:
    """Range contains element: range @> element."""

    column: str
    value: object

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column} @> ${{param}}"


@dataclass(slots=True)
class RangeContainedBy:
    """Range contained by another range: range <@ range."""

    column: str
    other: str

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column} <@ ${{param}}"


@dataclass(slots=True)
class RangeOverlap:
    """Range overlap: range && range."""

    column: str
    other: str

    def as_sql(self) -> str:
        _validate_field_name(self.column)
        return f"{self.column} && ${{param}}"


@dataclass(slots=True)
class RangeFullyLessThan:
    """Strictly left of: range << range."""

    column: str
    other: str

    def as_sql(self) -> str:
        return f"{self.column} << ${{param}}"


@dataclass(slots=True)
class RangeFullyGreaterThan:
    """Strictly right of: range >> range."""

    column: str
    other: str

    def as_sql(self) -> str:
        return f"{self.column} >> ${{param}}"


@dataclass(slots=True)
class RangeAdjacentTo:
    """Adjacent to: range -|- range."""

    column: str
    other: str

    def as_sql(self) -> str:
        return f"{self.column} -|- ${{param}}"


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExclusionConstraint:
    """PostgreSQL exclusion constraint using GiST index.

    Usage:
        ExclusionConstraint(
            name="no_overlap_rooms",
            expressions=[("room", "="), ("period", "&&")],
            index_type="GIST",
        )
    Generates:
        EXCLUDE USING GIST (room WITH =, period WITH &&)
    """

    name: str
    expressions: list[tuple[str, str]]
    index_type: str = "GIST"
    condition: str | None = None

    def as_sql(self) -> str:
        expr_parts = [f'"{col}" WITH {op}' for col, op in self.expressions]
        sql = f'CONSTRAINT "{self.name}" EXCLUDE USING {self.index_type} ({", ".join(expr_parts)})'
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GinIndex:
    """PostgreSQL GIN index -- for arrays, JSONB, full-text search."""

    name: str
    fields: list[str]
    opclass: str | None = None
    condition: str | None = None

    def as_sql(self, table: str) -> str:
        if self.opclass:
            cols = ", ".join(f'"{f}" {self.opclass}' for f in self.fields)
        else:
            cols = ", ".join(f'"{f}"' for f in self.fields)
        sql = f'CREATE INDEX "{self.name}" ON "{table}" USING GIN ({cols})'
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


@dataclass(slots=True)
class GistIndex:
    """PostgreSQL GiST index -- for geometric, full-text, range types."""

    name: str
    fields: list[str]
    opclass: str | None = None
    condition: str | None = None

    def as_sql(self, table: str) -> str:
        if self.opclass:
            cols = ", ".join(f'"{f}" {self.opclass}' for f in self.fields)
        else:
            cols = ", ".join(f'"{f}"' for f in self.fields)
        sql = f'CREATE INDEX "{self.name}" ON "{table}" USING GiST ({cols})'
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


@dataclass(slots=True)
class BrinIndex:
    """PostgreSQL BRIN index -- for large naturally-ordered tables."""

    name: str
    fields: list[str]
    pages_per_range: int = 128
    condition: str | None = None

    def as_sql(self, table: str) -> str:
        cols = ", ".join(f'"{f}"' for f in self.fields)
        sql = (
            f'CREATE INDEX "{self.name}" ON "{table}" USING BRIN ({cols}) '
            f"WITH (pages_per_range = {self.pages_per_range})"
        )
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


@dataclass(slots=True)
class HashIndex:
    """PostgreSQL hash index -- for exact-match equality lookups."""

    name: str
    fields: list[str]
    condition: str | None = None

    def as_sql(self, table: str) -> str:
        cols = ", ".join(f'"{f}"' for f in self.fields)
        sql = f'CREATE INDEX "{self.name}" ON "{table}" USING HASH ({cols})'
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


@dataclass(slots=True)
class SpGistIndex:
    """PostgreSQL SP-GiST index -- for partitioned search trees."""

    name: str
    fields: list[str]
    opclass: str | None = None
    condition: str | None = None

    def as_sql(self, table: str) -> str:
        if self.opclass:
            cols = ", ".join(f'"{f}" {self.opclass}' for f in self.fields)
        else:
            cols = ", ".join(f'"{f}"' for f in self.fields)
        sql = f'CREATE INDEX "{self.name}" ON "{table}" USING SPGiST ({cols})'
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


@dataclass(slots=True)
class BTreeIndex:
    """PostgreSQL B-Tree index -- standard index with opclass support."""

    name: str
    fields: list[str]
    opclass: str | None = None
    condition: str | None = None

    def as_sql(self, table: str) -> str:
        if self.opclass:
            cols = ", ".join(f'"{f}" {self.opclass}' for f in self.fields)
        else:
            cols = ", ".join(f'"{f}"' for f in self.fields)
        sql = f'CREATE INDEX "{self.name}" ON "{table}" USING BTREE ({cols})'
        if self.condition:
            sql += f" WHERE ({self.condition})"
        return sql


# ---------------------------------------------------------------------------
# ORM Lookup Registration -- integrate with hyperdjango.lookups
# ---------------------------------------------------------------------------


class ArrayContainsLookup(Lookup):
    """Array @> operator: field__array_contains=[1,2] -> col @> $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} @> ${param_idx}", [value]


class ArrayContainedByLookup(Lookup):
    """Array <@ operator: field__array_contained_by=[1,2,3] -> col <@ $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} <@ ${param_idx}", [value]


class ArrayOverlapLookup(Lookup):
    """Array && operator: field__array_overlap=[1,2] -> col && $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} && ${param_idx}", [value]


class ArrayLenLookup(Lookup):
    """Array length: field__array_len=3 -> array_length(col, 1) = $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"array_length({col}, 1) = ${param_idx}", [value]


class TrigramSimilarLookup(Lookup):
    """Trigram similarity: field__trigram_similar='val' -> col % $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} % ${param_idx}", [value]


class TrigramWordSimilarLookup(Lookup):
    """Trigram word similarity: field__trigram_word_similar='val' -> $N %> col"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"${param_idx} %> {col}", [value]


class SearchLookup(Lookup):
    """Full-text search: field__search='query' -> to_tsvector(col) @@ plainto_tsquery($N)"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"to_tsvector({col}) @@ plainto_tsquery(${param_idx})", [value]


class HasKeyLookup(Lookup):
    """JSONB/HStore has key: field__has_key='k' -> col ? $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ? ${param_idx}", [value]


class HasKeysLookup(Lookup):
    """JSONB/HStore has all keys: field__has_keys=['a','b'] -> col ?& $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ?& ${param_idx}", [value]


class HasAnyKeysLookup(Lookup):
    """JSONB/HStore has any keys: field__has_any_keys=['a','b'] -> col ?| $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ?| ${param_idx}", [value]


# Register PostgreSQL-specific lookups
register_lookup("array_contains", ArrayContainsLookup())
register_lookup("array_contained_by", ArrayContainedByLookup())
register_lookup("array_overlap", ArrayOverlapLookup())
register_lookup("array_len", ArrayLenLookup())
register_lookup("trigram_similar", TrigramSimilarLookup())
register_lookup("trigram_word_similar", TrigramWordSimilarLookup())
register_lookup("search", SearchLookup())
register_lookup("has_key", HasKeyLookup())
register_lookup("has_keys", HasKeysLookup())
register_lookup("has_any_keys", HasAnyKeysLookup())
