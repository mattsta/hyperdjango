# Multi-Dimensional Usage Metering

Track any usage across any number of dimensions. A single API call can record requests, bytes transferred, tokens consumed, and duration — all as one event. Aggregates incrementally into time buckets. Billing providers are optional downstream hook consumers.

## Quick Start

```python
from hyperdjango.metering import MeterEngine, DimensionSpec, set_meter_engine

engine = MeterEngine(db)
await engine.ensure_tables()
set_meter_engine(engine)

# Define a meter with dimensions
await engine.define_meter(
    "api_usage",
    [
        DimensionSpec("requests", "counter", "requests", "sum"),
        DimensionSpec("bytes_in", "counter", "bytes", "sum"),
        DimensionSpec("bytes_out", "counter", "bytes", "sum"),
        DimensionSpec("duration_ms", "gauge", "ms", "avg"),
    ],
)

# Record a multi-dimensional event
await engine.record(
    "api_usage",
    account_id="acme_corp",
    dimensions={
        "requests": 1,
        "bytes_in": 943_718_400,
        "bytes_out": 2_147_483_648,
        "duration_ms": 4500,
    },
)

# Query aggregated usage
from datetime import datetime, UTC

report = await engine.query_multi(
    "api_usage",
    "acme_corp",
    ["requests", "bytes_in", "bytes_out", "duration_ms"],
    period="monthly",
    start=datetime(2026, 3, 1, tzinfo=UTC),
    end=datetime(2026, 4, 1, tzinfo=UTC),
)

print(f"Requests: {report['requests'].value_sum:,.0f}")
print(f"Data in: {report['bytes_in'].value_sum / 1e9:.1f} GB")
print(f"Avg latency: {report['duration_ms'].value_avg:.0f} ms")
```

## How It Works

1. **Define meters** with named dimensions — each dimension has a type, unit, and default aggregation
2. **Record events** — one call with a dict of dimension values
3. **Aggregates update incrementally** — every `record()` upserts hourly, daily, and monthly rollups atomically
4. **Query aggregates** — read pre-computed rollups, not raw events
5. **Hooks fire** on every event — for quotas, alerts, billing export, or anything custom

All tables are **regular (LOGGED)** — financial/accounting data survives crashes.

## Defining Meters

A meter has a name and a list of dimensions. Each dimension defines what it measures:

```python
from hyperdjango.metering import DimensionSpec

# LLM API usage — 5 dimensions per event
await engine.define_meter(
    "llm_usage",
    [
        DimensionSpec("requests", "counter", "requests", "sum"),
        DimensionSpec("tokens_in", "counter", "tokens", "sum"),
        DimensionSpec("tokens_out", "counter", "tokens", "sum"),
        DimensionSpec("cost_units", "counter", "units", "sum"),
        DimensionSpec("duration_ms", "gauge", "ms", "avg"),
    ],
)

# Storage tracking — gauge type (latest value matters, not sum)
await engine.define_meter(
    "storage",
    [
        DimensionSpec("total_bytes", "gauge", "bytes", "last"),
        DimensionSpec("file_count", "gauge", "files", "last"),
    ],
)

# Seat tracking — peak in period
await engine.define_meter(
    "seats",
    [
        DimensionSpec("active_users", "gauge", "users", "max"),
        DimensionSpec("concurrent_sessions", "gauge", "sessions", "max"),
    ],
)
```

### DimensionSpec Fields

| Field            | Values                                             | Description                                                                                  |
| ---------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `name`           | any string                                         | Dimension identifier (e.g., "tokens_in")                                                     |
| `dimension_type` | `"counter"` `"gauge"` `"distribution"`             | Counter = summed over time. Gauge = point-in-time value. Distribution = percentile analysis. |
| `unit`           | any string                                         | Human-readable unit (e.g., "bytes", "ms", "tokens", "users")                                 |
| `default_agg`    | `"sum"` `"count"` `"last"` `"max"` `"min"` `"avg"` | Default aggregation function for reports                                                     |

