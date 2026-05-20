# Unified ID System

External-facing identifiers for API-safe object references. They hide sequential integer primary keys from URLs, responses, and logs — stopping trivial enumeration and count leakage.

> **Not access control.** Opaque/external IDs are defense-in-depth, not a substitute for authorization: they do not by themselves prevent IDOR/BOLA. Always gate access on ownership / tenancy / object permissions (see [guard](guard.md), [tenancy](tenancy.md)) — an attacker who obtains one valid ID can still use it.

## Why External IDs Matter

Sequential integer PKs (`/api/posts/1`, `/api/posts/2`) are a security liability:

- **Enumeration** -- Attackers iterate PKs to scrape every record in the table.
- **Bot amplification** -- Bots can guess valid IDs without authentication probes.
- **Information leakage** -- PK values reveal creation order, total record count, and growth rate.
- **IDOR/BOLA** -- Insecure Direct Object Reference attacks exploit predictable IDs to access other users' data.

HyperDjango's ID system keeps integer PKs internal (fast joins, compact indexes) and presents opaque, unforgeable strings at the API boundary.

---

## Two Systems

HyperDjango provides two ID systems. `IDMixin` is the recommended approach for new code. `PublicIDMixin` stores a generated `public_id` column and suits schemas that already carry one.

| System                     | Class                                | Approach                                                                          |
| -------------------------- | ------------------------------------ | --------------------------------------------------------------------------------- |
| **IDMixin** (new)          | `IDMixin` + `IDConfig` + `IDManager` | Derives external IDs from PK at runtime. No extra DB column (except random mode). |
| **PublicIDMixin** (legacy) | `PublicIDMixin` + `PublicIDConfig`   | Stores a `public_id` column in the database. Generated on first save.             |

---

## IDMode and IDStrategy Enums

```python
from hyperdjango.public_id import IDMode, IDStrategy

# IDMode -- controls external ID representation
IDMode.RAW  # "raw"
IDMode.ENCODED  # "encoded"
IDMode.SIGNED  # "signed"
IDMode.RANDOM  # "random"

# IDStrategy -- controls PublicIDMixin generation
IDStrategy.RANDOM  # "random"
IDStrategy.UUID7  # "uuid7"
IDStrategy.ENCODED_PK  # "encoded_pk"
```

```python
class Post(IDMixin, Model):
    class IDConfig:
        mode = IDMode.SIGNED  # not "signed"
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = ["key-2025-q1"]
```

---

## 4 ID Modes (IDMixin)

| Mode      | Format                 | DB Column   | Enumerable      | Use Case                                    |
| --------- | ---------------------- | ----------- | --------------- | ------------------------------------------- |
| `signed`  | `{encoded}.{hmac_hex}` | None        | No              | Default. Public APIs, user-facing URLs.     |
| `encoded` | Base-N encoded PK      | None        | Yes (bijection) | Internal services with trusted callers.     |
| `raw`     | Integer string         | None        | Yes             | Internal/admin APIs only.                   |
| `random`  | Random opaque string   | `public_id` | No              | Legacy compatibility, pre-existing schemas. |

---

## Quick Start

Signed mode is the recommended default. It derives an unforgeable external ID from the integer PK without storing anything extra in the database.

```python
from hyperdjango.public_id import IDMixin, generate_alphabet
from hyperdjango.models import Model, Field

# Step 1: Generate an alphabet (one time, copy the output into code)
# >>> from hyperdjango.public_id import generate_alphabet
# >>> print(generate_alphabet("olc32"))
# "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"


class Post(IDMixin, Model):
    class Meta:
        table = "posts"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = ["key-2024-q1"]

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()


# Encode PK to external ID
post = await Post.objects.get(id=42)
external_id = post.get_external_id()
# "cX9.a1b2c3d4e5f6a1b2"

# Decode external ID back to PK
pk = Post.decode_external_id(external_id)
# 42

# Verify without decoding
Post.verify_external_id(external_id)
# True

# Invalid/forged IDs raise ValueError
Post.decode_external_id("cX9.0000000000000000")
# ValueError: Invalid signed ID: signature verification failed
```

---

## IDConfig Reference

All options with their defaults:

