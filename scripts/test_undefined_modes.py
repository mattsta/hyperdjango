"""Tests for StrictUndefined / DebugUndefined behavior in the Zig template engine."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test(
    name: str, template: str, context: dict, expected: str, undefined: str = "silent"
) -> None:
    global passed, failed
    engine = TemplateEngine(undefined=undefined)
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


def test_error(
    name: str,
    template: str,
    context: dict,
    expected_fragment: str,
    undefined: str = "strict",
) -> None:
    global passed, failed
    engine = TemplateEngine(undefined=undefined)
    try:
        result = engine.render_string(template, context)
        # strict mode should raise, but if it doesn't, fail
        print(f"  FAIL: {name} — expected error but got: {result[:80]!r}")
        failed += 1
        errors.append(name)
    except Exception as e:
        if expected_fragment.lower() in str(e).lower():
            print(f"  PASS: {name} (exception: {type(e).__name__})")
            passed += 1
        else:
            print(f"  FAIL: {name} — wrong error: {e}")
            failed += 1
            errors.append(name)


print("=" * 60)
print("TEST: Undefined variable behavior modes")
print("=" * 60)

# ── Silent mode (default) ──
test("silent: missing var renders empty", "Hello {{ name }}!", {}, "Hello !", "silent")

test(
    "silent: defined var renders normally",
    "Hello {{ name }}!",
    {"name": "World"},
    "Hello World!",
    "silent",
)

test("silent: nested missing renders empty", "{{ user.name }}", {}, "", "silent")

test(
    "silent: missing with default filter",
    "{{ name|default('Guest') }}",
    {},
    "Guest",
    "silent",
)

# ── Strict mode ──
test_error("strict: missing var raises error", "Hello {{ name }}!", {}, "undefined")

test(
    "strict: defined var works fine",
    "Hello {{ name }}!",
    {"name": "World"},
    "Hello World!",
    "strict",
)

test(
    "strict: missing with default filter does NOT raise",
    "{{ name|default('Guest') }}",
    {},
    "Guest",
    "strict",
)

test_error("strict: nested missing raises error", "{{ user.profile }}", {}, "undefined")

# ── Debug mode ──
test(
    "debug: missing var shows variable name",
    "Hello {{ name }}!",
    {},
    "Hello {{ name }}!",
    "debug",
)

test(
    "debug: defined var renders normally",
    "Hello {{ name }}!",
    {"name": "World"},
    "Hello World!",
    "debug",
)

test(
    "debug: nested missing shows dot path",
    "{{ user.name }}",
    {},
    "{{ user.name }}",
    "debug",
)

test(
    "debug: multiple missing vars",
    "{{ a }} and {{ b }}",
    {},
    "{{ a }} and {{ b }}",
    "debug",
)

test(
    "debug: missing with default uses default",
    "{{ name|default('Guest') }}",
    {},
    "Guest",
    "debug",
)

# ── Mode doesn't affect conditionals ──
test(
    "silent: missing in if-block is falsy",
    "{% if missing %}yes{% else %}no{% endif %}",
    {},
    "no",
    "silent",
)

test(
    "debug: if-block with missing var",
    "{% if missing %}yes{% else %}no{% endif %}",
    {},
    "no",
    "debug",
)

# ── Mode reset between engines ──
test("mode isolation: silent engine after strict", "{{ x }}", {}, "", "silent")

# ── Performance ──
print("\n── Performance ──")
for mode in ["silent", "strict", "debug"]:
    engine = TemplateEngine(undefined=mode)
    tmpl = "{{ name }} {{ age }}" if mode != "strict" else "{{ name }}"
    ctx = {"name": "Alice"} if mode == "strict" else {"name": "Alice"}

    for _ in range(100):
        engine.render_string(tmpl, ctx)

    start = time.perf_counter_ns()
    N = 10000
    for _ in range(N):
        engine.render_string(tmpl, ctx)
    elapsed = time.perf_counter_ns() - start
    print(f"  {mode:6s} mode: {elapsed / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
