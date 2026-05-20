"""Validation gate: every ``scripts/test_*.py`` carries a canonical marker.

Wraps ``scripts/check_test_markers.py`` (the single marker authority) as a
subprocess and asserts a clean exit, so the CI lint job (``pytest
tests/test_no_*.py``) enforces the universal ``# hyper-test:`` classification
contract automatically. See that script for the rules; run it with ``--fix`` to
stamp or repair markers.
"""

import ast
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CHECKER = _ROOT / "scripts" / "check_test_markers.py"


def test_check_mode_imports_no_hyperdjango_at_module_level():
    """The CI lint job runs this gate with NO compiled native extension
    present. Any module-level ``hyperdjango`` import in the checker
    transitively loads the native module and breaks check mode there while
    passing everywhere a local build exists — the classify_test import must
    stay inside the --fix path only."""
    tree = ast.parse(_CHECKER.read_text())
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "hyperdjango"
        ):
            offenders.append(node.module)
        if isinstance(node, ast.Import):
            offenders.extend(
                a.name for a in node.names if a.name.startswith("hyperdjango")
            )
    assert not offenders, (
        f"check_test_markers.py imports {offenders} at module level; "
        f"check mode must run without the native build (CI lint job)"
    )


def test_no_test_markers():
    proc = subprocess.run(
        [sys.executable, str(_CHECKER)],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert proc.returncode == 0, (
        "check_test_markers.py reported test-marker violations. Run "
        "`uv run python scripts/check_test_markers.py --fix` to stamp/repair "
        "markers:\n" + proc.stdout + proc.stderr
    )
