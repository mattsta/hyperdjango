# Custom Fields -- Domain-Specific Model Fields

Framework for creating domain-specific field types that integrate with Model, ModelSerializer, Form, and Admin. Each `CustomField` defines its own PostgreSQL type mapping, Python/DB conversion, validation, serialization, and form widget rendering.

```python
from hyperdjango import Model, Field
from hyperdjango.fields import create_field, MoneyField, ColorField, SlugField
from decimal import Decimal


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    price: Decimal = create_field(MoneyField(currency="USD"))
    color: str = create_field(ColorField())
    slug: str = create_field(SlugField(max_length=100))
```

---

## CustomField Base Class

All custom fields inherit from `CustomField`. Subclasses must be `@dataclass(slots=True)`.

```python
from dataclasses import dataclass
from hyperdjango.fields import CustomField


@dataclass(slots=True)
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
```

### API Methods

| Method                     | Description                                                                |
| -------------------------- | -------------------------------------------------------------------------- |
| `db_type()`                | Return the PostgreSQL column type (e.g. `"varchar(7)"`, `"numeric(10,2)"`) |
| `to_db_value(value)`       | Convert Python value to database-storable value                            |
| `from_db_value(value)`     | Convert database value back to Python object                               |
| `validate(value)`          | Validate and clean the value; raise `ValueError` on failure                |
| `to_representation(value)` | Convert to JSON-serializable form for API responses                        |
| `to_internal_value(data)`  | Convert from JSON input to Python object                                   |
| `form_field_type()`        | Return HTML form field type (`"text"`, `"number"`, `"select"`, etc.)       |
| `form_widget_attrs()`      | Return HTML widget attributes dict                                         |

---

## `create_field()` -- Attaching Custom Fields to Models

`create_field()` creates a standard `Field()` with a `CustomField` instance attached. Model metaclass, ModelSerializer, and Form discover it via `field_info.custom_field`.

```python
from hyperdjango import Model, Field
from hyperdjango.fields import create_field, EmailField, PhoneField


class Contact(Model):
    class Meta:
        table = "contacts"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    email: str = create_field(EmailField(max_length=254))
    phone: str = create_field(PhoneField())
```

You can pass any standard `Field()` keyword arguments through `create_field()`:

```python
# Optional field with a default
website: str = create_field(URLField(), default="", required=False)
```

---

## Built-in Fields

### 1. MoneyField

Stores money as integer cents in a `bigint` column, exposed as `Decimal` in Python. Avoids floating-point precision issues.

