"""HyperDjango test toolkit — the shared surface for the standalone test suite.

This package is framework surface (like ``django.test``). It gives every
``scripts/test_*.py`` program one import path for three things: the assertion
harness (:class:`TestRun`, :func:`check`, :func:`finish`, :func:`run_main`), the
determinism helpers (:func:`wait_until`, :func:`await_until`, :func:`tamper`),
and the end-to-end HTTP/WebSocket primitives (:class:`AppRunner`,
:class:`Session`, :class:`E2EResponse`, ``http_*``, :func:`sse_post`,
:func:`build_multipart`).

Test-environment contract (AUTHORITATIVE)
-----------------------------------------
The test runner OWNS each subprocess's environment. The ambient shell / CI
environment is NOT passed through, except a documented allowlist for reaching
Postgres: ``PGHOST``, ``PGUSER``, ``PGPORT``, ``PGPASSWORD``. Everything a test
observes about configuration is therefore what the runner set — never what the
invoking shell happened to export.

Database locators are assigned per resource kind, never inherited:

- ``unit`` and ``db_shared`` tests get DB locators SCRUBBED
  (``DATABASE_URL``, ``HYPER_DATABASE_URL``, ``PGDATABASE`` removed), so they
  see the same clean environment as a fresh developer machine. A ``unit`` test
  that unexpectedly reaches the database takes the deterministic "no database
  configured" path instead of silently connecting to a foreign CI database;
  ``db_shared`` falls back to its hardcoded ``hyperdjango_test``.
- ``db_isolated`` and ``e2e`` tests receive an EXPLICIT per-run isolated
  ``DATABASE_URL`` (and matching ``HYPER_DATABASE_URL`` / ``PGDATABASE``) so
  concurrent files never share schema or rows.

Deployment-tuning variables (``HYPER_POOL_SIZE``, ``HYPER_THREAD_POOL_SIZE``)
are NEVER injected, so a test can assert the framework's built-in defaults.

Marker taxonomy
---------------
Every ``scripts/test_*.py`` declares exactly ONE resource kind, on its own line:

    # hyper-test: <kind>

where ``<kind>`` is one of — a RESOURCE contract only, not a scheduling hint:

- ``unit`` — no database, no native server.
- ``db_isolated`` — needs a private, per-run database.
- ``db_django`` — needs the Django-integration database.
- ``db_shared`` — uses the shared ``hyperdjango_test`` database.
- ``e2e`` — starts a live app server (``AppRunner``).

Scheduling and reliability concerns ride on orthogonal markers, one per line:

- ``# hyper-test-timeout: <seconds>`` — per-file override of the global budget,
  for a genuinely heavy file rather than raising the budget for everyone.
- ``# hyper-test-concurrency: low`` — schedule with reduced parallelism, for a
  starvation-sensitive file.
- ``# hyper-test-flaky: <reason>`` — quarantine with a mandatory reason; the
  file still runs, retries once, and is counted in a visible flaky tally.
"""

from __future__ import annotations

from .determinism import await_until, tamper, wait_until
from .e2e import (
    AppRunner,
    E2EResponse,
    Session,
    build_multipart,
    connect_with_retry,
    http_delete,
    http_get,
    http_post,
    http_put,
    sse_post,
)
from .harness import TestRun, check, finish, run_main
from .property import CI_PROFILE, DEV_PROFILE, run_property

__all__ = [
    "CI_PROFILE",
    "DEV_PROFILE",
    "AppRunner",
    "E2EResponse",
    "Session",
    "TestRun",
    "await_until",
    "build_multipart",
    "check",
    "connect_with_retry",
    "finish",
    "http_delete",
    "http_get",
    "http_post",
    "http_put",
    "run_main",
    "run_property",
    "sse_post",
    "tamper",
    "wait_until",
]
