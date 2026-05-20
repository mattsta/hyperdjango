"""
Workflow automation and approval models.

WorkflowRule defines condition→action automation triggered by post_save signals.
Approval gates status transitions that require sign-off.
"""

from datetime import datetime
from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin

from .tickets import Ticket
from .users import Agent


class WorkflowTrigger(Enum):
    TICKET_CREATED = "ticket_created"
    TICKET_UPDATED = "ticket_updated"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    ASSIGNED = "assigned"
    COMMENT_ADDED = "comment_added"
    SLA_BREACHED = "sla_breached"


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class WorkflowRule(TenantMixin, TimestampMixin, Model):
    """Condition→action automation rule.

    conditions (JSON): {"field": "priority_id", "op": "eq", "value": 1}
    actions (JSON): [{"type": "assign_team", "team_id": 5}, {"type": "add_tag", "tag": "urgent"}]
    """

    class Meta:
        table = "ht_workflow_rules"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    trigger_event: WorkflowTrigger = Field(default=WorkflowTrigger.TICKET_CREATED)
    conditions: str = Field(default="{}")  # JSON condition tree
    actions: str = Field(default="[]")  # JSON action list
    is_active: bool = Field(default=True)
    execution_order: int = Field(default=0)  # lower = runs first


class Approval(TenantMixin, TimestampMixin, Model):
    """Approval gate for status transitions requiring sign-off."""

    class Meta:
        table = "ht_approvals"

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    requested_by_id: int = Field(foreign_key=Agent)
    approver_id: int = Field(default=0)  # FK to Agent (0 = not yet assigned)
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING)
    comment: str = Field(default="")
    decided_at: datetime | None = Field(default=None)
