# PostgreSQL → Python Type Support

Complete mapping of PostgreSQL types to Python native types via the pg.zig native wire protocol.

All conversions happen in Zig at the binary protocol level — no Python parsing overhead, no string intermediaries. Types are returned as native Python objects directly from the C API.

## Scalar Types

| PostgreSQL Type                 | OID  | Python Type                                                 | Format    | Notes                                                |
| ------------------------------- | ---- | ----------------------------------------------------------- | --------- | ---------------------------------------------------- |
| `smallint` / `int2`             | 21   | `int`                                                       | Binary    | 2-byte big-endian                                    |
| `integer` / `int4`              | 23   | `int`                                                       | Binary    | 4-byte big-endian                                    |
| `bigint` / `int8`               | 20   | `int`                                                       | Binary    | 8-byte big-endian                                    |
| `real` / `float4`               | 700  | `float`                                                     | Binary    | IEEE 754 single                                      |
| `double precision` / `float8`   | 701  | `float`                                                     | Binary    | IEEE 754 double                                      |
| `boolean`                       | 16   | `bool`                                                      | Binary    | 1 byte (0/1)                                         |
| `text`                          | 25   | `str`                                                       | Direct    | UTF-8 passthrough                                    |
| `varchar` / `character varying` | 1043 | `str`                                                       | Direct    | UTF-8 passthrough                                    |
| `char` / `character`            | 1042 | `str`                                                       | Direct    | Padded with spaces                                   |
| `name`                          | 19   | `str`                                                       | Direct    | System identifier type                               |
| `"char"`                        | 18   | `str`                                                       | Direct    | Single-byte internal type                            |
| `numeric` / `decimal`           | 1700 | `decimal.Decimal`                                           | Binary    | Base-10000 digit decoding                            |
| `uuid`                          | 2950 | `uuid.UUID`                                                 | Binary    | 16-byte → UUID object                                |
| `timestamp`                     | 1114 | `datetime.datetime`                                         | Binary    | 8-byte microseconds from PG epoch                    |
| `timestamptz`                   | 1184 | `datetime.datetime`                                         | Binary    | 8-byte microseconds from PG epoch                    |
| `date`                          | 1082 | `datetime.date`                                             | Binary    | 4-byte days from PG epoch                            |
| `time`                          | 1083 | `datetime.time`                                             | Binary    | 8-byte microseconds from midnight                    |
| `timetz`                        | 1266 | `datetime.time` (with tzinfo)                               | Binary    | 8-byte usec + 4-byte tz offset                       |
| `interval`                      | 1186 | `datetime.timedelta`                                        | Binary    | 8-byte usec + 4-byte days + 4-byte months            |
| `bytea`                         | 17   | `bytes`                                                     | Direct    | Raw binary data                                      |
| `json`                          | 114  | `dict` / `list` / `str` / `int` / `float` / `bool` / `None` | SIMD JSON | Native SIMD parser (2-10x faster than json.loads)    |
| `jsonb`                         | 3802 | `dict` / `list` / `str` / `int` / `float` / `bool` / `None` | SIMD JSON | Skip version byte, SIMD parse                        |
| `inet`                          | 869  | `ipaddress.IPv4Address` / `IPv6Address`                     | Binary    | 4/16-byte address + family                           |
| `cidr`                          | 650  | `ipaddress.IPv4Network` / `IPv6Network`                     | Binary    | Address + mask bits                                  |
| `money`                         | 790  | `decimal.Decimal` (e.g. `Decimal("1234.56")`)               | Binary    | 8-byte cents → exact Decimal (no float rounding)     |
| `bit`                           | 1560 | `int` (e.g. `179` for `B'10110011'`)                        | Binary    | Bit extraction → Python int (supports bitwise ops)   |
| `varbit` / `bit varying`        | 1562 | `int` (e.g. `10` for `B'1010'`)                             | Binary    | Variable-length → Python int                         |
| `xml`                           | 142  | `str`                                                       | Direct    | XML text passthrough                                 |
| `pg_lsn`                        | 3220 | `str` (e.g. `"16/B374D848"`)                                | Binary    | 8-byte → hex format                                  |
| `tsvector`                      | 3614 | `list[tuple[str, list[int]]]`                               | Binary    | Native binary parse: `[("lexeme", [pos, ...]), ...]` |
| `tsquery`                       | 3615 | `str` (e.g. `"'fat' & 'cat'"`)                              | Binary    | Native binary tree → text reconstruction             |

## Array Types

All arrays use PostgreSQL binary array format: header (ndim, has_null, element_oid, dimensions) followed by element data with per-element length prefix.

| PostgreSQL Type      | OID  | Python Type           | Element Conversion          |
| -------------------- | ---- | --------------------- | --------------------------- |
| `smallint[]`         | 1005 | `list[int]`           | Binary int2                 |
| `integer[]`          | 1007 | `list[int]`           | Binary int4                 |
| `bigint[]`           | 1016 | `list[int]`           | Binary int8                 |
| `real[]`             | 1021 | `list[float]`         | Binary float4 → float64     |
| `double precision[]` | 1022 | `list[float]`         | Binary float8               |
| `boolean[]`          | 1000 | `list[bool]`          | Binary 1-byte               |
| `text[]`             | 1009 | `list[str]`           | Direct UTF-8                |
| `varchar[]`          | 1015 | `list[str]`           | Direct UTF-8                |
| `name[]`             | 1003 | `list[str]`           | Direct UTF-8                |
| `timestamp[]`        | 1115 | `list[datetime]`      | Binary 8-byte usec          |
| `timestamptz[]`      | 1185 | `list[datetime]`      | Binary 8-byte usec          |
| `date[]`             | 1182 | `list[date]`          | Binary 4-byte days          |
| `time[]`             | 1183 | `list[time]`          | Binary 8-byte usec          |
| `numeric[]`          | 1231 | `list[Decimal]`       | Binary base-10000           |
| `uuid[]`             | 2951 | `list[UUID]`          | Binary 16-byte              |
| `bytea[]`            | 1001 | `list[bytes]`         | Direct binary               |
| `jsonb[]`            | 3807 | `list[dict/list/...]` | SIMD JSON parse per element |
| `json[]`             | 199  | `list[dict/list/...]` | SIMD JSON parse per element |
| `oid[]`              | 1028 | `list[int]`           | Binary int4                 |

## Extension Types

| PostgreSQL Type | OID     | Python Type            | Format | Notes                                                                                                                                |
| --------------- | ------- | ---------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `hstore`        | Dynamic | `dict[str, str\|None]` | Binary | OID auto-detected via `pg_type` at connection init. Binary: num_pairs + key_len/key/val_len/val per pair. NULL values become `None`. |

Register hstore at connection time: `_db_register_hstore(pool_handle)` queries `pg_type` for the hstore OID and registers it for native dict conversion. Called automatically in `init_connection_state`.

## NULL Handling

All types return `None` for SQL `NULL` values. Array elements that are NULL also return `None` within the list.

## Fallback

Any OID not listed above falls through to pg.zig's `writeJsonValue` text serialization, which produces a string representation. This covers custom types, range types, composite types, and any future PostgreSQL types.

## Validation

Run the comprehensive OID test:

```bash
uv run hyper-test all_oids
```

This creates a table with every type, inserts test data, queries it back, and verifies Python types and values are correct. Covers 30+ scalar types and 19 array types.
