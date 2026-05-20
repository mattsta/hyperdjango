# HyperAdmin

Auto-generated CRUD admin interface for HyperApp models with full RBAC management, inlines, fieldsets, HTMX-powered interactions, bulk actions, and template customization. HyperAdmin reads model metadata to generate list views, create/edit forms, search, filtering, and pagination -- all without writing a single template.

Unlike Django's admin which requires `ModelAdmin` subclasses and class-based configuration, HyperAdmin uses a single `register()` call with keyword arguments. Registration is explicit and compositional: pass exactly the options you need.

## Quick Start

```python
from hyperdjango import HyperApp, Model, Field
from hyperdjango.admin import HyperAdmin

app = HyperApp(title="My App", database="postgres://localhost/mydb")


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    price: float = Field(ge=0)
    is_active: bool = Field(default=True)
    created_at: str = Field(auto_now_add=True)


admin = HyperAdmin(app, prefix="/admin", secret_key="change-me")
admin.register(Product)

# Auto-generates routes:
#   GET  /admin/              -> dashboard
#   GET  /admin/product/      -> list view (paginated, searchable)
#   GET  /admin/product/add/  -> create form
#   POST /admin/product/add/  -> create handler
#   GET  /admin/product/{id}/ -> edit form
#   POST /admin/product/{id}/ -> update handler
#   POST /admin/product/{id}/delete/ -> delete handler
```

## Constructor

```python
HyperAdmin(
    app,  # HyperApp instance
    prefix="/admin",  # URL prefix for all admin routes
    title="HyperAdmin",  # Page title shown in header and browser tab
    secret_key="change-me-in-prod",  # Session signing key (used for HMAC cookie signing)
    require_auth=True,  # Enable login gating (default: True)
)
```

Parameters:

- `app` -- the `HyperApp` instance. HyperAdmin registers routes directly on this app.
- `prefix` -- URL prefix. All admin URLs are mounted under this path. Must start with `/`.
- `title` -- displayed in the admin header, browser tab, and login page.
- `secret_key` -- used to sign session cookies with HMAC-SHA256. Must be a strong random string in production.
- `require_auth` -- when `True`, all admin routes redirect unauthenticated users to the login page. Set to `False` only for development/testing.

## register()

Register a model with the admin interface. This generates all CRUD routes, list views, and forms for the model.

```python
admin.register(Model, **options)
```

### Minimal Registration

```python
admin.register(Product)
```

With no options, HyperAdmin introspects the model and generates:

- List view showing all fields (except primary key internals)
- Create/edit forms with auto-detected widgets based on field types
- Default ordering by primary key descending
- 25 items per page
- A built-in "delete_selected" bulk action

### Full Options Reference

```python
admin.register(
    Product,
    # --- List View ---
    list_display=["name", "price", "is_active"],  # Columns shown in list view
    list_filter=["is_active", "category"],  # Sidebar filter dropdowns
    search_fields=["name", "description"],  # Fields for search bar (ILIKE)
    ordering="-price",  # Default sort (- prefix for DESC)
    list_per_page=25,  # Items per page (default: 25)
    list_editable=["price", "is_active"],  # Inline-editable fields in list view
    date_hierarchy="created_at",  # Date drill-down navigation bar
    # --- Computed Columns ---
    list_display_callables={  # Computed/virtual columns
        "margin": lambda row: f"${row['price'] * 0.3:.2f}",
        "status": lambda row: "Active" if row["is_active"] else "Inactive",
    },
    # --- Form Layout ---
    readonly_fields=["created_at", "updated_at"],  # Shown but not editable
    exclude_fields=["internal_code"],  # Hidden from all forms
    fieldsets=[  # Grouped form layout
        Fieldset(title="Basic Info", fields=["name", "description"]),
        Fieldset(title="Pricing", fields=["price", "discount"]),
        Fieldset(title="Advanced", fields=["sku", "weight"], classes=["collapse"]),
    ],
    prepopulated_fields={"slug": ("name",)},  # Auto-fill from other fields (JS)
    formfield_overrides={
        str: {"widget": "textarea"}
    },  # Widget overrides by Python type
    # --- Bulk Actions ---
    actions=[
        Action(name="activate", label="Activate selected", handler=activate_handler),
        Action(
            name="export_csv",
            label="Export as CSV",
            handler=export_csv_handler,
            confirm=False,
        ),
    ],
    # --- Hooks ---
    save_hooks=[
        validate_price,
        slugify_name,
    ],  # async callable(values, is_edit) -> values
    delete_hooks=[cleanup_files],  # async callable(pk) -> None
    # --- Inlines ---
    inlines=[
        InlineConfig(model_class=ProductImage, fk_field="product_id"),
        InlineConfig(model_class=ProductVariant, fk_field="product_id", extra=2),
    ],
    # --- Templates & Media ---
    slug="products",  # URL slug (default: lowercase class name)
    list_template="custom_list.html",  # Template override for list view
    form_template="custom_form.html",  # Template override for forms
    media_css=["admin/custom.css"],  # Extra CSS files
    media_js=["admin/custom.js"],  # Extra JS files
    # --- Dynamic Per-Request Hooks ---
    get_queryset=my_queryset,  # async (request) -> dict|None — row filtering
    get_readonly_fields=my_readonly,  # (request, obj) -> list[str] — dynamic readonly
    get_fieldsets=my_fieldsets,  # (request, obj) -> list[Fieldset] — dynamic groups
    get_list_display=my_columns,  # (request) -> list[str] — dynamic columns
    get_search_results=my_search,  # async (request, conditions, term) -> dict|None
    get_form=my_form,  # (request, obj) -> dict — dynamic form config
    can_view=True,  # view-only mode (view but not edit)
    # --- View Control ---
    list_display_links=["name"],  # which columns link to edit (None/False = no links)
    view_on_site=lambda obj: f"/products/{obj['id']}",  # link to front-end URL
    response_add="continue",  # redirect after add: list/continue/add/callable
    response_change="list",  # redirect after edit
    save_as=True,  # "Save as new" button on edit form
    save_on_top=True,  # save buttons at top of long forms
    empty_value_display="(not set)",  # customize NULL display (default: "-")
    show_full_result_count=True,  # show total count on filtered lists
    sortable_by=["name", "price"],  # restrict which columns are sortable
    preserve_filters=True,  # keep filter state across edit navigation
    # --- Form Widgets ---
    radio_fields={"status": "horizontal"},  # render as radio buttons
    raw_id_fields=["category_id"],  # plain number input (no autocomplete)
    autocomplete_fields=["brand_id"],  # selective FK autocomplete
    # --- Post-Action Hooks ---
    on_add=notify_created,  # async (request, values) — after add+audit
    on_change=notify_updated,  # async (request, values) — after edit+audit
    on_delete=cleanup_files,  # async (request, pk) — after delete+audit
)
```

