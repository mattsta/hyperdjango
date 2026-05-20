# HTTP server: connection models

The native HTTP server (`zig/src/server.zig`) is an accept loop, a pool of `W`
worker threads (`THREAD_POOL_SIZE`, GIL-free under free-threaded Python), and —
in reactor mode — one or more kqueue/epoll reactor threads (`reactor.zig`).
`HTTP_SERVER_MODEL` selects the connection model.

## Reactor (default)

Idle keep-alive connections wait in a kqueue (macOS/BSD) or epoll (Linux) reactor
and consume **zero worker threads** — an idle connection costs only an fd. When a
connection becomes readable, the reactor hands the whole fd to a worker for
**exactly one request** (`handleConnectionOnce` → `handleOneRequest`); the worker
serves it and returns the fd to the reactor, which re-arms read interest for the
next request (keep-alive) or closes.

Because a worker holds a connection only while a request is in flight, live
connection count is bounded by fds/memory, not by `W`. All connections share the
worker pool fairly, and the server holds tens of thousands of mostly-idle
connections. Per request there is one reactor→worker→reactor round-trip, so
latency has a ~0.5 ms floor (the dispatch/wakeup handoff) that is pure overhead on
requests whose own work is near zero; on real work (a DB query, a template
render) it is negligible.

`HTTP_REACTOR_COUNT` (default 1) sets the number of independent reactor groups,
each a reactor thread + work queue + subset of workers. One reactor saturates
well below the core count; more groups trade a little peak throughput for dispatch
parallelism on many-core hosts.

### fd ownership — the safety property

An fd is owned by the reactor **XOR** exactly one worker at any instant, and only
the reactor thread ever registers/unregisters fds (workers and the acceptor hand
connections to the reactor through a `register_queue` it drains). No socket is
ever read or written by two threads, so there is no double-close and no concurrent
I/O on one fd. HTTP/1.1 response ordering holds trivially: a worker owns the fd for
the whole of the one request it serves.

The reactor owns only the **idle wait** between requests. Active-request read/write
runs on the worker, so a slow-client _write_ or a slowloris _mid-body_ occupies a
worker for that one request's duration (bounded by `SO_RCVTIMEO`).

## Threaded

`handleConnection` pins a worker to a keep-alive connection for that connection's
whole life: it loops `handleOneRequest`, blocking on the socket read between
requests. So **at most `W` connections are served at once**, and a pinned worker
never returns to the others.

Past `W` connections this is a fairness collapse, not an even slow-down. With
`W=4` and 64 idle keep-alive connections (25 ms think-time), 4 connections receive
~76 requests each while the other 60 receive a single response and then get no
worker again; aggregate throughput is frozen at ≈`W`-connections-worth. A
`served %` counting "≥1 response" reads 100 % and hides this — the per-connection
distribution is the honest picture. At higher connection counts even the initial
response stops arriving once the accept backlog (`kern.ipc.somaxconn`) fills.

### Load-shedding

Once the accept backlog exceeds `HTTP_MAX_PENDING` (default `THREAD_POOL_SIZE × 8`)
the accept loop answers new connections with `503 Service Unavailable` +
`Connection: close` instead of queuing them to starve. The client gets an
immediate retry/failover signal. `HYPER_HTTP_MAX_PENDING=0` disables shedding
(unbounded queue). Reactor mode does not shed — it holds all connections.

## Choosing a model

- **Reactor** scales to many connections and degrades gracefully (latency rises
  evenly). It is the default and the right choice for public / general web
  serving, browser keep-alives, and high connection counts.
- **Threaded** has ~10 % higher peak throughput and lower per-request latency for
  _busy_ connections, with no dispatch hop — but only serves up to `W` connections
  and sheds the rest. Use it for an internal, high-RPS service whose concurrent
  connection count is known to stay ≤ `W` (e.g. behind a connection-pooling proxy).

Measured on an 18-core box (`benchmarks/http`, wrk / async client), `W=18`:

| Workload                                 | Reactor                | Threaded                                            |
| ---------------------------------------- | ---------------------- | --------------------------------------------------- |
| Peak throughput, busy connections, tiny  | ~169k req/s            | ~188k req/s                                         |
| 4096 idle keep-alive conns (25 ms think) | ~69k req/s, all served | 666 req/s, ~43% get any response (rest shed as 503) |
| 128 connections, W=1                     | ~120k req/s            | ~45k req/s                                          |

## Configuration

| Setting (`HYPER_`-prefixed env) | Default              | Effect                                            |
| ------------------------------- | -------------------- | ------------------------------------------------- |
| `HTTP_SERVER_MODEL`             | `reactor`            | `reactor` or `threaded`                           |
| `THREAD_POOL_SIZE`              | ≈ CPU cores          | worker threads `W`                                |
| `HTTP_REACTOR_COUNT`            | `1`                  | reactor groups (reactor mode)                     |
| `HTTP_MAX_PENDING`              | `THREAD_POOL_SIZE×8` | threaded load-shedding backlog cap (`0` disables) |

## Platform abstraction

`Reactor` (`reactor.zig`) wraps kqueue (macOS/BSD) and epoll (Linux) —
register/modify/unregister fd interest, wait, and a self-pipe wakeup — the two
platforms the server targets.
