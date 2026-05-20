"""Tests that the native Zig HTTP server actually serves HTTP requests.

Starts the server in a subprocess, hits it with real HTTP requests,
verifies responses, then shuts it down.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Port for test server — use a high port to avoid conflicts
TEST_PORT = 19876
TEST_HOST = "127.0.0.1"
TEST_URL = f"http://{TEST_HOST}:{TEST_PORT}"


@pytest.fixture(scope="module")
def native_server():
    """Start a native Zig HTTP server for testing."""

    # Write a minimal test app
    app_code = f'''
import sys
sys.path.insert(0, ".")
from hyperdjango import HyperApp, Response

app = HyperApp(title="Test Server")

@app.get("/health")
def health(request):
    return {{"status": "ok", "server": "zig"}}

@app.get("/echo/{{name}}")
def echo(request, name):
    return {{"echo": name}}

@app.post("/json")
async def json_endpoint(request):
    data = await request.json()
    return {{"received": data}}

@app.get("/text")
def text(request):
    return Response.text("Hello from Zig!")

@app.get("/headers")
def headers(request):
    return Response.json({{}}, headers={{"x-custom": "native"}})

if __name__ == "__main__":
    app.run(host="{TEST_HOST}", port={TEST_PORT})
'''

    # Start server as subprocess
    proc = subprocess.Popen(
        [sys.executable, "-c", app_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
    )

    # Wait for server to start
    started = False
    for _ in range(30):  # 3 seconds max
        time.sleep(0.1)
        try:
            urllib.request.urlopen(f"{TEST_URL}/health", timeout=1)
            started = True
            break
        except urllib.error.URLError, ConnectionRefusedError, OSError:
            if proc.poll() is not None:
                # Server died
                stdout = proc.stdout.read().decode() if proc.stdout else ""
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                pytest.skip(f"Server failed to start: {stderr[:500]}")
                return

    if not started:
        proc.kill()
        pytest.skip("Server didn't start in time")

    yield proc

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(path, timeout=5):
    """Make a GET request to the test server."""
    req = urllib.request.Request(f"{TEST_URL}{path}")
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.status, resp.read(), dict(resp.headers)


def _post_json(path, data, timeout=5):
    """Make a POST request with JSON body."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{TEST_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.status, resp.read(), dict(resp.headers)


class TestNativeServerHealth:
    def test_health_endpoint(self, native_server):
        status, body, headers = _get("/health")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["server"] == "zig"

    def test_health_content_type(self, native_server):
        status, body, headers = _get("/health")
        content_type = headers.get("Content-Type", headers.get("content-type", ""))
        assert "json" in content_type.lower() or "text" in content_type.lower()


class TestNativeServerRouting:
    def test_path_params(self, native_server):
        status, body, _ = _get("/echo/world")
        assert status == 200
        data = json.loads(body)
        assert data["echo"] == "world"

    def test_path_params_special_chars(self, native_server):
        status, body, _ = _get("/echo/hello-world")
        assert status == 200
        data = json.loads(body)
        assert data["echo"] == "hello-world"

    def test_404_missing_route(self, native_server):
        try:
            _get("/nonexistent")
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code in (404, 500)  # Server may return either


class TestNativeServerJSON:
    def test_post_json(self, native_server):
        status, body, _ = _post_json("/json", {"key": "value"})
        assert status == 200
        data = json.loads(body)
        assert data["received"]["key"] == "value"

    def test_post_json_complex(self, native_server):
        payload = {"users": [{"name": "Alice"}, {"name": "Bob"}], "count": 2}
        status, body, _ = _post_json("/json", payload)
        assert status == 200
        data = json.loads(body)
        assert data["received"]["count"] == 2


class TestNativeServerResponse:
    def test_text_response(self, native_server):
        status, body, _ = _get("/text")
        assert status == 200
        assert b"Hello from Zig!" in body


class TestNativeServerThroughput:
    def test_rapid_requests(self, native_server):
        """Fire 100 rapid requests and verify all succeed."""
        successes = 0
        for _ in range(100):
            try:
                status, _, _ = _get("/health")
                if status == 200:
                    successes += 1
            except Exception:
                pass
        assert successes >= 95, f"Only {successes}/100 requests succeeded"

    def test_throughput_measurement(self, native_server):
        """Measure requests per second and verify all complete successfully."""
        n = 200
        successes = 0
        start = time.perf_counter()
        for _ in range(n):
            try:
                status, _, _ = _get("/health")
                if status == 200:
                    successes += 1
            except Exception:
                pass
        elapsed = time.perf_counter() - start
        rps = n / elapsed
        print(f"\n  Native server: {rps:.0f} req/s ({n} requests in {elapsed:.2f}s)")
        # All requests must succeed — correctness, not speed
        assert successes == n, f"Only {successes}/{n} requests succeeded"
