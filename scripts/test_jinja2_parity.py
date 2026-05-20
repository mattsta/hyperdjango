"""Comprehensive Jinja2 parity test suite — exercises ALL template features.

Tests the full feature surface of the HyperDjango Zig template engine against
expected Jinja2-compatible behavior.
"""

# hyper-test: unit

import faulthandler

faulthandler.enable()

import sys
import tempfile
import time
from pathlib import Path

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []

_engine_cache: dict[tuple, TemplateEngine] = {}


def test(name: str, template: str, context: dict, expected: str, **kwargs) -> None:
    global passed, failed
    cache_key = tuple(sorted(kwargs.items()))
    if cache_key not in _engine_cache:
        _engine_cache[cache_key] = TemplateEngine(**kwargs)
    engine = _engine_cache[cache_key]
    try:
        result = engine.render_string(template, context)
        result_norm = " ".join(result.split())
        expected_norm = " ".join(expected.split())
        if result_norm == expected_norm:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name}")
            print(f"    Expected: {expected_norm!r}")
            print(f"    Got:      {result_norm!r}")
            failed += 1
            errors.append(name)
    except Exception as e:
        print(f"  ERROR: {name}: {e}")
        failed += 1
        errors.append(name)


def test_file(
    name: str,
    files: dict[str, str],
    render: str,
    context: dict,
    expected: str,
    **kwargs,
) -> None:
    global passed, failed
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname, src in files.items():
            (Path(tmpdir) / fname).write_text(src)
        engine = TemplateEngine(template_dir=tmpdir, **kwargs)
        try:
            result = engine.render(render, context)
            result_norm = " ".join(result.split())
            expected_norm = " ".join(expected.split())
            if result_norm == expected_norm:
                print(f"  PASS: {name}")
                passed += 1
            else:
                print(f"  FAIL: {name}")
                print(f"    Expected: {expected_norm!r}")
                print(f"    Got:      {result_norm!r}")
                failed += 1
                errors.append(name)
        except Exception as e:
            print(f"  ERROR: {name}: {e}")
            failed += 1
            errors.append(name)


print("=" * 70)
print("COMPREHENSIVE JINJA2 PARITY TEST SUITE")
print("=" * 70)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 1. Expressions ──")
# ═══════════════════════════════════════════════════════════════════════

test("math: add", "{{ 2 + 3 }}", {}, "5")
test("math: subtract", "{{ 10 - 4 }}", {}, "6")
test("math: multiply", "{{ 3 * 7 }}", {}, "21")
test("math: true division", "{{ 10 / 3 }}", {}, "3.3333333333333335")
test("math: true division exact", "{{ 10 / 2 }}", {}, "5")
test("math: floor division", "{{ 10 // 3 }}", {}, "3")
test("math: floor div", "{{ 7 // 2 }}", {}, "3")
test("math: modulo", "{{ 10 % 3 }}", {}, "1")
test("math: power", "{{ 2 ** 10 }}", {}, "1024")
test("math: unary neg", "{{ -x }}", {"x": 5}, "-5")
test("math: parenthesized", "{{ (2 + 3) * 4 }}", {}, "20")
test("math: complex", "{{ (a + b) * c - d }}", {"a": 2, "b": 3, "c": 4, "d": 1}, "19")
test("string concat ~", '{{ "Hello" ~ " " ~ name }}', {"name": "World"}, "Hello World")
test("ternary", '{{ "yes" if x else "no" }}', {"x": True}, "yes")
test("ternary false", '{{ "yes" if x else "no" }}', {"x": False}, "no")
test("comparison ==", "{% if x == 5 %}yes{% endif %}", {"x": 5}, "yes")
test("comparison !=", "{% if x != 5 %}yes{% endif %}", {"x": 3}, "yes")
test("comparison <", "{% if x < 5 %}yes{% endif %}", {"x": 3}, "yes")
test("comparison >", "{% if x > 5 %}yes{% endif %}", {"x": 7}, "yes")
test("in operator", "{% if 'a' in items %}yes{% endif %}", {"items": ["a", "b"]}, "yes")
test("not in", "{% if 'c' not in items %}yes{% endif %}", {"items": ["a", "b"]}, "yes")
test(
    "and/or",
    "{% if a and b %}yes{% elif a or c %}maybe{% endif %}",
    {"a": True, "b": False, "c": True},
    "maybe",
)
test("not", "{% if not x %}yes{% endif %}", {"x": False}, "yes")
test("float literal", "{{ 3.14 }}", {}, "3.14")
test("list literal", "{% for i in [1,2,3] %}{{ i }}{% endfor %}", {}, "123")
test("dict literal", "{% set d = {'a': 1} %}{{ d.a }}", {}, "1")

