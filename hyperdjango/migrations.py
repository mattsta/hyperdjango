"""
HyperUltimateMigrationSystem — schema migration engine.

Best-of-all-worlds migration framework combining Django's autodetection,
Alembic's branching flexibility, and modern innovations:

- Live DB introspection (diff models vs ACTUAL database schema)
- Schema snapshots (periodic checkpoints for fast state reconstruction)
- Mandatory reversibility (every operation has forward + reverse SQL)
- Post-apply verification (introspect after apply to confirm DDL succeeded)
- Deployment safety analysis (flag table locks, suggest CONCURRENTLY)
- Dry-run with real SQL (show exact DDL before applying)
- Schema version pinning (startup check that DB >= app requirement)

Usage:
    hyper makemigrations     # Diff models vs live DB, generate migration
    hyper migrate            # Apply pending migrations
    hyper showmigrations     # List applied/pending migrations
    hyper db snapshot        # Save current schema as checkpoint
    hyper db verify          # Verify models match live DB
    hyper db drift           # Detect schema drift from expected state
"""

import contextlib
import hashlib
import json as _stdlib_json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from hyperdjango.logging import logger
from hyperdjango.native import fast_json_loads
from hyperdjango.sqlident import quote_identifier, validate_type

# ─── Data Structures ───────────────────────────────────────────────────────────


@dataclass
class DbColumn:
    """Column as introspected from PostgreSQL system catalogs."""

    name: str
    type_name: str  # e.g. "int4", "varchar", "text", "timestamptz"
    type_display: str  # normalized display: "INTEGER", "TEXT", etc.
    nullable: bool
    has_default: bool
    default_expr: str | None  # e.g. "nextval('users_id_seq'::regclass)"
    is_serial: bool  # True if default is nextval(...)
    char_max_length: int | None  # from information_schema for varchar


@dataclass
class DbConstraint:
    """Constraint as introspected from PostgreSQL."""

    name: str
    type: str  # 'p' (PK), 'f' (FK), 'u' (UNIQUE), 'c' (CHECK)
    columns: list[str]
    fk_table: str | None = None
    fk_columns: list[str] | None = None
    # Normalized FK ON DELETE action ("CASCADE", "SET NULL", "RESTRICT",
    # "SET DEFAULT") or None for NO ACTION. FK constraints only.
    fk_on_delete: str | None = None


@dataclass
class DbIndex:
    """Index as introspected from PostgreSQL (non-constraint indexes)."""

    name: str
    columns: list[str]
    unique: bool
    definition: str | None = None  # full CREATE INDEX statement


@dataclass
class DbTable:
    """Complete table as introspected from PostgreSQL."""

    name: str
    columns: dict[str, DbColumn] = field(default_factory=dict)
    constraints: list[DbConstraint] = field(default_factory=list)
    indexes: list[DbIndex] = field(default_factory=list)

    def get_pk_columns(self) -> list[str]:
        for con in self.constraints:
            if con.type == "p":
                return con.columns
        return []

    def get_fk_constraints(self) -> list[DbConstraint]:
        return [c for c in self.constraints if c.type == "f"]

    def get_unique_constraints(self) -> list[DbConstraint]:
        return [c for c in self.constraints if c.type == "u"]


@dataclass
class ModelColumn:
    """Column derived from a Model class definition."""

    name: str
    type_sql: str  # "INTEGER", "TEXT", "TIMESTAMPTZ", etc.
    nullable: bool
    is_pk: bool
    is_auto: bool  # SERIAL
    is_unique: bool
    has_index: bool
    default_sql: str | None  # SQL literal default
    foreign_key: str | None  # target table name
    on_delete: str | None = None  # FK action: "CASCADE", "SET NULL", "RESTRICT", ...


@dataclass
class ModelSchema:
    """Schema derived from a Model class."""

    table: str
    columns: dict[str, ModelColumn]
    m2m_tables: list[
        dict[str, str]
    ]  # [{junction_table, source_col, target_col, source_table, target_table}]
    _model: type | None = None  # Reference to model class for metadata access
    # Meta.unique_together — composite UNIQUE constraints as column-name tuples.
    unique_together: list[tuple[str, ...]] = field(default_factory=list)
    # Meta.indexes — declarative composite/partial/GIN/expression Index objects.
    meta_indexes: list = field(default_factory=list)


@dataclass
class SchemaSnapshot:
    """Complete database schema at a point in time."""

    tables: dict[str, DbTable]
    migration_id: str | None = None
    timestamp: str | None = None
    checksum: str | None = None

    def to_dict(self) -> dict[str, str | dict | None]:
        result = {
            "migration_id": self.migration_id,
            "timestamp": self.timestamp,
            "checksum": self.checksum,
            "tables": {},
        }
        for tname, table in self.tables.items():
            result["tables"][tname] = {
                "columns": {
                    cname: {
                        "type_name": col.type_name,
                        "type_display": col.type_display,
                        "nullable": col.nullable,
                        "has_default": col.has_default,
                        "default_expr": col.default_expr,
                        "is_serial": col.is_serial,
                        "char_max_length": col.char_max_length,
                    }
                    for cname, col in table.columns.items()
                },
                "constraints": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "columns": c.columns,
                        "fk_table": c.fk_table,
                        "fk_columns": c.fk_columns,
                    }
                    for c in table.constraints
                ],
                "indexes": [
                    {"name": i.name, "columns": i.columns, "unique": i.unique}
                    for i in table.indexes
                ],
            }
        return result

    @classmethod
    def from_dict(cls, data: dict) -> SchemaSnapshot:
        tables = {}
        for tname, tdata in data.get("tables", {}).items():
            columns = {}
            for cname, cdata in tdata.get("columns", {}).items():
                columns[cname] = DbColumn(
                    name=cname,
                    type_name=cdata["type_name"],
                    type_display=cdata["type_display"],
                    nullable=cdata["nullable"],
                    has_default=cdata["has_default"],
                    default_expr=cdata.get("default_expr"),
                    is_serial=cdata.get("is_serial", False),
                    char_max_length=cdata.get("char_max_length"),
                )
            constraints = [
                DbConstraint(
                    name=c["name"],
                    type=c["type"],
                    columns=c["columns"],
                    fk_table=c.get("fk_table"),
                    fk_columns=c.get("fk_columns"),
                )
                for c in tdata.get("constraints", [])
            ]
            indexes = [
                DbIndex(name=i["name"], columns=i["columns"], unique=i["unique"])
                for i in tdata.get("indexes", [])
            ]
            tables[tname] = DbTable(
                name=tname, columns=columns, constraints=constraints, indexes=indexes
            )
        return cls(
            tables=tables,
            migration_id=data.get("migration_id"),
            timestamp=data.get("timestamp"),
            checksum=data.get("checksum"),
        )

    def compute_checksum(self) -> str:
        """Deterministic hash of schema state for drift detection."""
        data = _stdlib_json.dumps(self.to_dict(), sort_keys=True)
        self.checksum = hashlib.sha256(data.encode()).hexdigest()[:16]
        return self.checksum


# ─── Operations ────────────────────────────────────────────────────────────────


