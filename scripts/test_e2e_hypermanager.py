"""
E2E: HyperManager change-notification hub (services/hypermanager).

# hyper-test: e2e

Boots the real native server (plus the in-process mTLS terminator) and
exercises:

  - publish → ledger cursor; replay filtering by subscribe grants
  - validation: bad subject/kind, oversized metadata, publish denial
  - idempotent publish: one dedupe key → one ledger row, even under a re-POST
    storm; a fresh key appends; a truly-concurrent same-key double-POST collapses
    to one id with no 500 and no error-audit row
  - live watcher (wake hint + cursor-replay pull): delivery + reconnect resume
  - ordering property: concurrent publishes across many first-segment domains
    are delivered in exact ledger order, no loss, no dups
  - grant filtering: an out-of-grant subject reaches neither replay nor the
    watcher; a no-feed-scope identity is denied replay and delivered nothing
  - admin identity/grant lifecycle + revocation
  - mTLS: client-certificate identity over the terminator (HTTP + WS feed),
    unknown-CN rejection, forged attestation-header rejection
"""

import asyncio
import contextlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e_helper import (  # noqa: E402
    E2E_CLIENT_TIMEOUT,
    TEST_PORTS,
    AppRunner,
    http_get,
)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.hypermanager.client import (  # noqa: E402
    ManagerAuthError,
    ManagerError,
)
from services.hypermanager.client import (  # noqa: E402
    ManagerClient as _ManagerClient,
)


def ManagerClient(*args, **kwargs):  # noqa: N802 — stands in for the class
    """The service client, with an e2e-appropriate reachability timeout.

    Every call site in this file wants "the server answered", never "the server
    answered within the production default". Injecting it here rather than at
    seventy-five call sites keeps the two concerns separate: the client's own
    default stays honest for real callers, and this file stops failing on
    whichever CI runner happens to be the slow one.
    """
    kwargs.setdefault("timeout", E2E_CLIENT_TIMEOUT)
    return _ManagerClient(*args, **kwargs)


PORT = TEST_PORTS["hypermanager"]
MTLS_PORT = TEST_PORTS["hypermanager_mtls"]
BASE = f"http://127.0.0.1:{PORT}"
MTLS_BASE = f"https://127.0.0.1:{MTLS_PORT}"
DEMO_DIR = PROJECT_ROOT / ".test_scratch" / "hypermanager_demo"
CA_DIR = DEMO_DIR / "ca"

# Token-signing key (framework SESSION_SIGNING_KEY) must be STABLE across the
# seed and server subprocesses. hyper-test injects it; pin for standalone runs.
os.environ.setdefault(
    "HYPER_SESSION_SIGNING_KEY", "test-session-signing-key-for-tests-only"
)
# app.py now require_setting()s SECRET_KEY and ADMIN_SECRET (fail closed on the
# per-process default), so the seed + server subprocesses — which inherit this
# process's environ — need both set to a ≥32-char value. The framework test
# runner seeds a short HYPER_ADMIN_SECRET, so lengthen it here when needed.
os.environ.setdefault("HYPER_SECRET_KEY", "test-secret-key-for-hypermanager-e2e-only")
if len(os.environ.get("HYPER_ADMIN_SECRET", "")) < 32:
    os.environ["HYPER_ADMIN_SECRET"] = "test-admin-secret-for-hypermanager-e2e-only"

PASS = 0
FAIL = 0
ERRORS: list[str] = []

# A valid bearer token for the now-authenticated /metrics scrape (item 10). Set
# in main() from the seeded tokens; _metric_value presents it on every scrape.
_SCRAPE_TOKEN = ""


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")
    return cond


