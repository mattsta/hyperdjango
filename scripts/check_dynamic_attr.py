#!/usr/bin/env python
"""Enforcement gate: every getattr/setattr must be PROVEN necessary.

getattr/setattr on a well-defined object is an anti-pattern — it defeats mypy
and ruff, hides bugs (getattr on a dict silently returns the default), and is
unreadable. They are permitted ONLY for genuine dynamic access where the object
type or attribute name is not statically knowable (reflecting over arbitrary
user models, plugin objects, cross-backend capability probes, etc.).

This checker FAILS (exit 1) if any getattr()/setattr()/*.__getattr__()/
*.__setattr__() call site in the product source is not paired with a matching
prover comment:

    # dynamic-attr: <reason it is genuinely required>

The marker must appear on the call's own line(s) or the line immediately above.
A bare marker with no reason does not count. Definitions of __getattr__/
__setattr__ and calls to hasattr/delattr are out of scope.

Run: uv run python scripts/check_dynamic_attr.py [paths...]
Default path: hyperdjango/
"""

from __future__ import annotations

import ast
import pathlib
import sys

from _slop_markers import has_marker

# The pattern itself lives in _slop_markers.marker_pattern; this is the name.
MARKER_NAME = "dynamic-attr"
_BUILTINS = {"getattr", "setattr"}
_DUNDERS = {"__getattr__", "__setattr__"}


def _is_dynamic_attr_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _BUILTINS:
        return True
    # object.__setattr__(self, ...) / x.__getattr__(...) — the setattr/getattr
    # family used to bypass frozen dataclasses etc.
    return bool(isinstance(func, ast.Attribute) and func.attr in _DUNDERS)


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return [(lineno, source)] for unjustified call sites in `path`."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:  # pragma: no cover
        return [(e.lineno or 0, f"SYNTAX ERROR: {e.msg}")]

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_dynamic_attr_call(node)):
            continue
        start = node.lineno
        end = (
            getattr(node, "end_lineno", start) or start
        )  # dynamic-attr: ast.Call.end_lineno is optional in the stdlib AST API (may be None on older/edge nodes)
        # Justified if the marker is attached to the call — on any line it
        # spans, or in the comment block directly above it.
        if has_marker(lines, start, end, MARKER_NAME):
            continue
        violations.append(
            (start, lines[start - 1].strip() if start - 1 < len(lines) else "")
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
            print(f"{f}:{lineno}: unjustified getattr/setattr — {text}")
            total += 1

    if total:
        print(
            f"\nFAILED: {total} unjustified getattr/setattr call site(s).\n"
            "Each must be removed (use direct attribute/dict access on the known\n"
            "type) OR annotated with `# dynamic-attr: <why it is genuinely "
            "required>`.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: every getattr/setattr in {len(files)} files is justified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
