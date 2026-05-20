"""Correctness / RFC 6455 interop checks, run against both servers.

These aren't perf tests — they're the "does it actually work" gate that
should run before any throughput numbers are trusted. Each check
returns a pass/fail + detail so the report can show a compliance table
alongside the performance tables.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

MAX_SIZE = 20 * 1024 * 1024


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


async def _echo_roundtrip(uri: str, payload: str | bytes) -> str | bytes | None:
    async with connect(
        uri, compression=None, max_size=MAX_SIZE, ping_interval=None
    ) as ws:
        await ws.send(payload)
        return await ws.recv()


async def check_text_ascii(uri: str) -> CheckResult:
    try:
        reply = await _echo_roundtrip(uri, "hello hyperdjango")
        ok = reply == "hello hyperdjango"
        return CheckResult("echo_text_ascii", ok, "" if ok else f"got {reply!r}")
    except Exception as e:
        return CheckResult("echo_text_ascii", False, f"{type(e).__name__}: {e}")


async def check_text_unicode(uri: str) -> CheckResult:
    payload = "héllo wörld — こんにちは 🚀🔥"
    try:
        reply = await _echo_roundtrip(uri, payload)
        ok = reply == payload
        return CheckResult("echo_text_unicode", ok, "" if ok else f"got {reply!r}")
    except Exception as e:
        return CheckResult("echo_text_unicode", False, f"{type(e).__name__}: {e}")


async def check_binary_small(uri: str) -> CheckResult:
    payload = bytes(range(256))
    try:
        reply = await _echo_roundtrip(uri, payload)
        ok = reply == payload and isinstance(reply, bytes)
        return CheckResult("echo_binary_small", ok, "" if ok else f"got {reply!r}")
    except Exception as e:
        return CheckResult("echo_binary_small", False, f"{type(e).__name__}: {e}")


async def check_binary_large(uri: str) -> CheckResult:
    import random

    payload = bytes(random.Random(42).getrandbits(8) for _ in range(1_000_000))
    try:
        reply = await _echo_roundtrip(uri, payload)
        ok = reply == payload
        return CheckResult("echo_binary_1mb", ok, "" if ok else "content mismatch")
    except Exception as e:
        return CheckResult("echo_binary_1mb", False, f"{type(e).__name__}: {e}")


async def check_empty_message(uri: str) -> CheckResult:
    try:
        reply = await _echo_roundtrip(uri, "")
        ok = reply == ""
        return CheckResult("echo_empty_text", ok, "" if ok else f"got {reply!r}")
    except Exception as e:
        return CheckResult("echo_empty_text", False, f"{type(e).__name__}: {e}")


async def check_ping_pong(uri: str) -> CheckResult:
    try:
        async with connect(
            uri, compression=None, max_size=MAX_SIZE, ping_interval=None
        ) as ws:
            pong_waiter = await ws.ping(b"probe")
            await asyncio.wait_for(pong_waiter, timeout=5)
        return CheckResult("ping_pong_keepalive", True)
    except Exception as e:
        return CheckResult("ping_pong_keepalive", False, f"{type(e).__name__}: {e}")


async def check_clean_close(uri: str) -> CheckResult:
    try:
        ws = await connect(
            uri, compression=None, max_size=MAX_SIZE, ping_interval=None
        ).__aenter__()
        await ws.close(code=1000, reason="benchmark done")
        ok = ws.close_code == 1000
        return CheckResult("clean_close_handshake", ok, f"close_code={ws.close_code}")
    except Exception as e:
        return CheckResult("clean_close_handshake", False, f"{type(e).__name__}: {e}")


async def check_concurrent_send_ordering(uri: str, n: int = 50) -> CheckResult:
    """Fire N sequenced messages back-to-back without waiting between sends,
    then verify all N replies arrive, in order, uncorrupted — exercises the
    native server's per-connection write-serialization (write_mutex)."""
    messages = [f"msg-{i:04d}" for i in range(n)]
    try:
        async with connect(
            uri, compression=None, max_size=MAX_SIZE, ping_interval=None
        ) as ws:
            for m in messages:
                await ws.send(m)
            replies = [await ws.recv() for _ in range(n)]
        ok = replies == messages
        detail = (
            ""
            if ok
            else f"first mismatch at index {next(i for i, (a, b) in enumerate(zip(replies, messages)) if a != b)}"
        )
        return CheckResult("concurrent_send_ordering", ok, detail)
    except Exception as e:
        return CheckResult(
            "concurrent_send_ordering", False, f"{type(e).__name__}: {e}"
        )


async def check_multiple_connections_isolated(uri: str) -> CheckResult:
    """Two simultaneous connections shouldn't see each other's echoes."""
    try:
        async with (
            connect(
                uri, compression=None, max_size=MAX_SIZE, ping_interval=None
            ) as ws_a,
            connect(
                uri, compression=None, max_size=MAX_SIZE, ping_interval=None
            ) as ws_b,
        ):
            await ws_a.send("from-a")
            await ws_b.send("from-b")
            reply_a = await ws_a.recv()
            reply_b = await ws_b.recv()
        ok = reply_a == "from-a" and reply_b == "from-b"
        return CheckResult(
            "multi_connection_isolation",
            ok,
            f"a={reply_a!r} b={reply_b!r}" if not ok else "",
        )
    except Exception as e:
        return CheckResult(
            "multi_connection_isolation", False, f"{type(e).__name__}: {e}"
        )


ALL_CHECKS = [
    check_text_ascii,
    check_text_unicode,
    check_binary_small,
    check_binary_large,
    check_empty_message,
    check_ping_pong,
    check_clean_close,
    check_concurrent_send_ordering,
    check_multiple_connections_isolated,
]


async def run_all_checks(uri: str) -> list[CheckResult]:
    results = []
    for check in ALL_CHECKS:
        try:
            results.append(await check(uri))
        except (ConnectionClosed, OSError) as e:
            results.append(CheckResult(check.__name__, False, f"connection error: {e}"))
    return results
