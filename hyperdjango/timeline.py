"""
Status timeline — time-series event tracking for entity statuses.

Replaces boolean flags (is_banned, is_muted, is_staff, is_active) with a
temporal event model. Every status change is recorded as an event with:
- Start/end times (supports auto-expiry and indefinite statuses)
- Actor attribution (who made the change and why)
- Full queryable history (audit trail, reporting, compliance)

Activeness is query-derived: the latest event per (entity, category) where
``ended_at IS NULL AND (expires_at IS NULL OR expires_at > now())``.
No denormalized ``is_active``/``is_current`` flags — the database indexes
and query patterns enforce correctness without stale state.

Usage:
    from hyperdjango.timeline import TimelineManager, get_timeline

    tl = get_timeline()

    # Record a ban with 30-day auto-expiry
    await tl.add_event("user", 42, "moderation", "banned",
        reason="Repeated spam", actor_id=1,
        expires_in=timedelta(days=30))

    # Check current status
    status = await tl.current_status("user", 42, "moderation")
    # → StatusRecord(status="banned", since=..., expires_at=..., reason="...")

    # End early (appeal approved)
    await tl.end_status("user", 42, "moderation",
        ended_by=1, end_reason="Appeal approved")

Model mixin:
    from hyperdjango.timeline import StatusTimelineMixin

    class User(StatusTimelineMixin, TimestampMixin, Model):
        class TimelineConfig:
            entity_type = "user"
            categories = {
                "moderation": ["banned", "muted", "warned"],
                "access": ["staff", "moderator", "verified"],
            }

    await user.set_status("moderation", "banned", reason="spam", actor_id=1)
    is_banned = await user.has_status("moderation", "banned")
    history = await user.get_status_history("moderation")
"""

import json as _json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from hyperdjango.database import get_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.signals import Signal, log_robust_responses

_logger = logging.getLogger("hyperdjango.timeline")

# ── Signal ─────────────────────────────────────────────────────────────────

status_changed = Signal(name="status_changed")

# ── Result Dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StatusRecord:
    """Immutable snapshot of a status event for API consumers."""

    status: str
    category: str
    since: datetime
    expires_at: datetime | None
    ended_at: datetime | None
    reason: str
    end_reason: str
    actor_id: int | None
    ended_by: int | None
    detail: dict[str, str | int | float | bool | None]
    event_id: int
    entity_type: str
    entity_id: int


# ── StatusEvent Model ──────────────────────────────────────────────────────


class StatusEvent(TimestampMixin, Model):
    """A single status event in an entity's timeline.

    Each row represents one status period: from ``started_at`` until
    ``ended_at`` (or ``expires_at``, or indefinite if both are None).

    Activeness is determined by query — no ``is_active`` flags:
    - Active: ``ended_at IS NULL AND (expires_at IS NULL OR expires_at > now())``
    - Ended: ``ended_at IS NOT NULL``
    - Expired: ``expires_at IS NOT NULL AND expires_at <= now() AND ended_at IS NULL``

    Table is NOT UNLOGGED by default — status events (bans, role changes)
    are critical data that must survive PostgreSQL crashes. Set
    ``unlogged = True`` in a subclass Meta if you want fast writes for
    non-critical statuses (analytics, temp flags).
    """

    class Meta:
        table = "hyper_status_events"

    id: int = Field(primary_key=True, auto=True)

    # What entity this applies to (polymorphic)
    entity_type: str = Field(index=True)
    entity_id: int = Field(index=True)

    # Status categorization
    category: str = Field()
    status: str = Field()

    # Time range (proper datetime — native PostgreSQL TIMESTAMPTZ)
    started_at: datetime | None = Field(default=None)
    expires_at: datetime | None = Field(default=None)
    ended_at: datetime | None = Field(default=None)

    # Attribution
    actor_id: int | None = Field(default=None)
    ended_by: int | None = Field(default=None)

    # Context
    reason: str = Field(default="")
    end_reason: str = Field(default="")
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    # Multi-tenant support
    tenant_id: int = Field(default=0, index=True)


# ── Index DDL ──────────────────────────────────────────────────────────────
# Created by hyper setup after the table DDL — compound/partial indexes
# that can't be expressed via Field(index=True).

