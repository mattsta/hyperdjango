"""
Ticket split service — create a child ticket from a parent.
"""

from hyperdjango.logging import logger

from ..models import (
    ActivityAction,
    ActivityLog,
    Ticket,
    TicketStatusConfig,
)
from .ticket_numbers import next_ticket_number


async def split_ticket(
    parent: Ticket,
    title: str,
    description: str,
    actor_id: int,
    tenant_id: int,
    assignee_id: int = 0,
    team_id: int = 0,
) -> Ticket:
    """Create a child ticket split from a parent.

    Inherits priority, type, customer, and source from parent.
    Uses default status for the new ticket.
    """
    default_status = await TicketStatusConfig.objects.filter(is_default=True).first()
    status_id = default_status.id if default_status else parent.status_id

    child_number = await next_ticket_number(tenant_id)

    child = Ticket(
        tenant_id=tenant_id,
        ticket_number=child_number,
        title=title,
        description=description,
        status_id=status_id,
        priority_id=parent.priority_id,
        ticket_type_id=parent.ticket_type_id,
        customer_id=parent.customer_id,
        assignee_id=assignee_id or parent.assignee_id,
        team_id=team_id or parent.team_id,
        parent_ticket_id=parent.id,
        source=parent.source,
    )
    await child.save()

    # Activity log on parent
    await ActivityLog(
        tenant_id=tenant_id,
        ticket_id=parent.id,
        actor_type="agent",
        actor_id=actor_id,
        action=ActivityAction.SPLIT,
        detail=f'{{"child_ticket_id": {child.id}, "child_number": "{child_number}"}}',
    ).save()

    logger.info(
        "Split ticket {parent} → {child}",
        parent=parent.ticket_number,
        child=child_number,
    )

    return child
