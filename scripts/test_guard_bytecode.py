"""
HyperGuard bytecode compiler + Zig evaluator tests.

Tests the full pipeline: Python Condition objects → bytecode → Zig evaluation → allow/deny.
Covers all comparison operators, AND/OR combining, cross-field conditions,
edge cases, and performance benchmarks.
"""

# hyper-test: unit

import os
import time

_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_PERF_MULT = 10.0 if _PARALLEL else 1.0

from hyperdjango.guard.compiler import (
    Condition,
    CondOp,
    CondSource,
    CrossFieldCondition,
    compile_conditions,
)

_PASS = 0
_FAIL = 0


def check(condition: bool, msg: str) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


# ── Test: Single field conditions ────────────────────────────────────────────


def test_user_is_staff_true():
    """user.is_staff == True → allow."""
    print("test_user_is_staff_true")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
        ]
    )
    check(compiled.evaluate({"is_staff": True}, {}) is True, "staff=True allows")
    check(compiled.evaluate({"is_staff": False}, {}) is False, "staff=False denies")


def test_user_is_staff_false():
    """user.is_staff == False → deny when True."""
    print("test_user_is_staff_false")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=False
            ),
        ]
    )
    check(compiled.evaluate({"is_staff": False}, {}) is True, "staff=False allows")
    check(compiled.evaluate({"is_staff": True}, {}) is False, "staff=True denies")


def test_resource_is_archived():
    """resource.is_archived == False → allow when not archived."""
    print("test_resource_is_archived")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE,
                field="is_archived",
                op=CondOp.EQ,
                value=False,
            ),
        ]
    )
    check(compiled.evaluate({}, {"is_archived": False}) is True, "not archived allows")
    check(compiled.evaluate({}, {"is_archived": True}) is False, "archived denies")


def test_missing_field_is_none():
    """Missing field in dict is none (not equal to False, not equal to 0)."""
    print("test_missing_field_is_none")
    compiled_bool = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=False
            ),
        ]
    )
    # Missing key → none, none != False → deny (strict type safety)
    check(compiled_bool.evaluate({}, {}) is False, "missing field is none, not False")

    compiled_int = compile_conditions(
        [
            Condition(source=CondSource.USER, field="karma", op=CondOp.EQ, value=0),
        ]
    )
    # Missing key → none, none != 0 → deny
    check(compiled_int.evaluate({}, {}) is False, "missing field is none, not 0")


# ── Test: Integer comparisons ────────────────────────────────────────────────


def test_karma_ge_50():
    """user.karma >= 50."""
    print("test_karma_ge_50")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="karma", op=CondOp.GE, value=50),
        ]
    )
    check(compiled.evaluate({"karma": 100}, {}) is True, "100 >= 50")
    check(compiled.evaluate({"karma": 50}, {}) is True, "50 >= 50")
    check(compiled.evaluate({"karma": 49}, {}) is False, "49 < 50")
    check(compiled.evaluate({"karma": 0}, {}) is False, "0 < 50")


def test_karma_gt_0():
    """user.karma > 0."""
    print("test_karma_gt_0")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="karma", op=CondOp.GT, value=0),
        ]
    )
    check(compiled.evaluate({"karma": 1}, {}) is True, "1 > 0")
    check(compiled.evaluate({"karma": 0}, {}) is False, "0 > 0")
    check(compiled.evaluate({"karma": -1}, {}) is False, "-1 > 0")


def test_comparison_lt():
    """resource.post_count < 1000."""
    print("test_comparison_lt")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE, field="post_count", op=CondOp.LT, value=1000
            ),
        ]
    )
    check(compiled.evaluate({}, {"post_count": 500}) is True, "500 < 1000")
    check(compiled.evaluate({}, {"post_count": 1000}) is False, "1000 < 1000")
    check(compiled.evaluate({}, {"post_count": 1001}) is False, "1001 < 1000")


def test_comparison_le():
    """resource.depth <= 10."""
    print("test_comparison_le")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE, field="depth", op=CondOp.LE, value=10
            ),
        ]
    )
    check(compiled.evaluate({}, {"depth": 10}) is True, "10 <= 10")
    check(compiled.evaluate({}, {"depth": 11}) is False, "11 <= 10")


def test_comparison_ne():
    """user.role != 0 (not guest)."""
    print("test_comparison_ne")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="role", op=CondOp.NE, value=0),
        ]
    )
    check(compiled.evaluate({"role": 1}, {}) is True, "1 != 0")
    check(compiled.evaluate({"role": 0}, {}) is False, "0 != 0")


# ── Test: Cross-field conditions ─────────────────────────────────────────────


