"""
HyperManager — Infrastructure Change-Notification Hub.

Producers publish metadata-only change records under hierarchical subjects;
subscribers learn a covered subject changed and re-fetch the changed state from
the producing system themselves (change records carry a nudge, never a payload).

The hub runs one of three delivery tiers, selected by two config fields
(see config.py). The DEFAULT is a live in-memory pub/sub: a publish assigns an
in-memory sequence and pushes the event straight to connected subscribers, with
a bounded ring for brief-reconnect catch-up. ledger_mode=True instead opts into
the durable audited log — an append-only event ledger (its id is the feed cursor)
that subscribers pull in order, with the live WebSocket carrying only wake hints.
See ARCHITECTURE.md.

Run:
    uv run hyper setup --app services.hypermanager.app:app --seed services.hypermanager.seed:run
    uv run hyper start --app services.hypermanager.app:app --port 8970

API:
    POST /v1/events                  → publish change record (publish grant)
    WS   /ws/feed?prefixes=a,b       → live feed (subscribe grant); the hello
                                       frame declares the tier's delivery model
    GET  /v1/events?after=N&prefix=  → durable cursor replay (ledger mode only)
    GET  /v1/cursor                  → latest cursor id     (ledger mode only)
    POST /v1/admin/identities|grants → provisioning (admin scope)
    GET  /health, /ready, /metrics, /admin/, /api/docs
"""

import asyncio
import contextlib
import contextvars
import sys
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.channels import (
    InMemoryChannelLayer,
    PgChannelLayer,
    get_channel_layer,
    set_channel_layer,
)
from hyperdjango.conf import DEFAULTS, get_setting, require_setting
from hyperdjango.database import get_db
from hyperdjango.db import is_unique_violation
from hyperdjango.expressions import Q
from hyperdjango.identity import require_scope
from hyperdjango.logging import logger
from hyperdjango.mtls import MTLSTerminator
from hyperdjango.native import fast_json_dumps
from hyperdjango.openapi import mount_docs
from hyperdjango.standalone_middleware import (
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.tasks import TaskQueue, TaskScheduler
from hyperdjango.telemetry import configure_from_settings, mount_gated_metrics
from hyperdjango.telemetry.metrics import CounterVec, Gauge

from .audit import AuditWriter
from .authz import CallerCache, resolve_identity
from .catchup import RESYNC, CatchupBuffer
from .config import load_hypermanager_config
from .models import (
    KIND_RE,
    SCOPE_ADMIN,
    AccessLog,
    ChangeEvent,
    ManagerIdentity,
    TopicGrant,
    prefix_covered,
    subject_matches,
    valid_identity_name,
    valid_prefix,
    valid_scopes,
    valid_subject,
)

DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)
DATABASE_URL = get_setting("DATABASE_URL")

_DEBUG = bool(get_setting("DEBUG"))
_config = load_hypermanager_config()

if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0

app = HyperApp(
    title="HyperManager",
    database=DATABASE_URL,
    debug=_DEBUG,
    # require_setting (not get_setting): refuse to boot on the per-process
    # SECRET_KEY default — an empty/auto key silently weakens session, CSRF, and
    # signing. Same fail-closed posture as models.py's SESSION_SIGNING_KEY.
    secret_key=require_setting("SECRET_KEY", min_length=32),
    site_config=_config,
)

_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:

    async def _metrics_resolve(request):
        """Authenticate a Prometheus scrape and publish the auth context.

        The metric bodies expose the hub's traffic SHAPE — per-(action, outcome)
        request volume, caller-cache hit/miss counts, and the live subscriber
        gauge — but never a subject: the only labels are (action, outcome),
        (result), and the unlabeled subscriber gauge, so no subject-domain name
        is emitted. The traffic shape is still privileged, so the scrape requires
        a resolved identity — any valid bearer token or mTLS client cert, no
        grant needed — matching HyperSecret's posture. An unauthenticated scrape
        is denied (fail closed); ``/health`` and ``/ready`` stay open. The
        resolved method/fingerprint is published for the audit trail just like
        every gated route.
        """
        _auth_ctx.set(("", ""))
        resolved = await resolve_identity(request)
        _auth_ctx.set((resolved.method, resolved.fingerprint))

    async def _metrics_denied(request, exc):
        # A denied scrape is audited like every other rejected access, and the
        # per-(action, outcome) counter records it. A successful scrape writes no
        # ok row — unlike every other endpoint's one-ok-row-per-action —
        # deliberately: high-frequency polling would otherwise flood the trail.
        EVENTS.inc_tuple(("metrics", "denied"))
        await _audit(request, identity="", action="metrics", outcome="denied")

    mount_gated_metrics(
        app,
        _telemetry.prometheus_sink.handler,
        resolve=_metrics_resolve,
        on_deny=_metrics_denied,
    )


app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=not _DEBUG))

EVENTS = CounterVec(
    "hypermanager_requests_total",
    "HyperManager API requests by action and outcome.",
    ("action", "outcome"),
)
FEED_SUBSCRIBERS = Gauge(
    "hypermanager_feed_subscribers",
    "Live-feed WebSocket subscribers currently connected.",
)


def _wake_channel(subject_or_prefix: str) -> str:
    """Shard the wake fan-out by the first subject segment. A publish notifies
    only the subscribers watching that domain instead of every subscriber, so
    per-publish work scales with same-domain listeners, not the whole hub."""
    first = subject_or_prefix.lstrip("/").split("/", 1)[0]
    return f"hm:wake:{first}"


# Coarse capability on the identity: "feed" gates the change-notification API
# (publish, replay, cursor, live feed); "admin" gates provisioning. Fine-grained
# publish/subscribe authorization is by prefix grant on top of the scope.
SCOPE_FEED = "feed"

# Delivery tier, chosen once at boot from the two config selectors (see
# HyperManagerConfig). ledger_mode picks the durable audited log; otherwise the
# ring size picks catch-up (bounded reconnect replay) vs pure ephemeral. The
# in-memory buffer owns the seq + ring for the two non-ledger tiers and is unused
# (and reset to empty every restart) in ledger mode.
_LEDGER_MODE = _config.ledger_mode
if _LEDGER_MODE:
    _FEED_MODE = "ledger"
    _catchup: CatchupBuffer | None = None
else:
    _FEED_MODE = "catchup" if _config.catch_up_ring_size > 0 else "ephemeral"
    _catchup = CatchupBuffer(ring_size=_config.catch_up_ring_size)

_caller_cache = CallerCache(_config.caller_cache_ttl)
_audit_writer = AuditWriter(
    flush_interval=_config.audit_flush_interval,
    flush_batch=_config.audit_flush_batch,
)
# Self-register the periodic flush + shutdown drain on the app lifecycle.
_audit_writer.install(app)

# Per-request auth method + fingerprint (set by _gate, read by _audit).
_auth_ctx: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "hypermanager_auth", default=("", "")
)


