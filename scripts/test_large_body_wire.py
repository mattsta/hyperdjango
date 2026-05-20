"""Byte-exactness gate for large response bodies over the native Zig server.

The response body a handler returns is COPIED out of its PyBytes in
``server.zig:callPythonHandler`` (``allocator.dupe(u8, body_slice)``) and freed
in ``PythonResponse.deinit`` after the send, because the send runs with the GIL
released and must outlive every Python object in that frame. Anything that
touches that ownership transfer — a per-thread retained buffer, a resize, a
zero-copy attempt, a change to the send writers' chunking — can silently
truncate, alias, or stale-read a large body in ways a small-body test never
catches (a 21-byte body fits in the first write; a 1 MiB body does not).

This gate drives RAW HTTP bytes over a real socket against a live server and
asserts the exact bytes on the wire for a body-size ladder that brackets every
interesting threshold: 0, 1, 4 KiB, 64 KiB (the benchmark's large cell),
256 KiB (past glibc's 128 KiB mmap threshold), 1 MiB. Bodies use a
NON-REPEATING deterministic pattern, so a copy that duplicates a block, drops a
block, or starts at the wrong offset fails the comparison — a repeating filler
like b"x" * n would pass all three of those bugs.

Covers, per size:
  * GET      → 200, Content-Length == n, body byte-exact.
  * HEAD     → identical Content-Length, ZERO body bytes (RFC 7230 §3.3.3).
  * 304      → no body, correct ETag round-trip.
  * keep-alive → two large GETs back to back on ONE connection stay framed and
    byte-exact (a partial-write/desync bug shows up only on the second).

Run via:  uv run hyper-test large_body_wire
"""

# hyper-test: e2e

import hashlib
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_helper import AppRunner  # noqa: E402

from hyperdjango import HyperApp, Response  # noqa: E402

PORT = 19187

SIZES = (0, 1, 4096, 65536, 262144, 1048576)


def make_body(n: int) -> bytes:
    """Deterministic, non-repeating-at-block-granularity payload of exactly n
    bytes. Each 16-byte group encodes its own offset, so a duplicated,
    dropped, or misaligned block is detectable by comparison (and by the
    offset printed in the failure detail)."""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += b"%015x\n" % (i & 0xFFFFFFFFFFFFFFF)
        i += 1
    return bytes(out[:n])


_BODIES: dict[int, bytes] = {n: make_body(n) for n in SIZES}

# ── App under test ───────────────────────────────────────────────────────────
app = HyperApp(title="large-body-wire-fixture")


@app.route("/body/{n}", methods=["GET", "HEAD"])
async def body(request, n):
    size = int(n)
    return Response(
        body=make_body(size),
        status=200,
        content_type="application/octet-stream",
    )


@app.route("/etagbody/{n}", methods=["GET", "HEAD"])
async def etagbody(request, n):
    size = int(n)
    resp = Response(
        body=make_body(size), status=200, content_type="application/octet-stream"
    )
    resp.set_etag(f"lb-{size}")
    resp.check_not_modified(request)
    return resp


PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f" ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail or 'condition false'}")


def first_diff(a: bytes, b: bytes) -> str:
    """Where two bodies diverge — the diagnostic that separates 'truncated' from
    'wrong block' from 'off-by-N offset'."""
    if a == b:
        return "identical"
    if len(a) != len(b):
        return f"length {len(a)} != {len(b)}"
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        if x != y:
            return f"first diff at byte {i}: {a[i : i + 16]!r} != {b[i : i + 16]!r}"
    return "equal-length, no diff found"


def read_framed(sock: socket.socket, deadline: float = 20.0, expect_body: bool = True):
    """Read exactly ONE Content-Length-framed response, leaving anything after
    it in the returned remainder so keep-alive framing can be verified.

    ``expect_body=False`` is REQUIRED for HEAD: a HEAD response carries the
    Content-Length a GET would have but zero body bytes, so framing the read on
    Content-Length would block forever waiting for bytes the RFC forbids. That
    distinction is exactly what this gate is here to assert, so the reader must
    not assume it away."""
    sock.settimeout(deadline)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None, [], b"", b""
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ")
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    headers = []
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers.append((k.strip().lower(), v.strip()))
    cl = None
    for k, v in headers:
        if k == b"content-length":
            cl = int(v)
    if cl is None or not expect_body:
        return status, headers, b"", rest
    while len(rest) < cl:
        chunk = sock.recv(65536)
        if not chunk:
            break
        rest += chunk
    return status, headers, rest[:cl], rest[cl:]


def request_on(sock: socket.socket, method: str, path: str, extra: str = ""):
    sock.sendall(
        f"{method} {path} HTTP/1.1\r\nHost: t\r\n{extra}".encode()
        if extra
        else f"{method} {path} HTTP/1.1\r\nHost: t\r\n\r\n".encode()
    )
    return read_framed(sock, expect_body=(method != "HEAD"))


