"""
OAuth2 Authorization Code Flow for HyperDjango.

Server-side only. No JWT. Tokens stored in sessions.

Usage:
    from hyperdjango.auth.oauth2 import OAuth2, google, github

    oauth = OAuth2(secret="your-secret")
    oauth.add_provider(google(client_id="...", client_secret="..."))
    oauth.add_provider(github(client_id="...", client_secret="..."))
    app.use(oauth)

    @app.get("/dashboard")
    @require_oauth2()
    async def dashboard(request):
        return {"user": request.user, "provider": request.oauth2_provider}
"""

import base64
import functools
import hashlib
import secrets
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field

import httpx

from hyperdjango.exceptions import HTTPException
from hyperdjango.native._crypto import (
    generate_token,
    sign_data,
    verify_signed_data,
)
from hyperdjango.response import Response


@dataclass(slots=True)
class OAuth2Provider:
    """Configuration for an OAuth2/OIDC provider."""

    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: list[str]
    discovery_url: str | None = None
    id_field: str = "sub"
    email_field: str = "email"
    name_field: str = "name"
    avatar_field: str = "picture"


def google(
    client_id: str, client_secret: str, scopes: list[str] | None = None
) -> OAuth2Provider:
    """Google OAuth2 provider preset."""
    return OAuth2Provider(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scopes=scopes or ["openid", "email", "profile"],
        discovery_url="https://accounts.google.com/.well-known/openid-configuration",
    )


def github(
    client_id: str, client_secret: str, scopes: list[str] | None = None
) -> OAuth2Provider:
    """GitHub OAuth2 provider preset."""
    return OAuth2Provider(
        name="github",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scopes=scopes or ["read:user", "user:email"],
        id_field="id",
        name_field="login",
        avatar_field="avatar_url",
    )


def auth0(
    domain: str, client_id: str, client_secret: str, scopes: list[str] | None = None
) -> OAuth2Provider:
    """Auth0 OAuth2 provider preset."""
    return OAuth2Provider(
        name="auth0",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=f"https://{domain}/authorize",
        token_url=f"https://{domain}/oauth/token",
        userinfo_url=f"https://{domain}/userinfo",
        scopes=scopes or ["openid", "email", "profile"],
        discovery_url=f"https://{domain}/.well-known/openid-configuration",
    )


@dataclass(slots=True)
class OAuth2Tokens:
    """Server-side token storage for a user session."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    provider: str
    scopes: list[str]

    @property
    def expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at


def generate_state(secret: str, provider_name: str) -> str:
    """Generate HMAC-signed state parameter for CSRF protection.

    Format: provider.timestamp.nonce.signature
    """
    nonce = generate_token(16)
    timestamp = str(int(time.time()))
    payload = f"{provider_name}.{timestamp}.{nonce}"
    return sign_data(payload, secret)


def verify_state(state: str, secret: str, max_age: int = 300) -> str | None:
    """Verify state parameter. Returns provider name or None if invalid/expired."""
    payload = verify_signed_data(state, secret)
    if payload is None:
        return None
    parts = payload.split(".")
    if len(parts) != 3:
        return None
    provider_name, timestamp_str, _nonce = parts
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return None
    if time.time() - timestamp > max_age:
        return None
    return provider_name


async def exchange_code(
    provider: OAuth2Provider,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
) -> dict[str, str]:
    """Exchange authorization code for tokens via HTTP POST to provider's token endpoint.

    Returns the token response dict with access_token, refresh_token, etc.
    Includes PKCE code_verifier if provided.
    """
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
    }
    if code_verifier:
        params["code_verifier"] = code_verifier

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            provider.token_url,
            data=params,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            return resp.json()
        # GitHub returns form-encoded by default
        return dict(urllib.parse.parse_qsl(resp.text))


async def fetch_userinfo(
    provider: OAuth2Provider, access_token: str
) -> dict[str, str | int | bool | None]:
    """Fetch user profile from provider's userinfo endpoint.

    Returns raw user profile dict.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        resp = await client.get(provider.userinfo_url, headers=headers)
        resp.raise_for_status()
        profile = resp.json()

        # GitHub's /user endpoint carries no per-email verified flag and may
        # return a null/hidden email. Fetch /user/emails so extract_user_data
        # can trust only a VERIFIED primary address (account-takeover defense).
        if provider.name == "github" and isinstance(profile, dict):
            try:
                emails_resp = await client.get(
                    "https://api.github.com/user/emails", headers=headers
                )
                emails_resp.raise_for_status()
                emails = emails_resp.json()
                if isinstance(emails, list):
                    profile["emails"] = emails
            except httpx.HTTPError:
                # No user:email scope or transient error — leave emails unset so
                # the email is treated as unverified (fail closed), never trusted.
                profile.pop("emails", None)

        return profile


