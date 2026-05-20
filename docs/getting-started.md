# Getting Started

Build a complete web application from scratch. This tutorial covers project creation, models, database operations, views, routing, templates, forms, and admin — everything you need to ship a real app.

!!! tip "Cloned this repository instead?"
If you want to run one of the bundled services rather than start a new
project, go to **[Zero to Running: Clone and Run a Service](running-services.md)** —
`git clone` → `make bootstrap-db` → `uv run hyper service run <name>`.

## Requirements

| Dependency | Version               | Notes                                                        |
| ---------- | --------------------- | ------------------------------------------------------------ |
| Python     | 3.14t (free-threaded) | Set in `.python-version`. GIL-disabled for true parallelism. |
| PostgreSQL | 18+                   | `max_connections=10000` recommended for dev.                 |
| Zig        | 0.16+                 | Builds the native extension (.so/.dylib).                    |
| uv         | latest                | `curl -LsSf https://astral.sh/uv/install.sh \| sh`           |

HyperDjango has **no Python fallbacks** — the native Zig extension is required for all operations (HTTP server, database, validation, templates, JSON).

**Verify your prerequisites:**

```bash
python --version   # Should be 3.14+ (3.14t for free-threaded)
zig version        # Should be 0.16.0 or later
pg_isready         # Should print "accepting connections"
uv --version       # Should print uv version
```

## Part 1: Project Setup

### Install HyperDjango

```bash
uv init myproject && cd myproject
uv add hyperdjango

# Build the native Zig extension (required -- there are no fallbacks)
uv run hyper-build --install --release
```

Or use the scaffold command for a fully configured project:

```bash
uv run hyper new myproject --full
cd myproject
```

The `--full` preset creates a complete project with database, auth, admin, templates, and static files pre-configured.

### Verify the Build

```bash
uv run hyper doctor
```

This runs 30 diagnostic checks across 7 categories (build, python, database, performance, config, filesystem, security) and reports any issues.

### Your First App

```python
# app.py
from hyperdjango import HyperApp

app = HyperApp("myproject")


@app.get("/")
async def index(request):
    return {"message": "Hello from HyperDjango!"}


app.run(port=8000)
```

Run it:

```bash
uv run hyper run
```

Visit `http://localhost:8000/` -- you will see `{"message": "Hello from HyperDjango!"}`.

### Project Structure

A typical HyperDjango project:

```
myproject/
├── app.py              # Application entry point
├── models.py           # Database models
├── views.py            # View functions
├── commands.py         # Custom management commands
├── templates/          # HTML templates
│   └── base.html
├── static/             # CSS, JS, images
├── fixtures/           # Database seed data (JSON)
├── migrations/         # Database migrations
├── .env                # Environment settings
└── pyproject.toml
```

### Configure Settings

HyperDjango ships with 114 configurable settings covering database, security, caching, auth, rate limiting, email, logging, and static files. Settings load from three sources (in priority order):

1. **Environment variables** with `HYPER_*` prefix: `HYPER_SECRET_KEY=mysecret`
2. **`.env` file** in the project root (auto-loaded):
   ```bash
   # .env
   SECRET_KEY=your-secret-key-here
   DEBUG=true
   DATABASE_URL=postgres://localhost/myproject
   POOL_SIZE=0
   CACHE_BACKEND=memory
   ```
3. **Django-style settings** via `HYPERDJANGO_*` in your app config

Key settings categories:

| Category      | Examples                                                                             |
| ------------- | ------------------------------------------------------------------------------------ |
| Database      | `POOL_SIZE`, `PREPARED_STATEMENTS`, `CONNECT_TIMEOUT`, `QUERY_TIMEOUT`               |
| Security      | `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` |
| CSRF          | `CSRF_COOKIE_SECURE`, `CSRF_TRUSTED_ORIGINS`, `CSRF_COOKIE_SAMESITE`                 |
| Cache         | `CACHE_BACKEND` (memory/database), `CACHE_TTL`, `CACHE_MAX_BYTES`                    |
| Auth          | `PASSWORD_HASHER` (argon2id), `SESSION_COOKIE_AGE`, `LOGIN_URL`                      |
| Rate Limiting | `RATE_LIMIT_MAX_REQUESTS`, `RATE_LIMIT_WINDOW`                                       |

See the [Settings Reference](settings.md) for every setting with defaults and validation rules.

---

## Part 2: Models and Database

### Connect to PostgreSQL

```python
# app.py
from hyperdjango import HyperApp, Model, Field

app = HyperApp(
    "myproject",
    database="postgres://localhost/myproject",
    templates="templates",
)
```

### Define Models

