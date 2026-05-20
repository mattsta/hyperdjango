# Flat Pages

Simple CMS pages served from PostgreSQL with O(1) in-memory lookup. Replaces
`django.contrib.flatpages`.

---

## Overview

Flatpages are database-backed static pages (about, terms of service, privacy policy,
etc.) that can be created and updated without deploying code. Pages are stored in
PostgreSQL and cached in a thread-safe in-memory registry. A middleware catches 404
responses and serves matching pages automatically.

```python
from hyperdjango.flatpages import registry, FlatPageMiddleware, flatpage_view
```

---

## Data Model

### FlatPage

```python
from hyperdjango.flatpages import FlatPage

page = FlatPage(
    url="/about/",
    title="About Us",
    content="<h1>About</h1><p>Welcome to our site.</p>",
    template_name="flatpages/default.html",
    registration_required=False,
    is_active=True,
)
```

| Field                   | Type   | Default                    | Description                  |
| ----------------------- | ------ | -------------------------- | ---------------------------- |
| `url`                   | `str`  | --                         | URL path (primary key in DB) |
| `title`                 | `str`  | --                         | Page title                   |
| `content`               | `str`  | `""`                       | HTML content                 |
| `template_name`         | `str`  | `"flatpages/default.html"` | Template to render with      |
| `registration_required` | `bool` | `False`                    | Require authenticated user   |
| `is_active`             | `bool` | `True`                     | Only active pages are served |

### Template Context

`page.to_context()` returns a dict with all fields for use in templates:

```python
ctx = page.to_context()
# {"url": "/about/", "title": "About Us", "content": "...", ...}
```

---

## Database Table

The module auto-creates its table when `ensure_table()` is called:

```sql
CREATE TABLE IF NOT EXISTS hyperdjango_flatpages (
    url VARCHAR(200) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    template_name VARCHAR(200) NOT NULL DEFAULT 'flatpages/default.html',
    registration_required BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
)
```

---

## FlatPageRegistry

Thread-safe in-memory registry with PostgreSQL write-through.

```python
from hyperdjango.flatpages import registry
```

### Setup

```python
# Create the table (run once at startup)
await registry.ensure_table()

# Load all active pages into memory
await registry.load_all()
```

### Adding pages

```python
page = await registry.add(
    url="/about/",
    title="About Us",
    content="<h1>About</h1><p>Welcome.</p>",
    template_name="flatpages/default.html",
    registration_required=False,
)
```

This performs an `INSERT ... ON CONFLICT DO UPDATE` (upsert) in PostgreSQL and
updates the in-memory cache atomically. URLs are normalized to start and end with `/`.

### Removing pages

```python
removed = await registry.remove("/about/")  # returns True if deleted
```

Removes from both the database and the in-memory cache.

### Looking up pages

```python
page = registry.lookup("/about/")
if page is not None:
    print(page.title)
```

Tries exact URL first, then with trailing-slash normalization.

### Listing all pages

```python
pages = registry.get_all()  # list[FlatPage], sorted by URL
```

---

## FlatPageMiddleware

Catches 404 responses and serves matching flatpages.

```python
from hyperdjango.flatpages import FlatPageMiddleware

middleware = FlatPageMiddleware(template_engine=my_engine)
app.add_middleware(middleware)
```

| Attribute         | Type   | Description                                  |
| ----------------- | ------ | -------------------------------------------- |
| `template_engine` | object | Optional template engine for rendering pages |

Behavior:

- Non-404 responses pass through unchanged.
- On 404, looks up the request path in the registry.
- If `registration_required` is `True`, checks `request.user.is_authenticated`.
  Unauthenticated users see the original 404.
- Renders the page through the template engine if available. Falls back to raw
  HTML wrapping (`<html><head><title>...</title></head><body>...</body></html>`).

### Template Rendering Fallback

The rendering pipeline has a three-stage fallback:

1. **Template engine render** -- If a `template_engine` is provided, render
   `page.template_name` (or `"flatpages/default.html"`) with `{"flatpage": page.to_context()}`.
2. **Exception logging** -- If the template engine raises any exception (missing
   template, syntax error, context error), the error is logged at WARNING level
   via `logging.getLogger("hyperdjango.flatpages")` and rendering falls through
   to the next stage. The page is still served rather than returning a 500.
3. **Raw HTML fallback** -- Wraps the page content in minimal HTML:
   `<html><head><title>{title}</title></head><body>{content}</body></html>`.

This means a misconfigured template never takes down a flatpage. The warning log
provides visibility into rendering failures without impacting availability:

```
WARNING hyperdjango.flatpages: Template render failed for flatpage /about/: TemplateNotFound: flatpages/custom.html
```

---

## Standalone View

For explicit URL routing instead of middleware:

```python
from hyperdjango.flatpages import flatpage_view

app.route("/pages/{url:path}")(flatpage_view)
```

Returns the rendered page or a 404 response. Same auth-gating logic as the middleware.

---

## Auth-Gated Pages

Pages with `registration_required=True` are only visible to authenticated users:

```python
await registry.add(
    url="/members/",
    title="Members Only",
    content="<p>Welcome, member!</p>",
    registration_required=True,
)
```

The middleware and view both check `request.user.is_authenticated`. Anonymous users
receive the original 404 response, hiding the page's existence.

---

## Django Migration Guide

| Django                               | HyperDjango                       |
| ------------------------------------ | --------------------------------- |
| `django.contrib.flatpages`           | `hyperdjango.flatpages`           |
| `FlatPage` Django model              | `FlatPage` dataclass + PostgreSQL |
| `FlatpageFallbackMiddleware`         | `FlatPageMiddleware`              |
| Requires `django.contrib.sites`      | No site framework dependency      |
| DB query on every 404                | O(1) in-memory lookup             |
| Template rendering via Django engine | Pluggable template engine         |
