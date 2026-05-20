"""
App-level asset versioning, cache busting, and version routing.

Provides a single app-wide freshness signal derived from the static file
manifest, explicit settings, or registered component file hashes. Supports
client-side HTMX version mismatch detection, ``X-App-Version`` response
headers, and a ``/version`` JSON endpoint.

Design principles (adapted from CMS caching architecture):
- One manifest, not several unrelated version stores.
- Version actual bytes, not filenames/mtimes/deployment timestamps.
- Derive parent version from child versions (one freshness signal).
- Manual bust APIs are authoritative (no background file watchers for invalidation).
- Make stale-client detection explicit.
- Zero cost when disabled (settings-driven opt-in).

Two extensions build on those principles:

**Cohort routing.** Every request an instrumented page makes carries the
page's OWN version (``X-Client-Version`` header, ``hyper_client_version``
cookie), so a load balancer can pin v1 frontends to v1 backends through a
rolling deploy — a plain request-side map, no response post-processing.

**Operator-owned reload policy.** Whether a new version is "reload-worthy"
is an operator decision advertised BY THE SERVER at mismatch time via
``X-App-Version-Action``. Clients never force-reload and destroy user
state; the default UX is a user-initiated "new version available → Reload"
prompt.

Usage::

    from hyperdjango import HyperApp
    from hyperdjango.standalone_middleware import VersionMiddleware

    app = HyperApp(...)
    app.use(VersionMiddleware())
    app.mount_version()  # GET /version endpoint + POST /cache/bust
"""

from __future__ import annotations

import contextlib
import hashlib
import json as _json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyperdjango.app import HyperApp

from hyperdjango.conf import get_setting
from hyperdjango.logging import logger as _logger
from hyperdjango.native import fast_json_loads
from hyperdjango.native._crypto import (
    hmac_sha256_hex_truncated,
    hmac_sha256_verify_truncated,
)
from hyperdjango.request import Request
from hyperdjango.response import Response, _sanitize_header
from hyperdjango.staticfiles import get_manifest_storage

# ---------------------------------------------------------------------------
# Version cohort protocol — header + cookie names
# ---------------------------------------------------------------------------

#: Server → client: the version that produced this response.
APP_VERSION_HEADER_NAME = "x-app-version"

#: Server → client: the OPERATOR's policy for what a stale client should do.
#: One of :data:`VERSION_ACTIONS`. Emitted whenever the version header is.
APP_VERSION_ACTION_HEADER_NAME = "x-app-version-action"

#: Client → server: the version of the page making the request (per-tab truth).
CLIENT_VERSION_HEADER_NAME = "x-client-version"

#: Client → server fallback: set by the head script so full-page navigations
#: and un-instrumented fetches are routable too. The header wins when both
#: are present.
CLIENT_VERSION_COOKIE_NAME = "hyper_client_version"

#: Valid ``APP_VERSION_MISMATCH`` settings values and valid
#: ``X-App-Version-Action`` header values.
#:
#: - ``prompt``  — show a dismissible "new version available" banner (default).
#: - ``reload``  — reload at the next navigation boundary (never mid-state).
#: - ``warn``    — ``console.warn`` once per server version.
#: - ``ignore``  — do nothing. As a BAKED setting this means "inject no
#:   mismatch script at all"; as a HEADER it tells already-instrumented
#:   (older-version) clients to stand down.
VERSION_ACTIONS: frozenset[str] = frozenset({"prompt", "reload", "warn", "ignore"})

# ---------------------------------------------------------------------------
# Release stamps — the canonical trunk-driven release version format
# ---------------------------------------------------------------------------
#
# A release version is a single unbroken digit run, UTC to the millisecond:
# ``YYYYMMDDHHMMSSmmm`` (e.g. ``20260725143940411``). One shape serves every
# consumer at once: a valid single-segment PEP 440 package version (no
# multi-segment leading-zero normalization traps), fixed-width and lexically
# sortable for filenames/object keys/dashboards, header- and URL-safe, and
# human-decodable at a glance. Display formatting is a presentation concern —
# ``release_stamp_display`` renders the ISO form deterministically. The git
# commit deliberately stays OUT of the stamp (package indexes reject local
# version suffixes; cohort map keys stay clean) — /version metadata carries it.

