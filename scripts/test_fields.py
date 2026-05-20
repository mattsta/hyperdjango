"""Comprehensive tests for hyperdjango.fields — custom fields module."""

# hyper-test: unit

import json
import sys
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperdjango.fields import (
    ChoiceField,
    CIDRField,
    ColorField,
    CustomField,
    DurationField,
    EmailField,
    EncryptedField,
    IPAddressField,
    JSONField,
    MoneyField,
    PercentField,
    PhoneField,
    SlugField,
    URLField,
    UUIDField,
    convert_from_db,
    convert_to_db,
    create_field,
    get_column_type,
    get_custom_field,
    register_field,
    slugify,
    unregister_field,
)

PASS = 0
FAIL = 0


def test(name, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    got:      {got!r}")
        print(f"    expected: {expected!r}")


def test_raises(name, fn, exc_type=ValueError):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        print(f"  FAIL: {name} (no exception raised)")
    except exc_type:
        PASS += 1
    except Exception as e:
        FAIL += 1
        print(f"  FAIL: {name} (wrong exception: {type(e).__name__}: {e})")


# ---------------------------------------------------------------------------
# CustomField base class
# ---------------------------------------------------------------------------


def test_custom_field_base():
    cf = CustomField()
    test_raises(
        "base.db_type raises NotImplementedError", cf.db_type, NotImplementedError
    )
    test("base.to_db_value passthrough", cf.to_db_value(42), 42)
    test("base.to_db_value passthrough str", cf.to_db_value("hello"), "hello")
    test("base.from_db_value passthrough", cf.from_db_value(99), 99)
    test("base.from_db_value passthrough None", cf.from_db_value(None), None)
    test("base.validate passthrough", cf.validate("anything"), "anything")
    test("base.validate passthrough int", cf.validate(123), 123)
    test("base.to_representation passthrough", cf.to_representation([1, 2]), [1, 2])
    test("base.to_internal_value passthrough", cf.to_internal_value({"a": 1}), {"a": 1})
    test("base.form_field_type default", cf.form_field_type(), "text")
    test("base.form_widget_attrs default", cf.form_widget_attrs(), {})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry():
    # Clean state
    class DummyType:
        pass

    class DummyField(CustomField):
        def db_type(self):
            return "text"

    df = DummyField()
    test("registry.get unregistered returns None", get_custom_field(DummyType), None)

    register_field(DummyType, df)
    test("registry.get after register", get_custom_field(DummyType), df)

    result = unregister_field(DummyType)
    test("registry.unregister returns True", result, True)
    test("registry.get after unregister", get_custom_field(DummyType), None)

    result2 = unregister_field(DummyType)
    test("registry.unregister missing returns False", result2, False)

    # Overwrite
    df2 = DummyField()
    register_field(DummyType, df)
    register_field(DummyType, df2)
    test("registry.overwrite", get_custom_field(DummyType), df2)
    unregister_field(DummyType)

    # Thread safety
    errors = []

    class T1:
        pass

    def register_many(start, count):
        try:
            for i in range(start, start + count):

                class LocalType:
                    pass

                register_field(LocalType, DummyField())
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=register_many, args=(i * 100, 100)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    test("registry.thread safety no errors", len(errors), 0)


# ---------------------------------------------------------------------------
# create_field integration
# ---------------------------------------------------------------------------


def test_create_field():
    mf = MoneyField(currency="EUR")
    fi = create_field(mf)
    test("create_field.custom_field attached", fi.custom_field, mf)

    col = get_column_type(fi)
    test("create_field.get_column_type", col, "bigint")

    db_val = convert_to_db(fi, Decimal("19.99"))
    test("create_field.convert_to_db", db_val, 1999)

    py_val = convert_from_db(fi, 1999)
    test("create_field.convert_from_db", py_val, Decimal("19.99"))

    # No custom field
    fi2 = create_field(None)
    test("create_field.no custom field column type", get_column_type(fi2), None)
    test(
        "create_field.no custom field convert_to_db passthrough",
        convert_to_db(fi2, 42),
        42,
    )
    test(
        "create_field.no custom field convert_from_db passthrough",
        convert_from_db(fi2, 42),
        42,
    )


# ---------------------------------------------------------------------------
# MoneyField
# ---------------------------------------------------------------------------


def test_money_field():
    mf = MoneyField()
    test("money.db_type", mf.db_type(), "bigint")
    test("money.default currency", mf.currency, "USD")
    test("money.default decimal_places", mf.decimal_places, 2)

    # to_db_value
    test("money.to_db 19.99", mf.to_db_value(Decimal("19.99")), 1999)
    test("money.to_db 0", mf.to_db_value(Decimal(0)), 0)
    test("money.to_db 100", mf.to_db_value(Decimal("100.00")), 10000)
    test("money.to_db None", mf.to_db_value(None), None)
    test("money.to_db 0.01", mf.to_db_value(Decimal("0.01")), 1)
    test("money.to_db large", mf.to_db_value(Decimal("99999.99")), 9999999)

    # from_db_value
    test("money.from_db 1999", mf.from_db_value(1999), Decimal("19.99"))
    test("money.from_db 0", mf.from_db_value(0), Decimal(0))
    test("money.from_db None", mf.from_db_value(None), None)
    test("money.from_db 1", mf.from_db_value(1), Decimal("0.01"))

    # validate
    test("money.validate Decimal", mf.validate(Decimal("19.99")), Decimal("19.99"))
    test("money.validate None", mf.validate(None), None)
    test("money.validate zero", mf.validate(Decimal(0)), Decimal(0))
    test_raises("money.validate negative", lambda: mf.validate(Decimal(-1)))
    test_raises("money.validate non-numeric", lambda: mf.validate("abc"))
    test_raises("money.validate too large", lambda: mf.validate(Decimal(10000000000)))

    # to_representation
    rep = mf.to_representation(Decimal("19.99"))
    test("money.to_repr amount", rep["amount"], "19.99")
    test("money.to_repr currency", rep["currency"], "USD")
    test("money.to_repr None", mf.to_representation(None), None)

    # to_internal_value
    test(
        "money.to_internal dict",
        mf.to_internal_value({"amount": "19.99"}),
        Decimal("19.99"),
    )
    test("money.to_internal scalar", mf.to_internal_value("9.50"), Decimal("9.50"))

    # form
    test("money.form_field_type", mf.form_field_type(), "number")
    attrs = mf.form_widget_attrs()
    test("money.form_widget_attrs step", attrs["step"], "0.01")
    test("money.form_widget_attrs min", attrs["min"], "0")

    # custom decimal_places
    mf3 = MoneyField(decimal_places=3)
    test("money.3dp to_db", mf3.to_db_value(Decimal("1.234")), 1234)
    test("money.3dp from_db", mf3.from_db_value(1234), Decimal("1.234"))

    # custom currency
    mf_eur = MoneyField(currency="EUR")
    rep_eur = mf_eur.to_representation(Decimal(10))
    test("money.eur currency", rep_eur["currency"], "EUR")


# ---------------------------------------------------------------------------
# ColorField
# ---------------------------------------------------------------------------


def test_color_field():
    cf = ColorField()
    test("color.db_type", cf.db_type(), "varchar(7)")
    test("color.form_field_type", cf.form_field_type(), "color")

    # validate
    test("color.validate valid", cf.validate("#ff5733"), "#ff5733")
    test("color.validate uppercase normalized", cf.validate("#FF5733"), "#ff5733")
    test("color.validate None", cf.validate(None), None)
    test_raises("color.validate short hex", lambda: cf.validate("#FFF"))
    test_raises("color.validate no hash", lambda: cf.validate("ff5733"))
    test_raises("color.validate word", lambda: cf.validate("red"))
    test_raises("color.validate 8 chars", lambda: cf.validate("#ff573300"))
    test_raises("color.validate invalid hex chars", lambda: cf.validate("#gggggg"))

    # to_db_value
    test("color.to_db lowercase", cf.to_db_value("#AABBCC"), "#aabbcc")
    test("color.to_db None", cf.to_db_value(None), None)

    # from_db_value
    test("color.from_db", cf.from_db_value("#aabbcc"), "#aabbcc")
    test("color.from_db None", cf.from_db_value(None), None)

    # to_representation
    test("color.to_repr", cf.to_representation("#ff5733"), "#ff5733")
    test("color.to_repr None", cf.to_representation(None), None)


# ---------------------------------------------------------------------------
# EmailField
# ---------------------------------------------------------------------------


def test_email_field():
    ef = EmailField()
    test("email.db_type", ef.db_type(), "varchar(254)")

    # validate
    test("email.validate valid", ef.validate("user@example.com"), "user@example.com")
    test("email.validate None", ef.validate(None), None)
    test_raises("email.validate no at", lambda: ef.validate("invalid"))
    test_raises("email.validate no domain", lambda: ef.validate("user@"))
    test_raises("email.validate no local", lambda: ef.validate("@example.com"))

    # max length
    ef_short = EmailField(max_length=20)
    test("email.short db_type", ef_short.db_type(), "varchar(20)")
    test_raises(
        "email.max length exceeded",
        lambda: ef_short.validate("verylongemail@example.com"),
    )

    # to_db_value domain normalization
    test(
        "email.to_db domain lower",
        ef.to_db_value("User@EXAMPLE.COM"),
        "User@example.com",
    )
    test("email.to_db None", ef.to_db_value(None), None)
    test(
        "email.to_db preserves local case",
        ef.to_db_value("MyUser@Host.COM"),
        "MyUser@host.com",
    )

    # from_db_value
    test("email.from_db", ef.from_db_value("user@example.com"), "user@example.com")
    test("email.from_db None", ef.from_db_value(None), None)

    # form
    test("email.form_field_type", ef.form_field_type(), "email")
    test("email.form_widget_attrs", ef.form_widget_attrs(), {"maxlength": "254"})


# ---------------------------------------------------------------------------
# URLField
# ---------------------------------------------------------------------------


def test_url_field():
    uf = URLField()
    test("url.db_type", uf.db_type(), "varchar(2048)")

    # validate
    test(
        "url.validate https", uf.validate("https://example.com"), "https://example.com"
    )
    test(
        "url.validate http",
        uf.validate("http://example.com/path"),
        "http://example.com/path",
    )
    test("url.validate None", uf.validate(None), None)
    test_raises("url.validate ftp rejected", lambda: uf.validate("ftp://example.com"))
    test_raises("url.validate no scheme", lambda: uf.validate("example.com"))
    test_raises("url.validate no host", lambda: uf.validate("https://"))

    # custom schemes
    uf_ftp = URLField(allowed_schemes=frozenset({"ftp", "sftp"}))
    test(
        "url.custom scheme ftp",
        uf_ftp.validate("ftp://files.example.com"),
        "ftp://files.example.com",
    )
    test_raises(
        "url.custom scheme rejects http", lambda: uf_ftp.validate("http://example.com")
    )

    # max length
    uf_short = URLField(max_length=30)
    test_raises(
        "url.max length exceeded",
        lambda: uf_short.validate("https://example.com/very/long/path/that/exceeds"),
    )

    # from_db_value
    test("url.from_db", uf.from_db_value("https://x.com"), "https://x.com")
    test("url.from_db None", uf.from_db_value(None), None)

    # form
    test("url.form_field_type", uf.form_field_type(), "url")
    test("url.form_widget_attrs", uf.form_widget_attrs(), {"maxlength": "2048"})


# ---------------------------------------------------------------------------
# SlugField
# ---------------------------------------------------------------------------


def test_slug_field():
    sf = SlugField()
    test("slug.db_type", sf.db_type(), "varchar(50)")

    # validate
    test("slug.validate valid", sf.validate("hello-world"), "hello-world")
    test("slug.validate single word", sf.validate("hello"), "hello")
    test("slug.validate None", sf.validate(None), None)
    test_raises("slug.validate uppercase", lambda: sf.validate("Hello-World"))
    test_raises("slug.validate spaces", lambda: sf.validate("hello world"))
    test_raises("slug.validate empty", lambda: sf.validate(""))
    test_raises("slug.validate special chars", lambda: sf.validate("hello_world"))
    test_raises("slug.validate leading hyphen", lambda: sf.validate("-hello"))

    # max length
    sf_short = SlugField(max_length=5)
    test_raises("slug.max length exceeded", lambda: sf_short.validate("toolong"))

    # to_db_value
    test("slug.to_db", sf.to_db_value("my-slug"), "my-slug")
    test("slug.to_db None", sf.to_db_value(None), None)

    # form
    test("slug.form_field_type", sf.form_field_type(), "text")
    attrs = sf.form_widget_attrs()
    test("slug.form_widget_attrs maxlength", attrs["maxlength"], "50")
    test("slug.form_widget_attrs has pattern", "pattern" in attrs, True)


def test_slugify():
    test("slugify.basic", slugify("Hello World"), "hello-world")
    test("slugify.special chars", slugify("Hello, World!"), "hello-world")
    test("slugify.multiple spaces", slugify("hello   world"), "hello-world")
    test("slugify.leading trailing", slugify("  Hello World  "), "hello-world")
    test("slugify.already slug", slugify("hello-world"), "hello-world")
    test("slugify.max_length", slugify("a" * 100, max_length=10), "a" * 10)
    test(
        "slugify.truncate no trailing hyphen",
        slugify("hello-world-foo", max_length=6),
        "hello",
    )
    test("slugify.numbers", slugify("item 123"), "item-123")


# ---------------------------------------------------------------------------
# PhoneField
# ---------------------------------------------------------------------------


def test_phone_field():
    pf = PhoneField()
    test("phone.db_type", pf.db_type(), "varchar(20)")

    # validate
    test("phone.validate e164", pf.validate("+14155551234"), "+14155551234")
    test("phone.validate None", pf.validate(None), None)
    test("phone.validate strips parens", pf.validate("+1(415)5551234"), "+14155551234")
    test("phone.validate strips dashes", pf.validate("+1-415-555-1234"), "+14155551234")
    test("phone.validate strips spaces", pf.validate("+1 415 555 1234"), "+14155551234")
    test("phone.validate strips dots", pf.validate("+1.415.555.1234"), "+14155551234")
    test_raises("phone.validate no plus", lambda: pf.validate("14155551234"))
    test_raises("phone.validate too short", lambda: pf.validate("+1"))
    test_raises("phone.validate letters", lambda: pf.validate("+1abc"))
    test_raises("phone.validate starts with 0", lambda: pf.validate("+0123456789"))

    # to_db_value
    test("phone.to_db strips", pf.to_db_value("+1 (415) 555-1234"), "+14155551234")
    test("phone.to_db None", pf.to_db_value(None), None)

    # form
    test("phone.form_field_type", pf.form_field_type(), "tel")


# ---------------------------------------------------------------------------
# IPAddressField
# ---------------------------------------------------------------------------


def test_ip_field():
    ip = IPAddressField()
    test("ip.db_type", ip.db_type(), "inet")

    # validate both
    test("ip.validate ipv4", ip.validate("192.168.1.1"), "192.168.1.1")
    test("ip.validate ipv6", ip.validate("::1"), "::1")
    test("ip.validate ipv6 full", ip.validate("2001:db8::1"), "2001:db8::1")
    test("ip.validate None", ip.validate(None), None)
    test_raises("ip.validate invalid", lambda: ip.validate("not-an-ip"))
    test_raises("ip.validate bad octet", lambda: ip.validate("999.999.999.999"))

    # protocol restriction
    ip4 = IPAddressField(protocol="ipv4")
    test("ip.ipv4 only valid", ip4.validate("10.0.0.1"), "10.0.0.1")
    test_raises("ip.ipv4 rejects ipv6", lambda: ip4.validate("::1"))

    ip6 = IPAddressField(protocol="ipv6")
    test("ip.ipv6 only valid", ip6.validate("::1"), "::1")
    test_raises("ip.ipv6 rejects ipv4", lambda: ip6.validate("192.168.1.1"))

    # invalid protocol
    test_raises("ip.invalid protocol", lambda: IPAddressField(protocol="ipv5"))

    # to_db_value
    test("ip.to_db", ip.to_db_value("10.0.0.1"), "10.0.0.1")
    test("ip.to_db None", ip.to_db_value(None), None)

    # form
    test("ip.form_field_type", ip.form_field_type(), "text")
    test(
        "ip.form_widget_attrs placeholder",
        ip.form_widget_attrs()["placeholder"],
        "192.168.1.1 or ::1",
    )


# ---------------------------------------------------------------------------
# CIDRField
# ---------------------------------------------------------------------------


def test_cidr_field():
    cf = CIDRField()
    test("cidr.db_type", cf.db_type(), "cidr")

    # validate
    test("cidr.validate ipv4 /24", cf.validate("192.168.1.0/24"), "192.168.1.0/24")
    test("cidr.validate ipv6", cf.validate("2001:db8::/32"), "2001:db8::/32")
    test("cidr.validate None", cf.validate(None), None)
    # strict=False means host bits OK
    test("cidr.validate host bits", cf.validate("192.168.1.5/24"), "192.168.1.0/24")
    test_raises("cidr.validate invalid", lambda: cf.validate("not-a-network"))

    # to_db_value
    test("cidr.to_db", cf.to_db_value("10.0.0.0/8"), "10.0.0.0/8")
    test("cidr.to_db None", cf.to_db_value(None), None)

    # from_db_value
    test("cidr.from_db", cf.from_db_value("10.0.0.0/8"), "10.0.0.0/8")
    test("cidr.from_db None", cf.from_db_value(None), None)

    # form
    test("cidr.form_field_type", cf.form_field_type(), "text")


# ---------------------------------------------------------------------------
# UUIDField
# ---------------------------------------------------------------------------


def test_uuid_field():
    uf = UUIDField()
    test("uuid.db_type", uf.db_type(), "uuid")

    valid_uuid = "550e8400-e29b-41d4-a716-446655440000"
    u = uf.validate(valid_uuid)
    test("uuid.validate string", u, uuid.UUID(valid_uuid))
    test(
        "uuid.validate uuid obj",
        uf.validate(uuid.UUID(valid_uuid)),
        uuid.UUID(valid_uuid),
    )
    test("uuid.validate None", uf.validate(None), None)
    test_raises("uuid.validate invalid", lambda: uf.validate("not-a-uuid"))

    # version constraint
    uf4 = UUIDField(version=4)
    u4 = uuid.uuid4()
    test("uuid.v4 valid", uf4.validate(u4), u4)
    # uuid1 is version 1
    u1 = uuid.uuid1()
    test_raises("uuid.v4 rejects v1", lambda: uf4.validate(u1))

    # to_db_value
    test("uuid.to_db string", uf.to_db_value(valid_uuid), valid_uuid)
    test("uuid.to_db uuid obj", uf.to_db_value(uuid.UUID(valid_uuid)), valid_uuid)
    test("uuid.to_db None", uf.to_db_value(None), None)

    # from_db_value
    test("uuid.from_db string", uf.from_db_value(valid_uuid), uuid.UUID(valid_uuid))
    test(
        "uuid.from_db uuid obj",
        uf.from_db_value(uuid.UUID(valid_uuid)),
        uuid.UUID(valid_uuid),
    )
    test("uuid.from_db None", uf.from_db_value(None), None)

    # to_representation
    test("uuid.to_repr", uf.to_representation(uuid.UUID(valid_uuid)), valid_uuid)
    test("uuid.to_repr None", uf.to_representation(None), None)

    # to_internal_value
    test("uuid.to_internal", uf.to_internal_value(valid_uuid), uuid.UUID(valid_uuid))

    # form
    test("uuid.form_field_type", uf.form_field_type(), "text")


# ---------------------------------------------------------------------------
# JSONField
# ---------------------------------------------------------------------------


def test_json_field():
    jf = JSONField()
    test("json.db_type", jf.db_type(), "jsonb")

    # validate
    test("json.validate dict", jf.validate({"key": "val"}), {"key": "val"})
    test("json.validate list", jf.validate([1, 2, 3]), [1, 2, 3])
    test("json.validate string parses", jf.validate('{"a":1}'), {"a": 1})
    test("json.validate None", jf.validate(None), None)
    test("json.validate int", jf.validate(42), 42)
    test_raises("json.validate bad json string", lambda: jf.validate("{invalid"))

    # to_db_value
    db = jf.to_db_value({"key": "val"})
    test("json.to_db dict", json.loads(db), {"key": "val"})
    test("json.to_db None", jf.to_db_value(None), None)
    test("json.to_db string passthrough", jf.to_db_value('{"a":1}'), '{"a":1}')

    # from_db_value
    test("json.from_db string", jf.from_db_value('{"a":1}'), {"a": 1})
    test("json.from_db dict passthrough", jf.from_db_value({"a": 1}), {"a": 1})
    test("json.from_db None", jf.from_db_value(None), None)

    # roundtrip
    original = {"users": [{"name": "Alice"}, {"name": "Bob"}]}
    db_val = jf.to_db_value(original)
    restored = jf.from_db_value(db_val)
    test("json.roundtrip dict", restored, original)

    original_list = [1, "two", 3.0, True, None]
    db_val2 = jf.to_db_value(original_list)
    restored2 = jf.from_db_value(db_val2)
    test("json.roundtrip list", restored2, original_list)

    # form
    test("json.form_field_type", jf.form_field_type(), "textarea")


# ---------------------------------------------------------------------------
# ChoiceField
# ---------------------------------------------------------------------------


def test_choice_field():
    cf = ChoiceField(choices=("red", "green", "blue"))
    test("choice.db_type", cf.db_type(), "varchar(5)")

    # validate
    test("choice.validate valid", cf.validate("red"), "red")
    test("choice.validate None", cf.validate(None), None)
    test_raises("choice.validate invalid", lambda: cf.validate("yellow"))
    test_raises("choice.validate case sensitive", lambda: cf.validate("Red"))

    # empty choices
    cf_empty = ChoiceField()
    test("choice.empty db_type", cf_empty.db_type(), "varchar(255)")
    test("choice.empty allows anything", cf_empty.validate("whatever"), "whatever")

    # single char choices
    cf_short = ChoiceField(choices=("a", "b"))
    test("choice.short db_type", cf_short.db_type(), "varchar(1)")

    # to_representation
    test("choice.to_repr", cf.to_representation("red"), "red")
    test("choice.to_repr None", cf.to_representation(None), None)

    # form
    test("choice.form_field_type", cf.form_field_type(), "select")
    test("choice.form_widget_attrs", cf.form_widget_attrs(), {})


# ---------------------------------------------------------------------------
# EncryptedField
# ---------------------------------------------------------------------------


def test_encrypted_field():
    ef = EncryptedField(_secret_key="test-secret-key-12345")
    test("encrypted.db_type", ef.db_type(), "text")

    # Check if cryptography is available for encrypt/decrypt tests
    try:
        from cryptography.fernet import Fernet as _Fernet  # noqa: F401

        _has_crypto = True
    except ImportError:
        _has_crypto = False

    if _has_crypto:
        # encrypt/decrypt roundtrip
        plaintext = "sensitive data"
        encrypted = ef.to_db_value(plaintext)
        test("encrypted.encrypted is string", isinstance(encrypted, str), True)
        test("encrypted.encrypted differs from plaintext", encrypted != plaintext, True)
        decrypted = ef.from_db_value(encrypted)
        test("encrypted.roundtrip", decrypted, plaintext)

        # None passthrough
        test("encrypted.to_db None", ef.to_db_value(None), None)
        test("encrypted.from_db None", ef.from_db_value(None), None)

        # Different values produce different ciphertext
        enc1 = ef.to_db_value("hello")
        enc2 = ef.to_db_value("world")
        test("encrypted.different values different ciphertext", enc1 != enc2, True)

        # Same value different ciphertext (Fernet uses random IV)
        enc3 = ef.to_db_value("hello")
        enc4 = ef.to_db_value("hello")
        test(
            "encrypted.same value different ciphertext (random IV)", enc3 != enc4, True
        )

        # Roundtrip empty string
        enc_empty = ef.to_db_value("")
        test("encrypted.roundtrip empty", ef.from_db_value(enc_empty), "")

        # Roundtrip unicode
        enc_uni = ef.to_db_value("hello \u2603 world")
        test(
            "encrypted.roundtrip unicode",
            ef.from_db_value(enc_uni),
            "hello \u2603 world",
        )
    else:
        print("  SKIP: encrypted encrypt/decrypt tests (cryptography not installed)")

    # validate (no crypto needed)
    test("encrypted.validate string", ef.validate("ok"), "ok")
    test("encrypted.validate None", ef.validate(None), None)
    test_raises("encrypted.validate non-string", lambda: ef.validate(123))

    # to_representation masked
    test("encrypted.to_repr masked", ef.to_representation("anything"), "****")
    test("encrypted.to_repr None", ef.to_representation(None), None)

    # form
    test("encrypted.form_field_type", ef.form_field_type(), "password")
    test(
        "encrypted.form_widget_attrs autocomplete",
        ef.form_widget_attrs()["autocomplete"],
        "off",
    )


# ---------------------------------------------------------------------------
# DurationField
# ---------------------------------------------------------------------------


def test_duration_field():
    df = DurationField()
    test("duration.db_type", df.db_type(), "interval")

    # validate timedelta
    td = timedelta(hours=1, minutes=30)
    test("duration.validate timedelta", df.validate(td), td)
    test("duration.validate None", df.validate(None), None)

    # validate seconds number
    td_from_int = df.validate(3600)
    test("duration.validate int seconds", td_from_int, timedelta(seconds=3600))
    td_from_float = df.validate(90.5)
    test("duration.validate float seconds", td_from_float, timedelta(seconds=90.5))

    # validate string
    td_str = df.validate("1 day 02:30:00")
    test("duration.validate string", td_str, timedelta(days=1, hours=2, minutes=30))
    td_str2 = df.validate("00:05:00")
    test("duration.validate hms string", td_str2, timedelta(minutes=5))

    test_raises(
        "duration.validate invalid string", lambda: df.validate("not a duration")
    )

    # to_db_value
    db = df.to_db_value(timedelta(hours=2, minutes=15, seconds=30))
    test("duration.to_db is string", isinstance(db, str), True)
    test("duration.to_db None", df.to_db_value(None), None)

    # from_db_value
    test(
        "duration.from_db timedelta passthrough",
        df.from_db_value(timedelta(hours=1)),
        timedelta(hours=1),
    )
    test(
        "duration.from_db string",
        df.from_db_value("01:30:00"),
        timedelta(hours=1, minutes=30),
    )
    test("duration.from_db None", df.from_db_value(None), None)

    # to_representation
    test("duration.to_repr", df.to_representation(timedelta(hours=1)), 3600.0)
    test("duration.to_repr None", df.to_representation(None), None)

    # to_internal_value
    test("duration.to_internal int", df.to_internal_value(60), timedelta(seconds=60))
    test(
        "duration.to_internal float", df.to_internal_value(1.5), timedelta(seconds=1.5)
    )
    test("duration.to_internal None", df.to_internal_value(None), None)

    # form
    test("duration.form_field_type", df.form_field_type(), "text")


# ---------------------------------------------------------------------------
# PercentField
# ---------------------------------------------------------------------------


def test_percent_field():
    pf = PercentField()
    test("percent.db_type default", pf.db_type(), "numeric(5,2)")

    # validate 0-100 mode
    test("percent.validate 50", pf.validate(50), Decimal(50))
    test("percent.validate 0", pf.validate(0), Decimal(0))
    test("percent.validate 100", pf.validate(100), Decimal(100))
    test("percent.validate 99.5", pf.validate("99.5"), Decimal("99.5"))
    test("percent.validate None", pf.validate(None), None)
    test_raises("percent.validate negative", lambda: pf.validate(-1))
    test_raises("percent.validate over 100", lambda: pf.validate(101))
    test_raises("percent.validate non-numeric", lambda: pf.validate("abc"))

    # fraction mode
    pf_frac = PercentField(store_as_fraction=True)
    test("percent.frac db_type", pf_frac.db_type(), "numeric(5,4)")
    test("percent.frac validate 0.75", pf_frac.validate("0.75"), Decimal("0.75"))
    test("percent.frac validate 0", pf_frac.validate(0), Decimal(0))
    test("percent.frac validate 1", pf_frac.validate(1), Decimal(1))
    test_raises("percent.frac validate 1.1", lambda: pf_frac.validate("1.1"))
    test_raises("percent.frac validate negative", lambda: pf_frac.validate("-0.1"))

    # to_db_value
    test("percent.to_db", pf.to_db_value(Decimal(75)), "75")
    test("percent.to_db None", pf.to_db_value(None), None)

    # from_db_value
    test("percent.from_db", pf.from_db_value("75.50"), Decimal("75.50"))
    test("percent.from_db None", pf.from_db_value(None), None)

    # to_representation
    test("percent.to_repr", pf.to_representation(Decimal("75.5")), 75.5)
    test("percent.to_repr None", pf.to_representation(None), None)

    # to_internal_value
    test("percent.to_internal", pf.to_internal_value("50"), Decimal(50))
    test("percent.to_internal None", pf.to_internal_value(None), None)

    # form
    test("percent.form_field_type", pf.form_field_type(), "number")
    attrs = pf.form_widget_attrs()
    test("percent.form_widget_attrs min", attrs["min"], "0")
    test("percent.form_widget_attrs max", attrs["max"], "100")

    attrs_frac = pf_frac.form_widget_attrs()
    test("percent.frac form_widget_attrs max", attrs_frac["max"], "1")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_custom_field_base()
    test_registry()
    test_create_field()
    test_money_field()
    test_color_field()
    test_email_field()
    test_url_field()
    test_slug_field()
    test_slugify()
    test_phone_field()
    test_ip_field()
    test_cidr_field()
    test_uuid_field()
    test_json_field()
    test_choice_field()
    test_encrypted_field()
    test_duration_field()
    test_percent_field()

    print(f"\n{'=' * 60}")
    print(f"fields: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
