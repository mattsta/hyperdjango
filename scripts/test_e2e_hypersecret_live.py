"""
E2E: HyperSecret × HyperManager live integration.

# hyper-test: e2e

Boots BOTH apps against one database and proves the no-restart convergence
story end to end:

  - secret writes/rotations/rewraps/deletes publish metadata-only change
    events to the hub (values never leave HyperSecret)
  - SecretsClient.watch(): a cached secret converges to the rotated value
    live, with no restart and no manual invalidation
  - exposed + expired (rotation_due) events reach subscribers
  - hub-down durability: a secret committed while the hub is down is delivered
    by the outbox drainer once the hub returns (idempotent, no double-append)
  - poison parking: an outbox row the hub permanently rejects (4xx) is parked
    and CONTINUE-d past, so later events still deliver — no head-of-line block
  - mTLS on HyperSecret: certificate-authenticated fetch through the
    terminator, unknown-CN and forged-attestation rejection
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e_helper import TEST_PORTS, AppRunner, http_get, http_post  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.serviceclient import ChangeFeedWatcher  # noqa: E402
from services.hypermanager.client import ManagerClient  # noqa: E402
from services.hypersecret.client import AuthError, SecretsClient  # noqa: E402
from services.hypersecret.envelope import load_kek_file  # noqa: E402

SECRET_PORT = TEST_PORTS["hypersecret_live"]
MANAGER_PORT = TEST_PORTS["hypersecret_live_manager"]
MTLS_PORT = TEST_PORTS["hypersecret_live_mtls"]
SECRET_BASE = f"http://127.0.0.1:{SECRET_PORT}"
MANAGER_BASE = f"http://127.0.0.1:{MANAGER_PORT}"
SCRATCH = PROJECT_ROOT / ".test_scratch" / "hypersecret_live"
SECRET_DEMO = SCRATCH / "secret_demo"
MANAGER_DEMO = SCRATCH / "manager_demo"
CA_DIR = SCRATCH / "ca"

# Token-signing key (framework SESSION_SIGNING_KEY) must be STABLE across seed
# + both server subprocesses. hyper-test injects it; pin for standalone runs.
os.environ.setdefault(
    "HYPER_SESSION_SIGNING_KEY", "test-session-signing-key-for-tests-only"
)
# The app resolves SECRET_KEY / ADMIN_SECRET with require_setting(min_length=32),
# so it refuses to start on a missing or too-short signing secret. Provide stable
# >=32-char values for the seed and server subprocesses. The shared runner injects
# a shorter ADMIN_SECRET for other apps; override it here to satisfy the length
# gate without weakening the app's requirement.
os.environ.setdefault(
    "HYPER_SECRET_KEY", "test-secret-key-for-hypersecret-tests-only-0123456789"
)
if len(os.environ.get("HYPER_ADMIN_SECRET", "")) < 32:
    os.environ["HYPER_ADMIN_SECRET"] = (
        "test-admin-secret-for-hypersecret-tests-only-0123456789"
    )

from hyperdjango.testkit import check, finish  # noqa: E402

# The ceiling only bounds the CPU-starved worst case (watcher event delivery
# competing with the full parallel suite); polls return the moment the
# predicate holds, so normal runs never feel it. A genuinely lost event still
# fails once the bound elapses.
EVENT_WAIT_CEILING_S = 30.0


def wait_for(
    predicate, timeout: float = EVENT_WAIT_CEILING_S, interval: float = 0.05
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _HubSubscriber:
    """Live default-tier subscriber that records the event frames the hub pushes.

    The default (catch-up) hub keeps no queryable durable log, so a test observes
    what a producer published by subscribing to the live feed and collecting the
    pushed ``event`` frames — the same in-frame delivery the client SDK consumes.
    Built directly on the framework `ChangeFeedWatcher` (ws-only) so it needs no
    ledger endpoints. ``wait_connected`` blocks on the watcher's own observed
    connect (the hello is negotiated and its resync applied) so a producer only
    publishes once the subscription is established — the tier delivers only to
    connected subscribers, so publishing before that silently drops the event."""

    def __init__(self, token: str, prefixes: tuple[str, ...] = ("secrets/",)):
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._client = ManagerClient(MANAGER_BASE, token=token)
        ws_path = "/ws/feed?" + urllib.parse.urlencode({"prefixes": ",".join(prefixes)})
        self._watcher = ChangeFeedWatcher(
            client=self._client,
            ws_path=ws_path,
            on_event=self._record,
            prefixes=list(prefixes),
        ).start()

    def _record(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)

    def wait_connected(self, timeout: float = EVENT_WAIT_CEILING_S) -> bool:
        return self._watcher.wait_connected(timeout)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def stop(self) -> None:
        self._watcher.stop()


def notify_posted_metric(secret_tokens) -> float:
    """Read the change-notifications-delivered counter off HyperSecret's
    /metrics — the producer-side signal that the outbox drained to the hub."""
    resp = http_get(
        f"{SECRET_BASE}/metrics",
        headers={"Authorization": f"Bearer {secret_tokens['operator:admin']}"},
    )
    for line in resp.body.splitlines():
        if line.startswith("hypersecret_notify_posted_total "):
            return float(line.split()[-1])
    return 0.0


def run_cli(*args, env=None):
    result = subprocess.run(
        list(args),
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(f"command failed: {' '.join(args[:6])}")
    return result


def setup() -> tuple[dict, dict, dict]:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)

    # Certificate manager: one CA anchors both server and client certs.
    py = sys.executable
    run_cli(
        py, "-m", "services.hypersecret.provision", "ca", "init", "--dir", str(CA_DIR)
    )
    for cn, extra in (
        ("localhost", ["--server", "--dns", "localhost,127.0.0.1"]),
        ("service:prod-api", []),
        ("service:unknown-cn", []),
    ):
        run_cli(
            py,
            "-m",
            "services.hypersecret.provision",
            "cert",
            "issue",
            cn,
            "--ca-dir",
            str(CA_DIR),
            "--out-prefix",
            str(SCRATCH / cn.replace(":", "-")),
            *extra,
        )

    env = dict(os.environ)
    env["HYPERMANAGER_DEMO_DIR"] = str(MANAGER_DEMO)
    run_cli(
        "uv",
        "run",
        "hyper",
        "setup",
        "--app",
        "services.hypermanager.app:app",
        "--drop",
        "--seed",
        "services.hypermanager.seed:run",
        env=env,
    )
    env = dict(os.environ)
    env["HYPERSECRET_DEMO_DIR"] = str(SECRET_DEMO)
    run_cli(
        "uv",
        "run",
        "hyper",
        "setup",
        "--app",
        "services.hypersecret.app:app",
        "--drop",
        "--seed",
        "services.hypersecret.seed:run",
        env=env,
    )

    secret_tokens = json.loads((SECRET_DEMO / "tokens.json").read_text())
    manager_tokens = json.loads((MANAGER_DEMO / "tokens.json").read_text())
    keks = {}
    for ns in ("prod/api", "staging/api"):
        kek_id, kek = load_kek_file(str(SECRET_DEMO / (ns.replace("/", "-") + ".kek")))
        keks[ns] = (kek_id, kek)
    return secret_tokens, manager_tokens, keks


def parked_metric(secret_tokens) -> float:
    """Read the outbox-parked counter off HyperSecret's /metrics (authed)."""
    resp = http_get(
        f"{SECRET_BASE}/metrics",
        headers={"Authorization": f"Bearer {secret_tokens['operator:admin']}"},
    )
    for line in resp.body.splitlines():
        if line.startswith("hypersecret_outbox_parked_total "):
            return float(line.split()[-1])
    return 0.0


