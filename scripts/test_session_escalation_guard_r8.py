#!/usr/bin/env python3
"""Round-8 security regression: the request.session bridge must not let
application/anonymous code establish identity or authorization.

The real privilege-escalation vector (round-8 audit): an anonymous request whose
session is server-signed could do ``request.session["user_id"] = 999`` (or
``["groups"] = ["superuser"]``) and have it load back as *trusted* state on the
next request. The fix guards the writes at the `_SessionDict` bridge — NOT by
gutting the identity allow-list (which would break username/pk auth pinned by
test_security_regressions_round3) nor by second-guessing SessionUser.is_authenticated
(which would break group-only authorization pinned by test_rbac_guards).
"""

# hyper-test: unit

from hyperdjango.auth.sessions import (
    _AUTH_IDENTITY_KEYS,
    _RESERVED_SESSION_KEYS,
    _is_user_session,
    _SessionDict,
)
from hyperdjango.auth.user import SessionUser
from hyperdjango.testkit import check, finish, run_main


def test_bridge_rejects_reserved_writes() -> None:
    """Application writes to reserved auth keys are dropped (not persisted)."""
    s = _SessionDict({"cart": [1, 2]})  # loaded/app state
    for key in ("user_id", "id", "pk", "username", "groups", "is_superuser"):
        s[key] = "attacker"
        check(f"bridge drops reserved write session[{key!r}]", key not in s)
    # Non-reserved app keys still work and mark modified.
    s["cart"] = [3]
    s["flash"] = "hi"
    check("bridge allows non-reserved write", s.get("flash") == "hi")
    check("non-reserved write marks the session modified", s.modified)
    # update()/setdefault() are guarded too.
    s.update({"user_id": 7, "cart": [9]})
    check("update() filters reserved keys", "user_id" not in s and s.get("cart") == [9])
    s.setdefault("is_staff", True)
    check("setdefault() rejects reserved keys", "is_staff" not in s)


def test_trusted_construction_preserves_identity() -> None:
    """Loading trusted store data (via construction) keeps identity keys —
    only *writes* through the bridge are guarded."""
    loaded = _SessionDict({"user_id": 42, "groups": ["superuser"]})
    check("constructed session keeps loaded user_id", loaded.get("user_id") == 42)
    check("constructed login session is a user session", _is_user_session(loaded))


def test_identity_allowlist_contract_preserved() -> None:
    """round-3 contract: user_id/id/pk/username all authenticate."""
    for key in ("user_id", "id", "pk", "username"):
        check(f"{key!r} is an identity key", key in _AUTH_IDENTITY_KEYS)
        check(f"session with {key!r} authenticates", _is_user_session({key: 1}))


def test_sessionuser_authenticated_by_construction() -> None:
    """Group-only authorization: a directly-built SessionUser is authenticated."""
    u = SessionUser({"groups": ["admin"]})
    check("group-only SessionUser is authenticated", u.is_authenticated is True)
    check("group-only SessionUser is not anonymous", u.is_anonymous is False)


def test_reserved_covers_authz_fields() -> None:
    for key in ("groups", "permissions", "is_staff", "is_superuser", "role"):
        check(f"{key!r} is a reserved (authz) key", key in _RESERVED_SESSION_KEYS)


def main() -> bool:
    for fn in (
        test_bridge_rejects_reserved_writes,
        test_trusted_construction_preserves_identity,
        test_identity_allowlist_contract_preserved,
        test_sessionuser_authenticated_by_construction,
        test_reserved_covers_authz_fields,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
