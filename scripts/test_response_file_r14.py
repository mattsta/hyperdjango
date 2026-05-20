#!/usr/bin/env python3
# hyper-test: unit
"""Regression tests for Response.file()/attachment() header hardening (r14).

Covers two confirmed issues in hyperdjango/response.py:

A3-S2 (stored-XSS vector) — Response.file() serves a file INLINE with a
content-type guessed from the extension and NO ``X-Content-Type-Options:
nosniff``. A user-uploaded ``.svg``/``.html`` then runs as active content in the
app origin (stored XSS). Assert file() and attachment() both emit
``x-content-type-options: nosniff``; attachment() also keeps its
``Content-Disposition: attachment``.

A5-M3 (correctness) — file() passed the bare guessed type (e.g. ``text/css``,
``text/html``) with no ``; charset=utf-8`` for text/markup types, risking client
misdecode. Assert text/markup guesses gain a charset and binary guesses (png)
stay bare.

Pure test — no DB, no network. Constructs Responses over real temp files.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

from hyperdjango.response import Response


def _tmpfile(suffix: str, data: bytes = b"x") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def main():
    passed = 0
    failed = 0
    made = []

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    def mk(suffix, data=b"x"):
        p = _tmpfile(suffix, data)
        made.append(p)
        return p

    try:
        # ── A3-S2: inline file() carries nosniff ───────────────────────────
        print("\n=== A3-S2: Response.file() sets X-Content-Type-Options: nosniff ===")
        for suffix, label in ((".html", "html"), (".svg", "svg")):
            resp = Response.file(mk(suffix, b"<x/>"))
            check(
                f"file() {label}: x-content-type-options == nosniff",
                resp.headers.get("x-content-type-options") == "nosniff",
                f"headers={resp.headers}",
            )

        # Large (streamed) file path also carries nosniff.
        big = mk(".html", b"a" * (9 * 1024 * 1024))
        resp = Response.file(big)
        check(
            "file() streamed (>8MiB): nosniff present and streaming",
            resp.is_streaming
            and resp.headers.get("x-content-type-options") == "nosniff",
            f"streaming={resp.is_streaming} headers={resp.headers}",
        )

        # Caller override wins (setdefault, not clobber).
        resp = Response.file(mk(".html"), headers={"x-content-type-options": "custom"})
        check(
            "file(): explicit x-content-type-options header is preserved",
            resp.headers.get("x-content-type-options") == "custom",
            f"headers={resp.headers}",
        )

        # ── A3-S2: attachment() — nosniff + disposition ────────────────────
        print("\n=== A3-S2: Response.attachment() nosniff + disposition ===")
        resp = Response.attachment(mk(".svg", b"<svg/>"), filename="evil.svg")
        check(
            "attachment(): x-content-type-options == nosniff",
            resp.headers.get("x-content-type-options") == "nosniff",
            f"headers={resp.headers}",
        )
        cd = resp.headers.get("content-disposition", "")
        check(
            "attachment(): Content-Disposition is attachment",
            cd.startswith("attachment"),
            f"content-disposition={cd!r}",
        )

        # ── A5-M3: charset on text/markup, not on binary ───────────────────
        print("\n=== A5-M3: charset appended to text/markup guesses only ===")
        # text/* family
        for suffix, base in ((".html", "text/html"), (".css", "text/css")):
            resp = Response.file(mk(suffix))
            ct = resp.headers.get("content-type", "")
            check(
                f"file() {suffix}: {base}; charset=utf-8",
                ct == f"{base}; charset=utf-8",
                f"content-type={ct!r}",
            )
        # svg is XML/markup -> gets charset
        resp = Response.file(mk(".svg", b"<svg/>"))
        ct = resp.headers.get("content-type", "")
        check(
            "file() .svg: image/svg+xml; charset=utf-8",
            ct == "image/svg+xml; charset=utf-8",
            f"content-type={ct!r}",
        )
        # binary png -> NO charset
        resp = Response.file(mk(".png", b"\x89PNG"))
        ct = resp.headers.get("content-type", "")
        check(
            "file() .png: image/png, no charset",
            ct == "image/png" and "charset" not in ct,
            f"content-type={ct!r}",
        )
        # explicit binary octet-stream guess (unknown ext) -> no charset
        resp = Response.file(mk(".unknownext"))
        ct = resp.headers.get("content-type", "")
        check(
            "file() unknown ext: application/octet-stream, no charset",
            ct == "application/octet-stream",
            f"content-type={ct!r}",
        )

        # ── #124: RFC 7233 Range support (media seeking / resumable) ────────
        print("\n=== #124: Response.file() HTTP Range support ===")

        class _Req:
            def __init__(self, rng=None):
                self.headers = {"range": rng} if rng is not None else {}

        DATA = bytes(range(256)) * 4  # 1024 bytes, 0..255 repeating
        fp = mk(".bin", DATA)

        # No request → full body, no 206, but advertises Accept-Ranges.
        r = Response.file(fp)
        check(
            "no request → 200 full body",
            r.status == 200 and r.body == DATA,
            f"status={r.status}",
        )
        check(
            "200 advertises accept-ranges",
            r.headers.get("accept-ranges") == "bytes",
            f"{r.headers}",
        )

        # bytes=0-99 → 206, first 100 bytes, Content-Range + Content-Length.
        r = Response.file(fp, request=_Req("bytes=0-99"))
        check("bytes=0-99 → 206", r.status == 206, f"status={r.status}")
        check("bytes=0-99 body", r.body == DATA[0:100], f"len={len(r.body)}")
        check(
            "bytes=0-99 content-range",
            r.headers.get("content-range") == "bytes 0-99/1024",
            f"{r.headers}",
        )
        check(
            "bytes=0-99 content-length",
            r.headers.get("content-length") == "100",
            f"{r.headers}",
        )
        check(
            "bytes=0-99 accept-ranges",
            r.headers.get("accept-ranges") == "bytes",
            f"{r.headers}",
        )

        # Open-ended bytes=1000- → last 24 bytes.
        r = Response.file(fp, request=_Req("bytes=1000-"))
        check(
            "bytes=1000- → 206 tail",
            r.status == 206 and r.body == DATA[1000:],
            f"len={len(r.body)}",
        )
        check(
            "bytes=1000- content-range",
            r.headers.get("content-range") == "bytes 1000-1023/1024",
            f"{r.headers}",
        )

        # Suffix bytes=-10 → last 10 bytes.
        r = Response.file(fp, request=_Req("bytes=-10"))
        check(
            "bytes=-10 → last 10",
            r.status == 206 and r.body == DATA[-10:],
            f"len={len(r.body)}",
        )
        check(
            "bytes=-10 content-range",
            r.headers.get("content-range") == "bytes 1014-1023/1024",
            f"{r.headers}",
        )

        # end past EOF is clamped to size-1.
        r = Response.file(fp, request=_Req("bytes=1020-99999"))
        check(
            "clamped end → 206",
            r.status == 206 and r.body == DATA[1020:],
            f"len={len(r.body)}",
        )
        check(
            "clamped content-range",
            r.headers.get("content-range") == "bytes 1020-1023/1024",
            f"{r.headers}",
        )

        # Unsatisfiable (start >= size) → 416 with size.
        r = Response.file(fp, request=_Req("bytes=5000-6000"))
        check("start>=size → 416", r.status == 416, f"status={r.status}")
        check(
            "416 content-range */size",
            r.headers.get("content-range") == "bytes */1024",
            f"{r.headers}",
        )
        check("416 empty body", r.body in (b"", None), f"body={r.body!r}")

        # Malformed / unsupported unit / multi-range → ignore, serve full 200.
        for bad in ("bytes=abc-def", "items=0-10", "bytes=0-10,20-30", "bogus"):
            r = Response.file(fp, request=_Req(bad))
            check(
                f"ignore {bad!r} → 200 full",
                r.status == 200 and r.body == DATA,
                f"status={r.status}",
            )

        # No Range header at all → 200 full.
        r = Response.file(fp, request=_Req())
        check(
            "no range header → 200 full",
            r.status == 200 and r.body == DATA,
            f"status={r.status}",
        )

        # attachment(request=) also honors Range (resumable downloads).
        r = Response.attachment(fp, filename="d.bin", request=_Req("bytes=0-9"))
        check(
            "attachment range → 206",
            r.status == 206 and r.body == DATA[0:10],
            f"status={r.status}",
        )
        check(
            "attachment keeps disposition",
            "attachment" in r.headers.get("content-disposition", ""),
            f"{r.headers}",
        )

        # Large-file streamed range: window > 8 MiB streams (206, is_streaming).
        big = mk(".bin", b"a" * (9 * 1024 * 1024))
        r = Response.file(big, request=_Req("bytes=0-"))
        check(
            "large full-range → 206 streamed",
            r.status == 206 and r.is_streaming,
            f"status={r.status} streaming={r.is_streaming}",
        )
        check(
            "large range content-range",
            r.headers.get("content-range") == "bytes 0-9437183/9437184",
            f"{r.headers}",
        )
    finally:
        for p in made:
            # temp cleanup best-effort; not a test signal
            with contextlib.suppress(OSError):
                Path(p).unlink()

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All response file r14 tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
