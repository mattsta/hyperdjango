# Versioning and Cache Busting

HyperDjango provides a layered asset versioning system that ensures clients
always load the correct version of static files, detects stale-client
conditions during HTMX-boosted navigation, routes version cohorts through a
rolling deploy, and lets the operator decide — per deploy, from the server —
what a stale client should do about it.

## Architecture

Two loops, closed end to end.

**Server → client.** Every response carries the version that produced it plus
the operator's policy for stale clients. **Client → server.** Every request an
instrumented page makes carries that page's own version, so any load balancer
can pin a cohort with a plain request-side map.

```text
                  ┌──────────────────────────────────────────┐
                  │  1. content hash per static file          │
                  │  2. one app version (AppVersion)          │
                  └────────────────────┬─────────────────────┘
                                       │
    ┌──────────────────────────────────▼──────────────────────────────────┐
    │  3. VersionMiddleware stamps every response                          │
    │       X-App-Version: <version>                                       │
    │       X-App-Version-Action: prompt | reload | warn | ignore          │
    │     …and bakes the version into HTML responses                       │
    └──────────────────────────────────┬──────────────────────────────────┘
                                       │  page loads
    ┌──────────────────────────────────▼──────────────────────────────────┐
    │  4. the page broadcasts its OWN version on every request             │
    │       X-Client-Version: <baked version>   (htmx + fetch)             │
    │       Cookie: hyper_client_version=…      (full-page navigations)    │
    └──────────────────────────────────┬──────────────────────────────────┘
                                       │
    ┌──────────────────────────────────▼──────────────────────────────────┐
    │  5. the load balancer pins the cohort — a request-side map on the    │
    │     header (or cookie). No response post-processing, no Lua.         │
    │     VersionRouterMiddleware is the app-aware alternative.            │
    └──────────────────────────────────┬──────────────────────────────────┘
                                       │  mismatch revealed
    ┌──────────────────────────────────▼──────────────────────────────────┐
    │  6. the SERVING backend dictates the response: the action header     │
    │     it emits is what the stale page obeys — flippable per deploy,    │
    │     no client change, no redeploy of the old bundle.                 │
    └──────────────────────────────────────────────────────────────────────┘
```

The layers underneath:

1. **Per-file content hashing** — each static file gets a content-hash filename
   (`styles.a1b2c3d4e5f6.css`) via `ManifestStaticFilesStorage`. Files with
   hashed names are cached for one year with `Cache-Control: immutable`.

2. **App-level version** — a single hash derived from all static assets (the
   manifest `hash` field) or explicitly set. Exposed as the `X-App-Version`
   header, a template global, and the `/version` endpoint.

3. **Cohort routing** — the page broadcasts its own version on every request;
   a proxy or `VersionRouterMiddleware` pins it to a matching backend.

4. **Operator-owned reload policy** — whether a new version is reload-worthy
   is an operator decision advertised by the server, not a constant compiled
   into the page.

## Quick Start

```python
from hyperdjango import HyperApp
from hyperdjango.standalone_middleware import (
    CompressionMiddleware,
    VersionMiddleware,
)

app = HyperApp(title="MyApp", static="static")

# Compression FIRST — see "Middleware order" below. This is enforced.
app.use(CompressionMiddleware())
# Version headers + cohort broadcast + stale-client policy
app.use(VersionMiddleware())

# Mount /version endpoint + /cache/bust
app.mount_version()
```

In templates:

```html
{# Production: content-hash filename (already cache-safe) #}
<link rel="stylesheet" href="{{ static('css/styles.css') }}" />

{# Dev mode: appends ?v=hash from file content #}
<link rel="stylesheet" href="{{ static_url('css/styles.css') }}" />

{# App version for manual use #}
<meta name="app-version" content="{{ app_version() }}" />
```

### Middleware order

`VersionMiddleware` injects `<script>` tags into HTML bodies, so it must see
**uncompressed** bytes. `MiddlewareStack` runs the first-registered middleware
as the outermost one, so responses unwind innermost-first: whichever of the two
was registered **last** touches the body **first**. Therefore
`CompressionMiddleware` must be registered _before_ `VersionMiddleware`.

