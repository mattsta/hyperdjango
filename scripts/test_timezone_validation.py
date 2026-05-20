"""Tests for TIME_ZONE validation via zoneinfo.

A valid IANA zone passes; an invalid zone raises ValueError naming the bad
zone, instead of silently accepting a misconfigured zone.
"""

# hyper-test: unit

from hyperdjango.conf import (
    SETTING_DEFINITIONS,
    _validate_time_zone,
    validate_settings,
)

PASS = 0
FAIL = 0


def check(label: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {label}")
    else:
        FAIL += 1
        print(f"  FAIL: {label}")


def test_valid_zones_pass() -> None:
    """Real IANA zones resolve without raising."""
    print("\n--- valid zones pass ---")
    for zone in ("America/New_York", "UTC", "Europe/London", "Asia/Tokyo"):
        raised = False
        try:
            _validate_time_zone(zone)
        except ValueError:
            raised = True
        check(f"{zone} does not raise", not raised)


def test_invalid_zone_raises() -> None:
    """An unknown zone raises ValueError naming the bad zone."""
    print("\n--- invalid zone raises ValueError ---")
    raised = False
    message = ""
    try:
        _validate_time_zone("Not/AZone")
    except ValueError as exc:
        raised = True
        message = str(exc)
    check("Not/AZone raises ValueError", raised)
    check("error names the bad zone", "Not/AZone" in message)
    check("error mentions TIME_ZONE", "TIME_ZONE" in message)


def test_garbage_zone_raises() -> None:
    """A clearly invalid string raises ValueError too."""
    print("\n--- garbage zone raises ValueError ---")
    raised = False
    try:
        _validate_time_zone("definitely not a zone")
    except ValueError:
        raised = True
    check("garbage string raises ValueError", raised)


def test_definition_has_validator() -> None:
    """TIME_ZONE SettingDefinition wires the validator hook."""
    print("\n--- TIME_ZONE definition wires validator ---")
    defn = SETTING_DEFINITIONS["TIME_ZONE"]
    check("validator is set", defn.validator is _validate_time_zone)
    check("default UTC is itself valid", defn.default == "UTC")


def test_validate_settings_surfaces_error() -> None:
    """validate_settings reports an error string for an invalid TIME_ZONE."""
    print("\n--- validate_settings surfaces invalid TIME_ZONE ---")
    errors = validate_settings({"TIME_ZONE": "Not/AZone"})
    tz_errors = [e for e in errors if "TIME_ZONE" in e]
    check("at least one TIME_ZONE error", len(tz_errors) >= 1)
    check("error names the bad zone", any("Not/AZone" in e for e in tz_errors))


def test_validate_settings_accepts_valid() -> None:
    """validate_settings produces no TIME_ZONE error for a valid zone."""
    print("\n--- validate_settings accepts valid TIME_ZONE ---")
    for zone in ("America/New_York", "UTC"):
        errors = validate_settings({"SECRET_KEY": "x", "TIME_ZONE": zone})
        tz_errors = [e for e in errors if "TIME_ZONE" in e]
        check(f"no TIME_ZONE error for {zone}", len(tz_errors) == 0)


def main() -> None:
    test_valid_zones_pass()
    test_invalid_zone_raises()
    test_garbage_zone_raises()
    test_definition_has_validator()
    test_validate_settings_surfaces_error()
    test_validate_settings_accepts_valid()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 60}")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
