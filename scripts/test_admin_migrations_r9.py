#!/usr/bin/env python
"""Round-9 regression tests: admin privilege-escalation + migration/fixture/
form/doctor correctness fixes.

Pure-Python (no live DB). Run:  uv run python scripts/test_admin_migrations_r9.py

Covers:
  A1/A2  escalation_guard: a non-superuser edit/create can't set is_superuser/is_staff
  A3     privilege-granting bulk actions require a superuser
  M1     migration DDL emits ON DELETE; _diff_constraints detects on_delete drift
  M2     _diff_columns emits AlterColumnDefault on DEFAULT drift
  F1     fixture datetime/decimal/uuid/bytes/timedelta round-trip (serialize→deserialize)
  F4     IntegerField rejects non-integer floats
  DOCTOR check_debug_off uses parse_bool; check_secret_key resolves via get_setting
"""

# hyper-test: unit

import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from hyperdjango.testkit import check, finish, run_main

# ── Fixtures for a fake admin app ──────────────────────────────────────────────


class _FakeRouter:
    def add(self, *a, **k):
        pass


class _FakeApp:
    def __init__(self):
        self.router = _FakeRouter()
        self._db = None


class _Req:
    def __init__(self, admin_user):
        self._admin_user = admin_user


# ── A1/A2/A3: admin escalation_guard + bulk actions ────────────────────────────


async def test_admin_escalation():
    from hyperdjango.admin import HyperAdmin

    admin = HyperAdmin(_FakeApp(), secret_key="test-secret")
    admin.register_auth_models()
    users = admin._models["users"]

    guard = next(h for h in users.save_hooks if h.__name__ == "escalation_guard")

    # A1: non-superuser EDIT cannot escalate — original flags restored from obj.
    req = _Req({"is_superuser": False})
    obj = {"id": 7, "is_superuser": False, "is_staff": False}
    vals = await guard(req, {"is_superuser": True, "is_staff": True}, True, obj)
    check("A1 edit strips is_superuser", vals["is_superuser"] is False, str(vals))
    check("A1 edit strips is_staff", vals["is_staff"] is False, str(vals))

    # A1: preserves genuine existing privilege from obj (doesn't clobber to False
    # when the live row already had it).
    obj2 = {"id": 8, "is_superuser": True, "is_staff": True}
    vals = await guard(req, {"is_superuser": False, "is_staff": False}, True, obj2)
    check(
        "A1 edit preserves orig is_superuser", vals["is_superuser"] is True, str(vals)
    )
    check("A1 edit preserves orig is_staff", vals["is_staff"] is True, str(vals))

    # A1 fail-closed: unresolved obj → force both off.
    vals = await guard(req, {"is_superuser": True, "is_staff": True}, True, None)
    check("A1 fail-closed superuser", vals["is_superuser"] is False, str(vals))
    check("A1 fail-closed staff", vals["is_staff"] is False, str(vals))

    # A2: non-superuser CREATE cannot set is_superuser OR is_staff.
    vals = await guard(req, {"is_superuser": True, "is_staff": True}, False, None)
    check(
        "A2 create forces is_superuser False", vals["is_superuser"] is False, str(vals)
    )
    check("A2 create forces is_staff False", vals["is_staff"] is False, str(vals))

    # Superuser is unrestricted.
    sreq = _Req({"is_superuser": True})
    vals = await guard(sreq, {"is_superuser": True, "is_staff": True}, False, None)
    check(
        "superuser create keeps is_superuser", vals["is_superuser"] is True, str(vals)
    )

    # A3: privilege-granting bulk actions deny non-superusers (no DB touched).
    actions = {a.name: a for a in users.actions}
    for aname in ("add_to_staff", "add_to_superuser"):
        msg = await actions[aname].handler(users, req, ["1", "2"])
        check(
            f"A3 {aname} denies non-superuser",
            isinstance(msg, str) and "superuser" in msg.lower(),
            repr(msg),
        )


# ── M1/M2: migration DDL ────────────────────────────────────────────────────────


