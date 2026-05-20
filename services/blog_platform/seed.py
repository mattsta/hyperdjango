"""Seed data for blog_platform service."""

from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.database import Database

from .app import Author, Category, Post, PostCategory


async def run(db: Database) -> None:
    # Admin user for HyperAdmin
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()

    # Authors
    authors = []
    for name, bio, lang in [
        ("Alice Martin", "Tech writer and Python enthusiast", "en"),
        ("Pierre Dupont", "Journaliste et blogueur francophone", "fr"),
        ("Bob Chen", "Full-stack developer and open source contributor", "en"),
    ]:
        a = Author(name=name, bio=bio, language=lang)
        await a.save(db=db)
        authors.append(a)

    # Categories
    categories = []
    for name, slug in [
        ("Technology", "technology"),
        ("Python", "python"),
        ("Web Development", "web-development"),
        ("DevOps", "devops"),
    ]:
        c = Category(name=name, slug=slug)
        await c.save(db=db)
        categories.append(c)

    # Posts
    posts_data = [
        {
            "title": "Getting Started with HyperDjango",
            "slug": "getting-started-hyperdjango",
            "excerpt": "Learn how to build fast web apps with HyperDjango's native Zig performance.",
            "body": "HyperDjango combines Django's ergonomics with native Zig speed...",
            "author_id": authors[0].id,
            "published": True,
            "published_at": "2026-04-01T10:00:00Z",
            "language": "en",
        },
        {
            "title": "Premiers pas avec HyperDjango",
            "slug": "premiers-pas-hyperdjango",
            "excerpt": "Apprenez a construire des applications web rapides avec HyperDjango.",
            "body": "HyperDjango combine l'ergonomie de Django avec la vitesse native de Zig...",
            "author_id": authors[1].id,
            "published": True,
            "published_at": "2026-04-02T14:00:00Z",
            "language": "fr",
        },
        {
            "title": "Building REST APIs with ViewSets",
            "slug": "building-rest-apis-viewsets",
            "excerpt": "A deep dive into HyperDjango's REST framework with ViewSets and serializers.",
            "body": "The REST framework provides ModelSerializer, ModelViewSet, and APIRouter...",
            "author_id": authors[2].id,
            "published": True,
            "published_at": "2026-04-05T09:00:00Z",
            "language": "en",
        },
        {
            "title": "Native Zig Performance in Python",
            "slug": "native-zig-performance",
            "excerpt": "How HyperDjango achieves 2-5x database speedups with pg.zig.",
            "body": "The native pg.zig driver uses the PostgreSQL wire protocol directly...",
            "author_id": authors[0].id,
            "published": True,
            "published_at": "2026-04-08T11:00:00Z",
            "language": "en",
        },
        {
            "title": "Deploying to Production with systemd",
            "slug": "deploying-production-systemd",
            "excerpt": "Step-by-step guide to deploying HyperDjango apps with systemd and nginx.",
            "body": "Use `uv run hyper systemd install` to generate a hardened service unit...",
            "author_id": authors[2].id,
            "published": True,
            "published_at": "2026-04-10T16:00:00Z",
            "language": "en",
        },
        {
            "title": "Draft: Upcoming Features",
            "slug": "draft-upcoming-features",
            "excerpt": "Preview of upcoming HyperDjango features.",
            "body": "This post is still a draft...",
            "author_id": authors[0].id,
            "published": False,
            "published_at": "",
            "language": "en",
        },
    ]

    posts = []
    for data in posts_data:
        p = Post(**data)
        await p.save(db=db)
        posts.append(p)

    # Post-Category associations
    associations = [
        (0, 0),
        (0, 1),  # Getting Started → Technology, Python
        (1, 0),
        (1, 1),  # Premiers pas → Technology, Python
        (2, 1),
        (2, 2),  # REST APIs → Python, Web Development
        (3, 0),  # Zig Performance → Technology
        (4, 2),
        (4, 3),  # Deploying → Web Development, DevOps
    ]
    for pi, ci in associations:
        pc = PostCategory(post_id=posts[pi].id, category_id=categories[ci].id)
        await pc.save(db=db)

    print(
        f"  Blog Platform seeded: {len(posts)} posts, {len(authors)} authors, {len(categories)} categories"
    )