The wrong order is a hard startup error (raised from `HyperApp.run()`, in debug
mode too — a silently un-instrumented page is a misconfiguration, not a
prod-only risk):

```text
RuntimeError: Middleware order: VersionMiddleware is registered before
CompressionMiddleware, so compression runs first on the way out and
VersionMiddleware would inject its <script> into already-gzipped bytes …
```

As a runtime backstop, injection is also skipped outright on any response that
already carries a `Content-Encoding` header (logged once per process at debug
level), rather than corrupting the body.

An invalid `APP_VERSION_MISMATCH` value fails the same startup check.

## Development Mode

In development (no `collectstatic`), the `static_url()` template helper
appends a `?v=<content_hash>` query parameter computed from the file's actual
bytes:

```text
/static/css/styles.css?v=a1b2c3d4e5f6
```

When the file content changes, the hash changes, so browsers fetch the new
version. An mtime-based cache avoids re-hashing on every template render.

Controlled by `STATIC_DEV_VERSION_QUERY` (default: `True`).

## Production Mode

Run `collectstatic` to generate content-hash filenames and a manifest:

```bash
uv run hyper collectstatic --static-dirs static --static-root staticfiles
```

This produces:

- `staticfiles/css/styles.a1b2c3d4e5f6.css` — hashed filename
- `staticfiles/staticfiles.json` — manifest mapping originals to hashed names

The manifest includes a top-level `hash` field derived from all file hashes.
This becomes the app-level version automatically.

Both `{{ static('path') }}` and `{{ static_url('path') }}` resolve to the
hashed filename in production.

## Response Headers

When `APP_VERSION_HEADER` is `True` (default), **every** HTTP response — HTML,
JSON, anything — includes both of:

```http
X-App-Version: a1b2c3d4e5f6
X-App-Version-Action: prompt
```

`X-App-Version` is the version that produced this response, resolved in this
order:

1. Explicit `APP_VERSION` setting (git SHA, semver, etc.)
2. Manifest `hash` from `staticfiles.json`
3. Computed hash from registered component files
4. Fallback: `"unknown"`

`X-App-Version-Action` is the **operator's** policy for what a stale client
should do, resolved from `APP_VERSION_MISMATCH`. It is emitted on every
response, including when it is `ignore` — that value is itself a directive
telling already-instrumented older clients to stand down.

This is the load-bearing property of the design: **the backend that reveals the
mismatch dictates the response to it.** A page baked at v1 carries handlers for
every action, so the operator can change policy at deploy time — or mid-deploy,
by restarting the v2 pool with a different value — without shipping any new
client code.

Both headers are sanitized and cached; they are rebuilt only when the version
string itself changes.

## HTML Instrumentation

For `text/html` responses with a body, `VersionMiddleware` injects:

- before `</head>` — `window.__hyperAppVersion` (the version, JSON-encoded to
  keep a crafted version string out of script context) followed by the
  `window.hyperVersion` client API;
- before `</body>` — the mismatch / cohort-broadcast script.

Injection requires a `</body>` in the document; fragments without one are left
untouched. Content-encoded responses are skipped (see _Middleware order_).

### `window.hyperVersion`

| Member            | Description                                                                                              |
| ----------------- | -------------------------------------------------------------------------------------------------------- |
| `.version`        | The version baked into this page.                                                                        |
| `.headers()`      | Cohort headers for hand-written requests: `{"X-Client-Version": "<version>"}` (`{}` when broadcast off). |
| `.reload()`       | Reload the page.                                                                                         |
| `.onMismatch(cb)` | Sugar for `addEventListener('hyper:version-mismatch', cb)`.                                              |

Use `.headers()` for any request htmx does not make for you:

```js
fetch("/api/items", { headers: window.hyperVersion.headers() });
```

The API stands down entirely (no cookie, no globals beyond the version tag) if
`window.__hyperAppVersion` is absent.

