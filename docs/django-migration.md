# Migrating from Django

HyperDjango is Django-inspired with the same mental model. If you know Django, you know 80% of HyperDjango. This guide covers what's the same, what's different, and what's better.

## What's the Same

| Concept     | Django                                | HyperDjango                                   |
| ----------- | ------------------------------------- | --------------------------------------------- |
| Models      | `class Book(models.Model)`            | `class Book(TimestampMixin, Model)`           |
| Fields      | `models.CharField(max_length=100)`    | `title: str = Field(max_length=100)`          |
| ORM queries | `Book.objects.filter(published=True)` | `Book.objects.filter(published=True)`         |
| Admin       | `admin.site.register(Book)`           | `admin.register(Book)`                        |
| Migrations  | `python manage.py migrate`            | `uv run hyper setup --app app:app`            |
| Templates   | `{% for book in books %}`             | `{% for book in books %}` (Jinja2-compatible) |
| Middleware  | `MIDDLEWARE = [...]`                  | `app.use(CORSMiddleware(...))`                |
| Signals     | `post_save.connect(handler)`          | `post_save.connect(handler)`                  |
| Forms       | `class BookForm(forms.Form)`          | `class BookForm(Form)`                        |

## What's Different

### Models

```python
# Django
class Book(models.Model):
    title = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["title"])]


# HyperDjango — type annotations instead of field classes
class Book(TimestampMixin, Model):  # TimestampMixin adds created_at/updated_at
    title: str = Field(max_length=200)
    price: Decimal = Field(default=Decimal("0.00"))
    published: bool = Field(default=False)

    class Meta:
        indexes = [Index(fields=("title",))]
```

Key differences:

- **Type annotations** instead of `models.CharField` — the field type comes from the Python type
- **TimestampMixin** provides `created_at`/`updated_at` on every model
- **No `auto_now_add`** — timestamps are managed by `save()`, not the DB
- **`Field()`** for metadata (max_length, default, primary_key, foreign_key)

### Views/Routes

```python
# Django
# urls.py
urlpatterns = [path("books/", views.book_list)]


# views.py
def book_list(request):
    books = Book.objects.filter(published=True)
    return JsonResponse({"books": list(books.values())})


# HyperDjango — decorator-based, async by default
@app.get("/books/")
async def book_list(request):
    books = await Book.objects.filter(published=True).all()
    return Response.json({"books": [b.to_dict() for b in books]})
```

Key differences:

- **Async by default** — all handlers are `async def`
- **Decorator routing** — `@app.get("/path/")` instead of `urls.py`
- **Path params** — `@app.get("/books/{id:int}")` with type coercion
- **`await`** on all DB operations

### Auth

```python
# Django
from django.contrib.auth.decorators import login_required


@login_required
def my_view(request): ...


# HyperDjango — guard system with RBAC
from hyperdjango import guard, Require


@app.get("/admin/dashboard")
@guard(Require.authenticated(), Require.role("admin"))
async def admin_dashboard(request): ...
```

Key differences:

- **RBAC groups** instead of `is_staff` booleans — `Require.role("admin")`
- **SessionUser** with `frozenset[str]` groups/permissions — O(1) membership checks
- **`build_session_data()`** at login populates session with RBAC groups
- **Guard composition** — `Require.any_of(Require.role("admin"), Require.api_key())`

### Admin

```python
# Django
class BookAdmin(admin.ModelAdmin):
    list_display = ["title", "price", "published"]
    search_fields = ["title"]


admin.site.register(Book, BookAdmin)

# HyperDjango — single call, same options
admin.register(
    Book,
    list_display=["title", "price", "published"],
    search_fields=["title"],
    list_filter=["published"],
)
```

The admin is built-in with no separate Django dependency. It auto-generates CRUD, search, filters, FK dropdowns, inline editing, and RBAC management tools.

### REST API

```python
# Django REST Framework
class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = "__all__"


class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.all()
    serializer_class = BookSerializer


# HyperDjango — same pattern, built-in
class BookSerializer(ModelSerializer):
    class Meta:
        model = Book


class BookViewSet(ModelViewSet):
    model = Book
    serializer_class = BookSerializer


router = APIRouter(prefix="/api/v1")
router.register("books", BookViewSet)
router.mount(app)
```

## What's Better

| Feature         | Django                   | HyperDjango                                     |
| --------------- | ------------------------ | ----------------------------------------------- |
| **Database**    | psycopg3 (Python)        | pg.zig (native, 2-42x faster)                   |
| **HTTP Server** | uvicorn/gunicorn         | Native Zig (2.1x faster)                        |
| **Templates**   | Django templates         | Zig compiled (234x faster compile, 1.7x render) |
| **JSON**        | stdlib json              | SIMD Zig (6-10x faster)                         |
| **Validation**  | Python                   | SIMD Zig (13M models/sec)                       |
| **Threading**   | GIL-limited              | Free-threaded Python 3.14t                      |
| **Admin**       | Separate app             | Built-in, zero config                           |
| **RBAC**        | 3rd-party Django package | Built-in hierarchical RBAC                      |
| **Telemetry**   | 3rd-party Django package | Built-in Prometheus + spans                     |
| **WebSocket**   | channels (3rd party)     | Built-in native Zig RFC 6455                    |

## Migration Checklist

1. Replace `models.Model` with `TimestampMixin, Model`
2. Replace field classes with type annotations + `Field()`
3. Replace `urls.py` with `@app.get`/`@app.post` decorators
4. Add `await` to all DB operations
5. Replace `@login_required` with `@guard(Require.authenticated())`
6. Replace `is_staff` checks with `Require.role("staff")`
7. Replace `admin.site.register` with `admin.register`
8. Run `uv run hyper setup --app app:app` to create tables
