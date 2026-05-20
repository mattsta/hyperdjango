"""
Django field → dhi type annotation converters.

Maps Django form fields and model fields to dhi-compatible type annotations
so that dhi's SIMD-accelerated validation can replace Django's per-field Python loop.
"""

import datetime
from typing import Annotated, Any

from django import forms
from django.core import validators as django_validators

from hyperdjango.validation.core import BaseModel as _BaseModel
from hyperdjango.validation.core import EmailStr
from hyperdjango.validation.core import Field as DhiField


def _extract_min_max_from_validators(django_field):
    """Extract min_value/max_value from a Django field's validators."""
    min_val = None
    max_val = None
    for v in django_field.validators:
        if isinstance(v, django_validators.MinValueValidator):
            min_val = v.limit_value
        elif isinstance(v, django_validators.MaxValueValidator):
            max_val = v.limit_value
        elif isinstance(v, django_validators.MinLengthValidator):
            min_val = v.limit_value
        elif isinstance(v, django_validators.MaxLengthValidator):
            max_val = v.limit_value
    return min_val, max_val


def django_form_field_to_dhi(django_field):
    """Convert a Django form field to a dhi-compatible type annotation.

    Returns:
        A type annotation suitable for use in a dhi BaseModel's __annotations__.
    """
    field_class = type(django_field)
    required = django_field.required

    # CharField family
    if isinstance(django_field, forms.EmailField):
        return EmailStr if required else EmailStr | None

    if isinstance(django_field, forms.URLField):
        kwargs = {}
        if django_field.min_length is not None:
            kwargs["min_length"] = django_field.min_length
        if django_field.max_length is not None:
            kwargs["max_length"] = django_field.max_length
        base = Annotated[str, DhiField(**kwargs)] if kwargs else str
        return base if required else base | None

    if isinstance(django_field, forms.SlugField):
        kwargs = {}
        if django_field.max_length is not None:
            kwargs["max_length"] = django_field.max_length
        kwargs["pattern"] = r"^[-a-zA-Z0-9_]+$"
        base = Annotated[str, DhiField(**kwargs)]
        return base if required else base | None

    if isinstance(django_field, (forms.CharField, forms.RegexField)):
        kwargs = {}
        if django_field.min_length is not None:
            kwargs["min_length"] = django_field.min_length
        if django_field.max_length is not None:
            kwargs["max_length"] = django_field.max_length
        if isinstance(django_field, forms.RegexField):
            kwargs["pattern"] = django_field.regex.pattern
        base = Annotated[str, DhiField(**kwargs)] if kwargs else str
        return base if required else base | None

    # Numeric fields
    if isinstance(django_field, forms.IntegerField):
        kwargs = {}
        if hasattr(django_field, "min_value") and django_field.min_value is not None:
            kwargs["ge"] = django_field.min_value
        if hasattr(django_field, "max_value") and django_field.max_value is not None:
            kwargs["le"] = django_field.max_value
        base = Annotated[int, DhiField(**kwargs)] if kwargs else int
        return base if required else base | None

    if isinstance(django_field, forms.FloatField):
        kwargs = {}
        if hasattr(django_field, "min_value") and django_field.min_value is not None:
            kwargs["ge"] = django_field.min_value
        if hasattr(django_field, "max_value") and django_field.max_value is not None:
            kwargs["le"] = django_field.max_value
        base = Annotated[float, DhiField(**kwargs)] if kwargs else float
        return base if required else base | None

    if isinstance(django_field, forms.DecimalField):
        kwargs = {}
        if django_field.max_digits is not None:
            kwargs["max_digits"] = django_field.max_digits
        if django_field.decimal_places is not None:
            kwargs["decimal_places"] = django_field.decimal_places
        if hasattr(django_field, "min_value") and django_field.min_value is not None:
            kwargs["ge"] = float(django_field.min_value)
        if hasattr(django_field, "max_value") and django_field.max_value is not None:
            kwargs["le"] = float(django_field.max_value)
        base = Annotated[float, DhiField(**kwargs)] if kwargs else float
        return base if required else base | None

    # Boolean
    if isinstance(django_field, forms.NullBooleanField):
        return bool | None
    if isinstance(django_field, forms.BooleanField):
        return bool

    # Date/Time
    if isinstance(django_field, forms.DateTimeField):
        return datetime.datetime if required else datetime.datetime | None
    if isinstance(django_field, forms.DateField):
        return datetime.date if required else datetime.date | None
    if isinstance(django_field, forms.TimeField):
        return datetime.time if required else datetime.time | None

    # UUID
    if isinstance(django_field, forms.UUIDField):
        return str if required else str | None

    # JSON
    if isinstance(django_field, forms.JSONField):
        return Any

    # Fallback: treat as string
    return str if required else str | None


