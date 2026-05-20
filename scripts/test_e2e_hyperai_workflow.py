"""
Deep workflow tests for HyperAI: full user journey.

Register → Login → Dashboard → New conversation → Send message → View chat →
API completions (Bearer) → Account → API key management → Rate limit check.
"""

# hyper-test: e2e

import json
import re
import subprocess
import time

from e2e_helper import SEED_PASSWORD, TEST_PORTS, AppRunner, Session, http_post

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperAI Deep Workflow Tests")
    print("=" * 60)

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hyperai.app:app",
            "--drop",
            "--seed",
            "services.hyperai.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.hyperai.app:app",
        host="127.0.0.1",
        port=TEST_PORTS["hyperai_workflow"],
    ) as runner:
        base = runner.url()
        ts = str(int(time.time()))

        # ── 1. Register ──────────────────────────────────────────────
        print("\n--- 1. Register ---")
        s = Session(base)
        s.get("/register")  # CSRF
        r = s.post(
            "/register",
            body=f"username=aitest{ts}&email=ai{ts}@test.com&password=testpass123",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Registration succeeds", r.status in (200, 302, 303))
        ok("Session cookie set", "sessionid" in s.cookie_jar)

        # ── 2. Login as demo user (has seed data) ────────────────────
        print("\n--- 2. Login as demo ---")
        s2 = Session(base)
        s2.get("/login")  # CSRF
        r = s2.post(
            "/login",
            body=f"username=demo&password={SEED_PASSWORD}",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Demo login succeeds", r.status in (200, 302, 303))
        ok("Demo session set", "sessionid" in s2.cookie_jar)

        # ── 3. Dashboard ─────────────────────────────────────────────
        print("\n--- 3. Dashboard ---")
        r = s2.get("/")
        ok("Dashboard loads", r.status == 200)
        ok(
            "Dashboard has conversations or welcome",
            "conversation" in r.body.lower()
            or "chat" in r.body.lower()
            or "dashboard" in r.body.lower(),
        )

        # ── 4. New conversation ──────────────────────────────────────
        print("\n--- 4. New conversation ---")
        r = s2.get("/chat/new")
        ok("New chat response", r.status in (200, 302, 303))

        # Extract conversation opaque ID from redirect Location header or page content
        chat_id = ""
        location = r.headers.get("location", "")
        if "/chat/" in location:
            chat_id = location.split("/chat/")[1].split("/")[0].split("?")[0]
        elif r.status == 200 and "/chat/" in r.body:
            m = re.search(r"/chat/([A-Za-z0-9_.]+)", r.body)
            if m:
                chat_id = m.group(1)
        ok("Got conversation ID", bool(chat_id), f"location={location}")
        print(f"  INFO  Using conversation ID: {chat_id}")

        # ── 5. View conversation ─────────────────────────────────────
        print("\n--- 5. View conversation ---")
        r = s2.get(f"/chat/{chat_id}")
        ok("Chat view loads", r.status == 200)
        ok(
            "Chat has message area",
            "message" in r.body.lower() or "chat" in r.body.lower(),
        )

        # ── 6. Send a message ────────────────────────────────────────
        print("\n--- 6. Send message ---")
        r = s2.post(
            f"/chat/{chat_id}/send",
            body=f"content=Hello+from+workflow+test+{ts}",
            content_type="application/x-www-form-urlencoded",
        )
        # SSE response or redirect
        ok(
            "Send message response",
            r.status in (200, 302, 303),
            f"got {r.status}: {r.body[:100]}",
        )

        # ── 7. Account page ──────────────────────────────────────────
        print("\n--- 7. Account ---")
        r = s2.get("/account")
        ok("Account page loads", r.status == 200)
        ok("Account shows username", "demo" in r.body.lower())
        ok(
            "Account shows tier info",
            "tier" in r.body.lower()
            or "pro" in r.body.lower()
            or "free" in r.body.lower(),
        )
        ok(
            "Account shows API keys section",
            "api" in r.body.lower() or "key" in r.body.lower(),
        )

        # ── 8. API completions (Bearer token) ────────────────────────
        print("\n--- 8. API completions ---")
        # Create an API key for this test (seed keys are now randomly generated)
        r = s2.post(
            "/api-keys/create",
            body=f"name=Workflow+Test+Key+{ts}",
            content_type="application/x-www-form-urlencoded",
        )
        workflow_api_key = ""
        if r.status == 200 and "sk_hyper_" in r.body:
            match = re.search(r"(sk_hyper_[A-Za-z0-9._]+)", r.body)
            if match:
                workflow_api_key = match.group(1)
        ok("Workflow API key created", bool(workflow_api_key), "key not found")
        api_h = {
            "Authorization": f"Bearer {workflow_api_key}",
            "Content-Type": "application/json",
        }
        r = http_post(
            f"{base}/api/v1/chat/completions",
            body=json.dumps(
                {
                    "messages": [{"role": "user", "content": f"Test message {ts}"}],
                }
            ),
            headers=api_h,
        )
        ok("API completions succeeds", r.status == 200)
        ct = r.headers.get("content-type", "")
        ok("API returns SSE stream", "text/event-stream" in ct, f"content-type: {ct}")

        # ── 9. API auth failures ─────────────────────────────────────
        print("\n--- 9. API auth enforcement ---")
        r = http_post(
            f"{base}/api/v1/chat/completions",
            body=json.dumps({"messages": [{"role": "user", "content": "test"}]}),
            headers={"Content-Type": "application/json"},
        )
        ok("No bearer → 401", r.status == 401)

        r = http_post(
            f"{base}/api/v1/chat/completions",
            body=json.dumps({"messages": [{"role": "user", "content": "test"}]}),
            headers={
                "Authorization": "Bearer invalid",
                "Content-Type": "application/json",
            },
        )
        ok("Invalid bearer → 401", r.status == 401)

        # ── 10. Delete conversation ──────────────────────────────────
        print("\n--- 10. Delete conversation ---")
        r = s2.post(f"/chat/{chat_id}/delete")
        ok(
            "Delete conversation response",
            r.status in (200, 302, 303),
            f"got {r.status}: {r.body[:100]}",
        )

        # ── 11. Health check ─────────────────────────────────────────
        print("\n--- 11. Health ---")
        r = s2.get("/health")
        ok("Health check", r.status == 200)
        data = r.json
        ok("Health has status field", data.get("status") == "ok")

        # ── 12. Logout ───────────────────────────────────────────────
        print("\n--- 12. Logout ---")
        r = s2.post("/logout")
        ok("Logout succeeds", r.status in (200, 302, 303))

        # Verify logged out
        r = s2.get("/")
        ok("Post-logout redirects to login", r.status in (302, 303))

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