# ═══════════════════════════════════════════════════════════════════════
print("\n── 2. Is-Tests ──")
# ═══════════════════════════════════════════════════════════════════════

test("is defined", "{% if x is defined %}yes{% endif %}", {"x": 1}, "yes")
test("is undefined", "{% if x is undefined %}yes{% endif %}", {}, "yes")
test("is none", "{% if x is none %}yes{% endif %}", {"x": None}, "yes")
test("is string", "{% if x is string %}yes{% endif %}", {"x": "hi"}, "yes")
test("is number", "{% if x is number %}yes{% endif %}", {"x": 42}, "yes")
test("is boolean", "{% if x is boolean %}yes{% endif %}", {"x": True}, "yes")
test("is odd", "{% if 3 is odd %}yes{% endif %}", {}, "yes")
test("is even", "{% if 4 is even %}yes{% endif %}", {}, "yes")
test("is divisibleby", "{% if 9 is divisibleby(3) %}yes{% endif %}", {}, "yes")
test("is not divisibleby", "{% if 7 is not divisibleby(3) %}yes{% endif %}", {}, "yes")
test("is upper", "{% if 'ABC' is upper %}yes{% endif %}", {}, "yes")
test("is lower", "{% if 'abc' is lower %}yes{% endif %}", {}, "yes")
test("is eq", "{% if x is eq(5) %}yes{% endif %}", {"x": 5}, "yes")
test("is gt", "{% if x is gt(3) %}yes{% endif %}", {"x": 5}, "yes")
test("is lt", "{% if x is lt(5) %}yes{% endif %}", {"x": 3}, "yes")
test("is callable", "{% if f is callable %}yes{% endif %}", {"f": len}, "yes")
test("is iterable", "{% if x is iterable %}yes{% endif %}", {"x": [1, 2]}, "yes")
test("is mapping", "{% if x is mapping %}yes{% endif %}", {"x": {"a": 1}}, "yes")

# ═══════════════════════════════════════════════════════════════════════
print("\n── 3. Native Filters (49) ──")
# ═══════════════════════════════════════════════════════════════════════

