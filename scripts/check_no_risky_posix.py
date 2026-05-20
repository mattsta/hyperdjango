#!/usr/bin/env python
"""Enforcement gate: no panic-prone `std.posix` syscall wrapper on a socket path.

Zig's ``std.posix.<syscall>`` wrappers translate a fixed set of errnos to
``unreachable`` — which PANICS under ReleaseSafe and is silent UB under
ReleaseFast. A peer-closed / cancelled / torn-down socket returns exactly those
errnos (EBADF, ENOTSOCK, EINVAL, EPIPE, ECONNRESET, EFAULT). The ``try``/``catch``
around such a call CANNOT catch a panic raised INSIDE the wrapper, so a dead
socket aborts the worker thread. This is the class behind a long string of CI
crashes (setsockopt on a torn-down pinned connection, RCVTIMEO on a dead
socket, …), each found one CI run at a time.

The rule that collapses the whole class: every socket / fd I/O syscall on a path
that can touch a peer, cancelled, or torn-down fd must go through the RAW layer —
``std.c.<call>`` or ``std.posix.system.<call>`` — which returns the errno for the
caller to handle, instead of the ``std.posix.<call>`` wrapper that asserts
``unreachable``.

This checker FAILS (exit 1) on any ``std.posix.<fn>(`` / ``posix.<fn>(`` call to
a risky syscall wrapper (see ``_RISKY``) under ``zig/src/**``, EXCEPT:

  - ``std.posix.system.<fn>`` / ``posix.system.<fn>`` — the raw syscall layer,
    which has no error switch and cannot panic; and
  - a call carrying a prover comment on its own line or the line above:

        // posix-safe: <why this wrapper cannot hit its unreachable errnos here>

Constants and types (``posix.SOL.SOCKET``, ``posix.timeval``, ``posix.fd_t`` …)
are never matched — the pattern requires a call: ``<name>(``.

Run: uv run python scripts/check_no_risky_posix.py [paths...]
Default path: zig/src/
"""

from __future__ import annotations

import pathlib
import re
import sys

MARKER = "posix-safe"

# std.posix wrappers with an `unreachable` errno arm that a dead / half-closed /
# cancelled socket (or a teardown race) can plausibly hit. Route these through
# std.c.<fn> / posix.system.<fn> instead.
_RISKY = frozenset(
    {
        "send",
        "recv",
        "recvfrom",
        "sendto",
        "sendmsg",
        "recvmsg",
        "read",
        "write",
        "writev",
        "readv",
        "pread",
        "pwrite",
        "connect",
        "accept",
        "accept4",
        "shutdown",
        "listen",
        "bind",
        "setsockopt",
        "getsockopt",
        "getpeername",
        "getsockname",
        "poll",
        "ppoll",
        "close",
        "fcntl",
        "sigaction",
    }
)

# `std.posix.foo(` or `posix.foo(` — but NOT `...system.foo(` (raw layer), and
# only a CALL (open paren), so constants/types like `posix.SOL.SOCKET` or
# `posix.timeval{` never match.
_CALL = re.compile(r"(?<![\w.])(?:std\.)?posix\.(\w+)\s*\(")


def _marked_above(lines: list[str], i: int) -> bool:
    """True if the contiguous `//` comment block immediately above line ``i``
    carries the prover marker (so a multi-line justification works)."""
    j = i - 1
    while j >= 0 and lines[j].lstrip().startswith("//"):
        if MARKER in lines[j]:
            return True
        j -= 1
    return False


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return []
    violations: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        for m in _CALL.finditer(line):
            fn = m.group(1)
            if fn not in _RISKY:
                continue
            # Exclude the raw syscall layer: `posix.system.<fn>(`.
            if line[: m.start()].rstrip().endswith(".system"):
                continue
            if MARKER in line or _marked_above(lines, i):
                continue
            violations.append(
                (
                    i + 1,
                    f"std.posix.{fn}() — route through std.c.{fn}() or annotate `// {MARKER}: <why>`",
                )
            )
    return violations


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(p) for p in argv[1:]] or [pathlib.Path("zig/src")]
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.zig")))
        elif root.suffix == ".zig":
            files.append(root)

    total = 0
    for f in files:
        for lineno, text in check_file(f):
            print(f"{f}:{lineno} — {text}")
            total += 1

    if total:
        print(
            f"\nFAILED: {total} panic-prone std.posix syscall wrapper call(s) on a "
            "socket path.\nRoute each through the raw layer (std.c.<fn> / "
            "std.posix.system.<fn>), which RETURNS the errno instead of asserting\n"
            "`unreachable` (a panic under ReleaseSafe that a torn-down socket "
            "triggers), OR annotate `// posix-safe: <why it cannot panic here>`.",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: no panic-prone std.posix socket-syscall wrappers in {len(files)} files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
