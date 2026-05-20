"""
Admin field introspection and model configuration dataclasses.

Provides AdminField descriptors, type-to-widget mapping, model introspection,
and configuration dataclasses (Fieldset, Action, InlineConfig, ModelConfig)
for the HyperAdmin auto-generated CRUD interface.
"""

import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import _get_db_meta
from hyperdjango.validation.core.fields import _MISSING, FieldInfo

# Fields from TimestampMixin that are auto-populated by save() — never shown
# as required inputs on add forms, always readonly on all forms.
_TIMESTAMP_MIXIN_FIELDS = frozenset({"created_at", "updated_at"})

# ── Field type -> HTML widget mapping ──────────────────────────────────────


@dataclass(slots=True)
class AdminField:
    """Metadata for rendering a model field in admin forms."""

    name: str
    label: str
    python_type: type
    widget: str  # HTML input type or 'textarea' / 'select' / 'checkbox'
    required: bool
    default: Any
    attrs: dict[
        str, str | int | float
    ]  # Extra HTML attributes (min, max, step, maxlength, etc.)
    choices: list[tuple[str, str]] | None  # For enum/select fields
    is_pk: bool
    is_auto: bool
    is_readonly: bool
    foreign_key: str | None


def _type_to_widget(
    python_type, field_info: FieldInfo | None, db_meta: dict[str, str | bool | None]
) -> tuple[str, dict[str, str | int | float]]:
    """Map a Python type annotation to an HTML widget and attributes."""
    attrs = {}
    origin = typing.get_origin(python_type)

    # Unwrap Optional[T]
    if origin is typing.Union:
        args = [a for a in python_type.__args__ if a is not type(None)]
        if args:
            python_type = args[0]
            origin = typing.get_origin(python_type)

    # Extract constraints from FieldInfo
    if field_info:
        if field_info.max_length is not None:
            attrs["maxlength"] = field_info.max_length
        if field_info.min_length is not None:
            attrs["minlength"] = field_info.min_length
        if field_info.ge is not None:
            attrs["min"] = field_info.ge
        if field_info.gt is not None:
            attrs["min"] = field_info.gt + 1
        if field_info.le is not None:
            attrs["max"] = field_info.le
        if field_info.lt is not None:
            attrs["max"] = field_info.lt - 1
        if field_info.pattern is not None:
            attrs["pattern"] = field_info.pattern

    # Map type to widget
    if python_type is bool:
        return "checkbox", attrs
    if python_type is int:
        return "number", attrs
    if python_type is float or python_type is Decimal:
        attrs.setdefault("step", "0.01")
        return "number", attrs
    if python_type is datetime:
        return "datetime-local", attrs
    if python_type is date:
        return "date", attrs
    if python_type is time:
        return "time", attrs

    # Check for email-like field names or types
    if python_type is str:
        # Long text heuristic: no max_length or max_length > 500
        max_len = attrs.get("maxlength")
        if max_len is not None and max_len > 500:
            return "textarea", attrs
        return "text", attrs

    # Enum -> select
    if isinstance(python_type, type) and issubclass(python_type, Enum):
        return "select", attrs

    # FK -> number (ID input)
    if db_meta.get("foreign_key"):
        return "number", attrs

    # Fallback
    return "text", attrs


