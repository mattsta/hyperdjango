"""
Session-based authentication using HMAC-signed cookies.

No JWT. Sessions are stored server-side with pluggable backends:
- InMemorySessionStore: fast, single-process (default for development)
- DatabaseSessionStore: PostgreSQL UNLOGGED table (production, multi-server)

The cookie contains only a signed session ID.

Usage:
    # In-memory (development)
    app.use(SessionAuth(secret="your-secret-key"))

    # PostgreSQL-backed (production, multi-server coordination)
    # Table DDL from HyperSession model via `hyper setup`
    store = DatabaseSessionStore(max_age=86400)
    app.use(SessionAuth(secret="your-secret-key", store=store))
"""

import asyncio
import functools
import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse

from sortedcontainers import SortedList

from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import AnonymousUser, SessionUser, User
from hyperdjango.conf import get_setting
from hyperdjango.native._crypto import (
    hmac_sha256_hex,
    sign_data,
    verify_signed_data,
)
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.types import CookieKwargs, SessionData, UserID

_logger = logging.getLogger(__name__)

# ── Native telemetry (zero cost when disabled) ──────────────────────────────
#
# Per-request session-auth outcome counter. The `result` label is a small
# bounded enum (no_cookie / invalid_cookie / not_found / hash_mismatch / ok)
# so cardinality stays at five regardless of traffic shape.

_session_auth_total = _tel_metrics.CounterVec(
    "hyperdjango_session_auth_total",
    "Session authentication outcomes by result.",
    label_names=("result",),
)

# ── Session auth hash ─────────────────────────────────────────────────────
# Inspired by Django's get_session_auth_hash(): HMAC of the password hash
# using the app secret. Stored in the session on login, verified on each
# request. When a user changes their password, the hash changes, and all
# old sessions become invalid (forcing re-login everywhere).
#
# This is a lazy invalidation strategy: sessions aren't deleted immediately,
# but silently fail auth check on next request. This avoids needing to find
# and delete all sessions for a user on password change — the verification
# catches it naturally.

_SESSION_HASH_KEY = "_session_auth_hash"

# Every N failed-login records, sweep IPs whose attempts aged out of the lockout
# window so a distributed brute-force can't grow the tracking dict without bound.
_LOGIN_SWEEP_INTERVAL = 1000


def get_session_auth_hash(password_hash: str, secret: str) -> str:
    """Compute the session auth hash from a user's password hash.

    This is an HMAC-SHA256 of the password_hash keyed by the app secret.
    Changes whenever the password changes, invalidating all existing sessions.
    """
    return hmac_sha256_hex(secret.encode(), password_hash.encode())


def verify_session_auth_hash(
    session_hash: str, password_hash: str, secret: str
) -> bool:
    """Verify a stored session hash against the current password hash.

    Uses constant-time comparison to prevent timing attacks.
    """
    expected = get_session_auth_hash(password_hash, secret)
    return hmac.compare_digest(session_hash, expected)


# ── Async-framework user accessor ──────────────────────────────────────────
#
# Async views need to read the request user without blocking the event loop.
# Every place that sets ``request.user`` also installs
# ``request.auser`` — a zero-arg callable returning a coroutine that resolves
# to the same user object. Usage in async views:
#
#     user = await request.auser()
#
# while ``request.user`` keeps working unchanged for sync code. The accessor
# is always awaitable: it captures the user set at login/logout time, so it
# never re-hits the session store and is safe to await any number of times.


async def _resolve_auth_user(user: object) -> object:
    """Coroutine body for ``request.auser`` — returns the captured user."""
    return user


def _set_auth_user(request: Request, user: object) -> None:
    """Set both ``request.user`` (sync) and ``request.auser`` (async accessor).

    Keeps the sync attribute working unchanged while installing an awaitable
    accessor for async frameworks.
    ``request.auser`` is a zero-arg callable returning a coroutine that
    resolves to the same ``user`` object (``await request.auser()``).
    """
    request.user = user
    request.auser = functools.partial(_resolve_auth_user, user)


# ── request.session bridge ──────────────────────────────────────────────────
#
# The standalone stack has no request.session by default — SessionAuth only
# exposed request.user / request.session_id. Flash messages (hyperdjango.messages)
# and any code that reads/writes request.session therefore silently fell back to
# a per-request dict that did not survive a redirect. SessionAuth now attaches a
# `_SessionDict` as request.session and persists it to the session store after
# the response — but only when it was actually mutated, so read-only requests
# never touch the store.

# A session authenticates a user only when it carries a positive identity marker.
# This is an ALLOW-LIST (not a "anything that isn't flash" deny-list): the
# request.session bridge lets an *anonymous* request persist arbitrary keys
# (cart, form-wizard state, dismissed-banner flags), and a deny-list would
# wrongly promote any such session to authenticated on the next request.
#
# Identity may be a numeric id (user_id/id/pk) OR a username — apps that key
# users by username authenticate on username alone. This set is pinned by
# test_security_regressions_round3::test_login_shaped_sessions_authenticate.
_AUTH_IDENTITY_KEYS = frozenset({"user_id", "id", "pk", "username"})

