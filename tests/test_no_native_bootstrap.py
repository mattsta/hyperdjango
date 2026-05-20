"""Validation gate: the bootstrap spine imports WITHOUT the native extension.

``hyper-build`` (which PRODUCES the native extension), the test runner, the
source-invariant gates, and everything they import must work in environments
where no compiled extension can exist: a fresh checkout, the CI lint job,
sdist builds. A module-level native import anywhere on that spine recreates
the chicken-and-egg failure where the build tool cannot run until the
artifact it builds already exists.

The check runs each spine entry in a subprocess with a meta-path finder that
BLOCKS ``hyperdjango._hyperdjango_native`` outright, so it fails even on
machines that have a local build — the same environment-difference class the
CI lint job keeps exposing after local runs pass.
"""

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_BLOCKER_PRELUDE = """\
import importlib.abc
import sys


class _BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "hyperdjango._hyperdjango_native":
            raise ImportError("native extension blocked: bootstrap-spine gate")
        return None


sys.meta_path.insert(0, _BlockNative())
"""

# Each entry is (label, code executed after the blocker prelude). Keep these
# to the true bootstrap spine — runtime modules may hard-require native.
_SPINE = [
    (
        "hyperdjango.logging emits a record",
        "from hyperdjango.logging import logger; logger.info('bootstrap-gate')",
    ),
    (
        "hyperdjango.build (hyper-build entry point)",
        "import hyperdjango.build",
    ),
    (
        "hyperdjango.test_runner classification",
        "from hyperdjango.test_runner import classify_test",
    ),
    (
        "marker gate check mode",
        "import runpy, sys; sys.argv = ['check_test_markers.py']; "
        "runpy.run_path('scripts/check_test_markers.py', run_name='__main__')",
    ),
]


def test_incompatible_interpreter_aborts_before_building():
    """A standard (GIL-enabled) CPython must ABORT hyper-build up front with
    remediation — never reach zig, whose failure mode is a cryptic cimport
    error ("no field named 'ob_refcnt'") minutes into the build."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'zig'); "
            "import build_hyperdjango as b; "
            "b.require_compatible_toolchain("
            "{'free_threaded': False, 'version': '3.14.4'})",
        ],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "incompatible Python" in proc.stderr
    assert "uv python install 3.14t" in proc.stderr  # remediation, not just a no


def test_bootstrap_spine_imports_without_native():
    failures = []
    for label, code in _SPINE:
        proc = subprocess.run(
            [sys.executable, "-c", _BLOCKER_PRELUDE + code],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        # The marker gate exits via SystemExit(0) on success; imports exit 0.
        if proc.returncode != 0:
            failures.append(f"{label}:\n{proc.stdout}{proc.stderr}")
    assert not failures, (
        "bootstrap spine requires the native extension (must import/run "
        "without it):\n\n" + "\n\n".join(failures)
    )
