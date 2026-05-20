"""Tests for template cache stats API and dynamic path caching.

Tests TemplateEngine.cache_stats(), reset_cache_stats(), CacheStats dataclass,
resolved-path cache for dynamic extends/include, and cache counter accuracy
across all three tiers: LRU → disk bytecode → compile from source.
"""

# hyper-test: unit

import sys
import tempfile
import time
from pathlib import Path

from hyperdjango.templating import CacheStats, TemplateEngine, _LRUCache


def test_cache_stats_dataclass():
    """CacheStats fields and computed properties."""
    stats = CacheStats(
        lru_hits=80,
        lru_misses=20,
        disk_hits=15,
        disk_misses=5,
        compiles=5,
        evictions=2,
        lru_entries=10,
        lru_bytes=4096,
        lru_max_bytes=256 * 1024 * 1024,
    )
    assert stats.lru_hits == 80
    assert stats.lru_misses == 20
    assert stats.disk_hits == 15
    assert stats.disk_misses == 5
    assert stats.compiles == 5
    assert stats.evictions == 2
    assert stats.lru_entries == 10
    assert stats.lru_bytes == 4096
    assert stats.total_lookups == 100
    assert abs(stats.lru_hit_rate - 0.8) < 1e-9
    assert abs(stats.disk_hit_rate - 0.75) < 1e-9
    print("  PASS: CacheStats dataclass fields and properties")


def test_cache_stats_zero_division():
    """Hit rate returns 0.0 when no lookups have occurred."""
    stats = CacheStats(
        lru_hits=0,
        lru_misses=0,
        disk_hits=0,
        disk_misses=0,
        compiles=0,
        evictions=0,
        lru_entries=0,
        lru_bytes=0,
        lru_max_bytes=256 * 1024 * 1024,
    )
    assert stats.lru_hit_rate == 0.0
    assert stats.disk_hit_rate == 0.0
    assert stats.total_lookups == 0
    print("  PASS: CacheStats zero-division safety")


def test_lru_hit_miss_tracking():
    """_LRUCache tracks hits and misses on get()."""
    cache = _LRUCache()
    cache.put("a", "val_a", source_size=10)

    # Hit
    assert cache.get("a") == "val_a"
    assert cache.hits == 1
    assert cache.misses == 0

    # Miss
    assert cache.get("nonexistent") is None
    assert cache.hits == 1
    assert cache.misses == 1

    # Another hit
    assert cache.get("a") == "val_a"
    assert cache.hits == 2
    assert cache.misses == 1
    print("  PASS: LRU hit/miss tracking")


def test_lru_eviction_tracking():
    """_LRUCache tracks evictions when exceeding max_bytes."""
    cache = _LRUCache(max_bytes=100)
    cache.put("a", "val_a", source_size=60)
    cache.put("b", "val_b", source_size=60)  # Should evict "a"
    assert cache.evictions == 1
    assert cache.get("a") is None  # Evicted
    assert cache.get("b") == "val_b"
    print("  PASS: LRU eviction tracking")


def test_lru_reset_counters():
    """reset_counters() zeroes hit/miss/eviction but keeps data."""
    cache = _LRUCache()
    cache.put("a", "val_a", source_size=10)
    cache.get("a")
    cache.get("miss")
    assert cache.hits == 1
    assert cache.misses == 1

    cache.reset_counters()
    assert cache.hits == 0
    assert cache.misses == 0
    assert cache.evictions == 0
    # Data is still there
    assert cache.get("a") == "val_a"
    assert cache.hits == 1
    print("  PASS: LRU reset_counters preserves data")


