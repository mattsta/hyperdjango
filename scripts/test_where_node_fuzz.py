"""
Hypothesis property-based testing for WhereNode compiled query cache.

Tests the fundamental invariants that must hold for ALL possible queries:
1. Cache hit produces identical (sql, params) as cache miss
2. WhereNode compile $N count matches collect_bind_values length
3. Zig value_shape matches Python value_shape for all types
4. Q._structural_key is identical for same-structure, different-value Q trees
5. Param ordering: fast-path == tree-based for any filter combination

# hyper-test: unit
"""

import re
import sys

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hyperdjango.expressions import Q
from hyperdjango.query import QuerySet, clear_compiled_cache
from hyperdjango.where import WhereNode, value_shape

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Field names (no __ to avoid lookup/transform confusion in simple tests)
field_names = st.sampled_from(
    ["name", "age", "status", "email", "score", "role", "title", "active"]
)

# Lookup suffixes
lookup_suffixes = st.sampled_from(
    [
        "",
        "__gt",
        "__gte",
        "__lt",
        "__lte",
        "__iexact",
        "__contains",
        "__icontains",
        "__startswith",
        "__endswith",
        "__in",
        "__isnull",
        "__range",
        "__regex",
    ]
)

# Filter values that exercise different value_shape paths
filter_values = st.one_of(
    st.text(min_size=1, max_size=20),  # strings (shape=4)
    st.integers(min_value=-1000, max_value=1000),  # ints (shape=4)
    st.just(None),  # None (shape=0)
    st.just(True),  # True (shape=1)
    st.just(False),  # False (shape=2)
    st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
)


# Values appropriate for specific lookups
def value_for_lookup(suffix):
    """Generate a value appropriate for the lookup type."""
    if suffix == "__isnull":
        return st.sampled_from([True, False])
    if suffix == "__in":
        return st.lists(st.integers(min_value=1, max_value=100), min_size=0, max_size=5)
    if suffix == "__range":
        return st.tuples(
            st.integers(min_value=0, max_value=50),
            st.integers(min_value=51, max_value=100),
        )
    if suffix in ("__contains", "__icontains", "__startswith", "__endswith", "__regex"):
        return st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnop")
    if suffix in ("__gt", "__gte", "__lt", "__lte"):
        return st.integers(min_value=-100, max_value=100)
    if suffix == "__iexact":
        return st.text(min_size=1, max_size=10)
    # exact (no suffix)
    return st.one_of(
        st.text(min_size=1, max_size=10),
        st.integers(min_value=-100, max_value=100),
        st.just(None),
    )


# Generate a single filter (key, value) pair with compatible types
@st.composite
def filter_pair(draw):
    field = draw(field_names)
    suffix = draw(lookup_suffixes)
    key = field + suffix if suffix else field
    value = draw(value_for_lookup(suffix))
    return (key, value)


# ---------------------------------------------------------------------------
# Mock model for QuerySet tests
# ---------------------------------------------------------------------------


class MockMeta:
    table = "fuzz_table"
    fields = {}
    pk_field = "id"
    auto_field = "id"
    column_names = [
        "id",
        "name",
        "age",
        "status",
        "email",
        "score",
        "role",
        "title",
        "active",
    ]


class MockModel:
    _meta = MockMeta()


def make_qs(filters):
    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = list(filters)
    qs._excludes = []
    qs._raw_wheres = []
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = ("-id",)
    qs._limit = 10
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False
    return qs


# ---------------------------------------------------------------------------
# Property 1: Cache hit == cache miss
# ---------------------------------------------------------------------------


@given(filters=st.lists(filter_pair(), min_size=1, max_size=6))
@settings(max_examples=500, deadline=2000)
def test_cache_hit_equals_miss(filters):
    """For ANY filter combination, cache hit must produce identical (sql, params) as miss."""
    clear_compiled_cache()

    # First call: cache miss — builds tree, compiles SQL, caches
    qs1 = make_qs(filters)
    sql_miss, params_miss = qs1._build_select()

    # Second call: cache hit — fast-path, no tree
    qs2 = make_qs(filters)
    sql_hit, params_hit = qs2._build_select()

    assert sql_miss == sql_hit, f"SQL mismatch:\n  miss: {sql_miss}\n  hit:  {sql_hit}"
    assert params_miss == params_hit, (
        f"Params mismatch:\n  miss: {params_miss}\n  hit:  {params_hit}"
    )

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Property 2: compile $N count == bind values count
# ---------------------------------------------------------------------------


@given(
    templates=st.lists(
        st.sampled_from(
            ["x = {}", "y > {}", "z BETWEEN {} AND {}", "w IS NULL", "v = FALSE"]
        ),
        min_size=1,
        max_size=8,
    )
)
@settings(max_examples=300, deadline=1000)
def test_compile_param_count_matches_values(templates):
    """Number of $N in compiled SQL must equal number of collected bind values."""
    children = []
    for tmpl in templates:
        placeholder_count = tmpl.count("{}")
        values = list(range(placeholder_count))
        children.append(WhereNode(template=tmpl, bind_values=values))

    root = WhereNode(connector="AND", children=children)
    sql, params, _ = root.compile()
    collected = root.collect_bind_values()

    # Count $N placeholders in compiled SQL
    dollar_count = len(re.findall(r"\$\d+", sql))

    assert len(params) == len(collected), (
        f"compile params ({len(params)}) != collected ({len(collected)})"
    )
    assert dollar_count == len(params), (
        f"$N count ({dollar_count}) != params ({len(params)}) in: {sql}"
    )


