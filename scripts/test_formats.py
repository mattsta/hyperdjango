#!/usr/bin/env python3
"""Comprehensive tests for hyperdjango.formats module.

Tests all formatting functions: format_date, format_datetime, format_time,
format_short_date, format_short_datetime, format_number, format_currency,
format_percent, parse_date, parse_datetime, timezone utilities, calendar
utilities, and template filter registration.
"""

# hyper-test: unit

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hyperdjango.conf import DEFAULTS
from hyperdjango.formats import (
    format_currency,
    format_date,
    format_datetime,
    format_number,
    format_percent,
    format_short_date,
    format_short_datetime,
    format_time,
    get_first_day_of_week,
    get_week_start,
    is_aware,
    is_naive,
    localtime,
    make_aware,
    make_naive,
    now,
    parse_date,
    parse_datetime,
    register,
)
from hyperdjango.templating import Library

PASS = 0
FAIL = 0


def test(name, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    got:      {got!r}")
        print(f"    expected: {expected!r}")


# ── Helper: temporarily override DEFAULTS ────────────────────────────────────


class OverrideSettings:
    """Context manager to temporarily override DEFAULTS entries."""

    def __init__(self, **overrides):
        self.overrides = overrides
        self.originals: dict[str, object] = {}

    def __enter__(self):
        for key, value in self.overrides.items():
            self.originals[key] = DEFAULTS[key]
            DEFAULTS[key] = value
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for key, value in self.originals.items():
            DEFAULTS[key] = value
        return False


# ── format_date ──────────────────────────────────────────────────────────────


def test_format_date():
    print("format_date:")

    # Default format: "N j, Y" (AP month, day, year)
    d = date(2026, 3, 29)
    test("default format", format_date(d), "March 29, 2026")

    d2 = date(2026, 1, 5)
    test("default format jan", format_date(d2), "Jan. 5, 2026")

    d3 = date(2026, 9, 15)
    test("default format sep", format_date(d3), "Sep. 15, 2026")

    # Custom format strings
    d = date(2026, 3, 29)
    test("m/d/Y", format_date(d, "m/d/Y"), "03/29/2026")
    test("Y-m-d", format_date(d, "Y-m-d"), "2026-03-29")
    test("D, M j", format_date(d, "D, M j"), "Sun, Mar 29")
    test("l, F j, Y", format_date(d, "l, F j, Y"), "Sunday, March 29, 2026")

    # All date format chars — d (zero-padded day)
    test("d single digit", format_date(date(2026, 1, 5), "d"), "05")
    test("d double digit", format_date(date(2026, 1, 15), "d"), "15")

    # j (day without leading zero)
    test("j single digit", format_date(date(2026, 1, 5), "j"), "5")
    test("j double digit", format_date(date(2026, 1, 15), "j"), "15")

    # D (abbreviated weekday)
    test("D monday", format_date(date(2026, 3, 30), "D"), "Mon")
    test("D sunday", format_date(date(2026, 3, 29), "D"), "Sun")
    test("D wednesday", format_date(date(2026, 4, 1), "D"), "Wed")

    # l (full weekday)
    test("l monday", format_date(date(2026, 3, 30), "l"), "Monday")
    test("l sunday", format_date(date(2026, 3, 29), "l"), "Sunday")
    test("l friday", format_date(date(2026, 3, 27), "l"), "Friday")

    # m (zero-padded month)
    test("m january", format_date(date(2026, 1, 1), "m"), "01")
    test("m december", format_date(date(2026, 12, 1), "m"), "12")

    # n (month without leading zero)
    test("n january", format_date(date(2026, 1, 1), "n"), "1")
    test("n december", format_date(date(2026, 12, 1), "n"), "12")

    # M (abbreviated month name)
    test("M january", format_date(date(2026, 1, 1), "M"), "Jan")
    test("M march", format_date(date(2026, 3, 1), "M"), "Mar")
    test("M december", format_date(date(2026, 12, 1), "M"), "Dec")

    # N (AP style month abbreviation)
    test("N january", format_date(date(2026, 1, 1), "N"), "Jan.")
    test("N february", format_date(date(2026, 2, 1), "N"), "Feb.")
    test("N march", format_date(date(2026, 3, 1), "N"), "March")
    test("N april", format_date(date(2026, 4, 1), "N"), "April")
    test("N may", format_date(date(2026, 5, 1), "N"), "May")
    test("N june", format_date(date(2026, 6, 1), "N"), "June")
    test("N july", format_date(date(2026, 7, 1), "N"), "July")
    test("N august", format_date(date(2026, 8, 1), "N"), "Aug.")
    test("N september", format_date(date(2026, 9, 1), "N"), "Sep.")
    test("N october", format_date(date(2026, 10, 1), "N"), "Oct.")
    test("N november", format_date(date(2026, 11, 1), "N"), "Nov.")
    test("N december", format_date(date(2026, 12, 1), "N"), "Dec.")

    # F (full month name)
    test("F january", format_date(date(2026, 1, 1), "F"), "January")
    test("F march", format_date(date(2026, 3, 1), "F"), "March")
    test("F december", format_date(date(2026, 12, 1), "F"), "December")

    # y (two-digit year)
    test("y 2026", format_date(date(2026, 1, 1), "y"), "26")
    test("y 2000", format_date(date(2000, 1, 1), "y"), "00")
    test("y 1999", format_date(date(1999, 1, 1), "y"), "99")

    # Y (four-digit year)
    test("Y 2026", format_date(date(2026, 1, 1), "Y"), "2026")
    test("Y 2000", format_date(date(2000, 1, 1), "Y"), "2000")

    # S (ordinal suffix)
    test("S 1st", format_date(date(2026, 1, 1), "S"), "st")
    test("S 2nd", format_date(date(2026, 1, 2), "S"), "nd")
    test("S 3rd", format_date(date(2026, 1, 3), "S"), "rd")
    test("S 4th", format_date(date(2026, 1, 4), "S"), "th")
    test("S 11th", format_date(date(2026, 1, 11), "S"), "th")
    test("S 21st", format_date(date(2026, 1, 21), "S"), "st")

    # L (leap year)
    test("L 2024 leap", format_date(date(2024, 1, 1), "L"), "1")
    test("L 2026 not leap", format_date(date(2026, 1, 1), "L"), "0")

    # t (days in month)
    test("t february non-leap", format_date(date(2026, 2, 1), "t"), "28")
    test("t february leap", format_date(date(2024, 2, 1), "t"), "29")
    test("t january", format_date(date(2026, 1, 1), "t"), "31")
    test("t april", format_date(date(2026, 4, 1), "t"), "30")

    # z (day of year)
    test("z jan 1", format_date(date(2026, 1, 1), "z"), "1")
    test("z dec 31 non-leap", format_date(date(2026, 12, 31), "z"), "365")
    test("z dec 31 leap", format_date(date(2024, 12, 31), "z"), "366")

    # W (ISO week number)
    test("W jan 1 2026", format_date(date(2026, 1, 1), "W"), "01")

    # w (weekday number)
    # Django convention: 0=Sunday, 1=Monday, ..., 6=Saturday
    test("w monday", format_date(date(2026, 3, 30), "w"), "1")
    test("w sunday", format_date(date(2026, 3, 29), "w"), "0")

    # Backslash escaping
    test("backslash escape hyphens", format_date(d, "Y\\-m\\-d"), "2026-03-29")
    test("backslash escape slash", format_date(d, "m\\/d\\/Y"), "03/29/2026")
    test("backslash escape letters", format_date(d, "\\Y\\e\\a\\r: Y"), "Year: 2026")

    # Edge cases
    test("jan 1", format_date(date(2026, 1, 1), "F j, Y"), "January 1, 2026")
    test("dec 31", format_date(date(2026, 12, 31), "F j, Y"), "December 31, 2026")
    test(
        "leap day feb 29", format_date(date(2024, 2, 29), "F j, Y"), "February 29, 2024"
    )

    # Date with datetime input
    dt = datetime(2026, 3, 29, 14, 30, 0)
    test("datetime as date input", format_date(dt, "Y-m-d"), "2026-03-29")


# ── format_datetime ──────────────────────────────────────────────────────────


def test_format_datetime():
    print("format_datetime:")

    # Default format: "N j, Y, P"
    dt = datetime(2026, 3, 29, 15, 45, 0)
    test("default format", format_datetime(dt), "March 29, 2026, 3:45 p.m.")

    dt_morning = datetime(2026, 3, 29, 9, 5, 0)
    test("default morning", format_datetime(dt_morning), "March 29, 2026, 9:05 a.m.")

    # Time format chars
    dt = datetime(2026, 3, 29, 14, 5, 9, 123456)

    # G (hour 24h no leading zero)
    test("G afternoon", format_datetime(dt, "G"), "14")
    test("G midnight", format_datetime(datetime(2026, 1, 1, 0, 0), "G"), "0")
    test("G 9am", format_datetime(datetime(2026, 1, 1, 9, 0), "G"), "9")

    # H (hour 24h with leading zero)
    test("H afternoon", format_datetime(dt, "H"), "14")
    test("H midnight", format_datetime(datetime(2026, 1, 1, 0, 0), "H"), "00")
    test("H 9am", format_datetime(datetime(2026, 1, 1, 9, 0), "H"), "09")

    # g (hour 12h no leading zero)
    test("g 2pm", format_datetime(dt, "g"), "2")
    test("g midnight", format_datetime(datetime(2026, 1, 1, 0, 0), "g"), "12")
    test("g noon", format_datetime(datetime(2026, 1, 1, 12, 0), "g"), "12")
    test("g 1pm", format_datetime(datetime(2026, 1, 1, 13, 0), "g"), "1")

    # h (hour 12h with leading zero)
    test("h 2pm", format_datetime(dt, "h"), "02")
    test("h midnight", format_datetime(datetime(2026, 1, 1, 0, 0), "h"), "12")
    test("h 9am", format_datetime(datetime(2026, 1, 1, 9, 0), "h"), "09")

    # i (minutes with leading zero)
    test("i 05", format_datetime(dt, "i"), "05")
    test("i 30", format_datetime(datetime(2026, 1, 1, 0, 30), "i"), "30")
    test("i 00", format_datetime(datetime(2026, 1, 1, 0, 0), "i"), "00")

    # s (seconds with leading zero)
    test("s 09", format_datetime(dt, "s"), "09")
    test("s 00", format_datetime(datetime(2026, 1, 1, 0, 0, 0), "s"), "00")
    test("s 59", format_datetime(datetime(2026, 1, 1, 0, 0, 59), "s"), "59")

    # u (microseconds)
    test("u 123456", format_datetime(dt, "u"), "123456")
    test("u zero", format_datetime(datetime(2026, 1, 1, 0, 0, 0, 0), "u"), "000000")

    # A (AM/PM uppercase)
    test("A morning", format_datetime(datetime(2026, 1, 1, 9, 0), "A"), "AM")
    test("A afternoon", format_datetime(datetime(2026, 1, 1, 14, 0), "A"), "PM")
    test("A midnight", format_datetime(datetime(2026, 1, 1, 0, 0), "A"), "AM")
    test("A noon", format_datetime(datetime(2026, 1, 1, 12, 0), "A"), "PM")

    # a (a.m./p.m.)
    test("a morning", format_datetime(datetime(2026, 1, 1, 9, 0), "a"), "a.m.")
    test("a afternoon", format_datetime(datetime(2026, 1, 1, 14, 0), "a"), "p.m.")

    # P (AP style time)
    test("P midnight", format_datetime(datetime(2026, 1, 1, 0, 0), "P"), "midnight")
    test("P noon", format_datetime(datetime(2026, 1, 1, 12, 0), "P"), "noon")
    test("P 12:30am", format_datetime(datetime(2026, 1, 1, 0, 30), "P"), "12:30 a.m.")
    test("P 12:30pm", format_datetime(datetime(2026, 1, 1, 12, 30), "P"), "12:30 p.m.")
    test("P 3:45pm", format_datetime(datetime(2026, 1, 1, 15, 45), "P"), "3:45 p.m.")
    test("P 1am", format_datetime(datetime(2026, 1, 1, 1, 0), "P"), "1 a.m.")
    test("P 11pm", format_datetime(datetime(2026, 1, 1, 23, 0), "P"), "11 p.m.")
    test("P 11:59pm", format_datetime(datetime(2026, 1, 1, 23, 59), "P"), "11:59 p.m.")

    # c (ISO 8601)
    dt_utc = datetime(2026, 3, 29, 14, 30, 0, tzinfo=UTC)
    test("c ISO 8601 aware", format_datetime(dt_utc, "c"), "2026-03-29T14:30:00+00:00")
    dt_naive = datetime(2026, 3, 29, 14, 30, 0)
    test("c ISO 8601 naive", format_datetime(dt_naive, "c"), "2026-03-29T14:30:00")

    # r (RFC 2822)
    test("r RFC 2822", format_datetime(dt_utc, "r"), "Sun, 29 Mar 2026 14:30:00 +0000")
    dt_mon = datetime(2026, 3, 30, 9, 0, 0, tzinfo=UTC)
    test(
        "r RFC 2822 monday",
        format_datetime(dt_mon, "r"),
        "Mon, 30 Mar 2026 09:00:00 +0000",
    )

    # U (unix timestamp)
    dt_epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
    test("U epoch", format_datetime(dt_epoch, "U"), "0")

    # Timezone chars with aware datetime
    dt_utc = datetime(2026, 3, 29, 14, 30, 0, tzinfo=UTC)
    test("e timezone name utc", format_datetime(dt_utc, "e"), "UTC")
    test("O offset utc", format_datetime(dt_utc, "O"), "+0000")
    test("T abbrev utc", format_datetime(dt_utc, "T"), "UTC")
    test("Z seconds offset utc", format_datetime(dt_utc, "Z"), "0")

    # Timezone chars with offset timezone
    eastern = ZoneInfo("America/New_York")
    dt_eastern = datetime(2026, 3, 29, 14, 30, 0, tzinfo=eastern)
    test("e eastern", format_datetime(dt_eastern, "e"), "America/New_York")

    # Timezone chars with naive datetime (should be empty)
    dt_naive = datetime(2026, 3, 29, 14, 30, 0)
    test("e naive empty", format_datetime(dt_naive, "e"), "")
    test("O naive empty", format_datetime(dt_naive, "O"), "")
    test("T naive empty", format_datetime(dt_naive, "T"), "")
    test("Z naive empty", format_datetime(dt_naive, "Z"), "")

    # Combined format
    dt = datetime(2026, 3, 29, 15, 45, 30)
    test(
        "combined Y-m-d H:i:s",
        format_datetime(dt, "Y-m-d H:i:s"),
        "2026-03-29 15:45:30",
    )
    test("combined h:i A", format_datetime(dt, "h:i A"), "03:45 PM")


# ── format_time ──────────────────────────────────────────────────────────────


def test_format_time():
    print("format_time:")

    # Default format: "P" (AP style)
    t = time(15, 45, 0)
    test("default format", format_time(t), "3:45 p.m.")

    t_midnight = time(0, 0, 0)
    test("default midnight", format_time(t_midnight), "midnight")

    t_noon = time(12, 0, 0)
    test("default noon", format_time(t_noon), "noon")

    # Custom formats
    t = time(14, 5, 9)
    test("H:i:s", format_time(t, "H:i:s"), "14:05:09")
    test("g:i A", format_time(t, "g:i A"), "2:05 PM")
    test("h:i a", format_time(t, "h:i a"), "02:05 p.m.")

    # With datetime input
    dt = datetime(2026, 3, 29, 15, 30, 0)
    test("datetime input", format_time(dt, "H:i"), "15:30")

    # Midnight and noon edge cases for P
    test("P midnight time", format_time(time(0, 0), "P"), "midnight")
    test("P noon time", format_time(time(12, 0), "P"), "noon")
    test("P 12:01am", format_time(time(0, 1), "P"), "12:01 a.m.")
    test("P 12:01pm", format_time(time(12, 1), "P"), "12:01 p.m.")


# ── format_short_date / format_short_datetime ────────────────────────────────


def test_short_formats():
    print("format_short_date / format_short_datetime:")

    # SHORT_DATE_FORMAT default: "m/d/Y"
    d = date(2026, 3, 29)
    test("short date", format_short_date(d), "03/29/2026")
    test("short date jan", format_short_date(date(2026, 1, 5)), "01/05/2026")

    # SHORT_DATETIME_FORMAT default: "m/d/Y P"
    dt = datetime(2026, 3, 29, 15, 45, 0)
    test("short datetime", format_short_datetime(dt), "03/29/2026 3:45 p.m.")

    dt_midnight = datetime(2026, 1, 1, 0, 0, 0)
    test(
        "short datetime midnight",
        format_short_datetime(dt_midnight),
        "01/01/2026 midnight",
    )


# ── format_number ────────────────────────────────────────────────────────────


def test_format_number():
    print("format_number:")

    # Default: USE_THOUSAND_SEPARATOR=False, DECIMAL_SEPARATOR=".", NUMBER_GROUPING=3
    test("integer no sep", format_number(1000), "1000")
    test("float no sep", format_number(1234.56), "1234.56")

    # With USE_THOUSAND_SEPARATOR=True
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True):
        test("1000 with sep", format_number(1000), "1,000")
        test("1234567 with sep", format_number(1234567), "1,234,567")
        test("1234567890 with sep", format_number(1234567890), "1,234,567,890")
        test("float with sep", format_number(1234567.89), "1,234,567.89")
        test("100 no grouping needed", format_number(100), "100")
        test("negative with sep", format_number(-1234), "-1,234")
        test("negative large", format_number(-1234567), "-1,234,567")
        test("zero", format_number(0), "0")

    # Decimal places
    test("decimal places 2", format_number(3.14159, decimal_places=2), "3.14")
    test("decimal places 0", format_number(3.14159, decimal_places=0), "3")
    test("decimal places pad", format_number(3.1, decimal_places=4), "3.1000")
    test("decimal places int", format_number(42, decimal_places=2), "42.00")

    # Large numbers with thousand sep and decimals
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True):
        test(
            "large with dec",
            format_number(1234567.89, decimal_places=2),
            "1,234,567.89",
        )

    # String input
    test("string input", format_number("1234.56"), "1234.56")
    test("string int", format_number("1000"), "1000")
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True):
        test("string with sep", format_number("1000"), "1,000")

    # Zero and small numbers
    test("zero int", format_number(0), "0")
    test("zero float", format_number(0.0), "0.0")
    test("small float", format_number(0.001), "0.001")
    test("small with places", format_number(0.001, decimal_places=2), "0.00")

    # Custom separators (European style)
    with OverrideSettings(
        USE_THOUSAND_SEPARATOR=True,
        DECIMAL_SEPARATOR=",",
        THOUSAND_SEPARATOR=".",
    ):
        test("european 1234.56", format_number(1234.56), "1.234,56")
        test("european 1000000", format_number(1000000), "1.000.000")
        test("european negative", format_number(-1234.56), "-1.234,56")

    # Indian grouping (grouping=2 but first group is 3 — but our impl uses fixed grouping)
    # With NUMBER_GROUPING=2 and USE_THOUSAND_SEPARATOR=True
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True, NUMBER_GROUPING=2):
        test("grouping 2: 12345", format_number(12345), "1,23,45")
        test("grouping 2: 1234567", format_number(1234567), "1,23,45,67")

    # NUMBER_GROUPING=0 (disabled)
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True, NUMBER_GROUPING=0):
        test("grouping 0 no sep", format_number(1234567), "1234567")

    # Very small numbers
    test("very small", format_number(0.0000001), "1e-07")
    test(
        "very small dec places", format_number(0.0000001, decimal_places=7), "0.0000001"
    )

    # Negative zero
    test("negative float zero", format_number(-0.0), "-0.0")

    # Regression: scientific-notation floats must NOT be digit-grouped.
    # str(1e20) == "1e+20"; grouping previously produced garbage "1e,+20".
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True):
        test("huge float sci notation", format_number(1e20), "1e+20")
        test("negative huge float", format_number(-1e20), "-1e+20")
        test("tiny float sci notation", format_number(1e-10), "1e-10")


