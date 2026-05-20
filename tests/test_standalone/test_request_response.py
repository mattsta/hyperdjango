"""Tests for standalone Request and Response objects."""

import json

from hyperdjango.request import CaseInsensitiveDict, Request
from hyperdjango.response import Response


class TestRequest:
    def test_basic_request(self):
        req = Request(method="GET", path="/users")
        assert req.method == "GET"
        assert req.path == "/users"
        assert req.body == b""

    def test_method_uppercase(self):
        req = Request(method="post", path="/")
        assert req.method == "POST"

    def test_query_params(self):
        req = Request(path="/search", query_string="q=hello&page=2")
        assert req.query("q") == "hello"
        assert req.query("page") == "2"
        assert req.query("missing") is None
        assert req.query("missing", "default") == "default"

    def test_path_params(self):
        req = Request(path="/users/42", path_params={"id": 42})
        assert req.path_params["id"] == 42

    async def test_json_body(self):
        data = {"name": "Alice", "age": 25}
        req = Request(
            method="POST",
            path="/users",
            body=json.dumps(data).encode(),
            headers={"content-type": "application/json"},
        )
        result = await req.json()
        assert result == data

    def test_cookies(self):
        req = Request(headers={"cookie": "session=abc123; theme=dark"})
        assert req.cookies["session"] == "abc123"
        assert req.cookies["theme"] == "dark"

    def test_is_json(self):
        req = Request(headers={"content-type": "application/json"})
        assert req.is_json

    def test_repr(self):
        req = Request(method="GET", path="/hello")
        assert repr(req) == "Request(GET /hello)"

    def test_from_asgi(self):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/data",
            "query_string": b"key=val",
            "headers": [
                (b"content-type", b"application/json"),
            ],
        }
        req = Request.from_asgi(scope, body=b'{"x": 1}')
        assert req.method == "POST"
        assert req.path == "/api/data"
        assert req.query("key") == "val"


class TestCaseInsensitiveDict:
    def test_case_insensitive_get(self):
        d = CaseInsensitiveDict({"Content-Type": "text/html"})
        assert d["content-type"] == "text/html"
        assert d["CONTENT-TYPE"] == "text/html"

    def test_case_insensitive_contains(self):
        d = CaseInsensitiveDict({"Authorization": "Bearer xxx"})
        assert "authorization" in d
        assert "AUTHORIZATION" in d


class TestResponse:
    def test_json_response(self):
        resp = Response.json({"hello": "world"})
        assert resp.status == 200
        assert b"hello" in resp.body
        assert "application/json" in resp.headers["content-type"]

    def test_html_response(self):
        resp = Response.html("<h1>Hi</h1>")
        assert resp.status == 200
        assert resp.body == b"<h1>Hi</h1>"
        assert "text/html" in resp.headers["content-type"]

    def test_text_response(self):
        resp = Response.text("Hello")
        assert resp.body == b"Hello"
        assert "text/plain" in resp.headers["content-type"]

    def test_redirect(self):
        resp = Response.redirect("/login")
        assert resp.status == 302
        assert resp.headers["location"] == "/login"

    def test_empty(self):
        resp = Response.empty()
        assert resp.status == 204
        assert resp.body == b""

    def test_custom_status(self):
        resp = Response.json({"error": "not found"}, status=404)
        assert resp.status == 404

    def test_custom_headers(self):
        resp = Response.json({}, headers={"x-custom": "value"})
        assert resp.headers["x-custom"] == "value"

    def test_set_cookie(self):
        resp = Response.text("ok")
        resp.set_cookie("session", "abc123", max_age=3600)
        assert "session=abc123" in resp.headers["set-cookie"]
        assert "Max-Age=3600" in resp.headers["set-cookie"]

    def test_repr(self):
        resp = Response.json({})
        assert "200" in repr(resp)
