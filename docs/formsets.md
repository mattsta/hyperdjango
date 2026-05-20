# FormSets

Handle multiple instances of the same form on a single page. FormSets manage the creation, validation, and processing of a collection of identical forms -- useful for bulk creation, inline editing, and tabular data entry.

## Basic FormSet

```python
from hyperdjango.forms import Form, CharField, FormSet


class ItemForm(Form):
    name = CharField(max_length=100)
    quantity = CharField(max_length=10)


# Create a FormSet factory with 3 empty forms
ItemFormSet = FormSet(ItemForm, extra=3)


@app.get("/items/bulk-create")
async def bulk_create_form(request):
    formset = ItemFormSet()
    return render(request, "items/bulk_create.html", {"formset": formset})


@app.post("/items/bulk-create")
async def bulk_create(request):
    data = await request.form_data()
    formset = ItemFormSet(data=data)
    if formset.is_valid():
        for form_data in formset.cleaned_data:
            if form_data:  # skip empty forms
                await Item.objects.create(**form_data)
        return redirect("/items/")
    return render(request, "items/bulk_create.html", {"formset": formset}, status=400)
```

## FormSet Factory Options

```python
FormSet(
    form_class,  # The Form class to repeat
    extra=1,  # Number of extra empty forms
    max_num=None,  # Maximum total forms allowed
    min_num=0,  # Minimum number of forms that must be filled
    can_delete=False,  # Add a DELETE checkbox to each form
    can_order=False,  # Add an ORDER field to each form
    validate_max=False,  # Enforce max_num during validation
    validate_min=False,  # Enforce min_num during validation
)
```

### Option details

| Parameter      | Type          | Default    | Description                                                                    |
| -------------- | ------------- | ---------- | ------------------------------------------------------------------------------ |
| `form_class`   | `type[Form]`  | (required) | The Form class that each form in the set is an instance of                     |
| `extra`        | `int`         | `1`        | Number of additional empty forms displayed beyond any initial data             |
| `max_num`      | `int \| None` | `None`     | Upper limit on total forms. `None` means no limit.                             |
| `min_num`      | `int`         | `0`        | Minimum number of forms that must contain data                                 |
| `can_delete`   | `bool`        | `False`    | Adds a `DELETE` boolean field to each form                                     |
| `can_order`    | `bool`        | `False`    | Adds an `ORDER` integer field to each form                                     |
| `validate_max` | `bool`        | `False`    | If True, raises a validation error when submitted form count exceeds `max_num` |
| `validate_min` | `bool`        | `False`    | If True, raises a validation error when filled form count is below `min_num`   |

### Examples

```python
# Allow up to 10 items, require at least 1
ItemFormSet = FormSet(
    ItemForm,
    extra=3,
    min_num=1,
    max_num=10,
    validate_min=True,
    validate_max=True,
)

# Editable list with delete checkboxes
EditableItemFormSet = FormSet(
    ItemForm,
    extra=0,
    can_delete=True,
)

# Orderable list
OrderableItemFormSet = FormSet(
    ItemForm,
    extra=0,
    can_order=True,
)
```

## FormSet Validation

### is_valid()

Call `is_valid()` to validate all forms in the set. Returns `True` only if every non-empty form passes validation and the formset-level constraints (min_num, max_num) are met:

```python
formset = ItemFormSet(data=form_data)
if formset.is_valid():
    # All forms passed validation
    for cleaned in formset.cleaned_data:
        print(cleaned)  # {"name": "Widget", "quantity": "5"}
else:
    # Validation failed
    print(formset.errors)
```

### errors

`formset.errors` is a list of error dictionaries, one per form. Each dictionary maps field names to lists of error messages:

```python
formset = ItemFormSet(data=invalid_data)
formset.is_valid()  # False

# errors is a list with one dict per form
print(formset.errors)
# [
#     {},                                     # Form 0: valid
#     {"name": ["This field is required."]},  # Form 1: missing name
#     {},                                     # Form 2: empty (skipped)
# ]
```

### non_form_errors

Errors that apply to the formset as a whole (not to any individual form) are in `non_form_errors`:

