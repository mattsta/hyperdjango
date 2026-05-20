"""
Password hashing — argon2id via argon2-cffi.

The PASSWORD_HASHER setting (from conf.py) selects the hashing algorithm.
Currently only "argon2id" is supported. If a different value is configured,
hash_password() raises ValueError at call time.
"""

from hyperdjango.conf import get_setting
from hyperdjango.native._crypto import (
    hash_password as _argon2_hash,
)
from hyperdjango.native._crypto import (
    needs_rehash,
    verify_password,
)


def hash_password(password: str) -> str:
    """Hash a password using the configured PASSWORD_HASHER.

    Reads PASSWORD_HASHER from settings (default "argon2id"). Raises
    ValueError if the configured hasher is not supported.
    """
    hasher = get_setting("PASSWORD_HASHER")
    if hasher != "argon2id":
        raise ValueError(
            f"Unsupported PASSWORD_HASHER: {hasher!r}. Only 'argon2id' is supported."
        )
    return _argon2_hash(password)


__all__ = ["hash_password", "verify_password", "needs_rehash"]
