"""
End-to-end tests for Hello World service.

Tests that the minimal app starts and responds correctly:
- JSON root endpoint
- Path parameter greeting
"""

# hyper-test: e2e

import sys

from e2e_helper import (
    TEST_PORTS,
    AppRunner,
    http_get,
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
    print("Hello World Example E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["hello"]

    with AppRunner(
        "services.hello.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Root endpoint ──────────────────────────────────────
        print("--- Root endpoint ---")
        r = http_get(f"{base}/")
        check("GET / returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "Root has message",
                data.get("message") == "Hello from HyperDjango!",
                f"got {data.get('message')}",
            )

        # ── Greet endpoint ─────────────────────────────────────
        print("\n--- Greet endpoint ---")
        r = http_get(f"{base}/greet/World")
        check("GET /greet/World returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "Greeting correct",
                data.get("greeting") == "Hello, World!",
                f"got {data.get('greeting')}",
            )

        r = http_get(f"{base}/greet/HyperDjango")
        check("GET /greet/HyperDjango returns 200", r, 200)
        if r.status == 200:
            data = r.json
            check_true(
                "Custom greeting correct",
                data.get("greeting") == "Hello, HyperDjango!",
                f"got {data.get('greeting')}",
            )

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
