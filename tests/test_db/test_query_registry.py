"""Regression tests for the lock-free native query registry bounds + handle
validation (review items F2, F4).

F2: the native registry is a FIXED 4096-slot append-only table and the Python
    `_query_handle_cache` must not grow unbounded. Guards: register only on the
    2nd sighting of a SQL, never cache a -1 result, and size-cap both structures.

F4: `_db_query_dicts` must reject a handle that was never registered (only
    `handle < registry_count` AND matching SQL hash is honored) — a bogus
    in-array-range handle must NOT execute a different cached statement.

Run: uv run pytest tests/test_db/test_query_registry.py -v
"""

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest
from hyperdjango._hyperdjango_native import _db_query_dicts

from hyperdjango import database as _dbmod

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    _db.execute("DROP TABLE IF EXISTS qreg", [])
    _db.execute("CREATE TABLE qreg (id INT, label TEXT)", [])
    _db.execute("INSERT INTO qreg VALUES (1,'a'),(2,'b'),(3,'c')", [])
    yield
    _db.execute("DROP TABLE IF EXISTS qreg", [])


@pytest.fixture(autouse=True)
def _clean_handle_cache():
    # Isolate the module-level caches per test.
    _dbmod._query_handle_cache.clear()
    _dbmod._query_seen_once.clear()
    yield
    _dbmod._query_handle_cache.clear()
    _dbmod._query_seen_once.clear()


class TestSecondSightingRegistration:
    def test_first_sighting_returns_no_handle(self):
        sql = "SELECT id FROM qreg WHERE id = $1 UNIQUE_A"
        assert _dbmod._query_handle(sql) == -1  # 1st sight → fallback
        assert sql in _dbmod._query_seen_once
        assert sql not in _dbmod._query_handle_cache

    def test_second_sighting_registers(self):
        sql = "SELECT id FROM qreg WHERE id = $1 UNIQUE_B"
        assert _dbmod._query_handle(sql) == -1
        h = _dbmod._query_handle(sql)  # 2nd sight → registers
        assert h >= 0
        assert _dbmod._query_handle(sql) == h  # cached thereafter
        assert sql not in _dbmod._query_seen_once

    def test_one_off_sql_never_registers(self):
        # Simulate 500 distinct one-off SQL strings (the pagination / per-
        # cardinality-prefetch pattern). None is seen twice → none registers,
        # so the registry cannot be exhausted by one-off SQL.
        for i in range(500):
            assert _dbmod._query_handle(f"SELECT $1 AS n_{i}") == -1
        # A genuinely-repeated query still gets a handle afterward — proving
        # the registry was not exhausted by the 500 one-offs.
        repeated = "SELECT id FROM qreg WHERE id = $1 REPEATED"
        assert _dbmod._query_handle(repeated) == -1
        assert _dbmod._query_handle(repeated) >= 0

    def test_seen_once_set_is_size_capped(self, monkeypatch):
        # Small cap so we exercise the clear-on-overflow path cheaply (all
        # first-sight → nothing registers in the native registry).
        monkeypatch.setattr(_dbmod, "_QUERY_CACHE_MAX", 8)
        for i in range(20):
            _dbmod._query_handle(f"SELECT $1 AS capn_{i}")
        assert len(_dbmod._query_seen_once) <= 8

    def test_handle_cache_is_size_capped(self, monkeypatch):
        # Small cap: register (2nd-sight) more than the cap distinct SQLs and
        # confirm the dict is bounded. Keeps native-registry consumption tiny.
        monkeypatch.setattr(_dbmod, "_QUERY_CACHE_MAX", 8)
        for i in range(12):
            s = f"SELECT id FROM qreg WHERE id = $1 CAP_{i}"
            _dbmod._query_handle(s)
            _dbmod._query_handle(s)
        assert len(_dbmod._query_handle_cache) <= 8


class TestPaginationDoesNotExhaustRegistry:
    def test_500_page_loop_single_registration(self):
        # The ORM emits ONE SQL for all pages (LIMIT/OFFSET are params), so a
        # 500-page loop touches a single registry slot. Emulate at the
        # _query_handle level: same SQL string every page.
        page_sql = "SELECT id, label FROM qreg ORDER BY id LIMIT $1 OFFSET $2"
        handles = {_dbmod._query_handle(page_sql) for _ in range(500)}
        # At most one distinct non-negative handle (a single registry slot).
        assert len([h for h in handles if h >= 0]) <= 1
        final = _dbmod._query_handle(page_sql)
        assert final >= 0
        # Every page executes correctly through that one handle.
        for page in range(500):
            rows = _db_query_dicts(_db.handle, page_sql, [1, page], final)
            assert isinstance(rows, list)


class TestBogusHandleRejected:
    def test_out_of_range_handle_falls_back_correctly(self):
        # A handle far above registry_count must be rejected (fall back), NOT
        # index an uninitialized slot / run a different cached statement.
        sql = "SELECT id, label FROM qreg WHERE id = $1"
        bogus = 4000  # in [0, 4096) but almost certainly not registered
        rows = _db_query_dicts(_db.handle, sql, [2], bogus)
        assert [(r["id"], r["label"]) for r in rows] == [(2, "b")]

    def test_negative_handle_falls_back(self):
        sql = "SELECT id, label FROM qreg WHERE id = $1"
        rows = _db_query_dicts(_db.handle, sql, [3], -1)
        assert [(r["id"], r["label"]) for r in rows] == [(3, "c")]

    def test_wrong_sql_for_valid_handle_falls_back(self):
        # Register handle for SQL A, then use it with SQL B. The slot's SQL-hash
        # check must reject the mismatch and run B correctly (fallback path),
        # NOT execute A's cached statement.
        sql_a = "SELECT id FROM qreg WHERE id = $1 HANDLE_A"
        _dbmod._query_handle(sql_a)
        h = _dbmod._query_handle(sql_a)
        assert h >= 0
        sql_b = "SELECT id, label FROM qreg WHERE id = $1"
        rows = _db_query_dicts(_db.handle, sql_b, [1], h)  # h belongs to A
        assert [(r["id"], r["label"]) for r in rows] == [(1, "a")]
