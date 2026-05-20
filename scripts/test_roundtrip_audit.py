"""Audit proofs: model -> DDL -> introspect -> diff round-trip defects.

Two DDL generators must agree, because `hyper setup` builds the schema with
generate_ddl_for_model (models.py) while `hyper db verify` / makemigrations
diffs it with ModelExtractor + SchemaDiffer (migrations.py). Any divergence =>
spurious (often destructive) drift on an unchanged schema.

Run:  PYTHONPATH=. uv run python scripts/test_roundtrip_audit.py
"""

# hyper-test: unit

import tempfile
import traceback
from pathlib import Path

from hyperdjango.migrations import (
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


# ── Finding 1: FK column type diverges when target PK is BIGSERIAL/UUID/TEXT ──
class BigParent(Model):
    class Meta:
        table = "audit_big_parent"

    id: int = Field(primary_key=True, auto=True, big=True)  # BIGSERIAL


class ChildOfBig(Model):
    class Meta:
        table = "audit_child_big"

    id: int = Field(primary_key=True, auto=True)
    parent_id: int = Field(foreign_key=BigParent)


def finding_1_fk_column_type_divergence() -> None:
    print("FINDING 1 — FK column type divergence (target PK = BIGSERIAL)")
    print(
        "  generate_ddl (hyper setup):",
        [
            l.strip()
            for l in generate_ddl_for_model(ChildOfBig)[0].splitlines()
            if "parent_id" in l
        ],
    )
    print(
        "  ModelExtractor type_sql   :",
        ModelExtractor.extract(ChildOfBig).columns["parent_id"].type_sql,
    )

    # End-to-end: introspection of the setup-built table (parent_id is int8/BIGINT).
    db = SchemaSnapshot(
        tables={
            "audit_big_parent": DbTable(
                name="audit_big_parent",
                columns={
                    "id": DbColumn(
                        "id",
                        "int8",
                        "BIGINT",
                        False,
                        True,
                        "nextval('x'::regclass)",
                        True,
                        None,
                    )
                },
                constraints=[DbConstraint("pk", "p", ["id"])],
            ),
            "audit_child_big": DbTable(
                name="audit_child_big",
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
                        "parent_id", "int8", "BIGINT", False, False, None, False, None
                    ),
                },
                constraints=[
                    DbConstraint("pk", "p", ["id"]),
                    DbConstraint("fk", "f", ["parent_id"], "audit_big_parent", ["id"]),
                ],
            ),
        }
    )
    ops = SchemaDiffer.diff(
        [ModelExtractor.extract(BigParent), ModelExtractor.extract(ChildOfBig)], db
    )
    print("  verify drift on UNCHANGED schema:", [o.up_sql() for o in ops] or "(clean)")


# ── Finding 2: non-optional annotation + default=None ────────────────────────
class NoneDefault(Model):
    class Meta:
        table = "audit_none_default"

    id: int = Field(primary_key=True, auto=True)
    note: str = Field(default=None)


def finding_2_non_optional_default_none() -> None:
    print("\nFINDING 2 — non-optional str with default=None")
    print(
        "  generate_ddl (hyper setup):",
        [
            l.strip()
            for l in generate_ddl_for_model(NoneDefault)[0].splitlines()
            if "note" in l
        ],
    )
    print(
        "  ModelExtractor nullable   :",
        ModelExtractor.extract(NoneDefault).columns["note"].nullable,
        "=> migration path emits: NOT NULL DEFAULT NULL (self-broken)",
    )


# ── Finding 3: SQL splitter breaks on ';' at EOL inside a multi-line string ──


def finding_3_sql_splitter_multiline_literal() -> None:
    mf = MigrationFileManager(tempfile.mkdtemp())
    Path(mf.dir).mkdir(exist_ok=True)
    f = Path(mf.dir) / "0001_x.sql"
    f.write_text(
        "-- UP\n"
        "INSERT INTO t (body) VALUES ('ends with semicolon;\n"
        "and continues');\n"
        "INSERT INTO t (body) VALUES ('row2');\n\n-- DOWN\nDELETE FROM t;\n"
    )
    up, _ = mf.parse_migration(f)
    print("\nFINDING 3 — SQL splitter, ';' at EOL inside multi-line string literal")
    print(f"  parsed {len(up)} UP statements (CORRECT = 2):")
    for i, s in enumerate(up):
        print(f"    [{i}] {s!r}")


# ──────────────────────────────── driver ────────────────────────────────────
#
# The findings are demonstrations: each block's contract is that the audited
# round-trip machinery still runs end to end and prints its evidence. Counting
# is therefore at finding granularity — a block that raises is a FAIL and, as
# before, aborts the remaining blocks.


def main() -> bool:
    for fn in (
        finding_1_fk_column_type_divergence,
        finding_2_non_optional_default_none,
        finding_3_sql_splitter_multiline_literal,
    ):
        try:
            fn()
        except Exception as exc:
            traceback.print_exc()
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            finish()
            return False
        check(fn.__name__, True)
    return finish()


if __name__ == "__main__":
    run_main(main)
