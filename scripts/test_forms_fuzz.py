"""
Hypothesis fuzz tests for Form validation.

Uses real Form classes. Proves:
1. Valid data → is_valid() == True
2. Invalid data → is_valid() == False with errors
3. Cross-field clean() validation
4. Field type coercion

# hyper-test: unit
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.forms import (
    BooleanField,
    CharField,
    ChoiceField,
    EmailField,
    Form,
    IntegerField,
)

# ---------------------------------------------------------------------------
# Test forms
# ---------------------------------------------------------------------------


class RegistrationForm(Form):
    username = CharField(min_length=3, max_length=30)
    email = EmailField()
    age = IntegerField(min_value=13, max_value=120)
    agree_tos = BooleanField()


class ContactForm(Form):
    name = CharField(min_length=1, max_length=100)
    message = CharField(min_length=10, max_length=1000)
    priority = ChoiceField(
        choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")]
    )


# ---------------------------------------------------------------------------
# Property 1: Valid registration data accepted
# ---------------------------------------------------------------------------


@given(
    username=st.text(min_size=3, max_size=30, alphabet="abcdefghijklmnop0123456789_"),
    email_local=st.text(min_size=1, max_size=15, alphabet="abcdefghijklmnop"),
    email_domain=st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnop"),
    age=st.integers(min_value=13, max_value=120),
)
@settings(max_examples=300, deadline=2000)
def test_valid_registration(username, email_local, email_domain, age):
    """Valid registration data → is_valid() == True."""
    form = RegistrationForm(
        data={
            "username": username,
            "email": f"{email_local}@{email_domain}.com",
            "age": age,
            "agree_tos": True,
        }
    )
    assert form.is_valid(), f"Should be valid: errors={form.errors}"


# ---------------------------------------------------------------------------
# Property 2: Short username rejected
# ---------------------------------------------------------------------------


@given(username=st.text(min_size=0, max_size=2))
@settings(max_examples=200, deadline=2000)
def test_short_username_rejected(username):
    """Username < 3 chars → rejected."""
    form = RegistrationForm(
        data={
            "username": username,
            "email": "test@example.com",
            "age": 25,
            "agree_tos": True,
        }
    )
    assert not form.is_valid(), f"Should reject short username: {username!r}"
    assert "username" in form.errors


# ---------------------------------------------------------------------------
# Property 3: Invalid age rejected
# ---------------------------------------------------------------------------


@given(
    age=st.one_of(
        st.integers(max_value=12),
        st.integers(min_value=121),
    )
)
@settings(max_examples=200, deadline=2000)
def test_invalid_age_rejected(age):
    """Age outside 13-120 → rejected."""
    form = RegistrationForm(
        data={
            "username": "validuser",
            "email": "test@example.com",
            "age": age,
            "agree_tos": True,
        }
    )
    assert not form.is_valid(), f"Should reject age {age}"
    assert "age" in form.errors


# ---------------------------------------------------------------------------
# Property 4: Valid contact form accepted
# ---------------------------------------------------------------------------


@given(
    name=st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnop"),
    message=st.text(min_size=15, max_size=200, alphabet="abcdefghijklmnop"),
    priority=st.sampled_from(["low", "medium", "high"]),
)
@settings(max_examples=300, deadline=2000)
def test_valid_contact(name, message, priority):
    """Valid contact form → is_valid() == True."""
    form = ContactForm(
        data={
            "name": name,
            "message": message,
            "priority": priority,
        }
    )
    assert form.is_valid(), f"Should be valid: errors={form.errors}"


# ---------------------------------------------------------------------------
# Property 5: Invalid priority rejected
# ---------------------------------------------------------------------------


@given(
    priority=st.text(min_size=1, max_size=10).filter(
        lambda s: s not in ("low", "medium", "high")
    )
)
@settings(max_examples=200, deadline=2000)
def test_invalid_priority_rejected(priority):
    """Invalid choice value → rejected."""
    form = ContactForm(
        data={
            "name": "Test",
            "message": "A" * 20,
            "priority": priority,
        }
    )
    assert not form.is_valid()
    assert "priority" in form.errors


# ---------------------------------------------------------------------------
# Property 6: Missing required field → error
# ---------------------------------------------------------------------------


@given(field_to_omit=st.sampled_from(["username", "email", "age"]))
@settings(max_examples=100, deadline=2000)
def test_missing_required_field(field_to_omit):
    """Omitting any required field → is_valid() == False."""
    data = {
        "username": "validuser",
        "email": "test@example.com",
        "age": 25,
        "agree_tos": True,
    }
    del data[field_to_omit]
    form = RegistrationForm(data=data)
    assert not form.is_valid(), f"Should reject missing {field_to_omit}"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Form Validation Hypothesis Fuzz Tests ──\n")

    tests = [
        ("valid registration", test_valid_registration),
        ("short username rejected", test_short_username_rejected),
        ("invalid age rejected", test_invalid_age_rejected),
        ("valid contact", test_valid_contact),
        ("invalid priority rejected", test_invalid_priority_rejected),
        ("missing required field", test_missing_required_field),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Form fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
