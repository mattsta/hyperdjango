"""
User, Group, Permission models for HyperApp standalone auth.

No Django dependency. Uses HyperApp's Model + Database layer with pg.zig.
Password hashing via argon2id (argon2-cffi).
"""

import contextlib
import unicodedata
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import datetime

from hyperdjango.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from hyperdjango.database import get_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import (
    Field,
    Model,
    create_table_for_model,
    generate_ddl_for_model,
)
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.types import RuleConfigDict

# ── Identity normalization ──────────────────────────────────────────────────
# Usernames/emails are canonicalized to a single Unicode form so that
# visually-identical strings with different code-point sequences (NFC vs NFD
# composed accents, compatibility variants) collapse to ONE key. Without this,
# such variants create distinct rows and slip past the unique constraint /
# lookups. Applied at the User construction choke point (write) and exported so
# any lookup path can canonicalize its query key identically (read). ASCII is
# unchanged, so existing ASCII usernames are unaffected.


def normalize_username(username: str) -> str:
    """Canonicalize a username for storage and lookup.

    Uses NFKC — the standard normalization for identifiers — so both
    NFC/NFD sequences and compatibility variants (e.g. fullwidth forms) of
    the same characters fold to one key. ASCII input is returned unchanged.
    """
    return unicodedata.normalize("NFKC", username)


def normalize_email(email: str) -> str:
    """Canonicalize an email address to NFC.

    NFC is the form email infrastructure expects. The local part is case- and
    code-point-sensitive per RFC 5321, so it is deliberately NOT lowercased —
    only the Unicode composition form is canonicalized. ASCII is unchanged.
    """
    return unicodedata.normalize("NFC", email)


# ── Rule configuration types ────────────────────────────────────────────────
# Each rule_type has a specific config shape. These are the canonical types.


@dataclass(slots=True)
class IsOwnerConfig:
    """Config for is_owner rule: check if user owns the object."""

    owner_field: str = "user_id"


@dataclass(slots=True)
class TimeWindowConfig:
    """Config for time_window rule: restrict access to time range."""

    start: str = "00:00"
    end: str = "23:59"
    timezone: str = "UTC"


@dataclass(slots=True)
class IpRangeConfig:
    """Config for ip_range rule: restrict access to CIDR ranges."""

    ranges: list[str] = dc_field(default_factory=list)


@dataclass(slots=True)
class FieldMatchConfig:
    """Config for field_match rule: check object field against allowed values."""

    field_name: str = ""
    values: list[str] = dc_field(default_factory=list)


@dataclass(slots=True)
class CustomRuleConfig:
    """Config for custom rule: call a Python function by module path."""

    module: str = ""
    function: str = ""


# Union of all rule config types. Custom evaluators use RuleConfigDict.
RuleConfig = (
    IsOwnerConfig
    | TimeWindowConfig
    | IpRangeConfig
    | FieldMatchConfig
    | CustomRuleConfig
    | RuleConfigDict
)


def parse_rule_config(rule_type: str, data: RuleConfigDict) -> RuleConfig:
    """Parse a raw dict into the appropriate typed RuleConfig for the given rule_type."""
    if rule_type == "is_owner":
        return IsOwnerConfig(**{k: v for k, v in data.items() if k == "owner_field"})
    if rule_type == "time_window":
        return TimeWindowConfig(
            **{k: v for k, v in data.items() if k in ("start", "end", "timezone")}
        )
    if rule_type == "ip_range":
        return IpRangeConfig(**{k: v for k, v in data.items() if k == "ranges"})
    if rule_type == "field_match":
        return FieldMatchConfig(
            field_name=data.get("field", ""),
            values=data.get("values", []),
        )
    if rule_type == "custom":
        return CustomRuleConfig(
            **{k: v for k, v in data.items() if k in ("module", "function")}
        )
    # Unknown rule type — return raw dict for extensibility
    return data


