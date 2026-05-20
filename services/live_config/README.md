# Live Config — a three-service composition

Centralized secret/config distribution with **live invalidation**. A consumer
service (the **Storefront**) loads its runtime API keys from **HyperSecret** on
startup, caches them in memory, and subscribes to **HyperManager**'s change
feed. When an operator rotates a key in HyperSecret, the rotation propagates —
HyperSecret → HyperManager → Storefront — and the consumer converges on the new
value **with no restart and no polling loop**. Design rationale and the
credential model: [ARCHITECTURE.md](ARCHITECTURE.md).

This service does not fork or modify HyperSecret or HyperManager. It stands them
up through their own CLIs and talks to them purely over their HTTP + change-feed
APIs (through the published `SecretsClient`), then adds one new consuming app.

## The three services

| Service          | Port | Role                                                        | Credential it holds                                                                      |
| ---------------- | ---- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| **HyperSecret**  | 8960 | Secret store (ciphertext only); producer of change nudges   | `producer:hypersecret` → PUBLISH `secrets/`                                              |
| **HyperManager** | 8970 | Change-notification hub (default live in-memory tier)       | —                                                                                        |
| **Storefront**   | 8980 | Consumer: reads `prod/api`, watches the feed, serves config | `service:prod-api` → READ `prod/api`; `service:platform-api` → SUBSCRIBE `secrets/prod/` |

The Storefront's identities are **read-only** and **subscribe-only** — least
privilege. It can never write a secret or publish a change.

### Keys it manages (namespace `prod/api`)

| Key              | Class      | Behavior on the Storefront                            |
| ---------------- | ---------- | ----------------------------------------------------- |
| `stripe_pk_live` | public     | Publishable Stripe key — shown in full                |
| `maps_api_key`   | public     | Browser maps key — shown in full                      |
| `analytics_key`  | public     | Analytics client key — shown in full                  |
| `webhook_secret` | **secret** | Webhook signing secret — **used but never displayed** |

The secret-classified key is used through `secret_bytes()` (a wipeable
bytearray, zeroized after use) and shown only as a `sha256:` fingerprint. Its
plaintext never appears on any endpoint or in any log.

## Prerequisites

- PostgreSQL running locally (the mesh creates two throwaway databases:
  `live_config_hypersecret` and `live_config_hypermanager`).
- This repository, with `uv` (Python 3.14 free-threaded).

## Run the mesh (one command)

```bash
uv run python -m services.live_config.run_mesh
```

This creates the databases, seeds both upstream services, provisions the demo
keys into `prod/api`, wires each service's credentials, launches all three, and
waits for every `/ready`. It then prints a connection map and next steps and
stays up until **Ctrl-C** (which tears the whole mesh down cleanly).

All state is isolated: the two demo databases plus a gitignored
`services/live_config/.runtime/` directory holding each service's seeded tokens
and namespace KEK files, plus server logs (`.runtime/logs/`).

Ports and the database prefix are overridable via env:
`LIVE_CONFIG_HS_PORT` / `_HM_PORT` / `_SF_PORT` (default 8960/8970/8980) and
`LIVE_CONFIG_DB_PREFIX` (default `postgres://localhost/`).

Each service is pointed at its database with a **single** `DATABASE_URL` — the
framework resolves that one variable to the same connection for both the CLI
(setup/seed) and the running server, so they always agree. (Setting
`HYPER_DATABASE_URL` or the libpq `PG*` set instead would work identically; you
never need to set more than one.)

## See the live config

```bash
curl -s http://127.0.0.1:8980/config | python -m json.tool
```

Or open the human-readable status page at <http://127.0.0.1:8980/>. Each key
shows its version, value (or mask), freshness, and whether the change feed is
connected.

`feed_connected` is read from the watcher's own socket state
(`ChangeFeedWatcher.connected`), not tracked locally, so it goes **false the
moment the hub is lost** — the window in which values keep being served but
stop being invalidated, and can therefore age up to `cache_ttl`. `feed_drops`
counts feed sessions lost since startup: a climbing count means a flapping hub.
Startup waits up to `feed_connect_timeout` (15s) for the first connect so the
reported state is accurate from the first request; if the hub is down the
storefront still comes up on its warmed cache and says so.

Endpoints:

| Method | Path                | Purpose                                                                                      |
| ------ | ------------------- | -------------------------------------------------------------------------------------------- |
| GET    | `/`                 | Human-readable status page                                                                   |
| GET    | `/config`           | JSON: `feed_connected`, `feed_drops`, `{key: {version, value_or_masked, refreshed_at, ...}}` |
| GET    | `/events`           | Server-Sent Events: a snapshot, then a notice per rotation                                   |
| GET    | `/healthz`          | Liveness                                                                                     |
| GET    | `/health`, `/ready` | Framework health probes (`mount_health`)                                                     |

## The live-rotation demo

The money shot — a rotation propagates into the running consumer. In a second
terminal, with the mesh up:

```bash
uv run python -m services.live_config.demo
```

It reads the Storefront's current config (noting `stripe_pk_live`'s version),
rotates that key in HyperSecret through the operator CLI, then polls the
Storefront until the version increments — proving the change flowed through the
feed into the running consumer. It prints a before/after and confirms the
secret-classified key is never exposed.

Do it by hand instead:

```bash
export HYPERSECRET_URL=http://127.0.0.1:8960
export HYPERSECRET_TOKEN=$(python -c "import json;print(json.load(open('services/live_config/.runtime/secret_demo/tokens.json'))['operator:admin'])")
KEK=services/live_config/.runtime/secret_demo/prod-api.kek

# note the current version
curl -s http://127.0.0.1:8980/config | python -c "import sys,json;print(json.load(sys.stdin)['keys']['stripe_pk_live']['version'])"

# rotate (a put is also a rotation)
uv run python -m services.hypersecret.provision \
    put prod/api stripe_pk_live --kek-file "$KEK" --value pk_live_ROTATED_v2

# re-read — the version has incremented and the value changed, no restart
curl -s http://127.0.0.1:8980/config | python -m json.tool
```

What to observe: the Storefront never restarts, yet `stripe_pk_live` shows the
new version and value within a moment of the rotation. HyperSecret pushed a
metadata-only nudge to HyperManager; the Storefront's watcher invalidated the
cached key and lazily re-fetched. The `webhook_secret` mask never changes to
plaintext.

## Clean teardown

Press **Ctrl-C** in the `run_mesh` terminal — it stops all three services (their
process groups) and drains. To remove all state:

```bash
rm -rf services/live_config/.runtime
dropdb live_config_hypersecret
dropdb live_config_hypermanager
```

## Test

```bash
uv run hyper-test e2e_live_config
```

Boots all three services on isolated ports/database and asserts: the Storefront
loads the provisioned keys at their initial versions; a rotation in HyperSecret
is reflected within a bounded wait (live propagation through the feed); and the
secret-classified key is never exposed in plaintext on any endpoint.

## Layout

```
live_config/
├── app.py           the Storefront: in-memory config cache + feed watcher + endpoints
├── config.py        StorefrontConfig (LIVE_CONFIG_* env / TOML tunables)
├── run_mesh.py      one-command launcher: create DBs, seed, wire, run all three
├── demo.py          scripted live-rotation proof (before/after)
├── README.md        this runbook
└── ARCHITECTURE.md  data flow, credential/scoping model, security posture
```
