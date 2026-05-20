"""
Multi-dimensional usage metering and accounting engine.

Track any usage across any number of dimensions: a single API call can record
requests, bytes transferred, tokens consumed, and duration — all as one event.
Aggregates incrementally into time buckets for efficient querying and reporting.

Billing providers are optional downstream hook consumers, NOT integrated.

Usage:
    from hyperdjango.metering import MeterEngine, DimensionSpec, set_meter_engine

    engine = MeterEngine(db)
    await engine.ensure_tables()
    set_meter_engine(engine)

    # Define a meter with multiple dimensions
    await engine.define_meter("llm_usage", [
        DimensionSpec("requests", "counter", "requests", "sum"),
        DimensionSpec("tokens_in", "counter", "tokens", "sum"),
        DimensionSpec("tokens_out", "counter", "tokens", "sum"),
        DimensionSpec("duration_ms", "gauge", "ms", "avg"),
    ])

    # Record a multi-dimensional event
    await engine.record("llm_usage", "acme_corp", {
        "requests": 1,
        "tokens_in": 1_000_000,
        "tokens_out": 2_000_000,
        "duration_ms": 4500,
    })

    # Query aggregated usage
    report = await engine.query_multi("llm_usage", "acme_corp",
        ["requests", "tokens_in"], period="monthly", start=start, end=end)
"""

import contextlib
import enum
import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from hyperdjango.conf import METERING_BUCKETS
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field as ModelField
from hyperdjango.models import Model
from hyperdjango.signals import Signal, log_robust_responses

_logger = logging.getLogger("hyperdjango.metering")

# ── Enums ─────────────────────────────────────────────────────────────────


class DimensionType(enum.Enum):
    COUNTER = "counter"  # monotonically increasing, summed (requests, bytes)
    GAUGE = "gauge"  # point-in-time value (storage size, active seats)
    DISTRIBUTION = "distribution"  # for percentile analysis (latency, response time)


class AggregationFunc(enum.Enum):
    SUM = "sum"
    COUNT = "count"
    LAST = "last"
    MAX = "max"
    MIN = "min"
    AVG = "avg"


