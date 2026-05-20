# Cryptographic Signing

Generate and verify tamper-proof signed values for tokens, cookies, and URLs. Uses HMAC-SHA256 with constant-time comparison to prevent timing attacks.

## TokenEngine — Signed Tokens for Sessions & API Keys

The `TokenEngine` is HyperDjango's recommended approach for session cookies, API keys, and stateless data tokens. It provides HMAC-signed tokens with rolling key rotation, per-token salting, XOR obfuscation, randomized key ordering, and optional payload padding.

```python
from hyperdjango.signing import (
    TokenEngine,
    SigningKey,
    SignedSessionMixin,
    SignedAPIKeyMixin,
)
```

### Why TokenEngine?

Standard session tokens (`secrets.token_urlsafe`) are random strings — any valid-looking string hits the database. TokenEngine adds an HMAC signature so forged tokens are rejected instantly without a database query:

```python
engine = TokenEngine(
    keys=[
        SigningKey(secret="app-key-2026-q2", version=2),  # newest — signs new tokens
        SigningKey(secret="app-key-2026-q1", version=1),  # still accepted for decoding
    ]
)

# Signed reference token (for session IDs, API key references)
token = engine.encode_ref("sess_abc123def456")
# → "2rKx9mP4qR7wN8..." (base62, URL-safe, cookie-safe)

ref = engine.decode_ref(token)
# → "sess_abc123def456" (or None if forged/tampered/wrong key)

# Signed data token (stateless — any server can verify without DB)
token = engine.encode_data({"user_id": 42, "role": "admin"}, ttl=3600)
data = engine.decode_data(token)
# → {"user_id": 42, "role": "admin"} (or None if expired/invalid)
```

### Token Format

Tokens are pure alphanumeric + `.` separator — safe for URLs, cookies, headers, and query strings:

```
{version_char}{type_char}{base62(xor(salt + payload))}.{base62(hmac_signature)}
```

| Part         | Description                                             |
| ------------ | ------------------------------------------------------- |
| Version char | Single base62 char (0-61) for O(1) key lookup on decode |
| Type char    | `r` = reference token, `d` = data token                 |
| Payload      | XOR-obfuscated with per-token mask (salt-derived)       |
| Signature    | Truncated HMAC-SHA256 (default 8 bytes)                 |

### Security Properties

**HMAC integrity** — tokens cannot be forged without the secret key. Constant-time `hmac.compare_digest()` prevents timing attacks.

**Per-token random salt** (default 8 bytes) — identical inputs produce different tokens each time. Blocks frequency analysis and known-plaintext attacks against the XOR stream.

**Per-token XOR mask** — derived from `HMAC(key, domain + salt)`. Each token has a unique XOR stream. Recovering one token's mask reveals nothing about any other token.

**Randomized JSON key ordering** — when salt is active, data token fields are shuffled per token. Observers can't correlate field positions across tokens even knowing the schema.

**Optional payload padding** — `pad_to_bucket=True` rounds payloads to fixed-size buckets (16/32/64/128/256/512/1024/2048/4096 bytes) with random fill. Hides exact payload size.

**Key rotation** — encode with newest key (index 0), decode tries all keys. Old tokens remain valid until their key is removed from the list.

### Configuration

```python
engine = TokenEngine(
    keys=[  # Required: newest first
        SigningKey(secret="key-v2", version=2),
        SigningKey(secret="key-v1", version=1),
    ],
    signature_bytes=8,  # HMAC truncation (default 8 = 64 bits)
    salt_bytes=8,  # Per-token salt (default 8, 0 = deterministic)
    pad_to_bucket=False,  # Payload padding (default False)
)
```

| Parameter         | Default  | Description                              |
| ----------------- | -------- | ---------------------------------------- |
| `keys`            | required | List of `SigningKey`, newest first       |
| `signature_bytes` | 8        | HMAC truncation length in bytes          |
| `salt_bytes`      | 8        | Random salt bytes per token (0 disables) |
| `pad_to_bucket`   | False    | Pad payloads to fixed-size buckets       |

