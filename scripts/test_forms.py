#!/usr/bin/env python3
"""
Tests for standalone Form and ModelForm.

Usage:
    uv run hyper-test forms
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from datetime import date

from hyperdjango.database import Database, set_db
from hyperdjango.forms import (
    BooleanField,
    CharField,
    ChoiceField,
    DateField,
    EmailField,
    FloatField,
    Form,
    HiddenField,
    IntegerField,
    ModelForm,
    PasswordField,
)
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
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


class FormProduct(Model):
    class Meta:
        table = "form_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    price: float = Field(default=0.0)
    stock: int = Field(default=0)
    is_active: bool = Field(default=True)
    description: str = Field(max_length=2000, default="")


# ---------------------------------------------------------------------------
# Test forms
# ---------------------------------------------------------------------------


class ContactForm(Form):
    name = CharField(max_length=100, required=True, label="Your Name")
    email = EmailField(required=True)
    message = CharField(widget="textarea", required=True)
    age = IntegerField(min_value=0, max_value=150, required=False)


class LoginForm(Form):
    username = CharField(max_length=150, required=True)
    password = PasswordField(max_length=128, required=True)


class CrossFieldForm(Form):
    password = PasswordField(required=True)
    confirm_password = PasswordField(required=True)

    def clean(self):
        if self.cleaned_data.get("password") != self.cleaned_data.get(
            "confirm_password"
        ):
            raise ValueError("Passwords do not match")


class ProductForm(ModelForm):
    class Meta:
        model = FormProduct
        fields = ["name", "price", "stock", "is_active"]


class ProductFormExclude(ModelForm):
    class Meta:
        model = FormProduct
        exclude = ["description"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("Form and ModelForm Tests")
    print("=" * 60)

    test_form_fields()
    test_form_validation()
    test_form_rendering()
    test_cross_field_validation()
    test_modelform_generation()
    test_modelform_exclude()

    # Live DB test
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(test_modelform_save())

    # Summary
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


def test_form_fields():
    print("\n--- Form Field Validation ---")

    # CharField
    f = CharField(max_length=10)
    check("CharField accepts valid", f.clean("hello") == "hello")
    try:
        f.clean("a" * 20)
        check("CharField rejects too long", False)
    except ValueError:
        check("CharField rejects too long", True)

    f2 = CharField(min_length=3)
    try:
        f2.clean("ab")
        check("CharField rejects too short", False)
    except ValueError:
        check("CharField rejects too short", True)

    # IntegerField
    f = IntegerField(min_value=0, max_value=100)
    check("IntegerField accepts valid", f.clean("42") == 42)
    try:
        f.clean("abc")
        check("IntegerField rejects non-integer", False)
    except ValueError:
        check("IntegerField rejects non-integer", True)
    try:
        f.clean("-1")
        check("IntegerField rejects below min", False)
    except ValueError:
        check("IntegerField rejects below min", True)
    # A crafted JSON number like 1e309 parses to float('inf'); int(inf) raises
    # OverflowError, which must surface as a clean ValueError, not an uncaught 500.
    fi = IntegerField()
    for bad, label in (
        (float("inf"), "inf"),
        (float("nan"), "nan"),
        (float("-inf"), "-inf"),
    ):
        try:
            fi.clean(bad)
            check(f"IntegerField rejects {label} (no 500)", False)
        except ValueError:
            check(f"IntegerField rejects {label} (no 500)", True)
    # Huge finite float still coerces if integral.
    check("IntegerField accepts large finite float", fi.clean(1e15) == 10**15)

    # FloatField
    f = FloatField()
    check("FloatField accepts valid", f.clean("3.14") == 3.14)
    try:
        f.clean("not-a-number")
        check("FloatField rejects invalid", False)
    except ValueError:
        check("FloatField rejects invalid", True)
    # inf/nan reach float() but must be rejected as non-finite (incl. "1e999").
    for bad in ("1e999", "inf", "-inf", "nan"):
        try:
            f.clean(bad)
            check(f"FloatField rejects non-finite {bad!r}", False)
        except ValueError:
            check(f"FloatField rejects non-finite {bad!r}", True)

    # BooleanField
    f = BooleanField()
    check("BooleanField true values", f.clean("1") is True)
    check("BooleanField true on", f.clean("on") is True)
    check("BooleanField false empty", f.clean("") is False)
    check("BooleanField false None", f.clean(None) is False)

    # DateField
    f = DateField()
    check("DateField accepts valid", f.clean("2026-03-22") == date(2026, 3, 22))
    try:
        f.clean("not-a-date")
        check("DateField rejects invalid", False)
    except ValueError:
        check("DateField rejects invalid", True)

    # EmailField
    f = EmailField()
    check("EmailField accepts valid", f.clean("user@example.com") == "user@example.com")
    try:
        f.clean("not-email")
        check("EmailField rejects invalid", False)
    except ValueError:
        check("EmailField rejects invalid", True)

    # ChoiceField
    f = ChoiceField(choices=[("a", "Option A"), ("b", "Option B")])
    check("ChoiceField accepts valid", f.clean("a") == "a")
    try:
        f.clean("c")
        check("ChoiceField rejects invalid", False)
    except ValueError:
        check("ChoiceField rejects invalid", True)

    # Required fields
    f = CharField(required=True)
    try:
        f.clean("")
        check("required rejects empty", False)
    except ValueError:
        check("required rejects empty", True)
    try:
        f.clean(None)
        check("required rejects None", False)
    except ValueError:
        check("required rejects None", True)

    # Optional fields
    f = CharField(required=False, initial="default")
    check("optional returns initial for empty", f.clean("") == "default")

    # PasswordField
    f = PasswordField()
    check("PasswordField widget is password", f.widget == "password")

    # HiddenField
    f = HiddenField()
    check("HiddenField widget is hidden", f.widget == "hidden")
    check("HiddenField not required", f.required is False)


def test_form_validation():
    print("\n--- Form Validation ---")

    # Valid form
    form = ContactForm(
        data={
            "name": "Alice",
            "email": "alice@example.com",
            "message": "Hello!",
            "age": "25",
        }
    )
    check("valid form is valid", form.is_valid())
    check("cleaned_data has name", form.cleaned_data.get("name") == "Alice")
    check(
        "cleaned_data has email", form.cleaned_data.get("email") == "alice@example.com"
    )
    check("cleaned_data has age as int", form.cleaned_data.get("age") == 25)
    check("no errors", len(form.errors) == 0)

    # Missing required field
    form2 = ContactForm(
        data={
            "name": "Alice",
            "message": "Hi",
        }
    )
    check("missing required invalid", not form2.is_valid())
    check("email in errors", "email" in form2.errors)

    # Invalid email
    form3 = ContactForm(
        data={
            "name": "Alice",
            "email": "not-email",
            "message": "Hi",
        }
    )
    check("invalid email rejected", not form3.is_valid())
    check("email has error", "email" in form3.errors)

    # Unbound form
    form4 = ContactForm()
    check("unbound form is not valid", not form4.is_valid())

    # Optional field omitted
    form5 = ContactForm(
        data={
            "name": "Alice",
            "email": "alice@example.com",
            "message": "Hi",
        }
    )
    check("optional field omitted is valid", form5.is_valid())

    # Fields property
    check("fields returns dict", isinstance(form.fields, dict))
    check("fields has name", "name" in form.fields)
    check("fields has email", "email" in form.fields)


def test_form_rendering():
    print("\n--- Form Rendering ---")

    form = ContactForm(data={"name": "Alice"})

    # as_div
    html = form.as_div()
    check("as_div has form-group divs", "form-group" in html)
    check("as_div has labels", "<label" in html)
    check("as_div has inputs", "<input" in html)
    check("as_div has textarea", "<textarea" in html)
    check("as_div has value", "Alice" in html)

    # as_table
    html_table = form.as_table()
    check("as_table has tr", "<tr>" in html_table)
    check("as_table has th", "<th>" in html_table)

    # as_p
    html_p = form.as_p()
    check("as_p has p", "<p>" in html_p)

    # Field rendering
    name_field = form.fields["name"]
    field_html = name_field.render("Alice")
    check("field render has name attr", 'name="name"' in field_html)
    check("field render has value", "Alice" in field_html)

    # Error rendering
    form2 = ContactForm(data={})
    form2.is_valid()
    html_with_errors = form2.as_div()
    check("errors shown in HTML", "has-error" in html_with_errors)

    # Login form with password
    login = LoginForm(data={"username": "admin", "password": "secret"})
    html_login = login.as_div()
    check("password field renders as password type", 'type="password"' in html_login)

    # Custom label
    check("custom label rendered", "Your Name" in form.as_div())


def test_cross_field_validation():
    print("\n--- Cross-Field Validation ---")

    # Matching passwords
    form = CrossFieldForm(
        data={
            "password": "mypassword",
            "confirm_password": "mypassword",
        }
    )
    check("matching passwords valid", form.is_valid())

    # Mismatched passwords
    form2 = CrossFieldForm(
        data={
            "password": "mypassword",
            "confirm_password": "different",
        }
    )
    check("mismatched passwords invalid", not form2.is_valid())
    check("__all__ error exists", "__all__" in form2.errors)
    check("error message correct", "do not match" in form2.errors["__all__"][0])


def test_modelform_generation():
    print("\n--- ModelForm Auto-Generation ---")

    # Check fields were auto-generated
    check("ModelForm has fields", len(ProductForm._declared_fields) > 0)
    check("ModelForm has name field", "name" in ProductForm._declared_fields)
    check("ModelForm has price field", "price" in ProductForm._declared_fields)
    check("ModelForm has stock field", "stock" in ProductForm._declared_fields)
    check("ModelForm has is_active field", "is_active" in ProductForm._declared_fields)
    check("ModelForm excludes id (auto)", "id" not in ProductForm._declared_fields)

    # Check field types
    check(
        "name is CharField", isinstance(ProductForm._declared_fields["name"], CharField)
    )
    check(
        "price is FloatField",
        isinstance(ProductForm._declared_fields["price"], FloatField),
    )
    check(
        "stock is IntegerField",
        isinstance(ProductForm._declared_fields["stock"], IntegerField),
    )
    check(
        "is_active is BooleanField",
        isinstance(ProductForm._declared_fields["is_active"], BooleanField),
    )

    # Validate with data
    form = ProductForm(
        data={"name": "Widget", "price": "9.99", "stock": "100", "is_active": "1"}
    )
    check("ModelForm validates", form.is_valid())
    check("cleaned name", form.cleaned_data["name"] == "Widget")
    check("cleaned price", form.cleaned_data["price"] == 9.99)
    check("cleaned stock", form.cleaned_data["stock"] == 100)
    check("cleaned is_active", form.cleaned_data["is_active"] is True)


def test_modelform_exclude():
    print("\n--- ModelForm Exclude ---")

    check("exclude form has name", "name" in ProductFormExclude._declared_fields)
    check("exclude form has price", "price" in ProductFormExclude._declared_fields)
    check(
        "exclude form omits description",
        "description" not in ProductFormExclude._declared_fields,
    )
    check("exclude form omits id", "id" not in ProductFormExclude._declared_fields)


async def test_modelform_save():
    print("\n--- ModelForm Save (Live DB) ---")

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS form_products CASCADE")
    await db.execute("""
        CREATE TABLE form_products (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            price FLOAT DEFAULT 0.0,
            stock INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            description VARCHAR(2000) DEFAULT ''
        )
    """)

    try:
        # Create via ModelForm
        form = ProductForm(
            data={
                "name": "FormWidget",
                "price": "29.99",
                "stock": "50",
                "is_active": "1",
            }
        )
        check("save form is valid", form.is_valid())
        instance = await form.save()
        check("save returns instance", instance is not None)
        check("save instance has PK", instance.pk is not None)
        check("save instance name", instance.name == "FormWidget")
        check("save instance price", abs(instance.price - 29.99) < 0.01)

        # Verify in DB
        row = await db.query_one(
            "SELECT name, price, stock FROM form_products WHERE id = $1", instance.pk
        )
        check("saved in DB", row is not None)
        check("DB name correct", row["name"] == "FormWidget")
        check("DB price correct", abs(row["price"] - 29.99) < 0.01)
        check("DB stock correct", row["stock"] == 50)

        # Update via ModelForm
        form2 = ProductForm(
            data={
                "name": "UpdatedWidget",
                "price": "39.99",
                "stock": "75",
                "is_active": "1",
            },
            instance=instance,
        )
        check("update form is valid", form2.is_valid())
        updated = await form2.save()
        check("update returns same instance", updated.pk == instance.pk)
        check("update name changed", updated.name == "UpdatedWidget")

        # Verify update in DB
        row2 = await db.query_one(
            "SELECT name, price FROM form_products WHERE id = $1", instance.pk
        )
        check("updated in DB", row2["name"] == "UpdatedWidget")
        check("updated price in DB", abs(row2["price"] - 39.99) < 0.01)

        # Invalid form can't save
        form3 = ProductForm(data={})
        check("invalid form can't save", not form3.is_valid())
        try:
            await form3.save()
            check("invalid save raises", False)
        except ValueError:
            check("invalid save raises", True)

    finally:
        await db.execute("DROP TABLE IF EXISTS form_products CASCADE")
        await db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
