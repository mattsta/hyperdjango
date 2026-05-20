# Static Files

Serve, collect, and cache-bust static assets (CSS, JS, images, fonts). Production-ready pipeline with content-hash filenames, CSS URL rewriting, gzip pre-compression, ETag/304 conditional responses, and in-memory LRU caching.

## Architecture

The static files system has three layers:

1. **StaticFilesFinder** -- locates files across multiple source directories
2. **StaticFilesMiddleware** -- serves files with proper HTTP caching (development and production)
3. **ManifestStaticFilesStorage** -- collects files with content-hash filenames and generates a JSON manifest

## StaticFilesFinder

Low-level file finding across multiple directories. Supports prefixed directories for namespacing.

```python
from hyperdjango.staticfiles import StaticFilesFinder

finder = StaticFilesFinder(
    dirs=[
        "static",  # Your app's static files
        "node_modules/htmx.org/dist",  # NPM packages
        ("vendor", "/path/to/vendor/static"),  # Prefixed directory
    ]
)
```

### `find(path)`

Find the absolute filesystem path for a static file. Returns `None` if not found. First match wins across directories.

```python
abs_path = finder.find("css/style.css")  # "static/css/style.css"
abs_path = finder.find("vendor/jquery.js")  # "/path/to/vendor/static/jquery.js"
```

**Path traversal prevention**: Paths with `..`, absolute paths, and symlink escapes outside the root are blocked.

### `list_all(ignore_patterns=None)`

List all static files as `(relative_path, absolute_path)` pairs. Deduplicates by relative path (first occurrence wins). Results are sorted alphabetically.

```python
all_files = finder.list_all()
# [("css/style.css", "/abs/path/static/css/style.css"), ...]

# With ignore patterns
all_files = finder.list_all(ignore_patterns=["*.map", "*.less"])
```

Hidden files (starting with `.`) and hidden directories are automatically skipped.

### Prefixed Directories

Prefix a directory to namespace its contents:

```python
finder = StaticFilesFinder(
    dirs=[
        "static",
        ("vendor", "third_party/static"),
        ("admin", "hyperdjango/admin/static"),
    ]
)

# "vendor/jquery.js" resolves to "third_party/static/jquery.js"
# "admin/admin.css" resolves to "hyperdjango/admin/static/admin.css"
# "css/app.css" resolves to "static/css/app.css"
```

## StaticFilesMiddleware

Serve static files with proper HTTP caching headers. Suitable for both development and production use.

### Basic Setup

```python
from hyperdjango import HyperApp
from hyperdjango.staticfiles import StaticFilesMiddleware

app = HyperApp("myapp", static="static")

app.use(
    StaticFilesMiddleware(
        static_dirs=["static", "node_modules/bootstrap/dist"],
        prefix="/static/",
        max_age=3600,  # 1 hour cache
        gzip_min_size=1024,  # Compress files > 1KB
    )
)
```

### Parameters

| Parameter         | Type                           | Default             | Description                                           |
| ----------------- | ------------------------------ | ------------------- | ----------------------------------------------------- |
| `static_dirs`     | `list[str \| tuple[str, str]]` | `[]`                | Source directories (dev mode)                         |
| `static_root`     | `str \| None`                  | `None`              | Collected static root (production mode)               |
| `prefix`          | `str`                          | `"/static/"`        | URL prefix to intercept                               |
| `max_age`         | `int`                          | `3600`              | `Cache-Control: max-age` in seconds                   |
| `immutable`       | `bool`                         | `False`             | Add `immutable` directive to Cache-Control            |
| `gzip_min_size`   | `int`                          | `1024`              | Minimum file size for gzip compression (0 to disable) |
| `use_cache`       | `bool`                         | `True`              | Enable in-memory file cache                           |
| `max_cache_bytes` | `int`                          | `268435456` (256MB) | Maximum cache size in bytes (0 = unlimited)           |

### Features

#### ETag and Conditional Responses

Every served file gets an ETag header (MD5 of content, first 12 hex chars). The middleware handles conditional requests:

- **`If-None-Match`** -- if the client's ETag matches, returns `304 Not Modified` with no body
- **`If-Modified-Since`** -- if the file hasn't changed since the given date, returns `304 Not Modified`

