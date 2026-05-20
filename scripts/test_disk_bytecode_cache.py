"""Tests for disk-backed bytecode cache (.hztc files) in TemplateEngine."""

# hyper-test: unit

import os
import shutil
import sys
import time
from pathlib import Path

from hyperdjango.templating import TemplateEngine, _fnv1a_64

passed = 0
failed = 0
errors: list[str] = []

# Test directory setup
TEST_DIR = Path(__file__).parent / "_test_disk_cache_templates"
CACHE_DIR = TEST_DIR / "__pycache__" / "hztc"


def setup():
    """Create fresh test template directory."""
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True, exist_ok=True)


def teardown():
    """Remove test template directory."""
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)


def write_template(name: str, content: str) -> Path:
    """Write a template file and return its path, with a strictly-newer mtime.

    A REWRITE stamps the mtime a full second past the previous value. The
    engine validates its .hztc entries by content hash and its in-memory LRU by
    mtime, and filesystem timestamp granularity (1s on some CI filesystems) can
    leave a rewrite looking untouched to the mtime check. That used to be
    papered over by sleeping before asserting a recompile, which made the
    assertion depend on how much wall clock the machine happened to burn.
    Stamping the mtime forward makes the change observable immediately and on
    any filesystem.
    """
    path = TEST_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mtime = path.stat().st_mtime if path.exists() else None
    path.write_text(content, encoding="utf-8")
    if previous_mtime is not None:
        bumped = max(previous_mtime, path.stat().st_mtime) + 1.0
        os.utime(path, (bumped, bumped))
    return path


def test(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} {detail}")
        failed += 1
        errors.append(name)


print("=" * 60)
print("TEST: Disk-backed bytecode cache (.hztc files)")
print("=" * 60)

# ── 1. Basic: render creates .hztc file ──
print("\n── Basic disk cache creation ──")
setup()
write_template("hello.html", "Hello {{ name }}!")
engine = TemplateEngine(template_dir=str(TEST_DIR))
result = engine.render("hello.html", {"name": "World"})
test("basic render works", result == "Hello World!", f"got {result!r}")

hztc_path = CACHE_DIR / "hello.html.hztc"
test("hztc file created on disk", hztc_path.exists())
test(
    "hztc file is non-empty",
    hztc_path.stat().st_size > 20 if hztc_path.exists() else False,
)

# ── 2. Cold start from disk cache ──
print("\n── Cold start from disk ──")
# Create a fresh engine (empty LRU) but same template dir (hztc already exists)
engine2 = TemplateEngine(template_dir=str(TEST_DIR))
result2 = engine2.render("hello.html", {"name": "Cache"})
test(
    "cold start from disk cache renders correctly",
    result2 == "Hello Cache!",
    f"got {result2!r}",
)

# ── 3. Source change invalidates disk cache ──
print("\n── Source change invalidation ──")
# write_template stamps a strictly-newer mtime, so the change is observable
# the instant it returns — no sleep needed to outrun timestamp granularity.
write_template("hello.html", "Goodbye {{ name }}!")
engine3 = TemplateEngine(template_dir=str(TEST_DIR))
result3 = engine3.render("hello.html", {"name": "World"})
test(
    "source change triggers recompile", result3 == "Goodbye World!", f"got {result3!r}"
)

# Verify hztc was updated (new content)
engine4 = TemplateEngine(template_dir=str(TEST_DIR))
result4 = engine4.render("hello.html", {"name": "Test"})
test("updated hztc renders new content", result4 == "Goodbye Test!", f"got {result4!r}")

# ── 4. Corrupt .hztc file handled gracefully ──
print("\n── Corrupt cache file recovery ──")
write_template("corrupt.html", "Value: {{ x }}")
engine_c = TemplateEngine(template_dir=str(TEST_DIR))
engine_c.render("corrupt.html", {"x": 1})  # Create cache

