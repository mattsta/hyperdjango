"""Validation gate: no panic-prone `std.posix` syscall wrapper on a socket path.

Fails if any risky ``std.posix.<syscall>(`` wrapper (send/recv/read/write/
setsockopt/poll/close/…) appears in zig/src/** without going through the raw
``std.c.<call>`` / ``std.posix.system.<call>`` layer or carrying a
``// posix-safe: <reason>`` prover comment. See scripts/check_no_risky_posix.py
for the rule.

This collapses an entire recurring crash class: those wrappers assert
``unreachable`` on errnos a peer-closed / cancelled / torn-down socket returns
(EBADF/ENOTSOCK/EINVAL/EPIPE/ECONNRESET), and the surrounding try/catch cannot
catch a panic raised inside the wrapper — so a dead socket aborts a worker
thread (a ReleaseSafe panic; silent UB under ReleaseFast). The gate blocks a
regression at commit time instead of discovering it one CI run at a time.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_no_risky_posix as gate  # noqa: E402


def _all_violations():
    src = _ROOT / "zig" / "src"
    out = []
    for f in sorted(src.rglob("*.zig")):
        for lineno, text in gate.check_file(f):
            out.append(f"{f.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_risky_posix():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} panic-prone std.posix syscall wrapper(s) on a socket "
        "path. Route each through the raw layer (std.c.<fn> / "
        "std.posix.system.<fn>), which returns the errno instead of asserting "
        "`unreachable`, OR annotate `// posix-safe: <why it cannot panic here>`:\n"
        + "\n".join(violations[:50])
        + ("\n…" if len(violations) > 50 else "")
    )
