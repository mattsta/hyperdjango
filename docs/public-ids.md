# Public IDs

Opaque, non-sequential identifiers that hide your integer primary keys in URLs and responses.

> **Security scope — read this first.** Public IDs are **defense-in-depth**, not access control. They stop trivial `/api/users/1,2,3` enumeration and avoid leaking record counts/ordering — but they do **not** prevent IDOR/BOLA on their own. IDOR is an _authorization_ bug: you must still verify the caller is allowed to touch the object (ownership / tenancy / object permissions — see [guard](guard.md), `OwnershipMixin`, [tenancy](tenancy.md)). `encode(pk)` is a reversible base‑N transform (obfuscation, like Hashids/Sqids — not encryption), and even an unforgeable random ID can be reused once leaked. Treat a public ID as a nicer-looking key, never as a permission.

## Why?

Sequential integer PKs (`/api/users/1`, `/api/users/2`, ...) let anyone enumerate every object and read off your table sizes/growth. Public IDs replace them with opaque strings that reveal nothing about ordering, count, or internal structure — closing the _enumeration_ problem. They do not replace the authorization check that closes IDOR.

HyperDjango keeps integer PKs internally (fast joins, compact indexes) and exposes opaque public IDs externally.

## Quick Start

### 1. Generate an Alphabet

Each model needs its own alphabet — a random permutation of a base character set. Generate it once, copy it into your code:

```python
from hyperdjango.public_id import generate_alphabet

# 32-char: no vowels, no confusable chars (0/O, 1/l/I)
print(generate_alphabet("olc32"))
# "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"

# 62-char: full alphanumeric (shorter IDs, more entropy per char)
print(generate_alphabet("base62"))
# "tR4kL9xZGsp5x8u3JiS1..."
```

### 2. Add PublicIDMixin to Your Model

```python
from hyperdjango import Model, Field
from hyperdjango.public_id import PublicIDMixin


class Article(PublicIDMixin, Model):
    class Meta:
        table = "articles"

    class PublicIDConfig:
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"  # YOUR generated alphabet
        strategy = IDStrategy.RANDOM
        entropy_bytes = 8  # 40 bits of entropy

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
```

### 3. Use It

```python
# Create — public_id auto-generated on save
article = Article(title="Hello")
await article.save()
article.id  # 1 (internal, never expose)
article.public_id  # "Xf7RgW3pMc" (expose this)

# Lookup by public_id
article = await Article.get_by_public_id("Xf7RgW3pMc")

# Bulk lookup
articles = await Article.filter_by_public_ids(["Xf7RgW3pMc", "R4kL9xZG"])
```

### 4. Serialize Without Leaking PKs

```python
from hyperdjango.serializers import PublicIDSerializer, SerializerField


class ArticleSerializer(PublicIDSerializer):
    title: str = SerializerField()
    content: str = SerializerField()


serializer = ArticleSerializer(obj=article)
data = serializer.data
# {"id": "Xf7RgW3pMc", "title": "Hello", "content": "..."}
# Integer PK is never in the output
```

## Strategies

| Strategy           | When Generated | Reversible?         | Use Case                      |
| ------------------ | -------------- | ------------------- | ----------------------------- |
| `random` (default) | Before INSERT  | No                  | Most apps — maximum security  |
| `uuid7`            | Before INSERT  | No                  | Standard UUID format, interop |
| `encoded_pk`       | After INSERT   | Yes (with alphabet) | Zero storage overhead, weaker |

### random (recommended)

Generates cryptographically random bytes, encodes with your alphabet. No relation to the integer PK.

```python
class PublicIDConfig:
    alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
    strategy = IDStrategy.RANDOM
    entropy_bytes = 8  # → ~13 char IDs (base-32)
```

### uuid7

Uses Python's `uuid.uuid4()` (UUIDv7 when available). No alphabet needed. Produces 36-char UUIDs.

```python
class PublicIDConfig:
    strategy = IDStrategy.UUID7
    # alphabet not required
```

### encoded_pk

Encodes the integer PK with your alphabet. Deterministic and reversible — if the alphabet leaks, attackers can decode. Zero extra storage (computed, not stored).

```python
class PublicIDConfig:
    alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
    strategy = IDStrategy.ENCODED_PK
    width = 8  # pad to 8 chars
```

## Alphabet Design

### Base Character Sets

**OLC-32** (recommended for user-facing IDs):

- 32 characters: `23456789cfghjmpqrvwxCFGHJMPQRVWX`
- No vowels — can't accidentally spell words
- No confusable chars — no `0/O`, `1/l/I`
- 5 bits per character

**Base-62** (for API/internal IDs):

- 62 characters: `0-9a-zA-Z`
- Maximum density — shorter IDs for same entropy
- 5.95 bits per character

### Width Reference

Characters needed for N bits of entropy:

| Entropy  | Base-32  | Base-62  | Practical Meaning |
| -------- | -------- | -------- | ----------------- |
| 30 bits  | 6 chars  | 6 chars  | 1 billion values  |
| 40 bits  | 8 chars  | 7 chars  | 1 trillion values |
| 50 bits  | 10 chars | 9 chars  | 1 quadrillion     |
| 64 bits  | 13 chars | 11 chars | BIGSERIAL max     |
| 80 bits  | 16 chars | 14 chars | Very high entropy |
| 128 bits | 26 chars | 22 chars | UUID-equivalent   |

### Generating Alphabets

```python
from hyperdjango.public_id import generate_alphabet

# Each model gets its own permutation
article_alpha = generate_alphabet("olc32")  # random 32-char permutation
user_alpha = generate_alphabet("olc32")  # different permutation
event_alpha = generate_alphabet("base62")  # 62-char for shorter IDs

# Seeded for reproducible testing
test_alpha = generate_alphabet("olc32", seed=42)
```

!!! warning "Never Change an Alphabet"
Once deployed, an alphabet is permanent. Changing it would make all existing public IDs undecodable. Treat it like a database schema migration.

## BaseEncoder API

For direct encoding without models:

```python
from hyperdjango.public_id import BaseEncoder

encoder = BaseEncoder("W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4")

# Encode/decode integers
encoded = encoder.encode(12345)  # "cX9"
decoded = encoder.decode("cX9")  # 12345

# Fixed-width output
padded = encoder.encode_padded(42, 8)  # "44444443J"

# Random IDs
random_id = encoder.encode_random(8)  # 8 bytes entropy → ~13 chars

# Encode bytes (e.g., UUIDs, hashes)
encoded = encoder.encode_bytes(uuid_bytes)
decoded = encoder.decode_to_bytes(encoded, 16)

# Pack multiple integers into one string
packed = encoder.encode_packed([val1, val2], bits_per_value=128)
unpacked = encoder.decode_packed(packed, bits_per_value=128, count=2)

# Capacity planning
encoder.max_value_for_width(8)  # 4294967295 (base-32, 8 chars)
encoder.width_for_bits(64)  # 13 chars needed for 64-bit values
```

## Performance

Encoding uses native Zig with tiered fast paths:

| Value Range        | Ops/sec  | Method                          |
| ------------------ | -------- | ------------------------------- |
| ≤ 64-bit (PKs)     | **4.8M** | Pure native integer math        |
| ≤ 128-bit (tokens) | 825K     | Native u128 via byte extraction |
| > 128-bit          | 229K     | Python PyLong fallback          |

Typical database PKs hit the fastest u64 path with zero Python object creation in the encode loop.
