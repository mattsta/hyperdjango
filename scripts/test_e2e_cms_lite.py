"""
CMS Lite E2E tests — redirects and flat pages.

Tests flat page serving, redirect resolution, auth-gated pages, API endpoints.

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
    print("CMS Lite E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["cms_lite"]

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.cms_lite.app:app",
            "--drop",
            "--seed",
            "services.cms_lite.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.cms_lite.app:app",
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

        # ── Flat Pages ──
        print("\n--- Flat Pages ---")
        r = http_get(f"{base}/")
        ok("homepage 200", r.status == 200)
        body = r.json
        ok("homepage has title", "title" in body)
        ok("homepage title", "CMS Lite" in body.get("title", ""))

        r = http_get(f"{base}/about/")
        ok("about page 200", r.status == 200)
        ok("about has title", "About" in r.json.get("title", ""))

        r = http_get(f"{base}/help/")
        ok("help page 200", r.status == 200)

        r = http_get(f"{base}/privacy/")
        ok("privacy page 200", r.status == 200)

        # ── Auth-gated page ──
        print("\n--- Auth-gated Page ---")
        r = http_get(f"{base}/terms/")
        ok("terms unauthenticated → 401", r.status == 401)
        ok("terms error message", "Login required" in r.json.get("error", ""))

        # ── Redirects ──
        print("\n--- Redirects ---")
        # Exact match redirect (our http_get follows redirects, so check final)
        r = http_get(f"{base}/old-about")
        ok(
            "old-about redirect resolves",
            r.status in (200, 301, 302),
            f"got {r.status}: {r.body[:200]}",
        )

        r = http_get(f"{base}/old-terms")
        ok("old-terms redirect resolves", r.status in (200, 301, 302, 401))

        r = http_get(f"{base}/info")
        ok("info redirect resolves", r.status in (200, 301, 302))

        # ── API: List Pages ──
        print("\n--- API: Pages ---")
        r = http_get(f"{base}/api/pages")
        ok("api pages 200", r.status == 200)
        body = r.json
        ok("api pages has count", "count" in body)
        ok("api pages count=5", body.get("count") == 5)
        ok("api pages has pages list", isinstance(body.get("pages"), list))

        # ── API: List Redirects ──
        print("\n--- API: Redirects ---")
        r = http_get(f"{base}/api/redirects")
        ok("api redirects 200", r.status == 200)
        body = r.json
        ok("api redirects has count", "count" in body)
        ok("api redirects count=4", body.get("count") == 4)
        ok("api redirects has list", isinstance(body.get("redirects"), list))

        # Verify redirect entries
        redirects = body.get("redirects", [])
        old_paths = [r["old_path"] for r in redirects]
        ok("api: /old-about in redirects", "/old-about" in old_paths)
        ok("api: /old-terms in redirects", "/old-terms" in old_paths)

        # ── Swagger UI ──
        print("\n--- Swagger UI ---")
        r = http_get(f"{base}/docs/")
        ok("swagger 200", r.status == 200)

        # ── Admin ──
        print("\n--- Admin ---")
        r = http_get(f"{base}/admin/")
        ok("admin redirects to login", r.status in (200, 302))

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"CMS Lite: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("FAILURES:")
        for e in ERRORS:
            print(f"  {e}")
    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
