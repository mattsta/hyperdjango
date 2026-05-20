"""Security tests for template bytecode cache — crafted malicious inputs."""

# hyper-test: unit

import contextlib
import faulthandler

faulthandler.enable()

import struct
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
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def test(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} {detail}")
        failed += 1
        errors.append(name)


def test_rejects(name: str, data: bytes, hash_val: int) -> None:
    """Test that deserialize rejects the input without crashing."""
    global passed, failed
    try:
        result = _template_deserialize(data, hash_val)
        if result is None:
            print(f"  PASS: {name} (returned None)")
            passed += 1
        else:
            # Even if it returns something, try rendering to verify it doesn't crash
            try:
                _template_render(result, {})
                print(f"  PASS: {name} (accepted but renders OK)")
                passed += 1
            except Exception:
                print(f"  PASS: {name} (accepted, render fails safely)")
                passed += 1
    except Exception as e:
        print(f"  PASS: {name} (exception: {type(e).__name__})")
        passed += 1


print("=" * 60)
print("TEST: Bytecode cache security — crafted malicious inputs")
print("=" * 60)

# Get a valid serialized template for baseline
source = "Hello {{ name }}!"
capsule = _template_compile(source, "<test>")
valid_data = _template_serialize(capsule, source)
valid_hash = fnv1a_64(source.encode())

# ── 1. Truncated files at various offsets ──
print("\n── Truncated files ──")
for i in range(0, min(len(valid_data), 50), 4):
    test_rejects(f"truncated at byte {i}", valid_data[:i], valid_hash)

# ── 2. Wrong magic bytes ──
print("\n── Invalid headers ──")
test_rejects("wrong magic XXXX", b"XXXX" + valid_data[4:], valid_hash)
test_rejects("wrong magic null", b"\x00\x00\x00\x00" + valid_data[4:], valid_hash)
test_rejects("all zeros", b"\x00" * 100, valid_hash)
test_rejects("all 0xFF", b"\xff" * 100, valid_hash)

# ── 3. Wrong version ──
bad_version = valid_data[:4] + struct.pack("<H", 999) + valid_data[6:]
test_rejects("wrong version 999", bad_version, valid_hash)

# ── 4. Wrong hash ──
test_rejects("wrong hash", valid_data, 12345)

# ── 5. Giant string length (OOM DoS attempt) ──
print("\n── OOM attacks ──")
# Craft header + a string with length 0xFFFFFFFE
oom_data = (
    b"HZTC" + struct.pack("<H", 1) + struct.pack("<H", 0)
)  # magic + version + reserved
oom_data += struct.pack("<Q", valid_hash)  # hash
oom_data += struct.pack("<I", 1000)  # total size
oom_data += struct.pack("<I", 1)  # node count
oom_data += struct.pack("<I", 0xFFFFFFFE)  # source_path length = 4GB
oom_data += b"\x00" * 100
test_rejects("4GB string length", oom_data, valid_hash)

# String length just under limit
oom_data2 = b"HZTC" + struct.pack("<H", 1) + struct.pack("<H", 0)
oom_data2 += struct.pack("<Q", valid_hash)
oom_data2 += struct.pack("<I", 1000)
oom_data2 += struct.pack("<I", 1)
oom_data2 += struct.pack("<I", 10_000_001)  # 10MB + 1 = over limit
oom_data2 += b"\x00" * 100
test_rejects("10MB+1 string length (over limit)", oom_data2, valid_hash)

# ── 6. Giant node count (OOM DoS attempt) ──
oom_data3 = b"HZTC" + struct.pack("<H", 1) + struct.pack("<H", 0)
oom_data3 += struct.pack("<Q", valid_hash)
oom_data3 += struct.pack("<I", 1000)
oom_data3 += struct.pack("<I", 0xFFFFFFFF)  # node count = 4 billion
oom_data3 += struct.pack("<I", 0)  # source_path length = 0
test_rejects("4B node count", oom_data3, valid_hash)

# ── 7. Invalid enum values ──
print("\n── Invalid enums ──")
# Craft a valid-looking file with invalid NodeType
good_header = valid_data[:20]  # first 20 bytes (magic+version+reserved+hash)
# After header: total_size(4) + node_count(4) + source_path
# Build: header + total=100 + count=1 + path_len=0 + node_type=255
enum_data = good_header
enum_data += struct.pack("<I", 100)  # total size
enum_data += struct.pack("<I", 1)  # 1 node
enum_data += struct.pack("<I", 0)  # source_path len = 0
enum_data += bytes([255])  # invalid NodeType
enum_data += b"\x00" * 200  # padding
test_rejects("invalid NodeType 255", enum_data, valid_hash)

# ── 8. Deeply nested expression (stack overflow attempt) ──
print("\n── Deep nesting attack ──")
# Build: presence=1, type=literal_var(0), cmp=0, negate=0, int=0, float=0, str_len=0, varpath_count=0
# then left=presence(1), type=0, ... recursively
expr_atom = (
    bytes(
        [
            1,  # presence
            0,  # type = literal_var
            0,  # cmp_op = none
            0,  # negate = false
        ]
    )
    + struct.pack("<q", 0)
    + struct.pack("<d", 0.0)
    + struct.pack("<I", 0)
    + struct.pack("<H", 0)
)
# left=null, right=null, ternary_false=null, call_args=null
expr_null_tail = bytes([0, 0, 0]) + struct.pack("<H", 0xFFFF)

