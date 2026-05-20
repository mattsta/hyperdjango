"""
Metering API — Usage Metering, Quotas, and IETF Rate Limits Example.

Showcases platform features not covered by other services:

  - **Usage Metering** (metering.py): MeterEngine with multi-dimensional tracking
    (requests, tokens_in, tokens_out, duration_ms)
  - **Quota Enforcement** (metering.py): per-account monthly quota with WARN/REJECT
  - **IETF Rate Limit Headers** (ratelimit.py): RateLimit-Policy + RateLimit headers
  - **Rate Limit Client** (ratelimit_client.py): demonstrated in E2E tests

Simulates an LLM API provider with:
  - Per-account usage metering (token counts, request counts, durations)
  - Monthly quota enforcement (free tier: 10K tokens/month)
  - Per-minute rate limiting with IETF headers
  - Usage dashboard API

Run:
    uv run hyper setup --app services.metering_api.app:app --seed services.metering_api.seed:run
    uv run hyper run --app services.metering_api.app:app --port 8770

Endpoints:
    POST /api/v1/completions     → Simulated LLM completion (metered)
    GET  /api/v1/usage           → Usage report for current account
    GET  /api/v1/usage/quota     → Quota status
    GET  /health                 → Health check
    GET  /admin/                 → HyperAdmin panel
    GET  /docs/                  → Swagger UI
"""

import random

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth import verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import get_setting
from hyperdjango.database import get_db
from hyperdjango.metering import DimensionSpec, MeterEngine, set_meter_engine
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.signing import SigningKey, TokenEngine

# ─── Models ───────────────────────────────────────────────────────────────────


class Account(TimestampMixin, Model):
    class Meta:
        table = "mt_accounts"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    email: str = Field(max_length=200)
    password_hash: str = Field(max_length=200, exclude=True)
    tier: str = Field(max_length=20, default="free")
    monthly_token_limit: int = Field(default=10000)
    is_active: bool = Field(default=True)


# ─── App Setup ────────────────────────────────────────────────────────────────

app = HyperApp(
    title="Metering API",
    database=get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test",
)

token_engine = TokenEngine(
    keys=[SigningKey(secret=get_setting("SESSION_SIGNING_KEY"), version=1)]
)
auth = SessionAuth(secret=get_setting("SESSION_SECRET"), token_engine=token_engine)
app.use(auth)

# IETF-compliant rate limiting (60 req/min per user)
from datetime import UTC

from hyperdjango.ratelimit import RateLimitMiddleware

app.use(RateLimitMiddleware(max_requests=60, window=60, policy_name="api-minute"))

admin = HyperAdmin(
    app, prefix="/admin", title="Metering Admin", secret_key=get_setting("ADMIN_SECRET")
)
admin.register(
    Account,
    list_display=["id", "name", "email", "tier", "monthly_token_limit"],
    search_fields=["name", "email"],
)

# Metering engine — initialized at startup
_meter_engine: MeterEngine | None = None


# Metering setup deferred to first request — on_startup runs in a
# different context than the Zig server's worker threads, so we
# initialize lazily instead.
_metering_initialized = False


async def _ensure_metering(db):
    global _meter_engine, _metering_initialized
    if _metering_initialized:
        return
    _metering_initialized = True
    _meter_engine = MeterEngine(db)
    await _meter_engine.ensure_tables()
    set_meter_engine(_meter_engine)
    await _meter_engine.define_meter(
        "llm_usage",
        [
            DimensionSpec("requests", "counter", "requests", "sum"),
            DimensionSpec("tokens_in", "counter", "tokens", "sum"),
            DimensionSpec("tokens_out", "counter", "tokens", "sum"),
            DimensionSpec("duration_ms", "gauge", "ms", "avg"),
        ],
        description="LLM API usage metering",
    )


# ─── Auth ─────────────────────────────────────────────────────────────────────


@app.post("/auth/login")
async def login(request):
    data = await request.json()
    email = data.get("email", "")
    password = data.get("password", "")
    if not email or not password:
        raise HTTPException(400, "email and password required")

    account = await Account.objects.filter(email=email).first()
    if account is None or not verify_password(password, account.password_hash):
        raise HTTPException(401, "Invalid credentials")

    session = await build_session_data(
        account.id, get_db(), username=account.name, groups=["user"]
    )
    resp = Response.json({"id": account.id, "name": account.name, "tier": account.tier})
    auth.login(resp, session)
    return resp


