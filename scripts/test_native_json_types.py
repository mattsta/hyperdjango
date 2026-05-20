#!/usr/bin/env python3
"""Native JSON serialization of binary-wire Postgres types (round-trip).

Fix-wave cover for the native auto-CRUD JSON serializer (zig/src/pg/result.zig
`writeJsonValue`) and its byte-identical sibling (zig/src/db.zig query_json /
pg_render). Before this wave, `writeJsonValue`'s `else` branch quoted the RAW
BINARY bytes of any OID it did not explicitly decode — uuid, date, time,
bytea, and (via a truncated escaper) control chars in text[] — producing
corrupt / invalid JSON. It also emitted timestamps as a bare epoch-SECONDS
integer (sub-second precision lost, not ISO-8601).

This test inserts and reads back rows containing uuid / date / time /
timestamp (with sub-second) / bytea / text[] (with an embedded control char) /
int[], through the LIVE native `Database.query_json` path, and asserts the
emitted bytes are (1) valid JSON and (2) round-trip the values correctly. It
also cross-checks the native OBJECT path (`Database.query`) so the two native
decoders are proven to agree.

The `query_json` (db.zig) and `writeJsonValue` (result.zig) serializers are
intended to be byte-identical, but they had DRIFTED (NUMERIC was exact in
query_json yet lossy-float in writeJsonValue) — so this `query_json` test did
NOT transitively guard writeJsonValue. That drift is now fixed, and the DIRECT
coverage of writeJsonValue's prongs runs via `zig build test-pg` (the pg-module
unit tests, previously dormant because t.zig was a stub):
    * "writeJsonValue: binary scalar/array types render as valid JSON"
    * "writeJsonValue: non-finite floats + NUMERIC render losslessly, ..."
    * "writeJsonHex: ...", "isoDate/isoTime/isoTimestamp/uuidToStr: ...",
      "writeTextArrayJson: control char in element escaped to valid JSON"
Run `zig build test-pg` (needs a live PostgreSQL; see zig/src/pg/t.zig) so those
execute. The `writeJsonValue` path is what the native HTTP auto-CRUD route
handler (db.zig `handleDbRoute`) uses; an HTTP e2e hitting that route (task:
native auto-CRUD on-ramp) would exercise it end-to-end.

Usage:
    uv run hyper-test native_json_types
    uv run python scripts/test_native_json_types.py
"""

# hyper-test: db_isolated

import asyncio
import datetime
import json
import os
import uuid as uuidlib

from hyperdjango.database import Database, set_db
from hyperdjango.testkit import check, finish, run_main

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

TABLE = "test_native_json_types"

# Known fixtures.
UUID_VAL = "b7cc282f-ec43-49be-8e09-aafab0104915"
DATE_VAL = "2024-03-15"
TIME_VAL = "12:30:45.123456"
# Stored with a space separator (unambiguous for ::timestamp); the native JSON
# serializer must emit the canonical ISO-8601 form with the 'T' separator.
TS_INSERT = "2024-03-15 12:30:45.123456"
TS_VAL = "2024-03-15T12:30:45.123456"  # naive ISO-8601 with microseconds
BYTEA_HEX = b"\xde\xad\xbe\xef"
# A text[] element carrying an embedded control character (TAB) plus a quote and
# backslash — every one of these MUST be JSON-escaped, not emitted raw.
TEXT_ARR = ["a\tb", 'q"u\\x', "plain"]
INT_ARR = [1, -2, 3]


