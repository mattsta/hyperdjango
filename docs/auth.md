# Authentication and Authorization

Session-based auth, API keys, OAuth2, argon2id password hashing, and hierarchical RBAC with object-level and field-level permissions.

HyperDjango provides both authentication and authorization together. Unlike Django, which defaults to PBKDF2 and flat permissions, HyperDjango uses argon2id exclusively and implements a full hierarchical RBAC system with recursive CTE-based role inheritance, object-level permissions, conditional rules, and field-level access control -- all built on PostgreSQL with zero external dependencies beyond the database.

---

## Quick Start

```python
from hyperdjango.auth import (
    require_auth,
    require_permission,
    require_staff,
    require_api_key,
    hash_password,
    verify_password,
    User,
    AnonymousUser,
    Group,
    Permission,
    PermissionChecker,
    SessionAuth,
    session_auth,
    APIKeyAuth,
    api_key_auth,
    OAuth2,
    OAuth2Provider,
    google,
    github,
    auth0,
    require_oauth2,
    register_rule_type,
)

app = HyperApp()

# Session-based auth middleware
app.use(SessionAuth(secret="your-secret-key"))


@app.get("/protected")
@require_auth()
async def protected(request):
    return {"user": request.user}


@app.get("/admin-only")
@require_staff
async def admin_panel(request):
    return {"msg": "staff area"}


@app.get("/products/add")
@require_permission("add_product")
async def add_product(request):
    return {"msg": "create product"}


@app.get("/api/data")
@require_api_key
async def api_data(request):
    return {"data": "secret"}


@app.get("/dashboard")
@require_oauth2()
async def dashboard(request):
    return {"user": request.user, "provider": request.oauth2_provider}
```

---

## Session Auth

