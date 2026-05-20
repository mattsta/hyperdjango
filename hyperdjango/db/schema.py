"""Database schema editor — inherits from Django's PostgreSQL schema.

Overrides quote_value to use _pg_quote_literal (the single canonical
SQL literal quoting function) instead of psycopg's adapter system.
"""

from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as PgSchemaEditor,
)

from hyperdjango.db.pgzig_connection import _pg_quote_literal


class DatabaseSchemaEditor(PgSchemaEditor):
    def quote_value(self, value):
        """Quote a Python value as a PostgreSQL literal for DDL statements."""
        return _pg_quote_literal(value)
