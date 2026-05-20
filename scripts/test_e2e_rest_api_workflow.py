"""
Deep workflow tests for REST API: full CRUD + edge cases.

Register → Login → Create posts → List → Get → Update → Verify →
Delete → Verify gone → Ownership → Error responses → Admin API.
"""

# hyper-test: e2e

import subprocess
import time

from e2e_helper import TEST_PORTS, AppRunner, Session, http_get

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
    print("REST API Deep Workflow Tests")
    print("=" * 60)

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.rest_api.app:app",
            "--drop",
            "--seed",
            "services.rest_api.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.rest_api.app:app",
        host="127.0.0.1",
        port=TEST_PORTS["rest_api_workflow"],
    ) as runner:
        base = runner.url()
        ts = str(int(time.time()))

        # ── 1. Register user A ───────────────────────────────────────
        print("\n--- 1. Register user A ---")
        s1 = Session(base)
        r = s1.post(
            "/auth/register",
            body={
                "username": f"userA{ts}",
                "email": f"a{ts}@test.com",
                "password": "password123",
            },
        )
        ok("User A registration", r.status == 201)
        ok("Registration returns username", f"userA{ts}" in r.body)

        # Login user A
        r = s1.post(
            "/auth/login",
            body={
                "username": f"userA{ts}",
                "password": "password123",
            },
        )
        ok("User A login", r.status == 200)
        ok("Login sets session cookie", "sessionid" in s1.cookie_jar)

        # ── 2. Register user B ───────────────────────────────────────
        print("\n--- 2. Register user B ---")
        s2 = Session(base)
        r = s2.post(
            "/auth/register",
            body={
                "username": f"userB{ts}",
                "email": f"b{ts}@test.com",
                "password": "password456",
            },
        )
        ok("User B registration", r.status == 201)
        r = s2.post(
            "/auth/login",
            body={
                "username": f"userB{ts}",
                "password": "password456",
            },
        )
        ok("User B login", r.status == 200)

        # ── 3. User A creates posts ──────────────────────────────────
        print("\n--- 3. Create posts (user A) ---")
        r = s1.post(
            "/api/posts",
            body={
                "title": f"First Post {ts}",
                "body": "This is the body of the first post.",
            },
        )
        ok("Create post 1", r.status == 201)
        post1 = r.json
        ok("Post 1 has id", "id" in post1)
        ok("Post 1 title correct", post1.get("title") == f"First Post {ts}")
        post1_id = post1.get("id", 0)

        r = s1.post(
            "/api/posts",
            body={
                "title": f"Second Post {ts}",
                "body": "Body of second post.",
            },
        )
        ok("Create post 2", r.status == 201)
        post2_id = r.json.get("id", 0)

        # ── 4. List posts ────────────────────────────────────────────
        print("\n--- 4. List posts ---")
        r = s1.get("/api/posts")
        ok("List posts succeeds", r.status == 200)
        data = r.json
        posts = data.get("results", data) if isinstance(data, dict) else data
        ok("Posts is a list", isinstance(posts, list))
        ok("Posts has our entries", len(posts) >= 2)

        # ── 5. Get single post ───────────────────────────────────────
        print("\n--- 5. Get single post ---")
        r = s1.get(f"/api/posts/{post1_id}")
        ok("Get post 1", r.status == 200)
        data = r.json
        ok("Post data has correct title", data.get("title") == f"First Post {ts}")
        ok(
            "Post data has body",
            data.get("body") == "This is the body of the first post.",
        )
        ok("Post data has author_id", "author_id" in data)

        # ── 6. Update post ───────────────────────────────────────────
        print("\n--- 6. Update post (owner) ---")
        r = s1.put(
            f"/api/posts/{post1_id}",
            body={
                "title": f"Updated First Post {ts}",
                "body": "Updated body content.",
            },
        )
        ok("Update post succeeds", r.status == 200)
        ok("Update response confirms", r.json.get("updated") is True)

        # Verify update persisted
        r = s1.get(f"/api/posts/{post1_id}")
        ok("Updated title persisted", r.json.get("title") == f"Updated First Post {ts}")
        ok("Updated body persisted", r.json.get("body") == "Updated body content.")

        # ── 7. Ownership enforcement ─────────────────────────────────
        print("\n--- 7. Ownership checks ---")
        # User B tries to update user A's post
        r = s2.put(
            f"/api/posts/{post1_id}",
            body={
                "title": "Hacked!",
            },
        )
        ok("Non-owner cannot update", r.status == 404, f"got {r.status}")

        # User B tries to delete user A's post
        r = s2.delete(f"/api/posts/{post1_id}")
        ok("Non-owner cannot delete", r.status == 404, f"got {r.status}")

        # Verify post still exists and unchanged
        r = s1.get(f"/api/posts/{post1_id}")
        ok("Post survives unauthorized delete", r.status == 200)
        ok(
            "Post title unchanged after attack",
            r.json.get("title") == f"Updated First Post {ts}",
        )

        # ── 8. Delete post ───────────────────────────────────────────
        print("\n--- 8. Delete post (owner) ---")
        r = s1.delete(f"/api/posts/{post2_id}")
        ok("Delete post 2 succeeds", r.status == 204 or r.status == 200)

        # Verify deletion
        r = s1.get(f"/api/posts/{post2_id}")
        ok("Deleted post returns 404", r.status == 404)

        # ── 9. Validation errors ─────────────────────────────────────
        print("\n--- 9. Validation ---")
        r = s1.post("/api/posts", body={"title": "", "body": ""})
        ok("Empty title/body → 400", r.status == 400)

        r = s1.post("/api/posts", body={})
        ok("Missing fields → 400", r.status == 400)

        # ── 10. Non-existent resources ───────────────────────────────
        print("\n--- 10. Not found ---")
        r = s1.get("/api/posts/INVALID_OPAQUE_ID")
        ok(
            "Non-existent post → 404",
            r.status == 404,
            f"got {r.status}: {r.body[:100]}",
        )

        r = s1.put("/api/posts/INVALID_OPAQUE_ID", body={"title": "x"})
        ok(
            "Update non-existent → 404",
            r.status == 404,
            f"got {r.status}: {r.body[:100]}",
        )

        r = s1.delete("/api/posts/INVALID_OPAQUE_ID")
        ok(
            "Delete non-existent → 404",
            r.status == 404,
            f"got {r.status}: {r.body[:100]}",
        )

        # ── 11. Admin API key endpoints ──────────────────────────────
        print("\n--- 11. Admin API endpoints ---")
        admin_h = {"X-API-Key": "sk_live_demo_key_123"}
        r = http_get(f"{base}/api/admin/stats", headers=admin_h)
        ok("Admin stats", r.status == 200)
        stats = r.json
        ok("Stats has user_count", "user_count" in stats)
        ok("Stats has post_count", "post_count" in stats)

        r = http_get(f"{base}/api/admin/users", headers=admin_h)
        ok("Admin user list", r.status == 200)
        data = r.json
        users = data.get("results", data) if isinstance(data, dict) else data
        ok("User list is array", isinstance(users, list))
        ok("User list has our users", len(users) >= 2)

        # ── 12. Logout ───────────────────────────────────────────────
        print("\n--- 12. Logout ---")
        r = s1.post("/auth/logout")
        ok("Logout succeeds", r.status == 200)

        # Post-logout should fail
        r = s1.post("/api/posts", body={"title": "x", "body": "x"})
        ok("Post after logout → 401", r.status == 401)

        # ── 13. OpenAPI spec ─────────────────────────────────────────
        print("\n--- 13. OpenAPI ---")
        r = http_get(f"{base}/openapi.json")
        ok("OpenAPI spec loads", r.status == 200)
        spec = r.json
        ok("Spec has info.title", spec.get("info", {}).get("title") == "Blog API")
        ok("Spec has paths", len(spec.get("paths", {})) >= 5)

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
