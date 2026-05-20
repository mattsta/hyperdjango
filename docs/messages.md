# Flash Messages

Server-side messages that survive a single redirect. Used for success/error feedback after form submissions, CRUD operations, and other actions that redirect.

## Overview

Flash messages follow the Post/Redirect/Get (PRG) pattern:

1. User submits a form (POST)
2. Server processes the form and adds a flash message
3. Server redirects to a new page (GET)
4. The new page displays the message
5. Message is consumed -- it does not appear again on subsequent page loads

## Quick Start

```python
from hyperdjango.messages import add_message, get_messages


@app.post("/items/create")
async def create_item(request):
    data = request.form_data()
    item = await Item.objects.create(name=data["name"])
    add_message(request, "success", "Item created successfully!")
    return redirect("/items/")


@app.get("/items/")
async def list_items(request):
    messages = get_messages(request)
    items = await Item.objects.all()
    return render(
        request,
        "items/list.html",
        {
            "items": items,
            "messages": messages,
        },
    )
```

## add_message()

```python
from hyperdjango.messages import add_message

add_message(request, level, text)
```

| Parameter | Type      | Description                                                             |
| --------- | --------- | ----------------------------------------------------------------------- |
| `request` | `Request` | The current request object                                              |
| `level`   | `str`     | Message level: `"info"`, `"success"`, `"warning"`, `"error"`, `"debug"` |
| `text`    | `str`     | The message text to display                                             |

Messages are stored in the session (or per-request storage if no session middleware is active).

## get_messages()

```python
from hyperdjango.messages import get_messages

messages = get_messages(request, clear=True)
```

| Parameter | Type      | Default  | Description                               |
| --------- | --------- | -------- | ----------------------------------------- |
| `request` | `Request` | required | The current request object                |
| `clear`   | `bool`    | `True`   | Whether to clear messages after retrieval |

Returns a list of message dicts:

```python
[
    {"level": "success", "text": "Item created successfully!"},
    {"level": "warning", "text": "Your subscription expires soon."},
]
```

By default, messages are cleared after retrieval. Pass `clear=False` to peek without consuming:

```python
# Peek at messages without clearing
messages = get_messages(request, clear=False)

# Later, consume them
messages = get_messages(request)  # cleared now
```

## Message Levels

| Level       | Constant  | Use Case                      | CSS Class (Bootstrap) |
| ----------- | --------- | ----------------------------- | --------------------- |
| `"info"`    | `INFO`    | Neutral information           | `alert-info`          |
| `"success"` | `SUCCESS` | Action completed successfully | `alert-success`       |
| `"warning"` | `WARNING` | Something needs attention     | `alert-warning`       |
| `"error"`   | `ERROR`   | Action failed                 | `alert-danger`        |
| `"debug"`   | `DEBUG`   | Debug information (dev only)  | `alert-secondary`     |

### Convenience Functions

Instead of `add_message(request, "success", text)`, use the convenience shortcuts:

```python
from hyperdjango.messages import success, error, info, warning

success(request, "Order placed!")
error(request, "Payment failed. Please try again.")
info(request, "Your profile was updated.")
warning(request, "Your subscription expires soon.")
```

These are equivalent to calling `add_message()` with the corresponding level.

## Template Rendering

### Basic Alert Divs

```html
{% if messages %}
<div class="messages">
  {% for msg in messages %}
  <div class="alert alert-{{ msg.level }}">{{ msg.text }}</div>
  {% endfor %}
</div>
{% endif %}
```

### Bootstrap 5 Alerts

```html
{% if messages %}
<div class="container mt-3">
  {% for msg in messages %}
  <div
    class="alert alert-{{ msg.level }} alert-dismissible fade show"
    role="alert"
  >
    {{ msg.text }}
    <button
      type="button"
      class="btn-close"
      data-bs-dismiss="alert"
      aria-label="Close"
    ></button>
  </div>
  {% endfor %}
</div>
{% endif %}
```

### Tailwind CSS Alerts

