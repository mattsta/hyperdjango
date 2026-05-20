"""
Tests for system checks, security decorators, and query instrumentation.
"""

# hyper-test: unit

import asyncio
import sys

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        errors.append(msg)
        print(msg)


loop = asyncio.new_event_loop()

# ── System Checks ──────────────────────────────────────────────────────────

print("=== System Checks ===")

from hyperdjango.checks import (
    CheckMessage,
    _registry,
    get_check_count,
    register,
    run_checks,
)

# CheckMessage
msg = CheckMessage(level="error", msg="Test error", hint="Fix it", id="test.E001")
check("check_message_str", "Test error" in str(msg))
check("check_message_id", "test.E001" in str(msg))
check("check_message_hint", "Fix it" in str(msg))
check("check_message_serious", msg.is_serious)

info_msg = CheckMessage(level="info", msg="Info")
check("check_message_not_serious", not info_msg.is_serious)


# Register custom check
@register("test")
def check_always_passes(app):
    return []


@register("test")
def check_always_warns(app):
    return [CheckMessage(level="warning", msg="Test warning", id="test.W001")]


check("custom_check_registered", "test" in _registry)
check("custom_check_count", len(_registry["test"]) >= 2)


# Run checks
class FakeApp:
    _middleware = None
    _db = None


messages = run_checks(FakeApp())
check("run_checks_returns_list", isinstance(messages, list))
check("run_checks_has_messages", len(messages) > 0)

# Check built-in security checks ran
has_secret_warning = any("SECRET_KEY" in m.msg for m in messages)
check("builtin_secret_check", has_secret_warning)

# Count by level
counts = get_check_count(messages)
check("count_has_levels", "warning" in counts)

# Deployment checks excluded by default
deploy_msgs = [m for m in messages if m.id.startswith("deployment")]
check("deployment_excluded_default", len(deploy_msgs) == 0)

# Include deployment checks
all_msgs = run_checks(FakeApp(), include_deployment=True)
deploy_msgs = [m for m in all_msgs if m.id.startswith("deployment")]
check("deployment_included", len(deploy_msgs) > 0)

# ── Security Decorators ────────────────────────────────────────────────────

print("\n=== Security Decorators ===")

from hyperdjango.response import Response
from hyperdjango.shortcuts import (
    never_cache,
    sensitive_post_parameters,
    sensitive_variables,
    vary_on_cookie,
    vary_on_headers,
    xframe_options_deny,
    xframe_options_exempt,
    xframe_options_sameorigin,
)


class FakeRequest:
    method = "GET"


@xframe_options_deny
async def deny_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(deny_view(FakeRequest()))
check("xframe_deny", r.headers.get("X-Frame-Options") == "DENY")


@xframe_options_sameorigin
async def sameorigin_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(sameorigin_view(FakeRequest()))
check("xframe_sameorigin", r.headers.get("X-Frame-Options") == "SAMEORIGIN")


@xframe_options_exempt
async def exempt_view(request):
    resp = Response.json({"ok": True})
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


r = loop.run_until_complete(exempt_view(FakeRequest()))
check(
    "xframe_exempt",
    "X-Frame-Options" not in r.headers and "x-frame-options" not in r.headers,
)


@sensitive_variables("password", "token")
async def sensitive_view(request):
    return Response.json({"ok": True})


check(
    "sensitive_vars_attr", sensitive_view._sensitive_variables == ("password", "token")
)


@sensitive_post_parameters("password")
async def sensitive_post_view(request):
    return Response.json({"ok": True})


check(
    "sensitive_post_attr",
    sensitive_post_view._sensitive_post_parameters == ("password",),
)


@never_cache
async def no_cache_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(no_cache_view(FakeRequest()))
cc = r.headers.get("Cache-Control", "")
check("never_cache_no_store", "no-store" in cc)
check("never_cache_no_cache", "no-cache" in cc)
check("never_cache_private", "private" in cc)
check("never_cache_expires", r.headers.get("Expires") == "0")


@vary_on_headers("Accept-Language", "Cookie")
async def vary_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(vary_view(FakeRequest()))
vary = r.headers.get("Vary", "")
check("vary_headers", "Accept-Language" in vary and "Cookie" in vary)


@vary_on_cookie
async def vary_cookie_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(vary_cookie_view(FakeRequest()))
check("vary_cookie", "Cookie" in r.headers.get("Vary", ""))

# ── Query Instrumentation ──────────────────────────────────────────────────

print("\n=== Query Instrumentation ===")

from hyperdjango.database import Database

db = Database.__new__(Database)
db._execute_wrappers = []

# Test wrapper registration
check("no_wrappers_initially", len(db._execute_wrappers) == 0)


def my_wrapper(execute, sql, params):
    return execute(sql, params)


with db.execute_wrapper(my_wrapper):
    check("wrapper_registered", len(db._execute_wrappers) == 1)
    check("wrapper_is_ours", db._execute_wrappers[0] is my_wrapper)

check("wrapper_removed", len(db._execute_wrappers) == 0)


# Test nested wrappers
def wrapper1(execute, sql, params):
    return execute(sql, params)


def wrapper2(execute, sql, params):
    return execute(sql, params)


with db.execute_wrapper(wrapper1):
    check("nested_1", len(db._execute_wrappers) == 1)
    with db.execute_wrapper(wrapper2):
        check("nested_2", len(db._execute_wrappers) == 2)
    check("nested_back_to_1", len(db._execute_wrappers) == 1)

check("nested_back_to_0", len(db._execute_wrappers) == 0)

# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"Checks & decorators tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

loop.close()
sys.exit(0 if failed == 0 else 1)
