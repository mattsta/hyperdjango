"""
Audit: test ALL Zig native functions with "s#" format for non-UTF-8 resilience.

Each function that accepts user-facing strings must handle latin-1 encoded
bytes from ASGI without crashing. This is the same bug class as the
parse_query_string fix — systematically verify no other functions have it.

# hyper-test: unit
"""

from hyperdjango._hyperdjango_native import (
    html_escape_native,
    json_loads_native,
    parse_cookies_native,
    url_decode_native,
    url_encode_native,
)

# Latin-1 strings that contain bytes 0x80-0xFF (valid latin-1, NOT valid UTF-8)
LATIN1_INPUTS = [
    "\xa0",  # non-breaking space
    "\xff",  # ÿ
    "\x80",  # C1 control
    "\xc0",  # À in latin-1 but invalid UTF-8 start
    "café\xa0test",  # mixed valid + invalid
    "\xa0\xa1\xa2",  # multiple high bytes
    "key=\xa0value",  # query-string-like
    "a=\xff&b=\xfe",  # multi-param with high bytes
]


def test_function(name, func, inputs, expect_crash=False):
    """Test a function with latin-1 inputs. Reports pass/fail."""
    crashes = []
    for inp in inputs:
        try:
            result = func(inp)
        except (UnicodeDecodeError, UnicodeEncodeError) as e:
            crashes.append((inp, str(e)))
        except Exception:
            # Other errors are OK (e.g., invalid JSON)
            pass

    if crashes:
        print(f"  FAIL: {name} — {len(crashes)} crashes on non-UTF-8 input")
        for inp, err in crashes[:3]:
            print(f"    {inp!r}: {err}")
        return False
    else:
        print(f"  PASS: {name}")
        return True


def run_tests():
    print("\n── Zig 's#' Format Audit: Non-UTF-8 Resilience ──\n")

    passed = 0
    failed = 0

    tests = [
        ("html_escape_native", html_escape_native, LATIN1_INPUTS),
        ("url_encode_native", url_encode_native, LATIN1_INPUTS),
        ("url_decode_native", url_decode_native, ["%A0", "%FF", "%80", "test%C0value"]),
        ("parse_cookies_native", parse_cookies_native, LATIN1_INPUTS),
        (
            "json_loads_native (invalid JSON OK)",
            json_loads_native,
            ['"\xa0"', '"\xff"'],
        ),
    ]

    for name, func, inputs in tests:
        if test_function(name, func, inputs):
            passed += 1
        else:
            failed += 1

    # Regression tests for specific failures found
    print("\n── Regression Tests ──\n")

    # url_decode with %A0 (non-UTF-8 decoded byte)
    try:
        result = url_decode_native("%A0")
        assert isinstance(result, str), f"Expected str, got {type(result)}"
        assert result == "\xa0", f"Expected \\xa0, got {result!r}"
        print("  PASS: regression url_decode %A0")
        passed += 1
    except Exception as e:
        print(f"  FAIL: regression url_decode %A0: {e}")
        failed += 1

    try:
        result = url_decode_native("%FF%80%A0")
        assert isinstance(result, str)
        print("  PASS: regression url_decode %FF%80%A0")
        passed += 1
    except Exception as e:
        print(f"  FAIL: regression url_decode %FF%80%A0: {e}")
        failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"s# audit: {passed}/{total} passed")
    if failed:
        print(f"FAILURES: {failed} — these functions need the latin-1 fallback fix")
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED — no additional s# vulnerabilities found")


if __name__ == "__main__":
    run_tests()