# ── format_currency ──────────────────────────────────────────────────────────


def test_format_currency():
    print("format_currency:")

    # Default: "$", 2 decimal places, always uses thousand separator
    test("basic", format_currency(1234.56), "$1,234.56")
    test("round number", format_currency(1000), "$1,000.00")
    test("small", format_currency(5.5), "$5.50")
    test("zero", format_currency(0), "$0.00")
    test("large", format_currency(1234567890.12), "$1,234,567,890.12")

    # Custom symbol
    test("euro", format_currency(1234.56, currency_symbol="€"), "€1,234.56")
    test("pound", format_currency(1234.56, currency_symbol="£"), "£1,234.56")
    test(
        "yen no decimals",
        format_currency(1234, currency_symbol="¥", decimal_places=0),
        "¥1,234",
    )

    # Negative amounts
    test("negative", format_currency(-1234.56), "-$1,234.56")
    test("negative euro", format_currency(-500.0, currency_symbol="€"), "-€500.00")

    # Custom decimal places
    test("3 decimal places", format_currency(1234.567, decimal_places=3), "$1,234.567")
    test("0 decimal places", format_currency(1234.56, decimal_places=0), "$1,235")

    # European separators
    with OverrideSettings(DECIMAL_SEPARATOR=",", THOUSAND_SEPARATOR="."):
        test("european currency", format_currency(1234.56), "$1.234,56")