def rule_config_to_dict(config: RuleConfig) -> RuleConfigDict:
    """Convert a typed RuleConfig back to a plain dict for serialization."""
    if isinstance(config, dict):
        return config
    d = asdict(config)
    # FieldMatchConfig uses field_name internally but serializes as "field"
    if isinstance(config, FieldMatchConfig):
        d["field"] = d.pop("field_name")
    return d


def rule_config_to_json(config: RuleConfig) -> str:
    """Serialize a RuleConfig to a JSON string for database storage."""
    return fast_json_dumps(rule_config_to_dict(config)).decode()


def rule_config_from_json(rule_type: str, raw: str | RuleConfigDict) -> RuleConfig:
    """Deserialize a JSON string or dict into a typed RuleConfig."""
    if isinstance(raw, str):
        raw = fast_json_loads(raw)
    return parse_rule_config(rule_type, raw)


class User(TimestampMixin, Model):
    """HyperApp user with argon2id password hashing and permission support."""

    class Meta:
        table = "hyper_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: str = Field(default="")
    # editable=False: never writable through a ModelSerializer. password_hash is
    # set only via set_password(); the privilege/state flags are set only by
    # privileged code (admin, RBAC), never mass-assigned from a request body.
    password_hash: str = Field(default="", exclude=True, editable=False)
    first_name: str = Field(default="")
    last_name: str = Field(default="")
    is_active: bool = Field(default=True, editable=False)
    # Django-compatible privilege flags. is_superuser grants all permissions
    # (checked in has_perm); is_staff gates admin-site access. For finer-grained
    # authorization prefer RBAC roles — Require.role("staff") or
    # user.in_group("staff"). SessionUser derives these from the groups frozenset.
    is_staff: bool = Field(default=False, editable=False)
    is_superuser: bool = Field(default=False, editable=False)
    last_login: datetime | None = Field(default=None, editable=False)

    def model_post_init(self, __context: object) -> None:
        """Normalize identity fields at the single construction choke point.

        Every ``User(...)`` construction — the account-creation write path
        (``create_user`` → ``User(...)`` → ``save()``) and DB hydration — passes
        through here, so the stored username/email are always canonical. This
        keeps the unique constraint honest: NFC and NFD spellings of the same
        name can never become two rows. Normalization is idempotent, so
        re-normalizing an already-canonical hydrated value is a no-op.

        Lookup/authentication paths must canonicalize their query key with
        ``normalize_username`` / ``normalize_email`` so reads match writes.
        """
        super().model_post_init(__context)
        if isinstance(self.username, str) and self.username:
            canonical = normalize_username(self.username)
            if canonical != self.username:
                self.username = canonical
        if isinstance(self.email, str) and self.email:
            canonical_email = normalize_email(self.email)
            if canonical_email != self.email:
                self.email = canonical_email

    def set_password(self, raw_password: str):
        """Hash and store a password."""
        self.password_hash = hash_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        """Verify a password against the stored hash."""
        if not self.password_hash:
            return False
        return verify_password(raw_password, self.password_hash)

    def password_needs_rehash(self) -> bool:
        """Check if password hash parameters are outdated."""
        if not self.password_hash:
            return False
        return needs_rehash(self.password_hash)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    # RBAC capability surface, uniform with SessionUser / AnonymousUser so
    # permission classes never branch on user type. NOTE: these are plain
    # (unannotated) class attributes on purpose — an annotated `name: type`
    # here would be collected by ModelMeta as a DB column. User rows do not
    # eagerly load groups/permissions, so these default to empty; admin/staff
    # status for a hydrated User is carried by the is_staff/is_superuser
    # columns (the documented fallback).
    groups = frozenset()  # frozenset[str]
    permissions = frozenset()  # frozenset[str]

    def in_group(self, group_name: str) -> bool:
        """O(1) RBAC group membership check (parity with SessionUser)."""
        return group_name in self.groups

    def has_perm(self, perm: str) -> bool:
        """RBAC permission check. is_superuser grants all."""
        return perm in self.permissions or self.is_superuser

    def has_perms(self, perm_list: list[str]) -> bool:
        return all(self.has_perm(p) for p in perm_list)

    def __str__(self):
        return self.username


