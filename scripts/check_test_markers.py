#!/usr/bin/env python
"""Enforcement gate + migrator: universal ``# hyper-test:`` classification markers.

Every ``scripts/test_*.py`` MUST carry exactly one canonical classification
marker so the test runner never falls back to content heuristics to decide a
file's scheduling lane. This script is the single authority for that contract:

Checker mode (default, no args): scan every ``scripts/test_*.py`` and print one
``path: problem`` line per violation, exit 1 if any. Rules:

  - exactly one ``# hyper-test: <kind>`` line, ``<kind>`` CANONICAL — aliases
    like ``pure`` are violations here (the gate is what retires them);
  - the marker line contains nothing after the kind (catches trailing junk such
    as ``# hyper-test: e2e hypernews``);
  - at most one each of the orthogonal markers ``# hyper-test-timeout: <int>``
    (int in [1, 3600]), ``# hyper-test-concurrency: low`` (value exactly
    ``low``), ``# hyper-test-flaky: <reason>`` (reason non-empty);
  - ``db_shared`` + ``concurrency: low`` is forbidden (the shared-DB lane is
    already serial, which is stricter than ``low``);
  - total flaky-marked files stays at or under ``FLAKY_QUARANTINE_CAP``.

``--fix`` mode: stamp every file MISSING a kind marker with its runner-computed
kind (``from hyperdjango.test_runner import classify_test``), rewrite alias
markers (``pure`` → ``unit``) to canonical spelling in place, and repair a
trailing-junk marker line to the bare canonical kind. Idempotent: a second run
changes nothing.

Run: uv run python scripts/check_test_markers.py [--fix]
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"

# Canonical kinds the gate accepts on a marker line. Mirrors the runner's
# ``_VALID_KINDS`` — aliases (e.g. ``pure``) are intentionally NOT here so the
# gate rejects them and ``--fix`` retires them.
CANONICAL_KINDS = frozenset({"unit", "db_isolated", "db_django", "db_shared", "e2e"})

# Bounds for a per-file timeout override, in seconds. Below 1 is meaningless;
# above an hour is a runaway that should be split, not granted more budget.
TIMEOUT_MIN_SECONDS = 1
TIMEOUT_MAX_SECONDS = 3600

# The one accepted concurrency value (mirrors the runner's marker contract).
CONCURRENCY_LOW = "low"

# Upper bound on how many files may carry a ``# hyper-test-flaky`` marker at
# once. The flaky quarantine is a finite, reviewed holding pen — not a
# graveyard: a hard cap forces flaky tests to be FIXED and de-quarantined
# rather than left masked by a silent retry forever.
FLAKY_QUARANTINE_CAP = 8

# The classification marker: ``# hyper-test:`` with the colon immediately after
# ``hyper-test`` (so it never matches the ``-timeout``/``-concurrency``/``-flaky``
# orthogonal markers, whose names continue with ``-`` before their colon).
_KIND_LINE_RE = re.compile(r"#\s*hyper-test:")
# A well-formed kind marker: prefix, one word kind, then only whitespace.
_KIND_VALID_RE = re.compile(r"#\s*hyper-test:\s*(\w+)\s*$")
# Kind token regardless of trailing junk (for reporting/repair).
_KIND_TOKEN_RE = re.compile(r"#\s*hyper-test:\s*(\w+)")
# The whole marker comment, prefix through end of line — used to rewrite a
# marker line in place, dropping any trailing junk after the kind.
_KIND_COMMENT_RE = re.compile(r"#\s*hyper-test:.*$")

_TIMEOUT_RE = re.compile(r"#\s*hyper-test-timeout:\s*(\S+)")
_CONCURRENCY_RE = re.compile(r"#\s*hyper-test-concurrency:\s*(\S+)")
_FLAKY_RE = re.compile(r"#\s*hyper-test-flaky:([^\n]*)")


def _rel(path: Path) -> str:
    return str(path.relative_to(_ROOT))


def check_file(path: Path) -> list[str]:
    """Return a list of ``path: problem`` violation strings for one file."""
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    rel = _rel(path)
    out: list[str] = []

    kind_lines = [ln for ln in lines if _KIND_LINE_RE.search(ln)]
    if not kind_lines:
        out.append(f"{rel}: missing '# hyper-test: <kind>' marker")
    elif len(kind_lines) > 1:
        out.append(
            f"{rel}: {len(kind_lines)} '# hyper-test:' markers — expected exactly one"
        )
    else:
        line = kind_lines[0]
        valid = _KIND_VALID_RE.search(line)
        token_m = _KIND_TOKEN_RE.search(line)
        if valid is None:
            if token_m is not None:
                out.append(
                    f"{rel}: marker line has content after the kind — "
                    f"expected bare '# hyper-test: {token_m.group(1)}'"
                )
            else:
                out.append(f"{rel}: malformed '# hyper-test:' marker line")
        else:
            kind = valid.group(1)
            if kind not in CANONICAL_KINDS:
                out.append(
                    f"{rel}: non-canonical kind '{kind}' — use one of "
                    f"{sorted(CANONICAL_KINDS)}"
                )

    # Orthogonal markers: at most one each, with valid values.
    timeout_lines = [ln for ln in lines if _TIMEOUT_RE.search(ln)]
    if len(timeout_lines) > 1:
        out.append(
            f"{rel}: {len(timeout_lines)} '# hyper-test-timeout:' markers — "
            f"expected at most one"
        )
    for ln in timeout_lines:
        raw = _TIMEOUT_RE.search(ln).group(1)
        if not raw.isdigit() or not (
            TIMEOUT_MIN_SECONDS <= int(raw) <= TIMEOUT_MAX_SECONDS
        ):
            out.append(
                f"{rel}: '# hyper-test-timeout: {raw}' — value must be an int in "
                f"[{TIMEOUT_MIN_SECONDS}, {TIMEOUT_MAX_SECONDS}]"
            )

    concurrency_lines = [ln for ln in lines if _CONCURRENCY_RE.search(ln)]
    if len(concurrency_lines) > 1:
        out.append(
            f"{rel}: {len(concurrency_lines)} '# hyper-test-concurrency:' markers — "
            f"expected at most one"
        )
    for ln in concurrency_lines:
        raw = _CONCURRENCY_RE.search(ln).group(1)
        if raw != CONCURRENCY_LOW:
            out.append(
                f"{rel}: '# hyper-test-concurrency: {raw}' — only "
                f"'{CONCURRENCY_LOW}' is supported"
            )

    flaky_lines = [ln for ln in lines if _FLAKY_RE.search(ln)]
    if len(flaky_lines) > 1:
        out.append(
            f"{rel}: {len(flaky_lines)} '# hyper-test-flaky:' markers — "
            f"expected at most one"
        )
    for ln in flaky_lines:
        if not _FLAKY_RE.search(ln).group(1).strip():
            out.append(
                f"{rel}: '# hyper-test-flaky:' marker requires a non-empty reason"
            )

    # db_shared + concurrency: low is contradictory (shared lane is serial).
    if concurrency_lines and kind_lines:
        km = _KIND_TOKEN_RE.search(kind_lines[0])
        if (
            km
            and km.group(1) == "db_shared"
            and any(
                _CONCURRENCY_RE.search(ln).group(1) == CONCURRENCY_LOW
                for ln in concurrency_lines
            )
        ):
            out.append(
                f"{rel}: '# hyper-test-concurrency: low' cannot combine with kind "
                f"'db_shared' — the shared-DB lane is already serial"
            )

    return out


def _docstring_end_line(text: str) -> int:
    """Return the 1-based last line of the module docstring, or 0 if none."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    body = tree.body
    if not body:
        return 0
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value.end_lineno or 0
    return 0


