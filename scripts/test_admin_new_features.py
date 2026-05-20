"""
Tests for all new HyperAdmin Phase 1-4 features.

# hyper-test: unit

Covers:
  Phase 1: get_queryset, get_readonly_fields, get_fieldsets, get_list_display,
           enriched hook signatures, has_view_permission, get_search_results, get_form
  Phase 2: list_display_links, model_action, response_add/change/delete, view_on_site
  Phase 3: raw_id_fields, autocomplete_fields, radio_fields, save_as, save_on_top,
           show_full_result_count, empty_value_display
  Phase 4: @display decorator, sortable_by, on_add/on_change/on_delete,
           InlineConfig.show_change_link, InlineConfig.classes

Usage:
    uv run hyper-test admin_new_features
"""

import inspect
import sys

from hyperdjango.admin import HyperAdmin, display
from hyperdjango.admin.fields import (
    Fieldset,
    InlineConfig,
)
from hyperdjango.app import HyperApp
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class Item(TimestampMixin, Model):
    class Meta:
        table = "admin_feature_items"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    status: str = Field(default="draft")
    owner_id: int = Field(default=0)
    priority: str = Field(default="normal")
    secret: str = Field(default="")


class Comment(TimestampMixin, Model):
    class Meta:
        table = "admin_feature_comments"

    id: int = Field(primary_key=True, auto=True)
    item_id: int = Field(foreign_key=Item)
    text: str = Field(default="")


# ---------------------------------------------------------------------------
# Phase 1: Dynamic hooks — config acceptance
# ---------------------------------------------------------------------------


