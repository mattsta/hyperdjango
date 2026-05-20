"""
Tests for true streaming body reader (socket → Python → forward).

# hyper-test: unit

Validates:
1. request.stream() yields buffered body in chunks (small bodies)
2. request.stream() on empty body yields nothing
3. UploadedFile.chunks() for memory + disk modes
4. _stream_content_length field wiring
5. _read_body_chunk FFI function exists and callable

Usage:
    uv run hyper-test streaming_body
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from hyperdjango._hyperdjango_native import _read_body_chunk

from hyperdjango.request import Request, UploadedFile

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


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. request.stream() buffered fallback (small bodies, TestClient)
# ---------------------------------------------------------------------------


def test_stream_buffered_chunks():
    """Buffered body yields in chunks of configured size."""
    print("\n-- request.stream() buffered --")
    body = b"A" * 2000
    req = Request(
        method="POST",
        path="/upload",
        headers={"content-type": "application/octet-stream"},
        query_string="",
        body=body,
    )

    async def _collect():
        chunks = []
        async for chunk in req.stream(chunk_size=500):
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    check("buffered: 4 chunks", len(chunks) == 4, f"got {len(chunks)}")
    check("buffered: correct data", b"".join(chunks) == body)
    check("buffered: each chunk <=500", all(len(c) <= 500 for c in chunks))


def test_stream_empty():
    """Empty body yields nothing."""
    req = Request(
        method="POST",
        path="/upload",
        headers={},
        query_string="",
        body=b"",
    )

    async def _collect():
        chunks = []
        async for chunk in req.stream():
            chunks.append(chunk)
        return chunks

    check("empty: 0 chunks", len(_run(_collect())) == 0)


def test_stream_single_chunk():
    """Small body yields as single chunk."""
    req = Request(
        method="POST",
        path="/upload",
        headers={},
        query_string="",
        body=b"tiny",
    )

    async def _collect():
        chunks = []
        async for chunk in req.stream(chunk_size=65536):
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    check("single: 1 chunk", len(chunks) == 1)
    check("single: correct data", chunks[0] == b"tiny")


# ---------------------------------------------------------------------------
# 2. _stream_content_length field
# ---------------------------------------------------------------------------


def test_stream_content_length_default():
    """_stream_content_length defaults to 0."""
    print("\n-- _stream_content_length --")
    req = Request(
        method="POST",
        path="/upload",
        headers={},
        query_string="",
        body=b"data",
    )
    check("default is 0", req._stream_content_length == 0)


def test_stream_content_length_set():
    """_stream_content_length can be set for streaming mode."""
    req = Request(
        method="POST",
        path="/upload",
        headers={},
        query_string="",
        body=b"",
    )
    req._stream_content_length = 1048576  # 1MB
    check("set to 1MB", req._stream_content_length == 1048576)


# ---------------------------------------------------------------------------
# 3. _read_body_chunk FFI exists
# ---------------------------------------------------------------------------


def test_read_body_chunk_callable():
    """_read_body_chunk FFI function exists and is callable."""
    print("\n-- _read_body_chunk FFI --")
    check("_read_body_chunk callable", callable(_read_body_chunk))

    # When no streaming state is active, should return empty bytes
    result = _read_body_chunk(256)
    check("no active stream: empty bytes", result == b"", f"got {result!r}")


# ---------------------------------------------------------------------------
# 4. UploadedFile.chunks() for both modes
# ---------------------------------------------------------------------------


def test_uploaded_file_memory_chunks():
    """Memory-mode chunks yield correct data."""
    print("\n-- UploadedFile.chunks() --")
    f = UploadedFile(
        filename="f.bin", content_type="application/octet-stream", _data=b"ABCDE" * 200
    )

    async def _collect():
        return [c async for c in f.chunks(chunk_size=300)]

    chunks = _run(_collect())
    check("memory chunks: correct total", sum(len(c) for c in chunks) == 1000)


def test_uploaded_file_disk_chunks():
    """Disk-mode chunks yield correct data."""
    fd, path = tempfile.mkstemp(prefix="hyper_test_")
    try:
        data = b"Z" * 800
        os.write(fd, data)
        os.close(fd)

        f = UploadedFile(
            filename="f.bin",
            content_type="application/octet-stream",
            _path=path,
            _size=800,
        )

        async def _collect():
            return [c async for c in f.chunks(chunk_size=300)]

        chunks = _run(_collect())
        check("disk chunks: correct total", sum(len(c) for c in chunks) == 800)
        check("disk chunks: correct data", b"".join(chunks) == data)
    finally:
        _path = Path(path)
        if _path.exists():
            _path.unlink()


# ---------------------------------------------------------------------------
# 5. Streaming body with stream() when _stream_content_length is set
#    (but no actual socket — verifies the branching logic)
# ---------------------------------------------------------------------------


def test_stream_with_content_length_no_socket():
    """When _stream_content_length > 0 but no active Zig socket, returns empty."""
    print("\n-- stream() with content_length but no socket --")
    req = Request(
        method="POST",
        path="/upload",
        headers={},
        query_string="",
        body=b"",
    )
    req._stream_content_length = 5000  # Simulates streaming mode

    async def _collect():
        chunks = []
        async for chunk in req.stream(chunk_size=1000):
            chunks.append(chunk)
        return chunks

    chunks = _run(_collect())
    # No actual socket → _read_body_chunk returns empty → yields nothing
    check("no socket: 0 chunks", len(chunks) == 0)


# ---------------------------------------------------------------------------
# 6. Hypothesis fuzz: stream() roundtrip, chunks boundary, hostile inputs
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 1000
_SUPPRESS = [HealthCheck.too_slow] if _PARALLEL else []


@given(
    data=st.binary(min_size=0, max_size=10000),
    chunk_size=st.integers(min_value=1, max_value=5000),
)
@hsettings(max_examples=200, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_stream_roundtrip(data, chunk_size):
    """request.stream() always reconstructs the original body."""
    req = Request(
        method="POST",
        path="/",
        headers={},
        query_string="",
        body=data,
    )

    async def _collect():
        return b"".join([c async for c in req.stream(chunk_size=chunk_size)])

    assert asyncio.run(_collect()) == data


@given(
    data=st.binary(min_size=0, max_size=5000),
    chunk_size=st.integers(min_value=1, max_value=3000),
)
@hsettings(max_examples=200, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_uploaded_file_memory_chunks_roundtrip(data, chunk_size):
    """UploadedFile memory chunks always reconstruct the original data."""
    f = UploadedFile(filename="f", content_type="application/octet-stream", _data=data)

    async def _collect():
        return b"".join([c async for c in f.chunks(chunk_size=chunk_size)])

    assert asyncio.run(_collect()) == data


@given(
    data=st.binary(min_size=1, max_size=5000),
    chunk_size=st.integers(min_value=1, max_value=3000),
)
@hsettings(max_examples=100, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_uploaded_file_disk_chunks_roundtrip(data, chunk_size):
    """UploadedFile disk chunks always reconstruct the original data."""
    fd, path = tempfile.mkstemp(prefix="hyper_fuzz_")
    try:
        os.write(fd, data)
        os.close(fd)
        f = UploadedFile(
            filename="f",
            content_type="application/octet-stream",
            _path=path,
            _size=len(data),
        )

        async def _collect():
            return b"".join([c async for c in f.chunks(chunk_size=chunk_size)])

        assert asyncio.run(_collect()) == data
    finally:
        _path = Path(path)
        if _path.exists():
            _path.unlink()


@given(chunk_size=st.integers(min_value=1, max_value=100000))
@hsettings(max_examples=50, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_stream_chunk_count(chunk_size):
    """Number of chunks equals ceil(len/chunk_size) for non-empty bodies."""
    body = b"X" * 997  # prime number, tests boundary alignment
    req = Request(
        method="POST",
        path="/",
        headers={},
        query_string="",
        body=body,
    )

    async def _count():
        return sum(1 for _ in [c async for c in req.stream(chunk_size=chunk_size)])

    count = asyncio.run(_count())
    expected = (len(body) + chunk_size - 1) // chunk_size
    assert count == expected, (
        f"chunk_size={chunk_size}: expected {expected}, got {count}"
    )


@given(
    filename=st.text(
        min_size=1,
        max_size=200,
        alphabet=st.characters(
            whitelist_categories=("L", "N"), whitelist_characters="._- "
        ),
    ),
    content_type=st.text(
        min_size=1,
        max_size=100,
        alphabet=st.characters(
            whitelist_categories=("L", "N"), whitelist_characters="/-"
        ),
    ),
    data=st.binary(min_size=0, max_size=1000),
)
@hsettings(max_examples=100, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_uploaded_file_properties(filename, content_type, data):
    """UploadedFile properties are consistent for arbitrary inputs."""
    f = UploadedFile(filename=filename, content_type=content_type, _data=data)
    assert f.filename == filename
    assert f.content_type == content_type
    assert f.data == data
    assert f.size == len(data)
    assert f.in_memory is True
    assert f.path is None
    assert "memory" in repr(f)


@given(data=st.binary(min_size=0, max_size=1000))
@hsettings(max_examples=50, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_uploaded_file_disk_properties(data):
    """Disk-mode UploadedFile properties are consistent."""
    fd, path = tempfile.mkstemp(prefix="hyper_fuzz_")
    try:
        os.write(fd, data)
        os.close(fd)
        f = UploadedFile(
            filename="f.bin",
            content_type="application/octet-stream",
            _path=path,
            _size=len(data),
        )
        assert f.data == data
        assert f.size == len(data)
        assert f.in_memory is False
        assert f.path == path
        assert "disk" in repr(f)
    finally:
        _path = Path(path)
        if _path.exists():
            _path.unlink()


def test_uploaded_file_no_data_no_path():
    """UploadedFile with neither _data nor _path raises on .data access."""
    print("\n-- Hostile inputs --")
    f = UploadedFile(filename="f", content_type="text/plain")
    try:
        _ = f.data
        check("no data/path raises ValueError", False, "should have raised")
    except ValueError:
        check("no data/path raises ValueError", True)


def test_uploaded_file_zero_byte():
    """Zero-byte file works in both modes."""
    # Memory
    f_mem = UploadedFile(filename="empty.txt", content_type="text/plain", _data=b"")
    check("zero-byte memory: data is empty", f_mem.data == b"")
    check("zero-byte memory: size is 0", f_mem.size == 0)

    # Disk
    fd, path = tempfile.mkstemp(prefix="hyper_test_")
    os.close(fd)  # empty file
    try:
        f_disk = UploadedFile(
            filename="empty.txt", content_type="text/plain", _path=path, _size=0
        )
        check("zero-byte disk: data is empty", f_disk.data == b"")
        check("zero-byte disk: size is 0", f_disk.size == 0)
    finally:
        _path = Path(path)
        if _path.exists():
            _path.unlink()


def test_read_body_chunk_zero_size():
    """_read_body_chunk with 0 max_bytes returns empty."""
    result = _read_body_chunk(0)
    check("chunk size 0: empty bytes", result == b"")


def test_read_body_chunk_negative():
    """_read_body_chunk with negative max_bytes returns empty."""
    result = _read_body_chunk(-1)
    check("chunk size -1: empty bytes", len(result) == 0)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- Streaming Body Tests --\n")

    # Unit tests
    test_stream_buffered_chunks()
    test_stream_empty()
    test_stream_single_chunk()
    test_stream_content_length_default()
    test_stream_content_length_set()
    test_read_body_chunk_callable()
    test_uploaded_file_memory_chunks()
    test_uploaded_file_disk_chunks()
    test_stream_with_content_length_no_socket()

    # Hostile inputs
    test_uploaded_file_no_data_no_path()
    test_uploaded_file_zero_byte()
    test_read_body_chunk_zero_size()
    test_read_body_chunk_negative()

    # Hypothesis fuzz
    print("\n-- Hypothesis fuzz --")
    fuzz_tests = [
        ("fuzz: stream roundtrip", test_fuzz_stream_roundtrip),
        (
            "fuzz: memory chunks roundtrip",
            test_fuzz_uploaded_file_memory_chunks_roundtrip,
        ),
        ("fuzz: disk chunks roundtrip", test_fuzz_uploaded_file_disk_chunks_roundtrip),
        ("fuzz: stream chunk count", test_fuzz_stream_chunk_count),
        ("fuzz: uploaded file properties", test_fuzz_uploaded_file_properties),
        ("fuzz: disk file properties", test_fuzz_uploaded_file_disk_properties),
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
            import traceback

            traceback.print_exc()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Streaming body: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
