#!/usr/bin/env python3
# hyper-test: unit
"""Exception→HTTP-status registry (round-15 unification).

Proves exception_to_response resolves status via HTTPException → http_status
protocol hook → register_exception_status registry (MRO) → generic 500, and
that unmapped errors never leak their message.
"""

import json
import sys

from hyperdjango.exceptions import (
    HTTPException,
    exception_to_response,
    register_exception_status,
)
from hyperdjango.models import Model
from hyperdjango.paginator import EmptyPage, InvalidPage
from hyperdjango.storage import SuspiciousFileOperation
from hyperdjango.validation.core.validator import ValidationError, ValidationErrors

_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS {name}")
    else:
        _f += 1
        print(f"  FAIL {name} — {detail}")


def body(exc):
    r = exception_to_response(exc)
    b = r.body.decode() if isinstance(r.body, bytes) else r.body
    return r.status, json.loads(b)


def main():
    # HTTPException fast path unchanged.
    s, b = body(HTTPException(418, "teapot"))
    check(
        "HTTPException status",
        s == 418 and b["status"] == 418 and b["detail"] == "teapot",
    )

    # Registry, MRO-walked.
    s, b = body(PermissionError("nope"))
    check("PermissionError→403", s == 403 and b["detail"] == "Forbidden")
    s, b = body(Model.DoesNotExist())
    check("DoesNotExist→404", s == 404 and b["detail"] == "Not Found")
    s, _ = body(Model.MultipleObjectsReturned())
    check("MultipleObjectsReturned stays 500 (data anomaly)", s == 500)
    s, b = body(SuspiciousFileOperation("../etc"))
    check(
        "SuspiciousFileOperation→400 (no path leaked)",
        s == 400 and "etc" not in b["detail"],
    )
    s, _ = body(InvalidPage())
    check("InvalidPage→404", s == 404)
    s, _ = body(EmptyPage())  # subclass, MRO resolves via InvalidPage
    check("EmptyPage (subclass)→404 via MRO", s == 404)

    # safe_detail=True echoes the validation message.
    s, b = body(ValidationError("email", "is required"))
    check("ValidationError→400 safe_detail", s == 400 and "email" in b["detail"])
    s, b = body(ValidationErrors([ValidationError("a", "bad")]))
    check("ValidationErrors→422 safe_detail", s == 422)

    # Protocol hook.
    class Custom(Exception):
        http_status = 409

    s, b = body(Custom("dup"))
    check("http_status hook→409", s == 409 and b["detail"] == "dup")

    # Unmapped → generic 500, message NOT leaked.
    s, b = body(RuntimeError("secret internal detail xyz"))
    check("unmapped RuntimeError→500", s == 500)
    check("500 does not leak message", "secret internal detail" not in json.dumps(b))

    # dynamic registration works.
    class Weird(Exception):
        pass

    register_exception_status(Weird, 451, safe_detail=True)
    s, b = body(Weird("dmca"))
    check("dynamic register→451", s == 451 and b["detail"] == "dmca")

    print(f"\n{_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
