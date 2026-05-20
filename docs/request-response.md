# Request & Response

API reference for the Request and Response objects.

## Request

Every view receives a `Request` object as its first argument. The Request uses lazy parsing -- body, query parameters, cookies, and JSON are only parsed when accessed.

```python
from hyperdjango.request import Request
```

### Core Attributes

| Attribute      | Type                  | Description                                                                           |
| -------------- | --------------------- | ------------------------------------------------------------------------------------- |
| `method`       | `str`                 | HTTP method: `"GET"`, `"POST"`, `"PUT"`, `"PATCH"`, `"DELETE"`, etc. Auto-uppercased. |
| `path`         | `str`                 | URL path (e.g., `"/users/123"`)                                                       |
| `headers`      | `CaseInsensitiveDict` | HTTP headers with case-insensitive key access                                         |
| `query_string` | `str`                 | Raw query string (e.g., `"page=1&sort=name"`)                                         |
| `body`         | `bytes`               | Raw request body bytes                                                                |
| `path_params`  | `dict[str, str]`      | Path parameters extracted by the router (e.g., `{"id": "42"}`)                        |
| `scope`        | `AsgiScope \| None`   | ASGI scope dict (available when running under ASGI)                                   |
| `app`          | `HyperApp`            | Reference to the HyperApp instance (set by dispatch)                                  |

### Auth Attributes

Set by authentication middleware before your view runs:

| Attribute         | Type                   | Default | Description                                         |
| ----------------- | ---------------------- | ------- | --------------------------------------------------- |
| `user`            | `User \| dict \| None` | `None`  | Authenticated user instance or session dict         |
| `session_id`      | `str \| None`          | `None`  | Session ID string                                   |
| `api_key`         | `str \| None`          | `None`  | API key string from request                         |
| `api_key_valid`   | `bool`                 | `False` | Whether the API key was validated                   |
| `oauth2_provider` | `str \| None`          | `None`  | OAuth2 provider name (e.g., `"google"`, `"github"`) |

### Derived Properties

| Property       | Type                   | Description                                                                |
| -------------- | ---------------------- | -------------------------------------------------------------------------- |
| `client_ip`    | `str`                  | Client IP address (checks `X-Forwarded-For`, `X-Real-IP`, then ASGI scope) |
| `is_secure`    | `bool`                 | `True` if HTTPS (checks ASGI scheme, then `X-Forwarded-Proto`)             |
| `content_type` | `str`                  | Value of the `Content-Type` header                                         |
| `is_json`      | `bool`                 | `True` if `Content-Type` contains `"json"`                                 |
| `cookies`      | `dict[str, str]`       | Parsed cookies from the `Cookie` header (native Zig parser, 3x faster)     |
| `query_params` | `dict[str, list[str]]` | Parsed query string (SIMD-accelerated)                                     |
| `GET`          | `dict[str, str]`       | Django-compatible flat query dict (first value per key)                    |

### Client IP Resolution

`client_ip` checks headers in order for reverse proxy support:

1. `X-Forwarded-For` -- uses first IP in the comma-separated list
2. `X-Real-IP` -- used if `X-Forwarded-For` is absent
3. ASGI scope `client` tuple -- direct connection IP
4. Falls back to `"127.0.0.1"`

```python
@app.get("/whoami")
async def whoami(request):
    return {
        "ip": request.client_ip,
        "secure": request.is_secure,
    }
```

### Headers

Case-insensitive header access via `CaseInsensitiveDict`:

```python
request.headers["content-type"]  # "application/json"
request.headers["Content-Type"]  # same result
request.headers["Authorization"]  # "Bearer ..."
request.headers.get("X-Custom")  # None if missing
request.headers.get("x-custom", "default")  # with default
```

All header keys are stored lowercased internally.

### Query Parameters

#### query_params (multi-value)

Parsed query string as a dict of lists. Uses SIMD-accelerated parser via the native Zig extension.

```python
# URL: /search?tags=python&tags=web&page=1
params = request.query_params
# {"tags": ["python", "web"], "page": ["1"]}
```

#### query() helper

Get a single query parameter value with an optional default:

```python
page = request.query("page", "1")  # "1"
sort = request.query("sort", "name")  # "name"
```

#### GET (Django-compatible)

Flat dict with first value per key, matching Django's `request.GET`:

```python
request.GET.get("page", "1")  # "1"
request.GET["q"]  # raises KeyError if missing
```

### JSON Body

```python
@app.post("/api/users")
async def create_user(request):
    data = await request.json()
    # {"name": "Alice", "email": "alice@example.com"}
    return Response.json({"created": data["name"]}, status=201)
```

