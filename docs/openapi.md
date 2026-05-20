# OpenAPI & API Documentation

Auto-generate OpenAPI 3.1 specs and interactive Swagger UI from your routes and serializers.

## Quick Start

```python
from hyperdjango import HyperApp
from hyperdjango.openapi import mount_docs

app = HyperApp("My API")
mount_docs(app)

# GET /docs         → Swagger UI (interactive API explorer)
# GET /openapi.json → OpenAPI 3.1 JSON spec
```

## Serializer-Driven Schemas

The OpenAPI generator reads Serializer field metadata to produce accurate request/response schemas with constraints, types, and descriptions.

### Defining Serializers

```python
from hyperdjango.serializers import Serializer, SerializerField


class UserSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    username: str = SerializerField(min_length=1, max_length=150)
    email: str = SerializerField(
        max_length=254,
        label="Email Address",
        help_text="User's primary email",
    )
    password: str = SerializerField(write_only=True, min_length=8)
    role: str = SerializerField(choices=["admin", "user", "moderator"])
```

### Binding Serializers to Routes

Use `@api_input` and `@api_output` decorators to bind serializers to routes:

```python
from hyperdjango.openapi import api_input, api_output


@app.post("/users")
@api_input(UserCreateSerializer)
@api_output(UserSerializer)
async def create_user(request):
    data = await request.json()
    serializer = UserCreateSerializer(input_data=data)
    if serializer.is_valid():
        user = await User.objects.create(**serializer.validated_data)
        return UserSerializer(obj=user).data
    return Response.json(serializer.errors, status=400)


@app.get("/users/{id:int}")
@api_output(UserSerializer)
async def get_user(request, id: int):
    user = await User.objects.get(id=id)
    return UserSerializer(obj=user).data
```

### Generated Schema

The UserSerializer above produces:

**Response schema** (excludes write_only fields):

```json
{
  "type": "object",
  "properties": {
    "id": { "type": "integer" },
    "username": { "type": "string", "minLength": 1, "maxLength": 150 },
    "email": {
      "type": "string",
      "maxLength": 254,
      "title": "Email Address",
      "description": "User's primary email"
    },
    "role": { "type": "string", "enum": ["admin", "user", "moderator"] }
  }
}
```

**Request schema** (excludes read_only fields, includes required array):

```json
{
  "type": "object",
  "properties": {
    "username": { "type": "string", "minLength": 1, "maxLength": 150 },
    "email": { "type": "string", "maxLength": 254 },
    "password": { "type": "string", "minLength": 8 },
    "role": { "type": "string", "enum": ["admin", "user", "moderator"] }
  },
  "required": ["username", "email", "password", "role"]
}
```

Note how `id` is excluded from input (read_only) and `password` is excluded from output (write_only).

## Nested Serializers

Nested serializers generate `$ref` components:

```python
class AddressSerializer(Serializer):
    street: str = SerializerField()
    city: str = SerializerField()
    zip_code: str = SerializerField(max_length=10)


class ProfileSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    name: str = SerializerField(max_length=100)
    address: AddressSerializer = SerializerField(read_only=True)
```

Generates:

```json
{
  "ProfileSerializerOutput": {
    "type": "object",
    "properties": {
      "id": { "type": "integer" },
      "name": { "type": "string", "maxLength": 100 },
      "address": { "$ref": "#/components/schemas/AddressSerializer" }
    }
  },
  "AddressSerializer": {
    "type": "object",
    "properties": {
      "street": { "type": "string" },
      "city": { "type": "string" },
      "zip_code": { "type": "string", "maxLength": 10 }
    }
  }
}
```

## Field Constraints

All SerializerField constraints are mapped to OpenAPI:

| SerializerField Option | OpenAPI Schema                            |
| ---------------------- | ----------------------------------------- |
| `min_length=N`         | `minLength: N`                            |
| `max_length=N`         | `maxLength: N`                            |
| `min_value=N`          | `minimum: N`                              |
| `max_value=N`          | `maximum: N`                              |
| `choices=[...]`        | `enum: [...]`                             |
| `label="..."`          | `title: "..."`                            |
| `help_text="..."`      | `description: "..."`                      |
| `default=value`        | `default: value` (input schemas only)     |
| `read_only=True`       | Excluded from input schema                |
| `write_only=True`      | Excluded from output schema               |
| `required=True`        | Added to `required` array (input schemas) |

## Type Mapping