## Cohort Broadcast (Client → Server)

Gated by `APP_VERSION_CLIENT_BROADCAST` (default `True`). When on, an
instrumented page announces its own version two ways:

- **`X-Client-Version` header** — added to every htmx request by an
  `htmx:configRequest` listener, and available to hand-written requests via
  `window.hyperVersion.headers()`. This is per-tab truth.
- **`hyper_client_version` cookie** — written by the head script with
  `path=/; SameSite=Lax`. It covers full-page navigations and un-instrumented
  fetches, which have no opportunity to set a header.

The header wins when both are present.

### Server-side parsing

`VersionMiddleware` parses the inbound value **before** the handler runs, so
handlers and downstream middleware read the same attribute:

```python
async def handler(request):
    request.client_version  # "" when the client sent nothing
```

The standalone parser is available directly:

```python
from hyperdjango.versioning import client_version

version = client_version(request)  # header first, then cookie
```

The value is attacker-controlled, so it is CRLF-sanitized and length-capped
before it is returned, stored, or used as a metric label.

The inbound parse has exactly one switch (`APP_VERSION_CLIENT_BROADCAST`) and is
independent of `APP_VERSION_HEADER` — an operator may route on the client's
version without echoing the server's on every response.

### Skew metric

Every parsed request is classified into a bounded telemetry counter:

```text
hyperdjango_version_skew_requests_total{relation="match"}        # client == serving version
hyperdjango_version_skew_requests_total{relation="skew"}         # client != serving version
hyperdjango_version_skew_requests_total{relation="unversioned"}  # no client version at all
```

There is no stale/newer split: app versions are opaque hashes with no ordering.
`unversioned` is normal traffic — API clients, bots, health checks, and the
first request of a page load, before any script runs.

Like all HyperDjango metrics this is zero-cost when telemetry is disabled (the
production default): one module-level flag check and a branch.

## Mismatch Detection and Policy

HTMX-boosted navigation replaces page content without reloading the full page.
If the server deploys a new version mid-session, the client's JavaScript and
CSS become stale.

The injected script listens for `htmx:afterRequest`, compares the response's
`X-App-Version` against the page's `window.__hyperAppVersion`, and — on a
difference — reads `X-App-Version-Action` from the same response. An
unrecognized or missing action header falls back to the action baked into the
page at render time.

### The event comes first

Before any built-in behavior, a **cancelable** DOM event fires:

```js
document.addEventListener("hyper:version-mismatch", (e) => {
  e.detail; // { current: "<page version>", server: "<serving version>", action: "prompt" }
  e.preventDefault(); // the app owns the UX from here — no banner, no reload
});

// equivalent sugar:
window.hyperVersion.onMismatch((e) => {
  /* … */
});
```

The event bubbles and is cancelable. `preventDefault()` suppresses everything
that follows, which is the supported way to render your own toast, badge, or
modal while keeping the server-driven policy signal.

### The four actions

| Action   | Behavior                                                                                                                                                                       |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `prompt` | **Default.** A dismissible "A new version is available." banner with a **Reload** button, shown at most once per serving version. User-initiated — no in-flight state is lost. |
| `reload` | Reload at the **next navigation boundary**, never mid-interaction. Guarded against reload loops; degrades to the banner. See below.                                            |
| `warn`   | `console.warn` once per serving version. Nothing visual.                                                                                                                       |
| `ignore` | As a **setting**: inject no mismatch machinery at all. As a **response header**: tell already-instrumented older clients to stand down.                                        |

The banner is dependency-free and accessible: `role="status"`, a labelled
dismiss control, and styling applied through the CSSOM (element `style`
properties, not a `<style>` tag or inline `style` attribute) so it renders under
a strict CSP without `'unsafe-inline'`. It reads the CSS custom properties
`--surface`, `--text`, `--border`, and `--accent` with literal fallbacks, so a
themed app restyles it without shipping assets.

### How `reload` avoids destroying state — and avoids loops

