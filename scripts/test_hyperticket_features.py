"""
HyperTicket — New Feature Routes E2E Tests.

Tests attachments, templates, canned responses, saved views,
board, bulk ops, approvals, API keys, custom fields, tag management.

Usage:
    uv run hyper-test hyperticket_features
"""

# hyper-test: e2e

import re
import subprocess
import sys
import urllib.parse

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    Session,
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
    match = re.search(r"/tickets/(\d+)", html)
    return match.group(1) if match else None


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperTicket — Feature Routes E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["hyperticket"]

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

        acme_id = 1

        # Login as admin
        admin = TenantSession(base, acme_id)
        admin.ensure_csrf()
        r = admin.form_post(
            "/auth/agent/login",
            {
                "email": "admin@acme.com",
                "password": SEED_PASSWORD,
                "org_slug": "acme",
            },
        )
        check("admin login", r, 302)

        # Find a ticket ID
        r = admin.get("/tickets/")
        tid = find_ticket_id(r.body)
        check_true("found ticket", tid is not None)

        # ── Templates ────────────────────────────────────────
        print("\n--- Templates ---")
        r = admin.get("/templates/")
        check("templates page", r, 200)

        # ── Canned Responses ─────────────────────────────────
        print("\n--- Canned Responses ---")
        r = admin.get("/canned-responses/")
        check("canned responses page", r, 200)

        # ── Saved Views ──────────────────────────────────────
        print("\n--- Saved Views ---")
        r = admin.get("/saved-views/")
        check("saved views page", r, 200)

        # Create a saved view
        r = admin.form_post(
            "/saved-views/new",
            {
                "name": "My Open Tickets",
                "filter_criteria": '{"status": "open"}',
                "sort_order": "-created_at",
            },
        )
        check("create saved view", r, 302)

        r = admin.get("/saved-views/")
        check_true("saved view appears", "My Open Tickets" in r.body)

        # ── Kanban Board ─────────────────────────────────────
        print("\n--- Board ---")
        r = admin.get("/board/")
        check("board page", r, 200)
        check_true("board has status columns", "Open" in r.body or "ACME-" in r.body)

        # ── Approvals ────────────────────────────────────────
        print("\n--- Approvals ---")
        r = admin.get("/approvals/")
        check("approvals page", r, 200)

        # Request approval on a ticket
        if tid:
            r = admin.form_post(
                f"/tickets/{tid}/request-approval",
                {
                    "comment": "Please review before closing",
                },
            )
            check("request approval", r, 302)

            # Check approval appears
            r = admin.get("/approvals/")
            check_true(
                "approval in list", "review" in r.body.lower() or "Approve" in r.body
            )

        # ── API Keys ─────────────────────────────────────────
        print("\n--- API Keys ---")
        r = admin.get("/admin/api-keys/")
        check("api keys page", r, 200)

        r = admin.form_post(
            "/admin/api-keys/new",
            {
                "name": "E2E Test Key",
            },
        )
        check("create api key", r, 200)
        check_true("key shown once", "sk_ht_" in r.body)

        r = admin.get("/admin/api-keys/")
        check_true("key in list", "E2E Test Key" in r.body)

        # ── Custom Fields ────────────────────────────────────
        print("\n--- Custom Fields ---")
        r = admin.get("/admin/custom-fields/")
        check("custom fields page", r, 200)

        r = admin.form_post(
            "/admin/custom-fields/",
            {
                "schema_json": '[{"name": "department", "type": "text", "required": false}]',
            },
        )
        check("update custom fields schema", r, 302)

        r = admin.get("/admin/custom-fields/")
        check_true("schema persisted", "department" in r.body)

        # ── Tags on Tickets ──────────────────────────────────
        print("\n--- Tag Management ---")
        r = admin.get("/tags/")
        check("tags page", r, 200)
        check_true("tags rendered", "billing" in r.body or "login" in r.body)

        # ── Search ───────────────────────────────────────────
        print("\n--- Search ---")
        r = admin.get("/search/?q=login")
        check("search with results", r, 200)
        check_true("search results page", "Search" in r.body)

        # ── Export ───────────────────────────────────────────
        print("\n--- Export ---")
        r = admin.get("/tickets/export/?format=csv")
        check("CSV export", r, 200)
        check_true("CSV has data", "ACME-" in r.body)

        r = admin.get("/tickets/export/?format=json")
        check("JSON export", r, 200)
        check_true("JSON has tickets", "tickets" in r.body)

        # ── Org Settings ─────────────────────────────────────
        print("\n--- Org Settings ---")
        r = admin.get("/admin/settings/")
        check("settings page", r, 200)
        check_true("settings has timezone", "America/New_York" in r.body)

        # ── Analytics ────────────────────────────────────────
        print("\n--- Analytics ---")
        r = admin.get("/dashboard/")
        check("dashboard page", r, 200)
        check_true("dashboard has stats", "Total Tickets" in r.body)

        r = admin.get("/analytics/agents/")
        check("agent stats partial", r, 200)

        r = admin.get("/analytics/teams/")
        check("team stats partial", r, 200)

        r = admin.get("/analytics/volume/")
        check("volume trends partial", r, 200)

        # ── Attachment routes exist ──────────────────────────
        print("\n--- Attachments ---")
        if tid:
            r = admin.get(f"/tickets/{tid}/attachments/")
            check("attachment list", r, 200)

        # ── Bulk ops (team_lead required, admin has it) ──────
        print("\n--- Bulk Operations ---")
        if tid:
            r = admin.form_post(
                "/tickets/bulk-update",
                {
                    "ticket_ids": tid,
                    "action": "change_priority",
                    "value": "1",
                },
            )
            check("bulk update", r, 302)

    # Summary
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"HyperTicket Features: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
