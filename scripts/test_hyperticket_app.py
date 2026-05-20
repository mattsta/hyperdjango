"""
HyperTicket Phase 2 — Application E2E Tests.

Tests auth flows, guard enforcement, ticket CRUD, portal access,
admin panel, input validation, and tenant isolation against a live server.

Usage:
    uv run hyper-test hyperticket_app
"""

# hyper-test: e2e

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
        print(f"        body: {response.body[:300]}")
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


def check_val(name, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected!r}, got {actual!r}"
    print(msg)
    ERRORS.append(msg)
    return False


def form_encode(data: dict[str, str]) -> str:
    """URL-encode form data."""
    return urllib.parse.urlencode(data)


def tenant_session(base_url: str, tenant_id: int) -> Session:
    """Create a Session with X-Tenant-ID header baked in."""
    s = Session(base_url)
    s.cookie_jar["_tenant"] = str(tenant_id)  # not real — we use header
    s._default_tenant_id = tenant_id
    return s


class TenantSession(Session):
    """Session that sends X-Tenant-ID header on every request."""

    def __init__(self, base_url: str, tenant_id: int):
        super().__init__(base_url)
        self.tenant_id = tenant_id

    def _headers(self, extra=None, include_csrf=False):
        h = super()._headers(extra, include_csrf)
        h["X-Tenant-ID"] = str(self.tenant_id)
        return h

    def form_post(self, path: str, data: dict[str, str]) -> E2EResponse:
        """POST form-encoded data (for auth forms).

        Includes CSRF token from cookie jar if available.
        """
        # Include CSRF token in form body if we have it
        csrf = self.cookie_jar.get("csrftoken", "")
        if csrf:
            data["_csrf_token"] = csrf
        return self.post(
            path,
            body=form_encode(data),
            content_type="application/x-www-form-urlencoded",
        )

    def ensure_csrf(self, path: str = "/auth/agent/login") -> None:
        """GET a page to receive the CSRF cookie."""
        if "csrftoken" not in self.cookie_jar:
            self.get(path)


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Phase 2: Application E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["hyperticket_app"]

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

        # We need the tenant IDs. Acme is first org created, so ID=1 or we query.
        # Use direct DB query via the model test approach. For now assume seed order: acme=1, globex=2.
        # Actually, better: check the admin panel or health endpoint.
        acme_id = 1
        globex_id = 2

        # ── Health ──────────────────────────────────────────────
        print("\n--- Health ---")
        r = http_get(f"{base}/health")
        check("health", r, 200)

        # ── Unauthenticated Access → Redirect ──────────────────
        print("\n--- Unauthenticated Access ---")
        r = http_get(f"{base}/tickets/", headers={"X-Tenant-ID": str(acme_id)})
        # Guard should redirect to /auth/agent/login
        check("tickets list unauthed → redirect", r, 302)

        r = http_get(f"{base}/portal/", headers={"X-Tenant-ID": str(acme_id)})
        check("portal unauthed → redirect", r, 302)

        r = http_get(f"{base}/agents/", headers={"X-Tenant-ID": str(acme_id)})
        check("agents list unauthed → redirect", r, 302)

        # ── Agent Login ─────────────────────────────────────────
        print("\n--- Agent Auth ---")
        acme_agent = TenantSession(base, acme_id)

        # GET login page (also gets CSRF cookie)
        r = acme_agent.get("/auth/agent/login")
        check("agent login page", r, 200)
        check_true("login page has form", "email" in r.body)

        # Bad credentials
        r = acme_agent.form_post(
            "/auth/agent/login",
            {
                "email": "admin@acme.com",
                "password": "wrongpassword",
                "org_slug": "acme",
            },
        )
        check("bad password → 400", r, 400)
        check_true("error message in body", "Invalid" in r.body)

        # Good credentials
        r = acme_agent.form_post(
            "/auth/agent/login",
            {
                "email": "admin@acme.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("agent login → redirect", r, 302)
        check_true("session cookie set", len(acme_agent.cookie_jar) > 0)

        # ── Authenticated Agent Access ──────────────────────────
        print("\n--- Agent Ticket List ---")
        r = acme_agent.get("/tickets/")
        check("tickets list authed", r, 200)
        check_true("ticket list has table", "ACME-" in r.body)

        # ── Ticket Detail ───────────────────────────────────────
        print("\n--- Ticket Detail ---")
        # Ticket ID 1 should exist (first ticket in seed)
        r = acme_agent.get("/tickets/1")
        # May be 200 if ticket exists, or we need to find the right ID
        if r.status == 200:
            check("ticket detail", r, 200)
            check_true("detail has ticket number", "ACME-" in r.body)
            check_true("detail has comments section", "Comments" in r.body)
        else:
            # Try higher IDs (auto-increment after platform tables)
            for tid in range(2, 20):
                r = acme_agent.get(f"/tickets/{tid}")
                if r.status == 200 and "ACME-" in r.body:
                    check("ticket detail (found)", r, 200)
                    break

        # ── Ticket Create Form ──────────────────────────────────
        print("\n--- Ticket Create ---")
        r = acme_agent.get("/tickets/new")
        check("create form", r, 200)
        check_true(
            "form has fields", "title" in r.body and "priority" in r.body.lower()
        )

        # ── Ticket Actions ──────────────────────────────────────
        print("\n--- Ticket Actions ---")
        # Find a ticket to test actions on
        # Use ticket from seed — find via list
        r = acme_agent.get("/tickets/")
        if r.status == 200 and "ACME-0001" in r.body:
            # Extract ticket ID from href — /tickets/N
            import re

            match = re.search(r"/tickets/(\d+)", r.body)
            if match:
                test_ticket_id = match.group(1)

                # Add comment FIRST (before locking)
                r = acme_agent.form_post(
                    f"/tickets/{test_ticket_id}/comments",
                    {
                        "body": "E2E test comment from agent",
                        "is_internal": "",
                    },
                )
                check("add comment → redirect", r, 302)

                # Add internal comment
                r = acme_agent.form_post(
                    f"/tickets/{test_ticket_id}/comments",
                    {
                        "body": "Internal note from agent",
                        "is_internal": "on",
                    },
                )
                check("add internal comment → redirect", r, 302)

                # Mute (before lock, since lock blocks comments)
                r = acme_agent.form_post(f"/tickets/{test_ticket_id}/mute", {})
                check("mute ticket → redirect", r, 302)

                # Lock (after comments)
                r = acme_agent.form_post(f"/tickets/{test_ticket_id}/lock", {})
                check("lock ticket → redirect", r, 302)

                # Timeline
                r = acme_agent.get(f"/tickets/{test_ticket_id}/timeline")
                check("timeline → JSON", r, 200)
                check_true("timeline has entries", "timeline" in r.body)

        # ── Tenant Isolation ────────────────────────────────────
        print("\n--- Tenant Isolation ---")
        # Acme agent should NOT see Globex tickets
        globex_agent = TenantSession(base, globex_id)
        globex_agent.ensure_csrf()
        r = globex_agent.form_post(
            "/auth/agent/login",
            {
                "email": "admin@globex.com",
                "password": SEED_PASSWORD,
                "org_slug": "globex",
            },
        )
        check("globex login", r, 302)

        r = globex_agent.get("/tickets/")
        check("globex ticket list", r, 200)
        if r.status == 200:
            check_true("globex sees GLOBEX tickets", "GLOBEX-" in r.body)
            check_true("globex does NOT see ACME tickets", "ACME-" not in r.body)

        # ── Customer Auth ───────────────────────────────────────
        print("\n--- Customer Auth ---")
        acme_customer = TenantSession(base, acme_id)

        # Register page
        r = acme_customer.get("/auth/customer/register")
        check("customer register page", r, 200)

        # Login page
        r = acme_customer.get("/auth/customer/login")
        check("customer login page", r, 200)

        # Login with seeded customer (ensure CSRF first)
        acme_customer.ensure_csrf("/auth/customer/login")
        r = acme_customer.form_post(
            "/auth/customer/login",
            {
                "email": "cust1@example.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("customer login → redirect", r, 302)

        # ── Customer Portal ─────────────────────────────────────
        print("\n--- Customer Portal ---")
        r = acme_customer.get("/portal/")
        check("portal dashboard", r, 200)

        r = acme_customer.get("/portal/tickets/")
        check("portal ticket list", r, 200)

        # Customer new ticket form
        r = acme_customer.get("/portal/tickets/new")
        check("portal new ticket form", r, 200)

        # ── Portal Isolation ────────────────────────────────────
        print("\n--- Portal Isolation ---")
        # Customer should NOT see agent-only routes
        # Guard will reject because user_type is "customer", not "agent"
        # This results in redirect (302) or forbidden (403) depending on guard behavior
        r = acme_customer.get("/tickets/")
        check_true(
            "customer blocked from agent tickets",
            r.status in (302, 403, 404),
            f"got {r.status}",
        )

        r = acme_customer.get("/agents/")
        check_true(
            "customer blocked from agents",
            r.status in (302, 403, 404),
            f"got {r.status}",
        )

        # ── Admin Panel ─────────────────────────────────────────
        print("\n--- Admin Panel ---")
        r = http_get(f"{base}/admin/", headers={"X-Tenant-ID": str(acme_id)})
        # Admin should be accessible (may require its own auth or show login)
        check_true("admin endpoint responds", r.status in (200, 302, 401))

        # ── Input Validation ────────────────────────────────────
        print("\n--- Input Validation ---")
        # Agent login with empty email
        bad_session = TenantSession(base, acme_id)
        bad_session.ensure_csrf()
        r = bad_session.form_post(
            "/auth/agent/login",
            {
                "email": "",
                "password": "test",
                "org_slug": "acme",
            },
        )
        check("empty email → 400", r, 400)

        # Agent login with empty password
        r = bad_session.form_post(
            "/auth/agent/login",
            {
                "email": "test@test.com",
                "password": "",
                "org_slug": "acme",
            },
        )
        check("empty password → 400", r, 400)

        # ── Role Enforcement ────────────────────────────────────
        print("\n--- Role Enforcement ---")
        # Login as a regular agent (not admin)
        regular_agent = TenantSession(base, acme_id)
        regular_agent.ensure_csrf()
        r = regular_agent.form_post(
            "/auth/agent/login",
            {
                "email": "bob@acme.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("regular agent login", r, 302)

        # Regular agent should NOT be able to create agents (admin only)
        r = regular_agent.get("/agents/")
        check("regular agent → agents list blocked", r, 403)

        # Regular agent should be able to list tickets
        r = regular_agent.get("/tickets/")
        check("regular agent → tickets accessible", r, 200)

    # ── Summary ─────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"HyperTicket App: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
