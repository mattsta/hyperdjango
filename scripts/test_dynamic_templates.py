"""Tests for dynamic template extends/include — variable paths resolved at render time.

Covers:
- {% include partial_var %} — variable path
- {% include obj.template_name %} — dot-path variable
- {% include partial_var ignore missing %} — variable with ignore missing
- {% include ["a.html", "b.html"] %} — fallback list with string literals
- {% include ["a.html", "b.html"] ignore missing %} — fallback list, all missing
- {% extends layout_var %} — dynamic parent with block overrides
- {% extends layout_var %} with {{ super() }} — dynamic extends with super
- Dynamic include inside for loop with changing paths
- Dynamic include with undefined variable
- Nested dynamic includes

Usage:
    uv run hyper-test dynamic_templates
"""

# hyper-test: unit

import sys
import tempfile
from pathlib import Path

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


def write_template(tmpdir, name, content):
    """Write a template file to the temp directory."""
    filepath = Path(tmpdir) / name
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)


def main():
    tmpdir = tempfile.mkdtemp()
    engine = TemplateEngine(template_dir=tmpdir)

    print("=" * 60)
    print("Dynamic Template Extends / Include Tests")
    print("=" * 60)

    # ── Dynamic Include: Variable Path ──────────────────────────────────

    print("\n--- {% include variable %} ---")

    # Test 1: Simple variable include
    write_template(tmpdir, "header.html", "<header>Site Header</header>")
    result = engine.render_string(
        "{% include partial_name %}", {"partial_name": "header.html"}
    )
    check(
        "include simple variable",
        result == "<header>Site Header</header>",
        repr(result),
    )

    # Test 2: Variable include with context
    write_template(tmpdir, "greeting.html", "Hello, {{ name }}!")
    result = engine.render_string(
        "{% include tmpl %}", {"tmpl": "greeting.html", "name": "World"}
    )
    check("include variable with context", result == "Hello, World!", repr(result))

    # Test 3: Dot-path variable include
    write_template(tmpdir, "widget.html", "<widget>{{ title }}</widget>")
    result = engine.render_string(
        "{% include config.template %}",
        {"config": {"template": "widget.html"}, "title": "My Widget"},
    )
    check(
        "include dot-path variable",
        result == "<widget>My Widget</widget>",
        repr(result),
    )

    # Test 4: Variable include in for loop
    write_template(tmpdir, "a.html", "[A]")
    write_template(tmpdir, "b.html", "[B]")
    write_template(tmpdir, "c.html", "[C]")
    result = engine.render_string(
        "{% for t in templates %}{% include t %}{% endfor %}",
        {"templates": ["a.html", "b.html", "c.html"]},
    )
    check("include variable in for loop", result == "[A][B][C]", repr(result))

    # Test 5: Undefined variable — renders nothing
    result = engine.render_string("{% include missing_var %}", {})
    check("include undefined variable renders nothing", result == "", repr(result))

    # Test 6: Undefined variable with ignore missing
    result = engine.render_string("{% include missing_var ignore missing %}", {})
    check("include undefined variable ignore missing", result == "", repr(result))

    # Test 7: Variable resolves but file not found, ignore missing
    result = engine.render_string(
        "{% include tmpl ignore missing %}", {"tmpl": "nonexistent.html"}
    )
    check("include file not found ignore missing", result == "", repr(result))

    # Test 8: Variable resolves but file not found, no ignore
    result = engine.render_string("{% include tmpl %}", {"tmpl": "nonexistent.html"})
    check("include file not found no ignore", result == "", repr(result))

    # Test 9: Surrounded by text
    write_template(tmpdir, "nav.html", "<nav>Menu</nav>")
    result = engine.render_string(
        "<body>{% include nav_template %}<main>Content</main></body>",
        {"nav_template": "nav.html"},
    )
    check(
        "include variable surrounded by text",
        result == "<body><nav>Menu</nav><main>Content</main></body>",
        repr(result),
    )

    # Test 10: Inside if block
    write_template(tmpdir, "premium.html", "Premium Content")
    result = engine.render_string(
        "{% if show %}{% include tmpl %}{% endif %}",
        {"show": True, "tmpl": "premium.html"},
    )
    check("include variable inside if", result == "Premium Content", repr(result))

    # Test 11: Python list variable — try each until one works
    write_template(tmpdir, "fallback.html", "Fallback!")
    result = engine.render_string(
        "{% include templates %}",
        {"templates": ["missing1.html", "missing2.html", "fallback.html"]},
    )
    check("include Python list variable fallback", result == "Fallback!", repr(result))

    # Test 12: Multiple dynamic includes
    write_template(tmpdir, "head.html", "<head>HEAD</head>")
    write_template(tmpdir, "foot.html", "<footer>FOOT</footer>")
    result = engine.render_string(
        "{% include h %}{% include f %}",
        {"h": "head.html", "f": "foot.html"},
    )
    check(
        "multiple dynamic includes",
        result == "<head>HEAD</head><footer>FOOT</footer>",
        repr(result),
    )

    # Test 13: Integer variable — not a string, renders nothing
    result = engine.render_string("{% include tmpl %}", {"tmpl": 42})
    check("include integer variable renders nothing", result == "", repr(result))

    # Test 14: Empty string variable
    result = engine.render_string("{% include tmpl %}", {"tmpl": ""})
    check("include empty string variable renders nothing", result == "", repr(result))

    # ── Dynamic Include: Fallback List ──────────────────────────────────

    print("\n--- {% include ['a.html', 'b.html'] %} ---")

    # Test 15: First template exists
    write_template(tmpdir, "primary.html", "Primary")
    write_template(tmpdir, "secondary.html", "Secondary")
    result = engine.render_string(
        '{% include ["primary.html", "secondary.html"] %}', {}
    )
    check("fallback list first exists", result == "Primary", repr(result))

    # Test 16: First missing, falls back to second
    result = engine.render_string(
        '{% include ["missing.html", "secondary.html"] %}', {}
    )
    check("fallback list first missing", result == "Secondary", repr(result))

    # Test 17: All missing — renders nothing
    result = engine.render_string('{% include ["x.html", "y.html", "z.html"] %}', {})
    check("fallback list all missing", result == "", repr(result))

    # Test 18: Fallback list with context
    write_template(tmpdir, "page.html", "Page: {{ title }}")
    result = engine.render_string(
        '{% include ["missing.html", "page.html"] %}', {"title": "Hello"}
    )
    check("fallback list with context", result == "Page: Hello", repr(result))

    # Test 19: Fallback list single quotes
    write_template(tmpdir, "only.html", "Only One")
    result = engine.render_string("{% include ['only.html'] %}", {})
    check("fallback list single quotes", result == "Only One", repr(result))

    # Test 20: Fallback list three templates, only third exists
    write_template(tmpdir, "third.html", "Third")
    result = engine.render_string(
        '{% include ["first.html", "second.html", "third.html"] %}', {}
    )
    check("fallback list third exists", result == "Third", repr(result))

    # Test 21: Fallback list with explicit ignore missing
    result = engine.render_string(
        '{% include ["aa.html", "bb.html"] ignore missing %}', {}
    )
    check("fallback list explicit ignore missing", result == "", repr(result))

    # ── Mixed Static and Dynamic ──────────────────────────────────────

    print("\n--- Mixed static and dynamic includes ---")

    # Test 22: Both static and dynamic
    write_template(tmpdir, "static_part.html", "[STATIC]")
    write_template(tmpdir, "dynamic_part.html", "[DYNAMIC]")
    result = engine.render_string(
        '{% include "static_part.html" %}{% include dyn %}',
        {"dyn": "dynamic_part.html"},
    )
    check(
        "mixed static and dynamic includes", result == "[STATIC][DYNAMIC]", repr(result)
    )

    # Test 23: Dynamic include where included template uses filters
    write_template(tmpdir, "filtered.html", "{{ name|upper }}")
    result = engine.render_string(
        "{% include tmpl %}", {"tmpl": "filtered.html", "name": "hello"}
    )
    check("dynamic include with filters in included", result == "HELLO", repr(result))

    # Test 24: Dynamic include with for loop in included
    write_template(
        tmpdir, "list.html", "{% for item in items %}{{ item }},{% endfor %}"
    )
    result = engine.render_string(
        "{% include tmpl %}", {"tmpl": "list.html", "items": ["a", "b", "c"]}
    )
    check("dynamic include with for in included", result == "a,b,c,", repr(result))

    # Test 25: Dynamic include with if in included
    write_template(
        tmpdir, "conditional.html", "{% if show %}VISIBLE{% else %}HIDDEN{% endif %}"
    )
    result = engine.render_string(
        "{% include tmpl %}", {"tmpl": "conditional.html", "show": True}
    )
    check("dynamic include with if in included", result == "VISIBLE", repr(result))

    # ── Dynamic Extends ──────────────────────────────────────────────

    print("\n--- {% extends variable %} ---")

    # Test 26: Simple dynamic extends
    write_template(
        tmpdir, "base.html", "<html>{% block content %}DEFAULT{% endblock %}</html>"
    )
    result = engine.render_string(
        "{% extends layout %}{% block content %}CHILD{% endblock %}",
        {"layout": "base.html"},
    )
    check("dynamic extends simple", result == "<html>CHILD</html>", repr(result))

    # Test 27: Dynamic extends with context
    write_template(
        tmpdir,
        "base_titled.html",
        "<title>{{ title }}</title>{% block body %}{% endblock %}",
    )
    result = engine.render_string(
        "{% extends layout %}{% block body %}Body Content{% endblock %}",
        {"layout": "base_titled.html", "title": "My Site"},
    )
    check(
        "dynamic extends with context",
        result == "<title>My Site</title>Body Content",
        repr(result),
    )

    # Test 28: Different parents based on context
    write_template(
        tmpdir, "desktop.html", "<desktop>{% block content %}{% endblock %}</desktop>"
    )
    write_template(
        tmpdir, "mobile.html", "<mobile>{% block content %}{% endblock %}</mobile>"
    )
    child_src = "{% extends layout %}{% block content %}Hello{% endblock %}"

    result_desktop = engine.render_string(child_src, {"layout": "desktop.html"})
    check(
        "dynamic extends desktop parent",
        result_desktop == "<desktop>Hello</desktop>",
        repr(result_desktop),
    )

    result_mobile = engine.render_string(child_src, {"layout": "mobile.html"})
    check(
        "dynamic extends mobile parent",
        result_mobile == "<mobile>Hello</mobile>",
        repr(result_mobile),
    )

    # Test 29: Dynamic extends with super()
    write_template(
        tmpdir, "base_sidebar.html", "{% block sidebar %}BASE SIDEBAR{% endblock %}"
    )
    result = engine.render_string(
        "{% extends layout %}{% block sidebar %}{{ super() }} + CHILD{% endblock %}",
        {"layout": "base_sidebar.html"},
    )
    check(
        "dynamic extends with super()", result == "BASE SIDEBAR + CHILD", repr(result)
    )

    # Test 30: Dynamic extends with undefined variable — falls through to child nodes
    result = engine.render_string(
        "{% extends layout %}{% block content %}Orphan{% endblock %}", {}
    )
    check("dynamic extends undefined variable", "Orphan" in result, repr(result))

    # Test 31: Dynamic extends multiple blocks
    write_template(
        tmpdir,
        "base_multi.html",
        "{% block header %}H{% endblock %}-{% block body %}B{% endblock %}-{% block footer %}F{% endblock %}",
    )
    result = engine.render_string(
        "{% extends layout %}{% block header %}HEAD{% endblock %}{% block footer %}FOOT{% endblock %}",
        {"layout": "base_multi.html"},
    )
    check("dynamic extends multiple blocks", result == "HEAD-B-FOOT", repr(result))

    # Test 32: Dynamic extends dot-path
    write_template(tmpdir, "layouts/main.html", "[{% block main %}{% endblock %}]")
    result = engine.render_string(
        "{% extends config.layout %}{% block main %}Inside{% endblock %}",
        {"config": {"layout": "layouts/main.html"}},
    )
    check("dynamic extends dot-path", result == "[Inside]", repr(result))

    # Test 33: Dynamic extends preserves non-block parent content
    write_template(
        tmpdir, "base_wrap.html", "Header{% block content %}{% endblock %}Footer"
    )
    result = engine.render_string(
        "{% extends layout %}{% block content %}BODY{% endblock %}",
        {"layout": "base_wrap.html"},
    )
    check(
        "dynamic extends preserves non-block content",
        result == "HeaderBODYFooter",
        repr(result),
    )

    # ── Static Include: ignore missing ────────────────────────────────

    print("\n--- Static include ignore missing ---")

    # Test 34: Static include ignore missing — file not found
    result = engine.render_string(
        'Before{% include "nonexistent.html" ignore missing %}After', {}
    )
    check("static include ignore missing", result == "BeforeAfter", repr(result))

    # Test 35: Static include ignore missing — file exists
    write_template(tmpdir, "exists.html", "EXISTS")
    result = engine.render_string('{% include "exists.html" ignore missing %}', {})
    check("static include ignore missing file exists", result == "EXISTS", repr(result))

    # ── Summary ──────────────────────────────────────────────────────

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
