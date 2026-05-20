"""
SQL expressions for the standalone ORM.

Provides F(), Value(), Q(), and aggregate functions (Count, Sum, Avg, Max, Min, StdDev, Variance)
that generate parameterized PostgreSQL SQL.

Usage:
    from hyperdjango.expressions import F, Value, Count, Sum, Avg, Max, Min, Q

    # F expressions reference columns
    qs.annotate(upper_price=F("price") * 1.1)
    qs.filter(stock__gt=F("reorder_level"))

    # Q objects for complex conditions (OR, AND, NOT)
    qs.filter(Q(name="alice") | Q(name="bob"))
    qs.filter(Q(age__gte=18) & ~Q(is_banned=True))
    qs.filter(Q(title__contains="python") | Q(category="python"), published=True)

    # Aggregates
    qs.annotate(num_books=Count("id"))
    qs.aggregate(total=Sum("price"), avg_price=Avg("price"))

    # Combined expressions
    qs.annotate(profit=F("revenue") - F("cost"))
"""

import re
from dataclasses import dataclass
from typing import Any

from hyperdjango.lookups import resolve_bind_params as _resolve_bind_params
from hyperdjango.lookups import resolve_lookup as _resolve_lookup
from hyperdjango.lookups import resolve_lookup_node as _resolve_lookup_node
from hyperdjango.lookups import resolve_negated_node as _resolve_negated_node
from hyperdjango.sqlident import (
    validate_column_path,
    validate_qualified_column,
    validate_type,
)
from hyperdjango.where import PASSTHROUGH_SUFFIXES, WhereNode
from hyperdjango.where import value_shape as _value_shape


class Expression:
    """Base class for SQL expressions."""

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        """Generate SQL string and parameter list."""
        raise NotImplementedError("Subclass must implement this method")

    @property
    def default_alias(self) -> str:
        """Default column alias when used without explicit name."""
        raise NotImplementedError("Subclass must implement this method")

    @property
    def contains_aggregate(self) -> bool:
        """True if this expression contains an aggregate function."""
        return False

    def __add__(self, other):
        return CombinedExpression(self, "+", _wrap(other))

    def __radd__(self, other):
        return CombinedExpression(_wrap(other), "+", self)

    def __sub__(self, other):
        return CombinedExpression(self, "-", _wrap(other))

    def __rsub__(self, other):
        return CombinedExpression(_wrap(other), "-", self)

    def __mul__(self, other):
        return CombinedExpression(self, "*", _wrap(other))

    def __rmul__(self, other):
        return CombinedExpression(_wrap(other), "*", self)

    def __truediv__(self, other):
        return CombinedExpression(self, "/", _wrap(other))

    def __rtruediv__(self, other):
        return CombinedExpression(_wrap(other), "/", self)

    def __mod__(self, other):
        return CombinedExpression(self, "%", _wrap(other))

    def __neg__(self):
        return NegatedExpression(self)


def _wrap(val: Any) -> Expression:
    """Wrap a raw Python value as a Value expression."""
    if isinstance(val, Expression):
        return val
    return Value(val)


@dataclass(slots=True)
class F(Expression):
    """Reference to a database column."""

    name: str

    def __post_init__(self):
        # F.name is emitted raw as a column reference — validate it as a column
        # identifier path so an injection string can never reach SQL text.
        validate_column_path(self.name, source="F")

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        return self.name, []

    @property
    def default_alias(self) -> str:
        return self.name.replace("__", "_")

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class Value(Expression):
    """A literal SQL value."""

    value: Any

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        if self.value is None:
            return "NULL", []
        return f"${param_offset + 1}", [self.value]

    @property
    def default_alias(self) -> str:
        return "value"

    @property
    def contains_aggregate(self) -> bool:
        return False


