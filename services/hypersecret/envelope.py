"""
HyperSecret envelope encryption — the client-side crypto authority.

Every party that touches plaintext (client SDK, provisioning CLI, seed)
funnels through this module. Server request handlers never import it: the
server stores and returns opaque envelopes and holds no KEK material, so it
has no code path that could decrypt what it stores.

Format ``sm1``, AES-256-GCM on both layers:

    payload:  nonce(12) || GCM(DEK, plaintext, aad=sm1|ns|key|version)
    DEK wrap: nonce(12) || GCM(KEK, DEK,       aad=sm1|ns|key|version|kek_id)

The AAD binds each blob to its exact slot (namespace, key, version) and the
wrap layer additionally to the KEK generation, so a substituted or
re-attributed blob fails authentication instead of decrypting to the wrong
secret.
"""

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = "sm1"
ALG = "A256GCM"

_NONCE_BYTES = 12
_KEY_BYTES = 32
_GCM_TAG_BYTES = 16

# Exact wire size of a wrapped DEK: nonce(12) || GCM(KEK, DEK) where the GCM
# ciphertext is the 32-byte DEK plus the 16-byte tag. The server enforces this
# exact decoded length on every encrypted_dek it stores (put + rewrap) so a
# buggy or hostile writer cannot overwrite a version's wrapped key with a
# short/garbage blob and brick it. Derived here — the crypto authority owns the
# constants — so the server never hard-codes the arithmetic.
ENCRYPTED_DEK_BYTES = _NONCE_BYTES + _KEY_BYTES + _GCM_TAG_BYTES

# Slot-name grammar. Enforced here (client-side) and by the server's request
# validation so both ends agree on what can appear inside an AAD string.
SEGMENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
KEK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")

# Payload ceiling — a secret is a credential, not a data store.
MAX_PLAINTEXT_BYTES = 64 * 1024


class EnvelopeError(Exception):
    """Malformed envelope, bad slot names, or oversized payload."""


class DecryptError(EnvelopeError):
    """Authentication failed: tampered blob, wrong KEK, or wrong slot."""


@dataclass(slots=True, frozen=True)
class SealedEnvelope:
    """What the server stores and returns. All fields are safe to log."""

    format: str
    alg: str
    kek_id: str
    ciphertext: str  # base64: nonce || GCM(payload)
    encrypted_dek: str  # base64: nonce || GCM(DEK)

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "alg": self.alg,
            "kek_id": self.kek_id,
            "ciphertext": self.ciphertext,
            "encrypted_dek": self.encrypted_dek,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SealedEnvelope:
        try:
            env = cls(
                format=data["format"],
                alg=data["alg"],
                kek_id=data["kek_id"],
                ciphertext=data["ciphertext"],
                encrypted_dek=data["encrypted_dek"],
            )
        except (KeyError, TypeError) as exc:
            raise EnvelopeError(f"Malformed envelope: {exc}") from exc
        if env.format != FORMAT:
            raise EnvelopeError(f"Unsupported envelope format: {env.format!r}")
        if env.alg != ALG:
            raise EnvelopeError(f"Unsupported algorithm: {env.alg!r}")
        return env


def generate_kek() -> bytes:
    """Generate a fresh 32-byte namespace master key."""
    return AESGCM.generate_key(bit_length=256)


def kek_fingerprint(kek: bytes) -> str:
    """Short non-reversible fingerprint for display/sanity checks."""
    return hashlib.sha256(kek).hexdigest()[:16]


def validate_slot(namespace: str, key: str) -> None:
    """Validate namespace ('env/service') and key against the shared grammar."""
    parts = namespace.split("/")
    if len(parts) != 2 or not all(SEGMENT_RE.match(p) for p in parts):
        raise EnvelopeError(f"Invalid namespace: {namespace!r} (want env/service)")
    if not KEY_RE.match(key):
        raise EnvelopeError(f"Invalid secret key name: {key!r}")


def _payload_aad(namespace: str, key: str, version: int) -> bytes:
    return f"{FORMAT}|{namespace}|{key}|{version}".encode()


def _wrap_aad(namespace: str, key: str, version: int, kek_id: str) -> bytes:
    return f"{FORMAT}|{namespace}|{key}|{version}|{kek_id}".encode()


def _check_kek(kek: bytes) -> None:
    if len(kek) != _KEY_BYTES:
        raise EnvelopeError(f"KEK must be {_KEY_BYTES} bytes, got {len(kek)}")


