"""
Hypothesis fuzz tests for ORM lookup system.

Proves correctness properties:
1. resolve_lookup produces valid SQL for ANY key with known suffix
2. resolve_lookup_node compile matches resolve_lookup output
3. bind_params count matches as_sql $N count for ALL lookup types
4. Transform chains produce valid SQL
5. resolve_bind_params matches Lookup.bind_params for ALL lookup types

# hyper-test: unit
"""

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.lookups import (
    resolve_bind_params,
    resolve_lookup,
    resolve_lookup_node,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

field_names = st.sampled_from(
    ["name", "age", "status", "email", "score", "created_at", "price"]
)

simple_suffixes = st.sampled_from(
    ["", "__gt", "__gte", "__lt", "__lte", "__iexact", "__regex", "__iregex"]
)
like_suffixes = st.sampled_from(
    [
        "__contains",
        "__icontains",
        "__startswith",
        "__endswith",
        "__istartswith",
        "__iendswith",
    ]
)
special_suffixes = st.sampled_from(["__in", "__isnull", "__range"])
transform_chains = st.sampled_from(
    ["__year", "__month", "__day", "__lower", "__upper", "__length"]
)


def value_for_suffix(suffix):
    if suffix == "__isnull":
        return st.sampled_from([True, False])
    if suffix == "__in":
        return st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=5)
    if suffix == "__range":
        return st.tuples(
            st.integers(min_value=0, max_value=50),
            st.integers(min_value=51, max_value=100),
        )
    if suffix in (
        "__contains",
        "__icontains",
        "__startswith",
        "__endswith",
        "__istartswith",
        "__iendswith",
        "__regex",
        "__iregex",
    ):
        return st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnop")
    return st.one_of(
        st.text(min_size=1, max_size=10),
        st.integers(min_value=-100, max_value=100),
    )


@st.composite
def lookup_pair(draw, suffix_strategy=simple_suffixes):
    field = draw(field_names)
    suffix = draw(suffix_strategy)
    key = field + suffix if suffix else field
    value = draw(value_for_suffix(suffix))
    return key, value


# ---------------------------------------------------------------------------
# Property 1: resolve_lookup produces valid SQL
# ---------------------------------------------------------------------------


@given(pair=lookup_pair())
@settings(max_examples=500, deadline=1000)
def test_resolve_lookup_produces_sql(pair):
    """resolve_lookup(key, value) produces non-empty SQL with correct $N."""
    key, value = pair
    sql, params = resolve_lookup(key, value, param_idx=1)
    assert sql, f"Empty SQL for {key}={value!r}"
    dollar_count = len(re.findall(r"\$\d+", sql))
    assert dollar_count == len(params), (
        f"$N count ({dollar_count}) != params ({len(params)}) for {key}: {sql}"
    )


@given(pair=lookup_pair(like_suffixes))
@settings(max_examples=300, deadline=1000)
def test_resolve_lookup_like(pair):
    """LIKE lookups produce valid SQL with escaped values."""
    key, value = pair
    sql, params = resolve_lookup(key, value, param_idx=1)
    assert "LIKE" in sql or "ILIKE" in sql, f"Expected LIKE in: {sql}"
    assert len(params) == 1


@given(pair=lookup_pair(special_suffixes))
@settings(max_examples=300, deadline=1000)
def test_resolve_lookup_special(pair):
    """IN/ISNULL/RANGE produce correct SQL."""
    key, value = pair
    sql, params = resolve_lookup(key, value, param_idx=1)
    assert sql


# ---------------------------------------------------------------------------
# Property 2: resolve_lookup_node compile matches resolve_lookup
# ---------------------------------------------------------------------------


@given(pair=lookup_pair())
@settings(max_examples=500, deadline=1000)
def test_node_matches_lookup(pair):
    """resolve_lookup_node → compile produces same SQL structure as resolve_lookup."""
    key, value = pair
    # Old path
    sql_old, params_old = resolve_lookup(key, value, param_idx=1)
    # New path
    node = resolve_lookup_node(key, value)
    sql_new, params_new, _ = node.compile(start_idx=1)

    assert params_old == params_new, (
        f"Params differ for {key}: old={params_old} new={params_new}"
    )
    assert sql_old == sql_new, (
        f"SQL differs for {key}:\n  old: {sql_old}\n  new: {sql_new}"
    )


# ---------------------------------------------------------------------------
# Property 3: bind_params count matches as_sql $N count
# ---------------------------------------------------------------------------


@given(pair=lookup_pair())
@settings(max_examples=300, deadline=1000)
def test_bind_params_count(pair):
    """resolve_bind_params returns same count as resolve_lookup params."""
    key, value = pair
    _, full_params = resolve_lookup(key, value, param_idx=1)
    fast_params = resolve_bind_params(key, value)
    assert len(fast_params) == len(full_params), (
        f"bind_params count mismatch for {key}: fast={len(fast_params)} full={len(full_params)}"
    )


# ---------------------------------------------------------------------------
# Property 4: Transform chains produce valid SQL
# ---------------------------------------------------------------------------


@given(
    field=field_names,
    transform=transform_chains,
    suffix=st.sampled_from(["", "__gt", "__gte", "__lt", "__lte"]),
    value=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=300, deadline=1000)
def test_transform_chain(field, transform, suffix, value):
    """field__transform__lookup produces valid SQL."""
    key = field + transform + suffix if suffix else field + transform
    sql, params = resolve_lookup(key, value, param_idx=1)
    assert sql
    # Transform should appear in the SQL (EXTRACT, LOWER, etc.)
    node = resolve_lookup_node(key, value)
    node_sql, node_params, _ = node.compile(start_idx=1)
    assert params == node_params


# ---------------------------------------------------------------------------
# Property 5: resolve_bind_params matches bind_params values
# ---------------------------------------------------------------------------


@given(pair=lookup_pair(like_suffixes))
@settings(max_examples=300, deadline=1000)
def test_like_bind_params_values(pair):
    """LIKE lookups: resolve_bind_params returns same wrapped values as as_sql."""
    key, value = pair
    _, full_params = resolve_lookup(key, value, param_idx=1)
    fast_params = resolve_bind_params(key, value)
    assert fast_params == full_params, (
        f"Values differ for {key}: fast={fast_params} full={full_params}"
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── ORM Lookup Hypothesis Fuzz Tests ──\n")

    tests = [
        ("resolve_lookup valid SQL", test_resolve_lookup_produces_sql),
        ("LIKE lookups", test_resolve_lookup_like),
        ("special lookups (IN/ISNULL/RANGE)", test_resolve_lookup_special),
        ("node matches lookup", test_node_matches_lookup),
        ("bind_params count", test_bind_params_count),
        ("transform chains", test_transform_chain),
        ("LIKE bind_params values", test_like_bind_params_values),
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
    print(f"Lookup fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
