# Live Config — Architecture

A worked example of **centralized config distribution with live invalidation**:
one consuming service reads its runtime keys from a secret store, then stays
converged with that store through a change-notification hub — no restart when a
key rotates. It composes two existing services (HyperSecret, HyperManager) with
a new consumer (the Storefront) into one runnable mesh, wiring them only over
their public HTTP + change-feed APIs.

## 1. Problem statement

A service needs runtime configuration that is partly public (a publishable
payment key, a browser maps key) and partly secret (a webhook signing secret).
Two forces pull against each other:

- **Fetch it fresh every time** — correct on rotation, but a per-request round
  trip to the secret store on the hot path.
- **Cache it** — fast, but stale the moment an operator rotates a key, and a TTL
  only bounds the staleness, it does not end it.

The resolution is a cache that is **invalidated by an event**, not a timer. The
consumer caches, and a rotation actively tells it to re-fetch. That needs three
parties: the store that holds the values, a hub that carries "something changed"
nudges, and the consumer that reconciles.

## 2. The three services and the data flow

```
  operator / app
        │  rotate stripe_pk_live (a put = a new version)
        ▼
┌─────────────────┐   transactional outbox    ┌──────────────────┐
│   HyperSecret    │ ────────nudge───────────▶ │   HyperManager    │
│  (:8960)         │   subject                  │  (:8970)          │
│  ciphertext only │   secrets/prod/api/…       │  live pub/sub hub │
└─────────────────┘                            └──────────────────┘
        ▲                                                 │
        │ authenticated re-fetch                          │ WebSocket feed
        │ (ciphertext → client-side decrypt)              │ "subject changed"
        │                                                 ▼
        │                                       ┌──────────────────┐
        └────────────────────────────────────  │    Storefront     │
              invalidate changed key,           │  (:8980)          │
              lazily re-fetch (304 if same)     │  in-memory cache  │
                                                └──────────────────┘
```

1. An operator (or an app) **rotates** a key in HyperSecret — a `put`, which
   appends a new immutable version. Plaintext is sealed client-side; the store
   only ever sees ciphertext.
2. In the **same transaction** as the version write, HyperSecret enqueues a
   metadata-only change event to its **transactional outbox** (subject
   `secrets/<namespace>/<key>`, carrying the version — never the value). A
   scheduled drainer posts it to HyperManager. The event cannot be lost between
   the commit and the post, and the store operation never blocks on the hub.
3. **HyperManager** runs its default **live in-memory pub/sub tier**: it pushes
   the nudge to connected subscribers whose prefixes cover the subject. No
   durable ledger is needed for this use case — a nudge is a wake-up, and a
   subscriber that misses one reconciles by re-fetching anyway.
4. The **Storefront**'s watcher receives the nudge, **invalidates** exactly the
   changed key in its client cache, then **lazily re-fetches** it (an unchanged
   key costs a body-free `304` via `known_version`). It decrypts client-side and
   updates its in-memory view. No restart.

On (re)connect the watcher **full-resyncs** (invalidate all → lazy re-fetch), so
a change that landed while it was starting up or briefly disconnected is never
served stale past the connect. A brief disconnect resumes from the hub's
per-client catch-up buffer; a buffer overrun or hub restart falls back to a
resync. The values themselves never traverse the hub — only nudges — so the
Storefront always reconciles through its own authenticated fetch.

The producer side (steps 2–3) already exists in HyperSecret, enabled by
`HYPERSECRET_MANAGER_URL` + a publish-granted token. The mesh only sets those
env vars; it does not modify HyperSecret.

## 3. Credential & scoping model (least privilege)

Every identity holds the **minimum** capability for its role. The Storefront in
particular can only read and subscribe — it can never write a secret or publish
a change.

| Identity               | Where it lives | Capability                               | Why                                              |
| ---------------------- | -------------- | ---------------------------------------- | ------------------------------------------------ |
| `operator:admin`       | HyperSecret    | read/write/admin/audit on all namespaces | The operator/CI that provisions and rotates keys |
| `service:prod-api`     | HyperSecret    | **READ** on `prod/api` only              | The Storefront's fetch identity                  |
| `producer:hypersecret` | HyperManager   | **PUBLISH** on `secrets/`                | HyperSecret's outbox drainer, publishing nudges  |
| `service:platform-api` | HyperManager   | **SUBSCRIBE** on `secrets/prod/`         | The Storefront's feed identity                   |

