#!/usr/bin/env python3
"""Reusable failure-triage helper for the hyper-test suite.

Reads the most recent ``logs/test_runs/*_all.log`` and the per-file
``logs/test_runs/subprocess/<name>.log`` files, and prints a concise report:
each failed file plus the first few assertion/error lines from its subprocess
log. Beats ad-hoc shell greps — deterministic, reusable across iterations.

Usage:
    uv run python scripts/_triage_failures.py            # latest run
    uv run python scripts/_triage_failures.py --context 12
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "logs" / "test_runs"
SUB = RUNS / "subprocess"

# Lines in a subprocess log that signal a concrete failure/cause.
SIGNAL = re.compile(
    r"(FAIL|✗|AssertionError|Error:|Exception|Traceback|Expected|!=|"
    r"DoesNotExist|status=\d{3}|assert )",
)
NOISE = re.compile(r"^\s*(✓|PASS|# Passed|Results:|ok\b)")


def latest_all_log() -> Path | None:
    logs = sorted(RUNS.glob("*_all.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def failed_files(all_log: Path) -> list[tuple[str, str]]:
    """Return (name, summary) for each file the runner marked failed.

    The runner writes these lines with a logger prefix
    (``... :1073 -   <name>: N failures (exit K)``), so we search anywhere in the
    line rather than anchoring at the start. A file counts as failed on any
    nonzero exit, including "0 failures (exit 1)" (a crash/teardown with no
    per-test assertion failure).
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in all_log.read_text(errors="replace").splitlines():
        m = re.search(r"(\S+): (\d+ failures? \(exit (\d+)\))", line)
        if m and m.group(3) != "0" and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), m.group(2)))
    return out


def subprocess_log(name: str) -> Path | None:
    # runner writes subprocess/<name>.log; "pytest:standalone" -> pytest__standalone
    cand = SUB / f"{name.replace(':', '__')}.log"
    if cand.exists():
        return cand
    hits = list(SUB.glob(f"{name}*.log"))
    return hits[0] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", type=int, default=8)
    args = ap.parse_args()

    all_log = latest_all_log()
    if not all_log:
        print("no *_all.log found")
        return
    print(f"# triage of {all_log.name}\n")

    fails = failed_files(all_log)
    if not fails:
        print("no failed files 🎉")
        return

    for name, summary in fails:
        print(f"==== {name} — {summary} ====")
        log = subprocess_log(name)
        if not log:
            print("  (no subprocess log found)\n")
            continue
        shown = 0
        for line in log.read_text(errors="replace").splitlines():
            if NOISE.match(line):
                continue
            if SIGNAL.search(line):
                print("  " + line.strip()[:200])
                shown += 1
                if shown >= args.context:
                    break
        print()


if __name__ == "__main__":
    main()
