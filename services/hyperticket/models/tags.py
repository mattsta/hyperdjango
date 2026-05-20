"""
Tag and TicketTag junction models.

Tags provide colored labels for organizing tickets.
Tags also serve as skill identifiers for agent routing.
"""

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.tenancy import TenantMixin

from .tickets import Ticket


class Tag(TenantMixin, TimestampMixin, Model):
    """Colored label for categorizing tickets. Also used for skill-based routing."""

    class Meta:
        table = "ht_tags"
        indexes = [
            Index(fields=("tenant_id", "name"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    color: str = Field(default="#6b7280")
    description: str = Field(default="")


class TicketTag(TenantMixin, TimestampMixin, Model):
    """Junction: ticket <-> tag. Many-to-many relationship."""

    class Meta:
        table = "ht_ticket_tags"

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    tag_id: int = Field(foreign_key=Tag)
