"""Tests for {% for item in items recursive %} in the Zig template engine."""

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
        # Normalize whitespace for comparison
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


print("=" * 60)
print("TEST: Recursive for-loops")
print("=" * 60)

# ── Basic recursive tree rendering ──
tree = [
    {
        "name": "A",
        "children": [
            {"name": "A1", "children": []},
            {
                "name": "A2",
                "children": [
                    {"name": "A2a", "children": []},
                ],
            },
        ],
    },
    {"name": "B", "children": []},
]

test(
    "basic recursive tree",
    "{% for item in items recursive %}{{ item.name }}{% if item.children %}[{{ loop(item.children) }}]{% endif %},{% endfor %}",
    {"items": tree},
    "A[A1,A2[A2a,],],B,",
)

# ── Flat list (no recursion triggered) ──
test(
    "recursive keyword on flat list — no children",
    "{% for item in items recursive %}{{ item.name }},{% endfor %}",
    {"items": [{"name": "X", "children": []}, {"name": "Y", "children": []}]},
    "X,Y,",
)

# ── loop.depth tracking ──
test(
    "loop.depth starts at 1",
    "{% for item in items recursive %}{{ item.name }}(d{{ loop.depth }}){% if item.children %}{{ loop(item.children) }}{% endif %},{% endfor %}",
    {
        "items": [
            {
                "name": "A",
                "children": [
                    {
                        "name": "A1",
                        "children": [
                            {"name": "A1a", "children": []},
                        ],
                    },
                ],
            },
        ]
    },
    "A(d1)A1(d2)A1a(d3),,,",
)

# ── loop.depth0 ──
test(
    "loop.depth0 starts at 0",
    "{% for item in items recursive %}{{ item.name }}:{{ loop.depth0 }}{% if item.children %}/{{ loop(item.children) }}{% endif %} {% endfor %}",
    {"items": [{"name": "R", "children": [{"name": "C", "children": []}]}]},
    "R:0/C:1",
)

# ── loop.index works at each level ──
test(
    "loop.index resets at each recursive level",
    "{% for item in items recursive %}{{ loop.index }}:{{ item.name }}{% if item.children %}[{{ loop(item.children) }}]{% endif %} {% endfor %}",
    {
        "items": [
            {"name": "A", "children": [{"name": "X"}, {"name": "Y"}]},
            {"name": "B", "children": []},
        ]
    },
    "1:A[1:X 2:Y ] 2:B",
)

# ── Nested list rendering (menu with submenus) ──
menu = [
    {"title": "Home", "children": []},
    {
        "title": "Products",
        "children": [
            {"title": "Software", "children": []},
            {
                "title": "Hardware",
                "children": [
                    {"title": "Laptops", "children": []},
                    {"title": "Desktops", "children": []},
                ],
            },
        ],
    },
    {"title": "About", "children": []},
]

test(
    "menu with submenus",
    "{% for item in menu recursive %}<li>{{ item.title }}{% if item.children %}<ul>{{ loop(item.children) }}</ul>{% endif %}</li>{% endfor %}",
    {"menu": menu},
    "<li>Home</li><li>Products<ul><li>Software</li><li>Hardware<ul><li>Laptops</li><li>Desktops</li></ul></li></ul></li><li>About</li>",
)

# ── Empty recursive list ──
test(
    "empty recursive list",
    "{% for item in items recursive %}{{ item.name }}{% endfor %}DONE",
    {"items": []},
    "DONE",
)

# ── Single item, deep recursion ──
deep = {
    "name": "L1",
    "children": [
        {
            "name": "L2",
            "children": [{"name": "L3", "children": [{"name": "L4", "children": []}]}],
        }
    ],
}

test(
    "deep 4-level recursion",
    "{% for item in items recursive %}{{ item.name }}>{% if item.children %}{{ loop(item.children) }}{% endif %}{% endfor %}",
    {"items": [deep]},
    "L1>L2>L3>L4>",
)

# ── Non-recursive for-loop still works ──
test(
    "non-recursive for-loop unaffected",
    "{% for item in items %}{{ item }},{% endfor %}",
    {"items": [1, 2, 3]},
    "1,2,3,",
)

# ── Performance ──
print("\n── Performance ──")
engine = TemplateEngine()
perf_tree = [
    {
        "name": f"n{i}",
        "children": [{"name": f"n{i}c{j}", "children": []} for j in range(3)],
    }
    for i in range(10)
]
tmpl = "{% for item in items recursive %}{{ item.name }}{% if item.children %}({{ loop(item.children) }}){% endif %},{% endfor %}"
ctx = {"items": perf_tree}

for _ in range(50):
    engine.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 2000
for _ in range(N):
    engine.render_string(tmpl, ctx)
elapsed = time.perf_counter_ns() - start
print(f"  recursive tree (10x3): {elapsed / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