async def _audit(source, *, identity, action, outcome, subject=""):
    method, fingerprint = _auth_ctx.get()
    await _audit_writer.record(
        identity=identity,
        action=action,
        outcome=outcome,
        subject=subject,
        client_ip=source.client_ip or "",
        auth_method=method,
        fingerprint=fingerprint,
    )


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    return Response.json({"detail": "Internal server error"}, status=500)


async def _gate(source, *, action: str, scope: str | None = None):
    """Authenticate + scope-authorize or raise, auditing its OWN denials
    (bad/absent credential, missing scope). It does NOT write a success row:
    passing the gate is not the action's outcome — each handler writes exactly
    one outcome row (ok after the work commits, denied when it raises) so the
    trail reflects what actually happened, not merely that auth succeeded."""
    identity_name = ""
    _auth_ctx.set(("", ""))
    try:
        resolved = await resolve_identity(source)
        _auth_ctx.set((resolved.method, resolved.fingerprint))
        caller = await _caller_cache.get(resolved.identity)
        identity_name = caller.name
        if scope is not None:
            require_scope(caller.scopes, scope)
    except HTTPException:
        EVENTS.inc_tuple((action, "denied"))
        await _audit(source, identity=identity_name, action=action, outcome="denied")
        raise
    return caller


def _event_dict(ev: ChangeEvent) -> dict:
    return {
        "id": ev.id,
        "producer": ev.producer,
        "subject": ev.subject,
        "kind": ev.kind,
        "metadata": ev.metadata,
        "at": ev.created_at.isoformat(),
    }


# Publishes are serialized end-to-end by ONE transaction-scoped advisory lock,
# (HM_LOCK_CLASS, HM_GATE_KEY), taken as the first statement of every publish
# transaction. Postgres auto-releases a transaction-scoped advisory lock at
# COMMIT or ROLLBACK, so the gate can never leak onto a pooled connection — there
# is no finally, and nothing depends on the pool's session reset. Serializing the
# whole publish transaction means that while any publish's row is uncommitted no
# other publish can be mid-flight (they block on the gate), so a row's SERIAL id
# commits in sequence order and a committed id can never have an uncommitted
# LOWER sibling. That makes max(committed id) an exact, gapless replay ceiling
# (see _safe_replay_ceiling) with no pg_locks scan. HM_LOCK_CLASS is a fixed
# advisory classid (ASCII "HMEV") giving this gate its own key space in the
# cluster-wide advisory-lock namespace; HM_GATE_KEY is the single key within it.
HM_LOCK_CLASS = 0x484D4556  # "HMEV"
HM_GATE_KEY = 0

# First statement of every publish transaction: block until this publish holds
# the serialize gate. Transaction-scoped, so it releases automatically at
# COMMIT/ROLLBACK — no explicit unlock, no leak path.
_PUBLISH_GATE_SQL = "SELECT pg_advisory_xact_lock($1::int, $2::int)"


# ---------------------------------------------------------------------------
# Publish + replay
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.json({"service": "hypermanager", "api": "/v1", "feed": "/ws/feed"})


@app.post("/v1/events")
async def publish_event(request):
    """Publish a change record and push it to live subscribers.

    Authorization, validation, idempotency, and audit hold identically across
    every tier; only the persistence + fan-out differ (``_LEDGER_MODE``):

    - Default (catch-up / ephemeral): assign an in-memory monotonic seq, append
      it to the bounded ring, and broadcast the event IN an ``event`` frame to
      connected subscribers whose prefixes cover the subject. No Postgres.
    - Ledger: append the durable ``ChangeEvent`` row under the serialize gate and
      broadcast a content-free wake hint; subscribers pull the event via replay.

    Idempotent when the body carries a ``dedupe_key``: a re-POST of the same
    logical publish (transport retry, drainer restart) returns the existing
    event's id instead of appending a second record. In ledger mode the
    per-producer unique ``(producer, dedupe_key)`` makes this hold even under a
    concurrent double-POST; in the in-memory tiers it is best-effort against a
    bounded recent-key set (see CatchupBuffer) — a different producer reusing the
    same key always lands its own event either way.
    """
    caller = await _gate(request, action="publish", scope=SCOPE_FEED)
    body = await request.json()
    subject = ""
    try:
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON body must be an object")
        subject = body.get("subject", "")
        kind = body.get("kind", "")
        metadata = body.get("metadata", {})
        dedupe_key = body.get("dedupe_key")
        if not isinstance(subject, str) or not valid_subject(subject):
            raise HTTPException(400, "Invalid subject")
        if not isinstance(kind, str) or not KIND_RE.match(kind):
            raise HTTPException(400, "Invalid kind")
        if not isinstance(metadata, dict):
            raise HTTPException(400, "metadata must be an object")
        metadata_json = fast_json_dumps(metadata)
        if len(metadata_json) > _config.metadata_max_bytes:
            raise HTTPException(400, "metadata too large (metadata-only feed)")
        if dedupe_key is not None and (
            not isinstance(dedupe_key, str) or not (0 < len(dedupe_key) <= 200)
        ):
            raise HTTPException(400, "Invalid dedupe_key")
        if not caller.may_publish(subject):
            raise HTTPException(403, f"No publish grant covering {subject!r}")
        if _LEDGER_MODE:
            record_id, created, frame = await _publish_ledger(
                caller.name, subject, kind, metadata, dedupe_key
            )
        else:
            record_id, created, frame = _publish_inmem(
                caller.name, subject, kind, metadata, dedupe_key
            )
    except HTTPException:
        EVENTS.inc_tuple(("publish", "denied"))
        await _audit(
            request,
            identity=caller.name,
            action="publish",
            outcome="denied",
            subject=subject if isinstance(subject, str) else "",
        )
        raise
    except Exception:
        # A post-gate non-HTTPException failure (e.g. the ledger INSERT rejecting
        # a payload the size/grammar checks let through, such as a metadata
        # string carrying a NUL that JSONB cannot store) would otherwise 500 with
        # no audit row. Record its true "error" outcome before the generic
        # handler maps it to a 500.
        EVENTS.inc_tuple(("publish", "error"))
        await _audit(
            request,
            identity=caller.name,
            action="publish",
            outcome="error",
            subject=subject if isinstance(subject, str) else "",
        )
        raise
    if created:
        # Fan out to same-domain subscribers only (sharded by first subject
        # segment). An idempotent replay creates nothing and so fans out nothing.
        #
        # Best-effort: the record is already persisted (a committed ledger row or
        # a committed ring entry), so a fan-out failure must NOT turn a successful
        # publish into a 500 — subscribers still converge (ledger via replay,
        # in-memory via reconnect catch-up). Log and proceed to the ok outcome.
        try:
            await get_channel_layer().channel(_wake_channel(subject)).publish(frame)
        except Exception as exc:  # noqa: BLE001 - fan-out is a delivery hint only
            logger.warning(
                "feed fan-out failed for {s} (record {i}, persisted): {e}",
                s=subject,
                i=record_id,
                e=str(exc),
            )
    EVENTS.inc_tuple(("publish", "ok"))
    await _audit(
        request, identity=caller.name, action="publish", outcome="ok", subject=subject
    )
    return Response.json({"id": record_id}, status=201 if created else 200)


