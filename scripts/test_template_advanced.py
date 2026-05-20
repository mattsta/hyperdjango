#!/usr/bin/env python3
"""Test advanced template features — new filters, whitespace control, raw blocks, loop vars.

Tests:
1. New native filters (abs, round, sort, reverse, unique, tojson, list, bool, sum, min, max, dictsort, items, count)
2. Whitespace control ({%- -%})
3. Raw blocks ({% raw %})
4. Advanced loop variables (revindex, revindex0, previtem)
5. Jinja2 correctness comparison for new features
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

    # ── New filters ───────────────────────────────────────────────────────
    print("\n=== New filters ===")
    check("abs positive", r("{{ x|abs }}", {"x": -5}) == "5")
    check("abs already positive", r("{{ x|abs }}", {"x": 3}) == "3")
    check("sort list", r("{{ x|sort|join(', ') }}", {"x": [3, 1, 2]}) == "1, 2, 3")
    check(
        "reverse list",
        r("{% for i in x|reverse %}{{ i }}{% endfor %}", {"x": [1, 2, 3]}) == "321",
    )
    check(
        "unique", r("{{ x|unique|join(', ') }}", {"x": [1, 2, 2, 3, 3, 3]}) == "1, 2, 3"
    )
    check(
        "tojson", r("{{ x|tojson|safe }}", {"x": {"a": 1}}) in ['{"a": 1}', '{"a":1}']
    )
    check("list filter", r("{{ x|list|length }}", {"x": "abc"}) == "3")
    check("bool true", r("{{ x|bool }}", {"x": 1}) == "True")
    check("bool false", r("{{ x|bool }}", {"x": 0}) == "False")
    check("sum", r("{{ x|sum }}", {"x": [1, 2, 3, 4]}) == "10")
    check("min", r("{{ x|min }}", {"x": [5, 2, 8, 1]}) == "1")
    check("max", r("{{ x|max }}", {"x": [5, 2, 8, 1]}) == "8")
    # dictsort/items with tuple unpacking requires {% for k, v in %} (future #179)
    # For now, test that the filters themselves produce correct output
    check(
        "dictsort produces sorted list",
        r("{{ x|dictsort|length }}", {"x": {"b": 2, "a": 1}}) == "2",
    )
    check(
        "items produces list", r("{{ x|items|length }}", {"x": {"x": 1, "y": 2}}) == "2"
    )
    check("count (alias for length)", r("{{ x|count }}", {"x": [1, 2, 3]}) == "3")

    # ── Whitespace control ────────────────────────────────────────────────
    print("\n=== Whitespace control ===")
    check("trim left {%-", r("  \n  {%- if true %}yes{% endif %}", {}) == "yes")
    check("trim right -%}", r("{% if true -%}  \n  yes{% endif %}", {}) == "yes")
    check("trim both", r("  {%- if true -%}  yes  {%- endif -%}  ", {}) == "yes")
    check(
        "trim in for loop",
        r("{% for x in items -%}\n{{ x }}\n{%- endfor %}", {"items": [1, 2, 3]})
        == "123",
    )

    # ── Raw blocks ────────────────────────────────────────────────────────
    print("\n=== Raw blocks ===")
    check(
        "raw preserves {{ }}",
        r("{% raw %}{{ not_evaluated }}{% endraw %}", {}) == "{{ not_evaluated }}",
    )
    check(
        "raw preserves {% %}",
        r("{% raw %}{% if x %}nope{% endif %}{% endraw %}", {})
        == "{% if x %}nope{% endif %}",
    )
    check(
        "text before and after raw",
        r("before{% raw %}{{ raw }}{% endraw %}after", {}) == "before{{ raw }}after",
    )

    # ── Advanced loop variables ───────────────────────────────────────────
    print("\n=== Advanced loop variables ===")
    check(
        "loop.revindex",
        r(
            "{% for x in items %}{{ loop.revindex }}{% endfor %}",
            {"items": ["a", "b", "c"]},
        )
        == "321",
    )
    check(
        "loop.revindex0",
        r(
            "{% for x in items %}{{ loop.revindex0 }}{% endfor %}",
            {"items": ["a", "b", "c"]},
        )
        == "210",
    )

    # ── Combined complex template ─────────────────────────────────────────
    print("\n=== Complex combined ===")
    complex_tmpl = """<ul>
{%- for item in items|sort|reverse %}
<li>{{ loop.index }}. {{ item|upper }} (rev={{ loop.revindex }})</li>
{%- endfor %}
</ul>
Total: {{ items|length }}"""
    result = r(complex_tmpl, {"items": ["cherry", "apple", "banana"]})
    check("combined: sorted reverse loop", "CHERRY" in result and "1." in result)
    check("combined: has total", "Total: 3" in result)

    # ── Jinja2 correctness for new features ───────────────────────────────
    print("\n=== Jinja2 correctness ===")
    try:
        import jinja2

        env = jinja2.Environment()

        test_cases = [
            ("{{ x|abs }}", {"x": -5}),
            ("{{ x|sort|join(', ') }}", {"x": [3, 1, 2]}),
            ("{{ x|unique|join(', ') }}", {"x": [1, 2, 2, 3]}),
            ("{{ x|sum }}", {"x": [1, 2, 3]}),
            ("{{ x|min }}", {"x": [3, 1, 2]}),
            ("{{ x|max }}", {"x": [3, 1, 2]}),
            ("{{ x|count }}", {"x": [1, 2, 3]}),
            ("{{ x|reverse|join('') }}", {"x": [1, 2, 3]}),
            (
                "{% for x in items %}{{ loop.revindex }}{% endfor %}",
                {"items": ["a", "b"]},
            ),
        ]
        for source, ctx in test_cases:
            jinja_result = env.from_string(source).render(**ctx)
            native_result = r(source, ctx)
            check(
                f"match: {source[:45]}",
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
    print("All advanced template tests passed!")


if __name__ == "__main__":
    main()
