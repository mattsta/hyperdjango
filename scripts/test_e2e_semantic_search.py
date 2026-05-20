"""
End-to-end tests for the pgvector semantic search service.

# hyper-test: e2e

Tests the pgvector infrastructure (health, stats, auth, pages) always.
Tests embedding-dependent features (search, submit, API) only when
EMBEDDINGS_API_KEY is set — otherwise those tests are skipped.
"""

import json
import os
import subprocess

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    Session,
    http_get,
    http_post,
)

PASS = 0
FAIL = 0
SKIP = 0
ERRORS: list[str] = []

HAS_API_KEY = bool(os.environ.get("EMBEDDINGS_API_KEY", ""))


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


def skip(name: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP  {name} (no EMBEDDINGS_API_KEY)")


if __name__ == "__main__":
    port = TEST_PORTS["semantic_search"]
    ts = str(int(__import__("time").time()))

    # Build DATABASE_URL from env
    env = os.environ.copy()
    db_url = env.get("DATABASE_URL", "")
    if not db_url:
        host = env.get("PGHOST", "localhost")
        pg_port = env.get("PGPORT", "5432")
        user = env.get("PGUSER", env.get("USER", "postgres"))
        password = env.get("PGPASSWORD", "")
        dbname = env.get("PGDATABASE", "hyperdjango_test")
        db_url = f"postgresql://{user}:{password}@{host}:{pg_port}/{dbname}"
    os.environ["DATABASE_URL"] = db_url

    # Run setup (creates tables from models, seeds if API key available)
    print("Running setup...")
    setup_result = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.semantic_search.app:app",
            "--drop",
            "--seed",
            "services.semantic_search.seed:run",
        ],
        capture_output=True,
        timeout=120,
    )
    if setup_result.returncode != 0:
        print(f"Setup failed:\n{setup_result.stderr}")
        raise SystemExit(1)

    print("Starting server...")
    with AppRunner(
        "services.semantic_search.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        s = Session(runner.url())

        # ── Health + Readiness (always works) ──
        print("\n--- Health + Readiness ---")
        r = s.get("/health")
        ok("Health 200", r.status == 200)
        ok("Health has status ok", r.json.get("status") == "ok")

        r = s.get("/ready")
        ok("Readiness 200", r.status == 200)
        ready = r.json
        ok("Ready status ok", ready.get("status") == "ok")
        ok("Ready has checks", "checks" in ready)
        ok("Ready DB check ok", ready.get("checks", {}).get("database") == "ok")

        # ── Stats (always works) ──
        print("\n--- Stats ---")
        r = s.get("/stats")
        ok("Stats 200", r.status == 200)
        stats = r.json
        ok("Stats has dimensions", "vector_dimensions" in stats)
        ok("Stats has model", "embeddings_model" in stats)
        ok("Stats has hnsw", stats.get("index_type") == "hnsw")
        ok("Stats has configured flag", "configured" in stats)

        # ── Home page (always works) ──
        print("\n--- Home page ---")
        r = s.get("/")
        ok("Home 200", r.status == 200)
        ok("Home has search form", "Search articles" in r.body)
        ok("Home has nav brand", "Semantic Search" in r.body)
        ok("Home has category select", "<select" in r.body)
        ok("Home has login link", "/login" in r.body)

        # ── Auth: Register (always works) ──
        print("\n--- Auth: Register ---")
        r = s.get("/register")
        ok("Register page 200", r.status == 200)
        ok("Register has form", "Register" in r.body and "username" in r.body)

        r = s.post(
            "/register",
            body=f"username=tester_{ts}&password=test1234",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Register redirects", r.status in (200, 302), f"got {r.status}")

        r = s.get("/")
        ok("After register: logged in", f"tester_{ts}" in r.body or "Logout" in r.body)
        ok("After register: submit link visible", "/submit" in r.body)

        # ── Auth: Logout + Login (always works) ──
        print("\n--- Auth: Logout + Login ---")
        r = s.post("/logout")
        r = s.get("/")
        ok("After logout: login link", "/login" in r.body)

        r = s.post(
            "/login",
            body=f"username=demo&password={SEED_PASSWORD}",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Login with demo", r.status in (200, 302), f"got {r.status}")
        r = s.get("/")
        ok("Logged in as demo", "demo" in r.body or "Logout" in r.body)

        # ── Embedding-dependent tests (require API key) ──
        if HAS_API_KEY:
            # ── Search ──
            print("\n--- Search ---")
            r = s.get("/search?q=machine+learning+neural+network")
            ok("ML search 200", r.status == 200)
            ok("ML search has results", "% match" in r.body)
            ok("ML search shows timing", "ms" in r.body)

            # ── Category filter ──
            print("\n--- Category filter ---")
            r = s.get("/search?q=database&category=database")
            ok("Category search 200", r.status == 200)
            ok("Category results exist", "% match" in r.body)

            # ── Submit article ──
            print("\n--- Submit article ---")
            r = s.get("/submit")
            ok("Submit page 200", r.status == 200)
            ok("Submit has form", "Title" in r.body or "title" in r.body)

            r = s.post(
                "/submit",
                body=f"title=Test+Article+{ts}&body=Python+web+framework+performance&category=python",
                content_type="application/x-www-form-urlencoded",
            )
            ok("Submit redirects", r.status in (200, 302), f"got {r.status}")

            # ── Article detail ──
            print("\n--- Article detail ---")
            r = s.get("/article/1")
            ok("Detail page 200", r.status == 200)
            ok("Detail has title", r.body.count("<h1") >= 1)
            ok("Detail has similar articles", "Similar" in r.body)

            # ── API: search ──
            print("\n--- API ---")
            r = s.post(
                "/api/search",
                body=json.dumps({"text": "python async web framework"}),
                content_type="application/json",
            )
            ok("API search 200", r.status == 200)
            data = r.json
            ok("API has results", len(data.get("results", [])) > 0)
            ok("API has timing_ms", "timing_ms" in data)

            # API with category + limit
            r = s.post(
                "/api/search",
                body=json.dumps(
                    {"text": "database", "category": "database", "limit": 3}
                ),
                content_type="application/json",
            )
            ok("API category search 200", r.status == 200)
            ok("API limit respected", len(r.json["results"]) <= 3)

            # ── API: embed ──
            print("\n--- Embed API ---")
            r = s.post(
                "/api/embed",
                body=json.dumps({"text": "machine learning"}),
                content_type="application/json",
            )
            ok("Embed 200", r.status == 200)
            embed = r.json
            ok(
                "Embed has vector",
                len(embed.get("vector", [])) > 0,
                f"got {len(embed.get('vector', []))} dims",
            )
            ok("Embed has model name", "model" in embed)

            # ── Relevance check ──
            print("\n--- Relevance ---")
            r = s.post(
                "/api/search",
                body=json.dumps({"text": "vector embedding search", "category": "ml"}),
                content_type="application/json",
            )
            results = r.json.get("results", [])
            if len(results) >= 2:
                ok(
                    "Sorted by similarity",
                    results[0]["similarity"] >= results[1]["similarity"],
                )
            else:
                ok("Enough results for ranking", False, f"only {len(results)}")

        else:
            print(
                "\n--- Skipping embedding-dependent tests (no EMBEDDINGS_API_KEY) ---"
            )
            for name in [
                "ML search",
                "Category search",
                "Submit article",
                "Article detail",
                "API search",
                "API embed",
                "Relevance",
            ]:
                skip(name)

        # ── API: bad input + auth checks (always works) ──
        print("\n--- Error handling ---")
        r = s.post("/api/search", body=json.dumps({}), content_type="application/json")
        ok("API bad input 400", r.status == 400)

        # Unauthenticated embed should be 401
        r = http_post(
            f"{runner.url()}/api/embed",
            body=json.dumps({"text": "test"}),
        )
        ok("Embed without auth 401", r.status == 401, f"got {r.status}")

        r = s.get("/article/999999")
        ok(
            "Missing article 404",
            r.status == 404 or "not found" in r.body.lower(),
            f"got {r.status}",
        )

        r = s.get("/search?q=")
        ok("Empty search redirects", r.status in (200, 302))

        # ── HyperAdmin ──
        print("\n--- HyperAdmin ---")
        r = http_get(f"{runner.url()}/admin/login/")
        ok(
            "Admin login page accessible",
            r.status == 200 and "username" in r.body,
            f"got {r.status}",
        )

        r = http_get(f"{runner.url()}/admin/")
        ok(
            "Admin dashboard requires auth",
            r.status in (302, 303) or "login" in r.body.lower(),
            f"got {r.status}",
        )

    # ── Summary ──
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    parts = [f"{PASS}/{total} passed", f"{FAIL} failed"]
    if SKIP:
        parts.append(f"{SKIP} skipped")
    print(f"Results: {', '.join(parts)}")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    if not HAS_API_KEY:
        print(
            "\nNote: Set EMBEDDINGS_API_KEY to run full test suite with real embeddings."
        )
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)
