"""Tests for QuerySet Exists / NotExists / OuterRef — task #197.

Correlated subquery support for filter() / exclude():

    Forum.objects.filter(
        Exists(Post.objects.filter(forum_id=OuterRef("id")))
    )
    # → SELECT * FROM forums f WHERE EXISTS (SELECT ... FROM posts WHERE forum_id = f.id)

Tests cover:
1. Exists in filter() → positive match
2. Exists in filter() → no match
3. Exists in exclude() → NOT EXISTS semantics
4. ~Exists() → NotExists with same semantics as exclude()
5. OuterRef with a different column name
6. Exists + regular filters mixed (AND composition)
7. Exists with Q-wrapped OuterRef filters (sanity check Q-walk)

Usage:
    uv run hyper-test exists_outerref
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import Exists, OuterRef, Q
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class ExForum(Model):
    class Meta:
        table = "ex_forums"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    is_public: bool = Field(default=True)


class ExPost(Model):
    class Meta:
        table = "ex_posts"

    id: int = Field(primary_key=True, auto=True)
    forum_id: int = Field(foreign_key=ExForum)
    title: str = Field(max_length=200)
    is_deleted: bool = Field(default=False)


class ExHiddenEvent(Model):
    class Meta:
        table = "ex_hidden_events"

    id: int = Field(primary_key=True, auto=True)
    entity_type: str = Field(max_length=32)
    entity_id: int = Field()
    status: str = Field(max_length=32)


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in [
        "DROP TABLE IF EXISTS ex_hidden_events CASCADE",
        "DROP TABLE IF EXISTS ex_posts CASCADE",
        "DROP TABLE IF EXISTS ex_forums CASCADE",
        """CREATE TABLE ex_forums (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            is_public BOOLEAN NOT NULL DEFAULT TRUE
        )""",
        """CREATE TABLE ex_posts (
            id SERIAL PRIMARY KEY,
            forum_id INTEGER NOT NULL REFERENCES ex_forums(id) ON DELETE CASCADE,
            title VARCHAR(200) NOT NULL,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE
        )""",
        """CREATE TABLE ex_hidden_events (
            id SERIAL PRIMARY KEY,
            entity_type VARCHAR(32) NOT NULL,
            entity_id INTEGER NOT NULL,
            status VARCHAR(32) NOT NULL
        )""",
    ]:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for tbl in ("ex_hidden_events", "ex_posts", "ex_forums"):
        await db.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


async def main() -> int:
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    db = await setup_db()
    try:
        # Seed: forum1 has posts, forum2 doesn't; forum3 is hidden
        forum1 = ExForum(name="alpha")
        await forum1.save()
        forum2 = ExForum(name="beta")
        await forum2.save()
        forum3 = ExForum(name="gamma")
        await forum3.save()

        p1 = ExPost(forum_id=forum1.id, title="Post 1")
        await p1.save()
        p2 = ExPost(forum_id=forum1.id, title="Post 2")
        await p2.save()

        h1 = ExHiddenEvent(entity_type="forum", entity_id=forum3.id, status="hidden")
        await h1.save()

        # ── Test 1: Exists in filter — positive match ─────────────────
        print("\n=== Exists in filter() ===")

        forums_with_posts = (
            await ExForum.objects.filter(
                Exists(ExPost.objects.filter(forum_id=OuterRef("id")))
            )
            .order_by("id")
            .all()
        )
        check("Exists match count == 1", len(forums_with_posts) == 1)
        check(
            "Exists returns forum1 only",
            len(forums_with_posts) == 1 and forums_with_posts[0].name == "alpha",
        )

        # ── Test 2: Exists in exclude (NOT EXISTS) ────────────────────
        print("\n=== exclude(Exists(...)) → NOT EXISTS ===")

        forums_without_posts = (
            await ExForum.objects.exclude(
                Exists(ExPost.objects.filter(forum_id=OuterRef("id")))
            )
            .order_by("id")
            .all()
        )
        check("NOT EXISTS count == 2", len(forums_without_posts) == 2)
        names = {f.name for f in forums_without_posts}
        check("NOT EXISTS returns beta + gamma", names == {"beta", "gamma"})

        # ── Test 3: ~Exists() as positional in filter ─────────────────
        print("\n=== ~Exists() is NotExists ===")

        forums_without_posts_inv = (
            await ExForum.objects.filter(
                ~Exists(ExPost.objects.filter(forum_id=OuterRef("id")))
            )
            .order_by("id")
            .all()
        )
        check("~Exists count == 2", len(forums_without_posts_inv) == 2)
        inv_names = {f.name for f in forums_without_posts_inv}
        check("~Exists returns beta + gamma", inv_names == {"beta", "gamma"})

        # ── Test 4: Composed with regular filter (AND) ────────────────
        print("\n=== Exists composed with regular filter ===")

        public_with_posts = await ExForum.objects.filter(
            Exists(ExPost.objects.filter(forum_id=OuterRef("id"))),
            is_public=True,
        ).all()
        check("composed count == 1", len(public_with_posts) == 1)
        check("composed returns alpha", public_with_posts[0].name == "alpha")

        # ── Test 5: Correlated subquery on different column ───────────
        print("\n=== OuterRef on different outer column ===")

        # "Forums that have an active 'hidden' status event"
        hidden_forums = await ExForum.objects.filter(
            Exists(
                ExHiddenEvent.objects.filter(
                    entity_type="forum",
                    entity_id=OuterRef("id"),
                    status="hidden",
                )
            )
        ).all()
        check("hidden match count == 1", len(hidden_forums) == 1)
        check(
            "hidden match is gamma",
            hidden_forums and hidden_forums[0].name == "gamma",
        )

        # The hypernews-style visibility filter: forums that are NOT hidden
        visible_forums = (
            await ExForum.objects.filter(is_public=True)
            .exclude(
                Exists(
                    ExHiddenEvent.objects.filter(
                        entity_type="forum",
                        entity_id=OuterRef("id"),
                        status="hidden",
                    )
                )
            )
            .order_by("id")
            .all()
        )
        check("visible count == 2 (alpha, beta)", len(visible_forums) == 2)
        visible_names = {f.name for f in visible_forums}
        check("visible is alpha + beta", visible_names == {"alpha", "beta"})

        # ── Test 6: OuterRef inside Q subexpression ───────────────────
        print("\n=== OuterRef inside Q(...) ===")

        result = await ExForum.objects.filter(
            Exists(ExPost.objects.filter(Q(forum_id=OuterRef("id"), is_deleted=False)))
        ).all()
        check("Q-wrapped OuterRef count == 1", len(result) == 1)
        check(
            "Q-wrapped returns alpha",
            result and result[0].name == "alpha",
        )

    finally:
        await teardown_db(db)
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All Exists/OuterRef tests passed!")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