# SECURITY (privilege escalation): identity and authorization are established
# ONLY by login()/build_session_data(), which write the user_data dict DIRECTLY
# to the session store — never through the request.session bridge. Application
# code that reaches request.session[...] is therefore untrusted with respect to
# these keys: because the session cookie is server-signed, an anonymous request
# that could set e.g. session["user_id"] = 999 (or session["groups"] =
# ["superuser"]) would have that value load back as trusted state on its next
# request — a self-escalation to any user/role. The _SessionDict bridge below
# refuses application writes to these keys, closing the escalation at its source.
# Trusted session data loaded from the store still populates them, because
# dict construction bypasses the write guard (see _SessionDict.__init__).
_RESERVED_SESSION_KEYS = frozenset(
    {
        "user_id",
        "id",
        "pk",
        "username",
        "groups",
        "permissions",
        "is_staff",
        "is_superuser",
        "role",
        "field_access",
        _SESSION_HASH_KEY,
    }
)


def _is_user_session(data: dict) -> bool:
    """True when session data represents a logged-in user (not flash/anon state).

    Gated on a positive identity marker so anonymous sessions created solely to
    carry flash messages or app state across a redirect never authenticate.
    """
    return any(k in data for k in _AUTH_IDENTITY_KEYS)


class _SessionDict(dict):
    """A session-data dict that records whether it was mutated.

    Backs ``request.session``. SessionAuth persists it to the store after the
    response only when ``modified`` is True, keeping the no-write hot path free
    of any store round-trip.
    """

    # Subclasses the builtin ``dict`` (a C type) to intercept every mutation.
    # slots-required: a @dataclass cannot model a ``dict`` subclass.
    __slots__ = ("modified",)

    def __init__(self, *args, **kwargs):
        # Trusted store data is loaded here; dict.__init__ does NOT route through
        # __setitem__, so loaded identity/authorization keys are preserved. Only
        # subsequent application writes are subject to the reserved-key guard.
        super().__init__(*args, **kwargs)
        self.modified = False

    @staticmethod
    def _is_reserved(key: object) -> bool:
        """Reserved auth keys may only be established by auth.login()/logout(),
        never by an application (or anonymous) write through request.session."""
        if key in _RESERVED_SESSION_KEYS:
            _logger.warning(
                "request.session[%r] ignored: reserved auth key cannot be set "
                "via the session bridge; use auth.login() to establish identity.",
                key,
            )
            return True
        return False

    def __setitem__(self, key, value):
        if self._is_reserved(key):
            return
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key):
        super().__delitem__(key)
        self.modified = True

    def pop(self, *args):
        result = super().pop(*args)
        self.modified = True
        return result

    def popitem(self):
        result = super().popitem()
        self.modified = True
        return result

    def clear(self):
        if self:
            self.modified = True
        super().clear()

    def setdefault(self, key, default=None):
        if self._is_reserved(key):
            return super().get(key, default)
        self.modified = True
        return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        incoming = dict(*args, **kwargs)
        filtered = {k: v for k, v in incoming.items() if not self._is_reserved(k)}
        if filtered:
            self.modified = True
            super().update(filtered)


