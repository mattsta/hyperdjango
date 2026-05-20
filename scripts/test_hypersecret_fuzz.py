"""
Property-based fuzz: HyperSecret envelope crypto + injection parsing.

# hyper-test: unit

Properties proven:

  1. Roundtrip: open(seal(pt, slot)) == pt for arbitrary payloads and slots.
  2. AAD binding: perturbing ANY slot coordinate (namespace, key, version) or
     the wrap-layer kek_id makes decryption fail with DecryptError — blob
     substitution can never yield wrong-slot plaintext.
  3. Key isolation: a different KEK never decrypts, before or after rewrap;
     after rewrap the OLD KEK no longer decrypts.
  4. Tamper: any single-byte corruption of ciphertext or encrypted_dek fails.
  5. Rewrap preserves payload bytes exactly (payload ciphertext is immutable).
  6. wipe() zeroizes buffers in place.
  7. Envelope parsing rejects malformed/foreign format dicts, never crashes.
  8. Slot grammar: generated valid names pass, invalid shapes raise.
  9. secrets_run map files: parse(valid) roundtrips; bad lines raise ValueError;
     derived env-var names are always valid POSIX identifiers.
 10. KEK files: write/load roundtrip; group/world-readable files are refused.
"""

import base64
import os
import string
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.serviceclient import (  # noqa: E402
    AuthError,
    RequestError,
    ServerError,
    ServiceUnavailable,
)
from services.hypersecret import envelope as E  # noqa: E402
from services.hypersecret.client import SecretsError  # noqa: E402
from services.hypersecret.notify import (  # noqa: E402
    DELIVERED,
    PERMANENT,
    RETRYABLE,
    ChangeNotifier,
)
from services.hypersecret.secrets_run import (  # noqa: E402
    _ENV_NAME_RE,
    _env_name_for,
    _mapping_from_keys,
    parse_map_file,
)

SCRATCH = PROJECT_ROOT / ".test_scratch" / "hypersecret_fuzz"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


# -- strategies --------------------------------------------------------------

