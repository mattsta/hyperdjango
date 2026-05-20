"""
QuerySet — chainable query builder for Model.

Generates parameterized SQL and executes via the database layer.
No lazy evaluation — terminal methods (.all(), .get(), .first()) execute immediately.

No Django dependency.

Usage:
    users = await User.objects.filter(age__gte=18).order_by("-name").limit(10)
    user = await User.objects.get(id=1)
    await User.objects.create(name="Alice", email="alice@example.com")
    await User.objects.filter(id=1).update(name="Bob")
    await User.objects.filter(id=1).delete()

    # Relations:
    books = await Book.objects.select_related("author", "author__publisher").all()
    authors = await Author.objects.prefetch_related("books").all()

    # Aggregation:
    stats = await Book.objects.aggregate(total=Sum("price"), avg=Avg("price"))
    books = await Book.objects.annotate(num_orders=Count("id")).all()
"""

import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any

from hyperdjango._hyperdjango_native import (
    _where_cache_key as _native_where_hash,
)
from hyperdjango.database import get_db
from hyperdjango.expressions import Aggregate, Expression, Q
from hyperdjango.lookups import _qualify_column as _lookup_qualify_column
from hyperdjango.lookups import (
    key_is_value_dependent,
    resolve_bind_params,
    resolve_exclude_node,
    resolve_lookup_node,
)
from hyperdjango.multi_db import get_connections
from hyperdjango.query_cache import get_query_cache
from hyperdjango.sqlident import validate_identifier
from hyperdjango.validation.core.batch import validate_model_batch
from hyperdjango.where import PASSTHROUGH_SUFFIXES, BindValue, WhereNode
from hyperdjango.where import value_shape as _value_shape

# ---------------------------------------------------------------------------
# Model registry — populated by ModelMeta.__new__ in models.py
# Maps table_name -> model_class for FK resolution
# ---------------------------------------------------------------------------
_model_registry: dict[str, type] = {}


def _validate_alias_name(name: str, *, source: str) -> None:
    """Reject SQL-unsafe alias kwargs at construction.

    Used by annotate/aggregate/alias kwarg paths. Raises ValueError on any
    string containing SQL-control or ASCII/C1 control characters. Empty
    aliases are also rejected.
    """

    validate_identifier(name, kind="alias", source=source)


@dataclass(slots=True)
class CompiledQuery:
    """Typed debug view of a compiled ORM query (task #206).

    Returned by :meth:`QuerySet.to_sql`. Captures the exact SQL and
    parameter list the ORM would send to pg.zig — but without
    executing anything. Useful for:

    - Debugging unexpected `WHERE` compile behavior
    - Verifying `join_related` / `select_related` JOIN emission
    - Inspecting CTE prefix output (`with_cte`)
    - Understanding how `Exists` / `OuterRef` substitution resolved
    - Unit-testing query shape without needing a live DB
    - Copy-pasting the SQL into `psql` for `EXPLAIN ANALYZE`

    Use `str(compiled)` or `compiled.inlined()` to get a preview with
    parameter values substituted inline (for reading only — NEVER
    execute the inlined version, it's not safe against SQL injection
    if any value contains hostile content).
    """

    sql: str
    params: list[Any]
    kind: str  # "SELECT" | "UPDATE" | "DELETE"

    def __repr__(self) -> str:
        return (
            f"CompiledQuery(kind={self.kind!r}, "
            f"params={len(self.params)}, sql={self.sql[:60]!r}...)"
        )

    def __str__(self) -> str:
        """Human-readable dump: SQL + numbered params on separate lines."""
        lines = [f"-- {self.kind} ({len(self.params)} params)", self.sql]
        if self.params:
            lines.append("-- params:")
            for i, p in enumerate(self.params, 1):
                lines.append(f"--   ${i} = {p!r}")
        return "\n".join(lines)

    def inlined(self) -> str:
        """Return the SQL with `$N` placeholders replaced by literal
        Python repr of each param.

        **DANGER — read-only preview only.** Do NOT execute the returned
        string; it uses Python `repr()` for values which is not a
        safe SQL literal encoding. Use this only for debugging to get
        a copy-paste-friendly view of what the query LOOKS like when
        hydrated with real values.
        """
        out = self.sql
        # Replace `$N` markers in reverse order so `$10` is substituted
        # before `$1` (otherwise `$1` would partially match `$10`).
        for i in range(len(self.params), 0, -1):
            val = self.params[i - 1]
            if val is None:
                literal = "NULL"
            elif isinstance(val, bool):
                literal = "TRUE" if val else "FALSE"
            elif isinstance(val, (int, float)):
                literal = str(val)
            elif isinstance(val, (list, tuple)):
                literal = repr(list(val))
            else:
                literal = repr(val)
            out = out.replace(f"${i}", literal)
        return out


@dataclass(slots=True)
class _CTEClause:
    """A single WITH clause to prepend to a compiled SELECT.

    Task #197. Raw-SQL body for recursive CTEs that can't be expressed
    via pure ORM constructs (the recursive UNION ALL + self-join is
    inherently raw). Parameter placeholders use `{idx}` — one `{idx}`
    per entry in `params`, substituted left-to-right with consecutive
    `$N` indices at compile time.
    """

    name: str
    body_sql: str
    params: list[Any]
    recursive: bool = False


def _walk_q_for_outerref(q_obj, make_sentinel):
    """Walk a Q tree and replace OuterRef values with raw SQL sentinels.

    Returns a new Q with all OuterRef values substituted. Used by
    `QuerySet._compile_exists_filter` to support OuterRef inside Q
    subexpressions on the inner queryset of an Exists/NotExists
    correlated subquery.
    """
    from hyperdjango.expressions import OuterRef

    new_children = []
    for child in q_obj.children:
        if isinstance(child, Q):
            new_children.append(_walk_q_for_outerref(child, make_sentinel))
        else:
            # Q children are (key, value) tuples at the leaf level
            k, v = child
            if isinstance(v, OuterRef):
                new_children.append((k, make_sentinel(v.field)))
            else:
                new_children.append((k, v))
    new_q = Q()
    new_q.children = new_children
    new_q.connector = q_obj.connector
    new_q.negated = q_obj.negated
    return new_q


_model_registry_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Compiled SQL cache — stores structural fingerprint → SQL template
# WhereNode tree architecture separates template from values, enabling:
# - Cache HIT: skip SQL string assembly, just collect bind values
# - Cache MISS: compile tree → SQL + params, store in cache
# Thread-safe: reads are lock-free (dict.get), writes use lock
# ---------------------------------------------------------------------------
_compiled_sql_cache: dict[tuple | int, str] = {}
_compiled_count_cache: dict[tuple | int, str] = {}
_compiled_cache_lock = threading.Lock()

# Bound the compiled-SQL caches. In a normal app the number of distinct query
# shapes is finite (one per ORM call site), but shapes that fold variable data
# into the fingerprint (dynamic filter combinations, cardinality-varying IN
# lists) can grow without limit. Cap and wholesale-clear on overflow — the same
# discipline database.py uses for its handle cache; a clear costs a one-time
# recompile storm, never unbounded memory.
_COMPILED_CACHE_MAX = 8192


def _store_compiled(cache: dict[tuple | int, str], key: tuple | int, sql: str) -> None:
    """Insert into a compiled-SQL cache under the lock, bounding its size."""
    with _compiled_cache_lock:
        if len(cache) >= _COMPILED_CACHE_MAX and key not in cache:
            cache.clear()
        cache[key] = sql


def clear_compiled_cache() -> None:
    """Clear the compiled SQL cache. Useful for testing."""
    with _compiled_cache_lock:
        _compiled_sql_cache.clear()
        _compiled_count_cache.clear()


# Per-model cache of nullable DB column names (for exclude()/~Q() NULL demotion).
# Keyed by id(model) — models are long-lived, so id reuse is not a concern.
_nullable_cols_cache: dict[int, frozenset[str]] = {}
_nullable_cols_lock = threading.Lock()


def _compute_nullable_columns(model) -> frozenset[str]:
    """Column names of ``model`` that can be NULL in the database.

    Mirrors the DDL's NOT NULL inference (see models.generate_ddl): a non-PK,
    non-auto column is nullable when its type annotation permits None OR it has
    a ``default=None``. Used to decide whether a negated exclude()/~Q() leaf
    needs the ``OR col IS NULL`` demotion. Lazy imports avoid the
    query→models→query import cycle.
    """
    from hyperdjango.models import FieldInfo, _annotation_is_nullable

    meta = model._meta
    # Annotations + field objects across the MRO (subclass wins). Annotations
    # are read via attribute access, not __dict__: the DHI metaclass exposes
    # them through a descriptor (mirrors models._field_to_sql_type).
    annotations: dict = {}
    field_objs: dict = {}
    for klass in reversed(model.__mro__):
        if hasattr(klass, "__annotations__"):
            annotations.update(klass.__annotations__)
        for name, obj in klass.__dict__.items():
            if isinstance(obj, FieldInfo):
                field_objs[name] = obj

    nullable: set[str] = set()
    for col in meta.column_names:
        fmeta = meta.fields.get(col)
        if fmeta is None or fmeta.primary_key or fmeta.auto:
            continue
        if _annotation_is_nullable(annotations.get(col)):
            nullable.add(col)
            continue
        field_obj = field_objs.get(col)
        if field_obj is not None and field_obj.default is None:
            nullable.add(col)
    return frozenset(nullable)


def _is_inline_filter_value(value) -> bool:
    """True if ``value`` is inlined into SQL rather than bound as a $N param.

    Covers an F()/expression RHS (rendered as a column reference) and a
    QuerySet/Subquery ``__in`` (rendered as a subselect). Such values make a
    query unsafe for the compiled-SQL fast cache, which re-collects params by
    structural shape and would mis-handle the object.
    """
    if isinstance(value, Expression):
        return True
    # QuerySet duck-type (avoids importing itself); Subquery is caught above.
    return hasattr(value, "_build_select")


def _q_has_inline_value(q) -> bool:
    """True if a Q tree forces the compiled-SQL slow path.

    That is: any leaf value is an inline (non-param) value, OR any leaf key uses
    a value-dependent lookup (vector distance/nearest) whose SQL a shape-keyed
    cache entry would misrepresent.
    """
    for child in q.children:
        if isinstance(child, Q):
            if _q_has_inline_value(child):
                return True
        elif _is_inline_filter_value(child[1]) or key_is_value_dependent(child[0]):
            return True
    return False


def _register_model(table_name: str, model_class: type):
    """Register a model class by its table name."""
    with _model_registry_lock:
        _model_registry[table_name] = model_class


def _get_model_by_table(table_name: str) -> type | None:
    """Look up a model class by table name.

    Handles both "table" and "table.column" FK formats —
    strips the .column suffix if present before lookup.
    """
    # Strip .column suffix: "hn_users.id" → "hn_users"
    if "." in table_name:
        table_name = table_name.split(".", 1)[0]
    with _model_registry_lock:
        return _model_registry.get(table_name)