def secret_env(manager_tokens) -> dict:
    env = dict(os.environ)
    env["HYPERSECRET_MANAGER_URL"] = MANAGER_BASE
    env["HYPERSECRET_MANAGER_TOKEN"] = manager_tokens["producer:hypersecret"]
    env["HYPERSECRET_ROTATION_SWEEP_INTERVAL"] = "1"
    env["HYPERSECRET_MTLS_LISTEN_PORT"] = str(MTLS_PORT)
    # No MTLS_UPSTREAM_PORT: the terminator forwards to the app's real bound
    # port (SECRET_PORT here) automatically, so it can never desync.
    env["HYPERSECRET_MTLS_CERT_FILE"] = str(SCRATCH / "localhost.crt")
    env["HYPERSECRET_MTLS_KEY_FILE"] = str(SCRATCH / "localhost.key")
    env["HYPERSECRET_MTLS_CA_FILE"] = str(CA_DIR / "ca.crt")
    return env


# ---------------------------------------------------------------------------


def test_producer_events(secret_tokens, manager_tokens, keks):
    print("\n== secret operations publish change events ==")
    kek_id, kek = keks["prod/api"]
    ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=0,
    )
    # The default tier delivers only to connected subscribers (no historical
    # query), so subscribe and wait to connect BEFORE publishing. platform-api
    # holds a subscribe grant on secrets/prod/.
    subscriber = _HubSubscriber(
        manager_tokens["service:platform-api"], prefixes=("secrets/prod/",)
    )
    try:
        check("subscriber connected to the live feed", subscriber.wait_connected())

        ops.put_secret("live_demo", b"first-value")
        ops.put_secret("live_demo", b"second-value")

        def kinds():
            return [
                (e["kind"], (e.get("metadata") or {}).get("version"))
                for e in subscriber.snapshot()
                if e.get("subject") == "secrets/prod/api/live_demo"
            ]

        check(
            "created + rotated events pushed on the live feed",
            wait_for(lambda: kinds() == [("created", 1), ("rotated", 2)]),
            f"got {kinds()}",
        )
        events = [
            e
            for e in subscriber.snapshot()
            if e.get("subject") == "secrets/prod/api/live_demo"
        ]
        check(
            "events are metadata-only",
            all(
                "ciphertext" not in json.dumps(e)
                and "second-value" not in json.dumps(e)
                for e in events
            ),
        )

        ops.delete_secret("live_demo")
        check(
            "delete event published",
            wait_for(lambda: any(k == ("deleted", 2) for k in kinds())),
            f"got {kinds()}",
        )
    finally:
        subscriber.stop()


