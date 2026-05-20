# hyper-test: unit
"""
Regression tests for the staticfiles fix-wave (round 14).

Covers three independent findings, all in hyperdjango/staticfiles.py:

  A3-R3  If-Range conditional Range (RFC 7233 §3.2). A resuming client sends
         `If-Range: <validator>` + `Range:`. If the validator still matches the
         current representation, the Range is honored (206); if the
         representation changed (stale validator), the Range MUST be ignored and
         the full 200 body returned — never a 206 slice of the new file.

  A3-S3  Dotfile / manifest disclosure. StaticFilesFinder.find() served any file
         on disk, including /static/.env, /static/.git/config and
         staticfiles.json. It must now deny any path segment starting with "."
         and the manifest filename, mirroring list_all()'s skip logic.

  A5-C2  gzip;q=0 refusal. The gzip branch used a plain substring test, so
         `Accept-Encoding: gzip;q=0` (explicit refusal) still got gzipped. It
         must parse token;q=value pairs and honor q=0 / identity / wildcard.

Usage:
    uv run hyper-test staticfiles_r14
"""

import asyncio
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

from hyperdjango.staticfiles import StaticFilesFinder, StaticFilesMiddleware

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


class FakeRequest:
    """Minimal request with lowercase header keys (as the middleware reads)."""

    def __init__(self, path="/static/app.css", method="GET", headers=None):
        self.method = method
        self.path = path
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _make_env():
    """Create a temp static dir with an app.css (>1KB), a .env, and a
    staticfiles.json, plus a middleware wired to it. Returns (mw, tmpdir)."""
    tmp = tempfile.mkdtemp(prefix="staticfiles_r14_")
    css = ("/* app */\n" + "a{color:red}\n" * 200).encode()  # well over 1KB
    with (Path(tmp) / "app.css").open("wb") as f:
        f.write(css)
    with (Path(tmp) / ".env").open("wb") as f:
        f.write(b"SECRET_KEY=leaked-do-not-serve\n")
    with (Path(tmp) / "staticfiles.json").open("wb") as f:
        f.write(b'{"version": "1.1", "paths": {}}')
    mw = StaticFilesMiddleware(static_dirs=[tmp], prefix="/static/")
    return mw, tmp, css


# ---------------------------------------------------------------------------
# A3-R3 — If-Range
# ---------------------------------------------------------------------------


@test("A3-R3 If-Range matching ETag → honors Range (206)")
def t_if_range_match_etag():
    mw, _tmp, css = _make_env()
    # First fetch to learn the current ETag.
    r0 = mw._serve_file("app.css", FakeRequest())
    assert r0.status == 200, r0.status
    etag = r0.headers["etag"]

    r = mw._serve_file(
        "app.css",
        FakeRequest(headers={"If-Range": etag, "Range": "bytes=0-9"}),
    )
    assert r.status == 206, f"expected 206, got {r.status}"
    assert r.body == css[:10], r.body
    assert r.headers["content-range"] == f"bytes 0-9/{len(css)}"


@test("A3-R3 If-Range stale ETag → ignores Range, full 200 body")
def t_if_range_stale_etag():
    mw, _tmp, css = _make_env()
    r = mw._serve_file(
        "app.css",
        FakeRequest(headers={"If-Range": '"deadbeefcafe"', "Range": "bytes=0-9"}),
    )
    assert r.status == 200, f"expected full 200, got {r.status}"
    assert r.body == css, "stale If-Range must yield the full body, not a slice"
    assert "content-range" not in r.headers


@test("A3-R3 If-Range matching Last-Modified date → honors Range (206)")
def t_if_range_match_date():
    mw, _tmp, css = _make_env()
    r0 = mw._serve_file("app.css", FakeRequest())
    last_mod = r0.headers["last-modified"]
    r = mw._serve_file(
        "app.css",
        FakeRequest(headers={"If-Range": last_mod, "Range": "bytes=0-4"}),
    )
    assert r.status == 206, f"expected 206, got {r.status}"
    assert r.body == css[:5], r.body


@test("A3-R3 If-Range weak etag → strong compare fails, full 200")
def t_if_range_weak_etag():
    mw, _tmp, css = _make_env()
    r0 = mw._serve_file("app.css", FakeRequest())
    weak = "W/" + r0.headers["etag"]
    r = mw._serve_file(
        "app.css",
        FakeRequest(headers={"If-Range": weak, "Range": "bytes=0-9"}),
    )
    assert r.status == 200, f"weak If-Range must not honor Range, got {r.status}"
    assert r.body == css


