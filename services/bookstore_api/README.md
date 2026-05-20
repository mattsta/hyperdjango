# Bookstore API

Full-featured REST API showcasing ModelViewSet, serializers, pagination, filtering, caching, and nested routers.

## Quick Start

```bash
uv run hyper setup --app services.bookstore_api.app:app --seed services.bookstore_api.seed:run
uv run hyper run --app services.bookstore_api.app:app --port 8900
```

## Features

- Full CRUD via ModelViewSet (list, create, retrieve, update, partial_update, destroy)
- Separate read/write/list serializers with SerializerMethodField for computed output
- PageNumberPagination for books, CursorPagination for reviews
- FieldFilter, FullTextSearchFilter (PostgreSQL tsvector), OrderingFilter
- ETag + Cache-Control conditional caching on detail views
- Custom @action endpoints (publish, feature, stats, featured list)
- Nested router: `/api/v1/authors/{author_id}/books/`
- Session auth + API key auth with IsAuthenticatedOrReadOnly permissions
- OpenAPI 3.1 spec + Swagger UI at `/docs`
- CORS, rate limiting, security headers, timing middleware

## Platform Features Demonstrated

- **ModelViewSet** with perform_create/perform_update hooks
- **ModelSerializer** with nested serializers and computed fields
- **CursorPagination** with HMAC-signed opaque cursors
- **FieldFilter / FullTextSearchFilter / OrderingFilter** filter backends
- **CacheableMixin** for ETag + 304 Not Modified responses
- **APIRouter + NestedRouter** for REST resource routing
- **APIKeyAuth** for admin endpoints
- **mount_docs()** for automatic OpenAPI generation

## API Endpoints

```
GET  /docs                              Swagger UI
GET  /api/v1/books/                     List books (paginated, filterable)
POST /api/v1/books/                     Create book (auth required)
GET  /api/v1/books/{id}                 Book detail with author, reviews, rating
POST /api/v1/books/{id}/publish         Publish a book (staff only)
POST /api/v1/books/{id}/feature         Toggle featured status (staff only)
GET  /api/v1/books/featured             List featured books (cursor-paginated)
GET  /api/v1/books/stats                Collection statistics
GET  /api/v1/authors/                   List authors (searchable)
GET  /api/v1/authors/{id}/books/        Books by author (nested)
GET  /api/v1/categories/                List categories
GET  /api/v1/reviews/                   List reviews (cursor-paginated)
POST /api/v1/reviews/                   Create review (auth required)
POST /auth/login                        Session login
POST /auth/register                     Register new user
GET  /api/admin/stats                   Admin stats (API key required)
GET  /admin/                            HyperAdmin dashboard
GET  /admin/book/                       Book list (search, filter, bulk actions)
GET  /admin/author/                     Author list
GET  /admin/review/                     Review list
```

## HyperAdmin Panel

The bookstore includes a full admin panel at `/admin/` with:

- All 5 models registered (Author, Category, Book, Review, User)
- Custom bulk actions: Publish selected, Mark featured, Remove featured
- Search: Books by title/ISBN, Authors by name
- Filters: Books by published/featured/category
- Fieldsets: Organized form layout with collapsible timestamp section

## Error Handling

Global exception handlers provide consistent JSON error responses:

- `ValidationError` → 400 with field-level details
- Unhandled exceptions → 500 with generic message (no stack trace leak)

## Project Structure

```
bookstore_api/
    app.py          Models, serializers, ViewSets, router, admin, auth endpoints
    seed.py         Sample authors, categories, books, reviews, users
    templates/      (reserved for future HTML views)
```
