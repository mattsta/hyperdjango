#!/usr/bin/env python3
"""
Tests for signing model mixins — SignedSessionMixin, SignedAPIKeyMixin.

Tests hyperdjango/signing.py mixin integration with Model and Database:
- SignedSessionMixin: token auto-generation, signed_token, decode, DB lookup
- SignedAPIKeyMixin: generate(), verify(), verify_signature_only()
- Key rotation with mixins
- Adversarial inputs on mixins
- Padding + salt config on mixins
- Concurrent mixin operations

Usage:
    uv run hyper-test signing_mixins
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import time

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field
from hyperdjango.signing import (
    APIKeyResult,
    SignedAPIKeyMixin,
    SignedSessionMixin,
    SigningKey,
    TokenEngine,
)
from hyperdjango.testkit import tamper

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS  {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL  {name}" + (f" — {details}" if details else ""))


# ── Test Models ───────────────────────────────────────────────────────────


class TestSession(SignedSessionMixin, TimestampMixin):
    class Meta:
        table = "test_signing_sessions"

    class TokenConfig:
        keys = [SigningKey(secret="test-session-key-2026-q2", version=1)]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(default=0)
    data: str = Field(default="{}")


class TestAPIKey(SignedAPIKeyMixin, TimestampMixin):
    class Meta:
        table = "test_signing_apikeys"

    class TokenConfig:
        keys = [SigningKey(secret="test-apikey-key-2026-q2", version=1)]
        key_display_prefix = "sk_test_"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(default=0)
    name: str = Field(default="")


class PaddedSession(SignedSessionMixin, TimestampMixin):
    class Meta:
        table = "test_signing_padded_sessions"

    class TokenConfig:
        keys = [SigningKey(secret="padded-session-key-2026", version=2)]
        salt_bytes = 16
        pad_to_bucket = True

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(default=0)


class PaddedAPIKey(SignedAPIKeyMixin, TimestampMixin):
    class Meta:
        table = "test_signing_padded_apikeys"

    class TokenConfig:
        keys = [SigningKey(secret="padded-apikey-key-2026", version=2)]
        key_display_prefix = "sk_pad_"
        salt_bytes = 16
        pad_to_bucket = True

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(default=0)
    name: str = Field(default="")


# ── DB Setup ──────────────────────────────────────────────────────────────


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create tables
    for table, columns in [
        (
            "test_signing_sessions",
            """
            id SERIAL PRIMARY KEY,
            user_id INTEGER DEFAULT 0,
            data TEXT DEFAULT '{}',
            token TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        """,
        ),
        (
            "test_signing_apikeys",
            """
            id SERIAL PRIMARY KEY,
            user_id INTEGER DEFAULT 0,
            name TEXT DEFAULT '',
            key_hash TEXT UNIQUE,
            key_prefix TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT true,
            expires_at TEXT,
            scopes TEXT DEFAULT '*',
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        """,
        ),
        (
            "test_signing_padded_sessions",
            """
            id SERIAL PRIMARY KEY,
            user_id INTEGER DEFAULT 0,
            token TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        """,
        ),
        (
            "test_signing_padded_apikeys",
            """
            id SERIAL PRIMARY KEY,
            user_id INTEGER DEFAULT 0,
            name TEXT DEFAULT '',
            key_hash TEXT UNIQUE,
            key_prefix TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT true,
            expires_at TEXT,
            scopes TEXT DEFAULT '*',
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        """,
        ),
    ]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await db.execute(f"CREATE TABLE {table} ({columns})")

    return db


async def teardown_db(db):
    for table in [
        "test_signing_sessions",
        "test_signing_apikeys",
        "test_signing_padded_sessions",
        "test_signing_padded_apikeys",
    ]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await db.disconnect()


# ── SignedSessionMixin Tests ──────────────────────────────────────────────


async def test_session_mixin_basics():
    print("\n=== SignedSessionMixin Basics ===")

    # Auto-generates token on save
    session = TestSession(user_id=1, data='{"role":"admin"}')
    check("Token is None before save", session.token is None or session.token == "")
    await session.save()
    check(
        "Token generated after save",
        session.token is not None and len(session.token) > 20,
    )

    # signed_token returns a signed version
    signed = session.signed_token
    check("signed_token is a string", isinstance(signed, str) and len(signed) > 10)
    check("signed_token != raw token", signed != session.token)

    # signed_token contains version and type chars
    check("signed_token has type 'r'", signed[1] == "r")

    # decode_signed_token recovers the raw token
    raw = TestSession.decode_signed_token(signed)
    check("decode_signed_token recovers raw token", raw == session.token)

    # from_signed_token does DB lookup
    found = await TestSession.from_signed_token(signed)
    check("from_signed_token finds session", found is not None)
    check("from_signed_token correct user_id", found.user_id == 1)
    check("from_signed_token correct data", found.data == '{"role":"admin"}')

    # Non-deterministic: two signed_token calls produce different results (salted)
    s1 = session.signed_token
    s2 = session.signed_token
    check("signed_token non-deterministic (salted)", s1 != s2)

    # Both decode to same raw token
    check(
        "Both signed tokens decode same",
        TestSession.decode_signed_token(s1)
        == TestSession.decode_signed_token(s2)
        == session.token,
    )


async def test_session_mixin_invalid_tokens():
    print("\n=== SignedSessionMixin Invalid Tokens ===")

    # Garbage token
    check("Garbage rejected", TestSession.decode_signed_token("garbage") is None)
    found = await TestSession.from_signed_token("garbage")
    check("Garbage DB lookup returns None", found is None)

    # Empty token
    check("Empty rejected", TestSession.decode_signed_token("") is None)

    # Token from different key
    other_engine = TokenEngine(keys=[SigningKey(secret="wrong-key", version=0)])
    fake_signed = other_engine.encode_ref("fake_token")
    check("Wrong key rejected", TestSession.decode_signed_token(fake_signed) is None)

    # Valid signature but token doesn't exist in DB
    session = TestSession(user_id=99)
    await session.save()
    signed = session.signed_token

    # Delete from DB, then try lookup
    await get_db().execute(
        "DELETE FROM test_signing_sessions WHERE id = $1", session.id
    )
    found = await TestSession.from_signed_token(signed)
    check("Deleted session returns None", found is None)


async def test_session_multiple_sessions():
    print("\n=== Multiple Sessions ===")

    # Create 10 sessions, each gets a unique token
    sessions = []
    for i in range(10):
        s = TestSession(user_id=i)
        await s.save()
        sessions.append(s)

    tokens = [s.token for s in sessions]
    check("All tokens unique", len(set(tokens)) == 10)

    # Each signed token resolves to correct session
    for i, s in enumerate(sessions):
        signed = s.signed_token
        found = await TestSession.from_signed_token(signed)
        check(f"Session {i} lookup correct", found is not None and found.user_id == i)


# ── SignedAPIKeyMixin Tests ───────────────────────────────────────────────


async def test_apikey_mixin_generate():
    print("\n=== SignedAPIKeyMixin Generate ===")

    result = await TestAPIKey.generate(user_id=1, name="test-key")
    check("generate returns APIKeyResult", isinstance(result, APIKeyResult))
    check("raw_key starts with prefix", result.raw_key.startswith("sk_test_"))
    check("raw_key is substantial length", len(result.raw_key) > 30)

    instance = result.instance
    check("Instance has id", instance.id > 0)
    check("Instance has key_hash", len(instance.key_hash) == 64)  # SHA-256 hex
    check("Instance has key_prefix", len(instance.key_prefix) == 16)
    check("Instance is_active", instance.is_active is True)
    check("Instance scopes default", instance.scopes == "*")
    check("Instance user_id", instance.user_id == 1)
    check("Instance name", instance.name == "test-key")

    # raw_key is NOT the key_hash
    check("raw_key != key_hash", result.raw_key != instance.key_hash)

    # raw_key not stored anywhere in DB
    rows = await get_db().query(
        "SELECT * FROM test_signing_apikeys WHERE id = $1", instance.id
    )
    check("raw_key not in DB row", result.raw_key not in str(rows[0]))


async def test_apikey_mixin_verify():
    print("\n=== SignedAPIKeyMixin Verify ===")

    result = await TestAPIKey.generate(user_id=2, name="verify-key")
    raw_key = result.raw_key

    # Verify succeeds
    found = await TestAPIKey.verify(raw_key)
    check("verify finds key", found is not None)
    check("verify correct user_id", found.user_id == 2)
    check("verify correct name", found.name == "verify-key")

    # verify_signature_only (no DB hit)
    check("verify_signature_only valid", TestAPIKey.verify_signature_only(raw_key))
    check(
        "verify_signature_only garbage", not TestAPIKey.verify_signature_only("garbage")
    )
    check(
        "verify_signature_only wrong prefix",
        not TestAPIKey.verify_signature_only("sk_wrong_" + raw_key[8:]),
    )


async def test_apikey_mixin_revoke():
    print("\n=== SignedAPIKeyMixin Revoke ===")

    result = await TestAPIKey.generate(user_id=3, name="revoke-key")
    raw_key = result.raw_key

    # Deactivate
    await get_db().execute(
        "UPDATE test_signing_apikeys SET is_active = false WHERE id = $1",
        result.instance.id,
    )

    # Verify fails (is_active=false)
    found = await TestAPIKey.verify(raw_key)
    check("Revoked key verify returns None", found is None)

    # But signature is still valid (HMAC doesn't care about DB state)
    check(
        "Revoked key signature still valid", TestAPIKey.verify_signature_only(raw_key)
    )


async def test_apikey_malformed_expiry_rejected():
    print("\n=== SignedAPIKeyMixin Malformed expires_at (fail closed) ===")

    result = await TestAPIKey.generate(user_id=7, name="expiry-key")
    raw_key = result.raw_key

    # Sanity: a valid, non-expired key verifies.
    check("valid key verifies before tampering", await TestAPIKey.verify(raw_key))

    # Corrupt expires_at to an unparseable value. A fail-OPEN implementation
    # would swallow the ValueError and treat the key as never-expiring; we must
    # fail CLOSED and reject it.
    await get_db().execute(
        "UPDATE test_signing_apikeys SET expires_at = $1 WHERE id = $2",
        "not-a-timestamp",
        result.instance.id,
    )
    found = await TestAPIKey.verify(raw_key)
    check("malformed expires_at rejected (fail closed)", found is None)

    # A well-formed future expiry still verifies (regression guard).
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await get_db().execute(
        "UPDATE test_signing_apikeys SET expires_at = $1 WHERE id = $2",
        future,
        result.instance.id,
    )
    check("valid future expiry still verifies", await TestAPIKey.verify(raw_key))


async def test_apikey_mixin_adversarial():
    print("\n=== SignedAPIKeyMixin Adversarial ===")

    result = await TestAPIKey.generate(user_id=4, name="adv-key")
    raw_key = result.raw_key

    # Tampered key
    tampered = tamper(raw_key)
    found = await TestAPIKey.verify(tampered)
    check("Tampered key rejected", found is None)

    # Wrong prefix
    wrong_prefix = "sk_other_" + raw_key[len("sk_test_") :]
    found = await TestAPIKey.verify(wrong_prefix)
    check("Wrong prefix rejected", found is None)

    # Empty key
    found = await TestAPIKey.verify("")
    check("Empty key rejected", found is None)

    # Just the prefix
    found = await TestAPIKey.verify("sk_test_")
    check("Just prefix rejected", found is None)

    # Random garbage
    found = await TestAPIKey.verify("sk_test_totally_fake_key_12345")
    check("Random garbage rejected", found is None)

    # Key from different engine
    other_engine = TokenEngine(keys=[SigningKey(secret="other-secret", version=0)])
    fake_signed = other_engine.encode_ref("fake_ref")
    found = await TestAPIKey.verify("sk_test_" + fake_signed)
    check("Cross-engine key rejected", found is None)


async def test_apikey_multiple_keys():
    print("\n=== Multiple API Keys ===")

    keys_and_results = []
    for i in range(5):
        result = await TestAPIKey.generate(user_id=10, name=f"key-{i}")
        keys_and_results.append(result)

    # All raw keys are unique
    raw_keys = [r.raw_key for r in keys_and_results]
    check("All 5 raw keys unique", len(set(raw_keys)) == 5)

    # All verify correctly
    for i, result in enumerate(keys_and_results):
        found = await TestAPIKey.verify(result.raw_key)
        check(f"Key {i} verifies", found is not None and found.name == f"key-{i}")


# ── Padded Mixin Tests ────────────────────────────────────────────────────


async def test_padded_session_mixin():
    print("\n=== Padded Session Mixin ===")

    session = PaddedSession(user_id=42)
    await session.save()
    check("Padded session token generated", session.token is not None)

    signed = session.signed_token
    check("Padded signed token produced", len(signed) > 20)

    # Decode works
    raw = PaddedSession.decode_signed_token(signed)
    check("Padded decode works", raw == session.token)

    # DB lookup works
    found = await PaddedSession.from_signed_token(signed)
    check("Padded DB lookup works", found is not None and found.user_id == 42)

    # Multiple calls produce different signed tokens (salted)
    s1 = session.signed_token
    s2 = session.signed_token
    check("Padded signed tokens non-deterministic", s1 != s2)

    # Both decode to same raw token
    check(
        "Padded both decode same",
        PaddedSession.decode_signed_token(s1) == PaddedSession.decode_signed_token(s2),
    )


async def test_padded_apikey_mixin():
    print("\n=== Padded API Key Mixin ===")

    result = await PaddedAPIKey.generate(user_id=50, name="padded-key")
    check("Padded key generated", result.raw_key.startswith("sk_pad_"))
    check("Padded key substantial", len(result.raw_key) > 40)

    found = await PaddedAPIKey.verify(result.raw_key)
    check("Padded key verifies", found is not None and found.user_id == 50)

    # Tampered — cycle the last char to a GUARANTEED-DIFFERENT one within its
    # own base62 class. It is the units digit of the signature, so any real
    # change alters the decoded HMAC bytes and must be rejected.
    tampered = tamper(result.raw_key)
    found = await PaddedAPIKey.verify(tampered)
    check("Padded tampered key rejected", found is None)


# ── Concurrent Operations ─────────────────────────────────────────────────


async def test_concurrent_mixin_ops():
    print("\n=== Concurrent Mixin Operations ===")

    # Generate 20 API keys concurrently
    tasks = [
        TestAPIKey.generate(user_id=100 + i, name=f"concurrent-{i}") for i in range(20)
    ]
    results = await asyncio.gather(*tasks)
    check("20 concurrent generates succeeded", len(results) == 20)

    # All unique
    raw_keys = [r.raw_key for r in results]
    check("All 20 concurrent keys unique", len(set(raw_keys)) == 20)

    # All verify
    verify_tasks = [TestAPIKey.verify(r.raw_key) for r in results]
    verified = await asyncio.gather(*verify_tasks)
    all_valid = all(v is not None for v in verified)
    check("All 20 concurrent keys verify", all_valid)


# ── Benchmark ─────────────────────────────────────────────────────────────


async def test_mixin_benchmark():
    print("\n=== Mixin Benchmark ===")

    # API key generate benchmark
    n = 50
    start = time.perf_counter()
    for i in range(n):
        await TestAPIKey.generate(user_id=200 + i, name=f"bench-{i}")
    elapsed = time.perf_counter() - start
    ops = n / elapsed
    check(f"API key generate: {ops:.0f} ops/sec", ops > 10, f"{elapsed:.2f}s for {n}")

    # API key verify benchmark (valid key)
    result = await TestAPIKey.generate(user_id=999, name="bench-verify")
    start = time.perf_counter()
    for _ in range(n):
        await TestAPIKey.verify(result.raw_key)
    elapsed = time.perf_counter() - start
    ops = n / elapsed
    check(f"API key verify: {ops:.0f} ops/sec", ops > 10, f"{elapsed:.2f}s for {n}")

    # API key reject benchmark (invalid key)
    start = time.perf_counter()
    for _ in range(100):
        await TestAPIKey.verify("sk_test_totally_invalid_key")
    elapsed = time.perf_counter() - start
    ops = 100 / elapsed
    check(
        f"API key reject (no DB hit): {ops:.0f} ops/sec",
        ops > 1000,
        f"{elapsed:.3f}s for 100",
    )


# ── Main ──────────────────────────────────────────────────────────────────


async def async_main():
    db = await setup_db()
    try:
        await test_session_mixin_basics()
        await test_session_mixin_invalid_tokens()
        await test_session_multiple_sessions()
        await test_apikey_mixin_generate()
        await test_apikey_mixin_verify()
        await test_apikey_mixin_revoke()
        await test_apikey_malformed_expiry_rejected()
        await test_apikey_mixin_adversarial()
        await test_apikey_multiple_keys()
        await test_padded_session_mixin()
        await test_padded_apikey_mixin()
        await test_concurrent_mixin_ops()
        await test_mixin_benchmark()
    finally:
        await teardown_db(db)


def main():
    print("Token Signing Mixin Tests")
    print("=" * 60)

    asyncio.run(async_main())

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"{total} tests: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for e in RESULTS["errors"]:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if RESULTS["failed"] else 0)


if __name__ == "__main__":
    main()
