# Class-Based Views

Reusable view patterns for common CRUD operations.

## Overview

HyperDjango provides a `View` base class with HTTP method dispatch, 5 generic CBVs for CRUD, and 2 auth mixins:

| View         | Purpose                                  | HTTP Methods                      |
| ------------ | ---------------------------------------- | --------------------------------- |
| `View`       | Base class — dispatch to method handlers | Any                               |
| `ListView`   | Display a paginated list of objects      | GET                               |
| `DetailView` | Display a single object                  | GET                               |
| `CreateView` | Create a new object                      | GET (schema), POST (submit)       |
| `UpdateView` | Update an existing object                | GET (current), PUT/PATCH (submit) |
| `DeleteView` | Delete an object                         | GET (confirm), DELETE (submit)    |

| Mixin                     | Purpose                        |
| ------------------------- | ------------------------------ |
| `LoginRequiredMixin`      | Require authenticated user     |
| `PermissionRequiredMixin` | Require specific permission(s) |

All CBVs are async-native and return JSON by default. When a template engine is configured, they can render HTML templates instead.

```python
from hyperdjango.views import (
    View,
    ListView,
    DetailView,
    CreateView,
    UpdateView,
    DeleteView,
    LoginRequiredMixin,
    PermissionRequiredMixin,
)
```

## View Base Class

All CBVs inherit from `View`. It provides HTTP method dispatch, `as_view()` class method, and HEAD support.

### as_view()

`as_view()` returns an async handler function suitable for route registration. Each request creates a fresh view instance, so instance state is per-request.

```python
from hyperdjango.views import View
from hyperdjango import Response


class CustomView(View):
    async def get(self, request, **kwargs):
        return Response.json({"method": "GET"})

    async def post(self, request, **kwargs):
        data = await request.json()
        return Response.json({"received": data}, status=201)


# Register with the router
app.route("/custom", methods=["GET", "POST"])(CustomView.as_view())

# Pass init kwargs
app.route("/page")(CustomView.as_view(page_size=50))
```

The `**initkwargs` passed to `as_view()` are set as attributes on the view instance before dispatch.

### dispatch()

`dispatch()` routes to the appropriate method handler based on `request.method`. It checks `http_method_names` and calls the corresponding `get()`, `post()`, `put()`, `patch()`, or `delete()` method. If the method is not allowed, it returns 405 with an `Allow` header listing valid methods.

```python
class CustomView(View):
    http_method_names = ["get", "post"]  # Only allow GET and POST

    async def get(self, request, **kwargs):
        return Response.json({"ok": True})
```

### View Lifecycle

1. `as_view(**initkwargs)` returns an async callable
2. On each request, the callable instantiates a fresh view with `cls(**initkwargs)`
3. `self.request` and `self.kwargs` are set on the instance
4. `dispatch()` checks `request.method` against `http_method_names`
5. The matching method handler (`get`, `post`, etc.) is called
6. The handler returns a `Response`

### Method Handlers

Define async methods matching HTTP verbs:

```python
class ResourceView(View):
    async def get(self, request, **kwargs):
        """Handle GET requests."""
        return Response.json({"items": []})

    async def post(self, request, **kwargs):
        """Handle POST requests."""
        data = await request.json()
        return Response.json({"created": data}, status=201)

    async def put(self, request, **kwargs):
        """Handle PUT requests (full update)."""
        data = await request.json()
        return Response.json({"updated": data})

    async def patch(self, request, **kwargs):
        """Handle PATCH requests (partial update)."""
        data = await request.json()
        return Response.json({"patched": data})

    async def delete(self, request, **kwargs):
        """Handle DELETE requests."""
        return Response.json({"deleted": True}, status=204)
```

HEAD requests are handled automatically -- they call `get()` and return the response (the server strips the body).

### http_method_not_allowed()

When a request uses a method not in `http_method_names` or not implemented on the view, the view returns:

```json
{ "error": "Method not allowed", "allowed": ["GET", "POST"] }
```

With status 405 and an `Allow: GET, POST` header.

## ListView

Display a paginated list of model instances.

### Attributes

| Attribute             | Type          | Default         | Description                              |
| --------------------- | ------------- | --------------- | ---------------------------------------- |
| `model`               | Model class   | `None`          | The model to query                       |
| `queryset`            | QuerySet      | `None`          | Override default queryset                |
| `per_page`            | `int`         | `25`            | Items per page (0 = no pagination)       |
| `ordering`            | `str \| None` | `None`          | Default ordering (e.g., `"-created_at"`) |
| `template_name`       | `str \| None` | `None`          | Template to render                       |
| `context_object_name` | `str`         | `"object_list"` | Key name for the list in context         |

