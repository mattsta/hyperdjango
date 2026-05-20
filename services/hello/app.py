"""
Example: Hello World with HyperDjango.

The simplest possible app — two routes, no dependencies.
"""

import sys

from hyperdjango import HyperApp, Response

app = HyperApp(title="Hello")


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    return Response.json({"detail": "Internal server error"}, status=500)


@app.get("/")
async def index(request):
    return {"message": "Hello from HyperDjango!"}


@app.get("/greet/{name}")
async def greet(request, name):
    return {"greeting": f"Hello, {name}!"}


app.mount_health()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    app.run(port=port)
