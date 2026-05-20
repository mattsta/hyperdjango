#!/usr/bin/env python3
"""Round-8 regression tests: observability infrastructure + swallowed-exception
bugs + a large-message-loss regression.

Covers the correctness/visibility bugs fixed in this round:

1. PgChannelLayer._deliver_ref deletes the staging row ONLY after a successful
   parse+delivery — a parse/delivery failure leaves the row for the TTL reaper
   (no permanent silent loss of >7500B messages).
2. Channels cross-process drops (on_notify / _deliver_ref) log at ERROR with
   traceback, not DEBUG.
3. Stdlib `logging` records are routed through the framework sink via the
   InterceptHandler installed on the root logger.
4. JsonSink serializes the *formatted traceback string* into the exception
   object, not a bare boolean.
5. ReadReplicaRouter recovers after a transient connection error (cooldown +
   re-probe) and does NOT self-disable on an unexpected, non-connection error.
6. DatabaseRateLimitBackend.cleanup retains rows for the LARGEST configured
   window (a >1h window is preserved), not a hard-coded hour.
7. DatabaseCache.incr resets an expired counter (expiry-aware CASE) instead of
   incrementing a stale value forever.
8. ModelForm keeps ALL model fields when it also declares an extra field.
9. FloatField/DecimalField reject NaN/Inf; DecimalField enforces precision.

All assertions run WITHOUT a live database (fakes + direct unit checks) and
against the installed native extension.

Usage:
    uv run hyper-test observability_swallows_r8
    uv run python scripts/test_observability_swallows_r8.py
"""

# hyper-test: unit

import asyncio
import inspect
import json
import sys
import time
import traceback
import types
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Install a fake `django` package BEFORE importing the router so the lazy
# connection-error-type resolution and connections lookup use our doubles.
# ---------------------------------------------------------------------------


class _OperationalError(Exception):
    pass


class _InterfaceError(Exception):
    pass


class _ConnectionDoesNotExist(Exception):
    pass


class _FakeConn:
    def __init__(self):
        self.mode = "ok"  # "ok" | "conn_fail" | "weird"

    def ensure_connection(self):
        if self.mode == "conn_fail":
            raise _OperationalError("replica down")
        if self.mode == "weird":
            raise ValueError("unexpected router bug")


class _FakeConnections:
    def __init__(self):
        self._conns = {"replica": _FakeConn(), "default": _FakeConn()}

    def __getitem__(self, alias):
        return self._conns[alias]


_FAKE_CONNECTIONS = _FakeConnections()


def _install_fake_django():
    django = types.ModuleType("django")
    db = types.ModuleType("django.db")
    utils = types.ModuleType("django.db.utils")
    utils.OperationalError = _OperationalError
    utils.InterfaceError = _InterfaceError
    utils.ConnectionDoesNotExist = _ConnectionDoesNotExist
    db.connections = _FAKE_CONNECTIONS
    db.utils = utils
    django.db = db
    sys.modules.setdefault("django", django)
    sys.modules.setdefault("django.db", db)
    sys.modules.setdefault("django.db.utils", utils)


_install_fake_django()

from hyperdjango.cache import DatabaseCache  # noqa: E402
from hyperdjango.channels import Message, PgChannelLayer  # noqa: E402
from hyperdjango.db.routers import ReadReplicaRouter  # noqa: E402
from hyperdjango.forms import (  # noqa: E402
    CharField,
    DecimalField,
    FloatField,
    ModelForm,
)
from hyperdjango.logging import (  # noqa: E402
    InterceptHandler,
    JsonSink,
    RecordException,
    RecordFile,
    RecordLevel,
    RecordProcess,
    RecordThread,
    logger,
)
from hyperdjango.ratelimit import DatabaseRateLimitBackend  # noqa: E402

RESULTS = {"passed": 0, "failed": 0, "skipped": 0, "errors": []}