```python
class IDConfig:
    mode: IDMode = IDMode.SIGNED
    # IDMode.RAW, IDMode.ENCODED, IDMode.SIGNED, or IDMode.RANDOM

    alphabet: str = ""
    # Required for encoded/signed modes.
    # A random permutation of a base character set.
    # Generate with: generate_alphabet("olc32") or generate_alphabet("base62")

    hmac_keys: list[str] = []
    # Required for signed mode. Ordered newest-first.
    # The first key is used for signing; all keys are tried for verification.

    signature_bytes: int = 8
    # HMAC truncation length. 8 bytes = 16 hex characters.
    # Minimum recommended: 8 (64 bits). Increase for higher-security contexts.

    include_user: bool = False
    # Per-user signing. When True, user_id is included in the HMAC input,
    # producing different external IDs for the same object per user.

    separator: str = "."
    # Character between the encoded value and the HMAC signature.

    table_name: str = ""
    # Included in HMAC input for table isolation.
    # Auto-detected from Meta.table if empty, falls back to lowercase class name.

    # Random mode only:
    entropy_bytes: int = 10
    # Bytes of randomness for random ID generation.

    strategy: IDStrategy = IDStrategy.RANDOM
    # "random" or "uuid7" (for random mode only).
```

### Alphabet Generation

Generate a unique alphabet for each model. Never reuse alphabets across models.

```python
from hyperdjango.public_id import generate_alphabet

# 32-char alphabet: no vowels (can't spell words), no confusables (0/O, 1/l/I)
generate_alphabet("olc32")
# "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"

# 62-char alphabet: full alphanumeric, higher density (5.95 bits/char vs 5.0)
generate_alphabet("base62")
# "tR4kL9xZ..."
```

Width reference for planning ID length:

| Base | Chars | Bits | Covers              |
| ---- | ----- | ---- | ------------------- |
| 32   | 6     | 30   | 1 billion values    |
| 32   | 8     | 40   | 1 trillion values   |
| 32   | 13    | 65   | BIGSERIAL max       |
| 62   | 6     | 35   | 34 billion values   |
| 62   | 8     | 47   | 140 trillion values |
| 62   | 11    | 65   | BIGSERIAL max       |

---

## Signed Mode Deep Dive

Signed mode is the recommended default. It provides anti-enumeration, anti-forgery, and table isolation without any extra database storage.

### How It Works

```
Internal PK (integer)
       |
       v
  BaseEncoder.encode(pk)        # Bijective base-N encoding
       |                         # e.g., 42 -> "cX9"
       v
  HMAC-SHA256(key, "posts:cX9") # Sign with table name + encoded value
       |
       v
  Truncate to signature_bytes    # 8 bytes = 16 hex chars
       |
       v
  "{encoded}.{signature}"        # "cX9.a1b2c3d4e5f6a1b2"
```

### Why It Prevents Enumeration

An attacker who knows `cX9.a1b2c3d4e5f6a1b2` is PK 42 cannot compute the external ID for PK 43 without the HMAC key. The encoding step is a bijection (reversible), but the signature step requires the secret key.

### Signature Format

```
cX9.a1b2c3d4e5f6a1b2
 ^  ^
 |  +-- HMAC-SHA256 hex digest, truncated to signature_bytes * 2 chars
 +-- Base-N encoded PK
```

The separator (`.` by default) divides the encoded value from the signature. Decoding splits on the last separator occurrence, so encoded values containing the separator character are handled correctly.

### Table Isolation

The HMAC input includes the table name: `"posts:cX9"`. This means the same PK in different tables produces different external IDs:

```python
# PK 42 in "posts" table
post.get_external_id()  # "cX9.a1b2c3d4e5f6a1b2"

# PK 42 in "comments" table (different alphabet, different table name)
comment.get_external_id()  # "Rm7.f8e7d6c5b4a3f8e7"
```

An external ID valid for the `posts` table will fail signature verification against the `comments` table, even if both use the same HMAC key.

### Constant-Time Comparison

Signature verification uses `hmac.compare_digest()`, which runs in constant time regardless of where the first mismatch occurs. This prevents timing attacks that could incrementally guess the correct signature.

---

## Key Rotation

HMAC keys are stored as a list, ordered newest-first. The first key signs new IDs; all keys are tried during verification.

### Adding a New Key

Add the new key at position 0. Existing IDs signed with the old key continue to verify.

```python
class Post(IDMixin, Model):
    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = [
            "key-2025-q1",  # NEW — used for signing
            "key-2024-q1",  # OLD — still accepted for verification
        ]
```

**What happens:**

- New external IDs are signed with `key-2025-q1`.
- Incoming IDs signed with `key-2024-q1` still verify (tried second).
- No IDs are invalidated. No database migration needed.

### Removing an Old Key

