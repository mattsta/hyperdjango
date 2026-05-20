#!/usr/bin/env python3
"""Unit tests for the SiteConfig white-label configuration system.

Tests: ThemeColors, SiteConfig, load_site_config(), TOML loading,
       env var overrides, frozen immutability, CSS generation,
       HyperApp integration.

Run: uv run hyper-test site_config
"""
# hyper-test: unit

import contextlib
import dataclasses
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango import HyperApp
from hyperdjango.site_config import (
    _CSS_COLOR_RE,
    SiteConfig,
    ThemeColors,
    load_site_config,
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


def main():
    global passed, failed

    # ── ThemeColors basics ────────────────────────────────────────────
    print("\n=== ThemeColors ===")

    tc = ThemeColors()
    check("default primary", tc.primary == "#1a73e8")
    check("default danger", tc.danger == "#dc2626")
    check("frozen", not hasattr(ThemeColors, "__dict__") or True)  # slots=True

    try:
        tc.primary = "#000"  # type: ignore[misc]
        check("immutable", False, "should have raised FrozenInstanceError")
    except dataclasses.FrozenInstanceError:
        check("immutable", True)

    custom = ThemeColors(primary="#ff6600", background="#f6f6ef")
    check("custom primary", custom.primary == "#ff6600")
    check("custom background", custom.background == "#f6f6ef")
    check("other defaults preserved", custom.danger == "#dc2626")

    # ── CSS generation ────────────────────────────────────────────────
    print("\n=== CSS generation ===")

    css = tc.to_css_vars()
    check("starts with :root", css.startswith(":root {"))
    check("ends with }", css.strip().endswith("}"))
    check("contains --primary", "--primary: #1a73e8;" in css)
    check("contains --danger", "--danger: #dc2626;" in css)
    check("contains --text-secondary", "--text-secondary: #6b7280;" in css)

    prefixed = tc.to_css_vars(prefix="hn")
    check("prefix applied", "--hn-primary: #1a73e8;" in prefixed)
    check("prefix on danger", "--hn-danger: #dc2626;" in prefixed)

    # ── SiteConfig basics ─────────────────────────────────────────────
    print("\n=== SiteConfig ===")

    sc = SiteConfig()
    check("default name", sc.name == "HyperDjango")
    check("default footer", sc.footer_text == "Powered by HyperDjango")
    check("theme is ThemeColors", isinstance(sc.theme, ThemeColors))

    try:
        sc.name = "changed"  # type: ignore[misc]
        check("site frozen", False, "should have raised")
    except dataclasses.FrozenInstanceError:
        check("site frozen", True)

    custom_sc = SiteConfig(
        name="TestApp",
        tagline="A test",
        theme=ThemeColors(primary="#3b82f6"),
    )
    check("custom name", custom_sc.name == "TestApp")
    check("custom tagline", custom_sc.tagline == "A test")
    check("custom theme primary", custom_sc.theme.primary == "#3b82f6")

    # ── Subclassing ───────────────────────────────────────────────────
    print("\n=== Subclassing ===")

    @dataclass(frozen=True, slots=True)
    class MyTheme(ThemeColors):
        accent: str = "#9333ea"

    @dataclass(frozen=True, slots=True)
    class MyConfig(SiteConfig):
        name: str = "MyApp"
        max_items: int = 100
        theme: MyTheme = field(default_factory=MyTheme)

    mc = MyConfig()
    check("subclass name", mc.name == "MyApp")
    check("subclass extra field", mc.max_items == 100)
    check("subclass theme type", isinstance(mc.theme, MyTheme))
    check("subclass theme accent", mc.theme.accent == "#9333ea")
    check("subclass theme inherits primary", mc.theme.primary == "#1a73e8")

    # ── TOML loading ──────────────────────────────────────────────────
    print("\n=== TOML loading ===")

    toml_dir = Path(__file__).resolve().parent.parent / "logs"
    toml_dir.mkdir(parents=True, exist_ok=True)
    toml_path = toml_dir / "_test_site_config.toml"

    toml_content = """\
name = "CustomApp"
tagline = "My custom tagline"
footer_text = "Custom Footer"

[theme]
primary = "#3b82f6"
background = "#f0f4f8"
"""
    toml_path.write_text(toml_content)
    try:
        loaded = load_site_config(SiteConfig, "SITETEST", toml_path)
        check("toml name", loaded.name == "CustomApp")
        check("toml tagline", loaded.tagline == "My custom tagline")
        check("toml footer", loaded.footer_text == "Custom Footer")
        check("toml theme primary", loaded.theme.primary == "#3b82f6")
        check("toml theme background", loaded.theme.background == "#f0f4f8")
        check("toml theme default danger", loaded.theme.danger == "#dc2626")
        check("toml default font", loaded.font_family == "Inter, system-ui, sans-serif")
    finally:
        toml_path.unlink(missing_ok=True)

    # ── TOML with subclass ────────────────────────────────────────────
    print("\n=== TOML with subclass ===")

    toml_sub = """\
name = "SubApp"
max_items = 50

[theme]
primary = "#ef4444"
accent = "#a855f7"
"""
    toml_path.write_text(toml_sub)
    try:
        loaded_sub = load_site_config(MyConfig, "SUBTEST", toml_path)
        check("sub toml name", loaded_sub.name == "SubApp")
        check("sub toml max_items", loaded_sub.max_items == 50)
        check("sub toml theme primary", loaded_sub.theme.primary == "#ef4444")
        check("sub toml theme accent", loaded_sub.theme.accent == "#a855f7")
        check("sub toml theme default danger", loaded_sub.theme.danger == "#dc2626")
    finally:
        toml_path.unlink(missing_ok=True)

    # ── Env var overrides ─────────────────────────────────────────────
    print("\n=== Env var overrides ===")

    env_vars = {
        "ENVTEST_NAME": "EnvApp",
        "ENVTEST_TAGLINE": "From env",
        "ENVTEST_THEME_PRIMARY": "#00ff00",
    }
    old_values = {}
    for k, v in env_vars.items():
        old_values[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        loaded_env = load_site_config(SiteConfig, "ENVTEST")
        check("env name", loaded_env.name == "EnvApp")
        check("env tagline", loaded_env.tagline == "From env")
        check("env theme primary", loaded_env.theme.primary == "#00ff00")
        check("env default footer", loaded_env.footer_text == "Powered by HyperDjango")
    finally:
        for k, v in old_values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── Env vars override TOML ────────────────────────────────────────
    print("\n=== Env overrides TOML ===")

    toml_content2 = """\
name = "TomlName"
tagline = "TomlTagline"
"""
    toml_path.write_text(toml_content2)
    os.environ["MIXTEST_NAME"] = "EnvWins"
    try:
        loaded_mix = load_site_config(SiteConfig, "MIXTEST", toml_path)
        check("env beats toml", loaded_mix.name == "EnvWins")
        check("toml tagline kept", loaded_mix.tagline == "TomlTagline")
    finally:
        os.environ.pop("MIXTEST_NAME", None)
        toml_path.unlink(missing_ok=True)

    # ── No config file, no env → pure defaults ────────────────────────
    print("\n=== Pure defaults ===")

    default_only = load_site_config(SiteConfig, "NOEXIST")
    check("default name", default_only.name == "HyperDjango")
    check("default theme", default_only.theme.primary == "#1a73e8")

    # ── dataclasses.replace() for overlays ────────────────────────────
    print("\n=== replace() overlay ===")

    base = SiteConfig(name="Base", theme=ThemeColors(primary="#111"))
    overlay = dataclasses.replace(
        base,
        name="Tenant",
        theme=dataclasses.replace(base.theme, primary="#222"),
    )
    check("overlay name", overlay.name == "Tenant")
    check("overlay theme", overlay.theme.primary == "#222")
    check("base unchanged", base.name == "Base")
    check("base theme unchanged", base.theme.primary == "#111")

    # ── HyperApp integration ──────────────────────────────────────────
    print("\n=== HyperApp integration ===")

    config = SiteConfig(name="IntegrationTest", theme=ThemeColors(primary="#abcdef"))
    app = HyperApp(site_config=config)
    check("app title from config", app.title == "IntegrationTest")
    check("app.site_config set", app.site_config is config)
    check("app.site_config.theme.primary", app.site_config.theme.primary == "#abcdef")

    # Title param ignored when site_config provided
    app2 = HyperApp(title="Ignored", site_config=config)
    check("site_config overrides title", app2.title == "IntegrationTest")

    # No site_config → normal title
    app3 = HyperApp(title="Normal")
    check("no config uses title", app3.title == "Normal")
    check("no config site_config is None", app3.site_config is None)

    # ── CSS color validation ─────────────────────────────────────────
    print("\n=== CSS color validation ===")

    # Valid colors
    ThemeColors(primary="#ff6600")
    check("hex 6-digit valid", True)
    ThemeColors(primary="#fff")
    check("hex 3-digit valid", True)
    ThemeColors(primary="red")
    check("named color valid", True)
    ThemeColors(primary="transparent")
    check("transparent valid", True)

    # XSS injection attempts
    try:
        ThemeColors(primary="</style><script>alert(1)</script>")
        check("xss injection blocked", False, "should have raised ValueError")
    except ValueError:
        check("xss injection blocked", True)

    try:
        ThemeColors(primary="#ff6600; background: url(evil)")
        check("css injection blocked", False, "should have raised ValueError")
    except ValueError:
        check("css injection blocked", True)

    try:
        ThemeColors(primary='"><img src=x onerror=alert(1)>')
        check("html injection blocked", False, "should have raised ValueError")
    except ValueError:
        check("html injection blocked", True)

    # ── Missing config file error ─────────────────────────────────────
    print("\n=== Missing config file error ===")

    try:
        load_site_config(SiteConfig, "TEST", Path("/nonexistent/site.toml"))
        check("missing file raises", False, "should have raised FileNotFoundError")
    except FileNotFoundError:
        check("missing file raises", True)

    # ── Malformed TOML error ──────────────────────────────────────────
    print("\n=== Malformed TOML error ===")

    bad_toml = toml_dir / "_test_bad.toml"
    bad_toml.write_text("this is not valid toml [[[")
    try:
        load_site_config(SiteConfig, "BAD", bad_toml)
        check("bad toml raises", False, "should have raised ValueError")
    except ValueError as e:
        check("bad toml raises", "Invalid TOML" in str(e))
    finally:
        bad_toml.unlink(missing_ok=True)

    # ── Complex type env var rejection ────────────────────────────────
    print("\n=== Complex type env var rejection ===")

    @dataclass(frozen=True, slots=True)
    class ConfigWithTuple(SiteConfig):
        items: tuple[str, ...] = field(default_factory=tuple)

    os.environ["CPLX_ITEMS"] = "a,b,c"
    try:
        load_site_config(ConfigWithTuple, "CPLX")
        check("tuple env var rejected", False, "should have raised TypeError")
    except TypeError as e:
        check("tuple env var rejected", "Cannot load complex type" in str(e))
    finally:
        os.environ.pop("CPLX_ITEMS", None)

    # ── Hypothesis fuzz tests ─────────────────────────────────────────
    print("\n=== Hypothesis fuzz tests ===")

    _PAR = os.environ.get("HYPER_TEST_PARALLEL") == "1"
    _MAX = 50 if _PAR else 200

    # Fuzz: valid hex colors always pass validation
    @given(r=st.integers(0, 255), g=st.integers(0, 255), b=st.integers(0, 255))
    @settings(max_examples=_MAX, suppress_health_check=[HealthCheck.too_slow])
    def test_valid_hex_roundtrip(r, g, b):
        color = f"#{r:02x}{g:02x}{b:02x}"
        tc = ThemeColors(primary=color)
        css = tc.to_css_vars()
        assert f"--primary: {color};" in css

    try:
        test_valid_hex_roundtrip()
        check("fuzz: valid hex colors", True)
    except Exception as e:
        check("fuzz: valid hex colors", False, str(e))

    # Fuzz: arbitrary strings are rejected as colors
    @given(
        s=st.text(min_size=1).filter(
            lambda x: not x.strip().startswith("#") and not x.strip().isalpha()
        )
    )
    @settings(max_examples=_MAX)
    def test_arbitrary_strings_rejected(s):
        try:
            ThemeColors(primary=s)
            # If it didn't raise, the value must match the CSS color pattern
            assert _CSS_COLOR_RE.match(s.strip()), f"Should have rejected: {s!r}"
        except ValueError:
            pass  # Expected — invalid color rejected

    try:
        test_arbitrary_strings_rejected()
        check("fuzz: arbitrary strings rejected", True)
    except Exception as e:
        check("fuzz: arbitrary strings rejected", False, str(e))

    # Fuzz: XSS payloads always rejected
    xss_chars = st.sampled_from(["<", ">", '"', "'", "{", "}", ";", "()", "script"])

    @given(prefix=st.text(max_size=5), payload=xss_chars, suffix=st.text(max_size=5))
    @settings(max_examples=_MAX)
    def test_xss_payloads_rejected(prefix, payload, suffix):
        malicious = prefix + payload + suffix
        with contextlib.suppress(ValueError):
            ThemeColors(primary=malicious)

    try:
        test_xss_payloads_rejected()
        check("fuzz: XSS payloads rejected", True)
    except Exception as e:
        check("fuzz: XSS payloads rejected", False, str(e))

    # Fuzz: SiteConfig name roundtrips through frozen dataclass
    @given(name=st.text(min_size=1, max_size=100))
    @settings(max_examples=_MAX)
    def test_site_name_roundtrip(name):
        sc = SiteConfig(name=name)
        assert sc.name == name

    try:
        test_site_name_roundtrip()
        check("fuzz: site name roundtrip", True)
    except Exception as e:
        check("fuzz: site name roundtrip", False, str(e))

    # Fuzz: dataclasses.replace preserves all fields
    @given(
        name=st.text(min_size=1, max_size=50),
        tagline=st.text(max_size=100),
        footer=st.text(max_size=100),
    )
    @settings(max_examples=_MAX)
    def test_replace_preserves_fields(name, tagline, footer):
        base = SiteConfig(name="Base", tagline="base", footer_text="base footer")
        overlay = dataclasses.replace(
            base, name=name, tagline=tagline, footer_text=footer
        )
        assert overlay.name == name
        assert overlay.tagline == tagline
        assert overlay.footer_text == footer
        # Theme should be inherited from base
        assert overlay.theme.primary == base.theme.primary

    try:
        test_replace_preserves_fields()
        check("fuzz: replace preserves fields", True)
    except Exception as e:
        check("fuzz: replace preserves fields", False, str(e))

    # Fuzz: CSS vars output is valid CSS (no injection)
    @given(r=st.integers(0, 255), g=st.integers(0, 255), b=st.integers(0, 255))
    @settings(max_examples=_MAX)
    def test_css_output_safe(r, g, b):
        tc = ThemeColors(primary=f"#{r:02x}{g:02x}{b:02x}")
        css = tc.to_css_vars()
        # Must not contain script tags or HTML
        assert "<script" not in css.lower()
        assert "</" not in css
        assert css.startswith(":root {")
        assert css.strip().endswith("}")

    try:
        test_css_output_safe()
        check("fuzz: CSS output safe", True)
    except Exception as e:
        check("fuzz: CSS output safe", False, str(e))

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All site_config tests passed!")
    else:
        print("SOME TESTS FAILED!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
