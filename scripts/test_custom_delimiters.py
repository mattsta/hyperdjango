"""Tests for custom template delimiters in the Zig template engine."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test(
    name: str, template: str, context: dict, expected: str, **engine_kwargs
) -> None:
    global passed, failed
    engine = TemplateEngine(**engine_kwargs)
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
print("TEST: Custom template delimiters")
print("=" * 60)

# ── Default delimiters still work ──
test("default delimiters work", "Hello {{ name }}!", {"name": "World"}, "Hello World!")

test("default block delimiters work", "{% if True %}yes{% endif %}", {}, "yes")

# ── Custom variable delimiters ──
test(
    "custom variable delimiters << >>",
    "Hello << name >>!",
    {"name": "World"},
    "Hello World!",
    variable_start_string="<<",
    variable_end_string=">>",
)

# ── Custom block delimiters ──
test(
    "custom block delimiters <% %>",
    "<% if True %>yes<% endif %>",
    {},
    "yes",
    block_start_string="<%",
    block_end_string="%>",
)

# ── Both custom ──
test(
    "custom block + variable delimiters",
    "<% if show %><< name >><% endif %>",
    {"show": True, "name": "Alice"},
    "Alice",
    block_start_string="<%",
    block_end_string="%>",
    variable_start_string="<<",
    variable_end_string=">>",
)

# ── Custom comment delimiters ──
test(
    "custom comment delimiters ## ##",
    "Hello ## this is hidden ## World",
    {},
    "Hello  World",
    comment_start_string="##",
    comment_end_string="##",
)

# ── ERB-style delimiters ──
test(
    "ERB-style delimiters",
    "<% for item in items %><%= item %>,<% endfor %>",
    {"items": ["a", "b", "c"]},
    "a,b,c,",
    block_start_string="<%",
    block_end_string="%>",
    variable_start_string="<%=",
    variable_end_string="%>",
)

# ── Longer delimiters ──
test(
    "longer delimiters [[ ]] and [% %]",
    "[% if x %][[ x ]][% endif %]",
    {"x": 42},
    "42",
    block_start_string="[%",
    block_end_string="%]",
    variable_start_string="[[",
    variable_end_string="]]",
)

# ── Default delimiters don't conflict when custom set ──
test(
    "original {{ }} treated as text with custom delims",
    "{{ not_a_var }} << name >>",
    {"name": "World", "not_a_var": "HIDDEN"},
    "{{ not_a_var }} World",
    variable_start_string="<<",
    variable_end_string=">>",
    block_start_string="<%",
    block_end_string="%>",
)

# ── For loop with custom delimiters ──
test(
    "for loop with custom delimiters",
    "<% for i in items %>[[ i ]] <% endfor %>",
    {"items": [1, 2, 3]},
    "1 2 3",
    block_start_string="<%",
    block_end_string="%>",
    variable_start_string="[[",
    variable_end_string="]]",
)

# ── Filter with custom delimiters ──
test(
    "filter with custom delimiters",
    "<< name|upper >>",
    {"name": "hello"},
    "HELLO",
    variable_start_string="<<",
    variable_end_string=">>",
)

# ── Multiple engines with different delimiters don't interfere ──
test("engine A uses defaults", "{{ name }}", {"name": "A"}, "A")

test(
    "engine B uses custom (after A)",
    "<< name >>",
    {"name": "B"},
    "B",
    variable_start_string="<<",
    variable_end_string=">>",
)

test("engine C uses defaults again (after B)", "{{ name }}", {"name": "C"}, "C")

# ── Performance ──
print("\n── Performance ──")
engine_default = TemplateEngine()
engine_custom = TemplateEngine(
    block_start_string="<%",
    block_end_string="%>",
    variable_start_string="<<",
    variable_end_string=">>",
)

tmpl_default = "{% for i in items %}{{ i }}{% endfor %}"
tmpl_custom = "<% for i in items %><< i >><% endfor %>"
ctx = {"items": list(range(20))}

for _ in range(100):
    engine_default.render_string(tmpl_default, ctx)
    engine_custom.render_string(tmpl_custom, ctx)

start = time.perf_counter_ns()
N = 5000
for _ in range(N):
    engine_default.render_string(tmpl_default, ctx)
elapsed_default = time.perf_counter_ns() - start

start = time.perf_counter_ns()
for _ in range(N):
    engine_custom.render_string(tmpl_custom, ctx)
elapsed_custom = time.perf_counter_ns() - start

print(f"  default delimiters: {elapsed_default / N:.0f} ns/render ({N} iterations)")
print(f"  custom delimiters:  {elapsed_custom / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
