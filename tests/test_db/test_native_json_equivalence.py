"""Old-path vs new-path JSON equivalence for the native REST fast path.

Item 1 of the WS7 serialization overhaul routes read-only REST list/retrieve
responses for identity ModelSerializers straight through the native
PG-wire→JSON encoder (``db.query_json``), bypassing model hydration and Python
serialization. These tests assert the native path is *byte-identical* to the
existing Python path (model instances → serializer.data → fast_json_dumps) for
every code path (list, retrieve, PageNumber + LimitOffset pagination) over a
model covering all natively-supported column types — including field reordering,
column renames (``source=``), NULLs, unicode and JSON-escaping edge cases.

Since zig/src/pg_render.zig landed, TIMESTAMP(TZ)/DATE/TIME/NUMERIC/UUID/JSONB
are natively rendered too (kitchen-sink equivalence below). A model touching a
genuinely unsupported type (BYTEA) is *not* native-eligible and still serves
correct data via the Python fallback.

Requires PostgreSQL (see tests/test_db/conftest.py). Ports 19300-19340 /
localhost:5432.

Run: uv run pytest tests/test_db/test_native_json_equivalence.py -v
"""

import asyncio
import datetime
import decimal
import os
import uuid

import pytest

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.request import Request
from hyperdjango.rest import (
    LimitOffsetPagination,
    ModelSerializer,
    PageNumberPagination,
    ReadOnlyModelViewSet,
)
from hyperdjango.serializers import SerializerField

# ── Models ──────────────────────────────────────────────────────────────────


class NjWidget(Model):
    class Meta:
        table = "nj_widget"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    qty: int = Field()
    ratio: float = Field()
    active: bool = Field()
    note: str = Field()  # nullable in DB; serializer default None


class NjStamped(Model):
    """TIMESTAMPTZ column — native-eligible since pg_render.zig landed."""

    class Meta:
        table = "nj_stamped"

    id: int = Field(primary_key=True, auto=True)
    label: str = Field()
    created: datetime.datetime = Field()  # → TIMESTAMPTZ


class NjKitchen(Model):
    """All the pg_render.zig-added types in one model."""

    class Meta:
        table = "nj_kitchen"

    id: int = Field(primary_key=True, auto=True)
    created: datetime.datetime = Field()  # TIMESTAMPTZ
    day: datetime.date = Field()  # DATE
    tick: datetime.time = Field()  # TIME
    amount: decimal.Decimal = Field()  # NUMERIC
    uid: uuid.UUID = Field()  # UUID
    meta: dict = Field()  # JSONB


class NjBlob(Model):
    """BYTEA column — genuinely native-ineligible → Python path only."""

    class Meta:
        table = "nj_blob"

    id: int = Field(primary_key=True, auto=True)
    label: str = Field()
    payload: bytes = Field()  # → BYTEA (native-ineligible)


# ── Serializers (identity = pure model-field passthrough) ───────────────────


class WidgetSerializer(ModelSerializer):
    class Meta:
        model = NjWidget
        fields = ["id", "name", "qty", "ratio", "active", "note"]
        read_only_fields = ["id"]


class WidgetReorderedSerializer(ModelSerializer):
    # Deliberately NOT the table/column order — proves output order follows the
    # serializer field order in both paths.
    class Meta:
        model = NjWidget
        fields = ["name", "active", "id", "ratio", "note", "qty"]
        read_only_fields = ["id"]


class WidgetRenamed2Serializer(ModelSerializer):
    # source= renames a column: output key `title` <- column `name`.
    title: str = SerializerField(source="name")

    class Meta:
        model = NjWidget
        fields = ["id", "title", "qty"]
        read_only_fields = ["id"]


class StampedSerializer(ModelSerializer):
    class Meta:
        model = NjStamped
        fields = ["id", "label", "created"]
        read_only_fields = ["id"]


class KitchenSerializer(ModelSerializer):
    class Meta:
        model = NjKitchen
        fields = ["id", "created", "day", "tick", "amount", "uid", "meta"]
        read_only_fields = ["id"]


class BlobSerializer(ModelSerializer):
    class Meta:
        model = NjBlob
        fields = ["id", "label", "payload"]
        read_only_fields = ["id"]


# ── ViewSets ────────────────────────────────────────────────────────────────


class WidgetVS(ReadOnlyModelViewSet):
    serializer_class = WidgetSerializer
    model = NjWidget