```python
formset = ItemFormSet(data=too_many_forms)
formset.is_valid()

print(formset.non_form_errors)
# ["Please submit at most 10 forms."]
```

Non-form errors are raised when:

- `validate_max=True` and more than `max_num` forms are submitted
- `validate_min=True` and fewer than `min_num` forms contain data

### cleaned_data

After successful validation, `cleaned_data` is a list of dictionaries with the validated and cleaned data from each form. Empty forms (where all fields are blank) return empty dicts:

```python
formset = ItemFormSet(data=form_data)
if formset.is_valid():
    for i, cleaned in enumerate(formset.cleaned_data):
        if cleaned:  # Skip empty forms
            print(f"Form {i}: {cleaned}")
            # Form 0: {"name": "Widget", "quantity": "5"}
            # Form 1: {"name": "Gadget", "quantity": "3"}
```

## Template Usage

### Rendering the formset

```html
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />

  <!-- Management form data (required) -->
  <input
    type="hidden"
    name="form-TOTAL_FORMS"
    value="{{ formset.total_form_count }}"
  />
  <input
    type="hidden"
    name="form-INITIAL_FORMS"
    value="{{ formset.initial_form_count }}"
  />

  {% for form in formset.forms %}
  <fieldset>
    <legend>Item {{ loop.index }}</legend>
    {% for field in form.fields %}
    <label>{{ field.label }}</label>
    <input
      type="{{ field.widget }}"
      name="{{ field.html_name }}"
      value="{{ field.value }}"
    />
    {% if field.errors %}
    <span class="error">{{ field.errors[0] }}</span>
    {% endif %} {% endfor %}
  </fieldset>
  {% endfor %} {% if formset.non_form_errors %}
  <div class="errors">
    {% for error in formset.non_form_errors %}
    <p class="error">{{ error }}</p>
    {% endfor %}
  </div>
  {% endif %}

  <button type="submit">Save All</button>
</form>
```

### Management form

The management form is a set of hidden inputs that tell the formset how many forms were submitted. These are required for proper formset processing:

| Field                | Purpose                                              |
| -------------------- | ---------------------------------------------------- |
| `form-TOTAL_FORMS`   | Total number of forms in the submission              |
| `form-INITIAL_FORMS` | Number of forms with initial data (existing records) |

Without these fields, the formset cannot determine how many forms to process.

### Form numbering

Each form's fields are prefixed with a form number to avoid name collisions. For a form at index 0 with a field named `name`, the HTML name is `form-0-name`:

```html
<!-- Form 0 -->
<input type="text" name="form-0-name" value="Widget" />
<input type="text" name="form-0-quantity" value="5" />

<!-- Form 1 -->
<input type="text" name="form-1-name" value="Gadget" />
<input type="text" name="form-1-quantity" value="3" />

<!-- Form 2 (extra, empty) -->
<input type="text" name="form-2-name" value="" />
<input type="text" name="form-2-quantity" value="" />
```

### Dynamic form adding with JavaScript

To allow users to add more forms dynamically, clone a form template and update the form index:

```html
<div id="formset-container">
  {% for form in formset.forms %}
  <fieldset class="form-row" data-index="{{ loop.index0 }}">
    <legend>Item {{ loop.index }}</legend>
    {% for field in form.fields %}
    <label>{{ field.label }}</label>
    <input
      type="{{ field.widget }}"
      name="{{ field.html_name }}"
      value="{{ field.value }}"
    />
    {% endfor %}
  </fieldset>
  {% endfor %}
</div>

<button type="button" id="add-form">Add Another Item</button>

<script>
  document.getElementById("add-form").addEventListener("click", function () {
    const container = document.getElementById("formset-container");
    const totalForms = document.querySelector("[name='form-TOTAL_FORMS']");
    const currentCount = parseInt(totalForms.value);

    // Clone the last form row
    const lastRow = container.querySelector(".form-row:last-child");
    const newRow = lastRow.cloneNode(true);

    // Update field names and IDs with new index
    newRow
      .querySelectorAll("input, select, textarea")
      .forEach(function (input) {
        input.name = input.name.replace(
          /form-\d+-/,
          "form-" + currentCount + "-",
        );
        input.value = ""; // Clear values
      });

    // Update legend
    newRow.querySelector("legend").textContent = "Item " + (currentCount + 1);

    container.appendChild(newRow);
    totalForms.value = currentCount + 1;
  });
</script>
```