### Basic Usage

```python
from hyperdjango.views import ListView


class ArticleList(ListView):
    model = Article
    template_name = "articles/list.html"
    context_object_name = "articles"
    per_page = 20
    ordering = "-pub_date"


app.route("/articles/")(ArticleList.as_view())
```

### get_queryset() Override

Override to customize the query, add filters, or scope by user:

```python
class MyArticles(ListView):
    model = Article
    per_page = 10

    def get_queryset(self):
        """Only show published articles by the current user."""
        return self.model.objects.filter(
            published=True,
            author_id=self.request.user.id,
        ).order_by("-pub_date")
```

### apply_filters()

Override to apply request-based filters to the queryset:

```python
class ArticleList(ListView):
    model = Article

    def apply_filters(self, qs, request):
        category = request.query("category")
        if category:
            qs = qs.filter(category=category)
        search = request.query("q")
        if search:
            qs = qs.filter(title__icontains=search)
        return qs
```

### Pagination

When `per_page > 0`, the response includes pagination metadata:

```json
{
    "articles": [{"id": 1, "title": "..."}, ...],
    "page": 1,
    "num_pages": 5,
    "count": 100,
    "has_next": true,
    "has_previous": false
}
```

The page number is read from the `page` query parameter: `GET /articles/?page=2`

Template context receives:

- `articles` (or `object_list`) -- list of serialized model instances
- `paginator` -- Paginator object (if `per_page` set)
- `page_obj` -- current Page object with `has_next`, `has_previous`, `number`

### get_context_data()

Override to add extra context:

```python
class ArticleList(ListView):
    model = Article

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["featured"] = True
        return context
```

## DetailView

Display a single model instance by primary key or slug.

### Attributes

| Attribute             | Type        | Default    | Description                            |
| --------------------- | ----------- | ---------- | -------------------------------------- |
| `model`               | Model class | `None`     | The model to query                     |
| `pk_url_kwarg`        | `str`       | `"id"`     | URL parameter name for the primary key |
| `context_object_name` | `str`       | `"object"` | Key name for the object in context     |

### Basic Usage

```python
from hyperdjango.views import DetailView


class ArticleDetail(DetailView):
    model = Article
    template_name = "articles/detail.html"
    context_object_name = "article"


# pk parameter
app.route("/articles/{id:int}/")(ArticleDetail.as_view())
```

Returns 404 JSON if the object is not found:

```json
{ "error": "Not found" }
```

### pk vs slug URL Parameters

Use `pk_url_kwarg` to match your URL pattern parameter name:

```python
# Default: matches {id} in URL
class ArticleByID(DetailView):
    model = Article
    pk_url_kwarg = "id"  # default


app.route("/articles/{id:int}/")(ArticleByID.as_view())


# Slug-based lookup
class ArticleBySlug(DetailView):
    model = Article
    pk_url_kwarg = "slug"


app.route("/articles/{slug:str}/")(ArticleBySlug.as_view())
```

### get_object() Override

Override for custom lookup logic, multi-field lookups, or permission checks:

```python
class ArticleDetail(DetailView):
    model = Article

    async def get_object(self, **kwargs):
        """Only show published articles, or any article to staff."""
        obj = await super().get_object(**kwargs)
        if obj is None:
            return None
        if not obj.published and not self.request.user.is_staff:
            return None  # Treated as 404
        return obj
```

## CreateView

Handle object creation with field validation.

### Attributes

| Attribute     | Type                | Default | Description                          |
| ------------- | ------------------- | ------- | ------------------------------------ |
| `model`       | Model class         | `None`  | The model to create                  |
| `fields`      | `list[str] \| None` | `None`  | Allowed fields (None = all writable) |
| `success_url` | `str \| None`       | `None`  | Redirect URL after creation          |

### Basic Usage

```python
from hyperdjango.views import CreateView


class ArticleCreate(CreateView):
    model = Article
    fields = ["title", "content", "published"]
    template_name = "articles/form.html"
    success_url = "/articles/"


app.route("/articles/new", methods=["GET", "POST"])(ArticleCreate.as_view())
```

- **GET**: Returns the list of allowed fields as JSON: `{"fields": ["title", "content", "published"]}`
- **POST**: Validates, creates the object, returns 201 with the created data

### Response Format

Successful creation (201):

```json
{
  "created": {
    "id": 1,
    "title": "New Article",
    "content": "...",
    "published": true
  },
  "redirect": "/articles/"
}
```

Validation error (400):

```json
{ "errors": { "title": "Title is required" } }
```

### validate() Override

Override to add custom validation logic:

