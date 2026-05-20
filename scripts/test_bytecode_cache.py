"""Tests for template bytecode caching / compiled module persistence."""

# hyper-test: unit

import sys
import time

from hyperdjango._hyperdjango_native import (
    _template_compile,
    _template_deserialize,
    _template_render,
    _template_serialize,
)

passed = 0
failed = 0
errors: list[str] = []


def fnv1a_64(data: bytes) -> int:
    """FNV-1a 64-bit hash — must match Zig's std.hash.Fnv1a_64."""
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def test(name: str, passed_cond: bool, detail: str = "") -> None:
    global passed, failed
    if passed_cond:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} {detail}")
        failed += 1
        errors.append(name)


print("=" * 60)
print("TEST: Template bytecode caching")
print("=" * 60)

# ── 1. Basic roundtrip: compile → serialize → deserialize → render ──
source = "Hello {{ name }}!"
capsule = _template_compile(source, "<test>")
serialized = _template_serialize(capsule, source)
test(
    "serialize produces bytes",
    isinstance(serialized, bytes) and len(serialized) > 20,
    f"got {type(serialized)}, len={len(serialized) if serialized else 0}",
)

source_hash = fnv1a_64(source.encode())
capsule2 = _template_deserialize(serialized, source_hash)
test("deserialize returns capsule (not None)", capsule2 is not None)

result = _template_render(capsule2, {"name": "World"})
result_str = result.decode("utf-8") if isinstance(result, bytes) else result
test("roundtrip render matches", result_str == "Hello World!", f"got {result_str!r}")

# ── 2. Hash mismatch returns None ──
wrong_hash = fnv1a_64(b"different source")
capsule3 = _template_deserialize(serialized, wrong_hash)
test("hash mismatch returns None", capsule3 is None)

# ── 3. Corrupted data returns None ──
capsule4 = _template_deserialize(b"garbage data", source_hash)
test("corrupted data returns None", capsule4 is None)

# ── 4. Empty data returns None ──
capsule5 = _template_deserialize(b"", source_hash)
test("empty data returns None", capsule5 is None)

# ── 5. Wrong magic returns None ──
bad_magic = b"XXXX" + serialized[4:]
capsule6 = _template_deserialize(bad_magic, source_hash)
test("wrong magic returns None", capsule6 is None)

# ── 6. Complex template roundtrip ──
complex_source = """
{% if show %}
  {% for item in items %}
    {{ loop.index }}: {{ item.name|upper }} ({{ item.value|default('N/A') }})
  {% endfor %}
{% else %}
  No items
{% endif %}
"""
capsule_cx = _template_compile(complex_source, "<complex>")
ser_cx = _template_serialize(capsule_cx, complex_source)
hash_cx = fnv1a_64(complex_source.encode())
capsule_cx2 = _template_deserialize(ser_cx, hash_cx)
test("complex template deserializes", capsule_cx2 is not None)

ctx_cx = {"show": True, "items": [{"name": "alpha", "value": 42}, {"name": "beta"}]}
result_orig = _template_render(capsule_cx, ctx_cx)
result_cached = _template_render(capsule_cx2, ctx_cx)
test(
    "complex template render matches",
    result_orig == result_cached,
    f"orig={result_orig!r}, cached={result_cached!r}",
)

# ── 7. Template with macros ──
macro_source = (
    '{% macro greet(name) %}Hello {{ name }}!{% endmacro %}{{ greet("World") }}'
)
cap_m = _template_compile(macro_source, "<macro>")
ser_m = _template_serialize(cap_m, macro_source)
cap_m2 = _template_deserialize(ser_m, fnv1a_64(macro_source.encode()))
test("macro template deserializes", cap_m2 is not None)
r_m = _template_render(cap_m2, {})
test(
    "macro template renders from cache",
    r_m == _template_render(cap_m, {}),
    f"got {r_m!r}",
)

