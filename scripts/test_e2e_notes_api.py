"""
Notes API E2E tests — intermediate service.

Tests auth, CRUD, pagination, F expression updates, admin.

# hyper-test: e2e
"""

import subprocess
import sys
import time

from e2e_helper import (
    TEST_PORTS,
    AppRunner,
    Session,
    http_get,
    service_app,
    service_seed,
)

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


def check(name, r, expected_status):
    return ok(
        name, r.status == expected_status, f"expected {expected_status}, got {r.status}"
    )


def main():
    global PASS, FAIL
    print("=" * 60)
    print("Notes API E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["notes_api"]

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            service_app("notes_api"),
            "--drop",
            "--seed",
            service_seed("notes_api"),
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(service_app("notes_api"), host="127.0.0.1", port=port) as runner:
        base = runner.url()
        s = Session(base)

        # ── Health ──
        print("\n--- Health ---")
        r = http_get(f"{base}/health")
        check("health 200", r, 200)

        # ── Categories (no auth needed) ──
        print("\n--- Categories ---")
        r = http_get(f"{base}/api/categories/")
        check("categories 200", r, 200)
        # Defensive: validate shape once before iterating, so a flake yields
        # a useful diagnostic instead of "TypeError: string indices must be
        # integers" when r.json comes back as something unexpected (e.g.,
        # an error envelope dict under parallel load).
        cats = r.json
        if not (isinstance(cats, list) and all(isinstance(c, dict) for c in cats)):
            ok(
                "categories is list[dict]",
                False,
                f"unexpected shape {type(cats).__name__}: {r.body[:200]!r}",
            )
            return
        ok("has categories", len(cats) == 5, f"got {len(cats)}")
        ok("Work category exists", any(c.get("name") == "Work" for c in cats))

        # Check note_count from seed
        work_cat = next((c for c in cats if c.get("name") == "Work"), None)
        if work_cat is None:
            ok("Work category in list", False, f"got {[c.get('name') for c in cats]}")
        else:
            ok(
                "Work note_count = 2",
                work_cat.get("note_count") == 2,
                f"got {work_cat.get('note_count')}",
            )

        # ── FTS Search ──
        print("\n--- FTS Search ---")
        r = http_get(f"{base}/api/notes/search?q=python")
        check("search 200", r, 200)
        ok("search returns results", len(r.json) > 0, f"got {len(r.json)}")
        if r.json:
            ok("search result has title", "title" in r.json[0])

        r = http_get(f"{base}/api/notes/search?q=CI+pipeline")
        check("search CI pipeline 200", r, 200)
        ok("CI pipeline found", any("CI" in n.get("title", "") for n in r.json))

        r = http_get(f"{base}/api/notes/search?q=x")
        check("search too short 400", r, 400)

        # ── Notes list (no auth) ──
        print("\n--- Notes list ---")
        r = http_get(f"{base}/api/notes/")
        check("notes list 200", r, 200)
        ok("has results", "results" in r.json)
        ok(
            "10 seeded notes",
            len(r.json["results"]) == 10,
            f"got {len(r.json['results'])}",
        )

        # ── Auth: register ──
        print("\n--- Auth ---")
        ts = str(int(time.time()) % 100000)
        r = s.post(
            "/auth/register",
            body={"username": f"test_{ts}", "password": "testpass1234"},
        )
        check("register 201", r, 201)
        ok("register has id", "id" in r.json)

        # ── Auth: login with wrong password ──
        r = s.post("/auth/login", body={"username": f"test_{ts}", "password": "wrong"})
        check("bad login 401", r, 401)

        # ── Auth: login correct ──
        r = s.post(
            "/auth/login", body={"username": f"test_{ts}", "password": "testpass1234"}
        )
        check("login 200", r, 200)

        # ── Create note (auth required) ──
        print("\n--- CRUD ---")
        work_id = work_cat["id"]
        r = s.post(
            "/api/notes/",
            body={
                "title": "E2E test note",
                "body": "Created by test",
                "category_id": work_id,
            },
        )
        check("create note 201", r, 201)
        ok("created has id", "id" in r.json)
        note_id = r.json.get("id", 0)

        # Verify category note_count incremented (F expression)
        r = http_get(f"{base}/api/categories/")
        work_after = next(c for c in r.json if c["name"] == "Work")
        ok(
            "note_count incremented",
            work_after["note_count"] == 3,
            f"expected 3, got {work_after['note_count']}",
        )

        # ── Get single note ──
        r = http_get(f"{base}/api/notes/{note_id}")
        check("get note 200", r, 200)
        ok("title matches", r.json.get("title") == "E2E test note")

        # ── Get non-existent note ──
        r = http_get(f"{base}/api/notes/99999")
        check("missing note 404", r, 404)

        # ── Create without auth ──
        s2 = Session(base)
        r = s2.post("/api/notes/", body={"title": "No auth", "category_id": work_id})
        check("unauth create 401", r, 401)

        # ── Create without title ──
        r = s.post("/api/notes/", body={"category_id": work_id})
        check("no title 400", r, 400)

        # ── Create with invalid category ──
        r = s.post("/api/notes/", body={"title": "Bad cat", "category_id": 99999})
        check("bad category 404", r, 404)

        # ── Delete note ──
        r = s.delete(f"/api/notes/{note_id}")
        check("delete 200", r, 200)
        ok("delete confirmed", r.json.get("deleted") is True)

        # Verify note_count decremented
        r = http_get(f"{base}/api/categories/")
        work_final = next(c for c in r.json if c["name"] == "Work")
        ok(
            "note_count decremented",
            work_final["note_count"] == 2,
            f"expected 2, got {work_final['note_count']}",
        )

        # ── Delete non-existent ──
        r = s.delete(f"/api/notes/{note_id}")
        check("re-delete 404", r, 404)

        # ── Logout ──
        r = s.post("/auth/logout")
        check("logout 200", r, 200)

        # ── Admin ──
        print("\n--- Admin ---")
        r = http_get(f"{base}/admin/login/")
        check("admin login page 200", r, 200)
        ok("admin has form", "username" in r.body)

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    print(f"{'=' * 60}")

    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")

    return FAIL == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
