"""Tests for HyperForm — dhi-accelerated Django form validation."""

import uuid

from django import forms
from django.core.exceptions import ValidationError

from hyperdjango.validation.forms import HyperForm


class TestHyperFormBasic:
    """Test basic HyperForm functionality."""

    def test_simple_form_valid(self):
        class UserForm(HyperForm):
            name = forms.CharField(max_length=100)
            age = forms.IntegerField(min_value=0, max_value=150)

        form = UserForm(data={"name": "Alice", "age": "25"})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["name"] == "Alice"
        assert form.cleaned_data["age"] == 25

    def test_simple_form_invalid(self):
        class UserForm(HyperForm):
            name = forms.CharField(max_length=5)

        form = UserForm(data={"name": "This is way too long"})
        assert not form.is_valid()
        assert "name" in form.errors

    def test_required_field_missing(self):
        class UserForm(HyperForm):
            name = forms.CharField()

        form = UserForm(data={})
        assert not form.is_valid()
        assert "name" in form.errors

    def test_optional_field(self):
        class UserForm(HyperForm):
            name = forms.CharField()
            bio = forms.CharField(required=False)

        form = UserForm(data={"name": "Alice"})
        assert form.is_valid(), form.errors

    def test_email_field(self):
        class ContactForm(HyperForm):
            email = forms.EmailField()

        form = ContactForm(data={"email": "alice@example.com"})
        assert form.is_valid(), form.errors

    def test_email_field_invalid(self):
        class ContactForm(HyperForm):
            email = forms.EmailField()

        form = ContactForm(data={"email": "not-an-email"})
        assert not form.is_valid()

    def test_integer_bounds(self):
        class AgeForm(HyperForm):
            age = forms.IntegerField(min_value=0, max_value=150)

        # Valid
        form = AgeForm(data={"age": "25"})
        assert form.is_valid(), form.errors

        # Too low
        form = AgeForm(data={"age": "-1"})
        assert not form.is_valid()

    def test_multiple_fields(self):
        class RegistrationForm(HyperForm):
            username = forms.CharField(min_length=3, max_length=50)
            email = forms.EmailField()
            age = forms.IntegerField(min_value=13)
            agree = forms.BooleanField()

        form = RegistrationForm(
            data={
                "username": "alice",
                "email": "alice@example.com",
                "age": "25",
                "agree": "true",
            }
        )
        assert form.is_valid(), form.errors


class TestHyperFormCustomClean:
    """Test that custom clean_<field> methods still work."""

    def test_custom_clean_method(self):
        class UserForm(HyperForm):
            name = forms.CharField(max_length=100)

            def clean_name(self):
                name = self.cleaned_data.get("name", "")
                return name.strip().title()

        form = UserForm(data={"name": "  alice  "})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["name"] == "Alice"

    def test_custom_clean_raises_error(self):
        class UserForm(HyperForm):
            name = forms.CharField(max_length=100)

            def clean_name(self):
                name = self.cleaned_data.get("name", "")
                if name.lower() == "admin":
                    raise ValidationError("Name 'admin' is reserved")
                return name

        form = UserForm(data={"name": "admin"})
        assert not form.is_valid()
        assert "name" in form.errors


class TestHyperFormFieldValidationNotDropped:
    """Regression: validation dhi cannot model must not be silently dropped.

    Previously HyperForm overrode _clean_fields() and only ran field.to_python()
    plus dhi batch validation, which skipped Django's field validators. That
    silently dropped URL/format checks and any custom validators=[...], and made
    UUIDField always fail (to_python -> uuid.UUID vs the dhi str annotation).
    """

    def test_uuid_field_accepts_valid_uuid(self):
        class UF(HyperForm):
            uid = forms.UUIDField()

        value = str(uuid.uuid4())
        form = UF(data={"uid": value})
        assert form.is_valid(), form.errors
        # UUID is normalized to str so the dhi model accepts it.
        assert form.cleaned_data["uid"] == value

    def test_url_field_rejects_non_url(self):
        class URLForm(HyperForm):
            link = forms.URLField()

        form = URLForm(data={"link": "not a url"})
        assert not form.is_valid()
        assert "link" in form.errors

    def test_url_field_accepts_valid_url(self):
        class URLForm(HyperForm):
            link = forms.URLField()

        form = URLForm(data={"link": "https://example.com"})
        assert form.is_valid(), form.errors

    def test_custom_validators_run(self):
        def no_foo(value):
            if value == "foo":
                raise ValidationError("'foo' is not allowed")

        class CF(HyperForm):
            name = forms.CharField(validators=[no_foo])

        rejected = CF(data={"name": "foo"})
        assert not rejected.is_valid()
        assert "name" in rejected.errors

        accepted = CF(data={"name": "bar"})
        assert accepted.is_valid(), accepted.errors


class TestHyperFormInheritance:
    """Test form inheritance works correctly."""

    def test_inherited_form(self):
        class BaseForm(HyperForm):
            name = forms.CharField(max_length=100)

        class ExtendedForm(BaseForm):
            email = forms.EmailField()

        form = ExtendedForm(data={"name": "Alice", "email": "alice@example.com"})
        assert form.is_valid(), form.errors
        assert "name" in form.cleaned_data
        assert "email" in form.cleaned_data


class TestHyperFormDhiModel:
    """Test that the dhi model is correctly generated."""

    def test_dhi_model_created(self):
        class UserForm(HyperForm):
            name = forms.CharField(max_length=100)

        assert UserForm._dhi_model is not None

    def test_dhi_model_is_basemodel(self):
        from hyperdjango.validation import core as _vc

        class UserForm(HyperForm):
            name = forms.CharField(max_length=100)

        assert issubclass(UserForm._dhi_model, _vc.BaseModel)

    def test_empty_form_no_dhi_model(self):
        class EmptyForm(HyperForm):
            pass

        assert EmptyForm._dhi_model is None
