"""
HyperNews — Database setup and seed script.

Uses the framework's model registry to auto-create tables from Model definitions,
then seeds initial data using the ORM.

Usage:
    uv run hyper setup --app services.hypernews.app:app
    uv run hyper setup --app services.hypernews.app:app --seed services.hypernews.setup:seed
    uv run hyper setup --app services.hypernews.app:app --drop  # DESTRUCTIVE
"""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.database import get_db
from hyperdjango.logging import logger

from .models import Comment, Forum, ForumMember, ForumRole, Post, User


async def _ensure_forums(admin_id: int) -> dict[str, int]:
    """Create default forums. Returns {name: id} mapping."""
    forums_spec = [
        (
            "general",
            "General",
            "The default community for all topics.",
            "Be respectful. Stay on topic.",
        ),
        (
            "python",
            "Python Programming",
            "All things Python — libraries, frameworks, tips, and news.",
            "Tag questions with relevant library names.",
        ),
        (
            "rust",
            "Rust Language",
            "Systems programming with Rust — safety, performance, and community.",
            "Include playground links for code questions.",
        ),
        (
            "startups",
            "Startups & Founders",
            "Founder stories, fundraising, growth tactics, and startup culture.",
            "No recruiting posts. Use the jobs board instead.",
        ),
        (
            "webdev",
            "Web Development",
            "Frontend, backend, full-stack — HTML, CSS, JS, frameworks, and tools.",
            "Include browser/version for bug reports.",
        ),
        (
            "databases",
            "Databases",
            "PostgreSQL, MySQL, SQLite, NoSQL — schema design, optimization, migrations.",
            "Include EXPLAIN output for performance questions.",
        ),
        (
            "devops",
            "DevOps & Infrastructure",
            "CI/CD, containers, cloud, monitoring, and deployment.",
            "Specify cloud provider when relevant.",
        ),
        (
            "showhn",
            "Show HN",
            "Show off your projects, tools, and experiments to the community.",
            "Include a demo link or screenshots.",
        ),
    ]

    forum_ids: dict[str, int] = {}
    for name, title, description, rules in forums_spec:
        existing = await Forum.objects.filter(name=name).first()
        if existing:
            forum_ids[name] = existing.id
            continue
        f = Forum(
            name=name,
            title=title,
            description=description,
            rules=rules,
            created_by=admin_id,
        )
        await f.save()
        forum_ids[name] = f.id
        # Creator is auto-admin
        await ForumMember(
            forum_id=f.id,
            user_id=admin_id,
            role=ForumRole.ADMIN,
        ).save()
    return forum_ids