def test_live_rotation_convergence(secret_tokens, manager_tokens, keks):
    print("\n== live rotation: cached client converges without restart ==")
    kek_id, kek = keks["prod/api"]
    service = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["service:prod-api"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=3600,  # cache would serve stale for an hour without watch
    )
    ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=0,
    )

    check(
        "warm fetch",
        service.get_secret("stripe_key") == "sk_live_demo_4242424242424242",
    )

    changes: list[dict] = []
    watcher = service.watch(
        MANAGER_BASE,
        manager_token=manager_tokens["service:platform-api"],
        on_change=lambda ev: changes.append(ev),
    )

    ops.put_secret("stripe_key", b"sk_live_ROTATED_9999")
    check(
        "cached value converges to rotated secret (no restart)",
        wait_for(lambda: service.get_secret("stripe_key") == "sk_live_ROTATED_9999"),
    )
    check(
        "on_change callback fired with rotation event",
        # The callback fires asynchronously as the watcher delivers the event;
        # wait for it rather than checking immediately (convergence can now be
        # reached via watch()'s cache invalidation before the event lands).
        wait_for(
            lambda: any(
                ev["subject"] == "secrets/prod/api/stripe_key"
                and ev["kind"] == "rotated"
                for ev in changes
            )
        ),
    )

    # Exposure marking reaches the same feed.
    resp = http_post(
        f"{SECRET_BASE}/v1/secrets/prod/api/stripe_key/expose",
        body={},
        headers={"Authorization": f"Bearer {secret_tokens['operator:admin']}"},
    )
    check("expose endpoint (admin)", resp.status == 200)
    check(
        "exposed event reaches watcher",
        wait_for(lambda: any(ev["kind"] == "exposed" for ev in changes)),
    )

    # rotation_due in the past → the sweep publishes `expired` (interval=1s).
    ops.put_secret(
        "stale_cred",
        b"old",
        metadata={"rotation_due": "2020-01-01T00:00:00+00:00"},
    )
    check(
        "expired event published by rotation-due sweep",
        wait_for(
            lambda: any(
                ev["kind"] == "expired" and ev["subject"].endswith("stale_cred")
                for ev in changes
            ),
            timeout=10,
        ),
        f"kinds seen: {[ev['kind'] for ev in changes]}",
    )
    watcher.stop()


