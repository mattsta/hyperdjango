"""Register the WebSocket comparison with the unified benchmark registry.
Reuses the existing async matrix (automated native + reference server
setup/teardown, multiprocess client) and returns a core suite."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import benchmarks.websocket.run as wr
from benchmarks.core.registry import register
from benchmarks.websocket.suite import build_websocket_suite


@register(
    "websocket",
    "WebSocket servers",
    "hyperdjango native WebSocket vs the websockets PyPI library",
)
def run_ws(quick: bool = False, **opts) -> dict:
    args = SimpleNamespace(full=not quick, profile=False, out=str(wr.OUT_DIR))
    results = asyncio.run(wr._amain(args))
    return build_websocket_suite(results)