After sufficient time (all cached/bookmarked/stored external IDs have been refreshed), remove the old key:

```python
class Post(IDMixin, Model):
    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = [
            "key-2025-q1",  # Only key — old IDs no longer verify
        ]
```

**Warning:** Removing a key invalidates all external IDs signed with it. Plan a deprecation window.

### Example Rotation Schedule

| Quarter | hmac_keys                        | Effect                               |
| ------- | -------------------------------- | ------------------------------------ |
| 2024-Q1 | `["key-2024-q1"]`                | Initial deployment                   |
| 2025-Q1 | `["key-2025-q1", "key-2024-q1"]` | New key added, old still valid       |
| 2025-Q3 | `["key-2025-q1"]`                | Old key removed after 6-month window |
| 2026-Q1 | `["key-2026-q1", "key-2025-q1"]` | Next rotation                        |

### Key Entropy Requirements

HMAC keys should have at least 256 bits of entropy. Generate with:

```python
import secrets

key = f"key-2025-q1-{secrets.token_hex(32)}"
# "key-2025-q1-a1b2c3d4...64 hex chars..."
```

---

## Per-User Signing

When `include_user=True`, the user's ID is included in the HMAC input. The same object produces different external IDs for different users.

### When to Use

- Sensitive APIs where users should not share or compare IDs.
- Per-user data isolation (e.g., medical records, financial data).
- Preventing ID-sharing attacks where a valid ID from one user's session is replayed in another's.

### How It Works

```
HMAC input (standard):   "posts:cX9"
HMAC input (per-user):   "posts:cX9:17"    # user_id=17
```

```python
class MedicalRecord(IDMixin, Model):
    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = ["key-2025-q1"]
        include_user = True


# Same record, different external IDs per user
record.get_external_id(user_id=17)  # "cX9.a1b2c3d4e5f6a1b2"
record.get_external_id(user_id=42)  # "cX9.7f8e9d0c1b2a3f4e"

# Decoding requires the correct user_id
MedicalRecord.decode_external_id("cX9.a1b2c3d4e5f6a1b2", user_id=17)  # 42
MedicalRecord.decode_external_id("cX9.a1b2c3d4e5f6a1b2", user_id=42)
# ValueError: signature verification failed
```

### Preventing ID Sharing

If user A copies an external ID from their browser and sends it to user B, user B cannot use it -- the signature is bound to user A's identity. The server returns 404 (not 400 or 403), preventing information leakage about whether the object exists.

---

## Time-Windowed IDs

Create IDs that are only valid within a specific time window — fully stateless,
no database lookup needed for time validation.

### How It Works

Time constraints are embedded in the external ID itself, covered by the HMAC:

```
Standard:      {encoded_pk}.{hmac}
Time-windowed: {encoded_pk}.{start}-{end}.{hmac}
```

- Timestamps encoded compactly using the same alphabet
- `"0"` means no limit on that side
- HMAC covers the time window — can't tamper without breaking signature
- On decode: verify HMAC, check `start <= now <= end`, decode PK
- Expired/not-yet-valid → same error as invalid signature (no info leakage)

### Usage

```python
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone.utc)

# Valid for the next 24 hours
manager.encode(pk=42, valid_until=now + timedelta(hours=24))

# Valid starting Feb 1 through Feb 10
manager.encode(
    pk=42,
    valid_after=datetime(2025, 2, 1, tzinfo=timezone.utc),
    valid_until=datetime(2025, 2, 10, tzinfo=timezone.utc),
)

# Valid only after a future date (embargo)
manager.encode(pk=42, valid_after=datetime(2025, 3, 1, tzinfo=timezone.utc))
```

### Use Cases

- **Expiring share links**: "This link expires in 48 hours"
- **Embargo content**: "Available after March 1"
- **Time-limited API tokens**: Access windows for temporary integrations
- **Promotional URLs**: "Valid Feb 1-14 only"

---

## KeySlot Dataclass

The `KeySlot` dataclass represents a single HMAC key with optional PK offset and
custom epoch. It is the building block for `hmac_keys` lists in signed mode.

```python
from hyperdjango.public_id import KeySlot

slot = KeySlot(
    key="secret-key-2025-q1",  # HMAC signing key (required)
    offset=50_000,  # Added to PK before encoding, subtracted after decoding
    epoch=1704240000,  # Custom Unix timestamp for time-window calculations
)
```