def _insert_marker(text: str, kind: str) -> str:
    """Insert a fresh ``# hyper-test: <kind>`` marker at the canonical spot.

    Immediately after the module docstring if present, else after a shebang
    line, else at line 1 — set off by a blank line on each side, matching the
    placement of already-marked files.
    """
    lines = text.splitlines()
    marker = f"# hyper-test: {kind}"

    doc_end = _docstring_end_line(text)
    if doc_end:
        insert_at = doc_end
    elif lines and lines[0].startswith("#!"):
        insert_at = 1
    else:
        insert_at = 0

    head = lines[:insert_at]
    tail = lines[insert_at:]
    while tail and tail[0].strip() == "":
        tail.pop(0)

    new_lines = head + ["", marker, ""] + tail
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(new_lines) + trailing_newline


def _rewrite_kind_line(line: str, kind: str) -> str:
    """Rewrite a marker line's kind to the bare canonical ``kind``.

    Preserves any leading text (indentation / in-docstring context) before the
    ``# hyper-test:`` prefix; drops the prefix and everything after the kind.
    """
    return _KIND_COMMENT_RE.sub(f"# hyper-test: {kind}", line)


def fix_file(path: Path) -> bool:
    """Bring one file into compliance. Return True if it was modified."""
    # Imported here, NOT at module top: classify_test transitively imports the
    # native extension, and check mode must run where no native build exists
    # (the CI lint job). Only --fix needs the runner's classification.
    from hyperdjango.test_runner import classify_test

    text = path.read_text(errors="replace")
    lines = text.splitlines(keepends=True)

    kind_indices = [i for i, ln in enumerate(lines) if _KIND_LINE_RE.search(ln)]

    if not kind_indices:
        meta = classify_test(path)
        new_text = _insert_marker(text, meta.kind)
        if new_text != text:
            path.write_text(new_text)
            return True
        return False

    if len(kind_indices) > 1:
        # Ambiguous — leave for a human; the checker still flags it.
        return False

    idx = kind_indices[0]
    line = lines[idx]
    newline = "\n" if line.endswith("\n") else ""
    body = line[: -len(newline)] if newline else line

    if _KIND_VALID_RE.search(body):
        token = _KIND_TOKEN_RE.search(body).group(1)
        canonical = classify_test(path).kind if token not in CANONICAL_KINDS else token
        if token in CANONICAL_KINDS:
            return False  # already canonical and clean
        fixed = _rewrite_kind_line(body, canonical)
    else:
        # Trailing junk (or otherwise malformed): repair to bare canonical
        # kind. Trust the runner's own classification for the canonical value.
        canonical = classify_test(path).kind
        fixed = _rewrite_kind_line(body, canonical)

    if fixed == body:
        return False
    lines[idx] = fixed + newline
    path.write_text("".join(lines))
    return True


