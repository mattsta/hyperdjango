"""
Tests for WhereNode compiled query tree architecture.

Tests WhereNode dataclass, Lookup.to_node(), resolve_lookup_node(),
Q.to_node(), _build_where_tree(), mixin composition, and cache_key().

# hyper-test: unit
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_meta import make_model

from hyperdjango.where import WhereNode

# ---------------------------------------------------------------------------
# WhereNode compile tests
# ---------------------------------------------------------------------------


def test_empty_node():
    """Empty WhereNode compiles to empty string."""
    node = WhereNode()
    sql, params, idx = node.compile()
    assert sql == "", f"Expected empty, got {sql!r}"
    assert params == []
    assert idx == 1
    print("  PASS: empty node")


def test_leaf_node_single_param():
    """Leaf node with one {} placeholder."""
    node = WhereNode(template="name = {}", bind_values=["alice"])
    sql, params, idx = node.compile()
    assert sql == "name = $1", f"Got {sql!r}"
    assert params == ["alice"]
    assert idx == 2
    print("  PASS: leaf single param")


def test_leaf_node_no_params():
    """Leaf node with literal SQL (no params)."""
    node = WhereNode(template="is_deleted = FALSE")
    sql, params, idx = node.compile()
    assert sql == "is_deleted = FALSE", f"Got {sql!r}"
    assert params == []
    assert idx == 1
    print("  PASS: leaf no params")


def test_leaf_node_two_params():
    """Leaf node with two {} placeholders (BETWEEN)."""
    node = WhereNode(template="age BETWEEN {} AND {}", bind_values=[18, 65])
    sql, params, idx = node.compile()
    assert sql == "age BETWEEN $1 AND $2", f"Got {sql!r}"
    assert params == [18, 65]
    assert idx == 3
    print("  PASS: leaf two params")


def test_leaf_negated():
    """Negated leaf wraps in NOT(...)."""
    node = WhereNode(template="name = {}", bind_values=["alice"], negated=True)
    sql, params, idx = node.compile()
    assert sql == "NOT (name = $1)", f"Got {sql!r}"
    assert params == ["alice"]
    print("  PASS: leaf negated")


def test_branch_and():
    """Branch node joins children with AND."""
    root = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="name = {}", bind_values=["alice"]),
            WhereNode(template="age > {}", bind_values=[18]),
        ],
    )
    sql, params, idx = root.compile()
    assert sql == "name = $1 AND age > $2", f"Got {sql!r}"
    assert params == ["alice", 18]
    assert idx == 3
    print("  PASS: branch AND")


def test_branch_or():
    """Branch node joins children with OR, wrapped in parens for precedence."""
    root = WhereNode(
        connector="OR",
        children=[
            WhereNode(template="name = {}", bind_values=["alice"]),
            WhereNode(template="name = {}", bind_values=["bob"]),
        ],
    )
    sql, params, idx = root.compile()
    assert sql == "(name = $1 OR name = $2)", f"Got {sql!r}"
    assert params == ["alice", "bob"]
    print("  PASS: branch OR")


def test_branch_negated():
    """Negated branch wraps entire expression."""
    root = WhereNode(
        connector="AND",
        negated=True,
        children=[
            WhereNode(template="name = {}", bind_values=["alice"]),
            WhereNode(template="age > {}", bind_values=[18]),
        ],
    )
    sql, params, idx = root.compile()
    assert sql == "NOT (name = $1 AND age > $2)", f"Got {sql!r}"
    assert params == ["alice", 18]
    print("  PASS: branch negated")


def test_start_idx():
    """compile() respects start_idx for $N numbering."""
    node = WhereNode(template="name = {}", bind_values=["alice"])
    sql, params, idx = node.compile(start_idx=5)
    assert sql == "name = $5", f"Got {sql!r}"
    assert params == ["alice"]
    assert idx == 6
    print("  PASS: start_idx")


def test_nested_tree():
    """Nested AND/OR tree compiles correctly with proper precedence."""
    root = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="status = {}", bind_values=["active"]),
            WhereNode(
                connector="OR",
                children=[
                    WhereNode(template="name = {}", bind_values=["alice"]),
                    WhereNode(template="name = {}", bind_values=["bob"]),
                ],
            ),
            WhereNode(template="age > {}", bind_values=[18]),
        ],
    )
    sql, params, idx = root.compile()
    # OR branch wrapped in parens to preserve precedence inside AND
    assert sql == "status = $1 AND (name = $2 OR name = $3) AND age > $4", (
        f"Got {sql!r}"
    )
    assert params == ["active", "alice", "bob", 18]
    assert idx == 5
    print("  PASS: nested tree")


def test_single_child_no_connector():
    """Branch with single child doesn't add connector."""
    root = WhereNode(
        connector="AND",
        children=[WhereNode(template="name = {}", bind_values=["alice"])],
    )
    sql, params, _ = root.compile()
    assert sql == "name = $1", f"Got {sql!r}"
    print("  PASS: single child")