async def run():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
    await db.execute(
        f"""
        CREATE UNLOGGED TABLE {TABLE} (
            id        serial PRIMARY KEY,
            u         uuid,
            d         date,
            t         time,
            ts        timestamp,
            b         bytea,
            ta        text[],
            ia        integer[]
        )
        """
    )

    # Explicit casts on the text scalar params: PG has no implicit text→uuid /
    # text→date / text→time / text→timestamp cast in an INSERT assignment.
    await db.execute(
        f"INSERT INTO {TABLE} (u, d, t, ts, b, ta, ia) "
        f"VALUES ($1::uuid, $2::date, $3::time, $4::timestamp, $5, $6, $7)",
        UUID_VAL,
        DATE_VAL,
        TIME_VAL,
        TS_INSERT,
        BYTEA_HEX,
        TEXT_ARR,
        INT_ARR,
    )

    try:
        # ── Native JSON path ────────────────────────────────────────────────
        raw = await db.query_json(
            f"SELECT u, d, t, ts, b, ta, ia FROM {TABLE} ORDER BY id"
        )
        check(
            "query_json returns bytes",
            isinstance(raw, (bytes, bytearray)),
            f"got {type(raw)!r}",
        )

        # (1) VALID JSON — the whole point: no raw binary, no dropped control chars.
        obj = None
        try:
            obj = json.loads(raw)
            check("query_json output is valid JSON", True)
        except Exception as e:  # noqa: BLE001
            check("query_json output is valid JSON", False, repr(e))
            print(f"    raw bytes: {raw!r}")

        if obj:
            row = obj[0] if isinstance(obj, list) else obj

            # (2) Round-trip each value.
            check("uuid round-trips", row.get("u") == UUID_VAL, repr(row.get("u")))
            check(
                "date round-trips (ISO YYYY-MM-DD)",
                row.get("d") == DATE_VAL,
                repr(row.get("d")),
            )
            check(
                "time round-trips (HH:MM:SS.ffffff)",
                row.get("t") == TIME_VAL,
                repr(row.get("t")),
            )
            # Timestamp: real ISO-8601 with sub-second precision (not epoch seconds).
            check(
                "timestamp keeps sub-second precision + ISO-8601",
                row.get("ts") == TS_VAL,
                repr(row.get("ts")),
            )
            # bytea: `\xDEADBEEF` hex string (matches PG ::text), never raw bytes.
            check(
                "bytea renders as \\x hex string",
                row.get("b") == "\\xdeadbeef",
                repr(row.get("b")),
            )
            # text[] with control char / quote / backslash — must be escaped &
            # therefore parse back to the exact original element strings.
            check(
                "text[] round-trips with escaped control/quote/backslash",
                row.get("ta") == TEXT_ARR,
                repr(row.get("ta")),
            )
            check("int[] round-trips", row.get("ia") == INT_ARR, repr(row.get("ia")))

        # ── Native OBJECT path cross-check (the two decoders must agree) ─────
        orows = await db.query(
            f"SELECT u, d, t, ts, b, ta, ia FROM {TABLE} ORDER BY id"
        )
        orow = orows[0]

        def _s(v):
            # Normalize object-path Python values to their JSON-path spelling.
            if isinstance(v, uuidlib.UUID):
                return str(v)
            if isinstance(v, datetime.datetime):
                return v.isoformat()
            if isinstance(v, (datetime.date, datetime.time)):
                return v.isoformat()
            if isinstance(v, (bytes, bytearray)):
                return "\\x" + bytes(v).hex()
            return v

        check("object-path uuid agrees", _s(orow["u"]) == UUID_VAL, repr(orow["u"]))
        check("object-path date agrees", _s(orow["d"]) == DATE_VAL, repr(orow["d"]))
        check("object-path time agrees", _s(orow["t"]) == TIME_VAL, repr(orow["t"]))
        check(
            "object-path timestamp agrees", _s(orow["ts"]) == TS_VAL, repr(orow["ts"])
        )
        check(
            "object-path bytea agrees", _s(orow["b"]) == "\\xdeadbeef", repr(orow["b"])
        )
        check(
            "object-path text[] agrees", list(orow["ta"]) == TEXT_ARR, repr(orow["ta"])
        )
        check("object-path int[] agrees", list(orow["ia"]) == INT_ARR, repr(orow["ia"]))

    finally:
        await db.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        await db.disconnect()


def main() -> bool:
    try:
        asyncio.run(run())
    except Exception as e:  # noqa: BLE001
        print(f"\nFATAL: {e!r}")
        check("run() completed without a fatal error", False, repr(e))
    print(f"\n{'=' * 60}")
    return finish()


if __name__ == "__main__":
    run_main(main)
