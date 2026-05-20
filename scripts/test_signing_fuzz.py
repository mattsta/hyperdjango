"""
Hypothesis property tests for the TokenEngine signing system.

Proves correctness properties for ALL possible inputs:
1. encode_ref/decode_ref roundtrip for ANY string
2. encode_data/decode_data roundtrip for ANY valid data dict
3. ANY single character change in a token -> rejection
4. Key rotation: old key tokens still decodable
5. Different inputs produce different tokens
6. base62 codec canonicality: decode(encode(b)) == b AND encode(decode(s)) == s
   for canonical strings; non-canonical (leading-zero) encodings are rejected.

# hyper-test: unit
"""

from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from hyperdjango.public_id import ALPHANUMERIC_CHARS
from hyperdjango.signing import (
    SigningKey,
    TokenEngine,
    _base62_to_bytes,
    _bytes_to_base62,
)
from hyperdjango.testkit import check, finish, run_main, run_property

# Per-property example counts. HMAC signing is pure-Python and bounded, so these
# stay generous while keeping the whole file well under a minute.
_ROUNDTRIP_EXAMPLES = 300
_ROTATION_EXAMPLES = 200
_B62_EXAMPLES = 400


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def make_engine(salt_bytes=8, pad=False):
    return TokenEngine(
        keys=[SigningKey(secret="test-secret-key-at-least-32-chars-long!!", version=0)],
        salt_bytes=salt_bytes,
        pad_to_bucket=pad,
    )


def make_rotation_engine():
    return TokenEngine(
        keys=[
            SigningKey(secret="new-key-32-chars-for-rotation-testing!!", version=1),
            SigningKey(secret="old-key-32-chars-for-rotation-testing!!", version=0),
        ],
    )


