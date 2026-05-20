"""
End-to-end tests for Benchmark App example.

Tests that all benchmark routes respond correctly:
- JSON endpoint
- Plaintext endpoint
- Path parameter route
- Health check
- Echo query params
- POST body echo
"""

# hyper-test: e2e

import sys

from e2e_helper import (
    TEST_PORTS,
    AppRunner,
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


def check_true(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: condition was False"
    if detail:
        msg += f" — {detail}"
    print(msg)
    ERRORS.append(msg)
    return False


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Benchmark App Example E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["benchmark_app"]

    with AppRunner(
        "services.benchmark_app.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── JSON endpoint ──────────────────────────────────────
        print("--- JSON endpoint ---")
        r = http_get(f"{base}/json")
        check("GET /json returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "JSON has message",
                data.get("message") == "Hello, World!",
                f"got {data.get('message')}",
            )

        # ── Plaintext endpoint ─────────────────────────────────
        print("\n--- Plaintext endpoint ---")
        r = http_get(f"{base}/plaintext")
        check("GET /plaintext returns 200", r, 200)
        if r.status == 200:
            check_true(
                "Plaintext body correct",
                r.body == "Hello, World!",
                f"got {r.body!r}",
            )
            check_true(
                "Content-Type is text/plain",
                "text/plain" in r.headers.get("content-type", ""),
                f"got {r.headers.get('content-type')}",
            )

        # ── Path parameter route ───────────────────────────────
        print("\n--- User endpoint ---")
        r = http_get(f"{base}/users/42")
        check("GET /users/42 returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true("User id is 42", data.get("id") == 42, f"got {data.get('id')}")
            check_true("User has name", data.get("name") == "User 42")
            check_true("User is active", data.get("active") is True)

        r = http_get(f"{base}/users/1")
        check("GET /users/1 returns 200", r, 200)
        if r.status == 200:
            check_true("User id is 1", r.json.get("id") == 1)

        # ── Health check ───────────────────────────────────────
        print("\n--- Health check ---")
        r = http_get(f"{base}/health")
        check("GET /health returns 200", r, 200)
        if r.status == 200:
            check_true("Health status ok", r.json.get("status") == "ok")

        # ── Echo endpoint ──────────────────────────────────────
        print("\n--- Echo endpoint ---")
        r = http_get(f"{base}/echo?foo=bar&baz=qux")
        check("GET /echo with params returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true("Echo has foo", "foo" in data, f"got {data}")
            check_true("Echo has baz", "baz" in data, f"got {data}")

        # ── POST body echo ─────────────────────────────────────
        print("\n--- Body echo ---")
        payload = {"name": "test", "value": 123}
        r = http_post(f"{base}/body", body=payload)
        check("POST /body returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true("Body echo name", data.get("name") == "test")
            check_true("Body echo value", data.get("value") == 123)

        # ── 404 for unknown routes ─────────────────────────────
        print("\n--- Error handling ---")
        r = http_get(f"{base}/unknown")
        check("Unknown route returns 404", r, 404)

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
