# Forms

Standalone form system with field declaration, validation, and HTML rendering. No Django dependency.

## Quick Start

```python
from hyperdjango.forms import Form, CharField, EmailField, IntegerField


class ContactForm(Form):
    name = CharField(max_length=100, required=True)
    email = EmailField(required=True)
    message = CharField(widget="textarea", required=True)
    age = IntegerField(min_value=0, max_value=150, required=False)


form = ContactForm(
    data={"name": "Alice", "email": "alice@example.com", "message": "Hello"}
)
if form.is_valid():
    print(form.cleaned_data)  # {"name": "Alice", "email": "alice@example.com", ...}
else:
    print(form.errors)  # {"email": ["This field is required"]}

# Rendering
html = form.as_div()  # Each field in a <div class="form-group">
html = form.as_table()  # Each field in a <tr>
html = form.as_p()  # Each field in a <p>
```

---

## Common Field Options

Every field type accepts these keyword arguments:

| Option           | Type             | Default | Description                                                                                                                                          |
| ---------------- | ---------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `required`       | `bool`           | `True`  | Field must be present and non-empty. When `True`, submitting an empty value raises a validation error.                                               |
| `label`          | `str \| None`    | `None`  | Human-friendly label for HTML rendering. Auto-generated from the field name (underscores to spaces, first letter capitalized) when `None`.           |
| `help_text`      | `str`            | `""`    | Descriptive text displayed below the field in rendered HTML.                                                                                         |
| `initial`        | `object`         | `None`  | Default value for unbound forms. Can be a callable (evaluated at render time, not at class definition time). Not used as fallback during validation. |
| `widget`         | `str \| None`    | `None`  | Override the default widget type (e.g., `"textarea"`, `"hidden"`, `"password"`).                                                                     |
| `attrs`          | `dict[str, str]` | `{}`    | Extra HTML attributes added to the rendered input element (e.g., `{"class": "form-control", "placeholder": "Enter..."}` ).                           |
| `error_messages` | `dict[str, str]` | `{}`    | Override default error messages. Keys are error codes (`"required"`, `"invalid"`, `"max_length"`, etc.).                                             |
| `disabled`       | `bool`           | `False` | Renders the field with the `disabled` HTML attribute. Disabled fields are not editable and their submitted values are ignored in favor of `initial`. |
| `validators`     | `list[Callable]` | `[]`    | Additional validation functions. Each takes a single value and raises `ValueError` on invalid input.                                                 |

```python
CharField(
    required=True,
    label="Full Name",
    help_text="Enter your full legal name",
    initial="default",
    widget="textarea",
    attrs={"class": "lg", "rows": "4"},
    error_messages={"required": "Please fill this in"},
    disabled=False,
    validators=[
        lambda v: (
            None
            if len(v.split()) >= 2
            else (_ for _ in ()).throw(ValueError("Enter first and last name"))
        )
    ],
)
```

---

## Field Types Reference

### CharField

Text input field. The most common field type.

| Parameter    | Type          | Default | Description                                                            |
| ------------ | ------------- | ------- | ---------------------------------------------------------------------- |
| `max_length` | `int \| None` | `None`  | Maximum number of characters allowed. Adds `maxlength` HTML attribute. |
| `min_length` | `int \| None` | `None`  | Minimum number of characters required.                                 |
| `strip`      | `bool`        | `True`  | Strip leading and trailing whitespace before validation.               |

- **Default widget:** `text`
- **Normalizes to:** `str`
- **Empty value:** `""` (empty string)
- **Error message keys:** `required`, `max_length`, `min_length`

**Validation behavior:** If `required=True` and the value is empty (after stripping if `strip=True`), raises `"This field is required."`. If `max_length` is set and the value exceeds it, raises `"Ensure this value has at most {max_length} characters (it has {length})."`. Same pattern for `min_length`.

```python
from hyperdjango.forms import Form, CharField


class ProfileForm(Form):
    username = CharField(max_length=30, min_length=3)
    bio = CharField(max_length=500, required=False, widget="textarea")
    code = CharField(strip=False)  # Preserve whitespace


form = ProfileForm(data={"username": "alice", "bio": "", "code": "  x  "})
form.is_valid()  # True
form.cleaned_data["username"]  # "alice"
form.cleaned_data["bio"]  # ""
form.cleaned_data["code"]  # "  x  " (not stripped)
```

---

### IntegerField

Integer input field with optional range validation.

| Parameter   | Type          | Default | Description                        |
| ----------- | ------------- | ------- | ---------------------------------- |
| `min_value` | `int \| None` | `None`  | Minimum allowed value (inclusive). |
| `max_value` | `int \| None` | `None`  | Maximum allowed value (inclusive). |

- **Default widget:** `number`
- **Normalizes to:** `int`
- **Empty value:** `None`
- **Error message keys:** `required`, `invalid`, `min_value`, `max_value`

**Validation behavior:** Coerces the input string to `int`. If coercion fails, raises `"Enter a whole number."`. Then checks `min_value` and `max_value` bounds.

```python
from hyperdjango.forms import Form, IntegerField


class SettingsForm(Form):
    page_size = IntegerField(min_value=1, max_value=100)
    offset = IntegerField(min_value=0, required=False)


form = SettingsForm(data={"page_size": "25"})
form.is_valid()  # True
form.cleaned_data["page_size"]  # 25 (int, not str)
form.cleaned_data["offset"]  # None

# Invalid
form = SettingsForm(data={"page_size": "200"})
form.is_valid()  # False
form.errors  # {"page_size": ["Ensure this value is less than or equal to 100."]}
```

---

### FloatField

Floating-point number input with optional range validation.

| Parameter   | Type            | Default | Description                        |
| ----------- | --------------- | ------- | ---------------------------------- |
| `min_value` | `float \| None` | `None`  | Minimum allowed value (inclusive). |
| `max_value` | `float \| None` | `None`  | Maximum allowed value (inclusive). |

- **Default widget:** `number`
- **Normalizes to:** `float`
- **Empty value:** `None`
- **Error message keys:** `required`, `invalid`, `min_value`, `max_value`

