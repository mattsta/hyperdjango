"""
End-to-end tests for Multi-Tenant SaaS service.

Tests automatic tenant isolation via TenantMixin + TenantMiddleware:
- Tenant-scoped CRUD (projects, tasks, members)
- Tenant isolation (tenant A cannot see tenant B's data)
- Cross-tenant admin queries (unscoped)
- tenant_context() explicit scoping
- Missing tenant header → 400
- Per-tenant stats
- Auth scoped to tenant
"""

# hyper-test: e2e

import subprocess
import sys
import time

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    _http_request,
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


def check_true(name, condition):
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


def check_val(name, actual, expected):
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


def t_get(base, path, tenant_id, headers=None):
    """GET with X-Tenant-ID header."""
    h = {"X-Tenant-ID": str(tenant_id)}
    if headers:
        h.update(headers)
    return http_get(f"{base}{path}", headers=h)


def t_post(base, path, tenant_id, body=None, headers=None):
    """POST with X-Tenant-ID header."""
    h = {"X-Tenant-ID": str(tenant_id)}
    if headers:
        h.update(headers)
    return http_post(f"{base}{path}", body=body, headers=h)


def t_patch(base, path, tenant_id, body=None, headers=None):
    """PATCH with X-Tenant-ID header."""
    h = {"X-Tenant-ID": str(tenant_id), "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return _http_request("PATCH", f"{base}{path}", body=body, headers=h)


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Multi-Tenant SaaS E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["multi_tenant"]

    # Setup
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.multi_tenant.app:app",
            "--drop",
            "--seed",
            "services.multi_tenant.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.multi_tenant.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # First, get the tenant IDs
        r = http_get(f"{base}/api/admin/tenants", headers={"X-API-Key": "test-api-key"})
        check("admin list tenants", r, 200)
        tenant_ids = {}
        if r.status == 200:
            for org in r.json:
                tenant_ids[org["slug"]] = org["id"]
            check_true("has 3 tenants", len(tenant_ids) == 3)

        acme_id = tenant_ids.get("acme", 1)
        globex_id = tenant_ids.get("globex", 2)
        initech_id = tenant_ids.get("initech", 3)

        # ── Health ──────────────────────────────────────────────
        print("\n--- Health ---")
        r = http_get(f"{base}/health")
        check("health endpoint", r, 200)
        if r.status == 200:
            check_true("health status ok", r.json.get("status") == "ok")

        # ── Missing Tenant → 400 ───────────────────────────────
        print("\n--- Missing Tenant ---")
        r = http_get(f"{base}/api/projects/")
        check("projects without tenant → 400", r, 400)

        r = http_get(f"{base}/api/tasks/")
        check("tasks without tenant → 400", r, 400)

        r = http_get(f"{base}/api/members/")
        check("members without tenant → 400", r, 400)

        # ── Tenant Scoped: Projects ─────────────────────────────
        print("\n--- Projects (Acme) ---")
        r = t_get(base, "/api/projects/", acme_id)
        check("list acme projects", r, 200)
        acme_projects = []
        if r.status == 200:
            data = r.json
            # CursorPagination returns {results, next, previous}
            check_true("projects cursor-paginated", "results" in data)
            acme_projects = data.get("results", [])
            check_true("acme has projects", len(acme_projects) > 0)

        print("\n--- Projects (Globex) ---")
        r = t_get(base, "/api/projects/", globex_id)
        check("list globex projects", r, 200)
        globex_projects = []
        if r.status == 200:
            globex_projects = r.json.get("results", [])
            check_true("globex has projects", len(globex_projects) > 0)

        # ── TENANT ISOLATION: acme projects != globex projects ──
        print("\n--- Tenant Isolation ---")
        if acme_projects and globex_projects:
            acme_ids = {p["id"] for p in acme_projects}
            globex_ids = {p["id"] for p in globex_projects}
            check_true("no project ID overlap", len(acme_ids & globex_ids) == 0)

            acme_names = {p["name"] for p in acme_projects}
            globex_names = {p["name"] for p in globex_projects}
            check_true("different project names", acme_names != globex_names)

        # Acme can't see Globex project by ID
        if globex_projects:
            globex_proj_id = globex_projects[0]["id"]
            r = t_get(base, f"/api/projects/{globex_proj_id}", acme_id)
            check("acme can't see globex project → 404", r, 404)

        # ── Unauthenticated Write → 401 ─────────────────────────
        print("\n--- Auth Enforcement ---")
        r = t_post(base, "/api/projects/", acme_id, body={"name": "Unauthed"})
        check("create project without auth → 401", r, 401)

        r = t_post(
            base, "/api/tasks/", acme_id, body={"project_id": 1, "title": "Unauthed"}
        )
        check("create task without auth → 401", r, 401)

        # Login as acme admin
        r = t_post(
            base,
            "/auth/login",
            acme_id,
            body={"username": "acme_admin", "password": SEED_PASSWORD},
        )
        check("login acme admin", r, 200)
        acme_session_cookie = ""
        if r.status == 200:
            raw_cookie = r.headers.get("set-cookie", "")
            if "=" in raw_cookie:
                acme_session_cookie = raw_cookie.split(";")[0]

        # ── Create Project (authenticated) ──────────────────────
        print("\n--- Create Project ---")
        ts = str(int(time.time()) % 100000)
        auth_headers = {"Cookie": acme_session_cookie} if acme_session_cookie else {}
        r = t_post(
            base,
            "/api/projects/",
            acme_id,
            body={
                "name": f"E2E Test Project {ts}",
                "description": "Created by e2e test",
            },
            headers=auth_headers,
        )
        check("create project in acme", r, 201)
        new_proj_id = None
        if r.status == 201:
            data = r.json
            new_proj_id = data.get("id")
            check_true("project has id", new_proj_id is not None)
            check_val("project tenant_id", data.get("tenant_id"), acme_id)

        # Verify new project visible to acme
        if new_proj_id:
            r = t_get(base, f"/api/projects/{new_proj_id}", acme_id)
            check("acme sees new project", r, 200)

            # Acme CAN see its own project detail
            r = t_get(
                base, f"/api/projects/{new_proj_id}", acme_id, headers=auth_headers
            )
            check("acme project detail", r, 200)
            if r.status == 200:
                check_val(
                    "detail has project name",
                    r.json.get("name"),
                    f"E2E Test Project {ts}",
                )

            # Globex can't see it
            r = t_get(base, f"/api/projects/{new_proj_id}", globex_id)
            check("globex can't see acme's new project → 404", r, 404)

        # ── Tasks ───────────────────────────────────────────────
        print("\n--- Tasks ---")
        r = t_get(base, "/api/tasks/", acme_id)
        check("list acme tasks", r, 200)
        acme_tasks = []
        if r.status == 200:
            acme_tasks = r.json.get("results", [])
            check_true("acme has tasks", len(acme_tasks) > 0)

        r = t_get(base, "/api/tasks/", globex_id)
        check("list globex tasks", r, 200)
        globex_tasks = []
        if r.status == 200:
            globex_tasks = r.json.get("results", [])
            check_true("globex has tasks", len(globex_tasks) > 0)

        # Task isolation
        if acme_tasks and globex_tasks:
            acme_task_ids = {t["id"] for t in acme_tasks}
            globex_task_ids = {t["id"] for t in globex_tasks}
            check_true("no task ID overlap", len(acme_task_ids & globex_task_ids) == 0)

        # ── Create Task ─────────────────────────────────────────
        print("\n--- Create Task ---")
        new_task_id = None
        if new_proj_id:
            r = t_post(
                base,
                "/api/tasks/",
                acme_id,
                body={
                    "project_id": new_proj_id,
                    "title": f"E2E Task {ts}",
                    "priority": "high",
                    "assignee": "acme_admin",
                },
                headers=auth_headers,
            )
            check("create task in acme project", r, 201)
            if r.status == 201:
                data = r.json
                new_task_id = data.get("id")
                check_val("task tenant_id", data.get("tenant_id"), acme_id)

            # Globex can't create task in acme's project (even if authed as globex)
            r = t_post(
                base,
                "/api/tasks/",
                globex_id,
                body={
                    "project_id": new_proj_id,
                    "title": "Globex trying to inject",
                },
            )
            # Globex isn't logged in, so 401
            check("globex can't create task in acme project → 401", r, 401)

        # ── Update Task ─────────────────────────────────────────
        print("\n--- Update Task ---")
        if new_task_id:
            r = t_patch(
                base,
                f"/api/tasks/{new_task_id}",
                acme_id,
                body={"status": "in_progress"},
                headers=auth_headers,
            )
            check("update task status", r, 200)
            if r.status == 200:
                check_val("task status updated", r.json.get("status"), "in_progress")

            # Globex can't update acme's task (not authed)
            r = t_patch(
                base, f"/api/tasks/{new_task_id}", globex_id, body={"status": "done"}
            )
            check("globex can't update acme task → 401", r, 401)

        # ── Filter Tasks ────────────────────────────────────────
        print("\n--- Filter Tasks ---")
        r = t_get(base, "/api/tasks/?status=todo", acme_id)
        check("filter acme tasks by status", r, 200)
        if r.status == 200:
            tasks = r.json.get("results", [])
            if tasks:
                check_true(
                    "all filtered tasks are todo",
                    all(t.get("status") == "todo" for t in tasks),
                )

        if new_proj_id:
            r = t_get(base, f"/api/tasks/?project_id={new_proj_id}", acme_id)
            check("filter tasks by project_id", r, 200)

        # ── Members ─────────────────────────────────────────────
        print("\n--- Members ---")
        r = t_get(base, "/api/members/", acme_id)
        check("list acme members", r, 200)
        acme_member_count = 0
        if r.status == 200:
            members = r.json.get("results", [])
            acme_member_count = len(members)
            check_true("acme has members", acme_member_count >= 2)
            check_true(
                "acme seed members present",
                any("acme" in m.get("username", "") for m in members),
            )

        r = t_get(base, "/api/members/", globex_id)
        check("list globex members", r, 200)
        globex_member_count = 0
        if r.status == 200:
            members = r.json.get("results", [])
            globex_member_count = len(members)
            check_true("globex has members", globex_member_count >= 2)
            check_true(
                "globex seed members present",
                any("globex" in m.get("username", "") for m in members),
            )

        if acme_member_count and globex_member_count:
            check_true(
                "member lists are separate",
                acme_member_count >= 2 and globex_member_count >= 2,
            )

        # ── Add Member ──────────────────────────────────────────
        print("\n--- Add Member ---")
        r = t_post(
            base,
            "/api/members/",
            acme_id,
            body={
                "username": f"e2e_user_{ts}",
                "password": "test1234",
                "role": "member",
            },
            headers=auth_headers,
        )
        check("add member to acme", r, 201)
        if r.status == 201:
            check_val("member tenant_id", r.json.get("tenant_id"), acme_id)

        # Login as globex admin for globex write operations
        r = t_post(
            base,
            "/auth/login",
            globex_id,
            body={"username": "globex_admin", "password": SEED_PASSWORD},
        )
        globex_cookie = ""
        if r.status == 200:
            raw_cookie = r.headers.get("set-cookie", "")
            if "=" in raw_cookie:
                globex_cookie = raw_cookie.split(";")[0]
        globex_headers = {"Cookie": globex_cookie} if globex_cookie else {}

        # Same username in different tenant should work
        r = t_post(
            base,
            "/api/members/",
            globex_id,
            body={
                "username": f"e2e_user_{ts}",
                "password": "test1234",
                "role": "member",
            },
            headers=globex_headers,
        )
        check("same username in globex (different tenant)", r, 201)

        # ── Auth (tenant-scoped login) ──────────────────────────
        print("\n--- Auth ---")
        r = t_post(
            base,
            "/auth/login",
            acme_id,
            body={
                "username": "acme_admin",
                "password": SEED_PASSWORD,
            },
        )
        check("login as acme admin", r, 200)
        if r.status == 200:
            data = r.json
            check_val("login org_id", data.get("org_id"), acme_id)
            check_val("login role", data.get("role"), "admin")

        # Can't login with acme creds in globex tenant
        r = t_post(
            base,
            "/auth/login",
            globex_id,
            body={
                "username": "acme_admin",
                "password": SEED_PASSWORD,
            },
        )
        check("acme creds fail in globex → 401", r, 401)

        # ── Per-Tenant Stats ────────────────────────────────────
        print("\n--- Stats ---")
        r = t_get(base, "/api/stats", acme_id)
        check("acme stats", r, 200)
        if r.status == 200:
            data = r.json
            check_val("stats org_id", data.get("org_id"), acme_id)
            check_true("stats has projects", data.get("projects", 0) > 0)
            check_true("stats has tasks", data.get("tasks", 0) > 0)
            check_true("stats has members", data.get("members", 0) > 0)

        r = t_get(base, "/api/stats", globex_id)
        check("globex stats", r, 200)
        if r.status == 200:
            check_val("globex stats org_id", r.json.get("org_id"), globex_id)

        # ── Admin: Global Stats ─────────────────────────────────
        print("\n--- Admin ---")
        r = http_get(f"{base}/api/admin/stats", headers={"X-API-Key": "test-api-key"})
        check("admin global stats", r, 200)
        if r.status == 200:
            data = r.json
            check_true("global orgs", data.get("total_orgs", 0) >= 3)
            check_true("global projects", data.get("total_projects", 0) > 0)
            check_true("global tasks", data.get("total_tasks", 0) > 0)

        # Admin without key
        r = http_get(f"{base}/api/admin/stats")
        check("admin without key → 401", r, 401)

        # ── Cross-Tenant Demo (unscoped + tenant_context) ───────
        print("\n--- Cross-Tenant Demo ---")
        r = http_get(
            f"{base}/api/cross-tenant-demo", headers={"X-API-Key": "test-api-key"}
        )
        check("cross-tenant demo", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "unscoped > tenant 1",
                data.get("unscoped_total", 0) > data.get("tenant_1_projects", 0),
            )
            check_true(
                "unscoped > tenant 2",
                data.get("unscoped_total", 0) > data.get("tenant_2_projects", 0),
            )
            check_true("isolation verified", data.get("isolation_verified") is True)

        # ── Validation ──────────────────────────────────────────
        print("\n--- Validation ---")
        r = t_post(base, "/api/projects/", acme_id, body={}, headers=auth_headers)
        check("create project without name → 400", r, 400)

        r = t_post(base, "/api/tasks/", acme_id, body={}, headers=auth_headers)
        check("create task without fields → 400", r, 400)

        r = t_post(
            base,
            "/api/members/",
            acme_id,
            body={"username": "", "password": "x"},
            headers=auth_headers,
        )
        check("add member without username → 400", r, 400)

        # Duplicate member in same tenant
        r = t_post(
            base,
            "/api/members/",
            acme_id,
            body={"username": "acme_admin", "password": "test1234"},
            headers=auth_headers,
        )
        check("duplicate member → 409", r, 409)

        # ── Comments ───────────────────────────────────────
        print("\n--- Comments ---")

        # Use first acme task for comments
        cmt_tid = acme_tasks[0]["id"] if acme_tasks else new_task_id
        if cmt_tid:
            # List comments (initially empty)
            r = t_get(base, f"/api/tasks/{cmt_tid}/comments/", acme_id)
            check("list comments (empty)", r, 200)
            if r.status == 200:
                check_true("no comments yet", len(r.json.get("results", [])) == 0)

            # Create comment (authenticated)
            r = t_post(
                base,
                f"/api/tasks/{cmt_tid}/comments/",
                acme_id,
                body={"body": "First comment on this task"},
                headers=auth_headers,
            )
            check("create comment", r, 201)
            comment_id = None
            if r.status == 201:
                comment_id = r.json.get("id")
                check_true("comment has id", comment_id is not None)
                check_true("comment author", r.json.get("author") == "acme_admin")
                check_true(
                    "comment body", r.json.get("body") == "First comment on this task"
                )

            # Create second comment
            r = t_post(
                base,
                f"/api/tasks/{cmt_tid}/comments/",
                acme_id,
                body={"body": "Follow-up comment"},
                headers=auth_headers,
            )
            check("create second comment", r, 201)

            # List comments (should have 2)
            r = t_get(base, f"/api/tasks/{cmt_tid}/comments/", acme_id)
            check("list comments (2)", r, 200)
            if r.status == 200:
                check_true("2 comments", len(r.json.get("results", [])) == 2)

            # Update own comment
            if comment_id:
                r = t_patch(
                    base,
                    f"/api/comments/{comment_id}",
                    acme_id,
                    body={"body": "Updated first comment"},
                    headers=auth_headers,
                )
                check("update own comment", r, 200)
                if r.status == 200:
                    check_true(
                        "updated body", r.json.get("body") == "Updated first comment"
                    )

                # Delete own comment
                h = {"X-Tenant-ID": str(acme_id), "Content-Type": "application/json"}
                h.update(auth_headers)
                r = _http_request(
                    "DELETE", f"{base}/api/comments/{comment_id}", headers=h
                )
                check("delete own comment", r, 200)

            # List comments after delete (should have 1)
            r = t_get(base, f"/api/tasks/{cmt_tid}/comments/", acme_id)
            check("list after delete (1)", r, 200)
            if r.status == 200:
                check_true("1 comment left", len(r.json.get("results", [])) == 1)

            # Create comment without body → 400
            r = t_post(
                base,
                f"/api/tasks/{cmt_tid}/comments/",
                acme_id,
                body={"body": ""},
                headers=auth_headers,
            )
            check("empty comment body → 400", r, 400)

            # Create comment unauthenticated → 401
            r = t_post(
                base,
                f"/api/tasks/{cmt_tid}/comments/",
                acme_id,
                body={"body": "should fail"},
            )
            check("unauth comment → 401", r, 401)

            # Globex cannot see Acme's task comments → 404
            r = t_get(base, f"/api/tasks/{cmt_tid}/comments/", globex_id)
            check("cross-tenant comment list → 404", r, 404)

        # ── Audit Log ──────────────────────────────────────
        print("\n--- Audit Log ---")
        # Admin can view audit log
        r = t_get(base, "/api/audit-log", acme_id, headers=auth_headers)
        check("audit log (admin)", r, 200)
        if r.status == 200:
            entries = r.json.get("results", [])
            check_true("audit entries exist", len(entries) > 0)
            # Should have entries from project/task/member/comment creates
            actions = {e["action"] for e in entries}
            check_true("has create actions", "create" in actions)
            resource_types = {e["resource_type"] for e in entries}
            check_true("has project audit", "project" in resource_types)
            check_true("has task audit", "task" in resource_types)

        # Non-admin cannot view audit log (need to login as member)
        r = t_post(
            base,
            "/auth/login",
            acme_id,
            body={
                "username": "acme_member",
                "password": SEED_PASSWORD,
            },
        )
        member_cookie = ""
        if r.status == 200:
            member_cookie = r.headers.get("set-cookie", "").split(";")[0]
        if member_cookie:
            r = t_get(
                base, "/api/audit-log", acme_id, headers={"Cookie": member_cookie}
            )
            check("audit log (member) → 403", r, 403)

        # ── Tenant Suspension ──────────────────────────────
        print("\n--- Tenant Suspension ---")
        admin_key = {"X-API-Key": "test-api-key"}

        # Suspend Globex via admin API
        r = _http_request(
            "POST",
            f"{base}/api/admin/tenants/{globex_id}/suspend",
            headers=admin_key,
        )
        check("suspend globex", r, 200)

        # Globex requests should now fail with 403
        r = t_get(base, "/api/projects/", globex_id)
        check("suspended tenant → 403", r, 403)

        # Acme should still work
        r = t_get(base, "/api/projects/", acme_id)
        check("active tenant still works", r, 200)

        # Re-activate Globex
        r = _http_request(
            "POST",
            f"{base}/api/admin/tenants/{globex_id}/reactivate",
            headers=admin_key,
        )
        check("reactivate globex", r, 200)

        # Globex should work again
        r = t_get(base, "/api/projects/", globex_id)
        check("reactivated tenant works", r, 200)

        # ── RBAC Security ──────────────────────────────────
        print("\n--- RBAC Security ---")
        # Create a viewer user (admin is already logged in)
        r = t_post(
            base,
            "/api/members/",
            acme_id,
            body={"username": "acme_viewer", "password": "viewer123", "role": "viewer"},
            headers=auth_headers,
        )
        check("create viewer", r, 201)

        # Login as viewer
        r = t_post(
            base,
            "/auth/login",
            acme_id,
            body={
                "username": "acme_viewer",
                "password": "viewer123",
            },
        )
        viewer_cookie = ""
        if r.status == 200:
            viewer_cookie = r.headers.get("set-cookie", "").split(";")[0]
        viewer_headers = {"Cookie": viewer_cookie}

        # Viewer can read projects
        r = t_get(base, "/api/projects/", acme_id, headers=viewer_headers)
        check("viewer can list projects", r, 200)

        # Viewer cannot create projects (requires member role)
        r = t_post(
            base,
            "/api/projects/",
            acme_id,
            body={"name": "Viewer Project"},
            headers=viewer_headers,
        )
        check("viewer create project → 403", r, 403)

        # Viewer cannot create tasks
        r = t_post(
            base,
            "/api/tasks/",
            acme_id,
            body={"project_id": 1, "title": "Viewer Task"},
            headers=viewer_headers,
        )
        check("viewer create task → 403", r, 403)

        # Viewer cannot add members (requires admin role)
        r = t_post(
            base,
            "/api/members/",
            acme_id,
            body={"username": "sneaky", "password": "sneaky123"},
            headers=viewer_headers,
        )
        check("viewer add member → 403", r, 403)

        # Viewer cannot view audit log (requires admin role)
        r = t_get(base, "/api/audit-log", acme_id, headers=viewer_headers)
        check("viewer audit log → 403", r, 403)

        # Member can create but not add members
        if member_cookie:
            member_headers = {"Cookie": member_cookie}
            r = t_post(
                base,
                "/api/members/",
                acme_id,
                body={"username": "sneaky2", "password": "sneaky123"},
                headers=member_headers,
            )
            check("member add member → 403", r, 403)

        # ── HyperAdmin ─────────────────────────────────────────
        print("\n--- HyperAdmin ---")
        r = http_get(f"{base}/admin/login/")
        check("admin login page", r, 200)
        check_true("admin login has form", "username" in r.body)

        r = http_get(f"{base}/admin/")
        check_true(
            "admin requires auth",
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