def test_engine_cache_stats_initial():
    """Fresh engine has all zero stats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        stats = engine.cache_stats()
        assert stats.lru_hits == 0
        assert stats.lru_misses == 0
        assert stats.disk_hits == 0
        assert stats.disk_misses == 0
        assert stats.compiles == 0
        assert stats.evictions == 0
        assert stats.lru_entries == 0
        assert stats.lru_bytes == 0
        print("  PASS: Engine initial cache stats all zero")


def test_engine_cache_stats_compile():
    """First render compiles and increments compile counter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpl = Path(tmpdir) / "hello.html"
        tpl.write_text("Hello {{ name }}!")
        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        result = engine.render("hello.html", {"name": "World"})
        assert result == "Hello World!"

        stats = engine.cache_stats()
        assert stats.lru_misses == 1  # First lookup is a miss
        assert stats.compiles == 1
        assert stats.disk_misses == 0  # Bytecode cache disabled
        assert stats.disk_hits == 0
        assert stats.lru_entries == 1
        assert stats.lru_bytes > 0
        print("  PASS: Engine compile increments stats")


def test_engine_cache_stats_lru_hit():
    """Second render of same template hits LRU."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpl = Path(tmpdir) / "hello.html"
        tpl.write_text("Hello {{ name }}!")
        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        engine.render("hello.html", {"name": "A"})
        engine.render("hello.html", {"name": "B"})

        stats = engine.cache_stats()
        assert stats.lru_hits == 1  # Second render hit
        assert stats.lru_misses == 1  # First render miss
        assert stats.compiles == 1  # Only compiled once
        print("  PASS: Engine LRU hit on second render")


def test_engine_cache_stats_multiple_templates():
    """Stats accumulate across multiple different templates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for name in ("a.html", "b.html", "c.html"):
            (Path(tmpdir) / name).write_text(f"Template {name}")
        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        engine.render("a.html")
        engine.render("b.html")
        engine.render("c.html")
        engine.render("a.html")  # LRU hit
        engine.render("b.html")  # LRU hit

        stats = engine.cache_stats()
        assert stats.lru_misses == 3
        assert stats.lru_hits == 2
        assert stats.compiles == 3
        assert stats.lru_entries == 3
        print("  PASS: Engine stats accumulate across templates")


def test_engine_cache_stats_disk_hit():
    """Disk bytecode cache hit is tracked separately from compile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpl = Path(tmpdir) / "hello.html"
        tpl.write_text("Hello {{ name }}!")

        # First engine: compile and save to disk cache
        engine1 = TemplateEngine(template_dir=tmpdir, bytecode_cache=True)
        engine1.render("hello.html", {"name": "World"})
        stats1 = engine1.cache_stats()
        assert stats1.compiles == 1
        assert stats1.disk_misses == 1  # First time, no disk cache exists

        # Second engine: should load from disk cache
        engine2 = TemplateEngine(template_dir=tmpdir, bytecode_cache=True)
        engine2.render("hello.html", {"name": "World"})
        stats2 = engine2.cache_stats()
        assert stats2.disk_hits == 1
        assert stats2.compiles == 0  # Loaded from disk, no compile needed
        print("  PASS: Engine disk cache hit tracking")


def test_engine_cache_stats_disk_miss():
    """Disk cache miss (no .hztc file) tracked correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpl = Path(tmpdir) / "hello.html"
        tpl.write_text("Hello!")
        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=True)
        engine.render("hello.html")
        stats = engine.cache_stats()
        assert stats.disk_misses == 1
        assert stats.compiles == 1
        print("  PASS: Engine disk cache miss tracking")


