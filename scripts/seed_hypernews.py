"""
Seed HyperNews with realistic data for benchmarking and demo.

Creates 100 users, 500 posts, 2000 comments, 5000 votes.
Idempotent — safe to re-run (uses ON CONFLICT DO NOTHING).
"""

import asyncio
import os
import random

from hyperdjango.auth import hash_password
from hyperdjango.database import Database

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hypernews")

# Realistic post titles
TITLES = [
    "Show HN: {thing} – a {adj} {topic} tool",
    "Ask HN: {question}?",
    "{thing} is {adj} and here's why",
    "Why I switched from {thing} to {thing2}",
    "Lessons learned building {thing} at scale",
    "{thing}: A {adj} approach to {topic}",
    "The {adj} guide to {topic}",
    "{company} releases {thing} {version}",
    "How {thing} changed the way I think about {topic}",
    "Building a {adj} {thing} in {lang}",
]

THINGS = [
    "Zig",
    "Rust",
    "Go",
    "HyperDjango",
    "PostgreSQL",
    "SQLite",
    "WebAssembly",
    "HTMX",
    "Alpine.js",
    "Tailwind",
    "Bun",
    "Deno",
    "Kubernetes",
    "Docker",
    "Nix",
    "Neovim",
    "VS Code",
    "Linux",
    "FreeBSD",
    "ARM",
    "RISC-V",
    "SIMD",
    "WASM",
]
THINGS2 = [
    "Django",
    "Flask",
    "FastAPI",
    "Express",
    "Rails",
    "Spring",
    "Next.js",
    "Svelte",
]
ADJS = [
    "fast",
    "simple",
    "modern",
    "lightweight",
    "production-ready",
    "blazing-fast",
    "zero-dependency",
    "type-safe",
    "memory-safe",
    "concurrent",
    "distributed",
]
TOPICS = [
    "web development",
    "systems programming",
    "database design",
    "API design",
    "deployment",
    "observability",
    "security",
    "performance",
    "testing",
    "caching",
]
QUESTIONS = [
    "What's your favorite tool for",
    "How do you handle",
    "Best practices for",
    "Anyone using",
    "Thoughts on",
    "How to learn",
]
COMPANIES = ["Google", "Cloudflare", "Vercel", "Supabase", "PlanetScale", "Fly.io"]
LANGS = ["Zig", "Rust", "Go", "Python", "TypeScript", "C"]
VERSIONS = ["2.0", "3.0", "4.0", "1.0-beta", "5.0-rc1"]
URLS = [
    "https://github.com/",
    "https://blog.example.com/",
    "https://news.ycombinator.com/",
    "https://arxiv.org/abs/",
    "https://docs.example.com/",
    "",
]

COMMENT_TEXTS = [
    "Great work! I've been looking for something like this.",
    "How does this compare to {thing} in terms of performance?",
    "Interesting approach. Have you considered using {thing} instead?",
    "We've been using this in production for 6 months, very stable.",
    "The benchmarks look impressive but I'd like to see real-world numbers.",
    "This is exactly what the ecosystem needed. Thank you.",
    "I tried this last week and it's surprisingly easy to set up.",
    "Minor nit: the documentation could use more examples for {topic}.",
    "Does this support {thing}? That's a dealbreaker for us.",
    "Just deployed this to our staging environment. So far so good.",
    "The API design is clean. Very Django-like in the best way.",
    "I love the approach but the error messages could be more helpful.",
    "This reminds me of {thing} but with much better defaults.",
    "Any plans for {thing} support? Would be killer.",
    "We migrated from {thing2} to this and saw 3x throughput improvement.",
]


def random_title() -> str:
    template = random.choice(TITLES)
    return template.format(
        thing=random.choice(THINGS),
        thing2=random.choice(THINGS2),
        adj=random.choice(ADJS),
        topic=random.choice(TOPICS),
        question=random.choice(QUESTIONS) + " " + random.choice(TOPICS),
        company=random.choice(COMPANIES),
        version=random.choice(VERSIONS),
        lang=random.choice(LANGS),
    )


def random_comment() -> str:
    template = random.choice(COMMENT_TEXTS)
    return template.format(
        thing=random.choice(THINGS),
        thing2=random.choice(THINGS2),
        topic=random.choice(TOPICS),
    )


def slugify(title: str) -> str:
    import re
    import unicodedata

    slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^\w\s-]", "", slug).strip().lower()
    return re.sub(r"[-\s]+", "-", slug)[:200]


