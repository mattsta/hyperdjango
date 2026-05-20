# CLI Commands

The `hyper` command-line tool for project management, database operations, and development. All commands are run via `uv run hyper`.

## Commands Overview

| Command                   | Description                                          |
| ------------------------- | ---------------------------------------------------- |
| `hyper new`               | Create a new project                                 |
| `hyper service list`      | Table of every bundled service                       |
| `hyper service info`      | Everything needed to run one service                 |
| `hyper service run`       | Set up, seed and serve a service + its companions    |
| `hyper service stop`      | Stop a service and its companions                    |
| `hyper service install`   | Install systemd units for a service + its companions |
| `hyper service uninstall` | Remove the units `service install` created           |
| `hyper run`               | Start the development server (foreground)            |
| `hyper start`             | Start server in background (daemon, PID file)        |
| `hyper stop`              | Graceful shutdown (SIGTERM → drain → exit)           |
| `hyper restart`           | Stop + start                                         |
| `hyper status`            | Check if server is running                           |
| `hyper setup`             | Create tables from models (topological FK ordering)  |
| `hyper routes`            | List all registered routes                           |
| `hyper makemigrations`    | Generate migration files from model changes          |
| `hyper migrate`           | Apply or rollback migrations                         |
| `hyper createsuperuser`   | Create an admin user                                 |
| `hyper collectstatic`     | Collect static files for production                  |
| `hyper shell`             | Interactive Python shell with app context            |
| `hyper dbshell`           | PostgreSQL interactive shell                         |
| `hyper inspectdb`         | Generate model code from existing tables             |
| `hyper dumpdata`          | Export database rows to JSON                         |
| `hyper loaddata`          | Import JSON fixtures into database                   |
| `hyper doctor`            | Diagnose build, config, database, and perf issues    |
| `hyper systemd install`   | Generate and install systemd service unit            |
| `hyper systemd uninstall` | Remove systemd service                               |
| `hyper systemd status`    | Show systemd service status                          |
| `hyper benchmark`         | Run EXPLAIN ANALYZE performance regression tests     |

---

## hyper service

Runs the services bundled in `services/` — from a clean clone to a serving
process in one command. Every machine-readable fact about a service (app path,
seed, port, required secrets, companions) lives in
`hyperdjango/services_registry.py`, the single source of truth the CLI and the
e2e suite both read.

```bash
uv run hyper service list                  # every service: port, needs, what it demonstrates
uv run hyper service info hypersecret      # full detail + the equivalent manual commands
uv run hyper service run hypernews         # set up, seed and serve
uv run hyper service run hypersecret       # starts its companion (hypermanager) first
uv run hyper service stop hypersecret      # stops the service and its companions
uv run hyper service install hypersecret --dry-run   # print the systemd units, change nothing
sudo uv run hyper service install hypersecret --enable --start
sudo uv run hyper service uninstall hypersecret      # remove the units it created
```

### hyper service run

```bash
uv run hyper service run <name> [--port N] [--no-seed] [--fresh]
```

| Option      | Description                                           |
| ----------- | ----------------------------------------------------- |
| `--port N`  | Override the service's registry port                  |
| `--no-seed` | Create tables but skip seed data                      |
| `--fresh`   | Drop and recreate tables before seeding (DESTRUCTIVE) |

In order, `run`:

1. **Verifies the native extension** and, if missing, prints the exact build
   command. It never builds silently.
2. **Resolves a per-service database** — `hyper_service_<name>`, derived from
   your ambient `DATABASE_URL` — so running one service never drops another's
   tables. A missing database is provisioned by `hyper setup`'s existing
   auto-creation.
3. **Resolves secrets.** Services with `require_setting(..., min_length=32)`
   gates get stable random values generated on first run and persisted to
   `services/<name>/.env.local` (gitignored, mode 0600), so the seed process
   and the server process sign with identical keys. Credentials that only a
   human can supply (`EMBEDDINGS_API_KEY`) are reported, never invented.
