"""Regression tests for the ws6-native-leftovers review fixes.

Covers:
  * Bad status codes returned by a handler must not crash the server and must
    yield a valid HTTP response, with the server still alive afterwards
    (parseStatusCode / enhanced-path status hardening — review item 5).
  * Many idle held-open connections must not stall the server — the reactor
    parks them without pinning workers, so concurrent normal requests keep
    succeeding (connection responsiveness — related to review item 2).

Each test starts the native Zig server in a subprocess and drives it over real
sockets, then shuts it down.

Note on review item 2 (SO_SNDTIMEO for zero-window writers): the socket option
is now set on every accepted fd and the shed-503 path uses a short send timeout,
but deterministically forcing a kernel zero-window in CI is impractical, so that
specific bound is verified by inspection rather than a flaky timing test. The
responsiveness test below exercises the adjacent guarantee that held-open
connections do not starve new requests.
"""

import contextlib
import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOST = "127.0.0.1"
PORT = 19881
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

APP_CODE = f'''
import sys
sys.path.insert(0, ".")
from hyperdjango import HyperApp, Response

app = HyperApp(title="Review Regression Server")

@app.get("/health")
def health(request):
    return Response.json({{"ok": True}})

@app.get("/badstatus/high")
def bad_high(request):
    # Out-of-range status — must not crash the native writer.
    return Response.json({{"x": 1}}, status=99999)

@app.get("/badstatus/nonint")
def bad_nonint(request):
    # Force a non-int status object through the Zig tuple contract.
    r = Response.json({{"x": 1}})
    r.status = "not-an-int"
    return r

@app.get("/badstatus/zero")
def bad_zero(request):
    return Response.json({{"x": 1}}, status=0)

if __name__ == "__main__":
    app.run(host="{HOST}", port={PORT})
'''


@pytest.fixture(scope="module")
def server():
    env = os.environ.copy()
    # Reactor mode (the default): idle connections park on the reactor instead of
    # pinning a worker each, which is what makes the responsiveness test robust.
    env.update({"HYPER_HTTP_SERVER_MODEL": "reactor", "HYPER_THREAD_POOL_SIZE": "4"})
    proc = subprocess.Popen(
        [sys.executable, "-c", APP_CODE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )
    started = False
    for _ in range(50):
        time.sleep(0.1)
        try:
            conn = http.client.HTTPConnection(HOST, PORT, timeout=1)
            conn.request("GET", "/health")
            conn.getresponse().read()
            conn.close()
            started = True
            break
        except ConnectionRefusedError, OSError:
            if proc.poll() is not None:
                err = proc.stderr.read().decode() if proc.stderr else ""
                pytest.skip(f"server failed to start: {err[:500]}")
    if not started:
        proc.kill()
        pytest.skip("server didn't start in time")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(path, timeout=5):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    conn.close()
    return status, body


@pytest.mark.parametrize(
    "path", ["/badstatus/high", "/badstatus/nonint", "/badstatus/zero"]
)
def test_bad_status_does_not_crash(server, path):
    # The response must be a parseable HTTP response with a valid status line —
    # no crash, no connection reset, no UB from an out-of-range/non-int status.
    status, _ = _get(path)
    assert 100 <= status <= 599
    # And the server is still alive and serving afterwards.
    hstatus, _ = _get("/health")
    assert hstatus == 200


def test_idle_connections_do_not_block_new_requests(server):
    # Open many connections and hold them idle (connected, no bytes sent). In
    # reactor mode these park on the reactor without consuming a worker, so
    # concurrent normal requests must keep being served promptly.
    idle = []
    try:
        for _ in range(32):
            s = socket.create_connection((HOST, PORT), timeout=2)
            idle.append(s)

        deadline = time.time() + 5
        ok = 0
        for _ in range(20):
            assert time.time() < deadline, "normal requests stalled by idle connections"
            status, _ = _get("/health", timeout=2)
            assert status == 200
            ok += 1
        assert ok == 20
    finally:
        for s in idle:
            with contextlib.suppress(OSError):
                s.close()
