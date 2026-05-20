"""
Comment and Attachment models.

Comments support public (customer-visible) and internal (agent-only) notes.
Attachments can be on tickets directly or attached to specific comments.
"""

from enum import Enum

from hyperdjango.mixins import SoftDeleteMixin, TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.public_id import IDMixin, IDMode, KeySlot
from hyperdjango.tenancy import TenantMixin

from .tickets import Ticket


class AuthorType(Enum):
    AGENT = "agent"
    CUSTOMER = "customer"
    SYSTEM = "system"


class Comment(TenantMixin, TimestampMixin, SoftDeleteMixin, IDMixin, Model):
    """Comment on a ticket — public or internal note.

    is_internal=True hides the comment from customers in portal serializers.
    mentioned_agent_ids drives @mention notifications.
    """

    class Meta:
        table = "ht_comments"
        indexes = [
            Index(fields=("ticket_id", "-created_at"), where="is_deleted = FALSE"),
        ]

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "7xhYZcW4jm8nGrTBqC2Ds6NQ5kXvF3Rp"
        hmac_keys = [KeySlot(key="ht-comments-key-2026-q2", offset=40000)]

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    author_type: AuthorType = Field(default=AuthorType.AGENT)
    author_id: int = Field(
        default=0
    )  # FK to Agent or Customer depending on author_type
    body: str = Field(default="")  # plain text
    body_html: str = Field(default="")  # rendered HTML
    is_internal: bool = Field(default=False)  # True = agent-only note
    mentioned_agent_ids: str = Field(default="[]")  # JSON array of agent IDs


class Attachment(TenantMixin, TimestampMixin, Model):
    """File attachment on a ticket or comment."""

    class Meta:
        table = "ht_attachments"

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    comment_id: int = Field(default=0)  # FK to Comment (0 = ticket-level attachment)
    filename: str = Field()
    content_type: str = Field(default="application/octet-stream")
    size_bytes: int = Field(default=0)
    storage_path: str = Field()  # path on FileSystemStorage
    uploaded_by_type: AuthorType = Field(default=AuthorType.AGENT)
    uploaded_by_id: int = Field(default=0)


class CannedResponse(TenantMixin, TimestampMixin, Model):
    """Pre-written response template. Applied to tickets via @action."""

    class Meta:
        table = "ht_canned_responses"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
    body_html: str = Field(default="")
    shortcut: str = Field(default="")  # quick-insert shortcode
    category: str = Field(default="")  # grouping label
    created_by_id: int = Field(default=0)  # FK to Agent


class SatisfactionRating(TenantMixin, TimestampMixin, Model):
    """Customer satisfaction rating (CSAT) on a resolved ticket."""

    class Meta:
        table = "ht_satisfaction_ratings"

    id: int = Field(primary_key=True, auto=True)
    ticket_id: int = Field(foreign_key=Ticket)
    customer_id: int = Field(default=0)  # FK to Customer
    score: int = Field(default=0)  # 1-5
    comment: str = Field(default="")