```html
{% if messages %}
<div class="space-y-2 mb-4">
  {% for msg in messages %}
  <div
    class="p-4 rounded-lg
        {% if msg.level == 'success' %}bg-green-100 text-green-800 border border-green-200
        {% elif msg.level == 'error' %}bg-red-100 text-red-800 border border-red-200
        {% elif msg.level == 'warning' %}bg-yellow-100 text-yellow-800 border border-yellow-200
        {% else %}bg-blue-100 text-blue-800 border border-blue-200{% endif %}"
  >
    {{ msg.text }}
  </div>
  {% endfor %}
</div>
{% endif %}
```

### Icon-Enhanced Alerts

```html
{% if messages %}
<div class="messages">
  {% for msg in messages %}
  <div class="alert alert-{{ msg.level }}">
    {% if msg.level == 'success' %}&#10004; {% elif msg.level == 'error'
    %}&#10006; {% elif msg.level == 'warning' %}&#9888; {% else %}&#8505;{%
    endif %} {{ msg.text }}
  </div>
  {% endfor %}
</div>
{% endif %}
```

## Session Requirement

Flash messages require session middleware to persist across redirects:

```python
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore

session_store = DatabaseSessionStore(app.db)
await session_store.ensure_table()

app.use(SessionAuth(session_store))
```

Without session middleware, messages are stored per-request in `request._flash_messages` and will not survive redirects. This is useful for testing or for displaying messages on the same page (no redirect).

### How Session Storage Works

1. `add_message()` stores messages in `request.session["_messages"]`
2. On redirect, the session is saved to the database
3. On the next request, the session is loaded and `get_messages()` reads from it
4. After retrieval with `clear=True`, the messages are removed from the session

## Message Consumption

Messages are consumed (cleared) after `get_messages()` is called with `clear=True` (the default). This ensures each message is displayed exactly once:

```python
# First call: returns messages and clears them
messages = get_messages(request)  # [{"level": "success", "text": "Done!"}]

# Second call: returns empty list
messages = get_messages(request)  # []
```

This single-use behavior is essential for the PRG pattern. Without it, messages would reappear on every page load.

## Multiple Messages

You can add multiple messages to a single request:

```python
@app.post("/bulk-delete")
async def bulk_delete(request):
    ids = request.form_data().getlist("ids")
    deleted = 0
    errors = 0

    for item_id in ids:
        try:
            item = await Item.objects.get(id=int(item_id))
            await item.delete()
            deleted += 1
        except Item.DoesNotExist:
            errors += 1
            warning(request, f"Item {item_id} not found")
        except PermissionError:
            errors += 1
            error(request, f"No permission to delete item {item_id}")

    if deleted:
        success(request, f"Deleted {deleted} items")
    if errors:
        warning(request, f"{errors} items could not be deleted")

    return redirect("/items/")
```

All messages are stored in order and displayed together on the next page.

## MessageMiddleware

For automatic message loading into request context, use the `MessageMiddleware`:

```python
from hyperdjango.messages import MessageMiddleware

app.use(MessageMiddleware())
```

The middleware loads pending messages into `request._pending_messages` before the handler runs. This makes messages available to templates without explicitly calling `get_messages()` in every view:

```python
# With MessageMiddleware, templates can access messages directly
@app.get("/items/")
async def list_items(request):
    items = await Item.objects.all()
    return render(
        request,
        "items/list.html",
        {
            "items": items,
            "messages": request._pending_messages,
        },
    )
```

## Auto-Dismiss Pattern

Make messages automatically disappear after a few seconds using CSS animation or JavaScript:

### CSS Animation

```html
<style>
  .alert {
    animation: fadeOut 0.5s ease-in 4s forwards;
  }
  @keyframes fadeOut {
    from {
      opacity: 1;
      max-height: 100px;
      margin-bottom: 1rem;
      padding: 1rem;
    }
    to {
      opacity: 0;
      max-height: 0;
      margin-bottom: 0;
      padding: 0;
      overflow: hidden;
    }
  }
</style>

{% for msg in messages %}
<div class="alert alert-{{ msg.level }}">{{ msg.text }}</div>
{% endfor %}
```

