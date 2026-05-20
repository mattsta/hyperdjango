"""
Token signing engine — HMAC-signed tokens with key rotation and XOR obfuscation.

Two token types for distributed web applications:

1. **Reference tokens** (type='r'): Signed opaque references for DB lookups.
   Use for session IDs and API key references. Any server can reject forgeries
   without touching the database.

2. **Data tokens** (type='d'): Signed + encoded key-value data, stateless.
   Use for email verification links, CSRF tokens, short-lived auth data.
   Any server can verify + decode without DB.

Both types support:
- Rolling key rotation (encode with newest key, decode tries all)
- XOR payload obfuscation (data not visible in token)
- Self-describing format (version + type prefix)

Security hardening (defense-in-depth):
- Per-token random salt (default 8 bytes) — kills determinism, blocks
  known-plaintext attacks against the XOR stream
- Salt-derived XOR mask — unique per token, recovering one reveals nothing
  about any other token
- Randomized JSON key ordering — breaks structural correlation across data tokens
- Optional payload padding — hides exact payload size in fixed-size buckets

Token format:
    {version_char}{type_char}{base62(xor(salt + payload + padding))}.{base62(hmac_signature)}

Usage:
    from hyperdjango.signing import TokenEngine, SigningKey

    engine = TokenEngine(keys=[
        SigningKey(secret="app-key-2026-q2", version=2),  # newest, signs
        SigningKey(secret="app-key-2026-q1", version=1),  # still accepted
    ])

    # Signed reference (for session/API key DB lookups)
    token = engine.encode_ref("sess_abc123def456")
    ref = engine.decode_ref(token)  # "sess_abc123def456" or None

    # Signed data (stateless, no DB needed)
    token = engine.encode_data({"user_id": 42, "role": "admin"}, ttl=3600)
    data = engine.decode_data(token)  # {"user_id": 42, "role": "admin"} or None

Model mixins:
    from hyperdjango.signing import SignedSessionMixin, SignedAPIKeyMixin

    class Session(SignedSessionMixin, TimestampMixin, Model):
        class TokenConfig:
            keys = [SigningKey(secret="sess-2026-q2", version=1)]
        user_id: int = Field()

    class APIKey(SignedAPIKeyMixin, TimestampMixin, Model):
        class TokenConfig:
            keys = [SigningKey(secret="key-2026-q2", version=1)]
            key_display_prefix = "sk_myapp_"
        user_id: int = Field()
        name: str = Field(default="")
"""

import hashlib
import hmac
import json
import random
import secrets
import time
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import ClassVar, Final

from hyperdjango.models import Field as _ModelField
from hyperdjango.models import Model as _Model
from hyperdjango.native import xor_bytes as _maybe_native_xor_bytes
from hyperdjango.native._crypto import (
    hmac_sha256_bytes,
    hmac_sha256_bytes_truncated,
)
from hyperdjango.public_id import ALPHANUMERIC_CHARS, BaseEncoder
from hyperdjango.validation.core.fields import FieldInfo as _FieldInfo

# ── Constants ──────────────────────────────────────────────────────────────

# Base62 encoder shared by all TokenEngine instances (Zig-accelerated)
_BASE62: Final[BaseEncoder] = BaseEncoder(ALPHANUMERIC_CHARS)

# Version chars: base62 digits (0-9, a-z, A-Z) → versions 0-61
_VERSION_CHARS: Final[str] = ALPHANUMERIC_CHARS

# Token type markers
_TYPE_REF: Final[str] = "r"
_TYPE_DATA: Final[str] = "d"
_VALID_TYPES: Final[frozenset[str]] = frozenset({_TYPE_REF, _TYPE_DATA})

# Separator between payload and signature
_SEP: Final[str] = "."

# Max data token payload before compression (4 KB)
_MAX_DATA_BYTES: Final[int] = 4096

# Upper bound on a whole token / its base62 signature, checked BEFORE the
# O(N²) base62 decode that runs pre-HMAC. base62 inflates bytes ~1.34×, so a
# 4 KB payload plus salt/padding fits well under 8 KB; an 8-byte signature is
# ~12 base62 chars, so 64 leaves ample slack for larger signature_bytes without
# ever letting an attacker feed a huge string into the decoder unauthenticated.
_MAX_TOKEN_CHARS: Final[int] = 8192
_MAX_SIG_CHARS: Final[int] = 64

