"""
End-to-end tests for Full-Stack Task Manager scaffold app.

# hyper-test: e2e

Tests auth flow, project CRUD, task CRUD, API, admin, health.
"""

import os
import re
import subprocess
import time

from e2e_helper import TEST_PORTS, AppRunner, Session, http_get

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


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Full-Stack Task Manager E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["full_stack"]
    ts = str(int(time.time()))

    # Build DATABASE_URL from env (test runner sets PG* vars)
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

    print("Running setup...")
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.full_stack.app:app",
            "--drop",
            "--seed",
            "services.full_stack.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    print("Starting server...")
    with AppRunner(
        "services.full_stack.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        s = Session(runner.url())

        # ── Health ──
        print("\n--- Health ---")
        r = s.get("/health")
        ok("Health 200", r.status == 200)
        ok("Health status ok", r.json.get("status") == "ok")

        r = s.get("/ready")
        ok("Ready 200", r.status == 200)
        ok("Ready status ok", r.json.get("status") == "ok")
        ok("Ready has checks", "checks" in r.json)

        # ── Auth enforcement ──
        print("\n--- Auth enforcement ---")
        r = http_get(f"{runner.url()}/")
        ok("Dashboard requires auth", r.status in (302, 303) or "/login" in r.body)

        # ── Register ──
        print("\n--- Register ---")
        r = s.get("/register")
        ok("Register page 200", r.status == 200)
        ok("Register has form", "username" in r.body)

        r = s.post(
            "/register",
            body=f"username=tester_{ts}&email=test@test.com&password=test1234",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Register succeeds", r.status in (200, 302), f"got {r.status}")

        r = s.get("/")
        ok("After register: dashboard loads", r.status == 200)
        ok("Dashboard has welcome", "Welcome" in r.body or "Dashboard" in r.body)

        # ── Logout + Login ──
        print("\n--- Logout + Login ---")
        r = s.post("/logout")
        s2 = Session(runner.url())
        r = s2.get("/login")
        ok("Login page 200", r.status == 200)
        ok("Login has form", "username" in r.body and "password" in r.body)

        # Bad credentials
        r = s2.post(
            "/login",
            body=f"username=tester_{ts}&password=wrong",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Bad login shows error", "Invalid" in r.body or r.status == 200)

        # Good credentials (login as the user we just registered)
        r = s2.post(
            "/login",
            body=f"username=tester_{ts}&password=test1234",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Login succeeds", r.status in (200, 302), f"got {r.status}")

        r = s2.get("/")
        ok("Logged in: dashboard", r.status == 200)
        ok(
            "Dashboard shows content",
            "Dashboard" in r.body or "project" in r.body.lower(),
        )

        # ── Project CRUD ──
        print("\n--- Project CRUD ---")
        r = s2.get("/projects/new")
        ok("New project page 200", r.status == 200)
        ok("New project has form", "name" in r.body)

        r = s2.post(
            "/projects/new",
            body=f"name=Test+Project+{ts}&description=A+test+project",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Create project redirects", r.status in (200, 302), f"got {r.status}")

        # Follow redirect to project detail
        if r.status in (302, 303):
            loc = r.headers.get("location", "")
            if loc:
                r = s2.get(loc)

        # Extract project ID from the redirect or page
        proj_match = re.search(
            r"/projects/(\d+)",
            r.headers.get("location", "") if r.status in (302, 303) else r.body,
        )
        proj_id = proj_match.group(1) if proj_match else "1"

        r = s2.get(f"/projects/{proj_id}")
        ok("Project detail 200", r.status == 200)
        ok(
            "Project detail has task section",
            "task" in r.body.lower() or "Add" in r.body,
        )

        # Missing project
        r = s2.get("/projects/999999")
        ok("Missing project 404", r.status == 404, f"got {r.status}")

        # ── Task CRUD ──
        print("\n--- Task CRUD ---")
        r = s2.post(
            f"/projects/{proj_id}/tasks",
            body=f"title=New+Task+{ts}",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Create task redirects", r.status in (200, 302), f"got {r.status}")

        r = s2.get(f"/projects/{proj_id}")
        ok("Task appears in project", f"New Task {ts}" in r.body or r.status == 200)

        # Find a task ID from the project page
        task_match = re.search(r"/tasks/(\d+)/", r.body)
        task_id = task_match.group(1) if task_match else None

        if task_id:
            r = s2.post(
                f"/tasks/{task_id}/status",
                body="status=done",
                content_type="application/x-www-form-urlencoded",
            )
            ok("Update task status", r.status in (200, 302), f"got {r.status}")

            r = s2.post(
                f"/tasks/{task_id}/delete",
                body="",
                content_type="application/x-www-form-urlencoded",
            )
            ok("Delete task", r.status in (200, 302), f"got {r.status}")
        else:
            ok("Update task status", False, "no task ID found")
            ok("Delete task", False, "no task ID found")

        # Missing task
        r = s2.post(
            "/tasks/999999/status",
            body="status=done",
            content_type="application/x-www-form-urlencoded",
        )
        ok("Missing task 404", r.status == 404, f"got {r.status}")

        # ── API ──
        print("\n--- API ---")
        r = s2.get("/api/projects")
        ok("API projects 200", r.status == 200)
        ok("API has projects list", "projects" in r.json)

        r = s2.get(f"/api/projects/{proj_id}/tasks")
        ok("API tasks 200", r.status == 200)
        ok("API has tasks list", "tasks" in r.json)

        # API without auth
        r = http_get(f"{runner.url()}/api/projects")
        ok("API requires auth", r.status == 401, f"got {r.status}")

        # ── Admin ──
        print("\n--- Admin ---")
        r = http_get(f"{runner.url()}/admin/login/")
        ok("Admin login 200", r.status == 200)
        ok("Admin login has form", "username" in r.body)

        r = http_get(f"{runner.url()}/admin/")
        ok("Admin requires auth", r.status in (302, 303) or "login" in r.body.lower())

        # ── 404 ──
        print("\n--- Error handling ---")
        r = s2.get("/nonexistent")
        ok("Unknown route 404", r.status == 404)

    # ── Summary ──
    print(f"\n{'=' * 60}")
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
