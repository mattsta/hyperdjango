"""Memory leak stress test for template create/destroy lifecycle.

Creates and destroys templates with extends, includes, imports, dynamic
extends, and SIMD filters in a tight loop. Monitors RSS to detect leaks.

A leak would show RSS growing linearly with iterations.
Correct behavior: RSS stabilizes after initial warmup.

Usage:
    uv run hyper-test memory_template_lifecycle
"""

# hyper-test: unit

import gc
import sys
import tempfile
from pathlib import Path

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def get_rss_mb():
    """Get current RSS in MB (macOS/Linux)."""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
            1024 * 1024
        )  # macOS returns bytes
    except ImportError:
        return 0


def main():
    print("=" * 60)
    print("Template Memory Lifecycle Stress Test")
    print("=" * 60)

    from hyperdjango.templating import TemplateEngine

    tmpdir = tempfile.mkdtemp()

    # Write template files for extends/include/import testing
    tmppath = Path(tmpdir)
    (tmppath / "base.html").write_text(
        "<!DOCTYPE html><html>{% block title %}Default Title{% endblock %}"
        "{% block nav %}<nav>Nav</nav>{% endblock %}"
        "{% block body %}Default Body{% endblock %}"
        "{% block footer %}<footer>Footer</footer>{% endblock %}</html>"
    )

    (tmppath / "sidebar.html").write_text(
        "<aside>{% for item in items %}<div>{{ item }}</div>{% endfor %}</aside>"
    )

    (tmppath / "macros.html").write_text(
        "{% macro input_field(name, type) %}"
        '<input name="{{ name }}" type="{{ type|default(\'text\') }}">'
        "{% endmacro %}"
        "{% macro form_row(label, name) %}"
        '<div class="row"><label>{{ label }}</label>{{ input_field(name) }}</div>'
        "{% endmacro %}"
    )

    (tmppath / "card.html").write_text(
        '<div class="card">{{ content|striptags|truncate(50) }}</div>'
    )

    ITERATIONS = 500

    # ── Test 1: Static extends create/destroy cycle ──────────────

    print("\n--- Static Extends Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        result = engine.render_string(
            '{% extends "base.html" %}{% block body %}Body {{ i }}{% endblock %}',
            {"i": i},
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check(
        "static extends no major leak",
        growth < 50,
        f"grew {growth:.1f} MB over {ITERATIONS} iterations",
    )

    # ── Test 2: Static include create/destroy cycle ──────────────

    print("\n--- Static Include Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        result = engine.render_string(
            '{% include "sidebar.html" %}',
            {"items": ["a", "b", "c"]},
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check("static include no major leak", growth < 50, f"grew {growth:.1f} MB")

    # ── Test 3: Import macro create/destroy cycle ────────────────

    print("\n--- Import Macro Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        result = engine.render_string(
            '{% import "macros.html" as m %}{{ m.input_field("email", "email") }}',
            {},
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check("import macro no major leak", growth < 50, f"grew {growth:.1f} MB")

    # ── Test 4: Dynamic extends create/destroy cycle ─────────────

    print("\n--- Dynamic Extends Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        result = engine.render_string(
            "{% extends layout %}{% block body %}Dynamic {{ i }}{% endblock %}",
            {"layout": "base.html", "i": i},
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check("dynamic extends no major leak", growth < 50, f"grew {growth:.1f} MB")

    # ── Test 5: Dynamic include create/destroy cycle ─────────────

    print("\n--- Dynamic Include Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        result = engine.render_string(
            "{% include tmpl %}",
            {"tmpl": "sidebar.html", "items": [1, 2, 3]},
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check("dynamic include no major leak", growth < 50, f"grew {growth:.1f} MB")

    # ── Test 6: SIMD filters create/destroy cycle ────────────────

    print("\n--- SIMD Filter Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        result = engine.render_string(
            "{{ html|striptags|truncate(30) }} {{ text|urlencode }} {{ words|wordcount }}",
            {
                "html": "<p>Hello <b>World</b></p>",
                "text": "a b c",
                "words": "one two three",
            },
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check("simd filters no major leak", growth < 50, f"grew {growth:.1f} MB")

    # ── Test 7: Combined stress (all features together) ──────────

    print("\n--- Combined All-Features Lifecycle ---")
    gc.collect()
    rss_before = get_rss_mb()

    for i in range(ITERATIONS):
        engine = TemplateEngine(template_dir=tmpdir)
        # Static extends + static include
        engine.render_string(
            '{% extends "base.html" %}{% block body %}{% include "sidebar.html" %}{% endblock %}',
            {"items": ["x", "y"]},
        )
        # Dynamic extends + dynamic include + filters
        engine.render_string(
            "{% extends layout %}{% block body %}{% include partial %}{{ h|striptags }}{% endblock %}",
            {
                "layout": "base.html",
                "partial": "card.html",
                "content": "<b>test</b>",
                "h": "<p>hi</p>",
            },
        )
        # Import macros
        engine.render_string(
            '{% from "macros.html" import input_field %}{{ input_field("name") }}',
            {},
        )
        del engine

    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before
    print(
        f"    RSS before: {rss_before:.1f} MB, after: {rss_after:.1f} MB, growth: {growth:.1f} MB"
    )
    check(
        "combined lifecycle no major leak",
        growth < 100,
        f"grew {growth:.1f} MB over {ITERATIONS}x3 renders",
    )

    # ── Cleanup ──────────────────────────────────────────────────

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
