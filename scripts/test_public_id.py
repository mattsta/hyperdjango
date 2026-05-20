"""
Comprehensive tests for the Public ID system.

Tests:
- BaseEncoder: roundtrip, edge cases, padding, random, bytes, packed
- Alphabet validation and generation
- base_convert standalone utility
- Native Zig parity with Python
- PublicIDMixin: model integration, save hook, get_by_public_id
- PublicIDSerializer: output shape, PK hidden
- Error handling: invalid chars, empty strings, negative values, duplicate alphabets
"""

# hyper-test: unit

import os
import sys
import time

from hyperdjango.public_id import (
    ALPHANUMERIC_CHARS,
    OLC_SAFE_CHARS,
    BaseEncoder,
    IDStrategy,
    PublicIDMixin,
    base_convert,
    generate_alphabet,
    validate_alphabet,
)
from hyperdjango.serializers import PublicIDSerializer, SerializerField

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        errors.append(msg)
        print(msg)


# ── Alphabet validation ────────────────────────────────────────────────────

print("=== Alphabet Validation ===")

# Valid alphabets
validate_alphabet("ab")
check("min_alphabet_2_chars", True)

validate_alphabet(OLC_SAFE_CHARS)
check("olc_safe_chars_valid", True)

validate_alphabet(ALPHANUMERIC_CHARS)
check("alphanumeric_chars_valid", True)

validate_alphabet("0123456789abcdef")
check("hex_alphabet_valid", True)

# Invalid: too short
try:
    validate_alphabet("a")
    check("reject_single_char", False, "should have raised")
except ValueError:
    check("reject_single_char", True)

try:
    validate_alphabet("")
    check("reject_empty", False, "should have raised")
except ValueError:
    check("reject_empty", True)

# Invalid: duplicates
try:
    validate_alphabet("aab")
    check("reject_duplicates", False, "should have raised")
except ValueError as e:
    check("reject_duplicates", True)
    check("duplicate_error_shows_chars", "'a'" in str(e))

# Invalid: not a string
try:
    validate_alphabet(123)  # type: ignore[arg-type]
    check("reject_non_string", False, "should have raised")
except TypeError:
    check("reject_non_string", True)

# ── Alphabet generation ────────────────────────────────────────────────────

print("\n=== Alphabet Generation ===")

a1 = generate_alphabet("olc32")
check("gen_olc32_length", len(a1) == 32, f"got {len(a1)}")
check("gen_olc32_no_dupes", len(set(a1)) == 32)
check("gen_olc32_same_chars", set(a1) == set(OLC_SAFE_CHARS))

a2 = generate_alphabet("base62")
check("gen_base62_length", len(a2) == 62, f"got {len(a2)}")
check("gen_base62_no_dupes", len(set(a2)) == 62)
check("gen_base62_same_chars", set(a2) == set(ALPHANUMERIC_CHARS))

# Seeded generation is deterministic
a3 = generate_alphabet("olc32", seed=42)
a4 = generate_alphabet("olc32", seed=42)
check("gen_seeded_deterministic", a3 == a4)

# Different seeds produce different permutations
a5 = generate_alphabet("olc32", seed=1)
a6 = generate_alphabet("olc32", seed=2)
check("gen_different_seeds", a5 != a6)

# Invalid charset
try:
    generate_alphabet("base128")
    check("gen_invalid_charset", False, "should have raised")
except ValueError:
    check("gen_invalid_charset", True)

# ── BaseEncoder: basic encode/decode ───────────────────────────────────────

print("\n=== BaseEncoder: Basic ===")

# OLC-32 alphabet
olc_alpha = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
enc = BaseEncoder(olc_alpha)

check("encoder_base", enc.base == 32)
check("encoder_alphabet", enc.alphabet == olc_alpha)

# Roundtrip for various values
test_values = [
    0,
    1,
    2,
    10,
    31,
    32,
    33,
    100,
    1000,
    12345,
    999999,
    2**16,
    2**32,
    2**64 - 1,
    2**64,
    2**128,
    2**256,
]
for v in test_values:
    encoded = enc.encode(v)
    decoded = enc.decode(encoded)
    check(f"roundtrip_{v}", decoded == v, f"encoded={encoded!r}, decoded={decoded}")

# Zero encodes to first char
check("zero_first_char", enc.encode(0) == olc_alpha[0])

# One encodes to second char
check("one_second_char", enc.encode(1) == olc_alpha[1])

