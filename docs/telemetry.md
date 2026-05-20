# HyperDjango Telemetry

The `hyperdjango.telemetry` package gives you **native Prometheus
metrics + OpenTelemetry-style distributed tracing in one module**,
backed by a lock-free Zig ring buffer and a runtime-dynamic metric
registry. Zero cost when disabled; ≤3% overhead at production
sampling rates; no protobuf anywhere.

- [Why a unified telemetry layer?](#why-a-unified-telemetry-layer)
- [Quick start (5 lines)](#quick-start-5-lines)
- [Architecture](#architecture)
- [Enabling + configuring](#enabling-configuring)
- [Metrics — Counter, Gauge, Histogram](#metrics-counter-gauge-histogram)
- [Tracing — Tracer, Span, context](#tracing-tracer-span-context)
- [Sampling policies](#sampling-policies)
- [Sinks — Prometheus / Stdout / InMemory / custom](#sinks-prometheus-stdout-inmemory-custom)
- [Middleware + background drain](#middleware-background-drain)
- [W3C trace-context propagation](#w3c-trace-context-propagation)
- [Tests — assertions API](#tests-assertions-api)
- [Performance](#performance)
- [Example: bookstore_api](#example-bookstore_api)
- [FAQ](#faq)

## Why a unified telemetry layer?

Most Python frameworks split observability into two packages: one
for Prometheus (counters + histograms) and one for OpenTelemetry
(spans + traces). Both end up doing the same work — allocating a
Python object per event, taking a lock to update state, then
serializing. HyperDjango pushes both into a single Zig-backed
pipeline:

- **Counter / Gauge / Histogram** increments go through atomic RMW
  instructions on pre-registered native handles. ~107 ns per inc
  under ReleaseFast.
- **Spans** claim a slot in a 16384-entry MPSC ring buffer via a
  single CAS on the slot state. Slot layout is compile-asserted
  to 384 bytes (64B header + 64B name + 128B attrs + 128B events). ~107 ns per span_start
  - end cycle.
- **Drain** to Prometheus text + OpenTelemetry JSON dicts happens
  in a dedicated background thread at a configurable interval
  (default 1 s) — never on the request path.

One import, one `enable()`, one middleware, one sink list.

## Quick start (5 lines)

```python
from hyperdjango import HyperApp
from hyperdjango.telemetry import configure_from_settings

app = HyperApp()
telemetry = configure_from_settings(app)
if telemetry is not None and telemetry.prometheus_sink is not None:
    app.get("/metrics")(telemetry.prometheus_sink.handler)
```

Then set `HYPER_TELEMETRY_ENABLED=1` in your environment and hit
`curl http://localhost:8000/metrics`. You'll see per-request HTTP
counters, DB query histograms, and anything else the platform
instruments automatically.

## Architecture

```
┌───────────────────────────────┐
│ Application code              │
│   tracer.start_span("work")   │
│   counter.inc()               │
│   histogram.observe(0.037)    │
└──────────┬────────────────────┘
           │
┌──────────▼─────────────────┐   ┌─────────────────────────┐
│ hyperdjango.telemetry      │   │ TelemetryMiddleware     │
│   Counter / Histogram ─────┼──►│   per-request span      │
│   Tracer / Span            │   │   HTTP metric emit      │
└──────────┬─────────────────┘   │   W3C propagation       │
           │                     └──────────┬──────────────┘
           │ FFI                            │
┌──────────▼─────────────────┐               │ every 1s
│ zig/src/metrics_py.zig     │               ▼
│   atomic counter registry  │   ┌─────────────────────────┐
│   span_ring.zig (16384)    │◄──┤ _DrainWorker (daemon)   │
│   UTF-8 safe truncation    │   │   _span_drain() →       │
└──────────┬─────────────────┘   │   list[dict]            │
           │                     └──────────┬──────────────┘
           │ Prometheus text     │ span batch + metrics text
           └──────────┬──────────┴────────┐
                      ▼                   ▼
           ┌───────────────┐   ┌──────────────────┐
           │ PrometheusSink│   │ StdoutSink /     │
           │   /metrics    │   │ InMemorySink /   │
           │   handler     │   │ user adapter     │
           └───────────────┘   └──────────────────┘
```

## Enabling + configuring

Every telemetry knob is a **first-class HyperDjango setting** in
`hyperdjango.conf`, NOT a hardcoded env-var-only flag. They flow
through the same 4-tier resolution that every other framework
setting uses:

```
Django settings.py  →  HYPER_*  env var  →  .env file  →  DEFAULTS
   (highest)                                                (lowest)
```

So you can set them via whichever layer fits your deployment:

| Source                 | When to use                                                    |
| ---------------------- | -------------------------------------------------------------- |
| `DEFAULTS` dict        | Framework default — shipped value, never edit by hand          |
| `.env` file            | Local dev / CI — checked in or per-environment overlay         |
| `HYPER_*` env var      | Container deployments (k8s, ECS, Heroku, systemd unit)         |
| Django `settings.py`   | Apps using the Django integration — `TELEMETRY_ENABLED = True` |
| `patch.dict(DEFAULTS)` | Tests — `unittest.mock.patch.dict` for fixture setup           |

The full setting list (also visible in
`hyperdjango.conf.SETTING_DEFINITIONS`):

| Setting                          | Default          | Type      | Range / choices              |
| -------------------------------- | ---------------- | --------- | ---------------------------- |
| `TELEMETRY_ENABLED`              | `False`          | bool      | master switch                |
| `TELEMETRY_SERVICE_NAME`         | `hyperdjango`    | str       | tracer name                  |
| `TELEMETRY_SAMPLE_RATIO`         | `0.01`           | float     | 0.0 - 1.0                    |
| `TELEMETRY_DRAIN_INTERVAL`       | `1.0`            | float     | 0.01 - 300.0 seconds         |
| `TELEMETRY_EXTRACT_TRACEPARENT`  | `True`           | bool      | honor inbound W3C header     |
| `TELEMETRY_SINKS`                | `["prometheus"]` | list[str] | prometheus / stdout / memory |
| `TELEMETRY_SPAN_RING_CAPACITY`   | `16384`          | int       | power of 2, 256 - 16777216   |
| `TELEMETRY_AUTO_LOG_CORRELATION` | `True`           | bool      | auto-inject trace_id in logs |

`configure_from_settings()` reads ALL of these via
`hyperdjango.conf.get_setting()`, which means whichever layer you
choose to set them in, the rest of the framework sees the same
value. There is no separate "telemetry env var subsystem".

### 1. Bootstrap from settings (recommended)

```python
from hyperdjango import HyperApp
from hyperdjango.telemetry import configure_from_settings

app = HyperApp()
telemetry = configure_from_settings(app)
if telemetry is not None and telemetry.prometheus_sink is not None:
    app.get("/metrics")(telemetry.prometheus_sink.handler)
```

Then set the values from any layer you want:

```python
# Option A: Django integration
# settings.py
TELEMETRY_ENABLED = True
TELEMETRY_SAMPLE_RATIO = 0.05
TELEMETRY_SPAN_RING_CAPACITY = 65536
```

```bash
# Option B: env vars / .env file
HYPER_TELEMETRY_ENABLED=1
HYPER_TELEMETRY_SAMPLE_RATIO=0.05
HYPER_TELEMETRY_SPAN_RING_CAPACITY=65536
```

```python
# Option C: tests
from unittest.mock import patch
from hyperdjango.conf import DEFAULTS
with patch.dict(DEFAULTS, {"TELEMETRY_ENABLED": True, ...}):
    bootstrap = configure_from_settings(app)
```

`configure_from_settings` auto-wires `app.use(middleware)` and
`app.on_shutdown(middleware.shutdown)`, and returns a
`TelemetryBootstrap` dataclass holding the sinks so you can mount
their handlers.

### 2. Programmatic (full control, no settings)

```python
from hyperdjango.telemetry import (
    Tracer,
    TelemetryMiddleware,
    PrometheusSink,
    StdoutSink,
    enable,
    ParentBased,
    RatioSample,
)

enable()
tracer = Tracer("myapp", sampler=ParentBased(root=RatioSample(0.05)))
prom = PrometheusSink()
middleware = TelemetryMiddleware(
    tracer=tracer,
    sinks=[prom, StdoutSink()],
    drain_interval_seconds=0.5,
)
app.use(middleware)
app.on_shutdown(middleware.shutdown)
app.get("/metrics")(prom.handler)
```

### 3. Tests (enable + InMemorySink)

```python
from hyperdjango.telemetry import (
    InMemorySink,
    TelemetryMiddleware,
    Tracer,
    AlwaysSample,
    enable,
)

enable()
sink = InMemorySink()
tracer = Tracer("t", sampler=AlwaysSample())
mw = TelemetryMiddleware(tracer=tracer, sinks=[sink])
app.use(mw)
# ...drive test requests...
mw.drain_now()
assert len(sink.spans) >= 1
```

## Metrics — Counter, Gauge, Histogram

Four classes cover the full Prometheus type surface:

```python
from hyperdjango.telemetry import Counter, CounterVec, Gauge, Histogram, HistogramVec

# Non-labeled counter — one atomic u64 per process
requests = Counter("myapp_requests_total", "Total requests")
requests.inc()  # +1
requests.inc(5)  # +5

# Labeled counter (CounterVec) — one series per label combo
http = CounterVec(
    "myapp_http_total",
    "HTTP requests by method/status",
    label_names=("method", "status"),
)
http.inc({"method": "GET", "status": "200"})
http.inc_tuple(("POST", "500"))  # fast-path when labels are already ordered

# Gauge — goes up and down
in_flight = Gauge("myapp_in_flight", "In-flight requests")
in_flight.inc()
in_flight.dec()
in_flight.set(42)

# Histogram — default buckets tuned for request durations (ms-scale)
duration = Histogram("myapp_request_duration_seconds", "Request duration")
duration.observe(0.037)
```

**Zero cost when disabled**: every method starts with a `_enabled`
bool check that short-circuits to a return. One `LOAD_GLOBAL` +
branch — ~20-30 ns when telemetry is off.

**Thread-safe**: increments use atomic RMW on the native side, so
you can call them from any thread (including under free-threading)
without locks.

**Label cardinality**: keep the product of possible label values
**below ~1000**. Each unique combination creates a new time series
in memory. Prometheus best-practice applies: never use unbounded
values (user IDs, request IDs, URLs with query strings) as labels.

## Tracing — Tracer, Span, context

```python
from hyperdjango.telemetry import Tracer, STATUS_OK, STATUS_ERROR

tracer = Tracer("myapp")

# Sync context manager
with tracer.start_span("compute_recommendations") as span:
    span.set_attr("user_id", user.id)
    span.set_attr("batch_size", 100)
    result = heavy_work()
    # span.end() is called automatically on exit

# Async context manager
async with tracer.start_span_async("fetch_remote") as span:
    span.set_attr("url", url)
    response = await client.get(url)
    span.set_attr("http.status_code", response.status)


# Decorator (detects async vs sync automatically)
@tracer.trace("list_books")
async def list_books(request): ...


@tracer.trace()  # defaults to fn.__qualname__
def compute_totals(): ...
```

Every span started inside another span inherits the parent's
`trace_id` via `contextvars.ContextVar`. No manual plumbing is
required — nested spans in async handlers, `asyncio.gather`, and
child threads all see the correct parent automatically.

### Status codes

```python
STATUS_UNSET = 0  # default
STATUS_OK = 1
STATUS_ERROR = 2
```

An exception raised inside `start_span()` auto-sets
`status=ERROR` with `error.type` + `error.message` attributes before
re-raising.

### Span attribute model — what fits, what doesn't

Each span slot in the native ring has a **128-byte packed buffer**
for ALL attributes. Each attribute is stored as
`[key_len][val_len][key_bytes][val_bytes]`, so a typical
`http.method=GET` (12 bytes) leaves room for ~10 short attributes.
This is sized for the OpenTelemetry semantic conventions (short
key/value pairs identifying the request, user, tenant, etc.) — not
for arbitrary blobs.

| Attribute kind                         | Fits in 128B? | Where it belongs            |
| -------------------------------------- | ------------- | --------------------------- |
| `http.method`, `http.status_code`      | yes           | span.set_attr               |
| `user.id`, `tenant`, `region`          | yes           | span.set_attr               |
| `db.statement` (short table query)     | usually       | span.set_attr               |
| `http.user_agent` (browser UA string)  | usually       | span.set_attr               |
| Stack trace, full SQL query, JSON body | **no**        | logger w/ trace correlation |
| Long error message + context           | **no**        | logger w/ trace correlation |
| Binary blob, file upload metadata      | **no**        | logger w/ trace correlation |

**Overflow behavior**: when `set_attr()` would exceed the 128-byte
budget, it silently drops the attribute. This is by design — span
recording must never throw or block the request path. The dropped
attributes are visible at drain time via the `attrs_used` field on
the slot (currently exposed only to internal tooling).

**Fast-path methods**: when you know the value type at
call time, use the typed fast-paths instead of the generic
`set_attr`. They skip the 4-branch `isinstance` ladder that
`set_attr` uses to dispatch str / int / float / bool / bytes. On
heavily-instrumented spans (10+ attrs per request) this saves
~40 `isinstance` calls per request.

```python
span.set_attr_str("http.method", request.method)  # known str
span.set_attr_int("http.status_code", response.status)  # known int
span.set_attr_float("db.query_ms", elapsed_ms)  # known float
span.set_attr_bool("cache.hit", True)  # encodes as "true"

span.set_attr("feature.flag", some_user_value)  # polymorphic, use generic
```

`_attach_http_attrs` in `TelemetryMiddleware` uses the fast-paths
for the HTTP semantic attributes, which is where most of the
recorded-span overhead lives. User code is free to mix the generic
and fast-path forms — they produce identical on-the-wire output.

### Long debug payloads — use the logger

For anything that won't fit in 128 bytes, use
`hyperdjango.logging.logger` with trace correlation. Logs flow
through the full structured-logging stack (ConsoleSink, JsonSink,
FileSink, AsyncSink, etc.) and the JSON sink auto-promotes
`trace_id` / `span_id` / `trace_flags` to top-level fields so log
aggregators can join logs to traces with
zero extra mapping config.

**Every log emission inside an active span is automatically
correlated** — `configure_from_settings()` installs a
global `logger.patch()` that reads the active `SpanContext` and
injects `trace_id` / `span_id` / `trace_flags` into the record's
`extra` dict at emission time. You don't need to wrap the logger
explicitly:

```python
from hyperdjango.logging import logger

# TelemetryMiddleware has wrapped the request → a span is active.
logger.error(
    "payment gateway returned non-2xx",
    status=response.status,
    body=response.text[:5000],
    headers=dict(response.headers),
)
# JSON sink output now carries {"trace_id": "...", "span_id": "...", ...}
```

The auto-correlation patcher is enabled by default whenever
`TELEMETRY_ENABLED=True`. To opt out (e.g., if you're composing
your own correlation logic), set
`TELEMETRY_AUTO_LOG_CORRELATION=False` in settings. The patcher:

- is a **no-op** when no span is active (safe at module-load time)
- uses **first-write-wins** for collisions — your own `bind()` /
  `contextualize()` values are never overwritten
- **chains** with any existing `core.patcher` — the user patcher
  runs first, the auto-correlator runs last

The older `bind_trace_context(logger)` helper is still available
and is still the right choice for code paths that run **before**
`configure_from_settings()` (e.g., early boot) or for explicit
one-off correlation outside a traced request:

```python
from hyperdjango.telemetry import bind_trace_context

log = bind_trace_context(logger)
log.warning("something happened outside a request")
```

The JSON sink in `hyperdjango/logging/_sinks.py` auto-promotes
`trace_id` / `span_id` / `trace_flags` to top-level fields, so log
aggregators join logs to traces by
`trace_id` with zero extra mapping config.

### Span events

HyperDjango supports **span events** — timestamped
sub-events packed into a 128-byte per-slot arena. Each event has a
name and a nanosecond timestamp captured at call time:

```python
with tracer.start_span("process_order") as span:
    span.add_event("payment_started")
    result = await charge_card(order)
    span.add_event("payment_completed")
    await send_confirmation_email(order)
    span.add_event("email_queued")
```

Events appear in the drained span dict under `"events"` as a list
matching the OpenTelemetry JSON event schema:

```json
{
  "events": [
    { "name": "payment_started", "time_unix_nano": 1700000000001000000 },
    { "name": "payment_completed", "time_unix_nano": 1700000000003000000 },
    { "name": "email_queued", "time_unix_nano": 1700000000004000000 }
  ]
}
```

**Arena budget:** each event uses `9 + len(name)` bytes (8-byte
timestamp + 1-byte name_len + name). The 128-byte arena holds 4
events with 22-char names, or up to 13 events with 1-char names.
Overflow drops silently (same discipline as attributes — span
recording must never throw on the request path).

**When to use events vs child spans vs logs:**

| Scenario                                       | Use                                            |
| ---------------------------------------------- | ---------------------------------------------- |
| Short timestamp marker ("cache miss", "retry") | `span.add_event("cache_miss")`                 |
| Operation with its own duration + attributes   | child span via `tracer.start_span("sub_work")` |
| Long debug payload (stack trace, request body) | `logger.error(...)` with auto-correlation      |

Events have zero Python-side overhead when the span is a `NoopSpan`
(unsampled path) — `add_event` returns immediately.

## Sampling policies

```python
from hyperdjango.telemetry import AlwaysSample, NeverSample, RatioSample, ParentBased

AlwaysSample()  # record every span (development)
NeverSample()  # zero span records (but trace_id still propagates!)
RatioSample(0.01)  # deterministic 1% head sampling via trace_id hash
ParentBased(root=RatioSample(0.05))  # inherit parent decision; fall through to root
```

`ParentBased` is the recommended production default — it guarantees
that all spans in a single trace are sampled consistently, so you
never end up with orphan child spans in the UI.

`RatioSample` uses the low 32 bits of `trace_id` as the hash input,
so the decision is **deterministic for a given trace** (same trace
→ same decision on every node in the system).

## Sinks — Prometheus / Stdout / InMemory / custom

Sinks implement the `TelemetrySink` Protocol:

```python
@runtime_checkable
class TelemetrySink(Protocol):
    def export_metrics(self, prometheus_text: bytes) -> None: ...
    def export_spans(self, spans: list[dict]) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
```

Four built-ins:

| Sink             | Purpose                               | Span export | Metric export      |
| ---------------- | ------------------------------------- | ----------- | ------------------ |
| `PrometheusSink` | Pull-based `/metrics` HTTP handler    | No-op       | Caches last scrape |
| `StdoutSink`     | JSON lines + fenced Prometheus blocks | Yes         | Yes                |
| `InMemorySink`   | In-process ring for tests             | Yes         | Yes (history)      |
| User adapter     | OTLP-compatible / custom backend      | depends     | depends            |

### PrometheusSink

```python
prom = PrometheusSink()
app.use(TelemetryMiddleware(sinks=[prom], ...))
app.get("/metrics")(prom.handler)
```

On every drain interval the middleware calls
`prom.export_metrics(text)` with the latest exposition bytes. The
HTTP handler serves those bytes directly — no per-scrape
serialization cost. If no drain has happened yet, the handler
falls back to computing the text live.

### Auth-gating the scrape — `mount_gated_metrics`

A `/metrics` body exposes a deployment's metric names and traffic shape, so on
an internet-reachable service the scrape must not be anonymous.
`mount_gated_metrics` registers the sink handler behind an identity gate in one
call instead of hand-rolling the wrapper around `app.get("/metrics")`:

```python
from hyperdjango.telemetry import mount_gated_metrics

mount_gated_metrics(
    app,
    prom.handler,  # the sink handler to protect
    resolve=authenticate_scrape,  # raises HTTPException on failure
    on_deny=record_denied_scrape,  # optional audit / counter hook
    path="/metrics",  # default
)
```

```python
def mount_gated_metrics(
    app,
    handler: Callable[..., Awaitable],
    *,
    resolve: Callable[..., object],
    on_deny: Callable[..., object] | None = None,
    path: str = "/metrics",
) -> None
```

- `resolve(request)` authenticates the caller and raises
  `HTTPException` on failure — typically a thin wrapper over
  [`resolve_identity`](mtls.md#reading-the-verified-identity) that also records
  the resolved method/fingerprint for the app's audit context. Its return value
  is ignored; it may be sync or async (a returned awaitable is awaited).
- `on_deny(request, exc)` (optional) runs before the denial is re-raised — for
  an audit row or a denied-scrape counter. Sync or async.

The gate **fails closed**: only an `HTTPException` from `resolve` triggers
`on_deny` and re-raises; on success the wrapped route returns
`await handler(request)` unchanged. Every hook is a plain callable, so the
helper references no application code.

### StdoutSink

```python
sink = StdoutSink()  # default: sys.stdout
sink = StdoutSink(span_prefix="SPAN ")  # prepend each JSON line
sink = StdoutSink(include_metrics=False)  # suppress metrics, spans only
```

Each span is emitted as one JSON line. Metric scrapes are framed
with `# HYPER_METRICS_BEGIN` / `# HYPER_METRICS_END` markers so
log aggregators can trivially extract them.

### InMemorySink

The canonical test sink. Thread-safe, FIFO-bounded, with read
properties that return snapshot copies:

```python
sink = InMemorySink(max_spans=10_000, max_metric_scrapes=64)
# ...run test...
mw.drain_now()

assert len(sink.spans) == 3
assert sink.spans[0]["name"] == "GET /books"
assert b"hyperdjango_http_requests_total" in sink.latest_metrics
```

### Custom adapter

Implement four methods, pass it in:

```python
class CustomSink:
    def export_metrics(self, prometheus_text: bytes) -> None:
        # push to your backend
        ...

    def export_spans(self, spans: list[dict]) -> None:
        # convert to your span format + POST
        ...

    def flush(self) -> None: ...
    def close(self) -> None: ...


app.use(TelemetryMiddleware(sinks=[CustomSink()]))
```

No inheritance required — the Protocol is `runtime_checkable`, so
`isinstance(sink, TelemetrySink)` works for duck-typed sinks.

## Middleware + background drain

`TelemetryMiddleware` does two things:

1. **Wraps every request in a span** — starts the span on entry,
   attaches HTTP attributes (`http.method`, `http.route`,
   `http.status_code`, `net.peer.ip`, `http.user_agent`), records
   exception status, propagates the active trace-context via
   outbound `traceparent` header on success.
2. **Runs a daemon thread** — every `drain_interval_seconds`
   (default 1.0) it calls `_span_drain()` on the native ring and
   fans the spans + Prometheus scrape text out to every sink.
   Broken sinks don't starve healthy ones (errors are logged to
   stderr and isolated).

Per-request emission:

```
hyperdjango_http_requests_total{method="GET", status="200"}  1
hyperdjango_http_requests_total{method="POST", status="500"} 1
hyperdjango_http_request_duration_seconds_bucket{method="GET", le="0.005"}  7
hyperdjango_http_request_duration_seconds_bucket{method="GET", le="0.01"}   8
hyperdjango_db_queries_total                                42
hyperdjango_db_query_duration_seconds_bucket{le="0.001"}    38
```

Zero-cost when disabled: if `is_enabled()` returns False, the
middleware is a pure passthrough — one bool check and `await
call_next(request)`.

### Shutdown

Always register the shutdown hook so the drain thread exits
cleanly and the final span batch is flushed:

```python
middleware = TelemetryMiddleware(...)
app.use(middleware)
app.on_shutdown(middleware.shutdown)  # ← important
```

`shutdown()` is idempotent. It stops the drain loop, runs one
final drain, then calls `flush()` + `close()` on every sink.

### Periodic samplers — pushing external state into metrics

Some sources of truth live outside the Python state-update path —
pg.zig pool counters owned by Zig under a mutex, async task queue
depths, WebSocket connection counts, cache eviction tallies. These
can't be bumped from a per-request hot path because the state is
owned by a background thread or the native layer.

The `register_sampler(fn)` hook lets subsystems push a snapshot
into Prometheus gauges once per drain tick:

```python
from hyperdjango.telemetry import Gauge, register_sampler
from hyperdjango._hyperdjango_native import _db_pool_stats

_pool_waiters = Gauge(
    "myapp_pool_waiters",
    "Threads currently blocked waiting for a pool connection.",
)
_pool_in_use = Gauge(
    "myapp_pool_in_use_connections",
    "Currently-pinned pool connections.",
)


def _sample_pool_gauges() -> None:
    """Pull the latest pool stats into the Gauges.

    Called by the drain worker every TELEMETRY_DRAIN_INTERVAL
    seconds. Must be cheap — it runs on the drain thread, not on
    the request path.
    """
    stats = _db_pool_stats(pool_handle)
    _pool_waiters.set(int(stats.get("waiters", 0)))
    _pool_in_use.set(int(stats.get("in_use", 0)))


register_sampler(_sample_pool_gauges)
```

Contract:

- Samplers are invoked **once per drain tick** (default every
  1.0 s, tunable via `TELEMETRY_DRAIN_INTERVAL`).
- **Errors are isolated per sampler** — one broken sampler never
  starves the others or crashes the drain thread. Exceptions are
  collected and printed to stderr with the offending function
  name, matching the sink-error channel.
- Registration is **idempotent by identity** — the same function
  registered twice is deduped, so module-level singletons are
  the expected pattern.
- Samplers must be **cheap** — they run on the drain thread
  before the Prometheus exposition is generated, so a slow
  sampler delays every scrape.

Reference implementation: `_sample_pool_gauges` in
`hyperdjango/database.py`, which skips silently when no
`Database` has been instantiated (so telemetry boot doesn't
trigger DB pool creation).

## W3C trace-context propagation

Inbound `traceparent` header is parsed + installed as the parent
context before any local span starts. Outbound responses get a
`traceparent` header pointing at the current span so downstream
services can continue the trace.

```python
from hyperdjango.telemetry import parse_traceparent, format_traceparent

# Parse an inbound header
ctx = parse_traceparent(request.headers.get("traceparent"))
if ctx is not None:
    print(f"Inbound trace: {ctx.trace_id_high:016x}{ctx.trace_id_low:016x}")

# Format for outbound
outbound = format_traceparent(current_span().context)
client.get(url, headers={"traceparent": outbound})
```

`parse_traceparent()` is strict: wrong version, invalid hex, all-zero
reserved values, or malformed format all return `None` without
raising. `format_traceparent()` always emits version `00` + lowercase
hex per the W3C spec.

`tracestate` is supported via `parse_tracestate()` + `format_tracestate()` with the 32-entry + 256-byte-per-entry limits from the spec.

## Tests — assertions API

`TelemetryAssertions` gives you fluent assertions over an
`InMemorySink` buffer:

```python
from hyperdjango.telemetry import InMemorySink, TelemetryAssertions

sink = InMemorySink()
# ...drive the app...
mw.drain_now()
asserts = TelemetryAssertions(sink)

asserts.assert_span_count(3)
asserts.assert_has_span("GET /api/books")
asserts.assert_span_attr("GET /api/books", "http.status_code", "200")
asserts.assert_span_attr_contains("db.query", "sql", "FROM books")
asserts.assert_span_status("POST /fail", STATUS_ERROR)
asserts.assert_no_error_spans()
asserts.assert_span_chain(["GET /books", "db.query", "db.format"])

asserts.assert_metric_present("hyperdjango_http_requests_total")
asserts.assert_metric_has_label(
    "hyperdjango_http_requests_total",
    "method",
    "GET",
)
asserts.assert_metric_label_value(
    "hyperdjango_http_requests_total",
    {"method": "GET", "status": "200"},
    expected=1.0,
)
```

Every assertion raises `AssertionError` with a readable diff.

## Performance

Measured on M-class Apple Silicon, ReleaseFast.

| Operation                          | Cost     | Notes                       |
| ---------------------------------- | -------- | --------------------------- |
| `Counter.inc()` when disabled      | ~25 ns   | branch check only           |
| `Counter.inc()` enabled            | ~117 ns  | atomic RMW via FFI          |
| `CounterVec.inc_tuple()` enabled   | ~180 ns  | label join + atomic RMW     |
| `Histogram.observe()` enabled      | ~200 ns  | bucket find + atomic RMW    |
| `span_start()` + `end()` unsampled | ~75 ns   | flag check + FFI            |
| `span_start()` + `end()` sampled   | ~107 ns  | slot CAS + FFI              |
| `span_start()` + 3 attrs + `end()` | ~282 ns  | slot + 3 attr copies        |
| `span_drain(1000)` per call        | ~1 ms    | background thread only      |
| **Middleware floor — unsampled**   | ~4.25 μs | per-request middleware cost |
| **Middleware floor — sampled_01**  | ~5.25 μs | per-request middleware cost |
| **Middleware floor — sampled_100** | ~6.95 μs | per-request middleware cost |

At production request latencies the relative overhead shrinks
proportionally:

| Baseline (wall-clock)   | Overhead % | Where applicable           |
| ----------------------- | ---------- | -------------------------- |
| 100 μs (pure-CPU route) | 5-7%       | Benchmarks only            |
| 500 μs (fast cache hit) | 1.0-1.4%   | Static content, simple API |
| 1 ms (typical endpoint) | 0.5-0.7%   | Most production routes     |
| 5 ms (complex endpoint) | 0.10-0.14% | DB-heavy read endpoints    |
| 10 ms (slow endpoint)   | 0.05-0.07% | Multi-join writes          |

See `scripts/bench_telemetry_overhead.py` for the reproducible
bench — it checks both a percentage threshold (10% on the
artificial realistic shape) and an absolute ns floor (≤15 μs per
request) to catch regressions without false-positiving on
jitter-dominated microbench measurements.

## Settings reference

Every telemetry knob is a **first-class HyperDjango setting**. The
table below shows the canonical setting name (use this in
`settings.py`, `patch.dict(DEFAULTS, ...)`, and `get_setting(...)`)
and the matching `HYPER_*` env-var alias the conf loader honors.
Both pathways resolve to the same value via
`hyperdjango.conf.get_setting()`.

| Setting (canonical)              | Env-var alias                          | Default          | Meaning                                                         |
| -------------------------------- | -------------------------------------- | ---------------- | --------------------------------------------------------------- |
| `TELEMETRY_ENABLED`              | `HYPER_TELEMETRY_ENABLED`              | `False`          | Master switch. Zero cost when False.                            |
| `TELEMETRY_SERVICE_NAME`         | `HYPER_TELEMETRY_SERVICE_NAME`         | `hyperdjango`    | Default tracer name                                             |
| `TELEMETRY_SAMPLE_RATIO`         | `HYPER_TELEMETRY_SAMPLE_RATIO`         | `0.01`           | Float 0.0 - 1.0, head sampling                                  |
| `TELEMETRY_DRAIN_INTERVAL`       | `HYPER_TELEMETRY_DRAIN_INTERVAL`       | `1.0`            | Seconds between background drain ticks                          |
| `TELEMETRY_EXTRACT_TRACEPARENT`  | `HYPER_TELEMETRY_EXTRACT_TRACEPARENT`  | `True`           | Honor inbound W3C header                                        |
| `TELEMETRY_SINKS`                | `HYPER_TELEMETRY_SINKS`                | `["prometheus"]` | List: any of `prometheus`, `stdout`, `memory`                   |
| `TELEMETRY_SPAN_RING_CAPACITY`   | `HYPER_TELEMETRY_SPAN_RING_CAPACITY`   | `16384`          | Slots in native span ring (power of 2, 256 - 16M)               |
| `TELEMETRY_AUTO_LOG_CORRELATION` | `HYPER_TELEMETRY_AUTO_LOG_CORRELATION` | `True`           | Auto-inject `trace_id`/`span_id`/`trace_flags` into log records |

Set them via whichever layer fits your deployment — Django
`settings.py`, `HYPER_*` env vars, `.env` files, or
`patch.dict(DEFAULTS, ...)` in tests. They're not "env-var-only"
flags; the env var alias is one of four resolution sources.

Resolution order (highest to lowest priority):

1. Django `settings.py` (when using the Django integration)
2. `HYPER_*` environment variable
3. `.env` file in project root
4. Framework `DEFAULTS`

### Tuning the span ring

The span ring is allocated once at startup with a fixed slot count.
Each slot is 384 bytes, so:

| Capacity | Memory | Use case                                   |
| -------- | ------ | ------------------------------------------ |
| 256      | 64 KB  | edge / embedded / unit-test fixtures       |
| 1024     | 256 KB | low-traffic dev environments               |
| 4096     | 1 MB   | small production apps                      |
| 16384    | 6 MB   | **default** — most production apps         |
| 65536    | 16 MB  | high-throughput services (>100k spans/sec) |
| 262144   | 96 MB  | extreme burst headroom                     |

The ring is filled by producers in round-robin order. When a slot
that's still `complete` (not yet drained) is hit on the next wrap,
the producer drops the span and increments `dropped_count`. So the
right capacity is roughly:

```
capacity ≈ peak_spans_per_second × drain_interval × safety_factor (1.5-2.0)
```

For a service producing 10k spans/sec at the default 1-second drain
interval, 16384 gives ~1.6 seconds of burst headroom — plenty.

**Set the capacity:**

- Via env var: `HYPER_TELEMETRY_SPAN_RING_CAPACITY=65536`
- Via Python: `from hyperdjango._hyperdjango_native import _span_configure; _span_configure(65536)` BEFORE the first span

Capacity must be a power of 2 (the slot index uses an AND mask
instead of modulo). The configure call rejects non-powers-of-2 with
`ValueError`. After the first span has been recorded the ring is
locked — `_span_configure` raises `RuntimeError` because resizing
a live ring would dangle in-flight handles.

**Validation order guarantee**: input is always validated FIRST,
regardless of init state. So a typo'd setting (e.g.
`HYPER_TELEMETRY_SPAN_RING_CAPACITY=1023` — not a power of 2)
always raises `ValueError` even if the ring is already operational,
not the more confusing `RuntimeError("already initialized")`.

**Operational state**:

- `_span_capacity()` returns the live capacity (after successful
  init) OR the configured/intended capacity (before init OR after
  failed init).
- `_span_is_operational()` returns `True` only if the ring is
  allocated and recording. `False` before first use AND after a
  failed init (e.g. OOM on a giant capacity). Producers fall back
  to dropping every span when False.
- `_span_dropped_count()` returns the cumulative drop counter —
  alert on this in your monitoring if it grows unexpectedly.

**Failed-init recovery**: if the first allocation fails (OOM), a
subsequent `_span_configure(smaller_value)` rolls back the
init-attempted flag and lets the next span retry allocation. This
is the only case where `configure()` is allowed after init has
been attempted; the ring must be non-operational.

All settings also go through `hyperdjango.conf.SETTING_DEFINITIONS`
— see `test_settings.py` for the validation rules.

## Example: bookstore_api

A full working example lives in `services/bookstore_api/app.py`.
Key lines:

```python
from hyperdjango.telemetry import (
    PrometheusSink,
    TelemetryMiddleware,
    Tracer,
    enable as _enable_telemetry,
)
from hyperdjango.telemetry.sampling import ParentBased, RatioSample

if os.environ.get("HYPER_TELEMETRY_DISABLE") != "1":
    _enable_telemetry()
    tracer = Tracer(
        "bookstore_api",
        sampler=ParentBased(root=RatioSample(1.0 if app.debug else 0.05)),
    )
    prom = PrometheusSink()
    telemetry = TelemetryMiddleware(
        tracer=tracer,
        sinks=[prom],
        drain_interval_seconds=1.0,
    )
    app.use(telemetry)
    app.on_shutdown(telemetry.shutdown)
    app.get("/metrics")(prom.handler)
```

Run the example, hit a few endpoints, then `curl /metrics` to
see the full Prometheus exposition including the auto-emitted
`hyperdjango_http_*` + `hyperdjango_db_*` series.

## FAQ

**Q: Does this replace `PerformanceMiddleware`?**
No. `PerformanceMiddleware` is a dashboard-focused debug tool
(slow query log, N+1 detection, `/debug/performance`).
`TelemetryMiddleware` is a production metrics + tracing pipeline.
They coexist fine — bookstore_api uses both.

**Q: Can I use protobuf / OTLP?**
You can ship spans to any upstream OTLP collector by writing a
custom sink that wraps `opentelemetry-exporter-otlp`. The
framework itself never touches protobuf — span batches are
always `list[dict]` in OpenTelemetry JSON shape.

**Q: Do I need a separate trace collector?**
No, but you can. Point any OTLP-compatible SDK at your custom
sink. For dev / small-scale, `StdoutSink` + a log forwarder
is often enough.

**Q: What about trace sampling at multi-tenant scale?**
`RatioSample` is deterministic via the trace_id hash, so the
same trace is sampled consistently across every service. For
tenant-aware sampling, wrap `ParentBased` in your own policy
that inspects `current()` and varies the ratio per tenant.

**Q: Is telemetry safe under free-threading (3.14t)?**
Yes. Every native operation uses atomic RMW instructions.
ContextVar is per-thread / per-task. The span ring uses CAS for
slot state transitions. We run a 8-thread × 1000-iter fuzz
suite under free-threading in `test_span_ring_fuzz.py`.

**Q: How much memory does the span ring use?**
16384 slots × 384 bytes = 6 MB, allocated once at module load.
Each slot is a fixed-size `extern struct` — no per-span
allocations during normal operation. When the ring fills under
sustained load, `dropped_count` increments and oldest spans are
overwritten (never memory exhaustion).

## Production deployment — Prometheus end-to-end

### 1. Enable telemetry in your app

```python
# settings.py (Django integration)
TELEMETRY_ENABLED = True
TELEMETRY_SERVICE_NAME = "myapp"
TELEMETRY_SAMPLE_RATIO = 0.05  # 5% head sampling
TELEMETRY_SINKS = ["prometheus"]
TELEMETRY_SPAN_RING_CAPACITY = 32768  # 8 MB, for high-throughput services
```

Or via env vars (systemd, Docker, k8s):

```bash
HYPER_TELEMETRY_ENABLED=1
HYPER_TELEMETRY_SERVICE_NAME=myapp
HYPER_TELEMETRY_SAMPLE_RATIO=0.05
HYPER_TELEMETRY_SINKS=prometheus
HYPER_TELEMETRY_SPAN_RING_CAPACITY=32768
```

### 2. Prometheus scrape config

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "hyperdjango"
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets:
          - "myapp.internal:8000"
        labels:
          service: myapp
          environment: production
    # If behind a load balancer, use service discovery instead:
    # dns_sd_configs:
    #   - names: ["_http._tcp.myapp.service.consul"]
```

### 3. Key series to monitor

The telemetry stack emits these series automatically when
`TELEMETRY_ENABLED=True`:

| Series                                         | Type         | Labels         | What it measures                                 |
| ---------------------------------------------- | ------------ | -------------- | ------------------------------------------------ |
| `hyperdjango_http_requests_total`              | CounterVec   | method, status | Request rate by HTTP method + status code        |
| `hyperdjango_http_request_duration_seconds`    | HistogramVec | method         | Request latency distribution (p50/p95/p99)       |
| `hyperdjango_db_queries_total`                 | Counter      | —              | Total DB queries across all endpoints            |
| `hyperdjango_db_query_duration_seconds`        | Histogram    | —              | DB query latency distribution                    |
| `hyperdjango_rate_limit_hits_total`            | CounterVec   | backend        | Rate-limit denials (RateLimitMiddleware)         |
| `hyperdjango_csrf_violations_total`            | CounterVec   | reason         | CSRF token failures (missing/mismatch)           |
| `hyperdjango_session_auth_total`               | CounterVec   | result         | Session auth outcomes (ok/no_cookie/invalid/...) |
| `hyperdjango_guard_denials_total`              | CounterVec   | reason         | HyperGuard access denials by reason              |
| `hyperdjango_template_renders_total`           | Counter      | —              | Template render count                            |
| `hyperdjango_template_render_duration_seconds` | Histogram    | —              | Template render latency                          |
| `hyperdjango_dataloader_loads_total`           | CounterVec   | result         | DataLoader hit/miss rate                         |
| `hyperdjango_dataloader_batch_size`            | Histogram    | —              | DataLoader batch size distribution               |
| `hyperdjango_admin_actions_total`              | CounterVec   | model, action  | Admin write actions (add/change/delete)          |
| `hyperdjango_pool_total_connections`           | Gauge        | —              | Configured pool size                             |
| `hyperdjango_pool_in_use_connections`          | Gauge        | —              | Currently-pinned connections                     |
| `hyperdjango_pool_waiters`                     | Gauge        | —              | Threads blocked waiting for connections          |

### 4. Dashboard panel queries (PromQL)

**HTTP RPS by status (top panel):**

```
rate(hyperdjango_http_requests_total{service="myapp"}[5m])
```

Legend: `{{method}} {{status}}`

**Request latency p50/p95/p99:**

```
histogram_quantile(0.50, rate(hyperdjango_http_request_duration_seconds_bucket{service="myapp"}[5m]))
histogram_quantile(0.95, rate(hyperdjango_http_request_duration_seconds_bucket{service="myapp"}[5m]))
histogram_quantile(0.99, rate(hyperdjango_http_request_duration_seconds_bucket{service="myapp"}[5m]))
```

**DB query rate:**

```
rate(hyperdjango_db_queries_total{service="myapp"}[5m])
```

**Pool saturation (early warning):**

```
hyperdjango_pool_waiters{service="myapp"}
hyperdjango_pool_in_use_connections{service="myapp"} / hyperdjango_pool_total_connections{service="myapp"}
```

**Error rate:**

```
sum(rate(hyperdjango_http_requests_total{service="myapp", status=~"5.."}[5m]))
  /
sum(rate(hyperdjango_http_requests_total{service="myapp"}[5m]))
```

### 5. Alerting rules

```yaml
# prometheus_rules.yml
groups:
  - name: hyperdjango
    rules:
      # 5xx error rate > 5% over 5 minutes
      - alert: HighErrorRate
        expr: |
          sum(rate(hyperdjango_http_requests_total{status=~"5.."}[5m]))
          / sum(rate(hyperdjango_http_requests_total[5m]))
          > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High 5xx error rate ({{ $value | humanizePercentage }})"

      # p99 latency > 2 seconds
      - alert: HighLatency
        expr: |
          histogram_quantile(0.99, rate(hyperdjango_http_request_duration_seconds_bucket[5m]))
          > 2.0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "p99 latency above 2s ({{ $value | humanizeDuration }})"

      # Pool saturation — threads waiting for connections
      - alert: PoolSaturation
        expr: hyperdjango_pool_waiters > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "{{ $value }} threads waiting for DB pool connections"

      # Rate limiting firing — potential abuse or misconfiguration
      - alert: RateLimitSpike
        expr: rate(hyperdjango_rate_limit_hits_total[5m]) > 10
        for: 5m
        labels:
          severity: info
        annotations:
          summary: "Rate limiter triggering at {{ $value | humanize }}/s"
```

### 6. systemd unit example

```ini
# /etc/systemd/system/myapp.service
[Unit]
Description=MyApp (HyperDjango)
After=postgresql.service

[Service]
User=myapp
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/.venv/bin/uv run hyper start --app app:app
Environment=HYPER_TELEMETRY_ENABLED=1
Environment=HYPER_TELEMETRY_SERVICE_NAME=myapp
Environment=HYPER_TELEMETRY_SAMPLE_RATIO=0.05
Environment=DATABASE_URL=postgres://myapp:secret@localhost/myapp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 7. Kubernetes ConfigMap + Deployment

```yaml
# configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: myapp-config
data:
  HYPER_TELEMETRY_ENABLED: "1"
  HYPER_TELEMETRY_SERVICE_NAME: "myapp"
  HYPER_TELEMETRY_SAMPLE_RATIO: "0.05"
  HYPER_TELEMETRY_SPAN_RING_CAPACITY: "32768"
---
# deployment.yaml (relevant snippet)
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    metadata:
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: "/metrics"
    spec:
      containers:
        - name: myapp
          envFrom:
            - configMapRef:
                name: myapp-config
          ports:
            - containerPort: 8000
              name: http
```

### 8. OTLP span export (optional)

For distributed tracing via any OTLP/HTTP backend, add the
example OTLP sink from `services/otlp_sink/`:

```python
from services.otlp_sink import OTLPSpanSink

sink = OTLPSpanSink(service_name="myapp")
# Or configure via env vars:
#   OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.internal:4318
#   OTEL_SERVICE_NAME=myapp

telemetry = configure_from_settings(app)
telemetry.middleware._worker.sinks.append(sink)
```

This sends span batches to an OTLP/HTTP collector in JSON format
(no protobuf dependency). Pair with `PrometheusSink` for metrics
and `OTLPSpanSink` for traces — both sinks coexist on the same
middleware with zero conflict.

---

_See also: `docs/TelemetryArchitecturePlan.md` for the original
design doc and `scripts/test_e2e_telemetry.py` for the end-to-end
integration test that exercises every feature described above._
