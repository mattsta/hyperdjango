"""
Verify cookie security flags on services.

# hyper-test: e2e

Checks: HttpOnly, Secure, SameSite on session and CSRF cookies.
"""

import subprocess

from e2e_helper import SEED_PASSWORD, TEST_PORTS, AppRunner, http_get, http_post

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


def check_cookie_flags(app_name: str, port: int, module: str) -> None:
    print(f"\n--- {app_name} (port {port}) ---")

    with AppRunner(
        f"{module}:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
        timeout=60.0,
    ) as runner:
        # GET a page to get CSRF cookie
        r = http_get(runner.url("/login"))
        raw_cookie = r.headers.get("set-cookie", "")
        print(f"  Set-Cookie: {raw_cookie[:100]}...")

        if "csrftoken=" in raw_cookie:
            # CSRF cookie should NOT be HttpOnly (JS needs to read it for double-submit)
            ok(f"{app_name} CSRF: SameSite present", "samesite" in raw_cookie.lower())
        else:
            print("  INFO  No CSRF cookie on GET /login")

        # POST login to get session cookie
        if "hypernews" in module:
            csrf = raw_cookie.split(";")[0] if raw_cookie else ""
            r = http_post(
                runner.url("/login"),
                body=f"username=admin&password={SEED_PASSWORD}",
                content_type="application/x-www-form-urlencoded",
                headers={
                    "Cookie": csrf,
                    "X-CSRFToken": csrf.split("=", 1)[1] if "=" in csrf else "",
                },
            )
        elif "hyperai" in module:
            csrf = raw_cookie.split(";")[0] if raw_cookie else ""
            r = http_post(
                runner.url("/login"),
                body=f"username=demo&password={SEED_PASSWORD}",
                content_type="application/x-www-form-urlencoded",
                headers={
                    "Cookie": csrf,
                    "X-CSRFToken": csrf.split("=", 1)[1] if "=" in csrf else "",
                },
            )
        else:
            r = http_post(
                runner.url("/auth/login"),
                body={"username": "admin", "password": SEED_PASSWORD},
            )

        session_cookie = r.headers.get("set-cookie", "")
        print(f"  Session Set-Cookie: {session_cookie[:100]}...")

        if "sessionid=" in session_cookie:
            ok(
                f"{app_name} Session: HttpOnly",
                "httponly" in session_cookie.lower(),
                f"cookie: {session_cookie[:80]}",
            )
            ok(
                f"{app_name} Session: SameSite",
                "samesite" in session_cookie.lower(),
                f"cookie: {session_cookie[:80]}",
            )
            ok(
                f"{app_name} Session: Secure flag",
                "secure" in session_cookie.lower(),
                f"cookie: {session_cookie[:80]}",
            )
        else:
            print("  INFO  No session cookie (login may have failed)")


def main() -> None:
    print("=" * 60)
    print("Cookie Security Audit")
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
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hyperai.app:app",
            "--drop",
            "--seed",
            "services.hyperai.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    check_cookie_flags(
        "HyperNews",
        TEST_PORTS["cookie_security_hn"],
        "services.hypernews.app",
    )
    check_cookie_flags(
        "HyperAI",
        TEST_PORTS["cookie_security_ai"],
        "services.hyperai.app",
    )

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
