"""
Workflow Engine — condition matching + action dispatch.

Evaluates WorkflowRule conditions against ticket state and dispatches
configured actions. Triggered by post_save signals on Ticket and Comment.

Conditions JSON format:
    {"field": "priority_id", "op": "eq", "value": 1}
    {"field": "ticket_type_id", "op": "in", "value": [1, 2]}
    {"all": [cond1, cond2]}
    {"any": [cond1, cond2]}

Actions JSON format:
    [{"type": "assign_team", "team_id": 5},
     {"type": "add_tag", "tag_id": 3},
     {"type": "change_priority", "priority_id": 1},
     {"type": "set_status", "status_id": 2}]
"""

import json

from hyperdjango.logging import logger
from hyperdjango.tenancy import get_tenant

from ..models import (
    Ticket,
    TicketTag,
    WorkflowRule,
    WorkflowTrigger,
)


def _match_condition(
    condition: dict[str, object], ticket_data: dict[str, object]
) -> bool:
    """Evaluate a single condition or nested condition tree against ticket data."""
    # Nested AND
    if "all" in condition:
        return all(_match_condition(c, ticket_data) for c in condition["all"])

    # Nested OR
    if "any" in condition:
        return any(_match_condition(c, ticket_data) for c in condition["any"])

    field = condition.get("field", "")
    op = condition.get("op", "eq")
    expected = condition.get("value")

    actual = ticket_data.get(field)

    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "gt":
        return actual is not None and actual > expected
    if op == "gte":
        return actual is not None and actual >= expected
    if op == "lt":
        return actual is not None and actual < expected
    if op == "lte":
        return actual is not None and actual <= expected
    if op == "in":
        return actual in (expected or [])
    if op == "not_in":
        return actual not in (expected or [])
    if op == "contains":
        return (
            isinstance(actual, str) and isinstance(expected, str) and expected in actual
        )
    if op == "is_empty":
        return not actual
    if op == "is_not_empty":
        return bool(actual)

    return False


async def _ticket_to_dict(ticket: Ticket) -> dict[str, object]:
    """Extract ticket fields into a flat dict for condition matching.

    Uses active_statuses() for a single DB query instead of N has_status() calls.
    """
    statuses = await ticket.active_statuses()
    d = ticket.to_dict(
        include={
            "status_id",
            "priority_id",
            "ticket_type_id",
            "assignee_id",
            "team_id",
            "customer_id",
            "source",
            "title",
        }
    )
    d["is_locked"] = "locked" in statuses
    d["is_muted"] = "muted" in statuses
    return d


async def _execute_action(action: dict[str, object], ticket: Ticket) -> None:
    """Execute a single workflow action on a ticket."""
    action_type = action.get("type", "")

    if action_type == "assign_team":
        team_id = action.get("team_id", 0)
        if team_id:
            await Ticket.objects.filter(id=ticket.id).update(team_id=int(team_id))

    elif action_type == "assign_agent":
        agent_id = action.get("agent_id", 0)
        if agent_id:
            await Ticket.objects.filter(id=ticket.id).update(assignee_id=int(agent_id))

    elif action_type == "change_priority":
        priority_id = action.get("priority_id", 0)
        if priority_id:
            await Ticket.objects.filter(id=ticket.id).update(
                priority_id=int(priority_id)
            )

    elif action_type == "set_status":
        status_id = action.get("status_id", 0)
        if status_id:
            await Ticket.objects.filter(id=ticket.id).update(status_id=int(status_id))

    elif action_type == "add_tag":
        tag_id = action.get("tag_id", 0)
        if tag_id:
            tenant = get_tenant()
            existing = await TicketTag.objects.filter(
                ticket_id=ticket.id, tag_id=int(tag_id)
            ).first()
            if not existing:
                await TicketTag(
                    tenant_id=tenant.tenant_id if tenant else 0,
                    ticket_id=ticket.id,
                    tag_id=int(tag_id),
                ).save()

    elif action_type == "remove_tag":
        tag_id = action.get("tag_id", 0)
        if tag_id:
            await TicketTag.objects.filter(
                ticket_id=ticket.id, tag_id=int(tag_id)
            ).delete()

    else:
        logger.warning("Unknown workflow action type: {t}", t=action_type)


async def evaluate_rules(ticket: Ticket, trigger_event: WorkflowTrigger) -> int:
    """Evaluate all active workflow rules for a trigger event.

    Returns the number of rules that matched and executed.
    """
    rules = (
        await WorkflowRule.objects.filter(
            trigger_event=trigger_event.value,
            is_active=True,
        )
        .order_by("execution_order")
        .all()
    )

    if not rules:
        return 0

    ticket_data = await _ticket_to_dict(ticket)
    matched = 0

    for rule in rules:
        try:
            conditions = (
                json.loads(rule.conditions)
                if rule.conditions and rule.conditions != "{}"
                else {}
            )
        except json.JSONDecodeError, TypeError:
            continue

        # Empty conditions = always match
        if not conditions or _match_condition(conditions, ticket_data):
            try:
                actions = json.loads(rule.actions) if rule.actions else []
            except json.JSONDecodeError, TypeError:
                continue

            for action in actions:
                await _execute_action(action, ticket)

            matched += 1
            logger.info(
                "Workflow rule '{name}' fired for ticket {tid}",
                name=rule.name,
                tid=ticket.id,
            )

    return matched