**Validation behavior:** Coerces the input to `float`. If coercion fails (e.g., `"abc"`), raises `"Enter a number."`. Then checks `min_value` and `max_value` bounds.

```python
from hyperdjango.forms import Form, FloatField


class MeasurementForm(Form):
    temperature = FloatField(min_value=-273.15, max_value=1000.0)
    weight = FloatField(min_value=0.0, required=False)


form = MeasurementForm(data={"temperature": "36.6"})
form.is_valid()  # True
form.cleaned_data["temperature"]  # 36.6
```

---

### DecimalField

Decimal number input with precision control.

| Parameter        | Type              | Default | Description                                                      |
| ---------------- | ----------------- | ------- | ---------------------------------------------------------------- |
| `max_digits`     | `int \| None`     | `None`  | Maximum total number of digits (before and after decimal point). |
| `decimal_places` | `int \| None`     | `None`  | Maximum number of digits after the decimal point.                |
| `min_value`      | `Decimal \| None` | `None`  | Minimum allowed value (inclusive).                               |
| `max_value`      | `Decimal \| None` | `None`  | Maximum allowed value (inclusive).                               |

- **Default widget:** `number`
- **Normalizes to:** `decimal.Decimal`
- **Empty value:** `None`
- **Error message keys:** `required`, `invalid`, `max_value`, `min_value`, `max_digits`, `max_decimal_places`, `max_whole_digits`

**Validation behavior:** Coerces input to `Decimal`. Leading/trailing whitespace is ignored. Validates that total digit count does not exceed `max_digits`, that decimal places do not exceed `decimal_places`, and that the number of whole digits (total minus decimal) does not exceed the difference `max_digits - decimal_places`.

```python
from decimal import Decimal
from hyperdjango.forms import Form, DecimalField


class PriceForm(Form):
    price = DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    tax_rate = DecimalField(max_digits=5, decimal_places=4, required=False)


form = PriceForm(data={"price": "19.99"})
form.is_valid()  # True
form.cleaned_data["price"]  # Decimal("19.99")

# Too many decimal places
form = PriceForm(data={"price": "19.999"})
form.is_valid()  # False
form.errors  # {"price": ["Ensure that there are no more than 2 decimal places."]}
```

---

### BooleanField

Checkbox input. Note that `required` defaults to `False` for this field type, unlike all other fields.

- **Default widget:** `checkbox`
- **Normalizes to:** `bool` (`True` or `False`)
- **Empty value:** `False`
- **Error message keys:** `required`

**Validation behavior:** When `required=True`, the value must be `True` (the checkbox must be checked). When `required=False`, both `True` and `False` are valid. The string `"true"`, `"1"`, `"on"`, `"yes"` normalize to `True`. Everything else normalizes to `False`.

```python
from hyperdjango.forms import Form, BooleanField


class ConsentForm(Form):
    agree_tos = BooleanField(required=True)  # Must be checked
    subscribe = BooleanField(required=False)  # Optional checkbox


# Checkbox checked
form = ConsentForm(data={"agree_tos": "on", "subscribe": ""})
form.is_valid()  # True
form.cleaned_data["agree_tos"]  # True
form.cleaned_data["subscribe"]  # False

# Checkbox not checked but required
form = ConsentForm(data={"agree_tos": "", "subscribe": ""})
form.is_valid()  # False
form.errors  # {"agree_tos": ["This field is required."]}
```

---

### DateField

Date input that parses string input into `datetime.date` objects.

| Parameter       | Type                | Default | Description                                                                                     |
| --------------- | ------------------- | ------- | ----------------------------------------------------------------------------------------------- |
| `input_formats` | `list[str] \| None` | `None`  | List of `strftime` format strings to try when parsing. Defaults to `["%Y-%m-%d"]` (ISO format). |

- **Default widget:** `date`
- **Normalizes to:** `datetime.date`
- **Empty value:** `None`
- **Error message keys:** `required`, `invalid`

**Validation behavior:** Accepts `datetime.date` objects directly, or strings. Tries each format in `input_formats` until one matches. If none match, raises `"Enter a valid date."`.

```python
from hyperdjango.forms import Form, DateField


class EventForm(Form):
    start_date = DateField()
    end_date = DateField(input_formats=["%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"])


form = EventForm(data={"start_date": "2025-06-15", "end_date": "06/15/2025"})
form.is_valid()  # True
form.cleaned_data["start_date"]  # datetime.date(2025, 6, 15)
form.cleaned_data["end_date"]  # datetime.date(2025, 6, 15)

# Invalid date
form = EventForm(data={"start_date": "not-a-date", "end_date": "2025-06-15"})
form.is_valid()  # False
form.errors  # {"start_date": ["Enter a valid date."]}
```

---

### DateTimeField

Date and time input that parses into `datetime.datetime` objects.

| Parameter       | Type                | Default | Description                                                                     |
| --------------- | ------------------- | ------- | ------------------------------------------------------------------------------- |
| `input_formats` | `list[str] \| None` | `None`  | List of `strftime` format strings to try. ISO 8601 formats are always accepted. |

- **Default widget:** `datetime-local`
- **Normalizes to:** `datetime.datetime`
- **Empty value:** `None`
- **Error message keys:** `required`, `invalid`

**Validation behavior:** Accepts `datetime.datetime` or `datetime.date` objects directly, or strings. Always accepts ISO 8601 formats:

- `"2025-06-15 14:30:59"`
- `"2025-06-15T14:30:59"`
- `"2025-06-15T14:30"`
- `"2025-06-15T14:30Z"`
- `"2025-06-15T14:30+02:00"`
- `"2025-06-15"` (time defaults to midnight)

If `input_formats` is provided, those are tried in addition to ISO 8601 formats.

```python
from hyperdjango.forms import Form, DateTimeField


class LogEntryForm(Form):
    timestamp = DateTimeField()
    created = DateTimeField(input_formats=["%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p"])


form = LogEntryForm(
    data={
        "timestamp": "2025-06-15T14:30:00",
        "created": "06/15/2025 02:30 PM",
    }
)
form.is_valid()  # True
form.cleaned_data["timestamp"]  # datetime.datetime(2025, 6, 15, 14, 30)
```