class WidgetPageVS(ReadOnlyModelViewSet):
    serializer_class = WidgetSerializer
    model = NjWidget
    pagination_class = PageNumberPagination


class WidgetLimitVS(ReadOnlyModelViewSet):
    serializer_class = WidgetSerializer
    model = NjWidget
    pagination_class = LimitOffsetPagination


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_db():
    user = os.environ.get("USER", "postgres")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    import subprocess

    subprocess.run(["createdb", dbname], capture_output=True)
    db = Database(
        f"postgresql://{user}:@localhost:5432/{dbname}", min_size=1, max_size=3
    )

    async def _setup():
        await db.connect()
        set_db(db)
        await db.execute("DROP TABLE IF EXISTS nj_widget")
        await db.execute(
            """CREATE TABLE nj_widget (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                qty INTEGER NOT NULL,
                ratio DOUBLE PRECISION NOT NULL,
                active BOOLEAN NOT NULL,
                note TEXT
            )"""
        )
        rows = [
            ("Alpha", 3, 1.5, True, "plain"),
            ('quote "x"\nline', 0, 0.0, False, None),  # escaping + NULL + zero
            ("Ünîçødé ✓", -7, -2.25, True, ""),  # unicode + empty string
            ("Zeta", 999999, 3.141592653589793, False, "last"),
        ]
        for i, (name, qty, ratio, active, note) in enumerate(rows, start=1):
            await db.execute(
                "INSERT INTO nj_widget (name, qty, ratio, active, note) "
                "VALUES ($1,$2,$3,$4,$5)",
                name,
                qty,
                ratio,
                active,
                note,
            )
        await db.execute("DROP TABLE IF EXISTS nj_stamped")
        await db.execute(
            """CREATE TABLE nj_stamped (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL,
                created TIMESTAMPTZ NOT NULL
            )"""
        )
        await db.execute(
            "INSERT INTO nj_stamped (label, created) VALUES ($1, $2)",
            "evt",
            "2024-01-15T10:30:00Z",
        )
        await db.execute("DROP TABLE IF EXISTS nj_kitchen")
        await db.execute(
            """CREATE TABLE nj_kitchen (
                id SERIAL PRIMARY KEY,
                created TIMESTAMPTZ NOT NULL,
                day DATE NOT NULL,
                tick TIME NOT NULL,
                amount NUMERIC NOT NULL,
                uid UUID NOT NULL,
                meta JSONB NOT NULL
            )"""
        )
        await db.execute(
            "INSERT INTO nj_kitchen (created, day, tick, amount, uid, meta) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            "2024-06-30T23:59:59.123456Z",
            "2024-02-29",
            "07:05:00.5",
            "1000.5000",
            "A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11",
            '{"k": "v", "n": 1, "nested": {"a": [1, 2, null]}}',
        )
        await db.execute(
            "INSERT INTO nj_kitchen (created, day, tick, amount, uid, meta) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            "1999-01-01T00:00:00Z",
            "1999-12-31",
            "00:00:00",
            "-0.01",
            "00000000-0000-0000-0000-000000000000",
            "[]",
        )
        await db.execute("DROP TABLE IF EXISTS nj_blob")
        await db.execute(
            """CREATE TABLE nj_blob (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL,
                payload BYTEA NOT NULL
            )"""
        )
        await db.execute(
            "INSERT INTO nj_blob (label, payload) VALUES ($1, $2)",
            "evt",
            b"\x00\x01binary",
        )

    asyncio.run(_setup())
    yield db

    async def _teardown():
        await db.execute("DROP TABLE IF EXISTS nj_widget")
        await db.execute("DROP TABLE IF EXISTS nj_stamped")
        await db.execute("DROP TABLE IF EXISTS nj_kitchen")
        await db.execute("DROP TABLE IF EXISTS nj_blob")

    asyncio.run(_teardown())


def _make_request(query_string: str = "") -> Request:
    return Request(
        method="GET",
        path="/api/widgets/",
        headers={"accept": "application/json"},
        query_string=query_string,
    )


def _mk_view(view_cls, request, kwargs=None, action="list"):
    v = view_cls()
    v.request = request
    v.kwargs = kwargs or {}
    v.action = action
    return v


async def _both_paths_list(view_cls, request, kwargs=None, action="list"):
    """Return (native_body, python_body) for the same list/retrieve call."""
    native_view = _mk_view(view_cls, request, kwargs, action)
    assert native_view._can_use_native_json(), (
        f"{view_cls.__name__} expected to be native-eligible"
    )
    native_resp = await getattr(native_view, action)(request)

    py_view = _mk_view(view_cls, request, kwargs, action)
    py_view.use_native_json = False  # force Python hydrate+serialize path
    assert not py_view._can_use_native_json()
    py_resp = await getattr(py_view, action)(request)

    return native_resp.body, py_resp.body


