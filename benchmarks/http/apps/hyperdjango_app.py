"""hyperdjango benchmark app (native server). See apps/__init__.py.

The connection model (threaded vs reactor) is selected at run time via
HYPER_HTTP_SERVER_MODEL, so the SAME app binary benchmarks both.
"""

from hyperdjango import HyperApp, Response
from hyperdjango.native import fast_json_dumps

app = HyperApp(title="http-bench")


@app.get("/health")
async def health(request):
    return Response.json({"ok": True})


@app.get("/json")
async def json_ep(request):
    n = int(request.query("n", "0") or "0")
    return Response.json({"data": "x" * n})


@app.get("/plaintext")
async def plaintext(request):
    return Response.text("Hello, World!")


# Same bytes on the wire as /json?n=N, but the body is built ONCE instead of per
# request. /json allocates and serialises a fresh N-byte payload every time, so
# it measures the response path AND the per-request allocation path together;
# this route isolates the response path alone. Comparing the two across a worker
# ladder is what separates "the socket path doesn't scale" from "per-request
# large allocations don't scale".
_PRERENDERED: dict[int, bytes] = {}


@app.get("/jsoncached")
async def json_cached(request):
    n = int(request.query("n", "0") or "0")
    body = _PRERENDERED.get(n)
    if body is None:
        body = fast_json_dumps({"data": "x" * n})
        _PRERENDERED[n] = body
    return Response(body=body, content_type="application/json")


@app.get("/metrics")
async def metrics(request):
    # Serves the native Zig server's Prometheus registry (in-flight gauge,
    # response counts, pool waiters) so the worker sweep can scrape contention
    # signals around each load window. Not on the request hot path.
    # collect_prometheus_text returns bytes; decode for the text response.
    from hyperdjango.telemetry.metrics import collect_prometheus_text

    return Response.text(collect_prometheus_text().decode("utf-8", "replace"))