---

### EmailField

Email address input. Inherits from `CharField` and adds email format validation.

- **Default widget:** `email`
- **Normalizes to:** `str`
- **Empty value:** `""` (empty string)
- **Error message keys:** `required`, `invalid`

**Validation behavior:** After `CharField` validation (strip, length checks), validates that the value matches a valid email address format. Uses SIMD-accelerated native email validation when available.

All `CharField` parameters (`max_length`, `min_length`, `strip`) are also accepted.

```python
from hyperdjango.forms import Form, EmailField


class SubscribeForm(Form):
    email = EmailField(max_length=254)
    backup_email = EmailField(required=False)


form = SubscribeForm(data={"email": "user@example.com"})
form.is_valid()  # True
form.cleaned_data["email"]  # "user@example.com"

# Invalid email
form = SubscribeForm(data={"email": "not-an-email"})
form.is_valid()  # False
form.errors  # {"email": ["Enter a valid email address."]}
```

---

### URLField

URL input field that validates proper URL format.

- **Default widget:** `url`
- **Normalizes to:** `str`
- **Empty value:** `""` (empty string)
- **Error message keys:** `required`, `invalid`

**Validation behavior:** Validates that the value is a properly formatted URL with a scheme (`http://` or `https://`). Inherits `CharField` parameters.

```python
from hyperdjango.forms import Form, URLField


class BookmarkForm(Form):
    url = URLField()
    title = CharField(max_length=200, required=False)


form = BookmarkForm(data={"url": "https://example.com/page"})
form.is_valid()  # True

form = BookmarkForm(data={"url": "not-a-url"})
form.is_valid()  # False
form.errors  # {"url": ["Enter a valid URL."]}
```

---

### ChoiceField

Dropdown select field. Validates that the submitted value is one of the defined choices.

| Parameter | Type                    | Default | Description                                                                                         |
| --------- | ----------------------- | ------- | --------------------------------------------------------------------------------------------------- |
| `choices` | `list[tuple[str, str]]` | `[]`    | List of `(value, label)` tuples. The value is what gets submitted; the label is what the user sees. |

- **Default widget:** `select`
- **Normalizes to:** `str`
- **Empty value:** `""` (empty string)
- **Error message keys:** `required`, `invalid_choice`

**Validation behavior:** After coercing to string, validates that the value exists in the list of choice values. The `invalid_choice` error message includes the invalid value.

```python
from hyperdjango.forms import Form, ChoiceField


class SurveyForm(Form):
    color = ChoiceField(
        choices=[
            ("", "-- Select --"),
            ("red", "Red"),
            ("green", "Green"),
            ("blue", "Blue"),
        ]
    )
    priority = ChoiceField(
        choices=[
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
        ]
    )


form = SurveyForm(data={"color": "red", "priority": "high"})
form.is_valid()  # True
form.cleaned_data["color"]  # "red"

# Invalid choice
form = SurveyForm(data={"color": "purple", "priority": "high"})
form.is_valid()  # False
form.errors  # {"color": ["Select a valid choice. purple is not one of the available choices."]}
```

**Grouped choices** are supported by nesting tuples:

```python
color = ChoiceField(
    choices=[
        (
            "Warm",
            [
                ("red", "Red"),
                ("orange", "Orange"),
            ],
        ),
        (
            "Cool",
            [
                ("blue", "Blue"),
                ("green", "Green"),
            ],
        ),
    ]
)
```

This renders as `<optgroup>` elements in the HTML select widget.

---

### MultipleChoiceField

Multiple-select field. Like `ChoiceField` but allows selecting more than one value.

| Parameter | Type                    | Default | Description            |
| --------- | ----------------------- | ------- | ---------------------- |
| `choices` | `list[tuple[str, str]]` | `[]`    | Same as `ChoiceField`. |

- **Default widget:** `select` (with `multiple` attribute)
- **Normalizes to:** `list[str]`
- **Empty value:** `[]` (empty list)
- **Error message keys:** `required`, `invalid_choice`, `invalid_list`

**Validation behavior:** Validates that every submitted value exists in the choice list. If any value is invalid, raises `invalid_choice` for that value.

```python
from hyperdjango.forms import Form, MultipleChoiceField


class TagForm(Form):
    tags = MultipleChoiceField(
        choices=[
            ("python", "Python"),
            ("rust", "Rust"),
            ("zig", "Zig"),
            ("go", "Go"),
        ]
    )


form = TagForm(data={"tags": ["python", "zig"]})
form.is_valid()  # True
form.cleaned_data["tags"]  # ["python", "zig"]
```

---

### FileField

File upload field.

- **Default widget:** `file`
- **Normalizes to:** The uploaded file object
- **Empty value:** `None`
- **Error message keys:** `required`, `invalid`, `missing`, `empty`

**Validation behavior:** Validates that a file was uploaded (for `required=True`). Checks that the file is not empty.

```python
from hyperdjango.forms import Form, FileField


class UploadForm(Form):
    document = FileField()
    avatar = FileField(required=False)


# In a view handler:
form = UploadForm(data=request.form, files=request.files)
if form.is_valid():
    uploaded = form.cleaned_data["document"]
```

---

### PasswordField

Password input. Identical to `CharField` but renders as a `<input type="password">`.

- **Default widget:** `password`
- **Normalizes to:** `str`
- **Empty value:** `""` (empty string)
- **Error message keys:** `required`, `max_length`, `min_length`

Inherits all `CharField` parameters (`max_length`, `min_length`, `strip`).

```python
from hyperdjango.forms import Form, PasswordField


class LoginForm(Form):
    username = CharField(max_length=150)
    password = PasswordField(min_length=8)
```

---

### HiddenField

Hidden input field. Not visible to the user. `required` defaults to `False`.

- **Default widget:** `hidden`
- **Normalizes to:** `str`
- **Empty value:** `""` (empty string)

```python
from hyperdjango.forms import Form, HiddenField, CharField


class EditForm(Form):
    id = HiddenField()
    name = CharField(max_length=100)


form = EditForm(data={"id": "42", "name": "Updated"}, initial={"id": "42"})
```

