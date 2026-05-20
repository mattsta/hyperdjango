"""HyperNews site configuration — white-label branding & theming.

All values default to the current hardcoded HyperNews appearance.
Override via TOML file (``site.toml``) or environment variables
(``HYPERNEWS_NAME``, ``HYPERNEWS_THEME_PRIMARY``, etc.).

Usage:
    from services.hypernews.config import load_hypernews_config

    config = load_hypernews_config()
    app = HyperApp(site_config=config)

    # Or with a custom TOML:
    config = load_hypernews_config(Path("my_brand.toml"))
"""

from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango.site_config import SiteConfig, ThemeColors, load_site_config


@dataclass(frozen=True, slots=True)
class HyperNewsTheme(ThemeColors):
    """HN-inspired color palette."""

    primary: str = "#ff6600"
    primary_dark: str = "#e55b00"
    background: str = "#f6f6ef"
    surface: str = "#ffffff"
    text: str = "#333333"
    text_secondary: str = "#828282"
    border: str = "#e5e7eb"
    success: str = "#22c55e"
    warning: str = "#f59e0b"
    danger: str = "#dc2626"
    link: str = "#333333"
    mod_badge: str = "#5a9e6f"
    accent_gray: str = "#999999"


@dataclass(frozen=True, slots=True)
class NavItem:
    """A navigation link in the header."""

    url: str
    label: str


@dataclass(frozen=True, slots=True)
class EscalationConfig:
    """Auto-moderation escalation thresholds."""

    warn_to_mute_threshold: int = 3
    warn_to_mute_window_days: int = 365
    mute_duration_days: int = 7
    mute_to_ban_threshold: int = 2
    mute_to_ban_window_days: int = 30


@dataclass(frozen=True, slots=True)
class HyperNewsSiteConfig(SiteConfig):
    """Full HyperNews configuration.

    All defaults match current hardcoded values — deploying without
    a config file produces the exact same appearance.
    """

    name: str = "HyperNews"
    tagline: str = ""
    logo_icon: str = "Y"
    logo_url: str = ""
    favicon_url: str = ""
    footer_text: str = "Powered by HyperDjango"
    theme: HyperNewsTheme = field(default_factory=HyperNewsTheme)
    font_family: str = "Verdana, Geneva, sans-serif"
    base_font_size: str = "13px"

    # Navigation
    nav_items: tuple[NavItem, ...] = field(
        default_factory=lambda: (
            NavItem("/?tab=hot", "hot"),
            NavItem("/?tab=new", "new"),
            NavItem("/?tab=top", "top"),
            NavItem("/?tab=ask", "ask"),
            NavItem("/forums", "forums"),
            NavItem("/submit", "submit"),
            NavItem("/search", "search"),
        )
    )
    footer_links: tuple[NavItem, ...] = field(
        default_factory=lambda: (
            NavItem("/", "Home"),
            NavItem("/submit", "Submit"),
            NavItem("/admin/", "Admin"),
        )
    )

    # Feature limits
    max_pinned_per_forum: int = 3
    award_karma_cost: int = 10

    # Rate limits
    rate_limit_requests: int = 120
    rate_limit_window: int = 60

    # Escalation
    escalation: EscalationConfig = field(default_factory=EscalationConfig)

    # Security
    security_contact_email: str = "security@example.com"


def load_hypernews_config(config_path: Path | None = None) -> HyperNewsSiteConfig:
    """Load HyperNews config from TOML + env vars + defaults.

    If no ``config_path`` is given, looks for ``site.toml`` in the
    hypernews service directory.
    """
    if config_path is None:
        default_path = Path(__file__).parent / "site.toml"
        if default_path.exists():
            config_path = default_path
    return load_site_config(HyperNewsSiteConfig, "HYPERNEWS", config_path)
