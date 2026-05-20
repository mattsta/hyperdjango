# Transactions

Atomic database operations with automatic savepoint nesting, connection pinning, and configurable isolation levels. All transaction management is backed by pg.zig (native Zig PostgreSQL driver).

## Basic Transactions

Use `db.transaction()` as an async context manager. If the block completes normally, the transaction is committed. If an exception is raised, it is rolled back.

```python
async with db.transaction():
    user = await User.objects.create(name="Alice", email="alice@example.com")
    await Profile.objects.create(user_id=user.id, bio="Hello!")
    # Both committed together. If either fails, both roll back.
```

The context manager issues `BEGIN` on entry and `COMMIT` on successful exit. If any exception propagates out of the block, `ROLLBACK` is issued instead, and the exception is re-raised.

```python
try:
    async with db.transaction():
        await Account.objects.create(name="Test")
        raise ValueError("Something went wrong")
        # ROLLBACK issued here
except ValueError:
    # Account was NOT created
    pass
```

## transaction() API

```python
@asynccontextmanager
async def transaction(
    self, savepoint_name: str | None = None, *, isolation_level: str | None = None
):
    """Context manager for database transactions.

    Args:
        savepoint_name: Optional name for the savepoint (only used for
                       nested transactions). Defaults to "sp_N" where N
                       is the nesting depth.
        isolation_level: Optional SQL isolation level for the outermost
                       transaction. One of "read_committed" (the default),
                       "repeatable_read", "serializable", "read_uncommitted".
                       Applied as `BEGIN ISOLATION LEVEL ...` so it is bound
                       to the pinned connection atomically. Setting it on a
                       nested (savepoint) block raises ValueError — Postgres
                       cannot change the level of an already-open transaction.
    """
```

The context manager yields the `Database` instance, so you can optionally capture it:

```python
async with db.transaction() as tx:
    await tx.execute("INSERT INTO logs (msg) VALUES ($1)", "started")
    await tx.query("SELECT * FROM logs")
```

## Nested Transactions (Savepoints)

Nested `transaction()` calls automatically use PostgreSQL `SAVEPOINT` commands. The nesting depth is tracked per-thread using `threading.local()`, which is safe for concurrent requests in free-threaded Python 3.14t.

```python
async with db.transaction():
    # Depth 0: BEGIN
    await Account.objects.create(name="Main Account")

    try:
        async with db.transaction():
            # Depth 1: SAVEPOINT sp_2
            await Transfer.objects.create(from_id=1, to_id=2, amount=100)
            raise ValueError("Insufficient funds")
            # ROLLBACK TO SAVEPOINT sp_2
    except ValueError:
        pass  # Inner rolled back, outer continues

    await AuditLog.objects.create(action="transfer_failed")
    # COMMIT — outer transaction commits with Account + AuditLog
```

### Depth Tracking

| Nesting Level       | SQL on Entry         | SQL on Success               | SQL on Error                     |
| ------------------- | -------------------- | ---------------------------- | -------------------------------- |
| Level 0 (outermost) | `BEGIN`              | `COMMIT`                     | `ROLLBACK`                       |
| Level 1             | `SAVEPOINT sp_2`     | `RELEASE SAVEPOINT sp_2`     | `ROLLBACK TO SAVEPOINT sp_2`     |
| Level 2             | `SAVEPOINT sp_3`     | `RELEASE SAVEPOINT sp_3`     | `ROLLBACK TO SAVEPOINT sp_3`     |
| Level N             | `SAVEPOINT sp_{N+1}` | `RELEASE SAVEPOINT sp_{N+1}` | `ROLLBACK TO SAVEPOINT sp_{N+1}` |

You never need to manage savepoints manually -- the depth counter handles everything.

### Named Savepoints

You can explicitly name a savepoint if you need to reference it:

```python
async with db.transaction(savepoint_name="my_checkpoint"):
    # SAVEPOINT my_checkpoint
    await do_work()
    # RELEASE SAVEPOINT my_checkpoint
```

### Triple Nesting

