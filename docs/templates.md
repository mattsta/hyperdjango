# Templates

Native Zig template engine with Jinja2-compatible syntax. 1.7x faster than Jinja2 for rendering, 234x faster for compilation. Compiled to Zig node trees at load time, rendered by walking the tree and writing directly to a contiguous buffer.

**Table of Contents**

- [Quick Start](#quick-start)
- [Variables](#variables)
- [Expressions](#expressions)
- [Expression Operators](#expression-operators)
- [Template Tags](#template-tags)
  - [if / elif / else / endif](#if-elif-else-endif)
  - [for / else / endfor](#for-else-endfor)
  - [extends / block / endblock](#extends-block-endblock)
  - [include](#include)
  - [macro / endmacro](#macro-endmacro)
  - [with / endwith](#with-endwith)
  - [set](#set)
  - [raw / endraw](#raw-endraw)
  - [comment](#comment)
  - [filter / endfilter](#filter-endfilter)
  - [autoescape](#autoescape)
  - [do](#do)
  - [debug](#debug)
  - [trans / endtrans](#trans-endtrans)
- [Filters](#filters)
- [Is-Tests](#is-tests)
- [Whitespace Control](#whitespace-control)
- [Template Engine Configuration](#template-engine-configuration)
- [Template Tag Libraries](#template-tag-libraries)
- [Context Processors and Globals](#context-processors-and-globals)
- [CSRF Token](#csrf-token)
- [Performance](#performance)

---

## Quick Start

```python
app = HyperApp("myapp", templates="templates")


@app.get("/")
async def index(request):
    return app.render("index.html", {"title": "Home", "items": [1, 2, 3]})


# Or with the render shortcut:
from hyperdjango.shortcuts import render


@app.get("/")
async def index(request):
    return render(request, "index.html", {"title": "Home"})
```

Standalone engine usage without `HyperApp`:

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine("templates")
html = engine.render("index.html", {"title": "Hello", "items": [1, 2, 3]})

# Render from a string (no file needed):
html = engine.render_string("Hello {{ name }}!", {"name": "World"})
```

---

## Variables

Variables are output using double curly braces. The engine resolves names from the context dictionary passed at render time.

```html
{{ variable }} {{ user.name }} {{ items[0] }} {{ article.get_title() }} {{
data["key"] }}
```

**Resolution order for dot notation** (`user.name`):

1. Dictionary key lookup: `user["name"]`
2. Attribute access: `user.name`
3. List/tuple index: `user[name]` (if `name` is an integer)

If all lookups fail, the variable resolves to an empty string (with autoescape on) or the undefined value.

**Subscript access** uses bracket notation and can be chained:

```html
{{ items[0] }} {{ matrix[0][1] }} {{ data["key"] }} {{ nested[0].name }}
```

**Method calls** are supported with parentheses:

```html
{{ article.get_title() }} {{ items.count() }} {{ name.upper() }} {{ data.items()
}}
```

---

## Expressions

The template engine supports full expressions inside `{{ }}` output blocks and inside tag arguments (`{% if %}`, `{% set %}`, etc.).

```html
{{ price * 1.1 }} {{ "Hello" ~ " " ~ name }} {{ x + y }} {{ count > 0 }} {{
value if condition else default }} {{ (a + b) * c }} {{ 2 ** 10 }} {{ total //
count }} {{ value % 2 }}
```

**Ternary (inline if)**:

```html
{{ "active" if user.is_active else "inactive" }} {{ count if count > 0 else
"none" }} {{ items|length ~ " items" if items else "empty" }}
```

**String concatenation** uses the `~` operator (not `+`):

```html
{{ "Hello, " ~ user.name ~ "!" }} {{ first_name ~ " " ~ last_name }}
```

**Literals** are supported directly in expressions:

```html
{# String literals #} {{ "hello" }} {{ 'world' }} {# Numeric literals #} {{ 42
}} {{ 3.14 }} {{ -7 }} {# Boolean literals #} {{ true }} {{ false }} {#
None/null #} {{ none }} {# List literals #} {{ [1, 2, 3] }} {{ ["a", "b", "c"]
}} {# Tuple literals #} {{ (1, 2, 3) }} {# Dict literals #} {{ {"key": "value",
"count": 42} }}
```

---

## Expression Operators

Complete table of all operators, ordered by precedence (highest first):

| Precedence | Operator    | Description              | Example                   |
| ---------- | ----------- | ------------------------ | ------------------------- |
| 1          | `()`        | Grouping / function call | `(a + b) * c`, `func()`   |
| 1          | `[]`        | Subscript access         | `items[0]`, `data["key"]` |
| 1          | `.`         | Attribute / key access   | `user.name`               |
| 2          | `**`        | Exponentiation           | `2 ** 10` -> `1024`       |
| 3          | `+` (unary) | Positive                 | `+x`                      |
| 3          | `-` (unary) | Negation                 | `-x`                      |
| 4          | `*`         | Multiplication           | `price * qty`             |
| 4          | `/`         | Division (true division) | `10 / 3` -> `3.333...`    |
| 4          | `//`        | Floor division           | `10 // 3` -> `3`          |
| 4          | `%`         | Modulo                   | `10 % 3` -> `1`           |
| 5          | `+`         | Addition                 | `a + b`                   |
| 5          | `-`         | Subtraction              | `a - b`                   |
| 5          | `~`         | String concatenation     | `"hi" ~ " " ~ name`       |
| 6          | `==`        | Equal to                 | `x == 1`                  |
| 6          | `!=`        | Not equal to             | `x != 0`                  |
| 6          | `<`         | Less than                | `x < 100`                 |
| 6          | `>`         | Greater than             | `x > 0`                   |
| 6          | `<=`        | Less than or equal       | `x <= 100`                |
| 6          | `>=`        | Greater than or equal    | `x >= 0`                  |
| 6          | `in`        | Containment test         | `"a" in items`            |
| 6          | `not in`    | Negated containment      | `"x" not in items`        |
| 6          | `is`        | Is-test                  | `x is defined`            |
| 6          | `is not`    | Negated is-test          | `x is not none`           |
| 7          | `not`       | Logical NOT              | `not user.is_admin`       |
| 8          | `and`       | Logical AND              | `a and b`                 |
| 9          | `or`        | Logical OR               | `a or b`                  |
| 10         | `if`/`else` | Ternary conditional      | `x if cond else y`        |
| 11         | `\|`        | Filter application       | `name\|upper`             |

**Operator notes:**

- `and` has higher precedence than `or`: `a and b or c` is `(a and b) or c`
- Parentheses override precedence: `a and (b or c)`
- The `~` operator converts both operands to strings before concatenating
- Division `/` always returns a float; use `//` for integer floor division
- `**` is right-associative: `2 ** 3 ** 2` is `2 ** (3 ** 2)` = `512`
- `in` works with strings, lists, tuples, and dicts (tests keys)
- Comparisons can be chained conceptually but should use `and`: `{% if 0 < x and x < 100 %}`

---

## Template Tags

Tags provide control flow, template composition, and variable manipulation. They use `{% tag %}` syntax.

### if / elif / else / endif

Conditional rendering. Evaluates expressions and renders the first branch whose condition is truthy.

**Basic syntax:**

```html
{% if condition %} ...content... {% endif %}
```

**Full syntax with elif and else:**

```html
{% if user.is_admin %}
<p>Welcome, admin!</p>
{% elif user.is_authenticated %}
<p>Welcome, {{ user.name }}!</p>
{% elif user.is_guest %}
<p>Welcome, guest.</p>
{% else %}
<p>Please log in.</p>
{% endif %}
```

You can have as many `{% elif %}` clauses as needed. The `{% else %}` clause is optional and must come last.

**All comparison operators:**

```html
{# Equality #} {% if status == "active" %}Active{% endif %} {% if count == 0
%}Empty{% endif %} {# Inequality #} {% if status != "deleted" %}Visible{% endif
%} {# Numeric comparisons #} {% if age < 18 %}Minor{% endif %} {% if age > 65
%}Senior{% endif %} {% if score <= 50 %}Failing{% endif %} {% if score >= 90
%}Excellent{% endif %} {# Containment #} {% if "admin" in user.roles %}Has admin
role{% endif %} {% if item not in blacklist %}Allowed{% endif %} {# Identity
tests #} {% if value is none %}No value{% endif %} {% if value is not none %}Has
value{% endif %} {# Is-tests with parameters #} {% if count is divisibleby(3)
%}Divisible by 3{% endif %} {% if count is odd %}Odd number{% endif %}
```

**Logical operators:**

```html
{# AND — both must be truthy #} {% if user.is_authenticated and user.is_active
%} Active user {% endif %} {# OR — at least one must be truthy #} {% if error or
warning %} Something needs attention {% endif %} {# NOT — negation #} {% if not
user.is_banned %} User is allowed {% endif %} {# Combined — and binds tighter
than or #} {% if user.is_admin and user.is_active or user.is_superuser %} {#
Equivalent to: (user.is_admin and user.is_active) or user.is_superuser #} {%
endif %} {# Use nesting for complex precedence #} {% if user.is_active %} {% if
user.is_admin or user.is_moderator %} Has elevated privileges {% endif %} {%
endif %}
```

**Expressions in conditions:**

```html
{% if items|length > 0 %} {{ items|length }} items found {% endif %} {% if price
* quantity > 100 %} Free shipping! {% endif %} {% if name|lower == "admin" %}
Reserved name {% endif %}
```

**Truthiness rules:**

The following values are considered **falsy** (the `{% else %}` branch executes):

- `false` / `False`
- `none` / `None`
- `0`, `0.0`
- `""` (empty string)
- `[]` (empty list)
- `{}` (empty dict)
- `()` (empty tuple)

Everything else is **truthy**.

### for / else / endfor

Iterates over a sequence, rendering the block body once per item.

**Basic syntax:**

```html
{% for item in items %}
<p>{{ item }}</p>
{% endfor %}
```

**With else clause** (rendered when the sequence is empty):

```html
{% for article in articles %}
<div class="article">
  <h2>{{ article.title }}</h2>
  <p>{{ article.content }}</p>
</div>
{% else %}
<p>No articles found.</p>
{% endfor %}
```

The `{% else %}` clause is optional. It triggers when `articles` is an empty list, empty tuple, or any empty iterable. It does NOT trigger if the variable is undefined.

**Loop variables:**

Inside every `{% for %}` block, a special `loop` variable is available:

| Variable          | Type   | Description                                                         |
| ----------------- | ------ | ------------------------------------------------------------------- |
| `loop.index`      | `int`  | Current iteration, 1-indexed. First iteration = `1`.                |
| `loop.index0`     | `int`  | Current iteration, 0-indexed. First iteration = `0`.                |
| `loop.revindex`   | `int`  | Iterations remaining, 1-indexed. Last iteration = `1`.              |
| `loop.revindex0`  | `int`  | Iterations remaining, 0-indexed. Last iteration = `0`.              |
| `loop.first`      | `bool` | `true` on the first iteration only.                                 |
| `loop.last`       | `bool` | `true` on the last iteration only.                                  |
| `loop.length`     | `int`  | Total number of items in the sequence.                              |
| `loop.previtem`   | `any`  | The item from the previous iteration. Undefined on first iteration. |
| `loop.nextitem`   | `any`  | The item from the next iteration. Undefined on last iteration.      |
| `loop.cycle(...)` | `func` | Cycles through the given values on each iteration.                  |

**Loop variable examples:**

```html
<table>
  {% for user in users %}
  <tr class="{{ loop.cycle('odd', 'even') }}">
    <td>{{ loop.index }}</td>
    <td>{{ user.name }}</td>
    {% if loop.first %}
    <td>(first)</td>
    {% endif %} {% if loop.last %}
    <td>(last)</td>
    {% endif %}
  </tr>
  {% endfor %}
</table>
```

```html
{# Comma-separated list with no trailing comma #} {% for tag in tags %} {{ tag
}}{% if not loop.last %}, {% endif %} {% endfor %}
```

```html
{# Using revindex for countdown #} {% for step in steps %}
<p>
  Step {{ loop.index }} of {{ loop.length }} ({{ loop.revindex }} remaining)
</p>
{% endfor %}
```

```html
{# Using previtem / nextitem #} {% for item in items %} {% if loop.previtem is
defined and loop.previtem.category != item.category %}
<h3>{{ item.category }}</h3>
{% endif %}
<p>{{ item.name }}</p>
{% endfor %}
```

**Tuple unpacking:**

When iterating over a list of tuples, lists, or dict items, you can unpack into multiple variables:

```html
{# Dict items #} {% for key, value in data.items() %}
<dt>{{ key }}</dt>
<dd>{{ value }}</dd>
{% endfor %} {# List of tuples #} {% for x, y in coordinates %} Point: ({{ x }},
{{ y }}) {% endfor %} {# Three-element unpacking #} {% for name, age, role in
team_members %}
<p>{{ name }} ({{ age }}) — {{ role }}</p>
{% endfor %}
```

**Break and continue:**

```html
{% for item in items %} {% if item.skip %}{% continue %}{% endif %} {% if
item.stop %}{% break %}{% endif %}
<p>{{ item.name }}</p>
{% endfor %}
```

- `{% continue %}` skips the rest of the current iteration and moves to the next item
- `{% break %}` exits the loop entirely; no further iterations execute
- Both must appear inside a `{% for %}` block
- The `{% else %}` clause does NOT execute when the loop exits via `{% break %}`

**Nested loops:**

```html
{% for category in categories %}
<h2>{{ category.name }}</h2>
<ul>
  {% for product in category.products %}
  <li>{{ product.name }} — ${{ product.price }}</li>
  {% endfor %}
</ul>
{% endfor %}
```

In nested loops, each level has its own `loop` variable. There is no `loop.parent` in HyperDjango; use `{% set %}` to preserve outer-loop values:

```html
{% for category in categories %} {% set cat_index = loop.index %} {% for product
in category.products %}
<p>{{ cat_index }}.{{ loop.index }} — {{ product.name }}</p>
{% endfor %} {% endfor %}
```

**Iterating over integers:**

Use `range()` to iterate over a sequence of numbers:

```html
{% for i in range(5) %}
<p>Item {{ i }}</p>
{% endfor %} {# Outputs: Item 0, Item 1, Item 2, Item 3, Item 4 #} {% for i in
range(1, 6) %}
<p>Step {{ i }}</p>
{% endfor %} {# Outputs: Step 1, Step 2, Step 3, Step 4, Step 5 #} {% for i in
range(0, 10, 2) %} {{ i }} {% endfor %} {# Outputs: 0, 2, 4, 6, 8 #}
```

### extends / block / endblock

Template inheritance allows you to build a base "skeleton" template that contains common elements, with child templates overriding specific blocks.

**Base template** (`templates/base.html`):

```html
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{% block title %}My Site{% endblock %}</title>
    {% block extra_head %}{% endblock %}
  </head>
  <body>
    <nav>
      {% block nav %}
      <a href="/">Home</a>
      <a href="/about">About</a>
      {% endblock %}
    </nav>

    <main>{% block content %}{% endblock %}</main>

    <footer>{% block footer %}&copy; 2026 My Site{% endblock %}</footer>

    {% block extra_js %}{% endblock %}
  </body>
</html>
```

**Child template** (`templates/article.html`):

```html
{% extends "base.html" %} {% block title %}{{ article.title }} - My Site{%
endblock %} {% block content %}
<article>
  <h1>{{ article.title }}</h1>
  <p class="meta">By {{ article.author }} on {{ article.date }}</p>
  {{ article.content }}
</article>
{% endblock %} {% block extra_js %}
<script src="/static/js/article.js"></script>
{% endblock %}
```

**Rules for extends:**

1. `{% extends %}` must be the **first tag** in the template (ignoring whitespace and comments)
2. The argument is a string literal (`"base.html"`) or a variable (`layout_name`)
3. Only content inside `{% block %}` tags in the child template is rendered; everything outside blocks is ignored
4. Blocks not overridden in the child retain the parent's default content
5. A child can override any number of blocks (zero or more)

**Multi-level inheritance** (three or more levels):

```html
{# templates/base.html #}
<!DOCTYPE html>
<html>
  <head>
    <title>{% block title %}Site{% endblock %}</title>
  </head>
  <body>
    {% block content %}{% endblock %}
  </body>
</html>
```

```html
{# templates/base_two_column.html #} {% extends "base.html" %} {% block content
%}
<div class="sidebar">{% block sidebar %}{% endblock %}</div>
<div class="main">{% block main %}{% endblock %}</div>
{% endblock %}
```

```html
{# templates/article.html #} {% extends "base_two_column.html" %} {% block title
%}{{ article.title }}{% endblock %} {% block sidebar %}
<nav>{{ article.toc }}</nav>
{% endblock %} {% block main %}
<article>{{ article.body }}</article>
{% endblock %}
```

This produces a three-level chain: `article.html` -> `base_two_column.html` -> `base.html`. Each level can override blocks from any ancestor.

**Using `{{ super() }}` to include parent block content:**

```html
{% extends "base.html" %} {% block title %}{{ article.title }} - {{ super() }}{%
endblock %} {# If base has "My Site" in the title block, this renders: "Article
Name - My Site" #} {% block nav %} {{ super() }}
<a href="/dashboard">Dashboard</a>
{% endblock %} {# Appends the Dashboard link after the parent's nav content #}
```

`{{ super() }}` inserts the content from the parent template's version of the same block. This lets you extend rather than replace parent content.

**Dynamic extends:**

```html
{% extends layout_template %} {# layout_template is a context variable
containing "base.html" or "admin_base.html" #}
```

**Named endblock (for readability):**

```html
{% block sidebar %} ...lots of content... {% endblock sidebar %}
```

The name after `endblock` is optional and purely for readability. If provided, it must match the block name.

**Required blocks:**

```html
{# base.html — forces child templates to provide content #} {% block content
required %}{% endblock %} {% block sidebar %}Default sidebar{% endblock %}
```

If a child template extends this base without overriding `content`, a `RuntimeError` is raised at compile time. Optional blocks retain their default content if not overridden.

**Scoped blocks:**

```html
{# base.html — blocks inside for-loops that child templates can override #} {%
for item in items %} {% block row scoped %}
<tr>
  <td>{{ item.name }}</td>
</tr>
{% endblock %} {% endfor %}
```

```html
{# child.html — overrides the block, sees loop variables #} {% extends
"base.html" %} {% block row %}
<tr class="custom">
  <td>{{ item.name }} ({{ loop.index }})</td>
</tr>
{% endblock %}
```

Without `scoped`, blocks nested inside for-loops/if-blocks/with-blocks are not visible to child template overrides. The `scoped` keyword enables recursive block indexing so the child can access the surrounding loop/with context.

You can combine both keywords: `{% block content scoped required %}`.

### include

Includes another template, rendering it inline at the point of the tag. The included template has access to the current template context.

**Basic syntax:**

```html
{% include "partials/header.html" %} {% include "partials/sidebar.html" %} {%
include "partials/footer.html" %}
```

**With additional context:**

```html
{% include "partials/user_card.html" with user=current_user, compact=true %}
```

Variables passed via `with` are added to (and override) the current context for the included template only.

**Dynamic include:**

```html
{% include template_name %} {# template_name is a context variable, e.g.,
"partials/card_v2.html" #}
```

**Typical usage — reusable partials:**

```html
{# templates/partials/alert.html #}
<div class="alert alert-{{ level|default('info') }}">{{ message }}</div>
```

```html
{# In the parent template #} {% include "partials/alert.html" with
level="error", message="Something went wrong" %} {% include
"partials/alert.html" with level="success", message="Saved!" %}
```

**Context control:**

By default, included templates see the full parent context. Use `without context` to isolate the included template:

```html
{# Included template sees parent variables #} {% include "partial.html" with
context %} {# Included template gets an empty context — only sees its own data
#} {% include "partial.html" without context %} {# Works with dynamic includes
and ignore missing too #} {% include template_var without context %} {% include
"maybe.html" without context ignore missing %}
```

**Include and template inheritance:**

Included templates are rendered independently. They do NOT participate in the `{% extends %}` inheritance chain of the including template. An included template can itself use `{% extends %}` to inherit from its own base.

### Dynamic Template Composition

Both `{% extends %}` and `{% include %}` accept variables in addition to string literals. The template path is resolved at render time from the context, enabling dynamic layouts and component selection.

#### Dynamic extends

Select the parent template at render time based on context:

```html
{% extends layout %} {% block content %}Hello{% endblock %}
```

```python
# Desktop users get one layout, mobile users get another
engine.render_string(template, {"layout": "desktop.html"})
engine.render_string(template, {"layout": "mobile.html"})
```

Dot-path variables are supported:

```html
{% extends config.layout %} {% block content %}Inside{% endblock %}
```

`{{ super() }}` works with dynamic extends. The parent block content is resolved from whichever parent the variable points to:

```html
{% extends layout %} {% block sidebar %}{{ super() }} + CHILD{% endblock %}
```

If the variable is undefined, the child template's block content is rendered directly without a parent wrapper.

#### Dynamic include

Include a template whose name comes from a context variable:

```html
{% include partial_name %}
```

```python
engine.render_string("{% include tmpl %}", {"tmpl": "greeting.html", "name": "World"})
```

Dot-path variable resolution works the same as with extends:

```html
{% include config.template %}
```

**Dynamic include in for loops** -- render different templates per iteration:

```html
{% for t in templates %}{% include t %}{% endfor %}
```

```python
engine.render_string(template, {"templates": ["a.html", "b.html", "c.html"]})
# Renders: [A][B][C]
```

If the variable is undefined or not a string, the include renders nothing (no error).

#### Fallback lists

Pass a list of template names. The engine tries each in order and renders the first one that exists:

```html
{% include ["primary.html", "secondary.html", "fallback.html"] %}
```

If the first template is missing, the engine falls back to the second, then the third, and so on. If all templates are missing, nothing is rendered.

A Python list variable also works as a fallback list:

```python
engine.render_string(
    "{% include templates %}",
    {"templates": ["missing1.html", "missing2.html", "fallback.html"]},
)
# Renders the first template that exists
```

#### ignore missing

Suppress errors when a template cannot be found. Works with both static and dynamic includes:

```html
{% include "optional_sidebar.html" ignore missing %} {% include sidebar_var
ignore missing %} {% include ["a.html", "b.html"] ignore missing %}
```

Without `ignore missing`, a missing static include raises an error. With dynamic variables, missing templates already render nothing silently, but `ignore missing` makes the intent explicit.

### SIMD Filter Performance

Several template filters use SIMD-accelerated native implementations for large content processing:

| Filter      | Benchmark (native) | Input Size | Description                            |
| ----------- | ------------------ | ---------- | -------------------------------------- |
| `striptags` | 119 us             | 6.5 KB     | Remove all HTML tags from content      |
| `wordcount` | 26 us              | 3.9 KB     | Count words using SIMD whitespace scan |
| `urlencode` | 14 us              | 0.4 KB     | Percent-encode special characters      |

These filters are automatically dispatched to the native Zig implementation when the native extension is loaded. No configuration required.

### macro / endmacro

Macros are reusable template functions. They accept parameters with optional defaults and return rendered HTML.

**Basic macro:**

```html
{% macro button(text, type="button", class="btn") %}
<button type="{{ type }}" class="{{ class }}">{{ text }}</button>
{% endmacro %} {{ button("Save") }} {{ button("Submit", type="submit",
class="btn btn-primary") }} {{ button("Delete", class="btn btn-danger") }}
```

Output:

```html
<button type="button" class="btn">Save</button>
<button type="submit" class="btn btn-primary">Submit</button>
<button type="button" class="btn btn-danger">Delete</button>
```

**Macro with multiple parameters and defaults:**

```html
{% macro input(name, label, type="text", value="", required=false,
placeholder="") %}
<div class="form-group">
  <label for="{{ name }}">{{ label }}</label>
  <input
    type="{{ type }}"
    name="{{ name }}"
    id="{{ name }}"
    value="{{ value }}"
    {%
    if
    placeholder
    %}placeholder="{{ placeholder }}"
    {%
    endif
    %}
    {%
    if
    required
    %}required{%
    endif
    %}
  />
</div>
{% endmacro %} {{ input("email", "Email Address", type="email", required=true)
}} {{ input("bio", "Biography", placeholder="Tell us about yourself") }}
```

**Macro with no parameters:**

```html
{% macro divider() %}
<hr class="divider" />
{% endmacro %} {{ divider() }}
```

**Macro accessing loop variables:**

Macros do NOT have access to the calling template's local variables (they have their own scope). Pass any needed data as arguments:

```html
{% macro render_item(item, index) %}
<div class="item">
  <span class="number">{{ index }}.</span>
  <span class="name">{{ item.name }}</span>
</div>
{% endmacro %} {% for item in items %} {{ render_item(item, loop.index) }} {%
endfor %}
```

**Call blocks:**

Macros support call blocks, which let the caller pass a block of content into the macro:

```html
{% macro panel(title) %}
<div class="panel">
  <div class="panel-header">{{ title }}</div>
  <div class="panel-body">{{ caller() }}</div>
</div>
{% endmacro %} {% call panel("User Details") %}
<p>Name: {{ user.name }}</p>
<p>Email: {{ user.email }}</p>
{% endcall %}
```

Output:

```html
<div class="panel">
  <div class="panel-header">User Details</div>
  <div class="panel-body">
    <p>Name: Alice</p>
    <p>Email: alice@example.com</p>
  </div>
</div>
```

**Importing macros from other files:**

```html
{# templates/macros/forms.html #} {% macro input(name, label, type="text") %}
<div class="field">
  <label>{{ label }}</label>
  <input type="{{ type }}" name="{{ name }}" />
</div>
{% endmacro %} {% macro textarea(name, label, rows=4) %}
<div class="field">
  <label>{{ label }}</label>
  <textarea name="{{ name }}" rows="{{ rows }}"></textarea>
</div>
{% endmacro %}
```

```html
{# templates/contact.html #} {% import "macros/forms.html" as forms %}

<form method="post">
  {{ forms.input("name", "Your Name") }} {{ forms.input("email", "Email",
  type="email") }} {{ forms.textarea("message", "Message", rows=6) }}
  <button type="submit">Send</button>
</form>
```

### with / endwith

Creates a scoped block where additional variables are defined. Variables set inside `{% with %}` are only available within the block and do not leak into the surrounding scope.

**Basic syntax:**

```html
{% with total = items|length %}
<p>{{ total }} items found</p>
{% if total > 10 %}
<p>Showing first 10</p>
{% endif %} {% endwith %} {# "total" is no longer accessible here #}
```

**Multiple variables:**

```html
{% with total = items|length, first = items|first, last = items|last %}
<p>{{ total }} items: {{ first }} to {{ last }}</p>
{% endwith %}
```

**With expressions:**

```html
{% with tax = price * 0.08, total = price * 1.08 %}
<p>Subtotal: ${{ price|round(2) }}</p>
<p>Tax: ${{ tax|round(2) }}</p>
<p>Total: ${{ total|round(2) }}</p>
{% endwith %}
```

**Scoping behavior:**

Variables defined in `{% with %}` shadow any outer variable of the same name for the duration of the block. The outer variable is restored after `{% endwith %}`:

```html
{% set name = "outer" %} {{ name }} {# "outer" #} {% with name = "inner" %} {{
name }} {# "inner" #} {% endwith %} {{ name }} {# "outer" — restored #}
```

### set

Assigns a value to a variable in the current scope. Unlike `{% with %}`, the variable persists for the rest of the template (or the enclosing block scope).

**Basic assignment:**

```html
{% set greeting = "Hello, " ~ user.name ~ "!" %}
<h1>{{ greeting }}</h1>
```

**Assignment with expressions:**

```html
{% set price = base_price * (1 + tax_rate) %}
<p>Total: ${{ price|round(2) }}</p>

{% set full_name = first_name ~ " " ~ last_name %}
<p>{{ full_name }}</p>

{% set is_eligible = age >= 18 and has_id %} {% if is_eligible %}Approved{%
endif %}
```

**Set with filters:**

```html
{% set slug = title|lower|replace(" ", "-") %}
<a href="/articles/{{ slug }}">{{ title }}</a>
```

**Set inside loops:**

Variables set inside a for loop persist after the loop ends:

```html
{% set total = 0 %} {% for item in items %} {% set total = total + item.price %}
{% endfor %}
<p>Grand total: ${{ total|round(2) }}</p>
```

**Set with list/dict literals:**

```html
{% set colors = ["red", "green", "blue"] %} {% set config = {"debug": true,
"verbose": false} %}
```

### raw / endraw

Outputs the contents as literal text without template processing. Use this to display template syntax examples or embed client-side template code (e.g., Vue.js, Angular, Mustache).

```html
{% raw %} This {{ won't }} be {% processed %} Neither will {{ variable|filter }}
or {% if condition %}this{% endif %} {% endraw %}
```

Output:

```
    This {{ won't }} be {% processed %}
    Neither will {{ variable|filter }} or {% if condition %}this{% endif %}
```

**Common use case — Vue.js templates:**

```html
{% raw %}
<div id="app">
  <p>{{ message }}</p>
  <ul>
    <li v-for="item in items">{{ item.name }}</li>
  </ul>
</div>
{% endraw %}
```

**Documenting template syntax:**

```html
<h2>Template Syntax</h2>
<pre><code>
{% raw %}
{% for item in items %}
    {{ item.name|upper }}
{% endfor %}
{% endraw %}
</code></pre>
```

### comment

Comments are enclosed in `{# ... #}` and are stripped from the output entirely. They are not sent to the client.

**Single-line comment:**

```html
{# This is a comment — not rendered in output #}
<p>This is visible</p>
```

**Multi-line comment:**

```html
{# This entire block is a comment. None of this will appear in the rendered
HTML. Useful for documenting template logic. #}
```

**Inline comment:**

```html
<p>Hello {{ name }} {# TODO: add greeting based on time of day #}</p>
```

**Commenting out code:**

```html
{# {% for item in debug_items %}
<p>{{ item }}</p>
{% endfor %} #}
```

Comments cannot be nested. A `{#` inside a comment does not start a new comment level.

### filter / endfilter

Applies one or more filters to an entire block of content. The block is rendered first, then the result string is passed through the filter chain.

**Basic syntax:**

```html
{% filter upper %} this text will be uppercased {% endfilter %}
```

Output: `THIS TEXT WILL BE UPPERCASED`

**Chained filters:**

```html
{% filter lower|capitalize %} HELLO WORLD {% endfilter %}
```

Output: `Hello world`

**Filter with parameters:**

```html
{% filter truncate(50) %} This is a very long piece of text that should be
truncated to fit within the specified character limit. {% endfilter %}
```

**Practical use — indenting a block:**

```html
<pre>
{% filter indent(4) %}
line one
line two
line three
{% endfilter %}
</pre>
```

### autoescape

Controls HTML auto-escaping behavior within a block. When auto-escaping is on (the default), all variable output is HTML-escaped to prevent XSS attacks. When off, raw HTML is output as-is.

**Syntax:**

```html
{% autoescape true %}
    {# All variables are HTML-escaped in this block #}
    {{ user_input }}  {# <script> becomes &lt;script&gt; #}
{% endautoescape %}

{% autoescape false %}
    {# Variables are NOT escaped — use with trusted content only #}
    {{ trusted_html }}
{% endautoescape %}
```

Autoescape blocks are nestable — inner blocks override outer:

```html
{% autoescape false %} {{ raw_html }} {# NOT escaped #} {% autoescape true %} {{
user_input }} {# escaped — inner block overrides #} {% endautoescape %} {{
more_raw }} {# NOT escaped — outer block restored #} {% endautoescape %}
```

**When to disable auto-escaping:**

- Rendering HTML content from a trusted CMS or WYSIWYG editor
- Outputting pre-sanitized HTML
- Never with user-supplied input

**Prefer the `safe` filter for individual variables** rather than disabling autoescape for an entire block:

```html
{# Preferred — surgical control #} {{ trusted_html|safe }} {{ user_input }} {#
still escaped #} {# Avoid — disables protection for everything in the block #}
{% autoescape false %} {{ trusted_html }} {{ user_input }} {# DANGER: not
escaped! #} {% endautoescape %}
```

**Auto-escaping and filters:**

When auto-escaping is on, the `escape` filter is applied automatically after all other filters. The `safe` filter marks a value as already-safe, preventing the automatic escape step:

```html
{% autoescape true %} {{ value|upper }} {# uppercased then escaped #} {{
value|upper|safe }} {# uppercased, NOT escaped #} {% endautoescape %}
```

### do

Execute an expression for its side effects, discarding the result. Useful for mutating lists and dicts.

```html
{% set items = [] %} {% do items.append("first") %} {% do items.extend([2, 3])
%} {{ items|join(', ') }} {# first, 2, 3 #} {% set config = {} %} {% do
config.update({'debug': true}) %}
```

### debug

Dump the current template context for debugging. Outputs `repr(context)`.

```html
{% debug %} {# Outputs: {'name': 'Alice', 'items': [1, 2, 3], ...} #}
```

### trans / endtrans

Mark a block for internationalization. When an `i18n_callback` is configured on the `TemplateEngine`, the body text is passed to the callback for translation. Variable bindings use `%(name)s` placeholders.

```html
{% trans %}Hello World{% endtrans %} {% trans name=user.name %}Hello %(name)s{%
endtrans %}
```

```python
translations = {"Hello World": "Hola Mundo", "Hello %(name)s": "Hola %(name)s"}
engine = TemplateEngine(i18n_callback=lambda key: translations.get(key, key))
```

Without a callback, `{% trans %}` renders the body text unchanged (passthrough).

---

## Filters

Filters transform variable values. They are applied with the pipe operator `|` and can be chained. Some filters accept arguments in parentheses.

```html
{{ variable|filter }} {{ variable|filter(arg) }} {{ variable|filter(arg1, arg2)
}} {{ variable|filter1|filter2|filter3 }}
```

### String Filters

#### `escape` / `e`

HTML-escapes a string. Converts `<`, `>`, `&`, `"`, and `'` to their HTML entity equivalents.

```html
{{ html_content|escape }} {{ html_content|e }}
```

| Input                           | Output                                                |
| ------------------------------- | ----------------------------------------------------- |
| `<script>alert('xss')</script>` | `&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;` |
| `"Hello" & 'World'`             | `&quot;Hello&quot; &amp; &#x27;World&#x27;`           |

With auto-escaping enabled (the default), this filter is applied automatically. Applying it manually results in double-escaping only if the value is not already marked safe.

#### `safe`

Marks a string as safe HTML, preventing auto-escaping. Use only with trusted content.

```html
{{ trusted_html|safe }} {{ "<b>bold</b>"|safe }}
```

| Input         | Output                      |
| ------------- | --------------------------- |
| `<b>bold</b>` | **bold** (rendered as HTML) |

#### `lower`

Converts a string to lowercase.

```html
{{ "HELLO"|lower }}
```

| Input           | Output        |
| --------------- | ------------- |
| `"HELLO WORLD"` | `hello world` |
| `"Mixed Case"`  | `mixed case`  |
| `""`            | `""`          |

#### `upper`

Converts a string to uppercase.

```html
{{ "hello"|upper }}
```

| Input           | Output        |
| --------------- | ------------- |
| `"hello world"` | `HELLO WORLD` |
| `"Mixed Case"`  | `MIXED CASE`  |

#### `title`

Converts a string to title case (first letter of each word capitalized).

```html
{{ "hello world"|title }}
```

| Input                   | Output                |
| ----------------------- | --------------------- |
| `"hello world"`         | `Hello World`         |
| `"the quick brown fox"` | `The Quick Brown Fox` |
| `"already Title"`       | `Already Title`       |

#### `capitalize`

Capitalizes the first character of the string, lowercases the rest.

```html
{{ "hello world"|capitalize }}
```

| Input           | Output        |
| --------------- | ------------- |
| `"hello world"` | `Hello world` |
| `"HELLO"`       | `Hello`       |
| `""`            | `""`          |

#### `trim` / `strip`

Removes leading and trailing whitespace. `strip` is an alias for `trim`.

```html
{{ " hello "|trim }} {{ " hello "|strip }}
```

| Input           | Output  |
| --------------- | ------- |
| `"  hello  "`   | `hello` |
| `"\n\thello\n"` | `hello` |

#### `replace`

Replaces all occurrences of a substring with another string.

**Syntax:** `replace(old, new)`

```html
{{ "Hello World"|replace("World", "HyperDjango") }}
```

| Input           | `old`   | `new`   | Output               |
| --------------- | ------- | ------- | -------------------- |
| `"foo bar foo"` | `"foo"` | `"baz"` | `"baz bar baz"`      |
| `"hello"`       | `"x"`   | `"y"`   | `"hello"` (no match) |

#### `truncate`

Truncates a string to a given length and appends an ellipsis (`...`). Words are not split by default.

**Syntax:** `truncate(length, killwords=false, end="...")`

```html
{{ "This is a long sentence"|truncate(15) }}
```

| Input                       | Length | Output           |
| --------------------------- | ------ | ---------------- |
| `"This is a long sentence"` | `15`   | `"This is a..."` |
| `"Short"`                   | `100`  | `"Short"`        |
| `"Hello World"`             | `5`    | `"..."`          |

#### `wordcount`

Counts the number of words in a string.

```html
{{ "hello beautiful world"|wordcount }}
```

| Input                     | Output |
| ------------------------- | ------ |
| `"hello beautiful world"` | `3`    |
| `""`                      | `0`    |
| `"one"`                   | `1`    |

#### `wordwrap`

Wraps text at a specified column width.

**Syntax:** `wordwrap(width=79, break_long_words=true, wrapstring="\n")`

```html
{{ long_text|wordwrap(72) }}
```

| Input                         | Width | Output                         |
| ----------------------------- | ----- | ------------------------------ |
| `"The quick brown fox jumps"` | `15`  | `"The quick brown\nfox jumps"` |

#### `center`

Centers a string within a field of a given width, padding with spaces.

**Syntax:** `center(width=80)`

```html
{{ "hello"|center(20) }}
```

| Input  | Width | Output         |
| ------ | ----- | -------------- |
| `"hi"` | `10`  | `"    hi    "` |

#### `indent`

Indents each line of text by a given number of spaces.

**Syntax:** `indent(width=4, first=false, blank=false)`

```html
{{ text|indent(4) }} {{ text|indent(8, first=true) }}
```

| Parameter | Default | Description                      |
| --------- | ------- | -------------------------------- |
| `width`   | `4`     | Number of spaces to indent       |
| `first`   | `false` | Whether to indent the first line |
| `blank`   | `false` | Whether to indent blank lines    |

#### `striptags`

Strips all HTML/XML tags from a string, leaving only the text content.

```html
{{ "
<p>Hello <b>world</b></p>
"|striptags }}
```

| Input                         | Output         |
| ----------------------------- | -------------- |
| `"<p>Hello <b>world</b></p>"` | `Hello world`  |
| `"no tags here"`              | `no tags here` |

#### `urlencode`

URL-encodes a string for safe use in URLs.

```html
{{ "hello world & more"|urlencode }}
```

| Input           | Output          |
| --------------- | --------------- |
| `"hello world"` | `hello%20world` |
| `"a&b=c"`       | `a%26b%3Dc`     |

#### `urlize`

Converts URLs and email addresses in plain text to clickable HTML links.

```html
{{ "Visit https://example.com for more"|urlize }}
```

| Input                         | Output                                                        |
| ----------------------------- | ------------------------------------------------------------- |
| `"Go to https://example.com"` | `Go to <a href="https://example.com">https://example.com</a>` |

#### `format`

Applies Python-style `%` string formatting.

**Syntax:** `format(*args)`

```html
{{ "%s has %d items"|format(user.name, count) }} {{ "%.2f%%"|format(percentage)
}}
```

| Input      | Args            | Output        |
| ---------- | --------------- | ------------- |
| `"%s: %d"` | `("Alice", 42)` | `"Alice: 42"` |
| `"%.2f"`   | `(3.14159,)`    | `"3.14"`      |

### Numeric Filters

#### `abs`

Returns the absolute value of a number.

```html
{{ -5|abs }} {{ difference|abs }}
```

| Input  | Output |
| ------ | ------ |
| `-5`   | `5`    |
| `3.14` | `3.14` |
| `0`    | `0`    |

#### `round`

Rounds a number to a given number of decimal places.

**Syntax:** `round(precision=0, method="common")`

```html
{{ 3.14159|round(2) }} {{ 2.5|round }}
```

| Input     | Precision | Output |
| --------- | --------- | ------ |
| `3.14159` | `2`       | `3.14` |
| `2.5`     | `0`       | `3.0`  |
| `42.0`    | `2`       | `42.0` |

#### `int`

Converts a value to an integer. Returns `0` if conversion fails.

```html
{{ "42"|int }} {{ 3.7|int }}
```

| Input   | Output |
| ------- | ------ |
| `"42"`  | `42`   |
| `3.7`   | `3`    |
| `"abc"` | `0`    |

#### `float`

Converts a value to a floating-point number. Returns `0.0` if conversion fails.

```html
{{ "3.14"|float }} {{ 42|float }}
```

| Input    | Output |
| -------- | ------ |
| `"3.14"` | `3.14` |
| `42`     | `42.0` |
| `"abc"`  | `0.0`  |

#### `filesizeformat`

Formats a number of bytes as a human-readable file size.

```html
{{ 1048576|filesizeformat }} {{ file.size|filesizeformat }}
```

| Input           | Output    |
| --------------- | --------- |
| `0`             | `0 Bytes` |
| `1024`          | `1.0 kB`  |
| `1048576`       | `1.0 MB`  |
| `1073741824`    | `1.0 GB`  |
| `1099511627776` | `1.0 TB`  |

### Collection Filters

#### `length` / `count`

Returns the length of a string, list, dict, or other sized object. `count` is an alias.

```html
{{ items|length }} {{ "hello"|length }} {{ data|count }}
```

| Input              | Output |
| ------------------ | ------ |
| `[1, 2, 3]`        | `3`    |
| `"hello"`          | `5`    |
| `{"a": 1, "b": 2}` | `2`    |
| `[]`               | `0`    |

#### `first`

Returns the first element of a sequence.

```html
{{ items|first }} {{ "hello"|first }}
```

| Input          | Output       |
| -------------- | ------------ |
| `[10, 20, 30]` | `10`         |
| `"hello"`      | `h`          |
| `[]`           | `""` (empty) |

#### `last`

Returns the last element of a sequence.

```html
{{ items|last }} {{ "hello"|last }}
```

| Input          | Output |
| -------------- | ------ |
| `[10, 20, 30]` | `30`   |
| `"hello"`      | `o`    |

#### `join`

Joins a list into a string with a separator.

**Syntax:** `join(separator="")`

```html
{{ tags|join(", ") }} {{ letters|join }} {{ items|join(" | ") }}
```

| Input             | Separator | Output    |
| ----------------- | --------- | --------- |
| `["a", "b", "c"]` | `", "`    | `a, b, c` |
| `[1, 2, 3]`       | `"-"`     | `1-2-3`   |
| `["hello"]`       | `", "`    | `hello`   |

#### `sort`

Sorts a list. By default sorts in ascending order.

**Syntax:** `sort(reverse=false, attribute=none)`

```html
{{ items|sort }} {{ users|sort(attribute="name") }} {{ scores|sort(reverse=true)
}}
```

| Input             | Output            |
| ----------------- | ----------------- |
| `[3, 1, 2]`       | `[1, 2, 3]`       |
| `["c", "a", "b"]` | `["a", "b", "c"]` |

With `attribute`, sorts a list of objects by the given attribute name.

#### `reverse`

Reverses a list or string.

```html
{{ items|reverse }} {{ "hello"|reverse }}
```

| Input       | Output      |
| ----------- | ----------- |
| `[1, 2, 3]` | `[3, 2, 1]` |
| `"hello"`   | `olleh`     |

#### `unique`

Removes duplicate values from a list, preserving order.

```html
{{ items|unique }}
```

| Input             | Output       |
| ----------------- | ------------ |
| `[1, 2, 2, 3, 1]` | `[1, 2, 3]`  |
| `["a", "b", "a"]` | `["a", "b"]` |

#### `batch`

Groups items into fixed-size batches (sublists). Useful for creating rows in a grid layout.

**Syntax:** `batch(size, fill_with=none)`

```html
{% for row in items|batch(3) %}
<div class="row">
  {% for item in row %}
  <div class="col">{{ item }}</div>
  {% endfor %}
</div>
{% endfor %}
```

| Input             | Size | Output                |
| ----------------- | ---- | --------------------- |
| `[1, 2, 3, 4, 5]` | `3`  | `[[1, 2, 3], [4, 5]]` |
| `[1, 2, 3, 4]`    | `2`  | `[[1, 2], [3, 4]]`    |

#### `sum`

Sums a list of numbers. Returns `0` for an empty list.

```html
{{ prices|sum }} {{ items|map("price")|sum }}
```

| Input          | Output |
| -------------- | ------ |
| `[1, 2, 3]`    | `6`    |
| `[10.5, 20.3]` | `30.8` |
| `[]`           | `0`    |

#### `min`

Returns the minimum value from a list.

```html
{{ scores|min }}
```

| Input             | Output |
| ----------------- | ------ |
| `[5, 2, 8, 1]`    | `1`    |
| `["b", "a", "c"]` | `a`    |

#### `max`

Returns the maximum value from a list.

```html
{{ scores|max }}
```

| Input          | Output |
| -------------- | ------ |
| `[5, 2, 8, 1]` | `8`    |

#### `map`

Extracts an attribute from each item in a list, returning a new list.

**Syntax:** `map(attribute)`

```html
{{ users|map("name") }} {{ users|map("name")|join(", ") }}
```

| Input                                  | Attribute | Output             |
| -------------------------------------- | --------- | ------------------ |
| `[{"name": "Alice"}, {"name": "Bob"}]` | `"name"`  | `["Alice", "Bob"]` |

#### `select`

Filters a list, keeping only items that pass an is-test.

**Syntax:** `select(test)`

```html
{{ numbers|select("odd") }} {{ items|select("defined") }}
```

| Input             | Test     | Output      |
| ----------------- | -------- | ----------- |
| `[1, 2, 3, 4, 5]` | `"odd"`  | `[1, 3, 5]` |
| `[1, 2, 3, 4]`    | `"even"` | `[2, 4]`    |

#### `reject`

Filters a list, removing items that pass an is-test (the inverse of `select`).

**Syntax:** `reject(test)`

```html
{{ items|reject("none") }} {{ numbers|reject("odd") }}
```

| Input                | Test     | Output   |
| -------------------- | -------- | -------- |
| `[1, None, 3, None]` | `"none"` | `[1, 3]` |
| `[1, 2, 3, 4, 5]`    | `"odd"`  | `[2, 4]` |

#### `groupby`

Groups a list of objects by a common attribute, returning a list of `(grouper, list)` pairs.

**Syntax:** `groupby(attribute)`

```html
{% for group in users|groupby("role") %}
<h3>{{ group.grouper }}</h3>
<ul>
  {% for user in group.list %}
  <li>{{ user.name }}</li>
  {% endfor %}
</ul>
{% endfor %}
```

Given `users = [{"name": "Alice", "role": "admin"}, {"name": "Bob", "role": "user"}, {"name": "Carol", "role": "admin"}]`, this produces two groups: `admin` (Alice, Carol) and `user` (Bob).

#### `dictsort`

Sorts a dictionary by key, returning a list of `(key, value)` pairs.

```html
{% for key, value in data|dictsort %} {{ key }}: {{ value }} {% endfor %}
```

| Input                      | Output                           |
| -------------------------- | -------------------------------- |
| `{"b": 2, "a": 1, "c": 3}` | `[("a", 1), ("b", 2), ("c", 3)]` |

#### `items`

Converts a dictionary to a list of `(key, value)` pairs. Equivalent to Python's `dict.items()`.

```html
{% for key, value in config|items %}
<p>{{ key }} = {{ value }}</p>
{% endfor %}
```

| Input                                 | Output                                    |
| ------------------------------------- | ----------------------------------------- |
| `{"host": "localhost", "port": 8000}` | `[("host", "localhost"), ("port", 8000)]` |

#### `attr`

Gets a named attribute from an object.

**Syntax:** `attr(name)`

```html
{{ item|attr("name") }}
```

Equivalent to `item.name` but allows dynamic attribute names via variables.

#### `list`

Converts a value to a list. Useful with generators or range objects.

```html
{{ range(5)|list }} {{ "hello"|list }}
```

| Input      | Output                      |
| ---------- | --------------------------- |
| `range(5)` | `[0, 1, 2, 3, 4]`           |
| `"hello"`  | `["h", "e", "l", "l", "o"]` |

### Type Conversion Filters

#### `string`

Converts a value to its string representation.

```html
{{ 42|string }} {{ true|string }}
```

| Input  | Output   |
| ------ | -------- |
| `42`   | `"42"`   |
| `3.14` | `"3.14"` |
| `true` | `"True"` |
| `none` | `"None"` |

#### `bool`

Converts a value to a boolean.

```html
{{ ""|bool }} {{ "hello"|bool }} {{ 0|bool }}
```

| Input     | Output  |
| --------- | ------- |
| `""`      | `false` |
| `"hello"` | `true`  |
| `0`       | `false` |
| `1`       | `true`  |
| `[]`      | `false` |

### Serialization Filters

#### `tojson`

Serializes a value to a JSON string. The output is safe for embedding in `<script>` tags and HTML attributes.

```html
<script>
  var data = {{ items|tojson }};
  var config = {{ settings|tojson }};
</script>
```

| Input              | Output             |
| ------------------ | ------------------ |
| `[1, 2, 3]`        | `[1, 2, 3]`        |
| `{"key": "value"}` | `{"key": "value"}` |
| `"hello"`          | `"hello"`          |

The output is automatically marked safe (no HTML escaping applied to the JSON).

### Default/Fallback Filters

#### `default`

Returns a default value if the variable is falsy (undefined, `None`, empty string, empty list, `0`, `false`).

**Syntax:** `default(value, boolean=true)`

```html
{{ name|default("Anonymous") }} {{ count|default(0) }} {{ items|default([]) }}
```

| Input     | Default | Output  |
| --------- | ------- | ------- |
| `""`      | `"N/A"` | `N/A`   |
| `none`    | `"N/A"` | `N/A`   |
| `0`       | `"N/A"` | `N/A`   |
| `"hello"` | `"N/A"` | `hello` |
| `42`      | `0`     | `42`    |

### Complete Filter Reference Table

All 42 native filters:

| #   | Filter           | Aliases | Arguments                                | Description                       |
| --- | ---------------- | ------- | ---------------------------------------- | --------------------------------- |
| 1   | `escape`         | `e`     | —                                        | HTML-escape special characters    |
| 2   | `safe`           | —       | —                                        | Mark as safe (skip auto-escape)   |
| 3   | `lower`          | —       | —                                        | Lowercase string                  |
| 4   | `upper`          | —       | —                                        | Uppercase string                  |
| 5   | `title`          | —       | —                                        | Title Case string                 |
| 6   | `capitalize`     | —       | —                                        | Capitalize first character        |
| 7   | `trim`           | `strip` | —                                        | Strip leading/trailing whitespace |
| 8   | `length`         | `count` | —                                        | Length of string/list/dict        |
| 9   | `default`        | —       | `value`, `boolean=true`                  | Default for falsy values          |
| 10  | `join`           | —       | `separator=""`                           | Join list into string             |
| 11  | `first`          | —       | —                                        | First element of sequence         |
| 12  | `last`           | —       | —                                        | Last element of sequence          |
| 13  | `int`            | —       | —                                        | Convert to integer                |
| 14  | `float`          | —       | —                                        | Convert to float                  |
| 15  | `string`         | —       | —                                        | Convert to string                 |
| 16  | `replace`        | —       | `old`, `new`                             | String replacement                |
| 17  | `truncate`       | —       | `length`, `killwords=false`, `end="..."` | Truncate with ellipsis            |
| 18  | `wordcount`      | —       | —                                        | Count words                       |
| 19  | `urlencode`      | —       | —                                        | URL-encode string                 |
| 20  | `striptags`      | —       | —                                        | Remove HTML tags                  |
| 21  | `abs`            | —       | —                                        | Absolute value                    |
| 22  | `round`          | —       | `precision=0`, `method="common"`         | Round number                      |
| 23  | `sort`           | —       | `reverse=false`, `attribute=none`        | Sort list                         |
| 24  | `reverse`        | —       | —                                        | Reverse list or string            |
| 25  | `unique`         | —       | —                                        | Remove duplicates from list       |
| 26  | `tojson`         | —       | —                                        | JSON serialize                    |
| 27  | `list`           | —       | —                                        | Convert to list                   |
| 28  | `bool`           | —       | —                                        | Convert to boolean                |
| 29  | `batch`          | —       | `size`, `fill_with=none`                 | Group into sublists               |
| 30  | `sum`            | —       | —                                        | Sum numeric list                  |
| 31  | `min`            | —       | —                                        | Minimum value                     |
| 32  | `max`            | —       | —                                        | Maximum value                     |
| 33  | `map`            | —       | `attribute`                              | Extract attribute from each item  |
| 34  | `indent`         | —       | `width=4`, `first=false`, `blank=false`  | Indent text lines                 |
| 35  | `center`         | —       | `width=80`                               | Center text in field              |
| 36  | `dictsort`       | —       | —                                        | Sort dict by keys                 |
| 37  | `items`          | —       | —                                        | Dict to (key, value) pairs        |
| 38  | `groupby`        | —       | `attribute`                              | Group list by attribute           |
| 39  | `filesizeformat` | —       | —                                        | Human-readable file size          |
| 40  | `wordwrap`       | —       | `width=79`, `break_long_words=true`      | Wrap text at width                |
| 41  | `format`         | —       | `*args`                                  | Python `%` string formatting      |
| 42  | `urlize`         | —       | —                                        | Auto-link URLs in text            |
| 43  | `select`         | —       | `test`                                   | Keep items passing test           |
| 44  | `reject`         | —       | `test`                                   | Remove items passing test         |
| 45  | `attr`           | —       | `name`                                   | Get attribute by name             |

### Filter Chaining

Filters are applied left to right. The output of one filter becomes the input of the next:

```html
{{ name|lower|capitalize }} {# "ALICE" -> "alice" -> "Alice" #} {{
text|striptags|truncate(100)|escape }} {# Remove HTML -> truncate -> escape for
safe output #} {{ items|sort|reverse|first }} {# Sort ascending -> reverse to
descending -> take first (max) #} {{ users|map("email")|unique|sort|join(", ")
}} {# Extract emails -> deduplicate -> sort -> join as string #}
```

---

## Is-Tests

Is-tests are used with the `is` keyword inside `{% if %}` tags and with `select`/`reject` filters. They test a property of a value without modifying it.

**Syntax:**

```html
{% if value is testname %} {% if value is not testname %} {% if value is
testname(argument) %}
```

### All Is-Tests

#### `defined`

Returns `true` if the variable exists in the current context (even if its value is `None`, `0`, or `""`).

```html
{% if user is defined %} User exists: {{ user.name }} {% else %} No user in
context {% endif %}
```

#### `undefined`

The inverse of `defined`. Returns `true` if the variable does not exist in the context.

```html
{% if sidebar is undefined %} {# No sidebar content provided #} {% endif %}
```

#### `none`

Returns `true` if the value is `None`.

```html
{% if value is none %}
<p>No value provided</p>
{% endif %} {% if value is not none %}
<p>Value: {{ value }}</p>
{% endif %}
```

#### `true`

Returns `true` if the value is exactly `True`.

```html
{% if flag is true %}enabled{% endif %}
```

#### `false`

Returns `true` if the value is exactly `False`.

```html
{% if flag is false %}disabled{% endif %}
```

#### `string`

Returns `true` if the value is a string.

```html
{% if value is string %}
<p>String value: "{{ value }}"</p>
{% endif %}
```

#### `number`

Returns `true` if the value is a number (integer or float).

```html
{% if value is number %}
<p>Numeric value: {{ value }}</p>
{% endif %}
```

#### `integer`

Returns `true` if the value is an integer.

```html
{% if value is integer %}
<p>Integer: {{ value }}</p>
{% endif %}
```

#### `float`

Returns `true` if the value is a floating-point number.

```html
{% if value is float %}
<p>Float: {{ value }}</p>
{% endif %}
```

#### `odd`

Returns `true` if the numeric value is odd.

```html
{% if loop.index is odd %}
<tr class="odd">
  {% endif %} {# With select filter #} {{ numbers|select("odd") }}
</tr>
```

#### `even`

Returns `true` if the numeric value is even.

```html
{% if loop.index is even %}
<tr class="even">
  {% endif %} {{ numbers|select("even") }}
</tr>
```

#### `divisibleby`

Returns `true` if the value is divisible by the given number (modulo equals zero).

**Syntax:** `divisibleby(num)`

```html
{% if loop.index is divisibleby(3) %}
<hr />
{# Insert a divider every 3 items #} {% endif %} {% if year is divisibleby(4) %}
Might be a leap year {% endif %}
```

#### `iterable`

Returns `true` if the value can be iterated over (list, tuple, dict, string, generator, etc.).

```html
{% if value is iterable %}
<ul>
  {% for item in value %}
  <li>{{ item }}</li>
  {% endfor %}
</ul>
{% else %}
<p>{{ value }}</p>
{% endif %}
```

#### `mapping`

Returns `true` if the value is a dict-like mapping type.

```html
{% if value is mapping %}
<dl>
  {% for key, val in value|items %}
  <dt>{{ key }}</dt>
  <dd>{{ val }}</dd>
  {% endfor %}
</dl>
{% endif %}
```

#### `sequence`

Returns `true` if the value is a list or tuple (but not a string or mapping).

```html
{% if value is sequence %} {{ value|join(", ") }} {% endif %}
```

#### `callable`

Returns `true` if the value can be called as a function.

```html
{% if handler is callable %} {{ handler() }} {% endif %}
```

#### `sameas`

Returns `true` if the value is the exact same object as the argument (identity test, not equality).

**Syntax:** `sameas(other)`

```html
{% if item is sameas(none) %} Literally None {% endif %}
```

#### `escaped`

Returns `true` if the value is marked as safe/escaped HTML.

```html
{% if value is escaped %} Already safe {% endif %}
```

#### `in`

Returns `true` if the value is contained in the given sequence.

**Syntax:** `in(sequence)`

```html
{% if status is in(["active", "pending"]) %} Actionable status {% endif %}
```

#### `upper` / `lower`

Test whether a string is entirely uppercase or lowercase.

```html
{% if code is upper %}All caps{% endif %} {% if name is lower %}All lowercase{%
endif %}
```

### Is-Test Reference Table

| Test               | Arguments | Description                | Example                        |
| ------------------ | --------- | -------------------------- | ------------------------------ |
| `defined`          | —         | Variable exists in context | `{% if x is defined %}`        |
| `undefined`        | —         | Variable does not exist    | `{% if x is undefined %}`      |
| `none`             | —         | Value is `None`            | `{% if x is none %}`           |
| `true`             | —         | Value is exactly `True`    | `{% if x is true %}`           |
| `false`            | —         | Value is exactly `False`   | `{% if x is false %}`          |
| `string`           | —         | Value is a string          | `{% if x is string %}`         |
| `number`           | —         | Value is int or float      | `{% if x is number %}`         |
| `integer`          | —         | Value is an int            | `{% if x is integer %}`        |
| `float`            | —         | Value is a float           | `{% if x is float %}`          |
| `odd`              | —         | Value is odd integer       | `{% if x is odd %}`            |
| `even`             | —         | Value is even integer      | `{% if x is even %}`           |
| `divisibleby`      | `num`     | `value % num == 0`         | `{% if x is divisibleby(3) %}` |
| `iterable`         | —         | Value is iterable          | `{% if x is iterable %}`       |
| `mapping`          | —         | Value is a dict            | `{% if x is mapping %}`        |
| `sequence`         | —         | Value is a list/tuple      | `{% if x is sequence %}`       |
| `callable`         | —         | Value is callable          | `{% if x is callable %}`       |
| `sameas`           | `other`   | Identity test (`is`)       | `{% if x is sameas(y) %}`      |
| `eq`/`equalto`     | `other`   | Equality test              | `{% if x is eq(5) %}`          |
| `ne`               | `other`   | Not-equal test             | `{% if x is ne(0) %}`          |
| `gt`/`greaterthan` | `n`       | Greater-than test          | `{% if x is gt(3) %}`          |
| `ge`               | `n`       | Greater-or-equal test      | `{% if x is ge(5) %}`          |
| `lt`/`lessthan`    | `n`       | Less-than test             | `{% if x is lt(10) %}`         |
| `le`               | `n`       | Less-or-equal test         | `{% if x is le(5) %}`          |
| `escaped`          | —         | Value is marked safe       | `{% if x is escaped %}`        |
| `in`               | `seq`     | Value in sequence          | `{% if x is in(items) %}`      |
| `upper`            | —         | String is all uppercase    | `{% if x is upper %}`          |
| `lower`            | —         | String is all lowercase    | `{% if x is lower %}`          |

---

## Whitespace Control

By default, template tags produce the whitespace that surrounds them in the source file. Use the `-` modifier to strip whitespace before or after a tag.

**Syntax:**

- `{%-` — strip whitespace **before** the tag
- `-%}` — strip whitespace **after** the tag
- `{{-` / `-}}` — strip whitespace around variable output
- `{#-` / `-#}` — strip whitespace around comments

**Example without whitespace control:**

```html
<ul>
  {% for item in items %}
  <li>{{ item }}</li>
  {% endfor %}
</ul>
```

Output (note extra blank lines and indentation):

```html
<ul>
  <li>apple</li>

  <li>banana</li>
</ul>
```

**With whitespace control:**

```html
<ul>
  {%- for item in items %}
  <li>{{ item }}</li>
  {%- endfor %}
</ul>
```

Output (clean):

```html
<ul>
  <li>apple</li>
  <li>banana</li>
</ul>
```

**Full whitespace stripping:**

```html
<ul>
  {%- for item in items -%}
  <li>{{- item -}}</li>
  {%- endfor -%}
</ul>
```

Output (compact):

```html
<ul>
  <li>apple</li>
  <li>banana</li>
</ul>
```

**Rules:**

- The `-` must be immediately adjacent to `%`, `{`, or `}` (no space between)
- Stripping removes all whitespace (spaces, tabs, newlines) up to and including the nearest newline
- You can use `-` on one side only: `{%- tag %}` strips before, `{% tag -%}` strips after
- Works with all tag types: `{% %}`, `{{ }}`, and `{# #}`

---

## Template Engine Configuration

### TemplateEngine Constructor

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine(
    template_dir="templates",  # Path to template directory
    auto_reload=True,  # Check file mtime on each render (dev mode)
    autoescape=True,  # HTML-escape variables by default
    undefined="silent",  # "silent" | "strict" | "debug"
    sandboxed=False,  # Restrict access to __class__, __globals__, etc.
    block_start_string="{%",  # Custom block tag delimiter
    block_end_string="%}",
    variable_start_string="{{",  # Custom variable delimiter
    variable_end_string="}}",
    comment_start_string="{#",  # Custom comment delimiter
    comment_end_string="#}",
    cache_max_bytes=256 * 1024 * 1024,  # LRU cache size limit (256 MB default)
    bytecode_cache=True,  # Enable disk bytecode caching (.hztc files)
    bytecode_cache_dir=None,  # Default: {template_dir}/__pycache__/hztc
    i18n_callback=None,  # Translation function for {% trans %}: str -> str
)
```

| Parameter               | Type             | Default              | Description                                                                                                                                         |
| ----------------------- | ---------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `template_dir`          | `str`            | `"templates"`        | Filesystem path to the template directory. Resolved relative to the working directory.                                                              |
| `auto_reload`           | `bool`           | `True`               | When `True`, checks file modification time on each render and recompiles if the file changed. Set to `False` in production for maximum performance. |
| `autoescape`            | `bool`           | `True`               | When `True`, all variable output is HTML-escaped by default. Use `\|safe` to bypass for trusted content.                                            |
| `undefined`             | `str`            | `"silent"`           | Undefined variable behavior: `"silent"` renders empty, `"strict"` raises `RuntimeError`, `"debug"` renders `{{ variable_name }}`.                   |
| `sandboxed`             | `bool`           | `False`              | Restricts template access to dangerous attributes (`__class__`, `__globals__`, `__builtins__`, etc.). Use for user-supplied templates.              |
| `block_start_string`    | `str`            | `"{%"`               | Opening delimiter for block tags. Change to e.g. `"<%"` for ERB-style syntax.                                                                       |
| `block_end_string`      | `str`            | `"%}"`               | Closing delimiter for block tags.                                                                                                                   |
| `variable_start_string` | `str`            | `"{{"`               | Opening delimiter for variable output.                                                                                                              |
| `variable_end_string`   | `str`            | `"}}"`               | Closing delimiter for variable output.                                                                                                              |
| `comment_start_string`  | `str`            | `"{#"`               | Opening delimiter for comments.                                                                                                                     |
| `comment_end_string`    | `str`            | `"#}"`               | Closing delimiter for comments.                                                                                                                     |
| `max_string_len`        | `int`            | `0` (default 10 MB)  | Maximum string length in bytecode cache deserialization. 0 = default. Lower for sandboxed/untrusted cache files.                                    |
| `max_array_count`       | `int`            | `0` (default 100K)   | Maximum node/filter/branch array size in bytecode cache. 0 = default. Prevents OOM from crafted cache files.                                        |
| `max_expr_depth`        | `int`            | `0` (default 500)    | Maximum expression nesting depth in bytecode cache. 0 = default. Prevents stack overflow from deeply nested expressions.                            |
| `cache_max_bytes`       | `int`            | `268435456` (256 MB) | Maximum total size of cached compiled templates (measured by source file bytes). When exceeded, least-recently-used entries are evicted.            |
| `bytecode_cache`        | `bool`           | `True`               | Enable disk bytecode caching (`.hztc` files). Eliminates recompilation on process restart.                                                          |
| `bytecode_cache_dir`    | `str\|None`      | `None`               | Directory for `.hztc` files. Default: `{template_dir}/__pycache__/hztc`. Set to custom path for shared cache or deployment.                         |
| `i18n_callback`         | `Callable\|None` | `None`               | Translation function for `{% trans %}` blocks. Signature: `(str) -> str`. Pass `None` to render body unchanged.                                     |

### Template Directory Resolution

Templates are loaded from the configured `template_dir` path:

```python
# Relative path (resolved from current working directory)
engine = TemplateEngine("templates")

# Absolute path
engine = TemplateEngine("/var/www/myapp/templates")

# Via HyperApp
app = HyperApp("myapp", templates="templates")
```

Template names are relative to the template directory:

```python
engine.render("index.html")  # templates/index.html
engine.render("pages/about.html")  # templates/pages/about.html
engine.render("partials/header.html")  # templates/partials/header.html
```

**Path traversal protection:** Template names containing `../` that resolve outside the template directory raise `FileNotFoundError`. This prevents directory traversal attacks.

### Rendering Methods

```python
# Render a template file with context
html = engine.render("index.html", {"title": "Home", "items": [1, 2, 3]})

# Render from a string (no file needed)
html = engine.render_string("Hello {{ name }}!", {"name": "World"})

# Async rendering
html = await engine.render_async("index.html", {"title": "Home"})
```

### Cache Behavior

The engine uses a three-tier caching strategy:

1. **In-memory LRU** (fastest): Thread-safe `_LRUCache` with size-based eviction. Compile-once, render-many — the compiled Zig node tree is reused for every render call.
2. **Disk bytecode** (`.hztc` files): Survives process restarts. Automatically written on first compile, loaded on next cold start. Invalidated by FNV-1a source hash.
3. **Compile from source** (fallback): When both caches miss, reads source, compiles, writes `.hztc`, stores in LRU.

- **Auto-reload (development)**: When `auto_reload=True`, each render checks the file's modification time (`mtime`). If the file changed, it recompiles (and rewrites the `.hztc` cache).
- **No-reload (production)**: When `auto_reload=False`, templates are served from LRU without any filesystem checks. Maximum performance.

**Cache statistics:**

```python
cache = engine._compiled_cache
cache.count  # Number of cached templates
cache.total_bytes  # Total bytes of cached source
```

**Manual cache clear:**

```python
engine._compiled_cache.clear()  # Clear in-memory LRU only
engine.clear_bytecode_cache()  # Clear disk .hztc files (returns count)
```

### Custom Filter Registration

Register Python functions as template filters that can be used in any template:

```python
# Method 1: add_filter()
engine.add_filter("currency", lambda value, symbol="$": f"{symbol}{value:,.2f}")

# Method 2: @engine.filter decorator (on Library — see Template Tag Libraries)
```

Filters registered via `add_filter()` are:

- Available in all templates (current and future)
- Wired into already-compiled templates in the cache
- Passed through to the native Zig engine via `_template_register_filter`

**Filter function signature:**

The first argument is always the value being filtered. Additional arguments come from the template:

```python
def my_filter(value, arg1, arg2="default"):
    return transformed_value


engine.add_filter("my_filter", my_filter)
```

```html
{{ value|my_filter(arg1) }} {{ value|my_filter(arg1, arg2="custom") }}
```

### Global Variable Registration

Add variables or functions available in every template render without passing them in the context:

```python
engine.add_global("site_name", "My Website")
engine.add_global("now", lambda: datetime.now(UTC))
engine.add_global("static", get_static_url)
engine.add_global("debug", settings.DEBUG)
```

```html
<title>{{ site_name }}</title>
<p>Current time: {{ now() }}</p>
<link rel="stylesheet" href="{{ static('css/main.css') }}" />
```

Globals are merged into the context dict on each render. Context variables with the same name override globals.

---

## Template Tag Libraries

For organizing reusable filters and tags into libraries (similar to Django's `templatetags` system):

```python
from hyperdjango.templating import Library

register = Library("my_tags")
```

### Registering Filters

```python
# Decorator with auto-name (function name becomes filter name)
@register.filter
def currency(value, symbol="$"):
    return f"{symbol}{value:,.2f}"


# Decorator with explicit name
@register.filter("shout")
def make_loud(value):
    return str(value).upper() + "!"
```

```html
{{ price|currency }} {# Uses function name "currency" #} {{
price|currency("EUR") }} {# With argument #} {{ name|shout }} {# Uses explicit
name "shout" #}
```

### Registering Simple Tags

Simple tags are registered as global callable functions:

```python
@register.simple_tag
def current_time(fmt="%H:%M"):
    return datetime.now().strftime(fmt)


@register.simple_tag("version")
def get_version():
    return "2.1.0"
```

```html
{{ current_time() }} {{ current_time("%Y-%m-%d %H:%M") }} {{ version() }}
```

### Registering Inclusion Tags

Inclusion tags render a sub-template with a context returned by the decorated function:

```python
@register.inclusion_tag("_sidebar.html")
def sidebar(user):
    return {"items": get_sidebar_items(user)}
```

```html
{{ sidebar(user) }}
```

### Loading Libraries

Load a library into an engine to make its filters and tags available:

```python
engine.load_library("my_tags")

# Or by module path
engine.load_library("myapp.templatetags.my_tags")
```

### Library Discovery

```python
from hyperdjango.templating import get_library, get_all_libraries

lib = get_library("my_tags")
all_libs = get_all_libraries()
```

---

## Context Processors and Globals

Context processors add variables to every template render. In HyperDjango, this is done by registering globals on the engine:

```python
engine.add_global("static", get_static_url)
engine.add_global("csrf_token", lambda: generate_csrf_token())
engine.add_global("user", lambda: get_current_user())
engine.add_global("debug", settings.DEBUG)
engine.add_global("app_version", "2.1.0")
```

These are available in every template without passing them in the context dict:

```html
<link rel="stylesheet" href="{{ static('css/main.css') }}" />
<p>Logged in as {{ user().name }}</p>
{% if debug %}
<div class="debug-bar">...</div>
{% endif %}
```

**Callable globals vs. static globals:**

- **Static values** (`"2.1.0"`, `True`): evaluated once at registration time
- **Callable values** (`lambda: datetime.now()`): called on each template render, producing fresh values

When using `HyperApp`, the CSRF middleware automatically injects `csrf_token` into the template context.

---

## CSRF Token

When using `CSRFMiddleware`, include the CSRF token in forms to protect against Cross-Site Request Forgery:

```html
<form method="post" action="/submit">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <input type="text" name="message" />
  <button type="submit">Send</button>
</form>
```

The `csrf_token` variable is automatically injected into the template context by the CSRF middleware. No manual registration is needed.

**For AJAX requests**, include the token in a request header:

```html
<script>
  const csrfToken = "{{ csrf_token }}";
  fetch("/api/data", {
    method: "POST",
    headers: {
      "X-CSRF-Token": csrfToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ key: "value" }),
  });
</script>
```

---

## Performance

### Benchmarks

| Metric                   | HyperDjango (Zig)            | Jinja2               | Speedup         |
| ------------------------ | ---------------------------- | -------------------- | --------------- |
| Render (cached template) | **36 us**                    | 61 us                | **1.7x faster** |
| Compile (first load)     | **7.1 us**                   | 1,663 us             | **234x faster** |
| Expression parsing       | **native recursive descent** | Python regex         | —               |
| Output buffer            | **contiguous Zig memory**    | Python string concat | —               |

### How It Works

1. **Compilation** (once per template): Source text is lexed into tokens, parsed by a recursive descent parser into a node tree, and stored as a Zig-allocated structure. This node tree is wrapped in a Python `PyCapsule` and cached.

2. **Rendering** (every request): The node tree is walked, variables are resolved from the Python context dict, and output is written to a growable Zig buffer. The final buffer is returned to Python as a single string.

3. **Caching**: Compiled templates are stored in a thread-safe, size-based LRU cache. Default budget is 256 MB of source bytes. Each template's source size is used as a proxy for compiled size (they scale linearly).

### Compilation Details

The Zig template engine implements:

- **Lexer**: Scans source for `{{`, `{%`, `{#` delimiters. Produces token stream.
- **Parser**: Recursive descent. Handles operator precedence, nested expressions, parenthesized grouping, unspaced operators (`a+b`), list/tuple/dict literals, subscript chains (`a[0][1]`), method calls (`a.method()`).
- **49 native filters**: Implemented in Zig. Multi-arg filters (e.g., `replace('old', 'new')`) use proper expression parser call_args.
- **28 is-tests**: Type checks, numeric tests, comparison tests (`divisibleby(n)`, `sameas`, `eq`/`ne`/`gt`/`ge`/`lt`/`le`). Tokenized at parse time via `TestType` enum, dispatched via O(1) switch.
- **Macros**: Parameters with defaults. Call blocks via `{{ caller() }}`.
- **Template inheritance**: `extends`, `block`, `super()` resolved at render time.
- **Import**: `{% import "file" as ns %}` for macro namespacing.

### Production Configuration

For maximum performance in production, disable auto-reload:

```python
engine = TemplateEngine(
    template_dir="templates",
    auto_reload=False,  # No mtime checks — compile once, serve forever
    autoescape=True,
    cache_max_bytes=512 * 1024 * 1024,  # 512 MB for large template sets
)
```

### Sandbox Mode

For user-supplied or untrusted templates, enable sandbox mode to prevent code injection:

```python
engine = TemplateEngine(sandboxed=True)
```

Sandbox mode blocks access to:

- All dunder attributes (`__class__`, `__subclasses__`, `__globals__`, `__builtins__`, `__mro__`, `__init__`, `__dict__`, etc.)
- Dangerous frame/code attributes (`func_globals`, `gi_frame`, `gi_code`, `f_locals`, `f_globals`)

Safe dunders are allowed: `__len__`, `__iter__`, `__getitem__`, `__contains__`, `__str__`, `__repr__`, `__bool__`, `__int__`, `__float__`.

Blocked access raises `RuntimeError` (matching Jinja2's `SecurityError` behavior).

### Bytecode Caching

Compiled templates are automatically serialized to `.hztc` bytecode files on disk. On subsequent process starts, the engine loads from bytecode instead of re-parsing — eliminating the compile step entirely.

**Three-tier caching**:

1. **In-memory LRU** — fastest, per-process, cleared on restart
2. **Disk bytecode** (`.hztc` files) — survives restarts, auto-invalidated by source hash
3. **Compile from source** — fallback when both caches miss

```python
engine = TemplateEngine(
    template_dir="templates",
    bytecode_cache=True,  # Enable disk cache (default: True)
    bytecode_cache_dir=None,  # Default: {template_dir}/__pycache__/hztc
)

# First render: compile → write .hztc → cache in LRU
engine.render("index.html", ctx)

# Process restart — new engine, empty LRU:
engine2 = TemplateEngine(template_dir="templates")
engine2.render("index.html", ctx)  # Loads from .hztc — no recompile!
```

**Cache directory layout**: Mirrors the template directory structure:

```
templates/
├── index.html
├── admin/
│   └── base.html
└── __pycache__/
    └── hztc/
        ├── index.html.hztc
        └── admin/
            └── base.html.hztc
```

**Merkle dependency hash**: Each `.hztc` file is validated by a Merkle hash computed from the main template source AND all transitive dependencies (includes, extends, imports). A `.hztc.meta` sidecar JSON file stores the dependency tree:

- Main source hash (FNV-1a 64-bit)
- Per-dependency content hash (path → FNV-1a)
- Combined Merkle hash (chained FNV-1a over sorted deps)

Changing ANY file in the dependency tree — even a deeply nested partial — invalidates the parent template's cache. The dependency tree is automatically discovered during compilation via the template loader callback.

**Atomic writes**: Both `.hztc` and `.hztc.meta` files are written via `tempfile.mkstemp()` + `os.replace()` — no partial reads possible, even under concurrent access.

**Management**:

```python
# Clear all bytecode cache files
count = engine.clear_bytecode_cache()  # Returns number of files removed

# Disable disk cache entirely
engine = TemplateEngine(bytecode_cache=False)

# Custom cache directory
engine = TemplateEngine(bytecode_cache_dir="/tmp/template_cache")
```

**Low-level API** (for custom cache strategies):

```python
from hyperdjango._hyperdjango_native import _template_serialize, _template_deserialize

cache_bytes = _template_serialize(capsule, source_text)
cached_capsule = _template_deserialize(cache_bytes, source_hash)  # None on mismatch
```

**Format**: Binary TLV (Tag-Length-Value) with:

- Magic bytes `HZTC` + format version (auto-invalidates on engine changes)
- FNV-1a 64-bit source hash (detects content changes)
- Depth-first serialized node tree with all expressions, filters, and child arrays

**Security hardening** (all limits configurable via `TemplateEngine` constructor):

- `max_string_len`: Maximum string in cache (default 10 MB, configurable)
- `max_array_count`: Maximum node/filter array size (default 100K, configurable)
- `max_expr_depth`: Maximum expression nesting (default 500, configurable)
- Enum validation before conversion (prevents undefined behavior from crafted values)
- Overflow-safe bounds checking (subtraction-based, not addition-based)
- `limit_exceeded` flag — any limit violation rejects the ENTIRE cache file, no silent degradation

```python
# Tighter limits for user-supplied cache files
engine = TemplateEngine(
    max_string_len=1000,  # 1KB max strings
    max_array_count=100,  # 100 max nodes
    max_expr_depth=50,  # 50 max nesting
)
```

**Performance**: 2.2x faster deserialization vs compiling from source (pure API benchmark). Disk cache adds file I/O overhead but eliminates the entire parse+compile pipeline on cold starts. Hash rejection (stale cache) takes only 80ns.

### Memory Usage

Each compiled template consumes approximately 2-5x its source file size in Zig heap memory (for the node tree, string interning, and expression structures). A 10 KB template source compiles to roughly 20-50 KB of Zig heap.

With the default 256 MB cache budget, you can cache thousands of templates simultaneously. The LRU eviction ensures that inactive templates are freed when memory pressure builds.

### Thread Safety

The template cache uses a Python `threading.Lock` for all access. Multiple threads can render different templates (or the same template with different contexts) concurrently. The Zig node tree is read-only after compilation, so rendering is lock-free.

Per-instance growable output buffers mean each render call has its own buffer — no contention between threads during rendering.
