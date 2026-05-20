"""
Shared type definitions for the HyperDjango platform.

Named type aliases for consistent, readable, reusable types across the
platform. Even when the underlying type is dict[str, Any], the alias
name conveys semantic meaning — SessionData tells you what it IS.

Uses PEP 695 `type` syntax.
"""

from __future__ import annotations

import types as _types
from typing import Any, Protocol, runtime_checkable

# ── Recursive JSON type ──────────────────────────────────────────────────

type JSONValue = (
    str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]
)

# ── Auth / Session ───────────────────────────────────────────────────────

type UserID = int | str
type SessionData = dict[str, Any]  # session store payload — keys vary per app
type CookieKwargs = dict[str, Any]  # kwargs for response.set_cookie()

# ── Rule config ──────────────────────────────────────────────────────────

type RuleConfigDict = dict[
    str, Any
]  # raw rule config from JSON for unknown/custom rule types

# ── Permission objects ───────────────────────────────────────────────────

type PermissionObjDict = dict[str, Any]  # DB row dict used in permission checks

# ── Logging ──────────────────────────────────────────────────────────────

type LogExtra = dict[str, Any]  # user-provided extra fields bound to log records

# ── Traceback ────────────────────────────────────────────────────────────

TracebackType = _types.TracebackType | None


# ── Protocols ────────────────────────────────────────────────────────────


@runtime_checkable
class PasswordResetUser(Protocol):
    """User object in password reset flows."""

    id: int | None
    password_hash: str
    last_login: str | None


class ValidatableUser(Protocol):
    """User object for password similarity validation."""

    username: str
    email: str
    first_name: str
    last_name: str


class PasswordValidator(Protocol):
    """Password validator instance."""

    def validate(
        self, password: str, user: ValidatableUser | None = None
    ) -> str | None: ...
    def get_help_text(self) -> str: ...


__all__ = [
    "CookieKwargs",
    "JSONValue",
    "LogExtra",
    "PasswordResetUser",
    "PasswordValidator",
    "PermissionObjDict",
    "RuleConfigDict",
    "SessionData",
    "TracebackType",
    "UserID",
    "ValidatableUser",
]
