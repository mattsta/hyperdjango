#!/usr/bin/env python3
"""Attribute a slow test file's wall time to its individual sections.

`hyper-test` reports one duration per FILE, which is the wrong resolution when
a file approaches its timeout: it says the file is slow, not which part of it
is. Test files here print a section header per group (a line beginning `==` or
`--`), so timestamping stdout and differencing consecutive headers turns that
into a per-section breakdown with no changes to the file under test.

    uv run python scripts/time_test_sections.py scripts/test_serviceclient_unit.py
    uv run python scripts/time_test_sections.py <file> --top 15

Reads the child's stdout live, so a file that hangs still reports which section
it hung in — which a post-hoc log cannot tell you if the run was killed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Section:
    name: str
    started: float
    ended: float = 0.0
    lines: int = 0

    @property
    def seconds(self) -> float:
        return (self.ended or time.monotonic()) - self.started


@dataclass(slots=True)
class Timeline:
    sections: list[Section] = field(default_factory=list)

    def header(self, name: str, now: float) -> None:
        if self.sections:
            self.sections[-1].ended = now
        self.sections.append(Section(name=name, started=now))

    def line(self) -> None:
        if self.sections:
            self.sections[-1].lines += 1

    def close(self, now: float) -> None:
        if self.sections:
            self.sections[-1].ended = now


def _is_header(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("==") or stripped.startswith("--")


def run(path: Path, top: int) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-u", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    timeline = Timeline()
    start = time.monotonic()
    assert proc.stdout is not None
    for raw in proc.stdout:
        now = time.monotonic()
        if _is_header(raw):
            timeline.header(raw.strip(), now)
        else:
            timeline.line()
    proc.wait()
    total = time.monotonic() - start
    timeline.close(time.monotonic())

    ranked = sorted(timeline.sections, key=lambda s: -s.seconds)[:top]
    print(f"\n{'=' * 70}")
    print(f"{path.name}: {total:.1f}s total, {len(timeline.sections)} sections")
    print(f"{'=' * 70}")
    for section in ranked:
        share = (section.seconds / total * 100) if total else 0.0
        print(f"  {section.seconds:7.2f}s  {share:5.1f}%  {section.name[:64]}")
    accounted = sum(s.seconds for s in ranked)
    print(f"\n  top {len(ranked)} account for {accounted:.1f}s of {total:.1f}s")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()
    if not args.path.is_file():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 2
    return run(args.path, args.top)


if __name__ == "__main__":
    sys.exit(main())
