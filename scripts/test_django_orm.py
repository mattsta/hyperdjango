#!/usr/bin/env python3
"""
Test Django ORM through the hyperdjango.db backend against real PostgreSQL.

Prerequisites:
    PostgreSQL running on localhost:5432
    createdb hyperdjango_test

Run: uv run hyper-test django_orm
"""

# hyper-test: db_django

import os
import sys
from pathlib import Path

# Setup Django with our backend
os.environ["DJANGO_SETTINGS_MODULE"] = "scripts._test_django_settings"
sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: {detail}")


# Create settings module
settings_path = Path(__file__).parent / "_test_django_settings.py"
settings_path.write_text("""
import os
SECRET_KEY = 'test-key'
INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'hyperdjango',
]
DATABASES = {
    'default': {
        'ENGINE': 'hyperdjango.db',
        'NAME': 'hyperdjango_test',
        'USER': os.environ.get('USER', 'postgres'),
        'PASSWORD': '',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
""")

# Create the test database
import subprocess

user = os.environ.get("USER", "postgres")
subprocess.run(["createdb", "hyperdjango_test"], capture_output=True)

import django

django.setup()

print("=== Django ORM through hyperdjango.db backend ===")
print()

from django.db import connection

# Check what backend we're using
print(f"Backend: {connection.vendor}")
print(
    f"Display: {connection.display_name if hasattr(connection, 'display_name') else 'N/A'}"
)
print()

# Test basic connection
print("--- Connection ---")
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
        check("SELECT 1", row is not None and row[0] in (1, "1"), f"got {row}")
except Exception as e:
    check("SELECT 1", False, str(e))

# Test version query
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        row = cursor.fetchone()
        check(
            "SELECT version()",
            row is not None and "PostgreSQL" in str(row[0]),
            str(row),
        )
except Exception as e:
    check("SELECT version()", False, str(e))

# Test table creation
print("\n--- Schema ---")
try:
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(200),
                age INTEGER DEFAULT 0
            )
        """)
    connection.commit() if hasattr(connection, "commit") else None
    check("CREATE TABLE", True)
except Exception as e:
    check("CREATE TABLE", False, str(e))

# Test INSERT
print("\n--- Insert ---")
try:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO test_users (name, email, age) VALUES (%s, %s, %s)",
            ["Alice", "alice@example.com", 30],
        )
    check("INSERT single row", True)
except Exception as e:
    check("INSERT single row", False, str(e))

# Test SELECT
print("\n--- Query ---")
try:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, email, age FROM test_users WHERE name = %s", ["Alice"]
        )
        rows = cursor.fetchall()
        check("SELECT with params", len(rows) > 0, f"got {len(rows)} rows")
        if rows:
            check("Row data correct", "Alice" in str(rows[0]), str(rows[0]))
except Exception as e:
    check("SELECT with params", False, str(e))

# Test UPDATE — assert the exact affected-row count AND that the row changed.
print("\n--- Update ---")
try:
    with connection.cursor() as cursor:
        cursor.execute("UPDATE test_users SET age = %s WHERE name = %s", [31, "Alice"])
        check(
            "UPDATE rowcount == 1", cursor.rowcount == 1, f"rowcount={cursor.rowcount}"
        )
        cursor.execute("SELECT age FROM test_users WHERE name = %s", ["Alice"])
        row = cursor.fetchone()
        check(
            "UPDATE persisted (age == 31)", row is not None and row[0] == 31, str(row)
        )
except Exception as e:
    check("UPDATE", False, str(e))

# Test DELETE — assert the exact affected-row count AND that the row is gone.
print("\n--- Delete ---")
try:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM test_users WHERE name = %s", ["Alice"])
        check(
            "DELETE rowcount == 1", cursor.rowcount == 1, f"rowcount={cursor.rowcount}"
        )
        cursor.execute("SELECT COUNT(*) FROM test_users WHERE name = %s", ["Alice"])
        remaining = cursor.fetchone()[0]
        check(
            "DELETE removed the row (0 remaining)",
            remaining == 0,
            f"remaining={remaining}",
        )
except Exception as e:
    check("DELETE", False, str(e))

# Cleanup
try:
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS test_users")
except Exception:
    pass

# Cleanup temp settings
settings_path.unlink()

print(f"\n{'=' * 50}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("  Django ORM through hyperdjango.db works!")
else:
    print(f"  {FAIL} checks need attention")
    sys.exit(1)
