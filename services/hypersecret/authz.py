"""
HyperSecret authorization authority.

Single decision point for "who is calling and what may they touch". Every
route funnels through here (shared-authority pattern — no per-route drift),
and every gate fails closed:

    resolve_identity()             Bearer token → active ServiceIdentity or 401
    identity.require_scope()       coarse capability on the identity or 403
    check_namespace()              explicit grant row for the namespace or 403

The scope gate is the framework's :func:`hyperdjango.identity.require_scope`
called directly on ``caller.scopes`` — this module keeps only the identity and
namespace-grant authorities that are specific to the app's data model.

Grant lookups are cached in-process with a short TTL so the fetch hot path
does one cache probe instead of a query; revocations propagate within
HYPERSECRET_GRANT_CACHE_TTL seconds (default 15).
"""

from dataclasses import dataclass

from hyperdjango import HTTPException
from hyperdjango.identity import ResolvedIdentity, parse_scopes
from hyperdjango.identity import resolve_identity as _resolve_identity
from hyperdjango.telemetry.metrics import CounterVec
from hyperdjango.ttlcache import TTLCache

from .models import Namespace, NamespaceGrant, ServiceIdentity

GRANT_CACHE = CounterVec(
    "hypersecret_grant_cache_total",
    "Grant-cache lookups by result (hit/miss).",
    ("result",),
)


@dataclass(slots=True, frozen=True)
class CallerGrants:
    """Snapshot of one identity's grants: {namespace_name: (read, write)}."""

    identity_id: int
    identity_name: str
    scopes: frozenset[str]
    namespaces: dict[str, tuple[bool, bool]]
    namespace_ids: dict[str, int]


class GrantCache:
    """TTL cache of CallerGrants keyed by identity id (on ``TTLCache``)."""

    def __init__(self, ttl: float):
        self._cache: TTLCache[int, CallerGrants] = TTLCache(
            ttl,
            counter=GRANT_CACHE,
            hit_values=("hit",),
            miss_values=("miss",),
        )

    def invalidate(self, identity_id: int | None = None) -> None:
        self._cache.invalidate(identity_id)

    async def get(self, identity: ServiceIdentity) -> CallerGrants:
        return await self._cache.get(
            identity.id, lambda: self._build_snapshot(identity)
        )

    async def _build_snapshot(self, identity: ServiceIdentity) -> CallerGrants:
        grants = await NamespaceGrant.objects.filter(identity_id=identity.id).all()
        namespaces: dict[str, tuple[bool, bool]] = {}
        namespace_ids: dict[str, int] = {}
        if grants:
            ns_by_id = {
                ns.id: ns
                for ns in await Namespace.objects.filter(
                    id__in=[g.namespace_id for g in grants]
                ).all()
            }
            for g in grants:
                ns = ns_by_id.get(g.namespace_id)
                if ns is None:
                    continue
                namespaces[ns.name] = (g.can_read, g.can_write)
                namespace_ids[ns.name] = ns.id

        return CallerGrants(
            identity_id=identity.id,
            identity_name=identity.name,
            # The framework identity authority owns the CSV scope grammar — parse
            # through it instead of re-splitting, so the token's "*" wildcard and
            # whitespace handling stay identical everywhere scopes are read.
            scopes=parse_scopes(identity.scopes),
            namespaces=namespaces,
            namespace_ids=namespace_ids,
        )


async def resolve_identity(request) -> ResolvedIdentity:
    """Authenticate via the framework identity authority (token or mTLS cert),
    with per-identity certificate fingerprint pinning. Returns the resolved
    identity plus how it authenticated (for the audit trail).

    The certificate leg's terminator attestation is resolved from the process
    registry (an in-process terminator self-registers on start) or the
    configured ``MTLS_PROXY_SECRET`` — nothing is hand-threaded here."""
    return await _resolve_identity(
        request,
        ServiceIdentity,
        fingerprint_field="cert_fingerprint",
    )


def check_namespace(caller: CallerGrants, namespace: str, *, write: bool) -> None:
    """Explicit grant or 403. Unknown namespace and missing grant are the
    same denial — no existence oracle for ungranted namespaces."""
    grant = caller.namespaces.get(namespace)
    if grant is None:
        raise HTTPException(403, f"No grant for namespace {namespace!r}")
    can_read, can_write = grant
    if write and not can_write:
        raise HTTPException(403, f"No write grant for namespace {namespace!r}")
    if not write and not can_read:
        raise HTTPException(403, f"No read grant for namespace {namespace!r}")