| Field    | Type  | Default | Description                                                                                       |
| -------- | ----- | ------- | ------------------------------------------------------------------------------------------------- |
| `key`    | `str` | --      | HMAC-SHA256 signing key. Must have at least 256 bits of entropy.                                  |
| `offset` | `int` | `0`     | Added to PK before encoding. Prevents low PKs from producing short, predictable external IDs.     |
| `epoch`  | `int` | `0`     | Custom Unix timestamp used as the base for time-window calculations. 0 means standard Unix epoch. |

### Auto-Normalization of String Keys

When `hmac_keys` contains plain strings, they are automatically normalized to
`KeySlot` instances with `offset=0` and `epoch=0`. You can mix strings and
`KeySlot` objects freely:

```python
class Article(IDMixin, Model):
    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = [
            KeySlot("key-2025-q2", offset=100_000, epoch=1735689600),
            "key-2025-q1",  # auto-normalized to KeySlot("key-2025-q1", offset=0, epoch=0)
        ]
```

This lets you add offsets and epochs incrementally without rewriting existing key entries.

---

## Custom Epoch

Each KeySlot can define a custom epoch — a base timestamp for time calculations.
Instead of encoding full Unix timestamps (large numbers → long strings), times
are stored relative to the epoch (small numbers → short strings).

### Why Use It

| Epoch       | Feb 1, 2025 encoded as | String length |
| ----------- | ---------------------- | ------------- |
| Unix (1970) | 1738368000             | ~7 chars      |
| Jan 3, 2024 | 34128000               | ~5 chars      |

Shorter encodings → shorter URLs. Also makes time values harder to reverse-engineer
since attackers don't know your epoch.

### Configuration

```python
from hyperdjango.public_id import KeySlot

# Epoch = Jan 3, 2024 00:00 UTC (Unix timestamp 1704240000)
KeySlot(
    key="secret-key-2025",
    offset=50_000,
    epoch=1704240000,  # Custom epoch
)
```

### Rotation with Epochs

Each KeySlot has its own epoch. When rotating keys, you can also change the epoch:

```python
hmac_keys = [
    KeySlot("key-2025-q2", offset=100_000, epoch=1735689600),  # Jan 1, 2025
    KeySlot("key-2025-q1", offset=50_000, epoch=1704240000),  # Jan 3, 2024
]
```

Old IDs decode correctly using their original key's epoch.
New IDs use the newest key's epoch for even shorter encodings.

---

## Encoded Mode

Simple bijective encoding without HMAC signing. The external ID is a reversible transformation of the PK.

```python
class InternalWidget(IDMixin, Model):
    class IDConfig:
        mode = IDMode.ENCODED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"


widget.get_external_id()  # "cX9"
InternalWidget.decode_external_id("cX9")  # 42
```

**Properties:**

- No HMAC key needed.
- Deterministic: same PK always produces the same external ID.
- Enumerable: an attacker who knows the alphabet can decode any ID back to a PK and encode arbitrary PKs.
- Use only for internal service-to-service APIs where callers are trusted.

---

## Raw Mode

Exposes the integer PK directly as a string. No encoding, no signing.

```python
class AdminResource(IDMixin, Model):
    class IDConfig:
        mode = IDMode.RAW


resource.get_external_id()  # "42"
AdminResource.decode_external_id("42")  # 42
```

**Properties:**

- No alphabet or HMAC keys needed.
- Fully enumerable.
- Use only for internal admin APIs behind authentication and authorization.

---

## Random Mode

Generates a random opaque string and stores it in a `public_id` database column. This is the approach used by the legacy `PublicIDMixin` system.

```python
class LegacyItem(IDMixin, Model):
    class IDConfig:
        mode = IDMode.RANDOM
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        entropy_bytes = 10

    public_id: str | None = Field(default=None, unique=True, index=True)
```

**Properties:**

- Requires an extra database column (`public_id`).
- The external ID is generated once and stored; it never changes.
- `IDManager.encode()` and `IDManager.decode()` are not applicable -- use the stored `public_id` directly.
- Use for schemas that already have a `public_id` column, or when external IDs must survive PK changes (e.g., table migrations that reassign PKs).

---

## REST Integration

ViewSet integration auto-encodes and auto-decodes external IDs at the API boundary.

### Automatic Encoding in Responses

List and detail responses replace integer PKs with external IDs:

```python
# GET /api/posts/
# Response:
[
    {"id": "cX9.a1b2c3d4e5f6a1b2", "title": "Hello"},
    {"id": "Rm7.f8e7d6c5b4a3f8e7", "title": "World"},
]
```

