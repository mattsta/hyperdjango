#!/usr/bin/env python3
"""Comprehensive tests for hyperdjango.humanize module.

Tests all humanize functions: ordinal, intcomma, intword, naturaltime,
naturalday, filesizeformat, apnumber, naturaldate, the HUMANIZE_FILTERS registry,
and template engine integration (render humanize filters via Zig engine).
"""

# hyper-test: unit

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperdjango.humanize import (
    HUMANIZE_FILTERS,
    apnumber,
    filesizeformat,
    intcomma,
    intword,
    naturaldate,
    naturalday,
    naturaltime,
    ordinal,
)
from hyperdjango.humanize import (
    register as humanize_library,
)
from hyperdjango.templating import Library, TemplateEngine, _library_registry

passed = 0
failed = 0


def check(label: str, actual: str, expected: str) -> None:
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}: expected {expected!r}, got {actual!r}")


# ── ordinal ──────────────────────────────────────────────────────────────────

print("ordinal:")
check("1", ordinal(1), "1st")
check("2", ordinal(2), "2nd")
check("3", ordinal(3), "3rd")
check("4", ordinal(4), "4th")
check("11", ordinal(11), "11th")
check("12", ordinal(12), "12th")
check("13", ordinal(13), "13th")
check("21", ordinal(21), "21st")
check("22", ordinal(22), "22nd")
check("23", ordinal(23), "23rd")
check("100", ordinal(100), "100th")
check("101", ordinal(101), "101st")
check("111", ordinal(111), "111th")
check("112", ordinal(112), "112th")
check("113", ordinal(113), "113th")
check("0", ordinal(0), "0th")
check("negative", ordinal(-5), "-5")
check("1000", ordinal(1000), "1000th")
check("1001", ordinal(1001), "1001st")

# ── intcomma ─────────────────────────────────────────────────────────────────

print("\nintcomma:")
check("100", intcomma(100), "100")
check("1000", intcomma(1000), "1,000")
check("10000", intcomma(10000), "10,000")
check("100000", intcomma(100000), "100,000")
check("1000000", intcomma(1000000), "1,000,000")
check("negative", intcomma(-1000), "-1,000")
check("negative large", intcomma(-1000000), "-1,000,000")
check("float", intcomma(1234567.89), "1,234,567.89")
check("string input", intcomma("10000"), "10,000")
check("0", intcomma(0), "0")
check("small", intcomma(999), "999")

# ── intword ──────────────────────────────────────────────────────────────────

print("\nintword:")
check("1M", intword(1000000), "1 million")
check("1.2M", intword(1200000), "1.2 million")
check("1B", intword(1000000000), "1 billion")
check("1.2B", intword(1200000000), "1.2 billion")
check("1.5T", intword(1500000000000), "1.5 trillion")
check("1.8Q", intword(1800000000000000), "1.8 quadrillion")
check("below million", intword(999999), "999999")
check("0", intword(0), "0")
check("negative million", intword(-1000000), "-1 million")
check("negative 1.5M", intword(-1500000), "-1.5 million")
check("500", intword(500), "500")

# ── naturaltime ──────────────────────────────────────────────────────────────

print("\nnaturaltime:")
now = datetime.now()

# Past
check("just now", naturaltime(now - timedelta(seconds=3)), "just now")
check("seconds ago", naturaltime(now - timedelta(seconds=30)), "30 seconds ago")
check("1 minute ago", naturaltime(now - timedelta(minutes=1)), "1 minute ago")
check("5 minutes ago", naturaltime(now - timedelta(minutes=5)), "5 minutes ago")
check("1 hour ago", naturaltime(now - timedelta(hours=1)), "1 hour ago")
check("3 hours ago", naturaltime(now - timedelta(hours=3)), "3 hours ago")
check("1 day ago", naturaltime(now - timedelta(days=1)), "1 day ago")
check("3 days ago", naturaltime(now - timedelta(days=3)), "3 days ago")
check("2 weeks ago", naturaltime(now - timedelta(weeks=2)), "2 weeks ago")
check("3 months ago", naturaltime(now - timedelta(days=90)), "3 months ago")
check("2 years ago", naturaltime(now - timedelta(days=730)), "2 years ago")
check("1 year ago", naturaltime(now - timedelta(days=365)), "1 year ago")

# Regression: unhashable input must not crash the time-bucket cache lookup.
check("unhashable list", naturaltime([1, 2, 3]), "[1, 2, 3]")
check("unhashable dict", naturaltime({"a": 1}), "{'a': 1}")
check("non-datetime str", naturaltime("not a date"), "not a date")


# Future — use fresh now() for each call to avoid clock drift between test setup and call
def future_check(label: str, delta: timedelta, expected: str) -> None:
    """Create a future datetime relative to *current* now, avoiding clock skew."""
    value = datetime.now() + delta
    check(label, naturaltime(value), expected)


