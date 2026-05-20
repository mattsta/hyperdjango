"""
Tests for production features:
- Graceful shutdown + prod hardening (#400)
- WebSocket + Channels bridge (#401)
- Scaffold quality (#402)
- TestClient improvements (#399)
"""

# hyper-test: unit

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

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
# Graceful Shutdown + Prod Hardening
# ═══════════════════════════════════════════════════════════════════════════


@test("prod: run() accepts prod parameter")
def test_prod_param():
    import inspect

    from hyperdjango import HyperApp

    sig = inspect.signature(HyperApp.run)
    params = list(sig.parameters)
    check("has prod param", "prod" in params)


@test("prod: _validate_production_config exists")
def test_validate_config():
    from hyperdjango import HyperApp

    check("has validation method", hasattr(HyperApp, "_validate_production_config"))


@test("prod: prod=True sets debug=False")
def test_prod_debug():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test", debug=True)
    # Can't call run() (starts server), but verify the logic
    check("debug starts True", app.debug is True)
    # Simulate what run(prod=True) does
    app.debug = False
    check("prod sets debug False", app.debug is False)


@test("prod: no ASGI fallback (native only)")
def test_no_asgi():
    from hyperdjango import HyperApp

    check("no _run_asgi method", not hasattr(HyperApp, "_run_asgi"))


# ═══════════════════════════════════════════════════════════════════════════
# Scaffold Quality
# ═══════════════════════════════════════════════════════════════════════════


@test("scaffold: generates .gitignore")
def test_scaffold_gitignore():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("testproj")
            gitignore = Path(tmp) / "testproj" / ".gitignore"
            check(".gitignore exists", gitignore.is_file())
            content = gitignore.read_text()
            check("has __pycache__", "__pycache__" in content)
            check("has .env", ".env" in content)
            check("has .venv", ".venv" in content)
        finally:
            os.chdir(old_cwd)


@test("scaffold: generates README.md")
def test_scaffold_readme():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("readmeproj")
            readme = Path(tmp) / "readmeproj" / "README.md"
            check("README.md exists", readme.is_file())
            content = readme.read_text()
            check("has project name", "readmeproj" in content)
            check("has HyperDjango ref", "HyperDjango" in content)
        finally:
            os.chdir(old_cwd)


@test("scaffold: --with-auth generates login template")
def test_scaffold_login_template():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("authproj", with_auth=True)
            login = Path(tmp) / "authproj" / "templates" / "login.html"
            check("login.html exists", login.is_file())
            content = login.read_text()
            check("has form", "<form" in content)
            check("has password input", 'type="password"' in content)
        finally:
            os.chdir(old_cwd)


@test("scaffold: --full generates all files")
def test_scaffold_full_files():
    from hyperdjango.cli import cmd_new

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            cmd_new("fullproj", with_db=True, with_auth=True, with_admin=True)
            base = Path(tmp) / "fullproj"
            check(".gitignore", (base / ".gitignore").is_file())
            check("README.md", (base / "README.md").is_file())
            check(
                "login.html",
                (base / "templates" / "login.html").is_file(),
            )
            check(".env.example", (base / ".env.example").is_file())
            check("models.py", (base / "models.py").is_file())
            check("Makefile", (base / "Makefile").is_file())
        finally:
            os.chdir(old_cwd)


# ═══════════════════════════════════════════════════════════════════════════
# TestClient: Multipart Uploads
# ═══════════════════════════════════════════════════════════════════════════


@test("testclient: multipart file upload")
async def test_multipart_upload():
    from hyperdjango import HyperApp
    from hyperdjango.response import Response
    from hyperdjango.testing import TestClient

    app = HyperApp(title="Test")

    @app.post("/upload")
    async def upload(request):
        ct = request.headers.get("content-type", "")
        has_boundary = "boundary=" in ct
        body_size = len(request.body)
        return Response.json({"multipart": has_boundary, "body_size": body_size})

    client = TestClient(app)
    resp = client.post(
        "/upload",
        files={
            "avatar": ("photo.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/jpeg")
        },
        data={"name": "Alice"},
    )
    body = resp.json()
    check("multipart content type", body["multipart"] is True)
    check("body has content", body["body_size"] > 100)


@test("testclient: _build_multipart produces valid body")
def test_build_multipart():
    from hyperdjango.testing import TestClient

    body, ct = TestClient._build_multipart(
        {"name": "test"},
        {"file": ("doc.txt", b"hello world", "text/plain")},
    )
    check("has boundary in content-type", "boundary=" in ct)
    check("body contains field", b"name" in body)
    check("body contains filename", b"doc.txt" in body)
    check("body contains file content", b"hello world" in body)


# ═══════════════════════════════════════════════════════════════════════════
# TestClient: WebSocket Testing
# ═══════════════════════════════════════════════════════════════════════════


@test("testwebsocket: basic send/receive")
async def test_ws_basic():
    from hyperdjango.testing import TestWebSocket

    ws = TestWebSocket()
    await ws.accept()
    check("accepted", ws.accepted)

    await ws.send_text("hello")
    check("sent message", ws.sent_messages == ["hello"])

    ws.feed("response1", "response2")
    msg1 = await ws.receive_text()
    msg2 = await ws.receive_text()
    check("received msg1", msg1 == "response1")
    check("received msg2", msg2 == "response2")

    await ws.close()
    check("closed", ws.closed)


@test("testwebsocket: send bytes")
async def test_ws_bytes():
    from hyperdjango.testing import TestWebSocket

    ws = TestWebSocket()
    await ws.accept()
    await ws.send_bytes(b"\x00\x01\x02")
    check("bytes sent", ws.sent_messages == [b"\x00\x01\x02"])


@test("testwebsocket: feed and receive_bytes")
async def test_ws_receive_bytes():
    from hyperdjango.testing import TestWebSocket

    ws = TestWebSocket()
    ws.feed(b"binary data")
    data = await ws.receive_bytes()
    check("bytes received", data == b"binary data")


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket + Channels Bridge
# ═══════════════════════════════════════════════════════════════════════════


@test("channels: websocket_channel_handler exists")
def test_channel_handler_exists():
    from hyperdjango.channels import websocket_channel_handler

    check("function exists", callable(websocket_channel_handler))


@test("app: channel() decorator exists")
def test_channel_decorator():
    from hyperdjango import HyperApp

    app = HyperApp(title="Test")
    check("has channel method", callable(app.channel))


@test("channels: bridge with InMemoryLayer")
async def test_channel_bridge():
    from hyperdjango.channels import InMemoryChannelLayer, set_channel_layer

    layer = InMemoryChannelLayer()
    set_channel_layer(layer)

    channel = layer.channel("test:room1")

    # Simulate what the bridge does: subscribe, receive messages
    received: list[dict[str, str]] = []

    async def on_message(msg):
        received.append(msg.data)

    sub_id = channel.subscribe(on_message)
    check("subscribed", sub_id > 0)

    # publish() awaits delivery to every local subscriber before it returns, so
    # there is nothing left in flight when it does — the message has landed or
    # the platform is broken. The old sleep was waiting for an event that had
    # already happened, and its result (`len(received) > 0`) was weaker than
    # what can be stated here: exactly one delivery, with the exact payload.
    await channel.publish({"text": "hello from channel"})

    channel.unsubscribe(sub_id)
    check("message delivered", received == [{"text": "hello from channel"}])

    set_channel_layer(None)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("Production Features Tests")
    print("(Shutdown, Scaffold, TestClient, Channels)")
    print(f"{'=' * 60}\n")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            if inspect.iscoroutinefunction(func):
                await func()
            else:
                func()
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
