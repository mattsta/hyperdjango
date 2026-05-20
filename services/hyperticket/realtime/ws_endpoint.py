"""
WebSocket endpoint — /ws/connect with session auth.

Agents connect to receive live updates for their tenant.
Subscribes to relevant channels: personal notifications, team queue, dashboard.
Optionally subscribes to specific ticket channels.
"""

import asyncio
import contextlib
import queue as thread_queue
from urllib.parse import parse_qs

from hyperdjango.guard import Require, guard_websocket
from hyperdjango.websocket import WebSocketDisconnect

from .channels import (
    dashboard_channel,
    layer,
    notification_channel,
    team_channel,
    ticket_channel,
)


def register_ws_endpoint(app, auth):
    """Register the WebSocket endpoint on the app.

    Called from app.py after app and auth are created.
    """

    @app.websocket("/ws/connect")
    @guard_websocket(auth, Require.authenticated())
    async def ws_connect(ws):
        """WebSocket handler for agent live updates.

        Query params:
          ticket_id — subscribe to specific ticket channel
          team_id — subscribe to team channel
        """
        user = ws.user
        if user is None or not user.is_authenticated:
            await ws.send_json({"type": "error", "message": "Not authenticated"})
            await ws.close(4001, "Not authenticated")
            return

        user_id = user.id or 0
        tenant_id = user.get("tenant_id", 0)
        user_type = user.get("user_type", "agent")

        if not tenant_id:
            await ws.send_json({"type": "error", "message": "No tenant"})
            await ws.close(4002, "No tenant")
            return

        # Parse subscription params
        params = parse_qs(ws.query_string)

        # Subscribe to channels
        subscriptions: list[tuple[str, int]] = []
        _MAX_EXTRA_SUBSCRIPTIONS = 20  # prevent abuse

        # Thread-safe queue for channel → WS bridge
        tq = thread_queue.Queue(maxsize=1000)

        def on_msg(msg):
            with contextlib.suppress(thread_queue.Full):
                tq.put_nowait(msg)

        # Always subscribe to personal notifications + dashboard
        notif_ch = layer.channel(notification_channel(tenant_id, user_id))
        sub_id = notif_ch.subscribe(on_msg)
        subscriptions.append((notification_channel(tenant_id, user_id), sub_id))

        dash_ch = layer.channel(dashboard_channel(tenant_id))
        sub_id = dash_ch.subscribe(on_msg)
        subscriptions.append((dashboard_channel(tenant_id), sub_id))

        # Optional: subscribe to specific ticket channels (capped)
        # Channel names include tenant_id so cross-tenant impossible
        ticket_ids = params.get("ticket_id", [])[:_MAX_EXTRA_SUBSCRIPTIONS]
        for tid_str in ticket_ids:
            try:
                tid = int(tid_str)
            except ValueError, TypeError:
                continue
            ch = layer.channel(ticket_channel(tenant_id, tid))
            sub_id = ch.subscribe(on_msg)
            subscriptions.append((ticket_channel(tenant_id, tid), sub_id))

        # Optional: subscribe to team channels (capped)
        team_ids = params.get("team_id", [])[:_MAX_EXTRA_SUBSCRIPTIONS]
        for tid_str in team_ids:
            try:
                tid = int(tid_str)
            except ValueError, TypeError:
                continue
            ch = layer.channel(team_channel(tenant_id, tid))
            sub_id = ch.subscribe(on_msg)
            subscriptions.append((team_channel(tenant_id, tid), sub_id))

        await ws.send_json(
            {
                "type": "connected",
                "user_id": user_id,
                "tenant_id": tenant_id,
                "subscriptions": len(subscriptions),
            }
        )

        try:
            # Read + write loops
            async def write_ws():
                loop = asyncio.get_running_loop()
                while True:
                    msg = await loop.run_in_executor(None, tq.get)
                    if msg is None:
                        break
                    try:
                        await ws.send_json(msg.data)
                    except WebSocketDisconnect:
                        break

            async def read_ws():
                try:
                    async for data in ws.iter_json():
                        # Handle client messages (e.g., typing indicators)
                        msg_type = data.get("type", "")
                        if msg_type == "subscribe_ticket":
                            if len(subscriptions) >= _MAX_EXTRA_SUBSCRIPTIONS + 2:
                                await ws.send_json(
                                    {
                                        "type": "error",
                                        "message": "Subscription limit reached",
                                    }
                                )
                            else:
                                try:
                                    tid = int(data.get("ticket_id", 0))
                                except ValueError, TypeError:
                                    tid = 0
                                if tid:
                                    ch = layer.channel(ticket_channel(tenant_id, tid))
                                    sid = ch.subscribe(on_msg)
                                    subscriptions.append(
                                        (ticket_channel(tenant_id, tid), sid)
                                    )
                        elif msg_type == "ping":
                            await ws.send_json({"type": "pong"})
                except WebSocketDisconnect:
                    pass
                finally:
                    tq.put(None)  # Signal write loop to exit

            await asyncio.gather(read_ws(), write_ws())

        finally:
            # Cleanup subscriptions
            for ch_name, sub_id in subscriptions:
                ch = layer.channel(ch_name)
                ch.unsubscribe(sub_id)
