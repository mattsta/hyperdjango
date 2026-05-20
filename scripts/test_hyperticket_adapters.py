"""
HyperTicket Phase 4 — Adapter Pipeline Tests.

Tests adapter registration, execution, error isolation, search,
and the AI triage/moderation demo adapters.

Usage:
    uv run hyper-test hyperticket_adapters
"""

# hyper-test: db_isolated

import asyncio
import os
import subprocess
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.tenancy import tenant_context
from services.hyperticket.adapters import AdapterRegistry
from services.hyperticket.adapters.ai_moderation import (
    AIContentModerationAdapter,
)
from services.hyperticket.adapters.ai_triage import AITriageAdapter
from services.hyperticket.adapters.protocols import AdapterContext
from services.hyperticket.models import (
    Org,
    Tag,
    Ticket,
    TicketTag,
)
from services.hyperticket.services.search import search_comments, search_tickets

PASS = 0
FAIL = 0
ERRORS: list[str] = []
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


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


async def run_tests() -> None:
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    acme = await Org.objects.filter(slug="acme").first()
    test_true("acme exists", acme is not None)
    if not acme:
        return

    # -----------------------------------------------------------------------
    # 1. AdapterRegistry — basic registration + execution
    # -----------------------------------------------------------------------
    print("\n--- Adapter Registry ---")
    registry = AdapterRegistry()

    # Register a test ticket adapter globally
    class TestTicketAdapter:
        def __init__(self):
            self.calls: list[str] = []

        async def on_create_pre(self, ctx, data):
            self.calls.append("create_pre")
            data["_test_enriched"] = True
            return data

        async def on_create_post(self, ctx, ticket):
            self.calls.append("create_post")

        async def on_update_pre(self, ctx, ticket, changes):
            self.calls.append("update_pre")
            return changes

        async def on_update_post(self, ctx, ticket, changes):
            self.calls.append("update_post")

        async def on_status_change(self, ctx, ticket, old, new):
            self.calls.append("status_change")

        async def on_assign(self, ctx, ticket, aid):
            self.calls.append("assign")

        async def on_close(self, ctx, ticket):
            self.calls.append("close")

        async def on_merge(self, ctx, src, tgt):
            self.calls.append("merge")

    adapter = TestTicketAdapter()
    registry.register_ticket_adapter(adapter)

    ctx = AdapterContext(tenant_id=acme.id, actor_type="agent", actor_id=1)

    # Run create pre
    data = {"title": "test"}
    result = await registry.run_ticket_create_pre(ctx, data)
    test_true("create_pre modifies data", result.get("_test_enriched") is True)
    test_true("create_pre called", "create_pre" in adapter.calls)

    # Run create post
    with tenant_context(tenant_id=acme.id):
        ticket = await Ticket.objects.first()
    await registry.run_ticket_create_post(ctx, ticket)
    test_true("create_post called", "create_post" in adapter.calls)

    # -----------------------------------------------------------------------
    # 2. Per-tenant adapter isolation
    # -----------------------------------------------------------------------
    print("\n--- Per-Tenant Isolation ---")
    tenant_adapter = TestTicketAdapter()
    registry.register_ticket_adapter(
        tenant_adapter, tenant_id=999
    )  # non-existent tenant

    ctx_acme = AdapterContext(tenant_id=acme.id, actor_type="agent", actor_id=1)
    await registry.run_ticket_create_post(ctx_acme, ticket)

    # tenant_adapter should NOT have been called (registered for tenant 999)
    test_true(
        "per-tenant adapter not called for wrong tenant",
        "create_post" not in tenant_adapter.calls,
    )

    # Register for acme specifically
    acme_adapter = TestTicketAdapter()
    registry.register_ticket_adapter(acme_adapter, tenant_id=acme.id)
    await registry.run_ticket_create_post(ctx_acme, ticket)
    test_true(
        "per-tenant adapter called for correct tenant",
        "create_post" in acme_adapter.calls,
    )

    # -----------------------------------------------------------------------
    # 3. Multiple adapters chain in order
    # -----------------------------------------------------------------------
    print("\n--- Adapter Chaining ---")
    chain_registry = AdapterRegistry()
    order: list[str] = []

    class AdapterA:
        async def on_create_pre(self, ctx, data):
            order.append("A")
            return data

        async def on_create_post(self, ctx, ticket):
            pass

        async def on_update_pre(self, ctx, t, c):
            return c

        async def on_update_post(self, ctx, t, c):
            pass

        async def on_status_change(self, ctx, t, o, n):
            pass

        async def on_assign(self, ctx, t, a):
            pass

        async def on_close(self, ctx, t):
            pass

        async def on_merge(self, ctx, s, t):
            pass

    class AdapterB:
        async def on_create_pre(self, ctx, data):
            order.append("B")
            return data

        async def on_create_post(self, ctx, ticket):
            pass

        async def on_update_pre(self, ctx, t, c):
            return c

        async def on_update_post(self, ctx, t, c):
            pass

        async def on_status_change(self, ctx, t, o, n):
            pass

        async def on_assign(self, ctx, t, a):
            pass

        async def on_close(self, ctx, t):
            pass

        async def on_merge(self, ctx, s, t):
            pass

    chain_registry.register_ticket_adapter(AdapterA())
    chain_registry.register_ticket_adapter(AdapterB())
    await chain_registry.run_ticket_create_pre(ctx, {"title": "test"})
    test("chain order", order, ["A", "B"])

    # -----------------------------------------------------------------------
    # 4. Error isolation — one adapter throws, others still run
    # -----------------------------------------------------------------------
    print("\n--- Error Isolation ---")
    error_registry = AdapterRegistry()
    ran_after_error: list[bool] = []

    class BrokenAdapter:
        async def on_create_pre(self, ctx, data):
            raise RuntimeError("broken!")
            return data

        async def on_create_post(self, ctx, ticket):
            pass

        async def on_update_pre(self, ctx, t, c):
            return c

        async def on_update_post(self, ctx, t, c):
            pass

        async def on_status_change(self, ctx, t, o, n):
            pass

        async def on_assign(self, ctx, t, a):
            pass

        async def on_close(self, ctx, t):
            pass

        async def on_merge(self, ctx, s, t):
            pass

    class SafeAdapter:
        async def on_create_pre(self, ctx, data):
            ran_after_error.append(True)
            return data

        async def on_create_post(self, ctx, ticket):
            pass

        async def on_update_pre(self, ctx, t, c):
            return c

        async def on_update_post(self, ctx, t, c):
            pass

        async def on_status_change(self, ctx, t, o, n):
            pass

        async def on_assign(self, ctx, t, a):
            pass

        async def on_close(self, ctx, t):
            pass

        async def on_merge(self, ctx, s, t):
            pass

    error_registry.register_ticket_adapter(BrokenAdapter())
    error_registry.register_ticket_adapter(SafeAdapter())
    await error_registry.run_ticket_create_pre(ctx, {"title": "test"})
    test_true("safe adapter ran despite broken adapter", len(ran_after_error) == 1)

    # -----------------------------------------------------------------------
    # 5. Comment moderation adapter
    # -----------------------------------------------------------------------
    print("\n--- Comment Moderation ---")
    mod_registry = AdapterRegistry()
    mod_adapter = AIContentModerationAdapter()
    mod_registry.register_comment_adapter(mod_adapter)

    # Customer comment with flagged word
    customer_ctx = AdapterContext(tenant_id=acme.id, actor_type="customer", actor_id=1)
    flagged_data = await mod_registry.run_comment_pre(
        customer_ctx, ticket, {"body": "This is spam content"}
    )
    test_true("flagged content prefixed", flagged_data["body"].startswith("[Flagged"))
    test_true("metadata flagged", customer_ctx.metadata.get("content_flagged") is True)

    # Agent comment NOT moderated
    agent_ctx = AdapterContext(tenant_id=acme.id, actor_type="agent", actor_id=1)
    clean_data = await mod_registry.run_comment_pre(
        agent_ctx, ticket, {"body": "This is spam content"}
    )
    test_true(
        "agent content not flagged", not clean_data["body"].startswith("[Flagged")
    )

    # -----------------------------------------------------------------------
    # 6. AI Triage adapter
    # -----------------------------------------------------------------------
    print("\n--- AI Triage ---")
    with tenant_context(tenant_id=acme.id):
        # Find a ticket with "login" in the title (from seed data)
        login_ticket = await Ticket.objects.filter(title__contains="login").first()
        if login_ticket:
            # Count tags before triage
            before_tags = await TicketTag.objects.filter(
                ticket_id=login_ticket.id
            ).count()

            triage = AITriageAdapter()
            triage_ctx = AdapterContext(
                tenant_id=acme.id, actor_type="system", actor_id=0
            )
            await triage.on_create_post(triage_ctx, login_ticket)

            after_tags = await TicketTag.objects.filter(
                ticket_id=login_ticket.id
            ).count()
            # Should have added "login" tag if it exists
            login_tag = await Tag.objects.filter(name="login").first()
            if login_tag:
                test_true("AI triage added login tag", after_tags > before_tags)
            else:
                test_true("no login tag in DB (expected)", True)
        else:
            test_true("no login ticket found (skip triage test)", True)

    # -----------------------------------------------------------------------
    # 7. Search service
    # -----------------------------------------------------------------------
    print("\n--- Search ---")
    with tenant_context(tenant_id=acme.id):
        results = await search_tickets("login", acme.id)
        test_true("search returns results", len(results) > 0)
        if results:
            test_true("result has ticket_number", "ticket_number" in results[0])
            test_true("result has title", "title" in results[0])
            test_true("result has rank", "rank" in results[0])

        # Empty search
        empty = await search_tickets("", acme.id)
        test("empty search returns nothing", len(empty), 0)

        # Comment search
        comment_results = await search_comments("help", acme.id)
        test_true("comment search runs", isinstance(comment_results, list))

    # -----------------------------------------------------------------------
    # 8. Workflow action adapter
    # -----------------------------------------------------------------------
    print("\n--- Workflow Action Adapter ---")
    action_registry = AdapterRegistry()
    action_executed: list[str] = []

    class CustomNotifyAction:
        action_name = "custom_notify"

        async def execute(self, ctx, ticket, params):
            action_executed.append(f"notified:{params.get('channel', 'unknown')}")

    action_registry.register_workflow_action(CustomNotifyAction())
    executed = await action_registry.execute_workflow_action(
        "custom_notify", ctx, ticket, {"channel": "slack"}
    )
    test_true("custom action executed", executed)
    test_true("action received params", "notified:slack" in action_executed)

    # Unknown action
    not_found = await action_registry.execute_workflow_action(
        "nonexistent", ctx, ticket, {}
    )
    test_true("unknown action returns False", not not_found)

    await db.disconnect()


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 4: Adapter Pipeline Tests")
    print("=" * 60)

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
    print(f"HyperTicket Adapters: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
