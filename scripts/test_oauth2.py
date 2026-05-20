#!/usr/bin/env python3
"""Test OAuth2 authorization code flow.

Tests:
1. Provider preset construction (Google, GitHub, Auth0)
2. State generation + verification + expiry + CSRF
3. Authorization URL building
4. User data extraction from provider profiles
5. OAuth2 middleware routing
6. require_oauth2 decorator
7. TestClient login_oauth2 helper
8. Token storage + retrieval + expiry
"""

# hyper-test: unit

import sys
import time

from hyperdjango.app import HyperApp
from hyperdjango.auth.oauth2 import (
    OAuth2,
    OAuth2Tokens,
    auth0,
    build_authorize_url,
    extract_user_data,
    generate_state,
    github,
    google,
    require_oauth2,
    verify_state,
)
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.testing import TestClient


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    SECRET = "test-secret-key-for-oauth2"

    # ── Provider presets ──────────────────────────────────────────────────
    print("\n=== Provider presets ===")

    g = google("gid", "gsecret")
    check("google name", g.name == "google")
    check("google client_id", g.client_id == "gid")
    check("google authorize_url", "accounts.google.com" in g.authorize_url)
    check("google token_url", "googleapis.com" in g.token_url)
    check(
        "google userinfo_url",
        "googleapis.com" in g.userinfo_url or "googleusercontent" in g.userinfo_url,
    )
    check("google scopes", "openid" in g.scopes and "email" in g.scopes)
    check("google id_field", g.id_field == "sub")

    gh = github("ghid", "ghsecret")
    check("github name", gh.name == "github")
    check("github authorize_url", "github.com" in gh.authorize_url)
    check("github token_url", "github.com" in gh.token_url)
    check("github id_field", gh.id_field == "id")
    check("github name_field", gh.name_field == "login")

    a0 = auth0("myapp.auth0.com", "aid", "asecret")
    check("auth0 name", a0.name == "auth0")
    check("auth0 authorize_url", "myapp.auth0.com" in a0.authorize_url)
    check("auth0 token_url", "myapp.auth0.com" in a0.token_url)

    g_custom = google(
        "gid", "gsecret", scopes=["openid", "email", "profile", "calendar"]
    )
    check("google custom scopes", "calendar" in g_custom.scopes)

    # ── State parameter CSRF ──────────────────────────────────────────────
    print("\n=== State parameter ===")

    state = generate_state(SECRET, "google")
    check("state not empty", len(state) > 0)
    check("state contains dot (HMAC signed)", "." in state)

    provider = verify_state(state, SECRET)
    check("state verifies", provider == "google", f"got {provider}")

    check("state wrong secret", verify_state(state, "wrong-secret") is None)
    check("state tampered", verify_state(state + "x", SECRET) is None)
    check("state empty", verify_state("", SECRET) is None)
    check("state no dot", verify_state("nodot", SECRET) is None)

    # Expiry
    expired_state = generate_state(SECRET, "github")
    # Verify with max_age=0 (immediately expired)
    check("state expired", verify_state(expired_state, SECRET, max_age=0) is None)
    # Verify with generous max_age
    check(
        "state not expired", verify_state(expired_state, SECRET, max_age=60) == "github"
    )

    # ── Authorization URL ─────────────────────────────────────────────────
    print("\n=== Authorization URL ===")

    url = build_authorize_url(g, "https://myapp.com/auth/google/callback", state)
    check("url starts with authorize_url", url.startswith(g.authorize_url))
    check("url has client_id", "client_id=gid" in url)
    check("url has redirect_uri", "redirect_uri=" in url)
    check("url has response_type=code", "response_type=code" in url)
    check("url has state", "state=" in url)
    check("url has scope", "scope=" in url)

    # ── User data extraction ──────────────────────────────────────────────
    print("\n=== User data extraction ===")

    # ws27 item 6: OIDC email is trusted only when email_verified is True.
    google_profile = {
        "sub": "12345",
        "email": "alice@gmail.com",
        "email_verified": True,
        "name": "Alice",
        "picture": "https://pic.jpg",
    }
    user = extract_user_data(g, google_profile)
    check("google user id", user["id"] == "12345")
    check("google user email", user["email"] == "alice@gmail.com")
    check("google user email_verified", user["email_verified"] is True)
    check("google user name", user["name"] == "Alice")
    check("google user avatar", user["avatar"] == "https://pic.jpg")
    check("google user provider", user["oauth2_provider"] == "google")
    check("google raw_profile", user["raw_profile"] == google_profile)

    # An UNVERIFIED OIDC email must NOT be trusted (account-takeover defense).
    unverified = extract_user_data(
        g, {"sub": "9", "email": "attacker@victim.com", "email_verified": False}
    )
    check("google unverified email dropped", unverified["email"] == "")
    check("google unverified flagged", unverified["email_verified"] is False)

    # GitHub: trust an address only via a verified primary in /user/emails.
    github_profile = {
        "id": 67890,
        "login": "alice",
        "email": "alice@github.com",
        "avatar_url": "https://ghpic.jpg",
        "emails": [
            {"email": "alice@github.com", "primary": True, "verified": True},
        ],
    }
    user_gh = extract_user_data(gh, github_profile)
    check("github user id", user_gh["id"] == "67890")
    check("github user name", user_gh["name"] == "alice")
    check("github verified email", user_gh["email"] == "alice@github.com")

    # GitHub with no verified emails list → email not trusted.
    gh_unverified = extract_user_data(
        gh, {"id": 1, "login": "eve", "email": "eve@evil.com"}
    )
    check("github unverified email dropped", gh_unverified["email"] == "")

    # ── Token storage ─────────────────────────────────────────────────────
    print("\n=== Token storage ===")

    tokens = OAuth2Tokens(
        access_token="at_123",
        refresh_token="rt_456",
        expires_at=time.time() + 3600,
        provider="google",
        scopes=["openid"],
    )
    check("token not expired", not tokens.expired)

    expired_tokens = OAuth2Tokens(
        access_token="at_old",
        refresh_token=None,
        expires_at=time.time() - 100,
        provider="github",
        scopes=[],
    )
    check("token expired", expired_tokens.expired)

    oauth = OAuth2(secret=SECRET)
    oauth.store_tokens("sess1", tokens)
    check("store + get tokens", oauth.get_tokens("sess1") is not None)
    check("get tokens access", oauth.get_tokens("sess1").access_token == "at_123")
    check("get tokens missing", oauth.get_tokens("nonexistent") is None)

    oauth.clear_tokens("sess1")
    check("clear tokens", oauth.get_tokens("sess1") is None)

    # ── OAuth2 middleware routing ──────────────────────────────────────────
    print("\n=== OAuth2 middleware ===")

    app = HyperApp()
    sa = SessionAuth(secret=SECRET)
    app.use(sa)
    oauth_mw = OAuth2(secret=SECRET)
    oauth_mw.add_provider(g)
    oauth_mw.add_provider(gh)
    oauth_mw.set_session_auth(sa)
    app.use(oauth_mw)

    @app.get("/")
    async def index(request):
        user = request.user
        if user is None:
            return {"user": None}
        return {
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "oauth2_provider": request.oauth2_provider,
            }
        }

    @app.get("/protected")
    @require_oauth2()
    async def protected(request):
        user = request.user
        return {
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "oauth2_provider": request.oauth2_provider,
            }
        }

    @app.get("/google-only")
    @require_oauth2("google")
    async def google_only(request):
        return {"provider": request.oauth2_provider}

    client = TestClient(app)

    # Login route should return redirect
    resp = client.get("/auth/google/login")
    check(
        "login redirects", resp.status in (301, 302, 307, 308), f"status={resp.status}"
    )

    # Protected route without auth returns 401
    resp = client.get("/protected")
    check("protected unauthorized", resp.status == 401)

    # ── TestClient login_oauth2 ───────────────────────────────────────────
    print("\n=== TestClient login_oauth2 ===")

    client2 = TestClient(app)
    client2.login_oauth2(
        "google", {"id": "123", "email": "alice@gmail.com", "name": "Alice"}
    )

    resp = client2.get("/protected")
    check("protected after login", resp.status == 200, f"status={resp.status}")

    data = resp.json()
    check("user data present", data.get("user") is not None)
    check("user has provider", data["user"].get("oauth2_provider") == "google")
    check("user has email", data["user"].get("email") == "alice@gmail.com")

    # Google-only route with Google auth
    resp = client2.get("/google-only")
    check("google-only with google", resp.status == 200)

    # Google-only route with GitHub auth
    client3 = TestClient(app)
    client3.login_oauth2("github", {"id": "456", "login": "bob"})
    resp = client3.get("/google-only")
    check("google-only with github", resp.status == 403)

    # ── require_oauth2 decorator ──────────────────────────────────────────
    print("\n=== require_oauth2 decorator ===")

    resp = client.get("/protected")
    check("no auth → 401", resp.status == 401)

    # Session auth without oauth2_provider should also fail
    app2 = HyperApp()
    sa2 = SessionAuth(secret=SECRET)
    app2.use(sa2)
    oauth_mw2 = OAuth2(secret=SECRET)
    oauth_mw2.set_session_auth(sa2)
    app2.use(oauth_mw2)

    @app2.get("/needs-oauth")
    @require_oauth2()
    async def needs_oauth(request):
        return {"ok": True}

    client4 = TestClient(app2)
    # Login with session but no oauth2_provider
    from hyperdjango.response import Response as Resp

    resp_obj = Resp.empty()
    sa2.login(resp_obj, {"username": "alice"})  # no oauth2_provider
    # Extract cookie manually
    for key, val in resp_obj.headers.items():
        if key.lower() == "set-cookie":
            parts = val.split(";")[0].split("=", 1)
            if len(parts) == 2:
                client4._cookies[parts[0]] = parts[1]

    resp = client4.get("/needs-oauth")
    check("session without oauth → 401", resp.status == 401)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All OAuth2 tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
