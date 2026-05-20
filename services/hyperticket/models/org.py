"""
Organization, plans, settings, API keys, and tenant theming models.

Org is the tenant root — does NOT use TenantMixin because it IS the tenant.
PlanConfig + PlanFeatureLimit provide multi-dimensional, DB-configurable SaaS plans.
TenantTheme enables per-tenant branding across portal, admin, and email.
"""

from datetime import datetime
from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey
from hyperdjango.tenancy import TenantMixin


class AssignmentStrategy(Enum):
    ROUND_ROBIN = "round_robin"
    SKILL_BASED = "skill_based"
    MANUAL = "manual"


class QuotaEnforcement(Enum):
    REJECT = "reject"
    WARN = "warn"
    THROTTLE = "throttle"
    SOFT = "soft"  # log only


# ---------------------------------------------------------------------------
# Org — tenant root entity
# ---------------------------------------------------------------------------


class Org(TimestampMixin, Model):
    """Organization (tenant root). NOT scoped by TenantMixin — it IS the tenant."""

    class Meta:
        table = "ht_orgs"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    slug: str = Field(unique=True)
    is_active: bool = Field(default=True)
    plan_config_id: int = Field(default=0)  # FK to PlanConfig (0 = no plan)
    support_email: str = Field(default="")
    custom_domain: str = Field(default="")


# ---------------------------------------------------------------------------
# PlanConfig + PlanFeatureLimit — multi-dimensional SaaS plans
# ---------------------------------------------------------------------------


class PlanConfig(TimestampMixin, Model):
    """Configurable SaaS plan — N feature dimensions, no hardcoded tiers.

    Example plans: "Starter", "Professional", "Enterprise", or custom per-org.
    Each plan has N PlanFeatureLimit rows defining its limits.
    """

    class Meta:
        table = "ht_plan_configs"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    description: str = Field(default="")
    is_public: bool = Field(default=True)  # visible on pricing page
    display_order: int = Field(default=0)
    stripe_price_id: str = Field(default="")  # external billing ref
    base_price_cents: int = Field(default=0)


class PlanFeatureLimit(TimestampMixin, Model):
    """One dimension of a plan's limits.

    Add new feature dimensions by inserting rows — zero code changes.
    Feature keys: agent_seats, tickets_per_month, storage_bytes,
    ai_tokens_per_month, sso_enabled, custom_fields_count,
    api_requests_per_month, webhook_count, audit_retention_days.

    limit_value: -1 = unlimited, 0 = disabled, >0 = hard limit.
    """

    class Meta:
        table = "ht_plan_feature_limits"
        indexes = [
            Index(fields=("plan_config_id", "feature_key"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    plan_config_id: int = Field(foreign_key=PlanConfig)
    feature_key: str = Field()  # e.g. "agent_seats"
    limit_value: float = Field(default=0.0)
    enforcement: QuotaEnforcement = Field(default=QuotaEnforcement.REJECT)
    overage_price_cents: int = Field(default=0)  # 0 = hard cap


# ---------------------------------------------------------------------------
# OrgSettings — per-tenant business configuration
# ---------------------------------------------------------------------------


class OrgSettings(TenantMixin, TimestampMixin, Model):
    """Per-org business rules: business hours, custom fields, assignment strategy."""

    class Meta:
        table = "ht_org_settings"
        indexes = [
            Index(fields=("tenant_id",), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    business_hours: str = Field(default="{}")  # JSON: {"mon": ["09:00", "17:00"], ...}
    holidays: str = Field(default="[]")  # JSON: ["2026-01-01", "2026-12-25", ...]
    timezone: str = Field(default="UTC")
    custom_fields_schema: str = Field(
        default="[]"
    )  # JSON: [{"name": ..., "type": ...}, ...]
    default_sla_policy_id: int = Field(default=0)
    auto_assignment_strategy: AssignmentStrategy = Field(
        default=AssignmentStrategy.ROUND_ROBIN
    )


# ---------------------------------------------------------------------------
# OrgAPIKey — hashed API key storage with scoped permissions
# ---------------------------------------------------------------------------


class OrgAPIKey(SignedAPIKeyMixin, TenantMixin, TimestampMixin):
    """API key for programmatic access.

    SignedAPIKeyMixin provides: key_hash (SHA-256), key_prefix, is_active,
    expires_at, scopes, generate() classmethod, verify() classmethod.
    TenantMixin provides: tenant_id auto-scoping.
    """

    class Meta:
        table = "ht_org_api_keys"

    class TokenConfig:
        keys = [SigningKey(secret="ht-apikey-signing-2026-q2", version=1)]
        key_display_prefix = "sk_ht_"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(default="")
    last_used_at: datetime | None = Field(default=None)  # updated on use


# ---------------------------------------------------------------------------
# TenantTheme — per-tenant visual identity
# ---------------------------------------------------------------------------


class TenantTheme(TenantMixin, TimestampMixin, Model):
    """Per-tenant branding: colors, typography, logo, custom CSS, email templates.

    Portal, admin, and email rendering use these values to skin the UI.
    """

    class Meta:
        table = "ht_tenant_themes"
        indexes = [
            Index(fields=("tenant_id",), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    # Brand identity
    logo_url: str = Field(default="")
    favicon_url: str = Field(default="")
    company_name_display: str = Field(default="")  # rendered in UI header
    # Color palette
    primary_color: str = Field(default="#1a73e8")
    secondary_color: str = Field(default="#f8f9fa")
    accent_color: str = Field(default="#34a853")
    text_color: str = Field(default="#202124")
    background_color: str = Field(default="#ffffff")
    header_background: str = Field(default="#1a73e8")
    header_text_color: str = Field(default="#ffffff")
    # Typography
    font_family: str = Field(default="Inter, system-ui, sans-serif")
    heading_font_family: str = Field(default="")  # empty = same as font_family
    # Custom CSS (injected into portal <head>)
    custom_css: str = Field(default="")
    # Email templates
    email_header_html: str = Field(default="")
    email_footer_html: str = Field(default="")
    email_accent_color: str = Field(default="#1a73e8")
    # Portal customization
    portal_welcome_message: str = Field(default="How can we help?")
    portal_submit_label: str = Field(default="Submit a request")
