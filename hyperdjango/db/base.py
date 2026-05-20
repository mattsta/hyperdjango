"""
pg.zig database backend for Django.

Drop-in replacement for django.db.backends.postgresql that uses the
native Zig PostgreSQL driver (pg.zig) instead of psycopg2/psycopg3.

Usage:
    DATABASES = {
        'default': {
            'ENGINE': 'hyperdjango.db',
            'NAME': 'mydb',
            'USER': 'myuser',
            'PASSWORD': 'mypass',
            'HOST': 'localhost',
            'PORT': '5432',
        }
    }

All Django ORM features work unchanged — migrations, admin, querysets,
raw SQL, transactions, connection pooling. Only the wire protocol layer
is replaced with pg.zig for native Zig performance.
"""

import json as _stdlib_json

from django.db.backends.postgresql.base import (
    DatabaseWrapper as PsycopgDatabaseWrapper,
)

from hyperdjango.db.client import DatabaseClient
from hyperdjango.db.creation import DatabaseCreation
from hyperdjango.db.features import DatabaseFeatures
from hyperdjango.db.introspection import DatabaseIntrospection
from hyperdjango.db.operations import DatabaseOperations
from hyperdjango.db.pgzig_connection import PgZigConnection
from hyperdjango.db.schema import DatabaseSchemaEditor
from hyperdjango.native import fast_json_loads


# pg.zig returns JSONB as parsed Python objects (dict/list) via binary protocol.
# Django's JSONField.from_db_value() calls json.loads() which fails on non-strings.
# Patch it to handle already-parsed values gracefully.
def _patched_json_from_db_value(self, value, expression, connection):
    if value is None:
        return value
    if isinstance(value, (dict, list, int, float, bool)):
        return value  # Already parsed by pg.zig binary protocol
    try:
        if self.decoder:
            return _stdlib_json.loads(value, cls=self.decoder)
        return fast_json_loads(value)
    except _stdlib_json.JSONDecodeError, TypeError, RuntimeError:
        return value


try:
    from django.db.models.fields.json import JSONField

    JSONField.from_db_value = _patched_json_from_db_value
except ImportError:
    pass


