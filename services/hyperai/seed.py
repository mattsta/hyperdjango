"""Seed data for HyperAI chat service."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.hyperai.models import (
    APIKey,
    Conversation,
    Message,
    MessageRole,
    Tier,
    User,
)


async def run(db=None) -> None:
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="admin").first()
    if existing:
        logger.info("  ai_users: already seeded")
        return

    # Admin user (password from HYPER_SEED_PASSWORD_ADMIN / HYPER_SEED_PASSWORD or random + printed)
    admin = User(
        username="admin",
        email="admin@hyperai.local",
        password_hash=hash_password(seed_password("admin")),
        tier=Tier.ENTERPRISE,
    )
    await admin.save()

    # Demo user (password from HYPER_SEED_PASSWORD_DEMO / HYPER_SEED_PASSWORD or random + printed)
    demo = User(
        username="demo",
        email="demo@example.com",
        password_hash=hash_password(seed_password("demo")),
        tier=Tier.PRO,
    )
    await demo.save()

    # Sample API key for demo user — generated via SignedAPIKeyMixin
    key_result = await APIKey.generate(user_id=demo.id, name="Demo Key")
    demo_key = key_result.raw_key

    # Seed conversation + messages
    conv = Conversation(
        user_id=demo.id,
        title="Getting Started with HyperDjango",
        model_name="hyper-4",
    )
    await conv.save()

    user_msg = Message(
        conversation_id=conv.id,
        role=MessageRole.USER,
        content="What is HyperDjango and how does it compare to Django?",
        token_count=12,
    )
    await user_msg.save()

    assistant_msg = Message(
        conversation_id=conv.id,
        role=MessageRole.ASSISTANT,
        content=(
            "HyperDjango extends Django with native Zig performance. It provides a compiled "
            "native HTTP server, SIMD validation, a native PostgreSQL driver (pg.zig), and a "
            "Zig-compiled template engine. You can use it as a Django extension (drop-in "
            "middleware, database backend, template engine) or as a standalone framework with "
            "its own HyperApp class."
        ),
        token_count=58,
    )
    await assistant_msg.save()

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info(
        "  admin user (enterprise tier) — see seed_password log for actual value"
    )
    logger.info("  demo user (pro tier) — see seed_password log for actual value")
    logger.info("  demo API key: {key}", key=demo_key)
    logger.info("  sample conversation seeded")