async def _publish_ledger(
    producer: str, subject: str, kind: str, metadata: dict, dedupe_key: str | None
) -> tuple[int, bool, dict]:
    """Durable ledger append under the serialize gate. Returns
    ``(event_id, created, wake_frame)``; the frame is a content-free wake hint
    (subject + committed id) — subscribers pull the event itself via replay."""
    db = get_db()
    if dedupe_key is not None:
        # Idempotent publish, scoped to THIS producer. The per-producer unique
        # (producer, dedupe_key) makes get_or_create return the existing ledger
        # row (created=False) for a replayed or concurrently-raced re-POST by the
        # same producer, and append exactly one row otherwise. ``producer`` is
        # part of the lookup (not just the defaults) so a different producer
        # reusing the same key never resolves to a foreign row. The transaction
        # takes the serialize gate FIRST, so its SERIAL id commits in sequence
        # order and the max(committed id) ceiling stays gapless.
        async with db.transaction():
            await db.query_val(_PUBLISH_GATE_SQL, HM_LOCK_CLASS, HM_GATE_KEY)
            event, created = await ChangeEvent.objects.get_or_create(
                producer=producer,
                dedupe_key=dedupe_key,
                defaults={"subject": subject, "kind": kind, "metadata": metadata},
            )
        return event.id, created, {"subject": subject, "id": event.id}
    # Non-dedupe publish: take the serialize gate, then append one row — both in
    # one transaction, so the gate is held until COMMIT and the SERIAL id commits
    # in sequence order.
    async with db.transaction():
        await db.query_val(_PUBLISH_GATE_SQL, HM_LOCK_CLASS, HM_GATE_KEY)
        event = ChangeEvent(
            producer=producer, subject=subject, kind=kind, metadata=metadata
        )
        await event.save()
    return event.id, True, {"subject": subject, "id": event.id}


def _publish_inmem(
    producer: str, subject: str, kind: str, metadata: dict, dedupe_key: str | None
) -> tuple[int, bool, dict]:
    """In-memory seq assignment + ring append (default tiers). Returns
    ``(seq, created, event_frame)``; the frame carries the event itself so a
    connected subscriber is delivered it directly, no replay pull."""
    seq, created = _catchup.append(
        subject, kind, metadata, producer=producer, dedupe_key=dedupe_key
    )
    frame = {
        "type": "event",
        "subject": subject,
        "kind": kind,
        "seq": seq,
        "metadata": metadata,
    }
    return seq, created, frame


def _prefix_q(prefixes) -> Q | None:
    """OR of subject-prefix predicates in SQL (subject column is indexed).

    Mirrors ``subject_matches`` EXACTLY, trailing slash included: a prefix
    ending in '/' covers the subtree ONLY (never the exact node), while one
    without covers the exact subject and its subtree. Emitting ``Q(subject=root)``
    unconditionally (after rstrip) leaked the exact node "a/b" to a subtree-only
    subscription "a/b/" on replay, contradicting the live-wake filter. Returns
    None when there are no prefixes (caller treats that as match-nothing, fail
    closed)."""
    q: Q | None = None
    for p in prefixes:
        if p.endswith("/"):
            # Subtree only: excludes the exact node (grant "a/b/" ⇏ "a/b").
            clause = Q(subject__startswith=p)
        else:
            clause = Q(subject=p) | Q(subject__startswith=p + "/")
        q = clause if q is None else (q | clause)
    return q


# The replay ceiling is simply max(committed id). Publishes are serialized by the
# transaction-scoped gate (see HM_LOCK_CLASS / publish_event), so while any
# publish's row is uncommitted no other publish transaction can be open — they
# block on the gate. A committed id therefore can never have an uncommitted LOWER
# sibling still about to appear: SERIAL id order equals commit order. The plain
# MVCC-visible max is then an exact, gapless horizon — an uncommitted row is
# invisible to this SELECT and so excluded automatically, and a rolled-back
# (burned) id leaves a permanent gap the ceiling correctly advances past. No
# pg_locks scan, no id/xid-order assumption.
_CEILING_SQL = "SELECT COALESCE(max(id), 0) FROM hm_events"


async def _safe_replay_ceiling() -> int:
    """Highest ledger id that is safe to replay right now: ``max(committed id)``.

    The SERIAL id is assigned at INSERT but a row only becomes visible at COMMIT.
    A transaction-scoped advisory gate serializes the whole publish transaction
    (see ``HM_LOCK_CLASS`` and ``publish_event``), so no two publishes ever
    interleave: while one publish's row is uncommitted no other publish can be
    mid-flight. Commit order therefore equals id order, so the largest
    MVCC-visible id can have no lower, still-in-flight sibling — replay stays
    gapless and at-least-once delivery holds under concurrent publishers. An
    uncommitted row is invisible to this query and so excluded automatically; a
    rolled-back id leaves a gap the ceiling simply steps past."""
    val = await get_db().query_val(_CEILING_SQL)
    return int(val or 0)


async def _replay(prefixes, after: int, limit: int) -> tuple[list[dict], int, int]:
    """One page of events after ``after`` whose subject matches ``prefixes``,
    filtered in SQL (the subject column is indexed).

    Returns ``(events, cursor, raw_count)``. Because the prefix filter is
    pushed into the query, every returned row is visible, so ``raw_count``
    (== len(events)) is the correct pagination signal: page again iff
    ``raw_count == limit``. Filtering in Python after LIMIT (the old approach)
    let a diluted page end replay early and silently drop entitled events —
    the durable-ledger guarantee depended on fixing this. Replay is also capped
    at the gapless ceiling so a concurrently-committing lower id is never
    jumped. ``prefixes`` must already be authorized by the caller's grants."""
    q = _prefix_q(prefixes)
    if q is None:
        return [], after, 0
    ceiling = await _safe_replay_ceiling()
    rows = (
        await ChangeEvent.objects.filter(q, id__gt=after, id__lte=ceiling)
        .order_by("id")
        .limit(limit)
        .all()
    )
    out = [_event_dict(ev) for ev in rows]
    cursor = rows[-1].id if rows else after
    return out, cursor, len(rows)


def _authorized_prefixes(caller, requested: str) -> tuple[str, ...]:
    """Resolve the effective subscribe prefixes for a replay request: the
    caller's grants, optionally narrowed to a requested prefix (only if a
    grant covers it — fail closed)."""
    if requested:
        if any(prefix_covered(g, requested) for g in caller.subscribe_prefixes):
            return (requested,)
        return ()
    return caller.subscribe_prefixes