corrupt_path = CACHE_DIR / "corrupt.html.hztc"
test("corrupt test cache exists", corrupt_path.exists())

# Write garbage to cache file
corrupt_path.write_bytes(b"GARBAGE DATA NOT A VALID HZTC FILE")

# Fresh engine should recover by recompiling
engine_c2 = TemplateEngine(template_dir=str(TEST_DIR))
result_c = engine_c2.render("corrupt.html", {"x": 42})
test(
    "corrupt cache gracefully falls back to compile",
    result_c == "Value: 42",
    f"got {result_c!r}",
)

# ── 5. Subdirectory templates ──
print("\n── Subdirectory templates ──")
write_template("admin/base.html", "<h1>{{ title }}</h1>")
engine_sub = TemplateEngine(template_dir=str(TEST_DIR))
result_sub = engine_sub.render("admin/base.html", {"title": "Admin"})
test("subdir template renders", result_sub == "<h1>Admin</h1>", f"got {result_sub!r}")

subdir_cache = CACHE_DIR / "admin" / "base.html.hztc"
test("subdir hztc preserves directory structure", subdir_cache.exists())

# Cold start from subdir cache
engine_sub2 = TemplateEngine(template_dir=str(TEST_DIR))
result_sub2 = engine_sub2.render("admin/base.html", {"title": "Cached"})
test(
    "subdir cold start works", result_sub2 == "<h1>Cached</h1>", f"got {result_sub2!r}"
)

# ── 6. bytecode_cache=False disables disk cache ──
print("\n── Disable bytecode cache ──")
teardown()
setup()
write_template("no_cache.html", "{{ x }}")
engine_nc = TemplateEngine(template_dir=str(TEST_DIR), bytecode_cache=False)
result_nc = engine_nc.render("no_cache.html", {"x": "yes"})
test("render works with bytecode_cache=False", result_nc == "yes", f"got {result_nc!r}")

nc_cache = CACHE_DIR / "no_cache.html.hztc"
test("no hztc file when bytecode_cache=False", not nc_cache.exists())

# ── 7. Custom bytecode_cache_dir ──
print("\n── Custom cache directory ──")
custom_dir = TEST_DIR / "_my_cache"
write_template("custom.html", "{{ msg }}")
engine_cd = TemplateEngine(
    template_dir=str(TEST_DIR), bytecode_cache_dir=str(custom_dir)
)
result_cd = engine_cd.render("custom.html", {"msg": "custom"})
test("custom cache dir render works", result_cd == "custom", f"got {result_cd!r}")

custom_cache = custom_dir / "custom.html.hztc"
test("hztc in custom directory", custom_cache.exists())

# ── 8. clear_bytecode_cache() ──
print("\n── clear_bytecode_cache ──")
write_template("a.html", "a")
write_template("b.html", "b")
engine_clr = TemplateEngine(template_dir=str(TEST_DIR))
engine_clr.render("a.html", {})
engine_clr.render("b.html", {})

a_cache = CACHE_DIR / "a.html.hztc"
b_cache = CACHE_DIR / "b.html.hztc"
test(
    "cache files exist before clear",
    a_cache.exists() and b_cache.exists(),
)

count = engine_clr.clear_bytecode_cache()
test("clear returns correct count", count >= 2, f"got {count}")
test(
    "cache files removed after clear",
    not a_cache.exists() and not b_cache.exists(),
)

# Render still works after clear (recompiles)
result_after = engine_clr.render("a.html", {})
test("render works after clear", result_after == "a", f"got {result_after!r}")

# ── 9. Complex template roundtrip through disk cache ──
print("\n── Complex template disk roundtrip ──")
complex_src = """\
{% if items %}
  {% for item in items %}
    {{ loop.index }}. {{ item.name|upper }} - {{ item.value|default('N/A') }}
  {% endfor %}
{% else %}
  Empty
{% endif %}"""
write_template("complex.html", complex_src)

