#!/usr/bin/env python3
"""Test subscript access and method calls in templates.

Tests:
1. Integer subscript: {{ items[0] }}
2. String subscript: {{ dict['key'] }}
3. Negative subscript: {{ items[-1] }}
4. Method call no args: {{ text.upper() }}
5. Chained access: {{ data.items[0].name }}
6. Subscript + filter: {{ items[0]|upper }}
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

    # ── Subscript access ──────────────────────────────────────────────────
    print("\n=== Subscript access ===")
    check("int index [0]", r("{{ items[0] }}", {"items": ["a", "b", "c"]}) == "a")
    check("int index [1]", r("{{ items[1] }}", {"items": ["a", "b", "c"]}) == "b")
    check("int index [-1]", r("{{ items[-1] }}", {"items": ["a", "b", "c"]}) == "c")
    check("string key", r("{{ data['name'] }}", {"data": {"name": "Alice"}}) == "Alice")
    check(
        "nested subscript", r("{{ matrix[0][1] }}", {"matrix": [[1, 2], [3, 4]]}) == "2"
    )

    # ── Method calls ──────────────────────────────────────────────────────
    print("\n=== Method calls ===")
    check("upper()", r("{{ text.upper() }}", {"text": "hello"}) == "HELLO")
    check("lower()", r("{{ text.lower() }}", {"text": "HELLO"}) == "hello")
    check("strip()", r("{{ text.strip() }}", {"text": "  hi  "}) == "hi")
    check("title()", r("{{ text.title() }}", {"text": "hello world"}) == "Hello World")

    # ── Mixed access patterns ─────────────────────────────────────────────
    print("\n=== Mixed patterns ===")
    check(
        "dot + subscript",
        r("{{ users[0].name }}", {"users": [{"name": "Alice"}]}) == "Alice",
    )
    check(
        "subscript on attr", r("{{ data.items[0] }}", {"data": {"items": [42]}}) == "42"
    )

    # ── With filters ──────────────────────────────────────────────────────
    print("\n=== With filters ===")
    check(
        "subscript + filter", r("{{ items[0]|upper }}", {"items": ["hello"]}) == "HELLO"
    )
    check("method + filter", r("{{ text.upper()|length }}", {"text": "hello"}) == "5")

    # ── Edge cases ────────────────────────────────────────────────────────
    print("\n=== Edge cases ===")
    check("out of bounds returns empty", r("{{ items[99] }}", {"items": [1, 2]}) == "")
    check("missing key returns empty", r("{{ data['missing'] }}", {"data": {}}) == "")

    # ── Jinja2 correctness ────────────────────────────────────────────────
    print("\n=== Jinja2 correctness ===")
    try:
        import jinja2

        env = jinja2.Environment()
        test_cases = [
            ("{{ items[0] }}", {"items": ["a", "b"]}),
            ("{{ items[-1] }}", {"items": ["a", "b"]}),
            ("{{ text.upper() }}", {"text": "hello"}),
            ("{{ text.strip() }}", {"text": "  hi  "}),
            ("{{ users[0].name }}", {"users": [{"name": "Alice"}]}),
        ]
        for source, ctx in test_cases:
            jinja_result = env.from_string(source).render(**ctx)
            native_result = r(source, ctx)
            check(
                f"match: {source[:40]}",
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
    print("All access pattern tests passed!")


if __name__ == "__main__":
    main()