@dataclass(slots=True)
class CombinedExpression(Expression):
    """Two expressions combined with an operator."""

    lhs: Expression
    operator: str
    rhs: Expression

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        lhs_sql, lhs_params = self.lhs.as_sql(param_offset)
        rhs_sql, rhs_params = self.rhs.as_sql(param_offset + len(lhs_params))
        sql = f"({lhs_sql} {self.operator} {rhs_sql})"
        return sql, lhs_params + rhs_params

    @property
    def default_alias(self) -> str:
        return f"{self.lhs.default_alias}_{self.operator}_{self.rhs.default_alias}"

    @property
    def contains_aggregate(self) -> bool:
        return self.lhs.contains_aggregate or self.rhs.contains_aggregate


class NegatedExpression(Expression):
    """Unary negation: -expr -> (-expr)."""

    def __init__(self, expression: Expression):
        self.expression = expression

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        inner_sql, params = self.expression.as_sql(param_offset)
        return f"(-{inner_sql})", params

    @property
    def default_alias(self) -> str:
        return f"neg_{self.expression.default_alias}"

    @property
    def contains_aggregate(self) -> bool:
        return self.expression.contains_aggregate


def _validate_agg_identifier(name: str, *, source: str) -> None:
    """Reject SQL-unsafe raw identifiers interpolated into aggregate SQL.

    The aggregated column string and FILTER-clause keys are interpolated raw
    into the emitted SQL (unlike Expression operands, which are parameterized).
    They may be QUALIFIED (``table.column``), so validate dot-part-by-part via
    the shared identifier authority — not as a bare alias.
    """
    validate_qualified_column(name, source=source)


class Aggregate(Expression):
    """Base class for SQL aggregate functions.

    Not a dataclass — subclasses set class-level attributes.

    Column-collision safety (why no alias-vs-column rejection lives here):
    an Aggregate never binds its OWN top-level alias. Its ``expression`` operand
    is rendered strictly INSIDE the function call (``COUNT(<col>)``), so it can
    only reference a column, never shadow one in the SELECT list. The alias is
    supplied by the caller: ``annotate(alias=Agg(...))`` already rejects an alias
    equal to a model column (query.py — a bare column plus ``AS alias`` would
    double-emit and clobber the real value), and ``aggregate(alias=Agg(...))``
    builds a SELECT of ONLY the aggregate expressions and returns a plain dict of
    those alias→value pairs (no model-column hydration), so a collision has
    nothing to clobber. Hence collision handling belongs at those call sites, not
    in ``as_sql`` — which only guards raw-identifier SQL-safety below.
    """

    function: str = ""
    allow_distinct: bool = False
    empty_result_set_value: Any = None

    def __init__(
        self,
        expression: str | Expression,
        *,
        distinct: bool = False,
        filter_expr: dict | None = None,
    ):
        self.expression = expression
        self.distinct = distinct
        self.filter_expr = filter_expr

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        if isinstance(self.expression, Expression):
            inner_sql, params = self.expression.as_sql(param_offset)
        elif self.expression == "*":
            inner_sql, params = "*", []
        else:
            # Plain column identifier interpolated RAW into the SQL — validate it
            # against the same forbidden-char set as annotate/alias kwargs so a
            # user-controlled aggregate column can't smuggle SQL. (Expression
            # operands above are parameterized/recursively validated instead.)
            _validate_agg_identifier(self.expression, source="aggregate column")
            inner_sql, params = self.expression, []

        distinct_str = "DISTINCT " if self.distinct else ""
        sql = f"{self.function}({distinct_str}{inner_sql})"

        if self.filter_expr:
            filter_parts = []
            for key, value in self.filter_expr.items():
                # FILTER (WHERE key = $N): the key (column) is interpolated raw —
                # validate it too; the value is safely bound as $N.
                _validate_agg_identifier(key, source="aggregate filter_expr key")
                params.append(value)
                filter_parts.append(f"{key} = ${param_offset + len(params)}")
            sql += f" FILTER (WHERE {' AND '.join(filter_parts)})"

        return sql, params

    @property
    def default_alias(self) -> str:
        if isinstance(self.expression, Expression):
            col = self.expression.default_alias
        elif self.expression == "*":
            col = "all"
        else:
            col = self.expression.replace(".", "_")
        return f"{col}__{self.function.lower()}"

    @property
    def contains_aggregate(self) -> bool:
        return True