def test_empty_children_filtered():
    """Empty child nodes are skipped."""
    root = WhereNode(
        connector="AND",
        children=[
            WhereNode(),  # empty
            WhereNode(template="name = {}", bind_values=["alice"]),
            WhereNode(),  # empty
        ],
    )
    sql, params, _ = root.compile()
    assert sql == "name = $1", f"Got {sql!r}"
    print("  PASS: empty children filtered")


# ---------------------------------------------------------------------------
# cache_key tests
# ---------------------------------------------------------------------------


def test_cache_key_structural():
    """Same structure, different values → same cache key."""
    a = WhereNode(template="name = {}", bind_values=["alice"])
    b = WhereNode(template="name = {}", bind_values=["bob"])
    assert a.cache_key() == b.cache_key()
    print("  PASS: cache key structural equality")


def test_cache_key_different_template():
    """Different templates → different cache keys."""
    a = WhereNode(template="name = {}", bind_values=["alice"])
    b = WhereNode(template="age = {}", bind_values=[18])
    assert a.cache_key() != b.cache_key()
    print("  PASS: cache key template difference")


def test_cache_key_different_param_count():
    """Different param counts → different cache keys."""
    a = WhereNode(template="name = {}", bind_values=["alice"])
    b = WhereNode(template="age BETWEEN {} AND {}", bind_values=[18, 65])
    assert a.cache_key() != b.cache_key()
    print("  PASS: cache key param count difference")


def test_cache_key_negated():
    """Negated vs non-negated → different cache keys."""
    a = WhereNode(template="name = {}", bind_values=["alice"])
    b = WhereNode(template="name = {}", bind_values=["alice"], negated=True)
    assert a.cache_key() != b.cache_key()
    print("  PASS: cache key negated difference")


def test_cache_key_branch():
    """Branch structure is captured in cache key."""
    a = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="name = {}", bind_values=["alice"]),
            WhereNode(template="age > {}", bind_values=[18]),
        ],
    )
    b = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="name = {}", bind_values=["bob"]),
            WhereNode(template="age > {}", bind_values=[21]),
        ],
    )
    assert a.cache_key() == b.cache_key()
    print("  PASS: cache key branch structural equality")


def test_cache_key_connector_difference():
    """AND vs OR → different cache keys."""
    a = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="x = {}", bind_values=[1]),
            WhereNode(template="y = {}", bind_values=[2]),
        ],
    )
    b = WhereNode(
        connector="OR",
        children=[
            WhereNode(template="x = {}", bind_values=[1]),
            WhereNode(template="y = {}", bind_values=[2]),
        ],
    )
    assert a.cache_key() != b.cache_key()
    print("  PASS: cache key connector difference")


# ---------------------------------------------------------------------------
# collect_bind_values tests
# ---------------------------------------------------------------------------


def test_collect_bind_values_leaf():
    """Leaf collect returns its bind values."""
    node = WhereNode(template="name = {}", bind_values=["alice"])
    assert node.collect_bind_values() == ["alice"]
    print("  PASS: collect leaf")


def test_collect_bind_values_tree():
    """Tree collect returns all values in order."""
    root = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="name = {}", bind_values=["alice"]),
            WhereNode(template="is_deleted = FALSE"),
            WhereNode(template="age BETWEEN {} AND {}", bind_values=[18, 65]),
            WhereNode(template="tenant_id = {}", bind_values=[42]),
        ],
    )
    values = root.collect_bind_values()
    assert values == ["alice", 18, 65, 42], f"Got {values}"
    print("  PASS: collect tree")


# ---------------------------------------------------------------------------
# is_empty tests
# ---------------------------------------------------------------------------


def test_is_empty():
    assert WhereNode().is_empty
    assert not WhereNode(template="x = {}").is_empty
    assert not WhereNode(children=[WhereNode(template="x = {}")]).is_empty
    print("  PASS: is_empty")


# ---------------------------------------------------------------------------
# Lookup.to_node() tests
# ---------------------------------------------------------------------------


def test_lookup_exact_to_node():
    """ExactLookup.to_node produces correct WhereNode."""
    from hyperdjango.lookups import ExactLookup

    lookup = ExactLookup()
    node = lookup.to_node("name", "alice")
    assert node.template == "name = {}", f"Got {node.template!r}"
    assert node.bind_values == ["alice"]
    sql, params, _ = node.compile()
    assert sql == "name = $1"
    assert params == ["alice"]
    print("  PASS: ExactLookup.to_node")


