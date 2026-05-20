#!/usr/bin/env python3
# hyper-test: unit
"""Regression tests for CompressionMiddleware correctness (r14).

Covers four confirmed bugs in hyperdjango/standalone_middleware.py's
CompressionMiddleware plus two minor ones:

C1 — the middleware never checked response.status, so a 206 Partial Content
     (with a Content-Range describing offsets into the UNCOMPRESSED body) got
     gzipped, changing the byte length and invalidating the range. Assert a 206
     and any response carrying Content-Range are left untouched.

C2 — ``"gzip" in accept_encoding`` was a substring test that ignored q-values.
     ``gzip;q=0`` (explicit refusal) still compressed. Assert q=0 → no
     compression; a plain ``gzip`` and a ``*`` wildcard → compression.

C3 — ``headers.setdefault("vary", "Accept-Encoding")`` was a no-op when an
     upstream had already set e.g. ``Vary: Cookie`` → Accept-Encoding never
     added → cache poisoning. Assert the compressed response's Vary contains
     BOTH the pre-existing token AND Accept-Encoding.

C4 — text/html + json were compressed unconditionally, exposing per-user
     secrets to BREACH. Assert a response with Set-Cookie, and one with
     Cache-Control: no-transform, are NOT compressed.

M2 — token matching must be case-insensitive: ``Accept-Encoding: GZIP`` is
     honored.

Pure test — no DB, no network. Drives CompressionMiddleware.__call__ directly
with a fake request + call_next.
"""

import asyncio
import gzip
import sys

from hyperdjango.response import Response
from hyperdjango.standalone_middleware import CompressionMiddleware


class FakeRequest:
    """Minimal stand-in exposing exactly what CompressionMiddleware reads."""

    def __init__(self, accept_encoding="gzip"):
        self.headers = {"accept-encoding": accept_encoding}


def _run(mw, request, response):
    async def call_next(_req):
        return response

    return asyncio.run(mw(request, call_next))


