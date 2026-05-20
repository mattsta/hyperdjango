"""
Agent and Customer user models.

Agents are internal support staff (admin, team_lead, agent).
Customers are external users who submit and track tickets.
Both use IDMixin for opaque public-facing IDs (IDOR prevention).
"""

from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.public_id import IDMixin, IDMode, KeySlot
from hyperdjango.tenancy import TenantMixin
from hyperdjango.timeline import StatusTimelineMixin


class AgentRole(Enum):
    ADMIN = "admin"
    TEAM_LEAD = "team_lead"
    AGENT = "agent"


class Agent(TenantMixin, StatusTimelineMixin, TimestampMixin, IDMixin):
    """Internal support agent. Tenant-scoped, opaque public IDs.

    Status categories (via StatusTimelineMixin):
    - lifecycle: deactivated — a new agent has no events and is active by default.
      Check deactivation via has_status("lifecycle", "deactivated").
    """

    class Meta:
        table = "ht_agents"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "9QkXvF3Rp7xhYZcW4jm8nGrTBqC2Ds6N"
        hmac_keys = [KeySlot(key="ht-agents-key-2026-q2", offset=10000)]

    class TimelineConfig:
        entity_type = "agent"
        categories = {"lifecycle": ["active", "deactivated"]}

    id: int = Field(primary_key=True, auto=True)
    email: str = Field()
    display_name: str = Field(default="")
    password_hash: str = Field(exclude=True)
    role: AgentRole = Field(default=AgentRole.AGENT)
    avatar_url: str = Field(default="")
    max_concurrent_tickets: int = Field(default=25)


class Customer(TenantMixin, TimestampMixin, IDMixin, Model):
    """External customer who submits and tracks tickets via the portal."""

    class Meta:
        table = "ht_customers"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "Hg5M8pQYr3k7FvBxN4C2jTRnWc6mDsZ9"
        hmac_keys = [KeySlot(key="ht-customers-key-2026-q2", offset=20000)]

    id: int = Field(primary_key=True, auto=True)
    email: str = Field()
    display_name: str = Field(default="")
    password_hash: str = Field(exclude=True)
    is_verified: bool = Field(default=False)
    metadata: str = Field(default="{}")  # JSON: custom per-customer data
