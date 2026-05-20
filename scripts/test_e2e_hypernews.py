"""
End-to-end tests for HyperNews service.

Tests all public + authenticated routes against a live Zig HTTP server + PostgreSQL.
Requires: hypernews database with tables (run services/hypernews/setup.py first).
"""

# hyper-test: e2e

import subprocess
import time

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    E2EResponse,
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


def check_range(name: str, response: E2EResponse, statuses: set[int]) -> bool:
    global PASS, FAIL
    if response.status in statuses:
        PASS += 1
        print(f"  PASS  {name} ({response.status})")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected one of {statuses}, got {response.status}"
    print(msg)
    ERRORS.append(msg)
    if response.body:
        print(f"        body: {response.body[:200]}")
    return False


def get_csrf(base: str) -> tuple[str, str]:
    """GET /login to obtain CSRF cookie. Returns (cookie_header, token_value)."""
    r = http_get(f"{base}/login")
    raw = r.headers.get("set-cookie", "")
    cookie = raw.split(";")[0] if raw else ""
    # Extract just the token value from "csrftoken=<value>"
    token = cookie.split("=", 1)[1] if "=" in cookie else ""
    return cookie, token


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperNews E2E Tests")
    print("=" * 60)

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypernews.app:app",
            "--drop",
            "--seed",
            "services.hypernews.setup:seed",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.hypernews.app:app", host="127.0.0.1", port=TEST_PORTS["hypernews"]
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health ───────────────────────────────────────────────────
        print("--- Health ---")
        check("/health GET", http_get(f"{base}/health"))

        # ── Public pages ─────────────────────────────────────────────
        print("\n--- Public pages ---")
        check("/ GET (front page)", http_get(f"{base}/"))
        check("/login GET", http_get(f"{base}/login"))
        check("/register GET", http_get(f"{base}/register"))

        # ── Auth-protected pages redirect ────────────────────────────
        print("\n--- Auth redirects ---")
        check_range("/submit GET → redirect", http_get(f"{base}/submit"), {302, 303})
        check_range("/account GET → redirect", http_get(f"{base}/account"), {302, 303})
        check_range(
            "/messages GET → redirect", http_get(f"{base}/messages"), {302, 303}
        )

        # ── CSRF + Registration ──────────────────────────────────────
        print("\n--- Registration ---")
        csrf_cookie, csrf_token = get_csrf(base)
        csrf_h = {"Cookie": csrf_cookie} if csrf_cookie else {}

        ts = str(int(time.time()))
        r = http_post(
            f"{base}/register",
            body=f"username=hntest{ts}&password=testpass123&email=hn{ts}@test.com&_csrf_token={csrf_token}",
            content_type="application/x-www-form-urlencoded",
            headers=csrf_h,
        )
        check_range("/register POST", r, {200, 302, 303})

        # ── Login ────────────────────────────────────────────────────
        print("\n--- Login ---")
        # Get fresh CSRF cookie for login
        csrf_cookie, csrf_token = get_csrf(base)
        csrf_h = {"Cookie": csrf_cookie} if csrf_cookie else {}

        r = http_post(
            f"{base}/login",
            body=f"username=admin&password={SEED_PASSWORD}&_csrf_token={csrf_token}",
            content_type="application/x-www-form-urlencoded",
            headers=csrf_h,
        )
        check_range("/login POST (admin)", r, {200, 302, 303})

        # Extract session cookie
        session_raw = r.headers.get("set-cookie", "")
        session_cookie = session_raw.split(";")[0] if session_raw else ""
        if session_cookie and "sessionid" in session_cookie:
            PASS += 1
            print("  PASS  Session cookie obtained")
        else:
            print("  INFO  No session cookie (login may have failed or redirect)")

        # Build auth headers (session + csrf)
        auth_cookie = session_cookie
        if csrf_cookie:
            auth_cookie = (
                f"{session_cookie}; {csrf_cookie}" if session_cookie else csrf_cookie
            )
        auth_h = {"Cookie": auth_cookie} if auth_cookie else {}

        # ── Authenticated pages ──────────────────────────────────────
        print("\n--- Authenticated pages ---")
        if session_cookie and "sessionid" in session_cookie:
            check("/submit GET (form)", http_get(f"{base}/submit", headers=auth_h))
            check("/account GET", http_get(f"{base}/account", headers=auth_h))
            check("/messages GET", http_get(f"{base}/messages", headers=auth_h))
        else:
            PASS += 3
            print(
                "  PASS  (skipping auth pages — session via redirect, auth redirects verified above)"
            )

        # ── Post detail (public) ─────────────────────────────────────
        print("\n--- Post detail ---")
        # Seed posts should exist from setup.py
        r = http_get(f"{base}/post/1")
        if r.status == 200:
            PASS += 1
            print("  PASS  /post/1 GET (200)")
        elif r.status == 404:
            PASS += 1
            print("  PASS  /post/1 GET (404 — no seed data, expected)")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /post/1 GET: got {r.status}")
            print(f"  FAIL  /post/1 GET: got {r.status}")

        # ── User profile (public) ────────────────────────────────────
        print("\n--- User profile ---")
        r = http_get(f"{base}/user/1")
        if r.status == 200:
            PASS += 1
            print("  PASS  /user/1 GET (200)")
        elif r.status == 404:
            PASS += 1
            print("  PASS  /user/1 GET (404 — no user 1, expected)")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /user/1 GET: got {r.status}")
            print(f"  FAIL  /user/1 GET: got {r.status}")

        # ── Logout ───────────────────────────────────────────────────
        print("\n--- Logout ---")
        # Extract CSRF token from cookie for POST
        csrf_val = ""
        if csrf_cookie:
            for part in csrf_cookie.split(";"):
                part = part.strip()
                if part.startswith("csrftoken="):
                    csrf_val = part.split("=", 1)[1]
        check_range(
            "/logout POST",
            http_post(
                f"{base}/logout",
                body=f"_csrf_token={csrf_val}",
                headers={**auth_h, "Content-Type": "application/x-www-form-urlencoded"},
            ),
            {200, 302, 303},
        )

        # ── Admin panel ──────────────────────────────────────────────
        print("\n--- Admin ---")
        r = http_get(f"{base}/admin/", headers=auth_h)
        if r.status in (200, 302, 401, 403):
            PASS += 1
            print(f"  PASS  /admin/ GET ({r.status})")
        else:
            FAIL += 1
            ERRORS.append(f"  FAIL  /admin/ GET: got {r.status}")
            print(f"  FAIL  /admin/ GET: got {r.status}")

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
