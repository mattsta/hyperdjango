#!/usr/bin/env python3
"""
Tests for {% break %} and {% continue %} in Zig template engine for loops.

Tests:
- Basic break (exit loop)
- Basic continue (skip iteration)
- Break/continue inside conditionals
- Nested loops with break/continue (applies to innermost)
- Break with loop.index
- Continue preserves loop variables
- Break in empty loop (no-op)

Usage:
    uv run hyper-test template_break_continue
"""

# hyper-test: unit

import sys
import traceback

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}

from hyperdjango._hyperdjango_native import (
    _template_compile,
    _template_render,
)


def render(template_src, context=None):
    """Compile and render a template with the native Zig engine."""
    capsule = _template_compile(template_src, "<test>")
    result = _template_render(capsule, context or {})
    return result.decode() if isinstance(result, bytes) else result


def test(name):
    def decorator(func):
        def wrapper():
            try:
                func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# BREAK TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("break: exit loop immediately")
def test_break_basic():
    result = render(
        "{% for i in items %}{{ i }}{% if i == 3 %}{% break %}{% endif %},{% endfor %}",
        {"items": [1, 2, 3, 4, 5]},
    )
    # Should output: 1,2,3 (break after 3, before the comma)
    assert result == "1,2,3", f"Got: {result!r}"


@test("break: unconditional break on first iteration")
def test_break_first():
    result = render(
        "{% for i in items %}{% break %}{{ i }}{% endfor %}DONE",
        {"items": [1, 2, 3]},
    )
    assert result == "DONE", f"Got: {result!r}"


@test("break: in conditional only triggers when condition met")
def test_break_conditional():
    result = render(
        "{% for i in items %}{% if i > 3 %}{% break %}{% endif %}{{ i }},{% endfor %}",
        {"items": [1, 2, 3, 4, 5]},
    )
    assert result == "1,2,3,", f"Got: {result!r}"


@test("break: loop.index correct before break")
def test_break_loop_index():
    result = render(
        "{% for i in items %}{{ loop.index }}{% if loop.index == 2 %}{% break %}{% endif %},{% endfor %}",
        {"items": ["a", "b", "c", "d"]},
    )
    assert result == "1,2", f"Got: {result!r}"


@test("break: empty iterable (no-op)")
def test_break_empty():
    result = render(
        "{% for i in items %}{% break %}{% endfor %}DONE",
        {"items": []},
    )
    assert result == "DONE", f"Got: {result!r}"


@test("break: with text after loop")
def test_break_after_text():
    result = render(
        "START{% for i in items %}{% if i == 2 %}{% break %}{% endif %}{{ i }}{% endfor %}END",
        {"items": [1, 2, 3]},
    )
    assert result == "START1END", f"Got: {result!r}"


# ═══════════════════════════════════════════════════════════════════════════
# CONTINUE TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("continue: skip iteration")
def test_continue_basic():
    result = render(
        "{% for i in items %}{% if i == 2 %}{% continue %}{% endif %}{{ i }},{% endfor %}",
        {"items": [1, 2, 3]},
    )
    assert result == "1,3,", f"Got: {result!r}"


@test("continue: skip multiple iterations")
def test_continue_multiple():
    result = render(
        "{% for i in items %}{% if i == 2 or i == 4 %}{% continue %}{% endif %}{{ i }},{% endfor %}",
        {"items": [1, 2, 3, 4, 5]},
    )
    assert result == "1,3,5,", f"Got: {result!r}"


@test("continue: skip first iteration")
def test_continue_first():
    result = render(
        "{% for i in items %}{% if loop.first %}{% continue %}{% endif %}{{ i }},{% endfor %}",
        {"items": [1, 2, 3]},
    )
    assert result == "2,3,", f"Got: {result!r}"


@test("continue: skip all iterations outputs nothing")
def test_continue_all():
    result = render(
        "{% for i in items %}{% continue %}{{ i }}{% endfor %}DONE",
        {"items": [1, 2, 3]},
    )
    assert result == "DONE", f"Got: {result!r}"


@test("continue: loop.index still increments")
def test_continue_index():
    result = render(
        "{% for i in items %}{% if i == 'b' %}{% continue %}{% endif %}{{ loop.index }},{% endfor %}",
        {"items": ["a", "b", "c"]},
    )
    # loop.index for 'a'=1, 'b' is skipped, 'c'=3
    assert result == "1,3,", f"Got: {result!r}"


# ═══════════════════════════════════════════════════════════════════════════
# NESTED LOOP TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("break: nested loops — break inner only")
def test_break_nested():
    result = render(
        "{% for i in outer %}[{% for j in inner %}{% if j == 2 %}{% break %}{% endif %}{{ j }}{% endfor %}]{% endfor %}",
        {"outer": [1, 2], "inner": [1, 2, 3]},
    )
    # Inner loop breaks at j==2, so outputs [1] twice
    assert result == "[1][1]", f"Got: {result!r}"


@test("continue: nested loops — continue inner only")
def test_continue_nested():
    result = render(
        "{% for i in outer %}[{% for j in inner %}{% if j == 2 %}{% continue %}{% endif %}{{ j }}{% endfor %}]{% endfor %}",
        {"outer": ["a", "b"], "inner": [1, 2, 3]},
    )
    # Inner loop skips j==2, outputs [13] twice
    assert result == "[13][13]", f"Got: {result!r}"


@test("break inner, outer continues")
def test_break_inner_outer_continues():
    result = render(
        "{% for i in outer %}{{ i }}:{% for j in inner %}{% if j > i %}{% break %}{% endif %}{{ j }}{% endfor %};{% endfor %}",
        {"outer": [1, 2, 3], "inner": [1, 2, 3]},
    )
    # i=1: j=1 ok, j=2>1 break → "1:1;"
    # i=2: j=1 ok, j=2 ok, j=3>2 break → "2:12;"
    # i=3: j=1,2,3 all ok → "3:123;"
    assert result == "1:1;2:12;3:123;", f"Got: {result!r}"


# ═══════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════


@test("break: with else/empty — break doesn't trigger empty")
def test_break_with_empty():
    result = render(
        "{% for i in items %}{% if i == 2 %}{% break %}{% endif %}{{ i }}{% empty %}EMPTY{% endfor %}",
        {"items": [1, 2, 3]},
    )
    assert result == "1", f"Got: {result!r}"


@test("break: single element list")
def test_break_single():
    result = render(
        "{% for i in items %}{% break %}X{% endfor %}DONE",
        {"items": [42]},
    )
    assert result == "DONE", f"Got: {result!r}"


@test("continue: with string iteration")
def test_continue_strings():
    result = render(
        "{% for c in chars %}{% if c == 'b' %}{% continue %}{% endif %}{{ c }}{% endfor %}",
        {"chars": ["a", "b", "c", "d"]},
    )
    assert result == "acd", f"Got: {result!r}"


@test("break and continue in same loop")
def test_break_and_continue():
    result = render(
        "{% for i in items %}{% if i == 3 %}{% continue %}{% endif %}{% if i == 5 %}{% break %}{% endif %}{{ i }},{% endfor %}",
        {"items": [1, 2, 3, 4, 5, 6]},
    )
    # skip 3, break at 5
    assert result == "1,2,4,", f"Got: {result!r}"


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


def main():
    if False:
        print("Native extension not built — skipping tests")
        return True

    tests = [
        obj
        for name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print("\n═══ Template {% break %} / {% continue %} Tests ═══")
    for t in tests:
        t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
