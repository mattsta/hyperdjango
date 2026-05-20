#!/usr/bin/env python3
"""
Run Django's own backend test suite against the hyperdjango.db backend.

Uses Django's own runtests.py infrastructure for proper test setup.

Usage: uv run python scripts/run_django_backend_tests.py [test_module...]
Default: backends.tests
"""

import contextlib
import os
import subprocess
import sys
from pathlib import Path

user = os.environ.get("USER", "postgres")
subprocess.run(["createdb", "hyperdjango_test"], capture_output=True)
subprocess.run(["createdb", "hyperdjango_test_other"], capture_output=True)

# Django's own test suite lives in a separate Django checkout, not in this repo.
# Point DJANGO_TESTS_DIR at <django-checkout>/tests to run it.
django_tests_env = os.environ.get("DJANGO_TESTS_DIR")
if not django_tests_env:
    print(
        "ERROR: set DJANGO_TESTS_DIR to your Django checkout's tests/ directory, "
        "e.g. DJANGO_TESTS_DIR=/path/to/django/tests"
    )
    sys.exit(1)
django_tests_dir = Path(django_tests_env).expanduser()
if not django_tests_dir.is_dir():
    print(f"ERROR: Django tests not found at {django_tests_dir}")
    sys.exit(1)

# Determine test labels
test_labels = sys.argv[1:] if len(sys.argv) > 1 else ["backends.tests"]

# Use Django's runtests.py with our backend via --settings
# First create a settings module that Django's runtests.py can use
settings_path = django_tests_dir / "hyperdjango_test_settings.py"
settings_path.write_text(f"""
DATABASES = {{
    'default': {{
        'ENGINE': 'hyperdjango.db',
        'NAME': 'hyperdjango_test',
        'USER': '{user}',
        'PASSWORD': '',
        'HOST': 'localhost',
        'PORT': '5432',
    }},
    'other': {{
        'ENGINE': 'hyperdjango.db',
        'NAME': 'hyperdjango_test_other',
        'USER': '{user}',
        'PASSWORD': '',
        'HOST': 'localhost',
        'PORT': '5432',
    }},
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SECRET_KEY = 'django-backend-test'
USE_TZ = False
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
""")

# Run Django's runtests.py with our settings
cmd = [
    sys.executable,
    str(django_tests_dir / "runtests.py"),
    "--settings=hyperdjango_test_settings",
    "--verbosity=2",
    "--noinput",
    "--parallel=1",
] + test_labels

# Make sure hyperdjango is importable
env = os.environ.copy()
python_path = str(Path(__file__).resolve().parent.parent)
if "PYTHONPATH" in env:
    env["PYTHONPATH"] = python_path + ":" + env["PYTHONPATH"]
else:
    env["PYTHONPATH"] = python_path

result = subprocess.run(cmd, cwd=str(django_tests_dir), env=env)

with contextlib.suppress(OSError):
    settings_path.unlink()

sys.exit(result.returncode)
