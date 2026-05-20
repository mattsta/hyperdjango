"""Integration tests for transaction support through pg.zig pinned connections.

Tests BEGIN/COMMIT/ROLLBACK semantics with pinned connections that hold
state across multiple execute calls. Requires PostgreSQL running.

Run: uv run pytest tests/test_db/test_transactions.py -v
"""

import os

import pytest
from hyperdjango._hyperdjango_native import (
    _db_conn_acquire,
    _db_conn_execute,
    _db_conn_release,
)

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    yield


@pytest.fixture(autouse=True)
def clean_table():
    """Create a fresh table for each test."""
    _db.execute("DROP TABLE IF EXISTS tx_test")
    _db.execute(
        "CREATE TABLE tx_test (id SERIAL PRIMARY KEY, name TEXT, value INTEGER)"
    )
    yield
    _db.execute("DROP TABLE IF EXISTS tx_test")


class TestPinnedConnections:
    def test_acquire_release(self):
        handle = _db_conn_acquire(_db.handle)
        assert isinstance(handle, int)
        assert handle >= 0
        _db_conn_release(handle)

    def test_execute_on_pinned(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["test"])
        _db_conn_release(handle)
        rows = _db.query("SELECT name FROM tx_test")
        assert len(rows) == 1
        assert rows[0][0] == "test"

    def test_multiple_pinned(self):
        h1 = _db_conn_acquire(_db.handle)
        h2 = _db_conn_acquire(_db.handle)
        assert h1 != h2
        _db_conn_release(h1)
        _db_conn_release(h2)


class TestCommit:
    def test_committed_data_persists(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(
            handle, "INSERT INTO tx_test (name) VALUES ($1)", ["committed"]
        )
        _db_conn_execute(handle, "COMMIT", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM tx_test")
        assert len(rows) == 1
        assert rows[0][0] == "committed"

    def test_multiple_inserts_in_transaction(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        for i in range(5):
            _db_conn_execute(
                handle,
                "INSERT INTO tx_test (name, value) VALUES ($1, $2)",
                [f"item_{i}", str(i)],
            )
        _db_conn_execute(handle, "COMMIT", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT COUNT(*) FROM tx_test")
        assert rows[0][0] == 5


class TestRollback:
    def test_rolled_back_data_disappears(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(
            handle, "INSERT INTO tx_test (name) VALUES ($1)", ["rolled_back"]
        )
        _db_conn_execute(handle, "ROLLBACK", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM tx_test")
        assert len(rows) == 0

    def test_partial_rollback(self):
        # Insert outside transaction (auto-committed)
        _db.execute("INSERT INTO tx_test (name) VALUES ($1)", ["before_tx"])

        # Insert inside transaction then rollback
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["in_tx"])
        _db_conn_execute(handle, "ROLLBACK", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM tx_test ORDER BY id")
        assert len(rows) == 1
        assert rows[0][0] == "before_tx"


class TestTransactionIsolation:
    def test_uncommitted_not_visible_outside(self):
        """Data in an open transaction should not be visible from pool connections."""
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(
            handle, "INSERT INTO tx_test (name) VALUES ($1)", ["invisible"]
        )

        # Query from pool (different connection) should not see uncommitted data
        rows = _db.query("SELECT name FROM tx_test")
        assert len(rows) == 0

        _db_conn_execute(handle, "ROLLBACK", [])
        _db_conn_release(handle)


class TestSavepoints:
    """Test SAVEPOINT/RELEASE/ROLLBACK TO through pinned connections."""

    def test_savepoint_commit(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(
            handle, "INSERT INTO tx_test (name) VALUES ($1)", ["before_sp"]
        )
        _db_conn_execute(handle, "SAVEPOINT sp1", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["in_sp"])
        _db_conn_execute(handle, "RELEASE SAVEPOINT sp1", [])
        _db_conn_execute(handle, "COMMIT", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM tx_test ORDER BY id")
        assert len(rows) == 2
        assert rows[0][0] == "before_sp"
        assert rows[1][0] == "in_sp"

    def test_savepoint_rollback(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["keep"])
        _db_conn_execute(handle, "SAVEPOINT sp1", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["discard"])
        _db_conn_execute(handle, "ROLLBACK TO SAVEPOINT sp1", [])
        _db_conn_execute(handle, "COMMIT", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM tx_test ORDER BY id")
        assert len(rows) == 1
        assert rows[0][0] == "keep"

    def test_nested_savepoints(self):
        handle = _db_conn_acquire(_db.handle)
        _db_conn_execute(handle, "BEGIN", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["outer"])
        _db_conn_execute(handle, "SAVEPOINT sp1", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["inner1"])
        _db_conn_execute(handle, "SAVEPOINT sp2", [])
        _db_conn_execute(handle, "INSERT INTO tx_test (name) VALUES ($1)", ["inner2"])
        _db_conn_execute(handle, "ROLLBACK TO SAVEPOINT sp2", [])
        # inner2 gone, inner1 still there
        _db_conn_execute(handle, "RELEASE SAVEPOINT sp1", [])
        _db_conn_execute(handle, "COMMIT", [])
        _db_conn_release(handle)

        rows = _db.query("SELECT name FROM tx_test ORDER BY id")
        assert len(rows) == 2
        assert rows[0][0] == "outer"
        assert rows[1][0] == "inner1"

    def test_savepoint_through_cursor(self):
        """Test savepoints through PgZigCursor (Django's path)."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost",
            port=5432,
            dbname=os.environ.get("PGDATABASE", "hyperdjango_test"),
            user=user,
        )
        conn.connect()
        conn.autocommit = False

        cursor = conn.cursor()
        cursor.execute("INSERT INTO tx_test (name) VALUES (%s)", ["sp_cursor_keep"])
        cursor.execute("SAVEPOINT test_sp")
        cursor.execute("INSERT INTO tx_test (name) VALUES (%s)", ["sp_cursor_discard"])
        cursor.execute("ROLLBACK TO SAVEPOINT test_sp")
        conn.commit()

        rows = _db.query("SELECT name FROM tx_test WHERE name LIKE $1", ["sp_cursor%"])
        assert len(rows) == 1
        assert rows[0][0] == "sp_cursor_keep"


class TestPgZigConnectionTransactions:
    """Test transaction support through the PgZigConnection Python interface."""

    def test_connection_commit(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost",
            port=5432,
            dbname=os.environ.get("PGDATABASE", "hyperdjango_test"),
            user=user,
        )
        conn.connect()
        conn.autocommit = False

        cursor = conn.cursor()
        cursor.execute("INSERT INTO tx_test (name) VALUES (%s)", ["via_conn"])
        conn.commit()

        # Verify via direct query
        rows = _db.query("SELECT name FROM tx_test")
        assert any(r[0] == "via_conn" for r in rows)

    def test_connection_rollback(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost",
            port=5432,
            dbname=os.environ.get("PGDATABASE", "hyperdjango_test"),
            user=user,
        )
        conn.connect()
        conn.autocommit = False

        cursor = conn.cursor()
        cursor.execute("INSERT INTO tx_test (name) VALUES (%s)", ["to_rollback"])
        conn.rollback()

        rows = _db.query("SELECT name FROM tx_test WHERE name = $1", ["to_rollback"])
        assert len(rows) == 0
