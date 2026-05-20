"""Seed data for REST API example."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.rest_api.app import User


async def run(db=None) -> None:
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="admin").first()
    if existing:
        logger.info("  rest_api: already seeded")
        return

    admin = User(
        username="admin",
        email="admin@example.com",
        password_hash=hash_password(seed_password("admin")),
        is_active=True,
    )
    await admin.save()
    logger.info(
        "  rest_api: admin user created — see seed_password log for actual value"
    )

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("  hyper_users: admin ensured for HyperAdmin panel")
