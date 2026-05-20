# Service Client & Change-Feed Watcher

`hyperdjango.serviceclient` provides two reusable building blocks for programs
that call an internal JSON-over-HTTP service and follow its change feed:
`ServiceClient`, a retrying JSON transport, and `ChangeFeedWatcher`, an
ordered, self-healing feed consumer.

The module is stdlib-only (`urllib`, `socket`, `ssl`, a minimal RFC 6455
WebSocket client) and depends on no application. It is the shared foundation
for outbound SDKs: instead of each SDK hand-rolling a retry loop, a bearer
header, an mTLS context, and a feed watcher, it wraps `ServiceClient` and
`ChangeFeedWatcher` and parameterizes the specifics (base URL, paths, auth
header, error text).

## When to use it

Use `ServiceClient` whenever a program makes JSON HTTP calls to a service and
wants bounded, correct retries and typed errors. Use `ChangeFeedWatcher` when
that service exposes a change feed and the program must consume it without
losing events across disconnects. One watcher speaks three delivery models —
durable `ledger`, `ephemeral`, and `catchup` — and runs whichever the hub
advertises in its hello frame (see [Delivery models](#delivery-models)).

## `ServiceClient`

```python
from hyperdjango.serviceclient import ServiceClient, RetryPolicy

client = ServiceClient(
    "https://svc.internal:8960",
    token="hsk_...",  # optional credential
    token_header="Authorization",  # or "X-API-Key", etc.
    token_scheme="Bearer",  # "" sends the raw token (API-key style)
    timeout=5.0,
    retry=RetryPolicy(max_attempts=3, base_backoff=0.1, max_backoff=10.0),
    ca_file="ca.crt",  # optional mTLS identity
    client_cert_file="client.crt",
    client_key_file="client.key",
)

data = client.request("GET", "/v1/things", params={"limit": "50"})
client.request("POST", "/v1/things", json_body={"name": "x"})
```

`request(method, path, *, json_body=None, params=None, idempotent=None)`
returns the parsed JSON body (or `None` when the response is empty). A `2xx`
whose body is not decodable JSON raises `ResponseError` — a `200` carrying a
captive-portal or proxy error page is a contract violation, not data.

### Raw-status requests

`request_raw(method, path, ...) -> (status, headers, body)` runs the exact same
retry, backoff, TLS, and no-redirect machinery as `request`, but returns the
HTTP status instead of mapping a non-`2xx` to an exception. Use it when a status
is a first-class result rather than an error — a conditional `304`, or a `404` /
`409` a caller wants to branch on:

```python
status, headers, body = client.request_raw(
    "GET", "/v1/secrets/x", params={"known_version": "7"}
)
if status == 304:
    ...  # not modified — serve the cached copy
```

`body` is the parsed JSON when the body decodes, else `None`. A transport
failure still retries per the policy and raises `ServiceUnavailable` when
exhausted, and a body over the size cap still raises `ResponseError`; only
definitive HTTP statuses are returned rather than raised.

### Redirects and size caps

- **Redirects are never followed.** A JSON API has no valid `3xx` answer, and
  following one would re-send the credential to the redirect target (possibly a
  different host). A `3xx` surfaces as a `RequestError` from `request` and as
  the returned status from `request_raw`.
- **Every response body is capped** at `max_response_bytes` (default 32 MiB) —
  success and error alike. An over-cap `2xx` body raises `ResponseError`; an
  over-cap error body degrades to an empty `detail` and the status still maps to
  its typed error, so a hostile or misconfigured server cannot balloon memory
  through either path. A transport failure while reading a body — an error body,
  or a `2xx` body cut short by a mid-flight reset (an `IncompleteRead` from a
  truncated chunked stream, a `BadStatusLine` from a torn connection) — is
  caught like any other transport failure: it retries per the policy and
  surfaces typed (a `ServiceError` subtype), never as a bare `OSError` or
  `IncompleteRead`.

### Retry contract

Retries apply **only to idempotent requests**. `idempotent` defaults to `True`
for `GET`/`HEAD`/`OPTIONS` and `False` otherwise; pass `idempotent=True`
explicitly for a safe-to-repeat `POST` (for example one carrying a dedupe key).

- **HTTP status responses are definitive** — a `4xx` or `5xx` is never retried.
  The server saw the request and answered; repeating it is the caller's
  decision, not the transport's.
- **Transport failures** (connection refused, timeout, reset, or a body read
  cut short mid-flight — `URLError`, `TimeoutError`, `OSError`, or an
  `http.client.HTTPException` such as `IncompleteRead`/`BadStatusLine`) retry up
  to the policy, then raise `ServiceUnavailable`.

`RetryPolicy` backoff before retry _n_ (0-based) is
`min(base_backoff * 2**n, max_backoff)` plus uniform jitter in
`[0, base_backoff)`. The jitter breaks up synchronized retry storms; the cap
bounds the worst-case wait. `base_backoff` and `max_backoff` must be
non-negative — `0` is allowed and means retry immediately (no wait), a negative
value is rejected at construction.

**Local-resource exhaustion is retried out-of-band.** A connect-time
`EADDRNOTAVAIL` / `EADDRINUSE` (ephemeral-port starvation behind a busy NAT, or
a flood of short-lived connections leaving sockets in `TIME_WAIT`) is a purely
local condition — the request never left the host — so it is not the server's
fault and consumes **no** `RetryPolicy` attempt. It is instead retried against a
wall-clock deadline (~30s) on a short backoff, regardless of idempotency, since
nothing was sent; these conditions self-heal within seconds as ports leave
`TIME_WAIT`. This keeps a chatty client (or the full parallel test suite) from
failing an otherwise-fine request on a transient local hiccup. Once the deadline
passes it falls back to raising `ServiceUnavailable` like any other transport
failure.

### mTLS

Pass `ca_file` (to pin the server) and optionally `client_cert_file` /
`client_key_file` (to present a client certificate) with an `https` base URL.
`build_ssl_context(ca_file, client_cert_file, client_key_file)` is exposed for
callers that want to build the context directly. With a client certificate the
`token` is optional — the server may authenticate the certificate's subject. A
client certificate is honored independently of `ca_file`: presenting a client
identity does not require also pinning a CA (without a CA the default trust
store verifies the server).

### Error hierarchy

| Exception            | Raised for                                                                 |
| -------------------- | -------------------------------------------------------------------------- |
| `ServiceError`       | base type; carries `.status` and `.detail`                                 |
| `AuthError`          | `401` (bad/revoked credential) or `403` (missing grant/scope)              |
| `RequestError`       | any other `4xx`, or a refused `3xx` redirect — the request itself is wrong |
| `ServerError`        | `5xx`                                                                      |
| `ResponseError`      | a `2xx` body that is not decodable JSON, or a body over the size cap       |
| `ServiceUnavailable` | transport failure after the retry policy is exhausted                      |

`.detail` carries the server's message when the error body was JSON with a
`detail` field. Applications typically alias or subclass these (for example
`SecretNotFound(RequestError)`).

`classify_status(status, detail="")` exposes the status→type mapping as a plain
function (`401`/`403` → `AuthError`; a `3xx` or any other `4xx` → `RequestError`;
`5xx` → `ServerError`). An SDK that layers its own meanings on a few statuses (a
`404` that means "not found", a `409` that means "conflict") special-cases those
and delegates every other status here, instead of re-deriving the base taxonomy.

### Env-driven construction

`ServiceClient` never reads the process environment itself. Client programs
that want env-driven construction use the helper, which centralizes the
variable shape behind a prefix:

```python
from hyperdjango.serviceclient import service_client_from_env

client = service_client_from_env("HYPERSECRET")  # any override kwarg wins
```

reads `HYPERSECRET_URL`, `HYPERSECRET_TOKEN`, `HYPERSECRET_CA_FILE`,
`HYPERSECRET_CLIENT_CERT`, and `HYPERSECRET_CLIENT_KEY`.

An SDK **subclass** cannot go through `service_client_from_env` (that builds a
plain `ServiceClient`). It instead reads the same variable shape with
`service_client_env_kwargs(prefix) -> dict` and splats the result into its own
constructor:

```python
from hyperdjango.serviceclient import service_client_env_kwargs


class MySdk(ServiceClient):
    @classmethod
    def from_env(cls, prefix, **app_specific):
        return cls(**service_client_env_kwargs(prefix), **app_specific)
```

The returned dict holds `base_url`, `token`, `ca_file`, `client_cert_file`, and
`client_key_file`. `service_client_from_env` builds on it, so the `{PREFIX}_*`
variable shape lives in exactly one place.

## `ChangeFeedWatcher`

```python
from hyperdjango.serviceclient import ChangeFeedWatcher


def on_event(event):
    refresh(event)


def on_reset(response):
    full_resync_from_producer()


watcher = ChangeFeedWatcher(
    client,
    replay_path="/v1/events",
    ws_path="/ws/feed",  # None for poll-only
    on_event=on_event,
    on_reset=on_reset,
    cursor=last_seen_cursor,
    limit=500,
    poll_interval=30.0,
).start()
...
watcher.stop()
```

### Delivery models

The hub advertises a delivery `mode` in its hello frame and the watcher runs
the matching state machine — one class, one internal mode switch. The frames
are:

- subscribe (client → hub, on every connect):
  `{"type":"subscribe","prefixes":[...],"client_id":<str|null>,"last_seq":<int|null>,"cursor":<int|null>,"epoch":<str|null>}`
- hello (hub → client):
  `{"type":"hello","mode":"ephemeral"|"catchup"|"ledger","seq":<int>,"cursor":<int>,"resync":<bool>,"epoch":<str|null>}`
- event (hub → client, ephemeral/catchup):
  `{"type":"event","subject":...,"kind":...,"seq":N,"metadata":{...}}`
- wake (hub → client, ledger): `{"type":"wake","cursor":N}` — a hint only

| Model         | Configure                   | On (re)connect                                               | Delivery                                                       |
| ------------- | --------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------- |
| **ledger**    | `replay_path` (or `replay`) | pull replay to catch up on anything missed while down        | replay pages are the source of truth; a WS frame only wakes it |
| **ephemeral** | `ws_path`, no `replay_path` | `on_reset` (invalidate + lazily re-fetch) — every time       | each `event` frame → `on_event`; no per-client server state    |
| **catchup**   | `ws_path`, no `replay_path` | send `(client_id, last_seq)`; hub replays only missed events | each `event` frame → `on_event`, advancing `last_seq`          |

In **catchup**, the hub retains a per-client buffer keyed by `client_id` (stable
for the watcher's lifetime — pass one to survive a process restart, else a
per-watcher uuid is generated). On reconnect the watcher resumes from the
retained `last_seq`, so a brief disconnect replays only the events missed in the
gap — the server is more persistent than the client. The hub stamps a random
`epoch` at startup and advertises it in the hello; the watcher echoes it in the
next subscribe. If the hub evicted past `last_seq` (its ring overran or the
`client_id` is unknown) **or the epoch changed** (the hub restarted, so its
in-memory sequence is a fresh, unrelated space) the watcher does a full
`on_reset` instead of resuming — so a stale `last_seq` that happens to fall
inside a new incarnation's sequence range can never be mistaken for a valid
resume. Delivery is never partial or duplicated.

**Negotiation.** The watcher's configured intent proposes a model — a durable
`replay_path` means ledger, a ws-only feed means in-frame delivery — and the
hub's advertised `mode` decides. A mismatch always falls back to a
frame-delivery resync, which is safe: a hub advertising `ledger` to a watcher
with no replay endpoint is served as ephemeral, and a hub advertising an
in-frame model to a ledger-configured watcher quiesces the replay drain and
takes the hub's frames. A hub that sends no hello at all is treated as ledger
whenever a `replay_path` is configured, so a pure wake-hint hub needs no
handshake.

The rest of this section describes the **ledger** model in depth; it is the
durable tier and the one with the strongest ordering guarantee.

### The ordering principle: wake hint + pull replay

The correctness of the watcher rests on one invariant:

> **The replay endpoint is the single ordered source of truth. Live WebSocket
> traffic is only a wake-up hint. The cursor advances only through contiguous
> replay pages.**

The watcher never delivers a live WebSocket payload and never advances the
cursor from one. A live frame — of any content, in any order — only nudges the
watcher to pull replay pages. Delivery and cursor movement happen exclusively
through `replay(after=cursor)`, drained page by page in ledger order, with the
cursor advancing per delivered event.

This makes ordering and at-least-once delivery hold _by construction_ under
sharded fan-out, concurrent publishers, replay/live interleave, and dropped
wake-ups: an out-of-order, duplicated, or missing wake cannot reorder, dupe, or
lose an event, because the wake is never the source of the data.

A periodic **poll tick** (`poll_interval`, default 30s) pulls replay even when
no wake arrives, so the loop self-heals when every wake is lost — a live
WebSocket is an optimization for latency, never a requirement for correctness.

### Wake targets and re-drain

A wake frame may advertise the cursor the server has reached (the field named by
`wake_cursor_field`, default `cursor`). The watcher records the highest such
hint as a **liveness target only** — it is never delivered and never advances
the cursor. When a drain finishes with the cursor still _below_ that target, the
server's replay ceiling lagged the wake (typically an unrelated long transaction
holding the visibility horizon back), so the watcher re-drains on a short,
bounded backoff (a capped exponential from ~0.05s up to `max(3s, poll_interval)`
— 30s at the default `poll_interval`) until the target is reached, instead of
sleeping a full `poll_interval`. The floor is restored only when a fresh wake
raises the target: a bogus or mis-mapped hint that can never be reached — even
while unrelated events keep the cursor inching forward — decays to the poll
cadence rather than pinning the loop at the floor. This closes an
intermittent latency gap where an event is committed and announced but not yet
visible to replay for a few polls: it is delivered the instant replay reveals
it, in order, exactly once — because the re-drain still pulls every event from
the replay pages, never from the hint.

### Reset

When the client's cursor falls below the server's retention floor, the missed
events are gone and cannot be replayed. The server flags the replay response
with `reset=true`; the watcher invokes `on_reset(response)` (so the app can
resync from the producer), advances the cursor past the trimmed gap, and
continues delivering from the floor forward. `on_reset` fires once per cursor
position: a degenerate reset that never advances the cursor (an empty page with
no or a non-integer `cursor_field`) is not re-fired every poll — it fires once
for that stuck position and `resets` counts it once — while a later reset at a
new cursor value fires again.

### Reconnect backoff

An idle wake connection is held open by client keepalive pings (every
`ws_ping_interval`, default 20s), so a quiet hub does not trip a read timeout
and churn reconnects. A ping requires an answer, though: if several consecutive
ping intervals pass with **zero inbound bytes** — a black-holed peer (NAT drop,
power-off, no RST) that a live hub would never resemble — the connection is
declared dead and the reconnect logic takes over, rather than pinging into the
void for hours while wake latency silently degrades to `poll_interval`.

### Silence the client watched, not time that merely passed

A socket read timeout is wall-clock, so a bare count of expired timeouts
conflates two very different facts: _the peer sent nothing_ and _I was not
scheduled to look_. On a loaded host — a starved core, a stop-the-world pause,
a swap storm — a window the kernel closed on time can be observed seconds
later, and the same freeze that blinded the client also delayed the keepalive it
owed the peer and the peer's own reply. Counted naively, a perfectly healthy
idle connection gets torn down exactly when the host can least afford the
reconnect, and on a shared hub every client does it at once: the reconnect
storm lands on the one component already struggling.

So the watcher separates the two, against `time.monotonic()`:

- a missed interval counts only silence the reader actually **watched** — a
  window contributes at most the timeout the kernel was given;
- everything past that (a read that overran its own timeout, plus the gaps
  between two windows) is local **blindness**. It is never charged to the peer,
  and every whole interval of it buys the peer one extra interval of grace.

The grace is capped, so a permanently frozen client still reconnects rather
than staying blind forever. On an unloaded host the stall is ~0 and the rule is
the plain documented one: a black-holed peer is dropped after a few intervals.
Under load the deadline stretches by as much as the reader was provably absent,
and no more — the client stops adding load to a system that is already short of
it, without ever becoming permanently blind to a genuinely dead peer.

Announced frames are capped at `ws_max_frame_bytes` (default 8 MiB) — a larger
frame is rejected before its payload is read — and the upgrade handshake
response header block is itself capped (64 KiB) so a peer that never sends the
terminator cannot grow the read buffer without bound. `recv_json` handles one
whole text/binary frame per message: it does **not** reassemble fragmented
messages and treats continuation frames as unsupported. The wake loop does not
use `recv_json` — it consumes frames tolerantly, treating any complete message
as a wake and reading the cursor only as a best-effort hint: a non-JSON payload
or a fragmented (continuation-split) message is still a wake, reassembled and
drained, never a reconnect trigger, since the wake's content is never the source
of delivered data. A `_WebSocketConnection` is single-thread-at-a-time for
`send`/`recv` — the
watcher uses one reader thread and only `stop()` touches the socket
concurrently, and it touches it in exactly one way: `shutdown(SHUT_RDWR)`, never
`close()`.

That distinction is load-bearing, not stylistic. A reader parked in `recv` is
parked in a `poll()` on that file descriptor; `shutdown` makes the socket
readable at end-of-stream so the reader returns `b""` within microseconds, but
`close()` destroys the descriptor the wakeup was supposed to arrive on. Doing
both back to back is a race — whereupon the reader stays parked until its socket
timeout expires and `stop()` returns having failed to stop anything.

The kernels disagree about which half of this is dangerous, so neither one on
its own is portable:

| cross-thread teardown, reader parked in `recv` | macOS/arm64 | Linux/x86-64 |
| --- | --- | --- |
| `shutdown()` then `close()` — wakeup delivered? | 97/200 | 200/200 |
| `shutdown()` only — wakeup delivered? | 200/200 | 200/200 |
| `close()` only — FIN reaches the peer? | 40/40 | **0/40** |

macOS loses the wakeup roughly half the time when the descriptor is destroyed
underneath the parked reader. Linux never sends a FIN at all for a bare `close()`
while a reader is parked, because the in-flight syscall keeps the socket alive —
so the *peer* is the one left waiting, which is the same bug seen from the other
end. `shutdown()` is the only step both agree on. It is therefore the
cross-thread wakeup, and the reader's own `finally` closes the descriptor.
Connections used from a single thread can keep calling `close()`, which does
both.

For the same reason the socket is published to the watcher the moment it is
created, before the TLS wrap and before the upgrade handshake — each of which
blocks for up to the client `timeout`. Until that window is covered, a `stop()`
arriving mid-handshake finds no connection object to interrupt (`open_websocket`
has not returned one yet) and the thread runs on without it.

The wake WebSocket reconnects with exponential backoff and jitter. Backoff
resets to base after a **healthy session** — one that delivered a wake or stayed
connected past `stable_period` (default 30s) — not on mere connect success: a
hub that accepts then immediately drops keeps backing off instead of being
hammered.

The upgrade handshake itself is bounded by the client's `timeout`, not by
`ws_ping_interval`: a hub that has not answered the upgrade within it is
abandoned and retried on that same backoff. Note what the two sides then see —
the hub accepted a socket and may have counted it, while the client has no
session at all. A retried attempt is not a flap, and `connects` /
`disconnects` (below) count only sessions that were actually established.

### Connection state (is my live feed up?)

A consumer that serves cached state off the feed has a real question to answer:
_is my feed connected right now?_ While it is down nothing invalidates, so
cached values can silently age up to their own TTL. The watcher answers it:

| Member                       | Meaning                                                      |
| ---------------------------- | ------------------------------------------------------------ |
| `connected`                  | the feed session is established right now                    |
| `wait_connected(timeout)`    | block until established; returns the state on return         |
| `wait_disconnected(timeout)` | block until not established (never connected counts)         |
| `connects` / `disconnects`   | sessions established / lost (both climbing = a flapping hub) |
| `keepalives`                 | pings sent holding an idle session up (the only thing that moves against a quiet hub) |
| `peer_timeouts`              | sessions dropped for missing the keepalive deadline           |
| `last_peer_silence`          | that verdict's evidence: `PeerSilence(observed_seconds, stall_seconds, deadline_seconds)` |
| `stall_seconds`              | time this host was provably not scheduled to watch the feed  |

```python
@app.get("/ready")
async def ready(request):
    return Response.json(
        {
            "live_feed": "connected" if watcher.connected else "degraded",
            "flaps": watcher.disconnects,
            # Was it the hub, or was it us? A feed that looks flaky while this
            # climbs is a starved client, not a flapping hub — and reconnecting
            # harder would only make it worse.
            "feed_stall_seconds": round(watcher.stall_seconds, 3),
            "feed_peer_timeouts": watcher.peer_timeouts,
        }
    )
```

`connected` becomes true only once the session's delivery model is settled — the
hub's hello is processed **and any resync it demanded has already been handed to
`on_reset`** — or, for a ledger-intent watcher, once the subscribe frame is away
and ledger delivery is pre-entered. That ordering is a contract: seeing
`connected` means this connect's invalidation has already happened, so no
surprise reset from the same connect lands under a later read. It goes false
when the session drops, and `wait_disconnected` returns only after the session
is fully torn down — every frame that session received has been dispatched, so
nothing can still arrive from it. A poll-only watcher (no `ws_path`) has no feed
and always reports disconnected.

Both waits are the right way to sequence anything that spans a connect or a hub
loss — including tests. Waiting on the transition is exact; sleeping "long
enough" for it is not, and is the classic source of intermittent failures when
the machine is loaded.

They are **level** waits, though: they answer "is it up (yet)?". Use them when
the state persists (a hub that is genuinely down). To detect a transition that
may not persist — a hub that drops and is reconnected milliseconds later — read
the monotonic `connects` / `disconnects` counters instead: a level can flip back
before an observer looks at it, an edge cannot be un-counted.

`connects` counts sessions the client **established**, which is deliberately not
the number of sockets the hub accepted. An attempt the client abandoned — a hub
that took longer than `timeout` to answer the upgrade, say — is a retry, not a
session: it never flipped `connected`, so it is neither a connect nor a
disconnect here, even though the hub counted the socket. "Did my live feed
churn?" is `disconnects`, and only an established session can raise it.

### Callback safety and lifecycle

`on_event` and `on_reset` exceptions are contained (they never kill the loop)
and counted on the watcher (`event_callback_errors`, `reset_callback_errors`).
No drain-side error kills the feed thread either — a `ServiceError` (unreachable
replay, a non-JSON or oversized page) is retried on the next tick with the
cursor untouched, and any other unexpected error is counted at `drain_errors`.
`start()` launches the background threads; `stop(timeout=5.0)` sets the stop
flag, wakes the drain loop, shuts down the wake socket so a blocked receive (or
a blocked handshake read) returns at once, then joins both threads against one
shared `timeout` — `stop(timeout=5)` returns in about five seconds, not five
seconds per thread it happens to own.

**`stop()` means stopped.** When it returns normally, both threads have exited.
If a thread is still running when the deadline expires, `stop()` raises
`ServiceError` naming it rather than returning quietly: a caller that believes
the watcher is gone while its `on_event` can still fire, holding a socket, is
the worse of the two outcomes, and a daemon thread that outlives its `stop()`
is a defect to surface, not a detail to absorb. The one place the stop signal
cannot reach through the transport is the local-resource connect wait (the
`EADDRNOTAVAIL` / `EADDRINUSE` backoff, which holds no socket), so that loop
polls the stop flag directly and abandons a cancelled connect. A drain thread
parked in a retry-backoff `sleep` inside an in-flight replay call can still
overrun a short deadline; pass a `timeout` above the retry backoff ceiling if
your replay endpoint is slow. The ledger cursor is readable at `watcher.cursor`, and the last
in-frame `seq` delivered (catchup's resume key, ephemeral's high-water mark) at
`watcher.last_seq`. A ws-only ephemeral/catchup watcher has no replay drain
thread — `start()`/`stop()` manage only the WebSocket thread.

### Response shape

The watcher expects a replay response shaped like:

```json
{ "events": [{ "id": 42, "...": "..." }], "cursor": 42, "reset": false }
```

The field names (`events`, `cursor`, `reset`, and each event's `id`), the wake
frame's cursor field, and the query parameter names (`after`, `limit`) are all
constructor parameters (`events_field`, `cursor_field`, `reset_field`,
`event_id_field`, `wake_cursor_field`, `after_param`, `limit_param`,
`extra_params`), so a service with different names is expressed without changing
the watcher.

A replay page must be a JSON object; a top-level non-object (for example a bare
JSON array) is rejected as a bad page — counted at `drain_errors`, never
delivered, and the cursor is left untouched for the next tick.