# ── format_percent ───────────────────────────────────────────────────────────


def test_format_percent():
    print("format_percent:")

    test("15.6%", format_percent(0.156), "15.6%")
    test("100.0%", format_percent(1.0), "100.0%")
    test("0.0%", format_percent(0.0), "0.0%")
    test("50.0%", format_percent(0.5), "50.0%")
    test("99.9%", format_percent(0.999), "99.9%")
    test("200.0%", format_percent(2.0), "200.0%")

    # Custom decimal places
    test("0 decimals", format_percent(0.156, decimal_places=0), "16%")
    test("2 decimals", format_percent(0.15678, decimal_places=2), "15.68%")
    test("3 decimals", format_percent(0.123456, decimal_places=3), "12.346%")

    # Negative
    test("negative", format_percent(-0.05), "-5.0%")

    # Very small
    test("very small", format_percent(0.001), "0.1%")

    # European decimal separator
    with OverrideSettings(DECIMAL_SEPARATOR=","):
        test("european percent", format_percent(0.156), "15,6%")


# ── parse_date ───────────────────────────────────────────────────────────────


def test_parse_date():
    print("parse_date:")

    # Default formats: ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]
    test("ISO format", parse_date("2026-03-29"), date(2026, 3, 29))
    test("US format", parse_date("03/29/2026"), date(2026, 3, 29))
    test("US short year", parse_date("03/29/26"), date(2026, 3, 29))

    # Edge cases
    test("jan 1", parse_date("2026-01-01"), date(2026, 1, 1))
    test("dec 31", parse_date("2026-12-31"), date(2026, 12, 31))
    test("leap day", parse_date("2024-02-29"), date(2024, 2, 29))

    # Invalid input
    test("invalid garbage", parse_date("not-a-date"), None)
    test("empty string", parse_date(""), None)
    test("partial date", parse_date("2026-03"), None)
    test("invalid date", parse_date("2026-13-01"), None)
    test("invalid day", parse_date("2026-02-30"), None)

    # Whitespace handling
    test("leading whitespace", parse_date("  2026-03-29"), date(2026, 3, 29))
    test("trailing whitespace", parse_date("2026-03-29  "), date(2026, 3, 29))


