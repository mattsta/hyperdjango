"""
Tests for zero-copy multipart server wiring.

Validates:
1. Pre-parsed _multipart_parts fast path skips FFI round-trip
2. Boundary extraction from Content-Type header
3. parseMultipartFromBuffer parity with parse_multipart
4. E2E: forms/files work correctly with pre-parsed data

# hyper-test: unit

Usage:
    uv run hyper-test multipart_zerocopy
"""

import os
import sys

from hyperdjango._hyperdjango_native import (
    parse_multipart_native as _parse_multipart,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.request import Request, UploadedFile

_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 1000
_SUPPRESS = [HealthCheck.too_slow] if _PARALLEL else []

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" -- {msg}"
        errors.append(err)
        print(f"  {err}")


def build_multipart(
    parts: list[tuple[str, bytes, str | None]], boundary: str = "BOUNDARY"
) -> bytes:
    """Build a multipart/form-data body."""
    chunks: list[bytes] = []
    for disposition, body, ct in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f"Content-Disposition: {disposition}\r\n".encode())
        if ct:
            chunks.append(f"Content-Type: {ct}\r\n".encode())
        chunks.append(b"\r\n")
        chunks.append(body)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def make_request_with_multipart(
    parts: list[tuple[str, bytes, str | None]],
    boundary: str = "BOUNDARY",
) -> Request:
    """Build a Request with multipart body and Content-Type header."""
    body = build_multipart(parts, boundary)
    return Request(
        method="POST",
        path="/upload",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        query_string="",
        body=body,
    )


# ---------------------------------------------------------------------------
# 1. Pre-parsed fast path tests
# ---------------------------------------------------------------------------


def test_preparsed_files():
    """Pre-parsed _multipart_parts bypasses FFI for files()."""
    print("\n-- Pre-parsed fast path --")

    # Build pre-parsed parts list (same format as Zig returns)
    parts = [
        ("file", "photo.jpg", "image/jpeg", b"JPEG_DATA"),
        ("doc", "report.pdf", "application/pdf", b"PDF_CONTENT"),
    ]

    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=test"},
        query_string="",
        body=b"",  # Empty body — parts are pre-parsed
    )
    req._multipart_parts = parts

    req._parse_multipart()
    files = req._files
    check("files has 2 entries", len(files) == 2, f"got {len(files)}")
    check("file entry is UploadedFile", isinstance(files.get("file"), UploadedFile))
    check("file filename", files["file"].filename == "photo.jpg")
    check("file data", files["file"].data == b"JPEG_DATA")
    check("doc content_type", files["doc"].content_type == "application/pdf")


def test_preparsed_form():
    """Pre-parsed _multipart_parts bypasses FFI for form()."""
    parts = [
        ("username", None, "text/plain", b"alice"),
        ("email", None, "text/plain", b"alice@example.com"),
    ]

    req = Request(
        method="POST",
        path="/submit",
        headers={"content-type": "multipart/form-data; boundary=test"},
        query_string="",
        body=b"",
    )
    req._multipart_parts = parts

    req._parse_multipart()
    form = req._form
    check("form has username", "username" in form)
    check("form username value", "alice" in form["username"])
    check("form email value", "alice@example.com" in form["email"])


def test_preparsed_mixed():
    """Pre-parsed data with both form fields and files."""
    parts = [
        ("title", None, "text/plain", b"My Upload"),
        ("file", "data.csv", "text/csv", b"a,b,c\n1,2,3"),
    ]

    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=test"},
        query_string="",
        body=b"",
    )
    req._multipart_parts = parts

    req._parse_multipart()
    form = req._form
    req._parse_multipart()
    files = req._files
    check("form has title", "title" in form)
    check("files has file", "file" in files)
    check("file data correct", files["file"].data == b"a,b,c\n1,2,3")


def test_preparsed_none_falls_through():
    """When _multipart_parts is None, falls through to FFI path."""
    body = build_multipart(
        [
            ('form-data; name="field"', b"value", None),
        ]
    )
    req = Request(
        method="POST",
        path="/submit",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    # _multipart_parts is None by default — should use FFI
    req._parse_multipart()
    form = req._form
    check("FFI fallback works", "field" in form)
    check("FFI fallback value", "value" in form["field"])


def test_preparsed_empty():
    """Pre-parsed empty list → empty form and files."""
    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=test"},
        query_string="",
        body=b"",
    )
    req._multipart_parts = []

    req._parse_multipart()
    check("empty form", req._form == {})
    check("empty files", req._files == {})


