#!/usr/bin/env python
"""Enforcement gate: every 4xx/5xx error body must use the unified contract.

The framework's ONE error-response shape is ``{"detail": ..., "status": ...}``
(plus an optional ``"errors"``), produced by ``exception_to_response`` /
``HTTPException``. A hand-built error body that drifts from it (a ``{"error":...}``
key, a missing ``status`` field, a bespoke structure) breaks clients that parse
errors uniformly and is exactly the drift this codebase kept re-introducing.

This checker FAILS (exit 1) if a ``Response.json(...)`` / ``JsonResponse(...)``
call has a 4xx/5xx **literal** status and a **dict-literal** body whose keys are
not a subset of {detail, status, errors} that also includes detail+status.

Dynamic bodies (a variable), non-literal statuses, and calls through
``exception_to_response`` are not flagged (they can't be, or are the sanctioned
path). A deliberately-different contract (RFC 9457 problem+json, a bulk
partial-success body) is allowed with a prover comment on the call:

    # error-contract: <why this shape is intentional>

Run: uv run python scripts/check_error_contract.py [paths...]
Default path: hyperdjango/
"""

from __future__ import annotations

import ast
import pathlib
import sys

from _slop_markers import has_marker

# The pattern itself lives in _slop_markers.marker_pattern; this is the name.
MARKER_NAME = "error-contract"
_ALLOWED_KEYS = {"detail", "status", "errors"}


def _status_of(call: ast.Call) -> int | None:
    """The literal status of a Response.json/JsonResponse call, or None."""
    # keyword: status=NNN
    for kw in call.keywords:
        if (
            kw.arg == "status"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, int)
        ):
            return kw.value.value
    # JsonResponse(body, NNN) — 2nd positional
    if (
        len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, int)
    ):
        return call.args[1].value
    return None


def _is_error_response_call(call: ast.Call) -> bool:
    f = call.func
    # Response.json(...)
    if (
        isinstance(f, ast.Attribute)
        and f.attr == "json"
        and isinstance(f.value, ast.Name)
        and f.value.id == "Response"
    ):
        return True
    # JsonResponse(...)
    return bool(isinstance(f, ast.Name) and f.id == "JsonResponse")


def _body_keys(call: ast.Call) -> set[str] | None:
    """Return the string keys of the body dict literal (1st positional), or None
    if the body is not a plain dict literal we can inspect."""
    if not call.args:
        return None
    body = call.args[0]
    if not isinstance(body, ast.Dict):
        return None
    keys: set[str] = set()
    for k in body.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
        else:
            return None  # a non-string / computed key — don't classify
    return keys


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:  # pragma: no cover
        return [(e.lineno or 0, f"SYNTAX ERROR: {e.msg}")]

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_error_response_call(node)):
            continue
        status = _status_of(node)
        if status is None or not (400 <= status <= 599):
            continue
        keys = _body_keys(node)
        if keys is None:
            continue  # dynamic body — cannot classify, not flagged
        # Unified: keys ⊆ {detail,status,errors} AND contains detail+status.
        if keys <= _ALLOWED_KEYS and "detail" in keys and "status" in keys:
            continue
        start = node.lineno
        end = (
            getattr(node, "end_lineno", start) or start
        )  # error-contract: end_lineno may be None on edge AST nodes
        if has_marker(lines, start, end, MARKER_NAME):
            continue
        violations.append((start, f"{status} body keys {sorted(keys)}"))
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
            print(
                f"{f}:{lineno}: bespoke error body — {text} (want {{'detail','status'}})"
            )
            total += 1

    if total:
        print(
            f"\nFAILED: {total} bespoke 4xx/5xx error body/ies.\n"
            "Build the body via exception_to_response(HTTPException(status, detail, ...))\n"
            'or emit {"detail", "status"} (+optional "errors"), OR annotate a\n'
            "deliberately-different contract with `# error-contract: <why>`.",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: every 4xx/5xx error body in {len(files)} files uses the unified contract."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
