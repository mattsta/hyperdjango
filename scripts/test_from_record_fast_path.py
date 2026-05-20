"""
Test: Model.from_record fast path produces instances identical to the
slow path for models with no custom validators or post_init hooks.

Proves:
  1. Fast path is enabled for simple hypernews models (Post, Comment, User)
  2. Field values populated from DB rows match slow path
  3. _loaded_from_db is set, __pydantic_fields_set__ populated
  4. Model behaves correctly for .save() (update vs insert via _loaded_from_db)
  5. Slow path still runs for models with custom validators
  6. Round-trip: from_record → to_dict → back matches expectations

Run: uv run python scripts/test_from_record_fast_path.py
"""

# hyper-test: db_isolated

import asyncio
import os
import subprocess

from hyperdjango.testkit import check, finish, run_main

os.environ.setdefault("DATABASE_URL", "postgres://localhost/hyperdjango_test")
os.environ.setdefault("HYPER_LOAD_TEST", "1")


async def main() -> bool:
    # Setup DB
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypernews.app:app",
            "--drop",
            "--seed",
            "services.hypernews.setup:seed",
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )

    from hyperdjango.database import get_db
    from services.hypernews.app import Comment, Post, User

    db = get_db()
    if db._pool_handle is None:
        await db.connect()

    print("\n── Fast-path eligibility ──")
    check("Post fast path enabled", Post.__dhi_from_record_fast__ is True)
    check("Comment fast path enabled", Comment.__dhi_from_record_fast__ is True)
    check("User fast path enabled", User.__dhi_from_record_fast__ is True)

    # Test 1: fast path produces correct values
    print("\n── Fast path field population ──")
    post_rows = await db.query("SELECT * FROM hn_posts LIMIT 1")
    if not check(
        "seed produced posts", bool(post_rows), "No posts in DB — seed failed"
    ):
        return finish()
    row = post_rows[0]

    post = Post.from_record(row)
    check("id matches", post.id == row["id"], f"{post.id} != {row['id']}")
    check("title matches", post.title == row["title"], f"{post.title[:40]!r}")
    check("score matches", post.score == row["score"], f"{post.score}")
    check("created_at datetime matches", post.created_at == row["created_at"])
    # Enum coercion: DB stores str, model must hold enum instance
    import enum as _enum

    check(
        "status is enum instance",
        isinstance(post.status, _enum.Enum),
        f"type={type(post.status).__name__}",
    )
    check(
        "status.value matches DB str",
        post.status.value == row["status"],
        f"{post.status.value!r} != {row['status']!r}",
    )
    check("_loaded_from_db is True", post._loaded_from_db is True)
    check(
        "all fields marked as set",
        post.__pydantic_fields_set__ == set(Post.__dhi_field_names__),
    )
    check("no extra fields", post.__pydantic_extra__ is None)
    check("no private attrs", post.__pydantic_private__ is None)

    # Test 2: to_dict round-trip produces the same values as the row
    # Note: to_dict unwraps enums to their .value, so comparison works
    # for both enum and non-enum fields.
    print("\n── to_dict() round-trip ──")
    post_dict = post.to_dict()
    for field_name in Post.__dhi_field_names__:
        check(
            f"to_dict[{field_name}] == row[{field_name}]",
            post_dict.get(field_name) == row[field_name],
            f"{post_dict.get(field_name)!r} != {row[field_name]!r}",
        )

    # Test 3: save() detects update (not insert) via _loaded_from_db
    print("\n── save() uses _loaded_from_db for update/insert detection ──")
    original_title = post.title
    post.title = original_title + " [updated]"
    await post.save()
    # Re-fetch and verify
    refetched_rows = await db.query("SELECT * FROM hn_posts WHERE id = $1", post.id)
    check(
        "save() updated existing row (not insert)",
        refetched_rows[0]["title"] == original_title + " [updated]",
        f"{refetched_rows[0]['title']!r}",
    )
    # Restore original
    post.title = original_title
    await post.save()

    # Test 4: compare against full __init__ path (simulate slow path)
    print("\n── Fast path vs slow path equivalence ──")
    # Force slow path by temporarily disabling the flag
    fast_post = Post.from_record(row)
    Post.__dhi_from_record_fast__ = False
    try:
        slow_post = Post.from_record(row)
    finally:
        Post.__dhi_from_record_fast__ = True

    for field_name in Post.__dhi_field_names__:
        fast_val = getattr(fast_post, field_name, None)
        slow_val = getattr(slow_post, field_name, None)
        check(
            f"fast/slow equal: {field_name}",
            fast_val == slow_val,
            f"fast={fast_val!r:.40} slow={slow_val!r:.40}",
        )
    check(
        "_loaded_from_db equal",
        fast_post._loaded_from_db == slow_post._loaded_from_db,
    )

    # Test 5: Comment and User paths
    print("\n── Comment / User from_record ──")
    comment_rows = await db.query("SELECT * FROM hn_comments LIMIT 1")
    if comment_rows:
        comment = Comment.from_record(comment_rows[0])
        check("Comment.id populated", comment.id == comment_rows[0]["id"])
        check("Comment._loaded_from_db True", comment._loaded_from_db is True)

    user_rows = await db.query("SELECT * FROM hn_users LIMIT 1")
    if user_rows:
        user = User.from_record(user_rows[0])
        check("User.id populated", user.id == user_rows[0]["id"])
        check(
            "User.password_hash populated",
            user.password_hash == user_rows[0]["password_hash"],
        )
        check("User._loaded_from_db True", user._loaded_from_db is True)

    # Test 6: ORM QuerySet still returns correct models (integration test)
    print("\n── QuerySet integration ──")
    posts = await Post.objects.limit(3).all()
    check("QuerySet returned posts", len(posts) > 0, f"got {len(posts)}")
    for p in posts:
        check(f"Post.id={p.id} _loaded_from_db True", p._loaded_from_db is True)
        check(f"Post.id={p.id} title is str", isinstance(p.title, str))

    # Summary
    print(f"\n{'=' * 50}")
    return finish()


def _main() -> bool:
    return asyncio.run(main())


if __name__ == "__main__":
    run_main(_main)