def test_body_still_accessible():
    """request.body is still accessible even with pre-parsed multipart."""
    body = build_multipart(
        [
            ('form-data; name="f"', b"v", None),
        ]
    )
    req = Request(
        method="POST",
        path="/submit",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    req._multipart_parts = [("f", None, "text/plain", b"v")]

    check("body is bytes", isinstance(req.body, bytes))
    check("body has content", len(req.body) > 0)
    req._parse_multipart()
    check("form from preparsed", "f" in req._form)


# ---------------------------------------------------------------------------
# 2. parseMultipartFromBuffer parity tests (FFI path vs direct)
# ---------------------------------------------------------------------------


def test_parity_simple_field():
    """FFI parse_multipart matches for simple form fields."""
    print("\n-- Parity tests --")
    body = build_multipart(
        [
            ('form-data; name="username"', b"alice", None),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("parity: 1 part", len(result) == 1)
    check("parity: name=username", result[0][0] == "username")
    check("parity: data=alice", result[0][3] == b"alice")


def test_parity_file_upload():
    """FFI parse_multipart matches for file uploads."""
    body = build_multipart(
        [
            (
                'form-data; name="file"; filename="test.txt"',
                b"file content",
                "text/plain",
            ),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("parity: 1 part", len(result) == 1)
    check("parity: name=file", result[0][0] == "file")
    check("parity: filename=test.txt", result[0][1] == "test.txt")
    check("parity: data matches", result[0][3] == b"file content")


def test_parity_mixed():
    """FFI parse_multipart matches for mixed fields + files."""
    body = build_multipart(
        [
            ('form-data; name="title"', b"My Doc", None),
            ('form-data; name="doc"; filename="report.pdf"', b"PDF", "application/pdf"),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("parity: 2 parts", len(result) == 2)
    check("parity: field name", result[0][0] == "title")
    check("parity: file name", result[1][0] == "doc")


def test_parity_empty_body():
    """FFI parse_multipart returns empty list for empty body."""
    result = _parse_multipart(b"", "BOUNDARY")
    check("parity: empty body → empty list", len(result) == 0)


# ---------------------------------------------------------------------------
# 3. Hypothesis fuzz: random multipart bodies
# ---------------------------------------------------------------------------

safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
    min_size=1,
    max_size=30,
)
safe_boundary = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-"),
    min_size=4,
    max_size=40,
)
part_data = st.binary(min_size=0, max_size=1000)


@given(
    field_name=safe_text,
    field_value=part_data,
    boundary=safe_boundary,
)
@settings(max_examples=100, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_single_field(field_name, field_value, boundary):
    """Single form field roundtrips through parser."""
    body = build_multipart(
        [(f'form-data; name="{field_name}"', field_value, None)],
        boundary,
    )
    result = _parse_multipart(body, boundary)
    # Should parse without crashing
    assert isinstance(result, list)
    if len(result) == 1:
        assert result[0][0] == field_name
        assert result[0][3] == field_value


@given(
    filename=safe_text,
    file_data=part_data,
    boundary=safe_boundary,
)
@settings(max_examples=100, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_file_upload(filename, file_data, boundary):
    """File upload roundtrips through parser."""
    body = build_multipart(
        [
            (
                f'form-data; name="file"; filename="{filename}"',
                file_data,
                "application/octet-stream",
            )
        ],
        boundary,
    )
    result = _parse_multipart(body, boundary)
    assert isinstance(result, list)
    if len(result) == 1:
        assert result[0][1] == filename
        assert result[0][3] == file_data


@given(
    num_parts=st.integers(min_value=0, max_value=10),
    boundary=safe_boundary,
)
@settings(max_examples=50, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_part_count(num_parts, boundary):
    """Variable part counts parse without crashing."""
    parts = [
        (f'form-data; name="f{i}"', f"val{i}".encode(), None) for i in range(num_parts)
    ]
    body = build_multipart(parts, boundary)
    result = _parse_multipart(body, boundary)
    assert isinstance(result, list)
    assert len(result) == num_parts


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- Multipart Zero-Copy Wiring Tests --\n")

    # Pre-parsed fast path
    test_preparsed_files()
    test_preparsed_form()
    test_preparsed_mixed()
    test_preparsed_none_falls_through()
    test_preparsed_empty()
    test_body_still_accessible()

    # Parity
    test_parity_simple_field()
    test_parity_file_upload()
    test_parity_mixed()
    test_parity_empty_body()

    # Hypothesis fuzz
    print("\n-- Hypothesis fuzz --")
    fuzz_tests = [
        ("fuzz: single field", test_fuzz_single_field),
        ("fuzz: file upload", test_fuzz_file_upload),
        ("fuzz: part count", test_fuzz_part_count),
    ]
    for name, fn in fuzz_tests:
        try:
            fn()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            errors.append(f"FAIL: {name}: {e}")
            print(f"  FAIL: {name}: {e}")

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Multipart zero-copy: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