Savepoints nest to arbitrary depth:

```python
async with db.transaction():
    # BEGIN
    await db.execute("INSERT INTO t1 VALUES (1)")

    async with db.transaction():
        # SAVEPOINT sp_2
        await db.execute("INSERT INTO t2 VALUES (2)")

        async with db.transaction():
            # SAVEPOINT sp_3
            await db.execute("INSERT INTO t3 VALUES (3)")
            # RELEASE SAVEPOINT sp_3

        # RELEASE SAVEPOINT sp_2

    # COMMIT
```

## atomic() Alias

`atomic()` is a convenience alias for `transaction()`, matching Django's naming convention:

```python
async with db.atomic():
    await do_work()

# Equivalent to:
async with db.transaction():
    await do_work()

# Named savepoints also work:
async with db.atomic(savepoint_name="my_save"):
    await do_work()

# isolation_level is forwarded too:
async with db.atomic(isolation_level="serializable"):
    await do_work()
```

## Connection Pinning

During a transaction, all queries are routed through the same database connection (pinned from the pool). This is essential for correctness -- PostgreSQL transactions are per-connection.

```python
async with db.transaction():
    # All three statements use the SAME PostgreSQL connection
    await db.execute("INSERT INTO accounts (name) VALUES ($1)", "Alice")
    rows = await db.query("SELECT * FROM accounts WHERE name = $1", "Alice")
    await db.execute("UPDATE accounts SET balance = $1 WHERE name = $2", 100, "Alice")
    # Connection returned to pool on COMMIT
```

Without pinning, different statements could be routed to different pool connections and would not see each other's uncommitted changes. pg.zig handles this automatically.

## Isolation Levels

PostgreSQL supports four isolation levels. The default is `READ COMMITTED`. Select one by passing `isolation_level=` to `transaction()` (or `atomic()`) — it is emitted as `BEGIN ISOLATION LEVEL ...`, so the level is bound to the pinned connection atomically with the transaction start:

```python
async with db.transaction(isolation_level="serializable"):
    ...
```

> **Do not** issue `SET TRANSACTION ISOLATION LEVEL ...` yourself before `transaction()`. That statement runs on whatever pooled connection happens to serve it and has no effect on the _separate_ connection the transaction later pins — the level silently defaults to `READ COMMITTED`. The `isolation_level=` argument is the only correct way to set it, because it is part of the same `BEGIN` on the same pinned connection. The argument is validated against a fixed allowlist (`read_committed`, `repeatable_read`, `serializable`, `read_uncommitted`); anything else raises `ValueError`, so no caller-supplied string is ever interpolated into SQL. It applies only to the **outermost** transaction — passing it on a nested (savepoint) block raises `ValueError`.

### READ COMMITTED (Default)

Each statement in the transaction sees the most recently committed data. Two reads of the same row may return different results if another transaction commits between them.

```python
async with db.transaction():  # or isolation_level="read_committed"
    # Statement 1 sees data committed before it started
    row1 = await db.query_one("SELECT balance FROM accounts WHERE id = $1", 1)

    # If another transaction commits here...

    # Statement 2 might see different data
    row2 = await db.query_one("SELECT balance FROM accounts WHERE id = $1", 1)
    # row1["balance"] might != row2["balance"]
```

This is usually fine for web applications. It prevents dirty reads but allows non-repeatable reads.

### SERIALIZABLE

For strictest isolation, pass `isolation_level="serializable"`. All reads see a consistent snapshot taken at the start of the transaction.

```python
async with db.transaction(isolation_level="serializable"):
    # All reads see a consistent snapshot
    balance = await db.query_val("SELECT balance FROM accounts WHERE id = $1", 1)
    # Even if another transaction commits, we still see the original balance
    await db.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2", 100, 1)
```

If two serializable transactions conflict (read-write dependency), PostgreSQL will abort one with a serialization failure. You must retry the transaction:

```python
MAX_RETRIES = 3
for attempt in range(MAX_RETRIES):
    try:
        async with db.transaction(isolation_level="serializable"):
            await transfer_funds(from_id=1, to_id=2, amount=100)
        break  # Success
    except Exception as e:
        if "serialization failure" in str(e).lower() and attempt < MAX_RETRIES - 1:
            continue  # Retry
        raise
```

### REPEATABLE READ

Snapshot taken at the start of the first read statement. Prevents non-repeatable reads but may cause serialization failures on write conflicts.

```python
async with db.transaction(isolation_level="repeatable_read"):
    # All reads see a consistent snapshot from the first SELECT
    ...
```

### READ UNCOMMITTED

PostgreSQL treats this the same as `READ COMMITTED` -- dirty reads are never allowed in PostgreSQL regardless of isolation level. Accepted (`isolation_level="read_uncommitted"`) for completeness.

## Error Recovery Patterns

### The PostgreSQL Aborted Transaction Problem

In PostgreSQL, when a statement fails inside a transaction, the transaction enters an "aborted" state. No further statements can execute -- even `SELECT 1` will fail with "current transaction is aborted, commands ignored until end of transaction block." You must `ROLLBACK` (or rollback to a savepoint) to recover.

```python
from hyperdjango.db import IntegrityError

async with db.transaction():
    try:
        await risky_operation()
    except IntegrityError:
        # Transaction is now ABORTED in PostgreSQL
        # Cannot do anything else -- must re-raise
        raise
```

### Savepoint-Based Recovery

To handle errors within a transaction without aborting the entire thing, wrap the risky operation in a nested transaction (savepoint):

```python
async with db.transaction():
    for item in items:
        try:
            async with db.transaction():  # SAVEPOINT
                await process(item)
                # RELEASE SAVEPOINT on success
        except Exception:
            # ROLLBACK TO SAVEPOINT — outer transaction continues
            failed_items.append(item)

    # Outer transaction commits with all successful items
    await AuditLog.objects.create(
        action="batch_process", details=f"Failed: {len(failed_items)}"
    )
```

This is the standard pattern for batch operations where individual items may fail but you want to commit the rest.

### Constraint Violation Recovery