# The ledger id (the feed cursor) is a 32-bit SERIAL, so a cursor param binds
# into an INT4 column: values above INT4_MAX have no valid id and overflow the
# bind (a 500 with no audit). A first digit-LENGTH cap also stops an enormous
# digit string from tripping Python's int()-from-str digit cap (a ValueError,
# also a 500) before we can even range-check it. Both become a clean 400.
_MAX_PARAM_DIGITS = 18
_INT4_MAX = 2147483647


def _parse_bounded_int(
    raw: str, *, field: str, default: int, minimum: int = 0, maximum: int = _INT4_MAX
) -> int:
    """Parse a non-negative integer query param, failing closed with a 400 rather
    than a 500. An absent/empty or non-numeric value yields ``default`` (lenient,
    as before); a value with too many digits, below ``minimum``, or above
    ``maximum`` is a 400. The digit-length guard runs before ``int()`` so a
    pathological digit string can never trip the interpreter's own str→int cap.
    ``isdecimal`` (not ``isdigit``) gates the parse: superscript/other Unicode
    digit characters count as digits but are rejected by ``int()``, and must
    fall to the lenient default rather than raise mid-parse."""
    if not raw or not raw.isdecimal():
        return default
    if len(raw) > _MAX_PARAM_DIGITS:
        raise HTTPException(400, f"Invalid {field} (too large)")
    value = int(raw)
    if value < minimum or value > maximum:
        raise HTTPException(400, f"Invalid {field} (out of range)")
    return value


async def replay_events(request):
    """Cursor replay: everything after ``after`` the caller may subscribe to.

    Ledger-tier only (registered iff ``_LEDGER_MODE``): durable ordered replay is
    the durable log's contract. In the default tiers events are delivered in the
    feed frame, so this route does not exist and a pull 404s."""
    caller = await _gate(request, action="replay", scope=SCOPE_FEED)
    prefix = request.query("prefix", "")
    try:
        # Parse the numeric params INSIDE the try so an over-long/out-of-range
        # value is a denied-audited 400, never an audit-invisible 500.
        after = _parse_bounded_int(request.query("after", ""), field="after", default=0)
        if prefix and not valid_prefix(prefix):
            raise HTTPException(400, "Invalid prefix")
        # Resolve the effective prefixes through the one coverage authority.
        # Parity with the WS feed (which 4003-closes an uncovered prefix): a
        # well-formed prefix the caller holds no subscribe grant on is a 403, not
        # an empty 200 — still fail closed (no rows leak), audited as denied.
        prefixes = _authorized_prefixes(caller, prefix)
        if prefix and not prefixes:
            raise HTTPException(403, f"No subscribe grant covering {prefix!r}")
        limit = _parse_bounded_int(
            request.query("limit", ""), field="limit", default=_config.replay_limit
        )
        limit = min(limit if limit > 0 else _config.replay_limit, _config.replay_limit)
        # On-demand floor refresh: when the request's cursor sits at/below this
        # replica's cached floor (the region where a reset is plausible), re-read
        # the persisted floor from the shared marker first. With pg_fanout another
        # replica may have trimmed the shared ledger and raised the floor past
        # ours; refreshing here honors that trim within this request instead of
        # serving a gap-containing page as reset=False until our next sweep tick.
        # Skip it entirely on a never-trimmed ledger (cached floor 0): no reset is
        # possible below a zero floor, so the common after=0 full-catch-up avoids
        # a per-request marker SELECT.
        if _retention_floor > 0 and after <= _retention_floor:
            await _refresh_retention_floor()
        # If the caller is below the retention floor it missed trimmed events;
        # flag reset so it full-resyncs, and replay from the floor.
        reset = after < _retention_floor
        if reset:
            after = _retention_floor
        events, latest, _raw = await _replay(prefixes, after, limit)
    except HTTPException:
        EVENTS.inc_tuple(("replay", "denied"))
        await _audit(
            request,
            identity=caller.name,
            action="replay",
            outcome="denied",
            subject=prefix,
        )
        raise
    except Exception:
        # A post-gate non-HTTPException failure (e.g. a DB error) would otherwise
        # 500 with no audit row at all — record its true "error" outcome so the
        # trail reflects what happened, then let the generic handler map it to 500.
        EVENTS.inc_tuple(("replay", "error"))
        await _audit(
            request,
            identity=caller.name,
            action="replay",
            outcome="error",
            subject=prefix,
        )
        raise
    EVENTS.inc_tuple(("replay", "ok"))
    await _audit(
        request, identity=caller.name, action="replay", outcome="ok", subject=prefix
    )
    return Response.json({"events": events, "cursor": latest, "reset": reset})


async def latest_cursor(request):
    caller = await _gate(request, action="cursor", scope=SCOPE_FEED)
    # Hand out the gapless ceiling, not raw max(id): a watcher starting from this
    # cursor must never sit above an in-flight (assigned but not-yet-committed)
    # lower id, or that event would be skipped forever (cursors never move back).
    ceiling = await _safe_replay_ceiling()
    await _audit(request, identity=caller.name, action="cursor", outcome="ok")
    return Response.json({"cursor": ceiling})


# The durable replay/cursor endpoints exist only in the ledger tier. The default
# tiers deliver the event in the feed frame and keep no queryable durable log, so
# a pull to either path 404s — the mode-aware client tolerates that (it uses the
# hello-advertised tier, not these endpoints).
if _LEDGER_MODE:
    app.get("/v1/events")(replay_events)
    app.get("/v1/cursor")(latest_cursor)


# ---------------------------------------------------------------------------
# Live feed
# ---------------------------------------------------------------------------


# Bounded live-delivery queue for the in-frame tiers: a subscriber that cannot
# keep up fills it, at which point the hub stops delivering past the gap and
# closes so the subscriber reconnects and catches up from the ring (or resyncs).
# This is the in-frame equivalent of the ledger feed's back-pressure — it never
# delivers an event past a hole it dropped.
_FEED_QUEUE_MAX = 2048


