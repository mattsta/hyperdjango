"""
Hypothesis fuzz tests for Zig native template engine.

Proves correctness properties:
1. render_string(any_safe_template, context) → produces string (no crash)
2. Variable substitution: {{ var }} renders context value
3. Filter chaining: {{ var|filter1|filter2 }} → valid string output
4. Autoescape: HTML content in context → properly escaped in output
5. Expressions: math, comparison, ternary → valid result

# hyper-test: unit
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.templating import TemplateEngine

# ---------------------------------------------------------------------------
# Shared engine
# ---------------------------------------------------------------------------

_engine = TemplateEngine(
    template_dir="/dev/null", autoescape=True, bytecode_cache=False
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Safe template variable names
var_names = st.sampled_from(
    ["name", "age", "title", "count", "active", "score", "email", "x", "y"]
)

# Context values
context_values = st.one_of(
    st.text(max_size=50),
    st.integers(min_value=-10000, max_value=10000),
    st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    st.lists(st.integers(min_value=0, max_value=100), max_size=5),
)

# Safe filters that don't require arguments
safe_filters = st.sampled_from(
    [
        "upper",
        "lower",
        "title",
        "length",
        "trim",
        "string",
        "int",
        "float",
        "capitalize",
        "striptags",
        "urlencode",
        "escape",
        "e",
    ]
)


# ---------------------------------------------------------------------------
# Property 1: render_string with variable substitution → no crash
# ---------------------------------------------------------------------------


@given(
    var=var_names,
    value=context_values,
)
@settings(max_examples=500, deadline=3000)
def test_variable_renders_without_crash(var, value):
    """{{ var }} with ANY context value produces a string (no crash/segfault)."""
    template = f"{{{{ {var} }}}}"
    result = _engine.render_string(template, {var: value})
    assert isinstance(result, str), f"Expected str, got {type(result)}"


# ---------------------------------------------------------------------------
# Property 2: Variable value appears in output
# ---------------------------------------------------------------------------


@given(value=st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop"))
@settings(max_examples=300, deadline=3000)
def test_string_variable_in_output(value):
    """String variable value appears in rendered output."""
    result = _engine.render_string("{{ name }}", {"name": value})
    assert value in result, f"Value {value!r} not in output {result!r}"


@given(value=st.integers(min_value=-1000, max_value=1000))
@settings(max_examples=300, deadline=3000)
def test_int_variable_in_output(value):
    """Integer variable renders as string representation."""
    result = _engine.render_string("{{ x }}", {"x": value})
    assert str(value) in result


# ---------------------------------------------------------------------------
# Property 3: Filter produces string output
# ---------------------------------------------------------------------------


@given(
    var=var_names,
    value=context_values,
    filt=safe_filters,
)
@settings(max_examples=500, deadline=3000)
def test_filter_produces_string(var, value, filt):
    """{{ var|filter }} with ANY value produces a string (no crash)."""
    template = f"{{{{ {var}|{filt} }}}}"
    result = _engine.render_string(template, {var: value})
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Property 4: Autoescape — HTML in context is escaped
# ---------------------------------------------------------------------------


@given(
    html=st.text(
        min_size=1,
        max_size=50,
        alphabet=st.sampled_from(list("<>&\"'abcdef 123")),
    )
)
@settings(max_examples=300, deadline=3000)
def test_autoescape_html(html):
    """HTML special chars in context are escaped in output."""
    result = _engine.render_string("{{ content }}", {"content": html})
    # After autoescape, raw < > & " should not appear (they become &lt; etc)
    if "<" in html:
        assert "<" not in result or "&lt;" in result
    if ">" in html:
        assert ">" not in result or "&gt;" in result


# ---------------------------------------------------------------------------
# Property 5: If/else produces valid output
# ---------------------------------------------------------------------------


@given(
    condition=st.booleans(),
    true_val=st.text(min_size=1, max_size=10, alphabet="abc"),
    false_val=st.text(min_size=1, max_size=10, alphabet="xyz"),
)
@settings(max_examples=200, deadline=3000)
def test_if_else_renders(condition, true_val, false_val):
    """{% if %}/{% else %} renders correct branch."""
    template = f"{{% if cond %}}{true_val}{{% else %}}{false_val}{{% endif %}}"
    result = _engine.render_string(template, {"cond": condition})
    if condition:
        assert true_val in result
    else:
        assert false_val in result


# ---------------------------------------------------------------------------
# Property 6: For loop over list
# ---------------------------------------------------------------------------


@given(items=st.lists(st.integers(min_value=0, max_value=99), min_size=0, max_size=10))
@settings(max_examples=200, deadline=3000)
def test_for_loop_all_items(items):
    """{% for x in items %}{{ x }}{% endfor %} renders all items."""
    template = "{% for x in items %}[{{ x }}]{% endfor %}"
    result = _engine.render_string(template, {"items": items})
    for item in items:
        assert f"[{item}]" in result


# ---------------------------------------------------------------------------
# Property 7: Literal text passthrough
# ---------------------------------------------------------------------------


@given(
    text=st.text(min_size=1, max_size=100, alphabet="abcdefghijklmnop 0123456789.,!?")
)
@settings(max_examples=200, deadline=3000)
def test_literal_passthrough(text):
    """Template with no tags renders literal text unchanged."""
    result = _engine.render_string(text, {})
    assert result == text


# ---------------------------------------------------------------------------
# Property 8: recursion is bounded — no native stack overflow / DoS (#128)
# ---------------------------------------------------------------------------


def test_recursive_macro_does_not_stack_overflow():
    """A self-recursive macro crosses the macro render boundary (where the
    per-tree depth is reset to 0), so ONLY the total render-call cap stops it.
    Must return bounded output, never crash the worker / hang."""
    tmpl = "{% macro f() %}x{{ f() }}{% endmacro %}{{ f() }}"
    result = _engine.render_string(tmpl, {})
    assert isinstance(result, str)
    assert len(result) < 5000  # capped (render_call_depth), not runaway

    # Engine still usable after hitting the cap (state cleaned up).
    assert _engine.render_string("{{ a }}", {"a": "ok"}) == "ok"


def test_deeply_nested_blocks_bounded():
    """Deeply nested if/for blocks are bounded by the per-tree depth guard and
    never crash."""
    tmpl = "{% if True %}" * 300 + "deep" + "{% endif %}" * 300
    result = _engine.render_string(tmpl, {})
    assert isinstance(result, str)
    # nested for-loops with a body — also bounded, no crash
    tmpl2 = "{% for a in items %}" * 120 + "x" + "{% endfor %}" * 120
    assert isinstance(_engine.render_string(tmpl2, {"items": [1, 2]}), str)


# ---------------------------------------------------------------------------
# Property 8: Math expressions
# ---------------------------------------------------------------------------


@given(
    a=st.integers(min_value=-100, max_value=100),
    b=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=200, deadline=3000)
def test_math_add(a, b):
    """{{ a + b }} computes correct sum."""
    result = _engine.render_string("{{ a + b }}", {"a": a, "b": b})
    assert str(a + b) in result


@given(
    a=st.integers(min_value=-100, max_value=100),
    b=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=200, deadline=3000)
def test_math_multiply(a, b):
    """{{ a * b }} computes correct product."""
    result = _engine.render_string("{{ a * b }}", {"a": a, "b": b})
    assert str(a * b) in result


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Template Engine Hypothesis Fuzz Tests ──\n")

    tests = [
        ("variable renders", test_variable_renders_without_crash),
        ("string in output", test_string_variable_in_output),
        ("int in output", test_int_variable_in_output),
        ("filter produces string", test_filter_produces_string),
        ("autoescape HTML", test_autoescape_html),
        ("if/else branches", test_if_else_renders),
        ("for loop all items", test_for_loop_all_items),
        ("literal passthrough", test_literal_passthrough),
        ("math add", test_math_add),
        ("math multiply", test_math_multiply),
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
    print(f"Template fuzz: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
