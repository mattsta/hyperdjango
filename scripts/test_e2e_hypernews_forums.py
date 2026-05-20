"""
End-to-end tests for HyperNews multi-forum architecture.

Tests forum CRUD, membership (join/leave), per-forum post submission,
forum directory, forum search, per-forum moderation, and all forum routes.
"""

# hyper-test: e2e

import json
import os
import re
import subprocess
import sys
import urllib.parse

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    E2EResponse,
    Session,
    http_get,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, response: E2EResponse, expected_status: int = 200) -> bool:
    global PASS, FAIL
    if response.status == expected_status:
        PASS += 1
        print(f"  PASS  {name} ({response.status})")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected_status}, got {response.status}"
    print(msg)
    ERRORS.append(msg)
    if response.body:
        print(f"        body: {response.body[:300]}")
    return False


def check_contains(name: str, response: E2EResponse, needle: str) -> bool:
    global PASS, FAIL
    if needle in response.body:
        PASS += 1
        print(f"  PASS  {name} (contains '{needle}')")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: body does not contain '{needle}'"
    print(msg)
    ERRORS.append(msg)
    return False


def check_true(name: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}{': ' + detail if detail else ''}"
    print(msg)
    ERRORS.append(msg)
    return False


def _form_encode(data: dict[str, str]) -> str:
    """URL-encode form data dict."""
    return urllib.parse.urlencode(data)


def login_session(base: str, username: str, password: str) -> Session:
    """Create a session and log in via form POST."""
    sess = Session(base_url=base)
    # Get CSRF token from login page
    sess.get("/login")
    csrf_token = sess.cookie_jar.get("csrftoken", "")
    # Post form-encoded login
    form_body = _form_encode(
        {
            "username": username,
            "password": password,
            "_csrf_token": csrf_token,
        }
    )
    sess.post(
        "/login", body=form_body, content_type="application/x-www-form-urlencoded"
    )
    return sess


