"""WS25 ORM SQL result-correctness fixes — ORM vs raw-SQL ground truth.

Each test executes an ORM query AND the equivalent hand-written SQL against the
SAME seeded data (with NULLs, duplicates and boundary values) on live Postgres,
then asserts identical results. Covers the eight WS25 fixes:

  1. exclude()/~Q() NULL demotion on nullable columns (3-valued-logic trap).
  2. exclude(a=1, b=2) == NOT(a AND b), not NOT(a) AND NOT(b).
  3. values_list(*fields) non-flat returns tuples, not dicts.
  4. only() always includes the primary key.
  5. F() on the RHS of a filter lookup resolves to a column reference.
  6. filter() on an aggregate annotation alias emits HAVING, not WHERE.
  7. order_by("fk__field") resolves the FK-spanning JOIN.
  8. __in=<QuerySet/Subquery> subselect; M2M-span error guard.

Requires PostgreSQL (see tests/test_db/conftest.py). Run:
  uv run pytest tests/test_db/test_ws25_orm_correctness.py -v
"""

import asyncio
import os
import subprocess

import pytest

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import Count, F, Q, Subquery
from hyperdjango.models import Field, ManyToManyField, Model

# ── Models ──────────────────────────────────────────────────────────────────


class W25Publisher(Model):
    class Meta:
        table = "w25_publisher"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class W25Author(Model):
    class Meta:
        table = "w25_author"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    # Nullable columns — the exclude()/~Q() NULL-demotion surface.
    country: str | None = Field(default=None)
    price: int | None = Field(default=None)
    pages: int | None = Field(default=None)
    publisher_id: int | None = Field(default=None, foreign_key=W25Publisher)


class W25Tag(Model):
    class Meta:
        table = "w25_tag"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class W25Article(Model):
    class Meta:
        table = "w25_article"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    tags = ManyToManyField(W25Tag)


# ── Live DB fixture ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_db():
    user = os.environ.get("USER", "postgres")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    subprocess.run(["createdb", dbname], capture_output=True)
    db = Database(
        f"postgresql://{user}:@localhost:5432/{dbname}", min_size=1, max_size=3
    )

    async def _setup():
        await db.connect()
        set_db(db)
        for t in (
            "w25_article_w25_tag",
            "w25_article",
            "w25_tag",
            "w25_author",
            "w25_publisher",
        ):
            await db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")

        await db.execute(
            "CREATE TABLE w25_publisher (id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
        )
        await db.execute(
            """CREATE TABLE w25_author (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                country TEXT,
                price INTEGER,
                pages INTEGER,
                publisher_id INTEGER REFERENCES w25_publisher(id)
            )"""
        )
        await db.execute(
            "CREATE TABLE w25_tag (id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE w25_article (id SERIAL PRIMARY KEY, title TEXT NOT NULL)"
        )
        await db.execute(
            """CREATE TABLE w25_article_w25_tag (
                article_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (article_id, tag_id)
            )"""
        )

        # Publishers
        await db.execute(
            "INSERT INTO w25_publisher (id, name) VALUES (1,'Zed'),(2,' Acme')"
        )
        # Authors — NULLs, duplicate values, boundary prices/pages.
        await db.execute(
            """INSERT INTO w25_author (id, name, country, price, pages, publisher_id)
               VALUES
                 (1,'Alice','US',20,200,1),
                 (2,'Bob','CA',30,300,2),
                 (3,'Cara',NULL,NULL,NULL,NULL),
                 (4,'Dan','US',20,300,1),
                 (5,'Eve','US',20,200,NULL),
                 (6,'Fay','GB',50,999,2)"""
        )
        # Tags + articles + M2M links
        await db.execute(
            "INSERT INTO w25_tag (id,name) VALUES (1,'python'),(2,'sql'),(3,'rust')"
        )
        await db.execute(
            "INSERT INTO w25_article (id,title) VALUES (1,'A'),(2,'B'),(3,'C')"
        )
        await db.execute(
            "INSERT INTO w25_article_w25_tag (article_id, tag_id) "
            "VALUES (1,1),(1,2),(2,2),(3,3)"
        )

    asyncio.run(_setup())
    yield db

    async def _teardown():
        for t in (
            "w25_article_w25_tag",
            "w25_article",
            "w25_tag",
            "w25_author",
            "w25_publisher",
        ):
            await db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")

    asyncio.run(_teardown())


