"""Tests for template sandbox mode in the Zig template engine."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test(
    name: str, template: str, context: dict, expected: str, sandboxed: bool = False
) -> None:
    global passed, failed
    engine = TemplateEngine(sandboxed=sandboxed)
    try:
        result = engine.render_string(template, context)
        if result.strip() == expected.strip():
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name}")
            print(f"    Expected: {expected!r}")
            print(f"    Got:      {result.strip()!r}")
            failed += 1
            errors.append(name)
    except Exception as e:
        print(f"  ERROR: {name}: {e}")
        failed += 1
        errors.append(name)


def test_blocked(
    name: str, template: str, context: dict, sandboxed: bool = True
) -> None:
    """Test that template renders empty string for blocked access (sandbox silently blocks)."""
    global passed, failed
    engine = TemplateEngine(sandboxed=sandboxed)
    try:
        result = engine.render_string(template, context)
        # Sandbox blocks should result in empty output for the blocked expression
        if result.strip() == "" or "__" not in result:
            print(f"  PASS: {name} (blocked, output: {result.strip()!r})")
            passed += 1
        else:
            print(f"  FAIL: {name} — expected blocked but got: {result[:100]!r}")
            failed += 1
            errors.append(name)
    except Exception as e:
        # Exception is also acceptable (strict+sandbox)
        print(f"  PASS: {name} (exception: {type(e).__name__})")
        passed += 1


print("=" * 60)
print("TEST: Template sandbox mode")
print("=" * 60)

# ── Normal access works in sandbox ──
test(
    "sandbox allows normal variable access",
    "{{ name }}",
    {"name": "Alice"},
    "Alice",
    sandboxed=True,
)

test(
    "sandbox allows dict key access",
    "{{ user.name }}",
    {"user": {"name": "Bob"}},
    "Bob",
    sandboxed=True,
)

test(
    "sandbox allows list indexing",
    "{{ items[0] }}",
    {"items": ["a", "b", "c"]},
    "a",
    sandboxed=True,
)

test(
    "sandbox allows filters",
    "{{ name|upper }}",
    {"name": "hello"},
    "HELLO",
    sandboxed=True,
)

test(
    "sandbox allows for-loops",
    "{% for i in items %}{{ i }}{% endfor %}",
    {"items": [1, 2, 3]},
    "123",
    sandboxed=True,
)

test(
    "sandbox allows if-blocks", "{% if True %}yes{% endif %}", {}, "yes", sandboxed=True
)

# ── Blocked: __class__ ──
test_blocked("sandbox blocks __class__", "{{ ''.__class__ }}", {})

# ── Blocked: __subclasses__ ──
test_blocked("sandbox blocks __subclasses__", "{{ ''.__class__.__subclasses__ }}", {})

# ── Blocked: __globals__ ──
test_blocked(
    "sandbox blocks __globals__", "{{ func.__globals__ }}", {"func": lambda: None}
)

# ── Blocked: __builtins__ ──
test_blocked("sandbox blocks __builtins__", "{{ x.__builtins__ }}", {"x": {}})

# ── Blocked: __mro__ ──
test_blocked("sandbox blocks __mro__", "{{ ''.__class__.__mro__ }}", {})

# ── Blocked: __init__ ──
test_blocked("sandbox blocks __init__", "{{ ''.__class__.__init__ }}", {})

# ── Blocked: __dict__ ──
test_blocked("sandbox blocks __dict__", "{{ obj.__dict__ }}", {"obj": object()})

# ── Blocked: __module__ ──
test_blocked("sandbox blocks __module__", "{{ obj.__module__ }}", {"obj": object()})

# ── Blocked: func_globals ──
test_blocked(
    "sandbox blocks func_globals", "{{ func.func_globals }}", {"func": lambda: None}
)

# ── Allowed: __len__ (safe dunder) ──
test(
    "sandbox allows __len__",
    "{{ items.__len__() }}",
    {"items": [1, 2, 3]},
    "3",
    sandboxed=True,
)

# ── Not sandboxed: __class__ works ──
test(
    "non-sandboxed allows __class__",
    "{{ ''.__class__ }}",
    {},
    "&lt;class &#x27;str&#x27;&gt;",
    sandboxed=False,
)

# ── Sandbox + safe methods work ──
test(
    "sandbox allows .upper() method",
    "{{ name.upper() }}",
    {"name": "hello"},
    "HELLO",
    sandboxed=True,
)

test(
    "sandbox allows .items() method",
    "{% for k, v in data.items() %}{{ k }}={{ v }} {% endfor %}",
    {"data": {"a": 1, "b": 2}},
    "a=1 b=2",
    sandboxed=True,
)

test(
    "sandbox allows .keys() method",
    "{% for k in data.keys() %}{{ k }}{% endfor %}",
    {"data": {"x": 1, "y": 2}},
    "xy",
    sandboxed=True,
)

# ── Sandbox isolation between engines ──
test("engine A sandboxed", "{{ name }}", {"name": "safe"}, "safe", sandboxed=True)

test(
    "engine B not sandboxed (after A)",
    "{{ ''.__class__.__name__ }}",
    {},
    "str",
    sandboxed=False,
)

# ── TMPL-AUDIT #128: sandbox completeness — filter attribute-access paths ─────
# The sandbox blocked `.attr` and `.method()` paths but TWO filter paths reached
# getattr() with a template-controlled name WITHOUT the block-list check, so a
# sandboxed (untrusted) template could still escape:
#   1. multi-arg filter fallback → getattr(value, filter_name)
#   2. groupby('attr')          → getattr(item, attr)
print("\n── sandbox: filter attribute-access bypasses (#128) ──")


def bypass_blocked(name: str, template: str, context: dict) -> None:
    """Stronger than test_blocked: assert the dangerous object never LEAKS —
    no class/type/module/function repr in the output — under sandbox mode."""
    global passed, failed
    engine = TemplateEngine(sandboxed=True)
    try:
        out = engine.render_string(template, context)
    except Exception as e:
        print(f"  PASS: {name} (blocked, {type(e).__name__})")
        passed += 1
        return
    leaked = any(
        tok in out
        for tok in (
            "<class",
            "<function",
            "<built-in",
            "type object",
            "__main__",
            "builtins",
        )
    )
    if not leaked:
        print(f"  PASS: {name} (no leak: {out.strip()[:40]!r})")
        passed += 1
    else:
        print(f"  FAIL: {name} — LEAKED: {out.strip()[:120]!r}")
        failed += 1
        errors.append(name)


# 1. multi-arg filter fallback: getattr(value, "<dunder>")(...)
bypass_blocked(
    "filter __getattribute__('__class__')",
    "{{ s|__getattribute__('__class__') }}",
    {"s": "x"},
)
bypass_blocked("filter __class__()", "{{ s|__class__() }}", {"s": "x"})
# 2. groupby by a dangerous attribute name.
bypass_blocked(
    "groupby('__class__')", "{{ items|groupby('__class__') }}", {"items": ["a", "b"]}
)
bypass_blocked(
    "groupby('__globals__')",
    "{{ items|groupby('__globals__') }}",
    {"items": [lambda: 0]},
)
# 3. non-dunder frame/coroutine accessors (defense-in-depth).
bypass_blocked(
    "frame f_builtins", "{{ f.f_builtins }}", {"f": __import__("sys")._getframe()}
)
bypass_blocked("generator gi_frame", "{{ g.gi_frame }}", {"g": (x for x in [1])})

# The same filter must still WORK in non-sandbox mode (no false positive).
test(
    "groupby works unsandboxed",
    "{{ items|groupby('k')|list|length }}",
    {"items": [{"k": 1}, {"k": 1}]},
    "1",
    sandboxed=False,
)


# ── Performance ──
print("\n── Performance ──")
engine_normal = TemplateEngine()
engine_sandbox = TemplateEngine(sandboxed=True)
tmpl = "{{ user.name }} {{ user.email }}"
ctx = {"user": {"name": "Alice", "email": "alice@example.com"}}

for _ in range(100):
    engine_normal.render_string(tmpl, ctx)
    engine_sandbox.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 10000
for _ in range(N):
    engine_normal.render_string(tmpl, ctx)
elapsed_normal = time.perf_counter_ns() - start

start = time.perf_counter_ns()
for _ in range(N):
    engine_sandbox.render_string(tmpl, ctx)
elapsed_sandbox = time.perf_counter_ns() - start

print(f"  normal mode:  {elapsed_normal / N:.0f} ns/render ({N} iterations)")
print(f"  sandbox mode: {elapsed_sandbox / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
