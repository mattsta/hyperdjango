"""Tests for SIMD-accelerated template filters.

Tests native Zig implementations of striptags, truncate, urlencode,
wordcount, and wordwrap filters — all with SIMD fast paths.

Usage:
    uv run hyper-test simd_filters
"""

# hyper-test: unit

import os
import sys
import time

from hyperdjango.templating import TemplateEngine

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("SIMD Template Filter Tests")
    print("=" * 60)

    engine = TemplateEngine(template_dir=".")

    def render(src, ctx=None):
        return engine.render_string(src, ctx or {})

    # ── striptags ─────────────────────────────────────────────────

    print("\n--- striptags ---")

    # Test 1: Basic tag stripping
    result = render("{{ text|striptags }}", {"text": "<p>Hello <b>World</b></p>"})
    check("striptags basic", result == "Hello World", repr(result))

    # Test 2: No tags
    result = render("{{ text|striptags }}", {"text": "No tags here"})
    check("striptags no tags", result == "No tags here", repr(result))

    # Test 3: Only tags
    result = render("{{ text|striptags }}", {"text": "<br><hr><img>"})
    check("striptags only tags", result == "", repr(result))

    # Test 4: Nested tags
    result = render(
        "{{ text|striptags }}", {"text": "<div><p><span>Deep</span></p></div>"}
    )
    check("striptags nested", result == "Deep", repr(result))

    # Test 5: Mixed content
    result = render("{{ text|striptags }}", {"text": "A<b>B</b>C<i>D</i>E"})
    check("striptags mixed", result == "ABCDE", repr(result))

    # Test 6: Long text (triggers SIMD path)
    long_text = "Hello World! " * 100  # 1300 chars, no tags
    result = render("{{ text|striptags }}", {"text": long_text})
    check(
        "striptags long no tags",
        result == long_text,
        f"len={len(result)} expected={len(long_text)}",
    )

    # Test 7: Long text with tags
    tagged = "<p>" + "word " * 100 + "</p>"
    result = render("{{ text|striptags }}", {"text": tagged})
    check("striptags long with tags", "<" not in result and ">" not in result)

    # Test 8: Self-closing tags
    result = render("{{ text|striptags }}", {"text": "Hello<br/>World"})
    check("striptags self-closing", result == "HelloWorld", repr(result))

    # Test 9: Attributes in tags
    result = render(
        "{{ text|striptags }}", {"text": '<a href="url" class="link">Click</a>'}
    )
    check("striptags with attrs", result == "Click", repr(result))

    # ── truncate ──────────────────────────────────────────────────

    print("\n--- truncate ---")

    # Test 10: Basic truncate
    result = render(
        "{{ text|truncate(20) }}",
        {"text": "This is a long string that should be truncated"},
    )
    check("truncate basic", len(result) <= 25 and result.endswith("..."), repr(result))

    # Test 11: Short text (no truncation)
    result = render("{{ text|truncate(255) }}", {"text": "Short"})
    check("truncate short text", result == "Short", repr(result))

    # Test 12: Exact boundary
    text = "Hello World"
    result = render("{{ text|truncate(11) }}", {"text": text})
    check("truncate at exact length", result == "Hello World", repr(result))

    # Test 13: Default length (255)
    result = render("{{ text|truncate }}", {"text": "A" * 100})
    check("truncate default length no cut", result == "A" * 100, repr(result[:50]))

    # ── urlencode ─────────────────────────────────────────────────

    print("\n--- urlencode ---")

    # Test 14: Basic URL encoding
    result = render("{{ text|urlencode }}", {"text": "hello world"})
    check("urlencode space", result == "hello%20world", repr(result))

    # Test 15: Special characters
    result = render("{{ text|urlencode }}", {"text": "a=b&c=d"})
    check("urlencode special", result == "a%3Db%26c%3Dd", repr(result))

    # Test 16: Already safe characters
    result = render("{{ text|urlencode }}", {"text": "hello-world_test.html"})
    check("urlencode safe chars", result == "hello-world_test.html", repr(result))

    # Test 17: Unicode
    result = render("{{ text|urlencode }}", {"text": "café"})
    check("urlencode unicode", "%" in result and "caf" in result, repr(result))

    # Test 18: Empty string
    result = render("{{ text|urlencode }}", {"text": ""})
    check("urlencode empty", result == "", repr(result))

    # Test 19: Slash
    result = render("{{ text|urlencode }}", {"text": "path/to/file"})
    check("urlencode slash", result == "path%2Fto%2Ffile", repr(result))

    # ── wordcount ─────────────────────────────────────────────────

    print("\n--- wordcount ---")

    # Test 20: Basic word count
    result = render("{{ text|wordcount }}", {"text": "Hello World"})
    check("wordcount basic", result == "2", repr(result))

    # Test 21: Multiple spaces
    result = render("{{ text|wordcount }}", {"text": "one   two   three"})
    check("wordcount multi space", result == "3", repr(result))

    # Test 22: Tabs and newlines
    result = render("{{ text|wordcount }}", {"text": "one\ttwo\nthree"})
    check("wordcount tabs newlines", result == "3", repr(result))

    # Test 23: Empty string
    result = render("{{ text|wordcount }}", {"text": ""})
    check("wordcount empty", result == "0", repr(result))

    # Test 24: Single word
    result = render("{{ text|wordcount }}", {"text": "hello"})
    check("wordcount single", result == "1", repr(result))

    # Test 25: Long text (triggers SIMD path)
    long_words = " ".join(f"word{i}" for i in range(200))
    result = render("{{ text|wordcount }}", {"text": long_words})
    check("wordcount long text", result == "200", repr(result))

    # Test 26: Only whitespace
    result = render("{{ text|wordcount }}", {"text": "   \t\n   "})
    check("wordcount only whitespace", result == "0", repr(result))

    # ── wordwrap ──────────────────────────────────────────────────

    print("\n--- wordwrap ---")

    # Test 27: Basic word wrap
    text = "This is a test of word wrapping at a specific width"
    result = render("{{ text|wordwrap(20) }}", {"text": text})
    lines = result.split("\n")
    check("wordwrap produces multiple lines", len(lines) > 1, repr(result))
    check(
        "wordwrap lines within width",
        all(len(line) <= 25 for line in lines),
        repr(lines),
    )

    # Test 28: Short text (no wrap)
    result = render("{{ text|wordwrap(79) }}", {"text": "Short"})
    check("wordwrap short text", result == "Short", repr(result))

    # Test 29: Default width (79)
    text79 = "A " * 50  # 100 chars with spaces
    result = render("{{ text|wordwrap }}", {"text": text79})
    check("wordwrap default width", "\n" in result)

    # ── Performance Benchmark ─────────────────────────────────────

    print("\n--- Performance ---")

    # Benchmark striptags on large text
    large_html = "<div class='container'><p>" + "Hello World! " * 500 + "</p></div>"
    iterations = 1000

    start = time.perf_counter()
    for _ in range(iterations):
        render("{{ text|striptags }}", {"text": large_html})
    elapsed = time.perf_counter() - start
    per_call_us = (elapsed / iterations) * 1_000_000
    print(
        f"    striptags ({len(large_html)} chars): {per_call_us:.1f} μs/call ({iterations / elapsed:.0f}/sec)"
    )
    # Under parallel execution (240+ processes), CPU contention inflates timing 5-8x.
    # Proven: standalone=~100μs, parallel=~2800μs under 241-file suite load.
    _perf_mult = 20.0 if os.environ.get("HYPER_TEST_PARALLEL") == "1" else 1.0
    check(
        "striptags perf < 500μs", per_call_us < 500 * _perf_mult, f"{per_call_us:.1f}μs"
    )

    # Benchmark wordcount on large text
    large_text = " ".join(f"word{i}" for i in range(500))
    start = time.perf_counter()
    for _ in range(iterations):
        render("{{ text|wordcount }}", {"text": large_text})
    elapsed = time.perf_counter() - start
    per_call_us = (elapsed / iterations) * 1_000_000
    print(
        f"    wordcount ({len(large_text)} chars): {per_call_us:.1f} μs/call ({iterations / elapsed:.0f}/sec)"
    )
    check(
        "wordcount perf < 200μs", per_call_us < 200 * _perf_mult, f"{per_call_us:.1f}μs"
    )

    # Benchmark urlencode
    url_text = "path/to/resource?query=hello world&page=1" * 10
    start = time.perf_counter()
    for _ in range(iterations):
        render("{{ text|urlencode }}", {"text": url_text})
    elapsed = time.perf_counter() - start
    per_call_us = (elapsed / iterations) * 1_000_000
    print(
        f"    urlencode ({len(url_text)} chars): {per_call_us:.1f} μs/call ({iterations / elapsed:.0f}/sec)"
    )
    check(
        "urlencode perf < 200μs", per_call_us < 200 * _perf_mult, f"{per_call_us:.1f}μs"
    )

    # ── Summary ──────────────────────────────────────────────────

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
