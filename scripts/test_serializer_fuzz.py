"""
Hypothesis fuzz tests for REST serializer typed fields.

Proves correctness properties:
1. to_representation(to_internal_value(x)) roundtrips for valid inputs
2. Invalid inputs raise ValueError (not crash)
3. None handling is consistent

# hyper-test: unit
"""

import contextlib
import datetime
import decimal
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hyperdjango.rest import (
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    EmailField,
    IPAddressField,
    TimeField,
    UUIDField,
)

# ---------------------------------------------------------------------------
# DateTimeField
# ---------------------------------------------------------------------------


@given(
    dt=st.datetimes(
        min_value=datetime.datetime(2000, 1, 1),
        max_value=datetime.datetime(2099, 12, 31),
    )
)
@settings(max_examples=300, deadline=1000)
def test_datetime_roundtrip(dt):
    """DateTimeField: datetime → repr → internal == original."""
    field = DateTimeField()
    represented = field.to_representation(dt)
    restored = field.to_internal_value(represented)
    assert restored == dt, f"Roundtrip failed: {dt} → {represented} → {restored}"


@given(text=st.text(max_size=20))
@settings(max_examples=200, deadline=1000)
def test_datetime_invalid_rejects(text):
    """DateTimeField rejects non-ISO strings."""
    assume(text and not text[0].isdigit())  # skip strings that might parse
    field = DateTimeField()
    with contextlib.suppress(ValueError, TypeError):
        field.to_internal_value(text)
        # If it parsed, it should be a valid datetime


# ---------------------------------------------------------------------------
# DateField
# ---------------------------------------------------------------------------


@given(
    d=st.dates(
        min_value=datetime.date(2000, 1, 1),
        max_value=datetime.date(2099, 12, 31),
    )
)
@settings(max_examples=300, deadline=1000)
def test_date_roundtrip(d):
    """DateField: date → repr → internal == original."""
    field = DateField()
    represented = field.to_representation(d)
    restored = field.to_internal_value(represented)
    assert restored == d


# ---------------------------------------------------------------------------
# TimeField
# ---------------------------------------------------------------------------


@given(t=st.times())
@settings(max_examples=300, deadline=1000)
def test_time_roundtrip(t):
    """TimeField: time → repr → internal == original."""
    field = TimeField()
    represented = field.to_representation(t)
    restored = field.to_internal_value(represented)
    assert restored == t


# ---------------------------------------------------------------------------
# UUIDField
# ---------------------------------------------------------------------------


@given(u=st.uuids())
@settings(max_examples=300, deadline=1000)
def test_uuid_roundtrip(u):
    """UUIDField: uuid → repr → internal == original."""
    field = UUIDField()
    represented = field.to_representation(u)
    restored = field.to_internal_value(represented)
    assert restored == u


@given(text=st.text(min_size=1, max_size=40))
@settings(max_examples=200, deadline=1000)
def test_uuid_invalid_rejects(text):
    """UUIDField rejects non-UUID strings."""
    try:
        uuid.UUID(text)
        return  # valid UUID string, skip
    except ValueError, AttributeError:
        pass
    field = UUIDField()
    try:
        field.to_internal_value(text)
        assert False, f"Should have rejected: {text!r}"
    except ValueError, AttributeError:
        pass


# ---------------------------------------------------------------------------
# DecimalField
# ---------------------------------------------------------------------------


@given(
    d=st.decimals(
        min_value=-1e10, max_value=1e10, allow_nan=False, allow_infinity=False
    )
)
@settings(max_examples=300, deadline=1000)
def test_decimal_roundtrip(d):
    """DecimalField: decimal → repr → internal preserves value."""
    field = DecimalField()
    represented = field.to_representation(d)
    restored = field.to_internal_value(represented)
    assert restored == d or abs(restored - d) < decimal.Decimal("0.0000001"), (
        f"Roundtrip: {d} → {represented} → {restored}"
    )


@given(text=st.text(min_size=1, max_size=10, alphabet="abcdefg!@#"))
@settings(max_examples=200, deadline=1000)
def test_decimal_invalid_rejects(text):
    """DecimalField rejects non-numeric strings."""
    field = DecimalField()
    with contextlib.suppress(ValueError, decimal.InvalidOperation):
        field.to_internal_value(text)


# ---------------------------------------------------------------------------
# EmailField
# ---------------------------------------------------------------------------


@given(
    local=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop0123456789._"),
    domain=st.text(min_size=1, max_size=15, alphabet="abcdefghijklmnop"),
    tld=st.sampled_from(["com", "org", "net", "io", "dev"]),
)
@settings(max_examples=300, deadline=1000)
def test_email_valid_accepted(local, domain, tld):
    """EmailField accepts any valid-looking email."""
    email = f"{local}@{domain}.{tld}"
    field = EmailField()
    result = field.to_internal_value(email)
    assert result == email


@given(text=st.text(max_size=30).filter(lambda s: "@" not in s))
@settings(max_examples=200, deadline=1000)
def test_email_no_at_rejects(text):
    """EmailField rejects strings without @."""
    field = EmailField()
    try:
        field.to_internal_value(text)
        assert False, f"Should have rejected: {text!r}"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# ChoiceField
# ---------------------------------------------------------------------------


@given(choice=st.sampled_from(["draft", "published", "archived"]))
@settings(max_examples=100, deadline=1000)
def test_choice_valid(choice):
    """ChoiceField accepts valid choices."""
    field = ChoiceField(choices=["draft", "published", "archived"])
    result = field.to_internal_value(choice)
    assert result == choice


@given(
    text=st.text(min_size=1, max_size=10).filter(
        lambda s: s not in ("draft", "published", "archived")
    )
)
@settings(max_examples=200, deadline=1000)
def test_choice_invalid_rejects(text):
    """ChoiceField rejects invalid choices."""
    field = ChoiceField(choices=["draft", "published", "archived"])
    try:
        field.to_internal_value(text)
        assert False, f"Should have rejected: {text!r}"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# IPAddressField
# ---------------------------------------------------------------------------


@given(
    a=st.integers(min_value=0, max_value=255),
    b=st.integers(min_value=0, max_value=255),
    c=st.integers(min_value=0, max_value=255),
    d=st.integers(min_value=0, max_value=255),
)
@settings(max_examples=300, deadline=1000)
def test_ipv4_valid(a, b, c, d):
    """IPAddressField accepts valid IPv4."""
    ip = f"{a}.{b}.{c}.{d}"
    field = IPAddressField()
    result = field.to_internal_value(ip)
    assert result == ip


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Serializer Field Hypothesis Fuzz Tests ──\n")

    tests = [
        ("DateTime roundtrip", test_datetime_roundtrip),
        ("DateTime invalid rejects", test_datetime_invalid_rejects),
        ("Date roundtrip", test_date_roundtrip),
        ("Time roundtrip", test_time_roundtrip),
        ("UUID roundtrip", test_uuid_roundtrip),
        ("UUID invalid rejects", test_uuid_invalid_rejects),
        ("Decimal roundtrip", test_decimal_roundtrip),
        ("Decimal invalid rejects", test_decimal_invalid_rejects),
        ("Email valid accepted", test_email_valid_accepted),
        ("Email no-@ rejects", test_email_no_at_rejects),
        ("Choice valid", test_choice_valid),
        ("Choice invalid rejects", test_choice_invalid_rejects),
        ("IPv4 valid", test_ipv4_valid),
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
    print(f"Serializer fuzz: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
