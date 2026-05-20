"""
Custom model fields framework for HyperDjango.

Lets users create domain-specific field types that integrate with Model,
ModelSerializer, Form, and Admin. Each CustomField defines its own DB type
mapping, Python/DB conversion, validation, serialization, and form rendering.

Usage:
    from hyperdjango.fields import (
        CustomField, create_field, MoneyField, ColorField, EmailField,
        URLField, SlugField, PhoneField, IPAddressField, CIDRField,
        UUIDField, JSONField, ChoiceField, EncryptedField, DurationField,
        PercentField,
    )
    from decimal import Decimal

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        price: Decimal = create_field(MoneyField(currency="USD"))
        color: str = create_field(ColorField())
        slug: str = create_field(SlugField(max_length=100))

    # Custom field types
    class TemperatureField(CustomField):
        unit: str = "celsius"

        def db_type(self) -> str:
            return "numeric(5,2)"

        def validate(self, value: object) -> object:
            if not isinstance(value, (int, float)):
                raise ValueError(f"Expected numeric value, got {type(value).__name__}")
            v = float(value)
            if self.unit == "celsius" and not (-273.15 <= v <= 1000.0):
                raise ValueError(f"Temperature {v}C out of range")
            return v

        def to_representation(self, value: object) -> object:
            return {"value": float(value), "unit": self.unit}
"""

import base64
import hashlib
import ipaddress
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

try:
    from cryptography.fernet import Fernet as _Fernet
except ModuleNotFoundError:
    _Fernet = None  # type: ignore[assignment,misc]

from hyperdjango.conf import get_setting
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.validation.core import Field as _DhiField
from hyperdjango.validation.core.fields import FieldInfo

# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CustomField:
    """Base class for user-defined model fields.

    Subclass this to create domain-specific field types that integrate
    with Model, ModelSerializer, Form, and Admin.

    Subclasses MUST be @dataclass(slots=True).
    """

    def db_type(self) -> str:
        """Return the PostgreSQL column type (e.g., 'varchar(7)', 'numeric(10,2)')."""
        raise NotImplementedError("Subclass must implement db_type()")

    def to_db_value(self, value: object) -> object:
        """Convert Python value to database-storable value."""
        return value

    def from_db_value(self, value: object) -> object:
        """Convert database value back to Python object."""
        return value

    def validate(self, value: object) -> object:
        """Validate and clean the value. Return cleaned value or raise ValueError."""
        return value

    def to_representation(self, value: object) -> object:
        """Convert to JSON-serializable representation."""
        return value

    def to_internal_value(self, data: object) -> object:
        """Convert from JSON input to Python object."""
        return data

    def form_field_type(self) -> str:
        """Return form field type name (e.g., 'text', 'number', 'select')."""
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        """Return HTML widget attributes."""
        return {}


# ---------------------------------------------------------------------------
# Registry — thread-safe mapping of Python type -> CustomField
# ---------------------------------------------------------------------------

_field_registry: dict[type, CustomField] = {}
_field_registry_lock = threading.Lock()


def register_field(python_type: type, custom_field: CustomField) -> None:
    """Register a custom field for a Python type.

    Thread-safe. Overwrites any previous registration for the same type.
    """
    with _field_registry_lock:
        _field_registry[python_type] = custom_field


def get_custom_field(python_type: type) -> CustomField | None:
    """Look up the custom field for a Python type. Returns None if not found."""
    with _field_registry_lock:
        return _field_registry.get(python_type)


def unregister_field(python_type: type) -> bool:
    """Remove a custom field registration. Returns True if it existed."""
    with _field_registry_lock:
        return _field_registry.pop(python_type, None) is not None


# ---------------------------------------------------------------------------
# create_field() — attaches CustomField to FieldInfo
# ---------------------------------------------------------------------------