class InMemorySessionStore:
    """In-memory session store for development.

    Fast, single-process only. Sessions lost on restart.

    Uses SortedList for O(log n) expiry cleanup and user_id index
    for O(1) user session lookups instead of full-dict scans.
    """

    def __init__(self, max_age=86400):
        self._sessions: dict[str, dict[str, SessionData | float]] = {}
        # Sorted by expiry time for O(log n + k) cleanup
        self._expiry_index: SortedList = SortedList(key=lambda x: x[0])
        # user_id → {session_ids} for O(1) user invalidation
        self._user_index: defaultdict[UserID, set[str]] = defaultdict(set)
        self.max_age = max_age
        self._is_async = False
        # THREAD SAFETY: _sessions, _expiry_index (a SortedList) and _user_index
        # form a single consistent data structure that must be mutated together.
        # Under free-threading (no GIL) concurrent access corrupts the SortedList
        # (IndexError), double-deletes dict keys (KeyError), mutates sets while
        # iterating (RuntimeError) and diverges the indexes (orphaned entries →
        # missed session revocation). Every method that reads or mutates this
        # trio holds this single lock. Internal helpers whose names end in
        # ``_locked`` (and ``_remove_session``) assume it is already held and
        # must NEVER be called without it (the lock is non-reentrant).
        self._lock = threading.Lock()

    def _user_id_for(self, data: SessionData) -> UserID | None:
        """Extract user_id from session data."""
        return data.get("user_id") or data.get("id")

    def create(self, data: SessionData) -> str:
        """Create a new session, return session ID."""
        session_id = secrets.token_urlsafe(32)
        created = time.time()
        with self._lock:
            self._sessions[session_id] = {
                "data": data,
                "created": created,
            }
            expires_at = created + self.max_age
            self._expiry_index.add((expires_at, session_id))
            uid = self._user_id_for(data)
            if uid is not None:
                self._user_index[uid].add(session_id)
        return session_id

    def get(self, session_id: str) -> SessionData | None:
        """Get session data by ID. Returns None if expired or missing."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if time.time() - session["created"] > self.max_age:
                self._remove_session(session_id, session)
                return None
            return session["data"]

    def update(self, session_id: str, data: SessionData):
        """Update session data."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                # Remove old expiry entry
                old_expires = session["created"] + self.max_age
                self._expiry_index.discard((old_expires, session_id))
                # Remove old user index
                old_uid = self._user_id_for(session["data"])
                if old_uid is not None and session_id in self._user_index.get(
                    old_uid, set()
                ):
                    self._user_index[old_uid].discard(session_id)
                # Update
                created = time.time()
                session["data"] = data
                session["created"] = created
                self._expiry_index.add((created + self.max_age, session_id))
                uid = self._user_id_for(data)
                if uid is not None:
                    self._user_index[uid].add(session_id)

    def delete(self, session_id: str):
        """Delete a session."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                self._expiry_index.discard(
                    (session["created"] + self.max_age, session_id)
                )
                uid = self._user_id_for(session["data"])
                if uid is not None:
                    self._user_index.get(uid, set()).discard(session_id)

    def cleanup(self):
        """Remove all expired sessions. O(log n + k) where k = expired count."""
        with self._lock:
            self._cleanup_locked()

    def _cleanup_locked(self):
        """Expiry sweep; caller must hold ``self._lock``."""
        now = time.time()
        while self._expiry_index and self._expiry_index[0][0] <= now:
            expires_at, sid = self._expiry_index.pop(0)
            session = self._sessions.get(sid)
            if session is not None and session["created"] + self.max_age == expires_at:
                uid = self._user_id_for(session["data"])
                if uid is not None:
                    self._user_index.get(uid, set()).discard(sid)
                del self._sessions[sid]

    def count(self) -> int:
        """Count active sessions."""
        with self._lock:
            self._cleanup_locked()
            return len(self._sessions)

    def invalidate_for_user(self, user_id: UserID):
        """Invalidate all sessions for a user. O(k) where k = user's sessions."""
        with self._lock:
            sids = self._user_index.pop(user_id, set())
            for sid in sids:
                session = self._sessions.pop(sid, None)
                if session is not None:
                    self._expiry_index.discard((session["created"] + self.max_age, sid))

    def invalidate_by_hash(self, user_id: UserID, valid_hash: str):
        """Invalidate sessions where the session hash doesn't match.

        Uses user_id index for O(k) lookup instead of full-dict scan.
        """
        with self._lock:
            sids = list(self._user_index.get(user_id, set()))
            for sid in sids:
                session = self._sessions.get(sid)
                if session is not None:
                    if session["data"].get(_SESSION_HASH_KEY, "") != valid_hash:
                        self._remove_session(sid, session)

    def _remove_session(self, session_id: str, session: dict[str, SessionData | float]):
        """Remove a session from all indexes. Caller must hold ``self._lock``."""
        del self._sessions[session_id]
        self._expiry_index.discard((session["created"] + self.max_age, session_id))
        uid = self._user_id_for(session["data"])
        if uid is not None:
            self._user_index.get(uid, set()).discard(session_id)


# Default global session store
_store = InMemorySessionStore()


def is_safe_redirect_url(url: str) -> bool:
    """Validate that a redirect URL is safe (no open redirect).

    Only allows relative URLs that start with / and don't redirect to external hosts.
    Rejects: protocol-relative (//evil.com), absolute (http://evil.com),
    backslash tricks (/\\evil.com), data: URIs, javascript: URIs, and
    URLs with embedded credentials (user:pass@host).
    """
    if not url or not url.startswith("/"):
        return False
    # Reject any '//'-leading target. urlparse('//evil.com') reports
    # netloc='evil.com' (caught below), but urlparse('///evil.com') reports an
    # EMPTY netloc with path='/evil.com' — so the netloc check alone MISSES it,
    # while browsers/clients can still treat a '//'-leading Location as
    # cross-origin (protocol-relative). Reject the whole class up front (matches
    # Django's url_has_allowed_host_and_scheme).
    if url.startswith("//"):
        return False
    parsed = urlparse(url)
    # Reject if urlparse detects a scheme or netloc (covers http://, //host, etc.)
    if parsed.scheme or parsed.netloc:
        return False
    # Reject backslash after leading slash (browser normalization attack)
    return not (len(url) > 1 and url[1] == "\\")


