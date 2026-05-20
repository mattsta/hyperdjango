"""
Tests for standalone serializer layer.

- Serialization (obj → dict): read_only, write_only, computed, nested
- Deserialization (input → validated): validation, type coercion, required
- Nested serializers
- Many mode (lists)
- Cross-field validation
- Partial updates
"""

# hyper-test: unit

import sys

results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "\u2713" if condition else "\u2717"
    print(f"  {symbol} {label}")


# ═══════════════════════════════════════════════════════════════════════════
# Basic Serialization
# ═══════════════════════════════════════════════════════════════════════════


@test("serialize: basic dict to output")
def test_serialize_basic():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        id: int = SerializerField()
        username: str = SerializerField()

    data = UserSerializer(obj={"id": 1, "username": "alice"}).data
    check("has id", data["id"] == 1)
    check("has username", data["username"] == "alice")


@test("serialize: write_only excluded from output")
def test_serialize_write_only():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        username: str = SerializerField()
        password: str = SerializerField(write_only=True)

    data = UserSerializer(obj={"username": "alice", "password": "secret123"}).data
    check("username present", "username" in data)
    check("password excluded", "password" not in data)


@test("serialize: read_only appears in output")
def test_serialize_read_only():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        id: int = SerializerField(read_only=True)
        username: str = SerializerField()

    data = UserSerializer(obj={"id": 42, "username": "bob"}).data
    check("id in output", data["id"] == 42)


@test("serialize: computed field via method")
def test_serialize_computed():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        first_name: str = SerializerField()
        last_name: str = SerializerField()
        full_name: str = SerializerField(read_only=True, source="compute_full_name")

        def compute_full_name(self, obj):
            return f"{obj.get('first_name', '')} {obj.get('last_name', '')}".strip()

    data = UserSerializer(obj={"first_name": "Alice", "last_name": "Smith"}).data
    check("computed field", data["full_name"] == "Alice Smith")


@test("serialize: source maps to different attribute")
def test_serialize_source():
    from hyperdjango.serializers import Serializer, SerializerField

    class ProfileSerializer(Serializer):
        name: str = SerializerField(source="username")

    data = ProfileSerializer(obj={"username": "alice"}).data
    check("source mapping", data["name"] == "alice")


@test("serialize: None object returns empty dict")
def test_serialize_none():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        x: int = SerializerField()

    data = S(obj=None).data
    check("empty dict", data == {})


# ═══════════════════════════════════════════════════════════════════════════
# Many Mode
# ═══════════════════════════════════════════════════════════════════════════


@test("serialize: many=True returns list")
def test_serialize_many():
    from hyperdjango.serializers import Serializer, SerializerField

    class ItemSerializer(Serializer):
        id: int = SerializerField()
        name: str = SerializerField()

    items = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c"}]
    data = ItemSerializer(obj=items, many=True).data
    check("list of 3", len(data) == 3)
    check("first item", data[0]["name"] == "a")
    check("third item", data[2]["id"] == 3)


# ═══════════════════════════════════════════════════════════════════════════
# Nested Serializers
# ═══════════════════════════════════════════════════════════════════════════


@test("serialize: nested serializer")
def test_serialize_nested():
    from hyperdjango.serializers import Serializer, SerializerField

    class AuthorSerializer(Serializer):
        id: int = SerializerField()
        name: str = SerializerField()

    class PostSerializer(Serializer):
        id: int = SerializerField()
        title: str = SerializerField()
        author: AuthorSerializer = SerializerField(read_only=True)

    post = {
        "id": 1,
        "title": "Hello World",
        "author": {"id": 10, "name": "Alice"},
    }
    data = PostSerializer(obj=post).data
    check("post title", data["title"] == "Hello World")
    check("author is dict", isinstance(data["author"], dict))
    check("author name", data["author"]["name"] == "Alice")


