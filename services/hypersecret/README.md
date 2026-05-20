# HyperSecret

Minimal self-hosted secret manager: services inside your security boundary
authenticate to a small HTTP API and fetch secrets at runtime. The server
stores only ciphertext and wrapped data keys — it **cannot decrypt what it
stores**. Design rationale and trust model: [ARCHITECTURE.md](ARCHITECTURE.md).

## Features

- **Envelope encryption, client-side decryption** — AES-256-GCM payload keys
  wrapped by per-namespace master keys (KEKs) that never touch the server.
  Associated data binds every blob to its exact `namespace/key/version` slot,
  so a substituted blob fails authentication instead of decrypting.
- **Service identities** — HMAC-signed bearer tokens (`hsk_…`) hashed at
  rest, shown once at mint, revocable, scoped (`read`/`write`/`admin`/`audit`).
- **mTLS authentication** — services can authenticate with a client
  certificate (CN = identity) instead of a token, verified end-to-end by the
  in-process TLS terminator or an external proxy; revoking the identity cuts
  off both legs at once.
- **Live change notifications** — writes/rotations/rewraps/deletes/expiries/
  exposures publish metadata-only events to a HyperManager hub, so
  subscribed clients converge on rotated secrets with no restart.
- **Allow-list authorization** — explicit identity→namespace grants, fail
  closed at every gate, reviewable via API and admin panel.
- **Immutable versioning** — every write appends a version with provenance;
  any version fetchable for rollback; optimistic concurrency via 409.
- **KEK rotation** — operator rewraps DEKs under a new master key without
  touching payload ciphertext.
- **Full audit trail** — every access including denials, not-founds, and
  invalid input, batched off the hot path, queryable with filters, and trimmed
  past a retention window so the log stays bounded.
- **Application-transparent injection** — `secrets_run` exec-wrapper and
  systemd `EnvironmentFile` oneshot patterns (units in `deploy/`).
- **Operations** — Prometheus `/metrics`, health probes, HyperAdmin panel,
  OpenAPI docs, soft-delete retention sweep on the task scheduler.

## Setup

Three signing secrets must be set explicitly and stably — the app resolves them
with `require_setting` and **refuses to start** on a missing, blank, or
under-32-character value rather than boot on an auto-generated per-process
default (which would make identity tokens, sessions, and admin logins silently
non-verifiable across restarts and workers):

- `HYPER_SESSION_SIGNING_KEY` — the TokenEngine key that signs identity tokens;
  `hyper setup` and `hyper start` must share it or seed-minted tokens won't
  verify.
- `HYPER_SECRET_KEY` — sessions / CSRF / framework signing.
- `HYPER_ADMIN_SECRET` — the `/admin` panel's session-signing secret.

```bash
export HYPER_SESSION_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export HYPER_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export HYPER_ADMIN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
uv run hyper setup --app services.hypersecret.app:app --seed services.hypersecret.seed:run
uv run hyper start --app services.hypersecret.app:app --port 8960
```

Seeding writes demo credentials (identity tokens + namespace KEK files) to
`services/hypersecret/.demo/` — the demo stand-in for your deployment
pipeline's secret-distribution step. Three namespaces are provisioned:
`prod/api` (a Stripe-style key readable only by `service:prod-api`),
`prod/frontend`, and `staging/api`, each with its own master key.

## Fetching a secret (service runtime)

```python
from services.hypersecret.client import SecretsClient

client = SecretsClient(
    "http://127.0.0.1:8960",
    token="hsk_...",  # this service's identity
    namespace="prod/api",
    kek=kek_bytes,  # this namespace's master key
)

stripe.api_key = client.secret("stripe_key")  # returns a str (ergonomic)

# secret_bytes() is the wipeable path: a bytearray zeroized at block exit —
# secret() returns an immutable str that cannot be wiped, so it is NOT a
# context manager (a with-block there would only pretend to bound lifetime).
with client.secret_bytes("db_password") as pw:
    db = connect(pw)

creds = client.get_secrets(["db_password", "jwt_secret"])  # one round trip
```

The client caches _ciphertext_ (decrypt-per-access keeps plaintext lifetime
minimal) and revalidates with `known_version`, so an unchanged secret costs a
body-free 304. Degradation is fail-closed; pass `stale_max` to explicitly
allow bounded staleness during a server outage.