4. **Runs `hyper setup`** (plus the seed unless `--no-seed`).
5. **Starts companions first**, wiring each dependent with the companion's
   live URL and the identity token its seed minted, then starts the service —
   printing every URL worth opening and the demo credentials.
6. **Supervises.** Ctrl-C (or SIGTERM), a `hyper service stop` from another
   terminal, or any supervised process dying tears the whole set down cleanly.

Ports live in the **8600-8699** block, deliberately disjoint from the e2e
suite's reserved test ports (18100-19260) so serving a service never collides
with a test run.

### First-run example

```bash
$ uv run hyper service run hypersecret
  native extension present
  hypermanager: generated HYPER_SESSION_SIGNING_KEY, HYPER_SECRET_KEY, ... -> services/hypermanager/.env.local
  hypersecret:  generated HYPER_SESSION_SIGNING_KEY, HYPER_SECRET_KEY, ... -> services/hypersecret/.env.local
  PostgreSQL reachable
── hypermanager ─────────────────
  database ready (hyper_service_hypermanager)
  hypermanager is serving
── hypersecret ─────────────────
  database ready (hyper_service_hypersecret)
  hypersecret is serving

  hypermanager  (companion)  http://127.0.0.1:8611
      http://127.0.0.1:8611/admin/           HyperAdmin panel
  hypersecret   (main app)   http://127.0.0.1:8613
      http://127.0.0.1:8613/docs             Swagger UI

  Press Ctrl-C to stop everything.
```

### hyper service install / uninstall

```bash
uv run hyper service install <name> [--enable] [--start] [--user] [--dry-run]
                                    [--port N] [--host H] [--run-as USER] [--no-setup]
uv run hyper service uninstall <name> [--user] [--dry-run]
```

| Option          | Description                                                                   |
| --------------- | ----------------------------------------------------------------------------- |
| `--enable`      | Enable the units so they start at boot                                        |
| `--start`       | Start them immediately                                                        |
| `--user`        | Install `systemctl --user` units under `~/.config/systemd/user` (no root)     |
| `--dry-run`     | Print the unit files (secrets redacted) and systemctl commands; write nothing |
| `--port N`      | Override the registry port for the named service                              |
| `--host H`      | Bind address baked into `ExecStart` (default `127.0.0.1`)                     |
| `--run-as USER` | Account the unit runs as (default: the user who invoked `sudo`)               |
| `--no-setup`    | Skip `hyper setup` (+ seed); assume the database is already prepared          |

