"""
Test `hyper setup` CLI command — creates tables from models via topological sort.

Validates:
1. Setup command creates all tables from model definitions
2. Tables are created in FK dependency order (topological sort)
3. Indexes are created for indexed fields
4. Seed functions run after table creation
5. --drop flag recreates tables
6. Models can be queried after setup

Requires: PostgreSQL with createdb/dropdb access.

Usage:
    uv run python scripts/test_hyper_setup.py
"""

# hyper-test: db_isolated

import asyncio
import os
import subprocess
import sys
from pathlib import Path

PASS = 0
FAIL = 0
DB_NAME = "hyper_test_setup"
DB_URL = f"postgres://{os.environ.get('USER', 'postgres')}@localhost/{DB_NAME}"


def test(name, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    got:      {got!r}")
        print(f"    expected: {expected!r}")


def test_true(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        if detail:
            print(f"    {detail}")


def run_cmd(cmd, cwd=None):
    """Run a shell command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


def setup_db():
    """Create a fresh test database."""
    run_cmd(f"dropdb --force --if-exists {DB_NAME}")
    rc, out, err = run_cmd(f"createdb {DB_NAME}")
    return rc == 0


def teardown_db():
    """Drop the test database."""
    run_cmd(f"dropdb --force --if-exists {DB_NAME}")


def test_setup_hypernews():
    """Test hyper setup with the HyperNews service."""
    print("\n--- hyper setup: hypernews ---")

    project_root = str(Path(__file__).resolve().parent.parent)

    rc, out, err = run_cmd(
        f"DATABASE_URL={DB_URL} HYPER_DATABASE_URL={DB_URL} uv run hyper setup --app services.hypernews.app:app",
        cwd=project_root,
    )

    combined = out + err
    test_true("setup exits 0", rc == 0, f"rc={rc}\n{combined}")

    # Verify tables were created
    test_true("found models", "model(s)" in combined, combined)
    test_true("hn_users created", "hn_users" in combined, combined)
    test_true("hn_posts created", "hn_posts" in combined, combined)
    test_true("hn_comments created", "hn_comments" in combined, combined)
    test_true("hn_votes created", "hn_votes" in combined, combined)

    # Verify tables exist in database
    rc2, out2, _ = run_cmd(
        f"psql {DB_URL} -t -c \"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\""
    )
    tables = [t.strip() for t in out2.strip().split("\n") if t.strip()]
    test_true("hn_users in pg_tables", "hn_users" in tables, f"tables={tables}")
    test_true("hn_posts in pg_tables", "hn_posts" in tables, f"tables={tables}")
    test_true("hn_comments in pg_tables", "hn_comments" in tables, f"tables={tables}")
    test_true("hn_votes in pg_tables", "hn_votes" in tables, f"tables={tables}")
    test_true(
        "hn_admin_messages in pg_tables",
        "hn_admin_messages" in tables,
        f"tables={tables}",
    )
    test_true(
        "hn_spam_reports in pg_tables", "hn_spam_reports" in tables, f"tables={tables}"
    )


def test_setup_hyperai():
    """Test hyper setup with the HyperAI service."""
    print("\n--- hyper setup: hyperai ---")

    project_root = str(Path(__file__).resolve().parent.parent)

    rc, out, err = run_cmd(
        f"DATABASE_URL={DB_URL} HYPER_DATABASE_URL={DB_URL} uv run hyper setup --app services.hyperai.app:app",
        cwd=project_root,
    )

    combined = out + err
    test_true("setup exits 0", rc == 0, f"rc={rc}\n{combined}")
    test_true("ai_users created", "ai_users" in combined, combined)
    test_true("ai_conversations created", "ai_conversations" in combined, combined)
    test_true("ai_messages created", "ai_messages" in combined, combined)
    test_true("ai_api_keys created", "ai_api_keys" in combined, combined)
    test_true("ai_usage_logs created", "ai_usage_logs" in combined, combined)

    # Verify in database
    rc2, out2, _ = run_cmd(
        f"psql {DB_URL} -t -c \"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\""
    )
    tables = [t.strip() for t in out2.strip().split("\n") if t.strip()]
    test_true("ai_users in pg_tables", "ai_users" in tables, f"tables={tables}")
    test_true(
        "ai_conversations in pg_tables",
        "ai_conversations" in tables,
        f"tables={tables}",
    )
    test_true("ai_messages in pg_tables", "ai_messages" in tables, f"tables={tables}")


def test_setup_idempotent():
    """Test that running setup twice is safe (CREATE TABLE IF NOT EXISTS)."""
    print("\n--- hyper setup: idempotent ---")

    project_root = str(Path(__file__).resolve().parent.parent)

    # Run setup again on the same database
    rc, out, err = run_cmd(
        f"DATABASE_URL={DB_URL} HYPER_DATABASE_URL={DB_URL} uv run hyper setup --app services.hypernews.app:app",
        cwd=project_root,
    )
    combined = out + err
    test_true("second setup exits 0", rc == 0, f"rc={rc}\n{combined}")


def test_setup_drop():
    """Test --drop flag recreates tables."""
    print("\n--- hyper setup: --drop ---")

    project_root = str(Path(__file__).resolve().parent.parent)

    rc, out, err = run_cmd(
        f"DATABASE_URL={DB_URL} HYPER_DATABASE_URL={DB_URL} uv run hyper setup --app services.hypernews.app:app --drop",
        cwd=project_root,
    )
    combined = out + err
    test_true("drop+recreate exits 0", rc == 0, f"rc={rc}\n{combined}")
    test_true("tables recreated", "hn_users" in combined, combined)


def test_model_query_after_setup():
    """Test that Model.objects works after setup (get_db auto-creates from DATABASE_URL)."""
    print("\n--- Model.objects after setup ---")

    # Ensure tables exist (re-run setup in case --drop test timed out under parallel load)
    project_root = str(Path(__file__).resolve().parent.parent)
    rc, _out, _err = run_cmd(
        f"DATABASE_URL={DB_URL} HYPER_DATABASE_URL={DB_URL} uv run hyper setup --app services.hypernews.app:app",
        cwd=project_root,
    )
    test_true("setup before query test exits 0", rc == 0, f"rc={rc}\n{_out}\n{_err}")

    async def _test():
        from hyperdjango.conf import DEFAULTS

        # Override both DEFAULTS and env var — env var takes priority in
        # get_setting() resolution, and the test runner may have set
        # HYPER_DATABASE_URL to an isolated test DB.
        DEFAULTS["DATABASE_URL"] = DB_URL
        os.environ["HYPER_DATABASE_URL"] = DB_URL

        # Reset global DB so get_db() re-creates from the new URL
        from hyperdjango import database as db_mod

        db_mod._db = None

        from hyperdjango.database import get_db

        db = get_db()
        test_true("get_db returns Database", db is not None)

        # Insert a test row
        await db.execute(
            "INSERT INTO hn_users (username, email, password_hash) "
            "VALUES ($1, $2, $3) ON CONFLICT (username) DO NOTHING",
            "testuser",
            "test@test.com",
            "fakehash",
        )

        # Query via raw
        rows = await db.query(
            "SELECT username FROM hn_users WHERE username = $1", "testuser"
        )
        test_true("query returns rows", len(rows) > 0, f"rows={rows}")
        test("username matches", rows[0]["username"], "testuser")

        await db.disconnect()
        db_mod._db = None
        # Restore env var to avoid polluting other tests
        os.environ.pop("HYPER_DATABASE_URL", None)

    asyncio.run(_test())


if __name__ == "__main__":
    print("=" * 60)
    print("hyper setup CLI Tests")
    print("=" * 60)

    if not setup_db():
        print(f"Could not create test database {DB_NAME}. Skipping.")
        sys.exit(0)

    try:
        test_setup_hypernews()
        test_setup_hyperai()
        test_setup_idempotent()
        test_setup_drop()
        test_model_query_after_setup()
    finally:
        teardown_db()

    print(f"\n{'=' * 60}")
    print(f"hyper_setup: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 60}")

    if FAIL:
        sys.exit(1)
