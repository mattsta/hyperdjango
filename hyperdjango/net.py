"""SSRF-safe outbound URL validation + fetch.

Social/CMS features fetch user-supplied URLs (link previews, avatar-by-URL,
webhooks). Naively fetching them is a Server-Side Request Forgery hole: a URL
like ``http://169.254.169.254/latest/meta-data/`` (cloud metadata) or
``http://127.0.0.1:6379`` (internal Redis) reaches services the client could
never touch directly.

``validate_public_url`` is the single authority: parse → require http(s) →
resolve the host → refuse if ANY resolved address is loopback / private /
link-local / reserved / multicast / unspecified (covers 127/8, 10/8, 172.16/12,
192.168/16, 169.254/16 incl. the metadata IP, ::1, fc00::/7, fe80::/10, …).
``safe_get`` validates first, then fetches with redirects disabled (each hop
would otherwise re-open the SSRF), a timeout, and a response-size cap.

NOTE: a validated hostname is re-resolved by the HTTP client at connect time, so
this does not by itself defeat DNS rebinding (a name that resolves public here
then private at connect). For untrusted-URL fetching at scale, additionally pin
the connection to the validated IP.
"""

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(Exception):
    """Raised when a URL is not a safe public target (SSRF guard)."""


# Ranges not flagged by the ipaddress boolean properties but still internal.
_EXTRA_BLOCKED = [
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
]


def _ipv4_literal_candidates(host: str) -> set[str]:
    """Every IPv4 address a client's parser might connect ``host`` to.

    An IPv4 literal can be written many ways — dotted-decimal (127.0.0.1),
    dotted with octal/hex octets (0177.0.0.1, 0x7f.0.0.1), fewer-than-4 parts
    whose last spans the remaining bytes (127.1), or a bare integer
    (2130706433, 0x7f000001, 017700000001). Worse, a single spelling is
    AMBIGUOUS across parsers: "010.0.0.1" is 10.0.0.1 to a decimal reader but
    8.0.0.1 to a C ``inet_aton`` (leading zero → octal). An SSRF filter that
    trusts ONE parser (or ``getaddrinfo``, whose handling of these forms varies
    by platform) is bypassed when the HTTP client disagrees.

    So decode the literal under BOTH a strict-decimal and a C-style auto-base
    reading of each octet and return every resulting address; the caller blocks
    if ANY is internal. An empty set means ``host`` is not an IPv4 literal (a
    real hostname / IPv6) and must go through DNS resolution instead.
    """
    parts = host.split(".")
    if not host or len(parts) > 4:
        return set()
    candidates: set[str] = set()
    for decimal_only in (True, False):
        values: list[int] = []
        for part in parts:
            if not part:
                break
            try:
                if decimal_only:
                    if not part.isdigit():
                        break
                    value = int(part, 10)
                elif part.startswith(("0x", "0X")):
                    value = int(part, 16)  # C inet_aton: 0x → hex
                elif part.startswith("0") and len(part) > 1:
                    # C inet_aton reads a leading-zero octet as OCTAL. (Python's
                    # int(x, 0) rejects "0177" — base-0 wants an explicit "0o"
                    # prefix — so decode base 8 directly.)
                    value = int(part, 8)
                else:
                    value = int(part, 10)
            except ValueError:
                break
            values.append(value)
        if len(values) != len(parts):
            continue
        # inet_aton packing: with <4 parts the final value spans the leftover
        # low-order bytes (a.b → a.<24-bit b>, a.b.c → a.b.<16-bit c>).
        n = len(values)
        leading, last = values[:-1], values[-1]
        if any(v > 0xFF for v in leading):
            continue
        if last > (0xFFFFFFFF >> (8 * (n - 1))):
            continue
        addr = last
        for i, v in enumerate(leading):
            addr |= v << (8 * (3 - i))
        candidates.add(str(ipaddress.IPv4Address(addr)))
    return candidates


def _ip_is_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → refuse
    # Map IPv4-in-IPv6 (::ffff:127.0.0.1) down to the IPv4 for the checks below.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip in net for net in _EXTRA_BLOCKED)


def validate_public_url(
    url: str, *, allowed_schemes: tuple[str, ...] = ("http", "https")
) -> str:
    """Return ``url`` if it targets a public host, else raise ``UnsafeURLError``.

    Resolves the host and refuses if ANY resolved address is internal — so a
    name that resolves to both a public and an internal address is still refused.
    Blocking; call it off the event loop (``safe_get`` does) if latency matters.
    """
    parsed = urlparse(url)
    if parsed.scheme not in allowed_schemes:
        raise UnsafeURLError(
            f"scheme {parsed.scheme!r} not allowed (want one of {allowed_schemes})"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    # If the host is an IPv4 literal (in ANY encoding), decode every address a
    # client parser could reach from it and block if any is internal. This
    # closes the validator-vs-client differential that trusting getaddrinfo or a
    # single inet_aton reading leaves open (e.g. "010.0.0.1" → 10.0.0.1 to a
    # decimal parser, 8.0.0.1 to inet_aton). A literal needs no DNS — it
    # resolves to itself — so a fully-public literal returns here.
    literal_candidates = _ipv4_literal_candidates(host)
    if literal_candidates:
        for cand in literal_candidates:
            if _ip_is_blocked(cand):
                raise UnsafeURLError(
                    f"host {host!r} is an IPv4 literal a client could resolve to "
                    f"the blocked address {cand}"
                )
        return url

    # Not an IPv4 literal → a hostname (or IPv6 literal). Resolve and refuse if
    # ANY resolved address is internal.
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host {host!r}") from exc
    if not infos:
        raise UnsafeURLError(f"host {host!r} did not resolve")
    for info in infos:
        addr = info[4][0]
        if _ip_is_blocked(addr):
            raise UnsafeURLError(f"host {host!r} resolves to blocked address {addr}")
    return url


async def safe_get(
    url: str, *, timeout: float = 10.0, max_bytes: int = 5_000_000, **kwargs
):
    """Validate ``url`` (SSRF guard), then GET it with redirects disabled and a
    response-size cap. Returns the ``httpx.Response``. Raises ``UnsafeURLError``
    for an unsafe target.

    Redirects are disabled because each hop is a fresh URL that would re-open the
    SSRF; validate + re-issue explicitly if you need to follow them.
    """
    import asyncio

    import httpx

    # Resolution/validation is blocking — keep it off the event loop.
    await asyncio.get_running_loop().run_in_executor(None, validate_public_url, url)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.get(url, **kwargs)
        # Bound the response body (a huge download is its own DoS).
        if len(resp.content) > max_bytes:
            raise UnsafeURLError(f"response exceeds {max_bytes} bytes")
        return resp