### list_display

Controls which columns appear in the list view table. Accepts field names from the model.

```python
admin.register(Product, list_display=["name", "price", "is_active", "created_at"])
```

If not specified, all fields are shown. The primary key column is always included as a link to the edit form.

Field values are automatically formatted based on type:

- `bool` fields render as colored badges ("Yes" / "No")
- `float`/`Decimal` fields render with 2 decimal places
- `datetime` fields render in `YYYY-MM-DD HH:MM` format
- `None` values render as a dash (`-`)

### list_display_callables

Add computed/virtual columns that don't correspond to database fields. Each callable receives the row as a dict and returns a display string.

```python
admin.register(
    Product,
    list_display_callables={
        "margin": lambda row: f"${row['price'] * 0.3:.2f}",
        "full_name": lambda row: f"{row['first_name']} {row['last_name']}",
        "age_days": lambda row: (datetime.now() - row["created_at"]).days,
    },
)
```

Callable columns appear after regular columns in the list view. They cannot be sorted or filtered.

### list_filter

Adds sidebar filter dropdowns to the list view. Each filter queries distinct values from the database and presents them as clickable links.

```python
admin.register(Product, list_filter=["is_active", "category", "warehouse"])
```

Filters work with:

- `bool` fields: shows "Yes" / "No" / "All"
- `str` fields: shows distinct values (up to 100)
- Foreign key fields: shows related object names
- `date`/`datetime` fields: shows "Today", "Past 7 days", "This month", "This year"

### search_fields

Enable the search bar in the list view. Searches use `ILIKE` (case-insensitive) across all specified fields with OR logic.

```python
admin.register(Product, search_fields=["name", "description", "sku"])
```

The search query is split on whitespace. Each term must match at least one field:

```sql
-- Search for "red widget" generates:
WHERE (name ILIKE '%red%' OR description ILIKE '%red%' OR sku ILIKE '%red%')
  AND (name ILIKE '%widget%' OR description ILIKE '%widget%' OR sku ILIKE '%widget%')
```

### ordering

Default sort order for the list view. Prefix with `-` for descending.

```python
admin.register(Product, ordering="-created_at")  # Newest first
admin.register(Product, ordering="name")  # Alphabetical
```

Users can click column headers to sort by that column, overriding the default.

### list_per_page

Number of items per page in the list view. Default is 25.

```python
admin.register(Product, list_per_page=50)
```

Pagination controls appear at the bottom of the list view with page numbers and "Previous" / "Next" links.

### list_editable

Fields that can be edited directly in the list view without opening the edit form. Editable cells render as input fields; a "Save" button appears at the bottom of the list.

```python
admin.register(Product, list_editable=["price", "is_active", "stock_count"])
```

Only fields that appear in `list_display` can be `list_editable`. The primary key cannot be editable. Permission checks are applied: the user must have edit permission for the model.

### date_hierarchy

Adds a date-based drill-down navigation bar above the list. Click a year to see months, a month to see days.

```python
admin.register(Product, date_hierarchy="created_at")
```

The hierarchy automatically adapts to the data range. If all records are in one month, it shows day-level drill-down directly.

### readonly_fields

Fields displayed in the form but not editable. Rendered as plain text instead of input widgets.

```python
admin.register(Product, readonly_fields=["created_at", "updated_at", "computed_hash"])
```

### exclude_fields

Fields completely hidden from forms (not shown at all, not submitted). Useful for internal tracking fields.

```python
admin.register(Product, exclude_fields=["internal_code", "legacy_id"])
```

### prepopulated_fields

Fields whose values are automatically generated from other fields via JavaScript. Typically used for slug generation.

```python
admin.register(
    Product,
    prepopulated_fields={
        "slug": ("name",),  # slug auto-fills from name
        "full_name": ("first_name", "last_name"),  # concatenates with separator
    },
)
```

The auto-fill happens on keyup in the source fields. Once the user manually edits the target field, auto-fill stops.

### formfield_overrides

Override the default widget for fields by Python type. This applies globally to all fields of that type on this model.

```python
admin.register(
    Product,
    formfield_overrides={
        str: {"widget": "textarea"},  # All str fields become textareas
        float: {"widget": "number", "step": "0.01"},
        bool: {"widget": "toggle"},  # Toggle switch instead of checkbox
    },
)
```

Available widget types:

- `"text"` -- single-line text input (default for `str`)
- `"textarea"` -- multi-line text area
- `"number"` -- numeric input with step/min/max
- `"toggle"` -- on/off toggle switch
- `"select"` -- dropdown select
- `"date"` -- date picker
- `"datetime"` -- datetime picker
- `"color"` -- color picker
- `"password"` -- password input (masked)

## ModelConfig

