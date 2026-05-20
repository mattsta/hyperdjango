# Async Patterns Guide

This guide covers async programming patterns in HyperDjango: async views,
async ORM queries, background tasks, WebSocket channels, async middleware,
and concurrent query patterns.

For related API references, see [tasks.md](tasks.md), [channels.md](channels.md),
and [database.md](database.md).

---

## Table of Contents

- [Async Views](#async-views)
- [Async ORM](#async-orm)
- [Background Tasks](#background-tasks)
- [WebSocket Channels](#websocket-channels)
- [Async Middleware](#async-middleware)
- [Concurrent Patterns](#concurrent-patterns)
- [Migration from Django Async](#migration-from-django-async)

---

## Async Views

All HyperDjango route handlers are async by default. Define them with
`async def`:

```python
from hyperdjango import HyperApp, Response

app = HyperApp(title="My App", database="postgres://localhost/mydb")


@app.get("/api/products")
async def list_products(request):
    products = await Product.objects.filter(active=True).order_by("name").all()
    return Response.json(
        [{"id": p.id, "name": p.name, "price": str(p.price)} for p in products]
    )


@app.get("/api/products/{id}")
async def get_product(request):
    product = await Product.objects.get(id=request.params["id"])
    if product is None:
        return Response.json({"error": "Not found"}, status=404)
    return Response.json(
        {
            "id": product.id,
            "name": product.name,
            "price": str(product.price),
            "stock": product.stock,
        }
    )


@app.post("/api/products")
async def create_product(request):
    data = await request.json()
    product = Product(
        name=data["name"],
        price=data["price"],
        stock=data.get("stock", 0),
    )
    await product.save()
    return Response.json({"id": product.id}, status=201)
```

The Zig HTTP server runs a 24-thread pool. Each request is dispatched to a
thread, and the async handler runs on that thread's event loop. Under
Python 3.14t (free-threaded), there is no GIL contention between threads.

### How `await db.query()` runs (loop role)

An ORM/DB call is a synchronous native PostgreSQL round-trip wrapped in an
`async def`. Where that round-trip actually runs is chosen automatically from
the _role_ of the event loop driving it — you never configure this per call:

- **Thread-per-request and single-flow loops** (the HTTP model above, plus
  tests, scripts, startup, and thread-mode WebSockets) run the round-trip
  **inline**. The loop is only driving that one request, so blocking it just
  waits for the query the request already needs — and keeping the query on the
  calling thread means it reuses that thread's _pinned_ pool connection. This
  is what keeps the connection budget equal to `THREAD_POOL_SIZE + headroom`.

- **Multiplexing loops** — the shared WebSocket event-loop pool (the default
  WebSocket model), and later the HTTP reactor — drive _many_ connections per
  loop. There, a blocking round-trip would stall every connection on the loop,
  so it is **offloaded** to a small, bounded DB executor and the loop stays
  responsive. Free-threaded Python runs the executor thread in true parallel.

The executor size is `DB_OFFLOAD_WORKERS` (0 = auto = `min(cpu, 8)`), and its
slots are folded into the pool connection budget so offload never
over-subscribes PostgreSQL. A pure-HTTP app never offloads, so the executor
and its connections are never created. Queries inside a `db.transaction()`
always run inline (on the thread that ran `BEGIN`, which owns the transaction's
connection) even on a multiplexing loop — a transaction is never split across
pooled connections.

---

## Async ORM

All database operations on models are async. The QuerySet API returns
awaitable objects:

```python
# Fetch all matching records
users = await User.objects.filter(active=True).all()

# Fetch a single record
user = await User.objects.get(id=42)

# Count
total = await User.objects.filter(active=True).count()

# Check existence
exists = await User.objects.filter(email="alice@example.com").exists()

# Create
user = User(name="Alice", email="alice@example.com")
await user.save()

# Update
user.name = "Alice Smith"
await user.save()

# Delete
await user.delete()

# Bulk operations
await Product.objects.filter(stock=0).update(active=False)
await Product.objects.filter(discontinued=True).delete()

# get_or_create
user, created = await User.objects.get_or_create(
    email="bob@example.com",
    defaults={"name": "Bob", "active": True},
)

# update_or_create
user, created = await User.objects.update_or_create(
    email="bob@example.com",
    defaults={"name": "Bob Updated"},
)

# Aggregation
from hyperdjango.expressions import Sum, Avg, Count

stats = await Order.objects.filter(status="completed").aggregate(
    total=Sum("amount"),
    average=Avg("amount"),
    count=Count("id"),
)

# Async iteration
async for product in Product.objects.filter(active=True).aiterator():
    await process_product(product)
```

### Transactions

Use `atomic()` for transaction blocks:

```python
from hyperdjango.database import atomic

async with atomic(app.database):
    order = Order(customer_id=user.id, total=cart.total, status="pending")
    await order.save()

    for item in cart.items:
        order_item = OrderItem(
            order_id=order.id,
            product_id=item.product_id,
            quantity=item.quantity,
            unit_price=item.price,
        )
        await order_item.save()

    await Product.objects.filter(id__in=[i.product_id for i in cart.items]).update(
        stock=F("stock") - 1
    )
    # If anything fails, the entire transaction rolls back
```

Nested transactions use savepoints:

```python
async with atomic(app.database):
    await order.save()

    async with atomic(app.database):  # Creates SAVEPOINT
        await risky_operation()
        # If this fails, only the savepoint rolls back
        # The outer transaction continues

    await finalize_order(order)
```

---

## Background Tasks

The `@app.task` decorator turns a function into a background task that can
be enqueued for deferred execution. The task queue is in-process using
Python's free-threading (3.14t) -- no external dependencies.

```python
@app.task
async def send_welcome_email(user_id, email):
    """Runs in a background worker thread."""
    user = await User.objects.get(id=user_id)
    await email_service.send(
        to=email,
        subject="Welcome!",
        body=f"Hello {user.name}, welcome to our platform.",
    )


@app.task
async def generate_report(report_id):
    """Long-running report generation."""
    report = await Report.objects.get(id=report_id)
    data = await fetch_report_data(report.params)
    pdf = await render_pdf(data)
    await storage.save(f"reports/{report_id}.pdf", pdf)
    report.status = "completed"
    report.file_path = f"reports/{report_id}.pdf"
    await report.save()


@app.post("/api/register")
async def register(request):
    data = await request.json()
    user = User(name=data["name"], email=data["email"])
    await user.save()

    # Enqueue background task -- returns immediately
    send_welcome_email.delay(user_id=user.id, email=user.email)

    return Response.json({"id": user.id}, status=201)


@app.post("/api/reports")
async def create_report(request):
    data = await request.json()
    report = Report(params=data, status="pending")
    await report.save()

    generate_report.delay(report_id=report.id)

    return Response.json({"id": report.id, "status": "pending"}, status=202)
```

### Task Queue Configuration

The task queue starts automatically with the app. Configure workers and
queue size:

```python
app = HyperApp(
    task_workers=8,  # Number of worker threads (default 4)
    task_queue_size=10000,  # Maximum pending tasks (default 10000)
)
```

### Task Stats

Monitor the task queue:

```python
@app.get("/api/admin/task-stats")
async def task_stats(request):
    stats = app.task_queue.stats
    return Response.json(
        {
            "pending": stats["pending"],
            "processed": stats["processed"],
            "failed": stats["failed"],
            "workers": stats["workers"],
            "running": stats["running"],
        }
    )
```

### Calling Tasks Directly

Tasks can also be called directly (synchronously) without the queue:

```python
# Background execution (via queue)
send_welcome_email.delay(user_id=1, email="alice@example.com")

# Direct execution (blocks until complete)
await send_welcome_email(user_id=1, email="alice@example.com")
```

---

## WebSocket Channels

HyperDjango provides a pub/sub channel system for real-time communication
over WebSockets.

### Channel Layer Setup

```python
from hyperdjango.channels import InMemoryChannelLayer, PgChannelLayer

# Single-process (development)
layer = InMemoryChannelLayer()

# Multi-process (production, cross-server broadcast via LISTEN/NOTIFY)
layer = PgChannelLayer(database_url="postgres://localhost/mydb")
await layer.connect()
```

### Subscribe and Publish

```python
channel = layer.channel("chat:room1")


# Subscribe to messages
def on_message(msg):
    print(f"Received: {msg}")


sub_id = channel.subscribe(on_message)

# Publish to all subscribers
await channel.publish({"type": "message", "text": "Hello, room!"})

# Unsubscribe
channel.unsubscribe(sub_id)
```

### Channel Groups (Fan-Out)

Broadcast to multiple channels at once:

```python
group = layer.group("notifications")
group.add("user:1")
group.add("user:2")
group.add("user:3")

# All three user channels receive this message
await group.publish({"type": "alert", "text": "System maintenance in 1 hour"})
```

### Presence Tracking

Track who is connected to a channel:

```python
channel = layer.channel("chat:room1")

# User joins
channel.join(user_id="user42", metadata={"name": "Alice"})

# List members
members = channel.presence()
# [{"user_id": "user42", "name": "Alice"}]

# User leaves
channel.leave(user_id="user42")
```

### Message History

Retrieve recent messages from a channel:

```python
channel = layer.channel("chat:room1")
recent = channel.history(limit=50)
# Returns the last 50 messages published to this channel
```

### WebSocket Handler Example

```python
@app.websocket("/ws/chat/{room}")
async def chat_handler(ws, request):
    room = request.params["room"]
    channel = layer.channel(f"chat:{room}")
    user = await get_user(request)

    # Join presence
    channel.join(user_id=str(user.id), metadata={"name": user.name})

    # Subscribe to room messages
    async def forward(msg):
        await ws.send_json(msg)

    sub_id = channel.subscribe(forward)

    try:
        async for msg in ws:
            data = msg.json()
            await channel.publish(
                {
                    "type": "message",
                    "user": user.name,
                    "text": data["text"],
                }
            )
    finally:
        channel.unsubscribe(sub_id)
        channel.leave(user_id=str(user.id))
```

---

## Async Middleware

Middleware can be async. The `__call__` method receives the request and a
`next_handler` coroutine:

```python
class TimingMiddleware:
    async def __call__(self, request, next_handler):
        from hyperdjango.profiling import nanos, elapsed_nanos

        start = nanos()
        response = await next_handler(request)
        elapsed = elapsed_nanos(start)

        response.headers["x-response-time"] = f"{elapsed / 1_000_000:.2f}ms"
        return response


class BearerAuthMiddleware:
    async def __call__(self, request, next_handler):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token:
            return Response.json({"error": "Unauthorized"}, status=401)

        user = await verify_token(token)
        if user is None:
            return Response.json({"error": "Invalid token"}, status=401)

        request.user = user
        return await next_handler(request)


app.use(TimingMiddleware())
app.use(BearerAuthMiddleware())
```

Middleware executes in the order registered. Each middleware can:

- Modify the request before passing it on
- Short-circuit the chain by returning a response directly
- Modify the response before returning it
- Perform async work (database queries, external API calls)

---

## Concurrent Patterns

### Parallel Queries with asyncio.gather

When you need data from multiple independent sources, fetch them concurrently:

```python
import asyncio


@app.get("/api/dashboard")
async def dashboard(request):
    user_id = request.user.id

    # Run 4 queries in parallel
    orders, notifications, stats, preferences = await asyncio.gather(
        Order.objects.filter(user_id=user_id).order_by("-created_at")[:5].all(),
        Notification.objects.filter(user_id=user_id, read=False).all(),
        Order.objects.filter(user_id=user_id).aggregate(
            total_spent=Sum("total"),
            order_count=Count("id"),
        ),
        UserPreferences.objects.get(user_id=user_id),
    )

    return Response.json(
        {
            "recent_orders": [o.to_dict() for o in orders],
            "unread_notifications": len(notifications),
            "total_spent": str(stats["total_spent"]),
            "order_count": stats["order_count"],
            "theme": preferences.theme,
        }
    )
```

This reduces the total wait time from `sum(query_times)` to
`max(query_times)`.

### DataLoader for N+1 Prevention

Use the DataLoader to batch and deduplicate async lookups:

```python
from hyperdjango.dataloader import DataLoader


async def batch_load_users(user_ids):
    """Load multiple users in a single query."""
    users = await User.objects.filter(id__in=user_ids).all()
    user_map = {u.id: u for u in users}
    return [user_map.get(uid) for uid in user_ids]


user_loader = DataLoader(batch_load_users)

# These three calls are batched into a single SQL query
user1 = await user_loader.load(1)
user2 = await user_loader.load(2)
user3 = await user_loader.load(3)

# Duplicate IDs are deduplicated
user1_again = await user_loader.load(1)  # Returns cached result, no extra query
```

### Connection Pipelining

For sequences of independent queries, pg.zig can pipeline them over a single
connection for 5.7x faster execution:

```python
# Sequential: 20 queries = 20 round trips (1.40ms)
for i in range(20):
    await db.query(f"SELECT $1::int", i)

# Pipelined: 20 queries = 1 round trip (0.24ms)
results = await db.pipeline([("SELECT $1::int", [i]) for i in range(20)])
```

---

## Migration from Django Async

| Django                                   | HyperDjango                               |
| ---------------------------------------- | ----------------------------------------- |
| `async def view(request):`               | Same                                      |
| `await Model.objects.aget(id=1)`         | `await Model.objects.get(id=1)`           |
| `await Model.objects.afilter(...).all()` | `await Model.objects.filter(...).all()`   |
| `await sync_to_async(func)()`            | Not needed (everything is natively async) |
| 3rd-party task broker / queue            | `@app.task` + `.delay()`                  |
| `django-channels`                        | Built-in `channels` module                |
| `ASGI` middleware                        | `async def __call__` middleware           |
| `asyncio.gather`                         | Same                                      |

Key differences:

- HyperDjango's ORM is async-native. There is no `aget()` / `afilter()` /
  `acreate()` prefix -- the standard methods are already async.
- Background tasks use an in-process thread pool queue, not an external
  broker. No external dependencies.
- The WebSocket channel layer uses PostgreSQL `LISTEN/NOTIFY` for cross-server
  communication via PostgreSQL LISTEN/NOTIFY.
- Under Python 3.14t, all threads run truly concurrently (no GIL), so
  CPU-bound work in middleware or tasks does not block the event loop.
