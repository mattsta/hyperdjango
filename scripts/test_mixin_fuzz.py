"""
Hypothesis fuzz tests for QuerySet mixin composition.

Proves: for ANY combination of filters + mixin state:
1. _mixin_cache_key differs for different mixin states
2. _collect_where_params matches tree-based collect for mixin params
3. Cache hit produces same SQL as miss with SoftDelete active

# hyper-test: unit
"""

from _test_meta import make_model
from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.mixins import SoftDeleteQuerySet, VersionedQuerySet
from hyperdjango.query import clear_compiled_cache

# ---------------------------------------------------------------------------
# Mock model — real _meta via shared builder (scripts/_test_meta.py)
# ---------------------------------------------------------------------------

MockModel = make_model(
    "items",
    ["id", "name", "status", "is_deleted", "deleted_at", "is_current", "version"],
)


def make_sd_qs(filters, include_deleted=False):
    qs = SoftDeleteQuerySet(MockModel, include_deleted=include_deleted)
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


def make_ver_qs(filters, include_versions=False):
    qs = VersionedQuerySet(MockModel, include_versions=include_versions)
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


filter_pairs = st.lists(
    st.tuples(
        st.sampled_from(["name", "status", "id"]),
        st.one_of(
            st.text(min_size=1, max_size=10), st.integers(min_value=1, max_value=100)
        ),
    ),
    min_size=1,
    max_size=4,
)


# ---------------------------------------------------------------------------
# Property 1: SoftDelete mixin_cache_key differs by include_deleted
# ---------------------------------------------------------------------------


@given(filters=filter_pairs)
@settings(max_examples=200, deadline=2000)
def test_softdelete_cache_key_differs(filters):
    """SoftDeleteQuerySet with/without include_deleted → different cache keys."""
    qs_filtered = make_sd_qs(filters, include_deleted=False)
    qs_deleted = make_sd_qs(filters, include_deleted=True)
    assert qs_filtered._mixin_cache_key() != qs_deleted._mixin_cache_key()


# ---------------------------------------------------------------------------
# Property 2: Versioned mixin_cache_key differs by include_versions
# ---------------------------------------------------------------------------


@given(filters=filter_pairs)
@settings(max_examples=200, deadline=2000)
def test_versioned_cache_key_differs(filters):
    """VersionedQuerySet with/without include_versions → different cache keys."""
    qs_current = make_ver_qs(filters, include_versions=False)
    qs_all = make_ver_qs(filters, include_versions=True)
    assert qs_current._mixin_cache_key() != qs_all._mixin_cache_key()


# ---------------------------------------------------------------------------
# Property 3: SoftDelete fast-path params == tree-based params
# ---------------------------------------------------------------------------


@given(filters=filter_pairs)
@settings(max_examples=300, deadline=2000)
def test_softdelete_params_match_tree(filters):
    """SoftDelete _collect_where_params matches tree.collect_bind_values."""
    qs = make_sd_qs(filters)
    fast = qs._collect_where_params()
    tree = qs._build_where_tree()
    tree_vals = tree.collect_bind_values()
    assert fast == tree_vals, f"Mismatch: fast={fast} tree={tree_vals}"


@given(filters=filter_pairs)
@settings(max_examples=300, deadline=2000)
def test_versioned_params_match_tree(filters):
    """Versioned _collect_where_params matches tree.collect_bind_values."""
    qs = make_ver_qs(filters)
    fast = qs._collect_where_params()
    tree = qs._build_where_tree()
    tree_vals = tree.collect_bind_values()
    assert fast == tree_vals


# ---------------------------------------------------------------------------
# Property 4: SoftDelete cache hit == miss
# ---------------------------------------------------------------------------


@given(filters=filter_pairs)
@settings(max_examples=200, deadline=2000)
def test_softdelete_cache_hit_equals_miss(filters):
    """SoftDelete cache hit produces same SQL+params as miss."""
    clear_compiled_cache()
    qs1 = make_sd_qs(filters)
    sql1, params1 = qs1._build_select()

    qs2 = make_sd_qs(filters)
    sql2, params2 = qs2._build_select()

    assert sql1 == sql2
    assert params1 == params2
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Property 5: SoftDelete SQL contains is_deleted filter
# ---------------------------------------------------------------------------


@given(filters=filter_pairs)
@settings(max_examples=200, deadline=2000)
def test_softdelete_sql_has_filter(filters):
    """SoftDelete (not include_deleted) SQL contains is_deleted = FALSE."""
    clear_compiled_cache()
    qs = make_sd_qs(filters, include_deleted=False)
    sql, _ = qs._build_select()
    assert "is_deleted = FALSE" in sql, f"Missing is_deleted in: {sql}"
    clear_compiled_cache()


@given(filters=filter_pairs)
@settings(max_examples=200, deadline=2000)
def test_softdelete_with_deleted_no_filter(filters):
    """SoftDelete with include_deleted=True → no is_deleted in SQL."""
    clear_compiled_cache()
    qs = make_sd_qs(filters, include_deleted=True)
    sql, _ = qs._build_select()
    where_part = sql.split("WHERE ", 1)[1] if "WHERE" in sql else ""
    assert "is_deleted" not in where_part, (
        f"Unexpected is_deleted in WHERE: {where_part}"
    )
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Mixin Composition Hypothesis Fuzz Tests ──\n")

    tests = [
        ("SoftDelete cache key differs", test_softdelete_cache_key_differs),
        ("Versioned cache key differs", test_versioned_cache_key_differs),
        ("SoftDelete params match tree", test_softdelete_params_match_tree),
        ("Versioned params match tree", test_versioned_params_match_tree),
        ("SoftDelete cache hit == miss", test_softdelete_cache_hit_equals_miss),
        ("SoftDelete SQL has filter", test_softdelete_sql_has_filter),
        ("SoftDelete with_deleted no filter", test_softdelete_with_deleted_no_filter),
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
    print(f"Mixin fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