class AnonymousUser:
    """Represents an unauthenticated user. Mirrors User/SessionUser API."""

    id = None
    pk = None
    username = ""
    email = ""
    first_name = ""
    last_name = ""
    is_active = False
    is_staff = False
    is_superuser = False
    last_login = None
    groups: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    @property
    def is_authenticated(self) -> bool:
        return False

    @property
    def is_anonymous(self) -> bool:
        return True

    @property
    def full_name(self) -> str:
        return ""

    def in_group(self, group_name: str) -> bool:
        return False

    def has_perm(self, perm: str) -> bool:
        return False

    def has_perms(self, perm_list: list[str]) -> bool:
        return False

    def get(self, key: str, default: object = None) -> object:
        return default

    def __bool__(self) -> bool:
        # Falsy so the common `if request.user:` idiom means "an authenticated
        # real user" — preserving the semantics of the None sentinel this class
        # replaced (ws27 unified anon to AnonymousUser()). `.is_authenticated`
        # stays False and the object remains non-None, so explicit
        # `is None` / `.is_authenticated` checks are unaffected. Real users
        # (User model, SessionUser) have no __bool__ and stay truthy.
        return False

    def __str__(self):
        return "AnonymousUser"


@dataclass(slots=True)
class SessionUser:
    """Wraps a session dict with the same API as User/AnonymousUser.

    ``SessionAuth`` stores user data as a dict in the session store.
    This class provides a consistent interface so all auth consumers
    can access ``.id``, ``.is_authenticated``, ``.username``, etc.
    without branching on types.

    ``groups`` and ``permissions`` are materialized as ``frozenset[str]``
    at construction time (session JSON stores them as lists). Use these
    directly for O(1) membership checks — never call ``.get("groups")``.
    """

    _data: dict[str, object]
    groups: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    def __post_init__(self):
        # Materialize groups/permissions from session dict (stored as JSON lists)
        # SessionUser is a non-frozen slots dataclass — direct assignment to the
        # declared groups/permissions slots is all that is needed here.
        raw_groups = self._data.get("groups")
        if raw_groups and isinstance(raw_groups, list):
            self.groups = frozenset(raw_groups)
        raw_perms = self._data.get("permissions")
        if raw_perms and isinstance(raw_perms, list):
            self.permissions = frozenset(raw_perms)

    def in_group(self, group_name: str) -> bool:
        """O(1) RBAC group membership check."""
        return group_name in self.groups

    def has_perm(self, perm: str) -> bool:
        """O(1) RBAC permission check. Superuser group grants all."""
        return perm in self.permissions or "superuser" in self.groups

    def has_perms(self, perm_list: list[str]) -> bool:
        return all(self.has_perm(p) for p in perm_list)

    @property
    def id(self) -> int | None:
        return self._data.get("id") if "id" in self._data else self._data.get("pk")

    @property
    def pk(self) -> int | None:
        return self.id

    @property
    def username(self) -> str:
        return str(self._data.get("username", ""))

    @property
    def email(self) -> str:
        return str(self._data.get("email", ""))

    @property
    def first_name(self) -> str:
        return str(self._data.get("first_name", ""))

    @property
    def last_name(self) -> str:
        return str(self._data.get("last_name", ""))

    @property
    def is_active(self) -> bool:
        return bool(self._data.get("is_active", True))

    @property
    def is_staff(self) -> bool:
        return "staff" in self.groups

    @property
    def is_superuser(self) -> bool:
        return "superuser" in self.groups

    @property
    def is_authenticated(self) -> bool:
        # A SessionUser is authenticated by construction: it is only ever created
        # from a session that passed the identity allow-list (_is_user_session),
        # or constructed directly to represent an authenticated principal (which
        # may be authorized purely via groups/permissions, with no numeric id —
        # see test_rbac_guards / test_guard::test_authenticated_pass_no_id).
        # The privilege-escalation vector (an anonymous request planting an
        # identity key via the request.session bridge) is closed at its source by
        # _SessionDict's reserved-key write guard, NOT by second-guessing whether
        # an already-constructed SessionUser "really" has an id here.
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p).strip()

    @property
    def last_login(self) -> str | None:
        return self._data.get("last_login")

    @property
    def password_hash(self) -> str:
        return str(self._data.get("password_hash", ""))

    def get(self, key: str, default: object = None) -> object:
        """Dict-like read access for non-standard session keys."""
        return self._data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def model_dump(self) -> dict[str, object]:
        """Return a JSON-serializable dict of user properties."""
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "is_active": self.is_active,
            "is_staff": self.is_staff,
            "is_superuser": self.is_superuser,
            "is_authenticated": True,
        }

    def __str__(self) -> str:
        return self.username or f"SessionUser({self.id})"

    def __repr__(self) -> str:
        return f"SessionUser(id={self.id}, username={self.username!r})"


