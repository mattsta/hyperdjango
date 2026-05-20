"""
Tests for the static files system.

Tests StaticFilesFinder, StaticFilesMiddleware, ManifestStaticFilesStorage,
collectstatic, ETag/304, gzip, cache headers, CSS url rewriting, manifest,
path traversal prevention, and template helper integration.

Usage:
    uv run hyper-test static_files
"""

# hyper-test: unit

import asyncio
import gzip
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.staticfiles import (
    ManifestStaticFilesStorage,
    StaticFilesFinder,
    StaticFilesMiddleware,
    get_manifest_storage,
    get_static_url,
    set_manifest_storage,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

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
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Helper: create temp static directory with files
# ---------------------------------------------------------------------------


class TempStaticDir:
    """Context manager that creates a temp directory with static files."""

    def __init__(self):
        self.root = None

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="hyper_static_test_")
        return self

    def __exit__(self, *args):
        if self.root and Path(self.root).exists():
            shutil.rmtree(self.root)

    def write(self, rel_path, content):
        """Write a file to the temp directory."""
        full = Path(self.root) / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        full.write_bytes(content)
        return str(full)

    @property
    def path(self):
        return self.root


def make_request(method="GET", path="/", headers=None):
    """Create a minimal Request object for testing."""
    return Request(
        method=method,
        path=path,
        headers=headers or {},
        query_string="",
        body=b"",
    )


async def noop_next(request):
    """Fallback handler that returns 404."""
    return Response(body=b"Not Found", status=404)


# ---------------------------------------------------------------------------
# Tests: StaticFilesFinder
# ---------------------------------------------------------------------------


@test("Finder: find file in single directory")
def test_finder_single_dir():
    with TempStaticDir() as d:
        d.write("css/style.css", "body { color: red; }")
        finder = StaticFilesFinder(dirs=[d.path])

        result = finder.find("css/style.css")
        assert result is not None
        assert result.endswith("css/style.css")
        assert Path(result).is_file()


@test("Finder: file not found returns None")
def test_finder_not_found():
    with TempStaticDir() as d:
        finder = StaticFilesFinder(dirs=[d.path])
        assert finder.find("nonexistent.js") is None


@test("Finder: multiple directories, first wins")
def test_finder_multi_dir():
    with TempStaticDir() as d1, TempStaticDir() as d2:
        d1.write("shared.js", "// version 1")
        d2.write("shared.js", "// version 2")
        finder = StaticFilesFinder(dirs=[d1.path, d2.path])

        result = finder.find("shared.js")
        assert "version 1" in Path(result).read_text()


@test("Finder: prefixed directory")
def test_finder_prefix():
    with TempStaticDir() as d:
        d.write("jquery.js", "// jQuery")
        finder = StaticFilesFinder(dirs=[("vendor", d.path)])

        # Must use prefix
        assert finder.find("jquery.js") is None
        result = finder.find("vendor/jquery.js")
        assert result is not None


@test("Finder: path traversal prevented")
def test_finder_traversal():
    with TempStaticDir() as d:
        d.write("safe.txt", "safe")
        finder = StaticFilesFinder(dirs=[d.path])

        assert finder.find("../../../etc/passwd") is None
        assert finder.find("..%2f..%2fetc/passwd") is None


@test("Finder: list_all returns all files deduplicated")
def test_finder_list_all():
    with TempStaticDir() as d1, TempStaticDir() as d2:
        d1.write("a.css", "a")
        d1.write("b.js", "b")
        d2.write("b.js", "b2")  # duplicate — should be skipped
        d2.write("c.txt", "c")
        finder = StaticFilesFinder(dirs=[d1.path, d2.path])

        files = finder.list_all()
        names = [f[0] for f in files]
        assert "a.css" in names
        assert "b.js" in names
        assert "c.txt" in names
        assert len(names) == 3  # b.js not duplicated


