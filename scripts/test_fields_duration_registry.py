"""DurationField negative/sub-second fidelity + custom-field type registry.

Covers three fixes in ``hyperdjango/fields.py``:

  * N5 — ``DurationField.to_db_value`` must apply the sign to the HH:MM:SS part
    too, not only the ``N days`` part (PostgreSQL signs each interval field
    independently). A negative timedelta with both a day and a time component
    must serialize to a value that means the correct magnitude.
  * N6 — sub-second precision (microseconds) must survive serialization.
  * #6 — ``register_field(SomeType, SomeField())`` must actually take effect for
    a plainly-annotated field of that Python type, via the ``convert_to_db`` /
    ``convert_from_db`` / ``get_column_type`` integration helpers.

Run:  uv run hyper-test fields_duration_registry
  or:  uv run python scripts/test_fields_duration_registry.py
"""

# hyper-test: unit

from datetime import timedelta
from decimal import Decimal

from hyperdjango.fields import (
    DurationField,
    MoneyField,
    convert_from_db,
    convert_to_db,
    get_column_type,
    get_custom_field,
    register_field,
    unregister_field,
)
from hyperdjango.validation.core.fields import FieldInfo

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ── N5 / N6: DurationField serialization + round-trip ───────────────────────


def _roundtrip(td: timedelta) -> timedelta:
    fld = DurationField()
    serialized = fld.to_db_value(td)
    return fld.from_db_value(serialized)


def test_negative_full_components() -> None:
    # days + hours + minutes + seconds + microseconds, all negative.
    td = -timedelta(days=1, hours=2, minutes=3, seconds=4, microseconds=500000)
    got = _roundtrip(td)
    check(
        "negative full-component timedelta round-trips",
        got == td,
        f"expected {td!r}, got {got!r} (serialized {DurationField().to_db_value(td)!r})",
    )


def test_negative_days_plus_time_magnitude() -> None:
    # The regression that motivated N5: -1 day -2 hours == -26 hours total.
    # A sign only on the day part would (wrongly) mean -1 day +2 hours.
    td = timedelta(days=-1, hours=-2)
    fld = DurationField()
    serialized = fld.to_db_value(td)
    got = fld.from_db_value(serialized)
    check(
        "negative days+time keeps full magnitude",
        got == td and got.total_seconds() == -26 * 3600,
        f"serialized {serialized!r} -> {got!r} ({got.total_seconds()}s)",
    )


def test_negative_sub_day() -> None:
    # Pure negative sub-day value (no day component).
    td = timedelta(hours=-2, minutes=-30, seconds=-15)
    got = _roundtrip(td)
    check(
        "pure negative sub-day round-trips",
        got == td,
        f"expected {td!r}, got {got!r} (serialized {DurationField().to_db_value(td)!r})",
    )


def test_microseconds_preserved() -> None:
    # N6: sub-second precision must not be truncated.
    td = timedelta(seconds=5, microseconds=123456)
    fld = DurationField()
    serialized = fld.to_db_value(td)
    got = fld.from_db_value(serialized)
    check(
        "positive microseconds preserved",
        got == td and ".123456" in str(serialized),
        f"serialized {serialized!r} -> {got!r}",
    )


def test_negative_microseconds_only() -> None:
    td = timedelta(microseconds=-1)
    got = _roundtrip(td)
    check(
        "negative single microsecond round-trips",
        got == td,
        f"expected {td!r}, got {got!r} (serialized {DurationField().to_db_value(td)!r})",
    )


def test_positive_unchanged() -> None:
    # Guard: ordinary positive value with a day component still works.
    td = timedelta(days=3, hours=4, minutes=5, seconds=6)
    got = _roundtrip(td)
    check(
        "positive days+time round-trips",
        got == td,
        f"expected {td!r}, got {got!r}",
    )


def test_zero() -> None:
    td = timedelta(0)
    got = _roundtrip(td)
    check("zero duration round-trips", got == td, f"got {got!r}")


# ── #6: type registry wiring ────────────────────────────────────────────────


def test_registry_resolves_for_type() -> None:
    # Register a MoneyField for Decimal, then a plainly-annotated Decimal field
    # (no explicit create_field) must pick it up through the integration helpers.
    register_field(Decimal, MoneyField(currency="USD"))
    try:
        fi = FieldInfo(annotation=Decimal)  # no custom_field attached
        col = get_column_type(fi)
        db_val = convert_to_db(fi, Decimal("12.34"))
        py_val = convert_from_db(fi, db_val)
        check(
            "registered field resolves column type",
            col == MoneyField(currency="USD").db_type(),
            f"got {col!r}",
        )
        check(
            "registered field converts to DB (cents)",
            db_val == 1234,
            f"got {db_val!r}",
        )
        check(
            "registered field round-trips from DB",
            py_val == Decimal("12.34"),
            f"got {py_val!r}",
        )
        check(
            "get_custom_field returns the registration",
            get_custom_field(Decimal) is not None,
        )
    finally:
        unregister_field(Decimal)

    # After unregister, the plain Decimal field is inert again.
    fi2 = FieldInfo(annotation=Decimal)
    check(
        "unregistered type falls back to no-op",
        get_column_type(fi2) is None and convert_to_db(fi2, Decimal(1)) == Decimal(1),
    )


def test_explicit_custom_field_wins_over_registry() -> None:
    register_field(Decimal, MoneyField(currency="USD"))
    try:
        explicit = DurationField()
        fi = FieldInfo(annotation=Decimal, custom_field=explicit)
        check(
            "explicit custom_field takes precedence over registry",
            get_column_type(fi) == "interval",
            f"got {get_column_type(fi)!r}",
        )
    finally:
        unregister_field(Decimal)


def run() -> bool:
    test_negative_full_components()
    test_negative_days_plus_time_magnitude()
    test_negative_sub_day()
    test_microseconds_preserved()
    test_negative_microseconds_only()
    test_positive_unchanged()
    test_zero()
    test_registry_resolves_for_type()
    test_explicit_custom_field_wins_over_registry()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
