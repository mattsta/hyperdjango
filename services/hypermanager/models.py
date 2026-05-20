"""
HyperManager models.

3 tables: ChangeEvent (the append-only ledger whose id is the feed cursor),
ManagerIdentity (signed-token / mTLS identities), TopicGrant (prefix
allow-list). Events are metadata-only records — producers never place
secret material in them.
"""

from hyperdjango.conf import require_setting
from hyperdjango.fields import JSONField, create_field
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey

# The subject grammar + prefix-matching authority lives in a dependency-free
# module (no signing key, no ORM) so the client can import it for delivery-side
# filtering without server-only config. Re-exported here for the existing
# ``from .models import subject_matches, valid_subject, ...`` call sites.
from .subjects import (  # noqa: F401
    IDENTITY_NAME_RE,
    KIND_RE,
    MAX_SUBJECT_LEN,
    SCOPE_RE,
    SUBJECT_SEGMENT_RE,
    prefix_covered,
    subject_matches,
    valid_identity_name,
    valid_prefix,
    valid_scopes,
    valid_subject,
)

SCOPE_ADMIN = "admin"


class ChangeEvent(TimestampMixin, Model):
    """One infrastructure change record. The auto id is the feed cursor."""

    class Meta:
        table = "hm_events"
        # Dedup is scoped PER PRODUCER: the key is unique within a producer,
        # not globally. Two producers using natural outbox keys (e.g.
        # "outbox-42") must each land their own event — a global unique key let
        # the second producer's get_or_create return the first's row, silently
        # dropping its event (and disclosing foreign-key existence via 200/201).
        unique_together = [("producer", "dedupe_key")]

    id: int = Field(primary_key=True, auto=True)
    producer: str = Field(index=True)  # identity name that published
    subject: str = Field(index=True)
    kind: str = Field()
    metadata: dict = create_field(JSONField(), default=dict)
    # Idempotency key for a logical publish, unique WITHIN a producer (see
    # Meta.unique_together). A publisher that retries a POST (transport failure,
    # drainer restart) reuses the same key; the (producer, dedupe_key) constraint
    # collapses that producer's retries to one ledger row, so a re-POST returns
    # the existing event instead of appending a duplicate — while a different
    # producer reusing the same key is unaffected. NULL = no dedup requested
    # (multiple NULLs are distinct in Postgres, so unpinned publishes never
    # collide).
    dedupe_key: str | None = Field(default=None)


class ManagerIdentity(SignedAPIKeyMixin, TimestampMixin):
    """Producer/subscriber credential: bearer token or mTLS cert CN."""

    class Meta:
        table = "hm_identities"

    class TokenConfig:
        # require_setting (not get_setting): refuse to start on an
        # unconfigured/auto-generated signing key. Set HYPER_SESSION_SIGNING_KEY
        # (stable across seed + server).
        keys = [
            SigningKey(
                secret=require_setting("SESSION_SIGNING_KEY", min_length=32),
                version=1,
            )
        ]
        key_display_prefix = "hmk_"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)  # "producer:hypersecret", "service:platform-api"
    # Optional per-identity certificate pinning: comma-separated SHA-256
    # allow-list. Empty = accept any CA-issued cert with this CN.
    cert_fingerprint: str = Field(default="")


class TopicGrant(TimestampMixin, Model):
    """Allow-list row: identity → subject prefix, publish and/or subscribe."""

    class Meta:
        table = "hm_grants"
        unique_together = [("identity_id", "prefix")]

    id: int = Field(primary_key=True, auto=True)
    identity_id: int = Field(foreign_key=ManagerIdentity, index=True)
    prefix: str = Field()
    can_publish: bool = Field(default=False)
    can_subscribe: bool = Field(default=True)
    granted_by: str = Field(default="")


class RetentionFloor(Model):
    """Single-row marker (``id`` fixed at 1) persisting the highest ledger id the
    retention sweep has trimmed — the authoritative replay floor.

    Persisting the boundary in the shared ledger (rather than re-deriving it from
    ``min(surviving id)``) is what makes the floor correct across two failure
    modes: a fresh, never-trimmed ledger has NO marker row, so its floor is 0
    even when the first id was burned by a rolled-back insert (no spurious
    reset); and every replica in a ``pg_fanout`` deployment reads the SAME floor,
    so one replica's trim is visible to all without waiting for each to run its
    own sweep. The sweep advances it monotonically (``GREATEST`` on upsert);
    replicas cache it in-process and refresh on demand for a below-floor replay.
    """

    class Meta:
        table = "hm_retention_floor"

    id: int = Field(primary_key=True)  # fixed sentinel 1 — a single marker row
    floor: int = Field(default=0)


class AccessLog(TimestampMixin, Model):
    """Append-only audit trail: one row per gated action, including denials.

    "Who published/subscribed/administered what" for an mTLS control plane —
    the equivalent of HyperSecret's AccessLog, adapted to this hub.
    """

    class Meta:
        table = "hm_access_log"

    id: int = Field(primary_key=True, auto=True)
    identity: str = Field(default="", index=True)  # "" = unauthenticated
    action: str = Field(index=True)  # publish/replay/feed_connect/admin/...
    outcome: str = Field(index=True)  # ok/denied
    subject: str = Field(default="", index=True)  # when the action names one
    client_ip: str = Field(default="")
    auth_method: str = Field(default="")  # token/cert/""
    fingerprint: str = Field(default="")  # client-cert SHA-256 (cert method)