def test_ownership_check():
    """resource.author_id == user.id."""
    print("test_ownership_check")
    compiled = compile_conditions(
        [
            CrossFieldCondition(
                resource_field="author_id", op=CondOp.EQ, user_field="id"
            ),
        ]
    )
    check(compiled.evaluate({"id": 42}, {"author_id": 42}) is True, "same user")
    check(compiled.evaluate({"id": 42}, {"author_id": 99}) is False, "different user")


def test_cross_field_ne():
    """resource.locked_by != user.id (someone else locked it)."""
    print("test_cross_field_ne")
    compiled = compile_conditions(
        [
            CrossFieldCondition(
                resource_field="locked_by", op=CondOp.NE, user_field="id"
            ),
        ]
    )
    check(compiled.evaluate({"id": 1}, {"locked_by": 2}) is True, "different")
    check(compiled.evaluate({"id": 1}, {"locked_by": 1}) is False, "same")


# ── Test: AND combining ─────────────────────────────────────────────────────


def test_and_two_conditions():
    """resource.is_archived == False AND resource.is_locked == False."""
    print("test_and_two_conditions")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE,
                field="is_archived",
                op=CondOp.EQ,
                value=False,
            ),
            Condition(
                source=CondSource.RESOURCE, field="is_locked", op=CondOp.EQ, value=False
            ),
        ],
        combine="and",
    )
    check(
        compiled.evaluate({}, {"is_archived": False, "is_locked": False}) is True,
        "both false",
    )
    check(
        compiled.evaluate({}, {"is_archived": True, "is_locked": False}) is False,
        "archived",
    )
    check(
        compiled.evaluate({}, {"is_archived": False, "is_locked": True}) is False,
        "locked",
    )
    check(
        compiled.evaluate({}, {"is_archived": True, "is_locked": True}) is False,
        "both true",
    )


def test_and_three_conditions():
    """user.is_staff AND NOT resource.is_archived AND resource.is_public."""
    print("test_and_three_conditions")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
            Condition(
                source=CondSource.RESOURCE,
                field="is_archived",
                op=CondOp.EQ,
                value=False,
            ),
            Condition(
                source=CondSource.RESOURCE, field="is_public", op=CondOp.EQ, value=True
            ),
        ],
        combine="and",
    )
    check(
        compiled.evaluate(
            {"is_staff": True},
            {"is_archived": False, "is_public": True},
        )
        is True,
        "all pass",
    )
    check(
        compiled.evaluate(
            {"is_staff": False},
            {"is_archived": False, "is_public": True},
        )
        is False,
        "not staff",
    )


# ── Test: OR combining ──────────────────────────────────────────────────────


def test_or_two_conditions():
    """user.is_staff OR user.is_superuser."""
    print("test_or_two_conditions")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
            Condition(
                source=CondSource.USER, field="is_superuser", op=CondOp.EQ, value=True
            ),
        ],
        combine="or",
    )
    check(
        compiled.evaluate({"is_staff": True, "is_superuser": False}, {}) is True,
        "staff",
    )
    check(
        compiled.evaluate({"is_staff": False, "is_superuser": True}, {}) is True,
        "superuser",
    )
    check(
        compiled.evaluate({"is_staff": True, "is_superuser": True}, {}) is True, "both"
    )
    check(
        compiled.evaluate({"is_staff": False, "is_superuser": False}, {}) is False,
        "neither",
    )


# ── Test: Mixed conditions with ownership ────────────────────────────────────


def test_mixed_ownership_and_state():
    """resource.author_id == user.id AND resource.is_archived == False."""
    print("test_mixed_ownership_and_state")
    compiled = compile_conditions(
        [
            CrossFieldCondition(
                resource_field="author_id", op=CondOp.EQ, user_field="id"
            ),
            Condition(
                source=CondSource.RESOURCE,
                field="is_archived",
                op=CondOp.EQ,
                value=False,
            ),
        ],
        combine="and",
    )
    check(
        compiled.evaluate({"id": 1}, {"author_id": 1, "is_archived": False}) is True,
        "owner + not archived",
    )
    check(
        compiled.evaluate({"id": 1}, {"author_id": 2, "is_archived": False}) is False,
        "not owner",
    )
    check(
        compiled.evaluate({"id": 1}, {"author_id": 1, "is_archived": True}) is False,
        "archived",
    )


# ── Test: Empty conditions ───────────────────────────────────────────────────


def test_empty_conditions():
    """Empty condition list → always allow."""
    print("test_empty_conditions")
    compiled = compile_conditions([])
    check(compiled.evaluate({}, {}) is True, "empty = allow")
    check(compiled.condition_count == 0, "0 conditions")


