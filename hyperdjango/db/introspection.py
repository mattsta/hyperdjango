"""Database introspection — inherits from Django's PostgreSQL introspection."""

from django.db.backends.postgresql.introspection import (
    DatabaseIntrospection as PgIntrospection,
)


class DatabaseIntrospection(PgIntrospection):
    pass