### Live convergence (no restart on rotation)

Point the client at a HyperManager hub and it invalidates cached secrets the
moment they change, so the next access re-fetches the rotated value:

```python
watcher = client.watch(
    "http://hypermanager.internal:8970",
    manager_token="hmk_...",  # identity with subscribe on secrets/prod/
    on_change=lambda ev: rebuild_pool() if ev["kind"] == "rotated" else None,
)
# rotations, rewraps, deletes, exposures, and expiries all converge live
```

By default the hub is a live pub/sub: it pushes "this subject changed" nudges
and the client refetches on its own (stale-while-revalidate). A nudge drops
exactly that key, so the next access re-fetches — and costs a body-free `304`
via `known_version` when the value did not actually change. On connect the
client full-resyncs (invalidate all, then lazily re-fetch); a brief disconnect
resumes from the hub's per-client catch-up buffer, replaying only the keys
missed while away, and a buffer overrun or hub restart falls back to a full
resync. The values themselves never traverse the hub — only metadata-only
nudges — so a subscriber always reconciles through its own authenticated fetch.

The returned watcher reports its own liveness, which a service should surface
next to its cache: `watcher.connected` (is the live feed up right now?),
`watcher.wait_connected(timeout)` / `watcher.wait_disconnected(timeout)` to
block on a transition, and `watcher.connects` / `watcher.disconnects` to spot a
flapping hub.

```python
watcher.wait_connected(10)  # cache is now known fresh as of this connect
...
if not watcher.connected:  # e.g. in /ready or a metric
    degraded("secret rotations are not being pushed; values may be cache_ttl stale")
```

While the feed is down nothing invalidates the cache, so a rotation can go
unnoticed for up to `cache_ttl` — that is the window a health probe should
report. `connected` flips only after a connect's resync has been applied, so it
also marks the point at which the cache holds nothing older than that connect.

The watcher negotiates the model from the hub's hello frame. A **durable-ledger**
tier is an opt-in hub setting for audited deployments that need ordered,
retained, replayable delivery; `watch()` consumes the default live tier and, if
pointed at a ledger hub, degrades safely to a full resync on each (re)connect
(never stale past a connect) rather than pulling the ledger itself.

Enable the producer side on the server with `HYPERSECRET_MANAGER_URL` +
`HYPERSECRET_MANAGER_TOKEN` (an identity holding a publish grant on
`secrets/`). Publishing goes through a transactional outbox: each change is
written to an `OutboxEvent` row in the same transaction as the secret state
change, and a scheduled drainer posts it to the hub. A slow or down hub never
blocks or fails a secret operation, and a committed change is never lost — it
is retried until it lands.

#### Outbox drainer semantics and parked rows

The drainer posts each pending row idempotently (the row id is the hub dedupe
key, so a crash between the POST and the local delete cannot double-append) and
acts on the outcome:

- **delivered** — the row is deleted.
- **retryable** (hub down, or a 5xx) — the pass stops and the whole backlog
  retries next run, in order.
- **permanent** (a 4xx — bad token, malformed subject) — the row is **parked**
  with the rejection reason and the drainer continues to the next row, so a
  poison event can never head-of-line-block the feed. Parked rows are counted
  in `hypersecret_outbox_parked_total`.