def main():
    global PASS, FAIL
    port = TEST_PORTS["hypernews_forums"]
    base = f"http://127.0.0.1:{port}"

    # Unique forum name per run to avoid conflicts with shared DB
    test_forum_name = f"testforum-{os.getpid()}"

    print(
        f"\n=== HyperNews Forums E2E Tests (port {port}, forum={test_forum_name}) ===\n"
    )

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

    with AppRunner("services.hypernews.app:app", port=port) as runner:
        # No sleep before the first request: AppRunner.start() already blocks
        # until GET /_ready answers 200, and the framework registers /_ready
        # LAST — after every route, middleware and on_startup hook. "Let
        # startup hooks complete" was already guaranteed; the extra second only
        # measured how fast the machine booted the server.

        # ------------------------------------------------------------------
        # 1. Health check
        # ------------------------------------------------------------------
        print("\n--- Health ---")
        r = http_get(f"{base}/health")
        check("health", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("health.status", data.get("status") == "ok")

        # ------------------------------------------------------------------
        # 2. Forum directory (public, no auth)
        # ------------------------------------------------------------------
        print("\n--- Forum Directory ---")
        r = http_get(f"{base}/forums")
        check("GET /forums", r)

        # Sort by new
        r = http_get(f"{base}/forums?sort=new")
        check("GET /forums?sort=new", r)

        # ------------------------------------------------------------------
        # 3. Auth-required routes redirect
        # ------------------------------------------------------------------
        print("\n--- Auth Redirects ---")
        r = http_get(f"{base}/forums/create")
        check("GET /forums/create unauthenticated → redirect", r, 302)

        # ------------------------------------------------------------------
        # 4. Authenticated: Login as admin + seed forums
        # ------------------------------------------------------------------
        print("\n--- Authenticated (admin) ---")
        admin_sess = login_session(base, "admin", SEED_PASSWORD)
        r = admin_sess.get("/account")
        check("admin login success (GET /account)", r)

        # ------------------------------------------------------------------
        # 5. Forum creation (admin has enough karma)
        # ------------------------------------------------------------------
        print("\n--- Forum Creation ---")
        r = admin_sess.get("/forums/create")
        check("GET /forums/create (admin)", r)
        check_contains("create form has name field", r, "name")

        # Create a new forum
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/forums/create",
            body=_form_encode(
                {
                    "name": test_forum_name,
                    "title": "Test Forum",
                    "description": "A test forum for E2E testing",
                    "rules": "Be kind. No spam.",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /forums/create → redirect", r, 302)
        check_true(
            "redirect to new forum",
            f"/f/{test_forum_name}/" in r.headers.get("location", ""),
            f"location={r.headers.get('location', '')}",
        )

        # Verify the forum exists
        r = admin_sess.get(f"/f/{test_forum_name}/")
        check(f"GET /f/{test_forum_name}/ after creation", r)
        check_contains("new forum has title", r, "Test Forum")

        # Duplicate name
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/forums/create",
            body=_form_encode(
                {"name": test_forum_name, "title": "Duplicate", "_csrf_token": csrf}
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /forums/create duplicate → 400", r, 400)

        # Invalid name
        r = admin_sess.post(
            "/forums/create",
            body=_form_encode(
                {"name": "INVALID NAME!", "title": "Bad Name", "_csrf_token": csrf}
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /forums/create invalid name → 400", r, 400)

        # ------------------------------------------------------------------
        # 6. Forum pages
        # ------------------------------------------------------------------
        print("\n--- Forum Pages ---")
        r = http_get(f"{base}/f/{test_forum_name}/")
        check("GET /f/{test_forum_name}/", r)

        # Forum tabs
        for tab in ("hot", "new", "top"):
            r = http_get(f"{base}/f/{test_forum_name}/?tab={tab}")
            check(f"GET /f/{test_forum_name}/?tab={tab}", r)

        # Nonexistent forum
        r = http_get(f"{base}/f/nonexistent-forum/")
        check("GET /f/nonexistent/ → 404", r, 404)

        # ------------------------------------------------------------------
        # 7. Forum about page
        # ------------------------------------------------------------------
        print("\n--- Forum About ---")
        r = http_get(f"{base}/f/{test_forum_name}/about")
        check("GET /f/{test_forum_name}/about", r)
        check_contains("about has description", r, "E2E testing")
        check_contains("about has rules", r, "Be kind")

        # ------------------------------------------------------------------
        # 8. Forum members page
        # ------------------------------------------------------------------
        print("\n--- Forum Members ---")
        r = http_get(f"{base}/f/{test_forum_name}/members")
        check("GET /f/{test_forum_name}/members", r)

        # ------------------------------------------------------------------
        # 9. Forum search
        # ------------------------------------------------------------------
        print("\n--- Forum Search ---")
        r = http_get(f"{base}/forums/search?q=test")
        check("GET /forums/search?q=test", r)

        r = http_get(f"{base}/forums/search?q=nonexistent-gibberish-xyz")
        check("GET /forums/search no results", r)

        # Empty search redirects
        r = http_get(f"{base}/forums/search?q=")
        check("GET /forums/search empty → redirect", r, 302)

        # ------------------------------------------------------------------
        # 10. Post submission to a forum
        # ------------------------------------------------------------------
        print("\n--- Forum Post Submission ---")
        r = admin_sess.get(f"/f/{test_forum_name}/submit")
        check("GET /f/{test_forum_name}/submit", r)
        check_contains("submit form has forum context", r, test_forum_name)

        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/submit",
            body=_form_encode(
                {
                    "title": "First post in test forum",
                    "text": "This is a test post in the test forum.",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /f/{test_forum_name}/submit → redirect", r, 302)
        check_true(
            "redirect to post detail",
            "/post/" in r.headers.get("location", ""),
            f"location={r.headers.get('location', '')}",
        )

        # Verify post appears in forum
        r = admin_sess.get(f"/f/{test_forum_name}/?tab=new")
        check("forum shows new post", r)
        check_contains("post appears in forum", r, "First post in test forum")

        # Global submit with forum picker
        r = admin_sess.get("/submit")
        check("GET /submit (global with forum picker)", r)
        check_contains("submit has forum dropdown", r, test_forum_name)

        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/submit",
            body=_form_encode(
                {
                    "title": "Global post no forum",
                    "text": "This post has no forum.",
                    "forum": "",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /submit with no forum → redirect", r, 302)

        # ------------------------------------------------------------------
        # 11. Join/Leave forum
        # ------------------------------------------------------------------
        print("\n--- Forum Membership ---")

        # Login as alice
        alice_sess = login_session(base, "alice", SEED_PASSWORD)
        r = alice_sess.get("/account")
        check("alice login success", r)

        # Alice joins test-forum
        csrf = alice_sess.cookie_jar.get("csrftoken", "")
        r = alice_sess.post(
            f"/f/{test_forum_name}/join",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /f/{test_forum_name}/join → redirect", r, 302)

        # Verify membership — member page should show alice
        r = http_get(f"{base}/f/{test_forum_name}/members")
        check("members page after join", r)
        check_contains("alice appears in members", r, "alice")

        # Alice leaves test-forum
        csrf = alice_sess.cookie_jar.get("csrftoken", "")
        r = alice_sess.post(
            f"/f/{test_forum_name}/leave",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /f/{test_forum_name}/leave → redirect", r, 302)

        # ------------------------------------------------------------------
        # 11b. Report spam
        # ------------------------------------------------------------------
        print("\n--- Report spam ---")
        # Get a post ID from the forum
        r = admin_sess.get(f"/f/{test_forum_name}/")
        post_pids = re.findall(r'/post/([^\s"<]+)', r.body)
        if post_pids:
            r_detail = admin_sess.get(f"/post/{post_pids[0]}")
            internal_ids = re.findall(r'"post_id":\s*"(\d+)"', r_detail.body)
            report_post_id = internal_ids[0] if internal_ids else "1"
            r = admin_sess.post(
                "/report",
                body=f"post_id={report_post_id}&reason=Test+spam+report",
                content_type="application/x-www-form-urlencoded",
            )
            check_true(
                "POST /report succeeds",
                r.status in (200, 302),
                f"got {r.status}",
            )
        else:
            check_true("found post_id for report test", False, "no post IDs in forum")

        # ------------------------------------------------------------------
        # 11c. Mod note
        # ------------------------------------------------------------------
        print("\n--- Mod note ---")
        r = admin_sess.post(
            "/mod/note",
            body="target_user_id=1&note=Test+mod+note&visibility=mod_only",
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /mod/note succeeds", r)

        # ------------------------------------------------------------------
        # 12. Forum moderation
        # ------------------------------------------------------------------
        print("\n--- Forum Moderation ---")

        # Admin appoints alice as moderator
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/mod/appoint",
            body=_form_encode({"username": "alice", "_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /f/{test_forum_name}/mod/appoint → redirect", r, 302)

        # Verify alice shows as mod on about page
        r = http_get(f"{base}/f/{test_forum_name}/about")
        check("about page after mod appointment", r)
        check_contains("alice shows as mod", r, "alice")

        # Admin removes alice as mod
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/mod/remove",
            body=_form_encode({"username": "alice", "_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /f/{test_forum_name}/mod/remove → redirect", r, 302)

        # ------------------------------------------------------------------
        # 13. Forum directory shows all forums
        # ------------------------------------------------------------------
        print("\n--- Forum Directory ---")
        r = http_get(f"{base}/forums")
        check("forum directory shows forums", r)
        # Should have at least the test-forum we created
        check_contains("directory has test-forum", r, test_forum_name)

        # ------------------------------------------------------------------
        # 14. Non-member can view public forum
        # ------------------------------------------------------------------
        print("\n--- Public Forum Access ---")
        bob_sess = login_session(base, "bob", SEED_PASSWORD)
        r = bob_sess.get(f"/f/{test_forum_name}/")
        check("bob can view public forum without membership", r)

        # ------------------------------------------------------------------
        # 15. Forum editing (admin only)
        # ------------------------------------------------------------------
        print("\n--- Forum Editing ---")
        r = admin_sess.get(f"/f/{test_forum_name}/edit")
        check("GET /f/.../edit (admin)", r)
        check_contains("edit form has title field", r, "title")

        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/edit",
            body=_form_encode(
                {
                    "title": "Updated Test Forum",
                    "description": "Updated description for testing",
                    "rules": "Updated rules.",
                    "is_public": "on",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /f/.../edit → redirect", r, 302)

        # Verify update applied
        r = http_get(f"{base}/f/{test_forum_name}/about")
        check("about shows updated title", r)
        check_contains("updated description", r, "Updated description")

        # Non-admin cannot edit
        r = alice_sess.get(f"/f/{test_forum_name}/edit")
        check("alice cannot edit non-admin forum → 403", r, 403)

        # ------------------------------------------------------------------
        # 16. Count reconciliation (staff endpoint)
        # ------------------------------------------------------------------
        print("\n--- Count Reconciliation ---")
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/analytics/reconcile-counts",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /analytics/reconcile-counts", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("reconcile returns ok", data.get("ok") is True)

        r = admin_sess.get("/analytics/count-drift")
        check("GET /analytics/count-drift", r)

        # ------------------------------------------------------------------
        # 16b. Social graph analytics
        # ------------------------------------------------------------------
        print("\n--- Social Graph Analytics ---")
        r = admin_sess.get("/analytics/rings")
        check_true("GET /analytics/rings", r.status in (200, 404), f"got {r.status}")

        r = admin_sess.get("/analytics/domains")
        check_true("GET /analytics/domains", r.status in (200, 404), f"got {r.status}")

        r = admin_sess.get("/analytics/centrality")
        check_true(
            "GET /analytics/centrality", r.status in (200, 404), f"got {r.status}"
        )

        r = admin_sess.get("/analytics/communities")
        check_true(
            "GET /analytics/communities", r.status in (200, 404), f"got {r.status}"
        )

        r = admin_sess.get("/analytics/affinity/admin")
        check_true(
            "GET /analytics/affinity/{user}", r.status in (200, 404), f"got {r.status}"
        )

        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/analytics/refresh-graph",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check_true(
            "POST /analytics/refresh-graph", r.status in (200, 403), f"got {r.status}"
        )

        # ------------------------------------------------------------------
        # 17. Archive forum blocks new posts
        # ------------------------------------------------------------------
        print("\n--- Archived Forum ---")
        # Archive the test forum
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/edit",
            body=_form_encode(
                {
                    "title": "Updated Test Forum",
                    "description": "Updated description for testing",
                    "rules": "Updated rules.",
                    "is_public": "on",
                    "is_archived": "on",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST archive forum → redirect", r, 302)

        # Try to submit to archived forum
        r = admin_sess.get(f"/f/{test_forum_name}/submit")
        check("GET submit to archived → 403", r, 403)

        # Unarchive for cleanup (keep public)
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/edit",
            body=_form_encode(
                {
                    "title": "Updated Test Forum",
                    "description": "Updated description for testing",
                    "rules": "Updated rules.",
                    "is_public": "on",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST unarchive forum → redirect", r, 302)

        # ------------------------------------------------------------------
        # 18. Lock forum blocks new posts
        # ------------------------------------------------------------------
        print("\n--- Locked Forum ---")
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/edit",
            body=_form_encode(
                {
                    "title": "Updated Test Forum",
                    "description": "Updated description for testing",
                    "rules": "Updated rules.",
                    "is_public": "on",
                    "is_locked": "on",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST lock forum → redirect", r, 302)

        r = admin_sess.get(f"/f/{test_forum_name}/submit")
        check("GET submit to locked → 403", r, 403)

        # Can still browse locked forum
        r = http_get(f"{base}/f/{test_forum_name}/")
        check("GET locked forum still browsable", r)

        # Unlock (keep public)
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/edit",
            body=_form_encode(
                {
                    "title": "Updated Test Forum",
                    "description": "Updated description for testing",
                    "rules": "Updated rules.",
                    "is_public": "on",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST unlock forum → redirect", r, 302)

        # ------------------------------------------------------------------
        # 19. Admin transfer
        # ------------------------------------------------------------------
        print("\n--- Admin Transfer ---")
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum_name}/mod/transfer",
            body=_form_encode({"username": "alice", "_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST transfer admin to alice → redirect", r, 302)

        # Alice is now admin — verify she can edit
        r = alice_sess.get(f"/f/{test_forum_name}/edit")
        check("alice can edit as new admin", r)

        # Admin is now moderator — verify edit is denied
        r = admin_sess.get(f"/f/{test_forum_name}/edit")
        # admin is site staff so still allowed
        check("admin (site staff) can still edit", r)

        # Transfer back
        csrf = alice_sess.cookie_jar.get("csrftoken", "")
        r = alice_sess.post(
            f"/f/{test_forum_name}/mod/transfer",
            body=_form_encode({"username": "admin", "_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST transfer admin back to admin → redirect", r, 302)

        # ------------------------------------------------------------------
        # 20. Audit log
        # ------------------------------------------------------------------
        print("\n--- Audit Log ---")
        r = admin_sess.get(f"/f/{test_forum_name}/audit")
        check("GET /f/.../audit", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true(
                "audit log has actions",
                len(data.get("actions", [])) > 0,
                f"got {len(data.get('actions', []))} actions",
            )
            # Verify transfer actions are logged
            action_types = [a["action"] for a in data["actions"]]
            check_true(
                "transfer_admin in audit log",
                "transfer_admin" in action_types,
                f"actions: {action_types}",
            )

        # ------------------------------------------------------------------
        # 21. Forum soft-delete (staff only)
        # ------------------------------------------------------------------
        print("\n--- Forum Delete ---")
        # Create a throwaway forum to delete
        delete_forum_name = f"deleteme-{os.getpid()}"
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/forums/create",
            body=_form_encode(
                {
                    "name": delete_forum_name,
                    "title": "Delete Me",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("create forum to delete", r, 302)

        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{delete_forum_name}/delete",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST delete forum → redirect", r, 302)

        # Deleted forum should not appear in directory
        r = http_get(f"{base}/forums")
        check_true(
            "deleted forum hidden from directory",
            delete_forum_name not in r.body,
            f"found {delete_forum_name} in directory body",
        )

        # Still accessible to admin (member) via direct URL, but 404 for anonymous
        r = admin_sess.get(f"/f/{delete_forum_name}/")
        check("deleted forum still accessible to admin via direct URL", r)
        r_anon = http_get(f"{base}/f/{delete_forum_name}/")
        check("deleted forum is 404 for anonymous", r_anon, expected_status=404)

        # Non-staff cannot delete
        csrf = alice_sess.cookie_jar.get("csrftoken", "")
        r = alice_sess.post(
            f"/f/{test_forum_name}/delete",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("alice cannot delete → 403", r, 403)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailed tests:")
        for e in ERRORS:
            print(f"  {e}")
    print(f"{'=' * 60}\n")

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
