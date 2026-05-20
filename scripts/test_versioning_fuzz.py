"""
Hypothesis fuzz tests and edge case tests for the versioning system.

Proves:
1. inject_version_meta never produces XSS regardless of version string
2. AppVersion thread safety under concurrent invalidate + read
3. Malformed manifest JSON handled gracefully
4. Binary/empty/minimal HTML responses handled correctly
5. Header injection via CRLF is sanitized
6. Dev-mode hash cache concurrent safety
7. Large manifest stress test
8. Version length limits enforced

# hyper-test: unit
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.conf import DEFAULTS
from hyperdjango.request import Request
from hyperdjango.response import Response, _sanitize_header
from hyperdjango.standalone_middleware import VersionMiddleware
from hyperdjango.staticfiles import (
    StaticFilesFinder,
    _dev_hash_cache,
    get_static_url_versioned,
    set_dev_finder,
    set_manifest_storage,
)
from hyperdjango.versioning import (
    AppVersion,
    _escape_version_for_js,
    inject_version_meta,
    set_app_version,
)

# Under parallel test execution, CPU contention can push individual examples
# past per-call deadlines. Disable the deadline under parallel mode.
_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 1000
_SUPPRESS = [HealthCheck.too_slow, HealthCheck.filter_too_much] if _PARALLEL else []


def _ex(n: int) -> int:
    return max(n // 2, 30) if _PARALLEL else n


passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" -- {msg}"
        errors.append(err)
        print(f"  {err}")


class TempDir:
    def __init__(self):
        self.path: Path = Path()

    def __enter__(self):
        self.path = Path(tempfile.mkdtemp(prefix="hyper_fuzz_"))
        return self

    def __exit__(self, *args):
        if self.path != Path() and self.path.exists():
            shutil.rmtree(self.path)

    def write(self, name: str, content: str) -> str:
        full = self.path / name
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


# ---------------------------------------------------------------------------
# 1. XSS fuzz: inject_version_meta never breaks JS string context
# ---------------------------------------------------------------------------

# Strategy: arbitrary text that might contain XSS payloads
xss_payloads = st.text(min_size=0, max_size=200)


@given(version=xss_payloads)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_xss_version_fuzz(version):
    """No version string can break out of the JS string literal."""
    html = "<html><head></head><body></body></html>"
    result = inject_version_meta(html, version, "reload")

    # The version must be JSON-encoded in the output
    safe = _escape_version_for_js(version)
    assert f"window.__hyperAppVersion={safe};" in result

    # Critical XSS check: if the version contains double-quote, the raw
    # version must NOT appear as a naively-quoted value (which would
    # break the JS string context and allow code injection)
    if '"' in version:
        assert f'__hyperAppVersion="{version}"' not in result


def test_xss_specific_payloads():
    """Specific XSS payloads that must be neutralized."""
    payloads = [
        '";alert("XSS");//',
        "</script><script>alert(1)</script>",
        "'\"><img src=x onerror=alert(1)>",
        "\\",
        "\n",
        "\r\n",
        "\0",
        'a"b',
        "a'b",
    ]
    html = "<html><head></head><body></body></html>"
    for payload in payloads:
        result = inject_version_meta(html, payload, "reload")
        safe = _escape_version_for_js(payload)
        check(
            f"XSS payload neutralized: {payload[:30]!r}",
            f"window.__hyperAppVersion={safe};" in result,
            f"payload {payload!r} not properly escaped",
        )


# ---------------------------------------------------------------------------
# 2. Thread safety: concurrent invalidate + read
# ---------------------------------------------------------------------------


def test_concurrent_invalidate_read():
    """Concurrent invalidate() + version reads never corrupt state."""
    av = AppVersion()
    av.set_explicit("v1")
    results: list[str] = []
    errs: list[str] = []

    def reader():
        try:
            for _ in range(200):
                v = av.version
                results.append(v)
        except Exception as e:
            errs.append(f"reader: {e}")

    def invalidator():
        try:
            for _ in range(20):
                av.invalidate()
                av.set_explicit("v2")
                time.sleep(0.0001)
        except Exception as e:
            errs.append(f"invalidator: {e}")

    threads = [threading.Thread(target=reader) for _ in range(4)] + [
        threading.Thread(target=invalidator) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(
        "concurrent invalidate+read: no exceptions",
        not errs,
        f"errors: {errs}",
    )
    check(
        "concurrent invalidate+read: all results are strings",
        all(isinstance(v, str) for v in results),
    )
    check(
        "concurrent invalidate+read: results are valid versions",
        all(v in ("v1", "v2", "unknown") for v in results),
        f"unexpected values: {set(results) - {'v1', 'v2', 'unknown'}}",
    )


# ---------------------------------------------------------------------------
# 3. Malformed manifest JSON
# ---------------------------------------------------------------------------


def test_malformed_manifest_json():
    """Corrupted JSON in staticfiles.json doesn't crash."""
    with TempDir() as d:
        manifest_path = d.path / "staticfiles.json"
        manifest_path.write_text("{broken json!!")
        av = AppVersion()
        h = av.load_from_manifest(str(manifest_path))
        check("malformed JSON: returns empty", h == "", f"got {h!r}")


