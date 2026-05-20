"""Unified benchmark framework — one result schema, one history, one dashboard,
and one registry/runner that every subsystem (HTTP, WebSocket, startup, …) feeds.

- ``results``  — the Run → Suite → Sweep schema + non-destructive run history.
- ``dashboard`` — the shared Plotly report (Suite / Run / Compare / Sweep / Metric).
- ``registry``  — register a benchmark (variants + fixtures + sweeps + client).
- ``runner``    — execute selected benchmarks with automated setup/teardown and
                  archive into the shared history.
"""
