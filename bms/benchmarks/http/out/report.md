# HTTP framework benchmark

- Workers/threads per server (W): **64** (so the threaded/sync ceiling is ~W connections)
- Measurement: 3.0s after 1.0s warmup, keep-alive, closed-loop; single machine over loopback.
- Frameworks: hyperdjango native (threaded / reactor), FastAPI (async/uvicorn), Flask (sync/gunicorn-gthread).

## How to read this

Watch each column as **conns** grows past W. Threaded/sync models plateau or cliff at ~W (a connection pins a worker); reactor/async keep scaling (a connection holds a worker only during a request). Below W, threaded is typically fastest (no multiplexing overhead) — the crossover.

## Payload: tiny (~0 B)

### Throughput (requests/sec — higher is better)

| conns | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----- | -------------------- | ------------------- | ------- | ----- |
| 1     | 22,229               | 3,711               | —       | —     |
| 8     | 135,784              | 68,489              | —       | —     |
| 64    | 64,360               | 60,972              | —       | —     |

_requests/sec_

### Latency p99 (ms — lower is better)

| conns | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----- | -------------------- | ------------------- | ------- | ----- |
| 1     | 0.1                  | 0.3                 | —       | —     |
| 8     | 0.1                  | 0.2                 | —       | —     |
| 64    | 2.5                  | 2.6                 | —       | —     |

_milliseconds_

## Payload: 1k (~1024 B)

### Throughput (requests/sec — higher is better)

| conns | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----- | -------------------- | ------------------- | ------- | ----- |
| 1     | 19,201               | 4,000               | —       | —     |
| 8     | 109,886              | 61,124              | —       | —     |
| 64    | 63,758               | 62,228              | —       | —     |

_requests/sec_

### Latency p99 (ms — lower is better)

| conns | hyperdjango-threaded | hyperdjango-reactor | fastapi | flask |
| ----- | -------------------- | ------------------- | ------- | ----- |
| 1     | 0.1                  | 0.4                 | —       | —     |
| 8     | 0.1                  | 0.2                 | —       | —     |
| 64    | 2.5                  | 2.6                 | —       | —     |

_milliseconds_

## Peak server memory (RSS, MiB) at max concurrency

| framework            | RSS MiB |
| -------------------- | ------- |
| hyperdjango-threaded | 0.0     |
| hyperdjango-reactor  | 0.0     |
| fastapi              | 0.0     |
| flask                | 0.0     |