def _introspect_model(model_class) -> list[AdminField]:
    """Introspect a HyperApp Model and return AdminField descriptors."""
    fields = []
    meta = model_class._meta
    annotations = {}

    # Walk MRO to collect all annotations
    for klass in reversed(model_class.__mro__):
        if hasattr(klass, "__annotations__"):
            annotations.update(klass.__annotations__)

    # Internal BaseModel attributes to skip
    _SKIP_FIELDS = {"model_config", "model_fields", "model_computed_fields"}

    for field_name, annotation in annotations.items():
        if field_name.startswith("_"):
            continue
        if field_name in _SKIP_FIELDS:
            continue

        # Get FieldInfo and db metadata — walk MRO for mixin-inherited fields
        field_info = model_class.__dict__.get(field_name)
        if not isinstance(field_info, FieldInfo):
            # Field may be inherited from a mixin (e.g., TimestampMixin)
            for klass in model_class.__mro__[1:]:
                fi = klass.__dict__.get(field_name)
                if isinstance(fi, FieldInfo):
                    field_info = fi
                    break
            else:
                field_info = None

        field_meta = meta.fields.get(field_name)
        db_meta = {}
        if field_info:
            db_meta = _get_db_meta(field_info)

        is_pk = field_meta.primary_key if field_meta else False
        is_auto = field_meta.auto if field_meta else False
        foreign_key = field_meta.foreign_key if field_meta else None

        # TimestampMixin fields are auto-populated by save() — treat as auto
        if field_name in _TIMESTAMP_MIXIN_FIELDS and issubclass(
            model_class, TimestampMixin
        ):
            is_auto = True

        # Determine required
        if field_info:
            required = field_info.is_required and not is_auto
            default = field_info.default if field_info.default is not _MISSING else None
        else:
            required = True
            default = None

        # Widget mapping
        widget, attrs = _type_to_widget(annotation, field_info, db_meta)

        # Choices for enums
        choices = None
        raw_type = annotation
        origin = typing.get_origin(annotation)
        if origin is typing.Union:
            args = [a for a in annotation.__args__ if a is not type(None)]
            if args:
                raw_type = args[0]
        if isinstance(raw_type, type) and issubclass(raw_type, Enum):
            choices = [(e.value, e.name) for e in raw_type]

        # Label from field name
        label = field_name.replace("_", " ").title()

        fields.append(
            AdminField(
                name=field_name,
                label=label,
                python_type=annotation,
                widget=widget,
                required=required and not is_auto,
                default=default,
                attrs=attrs,
                choices=choices,
                is_pk=is_pk,
                is_auto=is_auto,
                is_readonly=is_auto,
                foreign_key=foreign_key,
            )
        )

    return fields


# ── Model registration config ────────────────────────────────────────────


@dataclass
class Fieldset:
    """A group of fields with an optional title and CSS classes."""

    title: str | None
    fields: list[str]
    classes: list[str] = field(default_factory=list)  # e.g. ["collapse"]
    description: str = ""


@dataclass
class Action:
    """A bulk action applicable to selected rows."""

    name: str
    label: str
    handler: Any  # async callable(config, request, selected_ids) -> str message
    confirm: bool = False  # Show confirmation page before executing


@dataclass
class InlineConfig:
    """Configuration for inline editing of related objects on a parent form.

    Usage:
        admin.register(Author, inlines=[
            InlineConfig(model_class=Book, fields=["title", "year"], extra=1),
        ])
    """

    model_class: type  # The related Model class
    fields: list[str] | None = None  # Fields to show (None = all non-auto)
    extra: int = 1  # Number of blank rows to show for new entries
    max_num: int | None = None  # Max total inline rows
    can_delete: bool = True  # Show delete checkbox per row
    fk_field: str | None = (
        None  # FK field name on inline model (auto-detected if only one FK)
    )
    ordering: str | None = None  # Sort inline rows by this field
    show_change_link: bool = False  # Show link to full edit form per inline row
    classes: list[str] = field(default_factory=list)  # CSS classes (e.g., ["collapse"])