@test("serialize: nested list")
def test_serialize_nested_list():
    from hyperdjango.serializers import Serializer, SerializerField

    class TagSerializer(Serializer):
        name: str = SerializerField()

    class PostSerializer(Serializer):
        title: str = SerializerField()
        tags: TagSerializer = SerializerField(read_only=True)

    post = {
        "title": "Hello",
        "tags": [{"name": "python"}, {"name": "zig"}],
    }
    data = PostSerializer(obj=post).data
    check("tags is list", isinstance(data["tags"], list))
    check("2 tags", len(data["tags"]) == 2)
    check("first tag", data["tags"][0]["name"] == "python")


# ═══════════════════════════════════════════════════════════════════════════
# Deserialization + Validation
# ═══════════════════════════════════════════════════════════════════════════


@test("validate: valid input passes")
def test_validate_valid():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        username: str = SerializerField(min_length=1, max_length=150)
        email: str = SerializerField(max_length=254)

    s = UserSerializer(input_data={"username": "alice", "email": "alice@example.com"})
    check("is_valid", s.is_valid())
    check("username in validated", s.validated_data["username"] == "alice")
    check("no errors", len(s.errors) == 0)


@test("validate: missing required field")
def test_validate_missing():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        username: str = SerializerField(required=True)
        email: str = SerializerField(required=True)

    s = UserSerializer(input_data={"username": "alice"})
    check("not valid", not s.is_valid())
    check("email error", "email" in s.errors)


@test("validate: optional field with default")
def test_validate_default():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        username: str = SerializerField()
        role: str = SerializerField(required=False, default="user")

    s = UserSerializer(input_data={"username": "alice"})
    check("valid", s.is_valid())
    check("default applied", s.validated_data.get("role") == "user")


@test("validate: read_only excluded from input")
def test_validate_read_only_excluded():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        id: int = SerializerField(read_only=True)
        username: str = SerializerField()

    s = UserSerializer(input_data={"id": 999, "username": "alice"})
    check("valid", s.is_valid())
    check("id not in validated", "id" not in s.validated_data)
    check("username in validated", s.validated_data["username"] == "alice")


@test("validate: write_only accepted in input")
def test_validate_write_only():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        username: str = SerializerField()
        password: str = SerializerField(write_only=True, min_length=8)

    s = UserSerializer(input_data={"username": "alice", "password": "secure123"})
    check("valid", s.is_valid())
    check("password in validated", s.validated_data["password"] == "secure123")


@test("validate: min_length violation")
def test_validate_min_length():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        name: str = SerializerField(min_length=3)

    s = S(input_data={"name": "ab"})
    check("not valid", not s.is_valid())
    check("name error", "name" in s.errors)
    check("error mentions min", "3" in s.errors["name"])


@test("validate: max_length violation")
def test_validate_max_length():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        name: str = SerializerField(max_length=5)

    s = S(input_data={"name": "toolong"})
    check("not valid", not s.is_valid())
    check("name error", "name" in s.errors)


@test("validate: min_value / max_value")
def test_validate_numeric():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        age: int = SerializerField(min_value=0, max_value=150)

    s1 = S(input_data={"age": 25})
    check("25 valid", s1.is_valid())

    s2 = S(input_data={"age": -1})
    check("-1 invalid", not s2.is_valid())

    s3 = S(input_data={"age": 200})
    check("200 invalid", not s3.is_valid())


@test("validate: choices constraint")
def test_validate_choices():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        role: str = SerializerField(choices=["admin", "editor", "viewer"])

    s1 = S(input_data={"role": "admin"})
    check("admin valid", s1.is_valid())

    s2 = S(input_data={"role": "superuser"})
    check("superuser invalid", not s2.is_valid())


@test("validate: type coercion int from string")
def test_validate_coerce_int():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        age: int = SerializerField()

    s = S(input_data={"age": "25"})
    check("valid", s.is_valid())
    check("coerced to int", s.validated_data["age"] == 25)


