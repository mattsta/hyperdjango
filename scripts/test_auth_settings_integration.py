"""
Tests that auth/session conf.py settings actually change behavior.

Each test sets a custom setting value, calls the relevant module function,
and verifies the behavior changed accordingly.

Covers: LOGIN_URL, LOGIN_REDIRECT_URL, LOGOUT_REDIRECT_URL,
PASSWORD_RESET_TIMEOUT, SESSION_EXPIRE_AT_BROWSER_CLOSE,
SESSION_COOKIE_DOMAIN, SESSION_COOKIE_PATH, SESSION_SAVE_EVERY_REQUEST.
"""

# hyper-test: unit

import asyncio
import sys
import time
import unittest
import unittest.mock

from hyperdjango.auth.user import SessionUser
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.response import Response
from hyperdjango.testkit import check, finish, run_main

# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


class _FakeRequest:
    """Minimal request object for testing."""

    def __init__(self, user=None, cookies=None, method="GET"):
        self.user = user
        self.cookies = cookies or {}
        self.method = method
        self.session_id = None
        self.app = None

    @property
    def GET(self):
        return {}


class _FakeUser:
    """Minimal user object for testing."""

    def __init__(self, is_authenticated=True, password_hash="hashed123"):
        self.is_authenticated = is_authenticated
        self.id = 1
        self.pk = 1
        self.password_hash = password_hash
        self.last_login = None
        self.username = "testuser"


# ── LoginRequiredMixin tests ────────────────────────────────────────────────


