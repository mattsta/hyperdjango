#!/usr/bin/env python3
# hyper-test: unit
"""Escaping differential gate (round-15, C3 dual-path-drift guard).

The native template HTML escaper and the Python escapers (stdlib html.escape,
admin._escape_html) MUST agree byte-for-byte — a divergence is the dual-dispatch
drift that keeps shipping native-only bugs. This feeds an adversarial corpus
through all three and asserts equality, so a future one-sided change fails CI.

Also pins the DELIBERATE divergence: a JSON response body (Response.json) is not
HTML-safe (correct for application/json), whereas the |tojson template filter is
HTML-safe — nobody should "fix" one to match the other.
"""

import html
import json
import sys

from hyperdjango._hyperdjango_native import html_escape_native

_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL {name} — {detail}")


CORPUS = [
    "",
    "plain text",
    "<script>alert(1)</script>",
    "a & b < c > d",
    '"double"',
    "'single'",
    "<>&\"'",
    "</script><img src=x onerror=alert(1)>",
    "café — ünïcode ☃",
    "tab\tnewline\nCR\r",
    "%3Cscript%3E",
    "&amp;already",
    "\x00\x01\x1f control",
    'mixed <a href="x">&\'</a>',
    "  ",
    "a" * 500 + "<b>",
    "&#x27;",
    chr(0x7F) + chr(0x80),
]


def main():
    from hyperdjango.admin.utils import _escape_html

    # 1. Native HTML escaper == Python html.escape(quote=True) == admin._escape_html.
    for s in CORPUS:
        n = html_escape_native(s)
        h = html.escape(s, quote=True)
        a = _escape_html(s)
        check(f"native==html.escape {s[:24]!r}", n == h, f"native={n!r} html={h!r}")
        check(
            f"native==admin._escape_html {s[:24]!r}",
            n == a,
            f"native={n!r} admin={a!r}",
        )

    # 2. The escaper covers ALL five HTML-significant chars.
    esc = html_escape_native("<>&\"'")
    for frag in ("&lt;", "&gt;", "&amp;", "&quot;", "&#x27;"):
        check(f"escaper emits {frag}", frag in esc, esc)

    # 3. Deliberate, PINNED divergence: Response.json is not HTML-safe; |tojson is.
    #    (Response.json escapes only JSON-structural chars, correct for
    #    application/json. Embedding it raw in HTML is misuse; use |tojson there.)
    from hyperdjango.response import Response

    body = Response.json({"x": "</script>"}).body
    body = body.decode() if isinstance(body, bytes) else body
    check(
        "Response.json is NOT html-safe (< left raw)",
        "</script>" in body,
        "if this fails, someone made Response.json HTML-escape — that's wrong",
    )
    check("Response.json is valid JSON", json.loads(body) == {"x": "</script>"})

    print(f"{_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
