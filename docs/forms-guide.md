# Forms and Validation Guide

This guide covers building real-world forms with HyperDjango's standalone form
system. It walks through field types, validation strategies, ModelForm
auto-generation, formsets, and file uploads with practical examples.

For the full API reference, see [forms.md](forms.md) and [formsets.md](formsets.md).

---

## Table of Contents

- [Defining a Form](#defining-a-form)
- [Field Types Reference](#field-types-reference)
- [Widgets and Rendering](#widgets-and-rendering)
- [Field-Level Validation](#field-level-validation)
- [Per-Field Clean Hooks](#per-field-clean-hooks)
- [Cross-Field Validation](#cross-field-validation)
- [Error Handling](#error-handling)
- [ModelForm Auto-Generation](#modelform-auto-generation)
- [Formsets](#formsets)
- [File Upload Handling](#file-upload-handling)
- [Putting It All Together](#putting-it-all-together)
- [Migration from Django Forms](#migration-from-django-forms)

---

## Defining a Form

A form is a Python class that declares fields as class attributes. Each field
handles type coercion, validation, and HTML rendering.

```python
from hyperdjango.forms import (
    Form,
    CharField,
    EmailField,
    IntegerField,
    BooleanField,
    ChoiceField,
    DateField,
)


class JobApplicationForm(Form):
    first_name = CharField(max_length=50, required=True)
    last_name = CharField(max_length=50, required=True)
    email = EmailField(required=True)
    years_experience = IntegerField(min_value=0, max_value=50)
    preferred_role = ChoiceField(
        choices=[
            ("backend", "Backend Engineer"),
            ("frontend", "Frontend Engineer"),
            ("fullstack", "Full-Stack Engineer"),
            ("devops", "DevOps Engineer"),
        ]
    )
    available_from = DateField(required=False)
    willing_to_relocate = BooleanField()
```

To process submitted data, pass it as the `data` keyword argument:

```python
@app.post("/apply")
async def apply(request):
    form = JobApplicationForm(data=await request.form())
    if form.is_valid():
        # form.cleaned_data is a dict of validated, type-coerced values
        await save_application(form.cleaned_data)
        return Response.redirect("/apply/thanks")
    # Re-render with errors
    return Response.html(engine.render("apply.html", {"form": form}))
```

---

## Field Types Reference

HyperDjango provides 12 built-in field types. Each field coerces raw string
input to the appropriate Python type and applies its own constraints.

| Field           | Python Type    | Default Widget   | Key Options                             |
| --------------- | -------------- | ---------------- | --------------------------------------- |
| `CharField`     | `str`          | `text`           | `max_length`, `min_length`              |
| `IntegerField`  | `int`          | `number`         | `min_value`, `max_value`                |
| `FloatField`    | `float`        | `number`         | `min_value`, `max_value`                |
| `DecimalField`  | `Decimal`      | `number`         | `max_digits`, `decimal_places`          |
| `BooleanField`  | `bool`         | `checkbox`       | (defaults `required=False`)             |
| `DateField`     | `date`         | `date`           | Expects ISO format `YYYY-MM-DD`         |
| `DateTimeField` | `datetime`     | `datetime-local` | Expects ISO format                      |
| `ChoiceField`   | `Any`          | `select`         | `choices=[(value, label), ...]`         |
| `EmailField`    | `str`          | `email`          | Inherits `CharField` + email validation |
| `PasswordField` | `str`          | `password`       | Inherits `CharField`                    |
| `HiddenField`   | `str`          | `hidden`         | Defaults `required=False`               |
| `FileField`     | `UploadedFile` | `file`           | `max_size`, `allowed_extensions`        |

Every field supports these common options:

```python
CharField(
    required=True,  # Must be present and non-empty
    label="Full Name",  # Custom label (auto-generated if None)
    help_text="As it appears on your passport",
    initial="",  # Default for unbound forms
    widget="textarea",  # Override default widget
    attrs={"class": "input-lg", "placeholder": "Enter name"},
    error_messages={"required": "We need your name to proceed"},
)
```

---

## Widgets and Rendering

Each field has a default widget, but you can override it. The widget determines
the HTML `<input type="...">` used for rendering.

```python
class NoteForm(Form):
    title = CharField(max_length=200)
    body = CharField(widget="textarea", attrs={"rows": "10", "cols": "60"})
    priority = IntegerField(widget="range", attrs={"min": "1", "max": "5"})
    secret_ref = CharField(widget="hidden", required=False)
```

Three rendering methods produce complete HTML:

```python
form = NoteForm(data={})

# Div-based (recommended for CSS styling)
html = form.as_div()
# Produces: <div class="form-group"><label>...</label><input ...></div>

# Table rows (for tabular layouts)
html = form.as_table()
# Produces: <tr><th><label>Title</label></th><td><input ...></td></tr>

# Paragraph (simple layouts)
html = form.as_p()
# Produces: <p><label>Title: <input ...></label></p>
```

For fine-grained control, render individual fields:

```python
for name, field in form.fields.items():
    value = form.data.get(name)
    errors = form.errors.get(name, [])
    html = field.render(value, errors)
```

---

## Field-Level Validation

Each field type performs built-in validation during its `clean()` method.
Validation happens automatically when you call `form.is_valid()`.

```python
class ProductForm(Form):
    name = CharField(min_length=3, max_length=200)
    price = DecimalField(max_digits=10, decimal_places=2)
    stock = IntegerField(min_value=0, max_value=999999)
    sku = CharField(
        max_length=20,
        error_messages={"required": "SKU is required for inventory tracking"},
    )
    category = ChoiceField(
        choices=[
            ("electronics", "Electronics"),
            ("clothing", "Clothing"),
            ("food", "Food & Beverage"),
        ]
    )


form = ProductForm(data={"name": "A", "price": "abc", "stock": "-5"})
form.is_valid()  # False

# Errors are collected per field
print(form.errors)
# {
#     "name": ["Must be at least 3 characters"],
#     "price": ["Enter a valid decimal number"],
#     "stock": ["Must be at least 0"],
#     "sku": ["SKU is required for inventory tracking"],
# }
```

---

## Per-Field Clean Hooks

Define a method named `clean_<fieldname>` on your form class for custom
validation logic that goes beyond built-in constraints. The method can access
`self.cleaned_data` for already-validated field values.

```python
import re


class RegistrationForm(Form):
    username = CharField(min_length=3, max_length=30)
    email = EmailField()
    password = CharField(widget="password", min_length=8)
    confirm_password = CharField(widget="password", min_length=8)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if not re.match(r"^[a-zA-Z0-9_]+$", username):
            raise ValueError(
                "Username can only contain letters, numbers, and underscores"
            )
        # Check uniqueness against the database
        # (in a real app, you would await a DB query here)
        if username.lower() in ("admin", "root", "system"):
            raise ValueError("This username is reserved")
        return username.lower()  # Normalize to lowercase

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email.endswith(".example.com"):
            raise ValueError("Example domains are not allowed")
        return email
```

The return value of `clean_<fieldname>` replaces the entry in `cleaned_data`,
so you can use it for normalization (lowercasing, stripping, etc.).

---

## Cross-Field Validation

Override the `clean()` method for validation that involves multiple fields.
This runs after all individual fields have been validated (and only if there
are no field-level errors).

```python
class RegistrationForm(Form):
    username = CharField(min_length=3, max_length=30)
    email = EmailField()
    password = CharField(widget="password", min_length=8)
    confirm_password = CharField(widget="password", min_length=8)

    def clean(self):
        password = self.cleaned_data.get("password")
        confirm = self.cleaned_data.get("confirm_password")
        if password and confirm and password != confirm:
            raise ValueError("Passwords do not match")

        # You can also use add_error for targeted messages
        email = self.cleaned_data.get("email", "")
        username = self.cleaned_data.get("username", "")
        if username and email and username in email:
            self.add_error("email", "Email should not contain your username")
```

---

## Error Handling

HyperDjango provides several methods for working with validation errors.

```python
form = RegistrationForm(data=incomplete_data)
form.is_valid()

# Check specific field errors
if form.has_error("email"):
    print("Email has problems")

if form.has_error("email", "This field is required"):
    print("Email was not provided")

# Get form-level (non-field) errors
global_errors = form.non_field_errors()
# Returns: ["Passwords do not match"]

# Programmatically add errors (useful in view logic)
form.add_error("username", "This username is already taken")
form.add_error(None, "Registration is currently closed")  # Non-field error

# Serialize errors to JSON (for AJAX/API responses)
json_str = form.errors_as_json()
# '{"username": [{"message": "This username is already taken"}], ...}'

# Structured dict (for manual serialization)
error_data = form.get_json_data()
# {"username": [{"message": "This username is already taken"}]}
```

Using errors in templates:

```html
{% if form.non_field_errors() %}
<div class="alert alert-danger">
  {% for error in form.non_field_errors() %}
  <p>{{ error }}</p>
  {% endfor %}
</div>
{% endif %}

<form method="post">
  {{ form.as_div() }}
  <button type="submit">Register</button>
</form>
```

---

## ModelForm Auto-Generation

`ModelForm` inspects a HyperApp `Model` and automatically generates form fields
from its type annotations and field constraints.

```python
from hyperdjango import Model, Field
from hyperdjango.forms import ModelForm


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    description: str = Field(default="")
    price: Decimal = Field(ge=0)
    stock: int = Field(ge=0, default=0)
    category: str = Field(max_length=50)
    is_active: bool = Field(default=True)


class ProductForm(ModelForm):
    class Meta:
        model = Product
        fields = ["name", "description", "price", "stock", "category", "is_active"]
        # Or use exclude to omit specific fields:
        # exclude = ["id", "created_at"]
        widgets = {
            "description": "textarea",
        }
```

The `Meta` class controls which fields are included:

- `fields` -- explicit allowlist of field names to include
- `exclude` -- fields to omit (include everything else)
- `widgets` -- override default widget for specific fields

### Saving a ModelForm

`ModelForm.save()` performs an `INSERT` (new instance) or `UPDATE` (existing instance):

```python
@app.post("/products/new")
async def create_product(request):
    form = ProductForm(data=await request.form())
    if form.is_valid():
        product = await form.save()  # INSERT INTO products ...
        return Response.redirect(f"/products/{product.id}")
    return Response.html(engine.render("product_form.html", {"form": form}))


@app.post("/products/{id}")
async def update_product(request):
    product = await Product.objects.get(id=request.params["id"])
    form = ProductForm(data=await request.form(), instance=product)
    if form.is_valid():
        product = await form.save()  # UPDATE products SET ... WHERE id = ...
        return Response.redirect(f"/products/{product.id}")
    return Response.html(engine.render("product_form.html", {"form": form}))
```

When an `instance` is provided, `save()` updates that record. Without one,
it creates a new record.

---

## Formsets

Formsets let you work with multiple instances of the same form on a single page.
Common use cases: bulk editing rows in a table, adding multiple items at once.

```python
from hyperdjango.forms import FormSet, model_formset_factory


# Manual formset from a regular form
class LineItemForm(Form):
    product = CharField(max_length=200)
    quantity = IntegerField(min_value=1, max_value=9999)
    unit_price = DecimalField(max_digits=10, decimal_places=2)


# Create a formset class that handles 3 forms at once
LineItemFormSet = FormSet.factory(LineItemForm, extra=3, max_num=20)

# Process submitted data
formset = LineItemFormSet(data=request_data)
if formset.is_valid():
    for form in formset.forms:
        if form.cleaned_data:  # Skip empty extra forms
            await save_line_item(form.cleaned_data)
```

### Model Formsets

Auto-generate formsets from models for bulk editing:

```python
ProductFormSet = model_formset_factory(
    Product,
    fields=["name", "price", "stock"],
    extra=2,  # 2 blank forms for new items
    max_num=50,  # Hard limit on total forms
)

# Pre-populate with existing data
existing = await Product.objects.filter(is_active=True).all()
formset = ProductFormSet(
    data=request_data,
    queryset=existing,
)

if formset.is_valid():
    saved = await formset.save()  # Bulk INSERT/UPDATE
```

### Rendering Formsets

```html
<form method="post">
  {{ formset.management_form }}
  <table>
    <thead>
      <tr>
        <th>Product</th>
        <th>Qty</th>
        <th>Price</th>
      </tr>
    </thead>
    <tbody>
      {% for form in formset.forms %}
      <tr>
        <td>{{ form.fields.product.render() }}</td>
        <td>{{ form.fields.quantity.render() }}</td>
        <td>{{ form.fields.unit_price.render() }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <button type="submit">Save All</button>
</form>
```

---

## File Upload Handling

HyperDjango supports file uploads through `FileField` and `ImageField` on
both standalone forms and models.

### Form-Based Uploads

```python
from hyperdjango.forms import Form, CharField, FileField


class DocumentUploadForm(Form):
    title = CharField(max_length=200)
    document = FileField(
        max_size=10 * 1024 * 1024,  # 10 MB limit
        allowed_extensions=[".pdf", ".docx", ".txt"],
    )


@app.post("/documents/upload")
async def upload_document(request):
    form = DocumentUploadForm(data=await request.form())
    if form.is_valid():
        uploaded = form.cleaned_data["document"]
        # uploaded.filename, uploaded.content_type, uploaded.read()
        await storage.save(f"documents/{uploaded.filename}", uploaded)
        return Response.redirect("/documents")
    return Response.html(engine.render("upload.html", {"form": form}))
```

### Model-Based Uploads

```python
from hyperdjango import Model, Field
from hyperdjango.models import FileField, ImageField


class Attachment(Model):
    class Meta:
        table = "attachments"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    file: str = FileField(upload_to="attachments/")
    thumbnail: str = ImageField(upload_to="thumbnails/", required=False)
```

Use `TestClient` with the `files` parameter for testing uploads:

```python
from hyperdjango.testing import TestClient

client = TestClient(app)
resp = client.post(
    "/documents/upload",
    files={
        "document": ("report.pdf", b"%PDF-1.4...", "application/pdf"),
    },
    data={"title": "Q4 Report"},
)
assert resp.status == 302
```

---

## Putting It All Together

Here is a complete real-world example: a multi-step checkout form with
cross-field validation, custom cleaning, and ModelForm integration.

```python
from decimal import Decimal
from hyperdjango.forms import (
    Form,
    ModelForm,
    CharField,
    EmailField,
    IntegerField,
    ChoiceField,
    DecimalField,
)
from hyperdjango import Model, Field


class Order(Model):
    class Meta:
        table = "orders"

    id: int = Field(primary_key=True, auto=True)
    customer_email: str = Field(max_length=200)
    shipping_address: str = Field()
    billing_address: str = Field()
    total: Decimal = Field(ge=0, default=Decimal("0"))
    status: str = Field(max_length=20, default="pending")


class CheckoutForm(ModelForm):
    class Meta:
        model = Order
        fields = ["customer_email", "shipping_address", "billing_address"]
        widgets = {"shipping_address": "textarea", "billing_address": "textarea"}

    same_as_shipping = BooleanField(label="Billing same as shipping")

    def clean(self):
        if self.cleaned_data.get("same_as_shipping"):
            self.cleaned_data["billing_address"] = self.cleaned_data.get(
                "shipping_address", ""
            )

    def clean_customer_email(self):
        email = self.cleaned_data["customer_email"]
        if "+" in email.split("@")[0]:
            raise ValueError("Plus-addressing is not supported for order emails")
        return email


@app.post("/checkout")
async def checkout(request):
    cart = await get_cart(request)
    form = CheckoutForm(data=await request.form())
    if form.is_valid():
        order = await form.save()
        order.total = cart.total
        await order.save()
        return Response.redirect(f"/orders/{order.id}/confirm")
    return Response.html(
        engine.render(
            "checkout.html",
            {
                "form": form,
                "cart": cart,
            },
        )
    )
```

---

## Migration from Django Forms

HyperDjango's form system follows Django's design closely. Here are the key
differences to be aware of when migrating:

| Django                     | HyperDjango                                    |
| -------------------------- | ---------------------------------------------- |
| `from django import forms` | `from hyperdjango.forms import Form, ...`      |
| `forms.ModelForm`          | `ModelForm` (same pattern, async `save()`)     |
| `form.save(commit=False)`  | Not needed -- `save()` is always explicit      |
| `formset_factory()`        | `FormSet.factory()`                            |
| `modelformset_factory()`   | `model_formset_factory()`                      |
| `form.cleaned_data`        | Same API                                       |
| `form.errors`              | Same API (dict of field -> list of messages)   |
| `clean_<fieldname>()`      | Same pattern                                   |
| `clean()` for cross-field  | Same pattern                                   |
| `add_error(field, error)`  | Same API                                       |
| `non_field_errors()`       | Same API                                       |
| Widget classes             | Widget strings (`"textarea"`, `"select"`, etc) |
| `ClearableFileInput`       | Not needed -- file handling is simpler         |

The main structural difference is that `ModelForm.save()` is `async` in
HyperDjango since all database operations go through pg.zig natively.
Widget customization uses string identifiers and `attrs` dicts rather than
separate widget classes.
