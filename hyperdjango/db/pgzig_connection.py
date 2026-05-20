"""
pg.zig connection and cursor — PEP 249 compatible interface for Django.

Wraps the _hyperdjango_native Zig PostgreSQL driver to provide the
connection/cursor interface that Django's ORM expects.
"""

import datetime
import decimal
import logging
import re
import uuid as _uuid
from collections import namedtuple
from contextlib import suppress
from dataclasses import dataclass

import psycopg as _psycopg_mod

from hyperdjango.native import fast_json_dumps

_logger = logging.getLogger("hyperdjango.db")


def _fast_json_serialize(value):
    """Serialize a Python object to JSON string using native SIMD when available."""
    result = fast_json_dumps(value)
    if isinstance(result, bytes):
        return result.decode("utf-8")
    return result


# PEP 249 cursor.description column descriptor — matches psycopg's Column type.
# Includes type_display for psycopg 3.2+ compatibility (Django dev uses it).
Column = namedtuple(
    "Column",
    "name type_code display_size internal_size precision scale null_ok type_display",
)

# PEP 249 database errors — must subclass psycopg's error hierarchy
# so Django's DatabaseErrorWrapper.issubclass() checks work correctly.
_PsycopgError = _psycopg_mod.Error
_PsycopgDatabaseError = _psycopg_mod.DatabaseError
_PsycopgOperationalError = _psycopg_mod.OperationalError
_PsycopgProgrammingError = _psycopg_mod.ProgrammingError
_PsycopgIntegrityError = _psycopg_mod.IntegrityError
_PsycopgInterfaceError = _psycopg_mod.InterfaceError
_PsycopgInternalError = _psycopg_mod.InternalError
_PsycopgNotSupportedError = _psycopg_mod.NotSupportedError
_PsycopgDataError = _psycopg_mod.DataError
_PsycopgDuplicateDatabase = _psycopg_mod.errors.DuplicateDatabase
_PsycopgDuplicateTable = _psycopg_mod.errors.DuplicateTable


# Subclass psycopg errors so Django's DatabaseErrorWrapper.issubclass() works
class Error(_PsycopgError):
    pass


class DatabaseError(_PsycopgDatabaseError):
    pass


class OperationalError(_PsycopgOperationalError):
    pass


class ProgrammingError(_PsycopgProgrammingError):
    pass


class IntegrityError(_PsycopgIntegrityError):
    pass


class InterfaceError(_PsycopgInterfaceError):
    pass


class InternalError(_PsycopgInternalError):
    pass


class NotSupportedError(_PsycopgNotSupportedError):
    pass


class DataError(_PsycopgDataError):
    pass


class DuplicateDatabase(_PsycopgDuplicateDatabase):
    pass


class DuplicateTable(_PsycopgDuplicateTable):
    pass


def _update_timezone_from_sql(info, sql):
    """Update _PgZigConnectionInfo timezone from SET TIME ZONE or set_config SQL."""
    upper = sql.upper().strip()
    # RESET TIMEZONE — clears to server default (unknown to us)
    if "RESET" in upper and "TIMEZONE" in upper.replace(" ", ""):
        info._timezone = None
        return
    # SET TIME ZONE 'America/Chicago'
    m = re.search(r"TIME\s+ZONE\s+'([^']+)'", sql, re.IGNORECASE)
    if m:
        info._timezone = m.group(1)
        return
    # SELECT set_config('TimeZone', 'America/Chicago', false)
    m = re.search(r"set_config\s*\(\s*'TimeZone'\s*,\s*'([^']+)'", sql)
    if m:
        info._timezone = m.group(1)
        return


_POS_INF = float("inf")
_NEG_INF = float("-inf")