def test_lookup_exact_null_to_node():
    """ExactLookup with None produces IS NULL (no params)."""
    from hyperdjango.lookups import ExactLookup

    lookup = ExactLookup()
    node = lookup.to_node("name", None)
    assert node.template == "name IS NULL", f"Got {node.template!r}"
    assert node.bind_values == []
    print("  PASS: ExactLookup.to_node None")


def test_lookup_range_to_node():
    """RangeLookup.to_node produces two-param WhereNode."""
    from hyperdjango.lookups import RangeLookup

    lookup = RangeLookup()
    node = lookup.to_node("age", (18, 65))
    assert node.template == "age BETWEEN {} AND {}", f"Got {node.template!r}"
    assert node.bind_values == [18, 65]
    sql, params, _ = node.compile()
    assert sql == "age BETWEEN $1 AND $2"
    assert params == [18, 65]
    print("  PASS: RangeLookup.to_node")


def test_lookup_isnull_to_node():
    """IsNullLookup.to_node produces no params."""
    from hyperdjango.lookups import IsNullLookup

    lookup = IsNullLookup()
    node = lookup.to_node("deleted_at", True)
    assert node.template == "deleted_at IS NULL", f"Got {node.template!r}"
    assert node.bind_values == []
    print("  PASS: IsNullLookup.to_node True")

    node2 = lookup.to_node("deleted_at", False)
    assert node2.template == "deleted_at IS NOT NULL"
    assert node2.bind_values == []
    print("  PASS: IsNullLookup.to_node False")


def test_lookup_in_to_node():
    """InLookup.to_node produces array param."""
    from hyperdjango.lookups import InLookup

    lookup = InLookup()
    node = lookup.to_node("id", [1, 2, 3])
    assert node.template == "id = ANY({})", f"Got {node.template!r}"
    assert node.bind_values == [[1, 2, 3]]
    print("  PASS: InLookup.to_node")


def test_lookup_in_empty_to_node():
    """InLookup.to_node with empty list → FALSE."""
    from hyperdjango.lookups import InLookup

    lookup = InLookup()
    node = lookup.to_node("id", [])
    assert node.template == "FALSE", f"Got {node.template!r}"
    assert node.bind_values == []
    print("  PASS: InLookup.to_node empty")


def test_lookup_contains_to_node():
    """ContainsLookup wraps value in % for LIKE."""
    from hyperdjango.lookups import ContainsLookup

    lookup = ContainsLookup()
    node = lookup.to_node("name", "ali")
    assert "LIKE" in node.template
    assert node.bind_values == ["%ali%"]
    print("  PASS: ContainsLookup.to_node")


def test_lookup_gt_to_node():
    """GtLookup.to_node."""
    from hyperdjango.lookups import GtLookup

    lookup = GtLookup()
    node = lookup.to_node("age", 18)
    assert node.template == "age > {}", f"Got {node.template!r}"
    assert node.bind_values == [18]
    print("  PASS: GtLookup.to_node")


# ---------------------------------------------------------------------------
# resolve_lookup_node / resolve_exclude_node
# ---------------------------------------------------------------------------


def test_resolve_lookup_node():
    """resolve_lookup_node resolves key → WhereNode."""
    from hyperdjango.lookups import resolve_lookup_node

    node = resolve_lookup_node("name", "alice")
    assert node.template == "name = {}", f"Got {node.template!r}"
    assert node.bind_values == ["alice"]
    print("  PASS: resolve_lookup_node exact")


def test_resolve_lookup_node_with_lookup():
    """resolve_lookup_node handles explicit lookups."""
    from hyperdjango.lookups import resolve_lookup_node

    node = resolve_lookup_node("age__gte", 18)
    assert node.template == "age >= {}", f"Got {node.template!r}"
    assert node.bind_values == [18]
    print("  PASS: resolve_lookup_node gte")


def test_resolve_lookup_node_with_transform():
    """resolve_lookup_node handles transforms."""
    from hyperdjango.lookups import resolve_lookup_node

    node = resolve_lookup_node("created__year", 2024)
    assert "EXTRACT(YEAR FROM created)" in node.template, f"Got {node.template!r}"
    assert node.bind_values == [2024]
    print("  PASS: resolve_lookup_node transform")


def test_resolve_lookup_node_qualified():
    """resolve_lookup_node qualifies column with table alias."""
    from hyperdjango.lookups import resolve_lookup_node

    node = resolve_lookup_node("name", "alice", table_alias="users")
    assert "users.name" in node.template, f"Got {node.template!r}"
    print("  PASS: resolve_lookup_node qualified")


