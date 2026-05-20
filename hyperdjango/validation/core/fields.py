"""
Field definition for dhi - Pydantic v2 compatible.

Provides the Field() function for defining field-level constraints
and metadata, matching Pydantic's API.

Example:
    from typing import Annotated
    from dhi import BaseModel, Field

    class User(BaseModel):
        name: Annotated[str, Field(min_length=1, max_length=100)]
        age: Annotated[int, Field(gt=0, le=120)]
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hyperdjango.fields import CustomField

_MISSING = object()  # Sentinel for unset defaults


@dataclass(slots=True, repr=False)
class FieldInfo:
    """Stores field constraints and metadata.

    This is the object returned by Field() and can be used in Annotated types.
    Pydantic v2 compatible.

    Note: default, default_factory, examples, json_schema_extra use Any because
    FieldInfo is the meta-programming core — it describes fields of arbitrary
    types. This is the one place Any is genuinely correct.
    """

    default: Any = _MISSING
    default_factory: Callable | None = None
    alias: str | None = None
    validation_alias: str | None = None
    serialization_alias: str | None = None
    title: str | None = None
    description: str | None = None
    examples: list[Any] | None = None
    gt: int | float | None = None
    ge: int | float | None = None
    lt: int | float | None = None
    le: int | float | None = None
    multiple_of: int | float | None = None
    strict: bool | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    strip_whitespace: bool | None = None
    to_lower: bool | None = None
    to_upper: bool | None = None
    allow_inf_nan: bool | None = None
    max_digits: int | None = None
    decimal_places: int | None = None
    unique_items: bool | None = None
    exclude: bool | None = None
    include: bool | None = None
    discriminator: str | None = None
    json_schema_extra: dict[str, Any] | None = None
    frozen: bool | None = None
    validate_default: bool | None = None
    repr: bool | None = True
    init: bool | None = None
    init_var: bool | None = None
    kw_only: bool | None = None
    annotation: type | None = None

    # ── HyperDjango database metadata (set by models.Field wrapper) ──
    primary_key: bool = False
    auto: bool = False
    unique: bool = False
    index: bool = False
    # editable=False → the field is never writable through a ModelSerializer
    # (always read-only, even under fields="__all__"). Privileged code still
    # assigns it directly on the instance. Used to protect security-sensitive
    # fields (is_staff, is_superuser, password_hash) from mass assignment.
    editable: bool = True
    foreign_key: str | type | None = None
    related_name: str | None = None
    one_to_one: bool = False
    on_delete: str | None = (
        None  # FK action: "CASCADE", "SET NULL", "RESTRICT", "SET DEFAULT", "NO ACTION"
    )
    db_default: str | None = (
        None  # SQL DEFAULT expression (e.g. "now()") separate from Python default
    )
    db_type: str | None = (
        None  # explicit SQL column type override (e.g. "BIGINT"); wins over annotation inference
    )
    big: bool = (
        False  # widen integer storage to 64-bit: an auto PK → BIGSERIAL, an int FK → BIGINT,
        # a plain auto/int column → BIGINT. Opt-in for high-volume append/cursor tables whose
        # 32-bit SERIAL (~2.1B) would overflow. Additive: default False keeps INTEGER/SERIAL.
    )

    # ── File field metadata (set by FileField/ImageField) ──
    upload_to: str | None = None
    file_field_type: str | None = None  # "file" or "image"
    allowed_extensions: tuple[str, ...] | None = None

    # ── Vector field metadata (set by VectorField for pgvector) ──
    vector_dimensions: int | None = None
    vector_index_type: str | None = None  # "hnsw" or "ivfflat"
    vector_index_ops: str | None = (
        None  # "vector_cosine_ops", "vector_l2_ops", "vector_ip_ops"
    )
    vector_index_params: dict[str, int] | None = None  # WITH (m=16, ef_construction=64)

    # ── Custom field metadata (set by create_field in hyperdjango.fields) ──
    custom_field: CustomField | None = None

    @property
    def is_required(self) -> bool:
        return self.default is _MISSING and self.default_factory is None

    def get_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is _MISSING:
            raise ValueError("Field is required")
        return self.default

    def __repr__(self) -> str:
        parts = []
        if self.default is not _MISSING:
            parts.append(f"default={self.default!r}")
        if self.gt is not None:
            parts.append(f"gt={self.gt}")
        if self.ge is not None:
            parts.append(f"ge={self.ge}")
        if self.lt is not None:
            parts.append(f"lt={self.lt}")
        if self.le is not None:
            parts.append(f"le={self.le}")
        if self.multiple_of is not None:
            parts.append(f"multiple_of={self.multiple_of}")
        if self.min_length is not None:
            parts.append(f"min_length={self.min_length}")
        if self.max_length is not None:
            parts.append(f"max_length={self.max_length}")
        if self.pattern is not None:
            parts.append(f"pattern={self.pattern!r}")
        if self.strict:
            parts.append("strict=True")
        return f"FieldInfo({', '.join(parts)})"


def Field(
    default: Any = _MISSING,
    *,
    default_factory: Callable | None = None,
    alias: str | None = None,
    validation_alias: str | None = None,
    serialization_alias: str | None = None,
    title: str | None = None,
    description: str | None = None,
    examples: list[Any] | None = None,
    gt: int | float | None = None,
    ge: int | float | None = None,
    lt: int | float | None = None,
    le: int | float | None = None,
    multiple_of: int | float | None = None,
    strict: bool | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    strip_whitespace: bool | None = None,
    to_lower: bool | None = None,
    to_upper: bool | None = None,
    allow_inf_nan: bool | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    unique_items: bool | None = None,
    exclude: bool | None = None,
    include: bool | None = None,
    discriminator: str | None = None,
    json_schema_extra: dict[str, Any] | None = None,
    frozen: bool | None = None,
    validate_default: bool | None = None,
    repr: bool | None = True,
    init: bool | None = None,
    init_var: bool | None = None,
    kw_only: bool | None = None,
    # HyperDjango database metadata
    primary_key: bool = False,
    auto: bool = False,
    unique: bool = False,
    index: bool = False,
    editable: bool = True,
    foreign_key: str | type | None = None,
    related_name: str | None = None,
    one_to_one: bool = False,
    on_delete: str | None = None,
    db_type: str | None = None,
    big: bool = False,
    # File field metadata
    upload_to: str | None = None,
    file_field_type: str | None = None,
    allowed_extensions: tuple[str, ...] | None = None,
    # Vector field metadata (pgvector)
    vector_dimensions: int | None = None,
    vector_index_type: str | None = None,
    vector_index_ops: str | None = None,
    vector_index_params: dict[str, int] | None = None,
    # Custom field metadata (hyperdjango.fields)
    custom_field: CustomField | None = None,
) -> FieldInfo:
    """Create a FieldInfo with constraints and metadata.

    Matches Pydantic v2's Field() function signature exactly.

    Args:
        default: Default value for the field.
        default_factory: Callable to generate default value.
        alias: Alias for validation and serialization.
        validation_alias: Alias used only during validation.
        serialization_alias: Alias used only during serialization.
        title: Human-readable title for JSON schema.
        description: Human-readable description for JSON schema.
        examples: Example values for JSON schema.
        gt: Value must be greater than this.
        ge: Value must be greater than or equal to this.
        lt: Value must be less than this.
        le: Value must be less than or equal to this.
        multiple_of: Value must be a multiple of this.
        strict: If True, no type coercion is performed.
        min_length: Minimum length for strings/collections.
        max_length: Maximum length for strings/collections.
        pattern: Regex pattern for string validation.
        strip_whitespace: Strip leading/trailing whitespace from strings.
        to_lower: Convert string to lowercase.
        to_upper: Convert string to uppercase.
        allow_inf_nan: Allow infinity and NaN for floats.
        max_digits: Maximum digits for Decimal.
        decimal_places: Maximum decimal places for Decimal.
        unique_items: Require unique items in list.
        exclude: Exclude field from serialization.
        include: Include field in serialization (deprecated).
        discriminator: Field name for tagged union discrimination.
        json_schema_extra: Extra properties for JSON schema.
        frozen: If True, field is immutable after creation.
        validate_default: If True, validate default value.
        repr: If True, include field in __repr__.
        init: If True, include in __init__ (dataclass compat).
        init_var: If True, init-only variable (dataclass compat).
        kw_only: If True, keyword-only argument (dataclass compat).

    Example:
        from typing import Annotated
        from dhi import Field

        # Numeric constraints
        age: Annotated[int, Field(gt=0, le=120)]

        # String constraints with alias
        name: Annotated[str, Field(min_length=1, alias='userName')]

        # Frozen field
        id: Annotated[str, Field(frozen=True)]
    """
    return FieldInfo(
        default=default,
        default_factory=default_factory,
        alias=alias,
        validation_alias=validation_alias,
        serialization_alias=serialization_alias,
        title=title,
        description=description,
        examples=examples,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        multiple_of=multiple_of,
        strict=strict,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        strip_whitespace=strip_whitespace,
        to_lower=to_lower,
        to_upper=to_upper,
        allow_inf_nan=allow_inf_nan,
        max_digits=max_digits,
        decimal_places=decimal_places,
        unique_items=unique_items,
        exclude=exclude,
        include=include,
        discriminator=discriminator,
        json_schema_extra=json_schema_extra,
        frozen=frozen,
        validate_default=validate_default,
        repr=repr,
        init=init,
        init_var=init_var,
        kw_only=kw_only,
        primary_key=primary_key,
        editable=editable,
        auto=auto,
        unique=unique,
        index=index,
        foreign_key=foreign_key,
        related_name=related_name,
        one_to_one=one_to_one,
        on_delete=on_delete,
        db_type=db_type,
        big=big,
        upload_to=upload_to,
        file_field_type=file_field_type,
        allowed_extensions=allowed_extensions,
        vector_dimensions=vector_dimensions,
        vector_index_type=vector_index_type,
        vector_index_ops=vector_index_ops,
        vector_index_params=vector_index_params,
        custom_field=custom_field,
    )


__all__ = ["Field", "FieldInfo"]
