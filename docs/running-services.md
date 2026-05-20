# Zero to Running: Clone and Run a Service

This page is the **clone-and-run path**: an empty machine, a `git clone` of this
repository, and one command that leaves you with a real service answering on
localhost with seeded data, an admin panel and demo credentials.

It is _not_ the same journey as the README's **Quickstart**, which starts a
**new, empty project** from the published `hyperdjango` package. Pick by intent:

| You want to…                                       | Follow                                          |
| -------------------------------------------------- | ----------------------------------------------- |
| Start your own app from scratch                    | [Getting Started](getting-started.md)           |
| Run the bundled services from a clone of this repo | **this page**                                   |
| Read what each service teaches, feature by feature | [Bundled Services](services.md)                 |
| Walk through a service's source                    | [Service Walkthroughs](service-walkthroughs.md) |

The apps under `services/` are production-quality **services**, not toy demos:
real applications with models, auth, seeded data, an admin panel and end-to-end
tests, each one deployable as it stands. They are how the framework proves
itself, and the best source to read when you want to know how something is
meant to be built.

---

## Prerequisites — what you supply, what the bootstrap installs

**You must supply:**

| Requirement                                                        | Why                                                                                                                                                                                                                                             |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Linux (x86_64/aarch64) or macOS (arm64)                            | The bootstrap only has a pinned Zig download for these; anything else needs Zig installed by hand.                                                                                                                                              |
| `git`, `curl`, `tar`                                               | Used to clone and to fetch the toolchains.                                                                                                                                                                                                      |
| A C toolchain / Xcode CLT                                          | Needed by some Python wheels and by the linker the Zig build drives.                                                                                                                                                                            |
| A **PostgreSQL server** you can reach                              | Every service except `hello` and `benchmark_app` stores data. The bootstrap can _install_ it on Ubuntu (`make bootstrap-db`), but on macOS you install PostgreSQL yourself (`brew install postgresql@18 && brew services start postgresql@18`). |
| **Passwordless `sudo`** _(only for `make bootstrap-db` on Ubuntu)_ | Installing PostgreSQL, creating the login role and marking `pgvector` trusted are privileged operations. Without sudo the database steps are **skipped with a printed explanation** — the build still completes.                                |
| Network access                                                     | To download `uv`, CPython 3.14t, the Python dependencies and the Zig toolchain.                                                                                                                                                                 |

**The bootstrap installs and manages for you — do not install these by hand:**

- `uv` (into `~/.local/bin` if missing)
- free-threaded **CPython 3.14t** and the `.venv` (it _recreates_ a `.venv` that
  was built with a GIL-enabled interpreter)
- all Python dependencies (`uv sync --group dev`)
- a pinned, SHA-verified **Zig** toolchain into `.toolchain/` when no usable
  `zig` is on `PATH` — `hyper-build` finds it there with no `PATH` setup
- the native extension itself (`ReleaseFast`), plus an import smoke test
- with `make bootstrap-db`: PostgreSQL 18 + pgvector, a login role with
  `CREATEDB`, the `hyperdjango_test` database, and `pgvector` marked trusted

---

## The five steps

### 1. Clone

```bash
git clone https://github.com/mattsta/hyperdjango.git
cd hyperdjango
```

### 2. Bootstrap the machine

```bash
make bootstrap-db
```

`make bootstrap-db` is `bash scripts/dev_bootstrap.sh --with-postgres`.

!!! note "`make bootstrap --with-postgres` does not work"
`make` consumes the words after a target name and dies with
`unrecognized option '--with-postgres'`. The optional stages have their own
targets: `make bootstrap` (build only), **`make bootstrap-db`** (build +
PostgreSQL), `make bootstrap-bench` (build + database + benchmark tooling).
Or call the script directly:
`bash scripts/dev_bootstrap.sh --with-postgres`.

Use plain `make bootstrap` if you already run PostgreSQL and don't want the
script touching it. The script is **idempotent and self-healing** — re-run it
any time; every step checks before it acts, and the database stage is
non-fatal, so a database problem never fails your build. On a machine that
already runs PostgreSQL 18+, the install half is a no-op and only the
provisioning half (role, database, trusted `pgvector`) does any work.

Expect a few minutes the first time: it downloads CPython 3.14t, the Python
dependencies and the Zig toolchain, then compiles the native extension in
`ReleaseFast`.

