"""
HyperSerializer — DRF-style serializer backed by dhi.

Provides fast model serialization/deserialization without requiring
Django REST Framework. Uses dhi's SIMD validation for 523x faster
input validation.

Usage:
    from hyperdjango.validation.serializers import HyperSerializer

    class UserSerializer(HyperSerializer):
        class Meta:
            model = User
            fields = ['name', 'email', 'age']

    serializer = UserSerializer(data=request_data)
    if serializer.is_valid():
        user = serializer.save()
"""

from typing import Annotated, Any, Optional

from django.core.exceptions import ValidationError as DjangoValidationError

from hyperdjango.validation.converters import django_model_field_to_dhi
from hyperdjango.validation.core import BaseModel as _BaseModel
from hyperdjango.validation.core import Field as DhiField
from hyperdjango.validation.core.validator import (
    ValidationError as DhiValidationError,
)
from hyperdjango.validation.core.validator import (
    ValidationErrors as DhiValidationErrors,
)


class HyperSerializerMeta(type):
    """Metaclass that builds a dhi model from the Django model's fields."""

    def __new__(mcs, name, bases, attrs):
        new_class = super().__new__(mcs, name, bases, attrs)

        if name == "HyperSerializer":
            return new_class

        # dynamic-attr: Meta is an optional user-declared inner class, resolved via the MRO when not in this class's attrs
        meta = attrs.get("Meta") or getattr(new_class, "Meta", None)
        if meta is None:
            return new_class

        # model/fields/exclude/read_only_fields/extra_kwargs are optional
        # attributes on the user-authored Meta class.
        model = getattr(meta, "model", None)  # dynamic-attr: optional on user Meta
        fields_list = getattr(
            meta, "fields", None
        )  # dynamic-attr: optional on user Meta
        exclude_list = getattr(
            meta, "exclude", []
        )  # dynamic-attr: optional on user Meta
        read_only_fields = getattr(
            meta, "read_only_fields", []
        )  # dynamic-attr: optional on user Meta
        extra_kwargs = getattr(
            meta, "extra_kwargs", {}
        )  # dynamic-attr: optional on user Meta

        if model is None:
            return new_class

        # Determine which fields to include
        model_fields = {
            f.name: f for f in model._meta.get_fields() if hasattr(f, "name")
        }

        if fields_list == "__all__":
            field_names = [
                f.name for f in model._meta.get_fields() if hasattr(f, "name")
            ]
        elif fields_list:
            field_names = list(fields_list)
        else:
            field_names = [
                f.name
                for f in model._meta.get_fields()
                if hasattr(f, "name") and f.name not in exclude_list
            ]

        # Build dhi model annotations
        annotations = {}
        defaults = {}

        for field_name in field_names:
            if field_name not in model_fields:
                continue

            mf = model_fields[field_name]
            base_type, field_kwargs = django_model_field_to_dhi(mf)

            # Apply extra_kwargs overrides
            if field_name in extra_kwargs:
                field_kwargs.update(extra_kwargs[field_name])

            # Handle nullable/blank fields
            # dynamic-attr: mf is a Django model field; null/blank exist on concrete fields but not on all reverse relations
            if getattr(mf, "null", False) or getattr(mf, "blank", False):
                if base_type is not Optional:
                    base_type = base_type | None
                defaults[field_name] = None

            # Handle defaults
            if hasattr(mf, "default") and mf.default is not None:
                from django.db.models.fields import NOT_PROVIDED

                if mf.default is not NOT_PROVIDED:
                    if callable(mf.default):
                        defaults[field_name] = None  # Can't use callable in dhi
                    else:
                        defaults[field_name] = mf.default

            # Build annotation
            if field_kwargs:
                annotations[field_name] = Annotated[base_type, DhiField(**field_kwargs)]
            else:
                annotations[field_name] = base_type

        # Create the dhi model
        namespace = {"__annotations__": annotations, **defaults}
        new_class._dhi_model = type(f"{name}DhiModel", (_BaseModel,), namespace)
        new_class._model = model
        new_class._field_names = field_names
        new_class._read_only_fields = set(read_only_fields)

        return new_class


