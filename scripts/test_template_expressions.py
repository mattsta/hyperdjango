#!/usr/bin/env python3
"""Test template expression system — and/or/not, comparisons, is tests, in operator.

Tests:
1. Logical operators (and, or, not)
2. Compound conditions (and + or + not combined)
3. "is" tests (defined, none, string, number, odd, even, etc.)
4. "in" containment operator
5. "is not" negated tests
6. "not in" negated containment
7. Complex mixed expressions
8. Jinja2 correctness comparison
"""

# hyper-test: unit

import sys

from hyperdjango._hyperdjango_native import _template_compile, _template_render


def r(source, context):
    capsule = _template_compile(source, "<test>")
    result = _template_render(capsule, context)
    return result.decode("utf-8") if isinstance(result, bytes) else result


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Logical operators ─────────────────────────────────────────────────
    print("\n=== Logical operators ===")
    check(
        "and both true",
        r("{% if a and b %}yes{% endif %}", {"a": True, "b": True}) == "yes",
    )
    check(
        "and left false",
        r("{% if a and b %}yes{% else %}no{% endif %}", {"a": False, "b": True})
        == "no",
    )
    check(
        "and right false",
        r("{% if a and b %}yes{% else %}no{% endif %}", {"a": True, "b": False})
        == "no",
    )
    check(
        "or both false",
        r("{% if a or b %}yes{% else %}no{% endif %}", {"a": False, "b": False})
        == "no",
    )
    check(
        "or left true",
        r("{% if a or b %}yes{% endif %}", {"a": True, "b": False}) == "yes",
    )
    check(
        "or right true",
        r("{% if a or b %}yes{% endif %}", {"a": False, "b": True}) == "yes",
    )
    check("not true", r("{% if not a %}yes{% endif %}", {"a": False}) == "yes")
    check(
        "not false", r("{% if not a %}yes{% else %}no{% endif %}", {"a": True}) == "no"
    )

    # ── Compound conditions ───────────────────────────────────────────────
    print("\n=== Compound conditions ===")
    check(
        "a and b or c (a=F,b=T,c=T)",
        r(
            "{% if a and b or c %}yes{% else %}no{% endif %}",
            {"a": False, "b": True, "c": True},
        )
        == "yes",
    )
    check(
        "not a and b",
        r("{% if not a and b %}yes{% endif %}", {"a": False, "b": True}) == "yes",
    )
    check(
        "a or b and c (precedence: and binds tighter)",
        r(
            "{% if a or b and c %}yes{% else %}no{% endif %}",
            {"a": False, "b": True, "c": False},
        )
        == "no",
    )

    # ── Comparisons with logical ops ──────────────────────────────────────
    print("\n=== Comparisons with logical ops ===")
    check(
        "x == 1 and y == 2",
        r("{% if x == '1' and y == '2' %}yes{% endif %}", {"x": "1", "y": "2"})
        == "yes",
    )
    check(
        "x == 1 or y == 2 (first true)",
        r("{% if x == '1' or y == '3' %}yes{% endif %}", {"x": "1", "y": "2"}) == "yes",
    )

    # ── "is" tests ────────────────────────────────────────────────────────
    print("\n=== 'is' tests ===")
    check(
        "is defined (yes)", r("{% if x is defined %}yes{% endif %}", {"x": 42}) == "yes"
    )
    check(
        "is defined (no)",
        r("{% if x is defined %}yes{% else %}no{% endif %}", {}) == "no",
    )
    check("is undefined", r("{% if x is undefined %}yes{% endif %}", {}) == "yes")
    check("is none", r("{% if x is none %}yes{% endif %}", {"x": None}) == "yes")
    check("is not none", r("{% if x is not none %}yes{% endif %}", {"x": 42}) == "yes")
    check("is true", r("{% if x is true %}yes{% endif %}", {"x": True}) == "yes")
    check("is false", r("{% if x is false %}yes{% endif %}", {"x": False}) == "yes")
    check("is string", r("{% if x is string %}yes{% endif %}", {"x": "hello"}) == "yes")
    check(
        "is not string", r("{% if x is not string %}yes{% endif %}", {"x": 42}) == "yes"
    )
    check("is number", r("{% if x is number %}yes{% endif %}", {"x": 42}) == "yes")
    check("is float", r("{% if x is float %}yes{% endif %}", {"x": 3.14}) == "yes")
    check("is boolean", r("{% if x is boolean %}yes{% endif %}", {"x": True}) == "yes")
    check("is callable", r("{% if x is callable %}yes{% endif %}", {"x": len}) == "yes")
    check(
        "is iterable", r("{% if x is iterable %}yes{% endif %}", {"x": [1, 2]}) == "yes"
    )
    check(
        "is mapping", r("{% if x is mapping %}yes{% endif %}", {"x": {"a": 1}}) == "yes"
    )
    check(
        "is sequence", r("{% if x is sequence %}yes{% endif %}", {"x": [1, 2]}) == "yes"
    )
    check("is odd", r("{% if x is odd %}yes{% endif %}", {"x": 3}) == "yes")
    check("is even", r("{% if x is even %}yes{% endif %}", {"x": 4}) == "yes")
    check("is upper", r("{% if x is upper %}yes{% endif %}", {"x": "HELLO"}) == "yes")
    check("is lower", r("{% if x is lower %}yes{% endif %}", {"x": "hello"}) == "yes")

    # ── "in" operator ─────────────────────────────────────────────────────
    print("\n=== 'in' operator ===")
    check(
        "in list",
        r("{% if x in items %}yes{% endif %}", {"x": 2, "items": [1, 2, 3]}) == "yes",
    )
    check(
        "not in list",
        r("{% if x in items %}yes{% else %}no{% endif %}", {"x": 5, "items": [1, 2, 3]})
        == "no",
    )
    check(
        "in string",
        r("{% if x in text %}yes{% endif %}", {"x": "ell", "text": "hello"}) == "yes",
    )
    check(
        "not in (keyword)",
        r("{% if x not in items %}yes{% endif %}", {"x": 5, "items": [1, 2, 3]})
        == "yes",
    )

    # ── Complex mixed ─────────────────────────────────────────────────────
    print("\n=== Complex mixed ===")
    check(
        "defined and truthy",
        r("{% if x is defined and x %}{{ x }}{% else %}nope{% endif %}", {"x": "hello"})
        == "hello",
    )
    check(
        "undefined or default",
        r("{% if x is defined %}{{ x }}{% else %}default{% endif %}", {}) == "default",
    )
    check(
        "comparison and test",
        r(
            "{% if x == 'admin' and role is defined %}admin{% endif %}",
            {"x": "admin", "role": "admin"},
        )
        == "admin",
    )

    # ── Jinja2 correctness ────────────────────────────────────────────────
    print("\n=== Jinja2 correctness ===")
    try:
        import jinja2

        env = jinja2.Environment()

        test_cases = [
            ("{% if a and b %}yes{% else %}no{% endif %}", {"a": True, "b": True}),
            ("{% if a and b %}yes{% else %}no{% endif %}", {"a": False, "b": True}),
            ("{% if a or b %}yes{% else %}no{% endif %}", {"a": False, "b": True}),
            ("{% if not a %}yes{% else %}no{% endif %}", {"a": False}),
            ("{% if x is defined %}yes{% else %}no{% endif %}", {"x": 1}),
            ("{% if x is none %}yes{% else %}no{% endif %}", {"x": None}),
            ("{% if x is odd %}yes{% else %}no{% endif %}", {"x": 3}),
            ("{% if x is even %}yes{% else %}no{% endif %}", {"x": 4}),
        ]
        for source, ctx in test_cases:
            jinja_result = env.from_string(source).render(**ctx)
            native_result = r(source, ctx)
            check(
                f"match: {source[:50]}",
                native_result == jinja_result,
                f"native={native_result!r} jinja={jinja_result!r}",
            )
    except ImportError:
        print("  SKIP: Jinja2 not installed")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All expression tests passed!")


if __name__ == "__main__":
    main()
