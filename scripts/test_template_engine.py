#!/usr/bin/env python3
"""Comprehensive test suite for native Zig template engine.

Tests:
1. Text rendering (static content)
2. Variable substitution (simple, dotted, nested)
3. Auto-escaping (HTML entities, safe filter)
4. Filters (native: lower, upper, title, length, default, join, first, last, trim, int, wordcount)
5. If/elif/else conditionals (truthiness, comparison, negation)
6. For loops (simple, loop vars, empty clause, nested)
7. Set variable
8. Comments (stripped)
9. Block/endblock (basic)
10. Mixed complex templates
11. Edge cases (missing vars, empty context, unicode, large templates)
12. Custom Python filters
13. Correctness vs Jinja2 (byte-identical output)
14. Performance benchmark vs Jinja2
"""

# hyper-test: unit

import os
import sys
import time

from hyperdjango._hyperdjango_native import (
    _template_compile,
    _template_register_filter,
    _template_render,
)


def compile_and_render(source, context):
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

    # ── Test 1: Static text ───────────────────────────────────────────────
    print("\n=== Test 1: Static text ===")
    check("plain text", compile_and_render("Hello World!", {}) == "Hello World!")
    check(
        "HTML preserved", compile_and_render("<h1>Title</h1>", {}) == "<h1>Title</h1>"
    )
    check("empty template", compile_and_render("", {}) == "")
    check(
        "multiline",
        compile_and_render("line1\nline2\nline3", {}) == "line1\nline2\nline3",
    )

    # ── Test 2: Variable substitution ─────────────────────────────────────
    print("\n=== Test 2: Variables ===")
    check(
        "simple var",
        compile_and_render("Hello {{ name }}!", {"name": "Alice"}) == "Hello Alice!",
    )
    check(
        "int var",
        compile_and_render("Count: {{ count }}", {"count": 42}) == "Count: 42",
    )
    check("float var", compile_and_render("Pi: {{ pi }}", {"pi": 3.14}) == "Pi: 3.14")
    check("bool True", compile_and_render("{{ flag }}", {"flag": True}) == "True")
    check("bool False", compile_and_render("{{ flag }}", {"flag": False}) == "False")
    check("None renders empty", compile_and_render("{{ x }}", {"x": None}) == "")
    check("missing var empty", compile_and_render("{{ missing }}", {}) == "")
    check(
        "dotted access",
        compile_and_render("{{ user.name }}", {"user": {"name": "Bob"}}) == "Bob",
    )
    check(
        "deep dotted",
        compile_and_render("{{ a.b.c }}", {"a": {"b": {"c": "deep"}}}) == "deep",
    )

    # ── Test 3: Auto-escaping ─────────────────────────────────────────────
    print("\n=== Test 3: Auto-escaping ===")
    check(
        "escapes <",
        compile_and_render("{{ x }}", {"x": "<script>"}) == "&lt;script&gt;",
    )
    check("escapes &", compile_and_render("{{ x }}", {"x": "a&b"}) == "a&amp;b")
    check('escapes "', compile_and_render("{{ x }}", {"x": 'a"b'}) == "a&quot;b")
    check("escapes '", compile_and_render("{{ x }}", {"x": "a'b"}) == "a&#x27;b")
    check(
        "safe filter",
        compile_and_render("{{ x|safe }}", {"x": "<b>bold</b>"}) == "<b>bold</b>",
    )
    check(
        "multiple escapes",
        compile_and_render("{{ x }}", {"x": '<a href="x">&</a>'})
        == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;",
    )

    # ── Test 4: Filters ──────────────────────────────────────────────────
    print("\n=== Test 4: Filters ===")
    check("lower", compile_and_render("{{ x|lower }}", {"x": "HELLO"}) == "hello")
    check("upper", compile_and_render("{{ x|upper }}", {"x": "hello"}) == "HELLO")
    check(
        "title",
        compile_and_render("{{ x|title }}", {"x": "hello world"}) == "Hello World",
    )
    check(
        "capitalize",
        compile_and_render("{{ x|capitalize }}", {"x": "hello"}) == "Hello",
    )
    check("trim", compile_and_render("{{ x|trim }}", {"x": "  hi  "}) == "hi")
    check("length", compile_and_render("{{ x|length }}", {"x": [1, 2, 3]}) == "3")
    check("length string", compile_and_render("{{ x|length }}", {"x": "hello"}) == "5")
    check(
        "default used",
        compile_and_render("{{ x|default('fallback') }}", {}) == "fallback",
    )
    check(
        "default not used",
        compile_and_render("{{ x|default('fallback') }}", {"x": "real"}) == "real",
    )
    check(
        "default empty string",
        compile_and_render("{{ x|default('fb') }}", {"x": ""}) == "fb",
    )
    check("first", compile_and_render("{{ x|first }}", {"x": [10, 20, 30]}) == "10")
    check("last", compile_and_render("{{ x|last }}", {"x": [10, 20, 30]}) == "30")
    check(
        "join",
        compile_and_render("{{ x|join(', ') }}", {"x": ["a", "b", "c"]}) == "a, b, c",
    )
    check("int filter", compile_and_render("{{ x|int }}", {"x": "42"}) == "42")
    check("string filter", compile_and_render("{{ x|string }}", {"x": 42}) == "42")
    check(
        "wordcount",
        compile_and_render("{{ x|wordcount }}", {"x": "hello beautiful world"}) == "3",
    )
    # Filter chain
    check(
        "chain lower+trim",
        compile_and_render("{{ x|trim|lower }}", {"x": "  HELLO  "}) == "hello",
    )

    # ── Test 5: Conditionals ──────────────────────────────────────────────
    print("\n=== Test 5: If/elif/else ===")
    check(
        "if true",
        compile_and_render("{% if show %}yes{% endif %}", {"show": True}) == "yes",
    )
    check(
        "if false",
        compile_and_render("{% if show %}yes{% endif %}", {"show": False}) == "",
    )
    check(
        "if else",
        compile_and_render("{% if show %}yes{% else %}no{% endif %}", {"show": False})
        == "no",
    )
    check(
        "if elif else",
        compile_and_render(
            "{% if x == 'a' %}A{% elif x == 'b' %}B{% else %}C{% endif %}", {"x": "b"}
        )
        == "B",
    )
    check(
        "if not",
        compile_and_render("{% if not hide %}visible{% endif %}", {"hide": False})
        == "visible",
    )
    check(
        "if truthy string",
        compile_and_render("{% if name %}{{ name }}{% endif %}", {"name": "Alice"})
        == "Alice",
    )
    check(
        "if falsy empty",
        compile_and_render(
            "{% if name %}{{ name }}{% else %}anon{% endif %}", {"name": ""}
        )
        == "anon",
    )
    check(
        "if comparison ==",
        compile_and_render(
            "{% if status == 'active' %}on{% else %}off{% endif %}",
            {"status": "active"},
        )
        == "on",
    )
    check(
        "if comparison !=",
        compile_and_render(
            "{% if status != 'active' %}off{% else %}on{% endif %}",
            {"status": "active"},
        )
        == "on",
    )

    # ── Test 6: For loops ─────────────────────────────────────────────────
    print("\n=== Test 6: For loops ===")
    check(
        "simple for",
        compile_and_render(
            "{% for x in items %}{{ x }} {% endfor %}", {"items": [1, 2, 3]}
        )
        == "1 2 3 ",
    )
    check(
        "for with text",
        compile_and_render(
            "{% for name in names %}Hello {{ name }}! {% endfor %}",
            {"names": ["Alice", "Bob"]},
        )
        == "Hello Alice! Hello Bob! ",
    )
    check(
        "for empty clause",
        compile_and_render(
            "{% for x in items %}{{ x }}{% empty %}none{% endfor %}", {"items": []}
        )
        == "none",
    )
    check(
        "for empty missing",
        compile_and_render(
            "{% for x in missing %}{{ x }}{% empty %}none{% endfor %}", {}
        )
        == "none",
    )
    check(
        "for loop.index",
        compile_and_render(
            "{% for x in items %}{{ loop.index }}{% endfor %}",
            {"items": ["a", "b", "c"]},
        )
        == "123",
    )
    check(
        "for loop.first/last",
        compile_and_render(
            "{% for x in items %}{% if loop.first %}[{% endif %}{{ x }}{% if loop.last %}]{% endif %}{% endfor %}",
            {"items": ["a", "b", "c"]},
        )
        == "[abc]",
    )
    check(
        "nested for",
        compile_and_render(
            "{% for row in rows %}{% for col in row %}{{ col }}{% endfor %} {% endfor %}",
            {"rows": [[1, 2], [3, 4]]},
        )
        == "12 34 ",
    )

    # ── Test 7: Set variable ──────────────────────────────────────────────
    print("\n=== Test 7: Set variable ===")
    check(
        "set and use",
        compile_and_render("{% set greeting = 'Hello' %}{{ greeting }} World!", {})
        == "Hello World!",
    )

    # ── Test 8: Comments ──────────────────────────────────────────────────
    print("\n=== Test 8: Comments ===")
    check(
        "comment stripped",
        compile_and_render("before{# this is hidden #}after", {}) == "beforeafter",
    )
    check(
        "multiline comment",
        compile_and_render("a{# multi\nline\ncomment #}b", {}) == "ab",
    )

    # ── Test 9: Block/endblock ────────────────────────────────────────────
    print("\n=== Test 9: Blocks ===")
    check(
        "basic block",
        compile_and_render("{% block content %}default{% endblock %}", {}) == "default",
    )

    # ── Test 10: Complex mixed template ───────────────────────────────────
    print("\n=== Test 10: Complex template ===")
    complex_template = """<html>
<head><title>{{ title|default('Untitled') }}</title></head>
<body>
<h1>{{ title }}</h1>
{% if items %}
<ul>
{% for item in items %}
<li>{{ loop.index }}. {{ item.name }} - {{ item.price }}</li>
{% endfor %}
</ul>
{% else %}
<p>No items found.</p>
{% endif %}
<footer>{{ footer|default('(c) 2026')|safe }}</footer>
</body>
</html>"""
    result = compile_and_render(
        complex_template,
        {
            "title": "Products",
            "items": [
                {"name": "Widget", "price": 9.99},
                {"name": "Gadget", "price": 19.99},
            ],
        },
    )
    check("contains title", "Products" in result)
    check("contains item 1", "Widget" in result)
    check("contains item 2", "Gadget" in result)
    check("contains loop index", "1." in result and "2." in result)
    check("contains footer default", "(c) 2026" in result)

    # ── Test 11: Edge cases ───────────────────────────────────────────────
    print("\n=== Test 11: Edge cases ===")
    check("unicode", compile_and_render("{{ x }}", {"x": "日本語 🎉"}) == "日本語 🎉")
    check("empty context", compile_and_render("static only", {}) == "static only")
    check(
        "special chars in text",
        compile_and_render("a < b && c > d", {}) == "a < b && c > d",
    )
    # Large template
    large = "{{ x }}" * 1000
    large_result = compile_and_render(large, {"x": "A"})
    check("large template (1000 vars)", large_result == "A" * 1000)

    # ── Test 12: Custom Python filters ────────────────────────────────────
    print("\n=== Test 12: Custom filters ===")
    capsule = _template_compile("{{ name|my_reverse }}", "<test>")
    _template_register_filter(
        capsule, "my_reverse", lambda s: s[::-1] if isinstance(s, str) else s
    )
    result = _template_render(capsule, {"name": "hello"})
    check("custom Python filter", result.decode("utf-8") == "olleh")

    # ── Test 13: Correctness vs Jinja2 ────────────────────────────────────
    print("\n=== Test 13: Correctness vs Jinja2 ===")
    try:
        import jinja2

        env = jinja2.Environment(autoescape=jinja2.select_autoescape(["html"]))

        test_cases = [
            ("Hello {{ name }}!", {"name": "World"}),
            ("{{ x|lower }}", {"x": "HELLO"}),
            ("{{ x|upper }}", {"x": "hello"}),
            ("{{ x|trim }}", {"x": "  hi  "}),
            ("{{ x|length }}", {"x": [1, 2, 3]}),
            ("{% if show %}yes{% else %}no{% endif %}", {"show": True}),
            ("{% if show %}yes{% else %}no{% endif %}", {"show": False}),
            ("{% for x in items %}{{ x }}{% endfor %}", {"items": [1, 2, 3]}),
        ]

        for source, ctx in test_cases:
            jinja_result = env.from_string(source).render(**ctx)
            native_result = compile_and_render(source, ctx)
            check(
                f"match Jinja2: {source[:40]}",
                native_result == jinja_result,
                f"native={native_result!r} jinja={jinja_result!r}",
            )
    except ImportError:
        print("  SKIP: Jinja2 not installed")

    # ── Test 14: Performance benchmark ────────────────────────────────────
    print("\n=== Test 14: Performance benchmark ===")

    bench_template = """<html>
<head><title>{{ title }}</title></head>
<body>
{% for item in items %}
<div class="item">
  <h2>{{ item.name }}</h2>
  <p>{{ item.description|default('No description') }}</p>
  <span>{{ item.price }}</span>
</div>
{% endfor %}
{% if footer %}
<footer>{{ footer }}</footer>
{% endif %}
</body>
</html>"""

    bench_context = {
        "title": "Products",
        "items": [
            {
                "name": f"Item {i}",
                "description": f"Description for item {i}",
                "price": i * 9.99,
            }
            for i in range(20)
        ],
        "footer": "Copyright 2026",
    }

    # Native benchmark
    capsule = _template_compile(bench_template, "<bench>")
    iterations = 10_000
    # Warmup
    for _ in range(100):
        _template_render(capsule, bench_context)
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        _template_render(capsule, bench_context)
    native_ns = (time.perf_counter_ns() - t0) / iterations

    print(
        f"  Native Zig:  {native_ns / 1000:.1f} μs/render ({iterations * 1_000_000_000 / (native_ns * iterations) * iterations:,.0f}/sec)"
    )

    # Jinja2 benchmark
    try:
        import jinja2

        env = jinja2.Environment(autoescape=jinja2.select_autoescape(["html"]))
        jinja_tmpl = env.from_string(bench_template)
        for _ in range(100):
            jinja_tmpl.render(**bench_context)
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            jinja_tmpl.render(**bench_context)
        jinja_ns = (time.perf_counter_ns() - t0) / iterations
        speedup = jinja_ns / native_ns
        print(f"  Jinja2:      {jinja_ns / 1000:.1f} μs/render")
        print(f"  Speedup:     {speedup:.1f}x faster")
        from hyperdjango.native import is_release_build

        # Skip the perf assertion under parallel test execution — CPU
        # contention between 290+ subprocesses makes comparative benchmarks
        # against an external library unreliable. The assertion is only
        # meaningful for single-threaded manual benchmark runs.
        _parallel = os.environ.get("HYPER_TEST_PARALLEL") == "1"
        if is_release_build and not _parallel:
            check("native faster than Jinja2", speedup > 1.0, f"speedup={speedup:.2f}x")
        elif _parallel:
            print(f"  (skipping perf assertion in parallel mode: {speedup:.2f}x)")
        else:
            print(f"  (skipping perf assertion in debug build: {speedup:.2f}x)")
    except ImportError:
        print("  SKIP: Jinja2 not installed for comparison")

    # ── Arbitrary-precision integers ──────────────────────────────────────
    # Python ints are unbounded; a C long is not. Every value >= 2**63 used
    # to raise OverflowError out of the renderer — snowflake ids, hashes and
    # 2**64 counters are ordinary template context, so this was a 500 on
    # real data, filter or not.
    print("\n=== Test: arbitrary-precision integers ===")
    for value in (2**62, 2**63, 2**64, 2**200, -(2**63) - 1, -(2**200)):
        check(
            f"renders {value.bit_length()}-bit int exactly",
            compile_and_render("{{ x }}", {"x": value}) == str(value),
            compile_and_render("{{ x }}", {"x": value}),
        )
    check(
        "big int survives a filter chain",
        compile_and_render("{{ x|string|upper }}", {"x": 2**70}) == str(2**70),
    )
    check(
        "big int inside a larger template",
        compile_and_render("id={{ x }};", {"x": 2**64}) == f"id={2**64};",
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All template engine tests passed!")


if __name__ == "__main__":
    main()