def test_watch_closes_staleness_gap(secret_tokens, manager_tokens, keks):
    """Regression (item 6): a rotation that lands BEFORE the watcher connects is
    never delivered as a per-key nudge, so a warm cache would serve the stale
    envelope until cache_ttl. watch() resyncs on connect (and invalidates the
    cache synchronously as it returns), so the next access revalidates and
    converges. Proven by rotating a warm-cached key with a long TTL — the cache
    keeps serving the stale value — then watching and reading."""
    print("\n== watch(): closes the warm-cache→watch staleness gap ==")
    kek_id, kek = keks["prod/api"]
    service = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["service:prod-api"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=3600,  # would serve stale for an hour without the resync
    )
    ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=0,
    )

    ops.put_secret("gap_key", b"gap-v1")
    check("warm fetch caches v1", service.get_secret("gap_key") == "gap-v1")

    # Rotate with no watcher connected: the long-TTL cache keeps serving the
    # stale v1, so only watch()'s resync/invalidate can close the gap.
    ops.put_secret("gap_key", b"gap-v2")
    check(
        "cache still serves stale v1 (ttl=3600, no watcher yet)",
        service.get_secret("gap_key") == "gap-v1",
    )

    watcher = service.watch(
        MANAGER_BASE, manager_token=manager_tokens["service:platform-api"]
    )
    try:
        check(
            "next get converges to rotated value (watch invalidated the cache)",
            wait_for(lambda: service.get_secret("gap_key") == "gap-v2"),
        )
    finally:
        watcher.stop()


