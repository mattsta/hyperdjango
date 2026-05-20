"""Utility helpers for HyperAdmin — FK detection, inline fields, HTML escaping, value coercion."""

import typing
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from hyperdjango.admin.fields import _introspect_model
from hyperdjango.conf import parse_bool


def _detect_fk_field(inline_model, parent_model):
    """Auto-detect the FK field on inline_model that points to parent_model.

    Looks for a field with foreign_key matching parent_model's table name.
    """
    parent_table = parent_model._meta.table
    for field_name, field_meta in inline_model._meta.fields.items():
        if field_meta.foreign_key == parent_table:
            return field_name
    # Fallback: look for a field named {parent_model_name}_id
    parent_name = parent_model.__name__.lower()
    candidate = f"{parent_name}_id"
    if candidate in inline_model._meta.fields:
        return candidate
    return None


def _get_inline_fields(inline_config):
    """Get the AdminField list for an inline model."""
    all_fields = _introspect_model(inline_config.model_class)
    if inline_config.fields:
        return [f for f in all_fields if f.name in inline_config.fields]
    # Default: all non-auto, non-FK-to-parent fields
    return [f for f in all_fields if not f.is_auto and f.name != inline_config.fk_field]


def _escape_html(s: str) -> str:
    """HTML-escape a string for safe output."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _coerce_value(raw_str, python_type):
    """Coerce a form string value to the target Python type."""
    origin = typing.get_origin(python_type)

    # Unwrap Optional
    if origin is typing.Union:
        args = [a for a in python_type.__args__ if a is not type(None)]
        if args:
            python_type = args[0]

    if python_type is bool:
        return parse_bool(raw_str)
    if python_type is int:
        return int(raw_str)
    if python_type is float:
        return float(raw_str)
    if python_type is Decimal:
        return Decimal(raw_str)
    if python_type is datetime:
        return datetime.fromisoformat(raw_str)
    if python_type is date:
        return date.fromisoformat(raw_str)
    if python_type is time:
        return time.fromisoformat(raw_str)
    if isinstance(python_type, type) and issubclass(python_type, Enum):
        return python_type(raw_str)
    return str(raw_str)
