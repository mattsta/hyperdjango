"""
Visual validation of service template rendering and UI.

# hyper-test: e2e

Tests that template inheritance, CSS styling, and navigation actually render
correctly in all services with HTML templates.

This catches bugs like the threadlocal template_loader issue where
{% extends "base.html" %} was silently ignored, producing unstyled pages.

Usage:
    uv run hyper-test visual_apps
"""

import subprocess
import sys

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


def _setup_app(module_app: str, seed_module: str | None = None) -> None:
    """Run `hyper setup --drop` for an app, optionally with seed."""
    args = ["uv", "run", "hyper", "setup", "--app", module_app, "--drop"]
    if seed_module:
        args.extend(["--seed", seed_module])
    subprocess.run(args, capture_output=True, timeout=60)


def _check_html_structure(name: str, html: str, checks: dict[str, str]) -> None:
    """Validate that HTML contains expected structural elements.

    checks: {check_name: substring_that_must_be_present}
    """
    for check_name, substring in checks.items():
        ok(
            f"{name}: {check_name}",
            substring in html,
            f"missing '{substring}' in first 200 chars: {html[:200]!r}",
        )


# ---------------------------------------------------------------------------
# full_stack — the "start here" app
# ---------------------------------------------------------------------------


def test_full_stack():
    print("\n--- full_stack: Template Inheritance + CSS ---")
    port = TEST_PORTS["full_stack"]
    _setup_app("services.full_stack.app:app", "services.full_stack.seed:run")

    with AppRunner(
        "services.full_stack.app:app", port=port, readiness_path="/health"
    ) as runner:
        s = Session(runner.url())

        # Login page — must have base template (nav, style, DOCTYPE)
        r = s.get("/login")
        ok("login 200", r.status == 200)
        _check_html_structure(
            "login",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE html>",
                "has <style>": "<style>",
                "has <nav>": "<nav",
                "has nav brand": "Task Manager",
                "has Login heading": "<h2>Login</h2>",
                "has form": 'name="username"',
                "has Register link": "/register",
            },
        )

        # Register page
        r = s.get("/register")
        ok("register 200", r.status == 200)
        _check_html_structure(
            "register",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE html>",
                "has <nav>": "<nav",
                "has form": 'name="username"',
            },
        )

        # Login and check dashboard
        r = s.post(
            "/register",
            body="username=viz_test&password=pass1234&password2=pass1234",
            content_type="application/x-www-form-urlencoded",
        )
        r = s.post(
            "/login",
            body="username=viz_test&password=pass1234",
            content_type="application/x-www-form-urlencoded",
        )

        r = s.get("/")
        ok("dashboard 200", r.status == 200)
        _check_html_structure(
            "dashboard",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE html>",
                "has <nav>": "<nav",
                "has Logout": "Logout",
            },
        )


# ---------------------------------------------------------------------------
# forms_demo — form validation + error display
# ---------------------------------------------------------------------------


def test_forms_demo():
    print("\n--- forms_demo: Forms + Templates ---")
    port = TEST_PORTS["forms_demo"]
    _setup_app("services.forms_demo.app:app", "services.forms_demo.seed:run")

    with AppRunner(
        "services.forms_demo.app:app", port=port, readiness_path="/health"
    ) as runner:
        s = Session(runner.url())

        r = s.get("/")
        ok("home 200", r.status == 200)
        _check_html_structure(
            "home",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE html>",
                "has <style> or <link>": "<style",  # inline or linked CSS
            },
        )

        r = s.get("/contact")
        ok("contact 200", r.status == 200)
        _check_html_structure(
            "contact",
            r.body,
            {
                "has form": "<form",
                "has name field": "name",
                "has email field": "email",
            },
        )


# ---------------------------------------------------------------------------
# hypernews — most complex template hierarchy
# ---------------------------------------------------------------------------


def test_hypernews():
    print("\n--- hypernews: Complex Template Hierarchy ---")
    port = TEST_PORTS["hypernews"]
    _setup_app("services.hypernews.app:app", "services.hypernews.seed:run")

    with AppRunner(
        "services.hypernews.app:app", port=port, readiness_path="/health"
    ) as runner:
        s = Session(runner.url())

        r = s.get("/")
        ok("home 200", r.status == 200)
        _check_html_structure(
            "home",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE html>",
                "has <html": "<html",
                "has </head>": "</head>",
            },
        )

        r = s.get("/login")
        ok("login 200", r.status == 200)
        _check_html_structure(
            "login",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE html>",
                "has form": "<form",
            },
        )


# ---------------------------------------------------------------------------
# content_hub — admin panel + templates
# ---------------------------------------------------------------------------


def test_content_hub():
    print("\n--- content_hub: Admin + Templates ---")
    port = TEST_PORTS["content_hub"]
    _setup_app("services.content_hub.app:app", "services.content_hub.seed:run")

    with AppRunner(
        "services.content_hub.app:app", port=port, readiness_path="/health"
    ) as runner:
        # content_hub is an API + admin app — no HTML root route
        r = http_get(f"{runner.url()}/admin/login/")
        ok("admin login 200", r.status == 200)
        _check_html_structure(
            "admin login",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE",
                "has form": "<form",
            },
        )


# ---------------------------------------------------------------------------
# notes_api — intermediate example with admin
# ---------------------------------------------------------------------------


def test_notes_api():
    print("\n--- notes_api: Auth + Admin ---")
    port = TEST_PORTS["notes_api"]
    _setup_app("services.notes_api.app:app", "services.notes_api.seed:run")

    with AppRunner(
        "services.notes_api.app:app", port=port, readiness_path="/health"
    ) as runner:
        # JSON API — no template, but admin has templates
        r = http_get(f"{runner.url()}/admin/login/")
        ok("admin login 200", r.status == 200)
        _check_html_structure(
            "admin login",
            r.body,
            {
                "has DOCTYPE": "<!DOCTYPE",
                "has form": "<form",
            },
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Visual App Validation — Template Rendering + UI")
    print("=" * 60)

    test_full_stack()
    test_forms_demo()
    test_hypernews()
    test_content_hub()
    test_notes_api()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