# Default salt size (8 bytes = 64 bits of randomness per token)
_DEFAULT_SALT_BYTES: Final[int] = 8

# XOR mask derivation domain separator
_XOR_DOMAIN: Final[str] = "hyper-xor-v"

# Padding bucket sizes — payload padded to nearest bucket boundary
# Chosen to cover common token sizes with reasonable granularity
_PAD_BUCKETS: Final[tuple[int, ...]] = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096)

# ── Base62 byte encoding ──────────────────────────────────────────────────
#
# BaseEncoder.encode_bytes() treats bytes as a big-endian integer.
# This loses leading zero bytes (0x00 → integer 0 → gone).
# XOR output routinely has leading zeros, so we need byte-exact roundtrips.
#
# Fix: prepend a 0x01 sentinel before integer conversion.
# On decode, compute byte length from the integer itself (leading 1-bit
# tells us where the sentinel is), then strip it.


def _bytes_to_base62(data: bytes) -> str:
    """Encode arbitrary bytes to base62, preserving leading zeros."""
    # 0x01 sentinel ensures the integer always has a leading 1-bit,
    # so int.to_bytes() roundtrips to the exact original length.
    value = int.from_bytes(b"\x01" + data, "big")
    return _BASE62.encode(value)


def _base62_to_bytes(code: str) -> bytes:
    """Decode base62 back to bytes, stripping the 0x01 sentinel."""
    value = _BASE62.decode(code)
    # Compute byte length from integer (sentinel guarantees leading 1)
    byte_len = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_len, "big")
    # Strip the 0x01 sentinel
    if not raw or raw[0] != 0x01:
        raise ValueError("Invalid base62 bytes (missing sentinel)")
    result = raw[1:]
    # Reject NON-CANONICAL encodings. base62 ignores leading "0" digits, so
    # "0"*k + code decodes to the same integer/bytes — distinct token STRINGS
    # that authenticate identically (token malleability). The encoder always
    # emits the canonical minimal form (the 0x01 sentinel forces a nonzero top
    # digit), so a legitimate token round-trips exactly; anything that doesn't
    # is a crafted alternate encoding and is refused. Gives every authenticated
    # value exactly one string form (so string-keyed caches/revocation hold).
    if _bytes_to_base62(result) != code:
        raise ValueError("Non-canonical base62 encoding")
    return result


# ── Core types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SigningKey:
    """A versioned HMAC signing key.

    Keys are ordered newest-first in TokenEngine.keys. The newest key
    (index 0) is used for signing new tokens. All keys are tried during
    verification to support graceful key rotation.

    Args:
        secret: HMAC-SHA256 secret string. Should be cryptographically
            random, at least 32 characters.
        version: Key version (0-61). Encoded as a single base62 character
            in the token prefix for O(1) key lookup on decode.
    """

    secret: str
    version: int

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("SigningKey.secret must not be empty")
        if not 0 <= self.version <= 61:
            raise ValueError(f"SigningKey.version must be 0-61, got {self.version}")


# ── XOR helpers ───────────────────────────────────────────��────────────────


def _derive_xor_mask(key: SigningKey, salt: bytes = b"") -> bytes:
    """Derive a 32-byte XOR mask from a signing key and optional salt.

    When salt is provided, the mask is unique per token (per-token salt
    makes each XOR stream independent — recovering one reveals nothing
    about any other token). Without salt, the mask is static per key version.

    Uses HMAC-SHA256 with a domain-separated label so the mask is
    independent from the token HMAC.
    """
    label = f"{_XOR_DOMAIN}{key.version}".encode() + salt
    # Central HMAC helper — OpenSSL HMAC fast path with hardware SHA-NI/NEON.
    return hmac_sha256_bytes(key.secret.encode("utf-8"), label)


