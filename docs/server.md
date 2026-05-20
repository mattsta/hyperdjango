# Server

Native Zig HTTP server -- 2.1x faster than uvicorn (13k vs 6k req/s on an 18-core box; 479-548k vs 239k req/s = 2.3x at W=64 on a 64-core pin, c=1024, NODELAY-corrected uvicorn).

## Architecture

The HyperDjango server is a compiled Zig native extension that handles HTTP parsing, routing, WebSocket upgrades, and response serialization entirely in native code. Key architectural components:

- **Capacity-scaled worker pool** -- pre-spawned OS threads (auto-sized to usable cores, floor 24) handle connections concurrently without the GIL
- **Radix trie router** -- O(log n) route matching with 528 ns per resolve for dynamic routes
- **Zero-copy request parsing** -- HTTP headers and body parsed without unnecessary allocations
- **Native response builder** -- responses serialized directly to socket buffers
- **RFC 6455 WebSocket** -- full WebSocket support with SIMD XOR unmasking

The server is designed for Python 3.14t (free-threaded) and runs request handlers without GIL contention.

## Quick Start

```python
from hyperdjango import HyperApp

app = HyperApp("myapp")


@app.get("/")
async def index(request):
    return {"message": "Hello!"}


@app.post("/users")
async def create_user(request):
    data = request.json
    return {"created": data["name"]}, 201


app.run(host="0.0.0.0", port=8000)
```

## Server Configuration

### app.run() Parameters

```python
app.run(
    host="0.0.0.0",  # Bind address (default: "127.0.0.1")
    port=8000,  # Bind port (default: 8000)
    prod=False,  # Production mode (disables debug pages, enables optimizations)
)
```

| Parameter | Default       | Description                                                             |
| --------- | ------------- | ----------------------------------------------------------------------- |
| `host`    | `"127.0.0.1"` | Network interface to bind to. Use `"0.0.0.0"` for all interfaces.       |
| `port`    | `8000`        | TCP port to listen on                                                   |
| `prod`    | `False`       | Production mode -- disables debug error pages, enables security headers |

### Production Mode

When `prod=True`:

- Detailed error pages are replaced with generic `{"detail": "Internal Server Error"}`
- Security headers are applied (HSTS, X-Frame-Options, etc.)
- Debug endpoints like `/debug/performance` are disabled
- Static file serving defers to the reverse proxy

```python
app.run(host="0.0.0.0", port=8000, prod=True)
```

## Routing

### HTTP Methods

```python
@app.get("/users")
async def list_users(request): ...


@app.post("/users")
async def create_user(request): ...


@app.put("/users/{id:int}")
async def update_user(request, id): ...


@app.patch("/users/{id:int}")
async def patch_user(request, id): ...


@app.delete("/users/{id:int}")
async def delete_user(request, id): ...
```

### Path Parameters with Type Conversion

```python
# Integer parameter -- auto-converted to int
@app.get("/users/{id:int}")
async def get_user(request, id):  # id is already an int
    ...


# String parameter (default)
@app.get("/users/{username:str}")
async def get_by_name(request, username): ...


# Slug parameter (letters, numbers, hyphens)
@app.get("/articles/{slug:slug}")
async def get_article(request, slug): ...


# UUID parameter
@app.get("/items/{item_id:uuid}")
async def get_item(request, item_id):  # item_id is a UUID string
    ...


# Path parameter (catches remaining path segments)
@app.get("/files/{path:path}")
async def serve_file(request, path):  # path = "docs/guide/intro.md"
    ...
```

Supported parameter types:

| Type   | Pattern         | Example Match                          |
| ------ | --------------- | -------------------------------------- |
| `str`  | `[^/]+`         | `hello`                                |
| `int`  | `[0-9]+`        | `42`                                   |
| `slug` | `[a-zA-Z0-9-]+` | `my-article`                           |
| `uuid` | UUID v4         | `550e8400-e29b-41d4-a716-446655440000` |
| `path` | `.+`            | `docs/guide/intro.md`                  |

### Multiple Methods

