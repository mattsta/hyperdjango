"""HTTP framework benchmark suite.

Compares hyperdjango (threaded and reactor connection models) against FastAPI
(async / uvicorn) and Flask (sync / gunicorn) across payload sizes and — most
importantly — client concurrency levels, to surface the threaded-vs-reactor
CROSSOVER: threaded is fastest below the worker-pool ceiling and cliffs above
it; reactor (and async) scale with connection count. Metrics: throughput,
latency percentiles, and server memory (RSS), with JSON + Markdown reporting.
"""
