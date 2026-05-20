"""
End-to-end tests for HyperNews P2 features:
drafts, post editing, polls, crossposting, user flairs, RSS feeds.
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
    port = TEST_PORTS["hypernews_p2"]
    base = f"http://127.0.0.1:{port}"
    test_suffix = str(os.getpid())

    print(f"\n=== HyperNews P2 Features E2E Tests (port {port}) ===\n")

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
        r = http_get(f"{base}/health")
        check("health", r)

        admin_sess = login_session(base, "admin", SEED_PASSWORD)
        check("admin login", admin_sess.get("/account"))

        alice_sess = login_session(base, "alice", SEED_PASSWORD)
        check("alice login", alice_sess.get("/account"))

        # Create a test forum for this run (idempotent)
        test_forum = f"p2test-{test_suffix}"
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        admin_sess.post(
            "/forums/create",
            body=_form_encode(
                {"name": test_forum, "title": "P2 Test Forum", "_csrf_token": csrf}
            ),
            content_type="application/x-www-form-urlencoded",
        )

        # Create a test post we can edit/crosspost/poll (via normal submit, not draft)
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/submit",
            body=_form_encode(
                {
                    "title": f"Editable Post {test_suffix}",
                    "text": "Original text content.",
                    "forum": test_forum,
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("create test post for editing", r, 302)
        test_post_pid = ""
        loc = r.headers.get("location", "")
        m = re.search(r"/post/([A-Za-z0-9._-]+)", loc)
        if m:
            test_post_pid = m.group(1)
        check_true("got test post PID", bool(test_post_pid), f"location={loc}")

        # ==============================================================
        # DRAFTS
        # ==============================================================
        print("\n--- Drafts ---")

        # Save a draft
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/draft",
            body=_form_encode(
                {
                    "title": f"Draft Post {test_suffix}",
                    "text": "This is a draft, not published yet.",
                    "forum": "",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST /draft → redirect", r, 302)

        # Drafts list
        r = admin_sess.get("/drafts")
        check("GET /drafts", r)
        check_contains("draft appears in list", r, f"Draft Post {test_suffix}")

        # Draft should NOT appear on front page
        r = http_get(f"{base}/?tab=new")
        check_true("draft not on front page", f"Draft Post {test_suffix}" not in r.body)

        # Publish the draft — need to find its PID
        r = admin_sess.get("/drafts")
        pids = re.findall(r"/post/([A-Za-z0-9._-]+)", r.body)
        if pids:
            draft_pid = pids[0]
            csrf = admin_sess.cookie_jar.get("csrftoken", "")
            r = admin_sess.post(
                f"/post/{draft_pid}/publish",
                body=_form_encode({"_csrf_token": csrf}),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST /post/.../publish → redirect", r, 302)
        else:
            check_true("found draft PID", False, "no PIDs in drafts page")

        # Unauthenticated drafts → redirect
        r = http_get(f"{base}/drafts")
        check("GET /drafts unauthenticated → redirect", r, 302)

        # ==============================================================
        # POST EDITING + REVISION HISTORY
        # ==============================================================
        print("\n--- Post Editing ---")

        if test_post_pid:
            csrf = admin_sess.cookie_jar.get("csrftoken", "")
            r = admin_sess.post(
                f"/post/{test_post_pid}/edit",
                body=_form_encode(
                    {
                        "title": f"Edited Title {test_suffix}",
                        "text": "Edited text content.",
                        "edit_reason": "fixed typo",
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST /post/.../edit → redirect", r, 302)

            r = http_get(f"{base}/post/{test_post_pid}/history")
            check("GET /post/.../history", r)
            if r.status == 200:
                data = json.loads(r.body)
                check_true(
                    "has revisions",
                    data.get("revision_count", 0) > 0,
                    f"count={data.get('revision_count')}",
                )
                check_true(
                    "reason recorded",
                    any(
                        "fixed typo" in rev.get("reason", "")
                        for rev in data.get("revisions", [])
                    ),
                )

            # Non-author cannot edit
            csrf = alice_sess.cookie_jar.get("csrftoken", "")
            r = alice_sess.post(
                f"/post/{test_post_pid}/edit",
                body=_form_encode(
                    {
                        "title": "Hacked title",
                        "text": "Hacked text",
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("non-author edit → 403", r, 403)

        # ==============================================================
        # POLLS
        # ==============================================================
        print("\n--- Polls ---")

        # Create a dedicated post for the poll
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            "/submit",
            body=_form_encode(
                {
                    "title": f"Poll Test {test_suffix}",
                    "text": "What do you prefer?",
                    "forum": test_forum,
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("create post for poll", r, 302)
        poll_post_pid = ""
        loc = r.headers.get("location", "")
        m = re.search(r"/post/([A-Za-z0-9._-]+)", loc)
        if m:
            poll_post_pid = m.group(1)

        if poll_post_pid:
            # Attach poll
            csrf = admin_sess.cookie_jar.get("csrftoken", "")
            r = admin_sess.post(
                f"/post/{poll_post_pid}/poll",
                body=_form_encode(
                    {
                        "question": "Favorite language?",
                        "poll_type": "single_choice",
                        "options": "Python\nRust\nZig\nGo",
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST /post/.../poll → redirect", r, 302)

            # Duplicate poll → 400
            csrf = admin_sess.cookie_jar.get("csrftoken", "")
            r = admin_sess.post(
                f"/post/{poll_post_pid}/poll",
                body=_form_encode(
                    {
                        "question": "Duplicate",
                        "options": "A\nB",
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("duplicate poll → 400", r, 400)

            # Get poll results (find poll_id)
            # Find poll by checking all polls
            db_polls = admin_sess.get(f"/post/{poll_post_pid}")
            # Get poll ID from the admin API or just use poll_id=1 approach
            # Actually let's use the results endpoint with a search
            # We need poll_id — let's query it
            r = http_get(f"{base}/poll/1/results")
            if r.status == 200:
                data = json.loads(r.body)
                check_true("poll has options", len(data.get("options", [])) >= 2)
                poll_id = 1

                # Vote on poll
                option_id = data["options"][0]["id"]
                csrf = alice_sess.cookie_jar.get("csrftoken", "")
                r = alice_sess.post(
                    f"/poll/{poll_id}/vote",
                    body=_form_encode(
                        {
                            "option_id": str(option_id),
                            "_csrf_token": csrf,
                        }
                    ),
                    content_type="application/x-www-form-urlencoded",
                )
                check("POST /poll/.../vote", r)
                if r.status == 200:
                    vdata = json.loads(r.body)
                    check_true("vote action=voted", vdata.get("action") == "voted")

                # Check results updated
                r = http_get(f"{base}/poll/{poll_id}/results")
                if r.status == 200:
                    data = json.loads(r.body)
                    check_true("total votes > 0", data.get("total_votes", 0) > 0)

                # Toggle vote off
                csrf = alice_sess.cookie_jar.get("csrftoken", "")
                r = alice_sess.post(
                    f"/poll/{poll_id}/vote",
                    body=_form_encode(
                        {
                            "option_id": str(option_id),
                            "_csrf_token": csrf,
                        }
                    ),
                    content_type="application/x-www-form-urlencoded",
                )
                check("toggle vote off", r)
                if r.status == 200:
                    vdata = json.loads(r.body)
                    check_true("vote action=removed", vdata.get("action") == "removed")
            else:
                check("poll results accessible", r)

        # ==============================================================
        # CROSSPOSTING
        # ==============================================================
        print("\n--- Crossposting ---")

        # Create a forum to crosspost to
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        xpost_forum = f"xpost-{test_suffix}"
        r = admin_sess.post(
            "/forums/create",
            body=_form_encode(
                {
                    "name": xpost_forum,
                    "title": "Crosspost Target",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("create crosspost target forum", r, 302)

        # Crosspost the test post
        if test_post_pid:
            csrf = admin_sess.cookie_jar.get("csrftoken", "")
            r = admin_sess.post(
                f"/post/{test_post_pid}/crosspost",
                body=_form_encode(
                    {
                        "forum": xpost_forum,
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST crosspost → redirect", r, 302)
            check_true(
                "crosspost redirects to new post",
                "/post/" in r.headers.get("location", ""),
            )

        # ==============================================================
        # USER FLAIRS
        # ==============================================================
        print("\n--- User Flairs ---")

        # Self-assign flair
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum}/flair",
            body=_form_encode(
                {
                    "flair_text": "Site Admin",
                    "css_class": "admin",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST self-assign flair", r)

        # Get flair
        r = http_get(f"{base}/f/{test_forum}/flair/admin")
        check("GET flair", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("flair text correct", data.get("flair") == "Site Admin")
            check_true("flair self-assigned", data.get("self_assigned") is True)

        # Mod assigns flair to another user
        csrf = admin_sess.cookie_jar.get("csrftoken", "")
        r = admin_sess.post(
            f"/f/{test_forum}/flair",
            body=_form_encode(
                {
                    "username": "alice",
                    "flair_text": "Community Member",
                    "css_class": "verified",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST assign flair to alice", r)

        r = http_get(f"{base}/f/{test_forum}/flair/alice")
        check("GET alice flair", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("alice flair assigned", data.get("flair") == "Community Member")
            check_true(
                "alice flair not self-assigned", data.get("self_assigned") is False
            )

        # ==============================================================
        # RSS FEEDS
        # ==============================================================
        print("\n--- RSS Feeds ---")

        r = http_get(f"{base}/feed/rss")
        check("GET /feed/rss", r)
        check_contains("RSS has channel", r, "<channel>")
        check_contains("RSS has items", r, "<item>")

        r = http_get(f"{base}/f/{test_forum}/feed/rss")
        check("GET /f/{forum}/feed/rss", r)
        check_contains("forum RSS has channel", r, "<channel>")

        r = http_get(f"{base}/user/admin/feed/rss")
        check("GET /user/admin/feed/rss", r)
        check_contains("user RSS has channel", r, "<channel>")

        # Private forum RSS → 404
        # (no private forum in seed, skip)

    # ==============================================================
    # Summary
    # ==============================================================
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