---

## Field Types Summary

| Field                 | Python Type | Default Widget    | Key Options                                              |
| --------------------- | ----------- | ----------------- | -------------------------------------------------------- |
| `CharField`           | `str`       | `text`            | `max_length`, `min_length`, `strip`                      |
| `IntegerField`        | `int`       | `number`          | `min_value`, `max_value`                                 |
| `FloatField`          | `float`     | `number`          | `min_value`, `max_value`                                 |
| `DecimalField`        | `Decimal`   | `number`          | `max_digits`, `decimal_places`, `min_value`, `max_value` |
| `BooleanField`        | `bool`      | `checkbox`        | (required defaults to `False`)                           |
| `DateField`           | `date`      | `date`            | `input_formats`                                          |
| `DateTimeField`       | `datetime`  | `datetime-local`  | `input_formats`                                          |
| `EmailField`          | `str`       | `email`           | Inherits `CharField` options                             |
| `URLField`            | `str`       | `url`             | Inherits `CharField` options                             |
| `ChoiceField`         | `str`       | `select`          | `choices=[(value, label), ...]`                          |
| `MultipleChoiceField` | `list[str]` | `select multiple` | `choices=[(value, label), ...]`                          |
| `FileField`           | file object | `file`            |                                                          |
| `PasswordField`       | `str`       | `password`        | Inherits `CharField` options                             |
| `HiddenField`         | `str`       | `hidden`          | (required defaults to `False`)                           |

---

## Form API Reference

### Constructor

```python
Form.__init__(
    data: dict[str, object] | None = None,
    initial: dict[str, object] | None = None,
    prefix: str | None = None,
    instance: object | None = None,
)
```

| Parameter  | Description                                                                                                                                                      |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data`     | Submitted data dictionary. Makes the form **bound** when provided. Keys are field names, values are the submitted values.                                        |
| `initial`  | Initial values for unbound form rendering. Not used as fallback during validation. If a field also defines `initial`, the form-level `initial` takes precedence. |
| `prefix`   | String prefix for field names in HTML output and submitted data keys. Useful when multiple forms appear on the same page.                                        |
| `instance` | Object to populate form with (used by `ModelForm` for updates).                                                                                                  |

```python
# Unbound form (renders empty)
form = ContactForm()

# Bound form (validates submitted data)
form = ContactForm(data={"name": "Alice", "email": "alice@example.com"})

# Initial values (display only, not validation fallback)
form = ContactForm(initial={"name": "Default Name"})

# Prefixed form (fields become "contact-name", "contact-email", etc.)
form = ContactForm(data=request_data, prefix="contact")
```

### Bound vs Unbound Forms

```python
form = ContactForm()  # unbound -- renders empty
form = ContactForm(data=request_data)  # bound -- validates data
form._is_bound  # True/False
```

An **unbound** form has no submitted data. It renders empty fields (or fields with `initial` values) and `is_valid()` always returns `False`. Its `errors` dictionary is empty.

A **bound** form has submitted data and can be validated. Only bound forms populate `cleaned_data` and `errors`.

Passing an empty dictionary creates a bound form with empty data:

```python
form = ContactForm(data={})
form._is_bound  # True -- this IS a bound form (with empty data)
```

Once created, a form's data is immutable. To change data, create a new form instance.

### is_valid()

```python
Form.is_valid() -> bool
```

Runs the full validation pipeline and returns `True` if the data is valid, `False` otherwise. Populates both `cleaned_data` (for valid fields) and `errors` (for invalid fields). Always returns `False` for unbound forms.

Validation runs only once -- subsequent calls to `is_valid()` return the cached result.

```python
form = ContactForm(
    data={"name": "Alice", "email": "alice@example.com", "message": "Hi"}
)
if form.is_valid():
    send_email(form.cleaned_data)
```

### errors

```python
Form.errors -> dict[str, list[str]]
```

Dictionary mapping field names to lists of error message strings. Accessing `errors` triggers validation if it has not already run (same as calling `is_valid()`).

```python
form = ContactForm(data={"name": "", "email": "bad"})
form.is_valid()  # False
form.errors
# {
#     "name": ["This field is required."],
#     "email": ["Enter a valid email address."],
# }
```

### cleaned_data

```python
Form.cleaned_data -> dict[str, object]
```

Dictionary of validated and normalized field values. Only available after `is_valid()` has been called. Contains entries only for fields that passed validation. For optional fields not provided in the input, the field's empty value is used (e.g., `""` for `CharField`, `None` for `DateField`).

```python
form = ContactForm(
    data={"name": "Alice", "email": "alice@example.com", "message": "Hi"}
)
form.is_valid()  # True
form.cleaned_data
# {"name": "Alice", "email": "alice@example.com", "message": "Hi", "age": None}
```

If validation fails, `cleaned_data` contains only the fields that passed:

```python
form = ContactForm(data={"name": "", "email": "bad", "message": "Hi"})
form.is_valid()  # False
form.cleaned_data  # {"message": "Hi"}
```

### fields

```python
Form.fields -> dict[str, FormField]
```

Dictionary of the form's declared field instances, keyed by field name.

```python
form = ContactForm()
form.fields["name"]  # <CharField max_length=100>
form.fields["email"]  # <EmailField>
```

### add_error()

```python
Form.add_error(field: str | None, message: str) -> None
```

Programmatically add an error to a specific field or to the form as a whole. When `field` is `None`, the error is treated as a non-field error. Calling `add_error()` automatically removes the field from `cleaned_data`.

```python
class RegistrationForm(Form):
    email = EmailField()

    def clean_email(self):
        email = self.cleaned_data["email"]
        if is_email_taken(email):
            self.add_error("email", "This email is already registered.")
        return email

    def clean(self):
        # Non-field error
        self.add_error(None, "Registration is currently closed.")
