"""
Unit tests for the mTLS wiring helpers added to hyperdjango.mtls:
the process-level attestation registry and MTLSTerminator.from_config.

# hyper-test: unit

Proves:
  - a started terminator registers its attestation secret, so
    resolve_client_cert accepts that terminator's injected identity; stop()
    deregisters it and the same headers then resolve to None
  - precedence is registry > MTLS_PROXY_SECRET setting
  - a MTLS_PROXY_SECRET below the 32-char floor is refused (fail closed)
  - from_config returns None when mTLS is disabled (no port / no cert) and a
    started terminator when enabled
  - from_config REQUIRES upstream_port and wires it straight onto the
    terminator (the value the caller passes from the app's real bound port)
  - install() registers the startup/shutdown hooks and readiness check on an
    app object and drives the terminator lifecycle (app-agnostic)
  - install() (trust_upstream_ip default) makes the loopback upstream a trusted
    proxy so client_ip reflects the injected X-Real-IP; =False leaves it untouched
  - install() fails loudly at startup when mTLS is enabled but app.bound_port is
    0 (ephemeral bind), and stays quiet when mTLS is disabled
"""

import asyncio
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import hyperdjango.mtls as mtls_mod  # noqa: E402
from hyperdjango.mtls import (  # noqa: E402
    ATTEST_HEADER,
    CN_HEADER,
    FINGERPRINT_HEADER,
    MTLSTerminator,
    create_ca,
    issue_cert,
    resolve_client_cert,
    write_pem,
)

SCRATCH = PROJECT_ROOT / ".test_scratch" / "mtls_wiring"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


class _FakeHeaders:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key: str, default: str = "") -> str:
        return self._data.get(key, default)


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


class _FakeApp:
    """Minimal stand-in for the framework app: the exact surface install() uses
    (on_startup / on_shutdown decorators, add_health_check, bound_port)."""

    def __init__(self, bound_port: int):
        self.bound_port = bound_port
        self.startup_hooks: list = []
        self.shutdown_hooks: list = []
        self.health_checks: dict = {}

    def on_startup(self, func):
        self.startup_hooks.append(func)
        return func

    def on_shutdown(self, func):
        self.shutdown_hooks.append(func)
        return func

    def add_health_check(self, name: str, check):
        self.health_checks[name] = check

    async def run_startup(self) -> None:
        for f in self.startup_hooks:
            await f()

    async def run_shutdown(self) -> None:
        for f in self.shutdown_hooks:
            await f()


def _write_certs():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = create_ca("wiring-ca")
    srv_key, srv_cert = issue_cert(
        ca_key, ca_cert, "localhost", server=True, san_dns=["localhost", "127.0.0.1"]
    )
    for name, data, priv in (
        ("ca.crt", ca_cert, False),
        ("srv.crt", srv_cert, False),
        ("srv.key", srv_key, True),
    ):
        path = SCRATCH / name
        if path.exists():
            path.unlink()
        write_pem(str(path), data, private=priv)


