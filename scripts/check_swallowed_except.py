#!/usr/bin/env python
"""Enforcement gate: no bug-hiding exception swallowing in product source.

A broad exception handler that catches and *continues* (or returns a default)
turns a real failure into a silent 500 / dropped message / corrupted result.
This gate makes that class of bug fail CI.

A handler is FLAGGED when its caught type is BROAD:

    except:                     # bare
    except Exception:           # or `as e`
    except BaseException:
    except (A, Exception, B):   # a tuple containing Exception / BaseException

UNLESS one of the following makes the broad catch safe:

  (a) it RE-RAISES on every path — every control-flow path through the handler
      body ends by propagating an exception (`raise` / `raise X` / `raise X from
      e`). A handler that logs *then* re-raises is fine; the log is incidental.

  (b) it is annotated with a prover comment on the `except` line or the line
      immediately above:

          # blind-except: <specific reason swallowing is correct here>

      The reason must say WHY the swallow is correct (best-effort cleanup that
      must not mask the real error, telemetry that must never break the request,
      a __del__/shutdown path, an optional-feature probe, …). A bare marker with
      no reason does not count. "optional" alone is not a reason.

Note: logging-and-CONTINUING still needs a marker — logging is not handling.
`except A, B:` (PEP 758 grouped narrow catch) is NOT broad and is never flagged.

Scope: hyperdjango/ product source only (not scripts/tests/vendored).

Run: uv run python scripts/check_swallowed_except.py [paths...]
Default path: hyperdjango/
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

MARKER = re.compile(r"#\s*blind-except\s*:\s*\S")
BROAD_NAMES = {"Exception", "BaseException"}

# Statement types that break the recursive body walks at a new scope boundary:
# a `return` inside a nested function/lambda/class is not an exit of the handler.
_SCOPE_BOUNDARIES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
)


def _is_broad(node_type: ast.expr | None) -> bool:
    """True if this `except` clause catches Exception/BaseException (broadly)."""
    if node_type is None:  # bare `except:`
        return True
    if isinstance(node_type, ast.Name) and node_type.id in BROAD_NAMES:
        return True
    # builtins.Exception / __builtins__.BaseException etc.
    if isinstance(node_type, ast.Attribute) and node_type.attr in BROAD_NAMES:
        return True
    if isinstance(node_type, ast.Tuple):
        return any(_is_broad(elt) for elt in node_type.elts)
    return False


def _has_nonraising_exit(body: list[ast.stmt]) -> bool:
    """True if `body` can reach a `return`/`break`/`continue` (a non-raising
    exit of the handler), not counting exits inside nested scopes."""
    return any(_stmt_has_nonraising_exit(stmt) for stmt in body)


def _stmt_has_nonraising_exit(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Return, ast.Break, ast.Continue)):
        return True
    if isinstance(stmt, _SCOPE_BOUNDARIES):
        return False  # exits inside a nested scope are not our handler's exits
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt) and _stmt_has_nonraising_exit(child):
            return True
    return False


def _terminates_in_raise(body: list[ast.stmt]) -> bool:
    """True iff every control-flow path through `body` ends by raising.

    `return`/`break`/`continue` are non-raising exits. Only straightforward
    control flow is proven; anything not provably always-raising returns False
    (conservative — the caller then demands a `# blind-except:` marker)."""
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, (ast.Return, ast.Break, ast.Continue)):
            return False
        if isinstance(stmt, ast.If):
            if (
                stmt.orelse
                and _terminates_in_raise(stmt.body)
                and _terminates_in_raise(stmt.orelse)
            ):
                return True
            # A conditional branch that can exit without raising = a swallow path.
            if _has_nonraising_exit(stmt.body) or _has_nonraising_exit(stmt.orelse):
                return False
            # Both branches fall through without exiting → keep scanning.
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            if _terminates_in_raise(stmt.body):
                return True
            if _has_nonraising_exit(stmt.body):
                return False
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # Loop bodies may run zero times → cannot guarantee a raise. A
            # non-raising exit inside is a swallow path.
            if _has_nonraising_exit(stmt.body) or _has_nonraising_exit(stmt.orelse):
                return False
        elif isinstance(stmt, ast.Try):
            if (
                _has_nonraising_exit(stmt.body)
                or any(_has_nonraising_exit(h.body) for h in stmt.handlers)
                or _has_nonraising_exit(stmt.orelse)
                or _has_nonraising_exit(stmt.finalbody)
            ):
                return False
            if stmt.finalbody and _terminates_in_raise(stmt.finalbody):
                return True
    return False


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return [(lineno, source)] for unjustified broad handlers in `path`."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:  # pragma: no cover
        return [(e.lineno or 0, f"SYNTAX ERROR: {e.msg}")]

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_broad(node.type):
            continue
        # (a) re-raises on every path → safe.
        if _terminates_in_raise(node.body):
            continue
        # (b) justified with a prover comment on the except line, or anywhere in
        # the contiguous comment block immediately above it (so a multi-line
        # justification counts — the marker need not be the last line).
        lineno = node.lineno
        if lineno - 1 < len(lines) and MARKER.search(lines[lineno - 1]):
            continue
        j = lineno - 2  # 0-indexed line directly above the `except`
        justified = False
        while j >= 0 and lines[j].lstrip().startswith("#"):
            if MARKER.search(lines[j]):
                justified = True
                break
            j -= 1
        if justified:
            continue
        violations.append(
            (lineno, lines[lineno - 1].strip() if lineno - 1 < len(lines) else "")
        )
    return violations


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(p) for p in argv[1:]] or [pathlib.Path("hyperdjango")]
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
        elif root.suffix == ".py":
            files.append(root)

    total = 0
    for f in files:
        for lineno, text in check_file(f):
            print(f"{f}:{lineno} — {text}")
            total += 1

    if total:
        print(
            f"\nFAILED: {total} unjustified broad exception handler(s).\n"
            "Each must RE-RAISE on every path (narrow the catch or log + `raise`)\n"
            "OR be annotated with `# blind-except: <why swallowing is correct>`.",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: every broad exception handler in {len(files)} files re-raises or is justified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
