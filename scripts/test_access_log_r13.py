#!/usr/bin/env python3
# hyper-test: unit
"""Regression tests for AccessLogMiddleware status + user_id extraction (r13).

Covers two confirmed bugs in hyperdjango/logging/__init__.py:

A5#1 — the access log recorded status 0 for every request. The middleware read
``getattr(response, "status_code", 0)`` but ``Response`` has no ``status_code``
field (it is ``status``), so the default 0 always fired — in both the human
message and the structured ``status=`` extra. Assert the REAL status (200/404/
500) lands in both the emitted message and record["extra"]["status"], and that a
non-Response return is flagged explicitly (-1) rather than silently as 0.

A5#5 — session-auth lost user correlation. ``request.user`` is polymorphic: a
plain dict (raw SessionAuth payload) or a user object (User/SessionUser/
AnonymousUser). ``getattr(dict, "id")`` misses dict keys, so user_id was None for
the entire SessionAuth mode. Assert user_id is extracted for a dict user AND a
model-shaped user, and stays None for anonymous / absent for a None user.

Pure test — no DB, no network. Drives AccessLogMiddleware.__call__ directly with
a fake request + call_next and captures the emitted log records via a sink.
"""

import asyncio
import sys
import time

from hyperdjango.auth.user import AnonymousUser
from hyperdjango.logging import AccessLogMiddleware, logger
from hyperdjango.response import Response


class FakeRequest:
    """Minimal stand-in exposing exactly what AccessLogMiddleware reads."""

    def __init__(self, user, method="GET", path="/x"):
        self.request_id = "req-r13"
        self.path = path
        self.method = method
        self.client_ip = "127.0.0.1"
        self.user = user


class ModelUser:
    """Object-shaped user (like User/SessionUser): id/pk are attributes."""

    def __init__(self, uid):
        self.id = uid
        self.pk = uid


async def _run_once(user, response, method="GET", path="/x"):
    """Drive the middleware once; return (access_record, inner_record).

    access_record — the access-log line (emitted AFTER context reset; carries
        status/duration in extra).
    inner_record — a log line emitted by call_next WHILE the request-scope log
        context is active (this is where user_id propagation is observable).
    """
    captured = []
    sink_id = logger.add(
        lambda record, message: captured.append(record),
        level="DEBUG",
        colorize=False,
    )

    def _find():
        access = next((r for r in captured if "status" in r["extra"]), None)
        inner = next(
            (r for r in captured if r["message"] == "inner-handler-log-r13"),
            None,
        )
        return access, inner

    try:

        async def call_next(request):
            # Emitted while log_context (with user_id) is active — mirrors a
            # real handler log line, the actual consumer of the injected ctx.
            logger.info("inner-handler-log-r13")
            return response

        mw = AccessLogMiddleware()
        returned = await mw(FakeRequest(user, method, path), call_next)

        # The global background writer thread drains records asynchronously —
        # poll until both the inner handler log and the access-log line land.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            access, inner = _find()
            if access is not None and inner is not None:
                break
            time.sleep(0.01)
    finally:
        logger.remove(sink_id)

    access, inner = _find()
    return access, inner, returned


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

    # ── A5#1: real status in message + structured field ───────────────────
    print("\n=== A5#1: access log records the actual status ===")
    for code in (200, 404, 500):
        resp = Response(body=b"", status=code)
        access, _inner, _ret = asyncio.run(_run_once(None, resp))
        check(
            f"status {code}: access record emitted",
            access is not None,
            "no access-log record captured",
        )
        if access is not None:
            check(
                f"status {code}: structured extra['status'] is real",
                access["extra"]["status"] == code,
                f"got {access['extra'].get('status')!r}",
            )
            check(
                f"status {code}: message contains the real status",
                f" {code} " in access["message"],
                f"message={access['message']!r}",
            )
            check(
                f"status {code}: NOT the old default 0",
                access["extra"]["status"] != 0,
                "status still 0 — bug not fixed",
            )

    # Non-Response return is flagged explicitly, never silently 0.
    print("\n=== A5#1b: non-Response return flagged explicitly ===")
    sentinel = object()
    access, _inner, ret = asyncio.run(_run_once(None, sentinel))
    check("non-Response passed through unchanged", ret is sentinel)
    check(
        "non-Response status marked -1 (explicit unknown), not 0",
        access is not None and access["extra"]["status"] == -1,
        f"got {access['extra'].get('status') if access else None!r}",
    )

    # ── A5#5: user_id extraction across shapes ────────────────────────────
    print("\n=== A5#5: user_id extracted for every request.user shape ===")
    resp = Response(body=b"", status=200)

    # dict user (raw SessionAuth payload) — the regression case.
    _a, inner, _r = asyncio.run(_run_once({"id": 42, "username": "alice"}, resp))
    check(
        "dict user: user_id == 42 (SessionAuth no longer lost)",
        inner is not None and inner["extra"].get("user_id") == 42,
        f"got {inner['extra'].get('user_id') if inner else None!r}",
    )

    # dict user with only pk (id absent) — falls back to pk.
    _a, inner, _r = asyncio.run(_run_once({"pk": 7}, resp))
    check(
        "dict user with pk-only: user_id == 7",
        inner is not None and inner["extra"].get("user_id") == 7,
        f"got {inner['extra'].get('user_id') if inner else None!r}",
    )

    # model-shaped user (attributes).
    _a, inner, _r = asyncio.run(_run_once(ModelUser(99), resp))
    check(
        "model user: user_id == 99 via .id",
        inner is not None and inner["extra"].get("user_id") == 99,
        f"got {inner['extra'].get('user_id') if inner else None!r}",
    )

    # anonymous user — id/pk are None → user_id None (present but None).
    _a, inner, _r = asyncio.run(_run_once(AnonymousUser(), resp))
    check(
        "anonymous user: user_id is None (not a crash, not a miss)",
        inner is not None and inner["extra"].get("user_id") is None,
        f"got {inner['extra'].get('user_id') if inner else None!r}",
    )

    # None user — no user_id key injected at all.
    _a, inner, _r = asyncio.run(_run_once(None, resp))
    check(
        "None user: user_id key absent from context",
        inner is not None and "user_id" not in inner["extra"],
        f"extra keys={list(inner['extra'].keys()) if inner else None}",
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All access-log r13 tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
