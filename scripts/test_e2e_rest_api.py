"""
End-to-end tests for rest_api service.

Tests all REST endpoints against a live Zig HTTP server + PostgreSQL.
Requires: blog database with tables (run hyper setup first).
"""

# hyper-test: e2e

import json
import subprocess

from e2e_helper import (
    TEST_PORTS,
    AppRunner,
    E2EResponse,
    _http_request,
    http_get,
    http_post,
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
        print(f"        body: {response.body[:200]}")
    return False


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


def http_put(url, body=None, headers=None):
    return _http_request("PUT", url, body=body, headers=headers)


def http_delete(url, headers=None):
    return _http_request("DELETE", url, headers=headers)


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("REST API E2E Tests")
    print("=" * 60)

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.rest_api.app:app",
            "--drop",
            "--seed",
            "services.rest_api.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.rest_api.app:app", host="127.0.0.1", port=TEST_PORTS["rest_api"]
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health ───────────────────────────────────────────────────
        print("--- Health ---")
        r = http_get(f"{base}/health")
        check("/health GET", r, 200)

        # ── OpenAPI docs ─────────────────────────────────────────────
        print("\n--- OpenAPI ---")
        r = http_get(f"{base}/docs")
        check("/docs GET (Swagger UI)", r, 200)

        r = http_get(f"{base}/openapi.json")
        if check("/openapi.json GET", r, 200):
            data = r.json
            assert "paths" in data, "OpenAPI spec missing 'paths'"
            PASS += 1
            print(f"  PASS  OpenAPI spec has {len(data['paths'])} paths")

        # ── Registration ─────────────────────────────────────────────
        print("\n--- Auth ---")
        import time

        ts = str(int(time.time()))
        r = http_post(
            f"{base}/auth/register",
            body={
                "username": f"testuser{ts}",
                "email": f"test{ts}@example.com",
                "password": "securepass123",
            },
        )
        if r.status in (200, 201):
            PASS += 1
            print(f"  PASS  /auth/register POST ({r.status})")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /auth/register POST: got {r.status}")
            print(f"  FAIL  /auth/register POST: got {r.status}")
            if r.body:
                print(f"        body: {r.body[:200]}")

        # ── Login ────────────────────────────────────────────────────
        r = http_post(
            f"{base}/auth/login",
            body={
                "username": f"testuser{ts}",
                "password": "securepass123",
            },
        )
        session_cookie = r.headers.get("set-cookie", "")
        if r.status in (200, 201):
            PASS += 1
            print(f"  PASS  /auth/login POST ({r.status})")
            if session_cookie:
                print(f"  INFO  Session cookie: {session_cookie[:60]}...")
            else:
                print("  WARN  No Set-Cookie from login response")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /auth/login POST: got {r.status}")
            print(f"  FAIL  /auth/login POST: got {r.status}")
            if r.body:
                print(f"        body: {r.body[:200]}")

        # ── Public posts (empty initially) ───────────────────────────
        print("\n--- Posts (public) ---")
        r = http_get(f"{base}/api/posts")
        check("/api/posts GET", r, 200)

        # ── Create post (requires auth) ──────────────────────────────
        print("\n--- Posts (authenticated) ---")
        auth_headers = {"Cookie": session_cookie} if session_cookie else {}

        r = http_post(
            f"{base}/api/posts",
            body={
                "title": f"Test Post {ts}",
                "body": "This is a test post body.",
            },
            headers=auth_headers,
        )
        if r.status in (200, 201):
            PASS += 1
            print(f"  PASS  /api/posts POST (create) ({r.status})")
            post_data = r.json
            post_id = post_data.get("id", 1)
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /api/posts POST: got {r.status}")
            print(f"  FAIL  /api/posts POST: got {r.status}")
            if r.body:
                print(f"        body: {r.body[:200]}")
            post_id = 1

        # ── Get single post ──────────────────────────────────────────
        r = http_get(f"{base}/api/posts/{post_id}")
        check(f"/api/posts/{post_id} GET", r, 200)

        # ── Update post ──────────────────────────────────────────────
        r = http_put(
            f"{base}/api/posts/{post_id}",
            body={
                "title": f"Updated Post {ts}",
                "body": "Updated body content.",
            },
            headers=auth_headers,
        )
        if r.status in (200, 204):
            PASS += 1
            print(f"  PASS  /api/posts/{post_id} PUT ({r.status})")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /api/posts/{post_id} PUT: got {r.status}")
            print(f"  FAIL  /api/posts/{post_id} PUT: got {r.status}")
            if r.body:
                print(f"        body: {r.body[:200]}")

        # ── Delete post ──────────────────────────────────────────────
        r = http_delete(f"{base}/api/posts/{post_id}", headers=auth_headers)
        if r.status in (200, 204):
            PASS += 1
            print(f"  PASS  /api/posts/{post_id} DELETE ({r.status})")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /api/posts/{post_id} DELETE: got {r.status}")
            print(f"  FAIL  /api/posts/{post_id} DELETE: got {r.status}")
            if r.body:
                print(f"        body: {r.body[:200]}")

        # ── Auth failures ────────────────────────────────────────────
        print("\n--- Auth enforcement ---")
        r = http_post(f"{base}/api/posts", body={"title": "x", "body": "x"})
        if r.status in (401, 403):
            PASS += 1
            print(f"  PASS  Unauthenticated POST rejected ({r.status})")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  Unauth POST: expected 401/403, got {r.status}")
            print(f"  FAIL  Unauth POST: expected 401/403, got {r.status}")

        # ── Admin endpoints (API key auth) ───────────────────────────
        print("\n--- Admin (API key) ---")
        admin_headers = {"X-API-Key": "sk_live_demo_key_123"}

        r = http_get(f"{base}/api/admin/stats", headers=admin_headers)
        check("/api/admin/stats GET", r, 200)

        r = http_get(f"{base}/api/admin/users", headers=admin_headers)
        check("/api/admin/users GET", r, 200)

        # Invalid API key
        r = http_get(f"{base}/api/admin/stats", headers={"X-API-Key": "bad-key"})
        if r.status in (401, 403):
            PASS += 1
            print(f"  PASS  Invalid API key rejected ({r.status})")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  Invalid key: expected 401/403, got {r.status}")
            print(f"  FAIL  Invalid key: expected 401/403, got {r.status}")

        # ── Security Audit Log ────────────────────────────────────────
        print("\n--- Security Audit Log ---")

        # Trigger a failed login to generate a security event
        http_post(
            f"{base}/auth/login",
            body=json.dumps({"username": f"testuser{ts}", "password": "wrongpassword"}),
        )

        # Query recent security events
        r = http_get(f"{base}/api/security/recent")
        check("/api/security/recent GET", r, 200)
        if r.status == 200:
            data = r.json
            ok("recent has events list", "events" in data)
            ok("recent has count", "count" in data)
            events = data.get("events", [])
            ok("has security events", len(events) > 0, f"got {len(events)}")
            if events:
                # Should have both LOGIN_SUCCESS (from earlier) and LOGIN_FAILED
                event_types = {e.get("event") for e in events}
                ok(
                    "has login_failed event",
                    "login_failed" in event_types,
                    f"types={event_types}",
                )
                ok(
                    "has login_success event",
                    "login_success" in event_types,
                    f"types={event_types}",
                )
                # Verify event structure
                first = events[0]
                ok("event has ip_address", "ip_address" in first)
                ok("event has timestamp", "timestamp" in first)

        # Query failed logins specifically
        r = http_get(f"{base}/api/security/failed-logins")
        check("/api/security/failed-logins GET", r, 200)
        if r.status == 200:
            data = r.json
            ok("failed-logins has count", data.get("count", 0) >= 1)
            failed = data.get("events", [])
            if failed:
                ok(
                    "failed login has detail",
                    "detail" in failed[0] and failed[0]["detail"],
                )

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
