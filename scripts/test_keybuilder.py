"""Unit + property tests for the injective composite-key authority.

# hyper-test: unit

injective_join must map distinct component lists to distinct strings even when
components contain the separator — the property that stops cache-key forgery /
cross-principal collisions (see hyperdjango/keybuilder.py).
"""

import itertools
import random

from hyperdjango.keybuilder import injective_join
from hyperdjango.testkit import check, finish, run_main


def test_basic() -> None:
    print("\nbasic encoding")
    check("empty -> empty", injective_join([]) == "")
    check("single component length-prefixed", injective_join(["a"]) == "1:a")
    check("two components", injective_join(["a", "b"]) == "1:a|1:b")
    check("length reflects content", injective_join(["ab", "c"]) == "2:ab|1:c")


def test_no_boundary_forgery() -> None:
    print("\nseparator inside a component cannot forge a boundary")
    # The classic collision: one component containing the separator vs two
    # components split at it.
    check(
        "'a|b' distinct from ['a','b']",
        injective_join(["a|b"]) != injective_join(["a", "b"]),
    )
    check(
        "forged trust-suffix distinct from real suffix",
        injective_join(["x=1|user=5"]) != injective_join(["x=1", "user=5"]),
    )
    # Count boundary: prefix vs suffix split.
    check(
        "differing component counts stay distinct",
        injective_join(["", "ab"]) != injective_join(["", "a", "b"]),
    )


def test_property_injective() -> None:
    print("\nproperty: distinct component lists -> distinct keys (randomized)")
    alphabet = ["a", "b", "|", ":", "=", "1", ""]
    rng = random.Random(1234567)
    seen: dict[str, tuple] = {}
    collisions = 0
    samples = 0
    for _ in range(20000):
        n = rng.randint(0, 4)
        parts = tuple(
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
            for _ in range(n)
        )
        key = injective_join(parts)
        samples += 1
        prev = seen.get(key)
        if prev is not None and prev != parts:
            collisions += 1
            print(f"    COLLISION: {prev!r} and {parts!r} -> {key!r}")
        seen[key] = parts
    check(f"no collisions across {samples} random component lists", collisions == 0)

    # Exhaustive small space over the adversarial alphabet.
    small = ["", "|", ":", "a", "a|b"]
    lists = []
    for n in range(0, 3):
        lists.extend(itertools.product(small, repeat=n))
    keys = {}
    exhaustive_collisions = 0
    for parts in lists:
        k = injective_join(parts)
        if k in keys and keys[k] != parts:
            exhaustive_collisions += 1
        keys[k] = parts
    check(
        f"no collisions over {len(lists)} exhaustive lists", exhaustive_collisions == 0
    )


def main() -> bool:
    test_basic()
    test_no_boundary_forgery()
    test_property_injective()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