### Key Rotation

Add new keys by prepending to the list. Old tokens remain valid:

```python
# Phase 1: Deploy with both keys
engine = TokenEngine(
    keys=[
        SigningKey(secret="new-key-2026-q3", version=2),  # signs new tokens
        SigningKey(secret="old-key-2026-q2", version=1),  # still verifies old tokens
    ]
)

# Phase 2: After all old tokens have expired, remove old key
engine = TokenEngine(
    keys=[
        SigningKey(secret="new-key-2026-q3", version=2),
    ]
)
```

### Reference Tokens vs Data Tokens

|                | Reference (`encode_ref`)              | Data (`encode_data`)                       |
| -------------- | ------------------------------------- | ------------------------------------------ |
| **Purpose**    | DB lookup (sessions, API keys)        | Stateless verification                     |
| **Contains**   | Opaque string (session ID, key ref)   | Key-value dict                             |
| **DB needed?** | Yes, to fetch the referenced data     | No — data is in the token                  |
| **TTL**        | Controlled by DB expiry               | Built-in `_exp` timestamp                  |
| **Use cases**  | Session cookies, API key verification | Email verify links, CSRF, short-lived auth |

### SignedSessionMixin

Model mixin that adds a `token` field and auto-generates signed session tokens:

```python
from hyperdjango.signing import SignedSessionMixin, SigningKey
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field


class Session(SignedSessionMixin, TimestampMixin):
    class Meta:
        table = "sessions"

    class TokenConfig:
        keys = [SigningKey(secret="sess-2026-q2", version=1)]
        # Optional:
        # salt_bytes = 16        # More salt (default 8)
        # pad_to_bucket = True   # Hide payload size
        # signature_bytes = 12   # Longer HMAC

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field()
    data: str = Field(default="{}")
```

**What the mixin provides:**

- `token: str` field (unique, indexed) — auto-generated `secrets.token_urlsafe(32)` on first `save()`
- `session.signed_token` property — returns HMAC-signed token for cookies
- `Session.decode_signed_token(signed)` — verifies HMAC, returns raw token for DB lookup (no DB hit)
- `Session.from_signed_token(signed)` — verifies HMAC + DB lookup, returns instance or None

```python
# Create session
session = Session(user_id=42)
await session.save()

# Get signed token for cookie
cookie_value = session.signed_token
# → "1rKx9mP4qR7wN8bF3kL2..." (salted — different each call)

# Verify cookie (Phase 1: HMAC check, no DB)
raw_token = Session.decode_signed_token(cookie_value)
if raw_token is None:
    # Forged cookie — reject without touching DB
    raise HTTPException(401, "Invalid session")

# Verify + DB lookup (Phase 1 + Phase 2)
session = await Session.from_signed_token(cookie_value)
if session is None:
    raise HTTPException(401, "Session expired or invalid")
```

### SignedAPIKeyMixin

Model mixin for API key models with two-phase verification:

```python
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field


class APIKey(SignedAPIKeyMixin, TimestampMixin):
    class Meta:
        table = "api_keys"

    class TokenConfig:
        keys = [SigningKey(secret="key-2026-q2", version=1)]
        key_display_prefix = "sk_myapp_"  # Prepended to signed keys

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field()
    name: str = Field(default="")
```

**What the mixin provides:**

- Fields: `key_hash` (SHA-256, unique), `key_prefix` (first 16 chars), `is_active`, `expires_at`, `scopes`
- `APIKey.generate(**kwargs)` — creates key, returns `APIKeyResult(instance, raw_key)`
- `APIKey.verify(raw_key)` — HMAC check (no DB) + hash lookup + active/expiry check
- `APIKey.verify_signature_only(raw_key)` — HMAC check only (for middleware fast-reject)

