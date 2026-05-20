r"""
Regression tests for native SIMD JSON parser.

Tests all edge cases, Unicode handling, surrogate pairs, and RFC 8259
compliance. These tests exist because bugs were found in production:

- Surrogate pairs (\uD83C\uDF89) were not decoded to emoji
- Lone surrogates caused crashes
- Non-BMP Unicode via \uXXXX escapes was silently corrupted

Every test verifies native fast_json_loads matches stdlib json.loads exactly.
"""

# hyper-test: unit

import json

from hyperdjango.native import fast_json_dumps, fast_json_loads

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)
    return condition


def check_roundtrip(name, value):
    """Verify fast_json_dumps → fast_json_loads round-trips correctly."""
    dumped = fast_json_dumps(value)
    loaded = fast_json_loads(dumped)
    return ok(
        f"roundtrip: {name}",
        loaded == value,
        f"expected {repr(value)}, got {repr(loaded)}",
    )


def check_loads_match(name, json_str):
    """Verify fast_json_loads matches json.loads for the same input."""
    stdlib = json.loads(json_str)
    native = fast_json_loads(json_str)
    return ok(
        f"loads match: {name}",
        stdlib == native,
        f"stdlib={repr(stdlib)}, native={repr(native)}",
    )


def main():
    global PASS, FAIL

    print("=" * 60)
    print("JSON Parser Regression Tests")
    print("=" * 60)

    # ── Basic types ──────────────────────────────────────────────
    print("\n--- Basic types ---")
    check_roundtrip("null", None)
    check_roundtrip("true", True)
    check_roundtrip("false", False)
    check_roundtrip("int 0", 0)
    check_roundtrip("int 42", 42)
    check_roundtrip("int -1", -1)
    check_roundtrip("float 3.14", 3.14)
    check_roundtrip("float -0.5", -0.5)
    check_roundtrip("empty string", "")
    check_roundtrip("simple string", "hello")
    check_roundtrip("empty list", [])
    check_roundtrip("empty dict", {})

    # ── String escapes ───────────────────────────────────────────
    print("\n--- String escape sequences ---")
    check_loads_match("backslash-n", r'{"x": "hello\nworld"}')
    check_loads_match("backslash-t", r'{"x": "tab\there"}')
    check_loads_match("backslash-r", r'{"x": "cr\rhere"}')
    check_loads_match("backslash-quote", r'{"x": "say \"hi\""}')
    check_loads_match("backslash-backslash", r'{"x": "path\\to\\file"}')
    check_loads_match("backslash-slash", r'{"x": "a\/b"}')
    check_loads_match("backslash-b", r'{"x": "back\bspace"}')
    check_loads_match("backslash-f", r'{"x": "form\ffeed"}')

    # ── Unicode \uXXXX (BMP) ────────────────────────────────────
    print("\n--- Unicode \\uXXXX (BMP) ---")
    check_loads_match("ASCII A", r'{"x": "\u0041"}')
    check_loads_match("e-acute", r'{"x": "\u00e9"}')
    check_loads_match("copyright", r'{"x": "\u00a9"}')
    check_loads_match("euro sign", r'{"x": "\u20ac"}')
    check_loads_match("CJK char", r'{"x": "\u4f60"}')  # 你
    check_loads_match("Arabic char", r'{"x": "\u0645"}')  # م
    check_loads_match("null char", r'{"x": "\u0000"}')
    check_loads_match("BMP max", r'{"x": "\uffff"}')
    check_loads_match("mixed BMP", r'{"x": "a\u00e9b\u00f1c"}')

    # ── Surrogate pairs (non-BMP) — THE CRITICAL REGRESSION ─────
    print("\n--- Surrogate pairs (non-BMP emoji) ---")
    check_loads_match("party popper 🎉", r'{"x": "\ud83c\udf89"}')
    check_loads_match("fire 🔥", r'{"x": "\ud83d\udd25"}')
    check_loads_match("hundred 💯", r'{"x": "\ud83d\udcaf"}')
    check_loads_match("smile 😀", r'{"x": "\ud83d\ude00"}')
    check_loads_match("heart ❤", r'{"x": "\u2764"}')  # BMP, not surrogate
    check_loads_match(
        "flag 🇺🇸", r'{"x": "\ud83c\uddfa\ud83c\uddf8"}'
    )  # Regional indicators
    check_loads_match("three emoji", r'{"x": "\ud83c\udf89\ud83d\udd25\ud83d\udcaf"}')
    check_loads_match("mixed text+emoji", r'{"x": "Hello \ud83c\udf89 world"}')
    check_loads_match("emoji in array", r'["\ud83c\udf89", "\ud83d\udd25"]')
    check_loads_match("emoji as key", r'{"\ud83c\udf89": "party"}')

    # Surrogate pair edge cases
    check_loads_match("first valid pair", r'{"x": "\ud800\udc00"}')  # U+10000
    check_loads_match("last valid pair", r'{"x": "\udbff\udfff"}')  # U+10FFFF
    check_loads_match("mid-range pair", r'{"x": "\ud834\udd1e"}')  # 𝄞 musical symbol

    # ── Surrogate pair case sensitivity ──────────────────────────
    print("\n--- Surrogate pair case variants ---")
    check_loads_match("lowercase hex", r'{"x": "\ud83c\udf89"}')
    check_loads_match("uppercase hex", r'{"x": "\uD83C\uDF89"}')
    check_loads_match("mixed case hex", r'{"x": "\uD83c\uDf89"}')

    # ── Lone surrogates (invalid but should not crash) ───────────
    print("\n--- Lone surrogates (error handling) ---")
    # These are technically invalid JSON but parsers should handle gracefully
    # stdlib json.loads accepts them and produces lone surrogates in the string
    for label, s in [
        ("lone high", r'{"x": "\ud83c"}'),
        ("lone low", r'{"x": "\udf89"}'),
        ("high at end", r'{"x": "abc\ud83c"}'),
        ("high + non-surrogate", r'{"x": "\ud83c\u0041"}'),
    ]:
        try:
            native = fast_json_loads(s)
            ok(f"lone surrogate no crash: {label}", True)
        except Exception as e:
            ok(f"lone surrogate no crash: {label}", False, str(e))

    # ── Direct UTF-8 (not escaped) ──────────────────────────────
    print("\n--- Direct UTF-8 (no escapes) ---")
    check_roundtrip("Chinese", "你好世界")
    check_roundtrip("Arabic", "مرحبا")
    check_roundtrip("Japanese", "日本語")
    check_roundtrip("Korean", "한국어")
    check_roundtrip("Emoji direct", "🎉🔥💯😀")
    check_roundtrip("Mixed scripts", "Hello 你好 مرحبا 🎉")
    check_roundtrip("Accented", "café résumé naïve")
    check_roundtrip("Cyrillic", "Привет мир")

    # ── Large strings ────────────────────────────────────────────
    print("\n--- Large strings ---")
    check_roundtrip("1KB ASCII", "A" * 1024)
    check_roundtrip("1KB emoji", "🎉" * 256)  # 256 × 4 bytes = 1KB
    check_roundtrip("10KB mixed", ("Hello 你好 🎉 " * 500))

    # ── Nested structures ────────────────────────────────────────
    print("\n--- Nested structures ---")
    check_roundtrip("nested dict", {"a": {"b": {"c": "d"}}})
    check_roundtrip("nested list", [[1, 2], [3, [4, 5]]])
    check_roundtrip("mixed nesting", {"users": [{"name": "你好", "emoji": "🎉"}]})

    # ── Numbers ──────────────────────────────────────────────────
    print("\n--- Number edge cases ---")
    check_loads_match("zero", "0")
    check_loads_match("negative zero", "-0")
    check_loads_match("max safe int", "9007199254740991")
    check_loads_match("min safe int", "-9007199254740991")
    check_loads_match("scientific", "1.5e10")
    check_loads_match("negative scientific", "-2.5e-3")
    check_loads_match("capital E", "1E10")

    # ── Whitespace handling ──────────────────────────────────────
    print("\n--- Whitespace ---")
    check_loads_match("leading space", '  {"x": 1}')
    check_loads_match("trailing space", '{"x": 1}  ')
    check_loads_match("inner spaces", '{  "x"  :  1  }')
    check_loads_match("tabs", '{\t"x"\t:\t1\t}')
    check_loads_match("newlines", '{\n"x"\n:\n1\n}')

    # ── Empty/minimal ────────────────────────────────────────────
    print("\n--- Minimal valid JSON ---")
    check_loads_match("empty object", "{}")
    check_loads_match("empty array", "[]")
    check_loads_match("empty string", '""')
    check_loads_match("null", "null")
    check_loads_match("true", "true")
    check_loads_match("false", "false")

    # ── Ensure ensure_ascii=True roundtrips ──────────────────────
    print("\n--- ensure_ascii=True compatibility ---")
    # This is the exact scenario that caused the original bug:
    # Python json.dumps(ensure_ascii=True) produces \uXXXX for non-ASCII
    for label, value in [
        ("simple emoji", {"msg": "🎉"}),
        ("CJK text", {"msg": "你好"}),
        ("mixed", {"msg": "Hello 🎉 你好"}),
        ("multiple emoji", {"msg": "🎉🔥💯😀"}),
    ]:
        # Simulate: Python json.dumps(ensure_ascii=True) → wire → native loads
        wire = json.dumps(value)  # ensure_ascii=True is default
        native_result = fast_json_loads(wire)
        ok(
            f"ensure_ascii roundtrip: {label}",
            native_result == value,
            f"expected {repr(value)}, got {repr(native_result)}",
        )

    # ── Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
