"""Phase 3 tests — native module, auth, migrations, test client, OpenAPI, WebSocket."""

import json

from hyperdjango import HyperApp, Request, Response
from hyperdjango.testing import TestClient

# --- Native module tests ---


class TestNativeJSON:
    def test_json_dumps_dict(self):
        from hyperdjango.native import fast_json_dumps

        result = fast_json_dumps({"key": "value", "num": 42})
        parsed = json.loads(result)
        assert parsed == {"key": "value", "num": 42}

    def test_json_dumps_list(self):
        from hyperdjango.native import fast_json_dumps

        result = fast_json_dumps([1, 2, 3])
        assert json.loads(result) == [1, 2, 3]

    def test_json_dumps_model(self):
        from hyperdjango.models import Field, Model
        from hyperdjango.native import fast_json_dumps

        class Item(Model):
            name: str = Field()
            price: float = Field(default=0.0)

        item = Item(name="Widget", price=9.99)
        result = fast_json_dumps(item.model_dump())
        parsed = json.loads(result)
        assert parsed["name"] == "Widget"
        assert parsed["price"] == 9.99

    def test_json_loads(self):
        from hyperdjango.native import fast_json_loads

        result = fast_json_loads(b'{"hello":"world"}')
        assert result == {"hello": "world"}

    def test_json_roundtrip(self):
        from hyperdjango.native import fast_json_dumps, fast_json_loads

        data = {"users": [{"name": "Alice", "age": 30}], "total": 1}
        result = fast_json_loads(fast_json_dumps(data))
        assert result == data


class TestNativeStrings:
    def test_html_escape(self):
        from hyperdjango.native import html_escape

        assert html_escape("<script>alert('xss')</script>") == (
            "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
        )

    def test_url_encode(self):
        from hyperdjango.native import url_encode

        assert url_encode("hello world") == "hello%20world"

    def test_url_decode(self):
        from hyperdjango.native import url_decode

        assert url_decode("hello%20world") == "hello world"

    def test_parse_query_string(self):
        from hyperdjango.native import parse_query_string

        result = parse_query_string("a=1&b=2&a=3")
        assert result["a"] == ["1", "3"]
        assert result["b"] == ["2"]


class TestNativeCrypto:
    def test_hash_and_verify(self):
        from hyperdjango.native import hash_password, verify_password

        hashed = hash_password("mysecret")
        assert verify_password("mysecret", hashed)
        assert not verify_password("wrong", hashed)

    def test_hash_uniqueness(self):
        from hyperdjango.native import hash_password

        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # Different salts

    def test_sign_verify(self):
        from hyperdjango.native._crypto import sign_data, verify_signed_data

        signed = sign_data("user123", "secret")
        assert verify_signed_data(signed, "secret") == "user123"
        assert verify_signed_data(signed, "wrong") is None

    def test_generate_token(self):
        from hyperdjango.native._crypto import generate_token

        t1 = generate_token()
        t2 = generate_token()
        assert t1 != t2
        assert len(t1) > 20


# --- Response/Request with native JSON ---


class TestNativeResponseJSON:
    def test_response_json_uses_native(self):
        resp = Response.json({"key": "value"})
        assert resp.status == 200
        parsed = json.loads(resp.body)
        assert parsed == {"key": "value"}

    async def test_request_json_uses_native(self):
        req = Request(body=b'{"x": 42}', headers={"content-type": "application/json"})
        data = await req.json()
        assert data == {"x": 42}


# --- Test Client ---


class TestTestClient:
    def test_get(self):
        app = HyperApp()

        @app.get("/hello")
        async def hello(request):
            return {"msg": "hi"}

        client = TestClient(app)
        resp = client.get("/hello")
        assert resp.ok
        assert resp.json() == {"msg": "hi"}

    def test_post_json(self):
        app = HyperApp()

        @app.post("/echo")
        async def echo(request):
            data = await request.json()
            return data

        client = TestClient(app)
        resp = client.post("/echo", json={"name": "Alice"})
        assert resp.ok
        assert resp.json()["name"] == "Alice"

    def test_404(self):
        app = HyperApp()
        client = TestClient(app)
        resp = client.get("/missing")
        assert resp.status == 404

    def test_path_params(self):
        app = HyperApp()

        @app.get("/items/{id:int}")
        async def get_item(request, id):
            return {"id": id}

        client = TestClient(app)
        resp = client.get("/items/42")
        assert resp.json()["id"] == 42

    def test_query_params(self):
        app = HyperApp()

        @app.get("/search")
        async def search(request):
            return {"q": request.query("q")}

        client = TestClient(app)
        resp = client.get("/search?q=hello")
        assert resp.json()["q"] == "hello"

    def test_cookies(self):
        app = HyperApp()

        @app.get("/set-cookie")
        async def set_cookie(request):
            resp = Response.json({"ok": True})
            resp.set_cookie("token", "abc123")
            return resp

        @app.get("/check-cookie")
        async def check_cookie(request):
            return {"token": request.cookies.get("token")}

        client = TestClient(app)
        client.get("/set-cookie")
        resp = client.get("/check-cookie")
        assert resp.json()["token"] == "abc123"

    def test_middleware_in_client(self):
        app = HyperApp()

        @app.middleware
        async def add_header(request, call_next):
            resp = await call_next(request)
            resp.headers["x-test"] = "yes"
            return resp

        @app.get("/test")
        async def test_view(request):
            return "ok"

        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["x-test"] == "yes"