```python
@app.route("/items", methods=["GET", "POST"])
async def items(request):
    if request.method == "GET":
        return await list_items()
    else:
        return await create_item(request)
```

### URL Namespaces & Includes

Organize routes with routers and namespaces:

```python
from hyperdjango.router import Router

blog = Router()
blog.get("/", list_posts, name="list")
blog.get("/{id:int}", post_detail, name="detail")
blog.post("/{id:int}/comment", add_comment, name="comment")

api = Router()
api.get("/users", list_users, name="users")
api.get("/stats", get_stats, name="stats")

# Mount routers with namespaces
app.router.include("/blog", blog, namespace="blog")
app.router.include("/api/v1", api, namespace="api")

# Reverse URL resolution
app.router.reverse("blog:detail", id=42)  # "/blog/42"
app.router.reverse("blog:list")  # "/blog/"
app.router.reverse("api:users")  # "/api/v1/users"
```

### Radix Trie Router

The router uses a native Zig radix trie data structure for route matching. This provides O(log n) lookups regardless of the number of routes, with benchmark performance of 528 ns per resolve for dynamic routes with parameters.

Static routes (no parameters) are resolved even faster via exact hash map lookup.

## Request Object

```python
request.method  # "GET", "POST", "PUT", "PATCH", "DELETE"
request.path  # "/users/42"
request.query  # {"page": "1", "sort": "name"}
request.json  # Parsed JSON body (lazy)
request.form  # Parsed form data (lazy)
request.headers  # Dict of headers (case-insensitive keys)
request.cookies  # Dict of cookies (native Zig parser)
request.client_ip  # Client IP address (respects X-Forwarded-For)
request.is_secure  # True if HTTPS (checks X-Forwarded-Proto)
request.text  # Raw body as string
request.bytes  # Raw body as bytes
```

The request object uses lazy parsing -- `request.json`, `request.form`, and `request.cookies` are only parsed when first accessed. Cookie parsing uses the native Zig cookie parser with percent-decoding, benchmarking faster than Python's `http.cookies` module.

## Response Builder

```python
from hyperdjango.response import Response

# JSON response (most common)
Response.json({"key": "value"})
Response.json(data, status=201)

# HTML response
Response.html("<h1>Hello</h1>")
Response.html(rendered_template, status=200)

# Plain text
Response.text("plain text")
Response.text("Not Found", status=404)

# Redirect
Response.redirect("/other")  # 302 temporary
Response.redirect("/other", status=301)  # 301 permanent

# File download
Response.file("/path/to/file.pdf")
Response.attachment("/path/to/file.pdf", filename="report.pdf")

# Streaming response
Response.stream(async_generator)

# Server-Sent Events
Response.sse(event_generator)
```

Response objects support headers, cookies, and caching:

```python
response = Response.json({"ok": True})
response.headers["X-Custom"] = "value"
response.headers["Set-Cookie"] = "session=abc; HttpOnly; Secure"
```

The native response builder serializes headers and body directly to the socket buffer, avoiding intermediate string concatenation.

## Middleware

```python
from hyperdjango import RateLimitMiddleware
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityMiddleware,
    TimingMiddleware,
    LoggingMiddleware,
    CompressionMiddleware,
    CSRFMiddleware,
)

# CORS
app.use(
    CORSMiddleware(
        origins=["https://mysite.com"],
        methods=["GET", "POST", "PUT", "DELETE"],
        headers=["Authorization", "Content-Type"],
        max_age=3600,
    )
)

# Security headers (HSTS, X-Frame-Options, etc.)
app.use(SecurityMiddleware(hsts=True, frame_deny=True))

# Request timing (adds X-Response-Time header)
app.use(TimingMiddleware())

# Access logging
app.use(LoggingMiddleware())

# Rate limiting
app.use(RateLimitMiddleware(max_requests=100, window=60))

# Response compression (gzip)
app.use(CompressionMiddleware(min_size=1024))

# CSRF protection
app.use(CSRFMiddleware(secret="your-secret"))
```

Middleware executes in the order it is registered. Each middleware receives the request and a `call_next` function:

```python
async def custom_middleware(request, call_next):
    # Before handler
    start = time.time()

    response = await call_next(request)

    # After handler
    elapsed = time.time() - start
    response.headers["X-Custom-Time"] = f"{elapsed:.3f}s"
    return response


app.use(custom_middleware)
```

## WebSocket Support

WebSocket connections are handled with full RFC 6455 compliance. The Zig
server performs the HTTP upgrade handshake and SIMD XOR unmasking of frames
natively, then hands the connection to your Python handler.

A handler is an `async def` registered with `@app.websocket(path)`. It is
called **once per connection**; you drive the connection with the methods on
the `ws` object. The canonical echo handler:

```python
from hyperdjango.websocket import WebSocketDisconnect


@app.websocket("/ws/echo")
async def echo(ws):
    await ws.accept()  # complete the handshake
    async for message in ws.iter_text():  # iterate frames until the client leaves
        await ws.send_text(message)
```

### WebSocket API

The `ws` object exposes a consistent, correctly-typed API (identical on the
native and ASGI backends, so handlers are portable):

```python
@app.websocket("/ws/chat")
async def chat(ws):
    await ws.accept(subprotocol=None)  # must be called before I/O

    # --- send ---
    await ws.send_text("hello")  # text frame
    await ws.send_bytes(b"\x00\x01")  # binary frame
    await ws.send_json({"type": "hi"})  # JSON-encoded text frame

    # --- receive (each raises WebSocketDisconnect when the client goes away) ---
    msg = await ws.receive()  # type-preserving: str for a text frame,
    #   bytes for a binary frame (use for mixed protocols)
    text = await ws.receive_text()  # always str  (a binary frame is UTF-8 decoded)
    data = await ws.receive_bytes()  # always bytes (a text frame is UTF-8 encoded)
    obj = await ws.receive_json()  # parsed JSON

    # --- iterate (each stops cleanly on disconnect) ---
    async for m in ws.iter_text():
        ...  # yields str
    async for b in ws.iter_bytes():
        ...  # yields bytes
    async for o in ws.iter_json():
        ...  # yields parsed JSON

    # --- close ---
    await ws.close(code=1000, reason="Normal closure")
```

Pick the receive method by protocol: `receive_text()`/`iter_text()` for a
text protocol, `receive_bytes()`/`iter_bytes()` for a binary protocol, and
`receive()` when a single connection carries **both** frame types (it
reflects the frame type in the return type — the right choice for an echo or
proxy).

Connection metadata is available as attributes: `ws.headers` (dict),
`ws.path`, `ws.query_string`. Handle disconnects by catching
`WebSocketDisconnect` (the `iter_*` helpers swallow it and stop cleanly):

```python
from hyperdjango.websocket import WebSocketDisconnect


@app.websocket("/ws/chat")
async def chat(ws):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            await handle(msg)
    except WebSocketDisconnect:
        pass  # client closed — run any cleanup here
```

### Concurrency model — and how to write handlers that scale

Each connection's handler runs on an asyncio event loop, and the receive
path is fully non-blocking (native `MSG_DONTWAIT` reads driven by the loop's
selector — no thread parked per message). There are two models for _how_
loops map to connections, selected by the `WEBSOCKET_CONCURRENCY` setting:

- **`shared` (the default) — shared event-loop pool.** A small fixed set of
  event loops (one per core) each multiplex _many_ connections. The
  concurrent-connection ceiling is bounded by fds/memory (not the thread
  pool), throughput uses every core, and memory stays ~flat as connections
  grow. This is the right default for real WebSocket workloads (chat,
  notifications, live dashboards — many mostly-idle clients) and still
  measures **1.6–2.3× faster on throughput than the single-loop `websockets`
  library** while holding far more connections (see [Benchmarks](benchmarks.md)).

- **`thread` (opt-out) — one OS thread per connection.** Set
  `WEBSOCKET_CONCURRENCY=thread` (or `HYPER_WEBSOCKET_CONCURRENCY=thread`).
  Max live connections equals the thread pool size. Only needed for handlers
  that do heavy _synchronous_ per-message CPU work and cannot be made
  cooperative — most handlers should use the default.