def seal(
    plaintext: bytes,
    *,
    kek: bytes,
    kek_id: str,
    namespace: str,
    key: str,
    version: int,
) -> SealedEnvelope:
    """Envelope-encrypt ``plaintext`` for one exact (namespace, key, version) slot."""
    _check_kek(kek)
    validate_slot(namespace, key)
    if not KEK_ID_RE.match(kek_id):
        raise EnvelopeError(f"Invalid kek_id: {kek_id!r}")
    if version < 1:
        raise EnvelopeError(f"Version must be >= 1, got {version}")
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise EnvelopeError(
            f"Plaintext exceeds {MAX_PLAINTEXT_BYTES} bytes ({len(plaintext)})"
        )

    dek = AESGCM.generate_key(bit_length=256)
    try:
        nonce_p = os.urandom(_NONCE_BYTES)
        ct = AESGCM(dek).encrypt(
            nonce_p, plaintext, _payload_aad(namespace, key, version)
        )
        nonce_w = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(kek).encrypt(
            nonce_w, dek, _wrap_aad(namespace, key, version, kek_id)
        )
    finally:
        dek = b""  # drop the only strong reference promptly

    return SealedEnvelope(
        format=FORMAT,
        alg=ALG,
        kek_id=kek_id,
        ciphertext=base64.b64encode(nonce_p + ct).decode(),
        encrypted_dek=base64.b64encode(nonce_w + wrapped).decode(),
    )


def _b64decode(field_name: str, value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError(f"Envelope field {field_name} is not valid base64") from exc


def unwrap_dek(
    env: SealedEnvelope, *, kek: bytes, namespace: str, key: str, version: int
) -> bytes:
    """Recover the clear DEK for one version. Used by open_envelope and rewrap."""
    _check_kek(kek)
    blob = _b64decode("encrypted_dek", env.encrypted_dek)
    if len(blob) <= _NONCE_BYTES:
        raise EnvelopeError("encrypted_dek too short")
    try:
        return AESGCM(kek).decrypt(
            blob[:_NONCE_BYTES],
            blob[_NONCE_BYTES:],
            _wrap_aad(namespace, key, version, env.kek_id),
        )
    except InvalidTag as exc:
        raise DecryptError(
            f"DEK unwrap failed for {namespace}/{key} v{version} "
            f"(wrong KEK, tampered blob, or wrong slot)"
        ) from exc


def open_envelope(
    env: SealedEnvelope, *, kek: bytes, namespace: str, key: str, version: int
) -> bytearray:
    """Decrypt an envelope. Returns a wipeable ``bytearray``.

    Callers should zero the result when done (``ba[:] = b"\\x00" * len(ba)``);
    the client SDK's context manager does this automatically.
    """
    dek = unwrap_dek(env, kek=kek, namespace=namespace, key=key, version=version)
    blob = _b64decode("ciphertext", env.ciphertext)
    if len(blob) <= _NONCE_BYTES:
        raise EnvelopeError("ciphertext too short")
    try:
        plaintext = AESGCM(dek).decrypt(
            blob[:_NONCE_BYTES],
            blob[_NONCE_BYTES:],
            _payload_aad(namespace, key, version),
        )
    except InvalidTag as exc:
        raise DecryptError(
            f"Payload decrypt failed for {namespace}/{key} v{version}"
        ) from exc
    finally:
        dek = b""
    return bytearray(plaintext)


def rewrap_dek(
    env: SealedEnvelope,
    *,
    old_kek: bytes,
    new_kek: bytes,
    new_kek_id: str,
    namespace: str,
    key: str,
    version: int,
) -> str:
    """KEK rotation: re-wrap one version's DEK under a new KEK.

    Payload ciphertext is untouched. Returns the new base64 encrypted_dek;
    the caller POSTs it with ``new_kek_id`` to the rewrap endpoint.
    """
    _check_kek(new_kek)
    if not KEK_ID_RE.match(new_kek_id):
        raise EnvelopeError(f"Invalid kek_id: {new_kek_id!r}")
    dek = unwrap_dek(env, kek=old_kek, namespace=namespace, key=key, version=version)
    try:
        nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(new_kek).encrypt(
            nonce, dek, _wrap_aad(namespace, key, version, new_kek_id)
        )
    finally:
        dek = b""
    return base64.b64encode(nonce + wrapped).decode()


def wipe(buf: bytearray) -> None:
    """Best-effort in-place zeroization of a decrypted secret."""
    buf[:] = b"\x00" * len(buf)


# -- KEK files ---------------------------------------------------------------
# Key material and its generation id travel together: a KEK file is a small
# JSON object {"kek_id": "...", "kek": "<base64>"} created 0600.


def write_kek_file(path: str, kek_id: str, kek: bytes) -> None:
    if not KEK_ID_RE.match(kek_id):
        raise EnvelopeError(f"Invalid kek_id: {kek_id!r}")
    _check_kek(kek)
    payload = json.dumps({"kek_id": kek_id, "kek": base64.b64encode(kek).decode()})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload + "\n")


def load_kek_file(path: str) -> tuple[str, bytes]:
    """Read a KEK file → (kek_id, kek). Refuses files with open permissions."""
    p = Path(path)
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise EnvelopeError(
            f"{path} is group/world-readable (mode {oct(mode)}); chmod 600 it"
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    kek = base64.b64decode(data["kek"])
    _check_kek(kek)
    kek_id = data["kek_id"]
    if not KEK_ID_RE.match(kek_id):
        raise EnvelopeError(f"Invalid kek_id in {path}: {kek_id!r}")
    return kek_id, kek
