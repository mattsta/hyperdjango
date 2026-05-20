# Tuning Settings Reference

All tuning parameters are configurable via `get_setting()` — set through Django
settings (`HYPERDJANGO_*`), environment variables (`HYPER_*`), `.env` files, or
the `DEFAULTS` dict. Values are cached after first read (no per-request overhead).

## Task Queue

| Setting                      | Type | Default | Description                                     |
| ---------------------------- | ---- | ------- | ----------------------------------------------- |
| `TASK_WORKERS`               | int  | `4`     | Number of worker threads                        |
| `TASK_MAX_QUEUE_SIZE`        | int  | `10000` | Max pending tasks before rejection              |
| `TASK_DLQ_MAX_SIZE`          | int  | `10000` | Dead letter queue max size                      |
| `TASK_MAX_COMPLETED_RESULTS` | int  | `10000` | Completed task results retained before eviction |
| `TASK_CLEANUP_INTERVAL`      | int  | `100`   | Check result eviction every N task completions  |
| `TASK_SHUTDOWN_TIMEOUT`      | int  | `5`     | Seconds to wait for workers on shutdown         |

## Performance Middleware

| Setting                            | Type | Default | Description                                   |
| ---------------------------------- | ---- | ------- | --------------------------------------------- |
| `PERFORMANCE_HISTORY_SIZE`         | int  | `1000`  | Request history ring buffer size              |
| `PERFORMANCE_N_PLUS_ONE_THRESHOLD` | int  | `5`     | Repeated query count to trigger N+1 detection |

## Slow Query Log

| Setting                     | Type | Default | Description                                  |
| --------------------------- | ---- | ------- | -------------------------------------------- |
| `SLOW_QUERY_SQL_LENGTH`     | int  | `2000`  | SQL text truncation length in slow query log |
| `SLOW_QUERY_PARAMS_LENGTH`  | int  | `500`   | Parameter text truncation length             |
| `SLOW_QUERY_RETENTION_DAYS` | int  | `7`     | Days to retain entries before `cleanup()`    |

## Rate Limiting

| Setting                       | Type | Default | Description                                                                                  |
| ----------------------------- | ---- | ------- | -------------------------------------------------------------------------------------------- |
| `RATELIMIT_CLEANUP_RETENTION` | int  | `3600`  | Seconds to retain rate limit entries                                                         |
| `RATELIMIT_IETF_HEADERS`      | bool | `True`  | Emit IETF `RateLimit` + `RateLimit-Policy` headers (draft-ietf-httpapi-ratelimit-headers-10) |
| `RATELIMIT_LEGACY_HEADERS`    | bool | `True`  | Emit legacy `x-ratelimit-*` headers alongside IETF headers                                   |
| `RATELIMIT_PROBLEM_DETAILS`   | bool | `True`  | Use RFC 9457 Problem Details JSON format for 429 responses                                   |

## Hot Reload (Development)

| Setting                    | Type  | Default | Description                                |
| -------------------------- | ----- | ------- | ------------------------------------------ |
| `HOT_RELOAD`               | bool  | `False` | Enable file watching + browser reload      |
| `HOT_RELOAD_POLL_INTERVAL` | float | `0.3`   | Seconds between file polls (fallback mode) |
| `HOT_RELOAD_SSE_HEARTBEAT` | int   | `30`    | Seconds between SSE keepalive pings        |

## Asset Versioning

See [versioning.md](versioning.md) for full documentation.

| Setting                        | Type | Default    | Description                                                       |
| ------------------------------ | ---- | ---------- | ----------------------------------------------------------------- |
| `APP_VERSION`                  | str  | `""`       | Explicit version (git SHA, semver). Empty = auto-compute          |
| `APP_VERSION_HEADER`           | bool | `True`     | Emit `X-App-Version` + `X-App-Version-Action` on all responses    |
| `APP_VERSION_MISMATCH`         | str  | `"prompt"` | Stale-client policy: `"prompt"`, `"reload"`, `"warn"`, `"ignore"` |
| `APP_VERSION_CLIENT_BROADCAST` | bool | `True`     | Client sends `X-Client-Version` + `hyper_client_version` cookie   |
| `VERSION_ENDPOINT`             | bool | `True`     | Mount `/version` when `mount_version()` called                    |
| `STATIC_DEV_VERSION_QUERY`     | bool | `True`     | Append `?v=hash` in dev mode                                      |

`APP_VERSION_MISMATCH` is the operator policy advertised on every response as
`X-App-Version-Action`, so the serving backend decides what stale clients do —
flippable per deploy without redeploying any client code. `prompt` shows a
dismissible "new version available" banner with a Reload button; `reload` acts
at the next navigation boundary (never mid-interaction) and degrades to the
banner rather than looping; `warn` logs once per version; `ignore` injects no
mismatch script and, as a header, stands down already-instrumented clients. An
invalid value is a startup error.

