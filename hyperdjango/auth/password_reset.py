"""
Password reset flow for HyperApp standalone auth.

Generates HMAC-signed time-limited tokens for password reset links.
Provides request/confirm endpoints.

Usage:
    from hyperdjango.auth.password_reset import (
        PasswordResetTokenGenerator,
        request_password_reset,
        confirm_password_reset,
    )

    # Generate a reset token for a user
    generator = PasswordResetTokenGenerator(secret_key="your-secret")
    token = generator.make_token(user)

    # Verify a token
    is_valid = generator.check_token(user, token)

    # Full flow via helper functions:
    # 1. User requests reset → sends email with link
    await request_password_reset(email="user@example.com", base_url="https://example.com")

    # 2. User clicks link → confirms with new password
    success = await confirm_password_reset(user_id=1, token="abc123", new_password="newpass")
"""

import hmac
import html as _html
import time

from hyperdjango.auth.passwords import hash_password
from hyperdjango.auth.validators import validate_password
from hyperdjango.conf import get_setting
from hyperdjango.database import get_db
from hyperdjango.mail import send_mail
from hyperdjango.native._crypto import hmac_sha256_hex
from hyperdjango.types import PasswordResetUser


class PasswordResetTokenGenerator:
    """Generate and verify HMAC-signed password reset tokens.

    Tokens include a timestamp and are valid for a configurable duration.
    The token is invalidated if the user changes their password (by including
    the password hash in the HMAC input).
    """

    # Placeholder secrets that must never sign real reset tokens. A token signed
    # with a well-known/empty key is trivially forgeable by anyone who reads the
    # source, letting an attacker mint a valid reset link for any account.
    _FORBIDDEN_SECRETS = frozenset({"", "change-me", "changeme", "secret", "default"})

    def __init__(self, secret_key: str, timeout: int | None = None):
        """
        Args:
            secret_key: HMAC secret key. Must be a real, non-placeholder secret —
                        an empty string or a well-known placeholder like
                        "change-me" is rejected (raises ValueError) because such
                        keys make reset tokens forgeable.
            timeout: Token validity in seconds. If None, uses
                     PASSWORD_RESET_TIMEOUT from conf settings.
        """
        if not secret_key or secret_key.strip().lower() in self._FORBIDDEN_SECRETS:
            raise ValueError(
                "PasswordResetTokenGenerator requires a real secret_key. "
                "Set SECRET_KEY (or pass secret_key=) to a strong random value; "
                "the placeholder/empty default is refused because it makes reset "
                "tokens forgeable."
            )
        self.secret_key = secret_key
        self.timeout = (
            timeout if timeout is not None else get_setting("PASSWORD_RESET_TIMEOUT")
        )

    def make_token(self, user: PasswordResetUser) -> str:
        """Generate a password reset token for a user.

        The token includes:
        - User ID
        - Current password hash (so token is invalidated on password change)
        - Timestamp

        Returns a string like "timestamp-signature".
        """
        timestamp = int(time.time())
        return self._make_token_with_timestamp(user, timestamp)

    def check_token(self, user: PasswordResetUser, token: str) -> bool:
        """Verify a password reset token.

        Returns True if the token is valid (correct signature, not expired,
        user password hasn't changed since token was issued).
        """
        if not token or "-" not in token:
            return False

        try:
            ts_str, signature = token.split("-", 1)
            timestamp = int(ts_str)
        except ValueError, TypeError:
            return False

        # Check expiration
        if time.time() - timestamp > self.timeout:
            return False

        # Regenerate and compare
        expected = self._make_token_with_timestamp(user, timestamp)
        return hmac.compare_digest(token, expected)

    def _make_token_with_timestamp(
        self, user: PasswordResetUser, timestamp: int
    ) -> str:
        """Generate token for a specific timestamp."""
        # PasswordResetUser is a Protocol declaring id/password_hash/last_login;
        # every caller passes a conforming object (User model or the DB-backed
        # _UserProxy), so these are direct field reads, not reflection.
        user_id = user.id
        password_hash = user.password_hash
        last_login = str(user.last_login)

        # HMAC input: user_id + password_hash + last_login + timestamp
        value = f"{user_id}:{password_hash}:{last_login}:{timestamp}"
        signature = hmac_sha256_hex(self.secret_key.encode(), value.encode())

        return f"{timestamp}-{signature}"


# ---------------------------------------------------------------------------
# High-level flow helpers
# ---------------------------------------------------------------------------

_default_generator: PasswordResetTokenGenerator | None = None


