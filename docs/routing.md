# URL Routing

HyperDjango uses a native Zig radix trie router for URL pattern matching -- 808ns per resolve.

## Basic Routing

```python
from hyperdjango import HyperApp

app = HyperApp("myapp")


@app.get("/")
async def index(request):
    return {"message": "Home"}


@app.get("/about")
async def about(request):
    return {"page": "About"}


@app.post("/contact")
async def contact(request):
    data = await request.json()
    return {"received": True}
```

Views can return dicts or lists directly -- they are auto-serialized to JSON responses.

## Route Decorators

HyperDjango provides method-specific decorators on both `HyperApp` and `Router`:

### @app.get(pattern, name=None)

Register a GET handler:

```python
@app.get("/users")
async def list_users(request):
    return {"users": []}
```

GET routes also automatically respond to HEAD requests.

### @app.post(pattern, name=None)

Register a POST handler:

```python
@app.post("/users")
async def create_user(request):
    data = await request.json()
    return Response.json({"id": 1}, status=201)
```

### @app.put(pattern, name=None)

Register a PUT handler (full replacement):

```python
@app.put("/users/{id:int}")
async def replace_user(request, id: int):
    data = await request.json()
    return {"updated": id}
```

### @app.patch(pattern, name=None)

Register a PATCH handler (partial update):

```python
@app.patch("/users/{id:int}")
async def patch_user(request, id: int):
    data = await request.json()
    return {"patched": id}
```

### @app.delete(pattern, name=None)

Register a DELETE handler:

```python
@app.delete("/users/{id:int}")
async def delete_user(request, id: int):
    return Response.json({"deleted": id}, status=200)
```

### @app.route(pattern, methods=None, name=None)

Register a handler for multiple HTTP methods:

```python
@app.route("/items/{id}", methods=["GET", "PUT", "DELETE"])
async def item(request, id: int):
    if request.method == "GET":
        return await get_item(id)
    elif request.method == "PUT":
        return await update_item(request, id)
    elif request.method == "DELETE":
        return await delete_item(id)
```

If `methods` is not specified, defaults to `["GET"]`.

## Path Parameters

Capture URL segments as typed parameters. Parameters use `{name:type}` syntax.

```python
@app.get("/users/{id:int}")
async def get_user(request, id: int):
    return {"user_id": id}


@app.get("/articles/{slug:str}")
async def get_article(request, slug: str):
    return {"slug": slug}


@app.get("/files/{filepath:path}")
async def get_file(request, filepath: str):
    # filepath can contain slashes: "docs/2024/report.pdf"
    return Response.file(filepath)
```

### Built-in Converters

| Converter       | Matches                                | Example                  | Python Type |
| --------------- | -------------------------------------- | ------------------------ | ----------- |
| `str` (default) | Non-empty string without `/`           | `{name}` or `{name:str}` | `str`       |
| `int`           | Positive integer                       | `{id:int}`               | `int`       |
| `slug`          | Letters, numbers, hyphens, underscores | `{slug:slug}`            | `str`       |
| `uuid`          | UUID format                            | `{pk:uuid}`              | `str`       |
| `path`          | Non-empty string including `/`         | `{filepath:path}`        | `str`       |

When `str` is the type, you can omit it: `{name}` is equivalent to `{name:str}`.

Type conversion happens automatically. If a value cannot be converted (e.g., `"abc"` for `int`), the route does not match and the router continues looking for other matches.

### Multiple Parameters

```python
@app.get("/users/{user_id:int}/posts/{post_id:int}")
async def user_post(request, user_id: int, post_id: int):
    return {"user": user_id, "post": post_id}


@app.get("/archive/{year:int}/{month:int}/{slug:str}")
async def archive_post(request, year: int, month: int, slug: str):
    return {"year": year, "month": month, "slug": slug}
```

## Router Class

The `Router` class provides modular route organization. Create routers for different sections of your application and mount them with prefixes.