segment = st.from_regex(r"[a-z][a-z0-9-]{0,31}", fullmatch=True)
namespace = st.builds(lambda a, b: f"{a}/{b}", segment, segment)
key_name = st.from_regex(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", fullmatch=True)
kek_id_s = st.from_regex(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", fullmatch=True)
payload = st.binary(min_size=0, max_size=1024)
version_s = st.integers(min_value=1, max_value=2**31 - 1)


@st.composite
def sealed(draw):
    kek = E.generate_kek()
    ns = draw(namespace)
    key = draw(key_name)
    ver = draw(version_s)
    kid = draw(kek_id_s)
    pt = draw(payload)
    env = E.seal(pt, kek=kek, kek_id=kid, namespace=ns, key=key, version=ver)
    return kek, ns, key, ver, kid, pt, env


# -- 1. roundtrip + 6. wipe --------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(sealed())
def prop_roundtrip(case):
    kek, ns, key, ver, _kid, pt, env = case
    buf = E.open_envelope(env, kek=kek, namespace=ns, key=key, version=ver)
    assert bytes(buf) == pt
    E.wipe(buf)
    assert bytes(buf) == b"\x00" * len(pt)


# -- 2. AAD binding + 3. key isolation ---------------------------------------


@settings(max_examples=50, deadline=None)
@given(sealed(), namespace, key_name, version_s)
def prop_aad_binding(case, other_ns, other_key, other_ver):
    kek, ns, key, ver, kid, _pt, env = case
    perturbations = []
    if other_ns != ns:
        perturbations.append((other_ns, key, ver))
    if other_key != key:
        perturbations.append((ns, other_key, ver))
    if other_ver != ver:
        perturbations.append((ns, key, other_ver))
    for p_ns, p_key, p_ver in perturbations:
        try:
            E.open_envelope(env, kek=kek, namespace=p_ns, key=p_key, version=p_ver)
            raise AssertionError(f"wrong slot decrypted: {p_ns}/{p_key} v{p_ver}")
        except E.DecryptError:
            pass
    # kek_id perturbation on the wrap layer
    forged = E.SealedEnvelope(
        format=env.format,
        alg=env.alg,
        kek_id=kid + "x" if len(kid) < 64 else "other-kek",
        ciphertext=env.ciphertext,
        encrypted_dek=env.encrypted_dek,
    )
    try:
        E.open_envelope(forged, kek=kek, namespace=ns, key=key, version=ver)
        raise AssertionError("kek_id re-attribution decrypted")
    except E.DecryptError:
        pass
    # foreign KEK
    try:
        E.open_envelope(env, kek=E.generate_kek(), namespace=ns, key=key, version=ver)
        raise AssertionError("foreign KEK decrypted")
    except E.DecryptError:
        pass


# -- 4. tamper ----------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(sealed(), st.data())
def prop_tamper(case, data):
    kek, ns, key, ver, _kid, _pt, env = case
    for field_name in ("ciphertext", "encrypted_dek"):
        raw = bytearray(base64.b64decode(env.to_dict()[field_name]))
        idx = data.draw(st.integers(min_value=0, max_value=len(raw) - 1))
        raw[idx] ^= data.draw(st.integers(min_value=1, max_value=255))
        mutated_fields = env.to_dict()
        mutated_fields[field_name] = base64.b64encode(bytes(raw)).decode()
        mutated = E.SealedEnvelope.from_dict(mutated_fields)
        try:
            E.open_envelope(mutated, kek=kek, namespace=ns, key=key, version=ver)
            raise AssertionError(f"tampered {field_name} decrypted")
        except E.DecryptError:
            pass


# -- 5. rewrap ----------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(sealed(), kek_id_s)
def prop_rewrap(case, new_kid):
    kek, ns, key, ver, _kid, pt, env = case
    new_kek = E.generate_kek()
    new_dek = E.rewrap_dek(
        env,
        old_kek=kek,
        new_kek=new_kek,
        new_kek_id=new_kid,
        namespace=ns,
        key=key,
        version=ver,
    )
    env2 = E.SealedEnvelope(
        format=env.format,
        alg=env.alg,
        kek_id=new_kid,
        ciphertext=env.ciphertext,  # rewrap must never touch the payload
        encrypted_dek=new_dek,
    )
    assert (
        bytes(E.open_envelope(env2, kek=new_kek, namespace=ns, key=key, version=ver))
        == pt
    )
    try:
        E.open_envelope(env2, kek=kek, namespace=ns, key=key, version=ver)
        raise AssertionError("old KEK decrypted after rewrap")
    except E.DecryptError:
        pass


# -- 7. malformed envelopes ----------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    st.dictionaries(
        st.sampled_from(
            ["format", "alg", "kek_id", "ciphertext", "encrypted_dek", "x"]
        ),
        st.one_of(st.text(max_size=20), st.integers(), st.none()),
    )
)
def prop_malformed_envelope(data):
    try:
        env = E.SealedEnvelope.from_dict(data)
        # Fully-formed dicts must still declare our exact format + alg.
        assert env.format == E.FORMAT and env.alg == E.ALG
    except E.EnvelopeError:
        pass


# -- 8. slot grammar -----------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(namespace, key_name)
def prop_valid_slots_accepted(ns, key):
    E.validate_slot(ns, key)  # must not raise


@settings(max_examples=50, deadline=None)
@given(st.text(alphabet=string.printable, max_size=80), st.text(max_size=80))
def prop_invalid_slots_rejected(ns, key):
    parts = ns.split("/")
    ns_valid = len(parts) == 2 and all(E.SEGMENT_RE.match(p) for p in parts)
    key_valid = bool(E.KEY_RE.match(key))
    try:
        E.validate_slot(ns, key)
        assert ns_valid and key_valid
    except E.EnvelopeError:
        assert not (ns_valid and key_valid)


# -- 9. secrets_run map parsing ------------------------------------------------

env_var_name = st.from_regex(r"[A-Z_][A-Z0-9_]{0,30}", fullmatch=True)


@settings(max_examples=30, deadline=None)
@given(st.dictionaries(env_var_name, key_name, min_size=1, max_size=10))
def prop_map_roundtrip(mapping):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"map_{os.getpid()}.map"
    lines = ["# comment", ""]
    lines += [f"{k}={v}" for k, v in mapping.items()]
    path.write_text("\n".join(lines) + "\n")
    assert parse_map_file(str(path)) == mapping


@settings(max_examples=30, deadline=None)
@given(key_name)
def prop_env_names_valid(key):
    assert _ENV_NAME_RE.match(_env_name_for(key))


