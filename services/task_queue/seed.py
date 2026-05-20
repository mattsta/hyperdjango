"""
Task Queue seed data.

Creates:
- 1 admin user (password from HYPER_SEED_PASSWORD or random + printed)
- Empty task log table (tasks create log entries as they run)
"""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.task_queue.app import User


async def run(db=None):
    """Seed the task queue database."""
    existing = await User.objects.count()
    if existing > 0:
        logger.info("  Task queue already seeded. Skipping.")
        return

    logger.info("  Seeding task queue data...")
    admin = User(username="admin", password_hash=hash_password(seed_password("admin")))
    await admin.save()
    logger.info("    1 user created (admin) — see seed_password log for actual value")

    # HyperAdmin panel user (hyper_users table, separate from app users)
    if db is None:
        db = get_db()
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info("  Task queue seeded.")