def _resolve_verified_email(
    provider: OAuth2Provider, raw_profile: dict[str, str | int | bool | None]
) -> tuple[str, bool]:
    """Return ``(email, verified)`` for the profile, trusting only verified mail.

    Account-takeover defense: providers happily return an ``email`` claim the
    user never proved they control (an attacker sets any address on their IdP
    account). Auto-linking a local account by an *unverified* address lets the
    attacker take over that account. We therefore surface an email as trusted
    ONLY when the provider positively asserts it is verified:

    - OIDC (Google/Auth0/any provider returning the standard ``email_verified``
      claim): trust the ``email`` claim iff ``email_verified`` is truthy. The
      claim may arrive as a real bool or the string "true".
    - GitHub (``/user`` carries no per-email verified flag): trust an address
      only if a verified primary (or any verified) entry appears in an
      ``emails`` list — populated by ``fetch_userinfo`` from ``/user/emails``.

    Anything else is treated as UNVERIFIED and returns ``("", False)`` so callers
    cannot auto-link accounts by an address the user never proved they own.
    """
    ev = raw_profile.get("email_verified")
    if isinstance(ev, bool):
        claimed = raw_profile.get(provider.email_field) or ""
        return (str(claimed), True) if ev and claimed else ("", False)
    if isinstance(ev, str):
        verified = ev.strip().lower() in ("true", "1", "yes")
        claimed = raw_profile.get(provider.email_field) or ""
        return (str(claimed), True) if verified and claimed else ("", False)

    # No standard email_verified claim — look for a verified entry in an
    # emails list (GitHub /user/emails shape: {email, primary, verified}).
    emails = raw_profile.get("emails")
    if isinstance(emails, list):
        for entry in emails:
            if (
                isinstance(entry, dict)
                and entry.get("verified")
                and entry.get("primary")
                and entry.get("email")
            ):
                return str(entry["email"]), True
        for entry in emails:
            if isinstance(entry, dict) and entry.get("verified") and entry.get("email"):
                return str(entry["email"]), True

    return "", False


