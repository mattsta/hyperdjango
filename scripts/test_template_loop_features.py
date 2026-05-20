#!/usr/bin/env python3
"""
Tests for template engine loop.cycle() and loop.changed().

Usage:
    uv run hyper-test template_loop_features
"""

# hyper-test: unit

import sys

from hyperdjango.templating import TemplateEngine

RESULTS = {"passed": 0, "failed": 0, "errors": []}


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
    print("Template Loop Features Tests")
    print("=" * 60)

    engine = TemplateEngine(".")

    # ── loop.cycle() ─────────────────────────────────────────────
    print("\n--- loop.cycle() ---")

    # Basic cycle with 2 values
    tmpl = "{% for item in items %}{{ loop.cycle('odd', 'even') }} {% endfor %}"
    result = engine.render_string(tmpl, {"items": [1, 2, 3, 4, 5]})
    check(
        "cycle 2 values", result.strip() == "odd even odd even odd", f"got {result!r}"
    )

    # Cycle with 3 values
    tmpl2 = "{% for item in items %}{{ loop.cycle('a', 'b', 'c') }},{% endfor %}"
    result2 = engine.render_string(tmpl2, {"items": range(6)})
    check("cycle 3 values", result2 == "a,b,c,a,b,c,", f"got {result2!r}")

    # Cycle with single value
    tmpl3 = "{% for item in items %}{{ loop.cycle('x') }}{% endfor %}"
    result3 = engine.render_string(tmpl3, {"items": [1, 2, 3]})
    check("cycle 1 value", result3 == "xxx", f"got {result3!r}")

    # Cycle in CSS class context
    tmpl4 = "{% for row in rows %}<tr class=\"{{ loop.cycle('row-odd', 'row-even') }}\">{{ row }}</tr>{% endfor %}"
    result4 = engine.render_string(tmpl4, {"rows": ["A", "B", "C"]})
    check(
        "cycle in HTML class",
        'class="row-odd"' in result4 and 'class="row-even"' in result4,
        f"got {result4!r}",
    )

    # Cycle with empty list
    tmpl5 = "{% for item in items %}{{ loop.cycle('a', 'b') }}{% endfor %}"
    result5 = engine.render_string(tmpl5, {"items": []})
    check("cycle empty list", result5.strip() == "", f"got {result5!r}")

    # ── loop.changed() ───────────────────────────────────────────
    print("\n--- loop.changed() ---")

    # Basic changed detection
    tmpl6 = "{% for item in items %}{% if loop.changed(item) %}[{{ item }}]{% endif %}{% endfor %}"
    result6 = engine.render_string(tmpl6, {"items": [1, 1, 2, 2, 3]})
    check("changed detects changes", result6 == "[1][2][3]", f"got {result6!r}")

    # All unique values
    tmpl7 = "{% for item in items %}{% if loop.changed(item) %}{{ item }} {% endif %}{% endfor %}"
    result7 = engine.render_string(tmpl7, {"items": [1, 2, 3]})
    check("changed all unique", result7.strip() == "1 2 3", f"got {result7!r}")

    # All same values
    tmpl8 = "{% for item in items %}{% if loop.changed(item) %}YES{% else %}NO{% endif %}{% endfor %}"
    result8 = engine.render_string(tmpl8, {"items": [5, 5, 5]})
    check("changed all same", result8 == "YESNONO", f"got {result8!r}")

    # ── loop variables still work ────────────────────────────────
    print("\n--- Existing loop vars still work ---")

    tmpl9 = "{% for item in items %}{{ loop.index0 }}:{{ item }} {% endfor %}"
    result9 = engine.render_string(tmpl9, {"items": ["a", "b", "c"]})
    check(
        "loop.index0 works",
        "0:a" in result9 and "1:b" in result9 and "2:c" in result9,
        f"got {result9!r}",
    )

    tmpl10 = "{% for item in items %}{% if loop.first %}FIRST{% endif %}{% if loop.last %}LAST{% endif %}{% endfor %}"
    result10 = engine.render_string(tmpl10, {"items": [1, 2, 3]})
    check(
        "loop.first/last work",
        "FIRST" in result10 and "LAST" in result10,
        f"got {result10!r}",
    )

    tmpl11 = "{% for item in items %}{{ loop.length }}{% endfor %}"
    result11 = engine.render_string(tmpl11, {"items": [1, 2, 3]})
    check("loop.length works", "333" in result11, f"got {result11!r}")

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
