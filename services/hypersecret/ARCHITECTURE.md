# HyperSecret — Architecture

Minimal, self-hosted, single-purpose secret management for your own
infrastructure. Services inside the security boundary authenticate to a small
HTTP API and fetch secrets at runtime. No policy engines, no web UI, no
dynamic-credential plugins — the entire system is a few files of Python you
can read in an afternoon.

## 1. Problem statement

Teams want an AWS-Secrets-Manager-like service they run themselves. Existing
self-hosted options (Vault/OpenBao, Infisical) bring enterprise surface area —
plugin ecosystems, policy languages, unseal ceremonies — that dwarfs the
actual need: _authenticated runtime access to secrets, with provenance,
rotation, and audit, inside one trust boundary_.

HyperSecret is the minimal version: one server, one client SDK, one operator
CLI, one injection wrapper. The defining property is that **the server cannot
read the secrets it stores**.

## 2. Trust model: envelope encryption, client-side decryption

```
operator/CI (has KEK) ──seal──▶ server (ciphertext only) ──▶ service (has KEK) ──open──▶ plaintext
```

- **KEK (master key)** — a 32-byte AES-256 key per _namespace_
  (`prod/api`, `staging/api`, …). Held only by operators and the services
  authorized for that namespace. The server stores the KEK's _id_, never its
  material.
- **DEK** — a fresh 32-byte key per secret _version_. Encrypts the payload
  (AES-256-GCM). The DEK itself is stored wrapped (AES-256-GCM under the KEK).
- The server persists `(ciphertext, encrypted_dek, alg, kek_id, metadata)`
  and enforces _authorization_; it has no decryption path. A full server or
  database compromise yields ciphertext only.

### AAD binding (defense against blob substitution)

Both GCM layers authenticate associated data:

```
payload AAD:  sm1|{namespace}|{key}|{version}
DEK-wrap AAD: sm1|{namespace}|{key}|{version}|{kek_id}
```

Without this, anyone with database write access could swap the blob of
`prod/api/stripe` with that of `prod/api/test-key`; clients would decrypt the
wrong secret _successfully_. With AAD binding, any cross-slot substitution
fails GCM authentication at the client. The wrap-layer AAD additionally pins
the KEK id, so a blob cannot be re-attributed to a different key generation.

### Version binding doubles as optimistic concurrency

The client seals against the version it intends to write (`current + 1`) and
sends that version number; the server rejects a mismatch with 409. Two racing
writers cannot silently interleave, and the AAD a reader verifies is exactly
the version the writer sealed.

### KEK rotation = rewrap

Rotating a namespace master key does not touch payload ciphertext. An
operator (holding old and new KEK) unwraps each version's DEK with the old
KEK, wraps it with the new one, and POSTs the rewrapped DEK + new `kek_id`.
This is the one permitted mutation of a stored version, and it is audited.
Payload bytes are immutable forever.

Each rewrap atomically retains the wrapped DEK it replaced (a one-deep undo). A
rewrap that writes an unusable blob — a bug or a compromised rotation — would
otherwise brick the version, so an admin-scoped, audited undo
(`POST …/rewrap/undo`) swaps the retained pair back and clears the undo slot. It
is one-shot: a second undo has nothing to restore. The restored `kek_id` is not
checked against the namespace's current KEK, because recovery means returning to
the prior generation.

## 3. Identity & authorization

- **Service identity** — each service/operator gets a bearer token minted by
  `SignedAPIKeyMixin`: HMAC-signed (forgeries rejected without a DB hit),
  stored only as a SHA-256 hash, shown once at creation, revocable,
  expirable. Identities carry coarse scopes: `read`, `write`, `admin`,
  `audit`.
- **mTLS leg** — a service may instead present a client certificate whose CN
  names its identity. The certificate is verified on the network socket (by
  the in-process `MTLSTerminator` against a private CA, or by an external
  proxy sharing an attestation secret) and mapped to the same identity row.
  Both legs end at the same active-identity check, so revoking an identity
  disables its token and its certificate together. Certificate issuance is a
  small CA in the provisioning CLI; the terminator forwards attested identity
  headers and strips any client-supplied spoof of them.
- **Grants** — an explicit allow-list table: identity → namespace with
  `can_read` / `can_write`. No wildcards, no policy language. "Which services
  can read `prod/api`?" is one indexed query, reviewable in the admin panel.
- **Fail closed** — unknown token, inactive identity, missing grant, missing
  namespace: everything denies. Denials are audited with the same fidelity
  as successes.
- **Defense in depth** — authorization is the first gate; cryptography is the
  second. A frontend server that somehow bypassed the grant check still holds
  the wrong KEK and cannot decrypt `prod/api` blobs.

