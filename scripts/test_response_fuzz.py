"""
Hypothesis fuzz tests for the Response class and its serialization.

Tests Response.json(), Response.html(), Response.text(), set_cookie(),
delete_cookie(), status codes, header CRLF injection prevention, and
Content-Type handling.

# hyper-test: unit
"""

import sys
import traceback

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.native import fast_json_loads
from hyperdjango.response import Response, _sanitize_header

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# JSON-serializable values (recursive)
json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=100),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=10),
        st.dictionaries(st.text(max_size=20), children, max_size=10),
    ),
    max_leaves=50,
)

# Dicts suitable for Response.json()
json_dicts = st.dictionaries(
    st.text(min_size=1, max_size=30),
    json_values,
    min_size=0,
    max_size=15,
)

# Valid HTTP status codes
http_status_codes = st.integers(min_value=100, max_value=599)

# Strings that may contain CRLF injection attempts
crlf_strings = st.one_of(
    st.text(max_size=200),
    st.sampled_from(
        [
            "\r\n",
            "\r",
            "\n",
            "value\r\nEvil-Header: injected",
            "value\nEvil-Header: injected",
            "value\rEvil-Header: injected",
            "\r\nSet-Cookie: evil=1",
            "normal-value",
            "",
            "\x00",
            "abc\r\ndef\r\nghi",
        ]
    ),
)

# Cookie name/value strings (printable-ish, may include adversarial chars)
cookie_strings = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=1,
        max_size=50,
    ),
    st.sampled_from(
        [
            "session_id",
            "csrf_token",
            "lang",
            "name with spaces",
            "name=with=equals",
            "name;with;semicolons",
        ]
    ),
)

cookie_values = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        max_size=100,
    ),
    st.sampled_from(
        [
            "",
            "abc123",
            "value with spaces",
            "value\r\nEvil: header",
            "\r\n\r\n",
        ]
    ),
)


# ---------------------------------------------------------------------------
# Property 1: Response.json() roundtrip — arbitrary dicts produce valid JSON
# ---------------------------------------------------------------------------


@given(data=json_dicts)
@settings(max_examples=200, deadline=2000)
def test_json_roundtrip(data):
    """Response.json(data).body is valid JSON that round-trips."""
    resp = Response.json(data)
    assert isinstance(resp.body, bytes)
    # Must be parseable JSON
    parsed = fast_json_loads(resp.body)
    # Round-trip: parsed data equals input
    assert parsed == data


# ---------------------------------------------------------------------------
# Property 2: Response.html() preserves content as-is in body
# ---------------------------------------------------------------------------


@given(content=st.text(max_size=500))
@settings(max_examples=200, deadline=1000)
def test_html_body_preserved(content):
    """Response.html(content) body equals the original string encoded as UTF-8."""
    resp = Response.html(content)
    assert resp.body == content.encode("utf-8")
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Property 3: Response.text() preserves content as-is in body
# ---------------------------------------------------------------------------


@given(content=st.text(max_size=500))
@settings(max_examples=200, deadline=1000)
def test_text_body_preserved(content):
    """Response.text(content) body equals the original string encoded as UTF-8."""
    resp = Response.text(content)
    assert resp.body == content.encode("utf-8")
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Property 4: set_cookie never allows CRLF injection in Set-Cookie headers
# ---------------------------------------------------------------------------


@given(name=cookie_strings, value=cookie_values)
@settings(max_examples=200, deadline=1000)
def test_set_cookie_no_crlf_injection(name, value):
    """set_cookie with arbitrary name/value never produces raw CRLF in the header value."""
    resp = Response.text("ok")
    resp.set_cookie(name, value)
    raw_header = resp.headers.get("set-cookie", "")
    # The set-cookie header uses \r\nset-cookie: as a multi-cookie separator
    # (an internal convention). Split on that separator and check each individual
    # cookie string for stray CRLF that is NOT the separator.
    cookie_parts = raw_header.split("\r\nset-cookie: ")
    for part in cookie_parts:
        # Within each individual cookie directive, no \r or \n allowed
        assert "\r" not in part, f"CR found in cookie part: {part!r}"
        assert "\n" not in part, f"LF found in cookie part: {part!r}"


# ---------------------------------------------------------------------------
# Property 5: Any valid HTTP status code (100-599) produces a response
# ---------------------------------------------------------------------------


@given(status=http_status_codes)
@settings(max_examples=200, deadline=1000)
def test_status_code_any_valid(status):
    """Response with any status code 100-599 is constructed without error."""
    resp = Response.json({"ok": True}, status=status)
    assert resp.status == status
    assert isinstance(resp.body, bytes)

    resp_html = Response.html("<p>hi</p>", status=status)
    assert resp_html.status == status

    resp_text = Response.text("hello", status=status)
    assert resp_text.status == status

    resp_empty = Response.empty(status=status)
    assert resp_empty.status == status


# ---------------------------------------------------------------------------
# Property 6: Response headers never allow CRLF injection
# ---------------------------------------------------------------------------


@given(header_value=crlf_strings)
@settings(max_examples=200, deadline=1000)
def test_response_header_no_crlf(header_value):
    """Headers set at construction time have CRLF stripped by _sanitize_header."""
    resp = Response(
        body=b"test",
        headers={"x-custom": header_value},
    )
    stored = resp.headers["x-custom"]
    # _sanitize_header strips both \r and \n
    assert "\r" not in stored, f"CR in header value: {stored!r}"
    assert "\n" not in stored, f"LF in header value: {stored!r}"


