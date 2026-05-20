"""
Standalone Form and ModelForm for HyperApp.

No Django dependency. Provides field declaration, validation, and HTML rendering.

Usage:
    from hyperdjango.forms import Form, ModelForm, CharField, IntegerField

    # Standalone form
    class ContactForm(Form):
        name = CharField(max_length=100, required=True)
        email = CharField(max_length=200, required=True)
        message = CharField(widget="textarea", required=True)
        age = IntegerField(min_value=0, max_value=150, required=False)

    form = ContactForm(data={"name": "Alice", "email": "alice@example.com", "message": "Hello"})
    if form.is_valid():
        print(form.cleaned_data)  # {"name": "Alice", ...}
    else:
        print(form.errors)  # {"email": ["This field is required"]}

    # ModelForm — auto-generated from Model
    class UserForm(ModelForm):
        class Meta:
            model = User
            fields = ["username", "email", "age"]
            # exclude = ["password_hash"]  # alternative

    form = UserForm(data=request_data)
    if form.is_valid():
        user = await form.save()  # INSERT or UPDATE

    # Rendering
    html = form.as_div()   # Each field in a <div>
    html = form.as_table() # Each field in a <tr>
"""

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from hyperdjango.conf import parse_bool
from hyperdjango.native import fast_json_dumps
from hyperdjango.validation.core.fields import _MISSING, FieldInfo

_BASE_FORM_NAMES = frozenset({"Form", "ModelForm"})

# ---------------------------------------------------------------------------
# Form fields
# ---------------------------------------------------------------------------


class FormField:
    """Base form field with validation and rendering."""

    choices: list[tuple[str, str]] = []

    def __init__(
        self,
        *,
        required: bool = True,
        label: str | None = None,
        help_text: str = "",
        initial: Any = None,
        widget: str | None = None,
        attrs: dict | None = None,
        error_messages: dict | None = None,
    ):
        self.required = required
        self.label = label  # Auto-set from field name if None
        self.help_text = help_text
        self.initial = initial
        self.widget = widget or self._default_widget()
        self.attrs = attrs or {}
        self.error_messages = error_messages or {}
        self.name = ""  # Set by Form metaclass

    def _default_widget(self) -> str:
        return "text"

    def clean(self, value: Any) -> Any:
        """Validate and coerce a single field value.

        Returns the cleaned value or raises ValueError.
        """
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if self.required:
                raise ValueError(
                    self.error_messages.get("required", "This field is required")
                )
            return self.initial
        return value

    def render(self, value: Any = None, errors: list[str] | None = None) -> str:
        """Render this field as HTML."""
        val = value if value is not None else (self.initial or "")
        attrs_str = self._render_attrs()
        error_html = ""
        if errors:
            error_html = "".join(
                f'<span class="field-error">{_escape(str(e))}</span>' for e in errors
            )

        if self.widget == "textarea":
            html = f'<textarea name="{self.name}"{attrs_str}>{_escape(str(val))}</textarea>'
        elif self.widget == "checkbox":
            checked = " checked" if val else ""
            html = f'<input type="checkbox" name="{self.name}" value="1"{checked}{attrs_str}>'
        elif self.widget == "select":
            html = self._render_select(val, attrs_str)
        elif self.widget == "hidden":
            html = f'<input type="hidden" name="{self.name}" value="{_escape(str(val))}"{attrs_str}>'
        else:
            html = f'<input type="{self.widget}" name="{self.name}" value="{_escape(str(val))}"{attrs_str}>'

        return html + error_html

    def _render_attrs(self) -> str:
        parts = []
        for k, v in self.attrs.items():
            parts.append(f' {k}="{_escape(str(v))}"')
        return "".join(parts)

    def _render_select(self, value, attrs_str) -> str:
        choices = self.choices
        options = []
        for choice_val, choice_label in choices:
            selected = " selected" if str(choice_val) == str(value) else ""
            options.append(
                f'<option value="{_escape(str(choice_val))}"{selected}>{_escape(str(choice_label))}</option>'
            )
        return f'<select name="{self.name}"{attrs_str}>{"".join(options)}</select>'