For advanced use cases, you can create a `ModelConfig` object directly instead of passing kwargs to `register()`. This is equivalent but allows reuse and subclassing.

```python
from hyperdjango.admin.fields import ModelConfig

config = ModelConfig(
    model_class=Product,
    list_display=["name", "price"],
    search_fields=["name"],
    ordering="-price",
    list_per_page=50,
)

admin.register_config(config)
```

`ModelConfig` is a dataclass with all the same fields as `register()` kwargs.

## Fieldsets

Group form fields into collapsible sections with titles and descriptions.

```python
from hyperdjango.admin.fields import Fieldset

admin.register(
    Product,
    fieldsets=[
        Fieldset(
            title="Basic Info",
            fields=["name", "description"],
            description="Core product information",
        ),
        Fieldset(
            title="Pricing",
            fields=["price", "discount", "tax_rate"],
        ),
        Fieldset(
            title="Inventory",
            fields=["sku", "stock_count", "warehouse"],
            classes=["collapse"],  # Initially collapsed, click to expand
        ),
        Fieldset(
            title="SEO",
            fields=["slug", "meta_title", "meta_description"],
            classes=["collapse"],
        ),
    ],
)
```

`Fieldset` parameters:

- `title` -- section heading displayed above the fields
- `fields` -- list of field names to include in this section
- `classes` -- list of CSS classes (e.g. `["collapse"]` to start collapsed with a toggle button)
- `description` -- optional text shown below the title (plain text or HTML)

If `fieldsets` is provided, only fields listed in fieldsets are shown in the form. Any field not in a fieldset is hidden.

If `fieldsets` is not provided, all fields (except `exclude_fields` and auto-increment PKs) are shown in a single ungrouped form.

## Inlines

Edit related objects directly on the parent form. When you edit an Order, you can add/remove/edit OrderItems inline without navigating away.

```python
from hyperdjango.admin.fields import InlineConfig

admin.register(
    Order,
    inlines=[
        InlineConfig(
            model_class=OrderItem,
            fk_field="order_id",  # FK column on child table (auto-detected if None)
            fields=["product", "qty", "unit_price"],  # Fields to show (None = all)
            extra=1,  # Number of blank rows for new items
            max_num=20,  # Maximum number of inline items
            can_delete=True,  # Show delete checkbox (default: True)
            ordering="product",  # Sort inline rows
        ),
    ],
)
```

`InlineConfig` parameters:

- `model_class` -- the related model class
- `fk_field` -- the foreign key field name on the child model pointing to the parent. If `None`, HyperAdmin auto-detects it by scanning the child model's fields for a FK to the parent table.
- `fields` -- list of field names to show. `None` shows all fields except the FK and auto-increment PK.
- `extra` -- number of empty rows for adding new related objects (default: 1)
- `max_num` -- maximum total inline items allowed. `None` for unlimited.
- `can_delete` -- show a delete checkbox on each row (default: `True`)
- `ordering` -- sort order for existing inline rows

### Multiple Inlines

A model can have multiple inline configurations:

```python
admin.register(
    Order,
    inlines=[
        InlineConfig(model_class=OrderItem, fk_field="order_id", extra=1),
        InlineConfig(model_class=OrderNote, fk_field="order_id", extra=0),
        InlineConfig(
            model_class=OrderStatusChange,
            fk_field="order_id",
            fields=["status", "changed_at", "changed_by"],
            can_delete=False,
        ),
    ],
)
```

Each inline renders as a separate section on the form page.

### HTMX Inline Row Endpoint

New inline rows can be added dynamically without a full page reload:

```
GET /admin/{model}/inline-row/?inline=0&index=3
```

Returns a single HTML table row for inline index 0 at position 3. The "Add another" button uses this endpoint via HTMX.

## Bulk Actions

Actions that operate on multiple selected rows from the list view. Users select rows with checkboxes, choose an action from the dropdown, and click "Go".

```python
from hyperdjango.admin.fields import Action


async def activate_handler(config, request, selected_ids):
    """Activate all selected products."""
    db = request.app.db
    for pk in selected_ids:
        await db.execute(
            f"UPDATE {config.model_class._meta.table} SET is_active = TRUE WHERE id = $1",
            int(pk),
        )
    return f"Activated {len(selected_ids)} products"


async def export_csv_handler(config, request, selected_ids):
    """Export selected rows as CSV. Return a Response to override default redirect."""
    from hyperdjango import Response

    db = request.app.db
    rows = await db.query(
        f"SELECT * FROM {config.model_class._meta.table} WHERE id = ANY($1)",
        [int(pk) for pk in selected_ids],
    )
    csv_data = format_as_csv(rows)
    return Response(
        csv_data,
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=export.csv"},
    )


admin.register(
    Product,
    actions=[
        Action(
            name="activate",
            label="Activate selected",
            handler=activate_handler,
            confirm=True,
        ),
        Action(
            name="deactivate",
            label="Deactivate selected",
            handler=deactivate_handler,
            confirm=True,
        ),
        Action(
            name="export_csv",
            label="Export as CSV",
            handler=export_csv_handler,
            confirm=False,
        ),
    ],
)
```

`Action` parameters:

- `name` -- unique identifier for the action (used in form submission)
- `label` -- human-readable label shown in the dropdown
- `handler` -- async function with signature `(config, request, selected_ids) -> str | Response`
- `confirm` -- if `True`, show a confirmation dialog before executing (default: `False`)

The handler receives:

- `config` -- the `ModelConfig` for the current model
- `request` -- the current `Request` object
- `selected_ids` -- list of primary key values (as strings)

The handler must return a `str` message (shown as a success toast after redirect back to the list view). If the handler returns a `Response` instead, it is sent directly.