@test("Finder: skips hidden files and directories")
def test_finder_skip_hidden():
    with TempStaticDir() as d:
        d.write("visible.css", "ok")
        d.write(".hidden", "skip")
        d.write(".git/config", "skip")
        finder = StaticFilesFinder(dirs=[d.path])

        files = finder.list_all()
        names = [f[0] for f in files]
        assert "visible.css" in names
        assert ".hidden" not in names
        assert ".git/config" not in names


@test("Finder: non-existent directory is silently skipped")
def test_finder_nonexistent_dir():
    finder = StaticFilesFinder(dirs=["/nonexistent/path/xyz"])
    assert finder.find("anything.css") is None
    assert finder.list_all() == []


# ---------------------------------------------------------------------------
# Tests: StaticFilesMiddleware — basic serving
# ---------------------------------------------------------------------------


@test("Middleware: serves static file with correct content-type")
async def test_mw_serve_basic():
    with TempStaticDir() as d:
        d.write("style.css", "body { color: blue; }")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/style.css")
        resp = await mw(req, noop_next)
        assert resp.status == 200
        assert b"color: blue" in resp.body
        assert "text/css" in resp.headers.get("content-type", "")


@test("Middleware: passes through non-static requests")
async def test_mw_passthrough():
    with TempStaticDir() as d:
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/api/users")
        resp = await mw(req, noop_next)
        assert resp.status == 404  # fell through to noop_next


@test("Middleware: returns 404 for missing static file via passthrough")
async def test_mw_missing_file():
    with TempStaticDir() as d:
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/nonexistent.js")
        resp = await mw(req, noop_next)
        assert resp.status == 404


@test("Middleware: prevents path traversal")
async def test_mw_path_traversal():
    with TempStaticDir() as d:
        d.write("secret.txt", "top secret")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/../../../etc/passwd")
        resp = await mw(req, noop_next)
        assert resp.status == 404


@test("Middleware: only serves GET and HEAD")
async def test_mw_method_filter():
    with TempStaticDir() as d:
        d.write("file.js", "// js")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(method="POST", path="/static/file.js")
        resp = await mw(req, noop_next)
        assert resp.status == 404  # POST falls through


@test("Middleware: HEAD returns headers without body")
async def test_mw_head_request():
    with TempStaticDir() as d:
        d.write("data.json", '{"key": "value"}')
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(method="HEAD", path="/static/data.json")
        resp = await mw(req, noop_next)
        assert resp.status == 200
        assert resp.body == b""
        assert int(resp.headers.get("content-length", 0)) > 0


# ---------------------------------------------------------------------------
# Tests: cache mtime validation, Range, oversized-skip (regressions)
# ---------------------------------------------------------------------------


@test("Middleware: cached file invalidated when it changes on disk")
async def test_mw_cache_mtime_revalidation():
    with TempStaticDir() as d:
        p = d.write("app.js", "// v1")
        mw = StaticFilesMiddleware(
            static_dirs=[d.path], prefix="/static/", use_cache=True
        )

        req = make_request(path="/static/app.js")
        resp1 = await mw(req, noop_next)
        assert resp1.body == b"// v1", resp1.body
        etag1 = resp1.headers.get("etag")

        # Overwrite the file with different content AND a newer mtime.
        import os as _os
        import time as _time

        Path(p).write_bytes(b"// v2 changed")
        future = _time.time() + 10
        _os.utime(p, (future, future))

        resp2 = await mw(make_request(path="/static/app.js"), noop_next)
        # Must serve the NEW bytes, not the stale cached copy.
        assert resp2.body == b"// v2 changed", resp2.body
        assert resp2.headers.get("etag") != etag1


@test("Middleware: Range request returns 206 with slice")
async def test_mw_range_206():
    with TempStaticDir() as d:
        d.write("movie.bin", b"0123456789")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/movie.bin", headers={"range": "bytes=2-5"})
        resp = await mw(req, noop_next)
        assert resp.status == 206, resp.status
        assert resp.body == b"2345", resp.body
        assert resp.headers.get("content-range") == "bytes 2-5/10"
        assert resp.headers.get("content-length") == "4"
        assert resp.headers.get("accept-ranges") == "bytes"


