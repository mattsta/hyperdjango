"""HyperTicket site configuration — white-label branding & theming.

All values default to the current hardcoded HyperTicket appearance.
Override via TOML file (``site.toml``) or environment variables
(``HYPERTICKET_NAME``, ``HYPERTICKET_THEME_PRIMARY``, etc.).

Usage:
    from services.hyperticket.config import load_hyperticket_config

    config = load_hyperticket_config()
    app = HyperApp(site_config=config)

    # Or with a custom TOML:
    config = load_hyperticket_config(Path("my_brand.toml"))
"""

from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango.site_config import SiteConfig, ThemeColors, load_site_config


@dataclass(frozen=True, slots=True)
class HyperTicketTheme(ThemeColors):
    """Professional support-desk color palette."""

    primary: str = "#1a73e8"
    primary_dark: str = "#1557b0"
    background: str = "#f8f9fa"
    surface: str = "#ffffff"
    text: str = "#202124"
    text_secondary: str = "#6b7280"
    border: str = "#e5e7eb"
    success: str = "#22c55e"
    warning: str = "#f59e0b"
    danger: str = "#dc2626"
    link: str = "#1a73e8"
    header_bg: str = "#1a73e8"
    header_text: str = "#ffffff"


@dataclass(frozen=True, slots=True)
class PortalLabels:
    """Customer-facing portal label strings."""

    welcome_message: str = "Welcome to the Support Portal"
    submit_label: str = "Submit a Request"
    empty_tickets: str = "You have no open requests."


@dataclass(frozen=True, slots=True)
class DashboardLabels:
    """Agent dashboard label strings."""

    total_tickets: str = "Total Tickets"
    open_tickets: str = "Open Tickets"
    unassigned: str = "Unassigned"
    sla_compliance: str = "SLA Compliance"


@dataclass(frozen=True, slots=True)
class HyperTicketSiteConfig(SiteConfig):
    """Full HyperTicket configuration.

    All defaults match current hardcoded values -- deploying without
    a config file produces the exact same appearance.
    """

    name: str = "HyperTicket"
    tagline: str = ""
    logo_icon: str = ""
    logo_url: str = ""
    favicon_url: str = ""
    footer_text: str = "Powered by HyperDjango"
    theme: HyperTicketTheme = field(default_factory=HyperTicketTheme)
    font_family: str = "Inter, system-ui, sans-serif"
    base_font_size: str = "14px"

    # Labels
    portal_labels: PortalLabels = field(default_factory=PortalLabels)
    dashboard_labels: DashboardLabels = field(default_factory=DashboardLabels)

    # Feature limits
    rate_limit_requests: int = 120
    rate_limit_window: int = 60

    # Security
    security_contact_email: str = "security@example.com"


def load_hyperticket_config(config_path: Path | None = None) -> HyperTicketSiteConfig:
    """Load HyperTicket config from TOML + env vars + defaults.

    If no ``config_path`` is given, looks for ``site.toml`` in the
    hyperticket service directory.
    """
    if config_path is None:
        default_path = Path(__file__).parent / "site.toml"
        if default_path.exists():
            config_path = default_path
    return load_site_config(HyperTicketSiteConfig, "HYPERTICKET", config_path)
