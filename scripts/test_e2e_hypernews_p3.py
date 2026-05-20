"""
End-to-end tests for HyperNews P3 features:
pinning, awards, sequences, related posts, automod rules.
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
    csrf = sess.cookie_jar.get("csrftoken", "")
    sess.post(
        "/login",
        body=_form_encode(
            {
                "username": username,
                "password": password,
                "_csrf_token": csrf,
            }
        ),
        content_type="application/x-www-form-urlencoded",
    )
    return sess


def main():
    global PASS, FAIL
    port = TEST_PORTS["hypernews_p3"]
    base = f"http://127.0.0.1:{port}"
    sfx = str(os.getpid())

    print(f"\n=== HyperNews P3 E2E Tests (port {port}) ===\n")

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

        admin = login_session(base, "admin", SEED_PASSWORD)
        check("admin login", admin.get("/account"))
        alice = login_session(base, "alice", SEED_PASSWORD)
        check("alice login", alice.get("/account"))

        # Create test forum + post
        test_forum = f"p3test-{sfx}"
        csrf = admin.cookie_jar.get("csrftoken", "")
        admin.post(
            "/forums/create",
            body=_form_encode(
                {
                    "name": test_forum,
                    "title": "P3 Test",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )

        csrf = admin.cookie_jar.get("csrftoken", "")
        r = admin.post(
            "/submit",
            body=_form_encode(
                {
                    "title": f"P3 Test Post {sfx}",
                    "text": "Test content with https://example.com link.",
                    "forum": test_forum,
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        test_pid = ""
        m = re.search(r"/post/([A-Za-z0-9._-]+)", r.headers.get("location", ""))
        if m:
            test_pid = m.group(1)
        check_true("got test PID", bool(test_pid))

        # ==============================================================
        # PINNING
        # ==============================================================
        print("\n--- Pinning ---")
        csrf = admin.cookie_jar.get("csrftoken", "")
        r = admin.post(
            f"/f/{test_forum}/pin/{test_pid}",
            body=_form_encode(
                {
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST pin post", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("post is pinned", data.get("is_pinned") is True)

        # Unpin
        csrf = admin.cookie_jar.get("csrftoken", "")
        r = admin.post(
            f"/f/{test_forum}/pin/{test_pid}",
            body=_form_encode(
                {
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST unpin post", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("post is unpinned", data.get("is_pinned") is False)

        # Non-mod cannot pin
        csrf = alice.cookie_jar.get("csrftoken", "")
        r = alice.post(
            f"/f/{test_forum}/pin/{test_pid}",
            body=_form_encode(
                {
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("alice cannot pin → 403", r, 403)

        # ==============================================================
        # AWARDS
        # ==============================================================
        print("\n--- Awards ---")

        # Create a post by alice so admin (1000 karma) can award it (no self-award)
        csrf = alice.cookie_jar.get("csrftoken", "")
        r = alice.post(
            "/submit",
            body=_form_encode(
                {
                    "title": f"Alice Award Target {sfx}",
                    "text": "Award me!",
                    "forum": test_forum,
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        alice_pid = ""
        m = re.search(r"/post/([A-Za-z0-9._-]+)", r.headers.get("location", ""))
        if m:
            alice_pid = m.group(1)

        if alice_pid:
            # Get numeric post_id from the post detail page
            r = admin.get(f"/post/{alice_pid}")
            post_ids = re.findall(r'"post_id":\s*"(\d+)"', r.body)
            if post_ids:
                numeric_id = post_ids[0]
                csrf = admin.cookie_jar.get("csrftoken", "")
                r = admin.post(
                    "/award",
                    body=_form_encode(
                        {
                            "post_id": numeric_id,
                            "award_type": "insightful",
                            "_csrf_token": csrf,
                        }
                    ),
                    content_type="application/x-www-form-urlencoded",
                )
                check("POST give award", r)

                r = http_get(f"{base}/awards/{numeric_id}")
                check("GET awards for post", r)
                if r.status == 200:
                    data = json.loads(r.body)
                    check_true("has awards", len(data.get("awards", [])) > 0)
            else:
                check_true("found numeric post_id", False)
        else:
            check_true("created alice post for award", False)

        # ==============================================================
        # SEQUENCES
        # ==============================================================
        print("\n--- Sequences ---")

        # List (empty initially)
        r = http_get(f"{base}/sequences")
        check("GET /sequences", r)

        # Browse and create pages
        r = http_get(f"{base}/sequences/browse")
        check("GET /sequences/browse", r)
        r = admin.get("/sequences/create")
        check_true("GET /sequences/create", r.status in (200, 302), f"got {r.status}")

        # Create
        csrf = admin.cookie_jar.get("csrftoken", "")
        r = admin.post(
            "/sequences",
            body=_form_encode(
                {
                    "title": f"Test Series {sfx}",
                    "description": "A test sequence.",
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST create sequence", r)
        seq_id = None
        if r.status == 200:
            data = json.loads(r.body)
            seq_id = data.get("id")
            check_true("got sequence ID", seq_id is not None)

        if seq_id and test_pid:
            # Add post to sequence
            csrf = admin.cookie_jar.get("csrftoken", "")
            r = admin.post(
                f"/sequence/{seq_id}/add",
                body=_form_encode(
                    {
                        "pid": test_pid,
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST add post to sequence", r)

            # View sequence
            r = http_get(f"{base}/sequence/{seq_id}")
            check("GET sequence detail", r)
            if r.status == 200:
                data = json.loads(r.body)
                check_true("sequence has entries", data.get("entry_count", 0) > 0)

            # Duplicate add → 400
            csrf = admin.cookie_jar.get("csrftoken", "")
            r = admin.post(
                f"/sequence/{seq_id}/add",
                body=_form_encode(
                    {
                        "pid": test_pid,
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("duplicate add → 400", r, 400)

            # Remove from sequence
            csrf = admin.cookie_jar.get("csrftoken", "")
            r = admin.post(
                f"/sequence/{seq_id}/remove",
                body=_form_encode(
                    {
                        "pid": test_pid,
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST remove from sequence", r)

            # Non-author cannot add
            csrf = alice.cookie_jar.get("csrftoken", "")
            r = alice.post(
                f"/sequence/{seq_id}/add",
                body=_form_encode(
                    {
                        "pid": test_pid,
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("alice cannot add to admin's sequence → 403", r, 403)

        # ==============================================================
        # RELATED POSTS
        # ==============================================================
        print("\n--- Related Posts ---")
        if test_pid:
            r = http_get(f"{base}/post/{test_pid}/related")
            check("GET related posts", r)
            if r.status == 200:
                data = json.loads(r.body)
                check_true("related is list", isinstance(data.get("related"), list))

        # ==============================================================
        # AUTOMOD
        # ==============================================================
        print("\n--- Automod ---")

        # List rules (empty)
        r = admin.get(f"/f/{test_forum}/automod")
        check("GET automod rules", r)
        if r.status == 200:
            data = json.loads(r.body)
            check_true("empty rules list", len(data.get("rules", [])) == 0)

        # Create rule
        csrf = admin.cookie_jar.get("csrftoken", "")
        r = admin.post(
            f"/f/{test_forum}/automod",
            body=_form_encode(
                {
                    "trigger": "new_post",
                    "action": "flag",
                    "condition": '{"contains_words": ["spam"]}',
                    "_csrf_token": csrf,
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        check("POST create automod rule", r)
        rule_id = None
        if r.status == 200:
            data = json.loads(r.body)
            rule_id = data.get("id")

        if rule_id:
            # List rules (has 1)
            r = admin.get(f"/f/{test_forum}/automod")
            if r.status == 200:
                data = json.loads(r.body)
                check_true("1 rule exists", len(data.get("rules", [])) == 1)

            # Toggle rule off
            csrf = admin.cookie_jar.get("csrftoken", "")
            r = admin.post(
                f"/f/{test_forum}/automod/{rule_id}/toggle",
                body=_form_encode(
                    {
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST toggle automod", r)
            if r.status == 200:
                data = json.loads(r.body)
                check_true("rule disabled", data.get("is_active") is False)

            # Delete rule
            csrf = admin.cookie_jar.get("csrftoken", "")
            r = admin.post(
                f"/f/{test_forum}/automod/{rule_id}/delete",
                body=_form_encode(
                    {
                        "_csrf_token": csrf,
                    }
                ),
                content_type="application/x-www-form-urlencoded",
            )
            check("POST delete automod", r)

        # Non-admin cannot manage automod
        r = alice.get(f"/f/{test_forum}/automod")
        check("alice cannot view automod → 403", r, 403)

    # ==============================================================
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailed:")
        for e in ERRORS:
            print(f"  {e}")
    print(f"{'=' * 60}\n")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
