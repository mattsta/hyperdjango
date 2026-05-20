"""HyperAdmin CSP nonce support (task J9).

Verifies the request-scoped CSP nonce mechanism:

  * The ``csp_nonce_attr`` template global renders ` nonce="..."` when a nonce
    is present and an empty string when it is absent — so the no-nonce output
    is byte-identical to the original (no stray ``nonce=`` literal anywhere).
  * The admin's explicit inline ``<script>``/``<style>`` blocks pick up the
    nonce from ``csp_nonce`` in the template context.
  * ``HyperAdmin._base_context`` surfaces ``request.csp_nonce`` (an optional,
    middleware-supplied attribute) into the template context, and ``_render``
    preserves it through to the rendered HTML.

Run:  uv run hyper-test admin_csp_nonce
"""

# hyper-test: unit

from dataclasses import dataclass

from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.templates import _ADMIN_CSS, _TEMPLATE_HEADER, _THEME_JS
from hyperdjango.templating import TemplateEngine, csp_nonce_attr

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


# ── csp_nonce_attr() helper ─────────────────────────────────────────────────


def test_helper_with_nonce() -> None:
    print("\n=== csp_nonce_attr() with a nonce ===")
    check(
        "leading-space prefixed attribute",
        csp_nonce_attr("abc123") == ' nonce="abc123"',
        repr(csp_nonce_attr("abc123")),
    )
    check(
        "url-safe base64 nonce passes through",
        csp_nonce_attr("aB9-_+/=") == ' nonce="aB9-_+/="',
        repr(csp_nonce_attr("aB9-_+/=")),
    )


def test_helper_without_nonce() -> None:
    print("\n=== csp_nonce_attr() without a nonce ===")
    check("None -> empty string", csp_nonce_attr(None) == "")
    check("empty string -> empty string", csp_nonce_attr("") == "")
    check("no-arg default -> empty string", csp_nonce_attr() == "")


def test_helper_sanitizes_injection() -> None:
    print("\n=== csp_nonce_attr() sanitizes hostile input ===")
    # A quote/angle-bracket payload must NOT break out of the attribute.
    hostile = '"><script>alert(1)</script>'
    out = csp_nonce_attr(hostile)
    check(
        "no double-quote in output",
        '"' not in out.replace(' nonce="', "").rstrip('"'),
        repr(out),
    )
    check("no angle bracket in output", "<" not in out and ">" not in out, repr(out))
    # All-illegal input collapses to empty (no bare ` nonce=""`).
    check(
        "all-illegal collapses to empty",
        csp_nonce_attr("<>\"'") == "",
        repr(csp_nonce_attr("<>\"'")),
    )


# ── Template global registration ────────────────────────────────────────────


def test_global_registered_by_default() -> None:
    print("\n=== csp_nonce_attr registered as default global ===")
    engine = TemplateEngine()
    check(
        "csp_nonce_attr in globals",
        engine._globals.get("csp_nonce_attr") is csp_nonce_attr,
    )


def test_render_fragment_with_nonce() -> None:
    print("\n=== render explicit <script>/<style> WITH nonce ===")
    engine = TemplateEngine()
    src = "<style{{ csp_nonce_attr(csp_nonce)|safe }}>x</style><script{{ csp_nonce_attr(csp_nonce)|safe }}>y</script>"
    out = engine.render_string(src, {"csp_nonce": "N0nc3Val"})
    check(
        "style block gets nonce",
        '<style nonce="N0nc3Val">' in out,
        out,
    )
    check(
        "script block gets nonce",
        '<script nonce="N0nc3Val">' in out,
        out,
    )


def test_render_fragment_without_nonce() -> None:
    print("\n=== render explicit <script>/<style> WITHOUT nonce ===")
    engine = TemplateEngine()
    src = "<style{{ csp_nonce_attr(csp_nonce)|safe }}>x</style><script{{ csp_nonce_attr(csp_nonce)|safe }}>y</script>"
    out = engine.render_string(src, {})
    check(
        "output byte-identical to bare tags",
        out == "<style>x</style><script>y</script>",
        out,
    )
    check("no 'nonce=' literal present", "nonce=" not in out, out)


# ── Admin header template (the real inline blocks) ──────────────────────────


def test_admin_header_with_nonce() -> None:
    print("\n=== admin _TEMPLATE_HEADER WITH nonce ===")
    engine = TemplateEngine()
    ctx = {
        "title": "T",
        "admin_title": "Admin",
        "prefix": "/admin",
        "registered_models": [],
        "extra_media": "",
        "csp_nonce": "HdrNonce",
    }
    out = engine.render_string(_TEMPLATE_HEADER, ctx)
    check("style block has nonce", '<style nonce="HdrNonce">' in out, out[:200])
    check("inline fk script has nonce", '<script nonce="HdrNonce">' in out, "")
    check(
        "htmx script tag has nonce",
        'src="/static/htmx.min.js" nonce="HdrNonce"' in out,
        "",
    )