class Permission(Model):
    """A single permission (e.g., 'add_product')."""

    class Meta:
        table = "hyper_permissions"
        unique_together = [("codename", "model_name")]

    id: int = Field(primary_key=True, auto=True)
    codename: str = Field()
    name: str = Field()
    model_name: str = Field()


class Group(TimestampMixin, Model):
    """A named group/role that aggregates permissions.

    Supports hierarchical inheritance via parent_id: a group inherits all
    permissions from its parent chain (recursive CTE in PostgreSQL).
    priority determines precedence when a user has multiple roles.
    """

    class Meta:
        table = "hyper_groups"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    parent_id: int | None = Field(
        default=None, foreign_key="hyper_groups", on_delete="SET NULL"
    )
    priority: int = Field(default=0)
    rate_limit_tier: str = Field(default="")


class ObjectPermission(Model):
    """Per-object permission grant (user or group can access specific row)."""

    class Meta:
        table = "hyper_object_permissions"

    id: int = Field(primary_key=True, auto=True)
    user_id: int | None = Field(default=None, foreign_key=User, on_delete="CASCADE")
    group_id: int | None = Field(default=None, foreign_key=Group, on_delete="CASCADE")
    permission_id: int = Field(foreign_key=Permission, on_delete="CASCADE")
    object_model: str = Field()
    object_id: str = Field()


class PermissionRule(Model):
    """Conditional rule attached to a permission (is_owner, time_window, etc.)."""

    class Meta:
        table = "hyper_permission_rules"

    id: int = Field(primary_key=True, auto=True)
    permission_id: int = Field(foreign_key=Permission, on_delete="CASCADE")
    rule_type: str = Field()
    rule_config: RuleConfig = Field(default_factory=dict)
    group_id: int | None = Field(default=None, foreign_key=Group, on_delete="CASCADE")
    user_id: int | None = Field(default=None, foreign_key=User, on_delete="CASCADE")
    priority: int = Field(default=0)
    is_deny: bool = Field(default=False)


class FieldPermission(Model):
    """Field-level access control (hidden/readonly/writable per field per role)."""

    class Meta:
        table = "hyper_field_permissions"

    id: int = Field(primary_key=True, auto=True)
    model_name: str = Field()
    field_name: str = Field()
    group_id: int | None = Field(default=None, foreign_key=Group, on_delete="CASCADE")
    user_id: int | None = Field(default=None, foreign_key=User, on_delete="CASCADE")
    access: str = Field(default="hidden")


# ── Junction table models (for admin inline management) ──────────────────


class UserGroup(Model):
    """User↔Group membership junction table."""

    class Meta:
        table = "hyper_user_groups"
        unique_together = [("user_id", "group_id")]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User, on_delete="CASCADE")
    group_id: int = Field(foreign_key=Group, on_delete="CASCADE")


class UserPermission(Model):
    """User↔Permission direct assignment junction table."""

    class Meta:
        table = "hyper_user_permissions"
        unique_together = [("user_id", "permission_id")]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User, on_delete="CASCADE")
    permission_id: int = Field(foreign_key=Permission, on_delete="CASCADE")
    tenant_id: int | None = Field(default=None, index=True)


