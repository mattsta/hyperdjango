"""
Public ID system — opaque, non-sequential identifiers for API-safe object references.

SECURITY POSITIONING — read before relying on this:
  Public IDs are DEFENSE-IN-DEPTH, NOT an access-control mechanism. They are
  NOT a substitute for an authorization check and DO NOT by themselves prevent
  IDOR/BOLA. IDOR is an *authorization* flaw; the fix is verifying the requester
  is allowed to access the object (ownership / tenancy / object permissions —
  see hyperdjango.guard, mixins.OwnershipMixin, tenancy). An endpoint that looks
  a record up by public_id but skips that check is still vulnerable — an attacker
  who obtains ONE valid public ID (shared link, referrer, prior response) can use
  it, and `encode(pk)` is a reversible base-N transform (a determined attacker who
  observes several IDs can recover the mapping — like Hashids/Sqids, it is
  obfuscation, not encryption). What public IDs DO buy you: no trivial
  ``/users/1,2,3`` sequential enumeration, and no leaking of record counts /
  growth rate. For unguessable references, prefer ``encode_random`` (a stored,
  high-entropy random id), which resists guessing — but STILL gate every access
  on authorization.

Provides arbitrary-base encoding with user-defined alphabets, model mixins for
automatic public ID generation, and serializer integration.

Design:
- Integer PKs remain internal (fast joins, compact indexes)
- Public IDs are opaque strings generated per-model with user-defined alphabets
- Each model defines its own alphabet (a random permutation of a base charset)
- Alphabets are validated once at class creation, bound to the model forever

Usage:
    from hyperdjango.public_id import BaseEncoder, generate_alphabet

    # Step 1: Generate an alphabet (one-time, copy into code)
    print(generate_alphabet("olc32"))   # "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
    print(generate_alphabet("base62"))  # "tR4kL9xZ..."

    # Step 2: Create encoder with your alphabet
    encoder = BaseEncoder("W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4")
    encoded = encoder.encode(12345)          # "cX9"
    decoded = encoder.decode("cX9")          # 12345
    random_id = encoder.encode_random(8)     # "Xf7RgW3pMc" (10 chars, 40 bits entropy)
    padded = encoder.encode_padded(42, 8)    # "44444443J" (fixed 8 chars)

    # Step 3: Use with models via PublicIDMixin (see mixins section below)
"""

import hmac
import math
import random
import secrets
import string
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Final

from hyperdjango.native import base_decode as _native_base_decode
from hyperdjango.native import base_encode as _native_base_encode
from hyperdjango.native._crypto import hmac_sha256_bytes_truncated
from hyperdjango.validation.core import Field as _CoreField
from hyperdjango.validation.core.fields import FieldInfo as _FieldInfo

# ── Base character sets ─────────────────────────────────────────────────────
# These define WHICH characters are safe to use. Users generate random
# permutations of these for their models. Never used directly as alphabets.

# 32 chars: no vowels (can't spell words), no confusables (0/O, 1/l/I)
# From Google Open Location Code charset + case sensitivity
OLC_SAFE_CHARS: Final[str] = "23456789cfghjmpqrvwxCFGHJMPQRVWX"

# 62 chars: full alphanumeric [0-9a-zA-Z]
ALPHANUMERIC_CHARS: Final[str] = (
    string.digits + string.ascii_lowercase + string.ascii_uppercase
)

# Decimal — used internally for base conversion
_DECIMAL: Final[str] = "0123456789"


class IDMode(StrEnum):
    """External ID representation modes."""

    RAW = "raw"
    ENCODED = "encoded"
    SIGNED = "signed"
    RANDOM = "random"


class IDStrategy(StrEnum):
    """PublicIDMixin generation strategies."""

    RANDOM = "random"
    UUID7 = "uuid7"
    ENCODED_PK = "encoded_pk"


# Frozenset constants for membership checks
_ENCODING_MODES: Final[frozenset[IDMode]] = frozenset({IDMode.ENCODED, IDMode.SIGNED})
_DB_COLUMN_MODES: Final[frozenset[IDMode]] = frozenset({IDMode.RANDOM})
_VALID_STRATEGIES: Final[frozenset[IDStrategy]] = frozenset(
    {IDStrategy.RANDOM, IDStrategy.UUID7, IDStrategy.ENCODED_PK}
)

# ── Alphabet generation ────────────────────────────────────────────────────


def generate_alphabet(charset: str = "olc32", *, seed: int | None = None) -> str:
    """Generate a random permutation of a base character set.

    Call this ONCE when setting up a new model. Copy the output string
    into your model's PublicIDConfig. Never call at runtime.

    Args:
        charset: "olc32" (32-char no-confusables) or "base62" (full alphanumeric).
        seed: Optional seed for reproducible output (testing only).

    Returns:
        Randomly permuted alphabet string — paste into your code as a constant.

    Example:
        $ python -c "from hyperdjango.public_id import generate_alphabet; print(generate_alphabet())"
        "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"

        # Then in your model:
        class Article(PublicIDMixin, Model):
            class PublicIDConfig:
                alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
    """
    if charset == "olc32":
        chars = list(OLC_SAFE_CHARS)
    elif charset == "base62":
        chars = list(ALPHANUMERIC_CHARS)
    else:
        raise ValueError(f"Unknown charset: {charset!r}. Use 'olc32' or 'base62'.")

    rng = random.Random(seed)
    rng.shuffle(chars)
    return "".join(chars)


