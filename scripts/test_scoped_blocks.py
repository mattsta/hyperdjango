"""Tests for {% block sidebar scoped %} in the Zig template engine."""

# hyper-test: unit

import sys
import tempfile
import time
from pathlib import Path

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test_extends(
    name: str, parent_src: str, child_src: str, context: dict, expected: str
) -> None:
    global passed, failed
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        (tmppath / "base.html").write_text(parent_src)
        (tmppath / "child.html").write_text(child_src)

        engine = TemplateEngine()
        engine.template_dir = tmpdir
        try:
            result = engine.render("child.html", context)
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
print("TEST: Scoped blocks — {% block sidebar scoped %}")
print("=" * 60)

# ── Scoped block inside for-loop: child sees loop var ──
test_extends(
    "scoped block in for-loop — child sees item",
    "{% for item in items %}{% block row scoped %}{{ item }}{% endblock %},{% endfor %}",
    '{% extends "base.html" %}{% block row %}[{{ item }}]{% endblock %}',
    {"items": ["a", "b", "c"]},
    "[a],[b],[c],",
)

# ── Non-scoped block inside for-loop: child doesn't see loop var ──
# In Jinja2, non-scoped blocks inside for-loops can't access the loop var from child.
# Our engine currently passes the context through, so the child CAN see it by default.
# The "scoped" keyword is about explicitly opting in — without it, behavior is undefined.
# For compatibility: we accept that our engine passes context through (simpler model).
test_extends(
    "non-scoped block in for-loop — child may not see item (engine-dependent)",
    "{% for item in items %}{% block row %}{{ item }}{% endblock %},{% endfor %}",
    '{% extends "base.html" %}{% block row %}[{{ item }}]{% endblock %}',
    {"items": ["a", "b", "c"]},
    "[a],[b],[c],",
)

# ── Scoped block sees loop.index ──
test_extends(
    "scoped block sees loop.index",
    "{% for x in items %}{% block cell scoped %}{{ loop.index }}{% endblock %},{% endfor %}",
    '{% extends "base.html" %}{% block cell %}#{{ loop.index }}{% endblock %}',
    {"items": [10, 20, 30]},
    "#1,#2,#3,",
)

# ── Scoped block inside with-block: child sees with-variables ──
test_extends(
    "scoped block in with-block — child sees with-var",
    '{% with greeting="Hello" %}{% block msg scoped %}{{ greeting }}{% endblock %}{% endwith %}',
    '{% extends "base.html" %}{% block msg %}{{ greeting }} World{% endblock %}',
    {},
    "Hello World",
)

# ── Scoped block: regular context vars still visible ──
test_extends(
    "scoped block still sees global context",
    "{% block content scoped %}{{ name }}{% endblock %}",
    '{% extends "base.html" %}{% block content %}Hello {{ name }}{% endblock %}',
    {"name": "World"},
    "Hello World",
)

# ── Scoped + required combined ──
test_extends(
    "scoped + required: both keywords work together",
    "{% for item in items %}{% block row scoped required %}{% endblock %},{% endfor %}",
    '{% extends "base.html" %}{% block row %}[{{ item }}]{% endblock %}',
    {"items": ["x", "y"]},
    "[x],[y],",
)

# ── Scoped block with super() ──
test_extends(
    "scoped block with super() — parent content includes loop var",
    "{% for item in items %}{% block row scoped %}({{ item }}){% endblock %},{% endfor %}",
    '{% extends "base.html" %}{% block row %}{{ super() }}!{% endblock %}',
    {"items": ["a", "b"]},
    "(a)!,(b)!,",
)

# ── Multiple scoped blocks in same for-loop ──
test_extends(
    "multiple scoped blocks in for-loop",
    "{% for item in items %}{% block left scoped %}L{{ item }}{% endblock %}{% block right scoped %}R{{ item }}{% endblock %},{% endfor %}",
    '{% extends "base.html" %}{% block left %}[{{ item }}]{% endblock %}{% block right %}({{ item }}){% endblock %}',
    {"items": ["a", "b"]},
    "[a](a),[b](b),",
)

# ── Nested for-loops with scoped block ──
test_extends(
    "scoped block in nested for-loop",
    "{% for row in rows %}{% for col in cols %}{% block cell scoped %}{{ row }}.{{ col }}{% endblock %} {% endfor %};{% endfor %}",
    '{% extends "base.html" %}{% block cell %}[{{ row }}-{{ col }}]{% endblock %}',
    {"rows": [1, 2], "cols": ["a", "b"]},
    "[1-a] [1-b] ;[2-a] [2-b] ;",
)

# ── Keyword parsing: block name doesn't include "scoped" ──
test_extends(
    "scoped keyword stripped from block name",
    "{% block sidebar scoped %}DEFAULT{% endblock %}",
    '{% extends "base.html" %}{% block sidebar %}CHILD{% endblock %}',
    {},
    "CHILD",
)

# ── Performance ──
print("\n── Performance ──")
with tempfile.TemporaryDirectory() as tmpdir:
    tmppath = Path(tmpdir)
    (tmppath / "base.html").write_text(
        "{% for i in items %}{% block row scoped %}{{ i }}{% endblock %},{% endfor %}"
    )
    (tmppath / "child.html").write_text(
        '{% extends "base.html" %}{% block row %}[{{ i }}]{% endblock %}'
    )

    engine = TemplateEngine()
    engine.template_dir = tmpdir
    ctx = {"items": list(range(20))}

    for _ in range(100):
        engine.render("child.html", ctx)

    start = time.perf_counter_ns()
    N = 3000
    for _ in range(N):
        engine.render("child.html", ctx)
    elapsed = time.perf_counter_ns() - start
    print(
        f"  scoped block in 20-item loop: {elapsed / N:.0f} ns/render ({N} iterations)"
    )

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