RELEASE_STAMP_LENGTH = 17
_RELEASE_STAMP_RE = re.compile(r"^\d{17}$")
_STAMP_FMT = "%Y%m%d%H%M%S"


def format_release_stamp(dt: datetime) -> str:
    """Render an aware datetime as a canonical release stamp (UTC ms)."""
    utc = dt.astimezone(UTC)
    return f"{utc.strftime(_STAMP_FMT)}{utc.microsecond // 1000:03d}"


def parse_release_stamp(stamp: str) -> datetime:
    """Parse a canonical release stamp back to an aware UTC datetime.

    Raises ValueError for anything that is not exactly 17 digits encoding a
    real instant — a stamp either round-trips or is rejected, never guessed.
    """
    if not _RELEASE_STAMP_RE.match(stamp):
        raise ValueError(
            f"not a release stamp: {stamp!r} (expected {RELEASE_STAMP_LENGTH} "
            f"digits, YYYYMMDDHHMMSSmmm UTC)"
        )
    base = datetime.strptime(stamp[:14], _STAMP_FMT).replace(tzinfo=UTC)
    return base.replace(microsecond=int(stamp[14:]) * 1000)


def release_stamp_display(stamp: str) -> str:
    """Human rendering of a stamp: ``2026-07-25 14:39:40.411Z``."""
    dt = parse_release_stamp(stamp)
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def mint_release_stamp(last: str = "", now: datetime | None = None) -> str:
    """Mint the next release stamp, enforcing forward-only ordering.

    Trunk-driven releases only move forward: when the wall clock reads at or
    before the previously released stamp (NTP step, wrong-timezone CI
    runner), the mint clamps to ``last + 1ms`` instead of emitting a stamp
    that sorts backwards. ``last`` values that are not stamps (the legacy
    static version, empty) impose no floor.
    """
    minted = format_release_stamp(now if now is not None else datetime.now(UTC))
    try:
        floor = parse_release_stamp(last)
    except ValueError:
        return minted
    if parse_release_stamp(minted) <= floor:
        return format_release_stamp(floor + timedelta(milliseconds=1))
    return minted


# ---------------------------------------------------------------------------
# Version string escaping — prevent XSS in injected script tags
# ---------------------------------------------------------------------------


# Characters that break JavaScript string literal context inside double quotes.
# We JSON-encode to get safe escaping of quotes, backslashes, newlines, etc.
def _escape_version_for_js(version: str) -> str:
    """Escape a version string for safe embedding in a JS string literal.

    Uses JSON encoding which handles all dangerous characters:
    double quotes, backslashes, newlines, carriage returns, tabs,
    null bytes, and Unicode escapes.
    """
    return _json.dumps(version)


# ---------------------------------------------------------------------------
# Client scripts — cohort broadcast + operator-owned mismatch policy
# ---------------------------------------------------------------------------
#
# The scripts are hand-tight source (no minifier) and are built ONCE per
# (action, broadcast) policy — never per request. Only the tiny version tag
# (``window.__hyperAppVersion=…``) varies with the version itself, and the
# middleware caches that per version change too.

# Broadcast bits of the HEAD script: write the cohort cookie so full-page
# navigations and un-instrumented fetches are routable without any JS.
_HEAD_COOKIE_JS = (
    "try{d.cookie='"
    + CLIENT_VERSION_COOKIE_NAME
    + "='+encodeURIComponent(v)+'; path=/; SameSite=Lax';}catch(e){}"
)

