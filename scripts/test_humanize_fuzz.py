"""
Hypothesis fuzz tests for the humanize module.

Proves correctness properties for ALL possible inputs:
1. ordinal: any non-negative integer produces a string ending in st/nd/rd/th
2. intcomma: any integer produces a string with commas, parseable back to original
3. intword: large integers produce human-readable word form without crashing
4. filesizeformat: any non-negative number produces a valid unit string
5. naturaltime: arbitrary datetime offsets produce valid English strings
6. apnumber: any integer produces correct AP-style output
7. Combined: applying multiple formatters in sequence doesn't crash
8. intcomma with floats: decimal values formatted correctly

# hyper-test: unit
"""

import os
import re
import sys
import traceback
from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.humanize import (
    apnumber,
    filesizeformat,
    intcomma,
    intword,
    naturaltime,
    ordinal,
)

# Under parallel test execution, CPU contention can push individual
# Hypothesis examples past their per-call deadlines.
_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 2000
_SUPPRESS = [HealthCheck.too_slow] if _PARALLEL else []

_VALID_SUFFIXES = frozenset({"st", "nd", "rd", "th"})
_VALID_FILESIZE_UNITS = frozenset({"byte", "bytes", "KB", "MB", "GB", "TB", "PB"})
_COMMA_PATTERN = re.compile(r"^-?\d{1,3}(,\d{3})*$")


def _ex(n: int) -> int:
    """Scale Hypothesis example count for parallel-mode CPU contention."""
    return max(n // 2, 30) if _PARALLEL else n


# ---------------------------------------------------------------------------
# Property 1: ordinal — any non-negative integer ends in st/nd/rd/th
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=0, max_value=10**9))
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_ordinal_suffix(value):
    """Any non-negative integer produces a string ending in st/nd/rd/th."""
    result = ordinal(value)
    assert isinstance(result, str), (
        f"ordinal({value}) returned non-string: {type(result)}"
    )
    assert len(result) >= 3, f"ordinal({value}) too short: {result!r}"
    suffix = result[-2:]
    assert suffix in _VALID_SUFFIXES, (
        f"ordinal({value}) has invalid suffix {suffix!r} in {result!r}"
    )
    # The numeric prefix must match the input
    numeric_part = result[:-2]
    assert numeric_part == str(value), (
        f"ordinal({value}) numeric prefix {numeric_part!r} != {str(value)!r}"
    )


# ---------------------------------------------------------------------------
# Property 2: intcomma — any integer produces correctly comma-separated string
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=-(10**15), max_value=10**15))
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_intcomma_roundtrip(value):
    """Any integer → intcomma → remove commas → original value."""
    result = intcomma(value)
    assert isinstance(result, str), f"intcomma({value}) returned non-string"
    # Remove commas and parse back
    stripped = result.replace(",", "")
    assert stripped == str(value), (
        f"intcomma({value}) → {result!r} → stripped {stripped!r} != {str(value)!r}"
    )


@given(value=st.integers(min_value=0, max_value=10**15))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_intcomma_format(value):
    """Non-negative integer commas appear at correct positions (every 3 digits)."""
    result = intcomma(value)
    # For non-negative values, must match the comma pattern
    assert _COMMA_PATTERN.match(result), (
        f"intcomma({value}) = {result!r} does not match comma pattern"
    )


# ---------------------------------------------------------------------------
# Property 3: intword — large integers produce word form without crashing
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=10**6, max_value=10**18))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_intword_large(value):
    """Large integers produce a string containing a scale word or the raw number."""
    result = intword(value)
    assert isinstance(result, str), f"intword({value}) returned non-string"
    # For values >= 1 million, should contain a word OR fall back to str
    valid_words = {"million", "billion", "trillion", "quadrillion"}
    has_word = any(w in result for w in valid_words)
    is_fallback = result == str(value)
    assert has_word or is_fallback, (
        f"intword({value}) = {result!r} has no scale word and isn't fallback"
    )


@given(value=st.integers(min_value=-(10**6 - 1), max_value=10**6 - 1))
@settings(max_examples=_ex(100), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_intword_small(value):
    """Integers with abs < 1 million return str(value) unchanged."""
    result = intword(value)
    assert result == str(value), (
        f"intword({value}) should return str for small values, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Property 4: filesizeformat — any non-negative number produces a valid unit
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=0, max_value=10**18))
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_filesizeformat_unit(value):
    """Any non-negative integer produces a string with a valid unit."""
    result = filesizeformat(value)
    assert isinstance(result, str), f"filesizeformat({value}) returned non-string"
    # Must end with a recognized unit
    has_unit = any(result.endswith(unit) for unit in _VALID_FILESIZE_UNITS)
    assert has_unit, f"filesizeformat({value}) = {result!r} has no valid unit"