# ── Tests ───────────────────────────────────────────────────────────────────


def test_native_eligibility_flags(live_db):
    assert WidgetSerializer._native_select_columns is not None
    assert WidgetReorderedSerializer._native_select_columns is not None
    assert WidgetRenamed2Serializer._native_select_columns is not None
    # renamed → (column, output_key)
    assert ("name", "title") in WidgetRenamed2Serializer._native_select_columns
    # pg_render.zig types are now native-eligible.
    assert StampedSerializer._native_select_columns is not None
    assert KitchenSerializer._native_select_columns is not None
    # A genuinely unsupported column type (BYTEA) disqualifies the serializer.
    assert BlobSerializer._native_select_columns is None


def test_list_equivalence(live_db):
    native, py = asyncio.run(_both_paths_list(WidgetVS, _make_request()))
    assert native == py, f"\nnative: {native}\npython: {py}"
    # sanity: real content, not both-empty
    assert native.startswith(b"[{") and b"Alpha" in native


def test_list_equivalence_reordered_fields(live_db):
    class VS(ReadOnlyModelViewSet):
        serializer_class = WidgetReorderedSerializer
        model = NjWidget

    native, py = asyncio.run(_both_paths_list(VS, _make_request()))
    assert native == py, f"\nnative: {native}\npython: {py}"
    # First key must be `name` (serializer order), not `id` (table order).
    assert native.startswith(b'[{"name":')


def test_list_equivalence_renamed_source(live_db):
    class VS(ReadOnlyModelViewSet):
        serializer_class = WidgetRenamed2Serializer
        model = NjWidget

    native, py = asyncio.run(_both_paths_list(VS, _make_request()))
    assert native == py, f"\nnative: {native}\npython: {py}"
    assert b'"title":' in native and b'"name":' not in native


def test_retrieve_equivalence(live_db):
    req = _make_request()
    native, py = asyncio.run(
        _both_paths_list(WidgetVS, req, kwargs={"id": 2}, action="retrieve")
    )
    assert native == py, f"\nnative: {native}\npython: {py}"
    assert native.startswith(b"{") and b"quote" in native


def test_page_number_pagination_equivalence(live_db):
    for qs in ("page=1&page_size=2", "page=2&page_size=2", "page=2&page_size=3"):
        req = _make_request(qs)
        native, py = asyncio.run(_both_paths_list(WidgetPageVS, req))
        assert native == py, f"\nquery={qs}\nnative: {native}\npython: {py}"
        assert b'"count":4' in native
        assert b'"results":[' in native


def test_limit_offset_pagination_equivalence(live_db):
    for qs in ("limit=2&offset=0", "limit=2&offset=2", "limit=3&offset=1"):
        req = _make_request(qs)
        native, py = asyncio.run(_both_paths_list(WidgetLimitVS, req))
        assert native == py, f"\nquery={qs}\nnative: {native}\npython: {py}"
        assert b'"count":4' in native


def test_timestamp_equivalence(live_db):
    class VS(ReadOnlyModelViewSet):
        serializer_class = StampedSerializer
        model = NjStamped

    native, py = asyncio.run(_both_paths_list(VS, _make_request()))
    assert native == py, f"\nnative: {native}\npython: {py}"
    assert b'"created":"2024-01-15T10:30:00' in native


def test_kitchen_sink_equivalence(live_db):
    """TIMESTAMPTZ/DATE/TIME/NUMERIC/UUID/JSONB — byte-identical to Python."""

    class VS(ReadOnlyModelViewSet):
        serializer_class = KitchenSerializer
        model = NjKitchen

    native, py = asyncio.run(_both_paths_list(VS, _make_request()))
    assert native == py, f"\nnative: {native}\npython: {py}"
    assert b'"uid":"a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"' in native
    assert b'"day":"2024-02-29"' in native


def test_unsupported_type_uses_python_path(live_db):
    class VS(ReadOnlyModelViewSet):
        serializer_class = BlobSerializer
        model = NjBlob

    v = _mk_view(VS, _make_request(), action="list")
    assert not v._can_use_native_json()
    resp = asyncio.run(v.list(_make_request()))
    assert resp.status == 200
    assert b'"label":"evt"' in resp.body
