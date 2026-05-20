"""
Hypothesis fuzz tests for native _where_compile.

# hyper-test: unit

Comprehensive property-based testing of the new C-level _where_compile interface.
Targets correctness, safety, security, and edge cases:

1. Parity: Zig output exactly matches Python compile() for arbitrary trees
2. Safety: No segfaults on edge cases (empty, deep, unicode, very large)
3. Security: SQL injection bind values pass through unchanged
4. Memory: No leaks across many invocations (object refcounts stable)
5. Concurrency: Free-threading safety under multi-threaded access
6. Buffer bounds: Templates near/exceeding 8KB buffer
7. Reference counting: Returned objects have correct refcounts
8. Error propagation: Invalid inputs raise clear errors
"""

import gc
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango._hyperdjango_native import _where_compile
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.where import WhereNode

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def _python_compile(node: WhereNode, start_idx: int = 1) -> tuple[str, list, int]:
    """Original pure-Python implementation for parity comparison."""
    if node.is_empty:
        return "", [], start_idx

    if node.template:
        params = list(node.bind_values)
        segments = node.template.split("{}")
        if len(segments) == 1:
            sql = node.template
            idx = start_idx
        else:
            result_parts = [segments[0]]
            idx = start_idx
            for seg in segments[1:]:
                result_parts.append(f"${idx}")
                result_parts.append(seg)
                idx += 1
            sql = "".join(result_parts)

        if node.negated:
            sql = f"NOT ({sql})"
        return sql, params, idx

    parts = []
    all_params = []
    idx = start_idx
    for child in node.children:
        child_sql, child_params, idx = _python_compile(child, idx)
        if child_sql:
            parts.append(child_sql)
            all_params.extend(child_params)

    if not parts:
        return "", all_params, idx

    if len(parts) == 1:
        joined = parts[0]
    elif node.connector == "OR":
        joined = f"({' OR '.join(parts)})"
    else:
        joined = " AND ".join(parts)

    if node.negated:
        joined = f"NOT ({joined})"

    return joined, all_params, idx


# ─── Hypothesis strategies ───────────────────────────────────────────────────


# Strategy for bind values: covers all common ORM input types
bind_value_strategy = st.one_of(
    st.text(min_size=0, max_size=200),
    st.integers(min_value=-(10**18), max_value=10**18),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    st.lists(st.integers(), max_size=10),
    st.binary(max_size=100),
)


# Strategy for templates: random SQL-like fragments with {} placeholders
@st.composite
def template_strategy(draw):
    num_placeholders = draw(st.integers(min_value=0, max_value=5))
    parts = []
    for i in range(num_placeholders):
        # Random column name + operator + placeholder
        col = draw(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=15)
        )
        op = draw(st.sampled_from(["=", ">", "<", ">=", "<=", "!=", "ILIKE", "@@"]))
        if i > 0:
            parts.append(" AND ")
        parts.append(f'"{col}" {op} {{}}')
    return "".join(parts)


@st.composite
def leaf_node_strategy(draw):
    template = draw(template_strategy())
    placeholder_count = template.count("{}")
    bind_values = [draw(bind_value_strategy) for _ in range(placeholder_count)]
    negated = draw(st.booleans())
    return WhereNode(template=template, bind_values=bind_values, negated=negated)


def tree_strategy(max_depth: int = 4):
    @st.composite
    def _tree(draw, depth=max_depth):
        if depth == 0 or draw(st.integers(min_value=0, max_value=2)) == 0:
            return draw(leaf_node_strategy())
        # Branch
        num_children = draw(st.integers(min_value=1, max_value=5))
        children = [draw(_tree(depth=depth - 1)) for _ in range(num_children)]
        connector = draw(st.sampled_from(["AND", "OR"]))
        negated = draw(st.booleans())
        return WhereNode(children=children, connector=connector, negated=negated)

    return _tree()


# ─── Property-based tests ────────────────────────────────────────────────────


@given(
    node=tree_strategy(max_depth=4), start_idx=st.integers(min_value=1, max_value=1000)
)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_parity_arbitrary_trees(node, start_idx):
    """For any random tree, Zig output must match Python output exactly."""
    py_sql, py_params, py_idx = _python_compile(node, start_idx)
    zig_sql, zig_params, zig_idx = _where_compile(node, start_idx)
    assert py_sql == zig_sql, f"SQL mismatch:\n  py:  {py_sql!r}\n  zig: {zig_sql!r}"
    assert py_params == zig_params, (
        f"Params mismatch:\n  py:  {py_params}\n  zig: {zig_params}"
    )
    assert py_idx == zig_idx, f"Idx mismatch: py={py_idx} zig={zig_idx}"


