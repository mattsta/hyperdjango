# Redirects

URL redirect management with O(1) in-memory lookup. Replaces `django.contrib.redirects`.

---

## Overview

The redirects module provides a thread-safe, in-memory registry of URL redirects backed
by an optional database loader. Every 404 response is checked against the registry in
constant time. Supports exact-match and prefix-match redirects, open redirect protection,
and a JSON API view for runtime management.

```python
from hyperdjango.redirects import registry, RedirectMiddleware, redirect_view
```

---

## Data Model

### Redirect

A single redirect entry stored in the registry.

```python
from hyperdjango.redirects import Redirect

r = Redirect(
    old_path="/old-page/",
    new_path="/new-page/",
    status_code=301,  # 301 permanent (default) or 302 temporary
    is_active=True,  # only active redirects are served
)
```

| Field         | Type   | Default | Description                          |
| ------------- | ------ | ------- | ------------------------------------ |
| `old_path`    | `str`  | --      | Source path to redirect from         |
| `new_path`    | `str`  | --      | Destination path or URL              |
| `status_code` | `int`  | `301`   | HTTP status (301 permanent, 302 tmp) |
| `is_active`   | `bool` | `True`  | Whether this redirect is active      |

### RedirectMatch

Internal lookup result returned by the registry.

```python
from hyperdjango.redirects import RedirectMatch

match = RedirectMatch(new_path="/new-page/", status_code=301)
```

---

## RedirectRegistry

Thread-safe in-memory registry. Uses a `dict` for O(1) exact-match and a separate dict
for prefix-match redirects (paths ending with `*`).

```python
from hyperdjango.redirects import RedirectRegistry

reg = RedirectRegistry()
```

### Adding redirects

```python
redirect = reg.add("/old/", "/new/", status_code=301)

# Prefix redirect -- matches any URL starting with /blog/2020/
redirect = reg.add("/blog/2020/*", "/archive/2020/", status_code=301)
```

The `add()` method validates that `new_path` starts with `/` (relative path) unless
`allow_external=True` is passed. This prevents open redirect vulnerabilities.

```python
# Raises ValueError -- open redirect protection
reg.add("/go/", "https://evil.com/")

# Explicitly allow external URLs
reg.add("/go/", "https://example.com/", allow_external=True)
```

Paths starting with `//` are also rejected (protocol-relative URLs) unless
`allow_external=True`.

### Removing redirects

```python
removed = reg.remove("/old/")  # returns True if found and removed
removed = reg.remove("/blog/2020/*")  # remove prefix redirect
```

### Looking up redirects

```python
result = reg.lookup("/old/")
if result is not None:
    new_path, status_code = result
```

Lookup order:

1. Exact match (O(1) dict lookup)
2. Prefix match (longest matching prefix wins)

### Listing all redirects

```python
all_redirects = reg.all_redirects()  # list[Redirect]
```

### Bulk loading

```python
# Load from a list of Redirect objects
redirects = [
    Redirect("/a/", "/b/"),
    Redirect("/old/*", "/new/"),
]
count = await reg.load_all(redirects)

# Load from a database loader callback
reg._db_loader = my_async_loader_function
count = await reg.load_all()
```

### Count and clear

```python
n = reg.count  # number of active redirects
reg.clear()  # remove all
```

---

## Module-Level Singleton

A global `registry` instance is available for convenience:

```python
from hyperdjango.redirects import registry

registry.add("/old/", "/new/")
```

---

## RedirectMiddleware

Async middleware that intercepts 404 responses and checks the registry.

```python
from hyperdjango.redirects import RedirectMiddleware, registry

middleware = RedirectMiddleware(registry)


# Register as app middleware
@app.middleware
async def redirect_mw(request, call_next):
    return await middleware(request, call_next)
```

Behavior:

- Non-404 responses pass through unchanged.
- On 404, checks the full path (with query string) first, then the bare path.
- If a match is found, returns a redirect response with the configured status code.
- Query strings are preserved on the redirect target unless the target already
  contains a `?`.

---

## API View

A built-in view for managing redirects via JSON API:

```python
from hyperdjango.redirects import redirect_view

app.route("/api/redirects")(redirect_view)
```

### GET /api/redirects

Returns all active redirects as a JSON array:

```json
[
  {
    "old_path": "/old/",
    "new_path": "/new/",
    "status_code": 301,
    "is_active": true
  }
]
```

### POST /api/redirects

Add a new redirect. Body:

```json
{ "old_path": "/old/", "new_path": "/new/", "status_code": 301 }
```

Returns 201 with the created redirect.

### DELETE /api/redirects

Remove a redirect. Body:

```json
{ "old_path": "/old/" }
```

Returns the removed path or 404 if not found.

---

## Django Migration Guide

| Django                          | HyperDjango                      |
| ------------------------------- | -------------------------------- |
| `django.contrib.redirects`      | `hyperdjango.redirects`          |
| `Redirect` model (DB-only)      | `Redirect` dataclass (in-memory) |
| `RedirectFallbackMiddleware`    | `RedirectMiddleware`             |
| DB lookup on every 404          | O(1) dict lookup                 |
| Requires `django.contrib.sites` | No site framework dependency     |
