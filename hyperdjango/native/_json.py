"""
Python fallback JSON serialization.

Optimized pure-Python JSON with fast paths for common types.
When the Zig native extension is available, these are replaced
by SIMD-accelerated versions.
"""

import json as _json
from typing import Any


def json_dumps(obj: Any) -> bytes:
    """Serialize obj to JSON bytes.

    Optimized over stdlib json.dumps:
    - Outputs bytes directly (no str → encode step)
    - Uses separators=(',', ':') for compact output
    - Handles common non-serializable types via default
    """
    return _json.dumps(
        obj,
        default=_default_serializer,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def json_loads(data: bytes | str) -> Any:
    """Parse JSON bytes/str to Python objects."""
    return _json.loads(data)


def _default_serializer(obj):
    """Handle common non-JSON-serializable types."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
