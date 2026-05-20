# Validation

Native Zig model validation -- 4.3x faster than Python dataclasses. SIMD-accelerated field validation, batch processing at 51M values/sec, and a complete Pydantic v2-compatible API.

HyperDjango's validation engine is self-contained in `hyperdjango.validation.core` (4,350 lines). It uses the compiled Zig native extension for hot-path validation (integer range, string length, email format) and falls back to pure Python for complex constraint types. The native extension is always required.

## Model Validation

Models validate automatically on creation. All type annotations and `Field()` constraints are checked in a single pass:

```python
from hyperdjango import Model, Field
from hyperdjango.validation.core import EmailStr


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    email: EmailStr = Field(unique=True)
    age: int = Field(ge=0, le=150, default=0)


# Validates on creation
user = User(name="Alice", email="alice@example.com", age=30)  # OK

# Raises ValidationError with all failures
user = User(name="A" * 200, email="bad", age=-1)
# ValidationError: Validation failed:
#   name: String length 200 exceeds maximum 100
#   email: Invalid email address: 'bad'
#   age: Value -1 must be >= 0
```

### Validation Flow

When a model instance is created, validation proceeds in this order:

1. **Type checking**: each value is checked against its annotated type (`str`, `int`, `float`, `bool`, `Optional[T]`, etc.)
2. **Before-validators**: `@field_validator(mode='before')` functions run on raw input
3. **Type coercion**: values are coerced to the target type if `strict=False` (default)
4. **Constraint validation**: `Field()` constraints are applied (`ge`, `le`, `max_length`, `pattern`, etc.)
5. **After-validators**: `@field_validator(mode='after')` functions run on validated values
6. **Model validators**: `@model_validator(mode='after')` runs on the complete model instance

If the native extension is compiled, steps 2-4 are handled by `compile_model_specs` + `init_model_full` in Zig -- a single FFI call that validates all fields at once (1.6M models/sec).

### Native Acceleration Pipeline

The native validation uses two Zig functions:

- **`compile_model_specs(field_specs)`**: Pre-compiles field constraint specifications into an optimized internal representation. Called once per model class at import time.
- **`init_model_full(compiled_specs, field_values)`**: Validates all field values against compiled specs in a single FFI call. Called on every model instantiation.

Additional native functions:

| Function              | Signature                                    | Description                             |
| --------------------- | -------------------------------------------- | --------------------------------------- |
| `validate_field`      | `validate_field(spec, value) -> bool`        | Validate a single field value           |
| `dump_model_compiled` | `dump_model_compiled(specs, values) -> dict` | Serialize model to dict                 |
| `dump_model_to_json`  | `dump_model_to_json(specs, values) -> str`   | Serialize model directly to JSON string |
| `json_loads_model`    | `json_loads_model(specs, json_str) -> dict`  | Parse JSON and validate in one pass     |

## Field Types and Constraints

### Core Field Types

| Python Type   | Validation Rule                               | Default Coercion                 |
| ------------- | --------------------------------------------- | -------------------------------- |
| `str`         | Must be string (or coerced from other types)  | `str(value)`                     |
| `int`         | Must be integer                               | `int(value)` for numeric strings |
| `float`       | Must be float or int                          | `float(value)`                   |
| `bool`        | Must be boolean                               | Truthy/falsy coercion            |
| `bytes`       | Must be bytes                                 | `encode()` from str              |
| `datetime`    | Must be datetime                              | ISO 8601 parsing from str        |
| `date`        | Must be date                                  | ISO 8601 parsing from str        |
| `Decimal`     | Must be Decimal                               | `Decimal(str(value))`            |
| `UUID`        | Must be UUID                                  | `UUID(value)` from str           |
| `Optional[T]` | `None` is allowed, otherwise validates as `T` | (none)                           |
| `list[T]`     | Each element validated as `T`                 | (none)                           |
| `dict[K, V]`  | Keys validated as `K`, values as `V`          | (none)                           |

### String Constraints

```python
name: str = Field(
    min_length=1,  # Minimum character count
    max_length=100,  # Maximum character count
    pattern=r"^[a-zA-Z]+$",  # Regex pattern (must match)
    strip_whitespace=True,  # Strip leading/trailing whitespace before validation
    to_lower=True,  # Convert to lowercase after validation
    to_upper=True,  # Convert to uppercase after validation
)
```

String length validation uses `validate_string_length()` from the Zig extension, which processes the string in a single pass.

### Numeric Constraints