`reload` never calls `location.reload()` the moment a mismatch is seen. It arms
a dirty flag and acts at a navigation boundary:

- an **htmx-boosted request** (a full page swap) — intercepted, and the browser
  navigates to the requested path instead of swapping stale HTML into a stale
  page;
- a plain **left-click on a link** — the browser is already fetching fresh HTML,
  so the script only records that this counts as the reload.

Modified clicks (meta/ctrl/shift/alt), middle clicks, `#` anchors,
`javascript:` hrefs, `target=`, and `download` links are all left alone.

Both boundaries record the serving version in `sessionStorage`. If a mismatch
against the _same_ serving version is seen **again** after that — a CDN, proxy,
or service worker still handing out the old HTML — the script does **not**
reload a second time. It degrades to the banner, which is a dead end for the
user rather than a reload storm for the origin. The banner has its own
once-per-version guard.

All `sessionStorage` access is wrapped: private-mode and sandboxed-iframe
throws degrade to "no guard state", never to a broken page.

### Setting the policy

```python
# settings.py or HYPER_APP_VERSION_MISMATCH env var
HYPERDJANGO_APP_VERSION_MISMATCH = "prompt"
```

The value is validated at startup; an unknown value raises `RuntimeError` with
the allowed set rather than silently falling back.

## Release Stamps (trunk-driven releases)

For trunk-driven, forward-only development the canonical release version is a
**single unbroken digit run, UTC to the millisecond**: `YYYYMMDDHHMMSSmmm`,
e.g. `20260725143940411`. One shape serves every consumer at once:

- a valid single-segment PEP 440 package version — multi-segment date formats
  hit the leading-zero normalization trap (`.093940411` → `.93940411`), a
  single digit run cannot;
- fixed-width and lexically sortable, so string order **is** release order in
  filenames, object keys, dashboards, and cohort maps;
- header- and URL-safe, human-decodable at a glance.

Mint one with the CLI:

```bash
uv run hyper release                 # dry run: prints stamp + commit
uv run hyper release --apply         # rewrites pyproject.toml version
```

The mint is **forward-only**: the previous pyproject version is the floor,
and a wall clock reading at or behind it (NTP step, misconfigured CI runner)
clamps to `floor + 1ms` instead of ever emitting a stamp that sorts
backwards. Legacy static versions impose no floor.

The git commit deliberately stays **out** of the stamp (package indexes
reject local version suffixes; map keys stay clean). `hyper release` prints
the commit to export as `HYPER_APP_BUILD_COMMIT`, which surfaces in
`/version` metadata as `commit`, alongside `released_at` — the stamp's human
rendering (`2026-07-25 14:39:40.411Z`).

Release stamps replace the human-coordinate layer only. The content-hash
layers below (`staticfiles.json` manifest, per-file hashes) remain the cache
truth, per the design principles: version actual bytes.

Programmatic API: `format_release_stamp`, `parse_release_stamp`,
`release_stamp_display`, `mint_release_stamp` in `hyperdjango.versioning`.

## Version Endpoint

`app.mount_version()` registers two endpoints:

### GET /version

Returns app version metadata:

```json
{
  "version": "a1b2c3d4e5f6",
  "source": "manifest",
  "components": {
    "templates": 15,
    "config": 3
  }
}
```

### POST /cache/bust

Manual cache invalidation for deploy scripts. Requires an auth token:

```bash
# Generate token from SECRET_KEY
TOKEN=$(python -c "
from hyperdjango.versioning import _make_cache_bust_token
print(_make_cache_bust_token())
")

curl -X POST https://myapp.example/cache/bust \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{ "status": "ok", "version": "new_hash_here" }
```

## Component Registration

Register non-static files that contribute to the app version:

```python
app.register_version_component(
    "templates",
    [
        "templates/base.html",
        "templates/nav.html",
    ],
)
app.register_version_component(
    "config",
    [
        "config/production.json",
    ],
)
```

The app version is derived from both the static manifest hash and the
registered component file hashes.

## Blue/Green and Canary Deployments

