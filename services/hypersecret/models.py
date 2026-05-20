"""
HyperSecret models.

6 tables: Namespace, Secret, SecretVersion, ServiceIdentity, NamespaceGrant,
AccessLog. The server persists only ciphertext + wrapped DEKs (envelope.py);
there is no column anywhere for plaintext or clear key material.
"""

from datetime import datetime

from hyperdjango.conf import require_setting
from hyperdjango.fields import JSONField, create_field
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey

# Coarse scopes carried on identities (comma-separated in the mixin's
# ``scopes`` field). Grants control *which* namespaces; scopes control *what*.
SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
SCOPE_AUDIT = "audit"


class Namespace(TimestampMixin, Model):
    """One trust boundary: ``env/service`` (e.g. ``prod/api``).

    ``kek_id`` names the master-key generation clients must hold to decrypt
    this namespace. The key material itself never exists server-side.
    """

    class Meta:
        table = "hs_namespaces"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)  # "prod/api"
    kek_id: str = Field()  # e.g. "prod-api-v1"
    description: str = Field(default="")
    owner: str = Field(default="")


class Secret(TimestampMixin, Model):
    """A named secret slot inside a namespace. Payloads live in versions."""

    class Meta:
        table = "hs_secrets"
        unique_together = [("namespace_id", "key")]

    id: int = Field(primary_key=True, auto=True)
    namespace_id: int = Field(foreign_key=Namespace, index=True)
    key: str = Field()
    current_version: int = Field(default=0)
    deleted_at: datetime | None = Field(default=None, index=True)
    metadata: dict = create_field(JSONField(), default=dict)
    # Indexed columns derived on write so the periodic sweeps query only the
    # rows they act on instead of scanning every secret. rotation_due is
    # parsed from the client's metadata; expiry_notified is server-managed
    # (set once the `expired` event has fired, re-armed on the next write).
    rotation_due: datetime | None = Field(default=None, index=True)
    expiry_notified: bool = Field(default=False)


class SecretVersion(TimestampMixin, Model):
    """Immutable envelope for one version of one secret.

    ``ciphertext`` is append-only forever; ``encrypted_dek``/``kek_id`` may
    change only through the audited rewrap path (KEK rotation). Both are
    ``exclude=True`` so no serializer or admin view ever emits them by
    accident — API responses that intentionally return the envelope build
    their payload explicitly.
    """

    class Meta:
        table = "hs_secret_versions"
        unique_together = [("secret_id", "version")]

    id: int = Field(primary_key=True, auto=True)
    secret_id: int = Field(foreign_key=Secret, index=True)
    version: int = Field()
    alg: str = Field(default="A256GCM")
    kek_id: str = Field()
    ciphertext: str = Field(exclude=True)  # base64 nonce||GCM(payload)
    encrypted_dek: str = Field(exclude=True)  # base64 nonce||GCM(DEK)
    created_by: str = Field(default="")  # identity name (provenance)
    rewrapped_at: datetime | None = Field(default=None)
    rewrapped_by: str = Field(default="")
    # One-deep undo for the only permitted mutation (rewrap): the prior wrapped
    # DEK + its kek_id, retained atomically each time the DEK is rewrapped. A
    # buggy or compromised rewrap that writes an unusable blob can be rolled back
    # to the immediately-preceding pair instead of bricking the version forever.
    prev_encrypted_dek: str = Field(default="", exclude=True)
    prev_kek_id: str = Field(default="")


