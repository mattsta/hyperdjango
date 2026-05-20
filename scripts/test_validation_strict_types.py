#!/usr/bin/env python3
"""Strict-type validation tests for BaseModel.

Covers:
  * int fields reject fractional / non-finite floats (no silent truncation);
    whole-valued floats like 5.0 are still accepted as 5
  * Optional[scalar] validates the inner type AND allows None; multi-member
    unions keep pass-through behavior
  * an unresolvable forward-ref annotation no longer disables validation for
    the whole model — resolvable fields still validate, and a warning is raised
  * ultra-fast classes get a specialized __init__; classes that need post-init
    keep the generic __init__

NOTE: the native-path int assertions (1.5 rejected) require the rebuilt
extension (`uv run hyper-build`) since the guard also lives in the native
validator.
"""

# hyper-test: unit

import warnings

from hyperdjango.validation.core import BaseModel
from hyperdjango.validation.core.validator import (
    ValidationError,
    ValidationErrors,
)

_PASS = 0


def _ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    print(f"  ✓ {msg}")


def _rejects(fn, label: str) -> None:
    try:
        fn()
    except ValidationError, ValidationErrors:
        _ok(f"rejects {label}")
        return
    raise AssertionError(f"expected rejection for {label}")


# ── int rejects fractional / non-finite floats ────────────────────────────────
class IntM(BaseModel):
    x: int


assert IntM(x=5).x == 5
_ok("int accepts int")
assert IntM(x=5.0).x == 5
_ok("int accepts whole-valued float 5.0 -> 5")
_rejects(lambda: IntM(x=1.5), "int x=1.5")
_rejects(lambda: IntM(x=float("inf")), "int x=inf")
_rejects(lambda: IntM(x=float("nan")), "int x=nan")


# ── Optional[scalar] validates inner type + allows None ───────────────────────
class OptStr(BaseModel):
    x: str | None = None


assert OptStr(x="hi").x == "hi"
_ok("Optional[str] accepts str")
assert OptStr().x is None
_ok("Optional[str] default None")
assert OptStr(x=None).x is None
_ok("Optional[str] explicit None")
_rejects(lambda: OptStr(x=123), "Optional[str] x=123")


class OptInt(BaseModel):
    y: int | None = None


assert OptInt(y=5).y == 5
_ok("Optional[int] accepts int")
assert OptInt(y=None).y is None
_ok("Optional[int] None")
_rejects(lambda: OptInt(y="no"), "Optional[int] y='no'")
_rejects(lambda: OptInt(y=1.5), "Optional[int] y=1.5 (fractional)")


# multi-member unions keep pass-through behavior
class UnionM(BaseModel):
    z: int | str = 0


assert UnionM(z=5).z == 5
assert UnionM(z="a").z == "a"
_ok("Union[int, str] pass-through accepts both")


# ── specialized fast __init__ on ultra-fast classes ───────────────────────────
class FastM(BaseModel):
    a: int
    b: str


assert FastM.__dhi_use_ultra_fast__ is True
assert getattr(FastM.__init__, "_dhi_managed", False) is True
assert FastM.__init__ is not BaseModel.__init__
_ok("ultra-fast class gets a specialized __init__")
m = FastM(a=1, b="x")
assert m.a == 1 and m.b == "x"
_ok("fast __init__ constructs correctly")
_rejects(lambda: FastM(a=1.5, b="x"), "fast-path int a=1.5")


# a model with model_post_init must NOT get the fast closure (pinned to generic)
class PostM(BaseModel):
    a: int

    def model_post_init(self, __context) -> None:
        object.__setattr__(self, "_post_ran", True)


assert PostM.__init__ is BaseModel.__init__
_ok("post_init class pinned to the generic __init__")
p = PostM(a=3)
assert p.a == 3
assert getattr(p, "_post_ran", False) is True
_ok("post_init still runs under the generic __init__")


# ── an unresolvable forward ref warns but does NOT disable validation ─────────
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")

    class Fwd(BaseModel):
        a: int
        b: ThisTypeIsNotDefinedAnywhere  # noqa: F821

    assert Fwd(a=7).a == 7
    _ok("resolvable field 'a' still validates despite unresolvable 'b'")
    _rejects(lambda: Fwd(a=1.5), "Fwd a=1.5 (field 'a' still validated)")

assert any("could not resolve" in str(w.message) for w in caught), (
    "expected a 'could not resolve type hints' warning"
)
_ok("unresolvable annotation emits a warning instead of silently disabling")


print(f"\n✅ strict-type validation: {_PASS} checks passed")
