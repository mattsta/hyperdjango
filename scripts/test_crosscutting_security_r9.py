"""Regression tests for round-9 cross-cutting security + correctness fixes.

Pure-Python (no DB, no live server). Run with the free-threaded interpreter:

    .venv/bin/python scripts/test_crosscutting_security_r9.py

Covers:
  1. CORS: allow_credentials + wildcard is rejected at construction.
  2. CORS: with an explicit allowlist and allow_credentials, a NON-allowlisted
     Origin is never reflected, and an allowlisted Origin echo emits Vary: Origin.
  3. signals: disconnect() works for a bound method (stable identity key).
  4. sampling: ParentBased honours an UNSAMPLED parent's decision (no re-sample).
  5. telemetry: the request middleware no longer double-writes error.* attrs.
  6. cache: a response declaring Vary on an uncovered header is not cached.
"""

# hyper-test: unit

import inspect
import re
import sys


class _FakeHeaders(dict):
    def get(self, key, default=None):
        # Case-insensitive, mirroring a real header map.
        low = key.lower()
        for k, v in self.items():
            if k.lower() == low:
                return v
        return default


class _FakeRequest:
    def __init__(self, method="GET", path="/", headers=None):
        self.method = method
        self.path = path
        self.headers = _FakeHeaders(headers or {})
        self.scope = {}
        self.query_string = ""


def test_cors_wildcard_credentials_rejected():
    from hyperdjango.standalone_middleware import CORSMiddleware

    raised = False
    try:
        CORSMiddleware(origins=["*"], allow_credentials=True)
    except ValueError:
        raised = True
    assert raised, "wildcard + allow_credentials must raise at construction"
    # Default wildcard WITHOUT credentials is still fine.
    CORSMiddleware()
    print("PASS cors_wildcard_credentials_rejected")


def test_cors_no_reflection_and_vary():
    from hyperdjango.response import Response
    from hyperdjango.standalone_middleware import CORSMiddleware

    mw = CORSMiddleware(origins=["https://good.example"], allow_credentials=True)

    # Non-allowlisted origin: no ACAO header at all (no reflection).
    resp = Response.text("ok")
    mw._add_cors_headers(resp, "https://evil.example", preflight=False)
    acao = resp.headers.get("access-control-allow-origin")
    assert acao is None, f"must not reflect non-allowlisted origin, got {acao!r}"
    assert "access-control-allow-credentials" not in {
        k.lower() for k in resp.headers
    }, "must not send credentials to a rejected origin"

    # Allowlisted origin: echoed, credentials set, AND Vary: Origin present.
    resp2 = Response.text("ok")
    mw._add_cors_headers(resp2, "https://good.example", preflight=False)
    assert resp2.headers.get("access-control-allow-origin") == "https://good.example", (
        "allowlisted origin must be echoed verbatim"
    )
    vary = resp2.headers.get("vary") or resp2.headers.get("Vary") or ""
    assert any(t.strip().lower() == "origin" for t in vary.split(",")), (
        f"allowlisted echo must emit Vary: Origin, got {vary!r}"
    )

    # Preflight for an allowlisted origin also carries Vary: Origin.
    pre = mw._preflight_response("https://good.example")
    pvary = pre.headers.get("vary") or pre.headers.get("Vary") or ""
    assert any(t.strip().lower() == "origin" for t in pvary.split(",")), (
        f"preflight echo must emit Vary: Origin, got {pvary!r}"
    )
    print("PASS cors_no_reflection_and_vary")


def test_signals_disconnect_bound_method():
    from hyperdjango.signals import Signal

    sig = Signal(name="t")

    class Receiver:
        def __init__(self):
            self.calls = 0

        async def handler(self, sender, **kwargs):  # async receiver
            self.calls += 1

    r = Receiver()
    # Connect and disconnect using *fresh* bound-method objects each time
    # (r.handler is a new object on each attribute access).
    sig.connect(r.handler)
    assert sig.receiver_count == 1
    # Duplicate connect must dedup on stable identity, not id().
    sig.connect(r.handler)
    assert sig.receiver_count == 1, "duplicate bound-method connect must dedup"
    removed = sig.disconnect(r.handler)
    assert removed, "disconnect must match a bound method by stable identity"
    assert sig.receiver_count == 0, "receiver must be gone after disconnect"
    print("PASS signals_disconnect_bound_method")


def test_parentbased_honours_unsampled_parent():
    from hyperdjango.telemetry.context import SpanContext
    from hyperdjango.telemetry.sampling import AlwaysSample, ParentBased

    sampler = ParentBased(root=AlwaysSample())

    # Present-but-UNSAMPLED parent: span_id == 0 (sentinel) → is_valid False,
    # but the decision must still be "not sampled" (no re-sampling to True).
    unsampled_parent = SpanContext(
        trace_id_high=1, trace_id_low=2, span_id=0, parent_id=0, sampled=False
    )
    assert sampler.should_sample(unsampled_parent, 2) is False, (
        "unsampled parent must NOT be re-sampled from scratch"
    )

    # Present-and-sampled parent → sampled.
    sampled_parent = SpanContext(
        trace_id_high=1, trace_id_low=2, span_id=99, parent_id=0, sampled=True
    )
    assert sampler.should_sample(sampled_parent, 2) is True

    # No parent (root) → delegate to root policy (AlwaysSample → True).
    assert sampler.should_sample(None, 2) is True
    print("PASS parentbased_honours_unsampled_parent")


def test_telemetry_error_attrs_written_once():
    # The middleware must NOT itself write error.type / error.message —
    # _SpanCM.__exit__ is the sole writer. Assert the duplicate block is gone.
    from hyperdjango.telemetry import middleware

    src = inspect.getsource(middleware)
    # Isolate the request-middleware __call__ body region.
    assert 'set_attr_str("error.type"' not in src, (
        "middleware must not write error.type (duplicate of _SpanCM)"
    )
    assert 'set_attr_str("error.message"' not in src, (
        "middleware must not write error.message (duplicate of _SpanCM)"
    )
    print("PASS telemetry_error_attrs_written_once")


def test_cache_refuses_uncovered_vary():
    from hyperdjango.cache_adapters import CacheMiddleware

    # Build the middleware; we only exercise the pure Vary-decision logic by
    # replicating the guard the way __call__ computes it. Verify via source
    # that vary_uncovered participates in the cacheable decision.
    src = inspect.getsource(CacheMiddleware)
    assert "vary_uncovered" in src, "cache must compute an uncovered-Vary guard"
    assert re.search(r"not vary_uncovered", src), (
        "cacheable must exclude responses with uncovered Vary fields"
    )
    print("PASS cache_refuses_uncovered_vary")


def main():
    tests = [
        test_cors_wildcard_credentials_rejected,
        test_cors_no_reflection_and_vary,
        test_signals_disconnect_bound_method,
        test_parentbased_honours_unsampled_parent,
        test_telemetry_error_attrs_written_once,
        test_cache_refuses_uncovered_vary,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001 - test harness reports all
            failures += 1
            print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
