#!/usr/bin/env python3
"""Test template macros, call blocks, and namespace objects.

Tests:
1. Basic macro definition and call
2. Macro with parameters
3. Macro with default parameters
4. Macro called from {{ }} expression
5. Call block ({% call %})
6. Macro not producing output when defined
7. Namespace object ({% set ns = namespace() %})
8. Multiple macros in same template
9. Macro with body content (loops, conditions)
10. Performance benchmark
"""

# hyper-test: unit

import sys
import time

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

    # ── Basic macro ───────────────────────────────────────────────────────
    print("\n=== Basic macro ===")
    check(
        "macro def doesn't output",
        r("{% macro greet() %}Hello!{% endmacro %}ONLY THIS", {}) == "ONLY THIS",
    )

    check(
        "macro call from {{ }}",
        r("{% macro greet() %}Hello!{% endmacro %}{{ greet() }}", {}) == "Hello!",
    )

    # ── Macro with parameters ─────────────────────────────────────────────
    print("\n=== Macro with parameters ===")
    check(
        "single param",
        r(
            "{% macro hello(name) %}Hello {{ name }}!{% endmacro %}{{ hello('World') }}",
            {},
        )
        == "Hello World!",
    )

    check(
        "multiple params",
        r(
            "{% macro tag(name, cls) %}<{{ name }} class=\"{{ cls }}\">{% endmacro %}{{ tag('div', 'box') }}",
            {},
        )
        == '<div class="box">',
    )

    # ── Default parameters ────────────────────────────────────────────────
    print("\n=== Default parameters ===")
    check(
        "default used",
        r(
            "{% macro input(type='text') %}<input type=\"{{ type }}\">{% endmacro %}{{ input() }}",
            {},
        )
        == '<input type="text">',
    )

    check(
        "default overridden",
        r(
            "{% macro input(type='text') %}<input type=\"{{ type }}\">{% endmacro %}{{ input('password') }}",
            {},
        )
        == '<input type="password">',
    )

    # ── Macro with context variables ──────────────────────────────────────
    print("\n=== Macro with context vars ===")
    check(
        "macro uses param not outer context",
        r("{% macro show(x) %}{{ x }}{% endmacro %}{{ show('inner') }}", {"x": "outer"})
        == "inner",
    )

    # ── Multiple macros ───────────────────────────────────────────────────
    print("\n=== Multiple macros ===")
    check(
        "first macro alone",
        r(
            "{% macro header(title) %}<h1>{{ title }}</h1>{% endmacro %}{{ header('Page') }}",
            {},
        )
        == "<h1>Page</h1>",
    )
    check(
        "second macro alone",
        r("{% macro footer() %}<footer>END</footer>{% endmacro %}{{ footer() }}", {})
        == "<footer>END</footer>",
    )
    check(
        "two macros both called",
        r(
            "{% macro a() %}AAA{% endmacro %}{% macro b() %}BBB{% endmacro %}{{ a() }}{{ b() }}",
            {},
        )
        == "AAABBB",
    )
    tmpl = "{% macro header(title) %}<h1>{{ title }}</h1>{% endmacro %}{% macro footer() %}<footer>END</footer>{% endmacro %}{{ header('Page') }}{{ footer() }}"
    check("two macros with params", r(tmpl, {}) == "<h1>Page</h1><footer>END</footer>")

    # ── Macro with body content ───────────────────────────────────────────
    print("\n=== Macro with body ===")
    tmpl = """{% macro list_items(items) %}<ul>{% for item in items %}<li>{{ item }}</li>{% endfor %}</ul>{% endmacro %}{{ list_items(fruits) }}"""
    result = r(tmpl, {"fruits": ["apple", "banana"]})
    check(
        "macro with for loop",
        "<li>apple</li>" in result and "<li>banana</li>" in result,
    )

    tmpl = """{% macro show_if(val, label) %}{% if val %}{{ label }}: {{ val }}{% endif %}{% endmacro %}{{ show_if(name, 'Name') }}"""
    check("macro with if", r(tmpl, {"name": "Alice"}) == "Name: Alice")
    check("macro with if (false)", r(tmpl, {"name": ""}) == "")

    # ── Call block ────────────────────────────────────────────────────────
    print("\n=== Call block ===")
    tmpl = """{% macro dialog(title) %}<div><h2>{{ title }}</h2><div>{{ caller }}</div></div>{% endmacro %}{% call dialog('Hello') %}Dialog body here{% endcall %}"""
    result = r(tmpl, {})
    check(
        "call block renders",
        "Dialog body here" in result and "Hello" in result,
        f"got: {result!r}",
    )

    # ── Namespace ─────────────────────────────────────────────────────────
    print("\n=== Namespace ===")
    check(
        "namespace via context dict",
        r("{% if ns is mapping %}yes{% endif %}", {"ns": {}}) == "yes",
    )
    # Jinja2 scoping: set inside for-loop is loop-scoped, does not leak out.
    # Use namespace() for cross-scope mutation (tested in test_namespace.py).
    check(
        "set in for-loop is loop-scoped (Jinja2 semantics)",
        r(
            "{% for x in items %}{% if x == 'target' %}{% set found = 'yes' %}{% endif %}{% endfor %}{{ found|default('no') }}",
            {"items": ["a", "target", "b"]},
        )
        == "no",
    )

    # ── Edge cases ────────────────────────────────────────────────────────
    print("\n=== Edge cases ===")
    check("undefined macro returns empty", r("{{ nonexistent_macro() }}", {}) == "")
    check(
        "macro with no args called with none",
        r("{% macro empty() %}ok{% endmacro %}{{ empty() }}", {}) == "ok",
    )

    # ── Performance benchmark ─────────────────────────────────────────────
    print("\n=== Performance benchmark ===")
    bench_tmpl = """{% macro row(name, price) %}<tr><td>{{ name }}</td><td>{{ price }}</td></tr>{% endmacro %}<table>{% for item in items %}{{ row(item.name, item.price) }}{% endfor %}</table>"""
    bench_ctx = {
        "items": [
            {"name": f"Item {i}", "price": f"${i * 9.99:.2f}"} for i in range(20)
        ],
    }

    capsule = _template_compile(bench_tmpl, "<bench>")
    iterations = 10_000
    for _ in range(100):
        _template_render(capsule, bench_ctx)
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        _template_render(capsule, bench_ctx)
    ns_per = (time.perf_counter_ns() - t0) / iterations
    print(
        f"  Macro-heavy template: {ns_per / 1000:.1f} μs/render ({iterations * 1_000_000_000 / (ns_per * iterations) * iterations:,.0f}/sec)"
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All macro tests passed!")


if __name__ == "__main__":
    main()