```
GET /static/css/style.css
→ 200 OK
  ETag: "a1b2c3d4e5f6"
  Last-Modified: Thu, 22 Mar 2026 14:30:00 GMT
  Cache-Control: max-age=3600, public

GET /static/css/style.css
If-None-Match: "a1b2c3d4e5f6"
→ 304 Not Modified
```

#### Cache-Control Headers

The `Cache-Control` header always includes `max-age` and `public`:

```
Cache-Control: max-age=3600, public
```

For content-hashed filenames (e.g., `styles.a1b2c3d4e5f6.css`), the `immutable` directive is automatically added even without `immutable=True`:

```
Cache-Control: max-age=31536000, public, immutable
```

The detection requires exactly 12 hex characters between the last two dots in the filename to minimize false positives on filenames like `chart.d3.js`.

#### Gzip Compression

Files above `gzip_min_size` bytes are automatically compressed for clients that send `Accept-Encoding: gzip`. Compressible content types:

- `text/html`, `text/css`, `text/javascript`, `text/plain`, `text/xml`
- `application/json`, `application/javascript`, `application/xml`
- `application/xhtml+xml`, `application/manifest+json`, `application/wasm`
- `image/svg+xml`

When caching is enabled, files are pre-compressed once and the gzip version is cached alongside the original. When caching is disabled, compression happens on-demand per request.

The response includes `Vary: Accept-Encoding` to ensure intermediate caches store separate versions.

#### Content-Type Detection

Content types are auto-detected via Python's `mimetypes` module. Falls back to `application/octet-stream` for unknown types.

#### In-Memory File Cache

When `use_cache=True` (default), served files are cached in an LRU OrderedDict with thread-safe access:

- Each entry stores: raw content, pre-compressed content, content type, ETag, mtime
- LRU eviction when cache exceeds `max_cache_bytes` (default 256MB)
- Thread-safe via `threading.Lock`

```python
# Clear the cache manually (e.g., after deploying new files)
middleware.clear_cache()
```

#### Path Traversal Prevention

All request paths are sanitized:

1. Percent-decoded (`%2e%2e` becomes `..`)
2. Normalized via `os.path.normpath`
3. Rejected if they start with `..` or are absolute paths
4. Symlink resolution checked to ensure the resolved path is within the root directory

#### HEAD Support

HEAD requests return all headers (Content-Type, ETag, Cache-Control, Content-Length) without the body.

### Multiple Source Directories

```python
app.use(
    StaticFilesMiddleware(
        static_dirs=[
            "static",  # Your app's static files
            "node_modules/htmx.org/dist",  # NPM packages
            ("vendor", "third_party/static"),  # Prefixed directory
        ],
    )
)
# "vendor/jquery.js" resolves to "third_party/static/jquery.js"
```

### Production Serving

Serve collected (hashed) files with long-lived immutable caching:

```python
app.use(
    StaticFilesMiddleware(
        static_root="staticfiles",
        prefix="/static/",
        max_age=31536000,  # 1 year
        immutable=True,  # Cache-Control: immutable
    )
)
```

Content-hashed filenames are auto-detected as immutable even without `immutable=True`.

## ManifestStaticFilesStorage

Collect static files with content-hash filenames and generate a JSON manifest. This enables far-future cache headers in production since the filename changes when the content changes.

### How It Works

1. **Find** all files across source directories (first occurrence wins)
2. **Hash** each file's content with MD5, take first 12 hex chars
3. **Rename** to `name.hash.ext` (e.g., `styles.a1b2c3d4e5f6.css`)
4. **Rewrite** CSS `url()` and `@import` references to hashed filenames
5. **Iterate** CSS rewriting up to 5 passes for transitive references (CSS referencing CSS that references images)
6. **Write** `staticfiles.json` manifest mapping original names to hashed names
7. Both the original and hashed versions are saved (so old URLs still work during rollout)

### Python API

```python
from hyperdjango.staticfiles import ManifestStaticFilesStorage

storage = ManifestStaticFilesStorage(
    static_dirs=["static", "node_modules/bootstrap/dist"],
    static_root="staticfiles",
)

result = storage.collectstatic(clear=True)
# result = {"copied": 42, "post_processed": 2, "skipped": 0, "errors": []}
```

### Parameters

