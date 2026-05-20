"""Tests for standalone middleware."""

from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    MiddlewareStack,
    TimingMiddleware,
)


class TestMiddlewareStack:
    async def test_empty_stack(self):
        stack = MiddlewareStack()

        async def handler(request):
            return Response.text("ok")

        wrapped = stack.wrap(handler)
        resp = await wrapped(Request())
        assert resp.body == b"ok"

    async def test_single_middleware(self):
        stack = MiddlewareStack()

        async def add_header(request, call_next):
            resp = await call_next(request)
            resp.headers["x-test"] = "yes"
            return resp

        stack.add(add_header)

        async def handler(request):
            return Response.text("ok")

        wrapped = stack.wrap(handler)
        resp = await wrapped(Request())
        assert resp.headers["x-test"] == "yes"

    async def test_middleware_ordering(self):
        stack = MiddlewareStack()
        order = []

        async def first(request, call_next):
            order.append("first_in")
            resp = await call_next(request)
            order.append("first_out")
            return resp

        async def second(request, call_next):
            order.append("second_in")
            resp = await call_next(request)
            order.append("second_out")
            return resp

        stack.add(first)
        stack.add(second)

        async def handler(request):
            order.append("handler")
            return Response.text("ok")

        wrapped = stack.wrap(handler)
        await wrapped(Request())
        assert order == ["first_in", "second_in", "handler", "second_out", "first_out"]


class TestCORSMiddleware:
    async def test_cors_headers(self):
        cors = CORSMiddleware(origins=["*"])

        async def handler(request):
            return Response.text("ok")

        req = Request(headers={"origin": "http://example.com"})
        resp = await cors(req, handler)
        assert "access-control-allow-origin" in resp.headers

    async def test_cors_preflight(self):
        cors = CORSMiddleware(origins=["*"])

        async def handler(request):
            return Response.text("ok")

        req = Request(method="OPTIONS", headers={"origin": "http://example.com"})
        resp = await cors(req, handler)
        assert resp.status == 204

    def test_cors_credentials_wildcard_is_rejected(self):
        """Wildcard origins + credentials is an account-takeover config and must
        be REFUSED at construction (previously it reflected any Origin with
        Access-Control-Allow-Credentials: true). Credentialed responses may only
        be exposed to an explicit origin allowlist."""
        import pytest

        with pytest.raises(ValueError, match="allow_credentials"):
            CORSMiddleware(origins=["*"], allow_credentials=True)

        # An explicit allowlist + credentials is fine and echoes only allowed origins.
        cors = CORSMiddleware(origins=["https://example.com"], allow_credentials=True)
        from hyperdjango.response import Response

        resp = Response.empty()
        cors._add_cors_headers(resp, "https://example.com")
        assert resp.headers["access-control-allow-origin"] == "https://example.com"
        assert resp.headers["access-control-allow-credentials"] == "true"
        # A non-allowlisted origin is NOT reflected.
        resp2 = Response.empty()
        cors._add_cors_headers(resp2, "https://evil.example")
        assert (
            resp2.headers.get("access-control-allow-origin") != "https://evil.example"
        )


class TestTimingMiddleware:
    async def test_adds_timing_header(self):
        timing = TimingMiddleware()

        async def handler(request):
            return Response.text("ok")

        resp = await timing(Request(), handler)
        assert "x-response-time" in resp.headers