def create_field(custom_field: CustomField, **kwargs: object) -> FieldInfo:
    """Create a model Field() with a custom field type attached.

    The CustomField instance is stored on the FieldInfo so that ModelMeta,
    ModelSerializer, and Form can discover it via field_info.custom_field.

    Usage:
        class Product(Model):
            price: Decimal = create_field(MoneyField(currency="USD"))
            color: str = create_field(ColorField())
    """
    return _DhiField(custom_field=custom_field, **kwargs)


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


def _effective_custom_field(field_info: FieldInfo) -> CustomField | None:
    """Resolve the CustomField that applies to a field.

    An explicit custom field (attached via create_field) always wins. Otherwise
    fall back to the type registry so that register_field(SomeType, SomeField())
    takes effect for plainly-annotated fields of that Python type.
    """
    cf = field_info.custom_field
    if cf is not None:
        return cf
    annotation = field_info.annotation
    if annotation is not None:
        return get_custom_field(annotation)
    return None


def get_column_type(field_info: FieldInfo) -> str | None:
    """If a custom field applies to field_info, return its db_type(). Otherwise None."""
    cf = _effective_custom_field(field_info)
    if cf is not None:
        return cf.db_type()
    return None


def convert_to_db(field_info: FieldInfo, value: object) -> object:
    """Convert a Python value to DB value using the custom field, if present.

    If no custom field applies (explicitly attached or registered for the
    annotated type), returns the value unchanged.
    """
    cf = _effective_custom_field(field_info)
    if cf is not None:
        validated = cf.validate(value)
        return cf.to_db_value(validated)
    return value


def convert_from_db(field_info: FieldInfo, value: object) -> object:
    """Convert a DB value to Python value using the custom field, if present.

    If no custom field applies (explicitly attached or registered for the
    annotated type), returns the value unchanged.
    """
    cf = _effective_custom_field(field_info)
    if cf is not None:
        return cf.from_db_value(value)
    return value


# ---------------------------------------------------------------------------
# Built-in custom fields
# ---------------------------------------------------------------------------

# Regex patterns compiled once at module level
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9]"
    r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_PHONE_STRIP_RE = re.compile(r"[\s\-\(\).]")
_SLUGIFY_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SLUGIFY_MULTI_HYPHEN_RE = re.compile(r"-{2,}")


# 1. MoneyField — stores cents as integer, exposes as Decimal


@dataclass(slots=True)
class MoneyField(CustomField):
    """Money stored as integer cents in the database, exposed as Decimal.

    Avoids floating-point precision issues by storing the smallest currency
    unit (e.g., cents for USD) as a bigint.
    """

    currency: str = "USD"
    max_digits: int = 12
    decimal_places: int = 2

    def db_type(self) -> str:
        return "bigint"

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        d = Decimal(str(value))
        multiplier = Decimal(10**self.decimal_places)
        return int((d * multiplier).to_integral_value(rounding=ROUND_HALF_UP))

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        divisor = Decimal(10**self.decimal_places)
        return Decimal(value) / divisor

    def validate(self, value: object) -> object:
        if value is None:
            return value
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid money value: {value!r}") from exc
        if d < 0:
            raise ValueError(f"Money value must be non-negative, got {d}")
        max_value = Decimal(10 ** (self.max_digits - self.decimal_places))
        if d >= max_value:
            raise ValueError(
                f"Money value {d} exceeds maximum "
                f"({self.max_digits} digits, {self.decimal_places} decimal places)"
            )
        return d

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return {"amount": str(Decimal(str(value))), "currency": self.currency}

    def to_internal_value(self, data: object) -> object:
        if isinstance(data, dict):
            return Decimal(str(data["amount"]))
        return Decimal(str(data))

    def form_field_type(self) -> str:
        return "number"

    def form_widget_attrs(self) -> dict[str, str]:
        step = str(Decimal(1) / Decimal(10**self.decimal_places))
        return {"step": step, "min": "0"}


# 2. ColorField — CSS hex color


