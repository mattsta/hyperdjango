"""
Tests for Q objects — composable AND/OR/NOT query conditions.

# hyper-test: db_isolated

Tests Q object SQL generation and integration with QuerySet.filter():
- Simple Q (single condition)
- OR composition (Q | Q)
- AND composition (Q & Q)
- NOT composition (~Q)
- Nested composition ((Q | Q) & Q)
- FK-spanning lookups in Q
- Transforms in Q
- Mixed Q + kwargs in filter()
- Q in exclude()
- Empty Q handling
- Real DB queries with Q objects
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import Q
from hyperdjango.models import Field, Model

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


# --- Unit tests: Q.resolve() SQL generation ---


def test_q_resolve():
    print("=== Q.resolve() SQL generation ===")

    # Simple condition
    q = Q(name="alice")
    params: list = []
    sql = q.resolve(params)
    check(
        "Simple Q → col = $1",
        sql == "name = $1" and params == ["alice"],
        f"sql={sql!r} params={params}",
    )

    # OR
    q = Q(name="alice") | Q(name="bob")
    params = []
    sql = q.resolve(params)
    check("OR → (a OR b)", "OR" in sql and "$1" in sql and "$2" in sql, f"sql={sql!r}")
    check("OR params correct", params == ["alice", "bob"], f"params={params}")

    # AND
    q = Q(name="alice") & Q(age__gt=18)
    params = []
    sql = q.resolve(params)
    check("AND → (a AND b)", "AND" in sql, f"sql={sql!r}")
    check("AND params correct", len(params) == 2, f"params={params}")

    # NOT
    q = ~Q(is_deleted=True)
    params = []
    sql = q.resolve(params)
    check("NOT → NOT (...)", sql.startswith("NOT"), f"sql={sql!r}")
    check("NOT params correct", params == [True], f"params={params}")

    # Nested: (Q | Q) & Q
    q = (Q(a=1) | Q(b=2)) & Q(c=3)
    params = []
    sql = q.resolve(params)
    check("Nested (OR) & AND", "OR" in sql and "AND" in sql, f"sql={sql!r}")
    check("Nested params", params == [1, 2, 3], f"params={params}")

    # Double NOT
    q = ~~Q(x=1)
    params = []
    sql = q.resolve(params)
    check("Double NOT cancels", "NOT" not in sql, f"sql={sql!r}")

    # Empty Q
    q = Q()
    params = []
    sql = q.resolve(params)
    check("Empty Q → empty string", sql == "", f"sql={sql!r}")

    # Multiple kwargs in single Q
    q = Q(a=1, b=2)
    params = []
    sql = q.resolve(params)
    check("Multi-kwarg Q is AND", "AND" in sql, f"sql={sql!r}")
    check("Multi-kwarg params", len(params) == 2, f"params={params}")

    # Lookup operators in Q
    q = Q(age__gte=18) | Q(role__in=["admin", "mod"])
    params = []
    sql = q.resolve(params)
    check("Lookups in Q (gte, in)", ">=" in sql and "ANY" in sql, f"sql={sql!r}")

    # icontains in Q
    q = Q(title__icontains="python")
    params = []
    sql = q.resolve(params)
    check("icontains in Q", "ILIKE" in sql, f"sql={sql!r}")

    # Q repr
    q = Q(name="alice") | Q(age__gt=18)
    r = repr(q)
    check("Q repr readable", "OR" in r and "name" in r, f"repr={r!r}")


# --- Integration tests: Q with real DB ---


class QTestItem(Model):
    class Meta:
        table = "q_test_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    category: str = Field(default="general")
    score: int = Field(default=0)
    is_active: bool = Field(default=True)


async def test_q_db():
    print("\n=== Q objects with real DB ===")

    db = Database(DATABASE_URL)
    await db.connect()
    set_db(db)

    # Setup
    await db.execute("DROP TABLE IF EXISTS q_test_items CASCADE")
    await db.execute("""
        CREATE TABLE q_test_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            score INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    # Seed data
    await db.execute(
        "INSERT INTO q_test_items (name, category, score, is_active) VALUES ($1, $2, $3, $4)",
        "alpha",
        "web",
        10,
        True,
    )
    await db.execute(
        "INSERT INTO q_test_items (name, category, score, is_active) VALUES ($1, $2, $3, $4)",
        "beta",
        "api",
        20,
        True,
    )
    await db.execute(
        "INSERT INTO q_test_items (name, category, score, is_active) VALUES ($1, $2, $3, $4)",
        "gamma",
        "web",
        30,
        False,
    )
    await db.execute(
        "INSERT INTO q_test_items (name, category, score, is_active) VALUES ($1, $2, $3, $4)",
        "delta",
        "db",
        40,
        True,
    )
    await db.execute(
        "INSERT INTO q_test_items (name, category, score, is_active) VALUES ($1, $2, $3, $4)",
        "epsilon",
        "api",
        50,
        False,
    )

    # Simple Q filter
    results = await QTestItem.objects.filter(Q(category="web")).all()
    check("Q(category=web) → 2 results", len(results) == 2, f"got {len(results)}")

    # OR filter
    results = await QTestItem.objects.filter(
        Q(category="web") | Q(category="api")
    ).all()
    check("Q(web) | Q(api) → 4 results", len(results) == 4, f"got {len(results)}")

    # AND filter
    results = await QTestItem.objects.filter(
        Q(category="web") & Q(is_active=True)
    ).all()
    check("Q(web) & Q(active) → 1 result", len(results) == 1, f"got {len(results)}")
    if results:
        check(
            "AND result is alpha", results[0].name == "alpha", f"got {results[0].name}"
        )

    # NOT filter
    results = await QTestItem.objects.filter(~Q(is_active=True)).all()
    check("~Q(active) → 2 inactive", len(results) == 2, f"got {len(results)}")

    # Nested: (OR) & single
    results = await QTestItem.objects.filter(
        (Q(category="web") | Q(category="api")) & Q(is_active=True)
    ).all()
    check("(web|api) & active → 2", len(results) == 2, f"got {len(results)}")

    # Mixed Q + kwargs
    results = await QTestItem.objects.filter(
        Q(category="web") | Q(category="api"),
        is_active=True,
    ).all()
    check("Q | Q + kwarg active → 2", len(results) == 2, f"got {len(results)}")

    # Q with lookups
    results = await QTestItem.objects.filter(Q(score__gte=30)).all()
    check("Q(score__gte=30) → 3", len(results) == 3, f"got {len(results)}")

    results = await QTestItem.objects.filter(Q(score__gte=20) & Q(score__lte=40)).all()
    check("Q(score 20-40) → 3", len(results) == 3, f"got {len(results)}")

    # Q with icontains
    results = await QTestItem.objects.filter(Q(name__icontains="a")).all()
    check("Q(name icontains 'a') matches", len(results) >= 2, f"got {len(results)}")

    # Q in exclude()
    results = await QTestItem.objects.exclude(
        Q(category="web") | Q(category="api")
    ).all()
    check("exclude(web|api) → 1 (delta)", len(results) == 1, f"got {len(results)}")
    if results:
        check(
            "exclude result is delta",
            results[0].name == "delta",
            f"got {results[0].name}",
        )

    # Complex: NOT OR + AND
    results = await QTestItem.objects.filter(
        ~(Q(category="web") | Q(is_active=False))
    ).all()
    names = sorted([r.name for r in results])
    check("~(web|inactive) → beta,delta", names == ["beta", "delta"], f"got {names}")

    # Q with count
    count = await QTestItem.objects.filter(Q(is_active=True)).count()
    check("Q with count() → 3", count == 3, f"got {count}")

    # Q with order_by
    results = await QTestItem.objects.filter(Q(is_active=True)).order_by("-score").all()
    check(
        "Q + order_by scores descending",
        [r.score for r in results] == [40, 20, 10],
        f"got {[r.score for r in results]}",
    )

    # Q with first()
    result = await QTestItem.objects.filter(Q(name="delta")).first()
    check(
        "Q with first() finds delta",
        result is not None and result.name == "delta",
        f"got {result}",
    )

    # Q with exists()
    exists = await QTestItem.objects.filter(Q(name="nonexistent")).exists()
    check("Q exists() false for missing", exists is False, f"got {exists}")

    exists = await QTestItem.objects.filter(Q(name="alpha")).exists()
    check("Q exists() true for alpha", exists is True, f"got {exists}")

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS q_test_items CASCADE")
    await db.disconnect()


def main():
    test_q_resolve()
    asyncio.run(test_q_db())

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