def test_engine_reset_cache_stats():
    """reset_cache_stats() zeroes all counters but preserves cached templates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpl = Path(tmpdir) / "hello.html"
        tpl.write_text("Hello {{ name }}!")
        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        engine.render("hello.html", {"name": "World"})
        engine.render("hello.html", {"name": "Again"})

        stats_before = engine.cache_stats()
        assert stats_before.compiles == 1
        assert stats_before.lru_hits == 1

        engine.reset_cache_stats()
        stats_after = engine.cache_stats()
        assert stats_after.compiles == 0
        assert stats_after.lru_hits == 0
        assert stats_after.lru_misses == 0
        assert stats_after.disk_hits == 0
        assert stats_after.disk_misses == 0
        assert stats_after.evictions == 0
        # Templates still cached
        assert stats_after.lru_entries == 1
        assert stats_after.lru_bytes > 0

        # Next render should be LRU hit
        engine.render("hello.html", {"name": "Post-reset"})
        stats_post = engine.cache_stats()
        assert stats_post.lru_hits == 1
        assert stats_post.compiles == 0
        print("  PASS: Engine reset_cache_stats preserves templates")


def test_engine_cache_stats_eviction():
    """Eviction counter tracks LRU evictions under memory pressure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(5):
            (Path(tmpdir) / f"t{i}.html").write_text(f"Template {i} " + "x" * 90)
        engine = TemplateEngine(
            template_dir=tmpdir,
            bytecode_cache=False,
            auto_reload=False,
            cache_max_bytes=250,  # Only ~2.5 templates fit
        )
        for i in range(5):
            engine.render(f"t{i}.html")

        stats = engine.cache_stats()
        assert stats.evictions > 0
        assert stats.compiles == 5
        print(f"  PASS: Engine eviction tracking ({stats.evictions} evictions)")


def test_engine_cache_stats_render_string():
    """render_string also triggers LRU stats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        engine.render_string("Hello {{ x }}", {"x": 1})
        engine.render_string("Hello {{ x }}", {"x": 2})  # Same source = LRU hit

        stats = engine.cache_stats()
        assert stats.lru_hits == 1
        assert stats.lru_misses == 1
        print("  PASS: render_string updates LRU stats")


# ── Resolved-path cache tests ────────────────────────────────────────────────


def test_resolved_path_cache_populated():
    """First load populates the resolved-path cache."""
    with tempfile.TemporaryDirectory() as tmpdir:
        partial = Path(tmpdir) / "partial.html"
        partial.write_text("Partial content")
        main = Path(tmpdir) / "main.html"
        main.write_text('{% include "partial.html" %}')

        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        result = engine.render("main.html")
        assert "Partial content" in result

        # The resolved-path cache should have an entry for partial.html
        assert "partial.html" in engine._resolved_path_cache
        abs_path, mtime = engine._resolved_path_cache["partial.html"]
        assert Path(abs_path).is_absolute()
        assert mtime > 0
        print("  PASS: Resolved-path cache populated on include")


def test_resolved_path_cache_reuse():
    """Second render reuses cached resolved path (avoids Path.resolve)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        partial = Path(tmpdir) / "partial.html"
        partial.write_text("Partial v1")
        main = Path(tmpdir) / "main.html"
        main.write_text('{% include "partial.html" %}')

        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        engine.render("main.html")
        assert "partial.html" in engine._resolved_path_cache

        # Second render should still work via cache
        result = engine.render("main.html")
        assert "Partial v1" in result
        print("  PASS: Resolved-path cache reuse on second render")


def test_resolved_path_cache_invalidation():
    """Changed file (different mtime) invalidates resolved-path cache entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        partial = Path(tmpdir) / "partial.html"
        partial.write_text("Partial v1")
        main = Path(tmpdir) / "main.html"
        main.write_text('{% include "partial.html" %}')

        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        result1 = engine.render("main.html")
        assert "Partial v1" in result1

        # Modify partial — need different mtime
        time.sleep(0.05)
        partial.write_text("Partial v2")

        # Clear main from LRU to force re-include
        engine._compiled_cache.clear()
        result2 = engine.render("main.html")
        assert "Partial v2" in result2
        print("  PASS: Resolved-path cache invalidation on file change")


def test_resolved_path_cache_removed_file():
    """Removed file gracefully falls through to full resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        partial = Path(tmpdir) / "partial.html"
        partial.write_text("Partial content")

        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        # Manually populate the cache
        source = engine._load_template_source("partial.html")
        assert source == "Partial content"
        assert "partial.html" in engine._resolved_path_cache

        # Remove the file
        partial.unlink()

        # Should fall through and raise FileNotFoundError
        try:
            engine._load_template_source("partial.html")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass

        # Cache entry should be removed
        assert "partial.html" not in engine._resolved_path_cache
        print("  PASS: Resolved-path cache handles removed file")