def validate_alphabet(alphabet: str) -> None:
    """Validate an alphabet string. Raises ValueError on any problem.

    Called automatically by BaseEncoder.__init__ and PublicIDMixin.__init_subclass__.
    """
    if not isinstance(alphabet, str):
        raise TypeError(f"Alphabet must be a string, got {type(alphabet).__name__}")
    if len(alphabet) < 2:
        raise ValueError(
            f"Alphabet must have at least 2 characters, got {len(alphabet)}"
        )
    if len(alphabet) != len(set(alphabet)):
        seen: set[str] = set()
        for ch in alphabet:
            if ch in seen:
                raise ValueError(f"Alphabet has duplicate character: {ch!r}")
            seen.add(ch)


# ── BaseEncoder ─────────────────────────────────────────────────────────────


@dataclass(slots=True, init=False)
class BaseEncoder:
    """Arbitrary-base encoder bound to a specific alphabet.

    The encoder is created with an alphabet and validates it once at init.
    All encode/decode operations use this alphabet — it's a sealed unit.

    Bits per character by base size:
        base-32: 5.00 bits/char
        base-62: 5.95 bits/char

    Width reference (characters needed for N bits of entropy):
        base-32:  6 chars → 30 bits (1B values)
                  8 chars → 40 bits (1T values)
                 10 chars → 50 bits
                 13 chars → 65 bits (covers BIGSERIAL max)
        base-62:  6 chars → 35 bits
                  8 chars → 47 bits
                 11 chars → 65 bits (covers BIGSERIAL max)
                 22 chars → 130 bits (UUID-equivalent)
    """

    alphabet: str
    base: int
    _lookup: dict[str, int]

    def __init__(self, alphabet: str) -> None:
        validate_alphabet(alphabet)
        self.alphabet = alphabet
        self.base = len(alphabet)
        self._lookup = {c: i for i, c in enumerate(alphabet)}

    def encode(self, value: int) -> str:
        """Encode a non-negative integer to a string in this alphabet's base.

        Uses native Zig acceleration for the core conversion.

        Args:
            value: Non-negative integer to encode.

        Returns:
            Encoded string. Single character for value 0.
        """
        return _native_base_encode(value, self.alphabet)

    def decode(self, code: str) -> int:
        """Decode a string back to an integer using this alphabet.

        Uses native Zig acceleration for the core conversion.

        Args:
            code: Encoded string (must only contain characters from this alphabet).

        Returns:
            Decoded integer value.
        """
        return _native_base_decode(code, self.alphabet)

    def encode_padded(self, value: int, width: int) -> str:
        """Encode with left-padding to a fixed width.

        Useful for consistent-length IDs (e.g., always 8 characters).

        Args:
            value: Non-negative integer to encode.
            width: Minimum output length. Padded with alphabet[0] on the left.

        Returns:
            Encoded string of at least `width` characters.
        """
        encoded = self.encode(value)
        if len(encoded) >= width:
            return encoded
        pad_char = self.alphabet[0]
        return pad_char * (width - len(encoded)) + encoded

    def encode_random(self, entropy_bytes: int) -> str:
        """Generate a random encoded string with the given entropy.

        Args:
            entropy_bytes: Number of random bytes to use. More bytes = longer string.
                8 bytes → ~40 bits for base-32, ~47 bits for base-62.

        Returns:
            Random encoded string.
        """
        raw = int.from_bytes(secrets.token_bytes(entropy_bytes), "big")
        return self.encode(raw)

    def encode_bytes(self, data: bytes) -> str:
        """Encode arbitrary bytes to a string.

        Args:
            data: Bytes to encode (interpreted as big-endian unsigned integer).

        Returns:
            Encoded string.
        """
        if not data:
            return self.alphabet[0]
        value = int.from_bytes(data, "big")
        return self.encode(value)

    def decode_to_bytes(self, code: str, length: int) -> bytes:
        """Decode a string back to bytes of a specific length.

        Args:
            code: Encoded string.
            length: Expected byte length of output.

        Returns:
            Decoded bytes, zero-padded to `length`.
        """
        value = self.decode(code)
        return value.to_bytes(length, "big")

    def encode_packed(self, values: list[int | bytes], bits_per_value: int) -> str:
        """Encode multiple integers into a single string.

        Each value is allocated `bits_per_value` bits of space in the packed
        representation. Values are packed left-to-right.

        Args:
            values: List of integers or bytes to pack.
            bits_per_value: Bit width allocated per value (e.g., 128, 256).

        Returns:
            Single encoded string representing all values.
        """
        max_val = 1 << bits_per_value
        digits_per_value = len(str(max_val - 1))
        divisor = 10**digits_per_value

        giant = 0
        multiplier = 1
        for v in values:
            if isinstance(v, bytes):
                v = int.from_bytes(v, "big")
            if v >= max_val:
                raise ValueError(f"Value {v} exceeds {bits_per_value}-bit maximum")
            giant += v * multiplier
            multiplier *= divisor

        return self.encode(giant)

    def decode_packed(self, code: str, bits_per_value: int, count: int) -> list[int]:
        """Decode a packed string back to multiple integers.

        Args:
            code: Packed encoded string.
            bits_per_value: Bit width per value (must match what was used to encode).
            count: Number of values to extract.

        Returns:
            List of decoded integers.
        """
        max_val = 1 << bits_per_value
        digits_per_value = len(str(max_val - 1))
        divisor = 10**digits_per_value

        big_int = self.decode(code)
        result: list[int] = []
        for _ in range(count):
            result.append(big_int % divisor)
            big_int //= divisor
        return result

    def max_value_for_width(self, width: int) -> int:
        """Calculate the maximum integer value encodable in `width` characters.

        Useful for understanding the capacity of a given width.
        """
        return self.base**width - 1

    def width_for_bits(self, bits: int) -> int:
        """Calculate minimum characters needed to represent `bits` bits of entropy.

        Args:
            bits: Number of bits of entropy needed.

        Returns:
            Minimum number of characters in this alphabet's base.
        """
        bits_per_char = math.log2(self.base)
        return math.ceil(bits / bits_per_char)

    def __repr__(self) -> str:
        return f"BaseEncoder(base={self.base}, alphabet={self.alphabet!r})"