@app.websocket("/ws/feed")
async def feed(ws):
    """Live change feed. The delivery model is the hub's active tier, announced
    to the subscriber in the ``hello`` frame:

    - ledger — ``hello{mode:ledger,cursor}`` then content-free
      ``{"type":"wake","cursor":N}`` hints; the subscriber pulls the durable
      replay endpoint. Delivery and cursor advancement happen exclusively through
      replay, so a dropped or coalesced wake can never reorder or lose an event.
    - catchup / ephemeral — ``hello{mode,seq,resync}`` then the event itself in
      each ``{"type":"event",...}`` frame. Catchup first replays the exact events
      the reconnecting subscriber missed (from the in-memory ring), then streams
      live ones; ephemeral resyncs on every connect and streams live only.

    Auth, scope, and per-prefix grant authorization are identical across tiers;
    only the post-hello delivery differs.
    """
    await ws.accept()
    identity_name = ""
    auth_method = ""
    fingerprint = ""
    try:
        resolved = await resolve_identity(ws)
        auth_method, fingerprint = resolved.method, resolved.fingerprint
        caller = await _caller_cache.get(resolved.identity)
        identity_name = caller.name
        require_scope(caller.scopes, SCOPE_FEED)
    except HTTPException as exc:
        # The WS denial records the same identity/auth attribution the ok row
        # does (the resolved identity + method/fingerprint when auth got that
        # far). The native WebSocket exposes no socket peer address, so client_ip
        # is unavailable on feed rows either way — unlike HTTP rows, where _audit
        # records it from the request's peer.
        EVENTS.inc_tuple(("feed_connect", "denied"))
        await _audit_writer.record(
            identity=identity_name,
            action="feed_connect",
            outcome="denied",
            auth_method=auth_method,
            fingerprint=fingerprint,
        )
        await ws.send_json({"type": "error", "message": exc.detail})
        await ws.close(code=4001 if exc.status_code == 401 else 4003)
        return

    # No ok row yet: authN + scope passing is not the outcome of the connect.
    # Each denial path below writes its own `denied` row, and the ok row is
    # written only once the subscription is actually established (one true
    # outcome row per connect, mirroring the _gate contract).
    async def _feed_denied(subject: str = "") -> None:
        EVENTS.inc_tuple(("feed_connect", "denied"))
        await _audit_writer.record(
            identity=caller.name,
            action="feed_connect",
            outcome="denied",
            subject=subject,
            auth_method=resolved.method,
            fingerprint=resolved.fingerprint,
        )

    # Prefixes come from the query string (the mode-aware client also echoes them
    # in its subscribe frame; the two are identical). Authorizing here — before
    # any frame is read — keeps the denial path uniform across every tier.
    requested = [
        p
        for chunk in parse_qs(ws.query_string).get("prefixes", [])
        for p in chunk.split(",")
        if p
    ]
    for p in requested:
        if not valid_prefix(p) or not any(
            prefix_covered(g, p) for g in caller.subscribe_prefixes
        ):
            await _feed_denied(subject=p)
            await ws.send_json(
                {"type": "error", "message": f"No subscribe grant covering {p!r}"}
            )
            await ws.close(code=4003)
            return
    prefixes = tuple(requested) or caller.subscribe_prefixes
    if not prefixes:
        await _feed_denied()
        await ws.send_json({"type": "error", "message": "No subscribe grants"})
        await ws.close(code=4003)
        return

    async def _feed_ok() -> None:
        EVENTS.inc_tuple(("feed_connect", "ok"))
        await _audit_writer.record(
            identity=caller.name,
            action="feed_connect",
            outcome="ok",
            auth_method=resolved.method,
            fingerprint=resolved.fingerprint,
        )

    if _LEDGER_MODE:
        await _feed_ledger(ws, prefixes, _feed_ok)
    else:
        await _feed_inmem(ws, prefixes, _feed_ok)


async def _feed_ledger(ws, prefixes, feed_ok) -> None:
    """Ledger tier: hello(cursor) → content-free wake hints. Delivery is the
    subscriber's replay pull; a wake is only a latency optimization, so repeated
    wakes between sends coalesce into one pending flag (nothing to overflow)."""
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    # Highest ledger id seen for a covered subject since the last wake was sent
    # (coalesced hint; the subscriber replays from its own cursor regardless).
    hint = {"cursor": 0}

    def _mark(event_id: int) -> None:
        if event_id > hint["cursor"]:
            hint["cursor"] = event_id
        wake.set()

    def on_wake(msg) -> None:
        # Publisher-thread callback: filter to this connection's exact prefixes
        # (the shard only narrows by first segment), then hop loops.
        data = msg.data
        if any(subject_matches(p, data["subject"]) for p in prefixes):
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_mark, data.get("id", 0))

    # Subscribe to the wake shards BEFORE computing/sending hello: an event
    # landing between the ceiling snapshot and the subscription would otherwise
    # get no wake frame and wait out the poll floor. A wake that arrives before
    # hello simply arms the flag; push_wakes (started after hello) delivers it.
    layer = get_channel_layer()
    shard_names = {_wake_channel(p) for p in prefixes}
    subscriptions = [
        (ch, ch.subscribe(on_wake))
        for ch in (layer.channel(name) for name in shard_names)
    ]

    writer: asyncio.Task | None = None
    gauge_held = False
    try:
        # The hello cursor is the gapless ceiling, not raw max(id): a subscriber
        # that adopts it must never sit above an in-flight lower id.
        ceiling = await _safe_replay_ceiling()
        if ceiling > hint["cursor"]:
            hint["cursor"] = ceiling
        await ws.send_json({"type": "hello", "mode": "ledger", "cursor": ceiling})

        FEED_SUBSCRIBERS.inc()
        gauge_held = True
        await feed_ok()

        async def push_wakes():
            while True:
                await wake.wait()
                wake.clear()
                await ws.send_json({"type": "wake", "cursor": hint["cursor"]})

        writer = asyncio.create_task(push_wakes())
        # The subscriber sends nothing in steady state (replay is an HTTP concern
        # here); iterate to keep the connection alive and answer an optional
        # keepalive ping, ending when the peer closes.
        async for msg in ws.iter_json():
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    finally:
        _teardown_feed(writer, subscriptions, gauge_held)
        if writer is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer


async def _feed_inmem(ws, prefixes, feed_ok) -> None:
    """Default tiers: deliver each event IN the frame. Catchup replays the exact
    missed events (from the ring) before going live; ephemeral resyncs and goes
    straight live. The subscribe frame carries the reconnect resume key."""
    # Read the subscribe frame for the resume key (last_seq) and the incarnation
    # token (epoch) it was last delivered under. The mode-aware client sends them
    # immediately after connect; a peer that sends nothing is reaped by the
    # receive idle deadline. A malformed/absent frame → no resume point (fresh
    # subscriber → resync), which is always safe. A stale epoch (from a previous
    # hub incarnation) makes the resume unrecoverable no matter what last_seq
    # holds, so a burst that climbed back into the stale seq range cannot be
    # misreplayed as this client's catch-up.
    last_seq: int | None = None
    client_epoch: str | None = None
    with contextlib.suppress(Exception):
        sub = await ws.receive_json()
        if isinstance(sub, dict):
            if isinstance(sub.get("last_seq"), int):
                last_seq = sub["last_seq"]
            if isinstance(sub.get("epoch"), str):
                client_epoch = sub["epoch"]

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_FEED_QUEUE_MAX)
    overflow = {"hit": False}

    def on_event(msg) -> None:
        # Publisher-thread callback: filter to this connection's exact prefixes,
        # then hop loops and enqueue the event for the delivery task. A full queue
        # (slow consumer) drops and flags for resync rather than delivering past
        # the hole.
        data = msg.data
        if any(subject_matches(p, data["subject"]) for p in prefixes):

            def _enqueue(d=data):
                if queue.full():
                    overflow["hit"] = True
                else:
                    queue.put_nowait(d)

            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_enqueue)

    # Subscribe BEFORE snapshotting the ring so an event landing in the
    # subscribe→snapshot window is not lost: it is covered by the replay snapshot
    # (seq <= boundary) AND arrives on the live queue, and the boundary check
    # below delivers it exactly once.
    layer = get_channel_layer()
    shard_names = {_wake_channel(p) for p in prefixes}
    subscriptions = [
        (ch, ch.subscribe(on_event))
        for ch in (layer.channel(name) for name in shard_names)
    ]

    writer: asyncio.Task | None = None
    gauge_held = False
    try:
        # The replay/live boundary: events at seq <= boundary are covered by the
        # catchup replay (or the ephemeral resync); live delivery is seq >
        # boundary. Snapshot AFTER subscribing so the subscribe→snapshot window is
        # closed, and take the replay list + boundary atomically so no event falls
        # between them.
        if _FEED_MODE == "catchup":
            missed, boundary = _catchup.snapshot(last_seq, prefixes, epoch=client_epoch)
            resync = missed is RESYNC
        else:  # ephemeral: no ring, always resync on connect
            missed = RESYNC
            resync = True
            boundary = _catchup.current_seq()
        # The epoch lets a reconnecting client detect a hub restart: a new
        # incarnation mints a fresh epoch, so a client resuming under the old one
        # is told to resync even if its stale last_seq now sits inside the new
        # seq range.
        await ws.send_json(
            {
                "type": "hello",
                "mode": _FEED_MODE,
                "seq": boundary,
                "epoch": _catchup.epoch,
                "resync": resync,
            }
        )

        FEED_SUBSCRIBERS.inc()
        gauge_held = True
        await feed_ok()

        if not resync:
            # Catchup replay: exactly the missed events (seq > last_seq matching
            # the subscriber's prefixes), in order, as event frames.
            for ev in missed:
                await ws.send_json(
                    {
                        "type": "event",
                        "subject": ev.subject,
                        "kind": ev.kind,
                        "seq": ev.seq,
                        "metadata": ev.metadata,
                    }
                )

        async def push_events():
            while True:
                data = await queue.get()
                if overflow["hit"]:
                    # Fell behind the live queue: close (1013 "try again later")
                    # so the subscriber reconnects and catches up from the ring or
                    # resyncs — never a silent gap.
                    await ws.close(code=1013)
                    return
                if data["seq"] > boundary:
                    await ws.send_json(
                        {
                            "type": "event",
                            "subject": data["subject"],
                            "kind": data["kind"],
                            "seq": data["seq"],
                            "metadata": data["metadata"],
                        }
                    )

        writer = asyncio.create_task(push_events())
        async for msg in ws.iter_json():
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    finally:
        _teardown_feed(writer, subscriptions, gauge_held)
        if writer is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer


def _teardown_feed(writer, subscriptions, gauge_held) -> None:
    """Release the channel subscription and the subscriber gauge on EVERY feed
    exit path. Cancelling/unsubscribing/decrementing FIRST guarantees they run
    even when the delivery task finished with a dead-peer WebSocketDisconnect;
    the caller then awaits the cancelled task with the exception suppressed."""
    if writer is not None:
        writer.cancel()
    for ch, sub_id in subscriptions:
        ch.unsubscribe(sub_id)
    if gauge_held:
        FEED_SUBSCRIBERS.dec()


# ---------------------------------------------------------------------------
# Admin provisioning (admin scope)
# ---------------------------------------------------------------------------


@app.post("/v1/admin/identities")
async def create_identity(request):
    caller = await _gate(request, action="admin", scope=SCOPE_ADMIN)
    body = await request.json()
    name = ""
    target = ""
    try:
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON body must be an object")
        name = body.get("name", "")
        target = name if isinstance(name, str) else ""
        # Grammar-check the name, not just its length: it flows into mTLS CN
        # matching, audit rows, and the admin UI, so control chars / unicode /
        # whitespace are rejected outright (same posture as subjects/kinds).
        if not isinstance(name, str) or not valid_identity_name(name):
            raise HTTPException(400, "Invalid identity name")
        scopes = body.get("scopes", "") or SCOPE_FEED
        if not isinstance(scopes, str) or not valid_scopes(scopes):
            raise HTTPException(400, "Invalid scopes")
        reactivate = bool(body.get("reactivate", False))
        existing = await ManagerIdentity.objects.filter(name=name).first()
        if existing is not None:
            # A revoked name is not silently re-mintable — that would resurrect a
            # retired credential by surprise. Reactivation is an explicit, audited
            # opt-in: `reactivate=true` on a REVOKED name flips it active again,
            # honoring the identity's ORIGINAL token/cert once more (is_active is
            # the single revocation switch both auth legs check), so rotate the
            # credential separately if the revocation was for compromise. Any other
            # case — an active name, or a revoked name without the flag — is a 409.
            if reactivate and not existing.is_active:
                await ManagerIdentity.objects.filter(id=existing.id).update(
                    is_active=True
                )
                _caller_cache.invalidate(existing.id)
                logger.info("Identity {n} reactivated by {a}", n=name, a=caller.name)
                await _audit(
                    request,
                    identity=caller.name,
                    action="admin",
                    outcome="ok",
                    subject=name,
                )
                return Response.json({"name": name, "reactivated": True})
            raise HTTPException(409, "Identity already exists")
        try:
            result = await ManagerIdentity.generate(name=name, scopes=scopes)
        except Exception as exc:  # noqa: BLE001 - re-raised unless a unique clash
            # TOCTOU: a concurrent create won the unique-name race between the
            # first() check and generate(). Both dispatch paths' unique-violation
            # shapes — the typed IntegrityError and the native driver's raw
            # RuntimeError carrying the PostgreSQL message — are matched by the
            # framework's is_unique_violation authority, so report the honest 409
            # instead of a 500 without re-hosting the pg-message contract here.
            # Anything else re-raises unchanged.
            if is_unique_violation(exc):
                raise HTTPException(409, "Identity already exists") from None
            raise
    except HTTPException:
        await _audit(
            request,
            identity=caller.name,
            action="admin",
            outcome="denied",
            subject=target,
        )
        raise
    logger.info("Identity {n} minted by {a}", n=name, a=caller.name)
    await _audit(
        request, identity=caller.name, action="admin", outcome="ok", subject=name
    )
    return Response.json({"name": name, "token": result.raw_key}, status=201)


@app.delete("/v1/admin/identities/{name}")
async def revoke_identity(request, name: str):
    caller = await _gate(request, action="admin", scope=SCOPE_ADMIN)
    identity = await ManagerIdentity.objects.filter(name=name).first()
    if identity is None:
        await _audit(
            request,
            identity=caller.name,
            action="admin",
            outcome="denied",
            subject=name,
        )
        raise HTTPException(404, "Identity not found")
    await ManagerIdentity.objects.filter(id=identity.id).update(is_active=False)
    _caller_cache.invalidate(identity.id)
    await _audit(
        request, identity=caller.name, action="admin", outcome="ok", subject=name
    )
    return Response.json({"revoked": name})


