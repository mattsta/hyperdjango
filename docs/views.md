# Views

Views handle HTTP requests and return responses. HyperDjango supports both function-based views (recommended) and class-based views.

## Function-Based Views

```python
from hyperdjango import HyperApp, Response

app = HyperApp("myapp")


@app.get("/")
async def index(request):
    return {"message": "Hello!"}  # auto-serialized to JSON


@app.get("/about")
async def about(request):
    return Response.html("<h1>About</h1>")


@app.post("/items")
async def create_item(request):
    data = await request.json()
    # ... create item ...
    return Response.json({"id": 1}, status=201)
```

### Route Decorators

| Decorator                         | HTTP Method      |
| --------------------------------- | ---------------- |
| `@app.get(path)`                  | GET              |
| `@app.post(path)`                 | POST             |
| `@app.put(path)`                  | PUT              |
| `@app.patch(path)`                | PATCH            |
| `@app.delete(path)`               | DELETE           |
| `@app.route(path, methods=[...])` | Multiple methods |

```python
@app.route("/items/{id}", methods=["GET", "PUT", "DELETE"])
async def item(request, id: int):
    if request.method == "GET":
        return await get_item(id)
    elif request.method == "PUT":
        return await update_item(id, request)
    elif request.method == "DELETE":
        return await delete_item(id)
```

## Response Types

```python
from hyperdjango import Response

# JSON (also returned automatically from dict/list)
Response.json({"key": "value"}, status=200)

# HTML
Response.html("<h1>Hello</h1>", status=200)

# Plain text
Response.text("Hello", status=200)

# Redirect
Response.redirect("/other/", status=302)  # temporary
Response.redirect("/new/", status=301)  # permanent

# Streaming
Response.stream(async_generator(), content_type="text/plain")

# Server-Sent Events
Response.sse(event_generator())

# File download
Response.file("path/to/report.pdf")
Response.attachment("data.csv", content=csv_bytes, content_type="text/csv")

# Empty response
Response(body=b"", status=204)
```

## Error Handling

### HTTPException

```python
from hyperdjango.app import HTTPException


@app.get("/items/{id}")
async def get_item(request, id: int):
    item = await db.query_one("SELECT * FROM items WHERE id = $1", id)
    if not item:
        raise HTTPException(404, "Item not found")
    return item
```

### Custom Exception Handlers

```python
@app.exception_handler(ValueError)
async def handle_value_error(request, exc):
    return Response.json({"error": str(exc)}, status=400)


@app.exception_handler(PermissionError)
async def handle_permission(request, exc):
    return Response.json({"error": "Forbidden"}, status=403)


# Handlers use MRO — a handler for Exception catches all unhandled exceptions
@app.exception_handler(Exception)
async def handle_all(request, exc):
    logger.error(f"Unhandled: {exc}")
    return Response.json({"error": "Internal server error"}, status=500)
```

## Shortcuts

```python
from hyperdjango.shortcuts import get_object_or_404, get_list_or_404, redirect, render

# Get or 404
article = await get_object_or_404(Article, id=42)
article = await get_object_or_404(Article, slug="hello-world")

# List or 404 (empty result → 404)
articles = await get_list_or_404(Article, published=True)

# Redirect
return redirect("/articles/")  # 302 temporary
return redirect("/new-url/", permanent=True)  # 301 permanent
return redirect("/api/data/", status=307)  # 307 preserve method

# Render template
return render(request, "articles/list.html", {"articles": articles})
return render(request, "error.html", {"msg": "Not found"}, status=404)
```

## View Decorators

### Authentication

```python
from hyperdjango.auth.decorators import require_auth, require_permission


@app.get("/dashboard")
@require_auth()
async def dashboard(request):
    return render(request, "dashboard.html", {"user": request.user})


@app.post("/admin/users")
@require_permission("add_user")
async def create_user(request): ...


# Custom auth check
@require_auth(lambda r: r.api_key_valid)
async def api_endpoint(request): ...
```

### Caching

```python
from hyperdjango.cache import cached


@app.get("/expensive")
@cached(ttl=300)  # cache for 5 minutes
async def expensive_view(request): ...
```

## Class-Based Views

For common CRUD patterns:

```python
from hyperdjango.views import ListView, DetailView, CreateView, UpdateView, DeleteView


class ArticleList(ListView):
    model = Article
    template_name = "articles/list.html"
    paginate_by = 20


class ArticleDetail(DetailView):
    model = Article
    template_name = "articles/detail.html"


class ArticleCreate(CreateView):
    model = Article
    fields = ["title", "content"]
    success_url = "/articles/"


class ArticleUpdate(UpdateView):
    model = Article
    fields = ["title", "content"]
    success_url = "/articles/"


class ArticleDelete(DeleteView):
    model = Article
    success_url = "/articles/"


# Register routes
app.route("/articles/")(ArticleList.as_view())
app.route("/articles/{id}")(ArticleDetail.as_view())
app.route("/articles/new", methods=["GET", "POST"])(ArticleCreate.as_view())
app.route("/articles/{id}/edit", methods=["GET", "PUT"])(ArticleUpdate.as_view())
app.route("/articles/{id}/delete", methods=["GET", "DELETE"])(ArticleDelete.as_view())
```

### Auth Mixins

```python
from hyperdjango.views import LoginRequiredMixin, PermissionRequiredMixin


class AdminDashboard(LoginRequiredMixin, ListView):
    model = User
    template_name = "admin/users.html"
    login_url = "/login/"


class EditArticle(PermissionRequiredMixin, UpdateView):
    model = Article
    permission_required = "change_article"
```

## Async Views

All HyperDjango views are async by default. The native Zig server is ASGI-native — no sync-to-async overhead.

```python
@app.get("/dashboard")
async def dashboard(request):
    # All database operations are async
    users = await User.objects.filter(active=True).all()
    stats = await db.query_one("SELECT COUNT(*) as c FROM orders")
    return render(
        request,
        "dashboard.html",
        {
            "users": users,
            "order_count": stats["c"],
        },
    )
```

## Dependency Injection

Register services and inject them by type annotation:

```python
class EmailService:
    async def send(self, to, subject, body): ...


app.provide(EmailService, EmailService())


@app.post("/contact")
async def contact(request, email: EmailService):
    data = await request.json()
    await email.send(data["to"], data["subject"], data["body"])
    return {"sent": True}
```

## HTTP Method Decorators

Restrict views to specific HTTP methods. Returns 405 Method Not Allowed with an Allow header.

```python
from hyperdjango.shortcuts import (
    require_http_methods,
    require_GET,
    require_POST,
    require_safe,
)


@app.route("/items/{id}")
@require_http_methods(["GET", "PUT"])
async def item_view(request, id: int):
    if request.method == "GET":
        return await get_item(id)
    else:
        return await update_item(request, id)


@app.get("/read-only")
@require_safe  # GET and HEAD only
async def read_only(request):
    return {"data": "read-only endpoint"}
```

| Decorator                               | Allowed Methods | Use Case                  |
| --------------------------------------- | --------------- | ------------------------- |
| `require_http_methods(["GET", "POST"])` | Custom list     | Multi-method endpoints    |
| `require_GET`                           | GET, HEAD       | Read-only endpoints       |
| `require_POST`                          | POST            | Form submission endpoints |
| `require_safe`                          | GET, HEAD       | Same as require_GET       |

When a disallowed method is used, the response is:

```json
{ "error": "Method not allowed", "allowed": ["GET", "HEAD"] }
```

With status 405 and `Allow: GET, HEAD` header.
