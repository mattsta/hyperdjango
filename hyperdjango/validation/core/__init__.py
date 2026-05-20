"""
HyperDjango validation engine.

Self-contained validation engine built from dhi's patterns.
No external dependencies — all validation logic is owned by hyperdjango.
"""

# ruff: noqa: F401  — public API re-exports

from hyperdjango.validation.core.config import ConfigDict, get_config_value
from hyperdjango.validation.core.constraints import (
    AllowInfNan,
    DecimalPlaces,
    Ge,
    Gt,
    Le,
    Lt,
    MaxDigits,
    MaxLength,
    MinLength,
    MultipleOf,
    Pattern,
    Strict,
    StringConstraints,
    StripWhitespace,
    ToLower,
    ToUpper,
    UniqueItems,
)
from hyperdjango.validation.core.fields import Field, FieldInfo
from hyperdjango.validation.core.functional_validators import (
    PrivateAttr,
    field_validator,
    model_validator,
)
from hyperdjango.validation.core.model import BaseModel
from hyperdjango.validation.core.networks import (
    AnyHttpUrl,
    AnyUrl,
    EmailStr,
    HttpUrl,
    NameEmail,
)
from hyperdjango.validation.core.types import (
    FiniteFloat,
    NegativeFloat,
    NegativeInt,
    NonNegativeFloat,
    NonNegativeInt,
    NonPositiveFloat,
    NonPositiveInt,
    PositiveFloat,
    PositiveInt,
    StrictBool,
    StrictBytes,
    StrictFloat,
    StrictInt,
    StrictStr,
    conbytes,
    condate,
    condecimal,
    confloat,
    confrozenset,
    conint,
    conlist,
    conset,
    constr,
)
from hyperdjango.validation.core.validator import (
    ValidationError,
    ValidationErrors,
)

__all__ = [
    "BaseModel",
    "Field",
    "FieldInfo",
    "ValidationError",
    "ValidationErrors",
    "ConfigDict",
    "get_config_value",
    # Constraints
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
    # Types
    "StrictInt",
    "StrictFloat",
    "StrictStr",
    "StrictBool",
    "PositiveInt",
    "NegativeInt",
    "NonNegativeInt",
    "NonPositiveInt",
    "EmailStr",
    "NameEmail",
    "AnyUrl",
    "HttpUrl",
    "conint",
    "confloat",
    "constr",
    # Functional validators
    "field_validator",
    "model_validator",
    "PrivateAttr",
]