# ── Conversion utility (standalone) ─────────────────────────────────────────


def base_convert(
    value: int | str | bytes,
    src_alphabet: str = "",
    dst_alphabet: str = "",
) -> str:
    """Convert a value between arbitrary bases.

    This is the standalone functional API — for repeated conversions,
    use BaseEncoder which pre-computes the lookup table.

    Args:
        value: Integer, string (in src_alphabet's base), or bytes.
        src_alphabet: Alphabet of the input string. Not needed if value is int.
        dst_alphabet: Alphabet for the output string.

    Returns:
        String representation of value in dst_alphabet's base.
    """
    dst_base = len(dst_alphabet)

    if isinstance(value, bytes):
        x = int.from_bytes(value, "big")
    elif isinstance(value, int):
        x = value
    else:
        src_base = len(src_alphabet)
        src_lookup = {c: i for i, c in enumerate(src_alphabet)}
        x = 0
        for digit in value:
            idx = src_lookup.get(digit)
            if idx is None:
                raise ValueError(f"Invalid digit '{digit}' not in source alphabet")
            x = x * src_base + idx

    if x == 0:
        return dst_alphabet[0]

    parts: list[str] = []
    while x > 0:
        parts.append(dst_alphabet[x % dst_base])
        x //= dst_base

    parts.reverse()
    return "".join(parts)


# ── PublicIDMixin ───────────────────────────────────────────────────────────


class PublicIDConfig:
    """Configuration for PublicIDMixin. Override in your model class.

    Attributes:
        alphabet: Required. Your model's unique alphabet permutation.
        strategy: "random" (default), "uuid7", or "encoded_pk".
        entropy_bytes: Bytes of randomness for "random" strategy.
        width: Fixed output width (0 = no padding).
    """

    alphabet: str = ""  # REQUIRED — must be set by user
    strategy: IDStrategy = IDStrategy.RANDOM
    entropy_bytes: int = 10  # for "random" strategy
    width: int = 0  # pad to fixed width (0 = variable)