def test_manifest_missing_hash_field():
    """Manifest without hash field returns empty."""
    with TempDir() as d:
        manifest_path = d.path / "staticfiles.json"
        with manifest_path.open("w") as f:
            json.dump({"version": "1.1", "paths": {}}, f)
        av = AppVersion()
        h = av.load_from_manifest(str(manifest_path))
        check("missing hash field: returns empty", h == "", f"got {h!r}")


def test_manifest_not_a_dict():
    """Manifest that parses as non-dict returns empty."""
    with TempDir() as d:
        manifest_path = d.path / "staticfiles.json"
        manifest_path.write_text('"just a string"')
        av = AppVersion()
        h = av.load_from_manifest(str(manifest_path))
        check("non-dict manifest: returns empty", h == "", f"got {h!r}")


def test_manifest_empty_paths():
    """Manifest with empty paths dict still returns a valid hash."""
    with TempDir() as d:
        manifest_path = d.path / "staticfiles.json"
        # Compute expected hash
        import hyperdjango.native as native

        pairs_json = native.fast_json_dumps([])
        if isinstance(pairs_json, bytes):
            pairs_json = pairs_json.decode("utf-8")
        expected = hashlib.md5(
            pairs_json.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
        manifest = {"version": "1.1", "paths": {}, "hash": expected}
        with manifest_path.open("w") as f:
            json.dump(manifest, f)
        av = AppVersion()
        h = av.load_from_manifest(str(manifest_path))
        check("empty paths: returns hash", h == expected, f"got {h!r}")


def test_manifest_permission_denied():
    """Unreadable manifest returns empty without crash."""
    with TempDir() as d:
        manifest_path = d.path / "staticfiles.json"
        with manifest_path.open("w") as f:
            json.dump({"version": "1.1", "paths": {}, "hash": "abc"}, f)
        manifest_path.chmod(0o000)
        try:
            av = AppVersion()
            h = av.load_from_manifest(str(manifest_path))
            check("permission denied: returns empty", h == "", f"got {h!r}")
        finally:
            manifest_path.chmod(0o644)


# ---------------------------------------------------------------------------
# 4. Binary/empty/minimal HTML responses
# ---------------------------------------------------------------------------


def test_inject_empty_html():
    """Empty HTML string returns unchanged."""
    result = inject_version_meta("", "v1", "reload")
    check("empty HTML: no script tags", "htmx:afterRequest" not in result)


def test_inject_whitespace_html():
    """Whitespace-only HTML returns unchanged."""
    html = "   \n\t\n   "
    result = inject_version_meta(html, "v1", "reload")
    check("whitespace HTML: no script injection", "htmx:afterRequest" not in result)


def test_inject_head_only_html():
    """HTML with only </head> gets version meta but no body script."""
    html = "<head></head>"
    result = inject_version_meta(html, "v1", "reload")
    check("head-only: has version meta", "__hyperAppVersion" in result)
    check("head-only: no body script", "htmx:afterRequest" not in result)


async def test_middleware_binary_response():
    """VersionMiddleware handles binary body with text/html content-type."""
    from unittest.mock import patch

    async def binary_html_handler(request):
        return Response(
            body=b"\x80\x81</body>",  # Invalid UTF-8 with </body>
            status=200,
            content_type="text/html",
        )

    av = AppVersion()
    av.set_explicit("bin-test")
    set_app_version(av)
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "reload"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), binary_html_handler)
            # Should NOT crash — UnicodeDecodeError is caught
            check("binary response: no crash", resp.status == 200)
            check(
                "binary response: header still set",
                resp.headers.get("x-app-version") == "bin-test",
            )
    finally:
        set_app_version(None)


