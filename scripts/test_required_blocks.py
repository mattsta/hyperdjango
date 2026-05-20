"""Tests for {% block content required %} in the Zig template engine."""

# hyper-test: unit

import sys
import tempfile
import time
from pathlib import Path

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


def test_error(
    name: str, parent_src: str, child_src: str, expected_fragment: str
) -> None:
    """Test that compiling child template raises an error (required block violation)."""
    global passed, failed
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "base.html").write_text(parent_src)
        (tmpdir_path / "child.html").write_text(child_src)

        engine = TemplateEngine()
        engine.template_dir = tmpdir
        try:
            result = engine.render("child.html", {})
            # If we got here, check if the error message was rendered inline
            if expected_fragment.lower() in result.lower():
                print(f"  PASS: {name} (error in output)")
                passed += 1
            else:
                print(f"  FAIL: {name} — expected error but got: {result[:100]!r}")
                failed += 1
                errors.append(name)
        except Exception as e:
            # The Zig engine raises RuntimeError with "Required block 'X' not overridden"
            err_str = str(e).lower()
            if expected_fragment.lower() in err_str:
                print(f"  PASS: {name} (exception: {type(e).__name__}: {e})")
                passed += 1
            else:
                print(f"  FAIL: {name} — wrong error: {type(e).__name__}: {e}")
                failed += 1
                errors.append(name)


def test_extends(
    name: str, parent_src: str, child_src: str, context: dict, expected: str
) -> None:
    """Test a child extending a parent with file-based templates."""
    global passed, failed
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "base.html").write_text(parent_src)
        (tmpdir_path / "child.html").write_text(child_src)

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
print("TEST: Required blocks — {% block content required %}")
print("=" * 60)

# ── Basic required block: child overrides → OK ──
test_extends(
    "required block overridden — renders child content",
    "<html>{% block content required %}{% endblock %}</html>",
    '{% extends "base.html" %}{% block content %}Hello{% endblock %}',
    {},
    "<html>Hello</html>",
)

# ── Required block NOT overridden → error ──
test_error(
    "required block not overridden — raises error",
    "<html>{% block content required %}{% endblock %}</html>",
    '{% extends "base.html" %}',
    "required block",
)

# ── Multiple blocks: one required, one optional ──
test_extends(
    "required + optional: required overridden, optional left default",
    "<html>{% block title %}Default Title{% endblock %} {% block content required %}{% endblock %}</html>",
    '{% extends "base.html" %}{% block content %}Body{% endblock %}',
    {},
    "<html>Default Title Body</html>",
)

# ── Multiple required blocks: both overridden → OK ──
test_extends(
    "two required blocks both overridden",
    "{% block header required %}{% endblock %}|{% block content required %}{% endblock %}",
    '{% extends "base.html" %}{% block header %}H{% endblock %}{% block content %}C{% endblock %}',
    {},
    "H|C",
)

# ── Multiple required blocks: one missing → error ──
test_error(
    "two required blocks, one not overridden — error",
    "{% block header required %}{% endblock %}|{% block content required %}{% endblock %}",
    '{% extends "base.html" %}{% block header %}H{% endblock %}',
    "required block",
)

# ── Required block with default content ──
test_extends(
    "required block with content — still requires override",
    "{% block content required %}DEFAULT{% endblock %}",
    '{% extends "base.html" %}{% block content %}OVERRIDE{% endblock %}',
    {},
    "OVERRIDE",
)

test_error(
    "required block with content — error if not overridden",
    "{% block content required %}DEFAULT{% endblock %}",
    '{% extends "base.html" %}',
    "required block",
)

# ── Non-required blocks work as before ──
test_extends(
    "non-required block uses default when not overridden",
    "{% block content %}Default{% endblock %}",
    '{% extends "base.html" %}',
    {},
    "Default",
)

test_extends(
    "non-required block can be overridden",
    "{% block content %}Default{% endblock %}",
    '{% extends "base.html" %}{% block content %}New{% endblock %}',
    {},
    "New",
)

# ── Required block with super() ──
test_extends(
    "required block with super() in child",
    "{% block content required %}Base{% endblock %}",
    '{% extends "base.html" %}{% block content %}{{ super() }}+Child{% endblock %}',
    {},
    "Base+Child",
)

# ── Dynamic extends with required block ──
test_extends(
    "dynamic extends respects required blocks",
    "{% block content required %}{% endblock %}",
    "{% extends parent %}{% block content %}Dynamic{% endblock %}",
    {"parent": "base.html"},
    "Dynamic",
)


# ── Three-level inheritance: grandchild overrides required ──
def test_three_level():
    global passed, failed
    name = "three-level inheritance — grandchild overrides required"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "base.html").write_text(
            "{% block content required %}{% endblock %}"
        )
        (tmpdir_path / "mid.html").write_text(
            '{% extends "base.html" %}{% block content %}Mid{% endblock %}'
        )
        (tmpdir_path / "child.html").write_text(
            '{% extends "mid.html" %}{% block content %}Child{% endblock %}'
        )

        engine = TemplateEngine()
        engine.template_dir = tmpdir
        try:
            result = engine.render("child.html", {})
            if result.strip() == "Child":
                print(f"  PASS: {name}")
                passed += 1
            else:
                print(f"  FAIL: {name} — got: {result!r}")
                failed += 1
                errors.append(name)
        except Exception as e:
            print(f"  ERROR: {name}: {e}")
            failed += 1
            errors.append(name)


test_three_level()

# ── Required block parsing doesn't break block_name ──
test_extends(
    "required keyword stripped from block name",
    "{% block sidebar required %}{% endblock %}|{% block content %}default{% endblock %}",
    '{% extends "base.html" %}{% block sidebar %}Sidebar{% endblock %}',
    {},
    "Sidebar|default",
)

# ── Performance: required block check is compile-time, no render overhead ──
print("\n── Performance ──")
with tempfile.TemporaryDirectory() as tmpdir:
    tmpdir_path = Path(tmpdir)
    (tmpdir_path / "base.html").write_text(
        "{% block a required %}{% endblock %}{% block b %}B{% endblock %}{% block c required %}{% endblock %}"
    )
    (tmpdir_path / "child.html").write_text(
        '{% extends "base.html" %}{% block a %}A{% endblock %}{% block c %}C{% endblock %}'
    )

    engine = TemplateEngine()
    engine.template_dir = tmpdir

    # Warmup
    for _ in range(100):
        engine.render("child.html", {})

    start = time.perf_counter_ns()
    N = 5000
    for _ in range(N):
        engine.render("child.html", {})
    elapsed_ns = time.perf_counter_ns() - start
    per_render = elapsed_ns / N
    print(f"  required blocks render: {per_render:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
