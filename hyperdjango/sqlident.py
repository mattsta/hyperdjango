"""SQL identifier & type safety — the ONE authority.

Every place a Python string becomes a SQL IDENTIFIER (column / alias / table /
CTE / index name) or a SQL TYPE name — i.e. text spliced into SQL rather than
bound as a ``$N`` parameter — validates here, so there is a single, consistent
policy instead of the several drifting per-site validators this module replaces.

Rules (forward-only; the strictest existing rule is the floor):
  * identifiers: non-empty, ASCII, first char ``[A-Za-z_]``, rest
    ``[A-Za-z0-9_]``, length ≤ 63 (Postgres NAMEDATALEN). This matches the
    unquoted-identifier grammar and cannot fail open. Dotted / operator-bearing
    / whitespace / quoted names are rejected — an alias never legitimately needs
    them; a column path is validated per ``__``-separated segment.
  * types: an allowlist of known base type names + an optional ``(n[,m])``
    precision and ``[]`` array suffix (a permissive char-class is insufficient —
    ``int) OR (SELECT …)`` would break out).
  * ``quote_identifier`` validates THEN escape-quotes (``"``→``""``) so a valid
    reserved-word field name (``order``) stays usable in DDL.

Import-cycle-free: this module imports nothing from the ORM; query / expressions
/ lookups / postgres / models / migrations import IT.
"""

import re

__all__ = [
    "IdentifierError",
    "validate_identifier",
    "validate_column_path",
    "validate_qualified_column",
    "validate_type",
    "quote_identifier",
    "escape_sql_literal",
]

# Postgres unquoted-identifier grammar, ASCII-only, NAMEDATALEN-bounded.
# Matched with fullmatch (NOT `$`, which would accept a trailing newline).
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_IDENT_LEN = 63

_IDENT_KINDS = frozenset({"column", "alias", "table", "cte", "index_name"})


class IdentifierError(ValueError):
    """A string that must be a SQL identifier/type failed validation."""


def validate_identifier(name: str, *, kind: str, source: str) -> str:
    """Validate a single SQL identifier of ``kind``; return it, or raise.

    ``kind`` ∈ column/alias/table/cte/index_name — one shared allowlist rule.
    ``source`` names the caller for the error message.
    """
    if kind not in _IDENT_KINDS:
        raise IdentifierError(f"unknown identifier kind {kind!r}")
    if not isinstance(name, str) or not name:
        raise IdentifierError(
            f"{source}: {kind} name must be a non-empty string, got {name!r}"
        )
    if len(name) > _MAX_IDENT_LEN:
        raise IdentifierError(
            f"{source}: {kind} name {name!r} exceeds {_MAX_IDENT_LEN} chars (Postgres NAMEDATALEN)"
        )
    if not _IDENT_RE.fullmatch(name):
        raise IdentifierError(
            f"{source}: {kind} name {name!r} is not a bare identifier "
            f"(ASCII letters/digits/underscore, not starting with a digit). "
            f"Refusing to build SQL from an unsafe {kind} reference."
        )
    return name


def validate_column_path(path: str, *, source: str) -> str:
    """Validate a ``field__lookup``-style column path segment-by-segment.

    Each ``__``-separated segment must be a bare column identifier. Legitimate
    multi-segment lookups (``author__name__icontains``, ``created__year__gte``)
    pass; a crafted key such as ``id IS NULL OR 1=1 --__isnull`` is rejected
    before it can reach SQL.
    """
    if not isinstance(path, str) or not path:
        raise IdentifierError(
            f"{source}: column path must be a non-empty string, got {path!r}"
        )
    for segment in path.split("__"):
        validate_identifier(segment, kind="column", source=source)
    return path


def validate_qualified_column(name: str, *, source: str) -> str:
    """Validate a possibly ``table.column``-qualified column reference.

    Each dot-separated part must be a bare identifier — for the raw column
    operands (aggregate column / FILTER key) that are interpolated qualified.
    """
    if not isinstance(name, str) or not name:
        raise IdentifierError(
            f"{source}: column must be a non-empty string, got {name!r}"
        )
    for part in name.split("."):
        validate_identifier(part, kind="column", source=source)
    return name


# ── SQL type validation (CAST / DDL ALTER TYPE) ──────────────────────────────
_TYPE_ALLOWED: frozenset[str] = frozenset(
    {
        "text",
        "varchar",
        "character varying",
        "char",
        "character",
        "bpchar",
        "int",
        "integer",
        "int2",
        "int4",
        "int8",
        "smallint",
        "bigint",
        "serial",
        "bigserial",
        "smallserial",
        "numeric",
        "decimal",
        "real",
        "float",
        "float4",
        "float8",
        "double precision",
        "money",
        "boolean",
        "bool",
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "timestamp with time zone",
        "timestamp without time zone",
        "time with time zone",
        "time without time zone",
        "interval",
        "uuid",
        "json",
        "jsonb",
        "bytea",
        "xml",
        "inet",
        "cidr",
        "macaddr",
        "macaddr8",
        "vector",
        "tsvector",
        "tsquery",
    }
)

_TYPE_RE = re.compile(
    r"^\s*(?P<base>[A-Za-z][A-Za-z ]*?)"  # base type name (letters/spaces)
    r"\s*(?:\(\s*\d+\s*(?:,\s*\d+\s*)?\))?"  # optional (n) or (n, m) precision
    r"\s*(?:\[\s*\])?\s*\Z"  # optional [] array suffix (\Z: no trailing newline)
)


def validate_type(type_name: str, *, source: str) -> str:
    """Validate a SQL type name (allowlist + shape); return it, or raise.

    Accepts ``int``, ``numeric(10,2)``, ``timestamptz``, ``int[]``, … Rejects
    anything whose base name isn't a recognized type or whose shape could carry
    an injection (``int) OR (SELECT …)``).
    """
    if not isinstance(type_name, str) or not type_name:
        raise IdentifierError(
            f"{source}: type must be a non-empty string, got {type_name!r}"
        )
    m = _TYPE_RE.match(type_name)
    if m is None:
        raise IdentifierError(
            f"{source}: type {type_name!r} is not a recognized SQL type "
            f"(expected e.g. 'int', 'text', 'numeric(10,2)', 'int[]')"
        )
    base = " ".join(m.group("base").lower().split())
    if base not in _TYPE_ALLOWED:
        raise IdentifierError(
            f"{source}: type {type_name!r} base {base!r} is not in the allowed SQL-type set"
        )
    return type_name


def quote_identifier(name: str, *, kind: str, source: str) -> str:
    """Validate then double-quote a DDL identifier (``order`` → ``"order"``).

    Validating first rejects control chars / over-length; quoting keeps a valid
    reserved-word field name usable. Embedded ``"`` is escaped as ``""`` (belt
    and braces — a valid identifier can't contain one).
    """
    validated = validate_identifier(name, kind=kind, source=source)
    return '"' + validated.replace('"', '""') + '"'


def escape_sql_literal(value: str) -> str:
    """Escape a string for a single-quoted SQL string LITERAL (``'`` → ``''``).

    For the rare developer-facing helpers that splice a constant (a delimiter, a
    ts config) into a quoted literal rather than binding it.
    """
    return value.replace("'", "''")
