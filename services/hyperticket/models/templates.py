"""
Ticket template and canned response models.

TicketTemplate pre-fills ticket fields for common request types.
CannedResponse is in comments.py (co-located with Comment model).
"""

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin


class TicketTemplate(TenantMixin, TimestampMixin, Model):
    """Pre-filled ticket template for common request types."""

    class Meta:
        table = "ht_ticket_templates"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    ticket_type_id: int = Field(default=0)  # FK to TicketTypeConfig
    priority_id: int = Field(default=0)  # FK to PriorityConfig
    default_title: str = Field(default="")
    default_body: str = Field(default="")
    default_tags: str = Field(default="[]")  # JSON array of tag IDs
    default_team_id: int = Field(default=0)  # FK to Team
    custom_fields: str = Field(default="{}")  # JSON pre-filled custom field values
