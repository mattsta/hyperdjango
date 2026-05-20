"""
Analytics tasks — metric recording + daily report generation.

Record ticket metrics on events, generate daily summary reports.
"""

from hyperdjango.metering import get_meter_engine


async def record_ticket_created(tenant_id: int, account_id: str) -> None:
    """Record a ticket creation event in the metering system."""
    engine = get_meter_engine()
    if engine is None:
        return
    await engine.record(
        "tickets",
        account_id,
        {"tickets_created": 1},
        tenant_id=tenant_id,
    )


async def record_ticket_resolved(
    tenant_id: int,
    account_id: str,
    resolution_ms: float,
) -> None:
    """Record a ticket resolution event with timing."""
    engine = get_meter_engine()
    if engine is None:
        return
    await engine.record(
        "tickets",
        account_id,
        {"tickets_resolved": 1, "resolution_ms": resolution_ms},
        tenant_id=tenant_id,
    )


async def record_api_request(tenant_id: int, account_id: str) -> None:
    """Record an API request in the metering system."""
    engine = get_meter_engine()
    if engine is None:
        return
    await engine.record(
        "api_usage",
        account_id,
        {"api_requests": 1},
        tenant_id=tenant_id,
    )