async def main() -> None:
    db = Database(DATABASE_URL)
    await db.connect()
    random.seed(42)  # Reproducible

    # 1. Create 100 users
    print("Creating 100 users...")
    pw_hash = hash_password("password")  # Same password for all demo users
    admin_hash = hash_password("admin")
    for i in range(100):
        karma = random.randint(0, 5000)
        is_staff = i < 3
        await db.execute(
            "INSERT INTO hn_users (username, email, password_hash, karma, is_staff, created_at) "
            "VALUES ($1, $2, $3, $4, $5, NOW() - interval '1 day' * $6) "
            "ON CONFLICT (username) DO NOTHING",
            f"user{i:03d}",
            f"user{i:03d}@example.com",
            admin_hash if is_staff else pw_hash,
            karma,
            is_staff,
            random.randint(1, 365),
        )
    print("  100 users ✓")

    # 2. Create 500 posts
    print("Creating 500 posts...")
    for i in range(500):
        title = random_title()
        slug = slugify(title) + f"-{i}"
        author_id = random.randint(1, 100)
        score = random.randint(-5, 500)
        has_url = random.random() > 0.3
        url = random.choice(URLS) + slug if has_url else ""
        text = (
            ""
            if has_url
            else f"This is a text post about {random.choice(TOPICS)}. "
            * random.randint(2, 10)
        )
        is_ask = not has_url and random.random() > 0.7
        days_ago = random.randint(0, 180)

        await db.execute(
            "INSERT INTO hn_posts (title, slug, url, text, author_id, score, comment_count, "
            "is_ask, is_deleted, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, 0, $7, false, NOW() - interval '1 day' * $8) "
            "ON CONFLICT DO NOTHING",
            title,
            slug,
            url,
            text,
            author_id,
            score,
            is_ask,
            days_ago,
        )
    print("  500 posts ✓")

    # 3. Create 2000 comments (threaded)
    print("Creating 2000 comments...")
    # Get actual post IDs
    post_rows = await db.query("SELECT id FROM hn_posts ORDER BY id LIMIT 500")
    post_ids = [r["id"] for r in post_rows]

    comment_ids_by_post: dict[int, list[int]] = {}
    for i in range(2000):
        post_id = random.choice(post_ids)
        author_id = random.randint(1, 100)
        text = random_comment()

        # 30% chance of being a reply to an existing comment
        parent_id = 0
        depth = 0
        existing = comment_ids_by_post.get(post_id, [])
        if existing and random.random() < 0.3:
            parent_id = random.choice(existing)
            depth = min(random.randint(1, 5), 8)

        days_ago = random.randint(0, 180)
        rows = await db.query(
            "INSERT INTO hn_comments (post_id, author_id, parent_id, depth, text, score, "
            "is_deleted, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, false, NOW() - interval '1 day' * $7) "
            "RETURNING id",
            post_id,
            author_id,
            parent_id,
            depth,
            text,
            random.randint(-2, 50),
            days_ago,
        )
        if rows:
            comment_ids_by_post.setdefault(post_id, []).append(rows[0]["id"])
    print("  2000 comments ✓")

    # 4. Update comment counts on posts
    print("Updating comment counts...")
    await db.execute("""
        UPDATE hn_posts SET comment_count = (
            SELECT COUNT(*) FROM hn_comments
            WHERE hn_comments.post_id = hn_posts.id AND NOT hn_comments.is_deleted
        )
    """)
    print("  comment counts ✓")

    # 5. Create 5000 votes
    print("Creating 5000 votes...")
    for i in range(5000):
        user_id = random.randint(1, 100)
        value = random.choice([1, 1, 1, -1])  # 75% upvotes

        if random.random() > 0.3:
            # Vote on post
            post_id = random.choice(post_ids)
            await db.execute(
                "INSERT INTO hn_votes (user_id, post_id, comment_id, value, created_at) "
                "VALUES ($1, $2, 0, $3, NOW() - interval '1 day' * $4) "
                "ON CONFLICT DO NOTHING",
                user_id,
                post_id,
                value,
                random.randint(0, 180),
            )
        else:
            # Vote on comment
            all_comment_ids = [
                cid for ids in comment_ids_by_post.values() for cid in ids
            ]
            if all_comment_ids:
                comment_id = random.choice(all_comment_ids)
                await db.execute(
                    "INSERT INTO hn_votes (user_id, post_id, comment_id, value, created_at) "
                    "VALUES ($1, 0, $2, $3, NOW() - interval '1 day' * $4) "
                    "ON CONFLICT DO NOTHING",
                    user_id,
                    comment_id,
                    value,
                    random.randint(0, 180),
                )
    print("  5000 votes ✓")

    # 6. ANALYZE tables for query planner
    print("Running ANALYZE...")
    for table in [
        "hn_users",
        "hn_posts",
        "hn_comments",
        "hn_votes",
        "hn_admin_messages",
        "hn_spam_reports",
    ]:
        await db.execute(f"ANALYZE {table}")
    print("  ANALYZE ✓")

    await db.disconnect()

    # Report
    print("\nSeed complete!")
    print("  Users: 100")
    print("  Posts: 500")
    print("  Comments: 2000")
    print("  Votes: 5000")


if __name__ == "__main__":
    asyncio.run(main())
