"""
Benchmark: @guard() overhead vs manual auth checks.

# hyper-test: bench

Measures per-request overhead of the guard decorator compared to inline auth checks.
"""

import asyncio
import time
from dataclasses import dataclass

from hyperdjango.auth.user import SessionUser
from hyperdjango.guard import Require
from hyperdjango.guard.evaluator import evaluate_guard
from hyperdjango.guard.types import GuardSpec

ITERATIONS = 10_000

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


@dataclass
class MockRequest:
    """Minimal request mock for benchmarking."""

    user: SessionUser | None = None
    path: str = "/test"
    method: str = "GET"
    path_params: dict[str, str] | None = None
    api_key_valid: bool = False
    headers: dict[str, str] | None = None
    guard: object = None

    def __post_init__(self):
        if self.path_params is None:
            self.path_params = {}
        if self.headers is None:
            self.headers = {}


def _make_authed_request() -> MockRequest:
    return MockRequest(
        user=SessionUser({"id": 1, "username": "demo", "groups": ["staff"]})
    )


def _make_anon_request() -> MockRequest:
    return MockRequest(user=None)


async def _bench_baseline(n: int) -> float:
    """Raw handler call — no auth check at all."""
    req = _make_authed_request()

    async def handler(request):
        return {"ok": True}

    t0 = time.perf_counter()
    for _ in range(n):
        await handler(req)
    return (time.perf_counter() - t0) * 1_000_000 / n  # µs per call


async def _bench_manual_auth(n: int) -> float:
    """Manual inline auth check."""
    req = _make_authed_request()

    async def handler(request):
        if request.user is None:
            return {"error": "unauthorized"}
        return {"ok": True}

    t0 = time.perf_counter()
    for _ in range(n):
        await handler(req)
    return (time.perf_counter() - t0) * 1_000_000 / n


async def _bench_guard_single(n: int) -> float:
    """@guard(Require.authenticated()) — single requirement."""
    req = _make_authed_request()
    spec = GuardSpec(requirements=(Require.authenticated(),))

    t0 = time.perf_counter()
    for _ in range(n):
        await evaluate_guard(req, spec)
    return (time.perf_counter() - t0) * 1_000_000 / n


async def _bench_guard_chain(n: int) -> float:
    """@guard with 3 requirements — authenticated + not_banned + resource."""

    async def _always_ok(request, ctx):
        return None

    req = _make_authed_request()
    spec = GuardSpec(
        requirements=(
            Require.authenticated(),
            Require.check("active", fn=_always_ok),
            Require.check("verified", fn=_always_ok),
        )
    )

    t0 = time.perf_counter()
    for _ in range(n):
        await evaluate_guard(req, spec)
    return (time.perf_counter() - t0) * 1_000_000 / n


async def _bench_guard_spec_creation(n: int) -> float:
    """GuardSpec creation overhead (happens at decoration time, not per-request)."""
    t0 = time.perf_counter()
    for _ in range(n):
        GuardSpec(requirements=(Require.authenticated(),))
    return (time.perf_counter() - t0) * 1_000_000 / n


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Guard Overhead Benchmark")
    print(f"Iterations: {ITERATIONS:,}")
    print("=" * 60)

    loop = asyncio.new_event_loop()

    baseline = loop.run_until_complete(_bench_baseline(ITERATIONS))
    manual = loop.run_until_complete(_bench_manual_auth(ITERATIONS))
    single = loop.run_until_complete(_bench_guard_single(ITERATIONS))
    chain = loop.run_until_complete(_bench_guard_chain(ITERATIONS))
    creation = loop.run_until_complete(_bench_guard_spec_creation(ITERATIONS))

    loop.close()

    print(f"\n  Baseline (no auth):           {baseline:8.2f} µs/req")
    print(f"  Manual inline auth:           {manual:8.2f} µs/req")
    print(f"  @guard(authenticated):        {single:8.2f} µs/req")
    print(f"  @guard(3 requirements):       {chain:8.2f} µs/req")
    print(f"  GuardSpec creation:           {creation:8.2f} µs/call")

    guard_overhead_single = single - manual
    guard_overhead_chain = chain - manual

    print(f"\n  Guard overhead (single):      {guard_overhead_single:8.2f} µs")
    print(f"  Guard overhead (3-chain):     {guard_overhead_chain:8.2f} µs")

    print("\n--- Assertions ---")
    ok(
        "Single guard < 50µs",
        single < 50.0,
        f"got {single:.2f}µs",
    )
    ok(
        "3-chain guard < 100µs",
        chain < 100.0,
        f"got {chain:.2f}µs",
    )
    ok(
        "Guard overhead (single) < 30µs vs manual",
        guard_overhead_single < 30.0,
        f"got {guard_overhead_single:.2f}µs",
    )
    ok(
        "GuardSpec creation < 5µs",
        creation < 5.0,
        f"got {creation:.2f}µs",
    )

    # ── Summary ──
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