class CharField(FormField):
    """String field."""

    def __init__(
        self, *, max_length: int | None = None, min_length: int | None = None, **kwargs
    ):
        self.max_length = max_length
        self.min_length = min_length
        super().__init__(**kwargs)

    def clean(self, value: Any) -> str | None:
        value = super().clean(value)
        if value is None:
            return value
        value = str(value).strip()
        if self.min_length and len(value) < self.min_length:
            raise ValueError(f"Must be at least {self.min_length} characters")
        if self.max_length and len(value) > self.max_length:
            raise ValueError(f"Must be at most {self.max_length} characters")
        return value


class IntegerField(FormField):
    """Integer field."""

    def __init__(
        self, *, min_value: int | None = None, max_value: int | None = None, **kwargs
    ):
        self.min_value = min_value
        self.max_value = max_value
        super().__init__(**kwargs)

    def _default_widget(self) -> str:
        return "number"

    def clean(self, value: Any) -> int | None:
        value = super().clean(value)
        if value is None:
            return value
        try:
            # Reject non-integer floats/Decimals instead of truncating them:
            # int(25.9) == 25 would silently accept a fractional value.
            if isinstance(value, (float, Decimal)):
                if value != int(value):
                    raise ValueError("Enter a valid integer")
                val = int(value)
            else:
                val = int(value)
        # OverflowError: int(float('inf')) / int(Decimal('Infinity')) — a crafted
        # JSON number like 1e309 parses to inf, so without this it would escape as
        # an uncaught 500 instead of a clean validation error.
        except ValueError, TypeError, InvalidOperation, OverflowError:
            raise ValueError("Enter a valid integer")
        if self.min_value is not None and val < self.min_value:
            raise ValueError(f"Must be at least {self.min_value}")
        if self.max_value is not None and val > self.max_value:
            raise ValueError(f"Must be at most {self.max_value}")
        return val


class FloatField(FormField):
    """Float field."""

    def __init__(
        self,
        *,
        min_value: float | None = None,
        max_value: float | None = None,
        **kwargs,
    ):
        self.min_value = min_value
        self.max_value = max_value
        super().__init__(**kwargs)

    def _default_widget(self) -> str:
        return "number"

    def clean(self, value: Any) -> float | None:
        value = super().clean(value)
        if value is None:
            return value
        try:
            val = float(value)
        except ValueError, TypeError:
            raise ValueError("Enter a valid number")
        # Reject NaN / Infinity: they pass float() but corrupt comparisons,
        # aggregates and JSON round-trips downstream.
        if not math.isfinite(val):
            raise ValueError("Enter a finite number")
        if self.min_value is not None and val < self.min_value:
            raise ValueError(f"Must be at least {self.min_value}")
        if self.max_value is not None and val > self.max_value:
            raise ValueError(f"Must be at most {self.max_value}")
        return val


class DecimalField(FormField):
    """Decimal field."""

    def __init__(
        self,
        *,
        max_digits: int | None = None,
        decimal_places: int | None = None,
        **kwargs,
    ):
        self.max_digits = max_digits
        self.decimal_places = decimal_places
        super().__init__(**kwargs)

    def _default_widget(self) -> str:
        return "number"

    def clean(self, value: Any) -> Decimal | None:
        value = super().clean(value)
        if value is None:
            return value
        try:
            val = Decimal(str(value))
        except InvalidOperation, ValueError, TypeError:
            raise ValueError("Enter a valid decimal number")
        # Reject NaN / Infinity (Decimal accepts them as valid literals).
        if not val.is_finite():
            raise ValueError("Enter a finite number")
        # Enforce precision (total-digit + decimal-place counting).
        _sign, digittuple, exponent = val.as_tuple()
        if exponent >= 0:
            digits = len(digittuple) + exponent
            decimals = 0
        elif abs(exponent) > len(digittuple):
            # e.g. 0.001 → all significant digits are decimal places
            digits = decimals = abs(exponent)
        else:
            digits = len(digittuple)
            decimals = abs(exponent)
        whole_digits = digits - decimals
        if self.max_digits is not None and digits > self.max_digits:
            raise ValueError(
                f"Ensure that there are no more than {self.max_digits} digits in total"
            )
        if self.decimal_places is not None and decimals > self.decimal_places:
            raise ValueError(
                f"Ensure that there are no more than {self.decimal_places} decimal places"
            )
        if (
            self.max_digits is not None
            and self.decimal_places is not None
            and whole_digits > (self.max_digits - self.decimal_places)
        ):
            raise ValueError(
                "Ensure that there are no more than "
                f"{self.max_digits - self.decimal_places} digits before the decimal point"
            )
        return val


