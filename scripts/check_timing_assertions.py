#!/usr/bin/env python
"""Audit gate: no assertion whose truth depends on how fast the machine is.

Every CI failure in this suite's recent history has been the same shape: a test
starts asynchronous work, waits a FIXED duration, then asserts a consequence.
That passes on a fast dev box, where the work has finished by the time the
assertion runs, and fails on a loaded 2-core runner, where it has not. The
platform was correct every time; the tests were measuring the machine.

Two spellings of the same defect are reported:

  * ``time.sleep(...)`` / ``await asyncio.sleep(...)`` followed within a few
    lines by an assertion — the sleep is standing in for a condition nobody
    stated. Wait for the condition instead (poll a queue's ``pending``, a
    watcher's ``connected``, an acquire/release balance) so the test is exact
    on any machine.
  * A snapshot taken immediately after an operation that only affects FUTURE
    work (``remove``/``cancel``/``stop``/``unsubscribe``/``disconnect``) —
    already-queued work still lands, so the snapshot measures the backlog.

A genuinely-bounded NEGATIVE ("this must NOT happen within N seconds") is a
legitimate use of a sleep, and there is no condition to wait on; annotate it

    # timing-window: <what is being bounded and why a window is correct>

Run: uv run python scripts/check_timing_assertions.py [paths...]
Default paths: scripts/ tests/
"""

from __future__ import annotations

import pathlib
import re
import sys

from _slop_markers import has_marker

MARKER_NAME = "timing-window"

# A sleep whose duration is a literal — a computed/parameterised sleep is
# usually a helper's bounded poll, not a hand-tuned guess.
_SLEEP = re.compile(r"(?:time\.sleep|asyncio\.sleep)\(\s*[0-9.]+\s*\)")
# The assertion vocabularies used across this repo's test styles.
_ASSERT = re.compile(
    r"\b(?:assert|check|test_true|test_false|test_eq|self\.assert\w+)\b|"
    r"^\s*test\("
)
# Operations that only stop FUTURE work; anything already in flight still lands.
_FUTURE_ONLY_OPS = re.compile(
    r"\.(?:remove|cancel|stop|unsubscribe|disconnect|close|drop_all|revoke)\("
)

# How far after a sleep an assertion still counts as "gated by that sleep".
_WINDOW = 4


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    hits: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if not _SLEEP.search(line):
            continue
        # A sleep inside a polling loop IS the correct construct — it is how a
        # bounded wait-for-condition is spelled. Detect the enclosing loop by
        # looking for a `for`/`while` above at a shallower indent.
        if _in_polling_loop(lines, idx):
            continue
        window = lines[idx + 1 : idx + 1 + _WINDOW]
        if not any(_ASSERT.search(w) for w in window):
            continue
        if has_marker(lines, idx + 1, idx + 1 + _WINDOW, MARKER_NAME):
            continue
        hits.append((idx + 1, line.strip()))
    return hits


def _in_polling_loop(lines: list[str], idx: int) -> bool:
    """True if the sleep sits inside a for/while whose body it paces."""
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    for j in range(idx - 1, max(-1, idx - 12), -1):
        stripped = lines[j].strip()
        if not stripped:
            continue
        j_indent = len(lines[j]) - len(lines[j].lstrip())
        if j_indent < indent and (
            stripped.startswith("for ") or stripped.startswith("while ")
        ):
            return True
        if j_indent < indent:
            return False
    return False


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(p) for p in argv[1:]] or [
        pathlib.Path("scripts"),
        pathlib.Path("tests"),
    ]
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("test_*.py")))
        elif root.suffix == ".py":
            files.append(root)

    total = 0
    for f in files:
        for lineno, text in check_file(f):
            print(f"{f}:{lineno}: assertion gated by a fixed sleep — {text}")
            total += 1

    if total:
        print(
            f"\nFAILED: {total} assertion(s) whose truth depends on machine speed.\n"
            "Wait for the CONDITION (poll a queue's pending, a watcher's\n"
            "connected flag, an acquire/release balance) instead of sleeping a\n"
            "guessed duration, OR — for a genuinely bounded negative — annotate\n"
            "it `# timing-window: <what is bounded and why>`.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: no sleep-gated assertions in {len(files)} test files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
