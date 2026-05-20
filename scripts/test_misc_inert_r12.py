"""Inert-layer / silent-no-op fixes, round 12 (misc_inert_r12).

Regression tests for four previously-inert code paths:

  * #14 openapi.py — a generated OpenAPI spec that declares
    ``components.securitySchemes`` must ALSO emit a top-level ``security``
    requirement referencing a declared scheme. Without it, OpenAPI marks
    every operation public and generated docs/clients drop auth entirely.
  * #7 metering.py — the documented ``MeterHook`` overrides
    ``on_period_close`` and ``on_quota_exceeded`` were never invoked.
    ``export_period()`` must fire ``on_period_close`` on every registered
    hook, and a quota breach must fire ``on_quota_exceeded``.
  * #13 staticfiles.py — ``_is_hashed_filename`` hardcoded a 12-char hash,
    ignoring ``STATICFILES_HASH_LENGTH``; hashes of length 8/16 must be
    recognized so the ``immutable`` Cache-Control survives.
  * #17a site_config.py — ``SiteConfig.to_css_vars()`` must emit
    ``--font-family`` / ``--base-font-size`` (when set), not colors alone,
    so ``{{ site_css }}`` carries the configured typography.

Run:  uv run hyper-test misc_inert_r12
"""

# hyper-test: unit

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ── #14 OpenAPI: security requirement emitted ───────────────────────────────


class _SessionMiddleware:
    """Name contains 'Session' -> _detect_security_schemes emits sessionAuth."""


def test_openapi_security_requirement() -> None:
    print("\n=== #14 OpenAPI emits a security requirement ===")
    from hyperdjango.app import HyperApp
    from hyperdjango.openapi import generate_openapi

    app = HyperApp(title="T")

    @app.get("/items")
    async def list_items(request):
        return None

    # Inject an auth middleware so a scheme is detected.
    app._middleware._middleware = [_SessionMiddleware()]

    spec = generate_openapi(app)

    schemes = spec.get("components", {}).get("securitySchemes", {})
    check("securitySchemes populated", bool(schemes), repr(schemes))

    security = spec.get("security")
    check("top-level 'security' present", security is not None, repr(security))
    check(
        "'security' is a non-empty list",
        isinstance(security, list) and len(security) > 0,
        repr(security),
    )
    if isinstance(security, list) and security:
        referenced = set()
        for req in security:
            referenced.update(req.keys())
        check(
            "security references a declared scheme",
            bool(referenced & set(schemes.keys())),
            f"referenced={referenced} declared={set(schemes.keys())}",
        )

    # No auth middleware -> no schemes -> no bogus security requirement.
    app2 = HyperApp(title="T2")

    @app2.get("/open")
    async def open_ep(request):
        return None

    app2._middleware._middleware = []
    spec2 = generate_openapi(app2)
    check(
        "no schemes -> no 'security' key",
        "security" not in spec2,
        repr(spec2.get("security")),
    )


# ── #7 Metering: on_period_close + on_quota_exceeded fire ───────────────────


class _SpyHook:
    """Records which documented overrides were invoked."""

    def __init__(self):
        self.period_closed = None
        self.quota_ctx = None
        self.quota_decision = None

    async def on_event(self, ctx):
        pass

    async def on_quota_exceeded(self, ctx, decision):
        self.quota_ctx = ctx
        self.quota_decision = decision

    async def on_period_close(self, export):
        self.period_closed = export


class _FakeDB:
    async def query(self, *args, **kwargs):
        return []

    async def query_one(self, *args, **kwargs):
        return None


class _FakeQuotaEngine:
    """Stand-in engine for QuotaEnforcementHook: forces a breach + carries hooks."""

    def __init__(self, decision, hooks):
        self._decision = decision
        self._hooks = hooks

    async def check_quota(self, *args, **kwargs):
        return self._decision


def test_metering_on_period_close() -> None:
    print("\n=== #7 export_period() fires on_period_close ===")
    from hyperdjango.metering import MeterEngine

    async def scenario():
        engine = MeterEngine(_FakeDB())
        engine._meter_cache["m"] = 1  # skip _resolve_meter DB lookup
        spy = _SpyHook()
        engine.register_hook(spy)
        now = datetime.now(UTC)
        export = await engine.export_period("m", "acct", now, now)
        return spy, export

    spy, export = asyncio.run(scenario())
    check("on_period_close invoked", spy.period_closed is not None)
    check(
        "on_period_close received the returned PeriodExport",
        spy.period_closed is export,
        f"{spy.period_closed!r} vs {export!r}",
    )


