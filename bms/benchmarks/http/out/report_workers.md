# HTTP worker-count scaling

- Machine cores: **256**. Fixed client concurrency: **128** (saturating). Sweep the per-server worker/parallelism budget W.
- W means W native threads (hyperdjango), W gthread threads (Flask), or W uvicorn worker processes (FastAPI) — the same parallelism budget.
- Watch throughput rise with W up to ~256 cores, then plateau or degrade as W over-subscribes the cores (context-switch / contention).

## Payload: plaintext (~0 B) — throughput (req/s) vs W

| W (workers) | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----------- | -------------------- | ------------------- | ------- | ----- |
| 8           | 104,198              | 145,847             | —       | —     |
| 16          | 189,363              | 254,828             | —       | —     |
| 32          | 197,833              | 124,300             | —       | —     |
| 64          | 68,440               | 66,051              | —       | —     |

### p99 latency (ms) vs W — plaintext

| W (workers) | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----------- | -------------------- | ------------------- | ------- | ----- |
| 8           | 2.0                  | 1.1                 | —       | —     |
| 16          | 0.1                  | 0.6                 | —       | —     |
| 32          | 0.9                  | 2.5                 | —       | —     |
| 64          | 2.5                  | 3.6                 | —       | —     |

## Payload: 16KiB (~16384 B) — throughput (req/s) vs W

| W (workers) | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----------- | -------------------- | ------------------- | ------- | ----- |
| 8           | 78,959               | 110,474             | —       | —     |
| 16          | 148,069              | 200,839             | —       | —     |
| 32          | 144,375              | 125,500             | —       | —     |
| 64          | 67,618               | 67,016              | —       | —     |

### p99 latency (ms) vs W — 16KiB

| W (workers) | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----------- | -------------------- | ------------------- | ------- | ----- |
| 8           | 2.0                  | 1.3                 | —       | —     |
| 16          | 0.2                  | 0.7                 | —       | —     |
| 32          | 0.9                  | 2.4                 | —       | —     |
| 64          | 2.5                  | 3.5                 | —       | —     |