class PublicIDMixin:
    """Model mixin that adds an opaque public_id field.

    Subclasses MUST define a PublicIDConfig inner class with at least
    an `alphabet` attribute. The alphabet is validated at class creation
    time — if it's invalid, you get an immediate error at import.

    The mixin:
    - Declares a `public_id` field (str, unique, indexed, max_length=64)
    - Overrides save() to auto-generate public_id on first insert
    - Provides get_by_public_id() and filter_by_public_ids() class methods
    - For 'encoded_pk' strategy, public_id is set after INSERT (needs PK)

    Usage:
        from hyperdjango.public_id import PublicIDMixin, generate_alphabet
        from hyperdjango.models import Model, Field

        class Article(PublicIDMixin, Model):
            class Meta:
                table = "articles"

            class PublicIDConfig:
                alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"  # from generate_alphabet()
                strategy = IDStrategy.RANDOM
                entropy_bytes = 8

            id: int = Field(primary_key=True, auto=True)
            title: str = Field()

        # On create, public_id is auto-generated:
        article = Article(title="Hello")
        await article.save()
        article.public_id  # "Xf7RgW3pMc"

        # Lookup by public_id:
        article = await Article.get_by_public_id("Xf7RgW3pMc")
    """

    # Class-level attributes set by __init_subclass__
    _public_id_encoder: BaseEncoder
    _public_id_strategy: IDStrategy
    _public_id_entropy_bytes: int
    _public_id_width: int

    # The public_id field — declared here so ModelMeta picks it up from
    # abstract parent field inheritance. Concrete models inherit this field.
    public_id: str | None = _CoreField(
        default=None,
        unique=True,
        index=True,
    )

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        # Look for PublicIDConfig on this class (not inherited)
        config = cls.__dict__.get("PublicIDConfig")
        if config is None:
            # Check if a parent already configured it — skip re-validation
            if "_public_id_encoder" in cls.__dict__ or any(
                # every class object carries a ``__dict__`` mappingproxy — direct access is total
                "_public_id_encoder" in b.__dict__
                for b in cls.__mro__[1:]
            ):
                return
            # Abstract mixin usage or intermediate class — defer
            return

        # Extract config values — access class attributes directly
        # (PublicIDConfig is a user-defined inner class with known attributes)
        alphabet: str = config.__dict__.get("alphabet", "")
        raw_strategy = config.__dict__.get("strategy", IDStrategy.RANDOM)
        strategy: IDStrategy = IDStrategy(raw_strategy)
        entropy_bytes: int = config.__dict__.get("entropy_bytes", 10)
        width: int = config.__dict__.get("width", 0)

        # Validate strategy
        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"Invalid PublicIDConfig.strategy: {strategy!r}. "
                f"Must be 'random', 'uuid7', or 'encoded_pk'."
            )

        # For uuid7, alphabet is optional (not used for generation)
        if strategy != IDStrategy.UUID7:
            if not alphabet:
                raise ValueError(
                    f"PublicIDConfig.alphabet is required for strategy '{strategy}'. "
                    f"Generate one with: from hyperdjango.public_id import generate_alphabet; "
                    f"print(generate_alphabet())"
                )
            validate_alphabet(alphabet)
            cls._public_id_encoder = BaseEncoder(alphabet)
        else:
            # uuid7 doesn't need an encoder, but we set a dummy for consistency
            # If alphabet is provided, we can still encode the UUID bytes
            if alphabet:
                validate_alphabet(alphabet)
                cls._public_id_encoder = BaseEncoder(alphabet)
            else:
                cls._public_id_encoder = None  # type: ignore[assignment]

        cls._public_id_strategy = strategy
        cls._public_id_entropy_bytes = entropy_bytes
        cls._public_id_width = width

    @staticmethod
    def _needs_public_id(current_value: str | None) -> bool:
        """Check if a public_id value is unset and needs generation."""
        return (
            current_value is None
            or current_value == ""
            or isinstance(current_value, _FieldInfo)
        )

    async def save(self, db=None, *, _using=None):
        """Save with automatic public_id generation on first insert.

        For 'random' and 'uuid7' strategies: public_id is set before INSERT.
        For 'encoded_pk' strategy: public_id is set after INSERT (needs PK),
        then a follow-up UPDATE writes the public_id to the row.

        ``_using`` mirrors the base ``Model.save`` signature so a QuerySet that
        constructs-then-saves (``create``/``get_or_create``/``update_or_create``
        forward ``_using=self._using``) keeps its bound connection; dropping it
        here made ``get_or_create`` raise on any public-id model. It is forwarded
        to ``super().save`` and honored by the encoded_pk follow-up UPDATE so both
        writes land on the same connection.
        """
        is_new = not self._loaded_from_db  # type: ignore[attr-defined]

        if is_new and self._needs_public_id(self.public_id):
            if self._public_id_strategy != IDStrategy.ENCODED_PK:
                self.public_id = self.generate_public_id()

        result = await super().save(db=db, _using=_using)  # type: ignore[misc]

        # For encoded_pk: generate after INSERT (now we have the PK)
        if is_new and self._public_id_strategy == IDStrategy.ENCODED_PK:
            if self._needs_public_id(self.public_id):
                self.public_id = self.generate_public_id()
                if db is None:
                    if _using is not None:
                        from hyperdjango.multi_db import get_connections

                        db = (
                            get_connections()[_using]
                            if isinstance(_using, str)
                            else _using
                        )
                    else:
                        # Resolve the same write connection ``super().save`` used
                        # for the INSERT (Meta.database / router), so the
                        # follow-up public_id UPDATE lands on the row we just
                        # inserted — not the global default, where it would be a
                        # silent no-op leaving public_id NULL for a routed model.
                        from hyperdjango.models import _resolve_instance_db

                        db = _resolve_instance_db(type(self), for_write=True)
                meta = self._meta  # type: ignore[attr-defined]
                pk_vals = self.pk_values  # type: ignore[attr-defined]
                where = meta.pk_where_clause(start_param=2)
                await db.execute(
                    f"UPDATE {meta.table} SET public_id = $1 WHERE {where}",
                    self.public_id,
                    *pk_vals,
                )

        return result

    def generate_public_id(self) -> str:
        """Generate a public ID based on this model's strategy.

        Called automatically during save() if public_id is not set.
        Can also be called manually.
        """
        strategy = self._public_id_strategy
        encoder = self._public_id_encoder
        width = self._public_id_width

        if strategy == IDStrategy.UUID7:
            # uuid7 gives time-ordered UUIDs (better index locality than uuid4).
            return str(uuid.uuid7())

        if strategy == IDStrategy.RANDOM:
            raw = int.from_bytes(
                secrets.token_bytes(self._public_id_entropy_bytes), "big"
            )
            if width > 0:
                return encoder.encode_padded(raw, width)
            return encoder.encode(raw)

        if strategy == IDStrategy.ENCODED_PK:
            # Encode the integer primary key
            pk = self.pk  # type: ignore[attr-defined]
            if pk is None:
                raise ValueError(
                    "Cannot generate encoded_pk public_id before save "
                    "(PK not assigned yet). Use 'random' strategy or "
                    "set public_id after save."
                )
            if width > 0:
                return encoder.encode_padded(pk, width)
            return encoder.encode(pk)

        raise ValueError(f"Unknown strategy: {strategy!r}")

    @classmethod
    async def get_by_public_id(cls, public_id: str, db=None):
        """Look up a model instance by its public_id.

        Args:
            public_id: The opaque public identifier string.
            db: Optional database connection (uses default if None).

        Returns:
            Model instance.

        Raises:
            DoesNotExist: If no matching record found.
        """
        if db is None:
            from hyperdjango.models import _resolve_instance_db

            db = _resolve_instance_db(cls, for_write=False)

        meta = cls._meta  # type: ignore[attr-defined]
        cols = ", ".join(meta.column_names)
        sql = f"SELECT {cols} FROM {meta.table} WHERE public_id = $1"
        row = await db.query_one(sql, public_id)
        if row is None:
            raise cls.DoesNotExist(  # type: ignore[attr-defined]
                f"{cls.__name__} with public_id={public_id!r} does not exist"
            )
        return cls.from_record(row)  # type: ignore[attr-defined]

    @classmethod
    async def filter_by_public_ids(cls, public_ids: list[str], db=None) -> list[object]:
        """Look up multiple model instances by their public_ids.

        Args:
            public_ids: List of public identifier strings.
            db: Optional database connection.

        Returns:
            List of model instances (order not guaranteed).
        """
        if not public_ids:
            return []

        if db is None:
            from hyperdjango.models import _resolve_instance_db

            db = _resolve_instance_db(cls, for_write=False)

        meta = cls._meta  # type: ignore[attr-defined]
        cols = ", ".join(meta.column_names)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(public_ids)))
        sql = f"SELECT {cols} FROM {meta.table} WHERE public_id IN ({placeholders})"
        rows = await db.query(sql, *public_ids)
        return [cls.from_record(row) for row in rows]  # type: ignore[attr-defined]

    @classmethod
    def decode_public_id(cls, public_id: str) -> int:
        """Decode a public_id back to its integer value.

        Only meaningful for 'encoded_pk' and 'random' strategies.
        For 'uuid7', raises ValueError.
        """
        if (
            cls._public_id_strategy == IDStrategy.UUID7
            and cls._public_id_encoder is None
        ):
            raise ValueError("Cannot decode uuid7 public_id — it's not base-encoded")
        return cls._public_id_encoder.decode(public_id)