@test("Middleware: suffix Range (last N bytes) returns 206")
async def test_mw_range_suffix():
    with TempStaticDir() as d:
        d.write("movie.bin", b"0123456789")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        resp = await mw(
            make_request(path="/static/movie.bin", headers={"range": "bytes=-3"}),
            noop_next,
        )
        assert resp.status == 206, resp.status
        assert resp.body == b"789", resp.body
        assert resp.headers.get("content-range") == "bytes 7-9/10"


@test("Middleware: unsatisfiable Range returns 416")
async def test_mw_range_416():
    with TempStaticDir() as d:
        d.write("movie.bin", b"0123456789")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        resp = await mw(
            make_request(path="/static/movie.bin", headers={"range": "bytes=50-60"}),
            noop_next,
        )
        assert resp.status == 416, resp.status
        assert resp.headers.get("content-range") == "bytes */10"


@test("Middleware: non-Range request advertises accept-ranges")
async def test_mw_accept_ranges_header():
    with TempStaticDir() as d:
        d.write("file.txt", "hello")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")
        resp = await mw(make_request(path="/static/file.txt"), noop_next)
        assert resp.status == 200
        assert resp.headers.get("accept-ranges") == "bytes"


@test("Middleware: oversized file is not cached (and doesn't purge cache)")
async def test_mw_oversized_skips_cache():
    with TempStaticDir() as d:
        d.write("small.txt", b"x" * 100)
        d.write("huge.bin", b"y" * 5000)
        # Budget only fits the small file, not the huge one.
        mw = StaticFilesMiddleware(
            static_dirs=[d.path],
            prefix="/static/",
            use_cache=True,
            max_cache_bytes=1000,
            gzip_min_size=0,
        )

        # Prime the cache with the small file.
        await mw(make_request(path="/static/small.txt"), noop_next)
        assert "small.txt" in mw._file_cache

        # Serve the oversized file: it must be served but NOT cached, and it
        # must NOT evict the small file to make room for itself.
        resp = await mw(make_request(path="/static/huge.bin"), noop_next)
        assert resp.status == 200
        assert len(resp.body) == 5000
        assert "huge.bin" not in mw._file_cache
        assert "small.txt" in mw._file_cache


# ---------------------------------------------------------------------------
# Tests: ETag and conditional responses
# ---------------------------------------------------------------------------


@test("Middleware: sets ETag header")
async def test_mw_etag():
    with TempStaticDir() as d:
        content = "body { margin: 0; }"
        d.write("base.css", content)
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/base.css")
        resp = await mw(req, noop_next)
        assert resp.status == 200
        etag = resp.headers.get("etag")
        assert etag is not None
        assert etag.startswith('"') and etag.endswith('"')

        # Verify ETag is content-based
        expected = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:12]
        assert etag == f'"{expected}"'


@test("Middleware: If-None-Match returns 304")
async def test_mw_if_none_match():
    with TempStaticDir() as d:
        content = "h1 { font-size: 2em; }"
        d.write("heading.css", content)
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        # First request to get ETag
        req1 = make_request(path="/static/heading.css")
        resp1 = await mw(req1, noop_next)
        etag = resp1.headers["etag"]

        # Second request with If-None-Match
        req2 = make_request(path="/static/heading.css", headers={"if-none-match": etag})
        resp2 = await mw(req2, noop_next)
        assert resp2.status == 304
        assert resp2.body == b""


@test("Middleware: If-None-Match with wildcard * returns 304")
async def test_mw_if_none_match_wildcard():
    with TempStaticDir() as d:
        d.write("any.txt", "anything")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/any.txt", headers={"if-none-match": "*"})
        resp = await mw(req, noop_next)
        assert resp.status == 304


