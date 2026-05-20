#!/usr/bin/env python3
"""
Tests for {% extends %}, {% import %}, {% from %} in the Zig template engine.

Usage:
    uv run hyper-test template_extends_import
"""

# hyper-test: unit

import sys
from pathlib import Path

from hyperdjango.templating import TemplateEngine

RESULTS = {"passed": 0, "failed": 0, "errors": []}
TEMPLATE_DIR = str(Path(__file__).resolve().parent.parent / "tests" / "templates")


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("Template Extends / Import / From Tests")
    print("=" * 60)

    engine = TemplateEngine(template_dir=TEMPLATE_DIR)

    # ── {% extends %} ────────────────────────────────────────────
    print("\n--- {% extends %} ---")

    # Test 1: Base template renders on its own
    base_html = engine.render("base.html", {})
    check("base template renders", "Default Title" in base_html)
    check("base has header", "Base Header" in base_html)
    check("base has footer", "Base Footer" in base_html)
    check("base has default content", "Default Content" in base_html)

    # Test 2: Child template extends base
    child_html = engine.render("child.html", {"name": "World"})
    check("child extends base", "<html>" in child_html, f"got {child_html[:200]!r}")
    check("child overrides title", "Child Page" in child_html, f"got {child_html!r}")
    check(
        "child overrides content", "Hello World!" in child_html, f"got {child_html!r}"
    )
    check("child inherits header", "Base Header" in child_html, f"got {child_html!r}")
    check("child inherits footer", "Base Footer" in child_html, f"got {child_html!r}")
    check("child does NOT have default content", "Default Content" not in child_html)

    # ── {% import %} ─────────────────────────────────────────────
    print("\n--- {% import %} ---")

    import_html = engine.render("page_with_import.html", {})
    check("import renders form", "<form>" in import_html, f"got {import_html!r}")
    check(
        "import has username input",
        'name="username"' in import_html,
        f"got {import_html!r}",
    )
    check(
        "import has password input",
        'type="password"' in import_html,
        f"got {import_html!r}",
    )

    # ── {% from %} ───────────────────────────────────────────────
    print("\n--- {% from %} ---")

    from_html = engine.render("page_with_from.html", {})
    check("from renders form", "<form>" in from_html, f"got {from_html!r}")
    check("from has email input", 'type="email"' in from_html, f"got {from_html!r}")
    check("from has name attr", 'name="email"' in from_html, f"got {from_html!r}")

    # ── {{ super() }} ──────────────────────────────────────────────
    print("\n--- {{ super() }} ---")

    super_html = engine.render("child_with_super.html", {})
    check(
        "super() renders parent title",
        "Default Title" in super_html,
        f"got {super_html!r}",
    )
    check("super() renders child prefix", "Child:" in super_html)
    check("super() renders parent content", "Default Content" in super_html)
    check("super() renders child content", "New Content" in super_html)
    check("super() inherits non-overridden blocks", "Base Header" in super_html)
    check("super() preserves HTML structure", "<html>" in super_html)

    # ── Existing templates still work ────────────────────────────
    print("\n--- Regression check ---")

    simple = engine.render_string("Hello {{ name }}!", {"name": "Test"})
    check("simple render works", simple == "Hello Test!")

    loop = engine.render_string(
        "{% for i in items %}{{ i }}{% endfor %}", {"items": [1, 2, 3]}
    )
    check("loop still works", loop == "123")

    macro = engine.render_string(
        "{% macro greet(name) %}Hi {{ name }}!{% endmacro %}{{ greet('Alice') }}", {}
    )
    check("inline macro still works", "Hi Alice!" in macro)

    # Macro with keyword args
    kwarg_macro = engine.render_string(
        "{% macro btn(label, type='button') %}<button type=\"{{ type }}\">{{ label }}</button>{% endmacro %}{{ btn('Click', type='submit') }}",
        {},
    )
    check(
        "keyword args in macro work",
        'type="submit"' in kwarg_macro,
        f"got {kwarg_macro!r}",
    )

    # Summary
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