The built-in `delete_selected` action is always included automatically. It respects the `confirm=True` setting and uses the admin's `confirm-delete` dialog.

## Save and Delete Hooks

Hooks run before save/delete operations. Use them for validation, auto-population, side effects, or audit logging.

### Save Hooks

```python
async def validate_price(values, is_edit):
    """Reject negative prices."""
    if values.get("price", 0) < 0:
        raise ValueError("Price cannot be negative")
    return values


async def slugify_name(values, is_edit):
    """Auto-generate slug from name on create or name change."""
    if not is_edit or "name" in values:
        values["slug"] = values.get("name", "").lower().replace(" ", "-")
    return values


async def set_updated_by(values, is_edit):
    """Track who last modified the record."""
    values["updated_by"] = "admin"  # In practice, get from request context
    return values


admin.register(Product, save_hooks=[validate_price, slugify_name, set_updated_by])
```

Save hooks are called in order. Each receives the form values dict and a boolean `is_edit` (True for updates, False for creates). Each must return the (possibly modified) values dict. Raising an exception aborts the save and shows an error.

### Delete Hooks

```python
async def cleanup_images(pk):
    """Remove associated files when a product is deleted."""
    # Query and delete image files
    pass


async def log_deletion(pk):
    """Audit log the deletion."""
    logger.info("Product {pk} deleted by admin", pk=pk)


admin.register(Product, delete_hooks=[cleanup_images, log_deletion])
```

Delete hooks receive the primary key value. They run before the DELETE query. Raising an exception aborts the deletion.

## HTMX Features

HyperAdmin includes built-in HTMX support for dynamic, SPA-like interactions without writing JavaScript. HTMX is loaded automatically in the admin base template.

### Partial List Reload

```
GET /admin/{model}/partial/?search=widget&filter_is_active=true&page=2
```

Returns just the table body HTML (no header, no footer). Used by the search bar and filter sidebar for instant updates without full page reload. The search input has `hx-get` with `hx-trigger="keyup changed delay:300ms"` for debounced search.

### Inline Validation

```
POST /admin/{model}/validate/
Body: {"field": "price", "value": "-5"}
Response: {"valid": false, "error": "Price must be >= 0"}
```

Individual field validation as the user fills out the form. Each input has `hx-post` with `hx-trigger="blur"` for validation on focus loss.

### Confirm Delete Dialog

```
GET /admin/{model}/{id}/confirm-delete/
```

Returns a modal dialog HTML fragment with the object summary and a confirmation button. The delete link uses `hx-get` with `hx-target="#modal"` to load the dialog.

### Autocomplete for Foreign Keys

```
GET /admin/{model}/autocomplete/?q=search&field=category_id&limit=20
```

Returns JSON results for FK field autocomplete. FK fields render as search inputs instead of full dropdowns when the related table has more than 50 rows.

Response format:

```json
{
  "results": [
    { "id": 1, "text": "Electronics" },
    { "id": 2, "text": "Clothing" }
  ]
}
```

The autocomplete endpoint validates that the `field` parameter refers to an actual FK column and that the target table is registered in the admin (preventing enumeration of unregistered tables).

### Inline Row Add

```
GET /admin/{model}/inline-row/?inline=0&index=5
```

Returns a new empty form row for the specified inline at the given index. The "Add another" button uses `hx-get` to append rows dynamically.

### Toast Messages

Success and error notifications appear as toast messages in the top-right corner. They auto-dismiss after 5 seconds. Toasts use `hx-swap-oob` for out-of-band insertion.

```html
<!-- Toast HTML (auto-injected) -->
<div id="toast" class="toast toast-success" hx-swap-oob="true">
  Product "Widget" saved successfully.
</div>
```

### List Editable Save

When `list_editable` fields are present, the list view table is wrapped in a form. The "Save" button submits all changed values via `hx-post` to `/admin/{model}/list-save/`, which updates all modified rows in a single transaction.

## RBAC Integration in Admin

HyperAdmin integrates with HyperDjango's hierarchical RBAC system. When RBAC models are registered, admin views enforce permissions at every level.

### Permission Checks

Every admin action checks permissions:

- **List view**: requires `view` permission on the model
- **Add**: requires `add` permission
- **Change**: requires `change` permission
- **Delete**: requires `delete` permission
- **Actions**: checked against the action's implied permission

Permissions are resolved through the full RBAC hierarchy (user permissions, group permissions, inherited permissions via CTE).

### Field-Level Access

When field-level permissions are configured, the admin respects them:

- `hidden` fields are not shown at all
- `readonly` fields are shown but not editable (same as `readonly_fields`)
- `writable` fields are fully editable

```python
# In RBAC configuration:
# FieldPermission(model="product", field="cost_price", role="viewer", access="hidden")
# FieldPermission(model="product", field="cost_price", role="editor", access="readonly")
# FieldPermission(model="product", field="cost_price", role="admin", access="writable")
```

### Object-Level Permissions

When object-level permissions are configured, users only see objects they have access to in list views, and can only edit/delete objects they have permission for.

## register_auth_models()

Register all RBAC models for self-managing admin. This is the single call that gives you complete user/group/permission management.

```python
admin.register_auth_models()

# This registers:
# - Users (with password change hook, escalation guard)
# - Groups (with parent hierarchy display)
# - Permissions
# - UserGroups (inline on User form)
# - GroupPermissions (inline on Group form)
# - UserPermissions (inline on User form)
# - ObjectPermissions
# - PermissionRules (5 rule types: ip_range, time_range, attribute, rate_limit, custom)
# - FieldPermissions
```

### Escalation Guard

Non-superuser staff cannot escalate privileges through the admin:

- Cannot create superuser or staff accounts
- Cannot set `is_superuser = True` or `is_staff = True` on existing users
- Cannot modify their own permission level
- The guard silently preserves original values on edit attempts (no error, just no change)