@test("Middleware: If-None-Match with wrong ETag returns 200")
async def test_mw_if_none_match_miss():
    with TempStaticDir() as d:
        d.write("fresh.txt", "fresh content")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(
            path="/static/fresh.txt", headers={"if-none-match": '"wrong-etag"'}
        )
        resp = await mw(req, noop_next)
        assert resp.status == 200


@test("Middleware: If-Modified-Since returns 304 for old file")
async def test_mw_if_modified_since():
    with TempStaticDir() as d:
        path = d.write("old.txt", "old content")
        # Set mtime to past
        old_time = 1700000000.0  # Nov 2023
        os.utime(path, (old_time, old_time))

        mw = StaticFilesMiddleware(
            static_dirs=[d.path], prefix="/static/", use_cache=False
        )

        from email.utils import formatdate

        future = formatdate(old_time + 100, usegmt=True)
        req = make_request(
            path="/static/old.txt", headers={"if-modified-since": future}
        )
        resp = await mw(req, noop_next)
        assert resp.status == 304


# ---------------------------------------------------------------------------
# Tests: Cache-Control headers
# ---------------------------------------------------------------------------


@test("Middleware: Cache-Control with default max_age")
async def test_mw_cache_control():
    with TempStaticDir() as d:
        d.write("app.js", "// app")
        mw = StaticFilesMiddleware(
            static_dirs=[d.path], prefix="/static/", max_age=3600
        )

        req = make_request(path="/static/app.js")
        resp = await mw(req, noop_next)
        cc = resp.headers.get("cache-control", "")
        assert "max-age=3600" in cc
        assert "public" in cc


@test("Middleware: immutable flag in Cache-Control")
async def test_mw_immutable():
    with TempStaticDir() as d:
        d.write("app.js", "// app")
        mw = StaticFilesMiddleware(
            static_dirs=[d.path],
            prefix="/static/",
            immutable=True,
            max_age=31536000,
        )

        req = make_request(path="/static/app.js")
        resp = await mw(req, noop_next)
        cc = resp.headers.get("cache-control", "")
        assert "immutable" in cc
        assert "max-age=31536000" in cc


@test("Middleware: auto-detects content-hashed filename as immutable")
async def test_mw_auto_immutable():
    with TempStaticDir() as d:
        d.write("app.a1b2c3d4e5f6.js", "// hashed")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/app.a1b2c3d4e5f6.js")
        resp = await mw(req, noop_next)
        cc = resp.headers.get("cache-control", "")
        assert "immutable" in cc


@test("Middleware: Last-Modified header set")
async def test_mw_last_modified():
    with TempStaticDir() as d:
        d.write("dated.txt", "content")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/dated.txt")
        resp = await mw(req, noop_next)
        assert "last-modified" in resp.headers


@test("Middleware: Vary: Accept-Encoding header")
async def test_mw_vary():
    with TempStaticDir() as d:
        d.write("file.txt", "content")
        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        req = make_request(path="/static/file.txt")
        resp = await mw(req, noop_next)
        assert resp.headers.get("vary") == "Accept-Encoding"


# ---------------------------------------------------------------------------
# Tests: Gzip compression
# ---------------------------------------------------------------------------


@test("Middleware: gzip compresses large CSS files")
async def test_mw_gzip_css():
    with TempStaticDir() as d:
        # Large enough to trigger compression
        content = "body { color: red; }\n" * 200
        d.write("big.css", content)
        mw = StaticFilesMiddleware(
            static_dirs=[d.path],
            prefix="/static/",
            gzip_min_size=1024,
        )

        req = make_request(
            path="/static/big.css",
            headers={"accept-encoding": "gzip, deflate"},
        )
        resp = await mw(req, noop_next)
        assert resp.status == 200
        assert resp.headers.get("content-encoding") == "gzip"
        # Verify gzip is valid
        decompressed = gzip.decompress(resp.body)
        assert decompressed == content.encode()


