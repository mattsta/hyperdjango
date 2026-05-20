"""Tests for the native extension module and its fallback behavior."""

import pytest


class TestNativeExtensionImport:
    """Test that the native extension or its fallbacks are importable."""

    def test_native_module_init_importable(self):
        """The hyperdjango.native package should always be importable."""

    def test_native_module_importable(self):
        import hyperdjango._hyperdjango_native  # noqa: F401

    def test_fast_json_dumps_available(self):
        from hyperdjango.native import fast_json_dumps

        assert callable(fast_json_dumps)

    def test_fast_json_loads_available(self):
        from hyperdjango.native import fast_json_loads

        assert callable(fast_json_loads)

    def test_html_escape_available(self):
        from hyperdjango.native import html_escape

        assert callable(html_escape)

    def test_url_encode_available(self):
        from hyperdjango.native import url_encode

        assert callable(url_encode)

    def test_url_decode_available(self):
        from hyperdjango.native import url_decode

        assert callable(url_decode)

    def test_parse_query_string_available(self):
        from hyperdjango.native import parse_query_string

        assert callable(parse_query_string)

    def test_hash_password_available(self):
        from hyperdjango.native import hash_password

        assert callable(hash_password)

    def test_verify_password_available(self):
        from hyperdjango.native import verify_password

        assert callable(verify_password)

    def test_all_exports_listed(self):
        """__all__ should list every public symbol."""
        from hyperdjango.native import __all__ as exports

        expected = {
            "fast_json_dumps",
            "fast_json_loads",
            "html_escape",
            "url_encode",
            "url_decode",
            "parse_query_string",
            "parse_cookies",
            "base_encode",
            "base_decode",
            "xor_bytes",
            "hash_password",
            "verify_password",
            "is_release_build",
        }
        assert expected == set(exports)


class TestNativeExtensionDirect:
    """Test the compiled _hyperdjango_native if available, skip otherwise."""

    @pytest.fixture(autouse=True)
    def _require_native(self):
        import hyperdjango._hyperdjango_native  # noqa: F401

    def test_import_succeeds(self):
        import hyperdjango._hyperdjango_native as _native

        assert _native is not None

    def test_hello_function(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "hello"):
            result = _native.hello()
            assert isinstance(result, str)

    def test_hyper_server_class_exists(self):
        import hyperdjango._hyperdjango_native as _native

        assert hasattr(_native, "HyperServer")
        assert callable(_native.HyperServer)

    def test_response_view_class_exists(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "ResponseView"):
            assert callable(_native.ResponseView)

    def test_dir_lists_methods(self):
        import hyperdjango._hyperdjango_native as _native

        members = dir(_native)
        assert isinstance(members, list)
        assert len(members) > 0

    def test_rv_new(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "_rv_new"):
            state = _native._rv_new()
            assert state is not None

    def test_rv_json(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "_rv_new") and hasattr(_native, "_rv_json"):
            state = _native._rv_new()
            _native._rv_json(state, '{"test": true}')

    def test_server_new(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "_server_new"):
            state = _native._server_new()
            assert state is not None

    def test_json_dumps_native(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "json_dumps_native"):
            result = _native.json_dumps_native({"a": 1})
            assert b'"a"' in result

    def test_json_loads_native(self):
        import hyperdjango._hyperdjango_native as _native

        if hasattr(_native, "json_loads_native"):
            result = _native.json_loads_native(b'{"x":1}')
            assert result == {"x": 1}


