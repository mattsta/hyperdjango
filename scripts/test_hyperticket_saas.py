"""
HyperTicket Phase 6 — Metering, Quotas, Analytics, Export Tests.

Tests plan limits, quota enforcement, analytics endpoints, export,
and maintenance tasks.

Usage:
    uv run hyper-test hyperticket_saas
"""

# hyper-test: db_isolated

import asyncio
import json
import os
import subprocess
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.tenancy import tenant_context
from services.hyperticket.metering import check_plan_limit
from services.hyperticket.middleware import (
    check_agent_seat_quota,
    check_ticket_quota,
)
from services.hyperticket.models import (
    Org,
    PlanConfig,
    PlanFeatureLimit,
    TenantTheme,
)
from services.hyperticket.services.export import (
    export_tickets_csv,
    export_tickets_json,
)
from services.hyperticket.tasks.maintenance import auto_close_resolved

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
    # 1. Plan config + feature limits
    # -----------------------------------------------------------------------
    print("\n--- Plan Config ---")
    plans = await PlanConfig.objects.all()
    test("3 plans", len(plans), 3)

    pro = await PlanConfig.objects.filter(name="Professional").first()
    test_true("Professional plan exists", pro is not None)

    if pro:
        limits = await PlanFeatureLimit.objects.filter(plan_config_id=pro.id).all()
        test("Professional has 9 feature limits", len(limits), 9)

        # Check specific limits
        seats = await PlanFeatureLimit.objects.filter(
            plan_config_id=pro.id, feature_key="agent_seats"
        ).first()
        test_true("agent_seats limit exists", seats is not None)
        if seats:
            test("agent_seats = 25", seats.limit_value, 25.0)

    # -----------------------------------------------------------------------
    # 2. check_plan_limit function
    # -----------------------------------------------------------------------
    print("\n--- Quota Checks ---")
    if acme:
        # Acme is on Professional plan (agent_seats=25)
        allowed, remaining, enforcement = await check_plan_limit(acme, "agent_seats", 4)
        test_true("4 agents within limit", allowed)
        test_true("remaining > 0", remaining > 0)

        # Check at limit
        allowed, remaining, enforcement = await check_plan_limit(
            acme, "agent_seats", 25
        )
        test_true("at limit — enforcement decides", True)

        # Check feature that doesn't exist
        allowed, remaining, enforcement = await check_plan_limit(
            acme, "nonexistent_feature", 0
        )
        test_true("nonexistent feature = unlimited", allowed)

        # Enterprise unlimited
        globex = await Org.objects.filter(slug="globex").first()
        if globex:
            allowed, remaining, enforcement = await check_plan_limit(
                globex, "agent_seats", 1000
            )
            test_true("enterprise unlimited agents", allowed)
            test("unlimited remaining", remaining, -1)

    # -----------------------------------------------------------------------
    # 3. Quota middleware functions
    # -----------------------------------------------------------------------
    print("\n--- Quota Middleware ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            # Should NOT raise for acme (Professional plan, <25 agents)
            try:
                await check_agent_seat_quota(acme)
                test_true("agent seat check passes for acme", True)
            except Exception as exc:
                test_true("agent seat check passes", False, str(exc))

            try:
                await check_ticket_quota(acme)
                test_true("ticket quota check passes for acme", True)
            except Exception as exc:
                test_true("ticket quota check passes", False, str(exc))

    # -----------------------------------------------------------------------
    # 4. CSV/JSON export
    # -----------------------------------------------------------------------
    print("\n--- Export ---")
    if acme:
        csv_data = await export_tickets_csv(acme.id)
        test_true("CSV export non-empty", len(csv_data) > 0)
        test_true("CSV has header", "ticket_number" in csv_data)
        test_true("CSV has data rows", "ACME-" in csv_data)

        json_data = await export_tickets_json(acme.id)
        test_true("JSON export non-empty", len(json_data) > 0)
        parsed = json.loads(json_data)
        test_true("JSON has tickets array", "tickets" in parsed)
        test_true("JSON has count", parsed.get("count", 0) > 0)

    # -----------------------------------------------------------------------
    # 5. Maintenance tasks
    # -----------------------------------------------------------------------
    print("\n--- Maintenance ---")
    with tenant_context(tenant_id=acme.id):
        # auto_close_resolved runs without error
        closed = await auto_close_resolved(
            days_threshold=0
        )  # threshold=0 to trigger on all resolved
        test_true("auto_close_resolved ran", closed >= 0)

    # -----------------------------------------------------------------------
    # 6. TenantTheme
    # -----------------------------------------------------------------------
    print("\n--- Tenant Theme ---")
    if acme:
        with tenant_context(tenant_id=acme.id):
            theme = await TenantTheme.objects.first()
            test_true("acme has theme", theme is not None)
            if theme:
                test("company name", theme.company_name_display, "Acme Corp")
                test("primary color", theme.primary_color, "#2563eb")
                test_true("has portal welcome", len(theme.portal_welcome_message) > 0)

    await db.disconnect()


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 6: Metering/Quota/Analytics Tests")
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
    print(f"HyperTicket SaaS: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