```python
# models.py
from datetime import datetime, UTC
from hyperdjango import Model, Field


class Question(Model):
    class Meta:
        table = "questions"

    id: int = Field(primary_key=True, auto=True)
    question_text: str = Field()
    pub_date: datetime = Field(default=None)

    def __str__(self):
        return self.question_text

    def was_published_recently(self):
        if self.pub_date is None:
            return False
        return (datetime.now(UTC) - self.pub_date).days < 1


class Choice(Model):
    class Meta:
        table = "choices"

    id: int = Field(primary_key=True, auto=True)
    question_id: int = Field(foreign_key=Question)
    choice_text: str = Field()
    votes: int = Field(default=0)

    def __str__(self):
        return self.choice_text
```

### Anti-Enumeration with PublicIDMixin

Expose opaque IDs in your API instead of sequential integers to prevent enumeration attacks:

```python
from hyperdjango.public_id import PublicIDMixin, generate_alphabet, IDMode, IDStrategy
from hyperdjango import Model, Field


class Question(PublicIDMixin, Model):
    class Meta:
        table = "questions"

    class PublicIDConfig:
        # Generate once: print(generate_alphabet("olc32"))
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        mode = IDMode.SIGNED  # HMAC-signed, tamper-proof
        strategy = IDStrategy.ENCODED_PK  # Derives from internal PK

    id: int = Field(primary_key=True, auto=True)
    question_text: str = Field()
    pub_date: datetime = Field(default=None)
```

Integer PKs remain internal for fast joins. Public IDs are opaque strings for API responses:

```python
q = await Question.objects.get(id=1)
print(q.public_id)  # "W9gx3P.hmac_sig" — not guessable, not sequential

# Look up by public ID
q = await Question.get_by_public_id("W9gx3P.hmac_sig")
```

Available modes: `RAW`, `ENCODED`, `SIGNED`, `RANDOM`. Available strategies: `random`, `uuid7`, `encoded_pk`.

### Create Tables

```python
# In your setup script or app startup:
async def setup_db():
    db = app.db
    await db.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            question_text VARCHAR(200) NOT NULL,
            pub_date TIMESTAMPTZ
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS choices (
            id SERIAL PRIMARY KEY,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            choice_text VARCHAR(200) NOT NULL,
            votes INTEGER DEFAULT 0
        )
    """)
```

Or use migrations:

```bash
uv run hyper makemigrations
uv run hyper migrate
```

### Working with the Database

```python
from datetime import datetime, UTC
from models import Question, Choice

# Create
q = Question(question_text="What's up?", pub_date=datetime.now(UTC))
await q.save()
print(q.id)  # 1 (auto-assigned)

# Read
questions = await Question.objects.all()
q = await Question.objects.get(id=1)

# Filter
recent = (
    await Question.objects.filter(pub_date__gte=datetime(2024, 1, 1))
    .order_by("-pub_date")
    .all()
)

# Update
q.question_text = "What's new?"
await q.save()

# Delete
await q.delete()

# Create related objects
q = await Question.objects.get(id=1)
c = Choice(question_id=q.id, choice_text="Not much", votes=0)
await c.save()

# Query related objects
choices = await Choice.objects.filter(question_id=q.id).all()
```

### Field Lookups

QuerySet supports Django-style double-underscore lookups:

```python
# Exact match (default)
await Question.objects.filter(id=1).get()

# String lookups
await Question.objects.filter(question_text__contains="up").all()
await Question.objects.filter(question_text__icontains="UP").all()
await Question.objects.filter(question_text__startswith="What").all()

# Comparison
await Question.objects.filter(pub_date__gte=datetime(2024, 1, 1)).all()
await Question.objects.filter(votes__gt=0).all()

# Date parts
await Question.objects.filter(pub_date__year=2024).all()

# Null check
await Question.objects.filter(pub_date__isnull=False).all()

# IN lookup
await Question.objects.filter(id__in=[1, 2, 3]).all()

# Chaining
await (
    Question.objects.filter(pub_date__year=2024)
    .exclude(question_text__startswith="Why")
    .order_by("-pub_date")
    .limit(10)
)
```

---

## Part 3: Views and Routing

### Function-Based Views

