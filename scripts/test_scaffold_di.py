"""
Tests for scaffold enhancement (#393) and dependency injection (#391).

Scaffold: hyper new --with-db/--with-auth/--with-admin/--full presets
DI: app.provide(Type, instance) + auto-inject into handlers by annotation
"""

# hyper-test: unit

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

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
# Scaffold Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("scaffold: basic (no flags)")
async def test_scaffold_basic():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "myapp"
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("myapp")
            check("app.py exists", (project_dir / "app.py").is_file())
            check("views/ exists", (project_dir / "views").is_dir())
            check(
                "templates/ exists",
                (project_dir / "templates").is_dir(),
            )
            check("static/ exists", (project_dir / "static").is_dir())
            check(
                "pyproject.toml exists",
                (project_dir / "pyproject.toml").is_file(),
            )
            check(
                ".python-version exists",
                (project_dir / ".python-version").is_file(),
            )
            check("Makefile exists", (project_dir / "Makefile").is_file())

            # No uvicorn in dependencies
            pyproject = (project_dir / "pyproject.toml").read_text()
            check("no uvicorn in deps", "uvicorn" not in pyproject)
            check("hyperdjango in deps", "hyperdjango" in pyproject)

            # No models.py without --with-db
            check(
                "no models.py without --with-db",
                not (project_dir / "models.py").is_file(),
            )

            # Health check always included
            app_py = (project_dir / "app.py").read_text()
            check("mount_health in app.py", "mount_health" in app_py)
        finally:
            os.chdir(old_cwd)


@test("scaffold: --with-db")
async def test_scaffold_db():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("dbapp", with_db=True)
            project_dir = Path(tmp) / "dbapp"

            check(
                "models.py exists",
                (project_dir / "models.py").is_file(),
            )
            check(
                ".env.example exists",
                (project_dir / ".env.example").is_file(),
            )

            app_py = (project_dir / "app.py").read_text()
            check("Database import in app.py", "Database" in app_py)
            # The scaffold no longer hardcodes a DSN in app.py: the framework
            # resolves DATABASE_URL / HYPER_DATABASE_URL / the PG* set, so the
            # generated app carries no explicit database= argument.
            check("app.py defers DB URL to the resolver", "database=" not in app_py)

            env = (project_dir / ".env.example").read_text()
            check("DATABASE_URL in .env", "DATABASE_URL" in env)

            makefile = (project_dir / "Makefile").read_text()
            check("migrate in Makefile", "migrate" in makefile)
        finally:
            os.chdir(old_cwd)


@test("scaffold: --with-auth (implies --with-db)")
async def test_scaffold_auth():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("authapp", with_auth=True)
            project_dir = Path(tmp) / "authapp"

            app_py = (project_dir / "app.py").read_text()
            check("SessionAuth in app.py", "SessionAuth" in app_py)
            check(
                "SessionAuth in app.py",
                "SessionAuth" in app_py or "session_auth" in app_py,
            )
            check("Database also included (implied)", "Database" in app_py)
            check(
                "models.py exists (implied)",
                (project_dir / "models.py").is_file(),
            )

            makefile = (project_dir / "Makefile").read_text()
            check("createsuperuser in Makefile", "createsuperuser" in makefile)
        finally:
            os.chdir(old_cwd)


@test("scaffold: --with-admin (implies --with-auth + --with-db)")
async def test_scaffold_admin():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("adminapp", with_admin=True)
            project_dir = Path(tmp) / "adminapp"

            app_py = (project_dir / "app.py").read_text()
            check("HyperAdmin in app.py", "HyperAdmin" in app_py)
            check("register_auth_models in app.py", "register_auth_models" in app_py)
            check("SessionAuth also included", "SessionAuth" in app_py)
            check("Database also included", "Database" in app_py)
        finally:
            os.chdir(old_cwd)


@test("scaffold: --full includes everything")
async def test_scaffold_full():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("fullapp", with_db=True, with_auth=True, with_admin=True)
            project_dir = Path(tmp) / "fullapp"

            app_py = (project_dir / "app.py").read_text()
            check("has Database", "Database" in app_py)
            check("has SessionAuth", "SessionAuth" in app_py)
            check("has HyperAdmin", "HyperAdmin" in app_py)
            check("has mount_health", "mount_health" in app_py)
            check("has models.py", (project_dir / "models.py").is_file())
            check(
                "has .env.example",
                (project_dir / ".env.example").is_file(),
            )
            check("has Makefile", (project_dir / "Makefile").is_file())
        finally:
            os.chdir(old_cwd)


