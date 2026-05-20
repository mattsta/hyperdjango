#!/usr/bin/env python3
"""
Validate the entire hyperdjango native stack end-to-end.

Tests every component in isolation and together:
1. Native extension import and exports
2. Zig validation functions
3. Zig JSON serialization
4. Handler wrapper interface
5. Native HTTP server serving real requests
6. Path params, query strings, POST bodies
7. Throughput measurement

Run: uv run python scripts/validate_native.py
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PASS = 0
FAIL = 0
TEST_PORT = 19880


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: {detail}")


def section(name):
    print(f"\n=== {name} ===")


# ── 1. Native Extension ──────────────────────────────────────────────────────

section("Native Extension Import")

try:
    from hyperdjango._hyperdjango_native import HyperServer, ResponseView, hello

    check("Import _hyperdjango_native", True)
    check("hello() returns string", "alive" in hello(), hello())
    check("HyperServer class exists", callable(HyperServer))
    check("ResponseView class exists", callable(ResponseView))
except ImportError as e:
    check("Import _hyperdjango_native", False, str(e))
    print("\n  Build the native extension first:")
    print("  uv run python zig/build_hyperdjango.py --install --release")
    sys.exit(1)

# Check all expected exports
import hyperdjango._hyperdjango_native as native

expected_exports = [
    "hello",
    "HyperServer",
    "ResponseView",
    "_rv_new",
    "_rv_json",
    "_rv_text",
    "_rv_set_header",
    "_server_new",
    "_server_add_route",
    "_server_run",
    "_db_configure",
    "_db_add_route",
    "validate_email",
    "validate_int_range",
    "validate_string_length",
    "json_dumps_native",
]
for name in expected_exports:
    check(f"Export: {name}", hasattr(native, name))

# ── 2. Zig Validation Functions ──────────────────────────────────────────────

section("Zig SIMD Validation")

check(
    "Email valid: alice@example.com", native.validate_email("alice@example.com") is True
)
check("Email invalid: not-email", native.validate_email("not-an-email") is False)
check("Email invalid: empty", native.validate_email("") is False)
check(
    "Email valid: user@sub.domain.com",
    native.validate_email("user@sub.domain.com") is True,
)

check("Int range [0,150] 25 = True", native.validate_int_range(25, 0, 150) is True)
check("Int range [0,150] -1 = False", native.validate_int_range(-1, 0, 150) is False)
check("Int range [0,150] 0 = True", native.validate_int_range(0, 0, 150) is True)
check("Int range [0,150] 150 = True", native.validate_int_range(150, 0, 150) is True)
check("Int range [0,150] 151 = False", native.validate_int_range(151, 0, 150) is False)

check(
    "Strlen [1,10] 'hello' = True",
    native.validate_string_length("hello", 1, 10) is True,
)
check("Strlen [5,10] 'hi' = False", native.validate_string_length("hi", 5, 10) is False)
check("Strlen [0,5] '' = True", native.validate_string_length("", 0, 5) is True)

# ── 3. Native JSON ───────────────────────────────────────────────────────────

section("Native JSON Serialization")

result = native.json_dumps_native({"hello": "world", "num": 42})
check("json_dumps returns bytes", isinstance(result, bytes))
parsed = json.loads(result)
check("json_dumps correct content", parsed == {"hello": "world", "num": 42})

result2 = native.json_dumps_native([1, 2, 3])
check("json_dumps list", json.loads(result2) == [1, 2, 3])

result3 = native.json_dumps_native({"nested": {"a": [1, 2]}})
check("json_dumps nested", json.loads(result3)["nested"]["a"] == [1, 2])

# ── 4. Handler Wrapper ───────────────────────────────────────────────────────

section("Handler Wrapper Interface")

from hyperdjango import HyperApp

app = HyperApp(title="Validation")


@app.get("/health")
def health_handler(request):
    return {"status": "ok"}


@app.get("/echo/{name}")
def echo_handler(request, name):
    return {"echo": name}


# Test wrapper with the kwargs interface the Zig server uses
wrapped_health = app._wrap_handler_for_zig(health_handler)
result = wrapped_health(
    method="GET",
    path="/health",
    body=b"",
    query_string="",
    headers={},
    path_params={},
)
# Wrapper returns the Zig enhanced-response tuple:
#   (status:int, content_type:str, body:bytes, extra_headers:str|None)
check("Wrapped handler returns tuple", isinstance(result, tuple), f"got {type(result)}")
check("Wrapped handler tuple shape", len(result) >= 4, f"got len {len(result)}")
status, content_type, body, _extra = result[0], result[1], result[2], result[3]
check("Wrapped handler status 200", status == 200, f"got {status}")
check("Wrapped handler correct JSON", json.loads(body)["status"] == "ok")

wrapped_echo = app._wrap_handler_for_zig(echo_handler)
result2 = wrapped_echo(
    method="GET",
    path="/echo/world",
    body=b"",
    query_string="",
    headers={},
    path_params={"name": "world"},
)
check("Wrapped path params work", isinstance(result2, tuple), f"got {type(result2)}")
check("Wrapped path params value", json.loads(result2[2])["echo"] == "world")


# Test async handler wrapper
@app.post("/data")
async def data_handler(request):
    body = await request.json()
    return {"received": body}


async_wrapped = app._wrap_async_for_zig(data_handler)
zig_wrapped = app._wrap_handler_for_zig(async_wrapped)
result3 = zig_wrapped(
    method="POST",
    path="/data",
    body=b'{"key":"value"}',
    query_string="",
    headers={"content-type": "application/json"},
    path_params={},
)
check("Async wrapped handler works", isinstance(result3, tuple), f"got {type(result3)}")
check(
    "Async handler got body",
    json.loads(result3[2])["received"]["key"] == "value",
)

# ── 5. Native HTTP Server ────────────────────────────────────────────────────

section("Native HTTP Server")

# Create a test app file
test_app_path = Path(__file__).parent / "_test_server_app.py"
with test_app_path.open("w") as f:
    f.write(f"""
