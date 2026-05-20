"""
REGRESSION (Round 11): config / crypto hardening + misc resource leaks.

Covers the seven fixes in this batch (pure-Python, native imported via the
built extension; no server is started):

  1. app.oauth2() must NEVER sign session cookies with the source-known
     constant "change-me-in-production". With no secret it fails closed in
     production (raises) and auto-generates a random key + warns in dev.
     (hyperdjango/app.py :: HyperApp.oauth2)
  2. app._validate_production_config(prod=True) raises on an empty SECRET_KEY
     instead of merely warning (fail closed). (hyperdjango/app.py)
  3. checks.check_allowed_hosts resolves via get_setting(), not the bare
     ALLOWED_HOSTS env var. (hyperdjango/checks.py)
  4. signing._decode rejects over-long tokens / signatures BEFORE the pre-auth
     O(N^2) base62 decode; decode_data guards non-dict payloads.
     (hyperdjango/signing.py)
  5. OAuth2.get_tokens evicts expired tokens; logout wires clear_tokens so the
     token store can't grow unbounded.
     (hyperdjango/auth/oauth2.py, hyperdjango/auth/sessions.py)
  6. mail._send_smtp suppresses server.quit() so the real send error surfaces.
     (hyperdjango/mail.py)
  7. test_runner._exec_subprocess kills a still-alive child on the cancellation
     path before untracking it. (verified by source inspection + shape check)

Run: uv run python scripts/test_config_crypto_r11.py
"""

# hyper-test: unit

import asyncio
import json
import time
import traceback
import zlib

import hyperdjango.app as app_mod
import hyperdjango.checks as checks_mod
import hyperdjango.mail as mail_mod
from hyperdjango.app import HyperApp
from hyperdjango.auth.oauth2 import OAuth2, OAuth2Tokens
from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth
from hyperdjango.checks import check_allowed_hosts
from hyperdjango.signing import (
    _MAX_SIG_CHARS,
    _MAX_TOKEN_CHARS,
    _TYPE_DATA,
    SigningKey,
    TokenEngine,
)
from hyperdjango.testkit import check, finish, run_main

_CONSTANT = "change-me-in-production"