`define_meter` is idempotent — calling it again updates the dimensions.

## Recording Events

### Single event

```python
await engine.record(
    "llm_usage",
    account_id="acme_corp",
    dimensions={
        "requests": 1,
        "tokens_in": 1_000_000,
        "tokens_out": 2_000_000,
        "cost_units": 15.5,
        "duration_ms": 4500,
    },
)
```

One call. Five dimensions. Each value is stored as a separate `MeterEventValue` row (fully relational, no JSON). The engine also atomically upserts 5×3 = 15 aggregate rows (5 dimensions × hourly/daily/monthly buckets).

### With idempotency key (prevent duplicates)

```python
await engine.record(
    "llm_usage",
    "acme_corp",
    dimensions={"requests": 1, "tokens_out": 500_000},
    idempotency_key="req-abc-123",
)
# Second call with same key is a no-op (returns None)
await engine.record(
    "llm_usage",
    "acme_corp",
    dimensions={"requests": 1, "tokens_out": 500_000},
    idempotency_key="req-abc-123",
)  # Returns None — duplicate, not recorded
```

`record()` returns the new integer `event_id` on success, or `None` when the
`idempotency_key` matched an already-recorded event. Distinguish the two with
`if event_id is not None:` — the deduplicated case is never a usable id.

### With explicit tenant

```python
await engine.record(
    "llm_usage",
    "acme_corp",
    dimensions={"requests": 1},
    tenant_id=42,
)
```

If `tenant_id` is not passed, it auto-reads from `get_tenant()` context (set by TenantMiddleware).

### Storage/gauge reporting

For gauge dimensions (storage size, seat count), record the **current value** periodically:

```python
# Cron job or background task: report current storage size
current_bytes = await get_storage_usage(account_id="acme_corp")
await engine.record(
    "storage",
    "acme_corp",
    dimensions={
        "total_bytes": current_bytes,
        "file_count": file_count,
    },
)
```

The aggregate's `value_last` captures the most recent value. `value_max` captures the peak.

## Querying Usage

### Single dimension

```python
result = await engine.query(
    "llm_usage",
    "acme_corp",
    "tokens_out",
    period="monthly",
    start=datetime(2026, 3, 1, tzinfo=UTC),
    end=datetime(2026, 4, 1, tzinfo=UTC),
)

print(f"Total tokens out: {result.value_sum:,.0f}")
print(f"Event count: {result.value_count}")
print(f"Peak single event: {result.value_max:,.0f}")
print(f"Average per event: {result.value_avg:,.0f}")
```

### Multiple dimensions

```python
report = await engine.query_multi(
    "llm_usage",
    "acme_corp",
    ["requests", "tokens_in", "tokens_out", "cost_units", "duration_ms"],
    period="monthly",
    start=start,
    end=end,
)

for name, agg in report.items():
    print(
        f"  {name}: sum={agg.value_sum:,.1f} {agg.unit} "
        f"(avg={agg.value_avg:,.1f}, max={agg.value_max:,.1f})"
    )
```

### AggregateResult fields

| Field            | Type            | Description                    |
| ---------------- | --------------- | ------------------------------ |
| `dimension_name` | `str`           | Dimension identifier           |
| `unit`           | `str`           | Unit label from dimension spec |
| `value_sum`      | `float`         | Sum of all values in period    |
| `value_count`    | `int`           | Number of events in period     |
| `value_min`      | `float \| None` | Minimum value                  |
| `value_max`      | `float \| None` | Maximum value                  |
| `value_last`     | `float \| None` | Most recent value              |
| `value_avg`      | `float`         | Computed: sum / count          |

### Hierarchical rollup (org → team → sub-account)

```python
# Register account hierarchy
await db.execute(
    "INSERT INTO hyper_meter_accounts (account_id, display_name, account_type, parent_account_id) "
    "VALUES ($1, $2, $3, $4)",
    "team-eng",
    "Engineering",
    "team",
    "acme_org",
)

# Query across entire hierarchy
org_total = await engine.query_hierarchy(
    "llm_usage", "acme_org", "tokens_out", "monthly", start, end
)
# Includes acme_org + all sub-accounts (team-eng, team-sales, etc.)
```

