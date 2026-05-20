"""Seed data for HyperNews — users, forums, posts, comments, votes."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.hypernews.models import (
    Comment,
    Forum,
    ForumMember,
    ForumRole,
    Post,
    PostStatus,
    User,
    UserProfile,
    Vote,
)
from services.hypernews.voting import TrustTier


async def run(db=None) -> None:
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="alice").first()
    if existing:
        logger.info("  hypernews: already seeded")
        return

    # ── Trust Tiers ──────────────────────────────────────────

    tiers = [
        TrustTier(
            name="New User",
            min_karma=0,
            max_karma=99,
            can_downvote=False,
            can_flag=False,
            vote_weight=1.0,
        ),
        TrustTier(
            name="Regular",
            min_karma=100,
            max_karma=499,
            can_downvote=True,
            can_flag=False,
            vote_weight=1.0,
        ),
        TrustTier(
            name="Trusted",
            min_karma=500,
            max_karma=1999,
            can_downvote=True,
            can_flag=True,
            vote_weight=1.5,
        ),
        TrustTier(
            name="Veteran",
            min_karma=2000,
            max_karma=99999,
            can_downvote=True,
            can_flag=True,
            vote_weight=2.0,
        ),
    ]
    for t in tiers:
        await t.save()
    logger.info("  hn_trust_tiers: {n} tiers created", n=len(tiers))

    # ── Users ────────────────────────────────────────────────

    users_data = [
        ("alice", "alice@example.com", 1250),
        ("bob", "bob@example.com", 50),
        ("carol", "carol@example.com", 3200),
        ("dave", "dave@example.com", 780),
        ("eve", "eve@example.com", 25),
        ("frank", "frank@example.com", 420),
        ("grace", "grace@example.com", 150),
        ("admin", "admin@example.com", 9999),
    ]
    users = {}
    for username, email, karma in users_data:
        u = User(
            username=username,
            email=email,
            password_hash=hash_password(seed_password(username)),
            karma=karma,
        )
        await u.save()
        users[username] = u
    logger.info("  hn_users: {n} users created", n=len(users))

    # ── User Profiles ────────────────────────────────────────

    profiles = [
        (
            users["alice"],
            "Full-stack developer. Python & Zig enthusiast.",
            "https://alice.dev",
        ),
        (users["carol"], "Community moderator. Former Django core contributor.", ""),
        (users["admin"], "Site administrator.", ""),
    ]
    for user, bio, website in profiles:
        p = UserProfile(user_id=user.id, bio=bio, website=website)
        await p.save()
    logger.info("  hn_user_profiles: {n} profiles created", n=len(profiles))

    # ── Forums ───────────────────────────────────────────────

    forums_data = [
        ("general", "General Discussion", "General discussion about anything", True),
        ("show-hn", "Show HN", "Share something you've made", True),
        ("ask-hn", "Ask HN", "Ask the community a question", True),
        ("python", "Python", "Python programming language", True),
        ("zig", "Zig", "Zig programming language", True),
        ("staff-only", "Staff Only", "Internal staff discussions", False),
    ]
    forums = {}
    for name, title, description, is_public in forums_data:
        f = Forum(
            name=name,
            title=title,
            description=description,
            is_public=is_public,
            created_by=users["admin"].id,
        )
        await f.save()
        forums[name] = f
    logger.info("  hn_forums: {n} forums created", n=len(forums))

    # ── Forum Members ────────────────────────────────────────

    # All users join General, Show HN, Ask HN
    public_forums = [forums["general"], forums["show-hn"], forums["ask-hn"]]
    for user in users.values():
        for forum in public_forums:
            fm = ForumMember(
                forum_id=forum.id, user_id=user.id, role=ForumRole.SUBSCRIBER
            )
            await fm.save()

    # Mods for specific forums — update role if already a subscriber
    mod_assignments = [
        (users["carol"], forums["general"], ForumRole.MODERATOR),
        (users["alice"], forums["python"], ForumRole.MODERATOR),
        (users["grace"], forums["zig"], ForumRole.MODERATOR),
        (users["admin"], forums["staff-only"], ForumRole.ADMIN),
        (users["carol"], forums["staff-only"], ForumRole.MODERATOR),
    ]
    for user, forum, role in mod_assignments:
        existing = await ForumMember.objects.filter(
            forum_id=forum.id, user_id=user.id
        ).first()
        if existing:
            await ForumMember.objects.filter(id=existing.id).update(role=role)
        else:
            fm = ForumMember(forum_id=forum.id, user_id=user.id, role=role)
            await fm.save()
    logger.info("  hn_forum_members: memberships created")

    # ── Posts ────────────────────────────────────────────────

    posts_data = [
        (
            "HyperDjango: Django with Native Zig Performance",
            "https://hyperdjango.dev",
            users["alice"],
            forums["general"],
            142,
            PostStatus.PUBLISHED,
        ),
        (
            "Show HN: I built a real-time chat with Zig WebSockets",
            "",
            users["alice"],
            forums["show-hn"],
            87,
            PostStatus.PUBLISHED,
        ),
        (
            "Ask HN: What's your experience with free-threaded Python?",
            "",
            users["bob"],
            forums["ask-hn"],
            64,
            PostStatus.PUBLISHED,
        ),
        (
            "Zig 0.15 Release Notes",
            "https://ziglang.org/release-notes",
            users["dave"],
            forums["zig"],
            95,
            PostStatus.PUBLISHED,
        ),
        (
            "PEP 703: Making the GIL Optional - Accepted!",
            "https://peps.python.org/pep-0703/",
            users["carol"],
            forums["python"],
            231,
            PostStatus.PUBLISHED,
        ),
        (
            "How to profile Python + Zig FFI code",
            "",
            users["frank"],
            forums["python"],
            38,
            PostStatus.PUBLISHED,
        ),
        (
            "The case for SIMD in web frameworks",
            "",
            users["grace"],
            forums["general"],
            56,
            PostStatus.PUBLISHED,
        ),
        (
            "Show HN: PostgreSQL UNLOGGED tables for cache",
            "",
            users["dave"],
            forums["show-hn"],
            23,
            PostStatus.PUBLISHED,
        ),
        (
            "Ask HN: Best practices for multi-tenant SaaS?",
            "",
            users["eve"],
            forums["ask-hn"],
            41,
            PostStatus.PUBLISHED,
        ),
        (
            "Template engines: Jinja2 vs compiled Zig templates",
            "",
            users["alice"],
            forums["general"],
            67,
            PostStatus.PUBLISHED,
        ),
    ]
    posts = []
    for i, (title, url, author, forum, score, status) in enumerate(posts_data):
        p = Post(
            title=title,
            slug=title.lower()
            .replace(" ", "-")
            .replace(":", "")
            .replace("!", "")
            .replace("?", "")
            .replace("'", "")[:50],
            url=url,
            text="" if url else f"Discussion about: {title}",
            author_id=author.id,
            forum_id=forum.id,
            score=score,
            status=status,
        )
        await p.save()
        posts.append(p)
    logger.info("  hn_posts: {n} posts created", n=len(posts))

    # ── Comments ─────────────────────────────────────────────

    comments_data = [
        # (post_index, author, text, parent_comment_index_or_None, score)
        (0, users["bob"], "This looks amazing! How does the Zig FFI work?", None, 12),
        (
            0,
            users["alice"],
            "It uses PyMethodDef C FFI — Zig compiles to .so and Python loads it directly.",
            0,
            8,
        ),
        (
            0,
            users["carol"],
            "I've been using it in production for 3 months. Rock solid.",
            None,
            15,
        ),
        (0, users["dave"], "What's the migration story from Django?", None, 5),
        (
            2,
            users["carol"],
            "Free-threaded Python is a game changer for I/O-bound workloads.",
            None,
            18,
        ),
        (
            2,
            users["alice"],
            "We run it on 3.14t with all our services. No GIL issues.",
            None,
            11,
        ),
        (
            2,
            users["frank"],
            "Still waiting for C extension ecosystem to catch up.",
            None,
            7,
        ),
        (
            4,
            users["alice"],
            "This is huge for frameworks like HyperDjango that use native extensions.",
            None,
            22,
        ),
        (
            4,
            users["bob"],
            "Does this mean we can finally have true concurrent request handling?",
            None,
            9,
        ),
        (
            4,
            users["grace"],
            "Combined with Zig's thread pool, it's incredibly fast.",
            1,
            13,
        ),
        (
            3,
            users["alice"],
            "The comptime features in 0.15 are brilliant for code generation.",
            None,
            10,
        ),
        (
            5,
            users["carol"],
            "cProfile first, then fix. Never optimize without profiling.",
            None,
            14,
        ),
        (
            6,
            users["dave"],
            "SIMD striptags is 500μs in release mode. Impressive.",
            None,
            8,
        ),
        (9, users["frank"], "Zig templates compile 220x faster than Jinja2.", None, 6),
    ]
    comments = []
    for post_idx, author, text, parent_idx, score in comments_data:
        parent_id = comments[parent_idx].id if parent_idx is not None else 0
        depth = 1 if parent_idx is not None else 0
        c = Comment(
            post_id=posts[post_idx].id,
            author_id=author.id,
            text=text,
            parent_id=parent_id,
            depth=depth,
            score=score,
        )
        await c.save()
        comments.append(c)
    logger.info("  hn_comments: {n} comments created", n=len(comments))

    # ── Votes ────────────────────────────────────────────────

    vote_count = 0
    for post in posts:
        # Each post gets a few upvotes from random users
        voters = [u for u in users.values() if u.id != post.author_id][:4]
        for voter in voters:
            v = Vote(
                user_id=voter.id,
                target_type="post",
                target_id=post.id,
                value=1,
            )
            await v.save()
            vote_count += 1

    for comment in comments[:8]:
        voters = [u for u in users.values() if u.id != comment.author_id][:2]
        for voter in voters:
            v = Vote(
                user_id=voter.id,
                target_type="comment",
                target_id=comment.id,
                value=1,
            )
            await v.save()
            vote_count += 1
    logger.info("  hn_votes: {n} votes created", n=vote_count)

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info(
        "  hypernews: seed complete — {u} users, {f} forums, {p} posts, {c} comments",
        u=len(users),
        f=len(forums),
        p=len(posts),
        c=len(comments),
    )