def test_admin_header_without_nonce() -> None:
    print("\n=== admin _TEMPLATE_HEADER WITHOUT nonce ===")
    engine = TemplateEngine()
    ctx = {
        "title": "T",
        "admin_title": "Admin",
        "prefix": "/admin",
        "registered_models": [],
        "extra_media": "",
    }
    out = engine.render_string(_TEMPLATE_HEADER, ctx)
    check("no 'nonce=' literal", "nonce=" not in out, "")
    check("bare <style> present", "<style>" in out, "")
    check(
        "bare inline <script> present",
        "<script>\nfunction hyperFkAutocomplete" in out,
        "",
    )
    check(
        "htmx script tag unchanged",
        '<script src="/static/htmx.min.js"></script>' in out,
        "",
    )


def test_theme_js_block() -> None:
    print("\n=== _THEME_JS inline block ===")
    engine = TemplateEngine()
    with_nonce = engine.render_string(_THEME_JS, {"csp_nonce": "ThemeN"})
    check(
        "theme js has nonce", '<script nonce="ThemeN">' in with_nonce, with_nonce[:60]
    )
    without = engine.render_string(_THEME_JS, {})
    check(
        "theme js unchanged without nonce", without.startswith("<script>"), without[:60]
    )
    check("theme js no 'nonce=' without nonce", "nonce=" not in without, "")


def test_admin_css_unmodified() -> None:
    print("\n=== _ADMIN_CSS body untouched ===")
    # We only nonce the explicit <style> TAG, never the ~230 inline style= attrs.
    check("no csp_nonce_attr leaked into CSS body", "csp_nonce" not in _ADMIN_CSS, "")


# ── Admin _base_context + _render end-to-end wiring ─────────────────────────


@dataclass
class _FakeRequest:
    """Minimal request stand-in carrying a middleware-supplied csp_nonce."""

    csp_nonce: str | None = None
    cookies: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.cookies is None:
            self.cookies = {}


class _FakeRouter:
    """Captures route registrations so HyperAdmin can construct without a server."""

    def add(self, method: str, path: str, handler) -> None:
        pass


class _FakeApp:
    """Minimal app stub exposing only what HyperAdmin.__init__ touches."""

    def __init__(self) -> None:
        self.router = _FakeRouter()


def _make_admin() -> HyperAdmin:
    return HyperAdmin(_FakeApp(), secret_key="x" * 32)


def test_base_context_surfaces_nonce() -> None:
    print("\n=== _base_context surfaces request.csp_nonce ===")
    admin = _make_admin()
    req = _FakeRequest(csp_nonce="CtxNonce")
    ctx = admin._base_context(req)
    check(
        "csp_nonce in context",
        ctx.get("csp_nonce") == "CtxNonce",
        str(ctx.get("csp_nonce")),
    )

    req_none = _FakeRequest(csp_nonce=None)
    ctx_none = admin._base_context(req_none)
    check(
        "no csp_nonce key when request lacks one",
        "csp_nonce" not in ctx_none,
        str(ctx_none),
    )

    ctx_no_req = admin._base_context()
    check("no csp_nonce key without request", "csp_nonce" not in ctx_no_req, "")


def test_render_end_to_end_with_nonce() -> None:
    print("\n=== _render full page WITH nonce flows to inline blocks ===")
    admin = _make_admin()
    req = _FakeRequest(csp_nonce="E2ENonce")
    ctx = admin._base_context(req)
    ctx["title"] = "Dashboard"
    ctx["extra_media"] = ""
    # Render just the header (sufficient to cover all explicit inline blocks).
    html = admin.engine.render_string(_TEMPLATE_HEADER, ctx)
    check("style nonce in full render", '<style nonce="E2ENonce">' in html, "")
    check("inline script nonce in full render", '<script nonce="E2ENonce">' in html, "")


def test_render_end_to_end_without_nonce() -> None:
    print("\n=== _render full page WITHOUT nonce stays identical ===")
    admin = _make_admin()
    ctx = admin._base_context()
    ctx["title"] = "Dashboard"
    ctx["extra_media"] = ""
    html = admin.engine.render_string(_TEMPLATE_HEADER, ctx)
    check("no 'nonce=' literal in full render", "nonce=" not in html, "")


def run() -> bool:
    test_helper_with_nonce()
    test_helper_without_nonce()
    test_helper_sanitizes_injection()
    test_global_registered_by_default()
    test_render_fragment_with_nonce()
    test_render_fragment_without_nonce()
    test_admin_header_with_nonce()
    test_admin_header_without_nonce()
    test_theme_js_block()
    test_admin_css_unmodified()
    test_base_context_surfaces_nonce()
    test_render_end_to_end_with_nonce()
    test_render_end_to_end_without_nonce()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
