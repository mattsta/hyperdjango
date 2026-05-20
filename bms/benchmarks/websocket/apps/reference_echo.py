"""Reference echo server using the `websockets` PyPI library directly.

This is the baseline we compare hyperdjango's native Zig WebSocket
server against: no framework, just `websockets.asyncio.server.serve`
running the idiomatic echo handler on a single asyncio event loop
(the library's standard, single-process deployment shape).

Run directly (host/port via CLI args, same convention as native_echo.py):

    uv run python benchmarks/websocket/apps/reference_echo.py 127.0.0.1 19902
"""

import asyncio
import sys

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

# Match hyperdjango's native defaults (websocket.py: WebSocketConfig) so
# neither side is artificially constrained relative to the other.
MAX_MESSAGE_SIZE = 16 * 1024 * 1024  # 16 MB, mirrors ZigWebSocket default
PING_INTERVAL = 30
PING_TIMEOUT = 120


async def echo(websocket):
    try:
        async for message in websocket:
            await websocket.send(message)
    except ConnectionClosed:
        pass


async def _health(connection, request):
    """Plain HTTP health check on the same port (process_request hook)."""
    if request.path == "/health":
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        body = b'{"status":"ok"}'
        headers = Headers(
            [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        )
        return Response(200, "OK", headers, body)
    return None


async def main(host: str, port: int) -> None:
    async with serve(
        echo,
        host,
        port,
        max_size=MAX_MESSAGE_SIZE,
        ping_interval=PING_INTERVAL,
        ping_timeout=PING_TIMEOUT,
        compression=None,  # disabled on both sides — apples-to-apples raw frames
        process_request=_health,
    ):
        await asyncio.get_running_loop().create_future()  # run forever


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 19902
    asyncio.run(main(host, port))
