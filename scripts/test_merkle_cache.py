"""Tests for Merkle dependency hash in bytecode cache.

Validates that changing ANY file in the dependency tree (includes, extends,
imports) correctly invalidates the parent template's bytecode cache.
"""

# hyper-test: unit

import atexit
import os
import shutil
import sys
import time
from pathlib import Path

from hyperdjango.templating import TemplateEngine, _BytecodeMeta, _fnv1a_64

passed = 0
failed = 0
errors: list[str] = []

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = PROJECT_ROOT / ".test_scratch" / "merkle_templates"
CACHE_DIR = TEST_DIR / "__pycache__" / "hztc"


def setup():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True, exist_ok=True)


def teardown():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)


# Guarantee teardown even if a test below raises an uncaught exception —
# this script is procedural at module load, not wrapped in a main() function.
atexit.register(teardown)


def write_template(name: str, content: str) -> None:
    """Write a template, guaranteeing an mtime strictly newer than the previous.

    Every rewrite in this file used to be followed by ``time.sleep(0.01)``. That
    sleep was standing in for a condition nobody stated: "the rewrite carries a
    distinct modification stamp". The Merkle disk cache validates by CONTENT
    hash so it never needed it, but the engine's inclusion cache is mtime-keyed,
    and a filesystem with coarse mtime resolution can stamp a rewrite with the
    same value as the original. Establish the condition explicitly here — the
    result is then identical on every machine, and no test has to guess how long
    a clock tick is.
    """
    path = TEST_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat().st_mtime if path.exists() else None
    path.write_text(content, encoding="utf-8")
    if previous is not None and path.stat().st_mtime <= previous:
        bumped = previous + 1.0
        os.utime(path, (bumped, bumped))


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
print("TEST: Merkle dependency hash for bytecode cache")
print("=" * 60)

# ── 1. _BytecodeMeta data structure ──
print("\n── _BytecodeMeta ──")

meta = _BytecodeMeta.build(
    b"main source",
    {
        "partial.html": b"partial source",
        "header.html": b"header source",
    },
)
test("meta has main_hash", meta.main_hash == _fnv1a_64(b"main source"))
test("meta has 2 deps", len(meta.dep_hashes) == 2)
test("meta merkle differs from main", meta.merkle_hash != meta.main_hash)

# Merkle changes when dep changes
meta2 = _BytecodeMeta.build(
    b"main source",
    {
        "partial.html": b"DIFFERENT partial",
        "header.html": b"header source",
    },
)
test("merkle changes when dep changes", meta2.merkle_hash != meta.merkle_hash)

# Merkle identical for identical content
meta3 = _BytecodeMeta.build(
    b"main source",
    {
        "partial.html": b"partial source",
        "header.html": b"header source",
    },
)
test("merkle deterministic for same content", meta3.merkle_hash == meta.merkle_hash)

# JSON roundtrip
json_str = meta.to_json()
meta_rt = _BytecodeMeta.from_json(json_str)
test("json roundtrip preserves main_hash", meta_rt.main_hash == meta.main_hash)
test("json roundtrip preserves dep_hashes", meta_rt.dep_hashes == meta.dep_hashes)
test("json roundtrip preserves merkle_hash", meta_rt.merkle_hash == meta.merkle_hash)

# Validate deps
test(
    "validate_deps passes for matching content",
    meta.validate_deps(
        lambda p: {
            "partial.html": b"partial source",
            "header.html": b"header source",
        }.get(p)
    ),
)

test(
    "validate_deps fails for changed content",
    not meta.validate_deps(
        lambda p: {
            "partial.html": b"CHANGED",
            "header.html": b"header source",
        }.get(p)
    ),
)

test(
    "validate_deps fails for missing dep",
    not meta.validate_deps(
        lambda p: {
            "header.html": b"header source",
        }.get(p)
    ),
)

# ── 2. Include: partial change invalidates parent cache ──
print("\n── Include: partial change invalidates cache ──")
setup()
write_template("_partial.html", "v1")
write_template("page.html", '{% include "_partial.html" %}')

engine = TemplateEngine(template_dir=str(TEST_DIR))
r1 = engine.render("page.html", {})
test("first render v1", r1 == "v1", f"got {r1!r}")

# Verify .hztc and .meta exist
hztc = CACHE_DIR / "page.html.hztc"
meta_file = CACHE_DIR / "page.html.hztc.meta"
test("hztc file created", hztc.exists())
test("meta file created", meta_file.exists())

# Change partial, create fresh engine
write_template("_partial.html", "v2")
engine2 = TemplateEngine(template_dir=str(TEST_DIR))
r2 = engine2.render("page.html", {})
test("partial change detected — renders v2", r2 == "v2", f"got {r2!r}")

# ── 3. Extends: parent change invalidates child cache ──
print("\n── Extends: parent change invalidates cache ──")
setup()
write_template("base.html", "HEADER {% block content %}{% endblock %} FOOTER_V1")
write_template(
    "child.html", '{% extends "base.html" %}{% block content %}BODY{% endblock %}'
)

engine3 = TemplateEngine(template_dir=str(TEST_DIR))
r3 = engine3.render("child.html", {})
test("extends first render", "FOOTER_V1" in r3, f"got {r3!r}")

# Change base, fresh engine
write_template("base.html", "HEADER {% block content %}{% endblock %} FOOTER_V2")
engine4 = TemplateEngine(template_dir=str(TEST_DIR))
r4 = engine4.render("child.html", {})
test("base change detected — renders v2", "FOOTER_V2" in r4, f"got {r4!r}")

# ── 4. Same main source, different partial (the original bug) ──
print("\n── Same main source, different partial ──")
setup()