### Automatic Decoding in Lookups

The ViewSet decodes the external ID from the URL back to a PK for database lookup:

```python
# GET /api/posts/cX9.a1b2c3d4e5f6a1b2
# Internally resolves to: Post.objects.get(pk=42)
```

### Invalid IDs Return 404

Invalid, forged, or malformed external IDs return HTTP 404 -- never 400 or any other status that distinguishes "malformed ID" from "object not found." This prevents information leakage:

```
GET /api/posts/INVALID         -> 404 Not Found
GET /api/posts/cX9.forged_sig  -> 404 Not Found
GET /api/posts/cX9.valid_sig   -> 200 OK (if exists) or 404 Not Found
```

An attacker cannot distinguish between a forged ID and a genuinely missing object.

---

## Security Analysis

### Anti-Enumeration

| Mode      | Enumerable | Why                                                       |
| --------- | ---------- | --------------------------------------------------------- |
| `signed`  | No         | HMAC signature requires secret key to forge.              |
| `encoded` | Yes        | Bijection is reversible by anyone who knows the alphabet. |
| `raw`     | Yes        | Integer PK is directly exposed.                           |
| `random`  | No         | Random string with no relationship to PK.                 |

### Timing Attack Prevention

Signed mode uses `hmac.compare_digest()` for signature comparison. This function compares in constant time, preventing timing side-channels that could leak signature bytes incrementally.

### Information Leakage Prevention

- Invalid external IDs return 404, not 400 or 403.
- No error messages distinguish between "bad format" and "not found."
- Per-user signing prevents cross-user ID correlation.

### Key Entropy

- HMAC keys must have at least 256 bits of entropy (32 bytes / 64 hex characters).
- The `signature_bytes` setting controls truncation. At 8 bytes (default), there are 2^64 possible signatures per encoded value. Brute-forcing requires ~2^63 attempts on average.
- Alphabets should be unique per model. Reusing an alphabet across models does not break security (table isolation handles it) but is poor practice.

### Table Isolation

The table name is included in the HMAC input. Even if two models share the same HMAC key and alphabet, an external ID from one table will not verify against the other.

---

## Migration from PublicIDMixin to IDMixin

### Before (Legacy)

```python
class Article(PublicIDMixin, Model):
    class PublicIDConfig:
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 8

    id: int = Field(primary_key=True, auto=True)
    # public_id column is auto-declared by PublicIDMixin
```

### After (IDMixin -- Signed Mode)

```python
class Article(IDMixin, Model):
    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = ["key-2025-q1"]

    id: int = Field(primary_key=True, auto=True)
    # No public_id column needed
```

### Migration Steps

1. **Add IDMixin alongside PublicIDMixin** during the transition. Both can coexist on the same model.

2. **Update API endpoints** to accept both the old `public_id` and the new signed external ID. Decode with `IDMixin.decode_external_id()` first; fall back to a `public_id` column lookup.

3. **Switch responses** to emit signed external IDs instead of stored `public_id` values.

4. **Remove PublicIDMixin** once all clients have migrated.

5. **Drop the `public_id` column** after confirming no code references it.

### Key Differences

| Aspect       | PublicIDMixin                 | IDMixin (signed)                                   |
| ------------ | ----------------------------- | -------------------------------------------------- |
| Storage      | Extra `public_id` column      | No extra column                                    |
| Generation   | On first `save()`             | On demand (runtime computation)                    |
| Determinism  | Random -- different each time | Deterministic -- same PK always produces same ID   |
| Key rotation | N/A                           | Built-in via `hmac_keys` list                      |
| Per-user IDs | Not supported                 | `include_user=True`                                |
| DB lookup    | `WHERE public_id = $1`        | Decode to PK, then `WHERE id = $1` (uses PK index) |

---

## BaseEncoder

The low-level encoding engine used by all modes except `raw`. Performs arbitrary-base encoding with Zig-accelerated native conversion.

```python
from hyperdjango.public_id import BaseEncoder

encoder = BaseEncoder("W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4")

encoder.encode(12345)  # "cX9"
encoder.decode("cX9")  # 12345
encoder.encode_padded(42, 8)  # "44444443J" (fixed 8 chars)
encoder.encode_random(8)  # random string with 8 bytes of entropy
encoder.encode_bytes(b"\x01\x02")  # encode raw bytes
encoder.max_value_for_width(8)  # maximum encodable value in 8 chars
encoder.width_for_bits(64)  # minimum chars for 64 bits of entropy
```
