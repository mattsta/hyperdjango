"""
Service-identity resolution — authenticate a caller by signed bearer token or
mutual-TLS client certificate.

A small, generic authority for services that identify callers with
:class:`~hyperdjango.signing.SignedAPIKeyMixin` tokens and/or client
certificates verified by :class:`~hyperdjango.mtls.MTLSTerminator` (or a
trusted proxy). It is model-agnostic: pass the identity model class and it
resolves either credential to a row, reporting *how* the caller
authenticated so an audit trail can record it.

Two authentication legs, one gate:

- **Bearer token** — ``Authorization: Bearer <token>``, verified by the
  model's ``verify()`` (an HMAC pre-check rejects forgeries before any DB
  hit).
- **Client certificate** — the network-verified CN (attested by the
  in-process terminator or a configured proxy) names an identity row.
  Optionally pinned to a per-identity allow-list of certificate fingerprints.

Both legs end at the same active-identity check, so revoking an identity cuts
off its token and its certificate together. Resolution fails closed: an
unknown token, an unknown/revoked cert identity, or a non-allow-listed
fingerprint all raise :class:`~hyperdjango.exceptions.HTTPException`.

``source`` is anything exposing a lowercased ``headers`` mapping — an HTTP
``Request`` or a WebSocket — so the same call guards both.

Usage::

    from hyperdjango.identity import resolve_identity

    async def gate(request):
        resolved = await resolve_identity(
            request, ServiceIdentity,
            fingerprint_field="cert_fingerprint",   # optional per-id pinning
        )
        audit(actor=resolved.identity.name,
              auth=resolved.method, fingerprint=resolved.fingerprint)
"""

from dataclasses import dataclass

from hyperdjango.exceptions import HTTPException
from hyperdjango.mtls import (
    ClientCertIdentity,
    normalize_fingerprint,
    resolve_client_cert,
)

__all__ = [
    "ResolvedIdentity",
    "has_scope",
    "parse_scopes",
    "require_scope",
    "resolve_identity",
]


def parse_scopes(raw: str) -> frozenset[str]:
    """Parse a comma-separated scope string into a set of scope names.

    Whitespace around each entry is stripped and empty entries are dropped, so
    ``"read, write,"``, ``"read,write"``, and ``" read , , write "`` all parse
    to ``{"read", "write"}``. An empty or whitespace-only input yields the empty
    set. This mirrors the scopes field owned by
    :class:`~hyperdjango.signing.SignedAPIKeyMixin` (default ``"*"``); it is the
    single parser services share instead of re-implementing the CSV split.
    """
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def has_scope(scopes: frozenset[str] | str, required: str) -> bool:
    """Return whether ``required`` is granted by ``scopes``.

    ``required`` is granted when it is present in ``scopes`` or when the
    wildcard ``"*"`` is — the one capability rule services share instead of
    re-writing ``required not in scopes and "*" not in scopes``. ``scopes`` may
    be an already-parsed ``frozenset`` or a raw comma-separated string (parsed
    on the fly via :func:`parse_scopes`), so either form works with no behavior
    difference. Dependency-free.
    """
    if isinstance(scopes, str):
        scopes = parse_scopes(scopes)
    return required in scopes or "*" in scopes


def require_scope(scopes: frozenset[str] | str, required: str) -> None:
    """Raise ``HTTPException(403)`` unless ``required`` is granted by ``scopes``.

    The one 403-raising scope gate services share instead of hand-rolling
    ``if not has_scope(...): raise HTTPException(403, ...)``. ``scopes`` may be an
    already-parsed ``frozenset`` or a raw comma-separated string — it delegates
    straight to :func:`has_scope`, so either form works with no behavior
    difference. Returns ``None`` when the scope is granted; fail closed
    otherwise. A caller keeps only its own identity-to-scopes extraction and
    funnels the capability check through here.
    """
    if not has_scope(scopes, required):
        raise HTTPException(403, f"Scope {required!r} required")


@dataclass(slots=True, frozen=True)
class ResolvedIdentity:
    """A caller whose identity was verified, and how.

    ``method`` is ``"token"`` or ``"cert"``; ``fingerprint`` is the client
    certificate's SHA-256 hex (cert method only, else ``""``) — record it in
    audit rows so a leaked certificate can be traced and pinned.
    """

    identity: object  # the SignedAPIKeyMixin instance (.id, .name, .scopes, ...)
    method: str
    fingerprint: str = ""


async def resolve_identity(
    source,
    model,
    *,
    fingerprint_field: str | None = None,
) -> ResolvedIdentity:
    """Authenticate ``source`` against ``model`` (bearer token, then cert).

    ``model`` is a :class:`~hyperdjango.signing.SignedAPIKeyMixin` subclass
    (used for ``verify()`` and a ``name``-keyed lookup).

    The certificate leg's attestation is resolved by
    :func:`~hyperdjango.mtls.resolve_client_cert` from the process registry of
    running in-process terminators (each self-registers on start) or a
    configured ``MTLS_PROXY_SECRET`` — nothing is hand-carried. ``fingerprint_field``,
    when given, names an attribute on the identity holding a comma-separated
    allow-list of certificate fingerprints — a non-empty value pins the identity
    to those certificates (an empty/absent value accepts any CA-issued cert with
    the right CN).

    Raises :class:`~hyperdjango.exceptions.HTTPException` (401/403) on any
    failure — fail closed.
    """
    auth_header = source.headers.get("authorization", "")
    # RFC 7235 auth-schemes are case-insensitive: match "bearer" in any case
    # (a lowercase "bearer " must not fall through to the cert leg and 401).
    # The token68 credential itself is left byte-exact — only the scheme is
    # matched case-insensitively.
    if auth_header[:7].lower() == "bearer ":
        identity = await model.verify(auth_header[7:])
        if identity is None or not identity.is_active:
            raise HTTPException(401, "Invalid or revoked token")
        return ResolvedIdentity(identity=identity, method="token")

    cert: ClientCertIdentity | None = resolve_client_cert(source)
    if cert is not None:
        identity = await model.objects.filter(name=cert.common_name).first()
        if identity is None or not identity.is_active:
            raise HTTPException(401, "Certificate identity unknown or revoked")
        if fingerprint_field:
            # dynamic-attr: fingerprint_field is a caller-supplied model field name (configurable), not statically knowable
            pinned = (getattr(identity, fingerprint_field, "") or "").strip()
            if pinned:
                # Normalize both sides (lowercase, strip colons/whitespace) so an
                # allow-list entry pasted in OpenSSL AB:CD: form matches the
                # terminator's lowercase-hex fingerprint instead of failing closed
                # on a cosmetic difference. Empty-after-split entries are dropped.
                allowed = {
                    norm
                    for fp in pinned.split(",")
                    if (norm := normalize_fingerprint(fp))
                }
                if normalize_fingerprint(cert.fingerprint_sha256) not in allowed:
                    raise HTTPException(
                        403, "Client certificate fingerprint not authorized"
                    )
        return ResolvedIdentity(
            identity=identity, method="cert", fingerprint=cert.fingerprint_sha256
        )

    raise HTTPException(401, "Bearer token or client certificate required")
