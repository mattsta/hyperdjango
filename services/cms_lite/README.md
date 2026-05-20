# CMS Lite

Lightweight CMS showcasing **URL redirects** and **flat pages**.

## Features

- Flat pages with FlatPageRegistry (`flatpages.py`)
- Auth-gated pages (registration_required flag)
- URL redirect management with RedirectRegistry (`redirects.py`)
- O(1) exact-match + prefix redirects
- Open-redirect protection
- HyperAdmin panel
- Swagger UI at `/docs/`

## Setup

```bash
cd services/cms_lite
uv run hyper setup --app services.cms_lite.app:app --drop --seed services.cms_lite.seed:run
uv run hyper run --app services.cms_lite.app:app --port 8760
```

## Key Routes

| Route                | Description               |
| -------------------- | ------------------------- |
| `GET /`              | Homepage flat page        |
| `GET /about/`        | About page                |
| `GET /terms/`        | Terms (auth-gated)        |
| `GET /help/`         | Help center               |
| `GET /privacy/`      | Privacy policy            |
| `GET /old-about`     | 301 redirect → /about/    |
| `GET /info`          | 302 redirect → /about/    |
| `GET /api/pages`     | List all pages (JSON)     |
| `GET /api/redirects` | List all redirects (JSON) |
| `GET /admin/`        | Admin panel               |
| `GET /docs/`         | Swagger UI                |