future_check("future just now", timedelta(seconds=3), "just now")
future_check("future seconds", timedelta(seconds=30, milliseconds=500), "in 30 seconds")
future_check("future 1 min", timedelta(seconds=61), "in 1 minute")
future_check("future 5 min", timedelta(minutes=5, seconds=1), "in 5 minutes")
future_check("future 1 hour", timedelta(hours=1, seconds=1), "in 1 hour")
future_check("future 2 hours", timedelta(hours=2, seconds=1), "in 2 hours")
future_check("future 1 day", timedelta(days=1, seconds=1), "in 1 day")
future_check("future 3 days", timedelta(days=3, seconds=1), "in 3 days")
future_check("future 2 weeks", timedelta(weeks=2, seconds=1), "in 2 weeks")
future_check("future 3 months", timedelta(days=91), "in 3 months")
future_check("future 2 years", timedelta(days=731), "in 2 years")

# Non-datetime input
check("non-datetime", naturaltime("not a date"), "not a date")

# ── naturalday ───────────────────────────────────────────────────────────────

print("\nnaturalday:")
today = date.today()
check("today", naturalday(today), "today")
check("yesterday", naturalday(today - timedelta(days=1)), "yesterday")
check("tomorrow", naturalday(today + timedelta(days=1)), "tomorrow")
check("other date", naturalday(date(2020, 1, 15)), "Jan 15, 2020")
check("other date 2", naturalday(date(2025, 12, 25)), "Dec 25, 2025")
check("non-date", naturalday("not a date"), "not a date")

# datetime input (should extract date)
check("datetime today", naturalday(datetime.now()), "today")

# ── naturaldate (alias) ─────────────────────────────────────────────────────

print("\nnaturaldate:")
check("alias today", naturaldate(today), "today")
check("alias yesterday", naturaldate(today - timedelta(days=1)), "yesterday")
check("alias other", naturaldate(date(2020, 6, 1)), "Jun 01, 2020")

# ── filesizeformat ───────────────────────────────────────────────────────────

print("\nfilesizeformat:")
check("0 bytes", filesizeformat(0), "0 bytes")
check("1 byte", filesizeformat(1), "1 byte")
check("512 bytes", filesizeformat(512), "512 bytes")
check("1 KB", filesizeformat(1024), "1.0 KB")
check("1.5 KB", filesizeformat(1536), "1.5 KB")
check("1 MB", filesizeformat(1048576), "1.0 MB")
check("1 GB", filesizeformat(1073741824), "1.0 GB")
check("1 TB", filesizeformat(1099511627776), "1.0 TB")
check("1 PB", filesizeformat(1125899906842624), "1.0 PB")
check("negative", filesizeformat(-1024), "-1.0 KB")
check("float bytes", filesizeformat(100.0), "100 bytes")
check("2.5 MB", filesizeformat(2621440), "2.5 MB")

# ── apnumber ─────────────────────────────────────────────────────────────────

print("\napnumber:")
check("0", apnumber(0), "zero")
check("1", apnumber(1), "one")
check("2", apnumber(2), "two")
check("3", apnumber(3), "three")
check("4", apnumber(4), "four")
check("5", apnumber(5), "five")
check("6", apnumber(6), "six")
check("7", apnumber(7), "seven")
check("8", apnumber(8), "eight")
check("9", apnumber(9), "nine")
check("10", apnumber(10), "10")
check("100", apnumber(100), "100")
check("negative", apnumber(-1), "-1")

# ── HUMANIZE_FILTERS registry ────────────────────────────────────────────────

print("\nHUMANIZE_FILTERS:")
expected_names = {
    "ordinal",
    "intcomma",
    "intword",
    "naturaltime",
    "naturalday",
    "filesizeformat",
    "apnumber",
}
actual_names = set(HUMANIZE_FILTERS.keys())
check("has all keys", str(sorted(actual_names)), str(sorted(expected_names)))
check("ordinal callable", str(HUMANIZE_FILTERS["ordinal"](42)), "42nd")
check("intcomma callable", str(HUMANIZE_FILTERS["intcomma"](5000)), "5,000")
check("apnumber callable", str(HUMANIZE_FILTERS["apnumber"](3)), "three")
check(
    "all are callable", str(all(callable(f) for f in HUMANIZE_FILTERS.values())), "True"
)

# ── Humanize Template Library Registration ──────────────────────────────────

