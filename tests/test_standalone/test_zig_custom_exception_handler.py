"""Regression: custom @app.exception_handler must fire on the Zig dispatch path.

The ASGI path resolved exceptions via app._resolve_exception (which walks
registered custom handlers), but the Zig enhanced-dispatch wrapper normalized
exceptions via a module function that ignored them — so a handler registered
with @app.exception_handler worked under the test client and returned a generic
500 in production under the native server. These tests pin that the wrapper,
when given the app's resolver, routes exceptions through the custom handlers.

Run: uv run pytest tests/test_standalone/test_zig_custom_exception_handler.py -q
"""

from hyperdjango.app import HyperApp
from hyperdjango.response import Response


def _drive(wrapped):
    return wrapped(
        method="GET", path="/", headers={}, query_string="", body=b"", path_params={}
    )


def test_custom_handler_fires_on_zig_path():
    app = HyperApp()

    @app.exception_handler(ValueError)
    async def handle_value_error(request, exc):
        return Response.json({"handled": str(exc)}, status=422)

    async def handler(request):
        raise ValueError("boom")

    # The registration path wires app._resolve_exception into the wrapper.
    wrapped = app._wrap_handler_for_zig(handler, None, app._resolve_exception)
    status, ct, body, extra = _drive(wrapped)

    assert status == 422, f"custom handler not applied on Zig path: {status}"
    assert b"boom" in body


def test_http_exception_still_normalized_on_zig_path():
    from hyperdjango.exceptions import HTTPException

    app = HyperApp()

    async def handler(request):
        raise HTTPException(403, "nope")

    wrapped = app._wrap_handler_for_zig(handler, None, app._resolve_exception)
    status, ct, body, extra = _drive(wrapped)
    assert status == 403
    assert b"nope" in body


def test_generic_500_body_is_detail_shaped():
    # Non-debug generic 500 uses the same {"detail": ...} shape as the ASGI path
    # (test_full_integration asserts this), so the two paths agree.
    app = HyperApp()

    async def handler(request):
        raise RuntimeError("kaboom")

    wrapped = app._wrap_handler_for_zig(handler, None, app._resolve_exception)
    status, ct, body, extra = _drive(wrapped)
    assert status == 500
    assert b'"detail"' in body


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
