"""Tests for self-contained validation engine (hyperdjango.validation.core)."""

from typing import Annotated

import pytest

from hyperdjango.validation.core import (
    BaseModel,
    EmailStr,
    Field,
    Ge,
    Gt,
    Le,
    Lt,
    MaxLength,
    MinLength,
    NegativeInt,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    ValidationErrors,
    conint,
    constr,
)


class TestBaseModelBasics:
    def test_create(self):
        class User(BaseModel):
            name: str
            age: int

        u = User(name="Alice", age=25)
        assert u.name == "Alice"
        assert u.age == 25

    def test_model_dump(self):
        class User(BaseModel):
            name: str
            age: int

        u = User(name="Bob", age=30)
        d = u.model_dump()
        assert d["name"] == "Bob"
        assert d["age"] == 30

    def test_model_validate(self):
        class User(BaseModel):
            name: str
            age: int

        u = User.model_validate({"name": "Eve", "age": 20})
        assert u.name == "Eve"
        assert u.age == 20

    def test_missing_required_field(self):
        class User(BaseModel):
            name: str

        with pytest.raises((ValidationError, ValidationErrors)):
            User()

    def test_wrong_type(self):
        class User(BaseModel):
            age: int

        with pytest.raises((ValidationError, ValidationErrors, TypeError)):
            User(age="not_a_number")


class TestFieldConstraints:
    def test_gt(self):
        class M(BaseModel):
            v: Annotated[int, Gt(gt=0)]

        M(v=1)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=0)

    def test_ge(self):
        class M(BaseModel):
            v: Annotated[int, Ge(ge=0)]

        M(v=0)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=-1)

    def test_lt(self):
        class M(BaseModel):
            v: Annotated[int, Lt(lt=100)]

        M(v=99)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=100)

    def test_le(self):
        class M(BaseModel):
            v: Annotated[int, Le(le=100)]

        M(v=100)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=101)

    def test_max_length(self):
        class M(BaseModel):
            s: Annotated[str, MaxLength(max_length=5)]

        M(s="hello")
        with pytest.raises((ValidationError, ValidationErrors)):
            M(s="toolong")

    def test_min_length(self):
        class M(BaseModel):
            s: Annotated[str, MinLength(min_length=3)]

        M(s="abc")
        with pytest.raises((ValidationError, ValidationErrors)):
            M(s="ab")

    def test_field_function_constraints(self):
        class M(BaseModel):
            name: Annotated[str, Field(max_length=10)]
            age: Annotated[int, Field(ge=0, le=150)]

        M(name="Alice", age=25)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(name="way too long name", age=25)


class TestEmailStr:
    def test_valid_email(self):
        class M(BaseModel):
            email: EmailStr

        m = M(email="alice@example.com")
        assert m.email == "alice@example.com"

    def test_invalid_email(self):
        class M(BaseModel):
            email: EmailStr

        with pytest.raises((ValidationError, ValidationErrors)):
            M(email="not-an-email")


class TestOptionalFields:
    def test_optional_none(self):
        class M(BaseModel):
            name: str | None = None

        m = M()
        assert m.name is None

    def test_optional_with_value(self):
        class M(BaseModel):
            name: str | None = None

        m = M(name="Alice")
        assert m.name == "Alice"


class TestTypeAliases:
    def test_positive_int(self):
        class M(BaseModel):
            v: PositiveInt

        M(v=1)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=0)

    def test_negative_int(self):
        class M(BaseModel):
            v: NegativeInt

        M(v=-1)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=0)

    def test_non_negative_int(self):
        class M(BaseModel):
            v: NonNegativeInt

        M(v=0)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=-1)


class TestConTypeFactories:
    def test_conint(self):
        MyInt = conint(ge=0, le=100)

        class M(BaseModel):
            v: MyInt

        M(v=50)
        with pytest.raises((ValidationError, ValidationErrors)):
            M(v=101)

    def test_constr(self):
        MyStr = constr(min_length=2, max_length=10)

        class M(BaseModel):
            s: MyStr

        M(s="hello")
        with pytest.raises((ValidationError, ValidationErrors)):
            M(s="x")


class TestNestedModels:
    def test_nested(self):
        class Address(BaseModel):
            city: str

        class User(BaseModel):
            name: str
            address: Address

        u = User(name="Alice", address=Address(city="NYC"))
        assert u.address.city == "NYC"

    def test_nested_from_dict(self):
        class Address(BaseModel):
            city: str

        class User(BaseModel):
            name: str
            address: Address

        u = User.model_validate({"name": "Bob", "address": {"city": "LA"}})
        assert u.address.city == "LA"


class TestValidationErrors:
    def test_error_has_field_name(self):
        class M(BaseModel):
            name: Annotated[str, Field(max_length=3)]

        try:
            M(name="toolong")
            assert False, "Should have raised"
        except (ValidationError, ValidationErrors) as e:
            err_str = str(e)
            assert "name" in err_str

    def test_validation_error_class(self):
        e = ValidationError("email", "invalid format")
        assert e.field == "email"
        assert e.message == "invalid format"