def test_watch_reconnect_catchup(secret_tokens, manager_tokens, keks, manager_runner):
    """Reconnect catch-up: a rotation that lands while the watcher is briefly
    disconnected is caught up on reconnect — the missed key is invalidated and
    the next fetch returns the new version, with no restart.

    The watcher stays alive across a hub bounce. While the hub is down the feed
    drops; a rotation commits (buffered in the outbox). When the hub returns, the
    watcher reconnects and resyncs — dropping the cache so the next fetch pulls
    the value it missed while disconnected, which a long-lived cache would
    otherwise serve stale for an hour. Exercises the default tier's reconnect
    catch-up path.

    Every step here is sequenced on an OBSERVED watcher transition
    (``wait_connected`` / ``wait_disconnected``), never on elapsed time: the
    connect's resync and the hub-loss detection are both asynchronous, and
    asserting across either without observing it first is what makes such a test
    flaky (a resync landing late clears the whole cache mid-assertion).

    The disconnected-window claim is likewise established INSIDE that window.
    While the feed is up, an invalidation may legitimately arrive at any moment —
    a duplicate nudge, or a resync from a reconnect the hub provoked — and each
    one only costs a re-fetch (a body-free 304 when nothing changed), so "the
    cache still holds what it held a moment ago" is not something the client
    promises while connected. What it does promise is that NOTHING invalidates
    while the feed is down, so the cache is (re)warmed after the disconnect is
    observed and only then is the rotation-while-down performed."""
    print("\n== watch(): reconnect catches up a change missed while disconnected ==")
    kek_id, kek = keks["prod/api"]
    service = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["service:prod-api"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=3600,  # would serve stale for an hour without catch-up
    )
    ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=0,
    )

    ops.put_secret("catchup_key", b"cv1")

    # Record what the feed actually delivered, so a failure names the
    # invalidation behind it instead of leaving a bare "wrong value".
    feed: list[str] = []
    watcher = service.watch(
        MANAGER_BASE,
        manager_token=manager_tokens["service:platform-api"],
        on_change=lambda ev: feed.append(f"{ev.get('kind')}:{ev.get('subject')}"),
    )

    def feed_state() -> str:
        return (
            f"connects={watcher.connects} disconnects={watcher.disconnects} feed={feed}"
        )

    try:
        # The feed's connect resync clears the WHOLE cache, so warming before it
        # lands proves nothing (and leaves an invalidation in flight under every
        # assertion below). Wait for the observed connect — which flips only
        # after that resync has been applied — and warm the cache after it.
        check(
            "live feed connected (hello negotiated, connect resync applied)",
            watcher.wait_connected(EVENT_WAIT_CEILING_S),
        )
        check("warm fetch caches v1", service.get_secret("catchup_key") == "cv1")

        # Prove the watcher is live and delivering before the disconnect: with a
        # warm cache and ttl=3600, the ONLY thing that can converge this read is
        # the feed's per-key invalidation.
        ops.put_secret("catchup_key", b"cv2")
        check(
            "converges live before disconnect (watcher established)",
            wait_for(lambda: service.get_secret("catchup_key") == "cv2"),
            feed_state(),
        )

        # Disconnect: drop the hub. The watcher's feed dies — but detecting that
        # is the watcher's own asynchronous business, so wait for it to observe
        # the loss before asserting anything about "while disconnected". Once
        # observed, the session is torn down and every frame it had received is
        # already dispatched: no invalidation can still be in flight.
        manager_runner.stop()
        check(
            "watcher observes the hub loss",
            watcher.wait_disconnected(EVENT_WAIT_CEILING_S),
            feed_state(),
        )
        # Re-warm INSIDE the disconnected window. Anything the connected phase
        # left pending (a duplicate nudge, a resync from a reconnect the loaded
        # machine provoked) has already been dispatched by the time the
        # disconnect is observed, and nothing new can arrive with the hub down —
        # so from here the cache is quiescent and the assertion below is about
        # the disconnected window alone.
        check(
            "cache holds v2 at the moment the feed goes down",
            service.get_secret("catchup_key") == "cv2",
            feed_state(),
        )
        # Rotate while disconnected — the write still succeeds (the outbox
        # absorbs the notification) and the watcher cannot see it yet.
        ops.put_secret("catchup_key", b"cv3")
        check(
            "cache still serves v2 while the watcher is disconnected",
            service.get_secret("catchup_key") == "cv2",
            feed_state(),
        )

        # Reconnect: the hub returns and the reconnected watcher resyncs,
        # dropping the cache so the next fetch pulls the value missed while away.
        manager_runner.start()
        check(
            "watcher reconnects to the returned hub",
            watcher.wait_connected(EVENT_WAIT_CEILING_S),
            feed_state(),
        )
        check(
            "reconnect catch-up invalidates the missed key → next fetch is v3",
            wait_for(lambda: service.get_secret("catchup_key") == "cv3", timeout=20),
            feed_state(),
        )
    finally:
        watcher.stop()


