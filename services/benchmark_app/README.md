# Benchmark App

Minimal routes designed for load testing with wrk, ab, or hey. Tests raw Zig HTTP server throughput.

## Run

```bash
uv run python -m services.benchmark_app.app
```

## Benchmark

```bash
# JSON endpoint (TechEmpower-style)
wrk -t4 -c100 -d10s http://localhost:8000/json

# Plain text (raw throughput, no JSON serialization)
wrk -t4 -c100 -d10s http://localhost:8000/plaintext

# Path parameter routing
wrk -t4 -c100 -d10s http://localhost:8000/users/42
```

## Routes

| Method | Path          | Content-Type     | Description                    |
| ------ | ------------- | ---------------- | ------------------------------ |
| GET    | `/json`       | application/json | `{"message": "Hello, World!"}` |
| GET    | `/plaintext`  | text/plain       | `Hello, World!`                |
| GET    | `/users/{id}` | application/json | Path param extraction          |

## Expected Performance

On a modern Mac (Apple Silicon):

| Route       | Requests/sec | Latency (avg) |
| ----------- | ------------ | ------------- |
| /json       | ~13,000      | ~7ms          |
| /plaintext  | ~13,000      | ~7ms          |
| /users/{id} | ~12,500      | ~8ms          |

These numbers are with the native Zig HTTP server (24-thread pool) and include full Python handler dispatch through the middleware chain.
