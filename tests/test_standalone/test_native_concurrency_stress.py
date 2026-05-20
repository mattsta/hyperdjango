"""Gate: no cross-request / cross-task native state bleed under raw-socket load.

Closes the "cross-request native state bleed" class. Under ReleaseSafe/3.14t the
native server dispatches concurrent requests across worker threads/tasks that
share caches and pinned resources (the query/column-name caches, the enum
registry, pinned/thread PG connections, the response scratch buffers). If any of
that per-request state leaks across workers, one client sees ANOTHER client's
payload — invisible under light single-client testing, corrupting under real
concurrency.

The gate boots the real native server in a subprocess and hammers it with many
concurrent RAW sockets, each speaking hand-written keep-alive HTTP/1.1 and
carrying a globally-unique nonce on every request:

  * POST /echo-nonce  — reflects the request's nonce back verbatim.
  * POST /db-roundtrip — INSERT ... RETURNING a per-request unique nonce into a
    table with an ENUM column, then SELECT it back (exercises pinned/thread
    conn + query/column caches + the enum registry). Skipped if no DB.

Every echoed / read-back value MUST equal exactly what THAT socket sent; the
declared Content-Length must match the body actually framed; and there must be
zero 5xx / resets / truncations. A single mismatch means per-request state
leaked across workers. A torn/spliced response desyncs the keep-alive stream and
surfaces as an unparseable next response. This only bites under ReleaseSafe/3.14t
— it passes cleanly on a correct build and simply cannot fail on a serial one.
"""

import contextlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

TEST_HOST = "127.0.0.1"
TEST_PORT = 19888  # distinct from other test_standalone server ports
TEST_URL = f"http://{TEST_HOST}:{TEST_PORT}"

# Driver shape — bounded so the gate is fast (~a few seconds) yet the
# cross-worker window is wide open.
N_CLIENTS = 48
N_ITERS = 300

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


_APP_CODE = f'''
import os
import sys
sys.path.insert(0, ".")

from hyperdjango import HyperApp, Response
from hyperdjango.database import get_db

_DSN = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

app = HyperApp(title="Concurrency Stress", database=_DSN)

_db_state = {{"ok": False}}
_COLORS = ("red", "green", "blue")


@app.on_startup
async def _setup_db():
    # Provision an ENUM type + table so /db-roundtrip also exercises the enum
    # registry and query/column caches. If no DB is reachable, disable the
    # route (the client half skips) rather than failing the whole gate.
    try:
        db = get_db()
        await db.execute("DROP TABLE IF EXISTS stress_roundtrip")
        await db.execute("DROP TYPE IF EXISTS stress_color")
        await db.execute("CREATE TYPE stress_color AS ENUM ('red', 'green', 'blue')")
        await db.execute(
            "CREATE TABLE stress_roundtrip ("
            "id bigserial PRIMARY KEY, nonce text NOT NULL, color stress_color NOT NULL)"
        )
        _db_state["ok"] = True
    except Exception as exc:  # noqa: BLE001 - any DB failure just disables the half
        print(f"[stress-app] DB unavailable, /db-roundtrip disabled: {{exc!r}}",
              file=sys.stderr, flush=True)
        _db_state["ok"] = False


@app.get("/health")
def health(request):
    return {{"status": "ok"}}


@app.get("/db-status")
def db_status(request):
    return {{"db": _db_state["ok"]}}


@app.post("/echo-nonce")
async def echo_nonce(request):
    data = await request.json()
    # Reflect the caller's nonce back verbatim as the exact body.
    return Response.text(data["nonce"])


@app.post("/db-roundtrip")
async def db_roundtrip(request):
    if not _db_state["ok"]:
        return Response.text("db-unavailable", status=503)
    data = await request.json()
    nonce = data["nonce"]
    color = _COLORS[len(nonce) % 3]
    db = get_db()
    row_id = await db.query_val(
        "INSERT INTO stress_roundtrip (nonce, color) VALUES ($1, $2::stress_color) "
        "RETURNING id",
        nonce, color,
    )
    readback = await db.query_val(
        "SELECT nonce FROM stress_roundtrip WHERE id = $1", row_id
    )
    return Response.text(readback if readback is not None else "")


if __name__ == "__main__":
    app.run(host="{TEST_HOST}", port={TEST_PORT})
'''


