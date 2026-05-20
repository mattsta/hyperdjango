"""
HyperTicket Phase 7 — Full Lifecycle E2E Tests.

Tests the complete user journey against a live server:
  - Agent login → dashboard → create ticket → assign → comment → close
  - Customer register → submit ticket → track → comment → rate
  - Multi-tenant isolation (org1 + org2 simultaneously)
  - Admin panel access
  - Search, export, analytics pages

Usage:
    uv run hyper-test e2e_hyperticket
"""

# hyper-test: e2e

import os
import re
import subprocess
import sys
import urllib.parse

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    Session,
    http_get,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, response, expected_status=200):
    global PASS, FAIL
    if response.status == expected_status:
        PASS += 1
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected_status}, got {response.status}"
    print(msg)
    ERRORS.append(msg)
    if response.body:
        print(f"        body: {response.body[:200]}")
    return False


def check_true(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        return True
    FAIL += 1
    msg = f"  FAIL  {name}"
    if detail:
        msg += f": {detail}"
    print(msg)
    ERRORS.append(msg)
    return False


def form_encode(data: dict[str, str]) -> str:
    return urllib.parse.urlencode(data)


class TenantSession(Session):
    """Session with X-Tenant-ID on every request + CSRF handling."""

    def __init__(self, base_url: str, tenant_id: int):
        super().__init__(base_url)
        self.tenant_id = tenant_id

    def _headers(self, extra=None, include_csrf=False):
        h = super()._headers(extra, include_csrf)
        h["X-Tenant-ID"] = str(self.tenant_id)
        return h

    def form_post(self, path: str, data: dict[str, str]):
        csrf = self.cookie_jar.get("csrftoken", "")
        if csrf:
            data["_csrf_token"] = csrf
        return self.post(
            path,
            body=form_encode(data),
            content_type="application/x-www-form-urlencoded",
        )

    def ensure_csrf(self, path: str = "/auth/agent/login"):
        if "csrftoken" not in self.cookie_jar:
            self.get(path)


def find_ticket_id(html: str) -> str | None:
    """Extract first ticket ID from /tickets/N link in HTML."""
    match = re.search(r"/tickets/(\d+)", html)
    return match.group(1) if match else None


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 7: Full Lifecycle E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["hyperticket"]

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
        # Hyperticket has 27+ models with cross-FK seed data; under
        # parallel CI load with multiple `hyper setup` subprocess
        # invocations contending for the database, the previous 60s
        # ceiling could trip a TimeoutExpired even though the seed
        # would eventually finish. 180s gives comfortable headroom.
        timeout=180 if os.environ.get("HYPER_TEST_PARALLEL") == "1" else 90,
    )
    if result.returncode != 0:
        print(f"Setup failed:\n{result.stderr[-500:]}\n{result.stdout[-500:]}")
        sys.exit(1)
    print("Setup complete.")

    with AppRunner(
        "services.hyperticket.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        acme_id = 1
        globex_id = 2

        # ==============================================================
        # 1. Health check
        # ==============================================================
        print("--- Health ---")
        r = http_get(f"{base}/health")
        check("health", r, 200)

        # ==============================================================
        # 2. Agent login flow — Acme admin
        # ==============================================================
        print("\n--- Agent Login Flow ---")
        acme = TenantSession(base, acme_id)
        acme.ensure_csrf()

        r = acme.get("/auth/agent/login")
        check("login page renders", r, 200)
        check_true("login page has form", "email" in r.body and "password" in r.body)

        r = acme.form_post(
            "/auth/agent/login",
            {
                "email": "admin@acme.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("login → redirect to dashboard", r, 302)

        r = acme.get("/dashboard/")
        check("dashboard page", r, 200)
        check_true(
            "dashboard has stats",
            "Total Tickets" in r.body or "total_tickets" in r.body.lower(),
        )

        # ==============================================================
        # 3. Ticket list + create
        # ==============================================================
        print("\n--- Ticket CRUD ---")
        r = acme.get("/tickets/")
        check("ticket list", r, 200)
        check_true("list has seeded tickets", "ACME-" in r.body)

        r = acme.get("/tickets/new")
        check("create form", r, 200)
        check_true("form has title field", "title" in r.body)

        # Find a ticket to interact with
        r = acme.get("/tickets/")
        tid = find_ticket_id(r.body)
        check_true("found a ticket ID", tid is not None)

        if tid:
            r = acme.get(f"/tickets/{tid}")
            check("ticket detail", r, 200)
            check_true("detail has ticket number", "ACME-" in r.body)
            check_true("detail has comments section", "Comments" in r.body)
            check_true("detail has actions", "Close" in r.body or "Lock" in r.body)

            # Add comment
            r = acme.form_post(
                f"/tickets/{tid}/comments",
                {
                    "body": "E2E agent comment on ticket",
                },
            )
            check("add comment", r, 302)

            # Add internal note
            r = acme.form_post(
                f"/tickets/{tid}/comments",
                {
                    "body": "Internal agent note - should not show on portal",
                    "is_internal": "on",
                },
            )
            check("add internal note", r, 302)

            # Lock
            r = acme.form_post(f"/tickets/{tid}/lock", {})
            check("lock ticket", r, 302)

            # Unlock (toggle)
            r = acme.form_post(f"/tickets/{tid}/lock", {})
            check("unlock ticket", r, 302)

            # Timeline
            r = acme.get(f"/tickets/{tid}/timeline")
            check("timeline", r, 200)
            check_true("timeline has entries", "timeline" in r.body)

        # ==============================================================
        # 4. Search
        # ==============================================================
        print("\n--- Search ---")
        r = acme.get("/search/")
        check("search page (empty)", r, 200)
        check_true("search has input", "Search" in r.body)

        r = acme.get("/search/?q=login")
        check("search with query", r, 200)
        check_true("search has results", "login" in r.body.lower() or "ACME-" in r.body)

        # ==============================================================
        # 5. Teams + Tags + Agents pages
        # ==============================================================
        print("\n--- Navigation Pages ---")
        r = acme.get("/teams/")
        check("teams page", r, 200)
        check_true("teams rendered", "Engineering" in r.body or "Billing" in r.body)

        r = acme.get("/tags/")
        check("tags page", r, 200)
        check_true("tags rendered", "billing" in r.body or "login" in r.body)

        r = acme.get("/agents/")
        check("agents page (admin)", r, 200)
        check_true("agents rendered", "admin@acme.com" in r.body or "Admin" in r.body)

        # ==============================================================
        # 6. Org settings (admin)
        # ==============================================================
        print("\n--- Org Settings ---")
        r = acme.get("/admin/settings/")
        check("org settings page", r, 200)
        check_true(
            "settings has timezone", "America/New_York" in r.body or "UTC" in r.body
        )

        # ==============================================================
        # 7. Export
        # ==============================================================
        print("\n--- Export ---")
        r = acme.get("/tickets/export/?format=csv")
        check("CSV export", r, 200)
        check_true("CSV has header", "ticket_number" in r.body)
        check_true("CSV has data", "ACME-" in r.body)

        r = acme.get("/tickets/export/?format=json")
        check("JSON export", r, 200)
        check_true("JSON has tickets", "tickets" in r.body)

        # ==============================================================
        # 8. Admin panel
        # ==============================================================
        print("\n--- Admin Panel ---")
        r = acme.get("/admin/")
        check_true("admin responds", r.status in (200, 302))

        # ==============================================================
        # 9. Customer flow — register + login + portal
        # ==============================================================
        print("\n--- Customer Flow ---")
        cust = TenantSession(base, acme_id)
        cust.ensure_csrf("/auth/customer/login")

        r = cust.get("/auth/customer/register")
        check("register page", r, 200)

        r = cust.get("/auth/customer/login")
        check("customer login page", r, 200)

        # Login as seeded customer
        r = cust.form_post(
            "/auth/customer/login",
            {
                "email": "cust1@example.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("customer login", r, 302)

        r = cust.get("/portal/")
        check("portal dashboard", r, 200)

        r = cust.get("/portal/tickets/")
        check("portal ticket list", r, 200)

        r = cust.get("/portal/tickets/new")
        check("portal new ticket form", r, 200)

        # Find a portal ticket
        r = cust.get("/portal/tickets/")
        portal_tid = find_ticket_id(r.body.replace("/portal/tickets/", "/tickets/"))
        if not portal_tid:
            # Try different pattern
            match = re.search(r"/portal/tickets/(\d+)", r.body)
            portal_tid = match.group(1) if match else None

        if portal_tid:
            r = cust.get(f"/portal/tickets/{portal_tid}")
            check("portal ticket detail", r, 200)

            # Internal notes should NOT be visible
            check_true(
                "internal notes hidden from portal", "Internal agent note" not in r.body
            )

            # Customer adds comment
            r = cust.form_post(
                f"/portal/tickets/{portal_tid}/comment",
                {
                    "body": "Customer reply from E2E test",
                },
            )
            check("portal add comment", r, 302)

            # CSAT rating
            r = cust.form_post(
                f"/portal/tickets/{portal_tid}/rate",
                {
                    "score": "5",
                    "comment": "Great support!",
                },
            )
            check("CSAT rating", r, 302)

        # ==============================================================
        # 10. Customer isolation — can't access agent routes
        # ==============================================================
        print("\n--- Customer Isolation ---")
        r = cust.get("/tickets/")
        check_true("customer blocked from agent tickets", r.status in (302, 403, 404))

        r = cust.get("/dashboard/")
        check_true("customer blocked from dashboard", r.status in (302, 403, 404))

        r = cust.get("/agents/")
        check_true("customer blocked from agents", r.status in (302, 403, 404))

        # ==============================================================
        # 11. Multi-tenant isolation
        # ==============================================================
        print("\n--- Tenant Isolation ---")
        globex = TenantSession(base, globex_id)
        globex.ensure_csrf()
        r = globex.form_post(
            "/auth/agent/login",
            {
                "email": "admin@globex.com",
                "password": SEED_PASSWORD,
                "org_slug": "globex",
            },
        )
        check("globex agent login", r, 302)

        r = globex.get("/tickets/")
        check("globex ticket list", r, 200)
        check_true("globex sees GLOBEX tickets", "GLOBEX-" in r.body)
        check_true("globex does NOT see ACME tickets", "ACME-" not in r.body)

        r = globex.get("/dashboard/")
        check("globex dashboard", r, 200)

        # Acme agent can't see globex data
        r = acme.get("/tickets/")
        check_true("acme does NOT see GLOBEX tickets", "GLOBEX-" not in r.body)

        # ==============================================================
        # 12. Role enforcement
        # ==============================================================
        print("\n--- Role Enforcement ---")
        regular = TenantSession(base, acme_id)
        regular.ensure_csrf()
        r = regular.form_post(
            "/auth/agent/login",
            {
                "email": "bob@acme.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("regular agent login", r, 302)

        r = regular.get("/tickets/")
        check("regular agent sees tickets", r, 200)

        r = regular.get("/agents/")
        check("regular agent blocked from agents list", r, 403)

        # ==============================================================
        # 13. Auth enforcement — bad credentials
        # ==============================================================
        print("\n--- Auth Enforcement ---")
        bad = TenantSession(base, acme_id)
        bad.ensure_csrf()
        r = bad.form_post(
            "/auth/agent/login",
            {
                "email": "admin@acme.com",
                "password": "wrong",
                "org_slug": "acme",
            },
        )
        check("bad password rejected", r, 400)
        check_true("error in response", "Invalid" in r.body)

        r = bad.form_post(
            "/auth/agent/login",
            {
                "email": "",
                "password": "test",
                "org_slug": "acme",
            },
        )
        check("empty email rejected", r, 400)

        # ==============================================================
        # 14. Unauthenticated access
        # ==============================================================
        print("\n--- Unauthenticated ---")
        unauth = TenantSession(base, acme_id)
        r = unauth.get("/tickets/")
        check("unauthed tickets → redirect", r, 302)
        r = unauth.get("/portal/")
        check("unauthed portal → redirect", r, 302)
        r = unauth.get("/dashboard/")
        check("unauthed dashboard → redirect", r, 302)

    # Summary
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"HyperTicket E2E: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