# ── ID Modes ──────────────────────────────────────────────────────────────

_ID_MODES: Final[frozenset[IDMode]] = frozenset(
    {IDMode.RAW, IDMode.ENCODED, IDMode.SIGNED, IDMode.RANDOM}
)


@dataclass(slots=True)
class KeySlot:
    """HMAC key with optional PK offset and custom epoch for signed ID mode.

    - offset: added to PK before encoding, subtracted after decoding.
    - epoch: custom Unix timestamp used as base for time-window calculations.
      Timestamps in time-windowed IDs are stored relative to this epoch,
      making them compact and harder to reverse-engineer.

    Usage:
        KeySlot("secret-key-2025", offset=50_000, epoch=1704240000)
        # epoch = Jan 3, 2024 00:00 UTC
        # PK=1 → encode(50001) → sign → "Kx7mP3q.a3f8c2d1"
    """

    key: str
    offset: int = 0
    epoch: int = 0  # Custom epoch as Unix timestamp (0 = standard Unix epoch)


def _normalize_hmac_keys(keys: list[str | KeySlot]) -> list[KeySlot]:
    """Convert a list of strings or KeySlots to a list of KeySlots."""
    result: list[KeySlot] = []
    for item in keys:
        if isinstance(item, KeySlot):
            result.append(item)
        elif isinstance(item, str):
            result.append(KeySlot(key=item, offset=0))
        else:
            raise ValueError(
                f"hmac_keys items must be str or KeySlot, got {type(item).__name__}"
            )
    return result