@dataclass(slots=True)
class ColorField(CustomField):
    """CSS hex color (#RRGGBB format)."""

    def db_type(self) -> str:
        return "varchar(7)"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value).strip()
        if not _HEX_COLOR_RE.match(s):
            raise ValueError(
                f"Invalid color format: {s!r}. Expected #RRGGBB (e.g., #ff5733)"
            )
        return s.lower()

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value).lower()

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "color"

    def form_widget_attrs(self) -> dict[str, str]:
        return {}


# 3. EmailField — validated email with normalization


@dataclass(slots=True)
class EmailField(CustomField):
    """Email address with validation and domain normalization."""

    max_length: int = 254

    def db_type(self) -> str:
        return f"varchar({self.max_length})"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value).strip()
        if len(s) > self.max_length:
            raise ValueError(
                f"Email exceeds maximum length of {self.max_length} characters"
            )
        if not _EMAIL_RE.match(s):
            raise ValueError(f"Invalid email address: {s!r}")
        return s

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        s = str(value).strip()
        # Normalize: lowercase the domain part
        parts = s.split("@", 1)
        if len(parts) == 2:
            return f"{parts[0]}@{parts[1].lower()}"
        return s

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "email"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"maxlength": str(self.max_length)}


# 4. URLField — validated URL


@dataclass(slots=True)
class URLField(CustomField):
    """URL with scheme validation."""

    max_length: int = 2048
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})

    def db_type(self) -> str:
        return f"varchar({self.max_length})"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value).strip()
        if len(s) > self.max_length:
            raise ValueError(
                f"URL exceeds maximum length of {self.max_length} characters"
            )
        parsed = urlparse(s)
        if not parsed.scheme:
            raise ValueError(f"URL missing scheme: {s!r}")
        if parsed.scheme not in self.allowed_schemes:
            raise ValueError(
                f"URL scheme {parsed.scheme!r} not allowed. "
                f"Allowed: {', '.join(sorted(self.allowed_schemes))}"
            )
        if not parsed.netloc:
            raise ValueError(f"URL missing host: {s!r}")
        return s

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "url"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"maxlength": str(self.max_length)}


# 5. SlugField — URL-safe slug


@dataclass(slots=True)
class SlugField(CustomField):
    """URL-safe slug (lowercase alphanumeric with hyphens)."""

    max_length: int = 50

    def db_type(self) -> str:
        return f"varchar({self.max_length})"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value).strip()
        if len(s) > self.max_length:
            raise ValueError(
                f"Slug exceeds maximum length of {self.max_length} characters"
            )
        if not s:
            raise ValueError("Slug cannot be empty")
        if not _SLUG_RE.match(s):
            raise ValueError(
                f"Invalid slug: {s!r}. "
                "Must be lowercase alphanumeric with hyphens (e.g., 'my-page')"
            )
        return s

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        return {
            "maxlength": str(self.max_length),
            "pattern": r"[a-z0-9]+(?:-[a-z0-9]+)*",
        }


def slugify(text: str, max_length: int = 50) -> str:
    """Generate a URL-safe slug from arbitrary text.

    Lowercases, replaces non-alphanumeric with hyphens, collapses runs,
    strips leading/trailing hyphens, and truncates to max_length.
    """
    s = text.lower().strip()
    s = _SLUGIFY_NON_ALNUM_RE.sub("-", s)
    s = s.strip("-")
    # Collapse multiple hyphens
    s = _SLUGIFY_MULTI_HYPHEN_RE.sub("-", s)
    if len(s) > max_length:
        s = s[:max_length].rstrip("-")
    return s


# 6. PhoneField — E.164 phone numbers


@dataclass(slots=True)
class PhoneField(CustomField):
    """E.164 international phone number (+1234567890)."""

    def db_type(self) -> str:
        return "varchar(20)"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = _PHONE_STRIP_RE.sub("", str(value).strip())
        if not _E164_RE.match(s):
            raise ValueError(
                f"Invalid phone number: {value!r}. "
                "Expected E.164 format (e.g., +14155551234)"
            )
        return s

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        return _PHONE_STRIP_RE.sub("", str(value).strip())

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "tel"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"pattern": r"\+[1-9]\d{1,14}"}