class BooleanField(FormField):
    """Boolean field (checkbox)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def _default_widget(self) -> str:
        return "checkbox"

    def clean(self, value: Any) -> bool:
        result = parse_bool(value)
        # A required BooleanField must be checked (truthy) — mirror Django, which
        # errors when a required checkbox comes back False rather than silently
        # accepting the unchecked value.
        if self.required and not result:
            raise ValueError(
                self.error_messages.get("required", "This field is required")
            )
        return result


class DateField(FormField):
    """Date field."""

    def _default_widget(self) -> str:
        return "date"

    def clean(self, value: Any) -> date | None:
        value = super().clean(value)
        if value is None:
            return value
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ValueError("Enter a valid date (YYYY-MM-DD)")


class DateTimeField(FormField):
    """DateTime field."""

    def _default_widget(self) -> str:
        return "datetime-local"

    def clean(self, value: Any) -> datetime | None:
        value = super().clean(value)
        if value is None:
            return value
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            raise ValueError("Enter a valid datetime")


class ChoiceField(FormField):
    """Field with predefined choices."""

    def __init__(self, *, choices: list[tuple[Any, str]] | None = None, **kwargs):
        self.choices = choices or []
        super().__init__(**kwargs)

    def _default_widget(self) -> str:
        return "select"

    def clean(self, value: Any) -> Any:
        value = super().clean(value)
        if value is None:
            return value
        valid_values = {str(c[0]) for c in self.choices}
        if str(value) not in valid_values:
            # Do NOT echo the raw submitted value back into the error message:
            # it would be reflected into rendered HTML / JSON error output (XSS).
            raise ValueError(
                self.error_messages.get(
                    "invalid_choice",
                    "Select a valid choice. That choice is not one of the available choices.",
                )
            )
        return value


class EmailField(CharField):
    """Email field with basic validation."""

    def _default_widget(self) -> str:
        return "email"

    def clean(self, value: Any) -> str | None:
        value = super().clean(value)
        if value is None:
            return value
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("Enter a valid email address")
        return value


class PasswordField(CharField):
    """Password field (renders as type=password)."""

    def _default_widget(self) -> str:
        return "password"


class HiddenField(CharField):
    """Hidden field."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def _default_widget(self) -> str:
        return "hidden"


# ---------------------------------------------------------------------------
# Form metaclass
# ---------------------------------------------------------------------------


class FormMeta(type):
    """Metaclass that collects FormField instances from class attributes."""

    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        # Skip base Form/ModelForm classes
        if name in _BASE_FORM_NAMES:
            return cls

        # Collect fields from class and bases
        declared_fields = {}
        for base in reversed(bases):
            if hasattr(base, "_declared_fields"):
                declared_fields.update(base._declared_fields)

        for attr_name, attr_value in namespace.items():
            if isinstance(attr_value, FormField):
                attr_value.name = attr_name
                if attr_value.label is None:
                    attr_value.label = attr_name.replace("_", " ").title()
                declared_fields[attr_name] = attr_value

        # MERGE with any fields already populated by __init_subclass__
        # (ModelForm builds the model's fields there). Overwriting with only the
        # namespace-collected `declared_fields` would silently drop EVERY model
        # field whenever a ModelForm also declares one extra (non-model) field.
        # __init_subclass__'s result already includes those same extra fields, so
        # {**existing, **declared_fields} preserves model fields + extras.
        existing = cls.__dict__.get("_declared_fields") or {}
        merged = {**existing, **declared_fields}
        if merged:
            cls._declared_fields = merged
        return cls


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------


