"""HyperDjango humanize module — human-friendly display of numbers, dates, and file sizes.

Replaces django.contrib.humanize. All functions work standalone or as template filters
via the HUMANIZE_FILTERS registry dict and the "humanize" template Library.

Usage:
    engine = TemplateEngine("templates")
    engine.load_library("humanize")
    html = engine.render_string("{{ 1000000|intcomma }}", {})
    # => "1,000,000"
"""

import re
import time as _time_mod
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from hyperdjango.templating import Library

# Constants
_TEEN_ORDINALS = frozenset({11, 12, 13})


@dataclass(slots=True)
class _CacheInfo:
    """Minimal cache_info shape — mirrors functools.lru_cache."""

    hits: int
    misses: int
    maxsize: int
    currsize: int


def time_bucket_cached(
    *,
    bucket_seconds: int = 30,
    maxsize: int = 512,
) -> Callable[[Callable], Callable]:
    """Memoize a pure-of-time function within a coarse time bucket.

    For functions whose output depends on both their input and "now"
    (e.g. ``naturaltime(dt)`` rendering "5 minutes ago"), this decorator
    adds an LRU cache keyed on ``(value, int(monotonic() / bucket_seconds))``.
    Within a single ``bucket_seconds`` window the same value always
    returns the same result — correct for coarse human-readable time.

    Cache entries for stale buckets are naturally evicted by LRU churn as
    new buckets advance the key space.

    **Why not functools.lru_cache?** Under free-threaded Python 3.14t,
    ``lru_cache`` acquires an internal lock on every call to maintain
    thread-safe doubly-linked list bookkeeping. On the hot path that's
    ~400 ns per call — comparable to the work we were trying to skip.
    The inline ``OrderedDict`` path uses Python dict atomic operations
    (``.get`` / ``.__setitem__`` / ``.popitem``) which are individually
    thread-safe. Races in cache state are benign (at worst two threads
    recompute the same value). Per-call overhead: ~150-200 ns.

    Why: ``time_ago`` / ``naturaltime`` gets called dozens of times per
    request rendering any list of timestamps, with heavy input repetition
    (the same 10 items in a feed, the same user's recent posts). Profile
    evidence on hypernews ``/user/alice`` showed 570K calls per 15K
    requests = 38 calls per request.

    Args:
        bucket_seconds: Granularity of the "now" bucket. Default 30s.
            Displayed time strings may be up to ``bucket_seconds`` stale.
        maxsize: Maximum cached entries. Default 512.

    Example:
        @time_bucket_cached(bucket_seconds=30)
        def naturaltime(value):
            ...
    """

    def decorator(func: Callable) -> Callable:
        cache: OrderedDict = OrderedDict()
        stats = [0, 0]  # [hits, misses]

        def wrapper(value):
            # Single-argument fast path — avoids tuple construction cost.
            # Multi-arg callers can use functools.lru_cache directly.
            bucket = int(_time_mod.monotonic() / bucket_seconds)
            key = (value, bucket)
            try:
                cached = cache.get(key)
            except TypeError:
                # Unhashable input (e.g. a list) can't be a cache key — skip
                # the cache and compute directly instead of raising.
                return func(value)
            if cached is not None:
                stats[0] += 1
                return cached
            stats[1] += 1
            result = func(value)
            cache[key] = result
            if len(cache) > maxsize:
                # O(1) FIFO eviction — never materialize full keys list
                cache.popitem(last=False)
            return result

        def cache_info() -> _CacheInfo:
            return _CacheInfo(
                hits=stats[0],
                misses=stats[1],
                maxsize=maxsize,
                currsize=len(cache),
            )

        def cache_clear() -> None:
            cache.clear()
            stats[0] = 0
            stats[1] = 0

        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        return wrapper

    return decorator


def ordinal(value: int) -> str:
    """Convert an integer to its ordinal string. 1->'1st', 2->'2nd', 11->'11th', etc."""
    try:
        value = int(value)
    except TypeError, ValueError:
        return str(value)
    if value < 0:
        return str(value)
    if value % 100 in _TEEN_ORDINALS:
        suffix = "th"
    else:
        last_digit = value % 10
        if last_digit == 1:
            suffix = "st"
        elif last_digit == 2:
            suffix = "nd"
        elif last_digit == 3:
            suffix = "rd"
        else:
            suffix = "th"
    return f"{value}{suffix}"


def intcomma(value: int | float | str) -> str:
    """Add commas every three digits. 1000->'1,000', 1234567.89->'1,234,567.89'."""
    result = str(value)
    match = re.match(r"-?\d+", result)
    if match:
        prefix = match[0]
        prefix_with_commas = re.sub(r"\d{3}", r"\g<0>,", prefix[::-1])[::-1]
        # Remove a leading comma if needed (e.g. ",123" -> "123" or "-,123" -> "-123").
        prefix_with_commas = re.sub(r"^(-?),", r"\1", prefix_with_commas)
        result = prefix_with_commas + result[len(prefix) :]
    return result


