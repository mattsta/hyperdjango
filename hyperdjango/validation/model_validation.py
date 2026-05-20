"""
HyperModel — Django model mixin with dhi-accelerated validation.

Adds dhi validation to Django's Model.full_clean() for faster
field constraint checking.

Usage:
    from django.db import models
    from hyperdjango.validation.model_validation import HyperModel

    class User(HyperModel, models.Model):
        name = models.CharField(max_length=100)
        age = models.PositiveIntegerField()
        email = models.EmailField()
"""

from typing import Annotated

from django.core.exceptions import ValidationError
from django.db import models

from hyperdjango.validation.converters import django_model_field_to_dhi
from hyperdjango.validation.core import BaseModel as _BaseModel
from hyperdjango.validation.core import Field as DhiField
from hyperdjango.validation.core.validator import (
    ValidationError as DhiValidationError,
)
from hyperdjango.validation.core.validator import (
    ValidationErrors as DhiValidationErrors,
)


class HyperModelMeta(type(models.Model)):
    """Metaclass that builds a dhi model from Django model fields at class creation."""

    def __new__(mcs, name, bases, attrs, **kwargs):
        new_class = super().__new__(mcs, name, bases, attrs, **kwargs)

        # Skip abstract models and the HyperModel base itself
        # dynamic-attr: during ModelBase construction _meta may not yet be set on the class
        if getattr(new_class, "_meta", None) and new_class._meta.abstract:
            return new_class

        # Build dhi model from concrete fields
        try:
            dhi_model = _build_dhi_model_for_django_model(name, new_class)
            new_class._dhi_model = dhi_model
        # blind-except: building the dhi shadow-model is best-effort acceleration; an unmappable field type disables dhi validation for this model (falls back to Django's) rather than breaking Django model class construction.
        except Exception:
            new_class._dhi_model = None

        return new_class


def _build_dhi_model_for_django_model(name, model_class):
    """Build a dhi BaseModel from a Django model's fields."""
    annotations = {}
    defaults = {}

    for field in model_class._meta.local_fields:
        # Skip auto fields (pk, auto-generated). local_fields are concrete
        # Django Field instances, which always define .primary_key.
        if field.primary_key and isinstance(
            field, (models.AutoField, models.BigAutoField, models.SmallAutoField)
        ):
            continue

        base_type, field_kwargs = django_model_field_to_dhi(field)

        # Handle nullable fields
        if field.null or field.blank:
            if not _is_optional(base_type):
                base_type = base_type | None
            defaults[field.name] = None

        # Handle field defaults
        if field.has_default():
            default = field.default
            if callable(default):
                defaults[field.name] = None  # Can't use callables in dhi
            else:
                defaults[field.name] = default

        if field_kwargs:
            annotations[field.name] = Annotated[base_type, DhiField(**field_kwargs)]
        else:
            annotations[field.name] = base_type

    if not annotations:
        return None

    namespace = {"__annotations__": annotations, **defaults}
    return type(f"{name}DhiModel", (_BaseModel,), namespace)


def _is_optional(tp):
    """Check if a type is Optional[X]."""
    # dynamic-attr: tp is an arbitrary annotation; __origin__ exists only on typing generics
    origin = getattr(tp, "__origin__", None)
    if origin is not None:
        # dynamic-attr: __args__ exists only on parameterized typing generics
        args = getattr(tp, "__args__", ())
        return type(None) in args
    return False


class HyperModel(models.Model):
    """Django model mixin with dhi-accelerated validation.

    Overrides full_clean() to run dhi validation first (fast path),
    then falls back to Django's standard validation for database-level checks.
    """

    _dhi_model = None

    class Meta:
        abstract = True

    def full_clean(self, exclude=None, validate_unique=True):
        """Run dhi validation, then Django's standard full_clean().

        dhi handles field-level constraint validation (type, min/max, length, etc.)
        much faster. Django's full_clean() still runs for:
        - Database-level unique constraints
        - Custom clean() methods
        - Cross-field validation
        """
        errors = {}
        exclude = exclude or []

        # Step 1: Run dhi validation (fast path)
        if self._dhi_model is not None:
            field_data = {}
            for field in self._meta.local_fields:
                if field.name in exclude:
                    continue
                # local_fields are concrete Django Field instances → .primary_key always present
                if field.primary_key and isinstance(
                    field,
                    (models.AutoField, models.BigAutoField, models.SmallAutoField),
                ):
                    continue
                try:
                    # dynamic-attr: field.attname is a runtime column name read off the model instance
                    value = getattr(self, field.attname)
                    field_data[field.name] = value
                except AttributeError:
                    pass

            try:
                self._dhi_model.model_validate(field_data)
            except (DhiValidationError, DhiValidationErrors) as e:
                dhi_errors = self._convert_dhi_errors_to_django(e)
                errors.update(dhi_errors)

        # Step 2: Run Django's standard validation (for DB constraints, unique, etc.)
        try:
            super().full_clean(exclude=exclude, validate_unique=validate_unique)
        except ValidationError as e:
            # Merge Django errors with dhi errors
            django_errors = e.message_dict if hasattr(e, "message_dict") else {}
            for field, msgs in django_errors.items():
                if field not in errors:
                    errors[field] = msgs

        if errors:
            raise ValidationError(errors)

    @staticmethod
    def _convert_dhi_errors_to_django(exc) -> dict[str, list[str]]:
        """Convert dhi validation errors to Django's {field: [messages]} format."""
        errors = {}
        # dhi ValidationErrors has an `errors` list attribute (not callable)
        # Each error has `.field` and `.message` attributes
        # dynamic-attr: exc is an arbitrary caught exception; only dhi ValidationErrors carries an `errors` list
        error_list = getattr(exc, "errors", None)
        if error_list and isinstance(error_list, list):
            for error in error_list:
                # dynamic-attr: error is a dhi error object (external type); field/message may be absent
                field = getattr(error, "field", "__all__")
                msg = getattr(
                    error, "message", str(error)
                )  # dynamic-attr: dhi error object, external type
                errors.setdefault(field or "__all__", []).append(msg)
        else:
            errors["__all__"] = [str(exc)]
        return errors