def extract_user_data(
    provider: OAuth2Provider, raw_profile: dict[str, str | int | bool | None]
) -> dict[str, str | bool | dict]:
    """Extract normalized user data from provider-specific profile.

    Returns dict with: id, email, email_verified, name, avatar, provider,
    raw_profile. ``email`` is only populated when the provider verified it;
    an unverified address yields ``email=""`` and ``email_verified=False`` so
    downstream account-linking logic cannot trust it.
    """
    email, email_verified = _resolve_verified_email(provider, raw_profile)
    return {
        "id": str(raw_profile.get(provider.id_field, "")),
        "email": email,
        "email_verified": email_verified,
        "name": raw_profile.get(provider.name_field, ""),
        "avatar": raw_profile.get(provider.avatar_field, ""),
        "oauth2_provider": provider.name,
        "raw_profile": raw_profile,
    }


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    RFC 7636: code_verifier is 43-128 chars of [A-Z, a-z, 0-9, -, ., _, ~].
    code_challenge = base64url(SHA256(code_verifier)).
    """
    code_verifier = secrets.token_urlsafe(64)[:96]  # 96 chars, URL-safe
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url(
    provider: OAuth2Provider,
    redirect_uri: str,
    state: str,
    code_challenge: str | None = None,
) -> str:
    """Build the authorization URL to redirect the user to.

    Includes PKCE code_challenge if provided (recommended for all flows).
    """
    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{provider.authorize_url}?{urllib.parse.urlencode(params)}"


@dataclass
class OAuth2:
    """OAuth2 middleware. Handles login redirect and callback routes.

    Usage:
        oauth = OAuth2(secret="your-secret")
        oauth.add_provider(google(client_id="...", client_secret="..."))
        app.use(oauth)
    """

    secret: str
    login_prefix: str = "/auth"
    state_max_age: int = 300
    providers: dict[str, OAuth2Provider] = field(default_factory=dict, init=False)
    _token_store: dict[str, OAuth2Tokens] = field(default_factory=dict, init=False)
    _session_auth: object | None = field(default=None, init=False)
    _used_nonces: set[str] = field(default_factory=set, init=False, repr=False)
    # Insertion-ordered record of consumed nonces, used for bounded FIFO
    # eviction so the size cap never drops still-in-flight (unexpired) nonces.
    _nonce_order: deque[str] = field(default_factory=deque, init=False, repr=False)
    # Guards the check-and-add of _used_nonces so concurrent callbacks with the
    # same state cannot both pass the replay check (TOCTOU). init=False keeps it
    # out of the generated __init__ / __eq__ / repr.
    _nonce_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    _max_nonces: int = field(default=10000, init=False, repr=False)

    def add_provider(self, provider: OAuth2Provider) -> OAuth2:
        """Register an OAuth2 provider."""
        self.providers[provider.name] = provider
        return self

    def set_session_auth(self, session_auth) -> OAuth2:
        """Set the SessionAuth instance to use for creating sessions."""
        self._session_auth = session_auth
        # Back-reference so SessionAuth.logout()/logout_async() can evict the
        # OAuth2 token store for the session being destroyed — otherwise
        # access+refresh tokens outlive the session they belong to.
        session_auth._oauth = self
        return self

    def get_tokens(self, session_id: str) -> OAuth2Tokens | None:
        """Get stored tokens for a session.

        Opportunistically evicts expired entries so the store can't grow
        unbounded (mirrors the bounded _used_nonces): a login stores an
        access+refresh pair, and without eviction every session that ever
        logged in would leak its tokens for the process lifetime.
        """
        tokens = self._token_store.get(session_id)
        if tokens is not None and tokens.expired:
            # Snapshot keys first (free-threaded 3.14t: don't mutate the dict
            # while iterating it), then drop every expired entry we can see.
            for sid in list(self._token_store.keys()):
                stale = self._token_store.get(sid)
                if stale is not None and stale.expired:
                    self._token_store.pop(sid, None)
            return None
        return tokens

    def store_tokens(self, session_id: str, tokens: OAuth2Tokens):
        """Store tokens for a session."""
        self._token_store[session_id] = tokens

    def clear_tokens(self, session_id: str):
        """Remove tokens for a session."""
        self._token_store.pop(session_id, None)

    async def __call__(self, request, call_next):
        """Middleware: handle OAuth2 login/callback routes, pass others through."""
        path = request.path

        # Check for login route: /auth/{provider}/login
        for provider_name, provider in self.providers.items():
            login_path = f"{self.login_prefix}/{provider_name}/login"
            callback_path = f"{self.login_prefix}/{provider_name}/callback"

            if path == login_path and request.method == "GET":
                return self._handle_login(request, provider)

            if path == callback_path and request.method == "GET":
                return await self._handle_callback(request, provider)

        # Set oauth2 context on request
        request.oauth2_provider = None
        if request.user is not None and request.user.is_authenticated:
            request.oauth2_provider = request.user.get("oauth2_provider")

        return await call_next(request)

    def _handle_login(self, request, provider: OAuth2Provider):
        """Generate state, redirect to provider's authorization page."""

        state = generate_state(self.secret, provider.name)

        # Build callback URL from request
        scheme = "https" if request.is_secure else "http"
        host = request.headers.get("host", "localhost")
        redirect_uri = f"{scheme}://{host}{self.login_prefix}/{provider.name}/callback"

        # Generate PKCE challenge
        code_verifier, code_challenge = generate_pkce()
        url = build_authorize_url(
            provider, redirect_uri, state, code_challenge=code_challenge
        )

        resp = Response.redirect(url)
        # Store code_verifier in a short-lived cookie for the callback. It is the
        # browser-binding that makes PKCE double as login-CSRF protection: the
        # callback (below) requires it, so an attacker's code + state replayed in
        # a victim's browser has no matching verifier and fails. Secure whenever
        # the flow is HTTPS; HttpOnly + Lax so it survives the provider's
        # top-level GET redirect back but is never readable by script.
        resp.set_cookie(
            "hyper_pkce_verifier",
            code_verifier,
            max_age=600,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
        return resp

    def _consume_nonce(self, state: str) -> None:
        """Atomically mark ``state`` as used, raising on replay.

        Replay defense: the check ("already used?") and the add must be a
        single atomic step. Without the lock, two concurrent callbacks carrying
        the SAME state both observe it absent and both proceed — the exact
        concurrent replay this guards against. Doing the check-and-add under one
        lock guarantees exactly one caller wins; every other caller (including a
        genuinely concurrent duplicate) raises.
        """
        with self._nonce_lock:
            if state in self._used_nonces:
                raise HTTPException(
                    400, "State parameter already used (replay detected)"
                )
            self._used_nonces.add(state)
            self._nonce_order.append(state)
            # Bounded FIFO eviction: drop the OLDEST nonces by insertion order.
            # The previous code did a blanket clear() at the cap, which wiped
            # every in-flight nonce at once and briefly reopened the replay
            # window. States are also time-limited (state_max_age), so the
            # oldest entries are the ones most likely already expired.
            while len(self._nonce_order) > self._max_nonces:
                oldest = self._nonce_order.popleft()
                self._used_nonces.discard(oldest)

    async def _handle_callback(self, request, provider: OAuth2Provider):
        """Handle OAuth2 callback: verify state, exchange code, create session."""

        # Verify state parameter
        state = request.query("state")
        if not state:
            raise HTTPException(400, "Missing state parameter")

        # Verify state and consume nonce (prevent replay)
        verified_provider = verify_state(state, self.secret, self.state_max_age)
        if verified_provider is None:
            raise HTTPException(400, "Invalid or expired state parameter")
        if verified_provider != provider.name:
            raise HTTPException(400, "State parameter provider mismatch")
        # Consume the state nonce (raises on replay). Atomic under one lock.
        self._consume_nonce(state)

        # Get authorization code
        code = request.query("code")
        if not code:
            error = request.query("error")
            error_desc = request.query("error_description", "Unknown error")
            raise HTTPException(400, f"OAuth2 error: {error} — {error_desc}")

        # Exchange code for tokens
        scheme = "https" if request.is_secure else "http"
        host = request.headers.get("host", "localhost")
        redirect_uri = f"{scheme}://{host}{self.login_prefix}/{provider.name}/callback"

        # Retrieve PKCE code_verifier from the cookie set at authorize. Require
        # it (fail closed): _handle_login always sets it, so its absence means
        # this callback did not originate from a flow started in THIS browser —
        # a login-CSRF / code-injection attempt. Rejecting here does not rely on
        # the provider to enforce PKCE.
        code_verifier = request.cookies.get("hyper_pkce_verifier")
        if not code_verifier:
            raise HTTPException(400, "Missing PKCE verifier (start login again)")

        token_response = await exchange_code(
            provider,
            code,
            redirect_uri,
            code_verifier=code_verifier,
        )
        access_token = token_response.get("access_token")
        if not access_token:
            raise HTTPException(502, "No access token in provider response")

        # Fetch user profile
        raw_profile = await fetch_userinfo(provider, access_token)
        user_data = extract_user_data(provider, raw_profile)

        # Create session
        response = Response.redirect("/")
        # The verifier is single-use — clear it so it can't back a second callback.
        response.delete_cookie("hyper_pkce_verifier")
        if self._session_auth:
            session_id = self._session_auth.login(response, user_data, request)

            # Store tokens server-side
            expires_in = token_response.get("expires_in", 3600)
            self.store_tokens(
                session_id,
                OAuth2Tokens(
                    access_token=access_token,
                    refresh_token=token_response.get("refresh_token"),
                    expires_at=time.time() + int(expires_in),
                    provider=provider.name,
                    scopes=provider.scopes,
                ),
            )

        return response


def require_oauth2(provider_name: str | None = None):
    """Decorator that requires OAuth2-authenticated session.

    Args:
        provider_name: If specified, requires authentication via this specific provider.

    Usage:
        @app.get("/dashboard")
        @require_oauth2()
        async def dashboard(request):
            return {"user": request.user}

        @app.get("/google-only")
        @require_oauth2("google")
        async def google_dashboard(request):
            return {"user": request.user}
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request, *args, **kwargs):
            user = request.user
            if user is None or not user.is_authenticated:
                raise HTTPException(401, "OAuth2 authentication required")
            if "oauth2_provider" not in user:
                raise HTTPException(401, "OAuth2 authentication required")
            if provider_name and user.get("oauth2_provider") != provider_name:
                raise HTTPException(403, f"Requires {provider_name} authentication")
            return await func(request, *args, **kwargs)

        return wrapper

    return decorator