@given(
    value=st.floats(
        min_value=0.0, max_value=1e18, allow_nan=False, allow_infinity=False
    )
)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_filesizeformat_float(value):
    """Float byte values produce valid output without crashing."""
    result = filesizeformat(value)
    assert isinstance(result, str), f"filesizeformat({value}) returned non-string"
    has_unit = any(result.endswith(unit) for unit in _VALID_FILESIZE_UNITS)
    assert has_unit, f"filesizeformat({value}) = {result!r} has no valid unit"


# ---------------------------------------------------------------------------
# Property 5: naturaltime — arbitrary datetime offsets produce valid English
# ---------------------------------------------------------------------------


@given(
    offset_seconds=st.integers(
        min_value=-365 * 24 * 3600 * 10, max_value=365 * 24 * 3600 * 10
    )
)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_naturaltime_offsets(offset_seconds):
    """Any datetime offset from now produces a non-empty English string."""
    # Clear naturaltime's internal cache to avoid stale bucket interference
    naturaltime.cache_clear()
    dt = datetime.now() + timedelta(seconds=offset_seconds)
    result = naturaltime(dt)
    assert isinstance(result, str), (
        f"naturaltime returned non-string for offset {offset_seconds}"
    )
    assert len(result) > 0, (
        f"naturaltime returned empty string for offset {offset_seconds}"
    )
    # Must contain recognizable time words or "just now"
    time_words = {
        "just now",
        "second",
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "year",
        "ago",
        "in ",
    }
    has_time_word = any(w in result for w in time_words)
    assert has_time_word, (
        f"naturaltime(offset={offset_seconds}s) = {result!r} has no time word"
    )


# ---------------------------------------------------------------------------
# Property 6: apnumber — 0-9 spelled out, 10+ returned as digits
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=0, max_value=9))
@settings(max_examples=_ex(100), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_apnumber_spelled(value):
    """AP numbers 0-9 are spelled out as English words."""
    result = apnumber(value)
    expected_words = [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
    ]
    assert result == expected_words[value], (
        f"apnumber({value}) = {result!r}, expected {expected_words[value]!r}"
    )


@given(value=st.integers(min_value=10, max_value=10**6))
@settings(max_examples=_ex(100), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_apnumber_digits(value):
    """AP numbers >= 10 return the digit string."""
    result = apnumber(value)
    assert result == str(value), (
        f"apnumber({value}) = {result!r}, expected {str(value)!r}"
    )


# ---------------------------------------------------------------------------
# Property 7: Combined — applying multiple formatters in sequence doesn't crash
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=0, max_value=10**15))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_combined_formatters(value):
    """Applying ordinal, intcomma, intword, filesizeformat, apnumber to the same
    value produces strings without crashing."""
    results: list[str] = []
    results.append(ordinal(value))
    results.append(intcomma(value))
    results.append(intword(value))
    results.append(filesizeformat(value))
    results.append(apnumber(value))
    for i, r in enumerate(results):
        assert isinstance(r, str), (
            f"Formatter {i} returned non-string {type(r)} for value {value}"
        )
        assert len(r) > 0, f"Formatter {i} returned empty string for value {value}"


# ---------------------------------------------------------------------------
# Property 8: intcomma with floats — decimal values formatted correctly
# ---------------------------------------------------------------------------


@given(
    integer_part=st.integers(min_value=0, max_value=10**12),
    decimal_str=st.from_regex(r"\.[0-9]{1,6}", fullmatch=True),
)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_intcomma_float_string(integer_part, decimal_str):
    """intcomma on a float-like string preserves the decimal portion."""
    input_str = f"{integer_part}{decimal_str}"
    result = intcomma(input_str)
    assert isinstance(result, str), f"intcomma({input_str!r}) returned non-string"
    # Decimal portion must be preserved verbatim
    assert result.endswith(decimal_str), (
        f"intcomma({input_str!r}) = {result!r} lost decimal {decimal_str!r}"
    )
    # Integer portion with commas removed must match original
    int_portion = result[: -len(decimal_str)].replace(",", "")
    assert int_portion == str(integer_part), (
        f"intcomma({input_str!r}) integer portion {int_portion!r} != {str(integer_part)!r}"
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Humanize Hypothesis Fuzz Tests ──\n")

    tests = [
        ("ordinal suffix", test_ordinal_suffix),
        ("intcomma roundtrip", test_intcomma_roundtrip),
        ("intcomma format", test_intcomma_format),
        ("intword large", test_intword_large),
        ("intword small", test_intword_small),
        ("filesizeformat unit (int)", test_filesizeformat_unit),
        ("filesizeformat unit (float)", test_filesizeformat_float),
        ("naturaltime offsets", test_naturaltime_offsets),
        ("apnumber spelled", test_apnumber_spelled),
        ("apnumber digits", test_apnumber_digits),
        ("combined formatters", test_combined_formatters),
        ("intcomma float string", test_intcomma_float_string),
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
            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Humanize fuzz: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