def django_model_field_to_dhi(model_field) -> tuple[type, dict]:
    """Convert a Django model field to a dhi-compatible (type, kwargs) pair.

    Returns:
        (python_type, field_kwargs) where field_kwargs can be passed to dhi.Field().
    """
    from django.db import models

    kwargs = {}
    base_type = str  # default fallback

    # String fields
    if isinstance(model_field, models.EmailField):
        return (EmailStr, kwargs)

    if isinstance(model_field, (models.CharField, models.SlugField)):
        base_type = str
        if model_field.max_length is not None:
            kwargs["max_length"] = model_field.max_length
        return (base_type, kwargs)

    if isinstance(model_field, models.TextField):
        return (str, kwargs)

    # Numeric fields
    if isinstance(model_field, models.SmallIntegerField):
        return (int, kwargs)

    if isinstance(
        model_field,
        (
            models.PositiveIntegerField,
            models.PositiveBigIntegerField,
            models.PositiveSmallIntegerField,
        ),
    ):
        kwargs["ge"] = 0
        return (int, kwargs)

    if isinstance(
        model_field, (models.IntegerField, models.BigIntegerField, models.AutoField)
    ):
        return (int, kwargs)

    if isinstance(model_field, models.FloatField):
        return (float, kwargs)

    if isinstance(model_field, models.DecimalField):
        if model_field.max_digits is not None:
            kwargs["max_digits"] = model_field.max_digits
        if model_field.decimal_places is not None:
            kwargs["decimal_places"] = model_field.decimal_places
        return (float, kwargs)

    # Boolean
    if isinstance(model_field, models.BooleanField):
        return (bool, kwargs)
    if isinstance(model_field, models.NullBooleanField):
        return (bool | None, kwargs)

    # Date/Time
    if isinstance(model_field, models.DateTimeField):
        return (datetime.datetime, kwargs)
    if isinstance(model_field, models.DateField):
        return (datetime.date, kwargs)
    if isinstance(model_field, models.TimeField):
        return (datetime.time, kwargs)

    # UUID
    if isinstance(model_field, models.UUIDField):
        return (str, kwargs)

    # JSON
    if isinstance(model_field, models.JSONField):
        return (Any, kwargs)

    # Binary
    if isinstance(model_field, models.BinaryField):
        return (bytes, kwargs)

    # Fallback
    return (str, kwargs)


def build_dhi_model_from_django_fields(name: str, declared_fields: dict) -> type:
    """Build a dhi BaseModel class from a dict of Django form fields.

    Args:
        name: Name for the generated model class.
        declared_fields: Dict of {field_name: django_form_field}.

    Returns:
        A _BaseModel subclass with matching field annotations.
    """
    annotations = {}
    field_defaults = {}

    for field_name, django_field in declared_fields.items():
        dhi_type = django_form_field_to_dhi(django_field)

        annotations[field_name] = dhi_type

        # Handle defaults
        if django_field.initial is not None:
            field_defaults[field_name] = django_field.initial
        elif not django_field.required:
            field_defaults[field_name] = None

    # Build the dhi model class dynamically
    namespace = {"__annotations__": annotations, **field_defaults}
    dhi_model = type(f"{name}DhiModel", (_BaseModel,), namespace)
    return dhi_model