# base-1 values are single chars
for i in range(32):
    check(f"single_char_{i}", len(enc.encode(i)) == 1)

# base value (32) needs 2 chars
check("base_value_2_chars", len(enc.encode(32)) == 2)

# ── BaseEncoder: base-62 ──────────────────────────────────────────────────

print("\n=== BaseEncoder: Base-62 ===")

full_alpha = "9vy4nzdZGsp5x8u3JiS1O7eM0VrDNbCKYEFafj6QLXHtmlckPgRoBTwAWhqU2I"
enc62 = BaseEncoder(full_alpha)

check("base62_base", enc62.base == 62)

for v in test_values:
    encoded = enc62.encode(v)
    decoded = enc62.decode(encoded)
    check(f"base62_roundtrip_{v}", decoded == v)

# Base-62 should produce shorter strings than base-32 for same values
for v in [12345, 999999, 2**64]:
    e32 = enc.encode(v)
    e62 = enc62.encode(v)
    check(
        f"base62_shorter_{v}",
        len(e62) <= len(e32),
        f"base62={len(e62)} vs base32={len(e32)}",
    )

# ── BaseEncoder: padding ──────────────────────────────────────────────────

print("\n=== BaseEncoder: Padding ===")

p = enc.encode_padded(0, 8)
check("padded_zero", len(p) == 8)
check("padded_zero_content", p == olc_alpha[0] * 8)

p2 = enc.encode_padded(42, 8)
check("padded_42_width", len(p2) == 8)
check("padded_42_roundtrip", enc.decode(p2) == 42)

# Value that already needs 8+ chars doesn't get truncated
big = enc.encode(2**64)
p3 = enc.encode_padded(2**64, 8)
check("padded_big_no_truncate", len(p3) >= 8)
check("padded_big_roundtrip", enc.decode(p3) == 2**64)

# Width 1 = no padding effect for multi-char values
p4 = enc.encode_padded(100, 1)
check("padded_width_1", p4 == enc.encode(100))

# ── BaseEncoder: random ───────────────────────────────────────────────────

print("\n=== BaseEncoder: Random ===")

# Random IDs are unique
ids: set[str] = set()
for _ in range(10000):
    rid = enc.encode_random(8)
    ids.add(rid)
check("random_10k_unique", len(ids) == 10000, f"got {len(ids)} unique")

# All chars in output are from alphabet
alpha_set = set(olc_alpha)
for rid in list(ids)[:100]:
    for ch in rid:
        if ch not in alpha_set:
            check(f"random_valid_chars_{rid}", False, f"bad char {ch!r}")
            break
    else:
        check("random_valid_chars", True)
        break

# Different entropy sizes produce different lengths
r4 = enc.encode_random(4)
r16 = enc.encode_random(16)
check(
    "random_entropy_length", len(r16) > len(r4), f"4bytes={len(r4)}, 16bytes={len(r16)}"
)

# ── BaseEncoder: bytes ────────────────────────────────────────────────────

print("\n=== BaseEncoder: Bytes ===")

# Encode/decode bytes
data = b"\x00\x01\x02\x03"
encoded = enc.encode_bytes(data)
check("bytes_encode", isinstance(encoded, str))

decoded_bytes = enc.decode_to_bytes(encoded, 4)
check("bytes_roundtrip", decoded_bytes == data, f"got {decoded_bytes!r}")

# Empty bytes
e = enc.encode_bytes(b"")
check("bytes_empty", e == olc_alpha[0])

# 16-byte (UUID-sized)
import secrets

uuid_bytes = secrets.token_bytes(16)
encoded = enc.encode_bytes(uuid_bytes)
decoded = enc.decode_to_bytes(encoded, 16)
check("bytes_16_roundtrip", decoded == uuid_bytes)

# ── BaseEncoder: packed multi-integer ──────────────────────────────────────

print("\n=== BaseEncoder: Packed ===")

# Pack two 128-bit values
vals = [42, 99]
packed = enc.encode_packed(vals, 128)
unpacked = enc.decode_packed(packed, 128, 2)
check("packed_128_roundtrip", unpacked == vals, f"got {unpacked}")

# Pack three 64-bit values
vals3 = [100, 200, 300]
packed3 = enc.encode_packed(vals3, 64)
unpacked3 = enc.decode_packed(packed3, 64, 3)
check("packed_64_roundtrip", unpacked3 == vals3, f"got {unpacked3}")

