"""Seed data for the Content Hub example."""

import random

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.content_hub.app import (
    Article,
    Content,
    ContentStatus,
    ContentType,
    Link,
    Role,
    Tag,
    User,
    UserProfile,
    Video,
)


async def run(db=None) -> None:
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="admin").first()
    if existing:
        logger.info("  hub_users: already seeded")
        return

    # Create users with different roles
    admin = User(
        username="admin",
        password_hash=hash_password(seed_password("admin")),
        role=Role.ADMIN,
    )
    await admin.save()
    editor = User(
        username="editor",
        password_hash=hash_password(seed_password("editor")),
        role=Role.EDITOR,
    )
    await editor.save()
    reader = User(
        username="reader",
        password_hash=hash_password(seed_password("reader")),
        role=Role.READER,
    )
    await reader.save()
    logger.info("  hub_users: 3 users created (admin/editor/reader)")

    # Profiles (OneToOneField)
    for user, display, bio, website in [
        (admin, "Admin User", "Platform administrator", "https://example.com"),
        (editor, "Content Editor", "Writes articles and curates content", ""),
        (reader, "Regular Reader", "Enjoys reading tech content", ""),
    ]:
        profile = UserProfile(
            user_id=user.id, display_name=display, bio=bio, website=website
        )
        await profile.save()
    logger.info("  hub_profiles: 3 profiles created")

    # Articles (STI type=article) — use raw SQL for random timestamps and view counts
    rng = random.Random(42)
    articles = [
        (
            "Getting Started with HyperDjango",
            "getting-started-hyperdjango",
            "A comprehensive guide to building web apps with HyperDjango and native Zig performance.",
            ContentStatus.PUBLISHED,
            True,
            8,
        ),
        (
            "PostgreSQL Query Optimization",
            "postgres-query-optimization",
            "Learn how to write efficient queries with proper indexing and EXPLAIN ANALYZE.",
            ContentStatus.PUBLISHED,
            False,
            12,
        ),
        (
            "Understanding Async Python",
            "understanding-async-python",
            "Deep dive into asyncio, event loops, and concurrent programming patterns.",
            ContentStatus.PUBLISHED,
            True,
            15,
        ),
        (
            "Draft Article on Security",
            "draft-security",
            "Work in progress security guide.",
            ContentStatus.DRAFT,
            False,
            0,
        ),
        (
            "Archived: Old Tutorial",
            "archived-old-tutorial",
            "This tutorial is outdated.",
            ContentStatus.ARCHIVED,
            False,
            5,
        ),
        (
            "Building REST APIs",
            "building-rest-apis",
            "How to build production REST APIs with ModelViewSet and CursorPagination.",
            ContentStatus.PUBLISHED,
            False,
            10,
        ),
        (
            "SIMD Validation Deep Dive",
            "simd-validation",
            "How HyperDjango validates 13M models/sec using SIMD batch processing.",
            ContentStatus.PUBLISHED,
            True,
            20,
        ),
    ]
    for title, slug, body, status, featured, reading_time in articles:
        a = Article(
            title=title,
            slug=slug,
            body=body,
            type=ContentType.ARTICLE,
            status=status,
            author_id=editor.id,
            featured=featured,
            reading_time_mins=reading_time,
            view_count=rng.randint(0, 500),
        )
        await a.save()
    logger.info("  hub_contents: {n} articles created", n=len(articles))

    # Videos (STI type=video)
    videos = [
        (
            "HyperDjango Tutorial Video",
            "hyperdjango-tutorial-video",
            "Video walkthrough of building your first app.",
            ContentStatus.PUBLISHED,
            "https://example.com/videos/tutorial.mp4",
            1200,
        ),
        (
            "Live Coding Session",
            "live-coding-session",
            "Building a real-time chat app with WebSockets.",
            ContentStatus.PUBLISHED,
            "https://example.com/videos/livecoding.mp4",
            3600,
        ),
        (
            "Draft: Performance Talk",
            "draft-perf-talk",
            "Upcoming conference talk on Zig performance.",
            ContentStatus.DRAFT,
            "https://example.com/videos/perf.mp4",
            2400,
        ),
    ]
    for title, slug, body, status, video_url, duration in videos:
        v = Video(
            title=title,
            slug=slug,
            body=body,
            type=ContentType.VIDEO,
            status=status,
            author_id=admin.id,
            video_url=video_url,
            duration_secs=duration,
            view_count=rng.randint(0, 200),
        )
        await v.save()
    logger.info("  hub_contents: {n} videos created", n=len(videos))

    # Links (STI type=link)
    links = [
        (
            "PostgreSQL Documentation",
            "pg-docs",
            "Official PostgreSQL docs.",
            ContentStatus.PUBLISHED,
            "https://www.postgresql.org/docs/",
        ),
        (
            "Zig Language",
            "zig-lang",
            "The Zig programming language homepage.",
            ContentStatus.PUBLISHED,
            "https://ziglang.org/",
        ),
        (
            "Python Asyncio Docs",
            "python-asyncio",
            "Python standard library asyncio reference.",
            ContentStatus.PUBLISHED,
            "https://docs.python.org/3/library/asyncio.html",
        ),
    ]
    for title, slug, body, status, url in links:
        lnk = Link(
            title=title,
            slug=slug,
            body=body,
            type=ContentType.LINK,
            status=status,
            author_id=admin.id,
            external_url=url,
            view_count=rng.randint(0, 100),
        )
        await lnk.save()
    logger.info("  hub_contents: {n} links created", n=len(links))

    # Tags
    tags = [
        "python",
        "zig",
        "postgresql",
        "web",
        "performance",
        "tutorial",
        "api",
        "async",
    ]
    for tag_name in tags:
        t = Tag(name=tag_name)
        await t.save()
    logger.info("  hub_tags: {n} tags created", n=len(tags))

    # Admin user in hyper_users (for HyperAdmin panel login) via RBAC system
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("  hyper_users: admin ensured for HyperAdmin panel")

    total = await Content.objects.count()
    logger.success("  Content Hub seeded: {total} content items", total=total)