@test("Middleware: no gzip for small files")
async def test_mw_no_gzip_small():
    with TempStaticDir() as d:
        d.write("tiny.css", "a{}")
        mw = StaticFilesMiddleware(
            static_dirs=[d.path],
            prefix="/static/",
            gzip_min_size=1024,
        )

        req = make_request(
            path="/static/tiny.css",
            headers={"accept-encoding": "gzip"},
        )
        resp = await mw(req, noop_next)
        assert "content-encoding" not in resp.headers


@test("Middleware: no gzip for binary files")
async def test_mw_no_gzip_binary():
    with TempStaticDir() as d:
        d.write("image.png", b"\x89PNG" + b"\x00" * 2000)
        mw = StaticFilesMiddleware(
            static_dirs=[d.path],
            prefix="/static/",
            gzip_min_size=1024,
        )

        req = make_request(
            path="/static/image.png",
            headers={"accept-encoding": "gzip"},
        )
        resp = await mw(req, noop_next)
        assert "content-encoding" not in resp.headers


@test("Middleware: no gzip when client doesn't accept")
async def test_mw_no_gzip_no_accept():
    with TempStaticDir() as d:
        content = "body { color: red; }\n" * 200
        d.write("big.css", content)
        mw = StaticFilesMiddleware(
            static_dirs=[d.path],
            prefix="/static/",
            gzip_min_size=1024,
        )

        req = make_request(path="/static/big.css")
        resp = await mw(req, noop_next)
        assert "content-encoding" not in resp.headers


# ---------------------------------------------------------------------------
# Tests: File caching
# ---------------------------------------------------------------------------


@test("Middleware: in-memory cache serves same content")
async def test_mw_cache_hit():
    with TempStaticDir() as d:
        d.write("cached.js", "// cached")
        mw = StaticFilesMiddleware(
            static_dirs=[d.path], prefix="/static/", use_cache=True
        )

        # First request populates cache
        req1 = make_request(path="/static/cached.js")
        resp1 = await mw(req1, noop_next)
        assert resp1.status == 200

        # Second request hits cache
        req2 = make_request(path="/static/cached.js")
        resp2 = await mw(req2, noop_next)
        assert resp2.status == 200
        assert resp2.body == resp1.body


@test("Middleware: clear_cache empties file cache")
async def test_mw_clear_cache():
    with TempStaticDir() as d:
        d.write("temp.js", "// temp")
        mw = StaticFilesMiddleware(
            static_dirs=[d.path], prefix="/static/", use_cache=True
        )

        req = make_request(path="/static/temp.js")
        await mw(req, noop_next)
        assert len(mw._file_cache) > 0

        mw.clear_cache()
        assert len(mw._file_cache) == 0


# ---------------------------------------------------------------------------
# Tests: ManifestStaticFilesStorage
# ---------------------------------------------------------------------------


@test("Manifest: collectstatic copies and hashes files")
def test_manifest_collect():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("app.js", "console.log('hello');")
        src.write("style.css", "body { margin: 0; }")
        src.write("logo.png", b"\x89PNG\x00\x00")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        result = storage.collectstatic()

        assert result["copied"] == 3
        assert len(result["errors"]) == 0

        # Manifest should exist
        manifest_path = Path(dest.path) / "staticfiles.json"
        assert manifest_path.exists()

        # All files should have hashed copies
        manifest = storage.load_manifest()
        assert "app.js" in manifest
        assert "style.css" in manifest
        assert "logo.png" in manifest

        # Hashed names should follow pattern
        for original, hashed in manifest.items():
            assert original != hashed
            parts = hashed.rsplit(".", 2)
            assert len(parts) >= 3  # base.hash.ext