def test_resolved_path_cache_security():
    """Path traversal is still blocked even with resolved-path cache."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        try:
            engine._load_template_source("../../etc/passwd")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError as e:
            assert "escapes" in str(e)
        # No entry cached for traversal attempt
        assert "../../etc/passwd" not in engine._resolved_path_cache
        print("  PASS: Resolved-path cache security (path traversal blocked)")


def test_resolved_path_cache_dynamic_extends():
    """Dynamic extends also benefits from resolved-path cache."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "base.html"
        base.write_text("BASE:{% block content %}default{% endblock %}")
        child = Path(tmpdir) / "child.html"
        child.write_text(
            '{% extends "base.html" %}{% block content %}CHILD{% endblock %}'
        )

        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        result = engine.render("child.html")
        assert "BASE:" in result
        assert "CHILD" in result

        # base.html should be in resolved-path cache (loaded via extends)
        assert "base.html" in engine._resolved_path_cache
        print("  PASS: Resolved-path cache works with extends")


def test_resolved_path_cache_multiple_includes():
    """Multiple different includes all get cached."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for name in ("header.html", "footer.html", "sidebar.html"):
            (Path(tmpdir) / name).write_text(f"<{name}>")
        (Path(tmpdir) / "page.html").write_text(
            '{% include "header.html" %}'
            '{% include "sidebar.html" %}'
            '{% include "footer.html" %}'
        )
        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)
        result = engine.render("page.html")
        assert "<header.html>" in result
        assert "<sidebar.html>" in result
        assert "<footer.html>" in result

        assert "header.html" in engine._resolved_path_cache
        assert "sidebar.html" in engine._resolved_path_cache
        assert "footer.html" in engine._resolved_path_cache
        print("  PASS: Resolved-path cache handles multiple includes")


def test_cache_stats_with_disk_and_lru():
    """End-to-end: compile → disk save → new engine → disk hit → LRU hit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpl = Path(tmpdir) / "hello.html"
        tpl.write_text("Hello {{ name }}!")

        # Phase 1: Compile from source (LRU miss → disk miss → compile)
        engine1 = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=True, auto_reload=False
        )
        engine1.render("hello.html", {"name": "A"})
        s1 = engine1.cache_stats()
        assert s1.lru_misses == 1
        assert s1.disk_misses == 1
        assert s1.compiles == 1
        assert s1.disk_hits == 0
        assert s1.lru_hits == 0

        # Phase 2: LRU hit (same engine, same template)
        engine1.render("hello.html", {"name": "B"})
        s2 = engine1.cache_stats()
        assert s2.lru_hits == 1
        assert s2.compiles == 1  # No new compile

        # Phase 3: New engine, disk cache hit (LRU miss → disk hit)
        engine2 = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=True, auto_reload=False
        )
        engine2.render("hello.html", {"name": "C"})
        s3 = engine2.cache_stats()
        assert s3.lru_misses == 1
        assert s3.disk_hits == 1
        assert s3.compiles == 0  # Loaded from disk

        # Phase 4: LRU hit on engine2
        engine2.render("hello.html", {"name": "D"})
        s4 = engine2.cache_stats()
        assert s4.lru_hits == 1
        assert s4.compiles == 0
        print("  PASS: End-to-end compile → disk → LRU stats flow")