Because every instrumented request now carries the page's own version, the
**primary** pattern is a plain request-side map in the load balancer. No
response post-processing, no Lua/njs, no app involvement.

### Pin the version string first

Cohort routing needs map keys you can write down, so set `APP_VERSION`
explicitly per release (git SHA, release tag) rather than letting it fall back
to a manifest hash that changes with any asset edit:

```bash
HYPER_APP_VERSION=v2  # in each backend's environment
```

### nginx: request-side cohort map

```nginx
# Header is per-tab truth; cookie covers full-page navigations.
map $http_x_client_version $cohort_header {
    default  "";
    "v1"     app_v1;
    "v2"     app_v2;
}
map $cookie_hyper_client_version $cohort_cookie {
    default  "";
    "v1"     app_v1;
    "v2"     app_v2;
}

# Header wins; cookie is the fallback; anything else gets the current release.
map $cohort_header $cohort_pref {
    default  $cohort_header;
    ""       $cohort_cookie;
}
map $cohort_pref $cohort {
    default  $cohort_pref;
    ""       app_v2;
}

upstream app_v1 { server 10.0.0.11:8000; }
upstream app_v2 { server 10.0.0.21:8000; }

server {
    location / {
        proxy_set_header X-Client-Version $http_x_client_version;
        proxy_pass http://$cohort;
    }
}
```

Envoy, HAProxy, and Traefik express the same thing with a header/cookie matcher
on the route — the point is that the routing key arrives **on the request**, so
it is available before the backend is chosen.

Unknown or absent versions fall through to the current release, which is what
you want for API clients, bots, and cold loads.

### `VersionRouterMiddleware` — the app-aware alternative

When the proxy cannot map versions itself, the app can signal the target:

```python
from hyperdjango.standalone_middleware import VersionRouterMiddleware

app.use(
    VersionRouterMiddleware(
        version_map={
            "v1.0": "backend-blue",
            "v1.1": "backend-green",
        },
        default_version="v1.1",
    )
)
```

The middleware:

1. Reads the client's version with the same shared parser — `X-Client-Version`
   header first, then the `hyper_client_version` cookie — falling back to
   `default_version`.
2. For a version in `version_map`, sets `x-backend-target` (the routing hint)
   and `x-app-served-version` on the response.
3. For a version **not** in `version_map`, returns `409 Conflict` with the
   unified `{"detail", "status"}` error shape; `detail` names the rejected
   version and lists the known ones.
4. With no `version_map` configured, it just stamps `x-app-served-version` with
   the serving version.

Header, response-header, and routing-header names are all constructor
parameters if your proxy expects different ones.

## Operator Runbook: a v1 → v2 rolling deploy

A worked sequence for a deploy where v2 changes templates or front-end
behavior, so mixing v1 fragments into a v2 page (or the reverse) is not safe.

**Before you start.** Both releases set `APP_VERSION` explicitly.
`APP_VERSION_CLIENT_BROADCAST` is on (default), so v1 pages already in the wild
are broadcasting `v1`. Confirm on the running fleet:

```bash
curl -sI https://myapp.example/ | grep -i '^x-app-version'
```

**1. Deploy v2 with `APP_VERSION_MISMATCH=prompt`.**

```bash
HYPER_APP_VERSION=v2
HYPER_APP_VERSION_MISMATCH=prompt
```

New page loads land on v2. Open v1 tabs keep working and keep sending
`X-Client-Version: v1`.

**2. Add the v2 cohort to the load balancer map** and keep the v1 pool up.
v1 tabs stay pinned to v1 backends, so no half-swapped fragment ever mixes
releases. This drain window is the whole reason the client broadcasts.

**3. Watch the skew drain.**

```promql
# share of traffic still on an older page
sum(rate(hyperdjango_version_skew_requests_total{relation="skew"}[5m]))
  / sum(rate(hyperdjango_version_skew_requests_total[5m]))
```

Right after the cutover this rises (every v1 tab that touches a v2 backend
counts), then falls as tabs roll over. `match` climbs in step. `unversioned`
should be roughly flat — it is API and bot traffic, not a signal about the
deploy.

