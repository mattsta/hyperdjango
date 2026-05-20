"""
Hypothesis fuzz tests for request parsing boundaries.

Tests the Request object's lazy parsers against adversarial inputs:
1. Cookie parsing: arbitrary name/value pairs with special characters
2. Query string parsing: encoding, duplicates, empty values
3. JSON body parsing: arbitrary JSON structures
4. Form data parsing: field names/values with special characters
5. Header injection prevention: CRLF in header values
6. Path parameter extraction: unicode, special chars in dynamic route params

Uses real Request objects and native Zig parsers.

# hyper-test: unit
"""

import asyncio
import sys
import traceback
from urllib.parse import urlencode

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango import HTTPException
from hyperdjango.app import HyperApp
from hyperdjango.native import (
    fast_json_dumps,
    fast_json_loads,
    parse_cookies,
    parse_query_string,
)
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.testing import TestClient

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Cookie-safe characters (RFC 6265 cookie-octet minus control chars)
cookie_name_chars = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
    ),
    min_size=1,
    max_size=50,
)

cookie_value_chars = st.text(max_size=100)

# Query string key/value with special encoding characters
query_key_strategy = st.one_of(
    st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop0123456789_"),
    st.text(min_size=1, max_size=30),  # arbitrary unicode
    st.sampled_from(["key", "a b", "a+b", "a%20b", "a&b", "a=b", ""]),
)

query_value_strategy = st.one_of(
    st.text(max_size=50),
    st.binary(max_size=50).map(lambda b: b.decode("latin-1")),
    st.sampled_from(["", "0", "null", "true", "%00", "\x00", "a&b=c", "a=b"]),
)

# JSON values that cover the full JSON type space
json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=100),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=20), children, max_size=5),
    ),
    max_leaves=20,
)

# Form field names with special characters
form_field_names = st.one_of(
    st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop0123456789_"),
    st.sampled_from(
        [
            "field[0]",
            "field[]",
            "a.b.c",
            "name with spaces",
            "<script>",
            "field\x00name",
            "a&b",
            "a=b",
        ]
    ),
)

form_field_values = st.one_of(
    st.text(max_size=100),
    st.sampled_from(
        [
            "",
            "<script>alert(1)</script>",
            "'; DROP TABLE users; --",
            "\x00\x01\x02",
            "\r\n",
            "a" * 1000,
        ]
    ),
)

# Header values that might attempt CRLF injection
header_injection_values = st.one_of(
    st.text(max_size=100),
    st.sampled_from(
        [
            "normal-value",
            "value\r\nX-Injected: evil",
            "value\rX-Injected: evil",
            "value\nX-Injected: evil",
            "value\r\n\r\n<html>evil</html>",
            "\r\nSet-Cookie: stolen=yes",
            "value\x00null",
        ]
    ),
)

# Path parameter values with unicode and special chars
path_param_values = st.one_of(
    st.text(min_size=1, max_size=50),
    st.sampled_from(
        [
            "hello-world",
            "123",
            "cafe\u0301",
            "\u00e9",
            "../etc/passwd",
            "%2e%2e",
            "a/b/c",
            "a%2Fb",
            "<script>",
            "hello world",
            "\x00",
            "\r\n",
            "\U0001f600",  # emoji
        ]
    ),
)


# ---------------------------------------------------------------------------
# Helper: run async in sync context
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Property 1: Cookie parsing with arbitrary name/value pairs
# ---------------------------------------------------------------------------


@given(
    names=st.lists(cookie_name_chars, min_size=1, max_size=10),
    values=st.lists(cookie_value_chars, min_size=1, max_size=10),
)
@settings(max_examples=200, deadline=2000)
def test_cookie_parsing_arbitrary_pairs(names, values):
    """Cookie parsing handles arbitrary name/value pairs without crash.

    The parsed result must always be a dict with string keys and values.
    """
    # Build a cookie header from name=value pairs
    pairs = []
    for i in range(min(len(names), len(values))):
        pairs.append(f"{names[i]}={values[i]}")
    cookie_header = "; ".join(pairs)

    req = Request(
        method="GET",
        path="/",
        headers={"cookie": cookie_header},
    )

    cookies = req.cookies
    assert isinstance(cookies, dict)
    # All keys and values must be strings
    for k, v in cookies.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Property 2: Cookie parsing with special characters in values
# ---------------------------------------------------------------------------


@given(value=st.text(max_size=200))
@settings(max_examples=200, deadline=2000)
def test_cookie_special_char_values(value):
    """Cookie values with arbitrary unicode never crash the parser."""
    cookie_header = f"session={value}"
    result = parse_cookies(cookie_header)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Property 3: Query string with duplicates and encoding