To recover a parked row, inspect it in the HyperAdmin panel (the `OutboxEvent`
list shows `status`, `attempts`, and `error_detail`), fix the underlying cause
(e.g. restore the producer's publish grant on the hub), then **requeue it by
setting `status` back to `pending`** — the next drain retries it.

### mTLS

```python
client = SecretsClient(
    "https://secrets.internal:8443",
    namespace="prod/api",
    kek=kek_bytes,
    ca_file="ca.crt",
    client_cert_file="prod-api.crt",  # CN = service:prod-api, no token needed
    client_key_file="prod-api.key",
)
```

Enable the in-process terminator with `HYPERSECRET_MTLS_LISTEN_PORT` +
`HYPERSECRET_MTLS_CERT_FILE`/`KEY_FILE`/`CA_FILE` (bind the plain HTTP port to
loopback). Issue the CA and certs with the provisioning CLI:

```bash
P=services.hypersecret.provision
uv run python -m $P ca init --dir ca
uv run python -m $P cert issue localhost --server --dns localhost --ca-dir ca --out-prefix tls/server
uv run python -m $P cert issue service:prod-api --ca-dir ca --out-prefix certs/prod-api
```

For an external TLS-terminating proxy instead, see
`deploy/nginx-mtls.conf` (set `HYPER_MTLS_PROXY_SECRET` to match). Behind such a
proxy, also set `HYPER_TRUSTED_PROXY_COUNT=1` (or list the proxy in
`HYPER_TRUSTED_PROXIES`) so the framework reads the real caller from the
proxy-appended `X-Forwarded-For` — otherwise `AccessLog.client_ip` records the
proxy's loopback address and the per-IP rate limit buckets every request into
one. The in-process `MTLSTerminator` needs no such setting: it trusts its own
loopback upstream and forwards `x-real-ip` to `request.client_ip` directly.

## Provisioning (operator/CI)

```bash
export HYPERSECRET_URL=http://127.0.0.1:8960
export HYPERSECRET_TOKEN=$(python -c "import json;print(json.load(open('services/hypersecret/.demo/tokens.json'))['operator:admin'])")

P=services.hypersecret.provision
uv run python -m $P keygen --out payments.kek --kek-id payments-v1
uv run python -m $P namespace create prod/payments --kek-id payments-v1
uv run python -m $P identity create service:payments --scopes read
uv run python -m $P grant service:payments prod/payments --read
echo -n "sk_live_..." | uv run python -m $P put prod/payments stripe_key --kek-file payments.kek
uv run python -m $P audit --namespace prod/payments
```

KEK rotation rewraps every version's data key — including the retained
versions of soft-deleted secrets, so a later revive is still decryptable under
the new KEK — and declares the new generation, without the server ever seeing
key material:

```bash
uv run python -m $P keygen --out payments-v2.kek --kek-id payments-v2
uv run python -m $P rewrap prod/payments --old-kek-file payments.kek --new-kek-file payments-v2.kek
```

> **Cutover window.** `rewrap` re-wraps every DEK under the new KEK and then
> declares the new generation on the namespace. A running service still
> holding the _old_ KEK can no longer decrypt once the new generation is
> declared — and with `watch()` enabled the rewrap event triggers an immediate
> re-fetch that will then fail. **Distribute the new KEK file to every granted
> service before running `rewrap`** (or run it during a maintenance window).
> A future enhancement is dual-KEK reads (accept old or new during a
> transition); today the KEK hand-off must lead the rewrap.
>
> **Resumability & concurrent writes.** `rewrap` is safe to rerun after an
> interruption: a version already on the new KEK is skipped. Pre-repoint passes
> are bounded (a busy namespace keeps sealing new writes under the old KEK, so
> a pure fixpoint could chase them forever); after the bounded passes the
> namespace is repointed to the new KEK — from that commit the server rejects
> any write still sealed under the old one — and a final pass drains the finite
> set of versions that raced the repoint. Every version therefore ends on the
> new KEK without freezing writes; a writer whose `put` races the repoint gets
> a clean rejection and retries under the new generation. Keep the old KEK file
> until you have confirmed every version reads under the new one.

## Injection (systemd / Docker / anywhere)

```ini
# systemd — exec wrapper (values never touch disk)
ExecStart=/usr/bin/python -m services.hypersecret.secrets_run --map /etc/myapp/secrets.map -- /usr/local/bin/myapp
```

```bash
# env-file mode for EnvironmentFile= (0600, oneshot unit in deploy/)
python -m services.hypersecret.secrets_run --keys stripe_key,db_password --output /run/secrets/myapp.env
```

You must say which secrets to inject — `--map <file>`, `--keys a,b,c`, or the
opt-in `--all` (every key in the namespace). There is no inject-everything
default: a service should get only the secrets it needs. Configuration comes
from `HYPERSECRET_URL` / `HYPERSECRET_TOKEN` / `HYPERSECRET_NAMESPACE` /
`HYPERSECRET_KEK_FILE`. Strict by default: a
missing secret aborts the launch instead of starting half-configured.

## Key routes

| Method | Path                                                | Purpose                                             |
| ------ | --------------------------------------------------- | --------------------------------------------------- |
| GET    | `/v1/secrets/{env}/{service}/{key}`                 | Fetch envelope (`?version`, `?known_version` → 304) |
| POST   | `/v1/secrets/{env}/{service}/{key}`                 | Append immutable version (write grant)              |
| GET    | `/v1/secrets/{env}/{service}`                       | List keys (`?include_deleted=1` + admin)            |
| GET    | `/v1/secrets/{env}/{service}/{key}/versions`        | History + provenance                                |
| POST   | `/v1/secrets/{env}/{service}/{key}/rewrap`          | KEK rotation for one version                        |
| POST   | `/v1/secrets/{env}/{service}/{key}/expose`          | Mark exposed/compromised (admin)                    |
| DELETE | `/v1/secrets/{env}/{service}/{key}`                 | Soft delete (`?purge=1` + admin scope)              |
| POST   | `/v1/batch/{env}/{service}`                         | Batch fetch                                         |
| GET    | `/v1/namespaces`                                    | Caller's granted namespaces                         |
| GET    | `/v1/audit`                                         | Audit query (audit scope)                           |
| POST   | `/v1/admin/…`                                       | Namespaces / identities / grants (admin scope)      |
| GET    | `/health` `/ready` `/metrics` `/admin/` `/api/docs` | Ops surface                                         |

## Configuration

App tunables load through `HyperSecretConfig` (`config.py`): override with
`HYPERSECRET_<FIELD>` env vars or a `site.toml` next to the app — e.g.
`HYPERSECRET_GRANT_CACHE_TTL=5`, `HYPERSECRET_RETENTION_DAYS=7`. Framework
settings (`DATABASE_URL`, `SECRET_KEY`, …) use the standard `HYPER_*` vars.

## Metrics (`/metrics`)

`hypersecret_requests_total{action,outcome}`,
`hypersecret_namespace_access_total{namespace,outcome}` (per-namespace access),
`hypersecret_grant_cache_total{result}` (hit/miss),
`hypersecret_notify_posted_total` / `hypersecret_notify_errors_total` /
`hypersecret_outbox_parked_total` (change-notification / outbox health), plus
the framework mTLS terminator gauges/counters.

The metric bodies expose namespace names and per-namespace access volume, so
`/metrics` requires a resolved identity — any valid bearer token or mTLS client
cert, no namespace grant needed. Anonymous scrapes are denied. Give your
scraper a low-privilege identity (a `read`-scoped token with no grants is
enough) and send it as a bearer header:

```yaml
# Prometheus scrape_config
- job_name: hypersecret
  authorization:
    type: Bearer
    credentials: hsk_... # a read-scoped identity token
  static_configs:
    - targets: ["secrets.internal:8960"]
```

## Framework features demonstrated

`SignedAPIKeyMixin` token identities · shared-authority authorization module ·
`unique_together` + JSONB fields · transactions with optimistic concurrency ·
telemetry `CounterVec` + `/metrics` · thread-based `TaskScheduler` for
periodic jobs · HyperAdmin with `Field(exclude=True)` masking · `SiteConfig`
app configuration · `mount_health()` + OpenAPI docs.

## Architecture

```
hypersecret/
├── app.py           server: routes, gate, lifecycle, admin, metrics
├── envelope.py      client-side crypto authority (seal/open/rewrap, KEK files)
├── models.py        Namespace, Secret, SecretVersion, ServiceIdentity,
│                    NamespaceGrant, AccessLog
├── authz.py         identity + scope + grant gate (fail closed, TTL cache)
├── audit.py         amortized batch audit writer
├── client.py        SecretsClient SDK on hyperdjango.serviceclient (fetch,
│                    decrypt, cache, provision, watch → ChangeFeedWatcher)
├── notify.py        outbox change poster (thin ServiceClient; no hub import)
├── secrets_run.py   env-injection wrapper (exec + env-file modes)
├── provision.py     operator CLI (ca/cert/keygen/namespace/identity/grant/
│                    put/get/rewrap/delete/audit; put also rotates)
├── config.py        HyperSecretConfig (env/TOML tunables)
├── seed.py          demo bootstrap (namespaces, identities, sealed secrets)
├── deploy/          systemd units, nginx mTLS config
└── ARCHITECTURE.md  trust model + design rationale
```

Tests: `scripts/test_e2e_hypersecret.py` (full lifecycle on the native
server, incl. ASGI parity) and `scripts/test_hypersecret_fuzz.py`
(property-based envelope + injection-parsing proofs).