```

### has_error()

```python
Form.has_error(field: str, code: str | None = None) -> bool
```

Returns `True` if the specified field has any errors. If `code` is provided, returns `True` only if an error with that specific code exists.

```python
form.is_valid()
form.has_error("email")  # True if email has any error
form.has_error("email", "required")  # True only if "required" error
```

### non_field_errors()

```python
Form.non_field_errors() -> list[str]
```

Returns errors not associated with any specific field. These come from `clean()` raising `ValueError` or from `add_error(None, ...)`.

```python
form.is_valid()
form.non_field_errors()  # ["Passwords don't match", ...]
```

### errors_as_json()

```python
Form.errors_as_json() -> str
```

Returns all errors serialized as a JSON string. Useful for AJAX form validation.

```python
form.is_valid()
form.errors_as_json()
# '{"email": [{"message": "Enter a valid email address."}], "name": [{"message": "This field is required."}]}'
```

### get_json_data()

```python
Form.get_json_data() -> dict[str, list[dict[str, str]]]
```

Returns errors as a Python dictionary (before JSON serialization). Same structure as `errors_as_json()` but as native Python objects.

```python
form.is_valid()
form.get_json_data()
# {"email": [{"message": "Enter a valid email address."}]}
```

---

## Validation Lifecycle

When `form.is_valid()` is called, validation runs as a three-stage pipeline:

```
                         form.is_valid()
                              |
                              v
               +---------------------------------+
               |  Stage 1: Field.clean(value)    |
               |  For each field (declaration     |
               |  order):                         |
               |  1. Coerce to Python type        |
               |  2. Check required               |
               |  3. Check field constraints       |
               |     (max_length, min_value, etc.) |
               |  4. Run field validators          |
               +---------------------------------+
                              |
                    Field passes? ----No----> Record error,
                              |               skip to next field
                              v
               +---------------------------------+
               |  Stage 2: clean_<fieldname>()   |
               |  Per-field hook methods on the   |
               |  form class. Runs only if the    |
               |  field passed Stage 1.           |
               |  Read from self.cleaned_data.    |
               |  Return value replaces entry.    |
               +---------------------------------+
                              |
                    Hook passes? ----No----> Record error,
                              |               continue to next field
                              v
               +---------------------------------+
               |  Stage 3: clean()               |
               |  Form-level cross-field          |
               |  validation. All fields that     |
               |  passed Stages 1-2 are in        |
               |  self.cleaned_data.              |
               |  Raise ValueError for non-field  |
               |  errors.                         |
               +---------------------------------+
                              |
                              v
                    Any errors? ---Yes---> return False
                              |
                              v
                        return True
```

### Stage 1: Field.clean(value)

Each field's `clean()` method handles type coercion and constraint enforcement. For example, `IntegerField.clean("42")` coerces the string to `int(42)` and then checks `min_value`/`max_value`. If coercion fails or a constraint is violated, the field records an error and the pipeline moves to the next field.

### Stage 2: clean\_\<fieldname\>()

After all fields run their own `clean()`, the form checks for per-field hook methods named `clean_<fieldname>`. These run with `self.cleaned_data` already populated (for fields that passed Stage 1). The hook can perform custom validation, normalization, or domain-specific checks. The return value replaces the field's entry in `cleaned_data`.

If a field failed in Stage 1, its `clean_<fieldname>()` hook is **not** called.

### Stage 3: clean()

Finally, the form-level `clean()` method runs. This is for cross-field validation where multiple field values must be checked together. Raise `ValueError` to add a non-field error. You can also use `self.add_error(field, message)` to attach errors to specific fields.

The `clean()` method runs even if some fields had errors in earlier stages. Check for the presence of keys in `self.cleaned_data` before accessing them.

---

## Per-Field Validation Hooks

Define `clean_<fieldname>()` methods on your form class to add custom validation for individual fields. The method takes no parameters -- read the value from `self.cleaned_data["fieldname"]`. Return the (possibly transformed) value to store back into `cleaned_data`.

```python
class RegistrationForm(Form):
    username = CharField(max_length=50)
    email = EmailField()
    password = PasswordField(min_length=8)
    password_confirm = PasswordField(min_length=8)

    def clean_username(self):
        """Normalize username and check uniqueness."""
        username = self.cleaned_data["username"]
        normalized = username.lower().strip()
        if is_username_taken(normalized):
            raise ValueError("This username is already taken.")
        return normalized

    def clean_email(self):
        """Reject disposable email providers."""
        email = self.cleaned_data["email"]
        domain = email.split("@")[1]
        if domain in DISPOSABLE_DOMAINS:
            raise ValueError("Please use a non-disposable email address.")
        return email

    def clean(self):
        """Cross-field: passwords must match."""
        password = self.cleaned_data.get("password")
        confirm = self.cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise ValueError("Passwords do not match.")
```

**Using add_error() instead of raising:**

Inside `clean_<fieldname>()` or `clean()`, you can use `self.add_error()` as an alternative to raising `ValueError`. This is useful when you want to report multiple errors:

```python
def clean(self):
    start = self.cleaned_data.get("start_date")
    end = self.cleaned_data.get("end_date")
    if start and end:
        if end < start:
            self.add_error("end_date", "End date must be after start date.")
        if (end - start).days > 365:
            self.add_error("end_date", "Date range cannot exceed one year.")
```

---

## Error Handling Methods

Complete reference for programmatic error management:

```python
# Add error to a specific field
form.add_error("email", "This email is already registered")

# Add non-field error (applies to the form as a whole)
form.add_error(None, "General error message")

# Check if a field has any errors
form.has_error("email")  # True

# Check for a specific error code
form.has_error("email", "required")  # True/False

# Get non-field errors
form.non_field_errors()  # ["General error message"]

# Serialize all errors as JSON string
form.errors_as_json()  # '{"email": [{"message": "..."}]}'

# Get errors as Python dict (pre-serialization)
form.get_json_data()  # {"email": [{"message": "..."}]}
```

Using `add_error()` inside `clean_<fieldname>()` or `clean()` is an alternative to raising `ValueError`. Passing `None` as the field name adds a non-field error. Calling `add_error()` on a field automatically removes that field from `cleaned_data`.

---

## Form Rendering

Forms render themselves as HTML. The output does **not** include `<form>` tags or submit buttons -- you provide those in your template.

### as_div()

Each field wrapped in a `<div class="form-group">` with label, input, help text, and errors:

```python
form = ContactForm()
html = form.as_div()
```

```html
<div class="form-group">
  <label for="id_name">Name</label>
  <input type="text" name="name" id="id_name" maxlength="100" required />
