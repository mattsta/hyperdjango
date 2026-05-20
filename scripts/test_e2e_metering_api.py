"""
Metering API E2E tests — usage metering, quotas, IETF rate limit headers.

Tests auth, completions (metered), usage reports, quota status,
IETF RateLimit headers on every response, admin.

# hyper-test: e2e
"""

import json
import subprocess
import sys

from e2e_helper import (
    SEED_PASSWORD,
    TEST_PORTS,
    AppRunner,
    Session,
    http_get,
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


def main():
    print("=" * 60)
    print("Metering API E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["metering_api"]

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.metering_api.app:app",
            "--drop",
            "--seed",
            "services.metering_api.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.metering_api.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health ──
        print("--- Health ---")
        r = http_get(f"{base}/health")
        ok("health endpoint", r.status == 200)

        # ── IETF Rate Limit Headers on Health ──
        print("\n--- IETF Rate Limit Headers ---")
        ok("health: ratelimit-policy header", "ratelimit-policy" in r.headers)
        ok("health: ratelimit header", "ratelimit" in r.headers)

        policy_hdr = r.headers.get("ratelimit-policy", "")
        ok("policy: has api-minute", '"api-minute"' in policy_hdr)
        ok("policy: has q=60", ";q=60" in policy_hdr)
        ok("policy: has w=60", ";w=60" in policy_hdr)

        rl_hdr = r.headers.get("ratelimit", "")
        ok("ratelimit: has api-minute", '"api-minute"' in rl_hdr)
        ok("ratelimit: has ;r=", ";r=" in rl_hdr)

        # Legacy headers also present
        ok("legacy: x-ratelimit-limit", r.headers.get("x-ratelimit-limit") == "60")
        ok("legacy: x-ratelimit-remaining", "x-ratelimit-remaining" in r.headers)

        # ── Root redirect ──
        print("\n--- Root ---")
        r = http_get(f"{base}/")
        ok("root redirects", r.status in (200, 301, 302))

        # ── Auth: Login ──
        print("\n--- Auth ---")
        s = Session(base)
        s.get("/health")  # warm up cookies

        r = s.post(
            "/auth/login",
            body=json.dumps({"email": "free@example.com", "password": SEED_PASSWORD}),
            content_type="application/json",
        )
        ok("login 200", r.status == 200)
        body = r.json
        ok("login has id", "id" in body)
        ok("login tier=free", body.get("tier") == "free")

        # ── Completions (metered) ──
        print("\n--- Completions ---")
        r = s.post(
            "/api/v1/completions",
            body=json.dumps({"prompt": "Write a haiku about web frameworks"}),
            content_type="application/json",
        )
        ok("completion 200", r.status == 200, f"got {r.status}: {r.body[:300]}")
        body = r.json
        ok("completion has text", "text" in body)
        ok("completion has usage", "usage" in body)
        usage = body.get("usage", {})
        ok("usage has tokens_in", "tokens_in" in usage)
        ok("usage has tokens_out", "tokens_out" in usage)
        ok("usage has duration_ms", "duration_ms" in usage)

        # IETF headers on completion response
        ok("completion: ratelimit-policy", "ratelimit-policy" in r.headers)
        ok("completion: ratelimit", "ratelimit" in r.headers)

        # ── Completions without auth ──
        r = http_get(f"{base}/api/v1/completions")
        # POST required, GET should fail
        ok("completion GET → 404 or 405", r.status in (404, 405))

        # ── Usage Report ──
        print("\n--- Usage Report ---")
        r = s.get("/api/v1/usage")
        ok("usage 200", r.status == 200, f"got {r.status}: {r.body[:300]}")
        body = r.json
        ok("usage has account_id", "account_id" in body)
        ok("usage has period", body.get("period") == "monthly")
        ok("usage has usage dict", "usage" in body)

        # ── Quota Status ──
        print("\n--- Quota Status ---")
        r = s.get("/api/v1/usage/quota")
        ok("quota 200", r.status == 200)
        body = r.json
        ok("quota has tier", body.get("tier") == "free")
        ok("quota has monthly_limit", body.get("monthly_limit") == 10000)
        ok("quota has remaining", "remaining" in body)
        ok("quota has percent_used", "percent_used" in body)
        ok("quota has status", "status" in body)

        # ── Usage/Quota without auth ──
        print("\n--- Auth enforcement ---")
        r = http_get(f"{base}/api/v1/usage")
        ok("usage unauthed → 401", r.status == 401)
        r = http_get(f"{base}/api/v1/usage/quota")
        ok("quota unauthed → 401", r.status == 401)

        # ── Completions without auth ──
        from e2e_helper import http_post

        r = http_post(
            f"{base}/api/v1/completions",
            body=json.dumps({"prompt": "test"}),
            headers={"Content-Type": "application/json"},
        )
        ok("completion unauthed → 401", r.status == 401)

        # ── Logout ──
        print("\n--- Logout ---")
        r = s.post("/auth/logout")
        ok("logout 200", r.status == 200)

        # ── Swagger UI ──
        print("\n--- Swagger UI ---")
        r = http_get(f"{base}/docs/")
        ok("swagger 200", r.status == 200)

        # ── Admin ──
        print("\n--- Admin ---")
        r = http_get(f"{base}/admin/")
        ok("admin redirects to login", r.status in (200, 302))

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Metering API: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("FAILURES:")
        for e in ERRORS:
            print(f"  {e}")
    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
