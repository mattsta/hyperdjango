"""Tests for groupby, select, reject filters in the Zig template engine."""

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
print("TEST: groupby, select, reject filters")
print("=" * 60)

# ── groupby basic ──
test(
    "groupby dict items by key",
    "{% for group in users|groupby('role') %}{{ group.grouper }}:{% for u in group.list %}{{ u.name }},{% endfor %} {% endfor %}",
    {
        "users": [
            {"name": "Alice", "role": "admin"},
            {"name": "Bob", "role": "user"},
            {"name": "Carol", "role": "admin"},
            {"name": "Dave", "role": "user"},
        ]
    },
    "admin:Alice,Carol, user:Bob,Dave,",
)

test(
    "groupby single group",
    "{% for g in items|groupby('type') %}{{ g.grouper }}({{ g.list|length }}){% endfor %}",
    {"items": [{"type": "A", "v": 1}, {"type": "A", "v": 2}]},
    "A(2)",
)

test(
    "groupby empty list",
    "{% for g in items|groupby('x') %}{{ g.grouper }}{% endfor %}DONE",
    {"items": []},
    "DONE",
)

test(
    "groupby preserves order",
    "{% for g in items|groupby('cat') %}{{ g.grouper }},{% endfor %}",
    {
        "items": [
            {"cat": "B"},
            {"cat": "A"},
            {"cat": "B"},
            {"cat": "C"},
            {"cat": "A"},
        ]
    },
    "B,A,C,",
)


# ── groupby with objects (attribute access) ──
class User:
    def __init__(self, name: str, role: str):
        self.name = name
        self.role = role


test(
    "groupby object attributes",
    "{% for g in users|groupby('role') %}{{ g.grouper }}:{{ g.list|length }} {% endfor %}",
    {"users": [User("A", "admin"), User("B", "user"), User("C", "admin")]},
    "admin:2 user:1",
)

# ── select filter ──
test(
    "select truthy items (default)",
    "{% for item in items|select %}{{ item }},{% endfor %}",
    {"items": [1, 0, "hello", "", None, True, False]},
    "1,hello,True,",
)

test(
    "select with 'none' test",
    "{% for item in items|select('none') %}NULL {% endfor %}",
    {"items": [1, None, "hi", None]},
    "NULL NULL",
)

test(
    "select with 'string' test",
    "{% for item in items|select('string') %}{{ item }},{% endfor %}",
    {"items": [1, "hello", 2.5, "world", True]},
    "hello,world,",
)

test(
    "select with 'number' test",
    "{% for item in items|select('number') %}{{ item }},{% endfor %}",
    {"items": [1, "hello", 42, "world", 0]},
    "1,42,0,",
)

test(
    "select with 'odd' test",
    "{% for item in items|select('odd') %}{{ item }},{% endfor %}",
    {"items": [1, 2, 3, 4, 5, 6]},
    "1,3,5,",
)

test(
    "select with 'even' test",
    "{% for item in items|select('even') %}{{ item }},{% endfor %}",
    {"items": [1, 2, 3, 4, 5, 6]},
    "2,4,6,",
)

test(
    "select empty result",
    "{% for item in items|select('none') %}X{% endfor %}DONE",
    {"items": [1, 2, 3]},
    "DONE",
)

# ── reject filter ──
test(
    "reject none values",
    "{% for item in items|reject('none') %}{{ item }},{% endfor %}",
    {"items": [1, None, "hello", None, 42]},
    "1,hello,42,",
)

test(
    "reject falsy items (default = truthy test inverted)",
    "{% for item in items|reject %}{{ item }},{% endfor %}",
    {"items": [1, 0, "hello", "", None, True, False]},
    "0,,,False,",
)

test(
    "reject with 'string' test — keep non-strings",
    "{% for item in items|reject('string') %}{{ item }},{% endfor %}",
    {"items": [1, "hello", 42, "world", True]},
    "1,42,True,",
)

test(
    "reject with 'even' test — keep odd numbers",
    "{% for item in items|reject('even') %}{{ item }},{% endfor %}",
    {"items": [1, 2, 3, 4, 5]},
    "1,3,5,",
)

# ── Chaining select/reject with other filters ──
test(
    "select then sort",
    "{% for item in items|select('number')|sort %}{{ item }},{% endfor %}",
    {"items": [3, "x", 1, "y", 2]},
    "1,2,3,",
)

test(
    "reject then length",
    "{{ items|reject('none')|length }}",
    {"items": [1, None, 2, None, 3]},
    "3",
)

# ── Combined groupby + select ──
test(
    "groupby after select",
    "{% for g in items|select('mapping')|groupby('type') %}{{ g.grouper }}:{{ g.list|length }} {% endfor %}",
    {"items": [{"type": "A"}, None, {"type": "B"}, {"type": "A"}, "skip"]},
    "A:2 B:1",
)

# ── Performance ──
print("\n── Performance ──")
engine = TemplateEngine()

# groupby benchmark
tmpl_gb = "{% for g in items|groupby('cat') %}{{ g.grouper }}{% endfor %}"
items_gb = [{"cat": f"cat{i % 5}", "val": i} for i in range(100)]
ctx_gb = {"items": items_gb}
for _ in range(50):
    engine.render_string(tmpl_gb, ctx_gb)
start = time.perf_counter_ns()
N = 2000
for _ in range(N):
    engine.render_string(tmpl_gb, ctx_gb)
elapsed = time.perf_counter_ns() - start
print(f"  groupby (100 items, 5 groups): {elapsed / N:.0f} ns/render ({N} iterations)")

# select benchmark
tmpl_sel = "{% for item in items|select('odd') %}{{ item }}{% endfor %}"
ctx_sel = {"items": list(range(100))}
for _ in range(50):
    engine.render_string(tmpl_sel, ctx_sel)
start = time.perf_counter_ns()
for _ in range(N):
    engine.render_string(tmpl_sel, ctx_sel)
elapsed = time.perf_counter_ns() - start
print(f"  select odd (100 items):        {elapsed / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