</div>
<div class="form-group">
  <label for="id_email">Email</label>
  <input type="email" name="email" id="id_email" required />
</div>
<div class="form-group">
  <label for="id_message">Message</label>
  <textarea name="message" id="id_message" required></textarea>
</div>
```

With errors (bound form that failed validation):

```html
<div class="form-group">
  <label for="id_email">Email</label>
  <ul class="errorlist">
    <li>Enter a valid email address.</li>
  </ul>
  <input
    type="email"
    name="email"
    id="id_email"
    value="bad-email"
    required
    aria-invalid="true"
  />
</div>
```

### as_table()

Each field as a table row. Wrap in `<table>` tags yourself:

```python
html = form.as_table()
```

```html
<tr>
  <th><label for="id_name">Name</label></th>
  <td>
    <input type="text" name="name" id="id_name" maxlength="100" required />
  </td>
</tr>
<tr>
  <th><label for="id_email">Email</label></th>
  <td><input type="email" name="email" id="id_email" required /></td>
</tr>
```

### as_p()

Each field wrapped in a `<p>` tag:

```python
html = form.as_p()
```

```html
<p>
  <label for="id_name">Name</label>
  <input type="text" name="name" id="id_name" maxlength="100" required />
</p>
<p>
  <label for="id_email">Email</label>
  <input type="email" name="email" id="id_email" required />
</p>
```

### Rendering individual fields

Access individual fields for custom layout:

```python
form = ContactForm(data=request_data)
# Each field renders its HTML input
str(form["name"])  # '<input type="text" name="name" ...>'
str(form["email"])  # '<input type="email" name="email" ...>'
```

### Help text rendering

When `help_text` is set, it is rendered below the input and linked via `aria-describedby`:

```html
<div class="form-group">
  <label for="id_email">Email</label>
  <div class="helptext" id="id_email_helptext">
    We'll never share your email.
  </div>
  <input
    type="email"
    name="email"
    id="id_email"
    required
    aria-describedby="id_email_helptext"
  />
</div>
```

---

## Widgets

Widgets control how a form field renders as HTML. Every field has a default widget, but you can override it with the `widget` parameter.

### Available Widgets

| Widget           | HTML Element                    | Used By Default                              |
| ---------------- | ------------------------------- | -------------------------------------------- |
| `text`           | `<input type="text">`           | `CharField`                                  |
| `textarea`       | `<textarea>`                    | `CharField` (when overridden)                |
| `number`         | `<input type="number">`         | `IntegerField`, `FloatField`, `DecimalField` |
| `email`          | `<input type="email">`          | `EmailField`                                 |
| `url`            | `<input type="url">`            | `URLField`                                   |
| `password`       | `<input type="password">`       | `PasswordField`                              |
| `hidden`         | `<input type="hidden">`         | `HiddenField`                                |
| `checkbox`       | `<input type="checkbox">`       | `BooleanField`                               |
| `select`         | `<select>`                      | `ChoiceField`, `MultipleChoiceField`         |
| `radio`          | `<input type="radio">` (group)  | `ChoiceField` (when overridden)              |
| `date`           | `<input type="date">`           | `DateField`                                  |
| `datetime-local` | `<input type="datetime-local">` | `DateTimeField`                              |
| `file`           | `<input type="file">`           | `FileField`                                  |

### Overriding Widgets

```python
class MyForm(Form):
    # Use textarea instead of text input
    description = CharField(widget="textarea")

    # Use radio buttons instead of dropdown
    size = ChoiceField(
        choices=[("s", "Small"), ("m", "Medium"), ("l", "Large")],
        widget="radio",
    )

    # Use hidden input
    token = CharField(widget="hidden")
```

### Widget HTML Attributes

Use the `attrs` parameter to add HTML attributes to the rendered widget:

```python
class StyledForm(Form):
    name = CharField(
        attrs={
            "class": "form-control",
            "placeholder": "Enter your name",
            "autofocus": "true",
        }
    )
    bio = CharField(
        widget="textarea",
        attrs={
            "class": "form-control",
            "rows": "5",
            "cols": "40",
        },
    )
    score = IntegerField(
        attrs={
            "class": "form-control",
            "step": "1",
            "min": "0",
            "max": "100",
        }
    )
```

### Widget Rendering Details

**text**: `<input type="text" name="{name}" value="{value}" {attrs}>`

**textarea**: `<textarea name="{name}" {attrs}>{value}</textarea>`

**number**: `<input type="number" name="{name}" value="{value}" {attrs}>`

**email**: `<input type="email" name="{name}" value="{value}" {attrs}>`

**password**: `<input type="password" name="{name}" {attrs}>` (value never pre-filled for security)

**hidden**: `<input type="hidden" name="{name}" value="{value}">`

**checkbox**: `<input type="checkbox" name="{name}" {checked} {attrs}>`

**select**:

```html
<select name="{name}" {attrs}>
  <option value="val1" {selected}>Label 1</option>
  <option value="val2">Label 2</option>
</select>
```

**radio**:

```html
<div>
  <label
    ><input type="radio" name="{name}" value="val1" {checked} /> Label 1</label
  >
  <label><input type="radio" name="{name}" value="val2" /> Label 2</label>
</div>
```

**date**: `<input type="date" name="{name}" value="{value}" {attrs}>`

**datetime-local**: `<input type="datetime-local" name="{name}" value="{value}" {attrs}>`

**file**: `<input type="file" name="{name}" {attrs}>`

---

## ModelForm

Auto-generates form fields from a HyperApp Model. Field types are inferred from model annotations.

```python
from hyperdjango.forms import ModelForm


class UserForm(ModelForm):
    class Meta:
        model = User
        fields = ["username", "email", "age"]


