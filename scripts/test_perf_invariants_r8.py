"""Behavior-preservation invariants for the round-8 perf/scalability pass.

Every change in this round removes REDUNDANT work (extra encodes, per-call SQL
rebuilds, per-row manager/renderer resolution, double dict lookups) while keeping
outputs byte-identical. These tests pin the *outputs*, not the speed — they prove
the optimizations didn't change behavior, without needing a benchmark or a live DB.

Run:  uv run python scripts/test_perf_invariants_r8.py
(also collected by pytest via the test_* names.)
"""

# hyper-test: unit

from __future__ import annotations

import asyncio
import traceback

from hyperdjango import rest
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.expressions import Exists, OuterRef
from hyperdjango.models import Field, Model
from hyperdjango.native import fast_json_dumps
from hyperdjango.query import QuerySet, _row_val
from hyperdjango.realtime import ConnectionInfo, ConnectionManager
from hyperdjango.rest import (
    _ENCODE_PK_STRATEGIES,
    JSONRenderer,
    ViewSet,
    _renderer_instance,
)
from hyperdjango.testkit import check, finish, run_main

# ---------------------------------------------------------------------------
# Fix 1 — realtime.py: broadcast / send_to_user encode ONCE, fan out N sends.
# ---------------------------------------------------------------------------


class _NativeWS:
    """Fake native WebSocket exposing the _send_text_bytes fast path."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def _send_text_bytes(self, data: bytes) -> None:
        self.sent.append(data)


class _AsgiWS:
    """Fake ASGI WebSocket without _send_text_bytes (fallback path)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _make_manager():
    mgr = ConnectionManager(layer=object())  # layer unused by broadcast/send_to_user
    return mgr, ConnectionInfo


def _register(mgr, ConnectionInfo, cid, user_id, ws):
    info = ConnectionInfo(
        connection_id=cid,
        user_id=user_id,
        ws=ws,
        connected_at=0.0,
        rooms=set(),
        metadata={},
    )
    mgr._connections[cid] = info
    if user_id is not None:
        mgr._user_connections.setdefault(user_id, set()).add(cid)


def test_broadcast_encodes_once_and_delivers_identical_payload():
    mgr, CI = _make_manager()
    data = {"type": "announcement", "n": 3, "text": "héllo ünicode", "arr": [1, 2, 3]}
    expected = fast_json_dumps(data)  # single canonical encode

    ws1, ws2, ws3 = _NativeWS(), _NativeWS(), _NativeWS()
    _register(mgr, CI, "c1", "u1", ws1)
    _register(mgr, CI, "c2", "u1", ws2)
    _register(mgr, CI, "c3", "u2", ws3)

    count = asyncio.run(mgr.broadcast(data))
    assert count == 3
    for ws in (ws1, ws2, ws3):
        assert ws.sent == [expected], "each socket must get the one canonical payload"
    # Every socket received the EXACT same bytes object semantics (byte-identical).
    assert ws1.sent[0] == ws2.sent[0] == ws3.sent[0] == expected


def test_broadcast_fallback_path_matches_send_json_bytes():
    mgr, CI = _make_manager()
    data = {"a": 1, "b": "two"}
    expected_text = fast_json_dumps(data).decode()  # what ASGI send_json would emit

    ws = _AsgiWS()
    _register(mgr, CI, "c1", None, ws)
    count = asyncio.run(mgr.broadcast(data))
    assert count == 1
    assert ws.sent == [expected_text]


def test_send_to_user_encodes_once_for_all_user_sockets():
    mgr, CI = _make_manager()
    data = {"type": "update", "payload": {"k": "v"}}
    expected = fast_json_dumps(data)

    a, b = _NativeWS(), _NativeWS()
    _register(mgr, CI, "c1", "u1", a)
    _register(mgr, CI, "c2", "u1", b)
    _register(mgr, CI, "c3", "u2", _NativeWS())  # different user, must NOT receive

    count = asyncio.run(mgr.send_to_user("u1", data))
    assert count == 2
    assert a.sent == [expected]
    assert b.sent == [expected]
    assert mgr._connections["c3"].ws.sent == []


