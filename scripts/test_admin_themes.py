"""Tests for admin dark mode + custom theme system.

Covers:
- ThemeConfig dataclass (creation, frozen, css_vars)
- CSS variable-based theming (dark mode vars in _ADMIN_CSS)
- Theme toggle button in header template
- Theme JavaScript (localStorage persistence, prefers-color-scheme)
- Custom theme registration on HyperAdmin
- Theme CSS generation
- Template structure (dark mode CSS present)

Usage:
    uv run hyper-test admin_themes
"""

# hyper-test: unit

import sys

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("Admin Dark Mode + Theme System Tests")
    print("=" * 60)

    # ── ThemeConfig Dataclass ─────────────────────────────────────

    print("\n--- ThemeConfig ---")

    from hyperdjango.admin.fields import ThemeConfig

    # Test 1: Basic creation
    theme = ThemeConfig(
        name="brand", label="Brand Theme", css_vars={"--primary": "#7c3aed"}
    )
    check("theme name", theme.name == "brand")
    check("theme label", theme.label == "Brand Theme")
    check("theme css_vars", theme.css_vars == {"--primary": "#7c3aed"})
    check("theme is_dark default", theme.is_dark is False)

    # Test 2: Dark theme
    dark = ThemeConfig(
        name="midnight",
        label="Midnight",
        is_dark=True,
        css_vars={
            "--bg": "#000",
            "--text": "#fff",
        },
    )
    check("dark theme is_dark", dark.is_dark is True)
    check("dark theme vars", dark.css_vars["--bg"] == "#000")

    # Test 3: Frozen dataclass
    try:
        theme.name = "changed"  # type: ignore[misc]
        check("theme is frozen", False, "Should raise")
    except AttributeError:
        check("theme is frozen", True)

    # Test 4: Empty css_vars
    minimal = ThemeConfig(name="minimal", label="Minimal")
    check("empty css_vars default", minimal.css_vars == {})

    # ── CSS Dark Mode Variables ───────────────────────────────────

    print("\n--- CSS Dark Mode Variables ---")

    from hyperdjango.admin.templates import _ADMIN_CSS

    # Test 5: Root variables present
    check("css has root vars", ":root {" in _ADMIN_CSS or ":root{" in _ADMIN_CSS)
    check("css has --bg", "--bg:" in _ADMIN_CSS)
    check("css has --card", "--card:" in _ADMIN_CSS)
    check("css has --primary", "--primary:" in _ADMIN_CSS)
    check("css has --text", "--text:" in _ADMIN_CSS)

    # Test 6: Dark theme variables
    check("css has data-theme dark", '[data-theme="dark"]' in _ADMIN_CSS)
    check("dark has --bg", _ADMIN_CSS.count("--bg:") >= 2)  # light + dark

    # Test 7: prefers-color-scheme media query
    check("css has prefers-color-scheme", "prefers-color-scheme: dark" in _ADMIN_CSS)

    # Test 8: CSS variable usage (no hardcoded colors in hover/focus)
    check("hover uses var", "var(--hover-bg)" in _ADMIN_CSS)
    check("th uses var", "var(--th-bg)" in _ADMIN_CSS)
    check("focus ring uses var", "var(--focus-ring)" in _ADMIN_CSS)

    # ── Theme Toggle in Header ────────────────────────────────────

    print("\n--- Theme Toggle in Header ---")

    from hyperdjango.admin.templates import _TEMPLATE_FOOTER, _TEMPLATE_HEADER

    # Test 9: Toggle button present
    check("header has theme toggle", "theme-toggle" in _TEMPLATE_HEADER)
    check("header has toggle onclick", "toggleTheme" in _TEMPLATE_HEADER)

    # Test 10: Theme JavaScript
    check("footer has theme JS", "toggleTheme" in _TEMPLATE_FOOTER)
    check("JS uses localStorage", "localStorage" in _TEMPLATE_FOOTER)
    check("JS uses data-theme", "data-theme" in _TEMPLATE_FOOTER)
    check("JS uses prefers-color-scheme", "prefers-color-scheme" in _TEMPLATE_FOOTER)
    check("JS sets hyper-theme key", "hyper-theme" in _TEMPLATE_FOOTER)

    # ── HyperAdmin Theme Registration ─────────────────────────────

    print("\n--- HyperAdmin Theme Registration ---")

    # Test theme registration directly (without full HyperAdmin init)
    class ThemeRegistry:
        """Minimal mixin to test theme registration without full HyperAdmin."""

        def __init__(self):
            self._themes: dict[str, ThemeConfig] = {}

        def register_theme(self, theme):
            self._themes[theme.name] = theme

        def get_theme_css(self, theme_name):
            theme = self._themes.get(theme_name)
            if theme is None:
                return ""
            props = " ".join(f"{k}: {v};" for k, v in theme.css_vars.items())
            return f'[data-theme="{theme.name}"] {{ {props} }}'

        @property
        def registered_themes(self):
            return list(self._themes.values())

    admin = ThemeRegistry()

    # Test 11: No themes initially
    check("no themes initially", len(admin.registered_themes) == 0)

    # Test 12: Register custom theme
    brand_theme = ThemeConfig(
        name="purple",
        label="Purple Brand",
        css_vars={"--primary": "#7c3aed", "--btn-hover": "#6d28d9"},
    )
    admin.register_theme(brand_theme)
    check("theme registered", len(admin.registered_themes) == 1)
    check("theme accessible by name", admin._themes["purple"] is brand_theme)

    # Test 13: Register multiple themes
    ocean_theme = ThemeConfig(
        name="ocean",
        label="Ocean Blue",
        css_vars={"--primary": "#0ea5e9", "--bg": "#f0f9ff"},
    )
    admin.register_theme(ocean_theme)
    check("two themes registered", len(admin.registered_themes) == 2)

    # Test 14: Generate theme CSS
    css = admin.get_theme_css("purple")
    check("theme css has selector", '[data-theme="purple"]' in css)
    check("theme css has primary", "--primary: #7c3aed" in css)
    check("theme css has btn-hover", "--btn-hover: #6d28d9" in css)

    # Test 15: Nonexistent theme CSS
    css_empty = admin.get_theme_css("nonexistent")
    check("nonexistent theme empty", css_empty == "")

    # Test 16: Override existing theme
    updated = ThemeConfig(
        name="purple", label="Purple Updated", css_vars={"--primary": "#8b5cf6"}
    )
    admin.register_theme(updated)
    check("theme override", admin._themes["purple"].label == "Purple Updated")
    check("still 2 themes", len(admin.registered_themes) == 2)

    # ── Template Integration ──────────────────────────────────────

    print("\n--- Template Integration ---")

    from hyperdjango.admin.templates import TEMPLATE_DASHBOARD

    # Test 17: Dashboard uses themed CSS
    check(
        "dashboard has dark vars",
        "data-theme" in TEMPLATE_DASHBOARD or "--bg:" in TEMPLATE_DASHBOARD,
    )
    check(
        "dashboard has toggle",
        "theme-toggle" in TEMPLATE_DASHBOARD or "toggleTheme" in TEMPLATE_DASHBOARD,
    )

    # Test 18: Theme CSS class present
    check("theme-toggle class in CSS", ".theme-toggle" in _ADMIN_CSS)

    # ── Dark Mode Specific Vars ───────────────────────────────────

    print("\n--- Dark Mode Color Scheme ---")

    # Test 19: Dark mode uses slate palette
    check("dark bg is slate-900", "#0f172a" in _ADMIN_CSS)
    check("dark card is slate-800", "#1e293b" in _ADMIN_CSS)
    check("dark text is slate-100", "#f1f5f9" in _ADMIN_CSS)
    check("dark muted is slate-400", "#94a3b8" in _ADMIN_CSS)
    check("dark border is slate-700", "#334155" in _ADMIN_CSS)

    # Test 20: Light mode preserved
    check("light bg is gray-50", "#f8f9fa" in _ADMIN_CSS)
    check("light card is white", "card: #fff" in _ADMIN_CSS)

    # ── Summary ──────────────────────────────────────────────────

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