```python
class ArticleCreate(CreateView):
    model = Article
    fields = ["title", "content"]

    def validate(self, data):
        errors = {}
        if len(data.get("title", "")) < 5:
            errors["title"] = "Title must be at least 5 characters"
        if not data.get("content"):
            errors["content"] = "Content is required"
        return errors
```

### form_valid Pattern

Use `post()` override for custom post-creation behavior (e.g., setting the author):

```python
class ArticleCreate(CreateView):
    model = Article
    fields = ["title", "content"]

    async def post(self, request, **kwargs):
        data = request.json or {}
        data["author_id"] = request.user.id  # Set author from session
        # Call parent logic or create manually
        obj = await self.model.objects.create(
            title=data["title"],
            content=data["content"],
            author_id=data["author_id"],
        )
        return Response.json({"created": self.serialize_object(obj)}, status=201)
```

## UpdateView

Handle object updates via PUT (full) or PATCH (partial).

### Attributes

| Attribute      | Type                | Default | Description                            |
| -------------- | ------------------- | ------- | -------------------------------------- |
| `model`        | Model class         | `None`  | The model to update                    |
| `fields`       | `list[str] \| None` | `None`  | Allowed fields (None = all writable)   |
| `pk_url_kwarg` | `str`               | `"id"`  | URL parameter name for the primary key |
| `success_url`  | `str \| None`       | `None`  | Redirect URL after update              |

### Basic Usage

```python
from hyperdjango.views import UpdateView


class ArticleUpdate(UpdateView):
    model = Article
    fields = ["title", "content", "published"]
    template_name = "articles/form.html"
    success_url = "/articles/"


app.route(
    "/articles/{id:int}/edit",
    methods=["GET", "PUT", "PATCH"],
)(ArticleUpdate.as_view())
```

- **GET**: Returns current object data and allowed fields
- **PUT**: Full update (all fields required)
- **PATCH**: Partial update (only provided fields updated)

### GET Response

```json
{
  "object": { "id": 1, "title": "Old Title", "content": "..." },
  "fields": ["title", "content", "published"]
}
```

### PUT/PATCH Response

```json
{
  "updated": { "id": 1, "title": "New Title", "content": "..." },
  "redirect": "/articles/"
}
```

### validate() Override

Same pattern as CreateView -- return a dict of field-to-error mappings:

```python
class ArticleUpdate(UpdateView):
    model = Article
    fields = ["title", "content"]

    def validate(self, data):
        errors = {}
        if "title" in data and len(data["title"]) < 5:
            errors["title"] = "Title must be at least 5 characters"
        return errors
```

## DeleteView

Handle object deletion with a confirmation step.

### Attributes

| Attribute      | Type          | Default | Description                            |
| -------------- | ------------- | ------- | -------------------------------------- |
| `model`        | Model class   | `None`  | The model to delete from               |
| `pk_url_kwarg` | `str`         | `"id"`  | URL parameter name for the primary key |
| `success_url`  | `str \| None` | `None`  | Redirect URL after deletion            |

### Basic Usage

```python
from hyperdjango.views import DeleteView


class ArticleDelete(DeleteView):
    model = Article
    template_name = "articles/confirm_delete.html"
    success_url = "/articles/"


app.route(
    "/articles/{id:int}/delete",
    methods=["GET", "DELETE"],
)(ArticleDelete.as_view())
```

- **GET**: Returns confirmation data: `{"object": {...}, "confirm": "DELETE to confirm"}`
- **DELETE**: Deletes the object, returns: `{"deleted": 42, "redirect": "/articles/"}`

### Confirmation Template Pattern

For HTML responses, the GET renders a confirmation page with a form that submits via DELETE:

```html
<h2>Delete {{ object.title }}?</h2>
<p>This action cannot be undone.</p>
<form method="POST" action="{{ request.path }}">
  <input type="hidden" name="_method" value="DELETE" />
  <button type="submit">Confirm Delete</button>
  <a href="/articles/">Cancel</a>
</form>
```

## Auth Mixins

### LoginRequiredMixin

Requires an authenticated user. Place before the View class in the MRO.

| Attribute   | Type  | Default    | Description                             |
| ----------- | ----- | ---------- | --------------------------------------- |
| `login_url` | `str` | `"/login"` | Where to redirect unauthenticated users |

```python
from hyperdjango.views import LoginRequiredMixin, ListView


class DashboardView(LoginRequiredMixin, ListView):
    model = Order
    template_name = "dashboard.html"
    login_url = "/auth/login/"
```

When the user is not authenticated, returns 401:

```json
{ "error": "Authentication required" }
```