# ── parse_datetime ───────────────────────────────────────────────────────────


def test_parse_datetime():
    print("parse_datetime:")

    # Default formats: ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S"]
    test(
        "ISO full",
        parse_datetime("2026-03-29 14:30:00"),
        datetime(2026, 3, 29, 14, 30, 0),
    )
    test(
        "ISO no seconds",
        parse_datetime("2026-03-29 14:30"),
        datetime(2026, 3, 29, 14, 30),
    )
    test(
        "US full",
        parse_datetime("03/29/2026 14:30:00"),
        datetime(2026, 3, 29, 14, 30, 0),
    )

    # Edge cases
    test(
        "midnight",
        parse_datetime("2026-03-29 00:00:00"),
        datetime(2026, 3, 29, 0, 0, 0),
    )
    test(
        "end of day",
        parse_datetime("2026-03-29 23:59:59"),
        datetime(2026, 3, 29, 23, 59, 59),
    )

    # Invalid input
    test("invalid garbage", parse_datetime("not-a-datetime"), None)
    test("empty string", parse_datetime(""), None)
    test("date only", parse_datetime("2026-03-29"), None)
    test("invalid time", parse_datetime("2026-03-29 25:00:00"), None)

    # Whitespace handling
    test(
        "whitespace",
        parse_datetime("  2026-03-29 14:30:00  "),
        datetime(2026, 3, 29, 14, 30, 0),
    )