@dataclass
class ModelConfig:
    """Configuration for a registered admin model."""

    model_class: type
    slug: str
    name: str
    fields: list[AdminField]
    list_display: list[str] | None = None
    search_fields: list[str] | None = None
    ordering: str | None = None
    per_page: int = 25
    readonly_fields: list[str] = field(default_factory=list)
    exclude_fields: list[str] = field(default_factory=list)
    fieldsets: list[Fieldset] | None = None
    list_filter: list[str] = field(default_factory=list)
    list_editable: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    list_display_callables: dict[str, Callable] = field(
        default_factory=dict
    )  # name -> callable(obj_dict) -> str
    save_hooks: list[Callable] = field(
        default_factory=list
    )  # list of async callable(values, is_edit) -> values
    delete_hooks: list[Callable] = field(
        default_factory=list
    )  # list of async callable(pk) -> None
    # Template overrides -- if set, used instead of built-in defaults
    list_template: str | None = None  # override list view template
    form_template: str | None = None  # override add/edit form template
    delete_template: str | None = None  # override delete confirmation template
    # Per-model CSS/JS media
    media_css: list[str] = field(default_factory=list)  # CSS file paths to inject
    media_js: list[str] = field(default_factory=list)  # JS file paths to inject
    # Widget overrides -- map Python type -> widget config dict
    formfield_overrides: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # {str: {"widget": "textarea"}, ...}
    # Inline related objects -- edit child models on the parent form
    inlines: list[InlineConfig] = field(default_factory=list)
    # Prepopulated fields -- auto-fill one field from others via JS
    # Format: {"slug": ["title"]} -> slug auto-filled from title
    prepopulated_fields: dict[str, list[str]] = field(default_factory=dict)
    # Date hierarchy -- date field for drill-down year/month/day navigation
    date_hierarchy: str | None = None
    # ── Dynamic per-request hooks ────────────────────────────────────
    # get_queryset: async (request) -> dict|None -- extra WHERE filters for list/edit/delete
    get_queryset: Callable | None = None
    # get_readonly_fields: (request, obj_dict|None) -> list[str] -- dynamic readonly
    get_readonly_fields: Callable | None = None
    # get_fieldsets: (request, obj_dict|None) -> list[Fieldset] -- dynamic fieldsets
    get_fieldsets: Callable | None = None
    # get_list_display: (request) -> list[str] -- dynamic columns
    get_list_display: Callable | None = None
    # get_search_results: async (request, base_conditions, search_term) -> dict|None
    get_search_results: Callable | None = None
    # get_form: (request, obj_dict|None) -> dict -- {fields, required, widgets}
    get_form: Callable | None = None
    # has_view_permission -- can view but not edit
    can_view: bool = True
    # ── View control ─────────────────────────────────────────────────
    # list_display_links: list[str]|None -- which columns link to edit. None = no links.
    list_display_links: list[str] | None = field(default=None, repr=False)
    # view_on_site: callable(obj_dict) -> str|None -- link to front-end URL
    view_on_site: Callable | None = None
    # response_add/change/delete: "list"|"continue"|"add"|callable(request,obj)->url
    response_add: str | Callable = "list"
    response_change: str | Callable = "list"
    response_delete: str | Callable = "list"
    # empty_value_display: what to show for NULL/empty
    empty_value_display: str = "-"
    # save_as: show "Save as new" button on edit form
    save_as: bool = False
    # save_on_top: show save buttons at top of form
    save_on_top: bool = False
    # show_full_result_count: show total count on filtered views
    show_full_result_count: bool = True
    # sortable_by: restrict which columns are sortable (None = all)
    sortable_by: list[str] | None = None
    # radio_fields: {"field_name": "horizontal"|"vertical"}
    radio_fields: dict[str, str] = field(default_factory=dict)
    # raw_id_fields: FK fields shown as plain number input (no autocomplete)
    raw_id_fields: list[str] = field(default_factory=list)
    # autocomplete_fields: FK fields with HTMX autocomplete (None = all FKs)
    autocomplete_fields: list[str] | None = None
    # preserve_filters: keep filter/search/sort state across edit navigation
    preserve_filters: bool = True
    # filter_horizontal: M2M field names to render as dual-select widget
    filter_horizontal: list[str] = field(default_factory=list)
    # on_add/on_change/on_delete: post-save hooks (after DB write + audit log)
    on_add: Callable | None = None
    on_change: Callable | None = None
    on_delete: Callable | None = None

    # ── Caches populated lazily / at __post_init__ ────────────────────
    # field name → AdminField, for O(1) lookup in hot paths (was linear
    # next(...) scan called per field per row in _build_list_context)
    _field_by_name: dict[str, AdminField] = field(default_factory=dict)
    # Cached display_columns result — list_display + fields are immutable
    # after register(), so we compute the descriptor list once.
    _display_columns_cache: list[dict[str, str | bool]] | None = None
    # FK column → resolved display column (lazy, cached per process).
    # Saves up to 5 DB probes per FK column per request after warmup.
    _fk_display_col_cache: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Build the field-name lookup dict once. This is O(N) at startup
        # vs O(N²) per request if we used the original next(...) scan.
        self._field_by_name = {f.name: f for f in self.fields}

    @property
    def display_fields(self) -> list[AdminField]:
        """Fields shown in the list view."""
        if self.list_display:
            return [f for f in self.fields if f.name in self.list_display]
        # Default: show all non-auto fields, up to 6
        return [f for f in self.fields if not f.name.startswith("_")][:6]

    @property
    def display_columns(self) -> list[dict[str, str | bool]]:
        """Column descriptors for list view, including callables.

        Cached after first call — list_display + fields are immutable
        after register(), so the descriptor list is too. Eliminates the
        per-request O(N) scan that the changelist hot path was doing.
        """
        if self._display_columns_cache is not None:
            return self._display_columns_cache
        cols: list[dict[str, str | bool]] = []
        if self.list_display:
            field_by_name = self._field_by_name
            for name in self.list_display:
                af = field_by_name.get(name)
                if af is not None:
                    cols.append({"name": name, "label": af.label, "is_callable": False})
                elif name in self.list_display_callables:
                    fn = self.list_display_callables[name]
                    # dynamic-attr: user-supplied list_display callables may carry an optional _admin_description label (Django short_description equivalent); attr is optional
                    label = getattr(
                        fn, "_admin_description", name.replace("_", " ").title()
                    )
                    cols.append({"name": name, "label": label, "is_callable": True})
        else:
            for af in self.display_fields:
                cols.append({"name": af.name, "label": af.label, "is_callable": False})
        self._display_columns_cache = cols
        return cols

    @property
    def form_fields(self) -> list[AdminField]:
        """Fields shown in add/edit forms."""
        fieldset_fields: set[str] = set()
        if self.fieldsets:
            for fs in self.fieldsets:
                fieldset_fields.update(fs.fields)
        return [
            f
            for f in self.fields
            if f.name not in self.exclude_fields
            and (not f.name.startswith("_") or f.name in fieldset_fields)
        ]

    @property
    def grouped_form_fields(self) -> list[dict[str, str | list[AdminField]]]:
        """Form fields organized by fieldsets. Returns list of {title, fields, classes, description}."""
        if not self.fieldsets:
            return [
                {
                    "title": None,
                    "fields": self.form_fields,
                    "classes": [],
                    "description": "",
                }
            ]
        groups: list[dict[str, str | list[AdminField]]] = []
        for fs in self.fieldsets:
            group_fields = [f for f in self.form_fields if f.name in fs.fields]
            groups.append(
                {
                    "title": fs.title,
                    "fields": group_fields,
                    "classes": fs.classes,
                    "description": fs.description,
                }
            )
        return groups

    @property
    def searchable_fields(self) -> list[str]:
        """Fields used for search."""
        if self.search_fields:
            return self.search_fields
        # Default: search all str fields
        return [f.name for f in self.fields if f.python_type is str]

    @property
    def filter_fields(self) -> list[AdminField]:
        """Fields available as sidebar filters."""
        return [f for f in self.fields if f.name in self.list_filter]

    @property
    def field_count(self) -> int:
        return len([f for f in self.fields if not f.name.startswith("_")])


@dataclass(slots=True, frozen=True)
class ThemeConfig:
    """Admin theme configuration.

    CSS variables are merged with the base theme — only override what you change.
    The built-in light/dark themes handle most cases. Custom themes let you
    match brand colors or create high-contrast accessibility modes.

    Usage:
        from hyperdjango.admin.fields import ThemeConfig

        brand_theme = ThemeConfig(
            name="brand",
            label="Brand",
            css_vars={
                "--primary": "#7c3aed",
                "--btn-hover": "#6d28d9",
            },
        )
        admin.register_theme(brand_theme)
    """

    name: str  # Unique identifier (e.g., "brand", "solarized")
    label: str  # Human-readable label for the toggle menu
    css_vars: dict[str, str] = field(default_factory=dict)  # CSS variable overrides
    is_dark: bool = False  # Whether this is a dark variant (affects auto-detection)
