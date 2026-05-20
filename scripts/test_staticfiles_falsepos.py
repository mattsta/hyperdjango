"""
Tests for ManifestStaticFilesStorage CSS url()/@import false-positive fix.

url() and @import tokens that appear INSIDE
CSS string literals (content: "url(...)") or comments (/* url(...) */) must NOT
be rewritten to hashed filenames, while real background:url(real.png) outside of
strings/comments IS hashed/rewritten exactly as before.

Usage:
    uv run hyper-test staticfiles_falsepos
"""

# hyper-test: unit

import asyncio
import inspect
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from hyperdjango.staticfiles import ManifestStaticFilesStorage

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
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


class TempStaticDir:
    """Context manager that creates a temp directory with static files."""

    def __init__(self):
        self.root = None

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="hyper_static_fp_test_")
        return self

    def __exit__(self, *args):
        if self.root and Path(self.root).exists():
            shutil.rmtree(self.root)

    def write(self, rel_path, content):
        full = Path(self.root) / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        full.write_bytes(content)
        return str(full)

    @property
    def path(self):
        return self.root


# ---------------------------------------------------------------------------
# Tests: get_ignored_blocks / is_in_ignored_block primitives
# ---------------------------------------------------------------------------


@test("get_ignored_blocks: detects block comment span")
def test_ignored_block_comment():
    css = "a {} /* url(x.png) */ b {}"
    blocks = ManifestStaticFilesStorage.get_ignored_blocks(css)
    # The url( token inside the comment must be covered by a span.
    pos = css.index("url(")
    assert ManifestStaticFilesStorage.is_in_ignored_block(pos, blocks)


@test("get_ignored_blocks: detects double-quoted string span")
def test_ignored_double_string():
    css = 'a { content: "url(x.png)"; }'
    blocks = ManifestStaticFilesStorage.get_ignored_blocks(css)
    pos = css.index("url(")
    assert ManifestStaticFilesStorage.is_in_ignored_block(pos, blocks)


@test("get_ignored_blocks: detects single-quoted string span")
def test_ignored_single_string():
    css = "a { content: 'url(x.png)'; }"
    blocks = ManifestStaticFilesStorage.get_ignored_blocks(css)
    pos = css.index("url(")
    assert ManifestStaticFilesStorage.is_in_ignored_block(pos, blocks)


@test("is_in_ignored_block: real url() outside strings is NOT ignored")
def test_real_url_not_ignored():
    css = "a { background: url(real.png); }"
    blocks = ManifestStaticFilesStorage.get_ignored_blocks(css)
    pos = css.index("url(")
    assert not ManifestStaticFilesStorage.is_in_ignored_block(pos, blocks)


# ---------------------------------------------------------------------------
# Tests: end-to-end collectstatic CSS rewriting
# ---------------------------------------------------------------------------


@test("Manifest: url() inside content string literal is left verbatim")
def test_content_string_url_unchanged():
    with TempStaticDir() as src, TempStaticDir() as dest:
        # An asset that would be hashed if referenced for real.
        src.write("should_not_change.png", b"\x89PNG")
        src.write(
            "css/style.css",
            'a::before { content: "url(should_not_change.png)"; }',
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        content = (Path(dest.path) / manifest["css/style.css"]).read_text()

        # The string literal must be preserved byte-for-byte (no hash inserted).
        assert 'content: "url(should_not_change.png)"' in content
        hashed_png = Path(manifest["should_not_change.png"]).name
        assert hashed_png not in content


@test("Manifest: url() inside a CSS block comment is left verbatim")
def test_commented_url_unchanged():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("commented.png", b"\x89PNG")
        src.write(
            "css/style.css",
            "/* url(commented.png) */\nbody { color: red; }",
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        content = (Path(dest.path) / manifest["css/style.css"]).read_text()

        assert "/* url(commented.png) */" in content
        hashed_png = Path(manifest["commented.png"]).name
        assert hashed_png not in content


@test("Manifest: real background url() IS still hashed/rewritten")
def test_real_url_still_rewritten():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("img/real.png", b"\x89PNG")
        src.write(
            "css/style.css",
            "body { background: url(../img/real.png); }",
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        content = (Path(dest.path) / manifest["css/style.css"]).read_text()

        hashed_img = Path(manifest["img/real.png"]).name
        assert hashed_img in content
        # The original plain reference must be gone.
        assert "url(../img/real.png)" not in content


@test("Manifest: mixed file — real url() hashed, string/comment ones verbatim")
def test_mixed_real_and_false_positives():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("img/real.png", b"\x89PNG")
        src.write("should_not_change.png", b"\x89PNG")
        src.write("commented.png", b"\x89PNG")
        src.write(
            "css/style.css",
            "\n".join(
                [
                    "/* url(commented.png) */",
                    'a::before { content: "url(should_not_change.png)"; }',
                    "body { background: url(../img/real.png); }",
                ]
            ),
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        content = (Path(dest.path) / manifest["css/style.css"]).read_text()

        # False positives untouched.
        assert "/* url(commented.png) */" in content
        assert 'content: "url(should_not_change.png)"' in content
        assert Path(manifest["should_not_change.png"]).name not in content
        assert Path(manifest["commented.png"]).name not in content

        # Real reference rewritten.
        assert Path(manifest["img/real.png"]).name in content
        assert "url(../img/real.png)" not in content


@test("Manifest: @import inside a comment is left verbatim, real @import rewritten")
def test_import_false_positive():
    with TempStaticDir() as src, TempStaticDir() as dest:
        src.write("css/base.css", "body { margin: 0; }")
        src.write("css/fake.css", "/* should not be collected */")
        src.write(
            "css/main.css",
            "\n".join(
                [
                    '/* @import "fake.css"; */',
                    '@import "base.css";',
                    "h1 { color: red; }",
                ]
            ),
        )

        storage = ManifestStaticFilesStorage(
            static_dirs=[src.path],
            static_root=dest.path,
        )
        storage.collectstatic()

        manifest = storage.load_manifest()
        content = (Path(dest.path) / manifest["css/main.css"]).read_text()

        # Commented @import stays verbatim (fake.css never substituted there).
        assert '/* @import "fake.css"; */' in content
        # Real @import rewritten to hashed base.css.
        hashed_base = Path(manifest["css/base.css"]).name
        assert hashed_base in content


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)  # noqa: B009
    ]

    print(f"\nStatic Files False-Positive Tests ({len(tests)} tests)")
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