engine_cx = TemplateEngine(template_dir=str(TEST_DIR))
ctx = {"items": [{"name": "alpha", "value": 10}, {"name": "beta"}]}
result_orig = engine_cx.render("complex.html", ctx)

# Cold start from disk
engine_cx2 = TemplateEngine(template_dir=str(TEST_DIR))
result_cached = engine_cx2.render("complex.html", ctx)
test(
    "complex template disk cache roundtrip matches",
    result_orig == result_cached,
    f"orig={result_orig!r}, cached={result_cached!r}",
)

# ── 10. Template with macros through disk cache ──
print("\n── Macro template disk cache ──")
macro_src = '{% macro greet(name) %}Hello {{ name }}!{% endmacro %}{{ greet("World") }}'
write_template("macro.html", macro_src)

engine_m = TemplateEngine(template_dir=str(TEST_DIR))
result_m = engine_m.render("macro.html", {})

engine_m2 = TemplateEngine(template_dir=str(TEST_DIR))
result_m2 = engine_m2.render("macro.html", {})
test(
    "macro template survives disk cache",
    result_m == result_m2,
    f"orig={result_m!r}, cached={result_m2!r}",
)

# ── 11. Custom filters work with disk-cached templates ──
print("\n── Custom filters + disk cache ──")
write_template("filter.html", "{{ x|double }}")
engine_f = TemplateEngine(template_dir=str(TEST_DIR))
engine_f.add_filter("double", lambda v: str(v) * 2)
result_f = engine_f.render("filter.html", {"x": "ha"})
test("custom filter works", result_f == "haha", f"got {result_f!r}")

# Cold start — custom filter must be re-wired to deserialized capsule
engine_f2 = TemplateEngine(template_dir=str(TEST_DIR))
engine_f2.add_filter("double", lambda v: str(v) * 2)
result_f2 = engine_f2.render("filter.html", {"x": "ho"})
test(
    "custom filter works on disk-cached template",
    result_f2 == "hoho",
    f"got {result_f2!r}",
)

# ── 12. auto_reload=False skips mtime check (uses LRU directly) ──
print("\n── auto_reload=False ──")
write_template("reload.html", "v1")
engine_ar = TemplateEngine(template_dir=str(TEST_DIR), auto_reload=False)
result_ar1 = engine_ar.render("reload.html", {})
test("auto_reload=False first render", result_ar1 == "v1", f"got {result_ar1!r}")

# Change source — should still serve cached v1
write_template("reload.html", "v2")
result_ar2 = engine_ar.render("reload.html", {})
test(
    "auto_reload=False serves cached version", result_ar2 == "v1", f"got {result_ar2!r}"
)

# ── 13. FNV-1a hash correctness ──
print("\n── FNV-1a hash ──")
# Known test vectors
test("fnv1a empty string", _fnv1a_64(b"") == 0xCBF29CE484222325)
test("fnv1a 'a'", _fnv1a_64(b"a") == 0xAF63DC4C8601EC8C)
test("fnv1a 'foobar'", _fnv1a_64(b"foobar") == 0x85944171F73967E8)

# ── 14. Multiple templates share same cache dir ──
print("\n── Multiple templates ──")
for i in range(10):
    write_template(f"multi_{i}.html", f"Template {i}: {{{{ x }}}}")
engine_multi = TemplateEngine(template_dir=str(TEST_DIR))
for i in range(10):
    r = engine_multi.render(f"multi_{i}.html", {"x": i})
    test(f"multi template {i}", r == f"Template {i}: {i}", f"got {r!r}")

# All cache files exist
all_cached = all((CACHE_DIR / f"multi_{i}.html.hztc").exists() for i in range(10))
test("all 10 hztc files created", all_cached)

# Cold start all 10
engine_multi2 = TemplateEngine(template_dir=str(TEST_DIR))
all_correct = True
for i in range(10):
    r = engine_multi2.render(f"multi_{i}.html", {"x": i * 10})
    if r != f"Template {i}: {i * 10}":
        all_correct = False