@test("validate: type coercion float from string")
def test_validate_coerce_float():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        price: float = SerializerField()

    s = S(input_data={"price": "9.99"})
    check("valid", s.is_valid())
    check("coerced to float", s.validated_data["price"] == 9.99)


@test("validate: type coercion bool from string")
def test_validate_coerce_bool():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        active: bool = SerializerField()

    s1 = S(input_data={"active": "true"})
    check("'true' → True", s1.is_valid() and s1.validated_data["active"] is True)

    s2 = S(input_data={"active": "false"})
    check("'false' → False", s2.is_valid() and s2.validated_data["active"] is False)


@test("validate: invalid type coercion")
def test_validate_bad_coerce():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        count: int = SerializerField()

    s = S(input_data={"count": "not_a_number"})
    check("not valid", not s.is_valid())
    check("count error", "count" in s.errors)


@test("validate: cross-field validation")
def test_validate_cross_field():
    from hyperdjango.serializers import Serializer, SerializerField

    class PasswordSerializer(Serializer):
        password: str = SerializerField(min_length=8)
        confirm: str = SerializerField(min_length=8)

        def validate(self, data):
            if data.get("password") != data.get("confirm"):
                raise ValueError("Passwords do not match")
            return data

    s1 = PasswordSerializer(
        input_data={"password": "secure123", "confirm": "secure123"}
    )
    check("matching valid", s1.is_valid())

    s2 = PasswordSerializer(
        input_data={"password": "secure123", "confirm": "different"}
    )
    check("mismatch invalid", not s2.is_valid())
    check("error in __all__", "__all__" in s2.errors)


@test("validate: partial mode skips missing fields")
def test_validate_partial():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        username: str = SerializerField(required=True)
        email: str = SerializerField(required=True)

    s = UserSerializer(input_data={"email": "new@example.com"}, partial=True)
    check("partial valid", s.is_valid())
    check("only email in validated", "email" in s.validated_data)
    check("username not required in partial", "username" not in s.validated_data)


@test("validate: no input data")
def test_validate_no_input():
    from hyperdjango.serializers import Serializer, SerializerField

    class S(Serializer):
        x: int = SerializerField()

    s = S(input_data=None)
    check("not valid", not s.is_valid())
    check("has error", "__all__" in s.errors)


# ═══════════════════════════════════════════════════════════════════════════
# Object Serialization (non-dict)
# ═══════════════════════════════════════════════════════════════════════════


@test("serialize: from object with attributes")
def test_serialize_object():
    from hyperdjango.serializers import Serializer, SerializerField

    class UserObj:
        def __init__(self):
            self.id = 1
            self.username = "alice"
            self.email = "alice@example.com"

    class UserSerializer(Serializer):
        id: int = SerializerField()
        username: str = SerializerField()

    data = UserSerializer(obj=UserObj()).data
    check("id from attr", data["id"] == 1)
    check("username from attr", data["username"] == "alice")


# ═══════════════════════════════════════════════════════════════════════════
# Inheritance
# ═══════════════════════════════════════════════════════════════════════════


@test("inheritance: child serializer inherits parent fields")
def test_inheritance():
    from hyperdjango.serializers import Serializer, SerializerField

    class BaseSerializer(Serializer):
        id: int = SerializerField(read_only=True)
        created_at: str = SerializerField(read_only=True)

    class UserSerializer(BaseSerializer):
        username: str = SerializerField()
        email: str = SerializerField()

    fields = UserSerializer._serializer_fields
    check("has id from parent", "id" in fields)
    check("has created_at from parent", "created_at" in fields)
    check("has username from child", "username" in fields)
    check("has email from child", "email" in fields)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


def main():
    print(f"\n{'=' * 60}")
    print("Standalone Serializer Tests")
    print(f"{'=' * 60}\n")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            func()
        except Exception as e:
            check(f"EXCEPTION: {e}", False)
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed:")
        for label, ok in results:
            if not ok:
                print(f"  \u2717 {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