```python
# Generate a new API key (returns the raw key ONCE — never stored)
result = await APIKey.generate(user_id=42, name="Production")
print(result.raw_key)  # "sk_myapp_1rKx9mP4qR7..."  (show to user once)
print(result.instance.id)  # 7 (saved to DB)

# Verify an incoming key (two phases)
api_key = await APIKey.verify(request.headers.get("x-api-key", ""))
if api_key is None:
    raise HTTPException(401, "Invalid API key")
# api_key.user_id, api_key.scopes, etc.

# Fast signature-only check (for middleware — no DB hit)
if not APIKey.verify_signature_only(raw_key):
    raise HTTPException(401, "Invalid key format")  # Forgery rejected instantly
```

**Two-phase verification flow:**

1. **Phase 1 (HMAC)**: Strip display prefix → verify HMAC signature. Rejects forged keys instantly without touching the database. ~3M rejections/sec.
2. **Phase 2 (DB)**: Hash the decoded reference → look up `key_hash` in DB → check `is_active` and `expires_at`.

### SessionAuth Integration

Add `token_engine` to `SessionAuth` for upgraded cookie signing:

```python
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.signing import TokenEngine, SigningKey

engine = TokenEngine(
    keys=[
        SigningKey(secret="session-key-2026-q2", version=1),
    ]
)

auth = SessionAuth(
    secret="your-session-secret",
    token_engine=engine,
)
app.use(auth)
```

When `token_engine` is set, all session cookies are signed via TokenEngine. Without it, plain HMAC `sign_data()` is used.

### Custom Session Models with SignedSessionMixin

For apps that need custom session fields (e.g., device info, IP tracking, tenant scoping), use `SignedSessionMixin` directly as your session model. This gives you ORM-backed sessions with signed cookie tokens:

```python
from hyperdjango.signing import SignedSessionMixin, SigningKey
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field


class AppSession(SignedSessionMixin, TimestampMixin):
    class Meta:
        table = "app_sessions"

    class TokenConfig:
        keys = [SigningKey(secret="session-key-2026-q2", version=1)]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(index=True)
    device: str = Field(default="")
    ip_address: str = Field(default="")


# Create session on login
session = AppSession(user_id=user.id, device=request.headers.get("user-agent", ""))
await session.save()

# Set signed cookie
response.set_cookie("session", session.signed_token, httponly=True, secure=True)

# Verify cookie on request
session = await AppSession.from_signed_token(request.cookies.get("session", ""))
if session is None:
    raise HTTPException(401, "Invalid session")
```

This approach is ideal when you need custom session data as queryable model fields rather than opaque JSONB blobs. Use `DatabaseSessionStore` (UNLOGGED table) for standard sessions without custom fields.

### APIKeyAuth Integration

Add `token_engine` to `APIKeyAuth` for signed key verification:

```python
from hyperdjango.auth.api_keys import APIKeyAuth
from hyperdjango.signing import TokenEngine, SigningKey

engine = TokenEngine(
    keys=[
        SigningKey(secret="apikey-key-2026-q2", version=1),
    ]
)

app.use(
    APIKeyAuth(
        valid_keys={"sk_live_abc123"},
        token_engine=engine,
        key_prefix="sk_live_",
    )
)
```

When `token_engine` is set, only signed keys are accepted (HMAC-first verification). Without it, direct hash comparison is used for static keys.

### Performance

Measured on Apple M3, Python 3.14t free-threading, Zig SIMD XOR acceleration:

| Operation                         | Throughput   |
| --------------------------------- | ------------ |
| `encode_ref`                      | 90K ops/sec  |
| `decode_ref`                      | 105K ops/sec |
| `encode_data` (small dict)        | 51K ops/sec  |
| `decode_data` (small dict)        | 78K ops/sec  |
| API key reject (HMAC only)        | 3.1M ops/sec |
| API key generate (with DB INSERT) | 3.3K ops/sec |
| API key verify (with DB lookup)   | 6.2K ops/sec |

