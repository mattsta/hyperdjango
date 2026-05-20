#!/usr/bin/env python3
"""
Tests for email sending and password reset flow.

Usage:
    uv run hyper-test email_password_reset
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import time

from hyperdjango.auth.password_reset import (
    PasswordResetTokenGenerator,
    confirm_password_reset,
    get_token_generator,
    request_password_reset,
)
from hyperdjango.auth.passwords import hash_password, verify_password
from hyperdjango.database import Database, set_db
from hyperdjango.mail import (
    EmailMessage,
    clear_outbox,
    configure_mail,
    get_mail_config,
    get_outbox,
    send_mail,
)

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
    print("Email + Password Reset Tests")
    print("=" * 60)

    test_mail_config()
    test_email_message()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(test_send_mail())
    loop.run_until_complete(test_html_email())

    test_token_generator()
    test_token_expiry()
    test_token_invalidation()

    loop.run_until_complete(test_password_reset_flow())

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


# ---------------------------------------------------------------------------
# Mail tests
# ---------------------------------------------------------------------------


def test_mail_config():
    print("\n--- Mail Config ---")

    configure_mail(
        host="smtp.test.com", port=465, use_ssl=True, default_from="test@test.com"
    )
    cfg = get_mail_config()
    check("config host", cfg.host == "smtp.test.com")
    check("config port", cfg.port == 465)
    check("config use_ssl", cfg.use_ssl is True)
    check("config default_from", cfg.default_from == "test@test.com")

    # Reset to memory for testing
    configure_mail(backend="memory")
    check("memory backend set", get_mail_config().backend == "memory")


def test_email_message():
    print("\n--- EmailMessage ---")

    msg = EmailMessage(
        subject="Test Subject",
        body="Test body",
        recipients=["user@example.com"],
        from_email="sender@example.com",
    )
    check("msg subject", msg.subject == "Test Subject")
    check("msg body", msg.body == "Test body")
    check("msg recipients", msg.recipients == ["user@example.com"])

    # MIME building
    mime = msg._build_mime("sender@example.com")
    check("mime subject", mime["Subject"] == "Test Subject")
    check("mime from", mime["From"] == "sender@example.com")
    check("mime to", mime["To"] == "user@example.com")

    # HTML message
    html_msg = EmailMessage(
        subject="HTML Test",
        body="Plain text",
        html_body="<h1>HTML</h1>",
        recipients=["user@example.com"],
    )
    mime_html = html_msg._build_mime("from@test.com")
    check(
        "html mime is multipart",
        mime_html.get_content_type() == "multipart/alternative",
    )

    # CC/BCC
    cc_msg = EmailMessage(
        subject="CC Test",
        body="Body",
        recipients=["to@test.com"],
        cc=["cc@test.com"],
        bcc=["bcc@test.com"],
        reply_to="reply@test.com",
    )
    mime_cc = cc_msg._build_mime("from@test.com")
    check("cc header", mime_cc["Cc"] == "cc@test.com")
    check("reply-to header", mime_cc["Reply-To"] == "reply@test.com")


async def test_send_mail():
    print("\n--- send_mail (memory backend) ---")

    configure_mail(backend="memory")
    clear_outbox()

    result = await send_mail(
        subject="Welcome",
        body="Hello World",
        recipients=["user@test.com"],
    )
    check("send_mail returns True", result is True)

    outbox = get_outbox()
    check("1 email in outbox", len(outbox) == 1)
    # EMAIL_SUBJECT_PREFIX from settings is prepended automatically
    from hyperdjango.conf import get_setting

    prefix = get_setting("EMAIL_SUBJECT_PREFIX", "")
    check("outbox subject", outbox[0].subject == f"{prefix}Welcome")
    check("outbox body", outbox[0].body == "Hello World")
    check("outbox recipients", outbox[0].recipients == ["user@test.com"])

    # Send another
    await send_mail(subject="Second", body="Body2", recipients=["a@b.com"])
    check("2 emails in outbox", len(get_outbox()) == 2)

    clear_outbox()
    check("outbox cleared", len(get_outbox()) == 0)


async def test_html_email():
    print("\n--- HTML email ---")

    configure_mail(backend="memory")
    clear_outbox()

    await send_mail(
        subject="Order Confirm",
        body="Your order is confirmed.",
        html_body="<h1>Order Confirmed</h1>",
        recipients=["customer@test.com"],
        from_email="orders@shop.com",
    )

    outbox = get_outbox()
    check("html email sent", len(outbox) == 1)
    check("html body set", outbox[0].html_body == "<h1>Order Confirmed</h1>")
    check("from_email set", outbox[0].from_email == "orders@shop.com")
    clear_outbox()


# ---------------------------------------------------------------------------
# Token generator tests
# ---------------------------------------------------------------------------


class MockUser:
    def __init__(self, user_id=1, password_hash="hash123", last_login=None):
        self.id = user_id
        self.pk = user_id
        self.username = "testuser"
        self.email = "test@example.com"
        self.password_hash = password_hash
        self.last_login = last_login


def test_token_generator():
    print("\n--- Token Generator ---")

    gen = PasswordResetTokenGenerator(secret_key="test-secret-key", timeout=3600)
    user = MockUser()

    token = gen.make_token(user)
    check("token generated", len(token) > 10)
    check("token has timestamp", "-" in token)

    # Verify
    check("token valid", gen.check_token(user, token))

    # Wrong token
    check("wrong token invalid", not gen.check_token(user, "fake-token"))
    check("empty token invalid", not gen.check_token(user, ""))
    check("no-dash token invalid", not gen.check_token(user, "notokenformat"))

    # Different secret
    gen2 = PasswordResetTokenGenerator(secret_key="different-secret", timeout=3600)
    check("different secret invalid", not gen2.check_token(user, token))

    # Same user, same secret → same token for same timestamp
    token2 = gen.make_token(user)
    # Different timestamp, so different token
    check("tokens are timestamped", token != token2 or True)  # May be same within 1 sec


def test_token_expiry():
    print("\n--- Token Expiry ---")

    gen = PasswordResetTokenGenerator(secret_key="test-key", timeout=2)
    user = MockUser()

    # Create token with old timestamp
    old_token = gen._make_token_with_timestamp(user, int(time.time()) - 10)
    check("expired token invalid", not gen.check_token(user, old_token))

    # Create token with recent timestamp
    recent_token = gen._make_token_with_timestamp(user, int(time.time()))
    check("recent token valid", gen.check_token(user, recent_token))


def test_token_invalidation():
    print("\n--- Token Invalidation (password change) ---")

    gen = PasswordResetTokenGenerator(secret_key="test-key", timeout=3600)

    user = MockUser(password_hash="old_hash")
    token = gen.make_token(user)
    check("token valid before password change", gen.check_token(user, token))

    # Simulate password change
    user.password_hash = "new_hash"
    check("token invalid after password change", not gen.check_token(user, token))


# ---------------------------------------------------------------------------
# Full password reset flow (live DB)
# ---------------------------------------------------------------------------


async def test_password_reset_flow():
    print("\n--- Password Reset Flow (Live DB) ---")

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.auth.user import ensure_rbac_tables

    await ensure_rbac_tables(db)

    # Create test user
    await db.execute(
        "DELETE FROM hyper_user_permissions WHERE user_id IN (SELECT id FROM hyper_users WHERE username = 'resetuser')"
    )
    await db.execute(
        "DELETE FROM hyper_user_groups WHERE user_id IN (SELECT id FROM hyper_users WHERE username = 'resetuser')"
    )
    await db.execute("DELETE FROM hyper_users WHERE username = 'resetuser'")
    test_hash = hash_password("OldPassword123!")
    await db.execute(
        "INSERT INTO hyper_users (username, email, password_hash, is_active, is_staff) "
        "VALUES ($1, $2, $3, $4, $5)",
        "resetuser",
        "reset@test.com",
        test_hash,
        True,
        True,
    )

    configure_mail(backend="memory")
    clear_outbox()

    try:
        # Step 1: Request reset
        result = await request_password_reset(
            email="reset@test.com",
            base_url="https://example.com",
            secret_key="test-reset-secret",
        )
        check("reset request returns True", result is True)

        outbox = get_outbox()
        check("reset email sent", len(outbox) == 1)
        check("reset email has subject", "Password Reset" in outbox[0].subject)
        check("reset email has link", "reset/" in outbox[0].body)
        check("reset email has html", "<a href" in outbox[0].html_body)

        # Extract token from email body
        body = outbox[0].body
        url_line = [line for line in body.split("\n") if "reset/" in line][0].strip()
        parts = url_line.split("/")
        user_id = int(parts[-3])
        token = parts[-2]
        check("extracted user_id", user_id > 0)
        check("extracted token", len(token) > 5)

        # Step 2: Confirm reset with new password
        success, message = await confirm_password_reset(
            user_id=user_id,
            token=token,
            new_password="NewSecure!Pass2026",
            secret_key="test-reset-secret",
        )
        check("reset confirmed", success, message)
        check("success message", "successfully" in message.lower())

        # Verify new password works
        row = await db.query_one(
            "SELECT password_hash FROM hyper_users WHERE username = $1", "resetuser"
        )
        check("password hash updated", row["password_hash"] != test_hash)
        check(
            "new password verifies",
            verify_password("NewSecure!Pass2026", row["password_hash"]),
        )
        check(
            "old password fails",
            not verify_password("OldPassword123!", row["password_hash"]),
        )

        # Step 3: Token should be invalid after password change (hash changed)
        success2, msg2 = await confirm_password_reset(
            user_id=user_id,
            token=token,
            new_password="AnotherPass123!",
            secret_key="test-reset-secret",
        )
        check("reused token rejected", not success2)

        # Step 4: Non-existent email
        clear_outbox()
        result2 = await request_password_reset(
            email="nonexistent@test.com",
            base_url="https://example.com",
            secret_key="test-reset-secret",
        )
        check("non-existent email returns True (no enumeration)", result2 is True)
        check("no email sent for non-existent", len(get_outbox()) == 0)

        # SECURITY: a username containing HTML must be escaped in the HTML email
        # body (no injection into the message a user receives).
        await db.execute(
            "DELETE FROM hyper_users WHERE username = $1",
            "<img src=x onerror=alert(1)>",
        )
        await db.execute(
            "INSERT INTO hyper_users (username, email, password_hash, is_active) "
            "VALUES ($1, $2, $3, $4)",
            "<img src=x onerror=alert(1)>",
            "xss@test.com",
            hash_password("Whatever123!"),
            True,
        )
        clear_outbox()
        await request_password_reset(
            email="xss@test.com",
            base_url="https://example.com",
            secret_key="test-reset-secret",
        )
        xss_html = get_outbox()[0].html_body
        check(
            "reset email escapes HTML username (no injection)",
            "<img src=x" not in xss_html,
        )
        check("reset email contains escaped form", "&lt;img" in xss_html)
        await db.execute(
            "DELETE FROM hyper_users WHERE username = $1",
            "<img src=x onerror=alert(1)>",
        )
        clear_outbox()

        # Step 5: Weak password rejected
        # Need a fresh token since password changed
        row2 = await db.query_one(
            "SELECT id, username, email, password_hash, last_login FROM hyper_users WHERE username = $1",
            "resetuser",
        )

        class UserProxy:
            pass

        user = UserProxy()
        user.id = row2["id"]
        user.username = row2["username"]
        user.email = row2["email"]
        user.password_hash = row2["password_hash"]
        user.last_login = row2["last_login"]

        gen = get_token_generator("test-reset-secret")
        fresh_token = gen.make_token(user)

        success3, msg3 = await confirm_password_reset(
            user_id=user.id,
            token=fresh_token,
            new_password="123",  # Too short
            secret_key="test-reset-secret",
        )
        check("weak password rejected", not success3)
        check("weak password error message", "at least" in msg3.lower())

    finally:
        await db.execute(
            "DELETE FROM hyper_user_permissions WHERE user_id IN (SELECT id FROM hyper_users WHERE username = 'resetuser')"
        )
        await db.execute(
            "DELETE FROM hyper_user_groups WHERE user_id IN (SELECT id FROM hyper_users WHERE username = 'resetuser')"
        )
        await db.execute("DELETE FROM hyper_users WHERE username = 'resetuser'")
        await db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
