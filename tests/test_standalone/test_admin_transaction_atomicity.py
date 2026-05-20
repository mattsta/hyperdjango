"""Regression test for admin add/change atomicity (F15).

The admin add/change handlers used to run the parent INSERT/UPDATE, inline
saves, M2M saves and the audit-log write as SEPARATE autocommitted statements.
A failure after the parent INSERT (e.g. a bad inline row) therefore left an
orphaned committed parent row with no children. The fix wraps the whole save
body in a single ``async with db.transaction():`` so it is all-or-nothing.

This test drives the REAL ``HyperAdmin._make_add_handler`` closure with a fake
``db`` whose ``query_val`` / ``execute`` autocommit immediately when NOT inside
a transaction (mirroring Postgres) and only commit on the outermost transaction
exit otherwise. With that faithful model the test fails on the unwrapped code
(orphan row survives) and passes once the body is wrapped.

Run: uv run pytest tests/test_standalone/test_admin_transaction_atomicity.py -q
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hyperdjango.admin import HyperAdmin


class FakeDB:
    """Minimal db modelling Postgres autocommit vs. explicit transactions.

    - Outside a transaction, each execute/query_val commits immediately.
    - Inside a transaction, writes stay pending until the OUTERMOST transaction
      exits cleanly (COMMIT); an exception discards them (ROLLBACK).
    """

    def __init__(self) -> None:
        self.committed_rows: list[str] = []
        self._pending: list[str] = []
        self._depth = 0
        self.events: list[str] = []

    def _write(self, tag: str) -> None:
        if self._depth > 0:
            self._pending.append(tag)
        else:
            # Autocommit path (no surrounding transaction)
            self.committed_rows.append(tag)

    async def query_val(self, sql, *args):
        self._write("parent")
        self.events.append("query_val")
        return 101  # simulated RETURNING id

    async def execute(self, sql, *args):
        self._write("parent")
        self.events.append("execute")

    @asynccontextmanager
    async def transaction(self, savepoint_name=None):
        self._depth += 1
        outermost = self._depth == 1
        if outermost:
            self.events.append("BEGIN")
        try:
            yield self
        except Exception:
            if outermost:
                self._pending.clear()  # ROLLBACK
                self.events.append("ROLLBACK")
            raise
        else:
            if outermost:
                self.committed_rows.extend(self._pending)  # COMMIT
                self._pending.clear()
                self.events.append("COMMIT")
        finally:
            self._depth -= 1


def _make_admin_and_config(*, inline_fails: bool):
    """Build a fake HyperAdmin `self` (bypassing __init__) and a config that
    drives the add handler down the INSERT → inline-save path."""
    admin = HyperAdmin.__new__(HyperAdmin)
    db = FakeDB()

    meta = SimpleNamespace(pk_field="id", auto_field="id", table="widget")
    config = SimpleNamespace(
        model_class=SimpleNamespace(_meta=meta),
        save_hooks=[],
        inlines=[object()],  # truthy -> triggers _save_inlines
        filter_horizontal=None,
        on_add=None,
        name="Widget",
        slug="widget",
    )

    async def _enforce_post_security(cfg, request, perm):
        return None

    def _get_db():
        return db

    def _parse_form_data(cfg, form_data):
        return ({"id": None, "title": "hello"}, None)

    async def _save_inlines(cfg, parent_pk, form_data):
        db.events.append("save_inlines")
        if inline_fails:
            raise RuntimeError("inline row rejected")

    async def _audit_log(*args, **kwargs):
        db.events.append("audit_log")

    def _post_save_redirect(cfg, request, pk, action, msg):
        return SimpleNamespace(kind="redirect", pk=pk)

    admin._enforce_post_security = _enforce_post_security
    admin._get_db = _get_db
    admin._parse_form_data = _parse_form_data
    admin._save_inlines = _save_inlines
    admin._audit_log = _audit_log
    admin._post_save_redirect = _post_save_redirect

    handler = HyperAdmin._make_add_handler(admin, config)
    return admin, config, db, handler


class _FakeRequest:
    async def form(self):
        return {"title": "hello"}


async def test_inline_failure_rolls_back_parent_insert():
    """An inline-save failure must roll back the parent INSERT — no orphan."""
    _admin, _config, db, handler = _make_admin_and_config(inline_fails=True)

    with pytest.raises(RuntimeError, match="inline row rejected"):
        await handler(_FakeRequest())

    assert db.committed_rows == [], f"orphaned committed rows: {db.committed_rows}"
    assert "BEGIN" in db.events and "ROLLBACK" in db.events
    assert "COMMIT" not in db.events


async def test_successful_add_commits_parent_once():
    """The happy path still commits the parent row exactly once, atomically."""
    _admin, _config, db, handler = _make_admin_and_config(inline_fails=False)

    result = await handler(_FakeRequest())

    assert getattr(result, "kind", None) == "redirect"
    assert db.committed_rows == ["parent"]
    assert db.events.index("BEGIN") < db.events.index("COMMIT")
    # Parent + inlines + audit all landed inside the same transaction
    for ev in ("query_val", "save_inlines", "audit_log"):
        assert (
            db.events.index("BEGIN") < db.events.index(ev) < db.events.index("COMMIT")
        )