Two independent credentials guard the Storefront's two channels: a HyperSecret
**read** token for the values, and a HyperManager **subscribe** token for the
notifications. Neither carries write or publish. The subscribe grant is scoped
to `secrets/prod/`, so the Storefront cannot even learn that a `staging/` key
changed. The namespace **KEK** (master key) is handed to the Storefront out of
band (the `.runtime/` demo dir stands in for a deployment pipeline); it is used
client-side for decryption and never sent to any server.

Defense in depth: authorization is the first gate, cryptography the second. Even
if the Storefront's read grant were mis-scoped, it holds only the `prod/api` KEK
and could not decrypt any other namespace's ciphertext.

## 4. The consumer (Storefront) design

- **In-memory only.** Keys are cached in a lock-guarded dict; nothing is
  persisted. The service owns no database.
- **Public vs secret handling.** A `public` key's plaintext is decrypted and
  displayed in full (that is its purpose — a publishable key). A `secret` key is
  used through `secret_bytes()` — a wipeable bytearray zeroized at block exit —
  and only a `sha256:` fingerprint of it is ever surfaced, proving it is present
  and current without leaking a byte. The plaintext never lands in a field, a
  log line, or a response body.
- **Non-blocking read path.** The request handlers read only the local state
  dict; network fetch + decrypt happen on the startup hook and on feed events
  (the watcher's background thread), never inline in a request.
- **Fail-closed with bounded tolerance.** On a transient HyperSecret outage the
  client may serve a stale-but-bounded cached envelope (`stale_max`); on a hard
  failure the Storefront retains its last-known value and annotates the error,
  rather than blanking a key. A genuinely-missing key is marked unavailable.
- **Live notifications.** `/events` streams Server-Sent Events — an initial
  snapshot, then a notice per rotation — for a browser or dashboard to watch
  convergence happen.

## 5. Why HyperManager's default tier (no ledger)

The nudge is a **wake-up, not a record of truth**. HyperSecret's own version
history is authoritative; the hub only says "re-check this subject." A missed or
duplicated nudge is harmless: the watcher re-fetches, and an unchanged value
costs a `304`. So this mesh runs HyperManager's **default live in-memory
pub/sub** — no durable ledger, no Postgres on the delivery path, a bounded ring
for brief-reconnect catch-up. The opt-in durable-ledger tier exists for audited
deployments that need ordered, retained, at-least-once replay; the consumer SDK
negotiates whichever tier the hub advertises, so pointing this Storefront at a
ledger hub would still work (it degrades to a resync on each connect).

## 6. Security posture

- **Secrets in memory only** — never persisted by the consumer; the secret store
  holds only ciphertext and wrapped data keys and cannot read what it stores.
- **Publishable vs secret** — public keys are shown in full by design; the
  secret-classified key is used but never exposed (fingerprint only). An
  unclassified key fails _closed_ to masked handling.
- **Least privilege** — the consumer is read-only on its namespace and
  subscribe-only on its subject prefix; it can neither write nor publish.
- **Metadata-only feed** — change events carry versions and key ids, never
  secret material; the value only ever moves over an authenticated, client-side-
  decrypted fetch.
- **mTLS is available.** Both HyperSecret and HyperManager ship an in-process
  mTLS terminator (set their `*_MTLS_LISTEN_PORT` + cert/key/CA). The
  `SecretsClient` and the feed watcher both accept `ca_file` +
  `client_cert_file`/`client_key_file` and an `https://` base URL, so the
  Storefront can authenticate with a client certificate (CN = identity) instead
  of a bearer token, end to end. This demo wires the plaintext HTTP path for
  brevity; the credential model is identical under mTLS.

## 7. Best practices this demonstrates

- Cache invalidation driven by an **event**, not a timer — bounded-staleness
  becomes end-of-staleness.
- A **transactional outbox** for producer notifications: never lost, never
  blocking the state change, idempotent on retry.
- **Client-side decryption** with an out-of-band KEK: the store compromise
  yields ciphertext only.
- **Stale-while-revalidate** with a body-free `304` for unchanged keys.
- **Least-privilege, two-channel credentials**: read here, subscribe there,
  write/publish nowhere.
- **Public vs secret classification** with a fail-closed default and a
  fingerprint-only surface for secrets.
- Composing services over their **public APIs** without forking them — the mesh
  wires HyperSecret and HyperManager via env + their existing CLIs.
