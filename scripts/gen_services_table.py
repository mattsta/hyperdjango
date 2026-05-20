"""Render the bundled-services table in the docs from the registry itself.

``hyperdjango/services_registry.py`` is the single source of truth for every
machine-readable fact about a service (port, database, secrets, companions,
description). A hand-maintained markdown copy of those facts drifts the moment
someone adds a service — so the table in ``docs/running-services.md`` is
generated, delimited by HTML comment markers, and verifiable in CI.

Usage:
    uv run python scripts/gen_services_table.py            # print the table
    uv run python scripts/gen_services_table.py --write    # rewrite the doc
    uv run python scripts/gen_services_table.py --check    # exit 1 if stale
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperdjango.services_registry import (  # noqa: E402
    SERVICE_PORT_BLOCK,
    Service,
    get_service,
    service_names,
)

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "running-services.md"
BEGIN = "<!-- BEGIN generated services table -->"
END = "<!-- END generated services table -->"


@dataclass(frozen=True, slots=True)
class Row:
    """One rendered table row's cells."""

    name: str
    port: str
    needs: str
    demonstrates: str

    def render(self) -> str:
        return f"| `{self.name}` | {self.port} | {self.needs} | {self.demonstrates} |"


def needs_of(service: Service) -> str:
    """The human-readable "what this service requires" cell."""
    parts: list[str] = []
    parts.append("database" if service.needs_database else "—")
    generated = service.generated_secrets
    supplied = service.supplied_secrets
    if generated:
        parts.append(f"{len(generated)} generated secrets")
    if supplied:
        parts.append("YOU supply " + ", ".join(s.env_var for s in supplied))
    if service.companions:
        parts.append("starts " + ", ".join(f"`{c}`" for c in service.companions))
    return "<br>".join(parts)


def rows() -> tuple[Row, ...]:
    return tuple(
        Row(
            name=name,
            port=str(get_service(name).port),
            needs=needs_of(get_service(name)),
            demonstrates=get_service(name).description,
        )
        for name in service_names()
    )


def render_table() -> str:
    lines = [
        BEGIN,
        "<!-- regenerate: uv run python scripts/gen_services_table.py --write -->",
        "",
        "| Service | Port | Needs | Demonstrates |",
        "| ------- | ---- | ----- | ------------ |",
    ]
    lines.extend(row.render() for row in rows())
    lines.extend(
        [
            "",
            f"Ports live in the {SERVICE_PORT_BLOCK.start}-{SERVICE_PORT_BLOCK.stop - 1} "
            "block, deliberately disjoint from the test suite's reserved ports.",
            END,
        ]
    )
    return "\n".join(lines)


def splice(doc: str, table: str) -> str:
    """Replace the marked region of ``doc`` with ``table``."""
    start = doc.find(BEGIN)
    end = doc.find(END)
    if start == -1 or end == -1:
        raise SystemExit(
            f"markers not found in {DOC_PATH} — expected {BEGIN} ... {END}"
        )
    return doc[:start] + table + doc[end + len(END) :]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the doc in place")
    parser.add_argument("--check", action="store_true", help="fail if the doc is stale")
    args = parser.parse_args()

    table = render_table()
    if not (args.write or args.check):
        print(table)
        return 0

    doc = DOC_PATH.read_text(encoding="utf-8")
    updated = splice(doc, table)
    if args.check:
        if updated != doc:
            print(
                f"{DOC_PATH} is out of date with hyperdjango/services_registry.py — "
                "run: uv run python scripts/gen_services_table.py --write",
                file=sys.stderr,
            )
            return 1
        print(f"{DOC_PATH}: services table matches the registry")
        return 0
    DOC_PATH.write_text(updated, encoding="utf-8")
    print(f"{DOC_PATH}: services table regenerated from the registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
