"""Seed data for deployment example."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.deployment.app import Item, ItemStatus, User


async def run(db=None):
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="admin").first()
    if existing:
        logger.info("  Deployment example already seeded. Skipping.")
        return

    logger.info("  Seeding deployment example...")
    admin = User(
        username="admin",
        password_hash=hash_password(seed_password("admin")),
    )
    await admin.save()

    for i in range(1, 11):
        item = Item(name=f"Sample Item {i}", status=ItemStatus.ACTIVE)
        await item.save()
    logger.info("    1 user, 10 items created")

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")
