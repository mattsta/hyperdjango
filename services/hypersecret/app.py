"""
HyperSecret — Minimal Self-Hosted Secret Manager.

An AWS-Secrets-Manager-shaped service you run inside your own security
boundary, with one defining property: the server stores only ciphertext and
wrapped data keys (envelope encryption, client-side decryption) — it cannot
read the secrets it serves. See ARCHITECTURE.md for the full design.

Demonstrates:
  - Envelope encryption with AAD slot-binding (envelope.py — client-side only)
  - SignedAPIKeyMixin service identities (hashed at rest, revocable)
  - Fail-closed allow-list authorization with TTL grant caching (authz.py)
  - Immutable versioning with provenance + optimistic concurrency (409)
  - Batched, never-dropped audit trail (audit.py)
  - Conditional fetch (304 via known_version) + batch fetch
  - KEK rotation via audited DEK rewrap
  - Telemetry counters + /metrics, health probes, HyperAdmin, OpenAPI docs

Run:
    uv run hyper setup --app services.hypersecret.app:app --seed services.hypersecret.seed:run
    uv run hyper start --app services.hypersecret.app:app --port 8960

API:
    GET    /v1/secrets/{env}/{service}/{key}           → fetch envelope (?version, ?known_version)
    POST   /v1/secrets/{env}/{service}/{key}           → append version (write grant)
    GET    /v1/secrets/{env}/{service}                 → list keys
    GET    /v1/secrets/{env}/{service}/{key}/versions  → history + provenance
    POST   /v1/secrets/{env}/{service}/{key}/rewrap    → KEK rotation (one version)
    DELETE /v1/secrets/{env}/{service}/{key}           → soft delete (?purge=1 + admin)
    POST   /v1/batch/{env}/{service}                   → batch fetch
    GET    /v1/namespaces                              → caller's namespaces
    GET    /v1/audit                                   → audit query (audit scope)
    POST   /v1/admin/*                                 → provisioning (admin scope)
    GET    /health, /ready, /metrics, /admin/, /api/docs
"""

import base64
import contextvars
import sys
from datetime import UTC, datetime, timedelta

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.conf import DEFAULTS, get_setting, require_setting
from hyperdjango.database import get_db
from hyperdjango.db import IntegrityError, is_unique_violation
from hyperdjango.identity import has_scope, require_scope
from hyperdjango.logging import logger
from hyperdjango.mtls import MTLSTerminator
from hyperdjango.native import fast_json_dumps
from hyperdjango.openapi import mount_docs
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.standalone_middleware import (
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.tasks import TaskQueue, TaskScheduler
from hyperdjango.telemetry import configure_from_settings, mount_gated_metrics
from hyperdjango.telemetry.metrics import Counter, CounterVec

from .audit import AuditWriter
from .authz import (
    GrantCache,
    check_namespace,
    resolve_identity,
)
from .config import load_hypersecret_config
from .envelope import ALG, ENCRYPTED_DEK_BYTES, FORMAT, KEK_ID_RE, KEY_RE, SEGMENT_RE
from .models import (
    OUTBOX_PARKED,
    OUTBOX_PENDING,
    SCOPE_ADMIN,
    SCOPE_AUDIT,
    SCOPE_READ,
    SCOPE_WRITE,
    AccessLog,
    Namespace,
    NamespaceGrant,
    OutboxEvent,
    Secret,
    SecretVersion,
    ServiceIdentity,
)
from .notify import DELIVERED, RETRYABLE, ChangeNotifier

# ---------------------------------------------------------------------------
# Configuration: framework settings via `HYPER_<NAME>` env vars; app tunables
# via the HyperSecretConfig authority (`HYPERSECRET_<FIELD>` env / site.toml)
# ---------------------------------------------------------------------------

DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)
DATABASE_URL = get_setting("DATABASE_URL")

_DEBUG = get_setting("DEBUG")
_config = load_hypersecret_config()

# Ciphertext of MAX_PLAINTEXT_BYTES + nonce + tag, base64-expanded, w/ margin.
_MAX_CIPHERTEXT_B64 = 120_000
_MAX_ENCRYPTED_DEK_B64 = 128
# Client metadata is annotation, not payload: cap its serialized JSON (measured
# with the same serializer the app uses everywhere) so a caller cannot smuggle
# bulk data into the store through the metadata column. The cap is a SiteConfig
# knob; keep the module name for the tests and existing references.
_MAX_METADATA_BYTES = _config.max_metadata_bytes

# Metadata keys the server owns — clients cannot set or clear these through a
# secret write; only the server's own flows (expose) do. (rotation_due is an
# indexed column derived from client metadata; expiry_notified is a purely
# server-managed column — see the Secret model.)
SERVER_META_KEYS = frozenset({"exposed", "exposed_at"})
# Keys stripped from client-submitted metadata before it is stored: the
# server-managed metadata keys plus expiry_notified (a column, never metadata).
_CLIENT_STRIPPED_META = SERVER_META_KEYS | {"expiry_notified"}


def _parse_iso(value) -> datetime | None:
    """Parse a client-supplied ISO-8601 rotation_due into a datetime, or None.

    A timezone-naive input is interpreted as UTC (UTC tzinfo attached), so the
    stored value is always tz-aware and compares correctly against the tz-aware
    ``now`` the rotation sweep uses — a naive value would otherwise fire off by
    the writer's session offset.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


async def _enqueue_outbox(namespace: str, key: str, kind: str, metadata: dict) -> None:
    """Write a change event to the transactional outbox. Call INSIDE the same
    transaction as the state change so the notification commits atomically with
    it — the drainer posts it to the hub and deletes it on ack. No-op when the
    hub isn't configured (nothing would ever drain it)."""
    if not _notifier.enabled:
        return
    await OutboxEvent(
        subject=f"secrets/{namespace}/{key}", kind=kind, metadata=metadata
    ).save()


if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0

app = HyperApp(
    title="HyperSecret",
    database=DATABASE_URL,
    debug=_DEBUG,
    # require_setting (not get_setting) so a missing/short session-signing
    # secret fails closed at startup instead of booting on the auto-generated
    # per-process default that would silently break sessions/CSRF/signing across
    # restarts and workers. Set HYPER_SECRET_KEY (stable across seed + server).
    secret_key=require_setting("SECRET_KEY", min_length=32),
    site_config=_config,
)

_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:

    async def _metrics_resolve(request):
        """Authenticate a Prometheus scrape and publish the auth context.

        The metric bodies expose namespace names and per-namespace access
        volume, so the scrape requires a resolved identity — any valid bearer
        token or mTLS client cert, no namespace grant needed. An unauthenticated
        scrape is denied (fail closed) rather than leaking the deployment's
        namespace inventory and traffic shape. The resolved method/fingerprint
        is published for the audit trail just like every gated route.
        """
        _auth_ctx.set(("", ""))
        resolved = await resolve_identity(request)
        _auth_ctx.set((resolved.method, resolved.fingerprint))

    async def _metrics_denied(request, exc):
        # The /metrics gate is an access too: a denied scrape is audited like
        # every other rejected access.
        await _audit(request, identity="", action="metrics", outcome="denied")

    mount_gated_metrics(
        app,
        _telemetry.prometheus_sink.handler,
        resolve=_metrics_resolve,
        on_deny=_metrics_denied,
    )


app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=not _DEBUG))
# Per-IP request cap: a public secrets endpoint is otherwise an open invitation
# to mint unbounded audit rows + metric labels with grammar-valid anonymous
# spam. The default is deliberately high (see config) so it never shapes the
# steady service-to-service polling this API is built for.
app.use(
    RateLimitMiddleware(
        max_requests=_config.rate_limit_requests,
        window=_config.rate_limit_window,
        policy_name="hypersecret",
    )
)
# No CORSMiddleware: this is a service-to-service API; browsers are not a
# client, so cross-origin access stays denied by default.

