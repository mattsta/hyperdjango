"""
Fixture system — dumpdata/loaddata for HyperDjango models.

Serialize model instances to JSON fixtures and load them back.
Supports natural keys, FK dependency ordering, and upsert semantics.

No Django dependency.

Usage:
    from hyperdjango.fixtures import dumpdata, loaddata

    # Dump all users and articles to JSON
    json_str = await dumpdata([User, Article])

    # Dump to file
    await dumpdata([User], output_path="users.json")

    # Load from JSON string
    result = await loaddata('[{"model": "users", "pk": 1, "fields": {"name": "Alice"}}]')

    # Load from file
    result = await loaddata("fixtures/users.json")

    # Natural keys
    json_str = await dumpdata_natural(User, ["email"])
"""

import base64
import json as _stdlib_json
import pathlib
import types as _types
import typing as _typing
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from hyperdjango.database import get_db
from hyperdjango.native import fast_json_loads
from hyperdjango.query import _get_model_by_table

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FixtureRecord:
    """A single fixture entry representing one model instance."""

    model_name: str
    pk: int | str | None
    fields: dict[str, object]


@dataclass(slots=True)
class LoadResult:
    """Result of a loaddata operation."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_value(value: object) -> object:
    """Convert a Python value to a JSON-safe representation.

    Handles datetime, date, time, timedelta, UUID, Decimal, bytes, and None.
    Passes through str, int, float, bool, list, and dict unchanged.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    # Fallback: convert to string
    return str(value)


def _deserialize_value(value: object, field_type: str) -> object:
    """Convert a JSON value back to the appropriate Python type.

    Args:
        value: The JSON value to convert.
        field_type: Target type name — "datetime", "date", "time", "timedelta",
            "uuid", "decimal", "bytes", "int", "float", "bool", "str".
    """
    if value is None:
        return None

    if field_type == "datetime":
        return datetime.fromisoformat(value)
    if field_type == "date":
        return date.fromisoformat(value)
    if field_type == "time":
        return time.fromisoformat(value)
    if field_type == "timedelta":
        return timedelta(seconds=value)
    if field_type == "uuid":
        return UUID(value)
    if field_type == "decimal":
        return Decimal(value)
    if field_type == "bytes":
        return base64.b64decode(value)
    if field_type == "int":
        return int(value)
    if field_type == "float":
        return float(value)
    if field_type == "bool":
        return bool(value)
    if field_type == "str":
        return str(value)
    return value


# Exact Python type → _deserialize_value field_type string. Order-independent
# because annotations resolve to exact types (datetime and date are distinct
# keys, bool and int are distinct keys).
_PYTYPE_TO_FIELDTYPE: dict[type, str] = {
    datetime: "datetime",
    date: "date",
    time: "time",
    timedelta: "timedelta",
    UUID: "uuid",
    Decimal: "decimal",
    bytes: "bytes",
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
}