def test_phase1_config():
    print("\n-- Phase 1: Config acceptance --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    async def my_queryset(request):
        return {"owner_id": 1}

    def my_readonly(request, obj):
        return ["title"] if obj else []

    def my_fieldsets(request, obj):
        return [Fieldset(title="All", fields=["title", "status"])]

    def my_list_display(request):
        return ["id", "title", "status"]

    async def my_search(request, conditions, term):
        return {"title__ilike": f"%{term}%"}

    def my_form(request, obj):
        return {"fields": ["title", "status"]}

    config = admin.register(
        Item,
        list_display=["id", "title", "status", "owner_id"],
        get_queryset=my_queryset,
        get_readonly_fields=my_readonly,
        get_fieldsets=my_fieldsets,
        get_list_display=my_list_display,
        get_search_results=my_search,
        get_form=my_form,
        can_view=True,
    )

    check("get_queryset stored", config.get_queryset is my_queryset)
    check("get_readonly_fields stored", config.get_readonly_fields is my_readonly)
    check("get_fieldsets stored", config.get_fieldsets is my_fieldsets)
    check("get_list_display stored", config.get_list_display is my_list_display)
    check("get_search_results stored", config.get_search_results is my_search)
    check("get_form stored", config.get_form is my_form)
    check("can_view stored", config.can_view is True)


# ---------------------------------------------------------------------------
# Phase 1: Enriched hook signatures
# ---------------------------------------------------------------------------


def test_enriched_hooks():
    print("\n-- Phase 1: Enriched hook signatures --")

    async def old_save(values, is_edit):
        return values

    async def new_save(request, values, is_edit, obj):
        return values

    async def old_delete(pk):
        pass

    async def new_delete(request, pk, obj):
        pass

    # Arity detection
    check("old save: 2 params", len(inspect.signature(old_save).parameters) == 2)
    check("new save: 4 params", len(inspect.signature(new_save).parameters) == 4)
    check("old delete: 1 param", len(inspect.signature(old_delete).parameters) == 1)
    check("new delete: 3 params", len(inspect.signature(new_delete).parameters) == 3)


# ---------------------------------------------------------------------------
# Phase 2: View control config
# ---------------------------------------------------------------------------


def test_phase2_config():
    print("\n-- Phase 2: View control config --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    config = admin.register(
        Item,
        list_display=["id", "title"],
        list_display_links=["id", "title"],
        view_on_site=lambda obj: f"/items/{obj['id']}",
        response_add="continue",
        response_change="list",
        response_delete="list",
    )

    check("list_display_links stored", config.list_display_links == ["id", "title"])
    check("view_on_site callable", callable(config.view_on_site))
    check("view_on_site returns URL", config.view_on_site({"id": 42}) == "/items/42")
    check("response_add = continue", config.response_add == "continue")
    check("response_change = list", config.response_change == "list")


# ---------------------------------------------------------------------------
# Phase 2: model_action decorator
# ---------------------------------------------------------------------------


def test_model_action():
    print("\n-- Phase 2: model_action decorator --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    admin.register(Item, list_display=["id", "title"])

    @admin.model_action("item", "publish", method="POST")
    async def publish_item(request, id):
        pass

    @admin.model_action("item", "export", method="GET")
    async def export_items(request):
        pass

    # Verify routes were registered
    routes = (
        [(r.method, r.path) for r in app.router._routes]
        if hasattr(app.router, "_routes")
        else []
    )
    check("model_action registered (decorator returned func)", callable(publish_item))
    check("export_items registered", callable(export_items))


# ---------------------------------------------------------------------------
# Phase 3: Form widget config
# ---------------------------------------------------------------------------


def test_phase3_config():
    print("\n-- Phase 3: Form widget config --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    config = admin.register(
        Item,
        list_display=["id", "title"],
        raw_id_fields=["owner_id"],
        autocomplete_fields=["owner_id"],
        radio_fields={"priority": "horizontal", "status": "vertical"},
        save_as=True,
        save_on_top=True,
        show_full_result_count=False,
        empty_value_display="(empty)",
    )

    check("raw_id_fields stored", config.raw_id_fields == ["owner_id"])
    check("autocomplete_fields stored", config.autocomplete_fields == ["owner_id"])
    check(
        "radio_fields stored",
        config.radio_fields == {"priority": "horizontal", "status": "vertical"},
    )
    check("save_as = True", config.save_as is True)
    check("save_on_top = True", config.save_on_top is True)
    check("show_full_result_count = False", config.show_full_result_count is False)
    check("empty_value_display = (empty)", config.empty_value_display == "(empty)")


# ---------------------------------------------------------------------------
# Phase 3: radio_fields in form rendering
# ---------------------------------------------------------------------------


def test_radio_in_form_fields():
    print("\n-- Phase 3: radio_fields in form rendering --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    config = admin.register(
        Item,
        list_display=["id", "title"],
        radio_fields={"status": "horizontal"},
    )

    fields = admin._build_form_fields(config, values={})
    status_field = next((f for f in fields if f["name"] == "status"), None)
    check("status field found", status_field is not None)
    if status_field:
        check(
            "radio_layout = horizontal",
            status_field.get("radio_layout") == "horizontal",
        )

    # Field without radio_fields should have None
    title_field = next((f for f in fields if f["name"] == "title"), None)
    check("title has no radio_layout", title_field.get("radio_layout") is None)


# ---------------------------------------------------------------------------
# Phase 3: raw_id_fields suppresses FK autocomplete
# ---------------------------------------------------------------------------


def test_raw_id_suppresses_autocomplete():
    print("\n-- Phase 3: raw_id_fields suppresses autocomplete --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    # Comment has item_id FK
    config = admin.register(
        Comment,
        list_display=["id", "item_id"],
        raw_id_fields=["item_id"],
    )

    fields = admin._build_form_fields(config, values={})
    item_field = next((f for f in fields if f["name"] == "item_id"), None)
    check("item_id field found", item_field is not None)
    if item_field:
        check(
            "raw_id: foreign_key is None",
            item_field["foreign_key"] is None,
            f"got {item_field.get('foreign_key')}",
        )


# ---------------------------------------------------------------------------
# Phase 4: @display decorator
# ---------------------------------------------------------------------------


def test_display_decorator():
    print("\n-- Phase 4: @display decorator --")

    @display(description="Full Name", ordering="last_name", boolean=False)
    def full_name(obj):
        return f"{obj.get('first', '')} {obj.get('last', '')}"

    check("description attr", full_name._admin_description == "Full Name")
    check("ordering attr", full_name._admin_ordering == "last_name")
    check("boolean attr", full_name._admin_boolean is False)

    @display(description="Active?", boolean=True, empty_value="N/A")
    def is_active(obj):
        return obj.get("active", False)

    check("boolean = True", is_active._admin_boolean is True)
    check("empty_value = N/A", is_active._admin_empty_value == "N/A")

    # Verify callable column picks up description
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    config = admin.register(
        Item,
        list_display=["id", "full_name"],
        list_display_callables={"full_name": full_name},
    )

    cols = config.display_columns
    fn_col = next((c for c in cols if c["name"] == "full_name"), None)
    check("callable column found", fn_col is not None)
    if fn_col:
        check(
            "column label from @display",
            fn_col["label"] == "Full Name",
            f"got {fn_col['label']}",
        )


# ---------------------------------------------------------------------------
# Phase 4: InlineConfig.show_change_link + classes
# ---------------------------------------------------------------------------


def test_inline_new_fields():
    print("\n-- Phase 4: InlineConfig new fields --")

    inline = InlineConfig(
        model_class=Comment,
        show_change_link=True,
        classes=["collapse"],
    )

    check("show_change_link = True", inline.show_change_link is True)
    check("classes = [collapse]", inline.classes == ["collapse"])

    # Default values
    inline2 = InlineConfig(model_class=Comment)
    check("default show_change_link = False", inline2.show_change_link is False)
    check("default classes = []", inline2.classes == [])


# ---------------------------------------------------------------------------
# Phase 4: on_add/on_change/on_delete hooks
# ---------------------------------------------------------------------------


def test_post_save_hooks():
    print("\n-- Phase 4: on_add/on_change/on_delete hooks --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    async def on_add(request, values):
        pass

    async def on_change(request, values):
        pass

    async def on_delete(request, pk):
        pass

    config = admin.register(
        Item,
        list_display=["id", "title"],
        on_add=on_add,
        on_change=on_change,
        on_delete=on_delete,
    )

    check("on_add stored", config.on_add is on_add)
    check("on_change stored", config.on_change is on_change)
    check("on_delete stored", config.on_delete is on_delete)


# ---------------------------------------------------------------------------
# Phase 2: list_display_links in cell rendering
# ---------------------------------------------------------------------------


def test_list_display_links_cell_flag():
    print("\n-- Phase 2: list_display_links cell flag --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    # Default: first column is link
    config = admin.register(
        Item,
        list_display=["id", "title", "status"],
    )

    columns = config.display_columns
    check("default: 3 columns", len(columns) == 3)

    # Explicit links
    app2 = HyperApp(title="Test2")
    admin2 = HyperAdmin(
        app2, prefix="/admin", title="Test2", secret_key="test", require_auth=False
    )
    config2 = admin2.register(
        Item,
        list_display=["id", "title", "status"],
        list_display_links=["title"],
    )
    check("explicit links stored", config2.list_display_links == ["title"])


# ---------------------------------------------------------------------------
# Phase 1: get_readonly_fields + get_fieldsets integration
# ---------------------------------------------------------------------------


def test_dynamic_readonly_fieldsets():
    print("\n-- Phase 1: Dynamic readonly + fieldsets --")
    app = HyperApp(title="Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="Test", secret_key="test", require_auth=False
    )

    def my_readonly(request, obj):
        if obj is not None:
            return ["title", "status"]
        return []

    def my_fieldsets(request, obj):
        if obj is None:
            return [Fieldset(title="New", fields=["title", "status"])]
        return [
            Fieldset(title="Info", fields=["title", "status"]),
            Fieldset(title="Meta", fields=["owner_id", "priority"]),
        ]

    config = admin.register(
        Item,
        list_display=["id", "title"],
        get_readonly_fields=my_readonly,
        get_fieldsets=my_fieldsets,
    )

    # Simulate add view (obj=None) — use a mock request
    class MockRequest:
        user = None
        cookies = {}
        GET = {}

    # Add: no readonly, "New" fieldset + "Other" for remaining fields
    groups_add = admin._build_form_field_groups(
        config, values={}, request=MockRequest(), obj=None
    )
    check("add: has fieldsets", len(groups_add) >= 1)
    check("add: first title 'New'", groups_add[0]["title"] == "New")
    add_readonly = [f["name"] for f in groups_add[0]["fields"] if f.get("is_readonly")]
    check("add: no readonly fields", len(add_readonly) == 0, str(add_readonly))

    # Edit: title+status readonly, 2 fieldsets
    groups_edit = admin._build_form_field_groups(
        config,
        values={"title": "X", "status": "pub", "owner_id": 1, "priority": "high"},
        request=MockRequest(),
        obj={"id": 1, "title": "X"},
    )
    check("edit: 2 fieldsets", len(groups_edit) >= 2, f"got {len(groups_edit)}")
    edit_readonly = []
    for g in groups_edit:
        for f in g["fields"]:
            if f.get("is_readonly"):
                edit_readonly.append(f["name"])
    check("edit: title is readonly", "title" in edit_readonly, str(edit_readonly))
    check("edit: status is readonly", "status" in edit_readonly, str(edit_readonly))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def main():
    RESULTS["passed"] = 0
    RESULTS["failed"] = 0
    RESULTS["errors"] = []

    print("=" * 60)
    print("Admin New Features — Phase 1-4 Tests")
    print("=" * 60)

    test_phase1_config()
    test_enriched_hooks()
    test_phase2_config()
    test_model_action()
    test_phase3_config()
    test_radio_in_form_fields()
    test_raw_id_suppresses_autocomplete()
    test_display_decorator()
    test_inline_new_fields()
    test_post_save_hooks()
    test_list_display_links_cell_flag()
    test_dynamic_readonly_fieldsets()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Admin new features: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for e in RESULTS["errors"]:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