It ends by running `hyper db doctor`. If that reports a problem, fix the first
`✗` and re-run `uv run hyper db doctor` until it is clean. The build is
finished either way.

### 3. Pick a service

```bash
uv run hyper service list           # every service, its port, what it needs
uv run hyper service info hypernews # one service in full, manual commands included
```

### 4. Run it

```bash
uv run hyper service run hypernews
```

One command takes it from nothing to serving:

1. verifies the native extension is built (it never builds behind your back),
2. gives the service its own `hyper_service_<name>` database — `hyper setup`
   creates the database if it does not exist,
3. generates any signing secrets it needs and **persists** them to
   `services/<name>/.env.local` so the seed and the server sign with identical
   keys (a mismatch there is the single most confusing first-run failure),
4. creates the tables and runs the seed,
5. starts companion services **first**, wiring their URLs and seed-minted
   tokens into the dependent service,
6. probes the running app and prints every URL that answers,
7. supervises everything and shuts the whole set down on Ctrl-C or `SIGTERM`.

Useful flags: `--port N` (bind elsewhere), `--no-seed` (tables only),
`--fresh` (drop and recreate — **destructive**).

### 5. Open it

The command prints a block like this — the URLs are **probed, not declared**,
so what you see is what the app actually serves:

```
==================================================================
  hypernews is running
==================================================================

  hypernews  (main app)  http://127.0.0.1:8612
      http://127.0.0.1:8612/                 app root
      http://127.0.0.1:8612/health           liveness probe
      http://127.0.0.1:8612/ready            readiness probe
      http://127.0.0.1:8612/admin/           HyperAdmin panel
      log: /path/to/.hyper.service.hypernews.8612.log

  Demo credentials (same for every seeded user):
      ADMIN_PASSWORD   <generated>
      SEED_PASSWORD    <generated>
      admin panel: user 'admin' + HYPER_ADMIN_PASSWORD above

  Generated secrets persisted to:
      /path/to/services/hypernews/.env.local

  Press Ctrl-C to stop everything.
==================================================================
```

A service with companions prints one block per process — running `hypersecret`
prints `hypermanager (companion)` and `hypersecret (main app)`, each with its
own URLs, plus the identity tokens each seed minted.

Stop it with Ctrl-C, or from another terminal:

```bash
uv run hyper service stop hypernews   # stops the service and its companions
```

---

## What you can do once it is running

- **App root** (`/`) — the service's own UI or API index.
- **HyperAdmin** (`/admin/`) — auto-generated CRUD over every registered model,
  plus the RBAC management screens. Log in as `admin` with the printed
  `ADMIN_PASSWORD`. See [Admin](admin.md).
- **Swagger UI** (`/docs`) and the raw spec (`/openapi.json`) on services that
  mount them (`bookstore_api`, `rest_api`, `task_queue`, …). See
  [OpenAPI](openapi.md).
- **Health probes** (`/health` liveness, `/ready` readiness) where the service
  mounts them — the same endpoints a load balancer or systemd unit would use.
  Every service also answers the framework's internal `/_ready`, which is what
  `hyper service run` polls before declaring the service up.
