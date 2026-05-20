"""
HyperSecret client SDK.

Small, synchronous, stdlib-HTTP client used by services, the ``secrets_run``
injection wrapper, and the provisioning CLI. All decryption happens here —
the server stores and returns only ciphertext and never sees plaintext.

Usage (service runtime):

    from services.hypersecret.client import SecretsClient

    client = SecretsClient(
        base_url="https://secrets.internal:8960",
        token=os.environ["HYPERSECRET_TOKEN"],
        namespace="prod/api",
        kek=load_kek_from_secure_location(),
    )

    stripe.api_key = client.secret("stripe_key")          # str, ergonomic
    with client.secret_bytes("db_password") as pw:        # wipeable, scoped
        db = connect(pw)

Caching stores *ciphertext* envelopes and decrypts per access: cache hits
skip the network while plaintext lifetime stays minimal. Revalidation uses
``known_version`` so an unchanged secret costs a body-free 304. Degradation
is fail-closed by default; pass ``stale_max`` to explicitly allow serving a
stale cached envelope for a bounded window when the server is down.
"""

import base64
import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from hyperdjango.serviceclient import (
    AuthError as _AuthError,
)
from hyperdjango.serviceclient import (
    ChangeFeedWatcher,
    RetryPolicy,
    ServiceClient,
    ServiceError,
    ServiceUnavailable,
    classify_status,
    service_client_env_kwargs,
)

from .envelope import (
    DecryptError,
    SealedEnvelope,
    load_kek_file,
    open_envelope,
    seal,
    validate_slot,
    wipe,
)

__all__ = [
    "AuthError",
    "DecryptError",
    "SecretNotFound",
    "SecretsClient",
    "SecretsError",
    "ServerUnavailable",
    "VersionConflict",
]


# The app's public error names ride the framework hierarchy. SecretsError and
# AuthError are ALIASES of the shared types (not subclasses) so the taxonomy the
# transport raises through classify_status — a framework AuthError for 401/403, a
# RequestError for any other 4xx, a ServerError for 5xx — is caught directly by
# `except SecretsError` / `except AuthError`, with 4xx-vs-5xx and the server
# detail preserved. SecretNotFound (404), VersionConflict (409), and
# ServerUnavailable are app-specific subclasses SecretsClient raises itself,
# because those statuses carry a meaning the base taxonomy does not.
SecretsError = ServiceError
AuthError = _AuthError


class SecretNotFound(SecretsError):
    """404 — no such secret or version (or hidden behind a soft-delete)."""


class ServerUnavailable(SecretsError):
    """Server unreachable and no acceptable cached value (fail closed)."""


class VersionConflict(SecretsError):
    """409 — someone else wrote the version you sealed against."""


@dataclass(slots=True)
class _CacheEntry:
    envelope: SealedEnvelope
    version: int
    metadata: dict
    fetched_at: float


