"""
Core validation classes for hyperdjango.

Uses native Zig SIMD validators from _hyperdjango_native when compiled,
falls back to pure Python otherwise.
"""

# Native Zig validators — always available
import re

from hyperdjango._hyperdjango_native import (
    validate_email as _native_validate_email,
)
from hyperdjango._hyperdjango_native import (
    validate_int_range as _native_validate_int_range,
)
from hyperdjango._hyperdjango_native import (
    validate_string_length as _native_validate_string_length,
)


class ValidationError(Exception):
    """Single validation error"""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class ValidationErrors(Exception):
    """Multiple validation errors"""

    def __init__(self, errors: list[ValidationError]):
        self.errors = errors
        msg = "Validation failed:\n" + "\n".join(str(e) for e in errors)
        super().__init__(msg)


class BoundedInt:
    """Integer with bounds validation — uses Zig SIMD when available."""

    def __init__(self, min_val: int, max_val: int):
        self.min_val = min_val
        self.max_val = max_val

    def validate(self, value: int) -> int:
        if not isinstance(value, int):
            raise ValidationError("value", f"Expected int, got {type(value).__name__}")

        if not _native_validate_int_range(value, self.min_val, self.max_val):
            if value < self.min_val:
                raise ValidationError(
                    "value", f"Value {value} must be >= {self.min_val}"
                )
            else:
                raise ValidationError(
                    "value", f"Value {value} must be <= {self.max_val}"
                )

        return value

    def __call__(self, value: int) -> int:
        return self.validate(value)


class BoundedString:
    """String with length bounds validation — uses Zig SIMD when available."""

    def __init__(self, min_len: int, max_len: int):
        self.min_len = min_len
        self.max_len = max_len

    def validate(self, value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("value", f"Expected str, got {type(value).__name__}")

        if not _native_validate_string_length(value, self.min_len, self.max_len):
            slen = len(value)
            if slen < self.min_len:
                raise ValidationError(
                    "value", f"String length {slen} must be >= {self.min_len}"
                )
            else:
                raise ValidationError(
                    "value", f"String length {slen} must be <= {self.max_len}"
                )

        return value

    def __call__(self, value: str) -> str:
        return self.validate(value)


class Email:
    """Email validation — uses Zig SIMD when available."""

    _SIMPLE_PATTERN = None

    @classmethod
    def _get_pattern(cls):
        if cls._SIMPLE_PATTERN is None:
            cls._SIMPLE_PATTERN = re.compile(
                r"^[a-zA-Z0-9.!#$%&\'*+/=?^_`{|}~-]+@"
                r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
                r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
            )
        return cls._SIMPLE_PATTERN

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("email", f"Expected str, got {type(value).__name__}")

        if not value:
            raise ValidationError("email", "Email cannot be empty")

        if not _native_validate_email(value):
            raise ValidationError("email", f"Invalid email address: '{value}'")

        return value

    def __call__(self, value: str) -> str:
        return Email.validate(value)


class _ZigValidator:
    """Native Zig validator — always uses _hyperdjango_native."""

    @property
    def available(self):
        return True

    def validate_int(self, value, min_val, max_val):
        return _native_validate_int_range(value, min_val, max_val)

    def validate_string_length(self, value, min_len, max_len):
        return _native_validate_string_length(value, min_len, max_len)

    def validate_email(self, value):
        return _native_validate_email(value)


_zig = _ZigValidator()


# Validation failures escaping to the boundary are client errors (safe to echo).
from hyperdjango.exceptions import register_exception_status as _register_exc_status

_register_exc_status(ValidationError, 400, safe_detail=True)
_register_exc_status(ValidationErrors, 422, safe_detail=True)
