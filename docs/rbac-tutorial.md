# RBAC Setup Tutorial

Step-by-step guide to setting up role-based access control (RBAC) in a HyperDjango app.

## Prerequisites

- HyperDjango installed with `uv sync`
- PostgreSQL running
- Native extension built: `uv run hyper-build`

## 1. Create Your App

```python
# app.py
from hyperdjango import HyperApp, Response, guard, Require, SessionAuth
from hyperdjango.auth.sessions import build_session_data
from hyperdjango.auth.user import User
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.signing import TokenEngine, SigningKey

app = HyperApp(title="RBAC Demo", database_url="postgres://localhost/rbac_demo")

token_engine = TokenEngine(keys=[SigningKey(secret="change-me-in-production")])
auth = SessionAuth(secret="change-me-in-production", token_engine=token_engine)
app.use(auth)
```

## 2. Create Tables

```bash
uv run hyper setup --app app:app
```

This creates all your Model tables plus the RBAC tables (`hyper_users`, `hyper_groups`, `hyper_permissions`, `hyper_user_groups`, `hyper_group_permissions`, etc.). It also auto-creates the `staff` and `superuser` groups.

## 3. Create a Superuser

```bash
uv run hyper createsuperuser --app app:app
```

This creates a user in `hyper_users` and auto-assigns them to the `staff` and `superuser` RBAC groups. The user can log into HyperAdmin and has all permissions.

## 4. Register Admin

```python
from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.fields import InlineConfig

admin = HyperAdmin(app, prefix="/admin")
admin.register_auth_models()  # User, Group, Permission CRUD
```

Start the server and navigate to `/admin/`. Log in with your superuser credentials.

## 5. Create Groups via Admin

Navigate to `/admin/groups/add/`:

- Create an **editor** group (name: `editor`)
- Create a **viewer** group (name: `viewer`)

Or programmatically in a seed file:

```python
from hyperdjango.auth.permissions import PermissionChecker


async def seed_rbac(db):
    checker = PermissionChecker(db)
    editor = await checker.ensure_group("editor")
    viewer = await checker.ensure_group("viewer")
```

## 6. Create Permissions

Navigate to `/admin/permissions/add/`:

- Create permission: codename=`change_article`, model_name=`article`
- Create permission: codename=`publish_article`, model_name=`article`

Or programmatically:

```python
await checker.create_default_permissions("article", "article")
# Creates: add_article, change_article, delete_article, view_article
```

## 7. Assign Permissions to Groups

Navigate to `/admin/groups/` and edit the **editor** group. In the GroupPermission inline, add the `change_article` and `publish_article` permissions.

Or programmatically:

```python
await checker.grant_group_perm(editor.id, "change_article", "article")
await checker.grant_group_perm(editor.id, "publish_article", "article")
```

## 8. Create Users and Assign Groups

Navigate to `/admin/users/add/` to create regular users. Then edit each user and add them to groups via the UserGroup inline.

Or programmatically:

```python
alice = await checker.create_user("alice", "alice123", email="alice@example.com")
await checker.add_user_to_group(alice.id, editor.id)
```

## 9. Protect Routes with Guards

```python
@app.get("/articles/{id}/edit")
@guard(Require.authenticated(), Require.role("editor"))
async def edit_article(request, id: int):
    """Only users in the 'editor' group can access this."""
    return Response.html("<h1>Edit Article</h1>")


@app.post("/articles/{id}/publish")
@guard(Require.authenticated(), Require.permission("publish_article"))
async def publish_article(request, id: int):
    """Only users with 'publish_article' permission can access this."""
    # ...
    return Response.json({"published": True})
```

### Guard Types

