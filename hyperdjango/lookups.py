"""
ORM lookups and transforms — extensible filter system for QuerySet.filter().

Lookups convert Django-style keyword arguments (e.g., name__icontains="alice")
into parameterized SQL WHERE clauses.

Transforms modify a column reference before a lookup is applied (e.g.,
created__year=2024 extracts the year from a timestamp before comparing).

Built-in lookups:
    exact, iexact, contains, icontains, startswith, istartswith,
    endswith, iendswith, gt, gte, lt, lte, in, range, isnull, regex, iregex

Built-in transforms:
    year, month, day, hour, minute, second, week_day, date,
    lower, upper, length, trim

Custom lookups and transforms can be registered via register_lookup()
and register_transform().

Usage:
    from hyperdjango.lookups import resolve_lookup

    # Returns (sql_fragment, params_list) for a single filter condition
    col_sql, lookup_sql, params = resolve_lookup("name__icontains", "alice")
    # -> ("name", "ILIKE $1", ["%alice%"])

    # With transforms:
    col_sql, lookup_sql, params = resolve_lookup("created__year", 2024)
    # -> ("EXTRACT(YEAR FROM created)", "= $1", [2024])
"""

import re
import threading
from typing import Any

from hyperdjango.sqlident import validate_column_path
from hyperdjango.where import SENTINEL_IDX, WhereNode

