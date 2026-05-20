"""Differential parity gate: ASGI in-process path == native Zig server path.

Two dispatch paths serve every request in this framework:

  Path A — the ASGI ``TestClient`` (``hyperdjango/testing.py``): builds a
           ``Request`` and drives ``app.handle`` in-process (what unit tests use).
  Path B — the native Zig HTTP server (``HyperApp.run`` → ``_run_native``): the
           PRODUCTION serving path, reached here over a raw socket to a live
           ``AppRunner`` subprocess.

They historically DRIFTED — the round-8 divergences: a ``str`` return became
``text/html`` on native but ``text/plain`` on ASGI; an unrecognized scalar
became a ``{"error": ...}`` 500 on native but a JSON 200 on ASGI;
``request.app`` / ``app.provide`` DI was missing on native so any handler
reaching through them 500'd only in production. This gate sends a FIXED CORPUS
through BOTH paths and asserts identical observable behavior (status, semantic
headers, body), so a divergence can never ship silently again.

The same ``app`` object is built at module top level: the ``TestClient`` uses it
directly in-process, and ``AppRunner`` imports THIS module in the server
subprocess (via a PYTHONPATH pointing at the scripts dir) and serves the same
app natively — so the two paths are guaranteed structurally identical inputs.

KNOWN-pending native-side divergences (being fixed in a parallel native wave)
are marked XFAIL with a precise reason so the REST of the parity contract is
enforced now, and the marker flips to XPASS (a loud reminder to enforce) once
the native side is fixed.

NOTE: drives the live Zig HTTP server, so it requires the rebuilt extension
(`uv run hyper-build`). Run via:  uv run hyper-test native_asgi_parity
"""

# hyper-test: e2e

import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_helper import AppRunner  # noqa: E402

from hyperdjango import HyperApp, Response  # noqa: E402
from hyperdjango.exceptions import HTTPException  # noqa: E402
from hyperdjango.testing import TestClient  # noqa: E402

PORT = 19151

# ── App under test ───────────────────────────────────────────────────────────
app = HyperApp(title="ParityFixture")


class Clock:
    """A tiny injectable service (exercises app.provide DI)."""

    def now(self) -> str:
        return "12:00"


app.provide(Clock, Clock())


@app.get("/str")
async def ret_str(request):
    # round-8: str must be text/plain (NOT text/html) on both paths.
    return "raw-string-value"


@app.get("/scalar")
async def ret_scalar(request):
    # round-8: an int scalar must be JSON 200 on both (NOT a 500 on native).
    return 424242


@app.get("/tuple")
async def ret_tuple(request):
    # (body, status) tuple coercion must be identical on both paths.
    return ({"created": True}, 201)


@app.get("/reqapp")
async def ret_reqapp(request):
    # round-8: request.app must be set on the native path too (render() and
    # shortcuts reach through it — missing => production-only 500).
    return {"title": request.app.title}


@app.get("/di")
async def ret_di(request, clock: Clock):
    # round-8: app.provide DI must inject on the native path exactly as on ASGI.
    return {"time": clock.now()}


@app.route("/head", methods=["GET", "HEAD"])
async def head_ep(request):
    return Response.text("head-endpoint-body")


@app.get("/raise405")
async def raise405(request):
    raise HTTPException(405, "Method Not Allowed")


@app.get("/guarded")
async def guarded(request):
    # Permission-style gate: denied unless the caller presents the role header.
    if request.headers.get("x-role") != "admin":
        raise HTTPException(403, "Forbidden")
    return {"ok": True}


@app.get("/boom")
async def boom(request):
    raise ValueError("intentional-parity-error")


@app.get("/slashed/")
async def slashed(request):
    # APPEND_SLASH: GET /slashed (no trailing slash) must 301 -> /slashed/ on
    # BOTH paths (native router now applies it, matching the ASGI router).
    return {"ok": True}


@app.get("/whoami")
async def whoami(request):
    # client_ip is resolved from the socket peer (getpeername) on native now,
    # not the unset-scope fallback. Over the loopback harness that peer is
    # 127.0.0.1 — the point is that it is a real resolved address.
    return {"client_ip": request.client_ip}