# ── Timezone utilities ───────────────────────────────────────────────────────


def test_timezone_utils():
    print("timezone utilities:")

    # now() with USE_TZ=True (default)
    result = now()
    test("now is aware", is_aware(result), True)
    test("now is utc", result.tzinfo, UTC)

    # now() with USE_TZ=False
    with OverrideSettings(USE_TZ=False):
        result = now()
        test("now naive when USE_TZ=False", is_naive(result), True)

    # localtime() — converts UTC to local timezone
    dt_utc = datetime(2026, 3, 29, 14, 0, 0, tzinfo=UTC)
    local = localtime(dt_utc, "America/New_York")
    test("localtime hour", local.hour, 10)  # EDT is UTC-4
    test("localtime tz", str(local.tzinfo), "America/New_York")

    # localtime() with naive datetime (assumed UTC)
    dt_naive = datetime(2026, 3, 29, 14, 0, 0)
    local = localtime(dt_naive, "America/New_York")
    test("localtime naive hour", local.hour, 10)

    # localtime() with default timezone (TIME_ZONE=UTC)
    local_default = localtime(dt_utc)
    test("localtime default tz hour", local_default.hour, 14)

    # make_aware() on naive datetime
    dt_naive = datetime(2026, 3, 29, 14, 0, 0)
    aware = make_aware(dt_naive)
    test("make_aware is aware", is_aware(aware), True)
    test("make_aware default tz", str(aware.tzinfo), "UTC")

    # make_aware() with custom timezone
    aware_eastern = make_aware(dt_naive, "America/New_York")
    test("make_aware custom tz", str(aware_eastern.tzinfo), "America/New_York")
    test("make_aware preserves time", aware_eastern.hour, 14)

    # make_aware() on already aware raises ValueError
    try:
        make_aware(aware)
        test("make_aware already aware raises", False, True)
    except ValueError:
        test("make_aware already aware raises", True, True)

    # make_naive() strips timezone
    dt_utc = datetime(2026, 3, 29, 14, 0, 0, tzinfo=UTC)
    naive = make_naive(dt_utc)
    test("make_naive is naive", is_naive(naive), True)
    test("make_naive tzinfo None", naive.tzinfo, None)
    test("make_naive preserves hour (utc->utc)", naive.hour, 14)

    # make_naive() converts to target tz then strips
    naive_eastern = make_naive(dt_utc, "America/New_York")
    test("make_naive to eastern", naive_eastern.hour, 10)
    test("make_naive to eastern tzinfo", naive_eastern.tzinfo, None)

    # make_naive() on already naive returns as-is
    dt_naive = datetime(2026, 3, 29, 14, 0, 0)
    result = make_naive(dt_naive)
    test("make_naive already naive", result, dt_naive)

    # is_aware / is_naive
    dt_aware = datetime(2026, 3, 29, 14, 0, 0, tzinfo=UTC)
    dt_naive = datetime(2026, 3, 29, 14, 0, 0)
    test("is_aware true", is_aware(dt_aware), True)
    test("is_aware false", is_aware(dt_naive), False)
    test("is_naive true", is_naive(dt_naive), True)
    test("is_naive false", is_naive(dt_aware), False)


