"""
Task #194: microbenchmark _db_query (tuples) vs _db_query_dicts (dicts) on
the same SQL + same DB state, to isolate the per-row dict construction cost
in pg.zig.

Runs both against the bookstore_api seeded DB, varying row count to
separate the per-row dict allocation from fixed parse/execute overhead.

Outputs logs/bench_db_query_dicts.json + stdout table.

Run: uv run python scripts/bench_db_query_dicts.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"

# Row counts chosen to cover small (FK lookup), medium (list page),
# and large (admin changelist / analytics) query shapes.
ROW_COUNTS = [1, 10, 50, 200]

# Per-iteration count per shape, tuned so each wall-clock run is ≥1s. These
# are microbenchmarks so wall-time > 1s per shape is enough to beat jitter.
ITERS_BY_SHAPE = {1: 20000, 10: 10000, 50: 4000, 200: 1500}
RUNS = 5  # Take median across RUNS runs for each shape


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=== bench_db_query_dicts (Task #194) ===")

    # Setup DB via subprocess
    print("Setting up bookstore_api DB...")
    setup = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.bookstore_api.app:app",
            "--drop",
            "--seed",
            "services.bookstore_api.seed:run",
        ],
        capture_output=True,
        timeout=120,
    )
    if setup.returncode != 0:
        print("setup FAILED:")
        print(setup.stdout.decode()[-2000:])
        print(setup.stderr.decode()[-2000:])
        sys.exit(1)

    os.environ["HYPER_LOAD_TEST"] = "1"

    from hyperdjango._hyperdjango_native import _db_query, _db_query_dicts

    from hyperdjango.database import get_db
    from services.bookstore_api.app import app  # noqa: F401 — triggers connect

    db = get_db()
    if db._pool_handle is None:
        asyncio.run(db.connect())

    handle = db._pool_handle
    # Use a query that can be parameterized by row limit. bookstore seed
    # has ~48 books — we need enough rows to hit larger shapes without
    # duplicating. Use a generate_series cross join on an existing table.
    # Simpler: just SELECT * FROM bk_books LIMIT $1 — we'll top up rows
    # first by duplicating seed data.

    # Count books, top up if needed
    async def topup_rows():
        count = await db.query_val("SELECT COUNT(*) FROM bk_books")
        if count < 500:
            # Insert cheap dummy rows so LIMIT 200 returns 200 real rows
            print(f"  Topping up bk_books from {count} → 500 rows...")
            for i in range(count, 500):
                await db.execute(
                    "INSERT INTO bk_books (title, isbn, description, price, "
                    "pages, published, featured, author_id, category_id, "
                    "created_at, updated_at) VALUES "
                    "($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), NOW())",
                    f"Bench Book {i}",
                    f"BENCH-{i:08d}",
                    "",
                    "0.00",
                    100,
                    False,
                    False,
                    1,
                    1,
                )

    asyncio.run(topup_rows())

    sql_tuples = "SELECT id, title, isbn, description, price, pages, published, featured, author_id, category_id, created_at, updated_at FROM bk_books LIMIT $1"
    sql_dicts = sql_tuples  # same SQL — pg.zig caches the prepared plan

    # Warmup both codepaths so prepared statement + cached columns are hot
    print("Warming up both paths...")
    for _ in range(2000):
        _db_query(handle, sql_tuples, ["10"])
        _db_query_dicts(handle, sql_dicts, ["10"])

    print(
        f"\n  {'rows':>5}  {'iters':>6}  {'tuples(ms/row)':>14}  "
        f"{'dicts(ms/row)':>14}  {'overhead':>9}  {'dict%':>7}"
    )
    print("  " + "-" * 70)

    results: list[dict] = []
    for n_rows in ROW_COUNTS:
        iters = ITERS_BY_SHAPE[n_rows]
        param = [str(n_rows)]

        tuple_runs: list[float] = []
        dict_runs: list[float] = []

        for _ in range(RUNS):
            # Tuples
            t0 = time.perf_counter()
            for _ in range(iters):
                rows = _db_query(handle, sql_tuples, param)
                if len(rows) != n_rows:
                    print(f"  FAIL: got {len(rows)} rows, expected {n_rows}")
                    sys.exit(1)
            tuple_runs.append(time.perf_counter() - t0)

            # Dicts
            t0 = time.perf_counter()
            for _ in range(iters):
                rows = _db_query_dicts(handle, sql_dicts, param)
                if len(rows) != n_rows:
                    print(f"  FAIL: got {len(rows)} rows, expected {n_rows}")
                    sys.exit(1)
            dict_runs.append(time.perf_counter() - t0)

        tuple_runs.sort()
        dict_runs.sort()
        tuple_median = tuple_runs[RUNS // 2]
        dict_median = dict_runs[RUNS // 2]

        total_rows = iters * n_rows
        tuple_ms_per_row = (tuple_median / total_rows) * 1000
        dict_ms_per_row = (dict_median / total_rows) * 1000
        overhead_us_per_row = (dict_ms_per_row - tuple_ms_per_row) * 1000
        dict_pct = (dict_median / tuple_median - 1) * 100

        print(
            f"  {n_rows:>5}  {iters:>6}  {tuple_ms_per_row:>14.6f}  "
            f"{dict_ms_per_row:>14.6f}  {overhead_us_per_row:>7.2f}µs  "
            f"{dict_pct:>6.1f}%"
        )

        results.append(
            {
                "rows": n_rows,
                "iters": iters,
                "tuple_median_s": round(tuple_median, 4),
                "dict_median_s": round(dict_median, 4),
                "tuple_us_per_row": round(tuple_ms_per_row * 1000, 3),
                "dict_us_per_row": round(dict_ms_per_row * 1000, 3),
                "overhead_us_per_row": round(overhead_us_per_row, 3),
                "dict_overhead_pct": round(dict_pct, 2),
                "tuple_runs_s": [round(r, 4) for r in tuple_runs],
                "dict_runs_s": [round(r, 4) for r in dict_runs],
            }
        )

    json_path = LOGS / "bench_db_query_dicts.json"
    json_path.write_text(json.dumps({"shapes": results}, indent=2))
    print(f"\n  JSON: {json_path}")
    print("\n=== bench_db_query_dicts complete ===")


if __name__ == "__main__":
    main()