**Cooperative-handler contract (required — the default depends on it):** a
handler may `await` network I/O and CPU-light work freely, but must **not
park a thread per connection** — e.g.
`await loop.run_in_executor(None, blocking_queue.get)`. On a shared loop,
many such handlers exhaust the loop's default executor and stall. When you
need to feed a handler from callbacks that fire on other threads
(channel/pub-sub delivery), use a cooperative bridge: an `asyncio.Queue` fed
via `loop.call_soon_threadsafe`, with the writer as a task. (The framework's
own `websocket_channel_handler` and the `Room`/`Channel` helpers already do
this for you — you only need the pattern below when hand-rolling a bridge.)

```python
import asyncio, contextlib


@app.websocket("/ws/room")
async def room(ws):
    await ws.accept()
    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def on_event(msg):  # called from ANY thread
        with contextlib.suppress(RuntimeError):  # loop closing/closed
            loop.call_soon_threadsafe(outgoing.put_nowait, msg)

    sub_id = channel.subscribe(on_event)  # your pub/sub source

    async def writer():
        while True:
            await ws.send_json(await outgoing.get())

    w = asyncio.create_task(writer())
    try:
        async for incoming in ws.iter_json():
            await channel.publish(incoming)  # read side
    except WebSocketDisconnect:
        pass
    finally:
        w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await w
        channel.unsubscribe(sub_id)
```

This pattern works identically under both models. The `services/websocket_chat`
app uses exactly this shape.

### Tuning

These are HyperDjango settings — set them in Django settings
(`HYPERDJANGO_<NAME>`), via `HYPER_<NAME>` environment variables, or in a
`.env` file. Defaults are chosen for good production behavior out of the box.

| Setting                   | Default    | Effect                                                                                                                                                                         |
| ------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `WEBSOCKET_CONCURRENCY`   | `shared`   | `shared` = multiplex connections over an event-loop pool (no connection ceiling, the recommended default). `thread` = one OS thread per connection (max = `THREAD_POOL_SIZE`). |
| `WEBSOCKET_LOOP_COUNT`    | `0` (auto) | Event loops in the shared pool. `0` = `min(cpu_count, 8)`. More loops = more parallelism up to core count.                                                                     |
| `THREAD_POOL_SIZE`        | 24         | HTTP worker threads. In `thread` WebSocket mode, also the max concurrent WebSocket connections.                                                                                |
| `HYPER_THREAD_STACK_SIZE` | 16 MiB     | Per-worker-thread stack (env-only). Lower it (bytes) for many-connection, shallow-call-depth workloads to cut memory; keep the default if handlers have deep call chains.      |

### SIMD XOR Unmasking

WebSocket frames from clients are masked per RFC 6455. The Zig implementation uses SIMD operations to XOR-unmask 16 bytes at a time, significantly faster than byte-by-byte unmasking. Server→client frames are written with a single `writev` syscall (header + payload) and `TCP_NODELAY` is set on every connection to minimize per-message latency.

## Request Validation Pipeline

Incoming requests pass through a validation pipeline before reaching the route handler:

1. **Connection accept** -- TCP connection accepted by the thread pool
2. **HTTP parsing** -- request line, headers, and body parsed in Zig
3. **Size validation** -- body size checked against `max_body_size` (default 10 MB)
4. **Routing** -- radix trie lookup to find matching handler
5. **Parameter extraction** -- path parameters extracted and type-converted
6. **Middleware chain** -- request passes through middleware stack
7. **Handler execution** -- route handler called with validated request

If any step fails, an appropriate HTTP error response is returned immediately without invoking downstream handlers.

## Connection Keep-Alive

The server supports HTTP/1.1 keep-alive connections by default. Multiple requests can be sent over the same TCP connection, reducing connection establishment overhead.

Keep-alive behavior:

- Connections are kept alive unless the client sends `Connection: close`
- Idle connections are reaped after a configurable timeout
- The server sends `Connection: keep-alive` in responses

### Socket Tuning

The native server exposes a few low-level socket knobs via environment variables (independent of the `HYPER_HTTP_SERVER_MODEL` threaded/reactor choice — both modes honor them):

