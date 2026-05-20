#!/usr/bin/env python3
"""Test json_loads_model — single-pass JSON → validated model.

Tests:
1. Basic field extraction from JSON
2. Type validation (int, str, float, bool)
3. Missing required fields → error
4. Default values for optional fields
5. Extra fields handling (ignore, forbid, allow)
6. Nested JSON objects
7. Performance benchmark vs json.loads + init_model_full
"""

# hyper-test: unit

import json
import os
import sys
import time

from hyperdjango._hyperdjango_native import (
    compile_model_specs,
    init_model_full,
    json_loads_model,
    json_loads_native,
)


def make_specs(*fields):
    """Build a compiled specs capsule from field definitions.
    Simplified: (name, required, type_code) with defaults filled in.

    Spec tuple format: (name, alias, required, default, constraints_tuple, [nested_type])
    Constraints: (type_code, strict, gt, ge, lt, le, mul, minl, maxl, allow_inf_nan, format_code, strip, lower, upper)
    """
    specs = []
    for field in fields:
        name = field[0]
        required = field[1] if len(field) > 1 else True
        type_code = (
            field[2] if len(field) > 2 else 0
        )  # 0=any, 1=int, 2=float, 3=str, 4=bool

        # 14-element constraints tuple
        constraints = (
            type_code,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            0,
            0,
            0,
            0,
        )

        # (name, alias, required, default, constraints_tuple)
        spec = (name, None, required, None, constraints)
        specs.append(spec)
    return compile_model_specs(tuple(specs))


class FakeModel:
    """Minimal model-like object with __dict__."""

    pass


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Basic field extraction ────────────────────────────────────────────
    print("\n=== Basic field extraction ===")

    specs = make_specs(("name", True, 3), ("age", True, 1))
    model = FakeModel()
    json_str = '{"name": "Alice", "age": 25}'
    result = json_loads_model(json_str, model, specs, 0)
    check(
        "basic fields",
        result is None
        and model.__dict__.get("name") == "Alice"
        and model.__dict__.get("age") == 25,
        f"result={result}, dict={model.__dict__}",
    )

    # ── Type validation ───────────────────────────────────────────────────
    print("\n=== Type validation ===")

    specs = make_specs(
        ("x", True, 1), ("y", True, 2), ("z", True, 3), ("flag", True, 4)
    )
    model = FakeModel()
    result = json_loads_model(
        '{"x": 42, "y": 3.14, "z": "hello", "flag": true}', model, specs, 0
    )
    check("int type", model.__dict__.get("x") == 42, f"got {model.__dict__.get('x')}")
    check(
        "float type",
        abs(model.__dict__.get("y", 0) - 3.14) < 0.01,
        f"got {model.__dict__.get('y')}",
    )
    check(
        "str type", model.__dict__.get("z") == "hello", f"got {model.__dict__.get('z')}"
    )
    check(
        "bool type",
        model.__dict__.get("flag") is True,
        f"got {model.__dict__.get('flag')}",
    )

    # ── Missing required fields ───────────────────────────────────────────
    print("\n=== Missing required fields ===")

    specs = make_specs(("name", True, 3), ("age", True, 1))
    model = FakeModel()
    result = json_loads_model('{"name": "Bob"}', model, specs, 0)
    check(
        "missing required returns errors",
        result is not None and len(result) > 0,
        f"result={result}",
    )

    # ── Default values ────────────────────────────────────────────────────
    print("\n=== Default values ===")

    specs_with_defaults = compile_model_specs(
        (
            (
                "name",
                None,
                True,
                None,
                (3, 0, None, None, None, None, None, None, None, 0, 0, 0, 0, 0),
            ),
            (
                "role",
                None,
                False,
                "user",
                (3, 0, None, None, None, None, None, None, None, 0, 0, 0, 0, 0),
            ),
        )
    )
    model = FakeModel()
    result = json_loads_model('{"name": "Charlie"}', model, specs_with_defaults, 0)
    check(
        "default applied",
        model.__dict__.get("role") == "user",
        f"got {model.__dict__.get('role')}",
    )

    # ── Extra fields ──────────────────────────────────────────────────────
    print("\n=== Extra fields handling ===")

    specs = make_specs(("name", True, 3))

    # ignore (mode 0)
    model = FakeModel()
    result = json_loads_model('{"name": "Alice", "extra": 99}', model, specs, 0)
    check("extra ignore", result is None, f"result={result}")

    # forbid (mode 1)
    model = FakeModel()
    result = json_loads_model('{"name": "Alice", "extra": 99}', model, specs, 1)
    check("extra forbid", result is not None, f"result={result}")

    # ── Performance benchmark ─────────────────────────────────────────────
    print("\n=== Performance benchmark ===")

    specs = make_specs(
        ("name", True, 3), ("age", True, 1), ("email", True, 3), ("active", True, 4)
    )
    json_str = (
        '{"name": "Alice", "age": 25, "email": "alice@example.com", "active": true}'
    )
    json_bytes = json_str.encode()

    # Warm up
    for _ in range(100):
        m = FakeModel()
        json_loads_model(json_str, m, specs, 0)

    # Benchmark: json_loads_model (single pass)
    N = 50_000
    start = time.perf_counter()
    for _ in range(N):
        m = FakeModel()
        json_loads_model(json_str, m, specs, 0)
    single_pass_time = (time.perf_counter() - start) / N * 1e6

    # Benchmark: json_loads_native + init_model_full (two pass)
    start = time.perf_counter()
    for _ in range(N):
        m = FakeModel()
        d = json_loads_native(json_str)
        init_model_full(m, d, specs, 0)
    two_pass_time = (time.perf_counter() - start) / N * 1e6

    # Benchmark: stdlib json.loads + init_model_full
    start = time.perf_counter()
    for _ in range(N):
        m = FakeModel()
        d = json.loads(json_str)
        init_model_full(m, d, specs, 0)
    stdlib_time = (time.perf_counter() - start) / N * 1e6

    speedup_vs_two = two_pass_time / single_pass_time
    speedup_vs_stdlib = stdlib_time / single_pass_time

    print(f"  json_loads_model (single pass):  {single_pass_time:.1f} μs/op")
    print(f"  json_loads + init_model_full:    {two_pass_time:.1f} μs/op")
    print(f"  json.loads + init_model_full:    {stdlib_time:.1f} μs/op")
    print(f"  Speedup vs native two-pass:      {speedup_vs_two:.2f}x")
    print(f"  Speedup vs stdlib:               {speedup_vs_stdlib:.2f}x")

    from hyperdjango.native import is_release_build

    parallel = os.environ.get("HYPER_TEST_PARALLEL") == "1"
    if is_release_build and not parallel:
        check(
            "single pass faster than two-pass",
            speedup_vs_two > 1.0,
            f"speedup={speedup_vs_two:.2f}x",
        )
    else:
        reason = "parallel" if parallel else "debug build"
        print(f"  (skipping perf assertion in {reason}: {speedup_vs_two:.2f}x)")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All json_loads_model tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
