"""HTTP keep-alive carry-over / pipelining correctness for the native server.

Regression coverage for the framing bug where handleOneRequest discarded bytes
read past the end of request N: a pipelined (or eagerly-sent) request N+1 that
arrived in the same read() was silently dropped — and in reactor mode a re-armed
connection with buffered bytes stalled, because buffered userspace bytes never
fire a fresh kevent.

Each test drives raw sockets (urllib can't pipeline) and runs against BOTH
connection models (threaded and reactor) via a parametrized subprocess fixture.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

TEST_HOST = "127.0.0.1"

APP_CODE = f"""
import sys
sys.path.insert(0, ".")
from hyperdjango import HyperApp, Response

app = HyperApp(title="Pipelining Test Server")

@app.get("/echo/{{n}}")
async def echo(request, n):
    return Response.json({{"n": n}})

@app.post("/upload")
async def upload(request):
    body = await request.text()
    return Response.json({{"len": len(body), "body": body}})

if __name__ == "__main__":
    import os
    app.run(host="{TEST_HOST}", port=int(os.environ["PIPE_TEST_PORT"]))
"""


@pytest.fixture(scope="module", params=["threaded", "reactor"])
def server(request):
    """Start the native server in the given connection model on a unique port."""
    mode = request.param
    port = 19900 + (1 if mode == "reactor" else 0)
    env = {**_base_env(), "HYPER_HTTP_SERVER_MODEL": mode, "PIPE_TEST_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-c", APP_CODE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        env=env,
    )
    # Wait until it accepts connections.
    started = False
    for _ in range(50):
        time.sleep(0.1)
        try:
            with socket.create_connection((TEST_HOST, port), timeout=1):
                started = True
                break
        except OSError:
            if proc.poll() is not None:
                err = proc.stderr.read().decode()[:500] if proc.stderr else ""
                proc.kill()
                pytest.skip(f"[{mode}] server failed to start: {err}")
                return
    if not started:
        proc.kill()
        pytest.skip(f"[{mode}] server didn't start in time")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _base_env():
    import os

    return dict(os.environ)


def _connect(port):
    s = socket.create_connection((TEST_HOST, port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def _get(n):
    return (
        f"GET /echo/{n} HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n"
    ).encode()


def _post(body: bytes):
    return (
        f"POST /upload HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode() + body


def _read_responses(s, count, deadline=10.0):
    """Read exactly `count` complete HTTP responses; return [(status, body), ...]."""
    s.settimeout(deadline)
    buf = b""
    out = []
    while len(out) < count:
        while b"\r\n\r\n" in buf:
            head, _, rest = buf.partition(b"\r\n\r\n")
            cl = 0
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    cl = int(line.split(b":", 1)[1].strip())
            if len(rest) < cl:
                break
            status = int(head.split(b" ")[1])
            out.append((status, rest[:cl]))
            buf = rest[cl:]
            if len(out) >= count:
                return out
        chunk = s.recv(65536)
        if not chunk:
            raise AssertionError(
                f"connection closed after {len(out)}/{count} responses"
            )
        buf += chunk
    return out


def _expect(n):
    return f'{{"n":"{n}"}}'.encode()


def _assert_silent(s, window=0.5):
    """Assert the server sends NOTHING for `window` seconds, then restore `s`.

    Used after writing an INCOMPLETE request: the server must hold it and wait
    for the rest, and the only way to state "it did not answer" is to watch for
    a bounded stretch. This replaces a bare sleep between the two writes — it
    guarantees the same thing (the halves land as separate reads, so the split
    path is genuinely exercised) while also checking the claim the sleep left
    unexamined. Oversleeping on a loaded runner only widens the window, which
    can never flip a negative into a false pass.
    """
    old = s.gettimeout()
    s.settimeout(window)
    try:
        early = s.recv(65536)
    except TimeoutError:
        return  # nothing arrived — correct
    finally:
        s.settimeout(old)
    raise AssertionError(
        "server responded to an incomplete request: "
        + ("connection closed" if early == b"" else repr(early[:200]))
    )


def test_three_pipelined_in_one_write(server):
    """Three requests in ONE write must produce three correct, in-order responses."""
    s = _connect(server)
    try:
        ns = [10, 20, 30]
        s.sendall(b"".join(_get(n) for n in ns))
        resp = _read_responses(s, 3)
        assert [r[0] for r in resp] == [200, 200, 200]
        assert [r[1] for r in resp] == [_expect(n) for n in ns]
    finally:
        s.close()


def test_request_split_across_writes(server):
    """A request whose boundary lands mid-buffer (sent in two writes) is served once."""
    s = _connect(server)
    try:
        full = _get(42)
        mid = len(full) // 2
        s.sendall(full[:mid])
        _assert_silent(s)
        s.sendall(full[mid:])
        resp = _read_responses(s, 1)
        assert resp[0] == (200, _expect(42))
    finally:
        s.close()


def test_request_plus_partial_next(server):
    """Full request + a partial next: first answered now, second after the rest arrives."""
    s = _connect(server)
    try:
        r1, r2 = _get(101), _get(202)
        cut = len(r2) - 10
        s.sendall(r1 + r2[:cut])
        assert _read_responses(s, 1)[0] == (200, _expect(101))
        s.sendall(r2[cut:])
        assert _read_responses(s, 1)[0] == (200, _expect(202))
    finally:
        s.close()


def test_pipelined_bodies_then_get(server):
    """Pipelined POST bodies (exact Content-Length carry) + a trailing GET."""
    s = _connect(server)
    try:
        bodies = [b"hello", b'{"a":1,"b":[2,3]}', b"x" * 500]
        s.sendall(b"".join(_post(b) for b in bodies) + _get(777))
        resp = _read_responses(s, 4)
        for i, b in enumerate(bodies):
            j = json.loads(resp[i][1])
            assert j["len"] == len(b)
            assert j["body"] == b.decode()
        assert resp[3][1] == _expect(777)
    finally:
        s.close()


def test_deep_pipeline_exceeds_burst_cap(server):
    """More pipelined requests than the reactor burst cap (32) — all served in order."""
    s = _connect(server)
    try:
        ns = list(range(1000, 1120))  # 120 > REACTOR_BURST_MAX
        s.sendall(b"".join(_get(n) for n in ns))
        resp = _read_responses(s, len(ns))
        assert [r[1] for r in resp] == [_expect(n) for n in ns]
    finally:
        s.close()