@test("Manifest: hashed filename format is correct")
def test_manifest_hash_format():
    with TempStaticDir() as src, TempStaticDir() as dest:
        content = b"test content"
        src.write("test.txt", content)

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        hashed = manifest["test.txt"]

        # Should be test.{12-char-hex}.txt
        expected_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()[:12]
        assert hashed == f"test.{expected_hash}.txt"


@test("Manifest: url() returns hashed name")
def test_manifest_url():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("main.js", "// main")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        url = storage.url("main.js")
        assert url != "main.js"
        assert url.startswith("main.")
        assert url.endswith(".js")


@test("Manifest: url() returns original name for missing file")
def test_manifest_url_missing():
    with TempStaticDir() as src, TempStaticDir() as dest:
        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        assert storage.url("nonexistent.css") == "nonexistent.css"


@test("Manifest: stored_name() raises ValueError in strict mode")
def test_manifest_strict():
    with TempStaticDir() as src, TempStaticDir() as dest:
        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        try:
            storage.stored_name("missing.css", strict=True)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "missing.css" in str(e)


@test("Manifest: stored_name() returns original in non-strict mode")
def test_manifest_non_strict():
    with TempStaticDir() as src, TempStaticDir() as dest:
        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        result = storage.stored_name("missing.css", strict=False)
        assert result == "missing.css"


@test("Manifest: clear=True removes old files")
def test_manifest_clear():
    with TempStaticDir() as src, TempStaticDir() as dest:
        # First collect
        src.write("old.txt", "old")
        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()
        assert (Path(dest.path) / "old.txt").exists()

        # Remove old, add new
        (Path(src.path) / "old.txt").unlink()
        src.write("new.txt", "new")

        storage2 = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage2.collectstatic(clear=True)

        # Old should be gone, new should exist
        assert not (Path(dest.path) / "old.txt").exists()
        assert (Path(dest.path) / "new.txt").exists()


@test("Manifest: dry_run doesn't write files")
def test_manifest_dry_run():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("test.js", "// test")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        result = storage.collectstatic(dry_run=True)

        assert result["copied"] > 0
        # No files should be written
        assert not (Path(dest.path) / "test.js").exists()
        assert not (Path(dest.path) / "staticfiles.json").exists()


@test("Manifest: preserves directory structure")
def test_manifest_dirs():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("css/base.css", "body {}")
        src.write("js/app.js", "// app")
        src.write("img/logo.svg", "<svg></svg>")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        assert "css/base.css" in manifest
        assert "js/app.js" in manifest
        assert "img/logo.svg" in manifest

        # Hashed files should be in correct subdirectories
        for original, hashed in manifest.items():
            assert str(Path(original).parent) == str(Path(hashed).parent)


@test("Manifest: manifest JSON format matches spec")
def test_manifest_format():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("a.css", "a")
        src.write("b.js", "b")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest_path = Path(dest.path) / "staticfiles.json"
        data = json.loads(manifest_path.read_text())

        assert data["version"] == "1.1"
        assert "paths" in data
        assert "hash" in data
        assert isinstance(data["paths"], dict)
        assert len(data["hash"]) == 12  # 12-char MD5


@test("Manifest: load_manifest reads from disk")
def test_manifest_load():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("test.txt", "hello")

        storage1 = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage1.collectstatic()

        # Fresh storage instance should load from disk
        storage2 = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        manifest = storage2.load_manifest()
        assert "test.txt" in manifest


# ---------------------------------------------------------------------------
# Tests: CSS URL rewriting
# ---------------------------------------------------------------------------


@test("Manifest: rewrites url() references in CSS")
def test_css_url_rewrite():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("img/bg.png", b"\x89PNG")
        src.write("css/style.css", 'body { background: url("../img/bg.png"); }')

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        hashed_css = manifest["css/style.css"]

        # Read the collected CSS
        collected_path = Path(dest.path) / hashed_css
        collected_css = collected_path.read_text()

        # The url() should reference the hashed image
        hashed_img = manifest["img/bg.png"]
        img_basename = Path(hashed_img).name
        assert img_basename in collected_css