def _terminator(**overrides) -> MTLSTerminator:
    kwargs = dict(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=1,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    kwargs.update(overrides)
    return MTLSTerminator(**kwargs)


def _free_port() -> int:
    """A currently-free TCP port. from_config treats listen_port=0 as DISABLED
    (the apps' convention), so the enabled path needs a real non-zero port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req_for(secret: str) -> _FakeRequest:
    return _FakeRequest(
        {
            ATTEST_HEADER: secret,
            CN_HEADER: "service:prod-api",
            FINGERPRINT_HEADER: "deadbeef",
        }
    )


def test_registry_registration_and_deregistration():
    print("\n== start() registers, stop() deregisters ==")
    _write_certs()
    term = _terminator()
    req = _req_for(term.attestation_secret)

    # Before start: nothing registered → the terminator's secret is not honored.
    check(
        "unregistered attestation resolves to None",
        resolve_client_cert(req) is None,
    )
    term.start()
    try:
        ident = resolve_client_cert(req)  # no terminator_secret argument
        check(
            "registered terminator identity resolves via the registry",
            ident is not None and ident.common_name == "service:prod-api",
        )
    finally:
        term.stop()
    check(
        "after stop() the same headers resolve to None",
        resolve_client_cert(req) is None,
    )


def test_precedence_registry_setting():
    print("\n== precedence: registry > setting ==")
    _write_certs()

    # 1. Registry: a running terminator's secret resolves — it self-registered
    # on start(), so no secret is hand-carried into resolve_client_cert.
    term = _terminator()
    term.start()
    try:
        check(
            "registry secret resolves",
            resolve_client_cert(_req_for(term.attestation_secret)) is not None,
        )
    finally:
        term.stop()

    # 2. Setting: MTLS_PROXY_SECRET is honored when the registry has no match.
    # Patch the module's get_setting for a hermetic check.
    proxy_secret = "x" * 64
    orig = mtls_mod.get_setting

    def fake_get_setting(name, default=None):
        if name == "MTLS_PROXY_SECRET":
            return proxy_secret
        return orig(name, default)

    mtls_mod.get_setting = fake_get_setting
    try:
        check(
            "proxy-secret setting resolves as the last source",
            resolve_client_cert(_req_for(proxy_secret)) is not None,
        )
        check(
            "an attestation matching no source still resolves to None",
            resolve_client_cert(_req_for("z" * 64)) is None,
        )
    finally:
        mtls_mod.get_setting = orig


def test_proxy_secret_min_length():
    print("\n== MTLS_PROXY_SECRET below the 32-char floor is refused ==")
    _write_certs()
    from hyperdjango.mtls import _MIN_PROXY_SECRET_LEN

    short = "x" * (_MIN_PROXY_SECRET_LEN - 1)
    floor = "y" * _MIN_PROXY_SECRET_LEN
    orig = mtls_mod.get_setting
    # Reset the log-once flag so this test's short secret is honestly evaluated.
    mtls_mod._short_proxy_secret_logged = False

    def with_secret(value):
        def fake(name, default=None):
            if name == "MTLS_PROXY_SECRET":
                return value
            return orig(name, default)

        return fake

    mtls_mod.get_setting = with_secret(short)
    try:
        check(
            "a too-short proxy secret does not attest (fail closed)",
            resolve_client_cert(_req_for(short)) is None,
        )
    finally:
        mtls_mod.get_setting = orig

    mtls_mod.get_setting = with_secret(floor)
    try:
        check(
            "a proxy secret at the 32-char floor attests",
            resolve_client_cert(_req_for(floor)) is not None,
        )
    finally:
        mtls_mod.get_setting = orig


def test_install_wires_lifecycle():
    print("\n== install() wires startup/shutdown/health in one call ==")
    _write_certs()
    app = _FakeApp(bound_port=23456)
    handle = MTLSTerminator.install(
        app,
        listen_port=_free_port(),
        cert_file=str(SCRATCH / "srv.crt"),
        key_file=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        listen_host="127.0.0.1",
    )
    check("one startup hook registered", len(app.startup_hooks) == 1)
    check("one shutdown hook registered", len(app.shutdown_hooks) == 1)
    check("readiness check registered", "mtls_terminator" in app.health_checks)
    hc = app.health_checks["mtls_terminator"]
    check("handle.terminator is None before startup", handle.terminator is None)
    check("readiness healthy before startup (None → disabled)", hc() is True)

    asyncio.run(app.run_startup())
    try:
        check("startup built and started the terminator", handle.terminator is not None)
        check("terminator alive after startup", handle.terminator.is_alive())
        check("readiness healthy while the front door is alive", hc() is True)
        check(
            "upstream_port wired from app.bound_port",
            handle.terminator.upstream_port == app.bound_port,
        )
        check(
            "started terminator self-registered its attestation",
            resolve_client_cert(_req_for(handle.terminator.attestation_secret))
            is not None,
        )
    finally:
        asyncio.run(app.run_shutdown())
    check("terminator stopped after shutdown", not handle.terminator.is_alive())
    check(
        "attestation deregistered after shutdown",
        resolve_client_cert(_req_for(handle.terminator.attestation_secret)) is None,
    )


def test_install_trusts_loopback_upstream():
    print("\n== install() makes the loopback upstream a trusted proxy for client_ip ==")
    from hyperdjango.client_ip import resolve_client_ip
    from hyperdjango.conf import DEFAULTS

    _write_certs()
    saved = list(DEFAULTS.get("TRUSTED_PROXIES") or [])
    try:
        # Baseline: with the loopback NOT trusted, an injected X-Real-IP is
        # ignored and client_ip falls back to the loopback peer — the collapse
        # that defeats per-IP rate limiting behind the in-process terminator.
        DEFAULTS["TRUSTED_PROXIES"] = []
        check(
            "baseline: loopback peer's X-Real-IP not honored",
            resolve_client_ip("127.0.0.1", None, "203.0.113.7") == "127.0.0.1",
        )

        app = _FakeApp(bound_port=23456)
        MTLSTerminator.install(
            app,
            listen_port=_free_port(),
            cert_file=str(SCRATCH / "srv.crt"),
            key_file=str(SCRATCH / "srv.key"),
            ca_file=str(SCRATCH / "ca.crt"),
            listen_host="127.0.0.1",
            upstream_host="127.0.0.1",
        )
        asyncio.run(app.run_startup())
        try:
            check(
                "after install+startup the loopback upstream is trusted",
                "127.0.0.1" in (DEFAULTS.get("TRUSTED_PROXIES") or []),
            )
            check(
                "client_ip now reflects the injected X-Real-IP",
                resolve_client_ip("127.0.0.1", None, "203.0.113.7") == "203.0.113.7",
            )
        finally:
            asyncio.run(app.run_shutdown())
    finally:
        DEFAULTS["TRUSTED_PROXIES"] = saved


def test_install_trust_upstream_ip_false_leaves_untouched():
    print("\n== install(trust_upstream_ip=False) leaves proxy trust untouched ==")
    from hyperdjango.client_ip import resolve_client_ip
    from hyperdjango.conf import DEFAULTS

    _write_certs()
    saved = list(DEFAULTS.get("TRUSTED_PROXIES") or [])
    try:
        DEFAULTS["TRUSTED_PROXIES"] = []
        app = _FakeApp(bound_port=23456)
        MTLSTerminator.install(
            app,
            listen_port=_free_port(),
            cert_file=str(SCRATCH / "srv.crt"),
            key_file=str(SCRATCH / "srv.key"),
            ca_file=str(SCRATCH / "ca.crt"),
            listen_host="127.0.0.1",
            upstream_host="127.0.0.1",
            trust_upstream_ip=False,
        )
        asyncio.run(app.run_startup())
        try:
            check(
                "trust_upstream_ip=False leaves TRUSTED_PROXIES empty",
                (DEFAULTS.get("TRUSTED_PROXIES") or []) == [],
            )
            check(
                "client_ip still falls back to the loopback peer",
                resolve_client_ip("127.0.0.1", None, "203.0.113.7") == "127.0.0.1",
            )
        finally:
            asyncio.run(app.run_shutdown())
    finally:
        DEFAULTS["TRUSTED_PROXIES"] = saved


def test_install_fails_loud_on_ephemeral_bound_port():
    print("\n== install() fails loudly when the app bound an ephemeral port ==")
    # PORT=0 → the native server assigns a real port but exposes no accessor, so
    # app.bound_port publishes 0. install() consumes bound_port as the upstream,
    # so an enabled terminator must fail loudly rather than forward to port 0.
    _write_certs()
    app = _FakeApp(bound_port=0)
    MTLSTerminator.install(
        app,
        listen_port=_free_port(),
        cert_file=str(SCRATCH / "srv.crt"),
        key_file=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        listen_host="127.0.0.1",
    )
    raised = False
    try:
        asyncio.run(app.run_startup())
    except Exception:  # noqa: BLE001 - the startup hook must fail loudly
        raised = True
    check("enabled mTLS + bound_port 0 fails loudly at startup", raised)

    # Disabled mTLS needs no upstream, so bound_port 0 must NOT fail.
    app2 = _FakeApp(bound_port=0)
    handle = MTLSTerminator.install(
        app2,
        listen_port=0,  # disabled
        cert_file=str(SCRATCH / "srv.crt"),
        key_file=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    ok = True
    try:
        asyncio.run(app2.run_startup())
    except Exception:  # noqa: BLE001
        ok = False
    check(
        "disabled mTLS + bound_port 0 does not fail",
        ok and handle.terminator is None,
    )


def test_loopback_trust_refcounted():
    print("\n== loopback trust is refcounted across concurrent installs ==")
    from hyperdjango.conf import DEFAULTS

    _write_certs()
    saved = list(DEFAULTS.get("TRUSTED_PROXIES") or [])
    saved_refs = dict(mtls_mod._LOOPBACK_TRUST_REFCOUNT)
    handles: list = []
    try:
        DEFAULTS["TRUSTED_PROXIES"] = []
        mtls_mod._LOOPBACK_TRUST_REFCOUNT.clear()

        # Two terminators installed for the SAME loopback upstream.
        for bp in (23456, 23457):
            app = _FakeApp(bound_port=bp)
            handle = MTLSTerminator.install(
                app,
                listen_port=_free_port(),
                cert_file=str(SCRATCH / "srv.crt"),
                key_file=str(SCRATCH / "srv.key"),
                ca_file=str(SCRATCH / "ca.crt"),
                listen_host="127.0.0.1",
                upstream_host="127.0.0.1",
            )
            asyncio.run(app.run_startup())
            handles.append((app, handle))

        check(
            "both installs trust the loopback upstream",
            "127.0.0.1" in (DEFAULTS.get("TRUSTED_PROXIES") or []),
        )
        # Shut the FIRST down while the second is still serving: the loopback
        # must stay trusted (order-independent teardown — the bug was the first
        # ripping 127.0.0.1 out from under the live second terminator).
        asyncio.run(handles[0][0].run_shutdown())
        check(
            "loopback still trusted after the FIRST install shuts down",
            "127.0.0.1" in (DEFAULTS.get("TRUSTED_PROXIES") or []),
        )
        # Now the last dependant goes away → the host is finally removed.
        asyncio.run(handles[1][0].run_shutdown())
        check(
            "loopback removed once the LAST install shuts down",
            "127.0.0.1" not in (DEFAULTS.get("TRUSTED_PROXIES") or []),
        )
    finally:
        for _app, handle in handles:
            if handle.terminator is not None and handle.terminator.is_alive():
                handle.terminator.stop()
        DEFAULTS["TRUSTED_PROXIES"] = saved
        mtls_mod._LOOPBACK_TRUST_REFCOUNT.clear()
        mtls_mod._LOOPBACK_TRUST_REFCOUNT.update(saved_refs)


def test_double_start_guard_and_restart():
    print("\n== start() guards double-start; supports restart after stop ==")
    _write_certs()
    term = _terminator()
    term.start()
    try:
        check("terminator alive after first start", term.is_alive())
        raised = False
        try:
            term.start()  # already running → must refuse, not orphan the thread
        except Exception:  # noqa: BLE001 - a double-start must fail loudly
            raised = True
        check("second start() while running raises", raised)
        check("still alive after the rejected double-start", term.is_alive())
    finally:
        term.stop()
    check("not alive after stop", not term.is_alive())

    # stop() → start() restart is supported: the ready gate and start error are
    # reset so the fresh listener thread is awaited cleanly and re-registers.
    term.start()
    try:
        check("restart after stop binds again", term.is_alive())
        check(
            "restarted terminator re-registered its attestation",
            resolve_client_cert(_req_for(term.attestation_secret)) is not None,
        )
    finally:
        term.stop()
    check("not alive after the final stop", not term.is_alive())


def test_upgrade_idle_ordering_preserved():
    print("\n== upgrade_idle_timeout < idle_timeout warns but is preserved ==")
    _write_certs()
    # Inverted ordering: the terminator warns (fails safe) but preserves the
    # operator's explicit value rather than clamping — a deliberately small
    # upgrade ceiling (e.g. a test) must survive unchanged.
    term = _terminator(idle_timeout=30.0, upgrade_idle_timeout=0.5)
    check(
        "inverted ordering preserves the configured upgrade ceiling (no clamp)",
        term.upgrade_idle_timeout == 0.5 and term.idle_timeout == 30.0,
    )
    ok = _terminator(idle_timeout=10.0, upgrade_idle_timeout=60.0)
    check(
        "correctly-ordered pair stored unchanged",
        ok.upgrade_idle_timeout == 60.0 and ok.idle_timeout == 10.0,
    )


def test_install_disabled():
    print("\n== install() stays disabled with no port / no cert ==")
    _write_certs()
    app = _FakeApp(bound_port=9000)
    handle = MTLSTerminator.install(
        app,
        listen_port=0,  # disabled
        cert_file=str(SCRATCH / "srv.crt"),
        key_file=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    asyncio.run(app.run_startup())
    check(
        "disabled config leaves the handle terminator None", handle.terminator is None
    )
    check(
        "readiness healthy when mTLS is disabled",
        app.health_checks["mtls_terminator"]() is True,
    )
    # Shutdown must be a no-op (no terminator to stop) rather than raising.
    asyncio.run(app.run_shutdown())
    check("shutdown is a no-op when disabled", handle.terminator is None)


def test_from_config_disabled():
    print("\n== from_config returns None when disabled ==")
    _write_certs()
    check(
        "no listen_port → disabled",
        MTLSTerminator.from_config(
            upstream_port=9000,
            listen_port=0,
            cert_file=str(SCRATCH / "srv.crt"),
            key_file=str(SCRATCH / "srv.key"),
            ca_file=str(SCRATCH / "ca.crt"),
        )
        is None,
    )
    check(
        "no cert_file → disabled",
        MTLSTerminator.from_config(
            upstream_port=9000,
            listen_port=8443,
            cert_file="",
            key_file="",
            ca_file="",
        )
        is None,
    )


def test_from_config_enabled_and_port_wiring():
    print("\n== from_config enabled: started + upstream_port wired ==")
    _write_certs()
    # Build WITHOUT starting to assert the port wiring cleanly.
    term = MTLSTerminator.from_config(
        upstream_port=12345,
        listen_port=_free_port(),
        cert_file=str(SCRATCH / "srv.crt"),
        key_file=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        listen_host="127.0.0.1",
        start=False,
    )
    check("enabled config yields a terminator", term is not None)
    check(
        "REQUIRED upstream_port wired straight onto the terminator",
        term is not None and term.upstream_port == 12345,
    )

    # Now the started path: it binds, registers, and reports alive.
    started = MTLSTerminator.from_config(
        upstream_port=23456,
        listen_port=_free_port(),
        cert_file=str(SCRATCH / "srv.crt"),
        key_file=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        listen_host="127.0.0.1",
    )
    check("started terminator is not None", started is not None)
    try:
        check("started terminator is alive", started.is_alive())
        check(
            "started terminator registered its attestation",
            resolve_client_cert(_req_for(started.attestation_secret)) is not None,
        )
    finally:
        started.stop()


def test_from_config_requires_upstream_port():
    print("\n== from_config makes upstream_port mandatory ==")
    raised = False
    try:
        # No upstream_port → TypeError (keyword-only + required).
        MTLSTerminator.from_config(  # type: ignore[call-arg]
            listen_port=0,
            cert_file="",
            key_file="",
            ca_file="",
        )
    except TypeError:
        raised = True
    check("omitting upstream_port is a TypeError", raised)


def main() -> bool:
    print("hyperdjango.mtls wiring unit tests")
    test_registry_registration_and_deregistration()
    test_precedence_registry_setting()
    test_proxy_secret_min_length()
    test_install_wires_lifecycle()
    test_install_trusts_loopback_upstream()
    test_install_trust_upstream_ip_false_leaves_untouched()
    test_loopback_trust_refcounted()
    test_double_start_guard_and_restart()
    test_upgrade_idle_ordering_preserved()
    test_install_fails_loud_on_ephemeral_bound_port()
    test_install_disabled()
    test_from_config_disabled()
    test_from_config_enabled_and_port_wiring()
    test_from_config_requires_upstream_port()
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