class _Skip(Exception):
    pass


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ok  {name}")
            except _Skip as e:
                RESULTS["skipped"] += 1
                print(f"  -   {name} (skipped: {e})")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  FAIL {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDB:
    """Records queries; returns programmable rows."""

    def __init__(self, query_rows=None, query_one_row=None):
        self.calls = []
        self._query_rows = query_rows if query_rows is not None else []
        self._query_one_row = query_one_row

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return 1

    async def query(self, sql, *args):
        self.calls.append(("query", sql, args))
        return self._query_rows

    async def query_one(self, sql, *args):
        self.calls.append(("query_one", sql, args))
        return self._query_one_row

    async def query_val(self, sql, *args):
        self.calls.append(("query_val", sql, args))
        return 0

    def transaction(self, *args, **kwargs):
        # Rate-limit check-and-increment now runs atomically inside a
        # transaction (pg_advisory_xact_lock); provide a no-op async CM that
        # yields this same fake so the recorded queries still flow through.
        fake = self

        class _Tx:
            async def __aenter__(self_):
                return fake

            async def __aexit__(self_, *exc):
                return False

        return _Tx()

    def executed_sql(self):
        return [sql for kind, sql, _ in self.calls if kind == "execute"]

    def deletes(self):
        return [
            (sql, args)
            for kind, sql, args in self.calls
            if kind == "execute" and "DELETE" in sql
        ]


class _RecordingChannel:
    def __init__(self, fail=False):
        self.delivered = []
        self.fail = fail

    async def _deliver(self, msg):
        if self.fail:
            raise RuntimeError("subscriber blew up")
        self.delivered.append(msg)


# ---------------------------------------------------------------------------
# 1 + 2: _deliver_ref ordering (staging row survives a failed delivery)
# ---------------------------------------------------------------------------


@test("deliver_ref keeps staging row on parse failure (no DELETE)")
async def test_deliver_ref_parse_failure_keeps_row():
    layer = PgChannelLayer(database_url="")
    # Staged payload is invalid (missing "channel") → Message.from_json raises.
    layer._db = FakeDB(query_rows=[{"payload": '{"data": {"x": 1}}'}])
    await layer._deliver_ref(42)
    assert layer._db.deletes() == [], (
        f"row must NOT be deleted on parse failure, got {layer._db.deletes()}"
    )


@test("deliver_ref keeps staging row on delivery failure (no DELETE)")
async def test_deliver_ref_delivery_failure_keeps_row():
    layer = PgChannelLayer(database_url="")
    payload = Message(channel="room1", data={"big": "x" * 10}).to_json()
    layer._db = FakeDB(query_rows=[{"payload": payload}])
    layer._channels["room1"] = _RecordingChannel(fail=True)
    await layer._deliver_ref(7)
    assert layer._db.deletes() == [], (
        f"row must NOT be deleted when delivery fails, got {layer._db.deletes()}"
    )


@test("deliver_ref delivers but does NOT delete (multi-node: TTL owns cleanup)")
async def test_deliver_ref_success_does_not_delete():
    # In a multi-node deployment every listening node receives the same _ref
    # NOTIFY and must independently read the staged payload to deliver to ITS
    # subscribers. If _deliver_ref deleted the row on delivery it would race the
    # other nodes and starve their subscribers (3+ nodes → all but one miss it).
    # Cleanup is time-based only (the shared table's TTL reaper), so a successful
    # _deliver_ref delivers the message and issues NO DELETE.
    layer = PgChannelLayer(database_url="")
    payload = Message(channel="room2", data={"big": "y" * 10}).to_json()
    layer._db = FakeDB(query_rows=[{"payload": payload}])
    ch = _RecordingChannel(fail=False)
    layer._channels["room2"] = ch
    await layer._deliver_ref(9)
    assert len(ch.delivered) == 1, "message should have been delivered"
    assert layer._db.deletes() == [], (
        f"_deliver_ref must not delete the row (TTL reaper owns it), "
        f"got {layer._db.deletes()}"
    )


# ---------------------------------------------------------------------------
# 3: stdlib logging routed through framework sink via InterceptHandler
# ---------------------------------------------------------------------------


@test("InterceptHandler installed on stdlib root logger")
def test_intercept_installed():
    import logging as std

    root = std.getLogger()
    assert any(isinstance(h, InterceptHandler) for h in root.handlers), (
        "InterceptHandler must be attached to the stdlib root logger by auto-init"
    )


@test("stdlib logging record flows through the framework sink")
def test_stdlib_routed_to_sink():
    import logging as std

    captured = []

    def _capture(record, message):
        captured.append(record)

    sink_id = logger.add(_capture, level="DEBUG")
    try:
        std.getLogger("r8.observability.probe").error("stdlib-bridge-probe-xyz")
        # Records may be dispatched via the background writer thread; poll.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if any(r.get("message") == "stdlib-bridge-probe-xyz" for r in captured):
                break
            time.sleep(0.02)
    finally:
        logger.remove(sink_id)

    hit = [r for r in captured if r.get("message") == "stdlib-bridge-probe-xyz"]
    assert hit, "stdlib ERROR record never reached the framework sink"
    assert hit[0]["level"].name == "ERROR", (
        f"level should map to ERROR, got {hit[0]['level'].name}"
    )


@test("InterceptHandler forwards exc_info to the framework record")
def test_intercept_forwards_exc_info():
    import logging as std

    captured = []
    sink_id = logger.add(lambda record, message: captured.append(record), level="DEBUG")
    try:
        try:
            raise ValueError("intercept-exc-probe-abc")
        except ValueError:
            std.getLogger("r8.observability.exc").exception("boom-intercept")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if any(r.get("message") == "boom-intercept" for r in captured):
                break
            time.sleep(0.02)
    finally:
        logger.remove(sink_id)

    hit = [r for r in captured if r.get("message") == "boom-intercept"]
    assert hit, "intercepted exception record never reached the sink"
    exc = hit[0].get("exception")
    assert exc is not None and exc.type is ValueError, (
        f"exc_info should propagate as RecordException(ValueError), got {exc}"
    )


# ---------------------------------------------------------------------------
# 4: JsonSink emits a real traceback STRING
# ---------------------------------------------------------------------------


@test("JsonSink serializes a formatted traceback string, not a bool")
def test_json_sink_traceback_string():
    import io

    try:
        raise ValueError("json-tb-probe-777")
    except ValueError:
        ei = sys.exc_info()
        exc = RecordException(ei[0], ei[1], ei[2])

    now = datetime.now(UTC)
    record = {
        "level": RecordLevel("ERROR", 40),
        "file": RecordFile("t.py", "/tmp/t.py"),
        "function": "f",
        "line": 1,
        "message": "kaboom",
        "module": "t",
        "name": "r8.json",
        "thread": RecordThread(1, "main"),
        "process": RecordProcess(1, "proc"),
        "time": now,
        "elapsed": timedelta(seconds=1),
        "exception": exc,
        "extra": {},
    }
    buf = io.StringIO()
    JsonSink(stream=buf).write("kaboom", record)
    obj = json.loads(buf.getvalue().strip())
    tb = obj["exception"]["traceback"]
    assert isinstance(tb, str), f"traceback must be a string, got {type(tb)}: {tb!r}"
    assert tb is not True and tb is not False
    assert "Traceback" in tb, f"traceback string should contain frames: {tb!r}"
    assert "json-tb-probe-777" in tb, "traceback should include the exception message"
    assert obj["exception"]["type"] == "ValueError"


# ---------------------------------------------------------------------------
# 5: ReadReplicaRouter recovery + no-permanent-disable
# ---------------------------------------------------------------------------


@test("replica router recovers after a transient connection error")
def test_router_recovers():
    conn = _FAKE_CONNECTIONS["replica"]
    conn.mode = "ok"
    router = ReadReplicaRouter(health_cooldown_seconds=0.0)

    assert router.db_for_read(None) == "replica", "healthy replica should be used"

    conn.mode = "conn_fail"
    assert router.db_for_read(None) == "default", "connection failure routes to primary"
    assert router._replica_healthy is False, "should be marked unhealthy"

    # Recovery: connection restored, cooldown is 0 → next read re-probes.
    conn.mode = "ok"
    assert router.db_for_read(None) == "replica", "should recover and resume replica"
    assert router._replica_healthy is True
    conn.mode = "ok"


@test("replica router does NOT permanently disable on an unexpected error")
def test_router_no_disable_on_weird_error():
    conn = _FAKE_CONNECTIONS["replica"]
    conn.mode = "weird"  # ValueError — not a connection error
    router = ReadReplicaRouter(health_cooldown_seconds=30.0)

    assert router.db_for_read(None) == "default", (
        "unexpected error falls back to primary"
    )
    # Must NOT self-disable: a later healthy read still uses the replica.
    assert router._replica_healthy is True, "must not disable on non-connection error"
    conn.mode = "ok"
    assert router.db_for_read(None) == "replica"


# ---------------------------------------------------------------------------
# 6: DatabaseRateLimitBackend.cleanup preserves a >1h window
# ---------------------------------------------------------------------------


@test("ratelimit cleanup retains rows for the largest window (>1h)")
async def test_ratelimit_cleanup_preserves_large_window():
    db = FakeDB(query_one_row={"total": 0})
    backend = DatabaseRateLimitBackend(db)

    # Daily quota: 86400s window.
    await backend.check_and_increment("k", max_requests=1000, window=86400)
    assert backend._max_window_seconds == 86400, (
        f"max window should track the daily window, got {backend._max_window_seconds}"
    )

    db.calls.clear()
    await backend.cleanup()
    cleanup_sql = db.executed_sql()
    assert len(cleanup_sql) == 1
    sql = cleanup_sql[0]
    assert "INTERVAL '1 hour'" not in sql, (
        "cleanup must not hard-code a 1-hour retention"
    )
    # Retention must be parameterized by the max window (86400s), not ~1h.
    _, _, args = db.calls[-1]
    assert 86400 in args, f"cleanup must retain the largest window, params={args}"


# ---------------------------------------------------------------------------
# 7: DatabaseCache.incr resets an expired counter
# ---------------------------------------------------------------------------


@test("cache incr uses expiry-aware CASE (resets expired counter)")
async def test_incr_expiry_aware():
    db = FakeDB(query_one_row={"counter": 5})
    cache = DatabaseCache(db, default_ttl=60)
    await cache.incr("hits", 5)
    upsert = [sql for kind, sql, _ in db.calls if kind == "query_one"]
    assert upsert, "incr should issue an upsert query"
    sql = upsert[0]
    # Must branch on expiry rather than blindly incrementing a stale value.
    assert "CASE WHEN hyper_cache.expires_at <= NOW()" in sql, (
        "incr must reset the counter/expires_at when the row is expired"
    )
    assert "expires_at = CASE WHEN hyper_cache.expires_at <= NOW()" in sql, (
        "incr must refresh expires_at on reset"
    )


# ---------------------------------------------------------------------------
# 8: ModelForm keeps model fields alongside an extra declared field
# ---------------------------------------------------------------------------


class _FakeFieldMeta:
    def __init__(self, auto=False):
        self.auto = auto


class _FakeModelMeta:
    def __init__(self, fields):
        self.fields = fields


class _FakeUserModel:
    __annotations__ = {"username": str, "email": str}
    _meta = _FakeModelMeta(
        {
            "id": _FakeFieldMeta(auto=True),
            "username": _FakeFieldMeta(),
            "email": _FakeFieldMeta(),
        }
    )


@test("ModelForm keeps model fields when it also declares an extra field")
def test_modelform_keeps_model_fields():
    class UserForm(ModelForm):
        captcha = CharField(required=False)  # non-model extra field

        class Meta:
            model = _FakeUserModel
            fields = ["username", "email"]

    declared = set(UserForm._declared_fields)
    assert "username" in declared and "email" in declared, (
        f"model fields must survive an extra declared field; got {declared}"
    )
    assert "captcha" in declared, f"extra field must be present; got {declared}"


# ---------------------------------------------------------------------------
# 9: FloatField / DecimalField reject NaN/Inf; DecimalField enforces precision
# ---------------------------------------------------------------------------


@test("FloatField rejects NaN and Infinity")
def test_float_rejects_nonfinite():
    f = FloatField()
    assert f.clean("3.5") == 3.5
    for bad in ("nan", "inf", "-inf", "Infinity"):
        try:
            f.clean(bad)
        except ValueError:
            continue
        raise AssertionError(f"FloatField accepted non-finite {bad!r}")


@test("DecimalField rejects non-finite and enforces precision")
def test_decimal_rejects_nonfinite_and_precision():
    from decimal import Decimal

    d = DecimalField(max_digits=5, decimal_places=2)
    assert d.clean("123.45") == Decimal("123.45")
    for bad in ("NaN", "Infinity", "-Infinity"):
        try:
            d.clean(bad)
        except ValueError:
            continue
        raise AssertionError(f"DecimalField accepted non-finite {bad!r}")

    # Too many total digits.
    try:
        d.clean("1234.56")
    except ValueError:
        pass
    else:
        raise AssertionError("DecimalField ignored max_digits")

    # Too many decimal places.
    try:
        d.clean("1.234")
    except ValueError:
        pass
    else:
        raise AssertionError("DecimalField ignored decimal_places")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    print("Round-8 observability / swallowed-exception regression tests\n")
    tests = [
        v
        for v in list(globals().values())
        if callable(v) and getattr(v, "_is_test", False)
    ]
    for t in tests:
        await t()

    print(
        f"\n{RESULTS['passed']} passed, {RESULTS['failed']} failed, "
        f"{RESULTS['skipped']} skipped"
    )
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n=== {name} ===\n{tb}")
    return 1 if RESULTS["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
