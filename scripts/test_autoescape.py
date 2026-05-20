"""Tests for {% autoescape false %}...{% endautoescape %} block-level control."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test(name: str, template: str, context: dict, expected: str) -> None:
    global passed, failed
    engine = TemplateEngine()
    try:
        result = engine.render_string(template, context)
        if result.strip() == expected.strip():
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name}")
            print(f"    Expected: {expected!r}")
            print(f"    Got:      {result.strip()!r}")
            failed += 1
            errors.append(name)
    except Exception as e:
        print(f"  ERROR: {name}: {e}")
        failed += 1
        errors.append(name)


print("=" * 60)
print("TEST: {% autoescape false/true %} block-level control")
print("=" * 60)

# ── Default: auto-escaping is ON ──
test(
    "default auto-escaping ON",
    "{{ content }}",
    {"content": "<b>bold</b>"},
    "&lt;b&gt;bold&lt;/b&gt;",
)

# ── autoescape false: no escaping ──
test(
    "autoescape false disables escaping",
    "{% autoescape false %}{{ content }}{% endautoescape %}",
    {"content": "<b>bold</b>"},
    "<b>bold</b>",
)

# ── autoescape true: explicit enable (same as default) ──
test(
    "autoescape true enables escaping",
    "{% autoescape true %}{{ content }}{% endautoescape %}",
    {"content": "<b>bold</b>"},
    "&lt;b&gt;bold&lt;/b&gt;",
)

# ── autoescape off: alternative keyword ──
test(
    "autoescape off disables escaping",
    "{% autoescape off %}{{ content }}{% endautoescape %}",
    {"content": "<script>alert('xss')</script>"},
    "<script>alert('xss')</script>",
)

# ── Scoping: outside block is still escaped ──
test(
    "autoescape false is scoped — outside still escaped",
    "{{ a }}{% autoescape false %}{{ b }}{% endautoescape %}{{ c }}",
    {"a": "<1>", "b": "<2>", "c": "<3>"},
    "&lt;1&gt;<2>&lt;3&gt;",
)

# ── Nesting: inner block overrides outer ──
test(
    "nested autoescape blocks — inner overrides outer",
    "{% autoescape false %}{{ a }}{% autoescape true %}{{ b }}{% endautoescape %}{{ c }}{% endautoescape %}",
    {"a": "<1>", "b": "<2>", "c": "<3>"},
    "<1>&lt;2&gt;<3>",
)

# ── autoescape false with |safe filter (no double-escaping issue) ──
test(
    "autoescape false + |safe filter (no-op)",
    "{% autoescape false %}{{ content|safe }}{% endautoescape %}",
    {"content": "<b>bold</b>"},
    "<b>bold</b>",
)

# ── autoescape true with |safe filter overrides ──
test(
    "autoescape true + |safe disables for that var",
    "{% autoescape true %}{{ content|safe }}{% endautoescape %}",
    {"content": "<b>bold</b>"},
    "<b>bold</b>",
)

# ── Static text unaffected ──
test(
    "autoescape false — static HTML unaffected",
    "{% autoescape false %}<p>Hello</p>{% endautoescape %}",
    {},
    "<p>Hello</p>",
)

# ── Multiple variables in autoescape false ──
test(
    "multiple vars in autoescape false",
    "{% autoescape false %}{{ a }} {{ b }} {{ c }}{% endautoescape %}",
    {"a": "<h1>", "b": "&amp;", "c": "</h1>"},
    "<h1> &amp; </h1>",
)

# ── Autoescape false with for-loop ──
test(
    "autoescape false inside for-loop",
    "{% autoescape false %}{% for item in items %}{{ item }} {% endfor %}{% endautoescape %}",
    {"items": ["<a>", "<b>", "<c>"]},
    "<a> <b> <c>",
)

# ── Autoescape false with if-block ──
test(
    "autoescape false inside if-block",
    "{% autoescape false %}{% if show %}{{ content }}{% endif %}{% endautoescape %}",
    {"show": True, "content": "<em>hi</em>"},
    "<em>hi</em>",
)

# ── Special characters: & < > " ' ──
test(
    "all special chars escaped by default",
    "{{ content }}",
    {"content": "& < > \" '"},
    "&amp; &lt; &gt; &quot; &#x27;",
)

test(
    "all special chars raw in autoescape false",
    "{% autoescape false %}{{ content }}{% endautoescape %}",
    {"content": "& < > \" '"},
    "& < > \" '",
)

# ── Performance ──
print("\n── Performance ──")
engine = TemplateEngine()
tmpl = "{% autoescape false %}{{ a }}{{ b }}{{ c }}{% endautoescape %}"
ctx = {"a": "<h1>Title</h1>", "b": "<p>Body</p>", "c": "<footer>&copy;</footer>"}

for _ in range(100):
    engine.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 10000
for _ in range(N):
    engine.render_string(tmpl, ctx)
elapsed = time.perf_counter_ns() - start
print(f"  autoescape false block: {elapsed / N:.0f} ns/render ({N} iterations)")

tmpl2 = "{{ a }}{{ b }}{{ c }}"
for _ in range(100):
    engine.render_string(tmpl2, ctx)

start = time.perf_counter_ns()
for _ in range(N):
    engine.render_string(tmpl2, ctx)
elapsed2 = time.perf_counter_ns() - start
print(f"  default (escaped):      {elapsed2 / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
