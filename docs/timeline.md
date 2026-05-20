# Status Timeline

Time-series event tracking for entity statuses. Replaces boolean flags (`is_banned`, `is_muted`, `is_staff`, `is_active`) with a temporal event model where every status change is recorded with start/end times, actor attribution, reasons, and full queryable history.

## Why Timeline?

Boolean flags are point-in-time snapshots. When `is_banned = True`:

- When was the user banned? Unknown.
- Who banned them? Unknown.
- Why? Unknown.
- Should it auto-expire? Can't express.
- Was there a warning first? No history.

Timeline events record all of this:

```python
await user.set_status(
    "moderation",
    "banned",
    reason="Repeated spam violations",
    actor_id=admin.id,
    expires_in=timedelta(days=30),
    detail={"violation_count": 5, "last_post_id": 789},
)
```

## Quick Start

```python
from hyperdjango.timeline import StatusTimelineMixin, get_timeline
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field


class User(StatusTimelineMixin, TimestampMixin):
    class Meta:
        table = "users"

    class TimelineConfig:
        entity_type = "user"
        categories = {
            "moderation": ["banned", "muted", "warned"],
            "access": ["staff", "moderator", "verified"],
        }

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    # No is_banned, is_muted, is_staff fields needed!
```

### Set a Status

```python
await user.set_status(
    "moderation",
    "banned",
    reason="Spam",
    actor_id=admin.id,
    expires_in=timedelta(days=30),
)
```

### Check a Status

```python
if await user.has_status("moderation", "banned"):
    raise HTTPException(403, "Account banned")
```

### Get Current Status

```python
status = await user.get_status("moderation")
if status:
    print(f"Status: {status.status} since {status.since}")
    print(f"Reason: {status.reason}")
    print(f"Banned by user #{status.actor_id}")
    print(f"Expires: {status.expires_at}")
```

### Clear a Status

```python
await user.clear_status("moderation", reason="Appeal approved", actor_id=admin.id)
```

### View Full History

```python
history = await user.get_status_history("moderation")
for record in history:
    print(f"{record.status} from {record.since} to {record.ended_at}")
    print(f"  Reason: {record.reason}")
    print(f"  By: user #{record.actor_id}")
```

## HyperGuard Integration

Timeline statuses integrate with the guard system for route-level enforcement:

```python
from hyperdjango.guard import Require, guard


@guard(
    Require.authenticated(redirect_url="/login"),
    Require.not_banned(),  # Timeline: no active "moderation/banned" status
    Require.not_muted(),  # Timeline: no active "moderation/muted" status
)
async def create_post(
    request,
): ...  # Only runs if user is authenticated, not banned, not muted


@guard(
    Require.authenticated(),
    Require.has_active_status("access", "staff"),
)
async def admin_panel(request): ...  # Only runs if user has active staff status
```

Custom status guards:

```python
@guard(
    Require.authenticated(),
    Require.no_active_status("moderation", "probation"),
)
async def sensitive_action(request): ...
```

## How Activeness Works

A status is **active** when:

- `ended_at IS NULL` (not explicitly ended)
- AND `expires_at IS NULL OR expires_at > now()` (not expired)

No `is_active` or `is_current` flags — activeness is derived from the data at query time. This eliminates stale flag bugs entirely.

When a new status is set in a category, the previous status in that category is automatically ended (sets `ended_at = now()`).

## Categories

Each model defines its own categories and valid statuses:

```python
class TimelineConfig:
    entity_type = "user"
    categories = {
        "moderation": ["banned", "muted", "warned", "probation"],
        "access": ["staff", "moderator", "verified"],
        "tier": ["free", "pro", "enterprise"],
    }
```

- An entity can have **one active status per category** at a time
- Different categories are independent (a user can be "warned" AND "staff" simultaneously)
- Setting a new status in a category automatically ends the previous one
- Invalid categories or statuses raise `ValueError` at call time

## StatusRecord

The `StatusRecord` dataclass is returned by all query methods:

| Field         | Type             | Description                                        |
| ------------- | ---------------- | -------------------------------------------------- |
| `status`      | str              | The status name ("banned", "staff", etc.)          |
| `category`    | str              | The category ("moderation", "access", etc.)        |
| `since`       | datetime         | When this status was set                           |
| `expires_at`  | datetime or None | When it auto-expires (None = indefinite)           |
| `ended_at`    | datetime or None | When it was explicitly ended (None = still active) |
| `reason`      | str              | Why it was set                                     |
| `end_reason`  | str              | Why it was ended                                   |
| `actor_id`    | int or None      | Who set it                                         |
| `ended_by`    | int or None      | Who ended it                                       |
| `detail`      | dict             | Arbitrary metadata (JSONB)                         |
| `event_id`    | int              | Database row ID                                    |
| `entity_type` | str              | Entity type ("user", "ticket", etc.)               |
| `entity_id`   | int              | Entity primary key                                 |

## TimelineManager (Direct API)

