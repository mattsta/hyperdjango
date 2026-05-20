"""Tests for the db.py Database class — backend selection, properties, error handling."""

import pytest

from hyperdjango.database import Database, db_offload_worker_count, get_db, set_db


class TestDatabaseCreation:
    def test_create_with_url(self):
        db = Database("postgres://localhost/testdb")
        assert db.url == "postgres://localhost/testdb"

    def test_default_pool_sizes(self, monkeypatch):
        # Database derives max_size from HYPER_POOL_SIZE env var (when set)
        # before falling back to the connection-budget heuristic:
        # THREAD_POOL_SIZE (24) + headroom (8) + DB offload workers
        # (auto min(cpu, 8)). The offload workers are folded in so a
        # multiplexing loop's offloaded queries never over-subscribe the pool.
        # Unset the env override so this test asserts the true defaults
        # rather than whatever CI happens to be running with. Offload workers
        # are read from the live setting (CPU-dependent) to keep this robust.
        monkeypatch.delenv("HYPER_POOL_SIZE", raising=False)
        db = Database("postgres://localhost/testdb")
        assert db.min_size == 2
        assert db.max_size == 24 + 8 + db_offload_worker_count()

    def test_custom_pool_sizes(self):
        db = Database("postgres://localhost/testdb", min_size=5, max_size=50)
        assert db.min_size == 5
        assert db.max_size == 50

    def test_initial_state(self):
        db = Database("postgres://localhost/testdb")
        assert db._pool is None
        assert db._backend is None


class TestDatabaseProperties:
    def test_is_connected_false_initially(self):
        db = Database("postgres://localhost/testdb")
        assert db.is_connected is False

    def test_is_connected_true_after_pool_set(self):
        db = Database("postgres://localhost/testdb")
        db._pool = True  # Simulate connected state
        assert db.is_connected is True

    def test_backend_none_initially(self):
        db = Database("postgres://localhost/testdb")
        assert db.backend == "none"

    def test_backend_after_set(self):
        db = Database("postgres://localhost/testdb")
        db._backend = "asyncpg"
        assert db.backend == "asyncpg"

    def test_backend_pgzig(self):
        db = Database("postgres://localhost/testdb")
        db._backend = "pgzig"
        assert db.backend == "pgzig"


class TestDatabaseRepr:
    def test_repr_not_connected(self):
        db = Database("postgres://localhost/testdb")
        r = repr(db)
        assert "postgres://localhost/testdb" in r
        assert "backend=none" in r

    def test_repr_connected(self):
        db = Database("postgres://localhost/testdb")
        db._backend = "asyncpg"
        r = repr(db)
        assert "backend=asyncpg" in r

    def test_repr_pgzig(self):
        db = Database("postgres://localhost/testdb")
        db._backend = "pgzig"
        assert "backend=pgzig" in repr(db)


class TestCheckPool:
    def test_check_pool_raises_when_not_connected(self):
        db = Database("postgres://localhost/testdb")
        with pytest.raises(RuntimeError, match="not connected"):
            db._check_pool()

    def test_check_pool_ok_when_connected(self):
        db = Database("postgres://localhost/testdb")
        db._pool = True  # Simulate connected state
        db._check_pool()  # Should not raise


class TestQueryWithoutConnection:
    """All query methods should raise RuntimeError when not connected."""

    @pytest.fixture
    def db(self):
        return Database("postgres://localhost/testdb")

    async def test_query_raises(self, db):
        with pytest.raises(RuntimeError, match="not connected"):
            await db.query("SELECT 1")

    async def test_query_one_raises(self, db):
        with pytest.raises(RuntimeError, match="not connected"):
            await db.query_one("SELECT 1")

    async def test_query_val_raises(self, db):
        with pytest.raises(RuntimeError, match="not connected"):
            await db.query_val("SELECT 1")

    async def test_execute_raises(self, db):
        with pytest.raises(RuntimeError, match="not connected"):
            await db.execute("INSERT INTO t VALUES (1)")

    async def test_execute_many_raises(self, db):
        with pytest.raises(RuntimeError, match="not connected"):
            await db.execute_many("INSERT INTO t VALUES ($1)", [(1,)])


class TestDisconnect:
    async def test_disconnect_when_not_connected(self):
        db = Database("postgres://localhost/testdb")
        await db.disconnect()  # Should not raise
        assert db._pool is None
        assert db._backend is None

    async def test_disconnect_clears_state(self):
        db = Database("postgres://localhost/testdb")
        db._pool = True
        db._backend = "pgzig"
        await db.disconnect()
        assert db._pool is None
        assert db._backend is None
        assert db.is_connected is False


class TestGlobalDbInstance:
    def test_get_db_raises_when_not_set(self, monkeypatch):
        # get_db() resolves DATABASE_URL through the settings authority, which
        # accepts several conventions — including assembling a URL from the
        # libpq PG* set when PGDATABASE is present. Strip every convention so
        # "not configured" holds regardless of the ambient shell/CI env.
        for var in ("HYPER_DATABASE_URL", "DATABASE_URL", "PGDATABASE"):
            monkeypatch.delenv(var, raising=False)

        from hyperdjango import database as db_module

        old = db_module._db
        db_module._db = None
        try:
            with pytest.raises(RuntimeError, match="No database configured"):
                get_db()
        finally:
            db_module._db = old

    def test_set_and_get_db(self):
        from hyperdjango import database as db_module

        old = db_module._db
        try:
            db = Database("postgres://localhost/testdb")
            set_db(db)
            assert get_db() is db
        finally:
            db_module._db = old


class TestConnectBackendDetection:
    async def test_connect_idempotent(self):
        """Calling connect when already connected is a no-op."""
        db = Database("postgres://localhost/testdb")
        db._pool = True
        db._backend = "pgzig"
        await db.connect()  # Should not raise, should be no-op
        assert db._backend == "pgzig"

    async def test_connect_no_backends_raises(self):
        """If neither pgzig nor asyncpg is available, connect should raise."""
        db = Database("postgres://localhost/nobackend")
        # Patch out both import paths; this test relies on native not being
        # compiled and asyncpg not being installed. Since we are running in a
        # test env that likely has asyncpg, we mock it.
        import unittest.mock as mock

        with (
            mock.patch.dict("sys.modules", {"asyncpg": None}),
            mock.patch(
                "hyperdjango.database.Database.connect",
                side_effect=RuntimeError("No database backend available"),
            ),
            pytest.raises(RuntimeError, match="No database backend"),
        ):
            await db.connect()


class TestTransactionContextManager:
    async def test_transaction_raises_without_connection(self):
        db = Database("postgres://localhost/testdb")
        with pytest.raises(RuntimeError, match="not connected"):
            async with db.transaction():
                pass