async def seed(db) -> None:
    """Seed initial data using the ORM."""
    logger.info("\nSeeding data...")

    # Admin user (password from HYPER_SEED_PASSWORD or random + printed)
    existing_admin = await User.objects.filter(username="admin").first()
    if not existing_admin:
        admin = User(
            username="admin",
            email="admin@hypernews.local",
            password_hash=hash_password(seed_password("admin")),
            display_name="Admin",
            karma=1000,
        )
        await admin.save()
        await admin.set_status("access", "staff", reason="Initial admin setup")
        logger.info(
            "  admin user (staff via timeline) — see seed_password log for actual value"
        )
    else:
        logger.info("  admin user already exists")

    # Regular users (password from HYPER_SEED_PASSWORD_<NAME> or random + printed)
    for uname, email, display, karma in [
        ("alice", "alice@example.com", "Alice", 42),
        ("bob", "bob@example.com", "Bob", 17),
    ]:
        existing = await User.objects.filter(username=uname).first()
        if not existing:
            u = User(
                username=uname,
                email=email,
                password_hash=hash_password(seed_password(uname)),
                display_name=display,
                karma=karma,
            )
            await u.save()

    logger.info("  sample users (alice, bob) — see seed_password log for actual values")

    # Forums
    admin_user = await User.objects.filter(username="admin").first()
    aid = admin_user.id if admin_user else 1
    forum_ids = await _ensure_forums(aid)
    logger.info(f"  {len(forum_ids)} forums created")

    # Subscribe alice and bob to some forums
    alice = await User.objects.filter(username="alice").first()
    bob = await User.objects.filter(username="bob").first()
    if alice:
        for fname in ("general", "python", "webdev"):
            fid = forum_ids.get(fname, 0)
            if fid:
                existing = await ForumMember.objects.filter(
                    forum_id=fid, user_id=alice.id
                ).first()
                if not existing:
                    await ForumMember(
                        forum_id=fid, user_id=alice.id, role=ForumRole.SUBSCRIBER
                    ).save()
        # Alice is a mod in python
        python_fid = forum_ids.get("python", 0)
        if python_fid:
            await ForumMember.objects.filter(
                forum_id=python_fid, user_id=alice.id
            ).update(role=ForumRole.MODERATOR.value)
    if bob:
        for fname in ("general", "rust", "databases"):
            fid = forum_ids.get(fname, 0)
            if fid:
                existing = await ForumMember.objects.filter(
                    forum_id=fid, user_id=bob.id
                ).first()
                if not existing:
                    await ForumMember(
                        forum_id=fid, user_id=bob.id, role=ForumRole.SUBSCRIBER
                    ).save()

    # Update subscriber counts
    actual_db = get_db()
    await actual_db.execute(
        """UPDATE hn_forums SET subscriber_count = (
            SELECT COUNT(*) FROM hn_forum_members WHERE forum_id = hn_forums.id
        )"""
    )

    # Sample posts (assigned to forums)
    existing_posts = await Post.objects.count()
    if existing_posts == 0:
        general_fid = forum_ids.get("general", 0)
        python_fid = forum_ids.get("python", 0)
        webdev_fid = forum_ids.get("webdev", 0)
        showhn_fid = forum_ids.get("showhn", 0)
        databases_fid = forum_ids.get("databases", 0)

        sample_posts = [
            (
                "HyperDjango: Django Extended with Native Zig Performance",
                "hyperdjango-native-zig",
                "https://github.com/hyperdjango/hyperdjango",
                "",
                False,
                False,
                general_fid,
            ),
            (
                "Ask HN: What's your favorite PostgreSQL extension?",
                "ask-favorite-postgres-extension",
                "",
                "I've been exploring pg_trgm and pgvector. What extensions do you use in production?",
                True,
                False,
                databases_fid,
            ),
            (
                "Show HN: Built a real-time dashboard with SSE streaming",
                "show-realtime-dashboard-sse",
                "",
                "Used HyperDjango's Response.sse() to stream live metrics. No WebSocket complexity needed.",
                False,
                True,
                showhn_fid,
            ),
            (
                "Why We Moved from Microservices Back to a Monolith",
                "microservices-back-to-monolith",
                "https://example.com/monolith-post",
                "",
                False,
                False,
                webdev_fid,
            ),
            (
                "Python 3.14 Free-Threading: Real-World Performance Results",
                "python-314-free-threading-results",
                "https://example.com/free-threading",
                "",
                False,
                False,
                python_fid,
            ),
        ]
        for title, slug, url, text, is_ask, is_show, forum_id in sample_posts:
            p = Post(
                title=title,
                slug=slug,
                url=url,
                text=text,
                author_id=aid,
                score=10 + len(title) % 50,
                is_ask=is_ask,
                is_show=is_show,
                forum_id=forum_id,
            )
            await p.save()
        logger.info("  sample posts")

        # Update forum post counts
        await actual_db.execute(
            """UPDATE hn_forums SET post_count = (
                SELECT COUNT(*) FROM hn_posts WHERE forum_id = hn_forums.id AND NOT is_deleted
            )"""
        )

        # Sample comments
        first_post = await Post.objects.order_by("id").first()
        if first_post and alice and bob:
            c1 = Comment(
                post_id=first_post.id,
                author_id=alice.id,
                text="This is amazing! The Zig integration really pays off in benchmarks.",
                score=5,
            )
            await c1.save()
            c2 = Comment(
                post_id=first_post.id,
                author_id=bob.id,
                text="How does it compare to uvicorn + asyncpg for raw throughput?",
                score=3,
            )
            await c2.save()
            await Post.objects.filter(id=first_post.id).update(comment_count=2)
            logger.info("  sample comments")
    else:
        logger.info("  posts already exist, skipping seed")

    logger.success("Seed complete!")