# Comparison lookups that accept an F()/expression on the right-hand side and
# render it as an inline column reference instead of a bound value.
_F_RHS_OPS = {"exact": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters (% and _) in a value string."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Lookup registry
# ---------------------------------------------------------------------------

_lookup_registry: dict[str, Lookup] = {}
_transform_registry: dict[str, Transform] = {}
_lookup_registry_lock = threading.Lock()

# Versioned snapshot — avoid dict() copy on every resolve call.
# Incremented on register_lookup/register_transform; snapshot rebuilt on
# version mismatch. Packed as a single (version, lookups, transforms) tuple
# that is swapped atomically so a lockless reader's single load is always
# internally consistent — it can never observe a new version paired with a
# stale or mismatched snapshot dict (mirrors signals.py's packed snapshot).
_registry_version: int = 0
_snapshot: tuple[int, dict[str, Lookup], dict[str, Transform]] = (-1, {}, {})


def _get_registry_snapshots() -> tuple[dict[str, Lookup], dict[str, Transform]]:
    """Get thread-safe registry snapshots, rebuilding only when version changes.

    Hot path (99.9% of calls): version matches → return cached snapshots (zero alloc).
    Cold path (after register_lookup/register_transform): rebuild under lock.
    """
    global _snapshot
    version, lookups, transforms = _snapshot  # single atomic load
    if version == _registry_version:
        return lookups, transforms
    with _lookup_registry_lock:
        snapshot = (
            _registry_version,
            dict(_lookup_registry),
            dict(_transform_registry),
        )
        _snapshot = snapshot
    return snapshot[1], snapshot[2]


def register_lookup(name: str, lookup: Lookup):
    """Register a custom lookup by name."""
    global _registry_version
    with _lookup_registry_lock:
        _lookup_registry[name] = lookup
        _registry_version += 1


def register_transform(name: str, transform: Transform):
    """Register a custom transform by name."""
    global _registry_version
    with _lookup_registry_lock:
        _transform_registry[name] = transform
        _registry_version += 1


# ---------------------------------------------------------------------------
# Lookup base class
# ---------------------------------------------------------------------------


class Lookup:
    """Base class for SQL lookups.

    A lookup takes a column reference and a value, producing a SQL condition
    fragment with parameterized placeholders.

    Supports two modes:
    - Full mode: as_sql() returns (sql_fragment, params) — for initial compilation
    - Bind-only mode: bind_params() returns just params — for cached SQL reuse
    """

    # True if this lookup's emitted SQL varies by the filter VALUE beyond the
    # structural shape codes in where.value_shape() (e.g. the pgvector lookups,
    # which format a per-call ::vector literal / pick the distance operator from
    # the value). Such lookups MUST skip the shape-keyed compiled-SQL fast cache.
    # A custom Lookup subclass that builds value-dependent SQL sets this True so
    # key_is_value_dependent() forces the slow path for it too.
    value_dependent: bool = False

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        """Generate SQL condition.

        Args:
            col: The qualified column reference (e.g., "users.name" or "LOWER(name)")
            param_idx: The next available parameter index (1-based for PostgreSQL)
            value: The filter value from the user

        Returns:
            (sql_fragment, params_list) — e.g., ("name = $1", [value])
        """
        raise NotImplementedError("Subclass must implement this method")

    def bind_params(self, value: Any) -> list[Any]:
        """Extract just the bind parameter values (no SQL generation).

        Used by compiled query cache — skip SQL string building on cache hit,
        only collect the parameter values in the same order as as_sql() would.

        Default: most lookups pass value through unchanged as a single param.
        Override in subclasses that transform the value (e.g., LIKE wrapping).
        """
        return [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        """Produce a WhereNode for this lookup.

        Calls as_sql() with a sentinel param index, then converts $SENTINEL
        placeholders to {} for the WhereNode template. This works for all
        lookup types including multi-param lookups (range, vector distance).
        """
        sql, params = self.as_sql(col, SENTINEL_IDX, value)
        template = sql
        for i in range(len(params)):
            template = template.replace(f"${SENTINEL_IDX + i}", "{}", 1)
        return WhereNode(template=template, bind_values=params)

    @property
    def negated_sql(self) -> str | None:
        """If set, used for exclude() instead of wrapping in NOT(...)."""
        return None


# ---------------------------------------------------------------------------
# Built-in lookups
# ---------------------------------------------------------------------------


class ExactLookup(Lookup):
    """Exact match: field=value -> col = $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        if value is None:
            return f"{col} IS NULL", []
        return f"{col} = ${param_idx}", [value]

    def bind_params(self, value: Any) -> list[Any]:
        return [] if value is None else [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        if value is None:
            return WhereNode(template=f"{col} IS NULL")
        return WhereNode(template=f"{col} = {{}}", bind_values=[value])


class IExactLookup(Lookup):
    """Case-insensitive exact match: field__iexact=value -> UPPER(col) = UPPER($N)"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"UPPER({col}) = UPPER(${param_idx})", [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(template=f"UPPER({col}) = UPPER({{}})", bind_values=[value])


class ContainsLookup(Lookup):
    """Case-sensitive substring match: field__contains=value -> col LIKE '%value%' ESCAPE '\\'"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} LIKE ${param_idx} ESCAPE '\\'", [f"%{_escape_like(str(value))}%"]

    def bind_params(self, value: Any) -> list[Any]:
        return [f"%{_escape_like(str(value))}%"]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(
            template=f"{col} LIKE {{}} ESCAPE '\\'",
            bind_values=[f"%{_escape_like(str(value))}%"],
        )


class IContainsLookup(Lookup):
    """Case-insensitive substring match: field__icontains=value -> col ILIKE '%value%' ESCAPE '\\'"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ILIKE ${param_idx} ESCAPE '\\'", [
            f"%{_escape_like(str(value))}%"
        ]

    def bind_params(self, value: Any) -> list[Any]:
        return [f"%{_escape_like(str(value))}%"]


class StartsWithLookup(Lookup):
    """Case-sensitive prefix match: field__startswith=value -> col LIKE 'value%' ESCAPE '\\'"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} LIKE ${param_idx} ESCAPE '\\'", [f"{_escape_like(str(value))}%"]

    def bind_params(self, value: Any) -> list[Any]:
        return [f"{_escape_like(str(value))}%"]


class IStartsWithLookup(Lookup):
    """Case-insensitive prefix match."""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ILIKE ${param_idx} ESCAPE '\\'", [f"{_escape_like(str(value))}%"]

    def bind_params(self, value: Any) -> list[Any]:
        return [f"{_escape_like(str(value))}%"]


class EndsWithLookup(Lookup):
    """Case-sensitive suffix match: field__endswith=value -> col LIKE '%value' ESCAPE '\\'"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} LIKE ${param_idx} ESCAPE '\\'", [f"%{_escape_like(str(value))}"]

    def bind_params(self, value: Any) -> list[Any]:
        return [f"%{_escape_like(str(value))}"]


class IEndsWithLookup(Lookup):
    """Case-insensitive suffix match."""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ILIKE ${param_idx} ESCAPE '\\'", [f"%{_escape_like(str(value))}"]

    def bind_params(self, value: Any) -> list[Any]:
        return [f"%{_escape_like(str(value))}"]


class GtLookup(Lookup):
    """Greater than: field__gt=value -> col > $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} > ${param_idx}", [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(template=f"{col} > {{}}", bind_values=[value])


class GteLookup(Lookup):
    """Greater than or equal: field__gte=value -> col >= $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} >= ${param_idx}", [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(template=f"{col} >= {{}}", bind_values=[value])


class LtLookup(Lookup):
    """Less than: field__lt=value -> col < $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} < ${param_idx}", [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(template=f"{col} < {{}}", bind_values=[value])


class LteLookup(Lookup):
    """Less than or equal: field__lte=value -> col <= $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} <= ${param_idx}", [value]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(template=f"{col} <= {{}}", bind_values=[value])