The mixin checks `request.user` set by authentication middleware. It handles both object-style users (checks `is_authenticated`) and dict-style session users.

### PermissionRequiredMixin

Requires the user to have specific permission(s). Returns 403 if the user lacks any required permission.

| Attribute             | Type               | Default | Description            |
| --------------------- | ------------------ | ------- | ---------------------- |
| `permission_required` | `str \| list[str]` | `""`    | Required permission(s) |

```python
from hyperdjango.views import PermissionRequiredMixin, UpdateView


class EditArticle(PermissionRequiredMixin, UpdateView):
    model = Article
    fields = ["title", "content"]
    permission_required = "change_article"
```

Multiple permissions (all required):

```python
class AdminEditArticle(PermissionRequiredMixin, UpdateView):
    model = Article
    permission_required = ["change_article", "publish_article"]
```

Returns 401 if not authenticated, 403 if authenticated but lacking permissions:

```json
{ "error": "Permission denied" }
```

### Combining Mixins

Stack mixins before the View class. They execute in MRO order (left to right):

```python
class ProtectedEditView(LoginRequiredMixin, PermissionRequiredMixin, UpdateView):
    model = Article
    fields = ["title", "content"]
    login_url = "/login/"
    permission_required = "change_article"
    success_url = "/articles/"
```

Order matters: `LoginRequiredMixin` checks authentication first, then `PermissionRequiredMixin` checks permissions.

## JSON API Pattern

CBVs return JSON by default when no template is specified. This makes them ideal for building REST APIs:

```python
class UserAPI(View):
    async def get(self, request, pk=None):
        if pk:
            user = await User.objects.get(id=pk)
            return Response.json({"id": user.id, "name": user.name})
        users = await User.objects.all()
        return Response.json([{"id": u.id, "name": u.name} for u in users])

    async def post(self, request):
        data = await request.json()
        user = await User.objects.create(**data)
        return Response.json({"id": user.id}, status=201)

    async def delete(self, request, pk=None):
        user = await User.objects.get(id=pk)
        await user.delete()
        return Response.json({"deleted": pk}, status=200)


# Register list and detail on different patterns
app.route("/api/users", methods=["GET", "POST"])(UserAPI.as_view())
app.route("/api/users/{pk:int}", methods=["GET", "PUT", "DELETE"])(UserAPI.as_view())
```

### CRUD Resource Pattern

A complete CRUD resource using separate CBVs:

```python
class ProductList(ListView):
    model = Product
    per_page = 50
    ordering = "-created_at"


class ProductDetail(DetailView):
    model = Product


class ProductCreate(LoginRequiredMixin, CreateView):
    model = Product
    fields = ["name", "price", "description"]
    success_url = "/products/"


class ProductUpdate(LoginRequiredMixin, PermissionRequiredMixin, UpdateView):
    model = Product
    fields = ["name", "price", "description"]
    permission_required = "change_product"
    success_url = "/products/"


class ProductDelete(LoginRequiredMixin, PermissionRequiredMixin, DeleteView):
    model = Product
    permission_required = "delete_product"
    success_url = "/products/"


# Register all routes
app.get("/products/")(ProductList.as_view())
app.get("/products/{id:int}/")(ProductDetail.as_view())
app.route("/products/new", methods=["GET", "POST"])(ProductCreate.as_view())
app.route("/products/{id:int}/edit", methods=["GET", "PUT", "PATCH"])(
    ProductUpdate.as_view()
)
app.route("/products/{id:int}/delete", methods=["GET", "DELETE"])(
    ProductDelete.as_view()
)
```

## When to Use CBVs vs Function Views

**Use function views when:**

- Simple, one-off endpoints
- Complex logic that does not fit CRUD patterns
- API endpoints with custom serialization
- WebSocket handlers
- Endpoints that mix multiple models in one response

```python
@app.get("/dashboard")
async def dashboard(request):
    orders = await Order.objects.filter(user_id=request.user.id).all()
    stats = await get_user_stats(request.user.id)
    return Response.json({"orders": orders, "stats": stats})
```

**Use CBVs when:**

- Standard CRUD operations on a model
- You want consistent patterns across many models
- You need pagination, auth mixins, or template rendering
- You want to share behavior via mixins
- Your team benefits from a predictable view structure

```python
class OrderList(LoginRequiredMixin, ListView):
    model = Order
    per_page = 25
    ordering = "-created_at"

    def get_queryset(self):
        return self.model.objects.filter(user_id=self.request.user.id)
```

**Performance note:** CBVs add minimal overhead. The `as_view()` instantiation cost is negligible compared to database and network I/O. Both function views and CBVs run on the same async dispatch path.