# ---------------------------------------------------------------------------
# QuerySet
# ---------------------------------------------------------------------------
class QuerySet:
    """Chainable query builder that generates SQL for a Model."""

    def __init__(
        self,
        model_class,
        filters=None,
        excludes=None,
        ordering=None,
        limit_val=None,
        offset_val=None,
        distinct_val=False,
        values_fields=None,
        flat=False,
        values_as_dict=True,
        cache_ttl=None,
        select_related_fields=None,
        prefetch_related_fields=None,
        annotations=None,
        group_by_fields=None,
        using_db=None,
        join_related_aliases=None,
    ):
        self._model = model_class
        self._filters = filters or []
        self._excludes = excludes or []
        self._ordering = tuple(ordering) if ordering else ()
        self._limit = limit_val
        self._offset = offset_val
        self._distinct = distinct_val
        self._values_fields = values_fields
        self._flat = flat
        # True for values() (rows → dicts), False for values_list() (rows → tuples).
        self._values_as_dict = values_as_dict
        self._cache_ttl = cache_ttl
        # Relations
        self._select_related = select_related_fields or []
        self._prefetch_related = prefetch_related_fields or []
        # join_related: FK field name → alias attribute name. Entries
        # here also appear in _select_related (so the SQL JOIN
        # machinery picks them up), but _populate_select_related
        # attaches the related instance on the alias attribute and
        # leaves the FK column value (the int) untouched — unlike
        # plain select_related which REPLACES the FK value with the
        # related instance. Designed for tight-coupling cases like
        # hyperticket where FK columns are read as ints downstream by
        # many handlers.
        self._join_related_aliases: dict[str, str] = join_related_aliases or {}
        # Annotations
        self._annotations = annotations or {}  # alias -> Expression
        self._group_by = group_by_fields  # None = auto, list = explicit
        # Multi-database
        self._using = using_db  # Database alias or Database instance
        # Row locking
        self._for_update: str = ""
        # Raw WHERE fragments (for OR conditions, search, etc.)
        self._raw_wheres: list[tuple[str, list[BindValue]]] = []
        # Fast-path flag: True if any Q objects in filters/excludes
        self._has_q = False
        # Fast-path flag: True if any __exists__ (Exists/NotExists) sentinel is
        # present in filters/excludes. Tracked incrementally (like _has_q) so
        # _build_select needn't allocate `self._filters + self._excludes` and
        # rescan on every SELECT. Monotonic: filter()/exclude() only ever ADD
        # entries, so OR-accumulating the flag stays accurate.
        self._has_exists = False
        # True if any filter/exclude is unsafe for the compiled-SQL fast cache
        # (which re-collects params / keys on structural shape only). Two causes:
        #   1. an inlined RHS — F()/expression or a QuerySet/Subquery `__in` —
        #      that embeds per-call SQL/params rather than a positional param; or
        #   2. a value-dependent lookup (pgvector distance/nearest) whose emitted
        #      SQL varies with the value (placeholder count / distance operator).
        # Either way the shape-keyed cache entry can't be reused, so we force the
        # slow (full-compile) path. Set incrementally in filter()/exclude().
        self._has_inline_rhs = False
        # Column selection (only/defer)
        self._only: list[str] | None = None
        self._defer: list[str] | None = None
        # CTE prefixes (task #197) — each is a dataclass describing a
        # `WITH [RECURSIVE] name AS (body_sql)` clause to prepend to the
        # compiled SELECT. The body_sql is raw SQL with `{idx}`
        # placeholders (matching where_raw convention) that are
        # substituted with consecutive `$N` indices at compile time.
        # Multiple `.with_cte()` calls accumulate in declaration order.
        self._ctes: list[_CTEClause] = []

    def _clone(self, **kwargs):
        qs = type(self)(
            model_class=self._model,
            filters=kwargs.get("filters", self._filters),
            excludes=kwargs.get("excludes", self._excludes),
            ordering=kwargs.get("ordering", self._ordering),
            limit_val=kwargs.get("limit_val", self._limit),
            offset_val=kwargs.get("offset_val", self._offset),
            distinct_val=kwargs.get("distinct_val", self._distinct),
            values_fields=kwargs.get("values_fields", self._values_fields),
            flat=kwargs.get("flat", self._flat),
            values_as_dict=kwargs.get("values_as_dict", self._values_as_dict),
            cache_ttl=kwargs.get("cache_ttl", self._cache_ttl),
            select_related_fields=kwargs.get(
                "select_related_fields", self._select_related
            ),
            prefetch_related_fields=kwargs.get(
                "prefetch_related_fields", self._prefetch_related
            ),
            annotations=kwargs.get("annotations", self._annotations),
            group_by_fields=kwargs.get("group_by_fields", self._group_by),
            using_db=kwargs.get("using_db", self._using),
            join_related_aliases=kwargs.get(
                "join_related_aliases", self._join_related_aliases
            ),
        )
        # Propagate state not in __init__ params
        qs._for_update = self._for_update
        qs._raw_wheres = self._raw_wheres
        qs._only = self._only
        qs._defer = self._defer
        qs._has_q = self._has_q
        qs._has_exists = self._has_exists
        qs._has_inline_rhs = self._has_inline_rhs
        qs._ctes = kwargs.get("ctes", self._ctes)
        return qs

    # --- Chainable methods ---

    def filter(self, *args, **kwargs):
        """Add WHERE conditions (AND).

        Accepts keyword arguments for simple filters and/or Q objects
        for complex OR/AND/NOT compositions:

            qs.filter(name="alice")                          # Simple
            qs.filter(Q(name="alice") | Q(name="bob"))       # OR via Q
            qs.filter(Q(a=1) | Q(b=2), c=3)                 # Q + kwargs
        """

        from hyperdjango.expressions import Exists, NotExists

        new_filters = list(self._filters)
        has_q = self._has_q
        has_exists = self._has_exists
        for arg in args:
            if isinstance(arg, Q):
                new_filters.append(("__q__", arg))
                has_q = True
            elif isinstance(arg, (Exists, NotExists)):
                # Store Exists/NotExists as a sentinel filter entry.
                # Resolved at _build_where_tree() time where the outer
                # table name is known (needed for OuterRef substitution).
                new_filters.append(("__exists__", arg))
                has_exists = True
            else:
                raise TypeError(
                    f"Positional arguments to filter() must be Q or Exists instances, got {type(arg).__name__}"
                )
        new_filters.extend(kwargs.items())
        qs = self._clone(filters=new_filters)
        qs._has_q = has_q
        qs._has_exists = has_exists
        if not qs._has_inline_rhs:
            qs._has_inline_rhs = (
                any(_is_inline_filter_value(v) for v in kwargs.values())
                or any(key_is_value_dependent(k) for k in kwargs)
                or any(isinstance(a, Q) and _q_has_inline_value(a) for a in args)
            )
        return qs

    def exclude(self, *args, **kwargs):
        """Add NOT WHERE conditions.

        Accepts keyword arguments and/or Q / Exists instances:

            qs.exclude(is_deleted=True)
            qs.exclude(Q(status="draft") | Q(status="archived"))
            qs.exclude(Exists(ChildQS.filter(parent_id=OuterRef("id"))))
        """

        from hyperdjango.expressions import Exists, NotExists

        new_excludes = list(self._excludes)
        has_q = self._has_q
        has_exists = self._has_exists
        for arg in args:
            if isinstance(arg, Q):
                new_excludes.append(("__q__", arg))
                has_q = True
            elif isinstance(arg, (Exists, NotExists)):
                # Store the Exists as a NEGATED marker — same sentinel
                # key as filter() but the where-tree builder will flip
                # to `NOT EXISTS (...)`.
                new_excludes.append(("__exists__", arg))
                has_exists = True
            else:
                raise TypeError(
                    f"Positional arguments to exclude() must be Q or Exists instances, got {type(arg).__name__}"
                )
        # Group all kwargs of a SINGLE exclude() call into ONE negated AND-group
        # so `exclude(a=1, b=2)` compiles to `NOT(a=1 AND b=2)` — not the buggy
        # `NOT(a) AND NOT(b)`. Reuses the ~Q(**kwargs) path (see _build_where_tree
        # and Q.to_node), which also applies the correct NULL demotion.
        if kwargs:
            new_excludes.append(("__q__", Q(**kwargs)))
            has_q = True
        qs = self._clone(excludes=new_excludes)
        qs._has_q = has_q
        qs._has_exists = has_exists
        if not qs._has_inline_rhs:
            qs._has_inline_rhs = (
                any(_is_inline_filter_value(v) for v in kwargs.values())
                or any(key_is_value_dependent(k) for k in kwargs)
                or any(isinstance(a, Q) and _q_has_inline_value(a) for a in args)
            )
        return qs

    def where_raw(self, sql_template: str, *params: BindValue) -> QuerySet:
        """Add a raw SQL WHERE fragment with parameterized values.

        The sql_template uses {idx} placeholders that are replaced with
        consecutive $N parameter indices. Each {idx} consumes the next
        parameter from *params in order (1:1 mapping).

        This is primarily used for conditions that can't be expressed
        through the standard filter() API:

            # Two placeholders, two params:
            qs.where_raw("score > {idx} AND score < {idx}", 10, 100)
            # → "score > $1 AND score < $2" with params [10, 100]

            # Single param for ILIKE OR:
            qs.where_raw("(title ILIKE {idx} OR content ILIKE {idx})",
                         "%search%", "%search%")
            # → "(title ILIKE $1 OR content ILIKE $2)" with params ["%search%", "%search%"]

        Args:
            sql_template: SQL fragment with {idx} placeholders
            *params: Parameter values, one per {idx} placeholder in order
        """
        qs = self._clone()
        qs._raw_wheres = list(self._raw_wheres) + [(sql_template, list(params))]
        return qs

    def with_cte(
        self,
        name: str,
        body_sql: str,
        *params: BindValue,
        recursive: bool = False,
    ) -> QuerySet:
        """Prepend a Common Table Expression to the compiled SELECT.

        Accepts the CTE body as raw SQL with `{idx}` placeholders (the
        same convention as :meth:`where_raw`). This is intentionally a
        raw-SQL escape hatch — recursive CTEs with self-joining anchor +
        recursive branches cannot be cleanly expressed via pure ORM
        constructs, so we don't try.

        The CTE body is compiled into the outer query as::

            WITH [RECURSIVE] <name> AS (<body_sql>) <outer SELECT>

        All downstream `.filter()`, `.order_by()`, `.limit()` etc.
        chains work normally and can reference the CTE by name via
        :meth:`where_raw` for the WHERE clause or by aliasing a join
        target. Multiple ``.with_cte()`` calls accumulate in declaration
        order (comma-separated in the emitted ``WITH`` clause) and any
        recursive CTE promotes the whole clause to ``WITH RECURSIVE``.

        Example — recursive RBAC role tree walk::

            qs = (
                Permission.objects
                .with_cte(
                    "role_tree",
                    "SELECT group_id AS id FROM hyper_user_groups "
                    "WHERE user_id = {idx} "
                    "UNION ALL "
                    "SELECT g.parent_id FROM hyper_groups g "
                    "JOIN role_tree rt ON g.id = rt.id "
                    "WHERE g.parent_id IS NOT NULL",
                    user_id,
                    recursive=True,
                )
                .where_raw(
                    "id IN (SELECT p.id FROM hyper_permissions p "
                    "JOIN hyper_group_permissions gp ON p.id = gp.permission_id "
                    "WHERE gp.group_id IN (SELECT id FROM role_tree))"
                )
            )

        Args:
            name: CTE name — valid SQL identifier.
            body_sql: Raw SQL body (contents of the ``AS (...)``). Use
                ``{idx}`` placeholders for parameterized values.
            *params: Parameter values, one per ``{idx}`` placeholder.
            recursive: If True, the entire ``WITH`` clause is emitted
                as ``WITH RECURSIVE`` — required for CTEs that
                self-reference in a ``UNION ALL`` branch.

        Returns:
            A cloned QuerySet with the CTE prepended to its compile path.
        """
        # The CTE name is interpolated RAW into `WITH <name> AS (...)` — validate
        # it as a SQL identifier (same gate as annotate/order_by aliases). The
        # body_sql stays the intentional raw-SQL escape hatch (documented above).
        _validate_alias_name(name, source="with_cte")
        clause = _CTEClause(
            name=name,
            body_sql=body_sql,
            params=list(params),
            recursive=recursive,
        )
        qs = self._clone()
        qs._ctes = list(self._ctes) + [clause]
        return qs

    def guard_filter(
        self,
        user: dict[str, object] | object,
        action: str,
        *,
        registry: object,
        resource_name: str | None = None,
    ) -> QuerySet:
        """Apply HyperGuard policy as a SQL WHERE filter.

        Generates SQL conditions from a compiled .guard policy and injects
        them into this QuerySet. This ensures listing pages enforce the same
        permission rules as single-object @guard checks — automatically.

        Args:
            user: User dict (from request.user). User fields referenced
                  in the policy are inlined as parameters.
            action: The policy action to filter for (e.g., "read", "write_post").
            registry: PolicyRegistry instance with loaded .guard policies.
            resource_name: Policy resource name to look up. Defaults to the
                           model class name. Use this when the policy resource
                           name differs from the Python class name.

        Returns:
            New QuerySet with policy-generated WHERE clause applied.

        Usage:
            posts = await Post.objects.guard_filter(
                request.user, "read", registry=registry
            ).order_by("-created_at").limit(20).all()
        """
        # Inline import: guard/__init__.py has circular dependency with query.py
        from hyperdjango.guard.sql import generate_where

        if resource_name is None:
            resource_name = self._model.__name__

        resource = registry.get_resource(resource_name)
        if resource is None:
            # No policy for this resource = deny all (empty queryset)
            return self.where_raw("FALSE")

        table_name = self._model._meta.table

        # SessionUser/User/AnonymousUser all support .get() for dict-like access;
        # generate_where only calls user_fields.get(field_name).
        fragment = generate_where(
            resource, action, user_fields=user, table_name=table_name
        )
        if fragment.is_empty or fragment.sql == "FALSE":
            return self.where_raw("FALSE")

        return self.where_raw(fragment.sql, *fragment.params)

    def order_by(self, *fields):
        """Set ORDER BY. Prefix with '-' for DESC. Stored as tuple (hashable for cache key)."""
        # order_by field names are interpolated into the SQL identifier position
        # (``_qualify_column`` → ``ORDER BY <col>``) — NOT bound as params. Reject
        # any SQL-unsafe characters so raw user input (``order_by(request.GET[...])``)
        # can't smuggle in a payload. Mirrors the annotate/aggregate alias gate.
        for f in fields:
            name = f[1:] if isinstance(f, str) and f.startswith("-") else f
            _validate_alias_name(name, source="order_by")
        return self._clone(ordering=fields)

    def limit(self, n):
        """Set LIMIT."""
        return self._clone(limit_val=n)

    def offset(self, n):
        """Set OFFSET."""
        return self._clone(offset_val=n)

    def cache(self, ttl=60):
        """Cache query results for ttl seconds."""
        return self._clone(cache_ttl=ttl)

    def distinct(self):
        """Add DISTINCT to the query."""
        return self._clone(distinct_val=True)

    def values(self, *fields):
        """Return dicts instead of model instances."""
        # values() field names are interpolated into the SELECT identifier
        # position (``_qualify_column``) — validate like order_by so raw user
        # input can't inject SQL.
        for f in fields:
            _validate_alias_name(f, source="values")
        return self._clone(
            values_fields=list(fields) if fields else None, values_as_dict=True
        )

    def values_list(self, *fields, flat=False):
        """Return tuples instead of model instances.

        With ``flat=True`` and a single field, yields the bare scalar values.
        Otherwise yields a tuple per row with the values in field order.
        """
        if flat and len(fields) != 1:
            raise TypeError(
                f"values_list(flat=True) requires exactly one field, got {len(fields)}"
            )
        # Field names are interpolated into the SELECT identifier position —
        # validate like values()/order_by against SQL injection.
        for f in fields:
            _validate_alias_name(f, source="values_list")
        return self._clone(
            values_fields=list(fields) if fields else None,
            flat=flat,
            values_as_dict=False,
        )

    def select_related(self, *fields):
        """Eagerly load FK relations via LEFT JOIN.

        Usage:
            books = await Book.objects.select_related("author").all()
            # book.author is a full Author instance (no extra query)

            # Nested:
            books = await Book.objects.select_related("author__publisher").all()
            # book.author.publisher is loaded too

        Semantics: after fetch, `book.author_id` is REPLACED with the
        full Author instance. Downstream code that needs the FK
        integer must read `book.author_id.id` or switch to
        ``join_related`` (see below) which preserves the int column.
        """
        new_fields = list(self._select_related) + list(fields)
        return self._clone(select_related_fields=new_fields)

    def join_related(self, **aliases: str):
        """Eagerly load FK relations via LEFT JOIN and attach them to
        *new* attribute names, leaving the original FK integer columns
        untouched.

        Unlike `select_related`, this is non-destructive: the FK column
        (e.g. `status_id`) stays an integer, and the related instance
        is attached on the alias attribute name you specify:

            ticket = await (
                Ticket.objects.join_related(
                    status="status_id",
                    priority="priority_id",
                    assignee="assignee_id",
                )
                .filter(id=ticket_id)
                .first()
            )
            # ticket.status_id is still an int (47)
            # ticket.status is the TicketStatusConfig instance (or None)

        Use this when downstream code (equality checks, filter
        kwargs, Activity log entries, etc.) depends on the FK
        column being an int. Plain `select_related` is the right
        default for fresh code.

        Args:
            **aliases: kwargs of form `attr_name="fk_field_name"`
                mapping attach-point attributes to FK column names.

        Returns:
            A cloned QuerySet with the JOIN + alias tracking set.
        """
        if not aliases:
            return self
        # Reverse kwargs to (fk_field → alias) since that's how the
        # row hydration needs to look it up.
        new_map = dict(self._join_related_aliases)
        for alias, fk_field in aliases.items():
            new_map[fk_field] = alias
        # Piggyback on the existing select_related SQL JOIN machinery
        # by registering each FK field as select_related — the
        # `_join_related_aliases` map is consulted at populate time
        # to decide whether to replace or attach-as-sibling.
        new_select_related = list(self._select_related)
        for fk_field in aliases.values():
            if fk_field not in new_select_related:
                new_select_related.append(fk_field)
        return self._clone(
            select_related_fields=new_select_related,
            join_related_aliases=new_map,
        )

    def prefetch_related(self, *fields):
        """Load related objects in separate batch queries.

        Works for reverse FK and M2M relations.

        Usage:
            authors = await Author.objects.prefetch_related("books").all()
            # author.books is a list of Book instances (1 extra query total)
        """
        new_fields = list(self._prefetch_related) + list(fields)
        return self._clone(prefetch_related_fields=new_fields)

    def using(self, db):
        """Select which database to use for this query.

        Args:
            db: Database alias string (e.g., "replica") or Database instance.

        Usage:
            users = await User.objects.using("replica").filter(active=True).all()
        """
        return self._clone(using_db=db)

    def only(self, *fields: str):
        """Load only the specified columns from the database.

        Generates `SELECT col1, col2 FROM ...` instead of `SELECT * FROM ...`.
        Reduces data transfer for wide tables when only a few columns are needed.

        Usage:
            users = await User.objects.only("id", "name").all()
        """
        qs = self._clone()
        qs._only = list(fields)
        qs._defer = None
        return qs

    def defer(self, *fields: str):
        """Exclude the specified columns from the SELECT.

        Loads all columns EXCEPT the deferred ones. Useful for skipping
        large text/blob columns that aren't needed.

        Usage:
            posts = await Post.objects.defer("body", "raw_html").all()
        """
        qs = self._clone()
        qs._defer = list(fields)
        qs._only = None
        return qs

    def annotate(self, **kwargs):
        """Add computed columns to the result.

        Usage:
            from hyperdjango.expressions import Count, Sum

            books = await Book.objects.annotate(num_orders=Count("id")).all()
            # book.num_orders is available on each instance

            # With GROUP BY (automatic when aggregate used):
            authors = await Author.objects.values("name").annotate(book_count=Count("id")).all()
        """
        model_columns = frozenset(self._model._meta.column_names)
        for alias in kwargs:
            _validate_alias_name(alias, source="annotate")
            # An annotation alias equal to a real column name emits both the
            # column and `AS <alias>` in the SELECT; the row dict keeps ONE key
            # (the annotation wins), silently CLOBBERING the real column value.
            # Django rejects this — mirror it with a clear error at build time.
            if alias in model_columns:
                raise ValueError(
                    f"The annotation '{alias}' conflicts with a field on the "
                    f"model {self._model.__name__}."
                )
        new_annotations = dict(self._annotations)
        new_annotations.update(kwargs)

        # Auto-detect GROUP BY when aggregates are present AND values() was used
        # (without values(), aggregates are per-row computed columns — no GROUP BY needed)
        group_by = self._group_by
        has_agg = any(
            isinstance(v, Expression) and v.contains_aggregate
            for v in new_annotations.values()
        )
        if has_agg and group_by is None and self._values_fields:
            group_by = True  # Sentinel: auto-group by values() fields

        return self._clone(annotations=new_annotations, group_by_fields=group_by)

    # --- Terminal methods (execute the query) ---

    async def all(self):
        """Execute and return all matching rows."""
        sql, params = self._build_select()

        # Resolve cache TTL: explicit .cache() > Meta.cache_ttl > None
        cache_ttl = self._cache_ttl
        if cache_ttl is None:
            # _meta is a TableMeta dataclass with a declared ``cache_ttl`` field.
            cache_ttl = self._model._meta.cache_ttl

        # Check cache
        qc = get_query_cache()
        cache_key = None
        if cache_ttl is not None and cache_ttl is not False:
            # Build cache key including versions of all involved tables
            tables = self._get_involved_tables()
            if tables is None:
                # A prefetch_related / M2M target couldn't be resolved to a
                # concrete table, so we can't version the key against it.
                # Caching the fully-hydrated result anyway would serve stale
                # children after a write to that unversioned table — skip the
                # result cache instead (fail-safe: a miss is always correct).
                cache_key = None
            elif len(tables) > 1:
                cache_key = qc.make_multi_table_key(tables, sql, tuple(params))
            else:
                cache_key = qc.make_key(self._model._meta.table, sql, tuple(params))

            if cache_key is not None:
                cached = qc.get(cache_key)
                if cached is not None:
                    return cached

        db = self._get_db()
        rows = await db.query(sql, *params)

        if self._values_fields and self._flat:
            result = [_row_val(row, 0) for row in rows]
        elif self._values_fields and not self._values_as_dict:
            # values_list(*fields) (non-flat) → tuples in field order, NOT dicts.
            fields = list(self._values_fields) + list(self._annotations.keys())
            result = [_row_to_tuple(row, fields) for row in rows]
        elif self._values_fields:
            # values(*fields) → dicts. Build field list including annotations.
            fields = list(self._values_fields) + list(self._annotations.keys())
            result = [_row_to_dict(row, fields) for row in rows]
        elif self._select_related:
            result = self._populate_select_related(rows)
        else:
            result = self._populate_results(rows)

        # Prefetch related objects (separate queries)
        if self._prefetch_related and result:
            await self._execute_prefetch(result)

        # Store in cache
        if cache_key is not None:
            qc.set(cache_key, result, cache_ttl)

        return result

    def _get_involved_tables(self) -> list[str] | None:
        """Get every table whose rows are baked into this query's cached result.

        Covers the main table, select_related JOIN targets, AND every
        prefetch_related / M2M target table — because ``all()`` caches the
        FULLY-HYDRATED result (prefetched children included) under a key
        versioned by these tables. If a child table were omitted, inserting a
        child would never bump a version the key depends on (reverse-relation
        dependency cascades the wrong way), so the cached parent would be
        served STALE until TTL.

        Returns ``None`` when a prefetch target can't be resolved to a concrete
        table; the caller must then SKIP the result cache rather than cache a
        result keyed by an incomplete table set.
        """
        tables = [self._model._meta.table]
        if self._select_related:
            _, join_aliases = self._resolve_joins()
            for field_path in join_aliases:
                # Walk the FK chain to find the target table
                current_model = self._model
                for part in field_path.split("__"):
                    field_meta = current_model._meta.fields.get(part)
                    if field_meta and field_meta.foreign_key:
                        tables.append(field_meta.foreign_key)
                        related_model = _get_model_by_table(field_meta.foreign_key)
                        if related_model:
                            current_model = related_model
        # prefetch_related / M2M targets are hydrated INTO the cached result, so
        # their tables must version the key too: a child insert bumps that
        # table's version → key changes → cache miss → fresh re-fetch.
        for field_path in self._prefetch_related:
            resolved = self._resolve_prefetch_tables(field_path)
            if resolved is None:
                return None  # unresolvable → caller skips the cache (fail-safe)
            tables.extend(resolved)
        return tables

    def _resolve_prefetch_tables(self, field_path: str) -> list[str] | None:
        """Tables whose rows are hydrated into a single prefetch_related result.

        Mirrors ``_execute_prefetch``'s single-level resolution: an M2M field
        contributes its junction AND target tables; a reverse-FK field
        contributes the related model's table. Returns ``None`` if the field
        can't be resolved to a concrete table (caller then skips caching).
        """
        field_name = field_path.split("__")[0]
        # Probe the model for the relation attribute named by the runtime
        # prefetch path segment (to detect an M2M descriptor).
        # dynamic-attr: field_name is a runtime prefetch path segment, not a known attribute
        desc = getattr(self._model, field_name, None)
        # dynamic-attr: `_is_m2m` marks a ManyToManyField descriptor
        if desc is not None and getattr(desc, "_is_m2m", False):
            resolved: list[str] = []
            if desc._junction_table:
                resolved.append(desc._junction_table)
            try:
                desc._ensure_target()
            except ValueError:
                return None
            if desc._target_model is not None:
                resolved.append(desc._target_model._meta.table)
            return resolved or None
        related_model = self._find_reverse_fk_model(field_name)
        if related_model is None:
            return None
        return [related_model._meta.table]

    async def first(self):
        """Execute and return the first matching row, or None."""
        qs = self.limit(1)
        results = await qs.all()
        return results[0] if results else None

    async def last(self):
        """Execute and return the last matching row, or None."""
        if self._ordering:
            reversed_ordering = []
            for f in self._ordering:
                if f.startswith("-"):
                    reversed_ordering.append(f[1:])
                else:
                    reversed_ordering.append(f"-{f}")
        else:
            # For composite PKs, order by all PK fields
            pks = self._model._meta.pk_fields
            reversed_ordering = [f"-{pk}" for pk in pks]
        qs = self._clone(ordering=reversed_ordering).limit(1)
        results = await qs.all()
        return results[0] if results else None

    async def get(self, **kwargs):
        """Get a single row matching the filters.

        Raises DoesNotExist if no row found, MultipleObjectsReturned if more than one.
        """
        qs = self.filter(**kwargs) if kwargs else self
        qs = qs.limit(2)  # Only need to know if 0, 1, or 2+
        results = await qs.all()
        if not results:
            raise self._model.DoesNotExist(
                f"{self._model.__name__} matching query does not exist."
            )
        if len(results) > 1:
            raise self._model.MultipleObjectsReturned(
                f"get() returned more than one {self._model.__name__}."
            )
        return results[0]

    async def count(self):
        """Return the count of matching rows.

        A slice (``qs[offset:limit]``) narrows the counted set, matching
        Django: ``count()`` on a sliced queryset returns the size of the slice,
        not the whole table. COUNT(*) itself ignores LIMIT/OFFSET, so clamp the
        raw total by the slice bounds here.
        """
        sql, params = self._build_count()
        db = self._get_db()
        total = await db.query_val(sql, *params)
        if self._offset is not None:
            total = max(0, total - self._offset)
        if self._limit is not None:
            total = min(total, self._limit)
        return total

    async def exists(self):
        """Return True if any matching rows exist.

        Uses SELECT 1 ... LIMIT 1 for early termination instead of COUNT(*).
        """
        qs = self.limit(1)
        # Emit `SELECT 1 FROM ...` directly (cached under its own key) rather
        # than compiling the full column list and splicing it out with two
        # whole-string .upper() copies.
        sql, params = qs._build_select(columns_override="1")
        db = self._get_db()
        result = await db.query_val(sql, *params)
        return result is not None

    async def latest(self, field_name: str | None = None):
        """Return the latest object by the given field (or pk by default).

        Raises DoesNotExist if the QuerySet is empty.
        """
        order_field = field_name or self._model._meta.pk_field
        qs = self.order_by(f"-{order_field}").limit(1)
        result = await qs.first()
        if result is None:
            raise self._model.DoesNotExist(
                f"{self._model.__name__} matching query has no results"
            )
        return result

    async def earliest(self, field_name: str | None = None):
        """Return the earliest object by the given field (or pk by default).

        Raises DoesNotExist if the QuerySet is empty.
        """
        order_field = field_name or self._model._meta.pk_field
        qs = self.order_by(order_field).limit(1)
        result = await qs.first()
        if result is None:
            raise self._model.DoesNotExist(
                f"{self._model.__name__} matching query has no results"
            )
        return result

    async def explain(self, analyze: bool = False) -> str:
        """Return the query execution plan as a string.

        Args:
            analyze: If True, actually execute the query and show real timings.
        """
        sql, params = self._build_select()
        prefix = "EXPLAIN ANALYZE" if analyze else "EXPLAIN"
        explain_sql = f"{prefix} {sql}"
        db = self._get_db()
        rows = await db.query(explain_sql, *params)
        # PostgreSQL returns plan as rows with a single column
        lines: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                lines.append(next(iter(row.values())))
            else:
                lines.append(str(row[0]) if row else "")
        return "\n".join(lines)

    def to_sql(
        self,
        kind: str = "select",
        update_values: dict[str, Any] | None = None,
        update_returning: list[str] | None = None,
    ) -> CompiledQuery:
        """Return the compiled SQL + params WITHOUT executing the query.

        Synchronous, read-only, no DB access. The primary debug
        chainable for understanding what SQL the ORM is generating.

        Usage:
            qs = Forum.objects.filter(is_public=True).select_related("owner_id")
            print(qs.to_sql())              # SELECT view
            print(qs.to_sql().inlined())    # SQL with $N substituted to repr

            # UPDATE preview:
            print(qs.to_sql(kind="update", update_values={"is_active": False}))

            # DELETE preview:
            print(qs.to_sql(kind="delete"))

        Args:
            kind: "select" (default), "update", or "delete". Update
                requires `update_values`.
            update_values: Required when ``kind="update"`` — the
                ``{column: value}`` dict that would be passed to
                ``.update()``.
            update_returning: Optional list of columns to include in
                the ``RETURNING`` clause when previewing an update.

        Returns:
            CompiledQuery dataclass with `.sql`, `.params`, and `.kind`.
            `str(result)` gives a human-readable dump; `result.inlined()`
            substitutes `$N` placeholders with Python `repr()` of each
            param (read-only — never execute the inlined version).

        Raises:
            ValueError: when ``kind="update"`` is used without
                ``update_values`` or with an unknown ``kind``.
        """
        if kind == "select":
            sql, params = self._build_select()
            return CompiledQuery(sql=sql, params=list(params), kind="SELECT")
        if kind == "update":
            if update_values is None:
                raise ValueError("to_sql(kind='update') requires update_values=")
            sql, params = self._build_update(update_values, returning=update_returning)
            return CompiledQuery(sql=sql, params=list(params), kind="UPDATE")
        if kind == "delete":
            sql, params = self._build_delete()
            return CompiledQuery(sql=sql, params=list(params), kind="DELETE")
        raise ValueError(
            f"to_sql(kind={kind!r}) — kind must be 'select', 'update', or 'delete'"
        )

    def select_for_update(self, nowait: bool = False, skip_locked: bool = False):
        """Lock selected rows for the duration of the transaction.

        Must be used inside a transaction context (db.transaction()).

        Args:
            nowait: If True, raise error instead of waiting for lock.
            skip_locked: If True, skip already-locked rows.
        """
        suffix = " FOR UPDATE"
        if nowait:
            suffix += " NOWAIT"
        elif skip_locked:
            suffix += " SKIP LOCKED"
        qs = self._clone()
        qs._for_update = suffix
        return qs

    async def create(self, **kwargs):
        """Create and return a new model instance."""
        instance = self._model(**kwargs)
        # Carry this queryset's .using() binding into save() so create() writes
        # to the SAME connection the queryset reads from (was dropped before —
        # instance.save() fell back to the global default).
        await instance.save(_using=self._using)
        # save() fires signals which trigger cache invalidation automatically
        return instance

    async def bulk_create(self, instances):
        """Create multiple model instances in a single INSERT."""
        if not instances:
            return []
        meta = self._model._meta
        columns = meta.writable_columns
        db = self._get_db(for_write=True)

        col_names = ", ".join(columns)
        value_rows = []
        params = []
        for inst in instances:
            placeholders = []
            for col in columns:
                # dynamic-attr: reading each user model instance's column, whose name comes from meta.writable_columns at runtime
                params.append(getattr(inst, col))
                placeholders.append(f"${len(params)}")
            value_rows.append(f"({', '.join(placeholders)})")

        sql = f"INSERT INTO {meta.table} ({col_names}) VALUES {', '.join(value_rows)}"
        # RETURNING for auto-generated PKs (single PK with auto_field)
        if meta.auto_field:
            sql += f" RETURNING {meta.auto_field}"
            rows = await db.query(sql, *params)
            if rows:
                for inst, row in zip(instances, rows):
                    # dynamic-attr: writing each user model instance's auto column, whose name (meta.auto_field) is only known at runtime
                    setattr(inst, meta.auto_field, _row_val(row, 0))
        elif meta.pk_field:
            # For composite/manual PKs, return all PK fields
            returning_cols = ", ".join(meta.pk_fields)
            sql += f" RETURNING {returning_cols}"
            rows = await db.query(sql, *params)
            if rows and not meta.is_composite_pk:
                for inst, row in zip(instances, rows):
                    # dynamic-attr: writing each user model instance's PK column, whose name (meta.pk_field) is only known at runtime
                    setattr(inst, meta.pk_field, _row_val(row, 0))
        else:
            await db.execute(sql, *params)
        # Raw INSERT bypasses Model.save()/signals, so nothing invalidates the
        # query cache for this table — cached .all()/.filter() results would go
        # stale. Invalidate directly (matches update()/delete()). Inherited by
        # bulk_create_validated, which funnels through here.
        get_query_cache().invalidate_table(meta.table)
        return instances

    async def bulk_create_validated(
        self, data: list[dict[str, object]], *, raise_on_error: bool = True
    ) -> list:
        """Validate a batch of dicts and create instances in a single INSERT.

        Uses SIMD batch validation (13M models/sec) instead of per-instance
        __init__ validation. Significantly faster for bulk imports (5-10x).

        Args:
            data: List of dicts to validate and insert.
            raise_on_error: If True, raise ValueError on first invalid row.
                If False, skip invalid rows and return only valid instances.

        Returns:
            List of created model instances (only valid rows if raise_on_error=False).
        """
        if not data:
            return []

        # Batch validate all rows at once (SIMD-accelerated)
        errors = validate_model_batch(data, self._model)

        # Separate valid from invalid
        valid_data: list[dict[str, object]] = []
        for i, err in enumerate(errors):
            if err is not None:
                if raise_on_error:
                    raise ValueError(f"Validation error in row {i}: {err}")
                continue
            valid_data.append(data[i])

        if not valid_data:
            return []

        # Create instances from validated dicts (skip __init__ validation)
        instances = [self._model(**d) for d in valid_data]
        return await self.bulk_create(instances)

    async def get_or_create(self, defaults: dict[str, object] | None = None, **kwargs):
        """Get an existing object or create a new one atomically.

        Looks up an object by **kwargs. If found, returns (instance, False).
        If not found, creates one using **kwargs + defaults, returns (instance, True).

        Args:
            defaults: Field values to use only when creating (not for lookup).
            **kwargs: Lookup parameters for finding existing object.

        Returns:
            Tuple of (instance, created) where created is True if new.
        """
        try:
            instance = await self.filter(**kwargs).get()
            return instance, False
        except self._model.DoesNotExist:
            pass
        # Not found on the first read. Create inside a savepoint so a concurrent
        # caller that inserted the same row between our get() and create() turns
        # into a catchable unique violation (rolled back to the savepoint, leaving
        # any enclosing transaction intact) instead of a duplicate row or a
        # poisoned outer transaction. On that race, retry the get. (Django's
        # _create_object_from_params pattern.)
        #
        # Both dispatch paths now classify at the native boundary, so the
        # violation always arrives as a typed IntegrityError (unique, FK, etc.).
        # Catch that one type and narrow to the unique-constraint case with
        # is_unique_violation — a non-unique IntegrityError re-raises unchanged.
        from hyperdjango.db.pgzig_connection import IntegrityError, is_unique_violation

        create_kwargs = dict(kwargs)
        if defaults:
            create_kwargs.update(defaults)
        db = self._get_db(for_write=True)
        try:
            async with db.transaction():
                instance = await self.create(**create_kwargs)
            return instance, True
        except IntegrityError as exc:
            if not is_unique_violation(exc):
                raise
            try:
                instance = await self.filter(**kwargs).get()
                return instance, False
            except self._model.DoesNotExist:
                # A unique violation whose row we still can't read (e.g. a
                # different constraint matched the substrings) — re-raise the
                # original error rather than masking it.
                pass
            raise

    async def update_or_create(
        self, defaults: dict[str, object] | None = None, **kwargs
    ):
        """Update an existing object or create a new one atomically.

        Looks up by **kwargs. If found, updates with defaults and saves.
        If not found, creates using **kwargs + defaults.

        Args:
            defaults: Field values to set on the object (update or create).
            **kwargs: Lookup parameters for finding existing object.

        Returns:
            Tuple of (instance, created) where created is True if new.
        """

        async def _apply_defaults(instance):
            if defaults:
                for key, value in defaults.items():
                    # dynamic-attr: applying caller-supplied ``defaults`` field names onto a user model instance
                    setattr(instance, key, value)
                await instance.save(_using=self._using)
            return instance

        try:
            instance = await self.filter(**kwargs).get()
            return await _apply_defaults(instance), False
        except self._model.DoesNotExist:
            pass
        # Not found on the first read — create inside a savepoint so a concurrent
        # insert of the same row surfaces as a catchable unique violation (rolled
        # back to the savepoint) rather than a duplicate row / poisoned outer
        # transaction. On that race, re-read and apply the defaults update. Both
        # dispatch paths classify at the native boundary, so the violation always
        # arrives as a typed IntegrityError; narrow it to the unique-constraint
        # case with is_unique_violation and re-raise any other IntegrityError.
        from hyperdjango.db.pgzig_connection import IntegrityError, is_unique_violation

        create_kwargs = dict(kwargs)
        if defaults:
            create_kwargs.update(defaults)
        db = self._get_db(for_write=True)
        try:
            async with db.transaction():
                instance = await self.create(**create_kwargs)
            return instance, True
        except IntegrityError as exc:
            if not is_unique_violation(exc):
                raise
            try:
                instance = await self.filter(**kwargs).get()
            except self._model.DoesNotExist:
                # A unique violation whose row we still can't read — re-raise the
                # original error rather than masking it.
                pass
            else:
                return await _apply_defaults(instance), False
            raise

    async def bulk_update(self, instances: list[object], fields: list[str]):
        """Update specific fields on multiple instances in batch.

        Uses a single UPDATE with CASE/WHEN for efficiency.
        Does NOT call save() or emit signals.

        Args:
            instances: List of model instances with updated field values.
            fields: List of field names to update.

        Returns:
            Number of rows updated.
        """
        if not instances or not fields:
            return 0
        meta = self._model._meta
        pk_field = meta.pk_field
        db = self._get_db(for_write=True)

        # Validate every field name against the model's own field set BEFORE it
        # is interpolated as a column identifier below. This turns field_name
        # into a trusted allowlist entry (no SQL injection if a caller passes an
        # unexpected/user-derived name) and gives a clear error on a typo rather
        # than a confusing SQL failure.
        for field_name in fields:
            if field_name not in meta.fields:
                raise ValueError(
                    f"bulk_update: {field_name!r} is not a field of "
                    f"{self._model.__name__}; valid fields: {sorted(meta.fields)}"
                )

        # Build UPDATE with CASE for each field
        params: list[BindValue] = []
        set_clauses: list[str] = []
        pk_values: list[BindValue] = []

        for field_name in fields:
            cases: list[str] = []
            for inst in instances:
                # dynamic-attr: reading each user model instance's PK column, whose name (pk_field) is only known at runtime
                pk_val = getattr(inst, pk_field)
                # dynamic-attr: reading each user model instance's field named by the caller-supplied ``fields`` list entry
                field_val = getattr(inst, field_name)
                params.append(pk_val)
                params.append(field_val)
                cases.append(
                    f"WHEN {pk_field} = ${len(params) - 1} THEN ${len(params)}"
                )
            set_clauses.append(f"{field_name} = CASE {' '.join(cases)} END")

        # Collect all PKs for WHERE clause
        for inst in instances:
            # dynamic-attr: reading each user model instance's PK column, whose name (pk_field) is only known at runtime
            pk_values.append(getattr(inst, pk_field))

        pk_placeholders = ", ".join(
            f"${len(params) + i + 1}" for i in range(len(pk_values))
        )
        params.extend(pk_values)

        sql = (
            f"UPDATE {meta.table} SET {', '.join(set_clauses)} "
            f"WHERE {pk_field} IN ({pk_placeholders})"
        )
        rowcount = await db.execute(sql, *params)
        get_query_cache().invalidate_table(meta.table)
        return rowcount

    async def in_bulk(
        self, id_list: list[int | str] | None = None, field_name: str = "pk"
    ):
        """Return a dict mapping field values to model instances.

        Args:
            id_list: List of values to fetch. None = all objects.
            field_name: Field to use as dict key (default "pk").

        Returns:
            Dict mapping field values to model instances.
        """
        actual_field = self._model._meta.pk_field if field_name == "pk" else field_name
        if id_list is not None:
            if not id_list:
                return {}
            qs = self.filter(**{f"{actual_field}__in": id_list})
        else:
            qs = self
        instances = await qs.all()
        # dynamic-attr: keying each user model instance by ``actual_field``, a column name resolved at runtime (meta.pk_field or a caller-supplied field)
        return {getattr(inst, actual_field): inst for inst in instances}

    async def aiterator(self, chunk_size: int = 2000):
        """Async iterator over results, fetched in chunks.

        Memory-efficient for large result sets — doesn't load all results
        into memory at once.

        Args:
            chunk_size: Number of rows to fetch per batch.

        Yields:
            Model instances one at a time.
        """
        offset = 0
        while True:
            batch = await self.offset(offset).limit(chunk_size).all()
            if not batch:
                break
            for instance in batch:
                yield instance
            if len(batch) < chunk_size:
                break
            offset += chunk_size

    async def update(self, returning: list[str] | None = None, **values):
        """Update all matching rows.

        Args:
            returning: Column names to return from updated rows.
                       None (default) returns affected row count (int).
                       List of column names returns list[dict] with those columns.
            **values: Column assignments (name=value or name=F(...)).

        Returns:
            int (affected count) when returning=None.
            list[dict] when returning is specified.

        Usage:
            # Standard: returns count
            n = await Post.objects.filter(id=1).update(score=F("score") + 1)

            # With RETURNING: returns updated rows
            rows = await Post.objects.filter(id=1).update(
                score=F("score") + 1,
                returning=["id", "score", "author_id"],
            )
            # rows = [{"id": 1, "score": 42, "author_id": 7}]
        """
        sql, params = self._build_update(values, returning=returning)
        db = self._get_db(for_write=True)
        # Invalidate AFTER the write succeeds, not before. Invalidating first
        # opens a window where a concurrent read repopulates the cache with
        # pre-update rows; delete() already invalidates post-execute.
        if returning is not None:
            rows = await db.query(sql, *params)
            get_query_cache().invalidate_table(self._model._meta.table)
            return rows
        rowcount = await db.execute(sql, *params)
        get_query_cache().invalidate_table(self._model._meta.table)
        return rowcount

    async def delete(self):
        """Delete all matching rows. Returns number of deleted rows."""
        sql, params = self._build_delete()
        db = self._get_db(for_write=True)
        rowcount = await db.execute(sql, *params)
        # Bulk delete doesn't go through Model.delete(), so invalidate directly
        get_query_cache().invalidate_table(self._model._meta.table)
        return rowcount

    async def aggregate(self, **kwargs):
        """Compute aggregate values over the queryset. Returns a dict.

        Usage:
            stats = await Book.objects.aggregate(
                total=Sum("price"),
                avg_price=Avg("price"),
                count=Count("id"),
            )
            # stats == {"total": 299.50, "avg_price": 14.97, "count": 20}
        """
        table = self._model._meta.table
        params = []

        # Build aggregate SELECT expressions
        select_parts = []
        aliases = []
        for alias, expr in kwargs.items():
            _validate_alias_name(alias, source="aggregate")
            if isinstance(expr, Expression):
                expr_sql, expr_params = expr.as_sql(len(params))
                params.extend(expr_params)
                select_parts.append(f"{expr_sql} AS {alias}")
            else:
                raise TypeError(
                    f"aggregate() values must be Expression instances, got {type(expr)}"
                )
            aliases.append(alias)

        sql = f"SELECT {', '.join(select_parts)} FROM {table}"

        # Apply WHERE (tree-based)
        where_tree = self._build_where_tree()
        where_sql, where_params, _ = where_tree.compile(start_idx=len(params) + 1)
        if where_sql:
            params.extend(where_params)
            sql += f" WHERE {where_sql}"

        db = self._get_db()
        row = await db.query_one(sql, *params)
        if row is None:
            # Return defaults for empty result sets
            result = {}
            for alias, expr in kwargs.items():
                if isinstance(expr, Aggregate):
                    result[alias] = expr.empty_result_set_value
                else:
                    result[alias] = None
            return result

        return _row_to_dict(row, aliases)

    # --- SQL generation ---

    def _build_select(self, columns_override=None):
        """Build a SELECT query with JOINs, annotations, GROUP BY.

        Two-tier cache architecture:
        - FAST PATH (cache hit): compute structural key from filter keys + mixin
          state, collect bind params directly — NO WhereNode tree allocation.
        - SLOW PATH (cache miss): build WhereNode tree, compile to SQL, cache it.

        Pre-allocates the parts list with estimated capacity, uses list-based
        SQL assembly with a single join at the end to avoid O(n²) concatenation.

        ``columns_override``: when given (e.g. ``"1"`` from ``exists()``), the
        SELECT list is emitted verbatim as that string and annotation columns
        are skipped — no full column list is built, no ``.upper()`` splicing.
        It participates in the compile cache key so the overridden variant
        caches independently from the normal SELECT.
        """
        meta = self._model._meta
        table = meta.table

        # Cacheable if no Expression annotations (which have runtime params)
        has_expr_annotations = self._annotations and any(
            isinstance(expr, Expression) for expr in self._annotations.values()
        )
        # Exists/NotExists filters compile correlated subqueries whose
        # SQL depends on the OUTER table name AND on the inner query's
        # per-call structure; they're not stable across cache hits.
        # Skip the fast-path compile cache entirely when any __exists__
        # entry is present in filters/excludes. Tracked incrementally on the
        # queryset (see filter()/exclude()) so this is a single bool read
        # instead of a per-SELECT `self._filters + self._excludes` scan.
        has_exists_filter = self._has_exists
        # CTE clauses inject a WITH prefix and their own parameters.
        # Skip the fast path so CTE params get collected + renumbered
        # correctly on every call.
        has_cte = bool(self._ctes)

        # ── FAST PATH: cache hit without tree construction ──
        if (
            not has_expr_annotations
            and not has_exists_filter
            and not has_cte
            and not self._has_inline_rhs
        ):
            cache_key = self._fast_cache_key(meta)
            if columns_override is not None:
                # Distinct cache namespace so "SELECT 1" variants never
                # collide with the full-column SELECT for the same shape.
                cache_key = ("__cols__", columns_override, cache_key)
            cached_sql = _compiled_sql_cache.get(cache_key)
            if cached_sql is not None:
                # CACHE HIT: collect params without building WhereNode tree.
                # LIMIT/OFFSET are trailing bound params (see the slow path) —
                # append them in the same order so $N numbering stays aligned.
                params = self._collect_where_params()
                if self._limit is not None:
                    params.append(self._limit)
                if self._offset is not None:
                    params.append(self._offset)
                return cached_sql, params

        # ── SLOW PATH: cache miss — full SQL generation ──
        joins: list[str] = []
        join_aliases: dict[str, str] = {}

        fk_paths = self._get_fk_filter_paths(include_order_by=True)
        if self._select_related or fk_paths:
            joins, join_aliases = self._resolve_joins(extra_paths=fk_paths)

        table_alias = table if join_aliases else None

        # _build_where_tree() already drops conditions that target an AGGREGATE
        # annotation alias (e.g. `.annotate(c=Count(...)).filter(c__gt=1)`) — an
        # aggregate can't appear in WHERE. It (and any mixin override) stays the
        # canonical WHERE path. The dropped conditions become the HAVING tree.
        where_tree = self._build_where_tree(table_alias, join_aliases)
        _, _, having_filters, having_excludes = self._partition_having_conditions()
        having_tree = None
        if having_filters or having_excludes:
            having_tree = self._build_condition_tree(
                having_filters,
                having_excludes,
                table_alias,
                join_aliases,
                include_raw=False,
            )

        params: list[BindValue] = []

        # CTE prefix (task #197) — prepend `WITH [RECURSIVE] name AS (body)`
        # clauses. Each clause's `{idx}` placeholders are substituted
        # left-to-right with consecutive `$N` indices; the clause
        # parameters are appended to `params` so that any downstream
        # `len(params)+1` offset calculations for annotations/WHERE
        # naturally flow after the CTE parameter positions.
        cte_prefix_sql = ""
        if self._ctes:
            recursive = any(c.recursive for c in self._ctes)
            cte_fragments: list[str] = []
            for clause in self._ctes:
                body = clause.body_sql
                for p in clause.params:
                    params.append(p)
                    body = body.replace("{idx}", f"${len(params)}", 1)
                cte_fragments.append(f"{clause.name} AS ({body})")
            keyword = "WITH RECURSIVE " if recursive else "WITH "
            cte_prefix_sql = keyword + ", ".join(cte_fragments) + " "

        # Build SELECT columns
        if columns_override is not None:
            # exists()-style probe: emit the literal SELECT list (e.g. "1")
            # and skip the annotation columns entirely — the caller only
            # cares whether a row matches, not about its values.
            columns = columns_override
        elif self._values_fields:
            select_cols = [
                self._qualify_column(f, table, join_aliases)
                for f in self._values_fields
            ]
            columns = ", ".join(select_cols)
        elif self._select_related:
            columns = self._build_joined_columns(table, join_aliases)
        else:
            col_names = meta.column_names
            if self._only is not None:
                # Always include the primary key — otherwise the hydrated
                # instance has no valid .pk (only() must never drop it),
                # matching Django. Preserve model column order.
                only_set = set(self._only)
                only_set.add(meta.pk_field)
                col_names = [c for c in col_names if c in only_set]
            elif self._defer is not None:
                defer_set = set(self._defer)
                col_names = [c for c in col_names if c not in defer_set]
            columns = (
                ", ".join(f"{table}.{c}" for c in col_names)
                if joins
                else ", ".join(col_names)
            )

        # Add annotation columns (skipped entirely for a columns_override probe)
        annotation_sql_parts: list[str] = []
        for alias, expr in (
            () if columns_override is not None else self._annotations.items()
        ):
            if isinstance(expr, Expression):
                expr_sql, expr_params = expr.as_sql(len(params))
                params.extend(expr_params)
                annotation_sql_parts.append(f"{expr_sql} AS {alias}")
            else:
                annotation_sql_parts.append(f"{expr} AS {alias}")

        if annotation_sql_parts:
            columns += ", " + ", ".join(annotation_sql_parts)

        # Assemble SQL parts list
        distinct = "DISTINCT " if self._distinct else ""
        parts = [f"SELECT {distinct}{columns} FROM {table}"]
        parts.extend(joins)

        # WHERE clause (compile tree → SQL + params)
        where_sql, where_params, _ = where_tree.compile(start_idx=len(params) + 1)
        if where_sql:
            params.extend(where_params)
            parts.append(f"WHERE {where_sql}")

        # GROUP BY
        if self._group_by is True and self._annotations:
            group_cols = self._auto_group_by_columns(table, join_aliases)
            if group_cols:
                parts.append(f"GROUP BY {', '.join(group_cols)}")
        elif isinstance(self._group_by, list) and self._group_by:
            parts.append(f"GROUP BY {', '.join(self._group_by)}")

        # HAVING (filters on aggregate annotation aliases) — comes after GROUP BY,
        # its params numbered after the WHERE params.
        if having_tree is not None:
            having_sql, having_params, _ = having_tree.compile(
                start_idx=len(params) + 1
            )
            if having_sql:
                # Postgres HAVING cannot reference a SELECT output alias, so
                # expand `c` back to its aggregate expression (`COUNT(id)`).
                having_sql = self._expand_aggregate_aliases(having_sql)
                params.extend(having_params)
                parts.append(f"HAVING {having_sql}")

        # ORDER BY
        if self._ordering:
            order_parts = []
            for field in self._ordering:
                desc = field.startswith("-")
                fname = field[1:] if desc else field
                col = self._qualify_column(fname, table, join_aliases)
                order_parts.append(f"{col} DESC" if desc else f"{col} ASC")
            parts.append(f"ORDER BY {', '.join(order_parts)}")

        # LIMIT/OFFSET are BOUND PARAMS, not inlined literals — otherwise every
        # distinct page (`LIMIT 20 OFFSET 40`, `OFFSET 60`, ...) compiles to a
        # different SQL string, blowing up both the ORM's compiled-SQL cache and
        # the fixed-size native query registry (one slot per page). Parameterized,
        # all pages of a query collapse to ONE `... LIMIT $n OFFSET $m` template.
        # They are appended AFTER the WHERE params so the $N numbering is
        # contiguous; the fast-path cache-hit branch mirrors this order.
        if self._limit is not None:
            params.append(self._limit)
            parts.append(f"LIMIT ${len(params)}")

        if self._offset is not None:
            params.append(self._offset)
            parts.append(f"OFFSET ${len(params)}")

        if self._for_update:
            parts.append(self._for_update.lstrip())

        sql = " ".join(parts)
        if cte_prefix_sql:
            sql = cte_prefix_sql + sql

        # Cache the compiled SQL template — but NOT if an Exists/NotExists
        # filter, a CTE, or an inline F()/subquery RHS was involved, since those
        # embed per-call SQL fragments or params that vary between invocations.
        if (
            not has_expr_annotations
            and not has_exists_filter
            and not has_cte
            and not self._has_inline_rhs
        ):
            _store_compiled(_compiled_sql_cache, cache_key, sql)

        return sql, params

    def _fast_cache_key(self, meta) -> tuple:
        """Compute structural cache key WITHOUT building a WhereNode tree.

        Compact path (common case: no values/only/defer/annotations/group_by/
        select_related/distinct/for_update): 4-element tuple.
        Full path (everything else): 11-element tuple.
        Different tuple lengths never collide in dict lookup.
        """
        is_common = (
            not self._values_fields
            and self._only is None
            and self._defer is None
            and not self._annotations
            and not self._select_related
            and not self._distinct
            and not self._for_update
            and not self._group_by
        )

        if is_common and self._offset is None:
            # Compact 4-element key — covers 90%+ of production queries
            # (no offset, no values/only/defer/annotations/group_by/
            #  select_related/distinct/for_update). LIMIT is a bound param now,
            # so only its PRESENCE (not its value) changes the SQL — key on the
            # bool so every `.limit(n)` shares one cached template.
            return (
                id(meta),
                self._fast_where_key(),
                self._ordering or (),
                self._limit is not None,
            )

        # Full key for complex queries
        if self._values_fields:
            col_key = ("v", tuple(self._values_fields))
        elif self._only is not None:
            col_key = ("o", tuple(sorted(self._only)))
        elif self._defer is not None:
            col_key = ("d", tuple(sorted(self._defer)))
        else:
            col_key = ()

        ann_key = (
            tuple((alias, str(expr)) for alias, expr in self._annotations.items())
            if self._annotations
            else ()
        )

        if self._group_by is True:
            gb_key: tuple = (True,)
        elif isinstance(self._group_by, list) and self._group_by:
            gb_key = tuple(self._group_by)
        else:
            gb_key = ()

        return (
            id(meta),
            col_key,
            tuple(sorted(self._select_related)) if self._select_related else (),
            self._fast_where_key(),
            ann_key,
            self._ordering or (),
            self._limit is not None,
            self._offset is not None,
            self._distinct,
            self._for_update or "",
            gb_key,
        )

    def _fast_where_key(self) -> int | tuple:
        """Structural fingerprint for WHERE clause only (no tree needed).

        Simple filters (no Q, no raw WHERE): Zig native FNV-1a hash.
        Q objects / raw WHERE: Python tuple key (Q needs recursive structural traversal).
        """
        if not self._has_q and not self._raw_wheres:
            # Zig native hash: iterates filter/exclude tuples directly, zero list alloc
            native_hash = _native_where_hash(self._filters, self._excludes)
            mixin = self._mixin_cache_key()
            return (native_hash, mixin) if mixin else native_hash

        # Q objects require recursive _structural_key() traversal
        filter_keys = tuple(
            ("__q__", v._structural_key()) if k == "__q__" else (k, _value_shape(v))
            for k, v in self._filters
        )
        exclude_keys = tuple(
            ("__q__", (~v)._structural_key()) if k == "__q__" else (k, _value_shape(v))
            for k, v in self._excludes
        )
        raw_keys = tuple(t for t, _ in self._raw_wheres)
        return (filter_keys, exclude_keys, raw_keys, self._mixin_cache_key())

    def _mixin_cache_key(self) -> tuple:
        """Mixin state contributing to cache key. Overridden by mixin QuerySets."""
        return ()

    _PASSTHROUGH_SUFFIXES = PASSTHROUGH_SUFFIXES

    def _collect_where_params(
        self, target: list[BindValue] | None = None
    ) -> list[BindValue]:
        """Collect WHERE bind params WITHOUT building a WhereNode tree.

        Fast-path for cache hits. Inlines passthrough lookups (exact, gt, gte,
        lt, lte, iexact, regex, iregex) as direct append — no bind_params()
        call, no list allocation. Uses rfind for suffix extraction (no rsplit list).

        If target is provided, appends to it (for UPDATE which has SET params first).
        """
        params = target if target is not None else []
        _passthrough = self._PASSTHROUGH_SUFFIXES

        for key, value in self._filters:
            if key == "__q__" and isinstance(value, Q):
                value._collect_bind_params(params)
            elif value is not None:
                sep = key.rfind("__")
                if sep < 0 or key[sep + 2 :] in _passthrough:
                    params.append(value)
                else:
                    params.extend(resolve_bind_params(key, value))
            else:
                params.extend(resolve_bind_params(key, value))

        for key, value in self._excludes:
            if key == "__q__" and isinstance(value, Q):
                value._collect_bind_params(params)
            elif value is not None:
                sep = key.rfind("__")
                if sep < 0 or key[sep + 2 :] in _passthrough:
                    params.append(value)
                else:
                    params.extend(resolve_bind_params(key, value))
            else:
                params.extend(resolve_bind_params(key, value))

        for _, raw_params in self._raw_wheres:
            params.extend(raw_params)

        self._collect_mixin_params(params)

        return params

    def _collect_mixin_params(self, params: list[BindValue]) -> None:
        """Append mixin-injected bind params. Overridden by mixin QuerySets."""
        pass

    def _build_count(self):
        """Build a COUNT query. Fast-path cached."""
        meta = self._model._meta
        table = meta.table

        # Inline F()/subquery RHS embeds per-call SQL/params — not fast-cacheable.
        use_cache = not self._has_inline_rhs
        if use_cache:
            # Fast-path cache key (no tree)
            count_key = (id(meta), self._fast_where_key())
            cached_sql = _compiled_count_cache.get(count_key)
            if cached_sql is not None:
                return cached_sql, self._collect_where_params()

        # Cache miss — build full SQL
        join_aliases: dict[str, str] = {}
        joins: list[str] = []
        fk_paths = self._get_fk_filter_paths()
        if self._select_related or fk_paths:
            joins, join_aliases = self._resolve_joins(extra_paths=fk_paths)

        table_alias = table if join_aliases else None
        where_tree = self._build_where_tree(table_alias, join_aliases)

        params: list[BindValue] = []
        parts = [f"SELECT COUNT(*) FROM {table}"]
        parts.extend(joins)

        where_sql, where_params, _ = where_tree.compile(start_idx=len(params) + 1)
        if where_sql:
            params.extend(where_params)
            parts.append(f"WHERE {where_sql}")

        sql = " ".join(parts)
        if use_cache:
            _store_compiled(_compiled_count_cache, count_key, sql)

        return sql, params

    def _build_update(self, values, returning: list[str] | None = None):
        """Build an UPDATE query with optional RETURNING clause.

        Cached by SET column names + WHERE structure + returning spec.
        """
        meta = self._model._meta
        table = meta.table

        # A slice (limit/offset) cannot be honored by a bare UPDATE — PostgreSQL
        # UPDATE has no LIMIT/OFFSET. Silently ignoring it would update EVERY
        # matching row (data-loss bug), so refuse it.
        if self._limit is not None or self._offset is not None:
            raise TypeError("Cannot update a query once a slice has been taken.")

        # Validate returning early (before cache lookup)
        if returning is not None and not returning:
            raise ValueError("returning must be a non-empty list of column names")

        # Validate every SET key and RETURNING column against the model's own
        # fields BEFORE they are interpolated as identifiers (and before the
        # compiled-SQL cache is consulted/populated — an unvalidated key would
        # otherwise poison the cache with injected SQL). This makes .update()
        # safe even when a caller spreads a user-controlled dict of column names,
        # and turns a typo into a clear error.
        valid_cols = meta.column_names
        for col in values:
            # Accept a field name ("author") or its column ("author_id" for FKs);
            # reject anything else (typo or an injected identifier).
            if col not in meta.fields and col not in valid_cols:
                raise ValueError(
                    f"update: {col!r} is not a field of {self._model.__name__}; "
                    f"valid fields: {sorted(meta.fields)}"
                )
        if returning is not None:
            for col in returning:
                if col not in valid_cols and col not in meta.fields:
                    raise ValueError(
                        f"update: returning column {col!r} is not a field of "
                        f"{self._model.__name__}"
                    )

        # Cache key: model + SET columns (not values) + WHERE structure + returning.
        # Skip cache when any SET value is an Expression (F, CombinedExpression)
        # OR the WHERE has an inline F()/subquery RHS — both embed per-call SQL.
        has_expressions = any(isinstance(v, Expression) for v in values.values())
        use_cache = not has_expressions and not self._has_inline_rhs
        if use_cache:
            set_cols = tuple(values.keys())
            ret_key = tuple(returning) if returning is not None else ()
            update_key = (id(meta), "U", set_cols, self._fast_where_key(), ret_key)
            cached_sql = _compiled_sql_cache.get(update_key)
            if cached_sql is not None:
                # Cache hit: collect SET values + WHERE params
                params = list(values.values())
                self._collect_where_params(target=params)
                return cached_sql, params

        # Cache miss: build full SQL
        params: list[BindValue] = []
        set_parts = []
        for col, val in values.items():
            if isinstance(val, Expression):
                expr_sql, expr_params = val.as_sql(len(params))
                params.extend(expr_params)
                set_parts.append(f"{col} = {expr_sql}")
            else:
                params.append(val)
                set_parts.append(f"{col} = ${len(params)}")

        parts = [f"UPDATE {table} SET {', '.join(set_parts)}"]

        where_tree = self._build_where_tree()
        where_sql, where_params, _ = where_tree.compile(start_idx=len(params) + 1)
        if where_sql:
            params.extend(where_params)
            parts.append(f"WHERE {where_sql}")

        if returning is not None:
            parts.append(f"RETURNING {', '.join(returning)}")

        sql = " ".join(parts)
        if use_cache:
            _store_compiled(_compiled_sql_cache, update_key, sql)
        return sql, params

    def _build_delete(self):
        """Build a DELETE query. Cached by WHERE structure."""
        meta = self._model._meta
        table = meta.table

        # A slice (limit/offset) cannot be honored by a bare DELETE — PostgreSQL
        # DELETE has no LIMIT/OFFSET. Silently ignoring it would delete EVERY
        # matching row (data-loss bug), so refuse it.
        if self._limit is not None or self._offset is not None:
            raise TypeError("Cannot delete a query once a slice has been taken.")

        # Cache key: model + WHERE structure. Inline F()/subquery RHS embeds
        # per-call SQL/params, so it is not fast-cacheable.
        use_cache = not self._has_inline_rhs
        if use_cache:
            delete_key = (id(meta), "D", self._fast_where_key())
            cached_sql = _compiled_sql_cache.get(delete_key)
            if cached_sql is not None:
                return cached_sql, self._collect_where_params()

        # Cache miss: build full SQL
        params: list[BindValue] = []
        parts = [f"DELETE FROM {table}"]

        where_tree = self._build_where_tree()
        where_sql, where_params, _ = where_tree.compile(start_idx=len(params) + 1)
        if where_sql:
            params.extend(where_params)
            parts.append(f"WHERE {where_sql}")

        sql = " ".join(parts)
        if use_cache:
            _store_compiled(_compiled_sql_cache, delete_key, sql)
        return sql, params

    def _nullable_columns(self) -> frozenset[str]:
        """Cached set of this model's nullable DB column names."""
        key = id(self._model)
        cached = _nullable_cols_cache.get(key)
        if cached is not None:
            return cached
        result = _compute_nullable_columns(self._model)
        with _nullable_cols_lock:
            _nullable_cols_cache[key] = result
        return result

    def _aggregate_annotation_aliases(self) -> set[str]:
        """Annotation aliases whose expression is an aggregate (Count/Sum/...)."""
        return {
            alias
            for alias, expr in self._annotations.items()
            if isinstance(expr, Expression) and expr.contains_aggregate
        }

    def _expand_aggregate_aliases(self, sql: str) -> str:
        """Replace aggregate-annotation aliases in a HAVING fragment with their
        SQL expression (Postgres forbids output-alias references in HAVING).

        Substitutes all aliases in a single pass so a replacement is never
        re-scanned. Aggregates whose expression binds parameters aren't
        supported here (they'd duplicate/reorder params) — raise clearly.
        """
        agg = self._aggregate_annotation_aliases()
        if not agg:
            return sql
        expr_map: dict[str, str] = {}
        for alias in agg:
            expr = self._annotations.get(alias)
            expr_sql, expr_params = expr.as_sql(0)
            if expr_params:
                raise NotImplementedError(
                    f"filter() on aggregate annotation '{alias}' whose "
                    f"expression binds parameters is not supported on this "
                    f"branch (HAVING would duplicate its parameters)."
                )
            expr_map[alias] = expr_sql
        pattern = r"\b(" + "|".join(re.escape(a) for a in expr_map) + r")\b"
        return re.sub(pattern, lambda m: expr_map[m.group(1)], sql)

    def _partition_having_conditions(self):
        """Split filters/excludes into (where, having) by aggregate-alias usage.

        A simple leaf condition whose column is an aggregate annotation alias
        (e.g. ``filter(c__gt=1)`` after ``annotate(c=Count("id"))``) must go to
        HAVING, not WHERE. Q/Exists entries and plain-column conditions stay in
        WHERE. Returns ``(where_filters, where_excludes, having_filters,
        having_excludes)``.
        """
        agg = self._aggregate_annotation_aliases()
        if not agg:
            return self._filters, self._excludes, [], []

        def _is_agg_key(key: str) -> bool:
            return not key.startswith("__") and key.split("__", 1)[0] in agg

        wf, hf = [], []
        for entry in self._filters:
            (hf if _is_agg_key(entry[0]) else wf).append(entry)
        we, he = [], []
        for entry in self._excludes:
            (he if _is_agg_key(entry[0]) else we).append(entry)
        return wf, we, hf, he

    def _build_where_tree(
        self,
        table_alias: str | None = None,
        join_aliases: dict[str, str] | None = None,
    ) -> WhereNode:
        """Build the WHERE clause as a composable WhereNode tree.

        Excludes conditions that target an aggregate annotation alias — those
        belong in HAVING and are built separately by the SELECT compiler. Mixin
        QuerySets override this to append their own WHERE conditions as children,
        so its signature must stay ``(table_alias, join_aliases)``.

        No param indexing here — that happens during WhereNode.compile().
        """
        where_filters, where_excludes, _, _ = self._partition_having_conditions()
        return self._build_condition_tree(
            where_filters, where_excludes, table_alias, join_aliases, include_raw=True
        )

    def _build_condition_tree(
        self,
        filters,
        excludes,
        table_alias: str | None = None,
        join_aliases: dict[str, str] | None = None,
        include_raw: bool = True,
    ) -> WhereNode:
        """Build a WhereNode tree from explicit filter/exclude lists.

        Shared by WHERE (via ``_build_where_tree``) and HAVING (SELECT compiler).
        NOT overridden by mixins, so the HAVING tree never inherits a mixin's
        WHERE-only condition. ``include_raw`` appends raw WHERE fragments (WHERE
        only). Negated leaves get Django-style NULL demotion for nullable columns.
        """
        root = WhereNode(connector="AND")
        annotation_aliases = set(self._annotations.keys())
        nullable = self._nullable_columns()
        # Outer table name used for OuterRef substitution in Exists subqueries.
        outer_table = table_alias or self._model._meta.table

        for key, value in filters:
            if key == "__q__" and isinstance(value, Q):
                q_node = value.to_node(
                    table_alias, join_aliases, annotation_aliases, nullable
                )
                if not q_node.is_empty:
                    root.children.append(q_node)
            elif key == "__exists__":
                # Exists / NotExists correlated subquery — compile the
                # inner queryset with OuterRef values resolved to
                # outer_table.field references.
                exists_node = self._compile_exists_filter(
                    value, outer_table, negate=False
                )
                root.children.append(exists_node)
            else:
                node = resolve_lookup_node(
                    key, value, table_alias, join_aliases, annotation_aliases
                )
                root.children.append(node)

        for key, value in excludes:
            if key == "__q__" and isinstance(value, Q):
                negated = ~value
                q_node = negated.to_node(
                    table_alias, join_aliases, annotation_aliases, nullable
                )
                if not q_node.is_empty:
                    root.children.append(q_node)
            elif key == "__exists__":
                # exclude(Exists(...)) → NOT EXISTS (...) at compile time.
                exists_node = self._compile_exists_filter(
                    value, outer_table, negate=True
                )
                root.children.append(exists_node)
            else:
                node = resolve_exclude_node(
                    key, value, table_alias, join_aliases, annotation_aliases, nullable
                )
                root.children.append(node)

        # Raw WHERE fragments: convert {idx} placeholders to {}
        if include_raw:
            for sql_template, raw_params in self._raw_wheres:
                template = sql_template
                for _ in raw_params:
                    template = template.replace("{idx}", "{}", 1)
                root.children.append(
                    WhereNode(template=template, bind_values=list(raw_params))
                )

        return root

    def _compile_exists_filter(
        self, exists_expr, outer_table: str, negate: bool
    ) -> WhereNode:
        """Compile an `Exists` / `NotExists` expression into a WhereNode.

        The inner queryset is compiled to SQL with any `OuterRef("field")`
        values in its filter list replaced by raw SQL fragments of the
        form `outer_table.field`. The resulting `$N` parameter markers
        are rewritten to `{}` placeholders so the outer WhereNode's
        compile can renumber them against the outer query's bind params.
        """
        from hyperdjango.expressions import NotExists, OuterRef

        # Determine negation from both exclude() semantics AND from any
        # pre-existing NotExists class wrap (user did `~Exists(...)`).
        effective_negate = negate
        if isinstance(exists_expr, NotExists):
            effective_negate = not effective_negate

        inner_qs = exists_expr.queryset

        # Detect OuterRef values in the inner queryset's filter list.
        # Replace each with a unique raw-SQL sentinel string that
        # survives the normal compile, then substitute the sentinel
        # for `outer_table.field` in the final SQL.
        #
        # Sentinel tokens include a 128-bit per-call random nonce so
        # that no user-supplied filter value can ever collide with
        # them — even adversarial input that hard-codes the literal
        # `__HYPER_OUTERREF_*__` prefix. Without the nonce, passing
        # `title="__HYPER_OUTERREF_1__"` as a filter value would make
        # `sub_params.index(token)` return the user's value instead of
        # the injected sentinel, scrambling the OuterRef substitution;
        # the per-call nonce eliminates the collision risk entirely.
        # Regression-covered by `scripts/test_orm_subquery_fuzz.py`.
        nonce = secrets.token_hex(16)
        patched_filters = []
        patched_excludes = []
        sentinels: list[tuple[str, str]] = []  # (sentinel_token, sql_fragment)
        counter = [0]

        def _make_sentinel(field_name: str) -> str:
            counter[0] += 1
            token = f"__HYPER_OUTERREF_{nonce}_{counter[0]}__"
            sentinels.append((token, f"{outer_table}.{field_name}"))
            return token

        def _scan_value(val):
            if isinstance(val, OuterRef):
                return _make_sentinel(val.field)
            return val

        for k, v in inner_qs._filters:
            if k == "__q__" and isinstance(v, Q):
                # Q subtree — walk its children for OuterRefs too
                patched_filters.append((k, _walk_q_for_outerref(v, _make_sentinel)))
            else:
                patched_filters.append((k, _scan_value(v)))

        for k, v in inner_qs._excludes:
            if k == "__q__" and isinstance(v, Q):
                patched_excludes.append((k, _walk_q_for_outerref(v, _make_sentinel)))
            else:
                patched_excludes.append((k, _scan_value(v)))

        # Build a sibling queryset with patched filters — avoid mutating
        # the user's original queryset.
        patched = inner_qs._clone(filters=patched_filters, excludes=patched_excludes)
        sub_sql, sub_params = patched._build_select()

        # Replace OuterRef sentinels with `outer_table.field` raw SQL.
        for token, sql_fragment in sentinels:
            # The sentinel appears as a bound string param in sub_sql
            # and in sub_params. Remove it from params and splice the
            # SQL fragment directly into sub_sql at the param position.
            # Strategy: find `$N` placeholders whose corresponding param
            # is the sentinel, and replace with the raw fragment.
            try:
                param_idx = sub_params.index(token)
            except ValueError:
                continue
            sub_params.pop(param_idx)
            # Rewrite sub_sql: replace `$(param_idx+1)` with the SQL
            # fragment, and shift higher-indexed `$N` down by one.
            target_marker = f"${param_idx + 1}"
            # Replace only once — the sentinel appears exactly once.
            sub_sql = sub_sql.replace(target_marker, sql_fragment, 1)

            # Renumber later `$N` markers down by 1 — anything with
            # N > param_idx + 1 becomes N - 1.
            def _shift(m, cutoff=param_idx + 1):
                n = int(m.group(1))
                if n > cutoff:
                    return f"${n - 1}"
                return m.group(0)

            sub_sql = re.sub(r"\$(\d+)", _shift, sub_sql)

        # Build a template with `{}` placeholders that outer compile()
        # will renumber to the right `$N` offsets.
        # After sentinel substitution, sub_sql's remaining `$N` markers
        # are real parameters that need to become `{}`.
        def _to_brace(m, seen=[0]):  # noqa: B006 — intentional mutable default as counter
            seen[0] += 1
            return "{}"

        sub_template = re.sub(r"\$\d+", _to_brace, sub_sql)

        prefix = "NOT EXISTS" if effective_negate else "EXISTS"
        return WhereNode(
            template=f"{prefix} ({sub_template})",
            bind_values=list(sub_params),
        )

    # --- JOIN resolution ---

    def _collect_filter_keys(self) -> list[str]:
        """Collect all leaf column keys from filters/excludes, descending Q trees.

        Q leaves (and grouped exclude kwargs, which are stored as ``__q__``
        entries) contribute their keys too, so FK-spanning inside Q — e.g.
        ``filter(Q(author__name="x"))`` or ``exclude(author__name="x")`` —
        resolves the JOIN just like a plain ``filter(author__name="x")``.
        """
        keys: list[str] = []

        def _walk_q(q):
            for child in q.children:
                if isinstance(child, Q):
                    _walk_q(child)
                else:
                    keys.append(child[0])

        for key, value in list(self._filters) + list(self._excludes):
            if key == "__q__" and isinstance(value, Q):
                _walk_q(value)
            elif not key.startswith("__"):
                keys.append(key)
        return keys

    def _is_m2m_field(self, name: str) -> bool:
        """True if ``name`` is a ManyToManyField descriptor on the model."""
        # dynamic-attr: probing the model class for a relation attribute named by a runtime filter-key segment, to detect an M2M descriptor
        desc = getattr(self._model, name, None)
        # dynamic-attr: `_is_m2m` is a marker present only on ManyToManyField descriptors, absent on ordinary fields/attributes
        return desc is not None and getattr(desc, "_is_m2m", False)

    def _fk_paths_for_key(self, key: str, fk_paths: set[str]) -> None:
        """Add the FK-span path prefixes of a single column key to ``fk_paths``."""
        parts = key.split("__")
        # M2M-spanning filters (e.g. filter(tags__name="x")) need a junction
        # JOIN the single-hop FK machinery can't express. Fail loudly with a
        # clear message instead of emitting invalid SQL (`table.tags__name`).
        if self._is_m2m_field(parts[0]):
            raise NotImplementedError(
                f"Relation-spanning across the many-to-many field "
                f"'{parts[0]}' (in filter {key!r}) is not supported on this "
                f"branch. Query the related model directly, or use "
                f"`field__in=<QuerySet/Subquery>` against the target's PKs."
            )
        # A single-part key is a scalar column comparison — never a JOIN.
        if len(parts) == 1:
            return
        current_fields = self._model._meta.fields
        accumulated = ""
        for i, part in enumerate(parts):
            field_meta = current_fields.get(part)
            # FK shorthand: "author" maps to the "author_id" FK column.
            if (
                not field_meta or not field_meta.foreign_key
            ) and f"{part}_id" in current_fields:
                field_meta = current_fields.get(f"{part}_id")
            if field_meta and field_meta.foreign_key:
                # Only a FK part FOLLOWED by another part is a real hop into
                # the related model; the last part is a terminal column.
                if i < len(parts) - 1:
                    accumulated = f"{accumulated}__{part}" if accumulated else part
                    fk_paths.add(accumulated)
                related_model = _get_model_by_table(field_meta.foreign_key)
                if related_model:
                    current_fields = related_model._meta.fields
                else:
                    break
            else:
                break

    def _order_by_fk_keys(self) -> list[str]:
        """Column keys referenced by ORDER BY (with the ``-`` DESC prefix stripped)."""
        if not self._ordering:  # None / empty when no order_by() was applied
            return []
        return [f.removeprefix("-") for f in self._ordering]

    def _get_fk_filter_paths(self, include_order_by: bool = False) -> list[str]:
        """Extract FK-spanning field paths from filters/excludes (+ order_by).

        For filter(author__name="Alice"), returns ["author"].
        For filter(author__publisher__name="O'Reilly"), returns
        ["author", "author__publisher"]. With ``include_order_by=True`` (used by
        SELECT), ``order_by("author__name")`` adds the same JOIN so the ORDER BY
        can qualify to the joined column. COUNT/UPDATE/DELETE pass False — they
        have no ORDER BY and must not add spurious joins.

        Scalar FK comparisons like ``filter(author_id=1)`` do NOT generate a
        JOIN — only traversal INTO the related model does.
        """
        fk_paths: set[str] = set()
        keys = self._collect_filter_keys()
        if include_order_by:
            keys = keys + self._order_by_fk_keys()
        for key in keys:
            self._fk_paths_for_key(key, fk_paths)
        return list(fk_paths)

    def _resolve_joins(
        self, extra_paths: list[str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """Resolve select_related fields + FK filter paths into LEFT JOIN clauses.

        Returns:
            (list_of_join_sql_strings, {field_path: table_alias})
        """
        joins = []
        aliases = {}
        alias_counter = [0]

        all_paths = list(self._select_related)
        if extra_paths:
            for p in extra_paths:
                if p not in all_paths:
                    all_paths.append(p)

        for field_path in all_paths:
            parts = field_path.split("__")
            current_model = self._model
            current_alias = self._model._meta.table
            accumulated_path = ""

            for part in parts:
                accumulated_path = (
                    f"{accumulated_path}__{part}" if accumulated_path else part
                )

                if accumulated_path in aliases:
                    # Already joined
                    current_alias = aliases[accumulated_path]
                    # Resolve the model for this path (try both "author" and "author_id")
                    field_meta = current_model._meta.fields.get(
                        part
                    ) or current_model._meta.fields.get(f"{part}_id")
                    if field_meta and field_meta.foreign_key:
                        related_model = _get_model_by_table(field_meta.foreign_key)
                        if related_model:
                            current_model = related_model
                    continue

                # Find the FK field (try both "author" and "author_id")
                field_meta = current_model._meta.fields.get(part)
                fk_col_name = part
                if not field_meta or not field_meta.foreign_key:
                    field_meta = current_model._meta.fields.get(f"{part}_id")
                    fk_col_name = f"{part}_id"
                if not field_meta or not field_meta.foreign_key:
                    raise ValueError(
                        f"Cannot resolve join for '{field_path}': "
                        f"'{part}' is not a FK field on {current_model.__name__}"
                    )

                # Look up related model
                related_model = _get_model_by_table(field_meta.foreign_key)
                if related_model is None:
                    raise ValueError(
                        f"Cannot resolve select_related('{field_path}'): "
                        f"no model registered for table '{field_meta.foreign_key}'"
                    )

                # Generate alias
                alias_counter[0] += 1
                alias = f"t{alias_counter[0]}"
                aliases[accumulated_path] = alias

                # Find the PK of the related model
                related_pk = related_model._meta.pk_field

                # Generate LEFT JOIN (use actual FK column name, e.g., author_id not author)
                join_sql = (
                    f"LEFT JOIN {related_model._meta.table} AS {alias} "
                    f"ON {current_alias}.{fk_col_name} = {alias}.{related_pk}"
                )
                joins.append(join_sql)

                current_model = related_model
                current_alias = alias

        return joins, aliases

    def _build_joined_columns(
        self, main_table: str, join_aliases: dict[str, str]
    ) -> str:
        """Build SELECT column list for select_related query.

        Includes all columns from main table + all columns from each joined table,
        with unique aliases to prevent name collisions.
        """
        parts = []

        # Main table columns
        for col in self._model._meta.column_names:
            parts.append(f"{main_table}.{col}")

        # Joined table columns (with alias prefix)
        for field_path, alias in join_aliases.items():
            related_table = None
            # Walk the path to find the related model
            current_model = self._model
            for part in field_path.split("__"):
                field_meta = current_model._meta.fields.get(part)
                if field_meta and field_meta.foreign_key:
                    related_model = _get_model_by_table(field_meta.foreign_key)
                    if related_model:
                        current_model = related_model

            # Add all columns from the related model
            for col in current_model._meta.column_names:
                parts.append(f"{alias}.{col} AS {field_path}__{col}")

        return ", ".join(parts)

    def _populate_select_related(self, rows) -> list[object]:
        """Convert rows with joined data back into nested model instances.

        Two attach modes:

        1. **Plain select_related** (default): the FK column value on
           the parent instance is REPLACED with the related model
           instance. `book.author_id` → Author instance.
        2. **join_related alias**: the related instance is attached
           on a SIBLING attribute name (from `_join_related_aliases`)
           and the FK column value on the parent is left untouched.
           `ticket.status_id` stays int, `ticket.status` becomes
           TicketStatusConfig.
        """
        result = []
        main_model = self._model
        main_cols = main_model._meta.column_names
        main_count = len(main_cols)
        main_from_record = main_model.from_record

        aliases = self._join_related_aliases

        # Precompute, ONCE per query (not per row × per related model), the
        # invariant hydration plan for every select_related path:
        #   - the related model's from_record fast-path constructor
        #   - the related column tuple (hoisted out of the row loop — #3)
        #   - the fully-qualified row keys (``"path__col"``) so the row loop
        #     does no per-row f-string formatting
        #   - alias / parent-parts attach target
        related_plan = []
        for field_path in self._select_related:
            parts = field_path.split("__")
            current_model = main_model
            for part in parts:
                field_meta = current_model._meta.fields.get(part)
                if field_meta and field_meta.foreign_key:
                    related_model = _get_model_by_table(field_meta.foreign_key)
                    if related_model:
                        current_model = related_model
            related_cols = current_model._meta.column_names
            row_keys = tuple(f"{field_path}__{col}" for col in related_cols)
            related_plan.append(
                (
                    current_model.from_record,
                    related_cols,
                    row_keys,
                    aliases.get(field_path),
                    parts,
                )
            )

        for row in rows:
            row_is_dict = isinstance(row, dict)
            # Build main instance from first N columns — slice once, feed the
            # slice through from_record's fast path (object.__new__ + dict
            # update, no per-field validation) instead of a full __init__.
            if row_is_dict:
                main_data = {col: row.get(col) for col in main_cols}
            else:
                main_data = dict(zip(main_cols, row[:main_count]))
            instance = main_from_record(main_data)

            # Attach related instances
            for (
                related_from_record,
                related_cols,
                row_keys,
                alias,
                parts,
            ) in related_plan:
                related_data = {}
                all_null = True

                if row_is_dict:
                    for col, key in zip(related_cols, row_keys):
                        val = row.get(key)
                        related_data[col] = val
                        if val is not None:
                            all_null = False
                else:
                    for col, key in zip(related_cols, row_keys):
                        val = _row_val_by_key(row, key)
                        related_data[col] = val
                        if val is not None:
                            all_null = False

                # Build nested instance (or None if all NULLs = no related row).
                # from_record hydrates via the same fast path as the main row.
                related_instance = (
                    None if all_null else related_from_record(related_data)
                )

                # Decide the attach attribute: plain select_related
                # overwrites the FK column; join_related attaches on
                # the alias sibling attribute leaving the FK int intact.
                if alias is not None:
                    # join_related path — attach on sibling attribute,
                    # do NOT touch the FK column on the instance.
                    # dynamic-attr: attaching the joined instance onto a user-model attribute named by the runtime join alias
                    setattr(instance, alias, related_instance)
                    continue

                # Plain select_related — set on the right parent
                # (handles nested paths like "author__publisher").
                target = instance
                for part in parts[:-1]:
                    # dynamic-attr: walking a user-supplied select_related path (e.g. "author__publisher") across user model instances' relation attributes
                    target = getattr(target, part, None)
                    if target is None:
                        break
                if target is not None:
                    # dynamic-attr: attaching the related instance onto the user model attribute named by the final select_related path segment
                    setattr(target, parts[-1], related_instance)

            result.append(instance)

        return result

    def _populate_results(self, rows) -> list[object]:
        """Convert rows to model instances, attaching annotation values."""
        if not self._annotations:
            # Hoist the bound classmethod out of the comprehension so the
            # attribute lookup happens once, not once per row.
            from_record = self._model.from_record
            return [from_record(row) for row in rows]

        result = []
        col_names = self._model._meta.column_names
        col_count = len(col_names)
        from_record = self._model.from_record
        annotation_aliases = list(self._annotations.keys())

        for row in rows:
            # Build instance from model columns — slice the model's columns
            # out of the (columns + annotations) row and hydrate via the
            # from_record fast path, then attach annotations as attributes.
            if isinstance(row, dict):
                model_data = {c: row.get(c) for c in col_names}
                instance = from_record(model_data)
                for alias in annotation_aliases:
                    # dynamic-attr: attaching an annotation value onto a user model instance under the runtime annotation alias
                    setattr(instance, alias, row.get(alias))
            else:
                model_data = dict(zip(col_names, row[:col_count]))
                instance = from_record(model_data)
                row_len = len(row)
                for i, alias in enumerate(annotation_aliases):
                    pos = col_count + i
                    val = row[pos] if pos < row_len else None
                    # dynamic-attr: attaching an annotation value onto a user model instance under the runtime annotation alias
                    setattr(instance, alias, val)

            result.append(instance)

        return result

    # --- Prefetch execution ---

    async def _execute_prefetch(self, instances: list):
        """Execute prefetch_related queries and attach results to instances."""
        db = self._get_db()

        for field_path in self._prefetch_related:
            parts = field_path.split("__")
            # For now, handle single-level prefetch
            field_name = parts[0]

            # Check if it's a M2M descriptor
            # dynamic-attr: probing the user model class for a relation attribute named by the runtime prefetch path segment, to detect an M2M descriptor
            m2m_desc = getattr(self._model, field_name, None)
            if m2m_desc is not None and hasattr(m2m_desc, "_is_m2m"):
                await self._prefetch_m2m(instances, field_name, m2m_desc, db)
                continue

            # Reverse FK prefetch: look for a model with an FK pointing to us
            related_model = self._find_reverse_fk_model(field_name)
            if related_model is None:
                raise ValueError(
                    f"Cannot resolve prefetch_related('{field_path}'): "
                    f"no reverse FK or M2M found for '{field_name}' on {self._model.__name__}"
                )

            # Find the FK field on the related model that points to our table.
            # Pass the prefetch name so a model with two FKs to us (e.g.
            # sender_id/recipient_id) resolves to the FK whose related_name
            # matches this prefetch, not just the first FK by iteration order.
            fk_field_name = self._find_fk_field(
                related_model, self._model._meta.table, field_name
            )
            if fk_field_name is None:
                raise ValueError(
                    f"Cannot resolve prefetch_related('{field_path}'): "
                    f"no FK field on {related_model.__name__} pointing to {self._model._meta.table}"
                )

            # Collect parent PKs
            pk_field = self._model._meta.pk_field
            # dynamic-attr: reading each user model instance's PK column, whose name (pk_field) is only known at runtime
            parent_pks = [getattr(inst, pk_field) for inst in instances]
            if not parent_pks:
                continue

            # Query related objects in batch — use IN with individual params
            related_table = related_model._meta.table
            related_cols = ", ".join(related_model._meta.column_names)
            placeholders = ", ".join(f"${i + 1}" for i in range(len(parent_pks)))
            sql = f"SELECT {related_cols} FROM {related_table} WHERE {fk_field_name} IN ({placeholders})"
            rows = await db.query(sql, *parent_pks)

            # Group by FK value
            related_by_fk: dict[Any, list] = {}
            fk_idx = related_model._meta.column_names.index(fk_field_name)
            for row in rows:
                related_inst = related_model.from_record(row)
                fk_val = _row_val(row, fk_idx)
                related_by_fk.setdefault(fk_val, []).append(related_inst)

            # Attach to parent instances
            for inst in instances:
                # dynamic-attr: reading each user model instance's PK column, whose name (pk_field) is only known at runtime
                pk_val = getattr(inst, pk_field)
                related_list = related_by_fk.get(pk_val, [])
                # dynamic-attr: attaching the prefetched list onto the user model instance under the runtime reverse-relation name
                setattr(inst, field_name, related_list)

    async def _prefetch_m2m(self, instances, field_name, descriptor, db):
        """Prefetch M2M related objects."""
        # Class-level descriptor access (getattr on the model class) returns the
        # descriptor WITHOUT resolving its target model, so `_target_model` may
        # still be None when the target was registered after the source. Resolve
        # it here (mirrors M2MManager.all()) to avoid a NoneType._meta crash.
        descriptor._ensure_target()
        pk_field = self._model._meta.pk_field
        # dynamic-attr: reading each user model instance's PK column, whose name (pk_field) is only known at runtime
        parent_pks = [getattr(inst, pk_field) for inst in instances]
        if not parent_pks:
            return

        junction_table = descriptor._junction_table
        source_col = descriptor._source_col
        target_col = descriptor._target_col
        target_model = descriptor._target_model

        # Query junction + target in a single JOIN
        target_table = target_model._meta.table
        target_pk = target_model._meta.pk_field
        target_cols = ", ".join(
            f"{target_table}.{c}" for c in target_model._meta.column_names
        )

        placeholders = ", ".join(f"${i + 1}" for i in range(len(parent_pks)))
        # Alias the junction source column: on a name collision with a target
        # column (self-referential M2M / overlapping names) the projected row
        # would otherwise carry two identically-named keys and a dict row would
        # keep only one — corrupting both the grouping key and the target
        # hydration. The unique alias keeps the source PK addressable.
        sql = (
            f"SELECT {junction_table}.{source_col} AS __hyper_m2m_src, {target_cols} "
            f"FROM {junction_table} "
            f"JOIN {target_table} ON {junction_table}.{target_col} = {target_table}.{target_pk} "
            f"WHERE {junction_table}.{source_col} = ANY(ARRAY[{placeholders}])"
        )
        rows = await db.query(sql, *parent_pks)

        # Group by source FK
        related_by_source: dict[Any, list] = {}
        target_cols = target_model._meta.column_names  # hoisted (#3)
        target_from_record = target_model.from_record
        for row in rows:
            # Slice the target model's columns out of the junction+target row
            # (index 0 / the "__hyper_m2m_src" alias is the junction source_col)
            # and hydrate via from_record.
            if isinstance(row, dict):
                source_pk = row.get("__hyper_m2m_src")
                target_data = {col: row.get(col) for col in target_cols}
            else:
                source_pk = _row_val(row, 0)
                row_len = len(row)
                target_data = {
                    col: (row[1 + i] if 1 + i < row_len else None)
                    for i, col in enumerate(target_cols)
                }
            target_inst = target_from_record(target_data)
            related_by_source.setdefault(source_pk, []).append(target_inst)

        # Attach to instances
        for inst in instances:
            # dynamic-attr: reading each user model instance's PK column, whose name (pk_field) is only known at runtime
            pk_val = getattr(inst, pk_field)
            # dynamic-attr: writing the framework-injected M2M prefetch cache attribute whose name is built from the runtime field name
            setattr(inst, f"_{field_name}_cache", related_by_source.get(pk_val, []))

    def _find_reverse_fk_model(self, attr_name: str) -> type | None:
        """Find a model that has an FK pointing to us, where the reverse name matches."""
        # Check if the model has a _reverse_relations registry
        # dynamic-attr: optional framework registry attribute a user model may carry; absent on ordinary models
        reverse = getattr(self._model, "_reverse_relations", None)
        if reverse and attr_name in reverse:
            return reverse[attr_name]

        # Brute-force: scan all registered models
        our_table = self._model._meta.table
        with _model_registry_lock:
            registry_snapshot = list(_model_registry.items())
        for table_name, model_cls in registry_snapshot:
            # Check if model name (lowercased + "s") matches attr_name
            model_name_lower = model_cls.__name__.lower() + "s"
            if (
                model_name_lower == attr_name
                or model_cls.__name__.lower() + "_set" == attr_name
            ):
                # Check if this model has an FK to our table
                for fname, fmeta in model_cls._meta.fields.items():
                    if fmeta.foreign_key == our_table:
                        return model_cls

        return None

    @staticmethod
    def _find_fk_field(
        model_cls, target_table: str, relation_name: str | None = None
    ) -> str | None:
        """Find the FK field name on model_cls that points to target_table.

        When model_cls has MULTIPLE FKs to the same target table (e.g. a Message
        with both sender_id and recipient_id → users), returning the first by
        iteration order silently attaches the wrong related set. Disambiguate by
        the reverse relation name being prefetched: pick the FK whose declared
        ``related_name`` equals ``relation_name``. Raise if still ambiguous so
        the caller sees a clear error instead of wrong prefetch results.
        """
        fks = [
            fname
            for fname, fmeta in model_cls._meta.fields.items()
            if fmeta.foreign_key == target_table
        ]
        if not fks:
            return None
        if len(fks) == 1:
            return fks[0]
        # Multiple FKs to the same target — must disambiguate by related_name.
        if relation_name is not None:
            for fname in fks:
                if model_cls._meta.fields[fname].related_name == relation_name:
                    return fname
        raise ValueError(
            f"{model_cls.__name__} has multiple foreign keys to "
            f"{target_table} ({', '.join(fks)}); cannot resolve reverse "
            f"prefetch{f' {relation_name!r}' if relation_name else ''} "
            "unambiguously. Set a distinct related_name on each FK matching "
            "the prefetch_related() name."
        )

    # --- Column qualification ---

    def _qualify_column(
        self, col: str, main_table: str | None, join_aliases: dict[str, str]
    ) -> str:
        """Qualify a column name with the correct table alias.

        Delegates to the lookups module for consistent FK-span resolution.
        """
        return _lookup_qualify_column(
            col,
            main_table,
            join_aliases,
            set(self._annotations.keys()),
        )

    def _auto_group_by_columns(
        self, main_table: str | None, join_aliases: dict[str, str]
    ) -> list[str]:
        """Auto-generate GROUP BY columns: all non-aggregate SELECT columns."""
        group_cols = []

        if self._values_fields:
            # GROUP BY the values() fields
            for f in self._values_fields:
                group_cols.append(
                    self._qualify_column(f, main_table, join_aliases)
                    if main_table
                    else f
                )
        else:
            # GROUP BY all model columns
            prefix = f"{main_table}." if main_table else ""
            for col in self._model._meta.column_names:
                group_cols.append(f"{prefix}{col}")

        return group_cols

    def _get_db(self, for_write: bool = False):
        """Resolve the database connection for this query.

        Priority:
        1. Explicit .using() — string alias or Database instance
        2. Model Meta.database — per-model binding
        3. ConnectionManager router — read/write routing (primary for writes)
        4. Global default database

        Args:
            for_write: If True, route to primary (not replica) for write operations.
        """
        # Explicit .using()
        if self._using is not None:
            if isinstance(self._using, str):
                return get_connections()[self._using]
            # Database instance or any object with query/execute interface
            return self._using

        # Per-model binding or router. Resolve the LIVE connection-manager
        # singleton at call time — importing `_connections` by value at module
        # load captured None (the pre-configuration state) and never observed
        # a later set_connections()/configure(), silently disabling read/write
        # routing. get_connections() returns the live singleton.
        try:
            conns = get_connections()
            if conns is not None and conns._databases:
                if for_write:
                    return conns.resolve_for_write(self._model)
                return conns.resolve_for_read(self._model)
        except ImportError, KeyError:
            pass

        return get_db()


# ---------------------------------------------------------------------------
# Row access helpers (handle both dict and tuple rows from pg.zig/asyncpg)
# ---------------------------------------------------------------------------


def _row_val(row, idx: int) -> Any:
    """Get value from row by positional index."""
    if isinstance(row, dict):
        # idx 0 is the overwhelming common case (scalar/PK read-backs). Grab the
        # first value directly instead of materializing list(row.keys()) just to
        # index it — dicts preserve insertion order, so values()[0] is column 0.
        # Empty dict → None, matching the original `idx < len(keys)` guard.
        if idx == 0:
            return next(iter(row.values()), None)
        keys = list(row.keys())
        return row[keys[idx]] if idx < len(keys) else None
    return row[idx] if idx < len(row) else None


def _row_val_by_key(row, key: str) -> Any:
    """Get value from row by column name/key."""
    if isinstance(row, dict):
        return row.get(key)
    # For tuple rows, we can't look up by name — this shouldn't happen
    # in well-formed queries since we use AS aliases
    return None


def _row_to_dict(row, fields: list[str]) -> dict[str, object]:
    """Convert a row to a dict using given field names."""
    if isinstance(row, dict):
        return {f: row.get(f) for f in fields}
    return dict(zip(fields, row))


def _row_to_tuple(row, fields: list[str]) -> tuple:
    """Convert a row to a tuple with values in ``fields`` order (values_list)."""
    if isinstance(row, dict):
        return tuple(row.get(f) for f in fields)
    return tuple(row[: len(fields)])
