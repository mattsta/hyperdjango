"""Round-11 audit regression tests (templating / i18n / staticfiles).

Locks in three CONFIRMED findings from the prior audit:

  1. i18n.parse_po_file — two entries adjacent with NO blank line between them
     must BOTH parse (gettext/msgfmt accept blank-line-less catalogs). Before
     the fix the earlier entry was silently overwritten and lost.

  2. templating.TemplateEngine(autoescape=False) — the engine-level autoescape
     default must NOT be silently ignored. When the native
     `_template_set_autoescape` FFI export is present the default is honored
     (unescaped output); when it is absent (main.zig not yet wired) construction
     is REJECTED loudly instead of quietly handing back escaped output.

  3. staticfiles._build_response — every static response (200 / 206 / 304 / 416)
     must carry `X-Content-Type-Options: nosniff` (MIME-sniffing hardening).

NOTE (native rebuild): assertion group (2) depends on whether the compiled
native module exports `_template_set_autoescape`. That export requires a
one-line method-table entry in zig/src/main.zig (NOT owned by this change):

    .{ .ml_name = "_template_set_autoescape",
       .ml_meth = @ptrCast(&template_engine.py_template_set_autoescape),
       .ml_flags = c.METH_VARARGS,
       .ml_doc = "Set engine-level autoescape default: (0 or 1)" },

Until that line ships + the module is rebuilt, this test verifies the loud
rejection path; after it ships it verifies the honored-output path.

Run:  uv run hyper-test templating_i18n_r11
"""

# hyper-test: unit

from hyperdjango.i18n import parse_po_file
from hyperdjango.staticfiles import StaticFilesMiddleware
from hyperdjango.templating import TemplateEngine, _native_set_autoescape

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class _FakeRequest:
    """Minimal request stand-in for StaticFilesMiddleware._build_response."""

    def __init__(self, method: str = "GET", headers: dict | None = None) -> None:
        self.method = method
        self.headers = headers or {}
        self.path = "/static/app.bin"


# ── Finding 1: adjacent .po entries (no blank line) ─────────────────────────


def test_po_adjacent_msgid_entries() -> None:
    print("\n=== parse_po_file: adjacent msgid entries (no blank line) ===")
    content = (
        'msgid ""\n'
        'msgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
        'msgid "hello"\n'
        'msgstr "bonjour"\n'
        'msgid "world"\n'
        'msgstr "monde"\n'
    )
    entries = parse_po_file(content)
    by_id = {e.msgid: e.msgstr for e in entries}
    check(
        "both adjacent entries parsed (header excluded)",
        len(entries) == 2,
        f"got {len(entries)}: {[e.msgid for e in entries]}",
    )
    check("first entry preserved", by_id.get("hello") == "bonjour", str(by_id))
    check("second entry preserved", by_id.get("world") == "monde", str(by_id))


def test_po_adjacent_msgctxt_entries() -> None:
    print("\n=== parse_po_file: adjacent msgctxt entries (no blank line) ===")
    content = (
        'msgctxt "menu"\n'
        'msgid "File"\n'
        'msgstr "Fichier"\n'
        'msgctxt "verb"\n'
        'msgid "File"\n'
        'msgstr "Classer"\n'
    )
    entries = parse_po_file(content)
    ctxs = {(e.msgctxt, e.msgid): e.msgstr for e in entries}
    check(
        "both context entries parsed",
        len(entries) == 2,
        f"got {len(entries)}: {[(e.msgctxt, e.msgid) for e in entries]}",
    )
    check("menu/File preserved", ctxs.get(("menu", "File")) == "Fichier", str(ctxs))
    check("verb/File preserved", ctxs.get(("verb", "File")) == "Classer", str(ctxs))


def test_po_blank_separated_still_works() -> None:
    print("\n=== parse_po_file: blank-separated entries still parse ===")
    content = 'msgid "a"\nmsgstr "A"\n\nmsgid "b"\nmsgstr "B"\n'
    entries = parse_po_file(content)
    by_id = {e.msgid: e.msgstr for e in entries}
    check("two blank-separated entries", len(entries) == 2, str(by_id))
    check("regression: a→A", by_id.get("a") == "A", str(by_id))
    check("regression: b→B", by_id.get("b") == "B", str(by_id))