class HyperSerializer(metaclass=HyperSerializerMeta):
    """DRF-style serializer backed by dhi for high-performance validation.

    Supports:
    - model_validate via dhi (523x faster than Pydantic)
    - .is_valid() / .errors / .validated_data API
    - .save() creates/updates Django model instances
    - .data for serialized output
    """

    _dhi_model = None
    _model = None
    _field_names = []
    _read_only_fields = set()

    def __init__(self, instance=None, data=None, partial=False, many=False):
        self.instance = instance
        self.initial_data = data
        self.partial = partial
        self.many = many
        self._validated_data = None
        self._errors = None

    def is_valid(self, raise_exception=False) -> bool:
        """Validate the input data using dhi.

        Returns True if valid, False otherwise.
        """
        if self.initial_data is None:
            self._errors = {"non_field_errors": ["No data provided."]}
            if raise_exception:
                raise DjangoValidationError(self._errors)
            return False

        if self.many:
            return self._validate_many(raise_exception)

        return self._validate_single(self.initial_data, raise_exception)

    def _validate_single(self, data, raise_exception=False) -> bool:
        try:
            validated = self._dhi_model.model_validate(data)
            self._validated_data = validated.model_dump()
            self._errors = {}
            return True
        except (DhiValidationError, DhiValidationErrors) as e:
            self._errors = self._convert_errors(e)
            if raise_exception:
                raise DjangoValidationError(self._errors)
            return False

    def _validate_many(self, raise_exception=False) -> bool:
        results = []
        all_errors = []
        all_valid = True

        for i, item in enumerate(self.initial_data):
            try:
                validated = self._dhi_model.model_validate(item)
                results.append(validated.model_dump())
                all_errors.append({})
            except (DhiValidationError, DhiValidationErrors) as e:
                all_valid = False
                all_errors.append(self._convert_errors(e))
                results.append(None)

        if all_valid:
            self._validated_data = results
            self._errors = {}
        else:
            self._errors = all_errors
            if raise_exception:
                raise DjangoValidationError(self._errors)

        return all_valid

    @property
    def validated_data(self) -> dict[str, Any]:
        if self._validated_data is None:
            raise AssertionError("Call .is_valid() before accessing .validated_data")
        return self._validated_data

    @property
    def errors(self) -> dict[str, list[str]]:
        if self._errors is None:
            self.is_valid()
        return self._errors

    @property
    def data(self) -> dict[str, Any]:
        """Serialize the instance (or validated_data) to a dict."""
        if self.instance is not None:
            return self._serialize_instance(self.instance)
        if self._validated_data is not None:
            return self._validated_data
        return {}

    def save(self, **kwargs):
        """Create or update a Django model instance."""
        if self._validated_data is None:
            raise AssertionError("Call .is_valid() before .save()")

        # Filter out read-only fields
        writable_data = {
            k: v
            for k, v in self._validated_data.items()
            if k not in self._read_only_fields
        }
        writable_data.update(kwargs)

        if self.instance is not None:
            # Update existing instance
            for key, value in writable_data.items():
                # dynamic-attr: key is a validated field name assigned onto the Django model instance
                setattr(self.instance, key, value)
            self.instance.save()
            return self.instance
        else:
            # Create new instance
            return self._model.objects.create(**writable_data)

    def _serialize_instance(self, instance) -> dict[str, Any]:
        """Serialize a Django model instance to a dict."""
        data = {}
        for field_name in self._field_names:
            # dynamic-attr: instance is a Django model; field_name is a runtime field name read off it
            value = getattr(instance, field_name, None)
            # Handle common non-serializable types
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            elif hasattr(value, "pk"):
                value = value.pk
            data[field_name] = value
        return data

    def _convert_errors(self, exc) -> dict[str, list[str]]:
        """Convert dhi errors to a dict keyed by field name."""
        errors = {}
        # dhi ValidationErrors has an `errors` list attribute (not callable)
        # Each error has `.field` and `.message` attributes
        # dynamic-attr: exc is an arbitrary caught exception; only dhi ValidationErrors carries an `errors` list
        error_list = getattr(exc, "errors", None)
        if error_list and isinstance(error_list, list):
            for error in error_list:
                # dynamic-attr: error is a dhi error object (external type); field/message may be absent
                field = getattr(error, "field", "non_field_errors")
                msg = getattr(
                    error, "message", str(error)
                )  # dynamic-attr: dhi error object, external type
                errors.setdefault(field or "non_field_errors", []).append(msg)
        else:
            errors["non_field_errors"] = [str(exc)]
        return errors