HyperDjango has one session-auth middleware, `SessionAuth`. Add `db=` to enable
RBAC (`@require_permission`); see [RBAC below](#rbac--enabling-require_permission).

### SessionAuth

```python
auth = SessionAuth(secret="...", token_engine=engine)
app.use(auth)
```

**How it works:**

1. `auth.login(response, {"id": user.id, "username": user.username})` stores the
   user dict in the server-side session store and sets a signed cookie.
2. On each request, the middleware reads the cookie, looks up the session, and
   wraps the stored dict in a `SessionUser` object.
3. `request.user` is a `SessionUser` — a `@dataclass(slots=True)` that exposes
   `.id`, `.username`, `.is_authenticated`, `.is_staff`, `.has_perm()`, etc.

**Tradeoffs:**

- No DB query per request (fast)
- User data is a snapshot from login time (stale if user is updated between logins)
- Password changes are detected via session hash verification (lazy invalidation)
- Used by all 14 services

**When to use:** Most web apps. This is the one session-auth middleware.

### RBAC — enabling `@require_permission`

Pass `db=` to install a `PermissionChecker` on every request so
`@require_permission` decorators can evaluate (without it, those routes return
`403 "Permission system not configured"`):

```python
auth = SessionAuth(secret="...", store=store, db=app.db)
app.use(auth)
```

**How it works:**

1. Session stores only `user_id` (not the full user dict).
2. On each request, the middleware loads the **real User model from the database**
   via `checker.get_user_by_id(user_id)`.
3. `request.user` is a full `User` model instance with current DB state.
4. `request._perm_checker` is set for `@require_permission()` decorators.

**Tradeoffs:**

- One DB query per request (heavier, but User table is small + cached by pg.zig)
- User data is always fresh (permissions, groups, active status)
- Required for `@require_permission()` and `PermissionChecker` RBAC
- Automatic invalidation on password change, account deactivation, etc.

**When to use:** Apps with complex RBAC, per-object permissions, or where
user state changes must take effect immediately (e.g., admin deactivating a
user should lock them out on the next request, not after re-login).

### request.user Type Guarantee

Regardless of which middleware you use, `request.user` is **always** one of:

| Type            | When                             | `.is_authenticated` |
| --------------- | -------------------------------- | ------------------- |
| `None`          | No auth middleware               | N/A                 |
| `AnonymousUser` | SessionAuth, no valid session    | `False`             |
| `SessionUser`   | SessionAuth, valid session       | `True`              |
| `SessionUser`   | SessionAuth, valid login session | `True`              |

**Never a raw dict.** All types expose `.id`, `.username`, `.is_staff`,
`.is_superuser`, `.has_perm()`, `.in_group()`, `.groups`, `.permissions`.
Access properties directly — no `isinstance()` checks, no `getattr()` fallbacks.

### SessionUser RBAC Properties

`SessionUser` materializes RBAC data as `frozenset[str]` at construction time
(from JSON-serialized session lists). All membership checks are O(1):

```python
user.groups  # frozenset[str] — e.g., frozenset({"staff", "editor"})
user.permissions  # frozenset[str] — e.g., frozenset({"add_book", "view_book"})
user.in_group("staff")  # O(1) group membership check
user.has_perm("add_book")  # O(1) permission check (superuser bypasses all)
user.is_staff  # derived: "staff" in user.groups
user.is_superuser  # derived: "superuser" in user.groups
```

`AnonymousUser` implements the same interface with empty frozensets and
`False` returns — no branching needed in consumer code.

---

## User Model

The built-in `User` model is a HyperDjango `Model` stored in the `hyper_users` table. It uses argon2id for all password operations.

### Fields

| Field           | Type               | Description                                 |
| --------------- | ------------------ | ------------------------------------------- |
| `id`            | `int`              | Primary key (auto-increment)                |
| `username`      | `str`              | Unique username (max 150 chars)             |
| `email`         | `str`              | Email address (max 254 chars, default `""`) |
| `password_hash` | `str`              | Argon2id password hash (max 256 chars)      |
| `first_name`    | `str`              | First name (max 150 chars, default `""`)    |
| `last_name`     | `str`              | Last name (max 150 chars, default `""`)     |
| `is_active`     | `bool`             | Account active flag (default `True`)        |
| `is_staff`      | `bool`             | Staff access flag (default `False`)         |
| `is_superuser`  | `bool`             | Full admin privileges (default `False`)     |
| `last_login`    | `datetime \| None` | Last login timestamp                        |
| `created_at`    | `datetime \| None` | Account creation timestamp (auto `NOW()`)   |

### Properties

| Property           | Type   | Description                              |
| ------------------ | ------ | ---------------------------------------- |
| `full_name`        | `str`  | `first_name + " " + last_name`, stripped |
| `is_authenticated` | `bool` | Always `True` for `User` instances       |
| `is_anonymous`     | `bool` | Always `False` for `User` instances      |

### Methods

#### `set_password(raw_password: str) -> None`

Hash and store a password using argon2id. Does not persist to database -- call `save()` or use `PermissionChecker.create_user()` for that.

```python
user = User(username="alice")
user.set_password("secure-password-123")
# user.password_hash is now an argon2id hash string
```

#### `check_password(raw_password: str) -> bool`

Verify a password against the stored hash. Returns `False` if no password is set.

```python
assert user.check_password("secure-password-123")
assert not user.check_password("wrong-password")
```

#### `password_needs_rehash() -> bool`

Check if the stored password hash uses outdated argon2 parameters. When `True`, the password should be re-hashed on next login. This happens automatically in `PermissionChecker.authenticate()`.

```python
if user.password_needs_rehash():
    user.set_password(raw_password)  # Re-hash with current params
```

---

## RBAC Users vs App Users

HyperDjango has two user systems that work together:

### 1. RBAC Users (`hyper_users`)

The built-in `User` model in `hyper_users` is used by:

- **HyperAdmin panel** — admin login, RBAC management
- **RBAC system** — Groups, Permissions, UserGroups, ObjectPermissions
- **`hyper createsuperuser`** CLI command

Created via `PermissionChecker.create_user()` or the admin UI.

### 2. App Users (custom tables)

Most apps define their **own** user model with app-specific fields:

```python
# HyperNews: hn_users with karma, bio, display_name
class User(TimestampMixin, Model):
    class Meta:
        table = "hn_users"

    username: str = Field(unique=True)
    karma: int = Field(default=0)


# HyperTicket: ht_agents with role, tenant_id
class Agent(TenantMixin, TimestampMixin, Model):
    class Meta:
        table = "ht_agents"

    role: AgentRole = Field(default=AgentRole.AGENT)
```

These live in **separate tables** from `hyper_users`. Their IDs are independent — `hn_users.id=1` (alice) is NOT the same entity as `hyper_users.id=1` (admin).

### How Permissions Flow

At login, `build_session_data()` constructs the session dict with RBAC groups:

```python
from hyperdjango.auth.sessions import build_session_data

# App with its own user table — pass explicit groups
is_staff = await user.has_status("access", "staff")
groups = ["staff"] if is_staff else []
session = await build_session_data(
    user.id,
    db,
    groups=groups,  # Explicit — no RBAC query
    username=user.username,
)
auth.login(resp, session, request)

# App using hyper_users directly — auto-fetch from RBAC tables
session = await build_session_data(user.id, db, username=user.username)
# groups fetched from hyper_user_groups via PermissionChecker
```

**Critical rule**: Apps with their own user tables (not `hyper_users`) **must always pass `groups=`** explicitly. If omitted, `build_session_data()` queries `hyper_user_groups` using the app user's ID, which may collide with a different `hyper_users` entry.

### Session Dict Structure

After `build_session_data()`, the session contains:

```python
{
    "id": 42,
    "username": "alice",
    "groups": ["staff", "moderator"],  # RBAC group names
    "is_staff": True,  # Derived: "staff" in groups
    "is_superuser": False,  # Derived: "superuser" in groups
    # ... app-specific fields (role, tenant_id, etc.)
}
```

### Guard Enforcement

Guards check the session groups, never boolean fields:

```python
@guard(Require.role("admin"))           # User must be in "admin" group
@guard(Require.role("team_lead"))       # User must be in "team_lead" group
@guard(Require.permission("edit_post")) # User must have "edit_post" permission
@guard(Require.staff())                 # Checks groups, then is_staff fallback
@guard(
    Require.any_of(                     # OR composition
        Require.role("admin"),
        Require.role("moderator"),
    )
)
```

### Role Hierarchy Pattern

For apps with tiered roles, map roles to hierarchical group lists at login:

```python
# HyperTicket: admin gets all lower roles too
ROLE_GROUPS = {
    AgentRole.ADMIN: ["admin", "team_lead", "agent"],
    AgentRole.TEAM_LEAD: ["team_lead", "agent"],
    AgentRole.AGENT: ["agent"],
}
groups = ROLE_GROUPS[agent.role]
session = await build_session_data(agent.id, db, groups=groups, role=agent.role.value)
```

Then guards use `Require.role("admin")` — an admin passes, an agent is denied. A team_lead passes `Require.role("team_lead")` and `Require.role("agent")` but NOT `Require.role("admin")`.

### When to Use Each Approach

| Scenario                | User Table    | Groups Source                           |
| ----------------------- | ------------- | --------------------------------------- |
| Admin panel access      | `hyper_users` | Auto from `hyper_user_groups`           |
| App with simple auth    | App's own     | Explicit `groups=[]` at login           |
| App with role enum      | App's own     | Map enum → groups list at login         |
| App with timeline roles | App's own     | Check timeline, pass `groups=["staff"]` |
| Multi-tenant SaaS       | App's own     | Map tenant role → groups at login       |

### Template Access

Templates check the `is_staff` context variable (derived from groups), never `user.is_staff`:

```html
{# CORRECT — uses context variable from build_context() #} {% if is_staff %}
<div>Admin controls here</div>
{% endif %} {# WRONG — accesses session boolean directly #} {% if user.is_staff
%} {# DO NOT DO THIS #}
```

---

## AnonymousUser

Represents an unauthenticated user. Mirrors the `User` API so code can treat all users uniformly.

| Attribute          | Value   |
| ------------------ | ------- |
| `id`               | `None`  |
| `pk`               | `None`  |
| `username`         | `""`    |
| `email`            | `""`    |
| `is_active`        | `False` |
| `is_staff`         | `False` |
| `is_superuser`     | `False` |
| `is_authenticated` | `False` |
| `is_anonymous`     | `True`  |
| `full_name`        | `""`    |

Methods `has_perm()` and `has_perms()` always return `False`.

```python
from hyperdjango.auth import AnonymousUser

anon = AnonymousUser()
assert not anon.is_authenticated
assert anon.is_anonymous
assert not anon.has_perm("anything")
```

---

## Seed Credentials

Seed files must NEVER hardcode passwords. Use the dynamic credential helpers:

```python
from hyperdjango.auth import seed_password, hash_password

# App users — resolves HYPER_SEED_PASSWORD, generates random if unset
user = User(username="admin", password_hash=hash_password(seed_password("admin")))

# Admin panel (hyper_users) — resolves HYPER_ADMIN_PASSWORD
checker = PermissionChecker(db)
await checker.ensure_admin_user()  # username="admin", random password if unset
```

Password resolution order:

1. `HYPER_SEED_PASSWORD_<USERNAME>` (per-user override)
2. `HYPER_SEED_PASSWORD` (global for all seed users)
3. Random `secrets.token_urlsafe(16)` — printed to stdout

---

## Password Hashing

HyperDjango uses argon2id exclusively. No PBKDF2, no bcrypt, no scrypt. Argon2id is the winner of the 2015 Password Hashing Competition and is recommended by NIST and OWASP for new applications. It is memory-hard, making GPU-based attacks impractical.

### `hash_password(password: str) -> str`

Hash a password with argon2id. Returns the full hash string including algorithm parameters, salt, and hash.

```python
from hyperdjango.auth import hash_password

hashed = hash_password("my-password")
# "$argon2id$v=19$m=65536,t=3,p=4$..."
```

### `verify_password(password: str, hash: str) -> bool`

Verify a password against an argon2id hash. Uses constant-time comparison internally to prevent timing attacks.

```python
from hyperdjango.auth import verify_password

assert verify_password("my-password", hashed)
assert not verify_password("wrong", hashed)
```

### `needs_rehash(hash: str) -> bool`

Check if a hash was created with outdated argon2 parameters (memory cost, time cost, parallelism). When parameters are upgraded in a new release, existing hashes will return `True` here.

```python
from hyperdjango.auth import needs_rehash

if needs_rehash(hashed):
    new_hash = hash_password("my-password")
    # Store new_hash in database
```

### Transparent Rehashing

`PermissionChecker.authenticate()` automatically checks `needs_rehash()` after every successful login. If the hash is outdated, it re-hashes the password and updates the database transparently. No manual intervention needed.

### User Enumeration Prevention

When `authenticate()` receives an invalid username, it still runs a dummy `verify_password()` call against a pre-computed hash. This ensures the response time is identical for valid and invalid usernames, preventing timing-based user enumeration attacks.

---

## Password Validation

HyperDjango includes a pluggable password validation chain. Run validators before accepting a new password.

### `validate_password(password: str, user: object = None, validators: list | None = None) -> list[str]`

Validate a password against all validators. Returns a list of error messages. Empty list means the password is valid.

```python
from hyperdjango.auth.validators import validate_password

errors = validate_password("short")
# ["Password must be at least 8 characters (got 5)"]

errors = validate_password("12345678")
# ["Password cannot be entirely numeric", "This password is too common"]

errors = validate_password("password123")
# ["This password is too common"]

errors = validate_password("Xk9#mP2$vL7@nQ4")
# [] -- valid
```

### `validate_password_or_raise(password: str, user: object = None, validators: list | None = None) -> None`

Same as `validate_password()` but raises `PasswordValidationError` on failure instead of returning a list.

```python
from hyperdjango.auth.validators import (
    validate_password_or_raise,
    PasswordValidationError,
)

try:
    validate_password_or_raise("short")
except PasswordValidationError as e:
    print(e.messages)  # ["Password must be at least 8 characters (got 5)"]
```

### `get_password_help_texts(validators: list | None = None) -> list[str]`

Return human-readable help text for all validators. Useful for displaying password requirements in forms.

```python
from hyperdjango.auth.validators import get_password_help_texts

texts = get_password_help_texts()
# [
#     "Your password must contain at least 8 characters.",
#     "Your password must be at most 128 characters.",
#     "Your password cannot be entirely numeric.",
#     "Your password cannot be a commonly used password.",
#     "Your password cannot be too similar to your personal information.",
# ]
```

### Built-in Validators

#### MinLengthValidator

Reject passwords shorter than `min_length` (default: 8).

```python
from hyperdjango.auth.validators import MinLengthValidator

validator = MinLengthValidator(min_length=12)
error = validator.validate("short")
# "Password must be at least 12 characters (got 5)"

error = validator.validate("long-enough-password")
# None (valid)
```

#### MaxLengthValidator

Reject passwords longer than `max_length` (default: 128). Prevents denial-of-service via extremely long passwords that would be expensive to hash.

```python
from hyperdjango.auth.validators import MaxLengthValidator

validator = MaxLengthValidator(max_length=128)
error = validator.validate("a" * 200)
# "Password must be at most 128 characters"
```

#### NumericValidator

Reject passwords that consist entirely of digits.

```python
from hyperdjango.auth.validators import NumericValidator

validator = NumericValidator()
error = validator.validate("12345678")
# "Password cannot be entirely numeric"

error = validator.validate("12345678a")
# None (valid -- contains a letter)
```

#### CommonPasswordValidator

Reject passwords found in a list of common passwords. Loads from a bundled `common_passwords.txt` file (20,000 entries) if available, otherwise falls back to a built-in set of 130+ common passwords.

```python
from hyperdjango.auth.validators import CommonPasswordValidator

validator = CommonPasswordValidator()
error = validator.validate("password123")
# "This password is too common"

error = validator.validate("qwerty")
# "This password is too common"

error = validator.validate("Xk9#mP2$vL7@nQ4")
# None (valid)
```

#### UserAttributeSimilarityValidator

Reject passwords that are too similar to user attributes (`username`, `email`, `first_name`, `last_name`). Uses `SequenceMatcher` with a configurable `max_similarity` threshold (default: 0.7). Also catches direct substring containment.

```python
from hyperdjango.auth.validators import UserAttributeSimilarityValidator

validator = UserAttributeSimilarityValidator(max_similarity=0.7)


class FakeUser:
    username = "alice_johnson"
    email = "alice@example.com"
    first_name = "Alice"
    last_name = "Johnson"


error = validator.validate("alice_johnson1", user=FakeUser())
# "Password is too similar to your username"

error = validator.validate("Xk9#mP2$vL7@nQ4", user=FakeUser())
# None (valid)
```

### Custom Validators

Any object with a `validate(password: str, user: object = None) -> str | None` method and a `get_help_text() -> str` method can be used as a validator.

```python
class UppercaseValidator:
    def validate(self, password, user=None):
        if not any(c.isupper() for c in password):
            return "Password must contain at least one uppercase letter"
        return None

    def get_help_text(self):
        return "Your password must contain at least one uppercase letter."


errors = validate_password("lowercase123", validators=[UppercaseValidator()])
# ["Password must contain at least one uppercase letter"]
```

### Default Validator Chain

`get_default_validators()` returns the five built-in validators with their default parameters:

1. `MinLengthValidator(min_length=8)`
2. `MaxLengthValidator(max_length=128)`
3. `NumericValidator()`
4. `CommonPasswordValidator()`
5. `UserAttributeSimilarityValidator(max_similarity=0.7)`

---

## PermissionChecker

Central class for all RBAC operations. Requires a database connection.

```python
from hyperdjango.auth import PermissionChecker

checker = PermissionChecker(db)
await checker.ensure_tables()  # Create all auth tables + indexes
```

### Table Setup

#### `ensure_tables() -> None`

Create all authentication and RBAC tables if they do not exist. Creates the following tables:

- `hyper_users` -- User accounts
- `hyper_permissions` -- Permission definitions
- `hyper_groups` -- Groups/roles with hierarchy
- `hyper_user_groups` -- User-to-group memberships
- `hyper_user_permissions` -- Direct user-to-permission grants
- `hyper_group_permissions` -- Group-to-permission grants
- `hyper_object_permissions` -- Per-object permission grants
- `hyper_permission_rules` -- Conditional rules (JSONB config)
- `hyper_field_permissions` -- Field-level access control
- `hyper_rbac_audit` -- Audit log for all RBAC changes

Also creates all necessary indexes for efficient queries.

### User Lifecycle

#### `create_user(username, password, email="", is_staff=False, is_superuser=False, first_name="", last_name="") -> User`

Create a new user with argon2id-hashed password. Returns the `User` model instance.

**Parameters:**

| Parameter      | Type   | Default  | Description                     |
| -------------- | ------ | -------- | ------------------------------- |
| `username`     | `str`  | required | Unique username (max 150 chars) |
| `password`     | `str`  | required | Raw password (will be hashed)   |
| `email`        | `str`  | `""`     | Email address                   |
| `is_staff`     | `bool` | `False`  | Staff access flag               |
| `is_superuser` | `bool` | `False`  | Superuser flag                  |
| `first_name`   | `str`  | `""`     | First name                      |
| `last_name`    | `str`  | `""`     | Last name                       |

**Returns:** `User` -- the new user model instance (access `.id` for the primary key).

```python
# Regular user
alice = await checker.create_user("alice", "secureP@ss123", email="alice@example.com")

# Staff user
bob = await checker.create_user("bob", "staffP@ss456", is_staff=True)

# Superuser (full admin)
admin = await checker.create_user(
    "admin",
    "adminP@ss789",
    email="admin@example.com",
    is_staff=True,
    is_superuser=True,
    first_name="Admin",
    last_name="User",
)
```

#### `authenticate(username: str, password: str) -> dict | None`

Authenticate a user by username and password. Returns the full user row as a dict on success, `None` on failure.

**Behavior:**

1. Looks up the user by username (must be `is_active=True`)
2. If no user found, runs a dummy `verify_password()` to prevent timing attacks, returns `None`
3. Verifies the password against the stored argon2id hash
4. If hash `needs_rehash()`, transparently re-hashes and updates the database
5. Updates `last_login` timestamp
6. Returns the user dict

**Returns:** `dict` with keys `id`, `username`, `email`, `password_hash`, `first_name`, `last_name`, `is_active`, `is_staff`, `is_superuser`, `last_login`, `created_at` -- or `None` on failure.

```python
# Successful authentication
user = await checker.authenticate("alice", "secureP@ss123")
# {"id": 1, "username": "alice", "email": "alice@example.com", ...}

# Failed authentication
user = await checker.authenticate("alice", "wrong-password")
# None

# Non-existent user (constant-time rejection)
user = await checker.authenticate("nonexistent", "anything")
# None
```

#### `get_user_by_id(user_id: int) -> dict | None`

Fetch a user by primary key. Returns the full user row dict or `None`.

```python
user = await checker.get_user_by_id(1)
# {"id": 1, "username": "alice", "email": "alice@example.com", ...}
```

### Permission Checking

#### `has_perm(user, perm: str, model_name: str | None = None) -> bool`

Check if a user has a specific permission.

**Rules:**

- Inactive users (`is_active=False`) always return `False`
- Superusers with `is_active=True` always return `True`
- Checks both direct user permissions and group permissions (with hierarchy)
- If `model_name` is provided, checks for `model_name.perm` exactly
- If `model_name` is `None`, matches any model that has that permission codename
- Results are cached on `user._perm_cache` for request lifetime

```python
# Check with model_name
allowed = await checker.has_perm(user, "add_product", "product")

# Check without model_name (matches any model)
allowed = await checker.has_perm(user, "add_product")
```

#### `has_perms(user, perm_list: list[str], model_name: str | None = None) -> bool`

Check if a user has ALL of the specified permissions. Returns `True` only if every permission in the list is granted.

```python
all_allowed = await checker.has_perms(user, ["add_product", "view_product"], "product")
```

#### `has_model_perms(user, model_name: str) -> dict[str, bool]`

Get all four CRUD permission flags for a specific model.

**Returns:** `{"add": bool, "change": bool, "delete": bool, "view": bool}`

```python
perms = await checker.has_model_perms(user, "product")
# {"add": True, "change": False, "delete": False, "view": True}

if perms["change"]:
    # User can edit products
    ...
```

#### `has_object_perm(user, perm: str, model_name: str, object_id: str) -> bool`

Check if a user has permission on a specific object.

**Resolution order:**

1. Superuser bypass
2. Model-level perm (grants access to ALL objects of that type)
3. Direct user object permission
4. Group object permission (with hierarchy via cached role tree)

```python
allowed = await checker.has_object_perm(user, "change_post", "post", "42")
```

#### `has_perm_with_rules(user, perm: str, model_name: str, obj=None, request=None) -> bool`

Full permission check combining all RBAC layers: model-level + object-level + conditional rules.

**Evaluation order:**

1. Inactive user check -> `False`
2. Superuser bypass -> `True`
3. Model-level permission check
4. Load applicable rules for this user and permission
5. If no rules defined, fall back to model-level result
6. Evaluate **deny rules first** (highest priority) -- if any match, return `False`
7. Evaluate **allow rules** -- if any match, return `True`; if none match, return `False`

```python
allowed = await checker.has_perm_with_rules(
    user,
    "change_post",
    "post",
    obj=post,  # The actual object (for is_owner, field_match)
    request=request,  # The HTTP request (for ip_range)
)
```

#### `clear_cache(user) -> None`

Clear ALL permission caches on a user object. Call after any permission grant/revoke operation to see changes immediately.

Clears: `_perm_cache`, `_role_tree_cache`, `_rules_cache`, `_field_access_*`.

```python
await checker.grant_user_perm(user_id, "delete_article", "article")
checker.clear_cache(user)
# Now has_perm will re-query the database
```

#### `get_all_permissions(user) -> set[str]`

Get all permission strings for a user (direct + group with hierarchy). Returns a set of `"model_name.codename"` strings. Cached on `user._perm_cache`.

This is called internally by `has_perm()` and `has_perms()`.

### Permission Management

#### `create_default_permissions(model_name: str, verbose_name: str) -> None`

Create the four standard CRUD permissions for a model: `add_`, `change_`, `delete_`, `view_`. Uses `ON CONFLICT DO NOTHING` so it is safe to call repeatedly.

```python
await checker.create_default_permissions("product", "Product")
# Creates: add_product, change_product, delete_product, view_product
```

#### `grant_user_perm(user_id: int, codename: str, model_name: str) -> None`

Grant a permission directly to a user. Uses `ON CONFLICT DO NOTHING` for idempotency. Logs the action to the RBAC audit trail.

```python
await checker.grant_user_perm(user_id, "add_product", "product")
```

#### `revoke_user_perm(user_id: int, codename: str, model_name: str) -> None`

Revoke a directly-granted permission from a user. Does not affect permissions inherited through groups.

```python
await checker.revoke_user_perm(user_id, "add_product", "product")
```

#### `grant_group_perm(group_id: int, codename: str, model_name: str) -> None`

Grant a permission to a group. All users in the group (and all child groups in the hierarchy) inherit this permission.

```python
await checker.grant_group_perm(editor_id, "change_product", "product")
```

#### `add_user_to_group(user_id: int, group_id: int) -> None`

Add a user to a group. The user inherits all permissions from the group and its ancestor chain.

```python
await checker.add_user_to_group(user_id, editor_id)
```

#### `remove_user_from_group(user_id: int, group_id: int) -> None`

Remove a user from a group.

```python
await checker.remove_user_from_group(user_id, editor_id)
```

---

## Group Management and Hierarchical RBAC

Groups inherit permissions from parent groups via PostgreSQL recursive CTEs. This enables role hierarchies where broader roles automatically include the permissions of narrower roles.

### `create_group(name: str, parent_id: int | None = None, priority: int = 0) -> Group`

Create a group/role. Returns the `Group` model instance.

**Parameters:**

| Parameter   | Type          | Default  | Description                                        |
| ----------- | ------------- | -------- | -------------------------------------------------- |
| `name`      | `str`         | required | Unique group name (max 150 chars)                  |
| `parent_id` | `int \| None` | `None`   | Parent group ID for hierarchy inheritance          |
| `priority`  | `int`         | `0`      | Higher = more authoritative in conflict resolution |

```python
# Create a flat group (no hierarchy)
moderators = await checker.create_group("moderators")

# Create a role hierarchy: admin > editor > viewer
viewer = await checker.create_group("viewer")
editor = await checker.create_group("editor", parent_id=viewer.id)
admin = await checker.create_group("admin", parent_id=editor.id)
```

### How Hierarchy Works

When a user belongs to the "editor" group:

1. The recursive CTE walks from "editor" up to "viewer" (its parent)
2. The user inherits all permissions from both "editor" and "viewer"
3. If the user belongs to "admin", they inherit permissions from all three levels

```
admin  --inherits--> editor --inherits--> viewer
  |                    |                    |
  |                    |                    +-- view_product
  |                    +-- change_product
  +-- delete_product

User in "admin" gets: view_product + change_product + delete_product
User in "editor" gets: view_product + change_product
User in "viewer" gets: view_product
```

### `set_group_parent(group_id: int, parent_id: int | None) -> None`

Change a group's parent. Includes cycle detection -- raises `ValueError` if the new parent would create a circular dependency.

```python
# Reparent a group
await checker.set_group_parent(editor_id, new_parent_id)

# Remove parent (make top-level)
await checker.set_group_parent(editor_id, None)

# Cycle detection
try:
    await checker.set_group_parent(
        viewer_id, admin_id
    )  # viewer -> admin -> editor -> viewer = CYCLE
except ValueError as e:
    print(e)  # "Cycle detected: group 1 is already an ancestor of 3"
```

### `get_role_ancestors(group_id: int) -> list[int]`

Get all ancestor group IDs (including self) via recursive CTE.

```python
ancestors = await checker.get_role_ancestors(admin_id)
# [admin_id, editor_id, viewer_id]
```

### `get_group_children(group_id: int) -> list[dict]`

Get direct child groups (one level down). Returns a list of dicts with `id`, `name`, `parent_id`, `priority`.

```python
children = await checker.get_group_children(viewer_id)
# [{"id": 2, "name": "editor", "parent_id": 1, "priority": 0}]
```

### Complete Hierarchy Example

```python
checker = PermissionChecker(db)
await checker.ensure_tables()

# Create hierarchy
viewer = await checker.create_group("viewer")
editor = await checker.create_group("editor", parent_id=viewer.id)
admin = await checker.create_group("admin", parent_id=editor.id)

# Create permissions
await checker.create_default_permissions("article", "Article")

# Assign permissions at each level
await checker.grant_group_perm(viewer.id, "view_article", "article")
await checker.grant_group_perm(editor.id, "change_article", "article")
await checker.grant_group_perm(editor.id, "add_article", "article")
await checker.grant_group_perm(admin.id, "delete_article", "article")

# Create users and assign roles
writer = await checker.create_user("writer", "pass123")
await checker.add_user_to_group(writer.id, editor.id)

# Writer now has: view_article (inherited from viewer) + change_article + add_article
writer = await checker.authenticate("writer", "pass123")
assert await checker.has_perm(writer, "view_article", "article")  # True (inherited)
assert await checker.has_perm(
    writer, "change_article", "article"
)  # True (direct group)
assert not await checker.has_perm(
    writer, "delete_article", "article"
)  # False (admin only)
```

---

## Object-Level Permissions

Per-row access control on top of model-level RBAC. Grant permissions on specific object instances.

### `grant_object_perm(codename, model_name, object_id, user_id=None, group_id=None) -> None`

Grant a permission on a specific object to a user or group. Provide exactly one of `user_id` or `group_id`.

```python
# User-level object permission
await checker.grant_object_perm("change_post", "post", "42", user_id=1)

# Group-level object permission
await checker.grant_object_perm("view_post", "post", "42", group_id=editor_id)
```

### `revoke_object_perm(codename, model_name, object_id, user_id=None, group_id=None) -> None`

Revoke a per-object permission.

```python
await checker.revoke_object_perm("change_post", "post", "42", user_id=1)
```

### `has_object_perm(user, perm, model_name, object_id) -> bool`

Check if a user has permission on a specific object. Checks model-level permissions first (which grant access to all objects), then per-object grants for both the user and their role hierarchy.

```python
allowed = await checker.has_object_perm(user, "change_post", "post", "42")
```

### `get_objects_with_perm(user, codename, model_name) -> list[str]`

Get all object IDs the user has a specific permission on. Useful for building filtered querysets.

```python
object_ids = await checker.get_objects_with_perm(user, "change_post", "post")
# ["42", "57", "103"]

# Use in a query filter
posts = await db.query(
    "SELECT * FROM posts WHERE id = ANY($1)",
    object_ids,
)
```

### Complete Object Permission Example

```python
# Editor can change all posts (model-level)
await checker.grant_group_perm(editor_id, "change_post", "post")

# But a specific contributor can only change their own post (object-level)
contributor = await checker.create_user("contributor", "pass123")
await checker.grant_object_perm("change_post", "post", "42", user_id=contributor.id)

contributor_info = await checker.authenticate("contributor", "pass123")

# Can change post 42 (object-level grant)
assert await checker.has_object_perm(contributor, "change_post", "post", "42")

# Cannot change post 99 (no object-level or model-level grant)
assert not await checker.has_object_perm(contributor, "change_post", "post", "99")
```

---

## Conditional Rules

Five built-in rule types plus a custom rule registry. Rules attach to permissions and evaluate dynamically at check time.

Rules are evaluated in priority order. **Deny rules are always evaluated first** and override allow rules. When allow rules exist, at least one must match for access to be granted.

### `add_rule(codename, model_name, rule_type, rule_config, group_id=None, user_id=None, priority=0, is_deny=False) -> None`

Add a conditional rule to a permission.

**Parameters:**

| Parameter     | Type          | Default  | Description                                                                                                   |
| ------------- | ------------- | -------- | ------------------------------------------------------------------------------------------------------------- |
| `codename`    | `str`         | required | Permission codename                                                                                           |
| `model_name`  | `str`         | required | Model name                                                                                                    |
| `rule_type`   | `str`         | required | One of: `"is_owner"`, `"time_window"`, `"ip_range"`, `"field_match"`, `"custom"`, or a registered custom type |
| `rule_config` | `dict`        | required | Configuration for the rule (stored as JSONB)                                                                  |
| `group_id`    | `int \| None` | `None`   | Apply to this group                                                                                           |
| `user_id`     | `int \| None` | `None`   | Apply to this user                                                                                            |
| `priority`    | `int`         | `0`      | Higher priority rules evaluated first                                                                         |
| `is_deny`     | `bool`        | `False`  | If `True`, this is a deny rule (blocks access)                                                                |

### Built-in Rule Types

#### `is_owner` -- Ownership Check

Grants access when the user's ID matches a field on the object. The `owner_field` config specifies which field on the object holds the owner's user ID.

```python
await checker.add_rule(
    "change_post", "post", "is_owner", {"owner_field": "user_id"}, group_id=editor_id
)

# When checking: if post.user_id == user.id, allow
allowed = await checker.has_perm_with_rules(user, "change_post", "post", obj=post)
```

#### `time_window` -- Time-Based Access

Restricts access to specific hours of the day (UTC).

```python
await checker.add_rule(
    "delete_post",
    "post",
    "time_window",
    TimeWindowConfig(start="09:00", end="17:00"),
    group_id=editor_id,
)

# Only allows deletion between 9:00 and 17:00 UTC
```

#### `ip_range` -- IP-Based Access

Restricts access to specific IP address ranges (CIDR notation).

```python
await checker.add_rule(
    "view_report", "report", "ip_range", {"ranges": ["10.0.0.0/8", "192.168.0.0/16"]}
)

# Only allows access from private network IPs
# Reads client IP from request.client_ip
```

#### `field_match` -- Object Field Matching

Grants access when a specific field on the object matches an expected value.

```python
await checker.add_rule(
    "change_post",
    "post",
    "field_match",
    {"field": "status", "value": "draft"},
    group_id=editor_id,
)

# Only allows editing posts with status == "draft"
```

#### `custom` -- Dynamic Python Evaluator

Loads and calls a Python function by dotted path.

```python
await checker.add_rule(
    "view_report",
    "report",
    "custom",
    {"module": "myapp.rules", "function": "can_view_report"},
)
```

### Deny Rules

Deny rules are evaluated first and take priority over allow rules. Use them to create exceptions to broad permissions.

```python
# Editors can change posts...
await checker.add_rule(
    "change_post", "post", "is_owner", {"owner_field": "user_id"}, group_id=editor_id
)

# ...but nobody can change posts during maintenance window
await checker.add_rule(
    "change_post",
    "post",
    "time_window",
    TimeWindowConfig(start="02:00", end="04:00"),
    is_deny=True,
    priority=10,
)  # Higher priority, deny
```

### Custom Rule Types

Register custom rule evaluators for domain-specific logic. Evaluators can be sync or async.

```python
from hyperdjango.auth import register_rule_type


# Sync evaluator
def eval_department(user, obj, request, config):
    user_dept = getattr(user, "department", None)
    return user_dept == config.get("department")


register_rule_type("department", eval_department)


# Async evaluator
async def eval_subscription(user, obj, request, config):
    sub = await db.query_one(
        "SELECT tier FROM subscriptions WHERE user_id = $1",
        user.id,
    )
    required_tier = config.get("min_tier", "free")
    return sub and tier_rank(sub["tier"]) >= tier_rank(required_tier)


register_rule_type("subscription", eval_subscription)

# Use it
await checker.add_rule(
    "export_data",
    "report",
    "department",
    {"department": "engineering"},
    group_id=eng_group_id,
)
```

---

## Field-Level Permissions

Control visibility and editability of individual fields per role. Three access levels: `"hidden"`, `"readonly"`, `"writable"`.

### `set_field_access(model_name, field_name, access="hidden", group_id=None, user_id=None) -> None`

Set the access level for a specific field. Provide exactly one of `group_id` or `user_id`.

```python
# Viewers can't see salary
await checker.set_field_access(
    "employee", "salary", group_id=viewer_id, access="hidden"
)

# Editors can see but not edit salary
await checker.set_field_access(
    "employee", "salary", group_id=editor_id, access="readonly"
)

# Admins (no restriction set) -- salary is writable by default
```

### `get_field_access(user, model_name) -> dict[str, str]`

Get field access levels for a user on a model. Returns a dict mapping field names to their access level. Fields not in the dict are `"writable"` (default permissive).

When multiple access levels apply (from different groups), the **most permissive** wins.

```python
access = await checker.get_field_access(user, "employee")
# {"salary": "hidden"} -- for a viewer
# {"salary": "readonly"} -- for an editor
# {} -- for an admin (no restrictions)
```

### `filter_fields(user, model_name, data, mode="read") -> dict`

Filter a data dict based on field-level permissions.

- `mode="read"`: removes `"hidden"` fields
- `mode="write"`: removes `"hidden"` and `"readonly"` fields

```python
data = {"name": "Alice", "salary": 100000, "department": "Engineering"}

# Viewer reading data -- salary is hidden
filtered = await checker.filter_fields(viewer_user, "employee", data, mode="read")
# {"name": "Alice", "department": "Engineering"}

# Editor writing data -- salary is readonly
filtered = await checker.filter_fields(editor_user, "employee", data, mode="write")
# {"name": "Alice", "department": "Engineering"}  -- salary stripped for writes

# Admin writing data -- no restrictions
filtered = await checker.filter_fields(admin_user, "employee", data, mode="write")
# {"name": "Alice", "salary": 100000, "department": "Engineering"}
```

---

## Explain and Audit

### `explain_effective_permissions(user_id: int) -> dict`

Build a complete permission picture for a user. Returns all permissions with source attribution -- where each permission came from (direct grant, group name, inherited via hierarchy chain).

**Returns:**

```python
{
    "user": {"id": 1, "username": "alice", ...},
    "groups": [
        {"id": 2, "name": "editor", "parent_id": 1, "priority": 0}
    ],
    "direct_permissions": [
        {"codename": "add_product", "model_name": "product", "name": "Can add Product",
         "source": "direct", "via": ""}
    ],
    "inherited_permissions": [
        {"codename": "view_product", "model_name": "product", "name": "Can view Product",
         "source": "group:viewer", "via": "editor -> viewer"}
    ],
    "object_permissions": [
        {"codename": "change_post", "model_name": "post", "object_id": "42",
         "source": "direct"}
    ],
    "rules": [
        {"codename": "change_post", "model_name": "post", "rule_type": "is_owner",
         "rule_config": {"owner_field": "user_id"}, "is_deny": False,
         "priority": 0, "scope": "group"}
    ],
    "field_access": [
        {"model_name": "employee", "field_name": "salary", "access": "readonly",
         "source": "group"}
    ],
}
```

```python
report = await checker.explain_effective_permissions(user_id)

# Display in admin UI
for perm in report["direct_permissions"]:
    print(f"  {perm['codename']} on {perm['model_name']} (direct)")

for perm in report["inherited_permissions"]:
    print(f"  {perm['codename']} on {perm['model_name']} via {perm['via']}")
```

### `explain_permission_decision(user, perm, model_name, object_id=None, obj=None, request=None) -> dict`

Trace the full decision chain for a specific permission check. Returns the final result and every step in the evaluation.

**Returns:** `{"allowed": bool, "steps": [{"check": str, "result": bool, "detail": str}]}`

```python
decision = await checker.explain_permission_decision(
    user,
    "change_post",
    "post",
    object_id="42",
    obj=post,
    request=request,
)
# {
#     "allowed": True,
#     "steps": [
#         {"check": "is_active", "result": True, "detail": "User is active"},
#         {"check": "is_superuser", "result": False, "detail": "Not superuser"},
#         {"check": "model_perm", "result": True, "detail": "post.change_post in cache"},
#         {"check": "rules", "result": True, "detail": "No conditional rules defined"},
#     ]
# }
```

### RBAC Audit Log

All permission changes are automatically logged to the `hyper_rbac_audit` table. This is a regular (not UNLOGGED) table so the audit trail survives crashes.

Each entry records: `actor_user_id`, `actor_username`, `action`, `target_type`, `target_id`, `detail` (JSONB), `timestamp`.

Actions logged: `grant_perm`, `revoke_perm`, `add_to_group`, `remove_from_group`, `create_group`, `set_parent`, `grant_object_perm`, `revoke_object_perm`, `add_rule`, `set_field_access`.

```python
# Get recent audit entries
entries = await checker._audit.get_recent(limit=50)
for entry in entries:
    print(
        f"{entry['timestamp']} {entry['action']} on {entry['target_type']} {entry['target_id']}"
    )
    print(f"  Detail: {entry['detail']}")
```

---

## Policy Export/Import

Backup and restore the complete RBAC configuration.

### `export_policy() -> dict`

Export the complete RBAC policy as a JSON-serializable dict. Includes all groups (with hierarchy), permissions, user-group memberships, group-permission assignments, user-permission assignments, object permissions, rules, and field permissions.

```python
policy = await checker.export_policy()
# {
#     "version": 1,
#     "exported_at": "2026-03-26T12:00:00+00:00",
#     "groups": [...],
#     "permissions": [...],
#     "user_groups": [...],
#     "group_permissions": [...],
#     "user_permissions": [...],
#     "object_permissions": [...],
#     "permission_rules": [...],
#     "field_permissions": [...],
# }

# Save to file
import json

with open("rbac_policy.json", "w") as f:
    json.dump(policy, f, indent=2)
```

### `import_policy(data, *, clear_existing=False) -> dict`

Import RBAC policy from a dict (as produced by `export_policy()`).

**Parameters:**

- `data`: The policy dict
- `clear_existing`: If `True`, wipe all RBAC data before importing. If `False` (default), merge with existing data using `ON CONFLICT DO NOTHING` / `ON CONFLICT DO UPDATE`.

**Returns:** `{"imported": {"groups": 3, "permissions": 12, ...}, "errors": ["..."]}`

```python
# Merge import (additive)
with open("rbac_policy.json") as f:
    policy = json.load(f)

result = await checker.import_policy(policy)
print(f"Imported: {result['imported']}")
print(f"Errors: {result['errors']}")

# Full replace (wipe and restore)
result = await checker.import_policy(policy, clear_existing=True)
```

Import respects foreign key constraints by importing in dependency order: groups -> permissions -> user_groups -> group_permissions -> user_permissions -> object_permissions -> rules -> field_permissions.

---

## Decorators

### `@require_auth(auth_check=None)`

Requires an authenticated user. Returns HTTP 401 if not authenticated. Can be called with or without parentheses.

**Parameters:**

- `auth_check`: Optional callable that takes a request and returns `bool`. Defaults to checking `request.user` is not `None` and is authenticated.

```python
# Default check (session or User object authentication)
@app.get("/protected")
@require_auth()
async def protected(request):
    return {"user": request.user}


# Custom check
@app.get("/api")
@require_auth(lambda r: r.api_key_valid)
async def api_endpoint(request):
    return {"data": "secret"}
```

**Default auth check behavior:**

`request.user` is always one of four types — never a raw dict:

- `None` — no auth middleware active
- `AnonymousUser` — SessionAuth active, no valid session (`.is_authenticated = False`)
- `SessionUser` — SessionAuth active, valid session (`.is_authenticated = True`)
- `SessionUser` — SessionAuth active, valid login session (`.is_authenticated = True`)

The default check: `user is not None and user.is_authenticated`.

### `@require_staff`

Requires `is_staff=True` on the user. Returns HTTP 401 if not authenticated, HTTP 403 if authenticated but not staff. No parentheses needed.

```python
@app.get("/admin")
@require_staff
async def admin_panel(request):
    return {"msg": "staff area"}
```

### `@require_permission(*perms)`

Requires one or more specific permissions via `PermissionChecker`. The `request._perm_checker` must be set (done automatically by `SessionAuth` when constructed with `db=`).

**Behavior:**

1. Returns 401 if not authenticated
2. Superusers bypass all checks
3. Checks each permission via `checker.has_perm()`
4. Returns 403 if any permission is missing

```python
# Single permission
@app.post("/products")
@require_permission("add_product")
async def create_product(request):
    return {"msg": "created"}


# Multiple permissions (ALL required)
@app.put("/products/{id}")
@require_permission("change_product", "view_product")
async def edit_product(request, id):
    return {"msg": "updated"}
```

### `@require_api_key`

Requires a valid API key. Checks `request.api_key_valid` which is set by `APIKeyAuth` middleware. Returns HTTP 401 if not valid.

```python
@app.get("/api/data")
@require_api_key
async def api_data(request):
    return {"key": request.api_key, "data": "secret"}
```

### `@require_oauth2(provider_name=None)`

Requires an OAuth2-authenticated session. Returns HTTP 401 if not authenticated via OAuth2, HTTP 403 if authenticated via the wrong provider.

**Parameters:**

- `provider_name`: If specified, requires authentication via this specific provider (e.g., `"google"`, `"github"`).

```python
# Any OAuth2 provider
@app.get("/dashboard")
@require_oauth2()
async def dashboard(request):
    return {"user": request.user, "provider": request.oauth2_provider}


# Specific provider only
@app.get("/google-only")
@require_oauth2("google")
async def google_dashboard(request):
    return {"user": request.user}
```

---

## Session Management

Sessions are stored server-side with pluggable backends. The cookie contains only a signed session ID. No JWT.

### Session Backends

#### InMemorySessionStore

Fast, single-process only. Sessions lost on restart. Default for development.

```python
from hyperdjango.auth.sessions import InMemorySessionStore

store = InMemorySessionStore(max_age=86400)  # 24 hours

session_id = store.create({"user_id": 1, "username": "alice"})
data = store.get(session_id)  # {"user_id": 1, "username": "alice"}
store.update(session_id, {**data, "role": "editor"})
store.delete(session_id)
store.cleanup()  # Remove expired sessions
count = store.count()  # Count active sessions
store.invalidate_for_user(1)  # Remove all sessions for user 1
store.invalidate_by_hash(1, hash)  # Remove sessions with wrong auth hash
```

#### DatabaseSessionStore

PostgreSQL UNLOGGED table for production. Multi-server coordination. Sessions survive app restarts but not PostgreSQL crashes (acceptable for session data).

```python
from hyperdjango.auth.db_sessions import DatabaseSessionStore

store = DatabaseSessionStore(db, max_age=86400)
await store.ensure_table()  # Creates hyper_sessions UNLOGGED table

session_id = await store.create({"user_id": 1, "username": "alice"})
data = await store.get(session_id)
await store.update(session_id, {**data, "role": "editor"})
await store.delete(session_id)
await store.cleanup()  # Delete expired sessions
count = await store.count()  # Count active sessions
await store.invalidate_for_user(1)  # "Log out everywhere"
await store.invalidate_by_hash(1, valid_hash)  # Invalidate old password sessions
sessions = await store.get_user_sessions(1)  # List all sessions for a user
await store.touch(session_id)  # Extend expiry without changing data
```

### DatabaseSessionStore Methods

| Method                                    | Parameters                      | Returns        | Description                       |
| ----------------------------------------- | ------------------------------- | -------------- | --------------------------------- |
| `ensure_table()`                          | --                              | `None`         | Create UNLOGGED table + indexes   |
| `create(data)`                            | `data: dict`                    | `str`          | Create session, return ID         |
| `get(session_id)`                         | `session_id: str`               | `dict \| None` | Get data or None if expired       |
| `update(session_id, data)`                | `session_id: str, data: dict`   | `None`         | Update data + extend expiry       |
| `delete(session_id)`                      | `session_id: str`               | `None`         | Delete a session                  |
| `cleanup()`                               | --                              | `None`         | Delete all expired sessions       |
| `count()`                                 | --                              | `int`          | Count active sessions             |
| `invalidate_for_user(user_id)`            | `user_id: int`                  | `None`         | Delete all sessions for a user    |
| `invalidate_by_hash(user_id, valid_hash)` | `user_id: int, valid_hash: str` | `None`         | Delete sessions with wrong hash   |
| `get_user_sessions(user_id)`              | `user_id: int`                  | `list[dict]`   | List all active sessions          |
| `touch(session_id)`                       | `session_id: str`               | `None`         | Extend expiry without data change |

### SessionAuth Middleware

Reads a signed session cookie, validates it, and attaches user data to `request.user`.

```python
from hyperdjango.auth import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore

# Development (in-memory)
app.use(SessionAuth(secret="your-secret-key"))

# Production (database-backed)
store = DatabaseSessionStore(db, max_age=86400)
await store.ensure_table()
session = SessionAuth(
    secret="your-secret-key",
    store=store,
    cookie_name="session",  # Cookie name (default: "session")
    secure_cookie=True,  # Secure flag on cookie (default: True)
    cookie_httponly=None,  # HttpOnly; None → SESSION_COOKIE_HTTPONLY setting
    cookie_samesite=None,  # SameSite; None → SESSION_COOKIE_SAMESITE setting
    get_user=None,  # Async callable to fetch user for hash verification
    verify_auth_hash=True,  # Verify session auth hash (default: True)
)
app.use(session)
```

### SessionAuth Constructor Parameters

| Parameter          | Type                  | Default                  | Description                                                                                                                                                              |
| ------------------ | --------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `secret`           | `str`                 | required                 | HMAC signing key for session cookies                                                                                                                                     |
| `cookie_name`      | `str`                 | `"session"`              | Name of the session cookie                                                                                                                                               |
| `store`            | session store         | `InMemorySessionStore()` | Session storage backend                                                                                                                                                  |
| `secure_cookie`    | `bool`                | `True`                   | Secure-by-default floor; pass `False` to defer to the `SESSION_COOKIE_SECURE` setting                                                                                    |
| `cookie_httponly`  | `bool \| None`        | `None`                   | `HttpOnly` flag; `None` defers to `SESSION_COOKIE_HTTPONLY` (default `True`)                                                                                             |
| `cookie_samesite`  | `str \| None`         | `None`                   | `SameSite` policy; `None` defers to `SESSION_COOKIE_SAMESITE` (default `"Lax"`)                                                                                          |
| `get_user`         | `callable \| None`    | `None`                   | Async function `(user_id) -> dict` for hash verification                                                                                                                 |
| `verify_auth_hash` | `bool`                | `True`                   | Whether to verify session auth hash                                                                                                                                      |
| `token_engine`     | `TokenEngine \| None` | `None`                   | TokenEngine for signed cookies (HMAC + salt + XOR). When set, cookies are signed via TokenEngine instead of plain `sign_data()`. Forged cookies rejected without DB hit. |

### Login and Logout

```python
from hyperdjango.response import Response

# Login (sync store)
response = Response.json({"ok": True})
session_id = session.login(response, user_data, request=request)

# Login (async store)
session_id = await session.login_async(response, user_data, request=request)

# Logout (sync store)
session.logout(response, session_id)

# Logout (async store)
await session.logout_async(response, session_id)
```

Login automatically:

1. Prevents **session fixation** by invalidating any pre-existing session
2. Injects a **session auth hash** derived from the user's `password_hash`
3. Creates a new session and sets a signed, HttpOnly, SameSite=Lax cookie

### Session Auth Hash

When a user changes their password, all existing sessions become invalid automatically. This is implemented via `get_session_auth_hash()`:

```python
from hyperdjango.auth.sessions import get_session_auth_hash, verify_session_auth_hash

# Compute hash from password_hash and app secret
hash_val = get_session_auth_hash(user["password_hash"], secret="app-secret")

# Verify a stored hash (constant-time comparison)
valid = verify_session_auth_hash(
    stored_hash, user["password_hash"], secret="app-secret"
)
```

When `verify_auth_hash=True` (default) and `get_user` is configured on SessionAuth, each request verifies that the session's stored hash matches the user's current password hash. If the password was changed, the hash mismatches and the session is silently invalidated.

---

## API Key Management

Validates API keys from HTTP headers or query parameters. Keys are stored as SHA-256 hashes in memory -- if process memory is dumped, raw API keys are not exposed.

### APIKeyAuth Middleware

```python
from hyperdjango.auth import APIKeyAuth

# Static key set
api_keys = APIKeyAuth(
    valid_keys={"sk_live_abc123", "sk_live_def456"},
    header="x-api-key",  # Header name (default, case-insensitive)
    query_param=None,  # Optional query parameter name
    validate_func=None,  # Optional async validation function
)
app.use(api_keys)
```

### Constructor Parameters

| Parameter       | Type               | Default       | Description                             |
| --------------- | ------------------ | ------------- | --------------------------------------- |
| `valid_keys`    | `set[str] \| None` | `None`        | Set of valid API key strings            |
| `header`        | `str`              | `"x-api-key"` | HTTP header to check (case-insensitive) |
| `query_param`   | `str \| None`      | `None`        | Query parameter to check as fallback    |
| `validate_func` | `callable \| None` | `None`        | Async function `(key: str) -> bool`     |

### How It Works

1. Checks the specified header for an API key
2. If not found and `query_param` is set, checks the query parameter
3. If `validate_func` is provided, calls it with the key
4. Otherwise, hashes the key with SHA-256 and compares against stored hashes (constant-time via `hmac.compare_digest`)
5. Sets `request.api_key` (the raw key) and `request.api_key_valid` (bool) on the request

### Database-Backed API Key Validation

For dynamic API key management (create/revoke via admin), use a custom `validate_func`:

```python
async def validate_api_key(key):
    """Look up API key in database."""
    row = await db.query_one(
        "SELECT id, user_id, is_active FROM api_keys "
        "WHERE key_hash = $1 AND is_active = true",
        hashlib.sha256(key.encode()).hexdigest(),
    )
    return row is not None


api_keys = APIKeyAuth(validate_func=validate_api_key)
app.use(api_keys)
```

### Using API Keys in Routes

```python
@app.get("/api/data")
@require_api_key
async def api_data(request):
    return {"key": request.api_key, "valid": request.api_key_valid}
```

---

## SessionAuth with RBAC (`db=`)

`SessionAuth` is the one session-auth middleware. Pass `db=` to also attach a
`PermissionChecker` on every request, enabling decorator-based permission checks:

```python
from hyperdjango.auth import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore

auth = SessionAuth(
    secret="your-secret-key",  # Cookie signing key
    store=DatabaseSessionStore(app.db),  # Session store instance
    db=app.db,  # Enables @require_permission (installs PermissionChecker)
)
app.use(auth)
```

### What It Does On Each Request

1. Sets `request.user = AnonymousUser()` (default)
2. Sets `request._perm_checker = PermissionChecker(db)` when `db=` was given (for `@require_permission`)
3. Reads and verifies the session cookie
4. Loads session data from the store, verifying the session auth hash (a password
   change silently invalidates the session)
5. If the session holds a login, sets `request.user` to the authenticated `SessionUser`

---

## OAuth2

Built-in OAuth2 Authorization Code Flow with PKCE. Server-side only. No JWT. Tokens stored in sessions.

### Provider Configuration

Three built-in provider presets: Google, GitHub, Auth0.

#### Google

```python
from hyperdjango.auth import google

provider = google(
    client_id="your-google-client-id",
    client_secret="your-google-client-secret",
    scopes=["openid", "email", "profile"],  # optional, these are defaults
)
```

Google uses OpenID Connect. Default scopes: `openid`, `email`, `profile`. User ID field: `sub`. Email field: `email`. Name field: `name`. Avatar field: `picture`.

**Google Cloud Console setup:**

1. Go to APIs & Services -> Credentials
2. Create an OAuth 2.0 Client ID (Web application)
3. Set Authorized redirect URI to `https://yourapp.com/auth/google/callback`
4. Copy Client ID and Client Secret

#### GitHub

```python
from hyperdjango.auth import github

provider = github(
    client_id="your-github-client-id",
    client_secret="your-github-client-secret",
    scopes=["read:user", "user:email"],  # optional, these are defaults
)
```

GitHub uses standard OAuth2. Default scopes: `read:user`, `user:email`. User ID field: `id`. Name field: `login`. Avatar field: `avatar_url`.

**GitHub Developer Settings setup:**

1. Go to Settings -> Developer settings -> OAuth Apps
2. Create a new OAuth App
3. Set Authorization callback URL to `https://yourapp.com/auth/github/callback`
4. Copy Client ID and Client Secret

#### Auth0

```python
from hyperdjango.auth import auth0

provider = auth0(
    domain="your-tenant.auth0.com",
    client_id="your-auth0-client-id",
    client_secret="your-auth0-client-secret",
    scopes=["openid", "email", "profile"],  # optional, these are defaults
)
```

Auth0 uses OpenID Connect. Default scopes: `openid`, `email`, `profile`. User ID field: `sub`. Email field: `email`. Name field: `name`. Avatar field: `picture`.

**Auth0 Dashboard setup:**

1. Go to Applications -> Create Application -> Regular Web Application
2. Set Allowed Callback URL to `https://yourapp.com/auth/auth0/callback`
3. Copy Domain, Client ID, Client Secret

### OAuth2Provider Dataclass

For custom providers, create an `OAuth2Provider` directly:

```python
from hyperdjango.auth import OAuth2Provider

custom_provider = OAuth2Provider(
    name="custom",
    client_id="...",
    client_secret="...",
    authorize_url="https://provider.com/authorize",
    token_url="https://provider.com/token",
    userinfo_url="https://provider.com/userinfo",
    scopes=["openid", "email"],
    discovery_url=None,  # OIDC discovery URL (optional)
    id_field="sub",  # Field in userinfo for user ID
    email_field="email",  # Field in userinfo for email
    name_field="name",  # Field in userinfo for display name
    avatar_field="picture",  # Field in userinfo for avatar URL
)
```

### OAuth2 Middleware Setup

```python
from hyperdjango.auth import OAuth2, SessionAuth, google, github, auth0

session = SessionAuth(secret="your-secret-key")
app.use(session)

oauth = OAuth2(secret="your-oauth-secret")
oauth.add_provider(google(client_id="...", client_secret="..."))
oauth.add_provider(github(client_id="...", client_secret="..."))
oauth.add_provider(
    auth0(domain="tenant.auth0.com", client_id="...", client_secret="...")
)
oauth.set_session_auth(session)  # Required for session creation on callback
app.use(oauth)
```

### OAuth2 Constructor Parameters

| Parameter       | Type  | Default   | Description                           |
| --------------- | ----- | --------- | ------------------------------------- |
| `secret`        | `str` | required  | HMAC key for state parameter signing  |
| `login_prefix`  | `str` | `"/auth"` | URL prefix for login/callback routes  |
| `state_max_age` | `int` | `300`     | Max age for state parameter (seconds) |

### Auto-Registered Routes

The OAuth2 middleware automatically registers two routes per provider:

| Route                                | Method | Description                                              |
| ------------------------------------ | ------ | -------------------------------------------------------- |
| `{login_prefix}/{provider}/login`    | GET    | Redirects user to provider's authorization page          |
| `{login_prefix}/{provider}/callback` | GET    | Handles OAuth2 callback, exchanges code, creates session |

With default `login_prefix="/auth"` and a Google provider:

- `/auth/google/login` -- Redirect to Google
- `/auth/google/callback` -- Handle Google callback

### OAuth2 Flow

1. User visits `/auth/google/login`
2. Middleware generates HMAC-signed state parameter (CSRF protection)
3. Middleware generates PKCE code_verifier and code_challenge
4. User is redirected to Google's authorization page
5. User authorizes the application
6. Google redirects to `/auth/google/callback?code=...&state=...`
7. Middleware verifies the state parameter (CSRF + replay protection)
8. Middleware exchanges the authorization code for tokens via HTTPS POST
9. Middleware fetches user profile from provider's userinfo endpoint
10. Middleware creates a session with normalized user data
11. User is redirected to `/` with a session cookie

### Security Features

- **PKCE (Proof Key for Code Exchange)**: S256 method. Prevents authorization code interception.
- **State parameter**: HMAC-signed with timestamp and nonce. Prevents CSRF attacks.
- **Replay protection**: Used state nonces are tracked and rejected on reuse.
- **Server-side tokens**: Access/refresh tokens stored server-side, never in cookies or client.
- **No JWT**: Token validation is server-side only.

### Token Management

```python
# Get stored tokens for a session
tokens = oauth.get_tokens(session_id)
if tokens and not tokens.expired:
    # Use tokens.access_token for API calls to the provider
    ...

# Store tokens (done automatically on callback)
oauth.store_tokens(session_id, tokens)

# Clear tokens (on logout)
oauth.clear_tokens(session_id)
```

### Complete OAuth2 Example

```python
from hyperdjango import HyperApp
from hyperdjango.auth import (
    SessionAuth,
    OAuth2,
    google,
    github,
    require_auth,
    require_oauth2,
)
from hyperdjango.auth.db_sessions import DatabaseSessionStore
from hyperdjango.response import Response

app = HyperApp()

# Database session store
store = DatabaseSessionStore(db, max_age=86400)

# Session middleware
session = SessionAuth(secret="your-secret-key", store=store)
app.use(session)

# OAuth2 middleware
oauth = OAuth2(secret="your-oauth-secret")
oauth.add_provider(google(client_id="...", client_secret="..."))
oauth.add_provider(github(client_id="...", client_secret="..."))
oauth.set_session_auth(session)
app.use(oauth)


@app.get("/")
async def home(request):
    if request.user:
        return {"logged_in": True, "user": request.user}
    return {
        "logged_in": False,
        "login_urls": ["/auth/google/login", "/auth/github/login"],
    }


@app.get("/dashboard")
@require_oauth2()
async def dashboard(request):
    return {
        "user": request.user,
        "provider": request.oauth2_provider,
    }


@app.get("/google-only")
@require_oauth2("google")
async def google_only(request):
    return {"user": request.user}


@app.get("/logout")
@require_auth()
async def logout(request):
    response = Response.redirect("/")
    await session.logout_async(response, request.session_id)
    return response
```

---

## Permission Caching

Permissions are cached per-request on the user object. The following caches exist:

| Cache Attribute         | Contents                                  | Set By                                  |
| ----------------------- | ----------------------------------------- | --------------------------------------- |
| `_perm_cache`           | `set[str]` of all permission strings      | `has_perm()`                            |
| `_role_tree_cache`      | `list[int]` of all group IDs in hierarchy | `has_object_perm()`, rules, field perms |
| `_rules_cache`          | `dict[str, list[dict]]` of loaded rules   | `has_perm_with_rules()`                 |
| `_field_access_{model}` | `dict[str, str]` of field access levels   | `get_field_access()`                    |

To see permission changes take effect immediately, clear the cache:

```python
# Grant new permission
await checker.grant_user_perm(user_id, "delete_article", "article")

# Option 1: Clear cache and re-check
checker.clear_cache(user)
has_perm = await checker.has_perm(user, "delete_article", "article")  # True

# Option 2: Re-fetch user from database
user = await checker.authenticate(username, password)
has_perm = await checker.has_perm(user, "delete_article", "article")  # True
```

---

## Database Schema

All authentication tables are created by `checker.ensure_tables()`. Here is the complete schema:

### hyper_users

```sql
CREATE TABLE hyper_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(150) UNIQUE NOT NULL,
    email VARCHAR(254) DEFAULT '',
    password_hash VARCHAR(256) DEFAULT '',
    first_name VARCHAR(150) DEFAULT '',
    last_name VARCHAR(150) DEFAULT '',
    is_active BOOLEAN DEFAULT TRUE,
    is_staff BOOLEAN DEFAULT FALSE,
    is_superuser BOOLEAN DEFAULT FALSE,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### hyper_groups

```sql
CREATE TABLE hyper_groups (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) UNIQUE NOT NULL,
    parent_id INTEGER REFERENCES hyper_groups(id) ON DELETE SET NULL,
    priority INTEGER DEFAULT 0,
    rate_limit_tier VARCHAR(50) DEFAULT ''
);
```

### hyper_permissions

```sql
CREATE TABLE hyper_permissions (
    id SERIAL PRIMARY KEY,
    codename VARCHAR(100) NOT NULL,
    name VARCHAR(255) NOT NULL,
    model_name VARCHAR(100) NOT NULL,
    UNIQUE(codename, model_name)
);
```

### hyper_sessions (UNLOGGED)

```sql
CREATE UNLOGGED TABLE hyper_sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    data JSONB NOT NULL DEFAULT '{}',
    user_id INTEGER,
    session_hash VARCHAR(64) DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