class SecretsClient(ServiceClient):
    """Authenticated client for one namespace.

    Args:
        base_url: e.g. ``http://127.0.0.1:8960`` (mTLS/TLS terminates at your
            internal proxy; the bearer token authenticates the service).
        token: identity token minted by the admin API (``hsk_...``). Optional
            when a client certificate is supplied (the cert's CN authenticates).
        namespace: ``env/service`` this client reads (its KEK must match).
            ``""`` builds an admin/audit-only client with no namespace ops.
        kek: 32-byte namespace master key. ``None`` builds a fetch-only
            client that can list/audit but not decrypt.
        kek_id: this namespace's KEK generation id. **Required for writes**
            (``put_secret``/rotation); may be omitted for a read-only client.
        ca_file / client_cert_file / client_key_file: mTLS transport — an
            ``https://`` base_url with a CA to pin and (optionally) a client
            certificate. With a cert, ``token`` is optional.
        cache_ttl: seconds an envelope is served without revalidation.
        stale_max: extra seconds a *stale* envelope may be served when the
            server is unreachable. 0 (default) = fail closed.
        timeout: per-request seconds. retries: transport-failure retries
            (HTTP status errors never retry).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        namespace: str,
        kek: bytes | None = None,
        kek_id: str = "",
        ca_file: str = "",
        client_cert_file: str = "",
        client_key_file: str = "",
        cache_ttl: float = 300.0,
        stale_max: float = 0.0,
        timeout: float = 5.0,
        retries: int = 2,
    ):
        if namespace:
            validate_slot(namespace, "placeholder")
        # ServiceClient owns the transport: base URL, bearer identity, retry
        # policy, and the mTLS context (https base_url + CA pin + optional client
        # certificate whose CN authenticates the identity, token then optional).
        # ``retries`` counts retries on top of the first try; RetryPolicy counts
        # the first try plus retries, hence +1.
        super().__init__(
            base_url,
            token=token,
            timeout=timeout,
            retry=RetryPolicy(max_attempts=retries + 1),
            ca_file=ca_file,
            client_cert_file=client_cert_file,
            client_key_file=client_key_file,
        )
        self.namespace = namespace
        self._kek = kek
        self.kek_id = kek_id
        self._cache_ttl = cache_ttl
        self._stale_max = stale_max
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._resolvers: dict[str, Callable[[bytes], object]] = {}

    @classmethod
    def from_env(cls, **overrides) -> SecretsClient:
        """Build from HYPERSECRET_URL/TOKEN/NAMESPACE/KEK_FILE env vars.

        ``HYPERSECRET_KEK_FILE`` points at a JSON KEK file written by
        ``provision.py keygen`` (kek_id + key material together). Raw
        ``HYPERSECRET_KEK`` (base64) + ``HYPERSECRET_KEK_ID`` also work for
        substrates that inject env vars directly.
        """
        kek, kek_id = None, ""
        kek_file = os.environ.get("HYPERSECRET_KEK_FILE")
        if kek_file:
            kek_id, kek = load_kek_file(kek_file)
        elif os.environ.get("HYPERSECRET_KEK"):
            kek = base64.b64decode(os.environ["HYPERSECRET_KEK"])
            kek_id = os.environ.get("HYPERSECRET_KEK_ID", "")
        # base_url / token / mTLS-identity share the shape every env-driven
        # client reads — take them from the framework helper so that contract
        # lives in one place; layer on only the app-specific namespace/KEK keys.
        kwargs = {
            **service_client_env_kwargs("HYPERSECRET"),
            "namespace": os.environ.get("HYPERSECRET_NAMESPACE", ""),
            "kek": kek,
            "kek_id": kek_id,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- HTTP ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
        idempotent: bool | None = None,
    ) -> tuple[int, dict | None]:
        """Perform a request and return ``(status, body)``.

        The framework's ``ServiceClient.request_raw`` owns the transport — the
        auth header, mTLS context, no-redirect policy, and the bounded,
        idempotent-only retry/backoff. This method layers HyperSecret's own
        status semantics on top: 304 is a first-class conditional-fetch result,
        and 401/403/404/409 map to typed errors. HTTP status responses are
        definitive (never retried); transport failures retry up to the policy —
        but ONLY for idempotent verbs, so a lost POST/DELETE the server DID
        apply is never silently re-sent (a re-put would 409 and the
        conflict-retry could append a DUPLICATE version) — then fail closed
        with ``ServerUnavailable``.

        ``idempotent`` defaults (via ``request_raw``) to True for
        GET/HEAD/OPTIONS and False for POST/DELETE; pass ``idempotent=True`` for
        a safe-to-replay POST (a pure read like batch fetch). A non-idempotent
        request is tried exactly once, so a lost response never re-applies a
        write."""
        try:
            status, _headers, payload = self.request_raw(
                method, path, json_body=body, params=query, idempotent=idempotent
            )
        except ServiceUnavailable as exc:
            # Transport exhausted after the (idempotent-only) retry policy —
            # fail closed under the app's error name.
            raise ServerUnavailable(f"{self.base_url} unreachable: {exc}") from exc

        if 200 <= status < 300 or status == 304:
            return status, payload

        # A definitive HTTP status → a typed error. request_raw parsed the JSON
        # body, so the server's ``detail`` is available directly.
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        if status == 404:
            raise SecretNotFound(detail or path, status=404, detail=detail)
        if status == 409:
            raise VersionConflict(detail, status=status, detail=detail)
        # 401/403, any other 4xx, and every 5xx go through the framework status
        # taxonomy — AuthError / RequestError / ServerError — so 4xx stays
        # distinct from 5xx and the server ``detail`` rides the error, instead of
        # collapsing every non-first-class status into one opaque SecretsError.
        raise classify_status(status, detail)

    # -- Fetch + decrypt ----------------------------------------------------

    def _ns_path(self, key: str = "") -> str:
        if not self.namespace:
            raise SecretsError("Client was built without a namespace")
        base = f"/v1/secrets/{self.namespace}"
        return f"{base}/{key}" if key else base

    def _entry_to_envelope(self, payload: dict) -> tuple[SealedEnvelope, int, dict]:
        return (
            SealedEnvelope.from_dict(payload),
            payload["version"],
            payload.get("metadata") or {},
        )

    def get_envelope(self, key: str, *, version: int | None = None) -> _CacheEntry:
        """Fetch (or serve cached) the sealed envelope for ``key``.

        Pinned-version fetches bypass the cache (rollback reads are rare and
        must be exact).
        """
        if version is not None:
            _, payload = self._fetch(key, query={"version": str(version)})
            env, ver, meta = self._entry_to_envelope(payload)
            return _CacheEntry(env, ver, meta, time.monotonic())

        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
        if entry is not None and now - entry.fetched_at < self._cache_ttl:
            return entry

        query = {"known_version": str(entry.version)} if entry is not None else None
        try:
            status, payload = self._fetch(key, query=query)
        except ServerUnavailable:
            if (
                entry is not None
                and self._stale_max > 0
                and now - entry.fetched_at < self._cache_ttl + self._stale_max
            ):
                # Explicitly-permitted bounded staleness (opt-in degradation).
                return entry
            raise

        if status == 304:
            entry = _CacheEntry(entry.envelope, entry.version, entry.metadata, now)
        else:
            env, ver, meta = self._entry_to_envelope(payload)
            entry = _CacheEntry(env, ver, meta, now)
        with self._lock:
            self._cache[key] = entry
        return entry

    def _fetch(self, key: str, *, query: dict | None = None):
        return self._request("GET", self._ns_path(key), query=query)

    def _require_kek(self) -> bytes:
        if self._kek is None:
            raise SecretsError("Client has no KEK — decryption unavailable")
        return self._kek

    def get_secret_bytes(self, key: str, *, version: int | None = None) -> bytearray:
        """Decrypt and return a wipeable bytearray. Caller owns the wipe."""
        entry = self.get_envelope(key, version=version)
        return open_envelope(
            entry.envelope,
            kek=self._require_kek(),
            namespace=self.namespace,
            key=key,
            version=entry.version,
        )

    def get_secret(self, key: str, *, version: int | None = None) -> str:
        """Decrypt and return as ``str`` (the ergonomic path).

        Strings are immutable and cannot be wiped; use ``secret_bytes()``
        when you need zeroization guarantees.
        """
        buf = self.get_secret_bytes(key, version=version)
        try:
            return buf.decode("utf-8")
        finally:
            wipe(buf)

    # ``secret()`` is deliberately NOT a context manager: it returns an
    # immutable ``str`` that cannot be zeroized, so a ``with`` block would only
    # pretend to bound its lifetime. Use ``secret_bytes()`` when you need the
    # plaintext scoped and wiped.
    secret = get_secret

    @contextmanager
    def secret_bytes(self, key: str):
        """Scoped, wipeable plaintext: ``with client.secret_bytes(k) as buf:``.

        Yields a ``bytearray`` zeroized when the block exits — the only API
        here that actually bounds plaintext lifetime in memory."""
        buf = self.get_secret_bytes(key)
        try:
            yield buf
        finally:
            wipe(buf)

    def get_secrets(self, keys: list[str]) -> dict[str, str]:
        """Batch fetch + decrypt. Raises SecretNotFound naming missing keys."""
        # Batch fetch is a pure read despite being a POST (body-carried key
        # list), so it is safe to replay on a transport failure.
        _, payload = self._request(
            "POST", f"/v1/batch/{self.namespace}", body={"keys": keys}, idempotent=True
        )
        blobs = payload["secrets"]
        missing = [k for k in keys if blobs.get(k) is None]
        if missing:
            raise SecretNotFound(f"Missing in {self.namespace}: {', '.join(missing)}")
        out = {}
        kek = self._require_kek()
        for k in keys:
            env, ver, _meta = self._entry_to_envelope(blobs[k])
            buf = open_envelope(
                env, kek=kek, namespace=self.namespace, key=k, version=ver
            )
            try:
                out[k] = buf.decode("utf-8")
            finally:
                wipe(buf)
        return out

    def list_keys(self, *, include_deleted: bool = False) -> list[dict]:
        """List a namespace's keys. ``include_deleted`` (admin token) also
        returns soft-deleted-but-retained keys so a KEK rotation can rewrap
        every version an operator can still revive."""
        query = {"include_deleted": "1"} if include_deleted else None
        _, payload = self._request("GET", self._ns_path(), query=query)
        return payload["keys"]

    def list_namespaces(self) -> list[dict]:
        _, payload = self._request("GET", "/v1/namespaces")
        return payload["namespaces"]

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    # -- Write path (provisioning / rotation) --------------------------------

    def put_secret(
        self,
        key: str,
        plaintext: bytes,
        *,
        kek_id: str = "",
        metadata: dict | None = None,
    ) -> int:
        """Seal locally and append the next version. Returns the new version.

        Retries the seal exactly once on a 409 (another writer won the race)
        so rotation scripts are safe to run concurrently.
        """
        kek_id = kek_id or self.kek_id
        if not kek_id:
            raise SecretsError("kek_id required (client built without one)")
        for attempt in range(2):
            next_version = self._next_version(key)
            env = seal(
                plaintext,
                kek=self._require_kek(),
                kek_id=kek_id,
                namespace=self.namespace,
                key=key,
                version=next_version,
            )
            body = env.to_dict()
            body["version"] = next_version
            if metadata:
                body["metadata"] = metadata
            try:
                self._request("POST", self._ns_path(key), body=body)
            except VersionConflict:
                if attempt == 0:
                    continue
                raise
            self.invalidate(key)
            return next_version
        raise AssertionError("unreachable")

    def _next_version(self, key: str) -> int:
        # The versions endpoint (unlike fetch) still answers for soft-deleted
        # secrets, so a re-provision after deletion continues the version
        # sequence instead of colliding with retained history.
        try:
            return self.versions(key)["current_version"] + 1
        except SecretNotFound:
            return 1

    def delete_secret(self, key: str, *, purge: bool = False) -> None:
        query = {"purge": "1"} if purge else None
        self._request("DELETE", self._ns_path(key), query=query)
        self.invalidate(key)

    def versions(self, key: str) -> dict:
        _, payload = self._request("GET", self._ns_path(key) + "/versions")
        return payload

    # -- Admin / provisioning API (admin scope) ------------------------------
    # Thin public wrappers over the /v1/admin endpoints so operator tooling
    # drives them through a stable client surface instead of the private
    # ``_request``.

    def create_namespace(
        self, name: str, kek_id: str, *, description: str = "", owner: str = ""
    ) -> dict:
        _, payload = self._request(
            "POST",
            "/v1/admin/namespaces",
            body={
                "name": name,
                "kek_id": kek_id,
                "description": description,
                "owner": owner,
            },
        )
        return payload

    def set_namespace_kek(self, name: str, kek_id: str) -> dict:
        _, payload = self._request(
            "POST", f"/v1/admin/namespaces/{name}/kek", body={"kek_id": kek_id}
        )
        return payload

    def create_identity(self, name: str, scopes: str) -> dict:
        _, payload = self._request(
            "POST", "/v1/admin/identities", body={"name": name, "scopes": scopes}
        )
        return payload

    def revoke_identity(self, name: str) -> dict:
        _, payload = self._request("DELETE", f"/v1/admin/identities/{name}")
        return payload

    def put_grant(
        self, identity: str, namespace: str, *, read: bool = True, write: bool = False
    ) -> dict:
        _, payload = self._request(
            "POST",
            "/v1/admin/grants",
            body={
                "identity": identity,
                "namespace": namespace,
                "read": read,
                "write": write,
            },
        )
        return payload

    def review_grants(self, *, namespace: str = "") -> dict:
        query = {"namespace": namespace} if namespace else None
        _, payload = self._request("GET", "/v1/admin/grants", query=query)
        return payload

    def query_audit(self, **filters) -> dict:
        """Query the access trail. ``filters`` are the query params the
        ``/v1/audit`` endpoint accepts (namespace/key/identity/action/outcome/
        limit)."""
        query = {k: str(v) for k, v in filters.items() if v not in ("", None)}
        _, payload = self._request("GET", "/v1/audit", query=query or None)
        return payload

    def fetch_envelope_raw(
        self, key: str, *, version: int, include_deleted: bool = False
    ) -> dict:
        """Fetch one version's raw envelope payload (no decrypt) — the read half
        of a rewrap. ``include_deleted`` (admin) reaches soft-deleted-but-retained
        secrets so rotation can cover every version an operator can still revive."""
        query = {"version": str(version)}
        if include_deleted:
            query["include_deleted"] = "1"
        _, payload = self._request("GET", self._ns_path(key), query=query)
        return payload

    def rewrap_version(
        self, key: str, version: int, encrypted_dek: str, kek_id: str
    ) -> dict:
        """Replace one version's wrapped DEK (the write half of a rewrap)."""
        _, payload = self._request(
            "POST",
            self._ns_path(key) + "/rewrap",
            body={
                "version": version,
                "encrypted_dek": encrypted_dek,
                "kek_id": kek_id,
            },
        )
        return payload

    def undo_rewrap(self, key: str, version: int) -> dict:
        """Roll one version's wrapped DEK back to the pair the last rewrap
        replaced (admin scope). Recovers a version bricked by a bad rewrap; the
        undo is one-shot, so a 409 means there is no retained pair to restore
        (the version was never rewrapped, or its undo slot is already spent)."""
        _, payload = self._request(
            "POST",
            self._ns_path(key) + "/rewrap/undo",
            body={"version": version},
        )
        return payload

    # -- Resolvers (derived credentials computed post-decrypt) ---------------

    def register_resolver(self, name: str, fn) -> None:
        """Register ``fn(secret: bytes) -> Any`` for derived values (e.g.
        HMAC-derived sub-tokens). Runs client-side, after decryption."""
        self._resolvers[name] = fn

    def resolve(self, name: str, key: str):
        fn = self._resolvers[name]
        buf = self.get_secret_bytes(key)
        try:
            return fn(bytes(buf))
        finally:
            wipe(buf)

    # -- Live change watching (HyperManager integration) ----------------------

    def watch(
        self,
        manager_url: str = "",
        *,
        manager: ServiceClient | None = None,
        manager_token: str = "",
        on_change=None,
        client_id: str = "",
        ca_file: str = "",
        client_cert_file: str = "",
        client_key_file: str = "",
    ):
        """Subscribe to this namespace's change feed and invalidate on changes.

        A rotation lands as a change on the hub; the matching cached envelope is
        dropped, so the next access re-fetches and decrypts the new version —
        live convergence without a restart. ``on_change(event)`` (optional) fires
        after invalidation for app-level reactions (e.g. rebuilding a connection
        pool). Returns a ``ChangeFeedWatcher``; call ``.stop()`` to end it.

        The returned watcher reports its own liveness — ``watcher.connected``
        (with ``wait_connected`` / ``wait_disconnected`` and the
        ``connects`` / ``disconnects`` counters). A service should surface it:
        while the feed is down nothing invalidates this cache, so a rotation can
        go unnoticed for up to ``cache_ttl`` and a fetch is only as fresh as the
        last connected moment. ``connected`` flips only after a connect's resync
        has been applied, so it is also the point at which the cache is known to
        hold nothing older than that connect.

        The hub is consumed generically through the framework's mode-aware
        watcher — this module has no dependency on the hub application. Pass a
        preconstructed ``manager`` (any ``ServiceClient`` pointed at the hub) to
        reuse one connection, or give ``manager_url`` (+ ``manager_token`` / mTLS
        kwargs) to build one.

        The watcher consumes the hub's default live pub/sub, and this app's
        semantics carry no state of their own so they need nothing more: a change
        to key ``K`` invalidates ``K`` (re-check it), a resync invalidates
        everything (re-check all). The hub pushes metadata-only "subject changed"
        nudges and the client refetches on its own (stale-while-revalidate) — each
        nudge drops exactly that key, so the next access re-fetches (a body-free
        304 via ``known_version`` when nothing actually changed). On (re)connect
        the client full-resyncs (invalidate all → lazy refetch); a brief
        disconnect resumes from the hub's per-client catch-up buffer, keyed by
        ``client_id`` and scoped to ``prefixes``, replaying only the keys missed
        while away, and a buffer overrun or hub restart falls back to a resync.

        The watcher negotiates the model from the hub's hello, so pointing it at
        a hub running the opt-in durable-ledger tier is safe: with no replay
        endpoint configured it degrades to a resync on every (re)connect rather
        than pulling the ledger — correct (it never serves stale ciphertext past
        a connect), just coarser than per-key nudges.

        ``client_id`` is the stable identity a reconnect presents so the hub can
        locate this consumer's catch-up buffer; it defaults to a namespace-scoped
        id. ``prefixes`` (this namespace's scope) is announced to the hub so the
        catch-up buffer is filtered to it.
        """
        if manager is None:
            if not manager_url:
                raise SecretsError(
                    "watch() needs a manager= transport or a manager_url"
                )
            manager = ServiceClient(
                manager_url,
                token=manager_token,
                ca_file=ca_file,
                client_cert_file=client_cert_file,
                client_key_file=client_key_file,
            )
        prefix = f"secrets/{self.namespace}/"

        def _on_event(event: dict) -> None:
            subject = event.get("subject", "")
            parts = subject.split("/", 3)
            if len(parts) == 4:
                self.invalidate(parts[3])
            if on_change is not None:
                on_change(event)

        def _on_reset(resp: dict) -> None:
            # A full-resync signal: the hub kept no per-client state for this
            # connect, or the catch-up buffer overran (or a ledger-tier hub fell
            # below its retention floor). Either way the per-key deltas are
            # unavailable, so drop the ENTIRE cache and let the next access
            # re-fetch every key (a body-free 304 via known_version where nothing
            # changed) rather than serve stale ciphertext until cache_ttl.
            self.invalidate()
            if on_change is not None:
                on_change({"subject": "", "kind": "reset", "reset": True})

        # ws-only: the default hub keeps no queryable durable log, so there is no
        # cursor to fetch and no replay to pull — delivery is entirely the feed's
        # in-frame event nudges. client_id + prefixes let a reconnect resume this
        # consumer's catch-up buffer. The watcher's own on-connect resync closes
        # the warm-cache→connect gap; the explicit invalidate below drops the
        # cache synchronously the moment watch() returns, so a change that landed
        # just before connect can never be served stale while the feed is coming
        # up (the next access revalidates, a body-free 304 when nothing changed).
        ws_path = "/ws/feed?" + urllib.parse.urlencode({"prefixes": prefix})
        watcher = ChangeFeedWatcher(
            client=manager,
            ws_path=ws_path,
            on_event=_on_event,
            on_reset=_on_reset,
            client_id=client_id or f"hypersecret/{self.namespace}",
            prefixes=[prefix],
        ).start()
        self.invalidate()
        return watcher
