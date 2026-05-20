"""Database creation — inherits from Django's PostgreSQL creation."""

import contextlib

from django.db.backends.postgresql.creation import (
    DatabaseCreation as PgCreation,
)


class DatabaseCreation(PgCreation):
    def _destroy_test_db(self, test_database_name, verbosity):
        """Close all pg.zig pools before dropping the test database.

        PostgreSQL refuses DROP DATABASE while connections are open.
        Close only the pools connected to the test database, not ALL pools
        (the _nodb_cursor needs a pool to the postgres database).
        """
        # Close the Django-level connection (this closes its pool via _db_close_pool)
        self.connection.close()

        # Force-close any remaining pools that might hold connections to the test db
        # (from temporary connections, test connections, etc.)
        # We close all pools except the one that _nodb_cursor will create
        try:
            from hyperdjango._hyperdjango_native import _db_close_pool

            for i in range(32):  # MAX_POOLS
                with contextlib.suppress(Exception):
                    _db_close_pool(i)
        except ImportError:
            pass

        return super()._destroy_test_db(test_database_name, verbosity)