_CONFDELTYPE_TO_ACTION = {
    "a": None,  # NO ACTION (default)
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _normalize_on_delete(action: str | None) -> str | None:
    """Normalize an ON DELETE action for drift comparison.

    Maps ``None`` and ``"NO ACTION"`` (the PostgreSQL default) to ``None`` so a
    model that omits ``on_delete`` compares equal to a DB FK with the default
    action. Everything else is upper-cased and whitespace-collapsed.
    """
    if action is None:
        return None
    norm = " ".join(action.upper().split())
    if norm in ("", "NO ACTION"):
        return None
    return norm


def _normalize_default(expr: str | None) -> str | None:
    """Normalize a column DEFAULT expression for drift comparison.

    The model emits a bare SQL literal (``'foo'``, ``5``, ``TRUE``) while
    PostgreSQL introspection returns the stored expression with an explicit
    type cast (``'foo'::text``, ``true``). To compare the two without churn:

    - ``None``/empty → ``None`` (no default).
    - A trailing ``::type`` cast is stripped (``'foo'::character varying`` →
      ``'foo'``).
    - The keywords ``TRUE``/``FALSE``/``NULL`` are lower-cased so casing can't
      cause spurious drift; string-literal case is otherwise preserved.
    """
    if expr is None:
        return None
    s = expr.strip()
    if s == "":
        return None
    # Strip a trailing type cast (possibly repeated), leaving the value.
    while True:
        idx = s.rfind("::")
        if idx == -1:
            break
        tail = s[idx + 2 :].strip()
        if tail and all(c.isalnum() or c in ' _[](),"' for c in tail):
            s = s[:idx].strip()
        else:
            break
    if s.lower() in ("true", "false", "null"):
        return s.lower()
    return s


def _qi(identifier: str) -> str:
    """Validate + double-quote a PostgreSQL identifier (table/column) for DDL.

    Delegates to the one SQL-identifier authority (``sqlident``) so migrations
    validate identifiers exactly like the query layer — rejecting control chars,
    over-length (>63), and non-identifier characters — instead of blindly quoting
    whatever they are handed. Reserved words (user, order, group, …) stay usable
    because the result is double-quoted.
    """
    return quote_identifier(identifier, kind="column", source="migration")


def _open_dollar_tag(text: str) -> str | None:
    """Return the currently-open dollar-quote tag in ``text``, or None.

    Scans for ``$tag$ ... $tag$`` (and bare ``$$ ... $$``) pairs and reports
    whether the text ends *inside* an unclosed dollar-quoted string. Used by
    the migration statement splitter so a ``;`` inside a function/DO body
    (``CREATE FUNCTION ... $$ ... ; ... $$;``) is not mistaken for the end of
    the statement.
    """
    i, n = 0, len(text)
    tag: str | None = None
    while i < n:
        if tag is None:
            if text[i] == "$":
                j = text.find("$", i + 1)
                if j != -1:
                    inner = text[i + 1 : j]
                    # $$ (empty) or $ident$ — a valid dollar-quote tag.
                    if inner == "" or inner.isidentifier():
                        tag = text[i : j + 1]
                        i = j + 1
                        continue
            i += 1
        else:
            idx = text.find(tag, i)
            if idx == -1:
                return tag  # unclosed — still inside the dollar body
            i = idx + len(tag)
            tag = None
    return tag


def _open_single_quote(text: str) -> bool:
    """Return True if ``text`` ends INSIDE an unclosed single-quoted string literal.

    Mirrors ``_open_dollar_tag`` but for apostrophe-delimited literals, so the
    migration splitter does not treat a ``;`` inside a multi-line ``'...;\\n...'``
    string as a statement terminator. ``''`` is an escaped quote (stays inside the
    string). Dollar-quoted bodies and ``--`` line comments are skipped so their
    apostrophes cannot corrupt the tracked state.
    """
    i, n = 0, len(text)
    in_str = False
    dollar_tag: str | None = None
    while i < n:
        ch = text[i]
        if dollar_tag is not None:
            # Inside a $tag$ body: scan to its close; apostrophes here are body text.
            idx = text.find(dollar_tag, i)
            if idx == -1:
                return False  # ends inside a dollar body, not a single-quote string
            i = idx + len(dollar_tag)
            dollar_tag = None
            continue
        if in_str:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2  # escaped '' — still inside the string
                    continue
                in_str = False
            i += 1
            continue
        # Neither inside a string nor a dollar body.
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == "$":
            j = text.find("$", i + 1)
            if j != -1:
                inner = text[i + 1 : j]
                if inner == "" or inner.isidentifier():
                    dollar_tag = text[i : j + 1]
                    i = j + 1
                    continue
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            nl = text.find("\n", i)
            if nl == -1:
                return False  # `-- comment` runs to EOF; not inside a string
            i = nl + 1
            continue
        i += 1
    return in_str


def _line_code_before_comment(line: str, in_str: bool = False) -> str:
    """Return ``line`` with a trailing ``-- ...`` line comment removed.

    Only a ``--`` that is NOT inside a single-quoted string literal starts a
    comment (dollar-quoted bodies are handled by the caller before this runs).
    Used so a statement terminator is still recognized when a generated line
    ends ``INSERT ...; -- note`` — without this the ``;`` is missed and the
    statement wrongly merges with the next one.

    ``in_str`` seeds the scanner with whether this line BEGINS inside an open
    single-quoted string (carried across lines by the caller via
    ``_open_single_quote``), so a ``--`` inside a multi-line string literal is
    not mistaken for a comment.
    """
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and line[i + 1] == "'":
                    i += 2  # escaped '' inside the string
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "-" and i + 1 < n and line[i + 1] == "-":
            return line[:i]
        i += 1
    return line


def _referenced_pk_column(target_table: str) -> str:
    """Resolve the actual PK column of an FK target table.

    A foreign key should reference the target's real primary key, not a
    hard-coded ``id`` — models may declare a differently-named PK (e.g.
    ``uuid``, ``code``). Falls back to ``"id"`` when the target model can't
    be resolved (unregistered table, composite PK, or lookup failure).
    """
    try:
        from hyperdjango.query import _model_registry

        model = _model_registry.get(target_table)
        if model is not None:
            pk = model._meta.pk_field
            if pk:
                return pk
    # blind-except: best-effort FK PK-column probe; an unregistered target or any registry/meta lookup failure falls back to the conventional "id" column.
    except Exception:
        pass
    return "id"


@dataclass
class Operation:
    """Base migration operation — every operation MUST produce forward and reverse SQL."""

    def up_sql(self) -> str:
        raise NotImplementedError("Subclass must implement this method")

    def down_sql(self) -> str:
        raise NotImplementedError("Subclass must implement this method")

    def safety_warnings(self, table_row_count: int | None = None) -> list[str]:
        """Return deployment safety warnings for this operation."""
        return []

    def description(self) -> str:
        return self.__class__.__name__


@dataclass
class CreateTable(Operation):
    table: str
    columns: list[ModelColumn] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # raw SQL constraint clauses

    def up_sql(self) -> str:
        parts = []
        pk_columns = [col for col in self.columns if col.is_pk]
        is_composite_pk = len(pk_columns) > 1

        for col in self.columns:
            col_sql = f"  {_qi(col.name)} "
            if col.is_auto:
                col_sql += "SERIAL" if col.type_sql == "INTEGER" else "BIGSERIAL"
            else:
                col_sql += col.type_sql
            # For composite PKs, use a table-level PRIMARY KEY constraint instead
            if col.is_pk and not is_composite_pk:
                col_sql += " PRIMARY KEY"
            elif not col.nullable:
                col_sql += " NOT NULL"
            if col.is_unique and not col.is_pk:
                col_sql += " UNIQUE"
            if col.default_sql is not None and not col.is_auto:
                col_sql += f" DEFAULT {col.default_sql}"
            if col.foreign_key:
                ref_pk = _referenced_pk_column(col.foreign_key)
                col_sql += f" REFERENCES {_qi(col.foreign_key)}({_qi(ref_pk)})"
                if col.on_delete:
                    col_sql += f" ON DELETE {col.on_delete}"
            parts.append(col_sql)

        # Composite PK: add table-level PRIMARY KEY constraint
        if is_composite_pk:
            pk_names = ", ".join(_qi(col.name) for col in pk_columns)
            parts.append(f"  PRIMARY KEY ({pk_names})")

        for con in self.constraints:
            parts.append(f"  {con}")
        cols = ",\n".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {_qi(self.table)} (\n{cols}\n);"

    def down_sql(self) -> str:
        return f"DROP TABLE IF EXISTS {_qi(self.table)} CASCADE;"

    def description(self) -> str:
        return f"Create table {self.table}"


@dataclass
class DropTable(Operation):
    table: str
    # For reversibility, store the CREATE TABLE SQL
    create_sql: str = ""

    def up_sql(self) -> str:
        return f"DROP TABLE IF EXISTS {_qi(self.table)} CASCADE;"

    def down_sql(self) -> str:
        return self.create_sql or f"-- Cannot recreate {self.table} (no schema stored)"

    def safety_warnings(self, table_row_count=None) -> list[str]:
        warnings = ["DROP TABLE is destructive — all data will be lost"]
        if table_row_count and table_row_count > 0:
            warnings.append(f"Table has {table_row_count} rows")
        return warnings

    def description(self) -> str:
        return f"Drop table {self.table}"


@dataclass
class AddColumn(Operation):
    table: str
    column: str
    type_sql: str
    nullable: bool = True
    default_sql: str | None = None
    foreign_key: str | None = None
    on_delete: str | None = None

    def up_sql(self) -> str:
        sql = f"ALTER TABLE {_qi(self.table)} ADD COLUMN {_qi(self.column)} {self.type_sql}"
        if not self.nullable:
            if self.default_sql is not None:
                sql += f" NOT NULL DEFAULT {self.default_sql}"
            else:
                sql += " NOT NULL"
        if self.default_sql is not None and self.nullable:
            sql += f" DEFAULT {self.default_sql}"
        if self.foreign_key:
            ref_pk = _referenced_pk_column(self.foreign_key)
            sql += f" REFERENCES {_qi(self.foreign_key)}({_qi(ref_pk)})"
            if self.on_delete:
                sql += f" ON DELETE {self.on_delete}"
        return sql + ";"

    def down_sql(self) -> str:
        return (
            f"ALTER TABLE {_qi(self.table)} DROP COLUMN IF EXISTS {_qi(self.column)};"
        )

    def safety_warnings(self, table_row_count=None) -> list[str]:
        warnings = []
        if not self.nullable and self.default_sql is None:
            warnings.append(
                f"Adding NOT NULL column '{self.column}' without DEFAULT "
                f"requires table rewrite. Suggestion:\n"
                f"  1. ADD COLUMN ... NULL\n"
                f"  2. UPDATE {self.table} SET {self.column} = <value>\n"
                f"  3. ALTER COLUMN {self.column} SET NOT NULL"
            )
        if table_row_count and table_row_count > 1_000_000:
            warnings.append(
                f"Table has {table_row_count:,} rows — ALTER TABLE may lock for extended time"
            )
        return warnings

    def description(self) -> str:
        return f"Add column {self.table}.{self.column} ({self.type_sql})"


@dataclass
class DropColumn(Operation):
    table: str
    column: str
    # For reversibility
    type_sql: str = "TEXT"
    nullable: bool = True
    default_sql: str | None = None

    def up_sql(self) -> str:
        return (
            f"ALTER TABLE {_qi(self.table)} DROP COLUMN IF EXISTS {_qi(self.column)};"
        )

    def down_sql(self) -> str:
        sql = f"ALTER TABLE {_qi(self.table)} ADD COLUMN {_qi(self.column)} {self.type_sql}"
        if not self.nullable:
            sql += " NOT NULL"
        if self.default_sql:
            sql += f" DEFAULT {self.default_sql}"
        return sql + ";"

    def safety_warnings(self, table_row_count=None) -> list[str]:
        return [f"Dropping column {self.column} — data will be lost"]

    def description(self) -> str:
        return f"Drop column {self.table}.{self.column}"


@dataclass
class RenameColumn(Operation):
    """Rename a column in place — preserves the column's data.

    Emitted instead of DropColumn+AddColumn when the differ detects an
    unambiguous rename (see ``SchemaDiffer._diff_columns``). A DROP+ADD would
    both fail (the ADD of a NOT NULL column on a populated table) AND silently
    discard the old column's data; ``ALTER TABLE ... RENAME COLUMN`` keeps it.
    """

    table: str
    old_name: str
    new_name: str

    def up_sql(self) -> str:
        return (
            f"ALTER TABLE {_qi(self.table)} "
            f"RENAME COLUMN {_qi(self.old_name)} TO {_qi(self.new_name)};"
        )

    def down_sql(self) -> str:
        return (
            f"ALTER TABLE {_qi(self.table)} "
            f"RENAME COLUMN {_qi(self.new_name)} TO {_qi(self.old_name)};"
        )

    def description(self) -> str:
        return f"Rename column {self.table}.{self.old_name} to {self.new_name}"


@dataclass
class AlterColumnType(Operation):
    table: str
    column: str
    old_type: str
    new_type: str

    def up_sql(self) -> str:

        new_type = validate_type(self.new_type, source="AlterColumnType")
        return (
            f"ALTER TABLE {_qi(self.table)} "
            f"ALTER COLUMN {_qi(self.column)} TYPE {new_type} "
            f"USING {_qi(self.column)}::{new_type};"
        )

    def down_sql(self) -> str:

        old_type = validate_type(self.old_type, source="AlterColumnType")
        return (
            f"ALTER TABLE {_qi(self.table)} "
            f"ALTER COLUMN {_qi(self.column)} TYPE {old_type} "
            f"USING {_qi(self.column)}::{old_type};"
        )

    def safety_warnings(self, table_row_count=None) -> list[str]:
        warnings = ["Changing column type requires table rewrite"]
        if table_row_count and table_row_count > 100_000:
            warnings.append(
                f"Table has {table_row_count:,} rows — consider:\n"
                f"  1. Add new column with new type\n"
                f"  2. Backfill data\n"
                f"  3. Drop old column\n"
                f"  4. Rename new column"
            )
        return warnings

    def description(self) -> str:
        return (
            f"Change {self.table}.{self.column} from {self.old_type} to {self.new_type}"
        )


@dataclass
class AlterColumnNullable(Operation):
    table: str
    column: str
    nullable: bool  # new nullable state

    def up_sql(self) -> str:
        if self.nullable:
            return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} DROP NOT NULL;"
        return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} SET NOT NULL;"

    def down_sql(self) -> str:
        if self.nullable:
            return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} SET NOT NULL;"
        return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} DROP NOT NULL;"

    def safety_warnings(self, table_row_count=None) -> list[str]:
        if not self.nullable:
            return [
                "SET NOT NULL requires scanning all rows. Ensure no NULLs exist first."
            ]
        return []

    def description(self) -> str:
        state = "nullable" if self.nullable else "NOT NULL"
        return f"Set {self.table}.{self.column} to {state}"


@dataclass
class AlterColumnDefault(Operation):
    table: str
    column: str
    new_default: str | None  # None means DROP DEFAULT
    old_default: str | None = None

    def up_sql(self) -> str:
        if self.new_default is None:
            return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} DROP DEFAULT;"
        return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} SET DEFAULT {self.new_default};"

    def down_sql(self) -> str:
        if self.old_default is None:
            return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} DROP DEFAULT;"
        return f"ALTER TABLE {_qi(self.table)} ALTER COLUMN {_qi(self.column)} SET DEFAULT {self.old_default};"

    def description(self) -> str:
        if self.new_default is None:
            return f"Drop default on {self.table}.{self.column}"
        return f"Set default on {self.table}.{self.column} to {self.new_default}"


@dataclass
class AddConstraint(Operation):
    table: str
    name: str
    sql_clause: (
        str  # e.g. "UNIQUE (email)" or "FOREIGN KEY (author_id) REFERENCES users(id)"
    )

    def up_sql(self) -> str:
        return f"ALTER TABLE {_qi(self.table)} ADD CONSTRAINT {_qi(self.name)} {self.sql_clause};"

    def down_sql(self) -> str:
        return (
            f"ALTER TABLE {_qi(self.table)} DROP CONSTRAINT IF EXISTS {_qi(self.name)};"
        )

    def description(self) -> str:
        return f"Add constraint {self.name} on {self.table}"


@dataclass
class DropConstraint(Operation):
    table: str
    name: str
    sql_clause: str = ""  # for reversibility

    def up_sql(self) -> str:
        return (
            f"ALTER TABLE {_qi(self.table)} DROP CONSTRAINT IF EXISTS {_qi(self.name)};"
        )

    def down_sql(self) -> str:
        if self.sql_clause:
            return f"ALTER TABLE {_qi(self.table)} ADD CONSTRAINT {_qi(self.name)} {self.sql_clause};"
        return f"-- Cannot recreate constraint {self.name} (definition not stored)"

    def description(self) -> str:
        return f"Drop constraint {self.name} on {self.table}"