def test_resolve_exclude_node():
    """resolve_exclude_node produces negated node."""
    from hyperdjango.lookups import resolve_exclude_node

    node = resolve_exclude_node("name", "alice")
    assert node.negated is True
    sql, params, _ = node.compile()
    assert sql == "NOT (name = $1)", f"Got {sql!r}"
    assert params == ["alice"]
    print("  PASS: resolve_exclude_node")


# ---------------------------------------------------------------------------
# Q.to_node()
# ---------------------------------------------------------------------------


def test_q_simple():
    """Simple Q → WhereNode."""
    from hyperdjango.expressions import Q

    q = Q(name="alice")
    node = q.to_node()
    assert len(node.children) == 1
    sql, params, _ = node.compile()
    assert sql == "name = $1", f"Got {sql!r}"
    assert params == ["alice"]
    print("  PASS: Q simple")


def test_q_or():
    """Q OR → WhereNode with OR connector, wrapped in parens."""
    from hyperdjango.expressions import Q

    q = Q(name="alice") | Q(name="bob")
    node = q.to_node()
    assert node.connector == "OR"
    sql, params, _ = node.compile()
    assert "(name = $1 OR name = $2)" in sql, f"Got {sql!r}"
    assert params == ["alice", "bob"]
    print("  PASS: Q OR")


def test_q_and():
    """Q AND → WhereNode with AND connector."""
    from hyperdjango.expressions import Q

    q = Q(name="alice") & Q(age__gt=18)
    node = q.to_node()
    assert node.connector == "AND"
    sql, params, _ = node.compile()
    assert "name = $1" in sql
    assert "age > $2" in sql
    assert params == ["alice", 18]
    print("  PASS: Q AND")


def test_q_not():
    """~Q → negated WhereNode."""
    from hyperdjango.expressions import Q

    q = ~Q(name="alice")
    node = q.to_node()
    assert node.negated is True
    sql, params, _ = node.compile()
    assert "NOT" in sql
    assert params == ["alice"]
    print("  PASS: Q NOT")


def test_q_nested():
    """Nested Q → nested WhereNode tree."""
    from hyperdjango.expressions import Q

    q = (Q(name="alice") | Q(name="bob")) & Q(age__gte=18)
    node = q.to_node()
    sql, params, _ = node.compile()
    # Should have both names and age
    assert params == ["alice", "bob", 18], f"Got {params}"
    assert "$1" in sql and "$2" in sql and "$3" in sql
    print("  PASS: Q nested")


# ---------------------------------------------------------------------------
# Mixin composition with WhereNode
# ---------------------------------------------------------------------------


def test_mixin_softdelete_tree():
    """SoftDeleteQuerySet._build_where_tree adds is_deleted filter."""
    from hyperdjango.mixins import SoftDeleteQuerySet

    # Real _meta via shared builder (scripts/_test_meta.py)
    MockModel = make_model("posts", ["id", "title", "is_deleted", "deleted_at"])

    qs = SoftDeleteQuerySet(MockModel)
    qs._annotations = {}
    qs._filters = [("title", "hello")]
    qs._excludes = []
    qs._raw_wheres = []

    tree = qs._build_where_tree()
    sql, params, _ = tree.compile()
    assert "title = $1" in sql, f"Got {sql!r}"
    assert "is_deleted = FALSE" in sql, f"Got {sql!r}"
    assert params == ["hello"]
    print("  PASS: SoftDelete _build_where_tree")


def test_mixin_softdelete_with_deleted():
    """SoftDeleteQuerySet.with_deleted() skips is_deleted filter."""
    from hyperdjango.mixins import SoftDeleteQuerySet

    MockModel = make_model("posts", ["id", "title"])

    qs = SoftDeleteQuerySet(MockModel, include_deleted=True)
    qs._annotations = {}
    qs._filters = [("title", "hello")]
    qs._excludes = []
    qs._raw_wheres = []

    tree = qs._build_where_tree()
    sql, params, _ = tree.compile()
    assert "is_deleted" not in sql, f"Got {sql!r}"
    print("  PASS: SoftDelete with_deleted _build_where_tree")


# ---------------------------------------------------------------------------
# Raw WHERE integration
# ---------------------------------------------------------------------------


def test_raw_where():
    """Raw WHERE fragments convert {idx} to {} in WhereNode."""
    from hyperdjango.query import QuerySet

    MockModel = make_model("posts", ["id", "title"])

    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = []
    qs._excludes = []
    qs._raw_wheres = [("score > {idx} AND score < {idx}", [10, 100])]

    tree = qs._build_where_tree()
    sql, params, _ = tree.compile()
    assert sql == "score > $1 AND score < $2", f"Got {sql!r}"
    assert params == [10, 100]
    print("  PASS: raw WHERE in tree")


# ---------------------------------------------------------------------------
# Compiled SQL cache tests
# ---------------------------------------------------------------------------


