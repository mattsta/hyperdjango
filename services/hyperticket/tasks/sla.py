"""
SLA breach checking cron task.

Scans all non-breached SLA instances, checks deadlines, marks breached,
fires escalation rules. Scheduled every 1 minute via TaskScheduler.
"""

from hyperdjango.logging import logger

from ..models import (
    ActivityAction,
    ActivityLog,
    EscalationRule,
    SLAInstance,
    Ticket,
)
from ..services.sla_engine import sla_engine


async def check_sla_breaches() -> int:
    """Scan all active SLA instances for breaches.

    Returns the number of newly breached instances.
    """
    # Query non-breached, non-paused instances
    instances = await SLAInstance.objects.filter(breached=False).all()

    breached_count = 0
    for instance in instances:
        newly_breached = await sla_engine.check_breach(instance)
        if newly_breached:
            breached_count += 1

            # Log breach activity
            ticket = await Ticket.objects.filter(id=instance.ticket_id).first()
            if ticket:
                await ActivityLog(
                    tenant_id=ticket.tenant_id,
                    ticket_id=ticket.id,
                    actor_type="system",
                    actor_id=0,
                    action=ActivityAction.SLA_BREACHED,
                    detail=f'{{"sla_instance_id": {instance.id}}}',
                ).save()

                # Fire escalation rules
                escalation_rules = await EscalationRule.objects.filter(
                    is_active=True
                ).all()
                for rule in escalation_rules:
                    if rule.escalate_to_agent_id:
                        await Ticket.objects.filter(id=ticket.id).update(
                            assignee_id=rule.escalate_to_agent_id
                        )
                    if rule.escalate_to_team_id:
                        await Ticket.objects.filter(id=ticket.id).update(
                            team_id=rule.escalate_to_team_id
                        )
                    await ActivityLog(
                        tenant_id=ticket.tenant_id,
                        ticket_id=ticket.id,
                        actor_type="system",
                        actor_id=0,
                        action=ActivityAction.ESCALATED,
                        detail=f'{{"escalation_rule_id": {rule.id}}}',
                    ).save()

    if breached_count:
        logger.warning("SLA: {n} breach(es) detected", n=breached_count)

    return breached_count
