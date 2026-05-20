# hyper-test: unit
"""Round-14 template XSS / auto-escape hardening regression tests.

Every vector here was a confirmed (or plausible) cross-site-scripting hole in the
Zig template engine's variable rendering. Each test renders a hostile value and
asserts the dangerous bytes come out ESCAPED (or the injection is otherwise
neutralised), while a paired legitimate case confirms we did not over-escape.

Bugs covered (see fix-wave report):
  #1 |tojson emitted stock json.dumps → `</script>` breakout in <script>.
  #2 |urlize www./email branches embedded the domain/word into href+text with
     NO escaping → `www.a"onmouseover=...` broke out of the attribute.
  #3 |list was marked "safe" → element HTML emitted raw.
  #4 every func_call result was marked "safe" → `{{ f(x) }}` global returning
     user data emitted raw.
  #5 a var path whose first part merely CONTAINED '(' was marked "safe".
  #6 |xmlattr wrote dict KEYS verbatim → `{'x onclick=alert(1)': ...}` injected
     an attribute.
  #7 |linebreaks (Python filter) produced <br> that the native re-escaped to a
     literal `&lt;br&gt;`.
  #8 mark_safe / SafeString / markupsafe.Markup (__html__ protocol) values were
     re-escaped instead of honored.

This test needs the native engine (the gate builds it before running). The
linebreaks vector also needs the Django-compat filters registered.
"""

import sys

from hyperdjango.templating import TemplateEngine

try:
    from hyperdjango.serving.template_compat import register_django_compat
except Exception:  # pragma: no cover - compat module optional
    register_django_compat = None

passed = 0
failed = 0
errors: list[str] = []


def check(name, template, context, *, must_contain=(), must_not_contain=(), setup=None):
    """Render `template` and assert substring presence / absence."""
    global passed, failed
    engine = TemplateEngine()
    if setup is not None:
        setup(engine)
    try:
        result = engine.render_string(template, context)
    except Exception as e:  # noqa: BLE001 - test harness reports any failure
        print(f"  ERROR: {name}: {e}")
        failed += 1
        errors.append(name)
        return

    problems = []
    for frag in must_contain:
        if frag not in result:
            problems.append(f"expected to CONTAIN {frag!r}")
    for frag in must_not_contain:
        if frag in result:
            problems.append(f"expected NOT to contain {frag!r}")

    if problems:
        print(f"  FAIL: {name}")
        print(f"    Rendered: {result!r}")
        for p in problems:
            print(f"    - {p}")
        failed += 1
        errors.append(name)
    else:
        print(f"  PASS: {name}")
        passed += 1


class _Safe(str):
    """Already-safe HTML string implementing the __html__ protocol."""

    def __html__(self):
        return str(self)


print("=" * 68)
print("TEST: template XSS / auto-escape hardening (r14)")
print("=" * 68)

# ── #1 |tojson must emit HTML-safe JSON (\\u003c etc.), not raw </script> ──
check(
    "#1 tojson escapes </script> breakout",
    "<script>var d = {{ data|tojson }};</script>",
    {"data": "</script><img src=x onerror=alert(1)>"},
    must_contain=["\\u003c"],
    must_not_contain=["</script><img", "<img src=x"],
)
check(
    "#1 tojson escapes & and '",
    "{{ data|tojson }}",
    {"data": "a&b'c"},
    must_contain=["\\u0026", "\\u0027"],
)
check(
    "#1 tojson still produces usable JSON for plain data",
    "{{ data|tojson }}",
    {"data": {"n": 1}},
    must_contain=['"n"', "1"],
)

# ── #2 |urlize must escape www. domain (attribute breakout) ──
check(
    "#2 urlize www. escapes attribute-breakout quote",
    "{{ text|urlize }}",
    {"text": 'www.a"onmouseover="alert(1)'},
    must_contain=["&quot;"],
    must_not_contain=['a"onmouseover', '"onmouseover='],
)
check(
    "#2 urlize email escapes attribute-breakout quote",
    "{{ text|urlize }}",
    {"text": 'a@b.com"onmouseover="alert(1)'},
    must_not_contain=['com"onmouseover'],
)
check(
    "#2 urlize http(s) branch still linkifies + escapes",
    "{{ text|urlize }}",
    {"text": "visit https://ex.com/a&b now"},
    must_contain=['href="https://ex.com/a&amp;b"', "</a>"],
)