TIMELINE_INDEXES: Final[list[str]] = [
    # Current status lookup: latest active event per entity+category
    "CREATE INDEX IF NOT EXISTS idx_stev_current "
    "ON hyper_status_events (entity_type, entity_id, category, started_at DESC)",
    # Active status scan: entities with a specific non-ended status
    "CREATE INDEX IF NOT EXISTS idx_stev_active "
    "ON hyper_status_events (entity_type, status, started_at DESC) "
    "WHERE ended_at IS NULL",
    # Expiry scan: events needing expiration
    "CREATE INDEX IF NOT EXISTS idx_stev_expires "
    "ON hyper_status_events (expires_at) "
    "WHERE ended_at IS NULL AND expires_at IS NOT NULL",
    # Tenant-scoped queries
    "CREATE INDEX IF NOT EXISTS idx_stev_tenant "
    "ON hyper_status_events (tenant_id, entity_type, entity_id, started_at DESC) "
    "WHERE tenant_id > 0",
]


# ── Helpers ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Current UTC time."""
    return datetime.now(UTC)


def _is_event_active(event: StatusEvent) -> bool:
    """Check if an event is currently active (not ended, not expired)."""
    if event.ended_at is not None:
        return False
    if event.expires_at is not None:
        # Strip tzinfo for safe comparison — pg.zig returns UTC naive,
        # _utcnow() returns UTC aware. Compare both as naive UTC.
        now = _utcnow().replace(tzinfo=None)
        exp = event.expires_at
        if exp.tzinfo is not None:
            exp = exp.replace(tzinfo=None)
        if now > exp:
            return False
    return True


def _event_to_record(event: StatusEvent) -> StatusRecord:
    """Convert a StatusEvent model instance to a frozen StatusRecord."""
    started = event.started_at
    if started is None:
        raise ValueError(
            f"StatusEvent {event.id} has NULL started_at — data corruption"
        )

    return StatusRecord(
        status=event.status,
        category=event.category,
        since=started,
        expires_at=event.expires_at,
        ended_at=event.ended_at,
        reason=event.reason,
        end_reason=event.end_reason,
        actor_id=event.actor_id,
        ended_by=event.ended_by,
        detail=event.detail if isinstance(event.detail, dict) else {},
        event_id=event.id,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
    )


# ── TimelineManager ────────────────────────────────────────────────────────