def test_metering_on_quota_exceeded() -> None:
    print("\n=== #7 quota breach fires on_quota_exceeded ===")
    from hyperdjango.metering import (
        MeterHookContext,
        QuotaDecision,
        QuotaEnforcementHook,
    )

    async def scenario():
        decision = QuotaDecision(
            allowed=False,
            remaining=0.0,
            limit_value=10.0,
            action="warn",
            dimension_name="tokens",
        )
        spy = _SpyHook()
        engine = _FakeQuotaEngine(decision, [spy])
        qhook = QuotaEnforcementHook(engine)
        ctx = MeterHookContext(
            meter_name="m",
            account_id="acct",
            event_id=1,
            dimensions={"tokens": 5.0},
            tenant_id=None,
            timestamp=datetime.now(UTC),
        )
        await qhook.on_event(ctx)
        return spy, ctx, decision

    spy, ctx, decision = asyncio.run(scenario())
    check("on_quota_exceeded invoked", spy.quota_ctx is not None)
    check("on_quota_exceeded got the ctx", spy.quota_ctx is ctx)
    check("on_quota_exceeded got the decision", spy.quota_decision is decision)


# ── #13 StaticFiles: STATICFILES_HASH_LENGTH honored ────────────────────────


def test_staticfiles_hash_length_recognized() -> None:
    print("\n=== #13 hashed-filename recognition tracks STATICFILES_HASH_LENGTH ===")
    from hyperdjango.conf import DEFAULTS
    from hyperdjango.staticfiles import StaticFilesMiddleware

    mw = StaticFilesMiddleware()

    for length in (8, 12, 16):
        with patch.dict(DEFAULTS, {"STATICFILES_HASH_LENGTH": length}):
            good_hash = "a" * length
            name = f"styles.{good_hash}.css"
            check(
                f"len={length} hash recognized",
                mw._is_hashed_filename(name) is True,
                name,
            )
            cc = mw._cache_control(name)
            check(
                f"len={length} -> immutable Cache-Control",
                "immutable" in cc,
                cc,
            )
            # A hash of the wrong length must NOT be treated as hashed.
            wrong = "a" * (length + 1)
            check(
                f"len={length} rejects {length + 1}-char hash",
                mw._is_hashed_filename(f"styles.{wrong}.css") is False,
                wrong,
            )


# ── #17a SiteConfig: fonts emitted in to_css_vars ───────────────────────────


def test_site_config_font_vars() -> None:
    print("\n=== #17a SiteConfig.to_css_vars emits font variables ===")
    from hyperdjango.site_config import SiteConfig

    cfg = SiteConfig(font_family="Roboto, sans-serif", base_font_size="16px")
    css = cfg.to_css_vars()
    check(
        "colors still present",
        "--primary:" in css,
        css,
    )
    check(
        "--font-family emitted",
        "--font-family: Roboto, sans-serif;" in css,
        css,
    )
    check(
        "--base-font-size emitted",
        "--base-font-size: 16px;" in css,
        css,
    )

    # prefix applies to font vars too.
    css_pfx = cfg.to_css_vars("hn")
    check(
        "prefix applied to font var",
        "--hn-font-family: Roboto, sans-serif;" in css_pfx,
        css_pfx,
    )

    # Only-when-set: empty font fields are omitted, not emitted blank.
    cfg_empty = SiteConfig(font_family="", base_font_size="")
    css_empty = cfg_empty.to_css_vars()
    check("empty font_family omitted", "--font-family" not in css_empty, css_empty)
    check(
        "empty base_font_size omitted", "--base-font-size" not in css_empty, css_empty
    )

    # CSS-injection breakout is rejected at construction.
    rejected = False
    try:
        SiteConfig(font_family="Inter; } body { display:none } :root {")
    except ValueError:
        rejected = True
    check("font_family injection rejected", rejected)


def run() -> bool:
    test_openapi_security_requirement()
    test_metering_on_period_close()
    test_metering_on_quota_exceeded()
    test_staticfiles_hash_length_recognized()
    test_site_config_font_vars()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
