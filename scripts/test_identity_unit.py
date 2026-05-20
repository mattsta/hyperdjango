"""
Unit tests for hyperdjango.identity.resolve_identity.

# hyper-test: unit

Model-agnostic: uses a stub identity model + stub verify()/objects so the test
has no database dependency. Proves the two auth legs, fail-closed behavior, and
per-identity certificate fingerprint pinning.
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.conf import clear_settings_cache  # noqa: E402
from hyperdjango.exceptions import HTTPException  # noqa: E402
from hyperdjango.identity import ResolvedIdentity, resolve_identity  # noqa: E402
from hyperdjango.mtls import (  # noqa: E402
    ATTEST_HEADER,
    CN_HEADER,
    FINGERPRINT_HEADER,
    _deregister_attestation,
    _register_attestation,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


TERM_SECRET = "terminator-attestation-secret"


@dataclass
class _StubIdentity:
    id: int
    name: str
    is_active: bool = True
    cert_fingerprint: str = ""


class _Objects:
    """Stand-in for model.objects.filter(name=...).first()."""

    def __init__(self, rows):
        self._rows = rows
        self._name = None

    def filter(self, **kw):
        self._name = kw.get("name")
        return self

    async def first(self):
        for r in self._rows:
            if r.name == self._name:
                return r
        return None


class _Model:
    """Stub SignedAPIKeyMixin-shaped model. verify() maps a raw token to a row."""

    _rows = [
        _StubIdentity(1, "service:api"),
        _StubIdentity(2, "service:revoked", is_active=False),
        _StubIdentity(3, "service:pinned", cert_fingerprint="AABBCC"),
        # Comma-separated multi-fingerprint allow-list, and one padded with
        # stray commas / whitespace to exercise split normalization.
        _StubIdentity(4, "service:multipin", cert_fingerprint="AABBCC,DDEEFF"),
        _StubIdentity(5, "service:commapin", cert_fingerprint=" AABBCC , , DDEEFF ,"),
        # Allow-list pasted in OpenSSL `-fingerprint -sha256` style (uppercase,
        # colon-separated) must still match the terminator's lowercase-hex form.
        _StubIdentity(6, "service:openssl", cert_fingerprint="A1:B2:C3:D4:E5"),
    ]
    objects = _Objects(_rows)
    _valid_tokens = {"good-token": "service:api", "revoked-token": "service:revoked"}

    @classmethod
    async def verify(cls, raw):
        name = cls._valid_tokens.get(raw)
        if name is None:
            return None
        return next(r for r in cls._rows if r.name == name)


class _Source:
    def __init__(self, headers):
        self.headers = headers


def _cert_headers(cn, fingerprint="", attest=TERM_SECRET):
    return {
        ATTEST_HEADER: attest,
        CN_HEADER: cn,
        FINGERPRINT_HEADER: fingerprint,
    }


async def _run():
    # -- bearer token leg --
    r = await resolve_identity(_Source({"authorization": "Bearer good-token"}), _Model)
    check(
        "token: valid → resolved",
        isinstance(r, ResolvedIdentity) and r.method == "token",
    )
    check("token: names the identity", r.identity.name == "service:api")
    check("token: no fingerprint on token method", r.fingerprint == "")

    # RFC 7235 auth-schemes are case-insensitive: a lowercase / mixed-case
    # "bearer" must be accepted, not fall through to the cert leg and 401.
    for scheme in ("bearer", "BEARER", "BeArEr"):
        rc = await resolve_identity(
            _Source({"authorization": f"{scheme} good-token"}), _Model
        )
        check(
            f"token: {scheme!r} scheme accepted (case-insensitive)",
            rc.method == "token" and rc.identity.name == "service:api",
        )

    await _expect_status(
        "token: forged → 401",
        _Source({"authorization": "Bearer nope"}),
        _Model,
        401,
    )
    await _expect_status(
        "token: revoked identity → 401",
        _Source({"authorization": "Bearer revoked-token"}),
        _Model,
        401,
    )

    # -- certificate leg --
    # The certificate attestation is resolved from the process registry (an
    # in-process terminator self-registers on start); register TERM_SECRET here
    # to stand in for a running terminator, and deregister when done.
    _register_attestation(TERM_SECRET)
    try:
        r = await resolve_identity(_Source(_cert_headers("service:api")), _Model)
        check(
            "cert: valid CN → resolved",
            r.method == "cert" and r.identity.name == "service:api",
        )

        await _expect_status(
            "cert: unknown CN → 401",
            _Source(_cert_headers("service:ghost")),
            _Model,
            401,
        )
        await _expect_status(
            "cert: wrong attestation → 401 (headers not honored)",
            _Source(_cert_headers("service:api", attest="forged")),
            _Model,
            401,
        )

        # -- no credential --
        await _expect_status("no credential → 401", _Source({}), _Model, 401)

        # -- fingerprint pinning --
        r = await resolve_identity(
            _Source(_cert_headers("service:pinned", fingerprint="AABBCC")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "pin: matching fingerprint → resolved", r.identity.name == "service:pinned"
        )
        check("pin: fingerprint recorded", r.fingerprint == "AABBCC")

        await _expect_status(
            "pin: wrong fingerprint → 403",
            _Source(_cert_headers("service:pinned", fingerprint="DEADBEEF")),
            _Model,
            403,
            fingerprint_field="cert_fingerprint",
        )
        # An identity with no pin accepts any CA cert with its CN.
        r = await resolve_identity(
            _Source(_cert_headers("service:api", fingerprint="whatever")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "pin: unpinned identity accepts any fingerprint",
            r.identity.name == "service:api",
        )

        # -- multi-fingerprint allow-list (comma-separated) --
        r = await resolve_identity(
            _Source(_cert_headers("service:multipin", fingerprint="DDEEFF")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "multi-pin: a listed fingerprint matches",
            r.identity.name == "service:multipin" and r.fingerprint == "DDEEFF",
        )
        r = await resolve_identity(
            _Source(_cert_headers("service:multipin", fingerprint="AABBCC")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "multi-pin: the other listed fingerprint also matches",
            r.identity.name == "service:multipin",
        )
        await _expect_status(
            "multi-pin: an unlisted fingerprint → 403",
            _Source(_cert_headers("service:multipin", fingerprint="C0FFEE")),
            _Model,
            403,
            fingerprint_field="cert_fingerprint",
        )

        # -- stray-comma / whitespace normalization in the allow-list --
        r = await resolve_identity(
            _Source(_cert_headers("service:commapin", fingerprint="DDEEFF")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "pin: stray commas + whitespace normalize away (still matches)",
            r.identity.name == "service:commapin",
        )
        await _expect_status(
            "pin: empty-after-split entries never admit an empty fingerprint",
            _Source(_cert_headers("service:commapin", fingerprint="")),
            _Model,
            403,
            fingerprint_field="cert_fingerprint",
        )

        # -- format-insensitive pinning: OpenSSL colon/uppercase pin vs the
        # terminator's lowercase-hex fingerprint (both normalized before compare)
        r = await resolve_identity(
            _Source(_cert_headers("service:openssl", fingerprint="a1b2c3d4e5")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "pin: OpenSSL AB:CD uppercase allow-list matches lowercase-hex cert",
            r.identity.name == "service:openssl",
        )
        # The reverse also holds: an incoming colon/uppercase fingerprint matches
        # a lowercase-hex allow-list entry.
        r = await resolve_identity(
            _Source(_cert_headers("service:pinned", fingerprint="aa:BB:cc")),
            _Model,
            fingerprint_field="cert_fingerprint",
        )
        check(
            "pin: colon/uppercase incoming fingerprint matches AABBCC pin",
            r.identity.name == "service:pinned",
        )
        await _expect_status(
            "pin: a different fingerprint still rejected after normalization",
            _Source(_cert_headers("service:openssl", fingerprint="ffffffffff")),
            _Model,
            403,
            fingerprint_field="cert_fingerprint",
        )
    finally:
        _deregister_attestation(TERM_SECRET)

    # -- external-proxy attestation (MTLS_PROXY_SECRET), no terminator registry --
    # With every in-process terminator deregistered, the certificate leg's
    # attestation can only come from the configured proxy secret. Set it, prove a
    # matching attestation resolves the cert identity and a mismatching one is
    # refused (headers ignored → no certificate → 401), then restore.
    os.environ["HYPER_MTLS_PROXY_SECRET"] = "external-proxy-shared-secret-padded-xyz"
    clear_settings_cache()
    try:
        r = await resolve_identity(
            _Source(
                _cert_headers(
                    "service:api", attest="external-proxy-shared-secret-padded-xyz"
                )
            ),
            _Model,
        )
        check(
            "proxy-secret: matching attestation resolves the cert identity",
            r.method == "cert" and r.identity.name == "service:api",
        )
        await _expect_status(
            "proxy-secret: mismatching attestation → 401 (headers ignored)",
            _Source(_cert_headers("service:api", attest="not-the-proxy-secret")),
            _Model,
            401,
        )
    finally:
        os.environ.pop("HYPER_MTLS_PROXY_SECRET", None)
        clear_settings_cache()


async def _expect_status(name, source, model, status, **kw):
    try:
        await resolve_identity(source, model, **kw)
        check(name, False, "no HTTPException raised")
    except HTTPException as exc:
        check(name, exc.status_code == status, f"got {exc.status_code}")


def test_scope_helpers():
    print("\n== scope helpers: parse_scopes + has_scope ==")
    from hyperdjango.identity import has_scope, parse_scopes

    # parse_scopes: CSV, trimmed, empties dropped — the exact semantics both
    # apps hand-rolled (frozenset(s.strip() for s in raw.split(",") if s.strip())).
    check(
        "parse: simple CSV", parse_scopes("read,write") == frozenset({"read", "write"})
    )
    check(
        "parse: whitespace around entries trimmed",
        parse_scopes(" read , write ") == frozenset({"read", "write"}),
    )
    check(
        "parse: empty-after-split entries dropped",
        parse_scopes("read,,write,") == frozenset({"read", "write"}),
    )
    check(
        "parse: stray commas + whitespace normalize away",
        parse_scopes(" , read , , ") == frozenset({"read"}),
    )
    check("parse: empty string → empty set", parse_scopes("") == frozenset())
    check("parse: whitespace-only → empty set", parse_scopes("   ") == frozenset())
    check("parse: wildcard preserved", parse_scopes("*") == frozenset({"*"}))

    # has_scope: present OR wildcard grants; the rule both apps hand-rolled as
    # `scope not in scopes and "*" not in scopes → deny`.
    scopes = parse_scopes("read,write")
    check("has: a present scope is granted", has_scope(scopes, "read"))
    check("has: an absent scope is denied", not has_scope(scopes, "admin"))
    check("has: wildcard grants any scope", has_scope(parse_scopes("*"), "anything"))
    check(
        "has: wildcard among others grants any scope",
        has_scope(parse_scopes("read,*"), "delete"),
    )
    check(
        "has: empty scopes deny a non-wildcard scope",
        not has_scope(frozenset(), "read"),
    )

    # Either input form works: a raw CSV string is parsed on the fly, with no
    # behavior difference from a pre-parsed frozenset.
    check("has: raw CSV string accepted", has_scope("read,write", "write"))
    check("has: raw wildcard string grants any", has_scope("*", "whatever"))
    check("has: raw string absent scope denied", not has_scope("read", "write"))
    check("has: empty raw string denies", not has_scope("", "read"))


def test_require_scope():
    print("\n== require_scope: 403 gate over has_scope ==")
    from hyperdjango.identity import parse_scopes, require_scope

    # Granted → returns None, no raise (present scope, wildcard, and raw CSV).
    def _granted(name, scopes, required):
        raised = False
        try:
            require_scope(scopes, required)
        except HTTPException:
            raised = True
        check(name, not raised)

    _granted("present scope granted", parse_scopes("read,write"), "read")
    _granted("wildcard grants any scope", parse_scopes("*"), "anything")
    _granted("raw CSV scopes accepted (delegates to has_scope)", "read,write", "write")
    _granted("raw wildcard string grants any", "*", "whatever")

    # Absent scope → 403 with the exact message shape both apps hand-rolled
    # (`Scope {scope!r} required`), so adoption is zero-behavior-change.
    try:
        require_scope(parse_scopes("read"), "admin")
        check("absent scope raises", False, "no HTTPException raised")
    except HTTPException as exc:
        check(
            "absent scope raises 403 with the app's message shape",
            exc.status_code == 403 and exc.detail == "Scope 'admin' required",
            f"got {exc.status_code} {exc.detail!r}",
        )
    # An empty scope set denies a non-wildcard scope.
    try:
        require_scope(frozenset(), "read")
        check("empty scopes raise", False, "no HTTPException raised")
    except HTTPException as exc:
        check("empty scopes deny (403)", exc.status_code == 403)


def main() -> bool:
    print("hyperdjango.identity unit tests")
    asyncio.run(_run())
    test_scope_helpers()
    test_require_scope()
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
