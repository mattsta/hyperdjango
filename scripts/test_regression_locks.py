"""Small regression locks for previously-uncaught bug classes.

Covers:
  A2. AnonymousUser().__bool__ is False, and the ``if request.user:`` route-guard
      idiom therefore treats an anonymous user as unauthenticated.
  A4. QuerySet._order_by_fk_keys() tolerates a None ``_ordering`` (no order_by()
      applied) instead of crashing with "NoneType is not iterable" — and the
      SELECT builder that consumes it survives an FK filter with no ORDER BY.

# hyper-test: unit
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_meta import make_model

from hyperdjango.auth.user import AnonymousUser, SessionUser
from hyperdjango.query import QuerySet

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} — {detail}")


# ---------------------------------------------------------------------------
# A2: AnonymousUser is falsy; the `if request.user:` idiom means "authenticated"
# ---------------------------------------------------------------------------


def test_anonymous_user_is_falsy() -> None:
    print("\n--- AnonymousUser falsy semantics ---")
    anon = AnonymousUser()
    check("bool(AnonymousUser()) is False", bool(anon) is False)
    check("assert not AnonymousUser()", not anon)
    check("AnonymousUser().is_authenticated is False", anon.is_authenticated is False)
    check("AnonymousUser() is not None", anon is not None)

    # A real authenticated user must stay truthy so the idiom distinguishes them.
    real = SessionUser({"id": 1, "username": "alice"})
    check("bool(SessionUser) is True", bool(real) is True)


def test_route_guard_if_request_user_idiom() -> None:
    """A route guard using the `if request.user:` idiom gates on truthiness."""
    print("\n--- Route guard: `if request.user:` idiom ---")

    class _Req:
        def __init__(self, user):
            self.user = user

    def protected_view(request) -> str:
        # The canonical idiom: falsy user (None OR AnonymousUser) → anonymous.
        if request.user:
            return "authed"
        return "anonymous"

    check(
        "AnonymousUser → treated as anonymous",
        protected_view(_Req(AnonymousUser())) == "anonymous",
    )
    check(
        "user=None → treated as anonymous",
        protected_view(_Req(None)) == "anonymous",
    )
    check(
        "real SessionUser → treated as authed",
        protected_view(_Req(SessionUser({"id": 7, "username": "bob"}))) == "authed",
    )


# ---------------------------------------------------------------------------
# A4: _order_by_fk_keys None-guard (no order_by() applied)
# ---------------------------------------------------------------------------


def _make_select_ready_qs(model, *, ordering):
    qs = QuerySet(model)
    qs._annotations = {}
    qs._filters = [("author_id", 1)]  # scalar FK filter, no traversal join
    qs._excludes = []
    qs._raw_wheres = []
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = ordering
    qs._limit = None
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False
    return qs


def test_order_by_fk_keys_none_guard() -> None:
    print("\n--- QuerySet._order_by_fk_keys None-guard ---")
    model = make_model("posts", ["id", "title", "author_id"])

    # _ordering explicitly None (no order_by() applied) must NOT crash.
    qs_none = _make_select_ready_qs(model, ordering=None)
    check(
        "_order_by_fk_keys() == [] when _ordering is None",
        qs_none._order_by_fk_keys() == [],
    )

    # Empty tuple (fresh queryset default) → [] too.
    qs_empty = _make_select_ready_qs(model, ordering=())
    check(
        "_order_by_fk_keys() == [] when _ordering is ()",
        qs_empty._order_by_fk_keys() == [],
    )

    # With order_by, the DESC-prefix is stripped and keys returned.
    qs_ord = _make_select_ready_qs(model, ordering=("-title", "id"))
    check(
        "_order_by_fk_keys() strips '-' prefix",
        qs_ord._order_by_fk_keys() == ["title", "id"],
    )

    # The consumer path (SELECT builder → _get_fk_filter_paths(include_order_by=True))
    # must survive a None ordering with an FK filter present — this is the exact
    # combination the None-guard protects.
    paths = qs_none._get_fk_filter_paths(include_order_by=True)
    check("_get_fk_filter_paths tolerates None ordering", isinstance(paths, list))
    sql, params = qs_none._build_select()
    check("_build_select() succeeds with FK filter + no ORDER BY", "SELECT" in sql)
    check("_build_select() omits ORDER BY when none applied", "ORDER BY" not in sql)


def main() -> int:
    print("=" * 60)
    print("Regression locks (A2 AnonymousUser, A4 order_by None-guard)")
    print("=" * 60)
    test_anonymous_user_is_falsy()
    test_route_guard_if_request_user_idiom()
    test_order_by_fk_keys_none_guard()
    print(f"\n{'=' * 60}\n  {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