@dataclass
class TimelineManager:
    """Central manager for status timeline operations.

    All queries use the ORM where possible, raw SQL for performance-critical
    bulk operations. Activeness is determined by querying the latest event
    per (entity, category) and checking ended_at/expires_at — no stale flags.
    """

    async def ensure_indexes(self) -> None:
        """Create compound/partial indexes. Called once at app startup."""
        db = get_db()
        for ddl in TIMELINE_INDEXES:
            await db.execute(ddl)

    async def add_event(
        self,
        entity_type: str,
        entity_id: int,
        category: str,
        status: str,
        *,
        reason: str = "",
        actor_id: int | None = None,
        expires_at: datetime | None = None,
        expires_in: timedelta | None = None,
        detail: dict[str, str | int | float | bool | None] | None = None,
        tenant_id: int = 0,
        _escalation_depth: int = 0,
    ) -> StatusEvent:
        """Record a new status event.

        Ends any currently active status in the same (entity, category)
        before inserting the new event. The entire operation runs inside
        a transaction with SELECT FOR UPDATE to prevent race conditions.
        """
        now = _utcnow()
        db = get_db()

        # Compute expiry
        effective_expires: datetime | None = expires_at
        if effective_expires is None and expires_in is not None:
            effective_expires = now + expires_in

        # Capture old status for signal (before transaction modifies state)
        old_status_name: str | None = None

        async with db.transaction():
            # Lock all non-ended events for this entity+category.
            # SELECT FOR UPDATE holds the lock until COMMIT, preventing
            # concurrent add_event calls from creating duplicate active events.
            await db.execute(
                "SELECT id FROM hyper_status_events "
                "WHERE entity_type = $1 AND entity_id = $2 AND category = $3 "
                "AND ended_at IS NULL FOR UPDATE",
                entity_type,
                entity_id,
                category,
            )

            # End current active status in this category (if any)
            current = await self._current_status_unlocked(
                entity_type, entity_id, category
            )
            if current is not None:
                old_status_name = current.status
                await self._end_event_raw(db, current.event_id, ended_by=actor_id)

            # Insert new event
            event = StatusEvent(
                entity_type=entity_type,
                entity_id=entity_id,
                category=category,
                status=status,
                started_at=now,
                expires_at=effective_expires,
                actor_id=actor_id,
                reason=reason,
                detail=detail or {},
                tenant_id=tenant_id,
            )
            await event.save()

        # Fire signal AFTER commit (consistent DB state). Post-commit: a
        # failing receiver must not abort the already-committed status change,
        # so dispatch robustly and log any receiver failure loudly.
        responses = await status_changed.send_robust(
            sender=StatusEvent,
            entity_type=entity_type,
            entity_id=entity_id,
            category=category,
            old_status=old_status_name,
            new_status=status,
            actor_id=actor_id,
            reason=reason,
            _escalation_depth=_escalation_depth,
        )
        log_robust_responses(responses, _logger, "status_changed")

        return event

    async def end_status(
        self,
        entity_type: str,
        entity_id: int,
        category: str,
        *,
        ended_by: int | None = None,
        end_reason: str = "",
    ) -> bool:
        """End the current active status in a category.

        Sets ended_at=now on the active event. Returns True if an active
        status was found and ended, False if no active status.
        """
        db = get_db()
        old_status_name: str | None = None

        async with db.transaction():
            current = await self._current_status_unlocked(
                entity_type, entity_id, category
            )
            if current is None:
                return False

            old_status_name = current.status
            await self._end_event_raw(
                db, current.event_id, ended_by=ended_by, end_reason=end_reason
            )

        # Fire signal AFTER commit. Post-commit robust dispatch + loud logging.
        responses = await status_changed.send_robust(
            sender=StatusEvent,
            entity_type=entity_type,
            entity_id=entity_id,
            category=category,
            old_status=old_status_name,
            new_status=None,
            actor_id=ended_by,
            reason=end_reason,
        )
        log_robust_responses(responses, _logger, "status_changed")

        return True

    async def current_status(
        self,
        entity_type: str,
        entity_id: int,
        category: str,
    ) -> StatusRecord | None:
        """Get the current active status for an entity+category.

        Queries the latest event where ended_at IS NULL, then checks
        expires_at inline. Does NOT lazily expire — let expire_overdue()
        handle that in a background task. This keeps current_status as
        a single read-only query with no side effects.
        """
        return await self._current_status_unlocked(entity_type, entity_id, category)

    async def _current_status_unlocked(
        self,
        entity_type: str,
        entity_id: int,
        category: str,
    ) -> StatusRecord | None:
        """Internal: get current status without acquiring locks.

        Pushes both ended_at AND expires_at checks into SQL so only truly
        active events are returned. Hits idx_stev_current index.

        ``id DESC`` breaks a ``started_at`` tie the same way ``get_history``
        does: when two active events share a timestamp tick, "current" is the
        one written last, not an arbitrary pick.
        """
        db = get_db()
        now = _utcnow()
        row = await db.query_one(
            "SELECT * FROM hyper_status_events "
            "WHERE entity_type = $1 AND entity_id = $2 AND category = $3 "
            "AND ended_at IS NULL AND (expires_at IS NULL OR expires_at > $4) "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            entity_type,
            entity_id,
            category,
            now,
        )
        if row is None:
            return None
        event = StatusEvent.from_record(row)
        return _event_to_record(event)

    async def is_active(
        self,
        entity_type: str,
        entity_id: int,
        status: str,
    ) -> bool:
        """Check if entity has a specific active (not ended, not expired) status.

        Single indexed query — hits idx_stev_active partial index.
        Returns True/False with no Python-side expiry checking.
        """
        db = get_db()
        now = _utcnow()
        row = await db.query_one(
            "SELECT 1 FROM hyper_status_events "
            "WHERE entity_type = $1 AND entity_id = $2 AND status = $3 "
            "AND ended_at IS NULL AND (expires_at IS NULL OR expires_at > $4) "
            "LIMIT 1",
            entity_type,
            entity_id,
            status,
            now,
        )
        return row is not None

    async def active_statuses(
        self,
        entity_type: str,
        entity_id: int,
    ) -> set[str]:
        """Get ALL active status names for an entity in ONE query.

        Returns a set of status strings (e.g., {"banned", "staff"}).
        Use this for guard chains that check multiple statuses — 1 query
        instead of N queries for N checks.

        Pushes expires_at check into SQL and SELECTs only the status column
        for minimal data transfer and zero Python-side filtering.
        """
        db = get_db()
        now = _utcnow()
        rows = await db.query(
            "SELECT DISTINCT status FROM hyper_status_events "
            "WHERE entity_type = $1 AND entity_id = $2 "
            "AND ended_at IS NULL AND (expires_at IS NULL OR expires_at > $3)",
            entity_type,
            entity_id,
            now,
        )
        return {row["status"] for row in rows}

    async def get_history(
        self,
        entity_type: str,
        entity_id: int,
        category: str | None = None,
        *,
        limit: int = 100,
    ) -> list[StatusRecord]:
        """Get full status history for an entity, newest first.

        Ordering is TOTAL: ``started_at DESC, id DESC``. Two events recorded in
        the same timestamp tick — an escalation that ends one status and starts
        the next, a scripted bulk action — are ordered by insertion, so the
        newest is genuinely the last one written. Without the ``id`` tiebreaker
        their relative order was whatever the plan happened to produce, which
        made "newest first" a coin flip for exactly the sequences a history view
        exists to explain (and forced callers to space writes apart in time to
        get a stable answer).
        """
        db = get_db()

        # Use raw SQL with LIMIT to avoid loading all rows into memory
        if category is not None:
            rows = await db.query(
                "SELECT * FROM hyper_status_events "
                "WHERE entity_type = $1 AND entity_id = $2 AND category = $3 "
                "ORDER BY started_at DESC, id DESC LIMIT $4",
                entity_type,
                entity_id,
                category,
                limit,
            )
        else:
            rows = await db.query(
                "SELECT * FROM hyper_status_events "
                "WHERE entity_type = $1 AND entity_id = $2 "
                "ORDER BY started_at DESC, id DESC LIMIT $3",
                entity_type,
                entity_id,
                limit,
            )

        return [_row_to_record(row) for row in rows]

    async def get_entities(
        self,
        entity_type: str,
        status: str,
        *,
        tenant_id: int | None = None,
    ) -> list[int]:
        """Get all entity IDs with a specific active status.

        Uses SQL with DISTINCT and expires_at filter to avoid loading
        all rows into Python memory.
        """
        db = get_db()
        now = _utcnow()

        if tenant_id is not None:
            rows = await db.query(
                "SELECT DISTINCT entity_id FROM hyper_status_events "
                "WHERE entity_type = $1 AND status = $2 AND ended_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > $3) "
                "AND tenant_id = $4",
                entity_type,
                status,
                now,
                tenant_id,
            )
        else:
            rows = await db.query(
                "SELECT DISTINCT entity_id FROM hyper_status_events "
                "WHERE entity_type = $1 AND status = $2 AND ended_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > $3)",
                entity_type,
                status,
                now,
            )

        return [row["entity_id"] for row in rows]

    async def expire_overdue(self) -> int:
        """Optional: mark expired events with ended_at for cleaner admin views.

        NOT required for correctness — all read methods (current_status,
        is_active, get_entities) already check expires_at inline and
        return None/False for expired events. This method is purely for
        housekeeping: setting ended_at on past-due events so admin UIs
        and history queries show them as explicitly ended rather than
        relying on expires_at comparison.

        Returns exact count of newly marked events.
        """
        now = _utcnow()
        db = get_db()

        rows = await db.query(
            "UPDATE hyper_status_events "
            "SET ended_at = $1, end_reason = 'expired' "
            "WHERE ended_at IS NULL AND expires_at IS NOT NULL AND expires_at < $2 "
            "RETURNING id",
            now,
            now,
        )

        return len(rows)

    async def cleanup(self, days: int = 90) -> int:
        """Delete ended events older than N days.

        Atomic: single DELETE with RETURNING for exact count.
        """
        cutoff = _utcnow() - timedelta(days=days)
        db = get_db()

        rows = await db.query(
            "DELETE FROM hyper_status_events "
            "WHERE ended_at IS NOT NULL AND ended_at < $1 "
            "RETURNING id",
            cutoff,
        )

        return len(rows)

    async def _end_event_raw(
        self,
        db,
        event_id: int,
        *,
        ended_by: int | None = None,
        end_reason: str = "",
    ) -> None:
        """Mark an event as ended. Caller must hold transaction lock."""
        now = _utcnow()
        parts = ["ended_at = $1"]
        params: list[datetime | int | str] = [now]
        idx = 2

        if ended_by is not None:
            parts.append(f"ended_by = ${idx}")
            params.append(ended_by)
            idx += 1
        if end_reason:
            parts.append(f"end_reason = ${idx}")
            params.append(end_reason)
            idx += 1

        params.append(event_id)
        await db.execute(
            f"UPDATE hyper_status_events SET {', '.join(parts)} WHERE id = ${idx}",
            *params,
        )