def test_cache_hit():
    """Compiled cache stores and returns SQL on structural match."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name", "age"])

    # First query — cache miss
    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("name", "alice")]
    qs1._excludes = []
    qs1._raw_wheres = []
    qs1._select_related = []
    qs1._values_fields = None
    qs1._only = None
    qs1._defer = None
    qs1._ordering = None
    qs1._limit = 1
    qs1._offset = None
    qs1._distinct = False
    qs1._for_update = None
    qs1._group_by = False

    sql1, params1 = qs1._build_select()
    cache_before = len(_compiled_sql_cache)
    assert cache_before == 1, f"Expected 1 cache entry, got {cache_before}"

    # Second query — same structure, different value — cache hit
    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("name", "bob")]
    qs2._excludes = []
    qs2._raw_wheres = []
    qs2._select_related = []
    qs2._values_fields = None
    qs2._only = None
    qs2._defer = None
    qs2._ordering = None
    qs2._limit = 1
    qs2._offset = None
    qs2._distinct = False
    qs2._for_update = None
    qs2._group_by = False

    sql2, params2 = qs2._build_select()
    cache_after = len(_compiled_sql_cache)

    # Same SQL template, different params. LIMIT is a BOUND param (trailing),
    # so each params list ends with the limit value (1) after the WHERE value.
    assert sql1 == sql2, f"SQL mismatch:\n  {sql1!r}\n  {sql2!r}"
    assert params1 == ["alice", 1]
    assert params2 == ["bob", 1]
    assert cache_after == 1, f"Cache grew to {cache_after} (should stay 1)"
    print("  PASS: cache hit")

    clear_compiled_cache()


def test_cache_miss_different_structure():
    """Different query structures produce different cache entries."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name", "age"])

    # Query 1: filter by name
    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("name", "alice")]
    qs1._excludes = []
    qs1._raw_wheres = []
    qs1._select_related = []
    qs1._values_fields = None
    qs1._only = None
    qs1._defer = None
    qs1._ordering = None
    qs1._limit = None
    qs1._offset = None
    qs1._distinct = False
    qs1._for_update = None
    qs1._group_by = False

    qs1._build_select()

    # Query 2: filter by age (different structure)
    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("age__gte", 18)]
    qs2._excludes = []
    qs2._raw_wheres = []
    qs2._select_related = []
    qs2._values_fields = None
    qs2._only = None
    qs2._defer = None
    qs2._ordering = None
    qs2._limit = None
    qs2._offset = None
    qs2._distinct = False
    qs2._for_update = None
    qs2._group_by = False

    qs2._build_select()

    assert len(_compiled_sql_cache) == 2, (
        f"Expected 2 entries, got {len(_compiled_sql_cache)}"
    )
    print("  PASS: cache miss different structure")

    clear_compiled_cache()


def test_cache_different_limit():
    """LIMIT is a BOUND param: different VALUES share one template; only the
    PRESENCE of a LIMIT (not its value) changes the cached SQL.

    This is the whole point of parameterizing LIMIT — every page of a query
    (`LIMIT 10`, `LIMIT 20`, ...) collapses to a single `... LIMIT $n` template
    instead of exploding the compiled-SQL cache with one entry per page.
    """
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name"])

    def _make(limit):
        qs = QuerySet(MockModel)
        qs._annotations = {}
        qs._filters = [("name", "alice")]
        qs._excludes = []
        qs._raw_wheres = []
        qs._select_related = []
        qs._values_fields = None
        qs._only = None
        qs._defer = None
        qs._ordering = None
        qs._limit = limit
        qs._offset = None
        qs._distinct = False
        qs._for_update = None
        qs._group_by = False
        return qs

    # Two different LIMIT VALUES → same template, ONE cache entry.
    sql10, params10 = _make(10)._build_select()
    sql20, params20 = _make(20)._build_select()
    assert sql10 == sql20, (
        f"LIMIT value should not change SQL:\n  {sql10!r}\n  {sql20!r}"
    )
    assert "LIMIT $" in sql10, f"LIMIT should be a bound param, got {sql10!r}"
    assert params10 == ["alice", 10]
    assert params20 == ["alice", 20]
    assert len(_compiled_sql_cache) == 1, (
        f"Different LIMIT values must share one template, got "
        f"{len(_compiled_sql_cache)} entries"
    )

    # LIMIT present vs absent → distinct templates (presence is in the key).
    _make(None)._build_select()
    assert len(_compiled_sql_cache) == 2, (
        f"LIMIT presence should be a distinct template, got "
        f"{len(_compiled_sql_cache)} entries"
    )
    print("  PASS: cache different LIMIT")

    clear_compiled_cache()