## 4. Lifecycle

- **Provisioning** — operators/CI run `provision.py` (or any client with a
  `write` grant): seal locally, POST ciphertext. Plaintext never leaves the
  provisioning host.
- **Versioning** — every write appends an immutable version with provenance
  (identity, timestamp). History is queryable; any version is fetchable for
  rollback.
- **Rotation** — a new version through the same path. There is deliberately
  no server-side "rotate" endpoint: the server cannot encrypt, so it cannot
  rotate. That asymmetry is the security model working as intended.
- **Deletion** — soft-delete stamps `deleted_at` and hides the secret from
  reads; a purge (admin scope) removes rows after the retention window. A
  scheduled sweep purges expired soft-deletes.

## 5. Live change notifications

Rotations only converge without restarts if consumers hear about them.
Every state change — `created`, `rotated`, `rewrapped`, `deleted`, `purged`,
`exposed`, and `expired` (from a scheduled rotation-due sweep) — becomes a
metadata-only event (versions, key ids; never secret material) under subject
`secrets/<namespace>/<key>`.

Delivery uses a **transactional outbox**: the event is written to an
`OutboxEvent` row in the _same transaction_ as the secret state change, so it
can never be lost between the commit and the post to the hub. A scheduled
drainer posts each pending row through a thin framework `ServiceClient` (it has
no dependency on the hub application) using the row id as the hub dedupe key,
so a crash between the POST and the local delete cannot double-append (the hub
collapses the re-POST idempotently).

The drainer acts on each post's outcome: **delivered** deletes the row;
**retryable** (hub down or a 5xx) stops the pass so the whole backlog retries
in order next run; **permanent** (a 4xx — bad token, malformed subject)
**parks** the row with its rejection reason and continues to the next, so a
poison event never head-of-line-blocks the feed. Parked rows are counted and
recoverable by an operator (inspect + set `status` back to `pending`).

HyperSecret's own version history stays authoritative. The client SDK's
`watch()` consumes the hub through the same generic transport and returns a
framework `ChangeFeedWatcher` that negotiates the hub's delivery tier from its
hello frame. The consumer semantics carry no state, so they fit every tier: a
change to key `K` invalidates `K`, a resync invalidates everything, and the next
access re-fetches and decrypts.

By default the hub is a **live pub/sub**: it pushes metadata-only "subject
changed" nudges and the client refetches on its own (stale-while-revalidate),
so an unchanged value costs a body-free `304` via `known_version`. On connect
the watcher full-resyncs (invalidate all → lazy refetch); a brief disconnect
resumes from the hub's per-client catch-up buffer (keyed by a stable
`client_id`, filtered to this namespace's `prefixes`), replaying only the missed
keys, and a buffer overrun or hub restart falls back to a full resync. The
values themselves never traverse the hub — only metadata-only nudges — so a
subscriber always reconciles through its own authenticated fetch.

A **durable-ledger** tier is an opt-in hub setting for audited deployments that
need ordered, retained, at-least-once replayable delivery. `watch()` consumes
the default live tier; pointed at a ledger hub it degrades safely to a resync on
each (re)connect (never stale past a connect) rather than pulling the ledger.
See the HyperManager architecture for each tier's guarantees.

## 6. Audit

Every request that reaches a route handler appends a row: identity, namespace,
key, version, action, outcome, client IP, timestamp. This covers reads, writes,
rewraps, deletes, exposures, batch and namespace listings, and admin
provisioning — each in every outcome: `ok`, `not_modified`, `not_found`,
`conflict`, `invalid` (post-gate 400-class input rejection), and `denied` (an
authn/scope/grant refusal, including an unauthenticated request and a rejected
`/metrics` scrape). The retention sweep's hard purges audit as the `system`
identity. Audit writes are decoupled from the request path through the framework
`BatchWriter`: rows buffer in memory and flush in batches when the batch size or
the flush interval is reached (and on shutdown), so the hot path only enqueues
and no committed row is dropped. The audit-query endpoint drains the buffer
before selecting, so an auditor always reads their own era. A periodic sweep
trims rows past the audit-retention window so the log stays bounded. Queryable
via `GET /v1/audit` (audit scope) and the admin panel.

`GET /v1/audit` is **global by design**: an `audit`-scoped identity reads the
entire access log across every namespace, not only namespaces it holds a grant
on. Auditing is a whole-deployment oversight role — a per-namespace audit view
would let a namespace owner hide activity from the auditor — so the audit scope
is deliberately more powerful than a namespace grant. Mint it sparingly.