@dataclass
class CreateIndex(Operation):
    table: str
    name: str
    columns: list[str] = field(default_factory=list)
    unique: bool = False
    concurrently: bool = False

    def up_sql(self) -> str:
        unique = "UNIQUE " if self.unique else ""
        conc = "CONCURRENTLY " if self.concurrently else ""
        cols = ", ".join(_qi(c) for c in self.columns)
        return f"CREATE {unique}INDEX {conc}{_qi(self.name)} ON {_qi(self.table)} ({cols});"

    def down_sql(self) -> str:
        return f"DROP INDEX IF EXISTS {_qi(self.name)};"

    def safety_warnings(self, table_row_count=None) -> list[str]:
        if not self.concurrently and table_row_count and table_row_count > 100_000:
            return [
                f"CREATE INDEX blocks writes on {self.table}. "
                f"Consider CREATE INDEX CONCURRENTLY."
            ]
        return []

    def description(self) -> str:
        return f"Create index {self.name} on {self.table}({', '.join(self.columns)})"


@dataclass
class DropIndex(Operation):
    name: str
    # For reversibility
    table: str = ""
    columns: list[str] = field(default_factory=list)
    unique: bool = False

    def up_sql(self) -> str:
        return f"DROP INDEX IF EXISTS {_qi(self.name)};"

    def down_sql(self) -> str:
        if self.table and self.columns:
            unique = "UNIQUE " if self.unique else ""
            cols = ", ".join(self.columns)
            return f"CREATE {unique}INDEX {self.name} ON {self.table} ({cols});"
        return f"-- Cannot recreate index {self.name} (definition not stored)"

    def description(self) -> str:
        return f"Drop index {self.name}"


@dataclass
class CreateVectorIndex(Operation):
    """Create a pgvector index (HNSW or IVFFlat) for similarity search."""

    table: str
    column: str
    index_type: str = "hnsw"  # "hnsw" or "ivfflat"
    index_ops: str = "vector_cosine_ops"
    lists: int = 100  # IVFFlat-only: number of lists
    m: int = 16  # HNSW-only: max connections per layer
    ef_construction: int = 64  # HNSW-only: construction search width

    def up_sql(self) -> str:
        name = f"idx_{self.table}_{self.column}_{self.index_type}"
        if self.index_type == "hnsw":
            return (
                f"CREATE INDEX {_qi(name)} ON {_qi(self.table)} "
                f"USING hnsw ({_qi(self.column)} {self.index_ops}) "
                f"WITH (m = {self.m}, ef_construction = {self.ef_construction});"
            )
        return (
            f"CREATE INDEX {_qi(name)} ON {_qi(self.table)} "
            f"USING ivfflat ({_qi(self.column)} {self.index_ops}) "
            f"WITH (lists = {self.lists});"
        )

    def down_sql(self) -> str:
        name = f"idx_{self.table}_{self.column}_{self.index_type}"
        return f"DROP INDEX IF EXISTS {_qi(name)};"

    def description(self) -> str:
        return f"Create {self.index_type} vector index on {self.table}.{self.column}"


@dataclass
class RunSQL(Operation):
    """Custom SQL operation — MUST provide both forward and reverse SQL."""

    forward: str
    reverse: str
    _description: str = "Run custom SQL"

    def up_sql(self) -> str:
        return self.forward

    def down_sql(self) -> str:
        return self.reverse

    def description(self) -> str:
        return self._description


@dataclass
class RunPython(Operation):
    """Data migration operation — runs Python code with DB access.

    Both forward and reverse functions are REQUIRED for reversibility.
    Functions receive (db) as argument and should use db.execute/db.query.

    Usage in migration file (Python, not SQL):
        async def forwards(db):
            rows = await db.query("SELECT id, name FROM users")
            for row in rows:
                await db.execute(
                    "UPDATE users SET slug = $1 WHERE id = $2",
                    row['name'].lower().replace(' ', '-'), row['id'],
                )

        async def backwards(db):
            await db.execute("UPDATE users SET slug = NULL")
    """

    forward_func: Any = None
    reverse_func: Any = None
    _description: str = "Run Python data migration"

    def up_sql(self) -> str:
        return f"-- {self._description} (Python code, not SQL)"

    def down_sql(self) -> str:
        return f"-- Reverse: {self._description} (Python code, not SQL)"

    async def apply(self, db):
        """Execute the forward function."""
        if self.forward_func:
            await self.forward_func(db)

    async def revert(self, db):
        """Execute the reverse function."""
        if self.reverse_func:
            await self.reverse_func(db)

    def description(self) -> str:
        return self._description


# ─── Type Mapping ──────────────────────────────────────────────────────────────

# Python type → PostgreSQL DDL type
PYTHON_TO_PG = {
    "int": "INTEGER",
    "float": "DOUBLE PRECISION",
    "str": "TEXT",
    "bool": "BOOLEAN",
    "dict": "JSONB",
    "bytes": "BYTEA",
    "datetime": "TIMESTAMPTZ",
    "date": "DATE",
    "time": "TIME",
    "timedelta": "INTERVAL",
    "uuid": "UUID",
    "UUID": "UUID",
    "Decimal": "NUMERIC",
    # pgvector — actual dimension comes from VectorField metadata
    "vector": "VECTOR",
}

# pg_catalog type name → normalized DDL type
PG_TYPE_NORMALIZE = {
    "int2": "SMALLINT",
    "int4": "INTEGER",
    "int8": "BIGINT",
    "float4": "REAL",
    "float8": "DOUBLE PRECISION",
    "bool": "BOOLEAN",
    "text": "TEXT",
    "varchar": "VARCHAR",
    "bpchar": "CHAR",
    "timestamptz": "TIMESTAMPTZ",
    "timestamp": "TIMESTAMP",
    "date": "DATE",
    "time": "TIME",
    "timetz": "TIMETZ",
    "interval": "INTERVAL",
    "uuid": "UUID",
    "bytea": "BYTEA",
    "numeric": "NUMERIC",
    "money": "MONEY",
    "json": "JSON",
    "jsonb": "JSONB",
    "inet": "INET",
    "cidr": "CIDR",
    "xml": "XML",
    "vector": "VECTOR",
}

# Types that are equivalent for diffing purposes
TYPE_EQUIVALENTS = {
    "INTEGER": {"INTEGER", "INT", "INT4"},
    "BIGINT": {"BIGINT", "INT8"},
    "SMALLINT": {"SMALLINT", "INT2"},
    "DOUBLE PRECISION": {"DOUBLE PRECISION", "FLOAT8"},
    "REAL": {"REAL", "FLOAT4"},
    "BOOLEAN": {"BOOLEAN", "BOOL"},
    "TEXT": {"TEXT"},
    "TIMESTAMPTZ": {"TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"},
    "TIMESTAMP": {"TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE"},
    "VECTOR": {"VECTOR"},
}


def _types_equivalent(type_a: str, type_b: str) -> bool:
    """Check if two SQL type strings are semantically equivalent."""
    a = type_a.upper().strip()
    b = type_b.upper().strip()
    if a == b:
        return True
    # Check VARCHAR with length
    if a.startswith("VARCHAR") and b.startswith("VARCHAR"):
        return a == b
    # A bare VECTOR (dimension unknown to one side) matches any VECTOR(n);
    # two sized vectors must agree on the dimension.
    if a.startswith("VECTOR") and b.startswith("VECTOR"):
        if a == "VECTOR" or b == "VECTOR":
            return True
        return a == b
    # Check equivalence groups
    for group in TYPE_EQUIVALENTS.values():
        if a in group and b in group:
            return True
    # SERIAL is equivalent to INTEGER with nextval default
    if (a == "SERIAL" and b == "INTEGER") or (a == "INTEGER" and b == "SERIAL"):
        return True
    return bool(
        a == "BIGSERIAL" and b == "BIGINT" or a == "BIGINT" and b == "BIGSERIAL"
    )


def _normalize_pg_type(
    type_name: str,
    char_max_length: int | None = None,
    atttypmod: int | None = None,
) -> str:
    """Normalize a pg_catalog type name to a DDL type.

    ``atttypmod`` is the raw ``pg_attribute.atttypmod`` and lets us recover a
    type modifier that pg_catalog does not expose as a distinct type name:

    - ``vector(N)`` — pgvector stores the dimension directly in atttypmod
      (no VARHDRSZ offset). Without decoding it, an ``embedding vector(8)``
      column introspects as bare ``VECTOR`` and diffs unequal against the
      model's ``VECTOR(8)`` → a spurious ``ALTER COLUMN ... TYPE vector(8)`` on
      every makemigrations plus a false ``hyper db verify`` mismatch.
    - ``numeric(p, s)`` — precision/scale are packed into atttypmod
      (``((p << 16) | s) + VARHDRSZ``). Recovering them keeps a
      ``Decimal(max_digits=10, decimal_places=2)`` column idempotent.
    """
    # pgvector: atttypmod is the dimension when a size was specified (else -1).
    if type_name == "vector":
        if atttypmod is not None and atttypmod > 0:
            return f"VECTOR({atttypmod})"
        return "VECTOR"
    # numeric: precision/scale packed into atttypmod (minus the 4-byte VARHDRSZ).
    if type_name == "numeric" and atttypmod is not None and atttypmod > 0:
        tm = atttypmod - 4
        precision = (tm >> 16) & 0xFFFF
        scale = tm & 0xFFFF
        if scale:
            return f"NUMERIC({precision}, {scale})"
        return f"NUMERIC({precision})"
    norm = PG_TYPE_NORMALIZE.get(type_name, type_name.upper())
    if norm == "VARCHAR" and char_max_length:
        return f"VARCHAR({char_max_length})"
    if norm == "CHAR" and char_max_length:
        return f"CHAR({char_max_length})"
    return norm


# ─── Database Introspector ─────────────────────────────────────────────────────


