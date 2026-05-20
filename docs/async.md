# Async Support

HyperDjango is async-first. All views, database operations, and middleware use `async`/`await`. The native Zig HTTP server is ASGI-native with zero sync-to-async overhead.

There is no sync mode. Every database call, every view handler, every middleware function is an async coroutine. This eliminates the impedance mismatch that plagues frameworks where async is bolted on -- in HyperDjango, there is one execution model.

## Async Views

All views are async by default:

```python
@app.get("/users")
async def list_users(request):
    users = await User.objects.all()  # async database query
    return {"users": [u.name for u in users]}
```

Every route handler must be declared `async def`. The framework will raise an error at registration time if you try to register a synchronous function as a view.

```python
@app.get("/users/{id:int}")
async def get_user(request, id: int):
    user = await User.objects.get(id=id)
    return {"id": user.id, "name": user.name, "email": user.email}


@app.post("/users")
async def create_user(request):
    data = await request.json()
    user = User(name=data["name"], email=data["email"])
    await user.save()
    return Response.json({"id": user.id}, status=201)
```

## Async Database Operations

Every database method is async. This applies to the ORM QuerySet, raw SQL, and transaction management:

### QuerySet methods

```python
# Fetch operations
users = await User.objects.filter(active=True).all()
user = await User.objects.get(id=1)
first = await User.objects.filter(name="Alice").first()
exists = await User.objects.filter(email="a@b.com").exists()

# Aggregation
count = await User.objects.count()
total = await Order.objects.aggregate(Sum("amount"))

# Create/update/delete
user = User(name="Alice")
await user.save()
user.name = "Bob"
await user.save()
await user.delete()

# Bulk operations
await User.objects.filter(active=False).update(active=True)
await User.objects.filter(last_login__lt=cutoff).delete()
```

### Raw SQL

```python
# Query returns list of row tuples
rows = await db.query("SELECT * FROM users WHERE id = $1", 1)

# query_one returns a single row or None
row = await db.query_one("SELECT COUNT(*) as c FROM users")

# Execute returns affected row count
affected = await db.execute("UPDATE users SET active = $1 WHERE id = $2", True, 1)
```

### Transactions

```python
async with db.transaction():
    await do_work()
    # COMMIT happens automatically when the block exits
    # ROLLBACK happens automatically on exception
```

Transactions support nesting via savepoints:

```python
async with db.transaction():  # BEGIN
    await create_order()
    async with db.transaction():  # SAVEPOINT sp_1
        await update_inventory()
        # If this raises, only the inner savepoint rolls back
    await send_confirmation()  # Still runs if inner failed
# COMMIT
```

## Concurrent Operations

Use `asyncio.gather()` for independent operations that can run at the same time. This is one of the biggest performance wins in async code -- instead of waiting for each query sequentially, you fire them all at once:

```python
import asyncio


@app.get("/dashboard")
async def dashboard(request):
    # Run 3 queries concurrently — much faster than sequential
    users, orders, stats = await asyncio.gather(
        User.objects.filter(active=True).all(),
        Order.objects.filter(status="pending").all(),
        db.query_one("SELECT COUNT(*) as c, SUM(amount) as s FROM orders"),
    )
    return {
        "active_users": len(users),
        "pending_orders": len(orders),
        "total_revenue": stats["s"],
    }
```

### When to use gather

Use `asyncio.gather()` when:

- You have multiple independent queries with no data dependencies between them
- Each query takes non-trivial time (10ms+)
- The results are all needed before returning a response

Do not use `asyncio.gather()` when:

- One query depends on the result of another
- You are inside a transaction (queries should be sequential within a transaction)
- You only have one or two trivially fast queries

### Concurrent with error handling

```python
@app.get("/report")
async def report(request):
    results = await asyncio.gather(
        fetch_sales_data(),
        fetch_user_metrics(),
        fetch_inventory(),
        return_exceptions=True,  # Don't fail if one query errors
    )

    sales, metrics, inventory = results
    response = {}
    if not isinstance(sales, Exception):
        response["sales"] = sales
    if not isinstance(metrics, Exception):
        response["metrics"] = metrics
    if not isinstance(inventory, Exception):
        response["inventory"] = inventory

    return response
```

## Async Middleware

Middleware is async. Each middleware receives the request and a `call_next` coroutine to invoke the next middleware (or the view):

```python
async def timing_middleware(request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Response-Time"] = f"{elapsed * 1000:.1f}ms"
    return response


app.use(timing_middleware)
```

### Middleware ordering

Middleware runs in registration order for the request phase and reverse order for the response phase:

```python
app.use(auth_middleware)  # 1st on request, 3rd on response
app.use(logging_middleware)  # 2nd on request, 2nd on response
app.use(timing_middleware)  # 3rd on request, 1st on response
```

### Short-circuit middleware

Middleware can return a response without calling `call_next` to short-circuit the chain:

```python
async def auth_middleware(request, call_next):
    if not request.user.is_authenticated:
        return Response.json({"error": "Unauthorized"}, status=401)
    return await call_next(request)
```

## Async Signals

Signal receivers can be async. When a signal is sent, async receivers are awaited and sync receivers are called normally:

```python
from hyperdjango.signals import post_save, receiver


@receiver(post_save, sender=Article)
async def on_article_save(sender, instance, created, **kwargs):
    if created:
        await notify_subscribers(instance)
```

Multiple receivers on the same signal all run. Async receivers are awaited in registration order:

```python
@receiver(post_save, sender=Order)
async def update_inventory(sender, instance, **kwargs):
    await Inventory.objects.filter(product_id=instance.product_id).update(
        quantity=F("quantity") - instance.quantity
    )


@receiver(post_save, sender=Order)
async def send_confirmation(sender, instance, created, **kwargs):
    if created:
        await send_order_email(instance)
```

## Async Context Managers

Database transactions and other resources use async context managers:

```python
async with db.transaction():
    await create_order()
    await update_inventory()
    # Auto-commit on success, auto-rollback on exception
```

### Nested transactions with savepoints

```python
async with db.transaction():  # BEGIN
    user = User(name="Alice")
    await user.save()

    async with db.transaction():  # SAVEPOINT sp_1
        profile = Profile(user_id=user.id)
        await profile.save()

    async with db.transaction():  # SAVEPOINT sp_2
        await send_welcome_email(user)
        # If email fails, only sp_2 rolls back
        # user and profile are preserved
# COMMIT
```

## WebSocket (Async Streaming)

WebSocket handlers are async generators that receive and send messages:

```python
@app.websocket("/ws/chat")
async def chat(ws):
    await ws.accept()
    async for message in ws:
        await ws.send_text(f"Echo: {message}")
```

### WebSocket with channels

```python
from hyperdjango.channels import ChannelGroup

chat_room = ChannelGroup("chat")


@app.websocket("/ws/chat/{room}")
async def chat_handler(ws, room: str):
    await ws.accept()
    channel = await chat_room.subscribe(room)

    async for message in ws:
        await chat_room.publish(
            room,
            {
                "user": ws.user.name,
                "text": message,
            },
        )
```

### Binary WebSocket

```python
@app.websocket("/ws/binary")
async def binary_handler(ws):
    await ws.accept()
    async for data in ws:
        # data is bytes when client sends binary frames
        processed = process_binary(data)
        await ws.send_bytes(processed)
```

### WebSocket Extensions

HyperDjango WebSocket connections expose subprotocol negotiation, extension awareness, and server-level configuration for keepalive and message size limits.

#### WebSocketConfig

`WebSocketConfig` controls server-wide WebSocket behavior. It is a frozen dataclass with three fields:

```python
from hyperdjango.websocket import WebSocketConfig

config = WebSocketConfig(
    max_message_size=16 * 1024 * 1024,  # 16 MB (default)
    ping_interval=30,  # seconds, 0 = disabled (default: 30)
    pong_timeout=120,  # seconds (default: 120)
)
```

- `max_message_size` -- maximum bytes per WebSocket message. Enforced in the Zig frame parser. If a client sends a message exceeding this limit, the server closes the connection with close code 1009 (Message Too Big).
- `ping_interval` -- how often the server sends a ping frame to the client, in seconds. Set to 0 to disable server-initiated pings.
- `pong_timeout` -- how long the server waits for a pong response before considering the connection dead. The default of 120 seconds is deliberately generous to accommodate mobile clients, wireless networks, and laptops waking from sleep.

Push the configuration to the native Zig server with `apply()`:

```python
config = WebSocketConfig(max_message_size=4 * 1024 * 1024, ping_interval=15)
config.apply()
```

Read the current configuration back from the native server with `current()`:

```python
live = WebSocketConfig.current()
print(live.max_message_size)  # 4194304
print(live.ping_interval)  # 15
print(live.pong_timeout)  # 120
```

#### Subprotocol Negotiation

WebSocket clients can request one or more subprotocols via the `Sec-WebSocket-Protocol` header. The server inspects the client's list and selects one during `accept()`.