Uses SIMD-accelerated JSON parser when the native extension is available. Raises `HTTPException(400, "Invalid JSON in request body")` on parse errors instead of a 500.

The parsed result is cached -- calling `await request.json()` multiple times returns the same object.

### Form Data

Parse URL-encoded or multipart form data:

```python
@app.post("/login")
async def login(request):
    form = await request.form()
    # {"username": ["alice"], "password": ["secret"]}
    username = form.get("username", [""])[0]
```

For URL-encoded forms (`application/x-www-form-urlencoded`), values are lists (same as `query_params`). For multipart forms, text fields go into `form()` and file fields go into `files()`.

### File Uploads

```python
@app.post("/upload")
async def upload(request):
    files = await request.files()
    # {"avatar": UploadedFile("photo.jpg", "image/jpeg", b"..."), ...}
    avatar = files["avatar"]
    print(avatar.filename)  # "photo.jpg"
    print(avatar.content_type)  # "image/jpeg"
    print(avatar.size)  # 45321 (bytes)
    print(len(avatar.data))  # raw bytes
```

File uploads use the native Zig SIMD multipart parser (20.4 GB/s boundary scanning).

#### UploadedFile

| Attribute      | Type    | Description                        |
| -------------- | ------- | ---------------------------------- |
| `filename`     | `str`   | Original filename                  |
| `content_type` | `str`   | MIME type                          |
| `data`         | `bytes` | File content as bytes              |
| `size`         | `int`   | Length of data in bytes (property) |

### Cookies

Parsed from the `Cookie` header using the native Zig cookie parser (3x faster than Python):

```python
cookies = request.cookies
# {"session_id": "abc123", "theme": "dark"}

session = request.cookies.get("session_id")
```

Cookies are lazily parsed on first access and cached.

### Body Access

#### await request.text()

Get the body as a decoded UTF-8 string:

```python
body_text = await request.text()
```

#### await request.bytes()

Get the raw body as bytes:

```python
raw = await request.bytes()
```

### Constructing Requests

#### From ASGI Scope

```python
request = Request.from_asgi(scope, body=b"...")
```

Creates a Request from an ASGI scope dict and body bytes. Headers are decoded from the ASGI `(bytes, bytes)` tuple format.

#### Direct Construction

```python
request = Request(
    method="POST",
    path="/api/users",
    headers={"content-type": "application/json"},
    body=b'{"name": "Alice"}',
    query_string="format=json",
)
```

---

## Response

The `Response` class provides static constructors for every common response type.

```python
from hyperdjango import Response

# or
from hyperdjango.response import Response
```

### Static Constructors

#### Response.json(data, status=200, headers=None)

Create a JSON response. Uses SIMD-accelerated serialization via the native Zig extension.

```python
response = Response.json({"key": "value"})
response = Response.json(data, status=201)
response = Response.json(data, headers={"X-Custom": "value"})
```

Content-Type: `application/json; charset=utf-8`

#### Response.html(content, status=200, headers=None)

Create an HTML response:

```python
response = Response.html("<h1>Hello</h1>")
response = Response.html(rendered_template, status=200)
```

Content-Type: `text/html; charset=utf-8`

#### Response.text(content, status=200, headers=None)

Create a plain text response:

```python
response = Response.text("Hello, world!")
```

Content-Type: `text/plain; charset=utf-8`

#### Response.redirect(url, status=302, headers=None)

Create a redirect response:

```python
response = Response.redirect("/dashboard/")  # 302 temporary
response = Response.redirect("/new-url/", status=301)  # 301 permanent
response = Response.redirect("/same-method/", status=307)  # preserve method
```

Sets the `Location` header to the target URL.

#### Response.stream(iterator, status=200, headers=None, content_type="text/plain")

Create a streaming response from an async iterator:

```python
async def generate():
    for i in range(100):
        yield f"chunk {i}\n"


response = Response.stream(generate())
```

#### Response.sse(iterator, status=200, headers=None)

Create a Server-Sent Events streaming response:

```python
async def events():
    yield {"event": "update", "data": {"count": 1}}
    yield {"event": "update", "data": {"count": 2}, "id": "msg-2"}
    yield "simple text data"


response = Response.sse(events())
```

Content-Type: `text/event-stream`. Automatically sets `Cache-Control: no-cache` and `Connection: keep-alive`.

SSE events can be:

- **dict** with keys: `event` (type), `data` (payload), `id` (event ID), `retry` (reconnect ms)
- **str** sent as a plain `data:` line

#### Response.file(path, content_type=None, status=200, headers=None, request=None)