# Broadcast bits of the BODY script: stamp every htmx request with the page's
# own version so the load balancer can pin this tab to a matching backend.
_BODY_CONFIG_REQUEST_JS = (
    "d.addEventListener('htmx:configRequest',function(e){"
    "if(e.detail&&e.detail.headers)e.detail.headers['X-Client-Version']=v;});"
)


def _build_head_script(broadcast: bool) -> str:
    """Build the ``</head>`` script: cohort cookie + ``window.hyperVersion``.

    Must be injected AFTER the ``window.__hyperAppVersion`` version tag —
    it reads that global and stands down when it is absent.
    """
    headers_js = "{'X-Client-Version':v}" if broadcast else "{}"
    cookie_js = _HEAD_COOKIE_JS if broadcast else ""
    return (
        "<script>(function(){"
        "var d=document,v=window.__hyperAppVersion;"
        "if(!v)return;"
        f"{cookie_js}"
        "window.hyperVersion={version:v,"
        f"headers:function(){{return {headers_js};}},"
        "reload:function(){location.reload();},"
        "onMismatch:function(cb){d.addEventListener('hyper:version-mismatch',cb);}};"
        "})();</script>"
    )


def _build_body_script(action: str, broadcast: bool) -> str:
    """Build the ``</body>`` script for one (action, broadcast) policy.

    ``action`` is only the BAKED FALLBACK: every response may override it
    with an ``X-App-Version-Action`` header, so the script always carries
    all four handlers. A page baked at v1 therefore honors a policy the
    operator changes in v2 — which is the whole point of advertising the
    action from the server.

    ``action == "ignore"`` keeps its historical meaning for the baked
    setting: no mismatch machinery is emitted at all. The broadcast
    listener still ships when broadcast is on, because cohort routing is
    a separate feature with its own switch.
    """
    config_request = _BODY_CONFIG_REQUEST_JS if broadcast else ""
    if action == "ignore":
        if not config_request:
            return ""
        return (
            "<script>(function(){"
            "var d=document,v=window.__hyperAppVersion;"
            "if(!v)return;"
            f"{config_request}"
            "})();</script>"
        )

    safe_action = _escape_version_for_js(action)
    return (
        "<script>(function(){"
        "var d=document,v=window.__hyperAppVersion;"
        "if(!v)return;"
        f"var HYPER_ACTION={safe_action},pending='',warned={{}};"
        "var KP='hyper_prompted_for',KR='hyper_reloaded_for';"
        # sessionStorage can throw (Safari private mode, sandboxed iframe).
        "function ss(){try{return window.sessionStorage;}catch(e){return null;}}"
        "function mark(sv){var s=ss();if(s)try{s.setItem(KR,sv);}catch(e){}}"
        "function reloadFor(sv){mark(sv);location.reload();}"
        # Minimal, dependency-free banner. Styles go through element.style
        # (CSSOM), NOT a style attribute or <style> tag, so a strict CSP
        # without 'unsafe-inline' still renders it. CSS custom properties
        # with literal fallbacks let a themed app restyle it, no assets.
        "function btn(label,css){var e=d.createElement('button');e.type='button';"
        "e.textContent=label;e.style.cssText='cursor:pointer;font:inherit;"
        "color:inherit;'+css;return e;}"
        "function banner(sv){"
        "var s=ss();if(s)try{if(s.getItem(KP)===sv)return;s.setItem(KP,sv);}catch(e){}"
        "var b=d.createElement('div');b.setAttribute('role','status');"
        "b.style.cssText='position:fixed;bottom:16px;right:16px;z-index:2147483647;"
        "display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:8px;"
        "font:14px/1.4 system-ui,sans-serif;background:var(--surface,#f8f9fa);"
        "color:var(--text,#333);box-shadow:0 2px 8px #0003';"
        "var t=d.createElement('span');t.textContent='A new version is available.';"
        "var r=btn('Reload','padding:4px 10px;border-radius:6px;"
        "border:1px solid var(--border,#ccc);background:var(--accent,#e9ecef)');"
        "r.onclick=function(){reloadFor(sv);};"
        "var x=btn('\\u00d7','border:0;background:transparent;padding:0 4px');"
        "x.setAttribute('aria-label','Dismiss');"
        "x.onclick=function(){if(b.parentNode)b.parentNode.removeChild(b);};"
        "b.appendChild(t);b.appendChild(r);b.appendChild(x);"
        "(d.body||d.documentElement).appendChild(b);}"
        f"{config_request}"
        "d.addEventListener('htmx:afterRequest',function(e){"
        "var x=e.detail&&e.detail.xhr;if(!x)return;"
        "var sv=x.getResponseHeader('X-App-Version');"
        "if(!sv||sv===v)return;"
        "var a=x.getResponseHeader('X-App-Version-Action')||'';"
        "if(a!=='prompt'&&a!=='reload'&&a!=='warn'&&a!=='ignore')a=HYPER_ACTION;"
        # The app owns the UX if it calls preventDefault() on this event.
        "if(!d.dispatchEvent(new CustomEvent('hyper:version-mismatch',"
        "{bubbles:true,cancelable:true,detail:{current:v,server:sv,action:a}})))return;"
        "if(a==='ignore')return;"
        "if(a==='warn'){if(!warned[sv]){warned[sv]=1;"
        "console.warn('[HyperDjango] App version mismatch: client='+v+' server='+sv);}"
        "return;}"
        # 'reload' never reloads mid-state: arm a dirty flag and act at the
        # next navigation boundary. If we ALREADY reloaded once for this
        # server version and are still stale (CDN/proxy serving the old
        # page), degrade to the banner instead of looping.
        "if(a==='reload'){var s=ss(),done=null;"
        "if(s)try{done=s.getItem(KR);}catch(e2){}"
        "if(done!==sv){pending=sv;return;}}"
        "banner(sv);});"
        # Navigation boundary 1: an htmx-boosted request (a full page swap).
        "d.addEventListener('htmx:beforeRequest',function(e){"
        "if(!pending)return;var t=e.detail;if(!t||!t.boosted)return;"
        "var p=t.pathInfo&&t.pathInfo.requestPath;"
        "e.preventDefault();mark(pending);"
        "if(p)location.href=p;else location.reload();},true);"
        # Navigation boundary 2: a plain link click. The browser fetches
        # fresh HTML on its own — we only record that this counts as the
        # one reload allowed for this server version.
        "d.addEventListener('click',function(e){"
        "if(!pending||e.defaultPrevented||e.button!==0)return;"
        "if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;"
        "var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;"
        "if(!a)return;var h=a.getAttribute('href')||'';"
        "if(!h||h.charAt(0)==='#'||h.slice(0,11)==='javascript:')return;"
        "if(a.target||a.hasAttribute('download'))return;"
        "mark(pending);},true);"
        "})();</script>"
    )


