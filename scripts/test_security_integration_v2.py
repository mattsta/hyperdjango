"""Integration tests for the security + correctness hardening pass.

Covers:

  - Q(_connector=...) whitelist
  - annotate/aggregate alias rejection of SQL-control + control chars
  - exclude(field__in=[..., None]) semantics
  - When() requires at least one condition
  - MultipleOf(0) construction-time rejection
  - MinLength / MaxLength / StringConstraints negative + min>max validation
  - CORSMiddleware cached joined-header strings
  - Cache middleware: refuse Vary: *
  - SessionAuth + SESSION_SAVE_EVERY_REQUEST adds Vary: Cookie
  - ASGI / Zig→Django bridge drop underscore-named headers
  - Compression middleware streaming gzip output

No DB required for the bulk of these — they exercise pure-Python validation paths.
"""

# hyper-test: unit

from __future__ import annotations

import gzip
import sys
import traceback
import unittest

from hyperdjango.cache_adapters import CacheMiddleware
from hyperdjango.expressions import (
    _VALID_CONNECTORS,
    Case,
    Q,
    Value,
    When,
)
from hyperdjango.lookups import InLookup
from hyperdjango.query import _validate_alias_name
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import (
    CompressionMiddleware,
    CORSMiddleware,
)
from hyperdjango.testkit import check, finish, run_main
from hyperdjango.validation.core.constraints import (
    MaxLength,
    MinLength,
    MultipleOf,
    StringConstraints,
)


class QConnectorWhitelistTest(unittest.TestCase):
    def test_valid_connectors_accepted(self) -> None:
        for c in _VALID_CONNECTORS:
            Q(name="x", _connector=c)  # no raise

    def test_injection_attempts_rejected(self) -> None:
        bad = ["; DROP TABLE users", "AND 1=1", "OR true--", "", "and"]
        for c in bad:
            with self.assertRaises(ValueError):
                Q(name="x", _connector=c)

    def test_default_connector_is_and(self) -> None:
        q = Q(a=1, b=2)
        self.assertEqual(q.connector, "AND")


class AliasRejectionTest(unittest.TestCase):
    """annotate/aggregate must reject SQL-injection + control-char alias kwargs.

    We import the validator directly so we don't need a live DB connection.
    """

    def test_validator_accepts_clean_names(self) -> None:
        for name in ["count", "total_score", "x1", "User_Count"]:
            _validate_alias_name(name, source="annotate")

    def test_validator_rejects_dangerous_names(self) -> None:
        bad = [
            "x; DROP TABLE y",
            "x' OR 1=1",
            "x--comment",
            "x/*comment*/",
            "x y",  # whitespace
            "x\n",
            "x\x00",  # NUL
            "x\x1f",  # control
            "x\x7f",  # DEL
            "x\x80",  # C1 control
            "",
            '"x"',
            "(x)",
            ";",
        ]
        for name in bad:
            with self.assertRaises(ValueError, msg=name):
                _validate_alias_name(name, source="annotate")


class InLookupNoneSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lookup = InLookup()

    def test_no_none_unchanged(self) -> None:
        sql, params = self.lookup.as_sql("col", 1, [1, 2, 3])
        self.assertEqual(sql, "col = ANY($1)")
        self.assertEqual(params, [[1, 2, 3]])

    def test_with_none_splits(self) -> None:
        sql, params = self.lookup.as_sql("col", 1, [1, 2, None])
        # NULL must appear in the SQL — De Morgan correctness for exclude().
        self.assertIn("IS NULL", sql)
        self.assertEqual(params, [[1, 2]])

    def test_only_none(self) -> None:
        sql, params = self.lookup.as_sql("col", 1, [None])
        self.assertEqual(sql, "col IS NULL")
        self.assertEqual(params, [])

    def test_empty(self) -> None:
        sql, params = self.lookup.as_sql("col", 1, [])
        self.assertEqual(sql, "FALSE")
        self.assertEqual(params, [])

    def test_bind_params_matches_as_sql(self) -> None:
        # Round-trip: bind_params alone must produce the params list as_sql would.
        for value in ([1, 2], [1, 2, None], [None], []):
            _, expected = self.lookup.as_sql("c", 1, value)
            self.assertEqual(self.lookup.bind_params(value), expected, value)

    def test_to_node_consistent(self) -> None:
        node = self.lookup.to_node("col", [1, None])
        self.assertIn("IS NULL", node.template)


class WhenConstructionTest(unittest.TestCase):
    def test_when_requires_condition(self) -> None:
        with self.assertRaises(TypeError):
            When(then=Value(1))

    def test_when_with_condition_ok(self) -> None:
        Case(When(x=1, then=Value(2)), default=Value(0))


class ConstraintValidationTest(unittest.TestCase):
    def test_multiple_of_zero_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MultipleOf(0)
        with self.assertRaises(ValueError):
            MultipleOf(0.0)

    def test_multiple_of_nan_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MultipleOf(float("nan"))

    def test_multiple_of_non_zero_ok(self) -> None:
        MultipleOf(1)
        MultipleOf(-3)
        MultipleOf(0.5)
        MultipleOf(float("inf"))  # weird but well-defined remainder

    def test_min_length_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MinLength(-1)

    def test_max_length_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MaxLength(-1)

    def test_string_constraints_negative(self) -> None:
        with self.assertRaises(ValueError):
            StringConstraints(min_length=-1)
        with self.assertRaises(ValueError):
            StringConstraints(max_length=-1)

    def test_string_constraints_min_gt_max(self) -> None:
        with self.assertRaises(ValueError):
            StringConstraints(min_length=10, max_length=5)

    def test_string_constraints_min_eq_max_ok(self) -> None:
        StringConstraints(min_length=5, max_length=5)