class BucketSize(enum.Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


class QuotaAction(enum.Enum):
    WARN = "warn"
    REJECT = "reject"
    THROTTLE = "throttle"


class AccountType(enum.Enum):
    USER = "user"
    ORG = "org"
    TEAM = "team"
    SERVICE = "service"


# ── Signals ───────────────────────────────────────────────────────────────

meter_event_recorded = Signal(name="meter_event_recorded")
quota_exceeded = Signal(name="quota_exceeded")

# ── SQL Table Definitions ─────────────────────────────────────────────────

CREATE_METERS_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meters (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""

CREATE_METER_DIMENSIONS_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meter_dimensions (
    id SERIAL PRIMARY KEY,
    meter_id INTEGER NOT NULL REFERENCES hyper_meters(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    dimension_type TEXT NOT NULL DEFAULT 'counter',
    unit TEXT NOT NULL DEFAULT 'events',
    default_agg TEXT NOT NULL DEFAULT 'sum',
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(meter_id, name)
)
"""

CREATE_METER_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meter_events (
    id BIGSERIAL PRIMARY KEY,
    meter_id INTEGER NOT NULL REFERENCES hyper_meters(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    tenant_id INTEGER,
    idempotency_key TEXT UNIQUE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

CREATE_METER_EVENT_VALUES_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meter_event_values (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES hyper_meter_events(id) ON DELETE CASCADE,
    dimension_id INTEGER NOT NULL REFERENCES hyper_meter_dimensions(id) ON DELETE CASCADE,
    value DOUBLE PRECISION NOT NULL
)
"""

CREATE_METER_AGGREGATES_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meter_aggregates (
    id BIGSERIAL PRIMARY KEY,
    meter_id INTEGER NOT NULL REFERENCES hyper_meters(id) ON DELETE CASCADE,
    dimension_id INTEGER NOT NULL REFERENCES hyper_meter_dimensions(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    bucket_size TEXT NOT NULL,
    bucket_start TIMESTAMPTZ NOT NULL,
    value_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    value_count BIGINT NOT NULL DEFAULT 0,
    value_min DOUBLE PRECISION,
    value_max DOUBLE PRECISION,
    value_last DOUBLE PRECISION,
    tenant_id INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(meter_id, dimension_id, account_id, bucket_size, bucket_start)
)
"""

CREATE_METER_ACCOUNTS_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meter_accounts (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT '',
    parent_account_id TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    tenant_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""

CREATE_METER_QUOTAS_SQL = """
CREATE TABLE IF NOT EXISTS hyper_meter_quotas (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    dimension_id INTEGER NOT NULL REFERENCES hyper_meter_dimensions(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    limit_value DOUBLE PRECISION NOT NULL,
    action TEXT NOT NULL DEFAULT 'warn',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, dimension_id, period)
)
"""

CREATE_METERING_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_me_acct_ts ON hyper_meter_events (account_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_me_meter_ts ON hyper_meter_events (meter_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_me_tenant ON hyper_meter_events (tenant_id) WHERE tenant_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_me_idempotency ON hyper_meter_events (idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_mev_event ON hyper_meter_event_values (event_id)",
    "CREATE INDEX IF NOT EXISTS idx_mev_dim ON hyper_meter_event_values (dimension_id, value)",
    "CREATE INDEX IF NOT EXISTS idx_ma_acct ON hyper_meter_aggregates (account_id, bucket_size, bucket_start DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ma_meter ON hyper_meter_aggregates (meter_id, bucket_size, bucket_start DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ma_tenant ON hyper_meter_aggregates (tenant_id) WHERE tenant_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_mac_parent ON hyper_meter_accounts (parent_account_id) WHERE parent_account_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_mac_type ON hyper_meter_accounts (account_type)",
    "CREATE INDEX IF NOT EXISTS idx_mac_tier ON hyper_meter_accounts (tier)",
    "CREATE INDEX IF NOT EXISTS idx_mq_acct ON hyper_meter_quotas (account_id)",
]

# ── Dataclasses ───────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class DimensionSpec:
    """Defines a dimension on a meter."""

    name: str
    dimension_type: str  # "counter" | "gauge" | "distribution"
    unit: str  # "bytes" | "ms" | "requests" | "tokens" | "seats"
    default_agg: str  # "sum" | "count" | "last" | "max" | "min" | "avg"


@dataclass(slots=True, frozen=True)
class AggregateResult:
    """Aggregated value for a single dimension over a time period."""

    dimension_name: str
    unit: str
    value_sum: float
    value_count: int
    value_min: float | None
    value_max: float | None
    value_last: float | None

    @property
    def value_avg(self) -> float:
        return self.value_sum / self.value_count if self.value_count > 0 else 0.0


@dataclass(slots=True, frozen=True)
class UsageReport:
    """Multi-dimensional usage report for an account over a period."""

    meter_name: str
    account_id: str
    period: str
    start: datetime
    end: datetime
    dimensions: dict[str, AggregateResult]


@dataclass(slots=True, frozen=True)
class QuotaDecision:
    """Result of a quota check."""

    allowed: bool
    remaining: float
    limit_value: float
    action: str
    dimension_name: str


@dataclass(slots=True, frozen=True)
class MeterHookContext:
    """Context passed to hooks on every recorded event."""

    meter_name: str
    account_id: str
    event_id: int
    dimensions: dict[str, float]
    tenant_id: int | None
    timestamp: datetime


@dataclass(slots=True, frozen=True)
class PeriodExport:
    """Provider-agnostic export for billing hooks."""

    meter_name: str
    account_id: str
    period_start: datetime
    period_end: datetime
    dimensions: dict[str, AggregateResult]


# ── Model Classes ─────────────────────────────────────────────────────────


class Meter(TimestampMixin, Model):
    class Meta:
        table = "hyper_meters"

    id: int = ModelField(primary_key=True, auto=True)
    name: str = ModelField(unique=True)
    description: str = ModelField(default="")
    is_active: bool = ModelField(default=True)


class MeterDimension(Model):
    class Meta:
        table = "hyper_meter_dimensions"
        unique_together = [("meter_id", "name")]

    id: int = ModelField(primary_key=True, auto=True)
    meter_id: int = ModelField(foreign_key=Meter)
    name: str = ModelField()
    dimension_type: DimensionType = ModelField(default=DimensionType.COUNTER)
    unit: str = ModelField(default="events")
    default_agg: AggregationFunc = ModelField(default=AggregationFunc.SUM)
    sort_order: int = ModelField(default=0)


class MeterEvent(Model):
    class Meta:
        table = "hyper_meter_events"

    id: int = ModelField(primary_key=True, auto=True)
    meter_id: int = ModelField(foreign_key=Meter)
    account_id: str = ModelField()
    tenant_id: int | None = ModelField(default=None)
    idempotency_key: str | None = ModelField(
        default=None,
        unique=True,
    )
    timestamp: datetime = ModelField(default=None)


class MeterEventValue(Model):
    class Meta:
        table = "hyper_meter_event_values"

    id: int = ModelField(primary_key=True, auto=True)
    event_id: int = ModelField(foreign_key=MeterEvent)
    dimension_id: int = ModelField(foreign_key=MeterDimension)
    value: float = ModelField(default=0.0)


class MeterAggregate(Model):
    class Meta:
        table = "hyper_meter_aggregates"
        unique_together = [
            ("meter_id", "dimension_id", "account_id", "bucket_size", "bucket_start")
        ]

    id: int = ModelField(primary_key=True, auto=True)
    meter_id: int = ModelField(foreign_key=Meter)
    dimension_id: int = ModelField(foreign_key=MeterDimension)
    account_id: str = ModelField()
    bucket_size: BucketSize = ModelField()
    bucket_start: datetime = ModelField(default=None)
    value_sum: float = ModelField(default=0.0)
    value_count: int = ModelField(default=0)
    value_min: float | None = ModelField(default=None)
    value_max: float | None = ModelField(default=None)
    value_last: float | None = ModelField(default=None)
    tenant_id: int | None = ModelField(default=None)
    updated_at: datetime = ModelField(default=None)


class MeterAccount(TimestampMixin, Model):
    class Meta:
        table = "hyper_meter_accounts"

    id: int = ModelField(primary_key=True, auto=True)
    account_id: str = ModelField()
    display_name: str = ModelField(default="")
    account_type: AccountType = ModelField(default=AccountType.USER)
    tier: str = ModelField(default="")
    parent_account_id: str | None = ModelField(
        default=None,
    )
    is_active: bool = ModelField(default=True)
    tenant_id: int | None = ModelField(default=None)


class MeterQuota(Model):
    class Meta:
        table = "hyper_meter_quotas"
        unique_together = [("account_id", "dimension_id", "period")]

    id: int = ModelField(primary_key=True, auto=True)
    account_id: str = ModelField()
    dimension_id: int = ModelField(foreign_key=MeterDimension)
    period: BucketSize = ModelField()
    limit_value: float = ModelField(default=0.0)
    action: QuotaAction = ModelField(default=QuotaAction.WARN)
    is_active: bool = ModelField(default=True)
    created_at: datetime = ModelField(default=None)


# ── Hook System ───────────────────────────────────────────────────────────


class MeterHook:
    """Base class for meter event consumers. Override methods you care about."""

    async def on_event(self, ctx: MeterHookContext) -> None:
        """Called for every recorded event."""

    async def on_quota_exceeded(
        self, ctx: MeterHookContext, decision: QuotaDecision
    ) -> None:
        """Called when a quota check fails."""

    async def on_period_close(self, export: PeriodExport) -> None:
        """Called when exporting a billing period."""


class QuotaEnforcementHook(MeterHook):
    """Check quotas on every event. Sets quota decisions."""

    def __init__(self, engine: MeterEngine):
        self._engine = engine

    async def on_event(self, ctx: MeterHookContext) -> None:
        for dim_name in ctx.dimensions:
            decision = await self._engine.check_quota(
                ctx.account_id, ctx.meter_name, dim_name, "monthly"
            )
            if not decision.allowed:
                # Notification of an already-made decision: a failing listener
                # must not abort event processing. Robust dispatch + loud log.
                responses = await quota_exceeded.send_robust(
                    sender=self, ctx=ctx, decision=decision
                )
                log_robust_responses(responses, _logger, "quota_exceeded")
                # Also fire the documented MeterHook.on_quota_exceeded override
                # on every registered hook (in addition to the signal), so
                # subclasses that override it actually run. Same guarded style
                # as the on_event dispatch loop: a failing hook must not abort
                # event processing.
                for hook in self._engine._hooks:
                    with contextlib.suppress(Exception):
                        await hook.on_quota_exceeded(ctx, decision)


class AlertHook(MeterHook):
    """Fire callback when dimension values cross thresholds."""

    def __init__(self, thresholds: dict[str, float], callback: Callable):
        self._thresholds = thresholds
        self._callback = callback

    async def on_event(self, ctx: MeterHookContext) -> None:
        for dim_name, value in ctx.dimensions.items():
            threshold = self._thresholds.get(dim_name)
            if threshold is not None and value > threshold:
                if inspect.iscoroutinefunction(self._callback):
                    await self._callback(ctx, dim_name, value, threshold)
                else:
                    self._callback(ctx, dim_name, value, threshold)


# ── Bucket Helpers ────────────────────────────────────────────────────────

_BUCKET_TRUNCATE = {
    "hourly": "hour",
    "daily": "day",
    "monthly": "month",
}

# ── MeterEngine ───────────────────────────────────────────────────────────


class MeterEngine:
    """Multi-dimensional usage metering engine.

    Records events, incrementally aggregates into time buckets,
    and provides query/report/quota APIs.
    """

    def __init__(self, db):
        self.db = db
        self._meter_cache: dict[str, int] = {}  # name → meter_id
        self._dim_cache: dict[
            tuple[int, str], int
        ] = {}  # (meter_id, dim_name) → dim_id
        self._dim_specs: dict[int, DimensionSpec] = {}  # dim_id → spec
        self._hooks: list[MeterHook] = []
        self._lock = threading.Lock()

    async def ensure_tables(self) -> None:
        """Create all metering tables and indexes."""
        for sql in (
            CREATE_METERS_SQL,
            CREATE_METER_DIMENSIONS_SQL,
            CREATE_METER_EVENTS_SQL,
            CREATE_METER_EVENT_VALUES_SQL,
            CREATE_METER_AGGREGATES_SQL,
            CREATE_METER_ACCOUNTS_SQL,
            CREATE_METER_QUOTAS_SQL,
        ):
            await self.db.execute(sql)
        for idx_sql in CREATE_METERING_INDEXES_SQL:
            with contextlib.suppress(Exception):
                await self.db.execute(idx_sql)

    def register_hook(self, hook: MeterHook) -> None:
        """Register a downstream hook consumer."""
        with self._lock:
            self._hooks.append(hook)

    # ── Meter Definition ──────────────────────────────────────────────────

    async def define_meter(
        self,
        name: str,
        dimensions: list[DimensionSpec],
        description: str = "",
    ) -> int:
        """Define a meter with its dimensions. Returns meter_id.

        Idempotent: if meter already exists, updates dimensions.
        """
        row = await self.db.query_one(
            "INSERT INTO hyper_meters (name, description) VALUES ($1, $2) "
            "ON CONFLICT (name) DO UPDATE SET description = $2, updated_at = NOW() "
            "RETURNING id",
            name,
            description,
        )
        meter_id = row["id"]
        self._meter_cache[name] = meter_id

        for i, dim in enumerate(dimensions):
            dim_row = await self.db.query_one(
                "INSERT INTO hyper_meter_dimensions "
                "(meter_id, name, dimension_type, unit, default_agg, sort_order) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (meter_id, name) DO UPDATE SET "
                "dimension_type = $3, unit = $4, default_agg = $5, sort_order = $6 "
                "RETURNING id",
                meter_id,
                dim.name,
                dim.dimension_type,
                dim.unit,
                dim.default_agg,
                i,
            )
            dim_id = dim_row["id"]
            self._dim_cache[(meter_id, dim.name)] = dim_id
            self._dim_specs[dim_id] = dim

        return meter_id

    async def _resolve_meter(self, meter_name: str) -> int:
        """Get meter_id, using cache or DB lookup."""
        if meter_name in self._meter_cache:
            return self._meter_cache[meter_name]
        row = await self.db.query_one(
            "SELECT id FROM hyper_meters WHERE name = $1", meter_name
        )
        if row is None:
            raise ValueError(f"Meter not found: {meter_name}")
        meter_id = row["id"]
        self._meter_cache[meter_name] = meter_id
        return meter_id

    async def _resolve_dimension(self, meter_id: int, dim_name: str) -> int:
        """Get dimension_id, using cache or DB lookup."""
        key = (meter_id, dim_name)
        if key in self._dim_cache:
            return self._dim_cache[key]
        row = await self.db.query_one(
            "SELECT id FROM hyper_meter_dimensions WHERE meter_id = $1 AND name = $2",
            meter_id,
            dim_name,
        )
        if row is None:
            raise ValueError(f"Dimension not found: {dim_name} on meter {meter_id}")
        dim_id = row["id"]
        self._dim_cache[key] = dim_id
        return dim_id

    # ── Event Recording ───────────────────────────────────────────────────

    async def record(
        self,
        meter_name: str,
        account_id: str,
        dimensions: dict[str, float],
        tenant_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> int | None:
        """Record a multi-dimensional event. Returns the new event_id.

        Atomically (single transaction): inserts the event + all dimension values
        + upserts aggregates for every bucket size. Either the whole record lands
        or none of it does.

        Returns ``None`` when ``idempotency_key`` matches an already-recorded event
        (the duplicate is a no-op). Callers can distinguish "recorded" from
        "deduplicated" with ``if event_id is not None:`` — the result is never a
        sentinel like ``-1`` that would read as a truthy, usable id.
        """
        # Auto-read tenant from context if not provided
        if tenant_id is None:
            from hyperdjango.tenancy import get_tenant

            tenant = get_tenant()
            if tenant is not None:
                tenant_id = tenant.tenant_id

        meter_id = await self._resolve_meter(meter_name)

        # Resolve every dimension id up front (cached after warmup, so this is
        # typically zero round-trips), then sort by dimension_id. The aggregate
        # upsert locks hyper_meter_aggregates conflict rows in unnest order, so a
        # deterministic (dimension_id, bucket_size) ordering guarantees any two
        # concurrent record() calls acquire those row locks in the SAME order —
        # eliminating the 40P01 deadlock window when callers pass dimensions in
        # different insertion orders.
        resolved: list[tuple[int, float]] = []
        for dim_name, value in dimensions.items():
            dim_id = await self._resolve_dimension(meter_id, dim_name)
            resolved.append((dim_id, float(value)))
        resolved.sort(key=lambda p: p[0])

        val_dim_ids = [d for d, _ in resolved]
        val_values = [v for _, v in resolved]

        agg_rows = sorted(
            (
                (d, bucket, _BUCKET_TRUNCATE[bucket], v)
                for d, v in resolved
                for bucket in METERING_BUCKETS
            ),
            key=lambda r: (r[0], r[1]),  # (dimension_id, bucket_size)
        )
        agg_dim_ids = [r[0] for r in agg_rows]
        agg_bucket_sizes = [r[1] for r in agg_rows]
        agg_truncs = [r[2] for r in agg_rows]
        agg_values = [r[3] for r in agg_rows]

        # One transaction: event + values + aggregates commit or roll back together.
        async with self.db.transaction():
            # 1. Insert event
            if idempotency_key:
                event_row = await self.db.query_one(
                    "INSERT INTO hyper_meter_events (meter_id, account_id, tenant_id, idempotency_key) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                    meter_id,
                    account_id,
                    tenant_id,
                    idempotency_key,
                )
                if event_row is None:
                    return None  # Duplicate idempotency key — event already recorded
            else:
                event_row = await self.db.query_one(
                    "INSERT INTO hyper_meter_events (meter_id, account_id, tenant_id) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    meter_id,
                    account_id,
                    tenant_id,
                )
            event_id = event_row["id"]

            if val_dim_ids:
                # 2. Insert all dimension values in one multi-row INSERT via unnest.
                await self.db.execute(
                    "INSERT INTO hyper_meter_event_values (event_id, dimension_id, value) "
                    "SELECT $1, d, v FROM unnest($2::int[], $3::float8[]) AS t(d, v)",
                    event_id,
                    val_dim_ids,
                    val_values,
                )

                # 3. Upsert every (dimension, bucket) aggregate in ONE statement.
                # Each row carries its own date_trunc field, so the three bucket
                # sizes collapse into a single round-trip regardless of dimension
                # count. Conflict targets are unique across the batch (distinct
                # dim_id × bucket_size), so DO UPDATE never touches a row twice.
                # Rows are pre-sorted by (dimension_id, bucket_size) for a
                # deterministic lock order (see above).
                await self.db.execute(
                    "INSERT INTO hyper_meter_aggregates "
                    "(meter_id, dimension_id, account_id, bucket_size, bucket_start, "
                    "value_sum, value_count, value_min, value_max, value_last, tenant_id) "
                    "SELECT $1, t.dim_id, $2, t.bucket_size, date_trunc(t.trunc, NOW()), "
                    "t.val, 1, t.val, t.val, t.val, $3 "
                    "FROM unnest($4::int[], $5::text[], $6::text[], $7::float8[]) "
                    "AS t(dim_id, bucket_size, trunc, val) "
                    "ON CONFLICT (meter_id, dimension_id, account_id, bucket_size, bucket_start) "
                    "DO UPDATE SET "
                    "value_sum = hyper_meter_aggregates.value_sum + EXCLUDED.value_sum, "
                    "value_count = hyper_meter_aggregates.value_count + EXCLUDED.value_count, "
                    "value_min = LEAST(hyper_meter_aggregates.value_min, EXCLUDED.value_min), "
                    "value_max = GREATEST(hyper_meter_aggregates.value_max, EXCLUDED.value_max), "
                    "value_last = EXCLUDED.value_last, updated_at = NOW()",
                    meter_id,
                    account_id,
                    tenant_id,
                    agg_dim_ids,
                    agg_bucket_sizes,
                    agg_truncs,
                    agg_values,
                )

        # 4. Fire hooks
        now = datetime.now(UTC)
        ctx = MeterHookContext(
            meter_name=meter_name,
            account_id=account_id,
            event_id=event_id,
            dimensions=dimensions,
            tenant_id=tenant_id,
            timestamp=now,
        )
        for hook in self._hooks:
            with contextlib.suppress(Exception):
                await hook.on_event(ctx)

        # 5. Fire signal. Post-commit: the event row is already recorded, so a
        # failing receiver must not abort recording. Robust dispatch + loud log.
        responses = await meter_event_recorded.send_robust(sender=self, ctx=ctx)
        log_robust_responses(responses, _logger, "meter_event_recorded")

        return event_id

    # ── Querying ──────────────────────────────────────────────────────────

    async def query(
        self,
        meter_name: str,
        account_id: str,
        dimension_name: str,
        period: str,
        start: datetime,
        end: datetime,
    ) -> AggregateResult:
        """Query aggregated usage for a single dimension."""
        meter_id = await self._resolve_meter(meter_name)
        dim_id = await self._resolve_dimension(meter_id, dimension_name)

        row = await self.db.query_one(
            "SELECT SUM(value_sum) AS s, SUM(value_count) AS c, "
            "MIN(value_min) AS mn, MAX(value_max) AS mx, "
            "(ARRAY_AGG(value_last ORDER BY bucket_start DESC))[1] AS lst "
            "FROM hyper_meter_aggregates "
            "WHERE meter_id = $1 AND dimension_id = $2 AND account_id = $3 "
            "AND bucket_size = $4 AND bucket_start >= $5 AND bucket_start < $6",
            meter_id,
            dim_id,
            account_id,
            period,
            start,
            end,
        )

        # Get unit from dim spec or DB
        spec = self._dim_specs.get(dim_id)
        unit = spec.unit if spec else ""

        if row is None or row["s"] is None:
            return AggregateResult(dimension_name, unit, 0.0, 0, None, None, None)

        return AggregateResult(
            dimension_name=dimension_name,
            unit=unit,
            value_sum=float(row["s"] or 0),
            value_count=int(row["c"] or 0),
            value_min=float(row["mn"]) if row["mn"] is not None else None,
            value_max=float(row["mx"]) if row["mx"] is not None else None,
            value_last=float(row["lst"]) if row["lst"] is not None else None,
        )

    async def query_multi(
        self,
        meter_name: str,
        account_id: str,
        dimension_names: list[str],
        period: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, AggregateResult]:
        """Query aggregated usage for multiple dimensions."""
        results: dict[str, AggregateResult] = {}
        for dim_name in dimension_names:
            results[dim_name] = await self.query(
                meter_name, account_id, dim_name, period, start, end
            )
        return results

    async def query_hierarchy(
        self,
        meter_name: str,
        root_account_id: str,
        dimension_name: str,
        period: str,
        start: datetime,
        end: datetime,
    ) -> AggregateResult:
        """Query aggregated usage across an account hierarchy (recursive CTE)."""
        meter_id = await self._resolve_meter(meter_name)
        dim_id = await self._resolve_dimension(meter_id, dimension_name)
        spec = self._dim_specs.get(dim_id)
        unit = spec.unit if spec else ""

        row = await self.db.query_one(
            "WITH RECURSIVE sub_accounts AS ("
            "  SELECT account_id FROM hyper_meter_accounts WHERE account_id = $1 "
            "  UNION ALL "
            "  SELECT ma.account_id FROM hyper_meter_accounts ma "
            "  JOIN sub_accounts sa ON ma.parent_account_id = sa.account_id"
            ") "
            "SELECT SUM(value_sum) AS s, SUM(value_count) AS c, "
            "MIN(value_min) AS mn, MAX(value_max) AS mx "
            "FROM hyper_meter_aggregates "
            "WHERE meter_id = $2 AND dimension_id = $3 "
            "AND account_id IN (SELECT account_id FROM sub_accounts) "
            "AND bucket_size = $4 AND bucket_start >= $5 AND bucket_start < $6",
            root_account_id,
            meter_id,
            dim_id,
            period,
            start,
            end,
        )

        if row is None or row["s"] is None:
            return AggregateResult(dimension_name, unit, 0.0, 0, None, None, None)

        return AggregateResult(
            dimension_name=dimension_name,
            unit=unit,
            value_sum=float(row["s"] or 0),
            value_count=int(row["c"] or 0),
            value_min=float(row["mn"]) if row["mn"] is not None else None,
            value_max=float(row["mx"]) if row["mx"] is not None else None,
            value_last=None,
        )

    # ── Quotas ────────────────────────────────────────────────────────────

    async def set_quota(
        self,
        account_id: str,
        meter_name: str,
        dimension_name: str,
        period: str,
        limit_value: float,
        action: str = "warn",
    ) -> None:
        """Set a usage quota for an account on a specific dimension."""
        meter_id = await self._resolve_meter(meter_name)
        dim_id = await self._resolve_dimension(meter_id, dimension_name)
        await self.db.execute(
            "INSERT INTO hyper_meter_quotas (account_id, dimension_id, period, limit_value, action) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (account_id, dimension_id, period) "
            "DO UPDATE SET limit_value = $4, action = $5",
            account_id,
            dim_id,
            period,
            limit_value,
            action,
        )

    async def check_quota(
        self,
        account_id: str,
        meter_name: str,
        dimension_name: str,
        period: str,
    ) -> QuotaDecision:
        """Check if account is within quota for a dimension."""
        meter_id = await self._resolve_meter(meter_name)
        dim_id = await self._resolve_dimension(meter_id, dimension_name)

        quota_row = await self.db.query_one(
            "SELECT limit_value, action FROM hyper_meter_quotas "
            "WHERE account_id = $1 AND dimension_id = $2 AND period = $3 AND is_active = TRUE",
            account_id,
            dim_id,
            period,
        )
        if quota_row is None:
            return QuotaDecision(True, float("inf"), 0, "warn", dimension_name)

        limit_val = float(quota_row["limit_value"])
        action = quota_row["action"]

        # Get current usage for this period
        trunc = _BUCKET_TRUNCATE.get(period, "month")
        usage_row = await self.db.query_one(
            f"SELECT value_sum FROM hyper_meter_aggregates "
            f"WHERE meter_id = $1 AND dimension_id = $2 AND account_id = $3 "
            f"AND bucket_size = $4 AND bucket_start = date_trunc('{trunc}', NOW())",
            meter_id,
            dim_id,
            account_id,
            period,
        )
        current = float(usage_row["value_sum"]) if usage_row else 0.0
        remaining = max(0.0, limit_val - current)
        allowed = current < limit_val

        return QuotaDecision(allowed, remaining, limit_val, action, dimension_name)

    # ── Export ────────────────────────────────────────────────────────────

    async def export_period(
        self,
        meter_name: str,
        account_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> PeriodExport:
        """Export aggregated usage for a billing period. Provider-agnostic."""
        meter_id = await self._resolve_meter(meter_name)

        # Get all dimensions for this meter
        dim_rows = await self.db.query(
            "SELECT id, name, unit FROM hyper_meter_dimensions WHERE meter_id = $1 ORDER BY sort_order",
            meter_id,
        )

        dims: dict[str, AggregateResult] = {}
        for dr in dim_rows:
            dim_name = dr["name"]
            result = await self.query(
                meter_name, account_id, dim_name, "monthly", period_start, period_end
            )
            dims[dim_name] = result

        export = PeriodExport(
            meter_name=meter_name,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
            dimensions=dims,
        )

        # Fire the documented MeterHook.on_period_close override on every
        # registered hook. Same guarded style as the on_event dispatch loop:
        # a failing hook must not abort the export.
        for hook in self._hooks:
            with contextlib.suppress(Exception):
                await hook.on_period_close(export)

        return export

    # ── Maintenance ───────────────────────────────────────────────────────

    async def reaggregate(
        self,
        meter_name: str,
        start: datetime,
        end: datetime,
    ) -> int:
        """Rebuild aggregates from raw events. Returns count of aggregates rebuilt."""
        meter_id = await self._resolve_meter(meter_name)

        # Clear existing aggregates for the range
        await self.db.execute(
            "DELETE FROM hyper_meter_aggregates "
            "WHERE meter_id = $1 AND bucket_start >= $2 AND bucket_start < $3",
            meter_id,
            start,
            end,
        )

        # Rebuild from events
        count = 0
        for bucket in METERING_BUCKETS:
            trunc = _BUCKET_TRUNCATE[bucket]
            result = await self.db.execute(
                f"INSERT INTO hyper_meter_aggregates "
                f"(meter_id, dimension_id, account_id, bucket_size, bucket_start, "
                f"value_sum, value_count, value_min, value_max, value_last, tenant_id) "
                f"SELECT e.meter_id, v.dimension_id, e.account_id, '{bucket}', "
                f"date_trunc('{trunc}', e.timestamp), "
                f"SUM(v.value), COUNT(*), MIN(v.value), MAX(v.value), "
                f"(ARRAY_AGG(v.value ORDER BY e.timestamp DESC))[1], "
                f"e.tenant_id "
                f"FROM hyper_meter_events e "
                f"JOIN hyper_meter_event_values v ON v.event_id = e.id "
                f"WHERE e.meter_id = $1 AND e.timestamp >= $2 AND e.timestamp < $3 "
                f"GROUP BY e.meter_id, v.dimension_id, e.account_id, "
                f"date_trunc('{trunc}', e.timestamp), e.tenant_id "
                f"ON CONFLICT (meter_id, dimension_id, account_id, bucket_size, bucket_start) "
                f"DO UPDATE SET "
                f"value_sum = EXCLUDED.value_sum, value_count = EXCLUDED.value_count, "
                f"value_min = EXCLUDED.value_min, value_max = EXCLUDED.value_max, "
                f"value_last = EXCLUDED.value_last, updated_at = NOW()",
                meter_id,
                start,
                end,
            )
            count += result

        return count

    async def cleanup(
        self,
        retain_events_days: int = 90,
        retain_aggregates_days: int = 730,
    ) -> int:
        """Delete old events and aggregates. Returns total rows deleted."""
        deleted = 0
        deleted += await self.db.execute(
            "DELETE FROM hyper_meter_events WHERE timestamp < NOW() - $1 * INTERVAL '1 day'",
            retain_events_days,
        )
        deleted += await self.db.execute(
            "DELETE FROM hyper_meter_aggregates WHERE bucket_start < NOW() - $1 * INTERVAL '1 day'",
            retain_aggregates_days,
        )
        return deleted


# ── MeteringMiddleware ────────────────────────────────────────────────────


@dataclass
class MeteringMiddleware:
    """Middleware that records multi-dimensional usage events per request.

    The dimension_extractor function extracts dimensions from request+response:
        def extract(request, response) -> dict[str, float]:
            return {"requests": 1, "bytes_out": len(response.body)}
    """

    engine: MeterEngine
    meter_name: str = "http_requests"
    account_resolver: Callable | None = None
    dimension_extractor: Callable | None = None
    quota_enforced: bool = False

    async def __call__(self, request, call_next):
        from hyperdjango.response import Response

        account_id = (
            self.account_resolver(request)
            if self.account_resolver
            else _default_account_id(request)
        )
        if account_id is None:
            return await call_next(request)

        # Pre-request quota check
        if self.quota_enforced:
            decision = await self.engine.check_quota(
                account_id, self.meter_name, "requests", "monthly"
            )
            if not decision.allowed and decision.action == "reject":
                return Response.error(
                    429,
                    f"Quota exceeded: {decision.dimension_name} {decision.action}",
                )

        response = await call_next(request)

        # Extract dimensions and record
        if self.dimension_extractor:
            dims = self.dimension_extractor(request, response)
        else:
            dims = {"requests": 1}

        await self.engine.record(self.meter_name, account_id, dims)
        return response


def _default_account_id(request) -> str | None:
    """Extract account_id from request.user.id or request.user.tenant_id."""
    user = request.user
    if user is None:
        return None
    # hasattr required: user model is pluggable, may not have these fields
    if hasattr(user, "tenant_id") and user.tenant_id is not None:
        return str(user.tenant_id)
    if hasattr(user, "id") and user.id is not None:
        return str(user.id)
    return None


# ── Admin Registration ────────────────────────────────────────────────────


def register_metering_admin(admin) -> None:
    """Register all metering models with HyperAdmin."""
    admin.register(
        Meter,
        list_display=["name", "description", "is_active"],
        search_fields=["name", "description"],
        list_filter=["is_active"],
    )
    admin.register(
        MeterDimension,
        list_display=[
            "meter_id",
            "name",
            "dimension_type",
            "unit",
            "default_agg",
            "sort_order",
        ],
        list_filter=["dimension_type", "default_agg"],
    )
    admin.register(
        MeterAccount,
        list_display=[
            "account_id",
            "display_name",
            "account_type",
            "tier",
            "is_active",
        ],
        search_fields=["account_id", "display_name"],
        list_filter=["account_type", "tier", "is_active"],
    )
    admin.register(
        MeterQuota,
        list_display=[
            "account_id",
            "dimension_id",
            "period",
            "limit_value",
            "action",
            "is_active",
        ],
        list_filter=["period", "action", "is_active"],
    )
    admin.register(
        MeterAggregate,
        list_display=[
            "meter_id",
            "dimension_id",
            "account_id",
            "bucket_size",
            "bucket_start",
            "value_sum",
            "value_count",
        ],
        readonly_fields=["id", "updated_at"],
        list_filter=["bucket_size"],
    )


# ── Global Singleton ──────────────────────────────────────────────────────

_meter_engine: MeterEngine | None = None


def get_meter_engine() -> MeterEngine | None:
    """Get the global meter engine, or None if not configured."""
    return _meter_engine


def set_meter_engine(engine: MeterEngine) -> None:
    """Set the global meter engine."""
    global _meter_engine
    _meter_engine = engine
