"""Tests for native JSON datetime / date / time serialization.

datetime values serialize as ISO 8601 with the 'T' separator; a zero
microsecond fraction is omitted, and a non-zero fraction is preserved at full
precision. Timezone offsets are kept.

NOTE: exercises the native fast path, so requires the rebuilt extension
(`uv run hyper-build`).

Usage:
    uv run hyper-test datetime_json
"""

# hyper-test: unit

import datetime
import json

from hyperdjango.native import fast_json_dumps

_PASS = 0


def _check(obj, expected):
    global _PASS
    got = json.loads(fast_json_dumps(obj))
    assert got == expected, f"expected {expected!r}, got {got!r}"
    _PASS += 1
    print(f"  ✓ {expected!r}")


# datetime with zero microseconds → fractional part omitted, 'T' separator
_check({"t": datetime.datetime(2000, 1, 1, 12, 30, 0)}, {"t": "2000-01-01T12:30:00"})

# datetime with microseconds → preserved at full precision, 'T' separator
_check(
    {"t": datetime.datetime(2000, 1, 1, 12, 30, 0, 123456)},
    {"t": "2000-01-01T12:30:00.123456"},
)

# date
_check({"d": datetime.date(2024, 3, 15)}, {"d": "2024-03-15"})

# time with and without microseconds
_check({"t": datetime.time(9, 5, 0)}, {"t": "09:05:00"})
_check({"t": datetime.time(9, 5, 0, 1000)}, {"t": "09:05:00.001000"})

# timezone-aware datetime keeps its offset
_check(
    {"t": datetime.datetime(2000, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)},
    {"t": "2000-01-01T00:00:00+00:00"},
)

# a bare datetime (top-level, not nested) round-trips
_check(datetime.datetime(2000, 1, 1, 12, 30, 0), "2000-01-01T12:30:00")


print(f"\n✅ datetime JSON serialization: {_PASS} checks passed")
