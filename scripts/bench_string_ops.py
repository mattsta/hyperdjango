#!/usr/bin/env python3
"""
Benchmark: Native SIMD string ops vs Python stdlib.

Run: uv run python scripts/bench_string_ops.py
"""

import html as _html
import time
from urllib.parse import parse_qs, quote, unquote

from hyperdjango._hyperdjango_native import (
    html_escape_native,
    parse_query_string_native,
    url_decode_native,
    url_encode_native,
)

N = 50000


def bench(name, native_fn, stdlib_fn, input_val):
    # Warm up
    for _ in range(100):
        native_fn(input_val)
        stdlib_fn(input_val)

    start = time.perf_counter_ns()
    for _ in range(N):
        native_fn(input_val)
    native_ns = (time.perf_counter_ns() - start) / N

    start = time.perf_counter_ns()
    for _ in range(N):
        stdlib_fn(input_val)
    stdlib_ns = (time.perf_counter_ns() - start) / N

    ratio = stdlib_ns / native_ns if native_ns > 0 else 0
    print(f"  {name:<40} {native_ns:>7.0f} ns  {stdlib_ns:>7.0f} ns  {ratio:>5.2f}x")

    # Verify correctness
    native_result = native_fn(input_val)
    stdlib_result = stdlib_fn(input_val)
    if native_result != stdlib_result:
        print(f"    MISMATCH: native={native_result!r} vs stdlib={stdlib_result!r}")


print(f"String Operations Benchmark — {N} iterations")
print(f"{'':>43} {'native':>7}   {'stdlib':>7}   {'ratio':>5}")
print("=" * 70)

# html_escape
print("\nhtml_escape:")
bench(
    "no escapes (short)",
    html_escape_native,
    lambda s: _html.escape(s, quote=True),
    "hello world",
)
bench(
    "no escapes (64 chars)",
    html_escape_native,
    lambda s: _html.escape(s, quote=True),
    "a" * 64,
)
bench(
    "with <>&\"' chars",
    html_escape_native,
    lambda s: _html.escape(s, quote=True),
    '<script>alert("xss")</script>&foo=bar\'s',
)
bench(
    "mixed long string",
    html_escape_native,
    lambda s: _html.escape(s, quote=True),
    "Hello <b>World</b> & 'everyone' who reads \"this\" page! " * 3,
)

# url_encode
print("\nurl_encode:")
bench("all safe chars", url_encode_native, lambda s: quote(s, safe=""), "helloworld123")
bench(
    "with spaces and specials",
    url_encode_native,
    lambda s: quote(s, safe=""),
    "hello world & foo=bar",
)
bench(
    "long path",
    url_encode_native,
    lambda s: quote(s, safe=""),
    "/api/v1/users/search?q=hello world&page=1",
)

# url_decode
print("\nurl_decode:")
bench("no encoding", url_decode_native, unquote, "helloworld123")
bench("percent-encoded", url_decode_native, unquote, "hello%20world%26foo%3Dbar")
bench("plus spaces", url_decode_native, unquote, "hello+world+foo+bar")

# parse_query_string
print("\nparse_query_string:")
bench(
    "simple k=v",
    parse_query_string_native,
    lambda s: parse_qs(s, keep_blank_values=True),
    "a=1&b=2&c=3",
)
bench(
    "multi-value",
    parse_query_string_native,
    lambda s: parse_qs(s, keep_blank_values=True),
    "tag=python&tag=zig&tag=rust&sort=name",
)
bench(
    "url-encoded values",
    parse_query_string_native,
    lambda s: parse_qs(s, keep_blank_values=True),
    "q=hello%20world&page=1&filter=name%3Dalice",
)
bench(
    "long query (10 params)",
    parse_query_string_native,
    lambda s: parse_qs(s, keep_blank_values=True),
    "&".join(f"key{i}=value{i}" for i in range(10)),
)

print("\nDone.")