class Count(Aggregate):
    """COUNT aggregate."""

    function = "COUNT"
    allow_distinct = True
    empty_result_set_value = 0


class Sum(Aggregate):
    """SUM aggregate."""

    function = "SUM"
    allow_distinct = True


class Avg(Aggregate):
    """AVG aggregate."""

    function = "AVG"
    allow_distinct = True


class Max(Aggregate):
    """MAX aggregate."""

    function = "MAX"


class Min(Aggregate):
    """MIN aggregate."""

    function = "MIN"


class StdDev(Aggregate):
    """STDDEV aggregate."""

    function = "STDDEV"


class Variance(Aggregate):
    """VARIANCE aggregate."""

    function = "VARIANCE"


class Coalesce(Expression):
    """COALESCE function — returns first non-NULL argument."""

    def __init__(self, *expressions: Expression | Any):
        self.expressions = [_wrap(e) for e in expressions]

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        parts = []
        all_params = []
        offset = param_offset
        for expr in self.expressions:
            sql, params = expr.as_sql(offset)
            parts.append(sql)
            all_params.extend(params)
            offset += len(params)
        return f"COALESCE({', '.join(parts)})", all_params

    @property
    def default_alias(self) -> str:
        return "coalesce"

    @property
    def contains_aggregate(self) -> bool:
        return any(e.contains_aggregate for e in self.expressions)


# Allowlisted base SQL/Postgres type names for Cast(..., output_type=...).
def _validate_cast_type(output_type: str) -> None:
    """Reject a Cast output_type that isn't a recognized SQL type (injection gate)."""

    validate_type(output_type, source="Cast")


class Cast(Expression):
    """CAST expression — type conversion."""

    def __init__(self, expression: Expression | Any, output_type: str):
        _validate_cast_type(output_type)
        self.expression = _wrap(expression)
        self.output_type = output_type

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        inner_sql, params = self.expression.as_sql(param_offset)
        return f"CAST({inner_sql} AS {self.output_type})", params

    @property
    def default_alias(self) -> str:
        return (
            f"{self.expression.default_alias}__{self.output_type.split('(')[0].lower()}"
        )

    @property
    def contains_aggregate(self) -> bool:
        return self.expression.contains_aggregate


class Case(Expression):
    """CASE/WHEN SQL expression."""

    def __init__(self, *whens: When, default: Expression | Any = None):
        self.whens = list(whens)
        self.default = _wrap(default) if default is not None else None

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        parts = ["CASE"]
        all_params = []
        offset = param_offset

        for when in self.whens:
            when_sql, when_params = when.as_sql(offset)
            parts.append(when_sql)
            all_params.extend(when_params)
            offset += len(when_params)

        if self.default is not None:
            default_sql, default_params = self.default.as_sql(offset)
            parts.append(f"ELSE {default_sql}")
            all_params.extend(default_params)

        parts.append("END")
        return " ".join(parts), all_params

    @property
    def default_alias(self) -> str:
        return "case"

    @property
    def contains_aggregate(self) -> bool:
        agg = any(w.then.contains_aggregate for w in self.whens)
        if self.default:
            agg = agg or self.default.contains_aggregate
        return agg


class When:
    """A single WHEN clause for a CASE expression."""

    def __init__(self, then: Expression | Any = None, **condition):
        # A When() with no conditions silently becomes `WHEN TRUE THEN ...`
        # which is almost always a bug. Require at least one lookup kwarg so
        # misuse fails at construction, not at SQL compile.
        if not condition:
            raise TypeError(
                "When() requires at least one lookup keyword argument "
                "(e.g. When(field__exact=x, then=...))"
            )
        self.condition = condition
        self.then = _wrap(then) if then is not None else Value(None)

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        all_params = []
        offset = param_offset
        cond_parts = []

        for key, value in self.condition.items():
            # Delegate to the full lookup registry for correct SQL generation
            # This handles all lookups: exact, gt, gte, lt, lte, contains,
            # icontains, startswith, endswith, in, range, isnull, regex, etc.
            param_idx = offset + len(all_params) + 1
            condition_sql, new_params = _resolve_when_lookup(key, value, param_idx)
            all_params.extend(new_params)
            cond_parts.append(condition_sql)

        then_sql, then_params = self.then.as_sql(offset + len(all_params))
        all_params.extend(then_params)

        condition_sql = " AND ".join(cond_parts) if cond_parts else "TRUE"
        return f"WHEN {condition_sql} THEN {then_sql}", all_params


