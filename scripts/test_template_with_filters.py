#!/usr/bin/env python3
"""Test {% with %} blocks, expression-based {% set %}, and new filters.

Tests:
1. {% with %} scoped variable blocks
2. {% set x = expr %} with math expressions
3. indent filter
4. center filter
5. wordwrap filter
6. filesizeformat filter
7. Jinja2 correctness comparison
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

    # ── {% with %} blocks ─────────────────────────────────────────────────
    print("\n=== {% with %} scoped variable blocks ===")

    result = r("{% with x=42 %}{{ x }}{% endwith %}", {})
    check("with simple int", result == "42", f"got '{result}'")

    result = r("{% with name='Alice' %}Hello {{ name }}!{% endwith %}", {})
    check("with string literal", result == "Hello Alice!", f"got '{result}'")

    result = r("{% with x=1 %}{{ x }}{% endwith %}{{ x }}", {"x": 99})
    check("with scoping (outer restored)", result == "199", f"got '{result}'")

    result = r("{% with x=a+b %}{{ x }}{% endwith %}", {"a": 3, "b": 4})
    check("with math expression", result == "7", f"got '{result}'")

    result = r("{% with x=10, y=20 %}{{ x }}+{{ y }}={{ x+y }}{% endwith %}", {})
    check("with multiple bindings", result == "10+20=30", f"got '{result}'")

    result = r(
        "{% with greeting='Hello' %}{% with name='World' %}{{ greeting }} {{ name }}{% endwith %}{% endwith %}",
        {},
    )
    check("nested with blocks", result == "Hello World", f"got '{result}'")

    # with + for loop using context variable
    result = r(
        "{% with prefix='item' %}{% for i in items %}{{ prefix }}{{ i }} {% endfor %}{% endwith %}",
        {"items": [1, 2, 3]},
    )
    check("with + for loop", result == "item1 item2 item3 ", f"got '{result}'")

    # ── List, tuple, dict literals ────────────────────────────────────────
    print("\n=== List, tuple, dict literals ===")
    result = r(
        "{% with items=[1, 2, 3] %}{% for i in items %}{{ i }}{% endfor %}{% endwith %}",
        {},
    )
    check("list literal in with", result == "123", f"got '{result}'")

    result = r("{% for x in [10, 20, 30] %}{{ x }} {% endfor %}", {})
    check("list literal in for", result == "10 20 30 ", f"got '{result}'")

    result = r("{{ [1, 2, 3]|length }}", {})
    check("list literal with filter", result == "3", f"got '{result}'")

    result = r("{{ ['a', 'b', 'c']|join(', ') }}", {})
    check("list literal join", result == "a, b, c", f"got '{result}'")

    # Dict literal as standalone expression
    result = r("{{ {'x': 1, 'y': 2}|length }}", {})
    check("dict literal length", result == "2", f"got '{result}'")

    # Tuple literal
    result = r("{% for x in (10, 20, 30) %}{{ x }} {% endfor %}", {})
    check("tuple literal in for", result == "10 20 30 ", f"got '{result}'")

    # ── {% set %} with expressions ────────────────────────────────────────
    print("\n=== {% set %} with math expressions ===")

    result = r("{% set x = 2 + 3 %}{{ x }}", {})
    check("set with addition", result == "5", f"got '{result}'")

    result = r("{% set x = a * b %}{{ x }}", {"a": 6, "b": 7})
    check("set with multiplication", result == "42", f"got '{result}'")

    result = r(
        "{% set total = price * qty %}Total: {{ total }}", {"price": 10, "qty": 5}
    )
    check("set computed total", result == "Total: 50", f"got '{result}'")

    result = r('{% set msg = "Hello" ~ " " ~ name %}{{ msg }}', {"name": "World"})
    check("set with string concat", result == "Hello World", f"got '{result}'")

    result = r("{% set x = (a + b) * 2 %}{{ x }}", {"a": 3, "b": 4})
    check("set with grouped expr", result == "14", f"got '{result}'")

    # ── indent filter ─────────────────────────────────────────────────────
    print("\n=== indent filter ===")

    result = r("{{ text|indent }}", {"text": "line1\nline2\nline3"})
    check(
        "indent default (4 spaces)",
        "    line1" in result and "    line2" in result,
        f"got '{result}'",
    )

    result = r("{{ text|indent(2) }}", {"text": "a\nb"})
    check("indent 2 spaces", result == "  a\n  b", f"got '{result}'")

    result = r("{{ text|indent(8) }}", {"text": "x"})
    check("indent 8 spaces", result == "        x", f"got '{result}'")

    # ── center filter ─────────────────────────────────────────────────────
    print("\n=== center filter ===")

    result = r("{{ text|center(10) }}", {"text": "hi"})
    check(
        "center 10",
        len(result) == 10 and "hi" in result,
        f"got '{result}' len={len(result)}",
    )

    result = r("{{ text|center(5) }}", {"text": "abc"})
    check("center 5 with 3 chars", result == " abc ", f"got '{result}'")

    # ── wordwrap filter ───────────────────────────────────────────────────
    print("\n=== wordwrap filter ===")

    long_text = "word " * 20  # 100 chars
    result = r("{{ text|wordwrap(40) }}", {"text": long_text.strip()})
    check("wordwrap 40", "\n" in result, f"got '{result[:60]}...'")

    # ── filesizeformat filter ─────────────────────────────────────────────
    print("\n=== filesizeformat filter ===")

    result = r("{{ size|filesizeformat }}", {"size": 100})
    check("bytes", "100" in result and "Bytes" in result, f"got '{result}'")

    result = r("{{ size|filesizeformat }}", {"size": 2048})
    check("kilobytes", "kB" in result, f"got '{result}'")

    result = r("{{ size|filesizeformat }}", {"size": 5 * 1024 * 1024})
    check("megabytes", "MB" in result, f"got '{result}'")

    result = r("{{ size|filesizeformat }}", {"size": 3 * 1024 * 1024 * 1024})
    check("gigabytes", "GB" in result, f"got '{result}'")

    # ── Jinja2 correctness comparison ─────────────────────────────────────
    print("\n=== Jinja2 correctness comparison ===")
    try:
        from jinja2 import Environment

        env = Environment()

        test_cases = [
            ("{% with x=42 %}{{ x }}{% endwith %}", {}),
            ("{% set x = 2 + 3 %}{{ x }}", {}),
            ("{% set x = 6 * 7 %}{{ x }}", {}),
            ("{{ text|indent(2) }}", {"text": "a\nb"}),
            ("{{ text|center(10) }}", {"text": "hi"}),
            ("{{ size|filesizeformat }}", {"size": 2048}),
        ]

        for template_str, ctx in test_cases:
            j2_result = env.from_string(template_str).render(ctx)
            zig_result = r(template_str, ctx)
            match = j2_result.strip() == zig_result.strip()
            check(
                f"match: {template_str[:50]}",
                match,
                f"jinja2='{j2_result}' zig='{zig_result}'",
            )

    except ImportError:
        print("  SKIP: Jinja2 not available for comparison")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All with/set/filter tests passed!")
    else:
        print("SOME TESTS FAILED!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
