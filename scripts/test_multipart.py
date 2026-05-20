#!/usr/bin/env python3
"""
Test the native multipart/form-data parser.

Run: uv run hyper-test multipart
"""

# hyper-test: unit

import asyncio
import traceback
from collections.abc import Callable

from hyperdjango._hyperdjango_native import parse_multipart_native

from hyperdjango.request import Request
from hyperdjango.testkit import check, finish, run_main


def test_simple_field():
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = (
        "------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
        'Content-Disposition: form-data; name="username"\r\n'
        "\r\n"
        "alice\r\n"
        "------WebKitFormBoundary7MA4YWxkTrZu0gW--\r\n"
    )
    parts = parse_multipart_native(body, boundary)
    assert len(parts) == 1
    name, filename, ct, data = parts[0]
    assert name == "username"
    assert filename is None
    assert data == b"alice"


def test_file_upload():
    boundary = "boundary123"
    body = (
        "--boundary123\r\n"
        'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
        "Content-Type: text/plain\r\n"
        "\r\n"
        "Hello, World!\r\n"
        "--boundary123--\r\n"
    )
    parts = parse_multipart_native(body, boundary)
    assert len(parts) == 1
    name, filename, ct, data = parts[0]
    assert name == "file"
    assert filename == "test.txt"
    assert ct == "text/plain"
    assert data == b"Hello, World!"


def test_mixed_fields_and_files():
    boundary = "abc"
    body = (
        "--abc\r\n"
        'Content-Disposition: form-data; name="title"\r\n'
        "\r\n"
        "My Document\r\n"
        "--abc\r\n"
        'Content-Disposition: form-data; name="doc"; filename="doc.pdf"\r\n'
        "Content-Type: application/pdf\r\n"
        "\r\n"
        "PDF CONTENT HERE\r\n"
        "--abc\r\n"
        'Content-Disposition: form-data; name="tags"\r\n'
        "\r\n"
        "important\r\n"
        "--abc--\r\n"
    )
    parts = parse_multipart_native(body, boundary)
    assert len(parts) == 3

    assert parts[0][0] == "title"
    assert parts[0][1] is None
    assert parts[0][3] == b"My Document"

    assert parts[1][0] == "doc"
    assert parts[1][1] == "doc.pdf"
    assert parts[1][2] == "application/pdf"
    assert parts[1][3] == b"PDF CONTENT HERE"

    assert parts[2][0] == "tags"
    assert parts[2][3] == b"important"


def test_empty_body():
    parts = parse_multipart_native("", "boundary")
    assert len(parts) == 0


def test_request_integration():
    """Test through the Request object."""
    boundary = "testbound"
    body = (
        "--testbound\r\n"
        'Content-Disposition: form-data; name="name"\r\n'
        "\r\n"
        "Alice\r\n"
        "--testbound\r\n"
        'Content-Disposition: form-data; name="avatar"; filename="pic.jpg"\r\n'
        "Content-Type: image/jpeg\r\n"
        "\r\n"
        "JPEG DATA\r\n"
        "--testbound--\r\n"
    )

    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        body=body.encode("latin-1"),
    )

    form = asyncio.run(req.form())
    files = asyncio.run(req.files())

    assert form["name"] == ["Alice"]
    assert "avatar" in files
    assert files["avatar"].filename == "pic.jpg"
    assert files["avatar"].content_type == "image/jpeg"
    assert files["avatar"].data == b"JPEG DATA"


def main() -> bool:
    print("Testing native multipart parser:")
    tests: tuple[Callable[[], None], ...] = (
        test_simple_field,
        test_file_upload,
        test_mixed_fields_and_files,
        test_empty_body,
        test_request_integration,
    )
    # Bare asserts abort the file on the first break — that is this suite's
    # contract; the counts are emitted before bailing out.
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
        check(fn.__name__, True)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
