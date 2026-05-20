"""Test Django manage.py migrate works with hyperdjango.db backend.

Tests the full migration pipeline: creating tables, applying migrations,
running syncdb, and verifying the resulting schema.

Requires PostgreSQL running on localhost:5432.

Run: uv run pytest tests/test_db/test_django_migrate.py -v
"""

import contextlib
import os
import subprocess
from pathlib import Path

import pytest
from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_query,
)

# Module-level pool for the migrate test database
_mig_pool_handle = None


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    global _mig_pool_handle
    user = os.environ.get("USER", "postgres")
    subprocess.run(["createdb", "hyperdjango_migrate_test"], capture_output=True)
    _mig_pool_handle = _db_configure(
        f"postgresql://{user}:@localhost:5432/hyperdjango_migrate_test", 2
    )
    yield
    if _mig_pool_handle is not None:
        _db_close_pool(_mig_pool_handle)
    subprocess.run(
        ["dropdb", "--if-exists", "hyperdjango_migrate_test"], capture_output=True
    )


@pytest.fixture(autouse=True)
def clean_schema():
    """Reset schema before each test."""
    try:
        _db_execute(_mig_pool_handle, "DROP SCHEMA public CASCADE", [])
        _db_execute(_mig_pool_handle, "CREATE SCHEMA public", [])
    except Exception:
        pass
    yield


class TestDjangoMigrateCommand:
    """Test that Django's migrate command works through our backend."""

    def _run_migrate(self, extra_args=None):
        """Run manage.py migrate via subprocess with our backend."""
        user = os.environ.get("USER", "postgres")
        settings_content = f"""
import os
SECRET_KEY = 'migrate-test'
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth']
DATABASES = {{
    'default': {{
        'ENGINE': 'hyperdjango.db',
        'NAME': 'hyperdjango_migrate_test',
        'USER': '{user}',
        'HOST': 'localhost',
        'PORT': '5432',
    }}
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
"""
        settings_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "_migrate_test_settings.py"
        )
        settings_path.write_text(settings_content)

        cmd = [
            "uv",
            "run",
            "python",
            "-m",
            "django",
            "migrate",
            "--settings=scripts._migrate_test_settings",
            "--verbosity=0",
        ]
        if extra_args:
            cmd.extend(extra_args)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).parent.parent.parent),
        )

        with contextlib.suppress(OSError):
            settings_path.unlink()

        return result

    def test_migrate_creates_auth_tables(self):
        """manage.py migrate should create Django's auth tables."""
        result = self._run_migrate()
        assert result.returncode == 0, (
            f"migrate failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Verify key tables exist
        for table in [
            "auth_user",
            "auth_group",
            "auth_permission",
            "django_content_type",
            "django_migrations",
        ]:
            rows = _db_query(
                _mig_pool_handle,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = $1 AND table_schema = $2",
                [table, "public"],
            )
            assert len(rows) == 1, f"Table {table} not found after migrate"

    def test_migrate_creates_migration_records(self):
        """migrate should record applied migrations in django_migrations."""
        result = self._run_migrate()
        assert result.returncode == 0, f"migrate failed:\nstderr: {result.stderr}"

        rows = _db_query(
            _mig_pool_handle, "SELECT app, name FROM django_migrations ORDER BY id", []
        )
        assert len(rows) > 0, "No migration records found"

        # Should have contenttypes and auth migrations
        apps = {r[0] for r in rows}
        assert "contenttypes" in apps
        assert "auth" in apps

    def test_migrate_idempotent(self):
        """Running migrate twice should succeed (no errors)."""
        result1 = self._run_migrate()
        assert result1.returncode == 0, (
            f"First migrate failed:\nstderr: {result1.stderr}"
        )

        result2 = self._run_migrate()
        assert result2.returncode == 0, (
            f"Second migrate failed:\nstderr: {result2.stderr}"
        )

    def test_migrate_showmigrations(self):
        """manage.py showmigrations should work after migrate."""
        self._run_migrate()
        user = os.environ.get("USER", "postgres")
        settings_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "_migrate_test_settings.py"
        )
        settings_path.write_text(f"""
import os
SECRET_KEY = 'test'
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth']
DATABASES = {{'default': {{'ENGINE': 'hyperdjango.db', 'NAME': 'hyperdjango_migrate_test', 'USER': '{user}', 'HOST': 'localhost', 'PORT': '5432'}}}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
""")
        result = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "django",
                "showmigrations",
                "--settings=scripts._migrate_test_settings",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        with contextlib.suppress(OSError):
            settings_path.unlink()

        assert result.returncode == 0, (
            f"showmigrations failed:\nstderr: {result.stderr}"
        )
        assert "[X]" in result.stdout, "No applied migrations shown"
