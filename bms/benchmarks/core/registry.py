"""Benchmark registry — each comparative benchmark registers once with a `run`
callable that measures its variants (with its own automated server setup/teardown
and high-performance client) and returns a `benchmarks.core.results.suite` block.

The runner (``benchmarks.core.runner``) discovers the registry, executes the
selected benchmarks, and archives every returned suite into the shared history —
so adding a new subsystem is just a new ``@register`` with a `run` that returns a
suite; it then appears in the unified dashboard for free."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# A benchmark's run(quick: bool, **opts) -> suite dict (see results.suite()).
RunFn = Callable[..., dict]


@dataclass
class Benchmark:
    key: str  # suite key, e.g. "http"
    label: str  # human label
    run: RunFn  # (quick=False, **opts) -> core suite dict
    description: str = ""


_REGISTRY: dict[str, Benchmark] = {}


def register(key: str, label: str, description: str = ""):
    """Decorator: register `fn` as the benchmark `key`. `fn(quick, **opts)`
    returns a core suite block."""

    def deco(fn: RunFn) -> RunFn:
        _REGISTRY[key] = Benchmark(
            key=key, label=label, run=fn, description=description
        )
        return fn

    return deco


def registry() -> dict[str, Benchmark]:
    """All registered benchmarks (importing this triggers registration modules)."""
    # Import the subsystem benchmark modules so their @register runs. Kept lazy
    # and defensive: a subsystem that fails to import must not break the others.
    for mod in ("benchmarks.http.bench", "benchmarks.websocket.bench"):
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            print(f"  (registry: {mod} unavailable: {e})")
    return dict(_REGISTRY)
