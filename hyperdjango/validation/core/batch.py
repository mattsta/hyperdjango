"""
High-performance batch validation API

Validates multiple items in single native FFI calls using SIMD vectorization.
Eliminates per-item Python↔Zig overhead for bulk operations.

Functions:
    validate_ints_batch      — SIMD parallel int range validation
    validate_strings_batch   — batch string length validation
    validate_emails_batch    — SIMD email format validation
    validate_users_batch     — batch dict validation with field specs
    validate_model_batch     — batch BaseModel validation using compiled specs
"""

import re
from dataclasses import dataclass, field
from typing import Any

from hyperdjango import _hyperdjango_native as _native
from hyperdjango.validation.core.validator import (
    BoundedInt,
    BoundedString,
    Email,
    ValidationError,
    ValidationErrors,
)


@dataclass(slots=True)
class BatchValidationResult:
    """Result of batch validation."""

    results: list[bool]
    valid_count: int
    total_count: int
    invalid_count: int = field(init=False)

    def __post_init__(self):
        self.invalid_count = self.total_count - self.valid_count

    def is_all_valid(self) -> bool:
        return self.valid_count == self.total_count

    def get_valid_indices(self) -> list[int]:
        return [i for i, valid in enumerate(self.results) if valid]

    def get_invalid_indices(self) -> list[int]:
        return [i for i, valid in enumerate(self.results) if not valid]


def validate_ints_batch(
    values: list[int],
    min_val: int,
    max_val: int,
) -> BatchValidationResult:
    """Validate a batch of integers against min/max bounds using SIMD.

    Single FFI call validates all N integers with 4-wide SIMD parallelism.

    Args:
        values: List of integers to validate
        min_val: Minimum allowed value (inclusive)
        max_val: Maximum allowed value (inclusive)

    Returns:
        BatchValidationResult with per-item results
    """
    if not values:
        return BatchValidationResult([], 0, 0)

    count = len(values)

    if _native and hasattr(_native, "validate_int_batch_simd"):
        results, valid_count = _native.validate_int_batch_simd(values, min_val, max_val)
        return BatchValidationResult(results, valid_count, count)

    # Fallback
    results = [min_val <= v <= max_val for v in values]
    return BatchValidationResult(results, sum(results), count)


def validate_strings_batch(
    strings: list[str],
    min_len: int,
    max_len: int,
) -> BatchValidationResult:
    """Validate a batch of string lengths in a single FFI call.

    Args:
        strings: List of strings to validate
        min_len: Minimum allowed length (inclusive)
        max_len: Maximum allowed length (inclusive)

    Returns:
        BatchValidationResult with per-item results
    """
    if not strings:
        return BatchValidationResult([], 0, 0)

    count = len(strings)

    if _native and hasattr(_native, "validate_string_length_batch"):
        results, valid_count = _native.validate_string_length_batch(
            strings, min_len, max_len
        )
        return BatchValidationResult(results, valid_count, count)

    # Fallback
    results = [min_len <= len(s) <= max_len for s in strings]
    return BatchValidationResult(results, sum(results), count)


def validate_emails_batch(emails: list[str]) -> BatchValidationResult:
    """Validate a batch of email addresses using SIMD email scanner.

    Uses 16-byte SIMD scanning for @ and . characters.

    Args:
        emails: List of email addresses to validate

    Returns:
        BatchValidationResult with per-item results
    """
    if not emails:
        return BatchValidationResult([], 0, 0)

    count = len(emails)

    if _native and hasattr(_native, "validate_email_batch"):
        results, valid_count = _native.validate_email_batch(emails)
        return BatchValidationResult(results, valid_count, count)

    # Fallback
    email_re = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
    results = [bool(email_re.match(e)) for e in emails]
    return BatchValidationResult(results, sum(results), count)


def validate_users_batch(
    users: list[dict[str, Any]],
    name_min: int = 1,
    name_max: int = 100,
    age_min: int = 18,
    age_max: int = 120,
) -> BatchValidationResult:
    """Validate a batch of user dicts in a single FFI call.

    Validates name (string length), email (format), and age (int range)
    for all N users in ONE native call.

    Args:
        users: List of dicts with 'name', 'email', 'age' keys
        name_min: Minimum name length
        name_max: Maximum name length
        age_min: Minimum age
        age_max: Maximum age

    Returns:
        BatchValidationResult with per-user results
    """
    if not users:
        return BatchValidationResult([], 0, 0)

    count = len(users)

    if _native and hasattr(_native, "validate_batch_direct"):
        field_specs = {
            "name": ("string", name_min, name_max),
            "email": ("email",),
            "age": ("int", age_min, age_max),
        }
        results, valid_count = _native.validate_batch_direct(users, field_specs)
        return BatchValidationResult(results, valid_count, count)

    # Fallback
    Name = BoundedString(name_min, name_max)
    Age = BoundedInt(age_min, age_max)

    results = []
    valid_count = 0
    for user in users:
        try:
            Name.validate(user.get("name", ""))
            Email.validate(user.get("email", ""))
            Age.validate(user.get("age", 0))
            results.append(True)
            valid_count += 1
        except ValidationError:
            results.append(False)

    return BatchValidationResult(results, valid_count, count)


def validate_model_batch(
    data: list[dict[str, Any]],
    model_class: type,
) -> list[list | None]:
    """Validate a batch of dicts against a BaseModel's compiled specs.

    Uses the same compiled specs as init_model_full for consistent validation.
    Returns per-item error lists (None = valid, list = errors).

    Args:
        data: List of dicts to validate
        model_class: BaseModel subclass with compiled specs

    Returns:
        List of None (valid) or [(field, msg)] error tuples
    """
    if not data:
        return []

    # dynamic-attr: model_class is typed only as `type`; __dhi_compiled_specs__ exists on BaseModel subclasses but not arbitrary types
    compiled = getattr(model_class, "__dhi_compiled_specs__", None)
    if compiled is not None and _native and hasattr(_native, "validate_model_batch"):
        return _native.validate_model_batch(data, compiled)

    # Fallback: validate individually
    results = []
    for item in data:
        try:
            model_class(**item)
            results.append(None)
        except (ValidationError, ValidationErrors) as e:
            if isinstance(e, ValidationErrors):
                results.append([(err.field, err.message) for err in e.errors])
            else:
                results.append([(e.field, e.message)])
    return results


__all__ = [
    "BatchValidationResult",
    "validate_ints_batch",
    "validate_strings_batch",
    "validate_emails_batch",
    "validate_users_batch",
    "validate_model_batch",
]
