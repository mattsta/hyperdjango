"""Tests for the xmlattr filter in the Zig template engine."""

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
print("TEST: xmlattr filter")
print("=" * 60)

# ── Basic dict → attributes ──
test(
    "basic dict to attributes",
    "{{ attrs|xmlattr }}",
    {"attrs": {"class": "btn", "id": "submit"}},
    'class="btn" id="submit"',
)

# ── Single attribute ──
test(
    "single attribute",
    "{{ attrs|xmlattr }}",
    {"attrs": {"href": "/home"}},
    'href="/home"',
)

# ── Empty dict ──
test("empty dict", "{{ attrs|xmlattr }}", {"attrs": {}}, "")

# ── HTML escaping in values ──
test(
    "html escape in values",
    "{{ attrs|xmlattr }}",
    {"attrs": {"title": "a<b>c&d"}},
    'title="a&lt;b&gt;c&amp;d"',
)

# ── Quote escaping ──
test(
    "quote escaping",
    "{{ attrs|xmlattr }}",
    {"attrs": {"data-val": 'say "hello"'}},
    'data-val="say &quot;hello&quot;"',
)

# ── None values skipped ──
test(
    "None values are skipped",
    "{{ attrs|xmlattr }}",
    {"attrs": {"class": "btn", "disabled": None, "id": "x"}},
    'class="btn" id="x"',
)

# ── Numeric values coerced to string ──
test(
    "numeric values coerced",
    "{{ attrs|xmlattr }}",
    {"attrs": {"tabindex": 1, "data-count": 42}},
    'tabindex="1" data-count="42"',
)

# ── Boolean values ──
test(
    "boolean values as strings",
    "{{ attrs|xmlattr }}",
    {"attrs": {"data-active": True}},
    'data-active="True"',
)

# ── Used in HTML context ──
test(
    "used in HTML tag",
    "<div {{ attrs|xmlattr }}>content</div>",
    {"attrs": {"class": "box", "id": "main"}},
    '<div class="box" id="main">content</div>',
)

# ── Used with autoescape false (raw HTML output) ──
test(
    "with autoescape false",
    "{% autoescape false %}<input {{ attrs|xmlattr }}>{% endautoescape %}",
    {"attrs": {"type": "text", "name": "q", "value": "search"}},
    '<input type="text" name="q" value="search">',
)

# ── Non-dict input passes through ──
test("non-dict input passes through", "{{ val|xmlattr }}", {"val": "hello"}, "hello")

# ── Data attributes ──
test(
    "data attributes",
    "{{ attrs|xmlattr }}",
    {"attrs": {"data-toggle": "modal", "data-target": "#myModal"}},
    'data-toggle="modal" data-target="#myModal"',
)

# ── Special characters in attribute name ──
test(
    "aria attributes",
    "{{ attrs|xmlattr }}",
    {"attrs": {"aria-label": "Close", "role": "button"}},
    'aria-label="Close" role="button"',
)

# ── Performance ──
print("\n── Performance ──")
engine = TemplateEngine()
tmpl = "{{ attrs|xmlattr }}"
ctx = {
    "attrs": {
        "class": "btn btn-primary",
        "id": "submit",
        "type": "button",
        "data-action": "save",
    }
}

for _ in range(100):
    engine.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 10000
for _ in range(N):
    engine.render_string(tmpl, ctx)
elapsed = time.perf_counter_ns() - start
print(f"  xmlattr (4 attrs): {elapsed / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
