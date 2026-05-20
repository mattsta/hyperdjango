"""Tests for {% include "file.html" with context %} / {% include "file.html" without context %}."""

# hyper-test: unit

import sys
import tempfile
import time
from pathlib import Path

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test_include(
    name: str, main_src: str, partials: dict[str, str], context: dict, expected: str
) -> None:
    """Test include with file-based templates."""
    global passed, failed
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_p = Path(tmpdir)
        (tmpdir_p / "main.html").write_text(main_src)
        for fname, src in partials.items():
            (tmpdir_p / fname).write_text(src)

        engine = TemplateEngine()
        engine.template_dir = tmpdir
        try:
            result = engine.render("main.html", context)
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
print("TEST: {% include ... with/without context %}")
print("=" * 60)

# ── Default (with context): included template sees parent vars ──
test_include(
    "default include sees parent context",
    '{% include "partial.html" %}',
    {"partial.html": "Hello {{ name }}!"},
    {"name": "World"},
    "Hello World!",
)

# ── Explicit "with context": same as default ──
test_include(
    "explicit 'with context' sees parent variables",
    '{% include "partial.html" with context %}',
    {"partial.html": "Hello {{ name }}!"},
    {"name": "World"},
    "Hello World!",
)

# ── "without context": included template does NOT see parent vars ──
test_include(
    "without context — parent var not visible",
    '{% include "partial.html" without context %}',
    {"partial.html": "Hello {{ name }}!"},
    {"name": "World"},
    "Hello !",
)

# ── "without context": global template vars also hidden ──
test_include(
    "without context — empty output for missing var",
    '{% include "partial.html" without context %}',
    {"partial.html": "{{ x }}{{ y }}{{ z }}"},
    {"x": "A", "y": "B", "z": "C"},
    "",
)

# ── "without context": static text still renders ──
test_include(
    "without context — static text renders fine",
    '{% include "partial.html" without context %}',
    {"partial.html": "Static text here"},
    {"name": "ignored"},
    "Static text here",
)

# ── Mixed: some includes with, some without ──
test_include(
    "mixed with/without in same template",
    'A:{% include "a.html" with context %} B:{% include "b.html" without context %}',
    {"a.html": "{{ x }}", "b.html": "{{ x }}"},
    {"x": "VAL"},
    "A:VAL B:",
)

# ── "without context" + "ignore missing" combined ──
test_include(
    "without context + ignore missing — missing file skipped",
    '{% include "missing.html" without context ignore missing %}OK',
    {},
    {},
    "OK",
)

# ── "with context" + "ignore missing" combined ──
test_include(
    "with context + ignore missing — missing file skipped",
    '{% include "missing.html" with context ignore missing %}OK',
    {},
    {},
    "OK",
)

# ── Dynamic variable include without context ──
test_include(
    "dynamic include without context",
    "{% include tpl without context %}",
    {"partial.html": "{{ name }}STATIC"},
    {"tpl": "partial.html", "name": "Hidden"},
    "STATIC",
)

# ── Dynamic include with context (default) ──
test_include(
    "dynamic include with context",
    "{% include tpl with context %}",
    {"partial.html": "{{ name }}"},
    {"tpl": "partial.html", "name": "Visible"},
    "Visible",
)

# ── Fallback list without context ──
test_include(
    "fallback list without context",
    '{% include ["missing.html", "fallback.html"] without context %}',
    {"fallback.html": "{{ name }}FB"},
    {"name": "Hidden"},
    "FB",
)

# ── Fallback list with context ──
test_include(
    "fallback list with context",
    '{% include ["missing.html", "fallback.html"] with context %}',
    {"fallback.html": "{{ name }}FB"},
    {"name": "Visible"},
    "VisibleFB",
)

# ── Without context: included template's own logic works ──
test_include(
    "without context — conditionals still work",
    '{% include "partial.html" without context %}',
    {"partial.html": "{% if True %}yes{% else %}no{% endif %}"},
    {"x": "ignored"},
    "yes",
)

# ── Without context: for-loop with inline data works ──
test_include(
    "without context — for-loop with literal data",
    '{% include "partial.html" without context %}',
    {"partial.html": "{% for i in [1,2,3] %}{{ i }}{% endfor %}"},
    {"items": [4, 5, 6]},
    "123",
)

# ── Nested includes: outer without, inner with ──
test_include(
    "nested: outer without context, inner with context",
    '{% include "outer.html" without context %}',
    {
        "outer.html": 'OUTER:{{ name }}{% include "inner.html" %}',
        "inner.html": "INNER:{{ name }}",
    },
    {"name": "World"},
    "OUTER:INNER:",
)

# ── Extends + include without context ──
test_include(
    "extends parent uses include without context",
    '{% extends "base.html" %}{% block content %}CHILD{% endblock %}',
    {
        "base.html": '{% block content %}{% endblock %}|{% include "partial.html" without context %}',
        "partial.html": "{{ name }}",
    },
    {"name": "Hidden"},
    "CHILD|",
)

# ── Performance ──
print("\n── Performance ──")
with tempfile.TemporaryDirectory() as tmpdir:
    tmpdir_p = Path(tmpdir)
    (tmpdir_p / "main.html").write_text('{% include "partial.html" without context %}')
    (tmpdir_p / "partial.html").write_text("Hello {{ name }}!")

    engine = TemplateEngine()
    engine.template_dir = tmpdir

    # Warmup
    for _ in range(100):
        engine.render("main.html", {"name": "World"})

    start = time.perf_counter_ns()
    N = 5000
    for _ in range(N):
        engine.render("main.html", {"name": "World"})
    elapsed = time.perf_counter_ns() - start
    print(f"  include without context: {elapsed / N:.0f} ns/render ({N} iterations)")

    # Compare with 'with context'
    (tmpdir_p / "main.html").write_text('{% include "partial.html" with context %}')

    engine2 = TemplateEngine()
    engine2.template_dir = tmpdir

    for _ in range(100):
        engine2.render("main.html", {"name": "World"})

    start = time.perf_counter_ns()
    for _ in range(N):
        engine2.render("main.html", {"name": "World"})
    elapsed2 = time.perf_counter_ns() - start
    print(f"  include with context:    {elapsed2 / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