# A body large enough to clear min_size and to actually shrink under gzip.
BIG_BODY = ("<html><body>" + ("hello world " * 200) + "</body></html>").encode()


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

    mw = CompressionMiddleware(min_size=500)

    # ── baseline: an ordinary public 200 IS compressed and decodes ────────
    print("\n=== baseline: ordinary public 200 compresses & round-trips ===")
    resp = Response(body=BIG_BODY, status=200, content_type="text/html")
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "content-encoding: gzip set",
        out.headers.get("content-encoding") == "gzip",
        f"headers={out.headers}",
    )
    check(
        "body decodes back to the original",
        out.headers.get("content-encoding") == "gzip"
        and gzip.decompress(out.body) == BIG_BODY,
        "gzip.decompress mismatch",
    )
    check(
        "content-length matches compressed body",
        out.headers.get("content-length") == str(len(out.body)),
        f"len={len(out.body)} header={out.headers.get('content-length')}",
    )
    check(
        "Vary contains Accept-Encoding",
        "accept-encoding"
        in (out.headers.get("vary") or out.headers.get("Vary") or "").lower(),
        f"vary={out.headers.get('vary')!r}",
    )

    # ── C1: 206 Partial Content NOT compressed, Content-Range intact ──────
    print("\n=== C1: 206 / Content-Range left untouched ===")
    resp = Response(
        body=BIG_BODY,
        status=206,
        headers={"content-range": "bytes 0-1099/5000"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "206: no content-encoding added",
        "content-encoding" not in out.headers,
        f"headers={out.headers}",
    )
    check(
        "206: body unchanged (still uncompressed)",
        out.body == BIG_BODY,
        "body was mutated",
    )
    check(
        "206: Content-Range preserved",
        out.headers.get("content-range") == "bytes 0-1099/5000",
        f"got {out.headers.get('content-range')!r}",
    )

    # A 200 that nonetheless carries Content-Range must also be skipped.
    resp = Response(
        body=BIG_BODY,
        status=200,
        headers={"content-range": "bytes 0-1099/5000"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "Content-Range on a 200 also skips compression",
        "content-encoding" not in out.headers,
        f"headers={out.headers}",
    )

    # ── C2 / M2: Accept-Encoding q-value parsing ──────────────────────────
    print("\n=== C2/M2: Accept-Encoding parsing (q=0, wildcard, case) ===")

    def compresses(accept):
        out = _run(
            mw,
            FakeRequest(accept),
            Response(body=BIG_BODY, status=200, content_type="text/html"),
        )
        return out.headers.get("content-encoding") == "gzip"

    check("gzip;q=0 → NOT compressed (explicit refusal)", not compresses("gzip;q=0"))
    check("gzip;q=0.0 → NOT compressed", not compresses("gzip;q=0.0"))
    check("gzip;q=0.5 → compressed", compresses("gzip;q=0.5"))
    check("GZIP → compressed (case-insensitive)", compresses("GZIP"))
    check("gzip, deflate → compressed", compresses("gzip, deflate"))
    check("* wildcard → compressed", compresses("*"))
    check("*;q=0 → NOT compressed", not compresses("*;q=0"))
    check(
        "identity only → NOT compressed",
        not compresses("identity"),
    )
    check(
        "gzip;q=0 alongside * → gzip refusal wins",
        not compresses("gzip;q=0, *"),
    )
    check("empty Accept-Encoding → NOT compressed", not compresses(""))

    # ── C3: Vary merge, not setdefault clobber ────────────────────────────
    print("\n=== C3: Vary merges with a pre-existing token ===")
    resp = Response(
        body=BIG_BODY,
        status=200,
        headers={"vary": "Cookie"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    vary = (out.headers.get("vary") or out.headers.get("Vary") or "").lower()
    check(
        "compressed despite pre-existing Vary",
        out.headers.get("content-encoding") == "gzip",
    )
    check("Vary still contains Cookie", "cookie" in vary, f"vary={vary!r}")
    check(
        "Vary now also contains Accept-Encoding",
        "accept-encoding" in vary,
        f"vary={vary!r}",
    )
    # Idempotence: a pre-existing Accept-Encoding is not duplicated.
    resp = Response(
        body=BIG_BODY,
        status=200,
        headers={"vary": "Accept-Encoding"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    vary = out.headers.get("vary") or out.headers.get("Vary") or ""
    check(
        "Accept-Encoding not duplicated in Vary",
        vary.lower().count("accept-encoding") == 1,
        f"vary={vary!r}",
    )

    # ── C4: BREACH guards ─────────────────────────────────────────────────
    print("\n=== C4: BREACH — Set-Cookie / no-transform not compressed ===")
    resp = Response(body=BIG_BODY, status=200, content_type="text/html")
    resp.set_cookie("session", "s3cr3t-token")
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "Set-Cookie (structured) → NOT compressed",
        "content-encoding" not in out.headers,
        f"headers={out.headers}",
    )

    # Set-Cookie assigned directly to the header dict is also caught.
    resp = Response(
        body=BIG_BODY,
        status=200,
        headers={"set-cookie": "session=abc"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "Set-Cookie (raw header) → NOT compressed",
        "content-encoding" not in out.headers,
        f"headers={out.headers}",
    )

    resp = Response(
        body=BIG_BODY,
        status=200,
        headers={"cache-control": "public, no-transform, max-age=60"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "Cache-Control: no-transform → NOT compressed",
        "content-encoding" not in out.headers,
        f"headers={out.headers}",
    )

    # A plain public cacheable response (no cookie, no no-transform) still
    # compresses — the guard must not over-reach.
    resp = Response(
        body=BIG_BODY,
        status=200,
        headers={"cache-control": "public, max-age=60"},
        content_type="text/html",
    )
    out = _run(mw, FakeRequest("gzip"), resp)
    check(
        "ordinary public cacheable response STILL compresses",
        out.headers.get("content-encoding") == "gzip",
        f"headers={out.headers}",
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All compression r14 tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