```python
from hyperdjango import HyperApp, Response
from hyperdjango.shortcuts import get_object_or_404, redirect, render
from models import Question, Choice

app = HyperApp(
    "polls", database="postgres://localhost/myproject", templates="templates"
)


@app.get("/")
async def index(request):
    """List the latest 5 questions."""
    questions = await Question.objects.order_by("-pub_date").limit(5)
    return render(request, "polls/index.html", {"latest_questions": questions})


@app.get("/questions/{id:int}")
async def detail(request, id: int):
    """Show a question and its choices."""
    question = await get_object_or_404(Question, id=id)
    choices = await Choice.objects.filter(question_id=id).all()
    return render(
        request,
        "polls/detail.html",
        {
            "question": question,
            "choices": choices,
        },
    )


@app.post("/questions/{id:int}/vote")
async def vote(request, id: int):
    """Vote on a choice."""
    question = await get_object_or_404(Question, id=id)
    form = await request.form()
    choice_id = form.get("choice")

    if not choice_id:
        choices = await Choice.objects.filter(question_id=id).all()
        return render(
            request,
            "polls/detail.html",
            {
                "question": question,
                "choices": choices,
                "error_message": "You didn't select a choice.",
            },
            status=400,
        )

    choice = await get_object_or_404(Choice, id=int(choice_id))
    choice.votes += 1
    await choice.save()
    return redirect(f"/questions/{id}/results/")


@app.get("/questions/{id:int}/results")
async def results(request, id: int):
    """Show voting results."""
    question = await get_object_or_404(Question, id=id)
    choices = await Choice.objects.filter(question_id=id).order_by("-votes").all()
    return render(
        request,
        "polls/results.html",
        {
            "question": question,
            "choices": choices,
        },
    )
```

### URL Parameters

HyperDjango supports typed URL parameters:

```python
@app.get("/users/{id:int}")  # int parameter
async def user(request, id: int): ...


@app.get("/articles/{slug:str}")  # string parameter
async def article(request, slug: str): ...


@app.get("/files/{path:path}")  # path parameter (includes slashes)
async def file(request, path: str): ...
```

### Response Types

```python
from hyperdjango import Response


# JSON (default for dict/list returns)
@app.get("/api/questions")
async def api_list(request):
    return {"questions": [...]}  # auto-serialized to JSON


# HTML
return Response.html("<h1>Hello</h1>")

# Text
return Response.text("plain text")

# Redirect
return Response.redirect("/other/")

# Custom status
return Response.json({"error": "not found"}, status=404)

# Streaming
return Response.stream(generate_data())

# File download
return Response.file("report.pdf")

# SSE
return Response.sse(event_generator())
```

### Shortcuts

```python
from hyperdjango.shortcuts import get_object_or_404, get_list_or_404, redirect, render

# get_object_or_404 — raises 404 if not found
article = await get_object_or_404(Article, id=42)
article = await get_object_or_404(Article, slug="hello-world")

# get_list_or_404 — raises 404 if empty result
articles = await get_list_or_404(Article, published=True)

# redirect — temporary (302) or permanent (301)
return redirect("/articles/")
return redirect("/new-url/", permanent=True)

# render — template + context → HTML response
return render(request, "articles/list.html", {"articles": articles})
```

---

## Part 4: Templates

HyperDjango includes a native Zig template engine (41 filters, macros, extends/include, 1.5x faster than Jinja2) and also supports Jinja2.

### Template Syntax

```html
<!-- templates/polls/index.html -->
{% extends "base.html" %} {% block content %}
<h1>Latest Questions</h1>

{% if latest_questions %}
<ul>
  {% for question in latest_questions %}
  <li>
    <a href="/questions/{{ question.id }}/">{{ question.question_text }}</a>
    <small>{{ question.pub_date }}</small>
  </li>
  {% endfor %}
</ul>
{% else %}
<p>No questions available.</p>
{% endif %} {% endblock %}
```

### Base Template

```html
<!-- templates/base.html -->
<!DOCTYPE html>
<html>
  <head>
    <title>{% block title %}My App{% endblock %}</title>
    <link rel="stylesheet" href="/static/style.css" />
  </head>
  <body>
    <nav>
      <a href="/">Home</a>
    </nav>
    <main>{% block content %}{% endblock %}</main>
  </body>
</html>
```

### Detail Template with Form

```html
<!-- templates/polls/detail.html -->
{% extends "base.html" %} {% block title %}{{ question.question_text }}{%
endblock %} {% block content %}
<h1>{{ question.question_text }}</h1>

{% if error_message %}
<p class="error">{{ error_message }}</p>
{% endif %}

<form action="/questions/{{ question.id }}/vote" method="post">
  {% for choice in choices %}
  <label>
    <input type="radio" name="choice" value="{{ choice.id }}" />
    {{ choice.choice_text }} </label
  ><br />
  {% endfor %}
  <button type="submit">Vote</button>
</form>
{% endblock %}
```

### Template Features