@dataclass(frozen=True, slots=True)
class ClientVersionScript:
    """Pre-rendered client scripts for one (action, broadcast) policy.

    Built once and cached — never rebuilt per request. ``head`` is injected
    right after the ``window.__hyperAppVersion`` version tag (before
    ``</head>``); ``body`` is injected before ``</body>`` and may be empty
    when the policy asks for no client behavior at all.
    """

    action: str
    broadcast: bool
    head: str
    body: str


_script_cache: dict[tuple[str, bool], ClientVersionScript] = {}
_script_cache_lock = threading.Lock()


def get_client_script(action: str, broadcast: bool) -> ClientVersionScript:
    """Return the cached :class:`ClientVersionScript` for a policy.

    Unknown actions fall back to the ``"prompt"`` policy — a stale-client
    banner is the state-safe default, and the setting is validated at
    startup so this only papers over programmatic callers.
    """
    if action not in VERSION_ACTIONS:
        action = "prompt"
    key = (action, broadcast)
    cached = _script_cache.get(key)
    if cached is not None:
        return cached
    with _script_cache_lock:
        cached = _script_cache.get(key)
        if cached is not None:
            return cached
        built = ClientVersionScript(
            action=action,
            broadcast=broadcast,
            head=_build_head_script(broadcast),
            body=_build_body_script(action, broadcast),
        )
        _script_cache[key] = built
        return built


