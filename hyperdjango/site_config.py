"""Site configuration for white-label app deployment.

Provides frozen dataclasses for branding, theming, and app-level settings
that can be loaded from TOML files, environment variables, or Python code.

Usage:
    from hyperdjango.site_config import SiteConfig, ThemeColors, load_site_config

    # Direct construction
    config = SiteConfig(name="MyApp", theme=ThemeColors(primary="#3b82f6"))
    app = HyperApp(site_config=config)

    # From TOML + env vars
    config = load_site_config(MySiteConfig, env_prefix="MYAPP")
    app = HyperApp(site_config=config)

    # Templates automatically get {{ site.name }}, {{ site_css }}, etc.

Thread-safe: All public functions perform reads only and return new frozen
instances. Safe to call from multiple threads concurrently.
"""

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TypeVar

from hyperdjango.conf import parse_bool

# Sanctioned env boundary: this module IS a config authority. It loads per-app
# SiteConfig dataclasses from their own `{PREFIX}_*` env vars + TOML — an
# app-config surface parallel to conf.py, not one of the framework's HYPER_*
# settings — so it reads os.environ directly. scripts/check_no_os_environ.py
# allowlists it alongside conf.py.

T = TypeVar("T")

# CSS color validation: hex colors, rgb(), hsl(), named colors, CSS keywords
_CSS_COLOR_RE = re.compile(
    r"^("
    r"#[0-9a-fA-F]{3,8}"  # #rgb, #rrggbb, #rrggbbaa
    r"|rgb\([^)]+\)"  # rgb(...)
    r"|rgba\([^)]+\)"  # rgba(...)
    r"|hsl\([^)]+\)"  # hsl(...)
    r"|hsla\([^)]+\)"  # hsla(...)
    r"|[a-zA-Z]+"  # named colors (red, blue, transparent, inherit, etc.)
    r")$"
)


def _validate_css_color(value: str, field_name: str) -> None:
    """Validate a CSS color value to prevent injection attacks.

    Rejects values containing HTML/CSS-breaking characters like
    ``<``, ``>``, ``"``, ``'``, ``{``, ``}``, ``;`` outside of
    valid CSS color functions.
    """
    if not _CSS_COLOR_RE.match(value.strip()):
        raise ValueError(
            f"Invalid CSS color for '{field_name}': {value!r}. "
            f"Expected hex (#rrggbb), rgb(), hsl(), or named color."
        )


# CSS declaration-value breakout characters. Font stacks legitimately contain
# commas, spaces, quotes, and units (e.g. '"Helvetica Neue", Arial', '14px'),
# so we can't reuse the strict color regex; instead reject only the characters
# that could terminate the `:root { --x: VALUE; }` declaration or break out of
# a surrounding <style> element.
_CSS_VALUE_UNSAFE = re.compile(r"[;{}<>]")