## Overview

Cryptographic signing lets you create tokens that can be verified as authentic without a database lookup. Common use cases:

- **Password reset links** -- time-limited tokens emailed to users
- **Signed cookies** -- tamper-proof client-side state
- **Unsubscribe links** -- one-click unsubscribe without authentication
- **Email verification** -- confirm email ownership
- **API webhook signatures** -- verify webhook payloads

Signed values are **tamper-proof but NOT encrypted**. Anyone can read the value -- they just cannot modify it without knowing the secret key.

## sign_value / verify_signed

The core signing functions:

```python
import hmac
import hashlib
import time
import base64


def sign_value(value: str, secret: str, max_age: int | None = None) -> str:
    """Create a signed token from a value."""
    timestamp = str(int(time.time()))
    payload = f"{value}:{timestamp}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def verify_signed(token: str, secret: str, max_age: int | None = None) -> str | None:
    """Verify and extract value from a signed token. Returns None if invalid."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        value, timestamp, signature = decoded.rsplit(":", 2)
        expected = hmac.new(
            secret.encode(), f"{value}:{timestamp}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if max_age and (time.time() - int(timestamp)) > max_age:
            return None
        return value
    except Exception:
        return None
```

### Token Structure

A signed token has three parts, base64-encoded:

```
base64(value:timestamp:signature)
```

| Part        | Description                                            |
| ----------- | ------------------------------------------------------ |
| `value`     | The original value being signed (e.g., user ID, email) |
| `timestamp` | Unix timestamp when the token was created              |
| `signature` | HMAC-SHA256 of `value:timestamp` using the secret key  |

The entire string is base64url-encoded for safe use in URLs, cookies, and headers.

### Signing a Value

```python
SECRET_KEY = "your-secret-key-at-least-50-characters-long-for-security"

# Sign a user ID
token = sign_value("42", secret=SECRET_KEY)
# "NDI6MTcxMTIzNDU2Nzo4YTJmM2..."

# Sign with max_age hint (used on verification side)
token = sign_value("42", secret=SECRET_KEY)
```

### Verifying a Token

```python
# Verify -- returns the original value or None
value = verify_signed(token, secret=SECRET_KEY, max_age=3600)
if value is None:
    # Token is invalid (tampered, expired, or wrong secret)
    raise HTTPException(400, "Invalid token")

user_id = int(value)  # "42" -> 42
```

Verification fails (returns `None`) when:

- The signature does not match (token was tampered with)
- The token has expired (age exceeds `max_age`)
- The token is malformed (cannot be decoded)
- The secret key does not match the one used to sign

### Constant-Time Comparison

The verification uses `hmac.compare_digest()` instead of `==` for signature comparison. This prevents timing attacks where an attacker measures response times to guess the signature byte by byte.

```python
# WRONG -- vulnerable to timing attacks
if signature == expected:
    ...

# CORRECT -- constant-time comparison
if not hmac.compare_digest(signature, expected):
    return None
```

## Password Reset Tokens

HyperDjango includes a dedicated `PasswordResetTokenGenerator` for password reset flows:

```python
from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

generator = PasswordResetTokenGenerator(
    secret_key="your-secret-key",
    timeout=3600,  # Token valid for 1 hour
)
```

### Generating a Reset Token

```python
# Generate a token for a user
token = generator.make_token(user)
# "1711234567-8a2f3b4c5d6e..."
```

The token includes:

- User ID
- Current password hash (so the token is invalidated if the password changes)
- Timestamp

This means a token becomes invalid as soon as the user changes their password, even if the token has not expired.

### Verifying a Reset Token

```python
is_valid = generator.check_token(user, token)
if not is_valid:
    raise HTTPException(400, "Invalid or expired reset link")
```

### Full Password Reset Flow

