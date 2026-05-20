# Template Engine Guide

This guide covers HyperDjango's native Zig template engine: syntax, filters,
control flow, template inheritance, macros, caching, and custom extensions.
The engine provides full Jinja2 syntax compatibility with significantly faster
compilation and rendering.

For the API reference, see [templates.md](templates.md).

---

## Table of Contents

- [Getting Started](#getting-started)
- [Zig Native Engine Architecture](#zig-native-engine-architecture)
- [Variables and Expressions](#variables-and-expressions)
- [Built-in Filters](#built-in-filters)
- [Control Flow](#control-flow)
- [Template Inheritance](#template-inheritance)
- [Macros](#macros)
- [Autoescape and Security](#autoescape-and-security)
- [Sandbox Mode](#sandbox-mode)
- [Custom Delimiters](#custom-delimiters)
- [Bytecode Caching](#bytecode-caching)
- [Custom Filter Registration](#custom-filter-registration)
- [Performance Characteristics](#performance-characteristics)
- [Migration from Django Templates](#migration-from-django-templates)

---

## Getting Started

Create a `TemplateEngine` pointed at your templates directory:

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine("templates")
html = engine.render("index.html", {"title": "Dashboard", "user": current_user})
```

In a HyperApp, the engine is typically created at app startup:

```python
from hyperdjango import HyperApp

app = HyperApp(title="My App", templates="templates")


@app.get("/")
async def homepage(request):
    products = await Product.objects.filter(active=True).all()
    return Response.html(
        app.engine.render(
            "home.html",
            {
                "products": products,
                "featured_count": len([p for p in products if p.featured]),
            },
        )
    )
```

---

## Zig Native Engine Architecture

The template engine compiles templates to Zig node trees at load time, then
renders by walking the tree and writing directly to a contiguous buffer.

- **Compilation**: Template source is parsed by a recursive descent parser in
  Zig with a proper expression tokenizer. Compilation takes ~7.1 microseconds
  (234x faster than Jinja2).
- **Rendering**: Cached node trees render in ~36 microseconds (1.7x faster
  than Jinja2).
- **Thread safety**: The LRU template cache uses per-instance locks. Each
  thread gets a growable output buffer.
- **SIMD filters**: Several filters (`striptags`, `truncate`, `urlencode`,
  `wordcount`, `wordwrap`) use SIMD acceleration for throughput up to 20 GB/s
  on boundary scanning.

All Jinja2 syntax is supported natively. There is no Python fallback -- the
Zig extension is required.

---

## Variables and Expressions

Output a variable with double braces:

```jinja
<h1>{{ title }}</h1>
<p>Welcome, {{ user.name }}</p>
```

The expression parser supports math, string concatenation, comparisons,
ternary expressions, and pipe filters:

```jinja
{# Math #}
{{ price * quantity }}
{{ (subtotal + tax) | round(2) }}

{# String concatenation with ~ #}
{{ first_name ~ " " ~ last_name }}

{# Comparisons #}
{{ "active" if user.is_active else "inactive" }}

{# Attribute and subscript access #}
{{ user.address.city }}
{{ items[0].name }}
{{ config["debug_mode"] }}

{# Method calls #}
{{ items | join(", ") }}
{{ name | replace("_", " ") | title }}
```

---

## Built-in Filters

The engine provides 49 native filters. Here are the most commonly used ones,
grouped by category.

### String Filters

```jinja
{{ name | upper }}                  {# "ALICE" #}
{{ name | lower }}                  {# "alice" #}
{{ name | title }}                  {# "Alice Smith" #}
{{ name | capitalize }}             {# "Alice smith" #}
{{ text | truncate(100) }}          {# "Lorem ipsum dolor sit am..." #}
{{ text | truncate(50, true) }}     {# Truncate at word boundary #}
{{ text | wordwrap(72) }}           {# Wrap at 72 columns #}
{{ text | striptags }}              {# Remove HTML tags (SIMD) #}
{{ text | trim }}                   {# Strip whitespace #}
{{ text | replace("old", "new") }} {# Multi-arg replace #}
{{ slug | urlencode }}              {# URL percent-encoding (SIMD) #}
{{ text | indent(4) }}              {# Indent each line by 4 spaces #}
{{ text | center(40) }}             {# Center in 40-char field #}
{{ text | wordcount }}              {# Count words (SIMD) #}
```

### Number Filters

```jinja
{{ price | round(2) }}              {# 19.99 #}
{{ count | abs }}                   {# Absolute value #}
{{ amount | float }}                {# Convert to float #}
{{ value | int }}                   {# Convert to integer #}
{{ amount | filesizeformat }}       {# "1.2 MB" #}
```

### Collection Filters

```jinja
{{ items | length }}                {# List length #}
{{ items | first }}                 {# First element #}
{{ items | last }}                  {# Last element #}
{{ items | reverse }}               {# Reversed list #}
{{ items | sort }}                  {# Sorted list #}
{{ items | sort(attribute="name") }}{# Sort by attribute #}
{{ items | unique }}                {# Deduplicated #}
{{ items | map(attribute="name") }} {# Extract attribute #}
{{ items | join(", ") }}            {# Join to string #}
{{ items | batch(3) }}              {# Group into batches of 3 #}
{{ items | slice(3) }}              {# Split into 3 slices #}
{{ items | reject("none") }}        {# Remove None values #}
{{ items | select("odd") }}         {# Keep odd numbers #}
{{ users | groupby("department") }} {# Group by attribute #}
```

### HTML and Markup Filters

```jinja
{{ content | e }}                   {# HTML escape #}
{{ content | safe }}                {# Mark as safe (no escape) #}
{{ html | striptags }}              {# Strip HTML tags #}
{{ text | urlize }}                 {# Auto-link URLs and emails #}
{{ attrs_dict | xmlattr }}          {# Dict to HTML attributes #}
```

### Default and Conditional Filters

```jinja
{{ value | default("N/A") }}        {# Fallback for undefined/empty #}
{{ value | default("N/A", true) }}  {# Treat falsy as undefined #}
{{ items | list }}                  {# Convert to list #}
{{ value | string }}                {# Convert to string #}
{{ value | tojson }}                {# Serialize to JSON #}
```

---

## Control Flow

### For Loops

```jinja
<ul>
{% for product in products %}
    <li>{{ product.name }} - ${{ product.price }}</li>
{% else %}
    <li>No products found.</li>
{% endfor %}
</ul>
```

The `loop` variable provides metadata inside for-blocks:

```jinja
{% for item in items %}
    {{ loop.index }}      {# 1-based index #}
    {{ loop.index0 }}     {# 0-based index #}
    {{ loop.first }}      {# True on first iteration #}
    {{ loop.last }}       {# True on last iteration #}
    {{ loop.length }}     {# Total number of items #}
    {{ loop.revindex }}   {# Reverse 1-based index #}
    {{ loop.cycle("odd", "even") }}
{% endfor %}
```

Recursive loops for tree structures:

```jinja
{% for item in menu recursive %}
    <li>{{ item.name }}
        {% if item.children %}
            <ul>{{ loop(item.children) }}</ul>
        {% endif %}
    </li>
{% endfor %}
```

Loop filtering:

```jinja
{% for user in users if user.is_active %}
    {{ user.name }}
{% endfor %}
```

Break and continue:

```jinja
{% for item in items %}
    {% if item.skip %}{% continue %}{% endif %}
    {% if item.done %}{% break %}{% endif %}
    {{ item.name }}
{% endfor %}
```

### Conditionals

```jinja
{% if user.is_admin %}
    <a href="/admin">Admin Panel</a>
{% elif user.is_staff %}
    <a href="/staff">Staff Dashboard</a>
{% else %}
    <p>Welcome, {{ user.name }}</p>
{% endif %}
```

### Is-Tests

Use `is` for type and value testing (28 built-in tests):

```jinja
{% if count is divisibleby(3) %}...{% endif %}
{% if value is none %}...{% endif %}
{% if name is defined %}...{% endif %}
{% if items is iterable %}...{% endif %}
{% if x is sameas(y) %}...{% endif %}
{% if score is ge(90) %}...{% endif %}
{% if number is odd %}...{% endif %}
{% if text is string %}...{% endif %}
```

### With and Set

Create scoped variables:

```jinja
{% with total = items | length, label = "Results" %}
    <h2>{{ label }}: {{ total }}</h2>
{% endwith %}

{% set page_title = "Dashboard - " ~ site_name %}
```

Use `namespace()` for variables that survive loop scoping:

```jinja
{% set ns = namespace(total=0) %}
{% for item in items %}
    {% set ns.total = ns.total + item.price %}
{% endfor %}
<p>Total: ${{ ns.total }}</p>
```

---

## Template Inheritance

Template inheritance is the most powerful feature for maintaining consistent
layouts across your site.

### Base Template (base.html)

```jinja
<!DOCTYPE html>
<html>
<head>
    <title>{% block title %}My Site{% endblock %}</title>
    {% block extra_css %}{% endblock %}
</head>
<body>
    <nav>{% block nav %}
        <a href="/">Home</a>
        <a href="/about">About</a>
    {% endblock %}</nav>

    <main>
        {% block content %}{% endblock content %}
    </main>

    <footer>{% block footer %}
        <p>Copyright 2026</p>
    {% endblock %}</footer>

    {% block extra_js %}{% endblock %}
</body>
</html>
```

### Child Template (products.html)

```jinja
{% extends "base.html" %}

{% block title %}Products - {{ super() }}{% endblock %}

{% block content %}
    <h1>Products</h1>
    {% for product in products %}
        <div class="product">
            <h2>{{ product.name }}</h2>
            <p>${{ product.price | round(2) }}</p>
        </div>
    {% endfor %}
{% endblock %}

{% block extra_js %}
    <script src="/static/js/products.js"></script>
{% endblock %}
```

`super()` inserts the parent block's content. Multi-level inheritance works
as expected -- a template can extend another that extends another.

### Dynamic Extends

The parent template can be a variable:

```jinja
{% extends layout_template %}
{% block content %}...{% endblock %}
```

### Include

Pull in reusable fragments:

```jinja
{% include "partials/header.html" %}
{% include "partials/sidebar.html" with context %}
{% include "partials/sidebar.html" without context %}
{% include "partials/widget.html" with count=items|length, title="Summary" %}
```

Handle missing includes gracefully:

```jinja
{% include "partials/optional.html" ignore missing %}
{% include ["theme/header.html", "default/header.html"] %}
```

### Scoped and Required Blocks

```jinja
{# Required: child MUST override this block #}
{% block content required %}{% endblock %}

{# Scoped: block sees for-loop variables #}
{% for item in items %}
    {% block item_display scoped %}
        {{ item.name }}
    {% endblock %}
{% endfor %}
```

---

## Macros

Macros are reusable template functions, similar to components.

### Defining and Calling Macros

```jinja
{% macro input(name, label, type="text", required=false, value="") %}
    <div class="form-group">
        <label for="{{ name }}">{{ label }}{% if required %} *{% endif %}</label>
        <input type="{{ type }}" name="{{ name }}" id="{{ name }}"
               value="{{ value }}" {% if required %}required{% endif %}>
    </div>
{% endmacro %}

{{ input("email", "Email Address", type="email", required=true) }}
{{ input("phone", "Phone Number") }}
{{ input("company", "Company", value=user.company) }}
```

### Importing Macros

```jinja
{# Import specific macros #}
{% from "macros/forms.html" import input, textarea, select %}

{{ input("name", "Full Name") }}

{# Import as namespace #}
{% import "macros/forms.html" as forms %}

{{ forms.input("name", "Full Name") }}
{{ forms.textarea("bio", "Biography", rows=5) }}
```

### Call Blocks

Pass a block of content to a macro using the `caller()` function:

```jinja
{% macro card(title, css_class="") %}
    <div class="card {{ css_class }}">
        <div class="card-header">{{ title }}</div>
        <div class="card-body">
            {{ caller() }}
        </div>
    </div>
{% endmacro %}

{% call card("User Profile", css_class="profile-card") %}
    <p>{{ user.name }}</p>
    <p>{{ user.email }}</p>
    <p>Member since {{ user.created_at | date }}</p>
{% endcall %}
```

---

## Autoescape and Security

By default, all variable output is HTML-escaped. You can control this per-block:

```jinja
{# Disable autoescaping for a block #}
{% autoescape false %}
    {{ trusted_html_content }}
{% endautoescape %}

{# Re-enable inside a disabled block #}
{% autoescape true %}
    {{ user_input }}
{% endautoescape %}
```

Mark individual values as safe:

```jinja
{{ rendered_markdown | safe }}
```

---

## Sandbox Mode

Sandbox mode prevents templates from accessing dangerous Python attributes:

```python
engine = TemplateEngine("templates", sandbox=True)
```

When enabled, these attribute accesses are blocked:

- `__class__`, `__globals__`, `__mro__`, `__init__`, `__dict__`
- `__subclasses__`, `__bases__`, `__code__`, `__func__`

Safe methods (like `.items()`, `.keys()`, `.values()`, `.upper()`) remain
accessible. This protects against server-side template injection when rendering
user-provided template strings.

---

## Custom Delimiters

Change the template syntax delimiters for compatibility with frontend
frameworks that also use `{{ }}`:

```python
engine = TemplateEngine("templates")
engine.set_delimiters(
    block_start="<%",
    block_end="%>",
    variable_start="<<",
    variable_end=">>",
    comment_start="<#",
    comment_end="#>",
)
```

Then in templates:

```
<% for item in items %>
    <div><< item.name >></div>
<% endfor %>
```

Each engine instance maintains its own delimiter configuration, so you can
use different delimiters for different template directories.

---

## Bytecode Caching

The engine supports three-tier caching for maximum performance:

1. **Memory LRU cache** -- compiled node trees in process memory (hit = ~0 cost)
2. **Disk bytecode cache** -- serialized `.hztc` files on disk (2.2x faster
   than recompilation)
3. **Merkle dependency hashing** -- tracks all transitive dependencies
   (includes, extends, imports) so any change to any file in the dependency
   tree invalidates the cache

### Enabling Disk Cache

```python
engine = TemplateEngine(
    "templates",
    bytecode_cache_dir=".template_cache",  # Directory for .hztc files
    auto_reload=True,  # Watch for file changes
    lru_max_bytes=50 * 1024 * 1024,  # 50 MB memory cache
)
```

### Cache Statistics

```python
stats = engine.cache_stats()
print(f"LRU hit rate: {stats.lru_hit_rate:.1%}")
print(f"Disk hit rate: {stats.disk_hit_rate:.1%}")
print(f"Total compiles: {stats.compiles}")
print(
    f"Memory usage: {stats.lru_bytes / 1024:.0f} KB of {stats.lru_max_bytes / 1024:.0f} KB"
)
```

### How Merkle Hashing Works

When a template is compiled, the engine records the FNV-1a hash of:

- The main template source
- Every transitively included/extended/imported template

These hashes are combined into a single Merkle hash stored in a `.hztc.meta`
sidecar file. On cache load, the engine re-hashes the current source files
and compares -- if any dependency has changed, the cache is invalidated and
the template is recompiled.

This means editing a shared `base.html` correctly invalidates the cache for
every template that extends it.

---

## Custom Filter Registration

Register Python functions as template filters:

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine("templates")


def currency(value, symbol="$", decimals=2):
    """Format a number as currency."""
    formatted = f"{float(value):,.{decimals}f}"
    return f"{symbol}{formatted}"


def timeago(dt):
    """Convert a datetime to a relative time string."""
    from datetime import datetime, timezone

    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


engine.register_filter("currency", currency)
engine.register_filter("timeago", timeago)
```

Use in templates:

```jinja
<span class="price">{{ product.price | currency("EUR ", 2) }}</span>
<time>{{ post.created_at | timeago }}</time>
```

You can also use the `Library` decorator pattern:

```python
from hyperdjango.templating import Library

register = Library()


@register.filter
def currency(value, symbol="$", decimals=2):
    formatted = f"{float(value):,.{decimals}f}"
    return f"{symbol}{formatted}"


# Register all library filters with an engine
engine.register_library(register)
```

---

## Performance Characteristics

| Operation            | HyperDjango (Zig) | Jinja2 (Python) | Speedup         |
| -------------------- | ----------------- | --------------- | --------------- |
| Template compilation | 7 us              | 1.5 ms          | 220x            |
| Cached render        | 41 us             | 62 us           | 1.5x            |
| `striptags` filter   | SIMD, 20 GB/s     | Python regex    | ~10x            |
| `urlencode` filter   | SIMD              | Python stdlib   | 3-12x           |
| Bytecode deserialize | 18 us             | N/A             | 2.2x vs compile |

The engine is compiled into the `_hyperdjango_native.so` extension. There is
no Python fallback.

---

## Migration from Django Templates

HyperDjango uses Jinja2 syntax, not Django template syntax. Here is a
conversion guide for the most common patterns.

| Django Template                   | HyperDjango (Jinja2)                 |
| --------------------------------- | ------------------------------------ |
| `{% load static %}`               | Not needed (use `get_static_url()`)  |
| `{{ value\|default:"N/A" }}`      | `{{ value \| default("N/A") }}`      |
| `{% url 'name' arg %}`            | `{{ url("name", arg) }}`             |
| `{% for x in list %}{% empty %}`  | `{% for x in list %}{% else %}`      |
| `{{ forloop.counter }}`           | `{{ loop.index }}`                   |
| `{{ forloop.first }}`             | `{{ loop.first }}`                   |
| `{% ifequal a b %}`               | `{% if a == b %}`                    |
| `{% with x=expr %}`               | `{% set x = expr %}` or `{% with %}` |
| `{{ block.super }}`               | `{{ super() }}`                      |
| `{% include "x.html" with a=1 %}` | `{% include "x.html" with a=1 %}`    |
| Filter arguments with `:`         | Filter arguments with `()`           |
| `{% csrf_token %}`                | `{{ csrf_token() }}` (function)      |
| `{% static "path" %}`             | `{{ static("path") }}`               |

### Django Template Backend

If you need to use HyperDjango's engine as a Django template backend:

```python
# settings.py
TEMPLATES = [
    {
        "BACKEND": "hyperdjango.serving.template_backend.ZigTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "OPTIONS": {
            "bytecode_cache_dir": ".template_cache",
            "auto_reload": DEBUG,
        },
    },
]
```

This lets you use the Zig engine for rendering while keeping Django's
template loading infrastructure.

---

## Custom Template Backend

### How the Zig Engine Works as a Backend

The Zig template engine is compiled into the `_hyperdjango_native.so` extension
and exposed to Python through `hyperdjango.templating.TemplateEngine`. When
used as a Django template backend via `hyperdjango.serving.template_backend.ZigTemplates`,
it integrates with Django's template loading infrastructure while using native
Zig compilation and rendering under the hood.

The pipeline:

1. **Django resolves the template path** using its standard template loaders
   (filesystem, app directories).
2. **The Zig engine compiles** the template source to a node tree using a
   recursive descent parser in Zig (~7 microseconds).
3. **The compiled tree is cached** in a thread-safe LRU cache in process memory,
   with optional disk bytecode caching for persistence across restarts.
4. **Rendering walks the node tree** and writes output to a per-thread growable
   buffer (~41 microseconds for cached templates).
5. **Python callables** (custom filters, context processors, template functions)
   are invoked via the C API boundary when the Zig renderer encounters them.

### Backend Configuration

```python
# Django settings.py
TEMPLATES = [
    {
        "BACKEND": "hyperdjango.serving.template_backend.ZigTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            # Enable HTML autoescaping (default: True)
            "autoescape": True,
            # Watch for file changes and recompile (default: settings.DEBUG)
            "auto_reload": True,
            # Memory cache size limit (default: 256 MB)
            "cache_max_bytes": 256 * 1024 * 1024,
            # Disk bytecode cache directory (optional, speeds up cold starts)
            "bytecode_cache_dir": ".template_cache",
            # Django context processors (same as with Jinja2 backend)
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
```

| Option               | Type        | Default     | Description                                                   |
| -------------------- | ----------- | ----------- | ------------------------------------------------------------- |
| `autoescape`         | `bool`      | `True`      | HTML-escape all variable output by default                    |
| `auto_reload`        | `bool`      | `DEBUG`     | Recompile templates when source files change                  |
| `cache_max_bytes`    | `int`       | `268435456` | Maximum bytes for the in-memory LRU template cache            |
| `bytecode_cache_dir` | `str`       | `None`      | Directory for persistent `.hztc` bytecode cache files         |
| `context_processors` | `list[str]` | `[]`        | Dotted paths to context processor functions                   |
| `sandbox`            | `bool`      | `False`     | Enable sandbox mode (blocks `__class__`, `__globals__`, etc.) |

### Extending with Python Callables

The Zig engine can call Python functions during rendering. This is how custom
filters, global functions, and template tags work. Register them on the engine
instance before rendering.

**Custom Filters:**

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine("templates")


def currency(value, symbol="$", decimals=2):
    """Format a number as currency: {{ price | currency("EUR", 2) }}"""
    formatted = f"{float(value):,.{decimals}f}"
    return f"{symbol}{formatted}"


def mask_email(email):
    """Mask an email address: {{ user.email | mask_email }}"""
    local, domain = email.split("@")
    masked = local[0] + "*" * (len(local) - 2) + local[-1] if len(local) > 2 else local
    return f"{masked}@{domain}"


engine.register_filter("currency", currency)
engine.register_filter("mask_email", mask_email)
```

**Global Functions (callable from any template):**

```python
from hyperdjango.router import reverse
from hyperdjango.staticfiles import get_static_url


def url(name, **kwargs):
    """Resolve a named URL: {{ url("product-detail", id=42) }}"""
    return reverse(name, **kwargs)


def static(path):
    """Get static file URL with content hash: {{ static("css/main.css") }}"""
    return get_static_url(path)


def csrf_token():
    """Generate CSRF token input: {{ csrf_token() }}"""
    return '<input type="hidden" name="_csrf" value="...">'


engine.register_global("url", url)
engine.register_global("static", static)
engine.register_global("csrf_token", csrf_token)
```

These are then available in every template:

```jinja
<link href="{{ static('css/main.css') }}" rel="stylesheet">

<form method="post" action="{{ url('product-create') }}">
    {{ csrf_token() }}
    <input name="name" value="{{ product.name }}">
    <span class="price">{{ product.price | currency("$", 2) }}</span>
    <button type="submit">Save</button>
</form>
```

**Using the Library Pattern for Reusable Filter Collections:**

```python
from hyperdjango.templating import Library

register = Library()


@register.filter
def pluralize(count, singular, plural=None):
    """{{ items|length|pluralize("item", "items") }}"""
    if plural is None:
        plural = singular + "s"
    return singular if count == 1 else plural


@register.filter
def percentage(value, total):
    """{{ completed|percentage(total) }}"""
    if total == 0:
        return "0%"
    return f"{(value / total) * 100:.1f}%"


@register.simple_tag
def query_string(request, **kwargs):
    """Build a query string: {% query_string request page=2 sort="name" %}"""
    params = dict(request.GET)
    params.update(kwargs)
    return "&".join(f"{k}={v}" for k, v in params.items())


# Register the entire library with the engine
engine.register_library(register)
```

### Performance Considerations

Python callable invocation crosses the C API boundary, which adds overhead
compared to the 49 built-in native filters. For hot-path rendering:

- Prefer built-in filters (`upper`, `lower`, `truncate`, `join`, etc.) over
  custom Python filters when a native equivalent exists.
- Custom filters that do simple string formatting are fine -- the overhead is
  typically under 1 microsecond per call.
- Avoid custom filters that perform I/O (database queries, HTTP requests)
  during template rendering. Pre-compute those values in the view and pass
  them via the context dict.