This prevents a common attack vector where a staff user with admin access creates a superuser account or escalates their own privileges.

### RBAC Management Views

After calling `register_auth_models()`, additional management routes are available:

| Route                              | Description                                                                                   |
| ---------------------------------- | --------------------------------------------------------------------------------------------- |
| `/admin/rbac/`                     | RBAC dashboard with summary stats (user count, group count, permission count, orphaned perms) |
| `/admin/rbac/effective/{user_id}/` | Effective permissions for a user (resolved through full hierarchy)                            |
| `/admin/rbac/check/`               | Permission checker tool: test "can user X do Y on object Z?"                                  |
| `/admin/rbac/audit/`               | RBAC audit log (who changed what permission, when)                                            |
| `/admin/rbac/export/`              | Export full RBAC policy as JSON, or import a policy (merge or replace)                        |
| `/admin/rbac/tree/`                | Group hierarchy tree view (visual parent-child relationships)                                 |

### RBAC Dashboard

The dashboard at `/admin/rbac/` shows:

- Total users, groups, permissions
- Active sessions count
- Orphaned permissions (permissions referencing deleted objects)
- Permission coverage (percentage of models with explicit permissions)
- Recent RBAC changes (last 50 audit log entries)

## register_cache_dashboard()

Register the cache monitoring dashboard at `/admin/cache/`. Displays real-time
stats from all cache subsystems configured in the app.

```python
admin.register_cache_dashboard()
```

This registers two routes:

| Route               | Description                                                     |
| ------------------- | --------------------------------------------------------------- |
| `/admin/cache/`     | HTML dashboard — auto-refreshes every 5 seconds                 |
| `/admin/cache/json` | JSON API returning the same stats (for programmatic monitoring) |

The dashboard reads from three sources:

**1. Query cache (`get_query_cache()`)**:

- Hit rate, hits, misses, total requests
- Per-table version counters (shown as a table)
- Invalidation counters (table-level, row-level, total)
- Set counter (how many compiled SQLs were stored)

**2. General cache (`get_cache()`)**:

- If `LocMemCache` — entry count, max size, utilization percentage
- If `TwoTierCache` — L1 hit rate, L2 hit rate, overall hit rate (rendered as bars)
- If `DatabaseCache` — stats shown via query cache layer only

**3. Overall stats card row**:

- Query cache hit rate
- Total requests
- Invalidations
- Tables tracked

Example in context (typical app setup):

```python
from hyperdjango.admin import HyperAdmin
from hyperdjango.cache import LocMemCache, set_cache
from hyperdjango.cache_adapters import TwoTierCache

# Set up a two-tier cache so the dashboard has L1/L2 stats to show
l1 = LocMemCache(max_size=256)
l2 = LocMemCache(max_size=2048)  # or DatabaseCache for multi-server
set_cache(TwoTierCache(l1, l2, l1_ttl=10))

admin = HyperAdmin(app, prefix="/admin", secret_key="...")
admin.register_auth_models()
admin.register_ratelimit_models()
admin.register_cache_dashboard()  # → /admin/cache/
```

**JSON endpoint** (`/admin/cache/json`) returns a serializable dict:

```json
{
  "query_cache": {
    "hits": 51234,
    "misses": 892,
    "total_requests": 52126,
    "hit_rate": "98.3%",
    "hit_rate_float": 0.9829,
    "invalidations": 142,
    "table_invalidations": 140,
    "row_invalidations": 2,
    "sets": 47,
    "table_count": 23
  },
  "table_versions": [
    { "name": "bk_books", "version": 12 },
    { "name": "bk_reviews", "version": 4 }
  ],
  "two_tier": {
    "l1_hits": 48201,
    "l2_hits": 3033,
    "misses": 892,
    "total_requests": 52126,
    "l1_hit_rate": 0.9247,
    "l2_hit_rate": 0.0582,
    "overall_hit_rate": 0.9829,
    "l1_hit_rate_pct": 92,
    "l2_hit_rate_pct": 5,
    "overall_hit_rate_pct": 98
  },
  "locmem": null
}
```

Use the JSON endpoint to wire the dashboard into external monitoring (any
Prometheus-compatible scraper) without HTML scraping.

## Authentication

HyperAdmin uses database-backed sessions with argon2id password hashing. No JWT.

### Login Flow

```
GET  /admin/login/     -> login page (HTML form)
POST /admin/login/     -> authenticate, create session, redirect to dashboard
GET  /admin/logout/    -> destroy session, redirect to login
```

Sessions are stored in a PostgreSQL UNLOGGED table for performance. Session cookies are signed with HMAC-SHA256 using the `secret_key`.

Password verification uses argon2id (memory-hard, side-channel resistant). Passwords are never stored in plaintext. The admin login page includes CSRF protection.

### Creating Admin Users

```bash
uv run hyper createsuperuser
```

Or programmatically:

```python
from hyperdjango.auth import User

user = User(
    username="admin", email="admin@example.com", is_staff=True, is_superuser=True
)
user.set_password("secure-password")
await user.save(db)
```

## Admin Template Customization

HyperAdmin uses a 3-level template cascade for customization:

1. **Filesystem templates** (highest priority): templates in your project's `templates/admin/` directory
2. **Per-registration overrides**: `list_template` and `form_template` kwargs
3. **Built-in defaults** (lowest priority): HyperAdmin's bundled templates

### Overriding Templates

Create files in `templates/admin/` to override any admin template:

```
templates/
  admin/
    base.html           # Override the base layout (header, nav, footer)
    list.html            # Override all list views
    form.html            # Override all forms
    product/
      list.html          # Override only the Product list view
      form.html          # Override only the Product form
    login.html           # Override the login page
    dashboard.html       # Override the admin dashboard
```

### Per-Model Template Override