class InLookup(Lookup):
    """Membership test: field__in=[1,2,3] -> col = ANY($N)

    Passes the list as a single parameter — pg.zig converts Python lists
    to PostgreSQL array literals ({1,2,3}) natively.

    When the values list contains None, PostgreSQL's ``col = ANY(array)`` does
    NOT match NULL (NULL comparison is unknown). We split into
    ``(col = ANY(non_null_array)) OR col IS NULL`` so semantics match Python's
    ``in`` operator and ``exclude(field__in=[..., None])`` correctly excludes
    NULL rows via De Morgan's law.
    """

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise TypeError(
                f"__in lookup requires a list/tuple/set, got {type(value).__name__}"
            )
        items = list(value)
        if not items:
            return "FALSE", []
        non_null = [v for v in items if v is not None]
        has_null = len(non_null) != len(items)
        if has_null and non_null:
            return (
                f"({col} = ANY(${param_idx}) OR {col} IS NULL)",
                [non_null],
            )
        if has_null:  # list of only Nones
            return f"{col} IS NULL", []
        return f"{col} = ANY(${param_idx})", [non_null]

    def bind_params(self, value: Any) -> list[Any]:
        items = list(value)
        non_null = [v for v in items if v is not None]
        if not non_null:
            return []
        return [non_null]

    def to_node(self, col: str, value: Any) -> WhereNode:
        items = list(value)
        if not items:
            return WhereNode(template="FALSE")
        non_null = [v for v in items if v is not None]
        has_null = len(non_null) != len(items)
        if has_null and non_null:
            return WhereNode(
                template=f"({col} = ANY({{}}) OR {col} IS NULL)",
                bind_values=[non_null],
            )
        if has_null:
            return WhereNode(template=f"{col} IS NULL")
        return WhereNode(template=f"{col} = ANY({{}})", bind_values=[non_null])


class RangeLookup(Lookup):
    """Range test: field__range=(low, high) -> col BETWEEN $N AND $M"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise TypeError(
                f"__range lookup requires a 2-element tuple/list, got {value!r}"
            )
        return f"{col} BETWEEN ${param_idx} AND ${param_idx + 1}", [value[0], value[1]]

    def bind_params(self, value: Any) -> list[Any]:
        return [value[0], value[1]]

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(
            template=f"{col} BETWEEN {{}} AND {{}}", bind_values=[value[0], value[1]]
        )


class IsNullLookup(Lookup):
    """Null check: field__isnull=True -> col IS NULL, field__isnull=False -> col IS NOT NULL"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        if value:
            return f"{col} IS NULL", []
        else:
            return f"{col} IS NOT NULL", []

    def bind_params(self, value: Any) -> list[Any]:
        return []  # No bind params — IS NULL/IS NOT NULL are literal SQL

    def to_node(self, col: str, value: Any) -> WhereNode:
        return WhereNode(template=f"{col} IS NULL" if value else f"{col} IS NOT NULL")