def _ids(rows):
    """PK list from ORM instances or raw dict/tuple rows."""
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r["id"])
        elif isinstance(r, (tuple, list)):
            out.append(r[0])
        else:
            out.append(r.id)
    return sorted(out)


async def _raw_ids(db, sql, *params):
    rows = await db.query(sql, *params)
    return sorted(r["id"] for r in rows)


# ── #1 exclude()/~Q() NULL demotion ─────────────────────────────────────────


def test_exclude_nullable_includes_null_rows(live_db):
    async def run():
        orm = await W25Author.objects.exclude(country="US").all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author WHERE NOT (country = $1) OR country IS NULL",
            "US",
        )
        # Cara (NULL country) MUST be present.
        assert _ids(orm) == raw
        assert 3 in _ids(orm)

    asyncio.run(run())


def test_exclude_gt_nullable_includes_null(live_db):
    async def run():
        orm = await W25Author.objects.exclude(price__gt=25).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author WHERE NOT (price > $1) OR price IS NULL",
            25,
        )
        assert _ids(orm) == raw
        assert 3 in _ids(orm)  # NULL price included

    asyncio.run(run())


def test_exclude_in_nullable_includes_null(live_db):
    async def run():
        orm = await W25Author.objects.exclude(publisher_id__in=[1]).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author "
            "WHERE NOT (publisher_id = ANY($1)) OR publisher_id IS NULL",
            [1],
        )
        assert _ids(orm) == raw
        assert 3 in _ids(orm) and 5 in _ids(orm)  # NULL publisher rows included

    asyncio.run(run())


def test_not_q_single_leaf_demotes(live_db):
    async def run():
        orm = await W25Author.objects.filter(~Q(country="US")).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author WHERE NOT (country = $1) OR country IS NULL",
            "US",
        )
        assert _ids(orm) == raw

    asyncio.run(run())


def test_exclude_non_nullable_column_no_demotion(live_db):
    # name is NOT NULL — exclude must not add a redundant OR name IS NULL and
    # results still match a plain NOT(...).
    async def run():
        orm = await W25Author.objects.exclude(name="Alice").all()
        raw = await _raw_ids(
            live_db, "SELECT id FROM w25_author WHERE NOT (name = $1)", "Alice"
        )
        assert _ids(orm) == raw
        sql, _ = W25Author.objects.exclude(name="Alice")._build_select()
        assert "IS NULL" not in sql

    asyncio.run(run())


def test_exclude_isnull_no_extra_or(live_db):
    async def run():
        orm = await W25Author.objects.exclude(country__isnull=True).all()
        raw = await _raw_ids(
            live_db, "SELECT id FROM w25_author WHERE NOT (country IS NULL)"
        )
        assert _ids(orm) == raw
        assert 3 not in _ids(orm)

    asyncio.run(run())


# ── #2 exclude(a, b) == NOT(a AND b) ─────────────────────────────────────────


def test_exclude_multi_kwarg_is_not_and(live_db):
    async def run():
        orm = await W25Author.objects.exclude(price=20, pages=200).all()
        via_q = await W25Author.objects.filter(~Q(price=20, pages=200)).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author WHERE NOT (price = $1 AND pages = $2)",
            20,
            200,
        )
        assert _ids(orm) == raw
        assert _ids(orm) == _ids(via_q)
        # Rows 1 and 5 have price=20 AND pages=200 → excluded; 4 (20/300) kept.
        assert 1 not in _ids(orm) and 5 not in _ids(orm)
        assert 4 in _ids(orm)

    asyncio.run(run())


