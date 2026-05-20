"""Seed data for Full-Stack Task Manager."""

from hyperdjango.auth import seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import User
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.full_stack.app import Project, Task, TaskStatus


async def run(db=None) -> None:
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="demo").first()
    if existing:
        logger.info("  hyper_users: already seeded")
        return

    # Create user via RBAC system
    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Ensure "staff" and "superuser" groups exist (may already be created by hyper setup)
    staff_group = await checker.ensure_group("staff")
    await checker.ensure_group("superuser")

    # Create demo user and assign to staff group
    demo = await checker.create_user(
        "demo",
        seed_password("demo"),
        email="demo@example.com",
        is_staff=True,
    )
    await checker.add_user_to_group(demo.id, staff_group.id)
    logger.info(
        "  hyper_users: demo user created + staff group — see seed_password log for actual value"
    )

    # Demo project
    proj = Project(
        name="Getting Started",
        description="Your first project with HyperDjango",
        owner_id=demo.id,
    )
    await proj.save()
    logger.info("  fs_projects: sample project created")

    # Demo tasks
    tasks = [
        ("Set up development environment", TaskStatus.DONE),
        ("Read the documentation", TaskStatus.IN_PROGRESS),
        ("Build your first feature", TaskStatus.TODO),
    ]
    for title, status in tasks:
        t = Task(title=title, status=status, project_id=proj.id)
        await t.save()
    logger.info("  fs_tasks: {n} sample tasks created", n=len(tasks))
