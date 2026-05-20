"""
Locale-aware formatting utilities for dates, times, numbers, and timezones.

Wires all 15 i18n settings from conf.py into actual formatting behavior.
Format characters for date/time, locale-aware number
formatting, input parsing, and timezone operations.

Usage:
    from hyperdjango.formats import format_date, format_number, now, localtime

    # Date formatting with Django format characters
    format_date(date.today())                  # "March 29, 2026"
    format_date(date.today(), "m/d/Y")         # "03/29/2026"

    # Number formatting with locale separators
    format_number(1234567.89)                  # "1,234,567.89"
    format_currency(1234.5)                    # "$1,234.50"

    # Timezone-aware current time
    dt = now()                                 # datetime with UTC tzinfo
    local = localtime(dt, "America/New_York")        # converted to Eastern
"""

import calendar
import math
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal as _Decimal
from zoneinfo import ZoneInfo

from hyperdjango.conf import get_setting
from hyperdjango.templating import Library

__all__ = [
    "format_date",
    "format_datetime",
    "format_time",
    "format_short_date",
    "format_short_datetime",
    "format_number",
    "format_currency",
    "format_percent",
    "parse_date",
    "parse_datetime",
    "now",
    "localtime",
    "make_aware",
    "make_naive",
    "is_aware",
    "is_naive",
    "get_first_day_of_week",
    "get_week_start",
    "register",
]

# ── Month and day name constants ─────────────────────────────────────────────

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_ABBREVS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_MONTH_AP = (
    "Jan.",
    "Feb.",
    "March",
    "April",
    "May",
    "June",
    "July",
    "Aug.",
    "Sep.",
    "Oct.",
    "Nov.",
    "Dec.",
)
_DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_DAY_ABBREVS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# ── Thread-safe ZoneInfo cache ───────────────────────────────────────────────

_tz_cache: dict[str, ZoneInfo] = {}
_tz_lock = threading.Lock()


def _get_tz(name: str) -> ZoneInfo:
    """Get a ZoneInfo instance, caching for reuse (thread-safe)."""
    tz = _tz_cache.get(name)
    if tz is not None:
        return tz
    with _tz_lock:
        # Double-check after acquiring lock
        tz = _tz_cache.get(name)
        if tz is not None:
            return tz
        tz = ZoneInfo(name)
        _tz_cache[name] = tz
        return tz


def _get_default_tz() -> ZoneInfo:
    """Get the ZoneInfo for the TIME_ZONE setting."""
    tz_name: str = get_setting("TIME_ZONE")  # type: ignore[assignment]
    return _get_tz(tz_name)


# ── Django format character mappings ─────────────────────────────────────────
# Each entry maps a single format character to a callable that takes a
# datetime (or date/time) and returns the formatted string.

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}


def _ordinal_suffix(n: int) -> str:
    """Return the English ordinal suffix for a number (st, nd, rd, th)."""
    return _ORDINAL_SUFFIXES.get(n, "th")


def _ap_time(dt: datetime | time) -> str:
    """Format time in AP style: midnight, noon, 1 a.m., 1:30 p.m."""
    hour = dt.hour
    minute = dt.minute
    if hour == 0 and minute == 0:
        return "midnight"
    if hour == 12 and minute == 0:
        return "noon"
    if hour == 0:
        display_hour = 12
        suffix = "a.m."
    elif hour < 12:
        display_hour = hour
        suffix = "a.m."
    elif hour == 12:
        display_hour = 12
        suffix = "p.m."
    else:
        display_hour = hour - 12
        suffix = "p.m."
    if minute == 0:
        return f"{display_hour} {suffix}"
    return f"{display_hour}:{minute:02d} {suffix}"


def _iso8601(dt: datetime) -> str:
    """ISO 8601 format: 2026-03-29T14:30:00+00:00."""
    return dt.isoformat()


