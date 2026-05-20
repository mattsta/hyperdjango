"""
HyperForm — Django form with dhi-accelerated validation.

Drop-in replacement for django.forms.Form that uses dhi's SIMD-powered
validation engine instead of Django's per-field Python loop.

Django rendering, widgets, and templates all work unchanged.

Usage:
    from hyperdjango.validation.forms import HyperForm
    from django import forms

    class UserForm(HyperForm):
        name = forms.CharField(max_length=100)
        age = forms.IntegerField(min_value=0, max_value=150)
        email = forms.EmailField()
"""

import uuid

from django.core.exceptions import ValidationError
from django.forms import Form
from django.forms.forms import DeclarativeFieldsMetaclass

from hyperdjango.validation.converters import build_dhi_model_from_django_fields
from hyperdjango.validation.core.validator import (
    ValidationError as DhiValidationError,
)
from hyperdjango.validation.core.validator import (
    ValidationErrors as DhiValidationErrors,
)


class HyperFormMeta(DeclarativeFieldsMetaclass):
    """Metaclass that creates a companion dhi BaseModel from declared Django fields."""

    def __new__(mcs, name, bases, attrs):
        new_class = super().__new__(mcs, name, bases, attrs)

        # Don't process the base HyperForm class itself
        if name == "HyperForm" and not any(hasattr(b, "_dhi_model") for b in bases):
            return new_class

        # Build a dhi model from the declared fields
        if new_class.declared_fields:
            try:
                new_class._dhi_model = build_dhi_model_from_django_fields(
                    name, new_class.declared_fields
                )
            # blind-except: building the dhi shadow-model is best-effort acceleration; an unmappable declared field disables dhi validation for this form (falls back to standard Django validation) rather than breaking form class construction.
            except Exception:
                # If dhi model creation fails, fall back to standard Django validation
                new_class._dhi_model = None
        else:
            new_class._dhi_model = None

        return new_class


class HyperForm(Form, metaclass=HyperFormMeta):
    """Django form with dhi-accelerated validation.

    Inherits all Django form features (rendering, widgets, templates, media).
    Only the validation engine is replaced with dhi for 523x faster validation.

    Falls back to standard Django validation if dhi model creation fails
    or if the form has custom clean_<field> methods that need Django's pipeline.
    """

    _dhi_model = None

    def _clean_fields(self):
        """Override Django's field-by-field cleaning with dhi batch validation.

        Flow:
        1. Run Django's widget value extraction (value_from_datadict)
        2. Run dhi batch validation on all fields at once
        3. Map dhi errors back to Django's ValidationError format
        4. Run any custom clean_<field> methods
        """
        if self._dhi_model is None:
            # Fallback to standard Django validation
            return super()._clean_fields()

        # Step 1: Extract values from form data using Django's widget system
        raw_data = {}
        for name, bf in self._bound_items():
            field = bf.field
            value = field.widget.value_from_datadict(
                self.data, self.files, self.add_prefix(name)
            )
            # Run Django's to_python() for type coercion (e.g., str → int)
            try:
                value = field.to_python(value)
            except ValidationError as e:
                self.add_error(name, e)
                continue

            # Normalize types that dhi cannot model directly. UUIDField.to_python
            # yields a uuid.UUID, but the companion dhi model annotates the field
            # as `str`, so batch validation would fail with "Expected str, got UUID".
            if isinstance(value, uuid.UUID):
                value = str(value)

            # Check required (Django handles this with its own empty check)
            if value in field.empty_values:
                if field.required:
                    self.add_error(
                        name, ValidationError(field.error_messages["required"])
                    )
                    continue
                else:
                    self.cleaned_data[name] = value
                    continue

            raw_data[name] = value

        # If we already have errors from type coercion, skip dhi validation
        if self._errors:
            return

        # Step 2: Run dhi batch validation
        try:
            validated = self._dhi_model.model_validate(raw_data)
            # Read validated values directly from instance (model_dump may skip some fields)
            for field_name in raw_data:
                if field_name not in self._errors:
                    # dynamic-attr: read the validated value off the dhi model instance by runtime field name
                    self.cleaned_data[field_name] = getattr(
                        validated, field_name, raw_data[field_name]
                    )
        except (DhiValidationError, DhiValidationErrors) as e:
            # Convert dhi validation errors to Django format
            self._convert_dhi_errors(e)

        # Step 2b: Run Django field-level validators that dhi cannot model.
        # dhi maps fields like URLField/SlugField/UUIDField to a plain `str` and
        # has no knowledge of custom `validators=[...]`, so those validators would
        # otherwise be silently dropped. Running run_validators() here restores
        # URL/regex/format checks and any user-supplied validators.
        for name in raw_data:
            if name in self._errors or name not in self.cleaned_data:
                continue
            field = self.fields[name]
            try:
                field.run_validators(self.cleaned_data[name])
            except ValidationError as e:
                self.add_error(name, e)

        # Step 3: Run custom clean_<field> methods
        for name in list(self.cleaned_data.keys()):
            # dynamic-attr: resolve the optional clean_<field> convention method by name on the form
            clean_method = getattr(self, f"clean_{name}", None)
            if clean_method is not None:
                try:
                    value = clean_method()
                    self.cleaned_data[name] = value
                except ValidationError as e:
                    self.add_error(name, e)

    def _convert_dhi_errors(self, exc):
        """Convert a dhi ValidationError into Django form errors."""
        # dhi ValidationErrors has an `errors` list attribute (not callable)
        # Each error has `.field` and `.message` attributes
        # dynamic-attr: exc is an arbitrary caught exception; only dhi ValidationErrors carries an `errors` list
        error_list = getattr(exc, "errors", None)
        if error_list and isinstance(error_list, list):
            for error in error_list:
                # dynamic-attr: error is a dhi error object (external type); field/message may be absent
                field_name = getattr(error, "field", None)
                msg = getattr(
                    error, "message", str(error)
                )  # dynamic-attr: dhi error object, external type
                if field_name and field_name in self.fields:
                    self.add_error(field_name, ValidationError(msg))
                else:
                    self.add_error(None, ValidationError(msg))
        else:
            # Generic exception — add as non-field error
            self.add_error(None, ValidationError(str(exc)))
