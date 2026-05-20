# HyperAdmin Guide

This guide covers setting up and customizing HyperAdmin, the auto-generated
CRUD admin interface for HyperApp models. HyperAdmin introspects your models
and generates list, create, edit, and delete views with zero boilerplate.

For the API reference, see [admin.md](admin.md).

---

## Table of Contents

- [Setup](#setup)
- [Model Registration](#model-registration)
- [ModelConfig Options](#modelconfig-options)
- [Fieldsets](#fieldsets)
- [List View Customization](#list-view-customization)
- [Search and Filtering](#search-and-filtering)
- [Inlines](#inlines)
- [Custom Actions](#custom-actions)
- [Save and Delete Hooks](#save-and-delete-hooks)
- [Themes and Dark Mode](#themes-and-dark-mode)
- [RBAC Management UI](#rbac-management-ui)
- [Django Admin Bridge](#django-admin-bridge)
- [Migration from Django Admin](#migration-from-django-admin)

---

## Setup

HyperAdmin is standalone -- it uses HyperApp's database layer (pg.zig) and
the Zig template engine directly. No Django dependency.

```python
from hyperdjango import HyperApp, Model, Field
from hyperdjango.admin import HyperAdmin

app = HyperApp(
    title="My App",
    database="postgres://localhost/mydb",
    templates="templates",
)

admin = HyperAdmin(app, prefix="/admin")
```

This auto-registers routes under `/admin/`:

| Method | Path                          | View           |
| ------ | ----------------------------- | -------------- |
| GET    | `/admin/`                     | Dashboard      |
| GET    | `/admin/<model>/`             | List view      |
| GET    | `/admin/<model>/add/`         | Create form    |
| POST   | `/admin/<model>/add/`         | Create handler |
| GET    | `/admin/<model>/<id>/`        | Edit form      |
| POST   | `/admin/<model>/<id>/`        | Update handler |
| POST   | `/admin/<model>/<id>/delete/` | Delete handler |

The admin also includes an authentication layer with login/logout views,
using argon2id password hashing and PostgreSQL UNLOGGED session tables.

---

## Model Registration

Register models to make them available in the admin:

```python
class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    price: Decimal = Field(ge=0)
    stock: int = Field(ge=0, default=0)
    category: str = Field()
    is_active: bool = Field(default=True)
    created_at: datetime = Field(auto_now_add=True)


class Order(Model):
    class Meta:
        table = "orders"

    id: int = Field(primary_key=True, auto=True)
    customer_email: str = Field()
    total: Decimal = Field(ge=0)
    status: str = Field(default="pending")
    created_at: datetime = Field(auto_now_add=True)


# Simple registration (auto-introspect everything)
admin.register(Product)
admin.register(Order)
```

After registration, visit `/admin/` to see the dashboard listing both models
with links to their list views.

---

## ModelConfig Options

For fine-grained control, pass a `ModelConfig` when registering:

```python
from hyperdjango.admin.fields import ModelConfig

admin.register(
    Product,
    ModelConfig(
        list_display=["name", "price", "stock", "category", "is_active"],
        list_filter=["category", "is_active"],
        search_fields=["name", "description"],
        ordering=["-created_at"],
        list_per_page=50,
        readonly_fields=["created_at"],
        list_editable=["price", "stock", "is_active"],
    ),
)
```

Full `ModelConfig` options:

| Option            | Type                 | Description                                    |
| ----------------- | -------------------- | ---------------------------------------------- |
| `list_display`    | `list[str]`          | Columns shown in the list view                 |
| `list_filter`     | `list[str]`          | Sidebar filter fields                          |
| `search_fields`   | `list[str]`          | Fields searched by the search box              |
| `ordering`        | `list[str]`          | Default sort order (`-` prefix for descending) |
| `list_per_page`   | `int`                | Rows per page (default 25)                     |
| `readonly_fields` | `list[str]`          | Fields displayed but not editable              |
| `list_editable`   | `list[str]`          | Fields editable directly in the list view      |
| `exclude`         | `list[str]`          | Fields hidden from forms                       |
| `fieldsets`       | `list[Fieldset]`     | Group fields into named sections               |
| `inlines`         | `list[InlineConfig]` | Inline editing of related models               |
| `actions`         | `list[Action]`       | Bulk actions for selected rows                 |
| `save_hooks`      | `list[Callable]`     | Functions called after save                    |
| `delete_hooks`    | `list[Callable]`     | Functions called after delete                  |

---

## Fieldsets

Group related fields into collapsible sections on the edit form:

```python
from hyperdjango.admin.fields import Fieldset, ModelConfig

admin.register(
    Product,
    ModelConfig(
        fieldsets=[
            Fieldset(
                name="Basic Information",
                fields=["name", "description", "category"],
            ),
            Fieldset(
                name="Pricing & Inventory",
                fields=["price", "stock"],
                description="Manage product pricing and stock levels.",
            ),
            Fieldset(
                name="Status",
                fields=["is_active", "created_at"],
                classes=["collapse"],  # Collapsed by default
            ),
        ],
        readonly_fields=["created_at"],
    ),
)
```

Each `Fieldset` accepts:

- `name` -- section heading
- `fields` -- list of field names in this section
- `description` -- optional help text below the heading
- `classes` -- CSS classes (e.g., `["collapse"]` for collapsible sections)

---

## List View Customization

### Callable Columns

Use functions for computed columns in the list view:

```python
def stock_status(obj):
    if obj.stock == 0:
        return "Out of Stock"
    if obj.stock < 10:
        return "Low Stock"
    return "In Stock"


admin.register(
    Product,
    ModelConfig(
        list_display=["name", "price", stock_status, "is_active"],
    ),
)
```

### Sortable Columns

All fields listed in `list_display` are sortable by clicking the column
header. The current sort is indicated by an arrow icon. Click again to
reverse the sort direction.

### List Editable

Fields in `list_editable` render as inline form inputs directly in the list
view. Users can edit multiple rows and save them all at once:

```python
admin.register(
    Product,
    ModelConfig(
        list_display=["name", "price", "stock", "is_active"],
        list_editable=["price", "stock", "is_active"],
    ),
)
```

This renders price and stock as number inputs and is_active as a checkbox
in each row, with a "Save" button at the bottom of the list.

---

## Search and Filtering

### Search

The `search_fields` option adds a search box that queries across multiple
fields using `ILIKE`:

```python
admin.register(
    Order,
    ModelConfig(
        search_fields=["customer_email", "id"],
    ),
)
```

HyperAdmin automatically creates `varchar_pattern_ops` indexes on searchable
text fields for efficient prefix matching.

### Filtering

The `list_filter` option adds a sidebar with clickable filter values:

```python
admin.register(
    Order,
    ModelConfig(
        list_filter=["status", "created_at"],
    ),
)
```

For choice-based fields, the filter shows all distinct values. For date
fields, it shows ranges (Today, Past 7 days, This month, This year).

---

## Inlines

Edit related models directly within a parent model's edit page:

```python
from hyperdjango.admin.fields import InlineConfig


class OrderItem(Model):
    class Meta:
        table = "order_items"

    id: int = Field(primary_key=True, auto=True)
    order_id: int = Field(foreign_key=Order)
    product_name: str = Field()
    quantity: int = Field(ge=1)
    unit_price: Decimal = Field(ge=0)


admin.register(
    Order,
    ModelConfig(
        list_display=["id", "customer_email", "total", "status", "created_at"],
        list_filter=["status"],
        inlines=[
            InlineConfig(
                model=OrderItem,
                fk_field="order_id",  # FK pointing to Order
                fields=["product_name", "quantity", "unit_price"],
                extra=1,  # 1 blank row for new items
                max_num=50,  # Maximum inline rows
            ),
        ],
    ),
)
```

The inline section appears below the parent form. New rows can be added
dynamically via HTMX without a full page reload. Each row has a delete
checkbox.

### Multiple Inlines

You can attach multiple inlines to a single model:

```python
admin.register(
    Order,
    ModelConfig(
        inlines=[
            InlineConfig(model=OrderItem, fk_field="order_id"),
            InlineConfig(model=OrderNote, fk_field="order_id", extra=0),
            InlineConfig(
                model=OrderStatusHistory, fk_field="order_id", readonly=True, extra=0
            ),
        ],
    ),
)
```

---

## Custom Actions

Bulk actions appear as a dropdown above the list view. Select rows with
checkboxes, choose an action, and execute:

```python
from hyperdjango.admin.fields import Action


async def mark_active(admin_instance, request, queryset):
    count = await queryset.update(is_active=True)
    return f"Marked {count} products as active"


async def export_csv(admin_instance, request, queryset):
    items = await queryset.all()
    csv_lines = ["name,price,stock"]
    for item in items:
        csv_lines.append(f"{item.name},{item.price},{item.stock}")
    return Response.text(
        "\n".join(csv_lines),
        headers={
            "content-type": "text/csv",
            "content-disposition": 'attachment; filename="products.csv"',
        },
    )


admin.register(
    Product,
    ModelConfig(
        actions=[
            Action(name="mark_active", label="Mark as Active", handler=mark_active),
            Action(
                name="mark_inactive", label="Mark as Inactive", handler=mark_inactive
            ),
            Action(name="export_csv", label="Export to CSV", handler=export_csv),
        ],
    ),
)
```

---

## Save and Delete Hooks

Run custom logic after an object is saved or deleted:

```python
async def on_product_save(instance, created):
    """Called after a product is saved in the admin."""
    if created:
        await notify_team(f"New product: {instance.name}")
    else:
        await invalidate_product_cache(instance.id)


async def on_product_delete(instance):
    """Called after a product is deleted."""
    await cleanup_product_images(instance.id)
    await audit_log("product_deleted", instance.id)


admin.register(
    Product,
    ModelConfig(
        save_hooks=[on_product_save],
        delete_hooks=[on_product_delete],
    ),
)
```

Save hooks receive `(instance, created)` where `created` is `True` for
new objects. Delete hooks receive `(instance,)`.

---

## Themes and Dark Mode

HyperAdmin supports theming via `ThemeConfig` with CSS variable overrides
and automatic dark mode detection:

```python
from hyperdjango.admin.fields import ThemeConfig

admin = HyperAdmin(
    app,
    prefix="/admin",
    theme=ThemeConfig(
        primary_color="#2563eb",
        secondary_color="#7c3aed",
        success_color="#059669",
        danger_color="#dc2626",
        dark_mode=True,  # Enable dark mode toggle
        custom_css="body { font-family: 'Inter', sans-serif; }",
    ),
)
```

The dark mode toggle appears in the admin header. It uses
`prefers-color-scheme` for automatic detection and stores the preference
in a cookie. All admin CSS uses CSS custom properties, so theme changes
apply instantly without page reload.

### CSS Variable Reference

The theme system exposes these CSS variables:

```css
--admin-primary: #2563eb;
--admin-secondary: #7c3aed;
--admin-success: #059669;
--admin-danger: #dc2626;
--admin-bg: #ffffff;
--admin-text: #1f2937;
--admin-border: #e5e7eb;
--admin-sidebar-bg: #f9fafb;
```

In dark mode, these switch automatically to their dark variants.

---

## RBAC Management UI

HyperAdmin includes a full role-based access control management interface.
When RBAC models (User, Group, Permission, Role) are registered, the admin
provides:

- **User management**: Create/edit users, assign groups and permissions,
  password management with argon2id hashing
- **Group hierarchy tree**: Visual tree of group inheritance (computed via CTE)
- **Effective permissions viewer**: See the resolved permissions for any user,
  including inherited permissions from groups and roles
- **Permission checker**: Test whether a user has a specific permission,
  with an explanation of how it was granted or denied
- **Audit log**: View a chronological log of all admin actions (add, change,
  delete) with JSON diffs of what changed

```python
from hyperdjango.auth import User, Group, Permission

admin.register(User)
admin.register(Group)
admin.register(Permission)
# RBAC views are auto-generated:
#   /admin/user/<id>/effective-perms/
#   /admin/permission-checker/
#   /admin/group-tree/
```

### Privilege Escalation Guard

The admin prevents users from granting permissions they do not themselves
possess. If user A has permissions `[read, write]`, they cannot assign
the `delete` permission to user B.

---

## Django Admin Bridge

If you are running HyperDjango alongside Django, you can use the Django admin
bridge to serve Django's admin through the Zig HTTP server:

```python
# settings.py
INSTALLED_APPS = ["hyperdjango.serving", "django.contrib.admin", ...]
```

This gives you Django's full admin interface served through HyperDjango's
native HTTP server with connection pooling via pg.zig.

---

## Migration from Django Admin

| Django Admin                      | HyperAdmin                              |
| --------------------------------- | --------------------------------------- |
| `admin.site.register(Model)`      | `admin.register(Model)`                 |
| `ModelAdmin` class                | `ModelConfig` dataclass                 |
| `list_display`                    | Same                                    |
| `list_filter`                     | Same                                    |
| `search_fields`                   | Same                                    |
| `ordering`                        | Same                                    |
| `readonly_fields`                 | Same                                    |
| `fieldsets` (tuple format)        | `Fieldset` dataclass (cleaner syntax)   |
| `TabularInline` / `StackedInline` | `InlineConfig` (unified)                |
| `actions` (function list)         | `Action` dataclass                      |
| `save_model()` / `delete_model()` | `save_hooks` / `delete_hooks` lists     |
| `django.contrib.auth`             | Built-in RBAC (argon2id, not pbkdf2)    |
| `has_add_permission()` etc.       | RBAC with hierarchical role inheritance |
| `AdminSite` with custom URLs      | `HyperAdmin(app, prefix=...)`           |

Key differences:

- HyperAdmin uses argon2id for passwords, not pbkdf2
- Sessions are stored in PostgreSQL UNLOGGED tables, not signed cookies
- All database operations use pg.zig natively (no Django ORM)
- HTMX is used for inline editing and partial page updates
- The audit log stores JSON diffs, not just text descriptions
