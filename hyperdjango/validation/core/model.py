"""
BaseModel implementation for dhi - Pydantic v2 compatible.

Provides a lightweight, high-performance BaseModel that validates data
on instantiation using type annotations and constraints.

Full Pydantic v2 API compatibility including:
- model_validate, model_validate_json, model_construct
- model_dump with all parameters (exclude, include, by_alias, exclude_unset, etc.)
- model_fields, model_fields_set, model_extra, model_computed_fields
- model_config (ConfigDict support)
- model_post_init hook
- Nested model validation
- computed_field and PrivateAttr support

Example:
    from typing import Annotated
    from dhi import BaseModel, Field, PositiveInt, EmailStr, ConfigDict

    class User(BaseModel):
        model_config = ConfigDict(frozen=True)

        name: Annotated[str, Field(min_length=1, max_length=100)]
        age: PositiveInt
        email: EmailStr
        score: Annotated[float, Field(ge=0, le=100)] = 0.0

    user = User(name="Alice", age=25, email="alice@example.com")
    print(user.model_dump())
"""

import annotationlib
import copy
import enum
import json as _stdlib_json
import math
import re
import sys
import warnings
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import (
    Any,
    ClassVar,
    Literal,
    TypeVar,
    Union,
    get_type_hints,
)

from hyperdjango.native import fast_json_loads

try:
    from typing import Annotated, Self, get_args, get_origin
except ImportError:
    from typing import Annotated, get_args, get_origin

    Self = TypeVar("Self", bound="BaseModel")

# Native Zig extension for 10-100x faster validation
import contextlib

from hyperdjango import _hyperdjango_native as _dhi_native
from hyperdjango.validation.core.config import (
    ConfigDict,
    get_config_value,
)
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
from hyperdjango.validation.core.fields import _MISSING, FieldInfo
from hyperdjango.validation.core.functional_validators import (
    ComputedFieldInfo,
    PrivateAttr,
)
from hyperdjango.validation.core.validator import (
    ValidationError,
    ValidationErrors,
)

# Type variable for model methods returning Self
_T = TypeVar("_T", bound="BaseModel")

# Include/Exclude type alias matching Pydantic
IncEx = set[str] | dict[str, Any] | None

# Type code mapping for native validator
_TYPE_CODES = {int: 1, float: 2, str: 3, bool: 4, bytes: 5}

# Cache for compiled validators per class
_CLASS_VALIDATORS_CACHE: dict[type, dict[str, Any]] = {}


