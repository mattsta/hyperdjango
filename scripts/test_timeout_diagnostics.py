#!/usr/bin/env python3
"""The runner reports WHERE a timed-out test hung, not merely that it did.

A file killed by the per-file ceiling never finalizes its subprocess log, so on
CI it is simply absent from the uploaded artifact — the only evidence a timeout
leaves is the word TIMEOUT. That is the one failure mode with no diagnosis
attached, and it cost a full cycle to learn nothing more than "it hung".

Children run under ``-X faulthandler``, whose default SIGABRT handler dumps
every thread's stack. The runner therefore aborts a hung child first and keeps
that dump with the failure. This pins that behaviour against a child that
really does hang.

Usage:
    uv run hyper-test timeout_diagnostics
"""

# hyper-test: unit

import asyncio
import os
import sys
import textwrap
from pathlib import Path

from hyperdjango.test_runner import _exec_subprocess, _hang_excerpt
from hyperdjango.testkit import check, finish, run_main

_HANGING_CHILD = """
import threading, time

def parked_in_a_named_function():
    threading.Event().wait()      # never set: a real, unkillable-by-timeout park

threading.Thread(target=parked_in_a_named_function, daemon=True).start()
print("child is up")
time.sleep(3600)                  # the main thread's park, which the dump shows
"""


def check_excerpt_formatting() -> None:
    check("empty stderr yields no excerpt", _hang_excerpt("") == "")
    check("whitespace-only stderr yields no excerpt", _hang_excerpt("  \n\n") == "")
    text = _hang_excerpt('Thread 0x1:\n  File "x.py", line 3 in parked\n')
    check("excerpt is labelled as thread stacks", "where it hung" in text)
    check("excerpt keeps the python frames", "parked" in text)
    check("excerpt is indented as evidence", "    │ " in text)
    long_dump = "Stack (most recent call first):\n" + "\n".join(
        f'  File "x.py", line {i} in f{i}' for i in range(500)
    )
    kept = _hang_excerpt(long_dump).count("│")
    check("excerpt is bounded", kept <= 60, f"kept {kept} lines")
    # faulthandler prints Python frames FIRST and a long binary C stack LAST.
    # Tailing it keeps "Binary file ..." and throws away the only lines that
    # name the hung test — the mistake this asserts against.
    c_stack = (
        "Stack (most recent call first):\n"
        '  File "hung_test.py", line 42 in the_hung_check\n'
        "Current thread's C stack trace (most recent call first):\n"
        + "\n".join(f'  Binary file "python" [0x{i:x}]' for i in range(200))
    )
    excerpt = _hang_excerpt(c_stack)
    check("excerpt keeps the python frame", "the_hung_check" in excerpt)
    check("excerpt drops the binary C stack", "Binary file" not in excerpt)


async def check_live_hang(tmp: Path) -> None:
    """A child that genuinely hangs must come back with its stacks."""
    script = tmp / "hanging_child.py"
    script.write_text(textwrap.dedent(_HANGING_CHILD))
    result = await _exec_subprocess(
        name="hanging_child",
        cmd=[sys.executable, "-X", "faulthandler", str(script)],
        cwd=str(tmp),
        env=dict(os.environ),
        timeout=3,
    )
    check("hung child is reported as a failure", result.failed == 1)
    check("hung child exits with the timeout code", result.exit_code == -1)
    check("failure still says TIMEOUT", "TIMEOUT" in result.error)
    check(
        "failure carries the thread dump",
        "where it hung" in result.error,
        result.error[:200],
    )
    # Free-threaded CPython prints "<Cannot show all threads while the GIL is
    # disabled>" and dumps only the CURRENT thread. That is still the answer
    # worth having — the main thread's frame says which check the file was
    # sitting in when the ceiling hit, which is the question a bare TIMEOUT
    # cannot answer. Background-thread stacks need a different mechanism.
    check(
        "the dump locates the main thread in the child's own source",
        "hanging_child.py" in result.error,
        result.error[-400:],
    )
    check(
        "the dump gives a line to look at",
        " line " in result.error,
        result.error[-200:],
    )


def main() -> bool:
    check_excerpt_formatting()
    tmp = Path(__file__).resolve().parent.parent / "logs" / "timeout_diag_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(check_live_hang(tmp))
    finally:
        for leftover in tmp.glob("*"):
            leftover.unlink(missing_ok=True)
        tmp.rmdir()
    return finish()


if __name__ == "__main__":
    run_main(main)
