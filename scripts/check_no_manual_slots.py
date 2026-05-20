#!/usr/bin/env python
"""Enforcement gate: no manual ``__slots__`` — use ``@dataclass(slots=True)``.

The codebase's convention for a slotted type is a dataclass:
``@dataclass(slots=True)``. A hand-written ``__slots__ = (...)`` is the legacy
form — it drifts from the field list, hides attributes from tooling, and pairs
with a hand-written ``__init__`` the dataclass would generate for free. This
gate keeps that slop out.

FAILS (exit 1) on any ``__slots__`` assignment in a class body under the product
source, EXCEPT one carrying a prover comment on its own line or the line above:

    # slots-required: <why a dataclass can't express this — e.g. a C-extension
    #                  base type like threading.local that manages storage>

Only genuine cases (a ``threading.local`` / other C-extension subclass a
dataclass cannot model) may be annotated; everything else must become a
``@dataclass(slots=True)``. AST-based, so it matches real ``__slots__``
assignments only — not string references in tests that merely CHECK for slots.

Run: uv run python scripts/check_no_manual_slots.py [paths...]
Default paths: hyperdjango/ services/
"""

from __future__ import annotations

import ast
import pathlib
import sys

MARKER = "slots-required"


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    lines = text.splitlines()
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            targets = (
                stmt.targets
                if isinstance(stmt, ast.Assign)
                else [stmt.target]
                if isinstance(stmt, ast.AnnAssign)
                else []
            )
            if not any(
                isinstance(t, ast.Name) and t.id == "__slots__" for t in targets
            ):
                continue
            lineno = stmt.lineno
            here = lines[lineno - 1] if lineno - 1 < len(lines) else ""
            above = lines[lineno - 2] if lineno - 2 >= 0 else ""
            if MARKER in here or MARKER in above:
                continue
            violations.append(
                (
                    lineno,
                    f"class {node.name}: manual __slots__ — use @dataclass(slots=True)",
                )
            )
    return violations


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(p) for p in argv[1:]] or [
        pathlib.Path("hyperdjango"),
        pathlib.Path("services"),
    ]
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
            f"\nFAILED: {total} manual __slots__ declaration(s).\n"
            "Use `@dataclass(slots=True)` instead (the codebase convention), OR — only\n"
            "for a type a dataclass genuinely cannot model (a C-extension base such as\n"
            "threading.local) — annotate `# slots-required: <why>`.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: no manual __slots__ in {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