### Periods

| Period      | Bucket                     | Use case                              |
| ----------- | -------------------------- | ------------------------------------- |
| `"hourly"`  | `date_trunc('hour', ...)`  | Real-time dashboards, spike detection |
| `"daily"`   | `date_trunc('day', ...)`   | Daily reports, trend analysis         |
| `"monthly"` | `date_trunc('month', ...)` | Billing cycles, invoicing             |

## Quotas

Set usage limits per account, per dimension, per period:

```python
# Limit tokens_out to 10M per month, reject when exceeded
await engine.set_quota(
    "acme_corp",
    "llm_usage",
    "tokens_out",
    period="monthly",
    limit_value=10_000_000,
    action="reject",
)

# Warn at 1M requests per day
await engine.set_quota(
    "acme_corp",
    "api_usage",
    "requests",
    period="daily",
    limit_value=1_000_000,
    action="warn",
)
```

### Check quota manually

```python
decision = await engine.check_quota("acme_corp", "llm_usage", "tokens_out", "monthly")
print(f"Allowed: {decision.allowed}")
print(f"Remaining: {decision.remaining:,.0f}")
print(f"Limit: {decision.limit_value:,.0f}")
print(f"Action: {decision.action}")
```

### Automatic enforcement via middleware

```python
from hyperdjango.metering import MeteringMiddleware

app.use(
    MeteringMiddleware(
        engine=engine,
        meter_name="api_usage",
        dimension_extractor=my_extractor,
        quota_enforced=True,  # Returns 429 when quota exceeded
    )
)
```

## Hooks (Downstream Consumers)

Hooks are pluggable. The engine knows nothing about billing providers.

### Built-in hooks

```python
from hyperdjango.metering import QuotaEnforcementHook, AlertHook

# Auto-check quotas on every event
engine.register_hook(QuotaEnforcementHook(engine))


# Alert when tokens_out exceeds threshold in a single event
def alert_callback(ctx, dim_name, value, threshold):
    print(f"ALERT: {ctx.account_id} {dim_name}={value} > {threshold}")


engine.register_hook(
    AlertHook(
        thresholds={"tokens_out": 5_000_000},
        callback=alert_callback,
    )
)
```

### Custom billing hook (Stripe example)

```python
from hyperdjango.metering import MeterHook, PeriodExport


class StripeSyncHook(MeterHook):
    def __init__(self, stripe_client):
        self.stripe = stripe_client

    async def on_period_close(self, export: PeriodExport) -> None:
        for dim_name, result in export.dimensions.items():
            await self.stripe.billing.meter_events.create(
                event_name=f"{export.meter_name}_{dim_name}",
                payload={
                    "stripe_customer_id": lookup_stripe_id(export.account_id),
                    "value": str(int(result.value_sum)),
                },
            )


engine.register_hook(StripeSyncHook(stripe_client))
```

The metering engine exports structured `PeriodExport` data. Your hook adapts it to any provider.

### Export period data

```python
export = await engine.export_period(
    "llm_usage",
    "acme_corp",
    period_start=datetime(2026, 3, 1, tzinfo=UTC),
    period_end=datetime(2026, 4, 1, tzinfo=UTC),
)

for dim_name, result in export.dimensions.items():
    print(f"  {dim_name}: {result.value_sum:,.0f} {result.unit}")
```

## MeteringMiddleware

Auto-records multi-dimensional events per HTTP request:

```python
from hyperdjango.metering import MeteringMiddleware


def extract_llm_dims(request, response) -> dict[str, float]:
    """Extract dimensions from request/response."""
    return {
        "requests": 1,
        "tokens_in": int(response.headers.get("x-tokens-in", 0)),
        "tokens_out": int(response.headers.get("x-tokens-out", 0)),
        "bytes_transferred": len(response.body) if hasattr(response, "body") else 0,
    }


app.use(
    MeteringMiddleware(
        engine=engine,
        meter_name="llm_usage",
        account_resolver=lambda r: str(r.user.id) if r.user else None,
        dimension_extractor=extract_llm_dims,
        quota_enforced=True,
    )
)
```