def test_migration_ddl():
    from hyperdjango.migrations import (
        AddColumn,
        AddConstraint,
        AlterColumnDefault,
        CreateTable,
        DbColumn,
        DbConstraint,
        DbTable,
        DropConstraint,
        ModelColumn,
        ModelSchema,
        SchemaDiffer,
    )

    fk_col = ModelColumn(
        name="author_id",
        type_sql="INTEGER",
        nullable=True,
        is_pk=False,
        is_auto=False,
        is_unique=False,
        has_index=False,
        default_sql=None,
        foreign_key="authors",
        on_delete="CASCADE",
    )
    ct = CreateTable(table="books", columns=[fk_col])
    check(
        "M1 CreateTable emits ON DELETE CASCADE",
        "ON DELETE CASCADE" in ct.up_sql(),
        ct.up_sql(),
    )

    ac = AddColumn(
        table="books",
        column="editor_id",
        type_sql="INTEGER",
        nullable=True,
        foreign_key="authors",
        on_delete="SET NULL",
    )
    check(
        "M1 AddColumn emits ON DELETE SET NULL",
        "ON DELETE SET NULL" in ac.up_sql(),
        ac.up_sql(),
    )

    # M1 drift: existing FK with default action, model wants CASCADE → drop+re-add.
    ms = ModelSchema(table="books", columns={"author_id": fk_col}, m2m_tables=[])
    db_col = DbColumn(
        name="author_id",
        type_name="int4",
        type_display="INTEGER",
        nullable=True,
        has_default=False,
        default_expr=None,
        is_serial=False,
        char_max_length=None,
    )
    existing_fk = DbConstraint(
        name="fk_books_author_id",
        type="f",
        columns=["author_id"],
        fk_table="authors",
        fk_columns=["id"],
        fk_on_delete=None,
    )
    dbt = DbTable(
        name="books",
        columns={"author_id": db_col},
        constraints=[existing_fk],
        indexes=[],
    )
    ops = SchemaDiffer._diff_constraints(ms, dbt)
    has_drop = any(isinstance(o, DropConstraint) for o in ops)
    add_with = any(
        isinstance(o, AddConstraint) and "ON DELETE CASCADE" in o.sql_clause
        for o in ops
    )
    check(
        "M1 drift emits DropConstraint", has_drop, str([type(o).__name__ for o in ops])
    )
    check("M1 drift re-adds with ON DELETE CASCADE", add_with, str(ops))

    # M1 no-drift: DB already CASCADE → no constraint ops.
    existing_fk.fk_on_delete = "CASCADE"
    ops2 = SchemaDiffer._diff_constraints(ms, dbt)
    check(
        "M1 no-drift when actions match",
        len(ops2) == 0,
        str([type(o).__name__ for o in ops2]),
    )

    # M2: DEFAULT drift → AlterColumnDefault.
    dcol = ModelColumn(
        name="status",
        type_sql="TEXT",
        nullable=False,
        is_pk=False,
        is_auto=False,
        is_unique=False,
        has_index=False,
        default_sql="'active'",
        foreign_key=None,
    )
    ms2 = ModelSchema(table="t", columns={"status": dcol}, m2m_tables=[])
    dbcol = DbColumn(
        name="status",
        type_name="text",
        type_display="TEXT",
        nullable=False,
        has_default=False,
        default_expr=None,
        is_serial=False,
        char_max_length=None,
    )
    dbt2 = DbTable(name="t", columns={"status": dbcol}, constraints=[], indexes=[])
    ops3 = SchemaDiffer._diff_columns(ms2, dbt2)
    check(
        "M2 emits AlterColumnDefault on default drift",
        any(isinstance(o, AlterColumnDefault) for o in ops3),
        str([type(o).__name__ for o in ops3]),
    )

    # M2 no false-positive: DB stores 'active'::text vs model 'active' → equal.
    dbcol.default_expr = "'active'::text"
    ops4 = SchemaDiffer._diff_columns(ms2, dbt2)
    check(
        "M2 no churn on cast-only difference",
        not any(isinstance(o, AlterColumnDefault) for o in ops4),
        str([type(o).__name__ for o in ops4]),
    )