```python
@app.websocket("/ws/graphql")
async def graphql_ws(ws):
    # Client sent Sec-WebSocket-Protocol: graphql-ws, graphql-transport-ws
    print(ws.requested_subprotocols)
    # ['graphql-ws', 'graphql-transport-ws']

    # Select one subprotocol
    await ws.accept(subprotocol="graphql-ws")

    # Confirm what was selected
    print(ws.accepted_subprotocol)  # 'graphql-ws'

    async for message in ws.iter_json():
        await handle_graphql(ws, message)
```

- `ws.requested_subprotocols` -- list of subprotocols the client offered. Parsed from the ASGI scope or from the `Sec-WebSocket-Protocol` header.
- `ws.accept(subprotocol="...")` -- accept the connection and select one subprotocol. The selected value should be one of `ws.requested_subprotocols`.
- `ws.accepted_subprotocol` -- the subprotocol that was selected during `accept()`, or `None` if no subprotocol was negotiated.

#### Extension Awareness

The `ws.extensions` dict exposes negotiated WebSocket extensions (populated from the ASGI scope). The most common extension is `permessage-deflate` for per-message compression:

```python
@app.websocket("/ws/data")
async def data_handler(ws):
    await ws.accept()

    # Check if compression was negotiated
    if ws.has_compression:
        print("permessage-deflate active — frames are compressed")

    # Full extensions dict
    print(ws.extensions)
    # {'permessage-deflate': {'client_max_window_bits': '15'}}
```

- `ws.has_compression` -- `True` if `permessage-deflate` was negotiated between client and server.
- `ws.extensions` -- dict of all negotiated extensions and their parameters.

#### Ping/Pong Keepalive

The Zig server sends ping frames at the configured `ping_interval` (default 30 seconds). If the client does not respond with a pong within `pong_timeout` (default 120 seconds), the connection is closed.

The generous default pong timeout exists because real-world clients -- mobile devices on cellular networks, laptops on WiFi, browsers backgrounded by the OS -- can experience long delays before responding to pings. A tight timeout causes false disconnections. The 120-second default handles these cases without requiring application code to manage reconnection.

To disable server-initiated pings entirely:

```python
WebSocketConfig(ping_interval=0).apply()
```

#### Max Message Size

The `max_message_size` limit is enforced in the Zig frame parser, not in Python. When a client sends a message that exceeds the limit, the server immediately closes the connection with close code 1009 (Message Too Big) before the full message is assembled in memory. This prevents memory exhaustion from oversized payloads.

```python
# Restrict to 1 MB for a resource-constrained endpoint
WebSocketConfig(max_message_size=1 * 1024 * 1024).apply()
```

## Server-Sent Events

SSE provides one-way server-to-client streaming over HTTP. The client uses `EventSource` in JavaScript, and the server yields events from an async generator:

```python
@app.get("/events")
async def events(request):
    async def generate():
        while True:
            data = await get_latest_event()
            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(1)

    return Response.sse(generate())
```

### SSE with event types and IDs

```python
@app.get("/events/orders")
async def order_events(request):
    async def generate():
        last_id = 0
        while True:
            orders = await Order.objects.filter(id__gt=last_id).all()
            for order in orders:
                yield f"id: {order.id}\nevent: new_order\ndata: {json.dumps({'id': order.id, 'total': order.total})}\n\n"
                last_id = order.id
            await asyncio.sleep(0.5)

    return Response.sse(generate())
```

## Background Tasks

Background tasks run after the response is sent. They are declared with the `@task` decorator and enqueued with `.delay()`:

```python
from hyperdjango.tasks import task


@task
async def send_welcome_email(user_id: int):
    user = await User.objects.get(id=user_id)
    await send_mail(to=[user.email], subject="Welcome!")


# Enqueue — returns immediately, task runs in background
await send_welcome_email.delay(user_id=42)
```

### Task from a view

```python
@app.post("/users")
async def create_user(request):
    data = await request.json()
    user = User(name=data["name"], email=data["email"])
    await user.save()

    # Enqueue background work — response returns immediately
    await send_welcome_email.delay(user_id=user.id)
    await generate_avatar.delay(user_id=user.id)

    return Response.json({"id": user.id}, status=201)
```

### Task error handling

Tasks that raise exceptions are logged. The task queue does not retry by default -- if you need retry logic, implement it in the task itself:

```python
@task
async def sync_external_api(user_id: int):
    for attempt in range(3):
        try:
            await call_external_api(user_id)
            return
        except ConnectionError:
            if attempt < 2:
                await asyncio.sleep(2**attempt)
    await log_event("sync_failed", user_id=user_id)
```

