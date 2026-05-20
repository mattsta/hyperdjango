"""Tests for namespace() constructor in the Zig template engine."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import Namespace, TemplateEngine

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
print("TEST: namespace() constructor")
print("=" * 60)

# ── Python class works ──
ns = Namespace(counter=0, name="test")
assert ns.counter == 0
assert ns.name == "test"
ns.counter = 5
assert ns.counter == 5
print("  PASS: Namespace Python class")
passed += 1

# ── namespace() available in templates as default global ──
test(
    "namespace available as global", "{% set ns = namespace(x=1) %}{{ ns.x }}", {}, "1"
)

# ── Counter pattern: accumulate across for-loop ──
test(
    "counter across for-loop",
    "{% set ns = namespace(counter=0) %}{% for i in items %}{% set ns.counter = ns.counter + 1 %}{% endfor %}{{ ns.counter }}",
    {"items": [1, 2, 3, 4, 5]},
    "5",
)

# ── String accumulation ──
test(
    "string accumulation across loop",
    '{% set ns = namespace(result="") %}{% for item in items %}{% set ns.result = ns.result ~ item ~ "," %}{% endfor %}{{ ns.result }}',
    {"items": ["a", "b", "c"]},
    "a,b,c,",
)

# ── Multiple attributes ──
test(
    "multiple attributes",
    "{% set ns = namespace(x=10, y=20) %}{{ ns.x }}+{{ ns.y }}={{ ns.x + ns.y }}",
    {},
    "10+20=30",
)

# ── Modify inside if-block ──
test(
    "modify inside if-block",
    "{% set ns = namespace(found=0) %}{% if True %}{% set ns.found = 1 %}{% endif %}{{ ns.found }}",
    {},
    "1",
)

# ── Without namespace: for-loop set doesn't survive (Jinja2 scoping) ──
test(
    "without namespace — set in for-loop is scoped",
    "{% set counter = 0 %}{% for i in items %}{% set counter = counter + 1 %}{% endfor %}{{ counter }}",
    {"items": [1, 2, 3]},
    "0",
)

# ── With namespace: for-loop set survives ──
test(
    "with namespace — set survives for-loop scope",
    "{% set ns = namespace(counter=0) %}{% for i in items %}{% set ns.counter = ns.counter + 1 %}{% endfor %}{{ ns.counter }}",
    {"items": [1, 2, 3]},
    "3",
)

# ── Namespace from context ──
test(
    "namespace passed from context",
    "{{ ns.greeting }} {{ ns.name }}!",
    {"ns": Namespace(greeting="Hello", name="World")},
    "Hello World!",
)

# ── Modify namespace from context ──
test(
    "modify namespace from context in template",
    "{% set ns.count = ns.count + 10 %}{{ ns.count }}",
    {"ns": Namespace(count=5)},
    "15",
)

# ── Nested namespace access ──
test(
    "namespace in for-loop with accumulation",
    "{% set ns = namespace(total=0) %}{% for item in items %}{% set ns.total = ns.total + item %}{% endfor %}Total: {{ ns.total }}",
    {"items": [10, 20, 30]},
    "Total: 60",
)

# ── Boolean namespace attribute ──
test(
    "boolean namespace attribute",
    "{% set ns = namespace(found=0) %}{% for item in items %}{% if item == target %}{% set ns.found = 1 %}{% endif %}{% endfor %}{% if ns.found %}Found!{% else %}Not found{% endif %}",
    {"items": [1, 2, 3, 4], "target": 3},
    "Found!",
)

# ── List attribute (append via concat) ──
test(
    "list attribute via concatenation",
    "{% set ns = namespace(count=0) %}{% for x in [1,2,3,4,5] %}{% if x is divisibleby(2) %}{% set ns.count = ns.count + 1 %}{% endif %}{% endfor %}Even count: {{ ns.count }}",
    {},
    "Even count: 2",
)

# ── Performance ──
print("\n── Performance ──")
engine = TemplateEngine()
tmpl = "{% set ns = namespace(counter=0) %}{% for i in items %}{% set ns.counter = ns.counter + 1 %}{% endfor %}{{ ns.counter }}"
ctx = {"items": list(range(50))}

for _ in range(100):
    engine.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 5000
for _ in range(N):
    engine.render_string(tmpl, ctx)
elapsed = time.perf_counter_ns() - start
print(f"  namespace counter (50 items): {elapsed / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