def _all_test_files() -> list[Path]:
    """Git-TRACKED scripts/test_*.py.

    Untracked files are local scratch — a work-in-progress debugging script is
    not a suite member, and failing this gate on one punishes the developer for
    having it on disk. ``test_runner.discover_tests`` already draws the line
    here, and a gate that disagreed with the runner would demand markers on
    files the runner refuses to run. When git cannot answer (release tarball,
    no git binary) every file on disk is in scope, which is the safe default
    for a checkout that has no scratch by construction.
    """
    found = sorted(_SCRIPTS.glob("test_*.py"))
    try:
        listed = subprocess.run(
            ["git", "ls-files", "scripts/test_*.py"],
            capture_output=True,
            text=True,
            cwd=_SCRIPTS.parent,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return found
    if listed.returncode != 0:
        return found
    tracked = {Path(line).name for line in listed.stdout.splitlines() if line}
    return [p for p in found if p.name in tracked]


def main(argv: list[str]) -> int:
    do_fix = "--fix" in argv[1:]
    files = _all_test_files()

    if do_fix:
        changed = 0
        for f in files:
            if fix_file(f):
                changed += 1
        print(f"--fix: modified {changed} file(s) of {len(files)}.")
        # Fall through to a check so --fix leaves the tree in a reportable state.

    violations: list[str] = []
    flaky_count = 0
    for f in files:
        violations.extend(check_file(f))
        if any(
            _FLAKY_RE.search(ln) for ln in f.read_text(errors="replace").splitlines()
        ):
            flaky_count += 1

    if flaky_count > FLAKY_QUARANTINE_CAP:
        violations.append(
            f"scripts/: {flaky_count} files carry '# hyper-test-flaky:' — over the "
            f"FLAKY_QUARANTINE_CAP of {FLAKY_QUARANTINE_CAP}; fix and de-quarantine"
        )

    if violations:
        for v in violations:
            print(v)
        print(
            f"\nFAILED: {len(violations)} test-marker violation(s). Run "
            "`uv run python scripts/check_test_markers.py --fix` to stamp/repair "
            "missing and alias markers.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(files)} test files carry canonical '# hyper-test:' markers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