def version_tag(version: str) -> str:
    """The ``window.__hyperAppVersion`` tag for a version string.

    The version is JSON-encoded to prevent XSS injection via crafted
    version strings containing quotes or script-breaking characters.
    """
    return (
        f"<script>window.__hyperAppVersion={_escape_version_for_js(version)};</script>"
    )


def inject_version_meta(html: str, version: str, mismatch_action: str) -> str:
    """Inject app version metadata and client version machinery into HTML.

    Injects ``window.__hyperAppVersion`` plus the ``window.hyperVersion``
    API before ``</head>``, and the mismatch/broadcast script before
    ``</body>``.

    The version string is JSON-encoded to prevent XSS injection via
    crafted version strings containing quotes or script-breaking chars.
    """
    broadcast = bool(get_setting("APP_VERSION_CLIENT_BROADCAST", True))
    script = get_client_script(mismatch_action, broadcast)

    # Inject window.__hyperAppVersion (+ the client API) before </head>
    head_idx = html.rfind("</head>")
    if head_idx != -1:
        html = html[:head_idx] + version_tag(version) + script.head + html[head_idx:]

    # Inject the mismatch/broadcast script before </body>
    if script.body:
        body_idx = html.rfind("</body>")
        if body_idx != -1:
            html = html[:body_idx] + script.body + html[body_idx:]

    return html


# ---------------------------------------------------------------------------
# Inbound client version (cohort routing)
# ---------------------------------------------------------------------------

# Cap on the inbound client version — it is attacker-controlled and lands in
# a metric label and (for the router) a response header.
_MAX_CLIENT_VERSION_LENGTH = 128


def client_version(
    request: Request,
    header_name: str = CLIENT_VERSION_HEADER_NAME,
    cookie_name: str = CLIENT_VERSION_COOKIE_NAME,
) -> str:
    """The version of the page that issued this request, or ``""``.

    Reads the ``X-Client-Version`` header first (per-tab truth), then falls
    back to the ``hyper_client_version`` cookie (set by the head script so
    full-page navigations and un-instrumented fetches are routable too).
    The value is attacker-controlled: it is CRLF-sanitized and length-capped
    before being returned.
    """
    raw = request.headers.get(header_name, "")
    if not raw:
        raw = request.cookies.get(cookie_name, "")
    if not raw:
        return ""
    return _sanitize_header(raw)[:_MAX_CLIENT_VERSION_LENGTH]


# ---------------------------------------------------------------------------
# AppVersion — single source of truth for the app-wide version signal
# ---------------------------------------------------------------------------

# Maximum length for explicit version strings to prevent header bloat
_MAX_VERSION_LENGTH = 256