| Variable               | Default  | Effect                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `HYPER_TCP_NODELAY`    | `1` (on) | Sets `TCP_NODELAY` on every accepted connection, disabling Nagle's algorithm. Without it, Nagle interacting with the peer's delayed ACK can add up to ~40 ms of latency to small keep-alive responses. Set `HYPER_TCP_NODELAY=0` to restore Nagle.                                                                                                                                                                                        |
| `HYPER_LISTEN_BACKLOG` | `1024`   | Depth of the kernel accept queue (`listen()` backlog). The kernel clamps this to the system `somaxconn` limit — raise `kern.ipc.somaxconn` (macOS) / `net.core.somaxconn` (Linux) via `sysctl` if you need a deeper queue under connection storms. The previous hardcoded value of 128 equalled macOS's default `somaxconn`, so bursts could overflow the queue and drop SYNs before load-shedding (`HYPER_HTTP_MAX_PENDING`) could fire. |

The accept loop is edge-drained: each `poll()` wakeup accepts every pending connection until the queue is empty, so a burst of simultaneous connects is admitted in one pass rather than one-per-wakeup.

## Static File Serving

In development, the server can serve static files directly:

```python
app = HyperApp("myapp", static="static/")

# Files in static/ are served at /static/
# /static/css/style.css -> static/css/style.css
```

For production, use a reverse proxy (nginx) for static files. The `StaticFilesMiddleware` handles ETag generation, Cache-Control headers, gzip compression, and If-None-Match/If-Modified-Since conditional requests.

See the [Static Files](static-files.md) documentation for details on `collectstatic`, manifest hashing, and CDN integration.

## Hot Reload

In development, the server watches for file changes and automatically reloads:

```python
from hyperdjango.hot_reload import HotReloader

reloader = HotReloader(
    watch_dirs=["myapp/"],
    extensions=[".py", ".html"],
)
reloader.start()
```

The file watcher uses native OS APIs:

- **macOS**: kqueue (no polling)
- **Linux**: inotify (no polling)

When a change is detected, affected modules are reloaded with `importlib.reload()` and an SSE event is pushed to connected browsers for instant page refresh.

The reloader reports its own liveness. `start()` returning says only that the
watcher was asked to start — the native thread may not have armed kqueue/inotify
yet, and a reloader that silently stopped watching looks identical to one where
nothing changed:

```python
seen = reloader.changes_seen  # monotonic, never reset
edit_a_watched_file()
if not reloader.wait_for_change(5.0, since=seen):
    ...  # the watcher is not delivering changes
```

`changes_seen` counts notifications the watcher has delivered, so an observer
that looks after the fact still sees one (a level flag could have flipped back).
`wait_for_change(timeout, since=...)` blocks until the count moves past `since`;
pass the value read _before_ the edit so a notification arriving between the two
calls is not missed.

## Django WSGI Bridge

Run Django applications through the Zig HTTP server for improved performance:

```bash
python manage.py runziserver 0.0.0.0:8000
```

The `ZigHandler` bridges the Zig HTTP server to Django's WSGI interface:

1. Zig server accepts the connection and parses the HTTP request
2. The request is converted to a WSGI environ dict
3. Django's WSGI handler processes the request
4. The Django response is sent back through the Zig server using `sendFullResponse`

The bridge supports:

- Full response headers (Set-Cookie, CSRF tokens, etc.)
- Streaming responses
- File responses
- WebSocket upgrade (passed through to ASGI)

```python
# In Django settings.py
INSTALLED_APPS = [
    ...
    'hyperdjango.serving',
]
```

### Performance

The WSGI bridge provides the Zig server's connection handling and parsing performance while running standard Django views. This typically yields 1.5-2x throughput improvement over gunicorn/uvicorn for Django applications due to faster HTTP parsing and connection management.

## Graceful Shutdown

The Zig server handles shutdown signals natively using a self-pipe + atomic flag architecture. No Python signal handlers needed -- the shutdown is coordinated at the C/Zig level for reliability.

### Signal Handling

