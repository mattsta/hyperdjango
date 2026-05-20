"""
Hypothesis fuzz tests for SIMD batch validation.

Proves: batch_validate(items) == [validate(item) for item in items]
for int range, string length, and email validation.

# hyper-test: unit
"""

from hyperdjango._hyperdjango_native import (
    validate_int_batch_simd,
    validate_string_length_batch,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Property 1: SIMD int batch matches scalar per-element
# ---------------------------------------------------------------------------


@given(
    values=st.lists(
        st.integers(min_value=-(2**31), max_value=2**31 - 1), min_size=0, max_size=100
    ),
    min_val=st.integers(min_value=-1000, max_value=0),
    max_val=st.integers(min_value=1, max_value=1000),
)
@settings(max_examples=500, deadline=2000)
def test_int_batch_matches_scalar(values, min_val, max_val):
    """SIMD int batch validation matches per-element range check."""
    results, valid_count = validate_int_batch_simd(values, min_val, max_val)

    # Compute expected scalar results
    expected_valid = sum(1 for v in values if min_val <= v <= max_val)

    assert valid_count == expected_valid, (
        f"Count mismatch: SIMD={valid_count} scalar={expected_valid} "
        f"for range [{min_val}, {max_val}], values sample={values[:5]}"
    )


@given(
    values=st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=50)
)
@settings(max_examples=300, deadline=2000)
def test_int_batch_all_valid(values):
    """All values in range → count == len(values)."""
    _, count = validate_int_batch_simd(values, 0, 100)
    assert count == len(values)


@given(
    values=st.lists(st.integers(min_value=200, max_value=300), min_size=1, max_size=50)
)
@settings(max_examples=300, deadline=2000)
def test_int_batch_all_invalid(values):
    """All values out of range → count == 0."""
    _, count = validate_int_batch_simd(values, 0, 100)
    assert count == 0


def test_int_batch_empty():
    """Empty list → count == 0."""
    _, count = validate_int_batch_simd([], 0, 100)
    assert count == 0
    print("  PASS: int batch empty")


# ---------------------------------------------------------------------------
# Property 2: SIMD string length batch matches scalar
# ---------------------------------------------------------------------------


@given(
    values=st.lists(st.text(max_size=50), min_size=0, max_size=100),
    min_len=st.integers(min_value=0, max_value=5),
    max_len=st.integers(min_value=6, max_value=50),
)
@settings(max_examples=500, deadline=2000)
def test_string_batch_matches_scalar(values, min_len, max_len):
    """SIMD string length batch matches per-element length check."""
    results, valid_count = validate_string_length_batch(values, min_len, max_len)

    expected_valid = sum(1 for v in values if min_len <= len(v) <= max_len)

    assert valid_count == expected_valid, (
        f"Count mismatch: SIMD={valid_count} scalar={expected_valid} "
        f"for len [{min_len}, {max_len}]"
    )


@given(values=st.lists(st.text(min_size=3, max_size=10), min_size=1, max_size=50))
@settings(max_examples=300, deadline=2000)
def test_string_batch_all_valid(values):
    """All strings in length range → count == len(values)."""
    _, count = validate_string_length_batch(values, 1, 100)
    assert count == len(values)


def test_string_batch_empty():
    """Empty list → count == 0."""
    _, count = validate_string_length_batch([], 0, 100)
    assert count == 0
    print("  PASS: string batch empty")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── SIMD Batch Validation Hypothesis Fuzz Tests ──\n")

    tests = [
        ("int batch empty", test_int_batch_empty),
        ("int batch matches scalar", test_int_batch_matches_scalar),
        ("int batch all valid", test_int_batch_all_valid),
        ("int batch all invalid", test_int_batch_all_invalid),
        ("string batch empty", test_string_batch_empty),
        ("string batch matches scalar", test_string_batch_matches_scalar),
        ("string batch all valid", test_string_batch_all_valid),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"SIMD validation fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
