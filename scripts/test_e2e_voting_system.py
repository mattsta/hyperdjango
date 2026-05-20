"""
End-to-end tests for the HyperNews voting system (Phases 1-4).

Tests:
  Phase 1: Self-vote prevention, ban/mute enforcement, vote rate limiting
  Phase 2: Hot/controversial/rising sort tabs render
  Phase 3: Downvote requires karma (new user can't downvote)
  Phase 4: Vote events recorded, analytics endpoints (staff only)

Runs against a live HyperNews server via AppRunner.
"""

# hyper-test: e2e

import re
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


def register_user(base: str, username: str, password: str) -> Session:
    """Register a new user and return an authenticated Session."""
    s = Session(base)
    s.get("/register")
    s.post(
        "/register",
        body=f"username={username}&password={password}&email={username}@test.com",
        content_type="application/x-www-form-urlencoded",
    )
    return s


def login_user(base: str, username: str, password: str) -> Session:
    """Login as an existing user and return an authenticated Session."""
    s = Session(base)
    s.get("/login")
    s.post(
        "/login",
        body=f"username={username}&password={password}",
        content_type="application/x-www-form-urlencoded",
    )
    return s


def submit_post(session: Session, title: str, url: str = "") -> str:
    """Submit a post and return the internal post_id extracted from the redirect page."""
    session.get("/submit")
    r = session.post(
        "/submit",
        body=f"title={title}&url={url}&text=",
        content_type="application/x-www-form-urlencoded",
    )
    # Follow redirect to post detail, extract internal ID from vote buttons
    location = r.headers.get("location", "")
    if "/post/" in location:
        pid = location.split("/post/")[1].split("/")[0].split("?")[0]
        r2 = session.get(f"/post/{pid}")
        ids = re.findall(r'"post_id":\s*"(\d+)"', r2.body)
        if ids:
            return ids[0]
    return ""


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperNews Voting System E2E Tests (Phases 1-4)")
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
        "services.hypernews.app:app", host="127.0.0.1", port=TEST_PORTS["voting_system"]
    ) as runner:
        base = runner.url()
        ts = str(int(time.time()))

        # Setup: create two users + admin login
        user_a = register_user(base, f"voterA{ts}", "password123")
        user_b = register_user(base, f"voterB{ts}", "password123")
        admin = login_user(base, "admin", SEED_PASSWORD)

        # Admin creates a post that others can vote on
        post_id = submit_post(admin, f"Admin+Post+{ts}", f"https://example.com/{ts}")
        ok("Admin post created", bool(post_id), f"post_id={post_id}")

        # ── Phase 1: Self-Vote Prevention ─────────────────────────────
        print("\n--- Phase 1: Self-vote prevention ---")
        r = admin.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Self-vote blocked (400)",
            r.status == 400,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Self-vote error message",
            "own content" in r.body.lower(),
            f"body: {r.body[:200]}",
        )

        # ── Phase 1: Cross-user vote works ────────────────────────────
        print("\n--- Phase 1: Cross-user vote works ---")
        r = user_a.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Cross-user upvote succeeds (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Vote returns score HTML",
            "point" in r.body.lower(),
            f"body: {r.body[:100]}",
        )

        # ── Phase 1: Vote toggle (upvote again = remove) ─────────────
        print("\n--- Phase 1: Vote toggle ---")
        r = user_a.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Vote toggle succeeds (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )

        # ── Phase 1: Vote flip (up → down) ───────────────────────────
        print("\n--- Phase 1: Vote flip ---")
        # First upvote
        user_a.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        # Then flip to downvote — BUT new user can't downvote (Phase 3 tier check)
        r = user_a.post(
            "/vote",
            body=f"post_id={post_id}&direction=down",
            content_type="application/x-www-form-urlencoded",
        )
        # New user (0 karma) should be blocked from downvoting
        ok(
            "New user downvote blocked (403)",
            r.status == 403,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Downvote error mentions karma",
            "karma" in r.body.lower() or "tier" in r.body.lower(),
            f"body: {r.body[:200]}",
        )

        # ── Phase 1: Unauthenticated vote blocked ────────────────────
        print("\n--- Phase 1: Unauthenticated vote ---")
        anon = Session(base)
        r = anon.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        # Without session: CSRF middleware rejects (403) or auth redirects (302/303)
        ok(
            "Unauthenticated vote blocked",
            r.status in (302, 303, 403),
            f"got {r.status}",
        )

        # ── Phase 2: Sort tabs render ─────────────────────────────────
        print("\n--- Phase 2: Sort tabs ---")
        for tab in ["hot", "new", "top", "controversial", "rising", "ask"]:
            r = user_a.get(f"/?tab={tab}")
            ok(
                f"Tab '{tab}' loads (200)",
                r.status == 200,
                f"got {r.status}: {r.body[:100]}",
            )

        # ── Phase 2: Hot tab is default ───────────────────────────────
        print("\n--- Phase 2: Hot is default ---")
        r = user_a.get("/")
        ok(
            "Default tab is hot",
            "tab == 'hot'" in r.body or "active" in r.body,
            "checking for active tab",
        )
        # The "Hot" tab link should have the 'active' class
        ok(
            "Hot tab marked active",
            'class="tab active"' in r.body
            and "hot" in r.body.split('class="tab active"')[0][-50:].lower(),
            f"body snippet around active: ...{r.body[r.body.find('active') : r.body.find('active') + 50] if 'active' in r.body else 'not found'}",
        )

        # ── Phase 3: Downvote requires karma ──────────────────────────
        print("\n--- Phase 3: Trust tier enforcement ---")
        # user_b (new, 0 karma) tries to downvote
        r = user_b.post(
            "/vote",
            body=f"post_id={post_id}&direction=down",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "0-karma user cannot downvote (403)",
            r.status == 403,
            f"got {r.status}: {r.body[:200]}",
        )

        # ── Phase 4: Analytics endpoints (staff only) ─────────────────
        print("\n--- Phase 4: Analytics endpoints ---")
        # Non-staff user cannot access
        r = user_a.get("/analytics/rings")
        ok("Non-staff → 403 on /analytics/rings", r.status == 403, f"got {r.status}")

        r = user_a.get("/analytics/domains")
        ok("Non-staff → 403 on /analytics/domains", r.status == 403, f"got {r.status}")

        # Admin can access
        r = admin.get("/analytics/rings")
        ok(
            "Admin can access /analytics/rings (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok("Rings returns JSON array", r.body.startswith("["), f"body: {r.body[:100]}")

        r = admin.get("/analytics/domains")
        ok(
            "Admin can access /analytics/domains (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Domains returns JSON array",
            r.body.startswith("["),
            f"body: {r.body[:100]}",
        )

        # Admin affinity for a user
        r = admin.get(f"/analytics/affinity/voterA{ts}")
        ok(
            "Admin can access /analytics/affinity (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Affinity returns JSON array",
            r.body.startswith("["),
            f"body: {r.body[:100]}",
        )

        # Non-existent user
        r = admin.get("/analytics/affinity/nobody_exists_999")
        ok("Affinity 404 for missing user", r.status == 404, f"got {r.status}")

        # ── Phase 4: Vote events are recorded ─────────────────────────
        print("\n--- Phase 4: Vote event recording ---")
        # user_b votes on admin's post (upvote — allowed for new users)
        r = user_b.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        ok("user_b upvote succeeds", r.status == 200, f"got {r.status}: {r.body[:200]}")

        # Check that affinity now shows admin as a target for user_b
        r = admin.get(f"/analytics/affinity/voterB{ts}")
        ok("Affinity shows vote target", r.status == 200, f"got {r.status}")
        # The response should contain the admin user as a target
        import json

        affinity_data = json.loads(r.body) if r.body.startswith("[") else []
        ok(
            "Affinity has entries after voting",
            len(affinity_data) >= 1,
            f"got {len(affinity_data)} entries: {r.body[:200]}",
        )

        # ── Phase 5: Agreement votes ──────────────────────────────────
        print("\n--- Phase 5: Agreement votes ---")
        r = user_a.post(
            "/agree",
            body=f"post_id={post_id}&direction=agree",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Agree vote succeeds (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Agree returns JSON ok",
            '"ok": true' in r.body.lower() or '"ok":true' in r.body.lower(),
            f"body: {r.body[:100]}",
        )

        # Disagree
        r = user_b.post(
            "/agree",
            body=f"post_id={post_id}&direction=disagree",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Disagree vote succeeds (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )

        # ── Phase 5: Content tags ─────────────────────────────────────
        print("\n--- Phase 5: Content tags ---")
        # New user (0 karma) can't tag — requires 'established' tier
        r = user_a.post(
            "/tag",
            body=f"post_id={post_id}&tag=insightful",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "0-karma user cannot tag (403)",
            r.status == 403,
            f"got {r.status}: {r.body[:200]}",
        )

        # Admin (staff) can tag
        r = admin.post(
            "/tag",
            body=f"post_id={post_id}&tag=insightful",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Admin can tag content (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )

        # Invalid tag rejected
        r = admin.post(
            "/tag",
            body=f"post_id={post_id}&tag=invalid_garbage",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Invalid tag rejected (400)",
            r.status == 400,
            f"got {r.status}: {r.body[:200]}",
        )

        # ── Phase 5: Mod notes ────────────────────────────────────────
        print("\n--- Phase 5: Mod notes ---")
        # Non-staff can't add mod notes
        r = user_a.post(
            "/mod/note",
            body=f"post_id={post_id}&note=test+note&visibility=mod_only",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Non-staff cannot add mod note (403)", r.status == 403, f"got {r.status}")

        # Admin can add mod note
        r = admin.post(
            "/mod/note",
            body=f"post_id={post_id}&note=Admin+review+note&visibility=author_visible",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Admin can add mod note (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok("Mod note returns id", '"id"' in r.body, f"body: {r.body[:100]}")

        # Empty note rejected
        r = admin.post(
            "/mod/note",
            body=f"post_id={post_id}&note=&visibility=mod_only",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Empty mod note rejected (400)", r.status == 400, f"got {r.status}")

        # ── Phase 5: Mod action history ───────────────────────────────
        print("\n--- Phase 5: Mod action history ---")
        r = admin.get(f"/mod/actions/voterA{ts}")
        ok("Mod actions endpoint (200)", r.status == 200, f"got {r.status}")
        ok("Returns JSON array", r.body.startswith("["), f"body: {r.body[:100]}")

        # Non-staff blocked
        r = user_a.get(f"/mod/actions/voterA{ts}")
        ok(
            "Non-staff blocked from mod history (403)",
            r.status == 403,
            f"got {r.status}",
        )

        # ── Phase 6: Graph analytics ──────────────────────────────────
        print("\n--- Phase 6: Graph analytics ---")

        # Non-staff blocked from all analytics
        r = user_a.get("/analytics/centrality")
        ok(
            "Non-staff blocked from centrality (403)",
            r.status == 403,
            f"got {r.status}",
        )
        r = user_a.get("/analytics/communities")
        ok(
            "Non-staff blocked from communities (403)",
            r.status == 403,
            f"got {r.status}",
        )

        # Trigger graph analytics refresh (admin only)
        r = admin.post(
            "/analytics/refresh-graph",
            body="",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Admin can trigger graph refresh (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Graph refresh returns ok",
            '"ok": true' in r.body.lower() or '"ok":true' in r.body.lower(),
            f"body: {r.body[:100]}",
        )

        # Centrality endpoint returns data
        r = admin.get("/analytics/centrality")
        ok(
            "Centrality endpoint (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Centrality returns JSON array",
            r.body.startswith("["),
            f"body: {r.body[:100]}",
        )

        # Communities endpoint returns data
        r = admin.get("/analytics/communities")
        ok(
            "Communities endpoint (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok(
            "Communities returns JSON array",
            r.body.startswith("["),
            f"body: {r.body[:100]}",
        )

        # Non-staff cannot trigger refresh
        r = user_a.post(
            "/analytics/refresh-graph",
            body="",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Non-staff cannot refresh graph (403)", r.status == 403, f"got {r.status}")

        # ── Edge Cases: Vote toggle after downvote denied ─────────────
        print("\n--- Edge Case: Vote toggle after downvote denied ---")
        # user_a already has upvote on admin's post. Toggle it OFF.
        r = user_a.post(
            "/vote",
            body=f"post_id={post_id}&direction=up",
            content_type="application/x-www-form-urlencoded",
        )
        if r.status != 200:
            print("  STDERR (last 50):")
            for line in runner._stderr_lines[-50:]:
                print(f"    {line.rstrip()}")
        ok(
            "Toggle off own upvote succeeds (200)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )

        # ── Edge Case: Rate limiting on submit ────────────────────────
        print("\n--- Edge Case: Rate limiting ---")
        # Submit 4 posts rapidly (limit is 3/min)
        for i in range(3):
            admin.post(
                "/submit",
                body=f"title=Rate+Test+{ts}+{i}&text=body",
                content_type="application/x-www-form-urlencoded",
            )
        r = admin.post(
            "/submit",
            body=f"title=Rate+Test+{ts}+overflow&text=body",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "4th submit in 60s → 429",
            r.status == 429,
            f"got {r.status}: {r.body[:200]}",
        )

        # ── Edge Case: Duplicate spam report ──────────────────────────
        print("\n--- Edge Case: Duplicate report ---")
        user_b.post(
            "/report",
            body=f"post_id={post_id}&reason=test",
            content_type="application/x-www-form-urlencoded",
        )
        r = user_b.post(
            "/report",
            body=f"post_id={post_id}&reason=test+again",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Duplicate report returns ok (dedup)",
            r.status == 200,
            f"got {r.status}: {r.body[:200]}",
        )
        ok("Dedup message", "already" in r.body.lower(), f"body: {r.body[:200]}")

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