# ── Harness ──────────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0
XFAIL = 0

# Headers that legitimately differ between the in-process app and a wire server
# (framing, correlation, date) — dropped before comparing the SEMANTIC headers.
_VOLATILE = {
    "date",
    "server",
    "connection",
    "keep-alive",
    "content-length",
    "x-request-id",
    "transfer-encoding",
}


def _ok(name, detail=""):
    global PASS
    PASS += 1
    print(f"  PASS  {name}" + (f" ({detail})" if detail else ""))


def _bad(name, detail):
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}: {detail}")


def check_eq(name, a, b):
    if a == b:
        _ok(
            name,
            repr(a) if not isinstance(a, (bytes, str)) or len(repr(a)) < 60 else "",
        )
    else:
        _bad(name, f"ASGI={a!r}  !=  native={b!r}")


def check(name, cond, detail=""):
    if cond:
        _ok(name, detail)
    else:
        _bad(name, detail or "condition false")


def xfail(name, diverged_now, reason):
    """Record a KNOWN native-side divergence. Never fails the suite."""
    global XFAIL
    XFAIL += 1
    if diverged_now:
        print(f"  XFAIL {name}: {reason}")
    else:
        print(f"  XPASS {name}: NOW MATCHES — remove the xfail and enforce. ({reason})")


def norm_headers(pairs):
    """Lowercase keys, drop volatile/framing headers -> comparable dict."""
    out = {}
    for k, v in pairs:
        k = k.lower()
        if k in _VOLATILE:
            continue
        out[k] = v
    return out


# ── Path A: ASGI TestClient ──────────────────────────────────────────────────
_client = TestClient(app)


def asgi_fetch(method, path, headers=None):
    resp = _client.request(method, path, headers=headers)
    pairs = [(k, v) for k, v in resp.headers.items()]
    return resp.status, norm_headers(pairs), resp.body


