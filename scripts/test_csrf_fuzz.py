"""
Hypothesis fuzz tests for CSRF token forgery prevention.

Proves:
1. Valid token roundtrip: generate → validate == True
2. ANY mutation of token → validate == False
3. Truncated token → rejected
4. Empty token → rejected
5. Wrong secret → rejected

Uses real CSRFMiddleware.

# hyper-test: unit
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hyperdjango.standalone_middleware import CSRFMiddleware

_csrf = CSRFMiddleware(secret="test-csrf-secret-32chars-minimum!!")


# ---------------------------------------------------------------------------
# Property 1: Generate → validate roundtrip
# ---------------------------------------------------------------------------


@given(dummy=st.integers(min_value=0, max_value=1000))
@settings(max_examples=200, deadline=1000)
def test_token_roundtrip(dummy):
    """Generated CSRF token validates successfully."""
    token = _csrf._generate_token()
    assert _csrf._validate_token(token), f"Valid token rejected: {token!r}"


# ---------------------------------------------------------------------------
# Property 2: ANY mutation → rejected
# ---------------------------------------------------------------------------


@given(flip_pos=st.integers(min_value=0, max_value=200))
@settings(max_examples=500, deadline=1000)
def test_token_mutation_rejected(flip_pos):
    """ANY single character change in CSRF token → rejected."""
    token = _csrf._generate_token()
    assume(flip_pos < len(token))

    chars = list(token)
    original = chars[flip_pos]
    replacement = chr((ord(original) + 1) % 128)
    if replacement == original:
        replacement = chr((ord(original) + 2) % 128)
    assume(replacement != original)
    chars[flip_pos] = replacement
    tampered = "".join(chars)

    assert not _csrf._validate_token(tampered), (
        f"Tampered token accepted! pos={flip_pos}, char {original!r}→{replacement!r}"
    )


# ---------------------------------------------------------------------------
# Property 3: Truncated → rejected
# ---------------------------------------------------------------------------


@given(keep=st.integers(min_value=0, max_value=50))
@settings(max_examples=200, deadline=1000)
def test_token_truncated_rejected(keep):
    """Truncated CSRF token → rejected."""
    token = _csrf._generate_token()
    assume(keep < len(token))
    truncated = token[:keep]
    assert not _csrf._validate_token(truncated)


# ---------------------------------------------------------------------------
# Property 4: Empty and garbage → rejected
# ---------------------------------------------------------------------------


def test_empty_token_rejected():
    """Empty string token → rejected."""
    assert not _csrf._validate_token("")
    assert not _csrf._validate_token(None)
    print("  PASS: empty token rejected")


@given(garbage=st.text(max_size=100))
@settings(max_examples=300, deadline=1000)
def test_garbage_token_rejected(garbage):
    """Random garbage string → rejected."""
    assert not _csrf._validate_token(garbage)


# ---------------------------------------------------------------------------
# Property 5: Wrong secret → rejected
# ---------------------------------------------------------------------------


@given(dummy=st.integers(min_value=0, max_value=100))
@settings(max_examples=100, deadline=1000)
def test_wrong_secret_rejected(dummy):
    """Token from different secret → rejected."""
    other_csrf = CSRFMiddleware(secret="different-secret-32chars-minimum!!")
    token = other_csrf._generate_token()
    assert not _csrf._validate_token(token), "Token from wrong secret accepted!"


# ---------------------------------------------------------------------------
# Property 6: Constant-time comparison (uses hmac.compare_digest)
# ---------------------------------------------------------------------------


def test_regression_non_ascii_token():
    """Regression: CSRF token with non-ASCII chars must be rejected, not crash."""
    # This exact input crashed before the fix — hypothesis found it
    assert not _csrf._validate_token("data.\xff")
    assert not _csrf._validate_token("data.\u0100")
    assert not _csrf._validate_token("data.café")
    assert not _csrf._validate_token("\x80.\x80")
    print("  PASS: regression non-ASCII token")


def test_uses_constant_time_comparison():
    """_validate_token uses constant-time comparison.

    The CSRF middleware routes through the unified HMAC helper in
    `hyperdjango.native._crypto` (`hmac_sha256_verify_truncated`) which
    internally uses `hmac.compare_digest`. This test verifies both the
    caller delegates to the helper AND the helper uses compare_digest.
    """
    import inspect

    from hyperdjango.native._crypto import hmac_sha256_verify_truncated

    caller_source = inspect.getsource(_csrf._validate_token)
    helper_source = inspect.getsource(hmac_sha256_verify_truncated)

    assert "hmac_sha256_verify" in caller_source or "compare_digest" in caller_source, (
        "_validate_token must delegate to hmac_sha256_verify_* or use compare_digest directly"
    )
    assert "compare_digest" in helper_source, (
        "hmac_sha256_verify_truncated must use hmac.compare_digest for timing safety"
    )

    # Runtime guarantee: prove the comparison path actually runs and rejects a
    # forged signature — a token with the correct data prefix but a wrong
    # signature of identical length. A dead/short-circuited comparison would
    # accept this; the source string alone can't catch that.
    valid = _csrf._generate_token()
    assert _csrf._validate_token(valid), "freshly generated token must validate"
    data, _, sig = valid.rpartition(".")
    assert sig and data, f"unexpected token format: {valid!r}"
    # Same-length forged signature (flip every hex char deterministically).
    forged_sig = "".join("b" if c != "b" else "c" for c in sig)
    forged = f"{data}.{forged_sig}"
    assert len(forged) == len(valid) and forged != valid
    assert not _csrf._validate_token(forged), (
        "forged-signature token accepted — HMAC comparison did not run"
    )
    print("  PASS: uses constant-time comparison (via hmac_sha256_verify_truncated)")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── CSRF Token Hypothesis Fuzz Tests ──\n")

    tests = [
        ("token roundtrip", test_token_roundtrip),
        ("mutation rejected", test_token_mutation_rejected),
        ("truncated rejected", test_token_truncated_rejected),
        ("empty rejected", test_empty_token_rejected),
        ("garbage rejected", test_garbage_token_rejected),
        ("wrong secret rejected", test_wrong_secret_rejected),
        ("regression non-ASCII token", test_regression_non_ascii_token),
        ("constant-time comparison", test_uses_constant_time_comparison),
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
    print(f"CSRF fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()


def test_hardcoded_placeholder_secret_never_signs_tokens():
    """#134 (open-source threat model): CSRFMiddleware used to default `secret` to
    the literal 'csrf-secret-change-me' baked into public source — an attacker who
    read the repo could forge valid CSRF tokens and defeat the protection. Weak
    placeholder secrets must never sign real tokens: they resolve to a configured
    secret, so a token forged with the public placeholder is rejected."""
    import hashlib
    import hmac as _hmac

    for placeholder in (
        "csrf-secret-change-me",
        "change-me",
        "changeme",
        "secret",
        "default",
        "",
    ):
        mw = CSRFMiddleware(secret=placeholder)
        assert mw.secret.strip().lower() not in mw._FORBIDDEN_SECRETS, (
            f"weak secret {placeholder!r} used verbatim for CSRF signing"
        )
        tok = "attacker-chosen-token"
        forged_sig = _hmac.new(
            b"csrf-secret-change-me", tok.encode(), hashlib.sha256
        ).hexdigest()[:16]
        assert not mw._validate_token(f"{tok}.{forged_sig}"), (
            "token forged with the public placeholder secret was ACCEPTED"
        )
    print("  PASS: hardcoded placeholder secret never signs tokens")
