"""
Tests for native Zig static file helpers.

Tests:
1. _hash_file_md5 produces correct MD5 hex digest
2. _file_read_with_hash returns (content, hash) tuple
3. Results match Python's hashlib.md5
4. Error handling for missing files
5. Performance comparison vs Python

Usage:
    uv run hyper-test native_static
"""

# hyper-test: unit

import asyncio
import hashlib
import inspect
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from hyperdjango._hyperdjango_native import (
    _file_read_with_hash,
    _hash_file_md5,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def create_temp_file(content):
    """Create a temp file with content, return absolute path."""
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as f:
        if isinstance(content, str):
            f.write(content.encode())
        else:
            f.write(content)
    return path


# ---------------------------------------------------------------------------
# _hash_file_md5
# ---------------------------------------------------------------------------


@test("hash_file_md5: matches Python hashlib for text file")
def test_hash_text():
    content = b"Hello, World! This is a test file for MD5 hashing."
    path = create_temp_file(content)
    try:
        native_hash = _hash_file_md5(path)
        python_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
        assert native_hash == python_hash, f"Mismatch: {native_hash} != {python_hash}"
    finally:
        Path(path).unlink()


@test("hash_file_md5: matches for binary file")
def test_hash_binary():
    content = bytes(range(256)) * 100  # 25.6KB of all byte values
    path = create_temp_file(content)
    try:
        native_hash = _hash_file_md5(path)
        python_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
        assert native_hash == python_hash
    finally:
        Path(path).unlink()


@test("hash_file_md5: returns 32-char lowercase hex")
def test_hash_format():
    path = create_temp_file(b"test")
    try:
        result = _hash_file_md5(path)
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)
    finally:
        Path(path).unlink()


@test("hash_file_md5: empty file")
def test_hash_empty():
    path = create_temp_file(b"")
    try:
        native_hash = _hash_file_md5(path)
        python_hash = hashlib.md5(b"", usedforsecurity=False).hexdigest()
        assert native_hash == python_hash
    finally:
        Path(path).unlink()


@test("hash_file_md5: missing file raises FileNotFoundError")
def test_hash_missing():
    try:
        _hash_file_md5("/nonexistent/path/to/file.txt")
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# _file_read_with_hash
# ---------------------------------------------------------------------------


@test("file_read_with_hash: returns (bytes, str) tuple")
def test_read_hash_types():
    content = b"test content for read+hash"
    path = create_temp_file(content)
    try:
        result = _file_read_with_hash(path)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bytes)
        assert isinstance(result[1], str)
    finally:
        Path(path).unlink()


@test("file_read_with_hash: content matches file")
def test_read_hash_content():
    content = b"exact content check"
    path = create_temp_file(content)
    try:
        data, hash_str = _file_read_with_hash(path)
        assert data == content
    finally:
        Path(path).unlink()


@test("file_read_with_hash: hash matches hashlib")
def test_read_hash_matches():
    content = b"hash verification test" * 1000
    path = create_temp_file(content)
    try:
        data, hash_str = _file_read_with_hash(path)
        expected = hashlib.md5(content, usedforsecurity=False).hexdigest()
        assert hash_str == expected
        assert data == content
    finally:
        Path(path).unlink()


@test("file_read_with_hash: missing file raises FileNotFoundError")
def test_read_hash_missing():
    try:
        _file_read_with_hash("/nonexistent/file.txt")
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Integration with staticfiles
# ---------------------------------------------------------------------------


@test("staticfiles: native read_with_hash is available")
def test_staticfiles_integration():
    from hyperdjango._hyperdjango_native import (
        _file_read_with_hash,  # noqa: F401
    )


# ---------------------------------------------------------------------------
# Performance benchmark
# ---------------------------------------------------------------------------


@test("benchmark: native vs Python file hash (100KB file)")
def test_benchmark():
    content = os.urandom(100 * 1024)  # 100KB
    path = create_temp_file(content)

    try:
        # Warmup
        p = Path(path)
        for _ in range(5):
            _hash_file_md5(path)
            hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest()

        # Benchmark native
        iterations = 500
        start = time.perf_counter()
        for _ in range(iterations):
            _hash_file_md5(path)
        native_time = time.perf_counter() - start

        # Benchmark Python (read + hash separately)
        start = time.perf_counter()
        for _ in range(iterations):
            data = p.read_bytes()
            hashlib.md5(data, usedforsecurity=False).hexdigest()
        python_time = time.perf_counter() - start

        native_us = native_time / iterations * 1_000_000
        python_us = python_time / iterations * 1_000_000
        speedup = python_time / native_time

        print(
            f"\n    Native: {native_us:.1f} µs/op, Python: {python_us:.1f} µs/op, speedup: {speedup:.2f}x"
        )

        # Note: Python's hashlib.md5 uses OpenSSL's ASM-optimized implementation.
        # Zig's std.crypto.Md5 is pure Zig. The native version's value is in
        # eliminating the double-read pattern (read once, get both content + hash),
        # not in raw hash speed. The combined _file_read_with_hash is the real win.

    finally:
        Path(path).unlink()


@test("benchmark: native read_with_hash vs Python read + hash (100KB)")
def test_benchmark_combined():
    content = os.urandom(100 * 1024)
    path = create_temp_file(content)

    try:
        iterations = 500
        p = Path(path)

        # Native: single call for read + hash
        start = time.perf_counter()
        for _ in range(iterations):
            _file_read_with_hash(path)
        native_time = time.perf_counter() - start

        # Python: separate read + hash
        start = time.perf_counter()
        for _ in range(iterations):
            data = p.read_bytes()
            hashlib.md5(data, usedforsecurity=False).hexdigest()[:12]
        python_time = time.perf_counter() - start

        native_us = native_time / iterations * 1_000_000
        python_us = python_time / iterations * 1_000_000
        speedup = python_time / native_time

        print(
            f"\n    Native read+hash: {native_us:.1f} µs/op, Python: {python_us:.1f} µs/op, speedup: {speedup:.2f}x"
        )

    finally:
        Path(path).unlink()


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nNative Static File Helpers Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
