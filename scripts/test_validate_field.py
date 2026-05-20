#!/usr/bin/env python3
"""Test validate_field and dump_model_compiled native functions."""

# hyper-test: unit

import time
from typing import Annotated

from hyperdjango import _hyperdjango_native as native
from hyperdjango.testkit import check, finish, run_main
from hyperdjango.validation.core.fields import Field
from hyperdjango.validation.core.model import BaseModel

# int with ge=0, le=150
int_constraints = (1, 0, None, 0, None, 150, None, None, None, 1, 0, 0, 0, 0)
# str with min=1, max=50, strip+to_lower
str_constraints = (3, 0, None, None, None, None, None, 1, 50, 1, 0, 1, 1, 0)
# float with ge=0.0, le=100.0
float_constraints = (2, 0, None, 0.0, None, 100.0, None, None, None, 0, 0, 0, 0, 0)
bool_constraints = (4, 0, None, None, None, None, None, None, None, 1, 0, 0, 0, 0)
bytes_constraints = (5, 0, None, None, None, None, None, None, None, 1, 0, 0, 0, 0)


def _rejects(value: object, name: str, constraints: tuple) -> str:
    """Return the ValueError message when the field rejects ``value``, else ""."""
    try:
        native.validate_field(value, name, constraints)
    except ValueError as e:
        return str(e)
    return ""


class User(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    age: Annotated[int, Field(ge=0, le=150)]
    score: float = 0.0


class Address(BaseModel):
    city: str
    zip_code: str


class Person(BaseModel):
    name: str
    address: Address


# ── validate_field tests ──────────────────────────────────────────────────────


def test_validate_field() -> None:
    check(
        "validate_field int valid",
        native.validate_field(25, "age", int_constraints) == 25,
    )

    ge_err = _rejects(-1, "age", int_constraints)
    check(f"validate_field int ge violation: {ge_err}", bool(ge_err))

    le_err = _rejects(200, "age", int_constraints)
    check(f"validate_field int le violation: {le_err}", bool(le_err))

    type_err = _rejects("not_int", "age", int_constraints)
    check(f"validate_field int type error: {type_err}", bool(type_err))

    stripped = native.validate_field("  HELLO  ", "name", str_constraints)
    check(
        "validate_field str strip+to_lower",
        stripped == "hello",
        f"Expected 'hello', got '{stripped}'",
    )

    min_err = _rejects("", "name", str_constraints)
    check(f"validate_field str min_length: {min_err}", bool(min_err))

    check(
        "validate_field float valid",
        native.validate_field(50.5, "score", float_constraints) == 50.5,
    )

    coerced = native.validate_field(50, "score", float_constraints)
    check(
        "validate_field int→float coercion",
        isinstance(coerced, float) and coerced == 50.0,
    )

    check(
        "validate_field bool valid",
        native.validate_field(True, "active", bool_constraints) is True,
    )

    check(
        "validate_field bytes valid",
        native.validate_field(b"data", "payload", bytes_constraints) == b"data",
    )


def bench_validate_field() -> None:
    n = 500_000
    start = time.perf_counter()
    for _ in range(n):
        native.validate_field(25, "age", int_constraints)
    elapsed = time.perf_counter() - start
    print(f"\nvalidate_field: {n:,} calls in {elapsed:.3f}s = {n / elapsed:,.0f}/sec")


# ── dump_model_compiled tests ─────────────────────────────────────────────────


def test_dump_model_compiled() -> None:
    print()
    u = User(name="Alice", age=25, score=95.5)
    d = u.model_dump()
    check(
        "model_dump basic",
        d == {"name": "Alice", "age": 25, "score": 95.5},
        f"Got: {d}",
    )

    # Test with defaults
    u2 = User(name="Bob", age=30)
    d2 = u2.model_dump()
    check(
        "model_dump with defaults",
        d2 == {"name": "Bob", "age": 30, "score": 0.0},
        f"Got: {d2}",
    )

    # Nested model test
    p = Person(name="Charlie", address=Address(city="NYC", zip_code="10001"))
    pd = p.model_dump()
    check(
        "model_dump nested model",
        pd == {"name": "Charlie", "address": {"city": "NYC", "zip_code": "10001"}},
        f"Got: {pd}",
    )


def bench_model_dump() -> None:
    u = User(name="Alice", age=25, score=95.5)
    n = 200_000
    start = time.perf_counter()
    for _ in range(n):
        u.model_dump()
    elapsed = time.perf_counter() - start
    print(f"\nmodel_dump: {n:,} calls in {elapsed:.3f}s = {n / elapsed:,.0f}/sec")


def main() -> bool:
    test_validate_field()
    bench_validate_field()
    test_dump_model_compiled()
    bench_model_dump()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
