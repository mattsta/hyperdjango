# HyperManager — Architecture

A generalized infrastructure change-notification hub: producers publish
metadata-only change records under hierarchical subjects; subscribers learn
that a covered subject changed and re-pull whatever changed from the producing
system on their own schedule. Nothing sensitive transits the hub — it carries
_that something changed_, never the changed value.

## 1. Problem statement

Infrastructure state changes constantly — credentials rotate, quotas move,
endpoints appear — and consuming services traditionally learn about it by
restarting. The goal is live convergence: a service subscribes to the parts
of the infrastructure it depends on, hears "X changed" within moments, and
refreshes exactly X through its normal read path (stale-while-revalidate,
no restart, no full reload).

HyperSecret is the first producer: secret creations, rotations, rewraps,
deletions, expiries, and exposure markings all flow here. The hub itself is
producer-agnostic — anything that manages infrastructure can publish.

## 2. Delivery tiers

A hub runs one of three delivery tiers, chosen at boot by two config selectors
(`config.py`). The **default is a live in-memory pub/sub** — the simplest thing
that meets the goal: push a nudge to connected subscribers and let each refetch
what changed. The durable audited log is the same design as before, now demoted
to an **opt-in** tier for the regulated/persistent-audit case.

| `ledger_mode` | `catch_up_ring_size` | Tier          | Contract                                                                         |
| ------------- | -------------------- | ------------- | -------------------------------------------------------------------------------- |
| `false`       | `> 0` (default)      | **catchup**   | Live push; a bounded ring lets a brief reconnect replay the exact missed events. |
| `false`       | `0`                  | **ephemeral** | Live push; no ring — every (re)connect resyncs. The simplest tier.               |
| `true`        | (n/a)                | **ledger**    | Durable append-only log with at-least-once ordered replay + retention. Opt-in.   |

All three share auth, scope, prefix-grant authorization, validation, idempotency,
audit, and the mTLS front door — only persistence and fan-out differ.

### Default tiers (catchup / ephemeral) — live in-memory pub/sub

A publish assigns an **in-memory monotonic sequence** under a lock and, in the
catchup tier, appends `(seq, subject, kind, metadata)` to a bounded global ring;
it then broadcasts an `event` frame — carrying the event itself — to connected
subscribers whose prefixes cover the subject, over the same first-segment-sharded
channel fan-out the ledger tier uses for its wake hints.

The lock-assigned seq is what makes the stream naturally gap-free and totally
ordered with **none** of the Postgres ceiling/xid/advisory-lock machinery the
ledger tier needs: no two publishes share a seq, and a committed seq can never
have a lower sibling still about to appear. The seq is process-local and resets
to 0 on restart — which correctly lowers it below any live subscriber's last-seen
value, so the reconnect check reads a restart as "resync". No Postgres touches
this path (auth and audit still use it; the change stream does not).

**Reconnect catch-up.** The server is more persistent than its clients but only
boundedly so. A subscriber remembers its `client_id` and the last `seq` it was
delivered. On reconnect it sends `(client_id, last_seq)`:

