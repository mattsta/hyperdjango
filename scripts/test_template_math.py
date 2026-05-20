#!/usr/bin/env python3
"""Test template math operators, string concat, ternary, parenthesized grouping,
and for-loop tuple unpacking.

Tests:
1. Math operators: +, -, *, /, //, %, **
2. String concatenation: ~
3. Ternary inline: x if cond else y
4. Parenthesized grouping: (a + b) * c
5. Float literals
6. Unary minus
7. Mixed math + comparisons in {% if %}
8. For-loop tuple unpacking: {% for k, v in items %}
9. Math expressions with filters
10. Jinja2 correctness comparison
"""

# hyper-test: unit

import sys

from hyperdjango._hyperdjango_native import _template_compile, _template_render


def r(source, context):
    capsule = _template_compile(source, "<test>")
    result = _template_render(capsule, context)
    return result.decode("utf-8") if isinstance(result, bytes) else result


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

    # ── Math operators ────────────────────────────────────────────────────
    print("\n=== Math operators: addition ===")
    result = r("{{ x + y }}", {"x": 3, "y": 4})
    check("int addition", result == "7", f"got '{result}'")

    result = r("{{ x + 1 }}", {"x": 10})
    check("int + literal", result == "11", f"got '{result}'")

    result = r("{{ 2 + 3 }}", {})
    check("literal + literal", result == "5", f"got '{result}'")

    print("\n=== Math operators: subtraction ===")
    result = r("{{ x - y }}", {"x": 10, "y": 3})
    check("int subtraction", result == "7", f"got '{result}'")

    result = r("{{ x - 1 }}", {"x": 5})
    check("int - literal", result == "4", f"got '{result}'")

    print("\n=== Math operators: multiplication ===")
    result = r("{{ x * y }}", {"x": 3, "y": 4})
    check("int multiplication", result == "12", f"got '{result}'")

    result = r("{{ x * 2 }}", {"x": 7})
    check("int * literal", result == "14", f"got '{result}'")

    print("\n=== Math operators: division ===")
    result = r("{{ x / y }}", {"x": 10, "y": 4})
    check("true division", result in ("2.5", "2.500000"), f"got '{result}'")

    result = r("{{ x // y }}", {"x": 10, "y": 3})
    check("floor division", result == "3", f"got '{result}'")

    result = r("{{ x % y }}", {"x": 10, "y": 3})
    check("modulo", result == "1", f"got '{result}'")

    print("\n=== Math operators: power ===")
    result = r("{{ 2 ** 3 }}", {})
    check("power", result == "8", f"got '{result}'")

    result = r("{{ x ** 2 }}", {"x": 5})
    check("var ** literal", result == "25", f"got '{result}'")

    result = r("{{ 2 ** 3 ** 2 }}", {})
    check("right-associative power (2**9=512)", result == "512", f"got '{result}'")

    print("\n=== Operator precedence ===")
    result = r("{{ 2 + 3 * 4 }}", {})
    check("add + mul precedence", result == "14", f"got '{result}'")

    result = r("{{ 10 - 2 * 3 }}", {})
    check("sub + mul precedence", result == "4", f"got '{result}'")

    result = r("{{ 2 * 3 + 4 }}", {})
    check("mul + add precedence", result == "10", f"got '{result}'")

    result = r("{{ 20 / 4 + 1 }}", {})
    check("div + add precedence", result in ("6.0", "6", "6.000000"), f"got '{result}'")

    print("\n=== Parenthesized grouping ===")
    result = r("{{ (2 + 3) * 4 }}", {})
    check("(add) * lit", result == "20", f"got '{result}'")

    result = r("{{ 2 * (3 + 4) }}", {})
    check("lit * (add)", result == "14", f"got '{result}'")

    result = r("{{ (x + y) * z }}", {"x": 2, "y": 3, "z": 4})
    check("(var + var) * var", result == "20", f"got '{result}'")

    result = r("{{ (10 - 2) // 3 }}", {})
    check("(sub) // lit", result == "2", f"got '{result}'")

    print("\n=== Float literals ===")
    result = r("{{ 3.14 }}", {})
    check("float literal display", "3.14" in result, f"got '{result}'")

    result = r("{{ x + 0.5 }}", {"x": 1})
    check("int + float", "1.5" in result, f"got '{result}'")

    print("\n=== Unary minus ===")
    result = r("{{ -x }}", {"x": 5})
    check("unary minus var", result == "-5", f"got '{result}'")

    result = r("{{ -1 }}", {})
    check("negative literal", result == "-1", f"got '{result}'")

    # ── String concatenation ──────────────────────────────────────────────
    print("\n=== String concatenation (~) ===")
    result = r('{{ "hello" ~ " " ~ "world" }}', {})
    check("literal concat", result == "hello world", f"got '{result}'")

    result = r('{{ name ~ "!" }}', {"name": "Alice"})
    check("var ~ literal", result == "Alice!", f"got '{result}'")

    result = r('{{ "Count: " ~ x }}', {"x": 42})
    check("string ~ int (auto-convert)", result == "Count: 42", f"got '{result}'")

    result = r("{{ a ~ b ~ c }}", {"a": "x", "b": "y", "c": "z"})
    check("triple concat", result == "xyz", f"got '{result}'")

    # ── Ternary inline ────────────────────────────────────────────────────
    print("\n=== Ternary inline (x if cond else y) ===")
    result = r('{{ "yes" if active else "no" }}', {"active": True})
    check("ternary true", result == "yes", f"got '{result}'")

    result = r('{{ "yes" if active else "no" }}', {"active": False})
    check("ternary false", result == "no", f"got '{result}'")

    result = r("{{ x if x else 0 }}", {"x": 42})
    check("ternary with var values", result == "42", f"got '{result}'")

    result = r("{{ x if x else 0 }}", {"x": 0})
    check("ternary falsy var", result == "0", f"got '{result}'")

    result = r('{{ "even" if x % 2 == 0 else "odd" }}', {"x": 4})
    check("ternary with math condition", result == "even", f"got '{result}'")

    result = r('{{ "even" if x % 2 == 0 else "odd" }}', {"x": 3})
    check("ternary with math condition (false)", result == "odd", f"got '{result}'")

    # ── Math in {% if %} conditions ───────────────────────────────────────
    print("\n=== Math in conditions ===")
    result = r("{% if x + y > 10 %}big{% else %}small{% endif %}", {"x": 7, "y": 5})
    check("if math > compare", result == "big", f"got '{result}'")

    result = r("{% if x * 2 == y %}equal{% else %}nope{% endif %}", {"x": 5, "y": 10})
    check("if math == compare", result == "equal", f"got '{result}'")

    result = r("{% if x % 2 == 0 %}even{% else %}odd{% endif %}", {"x": 4})
    check("if modulo compare", result == "even", f"got '{result}'")

    # ── For-loop tuple unpacking ──────────────────────────────────────────
    print("\n=== For-loop tuple unpacking ===")
    result = r(
        "{% for k, v in items %}{{ k }}={{ v }} {% endfor %}",
        {"items": [("a", 1), ("b", 2)]},
    )
    check("basic tuple unpack", result == "a=1 b=2 ", f"got '{result}'")

    result = r(
        "{% for k, v in items %}{{ k }}: {{ v }}\n{% endfor %}",
        {"items": [("name", "Alice"), ("age", "30")]},
    )
    check(
        "tuple unpack with strings",
        result == "name: Alice\nage: 30\n",
        f"got '{result}'",
    )

    result = r(
        "{% for i, name in enumerate %}{{ i }}. {{ name }}\n{% endfor %}",
        {"enumerate": [(0, "a"), (1, "b"), (2, "c")]},
    )
    check("enumerate-style unpack", result == "0. a\n1. b\n2. c\n", f"got '{result}'")

    # dict.items() returns tuples
    result = r(
        "{% for k, v in pairs %}{{ k }}={{ v }},{% endfor %}",
        {"pairs": list({"x": 1, "y": 2}.items())},
    )
    check("dict items unpack", result == "x=1,y=2,", f"got '{result}'")

    # 3-element tuple unpacking
    result = r(
        "{% for a, b, c in triples %}{{ a }}+{{ b }}+{{ c }} {% endfor %}",
        {"triples": [(1, 2, 3), (4, 5, 6)]},
    )
    check("3-tuple unpack", result == "1+2+3 4+5+6 ", f"got '{result}'")

    # Tuple unpacking with loop variables
    result = r(
        "{% for k, v in items %}{{ loop.index }}:{{ k }}={{ v }} {% endfor %}",
        {"items": [("a", 1), ("b", 2)]},
    )
    check("unpack + loop vars", result == "1:a=1 2:b=2 ", f"got '{result}'")

    # ── Math expressions with filters ─────────────────────────────────────
    print("\n=== Math expressions with filters ===")
    # Note: in our engine, | in expression context applies to the whole expression
    # Use parentheses for clarity: {{ (x + y)|filter }}
    result = r("{{ (x + y)|abs }}", {"x": -3, "y": 1})
    check("grouped math then abs filter", result == "2", f"got '{result}'")

    result = r("{{ (x + y)|abs }}", {"x": -10, "y": 3})
    check("grouped math with filter", result == "7", f"got '{result}'")

    # ── Complex expressions ───────────────────────────────────────────────
    print("\n=== Complex mixed expressions ===")
    result = r("{{ x * y + z }}", {"x": 2, "y": 3, "z": 4})
    check("mul then add", result == "10", f"got '{result}'")

    result = r("{{ x + y * z }}", {"x": 2, "y": 3, "z": 4})
    check("add with mul precedence", result == "14", f"got '{result}'")

    result = r('{{ "Score: " ~ x * 10 }}', {"x": 5})
    check("concat with math", result == "Score: 50", f"got '{result}'")

    # ── Unspaced operators (the whole reason for the tokenizer rewrite) ──
    print("\n=== Unspaced operators ===")
    result = r("{{ x+y }}", {"x": 3, "y": 4})
    check("x+y (no spaces)", result == "7", f"got '{result}'")

    result = r("{{ x-y }}", {"x": 10, "y": 3})
    check("x-y (no spaces)", result == "7", f"got '{result}'")

    result = r("{{ x*y }}", {"x": 3, "y": 4})
    check("x*y (no spaces)", result == "12", f"got '{result}'")

    result = r("{{ x//y }}", {"x": 10, "y": 3})
    check("x//y (no spaces)", result == "3", f"got '{result}'")

    result = r("{{ x%y }}", {"x": 10, "y": 3})
    check("x%y (no spaces)", result == "1", f"got '{result}'")

    result = r("{{ x**2 }}", {"x": 5})
    check("x**2 (no spaces)", result == "25", f"got '{result}'")

    result = r("{{ 2+3*4 }}", {})
    check("2+3*4 precedence (no spaces)", result == "14", f"got '{result}'")

    result = r('{{ "hello"~name }}', {"name": "world"})
    check('"hello"~name (no spaces)', result == "helloworld", f"got '{result}'")

    result = r("{{ (x+1)*(y-1) }}", {"x": 2, "y": 5})
    check("(x+1)*(y-1) (no spaces)", result == "12", f"got '{result}'")

    # ── Jinja2 correctness comparison ─────────────────────────────────────
    print("\n=== Jinja2 correctness comparison ===")
    try:
        from jinja2 import Environment

        env = Environment()

        test_cases = [
            ("{{ 2 + 3 }}", {}),
            ("{{ 10 - 4 }}", {}),
            ("{{ 3 * 7 }}", {}),
            ("{{ 10 // 3 }}", {}),
            ("{{ 10 % 3 }}", {}),
            ("{{ 2 ** 8 }}", {}),
            ("{{ (2 + 3) * 4 }}", {}),
            ('{{ "hello" ~ " " ~ "world" }}', {}),
            ('{{ "yes" if True else "no" }}', {}),
            ('{{ "yes" if False else "no" }}', {}),
            ("{{ x + y }}", {"x": 10, "y": 20}),
            ("{{ x * y + z }}", {"x": 2, "y": 3, "z": 4}),
        ]

        for template_str, ctx in test_cases:
            j2_result = env.from_string(template_str).render(ctx)
            zig_result = r(template_str, ctx)
            match = j2_result.strip() == zig_result.strip()
            check(
                f"match: {template_str[:50]}",
                match,
                f"jinja2='{j2_result}' zig='{zig_result}'",
            )

    except ImportError:
        print("  SKIP: Jinja2 not available for comparison")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All math/concat/ternary/unpacking tests passed!")
    else:
        print("SOME TESTS FAILED!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