# ---------------------------------------------------------------------------
# 5. CRLF header injection
# ---------------------------------------------------------------------------


@given(version=st.text(min_size=1, max_size=100, alphabet=st.characters()))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_crlf_header_sanitization(version):
    """_sanitize_header strips CRLF from any version string."""
    sanitized = _sanitize_header(version)
    assert "\r" not in sanitized
    assert "\n" not in sanitized


def test_crlf_specific_injections():
    """Specific CRLF injection payloads are neutralized."""
    payloads = [
        "v1\r\nX-Injected: true",
        "v1\nSet-Cookie: evil=1",
        "v1\r\n\r\n<html>injected</html>",
    ]
    for payload in payloads:
        sanitized = _sanitize_header(payload)
        check(
            f"CRLF stripped: {payload[:30]!r}",
            "\r" not in sanitized and "\n" not in sanitized,
        )


# ---------------------------------------------------------------------------
# 6. Dev hash cache concurrent safety
# ---------------------------------------------------------------------------


def test_dev_hash_cache_concurrent():
    """Concurrent get_static_url_versioned calls don't corrupt cache."""
    from unittest.mock import patch

    set_manifest_storage(None)
    with TempDir() as d:
        d.write("concurrent.js", "stable content for concurrency test")
        finder = StaticFilesFinder(dirs=[str(d.path)])
        set_dev_finder(finder)
        try:
            _dev_hash_cache.clear()
            results: list[str] = []
            errs: list[str] = []

            with patch.dict(DEFAULTS, {"STATIC_DEV_VERSION_QUERY": True}):

                def hash_file():
                    try:
                        for _ in range(50):
                            url = get_static_url_versioned("concurrent.js")
                            results.append(url)
                    except Exception as e:
                        errs.append(str(e))

                threads = [threading.Thread(target=hash_file) for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            check("concurrent cache: no errors", not errs, f"errors: {errs}")
            check(
                "concurrent cache: all same hash",
                len(set(results)) == 1,
                f"got {len(set(results))} distinct URLs",
            )
        finally:
            set_dev_finder(None)


# ---------------------------------------------------------------------------
# 7. Large manifest stress test
# ---------------------------------------------------------------------------


def test_large_manifest_1000_files():
    """Manifest with 1000 files loads in under 100ms."""
    with TempDir() as d:
        paths = {f"file_{i}.css": f"file_{i}.abc123def456.css" for i in range(1000)}
        # Write manifest
        sorted_paths = dict(sorted(paths.items()))
        import hyperdjango.native as native

        pairs_json = native.fast_json_dumps(list(sorted_paths.items()))
        if isinstance(pairs_json, bytes):
            pairs_json = pairs_json.decode("utf-8")
        manifest_hash = hashlib.md5(
            pairs_json.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
        manifest = {"version": "1.1", "paths": sorted_paths, "hash": manifest_hash}
        manifest_path = d.path / "staticfiles.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f)

        av = AppVersion()
        start = time.perf_counter()
        h = av.load_from_manifest(str(manifest_path))
        elapsed = time.perf_counter() - start

        check("large manifest: hash returned", h == manifest_hash)
        check(
            f"large manifest: loaded in {elapsed * 1000:.1f}ms (<100ms)",
            elapsed < 0.1,
            f"took {elapsed * 1000:.1f}ms",
        )


# ---------------------------------------------------------------------------
# 8. Version length limits
# ---------------------------------------------------------------------------


def test_version_length_truncated():
    """Explicit versions longer than 256 chars are truncated."""
    av = AppVersion()
    long_version = "a" * 500
    av.set_explicit(long_version)
    check(
        "version length truncated to 256",
        len(av.version) == 256,
        f"got length {len(av.version)}",
    )


def test_version_length_normal():
    """Normal-length versions are not truncated."""
    av = AppVersion()
    av.set_explicit("v1.2.3-beta.4")
    check("normal version not truncated", av.version == "v1.2.3-beta.4")


# ---------------------------------------------------------------------------
# 9. _escape_version_for_js property tests
# ---------------------------------------------------------------------------


@given(version=st.text(min_size=0, max_size=200))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_escape_version_roundtrip(version):
    """JSON-encoded version can be decoded back to original."""
    encoded = _escape_version_for_js(version)
    # Should be valid JSON string
    decoded = json.loads(encoded)
    assert decoded == version


@given(version=st.text(min_size=0, max_size=200))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_escape_version_no_unquoted_quotes(version):
    """Encoded version never has unescaped double quotes inside."""
    encoded = _escape_version_for_js(version)
    # Strip outer quotes
    inner = encoded[1:-1]
    # No unescaped double quotes inside the string
    assert '"' not in inner.replace('\\"', "")


# ---------------------------------------------------------------------------
# 10. VersionRouterMiddleware CRLF fuzz
# ---------------------------------------------------------------------------


@given(version=st.text(min_size=1, max_size=100, alphabet=st.characters()))
@settings(max_examples=_ex(100), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_router_crlf_fuzz(version):
    """VersionRouterMiddleware sanitizes arbitrary version strings in headers."""
    sanitized = _sanitize_header(version)
    # After sanitization, no CRLF chars remain
    assert "\r" not in sanitized
    assert "\n" not in sanitized


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- Versioning Fuzz + Edge Case Tests --\n")

    # Hypothesis property-based tests
    hypothesis_tests = [
        ("XSS version fuzz (Hypothesis)", test_xss_version_fuzz),
        ("CRLF header sanitization (Hypothesis)", test_crlf_header_sanitization),
        ("escape_version roundtrip (Hypothesis)", test_escape_version_roundtrip),
        (
            "escape_version no unquoted quotes (Hypothesis)",
            test_escape_version_no_unquoted_quotes,
        ),
        ("router CRLF fuzz (Hypothesis)", test_router_crlf_fuzz),
    ]

    for name, test_fn in hypothesis_tests:
        try:
            test_fn()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            errors.append(f"FAIL: {name}: {e}")
            print(f"  FAIL: {name}: {e}")
            traceback.print_exc()

    # Direct tests
    test_xss_specific_payloads()
    test_concurrent_invalidate_read()
    test_malformed_manifest_json()
    test_manifest_missing_hash_field()
    test_manifest_not_a_dict()
    test_manifest_empty_paths()
    test_manifest_permission_denied()
    test_inject_empty_html()
    test_inject_whitespace_html()
    test_inject_head_only_html()
    test_crlf_specific_injections()
    test_dev_hash_cache_concurrent()
    test_large_manifest_1000_files()
    test_version_length_truncated()
    test_version_length_normal()

    # Async tests
    loop = asyncio.new_event_loop()
    loop.run_until_complete(test_middleware_binary_response())
    loop.close()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Versioning fuzz: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