# ── Path B: native Zig server over a raw socket ──────────────────────────────
def native_fetch(host, port, method, path, headers=None):
    hdr_lines = [f"Host: {host}:{port}", "Connection: close"]
    for k, v in (headers or {}).items():
        hdr_lines.append(f"{k}: {v}")
    req = (
        f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(hdr_lines) + "\r\n\r\n"
    ).encode()
    with socket.create_connection((host, port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(req)
        s.settimeout(5)
        buf = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except TimeoutError:
            pass
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    pairs = []
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            pairs.append((k.strip().decode(), v.strip().decode()))
    return status, norm_headers(pairs), body


def both(host, port, method, path, headers=None):
    a_status, a_head, a_body = asgi_fetch(method, path, headers)
    n_status, n_head, n_body = native_fetch(host, port, method, path, headers)
    return (a_status, a_head, a_body), (n_status, n_head, n_body)


def _lower_headers(d):
    return {k.lower(): v.lower() for k, v in d.items()}


# ── The corpus ───────────────────────────────────────────────────────────────
def run(host, port):
    # 1. str return → identical status, Content-Type (text/plain), body.
    a, n = both(host, port, "GET", "/str")
    check_eq("str: status", a[0], n[0])
    check_eq(
        "str: Content-Type",
        _lower_headers(a[1]).get("content-type"),
        _lower_headers(n[1]).get("content-type"),
    )
    check_eq("str: body", a[2], n[2])

    # 2. scalar/int return → identical (JSON 200, not a native 500).
    a, n = both(host, port, "GET", "/scalar")
    check_eq("scalar: status", a[0], n[0])
    check_eq("scalar: headers", a[1], n[1])
    check_eq("scalar: body", a[2], n[2])

    # 3. (body, status) tuple → identical coercion.
    a, n = both(host, port, "GET", "/tuple")
    check_eq("tuple: status (201)", a[0], n[0])
    check_eq("tuple: headers", a[1], n[1])
    check_eq("tuple: body", a[2], n[2])

    # 4. request.app reachable on both paths.
    a, n = both(host, port, "GET", "/reqapp")
    check_eq("request.app: status", a[0], n[0])
    check_eq("request.app: body", a[2], n[2])

    # 5. app.provide DI injected on both paths.
    a, n = both(host, port, "GET", "/di")
    check_eq("DI (app.provide): status", a[0], n[0])
    check_eq("DI (app.provide): body", a[2], n[2])

    # 6. HEAD request. Status + semantic headers must match. The BODY is NOT
    #    compared here: the wire server strips the HEAD body (RFC 7230, verified
    #    in the conformance gate) while the in-process TestClient models
    #    app.handle and returns the would-be GET body — so a body mismatch is a
    #    layer artifact, not a real divergence.
    a, n = both(host, port, "HEAD", "/head")
    check_eq("HEAD: status", a[0], n[0])
    check_eq(
        "HEAD: Content-Type",
        _lower_headers(a[1]).get("content-type"),
        _lower_headers(n[1]).get("content-type"),
    )

    # 7. 404 → unified {"detail","status"} body on both paths.
    a, n = both(host, port, "GET", "/no-such-route-xyz")
    check_eq("404: status", a[0], n[0])  # both 404 — status parity holds
    if a[2] == n[2]:
        check_eq("404: unified {detail,status} body", a[2], n[2])
    else:
        xfail(
            "404: unified {detail,status} body",
            True,
            'native Zig fast-path 404 emits {"error":"Not Found"} with '
            "Content-Type application/json (no charset), while ASGI emits the "
            'unified {"detail":"Not Found","status":404}; the no-route 404 short-'
            "circuits in Zig before Python's error contract. Native-wave fix.",
        )

    # 8. 405 (raised HTTPException) → identical unified body.
    a, n = both(host, port, "GET", "/raise405")
    check_eq("405: status", a[0], n[0])
    check_eq("405: unified {detail,status} body", a[2], n[2])

    # 9. Endpoint behind a permission (denied) → identical 403 unified body.
    a, n = both(host, port, "GET", "/guarded")
    check_eq("permission 403: status", a[0], n[0])
    check_eq("permission 403: unified {detail,status} body", a[2], n[2])
    # And granting the permission yields an identical 200 on both.
    a, n = both(host, port, "GET", "/guarded", headers={"x-role": "admin"})
    check_eq("permission granted: status", a[0], n[0])
    check_eq("permission granted: body", a[2], n[2])

    # 10. Error-raising handler → same STATUS CLASS (5xx). Bodies differ by
    #     design (debug HTML vs generic JSON depending on each path's debug
    #     flag), so only the class is asserted, per the parity contract.
    a, n = both(host, port, "GET", "/boom")
    check("error handler: ASGI status is 5xx", a[0] // 100 == 5, f"got {a[0]}")
    check("error handler: native status is 5xx", n[0] // 100 == 5, f"got {n[0]}")
    check_eq("error handler: same status class", a[0] // 100, n[0] // 100)

    # ── Native-wave items, now IMPLEMENTED and ENFORCED (were xfail placeholders).
    # APPEND_SLASH: native GET /slashed (no trailing slash) must 301 to /slashed/.
    n_status, n_head, _ = native_fetch(host, port, "GET", "/slashed")
    check_eq("APPEND_SLASH: native 301", n_status, 301)
    loc = _lower_headers(n_head).get("location", "")
    check(
        "APPEND_SLASH: native Location ends with /slashed/",
        loc.endswith("/slashed/"),
        loc,
    )

    # client_ip: native resolves the socket peer via getpeername (was the
    # unset-scope fallback). Over loopback that is a real 127.0.0.1, not empty.
    import json as _json

    _, _, wb = native_fetch(host, port, "GET", "/whoami")
    got_ip = _json.loads(wb or b"{}").get("client_ip")
    check_eq("client_ip: native resolves the loopback peer", got_ip, "127.0.0.1")


def main():
    print("=" * 64)
    print("Native Zig vs ASGI differential parity")
    print("=" * 64)

    scripts_dir = str(Path(__file__).resolve().parent)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = scripts_dir + (os.pathsep + existing if existing else "")

    with AppRunner(
        "test_native_asgi_parity:app", port=PORT, env={"PYTHONPATH": pythonpath}
    ) as r:
        run(r.host, r.port)

    print(
        f"\nResults: {PASS} passed, {FAIL} failed, "
        f"{XFAIL} xfail (known native-wave gaps)"
    )
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