class DatabaseIntrospector:
    """Introspect PostgreSQL schema from system catalogs via pg.zig."""

    @staticmethod
    async def introspect(
        db, schema: str = "public", include_views: bool = False
    ) -> SchemaSnapshot:
        """Introspect full database schema, return SchemaSnapshot.

        Args:
            db: Database connection
            schema: PostgreSQL schema name (default: "public")
            include_views: If True, also introspect views and materialized views
        """
        tables = {}

        # 1. Get all tables (and optionally views)
        relkinds = "'r'"  # ordinary tables
        if include_views:
            relkinds = "'r', 'v', 'm'"  # + views + materialized views
        table_rows = await db.query(
            "SELECT c.relname "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind IN ({relkinds}) AND n.nspname = $1 "
            "AND c.relname NOT LIKE 'pg_%' "
            "ORDER BY c.relname",
            schema,
        )

        for trow in table_rows:
            table_name = trow["relname"]
            table = DbTable(name=table_name)

            # 2. Get columns + build attnum→name map in one query
            col_rows = await db.query(
                "SELECT a.attname, t.typname, a.attnotnull, a.atthasdef, "
                "pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr, "
                "a.attnum, a.atttypmod, "
                "CASE WHEN t.typname = 'varchar' OR t.typname = 'bpchar' "
                "THEN CASE WHEN a.atttypmod > 0 THEN a.atttypmod - 4 ELSE NULL END "
                "ELSE NULL END AS char_max_length "
                "FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_type t ON a.atttypid = t.oid "
                "LEFT JOIN pg_catalog.pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = ( "
                "  SELECT c.oid FROM pg_catalog.pg_class c "
                "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "  WHERE c.relname = $1 AND n.nspname = $2 "
                ") "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum",
                table_name,
                schema,
            )

            # Build attnum→name map for constraint/index resolution
            attnum_map: dict[int, str] = {}
            for crow in col_rows:
                attnum_map[crow["attnum"]] = crow["attname"]
                default_expr = crow.get("default_expr")
                is_serial = bool(
                    default_expr
                    and isinstance(default_expr, str)
                    and default_expr.startswith("nextval(")
                )
                char_max_length = crow.get("char_max_length")
                type_display = _normalize_pg_type(
                    crow["typname"], char_max_length, crow.get("atttypmod")
                )

                table.columns[crow["attname"]] = DbColumn(
                    name=crow["attname"],
                    type_name=crow["typname"],
                    type_display=type_display,
                    nullable=not crow["attnotnull"],
                    has_default=crow["atthasdef"],
                    default_expr=default_expr,
                    is_serial=is_serial,
                    char_max_length=char_max_length,
                )

            # 3. Get constraints — resolve attnum→name using map (no extra queries)
            con_rows = await db.query(
                "SELECT con.conname, con.contype, "
                "con.conkey, con.confkey, con.confdeltype, "
                "frel.relname AS fk_table "
                "FROM pg_catalog.pg_constraint con "
                "JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
                "LEFT JOIN pg_catalog.pg_class frel ON frel.oid = con.confrelid "
                "WHERE rel.relname = $1 AND nsp.nspname = $2",
                table_name,
                schema,
            )

            # Build FK target attnum maps lazily (one query per FK target table)
            fk_attnum_cache: dict[str, dict[int, str]] = {}

            for crow in con_rows:
                conkey = crow.get("conkey") or []
                confkey = crow.get("confkey") or []

                # Resolve local columns via attnum_map
                col_names = [
                    attnum_map[a]
                    for a in (conkey if isinstance(conkey, list) else [])
                    if a in attnum_map
                ]

                # Resolve FK target columns
                fk_col_names = []
                fk_table = crow.get("fk_table")
                if fk_table and confkey:
                    if fk_table not in fk_attnum_cache:
                        fk_col_rows = await db.query(
                            "SELECT a.attnum, a.attname "
                            "FROM pg_catalog.pg_attribute a "
                            "WHERE a.attrelid = ( "
                            "  SELECT c.oid FROM pg_catalog.pg_class c "
                            "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                            "  WHERE c.relname = $1 AND n.nspname = $2 "
                            ") AND a.attnum > 0 AND NOT a.attisdropped",
                            fk_table,
                            schema,
                        )
                        fk_attnum_cache[fk_table] = {
                            r["attnum"]: r["attname"] for r in fk_col_rows
                        }
                    fk_map = fk_attnum_cache[fk_table]
                    fk_col_names = [
                        fk_map[a]
                        for a in (confkey if isinstance(confkey, list) else [])
                        if a in fk_map
                    ]

                confdeltype = crow.get("confdeltype")
                fk_on_delete = (
                    _CONFDELTYPE_TO_ACTION.get(confdeltype)
                    if crow["contype"] == "f"
                    else None
                )
                table.constraints.append(
                    DbConstraint(
                        name=crow["conname"],
                        type=crow["contype"],
                        columns=col_names,
                        fk_table=fk_table,
                        fk_columns=fk_col_names or None,
                        fk_on_delete=fk_on_delete,
                    )
                )

            # 4. Get non-constraint indexes. Resolve the column names inside SQL
            # via a text[] ARRAY subquery. `pg_index.indkey` is an ``int2vector``
            # (not a real array): the pg.zig decoder surfaces it as an empty
            # STRING, so resolving it in Python yields no columns and every
            # secondary index diffs as "missing" forever (non-idempotent). The
            # ARRAY(... unnest(indkey) ...) approach returns a proper text[] the
            # decoder handles, and preserves column order via WITH ORDINALITY.
            # Expression-index positions (attnum 0) simply don't join and are
            # dropped, which is fine — Meta.indexes are matched by name.
            idx_rows = await db.query(
                "SELECT i.relname AS index_name, ix.indisunique, "
                "ARRAY( "
                "  SELECT a.attname "
                "  FROM unnest(string_to_array(ix.indkey::text, ' ')::int[]) "
                "       WITH ORDINALITY AS k(attnum, ord) "
                "  JOIN pg_catalog.pg_attribute a "
                "    ON a.attrelid = ix.indrelid AND a.attnum = k.attnum "
                "  ORDER BY k.ord "
                ") AS col_names "
                "FROM pg_catalog.pg_index ix "
                "JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid "
                "JOIN pg_catalog.pg_class t ON t.oid = ix.indrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "WHERE t.relname = $1 AND n.nspname = $2 "
                "AND NOT ix.indisprimary "
                "AND NOT EXISTS ( "
                "  SELECT 1 FROM pg_catalog.pg_constraint c "
                "  WHERE c.conindid = ix.indexrelid "
                ")",
                table_name,
                schema,
            )

            for irow in idx_rows:
                idx_col_names = irow.get("col_names") or []
                table.indexes.append(
                    DbIndex(
                        name=irow["index_name"],
                        columns=list(idx_col_names),
                        unique=irow["indisunique"],
                    )
                )

            tables[table_name] = table

        snapshot = SchemaSnapshot(
            tables=tables,
            timestamp=datetime.now().isoformat(),
        )
        snapshot.compute_checksum()
        return snapshot


# ─── Model Schema Extractor ───────────────────────────────────────────────────


class ModelExtractor:
    """Extract schema from Model class definitions."""

    @staticmethod
    def extract(model_class) -> ModelSchema:
        """Extract ModelSchema from a Model class with _meta."""
        meta = model_class._meta
        columns = {}

        for field_name, field_meta in meta.fields.items():
            type_sql = ModelExtractor._get_type(model_class, field_name, field_meta)
            nullable = ModelExtractor._is_nullable(model_class, field_name)
            default_sql = ModelExtractor._get_default_sql(model_class, field_name)

            columns[field_name] = ModelColumn(
                name=field_name,
                type_sql=type_sql,
                nullable=nullable,
                is_pk=field_meta.primary_key,
                is_auto=field_meta.auto,
                is_unique=field_meta.unique,
                has_index=field_meta.index,
                default_sql=default_sql,
                foreign_key=field_meta.foreign_key,
                on_delete=field_meta.on_delete,
            )

        # Detect M2M fields
        m2m_tables = []
        from hyperdjango.models import ManyToManyField

        for attr_name in dir(model_class):
            attr = model_class.__dict__.get(attr_name)
            if isinstance(attr, ManyToManyField) and attr._junction_table:
                m2m_tables.append(
                    {
                        "junction_table": attr._junction_table,
                        "source_col": attr._source_col,
                        "target_col": attr._target_col,
                        "source_table": attr._source_table,
                        "target_table": attr._target_table_name,
                    }
                )

        # Meta.unique_together / Meta.indexes — composite constraints and
        # indexes live on TableMeta, not on any single field. Without carrying
        # them here they are never generated or diffed (silent schema loss).
        unique_together = [tuple(ut) for ut in meta.unique_together]
        meta_indexes = list(meta.indexes)

        return ModelSchema(
            table=meta.table,
            columns=columns,
            m2m_tables=m2m_tables,
            _model=model_class,
            unique_together=unique_together,
            meta_indexes=meta_indexes,
        )

    @staticmethod
    def _get_type(model_class, field_name, field_meta) -> str:
        """Get SQL type from Python annotation + FieldInfo."""
        # Foreign-key columns take the TARGET model's PK type, NOT this FK field's
        # own `int` annotation. Delegating to the same shared helper that
        # generate_ddl_for_model (models.py) uses keeps the two DDL generators from
        # diverging — a divergence that otherwise emits a spurious, DESTRUCTIVE
        # `ALTER COLUMN ... TYPE INTEGER` narrowing a BIGINT/UUID/TEXT FK on an
        # unchanged schema (it never converges, since setup never made it INTEGER).
        if field_meta.foreign_key:
            from hyperdjango.models import _fk_column_sql_type

            return _fk_column_sql_type(
                field_meta.foreign_key, model_class.__dict__.get(field_name)
            )

        annotation = model_class.__annotations__.get(field_name)
        if annotation is None:
            return "TEXT"

        # Strip Optional[X] / X | None down to its single non-None member.
        # Only a None-union is unwrapped — a plain generic like list[str] or
        # dict[str, int] must keep its base name (list/dict → JSONB), NOT be
        # collapsed to its first arg (str → TEXT), which was the old bug.
        # dynamic-attr: annotation is an arbitrary typing object — __origin__/__args__ exist only on typing generics (list[str], X | None), not on plain types
        origin = getattr(annotation, "__origin__", None)
        if origin is not None:
            args = getattr(
                annotation, "__args__", ()
            )  # dynamic-attr: typing generic internals, absent on plain types
            if type(None) in args:
                non_none = [a for a in args if a is not type(None)]
                if len(non_none) == 1:
                    annotation = non_none[0]

        # list[str].__name__ == "list", so a JSONB list/dict field maps correctly.
        # dynamic-attr: annotation may be a plain type (has __name__) or a typing special form (does not); fall back to str()
        type_name = getattr(annotation, "__name__", str(annotation))

        # Check FieldInfo for special field types
        field_info = model_class.__dict__.get(field_name)
        if field_info is not None:
            # An explicit db_type override wins (e.g. "BIGINT" for a 64-bit column the
            # int→INTEGER default would narrow). For an auto PK, CreateTable.up_sql maps
            # a BIGINT type_sql → BIGSERIAL, so this also yields BIGSERIAL identity PKs.
            # dynamic-attr: field_info comes from model_class.__dict__ — a FieldInfo (has db_type) or a plain Python default value (str/int/…) that does not
            explicit = getattr(field_info, "db_type", None)
            if explicit:
                return explicit
            # big=True widens an integer column to 64-bit (BIGINT → BIGSERIAL when auto).
            # dynamic-attr: field_info may be a plain default value rather than a FieldInfo — "big" is FieldInfo-only
            if getattr(field_info, "big", False) and type_name == "int":
                return "BIGINT"
            # Vector field — vector(dimensions)
            if field_info.vector_dimensions is not None:
                return f"vector({field_info.vector_dimensions})"
            # Only emit VARCHAR(N) when max_length is explicitly set.
            # Default is TEXT — faster in PostgreSQL (no length check overhead).
            # VARCHAR(N) is used when the developer intentionally wants
            # DB-level truncation/rejection of oversized values.
            if field_info.max_length:
                return f"VARCHAR({field_info.max_length})"
            # Decimal precision/scale → NUMERIC(p, s). Without this a
            # Decimal(max_digits=10, decimal_places=2) column emits an
            # unconstrained NUMERIC, silently losing the precision constraint
            # (and disagreeing with generate_ddl_for_model in models.py).
            if type_name == "Decimal" and field_info.max_digits is not None:
                if field_info.decimal_places:
                    return (
                        f"NUMERIC({field_info.max_digits}, {field_info.decimal_places})"
                    )
                return f"NUMERIC({field_info.max_digits})"

        return PYTHON_TO_PG.get(type_name, "TEXT")

    @staticmethod
    def _is_nullable(model_class, field_name) -> bool:
        """Check if a field is nullable (Optional type, or explicit default=None)."""
        annotation = model_class.__annotations__.get(field_name)

        # An explicit Field(default=None) makes the column nullable even when the
        # annotation is non-optional — mirroring generate_ddl_for_model in models.py,
        # which suppresses NOT NULL when a non-PK/non-auto FieldInfo.default is None.
        # Without this the migration path emits a spurious `SET NOT NULL` on an
        # unchanged schema, and CreateTable renders the self-violating
        # `col TEXT NOT NULL DEFAULT NULL`.
        from hyperdjango.validation.core.fields import FieldInfo

        field_info = model_class.__dict__.get(field_name)
        if (
            isinstance(field_info, FieldInfo)
            and field_info.default is None
            and not field_info.primary_key
            and not field_info.auto
        ):
            return True

        if annotation is None:
            return True

        # dynamic-attr: annotation is an arbitrary typing object — __origin__/__args__ exist only on typing generics, not on plain types
        origin = getattr(annotation, "__origin__", None)
        if origin is not None:
            args = getattr(
                annotation, "__args__", ()
            )  # dynamic-attr: typing generic internals, absent on plain types
            if type(None) in args:
                return True

        # Non-optional types default to NOT NULL unless they're the PK
        return False

    @staticmethod
    def _get_default_sql(model_class, field_name) -> str | None:
        """Get SQL literal for a field's column DEFAULT.

        A usable Python-side ``default`` (a non-missing, non-callable literal)
        wins. Otherwise fall back to a DB-side default — ``Field(db_default=...)``
        or the ``default="now()"`` rewrite that moves the Python default to a
        factory and stashes a ``DatabaseDefault`` on ``db_default`` — normalized
        exactly as ``generate_ddl_for_model`` in models.py does.

        Without this db_default fallback the two DDL generators disagree: a
        column carrying only a ``db_default`` (e.g. ``gen_random_uuid()`` /
        ``now()``) would be emitted here NOT NULL with NO DEFAULT, so an INSERT
        that omits it fails — and because the omission is self-consistent it
        never surfaces as drift.
        """
        from hyperdjango.validation.core.fields import _MISSING, FieldInfo

        field_info = model_class.__dict__.get(field_name)
        if not isinstance(field_info, FieldInfo):
            return None

        default = field_info.default
        # `default=None` yields NO column DEFAULT (exactly as _python_default_to_sql
        # in models.py returns None for None) — it makes the column nullable (see
        # _is_nullable), it does not emit a `DEFAULT NULL` that would pair with a
        # NOT NULL into self-violating DDL.
        if default is not _MISSING and default is not None and not callable(default):
            return _sql_literal(default)

        # No usable Python literal — honor a DB-side default like models.py does.
        if field_info.db_default is not None:
            from hyperdjango.models import _db_default_to_sql

            return _db_default_to_sql(field_info.db_default)
        return None

    @staticmethod
    def extract_all() -> list[ModelSchema]:
        """Extract schemas from all registered models."""
        from hyperdjango.query import _model_registry

        schemas = []
        for table_name, model_class in _model_registry.items():
            schemas.append(ModelExtractor.extract(model_class))
        return schemas


