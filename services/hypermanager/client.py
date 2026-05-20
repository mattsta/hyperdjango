"""
HyperManager client — publish, replay, and live-watch change events.

Built on ``hyperdjango.serviceclient``: ``ManagerClient`` extends
``ServiceClient`` (retrying JSON transport, bearer/mTLS identity) and its
``watch()`` returns a framework ``ChangeFeedWatcher``. The watcher's ordering
is correct by construction — the WebSocket carries only wake hints, while
delivery and cursor advancement happen exclusively through contiguous
``replay(after=cursor)`` pulls of the hub's ledger.

    from services.hypermanager.client import ManagerClient

    mgr = ManagerClient("http://127.0.0.1:8970", token="hmk_...")
    watcher = mgr.watch(["secrets/prod/api/"], on_event=lambda ev: refresh(ev))
    ...
    watcher.stop()

mTLS: pass ``ca_file`` + ``client_cert_file``/``client_key_file`` and an
``https://`` base URL to authenticate with a client certificate instead of
a token (the feed WebSocket upgrades through the same TLS terminator).
"""

from __future__ import annotations

import urllib.parse
import uuid
from collections.abc import Callable

from hyperdjango.serviceclient import (
    AuthError,
    ChangeFeedWatcher,
    RetryPolicy,
    ServiceClient,
    ServiceError,
    ServiceUnavailable,
    service_client_env_kwargs,
)

from .subjects import subject_matches

# The app's public error names are the framework hierarchy under local aliases,
# so callers keep catching ManagerError / ManagerAuthError / ManagerUnavailable
# while the transport raises the shared types. Aliases (not subclasses) because
# ServiceClient raises the framework classes directly: `except ManagerError`
# must catch a raised ServiceError, which only an alias guarantees.
ManagerError = ServiceError
ManagerAuthError = AuthError
ManagerUnavailable = ServiceUnavailable

__all__ = [
    "ManagerAuthError",
    "ManagerClient",
    "ManagerError",
    "ManagerUnavailable",
]


