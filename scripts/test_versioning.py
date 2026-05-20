"""
Tests for the versioning system.

Tests AppVersion core, dev-mode ?v=hash URLs, version mismatch script
injection, version endpoint, cache bust endpoint, and settings.

# hyper-test: unit

Usage:
    uv run hyper-test versioning
"""

import asyncio
import hashlib
import inspect
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

from hyperdjango.conf import DEFAULTS, SETTING_DEFINITIONS
from hyperdjango.native import fast_json_dumps
from hyperdjango.request import Request
from hyperdjango.staticfiles import (
    ManifestStaticFilesStorage,
    StaticFilesFinder,
    get_static_url_versioned,
    set_dev_finder,
    set_manifest_storage,
)
from hyperdjango.versioning import (
    VERSION_ACTIONS,
    AppVersion,
    _cache_bust_handler,
    _make_cache_bust_token,
    _version_handler,
    get_app_version,
    get_client_script,
    inject_version_meta,
    set_app_version,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS: dict[str, int | list[tuple[str, str]]] = {
    "passed": 0,
    "failed": 0,
    "errors": [],
}


def test(name: str):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  PASS: {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  FAIL: {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


class TempDir:
    """Context manager for a temp directory with helper write method."""

    def __init__(self):
        self.path = ""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="hyper_version_test_")
        return self

    def __exit__(self, *args):
        if self.path and Path(self.path).exists():
            shutil.rmtree(self.path)

    def write(self, name: str, content: str) -> str:
        """Write a file and return its absolute path."""
        full = Path(self.path) / name
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return str(full)


def make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
) -> Request:
    return Request(
        method=method,
        path=path,
        headers=headers or {},
        query_string="",
        body=b"",
    )


def _write_manifest(root: str, paths: dict[str, str]) -> str:
    """Write a staticfiles.json manifest and return its path."""
    sorted_paths = dict(sorted(paths.items()))
    pairs_json = fast_json_dumps(list(sorted_paths.items()))
    if isinstance(pairs_json, bytes):
        pairs_json = pairs_json.decode("utf-8")
    manifest_hash = hashlib.md5(
        pairs_json.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    manifest = {
        "version": "1.1",
        "paths": sorted_paths,
        "hash": manifest_hash,
    }
    manifest_path = Path(root) / "staticfiles.json"
    import json

    manifest_path.write_text(json.dumps(manifest))
    return str(manifest_path)


# ---------------------------------------------------------------------------
# AppVersion core tests
# ---------------------------------------------------------------------------


@test("AppVersion: explicit version takes priority")
def test_explicit_priority():
    av = AppVersion()
    av.set_explicit("1.0.0")
    assert av.version == "1.0.0"
    assert av.source == "explicit"


@test("AppVersion: explicit via setting")
def test_explicit_via_setting():
    av = AppVersion()
    with patch.dict(DEFAULTS, {"APP_VERSION": "v2.3.4"}):
        av.invalidate()
        assert av.version == "v2.3.4"
        assert av.source == "explicit"


@test("AppVersion: manifest hash loaded")
def test_manifest_hash():
    with TempDir() as d:
        paths = {"css/a.css": "css/a.abc123def456.css"}
        _write_manifest(d.path, paths)
        av = AppVersion()
        h = av.load_from_manifest(str(Path(d.path) / "staticfiles.json"))
        assert h != ""
        assert len(h) == 12
        assert av.version == h
        assert av.source == "manifest"


@test("AppVersion: manifest hash empty when no file")
def test_manifest_no_file():
    av = AppVersion()
    h = av.load_from_manifest("/nonexistent/staticfiles.json")
    assert h == ""


@test("AppVersion: computed from components")
def test_computed_components():
    with TempDir() as d:
        p1 = d.write("a.html", "hello")
        p2 = d.write("b.html", "world")
        av = AppVersion()
        av.register_component("templates", [p1, p2])
        computed = av.compute_from_components()
        assert len(computed) == 12
        assert av.version == computed
        assert av.source == "computed"


@test("AppVersion: register_component multiple groups")
def test_multiple_components():
    with TempDir() as d:
        p1 = d.write("t1.html", "template")
        p2 = d.write("config.json", '{"key": "val"}')
        av = AppVersion()
        av.register_component("templates", [p1])
        av.register_component("config", [p2])
        assert av.components == {"templates": 1, "config": 1}
        computed = av.compute_from_components()
        assert len(computed) == 12


@test("AppVersion: invalidate clears all state")
def test_invalidate():
    av = AppVersion()
    av.set_explicit("1.0.0")
    assert av.version == "1.0.0"
    av.invalidate()
    # After invalidate with no manifest/components, should be "unknown"
    with patch.dict(DEFAULTS, {"APP_VERSION": ""}):
        assert av.version == "unknown"
        assert av.source == "unknown"


@test("AppVersion: resolution order — explicit > manifest > computed")
def test_resolution_order():
    with TempDir() as d:
        p = d.write("f.html", "content")
        paths = {"a.css": "a.hash.css"}
        _write_manifest(d.path, paths)
        av = AppVersion()

        # Start with computed
        av.register_component("test", [p])
        av.compute_from_components()
        computed = av.version
        assert av.source == "computed"

        # Manifest overrides computed
        av.load_from_manifest(str(Path(d.path) / "staticfiles.json"))
        manifest_v = av.version
        assert av.source == "manifest"
        assert manifest_v != computed

        # Explicit overrides manifest
        av.set_explicit("explicit-v1")
        assert av.version == "explicit-v1"
        assert av.source == "explicit"


@test("AppVersion: fallback to unknown")
def test_fallback_unknown():
    av = AppVersion()
    with patch.dict(DEFAULTS, {"APP_VERSION": "", "STATIC_ROOT": ""}):
        assert av.version == "unknown"
        assert av.source == "unknown"


@test("AppVersion: thread safety — concurrent reads")
def test_thread_safety():
    av = AppVersion()
    av.set_explicit("safe-version")
    results: list[str] = []
    errors: list[str] = []

    def reader():
        try:
            for _ in range(100):
                v = av.version
                results.append(v)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert all(v == "safe-version" for v in results)


@test("AppVersion: computed hash changes when file content changes")
def test_computed_hash_changes():
    with TempDir() as d:
        p = d.write("app.js", "version1")
        av = AppVersion()
        av.register_component("js", [p])
        h1 = av.compute_from_components()

        d.write("app.js", "version2")
        av._computed_hash = ""
        av._cached_version = ""
        h2 = av.compute_from_components()
        assert h1 != h2


@test("AppVersion: computed hash stable for same content")
def test_computed_hash_stable():
    with TempDir() as d:
        p = d.write("app.js", "stable")
        av = AppVersion()
        av.register_component("js", [p])
        h1 = av.compute_from_components()
        av._computed_hash = ""
        av._cached_version = ""
        h2 = av.compute_from_components()
        assert h1 == h2


@test("AppVersion: computed skips nonexistent files")
def test_computed_skip_nonexistent():
    with TempDir() as d:
        p = d.write("exists.js", "data")
        av = AppVersion()
        av.register_component("mixed", [p, "/nonexistent/file.js"])
        h = av.compute_from_components()
        assert len(h) == 12


# ---------------------------------------------------------------------------
# inject_version_meta tests
# ---------------------------------------------------------------------------


@test("inject_version_meta: injects window.__hyperAppVersion before </head>")
def test_inject_head():
    html = "<html><head><title>Test</title></head><body>Hi</body></html>"
    result = inject_version_meta(html, "abc123", "reload")
    assert 'window.__hyperAppVersion="abc123"' in result
    assert result.index("__hyperAppVersion") < result.index("</head>")


@test("inject_version_meta: injects mismatch script before </body>")
def test_inject_body():
    html = "<html><head></head><body>Content</body></html>"
    result = inject_version_meta(html, "v1", "reload")
    assert "htmx:afterRequest" in result
    assert "location.reload()" in result
    assert result.index("htmx:afterRequest") < result.index("</body>")


@test("inject_version_meta: warn action bakes warn as the fallback action")
def test_inject_warn():
    html = "<html><head></head><body></body></html>"
    result = inject_version_meta(html, "v1", "warn")
    assert "console.warn" in result
    # The baked action is only the FALLBACK — the script still carries every
    # handler so a server-advertised X-App-Version-Action can override it.
    assert 'HYPER_ACTION="warn"' in result


@test("inject_version_meta: ignore action injects no script")
def test_inject_ignore():
    html = "<html><head></head><body></body></html>"
    result = inject_version_meta(html, "v1", "ignore")
    assert "htmx:afterRequest" not in result
    # But version meta IS still injected in head
    assert "__hyperAppVersion" in result


@test("inject_version_meta: no </head> tag — version in body area")
def test_inject_no_head():
    html = "<body>Simple</body>"
    result = inject_version_meta(html, "v1", "reload")
    # Script still injected before </body> even without </head>
    assert "htmx:afterRequest" in result


@test("inject_version_meta: no </body> tag — no script injection")
def test_inject_no_body():
    html = "<html><head></head>No body tag"
    result = inject_version_meta(html, "v1", "reload")
    assert "htmx:afterRequest" not in result


@test("inject_version_meta: version with special chars is JSON-escaped")
def test_inject_special_chars():
    html = "<html><head></head><body></body></html>"
    result = inject_version_meta(html, 'a"b<c>d', "reload")
    # Version is JSON-encoded to prevent XSS — quotes are escaped
    assert r"a\"b<c>d" in result
    # Raw unescaped double-quote must NOT appear in the JS string context
    assert 'window.__hyperAppVersion="a"' not in result


@test("get_client_script: every action is buildable and cached")
def test_client_script_actions():
    for action in VERSION_ACTIONS:
        s1 = get_client_script(action, True)
        s2 = get_client_script(action, True)
        # Built once per (action, broadcast), never per request.
        assert s1 is s2
        assert s1.action == action


@test("get_client_script: unknown action falls back to prompt")
def test_client_script_unknown_action():
    s = get_client_script("nonsense", True)
    assert s.action == "prompt"


@test("get_client_script: mismatch script carries all four handlers")
def test_client_script_all_handlers():
    s = get_client_script("warn", True)
    assert "location.reload()" in s.body
    assert "console.warn" in s.body
    assert "A new version is available." in s.body
    assert "X-App-Version-Action" in s.body


@test("get_client_script: ignore bakes no mismatch machinery")
def test_client_script_ignore():
    s = get_client_script("ignore", True)
    assert "htmx:afterRequest" not in s.body
    assert "console.warn" not in s.body
    # …but the cohort broadcast is a separate switch and still ships.
    assert "htmx:configRequest" in s.body


@test("get_client_script: ignore + no broadcast emits no body script")
def test_client_script_ignore_no_broadcast():
    s = get_client_script("ignore", False)
    assert s.body == ""


@test("get_client_script: broadcast gates the client header and cookie")
def test_client_script_broadcast_gate():
    on = get_client_script("prompt", True)
    off = get_client_script("prompt", False)
    assert "hyper_client_version=" in on.head
    assert "X-Client-Version" in on.head
    assert "htmx:configRequest" in on.body
    assert "hyper_client_version=" not in off.head
    assert "X-Client-Version" not in off.head
    assert "htmx:configRequest" not in off.body


@test("get_client_script: head exposes the window.hyperVersion API")
def test_client_script_head_api():
    s = get_client_script("prompt", True)
    for member in ("window.hyperVersion", "headers:", "reload:", "onMismatch:"):
        assert member in s.head, member


@test("get_client_script: reload never reloads without a guard key")
def test_client_script_reload_guard():
    s = get_client_script("reload", True)
    # sessionStorage guard so a still-stale client degrades to the banner
    # instead of reload-looping.
    assert "hyper_reloaded_for" in s.body
    assert "hyper_prompted_for" in s.body


# ---------------------------------------------------------------------------
# Dev-mode versioned URL tests
# ---------------------------------------------------------------------------


@test("get_static_url_versioned: with manifest delegates to get_static_url")
def test_versioned_with_manifest():
    with TempDir() as d:
        d.write("css/styles.css", "body { color: red; }")
        storage = ManifestStaticFilesStorage(
            static_dirs=[d.path],
            static_root=str(Path(d.path) / "collected"),
        )
        storage.collectstatic()
        set_manifest_storage(storage)
        try:
            url = get_static_url_versioned("css/styles.css")
            # Should return hashed filename (no ?v= param)
            assert "?v=" not in url
            assert ".css" in url
        finally:
            set_manifest_storage(None)


@test("get_static_url_versioned: dev mode appends ?v=hash")
def test_versioned_dev_mode():
    set_manifest_storage(None)
    with TempDir() as d:
        d.write("app.js", "console.log('hello');")
        finder = StaticFilesFinder(dirs=[d.path])
        set_dev_finder(finder)
        try:
            with patch.dict(DEFAULTS, {"STATIC_DEV_VERSION_QUERY": True}):
                url = get_static_url_versioned("app.js")
                assert "?v=" in url
                assert url.startswith("/static/app.js?v=")
                # Hash is 12 chars
                v_param = url.split("?v=")[1]
                assert len(v_param) == 12
        finally:
            set_dev_finder(None)


@test("get_static_url_versioned: dev mode hash changes with content")
def test_versioned_dev_hash_changes():
    set_manifest_storage(None)
    with TempDir() as d:
        d.write("app.js", "version1")
        finder = StaticFilesFinder(dirs=[d.path])
        set_dev_finder(finder)
        try:
            with patch.dict(DEFAULTS, {"STATIC_DEV_VERSION_QUERY": True}):
                url1 = get_static_url_versioned("app.js")

                # Clear the cache by writing new content
                import time

                time.sleep(0.01)  # Ensure mtime changes
                d.write("app.js", "version2")

                # Clear internal cache
                from hyperdjango.staticfiles import _dev_hash_cache

                _dev_hash_cache.clear()

                url2 = get_static_url_versioned("app.js")
                h1 = url1.split("?v=")[1]
                h2 = url2.split("?v=")[1]
                assert h1 != h2, f"Hashes should differ: {h1} vs {h2}"
        finally:
            set_dev_finder(None)


@test("get_static_url_versioned: file not found returns plain URL")
def test_versioned_file_not_found():
    set_manifest_storage(None)
    with TempDir() as d:
        finder = StaticFilesFinder(dirs=[d.path])
        set_dev_finder(finder)
        try:
            url = get_static_url_versioned("nonexistent.js")
            assert url == "/static/nonexistent.js"
            assert "?v=" not in url
        finally:
            set_dev_finder(None)


@test("get_static_url_versioned: setting disabled returns plain URL")
def test_versioned_setting_disabled():
    set_manifest_storage(None)
    with TempDir() as d:
        d.write("app.js", "data")
        finder = StaticFilesFinder(dirs=[d.path])
        set_dev_finder(finder)
        try:
            with patch.dict(DEFAULTS, {"STATIC_DEV_VERSION_QUERY": False}):
                url = get_static_url_versioned("app.js")
                assert url == "/static/app.js"
                assert "?v=" not in url
        finally:
            set_dev_finder(None)


@test("get_static_url_versioned: no finder returns plain URL")
def test_versioned_no_finder():
    set_manifest_storage(None)
    set_dev_finder(None)
    url = get_static_url_versioned("app.js")
    assert url == "/static/app.js"


@test("get_static_url_versioned: custom prefix")
def test_versioned_custom_prefix():
    set_manifest_storage(None)
    with TempDir() as d:
        d.write("app.js", "data")
        finder = StaticFilesFinder(dirs=[d.path])
        set_dev_finder(finder)
        try:
            with patch.dict(DEFAULTS, {"STATIC_DEV_VERSION_QUERY": True}):
                url = get_static_url_versioned("app.js", prefix="/assets/")
                assert url.startswith("/assets/app.js?v=")
        finally:
            set_dev_finder(None)


@test("get_static_url_versioned: mtime cache hit avoids rehash")
def test_versioned_mtime_cache():
    set_manifest_storage(None)
    with TempDir() as d:
        d.write("cached.js", "stable content")
        finder = StaticFilesFinder(dirs=[d.path])
        set_dev_finder(finder)
        try:
            from hyperdjango.staticfiles import _dev_hash_cache

            _dev_hash_cache.clear()
            with patch.dict(DEFAULTS, {"STATIC_DEV_VERSION_QUERY": True}):
                url1 = get_static_url_versioned("cached.js")
                url2 = get_static_url_versioned("cached.js")
                assert url1 == url2
                # Cache should have one entry
                assert len(_dev_hash_cache) >= 1
        finally:
            set_dev_finder(None)


# ---------------------------------------------------------------------------
# Version endpoint tests
# ---------------------------------------------------------------------------


@test("version endpoint: returns JSON with version")
def test_version_endpoint_json():
    av = AppVersion()
    av.set_explicit("test-v1")
    set_app_version(av)
    try:
        req = make_request("GET", "/version")
        resp = _version_handler(req)
        assert resp.status == 200
        import json

        data = json.loads(resp.body)
        assert data["version"] == "test-v1"
        assert data["source"] == "explicit"
        assert "components" in data
    finally:
        set_app_version(None)


@test("version endpoint: includes component counts")
def test_version_endpoint_components():
    with TempDir() as d:
        p1 = d.write("a.html", "content")
        p2 = d.write("b.html", "content")
        av = AppVersion()
        av.register_component("templates", [p1, p2])
        av.set_explicit("v1")
        set_app_version(av)
        try:
            req = make_request("GET", "/version")
            resp = _version_handler(req)
            import json

            data = json.loads(resp.body)
            assert data["components"]["templates"] == 2
        finally:
            set_app_version(None)


@test("version endpoint: source is manifest when loaded from manifest")
def test_version_endpoint_manifest_source():
    with TempDir() as d:
        _write_manifest(d.path, {"a.css": "a.hash.css"})
        av = AppVersion()
        av.load_from_manifest(str(Path(d.path) / "staticfiles.json"))
        set_app_version(av)
        try:
            req = make_request("GET", "/version")
            resp = _version_handler(req)
            import json

            data = json.loads(resp.body)
            assert data["source"] == "manifest"
        finally:
            set_app_version(None)


# ---------------------------------------------------------------------------
# Cache bust endpoint tests
# ---------------------------------------------------------------------------


@test("cache bust: requires authorization")
def test_cache_bust_no_auth():
    req = make_request("POST", "/cache/bust")
    resp = _cache_bust_handler(req)
    assert resp.status == 401


@test("cache bust: invalid token returns 403")
def test_cache_bust_invalid_token():
    req = make_request(
        "POST", "/cache/bust", headers={"authorization": "Bearer invalid-token"}
    )
    with patch.dict(DEFAULTS, {"SECRET_KEY": "test-secret"}):
        resp = _cache_bust_handler(req)
        assert resp.status == 403


@test("cache bust: valid token succeeds")
def test_cache_bust_valid_token():
    with patch.dict(DEFAULTS, {"SECRET_KEY": "test-secret", "STATIC_ROOT": ""}):
        token = _make_cache_bust_token()
        assert len(token) == 32
        av = AppVersion()
        av.set_explicit("old-version")
        set_app_version(av)
        try:
            req = make_request(
                "POST", "/cache/bust", headers={"authorization": f"Bearer {token}"}
            )
            resp = _cache_bust_handler(req)
            assert resp.status == 200
            import json

            data = json.loads(resp.body)
            assert data["status"] == "ok"
            assert "version" in data
        finally:
            set_app_version(None)


@test("cache bust: invalidates app version")
def test_cache_bust_invalidates():
    with patch.dict(
        DEFAULTS, {"SECRET_KEY": "test-secret", "STATIC_ROOT": "", "APP_VERSION": ""}
    ):
        token = _make_cache_bust_token()
        av = AppVersion()
        av.set_explicit("will-be-cleared")
        set_app_version(av)
        try:
            assert av.version == "will-be-cleared"
            req = make_request(
                "POST", "/cache/bust", headers={"authorization": f"Bearer {token}"}
            )
            resp = _cache_bust_handler(req)
            # After bust, explicit is cleared via invalidate()
            # The response body accesses .version which re-resolves
            import json

            data = json.loads(resp.body)
            # With no manifest, no components, no explicit — should be "unknown"
            assert data["version"] == "unknown"
            assert av._explicit == ""
        finally:
            set_app_version(None)


@test("_make_cache_bust_token: empty secret returns empty string")
def test_bust_token_empty_secret():
    with patch.dict(DEFAULTS, {"SECRET_KEY": ""}):
        assert _make_cache_bust_token() == ""


@test("_make_cache_bust_token: deterministic for same secret")
def test_bust_token_deterministic():
    with patch.dict(DEFAULTS, {"SECRET_KEY": "stable-key"}):
        t1 = _make_cache_bust_token()
        t2 = _make_cache_bust_token()
        assert t1 == t2
        assert len(t1) == 32


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------


_VERSIONING_SETTINGS = [
    "APP_VERSION",
    "APP_VERSION_HEADER",
    "APP_VERSION_MISMATCH",
    "APP_VERSION_CLIENT_BROADCAST",
    "VERSION_ENDPOINT",
    "STATIC_DEV_VERSION_QUERY",
]


@test("settings: all 6 versioning settings in DEFAULTS")
def test_settings_in_defaults():
    for name in _VERSIONING_SETTINGS:
        assert name in DEFAULTS, f"{name} not in DEFAULTS"


@test("settings: all 6 versioning settings in SETTING_DEFINITIONS")
def test_settings_in_definitions():
    for name in _VERSIONING_SETTINGS:
        assert name in SETTING_DEFINITIONS, f"{name} not in SETTING_DEFINITIONS"


@test("settings: APP_VERSION_MISMATCH has correct choices")
def test_mismatch_choices():
    defn = SETTING_DEFINITIONS["APP_VERSION_MISMATCH"]
    assert defn.choices == frozenset({"prompt", "reload", "warn", "ignore"})
    assert defn.choices == VERSION_ACTIONS


@test("settings: default values are correct")
def test_default_values():
    assert DEFAULTS["APP_VERSION"] == ""
    assert DEFAULTS["APP_VERSION_HEADER"] is True
    # State-safe default: a user-initiated reload prompt, never a forced reload.
    assert DEFAULTS["APP_VERSION_MISMATCH"] == "prompt"
    assert DEFAULTS["APP_VERSION_CLIENT_BROADCAST"] is True
    assert DEFAULTS["VERSION_ENDPOINT"] is True
    assert DEFAULTS["STATIC_DEV_VERSION_QUERY"] is True


@test("settings: types are correct")
def test_setting_types():
    assert SETTING_DEFINITIONS["APP_VERSION"].type is str
    assert SETTING_DEFINITIONS["APP_VERSION_HEADER"].type is bool
    assert SETTING_DEFINITIONS["APP_VERSION_MISMATCH"].type is str
    assert SETTING_DEFINITIONS["APP_VERSION_CLIENT_BROADCAST"].type is bool
    assert SETTING_DEFINITIONS["VERSION_ENDPOINT"].type is bool
    assert SETTING_DEFINITIONS["STATIC_DEV_VERSION_QUERY"].type is bool


# ---------------------------------------------------------------------------
# Module singleton tests
# ---------------------------------------------------------------------------


@test("get_app_version: creates instance on first call")
def test_get_creates_instance():
    set_app_version(None)
    av = get_app_version()
    assert isinstance(av, AppVersion)


@test("get_app_version: returns same instance on subsequent calls")
def test_get_returns_same():
    set_app_version(None)
    av1 = get_app_version()
    av2 = get_app_version()
    assert av1 is av2


@test("set_app_version: replaces the global instance")
def test_set_replaces():
    av_new = AppVersion()
    av_new.set_explicit("custom")
    set_app_version(av_new)
    assert get_app_version().version == "custom"
    set_app_version(None)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for _name, obj in sorted(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nVersioning Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


def run_tests():
    exit_code = asyncio.run(main())
    return exit_code


if __name__ == "__main__":
    sys.exit(run_tests())