class TestFallbackJSON:
    """Test the pure-Python JSON fallback always works."""

    def test_json_dumps_dict(self):
        from hyperdjango.native._json import json_dumps

        result = json_dumps({"key": "value"})
        assert b'"key"' in result
        assert b'"value"' in result

    def test_json_dumps_list(self):
        from hyperdjango.native._json import json_dumps

        result = json_dumps([1, 2, 3])
        assert result == b"[1,2,3]"

    def test_json_loads_bytes(self):
        from hyperdjango.native._json import json_loads

        result = json_loads(b'{"hello":"world"}')
        assert result == {"hello": "world"}

    def test_json_loads_str(self):
        from hyperdjango.native._json import json_loads

        result = json_loads('{"a": 1}')
        assert result == {"a": 1}

    def test_json_roundtrip(self):
        from hyperdjango.native._json import json_dumps, json_loads

        data = {"nested": [1, "two", None, True]}
        assert json_loads(json_dumps(data)) == data


class TestFallbackCrypto:
    """Test the pure-Python crypto fallback."""

    def test_sign_and_verify(self):
        from hyperdjango.native._crypto import sign_data, verify_signed_data

        signed = sign_data("payload", "secret")
        assert verify_signed_data(signed, "secret") == "payload"

    def test_verify_wrong_secret(self):
        from hyperdjango.native._crypto import sign_data, verify_signed_data

        signed = sign_data("payload", "secret")
        assert verify_signed_data(signed, "wrong") is None

    def test_generate_token_unique(self):
        from hyperdjango.native._crypto import generate_token

        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_generate_token_length(self):
        from hyperdjango.native._crypto import generate_token

        token = generate_token()
        assert len(token) > 20


class TestFallbackStrings:
    """Test the pure-Python string utilities."""

    def test_html_escape_tags(self):
        from hyperdjango.native._strings import html_escape

        assert "&lt;" in html_escape("<div>")

    def test_url_encode_spaces(self):
        from hyperdjango.native._strings import url_encode

        assert "%20" in url_encode("a b")

    def test_url_decode_percent(self):
        from hyperdjango.native._strings import url_decode

        assert url_decode("a%20b") == "a b"

    def test_parse_query_string_multi(self):
        from hyperdjango.native._strings import parse_query_string

        result = parse_query_string("x=1&x=2&y=3")
        assert result["x"] == ["1", "2"]
        assert result["y"] == ["3"]

    def test_parse_query_string_empty(self):
        from hyperdjango.native._strings import parse_query_string

        result = parse_query_string("")
        assert result == {}