```

### Junction Tables

```sql
-- User-Group membership
CREATE TABLE hyper_user_groups (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES hyper_users(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES hyper_groups(id) ON DELETE CASCADE,
    UNIQUE(user_id, group_id)
);

-- User-Permission direct grant
CREATE TABLE hyper_user_permissions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES hyper_users(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES hyper_permissions(id) ON DELETE CASCADE,
    UNIQUE(user_id, permission_id)
);

-- Group-Permission grant
CREATE TABLE hyper_group_permissions (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES hyper_groups(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES hyper_permissions(id) ON DELETE CASCADE,
    UNIQUE(group_id, permission_id)
);
```

### RBAC Tables

```sql
-- Object-level permissions
CREATE TABLE hyper_object_permissions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES hyper_users(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES hyper_groups(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES hyper_permissions(id) ON DELETE CASCADE,
    object_model VARCHAR(100) NOT NULL,
    object_id VARCHAR(100) NOT NULL
);

-- Conditional rules (JSONB config)
CREATE TABLE hyper_permission_rules (
    id SERIAL PRIMARY KEY,
    permission_id INTEGER NOT NULL REFERENCES hyper_permissions(id) ON DELETE CASCADE,
    rule_type VARCHAR(50) NOT NULL,
    rule_config JSONB NOT NULL DEFAULT '{}',
    group_id INTEGER REFERENCES hyper_groups(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES hyper_users(id) ON DELETE CASCADE,
    priority INTEGER DEFAULT 0,
    is_deny BOOLEAN DEFAULT FALSE
);

-- Field-level access control
CREATE TABLE hyper_field_permissions (
    id SERIAL PRIMARY KEY,
    model_name VARCHAR(100) NOT NULL,
    field_name VARCHAR(100) NOT NULL,
    group_id INTEGER REFERENCES hyper_groups(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES hyper_users(id) ON DELETE CASCADE,
    access VARCHAR(20) NOT NULL DEFAULT 'hidden'
);

-- RBAC audit log (NOT UNLOGGED -- survives crashes)
CREATE TABLE hyper_rbac_audit (
    id SERIAL PRIMARY KEY,
    actor_user_id INTEGER,
    actor_username VARCHAR(150) DEFAULT '',
    action VARCHAR(50) NOT NULL,
    target_type VARCHAR(50) NOT NULL,
    target_id VARCHAR(100) DEFAULT '',
    detail JSONB DEFAULT '{}',
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Model Reference

### Permission

```python
class Permission(Model):
    class Meta:
        table = "hyper_permissions"

    id: int = Field(primary_key=True, auto=True)
    codename: str = Field()
    name: str = Field()
    model_name: str = Field()
```

### Group

```python
class Group(Model):
    class Meta:
        table = "hyper_groups"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    parent_id: int | None = Field(default=None)
    priority: int = Field(default=0)
    rate_limit_tier: str = Field(default="")
```

### ObjectPermission

```python
class ObjectPermission(Model):
    class Meta:
        table = "hyper_object_permissions"

    id: int = Field(primary_key=True, auto=True)
    user_id: int | None = Field(default=None, foreign_key=User)
    group_id: int | None = Field(default=None, foreign_key=Group)
    permission_id: int = Field(foreign_key=Permission)
    object_model: str = Field()
    object_id: str = Field()
```

### PermissionRule

```python
class PermissionRule(Model):
    class Meta:
        table = "hyper_permission_rules"

    id: int = Field(primary_key=True, auto=True)
    permission_id: int = Field(foreign_key=Permission)
    rule_type: str = Field()
    rule_config: RuleConfig = Field(default_factory=dict)
    group_id: int | None = Field(default=None, foreign_key=Group)
    user_id: int | None = Field(default=None, foreign_key=User)
    priority: int = Field(default=0)
    is_deny: bool = Field(default=False)
```

### FieldPermission

```python
class FieldPermission(Model):
    class Meta:
        table = "hyper_field_permissions"

    id: int = Field(primary_key=True, auto=True)
    model_name: str = Field()
    field_name: str = Field()
    group_id: int | None = Field(default=None, foreign_key=Group)
    user_id: int | None = Field(default=None, foreign_key=User)
    access: str = Field(default="hidden")
```

### Junction Table Models

```python
class UserGroup(Model):
    class Meta:
        table = "hyper_user_groups"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    group_id: int = Field(foreign_key=Group)


class UserPermission(Model):
    class Meta:
        table = "hyper_user_permissions"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    permission_id: int = Field(foreign_key=Permission)


class GroupPermission(Model):
    class Meta:
        table = "hyper_group_permissions"

    id: int = Field(primary_key=True, auto=True)
    group_id: int = Field(foreign_key=Group)
    permission_id: int = Field(foreign_key=Permission)
```

---

## Complete API Reference

### PermissionChecker Methods

| Method                                                                   | Parameters                                                               | Returns           | Description                      |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------ | ----------------- | -------------------------------- |
| `ensure_tables()`                                                        | --                                                                       | `None`            | Create all auth tables + indexes |
| `create_user(...)`                                                       | username, password, email, is_staff, is_superuser, first_name, last_name | `User`            | Create user, return instance     |
| `authenticate(username, password)`                                       | `str, str`                                                               | `dict \| None`    | Authenticate user                |
| `get_user_by_id(user_id)`                                                | `int`                                                                    | `dict \| None`    | Fetch user by ID                 |
| `has_perm(user, perm, model_name)`                                       | user, `str`, `str \| None`                                               | `bool`            | Check single permission          |
| `has_perms(user, perm_list, model_name)`                                 | user, `list[str]`, `str \| None`                                         | `bool`            | Check all permissions            |
| `has_model_perms(user, model_name)`                                      | user, `str`                                                              | `dict[str, bool]` | Get CRUD permission flags        |
| `has_object_perm(user, perm, model_name, object_id)`                     | user, `str`, `str`, `str`                                                | `bool`            | Check object-level permission    |
| `has_perm_with_rules(user, perm, model_name, obj, request)`              | user, `str`, `str`, Any, Any                                             | `bool`            | Full check with rules            |
| `clear_cache(user)`                                                      | user                                                                     | `None`            | Clear all permission caches      |
| `create_default_permissions(model_name, verbose_name)`                   | `str`, `str`                                                             | `None`            | Create 4 CRUD permissions        |
| `grant_user_perm(user_id, codename, model_name)`                         | `int`, `str`, `str`                                                      | `None`            | Grant direct permission          |
| `revoke_user_perm(user_id, codename, model_name)`                        | `int`, `str`, `str`                                                      | `None`            | Revoke direct permission         |
| `grant_group_perm(group_id, codename, model_name)`                       | `int`, `str`, `str`                                                      | `None`            | Grant group permission           |
| `add_user_to_group(user_id, group_id)`                                   | `int`, `int`                                                             | `None`            | Add user to group                |
| `remove_user_from_group(user_id, group_id)`                              | `int`, `int`                                                             | `None`            | Remove user from group           |
| `create_group(name, parent_id, priority)`                                | `str`, `int \| None`, `int`                                              | `Group`           | Create group, return instance    |
| `set_group_parent(group_id, parent_id)`                                  | `int`, `int \| None`                                                     | `None`            | Set/change group parent          |
| `get_role_ancestors(group_id)`                                           | `int`                                                                    | `list[int]`       | Get ancestor group IDs           |
| `get_group_children(group_id)`                                           | `int`                                                                    | `list[dict]`      | Get direct child groups          |
| `grant_object_perm(codename, model_name, object_id, user_id, group_id)`  | `str`, `str`, `str`, `int \| None`, `int \| None`                        | `None`            | Grant object permission          |
| `revoke_object_perm(codename, model_name, object_id, user_id, group_id)` | `str`, `str`, `str`, `int \| None`, `int \| None`                        | `None`            | Revoke object permission         |
| `get_objects_with_perm(user, codename, model_name)`                      | user, `str`, `str`                                                       | `list[str]`       | Get accessible object IDs        |
| `add_rule(codename, model_name, rule_type, rule_config, ...)`            | `str`, `str`, `str`, `dict`, ...                                         | `None`            | Add conditional rule             |
| `set_field_access(model_name, field_name, access, group_id, user_id)`    | `str`, `str`, `str`, `int \| None`, `int \| None`                        | `None`            | Set field access level           |
| `get_field_access(user, model_name)`                                     | user, `str`                                                              | `dict[str, str]`  | Get field access map             |
| `filter_fields(user, model_name, data, mode)`                            | user, `str`, `dict`, `str`                                               | `dict`            | Filter data by field perms       |
| `explain_effective_permissions(user_id)`                                 | `int`                                                                    | `dict`            | Full permission report           |
| `explain_permission_decision(user, perm, model_name, ...)`               | user, `str`, `str`, ...                                                  | `dict`            | Trace decision chain             |
| `export_policy()`                                                        | --                                                                       | `dict`            | Export complete RBAC policy      |
| `import_policy(data, clear_existing)`                                    | `dict`, `bool`                                                           | `dict`            | Import RBAC policy               |

### Imports Summary

```python
# Core auth
from hyperdjango.auth import (
    User,
    AnonymousUser,
    Group,
    Permission,
    ObjectPermission,
    PermissionRule,
    FieldPermission,
    UserGroup,
    UserPermission,
    GroupPermission,
    PermissionChecker,
    register_rule_type,
)

# Password hashing
from hyperdjango.auth import hash_password, verify_password, needs_rehash

# Password validation
from hyperdjango.auth.validators import (
    validate_password,
    validate_password_or_raise,
    get_password_help_texts,
    get_default_validators,
    PasswordValidationError,
    MinLengthValidator,
    MaxLengthValidator,
    NumericValidator,
    CommonPasswordValidator,
    UserAttributeSimilarityValidator,
)

# Sessions
from hyperdjango.auth import SessionAuth, session_auth
from hyperdjango.auth.sessions import (
    InMemorySessionStore,
    get_session_auth_hash,
    verify_session_auth_hash,
)
from hyperdjango.auth.db_sessions import DatabaseSessionStore

# API keys
from hyperdjango.auth import APIKeyAuth, api_key_auth

# Decorators
from hyperdjango.auth import (
    require_auth,
    require_staff,
    require_permission,
    require_api_key,
    require_oauth2,
)

# OAuth2
from hyperdjango.auth import OAuth2, OAuth2Provider, google, github, auth0
```
