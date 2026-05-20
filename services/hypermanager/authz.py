"""
HyperManager authorization authority.

One gate with two authentication legs — signed bearer token or
network-verified mTLS client certificate (CN = identity name) — and
prefix-grant authorization on top. Fails closed at every decision.
Works for both HTTP requests and WebSocket upgrades: each exposes a
lowercase ``headers`` mapping, which is all the gate reads.
"""

from dataclasses import dataclass

from hyperdjango.identity import ResolvedIdentity, parse_scopes
from hyperdjango.identity import resolve_identity as _resolve_identity
from hyperdjango.telemetry.metrics import CounterVec
from hyperdjango.ttlcache import TTLCache

from .models import ManagerIdentity, TopicGrant, subject_matches

CALLER_CACHE = CounterVec(
    "hypermanager_caller_cache_total",
    "Caller-cache lookups by result (hit/miss).",
    ("result",),
)


@dataclass(slots=True, frozen=True)
class Caller:
    """Resolved identity + grant snapshot for one request."""

    identity_id: int
    name: str
    scopes: frozenset[str]
    publish_prefixes: tuple[str, ...]
    subscribe_prefixes: tuple[str, ...]

    def may_publish(self, subject: str) -> bool:
        return any(subject_matches(p, subject) for p in self.publish_prefixes)

    def may_subscribe(self, subject: str) -> bool:
        return any(subject_matches(p, subject) for p in self.subscribe_prefixes)


class CallerCache:
    """TTL cache of Caller snapshots keyed by identity id (on ``TTLCache``)."""

    def __init__(self, ttl: float):
        self._cache: TTLCache[int, Caller] = TTLCache(
            ttl,
            counter=CALLER_CACHE,
            hit_values=("hit",),
            miss_values=("miss",),
        )

    def invalidate(self, identity_id: int | None = None) -> None:
        self._cache.invalidate(identity_id)

    async def get(self, identity: ManagerIdentity) -> Caller:
        return await self._cache.get(
            identity.id, lambda: self._build_snapshot(identity)
        )

    async def _build_snapshot(self, identity: ManagerIdentity) -> Caller:
        grants = await TopicGrant.objects.filter(identity_id=identity.id).all()
        return Caller(
            identity_id=identity.id,
            name=identity.name,
            scopes=parse_scopes(identity.scopes),
            publish_prefixes=tuple(g.prefix for g in grants if g.can_publish),
            subscribe_prefixes=tuple(g.prefix for g in grants if g.can_subscribe),
        )


async def resolve_identity(source) -> ResolvedIdentity:
    """Authenticate a request or WebSocket via the framework identity authority
    (bearer token, then mTLS cert with per-identity fingerprint pinning).
    Returns the identity plus how it authenticated (for the audit trail).

    The certificate leg's terminator attestation is resolved from the process
    registry (an in-process terminator self-registers on start) or the
    configured ``MTLS_PROXY_SECRET`` — nothing is hand-threaded here."""
    return await _resolve_identity(
        source,
        ManagerIdentity,
        fingerprint_field="cert_fingerprint",
    )