- **Variables**: `{{ variable }}`, `{{ obj.attr }}`, `{{ list[0] }}`
- **Tags**: `{% if %}`, `{% for %}`, `{% extends %}`, `{% include %}`, `{% block %}`, `{% macro %}`, `{% with %}`, `{% set %}`
- **Filters**: `{{ name|upper }}`, `{{ price|round(2) }}`, `{{ items|length }}`, `{{ text|truncate(100) }}`
- **Expressions**: `{{ price * 1.1 }}`, `{{ "hello" ~ " world" }}`, `{{ x if condition else y }}`
- **Whitespace control**: `{%- tag -%}` trims surrounding whitespace
- **Macros**: Reusable template functions with parameters

### Humanize Filters

Load the `humanize` library for human-friendly formatting:

```python
engine = app.template_engine
engine.load_library("humanize")
```

```html
{{ 1000000|intcomma }}
<!-- "1,000,000" -->
{{ 1200000|intword }}
<!-- "1.2 million" -->
{{ post.created_at|naturaltime }}
<!-- "3 hours ago" -->
{{ file_size|filesizeformat }}
<!-- "4.2 MB" -->
{{ rank|ordinal }}
<!-- "3rd" -->
```

---

## Part 5: Forms

```python
from hyperdjango.forms import Form, CharField, IntegerField, ChoiceField


class QuestionForm(Form):
    question_text = CharField(max_length=200, label="Question")


class VoteForm(Form):
    choice = ChoiceField(choices=[])  # populated dynamically


@app.get("/questions/new")
async def new_question(request):
    form = QuestionForm()
    return render(request, "polls/new.html", {"form": form})


@app.post("/questions/new")
async def create_question(request):
    data = await request.form()
    form = QuestionForm(data=data)
    if form.is_valid():
        q = Question(
            question_text=form.cleaned_data["question_text"],
            pub_date=datetime.now(UTC),
        )
        await q.save()
        return redirect(f"/questions/{q.id}/")
    return render(request, "polls/new.html", {"form": form}, status=400)
```

---

## Part 6: Admin Interface

HyperAdmin provides automatic CRUD for your models with RBAC integration:

```python
from hyperdjango.admin import HyperAdmin

admin = HyperAdmin(app)
admin.register(Question)
admin.register(Choice)

# Access at /admin/ after creating a superuser:
# uv run hyper createsuperuser
```

The admin auto-generates:

- List view with search, filters, and pagination
- Create/edit forms with validation
- Delete confirmation
- Audit logging
- Role-based access control

---

## Part 7: Middleware

```python
from hyperdjango.standalone_middleware import CORSMiddleware, SecurityHeadersMiddleware

app = HyperApp("myproject", database="postgres://localhost/myproject")

# Add middleware (executed top-down on request, bottom-up on response)
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware(origins=["https://myapp.com"]))
```

Built-in middleware: CORS, Security Headers, Timing, Rate Limiting, Compression, CSRF, Access Logging.

---

## Part 8: Testing

```python
from hyperdjango.testing import TestClient

client = TestClient(app)

# GET request
response = client.get("/")
assert response.status == 200

# POST with JSON
response = client.post("/api/questions", json={"question_text": "What's new?"})
assert response.status == 201

# POST with form data
response = client.post("/questions/1/vote", data={"choice": "2"})
assert response.status == 302  # redirect after vote

# With authentication (set a bearer token or API key)
client.set_auth("my-token")
response = client.get("/admin/")
assert response.status == 200
```

---

## Part 9: REST API with ModelViewSet

For full CRUD APIs with pagination, filtering, search, and permissions, use `ModelViewSet` and `APIRouter`:

```python
from hyperdjango.rest import APIRouter, ModelSerializer, ModelViewSet


class QuestionSerializer(ModelSerializer):
    class Meta:
        model = Question
        fields = ["id", "question_text", "pub_date"]
        read_only_fields = ["id"]


class QuestionViewSet(ModelViewSet):
    serializer_class = QuestionSerializer
    queryset = Question.objects
    search_fields = ["question_text"]
    ordering_fields = ["pub_date"]


router = APIRouter()
router.register("questions", QuestionViewSet)
app.include_router(router, prefix="/api")
```

This generates `GET/POST /api/questions/` and `GET/PUT/PATCH/DELETE /api/questions/{id}/` with 4 pagination styles (page number, limit/offset, keyset cursor, server-side DECLARE CURSOR), 3 filter backends, throttling, content negotiation, and ETag caching.

---

## Part 10: Custom Commands

Register project-specific CLI commands with the `@command` decorator:

