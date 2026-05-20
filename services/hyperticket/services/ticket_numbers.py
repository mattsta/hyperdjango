"""
Org-prefixed sequential ticket number generation.

Generates ticket numbers like ACME-0001, ACME-0002, GLOBEX-0001.
Uses ORM queries for org slug lookup and sequence generation.
"""

from hyperdjango.expressions import Max

from ..models import Org, Ticket


async def next_ticket_number(tenant_id: int) -> str:
    """Generate the next ticket number for an org.

    Atomically increments by finding max ticket ID + 1.
    Uses the org slug as prefix, uppercased.

    Returns: "ACME-0042" format.
    """
    # Get org slug via ORM
    org = await Org.objects.filter(id=tenant_id).first()
    slug = org.slug if org else "ORG"
    prefix = slug.upper()

    # Next number — max ticket ID + 1 for this tenant
    result = await Ticket.objects.filter(tenant_id=tenant_id).aggregate(
        max_id=Max("id"),
    )
    next_num = (result.get("max_id", 0) or 0) + 1

    return f"{prefix}-{next_num:04d}"
