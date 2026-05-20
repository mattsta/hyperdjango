# Metering API

LLM-style API with **usage metering**, **quota enforcement**, and **IETF rate limit headers**.

## Features

- Multi-dimensional usage metering (`metering.py`): requests, tokens_in, tokens_out, duration_ms
- Monthly quota enforcement with tier-based limits
- IETF RateLimit + RateLimit-Policy headers (draft-ietf-httpapi-ratelimit-headers-10)
- RFC 9457 Problem Details on 429 responses
- Per-user rate limiting (10 req/min)
- Usage dashboard API
- HyperAdmin panel
- Swagger UI at `/docs/`

## Setup

```bash
cd services/metering_api
uv run hyper setup --app services.metering_api.app:app --drop --seed services.metering_api.seed:run
uv run hyper run --app services.metering_api.app:app --port 8770
```

## Credentials

Passwords are dynamically generated via `seed_password()`. Set `HYPER_SEED_PASSWORD` env var or check the seed output.

| Account    | Email                  | Tier       | Monthly Limit    |
| ---------- | ---------------------- | ---------- | ---------------- |
| Free       | free@example.com       | free       | 10,000 tokens    |
| Pro        | pro@example.com        | pro        | 100,000 tokens   |
| Enterprise | enterprise@example.com | enterprise | 1,000,000 tokens |

## Key Routes

| Route                      | Description                        |
| -------------------------- | ---------------------------------- |
| `POST /auth/login`         | Login (email + password)           |
| `POST /api/v1/completions` | Simulated LLM completion (metered) |
| `GET /api/v1/usage`        | Usage report (current month)       |
| `GET /api/v1/usage/quota`  | Quota status                       |
| `GET /admin/`              | Admin panel                        |
| `GET /docs/`               | Swagger UI                         |

## IETF Rate Limit Headers

Every response includes:

```
RateLimit-Policy: "api-minute";q=10;w=60
RateLimit: "api-minute";r=8;t=45
```

On 429:

```json
{
  "type": "https://iana.org/assignments/http-problem-types#quota-exceeded",
  "title": "Rate limit exceeded",
  "status": 429,
  "violated-policies": ["api-minute"]
}
```
