"""
Maintenance tasks — auto-close resolved tickets + cleanup.

Scheduled via TaskScheduler cron:
  - auto_close_resolved: daily 2AM — close tickets resolved >7 days ago
  - cleanup_old_activity: weekly — prune activity logs beyond retention
"""

from datetime import UTC, datetime, timedelta

from hyperdjango.database import get_db
from hyperdjango.logging import logger

from ..models import ActivityAction, ActivityLog, Ticket, TicketStatusConfig


async def auto_close_resolved(days_threshold: int = 7) -> int:
    """Close tickets that have been in 'resolved' status for N days.

    Returns the number of tickets auto-closed.
    """
    db = get_db()
    cutoff = datetime.now(UTC) - timedelta(days=days_threshold)

    # Find resolved (non-terminal) status IDs
    resolved_statuses = await TicketStatusConfig.objects.filter(
        category="solved", is_terminal=False
    ).all()
    if not resolved_statuses:
        return 0

    resolved_ids = [s.id for s in resolved_statuses]

    # Find terminal status for closing
    closed_status = await TicketStatusConfig.objects.filter(is_terminal=True).first()
    if not closed_status:
        return 0

    # Find tickets resolved before cutoff
    tickets = await Ticket.objects.filter(
        status_id__in=resolved_ids,
        is_deleted=False,
    ).all()

    closed_count = 0
    for ticket in tickets:
        updated = ticket.updated_at
        if not updated:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if updated < cutoff:
            await Ticket.objects.filter(id=ticket.id).update(status_id=closed_status.id)
            await ActivityLog(
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                actor_type="system",
                actor_id=0,
                action=ActivityAction.STATUS_CHANGED,
                detail=f'{{"auto_close": true, "old_status_id": {ticket.status_id}, "new_status_id": {closed_status.id}}}',
            ).save()
            closed_count += 1

    if closed_count:
        logger.info("Auto-closed {n} resolved tickets", n=closed_count)

    return closed_count


async def cleanup_old_activity(retention_days: int = 365) -> int:
    """Remove activity log entries older than retention period.

    Returns the number of entries removed.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = await ActivityLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info("Cleaned up activity logs older than {d} days", d=retention_days)
    return deleted