@test("scaffold: python version is 3.14t")
async def test_scaffold_python_version():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("verapp")
            pv = (Path(tmp) / "verapp" / ".python-version").read_text().strip()
            check("python version is 3.14t", pv == "3.14t")
        finally:
            os.chdir(old_cwd)


@test("scaffold: requires-python >= 3.14")
async def test_scaffold_requires_python():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("reqapp")
            pyproject = (Path(tmp) / "reqapp" / "pyproject.toml").read_text()
            check("requires-python >= 3.14", ">=3.14" in pyproject)
        finally:
            os.chdir(old_cwd)


# ═══════════════════════════════════════════════════════════════════════════
# Dependency Injection Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("di: provide registers service")
async def test_di_provide():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")

    class MyService:
        pass

    svc = MyService()
    app.provide(MyService, svc)
    check("service registered", app._services[MyService] is svc)


@test("di: get_service retrieves service")
async def test_di_get():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")

    class MyService:
        value = 42

    svc = MyService()
    app.provide(MyService, svc)
    check("get_service returns instance", app.get_service(MyService).value == 42)


@test("di: get_service returns None for unregistered")
async def test_di_get_none():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")
    check("returns None", app.get_service(str) is None)


@test("di: auto-inject into handler by annotation")
async def test_di_inject():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    class Counter:
        def __init__(self):
            self.value = 0

        def inc(self):
            self.value += 1
            return self.value

    app = HyperApp(title="Test")
    counter = Counter()
    app.provide(Counter, counter)

    @app.get("/count")
    async def count_handler(request, counter: Counter):
        val = counter.inc()
        return Response.json({"count": val})

    req = Request(method="GET", path="/count")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("injected counter works", body.get("count") == 1)

    # Call again — same instance
    resp2 = await app.handle(req)
    body2 = (
        json.loads(resp2.body) if isinstance(resp2.body, (str, bytes)) else resp2.body
    )
    check("same instance reused", body2.get("count") == 2)


@test("di: multiple services injected")
async def test_di_multiple():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    class ServiceA:
        name = "A"

    class ServiceB:
        name = "B"

    app = HyperApp(title="Test")
    app.provide(ServiceA, ServiceA())
    app.provide(ServiceB, ServiceB())

    @app.get("/multi")
    async def multi_handler(request, a: ServiceA, b: ServiceB):
        return Response.json({"a": a.name, "b": b.name})

    req = Request(method="GET", path="/multi")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("service A injected", body.get("a") == "A")
    check("service B injected", body.get("b") == "B")


@test("di: path params take precedence over services")
async def test_di_path_precedence():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")
    app.provide(int, 999)  # register int service

    @app.get("/items/{id}")
    async def item_handler(request, id: int):
        return Response.json({"id": id})

    req = Request(method="GET", path="/items/42")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check(
        "path param wins over service", body.get("id") == "42" or body.get("id") == 42
    )


@test("di: handler without annotations works normally")
async def test_di_no_annotations():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    app = HyperApp(title="Test")

    @app.get("/plain")
    async def plain_handler(request):
        return Response.json({"ok": True})

    req = Request(method="GET", path="/plain")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("plain handler works", body.get("ok") is True)


@test("di: unregistered annotation type ignored")
async def test_di_unregistered_ignored():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    class Unknown:
        pass

    app = HyperApp(title="Test")

    @app.get("/unknown")
    async def handler(request, svc: Unknown = None):
        return Response.json({"svc": svc is None})

    req = Request(method="GET", path="/unknown")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("unregistered type uses default", body.get("svc") is True)


@test("di: sync handler injection works")
async def test_di_sync():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response

    class Config:
        name = "production"

    app = HyperApp(title="Test")
    app.provide(Config, Config())

    @app.get("/config")
    def config_handler(request, cfg: Config):
        return Response.json({"env": cfg.name})

    req = Request(method="GET", path="/config")
    resp = await app.handle(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    check("sync handler injection works", body.get("env") == "production")


@test("di: provide overwrites previous registration")
async def test_di_overwrite():
    from hyperdjango import HyperApp

    class Svc:
        def __init__(self, v):
            self.v = v

    app = HyperApp(title="Test")
    app.provide(Svc, Svc(1))
    app.provide(Svc, Svc(2))
    check("overwritten to 2", app.get_service(Svc).v == 2)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("Scaffold Enhancement + Dependency Injection Tests")
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