### JavaScript

```html
{% for msg in messages %}
<div class="alert alert-{{ msg.level }}" data-auto-dismiss="5000">
  {{ msg.text }}
</div>
{% endfor %}

<script>
  document.querySelectorAll("[data-auto-dismiss]").forEach((el) => {
    const delay = parseInt(el.dataset.autoDismiss, 10);
    setTimeout(() => {
      el.style.transition = "opacity 0.5s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 500);
    }, delay);
  });
</script>
```

## HTMX Integration

### Toast Notifications with HTMX

Use HTMX to display flash messages as toast notifications without a full page reload:

```python
@app.post("/items/create")
async def create_item(request):
    item = await Item.objects.create(name=request.form_data()["name"])

    # For HTMX requests, return messages as an OOB swap
    if request.headers.get("HX-Request"):
        html = f'<div id="toast-container" hx-swap-oob="beforeend">'
        html += f'<div class="toast" data-auto-dismiss="3000">Item "{item.name}" created!</div>'
        html += f"</div>"
        return Response.html(html)

    # For regular requests, use flash messages
    success(request, f'Item "{item.name}" created!')
    return redirect("/items/")
```

### HTMX Toast Container

Add a toast container to your base template:

```html
<!-- base.html -->
<body>
  <div id="toast-container" class="toast-container"></div>
  {% block content %}{% endblock %}
</body>
```

```css
.toast-container {
  position: fixed;
  top: 1rem;
  right: 1rem;
  z-index: 9999;
}
.toast {
  background: #333;
  color: white;
  padding: 0.75rem 1.5rem;
  border-radius: 0.5rem;
  margin-bottom: 0.5rem;
  animation:
    slideIn 0.3s ease-out,
    fadeOut 0.5s ease-in 2.5s forwards;
}
@keyframes slideIn {
  from {
    transform: translateX(100%);
    opacity: 0;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}
```

### HTMX Response Headers

Alternatively, use `HX-Trigger` headers to trigger client-side toast display:

```python
@app.post("/items/create")
async def create_item(request):
    item = await Item.objects.create(name=request.form_data()["name"])

    response = Response.html(render_items_list())
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": f"Item '{item.name}' created!", "level": "success"}}
    )
    return response
```

```html
<script>
  document.body.addEventListener("showToast", function (evt) {
    const { message, level } = evt.detail;
    const toast = document.createElement("div");
    toast.className = `toast toast-${level}`;
    toast.textContent = message;
    document.getElementById("toast-container").appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  });
</script>
```

## Testing Messages

### Checking Messages in Tests

```python
from hyperdjango.testing import TestClient
from hyperdjango.messages import get_messages


async def test_create_item_message():
    client = TestClient(app)

    response = await client.post("/items/create", data={"name": "Test"})
    assert response.status_code == 302

    # Follow the redirect
    response = await client.get("/items/")
    assert "Item created successfully!" in response.text


async def test_message_levels():
    client = TestClient(app)

    # Add multiple messages at different levels
    await client.post("/test-messages")

    response = await client.get("/show-messages")
    assert "alert-success" in response.text
    assert "alert-error" in response.text
```

### Testing Message Content Directly

```python
async def test_messages_stored_in_session():
    client = TestClient(app)

    # Trigger a message
    response = await client.post("/items/create", data={"name": "Test"})

    # Check session contains the message
    # (this depends on your session storage implementation)
    assert response.status_code == 302
```

### Testing Without Session Middleware

Without session middleware, messages are stored in `request._flash_messages`:

```python
async def test_messages_per_request():
    from hyperdjango.messages import add_message, get_messages

    class FakeRequest:
        pass

    request = FakeRequest()

    add_message(request, "success", "Test message")
    messages = get_messages(request)

    assert len(messages) == 1
    assert messages[0]["level"] == "success"
    assert messages[0]["text"] == "Test message"

    # Messages are cleared after get_messages
    messages = get_messages(request)
    assert len(messages) == 0
```
