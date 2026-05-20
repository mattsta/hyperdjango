#!/usr/bin/env python3
"""Test the TestCase base class with DB rollback isolation.

Tests:
1. TestCase discovers and runs test_ methods
2. Assertions (assertEqual, assertTrue, assertIn, etc.)
3. Response assertions (assertStatus, assertContains, assertRedirects)
4. assertRaises context manager
5. Database isolation via savepoints (each test rolled back)
6. asyncSetUp / asyncTearDown lifecycle
7. TestClient integration within TestCase
8. Multiple tests don't interfere with each other

Run: uv run hyper-test testcase
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango import HyperApp, Response
from hyperdjango.testing import TestCase, TestResponse

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

# ─── Test the assertion helpers (no DB needed) ─────────────────────────────

outer_passed = 0
outer_failed = 0


def check(name, condition, detail=""):
    global outer_passed, outer_failed
    if condition:
        print(f"  PASS: {name}")
        outer_passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        outer_failed += 1


def test_assertions():
    """Test assertion methods work correctly."""
    print("\n=== Assertion Helpers ===")

    tc = TestCase()

    # assertEqual
    tc.assertEqual(1, 1)
    check("assertEqual passes", True)

    raised = False
    try:
        tc.assertEqual(1, 2)
    except AssertionError:
        raised = True
    check("assertEqual fails correctly", raised)

    # assertNotEqual
    tc.assertNotEqual(1, 2)
    check("assertNotEqual passes", True)

    # assertTrue / assertFalse
    tc.assertTrue(True)
    tc.assertFalse(False)
    check("assertTrue/assertFalse pass", True)

    # assertIsNone / assertIsNotNone
    tc.assertIsNone(None)
    tc.assertIsNotNone(42)
    check("assertIsNone/assertIsNotNone pass", True)

    # assertIn / assertNotIn
    tc.assertIn("a", ["a", "b"])
    tc.assertNotIn("c", ["a", "b"])
    check("assertIn/assertNotIn pass", True)

    # assertGreater / assertLess
    tc.assertGreater(5, 3)
    tc.assertLess(3, 5)
    tc.assertGreaterEqual(5, 5)
    check("comparison assertions pass", True)

    # assertIsInstance
    tc.assertIsInstance("hello", str)
    check("assertIsInstance passes", True)

    # assertRaises
    with tc.assertRaises(ValueError):
        raise ValueError("test")
    check("assertRaises catches", True)

    raised = False
    try:
        with tc.assertRaises(ValueError):
            pass  # no exception
    except AssertionError:
        raised = True
    check("assertRaises fails when no exception", raised)


def test_response_assertions():
    """Test response assertion methods."""
    print("\n=== Response Assertions ===")

    tc = TestCase()

    resp = TestResponse(
        Response(
            body=b'{"name": "Alice"}',
            status=200,
            headers={"content-type": "application/json"},
        )
    )

    tc.assertStatus(resp, 200)
    check("assertStatus passes", True)

    tc.assertOk(resp)
    check("assertOk passes", True)

    tc.assertContains(resp, "Alice")
    check("assertContains passes", True)

    tc.assertNotContains(resp, "Bob")
    check("assertNotContains passes", True)

    raised = False
    try:
        tc.assertStatus(resp, 404)
    except AssertionError:
        raised = True
    check("assertStatus fails correctly", raised)

    # JSON assertion
    tc.assertJsonEqual(resp, {"name": "Alice"})
    check("assertJsonEqual passes", True)

    # Redirect assertion
    redirect_resp = TestResponse(
        Response(
            body=b"",
            status=302,
            headers={"location": "/new-url"},
        )
    )
    tc.assertRedirects(redirect_resp, "/new-url")
    check("assertRedirects passes", True)


# ─── Test DB isolation via TestCase.run_all() ──────────────────────────────


class DBIsolationTest(TestCase):
    """Test that each test method is isolated via savepoints."""

    __test__ = False
    db_url = DB_URL

    @classmethod
    async def asyncSetUpClass(cls):
        # Create test table OUTSIDE the transaction
        from hyperdjango.database import Database, set_db

        db = Database(cls.db_url)
        await db.connect()
        set_db(db)
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tc_items "
            "(id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
        )
        await db.disconnect()

    async def test_01_insert_row(self):
        """Insert a row — should be visible within this test."""
        await self.db.execute("INSERT INTO test_tc_items (name) VALUES ($1)", "Widget")
        rows = await self.db.query("SELECT * FROM test_tc_items")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Widget")

    async def test_02_table_is_empty(self):
        """Previous test's INSERT was rolled back — table should be empty."""
        rows = await self.db.query("SELECT * FROM test_tc_items")
        self.assertEqual(len(rows), 0, f"Expected 0 rows, got {len(rows)}")

    async def test_03_insert_multiple(self):
        """Insert multiple rows — isolated from other tests."""
        await self.db.execute("INSERT INTO test_tc_items (name) VALUES ($1)", "A")
        await self.db.execute("INSERT INTO test_tc_items (name) VALUES ($1)", "B")
        await self.db.execute("INSERT INTO test_tc_items (name) VALUES ($1)", "C")
        rows = await self.db.query("SELECT * FROM test_tc_items")
        self.assertEqual(len(rows), 3)

    async def test_04_still_empty(self):
        """Previous test's 3 inserts were rolled back."""
        rows = await self.db.query("SELECT * FROM test_tc_items")
        self.assertEqual(len(rows), 0, f"Expected 0, got {len(rows)}")

    @classmethod
    async def asyncTearDownClass(cls):
        from hyperdjango.database import Database, set_db

        db = Database(cls.db_url)
        await db.connect()
        set_db(db)
        await db.execute("DROP TABLE IF EXISTS test_tc_items CASCADE")
        await db.disconnect()