def expect_raises(name: str, exc_type, fn) -> None:
    try:
        fn()
        check(name, False, f"expected {exc_type.__name__}, no error raised")
    except exc_type:
        check(name, True)
    except Exception as exc:  # noqa: BLE001 - report wrong-type failures
        check(name, False, f"expected {exc_type.__name__}, got {exc!r}")


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _backdate_events(max_id: int, when: datetime) -> None:
    """Age every ledger row with ``id <= max_id`` to ``when`` in the shared DB.

    The test process and the server share one Postgres database, so writing the
    stale timestamp here makes the server's own retention sweep (running on a
    fast cadence under test) trim those rows and lift its in-process replay
    floor above ``max_id`` — the only honest way to exercise the floor without
    real days of elapsed time."""

    async def _run():
        from hyperdjango.database import Database

        db = Database(
            os.environ.get("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
        )
        await db.connect()
        try:
            await db.execute(
                "UPDATE hm_events SET created_at = $1 WHERE id <= $2", when, max_id
            )
        finally:
            await db.disconnect()

    asyncio.run(_run())


def _set_cert_fingerprint(name: str, fingerprint: str) -> None:
    """Pin (or clear) an identity's certificate-fingerprint allow-list in the
    shared DB, so a real terminator connection exercises the pin check."""

    async def _run():
        from hyperdjango.database import Database

        db = Database(
            os.environ.get("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
        )
        await db.connect()
        try:
            await db.execute(
                "UPDATE hm_identities SET cert_fingerprint = $1 WHERE name = $2",
                fingerprint,
                name,
            )
        finally:
            await db.disconnect()

    asyncio.run(_run())


def _metric_value(name: str, labels: str = "") -> float:
    """Scrape a single Prometheus sample value from the server's /metrics.

    ``labels`` is the exact ``{...}`` selector (or "" for an unlabeled metric).
    Returns 0.0 when the sample is absent (a never-touched counter/gauge). The
    scrape is authenticated (item 10), so it presents a seeded bearer token."""
    headers = {"Authorization": f"Bearer {_SCRAPE_TOKEN}"} if _SCRAPE_TOKEN else {}
    resp = http_get(f"{BASE}/metrics", headers=headers)
    # A non-200 scrape means the /metrics endpoint is broken/unauthorized — NOT
    # that the gauge is zero. Returning 0.0 there would silently mask a broken
    # measurement path as a plausible floor (and make a real regression read as
    # a benign zero). Fail loudly; a genuinely-absent sample on a 200 response
    # still returns 0.0 below (that IS a real zero).
    if resp.status != 200:
        raise AssertionError(
            f"/metrics scrape returned {resp.status} (expected 200) for "
            f"{name!r}; cannot measure the gauge"
        )
    needle = name + labels
    for line in resp.body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        key, _, val = line.partition(" ")
        if key == needle:
            with contextlib.suppress(ValueError):
                return float(val.strip())
    return 0.0


def _wait_metric_reaches(
    name: str,
    predicate,
    what: str,
    labels: str = "",
    *,
    interval: float = 0.05,
    timeout: float = 30.0,
) -> float:
    """Return a gauge's value once it SATISFIES ``predicate``.

    "Has stopped moving" and "has reached the value I am about to assert on"
    are different questions, and only the second one is safe to measure
    against. A gauge that has not been incremented yet is perfectly steady, so
    a settle-based read on a slow runner returns the value from BEFORE the
    event under test — which is how this file asserted `connected >= after + 1`
    against a `connected` of 0.0 on a three-core macOS runner and failed while
    five other runners passed.

    A wait for the transition itself cannot be satisfied early: it is only
    reached once the server has actually done the thing. The ceiling is
    generous because it bounds "the server never did it" and asserts nothing
    about how quickly — a slower machine only waits longer for a state that a
    correct server always reaches.
    """
    deadline = time.monotonic() + timeout
    value = _metric_value(name, labels)
    while time.monotonic() < deadline:
        if predicate(value):
            return value
        time.sleep(interval)
        value = _metric_value(name, labels)
    raise AssertionError(
        f"{name}{labels} never {what} within {timeout:.0f}s (last={value})"
    )


class _HeldForeignWriteTx:
    """Open a write transaction on a DIFFERENT table (hm_access_log) in this
    process and hold it uncommitted — a long-lived foreign writer that pins the
    cluster-global xmin horizon WITHOUT touching hm_events. Used to prove the
    replay ceiling is scoped to hm_events writers (finding 1): a committed
    hm_events row must still be servable even while this foreign tx runs."""

    def __init__(self):
        self._ready = threading.Event()
        self._release = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError("held foreign transaction did not open in time")

    def commit(self) -> None:
        self._release.set()
        self._done.wait(timeout=15)
        self._thread.join(timeout=15)

    def _run(self) -> None:
        async def _main():
            from hyperdjango.database import Database

            db = Database(
                os.environ.get("DATABASE_URL")
                or "postgres://localhost/hyperdjango_test"
            )
            await db.connect()
            try:
                async with db.transaction():
                    await db.execute(
                        "INSERT INTO hm_access_log "
                        "(identity, action, outcome, subject, client_ip, "
                        " auth_method, fingerprint, created_at) "
                        "VALUES ('held', 'probe', 'ok', '', '', '', '', now())"
                    )
                    self._ready.set()
                    while not self._release.is_set():
                        await asyncio.sleep(0.02)
            finally:
                await db.disconnect()
                self._done.set()

        with contextlib.suppress(Exception):
            asyncio.run(_main())
        self._ready.set()
        self._done.set()


# Mirrors services/hypermanager/app.py's transaction-scoped serialize gate so this
# process can hold the gate + an uncommitted ledger row EXACTLY as the server's
# publish path does. The class MUST equal app.HM_LOCK_CLASS and the key
# app.HM_GATE_KEY; a mismatch means this hold takes a DIFFERENT advisory lock than
# the server, so server publishes would not block on it and the no-skip assertion
# fails loudly — drift can never pass silently.
_HM_LOCK_CLASS = 0x484D4556  # "HMEV" — must equal app.HM_LOCK_CLASS
_HM_GATE_KEY = 0  # must equal app.HM_GATE_KEY


class _Rollback(Exception):
    """Sentinel raised inside the held transaction to force a ROLLBACK (burn)."""


class _HeldGatePublishTx:
    """Open a publish-shaped transaction in THIS process that takes the
    transaction-scoped serialize gate (HM_LOCK_CLASS, HM_GATE_KEY) as its first
    statement and inserts an uncommitted ledger row, holding both until released.

    While held, the gate blocks EVERY server publish (they serialize on the same
    lock) and the uncommitted row is invisible to the max(committed id) ceiling —
    which is exactly how the app guarantees no-skip: no other publish can be
    mid-flight and no uncommitted id can be served. With ``commit=True`` the hold
    COMMITs on release, the id becomes visible, and the ceiling advances to it;
    with ``commit=False`` it ROLLs BACK (burns the id, releasing the gate), so a
    new publish acquires the gate fine and the ceiling steps past the gap
    (liveness — the gate is transaction-scoped and can never leak)."""

    def __init__(self, subject: str, *, commit: bool = True):
        self._subject = subject
        self._commit = commit
        self._reserved: list[int] = []
        self._ready = threading.Event()
        self._release = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> int:
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError("gated publish transaction did not open in time")
        if not self._reserved:
            raise RuntimeError("gated publish transaction failed to insert a row")
        return self._reserved[0]

    def finish(self) -> None:
        """Release the hold: COMMIT (commit=True) or ROLLBACK (commit=False)."""
        self._release.set()
        self._done.wait(timeout=15)
        self._thread.join(timeout=15)

    def _run(self) -> None:
        async def _main():
            from hyperdjango.database import Database

            db = Database(
                os.environ.get("DATABASE_URL")
                or "postgres://localhost/hyperdjango_test"
            )
            await db.connect()
            try:
                async with db.transaction():
                    # Take the serialize gate FIRST, exactly like the app's
                    # publish transaction. It is transaction-scoped, so it is held
                    # until this transaction commits or rolls back and released
                    # automatically then — no explicit unlock.
                    await db.query_val(
                        "SELECT pg_advisory_xact_lock($1::int, $2::int)",
                        _HM_LOCK_CLASS,
                        _HM_GATE_KEY,
                    )
                    # Insert an uncommitted row; SERIAL assigns its id (as the
                    # app's event.save() does), invisible to max(id) until commit.
                    reserved_id = int(
                        await db.query_val(
                            "INSERT INTO hm_events "
                            "(producer, subject, kind, metadata, created_at) "
                            "VALUES ($1, $2, $3, '{}'::jsonb, now()) "
                            "RETURNING id",
                            "producer:hypersecret",
                            self._subject,
                            "created",
                        )
                    )
                    self._reserved.append(reserved_id)
                    self._ready.set()
                    while not self._release.is_set():
                        await asyncio.sleep(0.02)
                    if not self._commit:
                        # Roll the id back (burn it): the transaction's advisory
                        # gate auto-releases on ROLLBACK.
                        raise _Rollback()
                # COMMIT on context exit (commit=True).
            finally:
                await db.disconnect()
                self._done.set()

        with contextlib.suppress(Exception):
            asyncio.run(_main())
        self._ready.set()  # unblock start() even if the insert failed
        self._done.set()


class _BgPublish:
    """Run one ManagerClient.publish on a background thread so the caller can
    assert it BLOCKS while the serialize gate is held elsewhere, then completes
    once the gate is released. (A publish is now serialized end-to-end through the
    transaction-scoped gate, so a synchronous call would deadlock the test while a
    hold is active.)"""

    def __init__(self, client, subject: str, kind: str = "created", metadata=None):
        self._client = client
        self._subject = subject
        self._kind = kind
        self._metadata = metadata or {}
        self._id: list[int] = []
        self._err: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self._id.append(
                self._client.publish(self._subject, self._kind, self._metadata)
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via result()
            self._err.append(exc)

    def start(self) -> _BgPublish:
        self._thread.start()
        return self

    def blocked(self, settle: float = 1.5) -> bool:
        """True if the publish is still in flight after ``settle`` seconds — i.e.
        it is blocked on the gate rather than having returned."""
        self._thread.join(timeout=settle)
        return self._thread.is_alive()

    def result(self, timeout: float = 15) -> int | None:
        self._thread.join(timeout=timeout)
        if self._err:
            raise self._err[0]
        return self._id[0] if self._id else None


def _floor_marker(value: int) -> None:
    """Write the persisted retention-floor marker directly in the shared DB —
    simulates a DIFFERENT replica's trim (finding 8) so a single-process run can
    exercise the on-demand cross-replica floor refresh without a sweep tick."""

    async def _run():
        from hyperdjango.database import Database

        db = Database(
            os.environ.get("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
        )
        await db.connect()
        try:
            await db.execute(
                "INSERT INTO hm_retention_floor (id, floor) VALUES (1, $1) "
                "ON CONFLICT (id) DO UPDATE "
                "SET floor = GREATEST(hm_retention_floor.floor, EXCLUDED.floor)",
                value,
            )
        finally:
            await db.disconnect()

    asyncio.run(_run())


def _delete_min_event() -> int:
    """Directly delete the lowest surviving ledger id (a 'burned/absent first id'
    that was NOT recorded as a retention trim) and return it. Used to prove the
    floor derives from the persisted trim boundary, not min(surviving id)."""

    async def _run() -> int:
        from hyperdjango.database import Database

        db = Database(
            os.environ.get("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
        )
        await db.connect()
        try:
            gone = await db.query_val("SELECT min(id) FROM hm_events")
            if gone is not None:
                await db.execute("DELETE FROM hm_events WHERE id = $1", int(gone))
            return int(gone or 0)
        finally:
            await db.disconnect()

    return asyncio.run(_run())


def provision_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "services.hypersecret.provision", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def setup_db_and_certs() -> None:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)

    result = provision_cli("ca", "init", "--dir", str(CA_DIR))
    if result.returncode != 0:
        raise RuntimeError(f"ca init failed: {result.stderr}")
    for cn, extra in (
        ("localhost", ["--server", "--dns", "localhost,127.0.0.1"]),
        ("service:platform-api", []),
        ("service:unknown-cn", []),
    ):
        out_prefix = str(DEMO_DIR / cn.replace(":", "-"))
        result = provision_cli(
            "cert",
            "issue",
            cn,
            "--ca-dir",
            str(CA_DIR),
            "--out-prefix",
            out_prefix,
            *extra,
        )
        if result.returncode != 0:
            raise RuntimeError(f"cert issue {cn} failed: {result.stderr}")

    env = dict(os.environ)
    env["HYPERMANAGER_DEMO_DIR"] = str(DEMO_DIR)
    result = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypermanager.app:app",
            "--drop",
            "--seed",
            "services.hypermanager.seed:run",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError("hyper setup failed")


def runner_env(*, ledger: bool = True, ring_size: int | None = None) -> dict:
    """Server env for one boot. ``ledger`` selects the durable audited tier that
    the pre-existing suite exercises (its replay/cursor/retention/gapless
    coverage is ledger-only); the default-tier tests boot with ``ledger=False``
    and an explicit ``ring_size`` (a small ring to exercise catch-up + overrun,
    or 0 for the pure-ephemeral tier)."""
    env = dict(os.environ)
    env["HYPERMANAGER_MTLS_LISTEN_PORT"] = str(MTLS_PORT)
    env["HYPERMANAGER_LEDGER_MODE"] = "1" if ledger else "0"
    if ring_size is not None:
        env["HYPERMANAGER_CATCH_UP_RING_SIZE"] = str(ring_size)
    # Drive the ledger-retention sweep on a sub-second cadence so a backdated
    # event is trimmed (and the replay floor raised) within a test's lifetime
    # instead of on the hourly default. The sweep is a no-op until an event is
    # actually older than the retention window, so a fast cadence is harmless.
    env["HYPERMANAGER_RETENTION_SWEEP_INTERVAL"] = "0.5"
    # Enable telemetry so /metrics exposes the live gauges/counters this suite
    # asserts on (Gauge.inc/dec are no-ops when telemetry is disabled), letting
    # the feed-teardown test observe the subscriber gauge cross-process.
    env["HYPER_TELEMETRY_ENABLED"] = "1"
    # /metrics serves a snapshot refreshed by the background drain thread every
    # TELEMETRY_DRAIN_INTERVAL (default 1.0s). At 1.0s the served value is
    # constant between ticks, so a gauge read can settle on a STALE pre-inc
    # snapshot — the feed-teardown test's `connected=0.0` flake. Pin the cadence
    # well below any settle/poll window so the scrape reflects an inc/dec within
    # a test's observation, not up to a second later.
    env["HYPER_TELEMETRY_DRAIN_INTERVAL"] = "0.05"
    # No MTLS_UPSTREAM_PORT: the terminator forwards to the app's real bound
    # port (PORT here) automatically, so it can never desync.
    env["HYPERMANAGER_MTLS_CERT_FILE"] = str(DEMO_DIR / "localhost.crt")
    env["HYPERMANAGER_MTLS_KEY_FILE"] = str(DEMO_DIR / "localhost.key")
    env["HYPERMANAGER_MTLS_CA_FILE"] = str(CA_DIR / "ca.crt")
    return env


# ---------------------------------------------------------------------------


def test_publish_and_replay(tokens):
    print("\n== publish, cursor, grant-filtered replay ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    start_cursor = producer.cursor()
    check("cursor reflects seeded events", start_cursor >= 2)

    event_id = producer.publish(
        "secrets/prod/api/stripe_key", "rotated", {"version": 2}
    )
    check("publish returns cursor id", event_id > start_cursor)

    replay = subscriber.events(after=0)
    subjects = {e["subject"] for e in replay["events"]}
    check(
        "subscriber sees prod events only",
        "secrets/prod/api/stripe_key" in subjects
        and all(s.startswith("secrets/prod/") for s in subjects),
        f"got {subjects}",
    )

    replay = subscriber.events(after=0, prefix="secrets/prod/api/")
    check(
        "prefix param narrows replay",
        all(e["subject"].startswith("secrets/prod/api/") for e in replay["events"])
        and replay["events"],
    )

    expect_raises(
        "subscriber cannot publish",
        ManagerAuthError,
        lambda: subscriber.publish("secrets/prod/api/x", "created", {}),
    )
    expect_raises(
        "producer cannot publish outside grant",
        ManagerAuthError,
        lambda: producer.publish("quota/prod/api", "created", {}),
    )
    expect_raises(
        "invalid subject rejected",
        ManagerError,
        lambda: producer.publish("Bad//Subject", "created", {}),
    )
    expect_raises(
        "invalid kind rejected",
        ManagerError,
        lambda: producer.publish("secrets/prod/api/x", "Rotated!", {}),
    )
    expect_raises(
        "oversized metadata rejected",
        ManagerError,
        lambda: producer.publish("secrets/prod/api/x", "created", {"pad": "x" * 8192}),
    )
    expect_raises(
        "forged token rejected",
        ManagerAuthError,
        lambda: ManagerClient(BASE, token="hmk_forged").cursor(),
    )


def test_sparse_grant_replay(tokens):
    """Regression: a narrow-grant subscriber reconnecting into a ledger padded
    with events it can't see must still replay ALL of its own events. The old
    filter-after-LIMIT code broke the replay loop on a diluted page and
    permanently dropped entitled events."""
    print("\n== sparse-grant replay recovers all entitled events ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    start = subscriber.cursor()
    # Pad the ledger with >1 page of events OUTSIDE the subscriber's grant
    # (staging/), then a handful the subscriber IS entitled to (prod/).
    pad = 600  # > default replay_limit (500)
    for i in range(pad):
        producer.publish(f"secrets/staging/api/pad_{i}", "created", {"version": 1})
    mine = [f"secrets/prod/api/sparse_{i}" for i in range(5)]
    for subject in mine:
        producer.publish(subject, "rotated", {"version": 2})

    # Replay from before the pad: every prod event must come back despite the
    # 600 intervening staging rows.
    got = set()
    cursor = start
    for _ in range(20):  # bounded paging
        page = subscriber.events(after=cursor, limit=500)
        for ev in page["events"]:
            got.add(ev["subject"])
        if page["cursor"] == cursor:
            break
        cursor = page["cursor"]
    check(
        "all entitled events recovered past a >1-page foreign block",
        all(m in got for m in mine),
        f"missing={[m for m in mine if m not in got]}",
    )
    check(
        "foreign-prefix events never leak into replay",
        not any(s.startswith("secrets/staging/") for s in got),
    )

    # And through the watcher's cursor-replay drain (the bug's original home).
    seen: list[dict] = []
    lock = threading.Lock()
    watcher = subscriber.watch(
        ["secrets/prod/"],
        lambda ev: (lock.acquire(), seen.append(ev), lock.release()),
        from_cursor=start,
    )
    check(
        "watcher replay recovers all entitled events past the pad",
        wait_for(
            lambda: all(any(e["subject"] == m for e in seen) for m in mine), timeout=10
        ),
        f"got={sorted({e['subject'] for e in seen})}",
    )
    watcher.stop()


def test_live_feed(tokens):
    print("\n== live watcher: replay-pull delivery + wake + dedup ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    seen: list[dict] = []
    seen_lock = threading.Lock()

    def on_event(ev):
        with seen_lock:
            seen.append(ev)

    watcher = subscriber.watch(["secrets/prod/"], on_event, from_cursor=0)
    check(
        "replay delivered on connect",
        wait_for(
            lambda: any(e["subject"] == "secrets/prod/api/stripe_key" for e in seen)
        ),
        f"seen={[e['subject'] for e in seen]}",
    )

    before = len(seen)
    producer.publish("secrets/prod/api/db_password", "rotated", {"version": 3})
    check(
        "publish converges live (wake → replay pull)",
        wait_for(
            lambda: any(
                e["subject"] == "secrets/prod/api/db_password"
                and e["kind"] == "rotated"
                for e in seen[before:]
            )
        ),
    )
    # Out-of-grant filtering: publish an out-of-grant event, then an in-grant
    # SENTINEL with a higher ledger id. The watcher delivers in ledger order, so
    # the sentinel's arrival proves the drain scanned past the out-of-grant id —
    # if that event were deliverable it would already have arrived (it isn't:
    # cursor replay is grant-filtered in SQL, so it is never returned at all).
    producer.publish("secrets/staging/api/db_password", "rotated", {"version": 9})
    sentinel_id = producer.publish("secrets/prod/api/sentinel", "created", {"v": 1})
    check(
        "in-grant sentinel arrives",
        wait_for(lambda: any(e["id"] == sentinel_id for e in seen)),
        f"seen ids={[e['id'] for e in seen]}",
    )
    check(
        "out-of-grant events never delivered",
        all(e["subject"].startswith("secrets/prod/") for e in seen),
    )
    ids = [e["id"] for e in seen]
    check("no duplicate deliveries", len(ids) == len(set(ids)))

    # Reconnect resume: remember cursor, miss two events, catch up.
    cursor = watcher.cursor
    watcher.stop()
    producer.publish("secrets/prod/api/jwt_secret", "rotated", {"version": 2})
    producer.publish("secrets/prod/api/jwt_secret", "rotated", {"version": 3})
    resumed: list[dict] = []
    watcher2 = subscriber.watch(
        ["secrets/prod/"], lambda ev: resumed.append(ev), from_cursor=cursor
    )
    check(
        "resume replays missed events",
        wait_for(
            lambda: (
                len([e for e in resumed if e["subject"].endswith("jwt_secret")]) == 2
            )
        ),
        f"resumed={[(e['subject'], e['kind']) for e in resumed]}",
    )
    watcher2.stop()


def test_publish_idempotency(tokens):
    """A publish carrying a dedupe key is an idempotent POST: re-POSTing the
    same key (a transport retry storm, a drainer restart) collapses to one
    ledger row and returns the existing id; a fresh key appends."""
    print("\n== publish idempotency: one dedupe key → one ledger row ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])
    subject = "secrets/prod/api/idempotent_probe"
    key = "dedupe-probe-key-1"

    ids = [
        producer._request(
            "POST",
            "/v1/events",
            body={
                "subject": subject,
                "kind": "rotated",
                "metadata": {"version": 1},
                "dedupe_key": key,
            },
        )["id"]
        for _ in range(6)
    ]
    check("re-POST of one key returns a single stable id", len(set(ids)) == 1, f"{ids}")

    def rows_for(subj):
        return [e for e in subscriber.events(after=0)["events"] if e["subject"] == subj]

    check(
        "exactly one ledger row for the deduped subject",
        len(rows_for(subject)) == 1,
        f"rows={len(rows_for(subject))}",
    )
    new_id = producer._request(
        "POST",
        "/v1/events",
        body={
            "subject": subject,
            "kind": "rotated",
            "metadata": {"version": 2},
            "dedupe_key": "dedupe-probe-key-2",
        },
    )["id"]
    check("a distinct dedupe key appends a new row", new_id != ids[0])
    check("two rows after the distinct-key append", len(rows_for(subject)) == 2)
    # publish() mints a fresh key per logical call, so two calls are two rows.
    a = producer.publish(subject, "rotated", {"v": 3})
    b = producer.publish(subject, "rotated", {"v": 4})
    check("publish() mints a fresh key per call", a != b)


def test_concurrent_publish_dedupe(tokens):
    """The headline race: a truly-concurrent double-POST of the SAME
    (producer, dedupe_key) must collapse to one ledger row and hand BOTH callers
    the same id — no 500, no `error` audit row. Before get_or_create became
    race-safe on the native path, the loser's raw unique-violation RuntimeError
    escaped the handler as an audited `error` + HTTP 500, contradicting the
    idempotency-under-concurrent-double-POST contract."""
    print("\n== concurrent same-key publish → one id, no 500, no error row ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    subject = "secrets/prod/api/concurrent_probe"
    key = "concurrent-dedupe-key-1"
    fanout = 8
    ids: list[int] = []
    errors: list[str] = []
    result_lock = threading.Lock()
    barrier = threading.Barrier(fanout)

    def worker():
        # A dedicated client per thread; release every POST at the barrier so the
        # inserts actually race for the (producer, dedupe_key) unique constraint.
        client = ManagerClient(BASE, token=tokens["producer:hypersecret"])
        barrier.wait()
        try:
            resp = client._request(
                "POST",
                "/v1/events",
                body={
                    "subject": subject,
                    "kind": "created",
                    "metadata": {"version": 1},
                    "dedupe_key": key,
                },
            )
            with result_lock:
                ids.append(resp["id"])
        except Exception as exc:  # noqa: BLE001 - a 500 here is the bug under test
            with result_lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(fanout)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no concurrent same-key publish raised (no 500)", not errors, f"{errors}")
    check(
        "every concurrent caller got the SAME ledger id",
        len(ids) == fanout and len(set(ids)) == 1,
        f"ids={ids}",
    )

    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])
    rows = [
        e
        for e in subscriber.events(after=0, limit=500)["events"]
        if e["subject"] == subject
    ]
    check(
        "exactly one ledger row for the concurrently-deduped subject",
        len(rows) == 1,
        f"rows={len(rows)}",
    )
    errored = ops._request(
        "GET",
        "/v1/audit",
        query={"action": "publish", "outcome": "error", "limit": "200"},
    )["entries"]
    check(
        "the race produced no publish `error` audit row",
        all(e["subject"] != subject for e in errored),
        f"error publish rows={[(e['identity'], e['subject']) for e in errored]}",
    )


def test_multi_domain_ordering(tokens):
    """The high-severity ordering property, proven dead: publish concurrently
    across MULTIPLE first-segment domains (distinct wake shards) and assert a
    multi-domain subscriber's watcher delivers exactly ledger order — no loss,
    no dups. Ordering holds because the watcher never trusts a live wake for
    delivery; it pulls contiguous cursor-replay pages in id order."""
    print("\n== ordering: concurrent multi-domain publishes stay in ledger order ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    domains = ["alpha", "beta", "gamma"]
    pub = ops._request(
        "POST",
        "/v1/admin/identities",
        body={"name": "producer:multi", "scopes": "feed"},
    )
    sub = ops._request(
        "POST", "/v1/admin/identities", body={"name": "service:multi", "scopes": "feed"}
    )
    for d in domains:
        ops._request(
            "POST",
            "/v1/admin/grants",
            body={"identity": "producer:multi", "prefix": f"{d}/", "publish": True},
        )
        ops._request(
            "POST",
            "/v1/admin/grants",
            body={"identity": "service:multi", "prefix": f"{d}/", "subscribe": True},
        )
    producer = ManagerClient(BASE, token=pub["token"])
    subscriber = ManagerClient(BASE, token=sub["token"])

    seen: list[dict] = []
    seen_lock = threading.Lock()
    watcher = subscriber.watch(
        [f"{d}/" for d in domains],
        lambda ev: (seen_lock.acquire(), seen.append(ev), seen_lock.release()),
        from_cursor=0,
    )

    per_domain = 25
    published: list[int] = []
    pub_lock = threading.Lock()

    def worker(domain):
        for i in range(per_domain):
            eid = producer.publish(f"{domain}/svc/key_{i}", "rotated", {"version": i})
            with pub_lock:
                published.append(eid)

    threads = [threading.Thread(target=worker, args=(d,)) for d in domains]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = len(domains) * per_domain

    check(
        "every published event is delivered",
        wait_for(lambda: len(seen) >= expected, timeout=20),
        f"delivered={len(seen)}/{expected}",
    )
    with seen_lock:
        delivered = [e["id"] for e in seen]
    check(
        "delivered strictly in ascending ledger order",
        delivered == sorted(delivered),
        f"first out-of-order near {_first_disorder(delivered)}",
    )
    check("no duplicate deliveries", len(delivered) == len(set(delivered)))
    check(
        "no loss: exactly the published set delivered",
        set(delivered) == set(published),
        f"missing={sorted(set(published) - set(delivered))[:10]}",
    )
    watcher.stop()

    # Low (a): a multi-prefix watch must deliver ONLY the requested prefixes, not
    # the caller's grants-wide set. The subscriber is granted alpha/beta/gamma
    # but requests only alpha/ and beta/; a gamma publish must never be delivered
    # even though the grant covers it.
    narrowed: list[dict] = []
    narrow_lock = threading.Lock()
    watcher2 = subscriber.watch(
        ["alpha/", "beta/"],
        lambda ev: (narrow_lock.acquire(), narrowed.append(ev), narrow_lock.release()),
        from_cursor=0,
    )
    gamma_id = producer.publish("gamma/svc/late_key", "rotated", {"version": 99})
    alpha_id = producer.publish("alpha/svc/late_key", "rotated", {"version": 99})
    check(
        "multi-prefix watch delivers a requested-prefix event",
        wait_for(lambda: any(e["id"] == alpha_id for e in narrowed), timeout=15),
    )
    check(
        "multi-prefix watch never delivers a non-requested grant-covered event",
        all(e["id"] != gamma_id for e in narrowed)
        and not any(e["subject"].startswith("gamma/") for e in narrowed),
        f"leaked gamma={[e['subject'] for e in narrowed if e['subject'].startswith('gamma/')]}",
    )
    watcher2.stop()


def _first_disorder(seq):
    for i in range(1, len(seq)):
        if seq[i] < seq[i - 1]:
            return (seq[i - 1], seq[i])
    return None


def test_admin(tokens):
    print("\n== admin: identity + grant lifecycle ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    created = ops._request(
        "POST", "/v1/admin/identities", body={"name": "temp:subscriber"}
    )
    check("identity minted with hmk_ token", created["token"].startswith("hmk_"))
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "temp:subscriber",
            "prefix": "secrets/prod/",
            "subscribe": True,
        },
    )
    temp = ManagerClient(BASE, token=created["token"])
    check("granted identity replays", temp.events(after=0)["events"] != [])

    review = ops._request("GET", "/v1/admin/grants")
    check(
        "grant review lists identity",
        any(g["identity"] == "temp:subscriber" for g in review["grants"]),
    )
    ops._request("DELETE", "/v1/admin/identities/temp:subscriber")
    expect_raises("revoked token → 401", ManagerAuthError, lambda: temp.cursor())

    expect_raises(
        "non-admin denied admin API",
        ManagerAuthError,
        lambda: ManagerClient(BASE, token=tokens["service:platform-api"])._request(
            "POST", "/v1/admin/identities", body={"name": "x:y"}
        ),
    )


def test_retention_reset_signal(tokens):
    """Replay carries a `reset` flag. On a fresh (un-trimmed) ledger the floor
    is 0, so an in-window replay is never a reset. This test asserts that wire
    contract only: the response includes a `reset` field and it is False when
    the cursor is at or above the retention floor. The reset=True path (after <
    floor, once retention_sweep raises the floor) is covered separately."""
    print("\n== retention: replay carries a reset flag ==")
    sub = ManagerClient(BASE, token=tokens["service:platform-api"])
    page = sub.events(after=0, limit=10)
    check("replay response carries a reset flag", "reset" in page)
    check("in-window replay is not a reset (floor 0)", page["reset"] is False)


def test_feed_scope_required(tokens):
    """The 'feed' scope gates the whole change-notification API. An identity
    that holds a subscribe grant but lacks 'feed' is still denied on replay and
    on WS connect — proving the scope is a real enforcement point, not a dead
    capability string."""
    print("\n== feed scope enforced on replay and WS connect ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    created = ops._request(
        "POST",
        "/v1/admin/identities",
        body={"name": "service:no-feed-scope", "scopes": "probe"},
    )
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "service:no-feed-scope",
            "prefix": "secrets/prod/",
            "subscribe": True,
        },
    )
    no_feed = ManagerClient(BASE, token=created["token"])
    expect_raises(
        "replay without feed scope → 403",
        ManagerAuthError,
        lambda: no_feed.events(after=0),
    )
    # The watcher's replay pulls (and its WS upgrade) both require the feed
    # scope, so a no-feed identity is delivered nothing even while a covered
    # subject changes under it.
    seen: list[dict] = []
    watcher = no_feed.watch(
        ["secrets/prod/"], lambda ev: seen.append(ev), from_cursor=0
    )
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    producer.publish("secrets/prod/api/scope_probe", "created", {"version": 1})
    check(
        "no-feed-scope watcher receives no wakes and no replay rows",
        not wait_for(lambda: bool(seen), timeout=3),
        f"leaked={[e.get('subject') for e in seen]}",
    )
    watcher.stop()


def test_audit_trail(tokens):
    print("\n== audit trail: gated actions and denials recorded ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])

    producer.publish("secrets/prod/api/audited", "created", {"version": 1})
    # A grant denial: the subscriber holds no publish grant on this subject.
    # The old gate wrote "ok" the moment authN+scope passed, before this check,
    # so the denial produced no row; now the handler audits its true outcome.
    denied_subject = "secrets/prod/api/x"
    with contextlib.suppress(Exception):
        ManagerClient(BASE, token=tokens["service:platform-api"]).publish(
            denied_subject, "created", {}
        )
    # No sleep: the /v1/audit handler flushes pending rows before reading
    # (read-your-writes), so every row above is already durable when queried.

    audit = ops._request("GET", "/v1/audit", query={"limit": "200"})
    entries = audit["entries"]
    check("audit trail non-empty", len(entries) > 0)
    check(
        "publish by producer recorded",
        any(
            e["identity"] == "producer:hypersecret" and e["action"] == "publish"
            for e in entries
        ),
    )
    check(
        "auth method recorded (token)",
        any(e["auth_method"] == "token" for e in entries),
    )
    check(
        "publish rows record the subject",
        any(
            e["action"] == "publish"
            and e["outcome"] == "ok"
            and e["subject"] == "secrets/prod/api/audited"
            for e in entries
        ),
        f"publish rows={[(e['outcome'], e['subject']) for e in entries if e['action'] == 'publish']}",
    )
    denied_pub = ops._request(
        "GET",
        "/v1/audit",
        query={"action": "publish", "outcome": "denied", "limit": "50"},
    )["entries"]
    check(
        "grant-denied publish audited as denied, not ok, with its subject",
        any(e["subject"] == denied_subject for e in denied_pub),
        f"denied publish rows={[(e['identity'], e['subject']) for e in denied_pub]}",
    )
    admin_rows = ops._request(
        "GET", "/v1/audit", query={"action": "admin", "limit": "100"}
    )["entries"]
    check(
        "admin rows record the target",
        any(e["subject"] == "temp:subscriber" for e in admin_rows),
        f"admin rows={[(e['outcome'], e['subject']) for e in admin_rows]}",
    )
    denials = ops._request(
        "GET", "/v1/audit", query={"outcome": "denied", "limit": "50"}
    )
    check("denials recorded", len(denials["entries"]) > 0)

    expect_raises(
        "non-admin denied audit query",
        ManagerAuthError,
        lambda: ManagerClient(BASE, token=tokens["service:platform-api"])._request(
            "GET", "/v1/audit"
        ),
    )


def test_retention_reset(tokens):
    """The retention machinery, driven through its real purpose. Age a prefix of
    the ledger past the window; the server's sweep trims those rows and lifts its
    replay floor. Then prove both reset surfaces fire: (a) an HTTP replay from a
    below-floor cursor returns reset=true and floors past the trimmed block, and
    (b) a ChangeFeedWatcher consuming from a stale cursor has on_reset invoked
    and then resumes delivering the post-floor survivors in ledger order.

    Runs last: raising the floor trims the whole earlier ledger, so no other
    test may depend on those rows afterward."""
    print("\n== retention: sweep raises floor → replay + watcher reset ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    # A block to trim, then the survivors that must still replay past the floor.
    stale_ids = [
        producer.publish(f"secrets/prod/api/stale_{i}", "created", {"v": i})
        for i in range(5)
    ]
    floor = stale_ids[-1]
    survivors = [
        producer.publish(f"secrets/prod/api/fresh_{i}", "rotated", {"v": i})
        for i in range(4)
    ]

    # Age everything at/below `floor` far past any retention window; the server's
    # fast sweep trims those rows and lifts _retention_floor to `floor`.
    _backdate_events(floor, datetime.now(UTC) - timedelta(days=3650))

    check(
        "sweep raised the floor: a below-floor replay signals reset",
        wait_for(lambda: subscriber.events(after=0, limit=500)["reset"] is True, 15),
    )
    page = subscriber.events(after=0, limit=500)
    got_ids = [e["id"] for e in page["events"]]
    check("reset replay carries reset=true", page["reset"] is True)
    check(
        "trimmed events are not replayed (floor applied)",
        all(i > floor for i in got_ids),
        f"got={got_ids[:10]} floor={floor}",
    )
    check(
        "survivors replay past the floor",
        all(s in got_ids for s in survivors),
        f"missing={[s for s in survivors if s not in got_ids]}",
    )

    # (b) A stale-cursor watcher: on_reset fires, then it resumes in order.
    reset_events: list[dict] = []
    seen: list[dict] = []
    lock = threading.Lock()
    watcher = subscriber.watch(
        ["secrets/prod/"],
        lambda ev: (lock.acquire(), seen.append(ev), lock.release()),
        from_cursor=0,
        on_reset=lambda resp: reset_events.append(resp),
    )
    check(
        "stale-cursor watcher fires on_reset",
        wait_for(lambda: bool(reset_events), 15),
    )
    check(
        "watcher resumes and delivers every post-floor survivor",
        wait_for(lambda: all(any(e["id"] == s for e in seen) for s in survivors), 15),
        f"got={sorted(e['id'] for e in seen)}",
    )
    with lock:
        delivered = [e["id"] for e in seen]
    check(
        "watcher delivers post-floor events in ascending ledger order",
        delivered == sorted(delivered),
    )
    check("watcher never delivers a trimmed event", all(i > floor for i in delivered))
    watcher.stop()


def test_mtls(tokens):
    print("\n== mTLS: certificate identity over the terminator ==")
    ca = str(CA_DIR / "ca.crt")
    cert_client = ManagerClient(
        MTLS_BASE,
        ca_file=ca,
        client_cert_file=str(DEMO_DIR / "service-platform-api.crt"),
        client_key_file=str(DEMO_DIR / "service-platform-api.key"),
    )
    check("cert-authenticated cursor (no token)", cert_client.cursor() > 0)
    replay = cert_client.events(after=0)
    check(
        "cert identity gets its grants",
        replay["events"]
        and all(e["subject"].startswith("secrets/prod/") for e in replay["events"]),
    )

    unknown = ManagerClient(
        MTLS_BASE,
        ca_file=ca,
        client_cert_file=str(DEMO_DIR / "service-unknown-cn.crt"),
        client_key_file=str(DEMO_DIR / "service-unknown-cn.key"),
    )
    expect_raises(
        "valid cert with unknown CN → 401", ManagerAuthError, lambda: unknown.cursor()
    )

    # Feed over the terminator: WebSocket wake upgrade → raw TLS splice, then
    # cursor-replay pull over the same mTLS transport.
    seen: list[dict] = []
    watcher = cert_client.watch(
        ["secrets/prod/"], lambda ev: seen.append(ev), from_cursor=0
    )
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    producer.publish("secrets/prod/api/stripe_key", "rewrapped", {"kek_id": "v2"})
    check(
        "mTLS feed converges on a live event",
        wait_for(lambda: any(e["kind"] == "rewrapped" for e in seen)),
    )
    watcher.stop()

    # Forged attestation headers on the plaintext port are worthless.
    resp = http_get(
        f"{BASE}/v1/cursor",
        headers={
            "X-Hyper-MTLS-Attest": "forged",
            "X-Hyper-MTLS-CN": "service:platform-api",
        },
    )
    check("forged attestation headers → 401", resp.status == 401)

    # Fingerprint pinning REJECTION over the real terminator: pin the identity to
    # a fingerprint that is NOT this client cert's. A CA-valid cert with the right
    # CN but an unpinned fingerprint is then refused (403), never admitted — the
    # network-layer trust (handshake passed) does not override the app-layer pin.
    _set_cert_fingerprint("service:platform-api", "0" * 64)
    try:
        expect_raises(
            "CA-valid cert with an unpinned fingerprint → 403",
            ManagerAuthError,
            lambda: ManagerClient(
                MTLS_BASE,
                ca_file=ca,
                client_cert_file=str(DEMO_DIR / "service-platform-api.crt"),
                client_key_file=str(DEMO_DIR / "service-platform-api.key"),
            ).cursor(),
        )
    finally:
        _set_cert_fingerprint("service:platform-api", "")


def test_cross_producer_dedupe(tokens):
    """Regression (dedupe_key was globally unique): two DIFFERENT producers
    using the same natural key must EACH land their own event. The old global
    unique key made the second producer's get_or_create return the first
    producer's row, so its event was silently lost (and 200-vs-201 disclosed
    foreign-key existence). Dedup is now scoped per producer."""
    print("\n== cross-producer dedupe: same key, two producers, no loss ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    # producer:hypersecret (A) already holds a publish grant on secrets/prod/.
    a = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    # Mint a second producer (B) with its own publish grant on the same subtree.
    b_created = ops._request(
        "POST",
        "/v1/admin/identities",
        body={"name": "producer:second", "scopes": "feed"},
    )
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "producer:second",
            "prefix": "secrets/prod/",
            "publish": True,
        },
    )
    b = ManagerClient(BASE, token=b_created["token"])

    shared_key = "outbox-shared-42"
    id_a = a._request(
        "POST",
        "/v1/events",
        body={
            "subject": "secrets/prod/api/collide_a",
            "kind": "created",
            "metadata": {},
            "dedupe_key": shared_key,
        },
    )["id"]
    id_b = b._request(
        "POST",
        "/v1/events",
        body={
            "subject": "secrets/prod/api/collide_b",
            "kind": "created",
            "metadata": {},
            "dedupe_key": shared_key,
        },
    )["id"]
    check(
        "same key across producers yields distinct ledger ids",
        id_a != id_b,
        f"id_a={id_a} id_b={id_b}",
    )

    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])
    subjects = {e["subject"] for e in subscriber.events(after=0, limit=500)["events"]}
    check(
        "both producers' events land (neither swallowed)",
        "secrets/prod/api/collide_a" in subjects
        and "secrets/prod/api/collide_b" in subjects,
        f"collide_a={'secrets/prod/api/collide_a' in subjects} "
        f"collide_b={'secrets/prod/api/collide_b' in subjects}",
    )
    # Per-producer idempotency still holds: producer A re-POSTing its own key
    # returns its own existing id, not a new row.
    id_a2 = a._request(
        "POST",
        "/v1/events",
        body={
            "subject": "secrets/prod/api/collide_a",
            "kind": "created",
            "metadata": {},
            "dedupe_key": shared_key,
        },
    )["id"]
    check("same producer + key is still idempotent", id_a2 == id_a)


def test_trailing_slash_replay(tokens):
    """Regression (finding 5): a subtree-only grant "x/" must NOT deliver the
    exact node "x" on cursor replay. The live-wake filter already excluded it;
    replay must agree (single matching rule)."""
    print("\n== trailing-slash grant excludes the exact node on replay ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    created = ops._request(
        "POST",
        "/v1/admin/identities",
        body={"name": "service:subtree-only", "scopes": "feed"},
    )
    # Subtree-only grant: "quota/prod/" covers quota/prod/... but NOT "quota/prod".
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "service:subtree-only",
            "prefix": "quota/prod/",
            "subscribe": True,
        },
    )
    # Grant producer:hypersecret publish on quota/ so it can produce both nodes.
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={"identity": "producer:hypersecret", "prefix": "quota/", "publish": True},
    )
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    exact_id = producer.publish("quota/prod", "created", {"v": 1})  # the exact node
    child_id = producer.publish("quota/prod/api/tokens", "created", {"v": 1})

    sub = ManagerClient(BASE, token=created["token"])
    got = {e["subject"] for e in sub.events(after=0, limit=500)["events"]}
    check(
        "subtree-only replay delivers the child",
        "quota/prod/api/tokens" in got,
        f"got={sorted(got)}",
    )
    check(
        "subtree-only replay EXCLUDES the exact node",
        "quota/prod" not in got,
        f"leaked exact node quota/prod (ids exact={exact_id} child={child_id})",
    )
    # Also via a requested prefix equal to the exact node: coverage is denied, so
    # HTTP replay now 403s (parity with the WS feed's 4003), never widening to
    # deliver it and never leaking an empty-200 that a subtree grant could serve.
    expect_raises(
        "requesting the exact node under a subtree-only grant → 403",
        ManagerAuthError,
        lambda: sub.events(after=0, prefix="quota/prod", limit=500),
    )