def _resolve_secret_key(secret_key: str | None) -> str:
    """Resolve the reset-token secret, falling back to the SECRET_KEY setting.

    ``None`` means "use the configured SECRET_KEY". The resulting key is still
    validated by PasswordResetTokenGenerator, so an unset/placeholder SECRET_KEY
    raises rather than silently signing forgeable tokens.
    """
    if secret_key is None:
        return get_setting("SECRET_KEY")
    return secret_key


def get_token_generator(
    secret_key: str | None = None,
) -> PasswordResetTokenGenerator:
    """Get or create the default token generator.

    ``secret_key=None`` resolves to the configured ``SECRET_KEY``. A placeholder
    or empty secret is rejected by the generator (raises ValueError).
    """
    global _default_generator
    resolved = _resolve_secret_key(secret_key)
    if _default_generator is None or _default_generator.secret_key != resolved:
        _default_generator = PasswordResetTokenGenerator(secret_key=resolved)
    return _default_generator


async def request_password_reset(
    email: str, base_url: str, secret_key: str | None = None, from_email: str = ""
) -> bool:
    """Request a password reset for an email address.

    Looks up the user by email, generates a token, and sends a reset email.
    Returns True if the email was sent (even if user not found, to prevent
    email enumeration).

    Args:
        email: User's email address.
        base_url: Base URL for the reset link (e.g., "https://example.com").
        secret_key: HMAC secret key for token generation.
        from_email: Sender email address.
    """
    db = get_db()

    # Look up user by email
    row = await db.query_one(
        "SELECT id, username, email, password_hash, last_login FROM hyper_users WHERE email = $1 AND is_active = $2",
        email,
        True,
    )

    if row is None:
        # Don't reveal that the email doesn't exist — return True anyway
        return True

    # Build a minimal user-like object for token generation
    class _UserProxy:
        pass

    user = _UserProxy()
    user.id = row["id"]
    user.username = row["username"]
    user.email = row["email"]
    user.password_hash = row["password_hash"]
    user.last_login = row["last_login"]

    # Generate token
    generator = get_token_generator(secret_key)
    token = generator.make_token(user)

    # Build reset URL
    reset_url = f"{base_url.rstrip('/')}/reset/{user.id}/{token}/"

    # Send email
    subject = "Password Reset Request"
    body = (
        f"Hello {user.username},\n\n"
        f"You requested a password reset. Click the link below:\n\n"
        f"{reset_url}\n\n"
        f"This link expires in {generator.timeout // 60} minutes.\n\n"
        f"If you didn't request this, ignore this email.\n"
    )
    # HTML-escape everything interpolated into the HTML email body. username is
    # user-controlled (a `<img onerror=…>` username would otherwise inject into
    # the message), and reset_url is escaped for defense-in-depth.
    safe_username = _html.escape(str(user.username))
    safe_reset_url = _html.escape(reset_url, quote=True)
    html_body = (
        f"<p>Hello {safe_username},</p>"
        f"<p>You requested a password reset. Click the link below:</p>"
        f'<p><a href="{safe_reset_url}">{safe_reset_url}</a></p>'
        f"<p>This link expires in {generator.timeout // 60} minutes.</p>"
        f"<p>If you didn't request this, ignore this email.</p>"
    )

    await send_mail(
        subject=subject,
        body=body,
        recipients=[user.email],
        from_email=from_email,
        html_body=html_body,
    )
    return True


async def confirm_password_reset(
    user_id: int, token: str, new_password: str, secret_key: str | None = None
) -> tuple[bool, str]:
    """Confirm a password reset with a new password.

    Validates the token and password, then updates the user's password hash.

    Returns:
        (success: bool, message: str)
    """
    db = get_db()

    # Look up user
    row = await db.query_one(
        "SELECT id, username, email, password_hash, last_login FROM hyper_users WHERE id = $1",
        user_id,
    )

    if row is None:
        return False, "Invalid reset link."

    # Build user proxy
    class _UserProxy:
        pass

    user = _UserProxy()
    user.id = row["id"]
    user.username = row["username"]
    user.email = row["email"]
    user.password_hash = row["password_hash"]
    user.last_login = row["last_login"]

    # Verify token
    generator = get_token_generator(secret_key)
    if not generator.check_token(user, token):
        return False, "Invalid or expired reset link."

    # Validate new password
    errors = validate_password(new_password, user)
    if errors:
        return False, "; ".join(errors)

    # Update password
    new_hash = hash_password(new_password)
    await db.execute(
        "UPDATE hyper_users SET password_hash = $1 WHERE id = $2",
        new_hash,
        user_id,
    )

    return True, "Password reset successfully."