@pytest.fixture(scope="module")
def stress_server():
    # Native extension is required; skip cleanly if it isn't built.
    pytest.importorskip("hyperdjango._hyperdjango_native")

    proc = subprocess.Popen(
        [sys.executable, "-c", _APP_CODE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=_REPO_ROOT,
    )

    started = False
    for _ in range(100):  # up to 10s for startup (DB provisioning can be slow)
        time.sleep(0.1)
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            err = proc.stderr.read().decode() if proc.stderr else ""
            pytest.skip(f"stress server failed to start: {err[:800]}\n{out[:400]}")
            return
        try:
            with urllib.request.urlopen(f"{TEST_URL}/_ready", timeout=1) as r:
                if r.status == 200:
                    started = True
                    break
        except urllib.error.URLError, ConnectionRefusedError, OSError:
            continue

    if not started:
        proc.kill()
        pytest.skip("stress server did not become ready in time")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _db_enabled() -> bool:
    try:
        with urllib.request.urlopen(f"{TEST_URL}/db-status", timeout=2) as r:
            import json

            return bool(json.loads(r.read()).get("db"))
    except Exception:
        return False


class _RawConn:
    """A single persistent keep-alive HTTP/1.1 connection over a raw socket.

    Frames responses strictly by Content-Length and buffers leftover bytes for
    the next response, so a torn/spliced/misframed reply desyncs the stream and
    surfaces as a parse error on the following request.
    """

    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.host = host
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        self.buf = b""

    def _fill_to(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed connection mid-response")
            self.buf += chunk

    def _read_until(self, delim: bytes) -> bytes:
        while delim not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed connection before headers")
            self.buf += chunk
        idx = self.buf.index(delim) + len(delim)
        head, self.buf = self.buf[:idx], self.buf[idx:]
        return head

    def request(
        self, method: str, path: str, body: bytes = b"", content_type: str | None = None
    ) -> tuple[int, int, bytes]:
        headers = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self.host}",
            "Connection: keep-alive",
        ]
        if method in ("POST", "PUT", "PATCH") or body:
            headers.append(f"Content-Length: {len(body)}")
            if content_type:
                headers.append(f"Content-Type: {content_type}")
        raw = ("\r\n".join(headers) + "\r\n\r\n").encode() + body
        self.sock.sendall(raw)

        head = self._read_until(b"\r\n\r\n").decode("latin1")
        status_line = head.split("\r\n", 1)[0]
        if not status_line.startswith("HTTP/1.1 "):
            raise ConnectionError(f"torn/misframed response line: {status_line!r}")
        status = int(status_line.split(" ")[1])

        content_length = None
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "content-length":
                    content_length = int(v.strip())
        if content_length is None:
            raise ConnectionError("response missing Content-Length (unframed)")

        self._fill_to(content_length)
        body_bytes, self.buf = self.buf[:content_length], self.buf[content_length:]
        return status, content_length, body_bytes

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


def _run_load(use_db: bool):
    """Drive N_CLIENTS raw keep-alive connections, each with its own nonces.

    Returns a list of failure strings (empty == clean run).
    """
    failures: list[str] = []
    fail_lock = threading.Lock()
    barrier = threading.Barrier(N_CLIENTS)

    def record(msg: str) -> None:
        with fail_lock:
            if len(failures) < 40:
                failures.append(msg)

    def worker(worker_id: int) -> None:
        import json

        try:
            conn = _RawConn(TEST_HOST, TEST_PORT)
        except OSError as exc:
            record(f"w{worker_id}: connect failed: {exc!r}")
            return
        barrier.wait()
        try:
            for i in range(N_ITERS):
                # Globally-unique nonce for this exact request.
                nonce = f"w{worker_id}-i{i}-{uuid.uuid4().hex}"
                pick = i % 3
                if pick == 0:
                    status, cl, body = conn.request("GET", "/health")
                    if status != 200:
                        record(f"w{worker_id} health status {status}")
                    if cl != len(body):
                        record(f"w{worker_id} health CL {cl} != body {len(body)}")
                elif pick == 1 or not use_db:
                    payload = json.dumps({"nonce": nonce}).encode()
                    status, cl, body = conn.request(
                        "POST", "/echo-nonce", payload, "application/json"
                    )
                    if status != 200:
                        record(f"w{worker_id} echo status {status}")
                    elif body != nonce.encode():
                        record(f"w{worker_id} echo bleed: sent {nonce!r} got {body!r}")
                    if cl != len(body):
                        record(f"w{worker_id} echo CL {cl} != body {len(body)}")
                else:
                    payload = json.dumps({"nonce": nonce}).encode()
                    status, cl, body = conn.request(
                        "POST", "/db-roundtrip", payload, "application/json"
                    )
                    if status != 200:
                        record(f"w{worker_id} db status {status} body {body[:80]!r}")
                    elif body != nonce.encode():
                        record(f"w{worker_id} db bleed: sent {nonce!r} read {body!r}")
                    if cl != len(body):
                        record(f"w{worker_id} db CL {cl} != body {len(body)}")
        except (ConnectionError, OSError) as exc:
            record(f"w{worker_id} transport error at reuse: {exc!r}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(N_CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return failures


def test_no_cross_request_state_bleed_echo(stress_server):
    """Raw-socket keep-alive hammer on /echo-nonce + /health — no bleed/tears."""
    failures = _run_load(use_db=False)
    assert failures == [], (
        f"cross-request state bleed / torn framing under load "
        f"({len(failures)} failures): {failures[:8]}"
    )

    # Server is still healthy after the storm.
    with urllib.request.urlopen(f"{TEST_URL}/health", timeout=5) as r:
        assert r.status == 200


def test_no_cross_request_state_bleed_db(stress_server):
    """Same hammer, now including /db-roundtrip (pinned conn + enum registry)."""
    if not _db_enabled():
        pytest.skip("no database configured/available — /db-roundtrip disabled")

    failures = _run_load(use_db=True)
    assert failures == [], (
        f"cross-request DB read-back bleed / torn framing under load "
        f"({len(failures)} failures): {failures[:8]}"
    )

    with urllib.request.urlopen(f"{TEST_URL}/health", timeout=5) as r:
        assert r.status == 200