# ── Calendar utilities ───────────────────────────────────────────────────────


def test_calendar_utils():
    print("calendar utilities:")

    # get_first_day_of_week() — default is 0 (Sunday)
    test("first day default", get_first_day_of_week(), 0)

    # get_week_start() with default Sunday start
    # 2026-03-29 is a Sunday
    sunday = date(2026, 3, 29)
    test("week start sunday", get_week_start(sunday), date(2026, 3, 29))

    # 2026-03-30 is a Monday
    monday = date(2026, 3, 30)
    test("week start monday (sun start)", get_week_start(monday), date(2026, 3, 29))

    # 2026-04-04 is a Saturday
    saturday = date(2026, 4, 4)
    test("week start saturday (sun start)", get_week_start(saturday), date(2026, 3, 29))

    # 2026-03-31 is a Tuesday
    tuesday = date(2026, 3, 31)
    test("week start tuesday (sun start)", get_week_start(tuesday), date(2026, 3, 29))

    # Wednesday
    wednesday = date(2026, 4, 1)
    test(
        "week start wednesday (sun start)", get_week_start(wednesday), date(2026, 3, 29)
    )

    # Thursday
    thursday = date(2026, 4, 2)
    test("week start thursday (sun start)", get_week_start(thursday), date(2026, 3, 29))

    # Friday
    friday = date(2026, 4, 3)
    test("week start friday (sun start)", get_week_start(friday), date(2026, 3, 29))

    # With FIRST_DAY_OF_WEEK=1 (Monday)
    with OverrideSettings(FIRST_DAY_OF_WEEK=1):
        test("first day monday setting", get_first_day_of_week(), 1)
        # 2026-03-30 is Monday
        test("week start monday (mon start)", get_week_start(monday), date(2026, 3, 30))
        # 2026-03-29 is Sunday — week started the previous Monday (March 23)
        test("week start sunday (mon start)", get_week_start(sunday), date(2026, 3, 23))
        # Saturday April 4 — week started Monday March 30
        test(
            "week start saturday (mon start)",
            get_week_start(saturday),
            date(2026, 3, 30),
        )

    # With FIRST_DAY_OF_WEEK=6 (Saturday)
    with OverrideSettings(FIRST_DAY_OF_WEEK=6):
        # Saturday April 4 — IS the start
        test(
            "week start saturday (sat start)",
            get_week_start(saturday),
            date(2026, 4, 4),
        )
        # Sunday March 29 — prev Saturday is March 28
        test("week start sunday (sat start)", get_week_start(sunday), date(2026, 3, 28))


