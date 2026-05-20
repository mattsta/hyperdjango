"""
Storefront — a live-config consumer of HyperSecret + HyperManager.

A small service that fetches its runtime API keys from HyperSecret on startup,
caches them in memory, and subscribes to HyperManager's change feed so a
rotation in the store propagates here LIVE — no restart, no polling loop. See
ARCHITECTURE.md for the three-service data flow.

The defining properties:

  - It holds only READ (on its namespace) and SUBSCRIBE (on its subject prefix)
    credentials — it can never write a secret or publish a change. Least
    privilege.
  - Publishable / public keys are displayed in full (that is what they are for).
    A genuinely-secret value is *used* (via ``secret_bytes()``, wiped after) but
    NEVER exposed on any endpoint or written to a log — only a short fingerprint
    of it is shown, to prove it is live without leaking it.
  - Secrets live in memory only. Nothing here persists them.

Run (standalone; the mesh wires the env for you — see run_mesh.py):
    uv run hyper start --app services.live_config.app:app --port 8980

Endpoints:
    GET /         → human-readable status page
    GET /config   → {key: {version, value_or_masked, refreshed_at, ...}}
    GET /events   → Server-Sent Events stream of live "key rotated" notices
    GET /healthz  → liveness
    GET /health, /ready → framework health probes (mount_health)
"""

import asyncio
import contextlib
import hashlib
import html
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from hyperdjango import HyperApp, Response
from hyperdjango.conf import get_setting
from hyperdjango.logging import logger
from hyperdjango.native import fast_json_dumps

# The SDK boundary: the consumer talks to HyperSecret only through its published
# client (fetch/decrypt/cache/watch over HTTP + the change feed). load_kek_file
# reads the namespace master key this service was handed out of band — the KEK
# is client-side crypto material, never sent to the server. No server internals
# are imported.
from services.hypersecret.client import (
    SecretNotFound,
    SecretsClient,
    SecretsError,
    ServerUnavailable,
)
from services.hypersecret.envelope import load_kek_file

from .config import load_storefront_config

_config = load_storefront_config()
_DEBUG = bool(get_setting("DEBUG"))

# No database: the storefront owns no tables. It is a pure in-memory consumer,
# so readiness reduces to "the config load completed and the feed watcher is
# running" (see the custom readiness check below).
app = HyperApp(
    title=_config.name,
    database=None,
    debug=_DEBUG,
    secret_key=get_setting("SECRET_KEY"),
    site_config=_config,
)


# ---------------------------------------------------------------------------
# In-memory config cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class KeyState:
    """The storefront's live view of one configured key. Guarded by
    ``_state_lock``: written from the startup hook and the feed watcher's
    background thread, read from the request handlers on the event loop."""

    name: str
    is_secret: bool
    version: int | None = None
    # Public keys carry their plaintext value (safe to display). A secret key
    # carries only a fingerprint (sha256 prefix) — never plaintext.
    value: str | None = None
    fingerprint: str | None = None
    refreshed_at: str = ""
    available: bool = False
    error: str = ""


_state_lock = threading.Lock()
_keys: dict[str, KeyState] = {}

# Live-runtime handles set by the startup hook. A single dict keeps the mutable
# module state in one place instead of a scatter of globals.
_runtime: dict = {
    "client": None,
    "watcher": None,
    "loop": None,
    "last_feed_at": "",
}


def _feed_connected() -> bool:
    """Whether the live feed is up RIGHT NOW.

    Read from the watcher, never tracked here: a locally-maintained flag can
    only be set from things this process sees (watch() returning, an event
    arriving) and so keeps reporting "connected" straight through a hub outage —
    exactly the window where the storefront is serving values that no longer get
    invalidated. The watcher observes the socket, so it is the only honest
    source.
    """
    watcher = _runtime["watcher"]
    return watcher is not None and watcher.connected


def _feed_drops() -> int:
    """Feed sessions lost since startup (a climbing count = a flapping hub)."""
    watcher = _runtime["watcher"]
    return watcher.disconnects if watcher is not None else 0


