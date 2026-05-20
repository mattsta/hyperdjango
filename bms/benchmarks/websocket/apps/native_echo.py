"""Minimal HyperApp with a single native WebSocket echo route.

No database, no auth, no channels/rooms — just the raw native Zig
WebSocket path (`ZigWebSocket`, see hyperdjango/websocket.py) so the
benchmark measures the server primitive itself rather than any
application-level plumbing built on top of it.

Run directly (host/port via CLI args, matching scripts/e2e_helper.py's
AppRunner convention):

    uv run python benchmarks/websocket/apps/native_echo.py 127.0.0.1 19901
"""

import sys

from hyperdjango import HyperApp
from hyperdjango.websocket import WebSocketDisconnect

app = HyperApp(title="WS Bench (native)")


@app.websocket("/ws/echo")
async def echo(ws):
    await ws.accept()
    # receive() is type-preserving: str for a text frame, bytes for a binary
    # frame. That's the right primitive for an echo that must reflect each
    # frame back as the SAME type (unlike iter_text()/iter_bytes(), which
    # coerce to one type). Stops cleanly on disconnect.
    try:
        while True:
            msg = await ws.receive()
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_text(msg)
    except WebSocketDisconnect:
        pass


app.mount_health()


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 19901
    app.run(host=host, port=port)