test("all 10 cold-start from disk correct", all_correct)

# ── 15. render_string does NOT create disk cache ──
print("\n── render_string bypass ──")
engine_rs = TemplateEngine(template_dir=str(TEST_DIR))
result_rs = engine_rs.render_string("{{ x + 1 }}", {"x": 5})
test("render_string works", result_rs == "6", f"got {result_rs!r}")
# No new files should appear for string templates
string_caches = (
    [f.name for f in CACHE_DIR.iterdir() if f.name.startswith("__string__")]
    if CACHE_DIR.exists()
    else []
)
test(
    "render_string creates no disk cache",
    len(string_caches) == 0,
    f"found {string_caches}",
)

# ── 16. Namespace template through disk cache ──
print("\n── Namespace through disk cache ──")
ns_src = "{% set ns = namespace(c=0) %}{% for i in [1,2,3] %}{% set ns.c = ns.c + 1 %}{% endfor %}{{ ns.c }}"
write_template("ns.html", ns_src)
engine_ns = TemplateEngine(template_dir=str(TEST_DIR))
r_ns = engine_ns.render("ns.html", {})

engine_ns2 = TemplateEngine(template_dir=str(TEST_DIR))
r_ns2 = engine_ns2.render("ns.html", {})
test(
    "namespace template disk cache roundtrip",
    r_ns == r_ns2 == "3",
    f"orig={r_ns!r}, cached={r_ns2!r}",
)

# ── Performance ──
print("\n── Performance: compile vs disk cache cold start ──")
perf_src = """{% for i in items %}{{ i.name|upper }}: {{ i.value|default('?') }}, {% endfor %}"""
write_template("perf.html", perf_src)

# Warm up: create cache
engine_p = TemplateEngine(template_dir=str(TEST_DIR))
engine_p.render("perf.html", {"items": [{"name": "x", "value": 1}]})

# Measure: compile from source (clear disk cache first)
perf_cache = CACHE_DIR / "perf.html.hztc"
N = 500

# Compile from source
compile_times = []
for _ in range(N):
    if perf_cache.exists():
        perf_cache.unlink()
    e = TemplateEngine(template_dir=str(TEST_DIR), bytecode_cache=False)
    start = time.perf_counter_ns()
    e.render("perf.html", {"items": [{"name": "x"}]})
    compile_times.append(time.perf_counter_ns() - start)
avg_compile = sum(compile_times) / N

# Disk cache hit (fresh engine each time, LRU empty, disk has cache)
engine_warmup = TemplateEngine(template_dir=str(TEST_DIR))
engine_warmup.render("perf.html", {"items": [{"name": "x"}]})  # ensure cache exists

cache_times = []
for _ in range(N):
    e = TemplateEngine(template_dir=str(TEST_DIR))
    start = time.perf_counter_ns()
    e.render("perf.html", {"items": [{"name": "x"}]})
    cache_times.append(time.perf_counter_ns() - start)
avg_cache = sum(cache_times) / N

speedup = avg_compile / avg_cache if avg_cache > 0 else 0
print(f"  compile from source: {avg_compile:,.0f} ns")
print(f"  disk cache cold start: {avg_cache:,.0f} ns")
print(f"  speedup: {speedup:.1f}x")

# Disk cache may not be faster for tiny templates due to file I/O overhead.
# The real win is for larger templates and avoiding parse+compile.
# Just verify it works and isn't catastrophically slow.
# Under parallel execution (247 tests), disk I/O contention lowers speedup significantly.
_min_speedup = 0.1 if os.environ.get("HYPER_TEST_PARALLEL") == "1" else 0.2
test(
    "disk cache performance acceptable",
    speedup >= _min_speedup,
    f"speedup={speedup:.2f}x",
)

# ── Cleanup ──
teardown()

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