test("escape", "{{ x|escape }}", {"x": "<b>hi</b>"}, "&lt;b&gt;hi&lt;/b&gt;")
test("safe", "{{ x|safe }}", {"x": "<b>hi</b>"}, "<b>hi</b>")
test("lower", "{{ 'HELLO'|lower }}", {}, "hello")
test("upper", "{{ 'hello'|upper }}", {}, "HELLO")
test("title", "{{ 'hello world'|title }}", {}, "Hello World")
test("capitalize", "{{ 'hello'|capitalize }}", {}, "Hello")
test("trim/strip", "{{ '  hi  '|trim }}", {}, "hi")
test("length", "{{ items|length }}", {"items": [1, 2, 3]}, "3")
test("default", "{{ x|default('N/A') }}", {}, "N/A")
test("join", "{{ items|join(', ') }}", {"items": ["a", "b", "c"]}, "a, b, c")
test("first", "{{ items|first }}", {"items": [10, 20, 30]}, "10")
test("last", "{{ items|last }}", {"items": [10, 20, 30]}, "30")
test("int", "{{ '42'|int }}", {}, "42")
test("float", "{{ '3.14'|float }}", {}, "3.14")
test("string", "{{ 42|string }}", {}, "42")
test("replace", "{{ 'hello world'|replace('world', 'there') }}", {}, "hello there")
test(
    "truncate", "{{ 'hello world this is a long string'|truncate(11) }}", {}, "hello..."
)
test("wordcount", "{{ 'one two three'|wordcount }}", {}, "3")
test("urlencode", "{{ 'a b&c'|urlencode }}", {}, "a%20b%26c")
test("striptags", "{{ '<p>hello</p>'|striptags }}", {}, "hello")
test("abs", "{{ -5|abs }}", {}, "5")
test("round", "{{ 3.7|round }}", {}, "4")
test(
    "sort", "{% for i in items|sort %}{{ i }}{% endfor %}", {"items": [3, 1, 2]}, "123"
)
test(
    "reverse",
    "{% for i in items|reverse %}{{ i }}{% endfor %}",
    {"items": [1, 2, 3]},
    "321",
)
test(
    "unique",
    "{% for i in items|unique %}{{ i }}{% endfor %}",
    {"items": [1, 2, 1, 3, 2]},
    "123",
)
test("tojson", "{{ data|tojson }}", {"data": {"a": 1}}, '{"a": 1}')
# Under autoescape, a list's str() is escaped (Jinja2 parity: |list is not a
# safe filter) — the single quotes become &#x27;.
test("list", "{{ 'abc'|list }}", {}, "[&#x27;a&#x27;, &#x27;b&#x27;, &#x27;c&#x27;]")
test("sum", "{{ items|sum }}", {"items": [1, 2, 3]}, "6")
test("min", "{{ items|min }}", {"items": [3, 1, 2]}, "1")
test("max", "{{ items|max }}", {"items": [3, 1, 2]}, "3")
test("indent", '{{ "line1\nline2"|indent(4) }}', {}, "line1\n    line2")
test("center", "{{ 'hi'|center(10) }}", {}, "    hi")
test("filesizeformat", "{{ 1048576|filesizeformat }}", {}, "1.0 MB")
test("wordwrap", "{{ 'a b c d e f'|wordwrap(5) }}", {}, "a b c\nd e f")
test(
    "xmlattr",
    "{{ attrs|xmlattr }}",
    {"attrs": {"class": "btn", "id": "x"}},
    'class="btn" id="x"',
)
test(
    "urlize",
    "{{ 'http://a.com'|urlize }}",
    {},
    '<a href="http://a.com" rel="noopener">http://a.com</a>',
)
test(
    "groupby",
    "{% for g in items|groupby('t') %}{{ g.grouper }}:{{ g.list|length }} {% endfor %}",
    {"items": [{"t": "A"}, {"t": "B"}, {"t": "A"}]},
    "A:2 B:1",
)
test(
    "select",
    "{% for i in items|select('odd') %}{{ i }}{% endfor %}",
    {"items": [1, 2, 3, 4, 5]},
    "135",
)
test(
    "reject",
    "{% for i in items|reject('none') %}{{ i }}{% endfor %}",
    {"items": [1, None, 2, None, 3]},
    "123",
)
test(
    "dictsort",
    "{% for k, v in data|dictsort %}{{ k }}={{ v }} {% endfor %}",
    {"data": {"b": 2, "a": 1}},
    "a=1 b=2",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 4. Control Flow ──")
# ═══════════════════════════════════════════════════════════════════════

test(
    "if/elif/else",
    "{% if x == 1 %}one{% elif x == 2 %}two{% else %}other{% endif %}",
    {"x": 2},
    "two",
)
test(
    "for loop basic",
    "{% for i in items %}{{ i }} {% endfor %}",
    {"items": [1, 2, 3]},
    "1 2 3",
)
test(
    "for loop.index",
    "{% for i in items %}{{ loop.index }}{% endfor %}",
    {"items": ["a", "b"]},
    "12",
)
test(
    "for loop.first/last",
    "{% for i in items %}{% if loop.first %}F{% endif %}{% if loop.last %}L{% endif %}{% endfor %}",
    {"items": [1, 2, 3]},
    "FL",
)
test(
    "for empty",
    "{% for i in items %}{{ i }}{% else %}empty{% endfor %}",
    {"items": []},
    "empty",
)
test(
    "for tuple unpack",
    "{% for k, v in items %}{{ k }}={{ v }} {% endfor %}",
    {"items": [("a", 1), ("b", 2)]},
    "a=1 b=2",
)
test(
    "for break",
    "{% for i in items %}{% if i == 3 %}{% break %}{% endif %}{{ i }}{% endfor %}",
    {"items": [1, 2, 3, 4]},
    "12",
)
test(
    "for continue",
    "{% for i in items %}{% if i == 2 %}{% continue %}{% endif %}{{ i }}{% endfor %}",
    {"items": [1, 2, 3]},
    "13",
)
test(
    "loop.cycle",
    "{% for i in items %}{{ loop.cycle('a', 'b') }}{% endfor %}",
    {"items": [1, 2, 3, 4]},
    "abab",
)
test(
    "loop.changed",
    "{% for i in items %}{% if loop.changed(i) %}{{ i }}{% endif %}{% endfor %}",
    {"items": [1, 1, 2, 2, 3]},
    "123",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 5. Template Composition ──")
# ═══════════════════════════════════════════════════════════════════════

test_file(
    "static extends",
    {
        "base.html": "<html>{% block content %}default{% endblock %}</html>",
        "child.html": '{% extends "base.html" %}{% block content %}CHILD{% endblock %}',
    },
    "child.html",
    {},
    "<html>CHILD</html>",
)

test_file(
    "super()",
    {
        "base.html": "{% block content %}BASE{% endblock %}",
        "child.html": '{% extends "base.html" %}{% block content %}{{ super() }}+CHILD{% endblock %}',
    },
    "child.html",
    {},
    "BASE+CHILD",
)

test_file(
    "required block",
    {
        "base.html": "{% block content required %}{% endblock %}",
        "child.html": '{% extends "base.html" %}{% block content %}OK{% endblock %}',
    },
    "child.html",
    {},
    "OK",
)

test_file(
    "scoped block in for-loop",
    {
        "base.html": "{% for i in items %}{% block row scoped %}{{ i }}{% endblock %},{% endfor %}",
        "child.html": '{% extends "base.html" %}{% block row %}[{{ i }}]{% endblock %}',
    },
    "child.html",
    {"items": ["a", "b"]},
    "[a],[b],",
)

test_file(
    "include with context",
    {
        "main.html": '{% include "partial.html" with context %}',
        "partial.html": "{{ name }}",
    },
    "main.html",
    {"name": "World"},
    "World",
)

test_file(
    "include without context",
    {
        "main.html": '{% include "partial.html" without context %}',
        "partial.html": "{{ name }}STATIC",
    },
    "main.html",
    {"name": "Hidden"},
    "STATIC",
)

test_file(
    "dynamic extends",
    {
        "base.html": "{% block content %}{% endblock %}",
        "child.html": "{% extends parent %}{% block content %}DYN{% endblock %}",
    },
    "child.html",
    {"parent": "base.html"},
    "DYN",
)

test_file(
    "include fallback list",
    {
        "main.html": '{% include ["missing.html", "fallback.html"] %}',
        "fallback.html": "FALLBACK",
    },
    "main.html",
    {},
    "FALLBACK",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 6. Macros ──")
# ═══════════════════════════════════════════════════════════════════════

test(
    "macro basic",
    '{% macro greet(name) %}Hello {{ name }}!{% endmacro %}{{ greet("World") }}',
    {},
    "Hello World!",
)
test(
    "macro default param",
    '{% macro btn(text, type="primary") %}<button class="{{ type }}">{{ text }}</button>{% endmacro %}{{ btn("OK") }}',
    {},
    '<button class="primary">OK</button>',
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 7. Scoping & Namespace ──")
# ═══════════════════════════════════════════════════════════════════════

test("set variable", "{% set x = 42 %}{{ x }}", {}, "42")
test(
    "with block scoping",
    "{% set x = 1 %}{% with x=2 %}{{ x }}{% endwith %}{{ x }}",
    {},
    "21",
)
test(
    "for-loop scoping (set doesn't leak)",
    "{% set x = 0 %}{% for i in [1,2,3] %}{% set x = x + 1 %}{% endfor %}{{ x }}",
    {},
    "0",
)
test(
    "namespace survives loop",
    "{% set ns = namespace(c=0) %}{% for i in [1,2,3] %}{% set ns.c = ns.c + 1 %}{% endfor %}{{ ns.c }}",
    {},
    "3",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 8. Autoescape ──")
# ═══════════════════════════════════════════════════════════════════════

test(
    "autoescape on (default)",
    "{{ x }}",
    {"x": "<b>bold</b>"},
    "&lt;b&gt;bold&lt;/b&gt;",
)
test(
    "autoescape false block",
    "{% autoescape false %}{{ x }}{% endautoescape %}",
    {"x": "<b>bold</b>"},
    "<b>bold</b>",
)
test(
    "autoescape nested",
    "{% autoescape false %}{{ a }}{% autoescape true %}{{ b }}{% endautoescape %}{{ c }}{% endautoescape %}",
    {"a": "<1>", "b": "<2>", "c": "<3>"},
    "<1>&lt;2&gt;<3>",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 9. Recursive For-Loops ──")
# ═══════════════════════════════════════════════════════════════════════

tree = [
    {
        "name": "A",
        "children": [
            {"name": "A1", "children": [{"name": "A1a", "children": []}]},
            {"name": "A2", "children": []},
        ],
    },
    {"name": "B", "children": []},
]
# Empty children list [] is falsy, so {% if item.children %} correctly skips
test(
    "recursive for-loop",
    "{% for item in items recursive %}{{ item.name }}{% if item.children %}({{ loop(item.children) }}){% endif %},{% endfor %}",
    {"items": tree},
    "A(A1(A1a,),A2,),B,",
)

test(
    "recursive loop.depth",
    "{% for item in items recursive %}{{ item.name }}:{{ loop.depth }}{% if item.children %}/{{ loop(item.children) }}{% endif %} {% endfor %}",
    {"items": [{"name": "R", "children": [{"name": "C", "children": []}]}]},
    "R:1/C:2",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 10. Undefined Behavior ──")
# ═══════════════════════════════════════════════════════════════════════

test("silent undefined", "{{ x }}", {}, "", undefined="silent")
test("debug undefined", "{{ x }}", {}, "{{ x }}", undefined="debug")
test(
    "debug nested undefined",
    "{{ user.name }}",
    {},
    "{{ user.name }}",
    undefined="debug",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 11. Custom Delimiters ──")
# ═══════════════════════════════════════════════════════════════════════

test(
    "custom delimiters << >> / <% %>",
    "<% if True %><< name >><% endif %>",
    {"name": "OK"},
    "OK",
    block_start_string="<%",
    block_end_string="%>",
    variable_start_string="<<",
    variable_end_string=">>",
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 12. Sandbox ──")
# ═══════════════════════════════════════════════════════════════════════

test("sandbox: normal access", "{{ name }}", {"name": "safe"}, "safe", sandboxed=True)
# sandbox blocks __class__ — raises SecurityError (correct Jinja2 behavior)
engine_sb = TemplateEngine(sandboxed=True)
try:
    engine_sb.render_string("{{ ''.__class__ }}", {})
    print("  FAIL: sandbox: blocks __class__ — no exception raised")
    failed += 1
    errors.append("sandbox: blocks __class__")
except Exception as e:
    if (
        "security" in str(e).lower()
        or "sandbox" in str(e).lower()
        or "blocked" in str(e).lower()
    ):
        print("  PASS: sandbox: blocks __class__ (SecurityError)")
        passed += 1
    else:
        print(f"  FAIL: sandbox: blocks __class__ — wrong error: {e}")
        failed += 1
        errors.append("sandbox: blocks __class__")
test(
    "sandbox: allows upper()",
    "{{ name.upper() }}",
    {"name": "hi"},
    "HI",
    sandboxed=True,
)

# ═══════════════════════════════════════════════════════════════════════
print("\n── 13. Whitespace Control ──")
# ═══════════════════════════════════════════════════════════════════════

test("trim left {%-", "  {%- if True %}yes{% endif %}", {}, "yes")
test("trim right -%}", "{% if True -%}  yes{% endif %}", {}, "yes")

# ═══════════════════════════════════════════════════════════════════════
print("\n── 14. Raw Blocks ──")
# ═══════════════════════════════════════════════════════════════════════

test("raw block", "{% raw %}{{ not_parsed }}{% endraw %}", {}, "{{ not_parsed }}")

# ═══════════════════════════════════════════════════════════════════════
print("\n── 15. Performance Benchmarks ──")
# ═══════════════════════════════════════════════════════════════════════

engine = TemplateEngine()

benchmarks = [
    ("variable output", "{{ name }}", {"name": "World"}),
    ("math expression", "{{ (a + b) * c }}", {"a": 2, "b": 3, "c": 4}),
    (
        "for loop (20 items)",
        "{% for i in items %}{{ i }}{% endfor %}",
        {"items": list(range(20))},
    ),
    (
        "if/elif/else chain",
        "{% if x == 1 %}a{% elif x == 2 %}b{% elif x == 3 %}c{% else %}d{% endif %}",
        {"x": 3},
    ),
    ("filter chain", "{{ name|upper|trim }}", {"name": "  hello world  "}),
    (
        "is-test (divisibleby)",
        "{% if x is divisibleby(3) %}y{% else %}n{% endif %}",
        {"x": 9},
    ),
]

print()
for bname, tmpl, ctx in benchmarks:
    for _ in range(100):
        engine.render_string(tmpl, ctx)
    start = time.perf_counter_ns()
    N = 10000
    for _ in range(N):
        engine.render_string(tmpl, ctx)
    elapsed = time.perf_counter_ns() - start
    print(f"  {bname:30s}: {elapsed / N:,.0f} ns/render")

# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print(
    f"PARITY SUITE RESULTS: {passed} passed, {failed} failed out of {passed + failed}"
)
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 70}")
sys.exit(1 if failed else 0)