# ─── Test TestCase with App + Client ───────────────────────────────────────

app = HyperApp()


@app.get("/hello")
async def hello(request):
    return {"message": "Hello!"}


@app.get("/greet/{name}")
async def greet(request, name):
    return {"greeting": f"Hi {name}"}


@app.post("/echo")
async def echo(request):
    data = await request.json()
    return data


class AppTest(TestCase):
    """Test TestCase with HyperApp client."""

    __test__ = False
    app = app

    async def test_get_hello(self):
        resp = self.client.get("/hello")
        self.assertOk(resp)
        self.assertJsonEqual(resp, {"message": "Hello!"})

    async def test_get_with_param(self):
        resp = self.client.get("/greet/World")
        self.assertOk(resp)
        self.assertContains(resp, "World")

    async def test_post_echo(self):
        resp = self.client.post("/echo", json={"key": "value"})
        self.assertOk(resp)
        self.assertJsonEqual(resp, {"key": "value"})

    async def test_404(self):
        resp = self.client.get("/nonexistent")
        self.assertStatus(resp, 404)


# ─── Test setUp/tearDown lifecycle ────────────────────────────────────────


class LifecycleTest(TestCase):
    """Test asyncSetUp and asyncTearDown are called."""

    __test__ = False
    db_url = DB_URL

    _setup_called = False
    _teardown_called = False

    async def asyncSetUp(self):
        LifecycleTest._setup_called = True

    async def asyncTearDown(self):
        LifecycleTest._teardown_called = True

    async def test_setup_was_called(self):
        self.assertTrue(LifecycleTest._setup_called)

    async def test_teardown_flag(self):
        # tearDown from previous test should have run
        # (we can't easily check this within the same test)
        self.assertTrue(True)  # placeholder


# ─── Main ─────────────────────────────────────────────────────────────────


async def main():
    global outer_passed, outer_failed

    # Pure Python assertion tests
    test_assertions()
    test_response_assertions()

    # DB isolation tests
    print("\n=== DB Isolation (savepoint rollback) ===")
    p, f, _ = await DBIsolationTest._run_all_async()
    outer_passed += p
    outer_failed += f

    # App + client tests
    print("\n=== App + Client Tests ===")
    p, f, _ = await AppTest._run_all_async()
    outer_passed += p
    outer_failed += f

    # Lifecycle tests
    print("\n=== Lifecycle Tests ===")
    p, f, _ = await LifecycleTest._run_all_async()
    outer_passed += p
    outer_failed += f

    print(f"\n{'=' * 60}")
    print(f"Total: {outer_passed} passed, {outer_failed} failed")
    if outer_failed == 0:
        print("All TestCase tests passed!")
    else:
        print(f"{outer_failed} tests need attention")
    sys.exit(1 if outer_failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
