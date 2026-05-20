"""
Crypto operations — argon2id password hashing, HMAC signing.

Uses argon2-cffi for modern password hashing (PHC winner, memory-hard).
"""

import hmac
import secrets

import argon2

# ── Unified HMAC-SHA256 helpers ────────────────────────────────────────────
#
# Single source of truth for all HMAC signing across the platform. Uses
# `hmac.digest()` (CPython 3.7+) which goes directly to `_hashlib` — avoids
# the Python-level HMAC object and routes to OpenSSL HMAC. On modern CPUs
# with OpenSSL 3.x this uses **hardware SHA-NI (x86) or ARMv8 Crypto
# (arm64)** instructions — measured at 2.19 GB/s on Apple Silicon.
#
# Callers MUST use these helpers instead of `hmac.new(...).hexdigest()`.
# The latter pattern creates a Python HMAC object per call (~10% slower at
# typical public-ID input sizes) AND forces allocation of a 64-char hex
# string when most callers truncate to 16/32 chars.
#
# Input contract:
#   key:    bytes (call caller's .encode() once before the hot loop)
#   message: bytes (same)
# Output:
#   hmac_sha256_hex(key, msg) → 64-char hex str (full digest)
#   hmac_sha256_hex_truncated(key, msg, n) → first n hex chars (first n//2 bytes)
#   hmac_sha256_bytes(key, msg) → 32-byte digest
#   hmac_sha256_bytes_truncated(key, msg, n) → first n bytes
#
# The truncated variants avoid hex-encoding bytes you don't need.


def hmac_sha256_bytes(key: bytes, message: bytes) -> bytes:
    """Full 32-byte HMAC-SHA256 digest. Fast path via _hashlib."""
    return hmac.digest(key, message, "sha256")


def hmac_sha256_bytes_truncated(key: bytes, message: bytes, n: int) -> bytes:
    """First n bytes of HMAC-SHA256 digest."""
    return hmac.digest(key, message, "sha256")[:n]


def hmac_sha256_hex(key: bytes, message: bytes) -> str:
    """Full 64-char hex HMAC-SHA256 digest."""
    return hmac.digest(key, message, "sha256").hex()


def hmac_sha256_hex_truncated(key: bytes, message: bytes, n_hex_chars: int) -> str:
    """First n_hex_chars of the hex HMAC-SHA256 digest.

    Faster than `.hex()[:n]` because it only hex-encodes the bytes we need
    (hex-encoding 32 bytes when you want 16 chars wastes 24 bytes of work).
    """
    byte_count = (n_hex_chars + 1) // 2  # round up in case of odd n
    return hmac.digest(key, message, "sha256")[:byte_count].hex()[:n_hex_chars]


def hmac_sha256_verify(key: bytes, message: bytes, signature_hex: str) -> bool:
    """Constant-time comparison of a hex signature against the expected HMAC.

    Used by verify_signed_data and token verification paths. The comparison
    uses `hmac.compare_digest` to prevent timing side-channels.
    """
    expected = hmac.digest(key, message, "sha256").hex()
    return hmac.compare_digest(signature_hex, expected)


def hmac_sha256_verify_truncated(
    key: bytes, message: bytes, signature_hex: str, n_hex_chars: int
) -> bool:
    """Constant-time compare of a TRUNCATED hex signature."""
    byte_count = (n_hex_chars + 1) // 2
    expected = hmac.digest(key, message, "sha256")[:byte_count].hex()[:n_hex_chars]
    return hmac.compare_digest(signature_hex, expected)


# Singleton hasher — argon2id with secure defaults
# time_cost=3, memory_cost=65536 (64MB), parallelism=4
_hasher = argon2.PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=argon2.Type.ID,
)


def hash_password(password: str) -> str:
    """Hash a password using argon2id.

    Encodes to UTF-8 bytes before hashing to support all unicode passwords.
    Returns an argon2id hash string:
        $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
    """
    return _hasher.hash(password.encode("utf-8"))


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against an argon2id hash."""
    try:
        return _hasher.verify(password_hash, password.encode("utf-8"))
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.InvalidHashError:
        return False


def needs_rehash(password_hash: str) -> bool:
    """Check if hash parameters are outdated and should be re-hashed on next login."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except argon2.exceptions.InvalidHashError:
        # Hash string can't be parsed (corrupt/legacy/foreign format) — it is
        # unusable as-is, so signal that it must be re-hashed on next login.
        return True


def generate_token(nbytes: int = 32) -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(nbytes)


def sign_data(data: str, secret: str) -> str:
    """Sign data with HMAC-SHA256. Uses the unified HMAC helper."""
    sig = hmac_sha256_hex(secret.encode(), data.encode())
    return f"{data}.{sig}"


def verify_signed_data(signed: str, secret: str) -> str | None:
    """Verify and extract data from a signed string. Returns None if invalid."""
    if "." not in signed:
        return None
    data, sig = signed.rsplit(".", 1)
    if hmac_sha256_verify(secret.encode(), data.encode(), sig):
        return data
    return None