def test_cache_count():
    """COUNT queries use compiled cache."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_count_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name"])

    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("name", "alice")]
    qs1._excludes = []
    qs1._raw_wheres = []
    qs1._select_related = []

    sql1, params1 = qs1._build_count()
    assert len(_compiled_count_cache) == 1

    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("name", "bob")]
    qs2._excludes = []
    qs2._raw_wheres = []
    qs2._select_related = []

    sql2, params2 = qs2._build_count()
    assert sql1 == sql2
    assert params1 == ["alice"]
    assert params2 == ["bob"]
    assert len(_compiled_count_cache) == 1  # same structure = no new entry
    print("  PASS: COUNT cache")

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Zig native hash parity tests
# ---------------------------------------------------------------------------


def test_zig_hash_deterministic():
    """Same inputs produce same hash across calls."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    filters = [("name", "alice"), ("age__gte", 18), ("status", "active")]
    h1 = _where_cache_key(filters, [])
    h2 = _where_cache_key(filters, [])
    assert h1 == h2
    print("  PASS: Zig hash deterministic")


def test_zig_hash_structural_equality():
    """Same filter keys, different values → same hash."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h1 = _where_cache_key([("name", "alice"), ("age__gte", 18)], [])
    h2 = _where_cache_key([("name", "bob"), ("age__gte", 21)], [])
    assert h1 == h2, f"Should match: {h1} vs {h2}"
    print("  PASS: Zig hash structural equality")


def test_zig_hash_different_keys():
    """Different filter keys → different hash."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h1 = _where_cache_key([("name", "x")], [])
    h2 = _where_cache_key([("age", "x")], [])
    assert h1 != h2, "Different keys should produce different hashes"
    print("  PASS: Zig hash different keys")


def test_zig_hash_different_value_shape():
    """Different value shapes (None vs non-None) → different hash."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h1 = _where_cache_key([("name", None)], [])
    h2 = _where_cache_key([("name", "alice")], [])
    assert h1 != h2, "None vs non-None should differ"
    print("  PASS: Zig hash value shape difference")


def test_zig_hash_isnull_differentiation():
    """isnull=True vs isnull=False → different hashes."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h_true = _where_cache_key([("bio__isnull", True)], [])
    h_false = _where_cache_key([("bio__isnull", False)], [])
    assert h_true != h_false, "isnull True vs False must differ"
    print("  PASS: Zig hash isnull differentiation")


def test_zig_hash_excludes():
    """Filters vs excludes with same keys → different hashes."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h_filter = _where_cache_key([("name", "x")], [])
    h_exclude = _where_cache_key([], [("name", "x")])
    assert h_filter != h_exclude, "Filter vs exclude should differ"
    print("  PASS: Zig hash filter vs exclude")


def test_zig_hash_order_matters():
    """Different filter order → different hashes."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h1 = _where_cache_key([("name", "x"), ("age", 1)], [])
    h2 = _where_cache_key([("age", 1), ("name", "x")], [])
    assert h1 != h2, "Order should matter for hash"
    print("  PASS: Zig hash order matters")


def test_zig_hash_empty():
    """Empty filter lists produce a valid hash."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    h = _where_cache_key([], [])
    assert isinstance(h, int)
    print("  PASS: Zig hash empty lists")


def test_zig_hash_many_filters():
    """20 filters produce unique hashes."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    filters = [(f"field_{i}", i) for i in range(20)]
    h = _where_cache_key(filters, [])
    assert isinstance(h, int)

    filters2 = list(filters)
    filters2[19] = ("field_99", 19)
    h2 = _where_cache_key(filters2, [])
    assert h != h2
    print("  PASS: Zig hash many filters")


def test_zig_hash_collision_resistance():
    """Generate 1000 different filter patterns, check no collisions."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    hashes = set()
    for i in range(1000):
        filters = [(f"f{i}", i), (f"g{i % 10}", i % 3 == 0)]
        h = _where_cache_key(filters, [])
        hashes.add(h)

    assert len(hashes) >= 990, f"Too many collisions: {1000 - len(hashes)} in 1000"
    print(f"  PASS: Zig hash collision resistance ({len(hashes)}/1000 unique)")


def test_zig_hash_used_by_build_select():
    """Verify _build_select uses Zig hash for simple queries."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name"])

    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = [("name", "alice"), ("id", 1)]
    qs._excludes = []
    qs._raw_wheres = []
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = None
    qs._limit = None
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False
    qs._build_select()

    # Cache should have 1 entry with Zig hash in compact key
    assert len(_compiled_sql_cache) == 1
    key = next(iter(_compiled_sql_cache.keys()))
    # Compact key: (id(meta), where_hash, ordering, limit)
    # Full key: (id(meta), col_key, select_related, where_hash, ...)
    # Check that SOME element is a Zig int hash (not a tuple)
    has_int_hash = any(
        isinstance(part, int) and part != id(MockModel._meta) for part in key
    )
    assert has_int_hash, f"Expected Zig int hash in key, got {key}"
    print("  PASS: Zig hash used by _build_select")
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# UPDATE / DELETE cache tests
# ---------------------------------------------------------------------------