```python
age: int = Field(
    gt=0,  # Greater than (exclusive)
    ge=0,  # Greater than or equal (inclusive)
    lt=200,  # Less than (exclusive)
    le=150,  # Less than or equal (inclusive)
    multiple_of=5,  # Must be divisible by this value
)

price: float = Field(ge=0.0, le=999999.99)

amount: Decimal = Field(
    max_digits=10,  # Maximum total digits
    decimal_places=2,  # Maximum decimal places
    ge=Decimal("0.01"),
)
```

Integer range validation uses `validate_int_range()` from the Zig extension -- a single comparison instruction.

### Collection Constraints

```python
tags: list[str] = Field(
    min_length=1,  # Minimum number of items
    max_length=10,  # Maximum number of items
    unique_items=True,  # No duplicate items allowed
)
```

### Field() Full Signature

```python
def Field(
    default=_MISSING,
    *,
    # Default value
    default_factory=None,       # Callable to generate default

    # Aliases
    alias=None,                 # Alias for validation AND serialization
    validation_alias=None,      # Alias used only during validation
    serialization_alias=None,   # Alias used only during serialization

    # Documentation
    title=None,                 # Human-readable title (for JSON schema)
    description=None,           # Human-readable description
    examples=None,              # Example values (for JSON schema)

    # Numeric constraints
    gt=None,                    # Greater than
    ge=None,                    # Greater than or equal
    lt=None,                    # Less than
    le=None,                    # Less than or equal
    multiple_of=None,           # Must be multiple of

    # String/collection constraints
    min_length=None,            # Minimum length
    max_length=None,            # Maximum length
    pattern=None,               # Regex pattern for strings

    # String transforms
    strip_whitespace=None,      # Strip whitespace
    to_lower=None,              # Convert to lowercase
    to_upper=None,              # Convert to uppercase

    # Decimal constraints
    max_digits=None,            # Maximum total digits
    decimal_places=None,        # Maximum decimal places

    # Float constraints
    allow_inf_nan=None,         # Allow infinity and NaN

    # Collection constraints
    unique_items=None,          # Require unique items in lists

    # Type behavior
    strict=None,                # Disable type coercion
    frozen=None,                # Immutable after creation
    validate_default=None,      # Validate default value

    # Serialization control
    exclude=None,               # Exclude from serialization
    include=None,               # Include in serialization
    discriminator=None,         # Tagged union field name
    json_schema_extra=None,     # Extra JSON schema properties
    repr=True,                  # Include in __repr__

    # Dataclass compatibility
    init=None,                  # Include in __init__
    init_var=None,              # Init-only variable
    kw_only=None,               # Keyword-only argument

    # Database metadata (HyperDjango-specific)
    primary_key=False,          # Primary key column
    auto=False,                 # Auto-increment
    unique=False,               # Unique constraint
    index=False,                # Create database index
    foreign_key=None,           # Model class for FK (e.g., User)
    related_name=None,          # Reverse relation name

    # File metadata (HyperDjango-specific)
    upload_to=None,             # Upload directory path
    file_field_type=None,       # "file" or "image"
    allowed_extensions=None,    # Tuple of allowed extensions
) -> FieldInfo
```

### FieldInfo Properties

The `FieldInfo` dataclass stores all field metadata:

| Property             | Type    | Description                                  |
| -------------------- | ------- | -------------------------------------------- |
| `is_required`        | `bool`  | `True` if no default and no default_factory  |
| `get_default()`      | method  | Returns default value (calls factory if set) |
| `default`            | `Any`   | Default value or `_MISSING` sentinel         |
| `annotation`         | `Any`   | Type annotation for the field                |
| All constraint attrs | various | Mirror of `Field()` parameters               |

## Special Types

### Network Types

```python
from hyperdjango.validation.core import EmailStr, HttpUrl, AnyUrl, AnyHttpUrl, NameEmail


class Contact(BaseModel):
    email: EmailStr  # RFC 5322 email validation
    website: HttpUrl  # https:// URL validation
    any_url: AnyUrl  # Any scheme URL
    http_url: AnyHttpUrl  # http:// or https://
    display: NameEmail  # "Display Name <email>" format
```

**EmailStr** validation uses the native Zig SIMD validator when compiled (77ns per email). The validator checks:

- Valid local part (RFC 5322 character set)
- `@` separator present
- Valid domain with at least one dot
- Domain labels follow DNS rules

### Constrained Type Factories

Create reusable constrained types with `con*` functions:

```python
from hyperdjango.validation.core import (
    conint,
    confloat,
    constr,
    conlist,
    conset,
    condate,
    condecimal,
    conbytes,
    confrozenset,
)

# Constrained integer
Score = conint(ge=0, le=100)
EvenNumber = conint(multiple_of=2)

# Constrained float
Percentage = confloat(ge=0.0, le=100.0)

# Constrained string
Username = constr(min_length=3, max_length=20, pattern=r"^[a-zA-Z0-9_]+$")
Slug = constr(pattern=r"^[a-z0-9-]+$", to_lower=True)

# Constrained list
Tags = conlist(str, min_length=1, max_length=10)


# Use in models
class GameScore(BaseModel):
    player: Username
    score: Score
    tags: Tags
```

### Strict Types

Disable type coercion -- values must be the exact type:

```python
from hyperdjango.validation.core import (
    StrictInt,
    StrictFloat,
    StrictStr,
    StrictBool,
    StrictBytes,
)


class Config(BaseModel):
    count: StrictInt  # "42" will NOT be coerced to 42
    ratio: StrictFloat  # 42 will NOT be coerced to 42.0
    name: StrictStr  # 42 will NOT be coerced to "42"
    flag: StrictBool  # 1 will NOT be coerced to True
```

### Numeric Sign Types

```python
from hyperdjango.validation.core import (
    PositiveInt,  # > 0
    NegativeInt,  # < 0
    NonNegativeInt,  # >= 0
    NonPositiveInt,  # <= 0
    PositiveFloat,  # > 0.0
    NegativeFloat,  # < 0.0
    NonNegativeFloat,  # >= 0.0
    NonPositiveFloat,  # <= 0.0
    FiniteFloat,  # No inf or NaN
)
```

### Constraint Classes

For use with `Annotated` types directly:

```python
from typing import Annotated
from hyperdjango.validation.core import (
    Gt,
    Ge,
    Lt,
    Le,  # Numeric bounds
    MinLength,
    MaxLength,  # Length bounds
    Pattern,  # Regex pattern
    MultipleOf,  # Divisibility
    Strict,  # No coercion
    StripWhitespace,  # Whitespace handling
    ToLower,
    ToUpper,  # Case transforms
    MaxDigits,
    DecimalPlaces,  # Decimal precision
    AllowInfNan,  # Float special values
    UniqueItems,  # Collection uniqueness
    StringConstraints,  # Combined string constraints
)

# Compose constraints with Annotated
Percentage = Annotated[float, Ge(ge=0), Le(le=100)]
ShortString = Annotated[str, MaxLength(max_length=50), StripWhitespace()]
```

## Custom Validators

### Field Validators

Apply custom validation logic to specific fields:

```python
from hyperdjango.validation.core import BaseModel, field_validator


class User(BaseModel):
    name: str
    email: str
    password: str

    @field_validator("name")
    @classmethod
    def name_must_be_alpha(cls, v: str) -> str:
        if not v.replace(" ", "").isalpha():
            raise ValueError("Name must contain only letters")
        return v.title()  # Transform: capitalize each word

    @field_validator("email")
    @classmethod
    def email_must_be_lowercase(cls, v: str) -> str:
        return v.lower()

    @field_validator("password", mode="before")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a digit")
        return v
```

**`mode="before"`**: runs before type coercion and constraint validation. Receives raw input.

**`mode="after"`** (default): runs after type coercion and constraints. Receives validated value.

### Model Validators

Validate relationships between multiple fields:

```python
from hyperdjango.validation.core import BaseModel, model_validator


class DateRange(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def end_after_start(self) -> "DateRange":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class PasswordForm(BaseModel):
    password: str
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self) -> "PasswordForm":
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self
```

### Computed Fields

Properties included in serialization output:

```python
from hyperdjango.validation.core import BaseModel, computed_field


class User(BaseModel):
    first_name: str
    last_name: str

    @computed_field
    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


user = User(first_name="Alice", last_name="Smith")
user.model_dump()
# {"first_name": "Alice", "last_name": "Smith", "full_name": "Alice Smith"}
```

### Private Attributes

Attributes excluded from validation and serialization:

```python
from hyperdjango.validation.core import BaseModel, PrivateAttr


class User(BaseModel):
    name: str
    _login_count: int = PrivateAttr(default=0)
    _internal_id: str = PrivateAttr(default_factory=lambda: str(uuid4()))


user = User(name="Alice")
user._login_count = 5  # Accessible but not in model_dump()
```

## Error Handling

### ValidationError

Raised for a single field validation failure:

```python
from hyperdjango.validation.core import ValidationError

try:
    user = User(name="", email="bad", age=-1)
except ValidationError as e:
    print(e.field)  # "name"
    print(e.message)  # "String length 0 is below minimum 1"
```

### ValidationErrors

Raised when multiple fields fail validation (collects all errors, not just the first):

```python
from hyperdjango.validation.core import ValidationErrors

try:
    user = User(name="A" * 200, email="bad", age=-1)
except ValidationErrors as e:
    for error in e.errors:
        print(f"{error.field}: {error.message}")
    # name: String length 200 exceeds maximum 100
    # email: Invalid email address: 'bad'
    # age: Value -1 must be >= 0
```

## BaseModel API

The `BaseModel` class provides the full Pydantic v2-compatible API:

```python
from hyperdjango.validation.core import BaseModel, ConfigDict


class User(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    name: str
    age: int
```

### Class Methods

| Method                   | Signature                                        | Description                                          |
| ------------------------ | ------------------------------------------------ | ---------------------------------------------------- |
| `model_validate`         | `cls.model_validate(data: dict) -> Self`         | Validate a dict and return model instance            |
| `model_validate_json`    | `cls.model_validate_json(json_str: str) -> Self` | Parse JSON string and validate                       |
| `model_validate_strings` | `cls.model_validate_strings(data: dict) -> Self` | Validate a dict of strings with type coercion        |
| `model_construct`        | `cls.model_construct(**kwargs) -> Self`          | Create without validation (unsafe, for trusted data) |

### Instance Methods

| Method            | Signature                                                                                                                              | Description                             |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| `model_dump`      | `self.model_dump(exclude=None, include=None, by_alias=False, exclude_unset=False, exclude_defaults=False, exclude_none=False) -> dict` | Serialize to dict                       |
| `model_dump_json` | `self.model_dump_json(**kwargs) -> str`                                                                                                | Serialize to JSON string                |
| `model_copy`      | `self.model_copy(update=None) -> Self`                                                                                                 | Create copy with optional field updates |
| `model_post_init` | `self.model_post_init(context) -> None`                                                                                                | Hook called after `__init__`            |

### Class Attributes

| Attribute               | Type                           | Description                    |
| ----------------------- | ------------------------------ | ------------------------------ |
| `model_fields`          | `dict[str, FieldInfo]`         | Map of field name to FieldInfo |
| `model_computed_fields` | `dict[str, ComputedFieldInfo]` | Computed field definitions     |
| `model_config`          | `ConfigDict`                   | Model configuration            |

## SIMD Batch Validation

Validate millions of records per second using SIMD (Single Instruction, Multiple Data) operations in the Zig layer. Batch validators process 4 values simultaneously using `@Vector(4, i64)`:

### Batch Integer Validation

```python
from hyperdjango._hyperdjango_native import validate_batch_int

# Validate 10,000 integers against range [0, 1000]
values = list(range(10000))
results = validate_batch_int(values, min_val=0, max_val=1000)
# Returns list of booleans, True for valid
# Speed: 51.5M ints/sec
```

### Batch String Validation

```python
from hyperdjango._hyperdjango_native import validate_batch_string

names = ["Alice", "Bob", "A" * 200, ""]
results = validate_batch_string(names, min_len=1, max_len=100)
# [True, True, False, False]
```

### Batch Email Validation

```python
from hyperdjango._hyperdjango_native import validate_batch_email

emails = ["alice@example.com", "bad", "bob@test.org"]
results = validate_batch_email(emails)
# [True, False, True]
```

### Batch Model Validation

Validate entire model dictionaries in batch:

```python
from hyperdjango._hyperdjango_native import validate_batch_model

specs = compile_model_specs(User)  # Pre-compile once
users = [
    {"name": "Alice", "age": 30, "email": "alice@example.com"},
    {"name": "", "age": -1, "email": "bad"},
    {"name": "Bob", "age": 25, "email": "bob@test.org"},
]
results = validate_batch_model(specs, users)
# [True, False, True]
# Speed: 13.1M models/sec (8.2x faster than individual validation)
```

### Pattern Matching

SIMD pattern matching processes 16 bytes per cycle using `@Vector(16, u8)`:

```python
# Character class validation patterns:
# \d+       — digits only
# \w+       — word characters (alphanumeric + underscore)
# [a-zA-Z]+ — letters only
# [a-z0-9-]+ — slug characters

# Used internally by Field(pattern=...) when the pattern matches
# a known character class. Complex regex falls back to Python re module.
```

## Performance Benchmarks

### Per-Operation Throughput