def test_cursor_gapless_ceiling(tokens):
    """Regression (finding 6): /v1/cursor and the WS hello must return the
    gapless replay ceiling — max(committed id), never a raw max that could
    include an uncommitted id. With a publish still in flight (gate held, row
    uncommitted) the cursor must NOT include that id — else a watcher adopting the
    cursor would skip the event forever if the publish rolled back, or race ahead
    of its commit."""
    print("\n== cursor returns the gapless ceiling, never an uncommitted id ==")
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    # An in-flight id is one whose publish holds the serialize gate with its row
    # inserted-but-uncommitted (the only way the app creates one).
    held = _HeldGatePublishTx("secrets/prod/api/inflight_lower")
    held_id = held.start()
    try:
        cur = subscriber.cursor()
        check(
            "cursor excludes an uncommitted (in-flight) id",
            cur < held_id,
            f"cursor={cur} held_id={held_id}",
        )
    finally:
        held.finish()  # COMMIT
    # Once the in-flight id commits, the ceiling advances to include it.
    check(
        "cursor advances once the in-flight id commits",
        wait_for(lambda: subscriber.cursor() >= held_id, timeout=10),
        f"cursor={subscriber.cursor()} held_id={held_id}",
    )


def test_exact_inflight_ceiling(tokens):
    """Regression (headline): publishes are serialized by a transaction-scoped
    advisory gate, so SERIAL id order == commit order and max(committed id) is an
    EXACT, gapless ceiling — no id/xid-order guess, no pg_locks scan.

    NO-SKIP: while a publish holds the gate with its row uncommitted, (a) every
    concurrent publish BLOCKS on the gate (they cannot interleave, so no id can
    commit above an uncommitted lower one), and (b) the ceiling excludes the
    uncommitted id. Releasing the gate lets the blocked publish through with a
    strictly higher id and the ceiling advances to include both.

    NO-STALL: a publish that ROLLS BACK burns its id (a permanent gap) and, being
    transaction-scoped, releases the gate automatically — a leak is impossible. A
    later publish acquires the gate fine and the ceiling steps past the gap."""
    print(
        "\n== exact ceiling: gate serializes publishes (no skip); burned id stays live =="
    )
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    # (1) No-skip: hold the gate with a LOWER id uncommitted; a concurrent publish
    # must block on the gate and the ceiling must exclude the uncommitted id.
    held = _HeldGatePublishTx("secrets/prod/api/reserved_lower")
    held_id = held.start()
    bg = _BgPublish(producer, "secrets/prod/api/after_reserved").start()
    try:
        check(
            "a concurrent publish blocks while the gate is held (serialized)",
            bg.blocked(settle=1.5),
            "publish returned while the gate was held (no serialization)",
        )
        cur = subscriber.cursor()
        check(
            "ceiling excludes an uncommitted (in-flight) lower id (no skip)",
            cur < held_id,
            f"cursor={cur} held_id={held_id}",
        )
    finally:
        held.finish()  # COMMIT the lower id, releasing the gate
    high_id = bg.result(timeout=15)  # the blocked publish now completes
    check(
        "the blocked publish completes with a strictly higher id once released",
        high_id is not None and high_id > held_id,
        f"high={high_id} held={held_id}",
    )
    check(
        "ceiling advances to include both once the gate is released",
        wait_for(lambda: subscriber.cursor() >= high_id, timeout=10),
        f"cursor={subscriber.cursor()} high={high_id}",
    )

    # (2) No-stall / liveness: a rolled-back (burned) id leaves a permanent gap
    # but releases the gate; a new publish acquires it fine and the ceiling steps
    # past the burned id (a leaked gate would deadlock every future publish).
    burned = _HeldGatePublishTx("secrets/prod/api/burned_lower", commit=False)
    burned_id = burned.start()
    burned.finish()  # ROLLBACK → burns the id, auto-releases the gate
    high2 = producer.publish("secrets/prod/api/after_burned", "created", {})
    check(
        "a new publish acquires the gate after a rolled-back one (no leak)",
        high2 > burned_id,
        f"high2={high2} burned={burned_id}",
    )
    check(
        "ceiling steps past the burned id (liveness)",
        wait_for(lambda: subscriber.cursor() >= high2, timeout=10),
        f"cursor={subscriber.cursor()} burned={burned_id} high2={high2}",
    )