| Parameter                 | Type                           | Default              | Description                                   |
| ------------------------- | ------------------------------ | -------------------- | --------------------------------------------- |
| `static_dirs`             | `list[str \| tuple[str, str]]` | `[]`                 | Source directories                            |
| `static_root`             | `str`                          | `"staticfiles"`      | Output directory                              |
| `manifest_name`           | `str`                          | `"staticfiles.json"` | Manifest filename                             |
| `hash_length`             | `int`                          | `12`                 | Number of hex chars in hash (from MD5 digest) |
| `max_post_process_passes` | `int`                          | `5`                  | Maximum CSS rewriting iterations              |

### `collectstatic(clear=False, dry_run=False)`

Collect all static files into `static_root` with hashed filenames.

| Option         | Description                                                  |
| -------------- | ------------------------------------------------------------ |
| `clear=True`   | Remove all existing files in `static_root` before collecting |
| `dry_run=True` | Preview what would happen without writing files or manifest  |

Returns a dict with stats:

```python
{
    "copied": 42,  # Number of files copied
    "post_processed": 2,  # Number of CSS rewriting passes
    "skipped": 0,  # Unchanged files (not applicable with clear)
    "errors": [],  # List of error strings
}
```

### CLI Command

```bash
hyper collectstatic --static-dirs static --static-root staticfiles

# Options
hyper collectstatic --clear              # Remove old files first
hyper collectstatic --dry-run            # Preview without writing
hyper collectstatic --no-post-process    # Skip CSS url() rewriting
```

### URL Resolution

Look up the hashed filename for a static file:

```python
# After collectstatic
storage.url("css/styles.css")
# → "css/styles.a1b2c3d4e5f6.css"

storage.url("js/app.js")
# → "js/app.7890abcdef12.js"

# Strict lookup -- raises ValueError if not in manifest
storage.stored_name("css/styles.css")

# Non-strict -- returns original name if not found
storage.stored_name("css/styles.css", strict=False)
```

### Load Manifest

The manifest is loaded lazily on first `url()` call, or can be loaded explicitly:

```python
manifest = storage.load_manifest()
# {"css/styles.css": "css/styles.a1b2c3d4e5f6.css", ...}
```

### Manifest Format

```json
{
  "version": "1.1",
  "paths": {
    "css/styles.css": "css/styles.a1b2c3d4e5f6.css",
    "js/app.js": "js/app.7890abcdef12.js",
    "img/logo.png": "img/logo.deadbeef0123.png"
  },
  "hash": "abc123def456"
}
```

The top-level `hash` is an MD5 of all path mappings, used to detect when the manifest itself has changed.

## CSS URL Rewriting

During `collectstatic`, CSS files are automatically post-processed to rewrite references to their hashed equivalents.

### Patterns Matched

| Pattern   | Example                                         |
| --------- | ----------------------------------------------- |
| `url()`   | `url("../img/bg.png")`, `url(fonts/icon.woff2)` |
| `@import` | `@import "base.css"`, `@import 'reset.css'`     |

### Example

```css
/* Before */
body {
  background: url("../img/bg.png");
}
@import "base.css";

/* After */
body {
  background: url("../img/bg.deadbeef0123.png");
}
@import "base.a1b2c3d4e5f6.css";
```

### Preserved Unchanged

The rewriter skips references that should not be modified:

- Absolute URLs: `https://...`, `//...`
- Data URIs: `data:...`
- Fragment-only references: `#...`

### Query Strings and Fragments

Query strings and fragments are preserved through rewriting:

```css
/* Before */
src: url("fonts/icon.woff2?v=4.7.0#iefix");

/* After */
src: url("fonts/icon.ab12cd34ef56.woff2?v=4.7.0#iefix");
```

### Transitive References

CSS files that `@import` other CSS files which reference images require multiple passes. The `max_post_process_passes` parameter (default 5) controls the maximum number of iterations. Rewriting stops early if no changes are detected in a pass.

## Templates

Use `{{ static('path') }}` or `{{ 'path'|static }}` in templates:

```html
<link rel="stylesheet" href="{{ static('css/styles.css') }}" />
<script src="{{ static('js/app.js') }}"></script>
<img src="{{ 'img/logo.png'|static }}" />
```

With a manifest loaded, these resolve to hashed URLs automatically.

## `get_static_url()`

Resolve a static file name to its full URL path. This is the function behind `{{ static('path') }}` in templates:

```python
from hyperdjango.staticfiles import get_static_url

url = get_static_url("css/styles.css")
# Dev (no manifest):  "/static/css/styles.css"
# Prod (with manifest): "/static/css/styles.a1b2c3d4e5f6.css"

# Custom prefix
url = get_static_url("css/styles.css", prefix="/assets/")
# → "/assets/css/styles.a1b2c3d4e5f6.css"
```