def test_cache_stats_hit_rate_precision():
    """Hit rate calculation with asymmetric hit/miss counts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(10):
            (Path(tmpdir) / f"t{i}.html").write_text(f"Template {i}")
        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        # 10 unique templates = 10 misses
        for i in range(10):
            engine.render(f"t{i}.html")
        # Render first 3 again = 3 hits
        for i in range(3):
            engine.render(f"t{i}.html")

        stats = engine.cache_stats()
        assert stats.lru_misses == 10
        assert stats.lru_hits == 3
        assert stats.total_lookups == 13
        expected_rate = 3.0 / 13.0
        assert abs(stats.lru_hit_rate - expected_rate) < 1e-9
        print(f"  PASS: Hit rate precision ({stats.lru_hit_rate:.4f})")


def test_cache_stats_concurrent_safe():
    """Stats remain consistent under sequential rapid access."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(20):
            (Path(tmpdir) / f"t{i}.html").write_text(f"T{i}")
        engine = TemplateEngine(
            template_dir=tmpdir, bytecode_cache=False, auto_reload=False
        )
        # Rapid sequential: render all 20, then repeat 5 times
        for _ in range(5):
            for i in range(20):
                engine.render(f"t{i}.html")

        stats = engine.cache_stats()
        # 20 misses (first pass) + 80 hits (4 repeat passes)
        assert stats.lru_misses == 20
        assert stats.lru_hits == 80
        assert stats.compiles == 20
        assert stats.total_lookups == 100
        assert abs(stats.lru_hit_rate - 0.8) < 1e-9
        print("  PASS: Cache stats consistent under rapid access")


def test_resolved_path_cache_benchmark():
    """Benchmark: resolved-path cache vs cold resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        partial = Path(tmpdir) / "bench_partial.html"
        partial.write_text("Bench content")

        engine = TemplateEngine(template_dir=tmpdir, bytecode_cache=False)

        # Cold resolution (first call populates cache)
        iterations = 1000
        start = time.perf_counter_ns()
        for _ in range(iterations):
            engine._resolved_path_cache.clear()
            engine._load_template_source("bench_partial.html")
        cold_ns = (time.perf_counter_ns() - start) / iterations

        # Warm resolution (cache populated)
        engine._load_template_source("bench_partial.html")  # Populate
        start = time.perf_counter_ns()
        for _ in range(iterations):
            engine._load_template_source("bench_partial.html")
        warm_ns = (time.perf_counter_ns() - start) / iterations

        speedup = cold_ns / warm_ns if warm_ns > 0 else 0
        print(
            f"  PASS: Path cache benchmark — cold: {cold_ns:.0f}ns, warm: {warm_ns:.0f}ns, speedup: {speedup:.2f}x"
        )


def main():
    tests = [
        # CacheStats dataclass
        test_cache_stats_dataclass,
        test_cache_stats_zero_division,
        # _LRUCache counters
        test_lru_hit_miss_tracking,
        test_lru_eviction_tracking,
        test_lru_reset_counters,
        # Engine cache_stats()
        test_engine_cache_stats_initial,
        test_engine_cache_stats_compile,
        test_engine_cache_stats_lru_hit,
        test_engine_cache_stats_multiple_templates,
        test_engine_cache_stats_disk_hit,
        test_engine_cache_stats_disk_miss,
        test_engine_reset_cache_stats,
        test_engine_cache_stats_eviction,
        test_engine_cache_stats_render_string,
        # Resolved-path cache
        test_resolved_path_cache_populated,
        test_resolved_path_cache_reuse,
        test_resolved_path_cache_invalidation,
        test_resolved_path_cache_removed_file,
        test_resolved_path_cache_security,
        test_resolved_path_cache_dynamic_extends,
        test_resolved_path_cache_multiple_includes,
        # Integration
        test_cache_stats_with_disk_and_lru,
        test_cache_stats_hit_rate_precision,
        test_cache_stats_concurrent_safe,
        # Benchmark
        test_resolved_path_cache_benchmark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Template Cache Stats + Path Caching Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test.__name__, str(e)))
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
