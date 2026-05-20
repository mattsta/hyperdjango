#!/usr/bin/env python3
"""
Test manage.py inspectdb through hyperdjango.db backend.

Creates test tables, runs inspectdb, verifies output contains model definitions.

Run: uv run hyper-test inspectdb
"""

# hyper-test: db_django

import contextlib
import os
import subprocess
from pathlib import Path

from hyperdjango.testkit import check, finish, run_main

user = os.environ.get("USER", "postgres")
subprocess.run(["createdb", "hyperdjango_test"], capture_output=True)

# Create a temporary Django settings file
settings_content = f"""
DATABASES = {{
    'default': {{
        'ENGINE': 'hyperdjango.db',
        'NAME': 'hyperdjango_test',
        'USER': '{user}',
        'PASSWORD': '',
        'HOST': 'localhost',
        'PORT': '5432',
    }},
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SECRET_KEY = 'inspectdb-test'
INSTALLED_APPS = ['django.contrib.contenttypes']
"""

# Write settings to a temp file in project root so it's importable
settings_path = Path(__file__).resolve().parent.parent / "_inspectdb_settings.py"
settings_path.write_text(settings_content)

os.environ["DJANGO_SETTINGS_MODULE"] = "_inspectdb_settings"

try:
    import django

    django.setup()

    from django.db import connection

    # Create test tables
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS inspectdb_child CASCADE")
        cursor.execute("DROP TABLE IF EXISTS inspectdb_parent CASCADE")
        cursor.execute("""
            CREATE TABLE inspectdb_parent (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(255) UNIQUE,
                age INTEGER,
                score NUMERIC(10, 2),
                active BOOLEAN DEFAULT true,
                data JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                tags TEXT[]
            )
        """)
        cursor.execute("""
            CREATE TABLE inspectdb_child (
                id SERIAL PRIMARY KEY,
                parent_id INTEGER REFERENCES inspectdb_parent(id) ON DELETE CASCADE,
                label VARCHAR(50) NOT NULL,
                value DOUBLE PRECISION
            )
        """)

    # Run inspectdb
    from io import StringIO

    from django.core.management import call_command

    output = StringIO()
    call_command("inspectdb", stdout=output)
    result = output.getvalue()

    print("inspectdb output:")
    print("=" * 60)
    print(result)
    print("=" * 60)

    # Verify output
    check("InspectdbParent model generated", "class InspectdbParent" in result)
    check("InspectdbChild model generated", "class InspectdbChild" in result)
    check("'name' field definition present", "name = models." in result)
    check(
        "foreign key relationship present",
        "parent = models.ForeignKey" in result or "parent_id" in result,
    )

    # Cleanup
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS inspectdb_child CASCADE")
        cursor.execute("DROP TABLE IF EXISTS inspectdb_parent CASCADE")

    print()
    run_main(finish)

finally:
    with contextlib.suppress(OSError):
        settings_path.unlink()
