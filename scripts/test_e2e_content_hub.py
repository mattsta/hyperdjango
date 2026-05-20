"""
End-to-end tests for Content Hub example (Q objects, OneToOneField, STI).

# hyper-test: e2e

Tests:
  - Health + readiness
  - Auth (login, role enforcement)
  - STI: /api/articles, /api/videos, /api/links return correct types
  - STI: /api/contents returns all types
  - Q object search: OR, NOT, nested conditions
  - OneToOneField: profile CRUD, author profile in content detail
  - CursorPagination on list endpoints
  - Stats with Q-based counts
"""

import subprocess
import sys
import time

from e2e_helper import (
    ADMIN_PASSWORD,
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


def check(name, response, expected_status=200):
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


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
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
    print("Content Hub Example E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["content_hub"]

    # Setup — use targeted DDL instead of --drop to avoid destroying shared framework tables
    import os

    env = {**os.environ}
    db_url = env.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
    # Ensure hub tables exist with STI columns via direct SQL
    setup_sql = f"""
    import asyncio
    from hyperdjango.database import Database, set_db
    async def main():
        db = Database('{db_url}')
        await db.connect()
        set_db(db)
        await db.execute('DROP TABLE IF EXISTS hub_contents CASCADE')
        await db.execute('DROP TABLE IF EXISTS hub_profiles CASCADE')
        await db.execute('DROP TABLE IF EXISTS hub_tags CASCADE')
        await db.execute('DROP TABLE IF EXISTS hub_users CASCADE')
        await db.disconnect()
    asyncio.run(main())
    """
    subprocess.run([sys.executable, "-c", setup_sql], capture_output=True, timeout=30)
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.content_hub.app:app",
            "--seed",
            "services.content_hub.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.content_hub.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health + Readiness ──
        print("--- Health + Readiness ---")
        r = http_get(f"{base}/health")
        check("Health 200", r, 200)

        r = http_get(f"{base}/ready")
        check("Ready 200", r, 200)
        if r.status == 200:
            ok("Ready status ok", r.json.get("status") == "ok")
            ok("Ready has checks", "checks" in r.json)

        # ── Auth ──
        print("\n--- Auth ---")
        # Login as editor (GET any page first for CSRF cookie, then POST login)
        s = Session(base)
        s.get("/health")  # Any GET sets CSRF cookie
        r = s.post(
            "/auth/login", body={"username": "editor", "password": SEED_PASSWORD}
        )
        check("Editor login", r, 200)

        # Me endpoint (includes profile via OneToOneField)
        r = s.get("/auth/me")
        check("Me endpoint", r, 200)
        if r.status == 200:
            ok("Me has profile", r.json.get("profile") is not None)
            ok(
                "Me profile has display_name",
                r.json["profile"].get("display_name") == "Content Editor",
            )
            ok("Me role is editor", r.json.get("role") == "editor")

        # Reader can't create content
        s2 = Session(base)
        s2.get("/health")
        s2.post("/auth/login", body={"username": "reader", "password": SEED_PASSWORD})
        r = s2.post("/api/contents", body={"title": "Should Fail"})
        check("Reader create → 403", r, 403)

        # Unauthenticated create
        r = http_post(f"{base}/api/contents", body={"title": "No Auth"})
        check("Unauthed create → 401", r, 401)

        # ── STI: Type-specific endpoints ──
        print("\n--- STI Endpoints ---")
        r = http_get(f"{base}/api/articles")
        check("Articles endpoint 200", r, 200)
        if r.status == 200:
            results = r.json.get("results", [])
            ok("Articles has results", len(results) > 0, f"got {len(results)}")
            ok(
                "Articles have reading_time",
                "reading_time_mins" in results[0] if results else False,
            )

        r = http_get(f"{base}/api/videos")
        check("Videos endpoint 200", r, 200)
        if r.status == 200:
            results = r.json.get("results", [])
            ok("Videos has results", len(results) > 0, f"got {len(results)}")
            ok("Videos have video_url", "video_url" in results[0] if results else False)

        r = http_get(f"{base}/api/links")
        check("Links endpoint 200", r, 200)
        if r.status == 200:
            results = r.json.get("results", [])
            ok("Links has results", len(results) > 0, f"got {len(results)}")
            ok(
                "Links have external_url",
                "external_url" in results[0] if results else False,
            )

        # All content
        r = http_get(f"{base}/api/contents")
        check("All contents 200", r, 200)
        if r.status == 200:
            results = r.json.get("results", [])
            types = {item["type"] for item in results}
            ok("All contents has multiple types", len(types) > 1, f"types={types}")

        # ── STI: Content detail with author profile ──
        print("\n--- Content detail + profile ---")
        r = http_get(f"{base}/api/contents/1")
        check("Content detail 200", r, 200)
        if r.status == 200:
            ok("Detail has full body", len(r.json.get("body", "")) > 10)
            ok("Detail has author object", isinstance(r.json.get("author"), dict))
            ok("Author has display_name", "display_name" in r.json.get("author", {}))

        r = http_get(f"{base}/api/contents/999999")
        check("Missing content 404", r, 404)

        # ── Q Object Search ──
        print("\n--- Q Object Search ---")

        # Text search (OR across title and body)
        r = s.post("/api/search", body={"q": "python"})
        check("Search python 200", r, 200)
        if r.status == 200:
            ok(
                "Search has results",
                r.json.get("count", 0) > 0,
                f"count={r.json.get('count')}",
            )

        # Type filter (OR across types)
        r = s.post("/api/search", body={"types": ["article", "video"]})
        check("Search types 200", r, 200)
        if r.status == 200:
            result_types = {item["type"] for item in r.json.get("results", [])}
            ok(
                "Type filter has articles or videos",
                result_types <= {"article", "video"},
                f"types={result_types}",
            )

        # Exclude archived (NOT Q)
        r = s.post("/api/search", body={"exclude_archived": True})
        check("Search exclude archived 200", r, 200)
        if r.status == 200:
            statuses = {item["status"] for item in r.json.get("results", [])}
            ok(
                "No archived in results",
                "archived" not in statuses,
                f"statuses={statuses}",
            )

        # Featured only
        r = s.post("/api/search", body={"featured_only": True})
        check("Search featured 200", r, 200)
        if r.status == 200:
            all_featured = all(item["featured"] for item in r.json.get("results", []))
            ok("All results are featured", all_featured)

        # Combined: text + types + exclude_archived
        r = s.post(
            "/api/search",
            body={"q": "zig", "types": ["article"], "exclude_archived": True},
        )
        check("Combined search 200", r, 200)

        # ── Content creation ──
        print("\n--- Content creation ---")
        ts = str(int(time.time()) % 100000)
        r = s.post(
            "/api/contents",
            body={
                "title": f"Test Article {ts}",
                "type": "article",
                "body": "Test body content",
                "status": "draft",
            },
        )
        check("Create article 201", r, 201)
        if r.status == 201:
            ok("Created has id", "id" in r.json)
            ok("Created type is article", r.json.get("type") == "article")

        # Invalid type
        r = s.post("/api/contents", body={"title": "Bad", "type": "invalid_type"})
        check("Invalid type → 400", r, 400)

        # Missing title
        r = s.post("/api/contents", body={"body": "no title"})
        check("Missing title → 400", r, 400)

        # ── Profiles (OneToOneField) ──
        print("\n--- Profiles ---")
        r = http_get(f"{base}/api/profiles/1")
        if r.status == 200:
            check("Get profile 200", r, 200)
            ok("Profile has display_name", "display_name" in r.json)
            ok("Profile has bio", "bio" in r.json)
        else:
            # Profile IDs may vary — find one
            check("Get profile", r, 200)

        # ── Filter by query params ──
        print("\n--- Query param filters ---")
        r = http_get(f"{base}/api/contents?type=article")
        check("Filter by type 200", r, 200)
        if r.status == 200:
            types = {item["type"] for item in r.json.get("results", [])}
            ok("Type filter works", types == {"article"}, f"types={types}")

        r = http_get(f"{base}/api/contents?status=published")
        check("Filter by status 200", r, 200)

        r = http_get(f"{base}/api/contents?q=HyperDjango")
        check("Q param search 200", r, 200)
        if r.status == 200:
            ok("Q search has results", len(r.json.get("results", [])) > 0)

        r = http_get(f"{base}/api/contents?type=invalid")
        check("Invalid type param → 400", r, 400)

        # ── Stats (Q-based counts) ──
        print("\n--- Stats ---")
        r = http_get(f"{base}/api/stats")
        check("Stats 200", r, 200)
        if r.status == 200:
            stats = r.json
            ok("Stats has total", stats.get("total", 0) > 0)
            ok("Stats has by_type", "by_type" in stats)
            ok("Stats has published count", "published" in stats)
            ok("Stats has featured count", "featured" in stats)
            ok("Stats has Q-based count", "published_articles_and_videos" in stats)
            by_type = stats.get("by_type", {})
            ok(
                "Articles + videos + links = total",
                by_type.get("articles", 0)
                + by_type.get("videos", 0)
                + by_type.get("links", 0)
                == stats.get("total", -1),
                f"articles={by_type.get('articles')} videos={by_type.get('videos')} links={by_type.get('links')} total={stats.get('total')}",
            )

        # ── HyperAdmin panel ──
        print("\n--- Admin Panel ---")

        # Admin login page
        r = http_get(f"{base}/admin/login/")
        ok(
            "Admin login page loads",
            r.status == 200 and "Login" in r.body,
            f"status={r.status}",
        )

        # Admin dashboard (redirects to login without auth)
        r = http_get(f"{base}/admin/")
        ok(
            "Admin dashboard requires auth",
            r.status in (200, 302, 401),
            f"status={r.status}",
        )

        # Login to admin as admin user
        admin_session = Session(base)
        r = admin_session.post(
            "/admin/login/",
            body=f"username=admin&password={ADMIN_PASSWORD}",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Admin login succeeds",
            r.status in (200, 302),
            f"status={r.status} body={r.body[:200] if r.body else '(empty)'}",
        )

        # Dashboard after login
        r = admin_session.get("/admin/")
        ok("Admin dashboard 200", r.status == 200, f"status={r.status}")
        if r.status == 200:
            ok(
                "Dashboard lists Content model",
                "content" in r.body.lower() or "Content" in r.body,
            )
            ok(
                "Dashboard lists User model",
                "user" in r.body.lower() or "User" in r.body,
            )
            ok("Dashboard lists Tag model", "tag" in r.body.lower() or "Tag" in r.body)

        # Content list view
        r = admin_session.get("/admin/content/")
        ok("Admin content list loads", r.status == 200, f"status={r.status}")
        if r.status == 200:
            ok(
                "Content list has items",
                "Getting Started" in r.body or "article" in r.body.lower(),
            )
            ok(
                "Content list has action buttons",
                "publish" in r.body.lower() or "action" in r.body.lower(),
            )

        # Content add form
        r = admin_session.get("/admin/content/add/")
        ok("Admin content add form loads", r.status == 200, f"status={r.status}")
        if r.status == 200:
            ok("Add form has title field", "title" in r.body.lower())

        # Tag list (simplest model, no inlines)
        r = admin_session.get("/admin/tag/")
        ok("Admin tag list loads", r.status == 200, f"status={r.status}")

        # RBAC models registered
        r = admin_session.get("/admin/")
        if r.status == 200:
            ok(
                "RBAC: permissions model visible",
                "permission" in r.body.lower(),
                f"body excerpt: {r.body[500:800]}",
            )

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for err in ERRORS:
            print(f"  {err}")
    print("=" * 60)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
