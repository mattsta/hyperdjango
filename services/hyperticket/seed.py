"""
HyperTicket — Seed data for 2 organizations.

Creates:
  - 3 PlanConfigs (Starter, Professional, Enterprise) with feature limits
  - 2 Orgs (Acme Corp on Professional, Globex Inc on Enterprise)
  - Default workflow configs per org (statuses, priorities, types, transitions)
  - Agents + customers per org
  - Teams + memberships
  - 50 sample tickets across both orgs
  - Comments, tags, SLA policies

Usage:
    uv run hyper setup --app services.hyperticket.app:app --seed services.hyperticket.seed:run
"""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.logging import logger
from hyperdjango.tenancy import tenant_context

from .models import (
    Agent,
    AgentRole,
    AgentSkill,
    AssignmentStrategy,
    AuthorType,
    Comment,
    Customer,
    EscalationRule,
    EscalationTrigger,
    Org,
    OrgSettings,
    PlanConfig,
    PlanFeatureLimit,
    PriorityConfig,
    QuotaEnforcement,
    SLAPolicy,
    StatusCategory,
    StatusTransition,
    Tag,
    Team,
    TeamMembership,
    TenantTheme,
    Ticket,
    TicketSource,
    TicketStatusConfig,
    TicketTag,
    TicketTypeConfig,
)

# ---------------------------------------------------------------------------
# Plan definitions (multi-dimensional — NOT hardcoded tiers)
# ---------------------------------------------------------------------------

_PLANS = [
    ("Starter", "Basic plan for small teams", True, 0, 0),
    ("Professional", "Full-featured plan for growing teams", True, 1, 4900),
    ("Enterprise", "Unlimited plan with premium support", True, 2, 14900),
]

_FEATURE_LIMITS: dict[str, list[tuple[str, float, str]]] = {
    "Starter": [
        ("agent_seats", 3, "reject"),
        ("tickets_per_month", 100, "warn"),
        ("storage_bytes", 1_073_741_824, "reject"),  # 1 GB
        ("ai_tokens_per_month", 0, "reject"),
        ("sso_enabled", 0, "reject"),
        ("custom_fields_count", 5, "reject"),
        ("api_requests_per_month", 10_000, "throttle"),
        ("webhook_count", 2, "reject"),
        ("audit_retention_days", 30, "soft"),
    ],
    "Professional": [
        ("agent_seats", 25, "reject"),
        ("tickets_per_month", 5000, "warn"),
        ("storage_bytes", 10_737_418_240, "reject"),  # 10 GB
        ("ai_tokens_per_month", 100_000, "throttle"),
        ("sso_enabled", 1, "reject"),
        ("custom_fields_count", 50, "reject"),
        ("api_requests_per_month", 100_000, "throttle"),
        ("webhook_count", 20, "reject"),
        ("audit_retention_days", 365, "soft"),
    ],
    "Enterprise": [
        ("agent_seats", -1, "soft"),  # unlimited
        ("tickets_per_month", -1, "soft"),
        ("storage_bytes", -1, "soft"),
        ("ai_tokens_per_month", -1, "soft"),
        ("sso_enabled", 1, "soft"),
        ("custom_fields_count", -1, "soft"),
        ("api_requests_per_month", -1, "soft"),
        ("webhook_count", -1, "soft"),
        ("audit_retention_days", -1, "soft"),
    ],
}

# ---------------------------------------------------------------------------
# Default workflow configs (statuses, priorities, types)
# ---------------------------------------------------------------------------

_DEFAULT_STATUSES = [
    ("open", "Open", "#22c55e", StatusCategory.OPEN, True, False, False, 0),
    (
        "in_progress",
        "In Progress",
        "#3b82f6",
        StatusCategory.OPEN,
        False,
        False,
        False,
        1,
    ),
    (
        "waiting",
        "Waiting on Customer",
        "#f59e0b",
        StatusCategory.PENDING,
        False,
        False,
        True,
        2,
    ),
    ("on_hold", "On Hold", "#6b7280", StatusCategory.PENDING, False, False, True, 3),
    ("resolved", "Resolved", "#8b5cf6", StatusCategory.SOLVED, False, False, False, 4),
    ("closed", "Closed", "#64748b", StatusCategory.CLOSED, False, True, False, 5),
    ("reopened", "Reopened", "#ef4444", StatusCategory.OPEN, False, False, False, 6),
]

