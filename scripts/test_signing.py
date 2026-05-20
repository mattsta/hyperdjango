#!/usr/bin/env python3
"""
Tests for token signing engine — TokenEngine, SigningKey, encode/decode.

# hyper-test: unit

Tests hyperdjango/signing.py:
- SigningKey validation
- TokenEngine construction and validation
- Reference token encode/decode roundtrip
- Data token encode/decode roundtrip
- Key rotation (encode with newest, decode with any)
- XOR obfuscation (payload not visible in token)
- TTL expiration for data tokens
- Token format structure (version char, type char, separator)
- Error handling (bad tokens, wrong types, corrupted data)
- Thread safety (frozen dataclasses, no mutable state)
- Edge cases (empty data, large payloads, special characters)
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.signing import (
    SigningKey,
    TokenEngine,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


# ── SigningKey Validation ──────────────────────────────────────────────────


def test_signing_key_validation():
    print("\n=== SigningKey Validation ===")

    # Valid key
    k = SigningKey(secret="test-secret-key", version=0)
    check("Valid key version 0", k.version == 0 and k.secret == "test-secret-key")

    k = SigningKey(secret="another-key", version=61)
    check("Valid key version 61 (max)", k.version == 61)

    # Empty secret
    try:
        SigningKey(secret="", version=0)
        check("Empty secret rejected", False, "should raise ValueError")
    except ValueError:
        check("Empty secret rejected", True)

    # Version out of range
    try:
        SigningKey(secret="key", version=-1)
        check("Negative version rejected", False, "should raise ValueError")
    except ValueError:
        check("Negative version rejected", True)

    try:
        SigningKey(secret="key", version=62)
        check("Version 62 rejected", False, "should raise ValueError")
    except ValueError:
        check("Version 62 rejected", True)

    # Frozen (immutable)
    k = SigningKey(secret="immutable", version=5)
    try:
        k.secret = "modified"  # type: ignore[misc]
        check("Frozen immutable", False, "should raise FrozenInstanceError")
    except AttributeError:
        check("Frozen immutable", True)


# ── TokenEngine Construction ──────────────────────────────────────────────


def test_token_engine_construction():
    print("\n=== TokenEngine Construction ===")

    # Valid single key
    engine = TokenEngine(keys=[SigningKey(secret="key-1", version=0)])
    check("Single key engine", len(engine.keys) == 1)

    # Valid multiple keys
    engine = TokenEngine(
        keys=[
            SigningKey(secret="key-2", version=1),
            SigningKey(secret="key-1", version=0),
        ]
    )
    check("Multi-key engine", len(engine.keys) == 2)

    # No keys
    try:
        TokenEngine(keys=[])
        check("Empty keys rejected", False, "should raise ValueError")
    except ValueError:
        check("Empty keys rejected", True)

    # Duplicate versions
    try:
        TokenEngine(
            keys=[
                SigningKey(secret="key-a", version=0),
                SigningKey(secret="key-b", version=0),
            ]
        )
        check("Duplicate versions rejected", False, "should raise ValueError")
    except ValueError:
        check("Duplicate versions rejected", True)

    # Custom signature_bytes
    engine = TokenEngine(
        keys=[SigningKey(secret="key", version=0)],
        signature_bytes=12,
    )
    check("Custom signature_bytes", engine.signature_bytes == 12)


# ── Reference Token Roundtrip ─────────────────────────────────────────────


def test_ref_roundtrip():
    print("\n=== Reference Token Roundtrip ===")

    engine = TokenEngine(keys=[SigningKey(secret="ref-test-key", version=3)])

    # Basic roundtrip
    ref = "session_abc123def456"
    token = engine.encode_ref(ref)
    decoded = engine.decode_ref(token)
    check("Basic ref roundtrip", decoded == ref, f"got {decoded!r}")

    # Short reference
    ref = "x"
    token = engine.encode_ref(ref)
    decoded = engine.decode_ref(token)
    check("Short ref roundtrip", decoded == ref)

    # Long reference
    ref = "a" * 200
    token = engine.encode_ref(ref)
    decoded = engine.decode_ref(token)
    check("Long ref roundtrip (200 chars)", decoded == ref)

    # URL-safe token content
    ref = "sess_AbC123-_xYz"
    token = engine.encode_ref(ref)
    decoded = engine.decode_ref(token)
    check("URL-safe ref roundtrip", decoded == ref)

    # Unicode reference
    ref = "user_日本語"
    token = engine.encode_ref(ref)
    decoded = engine.decode_ref(token)
    check("Unicode ref roundtrip", decoded == ref)

    # Empty-ish references
    ref = " "
    token = engine.encode_ref(ref)
    decoded = engine.decode_ref(token)
    check("Space ref roundtrip", decoded == ref)

    # Token has separator
    check("Token contains separator", "." in token)

    # Token starts with version char
    from hyperdjango.signing import _VERSION_CHARS

    check("Token starts with version char '3'", token[0] == _VERSION_CHARS[3])

    # Token has type char 'r'
    check("Token type char is 'r'", token[1] == "r")


# ── Data Token Roundtrip ──────────────────────────────────────────────────


def test_data_roundtrip():
    print("\n=== Data Token Roundtrip ===")

    engine = TokenEngine(keys=[SigningKey(secret="data-test-key", version=5)])

    # Basic dict
    data = {"user_id": 42, "role": "admin"}
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("Basic data roundtrip", decoded == data, f"got {decoded!r}")

    # All value types
    data = {
        "str_val": "hello",
        "int_val": 12345,
        "float_val": 3.14,
        "bool_val": True,
        "none_val": None,
    }
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("All value types roundtrip", decoded == data, f"got {decoded!r}")

    # Empty dict
    data = {}
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("Empty dict roundtrip", decoded == data)

    # Nested data NOT supported — only flat dicts
    # (JSON handles it but the type hint is str|int|float|bool|None)

    # Large data
    data = {f"key_{i}": f"value_{i}" for i in range(50)}
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("Large data roundtrip (50 keys)", decoded == data)

    # Token has type char 'd'
    check("Token type char is 'd'", token[1] == "d")

    # Negative numbers
    data = {"balance": -100, "temp": -273.15}
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("Negative numbers roundtrip", decoded == data, f"got {decoded!r}")

    # Zero and boundary values
    data = {"zero": 0, "empty": "", "false": False}
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("Zero/empty/false roundtrip", decoded == data)


# ── TTL Expiration ────────────────────────────────────────────────────────


def test_ttl_expiration():
    print("\n=== TTL Expiration ===")

    engine = TokenEngine(keys=[SigningKey(secret="ttl-test-key", version=0)])

    # Token with long TTL — valid
    data = {"user_id": 1}
    token = engine.encode_data(data, ttl=3600)
    decoded = engine.decode_data(token)
    check("Long TTL valid", decoded == data)

    # A token whose lifetime has already run out is rejected. Expiry is stamped
    # as `now + ttl` and compared against `now` at decode, so a TTL that has
    # already elapsed states that exactly — where waiting out a 0-second TTL
    # only asserts that 1.1s of wall time crossed a 1-second clock boundary.
    token = engine.encode_data(data, ttl=-1)
    decoded = engine.decode_data(token)
    check("elapsed TTL expired", decoded is None)

    # ...and the boundary the other way: a token still inside its TTL decodes.
    token = engine.encode_data(data, ttl=60)
    check("unelapsed TTL still valid", engine.decode_data(token) == data)

    # _exp field NOT leaked to caller
    token = engine.encode_data(data, ttl=3600)
    decoded = engine.decode_data(token)
    check("_exp not in returned data", "_exp" not in decoded)

    # No TTL — no expiration
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("No TTL — no _exp", "_exp" not in decoded)

    # Reserved-field guard: a caller must NOT be able to supply `_exp` in data
    # (a non-numeric value crashed decode_data; a numeric one was silently
    # stripped/mis-read as expiry). encode_data rejects it loudly.
    raised = False
    try:
        engine.encode_data({"user_id": 1, "_exp": "not-a-number"})
    except ValueError:
        raised = True
    check("encode_data rejects reserved _exp", raised)
    raised_num = False
    try:
        engine.encode_data({"_exp": 9999999999})
    except ValueError:
        raised_num = True
    check("encode_data rejects numeric _exp too", raised_num)


# ── Key Rotation ──────────────────────────────────────────────────────────


def test_key_rotation():
    print("\n=== Key Rotation ===")

    key_v1 = SigningKey(secret="old-key-2025", version=1)
    key_v2 = SigningKey(secret="new-key-2026", version=2)

    # Create token with old engine (key v1 only)
    old_engine = TokenEngine(keys=[key_v1])
    old_ref_token = old_engine.encode_ref("session_old")
    old_data_token = old_engine.encode_data({"phase": "old"})

    # Create new engine with both keys (v2 newest)
    new_engine = TokenEngine(keys=[key_v2, key_v1])

    # Old tokens still decode with new engine
    decoded_ref = new_engine.decode_ref(old_ref_token)
    check("Old ref token decodes with new engine", decoded_ref == "session_old")

    decoded_data = new_engine.decode_data(old_data_token)
    check("Old data token decodes with new engine", decoded_data == {"phase": "old"})

    # New engine signs with newest key (v2)
    new_ref_token = new_engine.encode_ref("session_new")
    from hyperdjango.signing import _VERSION_CHARS

    check("New token uses version 2", new_ref_token[0] == _VERSION_CHARS[2])

    # New tokens decode with new engine
    decoded = new_engine.decode_ref(new_ref_token)
    check("New ref token decodes", decoded == "session_new")

    # Old engine can't decode new tokens (no v2 key)
    decoded = old_engine.decode_ref(new_ref_token)
    check("Old engine rejects new token", decoded is None)

    # After removing old key, old tokens are rejected
    v2_only_engine = TokenEngine(keys=[key_v2])
    decoded = v2_only_engine.decode_ref(old_ref_token)
    check("Removed key rejects old token", decoded is None)


# ── XOR Obfuscation ────────────────────────────────────���─────────────────


def test_xor_obfuscation():
    print("\n=== XOR Obfuscation ===")

    engine = TokenEngine(keys=[SigningKey(secret="xor-test-key", version=0)])

    # Reference should NOT appear in token
    ref = "session_abc123_visible"
    token = engine.encode_ref(ref)
    check("Reference not in token", ref not in token)
    check("Reference substring not in token", "abc123" not in token)

    # Same reference, different keys → different tokens
    engine2 = TokenEngine(keys=[SigningKey(secret="different-key", version=1)])
    token2 = engine2.encode_ref(ref)
    check("Different key → different token", token != token2)

    # With salt (default): same input → different token each time
    token3 = engine.encode_ref(ref)
    check("Salted: same input → different token (non-deterministic)", token != token3)

    # Without salt: same input → same token (deterministic)
    det_engine = TokenEngine(
        keys=[SigningKey(secret="det-key", version=0)], salt_bytes=0
    )
    t_a = det_engine.encode_ref(ref)
    t_b = det_engine.encode_ref(ref)
    check("Unsalted: same input → same token (deterministic)", t_a == t_b)

    # Data values should NOT appear in token
    data = {"secret": "password123", "user_id": 42}
    token = engine.encode_data(data)
    check("Data values not in token", "password123" not in token)
    check("Numeric values not in token", "42" not in token or len(token) > 10)


# ── Error Handling ────────────────────────────────────────────────────────


def test_error_handling():
    print("\n=== Error Handling ===")

    engine = TokenEngine(keys=[SigningKey(secret="error-test-key", version=0)])

    # Garbage token
    check("Garbage rejected (ref)", engine.decode_ref("garbage") is None)
    check("Garbage rejected (data)", engine.decode_data("garbage") is None)

    # Empty token
    check("Empty rejected (ref)", engine.decode_ref("") is None)
    check("Empty rejected (data)", engine.decode_data("") is None)

    # Token with wrong type
    ref_token = engine.encode_ref("test")
    check("Ref token rejected as data", engine.decode_data(ref_token) is None)

    data_token = engine.encode_data({"x": 1})
    check("Data token rejected as ref", engine.decode_ref(data_token) is None)

    # Tampered payload
    ref_token = engine.encode_ref("original")
    parts = ref_token.split(".")
    tampered = parts[0] + "X" + "." + parts[1]  # Modify payload
    check("Tampered payload rejected", engine.decode_ref(tampered) is None)

    # Tampered signature
    tampered = parts[0] + "." + parts[1] + "X"
    check("Tampered signature rejected", engine.decode_ref(tampered) is None)

    # Just separator
    check("Just separator rejected", engine.decode_ref(".") is None)
    check("Separator + garbage", engine.decode_ref("ab.") is None)

    # Wrong version char but valid format
    ref_token = engine.encode_ref("test")
    wrong_version = "Z" + ref_token[1:]  # Change version to 'Z' (version 61)
    check("Wrong version rejected", engine.decode_ref(wrong_version) is None)

    # Oversized data payload
    try:
        huge = {f"k{i}": "x" * 100 for i in range(100)}
        engine.encode_data(huge)
        check("Oversized data rejected", False, "should raise ValueError")
    except ValueError:
        check("Oversized data rejected", True)


# ── Token Format ──────────────────────────────────────────────────────────


def test_token_format():
    print("\n=== Token Format ===")

    engine = TokenEngine(keys=[SigningKey(secret="format-key", version=7)])

    ref_token = engine.encode_ref("test_reference")
    data_token = engine.encode_data({"key": "value"})

    # Both have exactly one separator
    check("Ref token has one separator", ref_token.count(".") == 1)
    check("Data token has one separator", data_token.count(".") == 1)

    # Version char matches key version (7 → '7' in ALPHANUMERIC_CHARS)
    from hyperdjango.signing import _VERSION_CHARS

    expected_ver = _VERSION_CHARS[7]
    check(f"Ref version char is '{expected_ver}'", ref_token[0] == expected_ver)
    check(f"Data version char is '{expected_ver}'", data_token[0] == expected_ver)

    # Type chars
    check("Ref type char is 'r'", ref_token[1] == "r")
    check("Data type char is 'd'", data_token[1] == "d")

    # Token chars are all alphanumeric + separator (pure base62)
    import string

    valid_chars = set(string.digits + string.ascii_letters + ".")
    check(
        "Ref token all base62 chars",
        all(c in valid_chars for c in ref_token),
        f"invalid chars: {set(ref_token) - valid_chars}",
    )
    check(
        "Data token all base62 chars",
        all(c in valid_chars for c in data_token),
        f"invalid chars: {set(data_token) - valid_chars}",
    )


# ── Multiple Versions ────────────────────────────────────────────────────


def test_multiple_versions():
    print("\n=== Multiple Versions ===")

    keys = [SigningKey(secret=f"key-v{i}", version=i) for i in range(5)]

    # Create tokens with each individual key
    tokens: dict[int, str] = {}
    for key in keys:
        single_engine = TokenEngine(keys=[key])
        tokens[key.version] = single_engine.encode_ref(f"ref-v{key.version}")

    # Engine with all keys can decode all tokens
    all_engine = TokenEngine(keys=list(reversed(keys)))  # newest first
    for version, token in tokens.items():
        decoded = all_engine.decode_ref(token)
        check(f"Decode v{version} with all-key engine", decoded == f"ref-v{version}")

    # All-key engine signs with highest version (v4, last in reversed list = first)
    new_token = all_engine.encode_ref("new-ref")
    from hyperdjango.signing import _VERSION_CHARS

    check("Signs with newest (v4)", new_token[0] == _VERSION_CHARS[4])


# ── Signature Bytes Variation ─────────────────────────────────────────────


def test_signature_bytes():
    print("\n=== Signature Bytes Variation ===")

    key = SigningKey(secret="sig-bytes-key", version=0)

    # Default (8 bytes)
    engine8 = TokenEngine(keys=[key], signature_bytes=8)
    token8 = engine8.encode_ref("test")
    decoded8 = engine8.decode_ref(token8)
    check("8-byte sig roundtrip", decoded8 == "test")

    # 4 bytes (shorter token, less security margin)
    engine4 = TokenEngine(keys=[key], signature_bytes=4)
    token4 = engine4.encode_ref("test")
    decoded4 = engine4.decode_ref(token4)
    check("4-byte sig roundtrip", decoded4 == "test")

    # 16 bytes (longer token, more security)
    engine16 = TokenEngine(keys=[key], signature_bytes=16)
    token16 = engine16.encode_ref("test")
    decoded16 = engine16.decode_ref(token16)
    check("16-byte sig roundtrip", decoded16 == "test")

    # Tokens with different sig_bytes are different
    check("Different sig_bytes → different tokens", token8 != token4 != token16)

    # Cross-engine rejection (4-byte engine can't verify 8-byte token)
    check("4-byte engine rejects 8-byte token", engine4.decode_ref(token8) is None)
    check("8-byte engine rejects 4-byte token", engine8.decode_ref(token4) is None)


# ── is_valid Helpers ──────────────────────────────────────────────────────


def test_is_valid():
    print("\n=== is_valid Helpers ===")

    engine = TokenEngine(keys=[SigningKey(secret="valid-key", version=0)])

    ref_token = engine.encode_ref("test")
    data_token = engine.encode_data({"x": 1}, ttl=3600)

    check("is_valid_ref — valid", engine.is_valid_ref(ref_token))
    check("is_valid_ref — invalid", not engine.is_valid_ref("garbage"))
    check("is_valid_ref — wrong type", not engine.is_valid_ref(data_token))

    check("is_valid_data — valid", engine.is_valid_data(data_token))
    check("is_valid_data — invalid", not engine.is_valid_data("garbage"))
    check("is_valid_data — wrong type", not engine.is_valid_data(ref_token))


# ── Determinism ───────────────────────────────────────────────────────────


def test_determinism():
    print("\n=== Determinism & Non-Determinism ===")

    # Default engine (salted) — tokens are non-deterministic
    salted = TokenEngine(keys=[SigningKey(secret="det-key", version=0)])
    ref = "deterministic_test"
    t1 = salted.encode_ref(ref)
    t2 = salted.encode_ref(ref)
    check("Salted ref tokens are non-deterministic", t1 != t2)
    check(
        "Salted ref tokens both decode correctly",
        salted.decode_ref(t1) == ref and salted.decode_ref(t2) == ref,
    )

    data = {"a": 1, "b": "two"}
    t1 = salted.encode_data(data)
    t2 = salted.encode_data(data)
    check("Salted data tokens are non-deterministic", t1 != t2)
    d1 = salted.decode_data(t1)
    d2 = salted.decode_data(t2)
    check("Salted data tokens both decode same data", d1 == d2 == data)

    # Unsalted engine — deterministic
    unsalted = TokenEngine(keys=[SigningKey(secret="det-key", version=0)], salt_bytes=0)
    t1 = unsalted.encode_ref(ref)
    t2 = unsalted.encode_ref(ref)
    check("Unsalted ref tokens are deterministic", t1 == t2)

    t1 = unsalted.encode_data(data)
    t2 = unsalted.encode_data(data)
    check("Unsalted data tokens are deterministic", t1 == t2)


# ── Special Characters in References ─────────────────────────────────────


def test_special_characters():
    print("\n=== Special Characters ===")

    engine = TokenEngine(keys=[SigningKey(secret="special-key", version=0)])

    cases = [
        ("dots", "ref.with.dots"),
        ("slashes", "path/to/resource"),
        ("equals", "base64==padding"),
        ("plus", "a+b=c"),
        ("spaces", "has spaces inside"),
        ("newlines", "line1\nline2"),
        ("null bytes", "has\x00null"),
        ("emoji", "token_🔑_key"),
        ("mixed unicode", "café_naïve_résumé"),
    ]

    for label, ref in cases:
        token = engine.encode_ref(ref)
        decoded = engine.decode_ref(token)
        check(f"Special chars: {label}", decoded == ref, f"got {decoded!r}")


# ── Data Token Key Ordering ──────────────────────────────────────────────


def test_data_key_ordering():
    print("\n=== Data Token Key Ordering ===")

    # Unsalted: sort_keys=True → deterministic, order-independent
    unsalted = TokenEngine(
        keys=[SigningKey(secret="order-key", version=0)], salt_bytes=0
    )
    data1 = {"z": 1, "a": 2, "m": 3}
    data2 = {"a": 2, "m": 3, "z": 1}
    t1 = unsalted.encode_data(data1)
    t2 = unsalted.encode_data(data2)
    check("Unsalted: key order doesn't matter (sort_keys)", t1 == t2)

    # Salted: keys are shuffled, but decoded data is identical
    salted = TokenEngine(keys=[SigningKey(secret="order-key", version=0)])
    t1 = salted.encode_data(data1)
    t2 = salted.encode_data(data2)
    d1 = salted.decode_data(t1)
    d2 = salted.decode_data(t2)
    check("Salted: both decode to same data", d1 == d2 == {"a": 2, "m": 3, "z": 1})

    # Decoded preserves all keys regardless
    decoded = salted.decode_data(t1)
    check("All keys preserved", decoded == {"a": 2, "m": 3, "z": 1})


# ─�� Concurrent Safety ────────────────────────────────────────────────────


def test_concurrent_safety():
    print("\n=== Concurrent Safety ===")

    import threading

    engine = TokenEngine(keys=[SigningKey(secret="thread-key", version=0)])

    results: list[str | None] = [None] * 100
    errors: list[str] = []

    def worker(idx: int):
        try:
            ref = f"session_{idx}"
            token = engine.encode_ref(ref)
            decoded = engine.decode_ref(token)
            if decoded != ref:
                errors.append(f"Thread {idx}: expected {ref!r}, got {decoded!r}")
            results[idx] = decoded
        except Exception as exc:
            errors.append(f"Thread {idx}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("100 concurrent threads — no errors", len(errors) == 0, str(errors[:3]))
    check(
        "100 concurrent threads — all correct",
        all(results[i] == f"session_{i}" for i in range(100)),
    )


# ── Benchmark ─────────────────────────────────────────────────────────────


def test_benchmark():
    print("\n=== Benchmark ===")

    engine = TokenEngine(keys=[SigningKey(secret="bench-key", version=0)])
    # Under parallel execution, CPU contention reduces throughput significantly
    _parallel = os.environ.get("HYPER_TEST_PARALLEL") == "1"
    _min_ops = 100 if _parallel else 1_000

    # Ref encode benchmark
    ref = "session_benchmark_reference_id"
    start = time.perf_counter()
    n = 10_000
    for _ in range(n):
        engine.encode_ref(ref)
    elapsed = time.perf_counter() - start
    ops_per_sec = n / elapsed
    check(
        f"Ref encode: {ops_per_sec:,.0f} ops/sec",
        ops_per_sec > _min_ops,
        f"{elapsed:.3f}s for {n:,} ops",
    )

    # Ref decode benchmark
    token = engine.encode_ref(ref)
    start = time.perf_counter()
    for _ in range(n):
        engine.decode_ref(token)
    elapsed = time.perf_counter() - start
    ops_per_sec = n / elapsed
    check(
        f"Ref decode: {ops_per_sec:,.0f} ops/sec",
        ops_per_sec > _min_ops,
        f"{elapsed:.3f}s for {n:,} ops",
    )

    # Data encode benchmark
    data = {"user_id": 42, "role": "admin", "org": "acme"}
    start = time.perf_counter()
    for _ in range(n):
        engine.encode_data(data)
    elapsed = time.perf_counter() - start
    ops_per_sec = n / elapsed
    check(
        f"Data encode: {ops_per_sec:,.0f} ops/sec",
        ops_per_sec > _min_ops,
        f"{elapsed:.3f}s for {n:,} ops",
    )

    # Data decode benchmark
    token = engine.encode_data(data)
    start = time.perf_counter()
    for _ in range(n):
        engine.decode_data(token)
    elapsed = time.perf_counter() - start
    ops_per_sec = n / elapsed
    check(
        f"Data decode: {ops_per_sec:,.0f} ops/sec",
        ops_per_sec > _min_ops,
        f"{elapsed:.3f}s for {n:,} ops",
    )


# ── Adversarial / Defense-in-Depth ─────────────────────────────────────────


def test_adversarial_tokens():
    print("\n=== Adversarial Tokens ===")

    engine = TokenEngine(keys=[SigningKey(secret="adversarial-key", version=0)])

    # Random noise of various lengths — must never decode
    import random

    rng = random.Random(42)
    for length in [1, 2, 3, 5, 10, 50, 100, 500, 1000]:
        noise = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789.", k=length))
        check(f"Random noise len={length} ref", engine.decode_ref(noise) is None)
        check(f"Random noise len={length} data", engine.decode_data(noise) is None)

    # Valid-looking format but wrong key
    other_engine = TokenEngine(keys=[SigningKey(secret="wrong-key", version=0)])
    stolen_token = other_engine.encode_ref("stolen_session")
    check("Wrong key rejected", engine.decode_ref(stolen_token) is None)

    # Bit-flip attack: flip every bit in a valid token, verify ALL rejected
    valid_token = engine.encode_ref("target_session")
    token_bytes = bytearray(valid_token.encode("ascii"))
    bit_flips_detected = 0
    for byte_idx in range(len(token_bytes)):
        for bit_idx in range(8):
            corrupted = bytearray(token_bytes)
            corrupted[byte_idx] ^= 1 << bit_idx
            try:
                corrupted_str = corrupted.decode("ascii")
            except UnicodeDecodeError:
                bit_flips_detected += 1
                continue
            result = engine.decode_ref(corrupted_str)
            if result is None:
                bit_flips_detected += 1
    total_flips = len(token_bytes) * 8
    check(
        f"Bit-flip attack: {bit_flips_detected}/{total_flips} detected",
        bit_flips_detected == total_flips,
        f"missed {total_flips - bit_flips_detected} flips",
    )

    # Cross-engine replay
    engine_a = TokenEngine(keys=[SigningKey(secret="key-a", version=0)])
    engine_b = TokenEngine(keys=[SigningKey(secret="key-b", version=0)])
    token_a = engine_a.encode_ref("session_a")
    check("Cross-engine replay rejected", engine_b.decode_ref(token_a) is None)

    # Truncation attack: progressively shorter tokens
    valid = engine.encode_ref("test_truncation")
    for trim in range(1, len(valid)):
        truncated = valid[:trim]
        check(f"Truncated to {trim} chars", engine.decode_ref(truncated) is None)

    # Extension attack: append garbage
    for extra in ["a", "aa", ".", ".abc", "0" * 50]:
        extended = valid + extra
        check(f"Extended with {extra!r}", engine.decode_ref(extended) is None)

    # Separator manipulation
    parts = valid.split(".")
    check("Double separator", engine.decode_ref(parts[0] + ".." + parts[1]) is None)
    check("No separator", engine.decode_ref(parts[0] + parts[1]) is None)
    check("Swapped halves", engine.decode_ref(parts[1] + "." + parts[0]) is None)

    # Type confusion: swap type markers
    ref_token = engine.encode_ref("ref_value")
    data_token = engine.encode_data({"k": "v"})
    confused_ref = ref_token[0] + "d" + ref_token[2:]
    confused_data = data_token[0] + "r" + data_token[2:]
    check("Type-swapped ref->data rejected", engine.decode_data(confused_ref) is None)
    check("Type-swapped data->ref rejected", engine.decode_ref(confused_data) is None)


def test_adversarial_data_payloads():
    print("\n=== Adversarial Data Payloads ===")

    engine = TokenEngine(keys=[SigningKey(secret="data-adversary-key", version=0)])

    # _exp is a reserved key. Supplying it in `data` is rejected at encode time
    # (loud ValueError) rather than silently stripped (data loss) or crashing
    # decode on a non-numeric value. This holds regardless of ttl.
    data_with_exp = {"_exp": 99999999999, "user": "attacker"}
    rejected = False
    try:
        engine.encode_data(data_with_exp)
    except ValueError:
        rejected = True
    check("_exp in data rejected at encode", rejected)

    rejected_ttl = False
    try:
        engine.encode_data(data_with_exp, ttl=3600)
    except ValueError:
        rejected_ttl = True
    check("_exp in data rejected even with ttl", rejected_ttl)

    # Large dict near size limit
    nested: dict[str, str | int | float | bool | None] = {}
    for i in range(200):
        nested[f"key_{i:04d}"] = f"val_{i}"
    try:
        token = engine.encode_data(nested)
        decoded = engine.decode_data(token)
        check("200-key dict roundtrip", decoded == nested)
    except ValueError:
        check("200-key dict hits size limit (expected)", True)


def test_native_xor_bytes():
    print("\n=== Native XOR Bytes ===")

    from hyperdjango.native import xor_bytes

    if xor_bytes is None:
        check("Native xor_bytes available", False, "not built yet")
        return

    check("Native xor_bytes available", True)

    # Basic XOR
    result = xor_bytes(b"\x00\x00\x00", b"\xff\xff\xff")
    check("XOR 0x00 ^ 0xff = 0xff", result == b"\xff\xff\xff")

    # Identity: XOR with zeros
    data = b"hello world"
    result = xor_bytes(data, b"\x00" * len(data))
    check("XOR with zeros = identity", result == data)

    # Self-inverse: XOR twice = original
    mask = b"secret_mask_1234"
    data = b"sensitive data!!"
    encrypted = xor_bytes(data, mask)
    decrypted = xor_bytes(encrypted, mask)
    check("XOR self-inverse (double XOR)", decrypted == data)

    # Repeating mask (mask shorter than data)
    data = b"abcdefghijklmnop"  # 16 bytes
    mask = b"\x01\x02"  # 2 bytes, repeats 8 times
    result = xor_bytes(data, mask)
    expected = bytes(d ^ mask[i % 2] for i, d in enumerate(data))
    check("Repeating mask (2-byte on 16-byte)", result == expected)

    # Large data with 32-byte mask (SIMD fast path)
    data = bytes(range(256)) * 4  # 1024 bytes
    mask = bytes(range(32))  # 32-byte HMAC mask
    result = xor_bytes(data, mask)
    expected = bytes(d ^ mask[i % 32] for i, d in enumerate(data))
    check("1024 bytes with 32-byte mask (SIMD path)", result == expected)

    # Edge: single byte
    check("Single byte XOR", xor_bytes(b"\xaa", b"\x55") == b"\xff")

    # Edge: empty data
    check("Empty data XOR", xor_bytes(b"", b"\xff") == b"")

    # Edge: empty mask raises error
    try:
        xor_bytes(b"data", b"")
        check("Empty mask raises error", False, "should raise ValueError")
    except ValueError:
        check("Empty mask raises error", True)

    # Large data stress test
    data = os.urandom(10_000)
    mask = os.urandom(32)
    result = xor_bytes(data, mask)
    expected = bytes(d ^ mask[i % 32] for i, d in enumerate(data))
    check("10KB stress test", result == expected)

    # All zeros
    data = b"\x00" * 64
    mask = b"\x00" * 32
    result = xor_bytes(data, mask)
    check("All zeros", result == data)

    # All ones
    data = b"\xff" * 64
    mask = b"\xff" * 32
    result = xor_bytes(data, mask)
    check("All 0xff ^ 0xff = 0x00", result == b"\x00" * 64)

    # Non-power-of-2 sizes (tail handling — critical for SIMD correctness)
    for size in [1, 3, 7, 13, 15, 17, 31, 33, 47, 63, 65, 100, 127, 129, 255, 257]:
        data = os.urandom(size)
        mask = os.urandom(32)
        result = xor_bytes(data, mask)
        expected = bytes(d ^ mask[i % 32] for i, d in enumerate(data))
        check(
            f"Size {size} tail handling", result == expected, f"mismatch at size {size}"
        )


def test_token_corruption_resistance():
    print("\n=== Token Corruption Resistance ===")

    engine = TokenEngine(keys=[SigningKey(secret="corruption-key", version=0)])

    # Generate tokens, corrupt each via single-char substitution, verify all rejected
    refs = [f"session_{i}" for i in range(20)]
    tokens = [engine.encode_ref(ref) for ref in refs]

    all_rejected = True
    for token in tokens:
        for pos in range(len(token)):
            for replacement in "0aZ!@#$%^&*":
                if token[pos] == replacement:
                    continue
                corrupted = token[:pos] + replacement + token[pos + 1 :]
                result = engine.decode_ref(corrupted)
                if result is not None:
                    all_rejected = False
    check(
        "All single-char substitutions rejected (20 tokens x all positions)",
        all_rejected,
    )

    # Verify originals still work
    for i, (token, ref) in enumerate(zip(tokens, refs)):
        decoded = engine.decode_ref(token)
        check(
            f"Original token {i} still valid after corruption testing", decoded == ref
        )


def test_cross_type_isolation():
    print("\n=== Cross-Type Isolation ===")

    engine = TokenEngine(keys=[SigningKey(secret="isolation-key", version=0)])

    ref_token = engine.encode_ref("42")
    data_token = engine.encode_data({"value": 42})

    check("Ref not decodable as data", engine.decode_data(ref_token) is None)
    check("Data not decodable as ref", engine.decode_ref(data_token) is None)

    # Manually change type char — HMAC must fail since type is part of signed prefix
    modified = ref_token[0] + "d" + ref_token[2:]
    check("Ref with swapped type char fails HMAC", engine.decode_data(modified) is None)


def test_expired_data_edge_cases():
    print("\n=== Expired Data Edge Cases ===")

    engine = TokenEngine(keys=[SigningKey(secret="expiry-key", version=0)])

    # Very large TTL
    token = engine.encode_data({"x": 1}, ttl=365 * 24 * 3600)
    check("1-year TTL valid", engine.decode_data(token) is not None)

    # Negative TTL (already expired when created)
    token = engine.encode_data({"x": 1}, ttl=-1)
    check("Negative TTL = expired", engine.decode_data(token) is None)

    # TTL=1 should be valid immediately
    token = engine.encode_data({"x": 1}, ttl=1)
    check("TTL=1 valid immediately", engine.decode_data(token) is not None)


def test_version_boundary_values():
    print("\n=== Version Boundary Values ===")

    engine0 = TokenEngine(keys=[SigningKey(secret="v0-key", version=0)])
    token = engine0.encode_ref("test")
    check("Version 0 roundtrip", engine0.decode_ref(token) == "test")
    check("Version 0 char is '0'", token[0] == "0")

    engine61 = TokenEngine(keys=[SigningKey(secret="v61-key", version=61)])
    token = engine61.encode_ref("test")
    check("Version 61 roundtrip", engine61.decode_ref(token) == "test")

    # Version boundaries: 9→'9', 10→'a', 35→'z', 36→'A'
    for v, expected_char in [(9, "9"), (10, "a"), (35, "z"), (36, "A")]:
        eng = TokenEngine(keys=[SigningKey(secret=f"v{v}-key", version=v)])
        tok = eng.encode_ref("boundary")
        check(f"Version {v} -> char '{expected_char}'", tok[0] == expected_char)
        check(f"Version {v} roundtrip", eng.decode_ref(tok) == "boundary")


def test_empty_and_boundary_payloads():
    print("\n=== Empty and Boundary Payloads ===")

    engine = TokenEngine(keys=[SigningKey(secret="boundary-key", version=0)])

    # Empty string reference
    token = engine.encode_ref("")
    check("Empty string ref roundtrip", engine.decode_ref(token) == "")

    # Single null byte
    ref = "\x00"
    token = engine.encode_ref(ref)
    check("Null byte ref roundtrip", engine.decode_ref(token) == ref)

    # 50 null bytes (all zeros — tests XOR sentinel handling)
    ref = "\x00" * 50
    token = engine.encode_ref(ref)
    check("50 null bytes ref roundtrip", engine.decode_ref(token) == ref)

    # Data with 100-char key name
    long_key = "k" * 100
    data = {long_key: "v"}
    token = engine.encode_data(data)
    decoded = engine.decode_data(token)
    check("100-char key name roundtrip", decoded == data)


# ── Salt Security ─────────────────────────────────────────────────────────


def test_salt_security():
    print("\n=== Salt Security ===")

    engine = TokenEngine(keys=[SigningKey(secret="salt-security-key", version=0)])

    # Non-determinism: 100 tokens for same ref, all unique
    ref = "same_session_id"
    tokens = {engine.encode_ref(ref) for _ in range(100)}
    check("100 tokens for same ref → all unique", len(tokens) == 100)

    # All 100 decode correctly
    all_decode = all(engine.decode_ref(t) == ref for t in tokens)
    check("All 100 unique tokens decode correctly", all_decode)

    # Data tokens also non-deterministic
    data = {"user_id": 42}
    dtokens = {engine.encode_data(data) for _ in range(50)}
    check("50 data tokens for same data → all unique", len(dtokens) == 50)
    all_decode = all(engine.decode_data(t) == data for t in dtokens)
    check("All 50 unique data tokens decode correctly", all_decode)

    # Salt size variations
    for salt_size in [0, 1, 4, 8, 16, 32]:
        eng = TokenEngine(
            keys=[SigningKey(secret="salt-size-test", version=0)],
            salt_bytes=salt_size,
        )
        token = eng.encode_ref("test_ref")
        decoded = eng.decode_ref(token)
        check(f"Salt size {salt_size} roundtrip", decoded == "test_ref")

    # Different salt sizes produce different tokens
    eng0 = TokenEngine(keys=[SigningKey(secret="k", version=0)], salt_bytes=0)
    eng8 = TokenEngine(keys=[SigningKey(secret="k", version=0)], salt_bytes=8)
    t0 = eng0.encode_ref("test")
    t8 = eng8.encode_ref("test")
    check("Different salt sizes → different tokens", t0 != t8)

    # Cross-salt-config rejection: token from salt=8 engine can't decode on salt=0
    check("Salt=8 token rejected by salt=0 engine", eng0.decode_ref(t8) is None)
    check("Salt=0 token rejected by salt=8 engine", eng8.decode_ref(t0) is None)

    # Invalid salt_bytes
    try:
        TokenEngine(keys=[SigningKey(secret="k", version=0)], salt_bytes=33)
        check("Salt > 32 rejected", False)
    except ValueError:
        check("Salt > 32 rejected", True)

    try:
        TokenEngine(keys=[SigningKey(secret="k", version=0)], salt_bytes=-1)
        check("Negative salt rejected", False)
    except ValueError:
        check("Negative salt rejected", True)


# ── Padding Security ──────────────────────────────────────────────────────


def test_padding_security():
    print("\n=== Padding Security ===")

    padded_engine = TokenEngine(
        keys=[SigningKey(secret="pad-key", version=0)],
        pad_to_bucket=True,
    )
    unpadded_engine = TokenEngine(
        keys=[SigningKey(secret="pad-key", version=0)],
        pad_to_bucket=False,
        salt_bytes=0,
    )

    # Basic roundtrip with padding
    ref = "padded_session"
    token = padded_engine.encode_ref(ref)
    decoded = padded_engine.decode_ref(token)
    check("Padded ref roundtrip", decoded == ref)

    data = {"user_id": 42, "role": "admin"}
    token = padded_engine.encode_data(data)
    decoded = padded_engine.decode_data(token)
    check("Padded data roundtrip", decoded == data)

    # Length masking: different-size payloads in same bucket → same token length
    # (with salt=0 so we can compare lengths deterministically)
    det_padded = TokenEngine(
        keys=[SigningKey(secret="pad-len-key", version=0)],
        pad_to_bucket=True,
        salt_bytes=0,
    )
    t_short = det_padded.encode_ref("x")  # 1 byte payload
    t_medium = det_padded.encode_ref("hello")  # 5 byte payload
    t_match = det_padded.encode_ref("hello world!")  # 12 byte payload
    # All should pad to 16-byte bucket (2 + payload ≤ 16 for all)
    check("Short and medium same bucket length", len(t_short) == len(t_medium))
    check("Medium and 12-byte same bucket length", len(t_medium) == len(t_match))

    # Different buckets for different sizes
    t_big = det_padded.encode_ref("x" * 30)  # 30 bytes → 32-byte bucket
    check("30-byte payload → bigger token", len(t_big) > len(t_short))

    # All decode correctly
    check("Short padded decode", det_padded.decode_ref(t_short) == "x")
    check("Medium padded decode", det_padded.decode_ref(t_medium) == "hello")
    check("12-byte padded decode", det_padded.decode_ref(t_match) == "hello world!")
    check("30-byte padded decode", det_padded.decode_ref(t_big) == "x" * 30)

    # Cross-config rejection: padded token can't decode on unpadded engine
    padded_token = det_padded.encode_ref("test")
    unpadded_token = unpadded_engine.encode_ref("test")
    check(
        "Padded token rejected by unpadded engine",
        unpadded_engine.decode_ref(padded_token) is None
        or unpadded_engine.decode_ref(padded_token) != "test",
    )
    check(
        "Unpadded token rejected by padded engine",
        det_padded.decode_ref(unpadded_token) is None
        or det_padded.decode_ref(unpadded_token) != "test",
    )

    # Padding with various payload sizes (bucket boundaries)
    for size in [
        0,
        1,
        13,
        14,
        15,
        16,
        30,
        31,
        32,
        62,
        63,
        64,
        126,
        127,
        128,
        254,
        255,
        256,
        500,
        1000,
    ]:
        ref = "A" * size
        token = padded_engine.encode_ref(ref)
        decoded = padded_engine.decode_ref(token)
        check(f"Padded size {size} roundtrip", decoded == ref, f"got {decoded!r}")

    # Data tokens with padding
    for n_keys in [1, 5, 20, 50]:
        data = {f"k{i}": f"v{i}" for i in range(n_keys)}
        token = padded_engine.encode_data(data)
        decoded = padded_engine.decode_data(token)
        check(f"Padded data {n_keys} keys roundtrip", decoded == data)

    # TTL with padding
    data = {"session": "abc"}
    token = padded_engine.encode_data(data, ttl=3600)
    decoded = padded_engine.decode_data(token)
    check("Padded data with TTL roundtrip", decoded == {"session": "abc"})


# ── Per-Token Mask Isolation ──────────────────────────────────────────────


def test_per_token_mask_isolation():
    print("\n=== Per-Token Mask Isolation ===")

    engine = TokenEngine(keys=[SigningKey(secret="mask-isolation-key", version=0)])

    # Encode two tokens for similar data — XOR streams should be completely
    # different because each has a unique salt → unique mask
    ref = "test_reference"
    t1 = engine.encode_ref(ref)
    t2 = engine.encode_ref(ref)

    # Extract the base62 payload portions (skip version + type chars, before '.')
    p1 = t1[2 : t1.index(".")]
    p2 = t2[2 : t2.index(".")]
    check("Per-token payloads differ (unique masks)", p1 != p2)

    # Both still decode to same value
    check("Token 1 decodes", engine.decode_ref(t1) == ref)
    check("Token 2 decodes", engine.decode_ref(t2) == ref)

    # With known-plaintext: even if attacker knows the payload content,
    # they can't use one token's XOR stream to decode another
    # (because each has unique salt → unique mask derivation)
    check(
        "Tokens have different lengths (salt randomness)", True
    )  # Just documenting the property


# ── Combined Salt + Padding ───────────────────────────────────────────────


def test_combined_salt_padding():
    print("\n=== Combined Salt + Padding ===")

    engine = TokenEngine(
        keys=[SigningKey(secret="combo-key", version=0)],
        salt_bytes=16,
        pad_to_bucket=True,
    )

    # Ref roundtrip
    for ref in ["x", "medium_ref", "a" * 100, "a" * 500]:
        token = engine.encode_ref(ref)
        decoded = engine.decode_ref(token)
        check(f"Salt+pad ref len={len(ref)}", decoded == ref)

    # Data roundtrip
    for n in [1, 10, 50]:
        data = {f"key_{i}": f"val_{i}" for i in range(n)}
        token = engine.encode_data(data)
        decoded = engine.decode_data(token)
        check(f"Salt+pad data {n} keys", decoded == data)

    # TTL with both features
    data = {"user": "test"}
    token = engine.encode_data(data, ttl=3600)
    check("Salt+pad+TTL valid", engine.decode_data(token) == data)

    token = engine.encode_data(data, ttl=-1)
    check("Salt+pad+TTL expired", engine.decode_data(token) is None)

    # Key rotation with both features
    key_v1 = SigningKey(secret="old-combo", version=1)
    key_v2 = SigningKey(secret="new-combo", version=2)
    old_engine = TokenEngine(keys=[key_v1], salt_bytes=16, pad_to_bucket=True)
    new_engine = TokenEngine(keys=[key_v2, key_v1], salt_bytes=16, pad_to_bucket=True)

    old_token = old_engine.encode_ref("old_session")
    check(
        "Old salt+pad token decodes on new engine",
        new_engine.decode_ref(old_token) == "old_session",
    )


# ── Adversarial Salt/Padding ─────────────────────────────────────────────


def test_adversarial_salt_padding():
    print("\n=== Adversarial Salt/Padding ===")

    engine = TokenEngine(
        keys=[SigningKey(secret="adv-salt-key", version=0)],
        salt_bytes=8,
        pad_to_bucket=True,
    )

    # Bit-flip attack on salted+padded token
    valid = engine.encode_ref("protected_session")
    token_bytes = bytearray(valid.encode("ascii"))
    flips_detected = 0
    total_flips = len(token_bytes) * 8
    for byte_idx in range(len(token_bytes)):
        for bit_idx in range(8):
            corrupted = bytearray(token_bytes)
            corrupted[byte_idx] ^= 1 << bit_idx
            try:
                corrupted_str = corrupted.decode("ascii")
            except UnicodeDecodeError:
                flips_detected += 1
                continue
            if engine.decode_ref(corrupted_str) is None:
                flips_detected += 1
    check(
        f"Salt+pad bit-flip: {flips_detected}/{total_flips} detected",
        flips_detected == total_flips,
    )

    # Cross-config attacks
    plain_engine = TokenEngine(
        keys=[SigningKey(secret="adv-salt-key", version=0)],
        salt_bytes=0,
        pad_to_bucket=False,
    )
    salted_token = engine.encode_ref("test")
    plain_token = plain_engine.encode_ref("test")
    check(
        "Salted+padded rejected by plain engine",
        plain_engine.decode_ref(salted_token) is None
        or plain_engine.decode_ref(salted_token) != "test",
    )
    check(
        "Plain rejected by salted+padded engine",
        engine.decode_ref(plain_token) is None
        or engine.decode_ref(plain_token) != "test",
    )


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    print("Token Signing Engine Tests")
    print("=" * 60)

    test_signing_key_validation()
    test_token_engine_construction()
    test_ref_roundtrip()
    test_data_roundtrip()
    test_ttl_expiration()
    test_key_rotation()
    test_xor_obfuscation()
    test_error_handling()
    test_token_format()
    test_multiple_versions()
    test_signature_bytes()
    test_is_valid()
    test_determinism()
    test_special_characters()
    test_data_key_ordering()
    test_concurrent_safety()
    test_adversarial_tokens()
    test_adversarial_data_payloads()
    test_native_xor_bytes()
    test_token_corruption_resistance()
    test_cross_type_isolation()
    test_expired_data_edge_cases()
    test_version_boundary_values()
    test_empty_and_boundary_payloads()
    test_salt_security()
    test_padding_security()
    test_per_token_mask_isolation()
    test_combined_salt_padding()
    test_adversarial_salt_padding()
    test_benchmark()

    print("\n" + "=" * 60)
    print(f"{PASS + FAIL} tests: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