def _pg_quote_literal(value):
    """Quote a Python value as a PostgreSQL literal for client-side binding.

    It produces a safely-escaped SQL literal that can be embedded directly in
    the query — the same client-side "mogrify" technique a client cursor uses.
    """
    if value is None:
        return "NULL"
    elif isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        # Non-finite doubles have no bare SQL literal — repr() yields the tokens
        # inf/-inf/nan, which parse as (nonexistent) column names. Emit the
        # PG-parseable quoted forms instead (mirrors the native extractParams
        # path); repr() is shortest-round-trip for every finite value.
        if value != value:
            return "'NaN'::float8"
        elif value == _POS_INF:
            return "'Infinity'::float8"
        elif value == _NEG_INF:
            return "'-Infinity'::float8"
        return repr(value)
    elif isinstance(value, str):
        # Standard SQL escaping with standard_conforming_strings=on (PostgreSQL default).
        # Only single quotes need escaping — backslash is a regular character.
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    elif isinstance(value, bytes):
        # bytea hex format
        return f"'\\x{value.hex()}'::bytea"
    elif hasattr(value, "obj") and hasattr(value, "dumps"):
        # psycopg Jsonb/Json adapter — extract .obj (raw Python value) and serialize
        raw = value.obj
        json_str = value.dumps(raw) if value.dumps else _fast_json_serialize(raw)
        if isinstance(json_str, bytes):
            json_str = json_str.decode("utf-8")
        escaped = json_str.replace("'", "''")
        return f"'{escaped}'::jsonb"
    elif isinstance(value, dict):
        # Plain dict → JSON/JSONB — use native SIMD serializer
        json_str = _fast_json_serialize(value)
        escaped = json_str.replace("'", "''")
        return f"'{escaped}'::jsonb"
    elif isinstance(value, (list, tuple)):
        # Check if this is a JSON structure (contains dicts/lists) vs PG array
        if value and any(isinstance(x, (dict, list)) for x in value):
            json_str = _fast_json_serialize(value)
            escaped = json_str.replace("'", "''")
            return f"'{escaped}'::jsonb"
        # PostgreSQL array literal (flat values only)
        return "ARRAY[{}]".format(",".join(_pg_quote_literal(x) for x in value))
    elif isinstance(value, memoryview):
        return f"'\\x{bytes(value).hex()}'::bytea"
    elif hasattr(value, "tobytes"):
        # Buffer protocol objects (memoryview, etc.)
        return f"'\\x{value.tobytes().hex()}'::bytea"
    elif hasattr(value, "obj") and type(value).__name__ == "Binary":
        # psycopg Binary wrapper — .obj is the raw bytes
        raw = value.obj
        return f"'\\x{raw.hex()}'::bytea"
    else:
        # datetime, date, time, Decimal, UUID — use str representation
        if isinstance(value, datetime.datetime):
            return f"'{value.isoformat()}'::timestamptz"
        elif isinstance(value, datetime.date):
            return f"'{value.isoformat()}'::date"
        elif isinstance(value, datetime.time):
            # A tz-aware time must cast to timetz: isoformat() carries the
            # offset, but ::time would strip it and reinterpret the clock in the
            # session tz (silent tz loss). Naive times stay ::time.
            cast = "timetz" if value.tzinfo is not None else "time"
            return f"'{value.isoformat()}'::{cast}"
        elif isinstance(value, datetime.timedelta):
            # Emit the exact normalized components — total_seconds() is a float
            # and loses microsecond precision for large intervals (silent
            # corruption). PG interval fields are additive, so the component
            # form round-trips negative timedeltas correctly too.
            return (
                f"'{value.days} days {value.seconds} seconds "
                f"{value.microseconds} microseconds'::interval"
            )
        elif isinstance(value, decimal.Decimal):
            return f"'{value}'::numeric"
        elif isinstance(value, _uuid.UUID):
            return f"'{value}'::uuid"
        else:
            # Fallback — str() and quote
            escaped = str(value).replace("'", "''")
            return f"'{escaped}'"


def _mogrify(sql, params):
    """Substitute parameters into SQL string (client-side binding).

    This is the production-correct approach matching psycopg3's ClientCursor:
    parameters are safely escaped and embedded into the SQL string, which is
    then sent as a simple query with no parameter placeholders.

    Handles both positional (%s) and named (%(name)s) parameter styles.
    """
    if params is None:
        return sql

    if isinstance(params, dict):
        # Named params: %(name)s
        quoted = {k: _pg_quote_literal(v) for k, v in params.items()}
        return sql % quoted
    else:
        # Positional params: %s
        quoted = tuple(_pg_quote_literal(p) for p in params)
        return sql % quoted


def _pg_array_literal(items):
    """Convert a Python list to a PostgreSQL array literal string.

    [1, 2, 3] → '{1,2,3}'
    ['hello', 'world'] → '{hello,world}'
    ['has "quotes"'] → '{"has \\"quotes\\"}'
    """
    parts = []
    for item in items:
        if item is None:
            parts.append("NULL")
        elif isinstance(item, str):
            # Escape backslashes and double quotes, wrap in double quotes
            escaped = item.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        elif isinstance(item, bool):
            parts.append("true" if item else "false")
        elif isinstance(item, (int, float)):
            parts.append(str(item))
        elif isinstance(item, (list, tuple)):
            # Nested array
            parts.append(_pg_array_literal(item))
        else:
            parts.append(f'"{item}"')
    return "{" + ",".join(parts) + "}"


_PsycopgInvalidParameterValue = _psycopg_mod.errors.InvalidParameterValue


class InvalidParameterValue(_PsycopgInvalidParameterValue):
    pass


# The PostgreSQL error-message fragments that identify a unique/duplicate-key
# violation. Defined once so `_classify_pg_error` (which turns the message into a
# typed IntegrityError) and `is_unique_violation` (which narrows an IntegrityError
# to the unique-constraint case) key off the identical contract and can never
# drift.
_UNIQUE_VIOLATION_SUBSTRINGS = ("duplicate key", "unique constraint")


def _classify_pg_error(msg):
    """Classify a PostgreSQL error message into the appropriate exception type."""
    lower = msg.lower()
    if "already exists" in msg:
        if "database" in lower:
            return DuplicateDatabase(msg)
        if "relation" in lower or "table" in lower:
            return DuplicateTable(msg)
    if any(s in lower for s in _UNIQUE_VIOLATION_SUBSTRINGS):
        return IntegrityError(msg)
    if "foreign key" in lower or "violates" in lower:
        return IntegrityError(msg)
    if "role" in lower and "does not exist" in lower:
        return InvalidParameterValue(msg)
    if "invalid" in lower and ("value" in lower or "parameter" in lower):
        return InvalidParameterValue(msg)
    if "syntax error" in lower:
        return ProgrammingError(msg)
    if "does not exist" in lower:
        return ProgrammingError(msg)
    if "connection" in lower or "unavailable" in lower:
        return OperationalError(msg)
    if "permission denied" in lower:
        return OperationalError(msg)
    if "being accessed by other users" in lower:
        return OperationalError(msg)
    return DatabaseError(msg)


