#!/usr/bin/env python3
"""Test SIMD-accelerated multipart boundary scanning.

Tests:
1. Simple two-field multipart
2. File upload with binary data
3. Large body (SIMD path exercised)
4. Multiple files
5. Empty body / missing boundary
6. Performance benchmark: small vs large bodies
"""

# hyper-test: unit

import sys
import time

from hyperdjango._hyperdjango_native import parse_multipart_native


def make_multipart(parts, boundary="abc123"):
    """Build a multipart/form-data body from a list of (name, filename, content_type, data) tuples."""
    chunks = []
    for name, filename, ct, data in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        if filename:
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            chunks.append(f"Content-Type: {ct}\r\n".encode())
        else:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n'.encode())
        chunks.append(b"\r\n")
        if isinstance(data, str):
            data = data.encode()
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Test 1: Simple two fields ─────────────────────────────────────────
    print("\n=== Test 1: Simple two fields ===")
    body = make_multipart(
        [
            ("field1", None, "", "value1"),
            ("field2", None, "", "value2"),
        ]
    )
    result = parse_multipart_native(body, "abc123")
    check("two parts returned", len(result) == 2, f"got {len(result)}")
    check("field1 name", result[0][0] == "field1")
    check("field1 data", result[0][3] == b"value1")
    check("field2 name", result[1][0] == "field2")
    check("field2 data", result[1][3] == b"value2")

    # ── Test 2: File upload ───────────────────────────────────────────────
    print("\n=== Test 2: File upload ===")
    body = make_multipart(
        [
            ("file", "test.txt", "text/plain", "hello world"),
        ]
    )
    result = parse_multipart_native(body, "abc123")
    check("one part", len(result) == 1)
    check("filename present", result[0][1] == "test.txt")
    check("content_type text/plain", result[0][2] == "text/plain")
    check("file data correct", result[0][3] == b"hello world")

    # ── Test 3: Binary data ───────────────────────────────────────────────
    print("\n=== Test 3: Binary data ===")
    binary_data = bytes(range(256)) * 10  # 2560 bytes of binary
    body = make_multipart(
        [
            ("binfile", "data.bin", "application/octet-stream", binary_data),
        ]
    )
    result = parse_multipart_native(body, "abc123")
    check("binary part returned", len(result) == 1)
    check(
        "binary data intact",
        result[0][3] == binary_data,
        f"len={len(result[0][3])} expected={len(binary_data)}",
    )

    # ── Test 4: Large body (exercises SIMD path) ──────────────────────────
    print("\n=== Test 4: Large body (SIMD path) ===")
    large_data = b"X" * 100_000  # 100KB
    body = make_multipart(
        [
            ("small", None, "", "hello"),
            ("large", "big.dat", "application/octet-stream", large_data),
            ("after", None, "", "still works"),
        ]
    )
    result = parse_multipart_native(body, "abc123")
    check("three parts from large body", len(result) == 3)
    check("large data size", len(result[1][3]) == 100_000, f"got {len(result[1][3])}")
    check("large data correct", result[1][3] == large_data)
    check("part after large still works", result[2][3] == b"still works")

    # ── Test 5: Multiple files ────────────────────────────────────────────
    print("\n=== Test 5: Multiple files ===")
    body = make_multipart(
        [
            ("file1", "a.txt", "text/plain", "aaa"),
            ("file2", "b.jpg", "image/jpeg", bytes([0xFF, 0xD8, 0xFF, 0xE0])),
            ("file3", "c.pdf", "application/pdf", b"%PDF-1.4"),
            ("name", None, "", "form value"),
        ]
    )
    result = parse_multipart_native(body, "abc123")
    check("four parts", len(result) == 4)
    check("jpg data", result[1][3] == bytes([0xFF, 0xD8, 0xFF, 0xE0]))
    check("pdf data", result[2][3] == b"%PDF-1.4")

    # ── Test 6: Empty / edge cases ────────────────────────────────────────
    print("\n=== Test 6: Edge cases ===")
    empty_result = parse_multipart_native(b"", "abc123")
    check("empty body returns empty list", len(empty_result) == 0)

    no_boundary = parse_multipart_native(b"just some random data", "notfound")
    check("no boundary returns empty list", len(no_boundary) == 0)

    # ── Test 7: Performance benchmark ─────────────────────────────────────
    print("\n=== Test 7: Performance benchmark ===")

    # Small body (< 64 bytes, non-SIMD path)
    small_body = make_multipart([("x", None, "", "val")])
    iterations = 50_000
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        parse_multipart_native(small_body, "abc123")
    small_ns = (time.perf_counter_ns() - t0) / iterations

    # Medium body (~1KB)
    medium_body = make_multipart(
        [("data", "f.bin", "application/octet-stream", b"Y" * 1000)]
    )
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        parse_multipart_native(medium_body, "abc123")
    medium_ns = (time.perf_counter_ns() - t0) / iterations

    # Large body (~100KB, SIMD path)
    large_body = make_multipart(
        [("data", "f.bin", "application/octet-stream", b"Z" * 100_000)]
    )
    large_iterations = 5_000
    t0 = time.perf_counter_ns()
    for _ in range(large_iterations):
        parse_multipart_native(large_body, "abc123")
    large_ns = (time.perf_counter_ns() - t0) / large_iterations

    print(f"  Small (~100B):  {small_ns / 1000:.1f} μs/parse")
    print(f"  Medium (~1KB):  {medium_ns / 1000:.1f} μs/parse")
    print(
        f"  Large (~100KB): {large_ns / 1000:.1f} μs/parse ({100_000 / (large_ns / 1e9) / 1e6:.0f} MB/s)"
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All multipart SIMD tests passed!")


if __name__ == "__main__":
    main()