def test_feed_teardown_release(tokens):
    """Regression (finding 2): a wake send to a dead peer must not leak the
    channel subscription or the subscriber gauge. Open a feed, drop the socket
    (RST), then publish to drive a wake→send that fails on the dead peer; the
    server must still unsubscribe and decrement the gauge on teardown."""
    print("\n== feed teardown releases subscription + gauge on dead-peer send ==")
    # hypermanager_feed_subscribers is a single process-global gauge shared by
    # every connection, and a prior test's server-side .dec() can still be in
    # flight when this test runs — so an ABSOLUTE baseline is inherently racy
    # (that is what made this flake on CI). Instead measure this subscriber's
    # contribution as a DELTA between two quiesced snapshots: the gauge settled
    # WITH the subscriber connected vs settled AFTER its teardown. Because the
    # suite runs tests serially, no other subscriber is added between the two
    # snapshots, so any shared contamination can only stay or drain — never
    # grow. Therefore `connected >= after + 1` holds iff this subscriber was
    # counted while connected AND released on teardown, independent of whatever
    # the gauge floor happens to be. Deterministic, not a settle heuristic.
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])
    conn = subscriber.open_websocket(
        "/ws/feed?" + urllib.parse.urlencode({"prefixes": "secrets/prod/"})
    )
    hello = conn.recv_json()
    check("feed hello received", hello is not None and hello.get("type") == "hello")
    # Wait for the server's .inc() to LAND, not merely for the gauge to sit
    # still: it fires after hello is sent, so a settle-based read can return
    # the pre-connect value — which is exactly how this asserted against
    # connected=0.0 on a slow runner.
    connected = _wait_metric_reaches(
        "hypermanager_feed_subscribers",
        lambda v: v >= 1.0,
        "counted this subscriber",
    )

    # Force an RST on close so the server's next send to this peer fails.
    conn._sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(OSError):
        conn._sock.close()

    # Drive wakes at the now-dead peer: each publish triggers a wake→send that
    # raises, exercising the writer-exception teardown path that must still
    # unsubscribe and decrement the gauge.
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    for i in range(12):
        producer.publish(f"secrets/prod/api/teardown_{i}", "created", {"i": i})
        time.sleep(0.05)

    # And wait for the teardown's .dec() to land, for the same reason.
    after = _wait_metric_reaches(
        "hypermanager_feed_subscribers",
        lambda v: v <= connected - 1.0,
        f"released the subscriber (below {connected - 1.0})",
    )
    check(
        "subscriber counted while connected, released on dead-peer teardown",
        connected >= after + 1,
        f"connected={connected} after={after}",
    )


