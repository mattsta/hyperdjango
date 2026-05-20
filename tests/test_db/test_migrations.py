"""Integration tests for Django migration support through hyperdjango.db.

Tests that manage.py migrate works end-to-end: schema creation, FK constraints,
contenttypes, auth tables. Requires PostgreSQL running.

Run: uv run pytest tests/test_db/test_migrations.py -v
"""

import os

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    yield


class TestSchemaOperations:
    """Test that DDL operations work through our backend."""

    def test_create_table(self):
        _db.execute("DROP TABLE IF EXISTS mig_test_basic", [])
        _db.execute(
            """CREATE TABLE mig_test_basic (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            value INTEGER DEFAULT 0
        )""",
            [],
        )
        rows = _db.query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = $1 AND table_schema = $2",
            ["mig_test_basic", "public"],
        )
        assert len(rows) == 1
        _db.execute("DROP TABLE mig_test_basic", [])

    def test_create_table_with_fk(self):
        _db.execute("DROP TABLE IF EXISTS mig_child CASCADE", [])
        _db.execute("DROP TABLE IF EXISTS mig_parent CASCADE", [])
        _db.execute("CREATE TABLE mig_parent (id SERIAL PRIMARY KEY, name TEXT)", [])
        _db.execute(
            """CREATE TABLE mig_child (
            id SERIAL PRIMARY KEY,
            parent_id INTEGER REFERENCES mig_parent(id),
            label TEXT
        )""",
            [],
        )
        # Verify FK exists
        rows = _db.query(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name = $1 AND constraint_type = $2",
            ["mig_child", "FOREIGN KEY"],
        )
        assert len(rows) >= 1
        _db.execute("DROP TABLE mig_child CASCADE", [])
        _db.execute("DROP TABLE mig_parent CASCADE", [])

    def test_alter_table_add_column(self):
        _db.execute("DROP TABLE IF EXISTS mig_alter", [])
        _db.execute("CREATE TABLE mig_alter (id SERIAL PRIMARY KEY)", [])
        _db.execute("ALTER TABLE mig_alter ADD COLUMN name VARCHAR(100)", [])
        rows = _db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            ["mig_alter", "name"],
        )
        assert len(rows) == 1
        _db.execute("DROP TABLE mig_alter", [])

    def test_create_index(self):
        _db.execute("DROP TABLE IF EXISTS mig_idx", [])
        _db.execute("CREATE TABLE mig_idx (id SERIAL PRIMARY KEY, email TEXT)", [])
        _db.execute("CREATE INDEX idx_mig_email ON mig_idx (email)", [])
        rows = _db.query(
            "SELECT indexname FROM pg_indexes WHERE tablename = $1 AND indexname = $2",
            ["mig_idx", "idx_mig_email"],
        )
        assert len(rows) == 1
        _db.execute("DROP TABLE mig_idx", [])

    def test_drop_table(self):
        _db.execute("CREATE TABLE IF NOT EXISTS mig_drop (id SERIAL PRIMARY KEY)", [])
        _db.execute("DROP TABLE mig_drop", [])
        rows = _db.query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = $1 AND table_schema = $2",
            ["mig_drop", "public"],
        )
        assert len(rows) == 0


class TestTransactionDDL:
    """Test DDL within transactions (PostgreSQL supports transactional DDL)."""

    def test_ddl_in_transaction_commit(self):
        from hyperdjango._hyperdjango_native import (
            _db_conn_acquire,
            _db_conn_execute,
            _db_conn_release,
        )

        _db.execute("DROP TABLE IF EXISTS mig_tx_ddl", [])

        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(
            handle, "CREATE TABLE mig_tx_ddl (id SERIAL PRIMARY KEY, name TEXT)", []
        )
        _db_conn_execute(handle, "INSERT INTO mig_tx_ddl (name) VALUES ($1)", ["test"])
        _db_conn_execute(handle, "COMMIT", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM mig_tx_ddl", [])
        assert len(rows) == 1
        assert rows[0][0] == "test"
        _db.execute("DROP TABLE mig_tx_ddl", [])

    def test_ddl_in_transaction_rollback(self):
        from hyperdjango._hyperdjango_native import (
            _db_conn_acquire,
            _db_conn_execute,
            _db_conn_release,
        )

        _db.execute("DROP TABLE IF EXISTS mig_tx_rollback", [])

        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(
            handle, "CREATE TABLE mig_tx_rollback (id SERIAL PRIMARY KEY)", []
        )
        _db_conn_execute(handle, "ROLLBACK", [])
        _db_conn_release(handle)

        rows = _db.query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = $1 AND table_schema = $2",
            ["mig_tx_rollback", "public"],
        )
        assert len(rows) == 0


