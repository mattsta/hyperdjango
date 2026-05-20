"""Tests that the native server rejects malformed or duplicate Content-Length.

A non-numeric / negative / overflowing Content-Length, or two conflicting
Content-Length headers, must be answered with 400 — not silently treated as an
empty body, and not a length disagreement that enables request smuggling. A
well-formed Content-Length is parsed normally.

NOTE: drives the live Zig HTTP server, so it requires the rebuilt extension
(`uv run hyper-build`).

Usage:
    uv run hyper-test content_length_hardening
"""

# hyper-test: e2e

import socket
import sys

from e2e_helper import TEST_PORTS, AppRunner

PASS = 0
FAIL = 0
PORT = TEST_PORTS["content_length"]


def _raw_status(host: str, port: int, raw: bytes) -> int | None:
    """Send raw HTTP bytes; return the response status code (or None)."""
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(raw)
        s.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            try:
                chunk = s.recv(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            data += chunk
    if not data:
        return None
    parts = data.split(b"\r\n", 1)[0].split(b" ")
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None


def _check(name: str, got: object, expected: object) -> None:
    global PASS, FAIL
    if got == expected:
        PASS += 1
        print(f"  PASS  {name} ({got})")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: expected {expected}, got {got}")


def main() -> bool:
    print("=" * 60)
    print("Content-Length hardening")
    print("=" * 60)

    with AppRunner(module_app="services._raw_http_fixture:app", port=PORT) as r:
        host, port = r.host, r.port

        malformed = (
            b"POST /echo HTTP/1.1\r\nHost: t\r\n"
            b"Content-Length: abc\r\nConnection: close\r\n\r\n"
        )
        _check(
            "malformed Content-Length -> 400",
            _raw_status(host, port, malformed),
            400,
        )

        duplicate = (
            b"POST /echo HTTP/1.1\r\nHost: t\r\n"
            b"Content-Length: 5\r\nContent-Length: 6\r\nConnection: close\r\n\r\nhello"
        )
        _check(
            "duplicate Content-Length -> 400",
            _raw_status(host, port, duplicate),
            400,
        )

        # A well-formed Content-Length is parsed and the body-reading handler runs.
        valid = (
            b"POST /echo HTTP/1.1\r\nHost: t\r\n"
            b"Content-Length: 5\r\nConnection: close\r\n\r\nhello"
        )
        _check(
            "valid Content-Length -> handler runs (200)",
            _raw_status(host, port, valid),
            200,
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