@app.post("/auth/logout")
async def logout(request):
    resp = Response.json({"ok": True})
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


# ─── Completions API (metered) ────────────────────────────────────────────────


@app.post("/api/v1/completions")
async def completions(request):
    """Simulated LLM completion endpoint. Records usage via MeterEngine."""
    user = request.user
    if user is None or not user.is_authenticated:
        raise HTTPException(401, "Authentication required")

    data = await request.json()
    prompt = data.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "prompt is required")

    # Simulate token counts and processing
    tokens_in = len(prompt.split())
    tokens_out = random.randint(10, 100)
    duration_ms = random.randint(50, 500)
    account_id = str(user.id)

    # Record metering event
    await _ensure_metering(get_db())
    if _meter_engine is not None:
        await _meter_engine.record(
            "llm_usage",
            account_id,
            {
                "requests": 1,
                "tokens_in": float(tokens_in),
                "tokens_out": float(tokens_out),
                "duration_ms": float(duration_ms),
            },
        )

    # Simulated response
    response_text = f"Simulated completion for: {prompt[:50]}... ({tokens_out} tokens)"

    return Response.json(
        {
            "text": response_text,
            "usage": {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "total_tokens": tokens_in + tokens_out,
                "duration_ms": duration_ms,
            },
        }
    )


# ─── Usage API ────────────────────────────────────────────────────────────────


@app.get("/api/v1/usage")
async def usage_report(request):
    """Get usage report for the current account."""
    user = request.user
    if user is None or not user.is_authenticated:
        raise HTTPException(401, "Authentication required")

    account_id = str(user.id)

    await _ensure_metering(get_db())
    if _meter_engine is None:
        return Response.json({"error": "Metering not initialized"}, status=503)

    # Query current month's usage
    from datetime import datetime

    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    report = await _meter_engine.query_multi(
        "llm_usage",
        account_id,
        ["requests", "tokens_in", "tokens_out", "duration_ms"],
        period="monthly",
        start=start,
        end=now,
    )

    dimensions: dict[str, float] = {}
    if report:
        for dim_name, agg in report.items():
            if agg is None:
                continue
            if dim_name == "duration_ms":
                dimensions[dim_name] = agg.value_avg
            else:
                dimensions[dim_name] = agg.value_sum

    return Response.json(
        {
            "account_id": account_id,
            "period": "monthly",
            "start": start.isoformat(),
            "end": now.isoformat(),
            "usage": dimensions,
        }
    )


@app.get("/api/v1/usage/quota")
async def quota_status(request):
    """Check quota status for the current account."""
    user = request.user
    if user is None or not user.is_authenticated:
        raise HTTPException(401, "Authentication required")

    account = await Account.objects.filter(id=user.id).first()
    if account is None:
        raise HTTPException(404, "Account not found")

    account_id = str(user.id)
    limit = account.monthly_token_limit

    # Get current usage
    total_tokens = 0.0
    await _ensure_metering(get_db())
    if _meter_engine is not None:
        from datetime import datetime

        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        report = await _meter_engine.query_multi(
            "llm_usage",
            account_id,
            ["tokens_in", "tokens_out"],
            period="monthly",
            start=start,
            end=now,
        )
        if report:
            for agg in report.values():
                if agg is not None:
                    total_tokens += agg.value_sum

    remaining = max(0, limit - total_tokens)
    pct_used = (total_tokens / limit * 100) if limit > 0 else 0

    return Response.json(
        {
            "account_id": account_id,
            "tier": account.tier,
            "monthly_limit": limit,
            "used": int(total_tokens),
            "remaining": int(remaining),
            "percent_used": round(pct_used, 1),
            "status": "ok"
            if pct_used < 80
            else ("warning" if pct_used < 100 else "exceeded"),
        }
    )


@app.get("/health")
async def health(request):
    return Response.json({"status": "ok"})


@app.get("/")
async def root(request):
    return Response.redirect("/docs/")


mount_docs(app)