## ModelFormSet

A ModelFormSet binds a formset to a database model, enabling bulk create and edit operations with automatic saving:

```python
from hyperdjango.forms import ModelForm, ModelFormSet


class ItemForm(ModelForm):
    class Meta:
        model = Item
        fields = ["name", "quantity", "price"]


# Create a ModelFormSet
ItemModelFormSet = ModelFormSet(ItemForm, extra=3)
```

### Loading existing data

```python
@app.get("/items/edit")
async def edit_items(request):
    items = await Item.objects.all()
    formset = ItemModelFormSet(initial=items)
    return render(request, "items/edit.html", {"formset": formset})
```

### Saving the formset

```python
@app.post("/items/edit")
async def save_items(request):
    data = await request.form_data()
    formset = ItemModelFormSet(data=data)
    if formset.is_valid():
        await formset.save()  # Creates new records, updates existing ones
        return redirect("/items/")
    return render(request, "items/edit.html", {"formset": formset}, status=400)
```

The `save()` method:

- Updates existing records (those with an `id` in the form data)
- Creates new records (forms without an `id`)
- Deletes records marked for deletion (if `can_delete=True`)

### Filtering the queryset

```python
@app.get("/items/edit")
async def edit_items(request):
    # Only show items belonging to the current user
    items = await Item.objects.filter(owner_id=request.user.id).all()
    formset = ItemModelFormSet(initial=items, extra=2)
    return render(request, "items/edit.html", {"formset": formset})
```

## Deletion Workflow

When `can_delete=True`, each form gets a `DELETE` checkbox. When checked, the form's data is included in `deleted_forms` instead of `cleaned_data`.

### Setup

```python
ItemFormSet = FormSet(ItemForm, extra=0, can_delete=True)
```

### Template

```html
{% for form in formset.forms %}
<fieldset>
  <legend>Item {{ loop.index }}</legend>
  {% for field in form.fields %} {% if field.name == "DELETE" %}
  <label>
    <input type="checkbox" name="{{ field.html_name }}" value="true" />
    Delete this item
  </label>
  {% else %}
  <label>{{ field.label }}</label>
  <input
    type="{{ field.widget }}"
    name="{{ field.html_name }}"
    value="{{ field.value }}"
  />
  {% endif %} {% endfor %}
</fieldset>
{% endfor %}
```

### Processing deletions

```python
@app.post("/items/edit")
async def edit_items(request):
    data = await request.form_data()
    items = await Item.objects.all()
    formset = ItemFormSet(data=data, initial=items)

    if formset.is_valid():
        # Process items marked for deletion
        for form_data in formset.deleted_forms:
            item_id = form_data.get("id")
            if item_id:
                item = await Item.objects.get(id=item_id)
                await item.delete()

        # Process remaining (non-deleted) items
        for form_data in formset.cleaned_data:
            if form_data and form_data not in formset.deleted_forms:
                await Item.objects.create(**form_data)

        return redirect("/items/")
```

## Ordering Workflow

When `can_order=True`, each form gets an `ORDER` integer field. The formset sorts forms by this value.

### Setup

```python
OrderableFormSet = FormSet(ItemForm, extra=0, can_order=True)
```

### Template

```html
{% for form in formset.forms %}
<fieldset>
  <legend>Item {{ loop.index }}</legend>
  {% for field in form.fields %} {% if field.name == "ORDER" %}
  <label>Position</label>
  <input type="number" name="{{ field.html_name }}" value="{{ field.value }}" />
  {% else %}
  <label>{{ field.label }}</label>
  <input
    type="{{ field.widget }}"
    name="{{ field.html_name }}"
    value="{{ field.value }}"
  />
  {% endif %} {% endfor %}
</fieldset>
{% endfor %}
```

### Processing ordered data