**4. Let `prompt` do the work.** Every v1 page that sees a v2 response shows the
banner once and offers a Reload. Users roll themselves over without losing form
state, scroll position, or an open editor.

**5. Flip to `reload` for the stragglers — if warranted.** When skew is down to
a long tail (tabs left open overnight) and you need it at zero — a security
fix, an incompatible API shape — restart the **v2** pool with:

```bash
HYPER_APP_VERSION_MISMATCH=reload
```

Nothing client-side is redeployed. The v1 pages already loaded carry handlers
for every action and obey the new `X-App-Version-Action` on their next
response, rolling over at their next navigation boundary. This is the payoff of
the policy living on the server.

**6. Retire v1** when skew is at zero: drop the v1 pool and its map entries, and
return `APP_VERSION_MISMATCH` to `prompt` for the next cycle.

**Variation — asset-only deploy.** If v2 is a CSS tweak or a copy change and
mixing releases is harmless, ship it with `APP_VERSION_MISMATCH=warn` (or
`ignore`) and skip the cohort pinning entirely. Sent as a header, `ignore`
actively stands down clients that were instrumented under a stricter policy.

## Settings Reference

| Setting                        | Type | Default    | Description                                                       |
| ------------------------------ | ---- | ---------- | ----------------------------------------------------------------- |
| `APP_VERSION`                  | str  | `""`       | Explicit version (git SHA, semver). Empty = auto-compute          |
| `APP_VERSION_HEADER`           | bool | `True`     | Emit `X-App-Version` + `X-App-Version-Action` on all responses    |
| `APP_VERSION_MISMATCH`         | str  | `"prompt"` | Stale-client policy: `"prompt"`, `"reload"`, `"warn"`, `"ignore"` |
| `APP_VERSION_CLIENT_BROADCAST` | bool | `True`     | Client sends `X-Client-Version` + `hyper_client_version` cookie   |
| `VERSION_ENDPOINT`             | bool | `True`     | Mount `/version` when `mount_version()` called                    |
| `STATIC_DEV_VERSION_QUERY`     | bool | `True`     | Append `?v=hash` in dev mode                                      |

`APP_VERSION_CLIENT_BROADCAST` gates the whole cohort feature: the injected
header listener, the cookie, the inbound parse onto `request.client_version`,
and the skew metric.

## Design Principles

These rules should be followed by any subsystem that needs caching or
versioning:

1. **One manifest, not several unrelated version stores.** All version
   information flows through `AppVersion`.

2. **Version actual bytes.** Hash the final emitted content (compiled CSS,
   minified JS, serialized JSON), not filenames, mtimes, or deployment
   timestamps.

3. **Derive parent version from child versions.** The app-level version is
   derived from per-file hashes, so one freshness signal answers "is the
   currently loaded runtime still valid for this response?"

4. **Rewrite embedded asset refs centrally.** Templates write canonical paths
   (`css/styles.css`); the renderer injects versioned URLs. Authors never
   hardcode version query params into source content.

5. **Manual bust APIs are authoritative.** Cache refreshes happen after
   coherent operator-triggered mutations (deploys), not during half-finished
   edits or via background file watchers.

6. **Distinguish canonical paths from cacheable paths.** Canonical path =
   stable identifier. Versioned path = cacheable delivery path.

7. **Make stale-client detection explicit.** If partial-page navigation exists
   (HTMX), emit a version header and act on a mismatch.

8. **The client broadcasts its version on every request.** Freshness is not
   something the server infers from a session or a sticky cookie hash — the
   page states which build it is, on the request, where a load balancer can
   read it before choosing a backend. A routing signal that only appears on
   the response is not a routing signal.

9. **Skew policy flows from the serving backend.** What a stale client should
   do is an operations decision that changes per deploy, so it travels with the
   response that reveals the staleness, not baked into the bundle that has
   already shipped. Clients implement every action and obey the one they are
   told; and the safe default is to ask the user, never to destroy their state.