@test("A3-R3 Range without If-Range still works (206 unconditional)")
def t_range_no_if_range():
    mw, _tmp, css = _make_env()
    r = mw._serve_file("app.css", FakeRequest(headers={"Range": "bytes=0-9"}))
    assert r.status == 206, r.status
    assert r.body == css[:10]


# ---------------------------------------------------------------------------
# A3-S3 — dotfile / manifest disclosure
# ---------------------------------------------------------------------------


@test("A3-S3 .env dotfile is denied (finder.find → None)")
def t_dotfile_finder_denied():
    _mw, tmp, _css = _make_env()
    finder = StaticFilesFinder(dirs=[tmp])
    assert (Path(tmp) / ".env").exists(), "fixture missing"
    assert finder.find(".env") is None, "finder must not disclose .env"


@test("A3-S3 .env dotfile served → None (404 fall-through)")
def t_dotfile_serve_denied():
    mw, _tmp, _css = _make_env()
    r = mw._serve_file(".env", FakeRequest(path="/static/.env"))
    assert r is None, "dotfile serve must return None (fall through to 404)"


@test("A3-S3 dotdir segment (.git/config) is denied")
def t_dotdir_denied():
    _mw, tmp, _css = _make_env()
    (Path(tmp) / ".git").mkdir(parents=True, exist_ok=True)
    with (Path(tmp) / ".git" / "config").open("wb") as f:
        f.write(b"[core]\n")
    finder = StaticFilesFinder(dirs=[tmp])
    assert finder.find(".git/config") is None, "must not disclose .git/config"


@test("A3-S3 staticfiles.json manifest is denied")
def t_manifest_denied():
    _mw, tmp, _css = _make_env()
    finder = StaticFilesFinder(dirs=[tmp])
    assert (Path(tmp) / "staticfiles.json").exists()
    assert finder.find("staticfiles.json") is None, "manifest must not be served"


@test("A3-S3 legitimate file still found")
def t_legit_file_found():
    _mw, tmp, _css = _make_env()
    finder = StaticFilesFinder(dirs=[tmp])
    assert finder.find("app.css") is not None, "legit file must still resolve"


# ---------------------------------------------------------------------------
# A5-C2 — gzip q-value
# ---------------------------------------------------------------------------


@test("A5-C2 gzip;q=0 (explicit refusal) → uncompressed")
def t_gzip_q0_uncompressed():
    mw, _tmp, css = _make_env()
    r = mw._serve_file("app.css", FakeRequest(headers={"Accept-Encoding": "gzip;q=0"}))
    assert r.headers.get("content-encoding") != "gzip", "q=0 must not gzip"
    assert r.body == css


@test("A5-C2 gzip → compressed")
def t_gzip_compressed():
    mw, _tmp, css = _make_env()
    r = mw._serve_file("app.css", FakeRequest(headers={"Accept-Encoding": "gzip"}))
    assert r.headers.get("content-encoding") == "gzip", "gzip must compress"
    assert len(r.body) < len(css)


@test("A5-C2 gzip;q=1 → compressed")
def t_gzip_q1_compressed():
    mw, _tmp, _css = _make_env()
    r = mw._serve_file(
        "app.css", FakeRequest(headers={"Accept-Encoding": "gzip;q=1.0"})
    )
    assert r.headers.get("content-encoding") == "gzip"


@test("A5-C2 GZIP (uppercase token) → compressed")
def t_gzip_uppercase_compressed():
    mw, _tmp, _css = _make_env()
    r = mw._serve_file("app.css", FakeRequest(headers={"Accept-Encoding": "GZIP"}))
    assert r.headers.get("content-encoding") == "gzip", (
        "token match is case-insensitive"
    )


@test("A5-C2 br only (no gzip, no wildcard) → uncompressed")
def t_no_gzip_uncompressed():
    mw, _tmp, css = _make_env()
    r = mw._serve_file("app.css", FakeRequest(headers={"Accept-Encoding": "br"}))
    assert r.headers.get("content-encoding") != "gzip"
    assert r.body == css


@test("A5-C2 wildcard * with q>0 → compressed")
def t_wildcard_compressed():
    mw, _tmp, _css = _make_env()
    r = mw._serve_file("app.css", FakeRequest(headers={"Accept-Encoding": "*"}))
    assert r.headers.get("content-encoding") == "gzip", "* should permit gzip"


async def main():
    all_tests = [
        obj
        for _name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print("\n═══ Staticfiles Round-14 Fix-Wave Tests ═══")
    for t in all_tests:
        await t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)
    return RESULTS["failed"] == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