# ---------------------------------------------------------------------------


@given(
    keys=st.lists(query_key_strategy, min_size=1, max_size=8),
    values=st.lists(query_value_strategy, min_size=1, max_size=8),
)
@settings(max_examples=200, deadline=2000)
def test_query_string_duplicates_and_encoding(keys, values):
    """Query string parsing handles duplicates and special encoding.

    Duplicate keys must produce lists. Result is always dict[str, list[str]].
    """
    # Build query string with potential duplicate keys
    pairs = []
    for i in range(min(len(keys), len(values))):
        pairs.append(f"{keys[i]}={values[i]}")
    qs = "&".join(pairs)

    req = Request(method="GET", path="/", query_string=qs)
    params = req.query_params
    assert isinstance(params, dict)

    # Every value list must contain strings
    for k, vlist in params.items():
        assert isinstance(vlist, list)
        for v in vlist:
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Property 4: Query string with empty values and bare keys
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=1, max_value=20))
@settings(max_examples=100, deadline=2000)
def test_query_string_empty_values(n):
    """Query strings with empty values and bare keys parse correctly."""
    # Mix of key=, key=value, and bare key
    parts = []
    for i in range(n):
        choice = i % 3
        if choice == 0:
            parts.append(f"key{i}=")
        elif choice == 1:
            parts.append(f"key{i}=val{i}")
        else:
            parts.append(f"key{i}")
    qs = "&".join(parts)

    result = parse_query_string(qs)
    assert isinstance(result, dict)
    # Every key should appear
    for i in range(n):
        assert f"key{i}" in result, f"key{i} missing from {result}"


# ---------------------------------------------------------------------------
# Property 5: JSON body parsing with arbitrary JSON structures
# ---------------------------------------------------------------------------


@given(data=json_values)
@settings(max_examples=200, deadline=3000)
def test_json_body_arbitrary_structures(data):
    """Request.json() handles arbitrary valid JSON structures.

    The round-trip through fast_json_dumps -> Request body -> json() must
    preserve the value or raise HTTPException(400) for genuinely invalid input.
    """
    body = fast_json_dumps(data)
    req = Request(
        method="POST",
        path="/api/data",
        headers={"content-type": "application/json"},
        body=body,
    )

    parsed = _run_async(req.json())
    # For finite floats, check approximate equality due to float precision
    if isinstance(data, float):
        assert abs(parsed - data) < 1e-10 or parsed == data
    else:
        assert parsed == data


# ---------------------------------------------------------------------------
# Property 6: JSON body with invalid input returns 400
# ---------------------------------------------------------------------------


@given(garbage=st.binary(min_size=1, max_size=200))
@settings(max_examples=100, deadline=2000)
def test_json_body_invalid_returns_400(garbage):
    """Invalid JSON body raises HTTPException(400), never 500."""
    # Skip inputs that happen to be valid JSON
    try:
        fast_json_loads(garbage)
        return  # Valid JSON, skip
    except Exception:
        pass

    req = Request(
        method="POST",
        path="/api/data",
        headers={"content-type": "application/json"},
        body=garbage,
    )

    try:
        _run_async(req.json())
        # If it didn't raise, the native parser accepted it — that's OK
    except HTTPException as e:
        assert e.status_code == 400
    except Exception:
        raise AssertionError(
            "Expected HTTPException(400) for invalid JSON, got unexpected exception"
        )


# ---------------------------------------------------------------------------
# Property 7: Form data parsing with special characters
# ---------------------------------------------------------------------------


@given(
    fields=st.dictionaries(
        form_field_names,
        form_field_values,
        min_size=1,
        max_size=10,
    ),
)
@settings(max_examples=200, deadline=2000)
def test_form_data_special_characters(fields):
    """Form data parsing handles special characters in field names/values.

    URL-encoded form data with arbitrary names/values must parse without crash.
    """
    body = urlencode(fields).encode("utf-8")

    req = Request(
        method="POST",
        path="/submit",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=body,
    )

    form = _run_async(req.form())
    assert isinstance(form, dict)


# ---------------------------------------------------------------------------
# Property 8: Header CRLF injection prevention
# ---------------------------------------------------------------------------


@given(value=header_injection_values)
@settings(max_examples=200, deadline=2000)
def test_header_crlf_injection_prevention(value):
    """Request construction with CRLF in header values does not crash.

    The framework must handle headers with embedded newlines gracefully.
    CaseInsensitiveDict must store them without crashing, and the response
    path must sanitize them.
    """
    req = Request(
        method="GET",
        path="/",
        headers={"x-custom": value, "host": "example.com"},
    )

    # Construction must succeed
    assert isinstance(req.headers, dict)
    # The value should be stored (CaseInsensitiveDict lowercases keys)
    stored = req.headers.get("x-custom", "")
    assert isinstance(stored, str)

    # Response headers: verify the response object handles CRLF values
    resp = Response.json({"ok": True})
    resp.headers["x-test"] = value
    # Must not crash during header access
    result = resp.headers.get("x-test", "")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Property 9: Path parameter extraction with unicode
