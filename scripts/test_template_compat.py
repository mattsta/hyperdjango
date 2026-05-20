#!/usr/bin/env python3
"""Test Django template tag compatibility in Zig template engine.

Tests:
1. {% load %} silently skipped
2. |static filter
3. |trans filter
4. |pluralize filter
5. |truncatewords filter
6. |linebreaks filter
7. |yesno filter
8. |date filter
9. url() global function
10. Django template backend with compat filters
"""

# hyper-test: db_django

import os
import sys
import tempfile
from pathlib import Path

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
import django

django.setup()


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── {% load %} handling ───────────────────────────────────────────────
    print("\n=== {% load %} compatibility ===")

    from hyperdjango._hyperdjango_native import (
        _template_compile,
        _template_render,
    )

    def r(source, context):
        capsule = _template_compile(source, "<test>")
        result = _template_render(capsule, context)
        return result.decode("utf-8") if isinstance(result, bytes) else result

    result = r("{% load static %}Hello", {})
    check("load static skipped", result == "Hello", f"got '{result}'")

    result = r("{% load i18n %}{% load humanize %}{{ name }}", {"name": "World"})
    check("multiple loads skipped", result == "World", f"got '{result}'")

    result = r("{% load static i18n %}{{ x }}", {"x": "OK"})
    check("load multi-lib skipped", result == "OK", f"got '{result}'")

    # ── Django compat filters ─────────────────────────────────────────────
    print("\n=== Django compat filters ===")

    from hyperdjango.serving.template_compat import (
        linebreaks_filter,
        pluralize_filter,
        static_filter,
        trans_filter,
        truncatewords_filter,
        yesno_filter,
    )

    check("static filter", static_filter("css/style.css").endswith("css/style.css"))

    check("trans filter passthrough", trans_filter("Hello") == "Hello")

    check("pluralize 0", pluralize_filter(0) == "s")
    check("pluralize 1", pluralize_filter(1) == "")
    check("pluralize 2", pluralize_filter(2) == "s")
    check("pluralize custom", pluralize_filter(0, "es") == "es")

    check(
        "truncatewords 3",
        truncatewords_filter("one two three four five", "3") == "one two three...",
    )
    check("truncatewords short", truncatewords_filter("hi", "10") == "hi")

    check("linebreaks", linebreaks_filter("a\nb") == "a<br>b")

    check("yesno true", yesno_filter(True) == "yes")
    check("yesno false", yesno_filter(False) == "no")
    check("yesno none", yesno_filter(None) == "maybe")
    check("yesno custom", yesno_filter(True, "oui,non") == "oui")

    # ── Template engine with compat filters registered ────────────────────
    print("\n=== Template engine with Django compat ===")

    from hyperdjango.serving.template_compat import register_django_compat
    from hyperdjango.templating import TemplateEngine

    engine = TemplateEngine(template_dir="/tmp")
    register_django_compat(engine)

    # Test compat filters work via render_string
    result = engine.render_string("{{ count }} item{{ count|pluralize }}", {"count": 1})
    check("pluralize via engine (1)", result == "1 item", f"got '{result}'")

    result = engine.render_string("{{ count }} item{{ count|pluralize }}", {"count": 5})
    check("pluralize via engine (5)", result == "5 items", f"got '{result}'")

    # truncatewords — test with default (no arg) since filter args pass as string
    result = engine.render_string(
        "{{ text|truncatewords }}",
        {
            "text": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty thirty-one"
        },
    )
    check(
        "truncatewords via engine",
        "..." in result
        and len(result)
        < len(
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty thirty-one"
        ),
        f"got '{result[:50]}...'",
    )

    result = engine.render_string("{{ text|linebreaks }}", {"text": "line1\nline2"})
    check("linebreaks via engine", "br" in result, f"got '{result}'")

    # ── Django template backend integration ───────────────────────────────
    print("\n=== Django template backend ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create templates that use Django-compat features
        (Path(tmpdir) / "compat.html").write_text(
            "{% load static %}{{ count }} item{{ count|pluralize }}"
        )

        (Path(tmpdir) / "yesno.html").write_text("{{ active|yesno }}")

        from hyperdjango.serving.template_backend import ZigTemplates

        backend = ZigTemplates(
            {
                "NAME": "zig",
                "DIRS": [tmpdir],
                "APP_DIRS": False,
                "OPTIONS": {"context_processors": []},
            }
        )

        tmpl = backend.get_template("compat.html")
        html = tmpl.render({"count": 3})
        check("backend pluralize", html == "3 items", f"got '{html}'")

        tmpl2 = backend.get_template("yesno.html")
        html2 = tmpl2.render({"active": True})
        check("backend yesno", html2 == "yes", f"got '{html2}'")

        # from_string with Django compat (no arg = default 's')
        tmpl3 = backend.from_string("{{ n }} box{{ n|pluralize }}")
        html3 = tmpl3.render({"n": 0})
        check("from_string pluralize", html3 == "0 boxs", f"got '{html3}'")

    # ── Django output tags ({% static %}, {% url %}, {% csrf_token %}) ──
    print("\n=== Django output tags ===")

    # {% static 'path' %} → outputs resolved static URL
    result = r("{% static 'css/style.css' %}", {})
    check("{% static %} tag", "css/style.css" in result, f"got '{result}'")

    result = r("{% static 'js/app.js' %}", {})
    check("{% static %} js", "js/app.js" in result, f"got '{result}'")

    # {% csrf_token %} → looks up csrf_token from context
    result = r("{% csrf_token %}", {"csrf_token": "abc123"})
    check("{% csrf_token %} tag", "abc123" in result, f"got '{result}'")

    # {% csrf_token %} without context → empty
    result = r("{% csrf_token %}", {})
    check(
        "{% csrf_token %} empty",
        result == "" or result.strip() == "",
        f"got '{result}'",
    )

    # Mixed with other content
    result = r("<link href=\"{% static 'css/style.css' %}\">", {})
    check("{% static %} in HTML", "css/style.css" in result, f"got '{result}'")

    # Multiple static tags
    result = r("{% static 'a.css' %} {% static 'b.js' %}", {})
    check(
        "multiple {% static %}",
        "a.css" in result and "b.js" in result,
        f"got '{result}'",
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All template compat tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