class RegexLookup(Lookup):
    """PostgreSQL regex match: field__regex=pattern -> col ~ $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ~ ${param_idx}", [value]


class IRegexLookup(Lookup):
    """Case-insensitive regex match: field__iregex=pattern -> col ~* $N"""

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        return f"{col} ~* ${param_idx}", [value]


# ---------------------------------------------------------------------------
# pgvector distance lookups
# ---------------------------------------------------------------------------


class L2DistanceLookup(Lookup):
    """Vector L2 (Euclidean) distance lookup.

    Usage: filter(embedding__l2_distance=(query_vector, 1.5))
    SQL: embedding <-> $1 < $2
    """

    value_dependent = True

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        vector, threshold = value
        vec_str = _format_vector(vector)
        return f"{col} <-> ${param_idx}::vector < ${param_idx + 1}", [
            vec_str,
            threshold,
        ]

    def bind_params(self, value: Any) -> list[Any]:
        # Mirror as_sql: the vector is bound FORMATTED as a pgvector literal, and
        # the threshold is a second param. The inherited default [value] returns a
        # single unformatted tuple, breaking the count/format on cache-hit.
        vector, threshold = value
        return [_format_vector(vector), threshold]


class CosineDistanceLookup(Lookup):
    """Vector cosine distance lookup.

    Usage: filter(embedding__cosine_distance=(query_vector, 0.2))
    SQL: embedding <=> $1 < $2
    """

    value_dependent = True

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        vector, threshold = value
        vec_str = _format_vector(vector)
        return f"{col} <=> ${param_idx}::vector < ${param_idx + 1}", [
            vec_str,
            threshold,
        ]

    def bind_params(self, value: Any) -> list[Any]:
        vector, threshold = value
        return [_format_vector(vector), threshold]


class InnerProductLookup(Lookup):
    """Vector negative inner product lookup.

    Usage: filter(embedding__inner_product=(query_vector, -0.8))
    SQL: embedding <#> $1 < $2
    Note: pgvector <#> returns NEGATIVE inner product, so smaller = more similar.
    """

    value_dependent = True

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        vector, threshold = value
        vec_str = _format_vector(vector)
        return f"{col} <#> ${param_idx}::vector < ${param_idx + 1}", [
            vec_str,
            threshold,
        ]

    def bind_params(self, value: Any) -> list[Any]:
        vector, threshold = value
        return [_format_vector(vector), threshold]


class NearestLookup(Lookup):
    """Find K nearest neighbors by distance metric.

    Usage: filter(embedding__nearest=(query_vector, "cosine"))
    This sets up ORDER BY for the distance operator — use with .limit(k).
    SQL: 1=1 (always true) with ORDER BY col <=> $1 implied via _vector_order.
    """

    value_dependent = True

    def as_sql(self, col: str, param_idx: int, value: Any) -> tuple[str, list[Any]]:
        vector, metric = (
            value if isinstance(value, (tuple, list)) else (value, "cosine")
        )
        vec_str = _format_vector(vector)
        op = {"cosine": "<=>", "l2": "<->", "inner_product": "<#>"}.get(metric, "<=>")
        # Return ordering expression that QuerySet can use
        return f"{col} {op} ${param_idx}::vector IS NOT NULL", [vec_str]

    def bind_params(self, value: Any) -> list[Any]:
        # Only the vector is bound; the metric selects the operator emitted by
        # as_sql (value-dependent SQL — see _VALUE_DEPENDENT_LOOKUPS).
        vector, _metric = (
            value if isinstance(value, (tuple, list)) else (value, "cosine")
        )
        return [_format_vector(vector)]


def _format_vector(vector: list[float] | str) -> str:
    """Format a vector for PostgreSQL pgvector."""
    if isinstance(vector, str):
        return vector
    return "[" + ",".join(str(float(v)) for v in vector) + "]"


# ---------------------------------------------------------------------------
# Transform base class
# ---------------------------------------------------------------------------


class Transform:
    """Base class for SQL transforms.

    A transform modifies a column reference before a lookup is applied.
    For example, EXTRACT(YEAR FROM col) or LOWER(col).
    """

    def as_sql(self, col: str) -> str:
        """Transform the column reference.

        Args:
            col: The column reference to transform

        Returns:
            Transformed SQL column expression
        """
        raise NotImplementedError("Subclass must implement this method")


# ---------------------------------------------------------------------------
# Built-in transforms — date/time
# ---------------------------------------------------------------------------


class ExtractTransform(Transform):
    """EXTRACT(field FROM col) — extracts a date/time component."""

    def __init__(self, field: str):
        self.field = field

    def as_sql(self, col: str) -> str:
        return f"EXTRACT({self.field} FROM {col})"


class DateTransform(Transform):
    """col::date — cast timestamp to date."""

    def as_sql(self, col: str) -> str:
        return f"{col}::date"


# ---------------------------------------------------------------------------
# Built-in transforms — text
# ---------------------------------------------------------------------------


class LowerTransform(Transform):
    """LOWER(col) — lowercase text."""

    def as_sql(self, col: str) -> str:
        return f"LOWER({col})"


class UpperTransform(Transform):
    """UPPER(col) — uppercase text."""

    def as_sql(self, col: str) -> str:
        return f"UPPER({col})"


class LengthTransform(Transform):
    """LENGTH(col) — string length."""

    def as_sql(self, col: str) -> str:
        return f"LENGTH({col})"


class TrimTransform(Transform):
    """TRIM(col) — strip whitespace."""

    def as_sql(self, col: str) -> str:
        return f"TRIM({col})"


# ---------------------------------------------------------------------------
# Register all built-in lookups and transforms
# ---------------------------------------------------------------------------

# Lookups
_BUILTIN_LOOKUPS: dict[str, Lookup] = {
    "exact": ExactLookup(),
    "iexact": IExactLookup(),
    "contains": ContainsLookup(),
    "icontains": IContainsLookup(),
    "startswith": StartsWithLookup(),
    "istartswith": IStartsWithLookup(),
    "endswith": EndsWithLookup(),
    "iendswith": IEndsWithLookup(),
    "gt": GtLookup(),
    "gte": GteLookup(),
    "lt": LtLookup(),
    "lte": LteLookup(),
    "in": InLookup(),
    "range": RangeLookup(),
    "isnull": IsNullLookup(),
    "regex": RegexLookup(),
    "iregex": IRegexLookup(),
    # pgvector distance lookups
    "l2_distance": L2DistanceLookup(),
    "cosine_distance": CosineDistanceLookup(),
    "inner_product": InnerProductLookup(),
    "nearest": NearestLookup(),
}

_BUILTIN_TRANSFORMS: dict[str, Transform] = {
    "year": ExtractTransform("YEAR"),
    "month": ExtractTransform("MONTH"),
    "day": ExtractTransform("DAY"),
    "hour": ExtractTransform("HOUR"),
    "minute": ExtractTransform("MINUTE"),
    "second": ExtractTransform("SECOND"),
    "week_day": ExtractTransform("DOW"),
    "date": DateTransform(),
    "lower": LowerTransform(),
    "upper": UpperTransform(),
    "length": LengthTransform(),
    "trim": TrimTransform(),
}

# Initialize registries with builtins
_lookup_registry.update(_BUILTIN_LOOKUPS)
_transform_registry.update(_BUILTIN_TRANSFORMS)

# Lookups whose emitted SQL depends on the filter VALUE — the pgvector lookups
# format the vector into a per-call ::vector literal and NearestLookup even
# picks the distance OPERATOR (<=> / <-> / <#>) from the value's metric. The
# compiled-SQL fast cache keys on structural SHAPE only, so a queryset using one
# of these must skip the cache (an `l2` request must not reuse a cached `cosine`
# SQL). Mirrors the __in-subquery / Exists slow-path forcing (see query.py).
#
# This hardcoded set is a FALLBACK/fast-path for the four builtins; the general
# mechanism is the ``Lookup.value_dependent`` class attribute — any registered
# lookup (builtin or user-registered) that sets it True also forces the slow
# path via key_is_value_dependent() below.
_VALUE_DEPENDENT_LOOKUPS: frozenset[str] = frozenset(
    ("l2_distance", "cosine_distance", "inner_product", "nearest")
)


def key_is_value_dependent(key: str) -> bool:
    """True if `key`'s lookup suffix emits value-dependent SQL.

    Used by QuerySet.filter()/exclude() to force the compiled-SQL slow path so
    value-dependent SQL is never served from a shape-keyed cache entry. Consults
    both the hardcoded builtin set AND the registered lookup's ``value_dependent``
    flag, so a user-registered value-dependent lookup is honoured too.
    """
    sep = key.rfind("__")
    if sep < 0:
        return False
    suffix = key[sep + 2 :]
    if suffix in _VALUE_DEPENDENT_LOOKUPS:
        return True
    lookups, _ = _get_registry_snapshots()
    lookup = lookups.get(suffix)
    return lookup is not None and lookup.value_dependent


# ---------------------------------------------------------------------------
# Lookup resolution — the main entry point
# ---------------------------------------------------------------------------


def resolve_lookup(
    key: str,
    value: Any,
    param_idx: int = 1,
    table_alias: str | None = None,
    join_aliases: dict[str, str] | None = None,
    annotation_aliases: set[str] | None = None,
) -> tuple[str, list[Any]]:
    """Resolve a Django-style filter key into a SQL condition.

    Handles:
    - Simple column: name="Alice" -> name = $1
    - Lookup: age__gte=18 -> age >= $1
    - Transform + lookup: created__year=2024 -> EXTRACT(YEAR FROM created) = $1
    - Transform chain: name__lower__contains="alice" -> LOWER(name) LIKE $1
    - FK span: author__name="Alice" -> t1.name = $1 (if join_aliases provided)

    Args:
        key: The filter keyword (e.g., "name", "age__gte", "created__year__gte")
        value: The filter value
        param_idx: Next available parameter index (1-based)
        table_alias: Main table name for qualification
        join_aliases: Dict of FK path -> table alias
        annotation_aliases: Set of annotation alias names (don't qualify these)

    Returns:
        (sql_condition, params_list)
    """
    parts = key.split("__")

    # Versioned snapshots — no dict copy on hot path (rebuilt only on registry change)
    lookups, xforms = _get_registry_snapshots()

    # Walk through parts: resolve FK spans, transforms, and final lookup
    col_parts: list[str] = []
    transforms: list[Transform] = []
    lookup_name = "exact"

    i = 0
    while i < len(parts):
        part = parts[i]

        # Check if this part is a known lookup (only valid as last part)
        if part in lookups and i == len(parts) - 1:
            lookup_name = part
            break

        # Check if this part is a known transform
        if part in xforms:
            transforms.append(xforms[part])
            i += 1
            continue

        # Otherwise it's a column name or FK path segment
        col_parts.append(part)
        i += 1

    # Build the column reference
    if not col_parts:
        raise ValueError(f"Cannot resolve filter key: {key!r}")

    col_name = "__".join(col_parts)

    # Qualify column with table alias
    col_sql = _qualify_column(
        col_name, table_alias, join_aliases or {}, annotation_aliases or set()
    )

    # Apply transforms
    for transform in transforms:
        col_sql = transform.as_sql(col_sql)

    # F()/expression RHS → inline column reference (not a bound value).
    if _is_expression(value):
        op = _F_RHS_OPS.get(lookup_name)
        if op is None:
            raise NotImplementedError(
                f"F()/expression on the right-hand side of a filter is only "
                f"supported for comparison lookups {sorted(_F_RHS_OPS)}, "
                f"not {lookup_name!r}."
            )
        rhs_sql, rhs_params = value.as_sql(param_idx - 1)
        return f"{col_sql} {op} {rhs_sql}", rhs_params

    # __in with a QuerySet/Subquery → col IN (SELECT ...) subselect.
    if lookup_name == "in" and _is_subquery_value(value):
        if hasattr(value, "_build_select"):
            sub_sql, sub_params = value._build_select()
        else:
            sub_sql, sub_params = value.queryset._build_select()

        def _shift(m, offset=param_idx - 1):
            return f"${int(m.group(1)) + offset}"

        sub_sql = re.sub(r"\$(\d+)", _shift, sub_sql)
        return f"{col_sql} IN ({sub_sql})", list(sub_params)

    # Apply lookup
    lookup = lookups[lookup_name]
    condition_sql, params = lookup.as_sql(col_sql, param_idx, value)

    return condition_sql, params


def resolve_exclude(
    key: str,
    value: Any,
    param_idx: int = 1,
    table_alias: str | None = None,
    join_aliases: dict[str, str] | None = None,
    annotation_aliases: set[str] | None = None,
    nullable_columns: set[str] | None = None,
) -> tuple[str, list[Any]]:
    """Like resolve_lookup but wraps in NOT(...), with Django-style NULL demotion.

    A negated value comparison on a NULLABLE column emits
    ``(NOT (<cond>) OR <col> IS NULL)`` so NULL rows survive the exclude
    (matching ``resolve_negated_node``). Demotion applies only when the column
    is in ``nullable_columns``; with no set (direct call, no model context) the
    condition is negated directly. ``isnull`` / NULL-aware ``in`` / F()-RHS /
    subquery lookups are always negated directly.
    """
    condition_sql, params = resolve_lookup(
        key,
        value,
        param_idx,
        table_alias,
        join_aliases,
        annotation_aliases,
    )

    if nullable_columns:
        try:
            col_name, col_base, _, lookup_name, _ = _parse_lookup(
                key, table_alias, join_aliases, annotation_aliases
            )
        except ValueError:
            col_name = col_base = None
            lookup_name = "exact"
        if (
            col_name in nullable_columns
            and not _skip_null_demotion(lookup_name, value)
            and not _is_expression(value)
            and not (lookup_name == "in" and _is_subquery_value(value))
        ):
            return f"(NOT ({condition_sql}) OR {col_base} IS NULL)", params

    return f"NOT ({condition_sql})", params


def _parse_lookup(
    key: str,
    table_alias: str | None,
    join_aliases: dict[str, str] | None,
    annotation_aliases: set[str] | None,
) -> tuple[str, str, str, str, Lookup]:
    """Parse a filter key into its column and lookup components.

    Returns ``(col_name, col_base, col_sql, lookup_name, lookup)`` where:
      - ``col_name`` is the raw, unqualified column path (e.g. ``country``) —
        used to test membership in the nullable-columns set.
      - ``col_base`` is the qualified column BEFORE any transforms — used as
        the target of the ``IS NULL`` demotion in exclude()/~Q().
      - ``col_sql`` is the column AFTER transforms (e.g. ``LOWER(col)``).
      - ``lookup_name`` / ``lookup`` identify the terminal comparison.
    """
    parts = key.split("__")
    lookups, xforms = _get_registry_snapshots()

    col_parts: list[str] = []
    transforms: list[Transform] = []
    lookup_name = "exact"

    i = 0
    while i < len(parts):
        part = parts[i]
        if part in lookups and i == len(parts) - 1:
            lookup_name = part
            break
        if part in xforms:
            transforms.append(xforms[part])
            i += 1
            continue
        col_parts.append(part)
        i += 1

    if not col_parts:
        raise ValueError(f"Cannot resolve filter key: {key!r}")

    col_name = "__".join(col_parts)
    col_base = _qualify_column(
        col_name, table_alias, join_aliases or {}, annotation_aliases or set()
    )
    col_sql = col_base
    for transform in transforms:
        col_sql = transform.as_sql(col_sql)

    return col_name, col_base, col_sql, lookup_name, lookups[lookup_name]


def _is_expression(value: Any) -> bool:
    """True if ``value`` is an ORM Expression (F, CombinedExpression, ...).

    Lazy import avoids a circular dependency (expressions imports lookups).
    """
    from hyperdjango.expressions import Expression, Subquery

    # Subquery is an Expression but is handled by the __in subquery path, not
    # the F()-inline path.
    return isinstance(value, Expression) and not isinstance(value, Subquery)


def _is_subquery_value(value: Any) -> bool:
    """True if ``value`` is a QuerySet or Subquery usable as ``col IN (SELECT ...)``."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return False
    from hyperdjango.expressions import Subquery

    if isinstance(value, Subquery):
        return True
    # Duck-type a QuerySet without importing query.py (circular).
    return hasattr(value, "_build_select")


def _render_expression_rhs(
    value: Any,
    table_alias: str | None,
    join_aliases: dict[str, str] | None,
) -> tuple[str, list[Any]]:
    """Render an F()/expression right-hand side as SQL + bind values.

    A bare F() is qualified against the current table/join aliases so it
    resolves to a real column reference. Composite expressions are rendered
    via ``as_sql`` with ``$SENTINEL`` markers converted to ``{}`` placeholders.
    """
    from hyperdjango.expressions import F

    if isinstance(value, F):
        col = _qualify_column(value.name, table_alias, join_aliases or {}, set())
        return col, []

    sql, params = value.as_sql(SENTINEL_IDX)
    for i in range(len(params)):
        sql = sql.replace(f"${SENTINEL_IDX + i}", "{}", 1)
    return sql, params


def _expression_rhs_node(
    col_sql: str,
    lookup_name: str,
    value: Any,
    table_alias: str | None,
    join_aliases: dict[str, str] | None,
) -> WhereNode:
    """Build a WhereNode comparing ``col_sql`` to an F()/expression RHS."""
    op = _F_RHS_OPS.get(lookup_name)
    if op is None:
        raise NotImplementedError(
            f"F()/expression on the right-hand side of a filter is only "
            f"supported for comparison lookups {sorted(_F_RHS_OPS)}, "
            f"not {lookup_name!r}."
        )
    rhs_sql, rhs_params = _render_expression_rhs(value, table_alias, join_aliases)
    return WhereNode(template=f"{col_sql} {op} {rhs_sql}", bind_values=rhs_params)


def _in_subquery_node(col_sql: str, value: Any) -> WhereNode:
    """Build ``col IN (SELECT ...)`` from a QuerySet/Subquery ``__in`` value."""
    if hasattr(value, "_build_select"):
        sub_sql, sub_params = value._build_select()
    else:  # Subquery
        sub_sql, sub_params = value.queryset._build_select()
    # Convert the subquery's $N markers to {} placeholders so the outer
    # WhereNode.compile() renumbers them contiguously with the outer params.
    template = re.sub(r"\$\d+", "{}", sub_sql)
    return WhereNode(
        template=f"{col_sql} IN ({template})", bind_values=list(sub_params)
    )


def resolve_lookup_node(
    key: str,
    value: Any,
    table_alias: str | None = None,
    join_aliases: dict[str, str] | None = None,
    annotation_aliases: set[str] | None = None,
) -> WhereNode:
    """Resolve a filter key into a WhereNode (no param indexing needed).

    Like resolve_lookup() but produces a WhereNode instead of (sql, params).
    The WhereNode holds a template with {} placeholders and bind values.
    Param indexing ($N) happens later during WhereNode.compile().

    Handles F()/expression right-hand sides (rendered inline as a column
    reference) and ``__in`` values that are a QuerySet/Subquery (compiled to
    a ``col IN (SELECT ...)`` subselect).
    """
    _col_name, _col_base, col_sql, lookup_name, lookup = _parse_lookup(
        key, table_alias, join_aliases, annotation_aliases
    )

    if lookup_name == "in" and _is_subquery_value(value):
        return _in_subquery_node(col_sql, value)

    if _is_expression(value):
        return _expression_rhs_node(
            col_sql, lookup_name, value, table_alias, join_aliases
        )

    return lookup.to_node(col_sql, value)


def _skip_null_demotion(lookup_name: str, value: Any) -> bool:
    """True when negating this leaf must NOT add an ``OR col IS NULL`` clause.

    Demotion re-includes NULL rows that 3-valued logic would drop from a bare
    ``NOT (...)``. It is WRONG when the positive predicate is already TRUE (not
    UNKNOWN) for NULL rows, i.e.:
      - ``isnull`` — negation is a complete IS NULL / IS NOT NULL.
      - ``exact`` with value ``None`` — this compiles to ``col IS NULL``.
      - ``in`` with an empty list (``FALSE``) or a list containing ``None``
        (already ``... OR col IS NULL``).
    """
    if lookup_name == "isnull":
        return True
    if lookup_name == "exact" and value is None:
        return True
    if lookup_name == "in":
        items = list(value) if isinstance(value, (list, tuple, set, frozenset)) else []
        if not items or None in items:
            return True
    return False


def resolve_negated_node(
    key: str,
    value: Any,
    table_alias: str | None = None,
    join_aliases: dict[str, str] | None = None,
    annotation_aliases: set[str] | None = None,
    nullable_columns: set[str] | None = None,
) -> WhereNode:
    """Resolve a single negated leaf (exclude()/~Q()) into a WhereNode.

    Mirrors Django's isnull demotion: a negated value comparison on a NULLABLE
    column emits ``(NOT (<cond>) OR <col> IS NULL)`` so that rows where the
    column is NULL are still returned (Postgres 3-valued logic would otherwise
    drop them from a bare ``NOT (...)``).

    Demotion applies only when ``col_name`` is in ``nullable_columns``; when the
    set is None (direct call with no model context) or the column is known
    non-nullable, the leaf is negated directly. ``isnull`` lookups and NULL-aware
    ``in`` lists are also negated directly (see ``_skip_null_demotion``).
    """
    col_name, col_base, col_sql, lookup_name, lookup = _parse_lookup(
        key, table_alias, join_aliases, annotation_aliases
    )

    # Subquery __in / F()-expression RHS: negate directly, no NULL demotion
    # (their NULL semantics are not a simple column-IS-NULL re-inclusion).
    if lookup_name == "in" and _is_subquery_value(value):
        node = _in_subquery_node(col_sql, value)
        node.negated = True
        return node
    if _is_expression(value):
        node = _expression_rhs_node(
            col_sql, lookup_name, value, table_alias, join_aliases
        )
        node.negated = True
        return node

    positive = lookup.to_node(col_sql, value)

    demote = (
        nullable_columns is not None
        and col_name in nullable_columns
        and not _skip_null_demotion(lookup_name, value)
    )
    if not demote:
        positive.negated = True
        return positive

    positive.negated = True
    null_leaf = WhereNode(template=f"{col_base} IS NULL")
    return WhereNode(connector="OR", children=[positive, null_leaf])


def resolve_exclude_node(
    key: str,
    value: Any,
    table_alias: str | None = None,
    join_aliases: dict[str, str] | None = None,
    annotation_aliases: set[str] | None = None,
    nullable_columns: set[str] | None = None,
) -> WhereNode:
    """Like resolve_lookup_node but negated (with NULL demotion) for exclude()."""
    return resolve_negated_node(
        key, value, table_alias, join_aliases, annotation_aliases, nullable_columns
    )


def resolve_bind_params(key: str, value: Any) -> list[Any]:
    """Extract bind parameter values for a filter key WITHOUT building SQL.

    Fast path for compiled query cache hits — skips all column qualification,
    transform application, and SQL string formatting. Only resolves which
    Lookup class handles this key and calls its bind_params() method.

    Uses rfind to extract lookup name without allocating a split list.
    """
    lookups, _ = _get_registry_snapshots()

    # Find lookup name: last segment after final "__" if it's a known lookup
    sep = key.rfind("__")
    if sep >= 0:
        suffix = key[sep + 2 :]
        if suffix in lookups:
            return lookups[suffix].bind_params(value)

    return lookups["exact"].bind_params(value)


# ---------------------------------------------------------------------------
# Column qualification helper
# ---------------------------------------------------------------------------


def _validate_column_path(col: str) -> None:
    """Reject SQL-unsafe filter/exclude/Q column references (SQL-injection gate).

    Every filter key (``field__lookup``) is split on ``__``; resolve_lookup /
    _parse_lookup strip the recognized lookup/transform suffixes, so only the
    FIELD-PATH portion (e.g. ``author__name`` from ``author__name__icontains``)
    reaches _qualify_column. Each ``__``-separated segment must be a bare
    identifier — letters, digits, or underscore — with no whitespace, quotes,
    parens, operators, comment markers (``--`` / ``/*``), ``;``, ``,``, ``|`` or
    ``*``. This is the SINGLE choke point through which filter(), exclude() AND
    Q() column references flow (via resolve_lookup / _parse_lookup / the *_node
    resolvers), so validating here blocks a crafted key such as
    ``filter(**{"id IS NULL OR 1=1 --__isnull": True})`` from ever reaching SQL,
    while leaving legitimate multi-segment lookups (``author__name__icontains``,
    ``created__year__gte``, ``data__key``) untouched.
    """

    validate_column_path(col, source="filter")


def _qualify_column(
    col: str,
    table_alias: str | None,
    join_aliases: dict[str, str],
    annotation_aliases: set[str],
) -> str:
    """Qualify a column name with the correct table alias.

    Handles FK-spanning paths like "author__name" -> "t1.name"
    """
    # SQL-injection gate: validate every user-supplied field path before it is
    # interpolated raw into SQL. Registered annotation aliases are exempt — they
    # were already validated at annotate()/alias() time and are legitimate
    # filter targets (HAVING) that need no re-check.
    if col not in annotation_aliases:
        _validate_column_path(col)

    if not table_alias:
        return col

    # Don't qualify annotation aliases
    if col in annotation_aliases:
        return col

    if "__" in col:
        # Check if it's a FK path: author__name -> t1.name
        parts = col.split("__")
        # Try progressively longer prefixes
        for i in range(len(parts) - 1, 0, -1):
            prefix = "__".join(parts[:i])
            if prefix in join_aliases:
                remainder = parts[i]
                return f"{join_aliases[prefix]}.{remainder}"
        # Not a join path — treat as plain column
        return f"{table_alias}.{col}"
    else:
        return f"{table_alias}.{col}"


# ---------------------------------------------------------------------------
# Public API for listing available lookups/transforms
# ---------------------------------------------------------------------------


def get_lookup(name: str) -> Lookup | None:
    """Get a registered lookup by name."""
    with _lookup_registry_lock:
        return _lookup_registry.get(name)


def get_transform(name: str) -> Transform | None:
    """Get a registered transform by name."""
    with _lookup_registry_lock:
        return _transform_registry.get(name)


def list_lookups() -> list[str]:
    """List all registered lookup names."""
    with _lookup_registry_lock:
        return sorted(_lookup_registry.keys())


def list_transforms() -> list[str]:
    """List all registered transform names."""
    with _lookup_registry_lock:
        return sorted(_transform_registry.keys())