# Pack with bytes
import secrets

b1 = secrets.token_bytes(16)
b2 = secrets.token_bytes(16)
packed_bytes = enc.encode_packed([b1, b2], 128)
unpacked_ints = enc.decode_packed(packed_bytes, 128, 2)
check(
    "packed_bytes_roundtrip",
    unpacked_ints[0] == int.from_bytes(b1, "big")
    and unpacked_ints[1] == int.from_bytes(b2, "big"),
)

# Overflow detection
try:
    enc.encode_packed([2**129], 128)
    check("packed_overflow", False, "should have raised")
except ValueError:
    check("packed_overflow", True)

# ── BaseEncoder: utility methods ──────────────────────────────────────────

print("\n=== BaseEncoder: Utilities ===")

# max_value_for_width
check("max_value_width_1", enc.max_value_for_width(1) == 31)  # base-32
check("max_value_width_2", enc.max_value_for_width(2) == 1023)
check("max_value_width_8", enc.max_value_for_width(8) == 32**8 - 1)

# width_for_bits
check("width_64_bits", enc.width_for_bits(64) == 13)  # ceil(64/5)
check("width_128_bits", enc.width_for_bits(128) == 26)  # ceil(128/5)
check("width_256_bits", enc.width_for_bits(256) == 52)  # ceil(256/5)

# base-62 widths
check("width62_64_bits", enc62.width_for_bits(64) == 11)
check("width62_128_bits", enc62.width_for_bits(128) == 22)

# repr
r = repr(enc)
check("repr_has_base", "base=32" in r)
check("repr_has_alphabet", olc_alpha in r)

# ── BaseEncoder: error handling ───────────────────────────────────────────

print("\n=== BaseEncoder: Errors ===")

# Negative value
try:
    enc.encode(-1)
    check("encode_negative", False, "should have raised")
except ValueError:
    check("encode_negative", True)

# Empty decode
try:
    enc.decode("")
    check("decode_empty", False, "should have raised")
except ValueError:
    check("decode_empty", True)

# Invalid character in decode
try:
    enc.decode("!")
    check("decode_invalid_char", False, "should have raised")
except ValueError:
    check("decode_invalid_char", True)

# Invalid alphabet (duplicate)
try:
    BaseEncoder("aab")
    check("encoder_duplicate_alphabet", False, "should have raised")
except ValueError:
    check("encoder_duplicate_alphabet", True)

# Invalid alphabet (too short)
try:
    BaseEncoder("a")
    check("encoder_short_alphabet", False, "should have raised")
except ValueError:
    check("encoder_short_alphabet", True)

# ── base_convert standalone ────────────────────────────────────────────────

print("\n=== base_convert ===")

# int -> custom base
result = base_convert(255, dst_alphabet="0123456789abcdef")
check("base_convert_hex", result == "ff")

# string -> string
result = base_convert("ff", src_alphabet="0123456789abcdef", dst_alphabet="01")
check("base_convert_hex_to_bin", result == "11111111")

# bytes -> custom base
result = base_convert(b"\x00\x01", dst_alphabet="01")
check("base_convert_bytes_to_bin", result == "1")

# zero
result = base_convert(0, dst_alphabet="ab")
check("base_convert_zero", result == "a")

# Invalid digit
try:
    base_convert("xyz", src_alphabet="abc", dst_alphabet="01")
    check("base_convert_invalid_digit", False, "should have raised")
except ValueError:
    check("base_convert_invalid_digit", True)

# ── Native Zig parity ─────────────────────────────────────────────────────

print("\n=== Native Zig Parity ===")

from hyperdjango.native import base_decode as native_decode
from hyperdjango.native import base_encode as native_encode

# Test that native and Python produce identical results
parity_values = [
    0,
    1,
    42,
    12345,
    2**32,
    2**64,
    2**128,
    2**256,
    999999999999999999999999999999,
]

for v in parity_values:
    py_enc = enc.encode(v)  # uses native under the hood
    native_enc = native_encode(v, olc_alpha)
    check(
        f"native_encode_parity_{v}",
        py_enc == native_enc,
        f"py={py_enc!r} native={native_enc!r}",
    )

    native_dec = native_decode(py_enc, olc_alpha)
    check(
        f"native_decode_parity_{v}", native_dec == v, f"expected {v}, got {native_dec}"
    )

