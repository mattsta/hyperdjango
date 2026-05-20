"""
Hypothesis fuzz tests for URL router.

Proves: register(pattern) → resolve(matching_url) returns correct handler.
Uses real Router from hyperdjango.router.

# hyper-test: unit
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hyperdjango.router import Router

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

path_segments = st.text(
    min_size=1, max_size=15, alphabet="abcdefghijklmnop0123456789_-"
)


# ---------------------------------------------------------------------------
# Property 1: Static routes resolve correctly
# ---------------------------------------------------------------------------


@given(segments=st.lists(path_segments, min_size=1, max_size=5))
@settings(max_examples=300, deadline=1000)
def test_static_route_resolves(segments):
    """Register a static path → resolve returns the handler."""
    path = "/" + "/".join(segments)
    handler = lambda req: None
    router = Router()
    router.add("GET", path, handler)
    match = router.resolve("GET", path)
    assert match is not None, f"Failed to resolve: {path}"
    route, params = match
    assert route is not None and route.handler is handler


@given(
    seg1=st.lists(path_segments, min_size=1, max_size=3),
    seg2=st.lists(path_segments, min_size=1, max_size=3),
)
@settings(max_examples=300, deadline=1000)
def test_different_paths_dont_collide(seg1, seg2):
    """Two different static paths → resolve to their own handlers."""
    path1 = "/" + "/".join(seg1)
    path2 = "/" + "/".join(seg2)
    assume(path1 != path2)

    h1 = lambda req: "one"
    h2 = lambda req: "two"
    router = Router()
    router.add("GET", path1, h1)
    router.add("GET", path2, h2)

    r1, _ = router.resolve("GET", path1)
    r2, _ = router.resolve("GET", path2)
    assert r1 is not None and r1.handler is h1
    assert r2 is not None and r2.handler is h2


# ---------------------------------------------------------------------------
# Property 2: Non-matching path → None
# ---------------------------------------------------------------------------


@given(
    registered=st.lists(path_segments, min_size=1, max_size=3),
    query=st.lists(path_segments, min_size=1, max_size=3),
)
@settings(max_examples=300, deadline=1000)
def test_unregistered_path_returns_none(registered, query):
    """Unregistered path → resolve returns None."""
    reg_path = "/" + "/".join(registered)
    query_path = "/" + "/".join(query)
    assume(reg_path != query_path)

    router = Router()
    router.add("GET", reg_path, lambda r: None)
    route, _ = router.resolve("GET", query_path)
    assert route is None, f"Unexpected match for {query_path} (registered: {reg_path})"


# ---------------------------------------------------------------------------
# Property 3: Param routes extract values
# ---------------------------------------------------------------------------


@given(
    name=path_segments,
    value=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop0123456789"),
)
@settings(max_examples=300, deadline=1000)
def test_param_route_extracts_value(name, value):
    """/{name}/{param} → params dict contains the value."""
    pattern = f"/{name}/{{id}}"
    url = f"/{name}/{value}"

    router = Router()
    router.add("GET", pattern, lambda r: None)
    route, params = router.resolve("GET", url)
    assert route is not None, f"Failed to resolve: {url} for pattern {pattern}"
    assert params.get("id") == value, (
        f"Param mismatch: expected {value!r}, got {params}"
    )


# ---------------------------------------------------------------------------
# Property 4: Many routes don't interfere
# ---------------------------------------------------------------------------


@given(names=st.lists(path_segments, min_size=2, max_size=20, unique=True))
@settings(max_examples=100, deadline=2000)
def test_many_routes_no_collision(names):
    """Register N routes → each resolves to its own handler."""
    router = Router()
    handlers = {}
    for name in names:
        path = f"/{name}"
        h = lambda r, n=name: n
        router.add("GET", path, h)
        handlers[path] = h

    for path, expected_handler in handlers.items():
        route, _ = router.resolve("GET", path)
        assert route is not None, f"Failed to resolve: {path}"
        assert route.handler is expected_handler


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── URL Router Hypothesis Fuzz Tests ──\n")

    tests = [
        ("static route resolves", test_static_route_resolves),
        ("different paths no collision", test_different_paths_dont_collide),
        ("unregistered → None", test_unregistered_path_returns_none),
        ("param extraction", test_param_route_extracts_value),
        ("many routes no collision", test_many_routes_no_collision),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Router fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