def _extract_constraints(annotation: Any) -> tuple[type, list[Any]]:
    """Extract base type and constraint metadata from an annotation.

    Handles:
    - Plain types: int, str, float
    - Annotated types: Annotated[int, Gt(gt=0), Le(le=100)]
    - FieldInfo in Annotated: Annotated[str, Field(min_length=1)]
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        base_type = args[0]
        constraints = list(args[1:])
        # Recursively unwrap nested Annotated (e.g., PositiveInt used in Annotated)
        nested_origin = get_origin(base_type)
        if nested_origin is Annotated:
            nested_args = get_args(base_type)
            base_type = nested_args[0]
            constraints = list(nested_args[1:]) + constraints
        return base_type, constraints
    return annotation, []


def _is_basemodel_subclass(typ: Any) -> bool:
    """Check if a type is a BaseModel subclass (for nested validation)."""
    try:
        # Avoid circular import issues
        return isinstance(typ, type) and hasattr(typ, "__dhi_fields__")
    except TypeError, AttributeError:
        return False


def _build_validator(
    field_name: str,
    base_type: type,
    constraints: list[Any],
    config: ConfigDict | None = None,
) -> Any:
    """Build a compiled validator function for a field.

    Returns a function that takes a value and returns the validated/transformed value,
    or raises ValidationError.

    Supports nested BaseModel validation.
    """
    # Collect all constraints from both individual metadata and FieldInfo objects
    gt = ge = lt = le = multiple_of = None
    min_length = max_length = None
    pattern_str = None
    strict = get_config_value(config, "strict", False)
    strip_whitespace = get_config_value(config, "str_strip_whitespace", False)
    to_lower = get_config_value(config, "str_to_lower", False)
    to_upper = get_config_value(config, "str_to_upper", False)
    allow_inf_nan = True
    max_digits = decimal_places = None
    unique_items = False
    custom_validators: list[Any] = []

    # Check if base_type is a nested BaseModel
    nested_model = None
    if _is_basemodel_subclass(base_type):
        nested_model = base_type

    for constraint in constraints:
        if isinstance(constraint, Gt):
            gt = constraint.gt
        elif isinstance(constraint, Ge):
            ge = constraint.ge
        elif isinstance(constraint, Lt):
            lt = constraint.lt
        elif isinstance(constraint, Le):
            le = constraint.le
        elif isinstance(constraint, MultipleOf):
            multiple_of = constraint.multiple_of
        elif isinstance(constraint, MinLength):
            min_length = constraint.min_length
        elif isinstance(constraint, MaxLength):
            max_length = constraint.max_length
        elif isinstance(constraint, Pattern):
            pattern_str = constraint.pattern
        elif isinstance(constraint, Strict):
            strict = constraint.strict
        elif isinstance(constraint, StripWhitespace):
            strip_whitespace = constraint.strip_whitespace
        elif isinstance(constraint, ToLower):
            to_lower = constraint.to_lower
        elif isinstance(constraint, ToUpper):
            to_upper = constraint.to_upper
        elif isinstance(constraint, AllowInfNan):
            allow_inf_nan = constraint.allow_inf_nan
        elif isinstance(constraint, MaxDigits):
            max_digits = constraint.max_digits
        elif isinstance(constraint, DecimalPlaces):
            decimal_places = constraint.decimal_places
        elif isinstance(constraint, UniqueItems):
            unique_items = constraint.unique_items
        elif isinstance(constraint, StringConstraints):
            # Unpack compound constraints
            if constraint.min_length is not None:
                min_length = constraint.min_length
            if constraint.max_length is not None:
                max_length = constraint.max_length
            if constraint.pattern is not None:
                pattern_str = constraint.pattern
            if constraint.strip_whitespace:
                strip_whitespace = True
            if constraint.to_lower:
                to_lower = True
            if constraint.to_upper:
                to_upper = True
            if constraint.strict:
                strict = True
        elif isinstance(constraint, FieldInfo):
            # Extract constraints from FieldInfo
            if constraint.gt is not None:
                gt = constraint.gt
            if constraint.ge is not None:
                ge = constraint.ge
            if constraint.lt is not None:
                lt = constraint.lt
            if constraint.le is not None:
                le = constraint.le
            if constraint.multiple_of is not None:
                multiple_of = constraint.multiple_of
            if constraint.min_length is not None:
                min_length = constraint.min_length
            if constraint.max_length is not None:
                max_length = constraint.max_length
            if constraint.pattern is not None:
                pattern_str = constraint.pattern
            if constraint.strict:
                strict = True
            if constraint.strip_whitespace:
                strip_whitespace = True
            if constraint.to_lower:
                to_lower = True
            if constraint.to_upper:
                to_upper = True
            if constraint.allow_inf_nan is not None:
                allow_inf_nan = constraint.allow_inf_nan
            if constraint.max_digits is not None:
                max_digits = constraint.max_digits
            if constraint.decimal_places is not None:
                decimal_places = constraint.decimal_places
            if constraint.unique_items:
                unique_items = True
        elif hasattr(constraint, "validate") and callable(constraint.validate):
            # Custom validator object (e.g., _EmailValidator, _UrlValidator, etc.)
            custom_validators.append(constraint)
        elif callable(constraint):
            custom_validators.append(constraint)

    # Pre-compile pattern if present
    compiled_pattern = re.compile(pattern_str) if pattern_str else None

    # Determine the expected Python type for type checking
    # Handle generic types (List[int] -> list, Set[str] -> set, etc.)
    check_type = base_type
    type_origin = get_origin(base_type)
    type_args = get_args(base_type) if type_origin is not None else ()
    if type_origin is not None:
        check_type = type_origin

    # Extract item type for collection validation (List[int] -> int, etc.)
    item_type = None
    if type_origin in (list, set, frozenset) and type_args:
        item_type = type_args[0]

    # Handle Optional[T] = Union[T, None] - extract T if it's a BaseModel
    optional_model = None
    if type_origin is Union:
        non_none_args = [a for a in type_args if a is not type(None)]
        if len(non_none_args) == 1:
            inner_type = non_none_args[0]
            if _is_basemodel_subclass(inner_type):
                optional_model = inner_type

    # Detect enum types at closure-build time. Fields annotated as an
    # enum subclass should coerce raw scalar values (str from text
    # columns, int from IntEnum columns) into enum instances on read
    # or assignment, so equality checks like `post.status ==
    # PostStatus.ACTIVE` compare enum instances rather than raw
    # strings/ints.
    is_enum_type = isinstance(check_type, type) and issubclass(check_type, enum.Enum)
    # Same detection for Optional[Enum] — coerce the non-None branch
    optional_enum_type: type | None = None
    if type_origin is Union:
        for _arg in type_args:
            if (
                _arg is not type(None)
                and isinstance(_arg, type)
                and issubclass(_arg, enum.Enum)
            ):
                optional_enum_type = _arg
                break

    # Optional[scalar/collection] = Union[T, None] with a single non-None member:
    # validate the inner type via a recursively-built validator and let None pass
    # through. We return early — BEFORE the native-constraints path below — on
    # purpose: routing the inner type's check_type into the native fast path would
    # compile the field as a non-nullable column and wrongly reject None. Optional
    # of a BaseModel/Enum is handled above (optional_model / optional_enum_type);
    # multi-member unions keep pass-through behavior.
    if type_origin is Union and optional_model is None and optional_enum_type is None:
        _non_none = [a for a in type_args if a is not type(None)]
        if len(_non_none) == 1:
            _inner_validator = _build_validator(
                field_name, _non_none[0], constraints, config
            )

            def optional_validator(value: Any) -> Any:
                if value is None:
                    return None
                return _inner_validator(value)

            return optional_validator

    def validator(value: Any) -> Any:
        # Enum coercion — fields annotated as Enum must hold enum instances,
        # not raw scalar values. Handles both `status: PostStatus` and
        # `status: PostStatus | None`. None passes through for optional.
        if is_enum_type:
            if isinstance(value, check_type):
                return value
            try:
                return check_type(value)
            except ValueError, TypeError:
                raise ValidationError(
                    field_name,
                    f"Invalid {check_type.__name__} value: {value!r}",
                ) from None
        if optional_enum_type is not None:
            if value is None or isinstance(value, optional_enum_type):
                return value
            try:
                return optional_enum_type(value)
            except ValueError, TypeError:
                raise ValidationError(
                    field_name,
                    f"Invalid {optional_enum_type.__name__} value: {value!r}",
                ) from None

        # Type checking
        if strict:
            if type(value) is not check_type:
                raise ValidationError(
                    field_name,
                    f"Expected exactly {check_type.__name__}, got {type(value).__name__}",
                )
        else:
            # Coerce compatible types
            if check_type in (int, float) and not isinstance(value, check_type):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    # Reject fractional / non-finite floats for int fields instead
                    # of silently truncating (int(1.5) -> 1). Whole-valued floats
                    # such as 5.0 -> 5 are still accepted.
                    if check_type is int and (
                        not math.isfinite(value) or value != math.floor(value)
                    ):
                        raise ValidationError(
                            field_name,
                            f"Expected int, got float with fractional part: {value!r}",
                        )
                    try:
                        value = check_type(value)
                    except ValueError, TypeError, OverflowError:
                        raise ValidationError(
                            field_name,
                            f"Cannot convert {type(value).__name__} to {check_type.__name__}",
                        )
                else:
                    raise ValidationError(
                        field_name,
                        f"Expected {check_type.__name__}, got {type(value).__name__}",
                    )
            elif check_type is str and not isinstance(value, str):
                raise ValidationError(
                    field_name, f"Expected str, got {type(value).__name__}"
                )
            elif check_type is bytes and not isinstance(value, bytes):
                raise ValidationError(
                    field_name, f"Expected bytes, got {type(value).__name__}"
                )
            elif check_type is bool and not isinstance(value, bool):
                raise ValidationError(
                    field_name, f"Expected bool, got {type(value).__name__}"
                )
            elif check_type in (list, set, frozenset) and not isinstance(
                value, check_type
            ):
                raise ValidationError(
                    field_name,
                    f"Expected {check_type.__name__}, got {type(value).__name__}",
                )

        # String transformations (before validation)
        if isinstance(value, str):
            if strip_whitespace:
                value = value.strip()
            if to_lower:
                value = value.lower()
            if to_upper:
                value = value.upper()

        # Numeric constraints
        if gt is not None and value <= gt:
            raise ValidationError(field_name, f"Value must be > {gt}, got {value}")
        if ge is not None and value < ge:
            raise ValidationError(field_name, f"Value must be >= {ge}, got {value}")
        if lt is not None and value >= lt:
            raise ValidationError(field_name, f"Value must be < {lt}, got {value}")
        if le is not None and value > le:
            raise ValidationError(field_name, f"Value must be <= {le}, got {value}")
        if multiple_of is not None and value % multiple_of != 0:
            raise ValidationError(
                field_name, f"Value must be a multiple of {multiple_of}, got {value}"
            )

        # Float-specific constraints
        if (
            not allow_inf_nan
            and isinstance(value, float)
            and (math.isinf(value) or math.isnan(value))
        ):
            raise ValidationError(field_name, f"Value must be finite, got {value}")

        # Length constraints (strings, bytes, collections)
        if min_length is not None or max_length is not None:
            length = len(value)
            if min_length is not None and length < min_length:
                raise ValidationError(
                    field_name, f"Length must be >= {min_length}, got {length}"
                )
            if max_length is not None and length > max_length:
                raise ValidationError(
                    field_name, f"Length must be <= {max_length}, got {length}"
                )

        # Pattern constraint
        if (
            compiled_pattern is not None
            and isinstance(value, str)
            and not compiled_pattern.match(value)
        ):
            raise ValidationError(
                field_name, f"String does not match pattern '{pattern_str}'"
            )

        # Decimal constraints
        if (max_digits is not None or decimal_places is not None) and isinstance(
            value, Decimal
        ):
            sign, digits, exp = value.as_tuple()
            num_digits = len(digits)
            if max_digits is not None and num_digits > max_digits:
                raise ValidationError(
                    field_name,
                    f"Decimal must have at most {max_digits} digits, got {num_digits}",
                )
            if decimal_places is not None:
                actual_places = -exp if exp < 0 else 0
                if actual_places > decimal_places:
                    raise ValidationError(
                        field_name,
                        f"Decimal must have at most {decimal_places} decimal places, got {actual_places}",
                    )

        # Unique items constraint
        if unique_items and isinstance(value, list):
            seen = set()
            for item in value:
                item_key = repr(item)  # Use repr for unhashable items
                if item_key in seen:
                    raise ValidationError(
                        field_name,
                        f"List items must be unique, found duplicate: {item!r}",
                    )
                seen.add(item_key)

        # List/set item type validation (e.g., List[int] validates each item is int)
        if item_type is not None and isinstance(value, (list, set, frozenset)):
            validated_items = []
            for i, item in enumerate(value):
                # Check item type
                if item_type is int:
                    if not isinstance(item, int) or isinstance(item, bool):
                        raise ValidationError(
                            field_name,
                            f"Item {i}: Expected int, got {type(item).__name__}",
                        )
                elif item_type is float:
                    if isinstance(item, bool):
                        raise ValidationError(
                            field_name, f"Item {i}: Expected float, got bool"
                        )
                    if not isinstance(item, (int, float)):
                        raise ValidationError(
                            field_name,
                            f"Item {i}: Expected float, got {type(item).__name__}",
                        )
                    item = float(item)  # Coerce int to float
                elif item_type is str:
                    if not isinstance(item, str):
                        raise ValidationError(
                            field_name,
                            f"Item {i}: Expected str, got {type(item).__name__}",
                        )
                elif item_type is bool:
                    if not isinstance(item, bool):
                        raise ValidationError(
                            field_name,
                            f"Item {i}: Expected bool, got {type(item).__name__}",
                        )
                elif _is_basemodel_subclass(item_type):
                    if isinstance(item, dict):
                        item = item_type.model_validate(item)
                    elif not isinstance(item, item_type):
                        raise ValidationError(
                            field_name,
                            f"Item {i}: Expected {item_type.__name__} or dict, got {type(item).__name__}",
                        )
                validated_items.append(item)
            # Reconstruct collection
            if isinstance(value, list):
                value = validated_items
            elif isinstance(value, set):
                value = set(validated_items)
            elif isinstance(value, frozenset):
                value = frozenset(validated_items)

        # Optional[Model] validation - convert dict to model
        if optional_model is not None and value is not None:
            if isinstance(value, optional_model):
                pass  # Already validated
            elif isinstance(value, dict):
                value = optional_model.model_validate(value)
            else:
                raise ValidationError(
                    field_name,
                    f"Expected {optional_model.__name__}, dict, or None, got {type(value).__name__}",
                )

        # Nested BaseModel validation
        if nested_model is not None:
            if isinstance(value, nested_model):
                pass  # Already validated
            elif isinstance(value, dict):
                value = nested_model.model_validate(value)
            else:
                raise ValidationError(
                    field_name,
                    f"Expected {nested_model.__name__} or dict, got {type(value).__name__}",
                )

        # Custom validators (objects with .validate() or callables)
        for custom_val in custom_validators:
            if hasattr(custom_val, "validate"):
                value = custom_val.validate(value, field_name)
            else:
                value = custom_val(value)

        return value

    # --- NATIVE ACCELERATION PATH ---
    # Use C extension for type check + numeric bounds + string length in one call.
    # Falls back to Python for: regex patterns, decimal constraints, unique items, nested models.
    can_use_native = (
        compiled_pattern is None
        and max_digits is None
        and decimal_places is None
        and not unique_items
        and nested_model is None
        and check_type in _TYPE_CODES
    )

    if can_use_native:
        type_code = _TYPE_CODES[check_type]
        native_constraints = (
            type_code,
            int(strict),
            gt,
            ge,
            lt,
            le,
            multiple_of,
            min_length,
            max_length,
            int(allow_inf_nan),
            0,  # format_code=0 (handled by custom validators)
            int(strip_whitespace),
            int(to_lower),
            int(to_upper),
        )

        if custom_validators:
            # Native for type+bounds, then Python for custom validators
            _custom_vals = custom_validators

            def native_validator_with_custom(value: Any) -> Any:
                try:
                    value = _dhi_native.validate_field(
                        value, field_name, native_constraints
                    )
                except ValueError as e:
                    msg = str(e)
                    prefix = field_name + ": "
                    msg = msg.removeprefix(prefix)
                    raise ValidationError(field_name, msg)
                for cv in _custom_vals:
                    if hasattr(cv, "validate"):
                        value = cv.validate(value, field_name)
                    else:
                        value = cv(value)
                return value

            return native_validator_with_custom
        else:
            # Fully native - one C call handles everything
            def native_validator(value: Any) -> Any:
                try:
                    return _dhi_native.validate_field(
                        value, field_name, native_constraints
                    )
                except ValueError as e:
                    msg = str(e)
                    prefix = field_name + ": "
                    msg = msg.removeprefix(prefix)
                    raise ValidationError(field_name, msg)

            # Tag for batch init_model detection
            native_validator.__dhi_native_constraints__ = native_constraints
            return native_validator

    return validator


def _resolve_hints(cls) -> dict[str, type]:
    """Resolve type hints for a class, handling forward references.

    Passes the module's global namespace and includes the class itself
    in localns so self-referencing models work.
    """
    # Build namespace: module globals + the class itself for self-references
    module = sys.modules.get(cls.__module__, None)
    globalns = module.__dict__ if module else {}
    localns = {cls.__name__: cls}

    try:
        hints = get_type_hints(
            cls, globalns=globalns, localns=localns, include_extras=True
        )
    # blind-except: get_type_hints evaluates eagerly and raises on the first unresolvable name; fall back to the forward-ref-tolerant resolution below so one bad annotation can't disable validation for the whole model.
    except Exception:
        # get_type_hints evaluates eagerly and raises on the first unresolvable
        # name. Fall back to a forward-ref-tolerant resolution (PEP 649 /
        # annotationlib) so one bad annotation can't disable validation for the
        # whole model.
        hints = {}
        for klass in reversed(cls.__mro__):
            if klass is object:
                continue
            try:
                hints.update(
                    annotationlib.get_annotations(
                        klass, format=annotationlib.Format.FORWARDREF
                    )
                )
            # blind-except: skip an MRO class whose annotations can't be read at all; the remaining base classes still contribute their resolvable hints.
            except Exception:
                continue

    # An annotation that did not resolve to a real type — a ForwardRef or a bare
    # string for a name not yet defined — is skipped (with a warning) rather than
    # becoming a broken required field or silently disabling all validation.
    # model_rebuild() can be called once the referenced types exist.
    resolved: dict[str, type] = {}
    unresolved: list[str] = []
    for field, hint in hints.items():
        if isinstance(hint, (str, annotationlib.ForwardRef)):
            unresolved.append(field)
        else:
            resolved[field] = hint
    if unresolved:
        warnings.warn(
            f"{cls.__name__}: could not resolve type hints for "
            f"{sorted(set(unresolved))}; validation is skipped for those "
            f"fields until model_rebuild() is called.",
            stacklevel=2,
        )
    return resolved


def _compile_model_fields(cls, hints: dict) -> None:
    """Compile fields, validators, and native specs for a model class.

    This is the shared logic used by both _ModelMeta.__new__ and model_rebuild().
    It expects cls to already have model_config, __dhi_private_attrs__, and
    __dhi_computed_fields__ set.
    """
    model_config = cls.model_config

    # Build field info and validators
    fields: dict[str, dict[str, Any]] = {}
    validators: dict[str, Any] = {}
    model_fields: dict[str, FieldInfo] = {}

    # Reserved attribute names that should not be treated as fields
    reserved_names = {
        "model_config",
        "model_fields",
        "model_computed_fields",
        "model_fields_set",
        "model_extra",
    }

    # Get the class namespace for defaults
    namespace = {}
    for klass in reversed(cls.__mro__):
        namespace.update(klass.__dict__)

    for field_name, annotation in hints.items():
        if field_name.startswith("_"):
            continue
        if field_name in reserved_names:
            continue

        base_type, constraints = _extract_constraints(annotation)

        # Check for class-level default
        default = namespace.get(field_name, _MISSING)
        default_factory = None

        # Handle Pydantic v2 `field: type = Field(...)` syntax —
        # the class attribute IS a FieldInfo, not a plain default value.
        # Extract the real default and merge it as both the field_info and
        # a constraint source (for min_length, pattern, etc.).
        if isinstance(default, FieldInfo):
            fi_from_default = default
            # Check if this FieldInfo has validation constraints (not just DB metadata).
            # DB-only FieldInfos (primary_key, foreign_key, etc.) should stay lenient
            # to allow partial model construction like Post(id=5).
            # FieldInfo is a slotted dataclass, so every validation constraint
            # below is always present — read them directly instead of by name.
            has_validation = any(
                v is not None
                for v in (
                    fi_from_default.gt,
                    fi_from_default.ge,
                    fi_from_default.lt,
                    fi_from_default.le,
                    fi_from_default.multiple_of,
                    fi_from_default.min_length,
                    fi_from_default.max_length,
                    fi_from_default.pattern,
                    fi_from_default.strip_whitespace,
                    fi_from_default.to_lower,
                    fi_from_default.to_upper,
                    fi_from_default.max_digits,
                    fi_from_default.decimal_places,
                    fi_from_default.strict,
                    fi_from_default.unique_items,
                    fi_from_default.allow_inf_nan,
                )
            )
            has_explicit_default = fi_from_default.default is not _MISSING

            if has_validation or has_explicit_default:
                # This is a validation schema field — extract the real default
                if has_explicit_default:
                    default = fi_from_default.default
                elif fi_from_default.default_factory is not None:
                    default_factory = fi_from_default.default_factory
                    default = default_factory  # Mark as not required
                else:
                    default = _MISSING  # Required field (no default)
                # Merge into constraints so _build_validator sees it
                constraints = list(constraints) + [fi_from_default]
            else:
                # DB-only FieldInfo — merge as constraint source but keep old
                # default behavior (FieldInfo stays as default for leniency)
                constraints = list(constraints) + [fi_from_default]

        # Find the FieldInfo if present (from Annotated or from default above)
        field_info = None
        for c in constraints:
            if isinstance(c, FieldInfo):
                field_info = c
                if c.default is not _MISSING:
                    default = c.default
                if c.default_factory is not None:
                    default_factory = c.default_factory
                    default = default_factory  # Mark as not required
                break

        # Create FieldInfo if not present
        if field_info is None:
            field_info = FieldInfo(
                default=default if default is not _MISSING else _MISSING,
                default_factory=default_factory,
                annotation=annotation,
            )
        else:
            # Update annotation on existing FieldInfo
            field_info.annotation = annotation

        fields[field_name] = {
            "annotation": annotation,
            "base_type": base_type,
            "constraints": constraints,
            "default": default,
            "default_factory": default_factory,
            "required": default is _MISSING and default_factory is None,
            "field_info": field_info,
        }
        validators[field_name] = _build_validator(
            field_name, base_type, constraints, model_config
        )
        model_fields[field_name] = field_info

    cls.__dhi_fields__ = fields
    cls.__dhi_validators__ = validators
    cls.__dhi_field_names__ = list(fields.keys())
    cls.model_fields = model_fields

    # Pre-compute flat field specs for fast __init__ (avoid dict lookups per-call)
    fast_fields = []
    for field_name, field_data in fields.items():
        fi = field_data.get("field_info")
        # Determine validation alias (validation_alias > alias)
        validation_alias = None
        if fi:
            validation_alias = fi.validation_alias or fi.alias
        if validation_alias is None:
            for c in field_data["constraints"]:
                if isinstance(c, FieldInfo):
                    validation_alias = c.validation_alias or c.alias
                    break
        fast_fields.append(
            (
                field_name,
                field_data["required"],
                field_data["default"],
                field_data.get("default_factory"),
                validation_alias,
                validators[field_name],
                fi,  # Include FieldInfo for frozen/exclude checks
            )
        )
    cls.__dhi_fast_fields__ = tuple(fast_fields)

    # Try to build native init specs for batch C init (one Python->C call)
    native_init_specs = []
    nested_field_specs = []
    has_nested_or_complex = False

    _NESTED_DUMMY_CONSTRAINTS = (
        0,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        1,
        0,
        0,
        0,
        0,
    )
    # type_code 7 = list-of-models, type_code 8 = union of models
    _LIST_OF_MODELS_CONSTRAINTS = (
        7,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        1,
        0,
        0,
        0,
        0,
    )
    _UNION_CONSTRAINTS = (8, 0, None, None, None, None, None, None, None, 1, 0, 0, 0, 0)

    for (
        field_name,
        required,
        default,
        default_factory,
        alias,
        validator,
        _fi,
    ) in fast_fields:
        # Only fully-native validator closures are tagged (see _build_validator);
        # custom/hybrid validators lack the tag, so presence is the native signal.
        # dynamic-attr: __dhi_native_constraints__ is present only on native validators
        constraints_attr = getattr(validator, "__dhi_native_constraints__", None)
        field_data = fields[field_name]
        base_type = field_data["base_type"]
        annotation = field_data["annotation"]

        is_nested = _is_basemodel_subclass(base_type)
        has_mutable_default = default_factory is not None or isinstance(
            default, (list, dict, set)
        )

        # Detect List[Union[BaseModel...]] or List[BaseModel]
        list_of_models_types = None
        union_model_types = None
        type_origin = get_origin(base_type)
        if type_origin in (list, set, frozenset):
            type_args = get_args(base_type)
            if type_args:
                item_type = type_args[0]
                item_origin = get_origin(item_type)
                if item_origin is Union:
                    union_args = get_args(item_type)
                    non_none = [a for a in union_args if a is not type(None)]
                    if non_none and all(_is_basemodel_subclass(a) for a in non_none):
                        list_of_models_types = tuple(non_none)
                elif _is_basemodel_subclass(item_type):
                    list_of_models_types = (item_type,)
        # Detect Union[BaseModel...] (not Optional)
        elif type_origin is Union:
            type_args = get_args(base_type)
            non_none = [a for a in type_args if a is not type(None)]
            if len(non_none) > 1 and all(_is_basemodel_subclass(a) for a in non_none):
                union_model_types = tuple(non_none)

        if is_nested and not has_mutable_default:
            native_init_specs.append(
                (
                    field_name,
                    alias,
                    required,
                    default if default is not _MISSING else None,
                    _NESTED_DUMMY_CONSTRAINTS,
                    base_type,
                )
            )
        elif list_of_models_types is not None:
            # Extract length constraints from validator if present
            lom_constraints = list(_LIST_OF_MODELS_CONSTRAINTS)
            for c in field_data["constraints"]:
                if (
                    isinstance(c, FieldInfo)
                    or hasattr(c, "min_length")
                    and hasattr(c, "max_length")
                ):
                    if c.min_length is not None:
                        lom_constraints[7] = c.min_length
                    if c.max_length is not None:
                        lom_constraints[8] = c.max_length
            native_init_specs.append(
                (
                    field_name,
                    alias,
                    required,
                    default if default is not _MISSING else None,
                    tuple(lom_constraints),
                    list_of_models_types,  # tuple of types passed as 6th element
                )
            )
        elif union_model_types is not None and not has_mutable_default:
            native_init_specs.append(
                (
                    field_name,
                    alias,
                    required,
                    default if default is not _MISSING else None,
                    _UNION_CONSTRAINTS,
                    union_model_types,  # tuple of types
                )
            )
        elif constraints_attr is not None and not is_nested and not has_mutable_default:
            native_init_specs.append(
                (
                    field_name,
                    alias,
                    required,
                    default if default is not _MISSING else None,
                    constraints_attr,
                    None,
                )
            )
        else:
            has_nested_or_complex = True
            nested_field_specs.append(
                (
                    field_name,
                    alias,
                    required,
                    default,
                    default_factory,
                    validator,
                    base_type,
                    is_nested,
                )
            )

    cls.__dhi_native_init_specs__ = (
        tuple(native_init_specs) if native_init_specs else None
    )
    cls.__dhi_nested_field_specs__ = (
        tuple(nested_field_specs) if nested_field_specs else None
    )
    cls.__dhi_has_nested_fields__ = has_nested_or_complex

    # Pre-compile into C structs for zero-overhead constraint access
    if native_init_specs:
        cls.__dhi_compiled_specs__ = _dhi_native.compile_model_specs(
            tuple(native_init_specs)
        )
    else:
        cls.__dhi_compiled_specs__ = None

    # Track if we can use full native (no nested/complex fields)
    cls.__dhi_full_native__ = bool(native_init_specs) and not has_nested_or_complex

    # Update ultra-fast flag
    has_custom = cls.__dhi_has_custom_validators__
    cls.__dhi_use_ultra_fast__ = cls.__dhi_full_native__ and not has_custom


# Reference to the generic BaseModel.__init__, pinned once BaseModel is defined.
# The metaclass uses it to (a) recognize a dhi-managed __init__ that is safe to
# override and (b) pin the generic init on classes that don't qualify for the
# specialized fast init (so they don't inherit a parent's spec-capturing closure).
_GENERIC_INIT = None


def _make_fast_init(_compiled, _extra_mode):
    """Specialized ``__init__`` for ultra-fast classes that need no post-init.

    Captures the compiled native specs + extra-mode as cell variables so the hot
    path avoids the generic ``__init__``'s per-call ``type(self)`` plus the
    ``__dhi_use_ultra_fast__`` / ``__dhi_compiled_specs__`` /
    ``__dhi_extra_mode_int__`` / ``__dhi_needs_post_init__`` class-attribute
    lookups. Same result, less per-call overhead.
    """

    def __init__(self, **kwargs: Any) -> None:
        result = _dhi_native.init_model_full(self, kwargs, _compiled, _extra_mode)
        if result is not None:
            raise ValidationErrors([ValidationError(f, m) for f, m in result])

    __init__._dhi_managed = True
    return __init__


class _ModelMeta(type):
    """Metaclass for BaseModel that compiles validators at class creation."""

    def __new__(mcs, name: str, bases: tuple, namespace: dict) -> type:
        cls = super().__new__(mcs, name, bases, namespace)

        if name == "BaseModel":
            # Set default values for the base class
            cls.__dhi_compiled_specs__ = None
            cls.__dhi_has_custom_validators__ = False
            cls.__dhi_private_attrs__ = {}
            cls.__dhi_has_post_init__ = False
            cls.__dhi_extra_mode_int__ = 0
            cls.__dhi_needs_post_init__ = False
            cls.__dhi_nested_field_specs__ = None
            cls.__dhi_has_nested_fields__ = False
            cls.__dhi_full_native__ = False
            cls.__dhi_use_ultra_fast__ = False
            # Empty-but-present defaults so direct attribute access on any
            # BaseModel (including a bare BaseModel instance) is well-defined.
            # Subclasses overwrite these in _compile_model_fields /
            # __init_subclass__. (Deliberately NOT setting __dhi_fields__ here:
            # its ABSENCE on the base is what _is_basemodel_subclass keys on.)
            cls.__dhi_computed_fields__ = {}
            cls.model_computed_fields = {}
            cls.__dhi_field_validator_funcs__ = {}
            cls.__dhi_model_validators_before__ = []
            cls.__dhi_model_validators_after__ = []
            return cls

        # Get model_config from class or inherit from parent
        model_config: ConfigDict | None = namespace.get("model_config")
        if model_config is None:
            for base in bases:
                if hasattr(base, "model_config") and base.model_config is not None:
                    model_config = base.model_config
                    break
        cls.model_config = model_config

        # Get type hints including Annotated metadata
        # Pass module globals + class itself as localns for forward/self references
        hints = _resolve_hints(cls)

        # Collect private attributes (underscore-prefixed with PrivateAttr)
        private_attrs: dict[str, PrivateAttr] = {}
        for attr_name, attr_value in namespace.items():
            if (
                attr_name.startswith("_")
                and not attr_name.startswith("__")
                and isinstance(attr_value, PrivateAttr)
            ):
                private_attrs[attr_name] = attr_value
        cls.__dhi_private_attrs__ = private_attrs

        # Collect computed fields
        computed_fields: dict[str, ComputedFieldInfo] = {}
        for attr_name, attr_value in namespace.items():
            if isinstance(attr_value, ComputedFieldInfo):
                computed_fields[attr_name] = attr_value
                # dynamic-attr: attr_name is a user-declared @computed_field method name, unknown at framework-authoring time
                setattr(cls, attr_name, attr_value.wrapped_property)
        cls.__dhi_computed_fields__ = computed_fields
        cls.model_computed_fields = computed_fields

        # Compile fields, validators, and native specs
        _compile_model_fields(cls, hints)

        # Check if model_post_init is overridden (for optimization)
        has_post_init = "model_post_init" in namespace
        cls.__dhi_has_post_init__ = has_post_init

        # Pre-compute extra_mode as int for fast native path (0=ignore, 1=forbid, 2=allow)
        extra_mode_str = get_config_value(model_config, "extra", "ignore")
        cls.__dhi_extra_mode_int__ = {"ignore": 0, "forbid": 1, "allow": 2}.get(
            extra_mode_str, 0
        )

        # Combined flag: needs any post-init processing (private attrs or post_init override)
        cls.__dhi_needs_post_init__ = bool(private_attrs) or has_post_init

        # Install a specialized fast __init__ when safe: only when this class
        # doesn't define its own __init__ AND the one it would inherit is
        # dhi-managed (never override a user's custom __init__ anywhere in the
        # MRO). Ultra-fast + no-post-init classes get the spec-capturing closure;
        # everything else is pinned to the generic init so it can't inherit a
        # parent's closure (which captured the parent's specs, not ours).
        # We must never override a user's custom __init__ anywhere in the MRO.
        # dynamic-attr: the inherited __init__ is either a user function (untagged) or a dhi-generated one tagged _dhi_managed
        if "__init__" not in namespace and getattr(cls.__init__, "_dhi_managed", False):
            if cls.__dhi_use_ultra_fast__ and not cls.__dhi_needs_post_init__:
                cls.__init__ = _make_fast_init(
                    cls.__dhi_compiled_specs__, cls.__dhi_extra_mode_int__
                )
            elif _GENERIC_INIT is not None:
                cls.__init__ = _GENERIC_INIT

        return cls


class BaseModel(metaclass=_ModelMeta):
    """High-performance validated model - Pydantic v2 compatible API.

    Define models with type annotations and constraints. Data is validated
    on instantiation.

    Full Pydantic v2 API compatibility including:
    - model_validate, model_validate_json, model_construct
    - model_dump (with mode, by_alias, exclude_unset, exclude_defaults, exclude_none)
    - model_fields, model_fields_set, model_extra, model_computed_fields
    - model_config (ConfigDict support)
    - model_post_init hook

    Example:
        from typing import Annotated
        from dhi import BaseModel, Field, PositiveInt, ConfigDict

        class User(BaseModel):
            model_config = ConfigDict(frozen=True)

            name: Annotated[str, Field(min_length=1, max_length=100)]
            age: PositiveInt
            email: str
            score: Annotated[float, Field(ge=0, le=100)] = 0.0

        user = User(name="Alice", age=25, email="alice@example.com")
        assert user.name == "Alice"
        assert user.model_dump() == {"name": "Alice", "age": 25, "email": "alice@example.com", "score": 0.0}
        assert "name" in user.model_fields_set
    """

    # Class-level attributes set by metaclass
    __dhi_fields__: ClassVar[dict[str, dict[str, Any]]]
    __dhi_validators__: ClassVar[dict[str, Any]]
    __dhi_field_names__: ClassVar[list[str]]
    __dhi_private_attrs__: ClassVar[dict[str, PrivateAttr]]
    __dhi_computed_fields__: ClassVar[dict[str, ComputedFieldInfo]]

    # Pydantic v2 compatible class attributes
    model_config: ClassVar[ConfigDict | None] = None
    model_fields: ClassVar[dict[str, FieldInfo]]
    model_computed_fields: ClassVar[dict[str, ComputedFieldInfo]]

    # Instance attributes
    __pydantic_private__: dict[str, Any] | None
    __pydantic_extra__: dict[str, Any] | None
    __pydantic_fields_set__: set[str]

    def __init__(self, **kwargs: Any) -> None:
        cls = type(self)

        # --- ULTRA-FAST PATH: Full native init (handles EVERYTHING in C) ---
        if cls.__dhi_use_ultra_fast__:
            result = _dhi_native.init_model_full(
                self, kwargs, cls.__dhi_compiled_specs__, cls.__dhi_extra_mode_int__
            )
            if result is None:
                # Success! C code already set __pydantic_fields_set__, __pydantic_extra__, __pydantic_private__
                if cls.__dhi_needs_post_init__:
                    if cls.__dhi_private_attrs__:
                        self._init_private_attrs()
                    if cls.__dhi_has_post_init__:
                        self.model_post_init(None)
                return
            # result is list of (field_name, error_msg) tuples
            errors = [ValidationError(f, m) for f, m in result]
            raise ValidationErrors(errors)

        # --- HYBRID PATH: Native for simple fields, Python for nested/complex ---
        compiled = cls.__dhi_compiled_specs__
        nested_specs = cls.__dhi_nested_field_specs__
        if (
            compiled is not None
            and nested_specs
            and not cls.__dhi_has_custom_validators__
        ):
            _setattr = object.__setattr__

            # Step 1: Native init for simple fields
            result = _dhi_native.init_model_full(
                self, kwargs, compiled, cls.__dhi_extra_mode_int__
            )
            if result is not None:
                errors = [ValidationError(f, m) for f, m in result]
                raise ValidationErrors(errors)

            # Step 2: Handle nested/complex fields in Python (OPTIMIZED)
            errors: list[ValidationError] = []
            fields_set = self.__pydantic_fields_set__

            for (
                field_name,
                alias,
                required,
                default,
                default_factory,
                validator,
                base_type,
                is_nested_model,
            ) in nested_specs:
                # Get value from kwargs
                if alias and alias in kwargs:
                    value = kwargs[alias]
                    fields_set.add(field_name)
                elif field_name in kwargs:
                    value = kwargs[field_name]
                    fields_set.add(field_name)
                elif not required:
                    if default_factory is not None:
                        _setattr(self, field_name, default_factory())
                    else:
                        _setattr(
                            self,
                            field_name,
                            copy.deepcopy(default)
                            if isinstance(default, (list, dict, set))
                            else default,
                        )
                    continue
                else:
                    errors.append(ValidationError(field_name, "Field required"))
                    continue

                # FAST PATH: Nested model fields - use pre-computed flag
                if is_nested_model:
                    value_type = type(value)
                    if value_type is base_type or (
                        value_type is not dict and isinstance(value, base_type)
                    ):
                        # Already validated, just assign
                        _setattr(self, field_name, value)
                        continue
                    elif value_type is dict:
                        # Convert dict to model directly (bypass validator wrapper)
                        try:
                            _setattr(self, field_name, base_type(**value))
                        except (ValidationError, ValidationErrors) as e:
                            if isinstance(e, ValidationErrors):
                                for ve in e.errors:
                                    errors.append(
                                        ValidationError(
                                            f"{field_name}.{ve.field}", ve.message
                                        )
                                    )
                            else:
                                errors.append(ValidationError(field_name, str(e)))
                        continue

                try:
                    _setattr(self, field_name, validator(value))
                except ValidationError as e:
                    errors.append(e)

            if errors:
                raise ValidationErrors(errors)

            if cls.__dhi_needs_post_init__:
                if cls.__dhi_private_attrs__:
                    self._init_private_attrs()
                if cls.__dhi_has_post_init__:
                    self.model_post_init(None)
            return

        # --- STANDARD PATH (fallback for models with custom validators or no native support) ---
        _setattr = object.__setattr__

        # Get config values
        config = cls.model_config
        extra_mode = get_config_value(config, "extra", "ignore")

        fields_set: set[str] = set()
        _setattr(self, "__pydantic_fields_set__", fields_set)
        _setattr(self, "__pydantic_private__", None)
        _setattr(self, "__pydantic_extra__", None)

        # --- STANDARD PATH ---
        errors: list[ValidationError] = []

        field_validators = cls.__dhi_field_validator_funcs__
        model_validators_before = cls.__dhi_model_validators_before__
        model_validators_after = cls.__dhi_model_validators_after__

        # Run 'before' model validators
        for mv in model_validators_before:
            kwargs = mv(kwargs)

        # Track which kwargs keys we've consumed
        consumed_keys: set[str] = set()

        if not field_validators:
            # Fast path: no field validators (common case)
            for (
                field_name,
                required,
                default,
                default_factory,
                alias,
                validator,
                field_info,
            ) in cls.__dhi_fast_fields__:
                if alias and alias in kwargs:
                    value = kwargs[alias]
                    consumed_keys.add(alias)
                    fields_set.add(field_name)
                elif field_name in kwargs:
                    value = kwargs[field_name]
                    consumed_keys.add(field_name)
                    fields_set.add(field_name)
                elif not required:
                    if default_factory is not None:
                        _setattr(self, field_name, default_factory())
                    else:
                        _setattr(
                            self,
                            field_name,
                            copy.deepcopy(default)
                            if isinstance(default, (list, dict, set))
                            else default,
                        )
                    continue
                else:
                    errors.append(ValidationError(field_name, "Field required"))
                    continue

                try:
                    _setattr(self, field_name, validator(value))
                except ValidationError as e:
                    errors.append(e)
        else:
            # Slow path: has field validators
            for (
                field_name,
                required,
                default,
                default_factory,
                alias,
                validator,
                field_info,
            ) in cls.__dhi_fast_fields__:
                if alias and alias in kwargs:
                    value = kwargs[alias]
                    consumed_keys.add(alias)
                    fields_set.add(field_name)
                elif field_name in kwargs:
                    value = kwargs[field_name]
                    consumed_keys.add(field_name)
                    fields_set.add(field_name)
                elif not required:
                    if default_factory is not None:
                        _setattr(self, field_name, default_factory())
                    else:
                        _setattr(
                            self,
                            field_name,
                            copy.deepcopy(default)
                            if isinstance(default, (list, dict, set))
                            else default,
                        )
                    continue
                else:
                    errors.append(ValidationError(field_name, "Field required"))
                    continue

                try:
                    validated = validator(value)
                    if field_name in field_validators:
                        for fv in field_validators[field_name]:
                            validated = fv(validated)
                    _setattr(self, field_name, validated)
                except ValidationError as e:
                    errors.append(e)

        # Handle extra fields
        extra_keys = set(kwargs.keys()) - consumed_keys
        if extra_keys:
            if extra_mode == "forbid":
                for key in extra_keys:
                    errors.append(
                        ValidationError(key, "Extra inputs are not permitted")
                    )
            elif extra_mode == "allow":
                extra_data = {k: kwargs[k] for k in extra_keys}
                _setattr(self, "__pydantic_extra__", extra_data)
            # 'ignore' mode: do nothing

        if errors:
            raise ValidationErrors(errors)

        # Initialize private attributes
        self._init_private_attrs()

        # Run 'after' model validators
        for mv in model_validators_after:
            mv(self)

        # Call model_post_init hook
        self.model_post_init(None)

    def _init_private_attrs(self) -> None:
        """Initialize private attributes with their defaults."""
        cls = type(self)
        private_attrs = cls.__dhi_private_attrs__
        if not private_attrs:
            return

        private_data: dict[str, Any] = {}
        for attr_name, private_attr in private_attrs.items():
            with contextlib.suppress(ValueError):
                private_data[attr_name] = private_attr.get_default()
        if private_data:
            # dynamic-attr: bypass BaseModel's guarding __setattr__ to store the private-attr container directly
            object.__setattr__(self, "__pydantic_private__", private_data)

    @property
    def model_fields_set(self) -> set[str]:
        """Set of fields that were explicitly set during initialization."""
        return self.__pydantic_fields_set__

    @property
    def model_extra(self) -> dict[str, Any] | None:
        """Extra fields when model_config extra='allow'."""
        return self.__pydantic_extra__

    def model_post_init(self, __context: Any) -> None:
        """Called after model initialization.

        Override this method to perform additional initialization.
        This matches Pydantic v2's model_post_init hook.

        Args:
            __context: Validation context (currently unused).
        """
        pass

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Collect field_validator and model_validator decorated methods
        field_validator_funcs: dict[str, list[Callable]] = {}
        model_validators_before: list[Callable] = []
        model_validators_after: list[Callable] = []

        # Check class __dict__ directly to find decorated methods
        # This handles @classmethod, @staticmethod wrapping properly
        for attr_name, raw_attr in cls.__dict__.items():
            if attr_name.startswith("__"):
                continue

            # Check both the raw attribute and unwrapped function for validator markers
            # Decorators may set attrs on either the wrapper or the inner function
            candidates = [raw_attr]
            if isinstance(raw_attr, (classmethod, staticmethod)):
                candidates.append(raw_attr.__func__)

            validator_fields = None
            model_validator_flag = False
            validator_mode = "after"

            for candidate in candidates:
                if hasattr(candidate, "__validator_fields__"):
                    validator_fields = candidate.__validator_fields__
                    # dynamic-attr: __validator_mode__ is stamped onto the user's function by the validator decorators; may be absent
                    validator_mode = getattr(candidate, "__validator_mode__", "after")
                    break
                if hasattr(candidate, "__model_validator__"):
                    model_validator_flag = True
                    # dynamic-attr: __validator_mode__ is stamped onto the user's function by the validator decorators; may be absent
                    validator_mode = getattr(candidate, "__validator_mode__", "after")
                    break

            if validator_fields:
                # dynamic-attr: attr_name is a user-declared validator method name, bound via its descriptor
                bound = getattr(cls, attr_name)
                for field_name in validator_fields:
                    if field_name not in field_validator_funcs:
                        field_validator_funcs[field_name] = []
                    field_validator_funcs[field_name].append(bound)

            if model_validator_flag:
                # dynamic-attr: attr_name is a user-declared validator method name, bound via its descriptor
                bound = getattr(cls, attr_name)
                if validator_mode == "before":
                    model_validators_before.append(bound)
                else:
                    model_validators_after.append(bound)

        cls.__dhi_field_validator_funcs__ = field_validator_funcs
        cls.__dhi_model_validators_before__ = model_validators_before
        cls.__dhi_model_validators_after__ = model_validators_after
        has_custom = bool(
            field_validator_funcs or model_validators_before or model_validators_after
        )
        cls.__dhi_has_custom_validators__ = has_custom
        # Update combined ultra-fast flag (single check in __init__)
        cls.__dhi_use_ultra_fast__ = cls.__dhi_full_native__ and not has_custom

    @classmethod
    def model_construct(
        cls: type[_T],
        _fields_set: set[str] | None = None,
        **values: Any,
    ) -> _T:
        """Create a model instance without running validation.

        This is useful when you have pre-validated or trusted data
        and want to skip validation for performance.

        Matches Pydantic v2's model_construct() exactly.

        Args:
            _fields_set: Set of field names to mark as explicitly set.
            **values: Field values to set on the model.

        Returns:
            A new model instance with values set directly.

        Example:
            # Skip validation for trusted data
            user = User.model_construct(name="Alice", age=25)
        """
        obj = object.__new__(cls)
        _setattr = object.__setattr__

        # Initialize tracking attributes
        fields_set = _fields_set if _fields_set is not None else set(values.keys())
        _setattr(obj, "__pydantic_fields_set__", fields_set)
        _setattr(obj, "__pydantic_private__", None)
        _setattr(obj, "__pydantic_extra__", None)

        # Set field values (with defaults for missing fields)
        for field_name, field_data in cls.__dhi_fields__.items():
            if field_name in values:
                _setattr(obj, field_name, values[field_name])
            else:
                default = field_data["default"]
                default_factory = field_data.get("default_factory")
                if default_factory is not None:
                    _setattr(obj, field_name, default_factory())
                elif default is not _MISSING:
                    _setattr(
                        obj,
                        field_name,
                        copy.deepcopy(default)
                        if isinstance(default, (list, dict, set))
                        else default,
                    )

        # Initialize private attributes
        obj._init_private_attrs()

        return obj

    @classmethod
    def model_validate(
        cls: type[_T],
        obj: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool = False,
        context: dict[str, Any] | None = None,
    ) -> _T:
        """Validate data and create a model instance.

        Matches Pydantic v2's model_validate() exactly.

        Args:
            obj: Data to validate (dict, object with attributes, or model instance).
            strict: If True, enforce strict validation.
            from_attributes: If True, extract data from object attributes (ORM mode).
            context: Optional validation context.

        Returns:
            Validated model instance.
        """
        # Handle model instances
        if isinstance(obj, cls):
            return obj

        # Handle from_attributes (ORM mode)
        if (
            from_attributes
            or get_config_value(cls.model_config, "from_attributes", False)
        ) and hasattr(obj, "__dict__"):
            data = {}
            for field_name in cls.__dhi_field_names__:
                if hasattr(obj, field_name):
                    # dynamic-attr: from_attributes/ORM mode reads fields off an arbitrary source object by runtime field name
                    data[field_name] = getattr(obj, field_name)
            return cls(**data)

        # Handle dict input - FAST PATH: bypass **kwargs unpacking
        if isinstance(obj, dict):
            # Fast path for simple models with native init
            compiled = cls.__dhi_compiled_specs__
            if (
                compiled is not None
                and cls.__dhi_full_native__
                and not cls.__dhi_has_custom_validators__
            ):
                instance = object.__new__(cls)
                result = _dhi_native.init_model_full(
                    instance, obj, compiled, cls.__dhi_extra_mode_int__
                )
                if result is None:
                    if cls.__dhi_needs_post_init__:
                        if cls.__dhi_private_attrs__:
                            instance._init_private_attrs()
                        if cls.__dhi_has_post_init__:
                            instance.model_post_init(None)
                    return instance
                errors = [ValidationError(f, m) for f, m in result]
                raise ValidationErrors(errors)
            # Standard path
            return cls(**obj)

        raise ValidationError(
            "__root__", f"Expected dict or {cls.__name__}, got {type(obj).__name__}"
        )

    @classmethod
    def model_validate_json(
        cls: type[_T],
        json_data: str | bytes,
        *,
        strict: bool | None = None,
        context: dict[str, Any] | None = None,
    ) -> _T:
        """Validate JSON data and create a model instance.

        Matches Pydantic v2's model_validate_json() exactly.

        Args:
            json_data: JSON string or bytes to validate.
            strict: If True, enforce strict validation.
            context: Optional validation context.

        Returns:
            Validated model instance.

        Example:
            user = User.model_validate_json('{"name": "Alice", "age": 25}')
        """
        if isinstance(json_data, bytes):
            json_data = json_data.decode("utf-8")
        data = fast_json_loads(json_data)
        return cls.model_validate(data, strict=strict, context=context)

    # Alias for API consistency with Struct
    from_json = model_validate_json

    @classmethod
    def model_validate_strings(
        cls: type[_T],
        obj: Mapping[str, Any],
        *,
        strict: bool | None = None,
        context: dict[str, Any] | None = None,
    ) -> _T:
        """Validate a mapping with string keys and coerce string values to field types.

        Matches Pydantic v2's model_validate_strings(): all values are expected
        to be strings (e.g., from HTML form data or query parameters) and are
        coerced to the declared field types before validation.

        Coercion rules:
            str → int:   int(value)  (empty string → use default)
            str → float: float(value)
            str → bool:  value in {"true","1","on","yes"} (case-insensitive)
            str → str:   no change

        Args:
            obj: Mapping with string keys and string values.
            strict: If True, enforce strict validation (no coercion).
            context: Optional validation context.

        Returns:
            Validated model instance.
        """
        data = dict(obj)

        if not strict:
            from typing import Union, get_args, get_origin

            from hyperdjango.conf import parse_bool

            def _unwrap_optional(tp):
                """Unwrap Optional[X] → X. Handles Union[X, None]."""
                origin = get_origin(tp)
                if origin is Union:
                    args = get_args(tp)
                    non_none = [a for a in args if a is not type(None)]
                    if len(non_none) == 1:
                        return non_none[0]
                return tp

            fields = cls.__dhi_fields__
            for key, value in list(data.items()):
                if not isinstance(value, str):
                    continue
                field_data = fields.get(key)
                if not field_data:
                    continue
                base_type = _unwrap_optional(field_data["base_type"])
                if base_type is int:
                    if value == "":
                        # Empty string for int → remove so default kicks in
                        del data[key]
                    else:
                        with contextlib.suppress(ValueError, TypeError):
                            data[key] = int(value)
                elif base_type is float:
                    if value == "":
                        del data[key]
                    else:
                        with contextlib.suppress(ValueError, TypeError):
                            data[key] = float(value)
                elif base_type is bool:
                    data[key] = parse_bool(value)

        return cls.model_validate(data, strict=strict, context=context)

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] = "python",
        include: IncEx = None,
        exclude: IncEx = None,
        by_alias: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        round_trip: bool = False,
        warnings: bool = True,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]:
        """Convert model to dictionary.

        Matches Pydantic v2's model_dump() exactly.

        Args:
            mode: 'python' returns Python objects, 'json' returns JSON-compatible types.
            include: Fields to include. Can be a set of names or nested dict.
            exclude: Fields to exclude. Can be a set of names or nested dict.
            by_alias: Use field aliases in output keys.
            exclude_unset: Exclude fields that weren't explicitly set.
            exclude_defaults: Exclude fields that equal their default value.
            exclude_none: Exclude fields with None values.
            round_trip: Enable round-trip serialization mode.
            warnings: Whether to emit warnings.
            serialize_as_any: Serialize as Any type.

        Returns:
            Dictionary representation of the model.

        Example:
            user = User(name="Alice", age=25, score=0.0)
            user.model_dump(exclude_defaults=True)  # Excludes score=0.0
        """
        cls = type(self)

        # FAST PATH: Native C dump (handles nested models recursively now)
        compiled = cls.__dhi_compiled_specs__
        if (
            compiled is not None
            and mode == "python"
            and not include
            and not exclude
            and not by_alias
            and not exclude_unset
            and not exclude_defaults
            and not exclude_none
        ):
            return _dhi_native.dump_model_compiled(self, compiled)

        result: dict[str, Any] = {}

        # Convert include/exclude to sets if they're dicts (simplified handling)
        include_set = set(include.keys()) if isinstance(include, dict) else include
        exclude_set = set(exclude.keys()) if isinstance(exclude, dict) else exclude

        for field_name in self.__dhi_field_names__:
            # Check exclude
            if exclude_set and field_name in exclude_set:
                continue

            # Check field-level exclude from FieldInfo
            field_info = cls.model_fields.get(field_name)
            if field_info and field_info.exclude:
                continue

            # Check include
            if include_set and field_name not in include_set:
                continue

            # Get the value
            # dynamic-attr: field_name is a runtime field name that may be unset on the instance (validation-error path)
            value = getattr(self, field_name, None)

            # Check exclude_unset
            if exclude_unset and field_name not in self.__pydantic_fields_set__:
                continue

            # Check exclude_defaults
            if exclude_defaults:
                field_data = cls.__dhi_fields__.get(field_name, {})
                default = field_data.get("default", _MISSING)
                default_factory = field_data.get("default_factory")
                if (
                    default_factory is None
                    and default is not _MISSING
                    and value == default
                ):
                    continue

            # Check exclude_none
            if exclude_none and value is None:
                continue

            # Determine output key
            output_key = field_name
            if by_alias and field_info:
                # serialization_alias > alias > field_name
                output_key = (
                    field_info.serialization_alias or field_info.alias or field_name
                )

            # Handle nested models
            if isinstance(value, BaseModel):
                value = value.model_dump(
                    mode=mode,
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                )
            elif isinstance(value, list):
                value = [
                    v.model_dump(
                        mode=mode,
                        by_alias=by_alias,
                        exclude_unset=exclude_unset,
                        exclude_defaults=exclude_defaults,
                        exclude_none=exclude_none,
                    )
                    if isinstance(v, BaseModel)
                    else v
                    for v in value
                ]
            elif isinstance(value, dict):
                value = {
                    k: v.model_dump(
                        mode=mode,
                        by_alias=by_alias,
                        exclude_unset=exclude_unset,
                        exclude_defaults=exclude_defaults,
                        exclude_none=exclude_none,
                    )
                    if isinstance(v, BaseModel)
                    else v
                    for k, v in value.items()
                }

            # JSON mode conversion
            if mode == "json":
                value = self._serialize_for_json(value)

            result[output_key] = value

        # Include computed fields
        computed_fields = cls.__dhi_computed_fields__
        for comp_name, comp_info in computed_fields.items():
            if exclude_set and comp_name in exclude_set:
                continue
            if include_set and comp_name not in include_set:
                continue

            # dynamic-attr: comp_name is a user-declared computed-field property name, evaluated via its descriptor
            value = getattr(self, comp_name)
            output_key = comp_name
            if by_alias and comp_info.alias:
                output_key = comp_info.alias

            if mode == "json":
                value = self._serialize_for_json(value)

            result[output_key] = value

        # Include extra fields if present
        if self.__pydantic_extra__:
            for key, value in self.__pydantic_extra__.items():
                if exclude_set and key in exclude_set:
                    continue
                if include_set and key not in include_set:
                    continue
                if exclude_none and value is None:
                    continue
                if mode == "json":
                    value = self._serialize_for_json(value)
                result[key] = value

        return result

    def _serialize_for_json(self, value: Any) -> Any:
        """Convert a value to JSON-compatible types."""
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (list, tuple)):
            return [self._serialize_for_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self._serialize_for_json(v) for k, v in value.items()}
        if isinstance(value, set):
            return list(value)
        if hasattr(value, "isoformat"):  # datetime, date
            return value.isoformat()
        if hasattr(value, "__str__"):
            return str(value)
        return value

    def model_dump_json(
        self,
        *,
        indent: int | None = None,
        include: IncEx = None,
        exclude: IncEx = None,
        by_alias: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        round_trip: bool = False,
        warnings: bool = True,
        serialize_as_any: bool = False,
    ) -> str:
        """Convert model to JSON string.

        Matches Pydantic v2's model_dump_json() exactly.

        Args:
            indent: Indentation level for pretty printing.
            include: Fields to include.
            exclude: Fields to exclude.
            by_alias: Use field aliases in output keys.
            exclude_unset: Exclude fields that weren't explicitly set.
            exclude_defaults: Exclude fields that equal their default value.
            exclude_none: Exclude fields with None values.
            round_trip: Enable round-trip serialization mode.
            warnings: Whether to emit warnings.
            serialize_as_any: Serialize as Any type.

        Returns:
            JSON string representation of the model.
        """
        # Fast path: native C JSON serialization (only for simple cases)
        cls = type(self)
        compiled = cls.__dhi_compiled_specs__
        if (
            compiled is not None
            and indent is None
            and not include
            and not exclude
            and not by_alias
            and not exclude_unset
            and not exclude_defaults
            and not exclude_none
        ):
            try:
                return _dhi_native.dump_json_compiled(self, compiled)
            # blind-except: the native compiled JSON dump is a fast-path optimization; any failure falls through to the equivalent Python serializer below.
            except Exception:
                pass  # Fall back to Python

        # Standard path: dump to dict then serialize
        data = self.model_dump(
            mode="json",
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )
        return _stdlib_json.dumps(data, indent=indent, ensure_ascii=False)

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        """Generate JSON Schema for this model.

        Matches Pydantic's model_json_schema() classmethod.
        """
        schema: dict[str, Any] = {
            "title": cls.__name__,
            "type": "object",
            "properties": {},
            "required": [],
        }

        type_map = {
            int: "integer",
            float: "number",
            str: "string",
            bool: "boolean",
            bytes: "string",
        }

        for field_name, field_info in cls.__dhi_fields__.items():
            base_type = field_info["base_type"]
            constraints = field_info["constraints"]

            prop: dict[str, Any] = {}

            # Base type
            json_type = type_map.get(base_type, "string")
            prop["type"] = json_type

            # Apply constraints to schema
            for c in constraints:
                if isinstance(c, Gt):
                    prop["exclusiveMinimum"] = c.gt
                elif isinstance(c, Ge):
                    prop["minimum"] = c.ge
                elif isinstance(c, Lt):
                    prop["exclusiveMaximum"] = c.lt
                elif isinstance(c, Le):
                    prop["maximum"] = c.le
                elif isinstance(c, MultipleOf):
                    prop["multipleOf"] = c.multiple_of
                elif isinstance(c, MinLength):
                    prop["minLength"] = c.min_length
                elif isinstance(c, MaxLength):
                    prop["maxLength"] = c.max_length
                elif isinstance(c, Pattern):
                    prop["pattern"] = c.pattern
                elif isinstance(c, FieldInfo):
                    if c.gt is not None:
                        prop["exclusiveMinimum"] = c.gt
                    if c.ge is not None:
                        prop["minimum"] = c.ge
                    if c.lt is not None:
                        prop["exclusiveMaximum"] = c.lt
                    if c.le is not None:
                        prop["maximum"] = c.le
                    if c.multiple_of is not None:
                        prop["multipleOf"] = c.multiple_of
                    if c.min_length is not None:
                        prop["minLength"] = c.min_length
                    if c.max_length is not None:
                        prop["maxLength"] = c.max_length
                    if c.pattern is not None:
                        prop["pattern"] = c.pattern
                    if c.title:
                        prop["title"] = c.title
                    if c.description:
                        prop["description"] = c.description
                    if c.examples:
                        prop["examples"] = c.examples

            # Default value
            if not field_info["required"]:
                prop["default"] = field_info["default"]

            schema["properties"][field_name] = prop

            if field_info["required"]:
                schema["required"].append(field_name)

        return schema

    def model_copy(
        self: _T,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> _T:
        """Create a copy of the model with optional field updates.

        Matches Pydantic v2's model_copy() exactly.

        Args:
            update: Dictionary of field values to update.
            deep: If True, perform a deep copy.

        Returns:
            A new model instance with copied (and optionally updated) values.

        Example:
            user2 = user.model_copy(update={'name': 'Bob'})
            user3 = user.model_copy(deep=True)
        """
        data = copy.deepcopy(self.model_dump()) if deep else self.model_dump()

        if update:
            data.update(update)

        # Preserve fields_set from original plus any updated fields
        new_fields_set = self.__pydantic_fields_set__.copy()
        if update:
            new_fields_set.update(update.keys())

        # Create new instance
        new_obj = self.__class__.model_construct(_fields_set=new_fields_set, **data)
        return new_obj

    def __setattr__(self, name: str, value: Any) -> None:
        """Set attribute with frozen/validate_assignment support."""
        cls = type(self)
        config = cls.model_config

        # Check if model is frozen
        if get_config_value(config, "frozen", False):
            raise TypeError(
                f"{cls.__name__} is frozen and does not support item assignment"
            )

        # Check if field is frozen
        if name in cls.model_fields:
            field_info = cls.model_fields[name]
            if field_info.frozen:
                raise TypeError(f"Field '{name}' is frozen and cannot be modified")

            # Validate on assignment if configured
            if get_config_value(config, "validate_assignment", False):
                validator = cls.__dhi_validators__.get(name)
                if validator:
                    value = validator(value)

                # Update fields_set
                if hasattr(self, "__pydantic_fields_set__"):
                    self.__pydantic_fields_set__.add(name)

        # dynamic-attr: this IS BaseModel.__setattr__; object.__setattr__ stores the value without re-entering this override
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        """Delete attribute (blocked if frozen)."""
        cls = type(self)
        if get_config_value(cls.model_config, "frozen", False):
            raise TypeError(
                f"{cls.__name__} is frozen and does not support item deletion"
            )
        object.__delattr__(self, name)

    def __repr__(self) -> str:
        """String representation of the model."""
        cls = type(self)
        parts = []
        for name in self.__dhi_field_names__:
            if hasattr(self, name):
                field_info = cls.model_fields.get(name)
                # Check repr flag on FieldInfo
                if field_info and field_info.repr is False:
                    continue
                # dynamic-attr: name is a field name resolved at runtime.
                parts.append(f"{name}={getattr(self, name)!r}")
        return f"{cls.__name__}({', '.join(parts)})"

    def __str__(self) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.model_dump() == other.model_dump()

    def __hash__(self) -> int:
        # Note: Pydantic raises error if model is mutable, but we allow it
        try:
            return hash(tuple(sorted(self.model_dump().items())))
        except TypeError:
            # Unhashable values in the model
            raise TypeError(f"unhashable type: '{type(self).__name__}'")

    def __iter__(self) -> Iterator[str]:
        """Iterate over field names."""
        return iter(self.__dhi_field_names__)

    def __getitem__(self, key: str) -> Any:
        """Get field value by name (dict-like access)."""
        if key in self.__dhi_field_names__:
            # dynamic-attr: dict-style access by runtime field name.
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        """Check if field exists."""
        return key in self.__dhi_field_names__

    @classmethod
    def model_rebuild(
        cls,
        *,
        force: bool = False,
        raise_errors: bool = True,
        _parent_namespace_depth: int = 2,
        _types_namespace: dict[str, Any] | None = None,
    ) -> bool | None:
        """Rebuild model schema, resolving forward references.

        Call this after all referenced types are defined so that
        forward-referenced annotations can be resolved.

        Matches Pydantic v2's model_rebuild().

        Args:
            force: Force rebuild even if fields are already resolved.
            raise_errors: If True, raise on resolution failure.
            _parent_namespace_depth: Stack depth to find caller's namespace.
            _types_namespace: Explicit namespace for type resolution.

        Returns:
            True if rebuild succeeded, None if not needed.
        """
        # Skip if already has fields and not forced. __dhi_fields__ is
        # intentionally NOT set on base BaseModel (its absence is what
        # _is_basemodel_subclass keys on), so it may be missing here.
        # dynamic-attr: __dhi_fields__ is absent on a not-yet-compiled class; presence gates rebuild
        if not force and getattr(cls, "__dhi_fields__", None):
            return None

        # Build namespace for resolving forward references
        module = sys.modules.get(cls.__module__, None)
        globalns = module.__dict__ if module else {}
        localns = {cls.__name__: cls}

        # Merge caller's frame locals for types defined in the same scope
        try:
            frame = sys._getframe(_parent_namespace_depth)
            localns.update(frame.f_locals)
        except ValueError, AttributeError:
            pass

        # Merge explicit namespace if provided
        if _types_namespace:
            localns.update(_types_namespace)

        try:
            hints = get_type_hints(
                cls, globalns=globalns, localns=localns, include_extras=True
            )
        # blind-except: model_rebuild honors its raise_errors flag — opt-in callers get the hint-resolution failure re-raised; opt-out callers get None so a premature rebuild (types not yet defined) can be retried later.
        except Exception:
            if raise_errors:
                raise
            return None

        # Re-compile fields, validators, and native specs
        _compile_model_fields(cls, hints)

        # Re-run __init_subclass__ logic to update custom validator flags
        has_custom = cls.__dhi_has_custom_validators__
        cls.__dhi_use_ultra_fast__ = cls.__dhi_full_native__ and not has_custom

        return True

    @classmethod
    def model_parametrized_name(cls, params: tuple[type[Any], ...]) -> str:
        """Generate parametrized class name for generics.

        Matches Pydantic v2's model_parametrized_name().
        """
        param_names = ", ".join(
            p.__name__ if hasattr(p, "__name__") else str(p) for p in params
        )
        return f"{cls.__name__}[{param_names}]"


# Pin the generic BaseModel.__init__ so the metaclass can recognize it as
# dhi-managed (safe to override) and pin it on classes that don't qualify for
# the specialized fast init. Must run after BaseModel is fully defined.
_GENERIC_INIT = BaseModel.__init__
_GENERIC_INIT._dhi_managed = True

__all__ = ["BaseModel", "IncEx"]
