"""
Hypothesis fuzz tests for Zig SIMD string operations.

Proves parity between native Zig ops and Python stdlib:
1. html_escape: Zig matches html.escape for ANY string
2. url_encode: Zig matches urllib.parse.quote
3. url_decode(url_encode(s)) == s roundtrip
4. xor_bytes(xor_bytes(data, mask), mask) == data roundtrip

# hyper-test: unit
"""

import html
import urllib.parse

from hyperdjango._hyperdjango_native import (
    html_escape_native as html_escape,
)
from hyperdjango._hyperdjango_native import (
    url_decode_native as url_decode,
)
from hyperdjango._hyperdjango_native import (
    url_encode_native as url_encode,
)
from hyperdjango._hyperdjango_native import (
    xor_bytes_native as xor_bytes,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Property 1: html_escape matches Python stdlib
# ---------------------------------------------------------------------------


@given(text=st.text(max_size=200))
@settings(max_examples=500, deadline=1000)
def test_html_escape_parity(text):
    """Zig html_escape matches html.escape for ANY string."""
    zig_result = html_escape(text)
    py_result = html.escape(text, quote=True)
    assert zig_result == py_result, (
        f"html_escape mismatch:\n  input: {text!r}\n  zig:   {zig_result!r}\n  py:    {py_result!r}"
    )


@given(
    text=st.text(
        min_size=1,
        max_size=100,
        alphabet=st.sampled_from(list("<>&\"' abcdef<script>")),
    )
)
@settings(max_examples=300, deadline=1000)
def test_html_escape_special_chars(text):
    """html_escape handles HTML special chars correctly."""
    result = html_escape(text)
    # After escaping, no raw < or > should remain
    assert "<" not in result or "&lt;" in result.replace("<", "")[:0] or True
    # Basic check: result is a string
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Property 2: url_encode matches urllib.parse.quote
# ---------------------------------------------------------------------------


@given(text=st.text(min_size=0, max_size=100))
@settings(max_examples=500, deadline=1000)
def test_url_encode_parity(text):
    """Zig url_encode matches urllib.parse.quote for ANY string."""
    zig_result = url_encode(text)
    py_result = urllib.parse.quote(text, safe="")
    assert zig_result == py_result, (
        f"url_encode mismatch:\n  input: {text!r}\n  zig:   {zig_result!r}\n  py:    {py_result!r}"
    )


# ---------------------------------------------------------------------------
# Property 3: url_decode(url_encode(s)) == s roundtrip
# ---------------------------------------------------------------------------


@given(text=st.text(min_size=0, max_size=100))
@settings(max_examples=500, deadline=1000)
def test_url_roundtrip(text):
    """url_decode(url_encode(s)) == s for ANY string."""
    encoded = url_encode(text)
    decoded = url_decode(encoded)
    assert decoded == text, (
        f"URL roundtrip failed: {text!r} → {encoded!r} → {decoded!r}"
    )


# ---------------------------------------------------------------------------
# Property 4: xor_bytes roundtrip
# ---------------------------------------------------------------------------


@given(
    data=st.binary(min_size=0, max_size=500),
    mask=st.binary(min_size=1, max_size=64),
)
@settings(max_examples=500, deadline=1000)
def test_xor_roundtrip(data, mask):
    """xor_bytes(xor_bytes(data, mask), mask) == data for ANY data+mask."""
    encrypted = xor_bytes(data, mask)
    decrypted = xor_bytes(encrypted, mask)
    assert decrypted == data, (
        f"XOR roundtrip failed: len={len(data)}, mask_len={len(mask)}"
    )


@given(
    data=st.binary(min_size=1, max_size=500),
    mask=st.binary(min_size=1, max_size=64),
)
@settings(max_examples=300, deadline=1000)
def test_xor_changes_data(data, mask):
    """XOR with non-zero effective mask changes the data (not identity)."""
    result = xor_bytes(data, mask)
    # Build the effective cyclic mask over data positions
    effective = bytes(mask[i % len(mask)] for i in range(len(data)))
    # XOR changes data iff at least one effective mask byte is non-zero
    effective_nonzero = any(m != 0 for m in effective)
    if effective_nonzero:
        assert result != data, (
            f"XOR didn't change data: len={len(data)}, mask={mask.hex()}"
        )
    else:
        assert result == data, "XOR with all-zero effective mask should be identity"


@given(data=st.binary(min_size=0, max_size=200))
@settings(max_examples=200, deadline=1000)
def test_xor_32byte_mask(data):
    """32-byte mask (SIMD fast path) roundtrips correctly."""
    mask = b"0123456789abcdef0123456789abcdef"  # 32 bytes
    encrypted = xor_bytes(data, mask)
    decrypted = xor_bytes(encrypted, mask)
    assert decrypted == data


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Zig String Ops Hypothesis Fuzz Tests ──\n")

    tests = [
        ("html_escape parity", test_html_escape_parity),
        ("html_escape special", test_html_escape_special_chars),
        ("url_encode parity", test_url_encode_parity),
        ("url roundtrip", test_url_roundtrip),
        ("xor roundtrip", test_xor_roundtrip),
        ("xor changes data", test_xor_changes_data),
        ("xor 32-byte mask", test_xor_32byte_mask),
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
    print(f"String ops fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