@dataclass(slots=True)
class IDConfig:
    """Configuration for a model's external ID representation.

    Modes:
      raw     — integer PK exposed directly (internal APIs only)
      encoded — bijection encoding (reversible, no HMAC)
      signed  — bijection + HMAC signature (unforgeable, anti-enumeration)
      random  — random opaque string stored in extra DB column
    """

    mode: IDMode = IDMode.SIGNED
    alphabet: str = ""  # Required for encoded/signed modes
    hmac_keys: list[KeySlot] = field(
        default_factory=list
    )  # For signed mode, newest first; accepts str or KeySlot

    def __post_init__(self) -> None:
        # Auto-normalize string keys to KeySlot for convenience.
        # IDConfig is a non-frozen slots dataclass — direct assignment is correct.
        if self.hmac_keys and isinstance(self.hmac_keys[0], str):
            self.hmac_keys = _normalize_hmac_keys(self.hmac_keys)

    signature_bytes: int = 8  # HMAC truncation (8 bytes = 16 hex chars)
    include_user: bool = False  # Per-user signing
    separator: str = "."  # Between encoded value and signature
    table_name: str = ""  # Auto-set from model, used in HMAC input
    offset: int = (
        0  # Global offset for encoded mode (signed mode uses per-KeySlot offsets)
    )
    # For random mode only:
    entropy_bytes: int = 10
    strategy: IDStrategy = IDStrategy.RANDOM