@given(header_value=crlf_strings)
@settings(max_examples=200, deadline=1000)
def test_sanitize_header_directly(header_value):
    """_sanitize_header truncates at the first CR/LF and strips controls.

    Everything after a header-split attempt is attacker payload, so the
    value TRUNCATES there (stripping used to concatenate the pieces into
    one token that reflected attacker text). Remaining C0 controls and
    DEL are removed; printable input passes through unchanged.
    """
    sanitized = _sanitize_header(header_value)
    assert "\r" not in sanitized
    assert "\n" not in sanitized
    cut = len(header_value)
    for i, ch in enumerate(header_value):
        if ch in "\r\n":
            cut = i
            break
    expected = "".join(
        ch for ch in header_value[:cut] if not (ord(ch) < 0x20 or ord(ch) == 0x7F)
    )
    assert sanitized == expected
    if header_value.isprintable():
        assert sanitized == header_value


# ---------------------------------------------------------------------------
# Property 7: Response.json() always produces application/json content-type
# ---------------------------------------------------------------------------


@given(data=json_dicts, status=http_status_codes)
@settings(max_examples=200, deadline=1000)
def test_json_content_type(data, status):
    """Response.json() always sets content-type to application/json."""
    resp = Response.json(data, status=status)
    ct = resp.headers["content-type"]
    assert ct == "application/json; charset=utf-8", f"Unexpected content-type: {ct!r}"


# ---------------------------------------------------------------------------
# Property 8: Response.html() always produces text/html content-type
# ---------------------------------------------------------------------------


@given(content=st.text(max_size=200), status=http_status_codes)
@settings(max_examples=100, deadline=1000)
def test_html_content_type(content, status):
    """Response.html() always sets content-type to text/html."""
    resp = Response.html(content, status=status)
    ct = resp.headers["content-type"]
    assert ct == "text/html; charset=utf-8", f"Unexpected content-type: {ct!r}"


# ---------------------------------------------------------------------------
# Property 9: delete_cookie sets Max-Age=0
# ---------------------------------------------------------------------------


@given(name=cookie_strings)
@settings(max_examples=100, deadline=1000)
def test_delete_cookie_max_age_zero(name):
    """delete_cookie always sets Max-Age=0 and strips CRLF from the name."""
    resp = Response.text("ok")
    resp.delete_cookie(name)
    raw_header = resp.headers.get("set-cookie", "")
    assert "Max-Age=0" in raw_header, f"Max-Age=0 missing in: {raw_header!r}"
    # No stray CRLF in the cookie parts
    cookie_parts = raw_header.split("\r\nset-cookie: ")
    for part in cookie_parts:
        assert "\r" not in part, f"CR found in delete_cookie part: {part!r}"
        assert "\n" not in part, f"LF found in delete_cookie part: {part!r}"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def test_set_cookie_no_attribute_injection():
    """set_cookie components (value/path/domain/samesite) cannot inject a ';'
    (a forged cookie attribute like Domain=/dropping Secure) or CR/LF (a new
    header). Every component is sanitized; samesite is allowlisted."""

    def only_line(**kw):
        r = Response.text("ok")
        r.set_cookie("sid", "abc", **kw)
        return r._cookie_lines()[-1]

    # ';' in the value must not open a new attribute
    r = Response.text("ok")
    r.set_cookie("sid", "abc; Domain=evil.com")
    val_line = r._cookie_lines()[-1]
    assert "; Domain=evil.com" not in val_line, (
        f"value injected attribute: {val_line!r}"
    )

    # CR/LF in path / domain must not split a header
    for kw in ({"path": "/x\r\nSet-Cookie: a=1"}, {"domain": "e.com\r\nX: 1"}):
        l = only_line(**kw)
        assert "\r" not in l and "\n" not in l, f"CRLF survived: {l!r}"

    # ';' in path is stripped so it can't open a SEPARATE attribute (the chars
    # remain as harmless path text, e.g. Path=/aDomain=evil — one value).
    p = only_line(path="/a;Domain=evil")
    assert "; Domain=evil" not in p, f"path injected a separate attribute: {p!r}"
    assert p.count("Domain=") == 0 or "; Domain=" not in p

    # samesite is allowlisted — a forged value falls back to Lax, never verbatim
    assert "SameSite=Lax" in only_line(samesite="Lax; Domain=evil.com")
    assert "Domain=evil.com" not in only_line(samesite="Lax; Domain=evil.com")
    assert "SameSite=Lax" in only_line(samesite="garbage")
    assert "SameSite=Strict" in only_line(samesite="strict")

    # legit cookie is unaffected
    legit = only_line(
        path="/app", domain="example.com", samesite="Strict", secure=True, max_age=3600
    )
    assert "Path=/app" in legit and "Domain=example.com" in legit
    assert "SameSite=Strict" in legit and "Secure" in legit and "Max-Age=3600" in legit


def main():
    print("\n-- Response Hypothesis Fuzz Tests --\n")

    tests = [
        ("json roundtrip", test_json_roundtrip),
        ("html body preserved", test_html_body_preserved),
        ("text body preserved", test_text_body_preserved),
        ("set_cookie no CRLF injection", test_set_cookie_no_crlf_injection),
        ("set_cookie no attribute injection", test_set_cookie_no_attribute_injection),
        ("status code any valid", test_status_code_any_valid),
        ("response header no CRLF", test_response_header_no_crlf),
        ("sanitize_header directly", test_sanitize_header_directly),
        ("json content-type", test_json_content_type),
        ("html content-type", test_html_content_type),
        ("delete_cookie max-age=0", test_delete_cookie_max_age_zero),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Response fuzz: {passed}/{total} passed")
    if failed:
        print(f"FAILED ({failed} failures)")
        sys.exit(failed)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    main()