@lru_cache(maxsize=256)
def _static_xor_mask(key: SigningKey) -> bytes:
    """Static (salt-free) XOR mask for a key, memoized per SigningKey.

    The salt-free mask depends only on the key, so it's constant for the key's
    lifetime. It's needed on every encode and decode (to obfuscate/recover the
    salt); recomputing an HMAC-SHA256 each time is pure waste. SigningKey is a
    frozen dataclass (hashable), so it's a safe cache key.
    """
    return _derive_xor_mask(key, b"")


def _xor_with_mask(data: bytes, mask: bytes) -> bytes:
    """XOR data with a repeating mask (Zig SIMD-accelerated).

    The mask repeats cyclically to cover the full data length.
    Uses native SIMD: 32 bytes/cycle for 32-byte masks (HMAC-SHA256),
    16 bytes/cycle for other sizes, scalar for tails.
    """
    if not data:
        return data
    if _maybe_native_xor_bytes is not None:
        return _maybe_native_xor_bytes(data, mask)
    # Pure Python fallback (pre-rebuild or test environments)
    mask_len = len(mask)
    return bytes(b ^ mask[i % mask_len] for i, b in enumerate(data))


# ── Padding helpers ────────────────────────────────────────────────────────


def _pad_to_bucket(data: bytes) -> bytes:
    """Pad data with random bytes to the next bucket boundary.

    Hides exact payload size. The first 2 bytes encode the real payload
    length (big-endian), followed by the payload, then random padding
    to the bucket boundary.
    """
    payload_len = len(data)
    # Find smallest bucket that fits: 2 bytes length prefix + payload
    total_needed = 2 + payload_len
    target = total_needed
    for bucket in _PAD_BUCKETS:
        if bucket >= total_needed:
            target = bucket
            break
    else:
        # Larger than all buckets — round up to next 256
        target = ((total_needed + 255) // 256) * 256

    pad_len = target - total_needed
    return payload_len.to_bytes(2, "big") + data + secrets.token_bytes(pad_len)


def _unpad_from_bucket(padded: bytes) -> bytes | None:
    """Strip padding, recovering the original payload."""
    if len(padded) < 2:
        return None
    payload_len = int.from_bytes(padded[:2], "big")
    if 2 + payload_len > len(padded):
        return None
    return padded[2 : 2 + payload_len]


# ── HMAC helpers ───────────────────────────────────────���───────────────────


def _compute_hmac(key: SigningKey, message: bytes, sig_bytes: int) -> bytes:
    """Compute truncated HMAC-SHA256 over message bytes.

    Routes through the central HMAC helper in `hyperdjango.native._crypto`
    which uses OpenSSL HMAC with hardware SHA-NI/NEON.
    """
    return hmac_sha256_bytes_truncated(key.secret.encode("utf-8"), message, sig_bytes)


def _verify_hmac(
    key: SigningKey, message: bytes, signature: bytes, sig_bytes: int
) -> bool:
    """Verify HMAC-SHA256 with constant-time comparison."""
    expected = _compute_hmac(key, message, sig_bytes)
    return hmac.compare_digest(expected, signature)


# ── TokenEngine ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TokenEngine:
    """Stateless token signing engine with key rotation and XOR obfuscation.

    Thread-safe: all mutable state is computed once in __post_init__.
    Compatible with Python 3.14t free-threading.

    Args:
        keys: Signing keys, newest first. Index 0 is used for encoding.
            All keys are tried during decoding for key rotation support.
        signature_bytes: HMAC truncation length (default 8 = 64 bits).
            Higher values increase security margin but lengthen tokens.
        salt_bytes: Random bytes prepended to each token payload (default 8).
            Makes every token unique even for identical inputs, and derives
            a per-token XOR mask so recovering one stream reveals nothing
            about others. Set to 0 to disable (deterministic tokens).
        pad_to_bucket: If True, pad payloads with random bytes to fixed-size
            buckets (16/32/64/128/256/512/1024/2048/4096). Hides exact
            payload size at the cost of longer tokens. Default False.
    """

    keys: list[SigningKey]
    signature_bytes: int = 8
    salt_bytes: int = _DEFAULT_SALT_BYTES
    pad_to_bucket: bool = False

    # Derived state (set in __post_init__)
    _key_by_version: dict[int, SigningKey] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("TokenEngine requires at least one SigningKey")

        if not 0 <= self.salt_bytes <= 32:
            raise ValueError(f"salt_bytes must be 0-32, got {self.salt_bytes}")

        # Validate no duplicate versions
        versions_seen: set[int] = set()
        for key in self.keys:
            if key.version in versions_seen:
                raise ValueError(f"Duplicate SigningKey version: {key.version}")
            versions_seen.add(key.version)

        # Build version → key lookup
        key_map: dict[int, SigningKey] = {}
        for key in self.keys:
            key_map[key.version] = key

        # TokenEngine is a non-frozen slots dataclass — assign the derived
        # lookup directly to its declared _key_by_version slot.
        self._key_by_version = key_map

    @property
    def _signing_key(self) -> SigningKey:
        """The newest key used for encoding new tokens."""
        return self.keys[0]

    # ── Encode ─────────────────────────────────────────────────────────

    def encode_ref(self, reference: str) -> str:
        """Sign an opaque reference string for DB lookups.

        The reference is XOR-obfuscated so it's not visible in the token,
        then HMAC-signed so it can't be forged.

        Args:
            reference: The raw reference string (e.g., session ID, key ID).

        Returns:
            Signed token string: ``{version}{type}{payload}.{signature}``
        """
        payload = reference.encode("utf-8")
        return self._encode(_TYPE_REF, payload)

    def encode_data(
        self,
        data: dict[str, str | int | float | bool | None],
        *,
        ttl: int | None = None,
    ) -> str:
        """Encode and sign arbitrary data into a stateless token.

        The data is JSON-serialized, zlib-compressed, XOR-obfuscated,
        and HMAC-signed. Any server with the same keys can verify and
        decode without a database.

        Args:
            data: Key-value data to encode. Values must be JSON-serializable
                primitives (str, int, float, bool, None).
            ttl: Optional time-to-live in seconds. If set, an ``_exp``
                timestamp is injected and checked on decode.

        Returns:
            Signed token string.
        """
        # `_exp` is reserved for the TTL machinery. If a caller could smuggle it
        # in via `data`, a non-numeric value would crash decode_data's expiry
        # comparison (TypeError) and a numeric one would be silently stripped as
        # an expiry (data loss / spurious expiry). Reject it loudly at encode
        # time — use the `ttl=` parameter to set expiration.
        if "_exp" in data:
            raise ValueError(
                "'_exp' is a reserved field name; pass ttl= to set expiration "
                "instead of putting '_exp' in the data dict"
            )
        token_data = dict(data)
        if ttl is not None:
            token_data["_exp"] = int(time.time()) + ttl

        # Randomize key order when salt is active — breaks structural
        # correlation across data tokens (observer can't match field
        # positions even if they know the schema)
        if self.salt_bytes > 0:
            keys_list = list(token_data.keys())
            random.shuffle(keys_list)
            token_data = {k: token_data[k] for k in keys_list}
            raw = json.dumps(token_data, separators=(",", ":")).encode("utf-8")
        else:
            raw = json.dumps(token_data, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )

        if len(raw) > _MAX_DATA_BYTES:
            raise ValueError(
                f"Data token payload too large: {len(raw)} bytes "
                f"(max {_MAX_DATA_BYTES})"
            )

        compressed = zlib.compress(raw, level=6)
        return self._encode(_TYPE_DATA, compressed)

    def _encode(self, type_char: str, payload: bytes) -> str:
        """Internal: salt, pad, XOR, base62-encode, sign, and format a token.

        Wire format of the XOR'd blob (before base62):
            [salt (N bytes)] [payload (M bytes)] [padding (P bytes)]

        Salt is prepended, padding is appended. Both are stripped on decode.
        The XOR mask is derived from key + salt, so each token has a unique
        stream. Padding (when enabled) rounds the total to a fixed bucket
        size so token length doesn't leak payload size.
        """
        key = self._signing_key
        version_char = _VERSION_CHARS[key.version]

        # Generate per-token random salt
        salt = secrets.token_bytes(self.salt_bytes) if self.salt_bytes > 0 else b""

        # Derive masks:
        # - Static mask (from key only) — used to obfuscate the salt itself
        # - Per-token mask (from key + salt) — used for the payload
        # This lets decode recover the salt first (using static mask),
        # then derive the per-token mask to decode the rest.
        static_mask = _static_xor_mask(key)
        token_mask = _derive_xor_mask(key, salt) if salt else static_mask

        # Obfuscate salt with static mask, payload with per-token mask
        salt_obf = _xor_with_mask(salt, static_mask) if salt else b""

        # Build payload portion (with optional padding)
        payload_portion = _pad_to_bucket(payload) if self.pad_to_bucket else payload

        payload_obf = _xor_with_mask(payload_portion, token_mask)

        # Concatenate: obfuscated salt + obfuscated payload
        obfuscated = salt_obf + payload_obf

        # Base62-encode with 0x01 sentinel (preserves leading zeros)
        b62_payload = _bytes_to_base62(obfuscated)

        # Build the prefix (everything before the separator)
        prefix = f"{version_char}{type_char}{b62_payload}"

        # Compute truncated HMAC over the prefix
        sig = _compute_hmac(key, prefix.encode("utf-8"), self.signature_bytes)
        b62_sig = _bytes_to_base62(sig)

        return f"{prefix}{_SEP}{b62_sig}"

    # ── Decode ─────────────────────────────────────────────────────────

    def decode_ref(self, token: str) -> str | None:
        """Verify and decode a reference token.

        Returns the original reference string, or None if the token is
        invalid (bad signature, wrong type, corrupted).

        Args:
            token: The signed token string.

        Returns:
            Original reference string, or None.
        """
        result = self._decode(token, _TYPE_REF)
        if result is None:
            return None
        try:
            return result.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def decode_data(
        self, token: str
    ) -> dict[str, str | int | float | bool | None] | None:
        """Verify and decode a data token.

        Returns the original data dict, or None if the token is invalid,
        corrupted, or expired (past TTL).

        Args:
            token: The signed token string.

        Returns:
            Original data dict, or None.
        """
        result = self._decode(token, _TYPE_DATA)
        if result is None:
            return None

        try:
            raw = zlib.decompress(result)
            data = json.loads(raw)
        except zlib.error, json.JSONDecodeError, UnicodeDecodeError:
            return None

        # Defense-in-depth: the payload is HMAC-gated so an attacker can't reach
        # here with a non-dict, but a JSON scalar/list (e.g. an old token that
        # encoded a bare value) would make data.get("_exp") raise. Reject it.
        if not isinstance(data, dict):
            return None

        # Check expiration. `_exp` is written only by encode_data as an int (the
        # caller is barred from supplying it), but decode is fail-closed by
        # discipline: a present-but-non-numeric `_exp` cannot be proven unexpired,
        # so reject rather than crash on the comparison (mirrors the API-key path).
        exp = data.get("_exp")
        if exp is not None:
            if not isinstance(exp, (int, float)) or isinstance(exp, bool):
                return None
            if int(time.time()) > exp:
                return None
            # Remove internal field from returned data
            del data["_exp"]

        return data

    def _decode(self, token: str, expected_type: str) -> bytes | None:
        """Internal: verify HMAC, XOR-decode, return raw payload bytes."""
        # Reject over-long tokens BEFORE the O(N²) base62 decode of the
        # signature (which runs pre-HMAC). A legitimate token is a small salted
        # payload + an 8-byte signature (~a dozen base62 chars); a 64 KB junk
        # token would otherwise burn hundreds of ms of unauthenticated CPU.
        # _MAX_DATA_BYTES bounds the plaintext; base62 inflates ~1.4×, so this
        # is a generous ceiling that never rejects a well-formed token.
        if len(token) > _MAX_TOKEN_CHARS:
            return None
        # Split into prefix and signature
        sep_idx = token.rfind(_SEP)
        if sep_idx < 3:
            # Minimum prefix: version(1) + type(1) + payload(1+)
            return None

        prefix = token[:sep_idx]
        b62_sig = token[sep_idx + 1 :]

        if not b62_sig or len(b62_sig) > _MAX_SIG_CHARS:
            # An 8-byte HMAC is ~12 base62 chars; anything past _MAX_SIG_CHARS
            # is malformed and must be rejected before _base62_to_bytes runs.
            return None

        # Read version and type from prefix
        version_char = prefix[0]
        type_char = prefix[1]

        if type_char != expected_type:
            return None

        # Look up version
        version_idx = _VERSION_CHARS.find(version_char)
        if version_idx < 0:
            return None

        # Decode the signature bytes
        try:
            sig = _base62_to_bytes(b62_sig)
        except ValueError, OverflowError:
            return None

        prefix_bytes = prefix.encode("utf-8")

        # The version char names exactly one key (O(1) lookup). A genuine token
        # always carries the version of the key that signed it.
        matched_key = self._key_by_version.get(version_idx)
        if matched_key is not None:
            # If THIS key's HMAC verifies, accept; otherwise the token is
            # forged/corrupt. Do NOT fall back to trying every other key —
            # that only burns HMAC work and can't succeed for a well-formed
            # version char.
            if _verify_hmac(matched_key, prefix_bytes, sig, self.signature_bytes):
                return self._extract_payload(prefix, matched_key)
            return None

        # No configured key matches this version char (e.g. the version was
        # retired). Fall back to trying all keys as a rotation safety net.
        for key in self.keys:
            if _verify_hmac(key, prefix_bytes, sig, self.signature_bytes):
                return self._extract_payload(prefix, key)

        return None

    def _extract_payload(self, prefix: str, key: SigningKey) -> bytes | None:
        """Extract, XOR-decode, strip salt + padding from a verified prefix."""
        b62_payload = prefix[2:]  # Skip version + type chars

        try:
            # Base62 → bytes (0x01 sentinel preserves exact byte length)
            obfuscated = _base62_to_bytes(b62_payload)
        except ValueError, OverflowError:
            return None

        salt_len = self.salt_bytes
        if len(obfuscated) < salt_len:
            return None

        # Step 1: Recover salt using static mask (key-only, no salt)
        static_mask = _static_xor_mask(key)
        if salt_len > 0:
            salt = _xor_with_mask(obfuscated[:salt_len], static_mask)
            token_mask = _derive_xor_mask(key, salt)
        else:
            salt = b""
            token_mask = static_mask

        # Step 2: Decode payload portion using per-token mask
        payload_obf = obfuscated[salt_len:]
        payload_portion = _xor_with_mask(payload_obf, token_mask)

        # Step 3: Strip padding if enabled
        if self.pad_to_bucket:
            return _unpad_from_bucket(payload_portion)

        return payload_portion

    # ── Utility ────────────────────────────────────────────────────────

    def is_valid_ref(self, token: str) -> bool:
        """Check if a token is a valid reference token (without decoding)."""
        return self.decode_ref(token) is not None

    def is_valid_data(self, token: str) -> bool:
        """Check if a token is a valid, non-expired data token."""
        return self.decode_data(token) is not None


# ── Model Mixins ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class APIKeyResult:
    """Result of SignedAPIKeyMixin.generate().

    Attributes:
        instance: The saved model instance (key_hash, key_prefix set).
        raw_key: The full signed key string. Show once to the user,
            then discard — it cannot be recovered.
    """

    instance: object
    raw_key: str


class SignedSessionMixin(_Model):
    """Model mixin for session models with signed tokens.

    Adds a ``token`` field (unique, indexed) that stores the raw session
    reference in the database. The signed version (for cookies) is produced
    by the class-level TokenEngine.

    Subclasses must define a ``TokenConfig`` inner class:

        class MySession(SignedSessionMixin, TimestampMixin, Model):
            class TokenConfig:
                keys = [SigningKey(secret="sess-key-2026", version=1)]

            user_id: int = Field()

    The mixin provides:
    - ``token`` field auto-generated on first save
    - ``signed_token`` property for cookie values
    - ``from_signed_token()`` classmethod for DB lookup
    """

    class Meta:
        abstract = True

    _token_engine: ClassVar[TokenEngine]

    # Raw token stored in DB — the reference that gets signed for cookies
    token: str | None = _ModelField(
        default=None,
        unique=True,
        index=True,
    )

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        config = cls.__dict__.get("TokenConfig")
        if config is None:
            # Abstract or intermediate class — defer
            if "_token_engine" in cls.__dict__ or any(
                "_token_engine" in b.__dict__ for b in cls.__mro__[1:]
            ):
                return
            return

        config_dict = config.__dict__
        keys = config_dict.get("keys")
        if not keys:
            raise ValueError(
                f"{cls.__name__}.TokenConfig.keys is required — "
                f"provide at least one SigningKey"
            )

        sig_bytes = config_dict.get("signature_bytes", 8)
        salt_bytes = config_dict.get("salt_bytes", _DEFAULT_SALT_BYTES)
        pad = config_dict.get("pad_to_bucket", False)
        cls._token_engine = TokenEngine(
            keys=list(keys),
            signature_bytes=sig_bytes,
            salt_bytes=salt_bytes,
            pad_to_bucket=pad,
        )

    @staticmethod
    def _needs_token(current_value: str | None) -> bool:
        """Check if token needs generation."""
        return (
            current_value is None
            or current_value == ""
            or isinstance(current_value, _FieldInfo)
        )

    async def save(self, db=None, *, _using=None):
        """Save with automatic token generation on first insert.

        ``_using`` mirrors the base ``Model.save`` signature so a QuerySet that
        constructs-then-saves (``create``/``get_or_create``/``update_or_create``
        forward ``_using=self._using``) keeps its bound connection; dropping it
        here made ``get_or_create`` raise on any signed-session model. It is
        forwarded down the save chain.
        """
        if self._needs_token(self.token):
            self.token = secrets.token_urlsafe(32)
        return await super().save(db=db, _using=_using)

    @property
    def signed_token(self) -> str:
        """The HMAC-signed token string for use in cookies.

        The raw ``self.token`` is XOR-obfuscated and signed. Safe to
        expose to clients — cannot be forged or read without the key.
        """
        if self._needs_token(self.token):
            raise ValueError("Cannot sign token before save()")
        return self._token_engine.encode_ref(self.token)

    @classmethod
    def decode_signed_token(cls, signed: str) -> str | None:
        """Decode a signed token to the raw reference.

        Returns the raw token string for DB lookup, or None if the
        signature is invalid.

        This does NOT hit the database — it only verifies the HMAC.
        Use the returned token to query the session table.
        """
        return cls._token_engine.decode_ref(signed)

    @classmethod
    async def from_signed_token(cls, signed: str):
        """Verify a signed token and look up the session in the database.

        Returns the session instance, or None if the signature is invalid
        or the session doesn't exist.
        """
        raw_token = cls._token_engine.decode_ref(signed)
        if raw_token is None:
            return None
        return await cls.objects.filter(token=raw_token).first()


class SignedAPIKeyMixin(_Model):
    """Model mixin for API key models with signed tokens.

    Generates signed API keys that can be verified without a database hit
    (HMAC check rejects forgeries instantly). Only valid keys proceed to
    hash lookup in the database.

    Subclasses must define a ``TokenConfig`` inner class:

        class APIKey(SignedAPIKeyMixin, TimestampMixin, Model):
            class TokenConfig:
                keys = [SigningKey(secret="key-2026-q2", version=1)]
                key_display_prefix = "sk_myapp_"

            user_id: int = Field()
            name: str = Field(default="")

    The mixin provides:
    - Fields: key_hash, key_prefix, is_active, expires_at, scopes
    - ``generate()`` classmethod → (instance, raw_key)
    - ``verify()`` classmethod → instance or None
    """

    class Meta:
        abstract = True

    _token_engine: ClassVar[TokenEngine]
    _key_display_prefix: ClassVar[str]

    # Fields added to the model
    key_hash: str = _ModelField(unique=True, index=True)
    key_prefix: str = _ModelField(default="")
    is_active: bool = _ModelField(default=True)
    expires_at: str | None = _ModelField(default=None)
    scopes: str = _ModelField(default="*")

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        config = cls.__dict__.get("TokenConfig")
        if config is None:
            if "_token_engine" in cls.__dict__ or any(
                "_token_engine" in b.__dict__ for b in cls.__mro__[1:]
            ):
                return
            return

        config_dict = config.__dict__
        keys = config_dict.get("keys")
        if not keys:
            raise ValueError(
                f"{cls.__name__}.TokenConfig.keys is required — "
                f"provide at least one SigningKey"
            )

        sig_bytes = config_dict.get("signature_bytes", 8)
        salt_bytes = config_dict.get("salt_bytes", _DEFAULT_SALT_BYTES)
        pad = config_dict.get("pad_to_bucket", False)
        cls._token_engine = TokenEngine(
            keys=list(keys),
            signature_bytes=sig_bytes,
            salt_bytes=salt_bytes,
            pad_to_bucket=pad,
        )
        cls._key_display_prefix = config_dict.get("key_display_prefix", "sk_")

    @classmethod
    async def generate(cls, **model_kwargs) -> APIKeyResult:
        """Generate a new signed API key.

        Creates a cryptographically random reference, signs it with the
        TokenEngine, hashes it for storage, and saves the model.

        The raw key is returned in ``APIKeyResult.raw_key`` — show it
        once to the user. It cannot be recovered after this call.

        Args:
            db: Optional database connection.
            **model_kwargs: Additional fields for the model (e.g., user_id, name).

        Returns:
            APIKeyResult with the saved instance and the raw key string.
        """
        # Generate random reference
        reference = secrets.token_urlsafe(32)

        # Hash for storage (SHA-256 — fast, appropriate for high-entropy secrets)
        reference_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()

        # Sign the reference for the user-facing key
        signed = cls._token_engine.encode_ref(reference)
        raw_key = f"{cls._key_display_prefix}{signed}"

        # Build and save the model
        instance = cls(
            key_hash=reference_hash,
            key_prefix=raw_key[:16],
            **model_kwargs,
        )
        await instance.save()

        return APIKeyResult(instance=instance, raw_key=raw_key)

    @classmethod
    async def verify(cls, raw_key: str):
        """Verify an API key and return the model instance.

        Two-phase verification:
        1. Strip display prefix, verify HMAC signature (no DB hit —
           rejects forgeries instantly)
        2. Hash the reference, look up by key_hash in DB
        3. Check is_active and expires_at

        Returns:
            The API key model instance, or None if invalid/inactive/expired.
        """
        # Strip display prefix
        prefix = cls._key_display_prefix
        if not raw_key.startswith(prefix):
            return None
        signed = raw_key[len(prefix) :]

        # Phase 1: Verify HMAC (no DB hit)
        reference = cls._token_engine.decode_ref(signed)
        if reference is None:
            return None

        # Phase 2: Hash and DB lookup
        reference_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        instance = await cls.objects.filter(
            key_hash=reference_hash, is_active=True
        ).first()

        if instance is None:
            return None

        # Check expiration
        if instance.expires_at is not None and instance.expires_at != "":
            from datetime import UTC, datetime

            raw_exp = instance.expires_at
            try:
                # expires_at is declared str, but a backend may hand back a
                # datetime object — accept both without re-parsing a datetime.
                exp = (
                    raw_exp
                    if isinstance(raw_exp, datetime)
                    else datetime.fromisoformat(raw_exp)
                )
            except ValueError, TypeError:
                # FAIL CLOSED: an unparseable expires_at must NOT be treated as
                # "never expires". A corrupt/garbage expiry means we can't prove
                # the key is still valid — reject it.
                return None
            # Normalize to timezone-aware UTC. A naive stored value (an ISO
            # string with no offset, or a naive datetime) is treated as UTC so
            # the comparison below can never raise TypeError on an
            # offset-naive vs offset-aware mismatch (uncaught → 500).
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if datetime.now(UTC) > exp:
                return None

        return instance

    @classmethod
    def verify_signature_only(cls, raw_key: str) -> bool:
        """Check if a key has a valid HMAC signature without DB lookup.

        Useful for fast rejection of forged keys at the middleware layer.
        """
        prefix = cls._key_display_prefix
        if not raw_key.startswith(prefix):
            return False
        signed = raw_key[len(prefix) :]
        return cls._token_engine.decode_ref(signed) is not None


# ── Exports ─────────────────────────────────────────���──────────────────────

__all__ = [
    "SigningKey",
    "TokenEngine",
    "SignedSessionMixin",
    "SignedAPIKeyMixin",
    "APIKeyResult",
]