# ---------------------------------------------------------------------------
# Property 3: Zig value_shape matches Python for all types
# ---------------------------------------------------------------------------


@given(
    value=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=20),
        st.lists(st.integers(), max_size=5),
        st.just(()),
        st.just([]),
        st.just(set()),
        st.just(frozenset()),
    )
)
@settings(max_examples=500, deadline=500)
def test_zig_value_shape_matches_python(value):
    """Zig valueShape() must match Python value_shape() for all types."""
    from hyperdjango._hyperdjango_native import _where_cache_key

    py_shape = value_shape(value)

    # Zig computes shape internally — we verify by checking that two filters
    # with same key but different values get same/different hashes matching
    # the Python shape classification
    h1 = _where_cache_key([("f", value)], [])
    h2 = _where_cache_key([("f", value)], [])
    assert h1 == h2, "Same value must produce same hash"

    # Different shape class should produce different hash
    if py_shape == 0:  # None
        h_other = _where_cache_key([("f", "notnone")], [])
        assert h1 != h_other, "None vs non-None should differ"
    elif py_shape == 1:  # True
        h_other = _where_cache_key([("f", False)], [])
        assert h1 != h_other, "True vs False should differ"


# ---------------------------------------------------------------------------
# Property 4: Q structural key same for same-structure, different values
# ---------------------------------------------------------------------------


@given(
    vals1=st.lists(st.text(min_size=1, max_size=5), min_size=1, max_size=4),
    vals2=st.lists(st.text(min_size=1, max_size=5), min_size=1, max_size=4),
)
@settings(max_examples=300, deadline=1000)
def test_q_structural_key_ignores_values(vals1, vals2):
    """Q trees with same keys but different values must have same structural key."""
    assume(len(vals1) == len(vals2))

    fields = [f"field_{i}" for i in range(len(vals1))]

    q1 = Q(**{f: v for f, v in zip(fields, vals1)})
    q2 = Q(**{f: v for f, v in zip(fields, vals2)})

    # Same structure, different values → same key (values have same shape: all strings = 4)
    assert q1._structural_key() == q2._structural_key()


# ---------------------------------------------------------------------------
# Property 5: Fast-path params == tree-based params
# ---------------------------------------------------------------------------


@given(filters=st.lists(filter_pair(), min_size=1, max_size=6))
@settings(max_examples=500, deadline=2000)
def test_fast_path_params_match_tree_params(filters):
    """_collect_where_params() must return same values as tree.collect_bind_values()."""
    qs = make_qs(filters)

    # Fast-path params
    fast_params = qs._collect_where_params()

    # Tree-based params
    tree = qs._build_where_tree()
    tree_params = tree.collect_bind_values()

    assert fast_params == tree_params, (
        f"Param mismatch for filters {[(k, type(v).__name__) for k, v in filters]}:\n"
        f"  fast: {fast_params}\n"
        f"  tree: {tree_params}"
    )


# ---------------------------------------------------------------------------
# Property 6: UPDATE cache correctness
# ---------------------------------------------------------------------------


@given(
    filters=st.lists(filter_pair(), min_size=1, max_size=4),
    update_cols=st.lists(
        st.sampled_from(["name", "status", "score", "email"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
@settings(max_examples=200, deadline=2000)
def test_update_cache_correctness(filters, update_cols):
    """UPDATE cache hit must produce same (sql, params) as miss."""
    clear_compiled_cache()

    values = {col: f"val_{i}" for i, col in enumerate(update_cols)}

    # UPDATE never slices — make_qs sets a default _limit for the SELECT-cache
    # fixtures, but _build_update now (correctly) refuses a sliced queryset, so
    # clear the slice here; this property is about SET-columns + WHERE caching.
    qs1 = make_qs(filters)
    qs1._limit = None
    qs1._offset = None
    sql_miss, params_miss = qs1._build_update(values)

    # Same structure, different SET values
    values2 = {col: f"new_{i}" for i, col in enumerate(update_cols)}
    qs2 = make_qs(filters)
    qs2._limit = None
    qs2._offset = None
    sql_hit, params_hit = qs2._build_update(values2)

    assert sql_miss == sql_hit, (
        f"UPDATE SQL mismatch:\n  miss: {sql_miss}\n  hit:  {sql_hit}"
    )
    # Params should have updated SET values but same $N structure
    assert len(params_miss) == len(params_hit)

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── WhereNode Hypothesis Fuzz Tests ──\n")

    tests = [
        ("cache hit == miss", test_cache_hit_equals_miss),
        ("compile $N == values", test_compile_param_count_matches_values),
        ("Zig value_shape parity", test_zig_value_shape_matches_python),
        ("Q structural key", test_q_structural_key_ignores_values),
        ("fast params == tree params", test_fast_path_params_match_tree_params),
        ("UPDATE cache correctness", test_update_cache_correctness),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Hypothesis fuzz: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
