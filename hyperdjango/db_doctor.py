"""PostgreSQL environment diagnosis — layered probes with exact remediation.

`hyper db doctor [--database URL]` walks every link the framework and the
test runner depend on, IN DEPENDENCY ORDER, and prints the precise
per-platform fix for the first broken one instead of the downstream symptom
("failed to connect to database: postgres://...") a broken link otherwise
produces minutes later inside a test run:

    1. URL resolution        — what will actually be dialed, and from where
    2. TCP reachability      — is a server listening there at all
    3. Authentication        — can this role log in over TCP (the classic
                               Ubuntu trap: pg_hba defaults to `peer` on the
                               unix socket but `scram-sha-256` on TCP, so a
                               passwordless role works in psql yet fails the
                               driver's localhost connection)
    4. Target database       — does it exist
    5. Role privileges       — CREATEDB (the runner creates one isolated
                               database PER TEST), superuser
    6. Create/drop probe     — perform the runner's actual operation once
    7. Extension binaries    — everything in the db_extensions registry
    8. Capacity              — max_connections vs the full parallel suite

Probes use the SAME native driver the framework serves with, so what doctor
proves is exactly what the runtime will experience.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import socket
import urllib.parse
from dataclasses import dataclass

from hyperdjango.database import Database
from hyperdjango.db_extensions import REGISTRY

CONNECT_PROBE_TIMEOUT_S = 3.0
DEFAULT_PG_PORT = 5432
# The runner's per-test isolated databases plus e2e app databases run
# concurrently; the stock server default (100) throttles or fails the FULL
# parallel suite, while small subsets run fine on it.
FULL_SUITE_MIN_CONNECTIONS = 1000
RECOMMENDED_MAX_CONNECTIONS = 10000
_PROBE_DB = f"hd_doctor_probe_{os.getpid()}"


@dataclass(slots=True)
class Verdict:
    """One diagnosis step: what was checked, what happened, how to fix it."""

    name: str
    ok: bool
    detail: str = ""
    remediation: str = ""
    fatal: bool = True  # False → warning: reported but doesn't fail doctor


def _mask(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts)


def _default_dev_url() -> str:
    # env-boundary: dev-default URL mirrors the libpq convention the suite uses when nothing is configured — not framework runtime config.
    user = os.environ.get("PGUSER") or os.environ.get("USER", "postgres")
    return f"postgresql://{user}@localhost:{DEFAULT_PG_PORT}/hyperdjango_test"


def _server_start_hint() -> str:
    if platform.system() == "Darwin":
        return "brew services start postgresql@17   (then re-run doctor)"
    return "sudo systemctl start postgresql   # check clusters with: pg_lsclusters"


def classify_connect_error(text: str, url: str) -> tuple[str, str]:
    """Map a driver connect-failure message to (cause, remediation).

    Pure string classification so every branch is unit-testable without a
    misconfigured server on hand.
    """
    low = text.lower()
    parts = urllib.parse.urlsplit(url)
    # env-boundary: fallback role name for remediation text only.
    user = parts.username or os.environ.get("USER", "postgres")
    dbname = (parts.path or "/").lstrip("/") or "postgres"

    if (
        "password authentication failed" in low
        or "no password supplied" in low
        or "scram" in low
        or "authenticationfailed" in low
    ):
        return (
            "TCP authentication rejected this role",
            "Ubuntu's default pg_hba.conf uses `peer` auth on the unix socket\n"
            "but `scram-sha-256` on TCP — a passwordless role works in psql\n"
            "yet fails the driver's localhost connection. Either:\n"
            f"  a) give the role a password and export it:\n"
            f"       sudo -u postgres psql -c \"ALTER ROLE {user} PASSWORD 'devpass'\"\n"
            f"       export PGPASSWORD=devpass\n"
            "  b) or trust local TCP for development:\n"
            "       add `host all all 127.0.0.1/32 trust` ABOVE the scram lines\n"
            "       in pg_hba.conf, then: sudo systemctl reload postgresql",
        )
    if "role" in low and "does not exist" in low:
        return (
            f'role "{user}" does not exist on the server',
            f"sudo -u postgres createuser --superuser {user}\n"
            "(macOS/brew clusters are initialized with your OS user already)",
        )
    if "database" in low and "does not exist" in low:
        return (
            f'database "{dbname}" does not exist',
            f"createdb {dbname}",
        )
    if "connection refused" in low or "could not connect" in low:
        return (
            "no server accepting connections at the resolved address",
            _server_start_hint(),
        )
    if "timeout" in low or "timed out" in low:
        return (
            "connection attempt timed out",
            "check listen_addresses in postgresql.conf and any firewall "
            "between you and the server",
        )
    return ("connection failed", f"driver reported: {text}")


def _maintenance_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path="/postgres"))


async def _query_one(db: Database, sql: str, *args):
    rows = await db.query(sql, *args)
    return rows[0] if rows else None


async def _probe(url: str) -> list[Verdict]:
    verdicts: list[Verdict] = []
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or "localhost"
    port = parts.port or DEFAULT_PG_PORT
    dbname = (parts.path or "/").lstrip("/")

    # 2. TCP reachability — cheapest possible probe, clearest failure.
    try:
        with socket.create_connection((host, port), timeout=CONNECT_PROBE_TIMEOUT_S):
            pass
        verdicts.append(Verdict("TCP reachability", True, f"{host}:{port} accepting"))
    except OSError as exc:
        verdicts.append(
            Verdict(
                "TCP reachability",
                False,
                f"cannot reach {host}:{port} ({exc})",
                f"PostgreSQL isn't listening there.\n{_server_start_hint()}",
            )
        )
        return verdicts  # nothing below can work

    # 3. Authentication, against the maintenance DB so a missing TARGET
    # database doesn't mask an auth problem (or vice versa).
    admin = Database(_maintenance_url(url), max_size=1)
    try:
        await admin.connect()
    # blind-except: every connect failure is the diagnosis itself — classified into cause+remediation and reported as the fatal verdict.
    except Exception as exc:  # noqa: BLE001
        cause, fix = classify_connect_error(str(exc), url)
        verdicts.append(Verdict("Authentication (TCP)", False, cause, fix))
        return verdicts
    try:
        who = await _query_one(
            admin,
            "SELECT current_user AS role, rolcreatedb, rolsuper "
            "FROM pg_roles WHERE rolname = current_user",
        )
        role = who["role"]
        verdicts.append(Verdict("Authentication (TCP)", True, f"as {role}"))

        # 4. Target database exists.
        target = await _query_one(
            admin, "SELECT 1 FROM pg_database WHERE datname = $1", dbname
        )
        if target:
            verdicts.append(Verdict("Target database", True, dbname))
        else:
            verdicts.append(
                Verdict(
                    "Target database",
                    False,
                    f'"{dbname}" does not exist',
                    f"createdb {dbname}",
                )
            )

        # 5. Privileges the test runner needs.
        can_createdb = bool(who["rolcreatedb"]) or bool(who["rolsuper"])
        verdicts.append(
            Verdict(
                "CREATEDB privilege",
                can_createdb,
                "the runner creates one isolated database per DB test",
                f'sudo -u postgres psql -c "ALTER ROLE {role} CREATEDB"',
            )
        )

        # 6. The runner's actual operation, once.
        if can_createdb:
            try:
                await admin.execute(f'CREATE DATABASE "{_PROBE_DB}"')
                await admin.execute(f'DROP DATABASE "{_PROBE_DB}"')
                verdicts.append(Verdict("Create/drop round-trip", True, _PROBE_DB))
            # blind-except: any create/drop failure IS the finding — reported verbatim as a fatal verdict, never swallowed.
            except Exception as exc:  # noqa: BLE001
                verdicts.append(
                    Verdict(
                        "Create/drop round-trip",
                        False,
                        str(exc),
                        "the role can nominally CREATEDB but the operation "
                        "failed — read the server log",
                    )
                )

        # 7. Extension binaries (availability; enabling is `extensions ensure`).
        ver = await _query_one(admin, "SHOW server_version")
        major = ver["server_version"].split(".")[0] if ver else "?"
        available = {
            r["name"]
            for r in await admin.query("SELECT name FROM pg_available_extensions")
        }
        for ext in REGISTRY:
            if ext.name in available:
                verdicts.append(Verdict(f"Extension {ext.name}", True, ext.purpose))
            else:
                if ext.apt_package:
                    # The registry pins one server major; rewrite the version
                    # digits to match the server actually running here.
                    apt_pkg = re.sub(r"\d+", major, ext.apt_package, count=1)
                    hint = (
                        f"Ubuntu: sudo apt install {apt_pkg}\n"
                        "macOS: brew install pgvector (against your postgres "
                        "formula)"
                    )
                else:
                    hint = "ships with postgres-contrib; install it"
                verdicts.append(
                    Verdict(
                        f"Extension {ext.name}",
                        False,
                        f"binary not available on the server "
                        f"(needed by: {', '.join(ext.required_by) or 'framework'})",
                        hint + "\nthen: uv run hyper db extensions ensure",
                        fatal=False,
                    )
                )

        # 8. Capacity for the full parallel suite.
        mc = await _query_one(admin, "SHOW max_connections")
        max_conn = int(mc["max_connections"])
        if max_conn >= FULL_SUITE_MIN_CONNECTIONS:
            verdicts.append(Verdict("max_connections", True, str(max_conn)))
        else:
            verdicts.append(
                Verdict(
                    "max_connections",
                    False,
                    f"{max_conn} — fine for test subsets, throttles the FULL "
                    "parallel suite",
                    f"set max_connections = {RECOMMENDED_MAX_CONNECTIONS} in "
                    "postgresql.conf and restart PostgreSQL",
                    fatal=False,
                )
            )
    finally:
        await admin.disconnect()
    return verdicts


def main(database_url: str | None = None) -> int:
    """Run the diagnosis; return 0 when every FATAL check passes."""
    from hyperdjango.conf import resolve_database_url

    resolved_from = "--database flag"
    url = database_url
    if not url:
        url = resolve_database_url()
        resolved_from = "settings resolution (HYPER_DATABASE_URL / DATABASE_URL / PG*)"
    if not url:
        url = _default_dev_url()
        resolved_from = "dev default (nothing configured)"

    print("PostgreSQL environment diagnosis")
    print("=" * 64)
    print(f"  URL: {_mask(url)}")
    print(f"  via: {resolved_from}")
    print()

    verdicts = asyncio.run(_probe(url))

    hard_failures = 0
    for v in verdicts:
        if v.ok:
            print(f"  ✓ {v.name}: {v.detail}")
            continue
        mark = "✗" if v.fatal else "!"
        if v.fatal:
            hard_failures += 1
        print(f"  {mark} {v.name}: {v.detail}")
        for line in v.remediation.splitlines():
            print(f"      {line}")

    print()
    if hard_failures:
        print(f"{hard_failures} blocking problem(s) — fix the FIRST one, re-run.")
        return 1
    print("Database environment OK (warnings, if any, affect only the full suite).")
    return 0
