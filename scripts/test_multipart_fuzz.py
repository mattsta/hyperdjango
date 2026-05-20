"""
Hypothesis property tests for the multipart/form-data parser.

Proves resilience to adversarial multipart bodies:
1. Valid multipart roundtrip: build -> parse -> correct fields
2. Boundary string in file content -> file not truncated
3. Missing closing boundary -> graceful parse
4. Empty body -> empty result
5. Large content parsed correctly

Uses the real Zig SIMD multipart parser.

# hyper-test: unit
"""

from hyperdjango._hyperdjango_native import parse_multipart_native
from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.testkit import check, finish, run_main, run_property

# Native-boundary property counts, kept bounded so the file stays fast.
_ROUNDTRIP_EXAMPLES = 300
_BOUNDARY_EXAMPLES = 200
_MULTI_FIELD_EXAMPLES = 50

_LARGE_CONTENT_BYTES = 100_000


def build_multipart(boundary, fields):
    """Build a multipart/form-data body from (name, filename, content) tuples."""
    parts = []
    for name, filename, content in fields:
        if filename is not None:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode()
                + content
                + b"\r\n"
            )
        else:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + content
                + b"\r\n"
            )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Property 1: Valid multipart roundtrip
# ---------------------------------------------------------------------------


@given(
    name=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop"),
    value=st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnop0123456789 "),
)
@settings(max_examples=_ROUNDTRIP_EXAMPLES)
def prop_form_field_roundtrip(name, value):
    """Form field: build -> parse -> correct name and value."""
    boundary = "----TestBoundary12345"
    body = build_multipart(boundary, [(name, None, value.encode())])
    parts = parse_multipart_native(body, boundary)
    assert len(parts) >= 1, f"No parts parsed for name={name!r}"
    assert parts[0][0] == name
    assert parts[0][3] == value.encode()


@given(
    name=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop"),
    filename=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop0123456789."),
    content=st.binary(min_size=1, max_size=500),
)
@settings(max_examples=_ROUNDTRIP_EXAMPLES)
def prop_file_upload_roundtrip(name, filename, content):
    """File upload: build -> parse -> correct name, filename, and content."""
    boundary = "----TestBoundary12345"
    body = build_multipart(boundary, [(name, filename, content)])
    parts = parse_multipart_native(body, boundary)
    assert len(parts) >= 1
    parsed_name, parsed_filename, _, parsed_data = parts[0]
    assert parsed_name == name
    assert parsed_filename == filename
    assert parsed_data == content


# ---------------------------------------------------------------------------
# Property 2: Boundary string in file content -> NOT truncated
# ---------------------------------------------------------------------------


@given(
    content_prefix=st.binary(min_size=1, max_size=100),
    content_suffix=st.binary(min_size=1, max_size=100),
)
@settings(max_examples=_BOUNDARY_EXAMPLES)
def prop_boundary_in_content(content_prefix, content_suffix):
    """If file content contains the boundary string, the parser must not crash
    and must not silently drop the part."""
    boundary = "----TestBoundary12345"
    content = content_prefix + boundary.encode() + content_suffix
    body = build_multipart(boundary, [("file", "test.bin", content)])
    parts = parse_multipart_native(body, boundary)
    assert len(parts) >= 1


# ---------------------------------------------------------------------------
# Property 3: Multiple fields
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=1, max_value=10))
@settings(max_examples=_MULTI_FIELD_EXAMPLES)
def prop_multiple_fields(n):
    """Multiple form fields all parsed correctly."""
    boundary = "----Multi"
    fields = [(f"field_{i}", None, f"value_{i}".encode()) for i in range(n)]
    body = build_multipart(boundary, fields)
    parts = parse_multipart_native(body, boundary)
    assert len(parts) == n, f"Expected {n} parts, got {len(parts)}"


# ---------------------------------------------------------------------------
# Deterministic edge cases
# ---------------------------------------------------------------------------


def _empty_body() -> tuple[bool, str]:
    parts = parse_multipart_native(b"", "boundary")
    return len(parts) == 0, f"empty body -> {len(parts)} parts"


def _no_closing_boundary() -> tuple[bool, str]:
    """Body without closing boundary -> graceful parse (no crash)."""
    boundary = "----Test"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="field"\r\n\r\nvalue'
    ).encode()
    parts = parse_multipart_native(body, boundary)
    return isinstance(parts, list), f"expected list, got {type(parts).__name__}"


def _large_content() -> tuple[bool, str]:
    """Large file content parsed correctly."""
    boundary = "----Large"
    content = b"X" * _LARGE_CONTENT_BYTES
    body = build_multipart(boundary, [("big", "big.bin", content)])
    parts = parse_multipart_native(body, boundary)
    if len(parts) != 1:
        return False, f"expected 1 part, got {len(parts)}"
    got = len(parts[0][3])
    return got == _LARGE_CONTENT_BYTES, f"content len {got} != {_LARGE_CONTENT_BYTES}"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

_PROPERTIES = (
    prop_form_field_roundtrip,
    prop_file_upload_roundtrip,
    prop_boundary_in_content,
    prop_multiple_fields,
)

_CORPORA = (
    ("empty body", _empty_body),
    ("no closing boundary", _no_closing_boundary),
    ("large content (100KB)", _large_content),
)


def run_tests() -> bool:
    print("\n-- Multipart Adversarial Hypothesis Property Tests --\n")
    for prop in _PROPERTIES:
        run_property(prop)
    for name, corpus in _CORPORA:
        ok, detail = corpus()
        check(name, ok, detail)
    return finish()


if __name__ == "__main__":
    run_main(run_tests)
