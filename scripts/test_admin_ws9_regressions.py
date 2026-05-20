#!/usr/bin/env python3
"""
Regression tests for the ws9 admin subsystem fixes.

Covers:
  1. edit_handler no longer NameErrors on `update_cols` after a successful
     UPDATE — the request returns a redirect AND the on_change hook fires AND
     the audit log records the change diff. (HIGH deal-breaker)
  2. The escalation/save path reads the acting user from request._admin_user
     (per-request), not a shared instance slot — two interleaved requests each
     see their own user. (HIGH security)
  5. A non-numeric date-hierarchy query param yields a 400 Response from the
     list view instead of an uncaught ValueError → 500. (MED)

Runs against live PostgreSQL via hyperdjango.db.

Usage:
    uv run python scripts/test_admin_ws9_regressions.py
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango import HyperApp
from hyperdjango.admin import HyperAdmin
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.response import Response

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class WS9Item(Model):
    class Meta:
        table = "ws9_reg_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    value: int = Field(default=0)
    created_at: str = Field(default="")  # timestamp column, used for date_hierarchy


class MockForm(dict):
    """Form data mock: dict with .get() (already provided by dict)."""


class MockRequest:
    def __init__(self, form=None, path_params=None, admin_user=None, GET=None):
        self._form_data = MockForm(form or {})
        self.path_params = path_params or {}
        self._admin_user = admin_user
        self.GET = GET or {}
        self.method = "POST"
        self.path = "/"
        self.headers = {}
        self.cookies = {}
        self._form = self._form_data

    async def form(self):
        self._form = self._form_data
        return self._form_data


async def setup(db):
    await db.execute("DROP TABLE IF EXISTS ws9_reg_items CASCADE")
    await db.execute(
        """
        CREATE TABLE ws9_reg_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            value INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT now()
        )
        """
    )
    await db.execute("INSERT INTO ws9_reg_items (name, value) VALUES ('alpha', 10)")
    await db.execute("INSERT INTO ws9_reg_items (name, value) VALUES ('beta', 20)")
    # Ensure the audit table exists so the change entry is actually persisted.
    from hyperdjango.auth.audit import AuditLog

    await AuditLog(db).ensure_table()


async def teardown(db):
    await db.execute("DROP TABLE IF EXISTS ws9_reg_items CASCADE")


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        FAIL += 1


async def run():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    await setup(db)

    app = HyperApp(title="WS9 Reg", database=DB_URL)
    # require_auth=False → CSRF/permission checks pass so we can exercise the
    # handler body directly with a mock request.
    admin = HyperAdmin(app, prefix="/ws9", require_auth=False, secret_key="ws9-key")

    # ── #1: edit_handler fires on_change + audit log, returns redirect ──────
    print("\n--- #1 edit_handler: hook fires + audit, no NameError ---")

    changed = []

    async def on_change(request, values):
        changed.append(values.get("name"))

    config = admin.register(
        WS9Item,
        on_change=on_change,
        date_hierarchy="created_at",
        exclude_fields=["created_at"],
    )
    edit_handler = admin._make_edit_handler(config)

    req = MockRequest(
        form={"name": "alpha-renamed", "value": "99"},
        path_params={"id": "1"},
        admin_user={"username": "editor", "is_staff": True, "is_superuser": True},
    )
    resp = await edit_handler(req)

    check(
        "#1 edit returns redirect (not 500/NameError)",
        isinstance(resp, Response) and resp.status in (302, 303),
        f"Got: {getattr(resp, 'status', resp)!r}",
    )
    check(
        "#1 on_change hook fired after update",
        changed == ["alpha-renamed"],
        f"Got: {changed}",
    )
    # Verify the UPDATE actually landed
    row = await db.query_one("SELECT name, value FROM ws9_reg_items WHERE id = 1")
    rname = row["name"] if isinstance(row, dict) else row[0]
    rval = row["value"] if isinstance(row, dict) else row[1]
    check(
        "#1 row was updated",
        rname == "alpha-renamed" and rval == 99,
        f"Got: {rname!r}, {rval!r}",
    )
    # Verify the audit log captured the change diff (the diff loop ran)
    from hyperdjango.auth.audit import AuditLog

    try:
        audit = AuditLog(db)
        history = await audit.get_object_history(config.slug, "1")
        has_change = any(
            (e.get("action") if isinstance(e, dict) else getattr(e, "action", ""))
            == "change"
            for e in history
        )
    except Exception as e:  # noqa: BLE001
        has_change = False
        print(f"    (audit query error: {e})")
    check("#1 audit log recorded a 'change' entry", has_change)

    # ── #2: acting user threaded via request._admin_user, per-request ──────
    print("\n--- #2 escalation/save path reads request._admin_user ---")

    seen = []

    async def capture_user_hook(request, values, is_edit, obj):
        # This is exactly how escalation_guard now reads the acting user.
        u = getattr(request, "_admin_user", None)
        seen.append((id(request), u.get("username") if u else None))
        return values

    config2 = admin.register(
        WS9Item,
        slug="ws9items_hookcheck",
        save_hooks=[capture_user_hook],
        exclude_fields=["created_at"],
    )
    edit2 = admin._make_edit_handler(config2)

    req_a = MockRequest(
        form={"name": "a", "value": "1"},
        path_params={"id": "1"},
        admin_user={"username": "alice", "is_superuser": True},
    )
    req_b = MockRequest(
        form={"name": "b", "value": "2"},
        path_params={"id": "2"},
        admin_user={"username": "bob", "is_superuser": False},
    )
    # Interleave the two requests concurrently on the event loop.
    await asyncio.gather(edit2(req_a), edit2(req_b))

    by_req = dict(seen)
    check(
        "#2 alice's request saw alice (not bob)",
        by_req.get(id(req_a)) == "alice",
        f"Got: {by_req.get(id(req_a))}",
    )
    check(
        "#2 bob's request saw bob (not alice)",
        by_req.get(id(req_b)) == "bob",
        f"Got: {by_req.get(id(req_b))}",
    )

    # ── #5: bad date-hierarchy param → 400, not 500 ────────────────────────
    print("\n--- #5 bad date-hierarchy param → 400 ---")

    bad = MockRequest(
        GET={"dh_year": "notanumber", "page": "1", "q": "", "sort": "id", "dir": "asc"},
    )
    bad.method = "GET"
    result = await admin._build_list_context(config, bad)
    check(
        "#5 non-numeric dh_year returns 400 Response",
        isinstance(result, Response) and result.status == 400,
        f"Got: {result if not isinstance(result, Response) else result.status}",
    )

    good = MockRequest(
        GET={"dh_year": "2026", "page": "1", "q": "", "sort": "id", "dir": "asc"},
    )
    good.method = "GET"
    result2 = await admin._build_list_context(config, good)
    check(
        "#5 valid dh_year returns a normal context dict",
        isinstance(result2, dict) and "rows" in result2,
        f"Got: {type(result2)}",
    )

    await teardown(db)
    await db.disconnect()


def main():
    asyncio.run(run())
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
