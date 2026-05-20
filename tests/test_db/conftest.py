"""Shared fixtures for database integration tests.

Provides db_pool fixture that configures a pool and returns
wrapper functions with the pool handle baked in.
"""

import os
import subprocess

import pytest
from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_query,
)


class DbPool:
    """Wraps a native pool handle, provides query/execute with handle baked in."""

    def __init__(self, handle):
        self.handle = handle

    def query(self, sql, params=None):
        return _db_query(self.handle, sql, params or [])

    def execute(self, sql, params=None):
        return _db_execute(self.handle, sql, params or [])

    def close(self):
        _db_close_pool(self.handle)


@pytest.fixture(scope="module")
def db_pool():
    """Configure a native pool and return a DbPool wrapper.

    All tests should use db_pool.query() and db_pool.execute()
    instead of calling _db_query/_db_execute directly.
    """
    if False:
        pytest.skip("Native extension not compiled")

    user = os.environ.get("USER", "postgres")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    subprocess.run(["createdb", dbname], capture_output=True)
    handle = _db_configure(f"postgresql://{user}:@localhost:5432/{dbname}", 5)
    pool = DbPool(handle)
    yield pool
    pool.close()