# ─── Schema Differ ─────────────────────────────────────────────────────────────


class SchemaDiffer:
    """Compare Model definitions against live database schema.

    Produces a list of Operations that transform the DB to match models.
    """

    # Tables managed by the framework itself — never suggest dropping.
    #
    # This explicit set documents the known framework tables, but it is NOT
    # the primary guard: framework model modules are lazily imported, so a
    # module that hasn't been imported is absent from the model registry.
    # If we diffed only against the registry we'd emit `DROP TABLE ... CASCADE`
    # for LIVE framework tables (security log, metering, rate-limit rules,
    # tenants, RBAC audit, object/field permissions, status events, ...) that
    # merely weren't imported this run. The `hyper_` prefix is reserved by the
    # framework, so `_is_system_table()` treats EVERY `hyper_*` table as
    # protected — auto-drop can never touch them regardless of import state.
    SYSTEM_TABLES = frozenset(
        {
            "hyper_migrations",
            "hyper_users",
            "hyper_groups",
            "hyper_permissions",
            "hyper_user_groups",
            "hyper_user_permissions",
            "hyper_group_permissions",
            "hyper_audit_log",
            "hyper_sessions",
            "hyper_security_log",
            "hyper_rate_limit_rules",
            "hyper_tenants",
            "hyper_status_events",
            "hyper_rbac_audit",
            "hyper_object_permissions",
            "hyper_permission_rules",
            "hyper_field_permissions",
        }
    )

    # All framework tables share the reserved `hyper_` prefix. Metering tables
    # (hyper_meter_*) and any future framework table are covered by the prefix
    # without needing to enumerate every name here.
    SYSTEM_TABLE_PREFIX = "hyper_"

    @classmethod
    def _is_system_table(cls, table_name: str) -> bool:
        """True if a table is framework-managed and must never be auto-dropped."""
        return table_name in cls.SYSTEM_TABLES or table_name.startswith(
            cls.SYSTEM_TABLE_PREFIX
        )

    @classmethod
    def diff(
        cls, models: list[ModelSchema], db_state: SchemaSnapshot
    ) -> list[Operation]:
        """Diff models against live DB state, return operations."""
        ops: list[Operation] = []

        model_tables = {m.table: m for m in models}

        # Collect all model-managed table names (including junction tables)
        all_model_tables = set(model_tables.keys())
        for m in models:
            for m2m in m.m2m_tables:
                all_model_tables.add(m2m["junction_table"])

        # 1. New tables (in models, not in DB) + their indexes.
        create_ops: list[CreateTable] = []
        new_table_index_ops: list[Operation] = []
        for table, model in model_tables.items():
            if table not in db_state.tables:
                create_ops.append(cls._create_table_op(model))
                # A freshly-created table has no secondary indexes yet, and the
                # column/constraint/index diff below only runs for tables ALREADY
                # in the DB. Diff the new table against an empty one so its
                # per-field indexes, pgvector ANN/HNSW indexes, and composite
                # Meta.indexes are emitted at initial deploy — otherwise a fresh
                # schema has no secondary indexes and a 2nd makemigrations
                # spuriously emits every CREATE INDEX.
                new_table_index_ops.extend(
                    cls._diff_indexes(model, DbTable(name=table))
                )

        # 2. New junction tables (M2M).
        for model in models:
            for m2m in model.m2m_tables:
                jt = m2m["junction_table"]
                if jt not in db_state.tables:
                    create_ops.append(
                        CreateTable(
                            table=jt,
                            columns=[
                                ModelColumn(
                                    name=m2m["source_col"],
                                    type_sql="INTEGER",
                                    nullable=False,
                                    is_pk=False,
                                    is_auto=False,
                                    is_unique=False,
                                    has_index=False,
                                    default_sql=None,
                                    foreign_key=m2m["source_table"],
                                ),
                                ModelColumn(
                                    name=m2m["target_col"],
                                    type_sql="INTEGER",
                                    nullable=False,
                                    is_pk=False,
                                    is_auto=False,
                                    is_unique=False,
                                    has_index=False,
                                    default_sql=None,
                                    foreign_key=m2m["target_table"],
                                ),
                            ],
                            constraints=[
                                f"PRIMARY KEY ({m2m['source_col']}, {m2m['target_col']})",
                            ],
                        )
                    )

        # Order CreateTable ops so a table is never created before a table it
        # REFERENCES via an inline FK. FK cycles (mutual FKs, forward references,
        # or a model defined before its target) are broken into deferred
        # AddConstraint ops emitted once every table exists.
        ordered_creates, deferred_fks = cls._order_creates(create_ops)
        ops.extend(ordered_creates)
        ops.extend(deferred_fks)
        ops.extend(new_table_index_ops)

        # 3. Tables in DB but not in models — suggest DROP (skip system tables).
        # Never auto-drop framework `hyper_*` tables: with lazy model imports
        # they may simply be absent from the registry this run.
        for table_name in db_state.tables:
            if table_name not in all_model_tables and not cls._is_system_table(
                table_name
            ):
                ops.append(DropTable(table=table_name))

        # 4. Column-level diffs (for tables in both models and DB)
        for table, model in model_tables.items():
            db_table = db_state.tables.get(table)
            if db_table is None:
                continue  # Already handled as new table

            ops.extend(cls._diff_columns(model, db_table))
            ops.extend(cls._diff_constraints(model, db_table))
            ops.extend(cls._diff_indexes(model, db_table))

        return ops

    @classmethod
    def _order_creates(
        cls, create_ops: list[CreateTable]
    ) -> tuple[list[CreateTable], list[Operation]]:
        """Topologically sort CreateTable ops by inline-FK dependency.

        Returns ``(ordered_creates, deferred_constraints)``. A table is emitted
        only after every table it FKs *that is also being created this run* has
        been emitted (references to pre-existing tables impose no ordering). An
        FK cycle — mutual FKs, or a forward reference to a table created later —
        is broken by stripping the offending inline ``REFERENCES`` off the
        CreateTable and re-adding it as an ``AddConstraint`` after all tables
        exist, so ``relation "..." does not exist`` can never occur.
        """
        creating = {op.table for op in create_ops}

        def unmet_deps(op: CreateTable, done: set[str]) -> set[str]:
            return {
                col.foreign_key
                for col in op.columns
                if col.foreign_key
                and col.foreign_key in creating
                and col.foreign_key != op.table  # self-FK is valid inline
                and col.foreign_key not in done
            }

        ordered: list[CreateTable] = []
        deferred: list[Operation] = []
        emitted: set[str] = set()
        remaining = list(create_ops)  # stable order for determinism

        while remaining:
            ready = [op for op in remaining if not unmet_deps(op, emitted)]
            if ready:
                for op in ready:
                    ordered.append(op)
                    emitted.add(op.table)
                remaining = [op for op in remaining if op.table not in emitted]
                continue

            # No table is ready → a cycle. Break it: take the first remaining
            # table and defer every FK still pointing at a not-yet-emitted table.
            op = remaining.pop(0)
            new_cols: list[ModelColumn] = []
            for col in op.columns:
                if (
                    col.foreign_key
                    and col.foreign_key in creating
                    and col.foreign_key != op.table
                    and col.foreign_key not in emitted
                ):
                    ref_pk = _referenced_pk_column(col.foreign_key)
                    on_del = f" ON DELETE {col.on_delete}" if col.on_delete else ""
                    deferred.append(
                        AddConstraint(
                            table=op.table,
                            name=f"fk_{op.table}_{col.name}",
                            sql_clause=(
                                f"FOREIGN KEY ({col.name}) "
                                f"REFERENCES {col.foreign_key}({ref_pk}){on_del}"
                            ),
                        )
                    )
                    new_cols.append(replace(col, foreign_key=None))
                else:
                    new_cols.append(col)
            op.columns = new_cols
            ordered.append(op)
            emitted.add(op.table)

        return ordered, deferred

    @classmethod
    def _create_table_op(cls, model: ModelSchema) -> CreateTable:
        """Build CreateTable operation from ModelSchema.

        Composite ``Meta.unique_together`` constraints are emitted inline as
        table-level ``UNIQUE (...)`` clauses so a fresh table gets them at
        creation. Per-column UNIQUE/FK are already emitted per-column by
        ``CreateTable.up_sql`` (so ``_diff_constraints`` is intentionally NOT
        run for new tables — that would double them).
        """
        constraints = [
            f"UNIQUE ({', '.join(_qi(c) for c in ut)})" for ut in model.unique_together
        ]
        return CreateTable(
            table=model.table,
            columns=list(model.columns.values()),
            constraints=constraints,
        )

    @classmethod
    def _diff_columns(cls, model: ModelSchema, db_table: DbTable) -> list[Operation]:
        """Diff columns between model and DB table."""
        ops = []

        added = {
            name: col
            for name, col in model.columns.items()
            if name not in db_table.columns
        }
        dropped = {
            name: col
            for name, col in db_table.columns.items()
            if name not in model.columns
        }

        # Rename detection: an unambiguous single add + single drop of the same
        # type is treated as a RENAME, not DROP+ADD. DROP+ADD would silently
        # discard the old column's data (and the ADD of a NOT NULL column on a
        # populated table would fail outright). We only do this for the 1:1
        # same-type case to avoid guessing wrong on unrelated churn.
        if len(added) == 1 and len(dropped) == 1:
            ((new_name, new_col),) = added.items()
            ((old_name, old_db_col),) = dropped.items()
            model_type = "INTEGER" if new_col.is_auto else new_col.type_sql
            if _types_equivalent(model_type, old_db_col.type_display):
                ops.append(
                    RenameColumn(
                        table=model.table,
                        old_name=old_name,
                        new_name=new_name,
                    )
                )
                # Align nullability if it also changed across the rename.
                if (
                    not new_col.is_pk
                    and not new_col.is_auto
                    and new_col.nullable != old_db_col.nullable
                ):
                    ops.append(
                        AlterColumnNullable(
                            table=model.table,
                            column=new_name,
                            nullable=new_col.nullable,
                        )
                    )
                # Consumed as a rename — do not also emit add/drop for them.
                added = {}
                dropped = {}

        # New columns (in model, not in DB)
        for col_name, model_col in added.items():
            ops.append(
                AddColumn(
                    table=model.table,
                    column=col_name,
                    type_sql=model_col.type_sql,
                    nullable=model_col.nullable or model_col.is_pk,
                    default_sql=model_col.default_sql,
                    foreign_key=model_col.foreign_key,
                    on_delete=model_col.on_delete,
                )
            )

        # Dropped columns (in DB, not in model) — an explicit field removal.
        for col_name, db_col in dropped.items():
            ops.append(
                DropColumn(
                    table=model.table,
                    column=col_name,
                    type_sql=db_col.type_display,
                    nullable=db_col.nullable,
                    default_sql=db_col.default_expr,
                )
            )

        # Changed columns (in both, different types/nullability)
        for col_name, model_col in model.columns.items():
            db_col = db_table.columns.get(col_name)
            if db_col is None:
                continue

            # Type change (skip SERIAL ↔ INTEGER since serial is just int + sequence)
            model_type = model_col.type_sql
            db_type = db_col.type_display
            if model_col.is_auto:
                # SERIAL columns show as INTEGER in DB with nextval default
                if db_col.is_serial:
                    continue  # Already serial, no change needed
                model_type = "INTEGER"  # Compare base type

            if not _types_equivalent(model_type, db_type):
                ops.append(
                    AlterColumnType(
                        table=model.table,
                        column=col_name,
                        old_type=db_type,
                        new_type=model_type,
                    )
                )

            # Nullable change (skip PK and auto fields)
            if not model_col.is_pk and not model_col.is_auto:
                if model_col.nullable != db_col.nullable:
                    ops.append(
                        AlterColumnNullable(
                            table=model.table,
                            column=col_name,
                            nullable=model_col.nullable,
                        )
                    )

            # Default change (skip PK and auto/serial columns, whose default is
            # the sequence nextval and must never be diffed as user default).
            if not model_col.is_pk and not model_col.is_auto:
                model_def = _normalize_default(model_col.default_sql)
                db_def = _normalize_default(db_col.default_expr)
                if model_def != db_def:
                    ops.append(
                        AlterColumnDefault(
                            table=model.table,
                            column=col_name,
                            new_default=model_col.default_sql,
                            old_default=db_col.default_expr,
                        )
                    )

        return ops

    @classmethod
    def _diff_constraints(
        cls, model: ModelSchema, db_table: DbTable
    ) -> list[Operation]:
        """Diff constraints between model and DB table."""
        ops = []

        # Check FK constraints. Only add an FK for a column that ALREADY exists
        # in the DB: a newly-added FK column is created WITH its inline
        # REFERENCES by the AddColumn in _diff_columns (and a brand-new table
        # gets it inline from CreateTable), so emitting an AddConstraint here
        # too would create a SECOND identical FK on the same column.
        existing_fks = {
            tuple(c.columns): c for c in db_table.constraints if c.type == "f"
        }
        for col_name, model_col in model.columns.items():
            if model_col.foreign_key and col_name in db_table.columns:
                col_key = (col_name,)
                ref_pk = _referenced_pk_column(model_col.foreign_key)
                on_del_norm = _normalize_on_delete(model_col.on_delete)
                on_del_clause = f" ON DELETE {on_del_norm}" if on_del_norm else ""
                constraint_name = f"fk_{model.table}_{col_name}"
                existing = existing_fks.get(col_key)
                if existing is None:
                    ops.append(
                        AddConstraint(
                            table=model.table,
                            name=constraint_name,
                            sql_clause=(
                                f"FOREIGN KEY ({col_name}) "
                                f"REFERENCES {model_col.foreign_key}({ref_pk})"
                                f"{on_del_clause}"
                            ),
                        )
                    )
                elif (
                    model_col.on_delete is not None
                    and _normalize_on_delete(existing.fk_on_delete) != on_del_norm
                ):
                    # ON DELETE action drift, but only when the model EXPLICITLY
                    # specifies on_delete — an unspecified (None) model FK does not
                    # manage the action, so it must not churn a DB FK that has one
                    # (e.g. a DB CASCADE vs a model that simply omits on_delete).
                    # PostgreSQL has no ALTER for an FK action, so drop + re-add.
                    ops.append(DropConstraint(table=model.table, name=existing.name))
                    ops.append(
                        AddConstraint(
                            table=model.table,
                            name=existing.name,
                            sql_clause=(
                                f"FOREIGN KEY ({col_name}) "
                                f"REFERENCES {model_col.foreign_key}({ref_pk})"
                                f"{on_del_clause}"
                            ),
                        )
                    )

        # Check UNIQUE constraints (not on PK columns)
        existing_uniques = {
            tuple(c.columns): c for c in db_table.constraints if c.type == "u"
        }
        for col_name, model_col in model.columns.items():
            if model_col.is_unique and not model_col.is_pk:
                col_key = (col_name,)
                if col_key not in existing_uniques:
                    constraint_name = f"uq_{model.table}_{col_name}"
                    ops.append(
                        AddConstraint(
                            table=model.table,
                            name=constraint_name,
                            sql_clause=f"UNIQUE ({col_name})",
                        )
                    )

        # Composite UNIQUE constraints from Meta.unique_together. Introspection
        # surfaces these as `u` constraints keyed by their column tuple (in
        # declared order), so re-runs recognize them and stay idempotent.
        for ut in model.unique_together:
            col_key = tuple(ut)
            if col_key not in existing_uniques:
                constraint_name = f"uq_{model.table}_{'_'.join(ut)}"
                cols = ", ".join(_qi(c) for c in ut)
                ops.append(
                    AddConstraint(
                        table=model.table,
                        name=constraint_name,
                        sql_clause=f"UNIQUE ({cols})",
                    )
                )

        return ops

    @classmethod
    def _diff_indexes(cls, model: ModelSchema, db_table: DbTable) -> list[Operation]:
        """Diff indexes between model and DB table."""
        ops = []

        existing_indexes = {tuple(i.columns): i for i in db_table.indexes}

        for col_name, model_col in model.columns.items():
            if model_col.has_index:
                col_key = (col_name,)
                if col_key not in existing_indexes:
                    # Check if this is a vector column — use vector index instead
                    if model_col.type_sql.startswith("vector("):
                        # Get vector metadata from model class
                        field_info = (
                            model._model.__dict__.get(col_name)
                            if model._model
                            else None
                        )
                        idx_type = (
                            field_info.vector_index_type
                            if field_info and field_info.vector_index_type
                            else "hnsw"
                        )
                        idx_ops = (
                            field_info.vector_index_ops
                            if field_info and field_info.vector_index_ops
                            else "vector_cosine_ops"
                        )
                        ops.append(
                            CreateVectorIndex(
                                table=model.table,
                                column=col_name,
                                index_type=idx_type,
                                index_ops=idx_ops,
                            )
                        )
                    else:
                        idx_name = f"idx_{model.table}_{col_name}"
                        ops.append(
                            CreateIndex(
                                table=model.table,
                                name=idx_name,
                                columns=[col_name],
                            )
                        )

        # Composite / partial / GIN / expression indexes from Meta.indexes.
        # Matched by generated index NAME against existing (non-constraint)
        # indexes so re-runs are idempotent; the rich DDL (INCLUDE / WITH /
        # WHERE / opclasses / expressions) is produced by models.py's single
        # source of truth rather than re-implemented here.
        if model.meta_indexes:
            from hyperdjango.models import _generate_index_ddl, _index_ddl_name

            existing_index_names = {i.name for i in db_table.indexes}
            for idx in model.meta_indexes:
                name = _index_ddl_name(model.table, idx)
                if name in existing_index_names:
                    continue
                ddl = _generate_index_ddl(model.table, idx)
                if not ddl.rstrip().endswith(";"):
                    ddl += ";"
                ops.append(
                    RunSQL(
                        forward=ddl,
                        reverse=f"DROP INDEX IF EXISTS {_qi(name)};",
                        _description=f"Create index {name} on {model.table}",
                    )
                )

        return ops