@test("Manifest: preserves absolute URLs in CSS")
def test_css_absolute_url():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write(
            "css/fonts.css",
            '@import url("https://fonts.googleapis.com/css?family=Roboto");',
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        hashed_css = storage.url("css/fonts.css")
        content = (Path(dest.path) / hashed_css).read_text()

        assert "https://fonts.googleapis.com" in content


@test("Manifest: preserves data URIs in CSS")
def test_css_data_uri():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write(
            "css/inline.css", 'div { background: url("data:image/png;base64,abc123"); }'
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        hashed_css = storage.url("css/inline.css")
        content = (Path(dest.path) / hashed_css).read_text()

        assert "data:image/png;base64,abc123" in content


@test("Manifest: rewrites @import in CSS")
def test_css_import_rewrite():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("css/base.css", "body { margin: 0; }")
        src.write("css/main.css", '@import "base.css";\nh1 { color: red; }')

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        hashed_main = manifest["css/main.css"]
        content = (Path(dest.path) / hashed_main).read_text()

        # Should reference hashed base.css
        hashed_base = manifest["css/base.css"]
        base_basename = Path(hashed_base).name
        assert base_basename in content


# ---------------------------------------------------------------------------
# Tests: Global helpers
# ---------------------------------------------------------------------------


@test("get_static_url: with manifest returns hashed URL")
def test_get_static_url_manifest():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("app.js", "// app")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        old = get_manifest_storage()
        set_manifest_storage(storage)
        try:
            url = get_static_url("app.js")
            assert url.startswith("/static/")
            assert url != "/static/app.js"
            assert ".js" in url
        finally:
            set_manifest_storage(old)


@test("get_static_url: without manifest returns plain URL")
def test_get_static_url_no_manifest():
    old = get_manifest_storage()
    set_manifest_storage(None)
    try:
        url = get_static_url("css/style.css")
        assert url == "/static/css/style.css"
    finally:
        if old is not None:
            set_manifest_storage(old)


@test("get_static_url: custom prefix")
def test_get_static_url_prefix():
    old = get_manifest_storage()
    set_manifest_storage(None)
    try:
        url = get_static_url("app.js", prefix="/assets/")
        assert url == "/assets/app.js"
    finally:
        if old is not None:
            set_manifest_storage(old)


# ---------------------------------------------------------------------------
# Tests: Middleware with static_root (production mode)
# ---------------------------------------------------------------------------


@test("Middleware: serves from static_root directory")
async def test_mw_static_root():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("prod.js", "// production")

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        # Middleware using static_root
        mw = StaticFilesMiddleware(
            static_root=dest.path,
            prefix="/static/",
        )

        manifest = storage.load_manifest()
        hashed = manifest["prod.js"]

        req = make_request(path=f"/static/{hashed}")
        resp = await mw(req, noop_next)
        assert resp.status == 200
        assert b"production" in resp.body


# ---------------------------------------------------------------------------
# Tests: _is_hashed_filename detection
# ---------------------------------------------------------------------------


@test("hashed filename detection: valid hashed name")
def test_hashed_detection_valid():
    assert StaticFilesMiddleware._is_hashed_filename("app.a1b2c3d4e5f6.js") is True
    assert (
        StaticFilesMiddleware._is_hashed_filename("css/style.abcdef123456.css") is True
    )


@test("hashed filename detection: non-hashed name")
def test_hashed_detection_invalid():
    assert StaticFilesMiddleware._is_hashed_filename("app.js") is False
    assert (
        StaticFilesMiddleware._is_hashed_filename("app.min.js") is False
    )  # "min" is not hex
    assert StaticFilesMiddleware._is_hashed_filename("readme.txt") is False


@test("hashed filename detection: edge cases")
def test_hashed_detection_edge():
    # Too short hash (< 12 chars)
    assert StaticFilesMiddleware._is_hashed_filename("app.abc.js") is False
    assert (
        StaticFilesMiddleware._is_hashed_filename("app.abcdef01.js") is False
    )  # 8 chars — not 12
    # Exactly 12 chars is valid
    assert StaticFilesMiddleware._is_hashed_filename("app.abcdef012345.js") is True
    # Legitimate filenames that should NOT be detected as hashed
    assert StaticFilesMiddleware._is_hashed_filename("chart.d3.js") is False


# ---------------------------------------------------------------------------
# Tests: Prefix normalization
# ---------------------------------------------------------------------------


@test("Middleware: normalizes prefix with missing slashes")
def test_prefix_normalization():
    mw1 = StaticFilesMiddleware(prefix="static")
    assert mw1.prefix == "/static/"

    mw2 = StaticFilesMiddleware(prefix="/static")
    assert mw2.prefix == "/static/"

    mw3 = StaticFilesMiddleware(prefix="/static/")
    assert mw3.prefix == "/static/"


# ---------------------------------------------------------------------------
# Tests: Multiple source directories
# ---------------------------------------------------------------------------


@test("Middleware: serves from multiple directories")
async def test_mw_multi_dir():
    with TempStaticDir() as d1, TempStaticDir() as d2:
        d1.write("from_first.js", "// first")
        d2.write("from_second.js", "// second")

        mw = StaticFilesMiddleware(static_dirs=[d1.path, d2.path], prefix="/static/")

        req1 = make_request(path="/static/from_first.js")
        resp1 = await mw(req1, noop_next)
        assert resp1.status == 200
        assert b"first" in resp1.body

        req2 = make_request(path="/static/from_second.js")
        resp2 = await mw(req2, noop_next)
        assert resp2.status == 200
        assert b"second" in resp2.body


# ---------------------------------------------------------------------------
# Tests: Content-type detection
# ---------------------------------------------------------------------------


@test("Middleware: detects common content types")
async def test_mw_content_types():
    with TempStaticDir() as d:
        d.write("style.css", "body {}")
        d.write("app.js", "// js")
        d.write("page.html", "<html>")
        d.write("data.json", "{}")
        d.write("image.svg", "<svg>")

        mw = StaticFilesMiddleware(static_dirs=[d.path], prefix="/static/")

        for filename, expected_type in [
            ("style.css", "text/css"),
            ("app.js", "javascript"),
            ("page.html", "text/html"),
            ("data.json", "application/json"),
            ("image.svg", "image/svg+xml"),
        ]:
            req = make_request(path=f"/static/{filename}")
            resp = await mw(req, noop_next)
            ct = resp.headers.get("content-type", "")
            assert expected_type in ct, f"{filename}: expected {expected_type} in {ct}"


# ---------------------------------------------------------------------------
# Tests: Manifest multiple source directories
# ---------------------------------------------------------------------------


@test("Manifest: collects from multiple directories")
def test_manifest_multi_dir():
    with TempStaticDir() as d1, TempStaticDir() as d2, TempStaticDir() as dest:
        d1.write("a.css", "a")
        d2.write("b.js", "b")

        storage = ManifestStaticFilesStorage(
            static_dirs=[d1.path, d2.path],
            static_root=dest.path,
        )
        result = storage.collectstatic()

        assert result["copied"] == 2
        manifest = storage.load_manifest()
        assert "a.css" in manifest
        assert "b.js" in manifest


@test("Manifest: first directory wins for duplicates")
def test_manifest_dedup():
    with TempStaticDir() as d1, TempStaticDir() as d2, TempStaticDir() as dest:
        d1.write("shared.js", "version1")
        d2.write("shared.js", "version2")

        storage = ManifestStaticFilesStorage(
            static_dirs=[d1.path, d2.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        # Read collected file — should be from d1
        assert "version1" in (Path(dest.path) / "shared.js").read_text()


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)  # noqa: B009
    ]

    print(f"\nStatic Files Tests ({len(tests)} tests)")
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


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