class ManagerClient(ServiceClient):
    """Publish, replay, and watch. One instance per identity."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        ca_file: str = "",
        client_cert_file: str = "",
        client_key_file: str = "",
        timeout: float = 5.0,
        retries: int = 2,
    ):
        # retries counts retries on top of the first try; RetryPolicy counts the
        # first try plus retries, hence +1.
        super().__init__(
            base_url,
            token=token,
            timeout=timeout,
            retry=RetryPolicy(max_attempts=retries + 1),
            ca_file=ca_file,
            client_cert_file=client_cert_file,
            client_key_file=client_key_file,
        )

    @classmethod
    def from_env(cls, **overrides) -> ManagerClient:
        # The base-URL/token/mTLS-identity variable shape is the framework's:
        # service_client_env_kwargs reads HYPERMANAGER_URL / _TOKEN / _CA_FILE /
        # _CLIENT_CERT / _CLIENT_KEY (as base_url/token/ca_file/client_cert_file/
        # client_key_file), so the env contract lives in one place instead of a
        # hand-rolled copy here. Overrides win over the environment.
        kwargs = service_client_env_kwargs("HYPERMANAGER")
        kwargs.update(overrides)
        return cls(**kwargs)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
        idempotent: bool | None = None,
    ) -> dict:
        """Perform a request and return the parsed JSON object (``{}`` if
        empty). A thin adapter over ``ServiceClient.request`` keeping the
        app's ``body=``/``query=`` argument names."""
        return (
            self.request(
                method, path, json_body=body, params=query, idempotent=idempotent
            )
            or {}
        )

    # -- producer -------------------------------------------------------------

    def publish(self, subject: str, kind: str, metadata: dict | None = None) -> int:
        """Publish one change event; returns its ledger id (the feed cursor).

        A fresh dedupe key is minted per logical publish and the request is
        marked idempotent, so the transport MAY safely retry it: a retry that
        actually reached the hub the first time returns the same row instead of
        appending a duplicate."""
        payload = self.request(
            "POST",
            "/v1/events",
            json_body={
                "subject": subject,
                "kind": kind,
                "metadata": metadata or {},
                "dedupe_key": uuid.uuid4().hex,
            },
            idempotent=True,
        )
        return payload["id"]

    # -- consumer -------------------------------------------------------------

    def cursor(self) -> int:
        return self.request("GET", "/v1/cursor")["cursor"]

    def events(self, *, after: int = 0, prefix: str = "", limit: int = 0) -> dict:
        query: dict[str, str] = {"after": str(after)}
        if prefix:
            query["prefix"] = prefix
        if limit:
            query["limit"] = str(limit)
        return self.request("GET", "/v1/events", params=query)

    def watch(
        self,
        prefixes: list[str],
        on_event: Callable[[dict], object],
        *,
        from_cursor: int | None = None,
        client_id: str | None = None,
        on_reset: Callable[[dict], object] | None = None,
    ) -> ChangeFeedWatcher:
        """Start a background watcher; returns it (call ``.stop()`` to end).

        One call works across every hub tier — the mode-aware
        ``ChangeFeedWatcher`` adopts whatever delivery model the hub advertises
        in its hello frame, so there is no per-tier client code:

        - Default (catchup / ephemeral): the hub delivers each event in the feed
          frame. ``on_event`` fires per event; a brief reconnect replays the
          missed events (catchup) or resyncs (ephemeral, and on ring overrun).
        - Ledger: the watcher pulls the durable feed through ``/v1/events`` in
          order and the WebSocket only wakes it to pull sooner; ``watcher.cursor``
          is the last delivered ledger id.

        ``from_cursor`` seeds the ledger cursor (None = current head, future
        changes only); it is ignored by the in-frame tiers, which resume from the
        watcher's own ``last_seq``. ``client_id`` is the stable catchup reconnect
        key (generated per watcher when None). ``on_reset(response)`` fires on a
        full resync — a ledger cursor below the retention floor, or an in-memory
        ring the hub could not replay — signalling the app to re-fetch from the
        producer.

        The returned watcher reports its own liveness: ``watcher.connected``
        (plus ``wait_connected`` / ``wait_disconnected`` and the
        ``connects`` / ``disconnects`` counters). Surface it — while the feed is
        down no event arrives, so anything the consumer derives from it silently
        stops converging.
        """
        if from_cursor is None:
            # The ledger head anchors a ledger-tier watcher at "future only". The
            # in-frame tiers expose no cursor endpoint (it 404s), and they resume
            # from last_seq rather than a cursor, so fall back to 0 there.
            try:
                from_cursor = self.cursor()
            except ManagerError:
                from_cursor = 0

        ws_path = "/ws/feed"
        extra_params: dict[str, str] = {}
        delivered = on_event
        if prefixes:
            ws_path += "?" + urllib.parse.urlencode({"prefixes": ",".join(prefixes)})
            if len(prefixes) == 1:
                # The replay endpoint narrows by a single prefix, so the pull is
                # scoped server-side identically to the wake subscription.
                extra_params["prefix"] = prefixes[0]
            else:
                # The replay endpoint narrows by only ONE prefix; with several
                # requested prefixes the pull returns everything the caller's
                # grants cover — a superset of the requested set. Filter to the
                # requested prefixes client-side so watch() delivers exactly what
                # was asked for. The cursor still advances over every replayed id
                # (the watcher's own bookkeeping), so ordering and no-loss for
                # the requested set are preserved.
                requested = tuple(prefixes)

                def delivered(ev, _cb=on_event, _req=requested):
                    if any(subject_matches(p, ev["subject"]) for p in _req):
                        return _cb(ev)
                    return None

        return ChangeFeedWatcher(
            client=self,
            replay_path="/v1/events",
            ws_path=ws_path,
            on_event=delivered,
            on_reset=on_reset,
            cursor=from_cursor,
            client_id=client_id,
            prefixes=list(prefixes),
            extra_params=extra_params,
        ).start()
