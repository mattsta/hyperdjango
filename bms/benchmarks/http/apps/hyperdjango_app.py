"""hyperdjango benchmark app (native server). See apps/__init__.py.

The connection model (threaded vs reactor) is selected at run time via
HYPER_HTTP_SERVER_MODEL, so the SAME app binary benchmarks both.
"""

from hyperdjango import HyperApp, Response

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


@app.get("/metrics")
async def metrics(request):
    # Serves the native Zig server's Prometheus registry (in-flight gauge,
    # response counts, pool waiters) so the worker sweep can scrape contention
    # signals around each load window. Not on the request hot path.
    # collect_prometheus_text returns bytes; decode for the text response.
    from hyperdjango.telemetry.metrics import collect_prometheus_text

    return Response.text(collect_prometheus_text().decode("utf-8", "replace"))