# 7. IPAddressField — IPv4/IPv6

_VALID_IP_PROTOCOLS = frozenset({"ipv4", "ipv6", "both"})


@dataclass(slots=True)
class IPAddressField(CustomField):
    """IPv4 or IPv6 address using PostgreSQL inet type."""

    protocol: str = "both"  # "ipv4", "ipv6", "both"

    def __post_init__(self) -> None:
        if self.protocol not in _VALID_IP_PROTOCOLS:
            raise ValueError(
                f"Invalid protocol {self.protocol!r}. "
                f"Must be one of: {', '.join(sorted(_VALID_IP_PROTOCOLS))}"
            )

    def db_type(self) -> str:
        return "inet"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value).strip()
        try:
            addr = ipaddress.ip_address(s)
        except ValueError as exc:
            raise ValueError(f"Invalid IP address: {s!r}") from exc
        if self.protocol == "ipv4" and addr.version != 4:
            raise ValueError(f"Expected IPv4 address, got IPv6: {s!r}")
        if self.protocol == "ipv6" and addr.version != 6:
            raise ValueError(f"Expected IPv6 address, got IPv4: {s!r}")
        return str(addr)

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"placeholder": "192.168.1.1 or ::1"}


# 8. CIDRField — network ranges


@dataclass(slots=True)
class CIDRField(CustomField):
    """CIDR network range (e.g., 192.168.0.0/24, 2001:db8::/32)."""

    def db_type(self) -> str:
        return "cidr"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value).strip()
        try:
            network = ipaddress.ip_network(s, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR network: {s!r}") from exc
        return str(network)

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        # Value is already normalized by validate() to str(ip_network(...))
        return str(value)

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"placeholder": "192.168.0.0/24"}


# 9. UUIDField — UUID type


@dataclass(slots=True)
class UUIDField(CustomField):
    """UUID field with optional version constraint."""

    version: int | None = None  # None = any UUID, 4 = UUIDv4 only

    def db_type(self) -> str:
        return "uuid"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            u = value
        else:
            try:
                u = uuid.UUID(str(value))
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"Invalid UUID: {value!r}") from exc
        if self.version is not None and u.version != self.version:
            raise ValueError(
                f"Expected UUID version {self.version}, got version {u.version}"
            )
        return u

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return str(value)

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def to_internal_value(self, data: object) -> object:
        if data is None:
            return None
        return uuid.UUID(str(data))

    def form_field_type(self) -> str:
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        return {
            "pattern": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        }


# 10. JSONField — structured JSON with JSONB storage


@dataclass(slots=True)
class JSONField(CustomField):
    """JSON/JSONB field for storing structured data."""

    def db_type(self) -> str:
        return "jsonb"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        # Already a Python object (dict/list/str/int/float/bool) is fine
        if isinstance(value, str):
            try:
                return fast_json_loads(value)
            except (ValueError, TypeError, RuntimeError) as exc:
                raise ValueError(f"Invalid JSON string: {exc}") from exc
        return value

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            # Assume it is already JSON
            return value
        return fast_json_dumps(value).decode()

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return fast_json_loads(value)
        # pg.zig may already return parsed dicts/lists
        return value

    def to_representation(self, value: object) -> object:
        return value

    def to_internal_value(self, data: object) -> object:
        return data

    def form_field_type(self) -> str:
        return "textarea"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"rows": "6", "class": "json-editor"}


# 11. ChoiceField — constrained to specific values


