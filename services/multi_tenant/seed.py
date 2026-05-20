"""Multi-Tenant SaaS seed data."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.multi_tenant.app import (
    Member,
    Org,
    Plan,
    Priority,
    Project,
    ProjectStatus,
    Role,
    Task,
    TaskStatus,
)

ORGS = [
    ("Acme Corp", "acme", Plan.PRO),
    ("Globex Inc", "globex", Plan.ENTERPRISE),
    ("Initech LLC", "initech", Plan.FREE),
]

PROJECTS_BY_ORG = {
    "acme": [
        ("Website Redesign", "Complete overhaul of the company website"),
        ("API v2", "Next-generation REST API"),
        ("Mobile App", "iOS and Android mobile application"),
        ("Data Pipeline", "Real-time data processing pipeline"),
    ],
    "globex": [
        ("World Domination", "Strategic growth initiative"),
        ("Product Launch", "Q3 product launch campaign"),
        ("Security Audit", "Annual security assessment"),
    ],
    "initech": [
        ("TPS Reports", "Automate TPS report generation"),
        ("Office Migration", "Move to new office infrastructure"),
        ("Legacy System", "Maintain legacy billing system"),
    ],
}

TASK_TEMPLATES = [
    ("Set up project structure", TaskStatus.TODO, Priority.HIGH),
    ("Design database schema", TaskStatus.TODO, Priority.HIGH),
    ("Implement authentication", TaskStatus.IN_PROGRESS, Priority.CRITICAL),
    ("Build REST API endpoints", TaskStatus.IN_PROGRESS, Priority.HIGH),
    ("Write unit tests", TaskStatus.TODO, Priority.NORMAL),
    ("Set up CI/CD pipeline", TaskStatus.TODO, Priority.NORMAL),
    ("Performance optimization", TaskStatus.TODO, Priority.LOW),
    ("Write documentation", TaskStatus.TODO, Priority.LOW),
    ("Code review", TaskStatus.IN_PROGRESS, Priority.NORMAL),
    ("Deploy to staging", TaskStatus.DONE, Priority.HIGH),
]


async def run(db=None):
    """Seed the multi-tenant database."""
    if db is None:
        db = get_db()

    existing = await Org.objects.count()
    if existing:
        logger.info("  Multi-tenant already seeded ({n} orgs). Skipping.", n=existing)
        return

    logger.info("  Seeding multi-tenant data...")

    # Create orgs
    org_map: dict[str, int] = {}
    for name, slug, plan in ORGS:
        org = Org(name=name, slug=slug, plan=plan)
        await org.save()
        org_map[slug] = org.id
    logger.info("    {n} orgs created", n=len(ORGS))

    # Create members for each org
    member_count = 0
    for slug, org_id in org_map.items():
        admin_member = Member(
            username=f"{slug}_admin",
            password_hash=hash_password(seed_password("admin")),
            role=Role.ADMIN,
            tenant_id=org_id,
        )
        await admin_member.save()
        regular_member = Member(
            username=f"{slug}_member",
            password_hash=hash_password(seed_password("member")),
            role=Role.MEMBER,
            tenant_id=org_id,
        )
        await regular_member.save()
        member_count += 2
    logger.info("    {n} members created", n=member_count)

    # Create projects and tasks for each org
    project_count = 0
    task_count = 0
    for slug, projects in PROJECTS_BY_ORG.items():
        org_id = org_map[slug]
        for proj_name, proj_desc in projects:
            proj = Project(
                name=proj_name,
                description=proj_desc,
                status=ProjectStatus.ACTIVE,
                tenant_id=org_id,
            )
            await proj.save()
            project_count += 1

            # Add tasks (varying count per project)
            num_tasks = min(len(TASK_TEMPLATES), 5 + (project_count % 3) * 2)
            for title, status, priority in TASK_TEMPLATES[:num_tasks]:
                task = Task(
                    project_id=proj.id,
                    title=title,
                    status=status,
                    priority=priority,
                    tenant_id=org_id,
                )
                await task.save()
                task_count += 1

    logger.info("    {p} projects, {t} tasks created", p=project_count, t=task_count)

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info(
        "  Multi-tenant seeded: {o} orgs, {m} members, {p} projects, {t} tasks",
        o=len(ORGS),
        m=member_count,
        p=project_count,
        t=task_count,
    )