class Form(metaclass=FormMeta):
    """Declarative form with validation and rendering.

    Usage:
        class ContactForm(Form):
            name = CharField(max_length=100)
            email = EmailField()

        form = ContactForm(data={"name": "Alice", "email": "alice@example.com"})
        if form.is_valid():
            print(form.cleaned_data)
    """

    _declared_fields: dict[str, FormField] = {}

    def __init__(
        self,
        data: dict | None = None,
        initial: dict | None = None,
        instance: Any = None,
    ):
        self.data = data or {}
        self.initial = initial or {}
        self.instance = instance
        self.errors: dict[str, list[str]] = {}
        self.cleaned_data: dict[str, Any] = {}
        self._is_bound = data is not None
        self._validated = False

    @property
    def fields(self) -> dict[str, FormField]:
        """Return all declared fields."""
        return self._declared_fields

    def is_valid(self) -> bool:
        """Validate all fields and return True if no errors."""
        if not self._is_bound:
            return False
        if not self._validated:
            self._validate()
        return len(self.errors) == 0

    def _validate(self):
        """Run field-level, per-field hook, and form-level validation.

        Validation pipeline for each field:
        1. field.clean(value) — type coercion + field constraints
        2. clean_<fieldname>() — per-field custom validation (if defined on form)
        Then after all fields:
        3. clean() — cross-field validation
        """
        self.errors = {}
        self.cleaned_data = {}

        # Field-level validation + per-field clean hooks
        for name, field_obj in self.fields.items():
            value = self.data.get(name)
            try:
                cleaned = field_obj.clean(value)
                self.cleaned_data[name] = cleaned
            except ValueError as e:
                self.errors.setdefault(name, []).append(str(e))
                continue

            # Per-field clean hook: clean_<fieldname>()
            clean_method = type(self).__dict__.get(f"clean_{name}")
            if clean_method is None:
                # Check parent classes
                for cls in type(self).__mro__[1:]:
                    clean_method = cls.__dict__.get(f"clean_{name}")
                    if clean_method is not None:
                        break
            if clean_method is not None:
                try:
                    result = clean_method(self)
                    if result is not None:
                        self.cleaned_data[name] = result
                except ValueError as e:
                    self.errors.setdefault(name, []).append(str(e))

        # Form-level cross-field validation. Django runs clean() regardless of
        # whether individual fields errored (clean() reads self.cleaned_data,
        # which simply omits the fields that failed), so we do the same.
        try:
            self.clean()
        except ValueError as e:
            self.errors.setdefault("__all__", []).append(str(e))

        self._validated = True

    def clean(self):
        """Override for cross-field validation.

        Raise ValueError to add a form-level error.
        Can also modify self.cleaned_data.
        """
        pass

    def add_error(self, field: str | None, error: str) -> None:
        """Add an error to a specific field or to the form as a whole.

        Args:
            field: Field name, or None for form-level (non-field) errors.
            error: Error message string.
        """
        key = field if field is not None else "__all__"
        self.errors.setdefault(key, []).append(error)
        # Remove from cleaned_data if field-specific
        if field is not None and field in self.cleaned_data:
            del self.cleaned_data[field]

    def has_error(self, field: str, code: str | None = None) -> bool:
        """Check if a field has any error (or a specific error message).

        Args:
            field: Field name to check.
            code: Optional error message to match (None = any error).
        """
        field_errors = self.errors.get(field, [])
        if not field_errors:
            return False
        if code is None:
            return True
        return code in field_errors

    def non_field_errors(self) -> list[str]:
        """Return errors not associated with any specific field."""
        return self.errors.get("__all__", [])

    def errors_as_json(self) -> str:
        """Serialize form errors as a JSON string."""
        return fast_json_dumps(self.get_json_data()).decode()

    def get_json_data(self) -> dict[str, list[dict[str, str]]]:
        """Return form errors as a structured dict for JSON serialization.

        Returns:
            Dict mapping field names to lists of error dicts with "message" key.
        """
        result: dict[str, list[dict[str, str]]] = {}
        for field, messages in self.errors.items():
            result[field] = [{"message": msg} for msg in messages]
        return result

    # --- Rendering ---

    def as_div(self) -> str:
        """Render the form as a series of <div> elements."""
        parts = []
        for name, field_obj in self.fields.items():
            value = self.data.get(name, self.initial.get(name, field_obj.initial))
            field_errors = self.errors.get(name, [])
            label_html = (
                f'<label for="id_{name}">{_escape(str(field_obj.label))}</label>'
            )
            field_html = field_obj.render(value, field_errors)
            help_html = (
                f'<span class="help-text">{_escape(str(field_obj.help_text))}</span>'
                if field_obj.help_text
                else ""
            )
            error_class = " has-error" if field_errors else ""
            parts.append(
                f'<div class="form-group{error_class}">'
                f"{label_html}{field_html}{help_html}"
                f"</div>"
            )

        # Form-level errors
        all_errors = self.errors.get("__all__", [])
        if all_errors:
            error_html = "".join(
                f'<div class="form-error">{_escape(str(e))}</div>' for e in all_errors
            )
            parts.insert(0, error_html)

        return "\n".join(parts)

    def as_table(self) -> str:
        """Render the form as table rows (<tr>)."""
        parts = []
        for name, field_obj in self.fields.items():
            value = self.data.get(name, self.initial.get(name, field_obj.initial))
            field_errors = self.errors.get(name, [])
            field_html = field_obj.render(value, field_errors)
            parts.append(
                f'<tr><th><label for="id_{name}">{_escape(str(field_obj.label))}</label></th>'
                f"<td>{field_html}</td></tr>"
            )
        return "\n".join(parts)

    def as_p(self) -> str:
        """Render the form as <p> elements."""
        parts = []
        for name, field_obj in self.fields.items():
            value = self.data.get(name, self.initial.get(name, field_obj.initial))
            field_errors = self.errors.get(name, [])
            field_html = field_obj.render(value, field_errors)
            parts.append(
                f"<p><label>{_escape(str(field_obj.label))}: {field_html}</label></p>"
            )
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# ModelForm
# ---------------------------------------------------------------------------

