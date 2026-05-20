#!/usr/bin/env python3
"""CORS expose_headers support — Access-Control-Expose-Headers emission.

Verifies that CORSMiddleware:
1. Emits 'Access-Control-Expose-Headers' on actual (non-preflight) responses
   when expose_headers is configured, with the comma-joined header list.
2. Omits the header entirely when expose_headers is unset (default empty).
3. Does NOT emit the header on preflight (OPTIONS) responses — it only applies
   to actual responses.
4. Caches the joined string once in __post_init__ (_expose_headers_str).

Run: uv run hyper-test cors_expose_headers
"""

# hyper-test: unit

import asyncio
import sys

from hyperdjango.response import Response
from hyperdjango.standalone_middleware import CORSMiddleware


class _FakeRequest:
    """Minimal request stand-in for driving the async middleware directly."""

    def __init__(self, method, origin):
        self.method = method
        self.headers = {"origin": origin}


async def _run(mw, method, origin):
    request = _FakeRequest(method, origin)

    async def call_next(_req):
        return Response.text("ok")

    return await mw(request, call_next)


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

    origin = "https://example.com"

    print("\n=== CORS expose_headers configured ===")
    mw = CORSMiddleware(
        origins=[origin],
        expose_headers=["X-Total-Count", "X-Page"],
    )

    check(
        "joined string cached in __post_init__",
        mw._expose_headers_str == "X-Total-Count, X-Page",
        f"got {mw._expose_headers_str!r}",
    )

    resp = asyncio.run(_run(mw, "GET", origin))
    exposed = resp.headers.get("access-control-expose-headers")
    check(
        "Access-Control-Expose-Headers set on actual response",
        exposed == "X-Total-Count, X-Page",
        f"got {exposed!r}",
    )
    check(
        "allow-origin still present on actual response",
        resp.headers.get("access-control-allow-origin") == origin,
        f"got {resp.headers.get('access-control-allow-origin')!r}",
    )

    # Preflight (OPTIONS) must NOT carry expose-headers — it only applies to
    # actual responses.
    preflight = asyncio.run(_run(mw, "OPTIONS", origin))
    check(
        "Access-Control-Expose-Headers absent on preflight response",
        "access-control-expose-headers" not in preflight.headers,
        f"got {preflight.headers.get('access-control-expose-headers')!r}",
    )
    check(
        "preflight still has allow-methods",
        preflight.headers.get("access-control-allow-methods") is not None,
        "missing allow-methods on preflight",
    )

    print("\n=== CORS expose_headers unset (default) ===")
    mw_none = CORSMiddleware(origins=[origin])
    check(
        "default expose_headers is empty list",
        mw_none.expose_headers == [],
        f"got {mw_none.expose_headers!r}",
    )
    check(
        "default cached string is empty",
        mw_none._expose_headers_str == "",
        f"got {mw_none._expose_headers_str!r}",
    )

    resp_none = asyncio.run(_run(mw_none, "GET", origin))
    check(
        "Access-Control-Expose-Headers absent when unset",
        "access-control-expose-headers" not in resp_none.headers,
        f"got {resp_none.headers.get('access-control-expose-headers')!r}",
    )
    check(
        "allow-origin still present when expose unset",
        resp_none.headers.get("access-control-allow-origin") is not None,
        "missing allow-origin",
    )

    print("\n=== Disallowed origin gets no CORS headers at all ===")
    resp_blocked = asyncio.run(_run(mw, "GET", "https://evil.example"))
    check(
        "no expose-headers for disallowed origin",
        "access-control-expose-headers" not in resp_blocked.headers,
        f"got {resp_blocked.headers.get('access-control-expose-headers')!r}",
    )
    check(
        "no allow-origin for disallowed origin",
        "access-control-allow-origin" not in resp_blocked.headers,
        "allow-origin leaked for disallowed origin",
    )

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