def test_chained_exclude_is_and_of_negations(live_db):
    # .exclude(a).exclude(b) stays NOT(a) AND NOT(b) — distinct from #2.
    async def run():
        orm = await W25Author.objects.exclude(price=20).exclude(pages=200).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author "
            "WHERE (NOT (price = $1) OR price IS NULL) "
            "AND (NOT (pages = $2) OR pages IS NULL)",
            20,
            200,
        )
        assert _ids(orm) == raw

    asyncio.run(run())


# ── #3 values_list non-flat → tuples ─────────────────────────────────────────


def test_values_list_returns_tuples(live_db):
    async def run():
        rows = await W25Author.objects.values_list("id", "name").order_by("id").all()
        assert all(isinstance(r, tuple) for r in rows)
        # Unpacking yields values, not dict keys.
        first_id, first_name = rows[0]
        assert first_id == 1 and first_name == "Alice"
        raw = await live_db.query("SELECT id, name FROM w25_author ORDER BY id")
        assert rows == [(r["id"], r["name"]) for r in raw]

    asyncio.run(run())


def test_values_list_flat_still_scalars(live_db):
    async def run():
        rows = await W25Author.objects.values_list("id", flat=True).order_by("id").all()
        assert rows == [1, 2, 3, 4, 5, 6]

    asyncio.run(run())


def test_values_still_dicts(live_db):
    async def run():
        rows = await W25Author.objects.values("id", "name").order_by("id").all()
        assert all(isinstance(r, dict) for r in rows)
        assert rows[0] == {"id": 1, "name": "Alice"}

    asyncio.run(run())


# ── #4 only() includes pk ────────────────────────────────────────────────────


def test_only_includes_pk(live_db):
    async def run():
        a = await W25Author.objects.only("name").order_by("id").first()
        assert a.id == 1  # real pk value, not a Field descriptor
        assert a.name == "Alice"
        sql, _ = W25Author.objects.only("name")._build_select()
        assert "id" in sql

    asyncio.run(run())


def test_defer_unchanged(live_db):
    async def run():
        a = await W25Author.objects.defer("country").order_by("id").first()
        assert a.id == 1
        sql, _ = W25Author.objects.defer("country")._build_select()
        assert "country" not in sql.split("FROM")[0]

    asyncio.run(run())


# ── #5 F() on RHS of a filter ────────────────────────────────────────────────


def test_filter_f_rhs_column_comparison(live_db):
    async def run():
        orm = await W25Author.objects.filter(pages__gt=F("price")).all()
        raw = await _raw_ids(live_db, "SELECT id FROM w25_author WHERE pages > price")
        assert _ids(orm) == raw
        assert len(raw) > 0

    asyncio.run(run())


def test_filter_f_rhs_exact(live_db):
    async def run():
        orm = await W25Author.objects.filter(price=F("pages")).all()
        raw = await _raw_ids(live_db, "SELECT id FROM w25_author WHERE price = pages")
        assert _ids(orm) == raw

    asyncio.run(run())


# ── #6 filter on aggregate annotation → HAVING ───────────────────────────────


def test_filter_aggregate_alias_uses_having(live_db):
    async def run():
        rows = (
            await W25Author.objects.values("country")
            .annotate(c=Count("id"))
            .filter(c__gt=1)
            .all()
        )
        got = sorted((r["country"], r["c"]) for r in rows)
        raw = await live_db.query(
            "SELECT country, COUNT(id) AS c FROM w25_author "
            "GROUP BY country HAVING COUNT(id) > $1",
            1,
        )
        expected = sorted((r["country"], r["c"]) for r in raw)
        assert got == expected
        sql, _ = (
            W25Author.objects.values("country")
            .annotate(c=Count("id"))
            .filter(c__gt=1)
            ._build_select()
        )
        assert "HAVING" in sql and "WHERE" not in sql

    asyncio.run(run())