```python
from hyperdjango.router import Router

# API routes in a separate module
api = Router()


@api.get("/users")
async def list_users(request):
    return await User.objects.all()


@api.get("/users/{id:int}")
async def get_user(request, id: int):
    return await User.objects.get(id=id)


@api.post("/users")
async def create_user(request):
    data = await request.json()
    return Response.json(await User.objects.create(**data), status=201)
```

### Router Methods

The `Router` class has the same decorator methods as `HyperApp`:

- `router.get(pattern, name=None)`
- `router.post(pattern, name=None)`
- `router.put(pattern, name=None)`
- `router.patch(pattern, name=None)`
- `router.delete(pattern, name=None)`
- `router.route(pattern, methods=None, name=None)`

## app.router.include() -- Mounting Sub-Routers

Mount a sub-router at a URL prefix:

```python
# Mount the API router at /api/v1
app.router.include("/api/v1", api)
# Routes: /api/v1/users, /api/v1/users/{id}
```

### With Namespace

Add a namespace for reverse URL resolution:

```python
app.router.include("/api/v1", api, namespace="api")
# Reverse: app.router.reverse("api:list_users") -> "/api/v1/users"
```

### Mounting Route Tuples

You can also mount a list of `(method, pattern, handler, name)` tuples:

```python
app.router.include(
    "/blog",
    [
        ("GET", "/", list_posts, "list"),
        ("GET", "/{id:int}", post_detail, "detail"),
        ("POST", "/", create_post, "create"),
    ],
    namespace="blog",
)
```

### Nested Includes

Routers can include other routers for deep nesting:

```python
# users/routes.py
users = Router()


@users.get("/")
async def list_users(request):
    return {"users": []}


@users.get("/{id:int}")
async def get_user(request, id: int):
    return {"id": id}


# api/routes.py
api = Router()
api.include("/users", users, namespace="users")

# app.py
app.router.include("/api/v1", api, namespace="api")
# Reverse: app.router.reverse("api:users:get_user", id=42) -> "/api/v1/users/42"
```

## URL Namespacing

Name your routes for reverse URL lookup:

```python
@app.get("/articles/", name="article_list")
async def article_list(request):
    return render(request, "articles/list.html")


@app.get("/articles/{id:int}/", name="article_detail")
async def article_detail(request, id: int):
    return render(request, "articles/detail.html")
```

When routes are included with a namespace, names are prefixed: `namespace:name`.

## Reverse URL Lookup

Generate URLs from route names:

```python
# Simple reverse
url = app.router.reverse("article_detail", id=42)
# "/articles/42/"

# Namespaced reverse
url = app.router.reverse("blog:detail", id=7)
# "/blog/7"
```

### reverse() API

```python
url = app.router.reverse(name, **kwargs)
```

- `name` -- Route name string (supports `namespace:name` format)
- `**kwargs` -- Path parameter values to substitute
- Returns the URL path string
- Raises `ValueError` if the route name is not found

### In Templates

```
{{ url_for("article_detail", id=42) }}
```

## WebSocket Routes

Register WebSocket handlers with `@app.websocket()`:

```python
@app.websocket("/ws/chat/{room}")
async def chat(ws, room: str):
    await ws.accept()
    while True:
        msg = await ws.receive_text()
        await ws.send_text(f"Echo: {msg}")
```

WebSocket handlers receive a `WebSocket` object (not a `Request`). The `ws` object provides:

- `await ws.accept()` -- Accept the WebSocket connection
- `await ws.receive_text()` -- Receive a text message
- `await ws.receive_bytes()` -- Receive binary data
- `await ws.send_text(data)` -- Send a text message
- `await ws.send_bytes(data)` -- Send binary data
- `async for msg in ws.iter_text()` -- Iterate over text messages
- `await ws.close(code=1000)` -- Close the connection

WebSocket routes support the same path parameters as HTTP routes.

## Channel Routes

Bridge WebSocket connections to the Channel layer for pub/sub:

```python
@app.channel("/ws/notifications/{user_id}")
async def on_message(text, channel, ws):
    # Messages received from the client
    await channel.publish({"user": user_id, "text": text})
```

Channel routes automatically:

1. Accept the WebSocket connection
2. Subscribe to a channel derived from the URL path
3. Forward published messages to the WebSocket client
4. Call your handler when the client sends a message

Path parameters in the URL pattern are used to construct the channel name. For example, `/ws/chat/{room}` with `room=general` subscribes to channel `chat:general`.

```python
# Auto-publishes received text as JSON
@app.channel("/ws/chat/{room}", channel_name="chat")
async def on_connect(ws, channel):
    print(f"Client connected to {channel.name}")
```

## Health Check Routes

Mount liveness and readiness probes:

```python
app.mount_health()
# GET /health -> liveness (always 200 if process is running)
# GET /ready  -> readiness (checks DB + custom checks)
```

Customize paths:

```python
app.mount_health("/healthz", "/readyz")
```

### Liveness Response

```json
{ "status": "ok" }
```

Always returns 200 if the process is alive.

### Readiness Response

```json
{
  "status": "ok",
  "checks": {
    "database": "ok",
    "cache": "ok"
  }
}
```

Returns 200 if all checks pass, 503 if any check fails. The database check runs `SELECT 1` automatically when a database is configured.

### Custom Health Checks

Register custom checks via `app._health_checks`:

```python
async def check_cache():
    try:
        await cache.get("health_check_key")
        return True
    except Exception:
        return False


app._health_checks["cache"] = check_cache
```

## Static File Routes

### Development (built-in)

```python
app = HyperApp("myapp", static_dir="static")
# Serves /static/* from the static/ directory
```

### Production (middleware)

For production features like content-hash URLs, gzip compression, and aggressive caching:

```python
from hyperdjango.staticfiles import StaticFilesMiddleware

app.use(
    StaticFilesMiddleware(
        static_dir="staticfiles",
        prefix="/static/",
    )
)
```

The production middleware adds:

- Content-hash URLs for cache busting
- Gzip compression
- `Cache-Control` headers with long max-age
- ETag support with 304 responses
- `If-Modified-Since` support

## Route Listing

View all registered routes with the CLI:

```bash
uv run hyper routes
```

Output:

```
GET  /                    index
GET  /articles/           article_list
GET  /articles/{id:int}/  article_detail
POST /articles/           article_create
GET  /api/v1/users        list_users
GET  /api/v1/users/{id}   get_user
```

Shows method, pattern, and handler/route name for every registered route.

## Wildcard Patterns

The `path` converter acts as a wildcard, matching everything including slashes:

```python
@app.get("/docs/{filepath:path}")
async def serve_docs(request, filepath: str):
    # filepath matches: "guide/getting-started"
    #                    "api/v2/reference.html"
    return Response.file(f"docs/{filepath}")
```

## Performance

The native Zig radix trie router delivers:

- **808ns** per route resolve (dynamic routes with parameters)
- **Zero allocations** for static routes
- **Compile-time optimized** pattern matching
- **O(log n)** parameterized routing instead of linear regex scanning

The router is finalized (compressed paths, sorted children) when `app.listen()` or `app.run()` is called. All routes must be registered before the server starts.

### Static vs Dynamic Routes

Static routes (no parameters) use a hash map for O(1) lookup. Dynamic routes (with `{param}` segments) use the radix trie. The router tries the native trie first and falls back to regex only if the native extension is not compiled.

### Benchmarks

| Operation             | Time   | Notes                      |
| --------------------- | ------ | -------------------------- |
| Static route resolve  | ~100ns | Hash map lookup            |
| Dynamic route resolve | ~808ns | Radix trie traversal       |
| Route registration    | ~1us   | One-time cost at startup   |
| Finalization          | ~10us  | Called once before serving |

## Class-Based View Registration

CBVs use `as_view()` to create a handler compatible with the router:

```python
from hyperdjango.views import ListView, DetailView

app.get("/products/")(ProductList.as_view())
app.get("/products/{id:int}/")(ProductDetail.as_view())
app.route("/products/new", methods=["GET", "POST"])(ProductCreate.as_view())
```

See [Class-Based Views](class-based-views.md) for full documentation.
