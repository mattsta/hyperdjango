"""Constructor kwargs / settings that used to be silently INERT (fix wave).

Several ``HyperApp(...)`` constructor arguments were stored only on the instance
and never bridged into the settings layer that the rest of the framework reads
via ``get_setting(...)``. The result was security-relevant no-ops: a
constructor ``secret_key`` left every signer (sessions, CSRF, password-reset,
versioning HMAC) effectively keyless, and ``allowed_hosts`` left Host-header
validation disabled — while the production guards, which checked the populated
instance attribute, falsely passed.

This test pins the bridges so they can't silently regress:

  * ``HyperApp(secret_key=...)`` reaches ``get_setting("SECRET_KEY")`` and a real
    signer (the password-reset token generator) picks it up.
  * ``HyperApp(allowed_hosts=...)`` reaches ``get_setting("ALLOWED_HOSTS")``.
  * ``FILE_ROUTING_DIR`` is honored by ``discover_routes()`` instead of the
    hardcoded ``"views"`` literal.
  * ``app.run()`` HOST/PORT fall back to the setting (explicit arg > setting >
    literal default).
  * ``@app.middleware`` registered after the handler chain was built invalidates
    the cached chain (parity with ``app.use()``).

Global DEFAULTS / env-cache state is snapshotted and restored around every
assertion so the checks don't leak settings into one another.

Run:  uv run hyper-test inert_app_config
"""

# hyper-test: unit

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from hyperdjango import HyperApp
from hyperdjango.conf import DEFAULTS, clear_settings_cache, get_setting

_PASS = 0
_FAIL = 0

# Settings whose env overrides would shadow the DEFAULTS bridges we test.
_ENV_KEYS = (
    "HYPER_SECRET_KEY",
    "HYPER_ALLOWED_HOSTS",
    "HYPER_HOST",
    "HYPER_PORT",
    "HYPER_FILE_ROUTING_DIR",
)


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


@contextmanager
def isolated_settings(*keys: str):
    """Snapshot/restore the given DEFAULTS keys + relevant env vars.

    Ensures each assertion starts from a clean settings state and never leaks a
    bridged value (or an ambient HYPER_* env override) into the next one.
    """
    saved_defaults = {k: DEFAULTS.get(k, _MISSING) for k in keys}
    saved_env = {k: os.environ.pop(k, _MISSING) for k in _ENV_KEYS}
    clear_settings_cache()
    try:
        yield
    finally:
        for k, v in saved_defaults.items():
            if v is _MISSING:
                DEFAULTS.pop(k, None)
            else:
                DEFAULTS[k] = v
        for k, v in saved_env.items():
            if v is not _MISSING:
                os.environ[k] = v
        clear_settings_cache()


_MISSING = object()


# ── secret_key bridge (SECURITY) ────────────────────────────────────────────


def test_secret_key_reaches_get_setting() -> None:
    print("\n=== HyperApp(secret_key=...) bridges into settings ===")
    with isolated_settings("SECRET_KEY"):
        app = HyperApp(secret_key="test-secret-key-abc123")
        check(
            "get_setting('SECRET_KEY') sees the constructor key",
            get_setting("SECRET_KEY") == "test-secret-key-abc123",
            repr(get_setting("SECRET_KEY")),
        )
        check(
            "self.secret_key reflects what signers see (prod guard honest)",
            app.secret_key == "test-secret-key-abc123",
            repr(app.secret_key),
        )


def test_secret_key_used_by_signer() -> None:
    print("\n=== A real signer picks up the bridged SECRET_KEY ===")
    with isolated_settings("SECRET_KEY"):
        HyperApp(secret_key="test-secret-key-abc123")
        # The password-reset token generator resolves secret_key=None to
        # get_setting("SECRET_KEY"). Reset its module-global cache first so we
        # observe a freshly-resolved generator, not a stale one.
        import hyperdjango.auth.password_reset as pr

        pr._default_generator = None
        gen = pr.get_token_generator()
        check(
            "password-reset token generator signs with the bridged key",
            gen.secret_key == "test-secret-key-abc123",
            repr(gen.secret_key),
        )
        pr._default_generator = None


def test_secret_key_empty_when_not_passed() -> None:
    print("\n=== No secret_key leaves the empty default (guard fails closed) ===")
    with isolated_settings("SECRET_KEY"):
        DEFAULTS["SECRET_KEY"] = ""  # simulate the pristine default
        clear_settings_cache()
        app = HyperApp()  # no secret_key
        check(
            "self.secret_key stays empty so the prod guard can refuse",
            not app.secret_key,
            repr(app.secret_key),
        )


# ── allowed_hosts bridge (SECURITY) ─────────────────────────────────────────


def test_allowed_hosts_reaches_get_setting() -> None:
    print("\n=== HyperApp(allowed_hosts=...) bridges into settings ===")
    with isolated_settings("ALLOWED_HOSTS"):
        app = HyperApp(allowed_hosts=["example.com", "www.example.com"])
        check(
            "get_setting('ALLOWED_HOSTS') sees the constructor list",
            get_setting("ALLOWED_HOSTS") == ["example.com", "www.example.com"],
            repr(get_setting("ALLOWED_HOSTS")),
        )
        check(
            "self.allowed_hosts matches (prod warning honest)",
            app.allowed_hosts == ["example.com", "www.example.com"],
            repr(app.allowed_hosts),
        )