REQUESTS = CounterVec(
    "hypersecret_requests_total",
    "HyperSecret API requests by action and outcome.",
    ("action", "outcome"),
)
NAMESPACE_ACCESS = CounterVec(
    "hypersecret_namespace_access_total",
    "Secret accesses per namespace and outcome (namespace cardinality is "
    "bounded by your deployment).",
    ("namespace", "outcome"),
)
OUTBOX_PARKED_TOTAL = Counter(
    "hypersecret_outbox_parked_total",
    "Outbox rows parked after a permanent (4xx) hub rejection — held out of "
    "the drain for operator inspection; a poison event never blocks the feed.",
)

_audit_writer = AuditWriter(
    flush_interval=_config.audit_flush_interval,
    flush_batch=_config.audit_flush_batch,
)
# Self-register the periodic flush + shutdown drain on the app lifecycle.
_audit_writer.install(app)
_grant_cache = GrantCache(_config.grant_cache_ttl)
_notifier = ChangeNotifier(
    manager_url=_config.manager_url, manager_token=_config.manager_token
)


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Gate: authn + scope + grant — fails closed, and every denial is audited
# ---------------------------------------------------------------------------


# Per-request authentication method + client-cert fingerprint, set by _gate
# after a successful resolve, read by _audit. A ContextVar keeps it isolated
# per request coroutine (not cached with the identity's grants, which are
# shared across requests that may authenticate differently).
_auth_ctx: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "hypersecret_auth", default=("", "")
)
# Namespaces the current caller holds a grant on, published by _gate once the
# grant snapshot is loaded. _audit reads it to keep the NAMESPACE_ACCESS metric
# label set bounded (see below). Isolated per request coroutine, like _auth_ctx.
_grants_ctx: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "hypersecret_grants", default=frozenset()
)


async def _audit(
    request, *, identity, namespace="", key="", version=0, action, outcome
):
    REQUESTS.inc_tuple((action, outcome))
    # NAMESPACE_ACCESS carries the namespace NAME as a metric label, so its
    # cardinality must stay bounded by the deployment's provisioned namespaces.
    # Meter only a namespace the caller actually holds a grant on (a real,
    # bounded set): an unauthenticated or ungranted request names an
    # attacker-chosen path segment, which must never mint a new label pair. The
    # AccessLog row below still records the true namespace — that table is
    # bounded by audit retention + the app-wide rate limit, not by label count.
    if namespace and namespace in _grants_ctx.get():
        NAMESPACE_ACCESS.inc_tuple((namespace, outcome))
    method, fingerprint = _auth_ctx.get()
    await _audit_writer.record(
        identity=identity,
        namespace=namespace,
        key=key,
        version=version,
        action=action,
        outcome=outcome,
        client_ip=request.client_ip or "",
        auth_method=method,
        fingerprint=fingerprint,
    )


async def _gate(request, *, scope, action, namespace=None, write=False, key=""):
    """Authenticate + authorize or raise, auditing the denial either way."""
    identity_name = ""
    _auth_ctx.set(("", ""))
    _grants_ctx.set(frozenset())
    try:
        resolved = await resolve_identity(request)
        _auth_ctx.set((resolved.method, resolved.fingerprint))
        identity = resolved.identity
        identity_name = identity.name
        caller = await _grant_cache.get(identity)
        # Publish the caller's held namespaces so _audit meters only those (a
        # scope/namespace denial for a namespace the caller does NOT hold, or an
        # unauthenticated request, then never mints a metric label).
        _grants_ctx.set(frozenset(caller.namespace_ids))
        require_scope(caller.scopes, scope)
        if namespace is not None:
            check_namespace(caller, namespace, write=write)
    except HTTPException:
        await _audit(
            request,
            identity=identity_name,
            namespace=namespace or "",
            key=key,
            action=action,
            outcome="denied",
        )
        raise
    return caller


async def _audit_invalid(request, caller, *, action, namespace="", key=""):
    """Audit a post-gate input rejection (a 400/404-class client error) under
    outcome ``invalid``.

    The audit contract (ARCHITECTURE §6/§11) records EVERY access, including one
    rejected for bad input — otherwise a caller can probe the API without
    leaving a trail. Route handlers call this from their own validation
    ``except`` block so the row lands on a clean connection, mirroring
    put_secret's original inline pattern in one shared, gate-adjacent helper."""
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        action=action,
        outcome="invalid",
    )


def _validate_slot_or_400(env: str, service: str, key: str = "x") -> str:
    """Return 'env/service' after grammar validation (shared with envelope.py)."""
    if not (SEGMENT_RE.match(env) and SEGMENT_RE.match(service)):
        raise HTTPException(400, "Invalid namespace segment")
    if not KEY_RE.match(key):
        raise HTTPException(400, "Invalid secret key name")
    return f"{env}/{service}"


async def _load_secret(namespace_id: int, key: str) -> Secret | None:
    return await Secret.objects.filter(namespace_id=namespace_id, key=key).first()


