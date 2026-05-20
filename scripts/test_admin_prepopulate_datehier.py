#!/usr/bin/env python3
"""
Tests for admin prepopulated_fields and date_hierarchy.

Usage:
    uv run hyper-test admin_prepopulate_datehier
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from datetime import datetime
from urllib.parse import urlencode

from hyperdjango.admin import TEMPLATE_FORM, TEMPLATE_LIST, HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.request import Request


def make_admin_request(path="/admin/", query_params=None, cookies=None, user=None):
    """Create a real Request configured for admin tests."""
    qs = urlencode(query_params) if query_params else ""
    cookie_str = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    req = Request(
        method="GET",
        path=path,
        headers={"cookie": cookie_str} if cookie_str else {},
        query_string=qs,
    )
    req._admin_user = user or {
        "username": "admin",
        "is_staff": True,
        "is_superuser": True,
    }
    return req


DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class BlogPost(Model):
    class Meta:
        table = "pp_blog_posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    slug: str = Field(max_length=200, default="")
    content: str = Field(max_length=10000, default="")
    published_at: datetime | None = Field(default=None)
    created_at: datetime | None = Field(default=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("Admin prepopulated_fields + date_hierarchy Tests")
    print("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = loop.run_until_complete(setup())

    try:
        test_prepopulated_fields_config()
        test_prepopulated_fields_template()
        test_prepopulated_fields_context()
        test_date_hierarchy_config()
        test_date_hierarchy_template()
        loop.run_until_complete(test_date_hierarchy_context(db))
        loop.run_until_complete(test_date_hierarchy_filtering(db))
    finally:
        loop.run_until_complete(teardown(db))

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


async def setup():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    await db.execute("DROP TABLE IF EXISTS pp_blog_posts CASCADE")
    await db.execute("""
        CREATE TABLE pp_blog_posts (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            slug VARCHAR(200) DEFAULT '',
            content TEXT DEFAULT '',
            published_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Seed data across different dates — use individual INSERTs with timestamptz casts
    for row_id, title, slug, ts in [
        (1, "Post Jan 2025", "post-jan-2025", "2025-01-15 10:00:00+00"),
        (2, "Post Feb 2025", "post-feb-2025", "2025-02-20 12:00:00+00"),
        (3, "Post Mar 2025", "post-mar-2025", "2025-03-10 08:00:00+00"),
        (4, "Post Jan 2026", "post-jan-2026", "2026-01-05 09:00:00+00"),
        (5, "Post Mar 2026", "post-mar-2026", "2026-03-22 14:00:00+00"),
    ]:
        await db.execute(
            f"INSERT INTO pp_blog_posts (id, title, slug, published_at) "
            f"VALUES ($1, $2, $3, '{ts}'::timestamptz)",
            row_id,
            title,
            slug,
        )
    await db.execute("SELECT setval('pp_blog_posts_id_seq', 10)")
    return db


async def teardown(db):
    await db.execute("DROP TABLE IF EXISTS pp_blog_posts CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# prepopulated_fields tests
# ---------------------------------------------------------------------------


def test_prepopulated_fields_config():
    print("\n--- prepopulated_fields Config ---")

    app = HyperApp(title="PPTest", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        BlogPost,
        list_display=["title", "slug"],
        prepopulated_fields={"slug": ["title"]},
        slug="pp_blog",
    )

    check(
        "config has prepopulated_fields",
        config.prepopulated_fields == {"slug": ["title"]},
    )
    check("prepopulated_fields stored on config", "slug" in config.prepopulated_fields)


def test_prepopulated_fields_template():
    print("\n--- prepopulated_fields Template ---")

    check(
        "TEMPLATE_FORM has prepopulated_fields block",
        "prepopulated_fields" in TEMPLATE_FORM,
    )
    check("TEMPLATE_FORM has slugify function", "slugify" in TEMPLATE_FORM)
    check("TEMPLATE_FORM has input listener", "addEventListener" in TEMPLATE_FORM)


def test_prepopulated_fields_context():
    print("\n--- prepopulated_fields Context ---")

    app = HyperApp(title="PPCtx", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        BlogPost, prepopulated_fields={"slug": ["title"]}, slug="pp_ctx"
    )

    ctx = admin._prepopulated_context(config)
    check("context has prepopulated_fields flag", ctx["prepopulated_fields"] is True)
    check("context has JSON", '"slug"' in ctx["prepopulated_fields_json"])
    check("context JSON has title source", '"title"' in ctx["prepopulated_fields_json"])

    # Empty prepopulated_fields
    app2 = HyperApp(title="PPCtx2", database=DB_URL)
    admin2 = HyperAdmin(app2, require_auth=False)
    config2 = admin2.register(BlogPost, slug="pp_ctx2")
    ctx2 = admin2._prepopulated_context(config2)
    check("empty prepopulated_fields is falsy", not ctx2["prepopulated_fields"])


# ---------------------------------------------------------------------------
# date_hierarchy tests
# ---------------------------------------------------------------------------


def test_date_hierarchy_config():
    print("\n--- date_hierarchy Config ---")

    app = HyperApp(title="DHTest", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        BlogPost,
        list_display=["title", "published_at"],
        date_hierarchy="published_at",
        slug="dh_blog",
    )

    check("config has date_hierarchy", config.date_hierarchy == "published_at")


def test_date_hierarchy_template():
    print("\n--- date_hierarchy Template ---")

    check("TEMPLATE_LIST has date_hierarchy block", "date_hierarchy" in TEMPLATE_LIST)
    check("TEMPLATE_LIST has dh_year link", "dh_year" in TEMPLATE_LIST)
    check("TEMPLATE_LIST has dh_month link", "dh_month" in TEMPLATE_LIST)
    check("TEMPLATE_LIST has All dates link", "All dates" in TEMPLATE_LIST)


async def test_date_hierarchy_context(db):
    print("\n--- date_hierarchy Context (Live DB) ---")

    app = HyperApp(title="DHCtx", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        BlogPost,
        list_display=["title", "published_at"],
        date_hierarchy="published_at",
        slug="dh_ctx",
    )

    # Year level (no filters)
    def MockRequest(query_params=None):
        return make_admin_request(path="/admin/dh_ctx/", query_params=query_params)

    ctx = await admin._build_list_context(config, MockRequest())
    dh = ctx["date_hierarchy"]
    check("year level — has items", dh is not None and len(dh["items"]) > 0)
    check("year level — level is year", dh["level"] == "year")
    years = [item["value"] for item in dh["items"]]
    check("year level — has 2025", 2025 in years)
    check("year level — has 2026", 2026 in years)

    # Month level (year selected)
    ctx2 = await admin._build_list_context(config, MockRequest({"dh_year": "2025"}))
    dh2 = ctx2["date_hierarchy"]
    check("month level — level is month", dh2["level"] == "month")
    check("month level — year is 2025", dh2["year"] == 2025)
    months = [item["value"] for item in dh2["items"]]
    check("month level — has Jan", 1 in months)
    check("month level — has Feb", 2 in months)
    check("month level — has Mar", 3 in months)

    # Day level (year + month selected)
    ctx3 = await admin._build_list_context(
        config, MockRequest({"dh_year": "2025", "dh_month": "1"})
    )
    dh3 = ctx3["date_hierarchy"]
    check("day level — level is day", dh3["level"] == "day")
    check("day level — has items", len(dh3["items"]) > 0)
    days = [item["value"] for item in dh3["items"]]
    check("day level — has 15th", 15 in days)

    # No date_hierarchy configured
    app2 = HyperApp(title="NoDH", database=DB_URL)
    admin2 = HyperAdmin(app2, require_auth=False)
    config2 = admin2.register(BlogPost, slug="no_dh")
    ctx4 = await admin2._build_list_context(config2, MockRequest())
    check("no date_hierarchy — is None", ctx4["date_hierarchy"] is None)


async def test_date_hierarchy_filtering(db):
    print("\n--- date_hierarchy Filtering (Live DB) ---")

    app = HyperApp(title="DHFilter", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        BlogPost,
        list_display=["title", "published_at"],
        date_hierarchy="published_at",
        slug="dh_filter",
    )

    class MockRequest:
        def __init__(self, query_params=None):
            self.GET = query_params or {}
            self.path = "/admin/dh_filter/"
            self.cookies = {}
            self._admin_user = {
                "username": "admin",
                "is_staff": True,
                "is_superuser": True,
            }

    # All posts
    ctx_all = await admin._build_list_context(config, MockRequest())
    check("all posts — 5 total", ctx_all["total"] == 5)

    # Filter to 2025 only
    ctx_2025 = await admin._build_list_context(config, MockRequest({"dh_year": "2025"}))
    check("2025 filter — 3 posts", ctx_2025["total"] == 3, f"got {ctx_2025['total']}")

    # Filter to 2026 only
    ctx_2026 = await admin._build_list_context(config, MockRequest({"dh_year": "2026"}))
    check("2026 filter — 2 posts", ctx_2026["total"] == 2, f"got {ctx_2026['total']}")

    # Filter to Jan 2025
    ctx_jan25 = await admin._build_list_context(
        config, MockRequest({"dh_year": "2025", "dh_month": "1"})
    )
    check(
        "Jan 2025 filter — 1 post", ctx_jan25["total"] == 1, f"got {ctx_jan25['total']}"
    )

    # Filter to specific day
    ctx_day = await admin._build_list_context(
        config, MockRequest({"dh_year": "2025", "dh_month": "1", "dh_day": "15"})
    )
    check(
        "Jan 15 2025 filter — 1 post", ctx_day["total"] == 1, f"got {ctx_day['total']}"
    )

    # Filter to non-existent date
    ctx_empty = await admin._build_list_context(
        config, MockRequest({"dh_year": "2020"})
    )
    check("2020 filter — 0 posts", ctx_empty["total"] == 0)


if __name__ == "__main__":
    sys.exit(main())
