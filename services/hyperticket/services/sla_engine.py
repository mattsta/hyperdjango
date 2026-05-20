"""
SLA Engine — business-hours-aware deadline calculation, pause/resume, breach detection.

Walks forward through business minutes, skipping weekends, holidays,
and hours outside the configured business window.

Usage:
    engine = SLAEngine()
    deadline = engine.calculate_deadline(
        start=datetime.now(UTC),
        business_minutes=240,
        business_hours={"mon": ["09:00", "17:00"], "tue": ["09:00", "17:00"], ...},
        holidays=["2026-01-01", "2026-12-25"],
        timezone="America/New_York",
    )
"""

import contextlib
import json
from datetime import UTC, datetime, timedelta

from hyperdjango.tenancy import get_tenant

from ..models import (
    OrgSettings,
    PriorityConfig,
    SLAInstance,
    SLAPolicy,
    Ticket,
)

# Day name mapping: Python weekday (0=Monday) → business_hours key
_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Default business hours (Mon-Fri 9-17) if org has none configured
_DEFAULT_BUSINESS_HOURS: dict[str, list[str]] = {
    "mon": ["09:00", "17:00"],
    "tue": ["09:00", "17:00"],
    "wed": ["09:00", "17:00"],
    "thu": ["09:00", "17:00"],
    "fri": ["09:00", "17:00"],
}


