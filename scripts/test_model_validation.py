#!/usr/bin/env python3
"""Test native model-level validation (compile_model_specs + init_model_full)."""

# hyper-test: unit

import time
import traceback

# Ensure hyperdjango is importable
from collections.abc import Callable
from typing import Annotated

from hyperdjango.testkit import check, finish, run_main
from hyperdjango.validation.core.fields import Field
from hyperdjango.validation.core.model import BaseModel
from hyperdjango.validation.core.validator import (
    ValidationError,
    ValidationErrors,
)

# ── Test 1: Basic model with constraints ──────────────────────────────────────


class User(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    age: Annotated[int, Field(ge=0, le=150)]
    score: Annotated[float, Field(ge=0.0, le=100.0)] = 0.0
    active: bool = True


def test_basic_creation() -> None:
    # Valid creation
    u = User(name="Alice", age=25, score=95.5)
    assert u.name == "Alice"
    assert u.age == 25
    assert u.score == 95.5
    assert u.active == True


def test_fields_set_tracking() -> None:
    u = User(name="Alice", age=25, score=95.5)
    assert "name" in u.__pydantic_fields_set__
    assert "age" in u.__pydantic_fields_set__
    assert "score" in u.__pydantic_fields_set__
    assert "active" not in u.__pydantic_fields_set__  # used default


def test_defaults() -> None:
    u2 = User(name="Bob", age=30)
    assert u2.score == 0.0
    assert u2.active == True


# ── Test 2: Validation errors ─────────────────────────────────────────────────


def test_min_length_violation() -> None:
    try:
        User(name="", age=25)  # min_length=1 violation
        assert False, "Should have raised"
    except (ValidationError, ValidationErrors) as e:
        print(f"    MinLength: {e}")


def test_ge_violation() -> None:
    try:
        User(name="Alice", age=-1)  # ge=0 violation
        assert False, "Should have raised"
    except (ValidationError, ValidationErrors) as e:
        print(f"    Ge: {e}")


def test_le_violation() -> None:
    try:
        User(name="Alice", age=200)  # le=150 violation
        assert False, "Should have raised"
    except (ValidationError, ValidationErrors) as e:
        print(f"    Le: {e}")


def test_required_field_missing() -> None:
    try:
        User(age=25)  # name is required
        assert False, "Should have raised"
    except (ValidationError, ValidationErrors) as e:
        print(f"    Required: {e}")


# ── Test 3: Type checking ─────────────────────────────────────────────────────


def test_type_checking_str() -> None:
    try:
        User(name=123, age=25)  # str expected, got int
        assert False, "Should have raised"
    except (ValidationError, ValidationErrors) as e:
        print(f"    Type(str): {e}")


def test_type_checking_int() -> None:
    try:
        User(name="Alice", age="not_an_int")  # int expected, got str
        assert False, "Should have raised"
    except (ValidationError, ValidationErrors) as e:
        print(f"    Type(int): {e}")


# ── Test 4: Float coercion ────────────────────────────────────────────────────


def test_int_to_float_coercion() -> None:
    u3 = User(name="Charlie", age=25, score=50)  # int→float coercion
    assert isinstance(u3.score, float)
    assert u3.score == 50.0


# ── Test 5: String transforms ─────────────────────────────────────────────────


def test_string_transforms() -> None:
    class Normalized(BaseModel):
        email: Annotated[str, Field(to_lower=True, strip_whitespace=True, min_length=1)]

    n = Normalized(email="  ALICE@EXAMPLE.COM  ")
    assert n.email == "alice@example.com"


# ── Test 6: Bool field ────────────────────────────────────────────────────────


def test_bool_fields() -> None:
    class Flags(BaseModel):
        enabled: bool
        visible: bool = False

    f = Flags(enabled=True)
    assert f.enabled == True
    assert f.visible == False


# ── Test 7: Bytes field ───────────────────────────────────────────────────────


def test_bytes_fields() -> None:
    class Data(BaseModel):
        payload: bytes

    d = Data(payload=b"hello")
    assert d.payload == b"hello"


# ── Test 8: Performance benchmark ─────────────────────────────────────────────


def test_creation_benchmark() -> None:
    N = 100_000
    start = time.perf_counter()
    for _ in range(N):
        User(name="Alice", age=25, score=95.5)
    elapsed = time.perf_counter() - start
    rate = N / elapsed
    print(f"\n{'=' * 60}")
    print(f"Performance: {N:,} model creations in {elapsed:.3f}s")
    print(f"Rate: {rate:,.0f} models/sec ({elapsed / N * 1e6:.1f} μs/model)")
    print(f"Ultra-fast: {User.__dhi_use_ultra_fast__}")
    print(f"{'=' * 60}")


def main() -> bool:
    print(f"\nUser.__dhi_use_ultra_fast__ = {User.__dhi_use_ultra_fast__}")
    print(f"User.__dhi_full_native__ = {User.__dhi_full_native__}")
    print(f"User.__dhi_compiled_specs__ = {User.__dhi_compiled_specs__}")

    tests: tuple[Callable[[], None], ...] = (
        test_basic_creation,
        test_fields_set_tracking,
        test_defaults,
        test_min_length_violation,
        test_ge_violation,
        test_le_violation,
        test_required_field_missing,
        test_type_checking_str,
        test_type_checking_int,
        test_int_to_float_coercion,
        test_string_transforms,
        test_bool_fields,
        test_bytes_fields,
        test_creation_benchmark,
    )
    # Bare asserts abort the file on the first break — that is this suite's
    # contract; the counts are emitted before bailing out.
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
        check(fn.__name__, True)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