# ─── Migration State Manager ──────────────────────────────────────────────────


class MigrationStateManager:
    """Track applied migrations in hyper_migrations table."""

    TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS hyper_migrations ("
        "  id SERIAL PRIMARY KEY,"
        "  name TEXT NOT NULL UNIQUE,"
        "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        "  checksum TEXT DEFAULT ''"
        ")"
    )

    @staticmethod
    async def ensure_table(db):
        """Create hyper_migrations table if it doesn't exist."""
        await db.execute(MigrationStateManager.TABLE_SQL)

    @staticmethod
    async def get_applied(db) -> set[str]:
        """Get set of applied migration names."""
        try:
            rows = await db.query("SELECT name FROM hyper_migrations ORDER BY id")
            return {r["name"] for r in rows}
        # blind-except: the hyper_migrations state table may not exist yet on first run; an unreadable/absent table means nothing has been applied.
        except Exception:
            return set()

    @staticmethod
    async def get_applied_ordered(db) -> list[dict]:
        """Get applied migrations in order."""
        try:
            return await db.query(
                "SELECT name, applied_at, checksum FROM hyper_migrations ORDER BY id"
            )
        # blind-except: the hyper_migrations state table may not exist yet on first run; an unreadable/absent table means nothing has been applied.
        except Exception:
            return []

    @staticmethod
    async def record_applied(db, name: str, checksum: str = ""):
        """Record a migration as applied."""
        await db.execute(
            "INSERT INTO hyper_migrations (name, checksum) VALUES ($1, $2)",
            name,
            checksum,
        )

    @staticmethod
    async def record_unapplied(db, name: str):
        """Remove a migration from applied set."""
        await db.execute(
            "DELETE FROM hyper_migrations WHERE name = $1",
            name,
        )


# ─── Deployment Safety Analyzer ───────────────────────────────────────────────


class SafetyAnalyzer:
    """Analyze operations for production deployment safety."""

    @staticmethod
    async def analyze(ops: list[Operation], db=None) -> list[dict]:
        """Analyze operations and return safety report."""
        reports = []
        for op in ops:
            row_count = None
            if db and hasattr(op, "table"):
                try:
                    result = await db.query_val(
                        "SELECT reltuples::bigint FROM pg_class WHERE relname = $1",
                        op.table,
                    )
                    row_count = result if result and result > 0 else None
                # blind-except: reltuples is a best-effort planner statistic used only to size safety warnings; if pg_class is unreadable leave row_count None.
                except Exception:
                    pass

            warnings = op.safety_warnings(row_count)
            if warnings:
                reports.append(
                    {
                        "operation": op.description(),
                        "warnings": warnings,
                        "row_count": row_count,
                    }
                )
        return reports


# ─── Migration File Manager ───────────────────────────────────────────────────


