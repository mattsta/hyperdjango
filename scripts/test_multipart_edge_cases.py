"""
Multipart parser edge case tests.

# hyper-test: unit

Tests extractParam escaped quotes and extractParamEncoded RFC 5987
filename* support via the parse_multipart_native FFI.
"""

import sys

from hyperdjango._hyperdjango_native import (
    parse_multipart_native as _parse_multipart,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}", flush=True)
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}", flush=True)


def build_multipart(
    parts: list[tuple[str, bytes, str | None]], boundary: str = "BOUNDARY"
) -> bytes:
    """Build a multipart/form-data body from (disposition_header, body, content_type) tuples."""
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


def test_normal_filename() -> None:
    print('\n── Normal filename="..." ──', flush=True)
    body = build_multipart(
        [
            ('form-data; name="file"; filename="hello.txt"', b"content", "text/plain"),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("one part returned", len(result) == 1)
    if result:
        name, filename, ct, data = result[0]
        check("name is 'file'", name == "file")
        check("filename is 'hello.txt'", filename == "hello.txt", f"got {filename!r}")
        check("body is 'content'", data == b"content")


def test_escaped_quote_in_filename() -> None:
    """filename="file\\"name.txt" should parse as file"name.txt"""
    print("\n── Escaped quote in filename ──", flush=True)
    body = build_multipart(
        [
            (
                'form-data; name="file"; filename="file\\"name.txt"',
                b"data",
                "text/plain",
            ),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("one part returned", len(result) == 1)
    if result:
        name, filename, ct, data = result[0]
        check(
            "filename preserves escaped quote",
            filename == 'file\\"name.txt' or filename == 'file"name.txt',
            f"got {filename!r}",
        )


def test_rfc5987_filename_star() -> None:
    """filename*=UTF-8''%E4%B8%AD%E6%96%87.txt should return the percent-encoded string."""
    print("\n── RFC 5987 filename* ──", flush=True)
    body = build_multipart(
        [
            (
                "form-data; name=\"file\"; filename*=UTF-8''%E4%B8%AD%E6%96%87.txt",
                b"data",
                "text/plain",
            ),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("one part returned", len(result) == 1)
    if result:
        name, filename, ct, data = result[0]
        print(f"[TRACE] filename*= returned: {filename!r}", flush=True)
        # The Zig side returns the percent-encoded value after the charset''
        # Python side can percent-decode if needed
        check(
            "filename* has percent-encoded content",
            filename is not None and "%E4" in filename,
            f"got {filename!r}",
        )


def test_both_filename_and_filename_star() -> None:
    """When both filename= and filename*= are present, filename= takes priority
    (it's found first by extractParam)."""
    print("\n── Both filename and filename* ──", flush=True)
    body = build_multipart(
        [
            (
                'form-data; name="file"; filename="fallback.txt"; filename*=UTF-8\'\'%E4%B8%AD.txt',
                b"data",
                "text/plain",
            ),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("one part returned", len(result) == 1)
    if result:
        name, filename, ct, data = result[0]
        check(
            "filename= takes priority", filename == "fallback.txt", f"got {filename!r}"
        )


def test_no_filename() -> None:
    """Form field with no filename — should still parse name and body."""
    print("\n── No filename (form field) ──", flush=True)
    body = build_multipart(
        [
            ('form-data; name="field1"', b"value1", None),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("one part returned", len(result) == 1)
    if result:
        name, filename, ct, data = result[0]
        check("name is 'field1'", name == "field1")
        check(
            "filename is empty/None",
            filename == "" or filename is None,
            f"got {filename!r}",
        )


def test_filename_before_name() -> None:
    """Content-Disposition params are order-independent per RFC 7578. When
    filename= appears BEFORE name=, the 'name' param must NOT be mis-matched
    against the 'name' substring inside 'filename' — the field name must be
    the real name= value, and the filename the real filename= value."""
    print("\n── filename= before name= (param order) ──", flush=True)
    body = build_multipart(
        [
            (
                'form-data; filename="evil.txt"; name="upload"',
                b"content",
                "text/plain",
            ),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("one part returned", len(result) == 1)
    if result:
        name, filename, ct, data = result[0]
        check("name is 'upload' (not the filename)", name == "upload", f"got {name!r}")
        check("filename is 'evil.txt'", filename == "evil.txt", f"got {filename!r}")


def test_multiple_parts_mixed() -> None:
    """Multiple parts: regular field + file with filename + file with filename*."""
    print("\n── Multiple mixed parts ──", flush=True)
    body = build_multipart(
        [
            ('form-data; name="title"', b"My Document", None),
            (
                'form-data; name="doc"; filename="report.pdf"',
                b"PDF_DATA",
                "application/pdf",
            ),
            (
                "form-data; name=\"photo\"; filename*=UTF-8''%E5%9B%BE%E7%89%87.jpg",
                b"JPEG_DATA",
                "image/jpeg",
            ),
        ]
    )
    result = _parse_multipart(body, "BOUNDARY")
    check("three parts returned", len(result) == 3)
    if len(result) >= 3:
        # Tuple order: (name, filename, content_type, data)
        check("part 1 name", result[0][0] == "title")
        check("part 2 filename", result[1][1] == "report.pdf")
        check(
            "part 3 has filename* content",
            result[2][1] is not None and "%E5" in result[2][1],
        )


def main() -> int:
    print("=" * 70, flush=True)
    print("  Multipart parser edge case tests", flush=True)
    print("=" * 70, flush=True)

    test_normal_filename()
    test_escaped_quote_in_filename()
    test_rfc5987_filename_star()
    test_both_filename_and_filename_star()
    test_no_filename()
    test_filename_before_name()
    test_multiple_parts_mixed()

    print(flush=True)
    print("=" * 70, flush=True)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed", flush=True)
    if errors:
        print("\nFailures:", flush=True)
        for e in errors:
            print(f"  {e}", flush=True)
    print("=" * 70, flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
