"""Minimal app fixture for raw-HTTP server tests (no database).

Provides a POST route so server-level request parsing — Content-Length
handling, header parsing, body reading — can be exercised end to end against
the live native server. Not a showcase app.
"""

from hyperdjango import HyperApp, Response

app = HyperApp(title="raw-http-fixture")


@app.post("/echo")
async def echo(request) -> Response:
    return Response.text("ok")


app.mount_health()
