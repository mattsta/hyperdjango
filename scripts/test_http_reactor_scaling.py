"""Proof that HTTP_SERVER_MODEL=reactor removes the keep-alive connection ceiling.

# hyper-test: e2e

The threaded model binds one worker thread to a connection for its whole life
(it blocks in read() between requests), so concurrent live connections are
capped at the thread-pool size. The reactor model parks IDLE keep-alive
connections in a kqueue/epoll reactor (zero worker threads) and dispatches a
connection to a worker only for the duration of one request.

Deterministic proof (pool size = 1):
  - reactor: after connection A's request, A returns to the reactor and the
    single worker is FREE, so a second connection B is served. B responds.
  - threaded: after A's request, the worker loops back to a blocking read on A
    (pinned), so B cannot be served. B stalls (no response within the window) —
    demonstrating the ceiling that the reactor removes.

Plus: reactor with pool=2 serves 8 simultaneous keep-alive connections (far
above the pool size), which the threaded model structurally cannot.
"""

import socket

from e2e_helper import TEST_PORTS, AppRunner

HOST = "127.0.0.1"
APP = "services.hello.app:app"

RESULTS = {"passed": 0, "failed": 0}


def check(name, cond, detail=""):
    if cond:
        RESULTS["passed"] += 1
        print(f"  ✓ {name}")
    else:
        RESULTS["failed"] += 1
        print(f"  ✗ {name} {detail}")


def _open(host, port):
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(5)
    return s


def _send(s, host, port, path="/"):
    s.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n".encode()
    )


def _read_response(s, timeout):
    """Read one full HTTP/1.1 response (status + headers + Content-Length body)
    so the socket stays clean for the next keep-alive request. Returns the
    status code, or None on timeout/close."""
    s.settimeout(timeout)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                return None
            buf += chunk
    except TimeoutError, OSError:
        return None
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split(b" ")[1])
    # Consume the body so the connection is ready for the next request.
    cl = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            cl = int(line.split(b":", 1)[1].strip())
    body = rest
    try:
        while len(body) < cl:
            chunk = s.recv(4096)
            if not chunk:
                break
            body += chunk
    except TimeoutError, OSError:
        pass
    return status


def _run(port_key, model, pool_size):
    return AppRunner(
        APP,
        host=HOST,
        port=TEST_PORTS[port_key],
        readiness_path="/",
        env={
            "HYPER_HTTP_SERVER_MODEL": model,
            "HYPER_THREAD_POOL_SIZE": str(pool_size),
        },
    )


def test_reactor_frees_worker():
    print("-- reactor (pool=1): idle keep-alive conn does NOT pin the worker --")
    port = TEST_PORTS["http_reactor_scaling_reactor"]
    with _run("http_reactor_scaling_reactor", "reactor", 1):
        a = _open(HOST, port)
        try:
            _send(a, HOST, port)
            check("A served", _read_response(a, 5) == 200)
            # A is now an IDLE keep-alive connection. With pool=1, the threaded
            # model would have the worker pinned to A — but the reactor freed it.
            b = _open(HOST, port)
            try:
                _send(b, HOST, port)
                check(
                    "B served while A held open (worker freed)",
                    _read_response(b, 5) == 200,
                )
                # A is still usable (reactor re-dispatches its next request).
                _send(a, HOST, port)
                check(
                    "A's 2nd request served (keep-alive via reactor)",
                    _read_response(a, 5) == 200,
                )
            finally:
                b.close()
        finally:
            a.close()


def test_threaded_pins_worker():
    print("-- threaded (pool=1): idle keep-alive conn PINS the worker (ceiling) --")
    port = TEST_PORTS["http_reactor_scaling_threaded"]
    with _run("http_reactor_scaling_threaded", "threaded", 1):
        a = _open(HOST, port)
        try:
            _send(a, HOST, port)
            check("A served", _read_response(a, 5) == 200)
            # A is held open. The single worker is now blocked reading A for its
            # next request, so B cannot be served — proving the ceiling.
            b = _open(HOST, port)
            try:
                _send(b, HOST, port)
                stalled = _read_response(b, 2) is None
                check("B stalls while A held open (worker pinned = ceiling)", stalled)
            finally:
                b.close()
        finally:
            a.close()


def test_reactor_many_keepalive():
    print("-- reactor (pool=2): 8 simultaneous keep-alive connections --")
    port = TEST_PORTS["http_reactor_scaling_reactor"]
    with _run("http_reactor_scaling_reactor", "reactor", 2):
        conns = [_open(HOST, port) for _ in range(8)]
        try:
            for s in conns:
                _send(s, HOST, port)
            ok = sum(1 for s in conns if _read_response(s, 5) == 200)
            check("all 8 keep-alive conns served with pool=2", ok == 8, f"got {ok}/8")
        finally:
            for s in conns:
                s.close()


def test_reactor_connection_burst():
    """Every connection of a simultaneous burst must be served — twice.

    A burst that outruns the acceptor overflows the kernel listen queue, and an
    overflowed connection is dropped with NO error visible to either side: the
    client believes it is connected, sends its request and waits out an
    exponential SYN-ACK backoff, so it reports zero responses and zero errors.
    Opening every socket BEFORE any request is sent is what makes this a burst.
    The second round re-uses the same sockets, so it also covers the re-arm path
    (a connection returned to the reactor after being served).
    """
    print("-- reactor (pool=2): 64-connection burst, all served, twice --")
    port = TEST_PORTS["http_reactor_scaling_reactor"]
    n = 64
    with _run("http_reactor_scaling_reactor", "reactor", 2):
        conns = [_open(HOST, port) for _ in range(n)]
        try:
            for rnd in (1, 2):
                for s in conns:
                    _send(s, HOST, port)
                ok = sum(1 for s in conns if _read_response(s, 10) == 200)
                check(
                    f"all {n} burst conns served (round {rnd})",
                    ok == n,
                    f"got {ok}/{n}",
                )
        finally:
            for s in conns:
                s.close()


def main() -> int:
    print("HTTP reactor connection-scaling proof")
    print("=" * 60)
    test_reactor_frees_worker()
    test_threaded_pins_worker()
    test_reactor_many_keepalive()
    test_reactor_connection_burst()
    print("=" * 60)
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    return 1 if RESULTS["failed"] else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
