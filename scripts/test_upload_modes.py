"""
Tests for three-mode upload system: memory, disk spill, pass-through streaming.

# hyper-test: unit

Validates:
1. UploadedFile memory mode (.data returns bytes directly)
2. UploadedFile disk spill mode (.data reads from temp file, .path set)
3. UploadedFile.chunks() works for all modes
4. _parse_multipart respects FILE_UPLOAD_MAX_MEMORY_SIZE threshold
5. _parse_multipart enforces FILE_UPLOAD_MAX_SIZE
6. request.stream() yields body in chunks
7. Settings wiring for new upload settings
8. Temp file cleanup

Usage:
    uv run hyper-test upload_modes
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.conf import DEFAULTS, SETTING_DEFINITIONS
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


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. UploadedFile memory mode
# ---------------------------------------------------------------------------


def test_memory_mode():
    print("\n-- UploadedFile memory mode --")
    f = UploadedFile(
        filename="test.txt", content_type="text/plain", _data=b"hello world"
    )
    check("data returns bytes", f.data == b"hello world")
    check("size correct", f.size == 11)
    check("in_memory is True", f.in_memory is True)
    check("path is None", f.path is None)
    check("repr shows memory", "memory" in repr(f))


def test_memory_chunks():
    f = UploadedFile(
        filename="big.bin", content_type="application/octet-stream", _data=b"A" * 1000
    )

    async def _collect():
        chunks = []
        async for chunk in f.chunks(chunk_size=300):
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    check("memory chunks: 4 chunks", len(chunks) == 4, f"got {len(chunks)}")
    check("memory chunks: total size", sum(len(c) for c in chunks) == 1000)
    check("memory chunks: last chunk smaller", len(chunks[-1]) == 100)


# ---------------------------------------------------------------------------
# 2. UploadedFile disk spill mode
# ---------------------------------------------------------------------------


def test_disk_spill_mode():
    print("\n-- UploadedFile disk spill mode --")
    # Write a temp file
    fd, path = tempfile.mkstemp(prefix="hyper_test_")
    try:
        os.write(fd, b"disk content here")
        os.close(fd)

        f = UploadedFile(
            filename="big.bin",
            content_type="application/octet-stream",
            _path=path,
            _size=17,
        )
        check("data reads from disk", f.data == b"disk content here")
        check("size from _size field", f.size == 17)
        check("in_memory is False", f.in_memory is False)
        check("path is set", f.path == path)
        check("repr shows disk", "disk" in repr(f))
    finally:
        _path = Path(path)
        if _path.exists():
            _path.unlink()


def test_disk_chunks():
    fd, path = tempfile.mkstemp(prefix="hyper_test_")
    try:
        data = b"X" * 500
        os.write(fd, data)
        os.close(fd)

        f = UploadedFile(
            filename="big.bin",
            content_type="application/octet-stream",
            _path=path,
            _size=500,
        )

        async def _collect():
            chunks = []
            async for chunk in f.chunks(chunk_size=200):
                chunks.append(chunk)
            return chunks

        chunks = _run(_collect())
        check("disk chunks: 3 chunks", len(chunks) == 3, f"got {len(chunks)}")
        check("disk chunks: total size", sum(len(c) for c in chunks) == 500)
    finally:
        _path = Path(path)
        if _path.exists():
            _path.unlink()


# ---------------------------------------------------------------------------
# 3. _parse_multipart disk spill based on FILE_UPLOAD_MAX_MEMORY_SIZE
# ---------------------------------------------------------------------------


def test_parse_multipart_small_in_memory():
    """Files smaller than threshold stay in memory."""
    print("\n-- _parse_multipart thresholds --")
    body = build_multipart(
        [
            ('form-data; name="small"; filename="tiny.txt"', b"tiny", "text/plain"),
        ]
    )
    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    with patch.dict(DEFAULTS, {"FILE_UPLOAD_MAX_MEMORY_SIZE": 1000}):
        req._parse_multipart()
        f = req._files["small"]
        check("small file in memory", f.in_memory is True)
        check("small file data", f.data == b"tiny")


def test_parse_multipart_large_spills_to_disk():
    """Files larger than threshold spill to disk."""
    large_data = b"X" * 5000
    body = build_multipart(
        [
            (
                'form-data; name="big"; filename="big.bin"',
                large_data,
                "application/octet-stream",
            ),
        ]
    )
    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    with patch.dict(
        DEFAULTS, {"FILE_UPLOAD_MAX_MEMORY_SIZE": 1000, "FILE_UPLOAD_MAX_SIZE": 0}
    ):
        req._parse_multipart()
        f = req._files["big"]
        check("large file on disk", f.in_memory is False)
        check("large file path exists", f.path is not None and Path(f.path).exists())
        check("large file data from disk", f.data == large_data)
        check("large file size", f.size == 5000)
        # Clean up temp file
        if f.path:
            Path(f.path).unlink()


def test_parse_multipart_max_size_enforced():
    """FILE_UPLOAD_MAX_SIZE rejects oversized files."""
    large_data = b"X" * 2000
    body = build_multipart(
        [
            (
                'form-data; name="huge"; filename="huge.bin"',
                large_data,
                "application/octet-stream",
            ),
        ]
    )
    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    with patch.dict(
        DEFAULTS, {"FILE_UPLOAD_MAX_MEMORY_SIZE": 10000, "FILE_UPLOAD_MAX_SIZE": 1000}
    ):
        try:
            req._parse_multipart()
            check("max size enforced", False, "should have raised")
        except Exception as e:
            check(
                "max size raises 413",
                "413" in str(type(e).__name__) or "exceeds" in str(e).lower(),
                str(e),
            )


def test_parse_multipart_mixed_modes():
    """Mixed small (memory) + large (disk) files in same request."""
    small_data = b"tiny"
    large_data = b"Y" * 3000
    body = build_multipart(
        [
            ('form-data; name="small"; filename="s.txt"', small_data, "text/plain"),
            (
                'form-data; name="large"; filename="l.bin"',
                large_data,
                "application/octet-stream",
            ),
            ('form-data; name="field"', b"value", None),
        ]
    )
    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    with patch.dict(
        DEFAULTS, {"FILE_UPLOAD_MAX_MEMORY_SIZE": 1000, "FILE_UPLOAD_MAX_SIZE": 0}
    ):
        req._parse_multipart()
        check("small in memory", req._files["small"].in_memory is True)
        check("large on disk", req._files["large"].in_memory is False)
        check("form field parsed", "field" in req._form)
        check("form field value", "value" in req._form["field"])
        # Clean up
        large = req._files["large"]
        if large.path:
            Path(large.path).unlink()


# ---------------------------------------------------------------------------
# 4. request.stream()
# ---------------------------------------------------------------------------


def test_stream_buffered_body():
    """request.stream() yields buffered body in chunks."""
    print("\n-- request.stream() --")
    body = b"A" * 1000
    req = Request(
        method="POST",
        path="/data",
        headers={"content-type": "application/octet-stream"},
        query_string="",
        body=body,
    )

    async def _collect():
        chunks = []
        async for chunk in req.stream(chunk_size=300):
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    check("stream: 4 chunks", len(chunks) == 4, f"got {len(chunks)}")
    check("stream: total bytes", b"".join(chunks) == body)


def test_stream_empty_body():
    """request.stream() on empty body yields nothing."""
    req = Request(
        method="POST",
        path="/data",
        headers={},
        query_string="",
        body=b"",
    )

    async def _collect():
        chunks = []
        async for chunk in req.stream():
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    check("stream empty: 0 chunks", len(chunks) == 0)


def test_stream_single_chunk():
    """Small body yields as single chunk."""
    req = Request(
        method="POST",
        path="/data",
        headers={},
        query_string="",
        body=b"small",
    )

    async def _collect():
        chunks = []
        async for chunk in req.stream(chunk_size=65536):
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    check("stream small: 1 chunk", len(chunks) == 1)
    check("stream small: correct data", chunks[0] == b"small")


# ---------------------------------------------------------------------------
# 5. Settings exist
# ---------------------------------------------------------------------------


def test_settings_exist():
    print("\n-- Settings --")
    for name in [
        "FILE_UPLOAD_MAX_MEMORY_SIZE",
        "FILE_UPLOAD_MAX_SIZE",
        "STREAM_BODY_CHUNK_SIZE",
    ]:
        check(f"{name} in DEFAULTS", name in DEFAULTS)
        check(f"{name} in SETTING_DEFINITIONS", name in SETTING_DEFINITIONS)


def test_settings_defaults():
    check(
        "FILE_UPLOAD_MAX_MEMORY_SIZE default = 2621440",
        DEFAULTS["FILE_UPLOAD_MAX_MEMORY_SIZE"] == 2621440,
    )
    check("FILE_UPLOAD_MAX_SIZE default = 0", DEFAULTS["FILE_UPLOAD_MAX_SIZE"] == 0)
    check(
        "STREAM_BODY_CHUNK_SIZE default = 262144",
        DEFAULTS["STREAM_BODY_CHUNK_SIZE"] == 262144,
    )


# ---------------------------------------------------------------------------
# 6. Hypothesis fuzz: random file sizes and thresholds
# ---------------------------------------------------------------------------


@given(
    file_size=st.integers(min_value=0, max_value=10000),
    threshold=st.integers(min_value=100, max_value=5000),
)
@settings(max_examples=100, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_spill_threshold(file_size, threshold):
    """Files correctly spill to disk based on threshold."""
    data = b"X" * file_size
    body = build_multipart(
        [
            ('form-data; name="f"; filename="f.bin"', data, "application/octet-stream"),
        ]
    )
    req = Request(
        method="POST",
        path="/",
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        query_string="",
        body=body,
    )
    with patch.dict(
        DEFAULTS, {"FILE_UPLOAD_MAX_MEMORY_SIZE": threshold, "FILE_UPLOAD_MAX_SIZE": 0}
    ):
        req._parse_multipart()
        f = req._files.get("f")
        if f is not None:
            if file_size <= threshold:
                assert f.in_memory, f"Expected memory for {file_size} <= {threshold}"
            else:
                assert not f.in_memory, f"Expected disk for {file_size} > {threshold}"
                assert f.path is not None
            assert f.data == data
            # Clean up temp files
            if f.path and Path(f.path).exists():
                Path(f.path).unlink()


@given(
    data=st.binary(min_size=1, max_size=5000),
    chunk_size=st.integers(min_value=1, max_value=2000),
)
@settings(max_examples=50, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_chunks_roundtrip(data, chunk_size):
    """UploadedFile.chunks() always reconstructs the original data."""
    f = UploadedFile(
        filename="f.bin", content_type="application/octet-stream", _data=data
    )

    async def _collect():
        chunks = []
        async for chunk in f.chunks(chunk_size=chunk_size):
            chunks.append(chunk)
        return b"".join(chunks)

    result = asyncio.run(_collect())
    assert result == data


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- Three-Mode Upload Tests --\n")

    test_memory_mode()
    test_memory_chunks()
    test_disk_spill_mode()
    test_disk_chunks()
    test_parse_multipart_small_in_memory()
    test_parse_multipart_large_spills_to_disk()
    test_parse_multipart_max_size_enforced()
    test_parse_multipart_mixed_modes()
    test_stream_buffered_body()
    test_stream_empty_body()
    test_stream_single_chunk()
    test_settings_exist()
    test_settings_defaults()

    # Hypothesis fuzz
    print("\n-- Hypothesis fuzz --")
    fuzz_tests = [
        ("fuzz: spill threshold", test_fuzz_spill_threshold),
        ("fuzz: chunks roundtrip", test_fuzz_chunks_roundtrip),
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
    print(f"Upload modes: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