def _resolve_when_lookup(key: str, value: Any, param_idx: int) -> tuple[str, list[Any]]:
    """Resolve a When condition using the full lookup registry.

    Supports all registered lookups and transforms, matching QuerySet.filter() behavior.
    """
    return _resolve_lookup(key, value, param_idx)


class Subquery(Expression):
    """Wrap a QuerySet as a subquery expression."""

    def __init__(self, queryset):
        self.queryset = queryset

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        sql, params = self.queryset._build_select()
        if param_offset > 0 and params:

            def _rewrite(m):
                n = int(m.group(1))
                return f"${n + param_offset}"

            sql = re.sub(r"\$(\d+)", _rewrite, sql)
        return f"({sql})", params

    @property
    def default_alias(self) -> str:
        return "subquery"

    @property
    def contains_aggregate(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Correlated subquery expressions — Exists / NotExists / OuterRef
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OuterRef:
    """Placeholder for a reference to the outer query's column in a
    correlated subquery.

    Used inside an `Exists(...)` / `NotExists(...)` inner queryset to
    reference a column on the outer query's table. The outer table
    name is resolved at outer-query compile time:

        # "Forums that have no active 'hidden' status event"
        Forum.objects.exclude(
            Exists(
                StatusEvent.objects.filter(
                    entity_type="forum",
                    entity_id=OuterRef("id"),   # → hn_forums.id
                    status="hidden",
                    ended_at=None,
                )
            )
        )

    Only valid as a filter value inside an inner queryset that is
    wrapped in Exists/NotExists. Using it anywhere else raises
    ``TypeError`` at compile time.
    """

    field: str

    def __repr__(self) -> str:
        return f"OuterRef({self.field!r})"


class Exists(Expression):
    """Correlated `EXISTS (subquery)` expression for filter/exclude.

    Wraps a QuerySet as a correlated EXISTS clause. Use inside
    ``.filter(...)`` to require the subquery return at least one row,
    or inside ``.exclude(...)`` for `NOT EXISTS` semantics. The inner
    queryset may reference the outer query's columns via ``OuterRef``.

    Example — "forums that have at least one active post"::

        Forum.objects.filter(
            Exists(
                Post.objects.filter(
                    forum_id=OuterRef("id"),
                    is_deleted=False,
                )
            )
        )

    Example — "forums that do NOT have an active 'hidden' status"::

        Forum.objects.exclude(
            Exists(
                StatusEvent.objects.filter(
                    entity_type="forum",
                    entity_id=OuterRef("id"),
                    status="hidden",
                    ended_at=None,
                )
            )
        )

    Compilation is handled by ``QuerySet._compile_exists_filter``
    at the outer query's ``_build_where_tree`` time, where the
    outer table name is known and OuterRef values can be resolved
    to fully-qualified column references.
    """

    def __init__(self, queryset):
        self.queryset = queryset

    def __invert__(self) -> NotExists:
        return NotExists(self.queryset)

    @property
    def default_alias(self) -> str:
        return "exists"

    @property
    def contains_aggregate(self) -> bool:
        return False

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        # Direct as_sql() without outer-table context is only valid
        # when no OuterRef values are present in the inner query.
        # The outer-query compile path calls _compile_with_outer()
        # instead, which handles OuterRef substitution.
        sql, params = self.queryset._build_select()
        if param_offset > 0 and params:

            def _rewrite(m):
                n = int(m.group(1))
                return f"${n + param_offset}"

            sql = re.sub(r"\$(\d+)", _rewrite, sql)
        return f"EXISTS ({sql})", params


class NotExists(Exists):
    """`NOT EXISTS (subquery)` — the negated counterpart of ``Exists``.

    Usually constructed via ``~Exists(...)`` or by passing an
    ``Exists`` to ``.exclude()``. Both forms produce identical SQL.
    """

    def __invert__(self) -> Exists:
        return Exists(self.queryset)

    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        sql, params = super().as_sql(param_offset)
        # super() returned "EXISTS (...)" — swap to "NOT EXISTS (...)"
        return "NOT " + sql, params


# ---------------------------------------------------------------------------
# Q objects — composable AND / OR / NOT conditions
# ---------------------------------------------------------------------------

_AND = "AND"
_OR = "OR"
_XOR = "XOR"
_VALID_CONNECTORS = frozenset({_AND, _OR, _XOR})


class Q:
    """Composable query condition for AND, OR, and NOT combinations.

    Usage:
        Q(name="alice")                           # Simple filter
        Q(name="alice") | Q(name="bob")           # OR
        Q(age__gte=18) & Q(is_active=True)        # AND
        ~Q(is_banned=True)                        # NOT
        (Q(a=1) | Q(b=2)) & Q(c=3)               # Nested
        Q(author__name__icontains="alice")        # FK spanning

    In QuerySet.filter():
        qs.filter(Q(name="a") | Q(name="b"))
        qs.filter(Q(a=1) | Q(b=2), c=3)  # positional Q + keyword AND
    """

    def __init__(
        self, *args: Q, _connector: str = _AND, _negated: bool = False, **kwargs: Any
    ):
        # Whitelist _connector to prevent arbitrary SQL injection via Q kwargs.
        if _connector not in _VALID_CONNECTORS:
            raise ValueError(
                f"Q connector must be one of {sorted(_VALID_CONNECTORS)}, got {_connector!r}"
            )
        self.children: list[Q | tuple[str, Any]] = []
        self.connector = _connector
        self.negated = _negated

        # Positional Q args are child nodes
        for arg in args:
            if not isinstance(arg, Q):
                raise TypeError(
                    f"Positional arguments to Q() must be Q instances, got {type(arg).__name__}"
                )
            self.children.append(arg)

        # Keyword args become leaf conditions
        for key, value in kwargs.items():
            self.children.append((key, value))

    def __or__(self, other: Q) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        node = Q(_connector=_OR)
        node.children = [self, other]
        return node

    def __and__(self, other: Q) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        node = Q(_connector=_AND)
        node.children = [self, other]
        return node

    def __invert__(self) -> Q:
        node = Q(_connector=self.connector, _negated=not self.negated)
        node.children = list(self.children)
        return node

    def __repr__(self) -> str:
        prefix = "NOT " if self.negated else ""
        parts = []
        for child in self.children:
            if isinstance(child, Q):
                parts.append(repr(child))
            else:
                parts.append(f"{child[0]}={child[1]!r}")
        return f"{prefix}Q({f' {self.connector} '.join(parts)})"

    def resolve(
        self,
        params: list[Any],
        table_alias: str | None = None,
        join_aliases: dict[str, str] | None = None,
        annotation_aliases: set[str] | None = None,
    ) -> str:
        """Resolve this Q tree into a SQL WHERE fragment.

        Appends parameter values to the provided params list and returns
        the SQL condition string with $N placeholders.

        Args:
            params: Mutable list to append parameter values to
            table_alias: Main table alias for column qualification
            join_aliases: FK path → table alias mapping
            annotation_aliases: Set of annotation alias names

        Returns:
            SQL condition string (e.g., "(name = $1 OR age > $2)")
        """
        if not self.children:
            return ""

        parts: list[str] = []

        for child in self.children:
            if isinstance(child, Q):
                # Recursive: resolve child Q node
                child_sql = child.resolve(
                    params, table_alias, join_aliases, annotation_aliases
                )
                if child_sql:
                    parts.append(child_sql)
            else:
                # Leaf: (key, value) tuple → resolve via lookup system
                key, value = child
                param_idx = len(params) + 1
                condition_sql, new_params = _resolve_lookup(
                    key,
                    value,
                    param_idx,
                    table_alias,
                    join_aliases,
                    annotation_aliases,
                )
                params.extend(new_params)
                parts.append(condition_sql)

        if not parts:
            return ""

        sql = parts[0] if len(parts) == 1 else f"({f' {self.connector} '.join(parts)})"

        if self.negated:
            sql = f"NOT ({sql})"

        return sql

    def to_node(
        self,
        table_alias: str | None = None,
        join_aliases: dict[str, str] | None = None,
        annotation_aliases: set[str] | None = None,
        nullable_columns: set[str] | None = None,
    ) -> WhereNode:
        """Convert this Q tree into a WhereNode tree.

        Like resolve() but produces composable WhereNodes instead of SQL strings.
        No param indexing needed — that happens during WhereNode.compile().

        ``nullable_columns`` is the set of the model's nullable column names; it
        drives the exclude()/~Q() NULL demotion (see below). When omitted (a
        direct call with no model context), no demotion is applied.
        """
        if not self.children:
            return WhereNode()

        # A negated Q that reduces to a SINGLE leaf condition gets Django-style
        # NULL demotion: `(NOT (<cond>) OR <col> IS NULL)`. This makes
        # `~Q(col=x)` / `exclude(col=x)` on a NULLABLE column still return rows
        # where the column IS NULL (Postgres 3-valued logic). Multi-leaf groups
        # negate as a whole — `NOT(a AND b)` — with no per-leaf demotion, so
        # `~Q(a=1, b=2)` matches raw `NOT(a=1 AND b=2)`.
        if (
            self.negated
            and len(self.children) == 1
            and not isinstance(self.children[0], Q)
        ):
            key, value = self.children[0]
            return _resolve_negated_node(
                key,
                value,
                table_alias,
                join_aliases,
                annotation_aliases,
                nullable_columns,
            )

        node = WhereNode(connector=self.connector, negated=self.negated)

        for child in self.children:
            if isinstance(child, Q):
                child_node = child.to_node(
                    table_alias, join_aliases, annotation_aliases, nullable_columns
                )
                if not child_node.is_empty:
                    node.children.append(child_node)
            else:
                key, value = child
                child_node = _resolve_lookup_node(
                    key, value, table_alias, join_aliases, annotation_aliases
                )
                node.children.append(child_node)

        return node

    def _structural_key(self) -> tuple:
        """Structural fingerprint for cache key — no tree allocation.

        Returns nested tuples of (connector, negated, children_keys) where
        leaf children contribute (key, value_shape) to distinguish None/True/False/empty.
        """
        return (
            self.connector,
            self.negated,
            tuple(
                child._structural_key()
                if isinstance(child, Q)
                else (child[0], _value_shape(child[1]))
                for child in self.children
            ),
        )

    _PASSTHROUGH_SUFFIXES = PASSTHROUGH_SUFFIXES

    def _collect_bind_params(self, target: list[object]) -> None:
        """Collect bind param values into target list — no tree allocation.

        Walks the Q tree and calls resolve_bind_params() for each leaf.
        Inlines passthrough lookups (exact non-None, gt, gte, lt, lte, etc.)
        as direct append — same optimization as QuerySet._collect_where_params().
        """
        _passthrough = self._PASSTHROUGH_SUFFIXES
        for child in self.children:
            if isinstance(child, Q):
                child._collect_bind_params(target)
            else:
                key, value = child
                if value is not None:
                    sep = key.rfind("__")
                    if sep < 0 or key[sep + 2 :] in _passthrough:
                        target.append(value)
                    else:
                        target.extend(_resolve_bind_params(key, value))
                else:
                    target.extend(_resolve_bind_params(key, value))