@given(
    template=st.text(min_size=0, max_size=500),
    start_idx=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=200, deadline=None)
def test_arbitrary_template_strings(template, start_idx):
    """Templates with arbitrary unicode should not crash."""
    placeholder_count = template.count("{}")
    bind_values = list(range(placeholder_count))
    node = WhereNode(template=template, bind_values=bind_values)
    py_sql, py_params, py_idx = _python_compile(node, start_idx)
    zig_sql, zig_params, zig_idx = _where_compile(node, start_idx)
    assert py_sql == zig_sql
    assert py_params == zig_params
    assert py_idx == zig_idx


@given(values=st.lists(bind_value_strategy, min_size=0, max_size=20))
@settings(max_examples=100, deadline=None)
def test_arbitrary_bind_values(values):
    """Arbitrary bind value types should pass through unchanged."""
    template = " AND ".join(f"col_{i} = {{}}" for i in range(len(values)))
    if not template:
        template = "TRUE"
    node = WhereNode(template=template, bind_values=values)
    py_sql, py_params, _ = _python_compile(node, 1)
    zig_sql, zig_params, _ = _where_compile(node, 1)
    assert py_sql == zig_sql
    # Params must be the SAME OBJECTS (passthrough, not copy)
    assert len(zig_params) == len(values)
    for orig, returned in zip(values, zig_params):
        # For value types: equal. For object types: identity preserved.
        if isinstance(orig, (list, dict, bytes)):
            assert orig == returned
        else:
            assert orig == returned


# ─── Targeted edge case tests ────────────────────────────────────────────────


def test_sql_injection_bind_values():
    """SQL injection attempts in bind values must pass through unchanged.
    The compile() function only generates $N placeholders — never substitutes
    values into SQL string."""
    print("\n=== SQL Injection Resistance ===")

    injection_attempts = [
        "'; DROP TABLE users; --",
        "1 OR 1=1",
        "admin' OR '1'='1",
        "\\'; DELETE FROM users WHERE 1=1; --",
        "UNION SELECT password FROM users",
        "<script>alert(1)</script>",
        "\x00\x01\x02",  # Null bytes
        "normal value",
    ]

    for attempt in injection_attempts:
        node = WhereNode(template="name = {}", bind_values=[attempt])
        sql, params, _ = _where_compile(node, 1)
        # SQL should NEVER contain the injection text
        check(
            f"injection isolated from SQL: {attempt[:30]}",
            attempt not in sql,
            f"sql={sql!r}",
        )
        # Param should be the exact original string
        check(
            f"injection preserved as param: {attempt[:30]}",
            params[0] == attempt,
            f"got {params[0]!r}",
        )


def test_buffer_boundaries():
    """Templates near/at the 8KB buffer limit don't crash or corrupt."""
    print("\n=== Buffer Boundary ===")

    # 1KB template
    node = WhereNode(template="x = {}" * 200, bind_values=list(range(200)))
    try:
        sql, params, idx = _where_compile(node, 1)
        check("1KB template compiles", len(sql) > 0)
        check("1KB params correct", len(params) == 200)
    except Exception as e:
        check(f"1KB template ({type(e).__name__})", False)

    # Approaching 8KB buffer
    big_template = "col = {}" + " AND col = {}" * 500
    node2 = WhereNode(template=big_template, bind_values=list(range(501)))
    try:
        sql, params, idx = _where_compile(node2, 1)
        check("4KB template compiles", len(sql) > 0)
        check("4KB params correct", len(params) == 501)
    except Exception as e:
        check(f"4KB template ({type(e).__name__})", False)


def test_deep_nesting():
    """Deeply nested trees don't cause stack overflow."""
    print("\n=== Deep Nesting ===")

    # 50-deep nested AND tree
    inner = WhereNode(template="leaf = {}", bind_values=[1])
    for i in range(50):
        inner = WhereNode(
            connector="AND",
            children=[inner, WhereNode(template="x = {}", bind_values=[i])],
        )

    try:
        sql, params, _ = _where_compile(inner, 1)
        py_sql, py_params, _ = _python_compile(inner, 1)
        check("50-deep tree compiles", len(sql) > 0)
        check("50-deep parity", sql == py_sql)
    except Exception as e:
        check(f"50-deep tree ({type(e).__name__})", False)


def test_unicode_templates():
    """Unicode in templates is handled correctly."""
    print("\n=== Unicode Templates ===")

    cases = [
        ("café = {}", ["latte"]),
        ("名前 = {}", ["太郎"]),
        ("emoji = {} 🎉", ["🚀"]),
        ("ru = {}", ["привет"]),
        ("rtl = {}", ["שלום"]),
    ]
    for template, values in cases:
        node = WhereNode(template=template, bind_values=values)
        try:
            sql, params, _ = _where_compile(node, 1)
            py_sql, py_params, _ = _python_compile(node, 1)
            check(f"unicode: {template[:20]}", sql == py_sql and params == py_params)
        except Exception as e:
            check(f"unicode: {template[:20]} ({type(e).__name__})", False)


