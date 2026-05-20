#!/usr/bin/env python3
# hyper-test: e2e
"""Native short-circuit responses carry security headers (round-15, C1-B1).

Framework-generated native responses (the no-route 404, framing errors) used to
skip the Python middleware chain and ship WITHOUT the security headers
SecurityHeadersMiddleware sets on routed responses. The app now pushes the
static block to the native server; this asserts a native 404 carries them.
"""

import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_helper import AppRunner  # noqa: E402

from hyperdjango import HyperApp, Response  # noqa: E402
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware  # noqa: E402

app = HyperApp(title="sec-headers-fixture")
app.use(SecurityHeadersMiddleware(frame_options="DENY", content_type_nosniff=True))


@app.get("/ok")
async def ok(request):
    return Response.text("ok")


PORT = 18821
_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS {name}")
    else:
        _f += 1
        print(f"  FAIL {name} — {detail}")


def raw_get(host, port, path):
    with socket.create_connection((host, port), timeout=3) as s:
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.split(b"\r\n\r\n", 1)[0].lower()


def main():
    scripts_dir = str(Path(__file__).resolve().parent)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = scripts_dir + (os.pathsep + existing if existing else "")
    runner = AppRunner(
        "test_native_security_headers_r15:app",
        host="127.0.0.1",
        port=PORT,
        env={"PYTHONPATH": pythonpath},
    )
    runner.start()
    try:
        # A routed 200 gets security headers from the middleware chain.
        h200 = raw_get(runner.host, runner.port, "/ok")
        check("routed 200 has x-frame-options", b"x-frame-options: deny" in h200, h200)
        check("routed 200 has nosniff", b"x-content-type-options: nosniff" in h200)
        check(
            "routed 200 no duplicate x-frame-options",
            h200.count(b"x-frame-options:") == 1,
            h200,
        )

        # The framework-generated no-route 404 now ALSO carries them (C1-B1).
        h404 = raw_get(runner.host, runner.port, "/no-such-route")
        check("native 404 status", b" 404 " in h404, h404)
        check(
            "native 404 has x-frame-options (C1-B1)",
            b"x-frame-options: deny" in h404,
            h404,
        )
        check(
            "native 404 has nosniff (C1-B1)",
            b"x-content-type-options: nosniff" in h404,
            h404,
        )
        check(
            "native 404 no duplicate x-frame-options",
            h404.count(b"x-frame-options:") == 1,
            h404,
        )
    finally:
        runner.stop()
    print(f"\n{_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