# ── 8. Template with filters ──
filter_source = "{{ items|select('odd')|sort|join(', ') }}"
cap_f = _template_compile(filter_source, "<filter>")
ser_f = _template_serialize(cap_f, filter_source)
cap_f2 = _template_deserialize(ser_f, fnv1a_64(filter_source.encode()))
test("filter template deserializes", cap_f2 is not None)
ctx_f = {"items": [5, 2, 3, 4, 1]}
r_f = _template_render(cap_f2, ctx_f)
test(
    "filter template renders from cache",
    r_f == _template_render(cap_f, ctx_f),
    f"got {r_f!r}",
)

# ── 9. Template with set/namespace ──
ns_source = "{% set ns = namespace(c=0) %}{% for i in [1,2,3] %}{% set ns.c = ns.c + 1 %}{% endfor %}{{ ns.c }}"
cap_ns = _template_compile(ns_source, "<ns>")
ser_ns = _template_serialize(cap_ns, ns_source)
cap_ns2 = _template_deserialize(ser_ns, fnv1a_64(ns_source.encode()))
test("namespace template deserializes", cap_ns2 is not None)

from hyperdjango.templating import Namespace

r_ns = _template_render(cap_ns2, {"namespace": Namespace})
test(
    "namespace template renders from cache",
    r_ns == _template_render(cap_ns, {"namespace": Namespace}),
    f"got {r_ns!r}",
)

# ── 10. Template with is-tests ──
test_source = "{% if x is divisibleby(3) %}yes{% else %}no{% endif %}"
cap_t = _template_compile(test_source, "<test>")
ser_t = _template_serialize(cap_t, test_source)
cap_t2 = _template_deserialize(ser_t, fnv1a_64(test_source.encode()))
test("is-test template deserializes", cap_t2 is not None)
r_t = _template_render(cap_t2, {"x": 9})
test(
    "is-test template renders from cache",
    r_t == _template_render(cap_t, {"x": 9}),
    f"got {r_t!r}",
)

# ── 11. Template with autoescape block ──
ae_source = "{% autoescape false %}{{ html }}{% endautoescape %}"
cap_ae = _template_compile(ae_source, "<ae>")
ser_ae = _template_serialize(cap_ae, ae_source)
cap_ae2 = _template_deserialize(ser_ae, fnv1a_64(ae_source.encode()))
test("autoescape template deserializes", cap_ae2 is not None)
r_ae = _template_render(cap_ae2, {"html": "<b>bold</b>"})
test(
    "autoescape template renders from cache",
    r_ae == _template_render(cap_ae, {"html": "<b>bold</b>"}),
    f"got {r_ae!r}",
)

# ── 12. Repeated deserialize doesn't leak ──
for _ in range(500):
    cap_leak = _template_deserialize(serialized, source_hash)
    _template_render(cap_leak, {"name": "test"})
test("500 deserialize cycles — no crash", True)

# ── Performance ──
print("\n── Performance ──")

# Compile from source
start = time.perf_counter_ns()
N = 5000
for _ in range(N):
    _template_compile(complex_source, "<bench>")
compile_ns = (time.perf_counter_ns() - start) / N

# Deserialize from cache
start = time.perf_counter_ns()
for _ in range(N):
    _template_deserialize(ser_cx, hash_cx)
deser_ns = (time.perf_counter_ns() - start) / N

speedup = compile_ns / deser_ns if deser_ns > 0 else 0
print(f"  compile from source: {compile_ns:,.0f} ns")
print(f"  deserialize cache:   {deser_ns:,.0f} ns")
print(f"  speedup:             {speedup:.1f}x")

# Serialize time
start = time.perf_counter_ns()
for _ in range(N):
    _template_serialize(capsule_cx, complex_source)
ser_ns = (time.perf_counter_ns() - start) / N
print(f"  serialize time:      {ser_ns:,.0f} ns")
print(f"  cache size:          {len(ser_cx):,} bytes")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
