"""
Ticket merge service — move comments/tags from source to target, close source.
"""

from hyperdjango.database import get_db
from hyperdjango.logging import logger

from ..models import (
    ActivityAction,
    ActivityLog,
    Comment,
    Ticket,
    TicketStatusConfig,
    TicketTag,
)


async def merge_tickets(
    source: Ticket,
    target: Ticket,
    actor_id: int,
    tenant_id: int,
) -> None:
    """Merge source ticket into target.

    Moves all comments and tags from source to target.
    Closes source with merged_into_id set.
    """
    db = get_db()
    async with db.transaction():
        # Move comments
        await Comment.objects.filter(ticket_id=source.id).update(ticket_id=target.id)

        # Move tags (skip duplicates via checking existence)
        source_tags = await TicketTag.objects.filter(ticket_id=source.id).all()
        for st in source_tags:
            existing = await TicketTag.objects.filter(
                ticket_id=target.id, tag_id=st.tag_id
            ).first()
            if not existing:
                await TicketTag(
                    tenant_id=tenant_id,
                    ticket_id=target.id,
                    tag_id=st.tag_id,
                ).save()
        # Remove source tags
        await TicketTag.objects.filter(ticket_id=source.id).delete()

        # Close source
        closed_status = await TicketStatusConfig.objects.filter(
            is_terminal=True
        ).first()
        closed_id = closed_status.id if closed_status else source.status_id
        await Ticket.objects.filter(id=source.id).update(
            merged_into_id=target.id,
            status_id=closed_id,
        )

    # Activity log
    await ActivityLog(
        tenant_id=tenant_id,
        ticket_id=source.id,
        actor_type="agent",
        actor_id=actor_id,
        action=ActivityAction.MERGED,
        detail=f'{{"target_ticket_id": {target.id}, "target_number": "{target.ticket_number}"}}',
    ).save()

    logger.info(
        "Merged ticket {src} into {tgt}",
        src=source.ticket_number,
        tgt=target.ticket_number,
    )
