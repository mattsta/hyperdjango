"""Regression asserts for the three round-trip DDL-divergence fixes.

Companion to scripts/test_roundtrip_audit.py (which *demonstrates* the defects).
This file *asserts* that the two DDL generators now agree, so `hyper db verify`
produces NO spurious/destructive drift on an unchanged schema and the migration
splitter never emits invalid SQL fragments.

  M1  FK column type follows the TARGET model's PK type (BIGINT/UUID/TEXT),
      not this FK field's own `int` annotation → no destructive ALTER ... TYPE INTEGER.
  M2  A non-optional field with Field(default=None) is nullable and emits NO
      DEFAULT → no spurious SET NOT NULL and no self-violating NOT NULL DEFAULT NULL.
  M3  The `;` statement splitter honors single-quoted string literals spanning
      multiple lines (incl. '' escapes) → correct statement count.

DB-free. Run:  PYTHONPATH=. uv run python scripts/test_roundtrip_fixes.py
"""

# hyper-test: unit

import tempfile
from pathlib import Path

from hyperdjango.migrations import (
    CreateTable,
    DbColumn,
    DbConstraint,
    DbTable,
    MigrationFileManager,
    ModelExtractor,
    SchemaDiffer,
    SchemaSnapshot,
)
from hyperdjango.models import Field, Model, generate_ddl_for_model
from hyperdjango.testkit import check, finish, run_main


def _fk_line(model_cls, col) -> str:
    return next(
        l.strip() for l in generate_ddl_for_model(model_cls)[0].splitlines() if col in l
    )


# ─────────────────────────── M1: FK column type parity ──────────────────────


class BigParent(Model):
    class Meta:
        table = "rf_big_parent"

    id: int = Field(primary_key=True, auto=True, big=True)  # BIGSERIAL → int8


class UuidParent(Model):
    class Meta:
        table = "rf_uuid_parent"

    id: str = Field(primary_key=True, db_type="UUID")


class TextParent(Model):
    class Meta:
        table = "rf_text_parent"

    code: str = Field(primary_key=True)  # TEXT PK


class ChildBig(Model):
    class Meta:
        table = "rf_child_big"

    id: int = Field(primary_key=True, auto=True)
    parent_id: int = Field(foreign_key=BigParent)


class ChildUuid(Model):
    class Meta:
        table = "rf_child_uuid"

    id: int = Field(primary_key=True, auto=True)
    parent_id: str = Field(foreign_key=UuidParent)


class ChildText(Model):
    class Meta:
        table = "rf_child_text"

    id: int = Field(primary_key=True, auto=True)
    parent_id: str = Field(foreign_key=TextParent)


def _child_snapshot(
    parent_tbl, child_tbl, pk_udt, pk_disp, fk_udt, fk_disp
) -> SchemaSnapshot:
    """Snapshot of a setup-built parent+child (unchanged schema)."""
    return SchemaSnapshot(
        tables={
            parent_tbl: DbTable(
                name=parent_tbl,
                columns={
                    ("id" if pk_disp != "TEXT" else "code"): DbColumn(
                        ("id" if pk_disp != "TEXT" else "code"),
                        pk_udt,
                        pk_disp,
                        False,
                        pk_disp != "UUID" and pk_disp != "TEXT",
                        "nextval('x'::regclass)"
                        if pk_disp not in ("UUID", "TEXT")
                        else None,
                        pk_disp not in ("UUID", "TEXT"),
                        None,
                    )
                },
                constraints=[
                    DbConstraint("pk", "p", ["id" if pk_disp != "TEXT" else "code"])
                ],
            ),
            child_tbl: DbTable(
                name=child_tbl,
                columns={
                    "id": DbColumn(
                        "id",
                        "int4",
                        "INTEGER",
                        False,
                        True,
                        "nextval('y'::regclass)",
                        True,
                        None,
                    ),
                    "parent_id": DbColumn(
                        "parent_id", fk_udt, fk_disp, False, False, None, False, None
                    ),
                },
                constraints=[
                    DbConstraint("pk", "p", ["id"]),
                    DbConstraint(
                        "fk",
                        "f",
                        ["parent_id"],
                        parent_tbl,
                        ["id" if pk_disp != "TEXT" else "code"],
                    ),
                ],
            ),
        }
    )


_CASES = [
    (BigParent, ChildBig, "int8", "BIGINT", "int8", "BIGINT"),
    (UuidParent, ChildUuid, "uuid", "UUID", "uuid", "UUID"),
    (TextParent, ChildText, "text", "TEXT", "text", "TEXT"),
]