Conversion to cents uses `ROUND_HALF_UP` rounding (banker's rounding is NOT used). For example, `Decimal("19.995")` rounds to `2000` cents (i.e., `$20.00`), not `1999`.

```python
from hyperdjango.fields import create_field, MoneyField
from decimal import Decimal

price: Decimal = create_field(
    MoneyField(currency="USD", max_digits=12, decimal_places=2)
)

# Python: Decimal("19.99")
# Database: 1999 (bigint)
# API output: {"amount": "19.99", "currency": "USD"}
# API input: {"amount": "19.99"} or "19.99"
# Form widget: <input type="number" step="0.01" min="0">
```

| Parameter        | Default | Description                                    |
| ---------------- | ------- | ---------------------------------------------- |
| `currency`       | `"USD"` | Currency code (included in API representation) |
| `max_digits`     | `12`    | Maximum total digits                           |
| `decimal_places` | `2`     | Decimal places                                 |

Validation rejects negative values and values exceeding the digit limit.

### 2. ColorField

CSS hex color in `#RRGGBB` format. Stored as `varchar(7)`, auto-lowercased.

```python
color: str = create_field(ColorField())
# Validates: "#ff5733" (valid), "red" (invalid)
# Form widget: <input type="color">
```

### 3. EmailField

Validated email address with domain normalization (lowercase domain part).

```python
email: str = create_field(EmailField(max_length=254))
# Normalizes: "User@EXAMPLE.COM" -> "User@example.com"
# Form widget: <input type="email" maxlength="254">
```

### 4. URLField

URL with scheme validation. Default allowed schemes: `http`, `https`.

```python
website: str = create_field(
    URLField(max_length=2048, allowed_schemes=frozenset({"http", "https"}))
)
# Validates: scheme present, host present, scheme in allowlist
# Form widget: <input type="url" maxlength="2048">
```

### 5. SlugField

URL-safe slug (lowercase alphanumeric with hyphens). Includes a standalone `slugify()` function.

```python
from hyperdjango.fields import create_field, SlugField, slugify

slug: str = create_field(SlugField(max_length=100))
# Validates: "my-product" (valid), "My Product" (invalid)
# Form widget: <input type="text" pattern="[a-z0-9]+(?:-[a-z0-9]+)*">

generated = slugify("My Product Name!")  # "my-product-name"
generated = slugify("Very Long Title Here", max_length=10)  # "very-long"
```

`slugify(text, max_length=50)` lowercases, replaces non-alphanumeric with hyphens, collapses runs, strips leading/trailing hyphens, and truncates to `max_length`.

### 6. PhoneField

E.164 international phone number. Auto-strips spaces, dashes, parentheses, and dots before validation.

```python
phone: str = create_field(PhoneField())
# Input: "+1 (415) 555-1234" -> stored as "+14155551234"
# Form widget: <input type="tel" pattern="\+[1-9]\d{1,14}">
```

### 7. IPAddressField

IPv4/IPv6 address stored as PostgreSQL `inet` type.

```python
ip: str = create_field(IPAddressField(protocol="both"))  # "ipv4", "ipv6", or "both"
# Validates: "192.168.1.1" (valid IPv4), "::1" (valid IPv6)
```

### 8. CIDRField

CIDR network range stored as PostgreSQL `cidr` type.

```python
network: str = create_field(CIDRField())
# Validates: "192.168.0.0/24", "2001:db8::/32"
```

### 9. UUIDField

UUID stored as PostgreSQL `uuid` type. Optional version constraint.

```python
import uuid

token: uuid.UUID = create_field(UUIDField(version=4))  # UUIDv4 only
any_uuid: uuid.UUID = create_field(UUIDField())  # any version
```

### 10. JSONField

Structured JSON data stored as PostgreSQL `jsonb`. Accepts Python dicts/lists or JSON strings (auto-parsed).

Serialization to the database uses strict `json.dumps()` without `default=str` or any other fallback serializer. If a value is not natively JSON-serializable (dict, list, str, int, float, bool, None), a `TypeError` is raised. This prevents silent data corruption from accidental serialization of non-JSON types.

```python
metadata: dict = create_field(JSONField())
# Input: '{"key": "value"}' (string) or {"key": "value"} (dict)
# Form widget: <textarea rows="6" class="json-editor">
```

### 11. ChoiceField

Constrained to a fixed set of allowed string values.

```python
status: str = create_field(ChoiceField(choices=("draft", "published", "archived")))
# DB type: varchar(9) (auto-sized to longest choice)
# Form widget: <select>
```

### 12. EncryptedField

At-rest encryption using Fernet symmetric encryption derived from `SECRET_KEY`. API responses are masked with `"****"` to prevent accidental exposure.

**Requires**: `cryptography` package (`uv add cryptography`). If `cryptography` is not installed, EncryptedField can still be imported and instantiated, but any attempt to encrypt (store) or decrypt (read) a value raises `RuntimeError` with the message: `"EncryptedField requires 'cryptography' package: uv add cryptography"`. All other 13 built-in fields work without this dependency.

**Encryption details**: The encryption key is derived from `SECRET_KEY` using SHA-256 (NOT HKDF). The 32-byte SHA-256 digest is base64url-encoded to produce a Fernet-compatible key. Security relies on `SECRET_KEY` having sufficient entropy. If `SECRET_KEY` is empty or not set, a `ValueError` is raised at encrypt/decrypt time.

**Masked output**: The `to_representation()` method always returns `"****"` for non-None values. This prevents accidental exposure of encrypted data in API responses, admin views, and serializers. To access the actual decrypted value, use `from_db_value()` directly.

```python
ssn: str = create_field(EncryptedField())
# Python: "123-45-6789"
# Database: encrypted ciphertext (Fernet token)
# API output: "****"
# Form widget: <input type="password" autocomplete="off">
```

Pass `_secret_key="..."` to use an explicit key instead of `SECRET_KEY`.

- **Related setting:** `SECRET_KEY` (from `hyperdjango.conf`) -- used to derive the Fernet encryption key.

### 13. DurationField

Time duration stored as PostgreSQL `interval` type. Accepts `timedelta`, seconds (int/float), or interval strings.

Negative timedeltas are handled correctly: `to_db_value()` decomposes the timedelta using `total_seconds()` and formats negative durations with a leading `-` sign (e.g., `timedelta(hours=-2)` becomes `"-02:00:00"`). The `to_representation()` method returns `total_seconds()` as a float, which is negative for negative durations.

```python
from datetime import timedelta

duration: timedelta = create_field(DurationField())
# Input: timedelta(hours=2, minutes=30), 9000 (seconds), "1 day 02:30:00"
# API output: total seconds (float)
# Negative: timedelta(hours=-1) -> API output: -3600.0
```

### 14. PercentField

Percentage value stored as `numeric`. Supports two storage modes.

```python
from decimal import Decimal

# Store as 0-100 (default)
discount: Decimal = create_field(PercentField())
# Validates: 0 <= value <= 100

# Store as fraction 0.0-1.0
rate: Decimal = create_field(PercentField(store_as_fraction=True))
# Validates: 0.0 <= value <= 1.0
```

---

## DB Type Mapping Table

| Field            | PostgreSQL Type                  |
| ---------------- | -------------------------------- |
| `MoneyField`     | `bigint`                         |
| `ColorField`     | `varchar(7)`                     |
| `EmailField`     | `varchar(254)`                   |
| `URLField`       | `varchar(2048)`                  |
| `SlugField`      | `varchar(50)`                    |
| `PhoneField`     | `varchar(20)`                    |
| `IPAddressField` | `inet`                           |
| `CIDRField`      | `cidr`                           |
| `UUIDField`      | `uuid`                           |
| `JSONField`      | `jsonb`                          |
| `ChoiceField`    | `varchar(N)` (auto-sized)        |
| `EncryptedField` | `text`                           |
| `DurationField`  | `interval`                       |
| `PercentField`   | `numeric(5,2)` or `numeric(5,4)` |

---

## Field Registry

Register custom fields globally so other subsystems can discover them by Python type.

```python
from hyperdjango.fields import register_field, get_custom_field, unregister_field

register_field(Temperature, TemperatureField(unit="celsius"))

field = get_custom_field(Temperature)  # TemperatureField instance or None

unregister_field(Temperature)  # returns True if it existed
```

The registry is thread-safe: all operations (`register_field`, `get_custom_field`, `unregister_field`) acquire a `threading.Lock` before accessing the internal dictionary. Safe for use under Python 3.14t free-threading.

---

## Integration with ModelSerializer

When a model field has a `CustomField` attached, `ModelSerializer` automatically uses its `to_representation()` and `to_internal_value()` methods for serialization/deserialization.

```python
from hyperdjango.serializers import ModelSerializer


class ProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "name", "price", "color"]


# Serializing:
data = ProductSerializer(product).data
# {"id": 1, "name": "Widget", "price": {"amount": "19.99", "currency": "USD"}, "color": "#ff5733"}

# Deserializing:
serializer = ProductSerializer(
    data={"name": "Widget", "price": {"amount": "19.99"}, "color": "#ff5733"}
)
serializer.is_valid()
```

---

## Integration with Forms

Forms discover the `CustomField` and use its `form_field_type()` and `form_widget_attrs()` for rendering.

```python
from hyperdjango.forms import ModelForm


class ProductForm(ModelForm):
    class Meta:
        model = Product
        fields = ["name", "price", "color"]


# Renders:
# name: <input type="text" maxlength="200">
# price: <input type="number" step="0.01" min="0">
# color: <input type="color">
```

---

## Integration with Admin

HyperAdmin auto-detects custom fields and renders appropriate widgets in the add/change forms. `ChoiceField` renders as a `<select>`, `ColorField` as a color picker, `JSONField` as a textarea with the `json-editor` class, and so on.

---

## Integration Helpers

Low-level functions for framework internals:

```python
from hyperdjango.fields import get_column_type, convert_to_db, convert_from_db

# Get the PostgreSQL type for migration generation
col_type = get_column_type(field_info)  # "bigint", "varchar(7)", etc.

# Convert Python -> DB (validates first, then converts)
db_value = convert_to_db(field_info, Decimal("19.99"))  # 1999

# Convert DB -> Python
py_value = convert_from_db(field_info, 1999)  # Decimal("19.99")
```

---

## Creating Your Own Custom Field

Step-by-step tutorial for building a custom field:

```python
from dataclasses import dataclass
from hyperdjango.fields import CustomField, create_field
from hyperdjango import Model, Field


@dataclass(slots=True)
class LatLngField(CustomField):
    """Geographic coordinate stored as PostgreSQL point type."""

    def db_type(self) -> str:
        return "point"

    def validate(self, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, (list, tuple)) and len(value) == 2:
            lat, lng = float(value[0]), float(value[1])
            if not (-90 <= lat <= 90):
                raise ValueError(f"Latitude must be -90..90, got {lat}")
            if not (-180 <= lng <= 180):
                raise ValueError(f"Longitude must be -180..180, got {lng}")
            return (lat, lng)
        raise ValueError(f"Expected (lat, lng) tuple, got {value!r}")

    def to_db_value(self, value: object) -> object:
        if value is None:
            return None
        lat, lng = value
        return f"({lat},{lng})"

    def from_db_value(self, value: object) -> object:
        if value is None:
            return None
        # PostgreSQL returns point as "(lat,lng)" string
        s = str(value).strip("()")
        parts = s.split(",")
        return (float(parts[0]), float(parts[1]))

    def to_representation(self, value: object) -> object:
        if value is None:
            return None
        return {"lat": value[0], "lng": value[1]}

    def to_internal_value(self, data: object) -> object:
        if isinstance(data, dict):
            return (float(data["lat"]), float(data["lng"]))
        return data

    def form_field_type(self) -> str:
        return "text"

    def form_widget_attrs(self) -> dict[str, str]:
        return {"placeholder": "37.7749, -122.4194"}


# Use it
class Location(Model):
    class Meta:
        table = "locations"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    coordinates: tuple = create_field(LatLngField())
```

---

## See Also

- [formats.md](formats.md) -- Locale-aware number/currency formatting (complements `MoneyField` and `PercentField`)
- [tasks.md](tasks.md) -- Background tasks (for async field processing like encryption or validation)
