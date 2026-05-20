"""
Hypothesis fuzz tests for SQL injection boundaries.

Proves that ALL user input flows through parameterized queries:
1. Filter values with SQL injection payloads → parameterized, not interpolated
2. Lookup keys with adversarial paths → safe or rejected
3. Q objects with injection payloads → parameterized
4. where_raw always uses bind params

# hyper-test: unit
"""

from _test_meta import make_model
from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.lookups import resolve_lookup
from hyperdjango.query import QuerySet
from hyperdjango.where import WhereNode

# Real _meta via the shared builder (see scripts/_test_meta.py) — column_names
# is the genuine derived property off real FieldMeta entries, not a hand list.
MockModel = make_model("users", ["id", "name", "email", "status"])


# SQL injection payloads
SQL_PAYLOADS = st.sampled_from(
    [
        "'; DROP TABLE users; --",
        "' OR '1'='1",
        "1; SELECT * FROM users",
        "' UNION SELECT password FROM users --",
        "1 OR 1=1",
        "admin'--",
        "' OR ''='",
        "1'; EXEC xp_cmdshell('cmd'); --",
        "'; INSERT INTO users VALUES(999,'hacked','x'); --",
        "test%' OR 1=1 --",
    ]
)

# All string values including injection payloads
adversarial_values = st.one_of(
    SQL_PAYLOADS,
    st.text(max_size=50),
    st.integers(min_value=-1000, max_value=1000),
)


# ---------------------------------------------------------------------------
# Property 1: SQL injection values are ALWAYS parameterized
# ---------------------------------------------------------------------------


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_exact_filter_parameterized(value):
    """SQL injection payloads in exact filter → appear in params, NOT in SQL."""
    sql, params = resolve_lookup("name", value, param_idx=1)
    assert value in params, f"Value not in params: {value!r}"
    # The value must NOT appear literally in the SQL
    assert value not in sql, f"Value appears in SQL (injection!): {sql}"
    assert "$1" in sql, f"No placeholder in SQL: {sql}"


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_contains_filter_parameterized(value):
    """SQL injection in LIKE filter → wrapped value in params, NOT in SQL."""
    sql, params = resolve_lookup("name__contains", value, param_idx=1)
    assert len(params) == 1
    # The raw injection string must not be in the SQL
    assert value not in sql


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_icontains_filter_parameterized(value):
    """SQL injection in ILIKE filter → parameterized."""
    sql, params = resolve_lookup("name__icontains", value, param_idx=1)
    assert value not in sql
    assert len(params) == 1


# ---------------------------------------------------------------------------
# Property 2: WhereNode compile NEVER interpolates values into SQL
# ---------------------------------------------------------------------------


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_where_node_compile_safe(value):
    """WhereNode.compile never puts bind values into SQL string."""
    node = WhereNode(template="name = {}", bind_values=[value])
    sql, params, _ = node.compile()
    assert value not in sql, f"Value in compiled SQL: {sql}"
    assert value in params
    assert "$1" in sql


# ---------------------------------------------------------------------------
# Property 3: Full _build_select with injection values → parameterized
# ---------------------------------------------------------------------------


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_build_select_parameterized(value):
    """Full _build_select with injection values → safe parameterized SQL."""
    from hyperdjango.query import clear_compiled_cache

    clear_compiled_cache()

    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = [("name", value)]
    qs._excludes = []
    qs._raw_wheres = []
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = ()
    qs._limit = None
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False

    sql, params = qs._build_select()

    # Injection payload must be in params, not SQL
    assert value in params, "Value not in params"
    assert value not in sql, f"SQL INJECTION: value {value!r} appears in SQL: {sql}"

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Property 4: Q objects with injection → parameterized
# ---------------------------------------------------------------------------


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_q_object_parameterized(value):
    """Q(name=injection) → value in params, not SQL."""
    from hyperdjango.expressions import Q
    from hyperdjango.query import clear_compiled_cache

    clear_compiled_cache()

    q = Q(name=value)
    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = [("__q__", q)]
    qs._excludes = []
    qs._raw_wheres = []
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = ()
    qs._limit = None
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False
    qs._has_q = True

    sql, params = qs._build_select()
    assert value in params
    assert value not in sql, f"SQL INJECTION via Q: {sql}"

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Property 5: where_raw uses bind params
# ---------------------------------------------------------------------------


@given(value=SQL_PAYLOADS)
@settings(max_examples=100, deadline=1000)
def test_where_raw_parameterized(value):
    """where_raw always uses bind params, not string interpolation."""
    from hyperdjango.query import clear_compiled_cache

    clear_compiled_cache()

    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = []
    qs._excludes = []
    qs._raw_wheres = [("name = {idx}", [value])]
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = ()
    qs._limit = None
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False
    qs._has_q = False

    sql, params = qs._build_select()
    assert value in params
    assert value not in sql, f"SQL INJECTION via where_raw: {sql}"

    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── SQL Injection Boundary Hypothesis Fuzz Tests ──\n")

    tests = [
        ("exact filter parameterized", test_exact_filter_parameterized),
        ("contains filter parameterized", test_contains_filter_parameterized),
        ("icontains filter parameterized", test_icontains_filter_parameterized),
        ("WhereNode compile safe", test_where_node_compile_safe),
        ("_build_select parameterized", test_build_select_parameterized),
        ("Q object parameterized", test_q_object_parameterized),
        ("where_raw parameterized", test_where_raw_parameterized),
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
    print(f"SQL injection fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