@dataclass(slots=True)
class AppVersion:
    """Single app-wide freshness signal.

    Resolution order for ``version`` property:

    1. Explicit ``APP_VERSION`` setting (if non-empty)
    2. Manifest hash from ``staticfiles.json``
    3. Computed hash from registered component file paths
    4. Fallback: ``"unknown"``

    Thread-safe via RLock (reentrant) to allow ``_resolve()`` to call
    ``load_from_manifest()`` and ``compute_from_components()`` which
    also acquire the lock.
    """

    _explicit: str = field(default="", init=False, repr=False)
    _manifest_hash: str = field(default="", init=False, repr=False)
    _manifest_loaded: bool = field(default=False, init=False, repr=False)
    _component_paths: dict[str, list[str]] = field(
        default_factory=dict, init=False, repr=False
    )
    _computed_hash: str = field(default="", init=False, repr=False)
    _cached_version: str = field(default="", init=False, repr=False)
    # RLock (not Lock) because _resolve() calls load_from_manifest() and
    # compute_from_components() which also acquire the lock.
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    @property
    def version(self) -> str:
        """The resolved app version string (lazy, cached until invalidate)."""
        if self._cached_version:
            return self._cached_version
        with self._lock:
            if self._cached_version:
                return self._cached_version
            v = self._resolve()
            self._cached_version = v
            return v

    @property
    def source(self) -> str:
        """Which source provided the current version."""
        _ = self.version  # ensure resolved
        if self._explicit:
            return "explicit"
        if self._manifest_hash:
            return "manifest"
        if self._computed_hash:
            return "computed"
        return "unknown"

    @property
    def components(self) -> dict[str, int]:
        """Registered component groups with file counts."""
        return {name: len(paths) for name, paths in self._component_paths.items()}

    def set_explicit(self, version: str) -> None:
        """Override the version with an explicit string."""
        if len(version) > _MAX_VERSION_LENGTH:
            version = version[:_MAX_VERSION_LENGTH]
        with self._lock:
            self._explicit = version
            self._cached_version = ""

    def load_from_manifest(self, manifest_path: str = "") -> str:
        """Read the manifest hash from ``staticfiles.json``.

        Since ``ManifestStaticFilesStorage.load_manifest()`` discards the
        ``"hash"`` field, this method reads the manifest independently.
        Returns empty string on any error (missing file, bad JSON, etc.).
        """
        if not manifest_path:
            static_root = get_setting("STATIC_ROOT", "")
            if not static_root:
                with self._lock:
                    self._manifest_loaded = True
                return ""
            manifest_path = str(Path(str(static_root)).resolve() / "staticfiles.json")
        if not Path(manifest_path).exists():
            with self._lock:
                self._manifest_loaded = True
            return ""
        try:
            with Path(manifest_path).open(encoding="utf-8") as f:
                raw = f.read()
            data = fast_json_loads(raw)
        except (OSError, ValueError, RuntimeError, UnicodeDecodeError) as exc:
            _logger.warning(
                "Failed to read manifest {path}: {err}",
                path=manifest_path,
                err=exc,
            )
            with self._lock:
                self._manifest_loaded = True
            return ""
        manifest_hash = data.get("hash", "") if isinstance(data, dict) else ""
        with self._lock:
            self._manifest_hash = manifest_hash
            self._manifest_loaded = True
            self._cached_version = ""
        return manifest_hash

    def register_component(self, name: str, paths: list[str]) -> None:
        """Register files contributing to the app version hash.

        Use for non-static files that affect the user experience:
        templates, config files, etc.
        """
        with self._lock:
            self._component_paths[name] = list(paths)
            self._computed_hash = ""
            self._cached_version = ""

    def compute_from_components(self) -> str:
        """Compute a rolling hash from all registered component files.

        Sorts all paths, hashes each file's contents, then produces a
        single MD5 of the concatenated per-file hashes.
        """
        all_paths: list[str] = []
        for paths in self._component_paths.values():
            all_paths.extend(paths)
        all_paths.sort()

        if not all_paths:
            return ""

        hasher = hashlib.md5(usedforsecurity=False)
        for path in all_paths:
            if Path(path).is_file():
                with Path(path).open("rb") as f:
                    file_hash = hashlib.md5(f.read(), usedforsecurity=False).hexdigest()
                hasher.update(file_hash.encode("ascii"))
        computed = hasher.hexdigest()[:12]
        with self._lock:
            self._computed_hash = computed
            self._cached_version = ""
        return computed

    def invalidate(self) -> None:
        """Clear all cached version data, forcing recompute on next access."""
        with self._lock:
            self._explicit = ""
            self._manifest_hash = ""
            self._manifest_loaded = False
            self._computed_hash = ""
            self._cached_version = ""

    def _resolve(self) -> str:
        """Resolve the version through the priority chain.

        Called while holding ``_lock`` (RLock allows reentrant acquisition
        when calling ``load_from_manifest`` / ``compute_from_components``).
        """
        # 1. Explicit setting
        explicit = self._explicit
        if not explicit:
            explicit = str(get_setting("APP_VERSION", ""))
        if explicit:
            self._explicit = explicit
            return explicit

        # 2. Manifest hash
        if not self._manifest_loaded:
            self.load_from_manifest()
        if self._manifest_hash:
            return self._manifest_hash

        # 3. Computed from components
        if self._component_paths and not self._computed_hash:
            self.compute_from_components()
        if self._computed_hash:
            return self._computed_hash

        # 4. Fallback
        return "unknown"


