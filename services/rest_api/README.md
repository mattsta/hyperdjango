# REST API Example

A complete Blog REST API built with HyperDjango, demonstrating CRUD endpoints, session auth, API key auth, CORS, and OpenAPI docs.

## Setup

```bash
# Ensure PostgreSQL is running, then create the database:
createdb blog

# Run the app:
python app.py
```

The server starts on `http://localhost:8000` by default.

## Endpoints

| Method | Path               | Auth    | Description               |
| ------ | ------------------ | ------- | ------------------------- |
| POST   | `/auth/register`   | None    | Register a new user       |
| POST   | `/auth/login`      | None    | Login (creates session)   |
| POST   | `/auth/logout`     | Session | Logout (destroys session) |
| GET    | `/api/posts`       | None    | List published posts      |
| GET    | `/api/posts/{id}`  | None    | Get a single post         |
| POST   | `/api/posts`       | Session | Create a post             |
| PUT    | `/api/posts/{id}`  | Session | Update a post             |
| DELETE | `/api/posts/{id}`  | Session | Delete a post             |
| GET    | `/api/admin/stats` | API Key | Admin stats               |
| GET    | `/api/admin/users` | API Key | List all users            |
| GET    | `/health`          | None    | Health check              |
| GET    | `/docs`            | None    | OpenAPI documentation     |

## Auth

- **Session auth**: Login via `/auth/login`, then pass the session cookie on subsequent requests.
- **API key auth**: Pass `X-API-Key: sk_live_demo_key_123` header for admin endpoints.
