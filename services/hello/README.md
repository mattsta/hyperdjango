# Hello World

The simplest HyperDjango app -- two routes, no database, no middleware.

## Run

```bash
uv run python -m services.hello.app
```

## Routes

| Method | Path            | Response                                 |
| ------ | --------------- | ---------------------------------------- |
| GET    | `/`             | `{"message": "Hello from HyperDjango!"}` |
| GET    | `/greet/{name}` | `{"greeting": "Hello, {name}!"}`         |

## What it demonstrates

- `HyperApp` constructor
- Route decorators (`@app.get`)
- Path parameters (`{name}`)
- Automatic dict-to-JSON response
- Native Zig HTTP server (24-thread pool)
