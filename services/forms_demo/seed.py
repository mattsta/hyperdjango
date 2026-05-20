"""Seed data for Forms Demo."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.forms_demo.app import Category, Priority, Ticket, User


async def run(db=None) -> None:
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="demo").first()
    if existing:
        logger.info("  forms_users: already seeded")
        return

    demo = User(
        username="demo",
        email="demo@example.com",
        password_hash=hash_password(seed_password("demo")),
    )
    await demo.save()
    logger.info("  forms_users: demo user created")

    # Seed some tickets
    tickets = [
        (
            "Login page broken",
            "Cannot log in on mobile",
            Category.BUG,
            Priority.HIGH,
            "user@example.com",
            True,
        ),
        (
            "Add dark mode",
            "Please add dark mode support",
            Category.FEATURE,
            Priority.NORMAL,
            "fan@example.com",
            False,
        ),
        (
            "How to deploy?",
            "What are the deployment steps?",
            Category.QUESTION,
            Priority.LOW,
            "new@example.com",
            False,
        ),
    ]
    for title, desc, cat, pri, email, urgent in tickets:
        t = Ticket(
            title=title,
            description=desc,
            category=cat,
            priority=pri,
            email=email,
            is_urgent=urgent,
            author_id=demo.id,
        )
        await t.save()
    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info("  forms_tickets: {n} tickets created", n=len(tickets))