import sys, os
from hyperdjango import HyperApp, Response

app = HyperApp(title="Validation Server")

@app.get("/health")
def health(request):
    return {{"status": "ok", "server": "zig"}}

@app.get("/echo/{{name}}")
def echo(request, name):
    return {{"echo": name}}

@app.post("/json")
async def json_ep(request):
    data = await request.json()
    return {{"received": data}}

@app.get("/text")
def text(request):
    return Response.text("Hello from Zig!")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port={TEST_PORT})
""")

# Start server
proc = subprocess.Popen(
    [sys.executable, test_app_path],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# Wait for it
server_ready = False
for _ in range(30):
    time.sleep(0.1)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/health", timeout=1):
            pass
        server_ready = True
        break
    except urllib.error.URLError, ConnectionRefusedError, OSError:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode()
            check("Server started", False, f"died: {stderr[:200]}")
            break

if server_ready:
    check("Server started", True)

    # Health
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/health") as resp:
            body = json.loads(resp.read())
            check("GET /health status", resp.status == 200)
        check("GET /health body", body.get("status") == "ok")
        check("GET /health server=zig", body.get("server") == "zig")
    except Exception as e:
        check("GET /health", False, str(e))

    # Path params
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{TEST_PORT}/echo/hyperdjango"
        ) as resp:
            body = json.loads(resp.read())
        check("GET /echo/{name} works", body.get("echo") == "hyperdjango")
    except Exception as e:
        check("GET /echo/{name}", False, str(e))

    # POST JSON
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{TEST_PORT}/json",
            data=json.dumps({"msg": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
        check("POST /json works", body.get("received", {}).get("msg") == "hello")
    except Exception as e:
        check("POST /json", False, str(e))

    # Text response
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/text") as resp:
            body = resp.read()
        check("GET /text works", b"Hello from Zig!" in body)
    except Exception as e:
        check("GET /text", False, str(e))

    # Throughput
    n = 200
    start = time.perf_counter()
    successes = 0
    for _ in range(n):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/health") as resp:
                if resp.status == 200:
                    successes += 1
        except Exception:
            pass
    elapsed = time.perf_counter() - start
    rps = n / elapsed
    check(f"Throughput: {rps:.0f} req/s ({successes}/{n} ok)", successes >= n * 0.95)

# Cleanup
proc.terminate()
try:
    proc.wait(timeout=3)
except subprocess.TimeoutExpired:
    proc.kill()
test_app_path.unlink()

# ── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'=' * 50}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("  All validations passed!")
else:
    print(f"  {FAIL} validations need attention")
    sys.exit(1)