class _Recorder:
    """Swap for a logger; records warning() calls."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, template="", *args, **kwargs):
        self.warnings.append(str(kwargs.get("msg", template)))

    # app.py / checks.py loggers only need .warning here, but be permissive.
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _patch_settings(module, overrides: dict):
    """Replace module.get_setting so only the given keys are overridden."""
    original = module.get_setting

    def fake(key, default=None):
        if key in overrides:
            return overrides[key]
        return original(key, default)

    module.get_setting = fake
    return original


# ── Fix #1: app.oauth2() never signs with the known constant ────────────────


def test_oauth2_secret_hardening() -> None:
    # --- Production (debug=False), no secret, empty settings -> RAISE --------
    app = HyperApp()
    app.debug = False
    orig_gs = _patch_settings(app_mod, {"SESSION_SECRET": "", "SECRET_KEY": ""})
    orig_logger = app_mod.logger
    rec = _Recorder()
    app_mod.logger = rec
    try:
        raised = False
        try:
            app.oauth2([])  # no secret
        except ValueError as e:
            raised = True
            assert "secret" in str(e).lower(), f"unexpected message: {e}"
        assert raised, "oauth2() with no secret must FAIL CLOSED in production"
    finally:
        app_mod.get_setting = orig_gs
        app_mod.logger = orig_logger

    # --- Dev (debug=True), no secret, empty settings -> random key + warn ----
    app2 = HyperApp()
    app2.debug = True
    orig_gs = _patch_settings(app_mod, {"SESSION_SECRET": "", "SECRET_KEY": ""})
    orig_logger = app_mod.logger
    rec = _Recorder()
    app_mod.logger = rec
    try:
        oauth = app2.oauth2([])
        assert oauth.secret, "dev fallback must produce a non-empty secret"
        assert oauth.secret != _CONSTANT, (
            "SECURITY: session cookies signed with the source-known constant!"
        )
        assert len(oauth.secret) >= 20, "auto-generated key should be substantial"
        assert rec.warnings, "dev fallback must warn loudly"
        # The SessionAuth it wired must use the SAME resolved secret (not the
        # constant) so cookies are actually signed with the random key.
        assert oauth._session_auth is not None
        assert oauth._session_auth.secret == oauth.secret
    finally:
        app_mod.get_setting = orig_gs
        app_mod.logger = orig_logger

    # --- Explicit secret is honored verbatim --------------------------------
    app3 = HyperApp()
    app3.debug = True
    oauth3 = app3.oauth2([], secret="my-explicit-secret")
    assert oauth3.secret == "my-explicit-secret"

    # The literal constant must be gone from the source entirely.
    import inspect

    src = inspect.getsource(HyperApp.oauth2)
    assert _CONSTANT not in src, "source still references the insecure constant"


# ── Fix #2: _validate_production_config fails closed on empty SECRET_KEY ─────


def test_validate_production_fail_closed() -> None:
    app = HyperApp()
    app.secret_key = ""
    orig_logger = app_mod.logger
    app_mod.logger = _Recorder()
    try:
        # prod=True + empty key -> raise
        raised = False
        try:
            app._validate_production_config(prod=True)
        except RuntimeError as e:
            raised = True
            assert "SECRET_KEY" in str(e)
        assert raised, "prod=True with empty SECRET_KEY must raise (fail closed)"

        # Not prod -> only warns, no raise
        app._validate_production_config(prod=False)  # must not raise

        # prod=True but key present -> no raise
        app.secret_key = "a-real-secret-key"
        app._validate_production_config(prod=True)
    finally:
        app_mod.logger = orig_logger


# ── Fix #3: check_allowed_hosts reads get_setting, not os.environ ───────────


def test_check_allowed_hosts_reads_get_setting() -> None:
    import os

    app = HyperApp()

    # get_setting resolves a host -> PASS (no message), even though the bare
    # env var is empty. Proves the check no longer reads os.environ.
    os.environ.pop("ALLOWED_HOSTS", None)
    orig = _patch_settings(checks_mod, {"ALLOWED_HOSTS": ["example.com"]})
    try:
        msgs = check_allowed_hosts(app)
        assert msgs == [], f"configured hosts via get_setting should pass: {msgs}"
    finally:
        checks_mod.get_setting = orig

    # A bare env var the framework ignores must NOT be treated as configured
    # (no false PASS): get_setting empty -> warn even with env var set.
    os.environ["ALLOWED_HOSTS"] = "sneaky.example.com"
    orig = _patch_settings(checks_mod, {"ALLOWED_HOSTS": []})
    try:
        msgs = check_allowed_hosts(app)
        assert len(msgs) == 1 and msgs[0].id == "deployment.W001", (
            f"bare env var must not count as configured: {msgs}"
        )
    finally:
        checks_mod.get_setting = orig
        os.environ.pop("ALLOWED_HOSTS", None)

    # Fallback to app.allowed_hosts when the setting is empty.
    app.allowed_hosts = ["from-app.example.com"]
    orig = _patch_settings(checks_mod, {"ALLOWED_HOSTS": []})
    try:
        msgs = check_allowed_hosts(app)
        assert msgs == [], f"app.allowed_hosts fallback should pass: {msgs}"
    finally:
        checks_mod.get_setting = orig


# ── Fix #4: over-long token rejected BEFORE the pre-auth O(N^2) decode ───────


def test_signing_overlong_rejected_before_decode() -> None:
    import hyperdjango.signing as signing_mod

    engine = TokenEngine(keys=[SigningKey(secret="k", version=1)])

    # Round-trips a real token (sanity).
    tok = engine.encode_data({"a": 1})
    assert engine.decode_data(tok) == {"a": 1}

    # Instrument the expensive decoder: it must NOT run for over-long input.
    orig_decode = signing_mod._base62_to_bytes
    calls = {"n": 0}

    def counting(code):
        calls["n"] += 1
        return orig_decode(code)

    signing_mod._base62_to_bytes = counting
    try:
        # (a) whole token over the char cap
        huge = "1d" + ("A" * (_MAX_TOKEN_CHARS + 10)) + "." + ("A" * 12)
        calls["n"] = 0
        assert engine._decode(huge, _TYPE_DATA) is None
        assert calls["n"] == 0, "over-long token reached the O(N^2) decoder"

        # (b) token short overall but signature part over the sig cap
        big_sig = "1d" + "AAAA" + "." + ("A" * (_MAX_SIG_CHARS + 5))
        assert len(big_sig) < _MAX_TOKEN_CHARS
        calls["n"] = 0
        assert engine._decode(big_sig, _TYPE_DATA) is None
        assert calls["n"] == 0, "over-long signature reached the O(N^2) decoder"

        # (c) a normal token still decodes (decoder IS called).
        calls["n"] = 0
        assert engine._decode(tok, _TYPE_DATA) is not None
        assert calls["n"] >= 1, "legitimate token must still be decoded"
    finally:
        signing_mod._base62_to_bytes = orig_decode

    # Timing sanity: a 64 KB junk token returns fast (was ~500ms via O(N^2)).
    junk = "1d" + ("Z" * 65536) + ".Zzzzzzzz"
    start = time.perf_counter()
    for _ in range(50):
        assert engine._decode(junk, _TYPE_DATA) is None
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, f"over-long rejection too slow: {elapsed_ms:.1f}ms/50"

    # decode_data dict guard: a validly-signed NON-dict payload -> None, not raise.
    payload = zlib.compress(json.dumps([1, 2, 3]).encode("utf-8"))
    list_token = engine._encode(_TYPE_DATA, payload)
    assert engine.decode_data(list_token) is None, "non-dict payload must return None"


# ── Fix #5: token store eviction + logout clears tokens ─────────────────────


def _mk_tokens(expires_at: float) -> OAuth2Tokens:
    return OAuth2Tokens(
        access_token="at",
        refresh_token="rt",
        expires_at=expires_at,
        provider="google",
        scopes=["email"],
    )


def test_oauth2_token_store_eviction_and_logout() -> None:
    oauth = OAuth2(secret="s")

    # get_tokens sweeps expired entries out of the store.
    oauth.store_tokens("live", _mk_tokens(time.time() + 3600))
    oauth.store_tokens("dead1", _mk_tokens(time.time() - 10))
    oauth.store_tokens("dead2", _mk_tokens(time.time() - 10))
    assert oauth.get_tokens("dead1") is None
    # The sweep triggered by looking up an expired entry drops ALL expired ones.
    assert "dead1" not in oauth._token_store
    assert "dead2" not in oauth._token_store
    assert "live" in oauth._token_store, "live token must be retained"
    assert oauth.get_tokens("live") is not None

    # logout() wires clear_tokens via the SessionAuth back-reference.
    store = InMemorySessionStore()
    auth = SessionAuth(secret="s2", store=store)
    assert auth._oauth is None
    oauth.set_session_auth(auth)
    assert auth._oauth is oauth, "set_session_auth must back-reference for logout"

    oauth.store_tokens("sid-1", _mk_tokens(time.time() + 3600))

    class _Resp:
        def delete_cookie(self, *a, **k):
            pass

    auth.logout(_Resp(), "sid-1")
    assert "sid-1" not in oauth._token_store, "sync logout must clear tokens"

    oauth.store_tokens("sid-2", _mk_tokens(time.time() + 3600))
    asyncio.run(auth.logout_async(_Resp(), "sid-2"))
    assert "sid-2" not in oauth._token_store, "async logout must clear tokens"


# ── Fix #6: mail server.quit() is suppressed so the real error surfaces ─────


def test_mail_quit_suppressed() -> None:
    import smtplib

    class _FakeServer:
        def __init__(self, *a, **k):
            pass

        def sendmail(self, *a, **k):
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")

        def quit(self):
            # Mask attempt: quit() raises on an already-dropped connection.
            raise smtplib.SMTPServerDisconnected("connection lost")

        def login(self, *a, **k):
            pass

        def starttls(self, *a, **k):
            pass

    orig_smtp = mail_mod.smtplib.SMTP
    orig_logger = mail_mod.logger
    rec = _Recorder()
    errors: list[str] = []
    rec.error = lambda msg, *a, **k: errors.append(str(msg))
    mail_mod.smtplib.SMTP = _FakeServer
    mail_mod.logger = rec
    try:
        m = mail_mod.EmailMessage(
            subject="s",
            body="b",
            recipients=["x@example.com"],
            from_email="f@example.com",
        )

        class _Cfg:
            backend = "smtp"
            host = "localhost"
            port = 25
            username = ""
            password = ""
            use_tls = False
            use_ssl = False
            timeout = 5

        ok = m._send_smtp(_Cfg(), "f@example.com")
        assert ok is False, "send must report failure"
        # The REAL error (auth), not the quit() disconnect, must be logged.
        assert errors, "the true send error must be logged"
        joined = " ".join(errors)
        assert "SMTPAuthenticationError" in joined or "535" in joined, (
            f"quit() masked the real error; logged: {joined!r}"
        )
    finally:
        mail_mod.smtplib.SMTP = orig_smtp
        mail_mod.logger = orig_logger


# ── Fix #7: subprocess killed on cancellation before untracking (shape) ─────


def test_exec_subprocess_kills_before_discard() -> None:
    import inspect

    from hyperdjango import test_runner

    src = inspect.getsource(test_runner._exec_subprocess)
    # The inner finally must kill a still-alive child before discarding it.
    finally_idx = src.index("finally:")
    discard_idx = src.index("_active_procs.discard(proc)", finally_idx)
    between = src[finally_idx:discard_idx]
    assert "proc.returncode is None" in between, (
        "must check the child is still alive in the finally"
    )
    assert "proc.kill()" in between, (
        "must kill the still-alive child BEFORE discarding it from _active_procs"
    )


# ── Runner ───────────────────────────────────────────────────────────────────

# Each entry is one counted check: the assert battery inside the function is the
# check body, so a raised AssertionError aborts the run exactly as before — the
# only difference is that the tally is emitted before exiting.
_TESTS = [
    (
        test_oauth2_secret_hardening,
        "#1 oauth2() fails closed in prod, random+warn in dev, never the constant",
    ),
    (
        test_validate_production_fail_closed,
        "#2 _validate_production_config raises on empty SECRET_KEY when prod=True",
    ),
    (
        test_check_allowed_hosts_reads_get_setting,
        "#3 check_allowed_hosts resolves via get_setting + app.allowed_hosts",
    ),
    (
        test_signing_overlong_rejected_before_decode,
        "#4 over-long token/sig rejected pre-decode; non-dict payload guarded",
    ),
    (
        test_oauth2_token_store_eviction_and_logout,
        "#5 token store evicts expired + logout clears tokens (sync & async)",
    ),
    (
        test_mail_quit_suppressed,
        "#6 server.quit() suppressed; the real send error surfaces",
    ),
    (
        test_exec_subprocess_kills_before_discard,
        "#7 _exec_subprocess kills a live child before untracking (no orphan)",
    ),
]


def main() -> bool:
    for fn, label in _TESTS:
        try:
            fn()
        except Exception as exc:
            check(label, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
        check(label, True)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
