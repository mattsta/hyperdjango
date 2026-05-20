"""Tests for {% include "file.html" with x=expr, y=expr %} variable binding syntax."""

# hyper-test: unit

import shutil
import sys
import time
from pathlib import Path

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []

TEST_DIR = Path(__file__).parent / "_test_include_bindings_templates"


def setup():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True, exist_ok=True)


def teardown():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)


def write_template(name: str, content: str) -> None:
    path = TEST_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
print("TEST: {% include ... with x=expr %} variable bindings")
print("=" * 60)

setup()

# ── 1. Basic: single binding ──
print("\n── Basic single binding ──")
write_template("_card.html", "Name: {{ name }}")
write_template("page1.html", '{% include "_card.html" with name="Alice" %}')
engine = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result = engine.render("page1.html", {})
test("single binding", result == "Name: Alice", f"got {result!r}")

# ── 2. Multiple bindings ──
print("\n── Multiple bindings ──")
write_template("_user.html", "{{ name }} ({{ age }})")
write_template("page2.html", '{% include "_user.html" with name="Bob", age=30 %}')
engine2 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result2 = engine2.render("page2.html", {})
test("multiple bindings", result2 == "Bob (30)", f"got {result2!r}")

# ── 3. Binding with expression evaluation ──
print("\n── Expression evaluation in bindings ──")
write_template("_expr.html", "Result: {{ x }}")
write_template("page3.html", '{% include "_expr.html" with x=a + b %}')
engine3 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result3 = engine3.render("page3.html", {"a": 10, "b": 20})
test("expression binding", result3 == "Result: 30", f"got {result3!r}")

# ── 4. Binding with parent context variable ──
print("\n── Parent context variable binding ──")
write_template("_greet.html", "Hello {{ who }}!")
write_template("page4.html", '{% include "_greet.html" with who=user.name %}')
engine4 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result4 = engine4.render("page4.html", {"user": {"name": "Charlie"}})
test("parent var binding", result4 == "Hello Charlie!", f"got {result4!r}")

# ── 5. Bindings override parent context ──
print("\n── Bindings override parent context ──")
write_template("_show.html", "{{ x }}")
write_template("page5.html", '{% include "_show.html" with x="override" %}')
engine5 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result5 = engine5.render("page5.html", {"x": "original"})
test("binding overrides parent", result5 == "override", f"got {result5!r}")

# ── 6. Parent context still visible (bindings ADD to context) ──
print("\n── Parent context still visible ──")
write_template("_both.html", "{{ parent_var }} {{ bound_var }}")
write_template("page6.html", '{% include "_both.html" with bound_var="new" %}')
engine6 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result6 = engine6.render("page6.html", {"parent_var": "existing"})
test(
    "parent context visible with bindings",
    result6 == "existing new",
    f"got {result6!r}",
)

# ── 7. Binding with string concatenation ──
print("\n── String concat in binding ──")
write_template("_concat.html", "{{ msg }}")
write_template("page7.html", '{% include "_concat.html" with msg=first ~ " " ~ last %}')
engine7 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result7 = engine7.render("page7.html", {"first": "Hello", "last": "World"})
test("string concat binding", result7 == "Hello World", f"got {result7!r}")

# ── 8. Binding with filter ──
print("\n── Filter in binding ──")
write_template("_filtered.html", "{{ val }}")
write_template("page8.html", '{% include "_filtered.html" with val=name|upper %}')
engine8 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result8 = engine8.render("page8.html", {"name": "alice"})
test("filter in binding", result8 == "ALICE", f"got {result8!r}")

# ── 9. Binding with boolean ──
print("\n── Boolean binding ──")
write_template("_flag.html", "{% if show %}visible{% else %}hidden{% endif %}")
write_template("page9.html", '{% include "_flag.html" with show=True %}')
engine9 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result9 = engine9.render("page9.html", {})
test("boolean binding True", result9 == "visible", f"got {result9!r}")

write_template("page9b.html", '{% include "_flag.html" with show=False %}')
engine9b = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result9b = engine9b.render("page9b.html", {})
test("boolean binding False", result9b == "hidden", f"got {result9b!r}")

# ── 10. Binding with list literal ──
print("\n── List literal binding ──")
write_template("_list.html", "{% for i in items %}{{ i }}{% endfor %}")
write_template("page10.html", '{% include "_list.html" with items=[1, 2, 3] %}')
engine10 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result10 = engine10.render("page10.html", {})
test("list binding", result10 == "123", f"got {result10!r}")

# ── 11. Binding in for-loop ──
print("\n── Binding inside for-loop ──")
write_template("_item.html", "{{ label }}: {{ val }}")
write_template(
    "page11.html",
    '{% for item in items %}{% include "_item.html" with label=item.name, val=item.score %}\n{% endfor %}',
)
engine11 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result11 = engine11.render(
    "page11.html", {"items": [{"name": "A", "score": 10}, {"name": "B", "score": 20}]}
)
test(
    "binding in for-loop",
    "A: 10" in result11 and "B: 20" in result11,
    f"got {result11!r}",
)

