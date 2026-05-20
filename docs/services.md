# Bundled Services

22 production-ready services showcasing every major HyperDjango feature. Each is self-contained with `app.py`, `seed.py`, and E2E tests.

## Quick Start

```bash
uv run hyper service list          # every service, its port, what it demonstrates
uv run hyper service run <name>    # set up, seed and serve — including companions
```

Starting from an empty machine? **[Zero to Running: Clone and Run a
Service](running-services.md)** is the full path from `git clone` to an open
browser tab, with prerequisites and troubleshooting.

`hyper service run` reads `hyperdjango/services_registry.py`, the single source
of truth for every service's app path, seed, port, required secrets and
companions. It gives each service its own `hyper_service_<name>` database,
generates and persists any signing secrets it needs, and prints every URL worth
opening. See [CLI → hyper service](cli.md#hyper-service) for the full flow and
the equivalent manual commands (`hyper service info <name>` prints them too).

The **Port** column below is the registry port each service binds under
`hyper service run` — all inside the 8600-8699 block, disjoint from the e2e
suite's reserved test ports.

## Service Catalog

### Beginner

| Service        | Directory     | Showcases                                                         | Port |
| -------------- | ------------- | ----------------------------------------------------------------- | ---- |
| **Hello**      | `hello/`      | Minimal starter — one route, one response                         | 8609 |
| **Notes API**  | `notes_api/`  | Session auth, F expression updates, cursor pagination, HyperAdmin | 8618 |
| **Forms Demo** | `forms_demo/` | Form validation, ModelForm, cross-field clean, file uploads       | 8607 |
| **REST API**   | `rest_api/`   | CRUD, opaque IDs, session + API key auth, OpenAPI/Swagger         | 8619 |

### Intermediate

| Service            | Directory         | Showcases                                                             | Port |
| ------------------ | ----------------- | --------------------------------------------------------------------- | ---- |
| **Full Stack**     | `full_stack/`     | Templates, forms, admin panel, project/task CRUD                      | 8608 |
| **Bookstore API**  | `bookstore_api/`  | ViewSets, serializers, CursorPagination, ETag, nested routers, RBAC   | 8603 |
| **Content Hub**    | `content_hub/`    | Q objects, OneToOneField, STI, custom actions, RBAC roles             | 8605 |
| **WebSocket Chat** | `websocket_chat/` | Zig RFC 6455, rooms, presence, typing indicators, Channel pub/sub     | 8622 |
| **Task Queue**     | `task_queue/`     | @app.task, priorities, retry, DLQ, TaskGroup, cron scheduling         | 8621 |
| **Blog Platform**  | `blog_platform/`  | XML sitemaps, RSS/Atom feeds, i18n (LocaleMiddleware), multi-language | 8602 |
| **CMS Lite**       | `cms_lite/`       | Flat pages (FlatPageRegistry), URL redirects (RedirectRegistry)       | 8604 |
| **Metering API**   | `metering_api/`   | Usage metering (MeterEngine), quotas, IETF RateLimit headers          | 8616 |

### Advanced

| Service              | Directory          | Showcases                                                                                                                                                                                                                                                                                       | Port |
| -------------------- | ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- |
| **HyperNews**        | `hypernews/`       | Voting system, eigenvector ranking, StatusTimeline, keyset pagination                                                                                                                                                                                                                           | 8612 |
| **HyperAI**          | `hyperai/`         | LLM chat, SSE streaming, tiered rate limits, SignedAPIKeyMixin                                                                                                                                                                                                                                  | 8610 |
| **HyperTicket**      | `hyperticket/`     | Multi-tenant SaaS, 27+ models, guard RBAC, SLA tracking, workflows                                                                                                                                                                                                                              | 8614 |
| **HyperSecret**      | `hypersecret/`     | Envelope-encrypted secret manager: client-side crypto, grants, audit, mTLS, live rotation                                                                                                                                                                                                       | 8613 |
| **HyperManager**     | `hypermanager/`    | Infrastructure change-feed hub: live WS pub/sub by default, opt-in durable ledger, prefix grants, mTLS                                                                                                                                                                                          | 8611 |
| **Live Config Mesh** | `live_config/`     | Composition of HyperSecret + HyperManager + a Storefront consumer: fetch API keys on startup, cache in memory, hot-reload live on rotation via the change feed; one-command `run_mesh.py` boots all three isolated with least-privilege credential wiring, plus a scripted rotation walkthrough | 8615 |
| **Multi-Tenant**     | `multi_tenant/`    | TenantMixin, enum fields, org hierarchy, tenant isolation                                                                                                                                                                                                                                       | 8617 |
| **Semantic Search**  | `semantic_search/` | pgvector, HNSW cosine similarity, OpenAI-compatible embeddings API                                                                                                                                                                                                                              | 8620 |

### Infrastructure

| Service        | Directory        | Showcases                                                      | Port |
| -------------- | ---------------- | -------------------------------------------------------------- | ---- |
| **Deployment** | `deployment/`    | systemd, nginx, env template, health probes, production config | 8606 |
| **Benchmark**  | `benchmark_app/` | EXPLAIN ANALYZE performance testing, regression detection      | 8601 |

## Features by Service

| Feature                     | Services                                                                                                                                                                           |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **HyperAdmin**              | All except hello, benchmark, deployment                                                                                                                                            |
| **Swagger UI**              | bookstore_api, rest_api, task_queue, multi_tenant, content_hub, hyperai, notes_api, forms_demo, semantic_search, websocket_chat, full_stack, blog_platform, cms_lite, metering_api |
| **SessionAuth**             | All auth-enabled services                                                                                                                                                          |
| **RBAC Groups**             | bookstore_api, content_hub, full_stack, hypernews, hyperticket                                                                                                                     |
| **XML Sitemaps**            | blog_platform                                                                                                                                                                      |
| **RSS/Atom Feeds**          | blog_platform                                                                                                                                                                      |
| **i18n**                    | blog_platform                                                                                                                                                                      |
| **Flat Pages**              | cms_lite                                                                                                                                                                           |
| **URL Redirects**           | cms_lite                                                                                                                                                                           |
| **Usage Metering**          | metering_api                                                                                                                                                                       |
| **IETF Rate Limit Headers** | metering_api                                                                                                                                                                       |
| **Meta.indexes**            | hypernews (36 indexes), hyperticket (46 indexes)                                                                                                                                   |
| **StatusTimeline**          | hypernews (ban/mute/staff), hyperticket (lock/mute/agent), multi_tenant (org suspend)                                                                                              |
| **Telemetry**               | bookstore_api (metrics + spans), hyperai (span tracing), hypersecret (CounterVec + /metrics)                                                                                       |
| **Envelope Encryption**     | hypersecret (AES-256-GCM client-side, AAD slot binding, KEK rotation)                                                                                                              |
| **SignedAPIKeyMixin**       | hyperai (user API keys), hyperticket (org keys), hypersecret + hypermanager (service identities)                                                                                   |
| **mTLS (client certs)**     | hypersecret + hypermanager (in-process terminator or external proxy; CA + cert issuance CLI)                                                                                       |
| **Live change feed (WS)**   | hypermanager (three tiers: default live pub/sub, in-memory reconnect catch-up, opt-in durable ledger + cursor replay), hypersecret (watch → live rotation convergence)             |
| **WebSocket**               | websocket_chat (native Zig RFC 6455)                                                                                                                                               |
| **CursorPagination**        | bookstore_api, notes_api, rest_api, content_hub                                                                                                                                    |
| **Field Permissions**       | bookstore_api (via field_permissions_model)                                                                                                                                        |

## Seed Credentials — Dynamic by Default

No service ships with hardcoded passwords. Every seed user (and the
HyperAdmin panel user) gets its password from the settings system at seed
time, falling back to a randomly-generated value that is printed to the
startup log.

**Resolution order for app users (via `seed_password("username")`):**

1. `HYPER_SEED_PASSWORD_<USERNAME>` setting (per-user override, e.g. `HYPER_SEED_PASSWORD_ALICE`)
2. `HYPER_SEED_PASSWORD` setting (global fallback for all seed users)
3. `secrets.token_urlsafe(16)` — random, printed to the log so the operator can record it

**Resolution order for the HyperAdmin panel user (via `ensure_admin_user()`):**

1. Explicit `password=` argument
2. `HYPER_ADMIN_PASSWORD` setting
3. `secrets.token_urlsafe(16)` — random, printed to the log

### Running an example with a known password

```bash
HYPER_SEED_PASSWORD=mydevpw HYPER_ADMIN_PASSWORD=mydevpw \
  uv run hyper setup --app services.bookstore_api.app:app --drop \
  --seed services.bookstore_api.seed:run

HYPER_SEED_PASSWORD=mydevpw HYPER_ADMIN_PASSWORD=mydevpw \
  uv run hyper run --app services.bookstore_api.app:app
```

### Letting the seed pick random passwords

```bash
uv run hyper setup --app services.bookstore_api.app:app --drop \
  --seed services.bookstore_api.seed:run
# Look for "Generated seed password for 'admin': <token>" in the startup log.
```

E2E tests set both env vars in `scripts/test_runner.py` so every test run gets a
deterministic, isolated password (`SEED_PASSWORD` / `ADMIN_PASSWORD` constants
in `e2e_helper.py`).