# Test A: include with content A
write_template("partial.html", "Content A")
write_template("main.html", '{% include "partial.html" %}')
engine_a = TemplateEngine(template_dir=str(TEST_DIR))
ra = engine_a.render("main.html", {})
test("test A renders content A", ra == "Content A", f"got {ra!r}")

# Test B: same main source, different partial content
write_template("partial.html", "Content B")
engine_b = TemplateEngine(template_dir=str(TEST_DIR))
rb = engine_b.render("main.html", {})
test("test B renders content B (not stale A)", rb == "Content B", f"got {rb!r}")

# ── 5. Without context + partial change ──
print("\n── Without context + partial change ──")
setup()
write_template("partial.html", "Hello {{ name }}!")
write_template("main.html", '{% include "partial.html" without context %}')

engine5a = TemplateEngine(template_dir=str(TEST_DIR))
r5a = engine5a.render("main.html", {"name": "World"})
test("without context hides parent vars", r5a == "Hello !", f"got {r5a!r}")

# Change partial to something with no vars
write_template("partial.html", "{{ x }}{{ y }}{{ z }}")
engine5b = TemplateEngine(template_dir=str(TEST_DIR))
r5b = engine5b.render("main.html", {"x": "A", "y": "B", "z": "C"})
test("partial change + without context = empty output", r5b == "", f"got {r5b!r}")

# ── 6. Transitive deps: A includes B includes C ──
print("\n── Transitive dependency chain ──")
setup()
write_template("c.html", "C_V1")
write_template("b.html", '{% include "c.html" %}')
write_template("a.html", '{% include "b.html" %}')

engine6 = TemplateEngine(template_dir=str(TEST_DIR))
r6 = engine6.render("a.html", {})
test("transitive chain renders", r6 == "C_V1", f"got {r6!r}")

# Change deepest dep
write_template("c.html", "C_V2")
engine6b = TemplateEngine(template_dir=str(TEST_DIR))
r6b = engine6b.render("a.html", {})
test("deepest dep change invalidates chain", r6b == "C_V2", f"got {r6b!r}")

# ── 7. Meta file records correct dependencies ──
print("\n── Meta file content verification ──")
setup()
write_template("header.html", "H")
write_template("footer.html", "F")
write_template(
    "layout.html", '{% include "header.html" %}BODY{% include "footer.html" %}'
)

engine7 = TemplateEngine(template_dir=str(TEST_DIR))
r7 = engine7.render("layout.html", {})
test("multi-include renders", r7 == "HBODYF", f"got {r7!r}")

meta_path = CACHE_DIR / "layout.html.hztc.meta"
test("meta file exists", meta_path.exists())

if meta_path.exists():
    meta_loaded = _BytecodeMeta.from_json(meta_path.read_text())
    test("meta has 2 deps", len(meta_loaded.dep_hashes) == 2)
    test("meta tracks header.html", "header.html" in meta_loaded.dep_hashes)
    test("meta tracks footer.html", "footer.html" in meta_loaded.dep_hashes)

# ── 8. Corrupt meta file → recompile ──
print("\n── Corrupt meta recovery ──")
if meta_path.exists():
    meta_path.write_text("NOT VALID JSON")

engine8 = TemplateEngine(template_dir=str(TEST_DIR))
r8 = engine8.render("layout.html", {})
test("corrupt meta → graceful recompile", r8 == "HBODYF", f"got {r8!r}")

# ── 9. No deps (standalone template) ──
print("\n── Standalone template (no deps) ──")
setup()
write_template("simple.html", "Just {{ x }}")
engine9 = TemplateEngine(template_dir=str(TEST_DIR))
r9 = engine9.render("simple.html", {"x": "text"})
test("standalone renders", r9 == "Just text", f"got {r9!r}")

simple_meta = CACHE_DIR / "simple.html.hztc.meta"
if simple_meta.exists():
    sm = _BytecodeMeta.from_json(simple_meta.read_text())
    test("standalone meta has 0 deps", len(sm.dep_hashes) == 0)

# Cold start from disk
engine9b = TemplateEngine(template_dir=str(TEST_DIR))
r9b = engine9b.render("simple.html", {"x": "cached"})
test("standalone disk cache cold start", r9b == "Just cached", f"got {r9b!r}")

# ── 10. Include with bindings + dependency tracking ──
print("\n── Include bindings + Merkle ──")
setup()
write_template("_card.html", "{{ title }}: {{ desc }}")
write_template("page.html", '{% include "_card.html" with title="Test", desc="OK" %}')

engine10 = TemplateEngine(template_dir=str(TEST_DIR))
r10 = engine10.render("page.html", {})
test("bindings render", r10 == "Test: OK", f"got {r10!r}")

# Change partial
write_template("_card.html", "[{{ title }}] {{ desc }}")
engine10b = TemplateEngine(template_dir=str(TEST_DIR))
r10b = engine10b.render("page.html", {})
test("binding + partial change detected", r10b == "[Test] OK", f"got {r10b!r}")

# ── Performance ──
print("\n── Merkle hash performance ──")
# Build meta for a template with 10 deps
dep_sources = {f"dep_{i}.html": f"content {i}".encode() for i in range(10)}
N = 10000
start = time.perf_counter_ns()
for _ in range(N):
    _BytecodeMeta.build(b"main source", dep_sources)
ns_per = (time.perf_counter_ns() - start) / N
print(f"  build meta (10 deps): {ns_per:,.0f} ns")

# Validate deps
meta_perf = _BytecodeMeta.build(b"main source", dep_sources)
start = time.perf_counter_ns()
for _ in range(N):
    meta_perf.validate_deps(lambda p: dep_sources.get(p))
ns_validate = (time.perf_counter_ns() - start) / N
print(f"  validate deps (10 deps): {ns_validate:,.0f} ns")

teardown()

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