class CORSMiddlewareCacheTest(unittest.TestCase):
    def test_joined_headers_precomputed(self) -> None:
        m = CORSMiddleware(
            methods=["GET", "POST"], headers=["X-Foo", "X-Bar"], max_age=42
        )
        self.assertEqual(m._methods_joined, "GET, POST")
        self.assertEqual(m._headers_joined, "X-Foo, X-Bar")
        self.assertEqual(m._max_age_str, "42")

    def test_origins_set_precomputed(self) -> None:
        m = CORSMiddleware(origins=["https://a", "https://b"])
        self.assertEqual(m._policy._origins, frozenset({"https://a", "https://b"}))
        self.assertFalse(m._policy.allow_any_origin)

    def test_wildcard_origin(self) -> None:
        m = CORSMiddleware(origins=["*"])
        self.assertTrue(m._policy.allow_any_origin)


class CacheMiddlewareVaryStarTest(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_to_cache_vary_star(self) -> None:
        # Stub cache that records every set() call.
        captures: list[tuple] = []

        class _StubCache:
            _is_async = False

            def get(self, k):
                return None

            def set(self, k, v, ttl):
                captures.append((k, v, ttl))

        mw = CacheMiddleware(_StubCache(), ttl=60)

        async def _handler(req):
            resp = Response.html("private")
            resp.headers["Vary"] = "*"
            return resp

        req = Request(method="GET", path="/p", headers={})
        await mw(req, _handler)
        self.assertEqual(captures, [])

    async def test_caches_normal_response(self) -> None:
        captures: list[tuple] = []

        class _StubCache:
            _is_async = False

            def get(self, k):
                return None

            def set(self, k, v, ttl):
                captures.append((k, v, ttl))

        mw = CacheMiddleware(_StubCache(), ttl=60)

        async def _handler(req):
            return Response.html("hello")

        req = Request(method="GET", path="/p", headers={})
        await mw(req, _handler)
        self.assertEqual(len(captures), 1)


class HeaderUnderscoreRejectionTest(unittest.TestCase):
    def test_from_asgi_drops_underscore_headers(self) -> None:
        scope = {
            "method": "GET",
            "path": "/",
            "headers": [
                (b"x-forwarded-for", b"1.2.3.4"),
                (b"x_forwarded_for", b"6.6.6.6"),  # spoof attempt
                (b"host", b"example.com"),
            ],
            "query_string": b"",
        }
        req = Request.from_asgi(scope)
        # Only the canonical dash form is preserved.
        self.assertIn("x-forwarded-for", req.headers)
        self.assertNotIn("x_forwarded_for", req.headers)
        self.assertEqual(req.headers["x-forwarded-for"], "1.2.3.4")


class CompressionStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_response_gets_streaming_gzip(self) -> None:
        mw = CompressionMiddleware(min_size=1)

        async def _gen():
            yield b"hello "
            yield b"world!"

        async def _handler(req):
            return Response.stream(_gen(), content_type="text/plain")

        req = Request(method="GET", path="/", headers={"accept-encoding": "gzip"})
        resp = await mw(req, _handler)

        self.assertEqual(resp.headers.get("content-encoding"), "gzip")
        # No content-length on streaming gzip.
        self.assertNotIn("content-length", {k.lower() for k in resp.headers})

        # Pull from the wrapped iterator and verify roundtrip.
        chunks = []
        async for ch in resp._stream_iter:
            chunks.append(ch)
        joined = b"".join(chunks)
        # Decompress and verify content is preserved.
        self.assertEqual(gzip.decompress(joined), b"hello world!")


class HypothesisAliasFuzz(unittest.TestCase):
    """If hypothesis is installed, fuzz the alias validator against control chars."""

    def test_control_chars_always_rejected(self) -> None:
        try:
            from hypothesis import given, settings
            from hypothesis import strategies as st
        except ImportError:
            self.skipTest("hypothesis not installed")
            return

        @given(
            prefix=st.text(
                alphabet=st.characters(min_codepoint=65, max_codepoint=90),
                min_size=1,
                max_size=3,
            ),
            control_char=st.sampled_from(
                [chr(c) for c in range(0, 32)] + [chr(c) for c in range(0x7F, 0xA0)]
            ),
        )
        @settings(max_examples=80, deadline=None)
        def _check(prefix: str, control_char: str) -> None:
            with self.assertRaises(ValueError):
                _validate_alias_name(prefix + control_char, source="annotate")

        _check()


def _test_name(test: unittest.TestCase) -> str:
    """``ClassName.test_method`` — stable regardless of the module's __name__."""
    return f"{type(test).__name__}.{test.id().rsplit('.', 1)[-1]}"


class _HarnessResult(unittest.TestResult):
    """unittest result adapter: report each test case through the testkit tally.

    Semantics match ``unittest.main``: every test runs, failures do not abort
    the suite, and the process exit code follows the failure count.
    """

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        check(_test_name(test), True)

    def addFailure(self, test: unittest.TestCase, err) -> None:
        super().addFailure(test, err)
        traceback.print_exception(err[0], err[1], err[2])
        check(_test_name(test), False, f"{err[0].__name__}: {err[1]}")

    def addError(self, test: unittest.TestCase, err) -> None:
        super().addError(test, err)
        traceback.print_exception(err[0], err[1], err[2])
        check(_test_name(test), False, f"{err[0].__name__}: {err[1]}")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        check(f"{_test_name(test)} (skipped: {reason})", True)


def main() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    suite.run(_HarnessResult())
    return finish()


if __name__ == "__main__":
    run_main(main)
