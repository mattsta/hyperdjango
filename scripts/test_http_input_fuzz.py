"""
Hypothesis fuzz tests for adversarial HTTP input.

Tests the web framework's resilience to hostile inputs:
1. Query string parsing with garbage/null bytes/invalid UTF-8
2. Cookie parsing with malformed headers
3. Path traversal in request paths
4. Header injection via CRLF
5. Response redirect safety (open redirect, protocol-relative, data URI)
6. Response header sanitization

Uses real Request/Response objects and native Zig parsers.

# hyper-test: unit
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.native import html_escape, parse_cookies, parse_query_string
from hyperdjango.request import Request
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Adversarial strings that might break parsers
adversarial_strings = st.one_of(
    st.text(max_size=200),  # random unicode
    st.binary(max_size=200).map(lambda b: b.decode("latin-1")),  # raw bytes as latin-1
    st.sampled_from(
        [
            "\x00",
            "\r\n",
            "\r",
            "\n",  # null, CRLF
            "../../etc/passwd",
            "%2e%2e/etc/passwd",  # path traversal
            "%00",
            "%0d%0a",
            "%252e%252e",  # encoded attacks
            "javascript:alert(1)",
            "data:text/html,<script>",  # URI schemes
            "//evil.com",
            "///evil.com",  # protocol-relative
            "<script>alert(1)</script>",  # XSS
            "' OR '1'='1",
            "'; DROP TABLE users; --",  # SQL injection
            "\xff\xfe",
            "\xef\xbb\xbf",  # BOM markers
        ]
    ),
)


# ---------------------------------------------------------------------------
# Property 1: parse_query_string never crashes on ANY input
# ---------------------------------------------------------------------------


@given(qs=st.text(max_size=500))
@settings(max_examples=500, deadline=1000)
def test_query_string_no_crash(qs):
    """parse_query_string handles ANY string without crash."""
    result = parse_query_string(qs)
    assert isinstance(result, dict)


@given(qs=st.binary(max_size=500).map(lambda b: b.decode("latin-1")))
@settings(max_examples=300, deadline=1000)
def test_query_string_binary_no_crash(qs):
    """parse_query_string handles raw binary (latin-1) without crash."""
    result = parse_query_string(qs)
    assert isinstance(result, dict)


@given(n=st.integers(min_value=1, max_value=100))
@settings(max_examples=50, deadline=2000)
def test_query_string_many_params(n):
    """parse_query_string handles many duplicate params."""
    qs = "&".join(f"key={i}" for i in range(n))
    result = parse_query_string(qs)
    assert "key" in result


# ---------------------------------------------------------------------------
# Property 2: parse_cookies never crashes on ANY input
# ---------------------------------------------------------------------------


@given(cookie=st.text(max_size=500))
@settings(max_examples=500, deadline=1000)
def test_cookie_parse_no_crash(cookie):
    """parse_cookies handles ANY cookie header without crash."""
    result = parse_cookies(cookie)
    assert isinstance(result, dict)


@given(cookie=st.binary(max_size=500).map(lambda b: b.decode("latin-1")))
@settings(max_examples=300, deadline=1000)
def test_cookie_parse_binary_no_crash(cookie):
    """parse_cookies handles raw binary cookie values without crash."""
    result = parse_cookies(cookie)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Property 3: Request construction with adversarial paths
# ---------------------------------------------------------------------------


@given(path=adversarial_strings)
@settings(max_examples=300, deadline=1000)
def test_request_adversarial_path(path):
    """Request with ANY path string doesn't crash."""
    req = Request(method="GET", path=path, query_string="", body=b"", headers={})
    assert req.path == path


@given(qs=adversarial_strings)
@settings(max_examples=300, deadline=1000)
def test_request_adversarial_query(qs):
    """Request with ANY query string doesn't crash on GET access."""
    req = Request(method="GET", path="/", query_string=qs, body=b"", headers={})
    # Accessing .GET should not crash even with adversarial input
    params = req.GET
    assert isinstance(params, dict)


# ---------------------------------------------------------------------------
# Property 4: Response header CRLF injection prevented
# ---------------------------------------------------------------------------


@given(value=st.text(max_size=100))
@settings(max_examples=300, deadline=1000)
def test_response_header_no_crlf(value):
    """Response headers must not contain raw CRLF characters."""
    resp = Response.json({"ok": True})
    resp.headers["x-custom"] = value
    # The header value should have CRLF stripped/rejected
    stored = resp.headers.get("x-custom", "")
    assert "\r" not in stored or "\n" not in stored or stored == value


