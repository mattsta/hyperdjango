"""
End-to-end tests for HyperNews P1 social features:
bookmarks, notifications, extended user profiles.
"""

# hyper-test: e2e

import json
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
    return urllib.parse.urlencode(data)


def login_session(base: str, username: str, password: str) -> Session:
    sess = Session(base_url=base)
    sess.get("/login")
    csrf_token = sess.cookie_jar.get("csrftoken", "")
    sess.post(
        "/login",
        body=_form_encode(
            {
                "username": username,
                "password": password,
                "_csrf_token": csrf_token,
            }
        ),
        content_type="application/x-www-form-urlencoded",
    )
    return sess


def main():
    global PASS, FAIL
    port = TEST_PORTS["hypernews_social"]
    base = f"http://127.0.0.1:{port}"

    print(f"\n=== HyperNews Social Features E2E Tests (port {port}) ===\n")

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
        # LAST — after every route, middleware and on_startup hook. The server
        # is ready by construction here; a guessed second on top of that
        # condition only measured how fast the machine booted it.

        # Health
        r = http_get(f"{base}/health")
        check("health", r)

        # Login as admin and alice
        admin_sess = login_session(base, "admin", SEED_PASSWORD)
        r = admin_sess.get("/account")
        check("admin login", r)

        alice_sess = login_session(base, "alice", SEED_PASSWORD)
        r = alice_sess.get("/account")
        check("alice login", r)

        # ------------------------------------------------------------------
        # Bookmarks
        # ------------------------------------------------------------------
        print("\n--- Bookmarks ---")

        # Saved page (empty initially)
        r = admin_sess.get("/saved")
        check("GET /saved (empty)", r)
        check_contains("saved page renders", r, "Saved")

        # Get a post ID to bookmark — use authenticated front page (has vote buttons with post_ids)
        r = admin_sess.get("/?tab=new")
        check("front page for post IDs", r)

        csrf = admin_sess.cookie_jar.get("csrftoken", "")

        # Extract post_id from hx-vals in vote buttons
        import re as _re

        post_ids = _re.findall(r'"post_id":\s*"(\d+)"', r.body)
        if post_ids:
            test_post_id = post_ids[0]
            r = admin_sess.post(
                "/bookmark",
                body=_form_encode({"post_id": test_post_id, "_csrf_token": csrf}),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST /bookmark (add)", r)
            if r.status == 200:
                data = json.loads(r.body)
                check_true("bookmark action=added", data.get("action") == "added")

            # Saved page should now have items
            r = admin_sess.get("/saved")
            check("GET /saved (has items)", r)

            # Toggle off — remove bookmark
            csrf = admin_sess.cookie_jar.get("csrftoken", "")
            r = admin_sess.post(
                "/bookmark",
                body=_form_encode({"post_id": test_post_id, "_csrf_token": csrf}),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST /bookmark (remove)", r)
            if r.status == 200:
                data = json.loads(r.body)
                check_true("bookmark action=removed", data.get("action") == "removed")
        else:
            check_true(
                "found post_id for bookmark test", False, "no post IDs in front page"
            )

        # Saved with filters
        r = admin_sess.get("/saved?type=posts")
        check("GET /saved?type=posts", r)

        r = admin_sess.get("/saved?type=comments")
        check("GET /saved?type=comments", r)

        # Unauthenticated → redirect
        r = http_get(f"{base}/saved")
        check("GET /saved unauthenticated → redirect", r, 302)

        # ------------------------------------------------------------------
        # Notifications / Inbox
        # ------------------------------------------------------------------
        print("\n--- Notifications ---")

        # Inbox (empty initially)
        r = admin_sess.get("/inbox")
        check("GET /inbox", r)
        check_contains("inbox page renders", r, "Inbox")

        # Unread count
        r = admin_sess.get("/inbox/count")
        check("GET /inbox/count", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("unread count is number", isinstance(data.get("unread"), int))

        # Create a comment with @mention to trigger notification
        # First, find a post to comment on
        if post_ids:
            csrf = alice_sess.cookie_jar.get("csrftoken", "")
            r = alice_sess.post(
                "/comment",
                body=_form_encode(
                    {
                        "post_id": test_post_id,
                        "text": "Hey @admin check this out!",
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST /comment with @mention", r, 302)

            # Admin should now have a notification
            r = admin_sess.get("/inbox/count")
            if r.status == 200:
                data = json.loads(r.body)
                check_true(
                    "admin has unread notifications",
                    data.get("unread", 0) > 0,
                    f"unread={data.get('unread')}",
                )

            r = admin_sess.get("/inbox")
            check("GET /inbox (with notifications)", r)
            check_contains("inbox has mention notification", r, "mentioned")

        # Mark all read
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/inbox/read",
            body=_form_encode({"_csrf_token": csrf}),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /inbox/read → redirect", r, 302)

        # Verify count is 0
        r = admin_sess.get("/inbox/count")
        if r.status == 200:
            data = json.loads(r.body)
            check_true("unread count is 0 after mark read", data.get("unread") == 0)

        # Unauthenticated inbox → redirect
        r = http_get(f"{base}/inbox")
        check("GET /inbox unauthenticated → redirect", r, 302)

        # ------------------------------------------------------------------
        # Send message (staff only)
        # ------------------------------------------------------------------
        print("\n--- Send message ---")
        r = admin_sess.post(
            "/messages/send",
            body="to_username=alice&subject=Test+message&body=Hello+from+E2E",
            content_type="application/x-www-form-urlencoded",
        )
        check_true(
            "Send message succeeds",
            r.status in (200, 302),
            f"got {r.status}",
        )

        # ------------------------------------------------------------------
        # Extended User Profile
        # ------------------------------------------------------------------
        print("\n--- User Profiles ---")

        # View admin profile
        r = http_get(f"{base}/user/admin")
        check("GET /user/admin", r)
        check_contains("profile shows username", r, "admin")
        check_contains("profile has karma breakdown link", r, "karma breakdown")

        # Karma breakdown API
        r = http_get(f"{base}/user/admin/karma")
        check("GET /user/admin/karma", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("karma has by_forum", "by_forum" in data)

        # Profile settings page
        r = admin_sess.get("/settings/profile")
        check("GET /settings/profile", r)
        check_contains("profile settings has fields", r, "website")

        # Update profile
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/settings/profile",
            body=_form_encode(
                {
                    "display_name": "Admin User",
                    "bio": "Site administrator",
                    "email": "admin@test.local",
                    "website": "https://example.com",
                    "location": "San Francisco",
                    "avatar_url": "",
                    "github_username": "admingh",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /settings/profile", r)
        check_contains("profile saved confirmation", r, "Profile updated")

        # Verify profile shows on user page
        r = http_get(f"{base}/user/admin")
        check("profile shows location", r)
        check_contains("profile has location", r, "San Francisco")
        check_contains("profile has github link", r, "admingh")

        # Profile settings unauthenticated → redirect
        r = http_get(f"{base}/settings/profile")
        check("GET /settings/profile unauthenticated → redirect", r, 302)

        # View alice profile (has forum memberships from seed)
        r = http_get(f"{base}/user/alice")
        check("GET /user/alice", r)

        # Nonexistent user
        r = http_get(f"{base}/user/nonexistent-user-xyz")
        check("GET /user/nonexistent → 404", r, 404)

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
