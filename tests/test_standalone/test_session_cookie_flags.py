"""SessionAuth cookie-flag resolution.

Regression for a slop bug: _cookie_kwargs()'s comment claimed the constructor
params self.cookie_httponly / self.cookie_samesite took priority over conf
settings, but those attributes did not exist — httponly/samesite were read from
settings ONLY, ignoring any per-instance intent. The fix adds real
cookie_httponly / cookie_samesite constructor params (None → defer to setting)
so the override story is symmetric with secure_cookie.

Run: uv run pytest tests/test_standalone/test_session_cookie_flags.py -q
"""

from unittest.mock import patch

from hyperdjango.auth.sessions import SessionAuth


def _kwargs(**ctor):
    return SessionAuth(secret="k", **ctor)._cookie_kwargs()


def test_defaults_defer_to_settings():
    # No cookie params → httponly/samesite come from conf defaults.
    k = _kwargs()
    assert k["httponly"] is True  # SESSION_COOKIE_HTTPONLY default
    assert k["samesite"] == "Lax"  # SESSION_COOKIE_SAMESITE default


def test_constructor_httponly_overrides_setting():
    # Explicit False must win even though the setting defaults True.
    k = _kwargs(cookie_httponly=False)
    assert k["httponly"] is False


def test_constructor_samesite_overrides_setting():
    k = _kwargs(cookie_samesite="Strict")
    assert k["samesite"] == "Strict"


def test_none_params_fall_through_to_setting():
    # None (the default) means "use the setting" — proven by flipping the setting.
    with patch("hyperdjango.auth.sessions.get_setting") as gs:
        gs.side_effect = lambda key, *a: {
            "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
            "SESSION_COOKIE_DOMAIN": "",
            "SESSION_COOKIE_PATH": "/",
            "SESSION_COOKIE_SECURE": False,
            "SESSION_COOKIE_HTTPONLY": False,  # atypical value to prove sourcing
            "SESSION_COOKIE_SAMESITE": "Strict",
            "SESSION_COOKIE_NAME": "sid",
            "SESSION_COOKIE_AGE": 3600,
        }.get(key)
        auth = SessionAuth(secret="k")
        k = auth._cookie_kwargs()
    assert k["httponly"] is False
    assert k["samesite"] == "Strict"


def test_secure_is_a_floor():
    # secure_cookie defaults True → Secure regardless of the (False) setting.
    assert _kwargs()["secure"] is True
    # Passing secure_cookie=False defers to the setting (default False → dev HTTP).
    assert _kwargs(secure_cookie=False)["secure"] is False