@dataclass(slots=True)
class IDManager:
    """Encode, decode, sign, and verify external IDs for a model.

    Thread-safe: the config is effectively immutable and the memoization cache
    is guarded by a lock (safe under 3.14t free-threading, where concurrent
    encodes would otherwise race the OrderedDict's structural mutations).

    Performance: `encode(pk)` for SIGNED mode without user_id or time window
    is memoized per-instance. HMAC-SHA256 is ~0.7μs per call and profile data
    showed 37 encode calls per request on list endpoints — the cache makes
    repeated encodes of the same pk O(1) dict lookups.
    """

    config: IDConfig
    _encoder: BaseEncoder | None = field(default=None, repr=False)
    _encode_cache: OrderedDict[int, str] = field(
        default_factory=OrderedDict, repr=False
    )
    _encode_cache_max: int = field(default=4096, repr=False)
    # Guards _encode_cache. OrderedDict.get / popitem / __setitem__ are NOT
    # atomic against each other under free-threading; without this lock a
    # concurrent encode racing an eviction can corrupt the dict's internal
    # linked list.
    _encode_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.config.mode in _ENCODING_MODES:
            if not self.config.alphabet:
                raise ValueError(
                    f"IDConfig.alphabet is required for mode '{self.config.mode}'. "
                    f"Generate one with: from hyperdjango.public_id import generate_alphabet; "
                    f"print(generate_alphabet())"
                )
            # BaseEncoder validates: min length, no duplicates, string type.
            # IDManager is a non-frozen slots dataclass — direct assignment is correct.
            self._encoder = BaseEncoder(self.config.alphabet)

    def encode(
        self,
        pk: int,
        *,
        user_id: int | str | None = None,
        valid_after: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> str:
        """Convert internal PK to external ID string.

        For signed mode: applies KeySlot offset from newest key (index 0).
        For encoded mode: applies IDConfig.offset.

        Time-windowed IDs (signed mode only):
            valid_after: earliest datetime this ID is valid (None = no start limit)
            valid_until: latest datetime this ID is valid (None = no end limit)
            Produces 3-part ID: {encoded_pk}.{time_window}.{hmac}

        Fast path: when user_id, valid_after, and valid_until are all None
        (the common case on list/detail pages), the result is a pure function
        of pk and is memoized in _encode_cache.
        """
        mode = self.config.mode
        if mode == IDMode.RAW:
            return str(pk)
        if mode == IDMode.ENCODED:
            return self._encoder.encode(pk + self.config.offset)
        if mode == IDMode.SIGNED:
            # Fast path: memoized SIGNED encode for the common case.
            # User-specific or time-windowed IDs skip the cache.
            is_common_case = (
                user_id is None and valid_after is None and valid_until is None
            )
            if is_common_case:
                with self._encode_lock:
                    cached = self._encode_cache.get(pk)
                if cached is not None:
                    return cached

            slot = self.config.hmac_keys[0]
            offset_pk = pk + slot.offset
            encoded = self._encoder.encode(offset_pk)

            # Build time window segment (empty string if no window)
            window = self._encode_time_window(slot, valid_after, valid_until)

            # HMAC covers encoded PK + time window
            hmac_input = (
                encoded if not window else f"{encoded}{self.config.separator}{window}"
            )
            signature = self._sign(hmac_input, user_id=user_id)

            if window:
                result_windowed = (
                    f"{encoded}{self.config.separator}{window}"
                    f"{self.config.separator}{signature}"
                )
                return result_windowed
            result = f"{encoded}{self.config.separator}{signature}"
            # Populate the fast-path cache for future encode(pk) calls.
            # OrderedDict.popitem(last=False) evicts the oldest entry in O(1)
            # — no full-keys materialization. Guarded so concurrent encodes
            # can't corrupt the OrderedDict under free-threading.
            if is_common_case:
                with self._encode_lock:
                    cache = self._encode_cache
                    if pk not in cache and len(cache) >= self._encode_cache_max:
                        cache.popitem(last=False)
                    cache[pk] = result
            return result
        raise ValueError(
            "IDManager.encode() not applicable for 'random' mode — use the stored public_id"
        )

    def _encode_time_window(
        self,
        slot: KeySlot,
        valid_after: datetime | None,
        valid_until: datetime | None,
    ) -> str:
        """Encode a time window as compact '{start}-{end}' string relative to slot epoch.

        Returns empty string if no time window specified.
        Uses "0" for open-ended sides.
        """
        if valid_after is None and valid_until is None:
            return ""

        epoch = slot.epoch
        if valid_after is not None:
            ts = int(valid_after.timestamp()) - epoch
            start_enc = self._encoder.encode(max(0, ts))
        else:
            start_enc = "0"

        if valid_until is not None:
            ts = int(valid_until.timestamp()) - epoch
            end_enc = self._encoder.encode(max(0, ts))
        else:
            end_enc = "0"

        return f"{start_enc}-{end_enc}"

    def decode(self, external_id: str, *, user_id: int | str | None = None) -> int:
        """Convert external ID back to internal PK. Raises ValueError if invalid.

        For signed mode: subtracts the matching KeySlot's offset.
        For encoded mode: subtracts IDConfig.offset.
        """
        mode = self.config.mode
        if mode == IDMode.RAW:
            return int(external_id)
        if mode == IDMode.ENCODED:
            return self._encoder.decode(external_id) - self.config.offset
        if mode == IDMode.SIGNED:
            return self._decode_signed(external_id, user_id=user_id)
        raise ValueError(
            "IDManager.decode() not applicable for 'random' mode — query by public_id directly"
        )

    def verify(self, external_id: str, *, user_id: int | str | None = None) -> bool:
        """Check if external ID has a valid signature (signed mode only)."""
        if self.config.mode != IDMode.SIGNED:
            return True  # Non-signed modes don't need verification
        try:
            self._decode_signed(external_id, user_id=user_id)
            return True
        except ValueError:
            return False

    def _decode_signed(
        self, external_id: str, *, user_id: int | str | None = None
    ) -> int:
        """Decode and verify a signed external ID. Try each key in rotation.

        Handles two formats:
          2-part: {encoded_pk}.{hmac}           — no time window
          3-part: {encoded_pk}.{window}.{hmac}  — with time window
        """
        sep = self.config.separator
        parts = external_id.split(sep)

        if len(parts) == 2:
            # No time window: {encoded_pk}.{hmac}
            encoded_pk_part = parts[0]
            window_part = ""
            signature_part = parts[1]
            hmac_input = encoded_pk_part
        elif len(parts) == 3:
            # Time window: {encoded_pk}.{window}.{hmac}
            encoded_pk_part = parts[0]
            window_part = parts[1]
            signature_part = parts[2]
            hmac_input = f"{encoded_pk_part}{sep}{window_part}"
        else:
            raise ValueError("Invalid signed ID: unexpected format")

        if not encoded_pk_part or not signature_part:
            raise ValueError("Invalid signed ID: empty parts")

        # Try each KeySlot in rotation order
        for slot in self.config.hmac_keys:
            expected = self._compute_hmac(slot.key, hmac_input, user_id=user_id)
            if hmac.compare_digest(expected, signature_part):
                # Signature valid — check time window if present
                if window_part:
                    self._validate_time_window(slot, window_part)
                # Decode PK and subtract offset
                raw_pk = self._encoder.decode(encoded_pk_part)
                return raw_pk - slot.offset

        raise ValueError("Invalid signed ID: signature verification failed")

    def _validate_time_window(self, slot: KeySlot, window_part: str) -> None:
        """Validate that the current time is within the encoded time window.

        Raises ValueError if outside the window.
        """
        if "-" not in window_part:
            raise ValueError("Invalid time window format")

        dash_pos = window_part.index("-")
        start_enc = window_part[:dash_pos]
        end_enc = window_part[dash_pos + 1 :]

        now = int(time.time())
        epoch = slot.epoch

        # Decode start timestamp (relative to epoch)
        if start_enc != "0":
            start_offset = self._encoder.decode(start_enc)
            start_ts = start_offset + epoch
            if now < start_ts:
                raise ValueError("ID not yet valid")

        # Decode end timestamp (relative to epoch)
        if end_enc != "0":
            end_offset = self._encoder.decode(end_enc)
            end_ts = end_offset + epoch
            if now > end_ts:
                raise ValueError("ID has expired")

    def _sign(self, encoded_value: str, *, user_id: int | str | None = None) -> str:
        """Compute HMAC signature for an encoded value. Always uses newest key (index 0)."""
        if not self.config.hmac_keys:
            raise ValueError("No HMAC keys configured for signed mode")
        return self._compute_hmac(
            self.config.hmac_keys[0].key, encoded_value, user_id=user_id
        )

    def _compute_hmac(
        self, key: str, encoded_value: str, *, user_id: int | str | None = None
    ) -> str:
        """Compute truncated HMAC-SHA256 hex digest.

        Routes through the central `hmac_sha256_bytes_truncated` helper in
        `hyperdjango.native._crypto` which uses `hmac.digest()` fast path
        (OpenSSL HMAC + hardware SHA-NI/NEON). Single source of truth for
        all HMAC-SHA256 across the platform.
        """
        # Build HMAC message: "table_name:encoded_value" or "table_name:encoded_value:user_id"
        message = f"{self.config.table_name}:{encoded_value}"
        if self.config.include_user:
            if user_id is None:
                raise ValueError("user_id required for per-user signed IDs")
            message = f"{message}:{user_id}"

        # Truncate bytes first, then hex — avoids encoding 32 bytes when we
        # only need `signature_bytes` (default 8 = 16 hex chars).
        digest_bytes = hmac_sha256_bytes_truncated(
            key.encode("utf-8"),
            message.encode("utf-8"),
            self.config.signature_bytes,
        )
        return digest_bytes.hex()

    def generate_random(self) -> str:
        """Generate a random public_id (for 'random' mode only)."""
        if self.config.strategy == IDStrategy.UUID7:
            return str(uuid.uuid7())
        return self._encoder.encode_random(self.config.entropy_bytes)


class IDMixin:
    """Model mixin for automatic external ID management.

    Usage:
        class Post(IDMixin, Model):
            class IDConfig:
                mode = IDMode.SIGNED
                alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
                hmac_keys = ["current-key-2024", "old-key-2023"]
                table_name = "posts"  # auto-detected from Meta.table if empty
    """

    # Class-level IDManager, set by __init_subclass__
    _id_manager: ClassVar[IDManager]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Look for IDConfig inner class — access cls.__dict__ directly
        # (not inherited) to avoid re-processing parent configs
        id_config_cls = cls.__dict__.get("IDConfig")
        if id_config_cls is None:
            return

        # Build IDConfig from inner class attributes
        # We access __dict__ on the inner class because it's a user-defined
        # namespace class (not a dataclass), so direct attribute access is
        # the correct pattern for reading class-level declarations.
        config_dict = id_config_cls.__dict__
        config = IDConfig(
            mode=IDMode(config_dict.get("mode", IDMode.SIGNED)),
            alphabet=config_dict.get("alphabet", ""),
            hmac_keys=_normalize_hmac_keys(list(config_dict.get("hmac_keys", []))),
            signature_bytes=config_dict.get("signature_bytes", 8),
            include_user=config_dict.get("include_user", False),
            separator=config_dict.get("separator", "."),
            table_name=config_dict.get("table_name", ""),
            entropy_bytes=config_dict.get("entropy_bytes", 10),
            strategy=IDStrategy(config_dict.get("strategy", IDStrategy.RANDOM)),
        )

        # Auto-detect table_name from Meta if not set
        if not config.table_name:
            meta = cls.__dict__.get("Meta")
            if meta and "table" in meta.__dict__:
                config.table_name = meta.__dict__["table"]
            else:
                config.table_name = cls.__name__.lower()

        # Validate
        if config.mode not in _ID_MODES:
            raise ValueError(f"Invalid ID mode: {config.mode}")
        if config.mode in _ENCODING_MODES and not config.alphabet:
            raise ValueError(f"Alphabet required for '{config.mode}' mode")
        if config.mode == IDMode.SIGNED and not config.hmac_keys:
            raise ValueError("hmac_keys required for 'signed' mode")

        cls._id_manager = IDManager(config=config)

    def get_external_id(self, *, user_id: int | str | None = None) -> str:
        """Get the external representation of this object's ID."""
        return self._id_manager.encode(self.id, user_id=user_id)

    @classmethod
    def decode_external_id(
        cls, external_id: str, *, user_id: int | str | None = None
    ) -> int:
        """Decode an external ID to internal PK."""
        return cls._id_manager.decode(external_id, user_id=user_id)

    @classmethod
    def verify_external_id(
        cls, external_id: str, *, user_id: int | str | None = None
    ) -> bool:
        """Verify an external ID is valid."""
        return cls._id_manager.verify(external_id, user_id=user_id)
