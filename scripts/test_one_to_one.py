"""
Tests for OneToOneField — unique FK with descriptor sugar.

# hyper-test: db_isolated

Tests:
- OneToOneField creates UNIQUE FK column
- Forward query (filter by FK)
- select_related across OneToOne
- Unique constraint enforced (duplicate insert fails)
- Metadata flags (one_to_one=True, unique=True)
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model, OneToOneField

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


class O2OUser(Model):
    class Meta:
        table = "o2o_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)


class O2OProfile(Model):
    class Meta:
        table = "o2o_profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = OneToOneField("o2o_users", related_name="profile")
    bio: str = Field(default="")
    website: str = Field(default="")


async def main():
    print("=" * 60)
    print("OneToOneField Tests")
    print("=" * 60)

    db = Database(DATABASE_URL)
    await db.connect()
    set_db(db)

    # Setup tables
    await db.execute("DROP TABLE IF EXISTS o2o_profiles CASCADE")
    await db.execute("DROP TABLE IF EXISTS o2o_users CASCADE")
    await db.execute("""
        CREATE TABLE o2o_users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE o2o_profiles (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE NOT NULL REFERENCES o2o_users(id) ON DELETE CASCADE,
            bio TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT ''
        )
    """)

    # === Metadata ===
    print("\n--- Metadata ---")
    profile_meta = O2OProfile._meta
    user_id_field = profile_meta.fields["user_id"]
    check("user_id has foreign_key", user_id_field.foreign_key == "o2o_users")
    check("user_id has unique=True", user_id_field.unique is True)
    check("user_id has one_to_one=True", user_id_field.one_to_one is True)

    # === CRUD ===
    print("\n--- CRUD ---")
    # Create users
    u1_id = await db.query_val(
        "INSERT INTO o2o_users (username) VALUES ($1) RETURNING id", "alice"
    )
    u2_id = await db.query_val(
        "INSERT INTO o2o_users (username) VALUES ($1) RETURNING id", "bob"
    )

    # Create profiles
    p1 = O2OProfile(user_id=u1_id, bio="Alice's bio", website="alice.com")
    await p1.save()
    check("Profile 1 saved", p1.id is not None)

    p2 = O2OProfile(user_id=u2_id, bio="Bob's bio")
    await p2.save()
    check("Profile 2 saved", p2.id is not None)

    # === Forward query ===
    print("\n--- Forward query ---")
    result = await O2OProfile.objects.filter(user_id=u1_id).first()
    check("Forward query finds profile", result is not None)
    check("Forward query correct bio", result.bio == "Alice's bio", f"got {result.bio}")

    # === Unique constraint ===
    print("\n--- Unique constraint ---")
    try:
        duplicate = O2OProfile(user_id=u1_id, bio="Duplicate")
        await duplicate.save()
        check("Unique constraint rejects duplicate", False, "insert succeeded")
    except Exception as e:
        check(
            "Unique constraint rejects duplicate",
            "unique" in str(e).lower() or "duplicate" in str(e).lower(),
            f"error: {e}",
        )

    # === select_related ===
    print("\n--- select_related ---")
    profiles = await O2OProfile.objects.select_related("user_id").all()
    check("select_related loads profiles", len(profiles) == 2, f"got {len(profiles)}")

    # === Filter with lookups ===
    print("\n--- Lookups ---")
    result = await O2OProfile.objects.filter(bio__icontains="alice").first()
    check(
        "icontains finds Alice's profile",
        result is not None and result.user_id == u1_id,
    )

    # Count
    count = await O2OProfile.objects.count()
    check("Count is 2", count == 2, f"got {count}")

    # === Update ===
    print("\n--- Update ---")
    updated = await O2OProfile.objects.filter(user_id=u1_id).update(
        website="https://alice.dev"
    )
    check("Update returns 1", updated == 1, f"got {updated}")
    refreshed = await O2OProfile.objects.filter(user_id=u1_id).first()
    check(
        "Update persisted",
        refreshed.website == "https://alice.dev",
        f"got {refreshed.website}",
    )

    # === Delete ===
    print("\n--- Delete ---")
    deleted = await O2OProfile.objects.filter(user_id=u2_id).delete()
    check("Delete returns 1", deleted == 1, f"got {deleted}")
    count = await O2OProfile.objects.count()
    check("Count after delete is 1", count == 1, f"got {count}")

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS o2o_profiles CASCADE")
    await db.execute("DROP TABLE IF EXISTS o2o_users CASCADE")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
