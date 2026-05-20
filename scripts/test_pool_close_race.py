#!/usr/bin/env python3
"""Pool close vs in-flight operation lifetime race (native `db_close_pool`).

`_db_close_pool` destroys the native pg pool while OTHER threads may still be
inside an operation that resolved the same pool handle — a pinned acquire
(`_db_conn_acquire`), a query on a checked-out connection, or the release that
returns it. On the multiplexing DB path those operations run on offload
executor threads with no lock ordering against `Database.disconnect()`, so a
close that lands mid-operation frees the pool under the operation's feet and
the subsequent `pool.acquire()` / `conn.release()` is a use-after-free SIGSEGV
(observed in CI as `test_pinned_conns_r9` dying with exit -11 during teardown).

This file drives that exact interleaving deterministically hard: hammer
threads run tight acquire→execute→release (pinned path) and plain-query
(pool acquire/release path) loops against a fresh pool while the main thread
closes it mid-storm. Correct behavior: every in-flight operation either
completes against the still-alive pool or fails with a clean RuntimeError
("Failed to acquire..." / "Query failed") once the handle is closed — the
process must never die.

Usage:
    uv run hyper-test pool_close_race
    DATABASE_URL=... uv run python scripts/test_pool_close_race.py
"""

# hyper-test: db_isolated

import os
import random
import threading
import time

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_conn_acquire,
    _db_conn_release,
    _db_execute,
    _db_mark_offload_worker,
    _db_query,
)

from hyperdjango.testkit import TestRun, run_main

DB_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/hyperdjango_test",
)

ROUNDS = 25
PINNED_HAMMERS = 4
QUERY_HAMMERS = 4  # unmarked threads → thread-owned slot path (claim/lazy/reap)
OFFLOAD_HAMMERS = 4  # offload-marked threads → per-op acquire/release path
POOL_SIZE = 3  # smaller than the hammer count → real acquire contention
CONNECT_TIMEOUT_MS = 2000
QUERY_TIMEOUT_MS = 2000
# Close lands anywhere from "immediately" to "mid-steady-state" so successive
# rounds sweep the window across acquire, execute, and release.
CLOSE_DELAY_RANGE_S = (0.0, 0.03)

# The pinned-execute handle encoding used by database.py's transaction path:
# a pinned connection handle h is addressed as -(h + 2).
PINNED_EXECUTE_BASE = 2


def _pinned_hammer(handle: int, stop: threading.Event, errors: list[str]) -> None:
    while not stop.is_set():
        try:
            h = _db_conn_acquire(handle)
        except RuntimeError:
            return  # pool closed (or momentarily exhausted at close) — clean
        try:
            _db_execute(-(h + PINNED_EXECUTE_BASE), "SELECT 1", [])
        except RuntimeError:
            pass  # connection torn down mid-query — clean typed failure
        except BaseException as exc:  # noqa: BLE001 - anything else is a bug
            errors.append(f"pinned execute raised {exc!r}")
        finally:
            try:
                _db_conn_release(h)
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"pinned release raised {exc!r}")


def _query_hammer(handle: int, stop: threading.Event, errors: list[str]) -> None:
    while not stop.is_set():
        try:
            _db_query(handle, "SELECT 1", [])
        except RuntimeError:
            return  # pool closed — clean
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"plain query raised {exc!r}")
            return


def _offload_hammer(handle: int, stop: threading.Event, errors: list[str]) -> None:
    # The multiplexing-loop DB executor path: worker threads marked so every
    # query acquires from the pool and releases per op — the exact path the
    # CI teardown SIGSEGV crashed on.
    _db_mark_offload_worker()
    _query_hammer(handle, stop, errors)


def main() -> bool:
    run = TestRun()
    print("=" * 64)
    print("Pool close vs in-flight native op race")
    print("=" * 64)
    rng = random.Random(0xC10E5)

    errors: list[str] = []
    for rnd in range(ROUNDS):
        handle = _db_configure(
            DB_URL, POOL_SIZE, CONNECT_TIMEOUT_MS, QUERY_TIMEOUT_MS, 0, 0
        )
        stop = threading.Event()
        threads = (
            [
                threading.Thread(
                    target=_pinned_hammer, args=(handle, stop, errors), daemon=True
                )
                for _ in range(PINNED_HAMMERS)
            ]
            + [
                threading.Thread(
                    target=_query_hammer, args=(handle, stop, errors), daemon=True
                )
                for _ in range(QUERY_HAMMERS)
            ]
            + [
                threading.Thread(
                    target=_offload_hammer, args=(handle, stop, errors), daemon=True
                )
                for _ in range(OFFLOAD_HAMMERS)
            ]
        )
        for t in threads:
            t.start()

        time.sleep(rng.uniform(*CLOSE_DELAY_RANGE_S))
        _db_close_pool(handle)

        stop.set()
        for t in threads:
            # In-flight waiters may sit out a bounded pool-acquire timeout on
            # the closing pool before failing cleanly; give them that budget.
            t.join(timeout=CONNECT_TIMEOUT_MS / 1000 + 10)
            if t.is_alive():
                errors.append(f"round {rnd}: hammer thread hung past close")
        survived = f"round {rnd + 1:2d}/{ROUNDS}: close mid-storm survived"
        if errors:
            run.check(survived, False, errors[0])
            break
        run.check(survived, True)

    if errors:
        print(f"\nFAIL: {errors[0]} ({len(errors)} error(s) total)")
    else:
        print("(each round = one pool closed mid-storm with no crash or stray error)")
    print()
    return run.finish()


if __name__ == "__main__":
    run_main(main)
