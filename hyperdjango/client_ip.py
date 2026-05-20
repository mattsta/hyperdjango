"""Client-IP resolution — the single authority for the trusted client address.

Rate limiting, audit logging, and security events all key on the client IP, so
X-Forwarded-For / X-Real-IP must be trusted by exactly one policy everywhere.
Both the ASGI ``Request.client_ip`` and the Django middleware chain resolve the
address here, so neither can trust a forwarding header the other wouldn't.
"""

from hyperdjango.conf import get_setting


def resolve_client_ip(
    peer_ip: str, forwarded_for: str | None, real_ip: str | None
) -> str:
    """Best-effort real client IP, resistant to X-Forwarded-For spoofing.

    SECURITY: ``X-Forwarded-For`` / ``X-Real-IP`` are attacker-controlled unless
    the request actually passed through a reverse proxy we trust. Trusting them
    unconditionally lets an attacker present a unique IP per request — defeating
    IP-based rate limiting AND growing per-IP buckets without bound (memory DoS).
    So forwarding headers are honored ONLY when:

      - ``TRUSTED_PROXY_COUNT`` > 0 (you run N reverse-proxy hops), or
      - the socket peer (``peer_ip``) is listed in ``TRUSTED_PROXIES``.

    With neither configured (the default), the socket peer address is returned.
    """
    proxy_count = int(get_setting("TRUSTED_PROXY_COUNT") or 0)
    trusted_proxies = get_setting("TRUSTED_PROXIES") or []

    trust_headers = proxy_count > 0 or (
        bool(trusted_proxies) and peer_ip in trusted_proxies
    )
    if not trust_headers:
        return peer_ip

    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            if proxy_count > 0:
                # Trust exactly the last ``proxy_count`` hops; the client is the
                # entry just left of them. Clamp so a short/forged chain can't
                # index past the real client into an attacker-supplied value.
                idx = len(parts) - proxy_count
                return parts[idx] if idx >= 0 else parts[0]
            # Peer is an allow-listed proxy but no hop count given — the original
            # client is the left-most entry it forwarded.
            return parts[0]
    if real_ip:
        return real_ip.strip()
    return peer_ip
