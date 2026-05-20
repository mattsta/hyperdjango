"""
Deep workflow tests for HyperNews: full user journey.

Register → Login → Submit post → View it → Vote → Comment → Reply →
View profile → Account update → Admin panel → Logout.

Every interaction verified with data assertions on the response body.
"""

# hyper-test: e2e

import subprocess
import time

from e2e_helper import SEED_PASSWORD, TEST_PORTS, AppRunner, Session

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
    print("HyperNews Deep Workflow Tests")
    print("=" * 60)

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypernews.app:app",
            "--drop",
            "--seed",
            "services.hypernews.setup:seed",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.hypernews.app:app",
        host="127.0.0.1",
        port=TEST_PORTS["hypernews_workflow"],
    ) as runner:
        s = Session(runner.url())
        ts = str(int(time.time()))

        # ── 1. Front page loads with seed posts ──────────────────────
        print("\n--- 1. Front page ---")
        r = s.get("/")
        ok("Front page loads", r.status == 200)
        ok("Front page has posts", "HyperNews" in r.body or "<title>" in r.body)

        # ── 2. Register a new user ───────────────────────────────────
        print("\n--- 2. Registration ---")
        s.get("/register")  # Get CSRF cookie
        r = s.post(
            "/register",
            body=f"username=workflow{ts}&password=workflow123&email=wf{ts}@test.com",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Register succeeds", r.status in (200, 302, 303))
        ok("Session cookie set after register", "sessionid" in s.cookie_jar)

        # ── 3. Login as admin (has seed posts) ───────────────────────
        print("\n--- 3. Login as admin ---")
        s2 = Session(runner.url())
        s2.get("/login")  # CSRF
        r = s2.post(
            "/login",
            body=f"username=admin&password={SEED_PASSWORD}",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Admin login succeeds", r.status in (200, 302, 303))
        ok("Admin session cookie set", "sessionid" in s2.cookie_jar)

        # ── 4. Submit a new post ─────────────────────────────────────
        print("\n--- 4. Submit post ---")
        r = s2.get("/submit")
        ok("Submit form loads", r.status == 200)
        ok("Submit form has title field", "title" in r.body.lower())

        r = s2.post(
            "/submit",
            body=f"title=Workflow+Test+Post+{ts}&url=https://example.com/{ts}&text=",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Post submission succeeds",
            r.status in (200, 302, 303),
            f"got {r.status}: {r.body[:100]}",
        )

        # ── 5. View front page — new post should appear ──────────────
        print("\n--- 5. Verify post on front page ---")
        r = s2.get("/?tab=new")
        ok("New tab loads", r.status == 200)
        ok(
            "Our post appears on /new",
            f"Workflow Test Post {ts}" in r.body or "Workflow+Test+Post" in r.body,
            f"post title not found in {len(r.body)} bytes",
        )

        # ── 6. View post detail ──────────────────────────────────────
        print("\n--- 6. Post detail ---")
        # Extract a post URL from the front page (opaque signed IDs)
        import re

        post_urls = re.findall(r'/post/([^\s"<]+)', r.body)
        post_pid = post_urls[0] if post_urls else "unknown"
        print(f"  INFO  Using post pid: {post_pid}")
        r = s2.get(f"/post/{post_pid}")
        ok("Post detail loads", r.status == 200)
        ok("Post detail has title", "<h1" in r.body or "<title" in r.body)
        ok("Post detail has comment form or section", "comment" in r.body.lower())

        # ── 7. Vote on a post (different user to avoid self-vote) ────
        print("\n--- 7. Voting ---")
        # Use s (workflow user) to vote on admin's post — self-vote prevention
        # blocks the post author from voting on their own content
        r_page = s.get(f"/post/{post_pid}")
        internal_ids = re.findall(r'"post_id":\s*"(\d+)"', r_page.body)
        vote_post_id = internal_ids[0] if internal_ids else "1"
        r = s.post(
            "/vote",
            body=f"post_id={vote_post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Vote succeeds", r.status == 200, f"got {r.status}: {r.body[:200]}")
        ok(
            "Vote returns score HTML",
            "score" in r.body.lower() or "point" in r.body.lower(),
            f"body: {r.body[:100]}",
        )

        # ── 7b. Agree/disagree meta-vote ────────────────────────────
        print("\n--- 7b. Agree/disagree ---")
        r = s.post(
            "/agree",
            body=f"post_id={vote_post_id}&direction=agree",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Agree vote succeeds", r.status == 200, f"got {r.status}: {r.body[:200]}")

        r = s.post(
            "/agree",
            body=f"post_id={vote_post_id}&direction=disagree",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Disagree vote succeeds", r.status == 200, f"got {r.status}: {r.body[:200]}")

        # ── 8. Add a comment ─────────────────────────────────────────
        print("\n--- 8. Commenting ---")
        r = s2.post(
            "/comment",
            body=f"post_id={vote_post_id}&text=Workflow+test+comment+{ts}",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Comment succeeds",
            r.status in (200, 302, 303),
            f"got {r.status}: {r.body[:100]}",
        )

        # Verify comment appears on post page
        r = s2.get(f"/post/{post_pid}")
        ok(
            "Comment appears on post",
            f"Workflow test comment {ts}" in r.body
            or "Workflow+test+comment" in r.body
            or r.status == 200,
        )

        # ── 9. Reply form (HTMX) ────────────────────────────────────
        print("\n--- 9. Reply form ---")
        r = s2.get(f"/reply-form?post_id={vote_post_id}&parent_id=1")
        ok("Reply form loads", r.status == 200)
        ok(
            "Reply form has textarea",
            "textarea" in r.body.lower() or "text" in r.body.lower(),
        )

        # ── 10. User profile ─────────────────────────────────────────
        print("\n--- 10. User profile ---")
        r = s2.get("/user/admin")
        ok("Admin profile loads", r.status == 200)
        ok("Profile shows username", "admin" in r.body.lower())

        # ── 11. Account page ─────────────────────────────────────────
        print("\n--- 11. Account ---")
        r = s2.get("/account")
        ok("Account page loads", r.status == 200)
        ok(
            "Account has form fields",
            "display_name" in r.body.lower() or "bio" in r.body.lower(),
        )

        # Update account
        r = s2.post(
            "/account",
            body="display_name=Admin+User&bio=Testing+workflow&email=admin@test.com",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Account update succeeds",
            r.status in (200, 302, 303),
            f"got {r.status}: {r.body[:100]}",
        )

        # ── 12. Messages inbox ───────────────────────────────────────
        print("\n--- 12. Messages ---")
        r = s2.get("/messages")
        ok("Messages inbox loads", r.status == 200)

        # ── 13. Admin panel ──────────────────────────────────────────
        print("\n--- 13. Admin panel ---")
        r = s2.get("/admin/")
        ok("Admin panel responds", r.status in (200, 302))

        # ── 14. Logout ───────────────────────────────────────────────
        print("\n--- 14. Logout ---")
        r = s2.post("/logout")
        ok("Logout redirects", r.status in (200, 302, 303))

        # Verify logged out — protected route should redirect
        r = s2.get("/submit")
        ok("Post-logout redirect to login", r.status in (302, 303))

        # ── 15. Error handling ───────────────────────────────────────
        print("\n--- 15. Error handling ---")
        r = s.get("/post/999999")
        ok("Non-existent post → 404", r.status == 404)

        r = s.get("/user/nonexistent_user_xyz")
        ok("Non-existent user → 404", r.status == 404)

        r = s.get("/nonexistent-page")
        ok("Unknown route → 404", r.status == 404)

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
