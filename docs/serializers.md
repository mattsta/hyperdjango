# Serializers

Standalone API serializer layer for shaping request/response data. Supports read/write separation, nested serialization, computed fields, validation, and type coercion.

## Quick Start

```python
from hyperdjango.serializers import Serializer, SerializerField


class UserSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    username: str = SerializerField(min_length=1, max_length=150)
    email: str = SerializerField(max_length=254)
    password: str = SerializerField(write_only=True, min_length=8)


# Serialize (object -> dict for API response)
serializer = UserSerializer(
    obj={"id": 1, "username": "alice", "email": "a@b.com", "password": "hashed"}
)
data = serializer.data
# {"id": 1, "username": "alice", "email": "a@b.com"}  -- password excluded (write_only)

# Deserialize (input dict -> validated data)
serializer = UserSerializer(
    input_data={"username": "alice", "email": "a@b.com", "password": "secret123"}
)
if serializer.is_valid():
    clean = serializer.validated_data
    # {"username": "alice", "email": "a@b.com", "password": "secret123"}  -- id excluded (read_only)
else:
    errors = serializer.errors
```

## SerializerField Options

```python
SerializerField(
    read_only=False,  # Include in output only (not accepted in input)
    write_only=False,  # Accept in input only (not in output)
    required=True,  # Must be present in input (ignored for read_only)
    default=None,  # Default value when missing from input
    source="get_name",  # Source attribute or method name on the object
    min_length=None,  # Minimum string length
    max_length=None,  # Maximum string length
    min_value=None,  # Minimum numeric value
    max_value=None,  # Maximum numeric value
    choices=None,  # Allowed values list
    label=None,  # Human-readable label (for OpenAPI)
    help_text=None,  # Description (for OpenAPI)
)
```

## Computed Fields

Use the `source` parameter to point to a method on the serializer.

```python
class UserSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    first_name: str = SerializerField()
    last_name: str = SerializerField()
    full_name: str = SerializerField(read_only=True, source="compute_full_name")

    def compute_full_name(self, obj):
        return f"{obj.get('first_name', '')} {obj.get('last_name', '')}".strip()


serializer = UserSerializer(obj={"id": 1, "first_name": "Alice", "last_name": "Smith"})
serializer.data["full_name"]  # "Alice Smith"
```

## Nested Serializers

Annotate a field with another Serializer subclass for nested serialization.

```python
class AddressSerializer(Serializer):
    city: str = SerializerField()
    country: str = SerializerField()


class UserSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    name: str = SerializerField()
    address: AddressSerializer = SerializerField(read_only=True)


user = {"id": 1, "name": "Alice", "address": {"city": "NYC", "country": "US"}}
serializer = UserSerializer(obj=user)
serializer.data
# {"id": 1, "name": "Alice", "address": {"city": "NYC", "country": "US"}}
```

Nested lists are handled automatically when the source value is a list.

## Many Mode

Serialize/deserialize lists of objects.

```python
users = [
    {"id": 1, "username": "alice", "email": "a@b.com"},
    {"id": 2, "username": "bob", "email": "b@b.com"},
]
serializer = UserSerializer(obj=users, many=True)
data = serializer.data  # List of dicts
```

## Validation

### Field-Level Validation

Constraints are checked automatically during `is_valid()`:

```python
class ProductSerializer(Serializer):
    name: str = SerializerField(min_length=1, max_length=200)
    price: float = SerializerField(min_value=0)
    status: str = SerializerField(choices=["draft", "active", "archived"])


serializer = ProductSerializer(
    input_data={"name": "", "price": -5, "status": "invalid"}
)
serializer.is_valid()  # False
serializer.errors
# {"name": "Minimum length is 1", "price": "Minimum value is 0",
#  "status": "Must be one of: ['draft', 'active', 'archived']"}
```

### Cross-Field Validation

Override `validate()` for multi-field checks.

```python
class DateRangeSerializer(Serializer):
    start: str = SerializerField()
    end: str = SerializerField()

    def validate(self, data):
        if data["start"] > data["end"]:
            raise ValueError("start must be before end")
        return data
```

## Type Coercion

Input values are automatically coerced to match the annotated type:

| Annotation | Coercion                            |
| ---------- | ----------------------------------- |
| `int`      | `int(value)`                        |
| `float`    | `float(value)`                      |
| `bool`     | String `"true"/"1"/"yes"` -> `True` |
| `str`      | `str(value)`                        |

Invalid coercions produce an error (e.g., `"abc"` for `int`).

## Partial Updates

Skip required field checks for PATCH-style updates.

```python
serializer = UserSerializer(
    input_data={"email": "new@example.com"},
    partial=True,  # Only validate fields that are present
)
if serializer.is_valid():
    # validated_data only contains "email"
    pass
```

## Context

Pass extra data to computed fields via context.

```python
serializer = UserSerializer(obj=user, context={"request": request})
# Access in compute methods: self.context["request"]
```

## Inheritance

Serializer fields are inherited from parent classes.

```python
class BaseSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    created_at: str = SerializerField(read_only=True)


class UserSerializer(BaseSerializer):
    username: str = SerializerField()
    email: str = SerializerField()
    # Inherits id and created_at from BaseSerializer
```

## API Reference

### Serializer

| Property/Method   | Description                                      |
| ----------------- | ------------------------------------------------ |
| `.data`           | Serialized output (dict or list for many=True)   |
| `.is_valid()`     | Validate input_data, returns bool                |
| `.validated_data` | Validated input (call `is_valid()` first)        |
| `.errors`         | Validation errors dict (call `is_valid()` first) |
| `.validate(data)` | Override for cross-field validation              |

## Serializer Inheritance

```python
class BaseSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    created_at: str = SerializerField(read_only=True)


class ArticleSerializer(BaseSerializer):
    title: str = SerializerField(max_length=200)
    content: str = SerializerField()
    # Inherits id and created_at from BaseSerializer
```

## Field Subsetting

Control which fields appear in output:

```python
class UserSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    username: str = SerializerField()
    email: str = SerializerField()
    bio: str = SerializerField()


# Full serialization
full = UserSerializer(obj=user)
# {"id": 1, "username": "alice", "email": "a@b.com", "bio": "..."}


# For list views, use a separate lightweight serializer
class UserListSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    username: str = SerializerField()
```

## Partial Updates

```python
serializer = ArticleSerializer(input_data={"title": "New Title"}, partial=True)
if serializer.is_valid():
    # Only "title" in validated_data — other fields not required
    await update_article(article_id, serializer.validated_data)
```

## Computed Fields

```python
class ArticleSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    title: str = SerializerField()
    summary: str = SerializerField(read_only=True, source="compute_summary")

    def compute_summary(self, obj):
        content = obj.get("content", "") if isinstance(obj, dict) else obj.content
        return content[:100] + "..." if len(content) > 100 else content
```
