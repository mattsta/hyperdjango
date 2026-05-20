"""Tests for divisibleby(n) and other parameterized is-tests in the Zig template engine."""

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
print("TEST: divisibleby(n) — parameterized is-test")
print("=" * 60)

# ── divisibleby basic ──
test(
    "divisibleby(2) even number",
    "{% if 4 is divisibleby(2) %}yes{% else %}no{% endif %}",
    {},
    "yes",
)

test(
    "divisibleby(2) odd number",
    "{% if 3 is divisibleby(2) %}yes{% else %}no{% endif %}",
    {},
    "no",
)

test(
    "divisibleby(3)",
    "{% if 9 is divisibleby(3) %}yes{% else %}no{% endif %}",
    {},
    "yes",
)

test(
    "divisibleby(3) not divisible",
    "{% if 10 is divisibleby(3) %}yes{% else %}no{% endif %}",
    {},
    "no",
)

test(
    "divisibleby(1) always true",
    "{% if 7 is divisibleby(1) %}yes{% else %}no{% endif %}",
    {},
    "yes",
)

test(
    "divisibleby(5)",
    "{% if 25 is divisibleby(5) %}yes{% else %}no{% endif %}",
    {},
    "yes",
)

test(
    "divisibleby with variable",
    "{% if n is divisibleby(d) %}yes{% else %}no{% endif %}",
    {"n": 12, "d": 4},
    "yes",
)

test(
    "divisibleby with variable not divisible",
    "{% if n is divisibleby(d) %}yes{% else %}no{% endif %}",
    {"n": 13, "d": 4},
    "no",
)

test(
    "divisibleby(0) safe — no crash",
    "{% if 5 is divisibleby(0) %}yes{% else %}no{% endif %}",
    {},
    "no",
)

test(
    "is not divisibleby",
    "{% if 7 is not divisibleby(3) %}yes{% else %}no{% endif %}",
    {},
    "yes",
)

test(
    "is not divisibleby false",
    "{% if 9 is not divisibleby(3) %}yes{% else %}no{% endif %}",
    {},
    "no",
)

# ── divisibleby in expressions ──
test(
    "divisibleby in ternary",
    '{{ "even" if x is divisibleby(2) else "odd" }}',
    {"x": 4},
    "even",
)

test(
    "divisibleby in ternary odd",
    '{{ "even" if x is divisibleby(2) else "odd" }}',
    {"x": 5},
    "odd",
)

# ── divisibleby in for-loop ──
test(
    "divisibleby for row striping",
    "{% for i in items %}{% if i is divisibleby(3) %}[{{ i }}]{% else %}{{ i }}{% endif %} {% endfor %}",
    {"items": [1, 2, 3, 4, 5, 6]},
    "1 2 [3] 4 5 [6]",
)

# ── sameas (identity test) ──
test(
    "sameas true",
    "{% if x is sameas(y) %}yes{% else %}no{% endif %}",
    {"x": None, "y": None},
    "yes",
)

a_list = [1, 2, 3]
b_list = [1, 2, 3]
test(
    "sameas false — equal but not same",
    "{% if x is sameas(y) %}yes{% else %}no{% endif %}",
    {"x": a_list, "y": b_list},
    "no",
)

test(
    "is not sameas",
    "{% if x is not sameas(y) %}yes{% else %}no{% endif %}",
    {"x": 1, "y": 2},
    "yes",
)

# ── eq / equalto ──
test("eq test", "{% if x is eq(5) %}yes{% else %}no{% endif %}", {"x": 5}, "yes")

test("eq test false", "{% if x is eq(5) %}yes{% else %}no{% endif %}", {"x": 3}, "no")

test(
    "equalto alias",
    "{% if x is equalto(5) %}yes{% else %}no{% endif %}",
    {"x": 5},
    "yes",
)

test("is not eq", "{% if x is not eq(5) %}yes{% else %}no{% endif %}", {"x": 3}, "yes")

# ── ne ──
test("ne test", "{% if x is ne(5) %}yes{% else %}no{% endif %}", {"x": 3}, "yes")

test("ne test false", "{% if x is ne(5) %}yes{% else %}no{% endif %}", {"x": 5}, "no")

# ── gt / greaterthan ──
test("gt test", "{% if x is gt(3) %}yes{% else %}no{% endif %}", {"x": 5}, "yes")

test("gt test false", "{% if x is gt(3) %}yes{% else %}no{% endif %}", {"x": 2}, "no")

test(
    "greaterthan alias",
    "{% if x is greaterthan(3) %}yes{% else %}no{% endif %}",
    {"x": 5},
    "yes",
)

# ── ge ──
test("ge test equal", "{% if x is ge(5) %}yes{% else %}no{% endif %}", {"x": 5}, "yes")

test(
    "ge test greater", "{% if x is ge(5) %}yes{% else %}no{% endif %}", {"x": 6}, "yes"
)

test("ge test less", "{% if x is ge(5) %}yes{% else %}no{% endif %}", {"x": 4}, "no")

# ── lt / lessthan ──
test("lt test", "{% if x is lt(5) %}yes{% else %}no{% endif %}", {"x": 3}, "yes")

test("lt test false", "{% if x is lt(5) %}yes{% else %}no{% endif %}", {"x": 7}, "no")

test(
    "lessthan alias",
    "{% if x is lessthan(5) %}yes{% else %}no{% endif %}",
    {"x": 3},
    "yes",
)

# ── le ──
test("le test equal", "{% if x is le(5) %}yes{% else %}no{% endif %}", {"x": 5}, "yes")

test("le test less", "{% if x is le(5) %}yes{% else %}no{% endif %}", {"x": 4}, "yes")

test("le test greater", "{% if x is le(5) %}yes{% else %}no{% endif %}", {"x": 6}, "no")

# ── Combined tests in and/or expressions ──
test(
    "divisibleby combined with and",
    "{% if x is divisibleby(2) and x is gt(5) %}yes{% else %}no{% endif %}",
    {"x": 8},
    "yes",
)

test(
    "divisibleby combined with and — fails gt",
    "{% if x is divisibleby(2) and x is gt(5) %}yes{% else %}no{% endif %}",
    {"x": 4},
    "no",
)

test(
    "divisibleby combined with or",
    "{% if x is divisibleby(3) or x is divisibleby(5) %}fizzbuzz{% else %}{{ x }}{% endif %}",
    {"x": 15},
    "fizzbuzz",
)

# ── Performance benchmark ──
print("\n── Performance ──")
engine = TemplateEngine()
tmpl = "{% if x is divisibleby(3) %}y{% else %}n{% endif %}"
ctx = {"x": 99}

# Warmup
for _ in range(100):
    engine.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 10000
for _ in range(N):
    engine.render_string(tmpl, ctx)
elapsed_ns = time.perf_counter_ns() - start
per_render = elapsed_ns / N
print(f"  divisibleby(3) test: {per_render:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
