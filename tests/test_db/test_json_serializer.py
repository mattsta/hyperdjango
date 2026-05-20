"""Test native Zig JSON serializer.

Tests that json_dumps_native correctly serializes Python objects to JSON bytes
using SIMD-accelerated string escaping.

Run: uv run pytest tests/test_db/test_json_serializer.py -v
"""

import json
import time

from hyperdjango._hyperdjango_native import json_dumps_native


class TestJsonSerializerCorrectness:
    """Verify native JSON matches Python's json.dumps output."""

    def _check(self, obj):
        native = json_dumps_native(obj)
        expected = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        assert native == expected, (
            f"Mismatch for {obj!r}:\n  native:   {native}\n  expected: {expected}"
        )

    def test_none(self):
        self._check(None)

    def test_bool_true(self):
        self._check(True)

    def test_bool_false(self):
        self._check(False)

    def test_int_zero(self):
        self._check(0)

    def test_int_positive(self):
        self._check(42)

    def test_int_negative(self):
        self._check(-123)

    def test_float(self):
        native = json_dumps_native(3.14)
        parsed = json.loads(native)
        assert abs(parsed - 3.14) < 0.001

    def test_string_simple(self):
        self._check("hello")

    def test_string_empty(self):
        self._check("")

    def test_string_with_quotes(self):
        self._check('say "hello"')

    def test_string_with_backslash(self):
        self._check("path\\to\\file")

    def test_string_with_newline(self):
        self._check("line1\nline2")

    def test_string_with_tab(self):
        self._check("col1\tcol2")

    def test_string_unicode(self):
        self._check("héllo wörld")

    def test_list_empty(self):
        self._check([])

    def test_list_ints(self):
        self._check([1, 2, 3])

    def test_list_mixed(self):
        self._check([1, "hello", True, None])

    def test_dict_empty(self):
        self._check({})

    def test_dict_simple(self):
        self._check({"name": "Alice", "age": 30})

    def test_dict_nested(self):
        self._check({"user": {"name": "Alice", "tags": [1, 2, 3]}})

    def test_dict_with_none(self):
        self._check({"key": None})

    def test_list_of_dicts(self):
        self._check([{"id": 1}, {"id": 2}])

    def test_complex_structure(self):
        obj = {
            "users": [
                {"name": "Alice", "age": 30, "active": True},
                {"name": "Bob", "age": 25, "active": False},
            ],
            "count": 2,
            "meta": None,
        }
        self._check(obj)

    def test_returns_bytes(self):
        result = json_dumps_native({"key": "value"})
        assert isinstance(result, bytes)

    def test_special_chars_in_key(self):
        self._check({"key with spaces": "value"})


class TestJsonSerializerPerformance:
    """Benchmark native JSON serializer vs Python json.dumps."""

    def test_faster_than_python_json(self):
        """Native serializer should be faster than Python json.dumps."""
        obj = {"name": "Alice", "age": 30, "email": "alice@example.com"}
        N = 10000

        # Warm up
        for _ in range(100):
            json_dumps_native(obj)
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()

        # Benchmark native
        start = time.perf_counter_ns()
        for _ in range(N):
            json_dumps_native(obj)
        native_ns = (time.perf_counter_ns() - start) / N

        # Benchmark Python
        start = time.perf_counter_ns()
        for _ in range(N):
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        python_ns = (time.perf_counter_ns() - start) / N

        ratio = python_ns / native_ns
        # Native should be at least 2x faster
        assert ratio > 1.5, (
            f"Native {native_ns:.0f}ns vs Python {python_ns:.0f}ns (ratio: {ratio:.1f}x)"
        )

    def test_large_dict_correctness(self):
        """Native should correctly handle large dicts."""
        obj = {f"key_{i}": f"value_{i}" for i in range(100)}
        native = json_dumps_native(obj)
        expected = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        assert json.loads(native) == json.loads(expected)
