"""Launch uvicorn multi-worker with a proto-correct shared listen socket.

`python -m uvicorn --workers N` pre-binds the socket its workers share via
`Config.bind_socket()`, which creates it as `socket.socket(family=...)` — that
constructor leaves `.proto == 0`. Sockets returned by `accept()` inherit the
listener's proto, and asyncio's `_set_nodelay()` only applies TCP_NODELAY when
`sock.proto == IPPROTO_TCP`, so in multi-worker mode every accepted connection
silently keeps Nagle ENABLED. A response written as two small segments
(headers, then body) then stalls on the peer's delayed ACK — ~40 ms per
request — flattening any closed-loop benchmark to `concurrency / 40 ms`
regardless of worker count. Observed here: FastAPI pinned at ~24.6k rps for
W=8..128 with p99 locked at ~43 ms, and 41 ms single-connection latency vs
219 us with `--workers 1` (the single-worker path hands host/port to
`loop.create_server()`, which builds the socket from getaddrinfo with the
proto filled in — so TCP_NODELAY works there, hiding the bug).

This launcher mirrors uvicorn.main's multi-worker path exactly, but binds the
listen socket itself with an explicit IPPROTO_TCP so accepted connections get
TCP_NODELAY — the benchmark then measures the framework, not a delayed-ACK
artifact.

Usage: python -m benchmarks.http.apps.uvicorn_main <app> <host> <port> <workers>
"""

from __future__ import annotations

import inspect
import socket
import sys

from uvicorn import Config, Server
from uvicorn.supervisors import Multiprocess


def main(argv: list[str]) -> int:
    app, host, port, workers = argv[0], argv[1], int(argv[2]), int(argv[3])
    config = Config(app, host=host, port=port, workers=workers, log_level="warning")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    # The supervisor API changed across uvicorn versions: older releases take
    # (config, target=server.run, sockets=[...]) while newer ones build the
    # per-worker Server themselves and take only (config, sockets=[...]).
    # Branch on the signature so the launcher tracks either.
    if "target" in inspect.signature(Multiprocess.__init__).parameters:
        Multiprocess(config, target=Server(config).run, sockets=[sock]).run()
    else:
        Multiprocess(config, sockets=[sock]).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