| Operation                            | Speed            | Latency |
| ------------------------------------ | ---------------- | ------- |
| Model creation (`init_model_full`)   | 1.6M models/sec  | 0.6 us  |
| Field validation (`validate_field`)  | 6.7M fields/sec  | 149 ns  |
| Model dump (`dump_model_compiled`)   | 2.4M dumps/sec   | 416 ns  |
| Model to JSON (`dump_model_to_json`) | 4.2M models/sec  | 238 ns  |
| JSON to model (`json_loads_model`)   | 1.67M models/sec | 0.6 us  |
| Email validation (SIMD)              | 13M emails/sec   | 77 ns   |

### Batch Throughput

| Operation                               | Speed            |
| --------------------------------------- | ---------------- |
| Batch int validation (SIMD 4-wide)      | 51.5M ints/sec   |
| Batch user validation (dict specs)      | 17.6M users/sec  |
| Batch model validation (compiled specs) | 13.1M models/sec |

### Comparison

| Framework            | Model creation | vs HyperDjango |
| -------------------- | -------------- | -------------- |
| HyperDjango (native) | 1.6M/sec       | baseline       |
| Python dataclasses   | ~370K/sec      | 4.3x slower    |
| Pydantic v2          | ~300K/sec      | 5.3x slower    |
| attrs                | ~400K/sec      | 4.0x slower    |

### Native vs Python Serialization

| Path                                 | Speed     | Speedup  |
| ------------------------------------ | --------- | -------- |
| `model_dump()` + `json.dumps()`      | 2.4M/sec  | baseline |
| `dump_model_to_json()` (native)      | 4.2M/sec  | 1.84x    |
| `json.loads()` + `init_model_full()` | 830K/sec  | baseline |
| `json_loads_model()` (native)        | 1.67M/sec | 2.05x    |

## Form Data Validation with model_validate_strings()

HTML form data arrives as `dict[str, str]` — every value is a string. `model_validate_strings()` coerces string values to the target types declared in the schema (`str` to `int`, `str` to `float`, `str` to `bool`, etc.) before running constraint validation. This eliminates manual `int(request.form["age"])` casting throughout route handlers.

### The validate_form Pattern

The recommended pattern for form-handling routes (used across HyperNews):

```python
from hyperdjango.validation.core import BaseModel, Field, ValidationErrors


class SubmitPostSchema(BaseModel):
    title: str = Field(default="", max_length=300)
    url: str = Field(default="")
    text: str = Field(default="")


async def validate_form(request, schema_cls: type):
    """Parse form data and validate against a BaseModel schema.

    Flattens multi-value form data (dict[str, list[str]]) to single values,
    then uses model_validate_strings() for automatic type coercion
    (str->int, str->bool, etc.) with Zig-accelerated validation.
    """
    raw = await request.form()
    flat: dict[str, str] = {}
    for key, val in raw.items():
        if key == "_csrf_token":
            continue
        if isinstance(val, list):
            flat[key] = val[0] if val else ""
        else:
            flat[key] = val
    return schema_cls.model_validate_strings(flat)
```

Usage in route handlers:

```python
@app.post("/submit")
async def submit_post(request):
    try:
        data = await validate_form(request, SubmitPostSchema)
    except ValidationErrors as e:
        return Response.html(render_form(errors=e.errors), status=422)

    # data.title, data.url, data.text are validated and typed
    await create_post(data.title, data.url, data.text)
    return Response.redirect("/")
```

### Field Defaults with = Field(default=...)

When using `BaseModel` for form schemas (not database models), use `= Field(default=...)` to make fields optional with defaults. The metaclass handles the `Field()` descriptor correctly — fields with defaults do not need to be provided:

```python
class PollVoteSchema(BaseModel):
    poll_id: int = Field(default=0)  # str "42" -> int 42
    option_id: int = Field(default=0)  # str "7" -> int 7


class ProfileSettingsSchema(BaseModel):
    bio: str = Field(default="", max_length=500)
    website: str = Field(default="")
    show_email: bool = Field(default=False)  # str "true" -> bool True
```

### Type Coercion Rules

`model_validate_strings()` applies these coercions from string input:

| Target Type | Input String                   | Result                |
| ----------- | ------------------------------ | --------------------- |
| `int`       | `"42"`                         | `42`                  |
| `float`     | `"3.14"`                       | `3.14`                |
| `bool`      | `"true"`, `"1"`, `"yes"`       | `True`                |
| `bool`      | `"false"`, `"0"`, `"no"`, `""` | `False`               |
| `str`       | `"hello"`                      | `"hello"` (no change) |
