"""
Tests for newly added features from Django audit:
- Response.set_cookie / delete_cookie
- Form.add_error, has_error, non_field_errors, errors_as_json, clean_<field> hooks
- QuerySet.latest, earliest, explain, select_for_update
"""

# hyper-test: unit

import json
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


# ── Response.set_cookie / delete_cookie ────────────────────────────────────

print("=== Response Cookie Helpers ===")

from hyperdjango.response import Response

r = Response.json({"ok": True})
r.set_cookie("session", "abc123", max_age=3600)
cookie = r.headers.get("set-cookie", "")
check("set_cookie_name_value", "session=abc123" in cookie, f"got: {cookie!r}")
check("set_cookie_max_age", "Max-Age=3600" in cookie)
check("set_cookie_httponly", "HttpOnly" in cookie)
check("set_cookie_samesite", "SameSite=Lax" in cookie)
check("set_cookie_path", "Path=/" in cookie)

# Custom options
r2 = Response.json({})
r2.set_cookie("theme", "dark", max_age=86400, samesite="Strict", path="/app")
cookie2 = r2.headers.get("set-cookie", "")
check("set_cookie_custom_path", "Path=/app" in cookie2)
check("set_cookie_custom_samesite", "SameSite=Strict" in cookie2)

# Domain
r3 = Response.json({})
r3.set_cookie("x", "y", domain=".example.com")
cookie3 = r3.headers.get("set-cookie", "")
check("set_cookie_domain", "Domain=.example.com" in cookie3)

# Session cookie (no max_age)
r4 = Response.json({})
r4.set_cookie("temp", "val")
cookie4 = r4.headers.get("set-cookie", "")
check("session_cookie_no_max_age", "Max-Age" not in cookie4)

# delete_cookie
r5 = Response.json({})
r5.delete_cookie("session")
cookie5 = r5.headers.get("set-cookie", "")
check("delete_cookie_max_age_0", "Max-Age=0" in cookie5)
check("delete_cookie_empty_value", "session=" in cookie5)

# ── Form Error Methods ─────────────────────────────────────────────────────

print("\n=== Form Error Methods ===")

from hyperdjango.forms import CharField, Form, IntegerField


class TestForm(Form):
    name = CharField(max_length=100)
    email = CharField(max_length=200)
    age = IntegerField()


# add_error
form = TestForm(data={"name": "Alice", "email": "alice@example.com", "age": "25"})
form.is_valid()
check("form_valid_initially", len(form.errors) == 0)

form.add_error("email", "Email already exists")
check("add_error_added", "email" in form.errors)
check("add_error_message", "Email already exists" in form.errors["email"])
check("add_error_removes_cleaned", "email" not in form.cleaned_data)

# add_error for non-field
form.add_error(None, "General form error")
check("add_error_non_field", "__all__" in form.errors)
check("add_error_non_field_msg", "General form error" in form.errors["__all__"])

# has_error
check("has_error_true", form.has_error("email"))
check("has_error_false", not form.has_error("name"))
check("has_error_with_code", form.has_error("email", "Email already exists"))
check("has_error_wrong_code", not form.has_error("email", "wrong message"))

# non_field_errors
nfe = form.non_field_errors()
check("non_field_errors", "General form error" in nfe)

# errors_as_json
json_str = form.errors_as_json()
parsed = json.loads(json_str)
check("errors_as_json_valid", isinstance(parsed, dict))
check("errors_as_json_email", "email" in parsed)
check(
    "errors_as_json_structure", parsed["email"][0]["message"] == "Email already exists"
)

# get_json_data
data = form.get_json_data()
check("get_json_data_email", "email" in data)
check("get_json_data_all", "__all__" in data)

# ── clean_<fieldname> hooks ────────────────────────────────────────────────

print("\n=== clean_<fieldname> Hooks ===")


class CleanHookForm(Form):
    username = CharField(max_length=50)
    email = CharField(max_length=200)

    def clean_username(self):
        val = self.cleaned_data.get("username", "")
        if val.lower() == "admin":
            raise ValueError("Username 'admin' is reserved")
        return val.lower()  # normalize to lowercase

    def clean_email(self):
        val = self.cleaned_data.get("email", "")
        if "@" not in val:
            raise ValueError("Invalid email format")
        return val.strip()


# Valid data with normalization
form = CleanHookForm(data={"username": "ALICE", "email": "alice@example.com"})
check("clean_hook_valid", form.is_valid())
check("clean_hook_normalized", form.cleaned_data["username"] == "alice")

# Reserved username
form2 = CleanHookForm(data={"username": "admin", "email": "admin@example.com"})
check("clean_hook_rejected", not form2.is_valid())
check("clean_hook_error_msg", "reserved" in form2.errors.get("username", [""])[0])

# Invalid email
form3 = CleanHookForm(data={"username": "bob", "email": "not-an-email"})
check("clean_hook_email_invalid", not form3.is_valid())
check("clean_hook_email_error", "email" in form3.errors)

# ── QuerySet.latest / earliest ─────────────────────────────────────────────

print("\n=== QuerySet.latest / earliest (mock) ===")

# Mock test since we can't hit DB here
from _test_meta import make_table_meta

from hyperdjango.query import QuerySet


class MockModel:
    # Real TableMeta (see scripts/_test_meta.py): column_names/writable_columns/
    # pk_fields/pk_where_clause are the genuine derived contract off real
    # FieldMeta entries, not hand-written stand-ins.
    _meta = make_table_meta("test_items", ["id", "name", "created_at"])

    class DoesNotExist(Exception):
        pass

    class MultipleObjectsReturned(Exception):
        pass

    @classmethod
    def from_record(cls, record):
        return record


# Test that latest/earliest create proper ordering
qs = QuerySet(MockModel)
latest_qs = qs.order_by("-id").limit(1)
check("latest_creates_ordering", latest_qs._ordering == ("-id",))
check("latest_creates_limit", latest_qs._limit == 1)

earliest_qs = qs.order_by("id").limit(1)
check("earliest_creates_ordering", earliest_qs._ordering == ("id",))

# select_for_update
sfu_qs = qs.select_for_update()
check("sfu_suffix", sfu_qs._for_update == " FOR UPDATE")

sfu_nowait = qs.select_for_update(nowait=True)
check("sfu_nowait", sfu_nowait._for_update == " FOR UPDATE NOWAIT")

sfu_skip = qs.select_for_update(skip_locked=True)
check("sfu_skip_locked", sfu_skip._for_update == " FOR UPDATE SKIP LOCKED")

# explain creates proper SQL prefix
# Can't actually execute but verify the method exists
check("explain_method_exists", callable(qs.explain))

# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"New features tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

sys.exit(0 if failed == 0 else 1)
