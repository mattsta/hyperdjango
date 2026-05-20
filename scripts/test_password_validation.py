#!/usr/bin/env python3
"""
Tests for password validators and createsuperuser CLI.

Usage:
    uv run hyper-test password_validation
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.auth.passwords import hash_password, verify_password
from hyperdjango.auth.validators import (
    CommonPasswordValidator,
    MaxLengthValidator,
    MinLengthValidator,
    NumericValidator,
    PasswordValidationError,
    UserAttributeSimilarityValidator,
    get_default_validators,
    get_password_help_texts,
    validate_password,
    validate_password_or_raise,
)
from hyperdjango.database import Database, set_db

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("Password Validation + createsuperuser CLI Tests")
    print("=" * 60)

    # ── MinLengthValidator ───────────────────────────────────────────
    print("\n--- MinLengthValidator ---")

    v = MinLengthValidator(min_length=8)
    check("short password rejected", v.validate("abc") is not None)
    check("7 chars rejected", v.validate("abcdefg") is not None)
    check("8 chars accepted", v.validate("abcdefgh") is None)
    check("long password accepted", v.validate("a" * 50) is None)
    check("empty rejected", v.validate("") is not None)
    check("has help text", "8 characters" in v.get_help_text())

    v2 = MinLengthValidator(min_length=12)
    check("custom min_length=12 rejects 10", v2.validate("a" * 10) is not None)
    check("custom min_length=12 accepts 12", v2.validate("a" * 12) is None)

    # ── MaxLengthValidator ───────────────────────────────────────────
    print("\n--- MaxLengthValidator ---")

    v = MaxLengthValidator(max_length=128)
    check("normal password accepted", v.validate("goodpassword123") is None)
    check("128 chars accepted", v.validate("a" * 128) is None)
    check("129 chars rejected", v.validate("a" * 129) is not None)
    check("very long rejected", v.validate("a" * 1000) is not None)

    # ── NumericValidator ─────────────────────────────────────────────
    print("\n--- NumericValidator ---")

    v = NumericValidator()
    check("all digits rejected", v.validate("123456789") is not None)
    check("mixed chars accepted", v.validate("abc123def") is None)
    check("alphanumeric accepted", v.validate("a1b2c3d4") is None)
    check("no digits accepted", v.validate("abcdefgh") is None)
    check("single digit rejected", v.validate("1") is not None)
    check("has help text", "numeric" in v.get_help_text())

    # ── CommonPasswordValidator ──────────────────────────────────────
    print("\n--- CommonPasswordValidator ---")

    v = CommonPasswordValidator()
    check("'password' rejected", v.validate("password") is not None)
    check("'123456' rejected", v.validate("123456") is not None)
    check("'qwerty' rejected", v.validate("qwerty") is not None)
    check("'letmein' rejected", v.validate("letmein") is not None)
    check("'iloveyou' rejected", v.validate("iloveyou") is not None)
    check("'admin' rejected", v.validate("admin") is not None)
    check("'Password' case insensitive rejected", v.validate("Password") is not None)
    check("unique password accepted", v.validate("xK9$mPq2vR7!nL4j") is None)
    check("random password accepted", v.validate("hyperdjango2026!") is None)
    check("has help text", "common" in v.get_help_text().lower())

    # ── UserAttributeSimilarityValidator ──────────────────────────────
    print("\n--- UserAttributeSimilarityValidator ---")

    v = UserAttributeSimilarityValidator()

    # Real SessionUser (not a bare stand-in) so attribute reads go through the
    # actual user contract.
    from hyperdjango.auth.user import AnonymousUser, SessionUser

    user = SessionUser(
        {
            "username": "johndoe",
            "email": "john@example.com",
            "first_name": "John",
            "last_name": "Doe",
        }
    )
    check("username as password rejected", v.validate("johndoe", user) is not None)
    check("username variant rejected", v.validate("johndoe123", user) is not None)
    check(
        "email as password rejected", v.validate("john@example.com", user) is not None
    )
    check("unrelated password accepted", v.validate("xK9$mPq2vR7!", user) is None)
    check("no user always accepted", v.validate("anything", user=None) is None)
    check("first_name as password rejected", v.validate("john", user) is not None)
    # Anonymous user (falsy __bool__, empty attributes) must be accepted like the
    # None sentinel — the validator must not treat the anon user as similar.
    check(
        "anonymous user always accepted",
        v.validate("anything", AnonymousUser()) is None,
    )
    check("has help text", "similar" in v.get_help_text().lower())

    # ── validate_password (full chain) ───────────────────────────────
    print("\n--- validate_password (full chain) ---")

    errors = validate_password("password")
    check("'password' fails validation", len(errors) > 0)
    check("'password' has common error", any("common" in e.lower() for e in errors))

    errors = validate_password("12345678")
    check("'12345678' fails (numeric + common)", len(errors) >= 2)

    errors = validate_password("abc")
    check("'abc' fails (too short + common)", len(errors) >= 1)

    errors = validate_password("xK9$mPq2vR7!nL4j")
    check("strong password passes all", len(errors) == 0, f"errors={errors}")

    errors = validate_password("a" * 200)
    check("very long password fails max length", len(errors) > 0)

    # ── validate_password_or_raise ───────────────────────────────────
    print("\n--- validate_password_or_raise ---")

    try:
        validate_password_or_raise("password")
        check("weak password raises", False, "should have raised")
    except PasswordValidationError as e:
        check("weak password raises", True)
        check("error has messages list", len(e.messages) > 0)

    try:
        validate_password_or_raise("xK9$mPq2vR7!nL4j")
        check("strong password doesn't raise", True)
    except PasswordValidationError:
        check("strong password doesn't raise", False)

    # ── get_password_help_texts ──────────────────────────────────────
    print("\n--- get_password_help_texts ---")

    texts = get_password_help_texts()
    check("has help texts", len(texts) >= 4)
    check("includes length help", any("8 characters" in t for t in texts))
    check("includes numeric help", any("numeric" in t.lower() for t in texts))
    check("includes common help", any("common" in t.lower() for t in texts))
    check("includes similarity help", any("similar" in t.lower() for t in texts))

    # ── get_default_validators ───────────────────────────────────────
    print("\n--- get_default_validators ---")

    validators = get_default_validators()
    check("has 5 default validators", len(validators) == 5)
    check("first is MinLength", isinstance(validators[0], MinLengthValidator))
    check("second is MaxLength", isinstance(validators[1], MaxLengthValidator))
    check("third is Numeric", isinstance(validators[2], NumericValidator))
    check("fourth is Common", isinstance(validators[3], CommonPasswordValidator))
    check(
        "fifth is Similarity",
        isinstance(validators[4], UserAttributeSimilarityValidator),
    )

    # ── Integration: validate + hash + verify ────────────────────────
    print("\n--- Integration: validate → hash → verify ---")

    good_password = "MyStr0ng!Pass2026"
    errors = validate_password(good_password)
    check("good password validates", len(errors) == 0, f"errors={errors}")
    hashed = hash_password(good_password)
    check("password hashed", hashed.startswith("$argon2id$"))
    check("password verifies", verify_password(good_password, hashed))
    check("wrong password fails", not verify_password("wrong", hashed))

    # ── createsuperuser CLI module import ─────────────────────────────
    print("\n--- createsuperuser CLI ---")

    from hyperdjango.cli import cmd_createsuperuser

    check("cmd_createsuperuser importable", callable(cmd_createsuperuser))

    # ── createsuperuser non-interactive (live DB) ─────────────────────
    print("\n--- createsuperuser non-interactive (live DB) ---")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(test_createsuperuser_noinput())

    # Summary
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


async def test_createsuperuser_noinput():
    """Test creating a superuser by calling the DB logic directly.

    We can't use cmd_createsuperuser() since it calls asyncio.run() which
    can't nest. Instead, we test the same DB operations the CLI performs.
    """
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.auth.passwords import hash_password as _hash
    from hyperdjango.auth.user import ensure_rbac_tables

    # Ensure table exists
    await ensure_rbac_tables(db)

    # Clean up any existing test user
    await db.execute("DELETE FROM hyper_users WHERE username = 'cli_test_admin'")

    try:
        # Simulate what cmd_createsuperuser does
        username = "cli_test_admin"
        email = "cli_admin@test.com"
        password = "TestCli!Pass2026"
        password_hash = _hash(password)

        await db.execute(
            "INSERT INTO hyper_users (username, email, password_hash, is_active, is_staff, is_superuser) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            username,
            email,
            password_hash,
            True,
            True,
            True,
        )

        # Verify user was created
        row = await db.query_one(
            "SELECT username, email, is_staff, is_superuser, password_hash FROM hyper_users WHERE username = $1",
            "cli_test_admin",
        )
        check("superuser created in DB", row is not None)
        if row:
            check("username correct", row["username"] == "cli_test_admin")
            check("email correct", row["email"] == "cli_admin@test.com")
            check("is_staff is true", row["is_staff"] is True)
            check("is_superuser is true", row["is_superuser"] is True)
            check(
                "password hash is argon2id",
                row["password_hash"].startswith("$argon2id$"),
            )
            check(
                "password verifies",
                verify_password("TestCli!Pass2026", row["password_hash"]),
            )

    finally:
        await db.execute("DELETE FROM hyper_users WHERE username = 'cli_test_admin'")
        await db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