def connect(host: str, port: int) -> socket.socket:
    s = socket.create_connection((host, port), timeout=20)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def run(host: str, port: int) -> None:
    for n in SIZES:
        expected = _BODIES[n]
        label = f"{n}B" if n < 1024 else f"{n // 1024}KiB"

        # 1. GET — byte-exact body of the declared length.
        with connect(host, port) as s:
            status, headers, got, _ = request_on(s, "GET", f"/body/{n}")
        check(f"GET {label} status 200", status == 200, str(status))
        cl = [v for k, v in headers if k == b"content-length"]
        check(
            f"GET {label} Content-Length == {n}",
            cl == [str(n).encode()],
            f"{cl!r}",
        )
        check(
            f"GET {label} body byte-exact",
            got == expected,
            first_diff(expected, got),
        )
        check(
            f"GET {label} sha256 matches",
            hashlib.sha256(got).hexdigest() == hashlib.sha256(expected).hexdigest(),
            hashlib.sha256(got).hexdigest()[:16],
        )

        # 2. HEAD — same framing, ZERO body bytes.
        with connect(host, port) as s:
            h_status, h_headers, h_body, h_rest = request_on(s, "HEAD", f"/body/{n}")
        h_cl = [v for k, v in h_headers if k == b"content-length"]
        check(f"HEAD {label} status 200", h_status == 200, str(h_status))
        check(
            f"HEAD {label} Content-Length matches GET",
            h_cl == cl,
            f"{h_cl!r} vs {cl!r}",
        )
        check(
            f"HEAD {label} emits ZERO body bytes",
            h_body == b"" and h_rest == b"",
            f"{len(h_body)} body + {len(h_rest)} trailing",
        )

        # 3. 304 — ETag round-trip carries no body.
        with connect(host, port) as s:
            e_status, _, e_body, _ = request_on(s, "GET", f"/etagbody/{n}")
        check(f"ETag {label} first GET 200", e_status == 200, str(e_status))
        check(
            f"ETag {label} first GET body byte-exact",
            e_body == expected,
            first_diff(expected, e_body),
        )
        with connect(host, port) as s:
            nm_status, _, nm_body, nm_rest = request_on(
                s, "GET", f"/etagbody/{n}", extra=f'If-None-Match: "lb-{n}"\r\n\r\n'
            )
        check(f"304 {label} status", nm_status == 304, str(nm_status))
        check(
            f"304 {label} carries no body",
            nm_body == b"" and nm_rest == b"",
            f"{len(nm_body)} + {len(nm_rest)}",
        )

    # 4. Keep-alive: two LARGE bodies back to back on one connection. A partial
    # write or a body buffer reused before the send completed desyncs here and
    # nowhere else.
    for n in (65536, 262144, 1048576):
        label = f"{n // 1024}KiB"
        with connect(host, port) as s:
            s1, _, b1, rest1 = request_on(s, "GET", f"/body/{n}")
            check(f"keep-alive {label} #1 status", s1 == 200, str(s1))
            check(
                f"keep-alive {label} #1 byte-exact",
                b1 == _BODIES[n],
                first_diff(_BODIES[n], b1),
            )
            check(
                f"keep-alive {label} #1 no trailing bytes",
                rest1 == b"",
                repr(rest1[:32]),
            )
            s2, _, b2, rest2 = request_on(s, "GET", f"/body/{n}")
            check(f"keep-alive {label} #2 status", s2 == 200, str(s2))
            check(
                f"keep-alive {label} #2 byte-exact (no desync)",
                b2 == _BODIES[n],
                first_diff(_BODIES[n], b2),
            )
            check(
                f"keep-alive {label} #2 no trailing bytes",
                rest2 == b"",
                repr(rest2[:32]),
            )

    # 5. Alternating sizes on one connection — catches a retained/reused body
    # buffer that is not re-sized or not fully overwritten between requests
    # (a stale tail from the previous, larger body).
    with connect(host, port) as s:
        for n in (1048576, 1, 262144, 4096, 65536, 0):
            st, _, bd, rest = request_on(s, "GET", f"/body/{n}")
            check(
                f"alternating {n}B byte-exact + framed",
                st == 200 and bd == _BODIES[n] and rest == b"",
                first_diff(_BODIES[n], bd),
            )


def main() -> bool:
    print("=" * 64)
    print("Large response body byte-exactness (native Zig server)")
    print("=" * 64)
    scripts_dir = str(Path(__file__).resolve().parent)
    with AppRunner(
        "test_large_body_wire:app",
        port=PORT,
        env={"PYTHONPATH": scripts_dir, "HYPER_MAX_BODY_SIZE": str(8 * 1024 * 1024)},
    ) as r:
        run(r.host, r.port)
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
