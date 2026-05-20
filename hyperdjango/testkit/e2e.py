"""End-to-end HTTP/WebSocket primitives for driving a live app server.

Provides:
- :class:`AppRunner`: starts a Zig HTTP server subprocess with threaded output
  streaming, waits for it to accept connections and report readiness, and tears
  it down cleanly.
- :class:`Session`: a cookie-persisting HTTP client (CSRF double-submit aware).
- :class:`E2EResponse` plus ``http_get`` / ``http_post`` / ``http_put`` /
  ``http_delete``, ``sse_post``, and ``build_multipart``.

All output from the server process is captured and printed to stderr in real
time via background threads (required because Zig worker threads write to
different file descriptors than Python).

The suite-local port registry ``TEST_PORTS`` is NOT part of this module; it
lives in ``scripts/e2e_helper.py`` alongside the seed-credential constants.
"""

import atexit
import contextlib
import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# Track all AppRunner instances for atexit cleanup
_active_runners: list[AppRunner] = []

# Connect-time transients under full-suite pressure: the OS ephemeral-port
# range and accept queues are shared by every parallel test, so a client
# connect can fail with EADDRNOTAVAIL ("Can't assign requested address") /
# EADDRINUSE from TIME_WAIT churn, or ECONNREFUSED / EAGAIN from a
# momentarily full accept queue. None of these mean the server is broken —
# they clear as ports and queue slots recycle. The deadline mirrors the
# framework's own policy for this exact class (serviceclient's
# _LOCAL_RESOURCE_RETRY_DEADLINE); a shorter local budget is what let this
# class keep resurfacing per-file.
CONNECT_RETRY_DEADLINE_S = 30.0
CONNECT_RETRY_BACKOFF_S = 0.05
_TRANSIENT_CONNECT_ERRNOS = frozenset(
    {
        errno.EADDRNOTAVAIL,
        errno.EADDRINUSE,
        errno.ECONNREFUSED,
        errno.EAGAIN,
    }
)


def connect_with_retry(
    host: str,
    port: int,
    timeout: float = 5.0,
    deadline_s: float = CONNECT_RETRY_DEADLINE_S,
) -> socket.socket:
    """Open a TCP connection, waiting out connect-time local-resource
    exhaustion — the ONE retry policy every test client shares.

    Returns the connected socket. Re-raises the underlying OSError once the
    deadline elapses, so a genuinely-down server still fails (and fails with
    the real errno), rather than being masked by retries.
    """
    deadline = time.monotonic() + deadline_s
    backoff = CONNECT_RETRY_BACKOFF_S
    while True:
        try:
            return socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            if exc.errno not in _TRANSIENT_CONNECT_ERRNOS:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 0.5)


def _cleanup_all_runners():
    """Kill all server subprocesses on interpreter exit.

    This catches cases where a test script crashes without reaching
    AppRunner.__exit__. Without this, Zig server processes become
    zombies holding ports indefinitely.
    """
    for runner in list(_active_runners):
        runner.stop()
    _active_runners.clear()


atexit.register(_cleanup_all_runners)


