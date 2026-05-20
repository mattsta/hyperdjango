#!/usr/bin/env python3
"""
Tests for FormSet and ModelFormSet.

Tests: validation, extra forms, deletion, min/max constraints, iteration.

Usage:
    uv run hyper-test formset
"""

# hyper-test: unit

import sys
import traceback

from hyperdjango.forms import (
    CharField,
    Form,
    FormSet,
    IntegerField,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        def wrapper():
            try:
                func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# Test form
class ItemForm(Form):
    name = CharField(max_length=100, required=True)
    quantity = IntegerField(min_value=0, required=True)


class OptionalForm(Form):
    label = CharField(max_length=50, required=False)


# ═══════════════════════════════════════════════════════════════════════════
# BASIC FORMSET
# ═══════════════════════════════════════════════════════════════════════════


@test("FormSet: basic creation with data")
def test_basic():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "Widget", "quantity": "5"},
            {"name": "Gadget", "quantity": "3"},
        ],
    )
    assert len(fs) == 2
    assert fs.is_valid()
    assert len(fs.cleaned_data) == 2
    assert fs.cleaned_data[0]["name"] == "Widget"
    assert fs.cleaned_data[0]["quantity"] == 5
    assert fs.cleaned_data[1]["name"] == "Gadget"


@test("FormSet: invalid form in set")
def test_invalid():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "Widget", "quantity": "5"},
            {"name": "", "quantity": "3"},  # name is required
        ],
    )
    assert not fs.is_valid()
    assert len(fs.errors) == 2
    assert fs.errors[0] == {}  # first form valid
    assert "name" in fs.errors[1]  # second form has error


@test("FormSet: empty data list")
def test_empty():
    fs = FormSet(ItemForm, data=[])
    assert len(fs) == 0
    assert fs.is_valid()  # empty is valid when min_num=0


@test("FormSet: iteration")
def test_iteration():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "A", "quantity": "1"},
            {"name": "B", "quantity": "2"},
            {"name": "C", "quantity": "3"},
        ],
    )
    names = [f.data["name"] for f in fs]
    assert names == ["A", "B", "C"]


@test("FormSet: indexing")
def test_indexing():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "First", "quantity": "1"},
            {"name": "Second", "quantity": "2"},
        ],
    )
    assert fs[0].data["name"] == "First"
    assert fs[1].data["name"] == "Second"


# ═══════════════════════════════════════════════════════════════════════════
# EXTRA FORMS
# ═══════════════════════════════════════════════════════════════════════════


@test("FormSet: extra blank forms")
def test_extra():
    fs = FormSet(
        OptionalForm,
        data=[
            {"label": "Existing"},
        ],
        extra=2,
    )
    assert len(fs) == 3  # 1 with data + 2 extra blank


@test("FormSet: extra with no data")
def test_extra_no_data():
    fs = FormSet(OptionalForm, extra=3)
    assert len(fs) == 3  # all blank


@test("FormSet: extra respects max_num")
def test_extra_max_num():
    fs = FormSet(
        OptionalForm,
        data=[
            {"label": "A"},
        ],
        extra=10,
        max_num=3,
    )
    assert len(fs) == 3  # 1 data + 2 extra (capped at max_num=3)


# ═══════════════════════════════════════════════════════════════════════════
# DELETION
# ═══════════════════════════════════════════════════════════════════════════


@test("FormSet: can_delete=True filters deleted forms")
def test_delete():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "Keep", "quantity": "1", "DELETE": False},
            {"name": "Remove", "quantity": "2", "DELETE": True},
            {"name": "Also Keep", "quantity": "3"},
        ],
        can_delete=True,
    )
    assert len(fs.forms) == 2  # 2 kept
    assert len(fs.deleted_forms) == 1  # 1 deleted
    assert fs.forms[0].data["name"] == "Keep"
    assert fs.forms[1].data["name"] == "Also Keep"
    assert fs.deleted_forms[0].data["name"] == "Remove"


@test("FormSet: can_delete=False ignores DELETE flag")
def test_no_delete():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "A", "quantity": "1", "DELETE": True},
        ],
        can_delete=False,
    )
    assert len(fs.forms) == 1  # DELETE flag ignored
    assert len(fs.deleted_forms) == 0


# ═══════════════════════════════════════════════════════════════════════════
# MIN/MAX NUM
# ═══════════════════════════════════════════════════════════════════════════


@test("FormSet: min_num violation")
def test_min_num():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "Only One", "quantity": "1"},
        ],
        min_num=3,
    )
    assert not fs.is_valid()
    errors = fs.non_form_errors()
    assert len(errors) > 0
    assert "3" in errors[0]


@test("FormSet: min_num satisfied")
def test_min_num_ok():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "A", "quantity": "1"},
            {"name": "B", "quantity": "2"},
            {"name": "C", "quantity": "3"},
        ],
        min_num=2,
    )
    assert fs.is_valid()


@test("FormSet: max_num truncates data")
def test_max_num():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "A", "quantity": "1"},
            {"name": "B", "quantity": "2"},
            {"name": "C", "quantity": "3"},
            {"name": "D", "quantity": "4"},
        ],
        max_num=2,
    )
    assert len(fs) == 2  # Only first 2 accepted


# ═══════════════════════════════════════════════════════════════════════════
# INITIAL DATA
# ═══════════════════════════════════════════════════════════════════════════


@test("FormSet: initial data populates forms")
def test_initial():
    fs = FormSet(
        ItemForm,
        initial=[
            {"name": "Default A", "quantity": "10"},
            {"name": "Default B", "quantity": "20"},
        ],
    )
    assert len(fs) == 2
    # initial data seeds UNBOUND forms (rendered with values, not yet submitted)
    assert not fs.forms[0]._is_bound
    assert fs.forms[0].initial["name"] == "Default A"


# ═══════════════════════════════════════════════════════════════════════════
# PROPERTIES
# ═══════════════════════════════════════════════════════════════════════════


@test("FormSet: total_form_count includes deleted")
def test_total_count():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "A", "quantity": "1"},
            {"name": "B", "quantity": "2", "DELETE": True},
        ],
        can_delete=True,
    )
    assert fs.total_form_count == 2  # 1 active + 1 deleted


@test("FormSet: initial_form_count counts bound forms")
def test_initial_count():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "A", "quantity": "1"},
        ],
        extra=2,
    )
    assert fs.initial_form_count == 1  # Only 1 has data


@test("FormSet: cleaned_data only from valid forms")
def test_cleaned_only_valid():
    fs = FormSet(
        ItemForm,
        data=[
            {"name": "Good", "quantity": "5"},
            {"name": "", "quantity": "bad"},  # two errors
        ],
    )
    fs.is_valid()
    # Only the first form should have cleaned_data
    assert len(fs.cleaned_data) == 1
    assert fs.cleaned_data[0]["name"] == "Good"


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


def main():
    tests = [
        obj
        for name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print("\n═══ FormSet Tests ═══")
    for t in tests:
        t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
