"""
Example: Benchmark App with HyperDjango.

Minimal routes designed for load testing with wrk, ab, or hey.

Usage:
    python app.py

Then benchmark with:
    wrk -t4 -c100 -d10s http://localhost:8000/json
    wrk -t4 -c100 -d10s http://localhost:8000/plaintext
    wrk -t4 -c100 -d10s http://localhost:8000/users/42
    ab -n 10000 -c 100 http://localhost:8000/json
"""

import sys

from hyperdjango import HyperApp, Response

app = HyperApp(title="Benchmark")


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    return Response.json({"detail": "Internal server error"}, status=500)


@app.get("/json")
async def json_endpoint(request):
    """Return a JSON object. Standard TechEmpower-style benchmark route."""
    return {"message": "Hello, World!"}


@app.get("/plaintext")
async def plaintext(request):
    """Return plain text. Tests raw throughput without JSON serialization."""
    return Response(body=b"Hello, World!", content_type="text/plain")


@app.get("/users/{id:int}")
async def get_user(request, id):
    """Path-param route. Tests router matching performance."""
    return {"id": id, "name": f"User {id}", "active": True}


app.mount_health()


@app.get("/echo")
async def echo(request):
    """Echo query params back. Tests query string parsing."""
    return dict(request.query_params)


@app.post("/body")
async def body_echo(request):
    """Echo POST body back. Tests request body parsing throughput."""
    data = await request.json()
    return data


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    app.run(port=port)