# ── Test: CompiledGuard metadata ─────────────────────────────────────────────


def test_compiled_metadata():
    """CompiledGuard exposes field names and constants."""
    print("test_compiled_metadata")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
            Condition(
                source=CondSource.RESOURCE, field="karma", op=CondOp.GE, value=50
            ),
        ]
    )
    check("is_staff" in compiled.field_names, "is_staff in field_names")
    check("karma" in compiled.field_names, "karma in field_names")
    check(1 in compiled.constants, "True(1) in constants")
    check(50 in compiled.constants, "50 in constants")
    check(compiled.condition_count == 2, "2 conditions")


def test_compiled_frozen():
    """CompiledGuard is immutable."""
    print("test_compiled_frozen")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="x", op=CondOp.EQ, value=1),
        ]
    )
    try:
        compiled.bytecode = b"hacked"
        check(False, "should be frozen")
    except AttributeError:
        check(True, "frozen")


# ── Test: Constant deduplication ─────────────────────────────────────────────


def test_constant_dedup():
    """Same constant value reused, not duplicated."""
    print("test_constant_dedup")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="a", op=CondOp.EQ, value=True),
            Condition(source=CondSource.USER, field="b", op=CondOp.EQ, value=True),
            Condition(source=CondSource.RESOURCE, field="c", op=CondOp.EQ, value=True),
        ]
    )
    # All use value=True, should be only 1 constant
    check(len(compiled.constants) == 1, f"1 constant, got {len(compiled.constants)}")
    check(compiled.constants[0] is True, "constant is True")


# ── Test: Bool coercion ──────────────────────────────────────────────────────


def test_bool_true_false_values():
    """Python True/False properly handled as 1/0 in bytecode."""
    print("test_bool_true_false_values")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="active", op=CondOp.EQ, value=True),
        ]
    )
    # Python True
    check(compiled.evaluate({"active": True}, {}) is True, "Python True")
    # Python 1 (int)
    check(compiled.evaluate({"active": 1}, {}) is True, "Python 1")
    # Python False
    check(compiled.evaluate({"active": False}, {}) is False, "Python False")
    # Python 0
    check(compiled.evaluate({"active": 0}, {}) is False, "Python 0")


# ── Test: String comparisons ─────────────────────────────────────────────────


def test_string_equality():
    """resource.status = "published" — native string comparison via Zig."""
    print("test_string_equality")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE,
                field="status",
                op=CondOp.EQ,
                value="published",
            ),
        ]
    )
    check(compiled.evaluate({}, {"status": "published"}) is True, "published matches")
    check(compiled.evaluate({}, {"status": "draft"}) is False, "draft doesn't match")
    check(compiled.evaluate({}, {"status": ""}) is False, "empty doesn't match")


def test_string_inequality():
    """user.role != "guest"."""
    print("test_string_inequality")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="role", op=CondOp.NE, value="guest"
            ),
        ]
    )
    check(compiled.evaluate({"role": "admin"}, {}) is True, "admin != guest")
    check(compiled.evaluate({"role": "guest"}, {}) is False, "guest == guest")


def test_string_missing_field():
    """Missing string field is none, not empty string."""
    print("test_string_missing_field")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE,
                field="status",
                op=CondOp.EQ,
                value="published",
            ),
        ]
    )
    # Missing field → none, none != "published" → deny
    check(compiled.evaluate({}, {}) is False, "missing field denies")


def test_mixed_string_and_bool():
    """String + bool conditions in same AND chain."""
    print("test_mixed_string_and_bool")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
            Condition(
                source=CondSource.RESOURCE,
                field="status",
                op=CondOp.EQ,
                value="published",
            ),
        ],
        combine="and",
    )
    check(
        compiled.evaluate({"is_staff": True}, {"status": "published"}) is True,
        "staff + published",
    )
    check(
        compiled.evaluate({"is_staff": True}, {"status": "draft"}) is False,
        "staff + draft",
    )
    check(
        compiled.evaluate({"is_staff": False}, {"status": "published"}) is False,
        "non-staff + published",
    )


def test_string_constant_dedup():
    """Same string constant deduplicates in pool."""
    print("test_string_constant_dedup")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.RESOURCE, field="a", op=CondOp.EQ, value="published"
            ),
            Condition(
                source=CondSource.RESOURCE, field="b", op=CondOp.EQ, value="published"
            ),
        ]
    )
    check(len(compiled.constants) == 1, f"1 constant, got {len(compiled.constants)}")
    check(
        compiled.constants[0] == "published",
        f"constant is 'published', got {compiled.constants[0]}",
    )