async def _json_object(request) -> dict:
    """Read the request body as a JSON object, or raise a 400.

    ``request.json()`` already raises a 400 on a malformed body; this adds the
    object-shape check so a JSON scalar or array (valid JSON, wrong shape)
    becomes a clean 400 the handler audits under ``invalid`` — instead of an
    ``AttributeError`` on the first ``body.get(...)`` that would escape as an
    unaudited generic 500. Every handler that reads a JSON body goes through
    here so the contract lives in one place."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return body


def _b64_field(
    body: dict, name: str, max_len: int, *, exact_bytes: int | None = None
) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise HTTPException(400, f"Field {name!r} missing or oversized")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError, TypeError:
        raise HTTPException(400, f"Field {name!r} is not valid base64") from None
    # Exact-size fields (the wrapped DEK) must decode to precisely the wire size
    # the crypto authority defines — a short/oversized blob is a bug or an
    # attempt to brick the version, never a valid envelope.
    if exact_bytes is not None and len(decoded) != exact_bytes:
        raise HTTPException(
            400, f"Field {name!r} must decode to exactly {exact_bytes} bytes"
        )
    return value


def _int_param_or_400(value: str, field: str) -> int:
    """Parse a query int STRICTLY (ASCII digits only), or 400.

    ``str.isdigit()`` is True for Unicode digit forms like ``"²"`` that
    ``int()`` then refuses — parsing on ``isdigit()`` alone 500s on such input.
    Requiring ASCII closes that, and a non-numeric value is a client error
    (400), never a silent fall-through to a default.
    """
    if not (value.isascii() and value.isdigit()):
        raise HTTPException(400, f"Invalid {field!r} (want a non-negative integer)")
    return int(value)


def _version_dict(sv: SecretVersion, namespace: str, key: str) -> dict:
    return {
        "namespace": namespace,
        "key": key,
        "version": sv.version,
        "format": FORMAT,
        "alg": sv.alg,
        "kek_id": sv.kek_id,
        "ciphertext": sv.ciphertext,
        "encrypted_dek": sv.encrypted_dek,
        "created_at": sv.created_at.isoformat(),
        "created_by": sv.created_by,
    }


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.json({"service": "hypersecret", "api": "/v1", "docs": "/api/docs"})


@app.get("/v1/secrets/{env}/{service}/{key}")
async def fetch_secret(request, env: str, service: str, key: str):
    """Fetch the envelope for a secret.

    ``?version=N`` pins a historical version (rollback); ``?known_version=N``
    returns 304 with no body when the caller already has the latest —
    steady-state pollers cost one indexed lookup and no payload bytes.
    """
    namespace = _validate_slot_or_400(env, service, key)
    caller = await _gate(
        request, scope=SCOPE_READ, action="read", namespace=namespace, key=key
    )

    secret = await _load_secret(caller.namespace_ids[namespace], key)
    # A soft-deleted secret reads as 404 for everyone EXCEPT an admin who
    # explicitly opts in (?include_deleted=1) — the envelope of a deleted
    # secret must not leak to an ordinary reader, but KEK rotation needs to
    # fetch it to rewrap the retained versions.
    allow_deleted = request.query("include_deleted") == "1" and has_scope(
        caller.scopes, SCOPE_ADMIN
    )
    if (
        secret is None
        or secret.current_version == 0
        or (secret.deleted_at is not None and not allow_deleted)
    ):
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            action="read",
            outcome="not_found",
        )
        raise HTTPException(404, "Secret not found")

    pinned = request.query("version")
    if pinned:
        try:
            version = _int_param_or_400(pinned, "version")
            if version < 1:
                raise HTTPException(400, "Invalid 'version' (want a positive integer)")
        except HTTPException:
            await _audit_invalid(
                request, caller, action="read", namespace=namespace, key=key
            )
            raise
    else:
        version = secret.current_version

    known = request.query("known_version")
    if pinned is None and known:
        try:
            known_version = _int_param_or_400(known, "known_version")
        except HTTPException:
            await _audit_invalid(
                request, caller, action="read", namespace=namespace, key=key
            )
            raise
    else:
        known_version = None
    if known_version is not None and known_version == version:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="read",
            outcome="not_modified",
        )
        return Response(b"", status=304)

    sv = await SecretVersion.objects.filter(
        secret_id=secret.id, version=version
    ).first()
    if sv is None:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="read",
            outcome="not_found",
        )
        raise HTTPException(404, "Version not found")

    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        version=version,
        action="read",
        outcome="ok",
    )
    payload = _version_dict(sv, namespace, key)
    payload["current_version"] = secret.current_version
    payload["metadata"] = secret.metadata
    return Response.json(payload)


@app.get("/v1/secrets/{env}/{service}")
async def list_keys(request, env: str, service: str):
    """List keys in a namespace — drives secrets_run discovery.

    Soft-deleted secrets are hidden by default. ``?include_deleted=1`` (admin
    scope) additionally lists soft-deleted-but-retained keys so KEK rotation
    (rewrap) can cover every version an operator can still revive.
    """
    namespace = _validate_slot_or_400(env, service)
    caller = await _gate(request, scope=SCOPE_READ, action="list", namespace=namespace)
    include_deleted = request.query("include_deleted") == "1"
    if include_deleted:
        try:
            require_scope(caller.scopes, SCOPE_ADMIN)
        except HTTPException:
            await _audit(
                request,
                identity=caller.identity_name,
                namespace=namespace,
                action="list",
                outcome="denied",
            )
            raise
    filters = {"namespace_id": caller.namespace_ids[namespace]}
    if not include_deleted:
        filters["deleted_at"] = None
    secrets = await Secret.objects.filter(**filters).order_by("key").all()
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        action="list",
        outcome="ok",
    )
    return Response.json(
        {
            "namespace": namespace,
            "keys": [
                {
                    "key": s.key,
                    "current_version": s.current_version,
                    "updated_at": s.updated_at.isoformat(),
                    "deleted": s.deleted_at is not None,
                    "metadata": s.metadata,
                }
                for s in secrets
                if s.current_version > 0
            ],
        }
    )


@app.get("/v1/secrets/{env}/{service}/{key}/versions")
async def list_versions(request, env: str, service: str, key: str):
    """Version history with provenance — envelopes omitted (fetch pins them)."""
    namespace = _validate_slot_or_400(env, service, key)
    caller = await _gate(
        request, scope=SCOPE_READ, action="versions", namespace=namespace, key=key
    )
    secret = await _load_secret(caller.namespace_ids[namespace], key)
    if secret is None:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            action="versions",
            outcome="not_found",
        )
        raise HTTPException(404, "Secret not found")
    versions = (
        await SecretVersion.objects.filter(secret_id=secret.id)
        .order_by("-version")
        .all()
    )
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        action="versions",
        outcome="ok",
    )
    return Response.json(
        {
            "namespace": namespace,
            "key": key,
            "current_version": secret.current_version,
            "deleted_at": secret.deleted_at.isoformat() if secret.deleted_at else None,
            "versions": [
                {
                    "version": v.version,
                    "alg": v.alg,
                    "kek_id": v.kek_id,
                    "created_at": v.created_at.isoformat(),
                    "created_by": v.created_by,
                    "rewrapped_at": v.rewrapped_at.isoformat()
                    if v.rewrapped_at
                    else None,
                    "rewrapped_by": v.rewrapped_by,
                }
                for v in versions
            ],
        }
    )


@app.post("/v1/batch/{env}/{service}")
async def batch_fetch(request, env: str, service: str):
    """Fetch many envelopes in one round trip. Missing keys map to null."""
    namespace = _validate_slot_or_400(env, service)
    # action="batch_read" on the gate too, so a gate denial and the per-key rows
    # below share one action taxonomy instead of a gate "read" vs per-key
    # "batch_read" split.
    caller = await _gate(
        request, scope=SCOPE_READ, action="batch_read", namespace=namespace
    )
    # Malformed body / key list is audited under the same batch_read action
    # before the 400 propagates (request.json() itself raises 400 on bad JSON).
    try:
        body = await _json_object(request)
        keys = body.get("keys")
        if not isinstance(keys, list) or not keys or len(keys) > _config.batch_max_keys:
            raise HTTPException(
                400, f"'keys' must be a list of 1..{_config.batch_max_keys}"
            )
        for k in keys:
            if not isinstance(k, str) or not KEY_RE.match(k):
                raise HTTPException(400, f"Invalid secret key name: {k!r}")
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(
                request, caller, action="batch_read", namespace=namespace
            )
        raise

    ns_id = caller.namespace_ids[namespace]
    secrets = {
        s.key: s
        for s in await Secret.objects.filter(
            namespace_id=ns_id, key__in=keys, deleted_at=None
        ).all()
    }
    live = [s for s in secrets.values() if s.current_version > 0]
    sv_by_secret = {}
    if live:
        # ``version__in`` is a cross-product across all live secrets, so a
        # retained OLDER version of secret A can match secret B's
        # current_version and be returned too. Keep only the row that is each
        # secret's OWN current version, keyed by secret_id — a bare
        # last-write-wins here would let a stale row overwrite the current one
        # and report an existing secret as null (SecretNotFound downstream).
        current_by_id = {s.id: s.current_version for s in live}
        for sv in await SecretVersion.objects.filter(
            secret_id__in=[s.id for s in live],
            version__in=[s.current_version for s in live],
        ).all():
            if sv.version == current_by_id.get(sv.secret_id):
                sv_by_secret[sv.secret_id] = sv

    out: dict[str, dict | None] = {}
    for k in keys:
        secret = secrets.get(k)
        sv = sv_by_secret.get(secret.id) if secret else None
        if secret is not None and sv is not None:
            entry = _version_dict(sv, namespace, k)
            entry["metadata"] = secret.metadata
            out[k] = entry
            outcome, version = "ok", sv.version
        else:
            out[k] = None
            outcome, version = "not_found", 0
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=k,
            version=version,
            action="batch_read",
            outcome=outcome,
        )
    return Response.json({"namespace": namespace, "secrets": out})


@app.get("/v1/namespaces")
async def list_namespaces(request):
    """Namespaces this identity holds grants for (from the cached snapshot)."""
    caller = await _gate(request, scope=SCOPE_READ, action="namespaces")
    await _audit(
        request, identity=caller.identity_name, action="namespaces", outcome="ok"
    )
    return Response.json(
        {
            "namespaces": [
                {"name": name, "read": rw[0], "write": rw[1]}
                for name, rw in sorted(caller.namespaces.items())
            ]
        }
    )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class _VersionConflict(Exception):
    """Optimistic-concurrency conflict detected inside the write transaction.

    Raised to roll the transaction back (dropping any just-created empty
    Secret row) so the conflict audit row is written only AFTER the block
    exits, on a clean connection. Auditing inside the doomed transaction can
    trip the batched flush, whose bulk_create — carrying buffered rows from
    unrelated requests — would then be discarded with the rollback.
    """

    def __init__(self, expected: int):
        self.expected = expected


@app.post("/v1/secrets/{env}/{service}/{key}")
async def put_secret(request, env: str, service: str, key: str):
    """Append an immutable version (create, update, and rotation are all this).

    The client seals against ``current_version + 1`` and sends that number;
    a mismatch is a 409 — optimistic concurrency, and the AAD a future reader
    verifies is exactly the version the writer sealed.
    """
    namespace = _validate_slot_or_400(env, service, key)
    caller = await _gate(
        request,
        scope=SCOPE_WRITE,
        action="write",
        namespace=namespace,
        write=True,
        key=key,
    )
    # Post-gate 400-class validation failures are audited with outcome
    # "invalid" (the audit contract: every access, including a rejected one,
    # produces a row) before the client-facing 400 propagates.
    try:
        body = await _json_object(request)

        if body.get("format") != FORMAT or body.get("alg") != ALG:
            raise HTTPException(400, f"Envelope must be format={FORMAT} alg={ALG}")
        ciphertext = _b64_field(body, "ciphertext", _MAX_CIPHERTEXT_B64)
        encrypted_dek = _b64_field(
            body,
            "encrypted_dek",
            _MAX_ENCRYPTED_DEK_B64,
            exact_bytes=ENCRYPTED_DEK_BYTES,
        )
        kek_id = body.get("kek_id", "")
        if not isinstance(kek_id, str) or not KEK_ID_RE.match(kek_id):
            raise HTTPException(400, "Invalid kek_id")
        version = body.get("version")
        if not isinstance(version, int) or version < 1:
            raise HTTPException(400, "Field 'version' must be a positive integer")
        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            raise HTTPException(400, "Field 'metadata' must be an object")
        # Measure with the framework serializer the app emits everywhere
        # (fast_json_dumps returns bytes) rather than stdlib json, so the byte
        # cap matches what the store actually persists.
        if len(fast_json_dumps(metadata)) > _MAX_METADATA_BYTES:
            raise HTTPException(
                400,
                f"Field 'metadata' exceeds the {_MAX_METADATA_BYTES}-byte "
                "serialized-JSON limit",
            )

        ns_id = caller.namespace_ids[namespace]
        ns = await Namespace.objects.filter(id=ns_id).first()
        if kek_id != ns.kek_id:
            raise HTTPException(
                400,
                f"Envelope sealed under kek_id {kek_id!r} but namespace expects "
                f"{ns.kek_id!r} — refresh your KEK",
            )
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit(
                request,
                identity=caller.identity_name,
                namespace=namespace,
                key=key,
                action="write",
                outcome="invalid",
            )
        raise

    # Client metadata can never touch server-managed keys (exposure /
    # expiry flags): a write-scoped caller must not be able to clear an
    # `exposed` marking or suppress an `expired` event by rotating.
    client_meta = {k: v for k, v in metadata.items() if k not in _CLIENT_STRIPPED_META}

    db = get_db()
    try:
        async with db.transaction():
            # Lock the Secret row (if it exists) so concurrent writers to the
            # same key serialize on the version check instead of racing into a
            # duplicate-version IntegrityError.
            secret = (
                await Secret.objects.filter(namespace_id=ns_id, key=key)
                .select_for_update()
                .first()
            )
            if secret is None:
                secret = Secret(namespace_id=ns_id, key=key, metadata=client_meta)
                await secret.save()
            expected = secret.current_version + 1
            if version != expected:
                raise _VersionConflict(expected)
            sv = SecretVersion(
                secret_id=secret.id,
                version=version,
                alg=ALG,
                kek_id=kek_id,
                ciphertext=ciphertext,
                encrypted_dek=encrypted_dek,
                created_by=caller.identity_name,
            )
            await sv.save()
            # Merge: preserve server-managed keys already on the row, overlay
            # the caller's non-reserved keys.
            merged = {k: v for k, v in secret.metadata.items() if k in SERVER_META_KEYS}
            merged.update(client_meta)
            # Derive the indexed rotation_due column from client metadata; a
            # new version re-arms the expiry notification.
            await Secret.objects.filter(id=secret.id).update(
                current_version=version,
                deleted_at=None,
                metadata=merged,
                rotation_due=_parse_iso(client_meta.get("rotation_due")),
                expiry_notified=False,
            )
            # Same transaction as the version write: the notification can't be
            # lost between commit and post.
            await _enqueue_outbox(
                namespace,
                key,
                "created" if version == 1 else "rotated",
                {"version": version, "kek_id": kek_id},
            )
    except _VersionConflict as conflict:
        # Audited here, after the transaction has rolled back, so the audit
        # write lands on a clean connection and never rides the doomed one.
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="write",
            outcome="conflict",
        )
        raise HTTPException(
            409, f"Version conflict: expected {conflict.expected}, got {version}"
        ) from None
    except IntegrityError as exc:
        # First-write race (two creators, unique namespace_id+key) that the row
        # lock can't cover surfaces as a unique violation. This insert also rides
        # FK/not-null constraints, so narrow the typed IntegrityError to the
        # unique case and re-raise every other one as the real fault it is.
        if not is_unique_violation(exc):
            raise
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="write",
            outcome="conflict",
        )
        raise HTTPException(
            409, f"Version conflict on {namespace}/{key} — retry"
        ) from None

    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        version=version,
        action="write",
        outcome="ok",
    )
    return Response.json(
        {"namespace": namespace, "key": key, "version": version}, status=201
    )


@app.post("/v1/secrets/{env}/{service}/{key}/rewrap")
async def rewrap_secret(request, env: str, service: str, key: str):
    """KEK rotation: replace one version's wrapped DEK.

    The payload ciphertext is never touched — the wrapped DEK is the only
    mutable column on a stored version, and every rewrap is audited.
    """
    namespace = _validate_slot_or_400(env, service, key)
    caller = await _gate(
        request,
        scope=SCOPE_WRITE,
        action="rewrap",
        namespace=namespace,
        write=True,
        key=key,
    )
    # Post-gate 400-class validation is audited (outcome invalid) before the
    # client-facing 400 propagates — request.json() itself raises 400 on bad
    # JSON, and an oversized/undersized encrypted_dek must never overwrite a
    # good wrapped DEK in place, so the exact wire size is enforced here.
    try:
        body = await _json_object(request)
        encrypted_dek = _b64_field(
            body,
            "encrypted_dek",
            _MAX_ENCRYPTED_DEK_B64,
            exact_bytes=ENCRYPTED_DEK_BYTES,
        )
        kek_id = body.get("kek_id", "")
        if not isinstance(kek_id, str) or not KEK_ID_RE.match(kek_id):
            raise HTTPException(400, "Invalid kek_id")
        version = body.get("version")
        if not isinstance(version, int) or version < 1:
            raise HTTPException(400, "Field 'version' must be a positive integer")
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(
                request, caller, action="rewrap", namespace=namespace, key=key
            )
        raise

    # Rewrap deliberately loads the secret WITHOUT a deleted_at filter: KEK
    # rotation must cover soft-deleted-but-retained secrets, else a revived
    # secret's earlier versions become undecryptable once the old KEK is retired.
    secret = await _load_secret(caller.namespace_ids[namespace], key)
    if secret is None:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="rewrap",
            outcome="not_found",
        )
        raise HTTPException(404, "Secret not found")
    db = get_db()
    version_missing = False
    async with db.transaction():
        # Lock the version and capture the prior (encrypted_dek, kek_id) so the
        # rewrap retains a one-deep undo: a bad rewrap can be rolled back to the
        # immediately-preceding wrapped DEK instead of bricking the version.
        sv = (
            await SecretVersion.objects.filter(secret_id=secret.id, version=version)
            .select_for_update()
            .first()
        )
        if sv is not None:
            await SecretVersion.objects.filter(id=sv.id).update(
                encrypted_dek=encrypted_dek,
                kek_id=kek_id,
                prev_encrypted_dek=sv.encrypted_dek,
                prev_kek_id=sv.kek_id,
                rewrapped_at=datetime.now(UTC),
                rewrapped_by=caller.identity_name,
            )
            await _enqueue_outbox(
                namespace, key, "rewrapped", {"version": version, "kek_id": kek_id}
            )
        else:
            # No matching version — nothing was written, so this empty
            # transaction commits harmlessly and the not_found row is audited
            # below on a clean connection (never inside a doomed transaction).
            version_missing = True
    if version_missing:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="rewrap",
            outcome="not_found",
        )
        raise HTTPException(404, "Version not found")

    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        version=version,
        action="rewrap",
        outcome="ok",
    )
    return Response.json({"namespace": namespace, "key": key, "version": version})


@app.post("/v1/secrets/{env}/{service}/{key}/rewrap/undo")
async def undo_rewrap(request, env: str, service: str, key: str):
    """Roll one version's wrapped DEK back to the pair the last rewrap replaced.

    Every rewrap retains the immediately-preceding ``(encrypted_dek, kek_id)``
    (the one-deep undo columns). A rewrap that wrote an unusable blob — a bug or
    a compromised rotation — would otherwise brick the version, since the payload
    ciphertext can only be decrypted with a DEK wrapped under a KEK a client
    holds. This admin-scoped, audited path swaps the current pair back to the
    retained one and clears the undo slot (one-shot; a second undo has nothing to
    restore). The restored ``kek_id`` is deliberately NOT checked against the
    namespace's current KEK — recovery means going back to the prior generation.
    """
    namespace = _validate_slot_or_400(env, service, key)
    caller = await _gate(
        request,
        scope=SCOPE_ADMIN,
        action="rewrap_undo",
        namespace=namespace,
        write=True,
        key=key,
    )
    try:
        body = await _json_object(request)
        version = body.get("version")
        if not isinstance(version, int) or version < 1:
            raise HTTPException(400, "Field 'version' must be a positive integer")
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(
                request, caller, action="rewrap_undo", namespace=namespace, key=key
            )
        raise

    # No deleted_at filter: undo must reach a soft-deleted-but-retained secret's
    # versions too, exactly like rewrap.
    secret = await _load_secret(caller.namespace_ids[namespace], key)
    if secret is None:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="rewrap_undo",
            outcome="not_found",
        )
        raise HTTPException(404, "Secret not found")

    db = get_db()
    restored_kek_id = ""
    outcome = ""
    async with db.transaction():
        sv = (
            await SecretVersion.objects.filter(secret_id=secret.id, version=version)
            .select_for_update()
            .first()
        )
        if sv is None:
            outcome = "not_found"
        elif not sv.prev_encrypted_dek:
            # The version was never rewrapped (or its undo slot is already
            # spent) — there is no retained pair to restore.
            outcome = "invalid"
        else:
            restored_kek_id = sv.prev_kek_id
            await SecretVersion.objects.filter(id=sv.id).update(
                encrypted_dek=sv.prev_encrypted_dek,
                kek_id=sv.prev_kek_id,
                prev_encrypted_dek="",
                prev_kek_id="",
                rewrapped_at=datetime.now(UTC),
                rewrapped_by=caller.identity_name,
            )
            await _enqueue_outbox(
                namespace,
                key,
                "rewrapped",
                {"version": version, "kek_id": restored_kek_id, "undo": True},
            )
    if outcome == "not_found":
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="rewrap_undo",
            outcome="not_found",
        )
        raise HTTPException(404, "Version not found")
    if outcome == "invalid":
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            version=version,
            action="rewrap_undo",
            outcome="invalid",
        )
        raise HTTPException(409, "No retained wrapped DEK to restore for this version")

    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        version=version,
        action="rewrap_undo",
        outcome="ok",
    )
    return Response.json(
        {
            "namespace": namespace,
            "key": key,
            "version": version,
            "kek_id": restored_kek_id,
        }
    )


@app.delete("/v1/secrets/{env}/{service}/{key}")
async def delete_secret(request, env: str, service: str, key: str):
    """Soft delete (default) or hard purge (``?purge=1``, admin scope)."""
    namespace = _validate_slot_or_400(env, service, key)
    purge = request.query("purge") == "1"
    caller = await _gate(
        request,
        scope=SCOPE_WRITE,
        action="purge" if purge else "delete",
        namespace=namespace,
        write=True,
        key=key,
    )
    if purge:
        # Admin scope is checked AFTER the write-grant gate; audit its denial
        # too so a purge attempt without admin scope leaves a trail.
        try:
            require_scope(caller.scopes, SCOPE_ADMIN)
        except HTTPException:
            await _audit(
                request,
                identity=caller.identity_name,
                namespace=namespace,
                key=key,
                action="purge",
                outcome="denied",
            )
            raise

    secret = await _load_secret(caller.namespace_ids[namespace], key)
    if secret is None:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            action="purge" if purge else "delete",
            outcome="not_found",
        )
        raise HTTPException(404, "Secret not found")

    db = get_db()
    async with db.transaction():
        if purge:
            await SecretVersion.objects.filter(secret_id=secret.id).delete()
            await Secret.objects.filter(id=secret.id).delete()
        else:
            await Secret.objects.filter(id=secret.id).update(
                deleted_at=datetime.now(UTC)
            )
        await _enqueue_outbox(
            namespace,
            key,
            "purged" if purge else "deleted",
            {"version": secret.current_version},
        )

    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        version=secret.current_version,
        action="purge" if purge else "delete",
        outcome="ok",
    )
    return Response.json({"deleted": True, "purged": purge})


@app.post("/v1/secrets/{env}/{service}/{key}/expose")
async def mark_exposed(request, env: str, service: str, key: str):
    """Mark a secret as exposed/compromised (admin scope).

    The secret keeps serving — killing it outright would turn an incident
    into an outage — but the marking lands in its metadata, the audit
    trail, and a change event, so subscribers and operators rotate it
    immediately.
    """
    namespace = _validate_slot_or_400(env, service, key)
    caller = await _gate(
        request,
        scope=SCOPE_ADMIN,
        action="expose",
        namespace=namespace,
        write=True,
        key=key,
    )
    ns_id = caller.namespace_ids[namespace]
    db = get_db()
    current_version = 0
    secret_missing = False
    async with db.transaction():
        # Lock the row for the read-modify-write so a concurrent expose or
        # rotation cannot overwrite the merged metadata (lost update) — same
        # locking shape as put_secret's version check.
        secret = (
            await Secret.objects.filter(namespace_id=ns_id, key=key)
            .select_for_update()
            .first()
        )
        if secret is None:
            # Empty transaction commits harmlessly; the not_found row is audited
            # below on a clean connection, never inside a doomed transaction.
            secret_missing = True
        else:
            current_version = secret.current_version
            metadata = dict(secret.metadata)
            metadata["exposed"] = True
            metadata["exposed_at"] = datetime.now(UTC).isoformat()
            await Secret.objects.filter(id=secret.id).update(metadata=metadata)
            await _enqueue_outbox(
                namespace, key, "exposed", {"version": secret.current_version}
            )
    if secret_missing:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            key=key,
            action="expose",
            outcome="not_found",
        )
        raise HTTPException(404, "Secret not found")
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        key=key,
        version=current_version,
        action="expose",
        outcome="ok",
    )
    return Response.json({"namespace": namespace, "key": key, "exposed": True})


# ---------------------------------------------------------------------------
# Audit query
# ---------------------------------------------------------------------------


@app.get("/v1/audit")
async def query_audit(request):
    """Query the access trail. Requires the ``audit`` scope."""
    caller = await _gate(request, scope=SCOPE_AUDIT, action="audit_query")
    # Drain buffered rows first so auditors always read their own era.
    await _audit_writer.flush_pending()
    qs = AccessLog.objects
    namespace = request.query("namespace")
    if namespace:
        qs = qs.filter(namespace=namespace)
    key = request.query("key")
    if key:
        qs = qs.filter(key=key)
    identity = request.query("identity")
    if identity:
        qs = qs.filter(identity=identity)
    action = request.query("action")
    if action:
        qs = qs.filter(action=action)
    outcome = request.query("outcome")
    if outcome:
        qs = qs.filter(outcome=outcome)
    limit_raw = request.query("limit", "100")
    # ASCII-strict so a Unicode-digit ``limit`` cannot 500 on int(); a
    # non-numeric limit falls back to the default rather than erroring (an
    # auditor's soft paging hint, not a hard client contract).
    limit = min(
        int(limit_raw) if limit_raw.isascii() and limit_raw.isdigit() else 100, 500
    )

    rows = await qs.order_by("-id").limit(limit).all()
    await _audit(
        request, identity=caller.identity_name, action="audit_query", outcome="ok"
    )
    return Response.json(
        {
            "entries": [
                {
                    "at": r.created_at.isoformat(),
                    "identity": r.identity,
                    "namespace": r.namespace,
                    "key": r.key,
                    "version": r.version,
                    "action": r.action,
                    "outcome": r.outcome,
                    "client_ip": r.client_ip,
                    "auth_method": r.auth_method,
                    "fingerprint": r.fingerprint,
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# Admin provisioning API (admin scope) — namespaces, identities, grants
# ---------------------------------------------------------------------------


@app.post("/v1/admin/namespaces")
async def create_namespace(request):
    caller = await _gate(request, scope=SCOPE_ADMIN, action="admin_namespace")
    try:
        body = await _json_object(request)
        name = body.get("name", "")
        kek_id = body.get("kek_id", "")
        parts = name.split("/") if isinstance(name, str) else []
        if len(parts) != 2 or not all(SEGMENT_RE.match(p) for p in parts):
            raise HTTPException(400, "Invalid namespace name (want env/service)")
        if not isinstance(kek_id, str) or not KEK_ID_RE.match(kek_id):
            raise HTTPException(400, "Invalid kek_id")
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(request, caller, action="admin_namespace")
        raise

    ns = Namespace(
        name=name,
        kek_id=kek_id,
        description=str(body.get("description", "")),
        owner=str(body.get("owner", "")),
    )
    # The unique(name) constraint is the source of truth: catch its violation
    # (rather than an exists-check-then-insert that races two concurrent creates
    # into a raw 500) and report a clean 409. name is the only constraint this
    # insert can trip, so the typed IntegrityError is always the duplicate race.
    try:
        await ns.save()
    except IntegrityError:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=name,
            action="admin_namespace",
            outcome="conflict",
        )
        raise HTTPException(409, "Namespace already exists") from None
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=name,
        action="admin_namespace",
        outcome="ok",
    )
    return Response.json({"name": name, "kek_id": kek_id}, status=201)


@app.post("/v1/admin/namespaces/{env}/{service}/kek")
async def set_namespace_kek(request, env: str, service: str):
    """Declare the namespace's current KEK generation.

    New writes must seal under this kek_id; called after every version has
    been rewrapped so stale-KEK provisioning is rejected at write time.
    """
    namespace = _validate_slot_or_400(env, service)
    caller = await _gate(request, scope=SCOPE_ADMIN, action="admin_kek")
    try:
        body = await _json_object(request)
        kek_id = body.get("kek_id", "")
        if not isinstance(kek_id, str) or not KEK_ID_RE.match(kek_id):
            raise HTTPException(400, "Invalid kek_id")
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(
                request, caller, action="admin_kek", namespace=namespace
            )
        raise
    updated = await Namespace.objects.filter(name=namespace).update(kek_id=kek_id)
    if not updated:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace,
            action="admin_kek",
            outcome="not_found",
        )
        raise HTTPException(404, "Namespace not found")
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace,
        action="admin_kek",
        outcome="ok",
    )
    return Response.json({"name": namespace, "kek_id": kek_id})


@app.post("/v1/admin/identities")
async def create_identity(request):
    """Mint a service identity. The raw token appears in this response only."""
    caller = await _gate(request, scope=SCOPE_ADMIN, action="admin_identity")
    try:
        body = await _json_object(request)
        name = body.get("name", "")
        if not isinstance(name, str) or not (3 <= len(name) <= 128):
            raise HTTPException(400, "Identity name must be 3..128 chars")
        scopes = body.get("scopes", SCOPE_READ)
        if not isinstance(scopes, str) or not scopes:
            raise HTTPException(400, "Invalid scopes")
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(
                request,
                caller,
                action="admin_identity",
                key=name if isinstance(name, str) else "",
            )
        raise

    # The unique(name) constraint decides duplicates (race-free), not an
    # exists-check that two concurrent creates could both pass. name is the only
    # unique key this insert can trip, so the typed IntegrityError is that clash.
    try:
        result = await ServiceIdentity.generate(name=name, scopes=scopes)
    except IntegrityError:
        await _audit(
            request,
            identity=caller.identity_name,
            action="admin_identity",
            outcome="conflict",
            key=name,
        )
        raise HTTPException(409, "Identity already exists") from None
    await _audit(
        request,
        identity=caller.identity_name,
        action="admin_identity",
        outcome="ok",
        key=name,
    )
    return Response.json(
        {"name": name, "scopes": scopes, "token": result.raw_key}, status=201
    )


@app.delete("/v1/admin/identities/{name}")
async def revoke_identity(request, name: str):
    caller = await _gate(request, scope=SCOPE_ADMIN, action="admin_revoke")
    identity = await ServiceIdentity.objects.filter(name=name).first()
    if identity is None:
        await _audit(
            request,
            identity=caller.identity_name,
            action="admin_revoke",
            outcome="not_found",
            key=name,
        )
        raise HTTPException(404, "Identity not found")
    await ServiceIdentity.objects.filter(id=identity.id).update(is_active=False)
    _grant_cache.invalidate(identity.id)
    await _audit(
        request,
        identity=caller.identity_name,
        action="admin_revoke",
        outcome="ok",
        key=name,
    )
    return Response.json({"revoked": name})


@app.post("/v1/admin/grants")
async def upsert_grant(request):
    caller = await _gate(request, scope=SCOPE_ADMIN, action="admin_grant")
    # A malformed or non-object JSON body is audited (outcome invalid) before the
    # 400 propagates — request.json() raises 400 on bad JSON and _json_object adds
    # the object-shape check, so a scalar/array body never AttributeErrors into an
    # unaudited 500.
    try:
        body = await _json_object(request)
    except HTTPException as exc:
        if exc.status_code == 400:
            await _audit_invalid(request, caller, action="admin_grant")
        raise
    identity_name = body.get("identity", "")
    namespace_name = body.get("namespace", "")
    identity = await ServiceIdentity.objects.filter(name=identity_name).first()
    ns = await Namespace.objects.filter(name=namespace_name).first()
    if identity is None or ns is None:
        await _audit(
            request,
            identity=caller.identity_name,
            namespace=namespace_name if isinstance(namespace_name, str) else "",
            key=identity_name if isinstance(identity_name, str) else "",
            action="admin_grant",
            outcome="not_found",
        )
        raise HTTPException(404, "Identity or namespace not found")
    can_read = bool(body.get("read", True))
    can_write = bool(body.get("write", False))

    existing = await NamespaceGrant.objects.filter(
        identity_id=identity.id, namespace_id=ns.id
    ).first()
    if existing is not None:
        await NamespaceGrant.objects.filter(id=existing.id).update(
            can_read=can_read, can_write=can_write, granted_by=caller.identity_name
        )
    else:
        # Two concurrent first-grants race past the exists-check above and both
        # insert; the unique(identity_id, namespace_id) constraint catches the
        # loser — surface a clean 409 (retry lands on the update branch) instead
        # of a raw 500.
        try:
            await NamespaceGrant(
                identity_id=identity.id,
                namespace_id=ns.id,
                can_read=can_read,
                can_write=can_write,
                granted_by=caller.identity_name,
            ).save()
        except IntegrityError as exc:
            # This insert can trip the unique(identity_id, namespace_id) pair OR
            # an FK — a concurrent revoke/delete of the identity or namespace
            # between the loads above and here. Narrow to the unique race for the
            # 409; a vanished FK target is a real error and re-raises.
            if not is_unique_violation(exc):
                raise
            await _audit(
                request,
                identity=caller.identity_name,
                namespace=namespace_name,
                key=identity_name,
                action="admin_grant",
                outcome="conflict",
            )
            raise HTTPException(409, "Concurrent grant create — retry") from None
    _grant_cache.invalidate(identity.id)
    await _audit(
        request,
        identity=caller.identity_name,
        namespace=namespace_name,
        key=identity_name,
        action="admin_grant",
        outcome="ok",
    )
    return Response.json(
        {
            "identity": identity_name,
            "namespace": namespace_name,
            "read": can_read,
            "write": can_write,
        }
    )


@app.get("/v1/admin/grants")
async def review_grants(request):
    """Access review: which identities can touch a namespace (and vice versa)."""
    caller = await _gate(request, scope=SCOPE_ADMIN, action="admin_review")
    qs = NamespaceGrant.objects
    namespace_name = request.query("namespace")
    if namespace_name:
        ns = await Namespace.objects.filter(name=namespace_name).first()
        if ns is None:
            await _audit(
                request,
                identity=caller.identity_name,
                namespace=namespace_name,
                action="admin_review",
                outcome="not_found",
            )
            raise HTTPException(404, "Namespace not found")
        qs = qs.filter(namespace_id=ns.id)
    grants = await qs.all()

    identities = (
        {
            i.id: i.name
            for i in await ServiceIdentity.objects.filter(
                id__in=[g.identity_id for g in grants]
            ).all()
        }
        if grants
        else {}
    )
    namespaces = (
        {
            n.id: n.name
            for n in await Namespace.objects.filter(
                id__in=[g.namespace_id for g in grants]
            ).all()
        }
        if grants
        else {}
    )

    await _audit(
        request, identity=caller.identity_name, action="admin_review", outcome="ok"
    )
    return Response.json(
        {
            "grants": [
                {
                    "identity": identities.get(g.identity_id, "?"),
                    "namespace": namespaces.get(g.namespace_id, "?"),
                    "read": g.can_read,
                    "write": g.can_write,
                    "granted_by": g.granted_by,
                }
                for g in grants
            ]
        }
    )


# ---------------------------------------------------------------------------
# Lifecycle: retention sweep on the thread-based scheduler + audit drain.
# The scheduler runs in its own thread with its own workers, so it is
# independent of whichever event loops the native server uses for requests.
# ---------------------------------------------------------------------------


async def _purge_if_still_expired(secret, namespace: str, cutoff: datetime) -> bool:
    """Hard-purge one soft-deleted secret inside its own transaction, but only
    if it STILL matches ``deleted_at < cutoff`` under a row lock. Returns True
    iff it was purged.

    A concurrent ``put_secret`` can revive the secret (``deleted_at=None`` + a
    new version) between the sweep's snapshot and this transaction. The re-select
    FOR UPDATE on the cutoff predicate skips a revived row, so the sweep can
    never destroy a live secret + all its history or publish a bogus ``purged``
    event for a secret that is back in service."""
    db = get_db()
    async with db.transaction():
        live = (
            await Secret.objects.filter(id=secret.id, deleted_at__lt=cutoff)
            .select_for_update()
            .first()
        )
        if live is None:
            return False
        await SecretVersion.objects.filter(secret_id=live.id).delete()
        await Secret.objects.filter(id=live.id).delete()
        if namespace:
            await _enqueue_outbox(
                namespace, live.key, "purged", {"version": live.current_version}
            )
    return True


@app.task
async def retention_sweep(cutoff: datetime | None = None) -> None:
    """Purge soft-deleted secrets past the retention window.

    A hard purge is an access too: it publishes a ``purged`` change event (so
    subscribers learn the secret is truly gone) and writes a system audit row,
    matching the fidelity of the admin-triggered purge path. ``cutoff`` defaults
    to the configured window; it is a parameter so a test can drive the sweep
    with a controlled boundary."""
    if cutoff is None:
        cutoff = datetime.now(UTC) - timedelta(days=_config.retention_days)
    expired = await Secret.objects.filter(deleted_at__lt=cutoff).all()
    if not expired:
        return
    ns_names = {ns.id: ns.name for ns in await Namespace.objects.all()}
    for secret in expired:
        namespace = ns_names.get(secret.namespace_id, "")
        # The re-select under lock skips a secret revived since the snapshot; the
        # metric/audit/log fire only when a purge actually happened.
        if not await _purge_if_still_expired(secret, namespace, cutoff):
            continue
        REQUESTS.inc_tuple(("purge", "ok"))
        if namespace:
            NAMESPACE_ACCESS.inc_tuple((namespace, "ok"))
        await _audit_writer.record(
            identity="system",
            namespace=namespace,
            key=secret.key,
            version=secret.current_version,
            action="purge",
            outcome="ok",
            client_ip="",
            auth_method="system",
        )
        logger.info(
            "Retention purge: secret id={sid} after {d}d soft-delete",
            sid=secret.id,
            d=_config.retention_days,
        )


@app.task
async def audit_retention_sweep() -> None:
    """Trim audit rows past the audit-retention window so the append-only
    access log stays bounded (self-managing, like the secret-retention sweep).
    Queries the indexed ``created_at`` bound and deletes in one statement."""
    cutoff = datetime.now(UTC) - timedelta(days=_config.audit_retention_days)
    deleted = await AccessLog.objects.filter(created_at__lt=cutoff).delete()
    if deleted:
        logger.info(
            "Audit retention: trimmed {n} rows older than {d}d",
            n=deleted,
            d=_config.audit_retention_days,
        )


async def _notify_if_still_due(secret, namespace: str, now: datetime) -> bool:
    """Emit the ``expired`` event for one due secret inside a transaction, but
    only if it STILL matches the snapshot predicate (un-notified AND past-due)
    under a conditional update. Returns True iff the event fired.

    A concurrent ``put_secret`` between the sweep's snapshot and here writes a
    new version, clears ``expiry_notified``, and re-arms ``rotation_due`` to a
    FUTURE date. The conditional update then matches nothing, so the sweep
    neither suppresses that new deadline (leaving the row armed for the future)
    nor publishes a stale-version ``expired`` event."""
    db = get_db()
    async with db.transaction():
        armed = await Secret.objects.filter(
            id=secret.id, expiry_notified=False, rotation_due__lte=now
        ).update(expiry_notified=True)
        if armed and namespace:
            await _enqueue_outbox(
                namespace,
                secret.key,
                "expired",
                {
                    "version": secret.current_version,
                    "rotation_due": secret.rotation_due.isoformat()
                    if secret.rotation_due
                    else "",
                },
            )
    return bool(armed)


@app.task
async def rotation_due_sweep() -> None:
    """Publish an ``expired`` change event once per secret whose declared
    ``rotation_due`` has passed — subscribers treat it as a prompt to rotate.
    The secret keeps serving (expiry is advisory). Queries the indexed
    rotation_due/expiry_notified columns, so it touches only the due,
    not-yet-notified rows rather than scanning every secret."""
    now = datetime.now(UTC)
    due_secrets = await Secret.objects.filter(
        deleted_at=None, expiry_notified=False, rotation_due__lte=now
    ).all()
    if not due_secrets:
        return
    ns_names = {ns.id: ns.name for ns in await Namespace.objects.all()}
    for secret in due_secrets:
        namespace = ns_names.get(secret.namespace_id, "")
        if await _notify_if_still_due(secret, namespace, now):
            logger.info(
                "Rotation due: {ns}/{key} (due {d})",
                ns=namespace,
                key=secret.key,
                d=secret.rotation_due,
            )


@app.task
async def outbox_drain() -> None:
    """Drain the transactional outbox in ledger order, posting each pending
    change event to the hub. The outcome of each post decides the row's fate:

    - DELIVERED  — delete the row (at-least-once delivery complete).
    - RETRYABLE  — the hub is down or erroring; stop this pass and retry the
      whole backlog next run, preserving order.
    - PERMANENT  — the hub rejected it definitively (4xx); park the row with the
      reason and CONTINUE. A poison event must never head-of-line-block the
      feed, so it is held aside for an operator instead of retried forever.

    Each post carries the row id as the hub dedupe key, so a crash between the
    POST and the delete cannot double-append. Nothing to do when the hub is
    unconfigured."""
    if not _notifier.enabled:
        return
    pending = (
        await OutboxEvent.objects.filter(status=OUTBOX_PENDING)
        .order_by("id")
        .limit(500)
        .all()
    )
    for ev in pending:
        outcome = _notifier.post(
            ev.subject, ev.kind, ev.metadata, dedupe_key=str(ev.id)
        )
        if outcome.status == DELIVERED:
            await OutboxEvent.objects.filter(id=ev.id).delete()
        elif outcome.status == RETRYABLE:
            # Hub unreachable/erroring — stop this pass; retry the backlog next
            # run, in order, so nothing jumps ahead of an undelivered row.
            break
        else:  # PERMANENT
            await OutboxEvent.objects.filter(id=ev.id).update(
                status=OUTBOX_PARKED,
                attempts=ev.attempts + 1,
                error_detail=outcome.detail[:500],
            )
            OUTBOX_PARKED_TOTAL.inc()
            logger.error(
                "Outbox row {id} parked ({sub} {kind}): {err}",
                id=ev.id,
                sub=ev.subject,
                kind=ev.kind,
                err=outcome.detail,
            )


_task_queue = TaskQueue()
_scheduler = TaskScheduler(task_queue=_task_queue)
# skip_if_running on every sweep/drain: a slow pass (a down hub blocks the
# drain ~15s; a large sweep runs long) must never stack backlogged instances
# that starve the other jobs sharing the worker pool.
_scheduler.add(
    retention_sweep, interval=_config.retention_sweep_interval, skip_if_running=True
)
_scheduler.add(
    rotation_due_sweep, interval=_config.rotation_sweep_interval, skip_if_running=True
)
_scheduler.add(
    outbox_drain, interval=_config.outbox_drain_interval, skip_if_running=True
)
_scheduler.add(
    audit_retention_sweep, interval=_config.audit_sweep_interval, skip_if_running=True
)

# One-liner mTLS adoption: install() registers the startup build (with the app's
# ACTUAL bound plaintext port as upstream, so a moved port can never desync the
# terminator), a ``mtls_terminator`` readiness check, and the shutdown stop —
# staying disabled when listen_port/cert_file are unset. ``_mtls.terminator`` is
# the running terminator after startup (None otherwise).
_mtls = MTLSTerminator.install(
    app,
    listen_port=_config.mtls_listen_port,
    cert_file=_config.mtls_cert_file,
    key_file=_config.mtls_key_file,
    ca_file=_config.mtls_ca_file,
    listen_host="127.0.0.1" if _DEBUG else "0.0.0.0",
    max_connections=_config.mtls_max_connections,
    idle_timeout=_config.mtls_idle_timeout,
)


@app.on_startup
async def _startup():
    _task_queue.start()
    _scheduler.start()


@app.on_shutdown
async def _shutdown():
    _scheduler.stop()
    _task_queue.stop()


# ---------------------------------------------------------------------------
# Admin panel + health + docs
# ---------------------------------------------------------------------------

admin = HyperAdmin(
    app,
    prefix="/admin",
    title="HyperSecret Admin",
    # require_setting so the admin panel's signing secret is explicitly
    # configured (≥32 chars) rather than an auto-generated per-process default
    # that would invalidate every admin session on restart. Set
    # HYPER_ADMIN_SECRET.
    secret_key=require_setting("ADMIN_SECRET", min_length=32),
)
admin.register(
    Namespace,
    list_display=["id", "name", "kek_id", "owner", "created_at"],
    search_fields=["name", "owner"],
)
admin.register(
    Secret,
    list_display=["id", "namespace_id", "key", "current_version", "deleted_at"],
    search_fields=["key"],
)
admin.register(
    SecretVersion,
    # Envelope columns are Field(exclude=True) — never shown here.
    list_display=["id", "secret_id", "version", "kek_id", "created_by", "created_at"],
    ordering="-created_at",
)
admin.register(
    ServiceIdentity,
    list_display=["id", "name", "scopes", "is_active", "key_prefix", "created_at"],
    search_fields=["name"],
)
admin.register(
    NamespaceGrant,
    list_display=[
        "id",
        "identity_id",
        "namespace_id",
        "can_read",
        "can_write",
        "granted_by",
    ],
)
admin.register(
    AccessLog,
    list_display=[
        "id",
        "created_at",
        "identity",
        "namespace",
        "key",
        "version",
        "action",
        "outcome",
    ],
    search_fields=["identity", "namespace", "key"],
    ordering="-id",
)
admin.register(
    OutboxEvent,
    # status/attempts/error_detail are the operator recovery surface: a parked
    # row shows here with its rejection reason; requeue it by editing status
    # back to "pending" (the next drain retries it).
    list_display=[
        "id",
        "created_at",
        "subject",
        "kind",
        "status",
        "attempts",
        "error_detail",
    ],
    search_fields=["subject", "status"],
    ordering="id",
)

# Readiness checks — the DB is checked by mount_health; the mTLS front door is
# registered by MTLSTerminator.install() above. Add the worker machinery so
# /ready fails when a worker has died (rather than reporting healthy while no
# events can be published).
app.add_health_check("scheduler", _scheduler.is_running)

app.mount_health()
mount_docs(app)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8960
    app.run(host="127.0.0.1", port=port)