# ---------------------------------------------------------------------------
# Fix 2 — models.py: memoized INSERT/UPDATE/DELETE SQL is byte-identical to a
#         freshly rebuilt one (the pre-memoization formulas, replicated here).
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []

    async def execute(self, sql, *params):
        self.calls.append(("execute", sql, params))
        return 1

    async def query_val(self, sql, *params):
        self.calls.append(("query_val", sql, params))
        return 999


def _model_cls():
    class R8Post(Model):
        class Meta:
            table = "r8_posts"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        body: str = Field()

    return R8Post


def _expected_insert_sql(meta, columns):
    """Original (pre-memoization) INSERT formula."""
    placeholders = ", ".join(
        f"${i + 1}::vector" if columns[i] in meta.vector_columns else f"${i + 1}"
        for i in range(len(columns))
    )
    col_names = ", ".join(columns)
    if columns:
        sql = f"INSERT INTO {meta.table} ({col_names}) VALUES ({placeholders})"
    else:
        sql = f"INSERT INTO {meta.table} DEFAULT VALUES"
    if meta.auto_field:
        sql += f" RETURNING {meta.auto_field}"
    return sql


def test_insert_sql_memoized_equals_freshly_built():
    cls = _model_cls()
    meta = cls._meta
    inst = cls(name="a", body="b")
    db = _FakeDB()
    asyncio.run(inst._insert(db, meta))

    columns = ("name", "body")  # non-auto writable columns, all present
    expected = _expected_insert_sql(meta, columns)
    got_sql = db.calls[-1][1]
    assert got_sql == expected, f"{got_sql!r} != {expected!r}"
    # Cache populated, and a second insert yields the byte-identical SQL.
    assert columns in meta._insert_sql_cache
    db2 = _FakeDB()
    asyncio.run(cls(name="c", body="d")._insert(db2, meta))
    assert db2.calls[-1][1] == got_sql


def test_update_sql_memoized_equals_freshly_built():
    cls = _model_cls()
    meta = cls._meta
    inst = cls(id=5, name="a", body="b")
    inst._loaded_from_db = True
    db = _FakeDB()
    asyncio.run(inst._update(db, meta))

    columns = ("name", "body")
    set_clauses = ", ".join(
        f"{c} = ${i + 1}::vector" if c in meta.vector_columns else f"{c} = ${i + 1}"
        for i, c in enumerate(columns)
    )
    where = meta.pk_where_clause(start_param=len(columns) + 1)
    expected = f"UPDATE {meta.table} SET {set_clauses} WHERE {where}"
    got_sql = db.calls[-1][1]
    assert got_sql == expected, f"{got_sql!r} != {expected!r}"
    # Params: column values then PK value(s), in the same order as before.
    assert db.calls[-1][2] == ("a", "b", 5)
    assert meta.writable_columns in meta._update_sql_cache


def test_delete_sql_memoized_equals_freshly_built():
    cls = _model_cls()
    meta = cls._meta
    inst = cls(id=7, name="a", body="b")
    inst._loaded_from_db = True
    db = _FakeDB()
    asyncio.run(inst.delete(db))

    where = meta.pk_where_clause(start_param=1)
    expected = f"DELETE FROM {meta.table} WHERE {where}"
    got = db.calls[-1]
    assert got[1] == expected, f"{got[1]!r} != {expected!r}"
    assert got[2] == (7,)
    assert meta._delete_sql_cache == expected


# ---------------------------------------------------------------------------
# Fix 3 — rest.py: resolve-once + mutate-in-place + native-columns cache + singletons.
# ---------------------------------------------------------------------------


def test_encode_response_ids_resolves_once_and_mutates_in_place():
    strat = next(s for s in _ENCODE_PK_STRATEGIES)
    calls = {"n": 0}

    class _V:
        def _get_public_id_strategy(self):
            return strat

        def _make_pk_encoder(self, request=None):
            calls["n"] += 1
            return lambda pk: f"enc:{pk}"

        _encode_response_ids = ViewSet._encode_response_ids
        _encode_single_item_id = ViewSet._encode_single_item_id

    v = _V()
    rows = [{"id": 1, "x": "a"}, {"id": 2, "x": "b"}, {"id": 3, "x": "c"}]
    out = v._encode_response_ids(rows, request=None)

    # Encoder resolved exactly ONCE for the whole list (not per row).
    assert calls["n"] == 1
    assert out == [
        {"id": "enc:1", "x": "a"},
        {"id": "enc:2", "x": "b"},
        {"id": "enc:3", "x": "c"},
    ]
    # Mutated in place — same dict objects, no defensive copies.
    assert out[0] is rows[0] and out[2] is rows[2]


