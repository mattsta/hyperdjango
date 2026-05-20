# Authentication & Authorization Guide

Comprehensive guide to HyperDjango's authentication system: session auth, API keys, OAuth2, password hashing, hierarchical RBAC, object-level permissions, and field-level access control.

HyperDjango uses argon2id exclusively for password hashing (never PBKDF2 or bcrypt), sessions for state management (never JWT), and PostgreSQL for all storage.

---

## Table of Contents

- [Quick Start](#quick-start)
- [User Model](#user-model)
- [Password Hashing](#password-hashing)
- [Session Authentication](#session-authentication)
- [API Key Authentication](#api-key-authentication)
- [OAuth2](#oauth2)
- [Permissions and Groups](#permissions-and-groups)
- [Role Hierarchy (RBAC)](#role-hierarchy-rbac)
- [Object-Level Permissions](#object-level-permissions)
- [Conditional Permission Rules](#conditional-permission-rules)
- [Field-Level Access Control](#field-level-access-control)
- [View Decorators](#view-decorators)
- [Auth Middleware](#auth-middleware)
- [Configuration Reference](#configuration-reference)
- [Migration Notes for Django Users](#migration-notes-for-django-users)

---

## Quick Start

```python
from hyperdjango import HyperApp
from hyperdjango.auth import (
    SessionAuth,
    hash_password,
    require_auth,
    require_permission,
    verify_password,
)
from hyperdjango.auth.db_sessions import DatabaseSessionStore
from hyperdjango.signing import SigningKey, TokenEngine

app = HyperApp("myapp", database="postgres://localhost/mydb")

# TokenEngine for signed session cookies (HMAC + salt + XOR obfuscation)
session_engine = TokenEngine(
    keys=[
        SigningKey(secret="your-session-signing-key-2026-q2", version=1),
    ]
)

# Database-backed sessions (PostgreSQL UNLOGGED table for performance)
store = DatabaseSessionStore(app.db, max_age=86400)
await store.ensure_table()
app.use(
    SessionAuth(
        secret="your-256-bit-secret-key-here",
        store=store,
        token_engine=session_engine,
    )
)
```

---

## User Model

HyperDjango provides a built-in User model stored in the `hyper_users` table:

```python
from hyperdjango.auth import User, AnonymousUser

# Create a user
user = User(
    username="alice",
    email="alice@example.com",
    is_active=True,
    is_staff=False,
    is_superuser=False,
)
user.set_password("secure-password-here")
await user.save()

# Verify password
if user.check_password("secure-password-here"):
    print("Password correct")

# Password rehashing (when argon2id parameters are upgraded)
if user.password_needs_rehash():
    user.set_password("secure-password-here")  # Re-hash with current params
    await user.save()

# User properties
print(user.is_authenticated)  # True
print(user.is_anonymous)  # False
print(user.full_name)  # "Alice Smith"

# Anonymous user (for unauthenticated requests)
anon = AnonymousUser()
print(anon.is_authenticated)  # False
print(anon.is_anonymous)  # True
```

### User Fields

| Field           | Type       | Description                                     |
| --------------- | ---------- | ----------------------------------------------- |
| `id`            | `int`      | Auto-increment primary key                      |
| `username`      | `str`      | Unique username                                 |
| `email`         | `str`      | Email address                                   |
| `password_hash` | `str`      | argon2id hash (never stored in plaintext)       |
| `first_name`    | `str`      | First name                                      |
| `last_name`     | `str`      | Last name                                       |
| `is_active`     | `bool`     | Account active flag                             |
| `is_staff`      | `bool`     | Staff access flag                               |
| `is_superuser`  | `bool`     | Superuser flag (bypasses all permission checks) |
| `last_login`    | `datetime` | Last login timestamp                            |
| `created_at`    | `datetime` | Account creation timestamp                      |

---

## Password Hashing

All passwords are hashed with argon2id via the `argon2-cffi` library. This is a memory-hard algorithm resistant to GPU and ASIC attacks.

```python
from hyperdjango.auth import hash_password, needs_rehash, verify_password

# Hash a password
hashed = hash_password("my-secure-password")
# "$argon2id$v=19$m=65536,t=3,p=4$..."

# Verify
is_valid = verify_password("my-secure-password", hashed)  # True
is_valid = verify_password("wrong-password", hashed)  # False

# Check if hash needs upgrading (e.g., argon2 params increased)
if needs_rehash(hashed):
    new_hash = hash_password("my-secure-password")
```

There is no option to use PBKDF2, bcrypt, or scrypt. argon2id is the only supported algorithm.

---

## Session Authentication

Sessions are stored server-side. The cookie contains only a signed session ID. Always use `TokenEngine` for cookie signing — it adds HMAC verification, per-token salt, and XOR obfuscation so forged cookies are rejected without touching the database.

### In-Memory Sessions (Development)

```python
from hyperdjango.auth import SessionAuth
from hyperdjango.signing import SigningKey, TokenEngine

session_engine = TokenEngine(
    keys=[
        SigningKey(secret="dev-session-signing-key", version=1),
    ]
)

# Fast, single-process. Sessions lost on restart.
app.use(SessionAuth(secret="dev-secret-key", token_engine=session_engine))
```

### Database Sessions (Production)

```python
import os

from hyperdjango.auth import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore
from hyperdjango.signing import SigningKey, TokenEngine

session_engine = TokenEngine(
    keys=[
        SigningKey(secret=os.environ["SESSION_SIGNING_KEY"], version=1),
    ]
)

# PostgreSQL UNLOGGED table -- fast writes, no WAL overhead
# Table DDL comes from HyperSession model via `hyper setup`
store = DatabaseSessionStore(max_age=86400)  # 24-hour sessions

app.use(
    SessionAuth(
        secret=os.environ["SESSION_SECRET"],
        store=store,
        token_engine=session_engine,
    )
)
```

### Key Rotation

Add new signing keys by prepending to the list. Old session cookies remain valid until their key is removed:

```python
session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=os.environ["SESSION_SIGNING_KEY_V2"], version=2
        ),  # signs new cookies
        SigningKey(
            secret=os.environ["SESSION_SIGNING_KEY_V1"], version=1
        ),  # still verifies old cookies
    ]
)
```

### Login / Logout Implementation

```python
from hyperdjango.auth import User, hash_password, verify_password
from hyperdjango.auth.sessions import get_session_auth_hash


@app.post("/api/login")
async def login(request):
    data = request.json
    username = data.get("username", "")
    password = data.get("password", "")

    try:
        user = await User.objects.get(username=username)
    except User.DoesNotExist:
        return Response.json({"error": "Invalid credentials"}, status=401)

    if not user.check_password(password):
        return Response.json({"error": "Invalid credentials"}, status=401)

    if not user.is_active:
        return Response.json({"error": "Account disabled"}, status=403)

    # Rehash if argon2 parameters were upgraded
    if user.password_needs_rehash():
        user.set_password(password)
        await user.save()

    # Store user data in session
    request.session["user_id"] = user.id
    request.session["username"] = user.username

    # Session auth hash: invalidates all sessions when password changes
    secret = app._middleware._stack[0].secret  # SessionAuth secret
    request.session["_session_auth_hash"] = get_session_auth_hash(
        user.password_hash, secret
    )

    return Response.json({"message": "Logged in", "user": user.username})


@app.post("/api/logout")
async def logout(request):
    request.session.clear()
    return Response.json({"message": "Logged out"})
```

### Session Auth Hash

When a user changes their password, all existing sessions are automatically invalidated:

```python
from hyperdjango.auth.sessions import get_session_auth_hash, verify_session_auth_hash

# Compute hash (stored in session on login)
session_hash = get_session_auth_hash(user.password_hash, secret)

# On each request, SessionAuth verifies the stored hash still matches
# If the user changed their password, the hash changes, and old sessions fail
is_valid = verify_session_auth_hash(
    session_hash=request.session.get("_session_auth_hash", ""),
    password_hash=user.password_hash,
    secret=secret,
)
```

---

## API Key Authentication

For machine-to-machine authentication. Use `SignedAPIKeyMixin` for database-backed keys with HMAC-first verification (rejects forgeries at 3.1M ops/sec without DB), or `APIKeyAuth` middleware for simple static keys.

### Database-Backed Keys with SignedAPIKeyMixin (Recommended)

Define an API key model with `SignedAPIKeyMixin`:

```python
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field


class APIKey(SignedAPIKeyMixin, TimestampMixin):
    class Meta:
        table = "api_keys"

    class TokenConfig:
        keys = [SigningKey(secret="apikey-signing-2026-q2", version=1)]
        key_display_prefix = "sk_myapp_"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field()
    name: str = Field(default="")
```

The mixin provides `key_hash`, `key_prefix`, `is_active`, `expires_at`, `scopes` fields automatically.

**Generate a key** (show the raw key to the user once):

```python
result = await APIKey.generate(user_id=user.id, name="Production")
# result.raw_key = "sk_myapp_2rKx9mP4qR7..." (show once, never stored)
# result.instance.key_hash = "a3f8..." (SHA-256 hash, stored in DB)
```

**Verify an incoming key** (two-phase: HMAC first, then DB):

```python
api_key = await APIKey.verify(
    request.headers.get("authorization", "").removeprefix("Bearer ")
)
if api_key is None:
    raise HTTPException(401, "Invalid API key")
# api_key.user_id, api_key.scopes, api_key.is_active, etc.
```

**Fast rejection in middleware** (no DB hit):

```python
if not APIKey.verify_signature_only(raw_key):
    raise HTTPException(401, "Invalid key")  # Forgery rejected instantly
```

### Static Key List (Simple Admin Endpoints)

For simple static keys without a database model:

```python
from hyperdjango.auth import APIKeyAuth

app.use(
    APIKeyAuth(
        valid_keys={"sk_live_abc123def456", "sk_live_xyz789ghi012"},
        header="X-API-Key",
    )
)
```

### Using API Keys in Views

```python
from hyperdjango.auth import require_api_key


@app.post("/api/webhooks/stripe")
@require_api_key
async def stripe_webhook(request):
    # request.api_key contains the raw key
    # request.api_key_valid is True if validated
    data = request.json
    await process_stripe_event(data)
    return {"received": True}
```

---

## OAuth2

Server-side OAuth2 Authorization Code Flow. Tokens stored in sessions, never exposed to the client.

### Provider Setup

```python
from hyperdjango.auth import OAuth2, google, github, auth0

oauth = OAuth2(secret="oauth-state-signing-secret")

# Google
oauth.add_provider(
    google(
        client_id="your-google-client-id.apps.googleusercontent.com",
        client_secret="your-google-client-secret",
    )
)

# GitHub
oauth.add_provider(
    github(
        client_id="your-github-client-id",
        client_secret="your-github-client-secret",
    )
)

# Auth0
oauth.add_provider(
    auth0(
        client_id="your-auth0-client-id",
        client_secret="your-auth0-client-secret",
        domain="your-tenant.auth0.com",
    )
)

app.use(oauth)
```

This automatically registers routes:

- `GET /auth/{provider}/login` -- Redirects to provider
- `GET /auth/{provider}/callback` -- Handles the callback

### Custom Provider

```python
from hyperdjango.auth import OAuth2Provider

custom_provider = OAuth2Provider(
    name="corporate-sso",
    client_id="...",
    client_secret="...",
    authorize_url="https://sso.corp.com/oauth2/authorize",
    token_url="https://sso.corp.com/oauth2/token",
    userinfo_url="https://sso.corp.com/oauth2/userinfo",
    scopes=["openid", "email", "profile"],
    id_field="sub",
    email_field="email",
    name_field="name",
)
oauth.add_provider(custom_provider)
```

### Protecting Views with OAuth2

```python
from hyperdjango.auth import require_oauth2


@app.get("/api/dashboard")
@require_oauth2()
async def dashboard(request):
    # request.user contains the OAuth2 user info dict
    # request.oauth2_provider contains the provider name
    return {
        "user": request.user,
        "provider": request.oauth2_provider,
    }
```

---

## Permissions and Groups

### Creating Permissions

```python
from hyperdjango.auth import Permission, PermissionChecker

checker = PermissionChecker(app.db)
await checker.ensure_tables()

# Create permissions (typically done once during setup)
await Permission.objects.create(codename="add_product", name="Can add product")
await Permission.objects.create(codename="change_product", name="Can change product")
await Permission.objects.create(codename="delete_product", name="Can delete product")
await Permission.objects.create(codename="view_product", name="Can view product")
```

### Groups

```python
from hyperdjango.auth import Group

# Create groups
editors_group = await Group.objects.create(name="editors")
viewers_group = await Group.objects.create(name="viewers")

# Assign permissions to groups (via PermissionChecker)
await checker.grant_perm(group_id=editors_group.id, codename="add_product")
await checker.grant_perm(group_id=editors_group.id, codename="change_product")
await checker.grant_perm(group_id=viewers_group.id, codename="view_product")

# Add user to group
await checker.add_user_to_group(user_id=user.id, group_id=editors_group.id)
```

### Checking Permissions

```python
# Simple permission check
can_edit = await checker.has_perm(user, "change_product")

# Multiple permissions
can_manage = await checker.has_perms(
    user, ["add_product", "change_product", "delete_product"]
)


# In views
@app.post("/api/products")
@require_permission("add_product")
async def create_product(request): ...
```

---

## Role Hierarchy (RBAC)

Groups can inherit from parent groups using recursive CTE queries. A child group automatically has all permissions of its ancestors.

```python
# Create hierarchy: admin -> editor -> viewer
viewer_id = (await Group.objects.create(name="viewer")).id
editor = await checker.create_group("editor", parent_id=viewer_id)
admin = await checker.create_group("admin", parent_id=editor.id)

# Grant permissions at each level
await checker.grant_perm(group_id=viewer_id, codename="view_product")
await checker.grant_perm(group_id=editor.id, codename="change_product")
await checker.grant_perm(group_id=admin.id, codename="delete_product")

# A user in the "admin" group inherits all permissions:
# - delete_product (direct)
# - change_product (from editor)
# - view_product (from viewer)
await checker.add_user_to_group(user_id=admin_user.id, group_id=admin.id)
assert await checker.has_perm(admin_user, "view_product")  # True (inherited)
assert await checker.has_perm(admin_user, "change_product")  # True (inherited)
assert await checker.has_perm(admin_user, "delete_product")  # True (direct)
```

### Explain Permissions

Debug why a user has (or lacks) a permission:

```python
explanation = await checker.explain_perm(user, "change_product")
# Returns the grant path: user -> group -> parent_group -> permission
```

---

## Object-Level Permissions

Control access to individual rows (records):

```python
# Grant: user 1 can edit post #42
await checker.grant_object_perm(
    user_id=1,
    codename="change_post",
    model_name="post",
    object_id="42",
)

# Check
can_edit = await checker.has_object_perm(user, "change_post", "post", "42")

# Group-level object permissions
await checker.grant_object_perm(
    group_id=editors_group.id,
    codename="change_post",
    model_name="post",
    object_id="42",
)
```

### Real-World Example: Multi-Author Blog

```python
@app.put("/api/posts/{id:int}")
@require_auth()
async def update_post(request, id: int):
    post = await get_object_or_404(Post, id=id)

    # Superusers can edit anything
    if not request.user.is_superuser:
        can_edit = await checker.has_object_perm(
            request.user, "change_post", "post", str(id)
        )
        if not can_edit:
            raise HTTPException(403, "You do not have permission to edit this post")

    data = request.json
    await Post.objects.filter(id=id).update(**data)
    return {"updated": True}
```

---

## Conditional Permission Rules

Attach conditions to permissions that are evaluated at runtime:

### is_owner Rule

```python
# Grant "change_post" only when user owns the post
await checker.add_rule(
    perm_id=change_post_perm.id,
    rule_type="is_owner",
    config={"owner_field": "author_id"},
    group_id=editors_group.id,
)

# Check with the object context
can_edit = await checker.has_perm_with_rules(
    user, "change_post", "post", obj=post, request=request
)
# True only if post.author_id == user.id
```

### time_window Rule

```python
# Allow changes only during business hours
await checker.add_rule(
    perm_id=perm.id,
    rule_type="time_window",
    config={"start_hour": 9, "end_hour": 17, "timezone": "US/Eastern"},
    group_id=group_id,
)
```

### ip_range Rule

```python
# Allow only from office network
await checker.add_rule(
    perm_id=perm.id,
    rule_type="ip_range",
    config={"ranges": ["10.0.0.0/8", "192.168.1.0/24"]},
    group_id=group_id,
)
```

### field_match Rule

```python
# Allow only when order status is "draft"
await checker.add_rule(
    perm_id=perm.id,
    rule_type="field_match",
    config={"field": "status", "values": ["draft", "pending"]},
    group_id=group_id,
)
```

### Custom Rule Types

```python
from hyperdjango.auth import register_rule_type


async def check_subscription(user, obj, request, config):
    """Only allow if user has an active subscription."""
    sub = await Subscription.objects.filter(user_id=user.id, is_active=True).first()
    return sub is not None and sub.plan in config.get("allowed_plans", [])


register_rule_type("subscription_check", check_subscription)

await checker.add_rule(
    perm_id=perm.id,
    rule_type="subscription_check",
    config={"allowed_plans": ["pro", "enterprise"]},
    group_id=group_id,
)
```

---

## Field-Level Access Control

Control which fields are visible or editable per role:

```python
# Viewers can see employee name but not salary
await checker.set_field_access(
    "employee", "salary", group_id=viewer.id, access="hidden"
)
await checker.set_field_access(
    "employee", "salary", group_id=editor.id, access="readonly"
)
await checker.set_field_access(
    "employee", "salary", group_id=admin.id, access="writable"
)

# Get field access map for a user
field_access = await checker.get_field_access(user, "employee")
# {"name": "writable", "salary": "hidden", "department": "writable", ...}

# Filter data before sending to client
data = {"name": "Alice", "salary": 95000, "department": "Engineering"}
filtered = await checker.filter_fields(user, "employee", data, mode="read")
# For a viewer: {"name": "Alice", "department": "Engineering"}
# salary is removed because access is "hidden"
```

---

## View Decorators

### @require_auth

```python
from hyperdjango.auth import require_auth


# Basic authentication check
@app.get("/api/profile")
@require_auth()
async def profile(request):
    return {"user": request.user}


# Custom auth check function
@app.get("/api/admin")
@require_auth(lambda r: r.user and r.user.is_staff)
async def admin_panel(request): ...
```

### @require_permission

```python
from hyperdjango.auth import require_permission


@app.post("/api/products")
@require_permission("add_product")
async def create_product(request): ...


# Multiple permissions (all required)
@app.delete("/api/products/{id:int}")
@require_permission("delete_product", "manage_inventory")
async def delete_product(request, id: int): ...
```

### @require_staff

```python
from hyperdjango.auth import require_staff


@app.get("/api/admin/stats")
@require_staff
async def admin_stats(request):
    """Only users with is_staff=True can access."""
    ...
```

### @require_api_key

```python
from hyperdjango.auth import require_api_key


@app.post("/api/webhooks")
@require_api_key
async def webhook_handler(request):
    """Requires a valid API key in the X-API-Key header."""
    ...
```

---

## Auth Middleware

`SessionAuth` combines session auth and permission checking. Pass `db=` to
attach the RBAC `PermissionChecker` used by `@require_permission`:

```python
from hyperdjango.auth import SessionAuth, PermissionChecker
from hyperdjango.auth.db_sessions import DatabaseSessionStore

await PermissionChecker(app.db).ensure_tables()

app.use(SessionAuth(secret="...", store=DatabaseSessionStore(app.db), db=app.db))
```

This populates `request.user` on every request (a `SessionUser` or `AnonymousUser`)
and, with `db=`, `request._perm_checker` for permission decorators.

---

## Configuration Reference

### SessionAuth Parameters

| Parameter         | Type          | Default                | Description                          |
| ----------------- | ------------- | ---------------------- | ------------------------------------ |
| `secret`          | `str`         | required               | HMAC signing key for session cookies |
| `store`           | session store | `InMemorySessionStore` | Session backend                      |
| `cookie_name`     | `str`         | `"session"`            | Cookie name                          |
| `cookie_path`     | `str`         | `"/"`                  | Cookie path                          |
| `cookie_httponly` | `bool`        | `True`                 | HttpOnly flag                        |
| `cookie_secure`   | `bool`        | `False`                | Secure flag (set True in production) |
| `cookie_samesite` | `str`         | `"Lax"`                | SameSite attribute                   |

### DatabaseSessionStore Parameters

| Parameter    | Type       | Default            | Description                      |
| ------------ | ---------- | ------------------ | -------------------------------- |
| `db`         | `Database` | required           | Database connection              |
| `max_age`    | `int`      | `86400`            | Session lifetime in seconds      |
| `table_name` | `str`      | `"hyper_sessions"` | PostgreSQL table name (UNLOGGED) |

### APIKeyAuth Parameters

| Parameter       | Type       | Default       | Description                          |
| --------------- | ---------- | ------------- | ------------------------------------ |
| `valid_keys`    | `set[str]` | `None`        | Set of valid API keys                |
| `header`        | `str`      | `"x-api-key"` | HTTP header to check                 |
| `query_param`   | `str`      | `None`        | Query parameter to check             |
| `validate_func` | `callable` | `None`        | Async function for custom validation |

---

## Migration Notes for Django Users

### Key Differences

| Django                           | HyperDjango                                        |
| -------------------------------- | -------------------------------------------------- |
| PBKDF2 (default)                 | argon2id (only option)                             |
| JWT support (via packages)       | No JWT -- sessions only                            |
| PostgreSQL UNLOGGED sessions     | Built-in                                           |
| `@login_required`                | `@require_auth()` (note the parentheses)           |
| `@permission_required("perm")`   | `@require_permission("perm")`                      |
| `user.has_perm("app.perm")`      | `await checker.has_perm(user, "perm")`             |
| Flat permissions only            | Hierarchical RBAC with CTE inheritance             |
| No object-level perms (built-in) | `checker.has_object_perm(...)` built-in            |
| No field-level perms             | `checker.get_field_access(...)` built-in           |
| `contrib.auth` tables            | `hyper_users`, `hyper_groups`, `hyper_permissions` |
| Sync permission checks           | Async permission checks                            |

### What Stayed the Same

- `User` model with `username`, `email`, `is_active`, `is_staff`, `is_superuser`
- `AnonymousUser` with matching interface
- `set_password()` / `check_password()` methods
- `Group` and `Permission` models
- Decorator-based view protection pattern
- Session-based authentication (no JWT)

---

## Custom Authentication

### Writing Custom Auth Backends

A custom authentication backend is an async function (or class) that takes
credentials and returns a `User` instance or `None`, used from a login handler
alongside the default session-based auth.

**Backend Interface:**

```python
from hyperdjango.auth import User


async def authenticate(request, **credentials) -> User | None:
    """Attempt to authenticate with the given credentials.

    Return a User instance on success, None on failure.
    """
    ...
```

**Example: Email-Based Authentication Backend**

The default backend authenticates by username. This backend allows users to
log in with their email address instead:

```python
from hyperdjango.auth import User, verify_password


async def email_auth_backend(
    request, email: str = "", password: str = ""
) -> User | None:
    """Authenticate using email + password instead of username."""
    if not email or not password:
        return None
    try:
        user = await User.objects.filter(email=email).get()
    except User.DoesNotExist:
        return None

    if not user.is_active:
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user
```

**Example: Header-Based Authentication (Reverse Proxy)**

When running behind a trusted reverse proxy that handles authentication
(e.g., AWS ALB with OIDC, Cloudflare Access), the proxy sets a header
with the authenticated user's identity:

```python
from hyperdjango.auth import User


async def proxy_auth_backend(request, **kwargs) -> User | None:
    """Trust the X-Forwarded-User header from a reverse proxy.

    Only use this behind a trusted proxy that strips this header from
    external requests. Your proxy configuration must prevent clients
    from setting this header directly.
    """
    forwarded_user = request.headers.get("x-forwarded-user")
    if not forwarded_user:
        return None

    try:
        return await User.objects.filter(email=forwarded_user).get()
    except User.DoesNotExist:
        # Auto-provision users from proxy auth
        user = User(
            username=forwarded_user.split("@")[0],
            email=forwarded_user,
            is_active=True,
        )
        await user.save()
        return user
```

**Registering Multiple Backends:**

```python
@app.post("/api/login")
async def login(request):
    data = request.json
    username = data.get("username", "")
    email = data.get("email", "")
    password = data.get("password", "")

    user = None

    # Try email backend first
    if email:
        user = await email_auth_backend(request, email=email, password=password)

    # Fall back to username backend
    if user is None and username:
        user = await username_auth_backend(
            request, username=username, password=password
        )

    if user is None:
        return Response.json({"error": "Invalid credentials"}, status=401)

    # Create session
    request.session["user_id"] = user.id
    return Response.json({"message": "Logged in", "user": user.username})
```

### External Auth Integration Patterns

#### LDAP / Active Directory

For organizations with existing LDAP directory services, authenticate against
the LDAP server and map users to HyperDjango `User` records:

```python
import ldap3
from hyperdjango.auth import User


LDAP_SERVER = "ldaps://ldap.corp.example.com:636"
LDAP_BASE_DN = "dc=corp,dc=example,dc=com"
LDAP_USER_DN_TEMPLATE = "uid={username},ou=people,dc=corp,dc=example,dc=com"


async def ldap_auth_backend(
    request, username: str = "", password: str = ""
) -> User | None:
    """Authenticate against an LDAP directory and sync to local User table."""
    if not username or not password:
        return None

    user_dn = LDAP_USER_DN_TEMPLATE.format(username=username)
    server = ldap3.Server(LDAP_SERVER, use_ssl=True)

    try:
        conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True)
    except ldap3.core.exceptions.LDAPBindError:
        return None

    # Search for user attributes
    conn.search(
        LDAP_BASE_DN,
        f"(uid={ldap3.utils.conv.escape_filter_chars(username)})",
        attributes=["mail", "givenName", "sn", "memberOf"],
    )

    if not conn.entries:
        conn.unbind()
        return None

    entry = conn.entries[0]
    email = str(entry.mail) if entry.mail else f"{username}@corp.example.com"
    first_name = str(entry.givenName) if entry.givenName else ""
    last_name = str(entry.sn) if entry.sn else ""
    conn.unbind()

    # Get or create local user record
    try:
        user = await User.objects.filter(username=username).get()
        # Sync LDAP attributes on each login
        user.email = email
        user.first_name = first_name
        user.last_name = last_name
        await user.save()
    except User.DoesNotExist:
        user = User(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        user.set_password(password)  # Cache password locally for offline auth
        await user.save()

    return user
```

#### SAML 2.0

SAML integration follows the standard SP-initiated flow. The Identity Provider
(IdP) sends a signed assertion to your callback endpoint. Parse and verify the
assertion, then create or update the local user:

```python
from hyperdjango.auth import User
from hyperdjango.response import Response


@app.post("/auth/saml/callback")
async def saml_callback(request):
    """Handle SAML assertion from the Identity Provider.

    The SAML library (e.g., python3-saml) handles XML parsing,
    signature verification, and assertion extraction.
    """
    saml_response = request.form.get("SAMLResponse", "")

    # Use your SAML library to parse and verify
    auth = init_saml_auth(request)
    auth.process_response()
    errors = auth.get_errors()

    if errors:
        return Response.json(
            {"error": "SAML validation failed", "details": errors}, status=400
        )

    if not auth.is_authenticated():
        return Response.json({"error": "Authentication failed"}, status=401)

    # Extract user attributes from the SAML assertion
    attributes = auth.get_attributes()
    name_id = auth.get_nameid()
    email = attributes.get("email", [name_id])[0]
    first_name = attributes.get("firstName", [""])[0]
    last_name = attributes.get("lastName", [""])[0]

    # Get or create the local user
    try:
        user = await User.objects.filter(email=email).get()
    except User.DoesNotExist:
        user = User(
            username=email.split("@")[0],
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        await user.save()

    # Map SAML groups to HyperDjango groups
    saml_groups = attributes.get("groups", [])
    checker = PermissionChecker(app.db)
    for group_name in saml_groups:
        try:
            group = await Group.objects.filter(name=group_name).get()
            await checker.add_user_to_group(user_id=user.id, group_id=group.id)
        except Group.DoesNotExist:
            pass  # Skip unmapped groups

    # Create session
    request.session["user_id"] = user.id
    request.session["auth_method"] = "saml"
    return Response.redirect("/dashboard")
```

### Token Verification Patterns

For API-to-API communication where session cookies are not practical, use
signed tokens that are verified on each request. HyperDjango uses API keys
(not JWT) for this purpose.

**Database-Backed API Key Verification:**

```python
import hashlib
from datetime import UTC, datetime

from hyperdjango.auth import User


async def verify_api_token(request) -> User | None:
    """Verify an API key from the Authorization header and resolve the owning user.

    API keys are stored as SHA-256 hashes in the database. The raw key
    is never stored.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    raw_key = auth_header[7:]  # Strip "Bearer " prefix
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    rows = await app.db.query(
        "SELECT user_id, expires_at, scopes FROM api_keys "
        "WHERE key_hash = $1 AND is_active = true",
        key_hash,
    )
    if not rows:
        return None

    row = rows[0]
    expires_at = row["expires_at"]
    if expires_at and expires_at < datetime.now(UTC):
        return None

    try:
        user = await User.objects.get(id=row["user_id"])
    except User.DoesNotExist:
        return None

    # Attach scopes to the request for permission checking
    request.token_scopes = row["scopes"] or []
    return user
```

**Scoped Token Authorization:**

```python
from hyperdjango.app import HTTPException


@app.get("/api/billing/invoices")
async def list_invoices(request):
    """Requires a token with the 'billing:read' scope."""
    user = await verify_api_token(request)
    if user is None:
        raise HTTPException(401, "Invalid or expired API key")

    if "billing:read" not in request.token_scopes:
        raise HTTPException(403, "Token missing required scope: billing:read")

    invoices = (
        await Invoice.objects.filter(customer_id=user.id).order_by("-created_at").all()
    )
    return {
        "invoices": [
            {"id": inv.id, "total": float(inv.total), "status": inv.status}
            for inv in invoices
        ]
    }
```

**Token Rotation:**

```python
@app.post("/api/keys/rotate")
@require_auth()
async def rotate_api_key(request):
    """Revoke the current API key and issue a new one."""
    import secrets

    old_key_hash = hashlib.sha256(
        request.headers.get("authorization", "")[7:].encode()
    ).hexdigest()

    # Revoke old key
    await app.db.execute(
        "UPDATE api_keys SET is_active = false WHERE key_hash = $1",
        old_key_hash,
    )

    # Generate new key
    new_key = f"sk_live_{secrets.token_urlsafe(32)}"
    new_hash = hashlib.sha256(new_key.encode()).hexdigest()

    await app.db.execute(
        "INSERT INTO api_keys (user_id, key_hash, scopes, is_active, created_at) "
        "VALUES ($1, $2, $3, true, NOW())",
        request.user.id,
        new_hash,
        request.token_scopes,
    )

    return Response.json(
        {
            "key": new_key,  # Only time the raw key is exposed
            "message": "Store this key securely. It will not be shown again.",
        }
    )
```
