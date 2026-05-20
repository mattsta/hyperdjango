"""
Auto-assignment background task.

Called from post_save signal when a ticket is created without an assignee.
"""

from hyperdjango.logging import logger

from ..models import Ticket
from ..services.assignment import auto_assign


async def auto_assign_ticket(ticket_id: int) -> int | None:
    """Background task: auto-assign a ticket based on org strategy.

    Returns assigned agent_id or None.
    """
    ticket = await Ticket.objects.filter(id=ticket_id).first()
    if not ticket:
        logger.warning("auto_assign: ticket {tid} not found", tid=ticket_id)
        return None

    if ticket.assignee_id:
        return ticket.assignee_id  # Already assigned

    return await auto_assign(ticket)