```python
@app.post("/items/reorder")
async def reorder_items(request):
    data = await request.form_data()
    formset = OrderableFormSet(data=data)

    if formset.is_valid():
        # ordered_forms returns cleaned_data sorted by ORDER value
        for position, form_data in enumerate(formset.ordered_forms):
            item_id = form_data["id"]
            await Item.objects.filter(id=item_id).update(position=position)

        return redirect("/items/")
```

## Practical Patterns

### Inline editing (edit existing + add new)

```python
@app.get("/projects/{id:int}/tasks")
async def edit_tasks(request, id: int):
    project = await Project.objects.get(id=id)
    tasks = await Task.objects.filter(project_id=project.id).all()

    TaskFormSet = FormSet(TaskForm, extra=3, can_delete=True)
    formset = TaskFormSet(initial=tasks)
    return render(
        request,
        "tasks/edit.html",
        {
            "project": project,
            "formset": formset,
        },
    )


@app.post("/projects/{id:int}/tasks")
async def save_tasks(request, id: int):
    data = await request.form_data()
    tasks = await Task.objects.filter(project_id=id).all()

    TaskFormSet = FormSet(TaskForm, extra=3, can_delete=True)
    formset = TaskFormSet(data=data, initial=tasks)

    if formset.is_valid():
        # Delete marked items
        for form_data in formset.deleted_forms:
            if form_data.get("id"):
                await Task.objects.filter(id=form_data["id"]).delete()

        # Save remaining items
        for form_data in formset.cleaned_data:
            if form_data and form_data not in formset.deleted_forms:
                if form_data.get("id"):
                    # Update existing
                    await Task.objects.filter(id=form_data["id"]).update(
                        name=form_data["name"],
                        status=form_data["status"],
                    )
                else:
                    # Create new
                    await Task.objects.create(
                        project_id=id,
                        name=form_data["name"],
                        status=form_data["status"],
                    )

        return redirect(f"/projects/{id}/tasks")
    return render(request, "tasks/edit.html", {"formset": formset}, status=400)
```

### Bulk create

```python
@app.get("/users/bulk-invite")
async def bulk_invite_form(request):
    InviteFormSet = FormSet(InviteForm, extra=5, min_num=1, validate_min=True)
    formset = InviteFormSet()
    return render(request, "users/bulk_invite.html", {"formset": formset})


@app.post("/users/bulk-invite")
async def bulk_invite(request):
    data = await request.form_data()
    InviteFormSet = FormSet(InviteForm, extra=5, min_num=1, validate_min=True)
    formset = InviteFormSet(data=data)

    if formset.is_valid():
        for form_data in formset.cleaned_data:
            if form_data:
                await send_invitation(
                    email=form_data["email"],
                    role=form_data["role"],
                )
        return redirect("/users/")
    return render(request, "users/bulk_invite.html", {"formset": formset}, status=400)
```

### Tabular display with validation errors

```html
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <input
    type="hidden"
    name="form-TOTAL_FORMS"
    value="{{ formset.total_form_count }}"
  />
  <input
    type="hidden"
    name="form-INITIAL_FORMS"
    value="{{ formset.initial_form_count }}"
  />

  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Quantity</th>
        <th>Price</th>
        {% if formset.can_delete %}
        <th>Delete</th>
        {% endif %}
      </tr>
    </thead>
    <tbody>
      {% for form in formset.forms %}
      <tr class="{{ 'error' if formset.errors[loop.index0] else '' }}">
        {% for field in form.fields %} {% if field.name == "DELETE" %}
        <td>
          <input type="checkbox" name="{{ field.html_name }}" value="true" />
        </td>
        {% else %}
        <td>
          <input
            type="{{ field.widget }}"
            name="{{ field.html_name }}"
            value="{{ field.value }}"
            class="{{ 'field-error' if field.errors else '' }}"
          />
          {% if field.errors %}
          <span class="error">{{ field.errors[0] }}</span>
          {% endif %}
        </td>
        {% endif %} {% endfor %}
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% if formset.non_form_errors %}
  <div class="formset-errors">
    {% for error in formset.non_form_errors %}
    <p class="error">{{ error }}</p>
    {% endfor %}
  </div>
  {% endif %}

  <button type="submit">Save All</button>
</form>
```
