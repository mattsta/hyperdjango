# Production Deployment

Step-by-step guide to deploying a HyperDjango app with systemd + nginx on a Linux server.

## Prerequisites

- Ubuntu 22.04+ or similar Linux distro
- PostgreSQL 15+
- Python 3.14+ (free-threaded build)
- nginx
- uv (Python package manager)

## 1. Build Release

```bash
uv run hyper-build --install --release
```

This compiles the Zig native extension with `ReleaseFast` optimizations. Never deploy a debug build.

## 2. Run Diagnostics

```bash
uv run hyper doctor
```

Runs 30 checks across build, database, security, performance. Fix any failures before deploying.

For CI pipelines:

```bash
uv run hyper doctor --json --ci
```

## 3. Configure Environment

```bash
cp env.example .env
# Edit .env with production values:
#   SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
#   DATABASE_URL=postgres://user:pass@dbhost:5432/mydb
#   DEBUG=0
#   ALLOWED_ORIGINS=https://myapp.com
```

## 4. Create Database Tables

```bash
uv run hyper setup --app services.deployment.app:app --seed services.deployment.seed:run
```

## 5. Install systemd Service

```bash
# Inspect the units and commands first — writes nothing:
uv run hyper service install deployment --dry-run --enable --start

# Generate, install, enable and start (also runs `hyper setup` + seed):
sudo uv run hyper service install deployment --enable --start
```

Every field comes from the service registry, so the unit cannot drift from the
code. The generated unit includes:

- Graceful shutdown (SIGTERM → 30s drain → SIGKILL)
- Auto-restart on failure (5s delay), with `StartLimitIntervalSec`/`Burst`
  tuned so a boot-time database race retries into success
- A `pg_isready` `ExecStartPre` gate — `After=postgresql.service` orders start
  jobs, it does **not** mean the database is accepting connections
- The PostgreSQL unit actually installed on the host (`postgresql.service` vs
  `postgresql@18-main.service`), and none at all for a remote database
- Security hardening (PrivateTmp, ProtectSystem, NoNewPrivileges)
- Resource limits (65536 open files)
- journald logging under a per-service `SyslogIdentifier`
- Membership of `hyperdjango.target`, so several installed services start,
  stop and restart as one set
- **No `ExecReload`** — the server has no SIGHUP reload path, so these units are
  restart-only and `systemctl reload` fails loudly instead of lying

Full reference: [docs/running-services.md](../../docs/running-services.md).

## 6. Configure nginx

```bash
sudo cp nginx.conf /etc/nginx/sites-available/myapp
sudo ln -s /etc/nginx/sites-available/myapp /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Edit `nginx.conf` to set your `server_name`.

## 7. TLS with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d myapp.com
```

Certbot auto-configures nginx for HTTPS and sets up certificate renewal.

## 8. Verify

```bash
# Service status
sudo systemctl status hyperdjango-deployment

# Health check
curl http://localhost:8000/health

# Readiness check (verifies DB connectivity)
curl http://localhost:8000/ready

# Through nginx
curl https://myapp.com/health

# Logs
journalctl -u hyperdjango-deployment -f
```

## 9. Log Rotation (optional)

For file-based logging with `hyper start`:

```bash
sudo cp logrotate.conf /etc/logrotate.d/hyperdjango
```

When using systemd, logs go to journald automatically. Use `journalctl` to query.

## Management Commands

```bash
# Restart after code changes
sudo systemctl restart hyperdjango-deployment

# Stop
sudo systemctl stop hyperdjango-deployment

# View recent logs
journalctl -u hyperdjango-deployment --since "1 hour ago"

# Run doctor on deployed app
uv run hyper doctor --database "$DATABASE_URL"

# Uninstall service
uv run hyper systemd uninstall
```

## Files in This Example

| File             | Purpose                                  |
| ---------------- | ---------------------------------------- |
| `app.py`         | Production-ready reference application   |
| `seed.py`        | Database seed data                       |
| `env.example`    | Environment variable template            |
| `nginx.conf`     | nginx reverse proxy with WebSocket + TLS |
| `logrotate.conf` | Log rotation for file-based logging      |
| `DEPLOY.md`      | This guide                               |