# ── F1: fixture round-trip ──────────────────────────────────────────────────────


def test_fixture_roundtrip():
    from hyperdjango.fixtures import _deserialize_value, _serialize_value

    cases = [
        ("datetime", datetime(2026, 7, 19, 13, 30, 15)),
        ("date", date(2026, 7, 19)),
        ("time", time(13, 30, 15)),
        ("timedelta", timedelta(days=1, seconds=3661)),
        ("uuid", uuid4()),
        ("decimal", Decimal("12345.6789")),
        ("bytes", b"\x00\x01\x02binarydata\xff"),
        ("int", 42),
        ("float", 3.14159),
        ("str", "hello"),
    ]
    for ftype, original in cases:
        wire = _serialize_value(original)
        restored = _deserialize_value(wire, ftype)
        check(
            f"F1 {ftype} round-trips",
            restored == original,
            f"{original!r} -> {wire!r} -> {restored!r}",
        )

    # Guard: decimal must not survive as a float (precision loss / wrong type).
    d = Decimal("0.1")
    check(
        "F1 decimal stays Decimal (not float)",
        isinstance(_deserialize_value(_serialize_value(d), "decimal"), Decimal),
    )


# ── F4: IntegerField rejects non-integer floats ────────────────────────────────


def test_integerfield():
    from hyperdjango.forms import IntegerField

    f = IntegerField(required=False)
    check("F4 int('25') accepted", f.clean("25") == 25)
    check("F4 float 25.0 accepted", f.clean(25.0) == 25)
    try:
        f.clean(25.9)
        check("F4 float 25.9 rejected", False, "no error raised (truncated!)")
    except ValueError:
        check("F4 float 25.9 rejected", True)
    try:
        f.clean(Decimal("25.9"))
        check("F4 Decimal 25.9 rejected", False, "no error raised")
    except ValueError:
        check("F4 Decimal 25.9 rejected", True)


# ── DOCTOR/checks ──────────────────────────────────────────────────────────────


def test_doctor_checks():
    import hyperdjango.checks as checks

    orig = checks.get_setting

    def fake_get_setting(name, default=None):
        return _fake_settings.get(name, default)

    _fake_settings: dict = {}
    checks.get_setting = fake_get_setting
    try:
        # check_debug_off must use parse_bool: "true"/"yes"/"on"/"TRUE" all trip it.
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            _fake_settings["DEBUG"] = truthy
            msgs = checks.check_debug_off(None)
            check(
                f"DOCTOR debug_off flags {truthy!r}",
                any(m.level == "error" for m in msgs),
                str(msgs),
            )
        for falsy in ("0", "false", "off", ""):
            _fake_settings["DEBUG"] = falsy
            msgs = checks.check_debug_off(None)
            check(f"DOCTOR debug_off ok for {falsy!r}", len(msgs) == 0, str(msgs))

        # check_secret_key resolves via get_setting (HYPER_SECRET_KEY /
        # HYPERDJANGO_SECRET_KEY), not a bare SECRET_KEY env var.
        _fake_settings["SECRET_KEY"] = ""
        msgs = checks.check_secret_key(None)
        check(
            "DOCTOR secret_key warns when unset",
            any(m.id == "security.W001" for m in msgs),
            str(msgs),
        )
        _fake_settings["SECRET_KEY"] = "x" * 60
        msgs = checks.check_secret_key(None)
        check("DOCTOR secret_key ok when set long", len(msgs) == 0, str(msgs))
    finally:
        checks.get_setting = orig


async def main() -> bool:
    print("== A1/A2/A3 admin escalation ==")
    await test_admin_escalation()
    print("== M1/M2 migration DDL ==")
    test_migration_ddl()
    print("== F1 fixture round-trip ==")
    test_fixture_roundtrip()
    print("== F4 IntegerField ==")
    test_integerfield()
    print("== DOCTOR checks ==")
    test_doctor_checks()

    print()
    return finish()


if __name__ == "__main__":
    run_main(lambda: asyncio.run(main()))