def test_po_multiline_continuation_preserved() -> None:
    print("\n=== parse_po_file: multi-line continuation across adjacency ===")
    content = (
        'msgid "greeting"\n'
        'msgstr ""\n'
        '"line one "\n'
        '"line two"\n'
        'msgid "next"\n'
        'msgstr "N"\n'
    )
    entries = parse_po_file(content)
    by_id = {e.msgid: e.msgstr for e in entries}
    check("continuation entry + adjacent entry", len(entries) == 2, str(by_id))
    check(
        "multi-line msgstr concatenated",
        by_id.get("greeting") == "line one line two",
        str(by_id),
    )
    check("adjacent entry after continuation", by_id.get("next") == "N", str(by_id))


# ── Finding 2: autoescape=False not silently ignored ────────────────────────


def test_autoescape_config_not_silently_ignored() -> None:
    print("\n=== TemplateEngine(autoescape=False) not silently ignored ===")
    hostile = {"x": "<script>alert(1)</script>"}

    if _native_set_autoescape is not None:
        # Native setter wired (main.zig registered): the default is honored.
        eng_off = TemplateEngine(autoescape=False)
        out_off = eng_off.render_string("{{ x }}", hostile)
        check(
            "autoescape=False honored: output is unescaped",
            out_off == "<script>alert(1)</script>",
            repr(out_off),
        )
        eng_on = TemplateEngine(autoescape=True)
        out_on = eng_on.render_string("{{ x }}", hostile)
        check(
            "autoescape=True still escapes (default path intact)",
            "&lt;script&gt;" in out_on and "<script>" not in out_on,
            repr(out_on),
        )
    else:
        # Native setter missing: config CANNOT take effect, so construction must
        # fail loudly rather than quietly returning escaped output.
        raised = False
        try:
            TemplateEngine(autoescape=False)
        except RuntimeError:
            raised = True
        check(
            "autoescape=False loudly rejected when native setter absent",
            raised,
            "expected RuntimeError; got silent construction",
        )
        # autoescape=True (default) must still construct + escape normally.
        eng_on = TemplateEngine(autoescape=True)
        out_on = eng_on.render_string("{{ x }}", hostile)
        check(
            "autoescape=True default still constructs + escapes",
            "&lt;script&gt;" in out_on and "<script>" not in out_on,
            repr(out_on),
        )


# ── Finding 3: X-Content-Type-Options: nosniff on static responses ──────────


def _nosniff(headers: dict) -> str | None:
    for k, v in headers.items():
        if k.lower() == "x-content-type-options":
            return v
    return None


def test_static_nosniff_header() -> None:
    print("\n=== staticfiles: X-Content-Type-Options: nosniff ===")
    mw = StaticFilesMiddleware(static_dirs=[], use_cache=False)
    content = b"BINARYPAYLOAD-0123456789"
    ct = "application/octet-stream"
    etag = "abc123def456"
    mtime = 1_600_000_000.0

    # 200 full response
    r200 = mw._build_response(_FakeRequest(), content, None, ct, etag, mtime, "app.bin")
    check("200 status", r200.status == 200, str(r200.status))
    check("200 carries nosniff", _nosniff(r200.headers) == "nosniff", str(r200.headers))

    # 206 partial (range)
    r206 = mw._build_response(
        _FakeRequest(headers={"range": "bytes=0-3"}),
        content,
        None,
        ct,
        etag,
        mtime,
        "app.bin",
    )
    check("206 status", r206.status == 206, str(r206.status))
    check("206 carries nosniff", _nosniff(r206.headers) == "nosniff", str(r206.headers))

    # 304 via If-None-Match
    r304 = mw._build_response(
        _FakeRequest(headers={"if-none-match": f'"{etag}"'}),
        content,
        None,
        ct,
        etag,
        mtime,
        "app.bin",
    )
    check("304 status", r304.status == 304, str(r304.status))
    check("304 carries nosniff", _nosniff(r304.headers) == "nosniff", str(r304.headers))

    # 416 unsatisfiable range
    r416 = mw._build_response(
        _FakeRequest(headers={"range": "bytes=9999-100000"}),
        content,
        None,
        ct,
        etag,
        mtime,
        "app.bin",
    )
    check("416 status", r416.status == 416, str(r416.status))
    check("416 carries nosniff", _nosniff(r416.headers) == "nosniff", str(r416.headers))


def run() -> bool:
    test_po_adjacent_msgid_entries()
    test_po_adjacent_msgctxt_entries()
    test_po_blank_separated_still_works()
    test_po_multiline_continuation_preserved()
    test_autoescape_config_not_silently_ignored()
    test_static_nosniff_header()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(
        f"native _template_set_autoescape present: {_native_set_autoescape is not None}"
    )
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