_INTWORD_SCALES: list[tuple[int, str]] = [
    (6, "million"),
    (9, "billion"),
    (12, "trillion"),
    (15, "quadrillion"),
]


def intword(value: int | float) -> str:
    """Convert large integers to friendly text. 1000000->'1.0 million'."""
    try:
        value = int(value)
    except TypeError, ValueError:
        return str(value)

    abs_value = abs(value)
    if abs_value < 1_000_000:
        return str(value)

    for exponent, word in _INTWORD_SCALES:
        large_number = 10**exponent
        if abs_value < large_number * 1000:
            new_value = value / large_number
            # Format with 1 decimal place, strip trailing .0
            formatted = f"{new_value:.1f}"
            formatted = formatted.removesuffix(".0")
            return f"{formatted} {word}"

    return str(value)


@time_bucket_cached(bucket_seconds=30)
def naturaltime(value: datetime) -> str:
    """Show relative time from now. Past: '2 hours ago', future: 'in 2 hours'.

    Results are cached within a 30-second bucket so repeated calls with
    the same timestamp (common when rendering a list of items) skip the
    formatting work. Staleness is bounded at ``bucket_seconds``.
    """
    if not isinstance(value, datetime):
        return str(value)

    now = datetime.now(value.tzinfo)
    if value <= now:
        delta = now - value
        total_seconds = int(delta.total_seconds())
        if total_seconds < 10:
            return "just now"
        if total_seconds < 60:
            return f"{total_seconds} seconds ago"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = delta.days
        if days < 7:
            return f"{days} day{'s' if days != 1 else ''} ago"
        weeks = days // 7
        if weeks < 5:
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        months = days // 30
        if months < 12:
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"
    else:
        delta = value - now
        total_seconds = int(delta.total_seconds())
        if total_seconds < 10:
            return "just now"
        if total_seconds < 60:
            return f"in {total_seconds} seconds"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"in {minutes} minute{'s' if minutes != 1 else ''}"
        hours = minutes // 60
        if hours < 24:
            return f"in {hours} hour{'s' if hours != 1 else ''}"
        days = delta.days
        if days < 7:
            return f"in {days} day{'s' if days != 1 else ''}"
        weeks = days // 7
        if weeks < 5:
            return f"in {weeks} week{'s' if weeks != 1 else ''}"
        months = days // 30
        if months < 12:
            return f"in {months} month{'s' if months != 1 else ''}"
        years = days // 365
        return f"in {years} year{'s' if years != 1 else ''}"


def naturalday(value: date) -> str:
    """Return 'today', 'yesterday', 'tomorrow', or a formatted date string."""
    if not isinstance(value, date):
        return str(value)
    # If it's a datetime, extract just the date part
    if isinstance(value, datetime):
        value = value.date()
    today = date.today()
    delta_days = (value - today).days
    if delta_days == 0:
        return "today"
    if delta_days == 1:
        return "tomorrow"
    if delta_days == -1:
        return "yesterday"
    return value.strftime("%b %d, %Y")


def naturaldate(value: date) -> str:
    """Alias for naturalday (Django compat)."""
    return naturalday(value)


_FILESIZE_UNITS: list[tuple[float, str]] = [
    (1024**5, "PB"),
    (1024**4, "TB"),
    (1024**3, "GB"),
    (1024**2, "MB"),
    (1024**1, "KB"),
]


def filesizeformat(value: int | float) -> str:
    """Format bytes as human readable file size. 1024->'1.0 KB'."""
    try:
        value = float(value)
    except TypeError, ValueError:
        return str(value)

    abs_value = abs(value)
    negative = value < 0

    for threshold, unit in _FILESIZE_UNITS:
        if abs_value >= threshold:
            formatted = f"{value / threshold:.1f} {unit}"
            return formatted

    # Bytes range
    int_value = int(value)
    if negative:
        return f"{int_value} bytes"
    if int_value == 1:
        return "1 byte"
    return f"{int_value} bytes"


_AP_NUMBERS: list[str] = [
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


def apnumber(value: int) -> str:
    """AP style: spell out 0-9, return digits for 10+."""
    try:
        value = int(value)
    except TypeError, ValueError:
        return str(value)
    if 0 <= value <= 9:
        return _AP_NUMBERS[value]
    return str(value)


HUMANIZE_FILTERS: dict[str, Callable[..., str]] = {
    "ordinal": ordinal,
    "intcomma": intcomma,
    "intword": intword,
    "naturaltime": naturaltime,
    "naturalday": naturalday,
    "filesizeformat": filesizeformat,
    "apnumber": apnumber,
}

# ── Template Library Registration ────────────────────────────────────────────
# Creates a "humanize" Library so engine.load_library("humanize") works.

register = Library("humanize")

for _filter_name, _filter_func in HUMANIZE_FILTERS.items():
    register.filters[_filter_name] = _filter_func