class SLAEngine:
    """Business-hours-aware SLA deadline calculator."""

    def calculate_deadline(
        self,
        start: datetime,
        business_minutes: int,
        business_hours: dict[str, list[str]],
        holidays: list[str],
        timezone: str = "UTC",
    ) -> datetime:
        """Walk forward through business minutes, skipping non-work time.

        Args:
            start: Start timestamp (UTC).
            business_minutes: Number of business minutes to add.
            business_hours: {"mon": ["09:00", "17:00"], ...}. Missing day = closed.
            holidays: List of date strings "YYYY-MM-DD" that are non-working days.
            timezone: IANA timezone name (for display; calculations use UTC).

        Returns:
            Target deadline timestamp (UTC).
        """
        if business_minutes <= 0:
            return start

        if not business_hours:
            business_hours = _DEFAULT_BUSINESS_HOURS

        holiday_set = frozenset(holidays)
        remaining = business_minutes
        current = start

        # Safety limit to prevent infinite loops
        max_iterations = (
            business_minutes + 365 * 24 * 60
        )  # worst case: skip a year of holidays
        iterations = 0

        while remaining > 0 and iterations < max_iterations:
            iterations += 1
            day_name = _DAY_NAMES[current.weekday()]

            # Check if this day is a business day
            day_hours = business_hours.get(day_name)
            if day_hours is None or len(day_hours) < 2:
                # Not a business day — skip to next day start
                current = current.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                continue

            # Check if it's a holiday
            date_str = current.strftime("%Y-%m-%d")
            if date_str in holiday_set:
                current = current.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                continue

            # Parse business hours for this day
            open_hour, open_min = map(int, day_hours[0].split(":"))
            close_hour, close_min = map(int, day_hours[1].split(":"))
            day_open = current.replace(
                hour=open_hour, minute=open_min, second=0, microsecond=0
            )
            day_close = current.replace(
                hour=close_hour, minute=close_min, second=0, microsecond=0
            )

            # If before business hours, jump to open
            if current < day_open:
                current = day_open

            # If after business hours, jump to next day
            if current >= day_close:
                current = current.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                continue

            # Calculate available minutes in this business window
            available = int((day_close - current).total_seconds() / 60)

            if remaining <= available:
                current = current + timedelta(minutes=remaining)
                remaining = 0
            else:
                remaining -= available
                current = current.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)

        return current

    async def create_instance(self, ticket: Ticket) -> SLAInstance | None:
        """Create an SLAInstance for a ticket based on matching SLA policy.

        Resolves the policy, calculates deadlines using org business hours,
        applies priority multiplier.
        """
        tenant = get_tenant()
        if tenant is None:
            return None

        # Find matching SLA policy
        policy = None
        if ticket.sla_policy_id:
            policy = await SLAPolicy.objects.filter(id=ticket.sla_policy_id).first()
        if policy is None:
            policy = await SLAPolicy.objects.filter(is_default=True).first()
        if policy is None:
            return None

        # Load org settings for business hours
        settings = await OrgSettings.objects.first()
        business_hours = _DEFAULT_BUSINESS_HOURS
        holidays: list[str] = []
        timezone = "UTC"
        if settings:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                business_hours = (
                    json.loads(settings.business_hours) or _DEFAULT_BUSINESS_HOURS
                )
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                holidays = json.loads(settings.holidays) or []
            timezone = settings.timezone or "UTC"

        # Get priority multiplier
        priority = await PriorityConfig.objects.filter(id=ticket.priority_id).first()
        multiplier = priority.sla_multiplier if priority else 1.0

        priority_slug = priority.slug if priority else "normal"

        # Direct field lookup by priority slug (no getattr — all fields known)
        response_map: dict[str, int] = {
            "critical": policy.first_response_critical,
            "high": policy.first_response_high,
            "normal": policy.first_response_normal,
            "low": policy.first_response_low,
        }
        resolution_map: dict[str, int] = {
            "critical": policy.resolution_critical,
            "high": policy.resolution_high,
            "normal": policy.resolution_normal,
            "low": policy.resolution_low,
        }

        response_minutes = int(
            response_map.get(priority_slug, policy.first_response_normal) * multiplier
        )
        resolution_minutes = int(
            resolution_map.get(priority_slug, policy.resolution_normal) * multiplier
        )

        now = datetime.now(UTC)
        response_target = self.calculate_deadline(
            now, response_minutes, business_hours, holidays, timezone
        )
        resolution_target = self.calculate_deadline(
            now, resolution_minutes, business_hours, holidays, timezone
        )

        instance = SLAInstance(
            tenant_id=tenant.tenant_id,
            ticket_id=ticket.id,
            sla_policy_id=policy.id,
            first_response_target=response_target,
            resolution_target=resolution_target,
        )
        await instance.save()
        return instance

    async def pause(self, sla_instance: SLAInstance) -> None:
        """Pause SLA clock (ticket enters 'Waiting' status)."""
        now = datetime.now(UTC)
        await SLAInstance.objects.filter(id=sla_instance.id).update(paused_at=now)

    async def resume(self, sla_instance: SLAInstance) -> None:
        """Resume SLA clock. Extend deadlines by paused duration."""
        if not sla_instance.paused_at:
            return

        now = datetime.now(UTC)
        paused_at = sla_instance.paused_at
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=UTC)

        paused_seconds = (now - paused_at).total_seconds()
        paused_minutes = int(paused_seconds / 60)
        extension = timedelta(minutes=paused_minutes)

        # Extend deadlines — compute new targets from current values + extension
        if sla_instance.first_response_target:
            sla_instance.first_response_target = (
                sla_instance.first_response_target + extension
            )
        if sla_instance.resolution_target:
            sla_instance.resolution_target = sla_instance.resolution_target + extension
        sla_instance.paused_duration_minutes = (
            sla_instance.paused_duration_minutes or 0
        ) + paused_minutes
        sla_instance.paused_at = now
        await sla_instance.save()

    async def check_breach(self, sla_instance: SLAInstance) -> bool:
        """Check if SLA is breached (called by cron task).

        Returns True if newly breached.
        """
        if sla_instance.breached:
            return False

        now = datetime.now(UTC)

        # Check first response
        if sla_instance.first_response_met == -1 and sla_instance.first_response_target:
            target = sla_instance.first_response_target
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            if now > target:
                await SLAInstance.objects.filter(id=sla_instance.id).update(
                    first_response_met=0, breached=True
                )
                return True

        # Check resolution
        if sla_instance.resolution_met == -1 and sla_instance.resolution_target:
            target = sla_instance.resolution_target
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            if now > target:
                await SLAInstance.objects.filter(id=sla_instance.id).update(
                    resolution_met=0, breached=True
                )
                return True

        return False


# Module-level singleton
sla_engine = SLAEngine()
