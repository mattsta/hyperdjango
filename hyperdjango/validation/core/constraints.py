"""
Constraint metadata classes for dhi - Pydantic v2 compatible.

These classes are used with typing.Annotated to define validation constraints
on fields, matching the annotated_types / Pydantic v2 pattern.

Example:
    from typing import Annotated
    from dhi import Gt, Le, MinLength

    age: Annotated[int, Gt(gt=0), Le(le=120)]
    name: Annotated[str, MinLength(min_length=1)]
"""

from dataclasses import dataclass

# --- Numeric Constraints ---


@dataclass(frozen=True, slots=True)
class Gt:
    """Greater than constraint."""

    gt: int | float


@dataclass(frozen=True, slots=True)
class Ge:
    """Greater than or equal constraint."""

    ge: int | float


@dataclass(frozen=True, slots=True)
class Lt:
    """Less than constraint."""

    lt: int | float


@dataclass(frozen=True, slots=True)
class Le:
    """Less than or equal constraint."""

    le: int | float


@dataclass(frozen=True, slots=True)
class MultipleOf:
    """Multiple of constraint."""

    multiple_of: int | float

    def __post_init__(self) -> None:
        if self.multiple_of == 0:
            raise ValueError("MultipleOf divisor must be non-zero")
        # NaN check (NaN != NaN); equality form so int+float both covered without isnan import
        if self.multiple_of != self.multiple_of:
            raise ValueError("MultipleOf divisor must not be NaN")


# --- String Constraints ---


@dataclass(frozen=True, slots=True)
class MinLength:
    """Minimum length constraint (for strings, bytes, collections)."""

    min_length: int

    def __post_init__(self) -> None:
        if self.min_length < 0:
            raise ValueError(f"MinLength must be non-negative, got {self.min_length}")


@dataclass(frozen=True, slots=True)
class MaxLength:
    """Maximum length constraint (for strings, bytes, collections)."""

    max_length: int

    def __post_init__(self) -> None:
        if self.max_length < 0:
            raise ValueError(f"MaxLength must be non-negative, got {self.max_length}")


@dataclass(frozen=True, slots=True)
class Pattern:
    """Regex pattern constraint for strings."""

    pattern: str


@dataclass(frozen=True, slots=True)
class Strict:
    """Strict type checking - no coercion allowed."""

    strict: bool = True


@dataclass(frozen=True, slots=True)
class StripWhitespace:
    """Strip leading/trailing whitespace from strings."""

    strip_whitespace: bool = True


@dataclass(frozen=True, slots=True)
class ToLower:
    """Convert string to lowercase."""

    to_lower: bool = True


@dataclass(frozen=True, slots=True)
class ToUpper:
    """Convert string to uppercase."""

    to_upper: bool = True


@dataclass(frozen=True, slots=True)
class AllowInfNan:
    """Control whether inf/nan float values are allowed."""

    allow_inf_nan: bool = True


@dataclass(frozen=True, slots=True)
class MaxDigits:
    """Maximum total digits for Decimal types."""

    max_digits: int


@dataclass(frozen=True, slots=True)
class DecimalPlaces:
    """Maximum decimal places for Decimal types."""

    decimal_places: int


@dataclass(frozen=True, slots=True)
class UniqueItems:
    """Ensure collection items are unique."""

    unique_items: bool = True


# --- Compound Constraint Classes (Pydantic v2 style) ---


@dataclass(frozen=True, slots=True)
class StringConstraints:
    """Combined string constraints - matches Pydantic v2's StringConstraints.

    Example:
        from typing import Annotated
        from dhi import StringConstraints

        Username = Annotated[str, StringConstraints(min_length=3, max_length=20, to_lower=True)]
    """

    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    strip_whitespace: bool = False
    to_lower: bool = False
    to_upper: bool = False
    strict: bool = False

    def __post_init__(self) -> None:
        if self.min_length is not None and self.min_length < 0:
            raise ValueError(
                f"StringConstraints.min_length must be non-negative, got {self.min_length}"
            )
        if self.max_length is not None and self.max_length < 0:
            raise ValueError(
                f"StringConstraints.max_length must be non-negative, got {self.max_length}"
            )
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError(
                f"StringConstraints.min_length ({self.min_length}) cannot exceed max_length ({self.max_length})"
            )


__all__ = [
    "Gt",
    "Ge",
    "Lt",
    "Le",
    "MultipleOf",
    "MinLength",
    "MaxLength",
    "Pattern",
    "Strict",
    "StripWhitespace",
    "ToLower",
    "ToUpper",
    "AllowInfNan",
    "MaxDigits",
    "DecimalPlaces",
    "UniqueItems",
    "StringConstraints",
]
