"""
Tests for HyperApp core features:
- Custom exception handlers (#385)
- Health check endpoints (#386)
- Nested transactions / savepoints (#387)
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import os
import sys

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "\u2713" if condition else "\u2717"
    print(f"  {symbol} {label}")


# ═══════════════════════════════════════════════════════════════════════════
# Exception Handlers
# ═══════════════════════════════════════════════════════════════════════════


@test("exc: exception_handler decorator registers handler")
async def test_exc_register():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")

    @app.exception_handler(ValueError)
    async def handle_value(request, exc):
        from hyperdjango.response import Response

        return Response.json({"error": str(exc)}, status=400)

    check("handler registered", ValueError in app._exception_handlers)
    check("handler is callable", callable(app._exception_handlers[ValueError]))


@test("exc: add_exception_handler programmatic API")
async def test_exc_add():
    from hyperdjango import HyperApp
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    def handle_type(request, exc):
        return Response.json({"error": "type error"}, status=422)

    app.add_exception_handler(TypeError, handle_type)
    check("handler registered", TypeError in app._exception_handlers)


@test("exc: custom handler invoked for matching exception")
async def test_exc_invoked():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    @app.exception_handler(ValueError)
    async def handle_value(request, exc):
        return Response.json({"caught": str(exc)}, status=422)

    @app.get("/fail")
    async def fail(request):
        raise ValueError("bad input")

    req = Request(method="GET", path="/fail")
    resp = await app.handle(req)
    check("status 422", resp.status == 422)
    import json

    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("body has caught key", body.get("caught") == "bad input")


@test("exc: sync handler supported")
async def test_exc_sync():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    def handle_perm(request, exc):
        return Response.json({"error": "forbidden"}, status=403)

    app.add_exception_handler(PermissionError, handle_perm)

    @app.get("/perm")
    async def perm_fail(request):
        raise PermissionError("no access")

    req = Request(method="GET", path="/perm")
    resp = await app.handle(req)
    check("sync handler works, status 403", resp.status == 403)


@test("exc: MRO resolution (subclass matches parent handler)")
async def test_exc_mro():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    class CustomError(ValueError):
        pass

    @app.exception_handler(ValueError)
    async def handle_value(request, exc):
        return Response.json({"caught": "ValueError handler"}, status=400)

    @app.get("/custom")
    async def custom_fail(request):
        raise CustomError("specific error")

    req = Request(method="GET", path="/custom")
    resp = await app.handle(req)
    check("subclass caught by parent handler", resp.status == 400)


@test("exc: specific handler takes precedence over parent")
async def test_exc_specific():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    class CustomError(ValueError):
        pass

    @app.exception_handler(ValueError)
    async def handle_value(request, exc):
        return Response.json({"handler": "parent"}, status=400)

    @app.exception_handler(CustomError)
    async def handle_custom(request, exc):
        return Response.json({"handler": "specific"}, status=422)

    @app.get("/custom")
    async def custom_fail(request):
        raise CustomError("specific")

    req = Request(method="GET", path="/custom")
    resp = await app.handle(req)
    check("specific handler used", resp.status == 422)
    import json

    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("handler is specific", body.get("handler") == "specific")


@test("exc: HTTPException still works without custom handler")
async def test_exc_http_default():
    from hyperdjango import HyperApp
    from hyperdjango.app import HTTPException
    from hyperdjango.request import Request

    app = HyperApp(title="Test")

    @app.get("/notfound")
    async def not_found(request):
        raise HTTPException(404, "Not Found")

    req = Request(method="GET", path="/notfound")
    resp = await app.handle(req)
    check("HTTPException gives 404", resp.status == 404)


@test("exc: custom handler for HTTPException overrides default")
async def test_exc_http_custom():
    from hyperdjango import HyperApp
    from hyperdjango.app import HTTPException
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    @app.exception_handler(HTTPException)
    async def custom_http(request, exc):
        return Response.json(
            {"custom": True, "status": exc.status_code}, status=exc.status_code
        )

    @app.get("/err")
    async def err(request):
        raise HTTPException(403, "Forbidden")

    req = Request(method="GET", path="/err")
    resp = await app.handle(req)
    check("custom HTTP handler used", resp.status == 403)
    import json

    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("body has custom flag", body.get("custom") is True)


@test("exc: unhandled exception falls to default 500")
async def test_exc_unhandled():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test", debug=False)

    @app.get("/crash")
    async def crash(request):
        raise RuntimeError("unexpected")

    req = Request(method="GET", path="/crash")
    resp = await app.handle(req)
    check("unhandled gives 500", resp.status == 500)


@test("exc: _find_exception_handler returns None for no match")
async def test_exc_no_match():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")
    result = app._find_exception_handler(RuntimeError("test"))
    check("returns None", result is None)


# ═══════════════════════════════════════════════════════════════════════════
# Health Checks
# ═══════════════════════════════════════════════════════════════════════════


@test("health: mount_health registers routes")
async def test_health_mount():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")
    app.mount_health()
    # Verify routes exist by trying to handle requests
    from hyperdjango.request import Request

    req = Request(method="GET", path="/health")
    resp = await app.handle(req)
    check("liveness returns 200", resp.status == 200)


@test("health: liveness always returns ok")
async def test_health_liveness():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")
    app.mount_health()

    req = Request(method="GET", path="/health")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("status ok", body.get("status") == "ok")


@test("health: readiness without DB returns ok (no checks)")
async def test_health_ready_no_db():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")
    app.mount_health()

    req = Request(method="GET", path="/ready")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("status ok without DB", body.get("status") == "ok")
    check("200 status", resp.status == 200)


@test("health: custom check passes")
async def test_health_custom_pass():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")

    def check_something():
        return True

    app.add_health_check("something", check_something)
    app.mount_health()

    req = Request(method="GET", path="/ready")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("status ok", body.get("status") == "ok")
    check("custom check listed", body.get("checks", {}).get("something") == "ok")


@test("health: custom check fails returns 503")
async def test_health_custom_fail():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")

    def check_failing():
        return False

    app.add_health_check("cache", check_failing)
    app.mount_health()

    req = Request(method="GET", path="/ready")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("status unhealthy", body.get("status") == "unhealthy")
    check("503 status code", resp.status == 503)
    check("cache check unhealthy", body.get("checks", {}).get("cache") == "unhealthy")


@test("health: custom check exception returns 503")
async def test_health_custom_exception():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")

    def check_crash():
        raise ConnectionError("cannot connect")

    app.add_health_check("external", check_crash)
    app.mount_health()

    req = Request(method="GET", path="/ready")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("503 on exception", resp.status == 503)
    # The readiness body reports a GENERIC "error" status and must NOT leak the
    # raw exception text (it's logged server-side instead) — prod-info-leak fix.
    check("error status generic", body.get("checks", {}).get("external") == "error")
    check("no raw exception leaked", "cannot connect" not in json.dumps(body))


@test("health: async custom check supported")
async def test_health_async_check():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")

    async def check_async():
        return True

    app.add_health_check("async_service", check_async)
    app.mount_health()

    req = Request(method="GET", path="/ready")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("async check ok", body.get("checks", {}).get("async_service") == "ok")


@test("health: custom paths")
async def test_health_custom_paths():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")
    app.mount_health("/healthz", "/readyz")

    req1 = Request(method="GET", path="/healthz")
    resp1 = await app.handle(req1)
    check("custom liveness path works", resp1.status == 200)

    req2 = Request(method="GET", path="/readyz")
    resp2 = await app.handle(req2)
    check("custom readiness path works", resp2.status == 200)


@test("health: readiness with DB connected")
async def test_health_db():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.database import Database
    from hyperdjango.request import Request

    db = Database(DB_URL)
    await db.connect()
    try:
        app = HyperApp(title="Test")
        app._db = db
        app.mount_health()

        req = Request(method="GET", path="/ready")
        resp = await app.handle(req)
        body = (
            json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
        )
        check("DB check ok", body.get("checks", {}).get("database") == "ok")
        check("200 status", resp.status == 200)
    finally:
        await db.disconnect()


@test("health: multiple checks mixed pass/fail")
async def test_health_mixed():
    import json

    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test")

    app.add_health_check("good", lambda: True)
    app.add_health_check("bad", lambda: False)
    app.mount_health()

    req = Request(method="GET", path="/ready")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("503 when any fail", resp.status == 503)
    check("good is ok", body.get("checks", {}).get("good") == "ok")
    check("bad is unhealthy", body.get("checks", {}).get("bad") == "unhealthy")


# ═══════════════════════════════════════════════════════════════════════════
# Nested Transactions / Savepoints
# ═══════════════════════════════════════════════════════════════════════════


@test("tx: basic transaction commit")
async def test_tx_basic():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        async with db.transaction():
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "committed")

        rows = await db.query("SELECT val FROM test_tx")
        check("committed row exists", len(rows) == 1)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: basic transaction rollback")
async def test_tx_rollback():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        with contextlib.suppress(ValueError):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO test_tx (val) VALUES ($1)", "should_rollback"
                )
                raise ValueError("abort")

        rows = await db.query("SELECT val FROM test_tx")
        check("rolled back row absent", len(rows) == 0)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: nested savepoint commit")
async def test_tx_nested_commit():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        async with db.transaction():
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "outer")
            async with db.transaction():
                await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "inner")

        rows = await db.query("SELECT val FROM test_tx ORDER BY id")
        vals = [r[0] if not isinstance(r, dict) else r["val"] for r in rows]
        check("both rows committed", vals == ["outer", "inner"])
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: nested savepoint rollback (inner only)")
async def test_tx_nested_rollback_inner():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        async with db.transaction():
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "outer")
            with contextlib.suppress(ValueError):
                async with db.transaction():
                    await db.execute(
                        "INSERT INTO test_tx (val) VALUES ($1)", "inner_bad"
                    )
                    raise ValueError("inner abort")
            # Outer transaction continues
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "after_inner")

        rows = await db.query("SELECT val FROM test_tx ORDER BY id")
        vals = [r[0] if not isinstance(r, dict) else r["val"] for r in rows]
        check("outer committed", "outer" in vals)
        check("inner rolled back", "inner_bad" not in vals)
        check("after_inner committed", "after_inner" in vals)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: outer rollback rolls back everything")
async def test_tx_outer_rollback():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        with contextlib.suppress(ValueError):
            async with db.transaction():
                await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "outer")
                async with db.transaction():
                    await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "inner")
                raise ValueError("outer abort")

        rows = await db.query("SELECT val FROM test_tx")
        check("everything rolled back", len(rows) == 0)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: named savepoint")
async def test_tx_named():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        async with db.transaction():
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "before")
            with contextlib.suppress(ValueError):
                async with db.transaction(savepoint_name="my_save"):
                    await db.execute(
                        "INSERT INTO test_tx (val) VALUES ($1)", "in_savepoint"
                    )
                    raise ValueError("rollback savepoint")

        rows = await db.query("SELECT val FROM test_tx ORDER BY id")
        vals = [r[0] if not isinstance(r, dict) else r["val"] for r in rows]
        check("before committed", "before" in vals)
        check("savepoint rolled back", "in_savepoint" not in vals)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: atomic() alias works")
async def test_tx_atomic():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        async with db.atomic():
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "atomic_val")

        rows = await db.query("SELECT val FROM test_tx")
        check("atomic committed", len(rows) == 1)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: triple nesting")
async def test_tx_triple():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS test_tx (id SERIAL PRIMARY KEY, val TEXT)"
        )
        await db.execute("DELETE FROM test_tx")

        async with db.transaction():  # BEGIN
            await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "level1")
            async with db.transaction():  # SAVEPOINT sp_2
                await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "level2")
                async with db.transaction():  # SAVEPOINT sp_3
                    await db.execute("INSERT INTO test_tx (val) VALUES ($1)", "level3")
                # RELEASE sp_3
            # RELEASE sp_2
        # COMMIT

        rows = await db.query("SELECT val FROM test_tx ORDER BY id")
        vals = [r[0] if not isinstance(r, dict) else r["val"] for r in rows]
        check("all 3 levels committed", vals == ["level1", "level2", "level3"])
    finally:
        await db.execute("DROP TABLE IF EXISTS test_tx")
        await db.disconnect()


@test("tx: depth tracking resets after exception")
async def test_tx_depth_reset():
    from hyperdjango.database import Database

    db = Database(DB_URL)
    await db.connect()
    try:
        with contextlib.suppress(ValueError):
            async with db.transaction():
                raise ValueError("abort")

        # Depth should be back to 0 — next transaction should use BEGIN, not SAVEPOINT
        check("depth reset", getattr(db._tx_depth, "depth", 0) == 0)
    finally:
        await db.disconnect()


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("HyperApp Core Features Tests")
    print("(Exception Handlers + Health Checks + Nested Transactions)")
    print(f"{'=' * 60}\n")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            await func()
        except Exception as e:
            check(f"EXCEPTION: {e}", False)
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed:")
        for label, ok in results:
            if not ok:
                print(f"  \u2717 {label}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
