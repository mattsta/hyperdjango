"""
Regression tests for OAuth2 PKCE support (RFC 7636).

Tests:
1. generate_pkce() produces valid verifier + challenge
2. build_authorize_url() includes code_challenge when provided
3. exchange_code() includes code_verifier when provided
4. OAuth2 login redirect sets PKCE cookie

Usage:
    uv run hyper-test oauth2_pkce
"""

# hyper-test: unit

import asyncio
import base64
import hashlib
import inspect
import sys
import traceback
import urllib.parse

from hyperdjango.auth.oauth2 import (
    OAuth2,
    OAuth2Provider,
    build_authorize_url,
    exchange_code,
    generate_pkce,
)

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
# PKCE generation
# ---------------------------------------------------------------------------


@test("generate_pkce: returns verifier and challenge")
def test_pkce_returns_pair():
    verifier, challenge = generate_pkce()
    assert isinstance(verifier, str)
    assert isinstance(challenge, str)
    assert len(verifier) > 42  # RFC 7636 minimum 43 chars
    assert len(challenge) > 0


@test("generate_pkce: challenge is SHA256 of verifier (base64url)")
def test_pkce_challenge_correct():
    verifier, challenge = generate_pkce()

    # Recompute: base64url(sha256(verifier))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    assert challenge == expected, f"Challenge mismatch: {challenge} != {expected}"


@test("generate_pkce: each call produces unique verifier")
def test_pkce_unique():
    v1, _ = generate_pkce()
    v2, _ = generate_pkce()
    assert v1 != v2


@test("generate_pkce: verifier is URL-safe")
def test_pkce_url_safe():
    verifier, _ = generate_pkce()
    # URL-safe base64 chars: A-Z, a-z, 0-9, -, _
    for c in verifier:
        assert c.isalnum() or c in "-_", f"Non-URL-safe char in verifier: {c!r}"


# ---------------------------------------------------------------------------
# build_authorize_url with PKCE
# ---------------------------------------------------------------------------


@test("build_authorize_url: includes code_challenge when provided")
def test_authorize_url_pkce():
    provider = OAuth2Provider(
        name="test",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        userinfo_url="https://auth.example.com/userinfo",
        scopes=["openid", "email"],
    )
    _, challenge = generate_pkce()

    url = build_authorize_url(
        provider,
        "https://app.example.com/callback",
        "state123",
        code_challenge=challenge,
    )

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)

    assert "code_challenge" in params
    assert params["code_challenge"][0] == challenge
    assert params["code_challenge_method"][0] == "S256"


@test("build_authorize_url: omits PKCE when code_challenge is None")
def test_authorize_url_no_pkce():
    provider = OAuth2Provider(
        name="test",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        userinfo_url="https://auth.example.com/userinfo",
        scopes=["openid", "email"],
    )

    url = build_authorize_url(provider, "https://app.example.com/callback", "state123")

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)

    assert "code_challenge" not in params
    assert "code_challenge_method" not in params


# ---------------------------------------------------------------------------
# exchange_code with PKCE
# ---------------------------------------------------------------------------


@test("exchange_code: includes code_verifier in params (code inspection)")
def test_exchange_code_pkce_param():
    src = inspect.getsource(exchange_code)
    assert "code_verifier" in src


# ---------------------------------------------------------------------------
# OAuth2 middleware PKCE integration
# ---------------------------------------------------------------------------


@test("OAuth2 login redirect: includes PKCE cookie")
def test_oauth2_login_pkce():
    src = inspect.getsource(OAuth2._handle_login)
    assert "generate_pkce" in src
    assert "hyper_pkce_verifier" in src
    assert "code_challenge" in src


@test("OAuth2 callback: reads PKCE verifier from cookie")
def test_oauth2_callback_pkce():
    src = inspect.getsource(OAuth2._handle_callback)
    assert "hyper_pkce_verifier" in src
    assert "code_verifier" in src


@test("OAuth2 callback: FAIL-CLOSED when the PKCE verifier cookie is absent")
async def test_oauth2_callback_requires_verifier():
    """A callback carrying a valid signed+unused state and a code but NO
    verifier cookie (login started in a different browser — the login-CSRF /
    code-injection shape) must be rejected BEFORE the token exchange, not
    forwarded with code_verifier=None for the provider to (maybe) reject."""
    from hyperdjango.auth.oauth2 import generate_state
    from hyperdjango.exceptions import HTTPException

    SECRET = "s" * 40
    provider = OAuth2Provider(
        name="github",
        client_id="id",
        client_secret="cs",
        authorize_url="https://x/authorize",
        token_url="https://x/token",
        userinfo_url="https://x/user",
        scopes=["read"],
    )
    oauth = OAuth2(secret=SECRET)
    oauth.add_provider(provider)

    class _Req:
        is_secure = True
        headers = {"host": "app.example"}
        _q = {"state": generate_state(SECRET, "github"), "code": "abc"}

        def query(self, key, default=None):
            return self._q.get(key, default)

        cookies: dict = {}  # ← no hyper_pkce_verifier

    _Req.headers = type(
        "_H",
        (),
        {"get": staticmethod(lambda k, d=None: {"host": "app.example"}.get(k, d))},
    )()
    _Req.cookies = type("_C", (), {"get": staticmethod(lambda k, d=None: None)})()

    raised = None
    try:
        await oauth._handle_callback(_Req(), provider)
    except HTTPException as e:
        raised = e
    assert raised is not None and raised.status_code == 400, (
        f"expected 400, got {raised}"
    )
    assert "verifier" in str(raised.detail).lower(), (
        f"unexpected detail: {raised.detail}"
    )


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nOAuth2 PKCE Regression Tests ({len(tests)} tests)")
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