# Base-62 parity
for v in parity_values:
    native_enc = native_encode(v, full_alpha)
    native_dec = native_decode(native_enc, full_alpha)
    check(f"native_base62_roundtrip_{v}", native_dec == v)

# ── Native performance benchmark ──────────────────────────────────────────

print("\n=== Benchmark ===")

N = 100_000
values = list(range(N))

# Encode benchmark
t0 = time.perf_counter()
for v in values:
    native_encode(v, olc_alpha)
t_encode = time.perf_counter() - t0

# Decode benchmark
encoded = [native_encode(v, olc_alpha) for v in values]
t0 = time.perf_counter()
for e in encoded:
    native_decode(e, olc_alpha)
t_decode = time.perf_counter() - t0

print(
    f"  Native encode: {N / t_encode:,.0f} ops/sec ({t_encode * 1000:.1f}ms for {N:,})"
)
print(
    f"  Native decode: {N / t_decode:,.0f} ops/sec ({t_decode * 1000:.1f}ms for {N:,})"
)
check("benchmark_encode_reasonable", t_encode < 5.0, f"took {t_encode:.1f}s")
check("benchmark_decode_reasonable", t_decode < 5.0, f"took {t_decode:.1f}s")

# Tiered benchmark: u64 vs u128 vs bigint
import secrets

N2 = 10_000

# u64 range (typical database PKs)
vals_u64 = [i * 1000 for i in range(N2)]
t0 = time.perf_counter()
for v in vals_u64:
    native_encode(v, olc_alpha)
t_u64 = time.perf_counter() - t0

# u128 range (random tokens, UUIDs)
vals_u128 = [int.from_bytes(secrets.token_bytes(16), "big") for _ in range(N2)]
t0 = time.perf_counter()
for v in vals_u128:
    native_encode(v, olc_alpha)
t_u128 = time.perf_counter() - t0

# > u128 (256-bit values)
vals_big = [int.from_bytes(secrets.token_bytes(32), "big") for _ in range(N2)]
t0 = time.perf_counter()
for v in vals_big:
    native_encode(v, olc_alpha)
t_big = time.perf_counter() - t0

print(f"\n  Tiered encode ({N2:,} each):")
print(f"    u64  (PKs):    {N2 / t_u64:>10,.0f} ops/sec  ({t_u64 * 1000:.1f}ms)")
print(f"    u128 (tokens): {N2 / t_u128:>10,.0f} ops/sec  ({t_u128 * 1000:.1f}ms)")
print(f"    >128 (bigint): {N2 / t_big:>10,.0f} ops/sec  ({t_big * 1000:.1f}ms)")
# Timing-order assertions: skip under parallel execution where CPU scheduling
# noise (50+ processes) inverts marginal differences between encode tiers.
# Proven: u128 vs bigint differs by <10%, but contention adds 100%+ jitter.
if os.environ.get("HYPER_TEST_PARALLEL") != "1":
    check("tiered_u64_fastest", t_u64 < t_u128, "u64 should be faster than u128")
    check(
        "tiered_u128_faster_than_big",
        t_u128 < t_big,
        "u128 should be faster than bigint",
    )
else:
    # Just verify they all completed without error
    check("tiered_encode_u64_ok", t_u64 > 0)
    check("tiered_encode_bigint_ok", t_big > 0)

# ── PublicIDMixin: class creation validation ───────────────────────────────

print("\n=== PublicIDMixin: Class Validation ===")


