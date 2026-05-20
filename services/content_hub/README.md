# Content Hub

CMS service showcasing Q objects, OneToOneField, Single-Table Inheritance, and HyperAdmin with custom actions.

## Quick Start

```bash
uv run hyper setup --app services.content_hub.app:app --seed services.content_hub.seed:run
uv run hyper run --app services.content_hub.app:app --port 8300
```

## Features

- Single-Table Inheritance: Article, Video, Link share one `hub_contents` table with a type discriminator
- OneToOneField: UserProfile linked 1:1 to User (UNIQUE FK constraint)
- Q objects: Complex queries with OR, NOT, nested conditions, multi-type filters
- Enum fields: ContentStatus (draft/published/archived), ContentType (article/video/link), Role (reader/editor/admin)
- CursorPagination on all list endpoints
- HyperAdmin panel with custom bulk actions (publish, archive, feature)
- Admin fieldsets, inline profile editing, search, and list filters
- Session auth with CSRF protection
- Advanced search endpoint accepting structured JSON queries

## Platform Features Demonstrated

- **Q objects** for `(title OR body)`, `NOT archived`, `published AND (article OR video)`
- **OneToOneField** with profile upsert and JOIN queries
- **Single-Table Inheritance** with automatic type filtering on STI child models
- **HyperAdmin** with Actions, Fieldsets, InlineConfig, and RBAC model registration
- **CursorPagination** for content listing
- **Enum fields** with validation on input
- **CSRFMiddleware** with configurable exempt paths

## API Endpoints

```
POST /auth/login                        Session login
GET  /auth/me                           Current user with profile (OneToOneField join)
GET  /api/contents                      List content (Q-based filtering, cursor-paginated)
GET  /api/contents/{id}                 Content detail with author profile
POST /api/contents                      Create content (editor/admin role required)
POST /api/search                        Advanced search with Q objects
GET  /api/articles                      List articles only (STI auto-filter)
GET  /api/videos                        List videos only (STI auto-filter)
GET  /api/links                         List links only (STI auto-filter)
GET  /api/profiles/{user_id}            User profile (OneToOneField)
PUT  /api/profiles/{user_id}            Update own profile (upsert)
GET  /api/stats                         Content statistics with Q object counting
GET  /admin/                            HyperAdmin CRUD panel
```

## Project Structure

```
content_hub/
    app.py          Models (STI + OneToOne), Q-based queries, HyperAdmin config
    seed.py         Sample users, profiles, articles, videos, links, tags
```
