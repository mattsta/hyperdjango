"""
Python fallback string operations.

When Zig native extension is available, these are replaced by
SIMD-accelerated versions using NEON (ARM) or SSE/AVX (x86).
"""

import html as _html
from urllib.parse import parse_qs as _parse_qs
from urllib.parse import quote, unquote


def html_escape(s: str) -> str:
    """Escape HTML special characters.

    Native version uses SIMD to scan 32 bytes at a time for
    <, >, &, ", ' characters.
    """
    return _html.escape(s, quote=True)


def url_encode(s: str, safe: str = "") -> str:
    """URL-encode a string.

    Native version uses SIMD to identify unreserved characters
    (alphanumeric + -_.~) and only encodes the rest.
    """
    return quote(s, safe=safe)


def url_decode(s: str) -> str:
    """URL-decode a string.

    Native version uses turboAPI's percentDecode from server.zig.
    """
    return unquote(s)


def parse_query_string(qs: str) -> dict[str, list[str]]:
    """Parse a query string into a dict of lists.

    Native version uses SIMD to find & and = delimiters.
    """
    return _parse_qs(qs, keep_blank_values=True)
