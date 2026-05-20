# Signals

Signals allow decoupled components to receive notifications when actions occur. When a model is saved, deleted, or a user logs in, signals notify registered receivers without tight coupling between the sender and handler code.

HyperDjango's signal system is async-first, thread-safe, supports both sync and async receivers, weak references, deduplication via `dispatch_uid`, and wildcard (sender-less) subscriptions.

## Quick Start

```python
from hyperdjango.signals import Signal, post_save


# Connect a receiver to a built-in signal
@post_save.connect
async def on_save(sender, **kwargs):
    instance = kwargs["instance"]
    created = kwargs["created"]
    if created:
        print(f"New {sender.__name__}: {instance.pk}")


# Send a custom signal
order_completed = Signal(name="order_completed")
await order_completed.send(sender=OrderView, order=order, total=99.99)
```

## Built-in Signals

HyperDjango provides 9 built-in signals covering model lifecycle, authentication, and request lifecycle.

### Model Lifecycle Signals

| Signal        | Sent When               | Keyword Arguments               |
| ------------- | ----------------------- | ------------------------------- |
| `pre_save`    | Before `model.save()`   | `sender`, `instance`, `created` |
| `post_save`   | After `model.save()`    | `sender`, `instance`, `created` |
| `pre_delete`  | Before `model.delete()` | `sender`, `instance`            |
| `post_delete` | After `model.delete()`  | `sender`, `instance`            |

The `sender` is always the model class (e.g., `Article`, `User`). The `instance` is the model instance being saved or deleted. The `created` flag on save signals is `True` for new inserts, `False` for updates.

```python
from hyperdjango.signals import pre_save, post_save, pre_delete, post_delete


@post_save.connect
async def on_article_save(sender, **kwargs):
    instance = kwargs["instance"]
    created = kwargs["created"]
    if sender.__name__ == "Article":
        if created:
            print(f"New article: {instance.title}")
        else:
            print(f"Updated article: {instance.title}")
```

### Authentication Signals

| Signal              | Sent When                       | Keyword Arguments           |
| ------------------- | ------------------------------- | --------------------------- |
| `user_logged_in`    | User authenticates successfully | `sender`, `request`, `user` |
| `user_logged_out`   | User logs out                   | `sender`, `request`, `user` |
| `user_login_failed` | Authentication attempt fails    | `sender`, `credentials`     |

```python
from hyperdjango.signals import user_logged_in, user_logged_out, user_login_failed


@user_logged_in.connect
async def on_login(sender, **kwargs):
    user = kwargs["user"]
    request = kwargs["request"]
    await SecurityLog.objects.create(
        event_type="login",
        user_id=user.pk,
        ip_address=request.client_ip,
    )


@user_login_failed.connect
async def on_failed_login(sender, **kwargs):
    credentials = kwargs["credentials"]
    await SecurityLog.objects.create(
        event_type="login_failed",
        details=f"Failed login for: {credentials.get('username', 'unknown')}",
    )
```

### Request Lifecycle Signals

| Signal             | Sent When                      | Keyword Arguments |
| ------------------ | ------------------------------ | ----------------- |
| `request_started`  | HTTP request begins processing | `sender`          |
| `request_finished` | HTTP request completes         | `sender`          |

```python
from hyperdjango.signals import request_started, request_finished


@request_started.connect
async def on_request_start(sender, **kwargs):
    print("Request processing started")


@request_finished.connect
async def on_request_end(sender, **kwargs):
    print("Request processing finished")
```

## Signal Class API

### Signal(name="")

Create a new signal instance. The `name` is optional and used for debugging:

```python
from hyperdjango.signals import Signal

payment_completed = Signal(name="payment_completed")
order_shipped = Signal(name="order_shipped")
inventory_low = Signal()  # Name is optional
```

### signal.connect()

Connect a receiver function to the signal. Can be used as a decorator or called directly:

```python
signal.connect(receiver, *, weak=False, dispatch_uid=None)
```

**Parameters:**

| Parameter      | Type          | Default | Description                                    |
| -------------- | ------------- | ------- | ---------------------------------------------- |
| `receiver`     | `Callable`    | None    | Function to call when signal fires             |
| `weak`         | `bool`        | `False` | Store a weak reference (auto-disconnect on GC) |
| `dispatch_uid` | `str \| None` | `None`  | Unique ID to prevent duplicate connections     |

**As a decorator:**

```python
@my_signal.connect
async def handler(sender, **kwargs): ...
```

**As a decorator with options:**

```python
@my_signal.connect(weak=True, dispatch_uid="unique_handler")
async def handler(sender, **kwargs): ...
```

**Direct call:**

```python
async def handler(sender, **kwargs): ...


my_signal.connect(handler)
my_signal.connect(handler, dispatch_uid="my_handler")
```

