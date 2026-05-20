"""
End-to-end tests for Bookstore API service.

# hyper-test: e2e

Tests the full REST framework showcase:
- ModelViewSet CRUD (list, create, retrieve, update, partial_update, destroy)
- Custom @action endpoints (publish, feature, featured, stats)
- Pagination (PageNumber for books, Cursor for reviews)
- Filtering (FieldFilter, SearchFilter, OrderingFilter)
- Authentication (session login, unauthenticated access, API key)
- ETag / conditional caching (304 Not Modified)
- Nested router (authors/{id}/books)
- OpenAPI / Swagger UI
- Validation and error handling
"""

import json
import subprocess
import sys
import time

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    E2EResponse,
    Session,
    _http_request,
    http_get,
    service_app,
    service_seed,
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


def check_val(name: str, actual, expected) -> bool:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected!r}, got {actual!r}"
    print(msg)
    ERRORS.append(msg)
    return False


def check_true(name: str, condition: bool) -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: condition was False"
    print(msg)
    ERRORS.append(msg)
    return False


def http_put(url, body=None, headers=None):
    return _http_request("PUT", url, body=body, headers=headers)


def http_patch(url, body=None, headers=None):
    return _http_request("PATCH", url, body=body, headers=headers)


def http_delete(url, headers=None):
    return _http_request("DELETE", url, headers=headers)


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Bookstore API E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["bookstore_api"]

    # Run setup + seed before starting the server
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            service_app("bookstore_api"),
            "--drop",
            "--seed",
            service_seed("bookstore_api"),
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        service_app("bookstore_api"),
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health ──────────────────────────────────────────────
        print("--- Health ---")
        r = http_get(f"{base}/health")
        check("health endpoint", r, 200)
        if r.status == 200:
            data = r.json
            check_true("health status ok", data.get("status") == "ok")

        # ── OpenAPI / Swagger ───────────────────────────────────
        print("\n--- OpenAPI & Swagger ---")
        r = http_get(f"{base}/openapi.json")
        check("openapi.json endpoint", r, 200)
        if r.status == 200:
            spec = r.json
            check_val("openapi version", spec.get("openapi"), "3.1.0")
            check_true("has paths", len(spec.get("paths", {})) > 0)
            check_true("has info title", "title" in spec.get("info", {}))

        r = http_get(f"{base}/docs")
        check("swagger UI page", r, 200)
        if r.status == 200:
            check_true("swagger UI has HTML", "swagger-ui" in r.body.lower())

        # ── API Root ────────────────────────────────────────────
        print("\n--- API Root ---")
        r = http_get(f"{base}/api/v1/")
        check("api root endpoint", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "api root lists books", "book" in data or "books" in str(data).lower()
            )

        # ── Books: List ─────────────────────────────────────────
        print("\n--- Books: List ---")
        r = http_get(f"{base}/api/v1/books/")
        check("list books", r, 200)
        if r.status == 200:
            data = r.json
            # Should be paginated
            check_true("paginated: has results", "results" in data)
            check_true("paginated: has count", "count" in data)
            check_true("has books in results", len(data.get("results", [])) > 0)
            check_true("count > 10 (multiple pages)", data.get("count", 0) > 10)

        # ── Books: Pagination ───────────────────────────────────
        print("\n--- Books: Pagination ---")
        r = http_get(f"{base}/api/v1/books/?page=2")
        check("page 2", r, 200)
        if r.status == 200:
            data = r.json
            check_true("page 2 has results", len(data.get("results", [])) > 0)
            check_true("page 2 has previous link", data.get("previous") is not None)

        r = http_get(f"{base}/api/v1/books/?page=1&page_size=5")
        check("custom page size", r, 200)
        if r.status == 200:
            data = r.json
            check_true("page_size=5 returns <=5", len(data.get("results", [])) <= 5)

        # ── Books: Filtering ────────────────────────────────────
        print("\n--- Books: Filtering ---")
        r = http_get(f"{base}/api/v1/books/?published=true")
        check("filter published=true", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if results:
                check_true("all published", all(b.get("published") for b in results))

        r = http_get(f"{base}/api/v1/books/?featured=true")
        check("filter featured=true", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if results:
                check_true("all featured", all(b.get("featured") for b in results))

        r = http_get(f"{base}/api/v1/books/?published=false")
        check("filter unpublished", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if results:
                check_true(
                    "none published", not any(b.get("published") for b in results)
                )

        # ── Books: Search ───────────────────────────────────────
        print("\n--- Books: Search ---")
        r = http_get(f"{base}/api/v1/books/?search=python")
        check("search 'python'", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            check_true("search returned results", len(results) > 0)
            check_true(
                "search relevant",
                any("python" in b.get("title", "").lower() for b in results),
            )

        r = http_get(f"{base}/api/v1/books/?search=kubernetes")
        check("search 'kubernetes'", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "kubernetes search found results", len(data.get("results", [])) > 0
            )

        # ── Books: Ordering ─────────────────────────────────────
        print("\n--- Books: Ordering ---")
        r = http_get(f"{base}/api/v1/books/?ordering=title")
        check("ordering by title asc", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if len(results) > 1:
                titles = [b["title"] for b in results]
                check_true("title ascending order", titles == sorted(titles))

        r = http_get(f"{base}/api/v1/books/?ordering=-pages")
        check("ordering by pages desc", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if len(results) > 1:
                pages = [b["pages"] for b in results]
                check_true(
                    "pages descending order", pages == sorted(pages, reverse=True)
                )

        # ── Books: Retrieve ─────────────────────────────────────
        print("\n--- Books: Retrieve ---")
        r = http_get(f"{base}/api/v1/books/1")
        check("retrieve book 1", r, 200)
        if r.status == 200:
            data = r.json
            check_true("has title", "title" in data)
            check_true("has author_name (computed)", "author_name" in data)
            check_true("has category_name (computed)", "category_name" in data)
            check_true("has review_count (computed)", "review_count" in data)
            check_true("has avg_rating (computed)", "avg_rating" in data)
            check_true("author_name not empty", len(data.get("author_name", "")) > 0)

        r = http_get(f"{base}/api/v1/books/99999")
        check("retrieve nonexistent book → 404", r, 404)

        # ── Books: ETag Caching ─────────────────────────────────
        print("\n--- Books: ETag Caching ---")
        r = http_get(f"{base}/api/v1/books/1")
        etag = r.headers.get("etag", "")
        check_true("response has ETag header", len(etag) > 0)
        check_true("has Cache-Control header", "cache-control" in r.headers)

        if etag:
            r2 = http_get(f"{base}/api/v1/books/1", headers={"If-None-Match": etag})
            check("conditional GET returns 304", r2, 304)

        # Unique suffix for this test run to avoid conflicts on re-runs
        ts = str(int(time.time()) % 100000)

        # ── Auth: Login ─────────────────────────────────────────
        print("\n--- Authentication ---")
        s = Session(base)

        # Unauthenticated create should fail
        r = s.post(
            "/api/v1/books/",
            body={
                "title": "Test Book",
                "isbn": f"978-0-TEST-{ts}",
                "author_id": 1,
                "category_id": 1,
            },
        )
        check("unauthenticated create → 403", r, 403)

        # Login
        r = s.post("/auth/login", body={"username": "admin", "password": SEED_PASSWORD})
        check("admin login", r, 200)

        # ── Books: Create (authenticated) ───────────────────────
        print("\n--- Books: CRUD (authenticated) ---")
        test_isbn = f"978-0-E2E-{ts}"
        r = s.post(
            "/api/v1/books/",
            body={
                "title": "E2E Test Book",
                "isbn": test_isbn,
                "description": "Created by E2E test suite",
                "price": "29.99",
                "pages": 200,
                "author_id": 1,
                "category_id": 1,
            },
        )
        check("create book", r, 201)
        created_id = None
        if r.status == 201:
            created_id = r.json.get("id")
            check_true("created book has id", created_id is not None)

        # Update
        if created_id:
            r = s.put(
                f"/api/v1/books/{created_id}",
                body={
                    "title": "E2E Test Book (Updated)",
                    "isbn": test_isbn,
                    "description": "Updated by E2E test",
                    "price": "39.99",
                    "pages": 250,
                    "author_id": 1,
                    "category_id": 1,
                },
            )
            check("update book (PUT)", r, 200)
            if r.status == 200:
                check_val(
                    "updated title", r.json.get("title"), "E2E Test Book (Updated)"
                )

        # Partial update
        if created_id:
            r = s.patch(f"/api/v1/books/{created_id}", body={"pages": 300})
            check("partial update (PATCH)", r, 200)
            if r.status == 200:
                check_val("patched pages", r.json.get("pages"), 300)

        # ── Custom Actions ──────────────────────────────────────
        print("\n--- Custom Actions ---")
        if created_id:
            r = s.post(f"/api/v1/books/{created_id}/publish")
            check("publish action", r, 200)
            if r.status == 200:
                check_true("book now published", r.json.get("published") is True)

            # Publish again should fail
            r = s.post(f"/api/v1/books/{created_id}/publish")
            check("publish already-published → 400", r, 400)

            r = s.post(f"/api/v1/books/{created_id}/feature")
            check("feature action (toggle on)", r, 200)
            if r.status == 200:
                check_true("book now featured", r.json.get("featured") is True)

            r = s.post(f"/api/v1/books/{created_id}/feature")
            check("feature action (toggle off)", r, 200)
            if r.status == 200:
                check_true("book unfeatured", r.json.get("featured") is False)

        # Featured list
        r = http_get(f"{base}/api/v1/books/featured")
        check("list featured books", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", []) if isinstance(data, dict) else data
            check_true("featured has results", len(results) > 0)
            if results:
                check_true(
                    "featured books are featured",
                    all(b.get("featured") for b in results),
                )

        # Stats
        r = http_get(f"{base}/api/v1/books/stats")
        check("books stats", r, 200)
        if r.status == 200:
            data = r.json
            check_true("stats has total_books", "total_books" in data)
            check_true("stats has published", "published" in data)
            check_true("stats has avg_pages", "avg_pages" in data)

        # ── StampedeProtection cached-stats ─────────────────────
        print("\n--- Cached Stats (StampedeProtection) ---")
        r = http_get(f"{base}/api/v1/books/cached-stats")
        check("cached-stats first call", r, 200)
        if r.status == 200:
            data = r.json
            check_true("cached-stats has total_books", "total_books" in data)
            check_true("first call is miss", data.get("_cache") == "miss")
            check_true("has compute_ms", "_compute_ms" in data)

        r = http_get(f"{base}/api/v1/books/cached-stats")
        check("cached-stats second call", r, 200)
        if r.status == 200:
            data = r.json
            check_true("second call is hit", data.get("_cache") == "hit")
            check_true("hit has total_books", "total_books" in data)

        # ── TwoTierCache two-tier-stats ─────────────────────────
        print("\n--- Two-Tier Stats ---")
        r = http_get(f"{base}/api/v1/books/two-tier-stats")
        check("two-tier first call", r, 200)
        if r.status == 200:
            data = r.json
            check_true("two-tier has total_books", "total_books" in data)
            check_true("two-tier first is miss", data.get("_cache") == "miss")
            check_true("two-tier has tier_stats", "_tier_stats" in data)

        r = http_get(f"{base}/api/v1/books/two-tier-stats")
        check("two-tier second call", r, 200)
        if r.status == 200:
            data = r.json
            check_true("two-tier second is hit", data.get("_cache") == "hit")
            tier = data.get("_tier_stats", {})
            check_true("tier stats has l1_hits", "l1_hits" in tier)
            check_true("tier stats l1_hits > 0", tier.get("l1_hits", 0) > 0)

        # ── DataLoader enriched endpoint ────────────────────────
        print("\n--- DataLoader Enriched ---")
        r = http_get(f"{base}/api/v1/books/enriched")
        check("enriched list", r, 200)
        if r.status == 200:
            data = r.json
            check_true("enriched paginated", "results" in data)
            results = data.get("results", [])
            check_true("enriched has results", len(results) > 0)
            if results:
                first = results[0]
                check_true("enriched has author_name", "author_name" in first)
                check_true("enriched has category_name", "category_name" in first)
                check_true(
                    "author_name not empty",
                    isinstance(first.get("author_name"), str)
                    and len(first["author_name"]) > 0,
                )
                check_true(
                    "category_name not empty",
                    isinstance(first.get("category_name"), str)
                    and len(first["category_name"]) > 0,
                )
                # Verify all results have author/category names
                all_enriched = all(
                    r.get("author_name") and r.get("category_name") for r in results
                )
                check_true("all results enriched", all_enriched)

        # ── Delete ──────────────────────────────────────────────
        if created_id:
            r = s.delete(f"/api/v1/books/{created_id}")
            check("delete book", r, 204)

            r = http_get(f"{base}/api/v1/books/{created_id}")
            check("deleted book → 404", r, 404)

        # ── Authors ─────────────────────────────────────────────
        print("\n--- Authors ---")
        r = http_get(f"{base}/api/v1/authors/")
        check("list authors", r, 200)
        if r.status == 200:
            data = r.json
            check_true("authors paginated", "results" in data)
            check_true("has authors", len(data.get("results", [])) > 0)

        r = http_get(f"{base}/api/v1/authors/1")
        check("retrieve author", r, 200)
        if r.status == 200:
            data = r.json
            check_true("author has name", "name" in data)
            check_true("author has bio", "bio" in data)

        # Author search
        r = http_get(f"{base}/api/v1/authors/?search=Martin")
        check("search authors", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            check_true(
                "found Martin", any("Martin" in a.get("name", "") for a in results)
            )

        # ── Nested Router: Author's Books ───────────────────────
        print("\n--- Nested Router: Author's Books ---")
        r = http_get(f"{base}/api/v1/authors/1/books/")
        check("list books by author 1", r, 200)
        if r.status == 200:
            data = r.json
            check_true("nested has results", "results" in data)
            results = data.get("results", [])
            if results:
                check_true(
                    "all books by author 1",
                    all(b.get("author_id") == 1 for b in results),
                )

        # ── Categories ──────────────────────────────────────────
        print("\n--- Categories ---")
        r = http_get(f"{base}/api/v1/categories/")
        check("list categories", r, 200)
        if r.status == 200:
            data = r.json
            check_true("categories paginated", "results" in data)
            results = data.get("results", [])
            check_true("has categories", len(results) > 0)
            if results:
                check_true("category has name", "name" in results[0])
                check_true("category has slug", "slug" in results[0])

        r = http_get(f"{base}/api/v1/categories/1")
        check("retrieve category", r, 200)

        # ── Reviews: Cursor Pagination ──────────────────────────
        print("\n--- Reviews: Cursor Pagination ---")
        r = http_get(f"{base}/api/v1/reviews/")
        check("list reviews (cursor paginated)", r, 200)
        if r.status == 200:
            data = r.json
            check_true("has results", "results" in data)
            check_true("has next cursor", "next" in data)
            results = data.get("results", [])
            check_true("reviews returned", len(results) > 0)
            if results:
                check_true("review has rating", "rating" in results[0])
                check_true("review has comment", "comment" in results[0])

            # Follow cursor to next page
            next_url = data.get("next")
            if next_url:
                # next_url is relative, append to base
                if next_url.startswith("http"):
                    r2 = http_get(next_url)
                else:
                    r2 = http_get(f"{base}{next_url}")
                check("cursor pagination page 2", r2, 200)
                if r2.status == 200:
                    data2 = r2.json
                    check_true("page 2 has results", len(data2.get("results", [])) > 0)
                    check_true("page 2 has previous", data2.get("previous") is not None)

        # Reviews: Filter by book_id
        r = http_get(f"{base}/api/v1/reviews/?book_id=1")
        check("filter reviews by book_id", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if results:
                check_true(
                    "filtered by book_id=1",
                    all(rv.get("book_id") == 1 for rv in results),
                )

        # Reviews: Filter by rating
        r = http_get(f"{base}/api/v1/reviews/?rating=5")
        check("filter reviews by rating=5", r, 200)
        if r.status == 200:
            data = r.json
            results = data.get("results", [])
            if results:
                check_true("all rating 5", all(rv.get("rating") == 5 for rv in results))

        # Reviews: Create (authenticated)
        r = s.post(
            "/api/v1/reviews/",
            body={
                "book_id": 1,
                "reviewer_name": "E2E Tester",
                "rating": 5,
                "comment": "Great book for testing!",
            },
        )
        check("create review (authenticated)", r, 201)
        review_id = None
        if r.status == 201:
            review_id = r.json.get("id")
            check_true("review has id", review_id is not None)
            check_val("review rating", r.json.get("rating"), 5)

        # Reviews: Validation
        r = s.post(
            "/api/v1/reviews/",
            body={
                "book_id": 99999,
                "reviewer_name": "Bad Reviewer",
                "rating": 5,
                "comment": "Book doesn't exist",
            },
        )
        check("create review invalid book → 400", r, 400)

        r = s.post(
            "/api/v1/reviews/",
            body={
                "book_id": 1,
                "reviewer_name": "Bad Reviewer",
                "rating": 10,
                "comment": "Invalid rating",
            },
        )
        check("create review invalid rating → 400", r, 400)

        # ── Admin (API Key) ─────────────────────────────────────
        print("\n--- Admin (API Key) ---")
        r = http_get(f"{base}/api/admin/stats")
        check("admin stats without key → 401", r, 401)

        r = http_get(f"{base}/api/admin/stats", headers={"X-API-Key": "test-api-key"})
        check("admin stats with API key", r, 200)
        if r.status == 200:
            data = r.json
            check_true("admin stats has books", "books" in data)
            check_true("admin stats has authors", "authors" in data)
            check_true("admin stats has reviews", "reviews" in data)

        # ── Unauthenticated Action Attempts ────────────────────
        # ViewSet's IsAuthenticatedOrReadOnly rejects POST with 403 before
        # @guard_action runs — tests that the overall auth stack works.
        print("\n--- Unauth Actions ---")
        if created_id:
            unauth = Session(base)
            r = unauth.post(f"/api/v1/books/{created_id}/publish")
            check("unauth publish → 403", r, 403)
            r = unauth.post(f"/api/v1/books/{created_id}/feature")
            check("unauth feature → 403", r, 403)

        # ── Bulk Operations ─────────────────────────────────────
        print("\n--- Bulk Operations ---")
        # Bulk create 3 books
        bulk_body = json.dumps(
            [
                {
                    "title": f"Bulk Book {i}",
                    "isbn": f"978-0-BULK-{ts}-{i}",
                    "description": f"Bulk created book {i}",
                    "author_id": 1,
                    "category_id": 1,
                }
                for i in range(3)
            ]
        )
        r = s.post("/api/v1/books/bulk", body=bulk_body)
        check("bulk create 3 books", r, 201)
        bulk_ids = []
        if r.status == 201:
            bulk_ids = [b["id"] for b in r.json]
            check_true("3 books created", len(bulk_ids) == 3)

        # Unauthenticated bulk create → 403
        unauth2 = Session(base)
        r = unauth2.post(
            "/api/v1/books/bulk",
            body=json.dumps(
                [
                    {
                        "title": "Sneaky",
                        "isbn": "978-0-SNEAK-0",
                        "author_id": 1,
                        "category_id": 1,
                    }
                ]
            ),
        )
        check("unauth bulk create → 403", r, 403)

        # ── Validation Errors ───────────────────────────────────
        print("\n--- Validation ---")
        r = s.post(
            "/api/v1/books/",
            body={
                "title": "Missing Required Fields"
                # Missing isbn, author_id, category_id
            },
        )
        check("create book missing fields → 400", r, 400)

        r = s.post(
            "/api/v1/books/",
            body={
                "title": "Bad Author",
                "isbn": "978-0-BAD-AUTH-0",
                "author_id": 99999,
                "category_id": 1,
            },
        )
        check("create book invalid author → 400", r, 400)

        # ── Auth Flows ──────────────────────────────────────────
        print("\n--- Auth Flows ---")
        # Register new user
        s2 = Session(base)
        test_user = f"e2e_tester_{ts}"
        r = s2.post(
            "/auth/register",
            body={
                "username": test_user,
                "password": "test1234",
            },
        )
        check("register user", r, 201)

        # Login with new user
        r = s2.post(
            "/auth/login",
            body={
                "username": test_user,
                "password": "test1234",
            },
        )
        check("login new user", r, 200)

        # New user can create books
        r = s2.post(
            "/api/v1/books/",
            body={
                "title": "User Test Book",
                "isbn": f"978-0-USER-{ts}",
                "description": "Book by new user",
                "author_id": 1,
                "category_id": 1,
            },
        )
        check("new user can create book", r, 201)

        # Logout
        r = s2.post("/auth/logout")
        check("logout", r, 200)

        # Non-staff user cannot publish or feature books
        if created_id:
            r = s2.post(f"/api/v1/books/{created_id}/publish")
            check("non-staff publish → 403", r, 403)
            r = s2.post(f"/api/v1/books/{created_id}/feature")
            check("non-staff feature → 403", r, 403)

        # After logout, create should fail
        r = s2.post(
            "/api/v1/books/",
            body={
                "title": "After Logout",
                "isbn": "978-0-POST-LOGOUT",
                "author_id": 1,
                "category_id": 1,
            },
        )
        check("create after logout → 403", r, 403)

        # Duplicate registration
        s3 = Session(base)
        r = s3.post(
            "/auth/register",
            body={
                "username": "admin",
                "password": "duplicate123",
            },
        )
        check("duplicate registration → 409", r, 409)

        # Bad login
        r = s3.post(
            "/auth/login",
            body={
                "username": "admin",
                "password": "wrongpassword",
            },
        )
        check("bad login → 401", r, 401)

        # ── OpenAPI Spec ────────────────────────────────────────
        print("\n--- OpenAPI ---")
        r = http_get(f"{base}/openapi.json")
        check("openapi.json loads", r, 200)
        if r.status == 200:
            spec = r.json
            check_true("Has openapi version", "openapi" in spec)
            check_true("Has paths", len(spec.get("paths", {})) > 0)
            paths = spec.get("paths", {})
            check_true("Has /api/v1/books/", any("/books" in p for p in paths))
            check_true("Has /api/v1/authors/", any("/authors" in p for p in paths))
            check_true("Has /api/v1/reviews/", any("/reviews" in p for p in paths))

        r = http_get(f"{base}/docs")
        check("Swagger UI loads", r, 200)
        check_true(
            "Swagger has HTML", "swagger" in r.body.lower() or "<html" in r.body.lower()
        )

        # ── Performance Dashboard ──────────────────────────────
        print("\n--- Performance Dashboard ---")

        # HTML dashboard
        r = http_get(f"{base}/debug/performance")
        check("perf dashboard HTML", r, 200)
        if r.status == 200:
            check_true("dashboard has Total Requests", "Total Requests" in r.body)
            check_true("dashboard has Total Queries", "Total Queries" in r.body)

        # JSON stats
        r = http_get(f"{base}/debug/performance/json")
        check("perf stats JSON", r, 200)
        if r.status == 200:
            data = r.json
            check_true("stats has total_requests", "total_requests" in data)
            check_true("total_requests > 0", data.get("total_requests", 0) > 0)
            check_true("stats has avg_queries", "avg_queries_per_request" in data)

        # X-Query-Count header on regular endpoints — must show NON-ZERO queries
        # (validates that PerformanceMiddleware.record_query is wired into the
        # database layer; without this, X-Query-Count would always be 0)
        r = http_get(f"{base}/api/v1/books/")
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        check_true("X-Query-Count header present", "x-query-count" in hdrs)
        qc = int(hdrs.get("x-query-count", "0"))
        check_true(f"X-Query-Count > 0 (got {qc})", qc > 0)
        check_true("X-Query-Time header present", "x-query-time" in hdrs)

        # Detail endpoint uses select_related + aggregate — should have >= 1 query
        r = http_get(f"{base}/api/v1/books/1")
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        detail_qc = int(hdrs.get("x-query-count", "0"))
        check_true(f"Detail tracks queries (got {detail_qc})", detail_qc >= 1)

        # After several requests, dashboard should show non-zero query count
        r = http_get(f"{base}/debug/performance/json")
        if r.status == 200:
            data = r.json
            tq = data.get("total_queries", 0)
            check_true(f"dashboard total_queries > 0 (got {tq})", tq > 0)

        # Flamegraph endpoint
        r = http_get(f"{base}/debug/performance/flamegraph")
        check("flamegraph endpoint", r, 200)

        # Profiles endpoint
        r = http_get(f"{base}/debug/performance/profiles")
        check("profiles JSON", r, 200)
        if r.status == 200:
            data = r.json
            check_true("profiles has list", "profiles" in data)
            check_true("profiles has total_stored", "total_stored" in data)

        # ── HyperAdmin ─────────────────────────────────────────
        print("\n--- HyperAdmin ---")

        # Admin login page
        r = http_get(f"{base}/admin/login/")
        check("admin login page", r, 200)
        check_true("admin login has form", "username" in r.body)

        # Admin dashboard (requires auth — should redirect to login)
        r = http_get(f"{base}/admin/")
        check_true(
            "admin dashboard requires auth",
            r.status in (302, 303) or "login" in r.body.lower(),
        )

        # Admin list pages (unauthenticated — redirect to login)
        r = http_get(f"{base}/admin/book/")
        check_true(
            "admin book list requires auth",
            r.status in (302, 303) or "login" in r.body.lower(),
        )

    # ── Summary ─────────────────────────────────────────────
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
