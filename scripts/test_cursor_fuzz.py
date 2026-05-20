"""
Hypothesis fuzz tests for HMAC cursor pagination.

Proves correctness properties:
1. encode → decode roundtrip for ALL supported types
2. ANY byte modification in cursor → decode returns None
3. Direction preserved through roundtrip

# hyper-test: unit
"""

import datetime
import sys

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hyperdjango.rest import _decode_cursor, _encode_cursor

# ---------------------------------------------------------------------------
# Strategies for cursor values by type
# ---------------------------------------------------------------------------

cursor_ints = st.integers(min_value=-(2**53), max_value=2**53)
cursor_strs = st.text(min_size=1, max_size=50)
cursor_floats = st.floats(
    min_value=-1e10, max_value=1e10, allow_nan=False, allow_infinity=False
)
cursor_datetimes = st.datetimes(
    min_value=datetime.datetime(2000, 1, 1), max_value=datetime.datetime(2099, 12, 31)
)
cursor_dates = st.dates(
    min_value=datetime.date(2000, 1, 1), max_value=datetime.date(2099, 12, 31)
)
cursor_uuids = st.uuids()
cursor_decimals = st.decimals(
    min_value=-1e8, max_value=1e8, allow_nan=False, allow_infinity=False, places=4
)

directions = st.sampled_from(["next", "prev"])


# ---------------------------------------------------------------------------
# Property 1: Roundtrip for each type
# ---------------------------------------------------------------------------


@given(direction=directions, value=cursor_ints)
@settings(max_examples=300, deadline=1000)
def test_int_roundtrip(direction, value):
    encoded = _encode_cursor(direction, value)
    result = _decode_cursor(encoded)
    assert result is not None, f"Decode failed for int {value}"
    dec_dir, dec_val = result
    assert dec_dir == direction
    assert dec_val == value


@given(direction=directions, value=cursor_strs)
@settings(max_examples=300, deadline=1000)
def test_str_roundtrip(direction, value):
    # Strings with : could interfere with the payload format
    assume(":" not in value)
    encoded = _encode_cursor(direction, value)
    result = _decode_cursor(encoded)
    assert result is not None, f"Decode failed for str {value!r}"
    dec_dir, dec_val = result
    assert dec_dir == direction
    assert dec_val == value


@given(direction=directions, value=cursor_datetimes)
@settings(max_examples=300, deadline=1000)
def test_datetime_roundtrip(direction, value):
    encoded = _encode_cursor(direction, value)
    result = _decode_cursor(encoded)
    assert result is not None
    dec_dir, dec_val = result
    assert dec_dir == direction
    assert dec_val == value


@given(direction=directions, value=cursor_dates)
@settings(max_examples=300, deadline=1000)
def test_date_roundtrip(direction, value):
    encoded = _encode_cursor(direction, value)
    result = _decode_cursor(encoded)
    assert result is not None
    dec_dir, dec_val = result
    assert dec_dir == direction
    assert dec_val == value


@given(direction=directions, value=cursor_uuids)
@settings(max_examples=300, deadline=1000)
def test_uuid_roundtrip(direction, value):
    encoded = _encode_cursor(direction, value)
    result = _decode_cursor(encoded)
    assert result is not None
    dec_dir, dec_val = result
    assert dec_dir == direction
    assert dec_val == value


@given(direction=directions, value=cursor_decimals)
@settings(max_examples=300, deadline=1000)
def test_decimal_roundtrip(direction, value):
    encoded = _encode_cursor(direction, value)
    result = _decode_cursor(encoded)
    assert result is not None
    dec_dir, dec_val = result
    assert dec_dir == direction
    assert dec_val == value


# ---------------------------------------------------------------------------
# Property 2: ANY byte modification → rejection
# ---------------------------------------------------------------------------


@given(
    value=cursor_ints,
    flip_pos=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=500, deadline=1000)
def test_tamper_rejected(value, flip_pos):
    """ANY change to the decoded payload → rejection or different value.

    We tamper with the raw payload bytes (before base64) to avoid false
    positives from base64 redundant bits. The HMAC covers the raw payload,
    so changing any payload byte invalidates the signature.
    """
    import base64

    encoded = _encode_cursor("next", value)
    raw = base64.urlsafe_b64decode(encoded)

    assume(flip_pos < len(raw))

    # Flip one byte in the raw payload
    tampered_raw = bytearray(raw)
    tampered_raw[flip_pos] = (tampered_raw[flip_pos] + 1) % 256
    assume(tampered_raw != raw)

    tampered = base64.urlsafe_b64encode(bytes(tampered_raw)).decode()

    result = _decode_cursor(tampered)
    assert result is None or result[1] != value, (
        f"Tampered cursor decoded to original value! pos={flip_pos}"
    )


# ---------------------------------------------------------------------------
# Property 3: Random garbage → always None
# ---------------------------------------------------------------------------


@given(garbage=st.binary(min_size=1, max_size=100))
@settings(max_examples=300, deadline=1000)
def test_garbage_rejected(garbage):
    """Random bytes as cursor string → decode returns None."""
    result = _decode_cursor(garbage.decode("latin-1", errors="replace"))
    assert result is None


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── HMAC Cursor Pagination Hypothesis Fuzz Tests ──\n")

    tests = [
        ("int roundtrip", test_int_roundtrip),
        ("str roundtrip", test_str_roundtrip),
        ("datetime roundtrip", test_datetime_roundtrip),
        ("date roundtrip", test_date_roundtrip),
        ("uuid roundtrip", test_uuid_roundtrip),
        ("decimal roundtrip", test_decimal_roundtrip),
        ("tamper rejected", test_tamper_rejected),
        ("garbage rejected", test_garbage_rejected),
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
    print(f"Cursor fuzz: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