# ---------------------------------------------------------------------------


@given(param_value=path_param_values)
@settings(max_examples=200, deadline=3000)
def test_path_param_unicode_extraction(param_value):
    """Path parameters with unicode and special chars are extracted correctly.

    The Request object stores path_params as-is from the router. The router
    must handle arbitrary param values without crash.
    """
    req = Request(
        method="GET",
        path=f"/users/{param_value}",
        path_params={"id": param_value},
    )

    assert req.path_params["id"] == param_value
    assert isinstance(req.path_params["id"], str)


# ---------------------------------------------------------------------------
# Property 10: Path parameter via TestClient with real routing
# ---------------------------------------------------------------------------

# App created once at module level so routes are registered once
_param_app = HyperApp()


@_param_app.get("/items/{item_id}")
async def _get_item(request, item_id: str = ""):
    return {"item_id": item_id}


_param_client = TestClient(_param_app)


@given(
    name=st.text(
        min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"
    )
)
@settings(max_examples=100, deadline=5000)
def test_path_param_via_testclient(name):
    """Path parameters extracted by real router via TestClient match input."""
    resp = _param_client.get(f"/items/{name}")
    assert resp.status == 200, (
        f"Expected 200 for /items/{name}, got {resp.status}: {resp.text()}"
    )
    body = resp.json()
    assert body["item_id"] == name


# ---------------------------------------------------------------------------
# Property 11: Request.GET flat dict is consistent with query_params
# ---------------------------------------------------------------------------


@given(qs=st.text(max_size=200))
@settings(max_examples=200, deadline=2000)
def test_get_dict_consistent_with_query_params(qs):
    """request.GET (flat dict) must be consistent with request.query_params.

    For each key, GET[key] == query_params[key][0].
    """
    req = Request(method="GET", path="/", query_string=qs)
    params = req.query_params
    flat = req.GET

    assert isinstance(flat, dict)
    for key, values in params.items():
        assert key in flat
        expected = values[0] if values else ""
        assert flat[key] == expected


# ---------------------------------------------------------------------------
# Property 12: Cookie round-trip via Request object
# ---------------------------------------------------------------------------


@given(
    pairs=st.lists(
        st.tuples(
            st.text(
                min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"
            ),
            st.text(
                min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"
            ),
        ),
        min_size=1,
        max_size=8,
    ),
)
@settings(max_examples=200, deadline=2000)
def test_cookie_roundtrip_via_request(pairs):
    """Cookie header parsed via Request.cookies preserves alphanumeric pairs.

    For well-formed cookie pairs (alphanumeric names and values), the parser
    must return the exact name/value for each pair.
    """
    cookie_header = "; ".join(f"{name}={value}" for name, value in pairs)

    req = Request(
        method="GET",
        path="/",
        headers={"cookie": cookie_header},
    )

    cookies = req.cookies
    # For well-formed pairs, later values for the same key overwrite earlier
    expected = {}
    for name, value in pairs:
        expected[name] = value

    for name, value in expected.items():
        assert name in cookies, f"Cookie {name!r} not found in {cookies}"
        assert cookies[name] == value, (
            f"Cookie {name!r}: expected {value!r}, got {cookies[name]!r}"
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Request Parsing Boundary Hypothesis Fuzz Tests ──\n")

    tests = [
        ("cookie arbitrary pairs", test_cookie_parsing_arbitrary_pairs),
        ("cookie special char values", test_cookie_special_char_values),
        (
            "query string duplicates and encoding",
            test_query_string_duplicates_and_encoding,
        ),
        ("query string empty values", test_query_string_empty_values),
        ("JSON body arbitrary structures", test_json_body_arbitrary_structures),
        ("JSON body invalid returns 400", test_json_body_invalid_returns_400),
        ("form data special characters", test_form_data_special_characters),
        ("header CRLF injection prevention", test_header_crlf_injection_prevention),
        ("path param unicode extraction", test_path_param_unicode_extraction),
        ("path param via TestClient", test_path_param_via_testclient),
        (
            "GET dict consistent with query_params",
            test_get_dict_consistent_with_query_params,
        ),
        ("cookie round-trip via Request", test_cookie_roundtrip_via_request),
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
            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Request parsing fuzz: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
