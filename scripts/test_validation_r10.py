"""Round-10 validation/serialization regression tests.

Covers three fixes:

1. Email: an empty first domain label (``user@.com`` / ``verylonglocalpart@.com``)
   must be rejected on BOTH the single-email native path and the SIMD batch path.
   Previously the SIMD chunk path accepted a ``.`` immediately after ``@`` while
   the scalar remainder rejected it, so the verdict depended on 16-byte alignment.

2. SlugRelatedField must emit its slug (not the raw related object) on read.

3. A serializer int field must reject a bool (bool is an int subclass, but the
   native dhi path rejects bool-for-int — the standalone serializer now matches).

Run directly:  uv run python scripts/test_validation_r10.py
"""

# hyper-test: unit

from __future__ import annotations

from hyperdjango._hyperdjango_native import validate_email as native_validate_email

from hyperdjango.rest import PrimaryKeyRelatedField, SlugRelatedField
from hyperdjango.serializers import Serializer, SerializerFieldInfo
from hyperdjango.validation.core.batch import validate_emails_batch

# --- Fixtures ----------------------------------------------------------------

# Empty first domain label: '.' immediately follows '@'. `verylonglocalpart@.com`
# is the alignment trigger — with a long local part the '@' lands inside a
# completed 16-byte SIMD chunk, the case the old chunk path mishandled.
REJECT_EMAILS = [
    "user@.com",
    "verylonglocalpart@.com",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@.com",  # 30-char local: @ and . in same chunk
    "aaaaaaaaaaaaaaa@." + "c" * 16,  # @ ends chunk 0, . starts chunk 1
]

ACCEPT_EMAILS = [
    "user@example.com",
    "rach@example.com",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example.com",  # long local, real domain char
]


def test_native_single_email_rejects_empty_domain_label() -> None:
    for email in REJECT_EMAILS:
        assert native_validate_email(email) is False, (
            f"native validate_email accepted {email!r}"
        )
    for email in ACCEPT_EMAILS:
        assert native_validate_email(email) is True, (
            f"native validate_email rejected valid {email!r}"
        )


def test_batch_email_rejects_empty_domain_label() -> None:
    # Mix rejects and accepts so both are exercised in one SIMD batch call.
    batch = REJECT_EMAILS + ACCEPT_EMAILS
    result = validate_emails_batch(batch)
    assert len(result.results) == len(batch)
    for i, email in enumerate(REJECT_EMAILS):
        assert result.results[i] is False, f"batch accepted {email!r}"
    for j, email in enumerate(ACCEPT_EMAILS):
        assert result.results[len(REJECT_EMAILS) + j] is True, (
            f"batch rejected valid {email!r}"
        )


# --- SlugRelatedField read representation -------------------------------------


class _Author:
    def __init__(self, username: str) -> None:
        self.username = username


class _Post:
    def __init__(self, title: str, author: _Author, editor_id: int) -> None:
        self.title = title
        self.author = author
        self.editor_id = editor_id


class _PostSerializer(Serializer):
    title: str
    author: str = SlugRelatedField(slug_field="username")
    editor_id: int = PrimaryKeyRelatedField()


def test_slug_related_field_serializes_slug_on_read() -> None:
    post = _Post("Hello", _Author("alice"), editor_id=42)
    data = _PostSerializer(post).data
    assert data["author"] == "alice", (
        f"SlugRelatedField emitted {data['author']!r}, expected slug 'alice'"
    )
    # PrimaryKeyRelatedField must still pass through the raw PK unchanged.
    assert data["editor_id"] == 42, (
        f"PrimaryKeyRelatedField emitted {data['editor_id']!r}, expected raw PK 42"
    )
    assert data["title"] == "Hello"


def test_slug_related_field_many_and_none() -> None:
    class _ManyPost:
        def __init__(self, tags):
            self.title = "t"
            self.author = None
            self.editor_id = 1
            self.tags = tags

    class _S(Serializer):
        tags: str = SlugRelatedField(slug_field="name", many=True)

    class _Tag:
        def __init__(self, name):
            self.name = name

    data = _S(_ManyPost([_Tag("x"), _Tag("y")])).data
    assert data["tags"] == ["x", "y"], data["tags"]


# --- bool rejected for int field ---------------------------------------------


class _CountSerializer(Serializer):
    count: int = SerializerFieldInfo(field_name="count", field_type=int)


def test_bool_rejected_for_int_field() -> None:
    s = _CountSerializer(input_data={"count": True})
    assert s.is_valid() is False, "serializer accepted bool for an int field"
    assert "count" in s.errors, s.errors

    # A genuine int still validates.
    ok = _CountSerializer(input_data={"count": 5})
    assert ok.is_valid() is True, ok.errors
    assert ok.validated_data["count"] == 5

    # A numeric string still coerces.
    coerced = _CountSerializer(input_data={"count": "7"})
    assert coerced.is_valid() is True, coerced.errors
    assert coerced.validated_data["count"] == 7


# --- Runner -------------------------------------------------------------------


def main() -> int:
    tests = [
        test_native_single_email_rejects_empty_domain_label,
        test_batch_email_rejects_empty_domain_label,
        test_slug_related_field_serializes_slug_on_read,
        test_slug_related_field_many_and_none,
        test_bool_rejected_for_int_field,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors in the report
            failures += 1
            print(f"ERROR {t.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {t.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
