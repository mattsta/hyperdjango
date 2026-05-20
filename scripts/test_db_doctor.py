#!/usr/bin/env python3
"""`hyper db doctor` — layered PostgreSQL diagnosis with remediation.

Proves the two halves independently:
  * the pure connect-error classifier maps every known failure signature
    (TCP-auth rejection, missing role, missing database, refused, timeout)
    to its cause + actionable remediation — no misconfigured server needed;
  * the live probe chain runs green against the suite-provided database and
    fails with the right verdict + suggestion for a missing target database
    and an unreachable server.

Usage:
    uv run hyper-test db_doctor
    DATABASE_URL=... uv run python scripts/test_db_doctor.py
"""

# hyper-test: db_isolated

import contextlib
import io
import os
import urllib.parse

from hyperdjango.db_doctor import _mask, classify_connect_error, main
from hyperdjango.testkit import check, finish, run_main

DB_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/hyperdjango_test",
)


def _run_doctor(url: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(url)
    return code, out.getvalue()


def test_classifier() -> None:
    url = "postgresql://alice@localhost:5432/hyperdjango_test"

    cause, fix = classify_connect_error("password authentication failed", url)
    check("auth failure classified", "authentication" in cause.lower(), cause)
    check("auth fix names pg_hba peer/scram trap", "pg_hba" in fix, fix)
    check("auth fix names the role", "ALTER ROLE alice" in fix, fix)

    cause, fix = classify_connect_error('FATAL: role "alice" does not exist', url)
    check("missing role classified", "does not exist" in cause, cause)
    check("missing role fix uses createuser", "createuser" in fix, fix)

    cause, fix = classify_connect_error(
        'FATAL: database "hyperdjango_test" does not exist', url
    )
    check("missing db classified", "hyperdjango_test" in cause, cause)
    check("missing db fix is createdb", fix == "createdb hyperdjango_test", fix)

    cause, fix = classify_connect_error("connection refused", url)
    check("refused classified as server down", "no server" in cause, cause)

    cause, fix = classify_connect_error("connect timed out", url)
    check("timeout classified", "timed out" in cause, cause)

    cause, fix = classify_connect_error("some novel failure text", url)
    check("unknown errors surface verbatim", "novel failure" in fix, fix)


def test_mask() -> None:
    # Synthetic URLs, NOT the ambient DB_URL: whether the suite-provided
    # DATABASE_URL carries a password is an environment property (CI uses
    # postgres:postgres@..., a local dev box usually has none), so asserting
    # on its shape would pass on one machine and fail on the other.
    # db-url-fixture: synthetic inputs for the masker, never dialed.
    masked = _mask("postgresql://u:secretpw@h:5432/d")
    check("password masked", "secretpw" not in masked and ":***@" in masked, masked)
    # db-url-fixture: synthetic passwordless URL, never dialed.
    passwordless = "postgres://localhost:5432/somedb"
    check(
        "no-password URL unchanged",
        _mask(passwordless) == passwordless,
        _mask(passwordless),
    )


def test_probe_happy_path() -> None:
    code, out = _run_doctor(DB_URL)
    check("doctor exits 0 against the suite database", code == 0, out)
    for expected in (
        "TCP reachability",
        "Authentication (TCP)",
        "Target database",
        "CREATEDB privilege",
        "Create/drop round-trip",
    ):
        check(f"probe ran: {expected}", f"✓ {expected}" in out, out)


def test_probe_missing_database() -> None:
    parts = urllib.parse.urlsplit(DB_URL)
    missing = urllib.parse.urlunsplit(parts._replace(path="/hd_doctor_missing_db"))
    code, out = _run_doctor(missing)
    check("missing target db fails doctor", code == 1, out)
    check(
        "missing target db suggests createdb",
        "createdb hd_doctor_missing_db" in out,
        out,
    )
    check(
        "auth still reported OK (missing db does not mask auth)",
        "✓ Authentication (TCP)" in out,
        out,
    )


def test_probe_unreachable_server() -> None:
    code, out = _run_doctor("postgresql://nobody@localhost:59999/x")
    check("unreachable server fails doctor", code == 1, out)
    check("unreachable verdict is TCP-level", "✗ TCP reachability" in out, out)
    check("start hint present", ("systemctl" in out) or ("brew services" in out), out)


def main_tests() -> bool:
    print("=" * 64)
    print("hyper db doctor — classifier + live probe chain")
    print("=" * 64)
    test_classifier()
    test_mask()
    test_probe_happy_path()
    test_probe_missing_database()
    test_probe_unreachable_server()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main_tests)
