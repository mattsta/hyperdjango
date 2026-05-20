"""Regression tests locking in the getattr/setattr -> direct-access removals.

Each removal in this sweep replaced a defensive ``getattr(cls, "__dhi_...__",
default)`` with direct attribute access, which is only correct because the
attribute is *always present* (set on the ``BaseModel`` base by ``_ModelMeta``
and/or by ``__init_subclass__`` / ``_compile_model_fields`` on every subclass).

These tests exercise the exact code paths that read those attributes directly:
- the STANDARD (non-native) ``__init__`` path reads
  ``__dhi_field_validator_funcs__`` / ``__dhi_model_validators_before__`` /
  ``__dhi_model_validators_after__`` (model.py) — only reached when a class has
  custom validators;
- ``model_dump`` reads ``__dhi_computed_fields__``;
- ``_init_private_attrs`` reads ``__dhi_private_attrs__``;
- ``model_validate(..., from_attributes=True)`` reflects over a source object;
- ``model_rebuild`` gates on ``__dhi_fields__`` (kept as getattr — absent on base).

A bare ``BaseModel()`` is also constructed to prove the base-class defaults
(added so direct access is well-defined even with no fields) hold.
"""

from hyperdjango.validation.core import (
    BaseModel,
    ValidationErrors,
    field_validator,
    model_validator,
)
from hyperdjango.validation.core.functional_validators import computed_field


class TestFieldAndModelValidators:
    """Standard __init__ path reads the validator-func dunders by direct access."""

    def test_field_validator_runs(self):
        class User(BaseModel):
            name: str

            @field_validator("name")
            @classmethod
            def _upper(cls, v):
                if len(v) < 2:
                    raise ValueError("too short")
                return v.upper()

        assert User(name="al").name == "AL"

    def test_field_validator_rejects(self):
        class User(BaseModel):
            name: str

            @field_validator("name")
            @classmethod
            def _check(cls, v):
                if len(v) < 2:
                    raise ValueError("too short")
                return v

        # Reaching the field-validator loop requires __dhi_field_validator_funcs__
        # to be read by direct access in the STANDARD __init__ path.
        try:
            User(name="a")
        except ValueError, ValidationErrors:
            pass
        else:
            raise AssertionError("expected validation failure")

    def test_model_validator_after(self):
        # A model with any custom validator forces the STANDARD __init__ path,
        # which reads __dhi_model_validators_before__/after__ by direct access.
        class Range(BaseModel):
            lo: int
            hi: int

            @model_validator(mode="after")
            def _ordered(self):
                if self.lo >= self.hi:
                    raise ValueError("lo must be < hi")
                return self

        r = Range(lo=1, hi=4)
        assert (r.lo, r.hi) == (1, 4)

        try:
            Range(lo=5, hi=2)
        except ValidationErrors, ValueError:
            pass
        else:
            raise AssertionError("expected validation failure")


class TestComputedFields:
    """model_dump reads __dhi_computed_fields__ by direct access."""

    def test_computed_field_in_dump(self):
        class Person(BaseModel):
            first: str
            last: str

            @computed_field
            @property
            def full(self) -> str:
                return f"{self.first} {self.last}"

        p = Person(first="Ada", last="Lovelace")
        assert p.full == "Ada Lovelace"
        # by_alias=True forces the Python dump path, which reads
        # __dhi_computed_fields__ by direct access and emits the computed field.
        dumped = p.model_dump(by_alias=True)
        assert dumped["full"] == "Ada Lovelace"


class TestPrivateAttrs:
    """_init_private_attrs reads __dhi_private_attrs__ by direct access."""

    def test_private_attr_default(self):
        from hyperdjango.validation.core.functional_validators import PrivateAttr

        class Widget(BaseModel):
            name: str
            _hits: int = PrivateAttr(default=0)

        # Construction runs _init_private_attrs, which reads __dhi_private_attrs__
        # by direct access and populates __pydantic_private__ with the defaults.
        w = Widget(name="x")
        assert w.__pydantic_private__ == {"_hits": 0}
        assert "_hits" not in w.model_dump()


class TestFromAttributes:
    """model_validate(from_attributes=True) reflects over an arbitrary object."""

    def test_orm_mode(self):
        class Source:
            def __init__(self):
                self.name = "Bea"
                self.age = 40

        class U(BaseModel):
            name: str
            age: int

        u = U.model_validate(Source(), from_attributes=True)
        assert (u.name, u.age) == ("Bea", 40)


class TestModelRebuild:
    """model_rebuild gates on getattr(cls, '__dhi_fields__') (kept + justified)."""

    def test_rebuild_is_noop_when_compiled(self):
        class M(BaseModel):
            x: int

        # Already compiled -> returns None (skip) without raising.
        assert M.model_rebuild() is None


class TestHyperModelPrimaryKeySkip:
    """model_validation.py replaced getattr(field, 'primary_key', False) with
    direct field.primary_key on Django local_fields (always present). This
    exercises both the class-build path and full_clean's per-field loop."""

    def test_autofield_pk_excluded_and_full_clean(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.db import models as dj_models

        from hyperdjango.validation.model_validation import (
            HyperModel,
            _build_dhi_model_for_django_model,
        )

        class Account(HyperModel):
            name = dj_models.CharField(max_length=50)
            balance = dj_models.PositiveIntegerField(default=0)

            class Meta:
                app_label = "tests"

        # Line 54 path: build the dhi model over the model's local_fields. The
        # AutoField pk ('id') is skipped via field.primary_key; declared fields
        # are kept. (A missing primary_key attribute would raise AttributeError.)
        dhi = _build_dhi_model_for_django_model("Account", Account)
        assert "id" not in dhi.__dhi_field_names__
        assert set(dhi.__dhi_field_names__) == {"name", "balance"}

        # Line 126 path: full_clean's per-field loop also reads field.primary_key
        # to skip the AutoField pk. Pin a known-good dhi model so the dhi branch
        # runs, then a valid instance validates and a bad value is rejected.
        Account._dhi_model = dhi
        Account(name="ok", balance=5).full_clean(validate_unique=False)
        try:
            Account(name="bad", balance=-1).full_clean(validate_unique=False)
        except DjangoValidationError:
            pass
        else:
            raise AssertionError("expected DjangoValidationError")


class TestBareBaseModelDefaults:
    """Base-class defaults make direct attribute access well-defined with no fields."""

    def test_bare_base_dunders_present(self):
        # These are read by direct access in __init__/model_dump; the base must
        # define them so a fields-less class is well-defined.
        assert BaseModel.__dhi_computed_fields__ == {}
        assert BaseModel.__dhi_field_validator_funcs__ == {}
        assert BaseModel.__dhi_model_validators_before__ == []
        assert BaseModel.__dhi_model_validators_after__ == []
        assert BaseModel.__dhi_private_attrs__ == {}
        assert BaseModel.__dhi_has_custom_validators__ is False
        assert BaseModel.__dhi_compiled_specs__ is None

    def test_bare_base_absent_dunder(self):
        # __dhi_fields__ is intentionally NOT set on the base (its absence is the
        # signal _is_basemodel_subclass keys on), so model_rebuild must getattr it.
        assert not hasattr(BaseModel, "__dhi_fields__")