| Python Type | OpenAPI Type | Format      |
| ----------- | ------------ | ----------- |
| `str`       | `string`     | —           |
| `int`       | `integer`    | —           |
| `float`     | `number`     | `double`    |
| `bool`      | `boolean`    | —           |
| `datetime`  | `string`     | `date-time` |
| `date`      | `string`     | `date`      |
| `time`      | `string`     | `time`      |
| `Decimal`   | `string`     | `decimal`   |
| `UUID`      | `string`     | `uuid`      |
| `bytes`     | `string`     | `binary`    |
| `dict`      | `object`     | —           |
| `list`      | `array`      | —           |

## Path Parameters

Path parameters from URL patterns are auto-detected with correct types:

```python
@app.get("/users/{id:int}")
async def get_user(request, id: int): ...
```

Generates:

```json
{
  "parameters": [
    {
      "name": "id",
      "in": "path",
      "required": true,
      "schema": { "type": "integer" }
    }
  ]
}
```

## Error Responses

Standard error responses are auto-generated:

- **400** — Validation error (for POST/PUT/PATCH)
- **404** — Not found (for all routes)
- POST routes get **201** instead of **200**
- DELETE routes get **204** (no body)

## Authentication Schemes

The generator auto-detects auth middleware and adds security scheme definitions:

```json
{
  "components": {
    "securitySchemes": {
      "sessionAuth": {
        "type": "apiKey",
        "in": "cookie",
        "name": "session"
      },
      "apiKeyAuth": {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key"
      }
    }
  }
}
```

## Tags

Routes are auto-tagged by their first URL segment:

- `/users/...` → tag: `users`
- `/articles/...` → tag: `articles`
- `/api/v1/...` → tag: `api`

## Customization

### mount_docs() Options

```python
mount_docs(
    app,
    path="/docs",  # Swagger UI URL
    openapi_path="/openapi.json",  # JSON spec URL
    title="My API",  # Override app title
    version="2.0.0",  # API version
    description="Production API for My Service",
)
```

### Programmatic Spec Generation

```python
from hyperdjango.openapi import generate_openapi

spec = generate_openapi(
    app,
    title="My API",
    version="1.0.0",
    description="API documentation",
)

# spec is a dict — serialize to JSON, YAML, or use directly
import json

print(json.dumps(spec, indent=2))
```

### serializer_to_schema()

Convert any Serializer to an OpenAPI schema programmatically:

```python
from hyperdjango.openapi import serializer_to_schema

# Output schema (for responses)
output = serializer_to_schema(UserSerializer, mode="output")

# Input schema (for requests)
input_schema = serializer_to_schema(UserSerializer, mode="input")
```

## Complete Example

```python
from hyperdjango import HyperApp, Response
from hyperdjango.serializers import Serializer, SerializerField, PublicIDSerializer
from hyperdjango.openapi import mount_docs, api_input, api_output
from hyperdjango.shortcuts import get_object_or_404

app = HyperApp("Blog API", database="postgres://localhost/blog")


# Serializers
class ArticleCreateSerializer(Serializer):
    title: str = SerializerField(min_length=1, max_length=200, label="Title")
    content: str = SerializerField(label="Content", help_text="Markdown supported")
    published: bool = SerializerField(default=False)


class ArticleSerializer(PublicIDSerializer):
    title: str = SerializerField()
    content: str = SerializerField()
    published: bool = SerializerField()
    views: int = SerializerField(read_only=True)
    created_at: str = SerializerField(read_only=True)


# Routes
@app.get("/articles")
@api_output(ArticleSerializer)
async def list_articles(request):
    """List all published articles."""
    articles = await Article.objects.filter(published=True).all()
    return ArticleSerializer(obj=articles, many=True).data


@app.post("/articles")
@api_input(ArticleCreateSerializer)
@api_output(ArticleSerializer)
async def create_article(request):
    """Create a new article."""
    serializer = ArticleCreateSerializer(input_data=await request.json())
    if not serializer.is_valid():
        return Response.json(serializer.errors, status=400)
    article = await Article.objects.create(**serializer.validated_data)
    return Response.json(ArticleSerializer(obj=article).data, status=201)


@app.get("/articles/{id:int}")
@api_output(ArticleSerializer)
async def get_article(request, id: int):
    """Get a single article by ID."""
    article = await get_object_or_404(Article, id=id)
    return ArticleSerializer(obj=article).data


# Mount docs
mount_docs(app, title="Blog API", version="1.0.0")

app.run()
```

Visit `http://localhost:8000/docs` to see the interactive Swagger UI with full schema documentation.