# SSE fan-out: one bounded queue per connected /events client.
_sse_lock = threading.Lock()
_sse_subscribers: set[asyncio.Queue] = set()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _refresh_key(client: SecretsClient, name: str, is_secret: bool) -> None:
    """Re-read one key from HyperSecret and update its in-memory state.

    Fail-closed with bounded tolerance: on a transient store outage the client
    itself may serve a stale-but-bounded cached envelope (``stale_max``); on a
    hard failure we RETAIN the last-known value (so the storefront keeps serving
    its current config) and annotate the error, rather than blanking the key.
    A genuinely-missing key is marked unavailable.
    """
    with _state_lock:
        prior = _keys.get(name)
    try:
        # get_envelope fetches (or serves cached) ciphertext and yields the
        # version — for a secret key this is all we need on the display path, so
        # its plaintext is never decrypted here.
        entry = client.get_envelope(name)
        version = entry.version
        if is_secret:
            # Demonstrate the secret-handling best practice: use secret_bytes()
            # (a wipeable bytearray, zeroized at block exit) and derive only a
            # short fingerprint. The plaintext never lands in a field, a log, or
            # a response body.
            with client.secret_bytes(name) as buf:
                fingerprint = hashlib.sha256(bytes(buf)).hexdigest()[:12]
            new = KeyState(
                name=name,
                is_secret=True,
                version=version,
                value=None,
                fingerprint=fingerprint,
                refreshed_at=_now(),
                available=True,
                error="",
            )
        else:
            # Public/publishable value: a cache hit from the get_envelope above,
            # so no extra round trip — decrypt and display in full.
            value = client.get_secret(name)
            new = KeyState(
                name=name,
                is_secret=False,
                version=version,
                value=value,
                fingerprint=None,
                refreshed_at=_now(),
                available=True,
                error="",
            )
    except SecretNotFound:
        new = KeyState(
            name=name,
            is_secret=is_secret,
            refreshed_at=_now(),
            available=False,
            error="not found",
        )
    except (ServerUnavailable, SecretsError) as exc:
        # Retain the last-known good value (fail-closed tolerance), attach the
        # error so /config surfaces the degradation.
        if prior is not None and prior.available:
            new = KeyState(
                name=name,
                is_secret=prior.is_secret,
                version=prior.version,
                value=prior.value,
                fingerprint=prior.fingerprint,
                refreshed_at=prior.refreshed_at,
                available=True,
                error=f"stale: {type(exc).__name__}",
            )
        else:
            new = KeyState(
                name=name,
                is_secret=is_secret,
                refreshed_at=_now(),
                available=False,
                error=type(exc).__name__,
            )
    with _state_lock:
        _keys[name] = new


def _refresh_all(client: SecretsClient) -> None:
    for name, is_secret in _config.key_specs():
        _refresh_key(client, name, is_secret)


def _key_from_subject(subject: str) -> str | None:
    """Map a feed subject ``secrets/<env>/<service>/<key>`` to its key name,
    if it belongs to this storefront's namespace."""
    parts = subject.split("/", 3)
    if len(parts) == 4 and f"{parts[1]}/{parts[2]}" == _config.namespace:
        return parts[3]
    return None


def _configured(name: str) -> bool:
    return any(n == name for n, _ in _config.key_specs())


def _is_secret_key(name: str) -> bool:
    return any(n == name and secret for n, secret in _config.key_specs())


