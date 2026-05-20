#!/usr/bin/env python3
"""Test SIMD batch validation end-to-end."""

# hyper-test: unit

import time
import traceback
from typing import Annotated

from hyperdjango import _hyperdjango_native as native
from hyperdjango.testkit import check, finish, run_main
from hyperdjango.validation.core.batch import (
    validate_emails_batch,
    validate_ints_batch,
    validate_model_batch,
    validate_strings_batch,
    validate_users_batch,
)
from hyperdjango.validation.core.fields import Field
from hyperdjango.validation.core.model import BaseModel

# Capability report only — every validate_*_batch has a pure-Python fallback, so
# a missing native symbol changes the path taken, not the expected results.
print(f"validate_int_batch_simd: {hasattr(native, 'validate_int_batch_simd')}")
print(f"validate_batch_direct: {hasattr(native, 'validate_batch_direct')}")
print(f"validate_model_batch: {hasattr(native, 'validate_model_batch')}")


class User(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    age: Annotated[int, Field(ge=0, le=150)]
    score: float = 0.0


# ── validate_ints_batch ───────────────────────────────────────────────────────


def test_ints_batch() -> None:
    values = [25, 30, 150, 18, 90, -5, 0, 120, 121, 17]
    result = validate_ints_batch(values, 18, 120)
    assert result.total_count == 10
    assert result.results == [
        True,
        True,
        False,
        True,
        True,
        False,
        False,
        True,
        False,
        False,
    ]
    assert result.valid_count == 5
    assert result.invalid_count == 5
    assert result.get_invalid_indices() == [2, 5, 6, 8, 9]
    check("validate_ints_batch works", True)

    # Edge case: empty
    result = validate_ints_batch([], 0, 100)
    assert result.is_all_valid()
    assert result.total_count == 0
    check("validate_ints_batch empty", True)


# ── validate_strings_batch ────────────────────────────────────────────────────


def test_strings_batch() -> None:
    strings = ["hello", "", "a" * 101, "ok", "x"]
    result = validate_strings_batch(strings, 1, 100)
    assert result.results == [True, False, False, True, True]
    assert result.valid_count == 3
    check("validate_strings_batch works", True)


# ── validate_emails_batch ─────────────────────────────────────────────────────


def test_emails_batch() -> None:
    emails = [
        "alice@example.com",
        "bob@test.org",
        "invalid",
        "no-at-sign",
        "has@dot.co",
        "@nodomain",
        "trailing@.",
    ]
    result = validate_emails_batch(emails)
    assert result.results[0] == True  # alice@example.com
    assert result.results[1] == True  # bob@test.org
    assert result.results[2] == False  # invalid
    assert result.results[3] == False  # no-at-sign
    assert result.results[4] == True  # has@dot.co
    assert result.results[5] == False  # @nodomain
    check("validate_emails_batch works", True)


# ── validate_users_batch ──────────────────────────────────────────────────────


def test_users_batch() -> None:
    users = [
        {"name": "Alice", "email": "alice@example.com", "age": 25},
        {"name": "Bob", "email": "bob@test.org", "age": 30},
        {"name": "", "email": "empty@name.com", "age": 25},  # invalid: empty name
        {"name": "Charlie", "email": "invalid-email", "age": 25},  # invalid: bad email
        {"name": "Dave", "email": "dave@ok.com", "age": 15},  # invalid: too young
        {"name": "Eve", "email": "eve@ok.com", "age": 130},  # invalid: too old
    ]
    result = validate_users_batch(users)
    assert result.results == [True, True, False, False, False, False]
    assert result.valid_count == 2
    check("validate_users_batch works", True)


# ── validate_model_batch ──────────────────────────────────────────────────────


def test_model_batch() -> None:
    data = [
        {"name": "Alice", "age": 25, "score": 95.5},
        {"name": "Bob", "age": 30},
        {"name": "", "age": 25},  # invalid: empty name
        {"name": "Charlie", "age": -1},  # invalid: negative age
        {"name": "Dave", "age": 200},  # invalid: age > 150
    ]
    results = validate_model_batch(data, User)
    assert results[0] is None, f"Expected None, got {results[0]}"  # valid
    assert results[1] is None, f"Expected None, got {results[1]}"  # valid (default)
    assert results[2] is not None  # error: empty name
    assert results[3] is not None  # error: negative age
    assert results[4] is not None  # error: age > 150
    check("validate_model_batch works", True)


# ── Performance benchmarks ────────────────────────────────────────────────────


def run_benchmarks() -> None:
    print(f"\n{'=' * 60}")

    # Batch int validation
    N = 10_000
    big_ints = list(range(N))
    start = time.perf_counter()
    for _ in range(100):
        validate_ints_batch(big_ints, 0, N)
    elapsed = time.perf_counter() - start
    total = N * 100
    print(
        f"validate_ints_batch: {total:,} ints in {elapsed:.3f}s = "
        f"{total / elapsed:,.0f}/sec"
    )

    # Batch user validation
    users_1k = [
        {"name": f"User{i}", "email": f"user{i}@test.com", "age": 20 + (i % 80)}
        for i in range(1000)
    ]
    start = time.perf_counter()
    for _ in range(100):
        validate_users_batch(users_1k)
    elapsed = time.perf_counter() - start
    total = 1000 * 100
    print(
        f"validate_users_batch: {total:,} users in {elapsed:.3f}s = "
        f"{total / elapsed:,.0f}/sec"
    )

    # Batch model validation
    model_data = [
        {"name": f"User{i}", "age": 20 + (i % 80), "score": float(i)}
        for i in range(1000)
    ]
    start = time.perf_counter()
    for _ in range(100):
        validate_model_batch(model_data, User)
    elapsed = time.perf_counter() - start
    total = 1000 * 100
    print(
        f"validate_model_batch: {total:,} models in {elapsed:.3f}s = "
        f"{total / elapsed:,.0f}/sec"
    )

    # Compare: individual model creation
    start = time.perf_counter()
    for _ in range(100):
        for d in model_data:
            User(**d)
    elapsed = time.perf_counter() - start
    print(
        f"Individual User(): {total:,} models in {elapsed:.3f}s = "
        f"{total / elapsed:,.0f}/sec"
    )

    print(f"{'=' * 60}")


_TESTS = [
    test_ints_batch,
    test_strings_batch,
    test_emails_batch,
    test_users_batch,
    test_model_batch,
]


def main() -> bool:
    # The asserts inside each section abort the run on the first violation, as
    # before — the tally is emitted before exiting.
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
    run_benchmarks()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