## DataLoader for N+1 Prevention

The DataLoader batches and deduplicates async lookups to prevent N+1 query patterns:

```python
from hyperdjango.dataloader import DataLoader


async def load_users(user_ids: list[int]) -> list[User]:
    users = await User.objects.filter(id__in=user_ids).all()
    user_map = {u.id: u for u in users}
    return [user_map[uid] for uid in user_ids]


user_loader = DataLoader(load_users)

# These three calls result in ONE query: SELECT * FROM users WHERE id IN (1, 2, 3)
user1 = await user_loader.load(1)
user2 = await user_loader.load(2)
user3 = await user_loader.load(3)
```

### DataLoader in views

```python
@app.get("/orders")
async def list_orders(request):
    orders = await Order.objects.all()

    async def load_user_for_order(order):
        order.user = await user_loader.load(order.user_id)
        return order

    # All user lookups are batched into a single query
    orders_with_users = await asyncio.gather(*[load_user_for_order(o) for o in orders])
    return {"orders": orders_with_users}
```

### DataLoader caching

DataLoader caches results within a single request. Each request should create a new DataLoader instance to avoid stale data across requests:

```python
@app.get("/orders")
async def list_orders(request):
    # Fresh loader per request
    user_loader = DataLoader(load_users)
    # ... use user_loader
```

## Free-Threaded Python 3.14

HyperDjango runs on Python 3.14+ with the GIL disabled (`free-threaded`). This means:

- **True parallelism** for CPU-bound Python code -- multiple threads execute Python bytecode simultaneously
- **No GIL contention** for the Zig native extension -- native code already ran without the GIL, but now Python code around it also runs in parallel
- **Thread-safe caches** -- prepared statement cache, template cache, and connection pool use proper mutexes
- **`threading.local()`** for per-thread state (transaction depth tracking, request context)

### What this means in practice

With the GIL disabled, a multi-threaded server truly handles requests in parallel at the Python level. CPU-bound work (template rendering, serialization, validation) runs concurrently across threads rather than being serialized by the GIL.

## Zig Native Code Outside Python Runtime

The native Zig components (HTTP server, router, database driver, template engine, JSON parser) run outside the Python runtime entirely. They are compiled native code with their own thread pool.

This means:

- HTTP parsing, routing, and response serialization happen in Zig threads -- Python is only invoked for the view function
- Database queries are executed by the Zig pg.zig driver with its own connection pool
- Template rendering (for cached templates) happens in Zig with SIMD-accelerated string operations
- JSON parsing and serialization use SIMD instructions in native code

The Python view function is the only part that touches the Python runtime. Everything before it (parsing, routing) and after it (response serialization, sending) is native code.

## Async Testing Patterns

The test client is async-aware. Use `async with` for the test client and `await` for requests:

```python
from hyperdjango.testing import TestClient


async def test_list_users():
    async with TestClient(app) as client:
        response = await client.get("/users")
        assert response.status_code == 200
        data = response.json()
        assert len(data["users"]) >= 0


async def test_create_user():
    async with TestClient(app) as client:
        response = await client.post(
            "/users", json={"name": "Alice", "email": "alice@example.com"}
        )
        assert response.status_code == 201
        data = response.json()
        assert "id" in data
```

### Testing with database transactions

Each test runs in a savepoint that is rolled back after the test completes, so tests are isolated:

```python
async def test_create_and_fetch():
    async with TestClient(app) as client:
        # Create
        resp = await client.post("/users", json={"name": "Test"})
        user_id = resp.json()["id"]

        # Fetch
        resp = await client.get(f"/users/{user_id}")
        assert resp.json()["name"] == "Test"

    # After the test, the user is rolled back -- no cleanup needed
```

### Testing concurrent behavior

```python
async def test_concurrent_requests():
    async with TestClient(app) as client:
        # Simulate concurrent requests
        responses = await asyncio.gather(
            client.get("/users"),
            client.get("/orders"),
            client.get("/stats"),
        )
        for resp in responses:
            assert resp.status_code == 200
```

### Testing WebSocket

```python
async def test_websocket_echo():
    async with TestClient(app) as client:
        async with client.websocket("/ws/echo") as ws:
            await ws.send_text("hello")
            response = await ws.receive_text()
            assert response == "Echo: hello"
```

### Testing SSE

```python
async def test_sse_stream():
    async with TestClient(app) as client:
        async with client.stream("GET", "/events") as response:
            events = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
                if len(events) >= 3:
                    break
            assert len(events) == 3
```
