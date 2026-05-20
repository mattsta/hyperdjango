"""
Native _where_compile parity tests — verify Zig output matches Python WhereNode.compile().

# hyper-test: unit

Tests:
1.  Empty node
2.  Leaf with no placeholders (literal SQL)
3.  Leaf with one placeholder
4.  Leaf with multiple placeholders
5.  Negated leaf
6.  Branch AND with two leaves
7.  Branch OR with two leaves
8.  Negated branch
9.  Nested branch (AND inside OR)
10. Mixed deep tree (matches real ORM patterns)
11. Single child branch (no parentheses)
12. Empty children skipped
13. Start_idx > 1
14. Bind value collection
15. Hypothesis-style fuzz across random trees
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango._hyperdjango_native import _where_compile

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


def assert_parity(name: str, node: WhereNode, start_idx: int = 1):
    """Verify _where_compile produces identical output to Python compile()."""
    py_sql, py_params, py_idx = node.compile(start_idx)
    zig_sql, zig_params, zig_idx = _where_compile(node, start_idx)

    sql_match = py_sql == zig_sql
    params_match = py_params == zig_params
    idx_match = py_idx == zig_idx

    detail = ""
    if not sql_match:
        detail = f"\n    py_sql=  {py_sql!r}\n    zig_sql= {zig_sql!r}"
    if not params_match:
        detail += f"\n    py_params=  {py_params}\n    zig_params= {zig_params}"
    if not idx_match:
        detail += f"\n    py_idx={py_idx} zig_idx={zig_idx}"

    check(name, sql_match and params_match and idx_match, detail)


def test_empty_node():
    print("=== Empty Node ===")
    n = WhereNode()
    assert_parity("empty node", n)


def test_leaf_literal():
    print("\n=== Leaf Literal SQL ===")
    n = WhereNode(template="is_deleted = FALSE")
    assert_parity("literal no placeholders", n)


def test_leaf_one_placeholder():
    print("\n=== Leaf One Placeholder ===")
    n = WhereNode(template="name = {}", bind_values=["alice"])
    assert_parity("one placeholder", n)


def test_leaf_multi_placeholders():
    print("\n=== Leaf Multi Placeholders ===")
    n = WhereNode(template="age >= {} AND age <= {}", bind_values=[18, 65])
    assert_parity("two placeholders", n)

    n3 = WhereNode(
        template="a = {} AND b = {} AND c = {}",
        bind_values=["x", 42, True],
    )
    assert_parity("three placeholders", n3)


def test_negated_leaf():
    print("\n=== Negated Leaf ===")
    n = WhereNode(template="status = {}", bind_values=["active"], negated=True)
    assert_parity("negated leaf", n)


def test_branch_and():
    print("\n=== Branch AND ===")
    n = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="name = {}", bind_values=["bob"]),
            WhereNode(template="age >= {}", bind_values=[18]),
        ],
    )
    assert_parity("branch AND two leaves", n)

    n3 = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="a = {}", bind_values=[1]),
            WhereNode(template="b = {}", bind_values=[2]),
            WhereNode(template="c = {}", bind_values=[3]),
        ],
    )
    assert_parity("branch AND three leaves", n3)


def test_branch_or():
    print("\n=== Branch OR ===")
    n = WhereNode(
        connector="OR",
        children=[
            WhereNode(template="email = {}", bind_values=["a@x.com"]),
            WhereNode(template="username = {}", bind_values=["alice"]),
        ],
    )
    assert_parity("branch OR two leaves (parenthesized)", n)


def test_negated_branch():
    print("\n=== Negated Branch ===")
    n = WhereNode(
        connector="AND",
        negated=True,
        children=[
            WhereNode(template="active = {}", bind_values=[True]),
            WhereNode(template="verified = {}", bind_values=[True]),
        ],
    )
    assert_parity("negated branch AND", n)

    n_or = WhereNode(
        connector="OR",
        negated=True,
        children=[
            WhereNode(template="status = {}", bind_values=["banned"]),
            WhereNode(template="status = {}", bind_values=["suspended"]),
        ],
    )
    assert_parity("negated branch OR", n_or)


def test_nested_branch():
    print("\n=== Nested Branches ===")
    # (a AND b) OR c
    n = WhereNode(
        connector="OR",
        children=[
            WhereNode(
                connector="AND",
                children=[
                    WhereNode(template="a = {}", bind_values=[1]),
                    WhereNode(template="b = {}", bind_values=[2]),
                ],
            ),
            WhereNode(template="c = {}", bind_values=[3]),
        ],
    )
    assert_parity("(AND) OR leaf", n)

    # (a OR b) AND (c OR d)
    n2 = WhereNode(
        connector="AND",
        children=[
            WhereNode(
                connector="OR",
                children=[
                    WhereNode(template="a = {}", bind_values=[1]),
                    WhereNode(template="b = {}", bind_values=[2]),
                ],
            ),
            WhereNode(
                connector="OR",
                children=[
                    WhereNode(template="c = {}", bind_values=[3]),
                    WhereNode(template="d = {}", bind_values=[4]),
                ],
            ),
        ],
    )
    assert_parity("(OR) AND (OR)", n2)


def test_single_child_branch():
    print("\n=== Single Child Branch (no parens) ===")
    n = WhereNode(
        connector="OR",
        children=[WhereNode(template="x = {}", bind_values=[42])],
    )
    assert_parity("single child OR (no parens)", n)


def test_empty_children_skipped():
    print("\n=== Empty Children Skipped ===")
    n = WhereNode(
        connector="AND",
        children=[
            WhereNode(),  # empty
            WhereNode(template="name = {}", bind_values=["x"]),
            WhereNode(),  # empty
            WhereNode(template="age = {}", bind_values=[30]),
        ],
    )
    assert_parity("empty children skipped", n)


def test_start_idx_offset():
    print("\n=== Start Index Offset ===")
    n = WhereNode(template="name = {}", bind_values=["alice"])
    assert_parity("start_idx=5", n, start_idx=5)
    assert_parity("start_idx=100", n, start_idx=100)

    n2 = WhereNode(
        connector="AND",
        children=[
            WhereNode(template="a = {}", bind_values=[1]),
            WhereNode(template="b = {}", bind_values=[2]),
        ],
    )
    assert_parity("branch start_idx=10", n2, start_idx=10)


def test_bind_value_types():
    print("\n=== Bind Value Types ===")
    types = [
        ("string", "hello"),
        ("int", 42),
        ("float", 3.14),
        ("bool_true", True),
        ("bool_false", False),
        ("none", None),
        ("list", [1, 2, 3]),
        ("dict", {"key": "value"}),
    ]
    for name, val in types:
        n = WhereNode(template="col = {}", bind_values=[val])
        assert_parity(f"bind value: {name}", n)


def test_realistic_orm_pattern():
    print("\n=== Realistic ORM Patterns ===")

    # Typical Django filter: Model.objects.filter(name="x", age__gte=18, status__in=["a","b"])
    n = WhereNode(
        connector="AND",
        children=[
            WhereNode(template='"name" = {}', bind_values=["alice"]),
            WhereNode(template='"age" >= {}', bind_values=[18]),
            WhereNode(template='"status" = ANY({})', bind_values=[["a", "b"]]),
        ],
    )
    assert_parity("typical filter chain", n)

    # Typical multi-tenant + soft delete mixin pattern
    n2 = WhereNode(
        connector="AND",
        children=[
            WhereNode(template='"is_deleted" = FALSE'),
            WhereNode(template='"tenant_id" = {}', bind_values=[42]),
            WhereNode(template='"name" ILIKE {}', bind_values=["%search%"]),
        ],
    )
    assert_parity("mixin composition", n2)


def test_random_trees():
    print("\n=== Random Tree Fuzz ===")

    rng = random.Random(42)

    def make_random_node(depth: int) -> WhereNode:
        if depth == 0 or rng.random() < 0.3:
            # Leaf
            num_placeholders = rng.randint(0, 3)
            template = "col" + " = {}" * num_placeholders
            return WhereNode(
                template=template,
                bind_values=[rng.randint(1, 1000) for _ in range(num_placeholders)],
                negated=rng.random() < 0.2,
            )
        else:
            # Branch
            num_children = rng.randint(1, 4)
            return WhereNode(
                connector=rng.choice(["AND", "OR"]),
                children=[make_random_node(depth - 1) for _ in range(num_children)],
                negated=rng.random() < 0.2,
            )

    for i in range(50):
        node = make_random_node(depth=rng.randint(1, 4))
        start = rng.randint(1, 100)
        assert_parity(f"random tree #{i}", node, start_idx=start)


def main():
    test_empty_node()
    test_leaf_literal()
    test_leaf_one_placeholder()
    test_leaf_multi_placeholders()
    test_negated_leaf()
    test_branch_and()
    test_branch_or()
    test_negated_branch()
    test_nested_branch()
    test_single_child_branch()
    test_empty_children_skipped()
    test_start_idx_offset()
    test_bind_value_types()
    test_realistic_orm_pattern()
    test_random_trees()

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