Handle unique constraint violations gracefully. A constraint violation on either
the ORM or the direct-SQL path raises a typed `IntegrityError`; narrow it to the
unique-constraint case with `is_unique_violation` (see
[Database → typed exception hierarchy](database.md#typed-exception-hierarchy-one-contract-for-both-paths))
so a foreign-key or not-null violation is not silently swallowed:

```python
from hyperdjango.db import IntegrityError, is_unique_violation

async with db.transaction():
    try:
        async with db.transaction():  # Savepoint
            await User.objects.create(name="Alice", email="alice@example.com")
    except IntegrityError as e:
        if is_unique_violation(e):
            # Email already exists — update instead
            await User.objects.filter(email="alice@example.com").update(name="Alice")
        else:
            raise  # FK / not-null / check violation — a real error
```

### Idempotent Operations

Use savepoints to make operations idempotent (safe to retry):

```python
async with db.transaction():
    try:
        async with db.transaction():
            await db.execute(
                "INSERT INTO processed_events (event_id) VALUES ($1)", event_id
            )
    except Exception:
        # Already processed — skip
        return

    # Process the event (only reached if INSERT succeeded)
    await handle_event(event_id)
```

## Transactions in Views

### Basic View Pattern

```python
@app.post("/transfer")
async def transfer(request):
    data = await request.json()
    async with app.db.transaction():
        sender = await Account.objects.get(id=data["from"])
        receiver = await Account.objects.get(id=data["to"])
        amount = data["amount"]

        if sender.balance < amount:
            raise HTTPException(400, "Insufficient funds")

        sender.balance -= amount
        await sender.save()
        receiver.balance += amount
        await receiver.save()
        await TransferLog.objects.create(
            from_id=sender.id,
            to_id=receiver.id,
            amount=amount,
        )
    return {"transferred": amount}
```

If the `HTTPException` is raised, the entire transaction is rolled back -- no partial balance updates.

### Multiple Independent Operations

For operations that should succeed or fail independently, use separate transactions:

```python
@app.post("/batch-update")
async def batch_update(request):
    items = await request.json()
    results = []
    for item in items:
        try:
            async with app.db.transaction():
                await process_item(item)
                results.append({"id": item["id"], "status": "ok"})
        except Exception as e:
            results.append({"id": item["id"], "status": "error", "message": str(e)})
    return {"results": results}
```

### Read-Only Transactions

For consistency across multiple reads (e.g., generating a report), use a transaction even for reads:

```python
@app.get("/report")
async def generate_report(request):
    async with app.db.transaction(isolation_level="repeatable_read"):
        total_users = await db.query_val("SELECT COUNT(*) FROM users")
        total_revenue = await db.query_val("SELECT SUM(amount) FROM payments")
        active_sessions = await session_store.count()
    return {
        "users": total_users,
        "revenue": total_revenue,
        "sessions": active_sessions,
    }
```

## Advisory Locks

PostgreSQL advisory locks provide application-level locking without row-level contention. Use raw SQL within a transaction:

```python
# Session-level advisory lock (held until end of session/transaction)
async with db.transaction():
    # pg_advisory_xact_lock: released when transaction ends
    await db.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
    # Only one connection can hold this lock at a time
    await do_exclusive_work()
    # Lock released on COMMIT/ROLLBACK

# Try-lock variant (non-blocking)
async with db.transaction():
    result = await db.query_val("SELECT pg_try_advisory_xact_lock($1)", lock_id)
    if result:
        await do_exclusive_work()
    else:
        # Lock held by another process
        raise HTTPException(409, "Resource locked")
```

### Common Advisory Lock Patterns

```python
# Prevent concurrent cron job execution
CRON_LOCK_ID = 12345


async def run_cron():
    async with db.transaction():
        got_lock = await db.query_val(
            "SELECT pg_try_advisory_xact_lock($1)", CRON_LOCK_ID
        )
        if not got_lock:
            return  # Another worker is running this cron
        await do_cron_work()


# Per-resource locking using hash of resource identifier
import hashlib


def resource_lock_id(resource_type: str, resource_id: int) -> int:
    """Generate a deterministic lock ID from resource type + ID."""
    h = hashlib.sha256(f"{resource_type}:{resource_id}".encode()).digest()
    return int.from_bytes(h[:8], "big", signed=True)


async with db.transaction():
    lock_id = resource_lock_id("invoice", invoice_id)
    await db.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
    await generate_invoice(invoice_id)
```

## Deadlock Detection

PostgreSQL automatically detects deadlocks and aborts one of the conflicting transactions. Handle this in your application:

```python
async def safe_transfer(from_id, to_id, amount):
    """Transfer with deadlock prevention via consistent ordering."""
    # Always lock accounts in ID order to prevent deadlocks
    first_id, second_id = sorted([from_id, to_id])

    async with db.transaction():
        first = await Account.objects.get(id=first_id)
        second = await Account.objects.get(id=second_id)

        if from_id == first_id:
            sender, receiver = first, second
        else:
            sender, receiver = second, first

        sender.balance -= amount
        receiver.balance += amount
        await sender.save()
        await receiver.save()
```

If you cannot control ordering, handle the deadlock exception:

```python
MAX_RETRIES = 3
for attempt in range(MAX_RETRIES):
    try:
        async with db.transaction():
            await transfer_funds(from_id, to_id, amount)
        break
    except Exception as e:
        if "deadlock" in str(e).lower() and attempt < MAX_RETRIES - 1:
            continue  # Retry
        raise
```

## Performance

### Transaction Overhead

- `BEGIN` / `COMMIT`: Minimal overhead. pg.zig issues these as simple queries on the pinned connection.
- Savepoints: Each `SAVEPOINT` / `RELEASE SAVEPOINT` adds approximately 0.1ms of overhead per nesting level.
- Connection pinning: No extra overhead -- the connection is simply held from the pool for the duration of the transaction rather than being returned after each query.

### UNLOGGED Tables in Transactions

UNLOGGED tables (sessions, cache, rate limits) do not write WAL records, making transactions involving them significantly faster:

```python
async with db.transaction():
    # This INSERT to an UNLOGGED table is ~2-3x faster than to a regular table
    await session_store.create({"user_id": user.id})
    # This INSERT to a regular table writes WAL normally
    await AuditLog.objects.create(action="login")
```

### Keep Transactions Short

Long-running transactions hold connections from the pool and can cause connection exhaustion under load:

```python
# BAD: Long-running transaction holds connection
async with db.transaction():
    data = await fetch_external_api()  # 500ms network call
    await db.execute("INSERT INTO ...", data)

# GOOD: Fetch data first, then transaction
data = await fetch_external_api()
async with db.transaction():
    await db.execute("INSERT INTO ...", data)
```

### Pipeline Queries

For multiple independent queries, consider `db.pipeline()` instead of a transaction. Pipelines send all queries in a single network round-trip but do not provide atomicity:

```python
# 5.74x faster than sequential queries
results = await db.pipeline(
    [
        "SELECT COUNT(*) FROM users",
        "SELECT COUNT(*) FROM orders",
        "SELECT COUNT(*) FROM products",
    ]
)
```

## Testing with Transactions

### Savepoint Isolation in Tests

HyperDjango's `TestCase` wraps each test in a transaction and rolls it back after the test, so each test starts with a clean database:

```python
from hyperdjango.testing import TestCase


class UserTests(TestCase):
    async def test_create_user(self):
        # This runs inside a transaction
        user = await User.objects.create(name="Alice")
        assert user.id is not None
        # Transaction is rolled back after test -- user is gone

    async def test_another(self):
        # Clean slate -- no Alice here
        users = await User.objects.all()
        assert len(users) == 0
```

### Testing Transaction Rollback

```python
async def test_transaction_rollback(self):
    try:
        async with app.db.transaction():
            await User.objects.create(name="Alice")
            raise ValueError("Rollback!")
    except ValueError:
        pass

    # User was not created (rolled back)
    count = await User.objects.count()
    assert count == 0
```

### Testing Nested Savepoints

```python
async def test_savepoint_partial_rollback(self):
    async with app.db.transaction():
        await User.objects.create(name="Alice")

        try:
            async with app.db.transaction():  # Savepoint
                await User.objects.create(name="Bob")
                raise ValueError("Undo Bob")
        except ValueError:
            pass

        # Alice exists, Bob does not
        users = await User.objects.all()
        assert len(users) == 1
        assert users[0].name == "Alice"
```

## on_commit() Callbacks

Register callbacks to run after the outermost transaction commits. Callbacks are discarded on rollback.

```python
async with db.transaction():
    user = await User.objects.create(name="Alice", email="alice@example.com")

    # This runs AFTER the COMMIT, not inside the transaction
    db.on_commit(lambda: print(f"User {user.id} created!"))

    # Async callbacks are supported
    @db.on_commit
    async def send_welcome():
        await send_welcome_email(user.id)
```

### Use Cases

- **Send emails after user creation** — don't send if the transaction rolls back
- **Invalidate caches** — only after data is actually committed
- **Trigger webhooks** — fire external calls only when the change is durable
- **Update search index** — index the committed state, not uncommitted

### Behavior

- Callbacks run in registration order after `COMMIT`
- On `ROLLBACK`, all callbacks are silently discarded
- Both sync and async callbacks are supported
- Callbacks registered in nested transactions (savepoints) run after the **outermost** commit
- `on_commit()` can be used as a decorator: `@db.on_commit`

```python
# Practical example: email after order
async with db.transaction():
    order = await Order.objects.create(user_id=user.id, total=99.99)
    for item in cart_items:
        await OrderItem.objects.create(order_id=order.id, **item)

    db.on_commit(lambda: send_order_confirmation.delay(order_id=order.id))
    # If any item fails, the entire transaction rolls back
    # and the email is never sent
```