def prop_bad_map_lines_rejected():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for bad in ("no_equals_line", "lower=x", "1BAD=x", "SP ACE=x"):
        path = SCRATCH / f"bad_{os.getpid()}.map"
        path.write_text(bad + "\n")
        try:
            parse_map_file(str(path))
            raise AssertionError(f"accepted bad line: {bad!r}")
        except ValueError:
            pass


# -- 9b. secrets_run env-name collisions --------------------------------------


def prop_env_name_collisions_refused():
    """Keys that fold to the SAME env var name (db-password / db_password →
    DB_PASSWORD) must be refused, not silently collapsed to one injection.
    Distinct keys still round-trip."""
    for colliding in (
        ["db-password", "db_password"],
        ["a.b", "a-b"],
        ["X", "x"],  # both → X
    ):
        try:
            _mapping_from_keys(colliding)
            raise AssertionError(f"collision accepted: {colliding!r}")
        except SecretsError:
            pass
    # A collision-free set maps one env var per key.
    clean = ["stripe_key", "db_password", "jwt_secret"]
    mapping = _mapping_from_keys(clean)
    assert set(mapping.values()) == set(clean)
    assert len(mapping) == len(clean)


# -- 11. change-notifier outcome taxonomy -------------------------------------


class _StubClient:
    """Stand-in ServiceClient whose request() raises a chosen error (or not)."""

    def __init__(self, error):
        self._error = error

    def request(self, *_args, **_kwargs):
        if self._error is not None:
            raise self._error
        return {"ok": True}


def prop_notify_outcome_taxonomy():
    """The drainer's park/retry decision must follow the HTTP taxonomy: a
    429/408 burst is RETRYABLE (re-drain later), only a genuinely permanent 4xx
    parks. A 5xx / transport failure is RETRYABLE; a clean post is DELIVERED."""
    notifier = ChangeNotifier(manager_url="http://hub.invalid", manager_token="t")
    cases = [
        (None, DELIVERED),
        (ServiceUnavailable("down"), RETRYABLE),
        (ServerError("boom", status=503), RETRYABLE),
        (RequestError("throttled", status=429), RETRYABLE),
        (RequestError("timeout", status=408), RETRYABLE),
        (RequestError("bad", status=400), PERMANENT),
        (RequestError("gone", status=404), PERMANENT),
        (RequestError("conflict", status=409), PERMANENT),
        (RequestError("unprocessable", status=422), PERMANENT),
        (AuthError("forbidden", status=403), PERMANENT),
    ]
    for error, expected in cases:
        notifier._client = _StubClient(error)
        outcome = notifier.post("secrets/prod/api/k", "rotated", {}, dedupe_key="1")
        assert outcome.status == expected, (
            f"{error!r} → {outcome.status}, expected {expected}"
        )


# -- 10. KEK files -------------------------------------------------------------


def prop_kek_file_roundtrip_and_perms():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"kek_{os.getpid()}.kek"
    if path.exists():
        path.unlink()
    kek = E.generate_kek()
    E.write_kek_file(str(path), "test-v1", kek)
    assert (path.stat().st_mode & 0o777) == 0o600
    kid, loaded = E.load_kek_file(str(path))
    assert kid == "test-v1" and loaded == kek
    path.chmod(0o644)
    try:
        E.load_kek_file(str(path))
        raise AssertionError("world-readable KEK file accepted")
    except E.EnvelopeError:
        pass
    finally:
        path.unlink()


# -----------------------------------------------------------------------------

PROPS = [
    ("roundtrip + wipe", prop_roundtrip),
    ("AAD slot binding + key isolation", prop_aad_binding),
    ("tamper detection", prop_tamper),
    ("rewrap semantics", prop_rewrap),
    ("malformed envelope parsing", prop_malformed_envelope),
    ("valid slots accepted", prop_valid_slots_accepted),
    ("invalid slots rejected", prop_invalid_slots_rejected),
    ("map file roundtrip", prop_map_roundtrip),
    ("derived env names valid", prop_env_names_valid),
    ("bad map lines rejected", prop_bad_map_lines_rejected),
    ("env-name collisions refused", prop_env_name_collisions_refused),
    ("notify outcome taxonomy (429/408 retry, 4xx park)", prop_notify_outcome_taxonomy),
    ("KEK file roundtrip + permission check", prop_kek_file_roundtrip_and_perms),
]


def main() -> bool:
    print("HyperSecret envelope + injection fuzz")
    for name, prop in PROPS:
        try:
            prop()
            check(name, True)
        except AssertionError as exc:
            check(name, False, str(exc)[:200])
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