### Global Manifest Configuration

Set the global manifest storage instance used by `get_static_url()` and templates:

```python
from hyperdjango.staticfiles import (
    ManifestStaticFilesStorage,
    set_manifest_storage,
    get_manifest_storage,
)

storage = ManifestStaticFilesStorage(
    static_dirs=["static"],
    static_root="staticfiles",
)
storage.load_manifest()
set_manifest_storage(storage)

# Now get_static_url() and templates resolve hashed URLs
get_static_url("css/styles.css")  # → "/static/css/styles.a1b2c3d4e5f6.css"
```

## CDN Patterns

### Serving via CDN

Use a CDN prefix with `get_static_url()`:

```python
url = get_static_url("css/styles.css", prefix="https://cdn.myapp.com/static/")
# → "https://cdn.myapp.com/static/css/styles.a1b2c3d4e5f6.css"
```

### CDN Cache Invalidation

Content-hashed filenames eliminate the need for cache invalidation. When a file changes, its hash changes, producing a new URL. The old URL continues to work (both versions are stored) during the transition.

## Development vs Production

### Development

Serve source files directly with short-lived caching:

```python
app.use(
    StaticFilesMiddleware(
        static_dirs=["static", "node_modules"],
        prefix="/static/",
        max_age=0,  # No caching (reload on every request)
        gzip_min_size=0,  # No compression
        use_cache=False,  # No file caching (always read from disk)
    )
)
```

### Production

Collect with hashed filenames, serve with immutable far-future headers:

```python
# At deploy time:
storage = ManifestStaticFilesStorage(
    static_dirs=["static"],
    static_root="staticfiles",
)
storage.collectstatic(clear=True)

# At runtime:
set_manifest_storage(storage)
app.use(
    StaticFilesMiddleware(
        static_root="staticfiles",
        prefix="/static/",
        max_age=31536000,  # 1 year
        immutable=True,
    )
)
```

## Complete Example

```python
from hyperdjango import HyperApp
from hyperdjango.staticfiles import (
    StaticFilesMiddleware,
    ManifestStaticFilesStorage,
    set_manifest_storage,
    get_static_url,
)

app = HyperApp("myapp")

# Production setup
storage = ManifestStaticFilesStorage(
    static_dirs=["static", "node_modules/bootstrap/dist"],
    static_root="staticfiles",
)
storage.load_manifest()
set_manifest_storage(storage)

app.use(
    StaticFilesMiddleware(
        static_root="staticfiles",
        prefix="/static/",
        max_age=31536000,
        immutable=True,
        gzip_min_size=1024,
    )
)


@app.get("/")
async def homepage(request):
    css_url = get_static_url("css/styles.css")
    return Response.html(f'<link rel="stylesheet" href="{css_url}">')
```

## Tuning Settings

These settings control internal behavior of the static files pipeline. All are
read via `get_setting()` and can be set through Django settings, environment
variables (`HYPER_*`), `.env` files, or the DEFAULTS dict.

| Setting                               | Type | Default    | Description                                                           |
| ------------------------------------- | ---- | ---------- | --------------------------------------------------------------------- |
| `STATIC_URL`                          | str  | `/static/` | URL prefix for serving static files                                   |
| `STATIC_ROOT`                         | str  | `""`       | Collected static files output directory                               |
| `STATIC_MAX_AGE`                      | int  | `3600`     | Cache-Control max-age in seconds (dev mode)                           |
| `STATICFILES_DIRS`                    | list | `[]`       | Additional source directories                                         |
| `STATIC_CACHE`                        | bool | `False`    | Enable in-memory static file cache (dev)                              |
| `STATICFILES_GZIP_MIN_SIZE`           | int  | `1024`     | Minimum file size (bytes) before gzip compression                     |
| `STATICFILES_HASH_LENGTH`             | int  | `12`       | Hex chars in content-hash filenames (e.g., `styles.a1b2c3d4e5f6.css`) |
| `STATICFILES_MAX_POST_PROCESS_PASSES` | int  | `5`        | Max CSS `url()` rewrite iterations during collectstatic               |
| `STATICFILES_DEV_HASH_CACHE_MAX`      | int  | `4096`     | Max entries in dev-mode `?v=hash` file hash cache                     |