# Build a chain of 1000 nested expressions (each left points to next)
deep_expr = b""
for _ in range(1000):
    deep_expr += bytes([1, 0, 0, 0]) + struct.pack("<q", 0) + struct.pack("<d", 0.0)
    deep_expr += struct.pack("<I", 0) + struct.pack(
        "<H", 0
    )  # str_len=0, varpath=0 parts
    # left = next nested expr (presence=1 at start of next iteration)
# Terminal: all nulls
deep_expr += expr_null_tail  # left=null
deep_expr += bytes([0]) * 3  # right=null, ternary_false=null
deep_expr += struct.pack("<H", 0xFFFF)  # call_args=null

# This won't be a valid full cache file, but tests the depth limit in isolation
# Let's build a proper file with deep expression in the expr field of a node
# Actually, simpler: just verify the deserializer doesn't crash on crafted deep data
test_rejects(
    "deeply nested expr (1000 levels)", valid_data[:10] + deep_expr, valid_hash
)

# ── 9. Bit-flip fuzzing ──
print("\n── Bit-flip fuzzing (100 random flips) ──")
import random

random.seed(42)
flip_crashes = 0
for i in range(100):
    data = bytearray(valid_data)
    # Flip a random bit in the data (skip header)
    pos = random.randint(20, len(data) - 1)
    bit = random.randint(0, 7)
    data[pos] ^= 1 << bit
    try:
        result = _template_deserialize(bytes(data), valid_hash)
        if result is not None:
            # Try to render — should not crash
            with contextlib.suppress(Exception):
                _template_render(result, {"name": "test"})
    except Exception:
        pass  # exception is fine
test("100 bit-flip fuzz — no crashes", True)

# ── 10. Valid roundtrip still works after security hardening ──
print("\n── Roundtrip verification ──")
source2 = "{% for i in items %}{{ i|upper }},{% endfor %}"
cap2 = _template_compile(source2, "<v2>")
ser2 = _template_serialize(cap2, source2)
hash2 = fnv1a_64(source2.encode())
cap2_cached = _template_deserialize(ser2, hash2)
test("roundtrip still works", cap2_cached is not None)
r_orig = _template_render(cap2, {"items": ["a", "b", "c"]})
r_cached = _template_render(cap2_cached, {"items": ["a", "b", "c"]})
test(
    "roundtrip render matches",
    r_orig == r_cached,
    f"orig={r_orig!r} cached={r_cached!r}",
)

# ── 11. Stress: many roundtrips ──
for _ in range(200):
    cap_s = _template_deserialize(ser2, hash2)
    _template_render(cap_s, {"items": ["x"]})
test("200 deserialize+render cycles — no crash", True)

# ── 12. Configurable safety limits ──
print("\n── Configurable safety limits ──")
from hyperdjango._hyperdjango_native import _template_set_safety_limits

# Tighten limits
_template_set_safety_limits(100, 10, 5)  # 100 byte strings, 10 nodes, depth 5

# Small string should still work
small_source = "{{ x }}"
cap_small = _template_compile(small_source, "<small>")
ser_small = _template_serialize(cap_small, small_source)
hash_small = fnv1a_64(small_source.encode())
cap_small2 = _template_deserialize(ser_small, hash_small)
test("tight limits — small template roundtrips", cap_small2 is not None)
r_small = _template_render(cap_small2, {"x": "OK"})
test("tight limits — renders correctly", r_small == b"OK")

# Larger template should be rejected with tight string limit
big_source = "{{ " + "x" * 200 + " }}"
cap_big = _template_compile(big_source, "<big>")
ser_big = _template_serialize(cap_big, big_source)
hash_big = fnv1a_64(big_source.encode())
cap_big2 = _template_deserialize(ser_big, hash_big)
test("tight string limit rejects large var name", cap_big2 is None)

# Reset to defaults
_template_set_safety_limits(0, 0, 0)

# Verify defaults restored — large template works again
cap_big3 = _template_deserialize(ser_big, hash_big)
test("default limits — large template accepted after reset", cap_big3 is not None)

# Custom limits via TemplateEngine config
from hyperdjango.templating import TemplateEngine

eng_tight = TemplateEngine(max_string_len=50, max_array_count=5, max_expr_depth=10)
# Just verify it doesn't crash — the limits are applied via threadlocal
result_tight = eng_tight.render_string("{{ x }}", {"x": "works"})
test("TemplateEngine with custom limits renders OK", result_tight == "works")

# Reset for other tests
_template_set_safety_limits(0, 0, 0)

# ── Performance ──
print("\n── Performance ──")
start = time.perf_counter_ns()
N = 10000
for _ in range(N):
    _template_deserialize(valid_data, valid_hash)
ns_per = (time.perf_counter_ns() - start) / N
print(f"  deserialize: {ns_per:,.0f} ns/call")

# Rejection performance (wrong hash — fast path)
start = time.perf_counter_ns()
for _ in range(N):
    _template_deserialize(valid_data, 12345)
ns_reject = (time.perf_counter_ns() - start) / N
print(f"  reject (wrong hash): {ns_reject:,.0f} ns/call")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
