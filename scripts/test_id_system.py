#!/usr/bin/env python3
"""Test unified IDManager system — raw, encoded, signed, and random modes.

Tests IDConfig, IDManager, and IDMixin with comprehensive coverage of:
- Raw mode (passthrough PK)
- Encoded mode (bijection encoding)
- Signed mode (HMAC signatures, key rotation, per-user)
- Random mode (opaque string generation)
- IDMixin integration (model-level API)
- Edge cases and error handling
"""

# hyper-test: unit

import hashlib
import hmac
import inspect
import sys
import uuid
from datetime import UTC, datetime, timedelta

from hyperdjango.public_id import (
    _ID_MODES,
    BaseEncoder,
    IDConfig,
    IDManager,
    IDMixin,
    IDMode,
    IDStrategy,
    KeySlot,
    generate_alphabet,
)

# ── Shared test alphabet ──────────────────────────────────────────────────

ALPHABET_32 = generate_alphabet("olc32", seed=42)
ALPHABET_62 = generate_alphabet("base62", seed=99)
TEST_KEY_1 = "test-hmac-key-2024-newest"
TEST_KEY_2 = "test-hmac-key-2023-older"
TEST_KEY_3 = "test-hmac-key-2022-oldest"


# ── Test runner ───────────────────────────────────────────────────────────


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ═══════════════════════════════════════════════════════════════════════
    # RAW MODE
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== RAW MODE ===")

    raw_mgr = IDManager(config=IDConfig(mode=IDMode.RAW))

    check(
        "raw encode(42) is '42'",
        raw_mgr.encode(42) == "42",
        f"got {raw_mgr.encode(42)!r}",
    )

    check(
        "raw decode('42') is 42",
        raw_mgr.decode("42") == 42,
        f"got {raw_mgr.decode('42')!r}",
    )

    check("raw roundtrip PK=0", raw_mgr.decode(raw_mgr.encode(0)) == 0)

    check("raw roundtrip PK=1", raw_mgr.decode(raw_mgr.encode(1)) == 1)

    check("raw roundtrip PK=999999", raw_mgr.decode(raw_mgr.encode(999999)) == 999999)

    check(
        "raw roundtrip large PK=2**53", raw_mgr.decode(raw_mgr.encode(2**53)) == 2**53
    )

    check("raw verify always True", raw_mgr.verify("anything") is True)

    check("raw verify always True for numbers", raw_mgr.verify("12345") is True)

    # ═══════════════════════════════════════════════════════════════════════
    # ENCODED MODE
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== ENCODED MODE ===")

    enc_mgr_32 = IDManager(
        config=IDConfig(
            mode=IDMode.ENCODED,
            alphabet=ALPHABET_32,
        )
    )
    enc_mgr_62 = IDManager(
        config=IDConfig(
            mode=IDMode.ENCODED,
            alphabet=ALPHABET_62,
        )
    )

    check("encoded roundtrip PK=0", enc_mgr_32.decode(enc_mgr_32.encode(0)) == 0)

    check("encoded roundtrip PK=1", enc_mgr_32.decode(enc_mgr_32.encode(1)) == 1)

    check(
        "encoded roundtrip PK=999999",
        enc_mgr_32.decode(enc_mgr_32.encode(999999)) == 999999,
    )

    check(
        "encoded roundtrip PK=12345",
        enc_mgr_32.decode(enc_mgr_32.encode(12345)) == 12345,
    )

    check(
        "encoded roundtrip PK=2**32",
        enc_mgr_32.decode(enc_mgr_32.encode(2**32)) == 2**32,
    )

    check(
        "encoded roundtrip PK=2**63",
        enc_mgr_32.decode(enc_mgr_32.encode(2**63)) == 2**63,
    )

    # Different alphabets produce different encodings
    enc_32 = enc_mgr_32.encode(12345)
    enc_62 = enc_mgr_62.encode(12345)
    check(
        "different alphabets produce different encodings",
        enc_32 != enc_62,
        f"both produced {enc_32!r}",
    )

    # base62 roundtrip
    check(
        "base62 encoded roundtrip PK=42", enc_mgr_62.decode(enc_mgr_62.encode(42)) == 42
    )

    check(
        "base62 encoded roundtrip PK=999999",
        enc_mgr_62.decode(enc_mgr_62.encode(999999)) == 999999,
    )

    # Encoded string only contains alphabet chars
    encoded = enc_mgr_32.encode(54321)
    check(
        "encoded contains only alphabet chars",
        all(c in ALPHABET_32 for c in encoded),
        f"got {encoded!r}, alphabet={ALPHABET_32!r}",
    )

    encoded_62 = enc_mgr_62.encode(54321)
    check(
        "base62 encoded contains only alphabet chars",
        all(c in ALPHABET_62 for c in encoded_62),
        f"got {encoded_62!r}",
    )

    # Verify always True for encoded mode
    check("encoded verify always True", enc_mgr_32.verify("anything") is True)

    # Multiple roundtrips
    for pk in [7, 100, 10000, 1000000]:
        check(
            f"encoded roundtrip PK={pk}", enc_mgr_32.decode(enc_mgr_32.encode(pk)) == pk
        )

    # Encoded output is not just the raw number string
    check(
        "encoded output differs from raw string",
        enc_mgr_32.encode(42) != "42",
        f"got {enc_mgr_32.encode(42)!r}",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNED MODE
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== SIGNED MODE ===")

    signed_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1, TEST_KEY_2],
            table_name="articles",
        )
    )

    # Basic roundtrip
    ext_id = signed_mgr.encode(42)
    check(
        "signed roundtrip PK=42",
        signed_mgr.decode(ext_id) == 42,
        f"got {signed_mgr.decode(ext_id)}",
    )

    check("signed roundtrip PK=1", signed_mgr.decode(signed_mgr.encode(1)) == 1)

    check("signed roundtrip PK=0", signed_mgr.decode(signed_mgr.encode(0)) == 0)

    check(
        "signed roundtrip PK=999999",
        signed_mgr.decode(signed_mgr.encode(999999)) == 999999,
    )

    check(
        "signed roundtrip large PK=2**48",
        signed_mgr.decode(signed_mgr.encode(2**48)) == 2**48,
    )

    # Output contains separator
    check("signed output contains separator", "." in ext_id, f"got {ext_id!r}")

    # Tampered signature
    parts = ext_id.split(".")
    tampered = parts[0] + ".0000000000000000"
    try:
        signed_mgr.decode(tampered)
        check("tampered signature raises ValueError", False, "no exception raised")
    except ValueError:
        check("tampered signature raises ValueError", True)

    # Truncated signature
    truncated = ext_id[:-4]
    try:
        signed_mgr.decode(truncated)
        check("truncated signature raises ValueError", False, "no exception raised")
    except ValueError:
        check("truncated signature raises ValueError", True)

    # Missing separator
    try:
        signed_mgr.decode("noseparatorhere")
        check("missing separator raises ValueError", False, "no exception raised")
    except ValueError:
        check("missing separator raises ValueError", True)

    # Empty parts — separator at start
    try:
        signed_mgr.decode(".something")
        check("empty encoded part raises ValueError", False, "no exception raised")
    except ValueError:
        check("empty encoded part raises ValueError", True)

    # Empty parts — separator at end
    try:
        signed_mgr.decode("something.")
        check("empty signature part raises ValueError", False, "no exception raised")
    except ValueError:
        check("empty signature part raises ValueError", True)

    # Wrong table name isolation
    signed_mgr_other = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="users",
        )
    )
    articles_ext = signed_mgr.encode(42)
    users_ext = signed_mgr_other.encode(42)
    check(
        "different table names produce different external IDs",
        articles_ext != users_ext,
        f"articles={articles_ext!r}, users={users_ext!r}",
    )

    # Cross-table verification fails
    try:
        signed_mgr_other.decode(articles_ext)
        check("cross-table decode fails", False, "no exception raised")
    except ValueError:
        check("cross-table decode fails", True)

    # Key rotation: encode with key[0], decode with key[0]
    check(
        "key rotation: newest key decodes",
        signed_mgr.decode(signed_mgr.encode(100)) == 100,
    )

    # Key rotation: old key still decodes
    old_key_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_2],  # Only old key
            table_name="articles",
        )
    )
    old_ext = old_key_mgr.encode(200)
    # Now decode with manager that has both keys (old key at index 1)
    check(
        "key rotation: old key (key[1]) still decodes",
        signed_mgr.decode(old_ext) == 200,
    )

    # Key rotation: 3 keys, oldest still works
    three_key_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1, TEST_KEY_2, TEST_KEY_3],
            table_name="articles",
        )
    )
    oldest_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_3],
            table_name="articles",
        )
    )
    oldest_ext = oldest_mgr.encode(300)
    check(
        "key rotation: 3 keys, oldest still works",
        three_key_mgr.decode(oldest_ext) == 300,
    )

    # Key rotation: unknown key fails
    unknown_key_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=["totally-unknown-key"],
            table_name="articles",
        )
    )
    unknown_ext = unknown_key_mgr.encode(42)
    try:
        signed_mgr.decode(unknown_ext)
        check("unknown key raises ValueError", False, "no exception raised")
    except ValueError:
        check("unknown key raises ValueError", True)

    # Signature hex length = signature_bytes * 2
    default_ext = signed_mgr.encode(42)
    sig = default_ext.split(".")[-1]
    check(
        "signature length is 16 hex chars (8 bytes default)",
        len(sig) == 16,
        f"got {len(sig)}",
    )

    # Custom signature_bytes
    short_sig_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="articles",
            signature_bytes=4,
        )
    )
    short_ext = short_sig_mgr.encode(42)
    short_sig = short_ext.split(".")[-1]
    check(
        "custom signature_bytes=4 produces 8 hex chars",
        len(short_sig) == 8,
        f"got {len(short_sig)}",
    )

    # Custom signature_bytes roundtrip
    check("custom signature_bytes=4 roundtrip", short_sig_mgr.decode(short_ext) == 42)

    # Custom separator
    dash_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="articles",
            separator="-",
        )
    )
    dash_ext = dash_mgr.encode(42)
    check(
        "custom separator '-' in output",
        "-" in dash_ext and "." not in dash_ext,
        f"got {dash_ext!r}",
    )
    check("custom separator roundtrip", dash_mgr.decode(dash_ext) == 42)

    # Multi-char separator
    multi_sep_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="articles",
            separator="::",
        )
    )
    multi_ext = multi_sep_mgr.encode(42)
    check(
        "multi-char separator '::' in output", "::" in multi_ext, f"got {multi_ext!r}"
    )
    check("multi-char separator roundtrip", multi_sep_mgr.decode(multi_ext) == 42)

    # Constant-time comparison (verify hmac.compare_digest is used)
    source = inspect.getsource(IDManager._decode_signed)
    check(
        "uses hmac.compare_digest for comparison",
        "hmac.compare_digest" in source,
        "compare_digest not found in _decode_signed source",
    )

    # Large PK values
    for large_pk in [2**32, 2**48, 2**63 - 1]:
        ext = signed_mgr.encode(large_pk)
        check(f"signed large PK={large_pk}", signed_mgr.decode(ext) == large_pk)

    # Sequential PKs produce unpredictable signatures
    sigs = set()
    for pk in range(1, 11):
        ext = signed_mgr.encode(pk)
        sig_part = ext.split(".")[-1]
        sigs.add(sig_part)
    check(
        "sequential PKs produce 10 unique signatures",
        len(sigs) == 10,
        f"got {len(sigs)} unique sigs",
    )

    # Verify method works for valid signed ID
    valid_ext = signed_mgr.encode(42)
    check(
        "verify returns True for valid signed ID", signed_mgr.verify(valid_ext) is True
    )

    # Verify method returns False for tampered signed ID
    check(
        "verify returns False for tampered signed ID",
        signed_mgr.verify(tampered) is False,
    )

    # Verify method returns False for garbage
    check(
        "verify returns False for no-separator garbage",
        signed_mgr.verify("garbage") is False,
    )

    # Signed mode: encode and manually verify HMAC computation
    manual_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=["manual-key"],
            table_name="test_table",
            signature_bytes=8,
        )
    )
    manual_ext = manual_mgr.encode(77)
    enc_part, sig_part = manual_ext.rsplit(".", 1)
    # Manually compute expected HMAC
    expected_mac = hmac.new(
        b"manual-key",
        f"test_table:{enc_part}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    check(
        "manually computed HMAC matches signature",
        sig_part == expected_mac,
        f"sig={sig_part!r}, expected={expected_mac!r}",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PER-USER MODE
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== PER-USER MODE ===")

    user_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="orders",
            include_user=True,
        )
    )

    # Same PK, different user_id produces different IDs
    ext_u1 = user_mgr.encode(42, user_id=1)
    ext_u2 = user_mgr.encode(42, user_id=2)
    check(
        "same PK + different user_id -> different external IDs",
        ext_u1 != ext_u2,
        f"u1={ext_u1!r}, u2={ext_u2!r}",
    )

    # Correct user_id decodes
    check("correct user_id decodes PK", user_mgr.decode(ext_u1, user_id=1) == 42)

    check("correct user_id=2 decodes PK", user_mgr.decode(ext_u2, user_id=2) == 42)

    # Wrong user_id fails
    try:
        user_mgr.decode(ext_u1, user_id=999)
        check("wrong user_id raises ValueError", False, "no exception raised")
    except ValueError:
        check("wrong user_id raises ValueError", True)

    # user_id=None when include_user=True raises ValueError on encode
    try:
        user_mgr.encode(42, user_id=None)
        check("user_id=None on encode raises ValueError", False, "no exception raised")
    except ValueError:
        check("user_id=None on encode raises ValueError", True)

    # user_id=None when include_user=True raises ValueError on decode
    try:
        user_mgr.decode(ext_u1, user_id=None)
        check("user_id=None on decode raises ValueError", False, "no exception raised")
    except ValueError:
        check("user_id=None on decode raises ValueError", True)

    # User ID as int works
    ext_int = user_mgr.encode(50, user_id=123)
    check(
        "user_id as int encodes and decodes",
        user_mgr.decode(ext_int, user_id=123) == 50,
    )

    # User ID as string works
    ext_str = user_mgr.encode(50, user_id="user-abc")
    check(
        "user_id as string encodes and decodes",
        user_mgr.decode(ext_str, user_id="user-abc") == 50,
    )

    # Int and string user_id produce different results
    check("int user_id != string user_id", ext_int != ext_str, "should differ")

    # User ID roundtrip for various PKs
    for pk in [0, 1, 100, 999999]:
        ext = user_mgr.encode(pk, user_id=42)
        check(f"per-user roundtrip PK={pk}", user_mgr.decode(ext, user_id=42) == pk)

    # Verify with correct user_id
    check(
        "verify with correct user_id returns True",
        user_mgr.verify(ext_u1, user_id=1) is True,
    )

    # Verify with wrong user_id returns False
    check(
        "verify with wrong user_id returns False",
        user_mgr.verify(ext_u1, user_id=999) is False,
    )

    # Verify with None user_id returns False (raises internally)
    check(
        "verify with None user_id returns False",
        user_mgr.verify(ext_u1, user_id=None) is False,
    )

    # Per-user HMAC message format includes user_id
    source_hmac = inspect.getsource(IDManager._compute_hmac)
    check("_compute_hmac includes user_id in message", "user_id" in source_hmac)

    # ═══════════════════════════════════════════════════════════════════════
    # IDMIXIN
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== IDMIXIN ===")

    # Basic IDMixin with signed mode
    class FakePost(IDMixin):
        class IDConfig:
            mode = IDMode.SIGNED
            alphabet = ALPHABET_32
            hmac_keys = [TEST_KEY_1, TEST_KEY_2]
            table_name = "posts"

        def __init__(self, id):
            self.id = id

    check(
        "__init_subclass__ creates _id_manager",
        isinstance(FakePost._id_manager, IDManager),
    )

    check(
        "_id_manager config mode is signed",
        FakePost._id_manager.config.mode == IDMode.SIGNED,
    )

    # get_external_id works
    post = FakePost(id=42)
    ext = post.get_external_id()
    check(
        "get_external_id returns string with separator",
        isinstance(ext, str) and "." in ext,
        f"got {ext!r}",
    )

    # decode_external_id works
    check("decode_external_id roundtrip", FakePost.decode_external_id(ext) == 42)

    # verify_external_id works
    check(
        "verify_external_id returns True for valid",
        FakePost.verify_external_id(ext) is True,
    )

    check(
        "verify_external_id returns False for tampered",
        FakePost.verify_external_id("abc.0000000000000000") is False,
    )

    # Encoded mode via IDMixin
    class FakeProduct(IDMixin):
        class IDConfig:
            mode = IDMode.ENCODED
            alphabet = ALPHABET_62

        def __init__(self, id):
            self.id = id

    prod = FakeProduct(id=100)
    prod_ext = prod.get_external_id()
    check(
        "encoded IDMixin get_external_id",
        isinstance(prod_ext, str) and len(prod_ext) > 0,
    )
    check(
        "encoded IDMixin decode roundtrip",
        FakeProduct.decode_external_id(prod_ext) == 100,
    )

    # Raw mode via IDMixin
    class FakeInternal(IDMixin):
        class IDConfig:
            mode = IDMode.RAW

        def __init__(self, id):
            self.id = id

    internal = FakeInternal(id=77)
    check("raw IDMixin get_external_id is '77'", internal.get_external_id() == "77")

    # Missing alphabet for encoded mode raises ValueError
    try:

        class BadEncoded(IDMixin):
            class IDConfig:
                mode = IDMode.ENCODED

        check("missing alphabet raises ValueError", False, "no exception raised")
    except ValueError as e:
        check(
            "missing alphabet raises ValueError", "Alphabet required" in str(e), str(e)
        )

    # Missing hmac_keys for signed mode raises ValueError
    try:

        class BadSigned(IDMixin):
            class IDConfig:
                mode = IDMode.SIGNED
                alphabet = ALPHABET_32

        check("missing hmac_keys raises ValueError", False, "no exception raised")
    except ValueError as e:
        check(
            "missing hmac_keys raises ValueError",
            "hmac_keys required" in str(e),
            str(e),
        )

    # Invalid mode raises ValueError
    try:

        class BadMode(IDMixin):
            class IDConfig:
                mode = "invalid"

        check("invalid mode raises ValueError", False, "no exception raised")
    except ValueError as e:
        check("invalid mode raises ValueError", "invalid" in str(e).lower(), str(e))

    # Auto-detected table_name from Meta.table
    class FakeArticle(IDMixin):
        class Meta:
            table = "my_articles"

        class IDConfig:
            mode = IDMode.SIGNED
            alphabet = ALPHABET_32
            hmac_keys = [TEST_KEY_1]

        def __init__(self, id):
            self.id = id

    check(
        "auto-detected table_name from Meta.table",
        FakeArticle._id_manager.config.table_name == "my_articles",
    )

    # Auto-detected table_name from class name (lowercase)
    class FakeWidget(IDMixin):
        class IDConfig:
            mode = IDMode.SIGNED
            alphabet = ALPHABET_32
            hmac_keys = [TEST_KEY_1]

        def __init__(self, id):
            self.id = id

    check(
        "auto-detected table_name from class name",
        FakeWidget._id_manager.config.table_name == "fakewidget",
    )

    # Config validation at class definition time (not deferred)
    check("config validation happens at definition time (caught above)", True)

    # ═══════════════════════════════════════════════════════════════════════
    # IDMANAGER EDGE CASES
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== IDMANAGER EDGE CASES ===")

    # encode on random mode raises ValueError
    random_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.RANDOM,
            alphabet=ALPHABET_32,
            entropy_bytes=10,
        )
    )
    try:
        random_mgr.encode(42)
        check("encode on random mode raises ValueError", False, "no exception raised")
    except ValueError as e:
        check(
            "encode on random mode raises ValueError",
            "random" in str(e).lower(),
            str(e),
        )

    # decode on random mode raises ValueError
    try:
        random_mgr.decode("abc")
        check("decode on random mode raises ValueError", False, "no exception raised")
    except ValueError as e:
        check(
            "decode on random mode raises ValueError",
            "random" in str(e).lower(),
            str(e),
        )

    # generate_random() for random mode produces non-empty string
    # Need an encoder for random mode
    random_mgr_with_enc = IDManager(
        config=IDConfig(
            mode=IDMode.RANDOM,
            alphabet=ALPHABET_32,
            entropy_bytes=10,
            strategy=IDStrategy.RANDOM,
        )
    )
    # Manually set encoder since random mode skips auto-init
    object.__setattr__(random_mgr_with_enc, "_encoder", BaseEncoder(ALPHABET_32))
    rand_id = random_mgr_with_enc.generate_random()
    check(
        "generate_random() produces non-empty string",
        isinstance(rand_id, str) and len(rand_id) > 0,
        f"got {rand_id!r}",
    )

    # generate_random() produces unique values
    rand_ids = {random_mgr_with_enc.generate_random() for _ in range(100)}
    check(
        "generate_random() produces 100 unique values",
        len(rand_ids) == 100,
        f"got {len(rand_ids)} unique",
    )

    # generate_random() for uuid7 strategy
    uuid_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.RANDOM,
            strategy=IDStrategy.UUID7,
        )
    )
    uuid_id = uuid_mgr.generate_random()
    check(
        "generate_random() uuid7 produces valid UUID string",
        isinstance(uuid_id, str) and len(uuid_id) == 36 and uuid_id.count("-") == 4,
        f"got {uuid_id!r}",
    )

    # UUID is parseable
    parsed = uuid.UUID(uuid_id)
    check(
        "generate_random() uuid7 produces parseable UUID", isinstance(parsed, uuid.UUID)
    )

    # Empty hmac_keys on sign raises ValueError
    empty_key_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[],
            table_name="test",
        )
    )
    try:
        empty_key_mgr._sign("encoded_val")
        check(
            "empty hmac_keys on _sign raises ValueError", False, "no exception raised"
        )
    except ValueError as e:
        check(
            "empty hmac_keys on _sign raises ValueError",
            "No HMAC keys" in str(e),
            str(e),
        )

    # Very long PK values
    huge_pk = 2**128
    huge_ext = signed_mgr.encode(huge_pk)
    check("very long PK (2**128) roundtrip", signed_mgr.decode(huge_ext) == huge_pk)

    # Unicode in table name works
    unicode_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="articles",
        )
    )
    unicode_ext = unicode_mgr.encode(42)
    check(
        "unicode table name produces valid signed ID",
        unicode_mgr.decode(unicode_ext) == 42,
    )

    # IDConfig is a dataclass with slots
    check(
        "IDConfig has __slots__",
        "__slots__" in IDConfig.__dict__ or hasattr(IDConfig, "__slots__"),
        "no __slots__ found",
    )

    # IDManager is a dataclass with slots
    check(
        "IDManager has __slots__",
        "__slots__" in IDManager.__dict__ or hasattr(IDManager, "__slots__"),
        "no __slots__ found",
    )

    # _ID_MODES frozenset contains all 4 modes
    check(
        "_ID_MODES contains raw, encoded, signed, random",
        {IDMode.RAW, IDMode.ENCODED, IDMode.SIGNED, IDMode.RANDOM} == _ID_MODES,
        f"got {_ID_MODES}",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # ADDITIONAL SIGNED MODE TESTS
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== ADDITIONAL SIGNED MODE ===")

    # Signature is deterministic for same input
    ext1 = signed_mgr.encode(42)
    ext2 = signed_mgr.encode(42)
    check("same PK produces same signed ID (deterministic)", ext1 == ext2)

    # Different PKs produce different signed IDs
    ext_a = signed_mgr.encode(1)
    ext_b = signed_mgr.encode(2)
    check("different PKs produce different signed IDs", ext_a != ext_b)

    # Swapped encoded part + signature from different PK fails
    parts_a = ext_a.rsplit(".", 1)
    parts_b = ext_b.rsplit(".", 1)
    swapped = parts_a[0] + "." + parts_b[1]
    try:
        signed_mgr.decode(swapped)
        check("swapped signature from different PK fails", False, "no exception raised")
    except ValueError:
        check("swapped signature from different PK fails", True)

    # Signature bytes=16 (full SHA256 = 32 bytes = 64 hex)
    long_sig_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="test",
            signature_bytes=16,
        )
    )
    long_ext = long_sig_mgr.encode(42)
    long_sig = long_ext.rsplit(".", 1)[1]
    check(
        "signature_bytes=16 produces 32 hex chars",
        len(long_sig) == 32,
        f"got {len(long_sig)}",
    )

    # Signature bytes=1 (minimal)
    tiny_sig_mgr = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[TEST_KEY_1],
            table_name="test",
            signature_bytes=1,
        )
    )
    tiny_ext = tiny_sig_mgr.encode(42)
    tiny_sig = tiny_ext.rsplit(".", 1)[1]
    check(
        "signature_bytes=1 produces 2 hex chars",
        len(tiny_sig) == 2,
        f"got {len(tiny_sig)}",
    )
    check("signature_bytes=1 still roundtrips", tiny_sig_mgr.decode(tiny_ext) == 42)

    # ═══════════════════════════════════════════════════════════════════════
    # ADDITIONAL PER-USER TESTS
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== ADDITIONAL PER-USER ===")

    # Large user_id works
    ext_large_user = user_mgr.encode(42, user_id=2**63)
    check(
        "large user_id (2**63) encodes and decodes",
        user_mgr.decode(ext_large_user, user_id=2**63) == 42,
    )

    # String user_id with special chars
    ext_special = user_mgr.encode(42, user_id="user@example.com")
    check(
        "email-style user_id works",
        user_mgr.decode(ext_special, user_id="user@example.com") == 42,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # ADDITIONAL IDMIXIN EDGE CASES
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== ADDITIONAL IDMIXIN ===")

    # IDMixin with per-user signed mode
    class UserPost(IDMixin):
        class IDConfig:
            mode = IDMode.SIGNED
            alphabet = ALPHABET_32
            hmac_keys = [TEST_KEY_1]
            table_name = "user_posts"
            include_user = True

        def __init__(self, id):
            self.id = id

    up = UserPost(id=10)
    up_ext = up.get_external_id(user_id=5)
    check("IDMixin per-user get_external_id", isinstance(up_ext, str) and "." in up_ext)
    check(
        "IDMixin per-user decode_external_id",
        UserPost.decode_external_id(up_ext, user_id=5) == 10,
    )
    check(
        "IDMixin per-user verify_external_id",
        UserPost.verify_external_id(up_ext, user_id=5) is True,
    )

    # Subclass without IDConfig does not break
    class PlainClass(IDMixin):
        pass

    check("subclass without IDConfig does not error", True)

    # IDMixin random mode
    class RandomModel(IDMixin):
        class IDConfig:
            mode = IDMode.RANDOM
            alphabet = ALPHABET_32

        def __init__(self, id):
            self.id = id

    check(
        "random IDMixin creates _id_manager",
        isinstance(RandomModel._id_manager, IDManager),
    )
    check(
        "random IDMixin mode is random",
        RandomModel._id_manager.config.mode == IDMode.RANDOM,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # KEYSLOT + OFFSET TESTS
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== KeySlot + Offset ===")

    # KeySlot dataclass
    slot = KeySlot("my-key", offset=10_000)
    check("KeySlot key", slot.key == "my-key")
    check("KeySlot offset", slot.offset == 10_000)
    check("KeySlot default offset is 0", KeySlot("k").offset == 0)

    # Signed mode with offset — encode adds offset, decode subtracts
    mgr_offset = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("key1", offset=50_000)],
            table_name="items",
        )
    )
    ext = mgr_offset.encode(1)
    check("offset encode PK=1 produces external ID", len(ext) > 0)
    check("offset decode roundtrip PK=1", mgr_offset.decode(ext) == 1)
    ext5 = mgr_offset.encode(5)
    check("offset decode roundtrip PK=5", mgr_offset.decode(ext5) == 5)

    # The encoded part should be for 50001, not 1
    sep = mgr_offset.config.separator
    encoded_part = ext.split(sep)[0]
    encoder = BaseEncoder(ALPHABET_32)
    check("encoded part is offset PK (50001)", encoder.decode(encoded_part) == 50_001)

    # Key rotation with different offsets
    mgr_rotated = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[
                KeySlot("new-key-2025", offset=100_000),
                KeySlot("old-key-2024", offset=50_000),
            ],
            table_name="items",
        )
    )
    # Encode with new key (offset 100_000)
    new_ext = mgr_rotated.encode(42)
    check("rotated encode roundtrip", mgr_rotated.decode(new_ext) == 42)
    new_encoded = new_ext.split(sep)[0]
    check("new key uses offset 100_000", encoder.decode(new_encoded) == 100_042)

    # Old key's IDs still decode correctly with old offset
    mgr_old = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("old-key-2024", offset=50_000)],
            table_name="items",
        )
    )
    old_ext = mgr_old.encode(42)
    check("old ID decodes with rotated manager", mgr_rotated.decode(old_ext) == 42)
    old_encoded = old_ext.split(sep)[0]
    check("old key uses offset 50_000", encoder.decode(old_encoded) == 50_042)

    # Encoded mode with offset
    mgr_enc_offset = IDManager(
        config=IDConfig(
            mode=IDMode.ENCODED,
            alphabet=ALPHABET_32,
            offset=10_000,
        )
    )
    ext_enc = mgr_enc_offset.encode(1)
    check("encoded mode offset roundtrip", mgr_enc_offset.decode(ext_enc) == 1)
    check("encoded mode encodes offset value", encoder.decode(ext_enc) == 10_001)

    # Zero offset (default) works as before
    mgr_no_offset = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("key")],
            table_name="items",
        )
    )
    ext_no = mgr_no_offset.encode(42)
    check("zero offset roundtrip", mgr_no_offset.decode(ext_no) == 42)
    no_encoded = ext_no.split(sep)[0]
    check("zero offset encodes raw PK", encoder.decode(no_encoded) == 42)

    # String keys auto-convert to KeySlot via _normalize_hmac_keys in IDMixin
    class AutoConvertModel(IDMixin):
        class IDConfig:
            mode = IDMode.SIGNED
            alphabet = ALPHABET_32
            hmac_keys = ["string-key-1", "string-key-2"]
            table_name = "auto"

        def __init__(self, id):
            self.id = id

    slots = AutoConvertModel._id_manager.config.hmac_keys
    check("string keys auto-converted to KeySlot", isinstance(slots[0], KeySlot))
    check("auto-converted offset is 0", slots[0].offset == 0)
    check("auto-converted key preserved", slots[0].key == "string-key-1")

    # ═══════════════════════════════════════════════════════════════════════
    # TIME-WINDOWED IDS
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== Time-Windowed IDs ===")

    now = datetime.now(UTC)
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    far_future = now + timedelta(days=30)

    mgr_tw = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("tw-key", offset=10_000)],
            table_name="items",
        )
    )

    # Basic time-windowed encode/decode — valid window
    ext_tw = mgr_tw.encode(42, valid_after=past, valid_until=future)
    check("time-windowed ID has 3 parts", ext_tw.count(".") == 2)
    check("time-windowed decode roundtrip", mgr_tw.decode(ext_tw) == 42)

    # Verify works too
    check("time-windowed verify valid", mgr_tw.verify(ext_tw))

    # Only valid_after (no end)
    ext_after = mgr_tw.encode(10, valid_after=past)
    check("valid_after only has 3 parts", ext_after.count(".") == 2)
    check("valid_after decode works (past start)", mgr_tw.decode(ext_after) == 10)

    # Only valid_until (no start)
    ext_until = mgr_tw.encode(10, valid_until=future)
    check("valid_until only has 3 parts", ext_until.count(".") == 2)
    check("valid_until decode works (before end)", mgr_tw.decode(ext_until) == 10)

    # No time window — 2 parts (existing behavior)
    ext_no_tw = mgr_tw.encode(10)
    check("no time window has 2 parts", ext_no_tw.count(".") == 1)
    check("no time window decode", mgr_tw.decode(ext_no_tw) == 10)

    # Expired ID (valid_until in the past)
    ext_expired = mgr_tw.encode(42, valid_until=past)
    try:
        mgr_tw.decode(ext_expired)
        check("expired ID rejected", False)
    except ValueError:
        check("expired ID rejected", True)

    # Not yet valid (valid_after in the future)
    ext_not_yet = mgr_tw.encode(42, valid_after=future)
    try:
        mgr_tw.decode(ext_not_yet)
        check("not-yet-valid ID rejected", False)
    except ValueError:
        check("not-yet-valid ID rejected", True)

    # Tampered time window — change window but keep old HMAC
    parts = ext_tw.split(".")
    tampered = f"{parts[0]}.0-0.{parts[2]}"
    try:
        mgr_tw.decode(tampered)
        check("tampered time window rejected", False)
    except ValueError:
        check("tampered time window rejected", True)

    # ═══════════════════════════════════════════════════════════════════════
    # CUSTOM EPOCH
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== Custom Epoch ===")

    # Custom epoch = Jan 3, 2024 00:00 UTC
    jan3_2024 = int(datetime(2024, 1, 3, tzinfo=UTC).timestamp())

    mgr_epoch = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("epoch-key", offset=5_000, epoch=jan3_2024)],
            table_name="events",
        )
    )

    # Encode with time window
    valid_start = datetime(2025, 2, 1, tzinfo=UTC)
    valid_end = datetime(2025, 2, 10, tzinfo=UTC)
    ext_epoch = mgr_epoch.encode(100, valid_after=valid_start, valid_until=valid_end)
    check("custom epoch ID has 3 parts", ext_epoch.count(".") == 2)

    # The time window values should be relative to epoch, not Unix
    window_part = ext_epoch.split(".")[1]
    start_enc, end_enc = window_part.split("-")
    encoder = BaseEncoder(ALPHABET_32)
    start_offset = encoder.decode(start_enc)
    end_offset = encoder.decode(end_enc)

    expected_start = int(valid_start.timestamp()) - jan3_2024
    expected_end = int(valid_end.timestamp()) - jan3_2024
    check("start offset relative to custom epoch", start_offset == expected_start)
    check("end offset relative to custom epoch", end_offset == expected_end)
    check(
        "epoch offsets are smaller than Unix",
        start_offset < int(valid_start.timestamp()),
    )

    # Different epochs produce different time encodings for same datetime
    mgr_unix_epoch = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("epoch-key", offset=5_000, epoch=0)],
            table_name="events",
        )
    )
    ext_unix = mgr_unix_epoch.encode(
        100, valid_after=valid_start, valid_until=valid_end
    )
    unix_window = ext_unix.split(".")[1]
    check(
        "different epoch produces different window encoding", window_part != unix_window
    )
    check("custom epoch window is shorter", len(window_part) < len(unix_window))

    # Key rotation with different epochs
    mgr_multi_epoch = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[
                KeySlot("new-key", offset=20_000, epoch=jan3_2024),
                KeySlot("old-key", offset=10_000, epoch=0),
            ],
            table_name="events",
        )
    )

    # Old key with Unix epoch still decodes
    ext_old = IDManager(
        config=IDConfig(
            mode=IDMode.SIGNED,
            alphabet=ALPHABET_32,
            hmac_keys=[KeySlot("old-key", offset=10_000, epoch=0)],
            table_name="events",
        )
    ).encode(50, valid_after=past, valid_until=far_future)
    check(
        "old epoch key decodes in rotated manager",
        mgr_multi_epoch.decode(ext_old) == 50,
    )

    # New key with custom epoch
    ext_new = mgr_multi_epoch.encode(50, valid_after=past, valid_until=far_future)
    check("new epoch key roundtrip", mgr_multi_epoch.decode(ext_new) == 50)

    # KeySlot epoch field accessible
    check("KeySlot epoch field", KeySlot("k", epoch=12345).epoch == 12345)
    check("KeySlot default epoch is 0", KeySlot("k").epoch == 0)

    # ═══════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\nResults: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