# ---------------------------------------------------------------------------
# Module-level singleton (follows _manifest_storage pattern)
# ---------------------------------------------------------------------------

_app_version: AppVersion | None = None
_singleton_lock = threading.Lock()


def get_app_version() -> AppVersion:
    """Get the global AppVersion instance (creates one if needed)."""
    global _app_version
    if _app_version is None:
        with _singleton_lock:
            if _app_version is None:
                _app_version = AppVersion()
    return _app_version


def set_app_version(v: AppVersion | None) -> None:
    """Set the global AppVersion instance."""
    global _app_version
    _app_version = v


# ---------------------------------------------------------------------------
# Version endpoint + cache bust endpoint
# ---------------------------------------------------------------------------


def _version_handler(_request: Request) -> Response:
    """GET /version — returns app version metadata as JSON.

    ``commit`` (APP_BUILD_COMMIT) and ``released_at`` (derived when the
    version is a canonical release stamp) carry the traceability that
    deliberately stays out of the version string itself.
    """
    av = get_app_version()
    payload: dict[str, object] = {
        "version": av.version,
        "source": av.source,
        "components": av.components,
    }
    commit = str(get_setting("APP_BUILD_COMMIT", ""))
    if commit:
        payload["commit"] = commit
    # Content-hash / legacy versions encode no instant to display.
    with contextlib.suppress(ValueError):
        payload["released_at"] = release_stamp_display(av.version)
    return Response.json(payload)


def _make_cache_bust_token() -> str:
    """Derive a cache-bust auth token from SECRET_KEY."""
    secret = str(get_setting("SECRET_KEY", ""))
    if not secret:
        return ""
    return hmac_sha256_hex_truncated(secret.encode("utf-8"), b"cache-bust", 32)


def _cache_bust_handler(request: Request) -> Response:
    """POST /cache/bust — manual cache invalidation endpoint.

    Requires ``Authorization: Bearer <token>`` where the token is
    derived from ``SECRET_KEY`` via ``hmac_sha256_hex_truncated``.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return Response.error(401, "Missing Bearer token")
    token = auth_header[7:]
    secret = str(get_setting("SECRET_KEY", ""))
    if not secret or not hmac_sha256_verify_truncated(
        secret.encode("utf-8"), b"cache-bust", token, 32
    ):
        return Response.error(403, "Invalid token")

    # Clear static file middleware cache
    storage = get_manifest_storage()
    if storage is not None:
        storage._manifest_loaded = False
        storage._manifest = {}
        storage.load_manifest()

    # Invalidate app version
    av = get_app_version()
    av.invalidate()

    return Response.json(
        {
            "status": "ok",
            "version": av.version,
        }
    )


def mount_version_endpoints(
    app: HyperApp, version_path: str = "/version", bust_path: str = "/cache/bust"
) -> None:
    """Mount version + cache bust endpoints on the app.

    Called by ``HyperApp.mount_version()``.
    """
    if get_setting("VERSION_ENDPOINT", True):
        app.router.add("GET", version_path, _version_handler)
    app.router.add("POST", bust_path, _cache_bust_handler)