The per-namespace access **metric** (`hypersecret_namespace_access_total`) is
labeled only for namespaces the caller actually holds a grant on — an
unauthenticated or ungranted request names an attacker-chosen path segment,
which must never mint an unbounded metric label. The AccessLog row still records
the true namespace; that table is bounded by audit retention and the app-wide
per-IP rate limit, not by label cardinality.

**HyperAdmin-panel writes bypass this trail.** Editing a grant, identity, or
outbox row (e.g. requeueing a parked event) through the `/admin` panel writes
the model row directly: it does **not** append an AccessLog row, and it does
**not** invalidate the in-process grant cache, so a grant change made in the
panel takes up to `grant_cache_ttl` seconds to take effect on the fetch path.
Use the audited `/v1/admin/*` API (which invalidates the cache) when either
property matters; the panel is an operator inspection/break-glass surface.

## 7. HTTP API (v1)

| Method | Path                                            | Purpose                                                               |
| ------ | ----------------------------------------------- | --------------------------------------------------------------------- |
| GET    | `/v1/secrets/{env}/{service}/{key}`             | Fetch blob (`?version=N` pins, `?known_version=N` → 304 if unchanged) |
| POST   | `/v1/secrets/{env}/{service}/{key}`             | Append version (write grant)                                          |
| GET    | `/v1/secrets/{env}/{service}`                   | List keys (`?include_deleted=1` + admin includes soft-deleted)        |
| GET    | `/v1/secrets/{env}/{service}/{key}/versions`    | Version history + provenance                                          |
| POST   | `/v1/secrets/{env}/{service}/{key}/rewrap`      | KEK rotation for one version                                          |
| POST   | `/v1/secrets/{env}/{service}/{key}/rewrap/undo` | Roll one version's wrapped DEK back to the retained pair (admin)      |
| POST   | `/v1/secrets/{env}/{service}/{key}/expose`      | Mark exposed/compromised (admin scope)                                |
| DELETE | `/v1/secrets/{env}/{service}/{key}`             | Soft delete (`?purge=1` + admin = hard)                               |
| POST   | `/v1/batch/{env}/{service}`                     | Batch fetch (body: `{"keys": [...]}`)                                 |
| GET    | `/v1/namespaces`                                | Namespaces this identity can read                                     |
| GET    | `/v1/audit`                                     | Audit query (audit scope)                                             |
| POST   | `/v1/admin/…`                                   | Namespaces / identities / grants (admin scope)                        |
| GET    | `/health`, `/ready`, `/metrics`                 | Liveness, readiness, Prometheus                                       |

Namespaces are two path segments (`env/service`) — typed router params, no
regex, native-path friendly.

## 8. Runtime injection (`secrets_run.py`)

Application-transparent env-var injection across substrates:

- **exec mode** (systemd `ExecStart=`, Docker `ENTRYPOINT`): fetch + decrypt
  the mapped secrets, set env vars, `os.execvp` the real program. One process
  layer, zero app changes.
- **env-file mode** (`--output /run/secrets/app.env`, mode 0600): for
  systemd `EnvironmentFile=` via a oneshot unit (`secrets-fetch@.service`).
- **strict by default**: a missing secret aborts the launch rather than
  starting the app half-configured.

Unit files in `deploy/`.

## 9. Performance posture

- Conditional fetch: pollers send `known_version` and get a body-free 304;
  steady-state polling costs one indexed row lookup.
- Batch endpoint: one round trip for a service's whole secret set.
- Grant decisions cached in-process with a short TTL (bounded staleness,
  revocation still propagates within seconds).
- Audit is enqueue-only on the hot path.
- Client SDK caches _ciphertext_ and decrypts per access: cache hits skip the
  network, while plaintext lifetime in memory stays minimal.

## 10. Non-goals

Dynamic secret generation, RBAC beyond identity+grant, multi-org tenancy,
web UI (HyperAdmin covers operator browsing), KMS integration.

## 11. Security invariants (each covered by tests)

- No plaintext secret or clear DEK is ever persisted or logged server-side.
- Server code contains no KEK material and no decrypt path for payloads.
- Authorization is fail-closed at every gate.
- Every request reaching a route handler — including a denial, a miss, and a
  post-gate invalid-input rejection — produces an audit row (HyperAdmin-panel
  writes are the one exception; see §6).
- Payload ciphertext of a stored version is immutable; the only permitted
  mutation is an audited DEK rewrap.
- Any tamper (ciphertext, AAD slot, wrong KEK) surfaces as a client
  `DecryptError`, never partial plaintext.
- Soft-deleted secrets are unreadable through every read path.