def test_native_columns_sql_cached_and_byte_identical():
    class _Meta:
        table = "r8_posts"

    class _Model:
        _meta = _Meta

    class _Serializer:
        _native_select_columns = [("id", "id"), ("name", "name"), ("body", "content")]

        class Meta:
            model = _Model

    class _View:
        model = _Model

        def get_serializer_class(self):
            return _Serializer

    rest._NATIVE_COLUMNS_SQL_CACHE.clear()
    view = _View()
    first = rest._native_columns_sql(view)
    expected = "r8_posts.id, r8_posts.name, r8_posts.body AS content"
    assert first == expected, f"{first!r} != {expected!r}"
    assert (_Serializer, "r8_posts") in rest._NATIVE_COLUMNS_SQL_CACHE
    # Second call returns the cached, byte-identical string.
    assert rest._native_columns_sql(view) == first


def test_renderer_instances_are_reused_singletons():
    a = _renderer_instance(JSONRenderer)
    b = _renderer_instance(JSONRenderer)
    assert a is b, "stateless renderer must be reused, not re-instantiated"
    assert isinstance(a, JSONRenderer)


# ---------------------------------------------------------------------------
# Fix 4 — query.py: _has_exists tracking + _row_val idx-0 fast path.
# ---------------------------------------------------------------------------


def test_row_val_idx0_matches_original_dict_semantics():
    # Non-empty dict: first inserted value.
    assert _row_val({"id": 42, "name": "x"}, 0) == 42
    # Empty dict: None (matches original `idx < len(keys)` guard).
    assert _row_val({}, 0) is None
    # Non-zero index still works via the general path.
    assert _row_val({"a": 1, "b": 2, "c": 3}, 2) == 3
    assert _row_val({"a": 1}, 5) is None
    # Tuple/list rows unchanged.
    assert _row_val((10, 20, 30), 0) == 10
    assert _row_val([10, 20, 30], 2) == 30


def test_has_exists_flag_matches_full_scan():
    class R8A(Model):
        class Meta:
            table = "r8_a"

        id: int = Field(primary_key=True, auto=True)
        n: int = Field()

    class R8B(Model):
        class Meta:
            table = "r8_b"

        id: int = Field(primary_key=True, auto=True)
        a_id: int = Field()

    def full_scan(qs):
        return any(k == "__exists__" for k, _ in (qs._filters + qs._excludes))

    base = QuerySet(R8A)
    assert base._has_exists is False and full_scan(base) is False

    plain = base.filter(n=1)
    assert plain._has_exists is False and full_scan(plain) is False

    sub = QuerySet(R8B).filter(a_id=OuterRef("id"))
    fexists = base.filter(Exists(sub))
    assert fexists._has_exists is True and full_scan(fexists) is True

    eexists = base.exclude(Exists(sub))
    assert eexists._has_exists is True and full_scan(eexists) is True

    # Chaining a plain filter after an exists keeps the flag set (monotonic).
    chained = fexists.filter(n=2)
    assert chained._has_exists is True and full_scan(chained) is True


# ---------------------------------------------------------------------------
# Fix 5 — conf.py: get_setting single-lookup returns identical values.
# ---------------------------------------------------------------------------


def test_get_setting_defaults_and_missing():
    # A DEFAULTS key with no env/Django override returns its DEFAULTS value.
    for key in ("POOL_SIZE", "PREPARED_STATEMENTS", "CONNECT_TIMEOUT"):
        if key in DEFAULTS:
            assert get_setting(key) == DEFAULTS[key]
    # A genuinely-absent key returns the caller-supplied default.
    sentinel = object()
    assert get_setting("R8_DEFINITELY_NOT_A_SETTING", sentinel) is sentinel


# Each test is a bare-assert battery: the first failing assert aborts the file,
# which is the contract here. Counting is therefore at test granularity.
def main() -> bool:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            traceback.print_exc()
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            finish()
            return False
        check(fn.__name__, True)
    return finish()


if __name__ == "__main__":
    run_main(main)