@app.post("/v1/admin/grants")
async def upsert_grant(request):
    caller = await _gate(request, action="admin", scope=SCOPE_ADMIN)
    body = await request.json()
    # Grant target descriptor for the audit trail: which identity, which prefix.
    grant_desc = ""
    identity = None
    try:
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON body must be an object")
        target_name = body.get("identity", "")
        prefix = body.get("prefix", "")
        grant_desc = f"{target_name}:{prefix}"
        identity = await ManagerIdentity.objects.filter(name=target_name).first()
        if identity is None:
            raise HTTPException(404, "Identity not found")
        if not isinstance(prefix, str) or not valid_prefix(prefix):
            raise HTTPException(400, "Invalid prefix")
        can_publish = bool(body.get("publish", False))
        can_subscribe = bool(body.get("subscribe", True))
        # Idempotent upsert on the (identity_id, prefix) unique key, done atomically
        # in the DB with ON CONFLICT DO UPDATE. A hand-rolled first()-then-insert had
        # a TOCTOU window where a concurrent double-POST raced the insert into a
        # unique violation → 500; this single statement can never violate the
        # constraint, so it is race-free by construction.
        await get_db().execute(
            "INSERT INTO hm_grants "
            "(identity_id, prefix, can_publish, can_subscribe, granted_by, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, now(), now()) "
            "ON CONFLICT (identity_id, prefix) DO UPDATE SET "
            "  can_publish = EXCLUDED.can_publish, "
            "  can_subscribe = EXCLUDED.can_subscribe, "
            "  granted_by = EXCLUDED.granted_by, "
            "  updated_at = now()",
            identity.id,
            prefix,
            can_publish,
            can_subscribe,
            caller.name,
        )
    except HTTPException:
        await _audit(
            request,
            identity=caller.name,
            action="admin",
            outcome="denied",
            subject=grant_desc,
        )
        raise
    _caller_cache.invalidate(identity.id)
    await _audit(
        request, identity=caller.name, action="admin", outcome="ok", subject=grant_desc
    )
    return Response.json(
        {
            "identity": identity.name,
            "prefix": prefix,
            "publish": can_publish,
            "subscribe": can_subscribe,
        }
    )


@app.get("/v1/admin/grants")
async def review_grants(request):
    caller = await _gate(request, action="admin", scope=SCOPE_ADMIN)
    grants = await TopicGrant.objects.all()
    identities = (
        {
            i.id: i.name
            for i in await ManagerIdentity.objects.filter(
                id__in=[g.identity_id for g in grants]
            ).all()
        }
        if grants
        else {}
    )
    await _audit(request, identity=caller.name, action="admin", outcome="ok")
    return Response.json(
        {
            "grants": [
                {
                    "identity": identities.get(g.identity_id, "?"),
                    "prefix": g.prefix,
                    "publish": g.can_publish,
                    "subscribe": g.can_subscribe,
                    "granted_by": g.granted_by,
                }
                for g in grants
            ]
        }
    )