def _rfc2822(dt: datetime) -> str:
    """RFC 2822 format: Sun, 29 Mar 2026 14:30:00 +0000."""
    # weekday: 0=Monday in Python
    weekday = dt.weekday()
    day_abbr = _DAY_ABBREVS[weekday]
    month_abbr = _MONTH_ABBREVS[dt.month - 1]
    if dt.tzinfo is not None:
        offset = dt.utcoffset()
        if offset is not None:
            total_seconds = int(offset.total_seconds())
            sign = "+" if total_seconds >= 0 else "-"
            total_seconds = abs(total_seconds)
            offset_hours = total_seconds // 3600
            offset_minutes = (total_seconds % 3600) // 60
            tz_str = f"{sign}{offset_hours:02d}{offset_minutes:02d}"
        else:
            tz_str = "+0000"
    else:
        tz_str = "+0000"
    return f"{day_abbr}, {dt.day} {month_abbr} {dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} {tz_str}"


# Build the format character dispatch table.
# Each function receives a datetime-like object and returns a string.
# We handle date-only and time-only values by converting them to datetime
# before dispatching.

_FORMAT_CHARS: dict[str, Callable[[datetime], str]] = {
    # Day
    "d": lambda dt: f"{dt.day:02d}",
    "j": lambda dt: str(dt.day),
    "D": lambda dt: _DAY_ABBREVS[dt.weekday()],
    "l": lambda dt: _DAY_NAMES[dt.weekday()],
    "S": lambda dt: _ordinal_suffix(dt.day),
    "w": lambda dt: str(
        (dt.weekday() + 1) % 7
    ),  # Django: 0=Sunday, Python weekday(): 0=Monday
    "z": lambda dt: str(dt.timetuple().tm_yday),
    # Month
    "m": lambda dt: f"{dt.month:02d}",
    "n": lambda dt: str(dt.month),
    "M": lambda dt: _MONTH_ABBREVS[dt.month - 1],
    "N": lambda dt: _MONTH_AP[dt.month - 1],
    "F": lambda dt: _MONTH_NAMES[dt.month - 1],
    "t": lambda dt: str(calendar.monthrange(dt.year, dt.month)[1]),
    # Year
    "y": lambda dt: f"{dt.year % 100:02d}",
    "Y": lambda dt: str(dt.year),
    "L": lambda dt: "1" if calendar.isleap(dt.year) else "0",
    # Hour
    "G": lambda dt: str(dt.hour),
    "H": lambda dt: f"{dt.hour:02d}",
    "g": lambda dt: str(dt.hour % 12 or 12),
    "h": lambda dt: f"{(dt.hour % 12 or 12):02d}",
    # Minute / Second / Microsecond
    "i": lambda dt: f"{dt.minute:02d}",
    "s": lambda dt: f"{dt.second:02d}",
    "u": lambda dt: f"{dt.microsecond:06d}",
    # AM/PM
    "A": lambda dt: "AM" if dt.hour < 12 else "PM",
    "a": lambda dt: "a.m." if dt.hour < 12 else "p.m.",
    "P": lambda dt: _ap_time(dt),
    # Timezone
    "e": lambda dt: str(dt.tzinfo) if dt.tzinfo is not None else "",
    "O": lambda dt: dt.strftime("%z") if dt.tzinfo is not None else "",
    "T": lambda dt: dt.strftime("%Z") if dt.tzinfo is not None else "",
    "Z": lambda dt: (
        str(int(dt.utcoffset().total_seconds()))
        if dt.tzinfo is not None and dt.utcoffset() is not None
        else ""
    ),
    # Full formats
    "U": lambda dt: (
        str(int(dt.timestamp()))
        if dt.tzinfo is not None
        else str(int(dt.replace(tzinfo=UTC).timestamp()))
    ),
    "c": _iso8601,
    "r": _rfc2822,
    # Week
    "W": lambda dt: f"{dt.isocalendar()[1]:02d}",
}

# Characters that are valid format specifiers (for fast membership check)
_FORMAT_CHAR_SET = frozenset(_FORMAT_CHARS)