```python
# commands.py
from hyperdjango.commands import command

from models import Choice, Question


@command(name="seed", help="Seed the database with sample polls")
async def seed_command(count: int = 5, verbose: bool = False):
    for i in range(count):
        q = await Question.objects.create(
            question_text=f"Question {i}?",
        )
        for j in range(3):
            await Choice.objects.create(
                question_id=q.id,
                choice_text=f"Choice {j}",
            )
        if verbose:
            print(f"Created question {i} with 3 choices")
    print(f"Seeded {count} questions.")


@command(help="Show poll statistics")
async def stats():
    total = await Question.objects.count()
    print(f"Total questions: {total}")
```

Run your commands:

```bash
uv run hyper seed --count=20 --verbose
uv run hyper stats
```

Arguments are automatically typed from function signatures -- `int`, `str`, `bool` flags all work.

---

## Part 11: Fixtures

Seed your database or export snapshots with the fixture system:

```bash
# Export models to JSON (with FK dependency sorting)
uv run hyper dumpdata questions choices -o fixtures/polls.json

# Load fixtures (UPSERT -- creates new, updates existing)
uv run hyper loaddata fixtures/polls.json
```

Programmatic usage:

```python
from hyperdjango.fixtures import dumpdata, loaddata

json_str = await dumpdata([Question, Choice])
result = await loaddata("fixtures/polls.json")
print(f"Created: {result.created}, Updated: {result.updated}")
```

Fixtures support natural keys, FK dependency sorting, and UPSERT semantics.

---

## Part 12: Testing and Running

### Testing

```bash
# Run all tests
uv run hyper-test

# Run tests matching a pattern
uv run hyper-test polls
uv run hyper-test admin rest

# List all test files
uv run hyper-test --list
```

### Running in Development

```bash
uv run hyper run
```

### Production Build

```bash
# Build the native extension in ReleaseFast mode
uv run hyper-build --release

# Verify everything is configured correctly
uv run hyper doctor
```

### Deployment Checklist

1. `uv run hyper-build --release` -- build optimized native binary
2. `uv run hyper doctor` -- run 30 diagnostic checks
3. Set `HYPER_SECRET_KEY` to a real random value
4. Set `HYPER_DEBUG=false`
5. Restrict `HYPER_ALLOWED_HOSTS` to your domain(s)
6. Enable `SECURE_SSL_REDIRECT=true` and `SECURE_HSTS_SECONDS=31536000`
7. Enable `CSRF_COOKIE_SECURE=true` and `SESSION_COOKIE_SECURE=true`
8. Run `uv run hyper collectstatic` and `uv run hyper migrate`

See the [Deployment Guide](deployment.md) for Docker, systemd, and reverse proxy configuration.

---

## Troubleshooting

**Build fails with Zig errors:**

- Verify Zig version: `zig version` must be 0.16.0+
- On macOS, install via `brew install zig`. On Linux, download from [ziglang.org](https://ziglang.org/download/)
- Run `uv run hyper doctor --category build` for detailed diagnostics

**Database connection errors:**

- Verify PostgreSQL is running: `pg_isready`
- Check your connection string: `postgres://user:pass@localhost:5432/dbname`
- Check `max_connections` in `postgresql.conf` (recommend 10000 for development)

**"Native extension not found" errors:**

- Run `uv run hyper-build` (debug) or `uv run hyper-build --release` (production)
- The extension must be rebuilt after Zig upgrades

**Template not found errors:**

- Check `template_dir` points to the right directory
- Paths are relative to the working directory, not the Python file

---

## What's Next

Choose your learning path based on what you're building:

**Building a REST API:**

- [REST Framework](rest.md) — ViewSets, serializers, pagination, filtering
- [Auth & RBAC](auth.md) — Sessions, API keys, role-based permissions
- [OpenAPI](openapi.md) — Auto-generated API docs and Swagger UI

**Building a full-stack web app:**

- [Templates](templates.md) — Zig-compiled Jinja2-compatible engine
- [Forms](forms.md) — Validation, ModelForms, cross-field clean
- [Admin Panel](admin.md) — Auto-generated CRUD with RBAC

**Going to production:**

- [Security Guide](security-guide.md) — Middleware stack, CSRF, CORS, rate limiting
- [Deployment Guide](deployment-guide.md) — systemd, Docker, nginx reverse proxy
- [Production Scaling](production-scaling.md) — Caching, read replicas, monitoring

**Deep reference:**

- [Models & ORM](models.md) — Full ORM reference with lookups, relations, aggregation
- [Database](database.md) — Connection pooling, transactions, raw SQL, COPY
- [Service Walkthroughs](service-walkthroughs.md) — Annotated code tours of production patterns
- [Benchmarks](benchmarks.md) — Measured performance numbers with methodology
