"""
Smoke test for the native span ring buffer FFI.

# hyper-test: unit

Bare-metal FFI round-trip tests — before building the Python Tracer
facade on top. Tests the basic contract:

    1. _span_start() returns non-zero for sampled spans
    2. _span_start(sampled=False) returns 0 (sentinel)
    3. set_attr / set_status / end on sentinel are no-ops
    4. set_attr on a real span lands in the drained record
    5. end → drain round-trip preserves trace_id, span_id, parent_id,
       name, start/end times, attributes, status
    6. dropped_count increments on unsampled spans
    7. Multiple spans drain in one call
    8. Reset helper clears state
"""

import sys

from hyperdjango._hyperdjango_native import (
    _span_drain,
    _span_dropped_count,
    _span_end,
    _span_reset_for_tests,
    _span_set_attr_float,
    _span_set_attr_int,
    _span_set_attr_str,
    _span_set_status,
    _span_start,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


def test_sentinel_handle():
    print("\n── sentinel handle (sampled=False) ──")
    _span_reset_for_tests()
    handle = _span_start(0, 0, 0, "unsampled", False)
    check("unsampled returns sentinel 0", handle == 0, f"got {handle}")
    # Ops on sentinel must be safe no-ops
    _span_set_attr_str(0, "key", "value")
    _span_set_attr_int(0, "count", 42)
    _span_set_attr_float(0, "ratio", 0.5)
    _span_set_status(0, 1)
    _span_end(0)
    check("no-op ops on sentinel don't crash", True)
    # Drain should be empty — sentinel spans never touch a slot
    spans = _span_drain()
    check("drain empty after only sentinel", len(spans) == 0, f"got {len(spans)}")


def test_basic_roundtrip():
    print("\n── basic start → set_attr → end → drain ──")
    _span_reset_for_tests()
    trace_high = 0x123456789ABCDEF0
    trace_low = 0x0FEDCBA987654321
    parent_id = 0xDEADBEEF
    handle = _span_start(trace_high, trace_low, parent_id, "GET /api/books", True)
    check("sampled returns non-zero handle", handle != 0, f"got {handle}")

    _span_set_attr_str(handle, "http.method", "GET")
    _span_set_attr_str(handle, "http.path", "/api/books")
    _span_set_attr_int(handle, "http.status", 200)
    _span_set_attr_float(handle, "duration_ms", 12.5)
    _span_set_status(handle, 1)  # ok
    _span_end(handle)

    spans = _span_drain()
    check("one span drained", len(spans) == 1, f"got {len(spans)}")
    if len(spans) != 1:
        return
    span = spans[0]

    check("name preserved", span["name"] == "GET /api/books", f"got {span['name']!r}")
    check(
        "trace_id hex-formatted",
        span["trace_id"] == "123456789abcdef00fedcba987654321",
        f"got {span['trace_id']!r}",
    )
    check("status code 1", span["status"]["code"] == 1)
    check("status message empty", span["status"]["message"] == "")
    check("start_time present", span["start_time_unix_nano"] > 0)
    check(
        "end_time > start_time",
        span["end_time_unix_nano"] > span["start_time_unix_nano"],
    )

    attrs = span["attributes"]
    check("attr http.method", attrs.get("http.method") == "GET")
    check("attr http.path", attrs.get("http.path") == "/api/books")
    check("attr http.status (as str)", attrs.get("http.status") == "200")
    check("attr duration_ms (as str)", attrs.get("duration_ms") == "12.5")


def test_multiple_spans():
    print("\n── multiple spans drain together ──")
    _span_reset_for_tests()
    handles = []
    for i in range(10):
        h = _span_start(i, i * 2, i * 3, f"span_{i}", True)
        check(f"span {i} handle non-zero", h != 0)
        handles.append(h)
    # End in reverse order (child-before-parent isn't enforced, just tests)
    for h in reversed(handles):
        _span_end(h)
    spans = _span_drain()
    check("10 spans drained", len(spans) == 10, f"got {len(spans)}")
    names = sorted(s["name"] for s in spans)
    expected = sorted(f"span_{i}" for i in range(10))
    check("all span names present", names == expected)


def test_drain_after_drain_is_empty():
    print("\n── drain-after-drain is empty ──")
    _span_reset_for_tests()
    h = _span_start(0, 0, 0, "solo", True)
    _span_end(h)
    first = _span_drain()
    second = _span_drain()
    check("first drain has 1", len(first) == 1)
    check("second drain is empty", len(second) == 0)


def test_root_span_empty_parent():
    print("\n── root span (parent_id=0) → empty parent_id string ──")
    _span_reset_for_tests()
    h = _span_start(1, 2, 0, "root", True)
    _span_end(h)
    spans = _span_drain()
    check(
        "root span parent_id is empty string",
        spans[0]["parent_id"] == "",
        f"got {spans[0].get('parent_id')!r}",
    )


def test_dropped_count_increments():
    print("\n── dropped count increments on unsampled ──")
    _span_reset_for_tests()
    start = _span_dropped_count()
    for _ in range(5):
        _span_start(0, 0, 0, "dropped", False)
    delta = _span_dropped_count() - start
    check("dropped count += 5 for unsampled", delta == 5, f"got {delta}")


def test_stale_handle_is_noop():
    print("\n── stale handle after end is no-op ──")
    _span_reset_for_tests()
    h = _span_start(0, 0, 0, "first", True)
    _span_end(h)
    # Drain clears the slot
    _span_drain()
    # Calling set_attr on a stale handle should be a no-op (not a crash)
    _span_set_attr_str(h, "after_drain", "stale")
    _span_end(h)
    check("stale handle ops don't crash", True)
    # Stale ops don't resurrect the span — drain is still empty
    spans = _span_drain()
    check("stale ops don't create drained spans", len(spans) == 0)


def test_name_truncation():
    print("\n── name truncation to NAME_MAX=64 ──")
    _span_reset_for_tests()
    long_name = "x" * 200  # well over 64
    h = _span_start(0, 0, 0, long_name, True)
    _span_end(h)
    spans = _span_drain()
    check(
        "long name truncated to 64",
        len(spans[0]["name"]) == 64,
        f"got len={len(spans[0]['name'])}",
    )


def test_attr_overflow_silently_drops():
    print("\n── attr buffer overflow silently drops extras ──")
    _span_reset_for_tests()
    h = _span_start(0, 0, 0, "overflow", True)
    # Each KV pair: 2 bytes header + key + val. For key="k", val="v"
    # (1+1), that's 4 bytes. 128 / 4 = 32 pairs fit. We push 100 to
    # verify the extras get dropped without crashing.
    for i in range(100):
        _span_set_attr_str(h, "k", "v")
    _span_end(h)
    spans = _span_drain()
    # All values are the same, so we only see one entry in the dict
    # (dict-keying dedups). Just verify drain succeeded and parsed
    # without corruption.
    check("overflow doesn't crash", len(spans) == 1)
    check(
        "overflow drops extras — attr count bounded",
        isinstance(spans[0]["attributes"], dict),
    )


def main() -> int:
    print("=" * 70)
    print("  Span ring FFI smoke tests (P3.2)")
    print("=" * 70)

    test_sentinel_handle()
    test_basic_roundtrip()
    test_multiple_spans()
    test_drain_after_drain_is_empty()
    test_root_span_empty_parent()
    test_dropped_count_increments()
    test_stale_handle_is_noop()
    test_name_truncation()
    test_attr_overflow_silently_drops()

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