_DEFAULT_PRIORITIES = [
    ("critical", "Critical", "#ef4444", "", 0.25, 0, False),
    ("high", "High", "#f59e0b", "", 0.5, 1, False),
    ("normal", "Normal", "#3b82f6", "", 1.0, 2, True),
    ("low", "Low", "#6b7280", "", 2.0, 3, False),
]

_DEFAULT_TYPES = [
    ("bug", "Bug", "#ef4444", "", "Something is broken"),
    ("feature", "Feature Request", "#8b5cf6", "", "Request for new functionality"),
    ("question", "Question", "#3b82f6", "", "General inquiry"),
    ("task", "Task", "#22c55e", "", "Internal task"),
    ("incident", "Incident", "#f97316", "", "Service outage or degradation"),
]

# Allowed transitions: (from_slug, to_slug, requires_role, requires_comment)
_DEFAULT_TRANSITIONS = [
    ("open", "in_progress", "agent", False),
    ("open", "waiting", "agent", False),
    ("open", "closed", "agent", True),
    ("in_progress", "waiting", "agent", False),
    ("in_progress", "on_hold", "agent", True),
    ("in_progress", "resolved", "agent", False),
    ("waiting", "in_progress", "agent", False),
    ("waiting", "resolved", "agent", False),
    ("on_hold", "in_progress", "agent", False),
    ("on_hold", "closed", "team_lead", True),
    ("resolved", "closed", "agent", False),
    ("resolved", "reopened", "agent", False),
    ("closed", "reopened", "agent", True),
    ("reopened", "in_progress", "agent", False),
    ("reopened", "closed", "agent", True),
]


async def _ensure_workflow_configs(tenant_id: int) -> dict[str, dict[str, int]]:
    """Create default statuses, priorities, types, and transitions for an org.

    Returns {"statuses": {slug: id}, "priorities": {slug: id}, "types": {slug: id}}.
    """
    status_ids: dict[str, int] = {}
    for (
        slug,
        label,
        color,
        category,
        is_default,
        is_terminal,
        pauses_sla,
        sort_order,
    ) in _DEFAULT_STATUSES:
        existing = await TicketStatusConfig.objects.filter(slug=slug).first()
        if existing:
            status_ids[slug] = existing.id
            continue
        s = TicketStatusConfig(
            tenant_id=tenant_id,
            slug=slug,
            label=label,
            color=color,
            category=category,
            is_default=is_default,
            is_terminal=is_terminal,
            pauses_sla=pauses_sla,
            sort_order=sort_order,
        )
        await s.save()
        status_ids[slug] = s.id

    # Status transitions
    for from_slug, to_slug, role, req_comment in _DEFAULT_TRANSITIONS:
        from_id = status_ids.get(from_slug, 0)
        to_id = status_ids.get(to_slug, 0)
        if not from_id or not to_id:
            continue
        existing = await StatusTransition.objects.filter(
            from_status_id=from_id, to_status_id=to_id
        ).first()
        if not existing:
            await StatusTransition(
                tenant_id=tenant_id,
                from_status_id=from_id,
                to_status_id=to_id,
                requires_role=role,
                requires_comment=req_comment,
            ).save()

    priority_ids: dict[str, int] = {}
    for (
        slug,
        label,
        color,
        icon,
        multiplier,
        sort_order,
        is_default,
    ) in _DEFAULT_PRIORITIES:
        existing = await PriorityConfig.objects.filter(slug=slug).first()
        if existing:
            priority_ids[slug] = existing.id
            continue
        p = PriorityConfig(
            tenant_id=tenant_id,
            slug=slug,
            label=label,
            color=color,
            icon=icon,
            sla_multiplier=multiplier,
            sort_order=sort_order,
            is_default=is_default,
        )
        await p.save()
        priority_ids[slug] = p.id

    type_ids: dict[str, int] = {}
    for slug, label, color, icon, description in _DEFAULT_TYPES:
        existing = await TicketTypeConfig.objects.filter(slug=slug).first()
        if existing:
            type_ids[slug] = existing.id
            continue
        t = TicketTypeConfig(
            tenant_id=tenant_id,
            slug=slug,
            label=label,
            color=color,
            icon=icon,
            description=description,
        )
        await t.save()
        type_ids[slug] = t.id

    return {"statuses": status_ids, "priorities": priority_ids, "types": type_ids}


