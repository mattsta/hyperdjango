"""
Signal-to-broadcast handlers — connect model save signals to realtime channels.

Broadcasts ticket events to appropriate channels when tickets/comments change.
"""

from hyperdjango.signals import post_save
from hyperdjango.tenancy import get_tenant

from ..models import Comment, Ticket
from .channels import broadcast_notification, broadcast_ticket_event


@post_save.connect
async def broadcast_ticket_changes(sender, instance, created, **kwargs):
    """Broadcast ticket create/update events to realtime channels."""
    if sender is not Ticket:
        return

    tenant = get_tenant()
    if tenant is None:
        return

    ticket = instance
    event = "ticket.created" if created else "ticket.updated"

    await broadcast_ticket_event(
        tenant_id=tenant.tenant_id,
        ticket_id=ticket.id,
        event=event,
        data={
            "id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "title": ticket.title,
            "status_id": ticket.status_id,
            "priority_id": ticket.priority_id,
            "assignee_id": ticket.assignee_id,
        },
        team_id=ticket.team_id,
    )

    # Notify assignee on new assignment
    if created and ticket.assignee_id:
        await broadcast_notification(
            tenant_id=tenant.tenant_id,
            user_id=ticket.assignee_id,
            notification_type="ticket_assigned",
            message=f"New ticket assigned: {ticket.ticket_number}",
            ticket_id=ticket.id,
        )


@post_save.connect
async def broadcast_comment_changes(sender, instance, created, **kwargs):
    """Broadcast new comment events to ticket channel."""
    if sender is not Comment:
        return
    if not created:
        return

    tenant = get_tenant()
    if tenant is None:
        return

    comment = instance
    ticket = await Ticket.objects.filter(id=comment.ticket_id).first()
    if not ticket:
        return

    await broadcast_ticket_event(
        tenant_id=tenant.tenant_id,
        ticket_id=ticket.id,
        event="ticket.commented",
        data={
            "ticket_id": ticket.id,
            "comment_id": comment.id,
            "author_type": comment.author_type,
            "author_id": comment.author_id,
            "is_internal": comment.is_internal,
        },
        team_id=ticket.team_id,
    )