### Default behavior

Without `dimension_extractor`, records `{"requests": 1}` per request.
Without `account_resolver`, extracts from `request.user.tenant_id` or `request.user.id`.

## Maintenance

### Rebuild aggregates

If aggregates get corrupted or schema changes, rebuild from raw events:

```python
count = await engine.reaggregate(
    "llm_usage",
    start=datetime(2026, 3, 1, tzinfo=UTC),
    end=datetime(2026, 4, 1, tzinfo=UTC),
)
print(f"Rebuilt {count} aggregate rows")
```

### Cleanup old data

```python
deleted = await engine.cleanup(
    retain_events_days=90,  # keep raw events for 90 days
    retain_aggregates_days=730,  # keep aggregates for 2 years
)
```

## Admin Integration

```python
from hyperdjango.metering import register_metering_admin

register_metering_admin(admin)
# Registers: Meter, MeterDimension, MeterAccount, MeterQuota, MeterAggregate
# With search, filter, readonly fields for audit trail
```

## Data Model

7 fully relational tables (no JSON blobs):

| Table                      | Purpose                    | Key columns                                                                           |
| -------------------------- | -------------------------- | ------------------------------------------------------------------------------------- |
| `hyper_meters`             | Meter definitions          | name, description, is_active                                                          |
| `hyper_meter_dimensions`   | Dimension specs per meter  | meter_id FK, name, type, unit, agg                                                    |
| `hyper_meter_events`       | Raw event log              | meter_id FK, account_id, timestamp                                                    |
| `hyper_meter_event_values` | Dimension values per event | event_id FK, dimension_id FK, value                                                   |
| `hyper_meter_aggregates`   | Pre-computed time buckets  | meter_id, dimension_id, account_id, bucket_size, bucket_start, sum/count/min/max/last |
| `hyper_meter_accounts`     | Account registry           | account_id, type, tier, parent_account_id                                             |
| `hyper_meter_quotas`       | Usage limits               | account_id, dimension_id, period, limit, action                                       |

All tables use proper FK relationships. Admin can CRUD all models with inline editing.

## API Reference

### MeterEngine

| Method                                                                             | Description                      |
| ---------------------------------------------------------------------------------- | -------------------------------- |
| `ensure_tables()`                                                                  | Create all 7 tables + indexes    |
| `define_meter(name, dimensions, description)`                                      | Define a meter with dimensions   |
| `record(meter_name, account_id, dimensions, tenant_id, idempotency_key)`           | Record a multi-dimensional event |
| `query(meter_name, account_id, dimension_name, period, start, end)`                | Query single dimension           |
| `query_multi(meter_name, account_id, dimension_names, period, start, end)`         | Query multiple dimensions        |
| `query_hierarchy(meter_name, root_account_id, dimension_name, period, start, end)` | Query across account hierarchy   |
| `check_quota(account_id, meter_name, dimension_name, period)`                      | Check quota                      |
| `set_quota(account_id, meter_name, dimension_name, period, limit, action)`         | Set quota                        |
| `export_period(meter_name, account_id, period_start, period_end)`                  | Export for billing               |
| `reaggregate(meter_name, start, end)`                                              | Rebuild aggregates from events   |
| `cleanup(retain_events_days, retain_aggregates_days)`                              | Delete old data                  |
| `register_hook(hook)`                                                              | Register downstream consumer     |

### MeterHook

| Method                             | Description                     |
| ---------------------------------- | ------------------------------- |
| `on_event(ctx)`                    | Called for every recorded event |
| `on_quota_exceeded(ctx, decision)` | Called when quota check fails   |
| `on_period_close(export)`          | Called when exporting a period  |
