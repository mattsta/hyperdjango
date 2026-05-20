"""Database client — inherits from Django's PostgreSQL client."""

from django.db.backends.postgresql.client import (
    DatabaseClient as PgClient,
)


class DatabaseClient(PgClient):
    pass
