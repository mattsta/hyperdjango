"""Validation gate: no assertion may depend on how fast the machine is.

Fails if a test sleeps a fixed duration and then asserts a consequence of
asynchronous work. That shape passes on a fast dev box, where the work has
finished by the time the assertion runs, and fails on a loaded CI runner where
it has not — the failure mode that produced a long run of red CI with a
different test named each time, while the platform was correct throughout.

The fix is always to wait for the CONDITION the assertion depends on (a queue's
``pending``, a watcher's ``connected``, an acquire/release balance, a row
appearing) so the same test is exact on any machine. Where the assertion is a
genuinely bounded NEGATIVE — "this must NOT happen within N seconds" — a window
is the correct construct and there is nothing to wait for; those carry a
``# timing-window: <why>`` annotation.

See scripts/check_timing_assertions.py for the rule.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_timing_assertions as gate  # noqa: E402


def _all_violations() -> list[str]:
    out: list[str] = []
    for root in (_ROOT / "scripts", _ROOT / "tests"):
        for path in sorted(root.rglob("test_*.py")):
            for lineno, text in gate.check_file(path):
                out.append(f"{path.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_sleep_gated_assertions() -> None:
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} assertion(s) gated by a fixed sleep. Wait for the "
        f"condition the assertion depends on, or annotate a genuinely bounded "
        f"negative with `# timing-window: <why>`:\n  " + "\n  ".join(violations)
    )
