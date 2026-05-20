"""Tests that the pg.zig database backend loads correctly in Django."""


class TestBackendImport:
    """Test that the backend module structure is correct for Django."""

    def test_import_base(self):
        from hyperdjango.db.base import DatabaseWrapper

        assert DatabaseWrapper.vendor == "postgresql"
        assert DatabaseWrapper.display_name == "PostgreSQL"

    def test_import_features(self):
        from hyperdjango.db.features import DatabaseFeatures

        assert DatabaseFeatures is not None

    def test_import_operations(self):
        from hyperdjango.db.operations import DatabaseOperations

        assert DatabaseOperations is not None

    def test_import_introspection(self):
        from hyperdjango.db.introspection import DatabaseIntrospection

        assert DatabaseIntrospection is not None

    def test_import_schema(self):
        from hyperdjango.db.schema import DatabaseSchemaEditor

        assert DatabaseSchemaEditor is not None

    def test_import_creation(self):
        from hyperdjango.db.creation import DatabaseCreation

        assert DatabaseCreation is not None

    def test_import_client(self):
        from hyperdjango.db.client import DatabaseClient

        assert DatabaseClient is not None

    def test_inherits_pg_data_types(self):
        """Our backend inherits ALL of Django's PostgreSQL type mappings."""
        from hyperdjango.db.base import DatabaseWrapper

        assert "AutoField" in DatabaseWrapper.data_types
        assert "CharField" in DatabaseWrapper.data_types
        assert "DateTimeField" in DatabaseWrapper.data_types
        assert "JSONField" in DatabaseWrapper.data_types
        assert "UUIDField" in DatabaseWrapper.data_types

    def test_inherits_pg_operators(self):
        """Our backend inherits ALL of Django's PostgreSQL operators."""
        from hyperdjango.db.base import DatabaseWrapper

        assert "exact" in DatabaseWrapper.operators
        assert "contains" in DatabaseWrapper.operators
        assert "icontains" in DatabaseWrapper.operators
        assert "regex" in DatabaseWrapper.operators

    def test_connection_class(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        conn = PgZigConnection(host="localhost", dbname="test")
        assert conn.host == "localhost"
        assert conn.dbname == "test"
        assert conn.autocommit is False

    def test_cursor_interface(self):
        from hyperdjango.db.pgzig_connection import PgZigCursor

        cursor = PgZigCursor(None, native=False)
        assert hasattr(cursor, "execute")
        assert hasattr(cursor, "fetchone")
        assert hasattr(cursor, "fetchmany")
        assert hasattr(cursor, "fetchall")
        assert hasattr(cursor, "close")
        assert hasattr(cursor, "description")
        assert hasattr(cursor, "rowcount")
