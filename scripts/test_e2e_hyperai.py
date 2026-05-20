"""
End-to-end tests for HyperAI — multi-conversation AI chat service.

# hyper-test: e2e

Tests SSE streaming, API key management, OpenAI-compatible REST endpoint,
conversation CRUD, auth flow, and guard enforcement.
"""

import re
import subprocess
import time

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    Session,
    http_get,
    http_post,
)

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


if __name__ == "__main__":
    port = TEST_PORTS["hyperai"]
    ts = str(int(time.time()))

    # Run setup (creates tables from models + seeds demo data)
    print("Running setup...")
    setup_result = subprocess.run(
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
    if setup_result.returncode != 0:
        print(f"Setup failed:\n{setup_result.stderr}")
        raise SystemExit(1)

    print("Starting server...")
    with AppRunner(
        "services.hyperai.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()

        # ── Health ──────────────────────────────────────────────
        print("\n--- Health ---")
        r = http_get(f"{base}/health")
        ok("Health 200", r.status == 200)
        ok("Health status ok", r.json.get("status") == "ok")
        # mount_health() returns {"status": "ok"} for liveness

        # ── Static endpoints ────────────────────────────────────
        print("\n--- Static endpoints ---")
        r = http_get(f"{base}/robots.txt")
        ok("robots.txt 200", r.status == 200)
        ok("robots.txt disallows /api/", "Disallow: /api/" in r.body)

        r = http_get(f"{base}/.well-known/security.txt")
        ok("security.txt 200", r.status == 200)
        ok("security.txt has contact", "Contact:" in r.body)

        # ── Auth enforcement (unauthenticated) ──────────────────
        print("\n--- Auth enforcement ---")
        r = http_get(f"{base}/")
        ok(
            "Dashboard requires auth",
            r.status in (302, 303) or "/login" in r.body,
            f"got {r.status}",
        )
        r = http_get(f"{base}/chat/new")
        ok(
            "New chat requires auth",
            r.status in (302, 303) or "/login" in r.body,
            f"got {r.status}",
        )
        r = http_get(f"{base}/account")
        ok(
            "Account requires auth",
            r.status in (302, 303) or "/login" in r.body,
            f"got {r.status}",
        )

        # ── Auth: Register ──────────────────────────────────────
        print("\n--- Auth: Register ---")
        s = Session(base)
        r = s.get("/register")
        ok("Register page 200", r.status == 200)
        ok("Register has form fields", "username" in r.body and "password" in r.body)

        r = s.post(
            "/register",
            body=f"username=tester_{ts}&email=tester_{ts}@test.com&password=test12345678",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Register succeeds", r.status in (200, 302), f"got {r.status}")

        r = s.get("/")
        ok("After register: dashboard loads", r.status == 200)

        # ── Register validation ─────────────────────────────────
        print("\n--- Register validation ---")
        s_val = Session(base)
        s_val.get("/register")
        r = s_val.post(
            "/register",
            body="username=&email=bad&password=short",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Register blank username rejected",
            r.status == 200
            and ("required" in r.body.lower() or "error" in r.body.lower()),
        )

        s_val2 = Session(base)
        s_val2.get("/register")
        r = s_val2.post(
            "/register",
            body="username=bad!user&email=x@x.com&password=longpassword123",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Register bad chars rejected",
            r.status == 200
            and ("error" in r.body.lower() or "letters" in r.body.lower()),
        )

        # ── Auth: Logout + Login ────────────────────────────────
        print("\n--- Auth: Logout + Login ---")
        r = s.post("/logout", body="", content_type="application/x-www-form-urlencoded")
        ok("Logout redirects", r.status in (200, 302), f"got {r.status}")

        # Fresh session for login
        s = Session(base)
        r = s.get("/login")
        ok("Login page 200", r.status == 200)

        # Bad credentials
        r = s.post(
            "/login",
            body="username=demo&password=wrongpassword",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Bad login shows error", "Invalid" in r.body or "invalid" in r.body)

        # Good credentials (demo user from seed)
        r = s.post(
            "/login",
            body=f"username=demo&password={SEED_PASSWORD}",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Login succeeds", r.status in (200, 302), f"got {r.status}")

        r = s.get("/")
        ok("Logged in: dashboard accessible", r.status == 200)

        # ── Dashboard ───────────────────────────────────────────
        print("\n--- Dashboard ---")
        ok(
            "Dashboard has conversations",
            "conversation" in r.body.lower() or "chat" in r.body.lower(),
        )

        # ── Conversation: Create ────────────────────────────────
        print("\n--- Conversation CRUD ---")
        r = s.get("/chat/new")
        ok("New chat creates conversation", r.status in (200, 302), f"got {r.status}")

        chat_cid = ""
        if r.status in (302, 303):
            loc = r.headers.get("location", "")
            if "/chat/" in loc:
                chat_cid = loc.split("/chat/")[-1]
                r = s.get(f"/chat/{chat_cid}")

        ok("Chat view loads", r.status == 200)
        ok(
            "Chat view has input area",
            "content" in r.body.lower()
            or "message" in r.body.lower()
            or "send" in r.body.lower(),
        )

        # ── Chat: Send message via SSE endpoint ─────────────────
        print("\n--- Chat send (SSE) ---")
        if chat_cid:
            r = s.post(
                f"/chat/{chat_cid}/send",
                body="content=Hello+from+E2E",
                content_type="application/x-www-form-urlencoded",
            )
            ok("Send message returns response", r.status == 200, f"got {r.status}")
            # SSE or redirect — just verify the endpoint works
            ok("Send message not 500", r.status != 500)
        else:
            ok("Send message returns response", False, "no cid")
            ok("Send message not 500", False, "skipped")

        # ── Conversation: Invalid ID ────────────────────────────
        r = s.get("/chat/ZZZINVALID999")
        ok("Invalid chat ID → 404", r.status == 404, f"got {r.status}")

        # ── Conversation: Delete ────────────────────────────────
        print("\n--- Conversation delete ---")
        if chat_cid:
            r = s.post(
                f"/chat/{chat_cid}/delete",
                body="",
                content_type="application/x-www-form-urlencoded",
            )
            ok("Delete redirects", r.status in (200, 302), f"got {r.status}")

            r = s.get(f"/chat/{chat_cid}")
            ok("Deleted conversation → 404", r.status == 404, f"got {r.status}")
        else:
            ok("Delete redirects", False, "no cid captured")
            ok("Deleted conversation → 404", False, "skipped")

        # ── Account page ────────────────────────────────────────
        print("\n--- Account ---")
        r = s.get("/account")
        ok("Account page loads", r.status == 200)
        ok(
            "Account has tier info",
            "tier" in r.body.lower()
            or "pro" in r.body.lower()
            or "free" in r.body.lower(),
        )
        ok(
            "Account has API keys section",
            "api" in r.body.lower() or "key" in r.body.lower(),
        )

        # ── API key: Create ─────────────────────────────────────
        print("\n--- API key management ---")
        r = s.post(
            "/api-keys/create",
            body=f"name=Test+Key+{ts}",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Create API key succeeds",
            r.status == 200 or r.status == 302,
            f"got {r.status}",
        )
        new_api_key = ""
        if r.status == 200 and "sk_hyper_" in r.body:
            # Signed keys are base62 + separator, variable length
            match = re.search(r"(sk_hyper_[A-Za-z0-9._]+)", r.body)
            if match:
                new_api_key = match.group(1)
        ok("New key shown in response", bool(new_api_key), "key not found")

        # ── API key: Create without name → 400 ──────────────────
        r = s.post(
            "/api-keys/create",
            body="name=",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Create key without name → 400", r.status == 400, f"got {r.status}")

        # ── API key: Revoke ─────────────────────────────────────
        print("\n--- API key revoke ---")
        if new_api_key:
            # Verify the new key works
            r = http_post(
                f"{base}/api/v1/chat/completions",
                body={
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": False,
                },
                headers={"Authorization": f"Bearer {new_api_key}"},
            )
            ok("New key works for API", r.status == 200, f"got {r.status}")

            # Find revoke link on account page
            r = s.get("/account")
            revoke_match = re.search(r"/api-keys/([A-Za-z0-9._]+)/revoke", r.body)
            if revoke_match:
                kid = revoke_match.group(1)
                r = s.post(
                    f"/api-keys/{kid}/revoke",
                    body="",
                    content_type="application/x-www-form-urlencoded",
                )
                ok("Revoke redirects", r.status in (200, 302), f"got {r.status}")

                r = http_post(
                    f"{base}/api/v1/chat/completions",
                    body={
                        "messages": [{"role": "user", "content": "test"}],
                        "stream": False,
                    },
                    headers={"Authorization": f"Bearer {new_api_key}"},
                )
                ok("Revoked key rejected", r.status == 401, f"got {r.status}")
            else:
                ok("Revoke redirects", False, "no revoke link found")
                ok("Revoked key rejected", False, "skipped")
        else:
            ok("New key works for API", False, "no key created")
            ok("Revoke redirects", False, "skipped")
            ok("Revoked key rejected", False, "skipped")

        # ── Create API key for OpenAI API tests ─────────────────
        print("\n--- OpenAI-compatible API (non-streaming) ---")
        r = s.post(
            "/api-keys/create",
            body=f"name=API+Test+Key+{ts}",
            content_type="application/x-www-form-urlencoded",
        )
        demo_key = ""
        if r.status == 200 and "sk_hyper_" in r.body:
            match = re.search(r"(sk_hyper_[A-Za-z0-9._]+)", r.body)
            if match:
                demo_key = match.group(1)
        ok("API test key created", bool(demo_key), "key not found in response")

        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={
                "model": "hyper-4",
                "messages": [{"role": "user", "content": "What is HyperDjango?"}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {demo_key}"},
        )
        ok("Non-streaming 200", r.status == 200, f"got {r.status}")
        if r.status == 200:
            data = r.json
            ok("Response has id", data.get("id", "").startswith("chatcmpl-"))
            ok("Response has choices", len(data.get("choices", [])) > 0)
            ok("Choice has message", "message" in data.get("choices", [{}])[0])
            ok(
                "Message has content",
                bool(data["choices"][0]["message"].get("content")),
            )
            ok("Response has usage", "usage" in data)
            ok(
                "Usage has total_tokens",
                data.get("usage", {}).get("total_tokens", 0) > 0,
            )
            ok("Model matches", data.get("model") == "hyper-4")
            ok("Object type correct", data.get("object") == "chat.completion")
            ok(
                "Finish reason is stop",
                data["choices"][0].get("finish_reason") == "stop",
            )

        # ── OpenAI API: Streaming ───────────────────────────────
        # ── OpenAI API: Streaming ───────────────────────────────
        # The Zig native HTTP server sends SSE with Content-Length: 0 + keep-alive,
        # streaming chunks after the initial response. Standard http.client and raw
        # sockets can't easily read this without a proper SSE client library.
        # We verify content-type and test the non-streaming path for body validation.
        print("\n--- OpenAI-compatible API (streaming) ---")
        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={
                "model": "hyper-4",
                "messages": [{"role": "user", "content": "Explain SSE streaming"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {demo_key}"},
        )
        ok("Streaming 200", r.status == 200, f"got {r.status}")
        ok("SSE content type", "text/event-stream" in r.headers.get("content-type", ""))

        # ── OpenAI API: Auth enforcement ────────────────────────
        print("\n--- API auth enforcement ---")
        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={"messages": [{"role": "user", "content": "test"}]},
            headers={"Authorization": "Bearer sk-invalid-key-999"},
        )
        ok("Invalid key → 401", r.status == 401, f"got {r.status}")

        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={"messages": [{"role": "user", "content": "test"}]},
        )
        ok("No key → 401", r.status == 401, f"got {r.status}")

        # ── OpenAI API: Error handling ──────────────────────────
        print("\n--- API error handling ---")
        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={"messages": [], "stream": False},
            headers={"Authorization": f"Bearer {demo_key}"},
        )
        ok("Empty messages → 400", r.status == 400, f"got {r.status}")

        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={"stream": False},
            headers={"Authorization": f"Bearer {demo_key}"},
        )
        ok("Missing messages → 400", r.status == 400, f"got {r.status}")

        r = http_post(
            f"{base}/api/v1/chat/completions",
            body={"messages": "not_an_array", "stream": False},
            headers={"Authorization": f"Bearer {demo_key}"},
        )
        ok("Non-array messages → 400", r.status == 400, f"got {r.status}")

        # ── Conversation ownership isolation ─────────────────────
        print("\n--- Ownership isolation ---")
        s2 = Session(base)
        s2.get("/register")
        r = s2.post(
            "/register",
            body=f"username=other_{ts}&email=other_{ts}@test.com&password=other12345678",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Second user registered", r.status in (200, 302), f"got {r.status}")

        r = s2.get("/chat/ZZZINVALID999")
        ok("Other user: unknown chat → 404", r.status == 404, f"got {r.status}")

        # Second user's dashboard should have no conversations from demo user
        r = s2.get("/")
        ok("Other user dashboard loads", r.status == 200)

        # ── HyperAdmin ──────────────────────────────────────────
        print("\n--- HyperAdmin ---")
        r = http_get(f"{base}/admin/login/")
        ok("Admin login page", r.status == 200 and "username" in r.body)

        r = http_get(f"{base}/admin/")
        ok("Admin requires auth", r.status in (302, 303) or "login" in r.body.lower())

    # ── Summary ──
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)