### signal.disconnect()

Remove a receiver from the signal. Returns `True` if a receiver was removed, `False` if not found:

```python
signal.disconnect(receiver=None, *, dispatch_uid=None)
```

```python
# Disconnect by function reference
removed = my_signal.disconnect(handler)  # True

# Disconnect by dispatch_uid
removed = my_signal.disconnect(dispatch_uid="my_handler")  # True

# Not found
removed = my_signal.disconnect(unknown_fn)  # False
```

### await signal.send()

Send the signal to all connected receivers. Returns a list of `(receiver, response)` tuples:

```python
responses = await signal.send(sender=MyClass, **kwargs)
```

Each receiver is called with `sender` as the first positional argument and all `**kwargs`. If a receiver raises an exception, it is captured as the response value (not re-raised), and the remaining receivers still execute:

```python
responses = await payment_completed.send(
    sender=PaymentProcessor,
    order_id=order.id,
    amount=99.99,
)

for receiver_fn, response in responses:
    if isinstance(response, Exception):
        print(f"Receiver {receiver_fn.__name__} failed: {response}")
    else:
        print(f"Receiver {receiver_fn.__name__} returned: {response}")
```

### await signal.send_robust()

Identical to `send()` -- guaranteed to call all receivers even if earlier ones raise. Exceptions are captured in the response list rather than propagated:

```python
responses = await my_signal.send_robust(sender=MyClass, key="value")
```

### signal.has_receivers()

Check if any receivers are connected:

```python
if payment_completed.has_receivers():
    await payment_completed.send(sender=PaymentProcessor, order_id=42)
```

### signal.receiver_count

Property returning the number of connected receivers:

```python
count = payment_completed.receiver_count
print(f"{count} receivers connected")
```

## Connecting Receivers

### Basic Connection

The simplest form -- connect a function to receive all sends of a signal:

```python
from hyperdjango.signals import post_save


@post_save.connect
async def on_any_save(sender, **kwargs):
    instance = kwargs["instance"]
    created = kwargs["created"]
    print(f"{sender.__name__} {'created' if created else 'updated'}: {instance.pk}")
```

### Filtering by Sender

To receive signals from a specific sender only, check the sender in your receiver:

```python
@post_save.connect
async def on_article_save(sender, **kwargs):
    if sender.__name__ != "Article":
        return
    instance = kwargs["instance"]
    if kwargs["created"]:
        await notify_subscribers(instance)
```

### Wildcard Receivers

Omitting any sender check means receiving signals from all senders. This is useful for cross-cutting concerns like audit logging:

```python
@post_save.connect
async def audit_all_saves(sender, **kwargs):
    instance = kwargs["instance"]
    action = "created" if kwargs["created"] else "updated"
    await AuditLog.objects.create(
        model_name=sender.__name__,
        object_id=str(instance.pk),
        action=action,
    )
```

### Sync and Async Receivers

Both sync and async receiver functions are supported. Async receivers are awaited; sync receivers are called directly:

```python
# Async receiver (awaited)
@post_save.connect
async def async_handler(sender, **kwargs):
    await do_async_work()


# Sync receiver (called directly)
@post_save.connect
def sync_handler(sender, **kwargs):
    do_sync_work()
```

The signal dispatch detects whether each receiver is a coroutine function (via `inspect.iscoroutinefunction`) and handles it appropriately.

## Weak References

By default, signals hold strong references to receivers. Set `weak=True` to use weak references, which allows receivers to be garbage collected:

```python
@my_signal.connect(weak=True)
async def temporary_handler(sender, **kwargs): ...


# When temporary_handler goes out of scope and is GC'd,
# it is automatically removed from the signal
```

Weak references are useful for:

- Receivers on objects that may be destroyed (e.g., view instances)
- Temporary debugging handlers
- Preventing memory leaks in long-running processes

When a weak-referenced receiver is garbage collected, it is automatically cleaned up from the signal's receiver list.

### Bound Methods

Weak references to bound methods use `weakref.WeakMethod`, which correctly handles method-level GC:

```python
class MyHandler:
    async def on_save(self, sender, **kwargs): ...


handler = MyHandler()
post_save.connect(handler.on_save, weak=True)

# When `handler` is garbage collected, the receiver is auto-removed
```

## Deduplication with dispatch_uid

The `dispatch_uid` parameter prevents the same receiver from being connected multiple times. If a receiver with the same `dispatch_uid` is already connected, the connection is silently ignored:

```python
# This handler will only be connected once, even if this code runs multiple times
@post_save.connect(dispatch_uid="article_index_update")
async def update_search_index(sender, **kwargs): ...


# Calling connect again with the same dispatch_uid is a no-op
post_save.connect(update_search_index, dispatch_uid="article_index_update")
```

