"""Hypothesis policy for the standalone ``scripts/test_*.py`` property programs.

This module owns EVERY Hypothesis configuration decision so the individual test
files never tune deadlines, seeds, or the example database themselves — they
import :func:`run_property`, wrap each ``@given`` property with it, and the
property then counts in the file's ``Results: N passed, M failed`` line exactly
like a plain :func:`check`.

Two settings profiles are registered at import and one is selected from the
environment:

- ``hyper-ci`` — derandomized (reproducible on shared, noisy CI runners) and
  deadline-free (CPU contention must never fail an example on timing alone).
- ``hyper-dev`` — randomized, so local runs keep exploring fresh inputs, and
  backed by the on-disk example database so a shrunk failing example becomes an
  observable artifact under ``logs/hypothesis_examples/`` and is replayed first
  on the next run.

The example database lives only on the randomized profile: Hypothesis enforces
``derandomize=True`` ⟹ ``database=None`` (a seed-reproducible run neither reads
nor writes the database), so persistence rides on the profile that actually
discovers new counterexamples. The directory is created either way.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable
from pathlib import Path

from hypothesis import settings
from hypothesis.database import DirectoryBasedExampleDatabase

from .harness import check

CI_PROFILE = "hyper-ci"
DEV_PROFILE = "hyper-dev"

# Shrunk failing examples persist here as observable artifacts across runs.
_EXAMPLE_DB_DIR = Path("logs/hypothesis_examples")


def _active_profile() -> str:
    # CI presence is an execution-environment fact, not a framework setting — it
    # only selects derandomized (CI) vs randomized (dev) example generation.
    # env-boundary: run-context detection, not a get_setting configuration value.
    return CI_PROFILE if "CI" in os.environ else DEV_PROFILE


def _register_profiles() -> None:
    _EXAMPLE_DB_DIR.mkdir(parents=True, exist_ok=True)
    database = DirectoryBasedExampleDatabase(str(_EXAMPLE_DB_DIR))
    settings.register_profile(CI_PROFILE, settings(derandomize=True, deadline=None))
    settings.register_profile(
        DEV_PROFILE, settings(derandomize=False, database=database)
    )
    settings.load_profile(_active_profile())


_register_profiles()


def _describe_failure(exc: BaseException) -> str:
    """Flatten a Hypothesis falsifying exception — including its ``add_note``
    ``Falsifying example: ...`` line — into a single-line ``check`` detail."""
    text = "".join(traceback.format_exception_only(exc)).strip()
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


def run_property(fn: Callable[[], object]) -> bool:
    """Execute a zero-arg ``@given`` property and record it as one ``check``.

    A passing property is a PASS; a Hypothesis failure is a FAIL whose detail
    carries the SHRUNK counterexample, so every property is tallied in the file's
    ``Results:`` line. Returns the pass/fail boolean, like :func:`check`.
    """
    try:
        fn()
    except BaseException as exc:  # blind-except: any property failure — assertion or a raise from the code under test — is a genuine finding surfaced as a FAIL
        return check(fn.__name__, False, _describe_failure(exc))
    return check(fn.__name__, True)