For advanced use cases without the mixin:

```python
from hyperdjango.timeline import get_timeline

tl = get_timeline()

# Add event
await tl.add_event(
    "user",
    42,
    "moderation",
    "banned",
    reason="Spam",
    actor_id=1,
    expires_in=timedelta(days=30),
)

# Check status
status = await tl.current_status("user", 42, "moderation")
is_banned = await tl.is_active("user", 42, "banned")

# End status
await tl.end_status("user", 42, "moderation", ended_by=1, end_reason="Appeal approved")

# Get all banned users
banned_ids = await tl.get_entities("user", "banned")

# History — newest first, ordered by (started_at DESC, id DESC) so events
# written in the same timestamp tick keep their insertion order
history = await tl.get_history("user", 42, "moderation", limit=50)

# Housekeeping (optional)
expired = await tl.expire_overdue()  # Mark past-due events as ended
deleted = await tl.cleanup(days=90)  # Delete old ended events
```

## Signals

```python
from hyperdjango.timeline import status_changed


@status_changed.connect
async def on_status_change(
    sender,
    entity_type,
    entity_id,
    category,
    old_status,
    new_status,
    actor_id,
    reason,
    **kwargs,
):
    if new_status == "banned":
        await send_ban_notification(entity_id, reason)
```

## Escalation Rules

The `EscalationEngine` auto-triggers actions when event patterns are detected. Define rules with `EscalationRule` and the engine evaluates them on each `set_status` call.

```python
from hyperdjango.timeline import EscalationEngine, EscalationRule

engine = EscalationEngine()

# 3 warnings in any window → auto-mute
engine.add_rule(
    EscalationRule(
        source_category="moderation",
        source_status="warned",
        threshold=3,
        target_category="moderation",
        target_status="muted",
        reason="Auto-muted: 3 warnings",
    )
)

# 2 mutes in 30 days → auto-ban
engine.add_rule(
    EscalationRule(
        source_category="moderation",
        source_status="muted",
        threshold=2,
        window=timedelta(days=30),
        target_category="moderation",
        target_status="banned",
        reason="Auto-banned: 2 mutes in 30 days",
    )
)

# Cross-category: 3 spam flags → auto-restrict posting
engine.add_rule(
    EscalationRule(
        source_category="content",
        source_status="spam_flagged",
        threshold=3,
        target_category="access",
        target_status="restricted",
        reason="Auto-restricted: repeated spam flags",
    )
)
```

The engine checks history counts after each `set_status` and fires the target action automatically when the threshold is met within the optional window.

## Admin Integration

`register_timeline_admin()` registers timeline event views in HyperAdmin. `make_timeline_actions()` generates admin actions for setting/clearing statuses.

```python
from hyperdjango.timeline import register_timeline_admin, make_timeline_actions

# Register timeline event browsing in admin
register_timeline_admin(admin)

# Generate bulk actions for a model's timeline categories
actions = make_timeline_actions(User)
# Returns actions like "Ban User", "Mute User", "Clear Moderation Status"
```

## Batch Queries

`active_statuses()` checks multiple statuses in a single query, returning a dict of category to active `StatusRecord`:

```python
statuses = await user.active_statuses()
# {"moderation": StatusRecord(status="warned", ...), "access": StatusRecord(status="staff", ...)}

if "moderation" in statuses and statuses["moderation"].status == "banned":
    raise HTTPException(403, "Banned")
```

This is especially useful when checking multiple categories at once instead of issuing separate queries.

## Guard Caching

Guard chains automatically cache timeline lookups per-request. When multiple guards check timeline statuses (e.g., `Require.not_banned()` + `Require.not_muted()` + `Require.has_active_status("access", "staff")`), the first guard calls `active_statuses()` and caches the result on the request object. Subsequent guards in the same chain read from cache. This means N timeline guards cost 1 query instead of N.

```python
@guard(
    Require.authenticated(),
    Require.not_banned(),  # Triggers active_statuses() query, caches result
    Require.not_muted(),  # Reads from cache — no query
    Require.has_active_status("access", "staff"),  # Reads from cache — no query
)
async def admin_action(request): ...  # 1 timeline query total, not 3
```

## Performance

- `current_status()` / `is_active()`: 1 indexed query per call
- `add_event()`: 3-4 queries in a transaction (SELECT FOR UPDATE + end old + INSERT)
- `get_entities()`: 1 query with DISTINCT and expires_at filter in SQL
- `get_history()`: 1 query with SQL LIMIT
- Guard checks: 1 query per request (cached via `active_statuses()`) instead of 1 per guard
- `active_statuses()`: 1 query returns all active statuses across all categories

## Setup

The `hyper_status_events` table is created automatically by `hyper setup` (StatusEvent is a registered Model). Compound indexes are created via `get_timeline().ensure_indexes()` in your app's startup hook:

```python
@app.on_startup
async def _startup():
    await get_timeline().ensure_indexes()
```
