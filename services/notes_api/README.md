# Notes API

Intermediate HyperDjango service bridging the gap between the minimal hello app (34 lines) and the full REST API service (278 lines). Demonstrates the core patterns every HyperDjango app needs in approximately 170 lines: session auth with signed cookies, cursor-paginated JSON endpoints, F expression atomic updates, full-text search with PostgreSQL FTS expressions, and a HyperAdmin auto-CRUD panel.

## Features

- 3 models with FK relationships (User, Category, Note)
- Session auth with signed cookies (register, login, logout)
- JSON REST endpoints with CursorPagination
- Full-text search using SearchVector, SearchQuery, and SearchRank
- F expression atomic updates for Category.note_count
- to_dict() serialization with Field(exclude=True) for password_hash
- HyperAdmin auto-CRUD panel with search and ordering
- Health endpoint
- Generic exception handler (logs full error, returns safe 500)

## Setup

```bash
cd services/notes_api
uv run hyper setup --app app:app --drop --seed seed:run
uv run hyper start --app app:app --port 18811
```

## Credentials

Seeded usernames: `admin`, `alice` (notes*users table) and `admin` (HyperAdmin
panel via hyper_users). Passwords are not hardcoded — they resolve from
`HYPER_SEED_PASSWORD*<USERNAME>`/`HYPER_SEED_PASSWORD`/`HYPER_ADMIN_PASSWORD`
settings, or are generated randomly and printed to the seed log. Set the env
vars before seeding to use known values:

```bash
HYPER_SEED_PASSWORD=devpw HYPER_ADMIN_PASSWORD=devpw \
  uv run hyper setup --app app:app --drop --seed seed:run
```

## Key Routes

**Auth**

- `POST /auth/register` -- Register (username + password, min 8 chars)
- `POST /auth/login` -- Login (returns session cookie)
- `POST /auth/logout` -- Logout

**Notes API**

- `GET /api/notes/` -- List notes (cursor-paginated, 20 per page)
- `POST /api/notes/` -- Create note (auth required; body: title, body, category_id)
- `GET /api/notes/{id}` -- Get single note
- `DELETE /api/notes/{id}` -- Delete note (owner only)
- `GET /api/notes/search?q=term` -- Full-text search (min 2 chars)

**Categories**

- `GET /api/categories/` -- List categories with note counts

**Admin and Health**

- `GET /` -- Redirects to /admin/
- `GET /admin/` -- HyperAdmin panel
- `GET /health` -- Health check

## Seed Data

- 2 users: admin (5 notes), alice (5 notes)
- 5 categories: Work, Personal, Ideas, Learning, Projects
- 10 notes distributed across categories with accurate note_count values

## Example Usage

```bash
# Register a new user
curl -X POST http://localhost:18811/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username": "bob", "password": "password123"}'

# Login (replace $PASSWORD with HYPER_SEED_PASSWORD or the random value from the seed log)
curl -c cookies.txt -X POST http://localhost:18811/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\": \"admin\", \"password\": \"$PASSWORD\"}"

# List notes (paginated)
curl http://localhost:18811/api/notes/

# Create a note (requires auth cookie)
curl -b cookies.txt -X POST http://localhost:18811/api/notes/ \
  -H 'Content-Type: application/json' \
  -d '{"title": "My note", "body": "Content here", "category_id": 1}'

# Search notes
curl 'http://localhost:18811/api/notes/search?q=python'

# List categories with counts
curl http://localhost:18811/api/categories/
```
