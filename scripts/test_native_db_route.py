#!/usr/bin/env python3
# hyper-test: e2e
"""End-to-end coverage for the native auto-CRUD route path (db.zig `handleDbRoute`).

`app.add_db_route(...)` registers a route that the Zig server answers ENTIRELY in
native code — HTTP request → native router → `handleDbRoute` → `serializeRowAlloc`
→ `writeJsonValue` → socket — never entering Python. That path (and its
`writeJsonValue` serializer) previously had a Python on-ramp but NO end-to-end
coverage, so this file drives it over real HTTP against a live PostgreSQL.

It doubles as a fidelity check for the serializer: the seeded row carries a
high-precision NUMERIC and a non-finite float, and we assert the served JSON is
the exact decimal string / lossless "Infinity" token (matching
docs/database.md#result-serialization-to-json) — i.e. the commit that fixed the
lossy-NUMERIC / null-non-finite drift is exercised end-to-end here.

Usage:
    uv run hyper-test native_db_route
    DATABASE_URL=... uv run python scripts/test_native_db_route.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_helper import TEST_PORTS, AppRunner, http_get  # noqa: E402

from hyperdjango import HyperApp  # noqa: E402
from hyperdjango.database import get_db  # noqa: E402

PORT = TEST_PORTS["native_db_route"]
DB_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/hyperdjango_test",
)
TABLE = "ndr_items"

# ── App under test: one native auto-CRUD route + seeded fixture row ──────────
app = HyperApp(title="native-db-route-fixture", database=DB_URL)


@app.on_startup
async def _seed():
    db = get_db()
    await db.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await db.execute(
        f"CREATE TABLE {TABLE} ("
        "  id int primary key,"
        "  label text,"
        "  price numeric,"  # exact decimal — must survive as a JSON string
        "  ratio float8"  # may be non-finite
        ")"
    )
    # id=1: high-precision NUMERIC a float64 can't hold + a finite ratio.
    await db.execute(
        f"INSERT INTO {TABLE} (id, label, price, ratio) VALUES ($1, $2, $3, $4)",
        1,
        "widget",
        "123456789012345678.99",
        1.5,
    )
    # id=2: +Infinity ratio — the serializer must not drop it to null.
    await db.execute(
        f"INSERT INTO {TABLE} (id, label, price, ratio) VALUES ($1, $2, $3::numeric, 'infinity'::float8)",
        2,
        "infinite",
        "0.00001234",
    )


# Native auto-CRUD: GET /items/{id} → SELECT * FROM ndr_items WHERE id = $1,
# answered without ever entering Python.
app.add_db_route(
    "GET", "/items/{id}", table=TABLE, op="select_one", pk_column="id", pk_param="id"
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def run(host, port):
    base = f"http://{host}:{port}"

    # id=1 — served natively, exact NUMERIC + finite float.
    r = http_get(f"{base}/items/1")
    check("GET /items/1 → 200", r.status == 200, f"status={r.status}")
    check(
        "content-type json",
        "application/json" in (r.headers.get("content-type", "")),
        r.headers.get("content-type", ""),
    )
    row = json.loads(r.body)
    check("id round-trips", row.get("id") == 1, repr(row.get("id")))
    check("label round-trips", row.get("label") == "widget", repr(row.get("label")))
    # NUMERIC must be the EXACT decimal string (a JSON number would lose precision).
    check(
        "NUMERIC exact as string (no float precision loss)",
        row.get("price") == "123456789012345678.99",
        repr(row.get("price")),
    )
    check("finite float is a number", row.get("ratio") == 1.5, repr(row.get("ratio")))

    # id=2 — non-finite float must be the lossless "Infinity" string, not null.
    r2 = http_get(f"{base}/items/2")
    # Surface the body on failure: a 500 here is either {"error":"Query failed"}
    # (query/decode path) or {"error":"Row too large to serialize"} (serializer
    # returned 0) — that one bit localizes the native fault when it only
    # reproduces on a specific runner/arch.
    check(
        "GET /items/2 → 200", r2.status == 200, f"status={r2.status} body={r2.body!r}"
    )
    row2 = json.loads(r2.body)
    check(
        'non-finite float → lossless "Infinity" (never null)',
        row2.get("ratio") == "Infinity",
        repr(row2.get("ratio")),
    )
    check(
        "float() round-trips the token",
        _is_pos_inf(row2.get("ratio")),
        repr(row2.get("ratio")),
    )
    check(
        "small NUMERIC exact",
        row2.get("price") == "0.00001234",
        repr(row2.get("price")),
    )

    # Missing pk → native 404 (not a crash / not a Python 500).
    r3 = http_get(f"{base}/items/999999")
    check("GET missing pk → 404", r3.status == 404, f"status={r3.status}")

    # Worker survives the native path: a normal request still works afterward.
    r4 = http_get(f"{base}/items/1")
    check("worker survives (200 after)", r4.status == 200, f"status={r4.status}")


def _is_pos_inf(v):
    try:
        f = float(v)
        return f == float("inf")
    except TypeError, ValueError:
        return False


def main() -> bool:
    print("=" * 64)
    print("Native auto-CRUD route (handleDbRoute → writeJsonValue) e2e")
    print("=" * 64)
    scripts_dir = str(Path(__file__).resolve().parent)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = scripts_dir + (os.pathsep + existing if existing else "")
    with AppRunner(
        "test_native_db_route:app",
        port=PORT,
        readiness_path="/_ready",
        env={"PYTHONPATH": pythonpath, "DATABASE_URL": DB_URL},
    ) as r:
        run(r.host, r.port)
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