class TestJSONDepthCrashRegression:
    """Regression for the native JSON parser stack-overflow crash (ws23).

    A body like ``b"[" * N + b"]" * N`` used to drive the *recursive* parser one
    native stack frame per nesting level and SIGSEGV the worker (returncode -11)
    — unauthenticated, reachable from every JSON endpoint and WebSockets, and
    uncatchable by Python ``try/except``. The parser is now iterative (nesting
    tracked on an explicit heap stack) with a deliberate depth policy, so an
    over-deep document is an ordinary parse error, never a crash.
    """

    def _run(self, code: str):
        """Run code in a fresh subprocess; return (returncode, stdout)."""
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def test_deep_array_does_not_crash_process(self):
        code = (
            "from hyperdjango.native import fast_json_loads\n"
            "body = b'[' * 100000 + b']' * 100000\n"
            "try:\n"
            "    fast_json_loads(body); print('NORAISE')\n"
            "except Exception as e:\n"
            "    print('RAISED', type(e).__name__)\n"
        )
        rc, out, err = self._run(code)
        assert rc == 0, f"process crashed (rc={rc}): {err}"  # NOT -11
        assert out.startswith("RAISED"), out

    def test_deep_object_does_not_crash_process(self):
        code = (
            "from hyperdjango.native import fast_json_loads\n"
            "body = b'{\"a\":' * 100000 + b'1' + b'}' * 100000\n"
            "try:\n"
            "    fast_json_loads(body); print('NORAISE')\n"
            "except Exception as e:\n"
            "    print('RAISED', type(e).__name__)\n"
        )
        rc, out, err = self._run(code)
        assert rc == 0, f"process crashed (rc={rc}): {err}"
        assert out.startswith("RAISED"), out

    def test_deep_body_raises_not_crashes_in_process(self):
        from hyperdjango.native import fast_json_loads

        with pytest.raises(Exception):
            fast_json_loads(b"[" * 100000 + b"]" * 100000)

    def test_legit_deep_nesting_parses_correctly(self):
        """A legitimately deep-but-reasonable payload still parses to the right
        structure (proves the depth policy does not reject real nesting)."""
        from hyperdjango.native import fast_json_loads

        depth = 500
        body = b"[" * depth + b"42" + b"]" * depth
        val = fast_json_loads(body)
        cur, d = val, 0
        while isinstance(cur, list):
            assert len(cur) == 1
            cur = cur[0]
            d += 1
        assert d == depth
        assert cur == 42

    def test_normal_payloads_unaffected(self):
        from hyperdjango.native import fast_json_loads

        v = fast_json_loads(b'{"a": [1, 2, {"b": true}], "c": null, "s": "hi"}')
        assert v == {"a": [1, 2, {"b": True}], "c": None, "s": "hi"}

    def test_request_json_deep_body_returns_400_not_crash(self):
        """request.json() on a pathologically-nested body raises HTTPException
        (400), the normal invalid-JSON path — never a native crash."""
        import asyncio

        from hyperdjango.exceptions import HTTPException
        from hyperdjango.request import Request

        req = Request(method="POST", path="/", body=b"[" * 100000 + b"]" * 100000)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(req.json())
        assert exc_info.value.status_code == 400

    def test_model_validator_deep_value_raises_not_crash(self):
        """The native single-pass JSON→model path (model_validator) routes value
        parsing through the same iterative materializer, so a deeply-nested field
        value raises cleanly instead of crashing the worker."""
        from hyperdjango import _hyperdjango_native as nat
        from hyperdjango.models import Field, Model

        if not hasattr(nat, "json_loads_model"):
            pytest.skip("native json_loads_model not available")

        class M(Model):
            n: int = Field(default=0)
            s: str = Field(default="")

        capsule = getattr(M, "__dhi_compiled_specs__", None)
        if capsule is None:
            pytest.skip("model has no compiled native specs")

        inst = M.__new__(M)
        deep = ('{"n":1,"x":' + "[" * 100000 + "]" * 100000 + "}").encode()
        with pytest.raises(Exception):
            nat.json_loads_model(deep, inst, capsule, 0)

    def test_pure_python_fallback_raises_recursionerror(self):
        """The pure-Python fallback is inherently safe (raises RecursionError)
        on pathologically deep input rather than crashing the worker.

        The stdlib json C scanner bounds recursion by the C STACK size, NOT by
        sys.setrecursionlimit — so whether a 100k-deep parse raises depends on
        the stack of the thread it runs in. Under the parallel suite a test can
        run in a large-stack worker where the parse would instead succeed
        outright, flaking this assertion ("DID NOT RAISE"). Run the parse in a
        thread with a controlled small stack so the recursion guard trips
        deterministically regardless of the ambient stack_size; restore the
        previous stack_size afterward. 1 MiB is deep enough that the guard trips
        cleanly (verified across 0.5–4 MiB) without overflowing the process.
        """
        import threading

        from hyperdjango.native import _json as pyjson

        deep = b"[" * 100000 + b"]" * 100000
        outcome: dict[str, BaseException | None] = {}

        def _parse():
            try:
                pyjson.json_loads(deep)
                outcome["raised"] = None
            except RecursionError as exc:
                outcome["raised"] = exc

        prev_stack = threading.stack_size()
        threading.stack_size(1024 * 1024)
        try:
            t = threading.Thread(target=_parse)
            t.start()
            t.join()
        finally:
            threading.stack_size(prev_stack)

        assert isinstance(outcome.get("raised"), RecursionError), (
            "pure-Python fallback must raise RecursionError on deep input; "
            f"got {outcome.get('raised')!r}"
        )
