"""Assertion harness shared by the standalone ``scripts/test_*.py`` programs.

Each test file is run as its OWN subprocess by the runner, so a module-level
default :class:`TestRun` is safe: there is no cross-test state sharing inside a
process. The top-level :func:`check` / :func:`finish` delegate to that default
instance, so a hand-rolled ``check()`` migrates to the harness by a pure import
swap — no call-site changes.

The output format is a contract: the runner parses the ``Results: N passed, M
failed`` line that :meth:`TestRun.finish` emits.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NoReturn

_PASS_PREFIX = "  PASS  "
_FAIL_PREFIX = "  FAIL  "


@dataclass(slots=True)
class TestRun:
    """A single test file's pass/fail tally.

    ``check`` records one assertion; ``finish`` prints the machine-parsed
    summary line and returns whether the run was clean.
    """

    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, name: str, cond: object, detail: str = "") -> bool:
        """Record one assertion. Prints ``  PASS  <name>`` or
        ``  FAIL  <name>  <detail>``, updates the counters, and returns the
        truthiness of ``cond`` so a call site can branch on it."""
        if cond:
            self.passed += 1
            print(f"{_PASS_PREFIX}{name}")
            return True
        self.failed += 1
        self.failures.append(name)
        suffix = f"  {detail}" if detail else ""
        print(f"{_FAIL_PREFIX}{name}{suffix}")
        return False

    def finish(self) -> bool:
        """Print the runner-parsed ``Results: N passed, M failed`` line and
        return ``True`` when nothing failed."""
        print(f"Results: {self.passed} passed, {self.failed} failed")
        return self.failed == 0


_DEFAULT = TestRun()


def check(name: str, cond: object, detail: str = "") -> bool:
    """Record one assertion against the module-level default :class:`TestRun`."""
    return _DEFAULT.check(name, cond, detail)


def finish() -> bool:
    """Print the summary for the module-level default :class:`TestRun`."""
    return _DEFAULT.finish()


def run_main(fn: Callable[[], bool]) -> NoReturn:
    """Run ``fn`` and exit ``0`` when it returns truthy, ``1`` otherwise —
    the standalone-test process contract in one call."""
    sys.exit(0 if fn() else 1)