@dataclass
class E2EResponse:
    status: int
    headers: dict[str, str]
    body: str

    @property
    def json(self) -> dict | list:
        return json.loads(self.body)

    @property
    def cookies(self) -> dict[str, str]:
        """Extract cookies from Set-Cookie headers."""
        result: dict[str, str] = {}
        raw = self.headers.get("set-cookie", "")
        if raw:
            # set-cookie: name=value; Path=/; ...
            pair = raw.split(";")[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


class Session:
    """HTTP session that persists cookies across requests.

    Usage:
        s = Session(base_url="http://localhost:8000")
        s.get("/login")          # sets CSRF cookie
        s.post("/login", ...)    # sends CSRF cookie, captures session cookie
        s.get("/dashboard")      # sends session + CSRF cookies
    """

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/")
        self.cookie_jar: dict[str, str] = {}

    def _merge_cookies(self, response: E2EResponse) -> None:
        """Extract Set-Cookie from response and add to jar."""
        raw = response.headers.get("set-cookie", "")
        if raw:
            pair = raw.split(";")[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                self.cookie_jar[k.strip()] = v.strip()

    def _cookie_header(self) -> str:
        """Build Cookie header from jar."""
        return "; ".join(f"{k}={v}" for k, v in self.cookie_jar.items())

    def _headers(
        self, extra: dict[str, str] | None = None, include_csrf: bool = False
    ) -> dict[str, str]:
        h = dict(extra or {})
        cookie = self._cookie_header()
        if cookie:
            h["Cookie"] = cookie
        # For non-GET requests, include CSRF token as header (double-submit pattern)
        if include_csrf and "csrftoken" in self.cookie_jar:
            h["X-CSRFToken"] = self.cookie_jar["csrftoken"]
        return h

    def get(self, path: str, headers: dict[str, str] | None = None) -> E2EResponse:
        r = http_get(f"{self.base_url}{path}", headers=self._headers(headers))
        self._merge_cookies(r)
        return r

    def post(
        self,
        path: str,
        body: str | bytes | dict | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> E2EResponse:
        r = http_post(
            f"{self.base_url}{path}",
            body=body,
            content_type=content_type,
            headers=self._headers(headers, include_csrf=True),
        )
        self._merge_cookies(r)
        return r

    def put(
        self,
        path: str,
        body: str | bytes | dict | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> E2EResponse:
        r = http_put(
            f"{self.base_url}{path}",
            body=body,
            content_type=content_type,
            headers=self._headers(headers, include_csrf=True),
        )
        self._merge_cookies(r)
        return r

    def patch(
        self,
        path: str,
        body: str | bytes | dict | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> E2EResponse:
        r = _http_request(
            "PATCH",
            f"{self.base_url}{path}",
            body=body,
            content_type=content_type,
            headers=self._headers(headers, include_csrf=True),
        )
        self._merge_cookies(r)
        return r

    def post_multipart(
        self,
        path: str,
        fields: dict[str, str | bytes | tuple[str, bytes, str]],
        headers: dict[str, str] | None = None,
    ) -> E2EResponse:
        """POST multipart/form-data with file upload support."""
        body, ct = build_multipart(fields)
        r = _http_request(
            "POST",
            f"{self.base_url}{path}",
            body=body,
            content_type=ct,
            headers=self._headers(headers, include_csrf=True),
        )
        self._merge_cookies(r)
        return r

    def delete(self, path: str, headers: dict[str, str] | None = None) -> E2EResponse:
        r = http_delete(
            f"{self.base_url}{path}",
            headers=self._headers(headers, include_csrf=True),
        )
        self._merge_cookies(r)
        return r


def build_multipart(
    fields: dict[str, str | bytes | tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    """Build a multipart/form-data body.

    fields values can be:
    - str: plain text field
    - bytes: raw binary field
    - tuple (filename, content_bytes, content_type): file upload field

    Returns (body_bytes, content_type_header).
    """
    boundary = f"----HyperTestBoundary{os.getpid()}"
    parts: list[bytes] = []
    for name, value in fields.items():
        if isinstance(value, tuple):
            filename, content, ct = value
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {ct}\r\n\r\n".encode()
                + content
                + b"\r\n"
            )
        elif isinstance(value, bytes):
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value
                + b"\r\n"
            )
        else:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    ct = f"multipart/form-data; boundary={boundary}"
    return body, ct


def sse_post(
    url: str,
    body: dict | str | bytes,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, list[str]]:
    """POST to an SSE endpoint and collect data lines via raw socket.

    Returns (status_code, list_of_data_lines).
    http.client can't read chunked SSE, so we use raw socket.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    send_headers = dict(headers or {})
    if isinstance(body, dict):
        send_body = json.dumps(body).encode()
        send_headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        send_body = body.encode()
    else:
        send_body = body

    send_headers["Host"] = f"{host}:{port}"
    send_headers["Content-Length"] = str(len(send_body))
    send_headers["Connection"] = "close"
    send_headers["Accept"] = "text/event-stream"

    header_lines = "".join(f"{k}: {v}\r\n" for k, v in send_headers.items())
    request_bytes = f"POST {path} HTTP/1.1\r\n{header_lines}\r\n".encode() + send_body

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall(request_bytes)

    # Read response
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            # Stop after seeing [DONE] or double-newline-separated end
            if b"[DONE]" in buf:
                break
    except TimeoutError:
        pass
    finally:
        sock.close()

    # Parse HTTP response
    raw = buf.decode("utf-8", errors="replace")
    if "\r\n\r\n" not in raw:
        return 0, []
    header_part, body_part = raw.split("\r\n\r\n", 1)
    status_line = header_part.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1]) if " " in status_line else 0

    # Extract data: lines from SSE body (handle chunked transfer encoding)
    data_lines: list[str] = []
    for line in body_part.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    return status_code, data_lines


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port. Prevents stale server conflicts."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            if pid.isdigit():
                os.kill(int(pid), signal.SIGKILL)
    except subprocess.TimeoutExpired, OSError, ProcessLookupError:
        pass


def _stream_pipe(pipe, label: str, lines: list[str]) -> None:
    """Read lines from a pipe in a background thread, print and collect."""
    for raw in pipe:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        lines.append(line)
        print(f"[{label}] {line}", file=sys.stderr, flush=True)


@dataclass
class AppRunner:
    """Start and manage a HyperDjango app subprocess for e2e testing.

    Args:
        module_app: "services.rest_api.app:app" format (module:attribute)
        host: bind address
        port: bind port
        timeout: seconds to wait for the server to accept connections
    """

    module_app: str
    host: str = "127.0.0.1"
    port: int = 18080
    timeout: float = 60.0
    readiness_path: str = "/_ready"  # HTTP path to poll for app readiness
    env: dict[str, str] = field(default_factory=dict)  # Extra env vars for subprocess
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _stdout_lines: list[str] = field(default_factory=list, init=False, repr=False)
    _stderr_lines: list[str] = field(default_factory=list, init=False, repr=False)

    def url(self, path: str = "") -> str:
        return f"http://{self.host}:{self.port}{path}"

    def start(self) -> None:
        """Start the server subprocess and wait for it to accept connections."""
        # Kill any stale process on our port from prior runs
        _kill_port(self.port)

        module, attr = self.module_app.split(":")
        script = (
            f"import {module}; "
            f"app = {module}.{attr}; "
            f"app.run(host='{self.host}', port={self.port})"
        )
        # The harness clones its own process environment to seed the spawned
        # app server's environment, then overlays HYPER_DEBUG and caller vars.
        # env-boundary: subprocess-env propagation, not a framework config read.
        proc_env = os.environ.copy()
        proc_env.setdefault("HYPER_DEBUG", "1")  # E2E tests run in debug mode
        # Bounded capacity for test servers. The server self-scales workers
        # (and its DB pool) to the machine's usable cores; on a big box the
        # parallel suite then runs DOZENS of e2e servers, each sized as if it
        # owned all cores — observed on a 256-core machine as thousands of
        # worker threads + hundreds of DB connections per app, with servers
        # randomly failing startup (49 files, nondeterministic set, all green
        # once capped). An e2e test exercises behavior, not capacity, so cap
        # the budget; capacity/scaling tests override via self.env.
        proc_env.setdefault("HYPER_CPU_BUDGET", "8")
        proc_env.update(self.env)  # Caller can override
        self._proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            env=proc_env,
            start_new_session=True,  # Own process group for clean killpg
        )
        _active_runners.append(self)

        # Stream stdout/stderr in background threads
        t_out = threading.Thread(
            target=_stream_pipe,
            args=(self._proc.stdout, "stdout", self._stdout_lines),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_stream_pipe,
            args=(self._proc.stderr, "stderr", self._stderr_lines),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        # Phase 1: Wait for TCP accept
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                # Include BOTH streams: the native server reports its startup
                # failure through the framework logger ("Zig server failed"),
                # which lands on STDOUT — a stderr-only message reads as an
                # unexplained exit ("stderr:" empty) for exactly the failures
                # this error exists to explain.
                raise RuntimeError(
                    f"Server process exited with code {self._proc.returncode}\n"
                    f"stdout: {''.join(self._stdout_lines[-20:])}\n"
                    f"stderr: {''.join(self._stderr_lines[-20:])}"
                )
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    break  # TCP is up
            except ConnectionRefusedError, OSError:
                time.sleep(0.05)
        else:
            raise TimeoutError(
                f"Server did not accept TCP within {self.timeout}s\n"
                f"stderr: {''.join(self._stderr_lines[-20:])}"
            )

        # Phase 2: Wait for HTTP readiness (app fully initialized,
        # on_startup hooks completed, routes registered)
        readiness_path = self.readiness_path
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Server died during startup\n"
                    f"stderr: {''.join(self._stderr_lines[-20:])}"
                )
            try:
                r = http_get(f"http://{self.host}:{self.port}{readiness_path}")
                if r.status == 200:
                    return  # App is ready (/_ready returns 200 after all routes registered)
            # blind-except: during readiness polling any connect/HTTP error means
            # the server is still starting; retry until the deadline below.
            except Exception:
                pass
            time.sleep(0.05)
        raise TimeoutError(
            f"Server not ready (HTTP {readiness_path}) within {self.timeout}s\n"
            f"stderr: {''.join(self._stderr_lines[-20:])}"
        )

    def stop(self) -> None:
        """Gracefully stop the server: SIGTERM → wait → SIGKILL fallback."""
        if self in _active_runners:
            _active_runners.remove(self)
        if self._proc is not None:
            pgid = None
            try:
                pgid = os.getpgid(self._proc.pid)
            except ProcessLookupError, OSError:
                self._proc = None
                return

            # Phase 1: SIGTERM for graceful shutdown
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError, OSError:
                self._proc = None
                return

            try:
                self._proc.wait(timeout=5)
                self._proc = None
                return
            except subprocess.TimeoutExpired:
                pass

            # Phase 2: SIGKILL if still alive after 5s
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=3)
            self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


def http_get(url: str, headers: dict[str, str] | None = None) -> E2EResponse:
    """Simple HTTP GET that returns an E2EResponse."""
    return _http_request("GET", url, headers=headers)


def http_post(
    url: str,
    body: str | bytes | dict | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
) -> E2EResponse:
    """Simple HTTP POST that returns an E2EResponse."""
    return _http_request(
        "POST", url, body=body, headers=headers, content_type=content_type
    )


def http_put(
    url: str,
    body: str | bytes | dict | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
) -> E2EResponse:
    """Simple HTTP PUT that returns an E2EResponse."""
    return _http_request(
        "PUT", url, body=body, headers=headers, content_type=content_type
    )


def http_delete(url: str, headers: dict[str, str] | None = None) -> E2EResponse:
    """Simple HTTP DELETE that returns an E2EResponse."""
    return _http_request("DELETE", url, headers=headers)


def _http_request(
    method: str,
    url: str,
    body: str | bytes | dict | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
) -> E2EResponse:
    """Low-level HTTP request using http.client (no external deps)."""
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)

    send_headers = dict(headers or {})
    send_body = None

    if body is not None:
        if isinstance(body, dict):
            send_body = json.dumps(body).encode()
            send_headers.setdefault("Content-Type", content_type)
        elif isinstance(body, str):
            send_body = body.encode()
            send_headers.setdefault("Content-Type", content_type)
        else:
            send_body = body
            send_headers.setdefault("Content-Type", content_type)

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    # Retry transient connection errors under parallel-suite pressure. The
    # full suite competes for the OS ephemeral-port range and accept queues, so
    # a client connect can transiently fail with EADDRNOTAVAIL ("Can't assign
    # requested address", errno 49), ECONNREFUSED, or a reset. These clear as
    # ports/queue slots recycle, so retry against a wall-clock DEADLINE with
    # capped backoff (mirroring the native pg driver's self-healing connect)
    # rather than a couple of sub-second attempts.
    last_err = None
    deadline = time.monotonic() + 10.0
    backoff = 0.05
    while True:
        try:
            conn.request(method, path, body=send_body, headers=send_headers)
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8", errors="replace")
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            conn.close()
            break
        except (
            ConnectionResetError,
            ConnectionRefusedError,
            BrokenPipeError,
            OSError,
        ) as e:
            last_err = e
            conn.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 0.5)
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)

    return E2EResponse(
        status=resp.status,
        headers=resp_headers,
        body=resp_body,
    )