# ── FILE_ROUTING_DIR honored by discover_routes ─────────────────────────────


def test_file_routing_dir_honored() -> None:
    print("\n=== discover_routes() honors FILE_ROUTING_DIR ===")
    with isolated_settings("FILE_ROUTING_DIR"), tempfile.TemporaryDirectory() as tmp:
        views_dir = Path(tmp) / "custom_views"
        views_dir.mkdir()
        (views_dir / "index.py").write_text(
            "from hyperdjango import Response\n\n"
            "async def get(request):\n"
            "    return Response.html('hi')\n"
        )
        DEFAULTS["FILE_ROUTING_DIR"] = str(views_dir)
        clear_settings_cache()

        # views=None so neither the arg nor self.views_dir short-circuits; the
        # setting must supply the directory (previously a hardcoded "views").
        app = HyperApp(views=None)
        app.discover_routes()
        patterns = {(r.method, r.pattern) for r in app.router.routes()}
        check(
            "route from FILE_ROUTING_DIR directory got registered",
            ("GET", "/") in patterns,
            repr(patterns),
        )


def test_file_routing_dir_default_literal_preserved() -> None:
    print("\n=== FILE_ROUTING_DIR default is still 'views' ===")
    with isolated_settings("FILE_ROUTING_DIR"):
        check(
            "get_setting('FILE_ROUTING_DIR') default is 'views'",
            get_setting("FILE_ROUTING_DIR") == "views",
            repr(get_setting("FILE_ROUTING_DIR")),
        )


# ── HOST/PORT fallback in app.run() ─────────────────────────────────────────


def _run_capture(app):
    """Call app.run() with _run_native stubbed, capturing the resolved bind."""
    captured = {}

    def _fake_native(host, port):
        captured["host"] = host
        captured["port"] = port

    app._run_native = _fake_native
    app.debug = True  # skip production validation
    return captured


def test_host_port_fallback_to_setting() -> None:
    print("\n=== app.run() HOST/PORT fall back to the setting ===")
    with isolated_settings("HOST", "PORT"):
        DEFAULTS["HOST"] = "0.0.0.0"
        DEFAULTS["PORT"] = 9999
        clear_settings_cache()
        app = HyperApp()
        captured = _run_capture(app)
        app.run()  # no host/port args
        check(
            "unspecified host resolves to HOST setting",
            captured.get("host") == "0.0.0.0",
            repr(captured.get("host")),
        )
        check(
            "unspecified port resolves to PORT setting",
            captured.get("port") == 9999,
            repr(captured.get("port")),
        )


def test_host_port_explicit_arg_wins() -> None:
    print("\n=== explicit run() args win over the setting ===")
    with isolated_settings("HOST", "PORT"):
        DEFAULTS["HOST"] = "0.0.0.0"
        DEFAULTS["PORT"] = 9999
        clear_settings_cache()
        app = HyperApp()
        captured = _run_capture(app)
        app.run(host="10.0.0.5", port=3000)
        check(
            "explicit host overrides the setting",
            captured.get("host") == "10.0.0.5",
            repr(captured.get("host")),
        )
        check(
            "explicit port overrides the setting",
            captured.get("port") == 3000,
            repr(captured.get("port")),
        )


def test_host_port_literal_default() -> None:
    print("\n=== HOST/PORT literal defaults preserved when unset ===")
    with isolated_settings("HOST", "PORT"):
        # Rely on the shipped DEFAULTS (127.0.0.1 / 8000); don't override.
        clear_settings_cache()
        app = HyperApp()
        captured = _run_capture(app)
        app.run()
        check(
            "default host is 127.0.0.1",
            captured.get("host") == "127.0.0.1",
            repr(captured.get("host")),
        )
        check(
            "default port is 8000",
            captured.get("port") == 8000,
            repr(captured.get("port")),
        )


# ── @app.middleware invalidates the cached handler chain ────────────────────


def test_middleware_decorator_invalidates_cache() -> None:
    print("\n=== @app.middleware invalidates the cached handler chain ===")
    app = HyperApp()

    # Build the chain (simulates first request / post-startup state).
    app._cached_handler = app._middleware.wrap(app._guarded_dispatch)
    check(
        "handler chain is cached before registration",
        app._cached_handler is not None,
    )

    @app.middleware
    async def _late_mw(request, call_next):
        return await call_next(request)

    check(
        "@app.middleware cleared the cached chain (won't drop the middleware)",
        app._cached_handler is None,
    )


def test_use_still_invalidates_cache() -> None:
    print("\n=== app.use() still invalidates (regression guard) ===")
    app = HyperApp()
    app._cached_handler = app._middleware.wrap(app._guarded_dispatch)

    async def _mw(request, call_next):
        return await call_next(request)

    app.use(_mw)
    check(
        "app.use() cleared the cached chain",
        app._cached_handler is None,
    )


def run() -> bool:
    test_secret_key_reaches_get_setting()
    test_secret_key_used_by_signer()
    test_secret_key_empty_when_not_passed()
    test_allowed_hosts_reaches_get_setting()
    test_file_routing_dir_honored()
    test_file_routing_dir_default_literal_preserved()
    test_host_port_fallback_to_setting()
    test_host_port_explicit_arg_wins()
    test_host_port_literal_default()
    test_middleware_decorator_invalidates_cache()
    test_use_still_invalidates_cache()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
