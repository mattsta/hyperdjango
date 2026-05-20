"""Seed utilities for generating dynamic credentials.

NEVER hardcode static passwords in seed files. Use these helpers to
generate random credentials when no explicit value is provided via
the settings system (Django settings / HYPER_* env / .env file).

Usage in seed.py::

    from hyperdjango.auth import seed_password, hash_password

    password = seed_password("admin")
    user = User(username="admin", password_hash=hash_password(password))
"""

import secrets

from hyperdjango.conf import get_setting
from hyperdjango.logging import logger


def seed_password(username: str, *, setting_name: str = "") -> str:
    """Get or generate a seed password for a user.

    Resolution order:
    1. ``HYPER_SEED_PASSWORD_<USERNAME>`` setting (via get_setting)
    2. ``HYPER_SEED_PASSWORD`` setting (global fallback for all seed users)
    3. Random ``secrets.token_urlsafe(16)`` — printed to stdout

    Args:
        username: The username this password is for (used in setting name + log)
        setting_name: Override the setting name instead of deriving from username

    Returns:
        The password as a plain string (caller must hash_password() it).
    """
    # Per-user setting: HYPER_SEED_PASSWORD_ADMIN, HYPER_SEED_PASSWORD_ALICE, etc.
    if not setting_name:
        setting_name = f"SEED_PASSWORD_{username.upper()}"
    password = get_setting(setting_name)
    if password:
        return password

    # Global fallback: HYPER_SEED_PASSWORD (same password for all seed users)
    password = get_setting("SEED_PASSWORD")
    if password:
        return password

    # Generate random — print so operator can record it
    password = secrets.token_urlsafe(16)
    logger.warning(
        "Generated seed password for '{username}': {password}  "
        "(set HYPER_SEED_PASSWORD or HYPER_{setting} to control this)",
        username=username,
        password=password,
        setting=setting_name,
    )
    return password
