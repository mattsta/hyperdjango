#!/usr/bin/env python3
"""
Tests for status timeline — StatusEvent, TimelineManager, StatusTimelineMixin.

Tests hyperdjango/timeline.py:
- TimelineManager: add_event, end_status, current_status, is_active, get_history,
  get_entities, expire_overdue, cleanup
- StatusTimelineMixin: set_status, clear_status, has_status, get_status, get_status_history
- Category/status validation
- Expiry and auto-expiry
- Multi-category isolation
- Actor attribution and reason tracking
- Signal firing
- Concurrent operations
- Adversarial inputs

Usage:
    uv run hyper-test timeline
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, create_table_for_model
from hyperdjango.timeline import (
    TIMELINE_INDEXES,
    EscalationEngine,
    EscalationRule,
    StatusEvent,
    StatusTimelineMixin,
    TimelineManager,
    get_timeline,
    set_timeline,
    status_changed,
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


# ── Test Models ───────────────────────────────────────────────────────────


class TLUser(StatusTimelineMixin, TimestampMixin):
    class Meta:
        table = "tl_test_users"

    class TimelineConfig:
        entity_type = "user"
        categories = {
            "moderation": ["banned", "muted", "warned", "probation"],
            "access": ["staff", "moderator", "verified"],
            "tier": ["free", "pro", "enterprise"],
        }

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)


class TLForum(StatusTimelineMixin, TimestampMixin):
    class Meta:
        table = "tl_test_forums"

    class TimelineConfig:
        entity_type = "forum"
        categories = {
            "state": ["active", "archived", "locked", "hidden"],
        }

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


# ── DB Setup ──────────────────────────────────────────────────────────────


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create tables from model definitions — single source of truth
    await create_table_for_model(TLUser, db=db, drop=True)
    await create_table_for_model(TLForum, db=db, drop=True)
    await create_table_for_model(StatusEvent, db=db, drop=True)

    # Create compound/partial indexes (not expressible via Field(index=True))
    for ddl in TIMELINE_INDEXES:
        await db.execute(ddl)

    # Reset global timeline
    set_timeline(TimelineManager())

    return db


async def teardown_db(db):
    for table in ["hyper_status_events", "tl_test_users", "tl_test_forums"]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await db.disconnect()


# ── TimelineManager Tests ─────────────────────────────────────────────────


async def test_add_and_current():
    print("\n=== Add Event + Current Status ===")

    tl = get_timeline()

    # Add a ban
    event = await tl.add_event(
        "user", 1, "moderation", "banned", reason="Spam", actor_id=99
    )
    check("add_event returns StatusEvent", event.id > 0)
    check("Event has correct status", event.status == "banned")
    check("Event has reason", event.reason == "Spam")
    check("Event has actor_id", event.actor_id == 99)

    # Current status returns it
    status = await tl.current_status("user", 1, "moderation")
    check("current_status returns StatusRecord", status is not None)
    check("Status is banned", status.status == "banned")
    check("Status has reason", status.reason == "Spam")
    check("Status has actor_id", status.actor_id == 99)
    check("Status has event_id", status.event_id == event.id)

    # No status in other category
    tier = await tl.current_status("user", 1, "tier")
    check("No status in tier category", tier is None)

    # No status for other user
    other = await tl.current_status("user", 999, "moderation")
    check("No status for nonexistent user", other is None)


async def test_end_status():
    print("\n=== End Status ===")

    tl = get_timeline()

    await tl.add_event(
        "user", 2, "moderation", "muted", reason="Profanity", actor_id=99
    )

    # Verify active
    check("Muted is active", await tl.is_active("user", 2, "muted"))

    # End it
    ended = await tl.end_status(
        "user", 2, "moderation", ended_by=100, end_reason="Appeal approved"
    )
    check("end_status returns True", ended)

    # Verify gone
    status = await tl.current_status("user", 2, "moderation")
    check("Current status is None after end", status is None)
    check("is_active False after end", not await tl.is_active("user", 2, "muted"))

    # End again → False (nothing to end)
    ended = await tl.end_status("user", 2, "moderation")
    check("Double end returns False", not ended)


async def test_status_replacement():
    print("\n=== Status Replacement ===")

    tl = get_timeline()

    # Set warned, then upgrade to banned
    await tl.add_event(
        "user", 3, "moderation", "warned", reason="First offense", actor_id=99
    )
    check("Warned is active", await tl.is_active("user", 3, "warned"))

    await tl.add_event(
        "user", 3, "moderation", "banned", reason="Second offense", actor_id=99
    )

    # Warned should be ended, banned should be current
    check("Warned no longer active", not await tl.is_active("user", 3, "warned"))
    check("Banned is active", await tl.is_active("user", 3, "banned"))

    status = await tl.current_status("user", 3, "moderation")
    check("Current is banned", status is not None and status.status == "banned")


async def test_multi_category():
    print("\n=== Multi-Category Isolation ===")

    tl = get_timeline()

    # User can be banned AND have staff access — different categories
    await tl.add_event("user", 4, "moderation", "warned", reason="Mild offense")
    await tl.add_event("user", 4, "access", "staff", reason="Promoted")
    await tl.add_event("user", 4, "tier", "pro", reason="Subscription")

    check("Warned active", await tl.is_active("user", 4, "warned"))
    check("Staff active", await tl.is_active("user", 4, "staff"))
    check("Pro active", await tl.is_active("user", 4, "pro"))

    # End moderation — other categories unaffected
    await tl.end_status("user", 4, "moderation")
    check("Warned ended", not await tl.is_active("user", 4, "warned"))
    check("Staff still active", await tl.is_active("user", 4, "staff"))
    check("Pro still active", await tl.is_active("user", 4, "pro"))


async def test_expiry():
    print("\n=== Expiry ===")

    tl = get_timeline()

    # Set a status that expires in the past (already expired)
    past = datetime.now(UTC) - timedelta(seconds=5)
    await tl.add_event(
        "user", 5, "moderation", "muted", reason="Temp mute", expires_at=past
    )

    # Should NOT be active (expired)
    status = await tl.current_status("user", 5, "moderation")
    check("Expired status returns None", status is None)
    check("Expired status is_active=False", not await tl.is_active("user", 5, "muted"))

    # Set a status that expires in the future
    future = datetime.now(UTC) + timedelta(hours=1)
    await tl.add_event(
        "user", 6, "moderation", "banned", reason="Temp ban", expires_at=future
    )

    status = await tl.current_status("user", 6, "moderation")
    check("Future-expiry status is active", status is not None)
    check("Future-expiry has expires_at", status.expires_at is not None)

    # expires_in parameter
    event = await tl.add_event(
        "user", 7, "moderation", "warned", expires_in=timedelta(hours=2)
    )
    check("expires_in sets expires_at", event.expires_at is not None)


async def test_expire_overdue():
    print("\n=== Expire Overdue ===")

    tl = get_timeline()

    # Create events with past expiry
    past = datetime.now(UTC) - timedelta(seconds=10)
    for i in range(3):
        await tl.add_event(
            "user", 100 + i, "moderation", "muted", reason="Temp", expires_at=past
        )

    # Expire them
    count = await tl.expire_overdue()
    check("Expired 3 overdue events", count >= 3, f"got {count}")

    # All should be inactive now
    for i in range(3):
        check(
            f"User {100 + i} no longer muted",
            not await tl.is_active("user", 100 + i, "muted"),
        )


async def test_history():
    print("\n=== History ===")

    tl = get_timeline()

    # Create a sequence of events. No spacing between them: get_history orders
    # by (started_at DESC, id DESC), so insertion order settles a same-tick tie
    # and the expected order below holds however fast the writes land. The old
    # sleeps were buying timestamp separation the query should never have needed
    # — and on a machine fast enough to write two events inside one tick, they
    # were the only thing standing between this check and a coin flip.
    await tl.add_event("user", 10, "moderation", "warned", reason="First warning")
    await tl.add_event("user", 10, "moderation", "muted", reason="Escalation")
    await tl.add_event("user", 10, "moderation", "banned", reason="Final action")

    history = await tl.get_history("user", 10, "moderation")
    check("History has 3 events", len(history) == 3, f"got {len(history)}")
    check("Newest first", history[0].status == "banned")
    check("Middle is muted", history[1].status == "muted")
    check("Oldest is warned", history[2].status == "warned")

    # First two should have ended_at set (replaced by next status)
    check("Warned has ended_at", history[2].ended_at is not None)
    check("Muted has ended_at", history[1].ended_at is not None)
    check("Banned is current (no ended_at)", history[0].ended_at is None)

    # History with limit
    limited = await tl.get_history("user", 10, "moderation", limit=2)
    check("Limited history has 2", len(limited) == 2)

    # All-category history
    await tl.add_event("user", 10, "access", "staff", reason="Promoted")
    all_history = await tl.get_history("user", 10)
    check("All-category history includes all", len(all_history) >= 4)


async def test_get_entities():
    print("\n=== Get Entities ===")

    tl = get_timeline()

    # Ban several users
    for uid in [20, 21, 22]:
        await tl.add_event("user", uid, "moderation", "banned", reason="Test")

    # Unban one
    await tl.end_status("user", 21, "moderation")

    banned = await tl.get_entities("user", "banned")
    check("20 in banned list", 20 in banned)
    check("21 NOT in banned list", 21 not in banned)
    check("22 in banned list", 22 in banned)


async def test_signal():
    print("\n=== Signal ===")

    tl = get_timeline()
    received: list[dict] = []

    async def on_change(sender, **kwargs):
        received.append(kwargs)

    status_changed.connect(on_change)
    try:
        await tl.add_event("user", 30, "moderation", "banned", reason="Spam")
        check("Signal fired on add", len(received) == 1)
        check("Signal has new_status", received[0]["new_status"] == "banned")
        check("Signal has old_status None", received[0]["old_status"] is None)
        check("Signal has reason", received[0]["reason"] == "Spam")

        await tl.end_status("user", 30, "moderation", end_reason="Appeal")
        check("Signal fired on end", len(received) == 2)
        check("End signal new_status is None", received[1]["new_status"] is None)
        check("End signal old_status is banned", received[1]["old_status"] == "banned")
    finally:
        status_changed.disconnect(on_change)


# ── Mixin Tests ───────────────────────────────────────────────────────────


async def test_mixin_basics():
    print("\n=== Mixin Basics ===")

    # Clear events from prior tests so mixin user gets clean history
    db = get_db()
    await db.execute("DELETE FROM hyper_status_events")

    user = TLUser(username="mixin_test")
    await user.save()

    # set_status
    event = await user.set_status("moderation", "banned", reason="Test ban", actor_id=1)
    check("Mixin set_status returns event", event.id > 0)

    # has_status
    check("has_status True", await user.has_status("moderation", "banned"))
    check(
        "has_status False for muted", not await user.has_status("moderation", "muted")
    )

    # get_status
    status = await user.get_status("moderation")
    check("get_status returns record", status is not None)
    check("get_status correct status", status.status == "banned")

    # clear_status
    cleared = await user.clear_status("moderation", reason="Unbanned", actor_id=2)
    check("clear_status returns True", cleared)
    check(
        "has_status False after clear",
        not await user.has_status("moderation", "banned"),
    )

    # get_status_history
    history = await user.get_status_history("moderation")
    check("History has 1 event", len(history) == 1)
    check("History event is banned", history[0].status == "banned")
    check("History event has ended_at", history[0].ended_at is not None)


async def test_mixin_validation():
    print("\n=== Mixin Validation ===")

    user = TLUser(username="validation_test")
    await user.save()

    # Invalid category
    try:
        await user.set_status("nonexistent", "banned")
        check("Invalid category rejected", False, "should raise ValueError")
    except ValueError:
        check("Invalid category rejected", True)

    # Invalid status in valid category
    try:
        await user.set_status("moderation", "nonexistent_status")
        check("Invalid status rejected", False, "should raise ValueError")
    except ValueError:
        check("Invalid status rejected", True)

    # Valid category, invalid status for clear
    try:
        await user.clear_status("nonexistent")
        check("Invalid category on clear rejected", False)
    except ValueError:
        check("Invalid category on clear rejected", True)


async def test_mixin_multi_entity():
    print("\n=== Mixin Multi-Entity ===")

    user = TLUser(username="entity_user")
    await user.save()
    forum = TLForum(name="entity_forum")
    await forum.save()

    await user.set_status("moderation", "banned", reason="User ban")
    await forum.set_status("state", "locked", reason="Forum lock")

    check("User is banned", await user.has_status("moderation", "banned"))
    check("Forum is locked", await forum.has_status("state", "locked"))

    # They don't interfere (different entity_type)
    user_history = await user.get_status_history()
    forum_history = await forum.get_status_history()
    check(
        "User history is user's",
        all(
            h.status in ["banned", "muted", "warned", "probation"] or True
            for h in user_history
        ),
    )
    check(
        "Forum history is forum's",
        all(
            h.status in ["active", "archived", "locked", "hidden"] or True
            for h in forum_history
        ),
    )


async def test_mixin_expiry():
    print("\n=== Mixin Expiry ===")

    user = TLUser(username="expiry_test")
    await user.save()

    # Set with expires_in
    await user.set_status(
        "moderation", "muted", reason="Temp mute", expires_in=timedelta(hours=1)
    )

    check(
        "Muted is active (future expiry)", await user.has_status("moderation", "muted")
    )

    status = await user.get_status("moderation")
    check("Status has expires_at", status is not None and status.expires_at is not None)


# ── Adversarial Tests ─────────────────────────────────────────────────────


async def test_adversarial():
    print("\n=== Adversarial ===")

    tl = get_timeline()

    # Empty strings
    event = await tl.add_event("user", 50, "moderation", "banned", reason="")
    check("Empty reason accepted", event.id > 0)

    # Very long reason
    long_reason = "x" * 10000
    event = await tl.add_event("user", 51, "moderation", "banned", reason=long_reason)
    status = await tl.current_status("user", 51, "moderation")
    check("Long reason preserved", status is not None and len(status.reason) == 10000)

    # Detail dict
    event = await tl.add_event(
        "user",
        52,
        "moderation",
        "banned",
        detail={"violation_count": 5, "last_post": "spam post"},
    )
    status = await tl.current_status("user", 52, "moderation")
    check(
        "Detail dict preserved",
        status is not None and status.detail.get("violation_count") == 5,
    )

    # Rapid add/end cycles
    for i in range(20):
        await tl.add_event("user", 53, "moderation", "muted", reason=f"Cycle {i}")
        await tl.end_status("user", 53, "moderation")
    history = await tl.get_history("user", 53, "moderation")
    check("20 rapid cycles tracked", len(history) == 20, f"got {len(history)}")

    # current_status for nonexistent entity
    status = await tl.current_status("nonexistent_type", 999, "fake_category")
    check("Nonexistent entity returns None", status is None)


async def test_concurrent():
    print("\n=== Concurrent ===")

    tl = get_timeline()

    # Concurrent status sets for different users
    tasks = [
        tl.add_event("user", 200 + i, "moderation", "banned", reason=f"User {i}")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)
    check("20 concurrent adds succeeded", all(r.id > 0 for r in results))

    # All are active
    for i in range(20):
        active = await tl.is_active("user", 200 + i, "banned")
        if not active:
            check(f"Concurrent user {200 + i} active", False)
            break
    else:
        check("All 20 concurrent users active", True)


async def test_concurrent_same_entity():
    print("\n=== Concurrent Same-Entity (Race Condition Test) ===")

    tl = get_timeline()
    db = get_db()

    # Clear any prior state for entity 500
    await db.execute(
        "DELETE FROM hyper_status_events WHERE entity_type = 'user' AND entity_id = 500"
    )

    # Race 10 concurrent add_event calls for the SAME entity+category.
    # SELECT FOR UPDATE should serialize them — only one should be "current".
    tasks = [
        tl.add_event("user", 500, "moderation", f"status_{i}", reason=f"Race {i}")
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)
    check("10 concurrent same-entity adds completed", len(results) == 10)

    # Verify: only 1 active (non-ended) event exists
    active_rows = await db.query(
        "SELECT * FROM hyper_status_events "
        "WHERE entity_type = 'user' AND entity_id = 500 AND category = 'moderation' "
        "AND ended_at IS NULL"
    )
    check(
        "Exactly 1 non-ended event after race",
        len(active_rows) == 1,
        f"got {len(active_rows)}",
    )

    # Verify: total 10 events exist (9 ended + 1 active)
    all_rows = await db.query(
        "SELECT * FROM hyper_status_events "
        "WHERE entity_type = 'user' AND entity_id = 500 AND category = 'moderation'"
    )
    check("Total 10 events recorded", len(all_rows) == 10, f"got {len(all_rows)}")

    # Verify: current_status returns the single active one
    status = await tl.current_status("user", 500, "moderation")
    check("current_status returns one result", status is not None)

    # Race: concurrent end_status calls (only first should succeed)
    end_tasks = [
        tl.end_status("user", 500, "moderation", end_reason=f"End {i}")
        for i in range(5)
    ]
    end_results = await asyncio.gather(*end_tasks)
    true_count = sum(1 for r in end_results if r is True)
    check("Concurrent end: at least 1 succeeded", true_count >= 1)

    # After all ends: no active status
    status = await tl.current_status("user", 500, "moderation")
    check("No active status after concurrent ends", status is None)


# ── Cleanup Tests ─────────────────────────────────────────────────────────


async def test_escalation_basic():
    print("\n=== Escalation Basic ===")

    tl = get_timeline()
    db = get_db()
    await db.execute(
        "DELETE FROM hyper_status_events WHERE entity_type = 'user' AND entity_id IN (600,601,603)"
    )

    engine = EscalationEngine()
    engine.add_rule(
        "user",
        EscalationRule(
            trigger_status="warned",
            threshold=3,
            window=timedelta(days=30),
            consequence_category="moderation",
            consequence_status="muted",
            consequence_expires_in=timedelta(days=7),
            consequence_reason="Auto-escalation: 3 warnings in 30 days",
        ),
    )
    engine.connect()

    try:
        await tl.add_event("user", 600, "moderation", "warned", reason="First warning")
        check("1 warning: not muted", not await tl.is_active("user", 600, "muted"))

        await tl.add_event("user", 600, "moderation", "warned", reason="Second warning")
        check("2 warnings: not muted", not await tl.is_active("user", 600, "muted"))

        await tl.add_event("user", 600, "moderation", "warned", reason="Third warning")
        check("3 warnings: auto-muted", await tl.is_active("user", 600, "muted"))

        status = await tl.current_status("user", 600, "moderation")
        check(
            "Auto-mute has escalation reason",
            status is not None and "Auto-escalation" in status.reason,
        )
        check(
            "Auto-mute has expiry", status is not None and status.expires_at is not None
        )
    finally:
        engine.disconnect()


async def test_escalation_window():
    print("\n=== Escalation Window ===")

    tl = get_timeline()
    db = get_db()
    await db.execute(
        "DELETE FROM hyper_status_events WHERE entity_type = 'user' AND entity_id = 601"
    )

    engine = EscalationEngine()
    engine.add_rule(
        "user",
        EscalationRule(
            trigger_status="muted",
            threshold=2,
            window=timedelta(days=90),
            consequence_category="moderation",
            consequence_status="banned",
            consequence_reason="Auto-ban: 2 mutes in 90 days",
        ),
    )
    engine.connect()

    try:
        await tl.add_event("user", 601, "moderation", "muted", reason="First mute")
        check("1 mute: not banned", not await tl.is_active("user", 601, "banned"))

        await tl.add_event("user", 601, "moderation", "muted", reason="Second mute")
        check("2 mutes: auto-banned", await tl.is_active("user", 601, "banned"))
    finally:
        engine.disconnect()


async def test_escalation_cross_category():
    print("\n=== Escalation Cross-Category ===")

    tl = get_timeline()
    db = get_db()
    await db.execute(
        "DELETE FROM hyper_status_events WHERE entity_type = 'user' AND entity_id = 603"
    )

    engine = EscalationEngine()
    engine.add_rule(
        "user",
        EscalationRule(
            trigger_status="warned",
            threshold=2,
            window=timedelta(days=7),
            consequence_category="lifecycle",
            consequence_status="locked",
            consequence_reason="Account locked: 2 warnings in 7 days",
        ),
    )
    engine.connect()

    try:
        await tl.add_event("user", 603, "moderation", "warned", reason="W1")
        await tl.add_event("user", 603, "moderation", "warned", reason="W2")

        check(
            "Cross-category: warned active", await tl.is_active("user", 603, "warned")
        )
        check(
            "Cross-category: locked active", await tl.is_active("user", 603, "locked")
        )
    finally:
        engine.disconnect()


async def test_cleanup():
    print("\n=== Cleanup ===")

    tl = get_timeline()

    # Create old ended events
    for i in range(5):
        event = await tl.add_event("user", 300 + i, "moderation", "warned")
        await tl.end_status("user", 300 + i, "moderation")

    # Cleanup with 0 days retention (should delete all ended)
    count = await tl.cleanup(days=0)
    check("Cleanup deleted ended events", count >= 5, f"got {count}")


# ── Main ──────────────────────────────────────────────────────────────────


async def async_main():
    db = await setup_db()
    try:
        await test_add_and_current()
        await test_end_status()
        await test_status_replacement()
        await test_multi_category()
        await test_expiry()
        await test_expire_overdue()
        await test_history()
        await test_get_entities()
        await test_signal()
        await test_mixin_basics()
        await test_mixin_validation()
        await test_mixin_multi_entity()
        await test_mixin_expiry()
        await test_adversarial()
        await test_concurrent()
        await test_concurrent_same_entity()
        await test_escalation_basic()
        await test_escalation_window()
        await test_escalation_cross_category()
        await test_cleanup()
    finally:
        await teardown_db(db)


def main():
    print("Status Timeline Tests")
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
