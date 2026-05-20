"""
Standalone serializer layer for HyperDjango APIs.

Defines read/write shapes separate from database models. Supports nested
serialization, computed fields, read-only/write-only fields, and validation
via the FieldInfo system.

Usage:
    from hyperdjango.serializers import Serializer, SerializerField

    class UserSerializer(Serializer):
        id: int = SerializerField(read_only=True)
        username: str = SerializerField(min_length=1, max_length=150)
        email: str = SerializerField(max_length=254)
        password: str = SerializerField(write_only=True, min_length=8)
        full_name: str = SerializerField(read_only=True, source="compute_full_name")

        def compute_full_name(self, obj):
            return f"{obj.get('first_name', '')} {obj.get('last_name', '')}".strip()

    # Serialize (object → dict for API response)
    serializer = UserSerializer(obj=user_dict)
    data = serializer.data  # excludes write_only fields, includes computed

    # Deserialize (input dict → validated data)
    serializer = UserSerializer(input_data=request_json)
    if serializer.is_valid():
        clean = serializer.validated_data  # excludes read_only fields
    else:
        errors = serializer.errors

    # Nested serialization
    class PostSerializer(Serializer):
        id: int = SerializerField(read_only=True)
        title: str = SerializerField(max_length=200)
        author: UserSerializer = SerializerField(read_only=True)

    # Many serialization
    serializer = UserSerializer(obj=list_of_users, many=True)
    data = serializer.data  # list of dicts
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hyperdjango.conf import parse_bool
from hyperdjango.exceptions import HTTPException

# JSON-compatible value type for serialized data
type JsonValue = (
    str | int | float | bool | None | dict[str, "JsonValue"] | list["JsonValue"]
)
type SerializedData = dict[str, JsonValue]


@dataclass
class SerializerFieldInfo:
    """Metadata for a serializer field."""

    read_only: bool = False
    write_only: bool = False
    required: bool = True
    default: Any = None
    source: str | None = None  # attribute name on source object, or method name
    min_length: int | None = None
    max_length: int | None = None
    min_value: int | float | None = None
    max_value: int | float | None = None
    choices: list | None = None
    label: str | None = None
    help_text: str | None = None

    # Set during metaclass processing
    field_name: str = ""
    field_type: type = str


def SerializerField(
    *,
    read_only: bool = False,
    write_only: bool = False,
    required: bool = True,
    default: Any = None,
    source: str | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    min_value: int | float | None = None,
    max_value: int | float | None = None,
    choices: list | None = None,
    label: str | None = None,
    help_text: str | None = None,
) -> Any:
    """Create a serializer field descriptor.

    Args:
        read_only: Field appears in output but not accepted in input.
        write_only: Field accepted in input but not in output.
        required: Field must be present in input (ignored for read_only).
        default: Default value when field is missing from input.
        source: Source attribute or method name on the object. Defaults to field name.
        min_length: Minimum string length validation.
        max_length: Maximum string length validation.
        min_value: Minimum numeric value validation.
        max_value: Maximum numeric value validation.
        choices: Allowed values list.
        label: Human-readable label (for OpenAPI).
        help_text: Description text (for OpenAPI).
    """
    return SerializerFieldInfo(
        read_only=read_only,
        write_only=write_only,
        required=required,
        default=default,
        source=source,
        min_length=min_length,
        max_length=max_length,
        min_value=min_value,
        max_value=max_value,
        choices=choices,
        label=label,
        help_text=help_text,
    )


_MISSING = object()


def _resolve_default(default: Any, context: dict) -> Any:
    """Resolve a field default, invoking callables instead of storing them raw.

    A plain (non-callable) default is returned unchanged. A callable default
    (e.g. ``list``, ``datetime.now``) is invoked to produce a fresh value, so
    the callable OBJECT never leaks into ``validated_data`` (which would then be
    written to the model → 500). A default that declares ``requires_context =
    True`` (e.g. ``CurrentUserDefault``) is passed the serializer context so it
    can read the current request/user.
    """
    if default is None or not callable(default):
        return default
    # A default may opt into context injection by declaring requires_context=True.
    # dynamic-attr: probing an arbitrary user-supplied default object for the context-injection protocol marker — its type is not known here.
    if getattr(default, "requires_context", False):
        return default(context)
    return default()


# ── Serialize plan: per-field getter closures baked at class creation ──
#
# v0.14.0 cached `_compute_method_cache` to remove the per-call MRO walk
# (+62.9% List rps). v0.14.10 takes the next step: instead of CALLING
# `_get_compute_method` and `_get_nested_serializer` per field per object
# (each ~0.5 μs), we precompute a flat list of `(output_field_name, getter)`
# tuples at metaclass time. The getter is a closure that bakes in the
# source path, default value, and optional nested serializer dispatch —
# zero per-call inspection in `_serialize_one`'s hot loop.
#
# Profile evidence: bookstore_api `/api/v1/books/` showed 1.5 million
# `_get_compute_method` + 1.5 million `_get_nested_serializer` calls per
# 15K-request profile (= 100 of each per request, 10 books × 10 fields).
# Eliminating both from the hot path drops List endpoint top-10 self-time
# by ~1080 ms and bumps wall-clock rps by ~10%.


def _wrap_nested(value, nested_cls, context):
    """Dispatch a value through a nested serializer if applicable."""
    if value is None or nested_cls is None:
        return value
    if isinstance(value, list):
        return nested_cls(obj=value, many=True, context=context).data
    return nested_cls(obj=value, context=context).data


def _make_method_getter(method, nested_cls):
    """Field source is a serializer method (or get_<source> convention)."""
    if nested_cls is None:

        def get(self, obj):
            return method(self, obj)

        return get

    def get(self, obj):
        return _wrap_nested(method(self, obj), nested_cls, self.context)

    return get


def _make_smart_getter(source, nested_cls, default):
    """Field source is a single attribute on the object (dict or instance).

    The isinstance(obj, dict) check is per-call but cheap (~30 ns) and
    necessary because the same Serializer class can serialize both dicts
    (from db.query) and Model instances (from QuerySet.all()).
    """
    if nested_cls is None:

        def get(self, obj):
            if isinstance(obj, dict):
                return obj.get(source, default)
            # dynamic-attr: obj is a user-supplied instance; source is a user-configured attribute name
            return getattr(obj, source, default)

        return get

    def get(self, obj):
        if isinstance(obj, dict):
            v = obj.get(source, default)
        else:
            # dynamic-attr: obj is a user-supplied instance; source is a user-configured attribute name
            v = getattr(obj, source, default)
        return _wrap_nested(v, nested_cls, self.context)

    return get


def _make_dotted_getter(source, nested_cls, default):
    """Field source is a dotted path like 'author.name' or 'a.b.c'."""
    parts = tuple(source.split("."))
    if nested_cls is None:

        def get(self, obj):
            current = obj
            for part in parts:
                if current is None:
                    return default
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    # dynamic-attr: current is a user-supplied instance; part is a segment of a user-configured dotted source path
                    current = getattr(current, part, None)
            return current if current is not None else default

        return get

    def get(self, obj):
        current = obj
        for part in parts:
            if current is None:
                return default
            if isinstance(current, dict):
                current = current.get(part)
            else:
                # dynamic-attr: current is a user-supplied instance; part is a segment of a user-configured dotted source path
                current = getattr(current, part, None)
        v = current if current is not None else default
        return _wrap_nested(v, nested_cls, self.context)

    return get


def _detect_nested_serializer(field_info) -> type | None:
    """Return the nested Serializer subclass for this field, or None."""
    ft = field_info.field_type
    if isinstance(ft, type) and issubclass(ft, Serializer) and ft is not Serializer:
        return ft
    return None


def build_serialize_plan(cls) -> list[tuple[str, Callable]]:
    """Build the per-class serialize plan from current ``_serializer_fields``.

    Called from both ``SerializerMeta.__new__`` (after collecting explicit
    field declarations) and ``ModelSerializerMeta.__new__`` (after
    auto-injecting model fields). Subclass metaclasses that mutate
    ``_serializer_fields`` after their parent metaclass runs MUST call
    this to refresh the plan, otherwise ``_serialize_one`` will skip
    the late-added fields.
    """
    fields = cls._serializer_fields
    compute_cache = cls._compute_method_cache
    typed_fields = getattr(
        cls, "_typed_fields", {}
    )  # dynamic-attr: metaclass-set attr, may be absent on partially-built classes
    relational_fields = getattr(
        cls, "_relational_fields", {}
    )  # dynamic-attr: metaclass-set attr, may be absent on partially-built classes
    plan: list[tuple[str, Callable]] = []
    for field_name, field_info in fields.items():
        if field_info.write_only:
            continue
        source = field_info.source or field_name
        default = field_info.default
        nested_cls = _detect_nested_serializer(field_info)

        method = compute_cache.get(source)
        if method is None:
            method = compute_cache.get(f"get_{source}")

        if method is not None:
            getter = _make_method_getter(method, nested_cls)
        elif "." in source:
            getter = _make_dotted_getter(source, nested_cls, default)
        else:
            getter = _make_smart_getter(source, nested_cls, default)

        # Typed fields transform the value on output via to_representation.
        typed_field = typed_fields.get(field_name)
        if typed_field is not None:
            getter = _make_typed_getter(getter, typed_field)

        # Relational fields with a computed read representation (SlugRelatedField
        # → slug string) wrap the getter too; PrimaryKeyRelatedField opts out
        # (its raw PK passthrough is already correct).
        related_field = relational_fields.get(field_name)
        if related_field is not None and related_field._wire_read_representation:
            getter = _make_related_repr_getter(getter, related_field)

        plan.append((field_name, getter))
    return plan


# ── Flat encoder: whole-object codegen for pure-passthrough serializers ──
#
# `_serialize_one` calls one getter closure per field per object, and each
# smart getter re-runs `isinstance(obj, dict)`. For a serializer class whose
# plan is entirely single-attribute passthrough fields (no method/nested/
# dotted sources), the shape is fully static, so we compile ONE specialized
# encoder at class-creation time. It branches on dict-vs-attribute exactly
# once per object (not once per field) and materializes the whole result dict
# in a single literal — no per-field Python call, no repeated isinstance.
#
# Output is identical to `_serialize_one`: same field order (read fields,
# write_only skipped), same dict `.get(source, default)` / `getattr(obj,
# source, default)` semantics, same `{}` for a None object. Serializers with
# any method/nested/dotted field get `None` here and keep using the plan loop.


def build_flat_encoder(cls):
    """Compile a specialized `(obj) -> dict` encoder, or None if not flat.

    "Flat" = every non-write_only field is a plain single-attribute passthrough
    (no computed/method source, no nested serializer, no dotted path). Rebuilt
    by any metaclass that mutates ``_serializer_fields`` (same contract as
    ``build_serialize_plan``).
    """
    fields = cls._serializer_fields
    compute_cache = cls._compute_method_cache
    # Typed fields transform values on output via to_representation, so the
    # whole-object flat encoder (which emits raw column values) would bypass
    # them — disqualify the serializer and let the plan loop run instead.
    if getattr(
        cls, "_typed_fields", None
    ):  # dynamic-attr: metaclass-set attr, may be absent on partially-built classes
        return None
    # A relational field with a computed read representation (SlugRelatedField)
    # also transforms values on output, so the raw-passthrough flat encoder would
    # bypass it — disqualify and let the plan loop run instead.
    relational_fields = getattr(
        cls, "_relational_fields", {}
    )  # dynamic-attr: metaclass-set attr, may be absent on partially-built classes
    for related_field in relational_fields.values():
        if related_field._wire_read_representation:
            return None
    flat: list[tuple[str, str, Any]] = []
    for field_name, field_info in fields.items():
        if field_info.write_only:
            continue
        source = field_info.source or field_name
        if _detect_nested_serializer(field_info) is not None:
            return None
        if "." in source:
            return None
        if compute_cache.get(source) is not None or (
            compute_cache.get(f"get_{source}") is not None
        ):
            return None
        flat.append((field_name, source, field_info.default))

    if not flat:
        # Nothing to specialize (no readable fields) — let _serialize_one run.
        return None

    # Codegen: one isinstance branch, then a dict literal per branch. Defaults
    # are captured by name (_d0.._dN) in the exec namespace so arbitrary
    # (non-literal) default objects are supported without repr().
    dict_items = ", ".join(
        f"{out!r}: obj.get({src!r}, _d{i})" for i, (out, src, _) in enumerate(flat)
    )
    attr_items = ", ".join(
        f"{out!r}: getattr(obj, {src!r}, _d{i})" for i, (out, src, _) in enumerate(flat)
    )
    src_lines = (
        "def _flat_encode(obj):\n"
        "    if obj is None:\n"
        "        return {}\n"
        "    if isinstance(obj, dict):\n"
        f"        return {{{dict_items}}}\n"
        f"    return {{{attr_items}}}\n"
    )
    ns: dict[str, Any] = {f"_d{i}": default for i, (_, _, default) in enumerate(flat)}
    exec(src_lines, ns)  # noqa: S102 — generated from class metadata, not user input
    # Wrap in staticmethod so `self._flat_encoder` returns the bare function
    # instead of binding `self` as its first argument.
    return staticmethod(ns["_flat_encode"])


def _make_typed_getter(base_getter, typed_field):
    """Wrap a serialize getter so a TypedField's ``to_representation`` runs.

    Typed fields (DateTimeField/UUIDField/DecimalField/...) transform the stored
    value on the way OUT (datetime → ISO string, Decimal → str, …). Without this
    wrapper the serialize plan emitted the raw column value and to_representation
    was never called.
    """

    def get(self, obj):
        return typed_field.to_representation(base_getter(self, obj))

    return get


def _make_related_repr_getter(base_getter, related_field):
    """Wrap a serialize getter so a relational field's read representation runs.

    SlugRelatedField must emit its slug on read, not the raw related object that
    the passthrough getter would produce. Its ``to_representation`` is async (for
    symmetry with ``to_internal_value``), but the read path does no awaiting, so
    the sync serialize plan calls ``represent_read`` here — analogous to
    ``_make_typed_getter`` for TypedFields.
    """

    def get(self, obj):
        return related_field.represent_read(base_getter(self, obj))

    return get


def _typed_field_to_info(typed_field, field_name, field_type):
    """Build a SerializerFieldInfo mirror for a TypedField descriptor.

    The TypedField instance itself (kept in ``cls._typed_fields``) does the real
    work via ``to_internal_value``/``to_representation``; this info object only
    carries the read_only/required/default metadata the generic missing-field
    and serialize-plan machinery needs.
    """
    return SerializerFieldInfo(
        field_name=field_name,
        field_type=field_type,
        read_only=typed_field.read_only,
        required=typed_field.required,
        default=typed_field.default,
        # HiddenField carries a server-side value only: it must never appear in
        # output (write_only) and never accept client input (handled in
        # is_valid). ``_is_hidden`` is declared on the TypedField base (default
        # False), so this is a plain attribute read on a known type.
        write_only=typed_field._is_hidden,
    )


def _related_field_to_info(field_name, field_type):
    """Build a SerializerFieldInfo mirror for a relational field descriptor.

    Existence validation runs asynchronously in ``avalidate_relations``; this
    info keeps the field visible to ``is_valid()`` (writable + coerced) and to
    the serialize plan (reads the raw PK, which is the correct representation).
    """
    return SerializerFieldInfo(
        field_name=field_name,
        field_type=field_type,
        read_only=False,
        required=True,
    )


_PRIMITIVE_ANNOTATIONS = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "bytes": bytes,
}


def _resolve_annotation_type(ann: object, owner: type) -> object:
    """Resolve a possibly-stringized annotation (PEP 563 / lazy) to a real type.

    Under ``from __future__ import annotations`` a field annotation arrives as a
    string ('int'); returning it verbatim would defeat identity-based type
    dispatch in ``_coerce``. Resolves primitives directly and other names via the
    owner's module globals; leaves genuinely-unresolvable forward refs as-is.
    """
    if not isinstance(ann, str):
        return ann
    prim = _PRIMITIVE_ANNOTATIONS.get(ann)
    if prim is not None:
        return prim
    import sys as _sys

    module = _sys.modules.get(owner.__module__)
    # dynamic-attr: resolving a PEP-563 annotation NAME (runtime string) against the owner module's namespace
    resolved = getattr(module, ann, None) if module is not None else None
    return resolved if isinstance(resolved, type) else ann


class SerializerMeta(type):
    """Metaclass that collects field definitions from annotations."""

    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        if name == "Serializer":
            cls._serializer_fields = {}
            cls._compute_method_cache = {}
            cls._flat_encoder = None
            cls._typed_fields = {}
            cls._relational_fields = {}
            return cls

        # Collect fields from base classes
        fields = {}
        typed_fields: dict[str, Any] = {}
        relational_fields: dict[str, Any] = {}
        for base in reversed(bases):
            if hasattr(base, "_serializer_fields"):
                fields.update(base._serializer_fields)
            if hasattr(base, "_typed_fields"):
                typed_fields.update(base._typed_fields)
            if hasattr(base, "_relational_fields"):
                relational_fields.update(base._relational_fields)

        # Python 3.14+ uses lazy annotations (PEP 749) — access via property
        # cls.__annotations__ includes inherited, so filter to only own fields
        all_annotations = cls.__annotations__
        parent_annotations = set()
        for base in bases:
            if base.__annotations__:
                parent_annotations.update(base.__annotations__)

        for field_name, field_type in all_annotations.items():
            if field_name.startswith("_"):
                continue
            # Skip fields already collected from parent (unless overridden in this class)
            if field_name in parent_annotations and field_name not in cls.__dict__:
                continue

            field_info = cls.__dict__.get(field_name)
            if isinstance(field_info, SerializerFieldInfo):
                field_info.field_name = field_name
                # The annotation is the declared field type; resolve it first:
                # under `from __future__ import annotations` (PEP 563) it arrives
                # as a STRING ('int'), which would defeat `_coerce`'s
                # `field_type is int` identity checks and silently disable type
                # coercion. A non-string annotation passes through unchanged.
                field_info.field_type = _resolve_annotation_type(field_type, cls)
                fields[field_name] = field_info
            elif isinstance(field_info, type) and issubclass(field_info, Serializer):
                # Nested serializer declared as type annotation with class as default
                # e.g., author: UserSerializer = SerializerField(read_only=True)
                # Already handled above
                pass
            # TypedField descriptors (EmailField/DateTimeField/ChoiceField/...)
            # live in rest.py and can't be isinstance-checked here without a
            # dynamic-attr: circular import — probe the marker on this arbitrary user-namespace value.
            elif getattr(field_info, "_is_typed_field", False):
                field_info._field_name = field_name
                typed_fields[field_name] = field_info
                fields[field_name] = _typed_field_to_info(
                    field_info, field_name, field_type
                )
            # Relational descriptors (PrimaryKeyRelatedField/SlugRelatedField) also
            # dynamic-attr: live in rest.py — same marker-probe rationale as the typed-field branch.
            elif getattr(field_info, "_is_related_field", False):
                relational_fields[field_name] = field_info
                fields[field_name] = _related_field_to_info(field_name, field_type)
            else:
                # Plain annotation without SerializerField — create default
                # (resolve PEP 563 string annotations to real types).
                info = SerializerFieldInfo(
                    field_name=field_name,
                    field_type=_resolve_annotation_type(field_type, cls),
                )
                fields[field_name] = info

        cls._serializer_fields = fields
        cls._typed_fields = typed_fields
        cls._relational_fields = relational_fields

        # Pre-compute compute-method lookup table.
        # _get_compute_method() is called once per field per serialize — ~N*M
        # times per list endpoint response. Caching this at class creation
        # eliminates ~14% of per-request time (profile_production_report.md).
        # The cache maps BOTH the exact method name and the get_<name> convention
        # to the bound method, so lookup is a single dict hit.
        compute_cache: dict[str, object] = {}
        for candidate_name in dir(cls):
            if candidate_name.startswith("_"):
                continue
            attr = cls.__dict__.get(candidate_name)
            if attr is None:
                # Walk MRO for inherited methods
                for base in cls.__mro__[1:]:
                    attr = base.__dict__.get(candidate_name)
                    if attr is not None:
                        break
            if attr is not None and callable(attr):
                compute_cache[candidate_name] = attr
        cls._compute_method_cache = compute_cache

        # Pre-compute the serialize plan: a flat list of (field_name, getter)
        # tuples used by _serialize_one. Each getter is a closure that bakes
        # in the source path, default value, and any nested serializer
        # dispatch — eliminating per-field _get_compute_method and
        # _get_nested_serializer calls from the hot path. Subclass
        # metaclasses (e.g. ModelSerializerMeta) that add fields LATER
        # must call build_serialize_plan() again to refresh.
        cls._serialize_plan = build_serialize_plan(cls)
        cls._flat_encoder = build_flat_encoder(cls)
        return cls


class Serializer(metaclass=SerializerMeta):
    """Base serializer for API request/response shaping.

    Supports:
    - Serialization (obj → dict): excludes write_only fields, resolves computed fields
    - Deserialization (input_data → validated_data): excludes read_only, validates constraints
    - Nested serializers: field type is another Serializer subclass
    - Many mode: serialize/deserialize lists of objects
    """

    _serializer_fields: dict[str, SerializerFieldInfo]

    def __init__(
        self,
        obj: Any = None,
        input_data: SerializedData | None = None,
        many: bool = False,
        partial: bool = False,
        context: dict | None = None,
    ):
        self._obj = obj
        self._input_data = input_data
        self._many = many
        self._partial = partial
        self.context = context or {}
        self._validated_data: SerializedData | None = None
        self._errors: dict[str, str] | None = None
        self._data: Any = None

    # ── Serialization (obj → dict) ───────────────────────────────────

    @property
    def data(self) -> Any:
        """Serialize the object(s) to a dict or list of dicts.

        When the class is a pure single-attribute passthrough, a specialized
        encoder compiled at class-creation time (``_flat_encoder``) replaces
        the per-field getter loop — one dict/attr branch per object instead of
        one isinstance per field. Output is identical to ``_serialize_one``.
        """
        if self._data is not None:
            return self._data

        encode = self._flat_encoder or self._serialize_one
        if self._many:
            self._data = [encode(item) for item in (self._obj or [])]
        else:
            self._data = encode(self._obj)
        return self._data

    def _serialize_one(self, obj) -> SerializedData:
        """Serialize a single object via the precomputed serialize plan.

        Each entry in `_serialize_plan` is `(output_field_name, getter)`
        where `getter` is a closure baked at class creation. The closure
        already knows the source path, default value, and any nested
        serializer dispatch — no per-field inspection needed in the loop.
        """
        if obj is None:
            return {}
        result = {}
        for field_name, getter in self._serialize_plan:
            result[field_name] = getter(self, obj)
        return result

    def _resolve_dotted(self, obj: Any, source: str, default: Any = None) -> Any:
        """Resolve a dotted source path like 'author.name' on an object."""
        current = obj
        for part in source.split("."):
            if current is None:
                return default
            if isinstance(current, dict):
                current = current.get(part)
            else:
                # dynamic-attr: current is a user-supplied instance; part is a segment of a user-configured dotted source string
                current = getattr(current, part, None)
        return current if current is not None else default

    def _get_attr(self, obj, name):
        """Get attribute from object (dict or object)."""
        if isinstance(obj, dict):
            return obj.get(name)
        # dynamic-attr: obj is a user-provided instance of unknown type; name is a runtime attribute name
        return getattr(obj, name, None)

    def _get_compute_method(self, source: str):
        """Check if source is a method on this serializer.

        Also checks `get_{source}` convention for SerializerMethodField:
            field_name: str = SerializerMethodField()
            def get_field_name(self, obj): ...

        Uses a class-level cache (built in SerializerMeta.__new__) so this
        becomes a single dict lookup per field instead of a full MRO walk.
        Measured impact: 87ms → ~5ms per 500-request profile
        (see logs/profile_production_report.md).
        """
        cache = type(self)._compute_method_cache
        method = cache.get(source)
        if method is None:
            method = cache.get(f"get_{source}")
        if method is None:
            return None
        return lambda obj, m=method: m(self, obj)

    def _get_nested_serializer(self, field_name: str):
        """Check if field type is a Serializer subclass."""
        field_info = self._serializer_fields[field_name]
        ft = field_info.field_type
        if isinstance(ft, type) and issubclass(ft, Serializer) and ft is not Serializer:
            return ft
        return None

    # ── Deserialization (input_data → validated_data) ─────────────────

    def is_valid(self, raise_exception: bool = False) -> bool:
        """Validate input data. Returns True if valid, False if errors.

        Args:
            raise_exception: If True, raises ValueError on validation failure
                instead of returning False.
        """
        if self._many:
            return self._validate_many(raise_exception)

        self._errors = {}
        self._validated_data = {}

        if self._input_data is None:
            self._errors["__all__"] = "No input data provided"
            if raise_exception:
                raise ValueError("No input data provided")
            return False

        for field_name, field_info in self._serializer_fields.items():
            if field_info.read_only:
                continue  # Don't accept read-only in input

            # HiddenField carries a server-side value ONLY. It must ignore client
            # input entirely (so a POSTed {"author_id": <other user>} can never
            # override it — mass-assignment/IDOR) and always stamp its resolved
            # default. A callable default (e.g. CurrentUserDefault) is INVOKED
            # with the context so the server-side value (current user id) is
            # produced, rather than the callable object landing in validated_data.
            typed_field = self._typed_fields.get(field_name)
            if typed_field is not None and typed_field._is_hidden:
                self._validated_data[field_name] = _resolve_default(
                    field_info.default, self.context
                )
                continue

            value = self._input_data.get(field_name, _MISSING)

            # Missing value handling
            if value is _MISSING:
                if self._partial:
                    continue
                if not field_info.required or field_info.default is not None:
                    if field_info.default is not None:
                        # Invoke callable defaults (e.g. list, datetime.now,
                        # context-aware defaults) instead of storing the object.
                        self._validated_data[field_name] = _resolve_default(
                            field_info.default, self.context
                        )
                    continue
                self._errors[field_name] = "This field is required"
                continue

            # Nested serializer validation. A nested field's declared type is a
            # Serializer subclass; its input MUST be an object (dict) or, for a
            # collection, a list of objects. Anything else (scalar, or a bare
            # list where an object is expected) is rejected as a clean field
            # error (→ 400) so unvalidated junk never flows into
            # create()/update().
            nested_cls = self._get_nested_serializer(field_name)
            if nested_cls is not None:
                if isinstance(value, dict):
                    nested = nested_cls(
                        input_data=value,
                        partial=self._partial,
                        context=self.context,
                    )
                elif isinstance(value, list):
                    nested = nested_cls(
                        input_data=value,
                        many=True,
                        partial=self._partial,
                        context=self.context,
                    )
                else:
                    self._errors[field_name] = "Expected an object"
                    continue
                if nested.is_valid():
                    self._validated_data[field_name] = nested.validated_data
                else:
                    self._errors[field_name] = nested.errors
                continue

            # Typed serializer field (EmailField/DateTimeField/ChoiceField/
            # UUIDField/DecimalField/URLField/IPAddressField/...). Dispatch to its
            # to_internal_value so it ACTUALLY validates/coerces the input. Before
            # this the TypedField was inert — the metaclass replaced it with a
            # bare SerializerFieldInfo and to_internal_value was never called, so
            # EmailField accepted "not-an-email", ChoiceField accepted anything,
            # DateTimeField left the raw string, etc. Relational fields are NOT
            # handled here (their to_internal_value is async) — see
            # avalidate_relations().
            typed_field = self._typed_fields.get(field_name)
            if typed_field is not None:
                try:
                    self._validated_data[field_name] = typed_field.to_internal_value(
                        value
                    )
                except (ValueError, TypeError, HTTPException) as exc:
                    self._errors[field_name] = str(exc)
                continue

            # Type coercion
            value = self._coerce(field_name, field_info, value)
            if field_name in self._errors:
                continue

            # Validate constraints
            self._validate_field(field_name, field_info, value)
            if field_name not in self._errors:
                self._validated_data[field_name] = value

        # Custom validation
        if not self._errors:
            try:
                self.validate(self._validated_data)
            except ValueError as e:
                self._errors["__all__"] = str(e)

        valid = len(self._errors) == 0
        if not valid and raise_exception:
            raise ValueError(str(self._errors))
        return valid

    def _validate_many(self, raise_exception: bool = False) -> bool:
        """Validate a list of input items (many=True deserialization)."""
        self._errors = {}
        self._validated_data = {}

        if self._input_data is None:
            self._errors["__all__"] = "No input data provided"
            if raise_exception:
                raise ValueError("No input data provided")
            return False

        if not isinstance(self._input_data, list):
            self._errors["__all__"] = "Expected a list of items"
            if raise_exception:
                raise ValueError("Expected a list of items")
            return False

        many_errors: dict[str, Any] = {}
        many_validated: list[SerializedData] = []
        has_errors = False

        for i, item in enumerate(self._input_data):
            child = type(self)(
                input_data=item, partial=self._partial, context=self.context
            )
            child._many = False  # validate as single item
            if child.is_valid():
                many_validated.append(child.validated_data)
            else:
                many_errors[str(i)] = child.errors
                has_errors = True

        if has_errors:
            self._errors = many_errors
            if raise_exception:
                raise ValueError(str(many_errors))
            return False

        self._validated_data = many_validated
        return True

    def validate(self, data: SerializedData) -> SerializedData:
        """Override for cross-field validation. Raise ValueError on invalid."""
        return data

    async def avalidate_relations(self) -> bool:
        """Async second phase: validate relational (FK) fields against the DB.

        ``is_valid()`` is synchronous, but relational fields
        (``PrimaryKeyRelatedField``/``SlugRelatedField``) must query the database
        to confirm the referenced row exists — an async operation. The REST
        create/update mixins call this AFTER ``is_valid()`` returns True: it runs
        each relational field's async ``to_internal_value`` over the already-
        coerced value in ``validated_data``, replacing it with the validated
        result (e.g. a slug resolved to a PK) or recording a field error.

        Returns True when no relational error was recorded. A serializer with no
        relational fields returns immediately (the common case), so calling this
        unconditionally after ``is_valid()`` is cheap and safe.
        """
        relational = type(self)._relational_fields
        if not relational or not self._validated_data:
            return not self._errors
        if self._errors is None:
            self._errors = {}
        # Single-object validated_data is a dict; many= yields a list of dicts.
        items = self._validated_data if self._many else [self._validated_data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for field_name, field in relational.items():
                if field_name not in item:
                    continue
                try:
                    item[field_name] = await field.to_internal_value(item[field_name])
                except (ValueError, TypeError, HTTPException) as exc:
                    # A genuine "does not exist"/bad-reference is a field error.
                    # (Unexpected DB/ORM failures are NOT caught here — the field
                    # re-raises them so they surface as a 500, not a silent 400.)
                    self._errors[field_name] = str(exc)
        return not self._errors

    async def save(self, **kwargs: Any) -> Any:
        """Create or update an instance from validated data.

        If self._obj (instance) was passed to the serializer, calls update().
        Otherwise calls create(). Extra kwargs are merged into validated_data.

        Usage:
            serializer = UserSerializer(input_data=data)
            if serializer.is_valid():
                user = await serializer.save(owner_id=request.user["id"])
        """
        if self._validated_data is None:
            raise RuntimeError("Call is_valid() before save()")

        data = dict(self._validated_data)
        data.update(kwargs)

        if self._obj is not None:
            return await self.update(self._obj, data)
        return await self.create(data)

    async def create(self, validated_data: SerializedData) -> Any:
        """Override in subclass to create a new instance."""
        raise NotImplementedError("Subclass must implement create()")

    async def update(self, instance: Any, validated_data: SerializedData) -> Any:
        """Override in subclass to update an existing instance."""
        raise NotImplementedError("Subclass must implement update()")

    @property
    def validated_data(self) -> SerializedData:
        """Get validated data (call is_valid() first)."""
        if self._validated_data is None:
            raise RuntimeError("Call is_valid() before accessing validated_data")
        return self._validated_data

    @property
    def errors(self) -> dict[str, str]:
        """Get validation errors (call is_valid() first)."""
        if self._errors is None:
            raise RuntimeError("Call is_valid() before accessing errors")
        return self._errors

    def _coerce(
        self, field_name: str, field_info: SerializerFieldInfo, value: Any
    ) -> Any:
        """Coerce value to the expected type."""
        ft = field_info.field_type
        if ft is int:
            # bool is an int subclass, so isinstance(True, int) is True — but a
            # bool is NOT a valid int-field value. The native dhi validation path
            # rejects bool-for-int; reject here too rather than storing True as 1.
            if isinstance(value, bool):
                self._errors[field_name] = (
                    f"Expected integer, got {type(value).__name__}"
                )
                return value
            if not isinstance(value, int):
                try:
                    return int(value)
                except ValueError, TypeError:
                    self._errors[field_name] = (
                        f"Expected integer, got {type(value).__name__}"
                    )
                    return value
        if ft is float and not isinstance(value, (int, float)):
            try:
                return float(value)
            except ValueError, TypeError:
                self._errors[field_name] = (
                    f"Expected number, got {type(value).__name__}"
                )
                return value
        if ft is bool and not isinstance(value, bool):
            return parse_bool(value)
        if ft is str and not isinstance(value, str):
            # Do NOT blindly str()-coerce: None → "None", a list/dict → its repr,
            # a bool → "True"/"False" — all silently corrupt the stored value.
            # Reject non-string structured/None/bool input as a field error;
            # numeric scalars (int/float) still coerce to their string form.
            if value is None or isinstance(value, (list, dict, bool)):
                self._errors[field_name] = (
                    f"Expected a string, got {type(value).__name__}"
                )
                return value
            return str(value)
        return value

    def _validate_field(
        self, field_name: str, field_info: SerializerFieldInfo, value: Any
    ):
        """Validate a single field value against constraints."""
        if (
            field_info.min_length is not None
            and isinstance(value, str)
            and len(value) < field_info.min_length
        ):
            self._errors[field_name] = f"Minimum length is {field_info.min_length}"
            return
        if (
            field_info.max_length is not None
            and isinstance(value, str)
            and len(value) > field_info.max_length
        ):
            self._errors[field_name] = f"Maximum length is {field_info.max_length}"
            return
        if (
            field_info.min_value is not None
            and isinstance(value, (int, float))
            and value < field_info.min_value
        ):
            self._errors[field_name] = f"Minimum value is {field_info.min_value}"
            return
        if (
            field_info.max_value is not None
            and isinstance(value, (int, float))
            and value > field_info.max_value
        ):
            self._errors[field_name] = f"Maximum value is {field_info.max_value}"
            return
        if field_info.choices is not None and value not in field_info.choices:
            self._errors[field_name] = f"Must be one of: {field_info.choices}"
            return


# ── PublicIDSerializer ──────────────────────────────────────────────────────


class PublicIDSerializer(Serializer):
    """Base serializer that exposes public_id as 'id' and hides the integer PK.

    Subclass this instead of Serializer for models using PublicIDMixin.
    The integer primary key is never included in serialized output.

    Usage:
        class ArticleSerializer(PublicIDSerializer):
            title: str = SerializerField()
            content: str = SerializerField()

        # Output: {"id": "Xf7RgW3pMc", "title": "Hello", "content": "..."}
        # Integer PK is NOT included

        # Input: {"title": "Hello", "content": "..."}
        # "id" is read-only, not accepted in input
    """

    id: str = SerializerField(source="public_id", read_only=True)