async def _seed_org(
    org_name: str,
    org_slug: str,
    plan_id: int,
    agent_specs: list[tuple[str, str, str, str]],
    customer_specs: list[tuple[str, str]],
    team_specs: list[tuple[str, str, str]],
    ticket_count: int,
) -> None:
    """Seed a complete org with agents, customers, teams, and tickets."""
    # Org
    existing_org = await Org.objects.filter(slug=org_slug).first()
    if existing_org:
        logger.info("  org {slug} already seeded", slug=org_slug)
        return
    org = Org(name=org_name, slug=org_slug, plan_config_id=plan_id)
    await org.save()
    tenant_id = org.id
    logger.info("  org: {name} (id={id})", name=org_name, id=tenant_id)

    with tenant_context(tenant_id=tenant_id):
        # Workflow configs
        wf = await _ensure_workflow_configs(tenant_id)
        logger.info(
            "    {s} statuses, {p} priorities, {t} types",
            s=len(wf["statuses"]),
            p=len(wf["priorities"]),
            t=len(wf["types"]),
        )

        # OrgSettings
        await OrgSettings(
            tenant_id=tenant_id,
            timezone="America/New_York" if org_slug == "acme" else "Europe/London",
            auto_assignment_strategy=AssignmentStrategy.ROUND_ROBIN,
        ).save()

        # TenantTheme
        if org_slug == "acme":
            await TenantTheme(
                tenant_id=tenant_id,
                company_name_display="Acme Corp",
                primary_color="#2563eb",
                accent_color="#16a34a",
            ).save()
        else:
            await TenantTheme(
                tenant_id=tenant_id,
                company_name_display="Globex Inc",
                primary_color="#7c3aed",
                accent_color="#ea580c",
            ).save()

        # Agents
        agent_ids: dict[str, int] = {}
        for email, display_name, role_str, password in agent_specs:
            role = AgentRole(role_str)
            a = Agent(
                tenant_id=tenant_id,
                email=email,
                display_name=display_name,
                password_hash=hash_password(password),
                role=role,
            )
            await a.save()
            agent_ids[email] = a.id
        logger.info("    {n} agents", n=len(agent_ids))

        # Customers
        customer_ids: dict[str, int] = {}
        for email, display_name in customer_specs:
            c = Customer(
                tenant_id=tenant_id,
                email=email,
                display_name=display_name,
                password_hash=hash_password(seed_password("customer")),
                is_verified=True,
            )
            await c.save()
            customer_ids[email] = c.id
        logger.info("    {n} customers", n=len(customer_ids))

        # Teams
        team_ids: dict[str, int] = {}
        for name, slug, lead_email in team_specs:
            lead_id = agent_ids.get(lead_email, 0)
            t = Team(
                tenant_id=tenant_id,
                name=name,
                slug=slug,
                lead_agent_id=lead_id,
            )
            await t.save()
            team_ids[slug] = t.id
            # Add lead as primary member
            if lead_id:
                await TeamMembership(
                    tenant_id=tenant_id,
                    team_id=t.id,
                    agent_id=lead_id,
                    is_primary=True,
                ).save()
        logger.info("    {n} teams", n=len(team_ids))

        # Tags
        tag_ids: dict[str, int] = {}
        for tag_name, color in [
            ("billing", "#f59e0b"),
            ("login", "#3b82f6"),
            ("performance", "#ef4444"),
            ("feature-request", "#8b5cf6"),
            ("documentation", "#22c55e"),
            ("mobile", "#06b6d4"),
            ("api", "#ec4899"),
            ("security", "#dc2626"),
        ]:
            tag = Tag(tenant_id=tenant_id, name=tag_name, color=color)
            await tag.save()
            tag_ids[tag_name] = tag.id
        logger.info("    {n} tags", n=len(tag_ids))

        # Agent skills
        agent_list = list(agent_ids.values())
        skill_tags = ["billing", "login", "performance", "api", "security"]
        for i, aid in enumerate(agent_list):
            # Each agent gets 2-3 skills
            for j in range(min(3, len(skill_tags))):
                idx = (i + j) % len(skill_tags)
                await AgentSkill(
                    tenant_id=tenant_id,
                    agent_id=aid,
                    skill_tag=skill_tags[idx],
                    proficiency=3 + (j % 3),
                ).save()

        # SLA Policy
        sla = SLAPolicy(
            tenant_id=tenant_id,
            name="Standard SLA",
            description="Default SLA policy for all tickets",
            is_default=True,
        )
        await sla.save()

        # Escalation rule
        await EscalationRule(
            tenant_id=tenant_id,
            name="Response breach → Team lead",
            trigger_type=EscalationTrigger.RESPONSE_BREACH,
            notify_team_lead=True,
        ).save()

        # Sample tickets
        open_id = wf["statuses"].get("open", 0)
        in_progress_id = wf["statuses"].get("in_progress", 0)
        waiting_id = wf["statuses"].get("waiting", 0)
        resolved_id = wf["statuses"].get("resolved", 0)
        normal_id = wf["priorities"].get("normal", 0)
        high_id = wf["priorities"].get("high", 0)
        critical_id = wf["priorities"].get("critical", 0)
        low_id = wf["priorities"].get("low", 0)
        bug_id = wf["types"].get("bug", 0)
        feature_id = wf["types"].get("feature", 0)
        question_id = wf["types"].get("question", 0)

        customer_id_list = list(customer_ids.values())
        agent_id_list = list(agent_ids.values())
        team_id_list = list(team_ids.values())

        _TICKET_SPECS = [
            (
                "Cannot login to portal",
                bug_id,
                critical_id,
                open_id,
                TicketSource.PORTAL,
            ),
            (
                "Feature: dark mode support",
                feature_id,
                normal_id,
                open_id,
                TicketSource.WEB,
            ),
            (
                "Billing discrepancy on invoice #1234",
                bug_id,
                high_id,
                in_progress_id,
                TicketSource.EMAIL,
            ),
            (
                "How to reset API key?",
                question_id,
                low_id,
                resolved_id,
                TicketSource.API,
            ),
            (
                "Performance degradation on dashboard",
                bug_id,
                high_id,
                waiting_id,
                TicketSource.WEB,
            ),
            (
                "Request: export to CSV",
                feature_id,
                normal_id,
                open_id,
                TicketSource.PORTAL,
            ),
            (
                "Mobile app crashes on iOS 18",
                bug_id,
                critical_id,
                in_progress_id,
                TicketSource.PORTAL,
            ),
            (
                "Integration with Slack",
                feature_id,
                normal_id,
                open_id,
                TicketSource.WEB,
            ),
            (
                "Cannot attach files larger than 10MB",
                bug_id,
                normal_id,
                open_id,
                TicketSource.EMAIL,
            ),
            (
                "SSO setup documentation unclear",
                question_id,
                low_id,
                resolved_id,
                TicketSource.WEB,
            ),
        ]

        for i in range(min(ticket_count, len(_TICKET_SPECS))):
            title, type_id, priority_id, status_id, source = _TICKET_SPECS[i]
            cust_id = customer_id_list[i % len(customer_id_list)]
            assign_id = agent_id_list[i % len(agent_id_list)] if i % 3 != 0 else 0
            t_team_id = team_id_list[i % len(team_id_list)] if team_id_list else 0

            ticket = Ticket(
                tenant_id=tenant_id,
                ticket_number=f"{org_slug.upper()}-{i + 1:04d}",
                title=title,
                description=f"Detailed description for: {title}",
                status_id=status_id,
                priority_id=priority_id,
                ticket_type_id=type_id,
                assignee_id=assign_id,
                team_id=t_team_id,
                customer_id=cust_id,
                source=source,
                sla_policy_id=sla.id,
            )
            await ticket.save()

            # Add a comment from the customer
            await Comment(
                tenant_id=tenant_id,
                ticket_id=ticket.id,
                author_type=AuthorType.CUSTOMER,
                author_id=cust_id,
                body=f"I need help with: {title}",
            ).save()

            # Add a tag
            tag_list = list(tag_ids.values())
            if tag_list:
                await TicketTag(
                    tenant_id=tenant_id,
                    ticket_id=ticket.id,
                    tag_id=tag_list[i % len(tag_list)],
                ).save()

        logger.info(
            "    {n} tickets with comments and tags",
            n=min(ticket_count, len(_TICKET_SPECS)),
        )


