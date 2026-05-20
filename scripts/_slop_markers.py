"""Shared marker-window logic for the source-invariant checks.

Every "code slop" check offers the same escape hatch: a statement that
genuinely needs the flagged construct proves it with a ``# <marker>: <why>``
comment. This module owns the single question "is that proof attached to this
statement?", because getting it wrong is silently expensive in both
directions — too narrow a window rejects a correctly-annotated line, too wide
a one lets a marker written for a neighbour launder an unrelated violation.

The window is the statement's own lines plus the CONTIGUOUS COMMENT BLOCK
immediately above it. Comment blocks, not a fixed line count: the house style
explains a boundary in prose and then names it, e.g.

    # SUDO_USER is set by sudo(8); no setting can carry "who typed sudo".
    # env-boundary: process invocation context, not framework configuration.
    invoker = os.environ.get("SUDO_USER", "")

A one-line window would reject that unless the marker were crammed onto the
last line, so authors either mangle the prose or drop the explanation — the
check would be training away the very thing it wants. Scanning the block lets
the marker sit on whichever line reads best.

The block ends at the first line that is not a comment, blank lines included:
a detached comment separated by whitespace belongs to whatever came before,
and must not vouch for the statement below it.
"""

from __future__ import annotations

import functools
import re


@functools.cache
def marker_pattern(name: str) -> re.Pattern[str]:
    """``# <name>: <reason>`` — the reason is REQUIRED.

    A bare ``# env-boundary`` states that a rule was noticed, not why it does
    not apply, and that is exactly the annotation worth nothing to the next
    reader. Two of these checks already demanded a reason and two accepted the
    marker alone; the strict form is the one worth keeping.
    """
    return re.compile(rf"#\s*{re.escape(name)}\s*:\s*\S")


def has_marker(lines: list[str], start: int, end: int, name: str) -> bool:
    """True if a justified ``name`` marker is attached to lines ``start..end``.

    ``start`` and ``end`` are 1-based inclusive line numbers (``ast`` node
    ``lineno`` / ``end_lineno``). Attached means: on one of the statement's own
    lines, or anywhere in the unbroken run of comment lines directly above it.
    """
    pattern = marker_pattern(name)
    for raw in lines[start - 1 : end]:
        if pattern.search(raw):
            return True
    idx = start - 2  # 0-based index of the line directly above the statement
    while idx >= 0:
        stripped = lines[idx].strip()
        if not stripped.startswith("#"):
            return False
        if pattern.search(stripped):
            return True
        idx -= 1
    return False