```python
from hyperdjango.auth.password_reset import (
    PasswordResetTokenGenerator,
    request_password_reset,
    confirm_password_reset,
)

generator = PasswordResetTokenGenerator(secret_key=SECRET_KEY)


# Step 1: User requests reset
@app.post("/auth/reset-request")
async def request_reset(request):
    email = request.json["email"]
    user = await User.objects.filter(email=email).first()
    if user:
        token = generator.make_token(user)
        await send_mail(
            subject="Password Reset",
            message=f"Reset your password: https://myapp.com/reset/{user.id}/{token}/",
            to=[user.email],
        )
    # Always return 200 to prevent email enumeration
    return Response.json({"message": "If the email exists, a reset link was sent."})


# Step 2: User clicks link and submits new password
@app.post("/auth/reset-confirm/{user_id:int}/{token}")
async def confirm_reset(request, user_id: int, token: str):
    user = await User.objects.get(id=user_id)
    if not generator.check_token(user, token):
        raise HTTPException(400, "Invalid or expired reset link")

    new_password = request.json["password"]
    validate_password(new_password)
    user.password_hash = hash_password(new_password)
    await user.save()
    return Response.json({"message": "Password updated"})
```

### Helper Functions

For simpler usage, HyperDjango provides high-level helper functions:

```python
from hyperdjango.auth.password_reset import generate_reset_token, verify_reset_token

# Generate
token = generate_reset_token(user, secret="your-secret-key")

# Verify (returns user_id or None)
user_id = verify_reset_token(token, secret="your-secret-key", max_age=3600)
if user_id is None:
    raise HTTPException(400, "Invalid or expired token")
```

## Signed Cookies

Use signing to store tamper-proof data in cookies without a server-side session:

```python
import json


@app.post("/preferences")
async def save_preferences(request):
    data = request.json
    token = sign_value(json.dumps(data), secret=SECRET_KEY)
    response = Response.json({"saved": True})
    response.headers["Set-Cookie"] = (
        f"prefs={token}; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000"
    )
    return response


@app.get("/preferences")
async def get_preferences(request):
    token = request.cookies.get("prefs")
    if token:
        value = verify_signed(token, secret=SECRET_KEY, max_age=2592000)
        if value:
            return json.loads(value)
    return {"theme": "light", "language": "en"}  # defaults
```

### When to Use Signed Cookies vs Sessions

| Use Case         | Signed Cookies        | Sessions             |
| ---------------- | --------------------- | -------------------- |
| User preferences | Good                  | Overkill             |
| Shopping cart    | Good (small carts)    | Better (large carts) |
| Authentication   | No -- use sessions    | Yes                  |
| CSRF tokens      | Good                  | Good                 |
| Data size        | < 4 KB (cookie limit) | Unlimited            |
| Server storage   | None                  | Database row         |

## Unsubscribe Links

Generate signed one-click unsubscribe URLs:

```python
# Generate signed unsubscribe URL
token = sign_value(f"unsub:{user.id}", secret=SECRET_KEY)
url = f"https://myapp.com/unsubscribe/{token}/"

# Include in email
await send_mail(
    subject="Weekly Digest",
    message=f"...\n\nUnsubscribe: {url}",
    to=[user.email],
)


# Verify and process
@app.get("/unsubscribe/{token}")
async def unsubscribe(request, token: str):
    value = verify_signed(token, secret=SECRET_KEY)
    if not value or not value.startswith("unsub:"):
        raise HTTPException(400, "Invalid link")
    user_id = int(value.split(":")[1])
    await User.objects.filter(id=user_id).update(subscribed=False)
    return Response.html("<p>You have been unsubscribed.</p>")
```

Note: unsubscribe tokens typically do not have a `max_age` -- they should work indefinitely.

## Email Verification