def test_update_cache_hit():
    """UPDATE cache: same columns + same WHERE structure → cache hit."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name", "status"])

    # First update: cache miss
    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("id", 1)]
    qs1._excludes = []
    qs1._raw_wheres = []
    sql1, params1 = qs1._build_update({"name": "alice", "status": "active"})
    assert "UPDATE users SET" in sql1
    assert "WHERE" in sql1
    cache_size_after_miss = len(_compiled_sql_cache)

    # Second update: same columns, different values → cache hit
    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("id", 2)]
    qs2._excludes = []
    qs2._raw_wheres = []
    sql2, params2 = qs2._build_update({"name": "bob", "status": "inactive"})

    assert sql1 == sql2, f"SQL should match:\n  {sql1}\n  {sql2}"
    assert params1 == ["alice", "active", 1]
    assert params2 == ["bob", "inactive", 2]
    assert len(_compiled_sql_cache) == cache_size_after_miss  # no new entry
    print("  PASS: UPDATE cache hit")
    clear_compiled_cache()


def test_update_cache_different_columns():
    """UPDATE cache: different SET columns → different cache entries."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name", "status"])

    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("id", 1)]
    qs1._excludes = []
    qs1._raw_wheres = []
    qs1._build_update({"name": "alice"})

    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("id", 1)]
    qs2._excludes = []
    qs2._raw_wheres = []
    qs2._build_update({"status": "active"})

    # Different columns → 2 cache entries
    update_entries = sum(
        1
        for k in _compiled_sql_cache
        if isinstance(k, tuple) and len(k) >= 2 and k[1] == "U"
    )
    assert update_entries == 2, f"Expected 2 UPDATE cache entries, got {update_entries}"
    print("  PASS: UPDATE cache different columns")
    clear_compiled_cache()