- `floor <= last_seq <= head` → the ring still holds everything after `last_seq`:
  the hub replays those events (matching the subscriber's prefixes) as `event`
  frames, then streams live ones.
- `last_seq < floor` (fell behind the ring) or `last_seq > head` (the process
  restarted and reset the seq) or no prior state → the missed window is
  unrecoverable: `hello.resync = true` and the subscriber full-resyncs from the
  producer, then takes live frames.

`floor` is the highest seq that has aged out of the ring; the retained window is
`(floor, head]`. Overrunning the ring, or a restart, therefore degrades to a
resync — never a silent gap.

**Best-effort by design.** Delivery is a live nudge; the subscriber re-fetches
authoritative state itself, so the default tiers do not promise exactly-once or
strict cross-publisher ordering. A slow consumer that fills its bounded live
queue is closed (so it reconnects and catches up from the ring, or resyncs)
rather than being served past a dropped event. Idempotency by `dedupe_key` is
honored against a bounded recent-key set: a producer's retried key collapses to
one event while the key is retained, but once evicted (or after a restart) a much
later re-publish may append again — the durable per-producer guarantee lives in
the ledger tier alone.

### Ledger tier (opt-in) — durable audited log

The opt-in tier for a regulated/persistent-audit deployment. Its correctness
rests on one invariant: **the replay endpoint is the single ordered source of
truth, and the cursor advances only through contiguous replay pages.** The live
WebSocket is only a wake-up hint.

- **Ledger** — `ChangeEvent` rows: an append-only table whose auto-increment id
  is the feed **cursor**. Replay is a WHERE-id-greater-than query. Nothing is
  ever _only_ in a channel message.
- **Wake hints** — every new row also publishes a content-free hint (subject +
  id, never the metadata payload) through the channel layer (in-memory
  single-process, or `PgChannelLayer` LISTEN/NOTIFY across replicas), sharded by
  first subject segment. A hint only tells a covered subscriber to pull sooner;
  it never carries the event and never advances a cursor. Repeated hints coalesce
  into one pending wake per subscriber (a dirty flag, not a queue).
- **Client contract** — remember the last cursor; on a wake or a periodic poll,
  pull `replay(after=<cursor>)` page by page in ledger order, advancing the
  cursor per delivered event. Because the wake is never the source of the data,
  an out-of-order, duplicated, or dropped wake cannot reorder, dupe, or lose an
  event. The result is **at-least-once, in-order delivery** that holds by
  construction under concurrent publishers, sharded fan-out, and lost wakes.
- **Gapless replay ceiling** — a serial id is assigned at INSERT but visible only
  at COMMIT, so a naive `max(id)` could jump a lower in-flight id, and a cursor
  never moves backward, so that event would be lost forever. Every publish
  transaction therefore takes a single **transaction-scoped advisory gate** (a
  fixed class + one key) as its first statement, so while any publish's row is
  uncommitted no other publish transaction can be open — they block on the gate.
  Serial id order then equals commit order, so a committed id can never have an
  uncommitted lower sibling, and the ceiling is simply `max(committed id)`. The
  gate auto-releases at COMMIT/ROLLBACK, so it can never leak onto a pooled
  connection. The tradeoff is serialized publish throughput — fine for this
  low-rate metadata feed, and in return replay is a plain indexed `max` with no
  `pg_locks` scan.
- **Idempotent publish** — a publish may carry a `dedupe_key`, unique per
  producer (`(producer, dedupe_key)`). A re-POST of the same key returns the
  existing event's id instead of appending a second row, holding even under a
  truly concurrent double-POST: the unique constraint collapses the race so both
  callers receive the same id and neither 500s. A different producer reusing the
  same key still lands its own event. The key, not the body, is the identity —
  reusing a key with a changed subject/kind/metadata returns the ORIGINAL event
  unchanged; the new subject is still grant-checked before the dedupe lookup, so a
  reused key can never smuggle a publish past authorization. The SDK's `publish()`
  mints a fresh `uuid4` key per call.
- **Retention floor** — the highest id the sweep has trimmed. The sweep trims by
  id boundary — `DELETE WHERE id <= max(id WHERE created_at < cutoff)`, batched
  into short transactions so it never holds one long write open (which would pin
  the ceiling for every subscriber) — and derives the floor from that same
  boundary. Deriving from the trim boundary (not `min(surviving id)`) keeps the
  floor exact: a burned first id never looks like a trimmed prefix, and a
  created_at/id inversion can't leave an unreported gap. The floor is persisted in
  `hm_retention_floor`, so under `pg_fanout` every replica reads the same value
  and one replica's trim is honored by all — a below-floor replay re-reads it on
  the request path without waiting for its own sweep tick. A replay from below the
  floor carries `"reset": true`, which the watcher surfaces through `on_reset`.

## 3. Subjects, producers, authorization

- **Subject**: hierarchical path, e.g. `secrets/prod/api/stripe_key`;
  segments of `[a-z0-9_.-]`, lowercase, no leading/trailing slash.
- **Kind**: short verb — `created`, `rotated`, `rewrapped`, `deleted`,
  `purged`, `exposed`, `expired`, or any producer-defined `[a-z_]` word.
- **Metadata**: a small JSON object (versions, key ids, hints). Producers
  must never place secret material here; the hub enforces a size ceiling
  and the metadata-only convention is part of the producer contract.
- **Identity**: the same signed-token identities as HyperSecret (hashed at
  rest, revocable), or a network-verified mTLS client certificate whose CN
  is the identity name. Both resolve through one gate.
- **Scopes**: coarse capabilities on the identity, checked in one place
  (`require_scope`). `feed` is required for the whole change-notification API —
  publish and the live feed everywhere, plus replay/cursor in ledger mode;
  `admin` for provisioning. Scopes gate _which API_; grants gate _which subjects_.
- **Grants**: identity → subject prefix with `can_publish` / `can_subscribe`
  flags. A producer publishes only under its granted prefixes; a subscriber
  sees only events under its granted prefixes — enforced on every delivery (and
  on replay in ledger mode). Fail closed everywhere.

## 4. API

| Method | Path                                        | Purpose                                         |
| ------ | ------------------------------------------- | ----------------------------------------------- |
| POST   | `/v1/events`                                | Publish a change record (publish grant)         |
| WS     | `/ws/feed?prefixes=a,b`                     | Live feed (subscribe grant)                     |
| GET    | `/v1/events?after=N&prefix=…&limit=…`       | Durable cursor replay — **ledger mode only**    |
| GET    | `/v1/cursor`                                | Latest cursor id — **ledger mode only**         |
| POST   | `/v1/admin/identities` · `/v1/admin/grants` | Provision identity / upsert grant (admin scope) |
| DELETE | `/v1/admin/identities/{name}`               | Revoke an identity (admin scope)                |
| GET    | `/v1/audit`                                 | Query the access trail (admin scope)            |
| GET    | `/health` `/ready`                          | Ops surface (open)                              |
| GET    | `/metrics`                                  | Prometheus scrape (requires an identity)        |

The replay/cursor endpoints exist only in ledger mode; in the default tiers the
event is delivered in the feed frame and a pull to either path 404s (the
mode-aware client tolerates that). Provisioning is idempotent and race-safe: an
identity name is unique for the deployment's life, but `POST /v1/admin/identities
{"reactivate": true}` on a **revoked** name reactivates it (re-audited); a grant
`POST` is an upsert on `(identity, prefix)`. `/metrics` is gated behind a resolved
identity so a scrape cannot leak subject domains and traffic shape; `/health` and
`/ready` stay open.

### WebSocket wire protocol (JSON text frames)

One protocol spans all three tiers; the `hello` frame declares which tier is
serving, and the client (a framework `ChangeFeedWatcher`) runs the matching state
machine with no per-tier code.

    client → {"type":"subscribe","prefixes":[…],"client_id":<str|null>,
              "last_seq":<int|null>,"cursor":<int|null>}
    server → {"type":"hello","mode":"ephemeral"|"catchup"|"ledger",
              "seq":<int in-mem head; ephemeral/catchup>,
              "cursor":<int ledger head; ledger>,"resync":<bool>}

Then, by mode:

- **ephemeral / catchup** — the event is delivered in the frame:

      server → {"type":"event","subject":…,"kind":…,"seq":N,"metadata":{…}}

  In catchup, the hub first replays the buffered missed events (`seq > last_seq`
  matching the subscriber's prefixes) as `event` frames, then streams live ones.
  In ephemeral (and on a catchup ring overrun / restart) `resync` is true and the
  subscriber full-resyncs from the producer before taking live frames.

- **ledger** — hints only; the subscriber pulls the durable replay endpoint:

      server → {"type":"wake","cursor":N}   # a covered subject changed → pull replay

  Delivery and cursor advancement happen exclusively through
  `GET /v1/events?after=<cursor>` (on each wake and a periodic poll, so a lost
  wake self-heals). A `"reset": true` replay means the cursor fell below the
  retention floor: full-resync and resume from the floor.

A keepalive `{"type":"ping"}` is answered with `{"type":"pong"}` on every tier.

## 5. Consuming side

`ManagerClient.watch(prefixes, on_event)` returns a framework
`ChangeFeedWatcher` (`hyperdjango.serviceclient`) running on daemon threads. One
call works against every tier: it announces its `client_id` + `prefixes` in the
subscribe frame and adopts whatever delivery model the hub advertises in `hello`
— in-frame delivery for the default tiers (with automatic reconnect catch-up),
or the replay-drain loop nudged by wake hints for ledger. `SecretsClient.watch()`
consumes the hub through the same generic transport (no dependency on the hub
application): a matching event invalidates that key's cached envelope, so the
next access re-fetches and re-decrypts the new version — rotation converges in
real time with no process restart, and the cache's fail-closed /
stale-while-revalidate semantics stay exactly as configured.

## 6. mTLS

Both the HTTP API and the WebSocket feed accept certificate identity via the
in-process `MTLSTerminator` (`hyperdjango.mtls`): the terminator verifies the
client certificate against the private CA on the network socket and injects
attested identity headers; WebSocket upgrades continue through it as transparent
byte splices. The external-proxy topology (nginx verifying certs, forwarding the
same headers with a shared attestation secret) is documented in the HyperSecret
deploy directory and works identically here.

## 7. Non-goals

Payload transport (the hub is metadata-only). Exactly-once push (every tier is
at-least-once at best; the default tiers are best-effort with resync-on-overrun,
the ledger tier is at-least-once ordered via cursor replay). Unbounded retention
(the default ring is bounded and the ledger sweep trims past a window; a
subscriber further behind than the ring or the retention window does a full
re-read of the producer).