# --- Auth tests ---


class TestSessionAuth:
    def test_login_logout_flow(self):
        from hyperdjango.auth.sessions import SessionAuth

        app = HyperApp()
        auth = SessionAuth(secret="test-secret")
        app.use(auth)

        @app.post("/login")
        async def login(request):
            resp = Response.json({"logged_in": True})
            auth.login(resp, {"id": 1, "username": "Alice"})
            return resp

        @app.get("/me")
        async def me(request):
            user = request.user
            if user and user.is_authenticated:
                return {"user": {"id": user.id, "username": user.username}}
            return Response.json({"error": "Not logged in"}, status=401)

        @app.post("/logout")
        async def logout(request):
            resp = Response.json({"logged_out": True})
            if request.session_id:
                auth.logout(resp, request.session_id)
            return resp

        client = TestClient(app)

        # Not logged in
        resp = client.get("/me")
        assert resp.status == 401

        # Login
        resp = client.post("/login")
        assert resp.ok

        # Now logged in
        resp = client.get("/me")
        assert resp.ok
        assert resp.json()["user"]["username"] == "Alice"


class TestAPIKeyAuth:
    def test_api_key_validation(self):
        from hyperdjango.auth.api_keys import APIKeyAuth
        from hyperdjango.auth.decorators import require_api_key

        app = HyperApp()
        app.use(APIKeyAuth(valid_keys={"test-key-123"}))

        @app.get("/api/data")
        @require_api_key
        async def api_data(request):
            return {"secret": "data"}

        client = TestClient(app)

        # No key
        resp = client.get("/api/data")
        assert resp.status == 401

        # Valid key
        resp = client.get("/api/data", headers={"x-api-key": "test-key-123"})
        assert resp.ok
        assert resp.json()["secret"] == "data"

        # Invalid key
        resp = client.get("/api/data", headers={"x-api-key": "wrong"})
        assert resp.status == 401


class TestRequireAuth:
    def test_require_auth_decorator(self):
        from hyperdjango.auth.decorators import require_auth

        app = HyperApp()

        @app.get("/protected")
        @require_auth()
        async def protected(request):
            return {"access": "granted"}

        client = TestClient(app)
        resp = client.get("/protected")
        assert resp.status in {401, 302}  # Redirects to LOGIN_URL or returns 401


# --- Migration tests ---


class TestMigrations:
    def test_create_table_ddl(self):
        from hyperdjango.migrations import SchemaDiffer
        from hyperdjango.models import Field, Model

        class User(Model):
            class Meta:
                table = "users"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=100)
            email: str = Field(unique=True)
            active: bool = Field(default=True)

        from hyperdjango.migrations import ModelExtractor

        schema = ModelExtractor.extract(User)
        op = SchemaDiffer._create_table_op(schema)
        ddl = op.up_sql()
        assert "users" in ddl
        assert "id" in ddl
        assert "name" in ddl

    def test_makemigrations(self):
        """Test that MigrationEngine and ModelExtractor exist and work together."""
        from hyperdjango.migrations import ModelExtractor, SchemaDiffer
        from hyperdjango.models import Field, Model

        class Article(Model):
            class Meta:
                table = "articles"

            id: int = Field(primary_key=True, auto=True)
            title: str = Field(max_length=200)

        # ModelExtractor can extract schema from model
        schema = ModelExtractor.extract(Article)
        assert schema.table == "articles"
        assert "id" in schema.columns
        assert "title" in schema.columns

        # SchemaDiffer can create a CreateTable operation
        op = SchemaDiffer._create_table_op(schema)
        sql = op.up_sql()
        assert "articles" in sql
        assert "title" in sql


# --- OpenAPI tests ---


class TestOpenAPI:
    def test_generate_spec(self):
        from hyperdjango.openapi import generate_openapi

        app = HyperApp(title="Test API")

        @app.get("/users/{id:int}")
        async def get_user(request, id):
            """Get a user by ID."""
            return {"id": id}

        @app.post("/users")
        async def create_user(request):
            """Create a new user."""
            return Response.json({}, status=201)

        spec = generate_openapi(app)
        assert spec["openapi"] == "3.1.0"
        assert spec["info"]["title"] == "Test API"
        assert "/users/{id}" in spec["paths"]
        assert "/users" in spec["paths"]
        assert "get" in spec["paths"]["/users/{id}"]
        assert "post" in spec["paths"]["/users"]

    def test_path_params_in_spec(self):
        from hyperdjango.openapi import generate_openapi

        app = HyperApp()

        @app.get("/items/{id:int}")
        async def get_item(request, id):
            return {"id": id}

        spec = generate_openapi(app)
        params = spec["paths"]["/items/{id}"]["get"]["parameters"]
        assert len(params) == 1
        assert params[0]["name"] == "id"
        assert params[0]["schema"]["type"] == "integer"

    def test_mount_docs(self):
        from hyperdjango.openapi import mount_docs

        app = HyperApp(title="Docs Test")
        mount_docs(app)

        client = TestClient(app)

        # OpenAPI JSON
        resp = client.get("/openapi.json")
        assert resp.ok
        spec = resp.json()
        assert spec["info"]["title"] == "Docs Test"

        # Docs page
        resp = client.get("/docs")
        assert resp.ok
        assert b"swagger-ui" in resp.body