# ── 12. Dynamic include with bindings ──
print("\n── Dynamic include with bindings ──")
write_template("_dyn.html", "Dynamic: {{ x }}")
write_template("page12.html", "{% include tmpl with x=42 %}")
engine12 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result12 = engine12.render("page12.html", {"tmpl": "_dyn.html"})
test("dynamic include with binding", result12 == "Dynamic: 42", f"got {result12!r}")

# ── 13. Existing "with context" still works ──
print("\n── Backward compat: with context ──")
write_template("_ctx.html", "{{ x }}")
write_template("page13.html", '{% include "_ctx.html" with context %}')
engine13 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result13 = engine13.render("page13.html", {"x": "yes"})
test("with context backward compat", result13 == "yes", f"got {result13!r}")

# ── 14. Existing "without context" still works ──
print("\n── Backward compat: without context ──")
write_template("page14.html", '{% include "_ctx.html" without context %}')
engine14 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result14 = engine14.render("page14.html", {"x": "no"})
test("without context backward compat", result14 == "", f"got {result14!r}")

# ── 15. Binding with ignore missing ──
print("\n── Binding + ignore missing ──")
write_template(
    "page15.html", '{% include "nonexistent.html" with x=1 ignore missing %}'
)
engine15 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result15 = engine15.render("page15.html", {})
test("binding + ignore missing", result15 == "", f"got {result15!r}")

# ── 16. Binding with ternary expression ──
print("\n── Ternary expression in binding ──")
write_template("_ternary.html", "{{ msg }}")
write_template(
    "page16.html", '{% include "_ternary.html" with msg="yes" if flag else "no" %}'
)
engine16 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result16a = engine16.render("page16.html", {"flag": True})
test("ternary binding true", result16a == "yes", f"got {result16a!r}")
result16b = engine16.render("page16.html", {"flag": False})
test("ternary binding false", result16b == "no", f"got {result16b!r}")

# ── 17. Binding does NOT leak into parent scope ──
print("\n── Binding scope isolation ──")
write_template("_scoped.html", "{{ injected }}")
write_template(
    "page17.html", '{% include "_scoped.html" with injected="inner" %}{{ injected }}'
)
engine17 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result17 = engine17.render("page17.html", {})
test("binding does not leak to parent", result17 == "inner", f"got {result17!r}")

# ── 18. Multiple includes with different bindings ──
print("\n── Multiple includes different bindings ──")
write_template("_val.html", "[{{ v }}]")
write_template(
    "page18.html",
    '{% include "_val.html" with v=1 %}{% include "_val.html" with v=2 %}{% include "_val.html" with v=3 %}',
)
engine18 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
result18 = engine18.render("page18.html", {})
test(
    "multiple includes different bindings", result18 == "[1][2][3]", f"got {result18!r}"
)

# ── 19. Bytecode cache roundtrip with bindings ──
print("\n── Bytecode cache roundtrip ──")
write_template("_bc.html", "{{ x }}-{{ y }}")
write_template("page19.html", '{% include "_bc.html" with x="a", y="b" %}')
# First render — compiles and caches
engine19 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=True)
result19a = engine19.render("page19.html", {})
test("first render with bindings", result19a == "a-b", f"got {result19a!r}")

# Second render from fresh engine (disk cache)
engine19b = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=True)
result19b = engine19b.render("page19.html", {})
test("disk cache roundtrip with bindings", result19b == "a-b", f"got {result19b!r}")

# ── Performance ──
print("\n── Performance ──")
write_template("_perf.html", "{{ x }}")
write_template("perf_include.html", '{% include "_perf.html" with x=val|upper %}')
engine_p = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
# Warm up
engine_p.render("perf_include.html", {"val": "test"})

N = 5000
start = time.perf_counter_ns()
for _ in range(N):
    engine_p.render("perf_include.html", {"val": "test"})
ns_per = (time.perf_counter_ns() - start) / N
print(f"  include with binding: {ns_per:,.0f} ns/render")

# Compare: without bindings
write_template("perf_plain.html", '{% include "_perf.html" %}')
engine_p2 = TemplateEngine(template_dir=TEST_DIR, bytecode_cache=False)
engine_p2.render("perf_plain.html", {"x": "TEST"})
start = time.perf_counter_ns()
for _ in range(N):
    engine_p2.render("perf_plain.html", {"x": "TEST"})
ns_plain = (time.perf_counter_ns() - start) / N
print(f"  include without binding: {ns_plain:,.0f} ns/render")
print(f"  overhead: {(ns_per - ns_plain) / 1000:.1f} μs")

# ── Cleanup ──
teardown()

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
