"""
Tests for OpenAPI 3.1 schema generation with Serializer integration.
"""

# hyper-test: unit

import sys

from hyperdjango.openapi import (
    _convert_path,
    _python_type_to_schema,
    api_input,
    api_output,
    serializer_to_schema,
)
from hyperdjango.serializers import Serializer, SerializerField

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        errors.append(msg)
        print(msg)


# ── Type mapping ───────────────────────────────────────────────────────────

print("=== Type Mapping ===")

check("str_to_string", _python_type_to_schema(str) == {"type": "string"})
check("int_to_integer", _python_type_to_schema(int) == {"type": "integer"})
check("float_to_number", _python_type_to_schema(float)["type"] == "number")
check("bool_to_boolean", _python_type_to_schema(bool) == {"type": "boolean"})

# ── Path conversion ────────────────────────────────────────────────────────

print("\n=== Path Conversion ===")

check("simple_path", _convert_path("/users") == "/users")
check("int_param", _convert_path("/users/{id:int}") == "/users/{id}")
check("str_param", _convert_path("/articles/{slug:str}") == "/articles/{slug}")
check("path_param", _convert_path("/files/{path:path}") == "/files/{path}")
check(
    "multi_params",
    _convert_path("/users/{uid:int}/posts/{pid:int}") == "/users/{uid}/posts/{pid}",
)
check("no_type", _convert_path("/items/{id}") == "/items/{id}")

# ── Serializer to Schema ──────────────────────────────────────────────────

print("\n=== Serializer to Schema ===")


class UserSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    username: str = SerializerField(min_length=1, max_length=150)
    email: str = SerializerField(
        max_length=254, label="Email Address", help_text="User's email"
    )
    password: str = SerializerField(write_only=True, min_length=8)
    role: str = SerializerField(choices=["admin", "user", "moderator"])


# Output schema (response)
output = serializer_to_schema(UserSerializer, mode="output")
check("output_type", output["type"] == "object")
check("output_has_id", "id" in output["properties"])
check("output_has_username", "username" in output["properties"])
check("output_has_email", "email" in output["properties"])
check(
    "output_no_password", "password" not in output["properties"]
)  # write_only excluded
check("output_has_role", "role" in output["properties"])

# Check constraints on output
email_prop = output["properties"]["email"]
check("email_max_length", email_prop.get("maxLength") == 254)
check("email_label", email_prop.get("title") == "Email Address")
check("email_help", email_prop.get("description") == "User's email")

role_prop = output["properties"]["role"]
check("role_enum", role_prop.get("enum") == ["admin", "user", "moderator"])

# Input schema (request)
input_schema = serializer_to_schema(UserSerializer, mode="input")
check("input_no_id", "id" not in input_schema["properties"])  # read_only excluded
check("input_has_password", "password" in input_schema["properties"])
check("input_has_username", "username" in input_schema["properties"])
check("input_required", "username" in input_schema.get("required", []))
check("input_password_required", "password" in input_schema.get("required", []))

# Constraint on input
username_prop = input_schema["properties"]["username"]
check("username_min_length", username_prop.get("minLength") == 1)
check("username_max_length", username_prop.get("maxLength") == 150)

password_prop = input_schema["properties"]["password"]
check("password_min_length", password_prop.get("minLength") == 8)

# ── Nested Serializers ─────────────────────────────────────────────────────

print("\n=== Nested Serializers ===")


class AddressSerializer(Serializer):
    street: str = SerializerField()
    city: str = SerializerField()
    zip_code: str = SerializerField(max_length=10)


class ProfileSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    name: str = SerializerField(max_length=100)
    address: AddressSerializer = SerializerField(read_only=True)


schemas: dict[str, dict[str, object]] = {}
profile_schema = serializer_to_schema(ProfileSerializer, mode="output", schemas=schemas)

check("nested_ref", "$ref" in str(profile_schema["properties"].get("address", {})))
# Nested components are mode-scoped: an output-mode parent nests the
# output-mode component ("AddressSerializerOutput"), so an input parent can
# never $ref an output-shaped nested component (and vice versa).
check("nested_schema_created", "AddressSerializerOutput" in schemas)
check(
    "nested_ref_is_mode_scoped",
    profile_schema["properties"]["address"]
    == {"$ref": "#/components/schemas/AddressSerializerOutput"},
)
check("nested_has_street", "street" in schemas["AddressSerializerOutput"]["properties"])
check("nested_has_city", "city" in schemas["AddressSerializerOutput"]["properties"])
check(
    "nested_zip_max",
    schemas["AddressSerializerOutput"]["properties"]["zip_code"].get("maxLength") == 10,
)

# ── Nested serializer mode isolation (regression) ──────────────────────────
# A nested serializer used by BOTH an input and an output parent must resolve
# to two DIFFERENT, mode-correct components. Previously nested components were
# keyed by bare class name, so whichever mode was generated first "won" and the
# other mode's $ref silently pointed at a wrong-shaped component (read-only
# fields shown writable, write-only fields omitted).

print("\n=== Nested Serializer Mode Isolation ===")


class CredentialSerializer(Serializer):
    token: str = SerializerField(read_only=True)  # output only
    secret: str = SerializerField(write_only=True)  # input only
    label: str = SerializerField()  # both


class AccountSerializer(Serializer):
    cred: CredentialSerializer = SerializerField()


