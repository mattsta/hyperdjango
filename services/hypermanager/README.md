# HyperManager

An infrastructure change-notification hub: producers publish metadata-only
change records under hierarchical subjects; subscribers learn that a covered
subject changed and re-pull whatever changed from the producing system on their
own schedule. The hub carries _that something changed_ — never the changed
value. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

HyperSecret is the first producer (secret rotations/expiries/exposures);
anything that manages infrastructure can publish. Services subscribe and
converge live instead of restarting.

## Delivery tiers

A hub runs one of three tiers, chosen at boot by two config selectors. The same
wire protocol and the same client serve all three — the `hello` frame declares
which tier is running.

| `HYPERMANAGER_LEDGER_MODE` | `HYPERMANAGER_CATCH_UP_RING_SIZE` | Tier          | What you get                                                                     |
| -------------------------- | --------------------------------- | ------------- | -------------------------------------------------------------------------------- |
| `0` (default)              | `> 0` (default 1024)              | **catchup**   | Live in-memory pub/sub; a bounded ring lets a brief reconnect replay the misses. |
| `0`                        | `0`                               | **ephemeral** | Live pub/sub, no ring — every (re)connect resyncs. Simplest tier.                |
| `1`                        | (n/a)                             | **ledger**    | Opt-in durable audited log: at-least-once ordered replay + retention.            |

- **Default (catchup / ephemeral)** — a publish assigns an in-memory monotonic
  sequence and pushes the event straight to connected subscribers whose prefixes
  cover the subject; the client refetches the changed state itself. In catchup a
  bounded ring retains recent events so a subscriber that briefly dropped off
  replays exactly what it missed; overrunning the ring (or a restart) resyncs.
  No Postgres on this path. Best-effort by design — a live nudge, not a log.
- **Ledger (`HYPERMANAGER_LEDGER_MODE=1`)** — the opt-in persistent-audit tier:
  an append-only `ChangeEvent` log whose id is the feed cursor is the single
  ordered source of truth. Subscribers pull `replay(after=<cursor>)` in order and
  the live WebSocket carries only content-free wake hints, so ordering and
  **at-least-once** delivery hold by construction under concurrent publishers,
  sharded fan-out, and dropped or coalesced wakes. Adds retention trimming,
  `/v1/events` replay, `/v1/cursor`, and idempotent-publish durability.

## Features (all tiers)

- **Hierarchical subjects + prefix grants** — `secrets/prod/api/stripe_key`;
  identities are granted publish/subscribe on subject prefixes, enforced on every
  delivery (and on replay in ledger mode), fail closed.
- **Two authentication legs** — signed bearer tokens (`hmk_…`, hashed at rest,
  revocable) or network-verified mTLS client certificates (CN = identity),
  through one gate.
- **Idempotent publish** — a publish may carry a dedupe key; a re-POST of the
  same key returns the existing event instead of appending a duplicate. Durable
  and race-safe per producer in ledger mode; best-effort against a bounded
  recent-key set in the in-memory tiers.
- **Cross-replica fan-out** — `PgChannelLayer` (Postgres LISTEN/NOTIFY) when
  `HYPERMANAGER_PG_FANOUT=1`; single-process in-memory otherwise.
- **Ops** — Prometheus `/metrics`, health probes, HyperAdmin, OpenAPI docs, and
  (ledger mode) scheduled retention trimming.

## Setup

Three secrets must be set explicitly — the app **fails closed** (refuses to
boot) rather than run on an auto-generated per-process default:

- `HYPER_SESSION_SIGNING_KEY` — the framework's TokenEngine key that signs
  identity tokens; it must be stable across `hyper setup` and `hyper start`.
- `HYPER_SECRET_KEY` — sessions / CSRF / signing (≥32 chars).
- `HYPER_ADMIN_SECRET` — the HyperAdmin panel session secret (≥32 chars).

```bash
export HYPER_SESSION_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export HYPER_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export HYPER_ADMIN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
uv run hyper setup --app services.hypermanager.app:app --seed services.hypermanager.seed:run
uv run hyper start --app services.hypermanager.app:app --port 8970
```

Seeding writes demo tokens to `services/hypermanager/.demo/tokens.json` and
provisions identities: `producer:hypersecret` (publish `secrets/`),
`service:platform-api` (subscribe `secrets/prod/`), `service:staging-worker`
(subscribe `secrets/staging/`), and `operator:admin`. Identities carry coarse
**scopes** — `feed` for the change-notification API (publish and the live feed;
plus replay/cursor in ledger mode), `admin` for provisioning — with prefix
grants authorizing which subjects on top.

## Subscribing (application server)

```python
from services.hypermanager.client import ManagerClient

mgr = ManagerClient("http://127.0.0.1:8970", token="hmk_...")


def on_change(event):
    # {"subject":"secrets/prod/api/stripe_key", "kind":"rotated", ...}
    reload_dependency(event["subject"])


watcher = mgr.watch(["secrets/prod/"], on_change)  # threaded, tier-agnostic
# ... later ...
watcher.stop()
```

`watch()` returns a framework `ChangeFeedWatcher` (from
`hyperdjango.serviceclient`). One call works against every tier with no per-tier
code: it announces its `client_id` + `prefixes` and adopts whatever delivery
model the hub advertises in `hello`. Against the default tiers it receives each
event in the feed frame and reconnects with automatic catch-up (or a resync on
overrun/restart); against a ledger hub it pulls contiguous replay pages in order
and treats the WebSocket purely as a wake hint. `on_reset(response)` fires on a
full resync. `from_cursor=N` seeds the ledger cursor (ignored by the in-frame
tiers, which resume from their own `last_seq`).