- **SIGTERM** -- graceful shutdown (systemd, Docker, process managers)
- **SIGINT** -- graceful shutdown (Ctrl+C in terminal)

Both signals write to a self-pipe that wakes the accept loop immediately (async-signal-safe). The accept loop exits, then the server drains in-flight requests.

### Shutdown Sequence

1. Signal handler sets atomic `shutdown_flag` and writes to self-pipe
2. Accept loop's `poll()` wakes and breaks (stops accepting new connections)
3. Worker threads finish their current request (tracked via `active_requests` counter)
4. Drain timeout: 30 seconds for in-flight requests to complete
5. Worker threads exit (queue returns null on shutdown, condition variable broadcast)
6. `on_shutdown` hooks called (Python-side cleanup)
7. PID file removed
8. Clean exit (exit code 0)

```python
@app.on_shutdown
async def cleanup():
    await db.close()
    logger.info("Server shutdown complete")
```

### Programmatic Shutdown

Trigger shutdown from Python code (useful for tests, health checks, or admin endpoints):

```python
from hyperdjango._hyperdjango_native import _server_shutdown

_server_shutdown()  # Sets the shutdown flag, server exits after draining
```

## Server Management

### CLI Commands

```bash
# Development (foreground)
hyper run --app app:app --port 8000

# Production (background daemon)
hyper start --app app:app --port 8000 --prod
hyper stop --port 8000                    # SIGTERM graceful shutdown
hyper restart --app app:app --port 8000   # stop + start
hyper status --port 8000                  # Check if running
```

`hyper start` writes a PID file (`.hyper.<port>.pid`) and redirects output to `.hyper.<port>.log`. `hyper stop` sends SIGTERM and waits up to 30 seconds for graceful exit, then SIGKILL as fallback.

### systemd Integration

Generate and install a production systemd unit file:

```bash
# Generate unit file (writes to current directory if not root)
hyper systemd install --app app:app --port 8000

# Install and enable as root
sudo hyper systemd install --app app:app --port 8000 --enable

# Manage the service
sudo systemctl status hyperdjango-myapp
sudo systemctl restart hyperdjango-myapp
sudo journalctl -u hyperdjango-myapp -f

# Remove
sudo hyper systemd uninstall
```

The generated unit file includes:

- `Type=exec` with `KillSignal=SIGTERM` for graceful shutdown
- `TimeoutStopSec=30` matching the drain timeout
- `Restart=on-failure` with 5-second delay
- Security hardening: `PrivateTmp`, `ProtectSystem=strict`, `NoNewPrivileges`
- Resource limits: `LimitNOFILE=65536`, `LimitNPROC=4096`
- `KillMode=mixed` to clean up worker threads

### Docker

```dockerfile
FROM python:3.14-slim
WORKDIR /app
COPY . .
RUN pip install -e .
RUN hyper-build --release
EXPOSE 8000
CMD ["hyper", "run", "--app", "app:app", "--host", "0.0.0.0", "--port", "8000", "--prod"]
```

The server handles `SIGTERM` from Docker's stop command, draining connections before exit.

## ASGI Compatibility

HyperDjango applications can be served by any ASGI server (uvicorn, hypercorn, daphne) for environments where the native Zig server is not available:

```python
# asgi.py
from myapp import app

# HyperApp implements the ASGI protocol
application = app
```

```bash
uvicorn asgi:application --host 0.0.0.0 --port 8000
```

However, the native Zig server is strongly recommended for production -- it provides 2.1x higher throughput and lower latency.

## Performance Benchmarks

| Metric                     | Zig Server            | uvicorn      |
| -------------------------- | --------------------- | ------------ |
| Requests/sec (hello world) | 13,000                | 6,000        |
| Route resolution (dynamic) | 528 ns                | N/A          |
| HTTP parse overhead        | ~2 us                 | ~10 us       |
| WebSocket frame unmask     | SIMD (16 bytes/cycle) | byte-by-byte |
| Multipart boundary scan    | 20.4 GB/s (SIMD)      | ~100 MB/s    |

The benchmarks were measured with wrk on a single-machine setup. Real-world performance depends on application logic, database queries, and network conditions.