# ── Template filter registration ─────────────────────────────────────────────


def test_template_filters():
    print("template filters:")

    # Verify register is a Library
    test("register is Library", isinstance(register, Library), True)
    test("register name", register.name, "formats")

    # Verify filters are registered
    test("date filter exists", "date" in register.filters, True)
    test("time filter exists", "time" in register.filters, True)
    test("datetime filter exists", "datetime" in register.filters, True)
    test("number filter exists", "number" in register.filters, True)
    test("currency filter exists", "currency" in register.filters, True)
    test("short_date filter exists", "short_date" in register.filters, True)

    # Verify filter functions are callable
    date_filter = register.filters["date"]
    test("date filter callable", callable(date_filter), True)

    # Filter returns empty string for None
    test("date filter None", date_filter(None), "")
    test("time filter None", register.filters["time"](None), "")
    test("datetime filter None", register.filters["datetime"](None), "")
    test("number filter None", register.filters["number"](None), "")
    test("currency filter None", register.filters["currency"](None), "")
    test("short_date filter None", register.filters["short_date"](None), "")

    # Filter with value
    d = date(2026, 3, 29)
    test("date filter value", date_filter(d), "March 29, 2026")
    test("date filter with arg", date_filter(d, "Y-m-d"), "2026-03-29")

    # Number filter with decimal places arg
    test("number filter", register.filters["number"](1234.56), "1234.56")
    test("number filter with arg", register.filters["number"](1234.56, "2"), "1234.56")

    # Currency filter with symbol arg
    test("currency filter", register.filters["currency"](99.99), "$99.99")
    test("currency filter euro", register.filters["currency"](99.99, "€"), "€99.99")


# ── Additional edge cases ────────────────────────────────────────────────────