class SessionAuth:
    """Session authentication middleware.

    Reads a signed session cookie, validates it, and attaches
    the user data to request.user.

    Supports both sync (InMemorySessionStore) and async (DatabaseSessionStore)
    backends transparently.

    Usage:
        app.use(SessionAuth(secret="your-secret-key"))

        # With database backend:
        store = DatabaseSessionStore(db)
        app.use(SessionAuth(secret="your-secret-key", store=store))
    """

    def __init__(
        self,
        secret,
        cookie_name=None,
        store=None,
        secure_cookie=True,
        cookie_httponly=None,
        cookie_samesite=None,
        get_user=None,
        verify_auth_hash=True,
        max_login_attempts: int = 10,
        login_lockout_seconds: int = 300,
        token_engine=None,
        db=None,
    ):
        self.secret = secret
        # Optional DB handle enabling RBAC: when provided, a PermissionChecker is
        # installed on every request as request._perm_checker so `@require_permission`
        # works on this (secure, auth-hash-verified) path. Without db, permission
        # checks 403 with "Permission system not configured".
        self.db = db
        self._perm_checker = PermissionChecker(db) if db is not None else None
        # TokenEngine for session cookie signing (optional).
        # When set, cookies are signed with HMAC + XOR + salt via TokenEngine.
        # When unset, cookies are HMAC-signed via sign_data (the default).
        self.token_engine = token_engine
        self.cookie_name = (
            cookie_name
            if cookie_name is not None
            else get_setting("SESSION_COOKIE_NAME")
        )
        self.store = store or _store
        # Wire SESSION_COOKIE_AGE into the store's max_age when using the default store
        if store is None:
            cookie_age = get_setting("SESSION_COOKIE_AGE")
            self.store.max_age = cookie_age
        # Cookie-policy overrides. secure_cookie defaults True (a Secure-by-default
        # floor); cookie_httponly / cookie_samesite default None, meaning "defer to
        # the SESSION_COOKIE_* conf setting". An explicit value on any of them wins.
        self.secure_cookie = secure_cookie
        self.cookie_httponly = cookie_httponly
        self.cookie_samesite = cookie_samesite
        # Async callable: get_user(user_id) -> user_dict or None
        # Required for session auth hash verification (fetches current password_hash)
        self._get_user = get_user
        self.verify_auth_hash = verify_auth_hash
        # Brute force protection: per-IP login attempt tracking
        self.max_login_attempts = max_login_attempts
        self.login_lockout_seconds = login_lockout_seconds
        self._login_attempts: dict[str, list[float]] = {}
        self._login_attempts_lock = threading.Lock()
        self._login_record_count = 0
        # Set by OAuth2.set_session_auth() when this instance backs an OAuth2
        # flow; lets logout() evict the OAuth2 token store for the session.
        self._oauth = None

    def _sign_session_id(self, session_id: str) -> str:
        """Sign a session ID for cookie storage.

        Uses TokenEngine if configured (salted, XOR-obfuscated, key-rotatable),
        otherwise falls back to simple HMAC sign_data().
        """
        if self.token_engine is not None:
            return self.token_engine.encode_ref(session_id)
        return sign_data(session_id, self.secret)

    def _verify_session_cookie(self, cookie: str) -> str | None:
        """Verify a session cookie and extract the session ID.

        Uses TokenEngine when configured, otherwise plain HMAC sign_data.
        """
        if self.token_engine is not None:
            return self.token_engine.decode_ref(cookie)
        return verify_signed_data(cookie, self.secret)

    async def _store_get(self, session_id: str) -> SessionData | None:
        """Get session data — handles both sync and async stores."""
        result = self.store.get(session_id)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            return await result
        return result

    async def _store_create(self, data: SessionData) -> str:
        """Create session — handles both sync and async stores."""
        result = self.store.create(data)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            return await result
        return result

    async def _store_delete(self, session_id: str):
        """Delete session — handles both sync and async stores."""
        result = self.store.delete(session_id)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            await result

    async def _store_update(self, session_id: str, data: SessionData):
        """Update session data — handles both sync and async stores."""
        result = self.store.update(session_id, data)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            await result

    async def __call__(self, request, call_next):
        # Default to anonymous for both sync (request.user) and async
        # (await request.auser()) accessors. login()/logout() overwrite both.
        #
        # SECURITY: use an AnonymousUser() sentinel, never None. Any permission
        # class or guard that reads request.user.is_authenticated would
        # AttributeError on None — an error upstream code has historically
        # swallowed into an allow. A real AnonymousUser() guarantees
        # is_authenticated is False for unauthenticated requests.
        _set_auth_user(request, AnonymousUser())
        # Install the RBAC checker (if this instance was given a db) so
        # `@require_permission` can evaluate; None → those routes 403.
        request._perm_checker = self._perm_checker
        request.session_id = None
        # Empty session bridge by default — writes to request.session (e.g. flash
        # messages) mark it modified and get persisted after the response. A
        # request that never writes leaves modified=False, so the store is never
        # touched on the hot path.
        request.session = _SessionDict()

        # Track the outcome of the session lookup as a single small enum.
        # Bumped exactly once per request after the lookup completes — keeps
        # the metric path off the inner branches and avoids double-counting.
        outcome = "no_cookie"
        cookie = request.cookies.get(self.cookie_name)
        if cookie:
            session_id = self._verify_session_cookie(cookie)
            if not session_id:
                outcome = "invalid_cookie"
            else:
                data = await self._store_get(session_id)
                if not data:
                    outcome = "not_found"
                else:
                    # Verify session auth hash if enabled and get_user is configured.
                    # This catches password changes: the stored hash no longer matches
                    # the current password_hash, so the session is silently invalidated.
                    if (
                        self.verify_auth_hash
                        and self._get_user
                        and _SESSION_HASH_KEY in data
                    ):
                        valid = await self._verify_session_hash(data)
                        if not valid:
                            # Password changed — invalidate this session
                            await self._store_delete(session_id)
                            outcome = "hash_mismatch"
                            data = None

                    if data:
                        # Expose the loaded session data via request.session so
                        # flash messages and other session consumers see it.
                        request.session = _SessionDict(data)
                        request.session_id = session_id
                        # Authenticate any real login session. A session that
                        # holds only flash messages (anonymous) stays anonymous
                        # rather than appearing logged in.
                        if _is_user_session(data):
                            _set_auth_user(request, SessionUser(data))
                        outcome = "ok"
        _session_auth_total.inc_tuple((outcome,))

        response = await call_next(request)

        # Persist the session bridge if it was written to during the request.
        # Authenticated sessions update in place (cookie already valid); a
        # non-empty anonymous session (e.g. flash messages set before login) is
        # created on demand below so it survives the redirect. Runs BEFORE the
        # SESSION_SAVE_EVERY_REQUEST block so a freshly-created anonymous session
        # isn't cookie-set twice (session_id is still None during that block).
        needs_new_cookie = False
        session = request.session
        if isinstance(session, _SessionDict) and session.modified:
            if request.session_id is not None:
                await self._store_update(request.session_id, dict(session))
            elif session:
                new_id = await self._store_create(dict(session))
                request.session_id = new_id
                needs_new_cookie = True

        # SESSION_SAVE_EVERY_REQUEST: re-set the cookie on every request
        # to extend its lifetime (touch the expiry). A just-created anonymous
        # session (needs_new_cookie) also gets its cookie issued here — one path,
        # no duplicate Set-Cookie.
        save_every = get_setting("SESSION_SAVE_EVERY_REQUEST")
        if needs_new_cookie or (save_every and request.session_id is not None):
            signed = self._sign_session_id(request.session_id)
            response.set_cookie(self.cookie_name, signed, **self._cookie_kwargs())
            # Response varies on Cookie when we re-issue the session cookie
            # per request; without Vary: Cookie, shared caches may serve one
            # user's response to another.
            existing_vary = (
                response.headers.get("Vary") or response.headers.get("vary") or ""
            )
            tokens = [t.strip() for t in existing_vary.split(",") if t.strip()]
            if not any(t.lower() == "cookie" for t in tokens):
                tokens.append("Cookie")
                response.headers["Vary"] = ", ".join(tokens)

        return response

    async def _verify_session_hash(self, session_data: SessionData) -> bool:
        """Verify the session auth hash against the user's current password hash.

        Fetches the current user from DB to get the latest password_hash,
        then compares the stored session hash against the expected hash.
        Returns False if the password has changed (hash mismatch).
        """
        stored_hash = session_data.get(_SESSION_HASH_KEY, "")
        if not stored_hash:
            return True  # No hash stored (session predates auth-hash tracking) — allow through

        user_id = session_data.get("user_id") or session_data.get("id")
        if user_id is None:
            return True  # No user ID — can't verify, allow through

        current_user = self._get_user(user_id)
        if asyncio.iscoroutine(current_user) or asyncio.isfuture(current_user):
            current_user = await current_user

        if current_user is None:
            return False  # User deleted — invalidate session

        password_hash = (
            current_user.get("password_hash", "")
            if isinstance(current_user, dict)
            # dynamic-attr: _get_user is a caller-supplied callback; a non-dict return is an arbitrary user object whose password_hash attr is optional
            else getattr(current_user, "password_hash", "")
        )
        return verify_session_auth_hash(stored_hash, password_hash, self.secret)

    def _inject_session_hash(
        self, user_data: dict[str, str | int | None]
    ) -> dict[str, str | int | None]:
        """Inject session auth hash into user data before storing.

        If the user_data contains a password_hash field, compute the session
        auth hash and store it alongside the session data. On subsequent
        requests, this hash is verified against the current password_hash
        to detect password changes.
        """
        password_hash = user_data.get("password_hash", "")
        if password_hash:
            user_data[_SESSION_HASH_KEY] = get_session_auth_hash(
                password_hash, self.secret
            )
            # Only the derived HMAC marker is needed on subsequent requests
            # (verification re-fetches the CURRENT password_hash from the DB via
            # _get_user). Don't persist the raw password hash in the session
            # store — that is credential material we have no reason to keep.
            user_data.pop("password_hash", None)
        return user_data

    def _cookie_kwargs(self) -> CookieKwargs:
        """Build cookie keyword arguments from conf settings."""
        expire_at_close = get_setting("SESSION_EXPIRE_AT_BROWSER_CLOSE")
        domain_setting = get_setting("SESSION_COOKIE_DOMAIN")
        path_setting = get_setting("SESSION_COOKIE_PATH")

        max_age = None if expire_at_close else self.store.max_age
        domain = domain_setting or None

        # An explicit constructor param wins; otherwise fall back to the conf
        # setting. secure_cookie is a Secure-by-default floor (True unless the
        # caller passes secure_cookie=False, which defers to the setting);
        # cookie_httponly / cookie_samesite default None → use the setting.
        secure = self.secure_cookie or get_setting("SESSION_COOKIE_SECURE")
        httponly = (
            self.cookie_httponly
            if self.cookie_httponly is not None
            else get_setting("SESSION_COOKIE_HTTPONLY")
        )
        samesite = (
            self.cookie_samesite
            if self.cookie_samesite is not None
            else get_setting("SESSION_COOKIE_SAMESITE")
        )

        return {
            "max_age": max_age,
            "path": path_setting,
            "domain": domain,
            "httponly": httponly,
            "secure": secure,
            "samesite": samesite,
        }

    def is_login_blocked(self, client_ip: str) -> bool:
        """Check if a client IP is blocked due to too many failed login attempts.

        Returns True if the IP has exceeded max_login_attempts within the
        login_lockout_seconds window.

        Usage in login handlers:
            if auth.is_login_blocked(request.client_ip):
                return Response.error(429, "Too many attempts")
        """
        if self.max_login_attempts <= 0:
            return False
        now = time.time()
        cutoff = now - self.login_lockout_seconds
        with self._login_attempts_lock:
            attempts = self._login_attempts.get(client_ip)
            if attempts is None:
                return False
            # Prune expired entries
            recent = [t for t in attempts if t > cutoff]
            self._login_attempts[client_ip] = recent
            return len(recent) >= self.max_login_attempts

    def record_failed_login(self, client_ip: str) -> None:
        """Record a failed login attempt for the given client IP.

        Called after credential verification fails. Tracks timestamps
        to enforce the rate limit window.

        Usage in login handlers:
            if not verify_password(password, user.password_hash):
                auth.record_failed_login(request.client_ip)
                return Response.error(401, "Invalid credentials")
        """
        now = time.time()
        with self._login_attempts_lock:
            # Bound memory: without this, a distributed brute-force from many
            # real IPs would grow this dict without limit (memory DoS). Every
            # _LOGIN_SWEEP_INTERVAL records, drop IPs whose most recent attempt
            # has aged out of the lockout window — they can no longer block, so
            # they only waste memory. O(N) amortized to O(1) per record.
            self._login_record_count += 1
            if self._login_record_count % _LOGIN_SWEEP_INTERVAL == 0:
                cutoff = now - self.login_lockout_seconds
                self._login_attempts = {
                    ip: ts
                    for ip, ts in self._login_attempts.items()
                    if ts and ts[-1] >= cutoff
                }
            if client_ip not in self._login_attempts:
                self._login_attempts[client_ip] = []
            self._login_attempts[client_ip].append(now)

    def clear_login_attempts(self, client_ip: str) -> None:
        """Clear failed login attempts for a client IP after successful login."""
        with self._login_attempts_lock:
            self._login_attempts.pop(client_ip, None)

    def login(
        self, response: Response, user_data: SessionData, request: Request | None = None
    ) -> str:
        """Create a session and set the cookie on the response.

        Prevents session fixation by rotating ONLY the session presented in
        this request's cookie (an anonymous or attacker-planted id must not
        survive the anon→authenticated upgrade). The user's other sessions —
        a phone, another browser — are untouched, so multi-location login keeps
        working; enforcing single-point-of-login is a per-application concern,
        not a framework default. Stores a session auth hash derived from the
        user's password_hash — if the password changes, this hash becomes
        invalid and the session is silently revoked.

        Cookie attributes are controlled by conf settings:
        - SESSION_COOKIE_DOMAIN: cookie domain scope
        - SESSION_COOKIE_PATH: cookie path scope
        - SESSION_EXPIRE_AT_BROWSER_CLOSE: if True, omit max-age (session cookie)

        Note: For async stores (DatabaseSessionStore), use login_async() instead.
        This method works with sync stores (InMemorySessionStore).
        """
        # Session fixation prevention: invalidate old session
        if request is not None:
            old_cookie = request.cookies.get(self.cookie_name)
            if old_cookie:
                old_id = self._verify_session_cookie(old_cookie)
                if old_id:
                    self.store.delete(old_id)

        self._inject_session_hash(user_data)
        session_id = self.store.create(user_data)
        signed = self._sign_session_id(session_id)
        response.set_cookie(self.cookie_name, signed, **self._cookie_kwargs())
        # Reflect the just-logged-in user on the request for both sync
        # (request.user) and async (await request.auser()) consumers.
        if request is not None:
            _set_auth_user(request, SessionUser(user_data))
            request.session_id = session_id
            # Rebind the session bridge to the fresh session data so a flash
            # message added after login() is merged with the login data rather
            # than overwriting it. modified=False → not re-persisted unless
            # further mutated.
            request.session = _SessionDict(user_data)
        return session_id

    async def login_async(
        self, response: Response, user_data: SessionData, request: Request | None = None
    ) -> str:
        """Async version of login() — works with both sync and async stores.

        Rotates ONLY the session presented in this request's cookie (see
        login()); the user's other sessions survive, so multi-location login
        keeps working. Stores a session auth hash derived from the user's
        password_hash. Cookie attributes are controlled by the same conf
        settings as login().
        """
        if request is not None:
            old_cookie = request.cookies.get(self.cookie_name)
            if old_cookie:
                old_id = self._verify_session_cookie(old_cookie)
                if old_id:
                    await self._store_delete(old_id)

        self._inject_session_hash(user_data)
        session_id = await self._store_create(user_data)
        signed = self._sign_session_id(session_id)
        response.set_cookie(self.cookie_name, signed, **self._cookie_kwargs())
        # Reflect the just-logged-in user on the request for both sync
        # (request.user) and async (await request.auser()) consumers.
        if request is not None:
            _set_auth_user(request, SessionUser(user_data))
            request.session_id = session_id
            # Rebind the session bridge to the fresh session data (see login()).
            request.session = _SessionDict(user_data)
        return session_id

    def logout(
        self, response: Response, session_id: str, request: Request | None = None
    ):
        """Destroy a session and clear the cookie.

        For async stores, use logout_async() instead.
        """
        self.store.delete(session_id)
        if self._oauth is not None:
            self._oauth.clear_tokens(session_id)
        response.delete_cookie(self.cookie_name)
        # Reset the request user to anonymous for both sync and async accessors.
        if request is not None:
            _set_auth_user(request, AnonymousUser())
            request.session_id = None
            # Drop the session bridge so stale data isn't re-persisted post-logout.
            request.session = _SessionDict()

    async def logout_async(
        self, response: Response, session_id: str, request: Request | None = None
    ):
        """Async version of logout()."""
        await self._store_delete(session_id)
        if self._oauth is not None:
            self._oauth.clear_tokens(session_id)
        response.delete_cookie(self.cookie_name)
        # Reset the request user to anonymous for both sync and async accessors.
        if request is not None:
            _set_auth_user(request, AnonymousUser())
            request.session_id = None
            # Drop the session bridge so stale data isn't re-persisted post-logout.
            request.session = _SessionDict()

    def get_login_redirect_url(self) -> str:
        """Return the URL to redirect to after successful login.

        Reads LOGIN_REDIRECT_URL from conf settings.
        """
        return get_setting("LOGIN_REDIRECT_URL")

    def get_logout_redirect_url(self) -> str:
        """Return the URL to redirect to after logout.

        Reads LOGOUT_REDIRECT_URL from conf settings.
        """
        return get_setting("LOGOUT_REDIRECT_URL")

    def login_and_redirect(
        self, response: Response, user_data: SessionData, request: Request | None = None
    ) -> Response:
        """Create a session and return a redirect Response to LOGIN_REDIRECT_URL.

        Convenience method that combines login() with a redirect to the
        configured post-login URL. Respects ?next= query parameter if present.

        For async stores, use login_async_and_redirect() instead.
        """
        self.login(response, user_data, request)
        redirect_url = self.get_login_redirect_url()
        if request is not None:
            next_url = request.GET.get("next", "")
            if is_safe_redirect_url(next_url):
                redirect_url = next_url
        return response

    async def login_async_and_redirect(
        self, response: Response, user_data: SessionData, request: Request | None = None
    ) -> Response:
        """Async version of login_and_redirect()."""
        await self.login_async(response, user_data, request)
        redirect_url = self.get_login_redirect_url()
        if request is not None:
            next_url = request.GET.get("next", "")
            if is_safe_redirect_url(next_url):
                redirect_url = next_url
        return response

    def logout_and_redirect(self, session_id: str) -> Response:
        """Destroy the session and return a redirect to LOGOUT_REDIRECT_URL.

        For async stores, use logout_async_and_redirect() instead.
        """
        redirect_url = self.get_logout_redirect_url()
        resp = Response.redirect(redirect_url)
        self.logout(resp, session_id)
        return resp

    async def logout_async_and_redirect(self, session_id: str) -> Response:
        """Async version of logout_and_redirect()."""
        redirect_url = self.get_logout_redirect_url()
        resp = Response.redirect(redirect_url)
        await self.logout_async(resp, session_id)
        return resp