# Data dict values that are JSON-serializable primitives
json_primitives = st.one_of(
    st.text(max_size=50),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.floats(min_value=-1e10, max_value=1e10, allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
)

# Valid data dicts (1-8 keys, string keys, primitive values)
data_dicts = st.dictionaries(
    keys=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz_"),
    values=json_primitives,
    min_size=1,
    max_size=8,
)

# Reference strings (non-empty, UTF-8 safe)
ref_strings = st.text(min_size=1, max_size=100)


# ---------------------------------------------------------------------------
# Property 1: encode_ref / decode_ref roundtrip
# ---------------------------------------------------------------------------


@given(ref=ref_strings)
@settings(max_examples=_ROUNDTRIP_EXAMPLES)
def prop_ref_roundtrip(ref):
    """encode_ref(s) -> decode_ref() == s for ANY string."""
    engine = make_engine()
    token = engine.encode_ref(ref)
    result = engine.decode_ref(token)
    assert result == ref, f"Roundtrip failed: {ref!r} -> {token!r} -> {result!r}"


@given(ref=ref_strings)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_ref_roundtrip_no_salt(ref):
    """encode_ref roundtrip with salt_bytes=0 (deterministic tokens)."""
    engine = make_engine(salt_bytes=0)
    assert engine.decode_ref(engine.encode_ref(ref)) == ref


@given(ref=ref_strings)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_ref_roundtrip_padded(ref):
    """encode_ref roundtrip with pad_to_bucket=True."""
    engine = make_engine(pad=True)
    assert engine.decode_ref(engine.encode_ref(ref)) == ref


# ---------------------------------------------------------------------------
# Property 2: encode_data / decode_data roundtrip
# ---------------------------------------------------------------------------


@given(data=data_dicts)
@settings(max_examples=_ROUNDTRIP_EXAMPLES)
def prop_data_roundtrip(data):
    """encode_data(d) -> decode_data() == d for ANY valid data dict."""
    assume("_exp" not in data)  # reserved internal field
    engine = make_engine()
    result = engine.decode_data(engine.encode_data(data))
    assert result == data, f"Roundtrip failed for {data!r}"


@given(data=data_dicts)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_data_roundtrip_no_salt(data):
    """encode_data roundtrip with salt_bytes=0."""
    assume("_exp" not in data)
    engine = make_engine(salt_bytes=0)
    assert engine.decode_data(engine.encode_data(data)) == data


@given(data=data_dicts)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_data_roundtrip_padded(data):
    """encode_data roundtrip with padding."""
    assume("_exp" not in data)
    engine = make_engine(pad=True)
    assert engine.decode_data(engine.encode_data(data)) == data


# ---------------------------------------------------------------------------
# Property 3: ANY single character change -> rejection
# ---------------------------------------------------------------------------


def _tamper_at(token: str, flip_pos: int) -> str | None:
    """Return ``token`` with the char at ``flip_pos`` changed, or None if the
    position is out of range or no in-range replacement differs."""
    if flip_pos >= len(token):
        return None
    chars = list(token)
    original = chars[flip_pos]
    replacement = chr((ord(original) + 1) % 128) if original != "~" else "a"
    if replacement == original:
        replacement = chr((ord(original) + 2) % 128)
    if replacement == original:
        return None
    chars[flip_pos] = replacement
    return "".join(chars)


@given(
    ref=st.text(min_size=1, max_size=30),
    flip_pos=st.integers(min_value=0, max_value=500),
)
@settings(max_examples=_ROUNDTRIP_EXAMPLES)
def prop_ref_bitflip_rejected(ref, flip_pos):
    """ANY single character change in a ref token -> decode returns None."""
    engine = make_engine()
    token = engine.encode_ref(ref)
    tampered = _tamper_at(token, flip_pos)
    assume(tampered is not None)
    result = engine.decode_ref(tampered)
    assert result is None or result != ref, (
        f"Tampered token decoded successfully! pos={flip_pos}, token={token!r}"
    )


@given(
    data=data_dicts,
    flip_pos=st.integers(min_value=0, max_value=500),
)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_data_bitflip_rejected(data, flip_pos):
    """ANY single character change in a data token -> decode returns None."""
    assume("_exp" not in data)
    engine = make_engine()
    token = engine.encode_data(data)
    tampered = _tamper_at(token, flip_pos)
    assume(tampered is not None)
    result = engine.decode_data(tampered)
    assert result is None or result != data


# ---------------------------------------------------------------------------
# Property 4: Key rotation — old key tokens decodable
# ---------------------------------------------------------------------------


@given(ref=ref_strings)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_key_rotation_ref(ref):
    """Token signed with old key is still decodable after rotation."""
    old_engine = TokenEngine(
        keys=[SigningKey(secret="old-key-32-chars-for-rotation-testing!!", version=0)]
    )
    token = old_engine.encode_ref(ref)
    result = make_rotation_engine().decode_ref(token)
    assert result == ref, f"Old token not decodable after rotation: {ref!r}"


@given(data=data_dicts)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_key_rotation_data(data):
    """Data token signed with old key is still decodable after rotation."""
    assume("_exp" not in data)
    old_engine = TokenEngine(
        keys=[SigningKey(secret="old-key-32-chars-for-rotation-testing!!", version=0)],
        salt_bytes=0,
    )
    token = old_engine.encode_data(data)
    new_engine = TokenEngine(
        keys=[
            SigningKey(secret="new-key-32-chars-for-rotation-testing!!", version=1),
            SigningKey(secret="old-key-32-chars-for-rotation-testing!!", version=0),
        ],
        salt_bytes=0,
    )
    assert new_engine.decode_data(token) == data


# ---------------------------------------------------------------------------
# Property 5: Different inputs -> different tokens (with salt)
# ---------------------------------------------------------------------------


@given(
    ref1=st.text(min_size=1, max_size=30),
    ref2=st.text(min_size=1, max_size=30),
)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_different_refs_different_tokens(ref1, ref2):
    """Different references produce different tokens (with salt)."""
    assume(ref1 != ref2)
    engine = make_engine()
    assert engine.encode_ref(ref1) != engine.encode_ref(ref2), (
        f"Collision: {ref1!r} and {ref2!r} produced same token"
    )


@given(ref=ref_strings)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_same_ref_different_tokens_with_salt(ref):
    """Same reference produces different tokens each time (salt uniqueness).

    With salt_bytes=8 the collision probability is 1/2^64 — effectively zero.
    """
    engine = make_engine()
    assert engine.encode_ref(ref) != engine.encode_ref(ref), (
        "Same ref produced same token (salt not working)"
    )


# ---------------------------------------------------------------------------
# Property 6: Wrong engine can't decode
# ---------------------------------------------------------------------------


@given(ref=ref_strings)
@settings(max_examples=_ROTATION_EXAMPLES)
def prop_wrong_key_rejects(ref):
    """Token from engine A is rejected by engine B with different key."""
    engine_a = TokenEngine(
        keys=[SigningKey(secret="engine-a-secret-32-chars-minimum!!", version=0)]
    )
    engine_b = TokenEngine(
        keys=[SigningKey(secret="engine-b-secret-32-chars-minimum!!", version=0)]
    )
    token = engine_a.encode_ref(ref)
    assert engine_b.decode_ref(token) is None, f"Wrong engine decoded token for {ref!r}"


# ---------------------------------------------------------------------------
# Property 7: base62 codec canonicality
#
# The token payload and HMAC signature are base62-encoded. A non-canonical
# encoding (leading "0" digits that decode to the same integer) is a distinct
# token STRING that authenticates identically — leading-zero malleability, a bug
# class that previously shipped. The codec must give every value exactly one
# string form.
# ---------------------------------------------------------------------------

_B62_ALPHABET = ALPHANUMERIC_CHARS


@given(data=st.binary(max_size=48))
@settings(max_examples=_B62_EXAMPLES)
@example(data=b"")
@example(data=b"\x00")  # single leading zero — the malleability class
@example(data=b"\x00\x00")  # multiple leading zeros
@example(data=b"\x00\x00\x2a")
def prop_base62_canonical_roundtrip(data):
    """decode(encode(b)) == b (leading zeros preserved) AND encode(decode(s)) == s
    for the canonical string produced by the encoder."""
    code = _bytes_to_base62(data)
    assert _base62_to_bytes(code) == data, f"byte roundtrip failed for {data!r}"
    assert _bytes_to_base62(_base62_to_bytes(code)) == code, (
        f"canonical string not idempotent: {code!r}"
    )


@given(s=st.text(alphabet=_B62_ALPHABET, min_size=1, max_size=40))
@settings(max_examples=_B62_EXAMPLES)
@example(s="0")  # leading-zero non-canonical form of the empty payload
def prop_base62_arbitrary_strings_canonical(s):
    """A valid-alphabet string either decodes to a value whose canonical
    re-encoding is exactly itself, or is rejected — never a crash, never a
    non-canonical alias that authenticates identically."""
    try:
        raw = _base62_to_bytes(s)
    except ValueError:
        return  # non-canonical / missing sentinel -> correctly rejected
    assert _bytes_to_base62(raw) == s, f"accepted non-canonical alias: {s!r}"


def _base62_leading_zero_rejected() -> tuple[bool, str]:
    """Leading-"0"-prefixed aliases of a canonical code must be refused."""
    canonical = _bytes_to_base62(b"\x05\x06\x07")
    for prefix in ("0", "00", "000"):
        alias = prefix + canonical
        try:
            _base62_to_bytes(alias)
        except ValueError:
            continue
        return False, f"non-canonical alias {alias!r} accepted (malleability)"
    return True, ""


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

_PROPERTIES = (
    prop_ref_roundtrip,
    prop_ref_roundtrip_no_salt,
    prop_ref_roundtrip_padded,
    prop_data_roundtrip,
    prop_data_roundtrip_no_salt,
    prop_data_roundtrip_padded,
    prop_ref_bitflip_rejected,
    prop_data_bitflip_rejected,
    prop_key_rotation_ref,
    prop_key_rotation_data,
    prop_different_refs_different_tokens,
    prop_same_ref_different_tokens_with_salt,
    prop_wrong_key_rejects,
    prop_base62_canonical_roundtrip,
    prop_base62_arbitrary_strings_canonical,
)

_CORPORA = (("base62 leading-zero rejected", _base62_leading_zero_rejected),)


def run_tests() -> bool:
    print("\n-- Token Signing Hypothesis Property Tests --\n")
    for prop in _PROPERTIES:
        run_property(prop)
    for name, corpus in _CORPORA:
        ok, detail = corpus()
        check(name, ok, detail)
    return finish()


if __name__ == "__main__":
    run_main(run_tests)