@dataclass(slots=True)
class ChoiceField(CustomField):
    """Field constrained to a fixed set of allowed values."""

    choices: frozenset[str] = frozenset()

    def db_type(self) -> str:
        if not self.choices:
            return "varchar(255)"
        max_len = max(len(c) for c in self.choices)
        return f"varchar({max(max_len, 1)})"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        s = str(value)
        if self.choices and s not in self.choices:
            raise ValueError(
                f"Invalid choice: {s!r}. "
                f"Must be one of: {', '.join(sorted(self.choices))}"
            )
        return s

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return str(value)

    def form_field_type(self) -> str:
        return "select"

    def form_widget_attrs(self) -> dict[str, str]:
        return {}


# 12. EncryptedField — at-rest encryption


def _derive_fernet_key(secret: str) -> bytes:
    """Derive a 32-byte Fernet-compatible key from an arbitrary secret string.

    Uses SHA-256 to produce exactly 32 bytes, then base64url-encodes
    for Fernet compatibility. Note: this is a simple hash derivation,
    not HKDF. The security relies on SECRET_KEY having sufficient entropy.
    """
    raw = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw)


@dataclass(slots=True)
class EncryptedField(CustomField):
    """At-rest encrypted text field.

    Encrypts values before storage and decrypts on retrieval.
    Uses Fernet symmetric encryption derived from the application SECRET_KEY.

    WARNING: The to_representation() method returns masked output ("****...")
    to prevent accidental exposure in API responses. Use from_db_value()
    directly when you need the decrypted plaintext.
    """

    _secret_key: str | None = field(default=None, repr=False)

    def _get_secret(self) -> str:
        """Resolve the encryption key: explicit or from settings."""
        if self._secret_key is not None:
            return self._secret_key
        key: str = get_setting("SECRET_KEY")  # type: ignore[assignment]
        if not key:
            raise ValueError(
                "EncryptedField requires SECRET_KEY in settings or "
                "an explicit _secret_key parameter"
            )
        return key

    def db_type(self) -> str:
        return "text"

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        if _Fernet is None:
            raise RuntimeError(
                "EncryptedField requires 'cryptography' package: uv add cryptography"
            )
        key = _derive_fernet_key(self._get_secret())
        f = _Fernet(key)
        return f.encrypt(str(value).encode("utf-8")).decode("ascii")

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        if _Fernet is None:
            raise RuntimeError(
                "EncryptedField requires 'cryptography' package: uv add cryptography"
            )
        key = _derive_fernet_key(self._get_secret())
        f = _Fernet(key)
        return f.decrypt(str(value).encode("ascii")).decode("utf-8")

    def validate(self, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError(
                f"EncryptedField expects a string, got {type(value).__name__}"
            )
        return value

    def to_representation(self, value: object) -> object:
        # Never expose encrypted values in API output
        if value is None:
            return None
        return "****"

    def form_field_type(self) -> str:
        return "password"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"autocomplete": "off"}


# 13. DurationField — time intervals

# Interval parsing regex: PostgreSQL interval output format
_INTERVAL_RE = re.compile(
    r"^(?:([+-]?\d+)\s+days?\s*)?"
    r"(?:([+-]?)(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?)?\s*$"
)


@dataclass(slots=True)
class DurationField(CustomField):
    """Time duration/interval field, stored as PostgreSQL interval."""

    def db_type(self) -> str:
        return "interval"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, timedelta):
            return value
        if isinstance(value, (int, float)):
            return timedelta(seconds=value)
        # Try parsing string
        s = str(value).strip()
        td = _parse_interval(s)
        if td is None:
            raise ValueError(
                f"Invalid duration: {s!r}. "
                "Expected timedelta, seconds number, or 'N days HH:MM:SS' format"
            )
        return td

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, timedelta):
            # timedelta stores (days, seconds, microseconds) where
            # 0 <= seconds < 86400 and 0 <= microseconds < 1_000_000; for
            # negative durations `days` is negative while seconds/microseconds
            # are non-negative offsets within that day. Collapse to a single
            # signed magnitude (in microseconds, so sub-second precision is
            # preserved) and emit that magnitude with a consistent sign on
            # every field — PostgreSQL signs each interval field independently,
            # so the sign must appear on the HH:MM:SS part too, not only days.
            total_us = (
                value.days * 86400 + value.seconds
            ) * 1_000_000 + value.microseconds
            sign = "-" if total_us < 0 else ""
            abs_seconds, microseconds = divmod(abs(total_us), 1_000_000)
            days, day_remainder = divmod(abs_seconds, 86400)
            hours, remainder = divmod(day_remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            if microseconds:
                time_str = (
                    f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{microseconds:06d}"
                )
            else:
                time_str = f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"
            if days:
                return f"{sign}{days} days {time_str}"
            return time_str
        return str(value)

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, timedelta):
            return value
        s = str(value).strip()
        td = _parse_interval(s)
        if td is not None:
            return td
        return value

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, timedelta):
            return value.total_seconds()
        return value

    def to_internal_value(self, data: object) -> object:
        if data is None:
            return None
        if isinstance(data, (int, float)):
            return timedelta(seconds=data)
        return data

    def form_field_type(self) -> str:
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"placeholder": "1 day 02:30:00"}