class GroupPermission(Model):
    """Group↔Permission junction table."""

    class Meta:
        table = "hyper_group_permissions"
        unique_together = [("group_id", "permission_id")]

    id: int = Field(primary_key=True, auto=True)
    group_id: int = Field(foreign_key=Group, on_delete="CASCADE")
    permission_id: int = Field(foreign_key=Permission, on_delete="CASCADE")
    tenant_id: int | None = Field(default=None, index=True)


class RBACAuditEntry(TimestampMixin, Model):
    """Tracks all RBAC permission changes for audit trail."""

    class Meta:
        table = "hyper_rbac_audit"

    id: int = Field(primary_key=True, auto=True)
    actor_user_id: int | None = Field(default=None)
    actor_username: str = Field(default="")
    action: str = Field()
    target_type: str = Field()
    target_id: str = Field(default="")
    detail: dict[str, str | int | bool | None] = Field(default_factory=dict)


# ── RBAC table management ────────────────────────────────────────────────
# All DDL comes from the Model definitions above via create_table_for_model().
# No raw CREATE TABLE SQL. Models are the single source of truth.

# FK-safe creation order (parents before children)
RBAC_MODELS: list[type] = [
    User,
    Permission,
    Group,
    UserGroup,
    UserPermission,
    GroupPermission,
    ObjectPermission,
    PermissionRule,
    FieldPermission,
    RBACAuditEntry,
]

# Compound/partial indexes not expressible via Field(index=True).
# Created after tables exist.
RBAC_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_objperm_user ON hyper_object_permissions (user_id, object_model, object_id)",
    "CREATE INDEX IF NOT EXISTS idx_objperm_group ON hyper_object_permissions (group_id, object_model, object_id)",
    "CREATE INDEX IF NOT EXISTS idx_rules_perm ON hyper_permission_rules (permission_id)",
    "CREATE INDEX IF NOT EXISTS idx_rules_group ON hyper_permission_rules (group_id)",
    "CREATE INDEX IF NOT EXISTS idx_fieldperm_model ON hyper_field_permissions (model_name, field_name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_objperm_user_uniq ON hyper_object_permissions (user_id, permission_id, object_model, object_id) WHERE user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_objperm_group_uniq ON hyper_object_permissions (group_id, permission_id, object_model, object_id) WHERE group_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fieldperm_group_uniq ON hyper_field_permissions (model_name, field_name, group_id) WHERE group_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fieldperm_user_uniq ON hyper_field_permissions (model_name, field_name, user_id) WHERE user_id IS NOT NULL",
]


async def ensure_rbac_tables(db=None) -> None:
    """Create all RBAC tables from Model definitions + compound indexes.

    Safe to call repeatedly (CREATE TABLE IF NOT EXISTS).
    Uses create_table_for_model() — Models are the single source of truth.
    """
    for model_cls in RBAC_MODELS:
        await create_table_for_model(model_cls, db=db)
    idx_db = db if db is not None else get_db()
    for idx_sql in RBAC_INDEXES:
        with contextlib.suppress(Exception):
            await idx_db.execute(idx_sql)


async def drop_rbac_tables(db=None) -> None:
    """Drop all RBAC tables in FK-safe order (children before parents)."""
    if db is None:
        db = get_db()
    for model_cls in reversed(RBAC_MODELS):
        table = model_cls._meta.table
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def ensure_rbac_tables_sync(cursor) -> None:
    """Sync version for Django test compatibility.

    Uses generate_ddl_for_model() — same single source of truth as the
    async version. No duplicated DDL logic.
    """
    for model_cls in RBAC_MODELS:
        for sql in generate_ddl_for_model(model_cls):
            with contextlib.suppress(Exception):
                cursor.execute(sql)
    for idx_sql in RBAC_INDEXES:
        with contextlib.suppress(Exception):
            cursor.execute(idx_sql)


def drop_rbac_tables_sync(cursor) -> None:
    """Sync drop for Django test compatibility."""
    for model_cls in reversed(RBAC_MODELS):
        table = model_cls._meta.table
        cursor.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
