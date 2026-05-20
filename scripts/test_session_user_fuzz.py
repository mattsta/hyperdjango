"""Hypothesis fuzz tests for SessionUser typed RBAC interface (v0.16.3).

Targets:
- ``SessionUser.__post_init__`` frozenset materialization from arbitrary session dicts
- ``user.in_group()`` / ``user.has_perm()`` O(1) membership checks
- ``user.is_staff`` / ``user.is_superuser`` derived-from-groups properties
- ``AnonymousUser`` interface parity
- ``PermissionChecker.ensure_group()`` idempotency (live DB)
- ``PermissionChecker.ensure_admin_user()`` idempotency (live DB)

Properties checked:

1. **groups is always frozenset[str]**: regardless of input shape (list, None,
   int, empty, nested), ``user.groups`` is always a ``frozenset``.

2. **in_group consistency**: ``user.in_group(x)`` == ``x in user.groups``.

3. **has_perm consistency**: ``user.has_perm(x)`` == ``x in user.permissions``
   OR ``"superuser" in user.groups``.

4. **is_staff/is_superuser derived**: ``user.is_staff`` == ``"staff" in user.groups``,
   ``user.is_superuser`` == ``"superuser" in user.groups``.

5. **AnonymousUser is always denied**: in_group/has_perm always False, groups/perms empty.

6. **ensure_group idempotency**: calling twice returns same group.id.

7. **ensure_admin_user idempotency**: calling twice returns same user.id.

Usage:
    uv run hyper-test session_user_fuzz
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.auth.user import AnonymousUser, SessionUser

PASS = 0
FAIL = 0
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

# ── Strategies ──────────────────────────────────────────────────────────────

# Realistic group names
group_names = st.sampled_from(
    [
        "staff",
        "superuser",
        "admin",
        "editor",
        "viewer",
        "moderator",
        "agent",
        "team_lead",
        "reader",
        "writer",
    ]
)

# Random strings for adversarial testing
random_strings = st.text(min_size=0, max_size=50)

# Session dict groups — can be list of strings, None, int, empty list, etc.
valid_groups = st.lists(group_names, min_size=0, max_size=5)
adversarial_groups = st.one_of(
    st.none(),
    st.integers(),
    st.text(),
    st.lists(st.text(min_size=0, max_size=20), min_size=0, max_size=10),
    valid_groups,
)

# Session dicts
session_dict = st.fixed_dictionaries(
    {},
    optional={
        "id": st.integers(min_value=1, max_value=10000),
        "username": st.text(min_size=0, max_size=30),
        "groups": adversarial_groups,
        "permissions": st.one_of(
            st.none(),
            st.lists(st.text(min_size=1, max_size=30), min_size=0, max_size=10),
        ),
        "is_staff": st.booleans(),
        "is_superuser": st.booleans(),
        "is_active": st.booleans(),
    },
)


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)


# ── Property tests ──────────────────────────────────────────────────────────


@given(data=session_dict)
@settings(max_examples=200)
def test_groups_always_frozenset(data):
    """user.groups is always frozenset regardless of input."""
    user = SessionUser(data)
    assert isinstance(user.groups, frozenset), f"groups is {type(user.groups)}"


@given(data=session_dict)
@settings(max_examples=200)
def test_permissions_always_frozenset(data):
    """user.permissions is always frozenset regardless of input."""
    user = SessionUser(data)
    assert isinstance(user.permissions, frozenset), (
        f"permissions is {type(user.permissions)}"
    )


@given(data=session_dict, group=random_strings)
@settings(max_examples=200)
def test_in_group_consistent(data, group):
    """in_group(x) is always x in user.groups."""
    user = SessionUser(data)
    assert user.in_group(group) == (group in user.groups)


@given(data=session_dict, perm=random_strings)
@settings(max_examples=200)
def test_has_perm_consistent(data, perm):
    """has_perm(x) is x in permissions OR superuser in groups."""
    user = SessionUser(data)
    expected = perm in user.permissions or "superuser" in user.groups
    assert user.has_perm(perm) == expected


@given(data=session_dict)
@settings(max_examples=200)
def test_is_staff_derived(data):
    """is_staff is always derived from groups."""
    user = SessionUser(data)
    assert user.is_staff == ("staff" in user.groups)


@given(data=session_dict)
@settings(max_examples=200)
def test_is_superuser_derived(data):
    """is_superuser is always derived from groups."""
    user = SessionUser(data)
    assert user.is_superuser == ("superuser" in user.groups)


@given(groups=valid_groups)
@settings(max_examples=100)
def test_frozenset_matches_input_list(groups):
    """When groups is a valid list, frozenset matches exactly."""
    user = SessionUser({"groups": groups})
    assert user.groups == frozenset(groups)


@given(groups=valid_groups, perms=st.lists(random_strings, min_size=0, max_size=5))
@settings(max_examples=100)
def test_model_dump_consistent(groups, perms):
    """model_dump() reflects derived is_staff/is_superuser."""
    user = SessionUser({"id": 1, "groups": groups, "permissions": perms})
    dump = user.model_dump()
    assert dump["is_staff"] == ("staff" in user.groups)
    assert dump["is_superuser"] == ("superuser" in user.groups)


# ── AnonymousUser invariants ────────────────────────────────────────────────


@given(group=random_strings)
@settings(max_examples=50)
def test_anonymous_always_denied_group(group):
    """AnonymousUser.in_group() is always False."""
    anon = AnonymousUser()
    assert not anon.in_group(group)


@given(perm=random_strings)
@settings(max_examples=50)
def test_anonymous_always_denied_perm(perm):
    """AnonymousUser.has_perm() is always False."""
    anon = AnonymousUser()
    assert not anon.has_perm(perm)


# ── Live DB idempotency tests ──────────────────────────────────────────────


async def test_ensure_group_idempotent():
    """ensure_group returns same group on repeated calls."""
    print("\n--- ensure_group idempotency (live DB) ---")
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import ensure_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    await ensure_rbac_tables(db=db)

    checker = PermissionChecker(db)

    g1 = await checker.ensure_group("fuzz_test_group")
    g2 = await checker.ensure_group("fuzz_test_group")
    check("same id", g1.id == g2.id, f"{g1.id} != {g2.id}")
    check("same name", g1.name == g2.name)

    # Cleanup
    await db.execute("DELETE FROM hyper_groups WHERE name = 'fuzz_test_group'")
    await db.disconnect()


async def test_ensure_admin_user_idempotent():
    """ensure_admin_user returns same user on repeated calls."""
    print("\n--- ensure_admin_user idempotency (live DB) ---")
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import ensure_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    await ensure_rbac_tables(db=db)

    checker = PermissionChecker(db)

    u1 = await checker.ensure_admin_user(username="fuzz_admin", password="fuzz123")
    u2 = await checker.ensure_admin_user(username="fuzz_admin", password="fuzz123")
    check("same id", u1.id == u2.id, f"{u1.id} != {u2.id}")
    check("same username", u1.username == u2.username)

    # Verify group membership
    groups = await checker.get_user_group_names(u1.id)
    check("in staff group", "staff" in groups, f"groups={groups}")
    check("in superuser group", "superuser" in groups, f"groups={groups}")

    # Cleanup
    await db.execute("DELETE FROM hyper_user_groups WHERE user_id = $1", u1.id)
    await db.execute("DELETE FROM hyper_users WHERE username = 'fuzz_admin'")
    await db.disconnect()


if __name__ == "__main__":
    print("=" * 60)
    print("SessionUser / PermissionChecker Fuzz Tests")
    print("=" * 60)

    # Hypothesis property tests
    print("\n--- Hypothesis: groups always frozenset ---")
    test_groups_always_frozenset()
    PASS += 1
    print("  PASS (200 examples)")

    print("\n--- Hypothesis: permissions always frozenset ---")
    test_permissions_always_frozenset()
    PASS += 1
    print("  PASS (200 examples)")

    print("\n--- Hypothesis: in_group consistent ---")
    test_in_group_consistent()
    PASS += 1
    print("  PASS (200 examples)")

    print("\n--- Hypothesis: has_perm consistent ---")
    test_has_perm_consistent()
    PASS += 1
    print("  PASS (200 examples)")

    print("\n--- Hypothesis: is_staff derived ---")
    test_is_staff_derived()
    PASS += 1
    print("  PASS (200 examples)")

    print("\n--- Hypothesis: is_superuser derived ---")
    test_is_superuser_derived()
    PASS += 1
    print("  PASS (200 examples)")

    print("\n--- Hypothesis: frozenset matches input ---")
    test_frozenset_matches_input_list()
    PASS += 1
    print("  PASS (100 examples)")

    print("\n--- Hypothesis: model_dump consistent ---")
    test_model_dump_consistent()
    PASS += 1
    print("  PASS (100 examples)")

    print("\n--- Hypothesis: anonymous always denied (group) ---")
    test_anonymous_always_denied_group()
    PASS += 1
    print("  PASS (50 examples)")

    print("\n--- Hypothesis: anonymous always denied (perm) ---")
    test_anonymous_always_denied_perm()
    PASS += 1
    print("  PASS (50 examples)")

    # Live DB tests
    asyncio.run(test_ensure_group_idempotent())
    asyncio.run(test_ensure_admin_user_idempotent())

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print("  (10 Hypothesis properties × 50-200 examples each + 6 DB checks)")
    sys.exit(0 if FAIL == 0 else 1)
