"""
Export service — CSV/JSON ticket export.

Supports filtered exports with adapter pipeline hooks for row transformation.
Large exports run as background tasks.
"""

import csv
import enum
import io
import json

from hyperdjango.tenancy import tenant_context

from ..models import Ticket


def _scalar(value):
    """Unwrap enum instances to their scalar value for CSV/JSON serialization.

    Ticket enum fields (source, etc.) hydrate from the DB as enum instances.
    Downstream serializers like csv.writer and json.dumps cannot handle
    Enum subclasses — they must see the underlying str/int value.
    """
    if isinstance(value, enum.Enum):
        return value.value
    return value


async def export_tickets_csv(
    tenant_id: int, filters: dict[str, object] | None = None
) -> str:
    """Export tickets as CSV string for a tenant."""
    # A service called with an explicit tenant_id establishes the tenant context
    # so the ORM auto-scopes (works whether invoked from a request or directly).
    with tenant_context(tenant_id=tenant_id):
        tickets = await (
            Ticket.objects.filter(is_deleted=False, is_current=True)
            .order_by("id")
            .all()
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "ticket_number",
            "title",
            "description",
            "status_id",
            "priority_id",
            "ticket_type_id",
            "assignee_id",
            "team_id",
            "customer_id",
            "source",
            "created_at",
            "updated_at",
        ]
    )

    for t in tickets:
        writer.writerow(
            [
                t.id,
                t.ticket_number,
                t.title,
                t.description,
                t.status_id,
                t.priority_id,
                t.ticket_type_id,
                t.assignee_id,
                t.team_id,
                t.customer_id,
                _scalar(t.source),
                str(t.created_at),
                str(t.updated_at),
            ]
        )

    return output.getvalue()


async def export_tickets_json(
    tenant_id: int, filters: dict[str, object] | None = None
) -> str:
    """Export tickets as JSON string for a tenant."""
    with tenant_context(tenant_id=tenant_id):
        tickets = await (
            Ticket.objects.filter(is_deleted=False, is_current=True)
            .order_by("id")
            .all()
        )

    result = []
    for t in tickets:
        result.append(
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "title": t.title,
                "description": t.description,
                "status_id": t.status_id,
                "priority_id": t.priority_id,
                "ticket_type_id": t.ticket_type_id,
                "assignee_id": t.assignee_id,
                "team_id": t.team_id,
                "customer_id": t.customer_id,
                "source": _scalar(t.source),
                "created_at": str(t.created_at),
                "updated_at": str(t.updated_at),
            }
        )

    return json.dumps({"tickets": result, "count": len(result)}, indent=2)
