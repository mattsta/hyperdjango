# Conditional View Processing

HTTP clients can send headers telling the server about cached copies of resources they already have. When the resource has not changed, the server can respond with `304 Not Modified` instead of sending the full response body, saving bandwidth and improving perceived performance.

HyperDjango supports conditional processing through two mechanisms:

1. **CacheableMixin** on REST ViewSets -- automatic ETag and Cache-Control for API responses
2. **Response-level headers** -- manual ETag and Last-Modified on any response

## How Conditional Requests Work

The flow for a conditional GET request:

1. Client requests `/api/posts/` for the first time.
2. Server responds with the full body, plus an `ETag` header (e.g., `W/"a1b2c3d4..."`).
3. Client caches the response and the ETag.
4. On next request, client sends `If-None-Match: W/"a1b2c3d4..."`.
5. Server computes the ETag for the current content. If it matches, it returns `304 Not Modified` with no body.
6. If content has changed, the server returns the full new response with a new ETag.

The same mechanism works with `Last-Modified` / `If-Modified-Since` for time-based validation.

## CacheableMixin (REST Framework)

The simplest way to add conditional processing to API endpoints is the `CacheableMixin` on a ViewSet. It automatically computes ETags from response content and handles `If-None-Match` validation.

```python
from hyperdjango.rest import CacheableMixin, ModelViewSet, ModelSerializer


class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = ["id", "title", "body", "updated_at"]


class PostViewSet(CacheableMixin, ModelViewSet):
    serializer_class = PostSerializer
    queryset = Post

    # Cache-Control configuration
    cache_max_age = 60  # Cache-Control: max-age=60
    cache_private = True  # Cache-Control: private (default)
    cache_no_cache = False  # If True, Cache-Control: no-cache
```

### How CacheableMixin Works

`CacheableMixin` is applied automatically on `list()` and `retrieve()` actions:

1. The response body is serialized as normal.
2. A weak ETag is computed from the SHA-256 hash of the response bytes: `W/"<first 32 hex chars>"`.
3. The `Cache-Control` header is built from `cache_max_age`, `cache_private`, and `cache_no_cache`.
4. If the request includes `If-None-Match` and the ETag matches, a `304 Not Modified` response is returned with no body.

### Cache-Control Configuration

The three class attributes control the `Cache-Control` header:

| Attribute        | Default | Description                                                                    |
| ---------------- | ------- | ------------------------------------------------------------------------------ |
| `cache_max_age`  | `0`     | Seconds the response can be cached. `0` means no `max-age` directive.          |
| `cache_private`  | `True`  | `True` = `private` (only browser cache). `False` = `public` (CDN/proxy cache). |
| `cache_no_cache` | `False` | `True` = `no-cache` (forces revalidation on every request).                    |

Examples of generated headers:

```
# cache_max_age=0, cache_private=True, cache_no_cache=False
Cache-Control: private

# cache_max_age=300, cache_private=False
Cache-Control: public, max-age=300

# cache_no_cache=True
Cache-Control: no-cache, private
```

### 304 Not Modified Responses

When `CacheableMixin` detects that the client's cached version is still valid, it returns a minimal 304 response:

```python
# Client sends:
# GET /api/posts/ HTTP/1.1
# If-None-Match: W/"a1b2c3d4e5f6..."

# Server checks: current ETag matches? Yes.
# Response: 304 Not Modified (no body)
```

This works for both `GET` and `HEAD` requests. For `POST`, `PUT`, and `DELETE`, ETag validation is skipped -- the full response is always returned.

## Manual ETag and Cache Headers

For non-ViewSet routes, set headers directly on the Response:

```python
import hashlib
from hyperdjango import HyperApp
from hyperdjango.response import Response

app = HyperApp("myapp")


@app.route("GET", "/articles/{id}")
async def get_article(request, id: int):
    article = await Article.objects.get(id=id)
    body = article.to_json()

    # Compute ETag from content
    etag = f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'

    # Check If-None-Match
    if_none_match = request.headers.get("if-none-match", "")
    if if_none_match and etag in if_none_match:
        return Response(body=b"", status=304, headers={"ETag": etag})

    return Response(
        body=body,
        status=200,
        content_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=120",
        },
    )
```

