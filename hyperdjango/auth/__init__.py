"""
Authentication system for HyperDjango.

Session-based auth, API keys, OAuth2, argon2id password hashing, RBAC permissions.

Usage:
    from hyperdjango.auth import require_auth, require_permission, require_staff
    from hyperdjango.auth import hash_password, verify_password
    from hyperdjango.auth import User, AnonymousUser, Group, Permission
    from hyperdjango.auth import PermissionChecker
    from hyperdjango.auth import SessionAuth, session_auth, api_key_auth

    # `@require_permission` needs the RBAC checker installed per request — pass
    # db= to SessionAuth: app.use(SessionAuth(secret=..., store=store, db=app.db))
"""

# ruff: noqa: F401  — public API re-exports

from hyperdjango.auth.api_keys import APIKeyAuth, api_key_auth
from hyperdjango.auth.decorators import (
    require_api_key,
    require_auth,
    require_permission,
    require_staff,
)
from hyperdjango.auth.oauth2 import (
    OAuth2,
    OAuth2Provider,
    auth0,
    github,
    google,
    require_oauth2,
)
from hyperdjango.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from hyperdjango.auth.permissions import PermissionChecker, register_rule_type
from hyperdjango.auth.seed_utils import seed_password
from hyperdjango.auth.sessions import (
    SessionAuth,
    build_session_data,
    get_session_auth_hash,
    is_safe_redirect_url,
    session_auth,
    verify_session_auth_hash,
)
from hyperdjango.auth.user import (
    AnonymousUser,
    CustomRuleConfig,
    FieldMatchConfig,
    FieldPermission,
    Group,
    GroupPermission,
    IpRangeConfig,
    IsOwnerConfig,
    ObjectPermission,
    Permission,
    PermissionRule,
    RuleConfig,
    TimeWindowConfig,
    User,
    UserGroup,
    UserPermission,
    parse_rule_config,
    rule_config_from_json,
    rule_config_to_dict,
    rule_config_to_json,
)

__all__ = [
    "SessionAuth",
    "get_session_auth_hash",
    "verify_session_auth_hash",
    "session_auth",
    "APIKeyAuth",
    "api_key_auth",
    "require_auth",
    "require_permission",
    "require_staff",
    "require_api_key",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "User",
    "AnonymousUser",
    "Group",
    "Permission",
    "ObjectPermission",
    "PermissionRule",
    "FieldPermission",
    "UserGroup",
    "GroupPermission",
    "UserPermission",
    "PermissionChecker",
    "register_rule_type",
    "RuleConfig",
    "IsOwnerConfig",
    "TimeWindowConfig",
    "IpRangeConfig",
    "FieldMatchConfig",
    "CustomRuleConfig",
    "parse_rule_config",
    "rule_config_to_dict",
    "rule_config_to_json",
    "rule_config_from_json",
    "OAuth2",
    "OAuth2Provider",
    "require_oauth2",
    "google",
    "github",
    "auth0",
    "seed_password",
]
