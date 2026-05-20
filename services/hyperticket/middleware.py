"""
HyperTicket middleware — quota check + plan-based rate limiting.

Checks PlanFeatureLimit quotas on state-changing operations.
Rate limits API requests per plan tier.
"""

from hyperdjango import HTTPException
from hyperdjango.logging import logger
from hyperdjango.timeline import get_timeline

from .metering import check_plan_limit
from .models import Agent, Org, Ticket


async def check_ticket_quota(org: Org) -> None:
    """Check if org can create more tickets this month.

    Raises HTTPException(429) if quota exceeded with reject enforcement.
    """
    # Count tickets created this month for this org
    current = await Ticket.objects.filter(tenant_id=org.id).count()

    allowed, remaining, enforcement = await check_plan_limit(
        org, "tickets_per_month", current
    )

    if not allowed:
        raise HTTPException(
            429,
            f"Ticket quota exceeded ({enforcement}). "
            f"Upgrade your plan for more tickets.",
        )

    if enforcement == "warn" and remaining <= 10:
        logger.warning(
            "Org {org} approaching ticket quota: {rem} remaining",
            org=org.slug,
            rem=remaining,
        )


async def check_agent_seat_quota(org: Org) -> None:
    """Check if org can add more agents.

    Raises HTTPException(429) if agent seat limit exceeded.
    """
    # Count agents that are NOT deactivated (no deactivated timeline event = active)
    all_agents = await Agent.objects.filter(tenant_id=org.id).all()
    tl = get_timeline()
    current = 0
    for a in all_agents:
        status = await tl.current_status("agent", a.id, "lifecycle")
        if not status or status.status != "deactivated":
            current += 1

    allowed, remaining, enforcement = await check_plan_limit(
        org, "agent_seats", current
    )

    if not allowed:
        raise HTTPException(
            429,
            f"Agent seat limit reached ({enforcement}). "
            f"Upgrade your plan for more agent seats.",
        )