# The PostgreSQL error-message fragments that identify an undefined-table
# error (SQLSTATE 42P01, `relation "x" does not exist`). Defined once so
# `is_undefined_table` keys off the same message contract `_classify_pg_error`
# uses and can never drift. The "column" exclusion keeps undefined-COLUMN
# errors (`column "c" of relation "t" does not exist`, SQLSTATE 42703) out —
# those are genuine schema bugs, not a missing table.
_UNDEFINED_TABLE_SUBSTRINGS = ('relation "', "does not exist")


def is_undefined_table(exc: BaseException) -> bool:
    """Whether ``exc`` is specifically a PostgreSQL undefined-table error.

    Both dispatch paths surface a missing relation as a typed
    :class:`ProgrammingError`, but that class also covers syntax errors and
    undefined columns/functions. This predicate NARROWS to the missing-TABLE
    case by the exact message shape PostgreSQL emits for SQLSTATE 42P01.
    Callers that treat "table not provisioned yet" as a benign
    not-configured state (e.g. guard timeline checks) use this to
    distinguish it from real DB errors that must fail closed.
    """
    lower = str(exc).lower()
    if "column" in lower:
        return False
    return all(s in lower for s in _UNDEFINED_TABLE_SUBSTRINGS)


def is_unique_violation(exc: BaseException) -> bool:
    """Whether ``exc`` is specifically a PostgreSQL UNIQUE / duplicate-key violation.

    Every constraint violation on BOTH dispatch paths surfaces as a typed
    :class:`IntegrityError` (the native direct-SQL / ORM path classifies at the
    FFI boundary, exactly like the psycopg-compatible cursor path), so a bare
    ``isinstance(exc, IntegrityError)`` does not single out a duplicate key —
    it also matches foreign-key, not-null, and check violations. This predicate
    NARROWS to the unique case by the ``_UNIQUE_VIOLATION_SUBSTRINGS`` fragments
    every unique violation carries in its message. That is the ergonomic
    distinction ``get_or_create`` / ``update_or_create`` need to turn a lost
    insert race into a clean re-read while re-raising every OTHER IntegrityError.
    """
    lower = str(exc).lower()
    return any(s in lower for s in _UNIQUE_VIOLATION_SUBSTRINGS)


# Native pg.zig — always available
from hyperdjango._hyperdjango_native import (
    _db_clear_stmt_cache,
    _db_close_pool,
    _db_configure,
    _db_conn_acquire,
    _db_conn_execute,
    _db_conn_release,
    _db_copy_from,
    _db_copy_to,
    _db_execute,
    _db_get_last_columns,
    _db_list_enums,
    _db_query,
    _db_register_enum,
    _db_release_thread_conn,
    _db_reset_stmt_cache_stats,
    _db_stmt_cache_stats,
)


@dataclass(slots=True)
class StmtCacheStats:
    """Snapshot of prepared statement cache statistics from pg.zig.

    All counters are cumulative since process start or last reset_stmt_cache_stats().
    """

    hits: int
    misses: int
    evictions: int
    entries: int
    max_entries: int

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as a fraction [0.0, 1.0]. Returns 0.0 if no lookups."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    @property
    def total_lookups(self) -> int:
        """Total cache lookups (hits + misses)."""
        return self.hits + self.misses


def stmt_cache_stats() -> StmtCacheStats:
    """Get current prepared statement cache statistics from pg.zig.

    Returns a StmtCacheStats snapshot with hit/miss/eviction counters,
    current entry count, and max capacity.
    """
    raw = _db_stmt_cache_stats()
    return StmtCacheStats(
        hits=raw["hits"],
        misses=raw["misses"],
        evictions=raw["evictions"],
        entries=raw["entries"],
        max_entries=raw["max_entries"],
    )


def reset_stmt_cache_stats() -> None:
    """Reset prepared statement cache counters to zero. Does NOT clear cached statements."""
    _db_reset_stmt_cache_stats()