# Type → FormField mapping for auto-generation from Model annotations
_TYPE_TO_FIELD = {
    str: CharField,
    int: IntegerField,
    float: FloatField,
    bool: BooleanField,
    Decimal: DecimalField,
    date: DateField,
    datetime: DateTimeField,
}


class ModelForm(Form):
    """Form auto-generated from a HyperApp Model.

    Usage:
        class UserForm(ModelForm):
            class Meta:
                model = User
                fields = ["username", "email", "age"]

        form = UserForm(data=request_data)
        if form.is_valid():
            user = await form.save()  # INSERT or UPDATE
    """

    class Meta:
        model = None
        fields: list[str] | None = None
        exclude: list[str] | None = None
        widgets: dict[str, str] | None = None  # {field_name: widget_type}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # ModelForm always defines a base Meta (model=None), so cls.Meta always
        # resolves — either the inherited base or a user-declared inner class.
        meta = cls.Meta
        # dynamic-attr: meta is a user-authored inner Meta class; `model` is optional on it
        if getattr(meta, "model", None) is None:
            return

        model = meta.model
        # dynamic-attr: model is an arbitrary user class; `_meta` is present only on HyperApp models
        model_meta = getattr(model, "_meta", None)
        if model_meta is None:
            return

        # fields/exclude/widgets are optional attributes on the user's Meta class
        fields_list = getattr(
            meta, "fields", None
        )  # dynamic-attr: optional on user Meta
        exclude_raw = getattr(
            meta, "exclude", None
        )  # dynamic-attr: optional on user Meta
        exclude_list = exclude_raw or []
        widgets = (
            getattr(meta, "widgets", None) or {}  # dynamic-attr: optional on user Meta
        )

        # SECURITY (mass-assignment): a ModelForm whose Meta sets NEITHER `fields`
        # NOR `exclude` would fall through the allow-list below and bind EVERY
        # writable model field — including sensitive ones like is_staff /
        # is_superuser — letting them be set straight from the request body via
        # save(). Django raises ImproperlyConfigured here for exactly this reason;
        # do the same so the unsafe form never comes into existence.
        if fields_list is None and exclude_raw is None:
            raise ValueError(
                f"{cls.__name__}: creating a ModelForm without either the "
                "'fields' or 'exclude' Meta attribute is prohibited (it would "
                "bind every writable model field, a mass-assignment risk). "
                "Set Meta.fields to an explicit allow-list of field names, or "
                "Meta.exclude to a deny-list. Use fields = '__all__' only if you "
                "deliberately intend to expose all fields."
            )
        # Allow the explicit Django-style opt-in: fields = "__all__".
        if fields_list == "__all__":
            fields_list = None

        # Build fields from model annotations
        declared = {}
        for field_name, field_meta in model_meta.fields.items():
            # Skip auto fields (PK)
            if field_meta.auto:
                continue
            # Apply fields/exclude filters
            if fields_list is not None and field_name not in fields_list:
                continue
            if field_name in exclude_list:
                continue

            # Get Python type from model annotations
            python_type = model.__annotations__.get(field_name, str)
            # Handle Optional[X]
            # dynamic-attr: python_type is an arbitrary annotation; __origin__ exists only on typing generics
            origin = getattr(python_type, "__origin__", None)
            if origin is type(int | None):
                # dynamic-attr: __args__ exists only on parameterized typing generics
                args = getattr(python_type, "__args__", ())
                python_type = args[0] if args else str

            # Map type to field class
            field_cls = _TYPE_TO_FIELD.get(python_type, CharField)

            # Check for Enum
            if isinstance(python_type, type) and issubclass(python_type, Enum):
                field_cls = ChoiceField

            # Build field kwargs — fields with defaults or auto are optional
            # Use the same FieldInfo lookup pattern as admin/_introspect_model
            field_info_obj = model.__dict__.get(field_name)
            if not isinstance(field_info_obj, FieldInfo):
                field_info_obj = None
            has_default = (
                field_info_obj is not None and field_info_obj.default is not _MISSING
            )
            field_kwargs = {
                "required": not field_meta.auto
                and field_name != "id"
                and not has_default,
                "label": field_name.replace("_", " ").title(),
            }

            # Widget override
            if field_name in widgets:
                field_kwargs["widget"] = widgets[field_name]

            # Create the field
            if (
                field_cls is ChoiceField
                and isinstance(python_type, type)
                and issubclass(python_type, Enum)
            ):
                field_kwargs["choices"] = [(m.value, m.name) for m in python_type]

            field_obj = field_cls(**field_kwargs)
            field_obj.name = field_name
            declared[field_name] = field_obj

        # Merge with any explicitly declared fields on the class
        for attr_name, attr_value in vars(cls).items():
            if isinstance(attr_value, FormField):
                attr_value.name = attr_name
                if attr_value.label is None:
                    attr_value.label = attr_name.replace("_", " ").title()
                declared[attr_name] = attr_value

        cls._declared_fields = declared

    async def save(self, db=None, commit: bool = True):
        """Save the form data to the database.

        If instance is set, performs UPDATE. Otherwise INSERT.
        Returns the model instance.
        """
        if not self.is_valid():
            raise ValueError("Cannot save invalid form")

        meta = self.Meta
        model = meta.model

        if self.instance is not None:
            # UPDATE
            for key, value in self.cleaned_data.items():
                # dynamic-attr: key is a model field name from cleaned_data, assigned onto the user's model instance
                setattr(self.instance, key, value)
            if commit:
                await self.instance.save(db=db)
            return self.instance
        else:
            # INSERT — include defaults for fields not in the form
            init_data = dict(self.cleaned_data)
            # dynamic-attr: model is an arbitrary user class; `_meta` is present only on HyperApp models
            model_meta = getattr(model, "_meta", None)
            if model_meta:
                for field_name, field_meta in model_meta.fields.items():
                    if field_name not in init_data and not field_meta.auto:
                        # Get the default from the FieldInfo descriptor
                        field_info = model.__dict__.get(field_name)
                        if (
                            isinstance(field_info, FieldInfo)
                            and field_info.default is not _MISSING
                        ):
                            init_data[field_name] = field_info.default
            instance = model(**init_data)
            if commit:
                await instance.save(db=db)
            return instance


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _escape(s: str) -> str:
    """HTML-escape a string."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


# ---------------------------------------------------------------------------
# FormSet — manage multiple form instances together
# ---------------------------------------------------------------------------


class FormSet:
    """Manage multiple Form instances as a group.

    Validates all forms together, supports adding/deleting forms,
    and provides aggregate error reporting.

    Usage:
        class ItemForm(Form):
            name = CharField(max_length=100)
            quantity = IntegerField(min_value=0)

        formset = FormSet(ItemForm, data=[
            {"name": "Widget", "quantity": 5},
            {"name": "Gadget", "quantity": 3},
        ])
        if formset.is_valid():
            for form in formset:
                print(form.cleaned_data)

        # With extra blank forms
        formset = FormSet(ItemForm, data=[...], extra=2)

        # With deletion support
        formset = FormSet(ItemForm, data=[
            {"name": "Widget", "quantity": 5, "DELETE": False},
            {"name": "Old", "quantity": 0, "DELETE": True},
        ], can_delete=True)
    """

    def __init__(
        self,
        form_class: type,
        data: list[dict] | None = None,
        initial: list[dict] | None = None,
        extra: int = 0,
        max_num: int | None = None,
        min_num: int = 0,
        can_delete: bool = False,
        can_order: bool = False,
        prefix: str = "form",
    ):
        self.form_class = form_class
        self.extra = extra
        self.max_num = max_num
        self.min_num = min_num
        self.can_delete = can_delete
        self.can_order = can_order
        self.prefix = prefix
        self._errors: list[dict[str, list[str]]] | None = None

        # Build form instances
        self.forms: list[Form] = []
        self.deleted_forms: list[Form] = []

        if data is not None:
            for i, form_data in enumerate(data):
                if self.max_num is not None and i >= self.max_num:
                    break
                # Check deletion flag
                if self.can_delete and form_data.get("DELETE"):
                    self.deleted_forms.append(form_class(data=form_data))
                    continue
                self.forms.append(form_class(data=form_data))
        elif initial:
            # Initial data seeds UNBOUND forms (rendered with values but not yet
            # submitted). Passing it as `data=` would mark them bound and force
            # validation of not-yet-submitted forms. Use `initial=` instead.
            for i, init_data in enumerate(initial):
                self.forms.append(form_class(initial=init_data))

        # Add extra blank forms
        for _ in range(extra):
            if self.max_num is not None and len(self.forms) >= self.max_num:
                break
            self.forms.append(form_class())

    def is_valid(self) -> bool:
        """Validate all forms. Returns True only if ALL forms are valid."""
        if not self.forms:
            # Empty formset: valid if min_num == 0
            return self.min_num == 0

        self._errors = []
        all_valid = True
        valid_count = 0

        for form in self.forms:
            # Unbound (blank extra / initial-only) forms are valid-and-ignored:
            # Django never fails a formset because empty extra forms exist. They
            # don't count toward valid_count for min_num either — only submitted
            # (bound) forms represent real data.
            if not form._is_bound:
                self._errors.append({})
                continue
            if form.is_valid():
                valid_count += 1
                self._errors.append({})
            else:
                all_valid = False
                self._errors.append(form.errors)

        # Check min_num constraint
        if valid_count < self.min_num:
            all_valid = False

        return all_valid

    @property
    def errors(self) -> list[dict[str, list[str]]]:
        """List of error dicts, one per form."""
        if self._errors is None:
            self.is_valid()
        return self._errors

    @property
    def cleaned_data(self) -> list[dict[str, Any]]:
        """List of cleaned data dicts from valid, bound forms only.

        Unbound extra/initial forms and invalid forms (whose ``cleaned_data`` is
        a PARTIAL dict of only the fields that happened to pass) are excluded —
        otherwise callers would consume half-validated rows.
        """
        return [f.cleaned_data for f in self.forms if f._is_bound and f.is_valid()]

    @property
    def total_form_count(self) -> int:
        """Total number of forms (including deleted)."""
        return len(self.forms) + len(self.deleted_forms)

    @property
    def initial_form_count(self) -> int:
        """Number of forms with data (not extra blanks)."""
        return len([f for f in self.forms if f._is_bound])

    def __len__(self) -> int:
        return len(self.forms)

    def __iter__(self):
        return iter(self.forms)

    def __getitem__(self, index):
        return self.forms[index]

    def non_form_errors(self) -> list[str]:
        """Errors that apply to the formset as a whole (e.g., min_num violation)."""
        errors = []
        if self._errors is not None:
            valid_count = sum(1 for e in self._errors if not e)
            if valid_count < self.min_num:
                errors.append(
                    f"At least {self.min_num} forms are required (got {valid_count})"
                )
        return errors


class ModelFormSet(FormSet):
    """FormSet for creating/updating model instances.

    Usage:
        class ItemForm(ModelForm):
            class Meta:
                model = Item
                fields = ["name", "value"]

        formset = ModelFormSet(ItemForm, data=[
            {"name": "A", "value": 1},
            {"name": "B", "value": 2},
        ])
        if formset.is_valid():
            items = await formset.save()
    """

    def __init__(self, form_class: type, queryset=None, **kwargs):
        self.queryset = queryset
        super().__init__(form_class, **kwargs)

    async def save(self) -> list[object]:
        """Save all valid forms and return created/updated instances."""
        instances = []
        for form in self.forms:
            # Only persist valid, bound forms — never an unbound extra blank or
            # an invalid form whose cleaned_data is a partial (half-saved) dict.
            if form._is_bound and form.is_valid() and hasattr(form, "save"):
                instance = await form.save()
                instances.append(instance)

        # Handle deletions
        for form in self.deleted_forms:
            if (
                hasattr(form, "instance")
                and form.instance
                and hasattr(form.instance, "delete")
            ):
                await form.instance.delete()

        return instances