This is especially useful when signal handlers are registered at module import time, which may happen multiple times in some configurations.

### Disconnecting by dispatch_uid

```python
# Disconnect by uid without needing the function reference
post_save.disconnect(dispatch_uid="article_index_update")
```

## Custom Signals

Define application-specific signals for domain events:

```python
from hyperdjango.signals import Signal

# Define custom signals
payment_completed = Signal(name="payment_completed")
order_shipped = Signal(name="order_shipped")
inventory_low = Signal(name="inventory_low")
user_upgraded = Signal(name="user_upgraded")
```

### Sending Custom Signals

```python
# Send with arbitrary keyword arguments
await payment_completed.send(
    sender=PaymentProcessor,
    order_id=order.id,
    amount=99.99,
    currency="USD",
)

await order_shipped.send(
    sender=ShippingService,
    order_id=order.id,
    tracking_number="1Z999AA10123456784",
    carrier="UPS",
)

await inventory_low.send(
    sender=InventoryChecker,
    product_id=product.id,
    current_stock=3,
    reorder_threshold=10,
)
```

### Receiving Custom Signals

```python
@payment_completed.connect
async def send_receipt(sender, **kwargs):
    await EmailService.send_receipt(
        order_id=kwargs["order_id"],
        amount=kwargs["amount"],
    )


@payment_completed.connect
async def update_analytics(sender, **kwargs):
    await AnalyticsEvent.objects.create(
        event_type="payment",
        data={"order_id": kwargs["order_id"], "amount": kwargs["amount"]},
    )


@inventory_low.connect
async def alert_ops(sender, **kwargs):
    await SlackNotifier.send(
        channel="#inventory",
        message=f"Low stock: product {kwargs['product_id']} at {kwargs['current_stock']} units",
    )
```

## Practical Patterns

### Audit Trail

Log all model changes to an audit log:

```python
from datetime import datetime, UTC

from hyperdjango.signals import post_save, post_delete


@post_save.connect(dispatch_uid="audit_save")
async def audit_save(sender, **kwargs):
    instance = kwargs["instance"]
    await AuditLog.objects.create(
        model_name=sender.__name__,
        object_id=str(instance.pk),
        action="create" if kwargs["created"] else "update",
        timestamp=datetime.now(UTC),
    )


@post_delete.connect(dispatch_uid="audit_delete")
async def audit_delete(sender, **kwargs):
    instance = kwargs["instance"]
    await AuditLog.objects.create(
        model_name=sender.__name__,
        object_id=str(instance.pk),
        action="delete",
        timestamp=datetime.now(UTC),
    )
```

### Cache Invalidation

Invalidate cached data when models change:

```python
from hyperdjango.signals import post_save, post_delete


@post_save.connect(dispatch_uid="cache_invalidate_save")
async def invalidate_on_save(sender, **kwargs):
    instance = kwargs["instance"]
    table = sender._meta.table
    cache.delete(f"{table}:{instance.pk}")
    cache.delete(f"{table}:list")


@post_delete.connect(dispatch_uid="cache_invalidate_delete")
async def invalidate_on_delete(sender, **kwargs):
    instance = kwargs["instance"]
    table = sender._meta.table
    cache.delete(f"{table}:{instance.pk}")
    cache.delete(f"{table}:list")
```

### Email on Registration

Send a welcome email when a new user is created:

```python
from hyperdjango.signals import post_save


@post_save.connect(dispatch_uid="welcome_email")
async def welcome_email(sender, **kwargs):
    if sender.__name__ != "User":
        return
    if not kwargs["created"]:
        return
    instance = kwargs["instance"]
    await send_mail(
        subject="Welcome!",
        message=f"Hi {instance.name}, welcome to our app.",
        to=[instance.email],
    )
```

### Search Index Update

Keep a search index in sync with database changes:

```python
from hyperdjango.signals import post_save, post_delete

INDEXED_MODELS = {"Article", "Product", "Page"}


@post_save.connect(dispatch_uid="search_index_save")
async def index_on_save(sender, **kwargs):
    if sender.__name__ not in INDEXED_MODELS:
        return
    instance = kwargs["instance"]
    await search_index.upsert(
        index=sender.__name__.lower(),
        id=instance.pk,
        document=instance.to_search_dict(),
    )


@post_delete.connect(dispatch_uid="search_index_delete")
async def remove_from_index(sender, **kwargs):
    if sender.__name__ not in INDEXED_MODELS:
        return
    instance = kwargs["instance"]
    await search_index.delete(
        index=sender.__name__.lower(),
        id=instance.pk,
    )
```

### Webhook Trigger

Fire webhooks when domain events occur:

```python
import json

webhook_event = Signal(name="webhook_event")


@webhook_event.connect(dispatch_uid="webhook_dispatch")
async def dispatch_webhook(sender, **kwargs):
    event_type = kwargs["event_type"]
    payload = kwargs["payload"]

    # Find all webhook subscriptions for this event type
    subscriptions = await WebhookSubscription.objects.filter(
        event_type=event_type,
        active=True,
    ).all()

    for sub in subscriptions:
        await enqueue_webhook_delivery(
            url=sub.url,
            secret=sub.secret,
            payload=json.dumps(payload),
        )


# Trigger from model signals
@post_save.connect(dispatch_uid="order_webhook")
async def order_webhook(sender, **kwargs):
    if sender.__name__ != "Order":
        return
    instance = kwargs["instance"]
    event = "order.created" if kwargs["created"] else "order.updated"
    await webhook_event.send(
        sender=sender,
        event_type=event,
        payload={"order_id": instance.pk, "status": instance.status},
    )
```

### Activity Feed

Generate activity items from model changes:

```python
@post_save.connect(dispatch_uid="activity_feed")
async def generate_activity(sender, **kwargs):
    TRACKED = {"Article": "article", "Comment": "comment", "Review": "review"}
    content_type = TRACKED.get(sender.__name__)
    if not content_type:
        return

    instance = kwargs["instance"]
    verb = "created" if kwargs["created"] else "updated"

    await Activity.objects.create(
        actor_id=instance.author_id,
        verb=verb,
        content_type=content_type,
        object_id=instance.pk,
    )
```

### Denormalized Counter Update

Keep denormalized counts in sync:

```python
@post_save.connect(dispatch_uid="comment_count_save")
async def update_comment_count_on_save(sender, **kwargs):
    if sender.__name__ != "Comment":
        return
    if not kwargs["created"]:
        return
    instance = kwargs["instance"]
    await Article.objects.filter(id=instance.article_id).update(
        comment_count=F("comment_count") + 1,
    )


@post_delete.connect(dispatch_uid="comment_count_delete")
async def update_comment_count_on_delete(sender, **kwargs):
    if sender.__name__ != "Comment":
        return
    instance = kwargs["instance"]
    await Article.objects.filter(id=instance.article_id).update(
        comment_count=F("comment_count") - 1,
    )
```

## Thread Safety

The Signal class is thread-safe. The receivers list is protected by a `threading.Lock`, making it safe to connect, disconnect, and send from multiple threads concurrently. This is important for free-threaded Python (3.14t) where the GIL is disabled.

## Signal Debugging

### Inspecting Connected Receivers

```python
# Check if any receivers are connected
if post_save.has_receivers():
    print(f"post_save has {post_save.receiver_count} receivers")

# String representation shows name and count
print(post_save)  # Signal('post_save', receivers=3)
```

### Temporarily Disabling Signals

```python
# Save the receivers, clear, do work, restore
original_receivers = list(post_save._receivers)
post_save._receivers.clear()

try:
    await do_work_without_signals()
finally:
    post_save._receivers = original_receivers
```

## Reference

### Imports

```python
from hyperdjango.signals import (
    # Signal class
    Signal,
    # Model lifecycle
    pre_save,
    post_save,
    pre_delete,
    post_delete,
    # Authentication
    user_logged_in,
    user_logged_out,
    user_login_failed,
    # Request lifecycle
    request_started,
    request_finished,
)
```

### Signal API Summary

| Method / Property                          | Returns       | Description                                    |
| ------------------------------------------ | ------------- | ---------------------------------------------- |
| `connect(receiver, *, weak, dispatch_uid)` | `Callable`    | Connect a receiver (also works as decorator)   |
| `disconnect(receiver, *, dispatch_uid)`    | `bool`        | Disconnect a receiver                          |
| `await send(sender, **kwargs)`             | `list[tuple]` | Send signal, return (receiver, response) pairs |
| `await send_robust(sender, **kwargs)`      | `list[tuple]` | Same as send, catches all exceptions           |
| `has_receivers()`                          | `bool`        | True if any receivers connected                |
| `receiver_count`                           | `int`         | Number of connected receivers                  |

### Built-in Signal Arguments

| Signal              | `sender`      | Additional kwargs     |
| ------------------- | ------------- | --------------------- |
| `pre_save`          | Model class   | `instance`, `created` |
| `post_save`         | Model class   | `instance`, `created` |
| `pre_delete`        | Model class   | `instance`            |
| `post_delete`       | Model class   | `instance`            |
| `user_logged_in`    | Auth class    | `request`, `user`     |
| `user_logged_out`   | Auth class    | `request`, `user`     |
| `user_login_failed` | Auth class    | `credentials`         |
| `request_started`   | Handler class | (none)                |
| `request_finished`  | Handler class | (none)                |
