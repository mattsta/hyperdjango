"""
SLA policy, instance tracking, and escalation rule models.

SLAPolicy defines per-priority response/resolution targets in business minutes.
SLAInstance tracks actual SLA performance per ticket.
EscalationRule defines automatic escalation on SLA breach.
"""

from datetime import datetime
from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.tenancy import TenantMixin

from .tickets import Ticket


class EscalationTrigger(Enum):
    RESPONSE_BREACH = "response_breach"
    RESOLUTION_BREACH = "resolution_breach"
    RESPONSE_WARNING = "response_warning"  # e.g. 80% of target
    RESOLUTION_WARNING = "resolution_warning"


class SLAPolicy(TenantMixin, TimestampMixin, Model):
    """SLA targets per priority level, in business minutes.

    conditions (JSON) enables auto-apply: match ticket attributes to policy.
    """

    class Meta:
        table = "ht_sla_policies"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    is_default: bool = Field(default=False)
    # First response targets (business minutes)
    first_response_critical: int = Field(default=15)
    first_response_high: int = Field(default=60)
    first_response_normal: int = Field(default=240)
    first_response_low: int = Field(default=480)
    # Resolution targets (business minutes)
    resolution_critical: int = Field(default=120)
    resolution_high: int = Field(default=480)
    resolution_normal: int = Field(default=1440)
    resolution_low: int = Field(default=2880)
    # Auto-apply conditions
    conditions: str = Field(
        default="{}"
    )  # JSON: match ticket type, source, customer, etc.


class SLAInstance(TenantMixin, TimestampMixin, Model):
    """Tracks SLA compliance for a specific ticket.

    Created when a ticket is assigned an SLA policy.
    Updated by the SLA engine cron task and signal handlers.
    """

    class Meta:
        table = "ht_sla_instances"
        indexes = [
            Index(fields=("tenant_id",), where="breached = FALSE"),
        ]

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    sla_policy_id: int = Field(foreign_key=SLAPolicy)
    first_response_target: datetime | None = Field(default=None)
    resolution_target: datetime | None = Field(default=None)
    first_response_met: int = Field(default=-1)  # -1=pending, 0=missed, 1=met
    resolution_met: int = Field(default=-1)  # -1=pending, 0=missed, 1=met
    paused_at: datetime | None = Field(default=None)  # set when SLA clock is paused
    paused_duration_minutes: int = Field(default=0)  # accumulated pause time
    breached: bool = Field(default=False)


class EscalationRule(TenantMixin, TimestampMixin, Model):
    """Automatic escalation when SLA breach occurs or approaches."""

    class Meta:
        table = "ht_escalation_rules"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    trigger_type: EscalationTrigger = Field(default=EscalationTrigger.RESPONSE_BREACH)
    trigger_minutes: int = Field(default=0)  # minutes before/after target
    escalate_to_team_id: int = Field(default=0)  # FK to Team (0 = no team escalation)
    escalate_to_agent_id: int = Field(
        default=0
    )  # FK to Agent (0 = no agent escalation)
    notify_team_lead: bool = Field(default=True)
    is_active: bool = Field(default=True)
