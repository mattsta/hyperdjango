"""
Assignment Service — round-robin + skill-based ticket routing.

Round-robin: rotates through active team members by least-recently-assigned.
Skill-based: matches ticket tags against AgentSkill.skill_tag weighted by proficiency.
"""

from hyperdjango.logging import logger
from hyperdjango.tenancy import get_tenant
from hyperdjango.timeline import get_timeline

from ..models import (
    Agent,
    AgentSkill,
    AssignmentStrategy,
    OrgSettings,
    Tag,
    TeamMembership,
    Ticket,
    TicketTag,
)


async def assign_round_robin(ticket: Ticket, team_id: int) -> int | None:
    """Assign ticket to next agent in team via round-robin.

    Finds the active team member who was least recently assigned a ticket.
    Returns the assigned agent_id, or None if no agent available.
    """
    # Get active team members
    memberships = await TeamMembership.objects.filter(team_id=team_id).all()
    if not memberships:
        return None

    agent_ids = [m.agent_id for m in memberships]

    # Filter to active agents (not deactivated via timeline)
    candidate_agents = await Agent.objects.filter(id__in=agent_ids).all()
    tl = get_timeline()
    active_agents = []
    for a in candidate_agents:
        status = await tl.current_status("agent", a.id, "lifecycle")
        if not status or status.status != "deactivated":
            active_agents.append(a)
    if not active_agents:
        return None

    active_ids = [a.id for a in active_agents]

    # Find the agent with the fewest currently assigned tickets (load balancing)
    tenant = get_tenant()
    tenant_id = tenant.tenant_id if tenant else 0

    # Build load map: count open tickets per agent
    load_map: dict[int, int] = {}
    for aid in active_ids:
        cnt = await Ticket.objects.filter(
            tenant_id=tenant_id,
            assignee_id=aid,
            is_deleted=False,
            is_current=True,
        ).count()
        if cnt > 0:
            load_map[aid] = cnt

    # Pick agent with lowest load; ties broken by ID (deterministic)
    best_agent_id = min(active_ids, key=lambda aid: (load_map.get(aid, 0), aid))

    # Assign
    await Ticket.objects.filter(id=ticket.id).update(
        assignee_id=best_agent_id, team_id=team_id
    )
    logger.info(
        "Round-robin assigned ticket {tid} to agent {aid}",
        tid=ticket.id,
        aid=best_agent_id,
    )
    return best_agent_id


async def assign_skill_based(ticket: Ticket) -> int | None:
    """Assign ticket to best-skilled agent based on ticket tags.

    Matches ticket tags against AgentSkill.skill_tag, sums proficiency
    scores per agent, picks the highest-scoring active agent with capacity.
    """
    tenant = get_tenant()
    if tenant is None:
        return None

    # Get ticket's tags
    ticket_tags = await TicketTag.objects.filter(ticket_id=ticket.id).all()
    if not ticket_tags:
        return None

    tag_ids = [tt.tag_id for tt in ticket_tags]
    tags = await Tag.objects.filter(id__in=tag_ids).all()
    tag_names = [t.name for t in tags]

    if not tag_names:
        return None

    # Find agents with matching skills
    skills = await AgentSkill.objects.filter(skill_tag__in=tag_names).all()
    if not skills:
        return None

    # Score agents by total proficiency across matching skills
    agent_scores: dict[int, int] = {}
    for skill in skills:
        agent_scores[skill.agent_id] = (
            agent_scores.get(skill.agent_id, 0) + skill.proficiency
        )

    # Filter to active agents with capacity (not deactivated via timeline)
    agent_ids = list(agent_scores.keys())
    candidate_agents = await Agent.objects.filter(id__in=agent_ids).all()
    tl = get_timeline()
    active_agents = []
    for a in candidate_agents:
        status = await tl.current_status("agent", a.id, "lifecycle")
        if not status or status.status != "deactivated":
            active_agents.append(a)

    if not active_agents:
        return None

    # Check capacity (max_concurrent_tickets)
    for agent in sorted(active_agents, key=lambda a: -agent_scores.get(a.id, 0)):
        current_count = await Ticket.objects.filter(
            tenant_id=tenant.tenant_id,
            assignee_id=agent.id,
            is_deleted=False,
            is_current=True,
        ).count()
        if current_count < agent.max_concurrent_tickets:
            await Ticket.objects.filter(id=ticket.id).update(assignee_id=agent.id)
            logger.info(
                "Skill-based assigned ticket {tid} to agent {aid} (score={score})",
                tid=ticket.id,
                aid=agent.id,
                score=agent_scores[agent.id],
            )
            return agent.id

    return None


async def auto_assign(ticket: Ticket) -> int | None:
    """Auto-assign a ticket based on org assignment strategy.

    Returns assigned agent_id or None.
    """
    settings = await OrgSettings.objects.first()
    if settings is None:
        return None

    strategy = settings.auto_assignment_strategy

    if strategy == AssignmentStrategy.MANUAL.value:
        return None

    if strategy == AssignmentStrategy.ROUND_ROBIN.value:
        if ticket.team_id:
            return await assign_round_robin(ticket, ticket.team_id)
        return None

    if strategy == AssignmentStrategy.SKILL_BASED.value:
        return await assign_skill_based(ticket)

    return None