Serve a file from disk:

```python
response = Response.file("/path/to/report.pdf")
response = Response.file("image.png", content_type="image/png")
```

Auto-detects content type from the file extension. Sets `Content-Length`. Returns 404 if the file does not exist. Files over 8 MiB stream from disk in bounded chunks instead of loading into memory.

**HTTP Range (media seeking / resumable downloads).** Pass `request=` to enable RFC 7233 range support — this is what makes `<video>`/`<audio>` seeking and resumable downloads work:

```python
@app.get("/media/{name}")
def media(request, name):
    return Response.file(safe_media_path(name), request=request)
```

With `request=`, a valid single byte-range yields `206 Partial Content` with `Content-Range`, an unsatisfiable one yields `416`, and every response advertises `Accept-Ranges: bytes`. Without it, the whole file is served (backward-compatible). Only a single range is honored; multi-range requests fall back to the full body.

#### Response.attachment(path, filename=None, headers=None, request=None)

Serve a file as a downloadable attachment:

```python
response = Response.attachment("/data/report.csv")
response = Response.attachment("/data/export.xlsx", filename="my-report.xlsx")
```

Sets `Content-Disposition: attachment; filename="..."`. Falls back to the original filename if not specified. Pass `request=` to allow interrupted downloads to be resumed (Range support, as above).

#### Response.empty(status=204, headers=None)

Create an empty response (no body):

```python
response = Response.empty()  # 204 No Content
response = Response.empty(status=202)  # 202 Accepted
```

### Response Attributes

| Attribute      | Type             | Description                                     |
| -------------- | ---------------- | ----------------------------------------------- |
| `status`       | `int`            | HTTP status code                                |
| `body`         | `bytes`          | Response body                                   |
| `headers`      | `dict[str, str]` | Response headers                                |
| `content_type` | `str \| None`    | Content-Type (also in headers)                  |
| `is_streaming` | `bool`           | Whether this is a streaming response (property) |

### Setting Headers

```python
response = Response.json(data)
response.headers["Cache-Control"] = "public, max-age=300"
response.headers["X-Request-ID"] = request_id
```

All header values are sanitized against CRLF injection automatically.

### set_cookie()

```python
response.set_cookie(
    key="session_id",
    value="abc123",
    max_age=86400,  # 1 day in seconds (None = session cookie)
    path="/",  # Cookie path scope
    domain=None,  # Cookie domain scope
    httponly=True,  # Not accessible via JavaScript
    secure=False,  # HTTPS-only
    samesite="Lax",  # "Strict", "Lax", or "None"
)
```

| Parameter  | Type          | Default  | Description                                                                                                                                                                                           |
| ---------- | ------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `key`      | `str`         | required | Cookie name                                                                                                                                                                                           |
| `value`    | `str`         | `""`     | Cookie value. Percent-encoded on write and percent-decoded when read back via `request.cookies`, so any value — spaces, `%`, `;`, unicode — round-trips losslessly and can't inject cookie attributes |
| `max_age`  | `int \| None` | `None`   | Lifetime in seconds. `None` = session cookie (deleted when browser closes)                                                                                                                            |
| `path`     | `str`         | `"/"`    | URL path scope. Cookie is sent for requests matching this path and below                                                                                                                              |
| `domain`   | `str \| None` | `None`   | Domain scope. `None` = current domain only                                                                                                                                                            |
| `httponly` | `bool`        | `True`   | When `True`, cookie is inaccessible to JavaScript (`document.cookie`)                                                                                                                                 |
| `secure`   | `bool`        | `False`  | When `True`, cookie is only sent over HTTPS. Auto-set to `True` when `samesite="None"`                                                                                                                |
| `samesite` | `str`         | `"Lax"`  | `"Strict"` (same-site only), `"Lax"` (safe cross-site), `"None"` (always, requires Secure)                                                                                                            |

Multiple cookies can be set on one response:

```python
response.set_cookie("session_id", "abc", max_age=86400, httponly=True, secure=True)
response.set_cookie("theme", "dark", max_age=31536000, httponly=False)
```

### delete_cookie()

Delete a cookie by setting it with `max_age=0`:

```python
response.delete_cookie("session_id")
response.delete_cookie("session_id", path="/", domain=".example.com")
```

### ETag and Conditional Responses

#### set_etag()

Auto-generate an ETag from the response body (MD5 hash), or provide a custom value:

```python
response = Response.json(data)
response.set_etag()  # Auto-generated from body hash
response.set_etag("custom-etag-v1")  # Custom ETag value
```

#### cache_control()

Set the `Cache-Control` header with a fluent API:

```python
response.cache_control(public=True, max_age=300)
# Cache-Control: public, max-age=300

response.cache_control(private=True, no_cache=True)
# Cache-Control: private, no-cache

response.cache_control(no_store=True)
# Cache-Control: no-store

response.cache_control(public=True, max_age=86400, s_maxage=3600)
# Cache-Control: public, max-age=86400, s-maxage=3600
```

| Parameter  | Description                                      |
| ---------- | ------------------------------------------------ |
| `public`   | Response can be cached by any cache              |
| `private`  | Response is for a single user (no shared caches) |
| `no_cache` | Must revalidate before using cached copy         |
| `no_store` | Do not cache at all                              |
| `max_age`  | Seconds the response is fresh                    |
| `s_maxage` | Override max-age for shared caches (CDNs)        |

#### check_not_modified()

Check `If-None-Match` against the ETag. If matched, sets status to 304 and clears the body:

```python
response = Response.json(data)
response.set_etag()

if response.check_not_modified(request):
    return response  # 304 Not Modified, empty body

return response  # 200 with full body
```

### Complete ETag/Conditional Pattern

```python
@app.get("/api/products/{id:int}")
async def get_product(request, id: int):
    product = await Product.objects.get(id=id)
    data = {"id": product.id, "name": product.name, "version": product.version}

    response = Response.json(data)
    response.set_etag(f"product-{id}-v{product.version}")
    response.cache_control(public=True, max_age=60)

    if response.check_not_modified(request):
        return response  # 304, saves bandwidth

    return response
```

### Status Code Reference

| Code | Name                  | Meaning                                              |
| ---- | --------------------- | ---------------------------------------------------- |
| 200  | OK                    | Successful GET/PUT/PATCH                             |
| 201  | Created               | Successful POST creating a resource                  |
| 202  | Accepted              | Request accepted for async processing                |
| 204  | No Content            | Successful DELETE or action with no body             |
| 301  | Moved Permanently     | Permanent redirect (search engines update)           |
| 302  | Found                 | Temporary redirect (default for `Response.redirect`) |
| 304  | Not Modified          | Conditional GET -- ETag matched                      |
| 307  | Temporary Redirect    | Redirect preserving HTTP method                      |
| 308  | Permanent Redirect    | Permanent redirect preserving HTTP method            |
| 400  | Bad Request           | Invalid input, malformed JSON                        |
| 401  | Unauthorized          | Authentication required                              |
| 403  | Forbidden             | Authenticated but permission denied                  |
| 404  | Not Found             | Resource does not exist                              |
| 405  | Method Not Allowed    | Wrong HTTP method for this endpoint                  |
| 409  | Conflict              | State conflict (e.g., duplicate resource)            |
| 413  | Payload Too Large     | Request body exceeds `max_body_size`                 |
| 422  | Unprocessable Entity  | Validation error (valid JSON, invalid data)          |
| 429  | Too Many Requests     | Rate limited                                         |
| 500  | Internal Server Error | Unhandled exception                                  |
| 502  | Bad Gateway           | Upstream server error                                |
| 503  | Service Unavailable   | Server overloaded or in maintenance                  |

### Auto-Serialization

Views can return dicts or lists directly -- they are automatically wrapped in `Response.json()`:

```python
@app.get("/api/users")
async def list_users(request):
    return [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    # Automatically becomes Response.json([...], status=200)


@app.get("/api/status")
async def status(request):
    return {"status": "ok", "version": "1.0"}
    # Automatically becomes Response.json({...}, status=200)
```

This works for:

- `dict` -- serialized as a JSON object
- `list` -- serialized as a JSON array
- `Response` -- returned as-is

Any other return type raises an error.

### ASGI Interface

Responses can send themselves via the ASGI protocol:

```python
await response.send(send_func)
```

For streaming responses, chunks are sent incrementally with `more_body=True` until the iterator is exhausted. For non-streaming responses, the full body is sent in a single frame.

### Combining Patterns

A complete API endpoint using multiple Response features:

```python
@app.get("/api/products")
async def list_products(request):
    page = int(request.query("page", "1"))
    per_page = int(request.query("per_page", "20"))

    products = await Product.objects.order_by("-created_at").all()
    total = len(products)
    start = (page - 1) * per_page
    items = products[start : start + per_page]

    data = {
        "items": [{"id": p.id, "name": p.name} for p in items],
        "page": page,
        "total": total,
        "pages": (total + per_page - 1) // per_page,
    }

    response = Response.json(data)
    response.set_etag()
    response.cache_control(private=True, max_age=30)

    if response.check_not_modified(request):
        return response

    return response
```
