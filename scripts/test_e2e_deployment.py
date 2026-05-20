"""
End-to-end tests for Deployment service.

Tests that the production reference application works correctly:
- Health and readiness probes
- Authenticated CRUD
- Cursor-paginated listing
- Environment-driven configuration
"""

# hyper-test: e2e

import subprocess
import sys
import time

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


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Deployment Example E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["deployment"]

    # Setup
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.deployment.app:app",
            "--drop",
            "--seed",
            "services.deployment.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.deployment.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health Probes ───────────────────────────────────────
        print("--- Health Probes ---")
        r = http_get(f"{base}/health")
        check("liveness probe", r, 200)
        if r.status == 200:
            check_true("health status ok", r.json.get("status") == "ok")

        r = http_get(f"{base}/ready")
        check("readiness probe", r, 200)
        if r.status == 200:
            data = r.json
            check_true("ready status ok", data.get("status") == "ok")
            check_true("ready has checks", "checks" in data)

        # ── List Items (cursor-paginated) ───────────────────────
        print("\n--- Items API ---")
        r = http_get(f"{base}/api/items/")
        check("list items", r, 200)
        if r.status == 200:
            data = r.json
            check_true("paginated response", "results" in data)
            results = data.get("results", [])
            check_true("has seeded items", len(results) > 0)
            if results:
                check_true("item has id", "id" in results[0])
                check_true("item has name", "name" in results[0])
                check_true("item has status", "status" in results[0])

        # ── Unauthenticated Create → 401 ───────────────────────
        print("\n--- Auth ---")
        r = http_post(f"{base}/api/items/", body={"name": "Unauthed Item"})
        check("create without auth → 401", r, 401)

        # Login
        s = Session(base)
        r = s.post("/auth/login", body={"username": "admin", "password": SEED_PASSWORD})
        check("login", r, 200)

        # Authenticated Create
        ts = str(int(time.time()) % 100000)
        r = s.post("/api/items/", body={"name": f"E2E Item {ts}"})
        check("create item (authenticated)", r, 201)
        if r.status == 201:
            check_true("created item has id", "id" in r.json)

        # Create without name → 400
        r = s.post("/api/items/", body={"name": ""})
        check("create without name → 400", r, 400)

        # ── Bad Login ───────────────────────────────────────────
        r = http_post(
            f"{base}/auth/login", body={"username": "admin", "password": "wrong"}
        )
        check("bad login → 401", r, 401)

        r = http_post(f"{base}/auth/login", body={})
        check("empty login → 400", r, 400)

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
