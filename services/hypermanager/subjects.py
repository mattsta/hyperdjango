"""
HyperManager subject grammar + prefix-matching authority.

The single source of truth for how subjects are validated and how a grant/
subscription prefix matches a subject. Kept dependency-free (no signing key,
no ORM) so it can be imported by the client for delivery-side filtering without
dragging in server-only configuration — ``models`` re-exports every name here.
"""

import re

# Subject grammar: lowercase hierarchical path, e.g. "secrets/prod/api/stripe_key".
SUBJECT_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
KIND_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
MAX_SUBJECT_LEN = 200

# A single capability scope token on an identity (e.g. "feed", "admin"). Format
# only — not an allow-list — so operators can mint app-specific scopes, but never
# arbitrary/oversized junk that would land unvalidated on the credential.
SCOPE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")

# Service-identity name grammar: the conventional "namespace:name" shape
# (e.g. "service:platform-api"), 3..128 chars, lowercase, restricted to the same
# character class as a subject segment plus ':' for the namespace separator.
# Anchored start/end on an alphanumeric so a name can never begin or end with a
# separator, and control chars / unicode / whitespace are excluded outright — the
# name flows into mTLS CN matching, audit rows, and the admin UI, so it must be
# as constrained as every other identifier here rather than merely length-capped.
IDENTITY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9:_.-]{1,126}[a-z0-9]$")


def valid_subject(subject: str) -> bool:
    if not subject or len(subject) > MAX_SUBJECT_LEN:
        return False
    return all(SUBJECT_SEGMENT_RE.match(seg) for seg in subject.split("/"))


def valid_prefix(prefix: str) -> bool:
    """A grant/subscription prefix: a subject, optionally ending with '/'."""
    return valid_subject(prefix.rstrip("/")) if prefix else False


def valid_identity_name(name: str) -> bool:
    """A service-identity name: the ``IDENTITY_NAME_RE`` grammar (3..128 chars,
    lowercase, subject-segment charset plus ':' for the namespace separator).
    Fail closed on control chars, unicode, whitespace, or bad shape — the name
    is not merely length-checked because it lands in mTLS CN matching, audit
    rows, and the admin UI."""
    return bool(IDENTITY_NAME_RE.match(name))


def valid_scopes(scopes: str) -> bool:
    """A comma-separated capability list: at least one token, each well-formed
    (see ``SCOPE_RE``). Fail closed on empty/garbage."""
    tokens = [t.strip() for t in scopes.split(",") if t.strip()]
    return bool(tokens) and all(SCOPE_RE.match(t) for t in tokens)


def subject_matches(prefix: str, subject: str) -> bool:
    """Prefix semantics: 'a/b/' covers the subtree; 'a/b' covers the exact
    subject and its subtree. Empty prefix matches nothing (fail closed)."""
    if not prefix:
        return False
    if prefix.endswith("/"):
        return subject.startswith(prefix)
    return subject == prefix or subject.startswith(prefix + "/")


def prefix_covered(grant: str, requested: str) -> bool:
    """True when a subscription for ``requested`` is authorized by ``grant``:
    every subject matching ``requested`` also matches ``grant``. Both are
    prefix strings (trailing slash optional). Fail closed on empties.

    ``subject_matches`` is the single matching authority. A requested exact
    prefix ("a/b") must have its EXACT node covered by the grant; a requested
    subtree ("a/b/") only requires its children covered — so the trailing slash
    is preserved here, never stripped from both sides. (Stripping widened a
    subtree-only grant "a/b/" to admit the exact node "a/b", which that grant
    excludes: a one-subject authorization leak.)"""
    if not grant or not requested:
        return False
    root = requested.rstrip("/")
    if not root:
        return False
    # Probe the grant with the tightest representative subject of the request:
    # the exact node for "a/b" (covering it covers the whole subtree too), or,
    # for the subtree form "a/b/", a child under it. \x00 is not a legal subject
    # character, so the probe can never spuriously complete a grant segment
    # boundary and widen coverage.
    probe = root + "/\x00" if requested.endswith("/") else root
    return subject_matches(grant, probe)