| Guard                            | Checks                                             | Example              |
| -------------------------------- | -------------------------------------------------- | -------------------- |
| `Require.authenticated()`        | User is logged in                                  | Login wall           |
| `Require.role("editor")`         | User is in named group                             | Role-based pages     |
| `Require.permission("codename")` | User has permission (superuser bypasses)           | Fine-grained actions |
| `Require.staff()`                | User in "staff" group or has staff timeline status | Admin areas          |
| `Require.superuser()`            | User in "superuser" group                          | System-level actions |
| `Require.any_of(a, b)`           | Either guard passes                                | OR composition       |

## 10. Login Flow with build_session_data()

```python
@app.post("/login")
async def login(request):
    body = await request.json()
    user = await User.objects.filter(username=body["username"]).first()
    if not user or not verify_password(body["password"], user.password_hash):
        return Response.json({"error": "Invalid credentials"}, status=401)

    # build_session_data auto-fetches RBAC groups from hyper_user_groups
    session = await build_session_data(user.id, app.db, username=user.username)
    resp = Response.json({"ok": True})
    auth.login(resp, session, request)
    return resp
```

After login, `request.user` is a `SessionUser` with:

- `user.groups` — `frozenset[str]` of group names (O(1) membership)
- `user.permissions` — `frozenset[str]` of permission codenames
- `user.in_group("editor")` — O(1) group check
- `user.has_perm("change_article")` — O(1) permission check (superuser bypasses)
- `user.is_staff` — derived from `"staff" in user.groups`
- `user.is_superuser` — derived from `"superuser" in user.groups`

## 11. Apps with Custom User Tables

If your app has its own user table (e.g., `hn_users`), pass explicit groups at login:

```python
# Map app roles to RBAC groups
role_groups = {
    "admin": ["admin", "staff", "editor"],
    "moderator": ["staff", "editor"],
    "user": [],
}

session = await build_session_data(
    user.id,
    db,
    groups=role_groups.get(user.role, []),
    username=user.username,
)
```

## 12. Field-Level Permissions

For per-field access control in REST APIs:

```python
class EmployeeViewSet(ModelViewSet):
    model = Employee
    serializer_class = EmployeeSerializer
    field_permissions_model = "employee"  # Enable field-level RBAC
```

Create `FieldPermission` entries via admin to hide/readonly specific fields per group:

| model_name | field_name | group_id     | access   |
| ---------- | ---------- | ------------ | -------- |
| employee   | salary     | viewer_group | hidden   |
| employee   | email      | viewer_group | readonly |

- **hidden**: field removed from API responses entirely
- **readonly**: field visible in responses but stripped from write requests
- **writable**: full access (default for all fields)

## 13. RBAC Admin Dashboard

Register the RBAC dashboard tools for visibility:

```python
admin.register_auth_models()  # User/Group/Permission CRUD
admin.register_ratelimit_models()  # Rate limit management
admin.register_cache_dashboard()  # Cache stats
```

Navigate to `/admin/` to see:

- **Permission Checker** — test what a user can access
- **Role Hierarchy** — visualize group inheritance
- **RBAC Overview** — groups, permissions, coverage stats
- **Policy Export** — export/import RBAC configuration

## Quick Reference

```python
# Guards
@guard(Require.role("editor"))              # Group membership
@guard(Require.permission("change_article")) # Permission check
@guard(Require.staff())                      # Staff group + timeline
@guard(Require.superuser())                  # Superuser group

# Session data
session = await build_session_data(user.id, db)           # Auto-fetch groups
session = await build_session_data(user.id, db, groups=["staff"])  # Explicit

# User properties (all O(1))
user.in_group("editor")        # frozenset membership
user.has_perm("change_article") # frozenset + superuser bypass
user.groups                     # frozenset[str]
user.permissions                # frozenset[str]
user.is_staff                   # "staff" in groups
user.is_superuser               # "superuser" in groups

# PermissionChecker
checker = PermissionChecker(db)
await checker.ensure_group("editor")
await checker.create_user("alice", "password123")
await checker.add_user_to_group(alice.id, editor.id)
await checker.has_perm(user, "change_article")
await checker.get_field_access(user, "employee")
```