```python
admin.register(
    Product,
    list_template="admin/product/custom_list.html",
    form_template="admin/product/custom_form.html",
)
```

### Extra CSS and JavaScript

Add custom stylesheets and scripts to admin pages for a specific model:

```python
admin.register(
    Product,
    media_css=["admin/css/product.css", "admin/css/charts.css"],
    media_js=["admin/js/product.js", "admin/js/price-calculator.js"],
)
```

CSS files are loaded in `<head>`, JS files are loaded at the end of `<body>`. Paths are relative to the static files directory.

### Admin JavaScript

HyperAdmin includes built-in JavaScript for:

- **Prepopulated fields**: auto-fill slug fields on keyup
- **Inline management**: add/remove inline rows via HTMX
- **Autocomplete**: FK search-as-you-type
- **Collapsible fieldsets**: toggle visibility of collapsed sections
- **Select all checkbox**: select/deselect all rows for bulk actions
- **Confirmation dialogs**: confirm before destructive actions
- **Toast dismissal**: auto-hide and manual close for toast messages

All admin JS is vanilla JavaScript with HTMX -- no jQuery, no React, no build step.

## Admin Audit Log

Every admin action is automatically logged for accountability:

```python
# Audit log records include:
# - action: "add", "change", "delete"
# - model: "product"
# - object_id: "42"
# - user: "admin"
# - timestamp: "2026-03-26T14:30:00Z"
# - changes: {"price": {"old": 9.99, "new": 14.99}}  # JSON diff for edits
```

View audit history:

- **Per-object**: `/admin/{model}/{id}/history/` shows all changes to that object
- **Per-user**: available through the RBAC audit view at `/admin/rbac/audit/`
- **Global**: the admin dashboard shows recent activity

## Admin Query Acceleration

HyperAdmin automatically optimizes database queries for list views:

- **Auto-prefetch**: FK fields are resolved with a single JOIN instead of N+1 queries
- **Nested FK resolution**: multi-level FK chains (e.g., `order.customer.company.name`) are resolved efficiently
- **M2M optimization**: Many-to-many fields use prefetch queries
- **Benchmarked**: 17.7x faster changelist rendering, 41x fewer queries compared to naive N+1

This optimization is automatic and requires no configuration.

## Complete Example

```python
from hyperdjango import HyperApp, Model, Field
from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.fields import Fieldset, Action, InlineConfig

app = HyperApp(title="E-Commerce", database="postgres://localhost/shop")


class Category(Model):
    class Meta:
        table = "categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    slug: str = Field()


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    price: float = Field(ge=0)
    cost_price: float = Field(ge=0)
    category_id: int = Field(foreign_key=Category)
    is_active: bool = Field(default=True)
    stock_count: int = Field(default=0)
    created_at: str = Field(auto_now_add=True)


class ProductImage(Model):
    class Meta:
        table = "product_images"

    id: int = Field(primary_key=True, auto=True)
    product_id: int = Field(foreign_key=Product)
    url: str = Field()
    alt_text: str = Field(default="")
    sort_order: int = Field(default=0)


async def validate_product(values, is_edit):
    if values.get("price", 0) < values.get("cost_price", 0):
        raise ValueError("Price cannot be less than cost price")
    return values


async def bulk_activate(config, request, selected_ids):
    db = request.app.db
    await db.execute(
        "UPDATE products SET is_active = TRUE WHERE id = ANY($1)",
        [int(pk) for pk in selected_ids],
    )


admin = HyperAdmin(app, prefix="/admin", secret_key="change-me-in-prod")
admin.register_auth_models()

admin.register(
    Category,
    list_display=["name", "slug"],
    search_fields=["name"],
    prepopulated_fields={"slug": ("name",)},
)

admin.register(
    Product,
    list_display=["name", "price", "category_id", "is_active", "stock_count"],
    list_filter=["is_active", "category_id"],
    search_fields=["name", "description"],
    ordering="-created_at",
    list_per_page=50,
    list_editable=["price", "is_active", "stock_count"],
    date_hierarchy="created_at",
    readonly_fields=["created_at"],
    fieldsets=[
        Fieldset(title="Product Info", fields=["name", "description", "category_id"]),
        Fieldset(title="Pricing", fields=["price", "cost_price"]),
        Fieldset(title="Inventory", fields=["stock_count", "is_active"]),
    ],
    actions=[
        Action(
            name="activate",
            label="Activate selected",
            handler=bulk_activate,
            confirm=True,
        ),
    ],
    save_hooks=[validate_product],
    inlines=[
        InlineConfig(
            model_class=ProductImage,
            fk_field="product_id",
            fields=["url", "alt_text", "sort_order"],
            extra=1,
            ordering="sort_order",
        ),
    ],
)
```

## Dark Mode + Custom Themes

HyperAdmin ships with built-in light and dark themes. The dark theme activates automatically via `prefers-color-scheme: dark` and can be toggled manually with a button in the admin header. The toggle state persists in `localStorage`.

### ThemeConfig

Custom themes are defined with the `ThemeConfig` dataclass:

```python
from hyperdjango.admin.fields import ThemeConfig
```

`ThemeConfig` is a frozen, slotted dataclass with these fields:

| Field      | Type             | Description                                                   |
| ---------- | ---------------- | ------------------------------------------------------------- |
| `name`     | `str`            | Unique identifier (e.g., `"brand"`, `"solarized"`)            |
| `label`    | `str`            | Human-readable label for the theme toggle menu                |
| `css_vars` | `dict[str, str]` | CSS variable overrides merged with the base theme             |
| `is_dark`  | `bool`           | Whether this is a dark variant (affects auto-detection logic) |

CSS variables are merged on top of the base theme -- only override the variables you want to change.

### Registering Themes

