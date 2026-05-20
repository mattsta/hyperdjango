"""
Async workflow action execution task.

Dispatches workflow actions that may be slow (external notifications,
API calls to integrations, etc.).
"""

from hyperdjango.logging import logger

from ..models import Ticket, WorkflowTrigger
from ..services.workflow_engine import evaluate_rules


async def execute_workflow_rules(ticket_id: int, trigger_event: str) -> int:
    """Background task: evaluate and execute workflow rules for a ticket.

    Returns the number of rules that matched.
    """
    ticket = await Ticket.objects.filter(id=ticket_id).first()
    if not ticket:
        logger.warning("workflow task: ticket {tid} not found", tid=ticket_id)
        return 0

    try:
        trigger = WorkflowTrigger(trigger_event)
    except ValueError:
        logger.warning("workflow task: unknown trigger {t}", t=trigger_event)
        return 0

    return await evaluate_rules(ticket, trigger)