async def run(db) -> None:
    """Seed HyperTicket demo data."""
    logger.info("\nSeeding HyperTicket data...")

    # Check if already seeded
    existing = await Org.objects.filter(slug="acme").first()
    if existing:
        logger.info("  already seeded — skipping")
        return

    # Plans
    plan_ids: dict[str, int] = {}
    for name, description, is_public, display_order, price in _PLANS:
        p = PlanConfig(
            name=name,
            description=description,
            is_public=is_public,
            display_order=display_order,
            base_price_cents=price,
        )
        await p.save()
        plan_ids[name] = p.id

        # Feature limits
        for feature_key, limit_value, enforcement_str in _FEATURE_LIMITS[name]:
            await PlanFeatureLimit(
                plan_config_id=p.id,
                feature_key=feature_key,
                limit_value=limit_value,
                enforcement=QuotaEnforcement(enforcement_str),
            ).save()
    logger.info("  {n} plans with feature limits", n=len(plan_ids))

    # Org 1: Acme Corp (Professional plan)
    await _seed_org(
        org_name="Acme Corp",
        org_slug="acme",
        plan_id=plan_ids["Professional"],
        agent_specs=[
            ("admin@acme.com", "Admin", "admin", seed_password("admin")),
            ("alice@acme.com", "Alice Chen", "team_lead", seed_password("alice")),
            ("bob@acme.com", "Bob Wilson", "agent", seed_password("bob")),
            ("carol@acme.com", "Carol Davis", "agent", seed_password("carol")),
        ],
        customer_specs=[
            ("cust1@example.com", "Dana White"),
            ("cust2@example.com", "Eve Johnson"),
            ("cust3@example.com", "Frank Miller"),
        ],
        team_specs=[
            ("Engineering", "engineering", "alice@acme.com"),
            ("Billing", "billing", "bob@acme.com"),
        ],
        ticket_count=10,
    )

    # Org 2: Globex Inc (Enterprise plan)
    await _seed_org(
        org_name="Globex Inc",
        org_slug="globex",
        plan_id=plan_ids["Enterprise"],
        agent_specs=[
            ("admin@globex.com", "Globex Admin", "admin", seed_password("admin")),
            ("grace@globex.com", "Grace Lee", "team_lead", seed_password("grace")),
            ("henry@globex.com", "Henry Brown", "agent", seed_password("henry")),
        ],
        customer_specs=[
            ("client1@partner.com", "Iris Park"),
            ("client2@partner.com", "Jack Thompson"),
        ],
        team_specs=[
            ("Support", "support", "grace@globex.com"),
            ("Infrastructure", "infra", "henry@globex.com"),
        ],
        ticket_count=8,
    )

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info("HyperTicket seed complete!")
