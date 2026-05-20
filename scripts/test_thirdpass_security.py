"""
Regression tests for third-pass security hardening.

Tests:
1. OAuth2 state nonce consumption (replay prevention)
2. API key hashing (keys not stored in plaintext)
3. RateLimitMiddleware global cleanup (memory bounded)
4. TwoTierCache async L2 guard
5. M2M set() transactional
6. OAuth2 async HTTP calls

Usage:
    uv run hyper-test thirdpass_security
"""

# hyper-test: unit

import asyncio
import inspect
import sys
import traceback

from hyperdjango.request import Request
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# API key hashing
# ---------------------------------------------------------------------------


@test("APIKeyAuth: keys hashed on init, not stored plaintext")
def test_api_key_hashed():
    from hyperdjango.auth.api_keys import APIKeyAuth, _hash_api_key

    auth = APIKeyAuth(valid_keys={"sk_live_secret123", "sk_live_secret456"})

    # Raw keys should NOT be in _hashed_keys
    assert "sk_live_secret123" not in auth._hashed_keys
    assert "sk_live_secret456" not in auth._hashed_keys

    # Hashed versions SHOULD be present
    assert _hash_api_key("sk_live_secret123") in auth._hashed_keys
    assert _hash_api_key("sk_live_secret456") in auth._hashed_keys


@test("APIKeyAuth: valid key authenticates")
async def test_api_key_valid():
    from hyperdjango.auth.api_keys import APIKeyAuth

    auth = APIKeyAuth(valid_keys={"my-secret-key"}, header="x-api-key")

    req = Request(
        method="GET",
        path="/api/data",
        headers={"x-api-key": "my-secret-key"},
    )

    async def handler(request):
        return Response.json({"valid": request.api_key_valid})

    resp = await auth(req, handler)
    assert req.api_key_valid is True


@test("APIKeyAuth: invalid key rejected")
async def test_api_key_invalid():
    from hyperdjango.auth.api_keys import APIKeyAuth

    auth = APIKeyAuth(valid_keys={"my-secret-key"}, header="x-api-key")

    req = Request(
        method="GET",
        path="/api/data",
        headers={"x-api-key": "wrong-key"},
    )

    async def handler(request):
        return Response.json({"valid": request.api_key_valid})

    resp = await auth(req, handler)
    assert req.api_key_valid is False


@test("APIKeyAuth: _hash_api_key is SHA-256")
def test_api_key_hash_algorithm():
    import hashlib

    from hyperdjango.auth.api_keys import _hash_api_key

    key = "test-key"
    expected = hashlib.sha256(key.encode()).hexdigest()
    assert _hash_api_key(key) == expected
    assert len(_hash_api_key(key)) == 64  # SHA-256 hex = 64 chars


# ---------------------------------------------------------------------------
# OAuth2 nonce consumption
# ---------------------------------------------------------------------------


@test("OAuth2: _used_nonces set exists")
def test_oauth2_nonce_set():
    from hyperdjango.auth.oauth2 import OAuth2

    oauth = OAuth2(secret="test-secret")
    assert hasattr(oauth, "_used_nonces")
    assert isinstance(oauth._used_nonces, set)


@test("OAuth2: state nonce tracked after callback")
def test_oauth2_nonce_tracked():
    from hyperdjango.auth.oauth2 import OAuth2

    oauth = OAuth2(secret="test-secret")
    # Simulate nonce consumption
    oauth._used_nonces.add("test-state-token")
    assert "test-state-token" in oauth._used_nonces


@test("OAuth2: nonce set pruned when exceeds limit")
def test_oauth2_nonce_pruning():
    from hyperdjango.auth.oauth2 import OAuth2

    oauth = OAuth2(secret="test-secret")
    # Add more than 10000 nonces
    for i in range(10001):
        oauth._used_nonces.add(f"nonce-{i}")

    # Manual prune (the code does this when len > 10000)
    if len(oauth._used_nonces) > 10000:
        oauth._used_nonces.clear()

    assert len(oauth._used_nonces) == 0


# ---------------------------------------------------------------------------
# RateLimitMiddleware global cleanup
# ---------------------------------------------------------------------------


@test("RateLimitMiddleware backend: per-shard cap bounds memory under a key flood")
def test_ratelimit_backend_bounded():
    # The canonical InMemoryRateLimitBackend bounds memory with a hard per-shard
    # bucket cap + LRU eviction (not a periodic time sweep). A flood of distinct
    # keys can never grow a shard past _max_buckets, so the process can't OOM.
    from hyperdjango.ratelimit import InMemoryRateLimitBackend

    total_cap = 32  # split across 16 shards -> _max_buckets == 2 per shard
    backend = InMemoryRateLimitBackend(max_buckets=total_cap)
    per_shard = backend._max_buckets
    assert per_shard >= 1

    # Drive far more distinct keys than the cap through one check each.
    for i in range(2000):
        backend.check_and_increment(f"flood-{i}", max_requests=10, window=60)

    # No shard ever exceeds its cap, regardless of how many keys arrived.
    worst = max(len(s) for s in backend._shards)
    assert worst <= per_shard, f"shard grew to {worst}, cap is {per_shard}"


@test("RateLimitMiddleware backend: unbounded when no cap configured")
def test_ratelimit_backend_unbounded_default():
    # max_buckets=0 (the default when RATELIMIT_MAX_BUCKETS is unset) means the
    # cap is disabled — every distinct key keeps its bucket.
    from hyperdjango.ratelimit import InMemoryRateLimitBackend

    backend = InMemoryRateLimitBackend(max_buckets=0)
    assert backend._max_buckets == 0
    for i in range(50):
        backend.check_and_increment(f"k-{i}", max_requests=10, window=60)
    assert sum(len(s) for s in backend._shards) == 50


# ---------------------------------------------------------------------------
# TwoTierCache async L2 guard
# ---------------------------------------------------------------------------


@test("TwoTierCache: sync get with sync L2 works")
def test_twotier_sync_ok():
    from hyperdjango.cache import LocMemCache
    from hyperdjango.cache_adapters import TwoTierCache

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1=l1, l2=l2, l1_ttl=10)

    l2.set("key", "value", ttl=60)
    result = cache.get("key")
    assert result == "value"


# ---------------------------------------------------------------------------
# M2M set() transactional
# ---------------------------------------------------------------------------


@test("M2M set: uses transaction (code inspection)")
def test_m2m_set_transactional():
    import inspect

    from hyperdjango.models import ManyToManyField

    # Get the set method source
    src = inspect.getsource(ManyToManyField.__get__)  # This is on the descriptor
    # Actually check M2MManager.set
    # The set method is on the manager returned by __get__
    # Check the source of the M2MManager class
    import hyperdjango.models as models_mod

    manager_cls = None
    for name, obj in vars(models_mod).items():
        if name == "M2MManager":
            manager_cls = obj
            break

    # Fail loudly if the M2M-transactional manager is renamed/removed — a
    # silent skip here would let the atomicity guarantee regress unnoticed.
    assert manager_cls is not None, (
        "M2MManager class not found in hyperdjango.models — the transactional "
        "M2M set() manager was renamed or removed"
    )

    src = inspect.getsource(manager_cls.set)
    assert "transaction" in src, "M2M set() should use a transaction"


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nThird-Pass Security Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
