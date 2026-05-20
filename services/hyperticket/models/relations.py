"""
Ticket relationship model — related, blocks, blocked_by, duplicates.
"""

from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin

from .tickets import Ticket


class RelationType(Enum):
    RELATED = "related"
    BLOCKS = "blocks"
    BLOCKED_BY = "blocked_by"
    DUPLICATES = "duplicates"


class TicketRelation(TenantMixin, TimestampMixin, Model):
    """Directed relationship between two tickets."""

    class Meta:
        table = "ht_ticket_relations"

    id: int = Field(primary_key=True, auto=True)
    source_ticket_id: int = Field(foreign_key=Ticket)
    target_ticket_id: int = Field(foreign_key=Ticket)
    relation_type: RelationType = Field(default=RelationType.RELATED)
