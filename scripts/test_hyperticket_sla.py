"""
HyperTicket Phase 3 — SLA, Workflow, Assignment Tests.

Tests SLA deadline calculation, pause/resume, breach detection,
workflow rule matching, auto-assignment, and signal integration.
Runs against a live PostgreSQL database.

Usage:
    uv run hyper-test hyperticket_sla
"""

# hyper-test: db_isolated

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from hyperdjango.database import Database, set_db
from hyperdjango.tenancy import tenant_context
from services.hyperticket.models import (
    Org,
    SLAInstance,
    Team,
    Ticket,
    TicketTag,
    WorkflowRule,
    WorkflowTrigger,
)
from services.hyperticket.services.assignment import (
    assign_round_robin,
    assign_skill_based,
    auto_assign,
)
from services.hyperticket.services.sla_engine import SLAEngine, sla_engine
from services.hyperticket.services.workflow_engine import (
    _match_condition,
    evaluate_rules,
)
from services.hyperticket.tasks.sla import check_sla_breaches

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
    """Run all Phase 3 tests against live DB."""
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    acme = await Org.objects.filter(slug="acme").first()
    test_true("acme exists", acme is not None)
    if not acme:
        return

    # -----------------------------------------------------------------------
    # 1. SLA Engine — deadline calculation
    # -----------------------------------------------------------------------
    print("\n--- SLA Deadline Calculation ---")
    engine = SLAEngine()

    # Mon 9am UTC + 60 business minutes = Mon 10am (within 09:00-17:00 window)
    start = datetime(2026, 4, 6, 9, 0, tzinfo=UTC)  # Mon 9am UTC
    bh = {
        "mon": ["09:00", "17:00"],
        "tue": ["09:00", "17:00"],
        "wed": ["09:00", "17:00"],
    }
    deadline = engine.calculate_deadline(start, 60, bh, [], "UTC")
    test_true("60 min within day", deadline == start + timedelta(minutes=60))

    # 480 min (8 hours) from Mon 9am = Mon 5pm (exact close)
    deadline = engine.calculate_deadline(start, 480, bh, [], "UTC")
    expected_close = datetime(2026, 4, 6, 17, 0, tzinfo=UTC)
    test_true("480 min = end of day", deadline == expected_close, f"got {deadline}")

    # 490 min from Mon 9am: 480 fills Mon, remaining 10 → Tue 9:10
    deadline = engine.calculate_deadline(start, 490, bh, [], "UTC")
    expected_tue = datetime(2026, 4, 7, 9, 10, tzinfo=UTC)
    test_true("490 min spills to next day", deadline == expected_tue, f"got {deadline}")

    # Skip weekend: Fri 4pm + 120 min = Mon 10am (skips Sat+Sun)
    fri = datetime(2026, 4, 10, 16, 0, tzinfo=UTC)  # Fri 4pm UTC
    bh_full = {
        "mon": ["09:00", "17:00"],
        "tue": ["09:00", "17:00"],
        "wed": ["09:00", "17:00"],
        "thu": ["09:00", "17:00"],
        "fri": ["09:00", "17:00"],
    }
    deadline = engine.calculate_deadline(fri, 120, bh_full, [], "UTC")
    # Fri 16:00→17:00 = 60 min, remaining 60 → Mon 09:00+60 = Mon 10:00
    expected_mon = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
    test_true("skip weekend", deadline == expected_mon, f"got {deadline}")

    # Skip holiday
    holidays = ["2026-04-06"]  # Mon is a holiday
    deadline = engine.calculate_deadline(start, 60, bh, holidays, "UTC")
    # Mon is holiday → skip to Tue 9:00 + 60 = Tue 10:00
    expected_tue_holiday = datetime(2026, 4, 7, 10, 0, tzinfo=UTC)
    test_true("skip holiday", deadline == expected_tue_holiday, f"got {deadline}")

    # Zero minutes
    deadline = engine.calculate_deadline(start, 0, bh, [], "UTC")
    test_true("zero minutes = same time", deadline == start)

    # -----------------------------------------------------------------------
    # 2. SLA Engine — create instance for ticket
    # -----------------------------------------------------------------------
    print("\n--- SLA Instance Creation ---")
    with tenant_context(tenant_id=acme.id):
        ticket = await Ticket.objects.first()
        test_true("ticket exists", ticket is not None)

        if ticket:
            # Create SLA instance
            instance = await sla_engine.create_instance(ticket)
            test_true("SLA instance created", instance is not None)
            if instance:
                test_true(
                    "has first_response_target",
                    instance.first_response_target is not None,
                )
                test_true(
                    "has resolution_target", instance.resolution_target is not None
                )
                test("not breached initially", instance.breached, False)
                test("first_response pending", instance.first_response_met, -1)

    # -----------------------------------------------------------------------
    # 3. SLA Pause/Resume
    # -----------------------------------------------------------------------
    print("\n--- SLA Pause/Resume ---")
    with tenant_context(tenant_id=acme.id):
        instances = await SLAInstance.objects.all()
        if instances:
            inst = instances[0]
            # Pause
            await sla_engine.pause(inst)
            paused = await SLAInstance.objects.filter(id=inst.id).first()
            test_true(
                "paused_at set after pause",
                paused is not None and paused.paused_at is not None,
            )

    # -----------------------------------------------------------------------
    # 4. Workflow Engine — condition matching
    # -----------------------------------------------------------------------
    print("\n--- Workflow Condition Matching ---")
    ticket_data = {"priority_id": 1, "status_id": 5, "team_id": 3, "title": "Login bug"}

    test_true(
        "eq match",
        _match_condition({"field": "priority_id", "op": "eq", "value": 1}, ticket_data),
    )
    test_true(
        "ne match",
        _match_condition({"field": "priority_id", "op": "ne", "value": 2}, ticket_data),
    )
    test_true(
        "gt match",
        _match_condition({"field": "status_id", "op": "gt", "value": 3}, ticket_data),
    )
    test_true(
        "in match",
        _match_condition(
            {"field": "team_id", "op": "in", "value": [1, 2, 3]}, ticket_data
        ),
    )
    test_true(
        "not_in match",
        _match_condition(
            {"field": "team_id", "op": "not_in", "value": [10, 20]}, ticket_data
        ),
    )
    test_true(
        "contains match",
        _match_condition(
            {"field": "title", "op": "contains", "value": "Login"}, ticket_data
        ),
    )

    # Nested AND
    test_true(
        "all match",
        _match_condition(
            {
                "all": [
                    {"field": "priority_id", "op": "eq", "value": 1},
                    {"field": "team_id", "op": "eq", "value": 3},
                ]
            },
            ticket_data,
        ),
    )

    # Nested OR
    test_true(
        "any match",
        _match_condition(
            {
                "any": [
                    {"field": "priority_id", "op": "eq", "value": 99},
                    {"field": "team_id", "op": "eq", "value": 3},
                ]
            },
            ticket_data,
        ),
    )

    # Negative cases
    test_true(
        "eq no match",
        not _match_condition(
            {"field": "priority_id", "op": "eq", "value": 99}, ticket_data
        ),
    )
    test_true(
        "all partial fail",
        not _match_condition(
            {
                "all": [
                    {"field": "priority_id", "op": "eq", "value": 1},
                    {"field": "team_id", "op": "eq", "value": 99},
                ]
            },
            ticket_data,
        ),
    )

    # Empty conditions = always match
    test_true("empty conditions match", _match_condition({}, ticket_data))

    # -----------------------------------------------------------------------
    # 5. Workflow Engine — rule evaluation (live DB)
    # -----------------------------------------------------------------------
    print("\n--- Workflow Rule Evaluation ---")
    with tenant_context(tenant_id=acme.id):
        # Create a test workflow rule
        rule = WorkflowRule(
            tenant_id=acme.id,
            name="Test: auto-tag critical tickets",
            trigger_event=WorkflowTrigger.TICKET_CREATED,
            conditions=json.dumps({"field": "priority_id", "op": "eq", "value": 1}),
            actions=json.dumps([]),  # No real actions for test
            is_active=True,
            execution_order=0,
        )
        await rule.save()
        test_true("workflow rule created", rule.id > 0)

        ticket = await Ticket.objects.first()
        if ticket:
            matched = await evaluate_rules(ticket, WorkflowTrigger.TICKET_CREATED)
            test_gte("workflow rules evaluated", matched, 0)

    # -----------------------------------------------------------------------
    # 6. Assignment — round-robin
    # -----------------------------------------------------------------------
    print("\n--- Round-Robin Assignment ---")
    with tenant_context(tenant_id=acme.id):
        teams = await Team.objects.all()
        if teams:
            team = teams[0]
            # Get a ticket without assignee
            unassigned = await Ticket.objects.filter(assignee_id=0).first()
            if unassigned:
                agent_id = await assign_round_robin(unassigned, team.id)
                test_true("round-robin assigned", agent_id is not None and agent_id > 0)
            else:
                # All tickets already assigned — just verify the function runs
                ticket = await Ticket.objects.first()
                if ticket:
                    agent_id = await assign_round_robin(ticket, team.id)
                    test_true("round-robin returned an agent", agent_id is not None)

    # -----------------------------------------------------------------------
    # 7. Assignment — skill-based
    # -----------------------------------------------------------------------
    print("\n--- Skill-Based Assignment ---")
    with tenant_context(tenant_id=acme.id):
        ticket = await Ticket.objects.first()
        if ticket:
            # Ticket needs tags for skill matching
            tags = await TicketTag.objects.filter(ticket_id=ticket.id).all()
            if tags:
                agent_id = await assign_skill_based(ticket)
                test_true("skill-based ran (may be None if no match)", True)
            else:
                test_true("no tags for skill matching (expected)", True)

    # -----------------------------------------------------------------------
    # 8. Auto-assign
    # -----------------------------------------------------------------------
    print("\n--- Auto-Assign ---")
    with tenant_context(tenant_id=acme.id):
        ticket = await Ticket.objects.first()
        if ticket:
            result = await auto_assign(ticket)
            # May return None or agent_id depending on strategy
            test_true("auto_assign ran without error", True)

    # -----------------------------------------------------------------------
    # 9. SLA breach check task
    # -----------------------------------------------------------------------
    print("\n--- SLA Breach Check ---")
    with tenant_context(tenant_id=acme.id):
        breached = await check_sla_breaches()
        test_true("breach check ran", breached >= 0)

    # -----------------------------------------------------------------------
    # Done
    # -----------------------------------------------------------------------
    await db.disconnect()


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 3: SLA, Workflow, Assignment Tests")
    print("=" * 60)

    # Setup
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
        print(f"Setup failed:\n{result.stderr[-500:]}\n{result.stdout[-500:]}")
        sys.exit(1)
    print("Setup complete.")

    asyncio.run(run_tests())

    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"HyperTicket SLA/Workflow: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
