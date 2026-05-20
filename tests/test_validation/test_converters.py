"""Tests for Django field → dhi type conversion."""

from typing import get_origin

import pytest
from django import forms

from hyperdjango.validation.converters import (
    build_dhi_model_from_django_fields,
    django_form_field_to_dhi,
)


class TestCharFieldConversion:
    def test_basic_charfield(self):
        field = forms.CharField()
        result = django_form_field_to_dhi(field)
        assert result is str

    def test_charfield_with_max_length(self):
        field = forms.CharField(max_length=100)
        result = django_form_field_to_dhi(field)
        # Should be Annotated[str, Field(max_length=100)]
        assert result is not str  # Should have annotation

    def test_charfield_with_min_and_max(self):
        field = forms.CharField(min_length=1, max_length=100)
        result = django_form_field_to_dhi(field)
        assert result is not str

    def test_optional_charfield(self):
        field = forms.CharField(required=False)
        result = django_form_field_to_dhi(field)
        # Should be Optional[str]
        origin = get_origin(result)
        assert origin is not None or result is (str | None)


class TestIntFieldConversion:
    def test_basic_intfield(self):
        field = forms.IntegerField()
        result = django_form_field_to_dhi(field)
        assert result is int

    def test_intfield_with_bounds(self):
        field = forms.IntegerField(min_value=0, max_value=150)
        result = django_form_field_to_dhi(field)
        assert result is not int  # Should have annotations


class TestEmailFieldConversion:
    def test_email_field(self):
        field = forms.EmailField()
        result = django_form_field_to_dhi(field)
        # Should map to EmailStr
        assert result is not str


class TestBooleanFieldConversion:
    def test_boolean_field(self):
        field = forms.BooleanField()
        result = django_form_field_to_dhi(field)
        assert result is bool


class TestDecimalFieldConversion:
    def test_decimal_field(self):
        field = forms.DecimalField(max_digits=10, decimal_places=2)
        result = django_form_field_to_dhi(field)
        assert result is not float  # Should have annotations


class TestBuildDhiModel:
    def test_build_simple_model(self):
        fields = {
            "name": forms.CharField(max_length=100),
            "age": forms.IntegerField(min_value=0),
            "email": forms.EmailField(),
        }
        model = build_dhi_model_from_django_fields("TestUser", fields)

        # Verify the model is a dhi BaseModel subclass
        from hyperdjango.validation import core as _vc

        assert issubclass(model, _vc.BaseModel)

    def test_model_validates_valid_data(self):
        fields = {
            "name": forms.CharField(max_length=100),
            "age": forms.IntegerField(),
        }
        model = build_dhi_model_from_django_fields("TestModel", fields)

        # Should validate successfully
        instance = model.model_validate({"name": "Alice", "age": 25})
        assert instance.name == "Alice"
        assert instance.age == 25

    def test_model_rejects_invalid_data(self):
        fields = {
            "name": forms.CharField(max_length=5),
        }
        model = build_dhi_model_from_django_fields("TestModel2", fields)

        # Should reject string longer than max_length
        with pytest.raises(Exception):
            model.model_validate({"name": "This is way too long"})

    def test_model_with_optional_field(self):
        fields = {
            "name": forms.CharField(max_length=100),
            "bio": forms.CharField(required=False),
        }
        model = build_dhi_model_from_django_fields("TestOptional", fields)

        # Should accept without optional field
        instance = model.model_validate({"name": "Alice"})
        assert instance.name == "Alice"