def _apply_format(dt: datetime, format_string: str) -> str:
    """Apply Django-style format string to a datetime.

    Iterates character by character. Backslash escapes the next character
    as a literal. Known format characters are replaced; unknown characters
    pass through as literals.
    """
    result: list[str] = []
    i = 0
    length = len(format_string)
    while i < length:
        ch = format_string[i]
        if ch == "\\":
            # Backslash: next char is literal
            i += 1
            if i < length:
                result.append(format_string[i])
            i += 1
            continue
        formatter = _FORMAT_CHARS.get(ch)
        if formatter is not None:
            result.append(formatter(dt))
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _ensure_datetime(value: date | datetime | time) -> datetime:
    """Convert date or time to datetime for format character dispatch."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    # time object — use epoch date as placeholder
    return datetime(
        1970,
        1,
        1,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=value.tzinfo,
    )


# ── Date/Time Formatting ────────────────────────────────────────────────────


def format_date(value: date | datetime, format_string: str | None = None) -> str:
    """Format a date using the DATE_FORMAT setting (or custom format).

    Args:
        value: A date or datetime to format.
        format_string: Django-style format string. If None, uses DATE_FORMAT setting.

    Returns:
        Formatted date string.
    """
    if format_string is None:
        format_string = get_setting("DATE_FORMAT")  # type: ignore[assignment]
    return _apply_format(_ensure_datetime(value), format_string)


def format_datetime(value: datetime, format_string: str | None = None) -> str:
    """Format a datetime using the DATETIME_FORMAT setting (or custom format).

    Args:
        value: A datetime to format.
        format_string: Django-style format string. If None, uses DATETIME_FORMAT setting.

    Returns:
        Formatted datetime string.
    """
    if format_string is None:
        format_string = get_setting("DATETIME_FORMAT")  # type: ignore[assignment]
    return _apply_format(value, format_string)


def format_time(value: time | datetime, format_string: str | None = None) -> str:
    """Format a time using the TIME_FORMAT setting (or custom format).

    Args:
        value: A time or datetime to format.
        format_string: Django-style format string. If None, uses TIME_FORMAT setting.

    Returns:
        Formatted time string.
    """
    if format_string is None:
        format_string = get_setting("TIME_FORMAT")  # type: ignore[assignment]
    return _apply_format(_ensure_datetime(value), format_string)


def format_short_date(value: date | datetime) -> str:
    """Format using SHORT_DATE_FORMAT setting.

    Args:
        value: A date or datetime to format.

    Returns:
        Short formatted date string.
    """
    format_string: str = get_setting("SHORT_DATE_FORMAT")  # type: ignore[assignment]
    return _apply_format(_ensure_datetime(value), format_string)


def format_short_datetime(value: datetime) -> str:
    """Format using SHORT_DATETIME_FORMAT setting.

    Args:
        value: A datetime to format.

    Returns:
        Short formatted datetime string.
    """
    format_string: str = get_setting("SHORT_DATETIME_FORMAT")  # type: ignore[assignment]
    return _apply_format(value, format_string)


# ── Number Formatting ────────────────────────────────────────────────────────


def _split_number_groups(integer_part: str, grouping: int) -> list[str]:
    """Split an integer string into groups for thousand separator insertion.

    Args:
        integer_part: The integer digits (no sign).
        grouping: Number of digits per group.

    Returns:
        List of digit groups from left to right.
    """
    if grouping <= 0 or len(integer_part) <= grouping:
        return [integer_part]
    groups: list[str] = []
    while len(integer_part) > grouping:
        groups.append(integer_part[-grouping:])
        integer_part = integer_part[:-grouping]
    if integer_part:
        groups.append(integer_part)
    groups.reverse()
    return groups


def format_number(
    value: int | float | str | _Decimal, decimal_places: int | None = None
) -> str:
    """Format a number using DECIMAL_SEPARATOR, THOUSAND_SEPARATOR, NUMBER_GROUPING settings.

    Args:
        value: The number to format (int, float, or numeric string).
        decimal_places: Fixed number of decimal places. None preserves the original.

    Returns:
        Locale-formatted number string.
    """
    # Handle non-finite floats early
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "-Infinity" if value < 0 else "Infinity"

    decimal_sep: str = get_setting("DECIMAL_SEPARATOR")  # type: ignore[assignment]
    thousand_sep: str = get_setting("THOUSAND_SEPARATOR")  # type: ignore[assignment]
    use_thousand_sep: bool = get_setting("USE_THOUSAND_SEPARATOR")  # type: ignore[assignment]
    grouping: int = get_setting("NUMBER_GROUPING")  # type: ignore[assignment]

    # Normalize to string
    if isinstance(value, _Decimal):
        # Decimal: use Decimal arithmetic for precision
        if decimal_places is not None:
            quantize_exp = _Decimal(10) ** -decimal_places
            value = value.quantize(quantize_exp, rounding=ROUND_HALF_UP)
        str_value = str(value)
    elif isinstance(value, str):
        # Parse string number; if decimal_places is given, round via Decimal
        str_value = value.strip()
        if decimal_places is not None:
            d = _Decimal(str_value)
            quantize_exp = _Decimal(10) ** -decimal_places
            d = d.quantize(quantize_exp, rounding=ROUND_HALF_UP)
            str_value = str(d)
    elif isinstance(value, int):
        str_value = str(value)
    else:
        if decimal_places is not None:
            str_value = f"{value:.{decimal_places}f}"
        else:
            str_value = str(value)

    # Scientific notation (very large / very small floats) can't be digit-
    # grouped meaningfully — str(1e20) == "1e+20", and applying thousand
    # separators to that yields garbage like "1e,+20". Return it verbatim.
    if "e" in str_value or "E" in str_value:
        return str_value

    # Handle negative sign
    negative = str_value.startswith("-")
    if negative:
        str_value = str_value[1:]

    # Split integer and decimal parts
    if "." in str_value:
        int_part, dec_part = str_value.split(".", 1)
    else:
        int_part = str_value
        dec_part = ""

    # Apply fixed decimal places if requested
    if decimal_places is not None:
        if decimal_places == 0:
            dec_part = ""
        elif len(dec_part) < decimal_places:
            dec_part = dec_part.ljust(decimal_places, "0")
        elif len(dec_part) > decimal_places:
            dec_part = dec_part[:decimal_places]

    # Apply thousand separator
    if use_thousand_sep and grouping > 0:
        groups = _split_number_groups(int_part, grouping)
        int_part = thousand_sep.join(groups)

    # Assemble result
    result = f"{int_part}{decimal_sep}{dec_part}" if dec_part else int_part

    if negative:
        result = f"-{result}"

    return result


def format_currency(
    value: int | float, currency_symbol: str = "$", decimal_places: int = 2
) -> str:
    """Format as currency with locale-aware separators.

    Args:
        value: The monetary value.
        currency_symbol: Currency symbol to prepend.
        decimal_places: Number of decimal places (default 2).

    Returns:
        Currency-formatted string like "$1,234.56".
    """
    # Force thousand separator on for currency regardless of setting
    decimal_sep: str = get_setting("DECIMAL_SEPARATOR")  # type: ignore[assignment]
    thousand_sep: str = get_setting("THOUSAND_SEPARATOR")  # type: ignore[assignment]
    grouping: int = get_setting("NUMBER_GROUPING")  # type: ignore[assignment]

    # Treat -0.0 as 0.0
    if value == 0:
        value = 0
    negative = value < 0
    abs_value = abs(value)
    str_value = f"{abs_value:.{decimal_places}f}"

    if "." in str_value:
        int_part, dec_part = str_value.split(".", 1)
    else:
        int_part = str_value
        dec_part = "0" * decimal_places

    # Always apply thousand separator for currency
    if grouping > 0:
        groups = _split_number_groups(int_part, grouping)
        int_part = thousand_sep.join(groups)

    # Normalize negative zero: a genuinely negative value that rounds to a zero
    # magnitude (e.g. -0.001 at 2 decimals) must render "$0.00", never "-$0.00".
    # (Exact -0.0 is already folded to 0 above; this covers the rounded case.)
    if negative and int_part.strip("0") == "" and dec_part.strip("0") == "":
        negative = False

    formatted = f"{int_part}{decimal_sep}{dec_part}" if decimal_places > 0 else int_part

    if negative:
        return f"-{currency_symbol}{formatted}"
    return f"{currency_symbol}{formatted}"


def format_percent(value: float, decimal_places: int = 1) -> str:
    """Format as percentage with locale-aware decimal separator.

    Args:
        value: The value as a fraction (0.5 = 50%, 1.0 = 100%).
        decimal_places: Number of decimal places.

    Returns:
        Percentage string like "50.0%".
    """
    decimal_sep: str = get_setting("DECIMAL_SEPARATOR")  # type: ignore[assignment]
    pct = value * 100
    formatted = f"{pct:.{decimal_places}f}"
    if decimal_sep != ".":
        formatted = formatted.replace(".", decimal_sep)
    return f"{formatted}%"


# ── Input Parsing ────────────────────────────────────────────────────────────


def parse_date(value: str) -> date | None:
    """Parse a date string trying DATE_INPUT_FORMATS in order.

    Args:
        value: Date string to parse.

    Returns:
        Parsed date or None if no format matches.
    """
    formats: list[str] = get_setting("DATE_INPUT_FORMATS")  # type: ignore[assignment]
    stripped = value.strip()
    for fmt in formats:
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(value: str) -> datetime | None:
    """Parse a datetime string trying DATETIME_INPUT_FORMATS in order.

    Args:
        value: Datetime string to parse.

    Returns:
        Parsed datetime or None if no format matches.
    """
    formats: list[str] = get_setting("DATETIME_INPUT_FORMATS")  # type: ignore[assignment]
    stripped = value.strip()
    for fmt in formats:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return None


# ── Timezone Utilities ───────────────────────────────────────────────────────


def now() -> datetime:
    """Return current datetime, timezone-aware if USE_TZ is True.

    Returns:
        Current datetime. If USE_TZ is True, includes UTC timezone info.
    """
    use_tz: bool = get_setting("USE_TZ")  # type: ignore[assignment]
    if use_tz:
        return datetime.now(tz=UTC)
    return datetime.now()


def localtime(value: datetime, timezone_name: str | None = None) -> datetime:
    """Convert a datetime to the TIME_ZONE setting (or custom timezone).

    Args:
        value: The datetime to convert. If naive, it is assumed to be UTC.
        timezone_name: Target timezone name. None uses TIME_ZONE setting.

    Returns:
        Datetime converted to the target timezone.
    """
    tz = _get_default_tz() if timezone_name is None else _get_tz(timezone_name)
    if value.tzinfo is None:
        # Assume naive datetimes are UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def make_aware(value: datetime, timezone_name: str | None = None) -> datetime:
    """Make a naive datetime timezone-aware.

    Args:
        value: A naive datetime.
        timezone_name: Timezone to attach. None uses TIME_ZONE setting.

    Returns:
        Timezone-aware datetime.

    Raises:
        ValueError: If the datetime is already timezone-aware.
    """
    if value.tzinfo is not None:
        raise ValueError(
            f"make_aware expects a naive datetime, got tzinfo={value.tzinfo}"
        )
    tz = _get_default_tz() if timezone_name is None else _get_tz(timezone_name)
    return value.replace(tzinfo=tz)


def make_naive(value: datetime, timezone_name: str | None = None) -> datetime:
    """Strip timezone info, converting to TIME_ZONE first if needed.

    Args:
        value: A datetime (aware or naive).
        timezone_name: Target timezone for conversion before stripping. None uses TIME_ZONE setting.

    Returns:
        Naive datetime in the target timezone.
    """
    if value.tzinfo is None:
        return value
    tz = _get_default_tz() if timezone_name is None else _get_tz(timezone_name)
    converted = value.astimezone(tz)
    return converted.replace(tzinfo=None)


def is_aware(value: datetime) -> bool:
    """Return True if datetime has timezone info.

    Args:
        value: Datetime to check.

    Returns:
        True if timezone-aware.
    """
    return value.tzinfo is not None and value.utcoffset() is not None


def is_naive(value: datetime) -> bool:
    """Return True if datetime has no timezone info.

    Args:
        value: Datetime to check.

    Returns:
        True if timezone-naive.
    """
    return value.tzinfo is None or value.utcoffset() is None


# ── Calendar Utilities ───────────────────────────────────────────────────────


def get_first_day_of_week() -> int:
    """Return FIRST_DAY_OF_WEEK setting.

    Returns:
        0 for Sunday, 1 for Monday, etc.
    """
    return get_setting("FIRST_DAY_OF_WEEK")  # type: ignore[return-value]


def get_week_start(dt: date) -> date:
    """Return the start of the week for the given date, respecting FIRST_DAY_OF_WEEK.

    FIRST_DAY_OF_WEEK uses 0=Sunday, 1=Monday convention.
    Python's weekday() uses 0=Monday, 6=Sunday.

    Args:
        dt: The date to find the week start for.

    Returns:
        The date of the first day of the week containing dt.
    """
    first_day: int = get_setting("FIRST_DAY_OF_WEEK")  # type: ignore[assignment]
    # Convert setting convention (0=Sun) to Python convention (0=Mon)
    # Setting 0 (Sunday) = Python 6
    # Setting 1 (Monday) = Python 0
    # Setting 2 (Tuesday) = Python 1
    # etc.
    python_first = 6 if first_day == 0 else first_day - 1

    current_weekday = dt.weekday()  # 0=Mon, 6=Sun
    days_back = (current_weekday - python_first) % 7
    return dt - timedelta(days=days_back)


# ── Template Filter Registration ────────────────────────────────────────────

register = Library("formats")


@register.filter("date")
def _filter_date(value: date | datetime | None, arg: str | None = None) -> str:
    """Template filter: {{ value|date }} or {{ value|date:"m/d/Y" }}."""
    if value is None:
        return ""
    if not isinstance(value, (date, datetime)):
        return str(value)
    return format_date(value, arg)


@register.filter("time")
def _filter_time(value: time | datetime | None, arg: str | None = None) -> str:
    """Template filter: {{ value|time }} or {{ value|time:"H:i" }}."""
    if value is None:
        return ""
    if not isinstance(value, (time, datetime)):
        return str(value)
    return format_time(value, arg)


@register.filter("datetime")
def _filter_datetime(value: datetime | None, arg: str | None = None) -> str:
    """Template filter: {{ value|datetime }} or {{ value|datetime:"Y-m-d H:i" }}."""
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return str(value)
    return format_datetime(value, arg)


@register.filter("number")
def _filter_number(value: int | float | str | None, arg: str | None = None) -> str:
    """Template filter: {{ value|number }} or {{ value|number:"2" }}."""
    if value is None:
        return ""
    decimal_places: int | None = None
    if arg is not None:
        try:
            decimal_places = int(arg)
        except ValueError, TypeError:
            decimal_places = None
    return format_number(value, decimal_places)


@register.filter("currency")
def _filter_currency(value: int | float | None, arg: str | None = None) -> str:
    """Template filter: {{ value|currency }} or {{ value|currency:"€" }}."""
    if value is None:
        return ""
    symbol = arg if arg is not None else "$"
    return format_currency(value, currency_symbol=symbol)


@register.filter("short_date")
def _filter_short_date(value: date | datetime | None) -> str:
    """Template filter: {{ value|short_date }}."""
    if value is None:
        return ""
    return format_short_date(value)