@app.get("/v1/audit")
async def query_audit(request):
    """Query the access trail (admin scope). Who published/subscribed/
    administered what, and how they authenticated."""
    caller = await _gate(request, action="audit_query", scope=SCOPE_ADMIN)
    await _audit_writer.flush_pending()  # read-your-writes
    qs = AccessLog.objects
    for field in ("identity", "action", "outcome"):
        value = request.query(field)
        if value:
            qs = qs.filter(**{field: value})
    limit = min(
        _parse_bounded_int(request.query("limit", ""), field="limit", default=100),
        500,
    )
    rows = await qs.order_by("-id").limit(limit).all()
    await _audit(request, identity=caller.name, action="audit_query", outcome="ok")
    return Response.json(
        {
            "entries": [
                {
                    "at": r.created_at.isoformat(),
                    "identity": r.identity,
                    "action": r.action,
                    "outcome": r.outcome,
                    "subject": r.subject,
                    "client_ip": r.client_ip,
                    "auth_method": r.auth_method,
                    "fingerprint": r.fingerprint,
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# Lifecycle: channel layer, mTLS terminator, ledger retention
# ---------------------------------------------------------------------------


# Highest ledger id that has been trimmed from the ledger: a subscriber
# replaying from below this floor missed events the ledger can no longer supply,
# so it is told to full-resync (see the reset signal in replay) instead of
# silently getting a short, gap-containing replay. This is a per-replica cache of
# the authoritative floor persisted in ``hm_retention_floor`` (see RetentionFloor):
# the sweep writes the trim boundary there, and every replica refreshes its cache
# from that single shared marker.
_retention_floor = 0
# Guards the monotonic compare-and-set of the ``_retention_floor`` cache. The
# cache read-modify-write (``max``) is not atomic under free-threading: two
# concurrent refreshes straddling a persist could each read the old value and
# the higher one lose to the lower, dropping the cache below the DB floor — a
# below-floor replay in that window would then compute reset=False and serve a
# gap page. The DB stays authoritative (GREATEST on persist); this lock only
# keeps the in-process cache from moving backward.
_retention_floor_lock = threading.Lock()

# One retention DELETE never trims the whole aged backlog in a single long write
# transaction: a long DELETE holds a write lock and pins the xmin horizon (the
# self-inflicted case of the replay-ceiling bug), freezing the ceiling/floor for
# every subscriber while it runs. Trim in short, bounded chunks that each commit
# and release instead.
_RETENTION_DELETE_BATCH = 5000


async def _refresh_retention_floor() -> None:
    """Load the authoritative floor from the shared ``hm_retention_floor`` marker
    into this replica's cache (monotonic — the cache never moves backward).

    The floor lives in the DB, not in ``min(surviving id)``: a fresh, NEVER-
    trimmed ledger has no marker row, so its floor is 0 even when the first id
    was burned by a rolled-back insert (no spurious full-resync for after=0
    subscribers). Reading the shared marker is also what lets one replica pick up
    a DIFFERENT replica's trim under pg_fanout without running its own sweep."""
    global _retention_floor
    val = await get_db().query_val("SELECT floor FROM hm_retention_floor WHERE id = 1")
    if val is not None:
        floor = int(val)
        # Compare-and-set under the lock so a concurrent refresh can't lose this
        # update and pull the cache below the persisted floor (monotonic cache).
        with _retention_floor_lock:
            if floor > _retention_floor:
                _retention_floor = floor


async def _persist_retention_floor(candidate: int) -> None:
    """Advance the shared floor marker to ``candidate`` (monotonic in the DB via
    GREATEST) and refresh this replica's cache. The upsert is atomic, so
    concurrent replicas racing the same boundary converge without a lost update."""
    await get_db().execute(
        "INSERT INTO hm_retention_floor (id, floor) VALUES (1, $1) "
        "ON CONFLICT (id) DO UPDATE "
        "SET floor = GREATEST(hm_retention_floor.floor, EXCLUDED.floor)",
        candidate,
    )
    await _refresh_retention_floor()


@app.task
async def retention_sweep() -> None:
    """Trim ledger rows past the retention window and advance the replay floor.

    Trim by ID BOUNDARY, not by created_at: delete every row at or below the
    highest id whose row aged past the window, then derive the floor from THAT
    boundary. A contiguous id-prefix delete keeps the surviving set gap-free at
    the low end, so the floor is exact — a created_at/id inversion can no longer
    delete a higher id while keeping a lower one (an unreported gap served as
    reset=False), and the floor is never `min(surviving id) - 1` (which turned a
    burned first id into a spurious reset)."""
    cutoff = datetime.now(UTC) - timedelta(days=_config.retention_days)
    db = get_db()
    boundary = await db.query_val(
        "SELECT max(id) FROM hm_events WHERE created_at < $1", cutoff
    )
    if boundary is None:
        # Nothing aged past the window: no trim, floor unchanged. (Still cheap to
        # re-read the shared marker so a peer's trim propagates promptly.)
        await _refresh_retention_floor()
        # Crash recovery: a prior sweep may have drained the ledger to empty and
        # died before anchoring the high-water floor. The persist is monotonic
        # (GREATEST), so re-anchoring here is idempotent and only ever raises
        # the floor to where that sweep would have put it.
        if await ChangeEvent.objects.count() == 0:
            high_water = await db.query_val(
                "SELECT COALESCE("
                "  pg_sequence_last_value(pg_get_serial_sequence('hm_events', 'id')),"
                "  0)"
            )
            if high_water:
                await _persist_retention_floor(int(high_water))
        return
    boundary = int(boundary)
    # Persist the floor BEFORE deleting: a floor at the boundary while boundary
    # rows still survive only over-triggers reset=True (a harmless extra
    # resync), whereas deleting first would leave a crash window where trimmed
    # events sit above a stale floor and a replay serves an unreported gap as
    # reset=False.
    await _persist_retention_floor(boundary)
    deleted = 0
    while True:
        n = await db.execute(
            "DELETE FROM hm_events WHERE id IN "
            "(SELECT id FROM hm_events WHERE id <= $1 ORDER BY id LIMIT $2)",
            boundary,
            _RETENTION_DELETE_BATCH,
        )
        deleted += n
        if n < _RETENTION_DELETE_BATCH:
            break
    candidate = boundary
    # Preserve the emptied-ledger high-water behavior: if the sweep drained the
    # ledger to empty, a top id burned by a rolled-back insert sits above the
    # boundary; anchor the floor at the id sequence's high-water mark so a
    # below-floor cursor still resets instead of reading an empty page as
    # "caught up".
    if await ChangeEvent.objects.count() == 0:
        high_water = await db.query_val(
            "SELECT COALESCE("
            "  pg_sequence_last_value(pg_get_serial_sequence('hm_events', 'id')),"
            "  0)"
        )
        candidate = max(candidate, int(high_water or 0))
    if candidate > boundary:
        await _persist_retention_floor(candidate)
    if deleted:
        logger.info(
            "Ledger retention: trimmed {n} events; replay floor now {f}",
            n=deleted,
            f=_retention_floor,
        )


_task_queue = TaskQueue()
_scheduler = TaskScheduler(task_queue=_task_queue)
# The retention sweep trims the durable ledger, so it only runs in ledger mode —
# the default tiers keep no durable log to trim (the ring self-bounds).
if _LEDGER_MODE:
    _scheduler.add(retention_sweep, interval=_config.retention_sweep_interval)

_pg_layer: PgChannelLayer | None = None

# One call wires the in-process mTLS terminator into the app lifecycle: it
# registers the on_startup that builds the terminator against the app's ACTUAL
# bound plaintext port (so the port can never desync), the on_shutdown that stops
# it, and the readiness check that fails on a dead front door. mTLS stays disabled
# (the handle's ``terminator`` is None) whenever listen_port or cert_file is unset.
_installed_mtls = MTLSTerminator.install(
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
    global _pg_layer
    if _config.pg_fanout:
        _pg_layer = PgChannelLayer(database_url=DATABASE_URL)
        await _pg_layer.connect()
        set_channel_layer(_pg_layer)
    else:
        set_channel_layer(InMemoryChannelLayer())

    # The retention floor + sweep are ledger-only. The default tiers keep no
    # durable log, so there is no floor to load and no sweep to schedule.
    if _LEDGER_MODE:
        await _refresh_retention_floor()
        _task_queue.start()
        _scheduler.start()


@app.on_shutdown
async def _shutdown():
    if _LEDGER_MODE:
        _scheduler.stop()
        _task_queue.stop()
    if _pg_layer is not None:
        await _pg_layer.disconnect()


# ---------------------------------------------------------------------------
# Admin panel + health + docs
# ---------------------------------------------------------------------------

admin = HyperAdmin(
    app,
    prefix="/admin",
    title="HyperManager Admin",
    # require_setting (not get_setting): the admin panel must never sign its
    # session cookies with the auto-generated per-process ADMIN_SECRET default.
    secret_key=require_setting("ADMIN_SECRET", min_length=32),
)
admin.register(
    ChangeEvent,
    list_display=["id", "created_at", "producer", "subject", "kind"],
    search_fields=["subject", "producer"],
    ordering="-id",
)
admin.register(
    ManagerIdentity,
    list_display=["id", "name", "scopes", "is_active", "key_prefix", "created_at"],
    search_fields=["name"],
)
admin.register(
    TopicGrant,
    list_display=[
        "id",
        "identity_id",
        "prefix",
        "can_publish",
        "can_subscribe",
        "granted_by",
    ],
)
admin.register(
    AccessLog,
    list_display=[
        "id",
        "created_at",
        "identity",
        "action",
        "outcome",
        "subject",
        "auth_method",
    ],
    search_fields=["identity", "action", "subject"],
    ordering="-id",
)

# Readiness: the mTLS front-door probe is registered by MTLSTerminator.install
# above (name "mtls_terminator"). Here we surface a stopped scheduler (ledger mode
# only — the default tiers run no scheduler), plus (when the cross-replica layer
# is enabled) a coarse "channel layer is wired up" probe. is_connected() inspects
# only the publish handle — it does NOT detect a dropped LISTEN/NOTIFY link — so
# this catches a torn-down/unconnected layer, not a silently-stalled wake path.
if _LEDGER_MODE:
    app.add_health_check("scheduler", _scheduler.is_running)
app.add_health_check(
    "channel_layer", lambda: _pg_layer is None or _pg_layer.is_connected()
)

app.mount_health()
mount_docs(app)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8970
    app.run(host="127.0.0.1", port=port)