class ServiceIdentity(SignedAPIKeyMixin, TimestampMixin):
    """A service or operator credential.

    SignedAPIKeyMixin: HMAC-signed bearer tokens (forgeries rejected without
    a DB hit), SHA-256 hash at rest, shown once at mint time, revocable via
    ``is_active``, expirable via ``expires_at``.
    """

    class Meta:
        table = "hs_identities"

    class TokenConfig:
        # Sign tokens with the framework's TokenEngine signing key. require_
        # setting (not get_setting) so the app REFUSES TO START on an
        # unconfigured/auto-generated key rather than mint forgeable-once-known
        # or ephemeral tokens. Set HYPER_SESSION_SIGNING_KEY (stable across
        # seed + server).
        keys = [
            SigningKey(
                secret=require_setting("SESSION_SIGNING_KEY", min_length=32),
                version=1,
            )
        ]
        key_display_prefix = "hsk_"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)  # "service:prod-api", "operator:alice"
    # Optional per-identity certificate pinning: comma-separated allow-list of
    # SHA-256 fingerprints. Empty = accept any CA-issued cert with this CN.
    # Revoke one leaked cert by removing its fingerprint here.
    cert_fingerprint: str = Field(default="")


class NamespaceGrant(TimestampMixin, Model):
    """Allow-list row: identity → namespace. No wildcards, no policy language."""

    class Meta:
        table = "hs_grants"
        unique_together = [("identity_id", "namespace_id")]

    id: int = Field(primary_key=True, auto=True)
    identity_id: int = Field(foreign_key=ServiceIdentity, index=True)
    namespace_id: int = Field(foreign_key=Namespace, index=True)
    can_read: bool = Field(default=True)
    can_write: bool = Field(default=False)
    granted_by: str = Field(default="")


class AccessLog(TimestampMixin, Model):
    """Append-only audit trail. One row per access, including denials."""

    class Meta:
        table = "hs_access_log"

    id: int = Field(primary_key=True, auto=True)
    identity: str = Field(default="", index=True)  # "" = unauthenticated
    namespace: str = Field(default="", index=True)
    key: str = Field(default="", index=True)
    version: int = Field(default=0)
    # Indexed: the audit query filters on action/outcome (and identity/namespace/
    # key above); created_at is indexed so the retention sweep bounds the log
    # without scanning it.
    action: str = Field(index=True)  # read/write/delete/rewrap/batch_read/...
    outcome: str = Field(index=True)  # ok/denied/not_found/conflict/invalid
    # Redeclared from TimestampMixin only to add the index the retention sweep's
    # created_at bound needs; default/auto-stamp semantics are unchanged.
    created_at: datetime | None = Field(default=None, index=True)
    client_ip: str = Field(default="")
    auth_method: str = Field(default="")  # token/cert/"" (unauthenticated)
    fingerprint: str = Field(default="")  # client-cert SHA-256 (cert method)


# Outbox row lifecycle. A pending row is retried until it lands; a parked row
# hit a permanent (4xx) rejection from the hub and is held out of the drain so
# it can never head-of-line-block the feed until an operator inspects it.
OUTBOX_PENDING = "pending"
OUTBOX_PARKED = "parked"


class OutboxEvent(TimestampMixin, Model):
    """Transactional outbox for change notifications.

    A change event is written to this table in the SAME transaction as the
    secret state change, then a background drainer posts it to the hub and
    deletes it on acknowledgement. This closes the durability hole of a naive
    fire-and-forget publish: if the process dies (or the hub is down) between
    the commit and the post, the row survives and is retried — so a committed
    secret change always reaches the ledger eventually.

    The row id doubles as the hub dedupe key: it is stable across drainer
    restarts, so a crash between the POST and the local delete cannot append a
    second ledger row (the hub collapses the re-POST idempotently).
    """

    class Meta:
        table = "hs_outbox"

    id: int = Field(primary_key=True, auto=True)
    subject: str = Field()  # secrets/<namespace>/<key>
    kind: str = Field()  # created/rotated/rewrapped/deleted/purged/exposed/expired
    metadata: dict = create_field(JSONField(), default=dict)
    # pending → still draining; parked → permanently rejected, held for an
    # operator to inspect and requeue (flip status back to pending).
    status: str = Field(default=OUTBOX_PENDING, index=True)
    attempts: int = Field(default=0)  # post attempts made before parking
    error_detail: str = Field(default="")  # hub's rejection reason when parked