```python
@app.post("/auth/register")
async def register(request):
    data = request.json
    user = await User.objects.create(
        email=data["email"],
        email_verified=False,
        password_hash=hash_password(data["password"]),
    )
    token = sign_value(f"verify:{user.id}", secret=SECRET_KEY)
    await send_mail(
        subject="Verify your email",
        message=f"Click to verify: https://myapp.com/verify/{token}/",
        to=[user.email],
    )
    return Response.json({"id": user.id}, status=201)


@app.get("/verify/{token}")
async def verify_email(request, token: str):
    value = verify_signed(token, secret=SECRET_KEY, max_age=86400)  # 24 hours
    if not value or not value.startswith("verify:"):
        raise HTTPException(400, "Invalid or expired verification link")
    user_id = int(value.split(":")[1])
    await User.objects.filter(id=user_id).update(email_verified=True)
    return Response.html("<p>Email verified! You can now log in.</p>")
```

## API Webhook Signatures

Sign outgoing webhook payloads so recipients can verify authenticity:

```python
# Sending side
@task
async def deliver_webhook(webhook_id: int, payload: str):
    webhook = await Webhook.objects.get(id=webhook_id)
    signature = hmac.new(
        webhook.secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    import httpx

    async with httpx.AsyncClient() as client:
        await client.post(
            webhook.url,
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Signature": signature,
            },
        )


# Receiving side
@app.post("/webhooks/incoming")
async def receive_webhook(request):
    payload = request.text
    signature = request.headers.get("X-Signature", "")
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(400, "Invalid signature")
    data = json.loads(payload)
    await process_webhook(data)
    return Response.json({"ok": True})
```

## Secret Key Management

### Generating a Strong Secret

```python
import secrets

# Generate a 64-character random secret
secret = secrets.token_hex(32)
# "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
```

Store the secret in an environment variable, never in source code:

```python
import os

SECRET_KEY = os.environ["SECRET_KEY"]
```

### Requirements

- At least 50 characters
- Cryptographically random (use `secrets.token_hex()` or `secrets.token_urlsafe()`)
- Never committed to version control
- Different per environment (dev, staging, production)

### Token Rotation

When you rotate the secret key:

1. All existing signed tokens become invalid
2. Password reset links stop working
3. Signed cookies are rejected

To rotate without invalidating existing tokens, you can verify against both old and new secrets during a transition period:

```python
def verify_with_rotation(
    token: str, secrets: list[str], max_age: int | None = None
) -> str | None:
    """Try verifying against multiple secrets (newest first)."""
    for secret in secrets:
        value = verify_signed(token, secret=secret, max_age=max_age)
        if value is not None:
            return value
    return None


# During rotation
CURRENT_SECRET = os.environ["SECRET_KEY"]
OLD_SECRET = os.environ.get("SECRET_KEY_OLD", "")
secrets = [CURRENT_SECRET, OLD_SECRET] if OLD_SECRET else [CURRENT_SECRET]

value = verify_with_rotation(token, secrets, max_age=3600)
```

## Security Notes

- **Constant-time comparison** -- `hmac.compare_digest()` prevents timing attacks. Never use `==` for signature comparison.
- **Always set max_age** -- tokens without expiry are dangerous. Password reset tokens should expire in 1-24 hours. Signed cookies should have a reasonable max age.
- **Strong secret key** -- at least 50 characters, cryptographically random. A weak secret can be brute-forced.
- **Rotate secrets periodically** -- especially after any suspected compromise.
- **Tamper-proof, not encrypted** -- anyone can decode the base64 and read the value. Do not sign sensitive data (passwords, credit card numbers). If you need confidentiality, encrypt instead.
- **Include context in values** -- prefix values with their purpose (`unsub:`, `verify:`, `reset:`) to prevent a token signed for one purpose from being used for another.
- **One-time use for critical tokens** -- for password resets, include the password hash in the token computation so it becomes invalid after the password changes. The `PasswordResetTokenGenerator` does this automatically.