```python
admin.register_theme(
    ThemeConfig(
        name="brand",
        label="Brand Purple",
        css_vars={
            "--primary": "#7c3aed",
            "--btn-hover": "#6d28d9",
        },
    )
)
```

### Generating Theme CSS

```python
css = admin.get_theme_css("brand")
# Returns: [data-theme="brand"] { --primary: #7c3aed; --btn-hover: #6d28d9; }
```

Returns an empty string if the theme name is not registered.

### Listing Registered Themes

```python
themes = admin.registered_themes  # list[ThemeConfig]
```

### CSS Variable Reference

The following CSS variables are available for override in custom themes:

| Variable      | Description                           |
| ------------- | ------------------------------------- |
| `--bg`        | Page background color                 |
| `--card`      | Card and panel background             |
| `--primary`   | Primary accent color (buttons, links) |
| `--text`      | Main text color                       |
| `--border`    | Border and separator color            |
| `--muted`     | Muted/secondary text color            |
| `--btn-hover` | Button hover state color              |

### Custom Theme Example

A high-contrast accessibility theme:

```python
from hyperdjango.admin.fields import ThemeConfig

high_contrast = ThemeConfig(
    name="high-contrast",
    label="High Contrast",
    is_dark=True,
    css_vars={
        "--bg": "#000000",
        "--card": "#1a1a1a",
        "--primary": "#ffff00",
        "--text": "#ffffff",
        "--border": "#ffffff",
        "--muted": "#cccccc",
        "--btn-hover": "#cccc00",
    },
)

admin.register_theme(high_contrast)
```

The theme toggle in the admin header automatically includes all registered themes alongside the built-in light and dark options.

---

## Dynamic Per-Request Hooks

These hooks let you customize admin behavior based on who's logged in, what they're editing, or any request-specific criteria. They're the most powerful customization surface — matching Django's `get_queryset`, `get_readonly_fields`, etc.

### get_queryset — Row-Level Filtering

Filter which rows a user can see and edit. Common for tenant-scoped admin or role-based visibility:

```python
async def project_queryset(request):
    if request.user.is_superuser:
        return None  # superuser sees everything
    return {"owner_id": request.user.id}  # others see only their own


admin.register(
    Project,
    list_display=["id", "name", "owner_id"],
    get_queryset=project_queryset,
)
```

The returned dict is applied as extra WHERE conditions on list, edit, and delete views. Returning `None` means no filter. Column names are validated against the model schema.

### get_readonly_fields — Dynamic Readonly

Make fields readonly based on object state or user role:

```python
def article_readonly(request, obj):
    if obj is not None:  # editing existing article
        return ["slug", "author_id"]  # can't change slug or author after creation
    return []  # all fields editable on add


admin.register(
    Article,
    get_readonly_fields=article_readonly,
)
```

Dynamic readonly fields are merged with static `readonly_fields`. The `obj` parameter is the existing row dict on edit (None on add).

### get_fieldsets — Dynamic Field Groups

Show different field layouts for add vs edit, or based on user role:

```python
def post_fieldsets(request, obj):
    base = [Fieldset(title="Content", fields=["title", "body"])]
    if obj is not None:
        base.append(Fieldset(title="Metadata", fields=["status", "published_at"]))
    if request.user.is_superuser:
        base.append(Fieldset(title="Admin", fields=["featured", "pinned"]))
    return base


admin.register(Post, get_fieldsets=post_fieldsets)
```

When set, overrides the static `fieldsets` parameter. Fields not covered by any fieldset appear in an "Other" group.

### get_list_display — Dynamic Columns

Show different columns based on user permissions:

```python
def employee_columns(request):
    cols = ["id", "name", "email", "department"]
    if request.user.has_perm("view_salary"):
        cols.append("salary")
    return cols


admin.register(Employee, get_list_display=employee_columns)
```

### get_search_results — Custom Search

Override the default ILIKE prefix search with custom logic (e.g., full-text search):

```python
async def post_search(request, base_conditions, search_term):
    return {"search_vector__search": search_term}


admin.register(Post, get_search_results=post_search)
```

### get_form — Dynamic Form Configuration

Use different fields or required constraints for add vs edit:

```python
def user_form(request, obj):
    if obj is None:  # adding new user
        return {"fields": ["username", "email", "password"]}
    else:  # editing existing
        return {"fields": ["email", "display_name", "bio"]}


admin.register(User, get_form=user_form)
```

### Enriched Save/Delete Hooks

Save and delete hooks now support a richer 4-parameter signature with request context and existing object:

```python
# New-style (4 params): request, values dict, is_edit flag, existing object
async def audit_save(request, values, is_edit, obj):
    values["modified_by"] = request.user.id
    if is_edit and obj:
        logger.info(f"User {request.user.username} editing {obj.get('title')}")
    return values


# Old-style (2 params) still works — arity detected automatically
async def legacy_save(values, is_edit):
    return values


# Delete hooks: new-style (3 params) or old-style (1 param)
async def audit_delete(request, pk, obj):
    logger.info(f"User {request.user.username} deleting #{pk}: {obj.get('title')}")


admin.register(Post, save_hooks=[audit_save], delete_hooks=[audit_delete])
```

---

## View-Only Mode

Allow users to view admin data without edit permission:

```python
admin.register(
    AuditLog,
    list_display=["timestamp", "action", "user"],
    can_view=True,  # users can see the list and detail views
    # can_add/change/delete controlled by RBAC permissions
)
```

When a user has `can_view` permission but not `can_change`, the edit form renders with all fields readonly and no save/delete buttons.

---

## List Display Links

Control which columns in the list view link to the edit form:

```python
# Both id and title are clickable links to the edit form
admin.register(
    Post,
    list_display=["id", "title", "status", "created_at"],
    list_display_links=["id", "title"],
)

# No links at all (view-only list, no edit navigation)
admin.register(
    AuditLog,
    list_display=["timestamp", "action"],
    list_display_links=False,
)
```

