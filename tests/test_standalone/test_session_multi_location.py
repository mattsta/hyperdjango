"""SessionAuth.login() must preserve multi-location login.

login() rotates ONLY the session id presented in the current request's cookie
(the session-fixation defense). It must NOT invalidate the user's *other*
sessions — a phone stays logged in when the laptop re-authenticates. Enforcing
single-point-of-login is a per-application concern, not a framework default.
This test locks that scope so a future change can't silently turn login() into
a user-wide session purge.

Run: uv run pytest tests/test_standalone/test_session_multi_location.py -q
"""

from types import SimpleNamespace

from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth


class _Resp:
    def __init__(self):
        self.headers = {}

    def set_cookie(self, *a, **k):  # noqa: D401 - test double
        pass


def _login_request(auth, sid):
    """A request carrying the signed cookie for `sid`, with the attrs login() sets."""
    return SimpleNamespace(
        cookies={auth.cookie_name: auth._sign_session_id(sid)},
        headers={},
        user=None,
        session=None,
        session_id=None,
    )


def test_login_preserves_other_devices():
    store = InMemorySessionStore()
    auth = SessionAuth(secret="k", store=store)

    # Same user, two devices → two independent session ids.
    laptop = store.create({"user_id": 42})
    phone = store.create({"user_id": 42})
    assert laptop != phone

    # Re-login on the laptop (its cookie is presented on the request).
    new_laptop = auth.login(
        _Resp(), {"user_id": 42}, request=_login_request(auth, laptop)
    )

    assert store.get(laptop) is None, (
        "presented session should be rotated (fixation defense)"
    )
    assert new_laptop != laptop and store.get(new_laptop) is not None, (
        "fresh laptop session"
    )
    assert store.get(phone) is not None, (
        "the phone's session must survive the laptop login"
    )


def test_login_from_fresh_browser_deletes_nothing():
    store = InMemorySessionStore()
    auth = SessionAuth(secret="k", store=store)

    existing = store.create({"user_id": 7})
    # A brand-new browser has no session cookie → nothing to rotate.
    req = SimpleNamespace(
        cookies={}, headers={}, user=None, session=None, session_id=None
    )
    new_sid = auth.login(_Resp(), {"user_id": 7}, request=req)

    assert store.get(existing) is not None, "unrelated session must not be touched"
    assert new_sid != existing and store.get(new_sid) is not None