Everything in the unit is derived from the registry, and the `EnvironmentFile`
reuses the service's already-persisted `.env.local` secrets so seeded tokens
keep verifying. Companions get their own units, ordered first and bound to the
parent's lifecycle; database-backed units get a `pg_isready` `ExecStartPre` gate
and restart backoff; all units join `hyperdjango.target`. These units are
**restart-only** — the server has no SIGHUP reload path, so no `ExecReload` is
emitted. Full reference:
[Running the bundled services](running-services.md#running-a-service-permanently-systemd-units).

---

## hyper new

Create a new project with scaffolded files and configuration.

```bash
uv run hyper new myproject              # minimal project
uv run hyper new myproject --with-db    # + database configuration
uv run hyper new myproject --with-auth  # + authentication
uv run hyper new myproject --with-admin # + admin interface
uv run hyper new myproject --full       # all of the above
```

### Options

| Flag           | Description                                                         |
| -------------- | ------------------------------------------------------------------- |
| `--with-db`    | Add database configuration, `models.py`, and migration setup        |
| `--with-auth`  | Add authentication (login/logout views, User model, session config) |
| `--with-admin` | Add HyperAdmin interface with auto-CRUD                             |
| `--full`       | Enable all flags: `--with-db --with-auth --with-admin`              |

### Generated file structure

**Minimal (`hyper new myproject`):**

```
myproject/
├── app.py              # Application entry point with example route
├── .gitignore          # Git ignore rules (*.pyc, __pycache__, .env, etc.)
├── README.md           # Project README with quick start instructions
├── pyproject.toml      # Python project config with hyperdjango dependency
├── templates/          # HTML templates
│   └── base.html       # Base template with <!DOCTYPE html> skeleton
└── static/             # Static files (CSS, JS, images)
```

**With database (`--with-db`):**

```
myproject/
├── app.py              # App with db.configure() call
├── models.py           # Example model with TimestampMixin
├── migrations/         # Migration directory (empty initially)
├── .gitignore
├── README.md
├── pyproject.toml
├── templates/
│   └── base.html
└── static/
```

**With auth (`--with-auth`):**

```
myproject/
├── app.py              # App with auth middleware registered
├── models.py           # User model + additional models
├── migrations/
├── .gitignore
├── README.md
├── pyproject.toml
├── templates/
│   ├── base.html
│   └── login.html      # Login form template
└── static/
```

**Full (`--full`):**

```
myproject/
├── app.py              # Full app with db, auth, admin configured
├── models.py           # User + example models
├── migrations/
├── .gitignore
├── README.md
├── pyproject.toml
├── templates/
│   ├── base.html
│   └── login.html
└── static/
```

### Generated app.py (--full)

The generated `app.py` includes:

```python
from hyperdjango import HyperApp, Response
from hyperdjango.auth import SessionAuth
from hyperdjango.admin import HyperAdmin
import models

app = HyperApp()

# Database
app.configure_db(
    host="localhost",
    port=5432,
    database="myproject",
    user="postgres",
    password="postgres",
)

# Auth
app.use(SessionAuth(secret="change-me-in-production"))

# Admin
admin = HyperAdmin(app)
admin.register(models.User)


@app.get("/")
async def index(request):
    return Response.html("<h1>Welcome to myproject</h1>")
```

---

## hyper run

Start the development server using the native Zig HTTP server.

```bash
uv run hyper run                    # default: 0.0.0.0:8000
uv run hyper run --port 3000        # custom port
uv run hyper run --host 127.0.0.1   # localhost only
uv run hyper run --prod             # production mode
```

### Options

| Flag     | Default   | Description                                                            |
| -------- | --------- | ---------------------------------------------------------------------- |
| `--host` | `0.0.0.0` | Bind address. Use `127.0.0.1` for localhost-only access                |
| `--port` | `8000`    | Port number                                                            |
| `--prod` | off       | Production mode: validates config, disables debug, requires SECRET_KEY |

### Development mode (default)

- Hot reload enabled: file changes trigger automatic restart via native kqueue/inotify watcher
- Debug error pages with tracebacks
- Verbose logging to stdout

### Production mode (`--prod`)

Production mode performs configuration validation before starting:

- **SECRET_KEY required:** Refuses to start without a configured secret key
- **Debug disabled:** No tracebacks in error responses
- **Hot reload disabled:** No file watching overhead
- **Optimized logging:** Structured JSON logs instead of human-readable

```bash
SECRET_KEY=your-secret-here uv run hyper run --prod --port 8080
```

---

## hyper start

Start the server as a background daemon. Writes a PID file and redirects output to a log file.

```bash
hyper start --app app:app --port 8000 --prod
```

### Options

| Flag     | Default     | Description     |
| -------- | ----------- | --------------- |
| `--host` | `127.0.0.1` | Bind address    |
| `--port` | `8000`      | Port number     |
| `--app`  | `app:app`   | App import path |
| `--prod` | off         | Production mode |

Creates `.hyper.<port>.pid` (PID file) and `.hyper.<port>.log` (output log).

---

## hyper stop

Gracefully stop a running server. Sends SIGTERM, waits for the drain timeout, then SIGKILL as fallback.

```bash
hyper stop --port 8000
hyper stop --port 8000 --timeout 60    # Wait longer for drain
```

### Options

| Flag        | Default | Description                    |
| ----------- | ------- | ------------------------------ |
| `--port`    | `8000`  | Port of the server to stop     |
| `--timeout` | `30`    | Seconds to wait before SIGKILL |

---

## hyper restart

Stop and restart the server.

```bash
hyper restart --app app:app --port 8000 --prod
```

Inherits all options from `hyper start` plus `--timeout` from `hyper stop`.

---

## hyper status

Check if a server is running on the given port.

```bash
hyper status --port 8000
```

Reads the PID file and verifies the process is alive. Cleans up stale PID files.

---

## hyper systemd

Manage systemd service integration for production deployments.

### hyper systemd install

Generate and install a systemd unit file with production-grade defaults.

```bash
# As root: install and optionally enable
sudo hyper systemd install --app app:app --port 8000 --enable

# Without root: generates unit file in current directory
hyper systemd install --app app:app --port 8000
```

| Flag       | Default      | Description                  |
| ---------- | ------------ | ---------------------------- |
| `--host`   | `0.0.0.0`    | Bind address                 |
| `--port`   | `8000`       | Port number                  |
| `--app`    | `app:app`    | App import path              |
| `--name`   | dir name     | Service name                 |
| `--user`   | current user | Run-as user                  |
| `--enable` | off          | Enable and start immediately |

### hyper systemd uninstall

Stop, disable, and remove the systemd unit file.

```bash
sudo hyper systemd uninstall
```

### hyper systemd status

Show the current service status via `systemctl status`.

```bash
hyper systemd status
```

---

## hyper setup

Create database tables from app models. Introspects all `Model` subclasses loaded by the app, generates `CREATE TABLE IF NOT EXISTS` SQL for each, and executes it. Tables are created in topological order (Kahn's algorithm) so foreign key dependencies are satisfied.

```bash
uv run hyper setup --app myapp.app:app                    # create tables
uv run hyper setup --app myapp.app:app --seed seed:run     # create + seed
uv run hyper setup --app myapp.app:app --drop              # drop and recreate (DESTRUCTIVE)
uv run hyper setup --app services.rest_api.app:app         # dotted module paths work
```

### Options

| Flag         | Description                                                       |
| ------------ | ----------------------------------------------------------------- |
| `--app`      | App import path (`module:attribute`). Required.                   |
| `--database` | Override database URL (default: reads from app's `database_url`)  |
| `--seed`     | Seed function import path (`module:function`). Runs after tables. |
| `--drop`     | Drop all tables before recreating. **DESTRUCTIVE.**               |

### How it works

1. **Imports the app module** — this registers all `Model` subclasses in the model registry
2. **Reads the database URL** from the app or `--database` flag
3. **Sorts models by FK dependencies** using topological sort (Kahn's algorithm)
4. **Generates SQL** for each model: column types, PRIMARY KEY, UNIQUE, DEFAULT, FOREIGN KEY REFERENCES
5. **Handles special Meta options**: `Meta.unlogged = True` generates `CREATE UNLOGGED TABLE`, composite primary keys generate table-level `PRIMARY KEY (col_a, col_b)` constraints
6. **Creates vector indexes** with optional `WITH (...)` tuning parameters from `VectorField(index_params=...)`
7. **Executes** DDL in dependency order

### Field type mapping

| Python type | PostgreSQL type                   |
| ----------- | --------------------------------- |
| `int`       | `INTEGER` (or `SERIAL` if `auto`) |
| `str`       | `TEXT` (or `VARCHAR(n)`)          |
| `float`     | `DOUBLE PRECISION`                |
| `bool`      | `BOOLEAN`                         |
| `datetime`  | `TIMESTAMPTZ`                     |
| `bytes`     | `BYTEA`                           |

### Default values

`Field(default=...)` values are translated to SQL `DEFAULT` clauses:

| Python default   | SQL DEFAULT          |
| ---------------- | -------------------- |
| `True` / `False` | `DEFAULT TRUE/FALSE` |
| `0`, `42`        | `DEFAULT 0`, `42`    |
| `"hello"`        | `DEFAULT 'hello'`    |
| `"now()"`        | `DEFAULT NOW()`      |
| `""`             | `DEFAULT ''`         |

### Example

```python
# models.py
from datetime import datetime
from hyperdjango.models import Field, Model


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: str = Field()
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default="now()")
```

```bash
$ uv run hyper setup --app myapp:app
Found 1 model(s): User
  users ✓
1 table(s) created.
Setup complete!
```

Generated SQL:

```sql
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE,
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## hyper routes

List all registered routes with their HTTP methods and handler names.

```bash
uv run hyper routes
```

### Output format

```
METHOD  PATH                          HANDLER
GET     /                             index
GET     /articles/                    article_list
GET     /articles/{id:int}/           article_detail
POST    /articles/                    article_create
PUT     /articles/{id:int}/           article_update
DELETE  /articles/{id:int}/           article_delete
GET     /admin/                       admin_index
GET     /health                       health_check
WS      /ws/chat                      chat_handler
```

The output includes:

- **METHOD**: HTTP method (GET, POST, PUT, DELETE, PATCH) or WS for WebSocket
- **PATH**: URL pattern with parameter types shown (e.g., `{id:int}`, `{slug:str}`)
- **HANDLER**: Python function name of the view
- **VIEW**: the resolved import path of the handler (`module.qualname`)

### Options

```bash
uv run hyper routes --format json        # machine-readable: [{method, pattern, name, view}]
uv run hyper routes --format stacked     # one route per block, with the view path
uv run hyper routes --prefix /api        # only routes whose pattern starts with /api
uv run hyper routes --unsorted           # preserve registration order (default: sorted by pattern)
```

`--format` accepts `tabular` (default), `stacked`, or `json`.

---

## hyper makemigrations

Detect model changes by comparing the current model definitions against the last migration state, and generate migration files.

```bash
uv run hyper makemigrations
uv run hyper makemigrations --name "add_user_email"  # custom migration name
uv run hyper makemigrations --dry-run                 # preview without writing
```

### Options

| Flag        | Description                                                      |
| ----------- | ---------------------------------------------------------------- |
| `--name`    | Custom name for the migration file (used as suffix)              |
| `--dry-run` | Show what migration would be generated without writing any files |

### How it works

1. Introspects all registered Model classes and their fields
2. Compares against the state stored in the latest migration file
3. Detects: added fields, removed fields, changed field types, renamed tables, new models, deleted models
4. Generates a numbered migration file in the `migrations/` directory

### Generated migration file

```python
# migrations/0002_add_user_email.py
from hyperdjango.migrations import Migration, AddField, Field


class Migration_0002(Migration):
    dependencies = ["0001_initial"]

    operations = [
        AddField(
            table="users",
            name="email",
            field=Field(type="varchar(254)", nullable=False, unique=True),
        ),
    ]

    def rollback_operations(self):
        return [
            DropField(table="users", name="email"),
        ]
```

### Dry run output

```bash
uv run hyper makemigrations --dry-run
```

```
Detected changes:
  users:
    + Add field email (varchar(254), NOT NULL, UNIQUE)
    ~ Change field name max_length: 100 → 200

Would generate: migrations/0002_auto.py
(dry-run mode, no files written)
```

---

## hyper migrate

Apply pending migrations to the database or rollback to a previous state.

```bash
uv run hyper migrate                # apply all pending
uv run hyper migrate --rollback     # rollback last migration
uv run hyper migrate --target 0003  # migrate to specific version
uv run hyper migrate --fake         # mark as applied without running SQL
```

### Options

| Flag               | Description                                                |
| ------------------ | ---------------------------------------------------------- |
| `--rollback`       | Rollback the most recently applied migration               |
| `--target VERSION` | Migrate forward or backward to a specific migration number |
| `--fake`           | Record the migration as applied without executing its SQL  |

### Apply all pending

```bash
uv run hyper migrate
```

```
Applying migrations:
  0001_initial ... OK (12ms)
  0002_add_user_email ... OK (8ms)
  0003_add_articles ... OK (15ms)

All migrations applied.
```

### Rollback

```bash
uv run hyper migrate --rollback
```

```
Rolling back:
  0003_add_articles ... OK (6ms)

Rolled back to 0002_add_user_email.
```

### Target a specific version

```bash
# Roll back from 0005 to 0003
uv run hyper migrate --target 0003
```

```
Rolling back:
  0005_add_tags ... OK (4ms)
  0004_add_categories ... OK (5ms)

Migrated to 0003_add_articles.
```

```bash
# Migrate forward to a specific version
uv run hyper migrate --target 0005
```

### Fake migration

Useful when you've manually applied schema changes and need to update the migration tracking table:

```bash
uv run hyper migrate --fake
```

```
Faking migrations:
  0004_add_categories ... FAKED
  0005_add_tags ... FAKED

All migrations marked as applied (no SQL executed).
```

---

## hyper createsuperuser

Create an admin user interactively. Prompts for username, email, and password.

```bash
uv run hyper createsuperuser
```

### Interactive prompts

```
Username: admin
Email: admin@example.com
Password: ********
Password (confirm): ********
Superuser 'admin' created successfully.
```

### Validation

- **Username**: Required, must be unique
- **Email**: Required, must be a valid email format, must be unique
- **Password**: Required, minimum 8 characters, checked against password validators (no common passwords, not too similar to username)
- **Confirm password**: Must match

If validation fails, the prompt repeats with an error message:

```
Username: admin
Email: admin@example.com
Password: ********
Password (confirm): ********
Error: This password is too common. Please choose a different password.
Password: ********
Password (confirm): ********
Superuser 'admin' created successfully.
```

The password is hashed with argon2id before storage. The user is created with `is_superuser=True` and `is_staff=True`.

---

## hyper collectstatic

Collect static files from all registered static directories into a single `STATIC_ROOT` directory for production serving.

```bash
uv run hyper collectstatic                    # collect to STATIC_ROOT
uv run hyper collectstatic --dry-run          # preview without copying
uv run hyper collectstatic --clear            # clear destination first
```

### Options

| Flag        | Description                                         |
| ----------- | --------------------------------------------------- |
| `--dry-run` | Show which files would be collected without copying |
| `--clear`   | Delete everything in STATIC_ROOT before collecting  |

### Content-hash manifesting

Collected files are renamed with a content hash for cache busting:

```
static/
├── style.css          → staticfiles/style.abc123def.css
├── app.js             → staticfiles/app.789xyz456.js
└── images/logo.png    → staticfiles/images/logo.fa3b2c1d.png
```

A `staticfiles.json` manifest is generated that maps original names to hashed names. The `get_static_url()` function uses this manifest in templates:

```html
<link rel="stylesheet" href="{{ get_static_url('style.css') }}" />
<!-- Renders: <link rel="stylesheet" href="/static/style.abc123def.css"> -->
```

### CSS url() rewriting

CSS files have their `url()` references rewritten to point to the hashed filenames:

```css
/* Before */
background: url(../images/logo.png);

/* After */
background: url(../images/logo.fa3b2c1d.png);
```

### Dry run output

```bash
uv run hyper collectstatic --dry-run
```

```
Would collect:
  static/style.css → staticfiles/style.abc123def.css
  static/app.js → staticfiles/app.789xyz456.js
  static/images/logo.png → staticfiles/images/logo.fa3b2c1d.png

3 files would be collected (dry-run mode, no files copied).
```

---

## hyper shell

Interactive Python shell with the app context pre-loaded. Uses the standard Python REPL.

```bash
uv run hyper shell
```

### Pre-loaded namespace

When the shell starts, these are already imported and available:

| Name          | Description                                                     |
| ------------- | --------------------------------------------------------------- |
| `app`         | The HyperApp instance                                           |
| `db`          | Database connection object                                      |
| All models    | Every registered Model class (e.g., `User`, `Article`, `Order`) |
| `asyncio`     | The asyncio module                                              |
| `asyncio.run` | For running async operations                                    |

### Example session

```python
>>> User
<class 'models.User'>
>>> asyncio.run(User.objects.count())
42
>>> asyncio.run(User.objects.filter(active=True).all())
[User(id=1, name='Alice'), User(id=2, name='Bob'), ...]
>>> user = asyncio.run(User.objects.get(id=1))
>>> user.name
'Alice'
>>> user.name = "Alice Smith"
>>> asyncio.run(user.save())
>>> asyncio.run(db.query("SELECT version()"))
[('PostgreSQL 16.13 ...',)]
```

### Running async code

Since the shell is a standard synchronous REPL, use `asyncio.run()` to execute async operations:

```python
>>> import asyncio
>>> asyncio.run(Article.objects.filter(title__contains="Python").all())
[Article(id=5, title='Python Tips'), Article(id=12, title='Advanced Python')]
```

---

## hyper dbshell

Open an interactive PostgreSQL shell (`psql`) connected to the project's database.

```bash
uv run hyper dbshell
```

The command reads database connection settings from the app configuration and launches `psql` with the correct host, port, database, and user.

### Passing psql arguments

Any additional arguments are forwarded directly to `psql`:

```bash
uv run hyper dbshell -- -c "SELECT COUNT(*) FROM users"
uv run hyper dbshell -- -f schema.sql
uv run hyper dbshell -- --tuples-only -c "SELECT name FROM users"
```

### Example session

```
psql (16.13)
Type "help" for help.

myproject=> \dt
              List of relations
 Schema |      Name      | Type  | Owner
--------+----------------+-------+-------
 public | articles       | table | postgres
 public | users          | table | postgres
 public | migrations     | table | postgres
(3 rows)

myproject=> SELECT id, name, email FROM users LIMIT 5;
 id |  name  |      email
----+--------+-----------------
  1 | Alice  | alice@example.com
  2 | Bob    | bob@example.com
(2 rows)
```

---

## hyper inspectdb

Generate HyperDjango model code from existing database tables. Useful for integrating with legacy databases or bootstrapping models from an existing schema.

```bash
uv run hyper inspectdb                   # all tables
uv run hyper inspectdb --table users     # specific table
```

### Options

| Flag           | Description                              |
| -------------- | ---------------------------------------- |
| `--table NAME` | Generate model for a specific table only |

### Output

The command prints Python model code to stdout. Redirect to a file to save:

```bash
uv run hyper inspectdb > models.py
uv run hyper inspectdb --table users >> models.py
```

### Example output

```python
class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    email: str = Field(unique=True)
    is_active: bool = Field(default=True)
    created_at: datetime | None = Field(default=None)
```

### PostgreSQL type mapping

The command maps PostgreSQL column types to Python types:

| PostgreSQL Type            | Python Type         | Field Options                    |
| -------------------------- | ------------------- | -------------------------------- |
| `integer`, `serial`        | `int`               | `auto=True` for serial           |
| `bigint`, `bigserial`      | `int`               | `auto=True` for bigserial        |
| `smallint`                 | `int`               |                                  |
| `text`                     | `str`               |                                  |
| `varchar(N)`               | `str`               | `max_length=N`                   |
| `char(N)`                  | `str`               | `max_length=N`                   |
| `boolean`                  | `bool`              |                                  |
| `real`, `double precision` | `float`             |                                  |
| `numeric(P,S)`             | `Decimal`           | `max_digits=P, decimal_places=S` |
| `timestamp`, `timestamptz` | `datetime`          |                                  |
| `date`                     | `date`              |                                  |
| `time`, `timetz`           | `time`              |                                  |
| `uuid`                     | `UUID`              |                                  |
| `jsonb`, `json`            | `dict[str, object]` |                                  |
| `bytea`                    | `bytes`             |                                  |
| `inet`, `cidr`             | `str`               |                                  |

### Introspection capabilities

The `inspectdb` command detects:

- **Primary keys**: Columns in the primary key constraint get `primary_key=True`
- **Auto-increment**: Serial/bigserial columns get `auto=True`
- **Foreign keys**: Detected and annotated as comments (e.g., `# FK -> users.id`)
- **Unique constraints**: Single-column unique constraints get `unique=True`
- **Nullable**: Nullable columns use `Type | None` and `default=None`
- **Default values**: Server defaults are mapped to Python values where possible
- **Table naming**: Table name `user_profiles` generates class name `UserProfile`

### Foreign key detection

```python
class Order(Model):
    class Meta:
        table = "orders"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field()  # FK -> users.id
    product_id: int = Field()  # FK -> products.id
    quantity: int = Field()
    total: int = Field()
    created_at: datetime | None = Field(default=None)
```

---

## hyper benchmark

Run EXPLAIN ANALYZE performance benchmarks against a seeded database. Detects query plan regressions (timing, new sequential scans) by comparing against a saved baseline.

```bash
# Run benchmarks
hyper benchmark

# Save current results as baseline
hyper benchmark --save-baseline

# JSON output for CI pipelines
hyper benchmark --json

# Custom database and threshold
hyper benchmark --database postgres://localhost/mydb --threshold 1.5

# Smaller dataset for faster runs
hyper benchmark --posts 10000
```

### Options

| Option            | Default         | Description                             |
| ----------------- | --------------- | --------------------------------------- |
| `--database`      | `$DATABASE_URL` | PostgreSQL connection URL               |
| `--save-baseline` | false           | Save results to `.hyper.benchmark.json` |
| `--json`          | false           | Output as JSON                          |
| `--threshold`     | 2.0             | Regression detection multiplier         |
| `--posts`         | 50000           | Number of posts to seed                 |

### What It Tests

14 benchmark queries covering critical access patterns:

- **Front page sorts**: hot, new, top, controversial, rising (all must use indexes)
- **Single record**: post by PK, vote check (index lookup)
- **Pagination**: keyset pagination with composite ordering
- **Aggregation**: author score summaries, post counts
- **Search**: ILIKE title search
- **Joins**: post + author join with ordering

### Regression Detection

When a `.hyper.benchmark.json` baseline exists, the tool compares:

- **Timing regression**: query > baseline \* threshold → FAIL
- **New sequential scan**: index scan → seq scan → FAIL
- **Improvements**: query < baseline \* 0.5 → reported

### CI Integration

```yaml
# GitHub Actions
- name: Performance regression check
  run: |
    hyper benchmark --save-baseline  # first run
    # ... make changes ...
    hyper benchmark --threshold 2.0  # detect regressions
```

Exit code 0 = pass, 1 = regression detected.

---

## Custom Management Commands

You can add custom commands to the `hyper` CLI by registering them with the app:

```python
from hyperdjango import HyperApp

app = HyperApp()


@app.command("seed")
async def seed_command(args):
    """Seed the database with test data."""
    from models import User, Article

    for i in range(10):
        user = User(name=f"User {i}", email=f"user{i}@example.com")
        await user.save()

    for i in range(50):
        article = Article(
            title=f"Article {i}",
            body=f"Content for article {i}",
        )
        await article.save()

    print(f"Seeded 10 users and 50 articles.")
```

Run the custom command:

```bash
uv run hyper seed
```

### Command with arguments

```python
@app.command("import-csv")
async def import_csv_command(args):
    """Import data from a CSV file."""
    if len(args) < 1:
        print("Usage: hyper import-csv <filename>")
        return

    filename = args[0]
    count = 0
    with open(filename) as f:
        reader = csv.DictReader(f)
        for row in reader:
            user = User(name=row["name"], email=row["email"])
            await user.save()
            count += 1

    print(f"Imported {count} users from {filename}.")
```

```bash
uv run hyper import-csv users.csv
```

### Command with flags

```python
@app.command("cleanup")
async def cleanup_command(args):
    """Remove old data."""
    dry_run = "--dry-run" in args
    days = 90

    for arg in args:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    old_sessions = await Session.objects.filter(expires_at__lt=cutoff).all()

    if dry_run:
        print(f"Would delete {len(old_sessions)} expired sessions.")
    else:
        for session in old_sessions:
            await session.hard_delete()
        print(f"Deleted {len(old_sessions)} expired sessions.")
```

```bash
uv run hyper cleanup --dry-run --days=30
```
