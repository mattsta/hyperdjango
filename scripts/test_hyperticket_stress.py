"""
HyperTicket Phase 7 — Stress + Concurrency Tests.

Tests concurrent tenants, bulk operations, search performance,
SLA cron at scale, and tenant isolation under load.

Usage:
    uv run hyper-test hyperticket_stress
"""

# hyper-test: db_isolated

import asyncio
import os
import subprocess
import sys
import time

from hyperdjango.database import Database, set_db
from hyperdjango.tenancy import tenant_context
from services.hyperticket.models import (
    Customer,
    Org,
    PriorityConfig,
    SLAInstance,
    Ticket,
    TicketStatusConfig,
    TicketTypeConfig,
)
from services.hyperticket.services.search import search_tickets
from services.hyperticket.services.sla_engine import sla_engine
from services.hyperticket.tasks.sla import check_sla_breaches

PASS = 0
FAIL = 0
ERRORS: list[str] = []
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

# Wall-clock budget assertions are only meaningful on a serial run. Under the
# parallel suite (24+ workers contending for CPU/DB), timing variance makes them
# flaky — a slow scheduler tick is not a correctness regression. Enforce them
# only when we know we are NOT racing other workers; otherwise report the
# measurement without asserting. The deterministic row-count / isolation checks
# alongside each timing check always run.
_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"


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


def test_timing(name: str, elapsed: float, budget: float) -> None:
    """Assert a wall-clock budget only on serial runs; else just report."""
    if _PARALLEL:
        print(f"  (timing skipped under parallel load) {name}: {elapsed:.3f}s")
        return
    test_true(name, elapsed < budget, f"took {elapsed:.3f}s (budget {budget}s)")


async def run_tests() -> None:
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    acme = await Org.objects.filter(slug="acme").first()
    globex = await Org.objects.filter(slug="globex").first()
    test_true("acme exists", acme is not None)
    test_true("globex exists", globex is not None)
    if not acme or not globex:
        return

    # -----------------------------------------------------------------------
    # 1. Bulk ticket creation — 50 tickets per tenant
    # -----------------------------------------------------------------------
    print("\n--- Bulk Ticket Creation ---")
    for org in [acme, globex]:
        with tenant_context(tenant_id=org.id):
            default_status = await TicketStatusConfig.objects.filter(
                is_default=True
            ).first()
            default_priority = await PriorityConfig.objects.filter(
                is_default=True
            ).first()
            default_type = await TicketTypeConfig.objects.first()
            customer = await Customer.objects.first()

            if not all([default_status, default_priority, default_type, customer]):
                test_true(f"{org.slug} has required config", False)
                continue

            before = await Ticket.objects.count()
            start = time.monotonic()

            for i in range(50):
                t = Ticket(
                    tenant_id=org.id,
                    ticket_number=f"{org.slug.upper()}-STRESS-{i:04d}",
                    title=f"Stress test ticket {i} for {org.slug}",
                    description=f"Description for stress test {i}",
                    status_id=default_status.id,
                    priority_id=default_priority.id,
                    ticket_type_id=default_type.id,
                    customer_id=customer.id,
                )
                await t.save()

            elapsed = time.monotonic() - start
            after = await Ticket.objects.count()
            test_true(
                f"{org.slug}: 50 tickets created",
                after - before == 50,
                f"got {after - before}",
            )
            test_timing(f"{org.slug}: bulk create <5s", elapsed, 5.0)

    # -----------------------------------------------------------------------
    # 2. Tenant isolation under load — cross-check
    # -----------------------------------------------------------------------
    print("\n--- Tenant Isolation Under Load ---")
    with tenant_context(tenant_id=acme.id):
        acme_tickets = await Ticket.objects.count()
        acme_stress = await Ticket.objects.filter(title__contains="Stress test").count()

    with tenant_context(tenant_id=globex.id):
        globex_tickets = await Ticket.objects.count()
        globex_stress = await Ticket.objects.filter(
            title__contains="Stress test"
        ).count()

    test_true("acme has its stress tickets", acme_stress >= 50)
    test_true("globex has its stress tickets", globex_stress >= 50)

    # Cross-check: acme stress tickets should NOT contain globex slug
    with tenant_context(tenant_id=acme.id):
        leaked = await Ticket.objects.filter(
            ticket_number__contains="GLOBEX-STRESS"
        ).count()
        test_true("no globex tickets leaked to acme", leaked == 0, f"found {leaked}")

    with tenant_context(tenant_id=globex.id):
        leaked = await Ticket.objects.filter(
            ticket_number__contains="ACME-STRESS"
        ).count()
        test_true("no acme tickets leaked to globex", leaked == 0, f"found {leaked}")

    # -----------------------------------------------------------------------
    # 3. SLA breach check at scale
    # -----------------------------------------------------------------------
    print("\n--- SLA at Scale ---")
    # Create SLA instances for stress tickets
    with tenant_context(tenant_id=acme.id):
        stress_tickets = (
            await Ticket.objects.filter(ticket_number__contains="ACME-STRESS")
            .limit(20)
            .all()
        )
        for t in stress_tickets:
            await sla_engine.create_instance(t)

        sla_count = await SLAInstance.objects.count()
        test_true("SLA instances created", sla_count >= 20, f"got {sla_count}")

        start = time.monotonic()
        breached = await check_sla_breaches()
        elapsed = time.monotonic() - start
        test_timing("SLA check completes <5s", elapsed, 5.0)

    # -----------------------------------------------------------------------
    # 4. Search performance
    # -----------------------------------------------------------------------
    print("\n--- Search Performance ---")
    with tenant_context(tenant_id=acme.id):
        start = time.monotonic()
        results = await search_tickets("stress test", acme.id, limit=25)
        elapsed = time.monotonic() - start
        test_true("search returns results", len(results) > 0, f"got {len(results)}")
        test_timing("search <1s", elapsed, 1.0)

    # -----------------------------------------------------------------------
    # 5. Bulk ORM operations
    # -----------------------------------------------------------------------
    print("\n--- Bulk ORM Ops ---")
    with tenant_context(tenant_id=acme.id):
        # Bulk update: set all stress tickets to a different priority
        priorities = await PriorityConfig.objects.all()
        if len(priorities) >= 2:
            new_priority = priorities[1].id
            start = time.monotonic()
            await Ticket.objects.filter(ticket_number__contains="ACME-STRESS").update(
                priority_id=new_priority
            )
            elapsed = time.monotonic() - start
            test_timing("bulk update 50 tickets <2s", elapsed, 2.0)

            # Verify update
            updated = await Ticket.objects.filter(
                ticket_number__contains="ACME-STRESS",
                priority_id=new_priority,
            ).count()
            test_true("all 50 updated", updated == 50, f"got {updated}")

    # -----------------------------------------------------------------------
    # 6. Concurrent tenant queries
    # -----------------------------------------------------------------------
    print("\n--- Concurrent Queries ---")

    async def count_tenant_tickets(org_id: int) -> int:
        with tenant_context(tenant_id=org_id):
            return await Ticket.objects.count()

    start = time.monotonic()
    acme_count, globex_count = await asyncio.gather(
        count_tenant_tickets(acme.id),
        count_tenant_tickets(globex.id),
    )
    elapsed = time.monotonic() - start
    test_true(
        "concurrent counts complete",
        acme_count > 0 and globex_count > 0,
        f"acme={acme_count}, globex={globex_count}",
    )
    test_timing("concurrent queries <1s", elapsed, 1.0)

    await db.disconnect()


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 7: Stress + Concurrency Tests")
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
    print(f"HyperTicket Stress: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