class DatabaseWrapper(PsycopgDatabaseWrapper):
    """Django database backend using pg.zig native Zig PostgreSQL driver.

    Inherits ALL of Django's PostgreSQL backend (data_types, operators,
    SQL compilation, migrations, introspection, schema editor) and only
    replaces the connection/cursor layer with pg.zig.
    """

    vendor = "postgresql"
    display_name = "PostgreSQL"

    # Explicitly set all component classes to our overrides
    ops_class = DatabaseOperations
    features_class = DatabaseFeatures
    introspection_class = DatabaseIntrospection
    SchemaEditorClass = DatabaseSchemaEditor
    creation_class = DatabaseCreation
    client_class = DatabaseClient

    def get_new_connection(self, conn_params):
        """Create a new pg.zig connection instead of psycopg."""
        from django.core.exceptions import ImproperlyConfigured
        from django.db.backends.postgresql.psycopg_any import IsolationLevel

        host = conn_params.get("host", "localhost")
        port = conn_params.get("port", 5432)
        dbname = conn_params.get("database", conn_params.get("dbname", ""))
        user = conn_params.get("user", "")
        password = conn_params.get("password", "")

        # Parse isolation level from OPTIONS (matching psycopg backend)
        options = self.settings_dict.get("OPTIONS", {})
        set_isolation_level = False
        try:
            isolation_level_value = options["isolation_level"]
        except KeyError:
            self.isolation_level = IsolationLevel.READ_COMMITTED
        else:
            try:
                self.isolation_level = IsolationLevel(isolation_level_value)
                set_isolation_level = True
            except ValueError:
                raise ImproperlyConfigured(
                    f"Invalid transaction isolation level {isolation_level_value} "
                    f"specified. Use one of the psycopg.IsolationLevel values."
                )

        conn = PgZigConnection(
            host=host,
            port=int(port) if port else 5432,
            dbname=dbname,
            user=user,
            password=password,
        )
        conn.connect()

        # Apply cursor_factory from OPTIONS (or server_side_binding)
        server_side_binding = options.get("server_side_binding")
        cursor_factory = options.get("cursor_factory")
        if cursor_factory:
            conn.cursor_factory = cursor_factory
        elif server_side_binding is True:
            from django.db.backends.postgresql.base import ServerBindingCursor

            conn.cursor_factory = ServerBindingCursor

        # Apply isolation level to the connection
        if set_isolation_level:
            conn.isolation_level = self.isolation_level
            # Send SET to PostgreSQL
            isolation_name = {
                1: "READ UNCOMMITTED",
                2: "READ COMMITTED",
                3: "REPEATABLE READ",
                4: "SERIALIZABLE",
            }.get(self.isolation_level.value, "READ COMMITTED")
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SET default_transaction_isolation TO '{isolation_name}'"
                )

        # Apply role from OPTIONS
        assume_role = options.get("assume_role")
        if assume_role:
            with conn.cursor() as cursor:
                cursor.execute(f"SET ROLE '{assume_role}'")

        return conn

    def create_cursor(self, name=None):
        """Create a pg.zig cursor — skips psycopg adapter registration."""
        # Override parent to avoid psycopg-specific timezone adapter setup.
        # pg.zig handles timestamp timezone conversion natively in Zig.
        return self.connection.cursor(name=name)

    def _set_autocommit(self, autocommit):
        """Set autocommit mode on the pg.zig connection."""
        with self.wrap_database_errors:
            self.connection.autocommit = autocommit

    def _close(self):
        """Close the pg.zig connection."""
        if self.connection is not None:
            with self.wrap_database_errors:
                self.connection.close()
                self.connection = None

    def is_usable(self):
        """Check if the connection is still usable."""
        if self.connection is None:
            return False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        # blind-except: is_usable() is a liveness probe (Django contract) — any failure means the connection is not usable; it must return a bool, never propagate.
        except Exception:
            return False

    def _configure_timezone(self, connection):
        """Set timezone on the connection. Returns True if SET was executed."""
        conn_tz = (
            connection.info.parameter_status("TimeZone")
            if hasattr(connection, "info")
            else None
        )
        timezone_name = self.timezone_name
        if timezone_name and conn_tz != timezone_name:
            with connection.cursor() as cursor:
                cursor.execute(self.ops.set_time_zone_sql(), [timezone_name])
            return True
        return False

    def _configure_role(self, connection):
        """Set role on the connection. Returns True if SET ROLE was executed."""
        new_role = self.settings_dict.get("OPTIONS", {}).get("assume_role")
        if new_role:
            with connection.cursor() as cursor:
                sql = self.ops.compose_sql("SET ROLE %s", [new_role])
                cursor.execute(sql)
            return True
        return False

    def _configure_connection(self, connection):
        """Configure timezone and role on a connection.

        Called from init_connection_state. Returns True if any SET was executed
        (meaning a COMMIT is needed if not in autocommit mode).
        """
        commit_tz = self._configure_timezone(connection)
        commit_role = self._configure_role(connection)
        return commit_role or commit_tz

    def init_connection_state(self):
        """Initialize connection state (timezone, role, hstore, etc.).

        Matches the reference PostgreSQL backend flow:
        1. Check database version (via base class)
        2. Configure connection (timezone, role)
        3. Register extension types (hstore OID)
        4. Commit if in transaction so settings persist
        """
        from django.db.backends.base.base import BaseDatabaseWrapper

        BaseDatabaseWrapper.init_connection_state(self)

        if self.connection is not None:
            needs_commit = self._configure_connection(self.connection)

            # Register extension type OIDs for native conversion
            try:
                from hyperdjango._hyperdjango_native import (
                    _db_register_hstore,
                    _db_register_vector,
                )

                # dynamic-attr: ``_pool_handle`` is a framework attribute present only on the native pgzig connection, not on a standard Django connection object
                pool_h = getattr(self.connection, "_pool_handle", None)
                if pool_h is not None:
                    _db_register_hstore(pool_h)
                    _db_register_vector(pool_h)
            except ImportError, AttributeError, RuntimeError:
                pass

            if needs_commit and not self.get_autocommit():
                self.connection.commit()

    def check_constraints(self, table_names=None):
        """Check constraints by setting them to immediate.

        Overrides PostgreSQL backend to handle the aborted transaction state
        that occurs when SET CONSTRAINTS ALL IMMEDIATE triggers a violation.
        """
        with self.cursor() as cursor:
            try:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            except Exception:
                # Constraint violation — transaction is now aborted.
                # Don't try SET CONSTRAINTS ALL DEFERRED, it will fail.
                raise
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    @property
    def pg_version(self):
        """Return the PostgreSQL server version."""
        if hasattr(self, "_pg_version_override"):
            return self._pg_version_override
        with self.temporary_connection() as cursor:
            cursor.execute("SHOW server_version_num")
            return int(cursor.fetchone()[0])

    @pg_version.setter
    def pg_version(self, value):
        """Allow setting pg_version for testing."""
        self._pg_version_override = value
