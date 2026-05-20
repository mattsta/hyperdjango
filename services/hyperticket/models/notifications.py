"""
Notification preference model — per-agent channel preferences.
"""

from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin

from .users import Agent


class NotificationEvent(Enum):
    TICKET_ASSIGNED = "ticket_assigned"
    TICKET_UPDATED = "ticket_updated"
    COMMENT_ADDED = "comment_added"
    MENTION = "mention"
    SLA_WARNING = "sla_warning"
    SLA_BREACH = "sla_breach"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"


class NotificationPreference(TenantMixin, TimestampMixin, Model):
    """Per-agent notification channel preferences for each event type."""

    class Meta:
        table = "ht_notification_preferences"

    id: int = Field(primary_key=True, auto=True)
    agent_id: int = Field(foreign_key=Agent)
    event_type: NotificationEvent = Field(default=NotificationEvent.TICKET_ASSIGNED)
    channel_email: bool = Field(default=True)
    channel_in_app: bool = Field(default=True)
    channel_websocket: bool = Field(default=True)
