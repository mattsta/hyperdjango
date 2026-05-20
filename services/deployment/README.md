# Deployment

Production deployment reference with systemd service, nginx reverse proxy, health probes, and environment-based configuration.

## Quick Start

```bash
# 1. Build release binary
uv run hyper-build --install --release

# 2. Run diagnostics
uv run hyper doctor

# 3. Create tables and seed data
uv run hyper setup --app services.deployment.app:app --seed services.deployment.seed:run

# 4. Install as a systemd service (Linux) — registry-driven, companions
#    and PostgreSQL readiness gate included. --dry-run prints it first.
sudo uv run hyper service install deployment --enable --start

# 5. Verify
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Features

- All configuration via environment variables (no hardcoded secrets)
- Liveness probe (`/health`) that does not query the database
- Readiness probe (`/ready`) that verifies database connectivity
- Production middleware stack: HSTS (auto-enabled in production), CORS, rate limiting, security headers
- Graceful shutdown with connection draining
- Structured logging
- systemd unit file with security hardening
- nginx reverse proxy config with TLS, caching, and WebSocket support
- logrotate configuration for log management
- Environment template (`env.example`) documenting all variables

## Platform Features Demonstrated

- **Health probes** separated by purpose (liveness vs readiness)
- **SecurityHeadersMiddleware** with HSTS auto-enabled when DEBUG=0
- **RateLimitMiddleware** for production traffic shaping
- **SessionAuth** with secret from environment variable
- **CursorPagination** for API endpoints
- **hyper service install** for registry-driven systemd units (see
  [docs/running-services.md](../../docs/running-services.md))
- **hyper doctor** for deployment diagnostics

## Configuration

```bash
DATABASE_URL=postgres://user:pass@host/dbname
SECRET_KEY=your-production-secret-key
DEBUG=0
ALLOWED_ORIGINS=https://example.com,https://www.example.com
```

## API Endpoints

```
GET  /health            Liveness probe (no DB query)
GET  /ready             Readiness probe (verifies DB connection)
GET  /api/items/        List items (cursor-paginated)
POST /api/items/        Create item (auth required)
POST /auth/login        Session login
```

## Project Structure

```
deployment/
    app.py              Production-ready application
    seed.py             Sample data seeder
    env.example         Environment variable template
    nginx.conf          nginx reverse proxy configuration
    logrotate.conf      Log rotation configuration
    DEPLOY.md           Detailed deployment guide
```
