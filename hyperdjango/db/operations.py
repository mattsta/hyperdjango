"""Database operations — inherits from Django's PostgreSQL operations.

Overrides compose_sql and last_executed_query to use our own SQL literal
quoting (_pg_quote_literal) instead of psycopg's mogrify, since we use
pg.zig for the wire protocol.
"""

from django.db.backends.postgresql.operations import (
    DatabaseOperations as PgOperations,
)

from hyperdjango.db.pgzig_connection import _pg_quote_literal


class DatabaseOperations(PgOperations):
    def adapt_json_value(self, value, encoder):
        """Override: pg.zig handles JSON serialization natively.

        We intercept the value here so _pg_quote_literal serializes dicts/lists
        to JSON directly, instead of wrapping them in a psycopg Jsonb adapter.
        """
        # Let _pg_quote_literal handle the serialization — it detects
        # Jsonb objects via hasattr(value, 'obj') and plain dicts directly.
        return super().adapt_json_value(value, encoder)

    def compose_sql(self, sql, params):
        """Client-side parameter binding for DDL and schema operations.

        Uses _pg_quote_literal for proper SQL escaping of all Python types
        including Binary, datetime, Decimal, UUID, bytes, etc.
        """
        if params is None:
            return sql
        try:
            if isinstance(params, dict):
                quoted = {k: _pg_quote_literal(v) for k, v in params.items()}
                return sql % quoted
            else:
                quoted = tuple(_pg_quote_literal(p) for p in params)
                return sql % quoted
        except TypeError, ValueError, KeyError:
            return sql

    def last_executed_query(self, cursor, sql, params):
        """Return the last executed query with params substituted."""
        try:
            return self.compose_sql(sql, params)
        # blind-except: last_executed_query is debug-only display; any literal-quoting failure (arbitrary param types) falls back to force_str formatting below, and must never propagate out of a logging path.
        except Exception:
            from django.utils.encoding import force_str

            if isinstance(params, (list, tuple)):
                u_params = tuple(
                    force_str(val, strings_only=True, errors="replace")
                    for val in params
                )
            elif params is None:
                u_params = ()
            elif isinstance(params, dict):
                u_params = {
                    k: force_str(v, strings_only=True, errors="replace")
                    for k, v in params.items()
                }
            else:
                u_params = params
            try:
                return sql % u_params
            except TypeError, ValueError, KeyError:
                return sql
