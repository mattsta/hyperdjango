"""Tests for Jinja2 extension tags: {% do %}, {% debug %}, {% trans %}."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} {detail}")
        failed += 1
        errors.append(name)


print("=" * 60)
print("TEST: Jinja2 extension tags (do, debug, trans)")
print("=" * 60)

# ── {% do %} — Execute expression, discard result ──
print("\n── {% do %} tag ──")

engine = TemplateEngine(bytecode_cache=False)

# 1. Basic: do with list.append()
result = engine.render_string(
    "{% set items = [1, 2] %}{% do items.append(3) %}{{ items|join(',') }}", {}
)
test("do list.append", result == "1,2,3", f"got {result!r}")

# 2. Do with dict update
result = engine.render_string(
    "{% set d = {} %}{% do d.update({'a': 1}) %}{{ d.a }}", {}
)
test("do dict.update", result == "1", f"got {result!r}")

# 3. Do in for-loop (accumulate)
result = engine.render_string(
    "{% set items = [] %}{% for i in [10, 20, 30] %}{% do items.append(i) %}{% endfor %}{{ items|join('-') }}",
    {},
)
test("do in for-loop", result == "10-20-30", f"got {result!r}")

# 4. Do with method call on context object
result = engine.render_string(
    "{% do data.append('x') %}{{ data|join(',') }}", {"data": ["a", "b"]}
)
test("do on context object", result == "a,b,x", f"got {result!r}")

# 5. Do with expression (no side effect, just evaluates)
result = engine.render_string("{% do 1 + 2 %}OK", {})
test("do with pure expression", result == "OK", f"got {result!r}")

# 6. Do with string method
result = engine.render_string(
    "{% set items = [] %}{% do items.extend([1, 2, 3]) %}{{ items|length }}", {}
)
test("do list.extend", result == "3", f"got {result!r}")

# ── {% debug %} — Dump context variables ──
print("\n── {% debug %} tag ──")

# 7. Basic debug output
result = engine.render_string("{% debug %}", {"x": 1, "y": "hello"})
test("debug produces output", len(result) > 0, f"got {result!r}")
test("debug contains key 'x'", "'x'" in result, f"got {result!r}")
test("debug contains key 'y'", "'y'" in result, f"got {result!r}")
test("debug contains value 1", "1" in result, f"got {result!r}")
test("debug contains value 'hello'", "'hello'" in result, f"got {result!r}")

# 8. Debug with empty context
result = engine.render_string("{% debug %}", {})
test("debug empty context", "{" in result and "}" in result, f"got {result!r}")

# 9. Debug with namespace
result = engine.render_string("{% set x = 42 %}{% debug %}", {"name": "test"})
test("debug shows set variables", "'x'" in result or "42" in result, f"got {result!r}")

# 10. Debug mixed with content
result = engine.render_string("BEFORE{% debug %}AFTER", {"k": "v"})
test(
    "debug inline with content",
    result.startswith("BEFORE") and result.endswith("AFTER"),
    f"got {result!r}",
)

# ── {% trans %}...{% endtrans %} — i18n translation ──
print("\n── {% trans %} tag ──")

# 11. Trans without callback (passthrough)
result = engine.render_string("{% trans %}Hello World{% endtrans %}", {})
test("trans passthrough (no callback)", result == "Hello World", f"got {result!r}")

# 12. Trans with callback
translations = {
    "Hello World": "Hola Mundo",
    "Welcome": "Bienvenido",
    "Hello %(name)s": "Hola %(name)s",
}
engine_i18n = TemplateEngine(
    bytecode_cache=False,
    i18n_callback=lambda key: translations.get(key, key),
)
result = engine_i18n.render_string("{% trans %}Hello World{% endtrans %}", {})
test("trans with callback", result == "Hola Mundo", f"got {result!r}")

# 13. Trans passthrough for unknown key
result = engine_i18n.render_string("{% trans %}Unknown key{% endtrans %}", {})
test("trans unknown key passthrough", result == "Unknown key", f"got {result!r}")

# 14. Trans with variable binding
result = engine_i18n.render_string(
    "{% trans name=user %}Hello %(name)s{% endtrans %}", {"user": "Alice"}
)
test("trans with variable binding", result == "Hola Alice", f"got {result!r}")

# 15. Multiple trans blocks
result = engine_i18n.render_string(
    "{% trans %}Hello World{% endtrans %} - {% trans %}Welcome{% endtrans %}", {}
)
test("multiple trans blocks", result == "Hola Mundo - Bienvenido", f"got {result!r}")

# 16. Trans with static content around it
result = engine_i18n.render_string("<h1>{% trans %}Hello World{% endtrans %}</h1>", {})
test("trans in HTML", result == "<h1>Hola Mundo</h1>", f"got {result!r}")

# 17. No i18n callback → renders raw body
engine_no_i18n = TemplateEngine(bytecode_cache=False)
result = engine_no_i18n.render_string("{% trans %}Some text{% endtrans %}", {})
test("trans no callback renders body", result == "Some text", f"got {result!r}")

# ── Regression: existing features still work ──
print("\n── Regression checks ──")

result = engine.render_string(
    "{% for i in [1,2,3] %}{% if i == 2 %}{% break %}{% endif %}{{ i }}{% endfor %}", {}
)
test("break still works", result == "1", f"got {result!r}")

result = engine.render_string(
    "{% for i in [1,2,3] %}{% if i == 2 %}{% continue %}{% endif %}{{ i }}{% endfor %}",
    {},
)
test("continue still works", result == "13", f"got {result!r}")

result = engine.render_string("{{ x|upper }}", {"x": "hello"})
test("filters still work", result == "HELLO", f"got {result!r}")

result = engine.render_string("{% with y=42 %}{{ y }}{% endwith %}", {})
test("with block still works", result == "42", f"got {result!r}")

# ── Performance ──
print("\n── Performance ──")
N = 5000

# do tag
start = time.perf_counter_ns()
for _ in range(N):
    engine.render_string("{% set x = [] %}{% do x.append(1) %}{{ x|length }}", {})
ns_do = (time.perf_counter_ns() - start) / N
print(f"  do tag: {ns_do:,.0f} ns/render")

# debug tag
start = time.perf_counter_ns()
for _ in range(N):
    engine.render_string("{% debug %}", {"a": 1})
ns_debug = (time.perf_counter_ns() - start) / N
print(f"  debug tag: {ns_debug:,.0f} ns/render")

# trans tag (no callback)
start = time.perf_counter_ns()
for _ in range(N):
    engine.render_string("{% trans %}Hello{% endtrans %}", {})
ns_trans = (time.perf_counter_ns() - start) / N
print(f"  trans tag (passthrough): {ns_trans:,.0f} ns/render")

# trans tag (with callback)
start = time.perf_counter_ns()
for _ in range(N):
    engine_i18n.render_string("{% trans %}Hello World{% endtrans %}", {})
ns_trans_cb = (time.perf_counter_ns() - start) / N
print(f"  trans tag (callback): {ns_trans_cb:,.0f} ns/render")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