def _row_to_record(row: dict) -> StatusRecord:
    """Convert a raw DB row dict to a StatusRecord."""
    detail_raw = row.get("detail")
    if isinstance(detail_raw, dict):
        detail = detail_raw
    elif isinstance(detail_raw, str) and detail_raw:
        parsed = _json.loads(detail_raw)
        detail = parsed if isinstance(parsed, dict) else {}
    else:
        detail = {}

    return StatusRecord(
        status=row["status"],
        category=row["category"],
        since=row["started_at"],
        expires_at=row.get("expires_at"),
        ended_at=row.get("ended_at"),
        reason=row.get("reason", ""),
        end_reason=row.get("end_reason", ""),
        actor_id=row.get("actor_id"),
        ended_by=row.get("ended_by"),
        detail=detail,
        event_id=row["id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
    )


# ── Model Mixin ────────────────────────────────────────────────────────────


class StatusTimelineMixin(Model):
    """Model mixin providing ergonomic per-instance status timeline methods.

    Subclasses define a ``TimelineConfig`` inner class with categories
    and allowed statuses per category. The mixin validates inputs at
    call time and delegates to the global TimelineManager.

    Usage:
        class User(StatusTimelineMixin, TimestampMixin, Model):
            class TimelineConfig:
                entity_type = "user"
                categories = {
                    "moderation": ["banned", "muted", "warned"],
                    "access": ["staff", "moderator", "verified"],
                    "tier": ["free", "pro", "enterprise"],
                }

            id: int = Field(primary_key=True, auto=True)
            username: str = Field(unique=True)
    """

    class Meta:
        abstract = True

    _timeline_entity_type: ClassVar[str]
    _timeline_categories: ClassVar[dict[str, list[str]]]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        config = cls.__dict__.get("TimelineConfig")
        if config is None:
            if "_timeline_entity_type" in cls.__dict__ or any(
                "_timeline_entity_type" in b.__dict__ for b in cls.__mro__[1:]
            ):
                return
            return

        config_dict = config.__dict__
        entity_type = config_dict.get("entity_type", "")
        if not entity_type:
            entity_type = cls.__name__.lower()

        categories = config_dict.get("categories")
        if not categories:
            raise ValueError(
                f"{cls.__name__}.TimelineConfig.categories is required — "
                f"provide a dict mapping category names to lists of valid statuses"
            )

        # Validate structure: categories must be dict[str, list[str]]
        for cat_name, statuses in categories.items():
            if not isinstance(statuses, (list, tuple)):
                raise TypeError(
                    f"{cls.__name__}.TimelineConfig.categories[{cat_name!r}] "
                    f"must be a list of status strings, got {type(statuses).__name__}"
                )
            if not statuses:
                raise ValueError(
                    f"{cls.__name__}.TimelineConfig.categories[{cat_name!r}] "
                    f"is empty — provide at least one valid status"
                )

        cls._timeline_entity_type = entity_type
        cls._timeline_categories = dict(categories)

    def _validate_category_status(
        self, category: str, status: str | None = None
    ) -> None:
        """Validate category and optional status against TimelineConfig."""
        if category not in self._timeline_categories:
            raise ValueError(
                f"Invalid category {category!r} for {type(self).__name__}. "
                f"Valid: {list(self._timeline_categories.keys())}"
            )
        if status is not None and status not in self._timeline_categories[category]:
            raise ValueError(
                f"Invalid status {status!r} for category {category!r}. "
                f"Valid: {self._timeline_categories[category]}"
            )

    async def set_status(
        self,
        category: str,
        status: str,
        *,
        reason: str = "",
        actor_id: int | None = None,
        expires_at: datetime | None = None,
        expires_in: timedelta | None = None,
        detail: dict[str, str | int | float | bool | None] | None = None,
    ) -> StatusEvent:
        """Set a status in the given category. Ends any current status first."""
        self._validate_category_status(category, status)
        tl = get_timeline()
        return await tl.add_event(
            self._timeline_entity_type,
            self.id,
            category,
            status,
            reason=reason,
            actor_id=actor_id,
            expires_at=expires_at,
            expires_in=expires_in,
            detail=detail,
        )

    async def clear_status(
        self,
        category: str,
        *,
        reason: str = "",
        actor_id: int | None = None,
    ) -> bool:
        """End the current status in a category. Returns True if ended."""
        self._validate_category_status(category)
        tl = get_timeline()
        return await tl.end_status(
            self._timeline_entity_type,
            self.id,
            category,
            ended_by=actor_id,
            end_reason=reason,
        )

    async def has_status(self, category: str, status: str) -> bool:
        """Check if this entity has a specific active status.

        Uses is_active() directly — single query, no full record fetch.
        """
        self._validate_category_status(category, status)
        tl = get_timeline()
        return await tl.is_active(self._timeline_entity_type, self.id, status)

    async def get_status(self, category: str) -> StatusRecord | None:
        """Get the current active status in a category."""
        self._validate_category_status(category)
        tl = get_timeline()
        return await tl.current_status(self._timeline_entity_type, self.id, category)

    async def active_statuses(self) -> set[str]:
        """Get all active status names for this entity in one query.

        Returns a set of status strings (e.g., {"banned", "staff"}).
        Use instead of multiple has_status() calls — 1 query instead of N.
        """
        tl = get_timeline()
        return await tl.active_statuses(self._timeline_entity_type, self.id)

    async def get_status_history(
        self, category: str | None = None, *, limit: int = 100
    ) -> list[StatusRecord]:
        """Get status history for this entity, newest first."""
        if category is not None:
            self._validate_category_status(category)
        tl = get_timeline()
        return await tl.get_history(
            self._timeline_entity_type, self.id, category, limit=limit
        )


# ── Global Singleton ───────────────────────────────────────────────────────

_timeline: TimelineManager | None = None


def get_timeline() -> TimelineManager:
    """Get or auto-create the global TimelineManager."""
    global _timeline
    if _timeline is None:
        _timeline = TimelineManager()
    return _timeline


def set_timeline(manager: TimelineManager) -> None:
    """Set the global TimelineManager (for testing or custom configuration)."""
    global _timeline
    _timeline = manager


# ── Escalation Rules ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EscalationRule:
    """If an entity accumulates N events matching a trigger in a time window,
    automatically apply a consequence status.

    Example: 3 "warned" events in 30 days → auto-apply "muted" for 7 days.

        EscalationRule(
            trigger_status="warned",
            threshold=3,
            window=timedelta(days=30),
            consequence_category="moderation",
            consequence_status="muted",
            consequence_expires_in=timedelta(days=7),
            consequence_reason="Auto-escalation: 3 warnings in 30 days",
        )
    """

    trigger_status: str  # Status that triggers counting (e.g., "warned")
    threshold: int  # Number of trigger events needed
    window: timedelta  # Time window to count within
    consequence_category: str  # Category to set consequence in
    consequence_status: str  # Status to auto-apply
    consequence_expires_in: timedelta | None = None  # Auto-expire consequence
    consequence_reason: str = ""  # Reason for the auto-escalation


class EscalationEngine:
    """Evaluates escalation rules when status events are recorded.

    Register rules per entity_type, then connect to the status_changed
    signal. When a matching trigger event fires, the engine counts
    recent events in the window and applies consequences if thresholds
    are met.

    Usage:
        from hyperdjango.timeline import EscalationEngine, EscalationRule

        escalation = EscalationEngine()
        escalation.add_rule("user", EscalationRule(
            trigger_status="warned",
            threshold=3,
            window=timedelta(days=30),
            consequence_category="moderation",
            consequence_status="muted",
            consequence_expires_in=timedelta(days=7),
            consequence_reason="Auto-escalation: 3 warnings in 30 days",
        ))
        escalation.connect()  # Hooks into status_changed signal
    """

    # Maximum depth for cascading escalations (e.g., warn→mute→ban stops here)
    MAX_DEPTH: int = 5

    def __init__(self) -> None:
        self._rules: dict[str, list[EscalationRule]] = {}  # entity_type → rules
        self._connected: bool = False

    def add_rule(self, entity_type: str, rule: EscalationRule) -> None:
        """Register an escalation rule for an entity type."""
        # setdefault is a single atomic op: two concurrent callers for the same
        # entity_type share ONE list instead of racing check-then-create (which
        # would let one caller's fresh [] clobber the other's — a lost rule).
        self._rules.setdefault(entity_type, []).append(rule)

    _DISPATCH_UID = "escalation_engine"

    def connect(self) -> None:
        """Connect to status_changed signal for automatic evaluation. Idempotent."""
        if self._connected:
            return
        status_changed.connect(self._on_status_changed, dispatch_uid=self._DISPATCH_UID)
        self._connected = True

    def disconnect(self) -> None:
        """Disconnect from signal."""
        status_changed.disconnect(dispatch_uid=self._DISPATCH_UID)
        self._connected = False

    async def _on_status_changed(self, sender, **kwargs) -> None:
        """Signal handler: check escalation rules when a status is set."""
        entity_type = kwargs.get("entity_type", "")
        entity_id = kwargs.get("entity_id", 0)
        new_status = kwargs.get("new_status")

        if new_status is None:
            # Status was cleared, not set — no escalation
            return

        # Recursion guard: cascading escalations (warn→mute→ban) are capped
        depth = kwargs.get("_escalation_depth", 0)
        if depth >= self.MAX_DEPTH:
            return

        rules = self._rules.get(entity_type, [])
        if not rules:
            return

        # Pre-load active statuses to avoid N+1 (one query for all rule checks)
        tl = get_timeline()
        active = await tl.active_statuses(entity_type, entity_id)

        for rule in rules:
            if rule.trigger_status != new_status:
                continue
            # Skip if consequence already active (don't double-apply)
            if rule.consequence_status in active:
                continue
            await self._evaluate_rule(entity_type, entity_id, rule, depth)

    async def _evaluate_rule(
        self,
        entity_type: str,
        entity_id: int,
        rule: EscalationRule,
        depth: int = 0,
    ) -> None:
        """Count recent trigger events (excluding ended) and apply consequence if threshold met."""
        tl = get_timeline()

        cutoff = _utcnow() - rule.window
        count = await StatusEvent.objects.filter(
            entity_type=entity_type,
            entity_id=entity_id,
            status=rule.trigger_status,
            started_at__gte=cutoff,
        ).count()

        if count >= rule.threshold:
            await tl.add_event(
                entity_type,
                entity_id,
                rule.consequence_category,
                rule.consequence_status,
                reason=rule.consequence_reason
                or (
                    f"Auto-escalation: {count} {rule.trigger_status} "
                    f"events in {rule.window.days} days"
                ),
                expires_in=rule.consequence_expires_in,
                _escalation_depth=depth + 1,
            )


# ── Admin Integration ──────────────────────────────────────────────────────


def register_timeline_admin(admin, *, per_page: int = 50) -> None:
    """Register StatusEvent in HyperAdmin with timeline-specific views.

    Adds:
    - A "Status Events" list page in the admin (browse/search/filter all events)
    - Admin actions for set_status and clear_status on registered timeline models

    Usage:
        from hyperdjango.admin import HyperAdmin
        from hyperdjango.timeline import register_timeline_admin

        admin = HyperAdmin(app, prefix="/admin", title="My App")
        register_timeline_admin(admin)
    """
    from hyperdjango.admin.fields import Action

    async def _expire_overdue_action(
        admin_instance, model_config, selected_ids, request
    ):
        """Mark selected expired events as ended."""
        tl = get_timeline()
        count = await tl.expire_overdue()
        return f"Expired {count} overdue event(s)"

    async def _cleanup_action(admin_instance, model_config, selected_ids, request):
        """Delete selected ended events older than 90 days."""
        tl = get_timeline()
        count = await tl.cleanup(days=90)
        return f"Cleaned up {count} old event(s)"

    admin.register(
        StatusEvent,
        slug="status-events",
        list_display=[
            "id",
            "entity_type",
            "entity_id",
            "category",
            "status",
            "started_at",
            "expires_at",
            "ended_at",
            "actor_id",
            "reason",
        ],
        search_fields=["entity_type", "status", "reason"],
        list_filter=["entity_type", "category", "status"],
        ordering="-started_at",
        per_page=per_page,
        readonly_fields=[
            "id",
            "entity_type",
            "entity_id",
            "category",
            "status",
            "started_at",
            "expires_at",
            "ended_at",
            "actor_id",
            "ended_by",
            "reason",
            "end_reason",
            "detail",
            "tenant_id",
            "created_at",
            "updated_at",
        ],
        actions=[
            Action(
                name="expire_overdue",
                label="Expire overdue events",
                handler=_expire_overdue_action,
            ),
            Action(
                name="cleanup_old",
                label="Clean up events older than 90 days",
                handler=_cleanup_action,
                confirm=True,
            ),
        ],
    )


def make_timeline_actions(
    model_class: type,
    category: str,
    statuses: list[str],
) -> list:
    """Generate HyperAdmin Action objects for set/clear timeline statuses.

    Creates one "Set {status}" action and one "Clear {category}" action
    per status in the list. Use in admin.register(actions=[...]).

    Usage:
        from hyperdjango.timeline import make_timeline_actions

        admin.register(
            User,
            actions=make_timeline_actions(User, "moderation", ["banned", "muted"]),
        )
    """
    from hyperdjango.admin.fields import Action

    entity_type = model_class._timeline_entity_type
    actions: list = []

    for status in statuses:

        async def _set_handler(
            admin_instance,
            model_config,
            selected_ids,
            request,
            _status=status,
            _category=category,
            _etype=entity_type,
        ):
            tl = get_timeline()
            uid = request.user.id if request.user is not None else None
            count = 0
            for eid in selected_ids:
                await tl.add_event(
                    _etype,
                    int(eid),
                    _category,
                    _status,
                    reason=f"Admin action: set {_status}",
                    actor_id=uid,
                )
                count += 1
            return f"Set {_status} on {count} {_etype}(s)"

        actions.append(
            Action(
                name=f"set_{status}",
                label=f"Set {status}",
                handler=_set_handler,
                confirm=True,
            )
        )

    async def _clear_handler(
        admin_instance,
        model_config,
        selected_ids,
        request,
        _category=category,
        _etype=entity_type,
    ):
        tl = get_timeline()
        uid = request.user.id if request.user is not None else None
        count = 0
        for eid in selected_ids:
            ended = await tl.end_status(
                _etype,
                int(eid),
                _category,
                ended_by=uid,
                end_reason="Admin action: cleared",
            )
            if ended:
                count += 1
        return f"Cleared {category} on {count} {entity_type}(s)"

    actions.append(
        Action(
            name=f"clear_{category}",
            label=f"Clear {category}",
            handler=_clear_handler,
            confirm=True,
        )
    )

    return actions


# ── Exports ────────────────────────────────────────────────────────────────

__all__ = [
    "StatusEvent",
    "StatusRecord",
    "TimelineManager",
    "StatusTimelineMixin",
    "status_changed",
    "get_timeline",
    "set_timeline",
    "EscalationRule",
    "EscalationEngine",
    "register_timeline_admin",
    "make_timeline_actions",
    "TIMELINE_INDEXES",
]