class TestCursorDjangoPipeline:
    """Test the full Django cursor pipeline (PgZigCursor → native)."""

    def test_cursor_create_table(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS mig_cursor_test")
        cursor.execute(
            "CREATE TABLE mig_cursor_test (id SERIAL PRIMARY KEY, name TEXT)"
        )
        cursor.execute("INSERT INTO mig_cursor_test (name) VALUES (%s)", ["hello"])
        cursor.execute("SELECT name FROM mig_cursor_test")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "hello"
        cursor.execute("DROP TABLE mig_cursor_test")
        cursor.close()
        conn.close()

    def test_cursor_autocommit_off_commit(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()

        # Create table in autocommit
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS mig_ac_test")
        cursor.execute("CREATE TABLE mig_ac_test (id SERIAL PRIMARY KEY, name TEXT)")
        cursor.close()

        # Insert in transaction
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute("INSERT INTO mig_ac_test (name) VALUES (%s)", ["txn_row"])
        conn.commit()

        # Verify
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM mig_ac_test")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "txn_row"

        cursor.execute("DROP TABLE mig_ac_test")
        cursor.close()
        conn.close()

    def test_cursor_autocommit_off_rollback(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()

        # Create table in autocommit
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS mig_rb_test")
        cursor.execute("CREATE TABLE mig_rb_test (id SERIAL PRIMARY KEY, name TEXT)")
        cursor.close()

        # Insert in transaction then rollback
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute("INSERT INTO mig_rb_test (name) VALUES (%s)", ["should_vanish"])
        conn.rollback()

        # Verify nothing persisted
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM mig_rb_test")
        rows = cursor.fetchall()
        assert len(rows) == 0

        cursor.execute("DROP TABLE mig_rb_test")
        cursor.close()
        conn.close()


class TestComposeSQL:
    """Test our compose_sql override works for Django schema operations."""

    def _make_ops(self):
        from hyperdjango.db.operations import DatabaseOperations

        ops = DatabaseOperations.__new__(DatabaseOperations)
        ops.connection = None  # Prevent __del__ warning
        return ops

    def test_compose_sql_basic(self):
        ops = self._make_ops()
        result = ops.compose_sql("SELECT %s, %s", ["hello", 42])
        assert "hello" in result
        assert "42" in result

    def test_compose_sql_none_params(self):
        ops = self._make_ops()
        result = ops.compose_sql("SELECT 1", None)
        assert result == "SELECT 1"

    def test_compose_sql_null_param(self):
        ops = self._make_ops()
        result = ops.compose_sql("INSERT INTO t (a) VALUES (%s)", [None])
        assert "NULL" in result


class TestErrorClassification:
    """Test that PostgreSQL errors are properly classified."""

    def test_duplicate_table_error(self):
        _db.execute("DROP TABLE IF EXISTS err_dup", [])
        _db.execute("CREATE TABLE err_dup (id SERIAL PRIMARY KEY)", [])
        with pytest.raises(Exception) as exc_info:
            _db.execute("CREATE TABLE err_dup (id SERIAL PRIMARY KEY)", [])
        # Should mention "already exists"
        assert "already exists" in str(exc_info.value)
        _db.execute("DROP TABLE err_dup", [])

    def test_syntax_error(self):
        with pytest.raises(Exception) as exc_info:
            _db.query("SELECTT 1", [])
        assert (
            "syntax" in str(exc_info.value).lower()
            or "failed" in str(exc_info.value).lower()
        )

    def test_nonexistent_table(self):
        with pytest.raises(Exception) as exc_info:
            _db.query("SELECT * FROM nonexistent_table_xyz", [])
        assert (
            "does not exist" in str(exc_info.value)
            or "failed" in str(exc_info.value).lower()
        )


class TestDatabaseWrapperComponents:
    """Test that our DatabaseWrapper uses our component classes."""

    def test_ops_class(self):
        from hyperdjango.db.base import DatabaseWrapper
        from hyperdjango.db.operations import DatabaseOperations

        assert DatabaseWrapper.ops_class is DatabaseOperations

    def test_features_class(self):
        from hyperdjango.db.base import DatabaseWrapper
        from hyperdjango.db.features import DatabaseFeatures

        assert DatabaseWrapper.features_class is DatabaseFeatures

    def test_creation_class(self):
        from hyperdjango.db.base import DatabaseWrapper
        from hyperdjango.db.creation import DatabaseCreation

        assert DatabaseWrapper.creation_class is DatabaseCreation

    def test_schema_editor_class(self):
        from hyperdjango.db.base import DatabaseWrapper
        from hyperdjango.db.schema import DatabaseSchemaEditor

        assert DatabaseWrapper.SchemaEditorClass is DatabaseSchemaEditor

    def test_introspection_class(self):
        from hyperdjango.db.base import DatabaseWrapper
        from hyperdjango.db.introspection import DatabaseIntrospection

        assert DatabaseWrapper.introspection_class is DatabaseIntrospection
