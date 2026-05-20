"""
Tests for Zig JSON serializer — model_dump() and __dict__ fallback.

# hyper-test: unit

Validates:
1. Basic types serialize correctly (dict, list, str, int, float, bool, None)
2. model_dump() protocol: objects with model_dump() serialize as the returned dict
3. __dict__ fallback: non-slotted objects serialize via __dict__
4. Enum _value_ protocol still works
5. SessionUser serializes via model_dump()
6. Nested model_dump() objects serialize recursively
7. Fallback to str() for objects without model_dump() or __dict__

Usage:
    uv run hyper-test json_serializer
"""

import enum
import json
import math
import os
import sys
from dataclasses import dataclass

from hyperdjango.auth.user import SessionUser
from hyperdjango.native import fast_json_dumps

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" -- {msg}"
        errors.append(err)
        print(f"  {err}")


def parse(obj: object) -> object:
    """Serialize via Zig, then parse back to Python for comparison."""
    return json.loads(fast_json_dumps(obj))


# ---------------------------------------------------------------------------
# 1. Basic types
# ---------------------------------------------------------------------------


def test_basic_types():
    print("\n-- Basic types --")
    check("dict", parse({"a": 1}) == {"a": 1})
    check("list", parse([1, 2, 3]) == [1, 2, 3])
    check("str", parse("hello") == "hello")
    check("int", parse(42) == 42)
    check("float", parse(3.14) == 3.14)
    check("float precision", parse(3.231595593784499e16) == 3.231595593784499e16)
    check("float negative", parse(-1.5e-10) == -1.5e-10)
    check("float zero", parse(0.0) == 0.0)
    # Non-finite floats → LOSSLESS strings (never null), matching the PG→JSON
    # path; each round-trips exactly through float().
    check('float nan→"NaN"', parse(float("nan")) == "NaN")
    check("float nan round-trips", math.isnan(float(parse(float("nan")))))
    check('float inf→"Infinity"', parse(float("inf")) == "Infinity")
    check('float -inf→"-Infinity"', parse(float("-inf")) == "-Infinity")
    check(
        "float inf round-trips",
        float(parse(float("inf"))) == float("inf")
        and float(parse(float("-inf"))) == float("-inf"),
    )
    check("bool true", parse(True) is True)
    check("bool false", parse(False) is False)
    check("none", parse(None) is None)
    check("nested", parse({"a": [1, {"b": 2}]}) == {"a": [1, {"b": 2}]})
    check("empty dict", parse({}) == {})
    check("empty list", parse([]) == [])
    check("tuple as list", parse((1, 2)) == [1, 2])


# ---------------------------------------------------------------------------
# 2. model_dump() protocol
# ---------------------------------------------------------------------------


class HasModelDump:
    def model_dump(self) -> dict:
        return {"x": 10, "y": "hello"}


class NestedModelDump:
    def __init__(self, inner):
        self._inner = inner

    def model_dump(self) -> dict:
        return {"inner": self._inner, "tag": "outer"}


def test_model_dump():
    print("\n-- model_dump() protocol --")
    obj = HasModelDump()
    result = parse(obj)
    check("model_dump dict", result == {"x": 10, "y": "hello"}, repr(result))

    # Nested: model_dump returns a dict containing another model_dump object
    inner = HasModelDump()
    outer = NestedModelDump(inner)
    result = parse(outer)
    check(
        "nested model_dump",
        result == {"inner": {"x": 10, "y": "hello"}, "tag": "outer"},
        repr(result),
    )

    # model_dump in a list
    result = parse([HasModelDump(), HasModelDump()])
    check(
        "model_dump in list",
        result == [{"x": 10, "y": "hello"}, {"x": 10, "y": "hello"}],
        repr(result),
    )

    # model_dump in a dict value
    result = parse({"item": HasModelDump()})
    check(
        "model_dump in dict", result == {"item": {"x": 10, "y": "hello"}}, repr(result)
    )


# ---------------------------------------------------------------------------
# 3. __dict__ fallback
# ---------------------------------------------------------------------------