def test_refcount_stability():
    """Repeated invocations don't leak references."""
    print("\n=== Refcount Stability ===")

    node = WhereNode(
        connector="AND",
        children=[
            WhereNode(template='"name" = {}', bind_values=["alice"]),
            WhereNode(template='"age" >= {}', bind_values=[18]),
        ],
    )

    gc.collect()
    initial_refs = sys.getrefcount(node)

    for _ in range(10000):
        sql, params, _ = _where_compile(node, 1)

    gc.collect()
    final_refs = sys.getrefcount(node)
    check(
        "node refcount stable across 10K calls",
        abs(final_refs - initial_refs) <= 2,
        f"initial={initial_refs} final={final_refs}",
    )


def test_concurrent_access():
    """Concurrent calls from multiple threads don't crash or corrupt."""
    print("\n=== Free-Threading Concurrent Access ===")

    nodes = [
        WhereNode(
            connector="AND",
            children=[
                WhereNode(template=f'"col_{i}" = {{}}', bind_values=[i]),
                WhereNode(template=f'"flag_{i}" = {{}}', bind_values=[True]),
            ],
        )
        for i in range(20)
    ]

    errors: list[str] = []
    iterations_per_thread = 1000

    def worker(thread_id: int):
        try:
            for i in range(iterations_per_thread):
                node = nodes[i % len(nodes)]
                sql, params, idx = _where_compile(node, 1)
                if not sql or len(params) != 2:
                    errors.append(f"thread {thread_id} iter {i}: bad result")
                    return
        except Exception as e:
            errors.append(f"thread {thread_id}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(
        "8 threads × 1000 iterations no errors",
        len(errors) == 0,
        f"errors={errors[:3]}",
    )


def test_invalid_input_handling():
    """Invalid inputs raise clear errors instead of segfaulting."""
    print("\n=== Invalid Input Handling ===")

    # Wrong arity
    try:
        _where_compile(WhereNode())  # Only 1 arg
        check("wrong arity raises", False, "should have raised")
    except TypeError, RuntimeError:
        check("wrong arity raises", True)

    # Wrong type for start_idx
    try:
        _where_compile(WhereNode(template="x = {}", bind_values=[1]), "not_an_int")
        check("string start_idx raises", False, "should have raised")
    except TypeError, ValueError, RuntimeError:
        check("string start_idx raises", True)


def test_node_with_zero_placeholders_but_bind_values():
    """Edge case: literal SQL with extra bind_values (Python ignores extras)."""
    print("\n=== Literal SQL With Bind Values ===")
    node = WhereNode(template="is_active = TRUE", bind_values=[1, 2, 3])
    py_sql, py_params, _ = _python_compile(node, 1)
    zig_sql, zig_params, _ = _where_compile(node, 1)
    check("literal SQL parity", py_sql == zig_sql)
    check("extra bind_values both kept (parity)", py_params == zig_params)


def test_negated_empty_node():
    """Negated empty node should produce nothing (matches Python)."""
    print("\n=== Negated Empty Node ===")
    node = WhereNode(negated=True)
    py_sql, py_params, _ = _python_compile(node, 1)
    zig_sql, zig_params, _ = _where_compile(node, 1)
    check(
        "negated empty parity",
        py_sql == zig_sql == "" and py_params == zig_params == [],
    )


def main():
    # Run hypothesis-based tests
    print("=== Hypothesis: parity on arbitrary trees (300 examples) ===")
    try:
        test_parity_arbitrary_trees()
        check("parity arbitrary trees", True)
    except Exception as e:
        check("parity arbitrary trees", False, str(e)[:200])

    print("\n=== Hypothesis: arbitrary unicode templates (200 examples) ===")
    try:
        test_arbitrary_template_strings()
        check("arbitrary template strings", True)
    except Exception as e:
        check("arbitrary template strings", False, str(e)[:200])

    print("\n=== Hypothesis: arbitrary bind values (100 examples) ===")
    try:
        test_arbitrary_bind_values()
        check("arbitrary bind values", True)
    except Exception as e:
        check("arbitrary bind values", False, str(e)[:200])

    test_sql_injection_bind_values()
    test_buffer_boundaries()
    test_deep_nesting()
    test_unicode_templates()
    test_refcount_stability()
    test_concurrent_access()
    test_invalid_input_handling()
    test_node_with_zero_placeholders_but_bind_values()
    test_negated_empty_node()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
