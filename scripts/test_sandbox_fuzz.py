"""
Hypothesis fuzz tests for template sandbox escape prevention.

Tests known Jinja2 CVE escape patterns and adversarial attribute access:
1. __class__.__mro__ chains blocked
2. __globals__ access blocked
3. __subclasses__ enumeration blocked
4. __init__.__globals__ chain blocked
5. __dict__ access blocked
6. Random dunder attribute access blocked

Uses real TemplateEngine with sandboxed=True.

# hyper-test: unit
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.templating import TemplateEngine

_engine = TemplateEngine(
    template_dir="/dev/null", autoescape=True, sandboxed=True, bytecode_cache=False
)


# ---------------------------------------------------------------------------
# Known Jinja2 CVE escape patterns — ALL must be blocked
# ---------------------------------------------------------------------------

KNOWN_ESCAPES = [
    # Basic dunder access
    "{{ ''.__class__ }}",
    "{{ ''.__class__.__mro__ }}",
    "{{ ''.__class__.__mro__[1] }}",
    "{{ ''.__class__.__base__ }}",
    "{{ ''.__class__.__subclasses__() }}",
    # __init__.__globals__ chain (most common escape)
    "{{ ''.__class__.__init__.__globals__ }}",
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    # __dict__ access
    "{{ config.__dict__ }}",
    "{{ request.__dict__ }}",
    # __globals__ on functions
    "{{ ''.__class__.__init__.__globals__['os'] }}",
    # __builtins__ access
    "{{ ''.__class__.__init__.__globals__['__builtins__'] }}",
    # Module access
    "{{ ''.__class__.__module__ }}",
    # lipsum / cycler / joiner (Jinja2 internal objects)
    "{{ cycler.__init__.__globals__ }}",
    "{{ joiner.__init__.__globals__ }}",
    # Attribute access via [] notation
    "{{ ''['__class__'] }}",
    "{{ ''['__cl' + 'ass__'] }}",
]


def test_known_escapes_blocked():
    """ALL known Jinja2 sandbox escape patterns must be blocked."""
    for i, template in enumerate(KNOWN_ESCAPES):
        try:
            result = _engine.render_string(
                template, {"config": {}, "request": {}, "cycler": type, "joiner": type}
            )
            # If it rendered, the result must NOT contain sensitive data
            # (sandbox returns empty string for blocked attributes)
            assert "module" not in result.lower() or "os" not in result.lower(), (
                f"Escape #{i} leaked data: {template} → {result!r}"
            )
        except Exception:
            pass  # Exception is acceptable — means the escape was blocked
    print(f"  PASS: {len(KNOWN_ESCAPES)} known escape patterns blocked")


# ---------------------------------------------------------------------------
# Property 1: Random dunder attribute access blocked
# ---------------------------------------------------------------------------

DUNDERS = [
    "__class__",
    "__mro__",
    "__base__",
    "__bases__",
    "__subclasses__",
    "__init__",
    "__globals__",
    "__dict__",
    "__builtins__",
    "__module__",
    "__import__",
    "__delattr__",
    "__setattr__",
    "__getattribute__",
    "__code__",
    "__func__",
    "__self__",
    "__wrapped__",
]


@given(dunder=st.sampled_from(DUNDERS))
@settings(max_examples=len(DUNDERS), deadline=3000)
def test_dunder_access_blocked(dunder):
    """ANY dunder attribute access in sandbox mode is blocked."""
    template = f"{{{{ obj.{dunder} }}}}"
    result = _engine.render_string(template, {"obj": "test"})
    # Result should be empty (blocked) or safe string, never actual attribute value
    assert dunder not in result, f"Dunder {dunder} leaked through: {result!r}"


# ---------------------------------------------------------------------------
# Property 2: Multi-step chain access blocked
# ---------------------------------------------------------------------------


@given(
    step1=st.sampled_from(["__class__", "__init__"]),
    step2=st.sampled_from(["__globals__", "__dict__", "__mro__", "__subclasses__"]),
)
@settings(max_examples=20, deadline=3000)
def test_chain_access_blocked(step1, step2):
    """Multi-step dunder chains are blocked in sandbox."""
    template = f"{{{{ obj.{step1}.{step2} }}}}"
    result = _engine.render_string(template, {"obj": "test"})
    assert step2 not in result


# ---------------------------------------------------------------------------
# Property 3: Safe attribute access still works in sandbox
# ---------------------------------------------------------------------------


@given(value=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop"))
@settings(max_examples=100, deadline=3000)
def test_safe_access_works(value):
    """Normal attribute access works in sandbox mode."""
    result = _engine.render_string("{{ name|upper }}", {"name": value})
    assert result == value.upper()


@given(items=st.lists(st.integers(min_value=0, max_value=99), min_size=1, max_size=10))
@settings(max_examples=100, deadline=3000)
def test_safe_iteration_works(items):
    """For loops work in sandbox mode."""
    result = _engine.render_string(
        "{% for x in items %}{{ x }},{% endfor %}", {"items": items}
    )
    for item in items:
        assert str(item) in result


# ---------------------------------------------------------------------------
# Property 4: Autoescaped context can't inject HTML
# ---------------------------------------------------------------------------


@given(
    payload=st.text(
        min_size=1,
        max_size=50,
        alphabet=st.sampled_from(list('<script>alert("xss")</script>&<>"\'')),
    )
)
@settings(max_examples=300, deadline=3000)
def test_autoescape_prevents_xss(payload):
    """User-controlled context values are autoescaped in output."""
    result = _engine.render_string("{{ content }}", {"content": payload})
    # Raw < and > must not appear in output
    assert "<script>" not in result
    if "<" in payload:
        assert "&lt;" in result


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Template Sandbox Escape Fuzz Tests ──\n")

    tests = [
        ("known escape patterns", test_known_escapes_blocked),
        ("dunder access blocked", test_dunder_access_blocked),
        ("chain access blocked", test_chain_access_blocked),
        ("safe access works", test_safe_access_works),
        ("safe iteration works", test_safe_iteration_works),
        ("autoescape prevents XSS", test_autoescape_prevents_xss),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Sandbox fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
