# HyperGuard — Declarative Route Protection

HyperGuard replaces scattered manual permission checks with a declarative requirement chain compiled at route registration time. Each route declares what it needs — authentication, resource access, custom checks — and the guard evaluates the chain per-request, short-circuiting on first failure.

## Quick Start

```python
from hyperdjango.guard import guard, Require


@app.post("/f/{forum_name}/submit")
@guard(
    Require.authenticated(redirect_url="/login"),
    Require.not_banned(),
    Require.resource("forum", resolver=resolve_forum_write, from_path="forum_name"),
)
async def forum_submit(request, forum_name: str):
    forum = request.guard.forum  # Resolved and validated by guard
    ...
```

## Requirements

Requirements are created via `Require.*` factory methods. They are frozen dataclasses — immutable after creation.

### Preconditions

Check request state from the user dict. No DB queries.

```python
Require.authenticated()  # User dict with 'id' key
Require.authenticated(redirect_url="/login")  # Redirect instead of 401
Require.staff()  # is_staff=True (checks groups, user dict, timeline)
Require.superuser()  # is_superuser=True
Require.api_key()  # Valid API key (APIKeyAuth middleware)

# RBAC group membership (type-safe: rejects string/dict/int groups)
Require.group("editors")  # User in "editors" group
Require.group("admin")  # User in "admin" group
```

### Timeline Status Checks

Query the StatusTimeline table for temporal access control.

```python
Require.not_banned()  # No active "banned" status
Require.not_muted()  # No active "muted" status
Require.has_active_status("access", "staff")  # Active timeline status required
Require.no_active_status("moderation", "banned")  # Deny if status is active
```

**Availability semantics** — the timeline lookup distinguishes three states:

- **Timeline answered** — the query result is authoritative.
- **Unavailable** (no `DATABASE_URL` configured, or the `hyper_status_events`
  table isn't provisioned) — benign: an unprovisioned timeline holds no
  status data, so checks fall back to session flags (`is_banned`,
  `is_muted`, or the requirement's `fallback_flag`).
- **Error** (connection failure, any other DB error) — fail closed: ban/mute
  and status checks deny with 403 "Account status check unavailable" rather
  than let a banned user through during an outage.

### Resource Resolvers

Resolve a named resource from path parameters + async lookup, store in guard context.

```python
Require.resource(
    "forum",  # Key name (accessible as request.guard.forum)
    resolver=my_forum_resolver,  # async (request, ctx, path_val) -> resource | None
    from_path="forum_name",  # Extract this path parameter
    deny_message="Forum not found",  # Custom 404 message
)
```

**Resolver signature:**

```python
async def my_forum_resolver(
    request, ctx: GuardContext, forum_name: str
) -> Forum | None:
    forum = await Forum.objects.filter(name=forum_name).first()
    if not forum:
        return None  # -> 404
    if forum.is_archived:
        raise HTTPException(403, "Forum is archived")  # Custom error
    return forum  # Stored in ctx.resources["forum"]
```

Resolvers can access previously-resolved resources via `ctx.resources`:

```python
async def resolve_post(request, ctx, pid):
    forum = ctx.resources["forum"]  # Resolved by earlier requirement
    post = await Post.objects.filter(id=int(pid), forum_id=forum.id).first()
    return post
```

### Custom Checks

Full control over evaluation logic:

```python
async def check_karma(request, ctx):
    uid = request.user.get("id", 0) if isinstance(request.user, dict) else 0
    if uid:
        user = await User.objects.filter(id=uid).first()
        if user and user.karma < 50:
            return GuardDenial(DenyReason.FORBIDDEN, "Need 50+ karma to post")
    return None  # Pass


Require.check("karma_check", fn=check_karma)
```

### OR Composition

Pass if ANY requirement passes:

```python
Require.any_of(
    Require.staff(),
    Require.check("is_mod", fn=check_is_mod),
)
```

## Guard Context

After all requirements pass, `request.guard` is a `GuardContext` with:

- `request.guard.forum` — attribute access to resolved resources
- `request.guard.resources["forum"]` — dict access
- `request.guard.metadata` — arbitrary metadata set by requirements

## Error Handling

| Requirement failure              | Status        | Message                       |
| -------------------------------- | ------------- | ----------------------------- |
| Not authenticated                | 401           | "Authentication required"     |
| Not authenticated + redirect_url | 302 redirect  | —                             |
| Not staff                        | 403           | "Staff access required"       |
| Not superuser                    | 403           | "Superuser access required"   |
| Banned                           | 403           | "Your account is suspended"   |
| Muted                            | 403           | "Your account has been muted" |
| Invalid API key                  | 401           | "Valid API key required"      |
| Resource not found               | 404           | Custom or "X not found"       |
| Resolver raises HTTPException    | Passthrough   | Resolver's message            |
| Custom denial                    | Custom status | Custom message                |

All denials are logged: `[GUARD] DENIED: POST /path user=alice(1) requirement=not_banned reason=forbidden status=403`

## Intent-Based Resolver Pattern

For apps with rich access control (like HyperNews), create one resolver per intent:

```python
def make_forum_resolver(intent: ForumIntent):
    async def resolver(request, ctx, forum_name):
        forum = await Forum.objects.filter(name=forum_name).first()
        if not forum:
            return None
        # ... intent-specific checks (archived, locked, private, mod, admin)
        return ForumAccess(forum, is_member, is_mod, membership)
    return resolver

resolve_read = make_forum_resolver(ForumIntent.READ)
resolve_write = make_forum_resolver(ForumIntent.WRITE_POST)
resolve_moderate = make_forum_resolver(ForumIntent.MODERATE)
resolve_admin = make_forum_resolver(ForumIntent.ADMIN)

# Routes declare intent via which resolver they use:
@guard(Require.authenticated(), Require.resource("access", resolver=resolve_read, from_path="name"))
@guard(Require.authenticated(), Require.resource("access", resolver=resolve_write, from_path="name"))
@guard(Require.authenticated(), Require.resource("access", resolver=resolve_admin, from_path="name"))
```

## Route Scanning

At startup, scan for unguarded routes:

```python
from hyperdjango.guard.scanner import scan_routes, log_guard_summary

result = scan_routes(app)
log_guard_summary(result)
# [GUARD] 35 guarded routes, 5 unguarded (88% coverage)
# [GUARD] WARNING: POST /webhook (webhook_handler) has no guard
```

## Architecture

```
Require.*() factories  →  GuardRequirement (frozen)
                            ↓
@guard(req1, req2, ...)  →  GuardSpec (frozen, tuple of requirements)
                            ↓ (per request)
evaluate_guard(request)  →  GuardContext (resources + metadata)
                            ↓
request.guard            →  Access resolved resources in handler
```

Requirements are compiled once at decoration time. Per-request evaluation is a simple loop with short-circuit — no parsing, no allocation beyond the GuardContext.

## Policy Bridge: Require.policy()

Connect `@guard()` decorator to `.guard` policy files:

```python
from hyperdjango.guard import Require, PolicyRegistry

registry = PolicyRegistry()
registry.load_directory("policies/")


@guard(
    Require.authenticated(),
    Require.resource("forum", resolver=resolve_forum, from_path="name"),
    Require.policy(
        "Forum.write_post",
        registry=registry,
        resource_dict_fn=lambda r, ctx: {
            "is_archived": ctx.forum.is_archived,
            "is_locked": ctx.forum.is_locked,
        },
    ),
)
async def forum_submit(request, name: str):
    forum = request.guard.forum
```

For user-only policies (no resource fields), omit `resource_dict_fn`:

```python
Require.policy("Admin.access", registry=registry)  # checks user.is_staff
```

## SQL Generation: guard_filter()

Apply policy rules as SQL WHERE clauses on listing queries:

```python
posts = (
    await Post.objects.guard_filter(request.user, "read", registry=registry)
    .order_by("-created_at")
    .limit(20)
    .all()
)
# Generates: WHERE "is_public" = $1 AND NOT ("is_deleted" = $2)
```

Multiple allow rules are OR-joined. Deny rules become `NOT(...)`. Deny rules requiring Python (relation checks) cause the filter to return empty results (safe default).

## ViewSet Actions: @guard_action()

Protect REST ViewSet `@action` methods with declarative guards:

```python
from hyperdjango.guard import guard_action, Require
from hyperdjango.rest import action, ModelViewSet


class BookViewSet(ModelViewSet):
    @action(methods=["POST"], detail=True, url_path="publish")
    @guard_action(Require.authenticated(), Require.staff())
    async def publish(self, request, **kwargs):
        instance = await self.get_object()
        ...
```

`@action()` must be outermost (ViewSet dispatch needs its metadata). `@guard_action()` evaluates after ViewSet-level `permission_classes` but before the handler.

## WebSocket: @guard_websocket()

Protect WebSocket handlers with session-based authentication:

```python
from hyperdjango.guard import guard_websocket, Require


@app.websocket("/ws/chat")
@guard_websocket(auth, Require.authenticated())
async def chat(ws):
    user = ws.user  # Authenticated user dict
    guard = ws.guard  # GuardContext
```

On denial: accepts the WebSocket (ASGI requirement), sends error JSON, closes with code 4001 (auth), 4003 (forbidden), 4004 (not found), or 4029 (rate limited).

## Integrated Apps

HyperGuard `@guard()` is live in 8 services:

| App             | Routes        | Auth Pattern                                 |
| --------------- | ------------- | -------------------------------------------- |
| semantic_search | 4             | Session auth + API auth                      |
| rest_api        | 6             | Session auth + API key                       |
| content_hub     | 2             | Session auth + inline role checks            |
| hyperai         | 9             | Session auth with redirect                   |
| websocket_chat  | 3 HTTP + 1 WS | `@guard_websocket` for WebSocket auth        |
| multi_tenant    | 7             | Session auth + API key + tenant middleware   |
| bookstore_api   | 3             | `@guard_action` on ViewSet actions + API key |
| hypernews       | ~70           | `@guard` + intent-based forum/post resolvers |

## RBAC Group + Timeline Integration

### Group-Based Access

```python
# Require user belongs to a specific RBAC group
@guard(Require.authenticated(), Require.group("editors"))
async def edit_article(request): ...


# Type safety: groups must be a list. String/dict/int groups are rejected
# to prevent substring-match attacks ("staff" in "staffing" = True).
```

### Timeline-Based Access

Use `StatusTimelineMixin` for temporal access control instead of boolean flags:

```python
# Model with timeline categories
class User(StatusTimelineMixin, TimestampMixin, Model):
    class TimelineConfig:
        entity_type = "user"
        categories = {
            "moderation": ["banned", "muted", "warned"],
            "access": ["staff", "moderator"],
        }


# Guard checks timeline status (no boolean flags needed)
@guard(
    Require.authenticated(),
    Require.no_active_status("moderation", "banned"),  # Not currently banned
    Require.has_active_status("access", "staff"),  # Currently staff
)
async def admin_panel(request): ...


# Set status with actor attribution + optional expiry
await user.set_status(
    "moderation",
    "banned",
    reason="Repeated spam",
    actor_id=admin.id,
    expires_in=timedelta(days=30),
)

# Clear status
await user.clear_status("moderation", reason="Appeal approved", actor_id=admin.id)

# Query history
history = await user.get_status_history("moderation")
```

### Batch Optimization

Guard chains with multiple timeline lookups are batched via `active_statuses()`:

```python
# 1 query instead of N per-guard timeline checks
@guard(
    Require.authenticated(),
    Require.no_active_status("moderation", "banned"),
    Require.no_active_status("moderation", "muted"),
    Require.has_active_status("access", "staff"),
)
```