## Last-Modified Headers

For resources with a clear modification timestamp, use `Last-Modified` instead of (or alongside) ETags:

```python
from email.utils import formatdate
from calendar import timegm


@app.route("GET", "/articles/{id}")
async def get_article(request, id: int):
    article = await Article.objects.get(id=id)

    # Format Last-Modified as HTTP date
    last_modified = formatdate(timegm(article.updated_at.timetuple()), usegmt=True)

    # Check If-Modified-Since
    ims = request.headers.get("if-modified-since", "")
    if ims == last_modified:
        return Response(
            body=b"",
            status=304,
            headers={
                "Last-Modified": last_modified,
            },
        )

    return Response.json(
        article.to_dict(),
        headers={
            "Last-Modified": last_modified,
            "Cache-Control": "public, max-age=60",
        },
    )
```

## Combining ETag and Last-Modified

For maximum compatibility, provide both headers. Different clients and proxies may prefer one over the other:

```python
class ArticleViewSet(CacheableMixin, ModelViewSet):
    serializer_class = ArticleSerializer
    queryset = Article
    cache_max_age = 300
    cache_private = False  # Allow CDN caching
```

The `CacheableMixin` handles ETag automatically. To also set `Last-Modified`, override the retrieve method:

```python
class ArticleViewSet(CacheableMixin, ModelViewSet):
    serializer_class = ArticleSerializer
    queryset = Article
    cache_max_age = 300

    async def retrieve(self, request, **kwargs):
        response = await super().retrieve(request, **kwargs)
        instance = await self.get_object()
        last_mod = formatdate(timegm(instance.updated_at.timetuple()), usegmt=True)
        response.headers["Last-Modified"] = last_mod
        return response
```

## Precondition Failed (412)

For write operations (`PUT`, `DELETE`), clients can send `If-Match` to ensure they are modifying the version they expect. If the ETag does not match, the server should return `412 Precondition Failed`:

```python
@app.route("PUT", "/articles/{id}")
async def update_article(request, id: int):
    article = await Article.objects.get(id=id)
    current_etag = compute_etag(article)

    if_match = request.headers.get("if-match", "")
    if if_match and current_etag not in if_match:
        return Response(body=b'{"detail":"Precondition Failed"}', status=412)

    # Proceed with update...
    data = await request.json()
    article.title = data["title"]
    await article.save()
    return Response.json(article.to_dict())
```

## When to Use Conditional Processing

| Scenario                          | Approach                                 |
| --------------------------------- | ---------------------------------------- |
| REST API list/detail endpoints    | `CacheableMixin` on ViewSet              |
| Static or rarely-changing content | `Cache-Control: public, max-age=3600`    |
| Frequently updated data           | ETag with `no-cache` (always revalidate) |
| CDN-cached public pages           | `Cache-Control: public` + ETag           |
| User-specific data                | `Cache-Control: private` + ETag          |
| Write operations (PUT/DELETE)     | `If-Match` with 412 responses            |
| File downloads with known mtime   | `Last-Modified` header                   |

## Performance Impact

Conditional processing reduces bandwidth and server load:

- **304 responses** skip serialization, rendering, and body transfer.
- **ETag computation** is fast -- SHA-256 of already-serialized bytes (nanosecond-scale for typical API responses).
- **CDN offloading** with `public` Cache-Control can eliminate server requests entirely for cacheable content.

For API endpoints that serve large collections, combining `CacheableMixin` with pagination ensures that only the pages that changed need to be re-transferred.

## See Also

- [REST Framework](rest.md) -- full ViewSet and serializer documentation
- [Performance](performance.md) -- query tracking and optimization
- [Static Files](static-files.md) -- static file caching with ETag and immutable headers
- [Middleware](middleware.md) -- request/response middleware pipeline
