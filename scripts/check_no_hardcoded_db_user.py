#!/usr/bin/env python3
"""Source invariant: no hardcoded role in a DATABASE_URL *default*.

A connection default like::

    DB_URL = os.environ.get("DATABASE_URL", "postgres://alice@localhost/hd_test")

embeds ONE developer's OS username. It works on that machine and fails on
every other one — CI, a teammate's box, a fresh Ubuntu server — with a connect
error naming a role the server has never heard of, which is expensive to trace
back to a string literal buried in a test's fallback argument.

The correct default omits the user::

    DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hd_test")

``hyperdjango.database._ensure_url_user`` -> ``hyperdjango.conf.fill_url_auth``
is the SINGLE authority that fills user/password/host/port from the libpq
``PG*`` set and OS defaults, so the userless URL resolves correctly for every
user on every platform. An f-string default (``f"postgres://{user}@..."``) is
equally fine: the value comes from the environment, not from source.

Scope is deliberately narrow — connection DEFAULTS only, matched structurally
via AST:
  * ``os.environ.get("DATABASE_URL", <literal with a role>)`` / ``os.getenv``
  * ``DATABASE_URL = <literal with a role>`` (and ``*_DB_URL`` names)
A URL literal passed to a parser, asserted as an expected value, or aimed at a
deliberately-unreachable host is a FIXTURE, not a default, and is not flagged.
For the rare default that must name a role, annotate the line (or the one
above) with ``# db-url-fixture: <why>``.

Run: uv run python scripts/check_no_hardcoded_db_user.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from _slop_markers import has_marker

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("scripts", "tests", "hyperdjango", "services")

MARKER = "db-url-fixture"

# postgres://ROLE@ or postgres://ROLE:password@ — a literal role in the URL.
_URL_WITH_ROLE = re.compile(
    r"postgres(?:ql)?://(?P<role>[A-Za-z_][A-Za-z0-9_.-]*)(?::[^@\s]*)?@"
)
# Env keys whose fallback argument is a real connection default.
_DB_URL_KEYS = frozenset({"DATABASE_URL", "HYPER_DATABASE_URL", "PGURL"})
# Assignment targets that name a connection default.
_DB_URL_NAME = re.compile(r"(?i)(?:^|_)(?:DATABASE_URL|DB_URL)$")


def _role_in(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    m = _URL_WITH_ROLE.search(value)
    return m.group("role") if m else None


def _is_env_get(node: ast.Call) -> bool:
    """os.environ.get(...) or os.getenv(...)"""
    fn = node.func
    if not isinstance(fn, ast.Attribute):
        return False
    if fn.attr == "getenv" and isinstance(fn.value, ast.Name) and fn.value.id == "os":
        return True
    return (
        fn.attr == "get"
        and isinstance(fn.value, ast.Attribute)
        and fn.value.attr == "environ"
    )


def check_file(path: Path) -> list[str]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []
    if "postgres" not in source and "PGUSER" not in source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()

    def marked(lineno: int) -> bool:
        return has_marker(lines, lineno, lineno, MARKER)

    violations: list[str] = []

    def flag(lineno: int, role: str, what: str) -> None:
        if marked(lineno):
            return
        # A path outside the repo (a unit test's tmp file) has no relative
        # form — report it as given rather than raising.
        rel = path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path
        violations.append(
            f"{rel}:{lineno}: hardcoded role '{role}' in a {what} — drop the "
            f"role (postgres://host/db) so PGUSER/USER resolves it"
        )

    for node in ast.walk(tree):
        # os.environ.get("DATABASE_URL", "postgres://role@...")
        if isinstance(node, ast.Call) and _is_env_get(node) and len(node.args) >= 2:
            key = node.args[0]
            default = node.args[1]
            if (
                isinstance(key, ast.Constant)
                and key.value in _DB_URL_KEYS
                and isinstance(default, ast.Constant)
            ):
                role = _role_in(default.value)
                if role:
                    flag(default.lineno, role, f"{key.value} default")

            # os.environ.get("PGUSER", "somebody") — same class, different
            # spelling: a literal role fallback that only works on one
            # machine. The correct form is a userless URL passed through
            # conf.fill_url_auth (which owns the PG*/OS-user resolution).
            if (
                isinstance(key, ast.Constant)
                and key.value == "PGUSER"
                and isinstance(default, ast.Constant)
                and isinstance(default.value, str)
                and default.value
            ):
                flag(default.lineno, default.value, "PGUSER fallback")

        # DATABASE_URL = "postgres://role@..."  /  APP_DB_URL = ...
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            role = _role_in(node.value.value)
            if role:
                for target in node.targets:
                    if isinstance(target, ast.Name) and _DB_URL_NAME.search(target.id):
                        flag(node.value.lineno, role, f"{target.id} default")

    return violations


def main() -> int:
    violations: list[str] = []
    for d in _SCAN_DIRS:
        root = _ROOT / d
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            violations.extend(check_file(path))

    if violations:
        print(f"{len(violations)} hardcoded database role(s) in connection defaults:")
        for v in violations:
            print(f"  {v}")
        print(
            "\nUse the userless form — postgres://localhost:5432/<db> — and let the\n"
            "connection layer fill the role from PGUSER/USER, or annotate a genuine\n"
            f"exception with `# {MARKER}: <why>`."
        )
        return 1
    print("OK: no hardcoded database roles in connection defaults.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