class _SessionPermUser:
    """Minimal user shim exposing only the primary key.

    ``PermissionChecker._get_all_permissions`` resolves a user's permissions
    from a user object (via ``id``/``pk``) and memoizes them on that object.
    ``build_session_data`` only has a bare ``user_id``, so this thin carrier
    gives the checker the pk it needs — plus a ``__dict__`` for its
    request-lifetime cache — without materializing a full ``User`` row.
    """

    def __init__(self, user_id: object) -> None:
        self.id = user_id
        self.pk = user_id


async def build_session_data(
    user_id: int,
    db: object | None,
    *,
    groups: list[str] | None = None,
    **extra: object,
) -> dict[str, object]:
    """Build a complete session dict with RBAC groups for ``auth.login()``.

    Fetches group memberships from RBAC tables and populates the session
    with ``groups``, ``is_staff``, ``is_superuser`` — all derived from
    group membership, not boolean fields.

    Args:
        user_id: The user's primary key (hyper_users.id or app user table).
        db: Database instance for RBAC queries, or None when no RBAC backend is
            configured (groups fall back to [] and field_access to {}).
        groups: Optional pre-computed groups list (skips DB query).
        **extra: Additional session fields (id, username, role, etc.).

    Returns:
        Complete session dict ready for ``auth.login(response, session_data)``.

    Usage::

        from hyperdjango.auth.sessions import build_session_data

        session = await build_session_data(
            user.id, db, id=user.id, username=user.username,
        )
        auth.login(resp, session, request)
    """
    if groups is None:
        # Fetch RBAC group names via PermissionChecker (uses ORM, not raw SQL)
        checker = PermissionChecker(db)
        try:
            groups = await checker.get_user_group_names(user_id)
        # RBAC group load failed — fall back to empty groups. The sibling
        # field_access load below deliberately does NOT swallow.
        # blind-except: empty groups fail CLOSED (no privileges); over-denying on a transient error is safe.
        except Exception:
            groups = []

    # Fetch field-level permissions for session cache (superusers bypass all).
    #
    # SECURITY: do NOT swallow an RBAC-load error here. A transient DB/RBAC
    # failure during login must not cache a known-incomplete field-access map.
    # Require.field_access now fails CLOSED (an absent/unknown field defaults to
    # the most restrictive level), so a half-loaded map would silently over-DENY
    # and lock the user out of fields they legitimately have. We therefore let
    # the error propagate and abort the login rather than persisting a partial
    # map for the whole session. (The sibling groups=[] fallback is safe because
    # empty groups already fail CLOSED — no privileges.)
    #
    # When ``db is None`` there is no RBAC backend to consult (Django-style
    # apps that don't use field-level RBAC, or callers that pass pre-computed
    # groups without a pool). That is a well-defined "no field-level
    # restrictions configured" state — leave field_access empty rather than
    # dereferencing a None db. This does NOT weaken the fail-closed contract:
    # a db that IS provided still lets any load error propagate below.
    field_access: dict[str, dict[str, str]] = {}
    if db is not None and "superuser" not in groups:
        fa_checker = PermissionChecker(db)
        field_access = await fa_checker.get_all_field_access(user_id)

    # Cache RBAC permission codenames so Require.permission() resolves O(1) at
    # request time (no per-request DB query) — consistent with how ``groups``
    # and ``field_access`` are resolved once here at login. WITHOUT this the
    # session ``permissions`` key was never populated, so SessionUser.has_perm()
    # always saw an empty frozenset and Require.permission() denied EVERY
    # legitimate permission-holder (superuser-only fail-closed over-deny).
    #
    # SessionUser.has_perm() does an EXACT-match lookup against this set, while
    # the RBAC store keys permissions as "<model>.<codename>". We therefore
    # cache BOTH forms: the fully-qualified "model.codename" AND the bare
    # "codename". A bare codename matches any model that grants it — exactly the
    # any-model semantics PermissionChecker.has_perm() already applies to an
    # unscoped codename — so this invents no authority and weakens no scoping (a
    # caller scopes a check by passing "model.codename" to Require.permission()).
    #
    # Superusers are skipped (has_perm grants via the "superuser" group), and
    # with no RBAC backend (db is None) there is nothing to load — both leave
    # the set empty, mirroring the field_access block above. As with
    # field_access, a load error is deliberately NOT swallowed: it propagates
    # and aborts login rather than caching a known-incomplete permission set
    # that would silently over-DENY for the whole session lifetime.
    permissions: list[str] = []
    if db is not None and "superuser" not in groups:
        perm_checker = PermissionChecker(db)
        # _get_all_permissions is the single source of truth for a user's full
        # permission set (direct + inherited group perms via the recursive CTE),
        # so reuse it rather than re-deriving the SQL here.
        qualified = await perm_checker._get_all_permissions(_SessionPermUser(user_id))
        codenames: set[str] = set(qualified)
        for qualified_perm in qualified:
            _, sep, bare = qualified_perm.partition(".")
            if sep:
                codenames.add(bare)
        permissions = sorted(codenames)

    session: dict[str, object] = {
        "id": user_id,
        "groups": groups,
        "permissions": permissions,
        "is_staff": "staff" in groups,
        "is_superuser": "superuser" in groups,
        "field_access": field_access,
    }
    session.update(extra)

    # Password-change session revocation: thread the user's password_hash into
    # the session dict so SessionAuth.login()/_inject_session_hash can derive the
    # `_session_auth_hash` marker (an HMAC of the password hash). This marker is
    # what powers the "revoke old sessions when the password changes" check
    # (verify_auth_hash). login() strips the raw hash again before it is
    # persisted — only the derived marker survives in the store.
    #
    # Best-effort / defense-in-depth: if the hash can't be loaded (no db, custom
    # user table, transient error) we skip it — the session is still valid, it
    # just won't auto-revoke on a password change. A caller that already supplied
    # password_hash via **extra is respected as-is.
    if "password_hash" not in session and db is not None:
        try:
            user_row = await User.objects.using(db).filter(id=user_id).first()
            if user_row is not None and user_row.password_hash:
                session["password_hash"] = user_row.password_hash
        # The password_hash only seeds the optional password-change session-
        # revocation marker; the session is still fully valid without it.
        # blind-except: best-effort — a load failure only disables password-change auto-revocation, never breaks login.
        except Exception:
            _logger.debug(
                "build_session_data: could not load password_hash for session "
                "auth-hash (user_id=%s); password-change revocation disabled for "
                "this session",
                user_id,
                exc_info=True,
            )
    return session


# Convenience instance (configure secret via app or get_setting)
session_auth = SessionAuth(secret=get_setting("SESSION_SECRET"))