def test_watch_on_reset_invalidates(keks):
    """A ChangeFeedWatcher resync — a fresh connect the hub kept no state for, a
    catch-up buffer overrun, or a ledger-tier floor reset — must drop the ENTIRE
    client cache: the missed per-key deltas are gone, so per-key invalidation
    cannot know which envelopes went stale, and waiting for cache_ttl would keep
    serving stale ciphertext. Assert watch() wires an on_reset that clears the
    whole cache — a real overrun is hard to force live, so the callback is
    exercised directly through a stub watcher. Also asserts watch() passes the
    stable client_id + prefixes the default tier's catch-up buffer keys on."""
    print("\n== watch(): resync invalidates the whole cache ==")
    import services.hypersecret.client as client_mod

    kek_id, kek = keks["prod/api"]
    service = SecretsClient(
        SECRET_BASE, token="t", namespace="prod/api", kek=kek, kek_id=kek_id
    )

    captured: dict = {}

    class _StubWatcher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            return self

    # watch() is ws-only now — it makes no ledger request — so a bare stub
    # transport suffices; it is only handed to the (stubbed) watcher as client=.
    class _StubManager:
        pass

    real = client_mod.ChangeFeedWatcher
    client_mod.ChangeFeedWatcher = _StubWatcher
    try:
        service.watch(manager=_StubManager())
        on_reset = captured.get("on_reset")
        check("watch() passes an on_reset callback", callable(on_reset))
        check(
            "watch() passes a stable client_id + namespace prefixes",
            captured.get("client_id") == "hypersecret/prod/api"
            and captured.get("prefixes") == ["secrets/prod/api/"],
            f"captured={ {k: captured.get(k) for k in ('client_id', 'prefixes')} }",
        )
        # Seed the cache AFTER watch()'s own start-time invalidate, then fire the
        # captured reset and assert it clears everything.
        with service._lock:
            service._cache["k"] = object()
        if callable(on_reset):
            on_reset({"reset": True})
        check("on_reset cleared the entire client cache", len(service._cache) == 0)
    finally:
        client_mod.ChangeFeedWatcher = real


def test_mtls_fetch(secret_tokens, keks):
    print("\n== mTLS: certificate-authenticated secret fetch ==")
    kek_id, kek = keks["prod/api"]
    ca = str(CA_DIR / "ca.crt")

    cert_client = SecretsClient(
        f"https://127.0.0.1:{MTLS_PORT}",
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        ca_file=ca,
        client_cert_file=str(SCRATCH / "service-prod-api.crt"),
        client_key_file=str(SCRATCH / "service-prod-api.key"),
    )
    check(
        "cert-authenticated fetch + decrypt (no token)",
        cert_client.get_secret("db_password") == "prod-api-db-pw-3f9c2e",
    )

    unknown = SecretsClient(
        f"https://127.0.0.1:{MTLS_PORT}",
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        ca_file=ca,
        client_cert_file=str(SCRATCH / "service-unknown-cn.crt"),
        client_key_file=str(SCRATCH / "service-unknown-cn.key"),
    )
    try:
        unknown.get_secret("db_password")
        check("valid cert, unknown CN → 401", False, "fetch succeeded")
    except AuthError:
        check("valid cert, unknown CN → 401", True)

    resp = http_get(
        f"{SECRET_BASE}/v1/secrets/prod/api/db_password",
        headers={
            "X-Hyper-MTLS-Attest": "forged",
            "X-Hyper-MTLS-CN": "service:prod-api",
        },
    )
    check("forged attestation on plaintext port → 401", resp.status == 401)


def test_hub_down_durability(secret_tokens, manager_tokens, keks, manager_runner):
    """A secret committed while the hub is DOWN is not lost: the outbox row
    survives, and the drainer delivers it once the hub returns. The write itself
    never touches the hub, so it succeeds regardless.

    The default tier keeps no queryable log and its live feed cannot be observed
    across a hub restart (the seq resets), so delivery is confirmed from the
    PRODUCER side: HyperSecret's ``notify_posted`` counter advances only when the
    drainer's post is acknowledged by the hub. The post carries the outbox row id
    as an idempotent dedupe key, so a retry across the crash window collapses to a
    single delivery."""
    print("\n== hub-down durability: outbox delivers after the hub returns ==")
    kek_id, kek = keks["prod/api"]
    ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="prod/api",
        kek=kek,
        kek_id=kek_id,
        cache_ttl=0,
    )

    posted_before = notify_posted_metric(secret_tokens)
    manager_runner.stop()
    # Commit a change while the hub is unreachable — the API call must still
    # succeed (the outbox absorbs the notification durably).
    version = ops.put_secret("durable_key", b"written-while-hub-down")
    check("secret write succeeds while hub is down", version >= 1)

    manager_runner.start()
    check(
        "outbox delivers the buffered change after the hub returns",
        wait_for(
            lambda: notify_posted_metric(secret_tokens) > posted_before, timeout=20
        ),
        f"posted before={posted_before} now={notify_posted_metric(secret_tokens)}",
    )