class PlainObject:
    def __init__(self):
        self.name = "test"
        self.value = 42


def test_dict_fallback():
    print("\n-- __dict__ fallback --")
    obj = PlainObject()
    result = parse(obj)
    check("__dict__ fallback", result == {"name": "test", "value": 42}, repr(result))


# ---------------------------------------------------------------------------
# 4. Enum
# ---------------------------------------------------------------------------


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


class IntColor(enum.IntEnum):
    RED = 1
    GREEN = 2


def test_enum():
    print("\n-- Enum --")
    check("str enum", parse(Color.RED) == "red")
    check("int enum", parse(IntColor.GREEN) == 2)
    check("enum in dict", parse({"color": Color.RED}) == {"color": "red"})
    check("enum in list", parse([Color.RED, Color.GREEN]) == ["red", "green"])


# ---------------------------------------------------------------------------
# 5. SessionUser
# ---------------------------------------------------------------------------


def test_session_user():
    print("\n-- SessionUser --")
    user = SessionUser(
        {"id": 42, "username": "alice", "email": "alice@test.com", "groups": ["staff"]}
    )
    result = parse(user)
    check("session user id", result["id"] == 42)
    check("session user username", result["username"] == "alice")
    check("session user email", result["email"] == "alice@test.com")
    check("session user is_staff", result["is_staff"] is True)
    check("session user is_authenticated", result["is_authenticated"] is True)
    check("session user is dict", isinstance(result, dict))

    # In a response payload
    payload = {"user": user, "status": "ok"}
    result = parse(payload)
    check("session user in payload", result["user"]["username"] == "alice")
    check("payload status", result["status"] == "ok")


# ---------------------------------------------------------------------------
# 6. str() fallback for unserializable objects
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SlottedNoModelDump:
    x: int
    y: str


def test_str_fallback():
    print("\n-- str() fallback --")
    obj = SlottedNoModelDump(x=1, y="hi")
    result = parse(obj)
    # Slotted dataclass without model_dump() → str() representation
    check("str fallback is string", isinstance(result, str), repr(result))
    check("str fallback contains data", "1" in result and "hi" in result, repr(result))


# ---------------------------------------------------------------------------
# 7. Hypothesis fuzz
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 1000
_SUPPRESS = [HealthCheck.too_slow] if _PARALLEL else []


@given(
    data=st.dictionaries(
        st.text(min_size=1, max_size=50),
        st.one_of(
            st.integers(min_value=-(2**63), max_value=2**63 - 1),
            st.floats(allow_nan=False, allow_infinity=False),
            st.text(),
            st.booleans(),
            st.none(),
        ),
        max_size=20,
    )
)
@hsettings(max_examples=200, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_roundtrip(data):
    """Zig serializer roundtrips arbitrary dicts (ints, floats, strings, bools, None)."""
    result = json.loads(fast_json_dumps(data))
    assert result == data


@given(
    id_val=st.integers(min_value=0, max_value=10000),
    username=st.text(
        min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))
    ),
)
@hsettings(max_examples=100, deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_fuzz_session_user(id_val, username):
    """SessionUser model_dump() roundtrips for arbitrary user data."""
    user = SessionUser({"id": id_val, "username": username})
    result = json.loads(fast_json_dumps(user))
    assert result["id"] == id_val
    assert result["username"] == username
    assert result["is_authenticated"] is True


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- JSON Serializer Tests --\n")

    test_basic_types()
    test_model_dump()
    test_dict_fallback()
    test_enum()
    test_session_user()
    test_str_fallback()

    # Hypothesis fuzz
    print("\n-- Hypothesis fuzz --")
    fuzz_tests = [
        ("fuzz: dict roundtrip", test_fuzz_roundtrip),
        ("fuzz: session user roundtrip", test_fuzz_session_user),
    ]
    for name, fn in fuzz_tests:
        try:
            fn()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            errors.append(f"FAIL: {name}: {e}")
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"JSON serializer: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