class MigrationFileManager:
    """Manage migration SQL files on disk."""

    def __init__(self, migrations_dir: str = "migrations"):
        self.dir = Path(migrations_dir)

    def ensure_dir(self):
        self.dir.mkdir(exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)

    def next_number(self) -> int:
        """Get next migration number."""
        existing = sorted(self.dir.glob("*.sql"))
        if not existing:
            return 1
        last = existing[-1].stem
        try:
            return int(last.split("_")[0]) + 1
        except ValueError, IndexError:
            return len(existing) + 1

    def write_migration(self, number: int, name: str, ops: list[Operation]) -> Path:
        """Write a migration SQL file."""
        self.ensure_dir()

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{number:04d}_{name}_{timestamp}.sql"
        filepath = self.dir / filename

        lines = [
            f"-- Migration {number:04d}: {name}",
            f"-- Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"-- Operations: {len(ops)}",
            "",
            "-- UP",
        ]

        for op in ops:
            lines.append(f"-- {op.description()}")
            lines.append(op.up_sql())
            lines.append("")

        lines.append("-- DOWN")
        for op in reversed(ops):
            lines.append(f"-- Reverse: {op.description()}")
            lines.append(op.down_sql())
            lines.append("")

        filepath.write_text("\n".join(lines))
        return filepath

    def list_migrations(self) -> list[Path]:
        """List all migration files in order."""
        if not self.dir.exists():
            return []
        return sorted(self.dir.glob("*.sql"))

    def migration_is_atomic(
        self, filepath: Path, up_stmts: list[str] | None = None
    ) -> bool:
        """Whether a migration may be wrapped in a single transaction.

        PostgreSQL forbids `CREATE/DROP INDEX CONCURRENTLY` (and a few others,
        e.g. VACUUM) inside a transaction block — yet the framework GENERATES
        concurrent-index DDL and its safety analysis RECOMMENDS it, so applying
        such a migration transactionally would fail. Returns False when the
        migration must run WITHOUT a wrapping transaction: any `CONCURRENTLY`
        statement, or an explicit `-- hyper:atomic = false` header marker (for
        other non-transactional ops). Non-atomic migrations are NOT rolled back
        on failure — a partial apply must be recovered manually.
        """
        if up_stmts is None:
            up_stmts, _ = self.parse_migration(filepath)
        for s in up_stmts:
            if "CONCURRENTLY" in s.upper():
                return False
        # Explicit opt-out marker in the file header (before -- UP).
        try:
            for line in filepath.read_text().split("\n"):
                st = line.strip().lower()
                if st == "-- up":
                    break
                compact = st.replace(" ", "")
                if compact.startswith("--hyper:atomic=false") or compact.startswith(
                    "--atomic:false"
                ):
                    return False
        except OSError:
            pass
        return True

    def parse_migration(self, filepath: Path) -> tuple[list[str], list[str]]:
        """Parse UP and DOWN SQL from a migration file.

        Returns (up_statements, down_statements).
        """
        content = filepath.read_text()
        up_stmts = []
        down_stmts = []

        section = None
        current: list[str] = []

        for line in content.split("\n"):
            stripped = line.strip()
            # Are we currently inside an unclosed dollar-quoted body OR an open
            # single-quoted string literal spanning multiple lines? If so, `;`,
            # `-- UP/DOWN` markers, and comment lines are all string/body text.
            prior = "\n".join(current)
            inside_dollar = _open_dollar_tag(prior) is not None
            inside_squote = _open_single_quote(prior)

            if not inside_dollar and not inside_squote:
                if stripped == "-- UP":
                    section = "up"
                    continue
                elif stripped == "-- DOWN":
                    # Flush any pending UP statement
                    if current:
                        stmt = "\n".join(current).strip()
                        if stmt and not stmt.startswith("--"):
                            up_stmts.append(stmt)
                    current = []
                    section = "down"
                    continue

            if section is None:
                continue

            # Outside a statement body, skip blank lines and standalone
            # comments. Once we're mid-statement (or inside a dollar body) we
            # keep every line verbatim so the SQL round-trips exactly.
            if not current and (not stripped or stripped.startswith("--")):
                continue

            current.append(line)

            # A statement terminates on a trailing `;` only when we're NOT inside
            # a dollar-quoted body NOR an open single-quoted string literal. Strip
            # any trailing inline `-- comment` first (seeding the stripper with
            # whether this line began inside a string) so `INSERT ...; -- note`
            # still terminates on its `;`, while a `;` inside a multi-line
            # `'foo;\nbar'` literal does not.
            joined = "\n".join(current)
            code = _line_code_before_comment(stripped, inside_squote).rstrip()
            if (
                code.endswith(";")
                and _open_dollar_tag(joined) is None
                and not _open_single_quote(joined)
            ):
                stmt = joined.strip()
                if stmt and not stmt.startswith("--"):
                    target = up_stmts if section == "up" else down_stmts
                    target.append(stmt)
                current = []

        # Flush remaining
        if current:
            stmt = "\n".join(current).strip()
            if stmt and not stmt.startswith("--"):
                target = up_stmts if section == "up" else down_stmts
                target.append(stmt)

        return up_stmts, down_stmts

    def write_snapshot(self, snapshot: SchemaSnapshot, migration_id: str) -> Path:
        """Save a schema snapshot to disk."""
        self.ensure_dir()
        snapshot.migration_id = migration_id
        filepath = self.dir / "snapshots" / f"{migration_id}_snapshot.json"
        filepath.write_text(_stdlib_json.dumps(snapshot.to_dict(), indent=2))
        return filepath

    def load_snapshot(self, migration_id: str) -> SchemaSnapshot | None:
        """Load a schema snapshot from disk."""
        filepath = self.dir / "snapshots" / f"{migration_id}_snapshot.json"
        if not filepath.exists():
            return None
        data = fast_json_loads(filepath.read_text())
        return SchemaSnapshot.from_dict(data)

    def latest_snapshot(self) -> SchemaSnapshot | None:
        """Load the most recent snapshot."""
        snap_dir = self.dir / "snapshots"
        if not snap_dir.exists():
            return None
        files = sorted(snap_dir.glob("*_snapshot.json"))
        if not files:
            return None
        data = fast_json_loads(files[-1].read_text())
        return SchemaSnapshot.from_dict(data)


# ─── Migration Engine ─────────────────────────────────────────────────────────


