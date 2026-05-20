# Blog Platform

Multi-language blog showcasing **sitemaps**, **RSS/Atom feeds**, and **i18n**.

## Features

- XML Sitemaps with section-based pagination (`sitemaps.py`)
- RSS 2.0 and Atom 1.0 feed generation (`syndication.py`)
- Internationalization with LocaleMiddleware (`i18n.py`)
- REST API with cursor pagination
- HyperAdmin panel
- Swagger UI at `/docs/`

## Setup

```bash
cd services/blog_platform
uv run hyper setup --app services.blog_platform.app:app --drop --seed services.blog_platform.seed:run
uv run hyper run --app services.blog_platform.app:app --port 8750
```

## Key Routes

| Route                  | Description                   |
| ---------------------- | ----------------------------- |
| `GET /`                | Latest posts (language-aware) |
| `GET /post/{slug}`     | Single post                   |
| `GET /category/{slug}` | Posts by category             |
| `GET /sitemap.xml`     | XML sitemap                   |
| `GET /feed/rss`        | RSS 2.0 feed                  |
| `GET /feed/atom`       | Atom 1.0 feed                 |
| `GET /api/posts`       | JSON API (paginated)          |
| `GET /admin/`          | Admin panel                   |
| `GET /docs/`           | Swagger UI                    |
