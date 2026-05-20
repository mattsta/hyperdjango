"""
Reproduce form validation bugs found in e2e_forms_demo.

# hyper-test: unit

1. ModelForm: empty title should fail validation (required field)
2. EmailField: "notanemail" should fail validation

Usage:
    uv run hyper-test form_required_bug
"""

import sys
from datetime import date
from enum import Enum

from hyperdjango.forms import (
    CharField,
    ChoiceField,
    EmailField,
    Form,
    ModelForm,
)
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model

passed = 0
failed = 0
errors: list[str] = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        errors.append(name)
        print(f"  FAIL: {name} — {detail}")


# --- Models ---


class Category(Enum):
    BUG = "bug"
    FEATURE = "feature"


class Priority(Enum):
    NORMAL = "normal"
    URGENT = "urgent"


class Ticket(TimestampMixin, Model):
    class Meta:
        table = "form_bug_tickets"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    description: str = Field(default="")
    category: Category = Field(default=Category.BUG)
    priority: Priority = Field(default=Priority.NORMAL)
    email: str = Field(default="")
    budget: float = Field(default=0.0)
    due_date: date = Field(default=None)
    is_urgent: bool = Field(default=False)


# --- Forms ---


class ContactForm(Form):
    name = CharField(min_length=2, label="Name")
    email = EmailField(label="Email")
    subject = CharField(min_length=5, label="Subject")
    message = CharField(min_length=10, widget="textarea", label="Message")
    priority = ChoiceField(choices=[("normal", "Normal"), ("urgent", "Urgent")])


class TicketForm(ModelForm):
    class Meta:
        model = Ticket
        fields = [
            "title",
            "description",
            "category",
            "priority",
            "email",
            "budget",
            "due_date",
            "is_urgent",
        ]


# --- Tests ---


def test_email_field_rejects_invalid():
    print("\n-- EmailField validation --")
    field = EmailField(label="Email")

    # Valid
    check("valid email accepted", field.clean("test@example.com") == "test@example.com")

    # Invalid - no @
    try:
        field.clean("notanemail")
        check("notanemail rejected", False, "should have raised ValueError")
    except ValueError as e:
        check("notanemail rejected", True, str(e))

    # Invalid - no dot after @
    try:
        field.clean("user@localhost")
        check("user@localhost rejected", False, "should have raised ValueError")
    except ValueError as e:
        check("user@localhost rejected", True, str(e))


def test_contact_form_invalid_email():
    print("\n-- ContactForm with invalid email --")
    form = ContactForm(
        data={
            "name": "Test",
            "email": "notanemail",
            "subject": "Hello Test",
            "message": "Long enough message here",
            "priority": "normal",
        }
    )
    valid = form.is_valid()
    check("form is NOT valid", not valid, f"is_valid={valid} errors={form.errors}")
    if not valid:
        check("email field has error", "email" in form.errors, f"errors={form.errors}")


def test_modelform_empty_title():
    print("\n-- TicketForm with empty title --")
    form = TicketForm(
        data={
            "title": "",
            "description": "No title",
            "category": "bug",
            "priority": "normal",
        }
    )
    valid = form.is_valid()
    check(
        "form is NOT valid",
        not valid,
        f"is_valid={valid} errors={form.errors} cleaned={form.cleaned_data}",
    )
    if not valid:
        check("title field has error", "title" in form.errors, f"errors={form.errors}")


def test_modelform_valid_title():
    print("\n-- TicketForm with valid title --")
    form = TicketForm(
        data={
            "title": "Bug Report",
            "description": "Something broke",
            "category": "bug",
            "priority": "normal",
        }
    )
    valid = form.is_valid()
    check("form IS valid", valid, f"errors={form.errors}")


def test_modelform_field_required_flags():
    print("\n-- TicketForm field required flags --")

    form = TicketForm()
    title_field = form.fields.get("title")
    check("title field exists", title_field is not None)
    if title_field:
        check(
            "title field required",
            title_field.required,
            f"required={title_field.required}",
        )

    desc_field = form.fields.get("description")
    check("description field exists", desc_field is not None)
    if desc_field:
        check(
            "description NOT required (has default)",
            not desc_field.required,
            f"required={desc_field.required}",
        )


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("=" * 60)
    print("Form Required Field Bug Reproduction")
    print("=" * 60)

    test_email_field_rejects_invalid()
    test_contact_form_invalid_email()
    test_modelform_empty_title()
    test_modelform_valid_title()
    test_modelform_field_required_flags()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
