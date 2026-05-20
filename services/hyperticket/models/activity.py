"""
Activity log model — immutable audit trail of all ticket changes.

Every create, update, status change, assignment, comment, etc. is recorded
by post_save signal handlers. Used for ticket timeline rendering.
"""

from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.tenancy import TenantMixin

from .tickets import Ticket


class ActivityAction(Enum):
    CREATED = "created"
    UPDATED = "updated"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    TEAM_CHANGED = "team_changed"
    COMMENT_ADDED = "comment_added"
    TAG_ADDED = "tag_added"
    TAG_REMOVED = "tag_removed"
    MERGED = "merged"
    SPLIT = "split"
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    REOPENED = "reopened"
    DELETED = "deleted"
    RESTORED = "restored"
    SLA_BREACHED = "sla_breached"
    ESCALATED = "escalated"


class ActivityLog(TenantMixin, TimestampMixin, Model):
    """Immutable audit trail entry for a ticket event."""

    class Meta:
        table = "ht_activity_log"
        indexes = [
            Index(fields=("ticket_id", "-created_at")),
        ]

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    actor_type: str = Field(default="agent")  # "agent", "customer", "system"
    actor_id: int = Field(default=0)
    action: ActivityAction = Field(default=ActivityAction.CREATED)
    detail: str = Field(
        default="{}"
    )  # JSON: {"old_status": "open", "new_status": "closed"}