- **Demo credentials.** No service ships a hardcoded password. `hyper service
run` pins `HYPER_SEED_PASSWORD` and `HYPER_ADMIN_PASSWORD` to generated,
  _stable_ values (persisted in `services/<name>/.env.local`) and prints them.
  Seeds resolve them through `seed_password()` / `ensure_admin_user()` — see
  [Bundled Services → Seed Credentials](services.md#seed-credentials-dynamic-by-default).
  Want your own? Export them before running:

  ```bash
  HYPER_SEED_PASSWORD=devpw HYPER_ADMIN_PASSWORD=devpw \
    uv run hyper service run hypernews
  ```

  An exported value always wins over the persisted one.

- **Logs.** Each service writes to `.hyper.service.<name>.<port>.log` in the
  repo root; the path is printed with the URLs.

### Pointing a service at your own database

`hyper service run` derives each service's database from your ambient
connection URL, replacing only the database _name_ with
`hyper_service_<name>` so two services never share a schema:

```bash
DATABASE_URL=postgres://user:pw@db.internal:5432/ignored \
  uv run hyper service run bookstore_api
# → connects to db.internal:5432/hyper_service_bookstore_api
```

`HYPER_DATABASE_URL`, `DATABASE_URL`, a repo-root `.env` file and the libpq
`PG*` variables are all honoured — see
[Settings → DATABASE_URL](settings.md). With nothing set at all it falls back
to `postgres://localhost/hyper_service_<name>`.

The role you connect as needs `CREATEDB` (the database is created on first
run). `make bootstrap-db` grants that; `uv run hyper doctor --category
database` verifies it.

---

## The services

<!-- BEGIN generated services table -->
<!-- regenerate: uv run python scripts/gen_services_table.py --write -->

| Service           | Port | Needs                                                    | Demonstrates                                                                                                              |
| ----------------- | ---- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `benchmark_app`   | 8601 | —                                                        | Minimal routes for load testing raw Zig HTTP throughput.                                                                  |
| `blog_platform`   | 8602 | database                                                 | Multi-language blog: XML sitemaps, RSS/Atom feeds, i18n.                                                                  |
| `bookstore_api`   | 8603 | database                                                 | Full REST API: ModelViewSet, serializers, pagination, filtering, caching, nested routers.                                 |
| `cms_lite`        | 8604 | database                                                 | Lightweight CMS: URL redirects and flat pages.                                                                            |
| `content_hub`     | 8605 | database                                                 | CMS with Q objects, OneToOneField, single-table inheritance, HyperAdmin custom actions.                                   |
| `deployment`      | 8606 | database                                                 | Production deployment reference: systemd, nginx, health probes, env-based config.                                         |
| `forms_demo`      | 8607 | database                                                 | Form + ModelForm validation, cross-field clean(), file uploads, server-rendered HTML.                                     |
| `full_stack`      | 8608 | database                                                 | Reference scaffold: project/task manager with session auth, templates, CRUD, JSON API, HyperAdmin.                        |
| `hello`           | 8609 | —                                                        | The simplest app — two routes, no database, no middleware.                                                                |
| `hyperai`         | 8610 | database                                                 | AI chat service: SSE streaming, API keys, tiered rate limits, OpenAI-compatible endpoint.                                 |
| `hypermanager`    | 8611 | database<br>3 generated secrets                          | Change-notification hub: producers publish metadata-only change records, subscribers watch a live feed.                   |
| `hypernews`       | 8612 | database                                                 | Community platform: multi-forum, threaded comments, karma voting, eigenvector ring detection, HTMX.                       |
| `hypersecret`     | 8613 | database<br>3 generated secrets<br>starts `hypermanager` | Self-hosted secret manager: envelope encryption, service identities, live rotation nudges via HyperManager.               |
| `hyperticket`     | 8614 | database                                                 | Multi-tenant SaaS ticketing: tenancy, guards, dual auth, HTMX, background tasks, metering, SLA.                           |
| `live_config`     | 8615 | —<br>starts `hypersecret`, `hypermanager`                | Three-service mesh: a storefront converges on a rotated key live, no restart (HyperSecret -> HyperManager -> Storefront). |
| `metering_api`    | 8616 | database                                                 | LLM-style API with usage metering, quota enforcement, IETF RateLimit headers.                                             |
| `multi_tenant`    | 8617 | database                                                 | Project-management SaaS: TenantMixin isolation, header-based tenant resolution, cross-tenant admin.                       |
| `notes_api`       | 8618 | database                                                 | Intermediate service (~170 lines): session auth, cursor pagination, F-expression updates, FTS, HyperAdmin.                |
| `rest_api`        | 8619 | database                                                 | Blog REST API: CRUD, session auth, API-key auth, CORS, OpenAPI docs.                                                      |
| `semantic_search` | 8620 | database<br>YOU supply EMBEDDINGS_API_KEY                | pgvector nearest-neighbour search with HNSW cosine indexing over an OpenAI-compatible embeddings API.                     |
| `task_queue`      | 8621 | database                                                 | Background tasks: priorities, retry with backoff, dead-letter queue, TaskGroup, cron.                                     |
| `websocket_chat`  | 8622 | database                                                 | Real-time chat on the native Zig RFC 6455 server: rooms, presence, channel pub/sub, LiveQuery.                            |

Ports live in the 8600-8699 block, deliberately disjoint from the test suite's reserved ports.

<!-- END generated services table -->

That table is **generated from `hyperdjango/services_registry.py`**, the one
place every port, secret and companion is declared. Regenerate it after
changing the registry:

```bash
uv run python scripts/gen_services_table.py --write   # rewrite this page
uv run python scripts/gen_services_table.py --check   # fail if it has drifted
```

The same facts are available live from `uv run hyper service list`.

### Where to start reading

- **`hypernews`** — the densest single-app tour: multi-forum community with
  threaded comments, karma voting with eigenvector ring detection, keyset
  pagination, HTMX partials and `StatusTimelineMixin` instead of boolean
  `is_banned` flags. Read `services/hypernews/models.py` to see status-as-events
  and `Meta.indexes` used seriously.
- **`hypersecret` + `hypermanager`** — the flagship _pair_, and the reason
  companions exist. HyperSecret is an envelope-encrypting secret manager;
  HyperManager is a change-notification hub. Running `hypersecret` starts
  `hypermanager` first, then hands the dependent service a producer identity
  token that HyperManager's **seed minted**, plus the URL HyperManager actually
  bound. That is a real out-of-band credential handoff, performed by the tooling
  instead of by a paragraph of README instructions.
- **`hyperticket`** — the biggest surface: multi-tenant SaaS ticketing with
  27+ models, `HyperGuard` access control, dual auth (session + API key),
  background tasks, metering and SLA tracking. Read it for how the pieces
  compose at scale.
- **`bookstore_api`** — the REST reference: `ModelViewSet`, serializers,
  cursor pagination, ETag caching, nested routers, telemetry and a browsable
  Swagger UI at `/docs`.

---

## What `hyper service run` does, step by step

Nothing here is magic, and you can do all of it by hand. `uv run hyper service
info <name>` prints the exact commands for the service you name — including the
companion wiring. For `hypernews`:

```bash
# 1. Native extension (no fallbacks — the framework refuses to run without it)
uv run hyper-build --release

# 2. Tables + seed data, into this service's own database
DATABASE_URL=postgres://localhost/hyper_service_hypernews \
  uv run hyper setup --app services.hypernews.app:app --drop \
                     --seed services.hypernews.seed:run

# 3. Serve it
DATABASE_URL=postgres://localhost/hyper_service_hypernews \
  uv run hyper start --app services.hypernews.app:app --port 8612

# 4. Stop it
uv run hyper stop --port 8612
```

A service with secrets adds two rules, and getting either wrong is the source
of most "it seeded fine but the server rejects everything" reports:

1. **The same secret values must reach the setup process and the server.**
   Seeded identity tokens are signed during `hyper setup` and verified by the
   running server.
2. **Sibling services reuse the same variable names with different values**
   (`HYPER_SECRET_KEY` and friends), so each service's `.env.local` must be
   loaded in its _own_ shell — sourcing both files into one shell silently
   gives one of the services the other's keys.

Run each service in a subshell, companions first, passing the registry's extra
environment (it is what redirects the seed's minted tokens to the path the next
service reads them from) plus the companion's URL and token:

```bash
( set -a; . services/hypermanager/.env.local; set +a
  DATABASE_URL=postgres://localhost/hyper_service_hypermanager \
  HYPERMANAGER_DEMO_DIR=$PWD/services/hypermanager/.runtime/manager_demo \
    uv run hyper setup --app services.hypermanager.app:app --drop \
                       --seed services.hypermanager.seed:run
  DATABASE_URL=postgres://localhost/hyper_service_hypermanager \
  HYPERMANAGER_DEMO_DIR=$PWD/services/hypermanager/.runtime/manager_demo \
    uv run hyper start --app services.hypermanager.app:app --port 8611 )

( set -a; . services/hypersecret/.env.local; set +a
  DATABASE_URL=postgres://localhost/hyper_service_hypersecret \
  HYPERSECRET_DEMO_DIR=$PWD/services/hypersecret/.runtime/secret_demo \
    uv run hyper setup --app services.hypersecret.app:app --drop \
                       --seed services.hypersecret.seed:run
  DATABASE_URL=postgres://localhost/hyper_service_hypersecret \
  HYPERSECRET_DEMO_DIR=$PWD/services/hypersecret/.runtime/secret_demo \
  HYPERSECRET_MANAGER_URL=http://127.0.0.1:8611 \
  HYPERSECRET_MANAGER_TOKEN=$(jq -r '."producer:hypersecret"' \
    services/hypermanager/.runtime/manager_demo/tokens.json) \
    uv run hyper start --app services.hypersecret.app:app --port 8613 )

uv run hyper stop --port 8613 && uv run hyper stop --port 8611
```

`hyper service info hypersecret` prints exactly this sequence with your absolute
paths filled in. `.env.local` only exists once `hyper service run` has generated
it — the automated path is what mints those secrets in the first place.

---

## Running a service permanently: systemd units

`hyper service run` is a foreground supervisor: close the terminal and the
service stops. To have a service survive logout, crashes and reboots, install
it as a systemd unit:

```bash
sudo -E uv run hyper service install hypersecret --enable --start
```

`-E` keeps your `DATABASE_URL` across `sudo`; if your sudoers configuration
refuses it, pass the value explicitly instead:

```bash
sudo env DATABASE_URL="$DATABASE_URL" uv run hyper service install hypersecret \
     --enable --start
```

Every field of the generated unit comes from the registry — description, port,
`ExecStart`, working directory, the companion graph and the environment. There
is nothing to hand-edit, and nothing that can drift from the code.

### Look before you install

`--dry-run` prints the unit files, the environment files (with secret values
redacted) and the exact `systemctl` commands, and writes nothing anywhere:

```bash
uv run hyper service install hypersecret --dry-run --enable --start
```

Useful flags:

| Flag            | Effect                                                                   |
| --------------- | ------------------------------------------------------------------------ |
| `--enable`      | Enable the units so they start at boot                                   |
| `--start`       | Start them now                                                           |
| `--port N`      | Override the registry port for the named service                         |
| `--host H`      | Bind address in `ExecStart` (default `127.0.0.1` — put nginx in front)   |
| `--run-as USER` | Account the unit runs as (default: the user who ran `sudo`)              |
| `--user`        | Install a `systemctl --user` unit instead; no root needed                |
| `--dry-run`     | Print everything, change nothing                                         |
| `--no-setup`    | Skip `hyper setup`; assume the database already has schema and seed data |

By default `install` runs `hyper setup` (+ seed) first, for the service **and
its companions**, with the same environment the unit will carry. A unit that
boots against a schema-less database would just crash-loop, and a companion's
seed is what mints the identity tokens the dependent service's environment file
needs.

### What gets installed

```
/etc/systemd/system/hyperdjango-<service>.service   0644, one per service + companion
/etc/systemd/system/hyperdjango.target              0644, the grouping handle
/etc/hyperdjango/hyperdjango-<service>.env          0600, the service's environment
```

The environment file is built from the service's **existing**
`services/<name>/.env.local` — the same persisted secrets `hyper service run`
uses. It is never a fresh set: the identity tokens a seed minted are signed with
those keys, and regenerating them would leave every seeded token permanently
unverifiable. It never lands in the unit file, because `/etc/systemd/system` is
world-readable and the secrets file is 0600.

### The dependency and ordering model

For a service with companions (`hypersecret` needs `hypermanager`) the units
carry the relationship in **both** directions:

```ini
# hyperdjango-hypersecret.service  (the parent)
Requires=hyperdjango-hypermanager.service
After=hyperdjango-hypermanager.service

# hyperdjango-hypermanager.service (the companion)
PartOf=hyperdjango-hypersecret.service
```

What each directive actually guarantees:

- **`After=`** orders _start jobs_ only. It says "do not start me until that
  unit's start job has finished". It says nothing about readiness, and on its
  own it has no effect on stopping.
- **`Requires=`** pulls the companion in when the parent starts, and propagates
  in one direction: if the companion stops or fails, the parent is stopped too.
  It does **not** make stopping the parent stop the companion.
- **`PartOf=`** is that missing direction, and it belongs on the _dependent_
  unit pointing at what owns it: stopping or restarting `hyperdjango-hypersecret`
  stops or restarts `hyperdjango-hypermanager` with it. It does not affect
  start ordering at all.

Both are needed. Together they give the behaviour you expect:

```bash
sudo systemctl start   hyperdjango-hypersecret   # hypermanager comes up first
sudo systemctl stop    hyperdjango-hypersecret   # hypermanager goes down too
sudo systemctl restart hyperdjango-hypersecret   # both come back
```

A companion shared by two installed parents is `PartOf=` **both**, and
`hyper service uninstall` keeps it as long as another installed service still
declares it.

`After=` ordering is not readiness, so a service must tolerate a companion that
is not listening yet. HyperSecret does: its change notifications are written to
an outbox and posted to HyperManager from there, so nothing is lost if the hub
is a second behind.

### Why `After=postgresql.service` is not enough

Three separate problems, all handled at install time:

1. **`After=` is not readiness.** It orders start jobs, and on Debian/Ubuntu
   the unit you would name is a no-op:

   ```console
   $ systemctl cat postgresql.service
   [Service]
   Type=oneshot
   ExecStart=/bin/true
   RemainAfterExit=on
   ```

   It is a meta unit that pulls in the per-cluster
   `postgresql@<version>-main.service` instances — and those are `Type=forking`,
   so systemd calls them started the moment the parent forks, not when the
   server accepts connections. Every database-backed unit therefore also gets a
   real gate:

   ```ini
   ExecStartPre=/usr/bin/pg_isready --host=127.0.0.1 --port=5432 --timeout=5
   ```

   with the host and port taken from that service's own `DATABASE_URL`. If the
   gate fails the unit fails, and:

   ```ini
   Restart=on-failure
   RestartSec=5
   StartLimitIntervalSec=600
   StartLimitBurst=30
   ```

   retries it — 30 attempts over ten minutes — so a boot-time race heals itself
   instead of tripping systemd's start limit and giving up.

2. **The unit is not always called `postgresql.service`.** Debian and Ubuntu
   also ship per-cluster instances (`postgresql@18-main.service`). The installer
   reads `systemctl list-unit-files` and picks what is actually there,
   preferring the meta-unit, then the highest-numbered cluster instance. It
   never picks the `postgresql@.service` _template_, which has no instance to
   start and would make the unit fail to load.

3. **A remote database has no local unit.** When `DATABASE_URL` points at
   another host, no `After=`/`Wants=` is emitted at all and the `pg_isready`
   gate carries the whole dependency. Services the registry marks
   `needs_database=False` (`hello`, `benchmark_app`) get no PostgreSQL
   dependency, no gate and no `DATABASE_URL`.

`hyper service install --dry-run` prints the decision it made and why, per unit.

### Reload vs restart — these units are restart-only

**There is no `ExecReload`, deliberately.** The native Zig server installs
signal handlers for SIGTERM and SIGINT only (graceful shutdown with connection
draining); there is no SIGHUP handler and no configuration-reload path anywhere
in the framework. An `ExecReload=/bin/kill -HUP $MAINPID` would therefore either
do nothing or kill the process outright on SIGHUP's default disposition, while
`systemctl reload` reported success. Omitting it means:

```console
$ sudo systemctl reload hyperdjango-hypersecret
Failed to reload hyperdjango-hypersecret.service: Job type reload is not
applicable for unit hyperdjango-hypersecret.service.
```

which is the truth. To pick up new configuration, restart:

```bash
sudo systemctl restart hyperdjango-hypersecret     # one service (+ its companions)
sudo systemctl restart hyperdjango.target          # everything installed
```

A restart is graceful: systemd sends SIGTERM, the server stops accepting new
connections, drains in-flight requests and exits (`TimeoutStopSec=30`).

### Running several services on one host

Nothing about the units is global. Each gets its own unit name, its own registry
port, its own database, its own environment file and its own
`SyslogIdentifier`, so journald keeps their logs apart:

```bash
sudo uv run hyper service install bookstore_api --enable --start
sudo uv run hyper service install hypernews     --enable --start

sudo journalctl -u hyperdjango-bookstore_api -f       # one service
sudo journalctl -t hyperdjango-hypernews --since -1h  # by syslog identifier
sudo journalctl -u 'hyperdjango-*' -f                 # all of them
```

`hyperdjango.target` is the handle for the whole set. Every unit is
`PartOf=hyperdjango.target` and `WantedBy=hyperdjango.target multi-user.target`,
so each is independently enabled for boot _and_ the target moves them together:

```bash
sudo systemctl stop  hyperdjango.target     # stops every installed service
sudo systemctl start hyperdjango.target     # brings them all back
systemctl list-units 'hyperdjango-*'        # what is installed and running
```

### `--user` mode (no root)

`--user` installs into `~/.config/systemd/user` with secrets in
`~/.config/hyperdjango`, and drops `User=`/`Group=` (a `--user` manager rejects
them). Use it on a machine where you do not have root, or to keep a service
scoped to one account:

```bash
uv run hyper service install bookstore_api --user --enable --start
systemctl --user status hyperdjango-bookstore_api
journalctl --user -u hyperdjango-bookstore_api -f
```

Two caveats worth knowing before you choose it:

- units are `WantedBy=default.target`, not `multi-user.target` — a user manager
  has no `multi-user.target`;
- a user manager normally starts at login and stops at logout. For a service
  that must survive logout and start at boot, enable lingering once:
  `sudo loginctl enable-linger $USER`.

### Uninstalling

```bash
sudo uv run hyper service uninstall hypersecret      # + its companions
sudo uv run hyper service uninstall hypersecret --dry-run
uv run hyper service uninstall bookstore_api --user  # for --user installs
```

This stops and disables the units, removes both the unit files and the 0600
environment files, drops `hyperdjango.target` once the last unit is gone, and
removes `/etc/hyperdjango` when it is empty. A companion still required by
another installed service is kept, and reported. Databases and
`services/<name>/.env.local` are **not** touched — see [Cleaning
up](#cleaning-up) for those.

### When install refuses

- **Not Linux.** systemd is a Linux concept; there is no macOS/BSD equivalent to
  install into. The error says so and points at `--dry-run` (which works
  everywhere) and `hyper service run`.
- **Not root, no `--user`.** Writing `/etc/systemd/system` needs root. The error
  gives you the `sudo` line, the `--user` alternative and the `--dry-run`
  alternative.
- **PostgreSQL unreachable.** Install runs `hyper setup`, which needs the
  database server up. Diagnose with `uv run hyper doctor --category database`.
- **A secret only you can supply** (`semantic_search` needs
  `EMBEDDINGS_API_KEY`). Export it or add it to the service's `.env.local`; no
  random value can stand in for an external credential.

---

## Troubleshooting

Run the broad diagnostic first — it covers most of what follows, with
remediation text attached to each failure:

```bash
uv run hyper doctor                      # everything
uv run hyper doctor --category build     # or: python, database, perf, config,
                                         #     filesystem, security
uv run hyper doctor --json               # machine-readable, for CI
uv run hyper db doctor                   # layered database diagnosis
uv run hyper service info <name>         # per-service specifics
```

### Native extension missing or stale

```
native extension is not built — HyperDjango has no fallback path.
```

`hyper service run` gates on the import and refuses to start rather than
spending minutes compiling behind your back.

```bash
uv run hyper-build --release       # build + install + codesign (macOS)
uv run hyper doctor --category build
```

A _stale_ extension shows up as an `ImportError` naming a missing symbol, or as
behaviour that does not match the source. Rebuild; if that does not clear it,
`make clean && uv run hyper-build --release`.

### Zig not found

The bootstrap downloads a pinned, SHA-verified Zig into `.toolchain/` when no
usable one is on `PATH`, and `hyper-build` looks there — so **do not install
Zig by hand first**. If you are on a platform with no pinned download the
bootstrap says so explicitly; install Zig ≥ 0.16 from
<https://ziglang.org/download/> and re-run. Point at a specific toolchain with
`HYPER_ZIG=/path/to/zig`.

`uv run hyper doctor --category build` reporting `SKIP  Zig compiler not found
in PATH` is **expected** after a bootstrap-downloaded toolchain — the compiler
lives in `.toolchain/`, which `hyper-build` searches but `PATH` does not. What
matters in that category is `PASS  Native extension loaded`.

### Wrong Python (not 3.14t, or the GIL is enabled)

The native extension is built against the free-threaded ABI
(`Py_GIL_DISABLED`); a standard CPython cannot load it. A `.venv` created
before the `3.14t` pin keeps the wrong interpreter forever.

```bash
make bootstrap                            # detects and RECREATES the bad .venv
uv run hyper doctor --category python
```

### PostgreSQL not running / not accepting connections

```
PostgreSQL is not accepting connections at localhost:5432
```

`hyper service run` TCP-preflights the server so the error names the server,
not the DDL.

```bash
pg_isready                                # is it up at all?
sudo systemctl start postgresql           # Ubuntu
brew services start postgresql@18         # macOS
make bootstrap-db                         # install + provision (Ubuntu)
uv run hyper doctor --category database
```

### Role or database missing, or the wrong permissions

Symptoms: `role "<you>" does not exist`, `permission denied to create
database`, `permission denied to create extension "vector"`.

`make bootstrap-db` provisions all three (role with `CREATEDB`, the
`hyperdjango_test` database, `pgvector` marked trusted). Without sudo it prints
the SQL to run yourself:

```sql
CREATE ROLE "<youruser>" LOGIN CREATEDB;
CREATE DATABASE hyperdjango_test OWNER "<youruser>";
-- and append `trusted = true` to vector.control in the PG extension directory
```

`uv run hyper db doctor` diagnoses connectivity, TCP auth, the target database,
`CREATEDB` and extensions as separate layers.

### Port already in use

```
port 8612 is already in use, so hypernews cannot bind it.
```

This check exists specifically to catch a **stale server from a previous run**:
without it, the readiness poll is satisfied by the stranger already on the
port, the launch looks successful, and the process you just started dies
unnoticed with `EADDRINUSE`.

```bash
uv run hyper service stop hypernews     # this service and its companions
uv run hyper stop --port 8612           # any HyperDjango server on that port
uv run hyper service run hypernews --port 8712   # or just move
lsof -nP -iTCP:8612 -sTCP:LISTEN        # who is it, really
```

### Required secrets missing, or unstable between setup and start

Services that call `require_setting(..., min_length=32)` fail closed at import
time. `hyper service run` resolves each declared secret **before anything
starts**, minting and persisting a stable value in `services/<name>/.env.local`
so `hyper setup` and the server process sign with the same key. A key that
changed between the two produces "invalid token" errors against data that was
seeded correctly.

- Deleting `.env.local` invalidates everything seeded with the old keys —
  re-run with `--fresh` after deleting it.
- Secrets **only you can supply** (an external service credential such as
  `EMBEDDINGS_API_KEY` for `semantic_search`) are reported and the run stops.
  No random value can work, so none is invented. Export it, or add it to
  `services/<name>/.env.local`.
- `uv run hyper service info <name>` lists every secret, its minimum length and
  whether it is generated or yours to provide.

### `benchmark-comparison` / dev dependency group not synced

`uv sync --group dev` **removes** packages from other groups. The benchmark
suite's comparison frameworks (FastAPI/uvicorn, Flask/gunicorn), `psutil` and
`plotly` live in the optional `benchmark-comparison` group, so a plain dev sync
silently drops comparison cells and the report render dies at the very end on
the missing `plotly` import.

```bash
uv sync --group dev --group benchmark-comparison   # both groups, together
make bootstrap-bench                               # does this plus wrk + sysctls
```

### macOS vs Ubuntu

|                    | Ubuntu                                                           | macOS                                                                                                               |
| ------------------ | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| PostgreSQL install | `make bootstrap-db` installs PG 18 + pgvector from the PGDG repo | Install it yourself (`brew install postgresql@18 pgvector`); the install stage prints that it is apt-only and skips |
| Role provisioning  | Runs via `sudo -u postgres`                                      | Runs as you if your login role already has superuser access (Homebrew's default)                                    |
| Native extension   | `.so`, no signing step                                           | `.dylib`/`.so` **codesigned** by `hyper-build` — build through `hyper-build`, never by copying a binary around      |
| Benchmark tooling  | `wrk`, `numactl`, kernel sysctls applied                         | sysctls skipped; install `wrk` yourself                                                                             |
| Zig                | Pinned x86_64/aarch64 download                                   | Pinned aarch64 download (Apple Silicon); Intel Macs need a manual install                                           |

---

## Cleaning up

```bash
uv run hyper service stop <name>      # stop the service and its companions
rm -rf services/<name>/.runtime       # minted demo tokens, KEKs, run state
rm -f  services/<name>/.env.local     # generated secrets (invalidates seeded data)
dropdb hyper_service_<name>           # the service's database
```

All of those paths are gitignored; none of them is shared with the test suite,
which creates its own isolated database per test.