def test_feed_connect_denials(tokens):
    """Regression (finding 7): a feed-connect denial writes a `denied` audit row
    (and no `ok` row), for both the uncovered-prefix and the no-grants paths."""
    print("\n== feed-connect denials audited as denied ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    # (a) Uncovered requested prefix: subscriber has no grant on quota/prod/.
    conn = subscriber.open_websocket(
        "/ws/feed?" + urllib.parse.urlencode({"prefixes": "quota/prod/"})
    )
    msg = conn.recv_json()
    check(
        "uncovered-prefix feed connect returns an error frame",
        msg is not None and msg.get("type") == "error",
        f"msg={msg}",
    )
    conn.close()

    # (b) No grants at all: an identity with feed scope but zero subscribe grants.
    created = ops._request(
        "POST",
        "/v1/admin/identities",
        body={"name": "service:no-grants", "scopes": "feed"},
    )
    no_grants = ManagerClient(BASE, token=created["token"])
    conn2 = no_grants.open_websocket("/ws/feed")
    msg2 = conn2.recv_json()
    check(
        "no-grants feed connect returns an error frame",
        msg2 is not None and msg2.get("type") == "error",
        f"msg={msg2}",
    )
    conn2.close()

    denied = ops._request(
        "GET",
        "/v1/audit",
        query={"action": "feed_connect", "outcome": "denied", "limit": "100"},
    )["entries"]
    check(
        "uncovered-prefix feed connect audited denied with its prefix",
        any(e["subject"] == "quota/prod/" for e in denied),
        f"denied feed rows={[(e['identity'], e['subject']) for e in denied]}",
    )
    check(
        "no-grants feed connect audited denied",
        any(e["identity"] == "service:no-grants" for e in denied),
        f"denied feed rows={[(e['identity'], e['subject']) for e in denied]}",
    )
    # And the denied identity has no ok feed_connect row for these attempts.
    ng_rows = ops._request(
        "GET",
        "/v1/audit",
        query={
            "action": "feed_connect",
            "identity": "service:no-grants",
            "limit": "50",
        },
    )["entries"]
    check(
        "no-grants feed connect produced no ok row",
        all(e["outcome"] == "denied" for e in ng_rows),
        f"rows={[(e['outcome'], e['subject']) for e in ng_rows]}",
    )


def test_bad_request_bodies(tokens):
    """Regression (low c/d): a non-dict JSON body is a 400 (not a 500), a
    duplicate identity name is a 409, and malformed scopes are a 400."""
    print("\n== malformed request bodies map to 4xx, not 500 ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    expect_raises(
        "non-dict publish body → 400",
        ManagerError,
        lambda: producer._request("POST", "/v1/events", body=[1, 2, 3]),
    )
    expect_raises(
        "non-dict admin body → 400",
        ManagerError,
        lambda: ops._request("POST", "/v1/admin/identities", body=[1, 2]),
    )
    ops._request("POST", "/v1/admin/identities", body={"name": "dup:identity"})
    expect_raises(
        "duplicate identity name → 409 (not 500)",
        ManagerError,
        lambda: ops._request(
            "POST", "/v1/admin/identities", body={"name": "dup:identity"}
        ),
    )
    expect_raises(
        "malformed scopes → 400",
        ManagerError,
        lambda: ops._request(
            "POST",
            "/v1/admin/identities",
            body={"name": "scope:bad", "scopes": "Bad Scope!"},
        ),
    )


def test_retention_floor_monotonic(tokens):
    """Regression (finding 3): when the sweep empties the ledger, the replay
    floor must NOT collapse to 0 — a below-old-floor cursor must still get
    reset=True (not an empty page it mistakes for "caught up").

    Runs dead last: it trims the ENTIRE ledger."""
    print("\n== retention: emptied ledger keeps a monotonic (non-zero) floor ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    last_id = producer.publish("secrets/prod/api/final_before_empty", "created", {})
    # Age EVERYTHING so the sweep trims the whole ledger to empty.
    _backdate_events(last_id, datetime.now(UTC) - timedelta(days=3650))
    check(
        "sweep empties the ledger",
        wait_for(
            lambda: subscriber.events(after=0, limit=500)["events"] == [], timeout=15
        ),
    )
    page = subscriber.events(after=0, limit=500)
    check(
        "an emptied ledger still signals reset for a below-floor cursor",
        page["reset"] is True,
        f"reset={page['reset']} cursor={page['cursor']}",
    )
    check(
        "floor did not collapse to 0 (cursor stayed at the high-water mark)",
        page["cursor"] >= last_id,
        f"cursor={page['cursor']} last_id={last_id}",
    )


def test_ceiling_liveness_foreign_tx(tokens):
    """Regression (finding 1): a long-lived write transaction on a DIFFERENT table
    (here hm_access_log) must NOT hold the ceiling below an already-committed
    hm_events row. The ceiling is now a plain max(committed id) over hm_events, so
    it has no dependency on any other backend's state: a foreign-relation tx never
    takes the HM serialize gate and never writes hm_events, so it can neither
    block publishes nor pin the ceiling — the committed row is served at once."""
    print("\n== ceiling advances past committed rows despite a foreign write tx ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    held = _HeldForeignWriteTx()
    held.start()
    try:
        new_id = producer.publish("secrets/prod/api/foreign_tx_probe", "created", {})
        check(
            "ceiling advances past a committed hm_events row under a foreign open tx",
            wait_for(lambda: subscriber.cursor() >= new_id, timeout=10),
            f"cursor={subscriber.cursor()} new_id={new_id}",
        )
        # And a replay actually returns the row while the foreign tx is still open.
        got = {e["id"] for e in subscriber.events(after=new_id - 1, limit=10)["events"]}
        check("replay serves the committed row under a foreign open tx", new_id in got)
    finally:
        held.commit()


def test_subscribe_before_hello_race(tokens):
    """Regression (finding 4): the feed subscribes to its wake shards BEFORE
    computing/sending the hello ceiling, so an event landing in that window still
    gets a wake frame instead of waiting out the 30s poll floor. Open a watcher
    from the current head and immediately publish, racing the handshake; prompt
    (<8s, well under the poll floor) delivery proves the wake was not lost."""
    print("\n== publish racing the feed handshake still delivers promptly ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])
    ok = True
    for i in range(4):
        seen: list[dict] = []
        lock = threading.Lock()
        watcher = subscriber.watch(
            ["secrets/prod/"],
            lambda ev: (lock.acquire(), seen.append(ev), lock.release()),
            from_cursor=None,  # start at head, then race a publish into the window
        )
        eid = producer.publish(f"secrets/prod/api/race_{i}", "created", {"i": i})
        delivered = wait_for(lambda: any(e["id"] == eid for e in seen), timeout=8)
        watcher.stop()
        if not delivered:
            ok = False
            break
    check(
        "event published into the handshake window delivers well under the poll floor",
        ok,
    )


def test_numeric_param_hardening(tokens):
    """Regression (finding 5): an over-long/out-of-range `after` or `limit` is a
    400 with a denied audit row, never a 500 (bigint overflow / int() digit cap)."""
    print("\n== numeric replay params fail closed as 400, not 500 ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    for label, query in (
        ("over-long after (40 digits)", {"after": "9" * 40, "prefix": "secrets/prod/"}),
        ("over-long after (25 digits)", {"after": "1" * 25, "prefix": "secrets/prod/"}),
        # >INT4_MAX but few digits: the cursor is a 32-bit SERIAL id, so this
        # would overflow the bind — must be a 400, not a 500.
        ("above-INT4 after", {"after": "5000000000", "prefix": "secrets/prod/"}),
        (
            "over-long limit",
            {"after": "0", "limit": "9" * 40, "prefix": "secrets/prod/"},
        ),
    ):
        try:
            subscriber._request("GET", "/v1/events", query=query)
            check(f"{label} rejected", False, "no error raised")
        except ManagerError as exc:
            check(f"{label} → 400 (not 500)", exc.status == 400, f"status={exc.status}")
    # The maximum valid cursor (INT4 max) is accepted and returns an empty page —
    # it binds into the id column without overflow.
    page = subscriber.events(after=2147483647, limit=10)
    check("max-valid cursor (INT4 max) is accepted (empty page)", page["events"] == [])

    denied = ops._request(
        "GET",
        "/v1/audit",
        query={"action": "replay", "outcome": "denied", "limit": "50"},
    )["entries"]
    check(
        "over-long param denials are audited (denied replay row with its prefix)",
        any(e["subject"] == "secrets/prod/" for e in denied),
        f"denied replay rows={[(e['identity'], e['subject']) for e in denied]}",
    )


def test_grant_upsert_race(tokens):
    """Regression (finding 6): a concurrent double-POST of the same
    (identity, prefix) grant is an idempotent upsert — a unique-violation race
    resolves to a 200 update, never a 500."""
    print("\n== concurrent grant upsert is idempotent, never a 500 ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    ops._request("POST", "/v1/admin/identities", body={"name": "service:grant-race"})
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def worker():
        client = ManagerClient(BASE, token=tokens["operator:admin"])
        barrier.wait()
        try:
            client._request(
                "POST",
                "/v1/admin/grants",
                body={
                    "identity": "service:grant-race",
                    "prefix": "secrets/prod/",
                    "subscribe": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent identical grant POSTs never 500", not errors, f"errors={errors}")

    review = ops._request("GET", "/v1/admin/grants")
    matching = [
        g
        for g in review["grants"]
        if g["identity"] == "service:grant-race" and g["prefix"] == "secrets/prod/"
    ]
    check("exactly one grant row after the race", len(matching) == 1, f"{matching}")


def test_identity_reactivation(tokens):
    """Regression (finding 7): a revoked identity name is not permanently
    unmintable — reactivation is an explicit, audited opt-in (`reactivate=true`),
    while a plain re-POST of a revoked (or active) name stays a 409."""
    print("\n== revoked identity reactivates only with an explicit flag ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    created = ops._request(
        "POST", "/v1/admin/identities", body={"name": "service:reactivate-me"}
    )
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "service:reactivate-me",
            "prefix": "secrets/prod/",
            "subscribe": True,
        },
    )
    client = ManagerClient(BASE, token=created["token"])
    check("fresh identity replays", client.events(after=0)["events"] != [])

    ops._request("DELETE", "/v1/admin/identities/service:reactivate-me")
    expect_raises("revoked token → 401", ManagerAuthError, lambda: client.cursor())
    expect_raises(
        "plain re-POST of a revoked name → 409",
        ManagerError,
        lambda: ops._request(
            "POST", "/v1/admin/identities", body={"name": "service:reactivate-me"}
        ),
    )
    resp = ops._request(
        "POST",
        "/v1/admin/identities",
        body={"name": "service:reactivate-me", "reactivate": True},
    )
    check(
        "reactivate=true reactivates the revoked name", resp.get("reactivated") is True
    )
    check(
        "reactivated identity's original token works again",
        wait_for(lambda: client.cursor() >= 0, timeout=5),
    )
    # Reactivating an ALREADY-active name is a 409 (nothing to reactivate).
    expect_raises(
        "reactivate on an active name → 409",
        ManagerError,
        lambda: ops._request(
            "POST",
            "/v1/admin/identities",
            body={"name": "service:reactivate-me", "reactivate": True},
        ),
    )
    reactivations = ops._request(
        "GET",
        "/v1/audit",
        query={"action": "admin", "limit": "100"},
    )["entries"]
    check(
        "reactivation is audited",
        any(
            e["subject"] == "service:reactivate-me" and e["outcome"] == "ok"
            for e in reactivations
        ),
    )


def test_identity_name_grammar(tokens):
    """Regression (finding 9): identity names are grammar-checked, not merely
    length-checked — control chars / unicode / whitespace are 400s."""
    print("\n== identity names are grammar-checked ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    for bad in ("bad name", "svc: nul", "UPPER:case", "sÉrvice:x", "x"):
        expect_raises(
            f"grammar-invalid name {bad!r} → 400",
            ManagerError,
            lambda b=bad: ops._request(
                "POST", "/v1/admin/identities", body={"name": b}
            ),
        )


def test_metrics_authn(tokens):
    """Regression (finding 10): /metrics requires a resolved identity (token or
    cert); /health and /ready stay open."""
    print("\n== /metrics is gated behind an identity; health/ready stay open ==")
    anon = http_get(f"{BASE}/metrics")
    check("unauthenticated /metrics → 401", anon.status == 401, f"status={anon.status}")
    authed = http_get(
        f"{BASE}/metrics",
        headers={"Authorization": f"Bearer {tokens['operator:admin']}"},
    )
    check(
        "authenticated /metrics → 200", authed.status == 200, f"status={authed.status}"
    )
    check("/health stays open", http_get(f"{BASE}/health").status == 200)
    check("/ready stays open", http_get(f"{BASE}/ready").status == 200)


def test_publish_error_outcome(tokens):
    """Regression (finding 12): a post-gate non-HTTPException failure (here a
    metadata string carrying a NUL that JSONB cannot store) is a 500 that is now
    audited with an 'error' outcome instead of vanishing from the trail."""
    print("\n== a post-gate 500 is audited as an error outcome ==")
    ops = ManagerClient(BASE, token=tokens["operator:admin"])
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subject = "secrets/prod/api/nul_probe"
    raised = None
    try:
        producer._request(
            "POST",
            "/v1/events",
            body={"subject": subject, "kind": "created", "metadata": {"k": "a\u0000b"}},
        )
    except ManagerError as exc:
        raised = exc
    check(
        "a NUL-in-metadata publish fails (post-gate server error)",
        raised is not None and (raised.status is None or raised.status >= 400),
        f"raised={raised!r}",
    )
    errored = ops._request(
        "GET",
        "/v1/audit",
        query={"action": "publish", "outcome": "error", "limit": "50"},
    )["entries"]
    check(
        "the failed publish is audited with an 'error' outcome and its subject",
        any(e["subject"] == subject for e in errored),
        f"error publish rows={[(e['identity'], e['subject']) for e in errored]}",
    )


def test_burned_first_id_no_reset(tokens):
    """Regression (finding 3): the replay floor derives from the persisted trim
    boundary, NOT from min(surviving id). Directly removing the lowest ledger id
    (a burned/absent first id that was never a recorded trim) must NOT push the
    floor up — an after=0 replay stays reset=False. Runs before any retention
    sweep raises the floor."""
    print("\n== a burned first id does not trigger a spurious reset ==")
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])
    before = subscriber.events(after=0, limit=10)
    check(
        "no trim has raised the floor yet (baseline reset=False)",
        before["reset"] is False,
    )
    gone = _delete_min_event()
    # timing-window: bounds a NEGATIVE — across ~3 sweep ticks (the suite pins
    # HYPERMANAGER_RETENTION_SWEEP_INTERVAL to 0.5s) the floor must NOT move.
    # There is no condition to wait for: the correct outcome is that nothing
    # happens, so a window is the construct, and no state transition can
    # substitute for it. A slow machine only oversleeps, which grants the sweep
    # MORE chances to misbehave and can therefore only strengthen the check —
    # never flip it green. Overshoot is also safe against a genuine trim:
    # retention_days is 14, so events this suite just published cannot age out
    # of the window no matter how long the sleep actually lasts.
    time.sleep(1.5)
    after = subscriber.events(after=0, limit=10)
    check(
        "removing the lowest id does NOT raise the floor (still reset=False)",
        after["reset"] is False,
        f"burned id={gone} reset={after['reset']} cursor={after['cursor']}",
    )


def test_pg_fanout_delivery_and_floor_refresh(tokens):
    """Findings 8: exercise the PgChannelLayer (cross-replica wake fan-out) path
    end to end (publish → wake → replay delivery), and the on-demand floor
    refresh — a below-floor replay re-reads the persisted floor from the shared
    marker (simulating another replica's trim) without waiting for a sweep tick."""
    print("\n== pg_fanout: PgChannelLayer delivery + on-demand floor refresh ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    seen: list[dict] = []
    lock = threading.Lock()
    watcher = subscriber.watch(
        ["secrets/prod/"],
        lambda ev: (lock.acquire(), seen.append(ev), lock.release()),
        from_cursor=None,
    )
    eid = producer.publish("secrets/prod/api/fanout_probe", "created", {"v": 1})
    check(
        "publish converges over the PgChannelLayer wake path",
        wait_for(lambda: any(e["id"] == eid for e in seen), timeout=15),
        f"seen={[e['id'] for e in seen]}",
    )
    watcher.stop()

    # On-demand floor refresh: simulate a DIFFERENT replica trimming the shared
    # ledger by writing the persisted marker directly. This process never ran a
    # sweep for it (interval is the 3600s default here), so a below-floor replay
    # must pick it up by re-reading the marker on the request path.
    head = subscriber.cursor()
    marker = head + 1000  # a floor comfortably above the current head
    _floor_marker(marker)
    page = subscriber.events(after=0, limit=10)
    check(
        "a below-floor replay honors a peer's persisted trim without a sweep tick",
        page["reset"] is True,
        f"reset={page['reset']} cursor={page['cursor']} marker={marker}",
    )
    check(
        "the refreshed floor is applied (replay floored at the marker)",
        page["cursor"] >= marker,
        f"cursor={page['cursor']} marker={marker}",
    )


# ---------------------------------------------------------------------------
# Default tiers: live in-frame pub/sub (catch-up + ephemeral)
# ---------------------------------------------------------------------------


def _feed_prefix_qs(prefix: str) -> str:
    return "/ws/feed?" + urllib.parse.urlencode({"prefixes": prefix})


def _feed_subscribe(
    conn, prefixes, *, last_seq=None, epoch=None, client_id="e2e-client"
):
    """Send the subscribe frame and return the hub's hello frame."""
    conn.send_json(
        {
            "type": "subscribe",
            "prefixes": prefixes,
            "client_id": client_id,
            "last_seq": last_seq,
            "epoch": epoch,
            "cursor": None,
        }
    )
    return conn.recv_json()


def test_default_catchup_wire(tokens):
    """Default tier (catchup) over the real wire: the hub advertises the catchup
    model in hello, delivers each event IN the frame, replays exactly the missed
    events on reconnect, then goes live."""
    print("\n== default catchup: hello + in-frame delivery + reconnect replay ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    # (a) hello advertises the catchup tier; a fresh subscriber resyncs.
    conn = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello = _feed_subscribe(conn, ["secrets/prod/"], last_seq=None)
    check(
        "hello advertises mode=catchup",
        hello is not None
        and hello.get("type") == "hello"
        and hello.get("mode") == "catchup",
        f"hello={hello}",
    )
    check("fresh subscriber (last_seq=null) resyncs", hello.get("resync") is True)
    anchor = hello.get("seq")
    check(
        "hello carries the in-memory head seq",
        isinstance(anchor, int),
        f"hello={hello}",
    )
    hub_epoch = hello.get("epoch")
    check(
        "hello carries the process-incarnation epoch",
        isinstance(hub_epoch, str) and len(hub_epoch) > 0,
        f"hello={hello}",
    )

    # (b) a live publish is delivered in an event frame carrying the event itself.
    live_id = producer.publish("secrets/prod/api/cat_live", "rotated", {"v": 2})
    frame = conn.recv_json()
    check(
        "live event delivered in an event frame (subject+kind+seq+metadata)",
        frame is not None
        and frame.get("type") == "event"
        and frame.get("seq") == live_id
        and frame.get("subject") == "secrets/prod/api/cat_live"
        and frame.get("kind") == "rotated"
        and frame.get("metadata") == {"v": 2},
        f"frame={frame}",
    )
    conn.close()

    # (c) reconnect resume: miss three events, replay EXACTLY them in order.
    resume_from = live_id
    missed = [
        producer.publish(f"secrets/prod/api/cr_{i}", "created", {"i": i})
        for i in range(3)
    ]
    conn2 = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello2 = _feed_subscribe(conn2, ["secrets/prod/"], last_seq=resume_from)
    check(
        "reconnect within the ring does not resync",
        hello2.get("mode") == "catchup" and hello2.get("resync") is False,
        f"hello2={hello2}",
    )
    replayed = [conn2.recv_json() for _ in range(3)]
    seqs = [f.get("seq") for f in replayed]
    check(
        "reconnect replays exactly the missed events in ascending order",
        all(f is not None and f.get("type") == "event" for f in replayed)
        and seqs == missed,
        f"seqs={seqs} missed={missed}",
    )
    # then live again past the replayed window.
    live2 = producer.publish("secrets/prod/api/cr_live", "rotated", {})
    tail = conn2.recv_json()
    check(
        "after replay the feed streams live events",
        tail is not None and tail.get("type") == "event" and tail.get("seq") == live2,
        f"tail={tail}",
    )
    conn2.close()

    # (d) epoch gate: a reconnect whose last_seq is WITHIN the ring but whose
    # epoch names a different (restarted) incarnation must resync, never replay —
    # the restart-burst misattribution guard, proven over the wire without an
    # actual process restart. The same resume under the correct epoch does NOT
    # resync, so the resync below is the epoch gate, not a ring/floor effect.
    conn3 = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    anchor3 = _feed_subscribe(conn3, ["secrets/prod/"], last_seq=None).get("seq")
    conn3.close()
    stale_epoch = ("x" + hub_epoch)[: len(hub_epoch)] if hub_epoch else "stale"
    conn4 = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello4 = _feed_subscribe(
        conn4, ["secrets/prod/"], last_seq=anchor3, epoch=stale_epoch
    )
    check(
        "a within-ring resume under a foreign epoch resyncs (restart guard)",
        hello4.get("mode") == "catchup" and hello4.get("resync") is True,
        f"hello4={hello4} stale_epoch={stale_epoch}",
    )
    conn4.close()
    conn5 = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello5 = _feed_subscribe(
        conn5, ["secrets/prod/"], last_seq=anchor3, epoch=hub_epoch
    )
    check(
        "the same within-ring resume under the matching epoch does not resync",
        hello5.get("resync") is False,
        f"hello5={hello5}",
    )
    conn5.close()


def test_default_catchup_ring_overrun(tokens):
    """Default tier (catchup): a subscriber that fell further behind than the
    bounded ring is told to full-resync, never served a gap."""
    print("\n== default catchup: falling behind the ring forces a resync ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    conn0 = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    stale = _feed_subscribe(conn0, ["secrets/prod/"], last_seq=None).get("seq")
    conn0.close()

    # Overrun the ring (booted at size 8) so `stale` ages out below the floor.
    for i in range(16):
        producer.publish(f"secrets/prod/api/ov_{i}", "created", {"i": i})

    conn = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello = _feed_subscribe(conn, ["secrets/prod/"], last_seq=stale)
    check(
        "a resume point below the ring floor resyncs",
        hello.get("mode") == "catchup" and hello.get("resync") is True,
        f"hello={hello} stale={stale}",
    )
    conn.close()


def test_default_watcher_delivery(tokens):
    """The tier-agnostic ManagerClient.watch() works unchanged against a
    default-tier (catchup) hub: the mode-aware watcher adopts in-frame delivery
    from the hello and delivers live events with no per-tier code."""
    print("\n== default catchup: mode-aware watcher delivers live events ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    seen: list[dict] = []
    lock = threading.Lock()
    watcher = subscriber.watch(
        ["secrets/prod/"],
        lambda ev: (lock.acquire(), seen.append(ev), lock.release()),
        from_cursor=None,
    )

    # Publish on each poll tick until one is observed: a publish that raced ahead
    # of the (resync-on-connect) handshake is not replayed, so keep nudging until
    # the watcher is live — then delivery is immediate and in-frame.
    def _pub_and_seen():
        producer.publish("secrets/prod/api/watch_live", "ping", {"n": len(seen)})
        return any(e.get("subject") == "secrets/prod/api/watch_live" for e in seen)

    check(
        "watcher delivers a live event in-frame against a catchup hub",
        wait_for(_pub_and_seen, timeout=15, interval=0.3),
        f"seen={[e.get('subject') for e in seen]}",
    )
    check(
        "the delivered frame carries the event itself (kind)",
        any(e.get("kind") == "ping" for e in seen),
        f"seen kinds={[e.get('kind') for e in seen]}",
    )
    watcher.stop()


def test_ephemeral_tier(tokens):
    """Ephemeral tier (ring_size=0): live in-frame delivery, and every
    (re)connect resyncs — the hub keeps no per-client catch-up state — and the
    tier-agnostic watcher still delivers."""
    print("\n== ephemeral tier: live delivery, always resync on connect ==")
    producer = ManagerClient(BASE, token=tokens["producer:hypersecret"])
    subscriber = ManagerClient(BASE, token=tokens["service:platform-api"])

    conn = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello = _feed_subscribe(conn, ["secrets/prod/"], last_seq=None)
    check(
        "hello advertises mode=ephemeral and resync",
        hello is not None
        and hello.get("mode") == "ephemeral"
        and hello.get("resync") is True,
        f"hello={hello}",
    )
    live_id = producer.publish("secrets/prod/api/eph_1", "created", {"v": 1})
    frame = conn.recv_json()
    check(
        "ephemeral delivers a live event in an event frame",
        frame is not None
        and frame.get("type") == "event"
        and frame.get("seq") == live_id,
        f"frame={frame}",
    )
    conn.close()

    # A reconnect carrying a real last_seq still resyncs — no ring to replay from.
    conn2 = subscriber.open_websocket(_feed_prefix_qs("secrets/prod/"))
    hello2 = _feed_subscribe(conn2, ["secrets/prod/"], last_seq=live_id)
    check(
        "ephemeral resyncs on every connect (even with a last_seq)",
        hello2.get("resync") is True,
        f"hello2={hello2}",
    )
    conn2.close()

    # The tier-agnostic watcher delivers against the ephemeral hub too.
    seen: list[dict] = []
    lock = threading.Lock()
    watcher = subscriber.watch(
        ["secrets/prod/"],
        lambda ev: (lock.acquire(), seen.append(ev), lock.release()),
        from_cursor=None,
    )

    def _pub_and_seen():
        producer.publish("secrets/prod/api/eph_watch", "ping", {})
        return any(e.get("subject") == "secrets/prod/api/eph_watch" for e in seen)

    check(
        "watcher delivers a live event against an ephemeral hub",
        wait_for(_pub_and_seen, timeout=15, interval=0.3),
        f"seen={[e.get('subject') for e in seen]}",
    )
    watcher.stop()


# ---------------------------------------------------------------------------


def main() -> bool:
    global _SCRAPE_TOKEN
    print(f"HyperManager E2E — port {PORT} (mTLS {MTLS_PORT})")
    setup_db_and_certs()
    tokens = json.loads((DEMO_DIR / "tokens.json").read_text())
    _SCRAPE_TOKEN = tokens["operator:admin"]  # authenticated /metrics scrape

    with AppRunner(
        "services.hypermanager.app:app",
        host="127.0.0.1",
        port=PORT,
        readiness_path="/ready",
        env=runner_env(),
    ):
        test_publish_and_replay(tokens)
        test_cross_producer_dedupe(tokens)
        test_trailing_slash_replay(tokens)
        test_cursor_gapless_ceiling(tokens)
        test_exact_inflight_ceiling(tokens)
        test_ceiling_liveness_foreign_tx(tokens)
        test_sparse_grant_replay(tokens)
        test_live_feed(tokens)
        test_subscribe_before_hello_race(tokens)
        test_feed_teardown_release(tokens)
        test_publish_idempotency(tokens)
        test_concurrent_publish_dedupe(tokens)
        test_multi_domain_ordering(tokens)
        test_admin(tokens)
        test_bad_request_bodies(tokens)
        test_numeric_param_hardening(tokens)
        test_grant_upsert_race(tokens)
        test_identity_reactivation(tokens)
        test_identity_name_grammar(tokens)
        test_metrics_authn(tokens)
        test_feed_scope_required(tokens)
        test_feed_connect_denials(tokens)
        test_publish_error_outcome(tokens)
        test_retention_reset_signal(tokens)
        test_audit_trail(tokens)
        test_mtls(tokens)
        # Burned/absent low id must not raise the floor — run before any trim.
        test_burned_first_id_no_reset(tokens)
        # Retention: raising the floor trims the entire earlier ledger.
        test_retention_reset(tokens)
        # Dead last in this run: empties the ledger completely.
        test_retention_floor_monotonic(tokens)

    # Second boot in cross-replica fan-out mode (PgChannelLayer): exercise the
    # LISTEN/NOTIFY wake path and the on-demand floor refresh. A high sweep
    # interval keeps the sweep from firing, so the floor refresh under test is
    # provably the request path, not a sweep tick.
    fanout_env = runner_env()
    fanout_env["HYPERMANAGER_PG_FANOUT"] = "1"
    fanout_env["HYPERMANAGER_RETENTION_SWEEP_INTERVAL"] = "3600"
    with AppRunner(
        "services.hypermanager.app:app",
        host="127.0.0.1",
        port=PORT,
        readiness_path="/ready",
        env=fanout_env,
    ):
        test_pg_fanout_delivery_and_floor_refresh(tokens)

    # Default tier — catch-up (ledger off, small ring): live in-frame delivery,
    # reconnect catch-up replay, and ring-overrun resync. A small ring (8)
    # exercises both the replay and the overrun paths without thousands of
    # publishes. The auth/grant tables from the shared DB are reused as-is.
    with AppRunner(
        "services.hypermanager.app:app",
        host="127.0.0.1",
        port=PORT,
        readiness_path="/ready",
        env=runner_env(ledger=False, ring_size=8),
    ):
        test_default_catchup_wire(tokens)
        test_default_catchup_ring_overrun(tokens)
        test_default_watcher_delivery(tokens)

    # Default tier — ephemeral (ledger off, ring 0): live delivery, always resync.
    with AppRunner(
        "services.hypermanager.app:app",
        host="127.0.0.1",
        port=PORT,
        readiness_path="/ready",
        env=runner_env(ledger=False, ring_size=0),
    ):
        test_ephemeral_tier(tokens)

    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    if ERRORS:
        print("Failures:")
        for err in ERRORS:
            print(f"  - {err}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