mode_schemas: dict[str, dict[str, object]] = {}
account_in = serializer_to_schema(AccountSerializer, mode="input", schemas=mode_schemas)
account_out = serializer_to_schema(
    AccountSerializer, mode="output", schemas=mode_schemas
)

# The parent's nested $ref must be mode-scoped.
check(
    "input_parent_refs_input_nested",
    account_in["properties"]["cred"]
    == {"$ref": "#/components/schemas/CredentialSerializerInput"},
)
check(
    "output_parent_refs_output_nested",
    account_out["properties"]["cred"]
    == {"$ref": "#/components/schemas/CredentialSerializerOutput"},
)
# Both mode components exist and are distinct.
check("input_nested_component", "CredentialSerializerInput" in mode_schemas)
check("output_nested_component", "CredentialSerializerOutput" in mode_schemas)

cred_in_props = mode_schemas["CredentialSerializerInput"]["properties"]
cred_out_props = mode_schemas["CredentialSerializerOutput"]["properties"]

# Input component: write_only present, read_only absent.
check("input_nested_has_write_only", "secret" in cred_in_props)
check("input_nested_omits_read_only", "token" not in cred_in_props)
# Output component: read_only present, write_only absent.
check("output_nested_has_read_only", "token" in cred_out_props)
check("output_nested_omits_write_only", "secret" not in cred_out_props)


# ── Default values ─────────────────────────────────────────────────────────

print("\n=== Defaults ===")


class ItemSerializer(Serializer):
    name: str = SerializerField(max_length=200)
    quantity: int = SerializerField(default=1, required=False)
    active: bool = SerializerField(default=True, required=False)


item_input = serializer_to_schema(ItemSerializer, mode="input")
check("default_quantity", item_input["properties"]["quantity"].get("default") == 1)
check("default_active", item_input["properties"]["active"].get("default") is True)
check("required_name", "name" in item_input.get("required", []))
check("not_required_quantity", "quantity" not in item_input.get("required", []))

# ── Numeric constraints ───────────────────────────────────────────────────

print("\n=== Numeric Constraints ===")


class ProductSerializer(Serializer):
    name: str = SerializerField()
    price: float = SerializerField(min_value=0.01, max_value=99999.99)
    stock: int = SerializerField(min_value=0)


product = serializer_to_schema(ProductSerializer, mode="output")
price_prop = product["properties"]["price"]
check("price_minimum", price_prop.get("minimum") == 0.01)
check("price_maximum", price_prop.get("maximum") == 99999.99)
check("price_type", price_prop.get("type") == "number")

stock_prop = product["properties"]["stock"]
check("stock_minimum", stock_prop.get("minimum") == 0)
check("stock_type", stock_prop.get("type") == "integer")

# ── api_input / api_output decorators ──────────────────────────────────────

print("\n=== API Decorators ===")


@api_input(UserSerializer)
async def create_user(request):
    pass


@api_output(ProfileSerializer)
async def get_profile(request, id: int):
    pass


check("api_input_attr", create_user.__openapi_request__ is UserSerializer)
check("api_output_attr", get_profile.__openapi_response__ is ProfileSerializer)

# ── Serializer inheritance ─────────────────────────────────────────────────

print("\n=== Inheritance ===")


class BaseSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    created_at: str = SerializerField(read_only=True)


class ArticleSerializer(BaseSerializer):
    title: str = SerializerField(max_length=200)
    content: str = SerializerField()


article_output = serializer_to_schema(ArticleSerializer, mode="output")
check("inherited_id", "id" in article_output["properties"])
check("inherited_created_at", "created_at" in article_output["properties"])
check("own_title", "title" in article_output["properties"])
check("own_content", "content" in article_output["properties"])

article_input = serializer_to_schema(ArticleSerializer, mode="input")
check("input_no_id", "id" not in article_input["properties"])
check("input_no_created_at", "created_at" not in article_input["properties"])
check("input_has_title", "title" in article_input["properties"])

# ── PublicIDSerializer ─────────────────────────────────────────────────────

print("\n=== PublicIDSerializer ===")

from hyperdjango.serializers import PublicIDSerializer


class ArticlePubSerializer(PublicIDSerializer):
    title: str = SerializerField()
    views: int = SerializerField(read_only=True)


pub_output = serializer_to_schema(ArticlePubSerializer, mode="output")
check("pub_id_output", "id" in pub_output["properties"])
check("pub_title_output", "title" in pub_output["properties"])
check("pub_views_output", "views" in pub_output["properties"])

pub_input = serializer_to_schema(ArticlePubSerializer, mode="input")
check("pub_no_id_input", "id" not in pub_input["properties"])
check("pub_no_views_input", "views" not in pub_input["properties"])
check("pub_title_input", "title" in pub_input["properties"])

# ── Empty / edge cases ─────────────────────────────────────────────────────

print("\n=== Edge Cases ===")


# Empty serializer
class EmptySerializer(Serializer):
    pass


empty = serializer_to_schema(EmptySerializer)
check("empty_type", empty["type"] == "object")
check(
    "empty_no_required", "required" not in empty or len(empty.get("required", [])) == 0
)

# Non-serializer
check("non_serializer", serializer_to_schema(str) == {"type": "object"})

# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"OpenAPI tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

sys.exit(0 if failed == 0 else 1)