class MigrationEngine:
    """Core migration engine — orchestrates introspection, diffing, and execution."""

    def __init__(self, migrations_dir: str = "migrations"):
        self.files = MigrationFileManager(migrations_dir)
        self.state = MigrationStateManager

    async def makemigrations(
        self, db, name: str = "auto", dry_run: bool = False
    ) -> dict[str, list[Operation] | str | None]:
        """Generate migration from model changes vs live DB.

        Returns dict with 'operations', 'filepath', 'sql' keys.
        """
        # 1. Introspect live database
        db_snapshot = await DatabaseIntrospector.introspect(db)

        # 2. Extract model schemas from registry
        model_schemas = ModelExtractor.extract_all()

        if not model_schemas:
            return {
                "operations": [],
                "filepath": None,
                "sql": [],
                "message": "No models registered",
            }

        # 3. Diff models vs live DB
        ops = SchemaDiffer.diff(model_schemas, db_snapshot)

        if not ops:
            return {
                "operations": [],
                "filepath": None,
                "sql": [],
                "message": "No changes detected",
            }

        # 4. Safety analysis
        safety = await SafetyAnalyzer.analyze(ops, db)

        # 5. Generate SQL preview
        sql_preview = [op.up_sql() for op in ops]

        if dry_run:
            return {
                "operations": ops,
                "filepath": None,
                "sql": sql_preview,
                "safety": safety,
                "message": f"Dry run: {len(ops)} operations",
            }

        # 6. Write migration file
        number = self.files.next_number()
        filepath = self.files.write_migration(number, name, ops)

        return {
            "operations": ops,
            "filepath": filepath,
            "sql": sql_preview,
            "safety": safety,
            "message": f"Created migration {filepath.name} ({len(ops)} operations)",
        }

    # Stable 64-bit-ish advisory lock id derived from a constant label.
    _LOCK_LABEL = "hyper_migrations"

    @contextlib.asynccontextmanager
    async def _migration_lock(self, db):
        """Hold the migration advisory lock on ONE pinned backend session.

        A PostgreSQL *session-level* advisory lock is released only by the
        exact backend session that acquired it. The connection pool hands out
        an arbitrary session per call, so acquiring via ``db.query_val`` and
        releasing via ``db.execute`` can hit different sessions — the unlock
        silently no-ops and the lock leaks until that connection is recycled,
        wedging every future migration.

        We therefore pin a single connection for the whole lock → work →
        unlock lifecycle. The pinned connection just holds the lock; the
        actual DDL still runs on the pool (advisory locks are independent of
        which session performs the work).
        """
        # These live-connection primitives are the same ones the server-side
        # cursor uses to pin a pool connection. Imported lazily to avoid a
        # module-load-time dependency on the native pool internals.
        from hyperdjango.database import (
            _db_conn_acquire,
            _db_conn_release,
            _db_query,
        )

        lock_sql = f"SELECT pg_try_advisory_lock(hashtext('{self._LOCK_LABEL}'))"
        unlock_sql = f"SELECT pg_advisory_unlock(hashtext('{self._LOCK_LABEL}'))"

        conn = _db_conn_acquire(db._pool_handle)
        # Native convention: a negative handle routes the query to this exact
        # pinned connection instead of checking out a fresh pooled session.
        pinned = -(conn + 2)
        try:
            rows = _db_query(pinned, lock_sql, [])
            if not (rows and rows[0][0]):
                raise RuntimeError(
                    "Another migration is in progress. Wait for it to "
                    "complete or run: "
                    f"SELECT pg_advisory_unlock(hashtext('{self._LOCK_LABEL}'))"
                )
            try:
                yield
            finally:
                # Unlock on the SAME pinned session that took the lock.
                with contextlib.suppress(Exception):
                    _db_query(pinned, unlock_sql, [])
        finally:
            _db_conn_release(conn)

    async def migrate(
        self, db, target: str | None = None, fake: bool = False, dry_run: bool = False
    ) -> list[str]:
        """Apply pending migrations.

        Uses a PostgreSQL advisory lock (held on one pinned connection) to
        prevent concurrent migration runs. Returns applied migration names.
        """
        # A read-only dry run doesn't mutate anything — no lock required.
        if dry_run:
            return await self._migrate_inner(db, target, fake, dry_run)

        async with self._migration_lock(db):
            return await self._migrate_inner(db, target, fake, dry_run)

    async def _migrate_inner(self, db, target, fake, dry_run) -> list[str]:
        """Inner migration logic (called under advisory lock)."""
        await self.state.ensure_table(db)
        applied = await self.state.get_applied(db)

        # Get pending migrations
        all_migrations = self.files.list_migrations()
        pending = [m for m in all_migrations if m.stem not in applied]

        if target:
            # Apply up to target
            pending = [m for m in pending if m.stem <= target]

        if not pending:
            return []

        applied_names = []

        for migration_file in pending:
            up_stmts, _ = self.files.parse_migration(migration_file)

            if dry_run:
                logger.info("Would apply: {migration}", migration=migration_file.stem)
                for stmt in up_stmts:
                    logger.info("  {stmt}", stmt=stmt)
                applied_names.append(migration_file.stem)
                continue

            if fake:
                await self.state.record_applied(db, migration_file.stem)
                applied_names.append(migration_file.stem)
                continue

            atomic = self.files.migration_is_atomic(migration_file, up_stmts)

            if atomic:
                # Apply DDL + bookkeeping in ONE transaction so a failure leaves
                # neither a half-applied schema nor a stale hyper_migrations row.
                try:
                    async with db.transaction():
                        for stmt in up_stmts:
                            await db.execute(stmt)
                        # Record inside transaction — if this fails, DDL is rolled back
                        await self.state.record_applied(db, migration_file.stem)
                    applied_names.append(migration_file.stem)
                except Exception as e:
                    raise RuntimeError(
                        f"Migration {migration_file.stem} failed: {e}\n"
                        f"Database has been rolled back to pre-migration state."
                    ) from e
            else:
                # Non-transactional migration (CONCURRENTLY / explicit atomic=false).
                # PostgreSQL forbids these inside a transaction block, so each
                # statement auto-commits. There is NO rollback: a failure part-way
                # leaves earlier statements applied and the migration UNrecorded, so
                # a re-run re-executes from the start — recover manually (e.g. drop a
                # half-built INVALID index) before retrying.
                try:
                    for stmt in up_stmts:
                        await db.execute(stmt)
                    await self.state.record_applied(db, migration_file.stem)
                    applied_names.append(migration_file.stem)
                except Exception as e:
                    raise RuntimeError(
                        f"Non-atomic migration {migration_file.stem} failed: {e}\n"
                        f"It ran WITHOUT a transaction — the schema may be partially "
                        f"applied and was NOT recorded. Inspect the database and clean "
                        f"up (e.g. DROP any INVALID index) before re-running."
                    ) from e

        return applied_names

    async def rollback(self, db, target: str | None = None) -> list[str]:
        """Rollback migrations (most recent first).

        If target is given, rollback to (but not including) that migration.
        Returns list of rolled-back migration names.

        Runs under the same advisory lock as migrate() so a rollback can't
        race a concurrent migration.
        """
        async with self._migration_lock(db):
            return await self._rollback_inner(db, target)

    async def _rollback_inner(self, db, target: str | None) -> list[str]:
        """Inner rollback logic (called under the advisory lock)."""
        await self.state.ensure_table(db)
        applied_ordered = await self.state.get_applied_ordered(db)

        if not applied_ordered:
            return []

        # Determine which to rollback
        to_rollback = []
        for mig in reversed(applied_ordered):
            if target and mig["name"] <= target:
                break
            to_rollback.append(mig["name"])

        if not target and to_rollback:
            # Default: rollback only the last migration
            to_rollback = [to_rollback[0]]

        rolled_back = []
        for mig_name in to_rollback:
            # Find the migration file
            matching = [f for f in self.files.list_migrations() if f.stem == mig_name]
            if not matching:
                raise RuntimeError(
                    f"Cannot rollback {mig_name}: migration file not found"
                )

            _, down_stmts = self.files.parse_migration(matching[0])
            if not down_stmts:
                raise RuntimeError(f"Cannot rollback {mig_name}: no DOWN statements")

            try:
                async with db.transaction():
                    for stmt in down_stmts:
                        await db.execute(stmt)
                    # Record un-applied INSIDE the transaction so the DOWN DDL
                    # and the bookkeeping commit atomically. If recording were
                    # outside and the process died first, the row would still
                    # show as applied despite its schema having been reverted.
                    await self.state.record_unapplied(db, mig_name)
                rolled_back.append(mig_name)
            except Exception as e:
                raise RuntimeError(f"Rollback of {mig_name} failed: {e}") from e

        return rolled_back

    async def verify(self, db) -> dict[str, bool | list[str] | SchemaSnapshot]:
        """Verify models match live database schema.

        Returns dict with 'matches', 'drift' (list of issues), 'snapshot'.
        """
        db_snapshot = await DatabaseIntrospector.introspect(db)
        model_schemas = ModelExtractor.extract_all()
        ops = SchemaDiffer.diff(model_schemas, db_snapshot)

        drift = [op.description() for op in ops]

        return {
            "matches": len(drift) == 0,
            "drift": drift,
            "snapshot": db_snapshot,
        }

    async def snapshot(self, db) -> Path:
        """Save current schema snapshot to disk."""
        db_snapshot = await DatabaseIntrospector.introspect(db)

        applied = await self.state.get_applied_ordered(db)
        migration_id = applied[-1]["name"] if applied else "initial"

        filepath = self.files.write_snapshot(db_snapshot, migration_id)
        return filepath

    async def showmigrations(self, db) -> list[dict]:
        """List all migrations with applied status."""
        await self.state.ensure_table(db)
        applied = await self.state.get_applied(db)
        all_migrations = self.files.list_migrations()

        result = []
        for m in all_migrations:
            result.append(
                {
                    "name": m.stem,
                    "applied": m.stem in applied,
                    "file": str(m),
                }
            )
        return result

    async def squash(
        self, db, up_to: str | None = None
    ) -> dict[str, int | str | list[str]]:
        """Squash old migrations into a single initial migration + snapshot.

        1. Saves current schema as a snapshot
        2. Generates a single CREATE TABLE migration from the snapshot
        3. Removes old migration files
        4. Updates hyper_migrations to reflect the squash

        Args:
            up_to: squash all migrations up to (and including) this name.
                   If None, squashes ALL applied migrations.

        Returns dict with 'squashed_count', 'new_migration', 'snapshot_path'.
        """
        await self.state.ensure_table(db)
        applied = await self.state.get_applied_ordered(db)

        if not applied:
            return {"squashed_count": 0, "message": "No migrations to squash"}

        # Determine which to squash
        if up_to:
            to_squash = [m for m in applied if m["name"] <= up_to]
        else:
            to_squash = list(applied)

        if len(to_squash) < 2:
            return {
                "squashed_count": 0,
                "message": "Need at least 2 migrations to squash",
            }

        # 1. Save current schema snapshot
        db_snapshot = await DatabaseIntrospector.introspect(db)
        last_squashed = to_squash[-1]["name"]
        snapshot_path = self.files.write_snapshot(db_snapshot, last_squashed)

        # 2. Generate the initial migration the SAME way a fresh schema is
        # built: diff the (snapshot-present) models against an EMPTY schema so
        # the squash gets FK topo-ordering + deferred FKs (_order_creates),
        # M2M junction tables, and every secondary/vector/composite index —
        # not just bare CREATE TABLEs. Replaying squashed_initial then produces
        # a byte-for-byte equivalent schema.
        model_schemas = ModelExtractor.extract_all()
        present = [s for s in model_schemas if s.table in db_snapshot.tables]
        ops = SchemaDiffer.diff(present, SchemaSnapshot(tables={}))

        squash_number = 1
        squash_name = "squashed_initial"
        squash_file = self.files.write_migration(squash_number, squash_name, ops)

        # 3. Remove old migration files
        removed = 0
        for mig in to_squash:
            matching = [
                f for f in self.files.list_migrations() if f.stem == mig["name"]
            ]
            for f in matching:
                if f != squash_file:
                    f.unlink()
                    removed += 1

        # 4. Update migration state — replace all squashed entries with the new one
        for mig in to_squash:
            await self.state.record_unapplied(db, mig["name"])
        await self.state.record_applied(
            db, squash_file.stem, db_snapshot.compute_checksum()
        )

        return {
            "squashed_count": len(to_squash),
            "removed_files": removed,
            "new_migration": squash_file.name,
            "snapshot_path": str(snapshot_path),
            "message": (
                f"Squashed {len(to_squash)} migrations into {squash_file.name}. "
                f"Snapshot saved."
            ),
        }

    def generate_sql(self, target: str | None = None) -> str:
        """Generate SQL for pending migrations without connecting to database.

        Like Alembic's --sql offline mode. Returns the full SQL script
        that would be executed by `migrate`.
        """
        all_migrations = self.files.list_migrations()
        if target:
            # Match by number prefix or full stem
            try:
                target_num = int(target.split("_")[0])
                all_migrations = [
                    m for m in all_migrations if int(m.stem.split("_")[0]) <= target_num
                ]
            except ValueError, IndexError:
                all_migrations = [m for m in all_migrations if m.stem <= target]

        lines = [
            "-- Generated SQL for pending migrations",
            f"-- Migrations: {len(all_migrations)}",
            "",
        ]

        for migration_file in all_migrations:
            up_stmts, _ = self.files.parse_migration(migration_file)
            lines.append(f"-- Migration: {migration_file.stem}")
            lines.append(f"-- {'=' * 60}")
            for stmt in up_stmts:
                lines.append(stmt)
                if not stmt.strip().endswith(";"):
                    lines.append(";")
                lines.append("")
            # Record in migration table
            safe_name = migration_file.stem.replace("'", "''")
            lines.append(f"INSERT INTO hyper_migrations (name) VALUES ('{safe_name}');")
            lines.append("")

        return "\n".join(lines)

    async def check_schema_version(self, db, min_version: int) -> bool:
        """Check that database schema meets minimum version requirement.

        Raises RuntimeError if schema is too old.
        """
        await self.state.ensure_table(db)
        applied = await self.state.get_applied_ordered(db)

        if not applied:
            if min_version > 0:
                raise RuntimeError(
                    f"No migrations applied. Minimum required: {min_version}. "
                    f"Run: hyper migrate"
                )
            return True

        last_name = applied[-1]["name"]
        try:
            current_version = int(last_name.split("_")[0])
        except ValueError, IndexError:
            current_version = len(applied)

        if current_version < min_version:
            raise RuntimeError(
                f"Schema version {current_version} is below minimum {min_version}. "
                f"Run: hyper migrate"
            )
        return True


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _sql_literal(value) -> str:
    """Convert a Python value to a SQL literal."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Escape single quotes
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if value is None:
        return "NULL"
    # Fallback: escape quotes in string representation
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# Async Migration Runner — progress reporting + timing + safety
# ---------------------------------------------------------------------------


@dataclass
class MigrationResult:
    """Result of a single migration run."""

    name: str
    status: str  # "applied", "skipped", "failed", "dry_run", "fake"
    duration_ms: float = 0.0
    sql_statements: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class MigrationRunReport:
    """Complete report from a migration run."""

    results: list[MigrationResult] = field(default_factory=list)
    total_duration_ms: float = 0.0
    applied_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0

    @property
    def success(self) -> bool:
        return self.failed_count == 0


class AsyncMigrationRunner:
    """Enhanced migration runner with progress reporting and safety checks.

    Wraps MigrationEngine with:
    - Per-migration timing and progress callbacks
    - Dry-run with SQL preview and destructive operation warnings
    - Rollback safety checks (warn before data-destroying operations)
    - Migration profiling (duration per statement)

    Usage:
        runner = AsyncMigrationRunner(migration_system)
        report = await runner.run(db, on_progress=my_callback)
        print(f"Applied {report.applied_count} migrations in {report.total_duration_ms}ms")
    """

    # SQL patterns that indicate potentially destructive operations
    _DESTRUCTIVE_PATTERNS = (
        "DROP TABLE",
        "DROP COLUMN",
        "TRUNCATE",
        "DELETE FROM",
        "ALTER TABLE",  # Can drop NOT NULL, change types, etc.
    )

    def __init__(self, migration_system: MigrationEngine):
        self._system = migration_system

    def _check_destructive(self, statements: list[str]) -> list[str]:
        """Check for potentially destructive SQL statements."""
        warnings: list[str] = []
        for stmt in statements:
            upper = stmt.upper().strip()
            for pattern in self._DESTRUCTIVE_PATTERNS:
                if pattern in upper:
                    warnings.append(f"Potentially destructive: {stmt[:80]}")
                    break
        return warnings

    async def run(
        self,
        db,
        target: str | None = None,
        fake: bool = False,
        dry_run: bool = False,
        on_progress: Callable | None = None,
    ) -> MigrationRunReport:
        """Run migrations with progress reporting.

        Args:
            db: Database connection.
            target: Apply up to this migration name.
            fake: Record migrations as applied without running SQL.
            dry_run: Preview SQL without applying.
            on_progress: Callback(migration_name, status, index, total).

        Returns:
            MigrationRunReport with per-migration results and timing.
        """
        report = MigrationRunReport()
        run_start = time.monotonic()

        # Hold the migration advisory lock on ONE pinned connection for the
        # whole run (skipped for a read-only dry run). Reuses the engine's
        # pinned-connection helper: the old code acquired the lock via
        # ``query_val`` on one pooled session and released it via ``execute`` on
        # another, so the session-level unlock hit the wrong backend and the
        # lock leaked until that connection was recycled.
        lock_ctx = None
        if not dry_run:
            lock_ctx = self._system._migration_lock(db)
            try:
                await lock_ctx.__aenter__()
            except RuntimeError as e:
                # Lock unavailable → another migration is already in progress.
                report.results.append(
                    MigrationResult(name="<lock>", status="failed", error=str(e))
                )
                report.failed_count = 1
                report.total_duration_ms = round(
                    (time.monotonic() - run_start) * 1000, 2
                )
                return report

        try:
            await self._system.state.ensure_table(db)
            applied = await self._system.state.get_applied(db)
            all_migrations = self._system.files.list_migrations()
            pending = [m for m in all_migrations if m.stem not in applied]

            if target:
                pending = [m for m in pending if m.stem <= target]

            total = len(pending)
            for idx, migration_file in enumerate(pending):
                mig_start = time.monotonic()
                up_stmts, _ = self._system.files.parse_migration(migration_file)
                mig_name = migration_file.stem

                # Progress callback
                if on_progress is not None:
                    on_progress(mig_name, "starting", idx, total)

                # Safety check
                warnings = self._check_destructive(up_stmts)

                if dry_run:
                    result = MigrationResult(
                        name=mig_name,
                        status="dry_run",
                        sql_statements=len(up_stmts),
                        warnings=warnings,
                    )
                    report.results.append(result)
                    report.applied_count += 1
                    if on_progress is not None:
                        on_progress(mig_name, "dry_run", idx + 1, total)
                    continue

                if fake:
                    await self._system.state.record_applied(db, mig_name)
                    result = MigrationResult(
                        name=mig_name,
                        status="fake",
                        sql_statements=len(up_stmts),
                    )
                    report.results.append(result)
                    report.applied_count += 1
                    if on_progress is not None:
                        on_progress(mig_name, "fake", idx + 1, total)
                    continue

                # Apply within transaction
                try:
                    async with db.transaction():
                        for stmt in up_stmts:
                            await db.execute(stmt)
                        await self._system.state.record_applied(db, mig_name)

                    duration = (time.monotonic() - mig_start) * 1000
                    result = MigrationResult(
                        name=mig_name,
                        status="applied",
                        duration_ms=round(duration, 2),
                        sql_statements=len(up_stmts),
                        warnings=warnings,
                    )
                    report.results.append(result)
                    report.applied_count += 1

                    if on_progress is not None:
                        on_progress(mig_name, "applied", idx + 1, total)

                # blind-except: per-migration failure is captured into MigrationResult(status="failed") and halts the run (break below); caller inspects report.failed_count, so it is not silently dropped.
                except Exception as e:
                    duration = (time.monotonic() - mig_start) * 1000
                    result = MigrationResult(
                        name=mig_name,
                        status="failed",
                        duration_ms=round(duration, 2),
                        sql_statements=len(up_stmts),
                        error=str(e),
                        warnings=warnings,
                    )
                    report.results.append(result)
                    report.failed_count += 1

                    if on_progress is not None:
                        on_progress(mig_name, "failed", idx + 1, total)
                    break  # Stop on first failure

        finally:
            # Release on the SAME pinned session that acquired the lock.
            if lock_ctx is not None:
                with contextlib.suppress(Exception):
                    await lock_ctx.__aexit__(None, None, None)

        report.total_duration_ms = round((time.monotonic() - run_start) * 1000, 2)
        return report

    async def preview(self, db, target: str | None = None) -> MigrationRunReport:
        """Preview pending migrations without applying (alias for dry_run)."""
        return await self.run(db, target=target, dry_run=True)