def _unwrap_optional(ann: object) -> object:
    """Strip ``| None`` / ``Optional[...]`` down to the inner type."""
    origin = _typing.get_origin(ann)
    if origin is _typing.Union or origin is _types.UnionType:
        args = [a for a in _typing.get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ann


def _resolve_field_types(model_class: type) -> dict[str, str]:
    """Map each column name to a :func:`_deserialize_value` field_type string.

    Reverses ``_serialize_value`` at load time: JSON stores datetime/Decimal/
    UUID/bytes/timedelta as strings/floats, but the binary pg encoder needs the
    real Python objects. Types are derived from the model's resolved annotations;
    FK columns carry the target PK (int); anything unrecognized maps to "" so
    :func:`_deserialize_value` passes it through unchanged.
    """
    try:
        hints = _typing.get_type_hints(model_class)
    # An unresolvable forward ref falls back to passthrough types, not a failed load.
    # blind-except: best-effort annotation resolution; any resolution error → safe passthrough
    except Exception:
        hints = dict(model_class.__dict__.get("__annotations__", {}))
    result: dict[str, str] = {}
    for col_name, fmeta in model_class._meta.fields.items():
        if fmeta.foreign_key:
            result[col_name] = "int"
            continue
        ann = _unwrap_optional(hints.get(col_name))
        result[col_name] = _PYTYPE_TO_FIELDTYPE.get(ann, "")
    return result


# ---------------------------------------------------------------------------
# FK dependency sorting
# ---------------------------------------------------------------------------


def _sort_by_dependencies(model_classes: list[type]) -> list[type]:
    """Sort model classes so that FK targets come before dependents.

    Uses a simple topological sort. Models with FK references to other models
    in the list are placed after their targets.
    """
    table_to_model: dict[str, type] = {}
    for cls in model_classes:
        table = cls._meta.table
        existing = table_to_model.get(table)
        if existing is None:
            table_to_model[table] = cls
        elif cls._meta.sti_type is None and existing._meta.sti_type is not None:
            # Prefer the STI parent (no sti_type) over children
            table_to_model[table] = cls

    # Build adjacency: model -> set of models it depends on
    deps: dict[str, set[str]] = {}
    for cls in model_classes:
        table = cls._meta.table
        deps[table] = set()
        for _name, fmeta in cls._meta.fields.items():
            if fmeta.foreign_key:
                # FK can be "table" or "table.column" — extract table name
                fk_table = (
                    fmeta.foreign_key.split(".")[0]
                    if "." in fmeta.foreign_key
                    else fmeta.foreign_key
                )
                if fk_table in table_to_model:
                    deps[table].add(fk_table)

    # Topological sort (Kahn's algorithm)
    in_degree: dict[str, int] = {t: 0 for t in deps}
    for table, dep_set in deps.items():
        for dep in dep_set:
            if dep in in_degree:
                in_degree[table] += 1

    # Wait — Kahn's: in_degree counts how many deps each node has.
    # Actually, let me redo this properly.
    in_degree_map: dict[str, int] = {t: len(dep_set) for t, dep_set in deps.items()}
    # reverse adjacency: dep -> list of tables that depend on it
    reverse: dict[str, list[str]] = {t: [] for t in deps}
    for table, dep_set in deps.items():
        for dep in dep_set:
            if dep in reverse:
                reverse[dep].append(table)

    queue: list[str] = [t for t, d in in_degree_map.items() if d == 0]
    result: list[str] = []
    while queue:
        table = queue.pop(0)
        result.append(table)
        for dependent in reverse.get(table, []):
            in_degree_map[dependent] -= 1
            if in_degree_map[dependent] == 0:
                queue.append(dependent)

    # Any remaining (cycles) — append in original order
    for cls in model_classes:
        if cls._meta.table not in result:
            result.append(cls._meta.table)

    return [table_to_model[t] for t in result if t in table_to_model]


# ---------------------------------------------------------------------------
# dumpdata
# ---------------------------------------------------------------------------


async def dumpdata(
    model_classes: list[type],
    output_path: str | None = None,
    indent: int = 2,
) -> str:
    """Dump model instances to a JSON fixture string.

    Queries all rows from each model class, serializes them as fixture records,
    and returns the JSON string. Optionally writes to a file.

    Args:
        model_classes: List of Model subclasses to dump.
        output_path: If provided, write JSON to this file path.
        indent: JSON indentation level (default 2).

    Returns:
        JSON string of all fixture records.
    """
    sorted_classes = _sort_by_dependencies(model_classes)
    records: list[dict[str, object]] = []

    for model_class in sorted_classes:
        meta = model_class._meta
        instances = await model_class.objects.all()

        for instance in instances:
            pk_field = meta.pk_field
            pk_value = _serialize_value(instance.pk)

            fields: dict[str, object] = {}
            for col_name in meta.column_names:
                if col_name == pk_field:
                    continue
                raw_value = instance._resolve_value(
                    object.__getattribute__(instance, col_name)
                )
                fields[col_name] = _serialize_value(raw_value)

            record = {
                "model": meta.table,
                "pk": pk_value,
                "fields": fields,
            }
            records.append(record)

    json_str = _stdlib_json.dumps(records, indent=indent, ensure_ascii=False)

    if output_path is not None:
        if ".." in str(output_path):
            raise ValueError(f"Path traversal not allowed: {output_path}")
        resolved = pathlib.Path(output_path).resolve()
        with resolved.open("w", encoding="utf-8") as f:
            f.write(json_str)

    return json_str


# ---------------------------------------------------------------------------
# dumpdata with natural keys
# ---------------------------------------------------------------------------


async def dumpdata_natural(
    model_class: type,
    natural_key_fields: list[str],
    indent: int = 2,
) -> str:
    """Dump model instances using natural keys instead of PKs.

    Natural keys are field combinations that uniquely identify a record
    without using the database primary key (e.g., username, email).

    Args:
        model_class: The Model subclass to dump.
        natural_key_fields: List of field names that form the natural key.
        indent: JSON indentation level.

    Returns:
        JSON string of fixture records with natural_key instead of pk.
    """
    meta = model_class._meta
    instances = await model_class.objects.all()
    records: list[dict[str, object]] = []

    for instance in instances:
        # Build natural key tuple
        natural_key = [
            _serialize_value(
                instance._resolve_value(object.__getattribute__(instance, nk_field))
            )
            for nk_field in natural_key_fields
        ]

        # Build fields dict (exclude PK and natural key fields)
        fields: dict[str, object] = {}
        pk_field = meta.pk_field
        nk_set = set(natural_key_fields)
        for col_name in meta.column_names:
            if col_name == pk_field or col_name in nk_set:
                continue
            raw_value = instance._resolve_value(
                object.__getattribute__(instance, col_name)
            )
            fields[col_name] = _serialize_value(raw_value)

        record = {
            "model": meta.table,
            "natural_key": natural_key,
            "natural_key_fields": natural_key_fields,
            "fields": fields,
        }
        records.append(record)

    return _stdlib_json.dumps(records, indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# loaddata
# ---------------------------------------------------------------------------


def _parse_fixture_source(
    source: str | list[dict[str, object]],
) -> list[dict[str, object]]:
    """Parse fixture source into a list of record dicts.

    Accepts:
    - A JSON string
    - A file path (ending in .json or existing file)
    - A list of dicts (passed through)

    Raises ValueError on invalid input.
    """
    if isinstance(source, list):
        return source

    if not isinstance(source, str):
        raise ValueError(f"Expected str or list, got {type(source).__name__}")

    # Try as file path first
    if pathlib.Path(source).is_file():
        if ".." in str(source):
            raise ValueError(f"Path traversal not allowed: {source}")
        resolved = pathlib.Path(source).resolve()
        with resolved.open(encoding="utf-8") as f:
            data = fast_json_loads(f.read())
        if not isinstance(data, list):
            raise ValueError(
                f"Fixture file must contain a JSON array, got {type(data).__name__}"
            )
        return data

    # Try as JSON string
    try:
        data = fast_json_loads(source)
    except (ValueError, RuntimeError) as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(f"Fixture JSON must be an array, got {type(data).__name__}")
    return data


async def _upsert_record(
    record_dict: dict[str, object],
    db: object,
) -> tuple[str, str | None]:
    """Insert or update a single fixture record.

    Returns:
        ("created", None) or ("updated", None) on success.
        ("error", error_message) on failure.
    """
    model_name = record_dict.get("model")
    if model_name is None:
        return ("error", "Record missing 'model' key")

    model_class = _get_model_by_table(model_name)
    if model_class is None:
        return ("error", f"Unknown model table: {model_name}")

    meta = model_class._meta
    pk_field = meta.pk_field

    # Determine if this is a natural key record
    natural_key = record_dict.get("natural_key")
    natural_key_field_names = record_dict.get("natural_key_fields")

    fields = record_dict.get("fields", {})
    pk_value = record_dict.get("pk")

    # Validate fields exist on the model
    valid_columns = set(meta.column_names)
    for field_name in fields:
        if field_name not in valid_columns:
            return ("error", f"Unknown field '{field_name}' on model '{model_name}'")

    # Reverse _serialize_value: turn JSON strings/floats back into the real
    # datetime/Decimal/UUID/bytes/timedelta objects the binary pg encoder needs.
    # Without this, a dumpdata → loaddata round-trip corrupts (or rejects) every
    # non-primitive column.
    type_map = _resolve_field_types(model_class)
    fields = {k: _deserialize_value(v, type_map.get(k, "")) for k, v in fields.items()}
    if pk_value is not None:
        pk_value = _deserialize_value(pk_value, type_map.get(pk_field, ""))

    # Natural key lookup
    if natural_key is not None and natural_key_field_names is not None:
        # Build filter kwargs from natural key
        filter_kwargs = {}
        for nk_field, nk_value in zip(natural_key_field_names, natural_key):
            filter_kwargs[nk_field] = nk_value

        existing = await model_class.objects.filter(**filter_kwargs).first()
        if existing is not None:
            # Update existing record
            all_fields = dict(fields)
            for nk_field, nk_value in zip(natural_key_field_names, natural_key):
                all_fields[nk_field] = nk_value
            columns_to_update = [
                c for c in all_fields if c != pk_field and c in valid_columns
            ]
            if columns_to_update:
                pk_vals = existing.pk_values
                set_clauses = ", ".join(
                    f'"{col}" = ${i + 1}' for i, col in enumerate(columns_to_update)
                )
                values = [all_fields[col] for col in columns_to_update]
                where = meta.pk_where_clause(start_param=len(values) + 1)
                values.extend(pk_vals)
                sql = f'UPDATE "{meta.table}" SET {set_clauses} WHERE {where}'
                await db.execute(sql, *values)
            return ("updated", None)
        else:
            # Insert new record with natural key fields
            all_fields = dict(fields)
            for nk_field, nk_value in zip(natural_key_field_names, natural_key):
                all_fields[nk_field] = nk_value
            columns = [c for c in meta.writable_columns if c in all_fields]
            values = [all_fields[c] for c in columns]
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            col_names = ", ".join(f'"{c}"' for c in columns)
            sql = f'INSERT INTO "{meta.table}" ({col_names}) VALUES ({placeholders})'
            await db.execute(sql, *values)
            return ("created", None)

    # PK-based upsert — atomic INSERT ... ON CONFLICT to avoid TOCTOU race
    if pk_value is not None:
        all_fields = dict(fields)
        all_fields[pk_field] = pk_value
        columns = [c for c in meta.column_names if c in all_fields]
        values = [all_fields[c] for c in columns]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
        col_names = ", ".join(f'"{c}"' for c in columns)
        # Build SET clause for ON CONFLICT (exclude PK from update)
        update_cols = [c for c in columns if c != pk_field]
        if update_cols:
            set_clauses = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            sql = (
                f'INSERT INTO "{meta.table}" ({col_names}) VALUES ({placeholders}) '
                f'ON CONFLICT ("{pk_field}") DO UPDATE SET {set_clauses}'
            )
        else:
            sql = (
                f'INSERT INTO "{meta.table}" ({col_names}) VALUES ({placeholders}) '
                f'ON CONFLICT ("{pk_field}") DO NOTHING'
            )
        # Check existence before upsert to distinguish created vs updated
        where = meta.pk_where_clause(start_param=1)
        existing = await db.query(
            f'SELECT 1 FROM "{meta.table}" WHERE {where} LIMIT 1',
            pk_value,
        )
        was_existing = len(existing) > 0
        await db.execute(sql, *values)
        return ("updated", None) if was_existing else ("created", None)
    else:
        # No PK — insert only (auto-generate PK)
        columns = [c for c in meta.writable_columns if c in fields]
        values = [fields[c] for c in columns]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
        col_names = ", ".join(f'"{c}"' for c in columns)
        sql = f'INSERT INTO "{meta.table}" ({col_names}) VALUES ({placeholders})'
        await db.execute(sql, *values)
        return ("created", None)


async def loaddata(
    source: str | list[dict[str, object]],
    db: object | None = None,
) -> LoadResult:
    """Load fixture data into the database.

    Accepts a JSON string, file path, or list of dicts. Each record must have:
    - "model": table name (str)
    - "pk": primary key value (optional)
    - "fields": dict of field name -> value

    Records are processed in order. FK dependencies are handled by retrying
    failed records after all others have been processed.

    Args:
        source: JSON string, file path, or list of fixture dicts.
        db: Database instance. If None, uses the global database.

    Returns:
        LoadResult with counts of created, updated, skipped, and errors.
    """
    if db is None:
        db = get_db()

    result = LoadResult()

    try:
        records = _parse_fixture_source(source)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    if not records:
        return result

    # Order records by a real FK topological sort of the referenced models so a
    # dependent row is loaded after the row it references (the old fk-COUNT sort
    # got this wrong whenever a 1-FK table pointed at a 2-FK table).
    referenced: list[type] = []
    seen_tables: set[str] = set()
    for rec in records:
        tbl = rec.get("model", "")
        if tbl not in seen_tables:
            seen_tables.add(tbl)
            m = _get_model_by_table(tbl)
            if m is not None:
                referenced.append(m)
    table_rank = {
        m._meta.table: i for i, m in enumerate(_sort_by_dependencies(referenced))
    }
    # Stable sort preserves in-table order; unknown tables sort last.
    records = sorted(
        records, key=lambda r: table_rank.get(r.get("model", ""), len(table_rank))
    )

    class _LoadRollback(Exception):
        """Internal sentinel: signals the atomic load to roll back on errors."""

    async def _apply_one(record_dict: dict[str, object]) -> tuple[str, str | None]:
        # Per-record SAVEPOINT: a failed record rolls back only its own
        # savepoint, leaving the outer transaction usable so the remaining
        # records (and retries) can still run.
        try:
            async with db.transaction():
                return await _upsert_record(record_dict, db)
        # A per-record failure (commonly an unsatisfied FK a later record provides)
        # is captured for retry; the savepoint already rolled the partial row back.
        # blind-except: per-record load failure is captured for the retry pass, not propagated
        except Exception as exc:
            return "error", str(exc)

    # Atomicity: the whole load runs in ONE transaction. If any record is still
    # failing after retries make no further progress, roll the entire load back
    # (all-or-nothing) rather than leave a half-applied fixture committed.
    try:
        async with db.transaction():
            # Retry loop: reprocess failed records until a full pass makes no
            # progress (handles intra-table ordering, e.g. self-referential FKs).
            pending = list(records)
            while pending:
                retry_queue: list[tuple[dict[str, object], str | None]] = []
                progressed = False
                for record_dict in pending:
                    status, error = await _apply_one(record_dict)
                    if status == "created":
                        result.created += 1
                        progressed = True
                    elif status == "updated":
                        result.updated += 1
                        progressed = True
                    else:
                        retry_queue.append((record_dict, error))
                if not progressed:
                    # Remaining records are genuine (non-ordering) errors.
                    for _rd, error in retry_queue:
                        result.errors.append(error or "Unknown error")
                        result.skipped += 1
                    break
                pending = [rd for rd, _ in retry_queue]

            if result.errors:
                raise _LoadRollback
    except _LoadRollback:
        # Outer transaction rolled back — nothing was committed. Reflect that the
        # created/updated rows did NOT persist; keep the errors/skipped detail.
        result.created = 0
        result.updated = 0

    return result
