"""Test pool isolation — multiple connections don't interfere.

Verifies that creating multiple PgZigConnection instances doesn't cause
crashes from shared global pool state.

Run: uv run pytest tests/test_db/test_pool_isolation.py -v
"""

import os
import subprocess

import pytest
from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_query,
)


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    subprocess.run(["createdb", "hyperdjango_test"], capture_output=True)
    yield


class TestPoolIsolation:
    """Test that multiple pools can coexist without crashes."""

    def test_two_pools_same_db(self):
        user = os.environ.get("USER", "postgres")
        h1 = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 2)
        assert isinstance(h1, int)
        rows = _db_query(h1, "SELECT 1", [])
        assert rows[0][0] == 1

        h2 = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 2)
        assert isinstance(h2, int)
        assert h1 != h2
        rows = _db_query(h2, "SELECT 2", [])
        assert rows[0][0] == 2

        # Original pool still works
        rows = _db_query(h1, "SELECT 3", [])
        assert rows[0][0] == 3

        _db_close_pool(h1)
        _db_close_pool(h2)

    def test_close_pool_doesnt_affect_others(self):
        user = os.environ.get("USER", "postgres")
        h1 = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 2)
        h2 = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 2)

        _db_close_pool(h1)

        # h2 still works after h1 is closed
        rows = _db_query(h2, "SELECT 42", [])
        assert rows[0][0] == 42

        _db_close_pool(h2)

    def test_pgzig_connection_isolation(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")

        conn1 = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn1.connect()
        conn1.autocommit = True

        conn2 = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn2.connect()
        conn2.autocommit = True

        # Both work independently
        c1 = conn1.cursor()
        c1.execute("SELECT 1")
        assert c1.fetchone()[0] == 1

        c2 = conn2.cursor()
        c2.execute("SELECT 2")
        assert c2.fetchone()[0] == 2

        # Close conn1, conn2 still works
        c1.close()
        conn1.close()

        c2 = conn2.cursor()
        c2.execute("SELECT 3")
        assert c2.fetchone()[0] == 3
        c2.close()
        conn2.close()

    def test_temporary_connection_pattern(self):
        """Simulate Django's temporary_connection() pattern that was crashing."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")

        # Main connection
        main = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        main.connect()
        main.autocommit = True
        c = main.cursor()
        c.execute("SELECT 'main'")
        assert c.fetchone()[0] == "main"
        c.close()

        # Temporary connection (like pg_version does)
        temp = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        temp.connect()
        temp.autocommit = True
        c = temp.cursor()
        c.execute("SELECT 'temp'")
        assert c.fetchone()[0] == "temp"
        c.close()
        temp.close()

        # Main connection still works after temp is closed
        c = main.cursor()
        c.execute("SELECT 'still_main'")
        assert c.fetchone()[0] == "still_main"
        c.close()
        main.close()