class TestLoginRequiredMixinLoginURL(unittest.TestCase):
    """LoginRequiredMixin redirects to LOGIN_URL from conf settings."""

    def _make_view_class(self, login_url=None):
        from hyperdjango.views import DetailView, LoginRequiredMixin

        attrs = {"model": None}
        if login_url is not None:
            attrs["login_url"] = login_url

        cls = type("TestView", (LoginRequiredMixin, DetailView), attrs)
        return cls

    def test_default_login_url(self):
        """Default LOGIN_URL is /login/ -- unauthenticated user redirected there."""
        cls = self._make_view_class()
        view = cls()
        request = _FakeRequest(user=None)
        view.request = request
        response = _run(view.dispatch(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/login/")

    def test_custom_login_url_from_settings(self):
        """Custom LOGIN_URL in conf changes the redirect target."""
        cls = self._make_view_class()
        view = cls()
        request = _FakeRequest(user=None)
        view.request = request
        with unittest.mock.patch.dict(DEFAULTS, {"LOGIN_URL": "/auth/signin/"}):
            response = _run(view.dispatch(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/auth/signin/")

    def test_class_level_login_url_overrides_setting(self):
        """Per-class login_url takes precedence over conf setting."""
        cls = self._make_view_class(login_url="/my-login/")
        view = cls()
        request = _FakeRequest(user=None)
        view.request = request
        with unittest.mock.patch.dict(DEFAULTS, {"LOGIN_URL": "/auth/signin/"}):
            response = _run(view.dispatch(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/my-login/")

    def test_authenticated_user_passes_through(self):
        """Authenticated dict user is not redirected."""
        from hyperdjango.views import DetailView, LoginRequiredMixin

        class TestView(LoginRequiredMixin, DetailView):
            model = None

            async def get(self, request, **kwargs):
                return Response.json({"ok": True})

        view = TestView()
        request = _FakeRequest(user=SessionUser({"username": "admin"}))
        view.request = request
        response = _run(view.dispatch(request))
        self.assertEqual(response.status, 200)

    def test_unauthenticated_user_object_redirects(self):
        """User object with is_authenticated=False triggers redirect."""
        cls = self._make_view_class()
        view = cls()
        user = _FakeUser(is_authenticated=False)
        request = _FakeRequest(user=user)
        view.request = request
        response = _run(view.dispatch(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/login/")

    def test_login_url_with_query_string(self):
        """LOGIN_URL can include query parameters."""
        cls = self._make_view_class()
        view = cls()
        request = _FakeRequest(user=None)
        view.request = request
        with unittest.mock.patch.dict(
            DEFAULTS, {"LOGIN_URL": "/login/?next=/dashboard/"}
        ):
            response = _run(view.dispatch(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/login/?next=/dashboard/")


# ── require_auth decorator tests ────────────────────────────────────────────


class TestRequireAuthLoginURL(unittest.TestCase):
    """require_auth decorator redirects to LOGIN_URL from conf settings."""

    def test_default_login_url_redirect(self):
        """Unauthenticated request redirects to default LOGIN_URL."""
        from hyperdjango.auth.decorators import require_auth

        @require_auth()
        async def protected(request):
            return Response.json({"ok": True})

        request = _FakeRequest(user=None)
        response = _run(protected(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/login/")

    def test_custom_login_url_from_settings(self):
        """Custom LOGIN_URL in conf changes redirect target."""
        from hyperdjango.auth.decorators import require_auth

        @require_auth()
        async def protected(request):
            return Response.json({"ok": True})

        request = _FakeRequest(user=None)
        with unittest.mock.patch.dict(DEFAULTS, {"LOGIN_URL": "/auth/login/"}):
            response = _run(protected(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/auth/login/")

    def test_decorator_login_url_overrides_setting(self):
        """Per-decorator login_url takes precedence over conf setting."""
        from hyperdjango.auth.decorators import require_auth

        @require_auth(login_url="/override-login/")
        async def protected(request):
            return Response.json({"ok": True})

        request = _FakeRequest(user=None)
        with unittest.mock.patch.dict(DEFAULTS, {"LOGIN_URL": "/auth/login/"}):
            response = _run(protected(request))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "/override-login/")

    def test_redirect_disabled_raises_401(self):
        """redirect_unauthenticated=False raises HTTPException instead of redirect."""
        from hyperdjango.app import HTTPException
        from hyperdjango.auth.decorators import require_auth

        @require_auth(redirect_unauthenticated=False)
        async def protected(request):
            return Response.json({"ok": True})

        request = _FakeRequest(user=None)
        with self.assertRaises(HTTPException) as ctx:
            _run(protected(request))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_authenticated_user_passes_through(self):
        """Authenticated user gets through to the actual view."""
        from hyperdjango.auth.decorators import require_auth

        @require_auth()
        async def protected(request):
            return Response.json({"ok": True})

        request = _FakeRequest(user=SessionUser({"username": "admin"}))
        response = _run(protected(request))
        self.assertEqual(response.status, 200)


# ── Session cookie setting tests ─────────────────────────────────────────────


class TestSessionCookieDomain(unittest.TestCase):
    """SESSION_COOKIE_DOMAIN controls the cookie domain attribute."""

    def _login(self, domain_setting):
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_COOKIE_DOMAIN": domain_setting,
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            auth.login(response, {"username": "admin"})
        return response

    def test_empty_domain_no_domain_attribute(self):
        """Empty SESSION_COOKIE_DOMAIN = no Domain on cookie."""
        response = self._login("")
        cookie = response.headers.get("set-cookie", "")
        self.assertNotIn("Domain=", cookie)

    def test_custom_domain_set(self):
        """SESSION_COOKIE_DOMAIN=".example.com" sets Domain on cookie."""
        response = self._login(".example.com")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Domain=.example.com", cookie)

    def test_subdomain(self):
        """SESSION_COOKIE_DOMAIN="app.example.com" sets Domain correctly."""
        response = self._login("app.example.com")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Domain=app.example.com", cookie)


class TestSessionCookiePath(unittest.TestCase):
    """SESSION_COOKIE_PATH controls the cookie path attribute."""

    def _login(self, path_setting):
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_COOKIE_PATH": path_setting,
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_DOMAIN": "",
            },
        ):
            auth.login(response, {"username": "admin"})
        return response

    def test_default_path_root(self):
        """Default SESSION_COOKIE_PATH is /."""
        response = self._login("/")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Path=/", cookie)

    def test_custom_path(self):
        """SESSION_COOKIE_PATH=/app/ sets custom path."""
        response = self._login("/app/")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Path=/app/", cookie)

    def test_deep_path(self):
        """SESSION_COOKIE_PATH=/api/v2/ sets deeply nested path."""
        response = self._login("/api/v2/")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Path=/api/v2/", cookie)


class TestSessionExpireAtBrowserClose(unittest.TestCase):
    """SESSION_EXPIRE_AT_BROWSER_CLOSE controls whether max-age is set."""

    def _login(self, expire_at_close):
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": expire_at_close,
                "SESSION_COOKIE_DOMAIN": "",
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            auth.login(response, {"username": "admin"})
        return response

    def test_false_sets_max_age(self):
        """SESSION_EXPIRE_AT_BROWSER_CLOSE=False includes Max-Age in cookie."""
        response = self._login(False)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Max-Age=3600", cookie)

    def test_true_no_max_age(self):
        """SESSION_EXPIRE_AT_BROWSER_CLOSE=True omits Max-Age (session cookie)."""
        response = self._login(True)
        cookie = response.headers.get("set-cookie", "")
        self.assertNotIn("Max-Age", cookie)

    def test_default_value_is_false(self):
        """Default value is False -- max-age is set by default."""
        self.assertFalse(DEFAULTS["SESSION_EXPIRE_AT_BROWSER_CLOSE"])


# ── SESSION_SAVE_EVERY_REQUEST tests ─────────────────────────────────────────


class TestSessionSaveEveryRequest(unittest.TestCase):
    """SESSION_SAVE_EVERY_REQUEST re-sets cookie on every request."""

    def _make_auth(self):
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        return auth, store

    def test_save_every_request_true_refreshes_cookie(self):
        """When SESSION_SAVE_EVERY_REQUEST=True, cookie is re-set on each request."""
        from hyperdjango.native._crypto import sign_data

        auth, store = self._make_auth()
        # Create a session
        session_id = store.create({"username": "admin"})
        signed = sign_data(session_id, "test-secret")

        request = _FakeRequest(
            user=SessionUser({"username": "admin"}),
            cookies={auth.cookie_name: signed},
        )

        response_from_view = Response.json({"ok": True})

        async def call_next(req):
            return response_from_view

        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_SAVE_EVERY_REQUEST": True,
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_DOMAIN": "",
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            response = _run(auth(request, call_next))

        cookie = response.headers.get("set-cookie", "")
        # Cookie should be set (refreshed)
        self.assertIn(f"{auth.cookie_name}=", cookie)
        self.assertIn("Max-Age=3600", cookie)

    def test_save_every_request_false_no_cookie_refresh(self):
        """When SESSION_SAVE_EVERY_REQUEST=False, cookie is NOT re-set."""
        from hyperdjango.native._crypto import sign_data

        auth, store = self._make_auth()
        session_id = store.create({"username": "admin"})
        signed = sign_data(session_id, "test-secret")

        request = _FakeRequest(
            user=SessionUser({"username": "admin"}),
            cookies={"session": signed},
        )

        response_from_view = Response.json({"ok": True})

        async def call_next(req):
            return response_from_view

        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_SAVE_EVERY_REQUEST": False,
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_DOMAIN": "",
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            response = _run(auth(request, call_next))

        cookie = response.headers.get("set-cookie", "")
        # No cookie refresh -- set-cookie should be empty
        self.assertEqual(cookie, "")

    def test_save_every_request_no_session_no_cookie(self):
        """When no session exists, SESSION_SAVE_EVERY_REQUEST does nothing."""
        auth, store = self._make_auth()

        request = _FakeRequest(user=None, cookies={})

        response_from_view = Response.json({"ok": True})

        async def call_next(req):
            return response_from_view

        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_SAVE_EVERY_REQUEST": True,
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_DOMAIN": "",
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            response = _run(auth(request, call_next))

        cookie = response.headers.get("set-cookie", "")
        self.assertEqual(cookie, "")


# ── login_async cookie settings tests ────────────────────────────────────────


class TestLoginAsyncCookieSettings(unittest.TestCase):
    """login_async uses the same conf settings as login."""

    def test_async_respects_domain(self):
        """login_async sets SESSION_COOKIE_DOMAIN on cookie."""
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_COOKIE_DOMAIN": ".async.example.com",
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            _run(auth.login_async(response, {"username": "admin"}))
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Domain=.async.example.com", cookie)

    def test_async_respects_expire_at_close(self):
        """login_async omits Max-Age when SESSION_EXPIRE_AT_BROWSER_CLOSE=True."""
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": True,
                "SESSION_COOKIE_DOMAIN": "",
                "SESSION_COOKIE_PATH": "/",
            },
        ):
            _run(auth.login_async(response, {"username": "admin"}))
        cookie = response.headers.get("set-cookie", "")
        self.assertNotIn("Max-Age", cookie)

    def test_async_respects_path(self):
        """login_async sets SESSION_COOKIE_PATH on cookie."""
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_COOKIE_PATH": "/async/",
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
                "SESSION_COOKIE_DOMAIN": "",
            },
        ):
            _run(auth.login_async(response, {"username": "admin"}))
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Path=/async/", cookie)


# ── PASSWORD_RESET_TIMEOUT tests ─────────────────────────────────────────────


class TestPasswordResetTimeout(unittest.TestCase):
    """PASSWORD_RESET_TIMEOUT configures token validity."""

    def test_default_timeout_from_settings(self):
        """PasswordResetTokenGenerator uses PASSWORD_RESET_TIMEOUT when no explicit timeout."""
        from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

        # Default is 259200 (3 days)
        gen = PasswordResetTokenGenerator(secret_key="test")
        self.assertEqual(gen.timeout, 259200)

    def test_custom_timeout_from_settings(self):
        """Custom PASSWORD_RESET_TIMEOUT changes generator timeout."""
        from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

        with unittest.mock.patch.dict(DEFAULTS, {"PASSWORD_RESET_TIMEOUT": 7200}):
            gen = PasswordResetTokenGenerator(secret_key="test")
        self.assertEqual(gen.timeout, 7200)

    def test_explicit_timeout_overrides_setting(self):
        """Explicit timeout parameter overrides conf setting."""
        from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

        with unittest.mock.patch.dict(DEFAULTS, {"PASSWORD_RESET_TIMEOUT": 7200}):
            gen = PasswordResetTokenGenerator(secret_key="test", timeout=1800)
        self.assertEqual(gen.timeout, 1800)

    def test_token_valid_within_timeout(self):
        """Token generated within timeout window validates successfully."""
        from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

        gen = PasswordResetTokenGenerator(secret_key="test", timeout=3600)
        user = _FakeUser()
        token = gen.make_token(user)
        self.assertTrue(gen.check_token(user, token))

    def test_token_expired_after_timeout(self):
        """Token generated before timeout window is rejected."""
        from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

        gen = PasswordResetTokenGenerator(secret_key="test", timeout=1)
        user = _FakeUser()
        # Generate token with a timestamp in the past
        old_time = time.time() - 10
        token = gen._make_token_with_timestamp(user, int(old_time))
        self.assertFalse(gen.check_token(user, token))

    def test_short_timeout_setting(self):
        """Very short PASSWORD_RESET_TIMEOUT (e.g. 60s) is respected."""
        from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

        with unittest.mock.patch.dict(DEFAULTS, {"PASSWORD_RESET_TIMEOUT": 60}):
            gen = PasswordResetTokenGenerator(secret_key="test")
        self.assertEqual(gen.timeout, 60)
        user = _FakeUser()
        # Fresh token should be valid
        token = gen.make_token(user)
        self.assertTrue(gen.check_token(user, token))


# ── Combined settings tests ──────────────────────────────────────────────────


class TestCombinedSettings(unittest.TestCase):
    """Multiple settings interact correctly."""

    def test_domain_and_path_together(self):
        """SESSION_COOKIE_DOMAIN + SESSION_COOKIE_PATH both appear in cookie."""
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_COOKIE_DOMAIN": ".example.com",
                "SESSION_COOKIE_PATH": "/app/",
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
            },
        ):
            auth.login(response, {"username": "admin"})
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Domain=.example.com", cookie)
        self.assertIn("Path=/app/", cookie)
        self.assertIn("Max-Age=3600", cookie)

    def test_expire_at_close_with_domain(self):
        """SESSION_EXPIRE_AT_BROWSER_CLOSE=True + domain = session cookie with domain."""
        from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth

        store = InMemorySessionStore(max_age=3600)
        auth = SessionAuth(secret="test-secret", store=store)
        response = Response.json({"ok": True})
        with unittest.mock.patch.dict(
            DEFAULTS,
            {
                "SESSION_COOKIE_DOMAIN": ".example.com",
                "SESSION_COOKIE_PATH": "/",
                "SESSION_EXPIRE_AT_BROWSER_CLOSE": True,
            },
        ):
            auth.login(response, {"username": "admin"})
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("Domain=.example.com", cookie)
        self.assertNotIn("Max-Age", cookie)

    def test_get_setting_returns_defaults(self):
        """get_setting returns correct defaults for all auth settings."""
        self.assertEqual(get_setting("LOGIN_URL"), "/login/")
        self.assertEqual(get_setting("LOGIN_REDIRECT_URL"), "/")
        self.assertEqual(get_setting("LOGOUT_REDIRECT_URL"), "/")
        self.assertEqual(get_setting("PASSWORD_RESET_TIMEOUT"), 259200)
        self.assertFalse(get_setting("SESSION_EXPIRE_AT_BROWSER_CLOSE"))
        self.assertEqual(get_setting("SESSION_COOKIE_DOMAIN"), "")
        self.assertEqual(get_setting("SESSION_COOKIE_PATH"), "/")
        self.assertFalse(get_setting("SESSION_SAVE_EVERY_REQUEST"))


# ── Runner ───────────────────────────────────────────────────────────────────


class _CheckResult(unittest.TestResult):
    """unittest result adapter — records one harness check per test method.

    The TestCase bodies stay untouched (same assertions, same ordering as
    ``unittest.main()``); only the reporting is routed through the harness so
    the runner sees the ``Results: N passed, M failed`` contract line.
    """

    @staticmethod
    def _name(test: unittest.TestCase) -> str:
        return test.id().removeprefix("__main__.")

    @staticmethod
    def _detail(err: tuple[type[BaseException], BaseException, object]) -> str:
        exc_type, exc, _tb = err
        return f"{exc_type.__name__}: {exc}"

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        check(self._name(test), True)

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        check(self._name(test), False, self._detail(err))

    def addError(self, test, err) -> None:
        super().addError(test, err)
        check(self._name(test), False, self._detail(err))

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        check(f"{self._name(test)} (skipped: {reason})", True)


def main() -> bool:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = _CheckResult()
    result.failfast = False
    suite.run(result)
    for test, tb in result.failures + result.errors:
        print(f"\n--- {_CheckResult._name(test)} ---\n{tb}")
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