# ── #3 |list output must be HTML-escaped ──
check(
    "#3 list escapes element HTML",
    "{{ items|list }}",
    {"items": ["<script>alert(1)</script>"]},
    must_contain=["&lt;script&gt;"],
    must_not_contain=["<script>"],
)


# ── #4 a plain global / callable returning user HTML must be escaped ──
def _reg_global(engine):
    engine.add_global("shout", lambda s: "<b>" + s + "</b>")


check(
    "#4 global call result is escaped (not marked safe)",
    "{{ shout(msg) }}",
    {"msg": "<script>alert(1)</script>"},
    must_contain=["&lt;b&gt;", "&lt;script&gt;"],
    must_not_contain=["<script>", "<b>"],
    setup=_reg_global,
)

# ── #5 subscript key containing '(' must not disable escaping ──
check(
    "#5 var path with '(' in a key is still escaped",
    "{{ data['a(b'] }}",
    {"data": {"a(b": "<script>alert(1)</script>"}},
    must_contain=["&lt;script&gt;"],
    must_not_contain=["<script>"],
)

# ── #6 |xmlattr must reject unsafe keys, keep safe ones ──
check(
    "#6 xmlattr rejects attribute-injecting key, keeps valid keys",
    "<div {{ attrs|xmlattr }}></div>",
    {"attrs": {"x onclick=alert(1)": "y", "class": "btn"}},
    must_contain=['class="btn"'],
    must_not_contain=["onclick=alert", "x onclick"],
)
check(
    "#6 xmlattr still escapes valid values",
    "<div {{ attrs|xmlattr }}></div>",
    {"attrs": {"title": '"><script>'}},
    must_contain=["&quot;", "&lt;script&gt;"],
    must_not_contain=["<script>"],
)

# ── #7 |linebreaks emits real <br>, not literal &lt;br&gt; ──
if register_django_compat is not None:
    check(
        "#7 linebreaks yields real <br> (not re-escaped)",
        "{{ text|linebreaks }}",
        {"text": "a\nb"},
        must_contain=["a<br>b"],
        must_not_contain=["&lt;br&gt;"],
        setup=register_django_compat,
    )
    check(
        "#7 linebreaks still escapes hostile content",
        "{{ text|linebreaks }}",
        {"text": "<script>alert(1)</script>\nx"},
        must_contain=["&lt;script&gt;", "<br>"],
        must_not_contain=["<script>"],
        setup=register_django_compat,
    )
else:
    print("  SKIP: #7 linebreaks (register_django_compat unavailable)")

# ── #8 __html__ protocol (mark_safe / SafeString / Markup) is honored ──
check(
    "#8 __html__ value is NOT re-escaped",
    "{{ safe_val }}",
    {"safe_val": _Safe("<b>ok</b>")},
    must_contain=["<b>ok</b>"],
    must_not_contain=["&lt;b&gt;"],
)
check(
    "#8 plain str is still escaped (no false-positive safe)",
    "{{ plain }}",
    {"plain": "<b>ok</b>"},
    must_contain=["&lt;b&gt;ok&lt;/b&gt;"],
    must_not_contain=["<b>ok</b>"],
)

# ── legitimate: a macro's HTML output must NOT be double-escaped ──
check(
    "macro HTML output is not double-escaped (arg still escaped)",
    "{% macro greet(n) %}<b>{{ n }}</b>{% endmacro %}{{ greet(name) }}",
    {"name": "<i>x</i>"},
    must_contain=["<b>", "</b>", "&lt;i&gt;x&lt;/i&gt;"],
    must_not_contain=["&lt;b&gt;", "<i>x</i>"],
)

print(f"\n{'=' * 68}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 68}")
sys.exit(1 if failed else 0)