class PgZigConnection:
    """PEP 249-compatible connection wrapping pg.zig native driver.

    Provides the interface Django's DatabaseWrapper expects:
    - cursor() → PgZigCursor
    - commit() / rollback()
    - autocommit property
    - close()
    - info.parameter_status()

    Falls back to psycopg when native extension not compiled.
    """

    def __init__(self, host="localhost", port=5432, dbname="", user="", password=""):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self._autocommit = False
        self._closed = False
        self._native_conn = None
        self._fallback_conn = None
        self.isolation_level = None
        self.cursor_factory = PgZigCursor
        self._pinned_handle = None
        self._in_transaction = False
        self._pool_handle = None
        self._info = _PgZigConnectionInfo()

    def _create_pool(self, conn_str: str) -> int:
        """Create a pg.zig pool from settings. Returns pool handle."""
        from hyperdjango.conf import get_setting

        return _db_configure(
            conn_str,
            get_setting("POOL_SIZE"),
            get_setting("CONNECT_TIMEOUT"),
            get_setting("QUERY_TIMEOUT"),
        )

    def connect(self):
        """Establish the connection.

        If the database doesn't exist yet (common during Django test setup),
        stores the connection string for deferred connection. The pool will
        be created on first actual use via _ensure_pool().
        """
        # Use pg.zig via the native extension
        self._conn_str = f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}"
        try:
            self._pool_handle = self._create_pool(self._conn_str)
        except RuntimeError:
            # Database may not exist yet (Django test infrastructure creates
            # the connection object before creating the test database).
            # Defer pool creation until first use.
            self._pool_handle = None
        self._native_conn = True

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        old_value = self._autocommit
        self._autocommit = value
        if self._fallback_conn is not None:
            self._fallback_conn.autocommit = value
        elif self._native_conn:
            if not value and old_value:
                # Entering non-autocommit: pin connection + BEGIN
                self._ensure_pinned()
            elif value and not old_value and self._pinned_handle is not None:
                # Leaving non-autocommit: COMMIT + release pinned
                try:
                    _db_conn_execute(self._pinned_handle, "COMMIT", [])
                except RuntimeError:
                    # COMMIT failed — ROLLBACK before releasing
                    with suppress(RuntimeError):
                        _db_conn_execute(self._pinned_handle, "ROLLBACK", [])
                self._in_transaction = False
                _db_conn_release(self._pinned_handle)
                self._pinned_handle = None

    def cursor(self, name=None):
        """Create a new cursor.

        If name is provided, creates a server-side cursor using
        DECLARE CURSOR for memory-efficient iteration over large result sets.
        Respects cursor_factory if set (Django's OPTIONS["cursor_factory"]).
        """
        if self._fallback_conn is not None:
            if name:
                raw = self._fallback_conn.cursor(name=name)
            else:
                raw = self._fallback_conn.cursor()
            return PgZigCursor(raw, native=False)
        elif self._native_conn:
            self._ensure_pool()
            if name:
                return PgZigServerCursor(name=name, connection=self)
            cursor = PgZigCursor(None, native=True, connection=self)
            # If a custom cursor_factory is set and it's not PgZigCursor,
            # wrap or adapt as needed for Django compatibility
            if (
                self.cursor_factory is not None
                and self.cursor_factory is not PgZigCursor
            ):
                cursor._cursor_factory = self.cursor_factory
            return cursor
        raise RuntimeError("Not connected")

    def commit(self):
        if self._fallback_conn is not None:
            self._fallback_conn.commit()
        elif self._native_conn and self._pinned_handle is not None:
            try:
                _db_conn_execute(self._pinned_handle, "COMMIT", [])
            except RuntimeError as e:
                # A COMMIT can genuinely fail at the server — e.g. a
                # DEFERRABLE INITIALLY DEFERRED constraint firing at commit
                # time. PostgreSQL has already rolled the transaction back and
                # the write is GONE. Swallowing this (the old
                # `with suppress(RuntimeError)`) reported success while
                # silently losing data — under Django ATOMIC_REQUESTS the
                # client gets 2xx for a write that never landed. Release the
                # pinned connection and surface the classified error so Django
                # returns 500.
                self._in_transaction = False
                _db_conn_release(self._pinned_handle)
                self._pinned_handle = None
                raise _classify_pg_error(str(e)) from e
            self._in_transaction = False
            _db_conn_release(self._pinned_handle)
            self._pinned_handle = None

    def rollback(self):
        if self._fallback_conn is not None:
            self._fallback_conn.rollback()
        elif self._native_conn and self._pinned_handle is not None:
            with suppress(RuntimeError):
                _db_conn_execute(self._pinned_handle, "ROLLBACK", [])
            self._in_transaction = False
            _db_conn_release(self._pinned_handle)
            self._pinned_handle = None
            # ROLLBACK reverts any SET TIME ZONE done in the transaction.
            # Reset tracked timezone so _configure_timezone will re-set it.
            self._info._timezone = None

    def _ensure_pool(self):
        """Create the pool if it was deferred during connect().

        This is called before any operation that needs the pool.
        Raises RuntimeError if the database is still unreachable.
        """
        if (
            self._pool_handle is None
            and self._native_conn
            and hasattr(self, "_conn_str")
        ):
            self._pool_handle = self._create_pool(self._conn_str)

    def _ensure_pinned(self):
        """Acquire a pinned connection and start a transaction.

        pg.zig connections autocommit by default, so we must send BEGIN
        explicitly. The pinned handle is passed explicitly to each call
        via the negative-encoded handle — no global state mutation.
        """
        if self._pinned_handle is None:
            self._ensure_pool()
            self._pinned_handle = _db_conn_acquire(
                self._pool_handle if self._pool_handle is not None else -1
            )
        if not self._in_transaction and self._pinned_handle is not None:
            _db_conn_execute(self._pinned_handle, "BEGIN", [])
            self._in_transaction = True
        return self._pinned_handle

    def close(self):
        """Close the connection and its pool.

        Each connect() creates a new pool via _db_configure, so we must
        close the pool here to avoid leaking PostgreSQL connections.
        Without this, each Django connection open/close cycle leaks 2
        PostgreSQL connections, quickly exhausting max_connections.
        """
        self._closed = True
        if self._fallback_conn is not None:
            self._fallback_conn.close()
            self._fallback_conn = None
        if self._native_conn:
            # Release pinned connection if held
            if self._pinned_handle is not None:
                try:
                    if self._in_transaction:
                        _db_conn_execute(self._pinned_handle, "ROLLBACK", [])
                except RuntimeError:
                    pass
                _db_conn_release(self._pinned_handle)
                self._pinned_handle = None
                self._in_transaction = False
            # Release thread-owned connection back to pool before closing
            if self._pool_handle is not None:
                with suppress(RuntimeError):
                    _db_release_thread_conn(self._pool_handle)
            # Close the pool — each connect() creates a new one
            if self._pool_handle is not None:
                with suppress(RuntimeError):
                    _db_close_pool(self._pool_handle)
                self._pool_handle = None
        self._native_conn = None

    @property
    def connection(self):
        """Self-reference for psycopg sql.Literal.as_string() compatibility.

        psycopg's sql module accesses context.connection to get encoding info.
        For psycopg's own connection class, .connection returns the pgconn.
        We return self since we handle encoding directly.
        """
        return self

    @property
    def closed(self):
        if self._fallback_conn is not None:
            return self._fallback_conn.closed
        return self._closed

    @property
    def info(self):
        """Connection info object (for timezone detection etc.)."""
        if self._fallback_conn is not None and hasattr(self._fallback_conn, "info"):
            return self._fallback_conn.info
        return self._info

    @property
    def pgconn(self):
        """Stub for psycopg's internal pgconn — provides server info."""
        if self._fallback_conn is not None and hasattr(self._fallback_conn, "pgconn"):
            return self._fallback_conn.pgconn
        return _PgZigPgConn()

    @property
    def adapters(self):
        """Stub for psycopg's adapters registry."""
        if self._fallback_conn is not None and hasattr(self._fallback_conn, "adapters"):
            return self._fallback_conn.adapters
        return _PgZigAdapters()

    # ── Custom enum type registration ──────────────────────────────────────

    # Maps Python enum classes to their PostgreSQL type names.
    # Populated by register_enum() — used by Python-side code to convert
    # raw string labels from pg.zig back to enum instances.
    _enum_map: dict[str, type] = {}

    def register_enum(self, type_name: str, enum_class=None):
        """Register a custom PostgreSQL enum type for native OID handling.

        After registration, queries returning this enum type will return
        string labels instead of raw bytes. If enum_class is provided,
        the labels will be automatically converted to Python enum instances.

        Args:
            type_name: PostgreSQL type name (e.g. 'mood')
            enum_class: Optional Python enum.Enum subclass for auto-conversion
        """
        self._ensure_pool()
        if self._pool_handle is not None:
            oid = _db_register_enum(self._pool_handle, type_name)
            if oid > 0 and enum_class is not None:
                PgZigConnection._enum_map[type_name] = enum_class
            return oid
        return 0

    def discover_enums(self):
        """Discover and auto-register all enum types in the database.

        Returns a dict mapping type names to their label lists:
        {'mood': ['happy', 'sad', 'neutral'], 'status': ['active', 'inactive']}
        """
        self._ensure_pool()
        if self._pool_handle is not None:
            return _db_list_enums(self._pool_handle)
        return {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PgZigCursor:
    """PEP 249-compatible cursor wrapping pg.zig or psycopg.

    Provides: execute(), fetchone(), fetchmany(), fetchall(),
    close(), description, rowcount.
    """

    def __init__(self, raw_cursor, native=False, connection=None):
        self._cursor = raw_cursor
        self._native = native
        self._connection = connection
        self.connection = connection  # Django expects cursor.connection
        self._description = None
        self._rowcount = -1
        self._results = None
        self._result_index = 0
        self._closed = False
        self.statusmessage = None  # Django checks this for some operations

    @staticmethod
    def _convert_params(sql, params):
        """Convert Django's %s/%%(name)s params to PostgreSQL's $1, $2... format.

        Positional: "SELECT * FROM t WHERE id = %s", [1] → "$1", [1]
        Named: "SELECT * FROM t WHERE id = %(id)s", {'id': 1} → "$1", [1]
        Lists: [1, 2, 3] → "{1,2,3}" (PostgreSQL array literal)
        """
        if params is None:
            return sql, []

        if isinstance(params, dict):
            # Named params: %(name)s → $1, $2, ... with ordered values
            ordered_params = []
            name_to_index = {}

            def replace_named(match):
                name = match.group(1)
                if name not in name_to_index:
                    name_to_index[name] = len(ordered_params) + 1
                    val = params[name]
                    if isinstance(val, (list, tuple)):
                        ordered_params.append(_pg_array_literal(val))
                    elif val is None:
                        ordered_params.append(None)
                    else:
                        ordered_params.append(val)
                return f"${name_to_index[name]}"

            converted_sql = re.sub(r"%\((\w+)\)s", replace_named, sql)
            return converted_sql, ordered_params
        else:
            # Positional params: %s → $1, $2, ...
            converted_params = []
            for p in params:
                if p is None:
                    converted_params.append(None)
                elif isinstance(p, (list, tuple)):
                    converted_params.append(_pg_array_literal(p))
                else:
                    converted_params.append(p)

            param_index = [0]

            def replace_placeholder(match):
                param_index[0] += 1
                return f"${param_index[0]}"

            converted_sql = re.sub(r"%s", replace_placeholder, sql)
            return converted_sql, list(converted_params)

    def execute(self, sql, params=None):
        """Execute a SQL statement.

        Uses client-side parameter binding (like psycopg3's ClientCursor):
        parameters are escaped and substituted into the SQL string Python-side,
        then sent as a plain query with no placeholders. This avoids PostgreSQL
        parameter type-ambiguity issues.
        """
        if self._closed:
            raise InterfaceError("cursor already closed")
        if self._cursor is not None:
            # psycopg fallback cursor
            self._cursor.execute(sql, params)
            self._description = self._cursor.description
            self._rowcount = self._cursor.rowcount
        elif self._native:
            # Client-side binding: substitute params into SQL
            final_sql = _mogrify(sql, params) if params else sql

            # Determine if this returns rows or just a rowcount
            stripped = final_sql.strip().upper()
            has_returning = "RETURNING" in stripped
            is_query = (
                stripped.startswith(("SELECT", "SHOW", "WITH", "EXPLAIN"))
                or has_returning
            )
            # Detect DDL — must invalidate prepared statement cache after execution
            is_ddl = stripped.startswith(("ALTER ", "DROP ", "CREATE ", "TRUNCATE "))

            try:
                # Get handle from connection — explicit per-call, no global state.
                # Pool handle >= 0: acquires from pool.
                # Pinned handle < -1: uses pinned connection (for transactions).
                # Encoding: pinned slot N → handle = -(N + 2)
                conn = self._connection
                pool_h = conn._pool_handle if conn is not None else -1

                # If in transaction, use pinned connection for ALL operations
                # (both SELECT and DML) to maintain transaction isolation.
                if conn is not None and not conn._autocommit:
                    pinned = conn._ensure_pinned()
                    pinned_h = -(pinned + 2)  # encode pinned slot as negative

                    if is_query:
                        self._results = _db_query(pinned_h, final_sql, [])
                        self._result_index = 0
                        self._rowcount = len(self._results)
                        self._description = None
                    else:
                        rowcount = _db_conn_execute(pinned, final_sql, [])
                        self._results = None
                        self._rowcount = rowcount
                        self._description = None
                else:
                    # Autocommit mode: use pool directly
                    if is_query:
                        self._results = _db_query(pool_h, final_sql, [])
                        self._result_index = 0
                        self._rowcount = len(self._results)
                        self._description = None
                    else:
                        rowcount = _db_execute(pool_h, final_sql, [])
                        self._results = None
                        self._rowcount = rowcount
                        self._description = None
                # After DDL, invalidate prepared statement cache
                if is_ddl:
                    with suppress(Exception):
                        _db_clear_stmt_cache()
                # Track timezone changes for info.parameter_status()
                if conn is not None and hasattr(conn, "_info"):
                    if (
                        "TIME" in stripped
                        or "RESET" in stripped
                        or "set_config" in final_sql
                    ):
                        _update_timezone_from_sql(conn._info, final_sql)
            except RuntimeError as e:
                raise _classify_pg_error(str(e)) from e

    def executemany(self, sql, param_list):
        """Execute a SQL statement with multiple parameter sets.

        For INSERT statements, builds a single multi-row INSERT for one
        network roundtrip instead of N separate executions.
        """
        if self._cursor is not None:
            self._cursor.executemany(sql, param_list)
            self._rowcount = self._cursor.rowcount
        elif self._native:
            param_list = list(param_list)  # materialize iterators
            if not param_list:
                self._rowcount = 0
                return

            stripped = sql.strip().upper()
            # Optimize INSERT with VALUES — build single multi-row statement
            if (
                stripped.startswith("INSERT")
                and "VALUES" in stripped
                and len(param_list) > 1
            ):
                try:
                    self._executemany_batch_insert(sql, param_list)
                    return
                except ValueError:
                    # SQL shape isn't batchable (no VALUES(...) pattern etc.).
                    # This is the only legitimate reason to degrade — fall
                    # through to row-by-row.
                    pass
                except Exception:
                    # A real execution failure (constraint/integrity/type
                    # error). Row-by-row would hit the same error but only
                    # AFTER partially applying rows outside a transaction —
                    # silent partial writes. Don't hide it; re-raise.
                    _logger.warning(
                        "executemany batch insert failed; not degrading to "
                        "row-by-row (would partially apply)",
                        exc_info=True,
                    )
                    raise

            # Default: row-by-row execution
            total = 0
            for params in param_list:
                self.execute(sql, params)
                if self._rowcount > 0:
                    total += self._rowcount
            self._rowcount = total

    def _executemany_batch_insert(self, sql, param_list):
        """Build a single multi-row INSERT from executemany params.

        INSERT INTO t (a, b) VALUES (%s, %s) with [[1,'a'], [2,'b']]
        becomes: INSERT INTO t (a, b) VALUES (1,'a'), (2,'b')
        """
        # Find the VALUES (...) pattern
        match = re.search(
            r"(.*VALUES\s*)\(([^)]+)\)(.*)", sql, re.IGNORECASE | re.DOTALL
        )
        if not match:
            raise ValueError("Cannot batch: no VALUES pattern found")

        prefix = match.group(1)  # "INSERT INTO t (a, b) VALUES "
        suffix = match.group(3)  # trailing (RETURNING, etc.)

        # Build multi-row values
        rows = []
        for params in param_list:
            quoted = [_pg_quote_literal(p) for p in params]
            rows.append("(" + ",".join(quoted) + ")")

        final_sql = prefix + ",".join(rows) + suffix
        self.execute(final_sql)
        self._rowcount = len(param_list)

    def fetchone(self):
        if self._cursor is not None:
            return self._cursor.fetchone()
        if self._results is not None and self._result_index < len(self._results):
            row = self._results[self._result_index]
            self._result_index += 1
            return row
        return None

    def fetchmany(self, size=None):
        if self._cursor is not None:
            return self._cursor.fetchmany(size) if size else self._cursor.fetchmany()
        if self._results is None:
            return []
        if size is None:
            size = 1
        end = min(self._result_index + size, len(self._results))
        rows = self._results[self._result_index : end]
        self._result_index = end
        return rows

    def fetchall(self):
        if self._cursor is not None:
            return self._cursor.fetchall()
        if self._results is None:
            return []
        rows = self._results[self._result_index :]
        self._result_index = len(self._results)
        return rows

    @property
    def description(self):
        if self._cursor is not None:
            return self._cursor.description
        # Lazily build description from cached column metadata (native path).
        # Returns Column namedtuples matching PEP 249 / psycopg interface.
        if self._description is None and self._native and self._results is not None:
            columns = _db_get_last_columns()
            if columns:
                self._description = [
                    Column(col[0], col[1], None, None, None, None, None, None)
                    if isinstance(col, tuple)
                    else Column(col, None, None, None, None, None, None, None)
                    for col in columns
                ]
        return self._description

    @property
    def rowcount(self):
        if self._cursor is not None:
            return self._cursor.rowcount
        return self._rowcount

    @property
    def lastrowid(self):
        if self._cursor is not None and hasattr(self._cursor, "lastrowid"):
            return self._cursor.lastrowid
        return None

    @property
    def closed(self):
        if self._cursor is not None:
            # dynamic-attr: ``self._cursor`` is a foreign raw cursor (pg.zig native or psycopg); its optional ``closed`` attribute is not present on every backend
            return getattr(self._cursor, "closed", False)
        return self._closed

    def close(self):
        if self._cursor is not None:
            self._cursor.close()
        self._closed = True

    def copy(self, sql):
        """Execute a COPY command via the PostgreSQL COPY protocol.

        COPY TO: returns a CopyResult with .rows (list of tab-delimited strings)
        COPY FROM: returns a CopyWriter to send rows

        Usage:
            # COPY TO (export)
            with cursor.copy("COPY table TO STDOUT") as copy:
                for row in copy.rows():
                    print(row)

            # COPY FROM (import)
            with cursor.copy("COPY table FROM STDIN") as copy:
                copy.write_row("col1\tcol2\n")
                copy.write_row("val1\tval2\n")
        """
        if self._native and self._connection is not None:
            return _CopyContext(self._connection, sql)
        elif self._cursor is not None and hasattr(self._cursor, "copy"):
            return self._cursor.copy(sql)
        raise NotSupportedError("COPY not supported without native extension")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __iter__(self):
        return iter(self.fetchall())


class _CopyContext:
    """Context manager for PostgreSQL COPY protocol operations.

    Usage:
        # COPY TO (read rows from server)
        with cursor.copy("COPY table TO STDOUT") as copy:
            for row in copy.rows():
                process(row)

        # COPY FROM (write rows to server)
        with cursor.copy("COPY table FROM STDIN") as copy:
            copy.write_row("col1\\tcol2\\n")
    """

    def __init__(self, connection, sql):
        self._conn = connection
        self._sql = sql
        self._is_copy_from = "FROM STDIN" in sql.upper()
        self._pinned = None
        self._rows_buffer = []

    def __enter__(self):
        # Acquire a pinned connection for the COPY operation
        self._conn._ensure_pool()
        self._pinned = _db_conn_acquire(
            self._conn._pool_handle if self._conn._pool_handle is not None else -1
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._pinned is not None:
            if self._is_copy_from and self._rows_buffer and exc_type is None:
                # Flush buffered rows
                with suppress(Exception):
                    _db_copy_from(self._pinned, self._sql, self._rows_buffer)
            _db_conn_release(self._pinned)
            self._pinned = None
        return False

    def rows(self):
        """Read all COPY TO rows from the server. Returns an iterator."""
        if self._is_copy_from:
            raise ProgrammingError("Cannot read rows from COPY FROM")
        result = _db_copy_to(self._pinned, self._sql)
        return iter(result)

    def write_row(self, row):
        """Buffer a row for COPY FROM. Sent on context exit."""
        if not self._is_copy_from:
            raise ProgrammingError("Cannot write rows to COPY TO")
        self._rows_buffer.append(row)

    def write_rows(self, rows):
        """Buffer multiple rows for COPY FROM."""
        self._rows_buffer.extend(rows)


class PgZigServerCursor:
    """Server-side cursor using DECLARE CURSOR + FETCH for large result streaming.

    Uses O(1) memory regardless of result set size by fetching in chunks.

    Usage:
        cursor = conn.cursor(name="large_query")
        cursor.execute("SELECT * FROM big_table")
        for row in cursor:  # fetches in chunks of itersize
            process(row)
    """

    def __init__(self, name, connection, itersize=2000):
        self.name = name
        self._connection = connection
        self.itersize = itersize
        self._declared = False
        self._exhausted = False
        self._results = []
        self._result_index = 0
        self.rowcount = -1
        self._description = None
        self._closed = False

    def execute(self, sql, params=None):
        """Execute a query using a server-side cursor."""
        if params:
            sql = _mogrify(sql, params)

        # Ensure we're in a transaction (server-side cursors require one).
        # _ensure_pinned() already sends BEGIN when the pinned connection is
        # not yet in a transaction — issuing an extra explicit BEGIN here
        # produced a "there is already a transaction in progress" double-BEGIN.
        conn = self._connection
        conn._ensure_pinned()

        # Declare the cursor
        declare_sql = f'DECLARE "{self.name}" CURSOR FOR {sql}'
        _db_conn_execute(conn._pinned_handle, declare_sql, [])
        self._declared = True
        self._exhausted = False

    def fetchone(self):
        if self._result_index < len(self._results):
            row = self._results[self._result_index]
            self._result_index += 1
            return row
        if self._exhausted:
            return None
        self._fetch_chunk()
        if not self._results:
            return None
        self._result_index = 1
        return self._results[0]

    def fetchmany(self, size=None):
        size = size or self.itersize
        rows = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self):
        rows = []
        while True:
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def _fetch_chunk(self):
        """Fetch the next chunk of rows from the server."""
        if self._exhausted or not self._declared:
            self._results = []
            return

        conn = self._connection
        pinned_h = -(conn._pinned_handle + 2)
        fetch_sql = f'FETCH {self.itersize} FROM "{self.name}"'
        try:
            self._results = _db_query(pinned_h, fetch_sql, [])
        except RuntimeError as e:
            # A FETCH failure (cursor already closed, aborted transaction,
            # connection error) must surface as an error — not be swallowed
            # into an empty result set that reads as normal exhaustion and
            # silently truncates the stream. Mark exhausted so we don't retry
            # a broken cursor, then raise the classified PG error.
            self._exhausted = True
            raise _classify_pg_error(str(e)) from e
        self._result_index = 0

        if len(self._results) < self.itersize:
            self._exhausted = True

    @property
    def description(self):
        return self._description

    def close(self):
        if self._declared and not self._closed:
            try:
                conn = self._connection
                if conn._pinned_handle is not None:
                    _db_conn_execute(conn._pinned_handle, f'CLOSE "{self.name}"', [])
            # blind-except: best-effort server-side cursor CLOSE during teardown — a failed CLOSE must not mask the exception unwinding a `with` block; logged (not silently swallowed) so genuine problems stay visible.
            except Exception:
                # Best-effort cleanup: a failed CLOSE (already-closed cursor,
                # aborted tx) must not mask the real exception unwinding a
                # `with` block, but silently swallowing it hides genuine
                # problems. Log it instead of `pass`.
                _logger.warning(
                    "failed to close server-side cursor %r", self.name, exc_info=True
                )
        self._closed = True

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _PgZigConnectionInfo:
    """Connection info for pg.zig — timezone, encoding, server version."""

    encoding = "utf-8"

    def __init__(self):
        self._timezone = "UTC"

    def parameter_status(self, name):
        if name == "TimeZone":
            return self._timezone
        if name == "server_version":
            return "160000"
        if name == "standard_conforming_strings":
            return "on"
        return None


class _PgZigAdapters:
    """Stub for psycopg's adapters registry — pg.zig handles types natively."""

    def get_loader(self, oid, fmt):
        """Return a stub loader with UTC timezone."""
        import datetime

        return type("TzLoader", (), {"timezone": datetime.UTC})()

    def register_loader(self, type_name, loader_class):
        pass  # pg.zig handles all type loading natively

    def register_dumper(self, type_class, dumper_class):
        pass


class _PgZigPgConn:
    """Stub for psycopg's pgconn — provides server info Django needs."""

    _encoding = "utf-8"

    @property
    def server_version(self):
        """Return PostgreSQL server version as integer (e.g., 160000 for 16.0)."""
        return 160000

    @property
    def transaction_status(self):
        # PQTRANS_IDLE = 0
        return 0

    def parameter_status(self, name):
        if isinstance(name, bytes):
            name = name.decode()
        if name == "server_version":
            return b"16.0"
        if name == "TimeZone":
            return b"UTC"
        if name == "standard_conforming_strings":
            return b"on"
        return None
