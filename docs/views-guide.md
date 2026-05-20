# Views & Routing Guide

Comprehensive guide to handling HTTP requests in HyperDjango: function views, class-based views, URL routing, shortcuts, and decorators.

---

## Table of Contents

- [Function-Based Views](#function-based-views)
- [URL Routing](#url-routing)
- [Request and Response Objects](#request-and-response-objects)
- [Class-Based Views](#class-based-views)
- [Shortcuts](#shortcuts)
- [View Decorators](#view-decorators)
- [URL Namespaces and Includes](#url-namespaces-and-includes)
- [WebSocket Views](#websocket-views)
- [Middleware](#middleware)
- [Error Handling](#error-handling)
- [Migration Notes for Django Users](#migration-notes-for-django-users)

---

## Function-Based Views

The simplest way to handle requests. Decorate an async function with a route method on the app.

```python
from hyperdjango import HyperApp
from hyperdjango.response import Response

app = HyperApp("myapp", database="postgres://localhost/mydb")


@app.get("/api/products")
async def product_list(request):
    """List products. Returning a dict auto-serializes to JSON."""
    products = await Product.objects.filter(is_active=True).order_by("name").all()
    return {
        "products": [{"id": p.id, "name": p.name, "price": p.price} for p in products]
    }


@app.get("/api/products/{id:int}")
async def product_detail(request, id: int):
    """Path parameters are passed as typed keyword arguments."""
    product = await get_object_or_404(Product, id=id)
    return {"product": {"id": product.id, "name": product.name, "price": product.price}}


@app.post("/api/products")
async def product_create(request):
    """Parse JSON body from the request."""
    data = request.json
    product = await Product.objects.create(
        name=data["name"],
        price=data["price"],
        sku=data["sku"],
    )
    return Response.json({"id": product.id}, status=201)


@app.put("/api/products/{id:int}")
async def product_update(request, id: int):
    product = await get_object_or_404(Product, id=id)
    data = request.json
    await Product.objects.filter(id=id).update(**data)
    return {"updated": True}


@app.delete("/api/products/{id:int}")
async def product_delete(request, id: int):
    deleted = await Product.objects.filter(id=id).delete()
    return {"deleted": deleted}
```

### Route Methods

| Decorator                            | HTTP Method     |
| ------------------------------------ | --------------- |
| `@app.get(pattern)`                  | GET             |
| `@app.post(pattern)`                 | POST            |
| `@app.put(pattern)`                  | PUT             |
| `@app.patch(pattern)`                | PATCH           |
| `@app.delete(pattern)`               | DELETE          |
| `@app.route(pattern, methods=[...])` | Any combination |

### Named Routes

```python
@app.get("/api/users/{id:int}", name="user-detail")
async def user_detail(request, id: int): ...


# Reverse URL resolution
from hyperdjango.router import reverse

url = reverse("user-detail", id=42)  # "/api/users/42"
```

---

## URL Routing

### Path Parameter Types

```python
# Integer parameter
@app.get("/orders/{id:int}")
async def order_detail(request, id: int): ...


# Slug parameter (letters, digits, hyphens, underscores)
@app.get("/articles/{slug:slug}")
async def article_by_slug(request, slug: str): ...


# UUID parameter
@app.get("/files/{file_id:uuid}")
async def file_download(request, file_id: str): ...


# String parameter (default, matches anything except /)
@app.get("/users/{username:str}")
async def user_profile(request, username: str): ...


# Path parameter (matches everything including /)
@app.get("/docs/{path:path}")
async def serve_docs(request, path: str): ...
```

### Route Registration via app.route()

For views that handle multiple HTTP methods:

```python
@app.route("/api/items/{id:int}", methods=["GET", "PUT", "DELETE"])
async def item_handler(request, id: int):
    if request.method == "GET":
        return await get_item(id)
    elif request.method == "PUT":
        return await update_item(id, request.json)
    elif request.method == "DELETE":
        return await delete_item(id)
```

---

## Request and Response Objects

### Request

```python
@app.post("/api/upload")
async def handle_upload(request):
    # Headers
    content_type = request.headers.get("content-type")
    auth_header = request.headers.get("authorization")

    # Query parameters
    page = request.GET.get("page", "1")
    search = request.GET.get("q")

    # JSON body (parsed lazily)
    data = request.json

    # Form data
    form_data = request.form

    # Raw body
    body_bytes = request.body
    body_text = request.text

    # Client info
    ip = request.client_ip
    is_secure = request.is_secure

    # Method and path
    method = request.method  # "POST"
    path = request.path  # "/api/upload"

    # Cookies
    session_id = request.cookies.get("session_id")
```

### Response

```python
from hyperdjango.response import Response

# JSON response (most common)
return Response.json({"key": "value"})
return Response.json({"error": "Not found"}, status=404)

# HTML response
return Response.html("<h1>Hello</h1>")

# Plain text
return Response.text("OK")

# Redirect
return Response.redirect("/dashboard")
return Response.redirect("/new-url", permanent=True)  # 301

# File download
return Response.file("report.pdf", content_type="application/pdf")
return Response.attachment(content, filename="export.csv")


# Streaming (Server-Sent Events)
async def event_stream():
    while True:
        data = await get_next_event()
        yield f"data: {data}\n\n"


return Response.sse(event_stream())

# Custom headers and cookies
response = Response.json({"ok": True})
response.headers["X-Custom"] = "value"
response.set_cookie("theme", "dark", max_age=86400, httponly=True)
response.delete_cookie("old_cookie")
return response
```

---

## Class-Based Views

For more structured view patterns with built-in CRUD operations.

### ListView

```python
from hyperdjango.views import ListView


class ProductList(ListView):
    model = Product
    per_page = 25
    ordering = "-created_at"
    context_object_name = "products"

    def apply_filters(self, qs, request):
        """Apply request-based filters."""
        category = request.GET.get("category")
        if category:
            qs = qs.filter(category=category)

        min_price = request.GET.get("min_price")
        if min_price:
            qs = qs.filter(price__gte=float(min_price))

        return qs


# Register with the app
app.route("/api/products", methods=["GET"])(ProductList.as_view())
```

### DetailView

```python
from hyperdjango.views import DetailView


class ProductDetail(DetailView):
    model = Product
    pk_url_kwarg = "id"
    context_object_name = "product"


app.route("/api/products/{id:int}", methods=["GET"])(ProductDetail.as_view())
```

### CreateView

```python
from hyperdjango.views import CreateView


class ProductCreate(CreateView):
    model = Product
    fields = ["name", "sku", "price", "stock", "category", "description"]
    success_url = "/api/products"

    def validate(self, data):
        """Custom validation. Return dict of field->errors or empty dict."""
        errors = {}
        if data.get("price", 0) < 0:
            errors["price"] = ["Price must be non-negative"]
        if not data.get("name"):
            errors["name"] = ["Name is required"]
        return errors


app.route("/api/products/new", methods=["GET", "POST"])(ProductCreate.as_view())
```

### UpdateView

```python
from hyperdjango.views import UpdateView


class ProductUpdate(UpdateView):
    model = Product
    fields = ["name", "price", "stock", "description", "is_active"]
    pk_url_kwarg = "id"
    success_url = "/api/products"


app.route("/api/products/{id:int}/edit", methods=["GET", "PUT"])(
    ProductUpdate.as_view()
)
```

### DeleteView

```python
from hyperdjango.views import DeleteView


class ProductDelete(DeleteView):
    model = Product
    pk_url_kwarg = "id"
    success_url = "/api/products"


app.route("/api/products/{id:int}/delete", methods=["DELETE", "POST"])(
    ProductDelete.as_view()
)
```

### Custom CBV

```python
from hyperdjango.views import View


class DashboardView(View):
    async def get(self, request, **kwargs):
        total_products = await Product.objects.count()
        total_orders = await Order.objects.count()
        recent_orders = await (
            Order.objects.select_related("customer")
            .order_by("-created_at")
            .limit(10)
            .all()
        )
        return Response.json(
            {
                "total_products": total_products,
                "total_orders": total_orders,
                "recent_orders": [
                    {"id": o.id, "customer": o.customer.name, "total": o.total}
                    for o in recent_orders
                ],
            }
        )


app.route("/api/dashboard", methods=["GET"])(DashboardView.as_view())
```

---

## Shortcuts

### get_object_or_404

```python
from hyperdjango.shortcuts import get_object_or_404


@app.get("/api/products/{id:int}")
async def product_detail(request, id: int):
    # Raises HTTPException(404) if not found
    product = await get_object_or_404(Product, id=id)
    return {"product": {"id": product.id, "name": product.name}}


# With multiple filter conditions
@app.get("/api/products/{slug:slug}")
async def product_by_slug(request, slug: str):
    product = await get_object_or_404(Product, slug=slug, is_active=True)
    return {"product": {"id": product.id, "name": product.name}}
```

### get_list_or_404

```python
from hyperdjango.shortcuts import get_list_or_404


@app.get("/api/categories/{category}/products")
async def products_by_category(request, category: str):
    # Raises 404 if no products in this category
    products = await get_list_or_404(Product, category=category, is_active=True)
    return {"products": [{"id": p.id, "name": p.name} for p in products]}
```

### redirect

```python
from hyperdjango.shortcuts import redirect


@app.post("/api/products/{id:int}/archive")
async def archive_product(request, id: int):
    await Product.objects.filter(id=id).update(is_active=False)
    return redirect("/api/products")


# Permanent redirect (301)
return redirect("/new-url", permanent=True)
```

### render

```python
from hyperdjango.shortcuts import render


@app.get("/products/{id:int}")
async def product_page(request, id: int):
    product = await get_object_or_404(Product, id=id)
    return render(request, "products/detail.html", {"product": product})
```

---

## View Decorators

### HTTP Method Restrictions

```python
from hyperdjango.shortcuts import require_GET, require_POST


@app.route("/api/status")
@require_GET
async def health_check(request):
    return {"status": "ok"}


@app.route("/api/webhook")
@require_POST
async def webhook(request):
    data = request.json
    await process_webhook(data)
    return {"received": True}
```

### Authentication Decorators

```python
from hyperdjango.auth import require_auth, require_permission, require_staff


@app.get("/api/profile")
@require_auth()
async def my_profile(request):
    """Requires any authenticated user."""
    return {"user": request.user}


@app.get("/api/admin/users")
@require_staff
async def admin_users(request):
    """Requires is_staff=True."""
    users = await User.objects.all()
    return {"users": [{"id": u.id, "name": u.username} for u in users]}


@app.post("/api/products")
@require_permission("add_product")
async def create_product(request):
    """Requires specific permission."""
    ...


@app.delete("/api/products/{id:int}")
@require_permission("delete_product")
async def delete_product(request, id: int): ...
```

### Security Decorators

```python
from hyperdjango.shortcuts import (
    never_cache,
    sensitive_variables,
    vary_on_headers,
    xframe_options_deny,
)


@app.get("/api/account/balance")
@never_cache
@require_auth()
async def account_balance(request):
    """Never cache sensitive financial data."""
    ...


@app.get("/api/embed")
@xframe_options_deny
async def no_iframe(request):
    """Prevent embedding in iframes."""
    ...


@app.get("/api/feed")
@vary_on_headers("Accept-Language", "Cookie")
async def localized_feed(request):
    """Tell caches to vary by these headers."""
    ...
```

---

## URL Namespaces and Includes

Organize routes into modules for large applications:

```python
# routes/products.py
from hyperdjango.router import Router

products_router = Router()


@products_router.get("/")
async def product_list(request): ...


@products_router.get("/{id:int}")
async def product_detail(request, id: int): ...


@products_router.post("/")
async def product_create(request): ...
```

```python
# routes/orders.py
from hyperdjango.router import Router

orders_router = Router()


@orders_router.get("/")
async def order_list(request): ...


@orders_router.get("/{id:int}")
async def order_detail(request, id: int): ...
```

```python
# app.py
from hyperdjango import HyperApp
from routes.products import products_router
from routes.orders import orders_router

app = HyperApp("myapp")

# Mount sub-routers with prefixes and namespaces
app.router.include(products_router, prefix="/api/products", namespace="products")
app.router.include(orders_router, prefix="/api/orders", namespace="orders")

# Namespaced reverse resolution
from hyperdjango.router import reverse

url = reverse("products:product-detail", id=42)  # "/api/products/42"
url = reverse("orders:order-detail", id=7)  # "/api/orders/7"
```

---

## WebSocket Views

```python
@app.websocket("/ws/chat/{room:str}")
async def chat(ws, room: str):
    await ws.accept()
    try:
        while True:
            message = await ws.receive_text()
            # Echo back to sender
            await ws.send_text(f"[{room}] {message}")
    except WebSocketDisconnect:
        pass
```

For pub/sub channels:

```python
from hyperdjango.channels import Channel, ChannelGroup, InMemoryChannelLayer

layer = InMemoryChannelLayer()
chat_group = ChannelGroup("chat", layer)


@app.websocket("/ws/chat")
async def chat_ws(ws):
    await ws.accept()
    channel = Channel("chat", layer)
    await chat_group.subscribe(channel)

    try:
        while True:
            message = await ws.receive_text()
            await chat_group.publish({"text": message, "user": ws.user})
    except WebSocketDisconnect:
        await chat_group.unsubscribe(channel)
```

---

## Middleware

### Application-Level Middleware

```python
from hyperdjango import RateLimitMiddleware
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    CompressionMiddleware,
    LoggingMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)

app.use(LoggingMiddleware())
app.use(TimingMiddleware())
app.use(CompressionMiddleware(min_size=1024))
app.use(SecurityHeadersMiddleware(hsts=True))
app.use(CORSMiddleware(origins=["https://mysite.com"]))
app.use(RateLimitMiddleware(max_requests=100, window=60))
```

### Custom Middleware

```python
@app.middleware
async def request_id_middleware(request, call_next):
    """Add a unique request ID to every response."""
    import uuid

    request_id = str(uuid.uuid4())
    request.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
```

---

## Error Handling

### Custom Exception Handlers

```python
from hyperdjango.app import HTTPException


# Register handlers for specific exception types
@app.exception_handler(HTTPException)
async def handle_http_error(request, exc):
    return Response.json(
        {"error": exc.detail, "status": exc.status_code},
        status=exc.status_code,
    )


@app.exception_handler(ValueError)
async def handle_validation_error(request, exc):
    return Response.json(
        {"error": "Validation failed", "detail": str(exc)},
        status=400,
    )


# Catch-all for unhandled exceptions
@app.exception_handler(Exception)
async def handle_internal_error(request, exc):
    # Log the full traceback
    import traceback

    traceback.print_exc()
    return Response.json(
        {"error": "Internal server error"},
        status=500,
    )
```

### Raising HTTP Errors

```python
from hyperdjango.app import HTTPException


@app.post("/api/products")
async def create_product(request):
    data = request.json
    if not data.get("name"):
        raise HTTPException(400, "Product name is required")
    if not data.get("sku"):
        raise HTTPException(400, "SKU is required")
    ...
```

---

## Migration Notes for Django Users

### Key Differences

| Django                                    | HyperDjango                                |
| ----------------------------------------- | ------------------------------------------ |
| `urls.py` with `urlpatterns` list         | Decorator-based `@app.get("/path")`        |
| `path("users/<int:id>/", view)`           | `@app.get("/users/{id:int}")`              |
| `HttpResponse`, `JsonResponse`            | `Response.json()`, `Response.html()`       |
| `HttpRequest.GET`, `HttpRequest.POST`     | `request.GET`, `request.json`              |
| `reverse("app:name", args=[42])`          | `reverse("namespace:name", id=42)`         |
| Sync views (default)                      | Async views (always)                       |
| `include("app.urls")`                     | `router.include(sub_router, prefix="...")` |
| `@login_required`                         | `@require_auth()`                          |
| `@permission_required("perm")`            | `@require_permission("perm")`              |
| `TemplateView.as_view(template_name=...)` | `render(request, "template.html", ctx)`    |

### What Stayed the Same

- Class-based views with `as_view()` pattern
- `ListView`, `DetailView`, `CreateView`, `UpdateView`, `DeleteView`
- Method dispatch: `get()`, `post()`, `put()`, `delete()`
- Middleware as callables wrapping the request/response cycle
- Named URL patterns with reverse resolution
- `get_object_or_404` shortcut function

---

## Generic Display Views

Generic display views handle the most common read patterns: listing collections and showing individual records.

### ListView In Depth

`ListView` provides paginated, filterable lists out of the box. It queries the model, applies filters from the request, paginates, serializes, and returns JSON.

**Attributes:**

| Attribute             | Type       | Default         | Description                              |
| --------------------- | ---------- | --------------- | ---------------------------------------- |
| `model`               | `Model`    | `None`          | The model class to query                 |
| `queryset`            | `QuerySet` | `None`          | Custom queryset (overrides `model`)      |
| `per_page`            | `int`      | `25`            | Items per page (0 = no pagination)       |
| `ordering`            | `str`      | `None`          | Default ordering (e.g., `"-created_at"`) |
| `template_name`       | `str`      | `None`          | Template path for HTML response          |
| `context_object_name` | `str`      | `"object_list"` | Key name for items in the response       |

**Pagination and Filtering Example:**

```python
from hyperdjango.views import ListView
from hyperdjango.models import Model, Field


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    category: str = Field(default="general")
    author_id: int = Field(foreign_key=User)
    published: bool = Field(default=False)
    published_at: datetime | None = Field(default=None)


class PublishedArticleList(ListView):
    model = Article
    per_page = 20
    ordering = "-published_at"
    context_object_name = "articles"

    def get_queryset(self):
        """Only show published articles."""
        return self.model.objects.filter(published=True).order_by(self.ordering)

    def apply_filters(self, qs, request):
        """Apply category and search filters from query parameters."""
        category = request.GET.get("category")
        if category:
            qs = qs.filter(category=category)

        author = request.GET.get("author_id")
        if author:
            qs = qs.filter(author_id=int(author))

        return qs

    def serialize_item(self, item) -> dict[str, object]:
        """Control exactly which fields appear in the response."""
        return {
            "id": item.id,
            "title": item.title,
            "category": item.category,
            "published_at": str(item.published_at) if item.published_at else None,
        }


app.route("/api/articles", methods=["GET"])(PublishedArticleList.as_view())
```

Requesting `GET /api/articles?page=2&category=tech` returns:

```json
{
    "page": 2,
    "num_pages": 5,
    "count": 94,
    "has_next": true,
    "has_previous": true,
    "articles": [
        {"id": 21, "title": "Building Native Extensions", "category": "tech", "published_at": "2026-03-15T10:00:00+00:00"},
        ...
    ]
}
```

**Template Context (when using HTML responses):**

When `template_name` is set, the context dictionary passed to the template contains:

| Key                                      | Description                                       |
| ---------------------------------------- | ------------------------------------------------- |
| `object_list` (or `context_object_name`) | The list of serialized items for the current page |
| `page`                                   | Current page number                               |
| `num_pages`                              | Total number of pages                             |
| `count`                                  | Total number of items across all pages            |
| `has_next`                               | Whether a next page exists                        |
| `has_previous`                           | Whether a previous page exists                    |

### DetailView In Depth

`DetailView` fetches a single object by primary key (or slug) and returns it. Returns 404 automatically when the object does not exist.

**Attributes:**

| Attribute             | Type    | Default    | Description                                 |
| --------------------- | ------- | ---------- | ------------------------------------------- |
| `model`               | `Model` | `None`     | The model class to query                    |
| `pk_url_kwarg`        | `str`   | `"id"`     | Name of the URL parameter containing the PK |
| `context_object_name` | `str`   | `"object"` | Key name for the object in the response     |

**Slug Lookup and Related Objects:**

```python
from hyperdjango.views import DetailView


class ArticleDetail(DetailView):
    model = Article
    pk_url_kwarg = "id"
    context_object_name = "article"

    async def get_object(self, **kwargs):
        """Override to use slug lookup with select_related."""
        slug = kwargs.get("slug")
        if slug:
            try:
                return await (
                    self.model.objects.select_related("author")
                    .filter(slug=slug, published=True)
                    .get()
                )
            except self.model.DoesNotExist:
                return None
        # Fall back to PK lookup
        return await super().get_object(**kwargs)

    def serialize_object(self, obj) -> dict[str, object]:
        """Include related author data and computed fields."""
        data = {
            "id": obj.id,
            "title": obj.title,
            "body": obj.body,
            "category": obj.category,
            "published_at": str(obj.published_at) if obj.published_at else None,
        }
        if hasattr(obj, "author"):  # select_related loaded the author
            data["author"] = {
                "id": obj.author.id,
                "name": obj.author.username,
            }
        return data


# Register for both PK and slug patterns
app.route("/api/articles/{id:int}", methods=["GET"])(ArticleDetail.as_view())
app.route("/api/articles/{slug:slug}", methods=["GET"])(ArticleDetail.as_view())
```

**404 Handling:**

`get_object()` returns `None` when the record is not found. The base `get()` method converts this to a JSON 404 response:

```json
{ "error": "Not found" }
```

For custom 404 responses, override `get()`:

```python
class ProductDetail(DetailView):
    model = Product
    pk_url_kwarg = "id"

    async def get(self, request, **kwargs):
        obj = await self.get_object(**kwargs)
        if obj is None:
            return Response.json(
                {"error": "Product not found", "id": kwargs.get("id")},
                status=404,
            )
        return Response.json({"product": self.serialize_object(obj)})
```

---

## Generic Editing Views

Editing views handle the three-phase form processing pattern: initial GET (blank or prepopulated form), POST with invalid data (return errors), POST with valid data (save and redirect).

### CreateView In Depth

**Attributes:**

| Attribute     | Type        | Default | Description                               |
| ------------- | ----------- | ------- | ----------------------------------------- |
| `model`       | `Model`     | `None`  | The model class to create                 |
| `fields`      | `list[str]` | `None`  | Allowed field names from request body     |
| `success_url` | `str`       | `None`  | URL to include in response after creation |

**Full Example with Validation and Form Integration:**

```python
from hyperdjango.views import CreateView
from hyperdjango.forms import Form, CharField, DecimalField, IntegerField


class ProductForm(Form):
    name = CharField(min_length=3, max_length=200)
    sku = CharField(min_length=3, max_length=50)
    price = DecimalField()
    stock = IntegerField(min_value=0)
    description = CharField(required=False)


class ProductCreate(CreateView):
    model = Product
    fields = ["name", "sku", "price", "stock", "description"]
    success_url = "/api/products"

    def validate(self, data):
        """Use a Form for structured validation."""
        form = ProductForm(data=data)
        if not form.is_valid():
            return form.errors
        return {}

    def serialize_object(self, obj) -> dict[str, object]:
        return {
            "id": obj.id,
            "name": obj.name,
            "sku": obj.sku,
            "price": float(obj.price),
        }


app.route("/api/products/new", methods=["GET", "POST"])(ProductCreate.as_view())
```

- `GET /api/products/new` returns `{"fields": ["name", "sku", "price", "stock", "description"]}`.
- `POST /api/products/new` with invalid data returns `{"errors": {"name": ["Minimum length is 3"]}}` with status 400.
- `POST /api/products/new` with valid data returns `{"created": {...}, "redirect": "/api/products"}` with status 201.

### UpdateView In Depth

**Attributes:**

| Attribute      | Type        | Default | Description                             |
| -------------- | ----------- | ------- | --------------------------------------- |
| `model`        | `Model`     | `None`  | The model class to update               |
| `fields`       | `list[str]` | `None`  | Allowed field names                     |
| `pk_url_kwarg` | `str`       | `"id"`  | URL parameter name for the PK           |
| `success_url`  | `str`       | `None`  | URL to include in response after update |

UpdateView supports both PUT (full replacement) and PATCH (partial update). Both methods filter the request body to only the allowed fields.

```python
from hyperdjango.views import UpdateView


class ProductUpdate(UpdateView):
    model = Product
    fields = ["name", "price", "stock", "description", "is_active"]
    pk_url_kwarg = "id"
    success_url = "/api/products"

    def validate(self, data):
        errors = {}
        if "price" in data and data["price"] < 0:
            errors["price"] = "Price must be non-negative"
        if "stock" in data and data["stock"] < 0:
            errors["stock"] = "Stock must be non-negative"
        return errors


app.route("/api/products/{id:int}/edit", methods=["GET", "PUT", "PATCH"])(
    ProductUpdate.as_view()
)
```

- `GET /api/products/42/edit` returns the current object state and list of editable fields.
- `PUT /api/products/42/edit` replaces all specified fields.
- `PATCH /api/products/42/edit` updates only the fields present in the request body.

### DeleteView In Depth

**Attributes:**

| Attribute      | Type    | Default | Description                               |
| -------------- | ------- | ------- | ----------------------------------------- |
| `model`        | `Model` | `None`  | The model class to delete from            |
| `pk_url_kwarg` | `str`   | `"id"`  | URL parameter name for the PK             |
| `success_url`  | `str`   | `None`  | URL to include in response after deletion |

```python
from hyperdjango.views import DeleteView


class ProductDelete(DeleteView):
    model = Product
    pk_url_kwarg = "id"
    success_url = "/api/products"


app.route("/api/products/{id:int}/delete", methods=["GET", "DELETE", "POST"])(
    ProductDelete.as_view()
)
```

- `GET /api/products/42/delete` returns the object data and a confirmation prompt: `{"object": {...}, "confirm": "DELETE to confirm"}`.
- `DELETE /api/products/42/delete` deletes the record and returns `{"deleted": 42, "redirect": "/api/products"}`.
- `POST` also triggers deletion (useful for HTML forms that cannot send DELETE).

### Combining with Auth Mixins

All generic views support authentication by wrapping `as_view()`:

```python
from hyperdjango.auth import require_auth, require_permission


# Protect with authentication
app.route("/api/products/new", methods=["GET", "POST"])(
    require_auth()(ProductCreate.as_view())
)

# Protect with specific permission
app.route("/api/products/{id:int}/delete", methods=["DELETE", "POST"])(
    require_permission("delete_product")(ProductDelete.as_view())
)
```

---

## View Decorators Reference

Complete reference of all view decorators available in HyperDjango.

### HTTP Method Restriction Decorators

Import from `hyperdjango.shortcuts`:

| Decorator                                | Allowed Methods | Description                                      |
| ---------------------------------------- | --------------- | ------------------------------------------------ |
| `@require_GET`                           | GET, HEAD       | Restrict to safe read-only requests              |
| `@require_POST`                          | POST            | Restrict to POST submissions only                |
| `@require_safe`                          | GET, HEAD       | Same as `require_GET` (Django naming convention) |
| `@require_http_methods(["GET", "POST"])` | Custom list     | Restrict to any combination of methods           |

All return `405 Method Not Allowed` with an `Allow` header when the request method does not match.

```python
from hyperdjango.shortcuts import (
    require_http_methods,
    require_GET,
    require_POST,
    require_safe,
)


@app.route("/api/reports")
@require_safe
async def reports_list(request):
    """Only GET and HEAD allowed."""
    reports = await Report.objects.order_by("-created_at").limit(50).all()
    return {"reports": [{"id": r.id, "name": r.name} for r in reports]}


@app.route("/api/payments/process")
@require_POST
async def process_payment(request):
    """Only POST allowed -- this modifies state."""
    data = request.json
    result = await PaymentService.charge(data["amount"], data["card_token"])
    return Response.json({"payment_id": result.id}, status=201)


@app.route("/api/orders/{id:int}", methods=["GET", "PUT", "DELETE"])
@require_http_methods(["GET", "PUT", "DELETE"])
async def order_detail(request, id: int):
    """Explicit method whitelist."""
    ...
```

### Authentication and Permission Decorators

Import from `hyperdjango.auth`:

| Decorator                               | Description                                                    |
| --------------------------------------- | -------------------------------------------------------------- |
| `@require_auth()`                       | Requires any authenticated user (returns 401 if not logged in) |
| `@require_auth(check_fn)`               | Requires authenticated user passing a custom check function    |
| `@require_staff`                        | Requires `user.is_staff == True` (returns 403 if not staff)    |
| `@require_permission("codename")`       | Requires the user to have a specific permission                |
| `@require_permission("perm1", "perm2")` | Requires ALL listed permissions                                |
| `@require_api_key`                      | Requires a valid API key in the configured header              |
| `@require_oauth2()`                     | Requires an active OAuth2 session                              |

### Security and Caching Decorators

Import from `hyperdjango.shortcuts`:

| Decorator                                       | Headers Set                                                                                                | Description                                               |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `@never_cache`                                  | `Cache-Control: max-age=0, no-cache, no-store, must-revalidate, private`, `Expires: 0`, `Pragma: no-cache` | Prevent all caching of the response                       |
| `@vary_on_headers("Accept-Language", "Cookie")` | `Vary: Accept-Language, Cookie`                                                                            | Tell caches to vary by specific request headers           |
| `@vary_on_cookie`                               | `Vary: Cookie`                                                                                             | Shortcut for `vary_on_headers("Cookie")`                  |
| `@xframe_options_deny`                          | `X-Frame-Options: DENY`                                                                                    | Prevent the page from being embedded in any iframe        |
| `@xframe_options_sameorigin`                    | `X-Frame-Options: SAMEORIGIN`                                                                              | Allow framing only from the same origin                   |
| `@xframe_options_exempt`                        | Removes `X-Frame-Options`                                                                                  | Allow framing from any origin (use with caution)          |
| `@sensitive_variables("password", "token")`     | N/A                                                                                                        | Exclude named variables from error reports and tracebacks |
| `@sensitive_post_parameters("password")`        | N/A                                                                                                        | Mask named POST parameters in error reports               |

**Combining Multiple Decorators:**

```python
from hyperdjango.auth import require_auth
from hyperdjango.shortcuts import never_cache, sensitive_variables, vary_on_headers


@app.get("/api/account/settings")
@require_auth()
@never_cache
@vary_on_headers("Cookie")
async def account_settings(request):
    """Authenticated, never cached, varies by session cookie."""
    return {"email": request.user.email, "theme": request.user.theme}


@app.post("/api/account/change-password")
@require_auth()
@never_cache
@sensitive_variables("old_password", "new_password")
async def change_password(request):
    """Passwords excluded from error reports if this view throws."""
    data = request.json
    old_password = data["old_password"]
    new_password = data["new_password"]
    ...
```

Decorators are applied bottom-up (innermost first), so `@require_auth()` runs before `@never_cache` in the example above. Place authentication decorators closest to the function.
