"""Seed data for cms_lite service."""

from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.database import Database
from hyperdjango.redirects import Redirect

from .app import page_registry, redirect_registry


async def run(db: Database) -> None:
    # Admin user for HyperAdmin
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()

    # ── Flat Pages (written to DB via registry.add) ───────────────────────
    await page_registry.ensure_table()
    pages = [
        (
            "/",
            "Welcome to CMS Lite",
            "<h1>CMS Lite</h1><p>A lightweight content management system built with HyperDjango.</p>"
            "<p>Features: flat pages, URL redirects, admin panel.</p>",
            False,
        ),
        (
            "/about/",
            "About Us",
            "<h1>About CMS Lite</h1><p>This service demonstrates HyperDjango's "
            "flatpages and redirects modules.</p>",
            False,
        ),
        (
            "/terms/",
            "Terms of Service",
            "<h1>Terms of Service</h1><p>By using this service you agree to our terms.</p>"
            "<p>This page requires authentication to view.</p>",
            True,
        ),
        (
            "/help/",
            "Help Center",
            "<h1>Help</h1><p>Need help? Check our documentation at /docs/.</p>",
            False,
        ),
        (
            "/privacy/",
            "Privacy Policy",
            "<h1>Privacy Policy</h1><p>We respect your privacy. No tracking, no ads.</p>",
            False,
        ),
    ]
    for url, title, content, auth_required in pages:
        await page_registry.add(
            url, title, content, registration_required=auth_required
        )

    # ── Redirects ─────────────────────────────────────────────────────────
    redirects = [
        Redirect(old_path="/old-about", new_path="/about/", status_code=301),
        Redirect(old_path="/old-terms", new_path="/terms/", status_code=301),
        Redirect(old_path="/info", new_path="/about/", status_code=302),
        Redirect(old_path="/blog/*", new_path="/api/pages", status_code=301),
    ]
    await redirect_registry.load_all(redirects)

    print(f"  CMS Lite seeded: {len(pages)} pages, {len(redirects)} redirects")