form = UserForm(data=request_data)
if form.is_valid():
    user = await form.save()  # INSERT (new) or UPDATE (if instance set)
    user = await form.save(db=db)  # Use specific db connection
```

### Meta Options

| Option           | Type                        | Description                                                                      |
| ---------------- | --------------------------- | -------------------------------------------------------------------------------- |
| `model`          | `type`                      | The HyperApp Model class to generate fields from. Required.                      |
| `fields`         | `list[str]`                 | Explicit list of model fields to include. Use `"__all__"` to include all fields. |
| `exclude`        | `list[str]`                 | List of model fields to exclude. Use `fields` or `exclude`, not both.            |
| `widgets`        | `dict[str, str]`            | Dict mapping field names to widget type overrides.                               |
| `labels`         | `dict[str, str]`            | Dict mapping field names to custom label strings.                                |
| `help_texts`     | `dict[str, str]`            | Dict mapping field names to custom help text strings.                            |
| `error_messages` | `dict[str, dict[str, str]]` | Nested dict mapping field names to error message overrides.                      |

```python
class UserForm(ModelForm):
    class Meta:
        model = User
        fields = ["username", "email", "bio", "role"]
        widgets = {
            "bio": "textarea",
            "role": "radio",
        }
        labels = {
            "username": "User Name",
            "bio": "Biography",
        }
        help_texts = {
            "username": "Letters, digits, and underscores only.",
            "bio": "Tell us about yourself (max 500 characters).",
        }
        error_messages = {
            "username": {
                "required": "You must choose a username.",
            },
        }
```

### Type Mapping

Model field annotations are automatically mapped to form field types:

| Python Type     | FormField       | Default Widget                                      |
| --------------- | --------------- | --------------------------------------------------- |
| `str`           | `CharField`     | `text`                                              |
| `int`           | `IntegerField`  | `number`                                            |
| `float`         | `FloatField`    | `number`                                            |
| `bool`          | `BooleanField`  | `checkbox`                                          |
| `Decimal`       | `DecimalField`  | `number`                                            |
| `date`          | `DateField`     | `date`                                              |
| `datetime`      | `DateTimeField` | `datetime-local`                                    |
| `Enum` subclass | `ChoiceField`   | `select` (choices auto-populated from enum members) |

### Adding Extra Fields

You can add explicit fields alongside auto-generated ones:

```python
class UserForm(ModelForm):
    confirm_password = PasswordField(min_length=8)

    class Meta:
        model = User
        fields = ["username", "email"]

    def clean(self):
        # The extra field is available in cleaned_data
        if self.cleaned_data.get("confirm_password") != some_value:
            raise ValueError("Password confirmation failed.")
```

### Saving

`ModelForm.save()` creates or updates a model instance:

```python
# Create new instance
form = UserForm(data=request_data)
if form.is_valid():
    user = await form.save()  # INSERT

# Update existing instance
form = UserForm(data=request_data, instance=existing_user)
if form.is_valid():
    user = await form.save()  # UPDATE

# Use specific database connection
user = await form.save(db=db)
```

When `instance` is provided, the form pre-populates with the instance's current values and `save()` performs an UPDATE. Without `instance`, `save()` performs an INSERT.

---

## FormSet

Manage multiple form instances as a group. Useful for editing lists of items, bulk creation, or tabular data entry.

```python
from hyperdjango.forms import FormSet


class ItemForm(Form):
    name = CharField(max_length=100)
    quantity = IntegerField(min_value=0)
```

### Constructor

```python
FormSet(
    form_class: type[Form],
    data: list[dict[str, object]] | None = None,
    extra: int = 1,
    max_num: int | None = None,
    min_num: int = 0,
    can_delete: bool = False,
    can_order: bool = False,
    prefix: str = "form",
)
```

| Parameter    | Type          | Default    | Description                                                                                    |
| ------------ | ------------- | ---------- | ---------------------------------------------------------------------------------------------- |
| `form_class` | `type[Form]`  | (required) | The form class to use for each form in the set.                                                |
| `data`       | `list[dict]`  | `None`     | List of data dictionaries, one per form. Makes the formset bound.                              |
| `extra`      | `int`         | `1`        | Number of extra blank forms to display.                                                        |
| `max_num`    | `int \| None` | `None`     | Maximum total number of forms allowed. `None` means no limit.                                  |
| `min_num`    | `int`         | `0`        | Minimum number of valid (non-empty) forms required. Fewer triggers a formset-level error.      |
| `can_delete` | `bool`        | `False`    | If `True`, each form gets a `DELETE` flag. Forms with `DELETE=True` are excluded from results. |
| `can_order`  | `bool`        | `False`    | If `True`, each form gets an `ORDER` field for explicit ordering.                              |
| `prefix`     | `str`         | `"form"`   | Prefix for field names in HTML output.                                                         |

### Usage

```python
formset = FormSet(
    ItemForm,
    data=[
        {"name": "Widget", "quantity": 5},
        {"name": "Gadget", "quantity": 3},
    ],
    extra=2,
    max_num=10,
    min_num=1,
    can_delete=True,
    prefix="items",
)

if formset.is_valid():
    for form in formset:
        print(form.cleaned_data)
    # {"name": "Widget", "quantity": 5}
    # {"name": "Gadget", "quantity": 3}
```

### FormSet Properties and Methods

```python
formset.is_valid()  # bool -- validates all forms and formset constraints
formset.cleaned_data  # list of cleaned data dicts (one per valid form)
formset.errors  # list of error dicts (one per form, empty dict if no errors)
formset.total_form_count  # Total forms including blanks and deleted
formset.non_form_errors()  # Formset-level errors (e.g., min_num violation, max_num exceeded)
formset.deleted_forms  # List of forms flagged for deletion (when can_delete=True)
```

### Validation

FormSet validation runs in two phases:

1. **Per-form validation:** Each form's `is_valid()` is called. Forms with all-empty data are skipped (treated as blank extras).

2. **Formset-level validation:** After per-form validation:
   - If fewer valid forms than `min_num`, adds non-form error.
   - If more forms than `max_num`, adds non-form error.
   - Custom formset validation can be added by subclassing.

```python
formset = FormSet(
    ItemForm,
    data=[],  # No data submitted
    min_num=1,  # At least 1 form required
)
formset.is_valid()  # False
formset.non_form_errors()  # ["Please submit at least 1 form."]
```

### Deletion

When `can_delete=True`, forms include a `DELETE` field. Forms flagged for deletion are available via `formset.deleted_forms` and are excluded from the main iteration:

```python
formset = FormSet(
    ItemForm,
    data=[
        {"name": "Widget", "quantity": 5, "DELETE": False},
        {"name": "Old Item", "quantity": 0, "DELETE": True},
    ],
    can_delete=True,
)
if formset.is_valid():
    for form in formset:
        # Only yields "Widget" -- "Old Item" is excluded
        print(form.cleaned_data)

    for deleted in formset.deleted_forms:
        # Process deletions
        print(f"Deleting: {deleted.cleaned_data}")
