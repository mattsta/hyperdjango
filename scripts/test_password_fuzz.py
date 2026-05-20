"""
Hypothesis fuzz tests for argon2id password hashing.

Proves correctness properties:
1. verify(hash(pw), pw) == True for ANY password
2. verify(hash(pw), wrong_pw) == False for ANY different password
3. Different passwords produce different hashes

# hyper-test: unit
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hyperdjango.auth.passwords import hash_password, verify_password

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

passwords = st.text(min_size=1, max_size=72)
short_passwords = st.text(min_size=1, max_size=20)


# ---------------------------------------------------------------------------
# Property 1: verify(hash(pw), pw) == True
# ---------------------------------------------------------------------------


@given(pw=short_passwords)
@settings(max_examples=50, deadline=10000)  # argon2 is slow (~100ms per hash)
def test_hash_verify_roundtrip(pw):
    """verify(hash(pw), pw) == True for ANY password."""
    hashed = hash_password(pw)
    assert verify_password(pw, hashed), f"Verify failed for password: {pw!r}"


# ---------------------------------------------------------------------------
# Property 2: verify(hash(pw), wrong_pw) == False
# ---------------------------------------------------------------------------


@given(pw=short_passwords, wrong=short_passwords)
@settings(max_examples=30, deadline=10000)
def test_wrong_password_rejected(pw, wrong):
    """verify(hash(pw), wrong_pw) == False for ANY different password."""
    assume(pw != wrong)
    hashed = hash_password(pw)
    assert not verify_password(wrong, hashed), (
        f"Wrong password accepted: pw={pw!r} wrong={wrong!r}"
    )


# ---------------------------------------------------------------------------
# Property 3: Different passwords → different hashes
# ---------------------------------------------------------------------------


@given(pw1=short_passwords, pw2=short_passwords)
@settings(max_examples=30, deadline=10000)
def test_different_passwords_different_hashes(pw1, pw2):
    """Different passwords produce different hashes (argon2 has random salt)."""
    h1 = hash_password(pw1)
    h2 = hash_password(pw2)
    # Even same password should produce different hashes due to random salt
    assert h1 != h2, f"Hash collision: {pw1!r} and {pw2!r}"


# ---------------------------------------------------------------------------
# Property 4: Same password → different hashes (salt uniqueness)
# ---------------------------------------------------------------------------


@given(pw=short_passwords)
@settings(max_examples=20, deadline=10000)
def test_same_password_different_hashes(pw):
    """Same password hashed twice produces different hashes (random salt)."""
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    assert h1 != h2, f"Same hash for same password (salt not random): {pw!r}"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── Password Hashing Hypothesis Fuzz Tests ──\n")

    tests = [
        ("hash/verify roundtrip", test_hash_verify_roundtrip),
        ("wrong password rejected", test_wrong_password_rejected),
        (
            "different passwords → different hashes",
            test_different_passwords_different_hashes,
        ),
        (
            "same password → different hashes (salt)",
            test_same_password_different_hashes,
        ),
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
    print(f"Password fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
