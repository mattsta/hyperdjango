"""
HyperTicket metering — meter definitions, quota setup from PlanFeatureLimit.

Defines 4 meters (tickets, agents, api_usage, storage) with multi-dimensional tracking.
Quota enforcement driven by PlanFeatureLimit rows — zero hardcoded limits.
"""

from hyperdjango.logging import logger
from hyperdjango.metering import (
    DimensionSpec,
    MeterEngine,
    set_meter_engine,
)

from .models import Org, PlanFeatureLimit

# Meter definitions per architecture doc
METER_DEFINITIONS: list[tuple[str, str, list[DimensionSpec]]] = [
    (
        "tickets",
        "Ticket creation and resolution tracking",
        [
            DimensionSpec(
                name="tickets_created",
                dimension_type="counter",
                unit="tickets",
                default_agg="sum",
            ),
            DimensionSpec(
                name="tickets_resolved",
                dimension_type="counter",
                unit="tickets",
                default_agg="sum",
            ),
            DimensionSpec(
                name="response_time_ms",
                dimension_type="distribution",
                unit="ms",
                default_agg="avg",
            ),
            DimensionSpec(
                name="resolution_ms",
                dimension_type="distribution",
                unit="ms",
                default_agg="avg",
            ),
        ],
    ),
    (
        "agents",
        "Agent seat tracking",
        [
            DimensionSpec(
                name="active_agents",
                dimension_type="gauge",
                unit="agents",
                default_agg="last",
            ),
        ],
    ),
    (
        "api_usage",
        "API request tracking",
        [
            DimensionSpec(
                name="api_requests",
                dimension_type="counter",
                unit="requests",
                default_agg="sum",
            ),
        ],
    ),
    (
        "storage",
        "Attachment storage tracking",
        [
            DimensionSpec(
                name="attachment_bytes",
                dimension_type="counter",
                unit="bytes",
                default_agg="sum",
            ),
        ],
    ),
]


async def setup_meters() -> MeterEngine:
    """Initialize meter engine and define all meters."""
    engine = MeterEngine()
    await engine.ensure_tables()

    for meter_name, description, dimensions in METER_DEFINITIONS:
        await engine.define_meter(meter_name, dimensions, description=description)

    set_meter_engine(engine)
    logger.info("Metering: {n} meters defined", n=len(METER_DEFINITIONS))
    return engine


async def check_plan_limit(
    org: Org,
    feature_key: str,
    current_value: float = 0,
) -> tuple[bool, float, str]:
    """Check if org is within plan limits for a feature dimension.

    Returns (allowed, remaining, enforcement).
    -1 limit_value = unlimited.
    0 limit_value = disabled.
    """
    limit = await PlanFeatureLimit.objects.filter(
        plan_config_id=org.plan_config_id, feature_key=feature_key
    ).first()

    if limit is None:
        return True, -1, "none"  # No limit defined = unlimited

    if limit.limit_value == -1:
        return True, -1, "unlimited"  # Explicitly unlimited

    if limit.limit_value == 0:
        return False, 0, limit.enforcement  # Disabled

    remaining = limit.limit_value - current_value
    allowed = remaining > 0 or limit.enforcement != "reject"

    return allowed, remaining, limit.enforcement
