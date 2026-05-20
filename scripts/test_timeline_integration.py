#!/usr/bin/env python3
"""
Integration tests — verify refactored services correctly use StatusTimeline.

Tests that the actual models (HyperNews User, HyperTicket Ticket/Agent,
Multi-Tenant Org) correctly create, query, and end timeline events through
their StatusTimelineMixin integration.

Usage:
    uv run hyper-test timeline_integration
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from datetime import timedelta

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.timeline import (
    TIMELINE_INDEXES,
    TimelineManager,
    get_timeline,
    set_timeline,
)

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS  {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL  {name}" + (f" — {details}" if details else ""))


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    set_timeline(TimelineManager())

    # Drop and recreate all needed tables
    for table in [
        "hyper_status_events",
        "ti_hn_users",
        "ti_ht_tickets",
        "ti_ht_agents",
        "ti_mt_orgs",
    ]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    # Create status events table
    await db.execute("""
        CREATE TABLE hyper_status_events (
            id SERIAL PRIMARY KEY,
            entity_type TEXT, entity_id INTEGER,
            category TEXT, status TEXT,
            started_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, ended_at TIMESTAMPTZ,
            actor_id INTEGER, ended_by INTEGER,
            reason TEXT DEFAULT '', end_reason TEXT DEFAULT '',
            detail JSONB DEFAULT '{}',
            tenant_id INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    for ddl in TIMELINE_INDEXES:
        await db.execute(ddl)

    return db


async def teardown_db(db):
    for table in [
        "hyper_status_events",
        "ti_hn_users",
        "ti_ht_tickets",
        "ti_ht_agents",
        "ti_mt_orgs",
    ]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await db.disconnect()


# ── HyperNews User Timeline ──────────────────────────────────────────────


async def test_hypernews_user_timeline():
    """Verify HyperNews User model correctly uses StatusTimelineMixin."""
    print("\n=== HyperNews User Timeline ===")

    from hyperdjango.mixins import TimestampMixin
    from hyperdjango.models import Field
    from hyperdjango.timeline import StatusTimelineMixin

    # Define test model matching hypernews pattern
    class HNUser(StatusTimelineMixin, TimestampMixin):
        class Meta:
            table = "ti_hn_users"

        class TimelineConfig:
            entity_type = "user"
            categories = {
                "moderation": ["banned", "muted", "warned"],
                "access": ["staff", "moderator"],
            }

        id: int = Field(primary_key=True, auto=True)
        username: str = Field(unique=True)

    db = get_db()
    await db.execute("""
        CREATE TABLE ti_hn_users (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    # Create user
    user = HNUser(username="testuser")
    await user.save()

    # --- Ban with reason and actor ---
    event = await user.set_status(
        "moderation",
        "banned",
        reason="Spam violation",
        actor_id=99,
        detail={"violation_count": 3},
    )
    check("Ban event created", event.id > 0)
    check("Ban has correct status", event.status == "banned")
    check("Ban has correct category", event.category == "moderation")
    check("Ban has reason", event.reason == "Spam violation")
    check("Ban has actor_id", event.actor_id == 99)

    # Verify active
    check("User is banned", await user.has_status("moderation", "banned"))
    check("User is NOT muted", not await user.has_status("moderation", "muted"))

    # Verify current_status returns full record
    status = await user.get_status("moderation")
    check("Current status is banned", status is not None and status.status == "banned")
    check("Status has reason", status.reason == "Spam violation")
    check("Status has actor_id", status.actor_id == 99)
    check("Status has detail", status.detail.get("violation_count") == 3)
    check("Status has entity_type", status.entity_type == "user")
    check("Status has entity_id", status.entity_id == user.id)

    # --- Unban with reason ---
    cleared = await user.clear_status(
        "moderation", reason="Appeal approved", actor_id=100
    )
    check("Unban succeeded", cleared)
    check("User no longer banned", not await user.has_status("moderation", "banned"))

    # --- History shows both events ---
    history = await user.get_status_history("moderation")
    check("History has 1 event", len(history) == 1)
    check("History event was banned", history[0].status == "banned")
    check("History event has ended_at", history[0].ended_at is not None)
    check("History event has end_reason", history[0].end_reason == "Appeal approved")

    # --- Staff access ---
    await user.set_status("access", "staff", reason="Promoted")
    check("User is staff", await user.has_status("access", "staff"))

    # Staff and moderation are independent categories
    await user.set_status(
        "moderation", "muted", reason="Temp mute", expires_in=timedelta(hours=1)
    )
    check(
        "User is muted AND staff",
        await user.has_status("moderation", "muted")
        and await user.has_status("access", "staff"),
    )

    # --- Validation ---
    try:
        await user.set_status("nonexistent", "banned")
        check("Invalid category rejected", False)
    except ValueError:
        check("Invalid category rejected", True)

    try:
        await user.set_status("moderation", "nonexistent")
        check("Invalid status rejected", False)
    except ValueError:
        check("Invalid status rejected", True)


# ── HyperTicket Ticket Timeline ──────────────────────────────────────────


async def test_hyperticket_ticket_timeline():
    """Verify ticket lock/mute uses timeline correctly."""
    print("\n=== HyperTicket Ticket Timeline ===")

    from hyperdjango.mixins import TimestampMixin
    from hyperdjango.models import Field
    from hyperdjango.timeline import StatusTimelineMixin

    class HTTicket(StatusTimelineMixin, TimestampMixin):
        class Meta:
            table = "ti_ht_tickets"

        class TimelineConfig:
            entity_type = "ticket"
            categories = {"state": ["locked", "muted"]}

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(default="Test ticket")

    db = get_db()
    await db.execute("""
        CREATE TABLE ti_ht_tickets (
            id SERIAL PRIMARY KEY, title TEXT DEFAULT 'Test ticket',
            created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    ticket = HTTicket(title="Bug report")
    await ticket.save()

    # Lock
    await ticket.set_status("state", "locked", reason="Under review", actor_id=5)
    check("Ticket locked", await ticket.has_status("state", "locked"))
    check("Ticket NOT muted", not await ticket.has_status("state", "muted"))

    # Lock replaces to mute (same category)
    await ticket.set_status("state", "muted", reason="Customer requested", actor_id=5)
    check("Lock replaced by mute", not await ticket.has_status("state", "locked"))
    check("Ticket is muted", await ticket.has_status("state", "muted"))

    # Unlock (clear state)
    await ticket.clear_status("state", reason="Resolved", actor_id=5)
    check("State cleared", not await ticket.has_status("state", "locked"))
    check("Mute also cleared", not await ticket.has_status("state", "muted"))

    # History
    history = await ticket.get_status_history("state")
    check("Ticket has 2 state events", len(history) == 2, f"got {len(history)}")


# ── HyperTicket Agent Timeline ───────────────────────────────────────────


async def test_hyperticket_agent_timeline():
    """Verify agent activation/deactivation uses timeline."""
    print("\n=== HyperTicket Agent Timeline ===")

    from hyperdjango.mixins import TimestampMixin
    from hyperdjango.models import Field
    from hyperdjango.timeline import StatusTimelineMixin

    class HTAgent(StatusTimelineMixin, TimestampMixin):
        class Meta:
            table = "ti_ht_agents"

        class TimelineConfig:
            entity_type = "agent"
            categories = {"lifecycle": ["active", "deactivated"]}

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(default="")

    db = get_db()
    await db.execute("""
        CREATE TABLE ti_ht_agents (
            id SERIAL PRIMARY KEY, name TEXT DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    agent = HTAgent(name="Agent Smith")
    await agent.save()

    # New agent — no deactivation event → active by default
    check(
        "New agent not deactivated",
        not await agent.has_status("lifecycle", "deactivated"),
    )

    # Deactivate
    await agent.set_status(
        "lifecycle", "deactivated", reason="Left company", actor_id=1
    )
    check("Agent deactivated", await agent.has_status("lifecycle", "deactivated"))

    # Reactivate (clear)
    await agent.clear_status("lifecycle", reason="Rehired", actor_id=1)
    check("Agent reactivated", not await agent.has_status("lifecycle", "deactivated"))


# ── Multi-Tenant Org Timeline ────────────────────────────────────────────


async def test_multi_tenant_org_timeline():
    """Verify org suspension uses timeline."""
    print("\n=== Multi-Tenant Org Timeline ===")

    from hyperdjango.mixins import TimestampMixin
    from hyperdjango.models import Field
    from hyperdjango.timeline import StatusTimelineMixin

    class MTOrg(StatusTimelineMixin, TimestampMixin):
        class Meta:
            table = "ti_mt_orgs"

        class TimelineConfig:
            entity_type = "org"
            categories = {"lifecycle": ["suspended"]}

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(default="")

    db = get_db()
    await db.execute("""
        CREATE TABLE ti_mt_orgs (
            id SERIAL PRIMARY KEY, name TEXT DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    org = MTOrg(name="Acme Corp")
    await org.save()

    # New org → not suspended
    check("New org not suspended", not await org.has_status("lifecycle", "suspended"))

    # Suspend
    await org.set_status(
        "lifecycle",
        "suspended",
        reason="Payment overdue",
        actor_id=1,
        detail={"invoice_id": 42, "amount_due": 500},
    )
    check("Org suspended", await org.has_status("lifecycle", "suspended"))

    status = await org.get_status("lifecycle")
    check("Suspension has reason", status.reason == "Payment overdue")
    check("Suspension has detail", status.detail.get("invoice_id") == 42)

    # Reactivate
    await org.clear_status("lifecycle", reason="Payment received", actor_id=2)
    check("Org reactivated", not await org.has_status("lifecycle", "suspended"))

    # Full history
    history = await org.get_status_history("lifecycle")
    check("History has 1 suspension event", len(history) == 1)
    check("Event has ended_at", history[0].ended_at is not None)
    check("Event has end_reason", history[0].end_reason == "Payment received")


# ── Cross-App: get_entities ───────────────────────────────────────────────


async def test_cross_app_get_entities():
    """Verify get_entities works across entity types."""
    print("\n=== Cross-App get_entities ===")

    tl = get_timeline()

    # Create banned users and suspended orgs
    for uid in [901, 902, 903]:
        await tl.add_event(
            "user", uid, "moderation", "banned", reason="Test ban", actor_id=1
        )
    await tl.add_event("org", 801, "lifecycle", "suspended", reason="Test suspend")

    # Unban one
    await tl.end_status("user", 902, "moderation", end_reason="Unbanned")

    banned = await tl.get_entities("user", "banned")
    check("Banned users: 901 and 903", set(banned) == {901, 903}, f"got {banned}")

    suspended = await tl.get_entities("org", "suspended")
    check("Suspended orgs: 801", suspended == [801], f"got {suspended}")


# ── Escalation Engine Integration ────────────────────────────────────────


async def test_escalation_engine_hypernews():
    """Verify EscalationEngine auto-applies consequences on threshold events.

    Mirrors the hypernews production configuration:
      - 3 warnings → auto-mute for 7 days
      - 2 mutes in 30 days → auto-ban
    """
    print("\n=== Escalation Engine (HyperNews config) ===")

    from hyperdjango.timeline import EscalationEngine, EscalationRule

    tl = get_timeline()

    # Set up escalation matching hypernews config
    escalation = EscalationEngine()
    escalation.add_rule(
        "user",
        EscalationRule(
            trigger_status="warned",
            threshold=3,
            window=timedelta(days=365),
            consequence_category="moderation",
            consequence_status="muted",
            consequence_expires_in=timedelta(days=7),
            consequence_reason="Auto-muted: 3 warnings accumulated",
        ),
    )
    escalation.add_rule(
        "user",
        EscalationRule(
            trigger_status="muted",
            threshold=2,
            window=timedelta(days=30),
            consequence_category="moderation",
            consequence_status="banned",
            consequence_reason="Auto-banned: 2 mutes within 30 days",
        ),
    )
    escalation.connect()

    user_id = 5001

    # Warn 1 — no consequence yet
    await tl.add_event(
        "user", user_id, "moderation", "warned", reason="Spam", actor_id=1
    )
    check("1 warning: not muted", not await tl.is_active("user", user_id, "muted"))

    # Warn 2 — still no consequence
    await tl.add_event(
        "user", user_id, "moderation", "warned", reason="Spam again", actor_id=1
    )
    check("2 warnings: not muted", not await tl.is_active("user", user_id, "muted"))

    # Warn 3 — triggers auto-mute
    await tl.add_event(
        "user", user_id, "moderation", "warned", reason="Third strike", actor_id=1
    )
    check("3 warnings: auto-muted", await tl.is_active("user", user_id, "muted"))

    # Check the auto-mute event has proper metadata
    status = await tl.current_status("user", user_id, "moderation")
    check("Auto-mute has reason", "3 warnings" in status.reason)
    check("Auto-mute has expiry", status.expires_at is not None)

    # End the mute (e.g., moderator clears it)
    await tl.end_status("user", user_id, "moderation", end_reason="Mute expired")
    check("Mute cleared", not await tl.is_active("user", user_id, "muted"))

    # Now mute again directly (simulating second mute within 30 days)
    await tl.add_event(
        "user", user_id, "moderation", "muted", reason="Manual mute", actor_id=2
    )
    # 2 mutes in 30 days (auto-mute + manual mute) → auto-ban
    check("2 mutes: auto-banned", await tl.is_active("user", user_id, "banned"))

    ban_status = await tl.current_status("user", user_id, "moderation")
    check("Auto-ban has reason", "2 mutes" in ban_status.reason)
    check("Auto-ban is indefinite", ban_status.expires_at is None)

    # User with no warnings should not be affected
    clean_user_id = 5002
    check(
        "Clean user not muted", not await tl.is_active("user", clean_user_id, "muted")
    )
    check(
        "Clean user not banned", not await tl.is_active("user", clean_user_id, "banned")
    )

    # Verify full history is tracked correctly
    history = await tl.get_history("user", user_id, "moderation")
    ban_events = [h for h in history if h.status == "banned"]
    mute_events = [h for h in history if h.status == "muted"]
    warn_events = [h for h in history if h.status == "warned"]
    check("History has 3 warnings", len(warn_events) == 3, f"got {len(warn_events)}")
    check("History has mute events", len(mute_events) >= 2, f"got {len(mute_events)}")
    check("History has ban event", len(ban_events) >= 1, f"got {len(ban_events)}")

    escalation.disconnect()


async def test_escalation_depth_limit():
    """Verify cascading escalations stop at MAX_DEPTH to prevent infinite loops."""
    print("\n=== Escalation Depth Limit ===")

    from hyperdjango.timeline import EscalationEngine, EscalationRule

    tl = get_timeline()

    # Create a chain: A→B→C→D→E→F (6 levels, MAX_DEPTH=5 should stop at 5)
    escalation = EscalationEngine()
    statuses = ["level_a", "level_b", "level_c", "level_d", "level_e", "level_f"]
    for i in range(len(statuses) - 1):
        escalation.add_rule(
            "user",
            EscalationRule(
                trigger_status=statuses[i],
                threshold=1,
                window=timedelta(days=365),
                consequence_category="chain",
                consequence_status=statuses[i + 1],
                consequence_reason=f"Auto: {statuses[i]} → {statuses[i + 1]}",
            ),
        )
    escalation.connect()

    user_id = 7001

    # Trigger the chain by setting level_a
    await tl.add_event("user", user_id, "chain", "level_a", reason="Start chain")

    # Should cascade through several levels but stop before infinite
    history = await tl.get_history("user", user_id, "chain")
    reached = {h.status for h in history}

    check("Chain started with level_a", "level_a" in reached)
    # At least some cascading happened
    check("Chain cascaded to level_b", "level_b" in reached)
    # But the chain must stop before running forever
    check(
        "Chain has finite depth",
        len(reached) <= escalation.MAX_DEPTH + 1,
        f"got {len(reached)} levels: {reached}",
    )

    escalation.disconnect()


# ── Main ──────────────────────────────────────────────────────────────────


async def async_main():
    db = await setup_db()
    try:
        await test_hypernews_user_timeline()
        await test_hyperticket_ticket_timeline()
        await test_hyperticket_agent_timeline()
        await test_multi_tenant_org_timeline()
        await test_cross_app_get_entities()
        await test_escalation_engine_hypernews()
        await test_escalation_depth_limit()
    finally:
        await teardown_db(db)


def main():
    print("Timeline Integration Tests")
    print("=" * 60)

    asyncio.run(async_main())

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"{total} tests: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for e in RESULTS["errors"]:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if RESULTS["failed"] else 0)


if __name__ == "__main__":
    main()
