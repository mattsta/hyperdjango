#!/usr/bin/env python
"""Enforcement gate: the settings system is the SINGLE configuration authority.

Every framework module must resolve configuration through ``get_setting(...)``
(and ``resolve_database_url()`` for the DB URL), never by reading the process
environment ad-hoc. A stray ``os.environ.get("HYPER_...")`` / ``os.getenv(...)``
re-introduces a second, drifting config path — the exact class of bug this gate
prevents: a value set in Django settings / DEFAULTS silently ignored because one
module still peeks at the raw env.

This checker FAILS (exit 1) on any ``os.environ`` / ``os.getenv`` **read** in the
product source, EXCEPT:

  - the sanctioned boundary MODULES (:data:`_ALLOWLIST`) — the two config
    authorities (``conf.py``, ``site_config.py``), the logging bootstrap (runs
    before the settings system exists), the external-service client adapter, and
    the test runner (test tooling that configures ITS OWN behavior);
  - a read carrying a prover comment on its own line or the line above:

        # env-boundary: <why this env read is genuinely not a framework setting>

Writes are NOT flagged: ``os.environ["X"] = ...`` is the sanctioned
settings→native-env bridge (``app._export_native_config``), which hands resolved
settings DOWN to the Zig server — the opposite direction from a config read.

Run: uv run python scripts/check_no_os_environ.py [paths...]
Default path: hyperdjango/
"""

from __future__ import annotations

import ast
import pathlib
import sys

from _slop_markers import has_marker

MARKER = "env-boundary"

# Whole-file sanctioned environment boundaries (paths relative to the package
# root, matched by their tail so the check is location-independent).
_ALLOWLIST = frozenset(
    {
        "conf.py",  # config authority: turns HYPER_*/PG*/DATABASE_URL into settings
        "site_config.py",  # per-app SiteConfig authority ({PREFIX}_* + TOML)
        "logging/__init__.py",  # bootstraps before the settings system exists
        "serviceclient.py",  # external-service adapter ({PREFIX}_URL etc.)
        "test_runner.py",  # test tooling: configures its own runs + subprocess env
    }
)


def _has_marker(lines: list[str], start: int, end: int) -> bool:
    """True if an ``env-boundary`` prover comment is attached to the read —
    on its own lines or in the comment block directly above it."""
    return has_marker(lines, start, end, MARKER)


def _write_target_environ_ids(tree: ast.AST) -> set[int]:
    """ids of ``os.environ`` Attribute nodes that are the *target* of a subscript
    assignment (``os.environ["X"] = ...``) — a WRITE, not a config read."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            val = node.value
            if _is_os_attr(val, "environ"):
                ids.add(id(val))
    return ids


def _is_os_attr(node: ast.AST, attr: str) -> bool:
    """True if ``node`` is the attribute access ``os.<attr>``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return [(lineno, source)] for unjustified env reads in ``path``."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:  # pragma: no cover
        return [(e.lineno or 0, f"SYNTAX ERROR: {e.msg}")]

    write_ids = _write_target_environ_ids(tree)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        # os.getenv(...) is always a read; os.environ is a read unless it is the
        # target of a subscript-assignment (the native-bridge write).
        is_read = _is_os_attr(node, "getenv") or (
            _is_os_attr(node, "environ") and id(node) not in write_ids
        )
        if not is_read:
            continue
        start = node.lineno
        end = node.end_lineno or start
        if _has_marker(lines, start, end):
            continue
        violations.append(
            (start, lines[start - 1].strip() if start - 1 < len(lines) else "")
        )
    return violations


def _is_allowlisted(path: pathlib.Path) -> bool:
    posix = path.as_posix()
    return any(posix.endswith(entry) for entry in _ALLOWLIST)


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(p) for p in argv[1:]] or [pathlib.Path("hyperdjango")]
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
        elif root.suffix == ".py":
            files.append(root)

    total = 0
    scanned = 0
    for f in files:
        if _is_allowlisted(f):
            continue
        scanned += 1
        for lineno, text in check_file(f):
            print(f"{f}:{lineno}: ad-hoc env read — {text}")
            total += 1

    if total:
        print(
            f"\nFAILED: {total} ad-hoc os.environ/os.getenv read(s).\n"
            "Read configuration through get_setting('<NAME>') (register a\n"
            "SettingDefinition in conf.py if new) / resolve_database_url(), OR — for\n"
            "a genuine non-setting env read (an OS concept, subprocess-env\n"
            "propagation) — annotate it `# env-boundary: <why>`.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: no ad-hoc env reads in {scanned} scanned files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
