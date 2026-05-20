"""
HyperTicket models — re-exports all models and enums for convenient access.

Usage:
    from services.hyperticket.models import Org, Ticket, Agent, Customer, Team
"""

from .activity import ActivityAction, ActivityLog
from .comments import (
    Attachment,
    AuthorType,
    CannedResponse,
    Comment,
    SatisfactionRating,
)
from .notifications import NotificationEvent, NotificationPreference
from .org import (
    AssignmentStrategy,
    Org,
    OrgAPIKey,
    OrgSettings,
    PlanConfig,
    PlanFeatureLimit,
    QuotaEnforcement,
    TenantTheme,
)
from .relations import RelationType, TicketRelation
from .sla import EscalationRule, EscalationTrigger, SLAInstance, SLAPolicy
from .tags import Tag, TicketTag
from .teams import AgentSkill, Team, TeamMembership
from .templates import TicketTemplate
from .tickets import Ticket, TicketSource
from .users import Agent, AgentRole, Customer
from .views import SavedView
from .workflow import Approval, ApprovalStatus, WorkflowRule, WorkflowTrigger
from .workflow_config import (
    PriorityConfig,
    StatusCategory,
    StatusTransition,
    TicketStatusConfig,
    TicketTypeConfig,
)

__all__ = [
    # Org & Plans
    "Org",
    "PlanConfig",
    "PlanFeatureLimit",
    "OrgSettings",
    "OrgAPIKey",
    "TenantTheme",
    "AssignmentStrategy",
    "QuotaEnforcement",
    # Users
    "Agent",
    "AgentRole",
    "Customer",
    # Teams
    "Team",
    "TeamMembership",
    "AgentSkill",
    # Workflow Config (DB-driven)
    "TicketStatusConfig",
    "StatusTransition",
    "PriorityConfig",
    "TicketTypeConfig",
    "StatusCategory",
    # Tickets
    "Ticket",
    "TicketSource",
    "Tag",
    "TicketTag",
    "TicketRelation",
    "RelationType",
    # Communication
    "Comment",
    "Attachment",
    "CannedResponse",
    "SatisfactionRating",
    "AuthorType",
    # SLA
    "SLAPolicy",
    "SLAInstance",
    "EscalationRule",
    "EscalationTrigger",
    # Workflow & Approvals
    "WorkflowRule",
    "WorkflowTrigger",
    "Approval",
    "ApprovalStatus",
    # Templates & Views
    "TicketTemplate",
    "SavedView",
    # Activity & Notifications
    "ActivityLog",
    "ActivityAction",
    "NotificationPreference",
    "NotificationEvent",
]