def _validate_css_value(value: str, field_name: str) -> None:
    """Validate a free-form CSS value (font family, size) against injection.

    Rejects declaration/element-breakout characters (``;``, ``{``, ``}``,
    ``<``, ``>``) while still allowing legitimate font-stack syntax
    (commas, spaces, quotes, units).
    """
    if _CSS_VALUE_UNSAFE.search(value):
        raise ValueError(
            f"Invalid CSS value for '{field_name}': {value!r}. "
            f"Must not contain any of ; {{ }} < >"
        )


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """CSS color palette for site theming.

    All fields are CSS color strings validated against injection attacks.
    The ``to_css_vars()`` method generates a ``:root { ... }`` CSS block
    that templates inject into ``<head>``.
    """

    primary: str = "#1a73e8"
    primary_dark: str = "#1557b0"
    background: str = "#ffffff"
    surface: str = "#f8f9fa"
    text: str = "#333333"
    text_secondary: str = "#6b7280"
    border: str = "#e5e7eb"
    success: str = "#22c55e"
    warning: str = "#f59e0b"
    danger: str = "#dc2626"
    link: str = "#1a73e8"
    # Per-prefix render cache. The instance is FROZEN, so a rendered block is
    # immutable for its lifetime — yet ``{{ site_css }}`` asked for it on
    # every request, rebuilding the same string through fields() reflection
    # each time (measured 6+ us/request on the hypernews cached index). The
    # dict's contents may mutate inside a frozen instance; identical
    # concurrent builds racing into it are benign (last-write-wins, same
    # value). Excluded from compare so frozen-dataclass hashing is untouched.
    _css_vars_cache: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Validate all color fields to prevent CSS injection."""
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            _validate_css_color(object.__getattribute__(self, f.name), f.name)

    def to_css_vars(self, prefix: str = "") -> str:
        """Generate a CSS ``:root`` block with custom properties.

        Cached per prefix — the instance is frozen, so the block never
        changes after construction.

        Args:
            prefix: Optional prefix for variable names (e.g., "hn" -> ``--hn-primary``).

        Returns:
            CSS string like ``:root { --primary: #1a73e8; --danger: #dc2626; }``
        """
        cached = self._css_vars_cache.get(prefix)
        if cached is not None:
            return cached
        pfx = f"{prefix}-" if prefix else ""
        lines = []
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            # Use object.__getattribute__ — frozen slotted dataclass, no __dict__
            val = object.__getattribute__(self, f.name)
            lines.append(f"  --{pfx}{f.name.replace('_', '-')}: {val};")
        css = ":root {\n" + "\n".join(lines) + "\n}"
        self._css_vars_cache[prefix] = css
        return css


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """Base site configuration for white-label app deployment.

    Subclass this with app-specific fields. All defaults produce the
    current hardcoded behavior, so deploying without a config file
    changes nothing.

    Templates access fields via ``{{ site.name }}``, ``{{ site.tagline }}``, etc.
    CSS custom properties are available via ``{{ site_css }}``.
    """

    name: str = "HyperDjango"
    tagline: str = ""
    logo_icon: str = ""
    logo_url: str = ""
    favicon_url: str = ""
    footer_text: str = "Powered by HyperDjango"
    theme: ThemeColors = field(default_factory=ThemeColors)
    font_family: str = "Inter, system-ui, sans-serif"
    base_font_size: str = "14px"
    # Same per-prefix render cache as ThemeColors — see the note there.
    _css_vars_cache: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Validate free-form CSS values (fonts) to prevent CSS injection."""
        if self.font_family:
            _validate_css_value(self.font_family, "font_family")
        if self.base_font_size:
            _validate_css_value(self.base_font_size, "base_font_size")

    def to_css_vars(self, prefix: str = "") -> str:
        """Generate the full site CSS ``:root`` block: theme colors + fonts.

        Extends ``theme.to_css_vars()`` with ``--font-family`` and
        ``--base-font-size`` custom properties (emitted only when set), so
        ``{{ site_css }}`` carries the configured typography rather than colors
        alone. Emitted as an additional ``:root`` block, which merges with the
        theme block per the CSS cascade.

        Args:
            prefix: Optional prefix for variable names (e.g., "hn" ->
                ``--hn-font-family``). Applied to both colors and fonts.

        Returns:
            CSS string containing one or two ``:root { ... }`` blocks.
        """
        cached = self._css_vars_cache.get(prefix)
        if cached is not None:
            return cached
        css = self.theme.to_css_vars(prefix)
        pfx = f"{prefix}-" if prefix else ""
        font_lines = []
        if self.font_family:
            font_lines.append(f"  --{pfx}font-family: {self.font_family};")
        if self.base_font_size:
            font_lines.append(f"  --{pfx}base-font-size: {self.base_font_size};")
        if font_lines:
            css += "\n:root {\n" + "\n".join(font_lines) + "\n}"
        self._css_vars_cache[prefix] = css
        return css


def load_site_config[T](
    config_cls: type[T],
    env_prefix: str,
    config_path: Path | None = None,
) -> T:
    """Load a SiteConfig subclass from TOML file + env vars + defaults.

    Resolution order (highest wins):
      1. Environment variables (``{ENV_PREFIX}_{FIELD_NAME}``)
      2. TOML config file (if ``config_path`` provided or auto-detected)
      3. Dataclass field defaults

    Nested dataclasses (like ``theme: ThemeColors``) are loaded from
    TOML sections and env vars with double prefix:
    ``{ENV_PREFIX}_THEME_PRIMARY=#ff6600``.

    Only simple scalar types (str, int, float, bool) can be loaded from
    env vars. Complex types (tuples, nested dataclasses) must use TOML
    or Python construction.

    Args:
        config_cls: A frozen dataclass type (subclass of SiteConfig).
        env_prefix: Env var prefix, e.g. ``"HYPERNEWS"`` -> ``HYPERNEWS_NAME``.
        config_path: Path to a TOML file. If explicitly provided and the
                     file does not exist, raises ``FileNotFoundError``.
                     If None, no TOML file is loaded.

    Returns:
        A fully constructed config instance.

    Raises:
        FileNotFoundError: If config_path is provided but doesn't exist.
        ValueError: If the TOML file is malformed or contains invalid values.
    """
    # Phase 1: Load TOML values
    toml_data: dict[str, object] = {}
    if config_path is not None:
        if not config_path.exists():
            raise FileNotFoundError(f"Site config file not found: {config_path}")
        try:
            with config_path.open("rb") as f:
                toml_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"Invalid TOML in {config_path}: {e}") from e

    # Phase 2: Build kwargs from defaults -> TOML -> env vars
    kwargs: dict[str, object] = {}
    prefix_upper = env_prefix.upper()

    for f in fields(config_cls):
        # Private fields (the render cache) are init=False internals — never
        # loadable from TOML/env, and passing one as a kwarg would TypeError.
        if f.name.startswith("_"):
            continue
        # Check if field type is a nested frozen dataclass
        if dataclasses.is_dataclass(f.type):
            toml_value = toml_data.get(f.name)
            if toml_value is not None and not isinstance(toml_value, dict):
                raise ValueError(
                    f"Config field '{f.name}' must be a TOML table [section], "
                    f"got {type(toml_value).__name__}: {toml_value!r}"
                )
            nested_kwargs = _load_nested(
                f.type,
                prefix_upper + "_" + f.name.upper(),
                toml_value or {},
            )
            if nested_kwargs:
                kwargs[f.name] = f.type(**nested_kwargs)
        else:
            # Simple field: check env, then TOML, then skip (use default)
            env_key = f"{prefix_upper}_{f.name.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                kwargs[f.name] = _coerce(env_val, f.type, env_key)
            elif f.name in toml_data:
                kwargs[f.name] = toml_data[f.name]

    return config_cls(**kwargs)


