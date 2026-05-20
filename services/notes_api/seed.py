"""Notes API seed data."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.expressions import F
from hyperdjango.logging import logger
from services.notes_api.app import Category, Note, User

CATEGORIES = ["Work", "Personal", "Ideas", "Learning", "Projects"]

NOTES = [
    ("Set up CI pipeline", "Configure GitHub Actions for automated testing.", "Work"),
    ("Grocery list", "Milk, eggs, bread, avocados, coffee beans.", "Personal"),
    (
        "App idea: habit tracker",
        "Track daily habits with streaks and reminders.",
        "Ideas",
    ),
    (
        "Learn Zig basics",
        "Comptime, allocators, error unions, SIMD vectors.",
        "Learning",
    ),
    (
        "HyperDjango notes app",
        "Build intermediate example showcasing ORM + auth + admin.",
        "Projects",
    ),
    ("Code review feedback", "Improve error handling in the payment service.", "Work"),
    (
        "Weekend hike plan",
        "Trail: Mt. Wilson via Chantry Flat. Bring water + sunscreen.",
        "Personal",
    ),
    (
        "Blog post draft",
        "Why PostgreSQL full-text search is enough for 90% of apps.",
        "Ideas",
    ),
    (
        "Read DDIA chapter 5",
        "Replication: leaders, followers, multi-leader, leaderless.",
        "Learning",
    ),
    (
        "Migrate raw SQL to ORM",
        "Replace db.execute with QuerySet operations for type safety.",
        "Projects",
    ),
]


async def run(db=None):
    """Seed the notes database."""
    existing = await Note.objects.count()
    if existing:
        logger.info("  Notes already seeded ({n} notes). Skipping.", n=existing)
        return

    logger.info("  Seeding notes data...")

    # Users
    admin = User(username="admin", password_hash=hash_password(seed_password("admin")))
    await admin.save()
    alice = User(username="alice", password_hash=hash_password(seed_password("alice")))
    await alice.save()
    logger.info(
        "    2 users created (admin, alice) — see seed_password log for actual values"
    )

    # Categories
    cat_map: dict[str, int] = {}
    for name in CATEGORIES:
        cat = Category(name=name)
        await cat.save()
        cat_map[name] = cat.id
    logger.info("    {n} categories created", n=len(CATEGORIES))

    # Notes
    authors = [admin, alice]
    for i, (title, body, cat_name) in enumerate(NOTES):
        note = Note(
            title=title,
            body=body,
            author_id=authors[i % 2].id,
            category_id=cat_map[cat_name],
        )
        await note.save()
        await Category.objects.filter(id=cat_map[cat_name]).update(
            note_count=F("note_count") + 1,
        )

    logger.info("    {n} notes created", n=len(NOTES))

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.success(
        "  Notes seeded: {u} users, {c} categories, {n} notes",
        u=2,
        c=len(CATEGORIES),
        n=len(NOTES),
    )
