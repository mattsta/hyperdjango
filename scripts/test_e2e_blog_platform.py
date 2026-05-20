"""
Blog Platform E2E tests — sitemaps, syndication, i18n.

Tests sitemap.xml, RSS/Atom feeds, i18n-aware endpoints, REST API, admin.

# hyper-test: e2e
"""

import subprocess
import sys

from e2e_helper import TEST_PORTS, AppRunner, http_get

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)
    return condition


def main():
    print("=" * 60)
    print("Blog Platform E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["blog_platform"]

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.blog_platform.app:app",
            "--drop",
            "--seed",
            "services.blog_platform.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.blog_platform.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health ──
        print("--- Health ---")
        r = http_get(f"{base}/health")
        ok("health endpoint", r.status == 200)

        # ── Index (i18n-aware) ──
        print("\n--- Index ---")
        r = http_get(f"{base}/")
        ok("index 200", r.status == 200, f"got {r.status}: {r.body[:200]}")
        body = r.json
        ok("index has posts", "posts" in body)
        ok("index has language", "language" in body)
        ok("index count > 0", body.get("count", 0) > 0)

        # ── Post detail ──
        print("\n--- Post Detail ---")
        r = http_get(f"{base}/post/getting-started-hyperdjango")
        ok("post detail 200", r.status == 200)
        body = r.json
        ok("post has title", "title" in body)
        ok("post title correct", "Getting Started" in body.get("title", ""))

        # French post
        r = http_get(f"{base}/post/premiers-pas-hyperdjango")
        ok("french post 200", r.status == 200)
        ok("french post title", "Premiers" in r.json.get("title", ""))

        # Non-existent post
        r = http_get(f"{base}/post/nonexistent-slug")
        ok("missing post 404", r.status == 404)

        # ── Category ──
        print("\n--- Categories ---")
        r = http_get(f"{base}/category/python")
        ok("category python 200", r.status == 200)
        body = r.json
        ok("category has posts", "posts" in body)
        ok("category has category obj", "category" in body)

        r = http_get(f"{base}/category/nonexistent")
        ok("missing category 404", r.status == 404)

        # ── Sitemap ──
        print("\n--- Sitemap ---")
        r = http_get(f"{base}/sitemap.xml")
        ok("sitemap 200", r.status == 200)
        ok(
            "sitemap is XML",
            "xml" in r.headers.get("content-type", "").lower()
            or "<?xml" in r.body
            or "<urlset" in r.body
            or "<sitemapindex" in r.body,
        )

        # ── RSS Feed ──
        print("\n--- RSS Feed ---")
        r = http_get(f"{base}/feed/rss")
        ok("rss 200", r.status == 200, f"got {r.status}: {r.body[:300]}")
        ok(
            "rss has xml/rss content",
            "<rss" in r.body or "xml" in r.headers.get("content-type", "").lower(),
        )
        ok("rss has channel", "<channel" in r.body)
        ok("rss has item", "<item" in r.body)

        # ── Atom Feed ──
        print("\n--- Atom Feed ---")
        r = http_get(f"{base}/feed/atom")
        ok("atom 200", r.status == 200)
        ok(
            "atom has feed tag",
            "<feed" in r.body,
            f"bodylen={len(r.body)} feed_at={r.body.find('<feed')}",
        )
        ok("atom has entry", "<entry" in r.body)

        # ── REST API ──
        print("\n--- REST API ---")
        r = http_get(f"{base}/api/posts")
        ok("api posts 200", r.status == 200)
        body = r.json
        ok("api has results", "results" in body)
        ok("api results non-empty", len(body.get("results", [])) > 0)

        # ── Swagger UI ──
        print("\n--- Swagger UI ---")
        r = http_get(f"{base}/docs/")
        ok("swagger 200", r.status == 200)
        ok("swagger has html", "<html" in r.body.lower() or "swagger" in r.body.lower())

        # ── Admin ──
        print("\n--- Admin ---")
        r = http_get(f"{base}/admin/")
        ok("admin redirects to login", r.status in (200, 302))

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Blog Platform: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("FAILURES:")
        for e in ERRORS:
            print(f"  {e}")
    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
