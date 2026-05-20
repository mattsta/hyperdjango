# Full-Stack Task Manager

Complete reference scaffold for new HyperDjango developers. A project/task manager with session auth, template inheritance, CRUD, JSON API, and HyperAdmin.

## Quick Start

```bash
uv run hyper setup --app services.full_stack.app:app --seed services.full_stack.seed:run
uv run hyper run --app services.full_stack.app:app --port 8400
```

Visit http://localhost:8400 — register an account and start creating projects.

## Features

- **Session auth**: Register, login, logout with argon2id password hashing
- **3 models with FK relationships**: User -> Project -> Task (with TaskStatus enum)
- **Template inheritance**: `base.html` -> dashboard, project forms, login, register
- **Project CRUD**: Create projects, view detail with task list
- **Task CRUD**: Add tasks to projects, update status (todo/in_progress/done), delete
- **JSON API**: `/api/projects` and `/api/projects/{id}/tasks` (auth required)
- **HyperAdmin panel**: Full CRUD at `/admin/` for all 3 models
- **Error handling**: Global exception handler for consistent JSON errors
- **Health probes**: `/health` and `/ready` with DB connection check

## Routes

```
GET  /                              Dashboard (auth required)
GET  /login                         Login page
POST /login                         Login submit
GET  /register                      Register page
POST /register                      Register submit
GET  /logout                        Logout
GET  /projects/new                  New project form
POST /projects/new                  Create project
GET  /projects/{id}                 Project detail with tasks
POST /projects/{id}/tasks           Add task to project
POST /tasks/{id}/status             Update task status
POST /tasks/{id}/delete             Delete task
GET  /api/projects                  JSON: list user's projects (auth required)
GET  /api/projects/{id}/tasks       JSON: list project tasks (auth required)
GET  /admin/                        HyperAdmin dashboard
GET  /health                        Health check
GET  /ready                         Readiness probe
```

## Platform Features Demonstrated

- **HyperApp** with route decorators and template rendering
- **Model** with Field, foreign_key (type-safe class references), Enum fields
- **SessionAuth** with login/logout and session cookies
- **CSRFMiddleware** for form protection
- **HyperAdmin** with model registration, search, and filters
- **Template inheritance** via Zig template engine (`{% extends "base.html" %}`)
- **HTTPException** for proper error responses (401, 404)

## Project Structure

```
full_stack/
    app.py          Models, routes, auth, API, admin registration
    seed.py         Demo user + sample project with tasks
    setup.py        Database table creation
    templates/      base.html, dashboard, project forms, auth pages
```