def test_filter_before_annotate_stays_where(live_db):
    # A plain-column filter with an aggregate annotation stays in WHERE.
    async def run():
        rows = (
            await W25Author.objects.filter(country="US")
            .values("country")
            .annotate(c=Count("id"))
            .all()
        )
        assert rows == [{"country": "US", "c": 3}]
        sql, _ = (
            W25Author.objects.filter(country="US")
            .values("country")
            .annotate(c=Count("id"))
            ._build_select()
        )
        assert "WHERE" in sql

    asyncio.run(run())


# ── #7 order_by FK span ──────────────────────────────────────────────────────


def test_order_by_fk_span(live_db):
    async def run():
        rows = (
            await W25Author.objects.filter(publisher_id__isnull=False)
            .order_by("publisher__name", "id")
            .all()
        )
        got = [a.id for a in rows]
        raw = await live_db.query(
            "SELECT a.id FROM w25_author a "
            "JOIN w25_publisher p ON a.publisher_id = p.id "
            "WHERE a.publisher_id IS NOT NULL "
            "ORDER BY p.name, a.id"
        )
        assert got == [r["id"] for r in raw]
        sql, _ = W25Author.objects.order_by("publisher__name")._build_select()
        assert "JOIN" in sql and "publisher__name" not in sql

    asyncio.run(run())


# ── #8 __in subquery + M2M guards ────────────────────────────────────────────


def test_in_subquery(live_db):
    async def run():
        sub = W25Author.objects.filter(country="US").values_list(
            "publisher_id", flat=True
        )
        orm = await W25Author.objects.filter(publisher_id__in=sub).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author WHERE publisher_id IN "
            "(SELECT publisher_id FROM w25_author WHERE country = $1)",
            "US",
        )
        assert _ids(orm) == raw

    asyncio.run(run())


def test_in_subquery_expression(live_db):
    async def run():
        sub = Subquery(
            W25Author.objects.filter(country="US").values_list(
                "publisher_id", flat=True
            )
        )
        orm = await W25Author.objects.filter(publisher_id__in=sub).all()
        raw = await _raw_ids(
            live_db,
            "SELECT id FROM w25_author WHERE publisher_id IN "
            "(SELECT publisher_id FROM w25_author WHERE country = $1)",
            "US",
        )
        assert _ids(orm) == raw

    asyncio.run(run())


def test_m2m_span_filter_raises_clear_error(live_db):
    async def run():
        with pytest.raises(NotImplementedError, match="many-to-many"):
            await W25Article.objects.filter(tags__name="python").all()

    asyncio.run(run())


def test_m2m_prefetch_ensures_target(live_db):
    # _prefetch_m2m must resolve the target model (calls _ensure_target),
    # otherwise NoneType._meta crashes when the target registered after source.
    async def run():
        articles = (
            await W25Article.objects.prefetch_related("tags").order_by("id").all()
        )
        by_id = {a.id: a for a in articles}
        assert sorted(t.name for t in await by_id[1].tags.all()) == ["python", "sql"]
        assert sorted(t.name for t in await by_id[3].tags.all()) == ["rust"]

    asyncio.run(run())


# ── Regression: previously-correct shapes stay correct ───────────────────────


def test_regression_basic_shapes(live_db):
    async def run():
        # plain filter
        assert _ids(await W25Author.objects.filter(country="US").all()) == [1, 4, 5]
        # filter + order + limit
        rows = (
            await W25Author.objects.filter(price__gte=20)
            .order_by("-price")
            .limit(2)
            .all()
        )
        assert [a.id for a in rows] == [6, 2]
        # exclude on non-null column
        assert 1 not in _ids(await W25Author.objects.exclude(name="Alice").all())
        # __in list
        assert _ids(await W25Author.objects.filter(id__in=[1, 3, 6]).all()) == [1, 3, 6]
        # Q OR
        got = _ids(
            await W25Author.objects.filter(Q(country="CA") | Q(country="GB")).all()
        )
        assert got == [2, 6]
        # count
        assert await W25Author.objects.filter(country="US").count() == 3
        # values + aggregate (no filter on agg)
        agg = await W25Author.objects.aggregate(total=Count("id"))
        assert agg["total"] == 6

    asyncio.run(run())