print("\nLibrary registration:")
check("library registered", str("humanize" in _library_registry), "True")
check("library is Library", str(isinstance(humanize_library, Library)), "True")
check("library name", humanize_library.name, "humanize")
check("has ordinal filter", str("ordinal" in humanize_library.filters), "True")
check("has intcomma filter", str("intcomma" in humanize_library.filters), "True")
check("has intword filter", str("intword" in humanize_library.filters), "True")
check("has naturaltime filter", str("naturaltime" in humanize_library.filters), "True")
check("has naturalday filter", str("naturalday" in humanize_library.filters), "True")
check(
    "has filesizeformat filter",
    str("filesizeformat" in humanize_library.filters),
    "True",
)
check("has apnumber filter", str("apnumber" in humanize_library.filters), "True")
check(
    "filter count matches",
    str(len(humanize_library.filters)),
    str(len(HUMANIZE_FILTERS)),
)

# ── Template Engine Integration ─────────────────────────────────────────────

print("\nTemplate engine integration:")
engine = TemplateEngine(template_dir="/tmp/hyper_humanize_test_templates")

# Create the template directory
Path("/tmp/hyper_humanize_test_templates").mkdir(parents=True, exist_ok=True)

engine.load_library("humanize")

# ordinal filter
result = engine.render_string("{{ 1|ordinal }}", {})
check("render ordinal 1", result, "1st")

result = engine.render_string("{{ 2|ordinal }}", {})
check("render ordinal 2", result, "2nd")

result = engine.render_string("{{ 3|ordinal }}", {})
check("render ordinal 3", result, "3rd")

result = engine.render_string("{{ 11|ordinal }}", {})
check("render ordinal 11", result, "11th")

result = engine.render_string("{{ 21|ordinal }}", {})
check("render ordinal 21", result, "21st")

result = engine.render_string("{{ 100|ordinal }}", {})
check("render ordinal 100", result, "100th")

# intcomma filter
result = engine.render_string("{{ 1000000|intcomma }}", {})
check("render intcomma 1M", result, "1,000,000")

result = engine.render_string("{{ 1000|intcomma }}", {})
check("render intcomma 1K", result, "1,000")

result = engine.render_string("{{ 100|intcomma }}", {})
check("render intcomma 100", result, "100")

result = engine.render_string("{{ val|intcomma }}", {"val": 1234567})
check("render intcomma from context", result, "1,234,567")

# intword filter
result = engine.render_string("{{ 1000000|intword }}", {})
check("render intword 1M", result, "1 million")

result = engine.render_string("{{ 1000000000|intword }}", {})
check("render intword 1B", result, "1 billion")

result = engine.render_string("{{ val|intword }}", {"val": 1200000})
check("render intword 1.2M", result, "1.2 million")

# filesizeformat filter
# NOTE: The Zig native engine has a built-in filesizeformat that uses Django casing
# ("kB"/"Bytes") and takes priority over the Python custom filter callback.
# The Python humanize.filesizeformat uses "KB"/"bytes" (tested in unit tests above).
# Integration tests here validate the native engine output.
result = engine.render_string("{{ bytes_value|filesizeformat }}", {"bytes_value": 1024})
check("render filesizeformat 1KB", result, "1.0 kB")

result = engine.render_string(
    "{{ bytes_value|filesizeformat }}", {"bytes_value": 1048576}
)
check("render filesizeformat 1MB", result, "1.0 MB")

result = engine.render_string("{{ 0|filesizeformat }}", {})
check("render filesizeformat 0", result, "0 Bytes")

result = engine.render_string("{{ 1|filesizeformat }}", {})
check("render filesizeformat 1", result, "1 Bytes")

# apnumber filter
result = engine.render_string("{{ 5|apnumber }}", {})
check("render apnumber 5", result, "five")

result = engine.render_string("{{ 0|apnumber }}", {})
check("render apnumber 0", result, "zero")

result = engine.render_string("{{ 9|apnumber }}", {})
check("render apnumber 9", result, "nine")

result = engine.render_string("{{ 10|apnumber }}", {})
check("render apnumber 10", result, "10")

# naturalday filter (with context variable)
result = engine.render_string("{{ d|naturalday }}", {"d": date.today()})
check("render naturalday today", result, "today")

result = engine.render_string(
    "{{ d|naturalday }}", {"d": date.today() - timedelta(days=1)}
)
check("render naturalday yesterday", result, "yesterday")

result = engine.render_string(
    "{{ d|naturalday }}", {"d": date.today() + timedelta(days=1)}
)
check("render naturalday tomorrow", result, "tomorrow")

# chained / combined in a sentence
result = engine.render_string("Order #{{ 1|ordinal }} — {{ 5000|intcomma }} items", {})
check("render combined filters", result, "Order #1st — 5,000 items")

# multiple filters in one template
result = engine.render_string(
    "{{ a|ordinal }}, {{ b|intcomma }}, {{ c|apnumber }}",
    {"a": 3, "b": 50000, "c": 7},
)
check("render multiple filters", result, "3rd, 50,000, seven")

# ── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
total = passed + failed
print(f"Results: {passed}/{total} passed, {failed} failed")
if failed:
    print("FAILURES DETECTED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
