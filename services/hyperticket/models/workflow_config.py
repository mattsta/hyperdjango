"""
DB-driven configurable workflow models: statuses, priorities, types, transitions.

These are NOT hardcoded enums. Each org configures its own values.
System provides sensible defaults on org creation via seed.py.
Ticket model references these via FK instead of enum fields.
"""

from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.tenancy import TenantMixin


class StatusCategory(Enum):
    """Fixed categories for reporting/SLA grouping.

    Custom statuses map to one of these. This IS an enum because it controls
    SLA clock behavior and reporting aggregation — not user-customizable.
    """

    OPEN = "open"
    PENDING = "pending"
    SOLVED = "solved"
    CLOSED = "closed"


class TicketStatusConfig(TenantMixin, TimestampMixin, Model):
    """Per-org ticket statuses. Org admin can add/rename/reorder/disable."""

    class Meta:
        table = "ht_ticket_status_configs"
        indexes = [
            Index(fields=("tenant_id", "slug"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    slug: str = Field()  # "open", "in_progress", "waiting_on_customer"
    label: str = Field()  # "Open", "In Progress", "Waiting on Customer"
    color: str = Field(default="#6b7280")
    icon: str = Field(default="")  # optional icon name
    category: StatusCategory = Field(default=StatusCategory.OPEN)
    is_default: bool = Field(default=False)  # used when no explicit status
    is_terminal: bool = Field(default=False)  # SLA clock stops
    pauses_sla: bool = Field(default=False)  # SLA clock pauses
    sort_order: int = Field(default=0)
    is_active: bool = Field(default=True)


class StatusTransition(TenantMixin, TimestampMixin, Model):
    """Allowed transitions between statuses. Per-org state machine.

    If no transition row exists for a (from, to) pair, the transition is blocked.
    """

    class Meta:
        table = "ht_status_transitions"

    id: int = Field(primary_key=True, auto=True)
    from_status_id: int = Field(foreign_key=TicketStatusConfig)
    to_status_id: int = Field(foreign_key=TicketStatusConfig)
    requires_role: str = Field(
        default="agent"
    )  # minimum role: "agent", "team_lead", "admin"
    requires_comment: bool = Field(default=False)
    requires_approval: bool = Field(default=False)


class PriorityConfig(TenantMixin, TimestampMixin, Model):
    """Per-org priority levels with SLA time multipliers."""

    class Meta:
        table = "ht_priority_configs"
        indexes = [
            Index(fields=("tenant_id", "slug"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    slug: str = Field()  # "critical", "high", "normal", "low"
    label: str = Field()
    color: str = Field(default="#6b7280")
    icon: str = Field(default="")
    sla_multiplier: float = Field(default=1.0)  # multiplied against SLA policy times
    sort_order: int = Field(default=0)
    is_default: bool = Field(default=False)
    is_active: bool = Field(default=True)


class TicketTypeConfig(TenantMixin, TimestampMixin, Model):
    """Per-org ticket types with default routing."""

    class Meta:
        table = "ht_ticket_type_configs"
        indexes = [
            Index(fields=("tenant_id", "slug"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    slug: str = Field()  # "bug", "feature", "question", "task", "incident"
    label: str = Field()
    color: str = Field(default="#6b7280")
    icon: str = Field(default="")
    description: str = Field(default="")
    default_priority_id: int = Field(
        default=0
    )  # FK to PriorityConfig (0 = org default)
    default_team_id: int = Field(default=0)  # FK to Team (0 = no auto-routing)
    sort_order: int = Field(default=0)
    is_active: bool = Field(default=True)
