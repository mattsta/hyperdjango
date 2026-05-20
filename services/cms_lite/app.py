"""
CMS Lite — Redirects and Flat Pages Example.

Showcases 2 platform features not covered by other services:

  - **URL Redirects** (redirects.py): RedirectRegistry, RedirectMiddleware,
    O(1) exact match, prefix matching, open-redirect protection
  - **Flat Pages** (flatpages.py): FlatPageRegistry, FlatPageMiddleware,
    auth-gated pages, template rendering

Models:
  - Uses the built-in FlatPage and Redirect registries (no custom models)

Run:
    uv run hyper setup --app services.cms_lite.app:app --seed services.cms_lite.seed:run
    uv run hyper run --app services.cms_lite.app:app --port 8760

Endpoints:
    GET  /                       → Homepage (flat page)
    GET  /about/                 → About page (flat page)
    GET  /terms/                 → Terms page (flat page, auth-gated)
    GET  /old-about              → Redirects to /about/ (301)
    GET  /blog/*                 → Prefix redirect to /posts/* (301)
    GET  /api/pages              → List all flat pages (JSON)
    GET  /api/redirects          → List all redirects (JSON)
    GET  /health                 → Health check
    GET  /admin/                 → HyperAdmin panel
    GET  /docs/                  → Swagger UI
"""

from hyperdjango import HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.conf import get_setting
from hyperdjango.flatpages import FlatPageRegistry
from hyperdjango.openapi import mount_docs
from hyperdjango.redirects import Redirect, RedirectMiddleware, RedirectRegistry
from hyperdjango.signing import SigningKey, TokenEngine

# ─── App Setup ────────────────────────────────────────────────────────────────

app = HyperApp(
    title="CMS Lite",
    database=get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test",
)

token_engine = TokenEngine(
    keys=[SigningKey(secret=get_setting("SESSION_SIGNING_KEY"), version=1)]
)
auth = SessionAuth(secret=get_setting("SESSION_SECRET"), token_engine=token_engine)
app.use(auth)

# Registries
page_registry = FlatPageRegistry()
redirect_registry = RedirectRegistry()

_registries_loaded = False


async def _ensure_registries():
    """Load flat pages from DB and redirects on first request."""
    global _registries_loaded
    if _registries_loaded:
        return
    _registries_loaded = True
    await page_registry.load_all()
    redirects = [
        Redirect(old_path="/old-about", new_path="/about/", status_code=301),
        Redirect(old_path="/old-terms", new_path="/terms/", status_code=301),
        Redirect(old_path="/info", new_path="/about/", status_code=302),
        Redirect(old_path="/blog/*", new_path="/api/pages", status_code=301),
    ]
    await redirect_registry.load_all(redirects)


# Redirect middleware — wraps RedirectMiddleware with lazy registry loading
_redirect_mw = RedirectMiddleware(registry=redirect_registry)


async def _redirect_with_lazy_load(request, call_next):
    await _ensure_registries()
    return await _redirect_mw(request, call_next)


app.use(_redirect_with_lazy_load)

admin = HyperAdmin(
    app, prefix="/admin", title="CMS Admin", secret_key=get_setting("ADMIN_SECRET")
)


# ─── Flat Page Views ──────────────────────────────────────────────────────────


@app.get("/")
async def homepage(request):
    """Serve homepage flat page."""
    await _ensure_registries()
    page = page_registry.lookup("/")
    if page is None:
        return Response.json(
            {"title": "CMS Lite", "message": "Welcome! Run seed to populate pages."}
        )
    return Response.json(page.to_context())


@app.get("/about/")
async def about_page(request):
    """Serve about flat page."""
    await _ensure_registries()
    page = page_registry.lookup("/about/")
    if page is None:
        return Response.json({"error": "Page not found"}, status=404)
    return Response.json(page.to_context())


@app.get("/terms/")
async def terms_page(request):
    """Serve terms page (auth-gated)."""
    await _ensure_registries()
    page = page_registry.lookup("/terms/")
    if page is None:
        return Response.json({"error": "Page not found"}, status=404)
    if page.registration_required:
        user = request.user
        if user is None or not user.is_authenticated:
            return Response.json(
                {"error": "Login required to view this page"}, status=401
            )
    return Response.json(page.to_context())


@app.get("/help/")
async def help_page(request):
    """Serve help flat page."""
    await _ensure_registries()
    page = page_registry.lookup("/help/")
    if page is None:
        return Response.json({"error": "Page not found"}, status=404)
    return Response.json(page.to_context())


@app.get("/privacy/")
async def privacy_page(request):
    """Serve privacy flat page."""
    await _ensure_registries()
    page = page_registry.lookup("/privacy/")
    if page is None:
        return Response.json({"error": "Page not found"}, status=404)
    return Response.json(page.to_context())


# ─── API ──────────────────────────────────────────────────────────────────────


@app.get("/api/pages")
async def list_pages(request):
    """List all active flat pages."""
    await _ensure_registries()
    pages = page_registry.get_all()
    return Response.json(
        {
            "count": len(pages),
            "pages": [p.to_context() for p in pages],
        }
    )


@app.get("/api/redirects")
async def list_redirects(request):
    """List all active redirects."""
    await _ensure_registries()
    redirects = redirect_registry.all_redirects()
    return Response.json(
        {
            "count": len(redirects),
            "redirects": [
                {
                    "old_path": r.old_path,
                    "new_path": r.new_path,
                    "status_code": r.status_code,
                }
                for r in redirects
            ],
        }
    )


# ─── Redirect Routes (explicit, since Zig router 404 bypasses middleware) ─────


@app.get("/old-about")
@app.get("/old-terms")
@app.get("/info")
async def redirect_handler(request):
    """Handle redirected URLs explicitly."""
    await _ensure_registries()
    result = redirect_registry.lookup(request.path)
    if result is not None:
        new_path, status_code = result
        return Response.redirect(new_path, status=status_code)
    return Response.json({"error": "Not Found"}, status=404)


@app.get("/health")
async def health(request):
    return Response.json({"status": "ok"})


mount_docs(app)