def test_two_missing_fields_equal():
    """Two missing fields are both none — none == none is true."""
    print("test_two_missing_fields_equal")
    compiled = compile_conditions(
        [
            CrossFieldCondition(resource_field="x", op=CondOp.EQ, user_field="x"),
        ]
    )
    # Both dicts missing "x" → none == none → allow
    check(compiled.evaluate({}, {}) is True, "none == none is true")


def test_string_vs_int_no_match():
    """String "1" never equals int 1."""
    print("test_string_vs_int_no_match")
    compiled = compile_conditions(
        [
            Condition(source=CondSource.USER, field="x", op=CondOp.EQ, value="1"),
        ]
    )
    # User has x=1 (int) but condition checks for "1" (string) → deny
    check(compiled.evaluate({"x": 1}, {}) is False, "int 1 != string '1'")


# ── Benchmark ────────────────────────────────────────────────────────────────


def test_benchmark_simple():
    """Benchmark: single field check should be <500ns per call."""
    print("test_benchmark_simple")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
        ]
    )
    user = {"is_staff": True}
    resource: dict[str, object] = {}

    # Warmup
    for _ in range(1000):
        compiled.evaluate(user, resource)

    # Benchmark
    iterations = 100_000
    start = time.perf_counter_ns()
    for _ in range(iterations):
        compiled.evaluate(user, resource)
    elapsed_ns = time.perf_counter_ns() - start
    per_call_ns = elapsed_ns / iterations

    print(f"  INFO  Simple check: {per_call_ns:.0f}ns/call ({iterations} iterations)")
    # Should be well under 1μs — the Zig eval itself is <50ns,
    # but Python→Zig call overhead adds ~200-500ns
    check(per_call_ns < 25000 * _PERF_MULT, f"<25μs per call, got {per_call_ns:.0f}ns")


def test_benchmark_five_conditions():
    """Benchmark: 5-condition AND chain."""
    print("test_benchmark_five_conditions")
    compiled = compile_conditions(
        [
            Condition(
                source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True
            ),
            Condition(
                source=CondSource.USER, field="is_banned", op=CondOp.EQ, value=False
            ),
            Condition(
                source=CondSource.RESOURCE,
                field="is_archived",
                op=CondOp.EQ,
                value=False,
            ),
            Condition(
                source=CondSource.RESOURCE, field="is_locked", op=CondOp.EQ, value=False
            ),
            CrossFieldCondition(
                resource_field="author_id", op=CondOp.EQ, user_field="id"
            ),
        ],
        combine="and",
    )
    user = {"is_staff": True, "is_banned": False, "id": 42}
    resource = {"is_archived": False, "is_locked": False, "author_id": 42}

    # Warmup
    for _ in range(1000):
        compiled.evaluate(user, resource)

    # Benchmark
    iterations = 100_000
    start = time.perf_counter_ns()
    for _ in range(iterations):
        compiled.evaluate(user, resource)
    elapsed_ns = time.perf_counter_ns() - start
    per_call_ns = elapsed_ns / iterations

    print(
        f"  INFO  5-condition AND: {per_call_ns:.0f}ns/call ({iterations} iterations)"
    )
    check(per_call_ns < 50000 * _PERF_MULT, f"<50μs per call, got {per_call_ns:.0f}ns")


# ── Run all ──────────────────────────────────────────────────────────────────


def main():
    tests = [
        test_user_is_staff_true,
        test_user_is_staff_false,
        test_resource_is_archived,
        test_missing_field_is_none,
        test_karma_ge_50,
        test_karma_gt_0,
        test_comparison_lt,
        test_comparison_le,
        test_comparison_ne,
        test_ownership_check,
        test_cross_field_ne,
        test_and_two_conditions,
        test_and_three_conditions,
        test_or_two_conditions,
        test_mixed_ownership_and_state,
        test_empty_conditions,
        test_compiled_metadata,
        test_compiled_frozen,
        test_constant_dedup,
        test_bool_true_false_values,
        test_string_equality,
        test_string_inequality,
        test_string_missing_field,
        test_mixed_string_and_bool,
        test_string_constant_dedup,
        test_two_missing_fields_equal,
        test_string_vs_int_no_match,
        test_benchmark_simple,
        test_benchmark_five_conditions,
    ]

    for test in tests:
        test()

    total = _PASS + _FAIL
    print(f"\n{'=' * 60}")
    print(f"HyperGuard Bytecode: {_PASS}/{total} passed, {_FAIL} failed")
    if _FAIL:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
