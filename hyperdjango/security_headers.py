"""Security response headers — the single authority for the header set.

Both the ASGI ``SecurityHeadersMiddleware`` (``hyperdjango.standalone_middleware``)
and the Django ``HyperSecurityMiddleware`` (``hyperdjango.serving.django_middleware``)
build their response-header set here, so a request served through either path
gets exactly the same protections (Referrer-Policy, Cross-Origin-Opener-Policy,
and the rest can never be present on one path and missing on the other).

The header keys are lowercase; HTTP header names are case-insensitive and this
matches the casing the native serving path emits.
"""


def build_security_headers(
    *,
    nosniff: bool,
    frame_options: str | None,
    hsts_seconds: int,
    hsts_include_subdomains: bool,
    hsts_preload: bool,
    csp: str | dict[str, str] | None,
    referrer_policy: str | None,
    permissions_policy: str | None,
    cross_origin_opener_policy: str | None,
) -> dict[str, str]:
    """Build the security response-header dict from resolved policy values.

    An empty/false value for any field simply omits that header.
    """
    headers: dict[str, str] = {}
    if nosniff:
        headers["x-content-type-options"] = "nosniff"
    if frame_options:
        headers["x-frame-options"] = frame_options
    if hsts_seconds > 0:
        value = f"max-age={hsts_seconds}"
        if hsts_include_subdomains:
            value += "; includeSubDomains"
        if hsts_preload:
            value += "; preload"
        headers["strict-transport-security"] = value
    if isinstance(csp, dict) and csp:
        headers["content-security-policy"] = "; ".join(
            f"{directive} {value}" for directive, value in csp.items()
        )
    elif isinstance(csp, str) and csp:
        headers["content-security-policy"] = csp
    if referrer_policy:
        headers["referrer-policy"] = referrer_policy
    if permissions_policy:
        headers["permissions-policy"] = permissions_policy
    if cross_origin_opener_policy:
        headers["cross-origin-opener-policy"] = cross_origin_opener_policy
    return headers