Default: first column is the link (like Django).

---

## Custom Model Actions

Add custom URL endpoints under a model's admin section:

```python
@admin.model_action("order", "ship", method="POST")
async def ship_order(request, id):
    await Order.objects.filter(id=int(id)).update(status="shipped")
    return Response.redirect(f"/admin/order/{id}/")


@admin.model_action("report", "export", method="GET")
async def export_reports(request):
    data = await Report.objects.all()
    return Response.json([r.to_dict() for r in data])
```

Routes: `{prefix}/{slug}/{pk}/{action}/` (if handler has `pk`/`id` param) or `{prefix}/{slug}/{action}/` (no pk). All auth-wrapped.

---

## Post-Save Redirects

Control where the user goes after saving:

```python
admin.register(
    Post,
    response_add="continue",  # stay on edit form after creating
    response_change="list",  # back to list after editing (default)
    response_delete="list",  # back to list after deleting (default)
)

# Or use a callable for custom redirect logic:
admin.register(
    Post,
    response_add=lambda request, pk: f"/posts/{pk}/preview/",
)
```

Values: `"list"` (default), `"continue"` (stay on edit form), `"add"` (add another), or a callable `(request, pk) -> url`.

---

## View on Site

Show a link from the admin edit form to the object's public-facing URL:

```python
admin.register(
    Post,
    view_on_site=lambda obj: f"/posts/{obj['id']}",
)
```

Renders a "View on site" link above the save buttons on the edit form.

---

## Save As (Duplicate Objects)

Add a "Save as new" button to create a copy of an existing object:

```python
admin.register(EmailTemplate, save_as=True)
```

On the edit form, clicking "Save as new" creates a new object with the modified field values (auto-increment PK is assigned fresh).

---

## Form UX Options

```python
admin.register(
    Article,
    save_on_top=True,  # save buttons at top AND bottom of long forms
    show_full_result_count=False,  # skip total count on filtered list (perf win on large tables)
    empty_value_display="(not set)",  # customize NULL/empty display (default: "-")
)
```

---

## Radio Fields

Render choice/enum fields as radio buttons instead of select dropdowns:

```python
admin.register(
    Post,
    radio_fields={"status": "horizontal", "priority": "vertical"},
)
```

---

## Raw ID and Autocomplete Fields

Control FK widget behavior per field:

```python
admin.register(
    Comment,
    raw_id_fields=["post_id"],  # plain number input, no autocomplete
    autocomplete_fields=["author_id"],  # only this FK gets autocomplete
)
```

`raw_id_fields`: suppresses the autocomplete widget, renders a plain number input. Useful for FKs with millions of rows where autocomplete is impractical.

`autocomplete_fields`: when set, only the listed FK fields get HTMX autocomplete. Others get plain number inputs. Default (`None`) = all FK fields get autocomplete.

---

## @display Decorator

Set display properties on list_display callables:

```python
from hyperdjango.admin import display


@display(description="Full Name", ordering="last_name")
def full_name(obj):
    return f"{obj['first_name']} {obj['last_name']}"


@display(description="Active?", boolean=True, empty_value="N/A")
def is_active(obj):
    return obj.get("is_active", False)


admin.register(
    User,
    list_display=["id", "full_name", "is_active", "email"],
    list_display_callables={"full_name": full_name, "is_active": is_active},
)
```

- `description`: column header text (default: function name title-cased)
- `ordering`: DB column to sort by when clicking the column header
- `boolean`: render True/False as yes/no icons
- `empty_value`: display for None/empty return values

---

## Post-Action Hooks

Run async callbacks after database writes and audit logging:

```python
async def notify_on_create(request, values):
    await send_notification(
        f"New {values.get('title')} created by {request.user.username}"
    )


async def cleanup_on_delete(request, pk):
    await remove_cached_data(pk)


admin.register(
    Post,
    on_add=notify_on_create,
    on_change=None,  # no callback after edit
    on_delete=cleanup_on_delete,
)
```

---

## Inline Enhancements

```python
from hyperdjango.admin.fields import InlineConfig

admin.register(
    Author,
    inlines=[
        InlineConfig(
            model_class=Book,
            fields=["title", "year"],
            show_change_link=True,  # link to full edit form per inline row
            classes=["collapse"],  # collapsed by default, click header to expand
        ),
    ],
)
```

---

## M2M Dual-Select Widget (filter_horizontal)

Edit many-to-many relationships with a dual-pane "Available / Chosen" widget:

```python
from hyperdjango.models import ManyToManyField


class Tag(Model):
    class Meta:
        table = "tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=50)


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)


# Declare M2M relationship
Article.tags = ManyToManyField(Tag)
Article.tags._configure(Article, "tags")

# Register with filter_horizontal
admin.register(
    Article,
    list_display=["id", "title"],
    filter_horizontal=["tags"],  # renders dual-select widget on edit form
)
```

The widget shows two panes: "Available" (all items not yet selected) and "Chosen" (currently selected). Arrow buttons move items between panes. On save, the junction table is synced atomically (DELETE all + INSERT selected).

The widget auto-detects a display column on the target model (tries `name`, `title`, `username`, `label`, `codename` in order, falls back to PK). Limited to 500 available items per field.

---

## Sortable Columns

Restrict which columns show sort arrows in the list view:

```python
admin.register(
    Post,
    list_display=["id", "title", "computed_score", "created_at"],
    sortable_by=["id", "title", "created_at"],  # computed_score not sortable
)
```

---

## Preserve Filters

After editing an object and returning to the list, preserve the active search, filters, sort, and page:

```python
admin.register(Post, preserve_filters=True)  # default: True
```