```

---

## ModelFormSet

FormSet for creating/updating model instances in bulk.

```python
from hyperdjango.forms import ModelFormSet, ModelForm


class ItemForm(ModelForm):
    class Meta:
        model = Item
        fields = ["name", "value"]


formset = ModelFormSet(
    ItemForm,
    data=[
        {"name": "A", "value": 1},
        {"name": "B", "value": 2},
    ],
)
if formset.is_valid():
    items = await formset.save()  # Returns list of created/updated instances
```

When forms include an `id` or primary key field, existing instances are updated. Forms without a primary key create new instances. Forms with `DELETE=True` trigger deletion of the corresponding instance.

---

## Cross-Field Validation

Override `clean()` for validation that spans multiple fields. This method runs after all individual field validations pass. Use `self.cleaned_data.get()` to safely access values (some fields may have failed validation and won't be present).

```python
class RegistrationForm(Form):
    password = PasswordField(min_length=8)
    confirm = PasswordField(min_length=8)
    start_date = DateField()
    end_date = DateField()

    def clean(self):
        # Password confirmation
        if self.cleaned_data.get("password") != self.cleaned_data.get("confirm"):
            raise ValueError("Passwords do not match")

        # Date range validation
        start = self.cleaned_data.get("start_date")
        end = self.cleaned_data.get("end_date")
        if start and end and end <= start:
            self.add_error("end_date", "End date must be after start date.")
```

**Raising ValueError vs add_error:**

| Method                           | Effect                                                    |
| -------------------------------- | --------------------------------------------------------- |
| `raise ValueError("msg")`        | Adds a **non-field error** and stops `clean()` execution. |
| `self.add_error("field", "msg")` | Adds error to a **specific field**. Execution continues.  |
| `self.add_error(None, "msg")`    | Adds a **non-field error**. Execution continues.          |

Use `raise ValueError` when the error is fatal and no further cross-field checks make sense. Use `add_error()` when you want to report multiple issues.

---

## Complete Example

A real-world form combining multiple features:

```python
from hyperdjango.forms import (
    Form,
    ModelForm,
    CharField,
    EmailField,
    IntegerField,
    ChoiceField,
    BooleanField,
    DateField,
    PasswordField,
    DecimalField,
)


class OrderForm(Form):
    # Customer info
    name = CharField(max_length=200, label="Full Name")
    email = EmailField(help_text="Order confirmation will be sent here")
    phone = CharField(max_length=20, required=False)

    # Order details
    product = ChoiceField(
        choices=[
            ("basic", "Basic Plan - $9.99"),
            ("pro", "Pro Plan - $29.99"),
            ("enterprise", "Enterprise - $99.99"),
        ]
    )
    quantity = IntegerField(min_value=1, max_value=100, initial=1)
    discount_code = CharField(max_length=20, required=False)
    amount = DecimalField(max_digits=10, decimal_places=2, min_value=0)

    # Shipping
    ship_date = DateField(help_text="Earliest available shipping date")
    express = BooleanField(required=False, label="Express Shipping (+$15)")

    # Terms
    agree_tos = BooleanField(required=True, label="I agree to the Terms of Service")

    def clean_discount_code(self):
        code = self.cleaned_data["discount_code"]
        if code and not is_valid_discount(code):
            raise ValueError("Invalid or expired discount code.")
        return code.upper() if code else code

    def clean_email(self):
        email = self.cleaned_data["email"]
        return email.lower()

    def clean(self):
        product = self.cleaned_data.get("product")
        quantity = self.cleaned_data.get("quantity")
        amount = self.cleaned_data.get("amount")

        if product and quantity and amount:
            expected = calculate_total(product, quantity)
            if amount != expected:
                self.add_error("amount", f"Expected total is {expected}")

        ship_date = self.cleaned_data.get("ship_date")
        if ship_date and ship_date < date.today():
            self.add_error("ship_date", "Ship date cannot be in the past.")
```

Usage in a view:

```python
async def create_order(request):
    form = OrderForm(data=request.json)
    if not form.is_valid():
        return Response.json({"errors": form.get_json_data()}, status=400)

    order = await Order.objects.create(**form.cleaned_data)
    return Response.json({"id": order.id, "status": "created"}, status=201)
```

---

## Form Prefixes

When multiple forms appear on the same page, use `prefix` to namespace field names and avoid collisions:

```python
billing = AddressForm(data=request_data, prefix="billing")
shipping = AddressForm(data=request_data, prefix="shipping")

# In HTML, fields become:
# billing-street, billing-city, billing-zip
# shipping-street, shipping-city, shipping-zip

if billing.is_valid() and shipping.is_valid():
    process_order(billing.cleaned_data, shipping.cleaned_data)
```

---

## Validators

Fields accept a `validators` parameter for reusable validation functions:

```python
def validate_even(value):
    if value % 2 != 0:
        raise ValueError("Value must be even.")


def validate_no_profanity(value):
    if contains_profanity(value):
        raise ValueError("Please keep it clean.")


class MyForm(Form):
    count = IntegerField(validators=[validate_even])
    comment = CharField(max_length=500, validators=[validate_no_profanity])
```

Validators run after the field's built-in type coercion and constraint checks. Multiple validators run in order; all errors are collected.
