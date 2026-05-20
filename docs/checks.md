# System Checks

Validate configuration at startup to catch errors early. The check framework runs registered checks before the server starts and reports issues by severity.

## Quick Start

```bash
uv run hyper check
uv run hyper check --deploy  # include deployment checks
```

```python
from hyperdjango.checks import register, CheckMessage, run_checks


# Register a custom check
@register("myapp")
def check_api_key(app):
    import os

    if not os.environ.get("API_KEY"):
        return [
            CheckMessage(
                level="warning",
                msg="API_KEY not configured",
                hint="Set API_KEY environment variable",
                id="myapp.W001",
            )
        ]
    return []


# Run all checks programmatically
messages = run_checks(app)
for msg in messages:
    if msg.is_serious:
        print(f"[{msg.level.upper()}] {msg}")
```

## CheckMessage

Each check result is a `CheckMessage` with a severity level:

```python
from hyperdjango.checks import CheckMessage

msg = CheckMessage(
    level="error",  # "info", "warning", "error", or "critical"
    msg="Database pool too small",
    hint="Increase pool_size to at least 10 for production",
    id="database.E001",  # unique identifier
    obj=db,  # optional: the object with the issue
)
```

| Level      | Meaning             | Blocks Startup     |
| ---------- | ------------------- | ------------------ |
| `info`     | Informational note  | No                 |
| `warning`  | Potential issue     | No                 |
| `error`    | Configuration error | Yes (in prod mode) |
| `critical` | Cannot start        | Yes                |

### Properties

| Property         | Type   | Description                          |
| ---------------- | ------ | ------------------------------------ |
| `msg.is_serious` | `bool` | `True` for error and critical levels |
| `str(msg)`       | `str`  | Formatted message with ID and hint   |

## @register() Decorator

Register a check function with an optional tag for categorization:

```python
from hyperdjango.checks import register, CheckMessage


@register("security")
def check_csrf_middleware(app):
    """Verify CSRF middleware is configured."""
    if not has_csrf_middleware(app):
        return [
            CheckMessage(
                level="warning",
                msg="CSRFMiddleware not configured",
                hint="Add app.use(CSRFMiddleware(secret=...)) for CSRF protection",
                id="security.W010",
            )
        ]
    return []


@register("database")
def check_pool_size(app):
    """Verify database pool is large enough for production."""
    if app._db and app._db.max_size < 5:
        return [
            CheckMessage(
                level="warning",
                msg=f"Database pool size is {app._db.max_size} (recommended: 10+)",
                hint="Increase pool_size in database URL or Database() constructor",
                id="database.W001",
            )
        ]
    return []


@register()  # no tag = runs with all checks
def check_python_version(app):
    import sys

    if sys.version_info < (3, 14):
        return [
            CheckMessage(
                level="warning",
                msg=f"Python {sys.version_info.major}.{sys.version_info.minor} detected",
                hint="HyperDjango is optimized for Python 3.14+ (free-threaded)",
                id="python.W001",
            )
        ]
    return []
```

## run_checks()

Run all registered checks:

```python
from hyperdjango.checks import run_checks, get_check_count

messages = run_checks(app)

# Filter by severity
errors = [m for m in messages if m.level in ("error", "critical")]
warnings = [m for m in messages if m.level == "warning"]

# Get counts
counts = get_check_count(messages)
# {"critical": 0, "error": 1, "warning": 3, "info": 2}

# Include deployment-specific checks
all_messages = run_checks(app, include_deployment=True)

# Run only specific tags
db_messages = run_checks(app, tags=["database"])
```

## Built-in Checks

### Security Checks

| ID              | Level   | Check                             |
| --------------- | ------- | --------------------------------- |
| `security.W001` | warning | SECRET_KEY not set                |
| `security.W002` | warning | SECRET_KEY too short (< 50 chars) |
| `security.W003` | warning | No middleware configured          |

### Database Checks

| ID              | Level | Check                  |
| --------------- | ----- | ---------------------- |
| `database.I001` | info  | No database configured |

### Deployment Checks

Only run with `--deploy` flag or `include_deployment=True`:

| ID                | Level   | Check                        |
| ----------------- | ------- | ---------------------------- |
| `deployment.E001` | error   | HYPER_DEBUG=1 in production  |
| `deployment.W001` | warning | ALLOWED_HOSTS not configured |

## Writing Custom Checks

### Model Validation

```python
@register("models")
def check_model_tables(app):
    """Verify all model tables exist in the database."""
    messages = []
    for model in get_registered_models():
        # Could async check table existence here
        if not model._meta.table:
            messages.append(
                CheckMessage(
                    level="error",
                    msg=f"Model {model.__name__} has no table configured",
                    hint="Add class Meta: table = 'table_name'",
                    id="models.E001",
                    obj=model,
                )
            )
    return messages
```

### RBAC Configuration

```python
@register("auth")
def check_rbac_tables(app):
    """Verify RBAC tables are created."""
    messages = []
    if app._db is None:
        return messages
    required_tables = ["hyper_users", "hyper_groups", "hyper_permissions"]
    # Check tables exist...
    return messages
```

### Custom Application Check

```python
@register("billing")
def check_stripe_key(app):
    import os

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return [
            CheckMessage(
                level="error",
                msg="STRIPE_SECRET_KEY not configured",
                hint="Set STRIPE_SECRET_KEY environment variable",
                id="billing.E001",
            )
        ]
    if key.startswith("sk_test_"):
        return [
            CheckMessage(
                level="warning",
                msg="Using Stripe test key in production",
                hint="Set STRIPE_SECRET_KEY to a live key",
                id="billing.W001",
            )
        ]
    return []
```

## CLI Usage

```bash
# Run all checks
uv run hyper check

# Output:
# System check identified 2 issues:
#
# (security.W001) SECRET_KEY environment variable is not set
#     HINT: Set SECRET_KEY to a random string of 50+ characters
#
# (database.I001) No database configured
#     HINT: Pass database= to HyperApp() for database features
#
# 0 errors, 1 warning, 1 info

# Include deployment checks
uv run hyper check --deploy

# Run specific tags only
uv run hyper check --tag security --tag database
```

## Integration with app.run(prod=True)

When running in production mode, checks are run automatically at startup. Any `error` or `critical` messages prevent the server from starting:

```python
app.run(prod=True)
# Runs: run_checks(app, include_deployment=True)
# If any errors: prints messages and sys.exit(1)
```
