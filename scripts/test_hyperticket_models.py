"""
HyperTicket Phase 1 — Model & Data Foundation Tests.

Tests table creation, seed data, tenant isolation, configurable workflow,
soft delete, versioned mixin, IDMixin, ticket numbers, and plan features.
Runs against a live PostgreSQL database.

Usage:
    uv run hyper-test hyperticket_models
"""

# hyper-test: db_isolated

import asyncio
import os
import subprocess
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.tenancy import tenant_context
from services.hyperticket.models import (
    Agent,
    AgentRole,
    AgentSkill,
    Comment,
    EscalationRule,
    Org,
    OrgSettings,
    PlanConfig,
    PlanFeatureLimit,
    PriorityConfig,
    SLAPolicy,
    StatusTransition,
    Tag,
    Team,
    TeamMembership,
    TenantTheme,
    Ticket,
    TicketStatusConfig,
    TicketTag,
    TicketTypeConfig,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name: str, got: object, expected: object) -> bool:
    global PASS, FAIL
    if got == expected:
        PASS += 1
        return True
    FAIL += 1
    msg = f"  FAIL: {name} — got {got!r}, expected {expected!r}"
    print(msg)
    ERRORS.append(msg)
    return False


def test_true(name: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        return True
    FAIL += 1
    msg = f"  FAIL: {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    ERRORS.append(msg)
    return False


def test_gte(name: str, got: int | float, minimum: int | float) -> bool:
    global PASS, FAIL
    if got >= minimum:
        PASS += 1
        return True
    FAIL += 1
    msg = f"  FAIL: {name} — got {got}, expected >= {minimum}"
    print(msg)
    ERRORS.append(msg)
    return False


async def run_tests() -> None:
    """Run all Phase 1 model tests against live DB."""
    # Connect to DB
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Indexes are now in Meta.indexes and created by `hyper setup`

    # -----------------------------------------------------------------------
    # 1. Table existence (all 27+ tables exist)
    # -----------------------------------------------------------------------
    print("\n--- Table Existence ---")
    expected_tables = [
        "ht_orgs",
        "ht_plan_configs",
        "ht_plan_feature_limits",
        "ht_org_settings",
        "ht_org_api_keys",
        "ht_tenant_themes",
        "ht_agents",
        "ht_customers",
        "ht_teams",
        "ht_team_memberships",
        "ht_agent_skills",
        "ht_ticket_status_configs",
        "ht_status_transitions",
        "ht_priority_configs",
        "ht_ticket_type_configs",
        "ht_tickets",
        "ht_tags",
        "ht_ticket_tags",
        "ht_ticket_relations",
        "ht_comments",
        "ht_attachments",
        "ht_canned_responses",
        "ht_satisfaction_ratings",
        "ht_sla_policies",
        "ht_sla_instances",
        "ht_escalation_rules",
        "ht_workflow_rules",
        "ht_approvals",
        "ht_ticket_templates",
        "ht_saved_views",
        "ht_notification_preferences",
        "ht_activity_log",
    ]
    for tbl in expected_tables:
        exists = await db.query_val(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
            tbl,
        )
        test_true(f"table {tbl} exists", exists)

    # -----------------------------------------------------------------------
    # 2. Seed data row counts
    # -----------------------------------------------------------------------
    print("\n--- Seed Data ---")
    plan_count = await PlanConfig.objects.count()
    test("plan configs", plan_count, 3)

    org_count = await Org.objects.count()
    test("orgs", org_count, 2)

    # Check per-plan feature limits
    starter = await PlanConfig.objects.filter(name="Starter").first()
    test_true("Starter plan exists", starter is not None)
    if starter:
        limit_count = await PlanFeatureLimit.objects.filter(
            plan_config_id=starter.id
        ).count()
        test("Starter feature limits", limit_count, 9)

    # -----------------------------------------------------------------------
    # 3. Tenant isolation — org1 data invisible from org2 context
    # -----------------------------------------------------------------------
    print("\n--- Tenant Isolation ---")
    acme = await Org.objects.filter(slug="acme").first()
    globex = await Org.objects.filter(slug="globex").first()
    test_true("acme exists", acme is not None)
    test_true("globex exists", globex is not None)

    if acme and globex:
        # Acme's agents should be invisible from globex context
        with tenant_context(tenant_id=acme.id):
            acme_agents = await Agent.objects.count()
            test_gte("acme has agents", acme_agents, 3)

            acme_tickets = await Ticket.objects.count()
            test_gte("acme has tickets", acme_tickets, 5)

        with tenant_context(tenant_id=globex.id):
            globex_agents = await Agent.objects.count()
            test_gte("globex has agents", globex_agents, 2)

            globex_tickets = await Ticket.objects.count()
            test_gte("globex has tickets", globex_tickets, 5)

        # Cross-tenant isolation: acme's tickets not visible in globex
        with tenant_context(tenant_id=globex.id):
            acme_ticket = await Ticket.objects.filter(ticket_number="ACME-0001").first()
            test_true("acme ticket NOT visible in globex", acme_ticket is None)

        with tenant_context(tenant_id=acme.id):
            globex_ticket = await Ticket.objects.filter(
                ticket_number="GLOBEX-0001"
            ).first()
            test_true("globex ticket NOT visible in acme", globex_ticket is None)

        # Unscoped query sees all
        all_tickets = await Ticket.objects.unscoped().count()
        test_gte(
            "unscoped sees all tickets", all_tickets, acme_tickets + globex_tickets
        )

    # -----------------------------------------------------------------------
    # 4. Configurable workflow — statuses, priorities, types per org
    # -----------------------------------------------------------------------
    print("\n--- Configurable Workflow ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            status_count = await TicketStatusConfig.objects.count()
            test("acme statuses", status_count, 7)

            priority_count = await PriorityConfig.objects.count()
            test("acme priorities", priority_count, 4)

            type_count = await TicketTypeConfig.objects.count()
            test("acme types", type_count, 5)

            # Status transitions exist
            transition_count = await StatusTransition.objects.count()
            test_gte("acme has transitions", transition_count, 10)

            # Default status
            default_status = await TicketStatusConfig.objects.filter(
                is_default=True
            ).first()
            test_true("has default status", default_status is not None)
            if default_status:
                test("default status is open", default_status.slug, "open")

            # Terminal status
            terminal = await TicketStatusConfig.objects.filter(is_terminal=True).first()
            test_true("has terminal status", terminal is not None)
            if terminal:
                test("terminal status is closed", terminal.slug, "closed")

            # SLA-pausing status
            pausing = await TicketStatusConfig.objects.filter(pauses_sla=True).first()
            test_true("has SLA-pausing status", pausing is not None)

            # Ticket references configurable status FK
            ticket = await Ticket.objects.filter(ticket_number="ACME-0001").first()
            test_true(
                "ticket has status_id FK", ticket is not None and ticket.status_id > 0
            )
            test_true(
                "ticket has priority_id FK",
                ticket is not None and ticket.priority_id > 0,
            )
            test_true(
                "ticket has ticket_type_id FK",
                ticket is not None and ticket.ticket_type_id > 0,
            )

    # -----------------------------------------------------------------------
    # 5. Plan features — multi-dimensional, DB-configurable
    # -----------------------------------------------------------------------
    print("\n--- Plan Features ---")
    pro = await PlanConfig.objects.filter(name="Professional").first()
    test_true("Professional plan exists", pro is not None)
    if pro:
        agent_limit = await PlanFeatureLimit.objects.filter(
            plan_config_id=pro.id, feature_key="agent_seats"
        ).first()
        test_true("Professional has agent_seats limit", agent_limit is not None)
        if agent_limit:
            test("Professional agent_seats = 25", agent_limit.limit_value, 25.0)

        storage_limit = await PlanFeatureLimit.objects.filter(
            plan_config_id=pro.id, feature_key="storage_bytes"
        ).first()
        test_true("Professional has storage limit", storage_limit is not None)

    enterprise = await PlanConfig.objects.filter(name="Enterprise").first()
    test_true("Enterprise plan exists", enterprise is not None)
    if enterprise:
        ent_seats = await PlanFeatureLimit.objects.filter(
            plan_config_id=enterprise.id, feature_key="agent_seats"
        ).first()
        test_true("Enterprise has agent_seats", ent_seats is not None)
        if ent_seats:
            test("Enterprise agent_seats = unlimited", ent_seats.limit_value, -1.0)

    # -----------------------------------------------------------------------
    # 6. TenantTheme — all fields persist
    # -----------------------------------------------------------------------
    print("\n--- Tenant Theme ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            theme = await TenantTheme.objects.first()
            test_true("acme has theme", theme is not None)
            if theme:
                test("theme company name", theme.company_name_display, "Acme Corp")
                test("theme primary color", theme.primary_color, "#2563eb")
                test_true("theme has font_family", len(theme.font_family) > 0)

    # -----------------------------------------------------------------------
    # 7. OrgSettings — per-tenant config
    # -----------------------------------------------------------------------
    print("\n--- Org Settings ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            settings = await OrgSettings.objects.first()
            test_true("acme has settings", settings is not None)
            if settings:
                test("acme timezone", settings.timezone, "America/New_York")

    # -----------------------------------------------------------------------
    # 8. Teams and memberships
    # -----------------------------------------------------------------------
    print("\n--- Teams ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            team_count = await Team.objects.count()
            test("acme teams", team_count, 2)

            memberships = await TeamMembership.objects.count()
            test_gte("acme team memberships", memberships, 2)

    # -----------------------------------------------------------------------
    # 9. Agent skills
    # -----------------------------------------------------------------------
    print("\n--- Agent Skills ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            skill_count = await AgentSkill.objects.count()
            test_gte("acme agent skills", skill_count, 6)

    # -----------------------------------------------------------------------
    # 10. Tickets — tags, comments
    # -----------------------------------------------------------------------
    print("\n--- Tickets with Relations ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            # Tags exist
            tag_count = await Tag.objects.count()
            test_gte("acme tags", tag_count, 5)

            # Tickets have tags
            ticket_tag_count = await TicketTag.objects.count()
            test_gte("acme ticket-tag junctions", ticket_tag_count, 5)

            # Tickets have comments
            comment_count = await Comment.objects.count()
            test_gte("acme comments", comment_count, 5)

    # -----------------------------------------------------------------------
    # 11. SLA Policy + Escalation Rules
    # -----------------------------------------------------------------------
    print("\n--- SLA ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            sla_count = await SLAPolicy.objects.count()
            test_gte("acme SLA policies", sla_count, 1)

            default_sla = await SLAPolicy.objects.filter(is_default=True).first()
            test_true("has default SLA", default_sla is not None)
            if default_sla:
                test(
                    "SLA first_response_critical",
                    default_sla.first_response_critical,
                    15,
                )
                test("SLA resolution_normal", default_sla.resolution_normal, 1440)

            esc_count = await EscalationRule.objects.count()
            test_gte("acme escalation rules", esc_count, 1)

    # -----------------------------------------------------------------------
    # 12. Ticket numbers — org-prefixed sequential
    # -----------------------------------------------------------------------
    print("\n--- Ticket Numbers ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            t1 = await Ticket.objects.filter(ticket_number="ACME-0001").first()
            test_true("ACME-0001 exists", t1 is not None)
            t2 = await Ticket.objects.filter(ticket_number="ACME-0002").first()
            test_true("ACME-0002 exists", t2 is not None)

    if globex:
        with tenant_context(tenant_id=globex.id):
            g1 = await Ticket.objects.filter(ticket_number="GLOBEX-0001").first()
            test_true("GLOBEX-0001 exists", g1 is not None)

    # -----------------------------------------------------------------------
    # 13. SoftDeleteMixin — excluded from default query
    # -----------------------------------------------------------------------
    print("\n--- Soft Delete ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            before_count = await Ticket.objects.count()

            # Soft-delete one ticket
            ticket_to_delete = await Ticket.objects.filter(
                ticket_number="ACME-0001"
            ).first()
            if ticket_to_delete:
                await ticket_to_delete.delete()
                after_count = await Ticket.objects.count()
                test(
                    "soft delete excludes from default query",
                    after_count,
                    before_count - 1,
                )

                # with_deleted() includes it
                with_deleted = await Ticket.objects.with_deleted().count()
                test_gte(
                    "with_deleted includes soft-deleted", with_deleted, before_count
                )

                # Restore
                await ticket_to_delete.restore()
                restored_count = await Ticket.objects.count()
                test(
                    "restore brings back to default query", restored_count, before_count
                )

    # -----------------------------------------------------------------------
    # 14. IDMixin — HMAC round-trip
    # -----------------------------------------------------------------------
    print("\n--- IDMixin ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            agent = await Agent.objects.first()
            if agent:
                # Agent has IDMixin — test get_external_id / decode_external_id
                public_id = agent.get_external_id()
                test_true(
                    "public ID is a non-empty string",
                    isinstance(public_id, str) and len(public_id) > 0,
                )
                decoded = Agent.decode_external_id(public_id)
                test("IDMixin round-trip", decoded, agent.id)

            ticket = await Ticket.objects.first()
            if ticket:
                ticket_pid = ticket.get_external_id()
                test_true(
                    "ticket public ID",
                    isinstance(ticket_pid, str) and len(ticket_pid) > 0,
                )
                test(
                    "ticket IDMixin round-trip",
                    Ticket.decode_external_id(ticket_pid),
                    ticket.id,
                )

    # -----------------------------------------------------------------------
    # 15. Custom indexes exist
    # -----------------------------------------------------------------------
    print("\n--- Custom Indexes ---")
    expected_indexes = [
        "idx_ht_tickets_tenant_id_status_id",
        "idx_ht_tickets_tenant_id_assignee_id",
        "idx_ht_tickets_tenant_id_created_at",
        "idx_ht_comments_ticket_id_created_at",
        "idx_ht_activity_log_ticket_id_created_at",
        "uq_ht_plan_feature_limits_plan_config_id_feature_key",
        "uq_ht_tickets_tenant_id_ticket_number",
        "uq_ht_tags_tenant_id_name",
    ]
    for idx_name in expected_indexes:
        exists = await db.query_val(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)",
            idx_name,
        )
        test_true(f"index {idx_name}", exists)

    # -----------------------------------------------------------------------
    # 16. Org linked to plan
    # -----------------------------------------------------------------------
    print("\n--- Org-Plan Link ---")
    if acme:
        test_true("acme has plan_config_id", acme.plan_config_id > 0)
        plan = await PlanConfig.objects.filter(id=acme.plan_config_id).first()
        test_true(
            "acme plan is Professional",
            plan is not None and plan.name == "Professional",
        )

    # -----------------------------------------------------------------------
    # 17. Enum fields stored correctly
    # -----------------------------------------------------------------------
    print("\n--- Enum Fields ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            # Enum fields hydrate to enum instances (the ORM coerces raw DB
            # strings back to the declared enum class on read).
            admin_agent = await Agent.objects.filter(role=AgentRole.ADMIN).first()
            test_true("admin agent exists", admin_agent is not None)
            if admin_agent:
                test("admin role value", admin_agent.role, AgentRole.ADMIN)

    # -----------------------------------------------------------------------
    # Done
    # -----------------------------------------------------------------------
    await db.disconnect()


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 1: Model & Data Foundation Tests")
    print("=" * 60)

    # Setup: create tables and seed
    print("\nSetting up database...")
    result = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hyperticket.app:app",
            "--seed",
            "services.hyperticket.seed:run",
            "--drop",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"Setup failed:\n{result.stderr}\n{result.stdout}")
        sys.exit(1)
    print("Setup complete.")

    # Run async tests
    asyncio.run(run_tests())

    # Summary
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"HyperTicket Models: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