## Publishing (producer)

```python
mgr = ManagerClient("http://127.0.0.1:8970", token="hmk_...")  # publish grant
mgr.publish("secrets/prod/api/stripe_key", "rotated", {"version": 7})
```

Each `publish()` mints a per-call dedupe key and marks the request idempotent, so
the transport may safely retry it. Metadata is capped and must never contain
secret material — the hub is a notification bus, not a value transport.

If you POST `/v1/events` directly (not through the SDK) and **reuse** a
`dedupe_key`, the key — not the body — is the identity: the original event is
returned and a changed subject/kind/metadata is dropped, not applied. Dedup is
scoped per producer, so two producers may reuse the same natural key without
colliding. In ledger mode this is durable and race-safe; in the in-memory tiers
it is best-effort (a much-later re-publish of an evicted key may append again).

## mTLS

```python
mgr = ManagerClient(
    "https://secrets.internal:9443",
    ca_file="ca.crt",
    client_cert_file="platform-api.crt",
    client_key_file="platform-api.key",
)
```

The in-process `MTLSTerminator` verifies the client certificate against the
private CA and forwards attested identity to the app; the feed WebSocket upgrades
through it transparently. Certificate issuance and the external-proxy (nginx)
alternative are shared with HyperSecret — see
`services/hypersecret/provision.py` (`ca`/`cert` commands) and
`services/hypersecret/deploy/nginx-mtls.conf`.

## Wire protocol

One protocol, tier declared in `hello`:

```
client → {"type":"subscribe","prefixes":[…],"client_id":…,"last_seq":…,"cursor":…}
server → {"type":"hello","mode":"ephemeral"|"catchup"|"ledger","seq":…,"cursor":…,"resync":…}

# ephemeral / catchup: the event itself, in the frame
server → {"type":"event","subject":…,"kind":…,"seq":N,"metadata":{…}}

# ledger: a wake hint; the subscriber pulls /v1/events?after=<cursor>
server → {"type":"wake","cursor":N}
```

In catchup, the hub first replays the missed events (`seq > last_seq`) then
streams live ones; `resync:true` (ephemeral, or a catchup overrun/restart) tells
the subscriber to full-resync from the producer first.

## Key routes

| Method | Path                                        | Purpose                                                         |
| ------ | ------------------------------------------- | --------------------------------------------------------------- |
| POST   | `/v1/events`                                | Publish a change record (publish grant)                         |
| WS     | `/ws/feed?prefixes=a,b`                     | Live feed (subscribe grant)                                     |
| GET    | `/v1/events?after=N&prefix=…&limit=…`       | Cursor replay — ledger mode only                                |
| GET    | `/v1/cursor`                                | Latest cursor id — ledger mode only                             |
| POST   | `/v1/admin/identities` · `/v1/admin/grants` | Provision identity / upsert grant (admin scope)                 |
| DELETE | `/v1/admin/identities/{name}`               | Revoke an identity (admin scope)                                |
| GET    | `/v1/audit`                                 | Query the access trail (admin scope)                            |
| GET    | `/health` `/ready` `/admin/` `/api/docs`    | Ops surface (open)                                              |
| GET    | `/metrics`                                  | Prometheus scrape (requires an identity — token or client cert) |

`POST /v1/admin/identities` is also how a **revoked** identity is brought back: a
plain re-POST of an existing name is a `409`, but `{"name": …, "reactivate":
true}` on a revoked name reactivates it (and re-audits). Reactivation restores
the identity's original token/cert — `is_active` is the single revocation switch
both auth legs check — so rotate the credential separately if it was revoked for
compromise. `/metrics` is gated behind a resolved identity so a scrape can't leak
the hub's traffic shape; `/health` and `/ready` stay open.

## Configuration

App tunables load through `HyperManagerConfig` (`config.py`): override with
`HYPERMANAGER_<FIELD>` env vars or a `site.toml` — e.g.
`HYPERMANAGER_LEDGER_MODE=1`, `HYPERMANAGER_CATCH_UP_RING_SIZE=4096`,
`HYPERMANAGER_PG_FANOUT=1`, `HYPERMANAGER_RETENTION_DAYS=7` (ledger),
`HYPERMANAGER_MTLS_LISTEN_PORT=9443`.

## Architecture

```
hypermanager/
├── app.py           server: publish, WS feed (3 tiers), ledger replay/cursor,
│                    admin, lifecycle
├── catchup.py       in-memory seq + bounded ring + resume query (default tiers)
├── models.py        ChangeEvent (ledger/cursor), ManagerIdentity, TopicGrant
│                    + subject grammar & prefix matching
├── authz.py         two-leg auth (token / mTLS cert) + prefix-grant gate
├── client.py        ManagerClient on hyperdjango.serviceclient (publish,
│                    replay, watch → mode-aware ChangeFeedWatcher)
├── config.py        HyperManagerConfig (env/TOML tunables + tier selectors)
├── seed.py          demo identities, grants, sample events
└── ARCHITECTURE.md  delivery tiers + design rationale
```

Tests: `scripts/test_e2e_hypermanager.py` (all three tiers over the native
server: catch-up replay/resync, ephemeral, and the ledger replay/cursor/
retention/gapless/concurrent-dedupe suite in ledger mode; plus mTLS),
`scripts/test_e2e_hypersecret_live.py` (end-to-end live rotation convergence
with HyperSecret), `scripts/test_hypermanager_fuzz.py` (subject grammar,
catch-up seq/ring/resume + dedupe properties, mTLS head-rewrite),
`scripts/test_mtls_unit.py` (certificate issuance + terminator handshake).
