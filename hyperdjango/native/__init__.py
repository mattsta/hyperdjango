"""
Native acceleration module.

Uses the unified _hyperdjango_native Zig extension (which bundles
SIMD validation, pg.zig database, and HTTP server/router).

The native extension is ALWAYS required — build with:
    uv run hyper-build
"""

# ruff: noqa: F401  — public API re-exports

import hyperdjango._hyperdjango_native as _native

# --- Build mode ---
is_release_build: bool = _native._is_release_build()

# --- JSON ---
fast_json_loads = _native.json_loads_native
fast_json_dumps = _native.json_dumps_native

# --- String ops (SIMD-accelerated) ---
html_escape = _native.html_escape_native
url_encode = _native.url_encode_native
url_decode = _native.url_decode_native
parse_query_string = _native.parse_query_string_native


def parse_cookies(header):
    """Parse a Cookie header into a dict. Hardened wrapper: a hostile cookie value
    that percent-decodes to non-UTF-8 bytes (e.g. ``session=%80``) must never crash
    request handling — fall back to a lenient split that keeps raw values."""
    try:
        return _native.parse_cookies_native(header)
    except UnicodeDecodeError, ValueError:
        out: dict[str, str] = {}
        for part in header.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                k = k.strip()
                if k:
                    out[k] = v.strip()
        return out


# --- Base encoding (arbitrary-base int↔string) ---
base_encode = _native.base_encode_native
base_decode = _native.base_decode_native

# --- XOR (SIMD-accelerated, repeating mask) ---
# Loaded conditionally: available after rebuild with xor_bytes support.
# signing.py falls back to Python XOR if this is not yet available.
# dynamic-attr: xor_bytes_native is an optional native symbol present only in builds compiled with XOR support; probe with a fallback default
xor_bytes = getattr(_native, "xor_bytes_native", None)

# --- Crypto (argon2-cffi for password hashing) ---
# Single source of truth in _crypto.py with explicit secure params
from hyperdjango.native._crypto import (
    hash_password,
    needs_rehash,
    verify_password,
)

__all__ = [
    "fast_json_dumps",
    "fast_json_loads",
    "html_escape",
    "url_encode",
    "url_decode",
    "parse_query_string",
    "parse_cookies",
    "base_encode",
    "base_decode",
    "xor_bytes",
    "hash_password",
    "verify_password",
    "is_release_build",
]
