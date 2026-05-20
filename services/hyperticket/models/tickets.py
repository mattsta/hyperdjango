"""
Ticket model — the core entity of HyperTicket.

Uses all major platform mixins: TenantMixin (multi-tenancy), TimestampMixin (auto-timestamps),
SoftDeleteMixin (soft delete), VersionedMixin (append-only audit trail), IDMixin (opaque IDs).

Status, priority, and type reference DB-driven config models (not hardcoded enums).
"""

from datetime import datetime
from enum import Enum

from hyperdjango.mixins import SoftDeleteMixin, TimestampMixin, VersionedMixin
from hyperdjango.models import Field, Index
from hyperdjango.public_id import IDMixin, IDMode, KeySlot
from hyperdjango.tenancy import TenantMixin
from hyperdjango.timeline import StatusTimelineMixin

from .users import Customer
from .workflow_config import (
    PriorityConfig,
    TicketStatusConfig,
    TicketTypeConfig,
)


class TicketSource(Enum):
    WEB = "web"
    EMAIL = "email"
    API = "api"
    PORTAL = "portal"


class Ticket(
    TenantMixin,
    StatusTimelineMixin,
    TimestampMixin,
    SoftDeleteMixin,
    VersionedMixin,
    IDMixin,
):
    """Support ticket — the central entity.

    References DB-driven workflow configs for status/priority/type.
    Supports parent/child (split) and merge relationships.

    Status categories (via StatusTimelineMixin):
    - state: locked, muted — enforced via has_status("state", "locked") / ("state", "muted")
    """

    class Meta:
        table = "ht_tickets"
        indexes = [
            Index(fields=("tenant_id", "status_id"), where="is_deleted = FALSE"),
            Index(fields=("tenant_id", "assignee_id"), where="is_deleted = FALSE"),
            Index(fields=("tenant_id", "team_id"), where="is_deleted = FALSE"),
            Index(fields=("tenant_id", "-created_at"), where="is_deleted = FALSE"),
            Index(fields=("tenant_id", "priority_id"), where="is_deleted = FALSE"),
            Index(fields=("tenant_id", "ticket_number"), unique=True),
            Index(
                expressions=("to_tsvector('english', title || ' ' || description)",),
                using="gin",
                name="ix_ht_tickets_search",
            ),
            Index(fields=("title",), using="gin", opclasses=("gin_trgm_ops",)),
        ]

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "5kXvF3Rp7xhYZcW4jm8nGrTBqC2Ds6NQ"
        hmac_keys = [KeySlot(key="ht-tickets-key-2026-q2", offset=30000)]

    class TimelineConfig:
        entity_type = "ticket"
        categories = {"state": ["locked", "muted"]}

    id: int = Field(primary_key=True, auto=True)
    ticket_number: str = Field()  # org-prefixed sequential: "ACME-0042"
    title: str = Field()
    description: str = Field(default="")  # plain text
    body_html: str = Field(default="")  # rendered HTML
    # Configurable workflow FKs (DB-driven, per-org)
    status_id: int = Field(foreign_key=TicketStatusConfig)
    priority_id: int = Field(foreign_key=PriorityConfig)
    ticket_type_id: int = Field(foreign_key=TicketTypeConfig)
    # Assignment
    assignee_id: int = Field(default=0)  # FK to Agent (0 = unassigned)
    team_id: int = Field(default=0)  # FK to Team (0 = no team)
    customer_id: int = Field(foreign_key=Customer)
    source: TicketSource = Field(default=TicketSource.WEB)
    # SLA tracking
    sla_policy_id: int = Field(default=0)  # FK to SLAPolicy (0 = default)
    first_response_due: datetime | None = Field(default=None)
    resolution_due: datetime | None = Field(default=None)
    first_responded_at: datetime | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)
    # Relationships
    parent_ticket_id: int = Field(default=0)  # FK self — child from split
    merged_into_id: int = Field(default=0)  # FK self — merged target
    # Extensibility
    custom_fields: str = Field(default="{}")  # JSON per org schema