`APP_VERSION_CLIENT_BROADCAST` gates the whole cohort-routing feature: the
injected `X-Client-Version` header on htmx requests, the `hyper_client_version`
cookie for full-page navigations, the inbound parse onto
`request.client_version`, and the
`hyperdjango_version_skew_requests_total{relation}` metric.

`CompressionMiddleware` must be registered **before** `VersionMiddleware` —
version injection has to see uncompressed bytes. The wrong order is a hard
startup error.

## Static Files

See [static-files.md](static-files.md) for full documentation.

| Setting                               | Type | Default    | Description                     |
| ------------------------------------- | ---- | ---------- | ------------------------------- |
| `STATIC_URL`                          | str  | `/static/` | URL prefix                      |
| `STATIC_ROOT`                         | str  | `""`       | Collected output directory      |
| `STATIC_MAX_AGE`                      | int  | `3600`     | Cache-Control max-age (seconds) |
| `STATICFILES_DIRS`                    | list | `[]`       | Additional source directories   |
| `STATICFILES_GZIP_MIN_SIZE`           | int  | `1024`     | Min bytes before gzip           |
| `STATICFILES_HASH_LENGTH`             | int  | `12`       | Hex chars in hash filenames     |
| `STATICFILES_MAX_POST_PROCESS_PASSES` | int  | `5`        | CSS rewrite iterations          |
| `STATICFILES_DEV_HASH_CACHE_MAX`      | int  | `4096`     | Dev hash cache entries          |

## Authentication

### request.user Types

`request.user` is always one of four types:

| Type            | When                                            | Properties                                                                                                                                               |
| --------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `None`          | No auth middleware                              | N/A                                                                                                                                                      |
| `AnonymousUser` | SessionAuth, no valid session                   | `.is_authenticated = False`, `.id = None`                                                                                                                |
| `SessionUser`   | SessionAuth, valid login session                | Full model: `.id`, `.username`, `.email`, `.is_staff`, `.is_superuser`, `.has_perm()`                                                                    |
| `SessionUser`   | SessionAuth, valid session (from session store) | Same API as User: `.id`, `.username`, `.is_authenticated = True`, `.is_staff`, `.has_perm()`. Also supports dict access: `user["id"]`, `user.get("key")` |

`SessionUser` wraps the session dict with User-like properties. It is NOT a dict
and should never be serialized directly into API responses. Endpoints should
construct explicit response dicts:

```python
# WRONG: dumps internal auth object
return {"user": request.user}

# RIGHT: explicit response shape
return {"user": {"id": request.user.id, "username": request.user.username}}
```

### Auth Settings

| Setting                      | Type | Default | Description                                                        |
| ---------------------------- | ---- | ------- | ------------------------------------------------------------------ |
| `SECRET_KEY`                 | str  | `""`    | **Required in production**. Signing key for sessions, CSRF, tokens |
| `SESSION_SAVE_EVERY_REQUEST` | bool | `False` | Re-set cookie on every request to extend lifetime                  |

## Telemetry

See CLAUDE.md for full telemetry settings documentation.

| Setting                          | Type  | Default          | Description                           |
| -------------------------------- | ----- | ---------------- | ------------------------------------- |
| `TELEMETRY_ENABLED`              | bool  | `False`          | Master switch (zero cost when off)    |
| `TELEMETRY_SERVICE_NAME`         | str   | `"hyperdjango"`  | Attached to every root span           |
| `TELEMETRY_SAMPLE_RATIO`         | float | `0.01`           | Head sampling rate 0.0-1.0            |
| `TELEMETRY_DRAIN_INTERVAL`       | float | `1.0`            | Seconds between background drains     |
| `TELEMETRY_SINKS`                | list  | `["prometheus"]` | Export targets                        |
| `TELEMETRY_SPAN_RING_CAPACITY`   | int   | `16384`          | MPSC ring buffer slots (power of 2)   |
| `TELEMETRY_AUTO_LOG_CORRELATION` | bool  | `True`           | Auto-inject trace_id into log records |

## Database / Pool

| Setting                | Type | Default | Description                                                                                                        |
| ---------------------- | ---- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| `POOL_SIZE`            | int  | `0`     | Connection pool size (0 = auto-tune)                                                                               |
| `PREPARED_STATEMENTS`  | bool | `True`  | RESERVED — not yet honored by the native pool (no effect)                                                          |
| `STATEMENT_CACHE_SIZE` | int  | `256`   | RESERVED — not yet honored by the native pool (no effect)                                                          |
| `CONNECT_TIMEOUT`      | int  | `10000` | Connection timeout in milliseconds                                                                                 |
| `QUERY_TIMEOUT`        | int  | `0`     | Statement timeout (0 = unlimited)                                                                                  |
| `POOL_MAX_QUERIES`     | int  | `10000` | Rotate connections after N queries                                                                                 |
| `POOL_MAX_LIFETIME`    | int  | `3600`  | Max connection age in seconds                                                                                      |
| `DB_OFFLOAD_WORKERS`   | int  | `0`     | Max concurrent DB round-trips offloaded off a multiplexing loop (0 = auto min(cpu,8)); folded into the pool budget |