def test_m1() -> None:
    print("M1 — FK column type follows the target PK type (no destructive drift)")

    for child, expected in (
        (ChildBig, "BIGINT"),
        (ChildUuid, "UUID"),
        (ChildText, "TEXT"),
    ):
        ext = ModelExtractor.extract(child).columns["parent_id"].type_sql
        ddl_has = expected in _fk_line(child, "parent_id")
        check(
            f"{child.__name__}.parent_id: extractor == generate_ddl {expected}",
            ext == expected and ddl_has,
            f"extractor={ext!r} ddl_has={ddl_has}",
        )

    for parent, child, pk_udt, pk_disp, fk_udt, fk_disp in _CASES:
        db = _child_snapshot(
            parent._meta.table, child._meta.table, pk_udt, pk_disp, fk_udt, fk_disp
        )
        ops = SchemaDiffer.diff(
            [ModelExtractor.extract(parent), ModelExtractor.extract(child)], db
        )
        up = [o.up_sql() for o in ops]
        check(
            f"{child.__name__}: verify drift on unchanged schema is clean",
            up == [],
            f"{up}",
        )


# ─────────────────────── M2: non-optional str with default=None ─────────────


class NoneDefault(Model):
    class Meta:
        table = "rf_none_default"

    id: int = Field(primary_key=True, auto=True)
    note: str = Field(default=None)


def test_m2() -> None:
    print("\nM2 — non-optional field with default=None: nullable, no DEFAULT NULL")

    note_col = ModelExtractor.extract(NoneDefault).columns["note"]
    check(
        "note is nullable (matches generate_ddl suppressing NOT NULL)",
        note_col.nullable is True,
        f"nullable={note_col.nullable}",
    )
    check(
        "note has NO column DEFAULT (no spurious DEFAULT NULL)",
        note_col.default_sql is None,
        f"default_sql={note_col.default_sql!r}",
    )

    # The CreateTable render must never be the self-violating `NOT NULL DEFAULT NULL`.
    create_sql = CreateTable(
        table="rf_none_default",
        columns=list(ModelExtractor.extract(NoneDefault).columns.values()),
    ).up_sql()
    note_line = next(l for l in create_sql.splitlines() if '"note"' in l)
    check(
        "CreateTable note col has no NOT NULL",
        "NOT NULL" not in note_line,
        f"{note_line.strip()!r}",
    )
    check(
        "CreateTable note col has no DEFAULT NULL",
        "DEFAULT NULL" not in note_line,
        f"{note_line.strip()!r}",
    )

    # And no drift vs a DB where `note` is a plain nullable TEXT column.
    db2 = SchemaSnapshot(
        tables={
            "rf_none_default": DbTable(
                name="rf_none_default",
                columns={
                    "id": DbColumn(
                        "id",
                        "int4",
                        "INTEGER",
                        False,
                        True,
                        "nextval('z'::regclass)",
                        True,
                        None,
                    ),
                    "note": DbColumn(
                        "note", "text", "TEXT", True, False, None, False, None
                    ),
                },
                constraints=[DbConstraint("pk", "p", ["id"])],
            ),
        }
    )
    up2 = [
        o.up_sql()
        for o in SchemaDiffer.diff([ModelExtractor.extract(NoneDefault)], db2)
    ]
    check(
        "NoneDefault: verify drift on unchanged schema is clean",
        up2 == [],
        f"{up2}",
    )


# ─────────────────── M3: splitter honors multi-line string literals ─────────


def _parse(text: str) -> list[str]:
    mf = MigrationFileManager(tempfile.mkdtemp())
    Path(mf.dir).mkdir(exist_ok=True)
    f = Path(mf.dir) / "0001_x.sql"
    f.write_text(text)
    up, _ = mf.parse_migration(f)
    return up


def test_m3() -> None:
    print("\nM3 — SQL splitter: ';' inside a multi-line single-quoted string")

    up = _parse(
        "-- UP\n"
        "INSERT INTO t (body) VALUES ('ends with semicolon;\n"
        "and continues');\n"
        "INSERT INTO t (body) VALUES ('row2');\n\n-- DOWN\nDELETE FROM t;\n"
    )
    check(
        "multi-line string literal → 2 UP statements",
        len(up) == 2,
        f"got {len(up)}",
    )
    check(
        "the split preserves the whole multi-line string statement intact",
        up[0] == "INSERT INTO t (body) VALUES ('ends with semicolon;\nand continues');",
        f"got {up[0]!r}",
    )

    # Escaped '' quotes inside a multi-line literal must not desync the parser.
    up_esc = _parse(
        "-- UP\n"
        "INSERT INTO t (body) VALUES ('it''s a semicolon; still\n"
        "going ''strong''');\n"
        "INSERT INTO t (body) VALUES ('row2');\n"
    )
    check(
        "'' escapes in a multi-line literal → 2 UP statements",
        len(up_esc) == 2,
        f"got {len(up_esc)}",
    )

    # A trailing inline comment AFTER a normal statement still terminates it.
    up_cmt = _parse(
        "-- UP\n"
        "INSERT INTO t (body) VALUES ('plain'); -- a note\n"
        "INSERT INTO t (body) VALUES ('row2');\n"
    )
    check(
        "inline trailing comment still terminates → 2 statements",
        len(up_cmt) == 2,
        f"got {len(up_cmt)}",
    )


# ────────────────────────────────── result ─────────────────────────────────


def main() -> bool:
    test_m1()
    test_m2()
    test_m3()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
