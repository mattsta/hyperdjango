"""Seed data for metering_api service."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.database import Database
from hyperdjango.metering import DimensionSpec, MeterEngine

from .app import Account


async def run(db: Database) -> None:
    # Admin user for HyperAdmin
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()

    # Demo accounts with different tiers
    accounts = [
        {
            "name": "Free User",
            "email": "free@example.com",
            "password_hash": hash_password(seed_password("free")),
            "tier": "free",
            "monthly_token_limit": 10000,
        },
        {
            "name": "Pro User",
            "email": "pro@example.com",
            "password_hash": hash_password(seed_password("pro")),
            "tier": "pro",
            "monthly_token_limit": 100000,
        },
        {
            "name": "Enterprise User",
            "email": "enterprise@example.com",
            "password_hash": hash_password(seed_password("enterprise")),
            "tier": "enterprise",
            "monthly_token_limit": 1000000,
        },
    ]

    for data in accounts:
        a = Account(**data)
        await a.save(db=db)

    # Pre-create metering tables + meter definition at seed time
    engine = MeterEngine(db)
    await engine.ensure_tables()
    await engine.define_meter(
        "llm_usage",
        [
            DimensionSpec("requests", "counter", "requests", "sum"),
            DimensionSpec("tokens_in", "counter", "tokens", "sum"),
            DimensionSpec("tokens_out", "counter", "tokens", "sum"),
            DimensionSpec("duration_ms", "gauge", "ms", "avg"),
        ],
        description="LLM API usage metering",
    )

    print(f"  Metering API seeded: {len(accounts)} accounts (free/pro/enterprise)")