# Valid config
class GoodMixin(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 8


check("mixin_valid_config", True)
check("mixin_has_encoder", GoodMixin._public_id_encoder is not None)
check("mixin_encoder_base", GoodMixin._public_id_encoder.base == 32)
check("mixin_strategy", GoodMixin._public_id_strategy == IDStrategy.RANDOM)
check("mixin_entropy", GoodMixin._public_id_entropy_bytes == 8)


# UUID7 strategy without alphabet
class UuidMixin(PublicIDMixin):
    class PublicIDConfig:
        strategy = IDStrategy.UUID7


check("mixin_uuid7_no_alphabet", UuidMixin._public_id_encoder is None)


# UUID7 with alphabet (optional)
class UuidWithAlpha(PublicIDMixin):
    class PublicIDConfig:
        strategy = IDStrategy.UUID7
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"


check("mixin_uuid7_with_alphabet", UuidWithAlpha._public_id_encoder is not None)

# Invalid strategy
try:

    class BadStrategy(PublicIDMixin):
        class PublicIDConfig:
            alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
            strategy = "magic"

    check("mixin_invalid_strategy", False, "should have raised")
except ValueError:
    check("mixin_invalid_strategy", True)

# Missing alphabet for non-uuid7
try:

    class NoAlphabet(PublicIDMixin):
        class PublicIDConfig:
            strategy = IDStrategy.RANDOM

    check("mixin_missing_alphabet", False, "should have raised")
except ValueError:
    check("mixin_missing_alphabet", True)

# Duplicate alphabet chars
try:

    class DupeAlpha(PublicIDMixin):
        class PublicIDConfig:
            alphabet = "aab"

    check("mixin_dupe_alphabet", False, "should have raised")
except ValueError:
    check("mixin_dupe_alphabet", True)


# Different models get different encoders
class ModelA(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"


class ModelB(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "9vy4nzdZGsp5x8u3JiS1O7eM0VrDNbCKYEFafj6QLXHtmlckPgRoBTwAWhqU2I"


check("different_encoders", ModelA._public_id_encoder is not ModelB._public_id_encoder)
check(
    "different_bases", ModelA._public_id_encoder.base != ModelB._public_id_encoder.base
)

# ── PublicIDMixin: generate_public_id ──────────────────────────────────────

print("\n=== PublicIDMixin: ID Generation ===")


class MockInstance(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 10

    pk = None
    _loaded_from_db = False


inst = MockInstance()
pid = inst.generate_public_id()
check("gen_random_id", isinstance(pid, str) and len(pid) > 0)
check("gen_random_valid_chars", all(c in olc_alpha for c in pid))

# Generate many and check uniqueness
pids: set[str] = set()
for _ in range(1000):
    i = MockInstance()
    pids.add(i.generate_public_id())
check("gen_random_unique_1k", len(pids) == 1000)


# UUID7 strategy
class UuidInst(PublicIDMixin):
    class PublicIDConfig:
        strategy = IDStrategy.UUID7

    pk = None
    _loaded_from_db = False


ui = UuidInst()
uid = ui.generate_public_id()
check("gen_uuid7", len(uid) == 36 and uid.count("-") == 4)

# UUID7 strategy must actually produce a version-7 (time-ordered) UUID,
# not silently fall back to uuid4.
import uuid as _uuid_mod

check("gen_uuid7_is_v7", _uuid_mod.UUID(uid).version == 7)
# Time-ordered: two IDs generated in sequence sort by creation order.
uid_a = ui.generate_public_id()
uid_b = ui.generate_public_id()
check("gen_uuid7_monotonic", uid_a <= uid_b)


# Encoded PK strategy
class PkInst(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
        strategy = IDStrategy.ENCODED_PK
        width = 8

    pk = 12345
    _loaded_from_db = False


pk_inst = PkInst()
pk_id = pk_inst.generate_public_id()
check("gen_encoded_pk", len(pk_id) == 8)
check("gen_encoded_pk_roundtrip", enc.decode(pk_id) == 12345)


# Encoded PK without PK set
class NoPkInst(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
        strategy = IDStrategy.ENCODED_PK

    pk = None
    _loaded_from_db = False


try:
    NoPkInst().generate_public_id()
    check("gen_encoded_pk_no_pk", False, "should have raised")
except ValueError:
    check("gen_encoded_pk_no_pk", True)

# ── PublicIDMixin: decode_public_id ────────────────────────────────────────

print("\n=== PublicIDMixin: Decode ===")

decoded = ModelA.decode_public_id(enc.encode(42))
check("decode_public_id", decoded == 42)

# UUID7 without encoder
try:
    UuidMixin.decode_public_id("some-uuid")
    check("decode_uuid7_fails", False, "should have raised")
except ValueError:
    check("decode_uuid7_fails", True)

# UUID7 with encoder can decode
decoded_ua = UuidWithAlpha.decode_public_id(enc.encode(100))
check("decode_uuid7_with_alpha", decoded_ua == 100)

# ── PublicIDSerializer ─────────────────────────────────────────────────────

print("\n=== PublicIDSerializer ===")


class ArticleSerializer(PublicIDSerializer):
    title: str = SerializerField()
    content: str = SerializerField()


# Serialize: public_id becomes "id", integer pk not present
obj = {"public_id": "Xf7RgW3pMc", "title": "Hello", "content": "World", "id": 42}
s = ArticleSerializer(obj=obj)
data = s.data
check("serializer_has_id", "id" in data)
check("serializer_id_is_public", data["id"] == "Xf7RgW3pMc")
check("serializer_has_title", data["title"] == "Hello")
check("serializer_has_content", data["content"] == "World")

# "id" in input should be ignored (read_only)
s2 = ArticleSerializer(input_data={"id": "hacked", "title": "New", "content": "Post"})
check("serializer_is_valid", s2.is_valid())
check("serializer_no_id_in_validated", "id" not in s2.validated_data)
check("serializer_title_in_validated", s2.validated_data["title"] == "New")

# Many mode
objs = [
    {"public_id": "aaa", "title": "One", "content": "1"},
    {"public_id": "bbb", "title": "Two", "content": "2"},
]
s3 = ArticleSerializer(obj=objs, many=True)
check("serializer_many_count", len(s3.data) == 2)
check("serializer_many_ids", s3.data[0]["id"] == "aaa" and s3.data[1]["id"] == "bbb")


# Inheritance
class DetailedArticleSerializer(ArticleSerializer):
    author: str = SerializerField()


obj2 = {"public_id": "xyz", "title": "T", "content": "C", "author": "Alice"}
s4 = DetailedArticleSerializer(obj=obj2)
check("serializer_inherited_id", s4.data["id"] == "xyz")
check("serializer_inherited_author", s4.data["author"] == "Alice")

# ── Edge cases ─────────────────────────────────────────────────────────────

print("\n=== Edge Cases ===")

# Binary alphabet (base-2)
enc2 = BaseEncoder("01")
for v in [0, 1, 2, 7, 8, 255, 1024]:
    e = enc2.encode(v)
    d = enc2.decode(e)
    check(f"binary_roundtrip_{v}", d == v)
    # Verify it matches Python's bin() (without '0b' prefix)
    if v > 0:
        check(f"binary_matches_builtin_{v}", e == f"{v:b}")

# Hex alphabet (base-16)
enc16 = BaseEncoder("0123456789abcdef")
for v in [0, 1, 15, 16, 255, 65535]:
    e = enc16.encode(v)
    d = enc16.decode(e)
    check(f"hex_roundtrip_{v}", d == v)
    if v > 0:
        check(f"hex_matches_builtin_{v}", e == f"{v:x}")

# Very large values (512-bit)
big512 = 2**512 - 1
e512 = enc.encode(big512)
d512 = enc.decode(e512)
check("512_bit_roundtrip", d512 == big512)

# Sequential values produce different encodings
e1 = enc.encode(1000)
e2 = enc.encode(1001)
check("sequential_different", e1 != e2)


# Width config
class WidthModel(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 8
        width = 12

    pk = None
    _loaded_from_db = False


wm = WidthModel()
wid = wm.generate_public_id()
check("width_config", len(wid) >= 12, f"got {len(wid)}")

# ── Inheriting encoder from parent ─────────────────────────────────────────

print("\n=== Inheritance ===")


class BasePublicModel(PublicIDMixin):
    class PublicIDConfig:
        alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 10


# Child without its own PublicIDConfig inherits parent's
class ChildModel(BasePublicModel):
    pass


check(
    "child_inherits_encoder",
    ChildModel._public_id_encoder is BasePublicModel._public_id_encoder,
)
check("child_inherits_strategy", ChildModel._public_id_strategy == IDStrategy.RANDOM)


# Child with its own config overrides
class OverrideChild(BasePublicModel):
    class PublicIDConfig:
        alphabet = "9vy4nzdZGsp5x8u3JiS1O7eM0VrDNbCKYEFafj6QLXHtmlckPgRoBTwAWhqU2I"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 12


check(
    "override_different_encoder",
    OverrideChild._public_id_encoder is not BasePublicModel._public_id_encoder,
)
check("override_base62", OverrideChild._public_id_encoder.base == 62)

# ── OLC safe chars properties ──────────────────────────────────────────────

print("\n=== OLC Safe Chars ===")

# No vowels (prevents spelling words)
vowels = set("aeiouAEIOU")
check("olc_no_vowels", not (set(OLC_SAFE_CHARS) & vowels))

# No confusable chars
confusable = set("01lIOo")
check("olc_no_confusable", not (set(OLC_SAFE_CHARS) & confusable))

# 32 chars
check("olc_32_chars", len(OLC_SAFE_CHARS) == 32)

# ── IDManager + IDConfig (encode/signed modes, no column) ────────────────

print("\n=== IDManager: Encoded + Signed modes ===")

from hyperdjango.public_id import IDConfig, IDManager, IDMode, KeySlot

_test_alphabet = "4fqxvPFhX5wHjc3pMVRgWJ8mrG7QC692"

# Encoded mode — pure bijection with offset
enc_cfg = IDConfig(mode=IDMode.ENCODED, alphabet=_test_alphabet, offset=10000)
enc_mgr = IDManager(config=enc_cfg)
_test_enc = BaseEncoder(_test_alphabet)

# Encode/decode roundtrip with offset
encoded_42 = enc_mgr.encode(42)
check("idmgr_encoded_roundtrip", enc_mgr.decode(encoded_42) == 42)
# The actual encoded value is encode(42 + 10000) = encode(10042)
check("idmgr_encoded_offset", _test_enc.decode(encoded_42) == 10042)

# Different PKs produce different encodings
check("idmgr_encoded_different", enc_mgr.encode(1) != enc_mgr.encode(2))

# Signed mode — bijection + HMAC
signed_cfg = IDConfig(
    mode=IDMode.SIGNED,
    alphabet=_test_alphabet,
    hmac_keys=[KeySlot(key="test-secret-key-2025", offset=50000)],
)
signed_mgr = IDManager(config=signed_cfg)

# Encode produces "encoded.hmac" format
signed_42 = signed_mgr.encode(42)
check("idmgr_signed_has_separator", "." in signed_42)
parts = signed_42.split(".")
check("idmgr_signed_two_parts", len(parts) == 2)

# Decode roundtrip
check("idmgr_signed_roundtrip", signed_mgr.decode(signed_42) == 42)

# Verify valid signature
check("idmgr_signed_verify", signed_mgr.verify(signed_42))

# Tampered signature fails
tampered = parts[0] + ".0000000000000000"
check("idmgr_signed_tamper_fails", not signed_mgr.verify(tampered))

# Raw mode — integer PK directly
raw_cfg = IDConfig(mode=IDMode.RAW)
raw_mgr = IDManager(config=raw_cfg)
check("idmgr_raw_encode", raw_mgr.encode(42) == "42")
check("idmgr_raw_decode", raw_mgr.decode("42") == 42)

# Alphabet validation on IDManager creation
try:
    IDConfig(mode=IDMode.ENCODED, alphabet="")
    bad_mgr = IDManager(config=IDConfig(mode=IDMode.ENCODED, alphabet=""))
    check("idmgr_empty_alphabet_fails", False, "should have raised")
except ValueError:
    check("idmgr_empty_alphabet_fails", True)

# ── IDManager: concurrent encode cache (3.14t free-threading) ───────────────

print("\n=== IDManager: Concurrent encode cache ===")

import threading as _threading

# Small cache so eviction (popitem) races insertion under contention — the
# exact window the lock protects. Encode a large PK range from many threads
# and assert every result is correct and the cache never corrupts.
conc_cfg = IDConfig(
    mode=IDMode.SIGNED,
    alphabet=_test_alphabet,
    hmac_keys=[KeySlot(key="conc-secret-key", offset=7)],
)
conc_mgr = IDManager(config=conc_cfg)
conc_mgr._encode_cache_max = 64  # force frequent eviction

# Reference results computed single-threaded first.
_pks = list(range(2000))
_expected = {pk: conc_mgr.encode(pk) for pk in _pks}
conc_mgr._encode_cache.clear()

_conc_errors: list[str] = []


def _hammer():
    for pk in _pks:
        got = conc_mgr.encode(pk)
        if got != _expected[pk]:
            _conc_errors.append(f"pk={pk} got {got!r} != {_expected[pk]!r}")
        # Every cached value must still decode back to its pk.
        if conc_mgr.decode(got) != pk:
            _conc_errors.append(f"pk={pk} decode mismatch")


_threads = [_threading.Thread(target=_hammer) for _ in range(8)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()

check("idmgr_concurrent_encode_correct", not _conc_errors, str(_conc_errors[:3]))
check(
    "idmgr_concurrent_cache_bounded",
    len(conc_mgr._encode_cache) <= conc_mgr._encode_cache_max,
    f"cache grew to {len(conc_mgr._encode_cache)} > {conc_mgr._encode_cache_max}",
)

# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"Public ID tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

sys.exit(0 if failed == 0 else 1)