def _load_nested(
    nested_cls: type,
    env_prefix: str,
    toml_section: object,
) -> dict[str, object]:
    """Load a nested frozen dataclass from env vars + TOML section."""
    toml_dict = toml_section if isinstance(toml_section, dict) else {}
    kwargs: dict[str, object] = {}

    for f in fields(nested_cls):
        if f.name.startswith("_"):
            continue
        env_key = f"{env_prefix}_{f.name.upper()}"
        env_val = os.environ.get(env_key)

        if env_val is not None:
            kwargs[f.name] = _coerce(env_val, f.type, env_key)
        elif f.name in toml_dict:
            toml_val = toml_dict[f.name]
            # Recursively handle nested dataclasses within nested dataclasses
            if dataclasses.is_dataclass(f.type) and isinstance(toml_val, dict):
                sub_kwargs = _load_nested(
                    f.type,
                    env_prefix + "_" + f.name.upper(),
                    toml_val,
                )
                if sub_kwargs:
                    kwargs[f.name] = f.type(**sub_kwargs)
            else:
                kwargs[f.name] = toml_val

    return kwargs


def _coerce(value: str, target_type: type, env_key: str) -> int | float | bool | str:
    """Coerce a string env var value to the target type.

    Only simple scalar types are supported from env vars. Complex types
    (tuples, lists, nested dataclasses) raise TypeError with guidance.
    """
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is bool:
        return parse_bool(value)
    if target_type is str:
        return value

    # Reject complex types from env vars with a clear message
    raise TypeError(
        f"Cannot load complex type {target_type} from env var {env_key}. "
        f"Use a TOML config file or Python construction instead."
    )
