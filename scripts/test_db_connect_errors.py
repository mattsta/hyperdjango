"""Test pg.zig connection error reporting end-to-end.

# hyper-test: unit

Exercises each ConnectError variant we can trigger from userland and
asserts the Python-side error message contains the *specific* variant
name — not a collapsed "ConnectionRefused" for everything. This is the
regression net for the bug that hid 3 different CI root causes
(auth-failed, FD-exhaustion, real TCP refused) all behind one
ConnectionRefused string and made the whole CI fix take ~10 iterations.
"""

import sys

from hyperdjango._hyperdjango_native import _db_configure


def _expect_err(name: str, url: str, expected_substr: str) -> tuple[bool, str]:
    try:
        h = _db_configure(url, 1, 5000, 0, 0, 0)
        return False, f"{name}: expected RuntimeError but got handle={h}"
    except RuntimeError as e:
        msg = str(e)
        if expected_substr in msg:
            return True, f"{name}: ✓ {expected_substr!r} in error"
        return False, f"{name}: expected {expected_substr!r} in error, got: {msg}"


CASES: list[tuple[str, str, str]] = [
    # name, conn_url, expected_substr_in_python_error
    # Port 1 (tcpmux) is closed on every reasonable host. Picking a known
    # closed port avoids the race that "find a free port and connect to it"
    # has on macOS where the kernel can briefly accept on a TIME_WAIT port.
    (
        "tcp_refused",
        "postgres://postgres@127.0.0.1:1/postgres",
        "ConnectionRefused",
    ),
    (
        "dns_resolve_failed",
        "postgres://postgres@no-such-host-exists.invalid:5432/postgres",
        "DnsResolveFailed",
    ),
]


def main() -> int:
    passed = 0
    failed = 0
    for name, url, expected in CASES:
        ok, line = _expect_err(name, url, expected)
        print(f"  {line}")
        if ok:
            passed += 1
        else:
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
