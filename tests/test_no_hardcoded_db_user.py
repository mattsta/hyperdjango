"""Validation gate: no hardcoded database role in a connection URL.

Wraps ``scripts/check_no_hardcoded_db_user.py`` as a subprocess so the CI lint
job (``pytest tests/test_no_*.py``) enforces it. A URL literal carrying one
developer's OS username builds and passes on that machine and fails on every
other one — see the checker for the rule and its fixture escape hatch.
"""

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CHECKER = _ROOT / "scripts" / "check_no_hardcoded_db_user.py"


def test_no_hardcoded_db_user():
    proc = subprocess.run(
        [sys.executable, str(_CHECKER)],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert proc.returncode == 0, (
        "hardcoded database role(s) found in PostgreSQL URLs:\n"
        + proc.stdout
        + proc.stderr
    )


def _checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_dbuser_check", _CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checker_flags_hardcoded_roles_in_defaults(tmp_path):
    """The gate must catch the exact shape that broke a fresh Ubuntu box."""
    mod = _checker()

    env_default = tmp_path / "env_default.py"
    env_default.write_text(
        "import os\n"
        'DB_URL = os.environ.get("DATABASE_URL", "postgres://alice@localhost/db")\n'
    )
    assert mod.check_file(env_default), "DATABASE_URL fallback role slipped past"

    getenv_default = tmp_path / "getenv_default.py"
    getenv_default.write_text(
        'import os\nDB_URL = os.getenv("HYPER_DATABASE_URL", "postgres://alice@h/db")\n'
    )
    assert mod.check_file(getenv_default), "os.getenv fallback role slipped past"

    const = tmp_path / "const.py"
    const.write_text('DATABASE_URL = "postgres://alice@localhost/db"\n')
    assert mod.check_file(const), "constant DB URL default slipped past"


def test_checker_accepts_correct_and_synthetic_forms(tmp_path):
    """Precision matters: a gate that flags fixtures gets blanket-suppressed."""
    mod = _checker()

    userless = tmp_path / "userless.py"
    userless.write_text(
        "import os\n"
        'DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/db")\n'
    )
    assert not mod.check_file(userless), "userless default must be accepted"

    interpolated = tmp_path / "interp.py"
    interpolated.write_text(
        "import os\n"
        'DB_URL = os.environ.get("DATABASE_URL", f"postgres://{user}@h/db")\n'
    )
    assert not mod.check_file(interpolated), "interpolated role must be accepted"

    # Parser input / expected value / unreachable probe — not a default.
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        'parsed = urlparse("postgres://alice:pw@dbhost:5433/mydb")\n'
        'assert resolve() == "postgresql://pguser@pghost:5432/pgdb"\n'
    )
    assert not mod.check_file(fixture), "URL fixtures must not be flagged"

    marked = tmp_path / "marked.py"
    marked.write_text(
        "import os\n"
        "# db-url-fixture: exercises the role-bearing branch on purpose\n"
        'DB_URL = os.environ.get("DATABASE_URL", "postgres://alice@h/db")\n'
    )
    assert not mod.check_file(marked), "marked exception must be accepted"
