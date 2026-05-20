"""SSRF guard (hyperdjango.net.validate_public_url) — public-surface unit tests.

# hyper-test: unit

Covers the internal-range blocks AND the IPv4-literal encoding bypasses
(octal/hex/decimal-integer) where a DNS resolver and a TCP client's libc
inet_aton disagree — e.g. getaddrinfo reads "0177.0.0.1" as 177.0.0.1 (public)
while a client connects to 127.0.0.1 (loopback). The guard must block the whole
class, not just the canonical dotted-decimal form.
"""

from hyperdjango.net import UnsafeURLError, validate_public_url
from hyperdjango.testkit import check, finish, run_main


def _blocked(url: str) -> bool:
    try:
        validate_public_url(url)
        return False
    except UnsafeURLError:
        return True


# Internal ranges — every form must be refused.
MUST_BLOCK = [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://[::1]/",
    "http://0.0.0.0/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://100.64.0.1/",  # CGNAT
    "http://[::ffff:127.0.0.1]/",  # ipv4-mapped ipv6
    "http://public.com@127.0.0.1/",  # userinfo trick
    # IPv4-literal encoding bypasses (validator vs client parser differential):
    "http://0177.0.0.1/",  # octal → 127.0.0.1
    "http://0177.0.0.01/",
    "http://0x7f.0.0.1/",  # hex → 127.0.0.1
    "http://0x7f000001/",  # single hex dword
    "http://2130706433/",  # decimal int → 127.0.0.1
    "http://017700000001/",  # octal dword
    "http://010.0.0.1/",  # octal-ambiguous 10.x
    # Non-http schemes:
    "gopher://127.0.0.1/",
    "file:///etc/passwd",
]

MUST_ALLOW = [
    "http://example.com/",
    "https://example.com/path?q=1",
    "http://1.1.1.1/",
    "http://8.8.8.8/",
]


def main() -> bool:
    for u in MUST_BLOCK:
        check(f"block {u}", _blocked(u))
    for u in MUST_ALLOW:
        check(f"allow {u}", not _blocked(u))
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
