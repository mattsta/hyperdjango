"""Collision-free construction of composite string keys.

Single authority for building keys that concatenate several components — cache
keys, dedup keys, namespacing — where at least one component may be attacker- or
user-controlled.

The failure this prevents: a naive ``sep.join(parts)`` is NOT injective when a
component can itself contain ``sep``. Two *different* component lists then map to
the *same* string, so distinct requests collide on one key. When the components
carry identity (``user=5``, a tenant id, an auth-token hash), that collision
cross-serves or poisons another principal's cached entry — a real
confidentiality/integrity bug, not just a cache-hit-rate slip. It bit both the
``@cached`` decorator key and the HTTP page-cache key (an unauthenticated
``/p?x=1|user=5`` forged authenticated user 5's ``/p?x=1`` key).

The fix is length-prefixing: each component is emitted as ``len:value``, so a
reader consumes exactly ``len`` characters and any ``sep`` inside a component is
unambiguous data, never a boundary. Length-prefixed encoding is injective by
construction regardless of component content.

Use ``injective_join`` for EVERY new composite key. Do not hand-roll
``"|".join(...)`` over untrusted parts.
"""

from collections.abc import Iterable

__all__ = ["injective_join"]


def injective_join(parts: Iterable[str], sep: str = "|") -> str:
    """Join ``parts`` so distinct component lists never produce the same string.

    Each component is length-prefixed (``f"{len(p)}:{p}"``) before joining with
    ``sep``, making the result injective even when a component contains ``sep``.

    Args:
        parts: string components, in significant order (order is part of the key).
        sep: separator between the length-prefixed components. Its bytes may
            appear freely inside components — the length prefix disambiguates.

    Returns:
        A single string that maps 1:1 to the exact sequence of components.
    """
    return sep.join(f"{len(p)}:{p}" for p in parts)