def _on_key_change(event: dict) -> None:
    """Feed callback (runs on the watcher's background thread). By the time this
    fires the SDK has already invalidated the changed key's cached envelope, so
    the refetch below pulls the new version. A ``reset`` event (a (re)connect
    resync) refetches everything."""
    client = _runtime["client"]
    if client is None:
        return
    # Delivery time only — liveness comes from the watcher (_feed_connected).
    _runtime["last_feed_at"] = _now()

    kind = event.get("kind", "")
    subject = event.get("subject", "")
    if event.get("reset") or kind == "reset":
        _refresh_all(client)
        _broadcast_sse({"event": "resync", "data": "feed (re)connected — resynced"})
        return

    key = _key_from_subject(subject)
    if key is None or not _configured(key):
        return
    _refresh_key(client, key, _is_secret_key(key))
    with _state_lock:
        state = _keys.get(key)
    version = state.version if state is not None else None
    logger.info(
        "Live config: {k} changed ({kind}) → version {v}", k=key, kind=kind, v=version
    )
    _broadcast_sse(
        {
            "event": "rotation",
            "data": fast_json_dumps(
                {"key": key, "kind": kind, "version": version, "at": _now()}
            ).decode(),
        }
    )


def _broadcast_sse(payload: dict) -> None:
    """Push an SSE payload to every connected /events client. Called from the
    watcher thread, so each enqueue hops onto the event loop."""
    loop = _runtime["loop"]
    if loop is None:
        return
    with _sse_lock:
        subscribers = list(_sse_subscribers)
    for queue in subscribers:

        def _put(q=queue, p=payload):
            if not q.full():
                q.put_nowait(p)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_put)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup() -> None:
    _runtime["loop"] = asyncio.get_running_loop()

    kek, kek_id = None, ""
    if _config.kek_file:
        kek_id, kek = load_kek_file(_config.kek_file)

    # A READ-only client for this namespace. cache_ttl is long by design — the
    # change feed, not the TTL, drives convergence — and stale_max grants a small
    # bounded tolerance so a momentary store blip does not blank the config.
    client = SecretsClient(
        _config.hypersecret_url,
        token=_config.hypersecret_token,
        namespace=_config.namespace,
        kek=kek,
        kek_id=kek_id,
        cache_ttl=_config.cache_ttl,
        stale_max=_config.stale_max,
        timeout=_config.fetch_timeout,
    )
    _runtime["client"] = client

    # Warm the cache: fetch every configured key once.
    _refresh_all(client)

    # Subscribe to the change feed. watch() invalidates the changed key on every
    # nudge (and resyncs on connect), then fires _on_key_change so we refetch and
    # update our in-memory view. The storefront never restarts to see a rotation.
    if _config.manager_url:
        watcher = client.watch(
            _config.manager_url,
            manager_token=_config.manager_token,
            on_change=_on_key_change,
            client_id=f"storefront/{_config.namespace}",
        )
        _runtime["watcher"] = watcher
        _runtime["last_feed_at"] = _now()
        # Wait (bounded) for the feed's first connect so the state this service
        # reports is true from its first request, rather than optimistic. The
        # watcher keeps reconnecting on its own, so a timeout here is a warning,
        # never a startup failure — the storefront serves its warmed cache and
        # reports the feed as disconnected until it lands.
        if watcher.wait_connected(_config.feed_connect_timeout):
            logger.info(
                "Storefront watching {u} for {ns} changes",
                u=_config.manager_url,
                ns=_config.namespace,
            )
        else:
            logger.warning(
                "Change feed at {u} not connected within {t}s — serving cached "
                "config; rotations will not converge until the feed comes up",
                u=_config.manager_url,
                t=_config.feed_connect_timeout,
            )
    else:
        logger.warning("No manager_url configured — live convergence disabled")

    loaded = sum(1 for s in _keys.values() if s.available)
    logger.success(
        "Storefront ready: {n}/{t} keys loaded from {u}",
        n=loaded,
        t=len(_config.key_specs()),
        u=_config.hypersecret_url,
    )


@app.on_shutdown
async def _shutdown() -> None:
    watcher = _runtime["watcher"]
    if watcher is not None:
        watcher.stop()
    client = _runtime["client"]
    if client is not None:
        client.close()


