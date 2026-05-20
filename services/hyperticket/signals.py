"""
HyperTicket signal handlers — post_save for SLA, workflow, activity, assignment.

Connected to the platform's Signal system. Triggered automatically when
Ticket and Comment models are saved via the ORM.
"""

from hyperdjango.signals import post_save
from hyperdjango.tenancy import get_tenant

from .models import (
    ActivityAction,
    ActivityLog,
    Comment,
    SLAInstance,
    Ticket,
    TicketStatusConfig,
    WorkflowTrigger,
)
from .services.assignment import auto_assign
from .services.sla_engine import sla_engine
from .services.workflow_engine import evaluate_rules


@post_save.connect
async def on_ticket_save(sender, instance, created, **kwargs):
    """Handle ticket creation and updates."""
    if sender is not Ticket:
        return

    tenant = get_tenant()
    if tenant is None:
        return

    ticket = instance

    if created:
        # New ticket: create SLA instance, run workflow rules, auto-assign
        await sla_engine.create_instance(ticket)
        await evaluate_rules(ticket, WorkflowTrigger.TICKET_CREATED)

        # Auto-assign if no assignee set
        if not ticket.assignee_id:
            await auto_assign(ticket)

        # Log creation activity
        await ActivityLog(
            tenant_id=tenant.tenant_id,
            ticket_id=ticket.id,
            actor_type="system",
            actor_id=0,
            action=ActivityAction.CREATED,
        ).save()

    else:
        # Updated ticket: check for status change, run workflow rules
        await evaluate_rules(ticket, WorkflowTrigger.TICKET_UPDATED)

        # Check if status changed and affects SLA
        sla_instance = await SLAInstance.objects.filter(ticket_id=ticket.id).first()
        if sla_instance:
            status = await TicketStatusConfig.objects.filter(
                id=ticket.status_id
            ).first()
            if status:
                if status.pauses_sla and not sla_instance.paused_at:
                    await sla_engine.pause(sla_instance)
                elif not status.pauses_sla and sla_instance.paused_at:
                    await sla_engine.resume(sla_instance)


@post_save.connect
async def on_comment_save(sender, instance, created, **kwargs):
    """Handle new comments — trigger mention notifications, workflow rules."""
    if sender is not Comment:
        return
    if not created:
        return

    comment = instance
    tenant = get_tenant()
    if tenant is None:
        return

    # Load the ticket to check if this is the first agent response
    ticket = await Ticket.objects.filter(id=comment.ticket_id).first()
    if not ticket:
        return

    # Check if this is first agent response (for SLA tracking)
    if comment.author_type == "agent":
        sla_instance = await SLAInstance.objects.filter(ticket_id=ticket.id).first()
        if sla_instance and sla_instance.first_response_met == -1:
            await SLAInstance.objects.filter(id=sla_instance.id).update(
                first_response_met=1  # met
            )

    # Run comment-triggered workflow rules
    await evaluate_rules(ticket, WorkflowTrigger.COMMENT_ADDED)