def test_edge_cases():
    print("edge cases:")

    # format_date with datetime (should work fine)
    dt = datetime(2026, 6, 15, 10, 30)
    test("format_date with datetime", format_date(dt, "Y-m-d"), "2026-06-15")

    # format_time with time object
    t = time(23, 59, 59, 999999)
    test("format_time microseconds", format_time(t, "H:i:s.u"), "23:59:59.999999")

    # Literal characters pass through
    d = date(2026, 3, 29)
    test("literal chars", format_date(d, "Y/m/d"), "2026/03/29")
    test("literal spaces", format_date(d, "j F Y"), "29 March 2026")
    test("literal comma", format_date(d, "F j, Y"), "March 29, 2026")

    # Empty format string
    test("empty format", format_date(d, ""), "")

    # All-escaped format string
    test("all escaped", format_date(d, "\\Y\\m\\d"), "Ymd")

    # Backslash at end of string (edge case)
    test("trailing backslash", format_date(d, "Y\\"), "2026")

    # format_number with string that has whitespace
    test("string whitespace", format_number("  1234.56  "), "1234.56")

    # format_number negative string
    test("negative string", format_number("-1234.56"), "-1234.56")
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True):
        test("negative string with sep", format_number("-1234.56"), "-1,234.56")

    # format_currency with very small amount
    test("currency small", format_currency(0.01), "$0.01")
    test("currency tiny", format_currency(0.001), "$0.00")

    # format_percent edge
    test("percent 1/3", format_percent(1 / 3, decimal_places=2), "33.33%")

    # RFC 2822 with non-UTC timezone
    eastern = ZoneInfo("America/New_York")
    dt_east = datetime(2026, 3, 29, 10, 0, 0, tzinfo=eastern)
    rfc = format_datetime(dt_east, "r")
    test("rfc2822 eastern starts with Sun", rfc[:3], "Sun")
    # EDT offset is -0400
    test("rfc2822 eastern offset", rfc.endswith("-0400"), True)

    # Naive datetime RFC 2822 — defaults to +0000
    dt_naive = datetime(2026, 3, 29, 10, 0, 0)
    rfc_naive = format_datetime(dt_naive, "r")
    test("rfc2822 naive offset", rfc_naive.endswith("+0000"), True)

    # RFC 2822 single-digit day — no space padding, just the digit
    dt_single_day = datetime(2026, 3, 5, 14, 30, 0, tzinfo=UTC)
    rfc_single = format_datetime(dt_single_day, "r")
    test("rfc2822 single digit day", rfc_single, "Thu, 5 Mar 2026 14:30:00 +0000")

    # format_currency negative zero — should display as "$0.00" not "-$0.00"
    test("currency neg zero float", format_currency(-0.0), "$0.00")
    test("currency neg zero int", format_currency(0), "$0.00")

    # format_number NaN and Infinity
    test("number nan", format_number(float("nan")), "NaN")
    test("number inf", format_number(float("inf")), "Infinity")
    test("number neg inf", format_number(float("-inf")), "-Infinity")

    # format_number very large number with thousand sep
    with OverrideSettings(USE_THOUSAND_SEPARATOR=True):
        test("number 10**18", format_number(10**18), "1,000,000,000,000,000,000")

    # Ordinal suffix for all teens (11th, 12th, 13th)
    test("S 11th", format_date(date(2026, 1, 11), "S"), "th")
    test("S 12th", format_date(date(2026, 1, 12), "S"), "th")
    test("S 13th", format_date(date(2026, 1, 13), "S"), "th")

    # Leap year edge cases
    test("L 1900 not leap", format_date(date(1900, 1, 1), "L"), "0")
    test("L 2000 is leap", format_date(date(2000, 1, 1), "L"), "1")

    # W (ISO week) zero-padded
    # Jan 4 is always in week 01
    test("W week 01", format_date(date(2026, 1, 4), "W"), "01")

    # e timezone — empty for naive, name for aware
    test("e naive empty", format_datetime(datetime(2026, 1, 1), "e"), "")
    test("e utc", format_datetime(datetime(2026, 1, 1, tzinfo=UTC), "e"), "UTC")

    # P format with midnight from date->datetime conversion (time=00:00)
    test("P midnight from date", format_date(date(2026, 1, 1), "P"), "midnight")

    # get_week_start all 7 days with Sunday start (FIRST_DAY_OF_WEEK=0)
    # Week of 2026-03-29 (Sunday) through 2026-04-04 (Saturday)
    for i in range(7):
        d = date(2026, 3, 29) + timedelta(days=i)
        test(f"week_start sun_start day {i}", get_week_start(d), date(2026, 3, 29))

    # get_week_start all 7 days with Monday start
    with OverrideSettings(FIRST_DAY_OF_WEEK=1):
        # Week of 2026-03-30 (Monday) through 2026-04-05 (Sunday)
        for i in range(7):
            d = date(2026, 3, 30) + timedelta(days=i)
            test(f"week_start mon_start day {i}", get_week_start(d), date(2026, 3, 30))


# ── Run all tests ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_format_date()
    test_format_datetime()
    test_format_time()
    test_short_formats()
    test_format_number()
    test_format_currency()
    test_format_percent()
    test_parse_date()
    test_parse_datetime()
    test_timezone_utils()
    test_calendar_utils()
    test_template_filters()
    test_edge_cases()

    print(f"\n{'=' * 60}")
    print(f"formats: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