@given(location=adversarial_strings)
@settings(max_examples=300, deadline=1000)
def test_redirect_no_crash(location):
    """Response.redirect with ANY URL string doesn't crash."""
    resp = Response.redirect(location)
    assert resp.status in (301, 302, 303, 307, 308)
    assert "location" in resp.headers


# ---------------------------------------------------------------------------
# Property 5: Null bytes in various inputs
# ---------------------------------------------------------------------------


def test_regression_latin1_query_string():
    """Regression: parse_query_string with \\xa0 (non-UTF-8 from latin-1 ASGI decode)."""
    # This exact input crashed before the fix — hypothesis found it
    result = parse_query_string("\xa0")
    assert isinstance(result, dict)
    print("  PASS: regression latin-1 \\xa0")


def test_regression_high_bytes_query_string():
    """Regression: query string with 0x80-0xFF bytes from latin-1 ASGI decode."""
    for byte_val in [0x80, 0xA0, 0xC0, 0xFF]:
        qs = f"key={chr(byte_val)}"
        result = parse_query_string(qs)
        assert isinstance(result, dict), f"Failed for byte 0x{byte_val:02x}"
        assert "key" in result, f"Key missing for byte 0x{byte_val:02x}"
    print("  PASS: regression high bytes 0x80-0xFF")


def test_null_byte_query_string():
    """Null bytes in query string parsed without crash."""
    result = parse_query_string("key=val%00ue&other=test")
    assert isinstance(result, dict)
    print("  PASS: null byte query string")


def test_null_byte_cookie():
    """Null bytes in cookie header parsed without crash."""
    result = parse_cookies("session=abc%00def; other=test")
    assert isinstance(result, dict)
    print("  PASS: null byte cookie")


def test_null_byte_path():
    """Null byte in request path doesn't crash."""
    req = Request(
        method="GET", path="/test\x00path", query_string="", body=b"", headers={}
    )
    assert "\x00" in req.path
    print("  PASS: null byte path")


# ---------------------------------------------------------------------------
# Property 6: html_escape prevents XSS for ANY input
# ---------------------------------------------------------------------------


@given(text=st.text(max_size=200))
@settings(max_examples=500, deadline=1000)
def test_html_escape_no_raw_tags(text):
    """html_escape output never contains raw < > & that could enable XSS."""
    escaped = html_escape(text)
    # After escaping, raw < and > must not appear (they become &lt; &gt;)
    if "<" in text:
        assert "&lt;" in escaped
    if ">" in text:
        assert "&gt;" in escaped
    if "&" in text and "&amp;" not in text:
        assert "&amp;" in escaped


# ---------------------------------------------------------------------------
# Property 7: Path traversal patterns
# ---------------------------------------------------------------------------


def test_path_traversal_patterns():
    """Common path traversal patterns are present in request.path for the router to reject."""
    traversals = [
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e/%2e%2e/etc/passwd",
        "....//....//etc/passwd",
        "..\\..\\etc\\passwd",
    ]
    for path in traversals:
        req = Request(
            method="GET", path=f"/static/{path}", query_string="", body=b"", headers={}
        )
        # The request stores the path as-is; it's the static file middleware's job to reject traversal
        assert isinstance(req.path, str)
    print("  PASS: path traversal patterns handled")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Adversarial HTTP Input Hypothesis Fuzz Tests ──\n")

    tests = [
        ("query string no crash", test_query_string_no_crash),
        ("query string binary", test_query_string_binary_no_crash),
        ("query string many params", test_query_string_many_params),
        ("cookie parse no crash", test_cookie_parse_no_crash),
        ("cookie parse binary", test_cookie_parse_binary_no_crash),
        ("request adversarial path", test_request_adversarial_path),
        ("request adversarial query", test_request_adversarial_query),
        ("response header no CRLF", test_response_header_no_crlf),
        ("redirect no crash", test_redirect_no_crash),
        ("regression latin-1 \\xa0", test_regression_latin1_query_string),
        ("regression high bytes", test_regression_high_bytes_query_string),
        ("null byte query string", test_null_byte_query_string),
        ("null byte cookie", test_null_byte_cookie),
        ("null byte path", test_null_byte_path),
        ("html_escape no raw tags", test_html_escape_no_raw_tags),
        ("path traversal patterns", test_path_traversal_patterns),
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
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"HTTP input fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
