"""Regression tests for the ws15 protocol/refcount/error-path fixes.

Covers:
  * F10  — a query that errors mid-result-stream must recover the (pinned/
           thread-owned) connection instead of wedging it in .query so every
           later query fails ConnectionBusy, and must SURFACE the error rather
           than return a silently truncated row set as success.
  * LEAK1/LEAK2 — timestamp + timetz column conversion must not leak PyObject
           refs per row (the old gmtime/PySequence_GetItem and PyTuple_Pack
           paths did).
  * LEAK3 — _server_get_ws_config must not leak the 3 PyLongs it packs.

Requires PostgreSQL (see tests/test_db/conftest.py).

Run: uv run pytest tests/test_db/test_ws15_protocol_fixes.py -v
"""

import resource

import pytest


def _rss_bytes():
    # macOS reports ru_maxrss in bytes; Linux in KiB. We only compare deltas,
    # and use a generous threshold, so the unit difference is immaterial.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


# ── F10: mid-stream error recovery ──────────────────────────────────────────

# Streams 9 DataRows, then raises division-by-zero computing the 10th → the
# ErrorResponse arrives mid-result-stream, exactly the condition that used to
# leave the connection wedged.
_MID_STREAM_ERROR_SQL = "SELECT 1 / (10 - g) AS x FROM generate_series(1, 20) g"


def test_mid_stream_error_surfaces_and_recovers_connection(db_pool):
    for _ in range(5):
        # The erroring query must raise, not return a truncated 9-row result.
        with pytest.raises(Exception):
            db_pool.query(_MID_STREAM_ERROR_SQL, [])

        # The connection (thread-owned slot — reused by this same thread) must
        # be usable again immediately; a wedged .query connection would fail
        # with ConnectionBusy here.
        rows = db_pool.query("SELECT 42 AS n", [])
        assert rows == [(42,)]


def test_mid_stream_error_json_path_recovers(db_pool):
    # The query_json fast path has its own row loop — same recovery contract.
    from hyperdjango._hyperdjango_native import _db_query_json

    for _ in range(5):
        with pytest.raises(Exception):
            _db_query_json(db_pool.handle, _MID_STREAM_ERROR_SQL, [])
        assert _db_query_json(db_pool.handle, "SELECT 7 AS n", []) == b'[{"n":7}]'


# ── LEAK1 / LEAK2: timestamp + timetz conversion refcount leak ──────────────


def test_timestamp_and_timetz_no_per_row_leak(db_pool):
    # Each query returns 1000 timestamp + timetz values; the old converters
    # leaked ~10 PyObject refs per row. Compare the RSS growth of a second big
    # batch against the first — a per-row leak grows both roughly equally, so
    # the delta would stay large; the fixed code plateaus.
    sql = (
        "SELECT (now() + (g || ' seconds')::interval)::timestamp AS ts, "
        "       (now() + (g || ' seconds')::interval)::timetz AS tz "
        "FROM generate_series(1, 1000) g"
    )

    def run_batch(n):
        for _ in range(n):
            rows = db_pool.query(sql, [])
            assert len(rows) == 1000

    run_batch(50)  # warm up allocators/caches
    before = _rss_bytes()
    run_batch(200)  # 200k rows converted
    after = _rss_bytes()

    growth = after - before
    # Old code leaked ~13.6 MB per 300k rows → ~9 MB for 200k. Fixed code stays
    # well under 4 MB (ru_maxrss is a high-water mark; allow arena headroom).
    max_growth = 4 * 1024 * 1024 if _rss_is_bytes() else 4 * 1024
    assert growth < max_growth, f"timestamp/timetz RSS grew {growth} (leak?)"


# ── LEAK3: _server_get_ws_config PyLong leak ────────────────────────────────


def test_ws_config_no_leak():
    from hyperdjango._hyperdjango_native import _server_get_ws_config

    # Sanity: returns the 3-tuple.
    cfg = _server_get_ws_config()
    assert isinstance(cfg, tuple) and len(cfg) == 3

    for _ in range(200_000):
        _server_get_ws_config()
    before = _rss_bytes()
    for _ in range(1_000_000):
        _server_get_ws_config()
    after = _rss_bytes()

    growth = after - before
    max_growth = 3 * 1024 * 1024 if _rss_is_bytes() else 3 * 1024
    assert growth < max_growth, f"_server_get_ws_config RSS grew {growth} (leak?)"


def _rss_is_bytes():
    import sys

    return sys.platform == "darwin"