def _parse_interval(s: str) -> timedelta | None:
    """Parse a PostgreSQL interval string to timedelta."""
    m = _INTERVAL_RE.match(s)
    if not m:
        return None
    days = int(m.group(1)) if m.group(1) else 0
    # PostgreSQL signs each interval field independently. The leading sign on
    # the HH:MM:SS component applies to the whole time part; days carries its
    # own sign via group 1.
    time_sign = -1 if m.group(2) == "-" else 1
    hours = int(m.group(3)) if m.group(3) else 0
    minutes = int(m.group(4)) if m.group(4) else 0
    seconds = int(m.group(5)) if m.group(5) else 0
    microseconds = 0
    if m.group(6):
        frac = m.group(6).ljust(6, "0")[:6]
        microseconds = int(frac)
    return timedelta(days=days) + time_sign * timedelta(
        hours=hours,
        minutes=minutes,
        seconds=seconds,
        microseconds=microseconds,
    )


# 14. PercentField — 0-100 or 0.0-1.0


@dataclass(slots=True)
class PercentField(CustomField):
    """Percentage value, stored as numeric.

    If store_as_fraction is True, stores 0.0-1.0 (e.g., 0.75 for 75%).
    If False (default), stores 0-100 (e.g., 75 for 75%).
    """

    store_as_fraction: bool = False

    def db_type(self) -> str:
        if self.store_as_fraction:
            return "numeric(5,4)"
        return "numeric(5,2)"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid percentage value: {value!r}") from exc
        if self.store_as_fraction:
            if not (Decimal(0) <= d <= Decimal(1)):
                raise ValueError(
                    f"Fraction percentage must be between 0.0 and 1.0, got {d}"
                )
        else:
            if not (Decimal(0) <= d <= Decimal(100)):
                raise ValueError(f"Percentage must be between 0 and 100, got {d}")
        return d

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        return str(Decimal(str(value)))

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        return Decimal(str(value))

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return float(Decimal(str(value)))

    def to_internal_value(self, data: object) -> object:
        if data is None:
            return None
        return Decimal(str(data))

    def form_field_type(self) -> str:
        return "number"

    def form_widget_attrs(self) -> dict[str, str]:
        if self.store_as_fraction:
            return {"min": "0", "max": "1", "step": "0.01"}
        return {"min": "0", "max": "100", "step": "0.01"}


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Base
    "CustomField",
    # Registry
    "register_field",
    "get_custom_field",
    "unregister_field",
    # Factory
    "create_field",
    # Integration
    "get_column_type",
    "convert_to_db",
    "convert_from_db",
    # Built-in fields
    "MoneyField",
    "ColorField",
    "EmailField",
    "URLField",
    "SlugField",
    "PhoneField",
    "IPAddressField",
    "CIDRField",
    "UUIDField",
    "JSONField",
    "ChoiceField",
    "EncryptedField",
    "DurationField",
    "PercentField",
    # Utilities
    "slugify",
]
