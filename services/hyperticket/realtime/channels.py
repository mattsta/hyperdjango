"""
HyperTicket realtime — channel setup + LiveQuery-style event broadcasting.

Channel naming:
  ticket:{tenant_id}:{ticket_id}         — per-ticket live updates
  team:{tenant_id}:{team_id}             — team queue changes
  notifications:{tenant_id}:{user_id}    — personal notification feed
  dashboard:{tenant_id}                  — dashboard stats refresh

Uses InMemoryChannelLayer for pub/sub with per-tenant isolation.
"""

import threading

from hyperdjango.channels import InMemoryChannelLayer, set_channel_layer

# Module-level channel layer + lock
layer = InMemoryChannelLayer()
set_channel_layer(layer)

_subscriptions_lock = threading.Lock()


def ticket_channel(tenant_id: int, ticket_id: int) -> str:
    """Channel name for per-ticket live updates."""
    return f"ticket:{tenant_id}:{ticket_id}"


def team_channel(tenant_id: int, team_id: int) -> str:
    """Channel name for team queue changes."""
    return f"team:{tenant_id}:{team_id}"


def notification_channel(tenant_id: int, user_id: int) -> str:
    """Channel name for personal notifications."""
    return f"notifications:{tenant_id}:{user_id}"


def dashboard_channel(tenant_id: int) -> str:
    """Channel name for dashboard stats refresh."""
    return f"dashboard:{tenant_id}"


async def broadcast_event(channel_name: str, event: str, data: dict) -> None:
    """Publish an event to a channel."""
    message = {"event": event, "data": data}
    channel = layer.channel(channel_name)
    await channel.publish(message)


async def broadcast_ticket_event(
    tenant_id: int,
    ticket_id: int,
    event: str,
    data: dict,
    team_id: int = 0,
) -> None:
    """Broadcast an event to ticket channel + team channel + dashboard."""
    await broadcast_event(ticket_channel(tenant_id, ticket_id), event, data)
    if team_id:
        await broadcast_event(team_channel(tenant_id, team_id), event, data)
    await broadcast_event(dashboard_channel(tenant_id), event, data)


async def broadcast_notification(
    tenant_id: int,
    user_id: int,
    notification_type: str,
    message: str,
    ticket_id: int = 0,
) -> None:
    """Send a personal notification to a user's channel."""
    await broadcast_event(
        notification_channel(tenant_id, user_id),
        "notification.new",
        {
            "type": notification_type,
            "message": message,
            "ticket_id": ticket_id,
        },
    )