def test_poison_parking(secret_tokens, manager_tokens, keks):
    """A permanently-rejected outbox row (4xx from the hub) is parked and the
    drainer CONTINUES: a poison event never head-of-line-blocks the feed, and
    the parked row is recorded (metric)."""
    print("\n== poison parking: a rejected row is parked, later rows deliver ==")
    ops_mgr = ManagerClient(MANAGER_BASE, token=manager_tokens["operator:admin"])
    # Narrow the HyperSecret producer so a staging publish is rejected (403 =
    # permanent) while prod still succeeds — without changing its identity name.
    ops_mgr._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "producer:hypersecret",
            "prefix": "secrets/",
            "publish": False,
        },
    )
    ops_mgr._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "producer:hypersecret",
            "prefix": "secrets/prod/",
            "publish": True,
        },
    )

    # platform-api can subscribe only to secrets/prod/, so the live feed observes
    # the prod event directly; the staging poison is out of its scope AND rejected
    # at publish, so its absence here is the expected cross-check.
    subscriber = _HubSubscriber(
        manager_tokens["service:platform-api"], prefixes=("secrets/prod/",)
    )
    check("subscriber connected to the live feed", subscriber.wait_connected())
    before_parked = parked_metric(secret_tokens)

    stg_id, stg_kek = keks["staging/api"]
    staging_ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="staging/api",
        kek=stg_kek,
        kek_id=stg_id,
        cache_ttl=0,
    )
    prod_id, prod_kek = keks["prod/api"]
    prod_ops = SecretsClient(
        SECRET_BASE,
        token=secret_tokens["operator:admin"],
        namespace="prod/api",
        kek=prod_kek,
        kek_id=prod_id,
        cache_ttl=0,
    )

    # Poison first (drains earlier), then a good prod event behind it.
    staging_ops.put_secret("poison_key", b"never-lands-on-hub")
    prod_ops.put_secret("after_poison", b"lands-despite-poison")

    check(
        "later prod event delivers despite the earlier poison row",
        wait_for(
            lambda: any(
                e.get("subject") == "secrets/prod/api/after_poison"
                for e in subscriber.snapshot()
            ),
            timeout=15,
        ),
    )
    check(
        "the poison (staging) event never reaches the hub",
        not any(
            e.get("subject") == "secrets/staging/api/poison_key"
            for e in subscriber.snapshot()
        ),
    )
    check(
        "a parked row is recorded in the metric",
        wait_for(lambda: parked_metric(secret_tokens) > before_parked, timeout=10),
        f"parked before={before_parked} now={parked_metric(secret_tokens)}",
    )
    subscriber.stop()


# ---------------------------------------------------------------------------


def main() -> bool:
    print(
        f"HyperSecret live E2E — secret {SECRET_PORT} / manager {MANAGER_PORT} "
        f"/ mTLS {MTLS_PORT}"
    )
    secret_tokens, manager_tokens, keks = setup()

    manager_runner = AppRunner(
        "services.hypermanager.app:app",
        host="127.0.0.1",
        port=MANAGER_PORT,
        readiness_path="/ready",
    )
    with (
        manager_runner,
        AppRunner(
            "services.hypersecret.app:app",
            host="127.0.0.1",
            port=SECRET_PORT,
            readiness_path="/ready",
            env=secret_env(manager_tokens),
        ),
    ):
        test_producer_events(secret_tokens, manager_tokens, keks)
        test_live_rotation_convergence(secret_tokens, manager_tokens, keks)
        test_watch_closes_staleness_gap(secret_tokens, manager_tokens, keks)
        test_watch_on_reset_invalidates(keks)
        test_hub_down_durability(secret_tokens, manager_tokens, keks, manager_runner)
        test_watch_reconnect_catchup(
            secret_tokens, manager_tokens, keks, manager_runner
        )
        test_mtls_fetch(secret_tokens, keks)
        test_poison_parking(secret_tokens, manager_tokens, keks)

    print()
    return finish()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