def test_delete_cache_hit():
    """DELETE cache: same WHERE structure → cache hit."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name"])

    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("id", 1)]
    qs1._excludes = []
    qs1._raw_wheres = []
    sql1, params1 = qs1._build_delete()

    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("id", 2)]
    qs2._excludes = []
    qs2._raw_wheres = []
    sql2, params2 = qs2._build_delete()

    assert sql1 == sql2
    assert params1 == [1]
    assert params2 == [2]
    delete_entries = sum(
        1
        for k in _compiled_sql_cache
        if isinstance(k, tuple) and len(k) >= 2 and k[1] == "D"
    )
    assert delete_entries == 1
    print("  PASS: DELETE cache hit")
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Benchmark: cache hit vs miss
# ---------------------------------------------------------------------------


def test_benchmark_cache():
    """Benchmark compiled cache hit vs miss speedup."""
    import time

    from hyperdjango.query import QuerySet, clear_compiled_cache

    _PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"

    MockModel = make_model(
        "users", ["id", "name", "email", "age", "status", "created_at"]
    )

    clear_compiled_cache()

    iterations = 10_000

    # Cold run (cache miss on first, hits after)
    start = time.perf_counter()
    for i in range(iterations):
        qs = QuerySet(MockModel)
        qs._annotations = {}
        qs._filters = [("name", f"user_{i}"), ("age__gte", 18), ("status", "active")]
        qs._excludes = []
        qs._raw_wheres = []
        qs._select_related = []
        qs._values_fields = None
        qs._only = None
        qs._defer = None
        qs._ordering = ("-created_at",)
        qs._limit = 10
        qs._offset = None
        qs._distinct = False
        qs._for_update = None
        qs._group_by = False
        qs._build_select()
    elapsed_with_cache = time.perf_counter() - start

    clear_compiled_cache()

    # Measure just tree building + compile (no cache)
    # We can't easily disable the cache, so measure differently:
    # The first iteration is a miss, subsequent are hits.
    # Total time includes 1 miss + (N-1) hits.

    per_query_us = (elapsed_with_cache / iterations) * 1_000_000

    # Includes QuerySet creation + tree build + cache lookup + param collection.
    # Expect <200µs per query (vs hundreds of µs for full SQL string assembly).
    threshold = 500.0 if _PARALLEL else 200.0
    assert per_query_us < threshold, (
        f"Cache hit too slow: {per_query_us:.1f}µs per query (threshold: {threshold}µs)"
    )

    print(f"  PASS: benchmark {per_query_us:.2f}µs/query ({iterations} iterations)")

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Fast-path cache tests (no tree construction on cache hit)
# ---------------------------------------------------------------------------


def test_fast_path_cache_hit():
    """Fast-path cache hit: no WhereNode tree built on second call."""
    from hyperdjango.query import (
        QuerySet,
        _compiled_sql_cache,
        clear_compiled_cache,
    )

    clear_compiled_cache()

    MockModel = make_model("users", ["id", "name", "age"])

    # First call: cache miss, builds tree
    qs1 = QuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("name", "alice")]
    qs1._excludes = []
    qs1._raw_wheres = []
    qs1._select_related = []
    qs1._values_fields = None
    qs1._only = None
    qs1._defer = None
    qs1._ordering = None
    qs1._limit = 1
    qs1._offset = None
    qs1._distinct = False
    qs1._for_update = None
    qs1._group_by = False
    sql1, params1 = qs1._build_select()
    assert len(_compiled_sql_cache) == 1

    # Second call: same structure, different value — cache hit, no tree
    qs2 = QuerySet(MockModel)
    qs2._annotations = {}
    qs2._filters = [("name", "bob")]
    qs2._excludes = []
    qs2._raw_wheres = []
    qs2._select_related = []
    qs2._values_fields = None
    qs2._only = None
    qs2._defer = None
    qs2._ordering = None
    qs2._limit = 1
    qs2._offset = None
    qs2._distinct = False
    qs2._for_update = None
    qs2._group_by = False
    sql2, params2 = qs2._build_select()

    # LIMIT is a trailing BOUND param, so both params lists end with the
    # limit value (1). The fast-path cache-hit branch appends it in the same
    # order as the slow path.
    assert sql1 == sql2, f"SQL mismatch: {sql1!r} vs {sql2!r}"
    assert params1 == ["alice", 1]
    assert params2 == ["bob", 1]
    assert len(_compiled_sql_cache) == 1
    print("  PASS: fast-path cache hit")
    clear_compiled_cache()


def test_fast_path_q_structural_key():
    """Q._structural_key() captures connector/negated/children structure."""
    from hyperdjango.expressions import Q

    q1 = Q(name="alice") | Q(name="bob")
    q2 = Q(name="charlie") | Q(name="dave")
    assert q1._structural_key() == q2._structural_key()

    q3 = Q(name="alice") & Q(name="bob")
    assert q1._structural_key() != q3._structural_key()

    q4 = ~Q(name="alice")
    q5 = Q(name="alice")
    assert q4._structural_key() != q5._structural_key()
    print("  PASS: Q _structural_key")


def test_fast_path_q_collect_bind_params():
    """Q._collect_bind_params() extracts values in correct order."""
    from hyperdjango.expressions import Q

    q = (Q(name="alice") | Q(name="bob")) & Q(age__gte=18)
    params = []
    q._collect_bind_params(params)
    assert params == ["alice", "bob", 18], f"Got {params}"
    print("  PASS: Q _collect_bind_params")


def test_fast_path_collect_where_params():
    """_collect_where_params() matches tree-based param collection."""
    from hyperdjango.query import QuerySet

    MockModel = make_model("users", ["id", "name", "age"])

    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = [("name", "alice"), ("age__gte", 18)]
    qs._excludes = [("status", "banned")]
    qs._raw_wheres = [("score > {idx}", [50])]

    # Fast-path params
    fast_params = qs._collect_where_params()

    # Tree-based params
    tree = qs._build_where_tree()
    tree_params = tree.collect_bind_values()

    assert fast_params == tree_params, (
        f"Mismatch: fast={fast_params} tree={tree_params}"
    )
    print("  PASS: _collect_where_params matches tree")


def test_fast_path_mixin_cache_key():
    """Mixin QuerySets contribute to fast-path cache key."""
    from hyperdjango.mixins import SoftDeleteQuerySet

    MockModel = make_model("posts", ["id", "title"])

    qs1 = SoftDeleteQuerySet(MockModel)
    qs1._annotations = {}
    qs1._filters = [("title", "hello")]
    qs1._excludes = []
    qs1._raw_wheres = []

    qs2 = SoftDeleteQuerySet(MockModel, include_deleted=True)
    qs2._annotations = {}
    qs2._filters = [("title", "hello")]
    qs2._excludes = []
    qs2._raw_wheres = []

    # Different mixin state → different cache keys
    key1 = qs1._fast_where_key()
    key2 = qs2._fast_where_key()
    assert key1 != key2, "SoftDelete mixin state not in cache key"
    print("  PASS: mixin cache key differentiation")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


def run_tests():
    passed = 0
    failed = 0

    test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    for func in test_funcs:
        try:
            func()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {func.__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"WhereNode tests: {passed}/{total} passed")
    if failed:
        print(f"FAILURES: {failed}")
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