# Readiness: the storefront is ready once the watcher is running (or was
# deliberately disabled) — i.e. startup completed. It owns no database.
app.add_health_check(
    "config_loaded", lambda: _runtime["watcher"] is not None or not _config.manager_url
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _key_view(state: KeyState) -> dict:
    """The public projection of one key — a secret's plaintext is NEVER here."""
    view = {
        "classification": "secret" if state.is_secret else "public",
        "version": state.version,
        "refreshed_at": state.refreshed_at,
        "available": state.available,
    }
    if state.is_secret:
        # Masked: a fingerprint proves the value is present and current without
        # revealing a single byte of it.
        view["value"] = None
        view["masked"] = (
            f"sha256:{state.fingerprint}…" if state.fingerprint else "•••• (masked)"
        )
    else:
        view["value"] = state.value
    if state.error:
        view["error"] = state.error
    return view


def _snapshot() -> dict:
    with _state_lock:
        states = {name: _keys.get(name) for name, _ in _config.key_specs()}
    return {
        name: _key_view(state) for name, state in states.items() if state is not None
    }


@app.get("/config")
async def config(request):
    """The live config view: each key's version, value (or mask), and freshness."""
    return Response.json(
        {
            "namespace": _config.namespace,
            "feed_connected": _feed_connected(),
            "feed_drops": _feed_drops(),
            "last_feed_at": _runtime["last_feed_at"],
            "keys": _snapshot(),
        }
    )


@app.get("/healthz")
async def healthz(request):
    return Response.json({"status": "ok"})


@app.get("/")
async def root(request):
    """Human-readable status page."""
    snap = _snapshot()
    feed = "connected" if _feed_connected() else "disconnected"
    rows = []
    for name, view in snap.items():
        if view["classification"] == "secret":
            shown = html.escape(view["masked"])
            cls = "secret"
        elif view["available"]:
            shown = html.escape(str(view["value"]))
            cls = "public"
        else:
            shown = "(unavailable)"
            cls = "public"
        note = (
            f" <span class=err>{html.escape(view['error'])}</span>"
            if view.get("error")
            else ""
        )
        rows.append(
            f"<tr><td><code>{html.escape(name)}</code></td>"
            f"<td><span class={cls}>{view['classification']}</span></td>"
            f"<td>v{view['version']}</td>"
            f"<td><code>{shown}</code>{note}</td>"
            f"<td>{html.escape(view['refreshed_at'])}</td></tr>"
        )
    body = f"""<!doctype html>
<meta charset=utf-8>
<title>{html.escape(_config.name)}</title>
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #ddd; }}
  code {{ background: #f4f4f4; padding: .1rem .3rem; border-radius: 3px; }}
  .secret {{ color: #b00; font-weight: 600; }}
  .public {{ color: #070; font-weight: 600; }}
  .err {{ color: #b60; }}
  .feed-connected {{ color: #070; }} .feed-disconnected {{ color: #b00; }}
</style>
<h1>{html.escape(_config.name)}</h1>
<p>{html.escape(_config.tagline)}</p>
<p>Namespace <code>{html.escape(_config.namespace)}</code> &middot;
   change feed <span class=feed-{feed}>{feed}</span>
   (last event {html.escape(_runtime["last_feed_at"] or "never")}).</p>
<table>
  <tr><th>Key</th><th>Class</th><th>Version</th><th>Value</th><th>Refreshed</th></tr>
  {"".join(rows)}
</table>
<p><small>Public keys are shown in full; the secret-classified key is shown only
as a fingerprint — its plaintext is never exposed on any endpoint or log. Rotate
a key in HyperSecret and this page converges live, no restart. See
<code>/config</code> (JSON) and <code>/events</code> (SSE).</small></p>
"""
    return Response.html(body)


@app.get("/events")
async def events(request):
    """Server-Sent Events: an initial snapshot, then a live notice per rotation."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    with _sse_lock:
        _sse_subscribers.add(queue)

    async def gen():
        try:
            yield {"event": "snapshot", "data": fast_json_dumps(_snapshot()).decode()}
            while True:
                yield await queue.get()
        finally:
            with _sse_lock:
                _sse_subscribers.discard(queue)

    return Response.sse(gen())


app.mount_health()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else _config.port
    app.run(host="127.0.0.1", port=port)
