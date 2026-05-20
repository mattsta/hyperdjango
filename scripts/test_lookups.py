#!/usr/bin/env python3
"""
Tests for ORM lookups and transforms.

Tests all 17 built-in lookups, 12 built-in transforms, transform+lookup chains,
custom lookup/transform registration, FK-spanning lookups, and exclude() negation.

Both unit tests (SQL generation) and integration tests (live PostgreSQL queries).

Usage:
    uv run hyper-test lookups
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.lookups import (
    Lookup,
    Transform,
    _lookup_registry,
    _transform_registry,
    get_lookup,
    get_transform,
    list_lookups,
    list_transforms,
    register_lookup,
    register_transform,
    resolve_exclude,
    resolve_lookup,
)
from hyperdjango.models import Field, Model

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test models for integration tests
# ---------------------------------------------------------------------------


class LookupUser(Model):
    class Meta:
        table = "test_lookup_users"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    email: str = Field(max_length=200, default="")
    age: int = Field(default=0)
    score: float = Field(default=0.0)
    bio: str | None = Field(default=None)
    is_active: bool = Field(default=True)
    created_at: str | None = Field(default=None)


class LookupPost(Model):
    class Meta:
        table = "test_lookup_posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=300)
    author_id: int = Field(foreign_key=LookupUser)
    views: int = Field(default=0)


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS — SQL generation without database
# ═══════════════════════════════════════════════════════════════════════════

# --- Exact ---


@test("exact lookup: implicit")
def test_exact_implicit():
    sql, params = resolve_lookup("name", "Alice")
    assert sql == "name = $1", f"Got: {sql}"
    assert params == ["Alice"]


@test("exact lookup: explicit")
def test_exact_explicit():
    sql, params = resolve_lookup("name__exact", "Alice")
    assert sql == "name = $1", f"Got: {sql}"
    assert params == ["Alice"]


@test("exact lookup: None value -> IS NULL")
def test_exact_none():
    sql, params = resolve_lookup("name", None)
    assert sql == "name IS NULL", f"Got: {sql}"
    assert params == []


# --- IExact ---


@test("iexact lookup: case-insensitive exact")
def test_iexact():
    sql, params = resolve_lookup("name__iexact", "alice")
    assert sql == "UPPER(name) = UPPER($1)", f"Got: {sql}"
    assert params == ["alice"]


# --- Contains ---


@test("contains lookup: wraps value in %")
def test_contains():
    sql, params = resolve_lookup("name__contains", "ali")
    assert sql == "name LIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["%ali%"]


@test("icontains lookup: ILIKE with %")
def test_icontains():
    sql, params = resolve_lookup("name__icontains", "ALI")
    assert sql == "name ILIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["%ALI%"]


# --- StartsWith / EndsWith ---


@test("startswith lookup: trailing %")
def test_startswith():
    sql, params = resolve_lookup("name__startswith", "Al")
    assert sql == "name LIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["Al%"]


@test("istartswith lookup: ILIKE trailing %")
def test_istartswith():
    sql, params = resolve_lookup("name__istartswith", "al")
    assert sql == "name ILIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["al%"]


@test("endswith lookup: leading %")
def test_endswith():
    sql, params = resolve_lookup("name__endswith", "ce")
    assert sql == "name LIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["%ce"]


@test("iendswith lookup: ILIKE leading %")
def test_iendswith():
    sql, params = resolve_lookup("name__iendswith", "CE")
    assert sql == "name ILIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["%CE"]


# --- Comparison ---


@test("gt lookup")
def test_gt():
    sql, params = resolve_lookup("age__gt", 18)
    assert sql == "age > $1", f"Got: {sql}"
    assert params == [18]


@test("gte lookup")
def test_gte():
    sql, params = resolve_lookup("age__gte", 18)
    assert sql == "age >= $1", f"Got: {sql}"
    assert params == [18]


@test("lt lookup")
def test_lt():
    sql, params = resolve_lookup("age__lt", 65)
    assert sql == "age < $1", f"Got: {sql}"
    assert params == [65]


@test("lte lookup")
def test_lte():
    sql, params = resolve_lookup("age__lte", 65)
    assert sql == "age <= $1", f"Got: {sql}"
    assert params == [65]


# --- IN ---


@test("in lookup: ANY(array)")
def test_in():
    sql, params = resolve_lookup("id__in", [1, 2, 3])
    assert sql == "id = ANY($1)", f"Got: {sql}"
    assert params == [[1, 2, 3]]


@test("in lookup: tuple input")
def test_in_tuple():
    sql, params = resolve_lookup("id__in", (4, 5))
    assert sql == "id = ANY($1)", f"Got: {sql}"
    assert params == [[4, 5]]


@test("in lookup: set input")
def test_in_set():
    sql, params = resolve_lookup("id__in", {1, 2})
    assert sql == "id = ANY($1)", f"Got: {sql}"
    assert isinstance(params[0], list) and len(params[0]) == 2


@test("in lookup: empty list -> FALSE")
def test_in_empty():
    sql, params = resolve_lookup("id__in", [])
    assert sql == "FALSE", f"Got: {sql}"
    assert params == []


@test("in lookup: type error on non-iterable")
def test_in_type_error():
    try:
        resolve_lookup("id__in", 42)
        assert False, "Should have raised TypeError"
    except TypeError as e:
        assert "list/tuple/set" in str(e)


# --- Range ---


@test("range lookup: BETWEEN")
def test_range():
    sql, params = resolve_lookup("age__range", (18, 65))
    assert sql == "age BETWEEN $1 AND $2", f"Got: {sql}"
    assert params == [18, 65]


@test("range lookup: type error on non-pair")
def test_range_type_error():
    try:
        resolve_lookup("age__range", [1, 2, 3])
        assert False, "Should have raised TypeError"
    except TypeError:
        pass


# --- IsNull ---


@test("isnull lookup: True -> IS NULL")
def test_isnull_true():
    sql, params = resolve_lookup("bio__isnull", True)
    assert sql == "bio IS NULL", f"Got: {sql}"
    assert params == []


@test("isnull lookup: False -> IS NOT NULL")
def test_isnull_false():
    sql, params = resolve_lookup("bio__isnull", False)
    assert sql == "bio IS NOT NULL", f"Got: {sql}"
    assert params == []


# --- Regex ---


@test("regex lookup: PostgreSQL ~")
def test_regex():
    sql, params = resolve_lookup("name__regex", r"^[A-Z].*")
    assert sql == "name ~ $1", f"Got: {sql}"
    assert params == [r"^[A-Z].*"]


@test("iregex lookup: PostgreSQL ~*")
def test_iregex():
    sql, params = resolve_lookup("name__iregex", r"alice|bob")
    assert sql == "name ~* $1", f"Got: {sql}"
    assert params == [r"alice|bob"]


# --- Param index offset ---


@test("param_idx offset: starts at correct position")
def test_param_idx():
    sql, params = resolve_lookup("age__gte", 18, param_idx=5)
    assert sql == "age >= $5", f"Got: {sql}"
    assert params == [18]


@test("range param_idx offset")
def test_range_param_idx():
    sql, params = resolve_lookup("age__range", (18, 65), param_idx=3)
    assert sql == "age BETWEEN $3 AND $4", f"Got: {sql}"
    assert params == [18, 65]


# ═══════════════════════════════════════════════════════════════════════════
# TRANSFORM UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("year transform")
def test_year_transform():
    sql, params = resolve_lookup("created_at__year", 2024)
    assert sql == "EXTRACT(YEAR FROM created_at) = $1", f"Got: {sql}"
    assert params == [2024]


@test("month transform")
def test_month_transform():
    sql, params = resolve_lookup("created_at__month", 3)
    assert sql == "EXTRACT(MONTH FROM created_at) = $1", f"Got: {sql}"
    assert params == [3]


@test("day transform")
def test_day_transform():
    sql, params = resolve_lookup("created_at__day", 15)
    assert sql == "EXTRACT(DAY FROM created_at) = $1", f"Got: {sql}"
    assert params == [15]


@test("hour transform")
def test_hour_transform():
    sql, params = resolve_lookup("created_at__hour", 14)
    assert sql == "EXTRACT(HOUR FROM created_at) = $1", f"Got: {sql}"


@test("minute transform")
def test_minute_transform():
    sql, params = resolve_lookup("created_at__minute", 30)
    assert sql == "EXTRACT(MINUTE FROM created_at) = $1", f"Got: {sql}"


@test("second transform")
def test_second_transform():
    sql, params = resolve_lookup("created_at__second", 0)
    assert sql == "EXTRACT(SECOND FROM created_at) = $1", f"Got: {sql}"


@test("week_day transform")
def test_week_day_transform():
    sql, params = resolve_lookup("created_at__week_day", 1)
    assert sql == "EXTRACT(DOW FROM created_at) = $1", f"Got: {sql}"


@test("date transform")
def test_date_transform():
    sql, params = resolve_lookup("created_at__date", "2024-03-15")
    assert sql == "created_at::date = $1", f"Got: {sql}"
    assert params == ["2024-03-15"]


@test("lower transform")
def test_lower_transform():
    sql, params = resolve_lookup("name__lower", "alice")
    assert sql == "LOWER(name) = $1", f"Got: {sql}"
    assert params == ["alice"]


@test("upper transform")
def test_upper_transform():
    sql, params = resolve_lookup("name__upper", "ALICE")
    assert sql == "UPPER(name) = $1", f"Got: {sql}"
    assert params == ["ALICE"]


@test("length transform")
def test_length_transform():
    sql, params = resolve_lookup("name__length", 5)
    assert sql == "LENGTH(name) = $1", f"Got: {sql}"
    assert params == [5]


@test("trim transform")
def test_trim_transform():
    sql, params = resolve_lookup("name__trim", "Alice")
    assert sql == "TRIM(name) = $1", f"Got: {sql}"


# --- Transform + Lookup chains ---


@test("transform + lookup: year__gte")
def test_transform_lookup_year_gte():
    sql, params = resolve_lookup("created_at__year__gte", 2020)
    assert sql == "EXTRACT(YEAR FROM created_at) >= $1", f"Got: {sql}"
    assert params == [2020]


@test("transform + lookup: lower__contains")
def test_transform_lookup_lower_contains():
    sql, params = resolve_lookup("name__lower__contains", "ali")
    assert sql == "LOWER(name) LIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["%ali%"]


@test("transform + lookup: length__gte")
def test_transform_lookup_length_gte():
    sql, params = resolve_lookup("name__length__gte", 5)
    assert sql == "LENGTH(name) >= $1", f"Got: {sql}"
    assert params == [5]


@test("transform + lookup: upper__startswith")
def test_transform_lookup_upper_startswith():
    sql, params = resolve_lookup("name__upper__startswith", "AL")
    assert sql == "UPPER(name) LIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["AL%"]


@test("chained transforms: lower + length + gte")
def test_chained_transforms():
    sql, params = resolve_lookup("name__trim__length__gte", 3)
    assert sql == "LENGTH(TRIM(name)) >= $1", f"Got: {sql}"
    assert params == [3]


# ═══════════════════════════════════════════════════════════════════════════
# EXCLUDE TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("exclude: wraps in NOT()")
def test_exclude():
    sql, params = resolve_exclude("name", "Alice")
    assert sql == "NOT (name = $1)", f"Got: {sql}"
    assert params == ["Alice"]


@test("exclude with lookup: NOT(col > $N)")
def test_exclude_with_lookup():
    sql, params = resolve_exclude("age__gt", 65)
    assert sql == "NOT (age > $1)", f"Got: {sql}"
    assert params == [65]


@test("exclude isnull: NOT(IS NULL)")
def test_exclude_isnull():
    sql, params = resolve_exclude("bio__isnull", True)
    assert sql == "NOT (bio IS NULL)", f"Got: {sql}"
    assert params == []


# ═══════════════════════════════════════════════════════════════════════════
# TABLE ALIAS / FK SPAN TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("table alias qualification")
def test_table_alias():
    sql, params = resolve_lookup("name", "Alice", table_alias="users")
    assert sql == "users.name = $1", f"Got: {sql}"


@test("FK-span with join alias: author__name")
def test_fk_span():
    sql, params = resolve_lookup(
        "author__name",
        "Alice",
        table_alias="posts",
        join_aliases={"author": "t1"},
    )
    assert sql == "t1.name = $1", f"Got: {sql}"


@test("FK-span with lookup: author__name__icontains")
def test_fk_span_with_lookup():
    sql, params = resolve_lookup(
        "author__name__icontains",
        "ali",
        table_alias="posts",
        join_aliases={"author": "t1"},
    )
    assert sql == "t1.name ILIKE $1 ESCAPE '\\'", f"Got: {sql}"
    assert params == ["%ali%"]


@test("annotation alias not qualified")
def test_annotation_not_qualified():
    sql, params = resolve_lookup(
        "total",
        100,
        table_alias="books",
        annotation_aliases={"total"},
    )
    assert sql == "total = $1", f"Got: {sql}"


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOM LOOKUP/TRANSFORM REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════


@test("register custom lookup")
def test_custom_lookup():
    class NotEqualLookup(Lookup):
        def as_sql(self, col, param_idx, value):
            return f"{col} != ${param_idx}", [value]

    register_lookup("ne", NotEqualLookup())
    sql, params = resolve_lookup("age__ne", 0)
    assert sql == "age != $1", f"Got: {sql}"
    assert params == [0]
    # Cleanup
    del _lookup_registry["ne"]


@test("register custom transform")
def test_custom_transform():
    class AbsTransform(Transform):
        def as_sql(self, col):
            return f"ABS({col})"

    register_transform("abs", AbsTransform())
    sql, params = resolve_lookup("score__abs__gte", 10)
    assert sql == "ABS(score) >= $1", f"Got: {sql}"
    # Cleanup
    del _transform_registry["abs"]


@test("get_lookup returns lookup or None")
def test_get_lookup():
    assert get_lookup("exact") is not None
    assert get_lookup("nonexistent") is None


@test("get_transform returns transform or None")
def test_get_transform():
    assert get_transform("year") is not None
    assert get_transform("nonexistent") is None


@test("list_lookups returns all names sorted")
def test_list_lookups():
    names = list_lookups()
    assert "exact" in names
    assert "icontains" in names
    assert "regex" in names
    assert names == sorted(names)


@test("list_transforms returns all names sorted")
def test_list_transforms():
    names = list_transforms()
    assert "year" in names
    assert "lower" in names
    assert "length" in names
    assert names == sorted(names)


# ═══════════════════════════════════════════════════════════════════════════
# QUERYSET INTEGRATION — SQL generation via QuerySet._build_where_tree
# ═══════════════════════════════════════════════════════════════════════════


@test("QuerySet.filter(age__gte=18) generates correct WHERE")
def test_qs_filter_gte():
    qs = LookupUser.objects.filter(age__gte=18)
    sql, params = qs._build_select()
    assert ">=" in sql, f"Expected >= in SQL: {sql}"
    assert 18 in params


@test("QuerySet.filter(name__icontains='ali') generates ILIKE with %")
def test_qs_filter_icontains():
    qs = LookupUser.objects.filter(name__icontains="ali")
    sql, params = qs._build_select()
    assert "ILIKE" in sql, f"Expected ILIKE in SQL: {sql}"
    assert "%ali%" in params


@test("QuerySet.filter(bio__isnull=True) generates IS NULL")
def test_qs_filter_isnull():
    qs = LookupUser.objects.filter(bio__isnull=True)
    sql, params = qs._build_select()
    assert "IS NULL" in sql, f"Expected IS NULL in SQL: {sql}"


@test("QuerySet.filter(id__in=[1,2,3]) generates ANY")
def test_qs_filter_in():
    qs = LookupUser.objects.filter(id__in=[1, 2, 3])
    sql, params = qs._build_select()
    assert "ANY" in sql, f"Expected ANY in SQL: {sql}"
    assert [1, 2, 3] in params


@test("QuerySet.filter(age__range=(18,65)) generates BETWEEN")
def test_qs_filter_range():
    qs = LookupUser.objects.filter(age__range=(18, 65))
    sql, params = qs._build_select()
    assert "BETWEEN" in sql, f"Expected BETWEEN in SQL: {sql}"
    assert 18 in params
    assert 65 in params


@test("QuerySet.exclude(name='Bob') generates NOT")
def test_qs_exclude():
    qs = LookupUser.objects.exclude(name="Bob")
    sql, params = qs._build_select()
    assert "NOT" in sql, f"Expected NOT in SQL: {sql}"


@test("QuerySet.filter(name__regex='^A') generates ~")
def test_qs_filter_regex():
    qs = LookupUser.objects.filter(name__regex="^A")
    sql, params = qs._build_select()
    assert "~" in sql, f"Expected ~ in SQL: {sql}"


@test("QuerySet.filter(created_at__year=2024) generates EXTRACT")
def test_qs_filter_year():
    qs = LookupUser.objects.filter(created_at__year=2024)
    sql, params = qs._build_select()
    assert "EXTRACT" in sql, f"Expected EXTRACT in SQL: {sql}"
    assert "YEAR" in sql


@test("QuerySet chained filters: multiple lookups")
def test_qs_chained():
    qs = LookupUser.objects.filter(age__gte=18, name__icontains="al").filter(
        is_active=True
    )
    sql, params = qs._build_select()
    assert ">=" in sql
    assert "ILIKE" in sql
    assert "= $" in sql  # is_active = $N


@test("QuerySet.filter + exclude combined")
def test_qs_filter_exclude():
    qs = LookupUser.objects.filter(age__gte=18).exclude(name__startswith="X")
    sql, params = qs._build_select()
    assert ">=" in sql
    assert "NOT" in sql
    assert "LIKE" in sql


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — live PostgreSQL queries
# ═══════════════════════════════════════════════════════════════════════════


@test("DB: setup test tables")
async def test_db_setup():
    db = get_db()
    await db.execute("DROP TABLE IF EXISTS test_lookup_posts CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_lookup_users CASCADE")
    await db.execute("""
        CREATE TABLE test_lookup_users (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            email VARCHAR(200) DEFAULT '',
            age INTEGER DEFAULT 0,
            score FLOAT DEFAULT 0.0,
            bio TEXT DEFAULT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE test_lookup_posts (
            id SERIAL PRIMARY KEY,
            title VARCHAR(300) NOT NULL,
            author_id INTEGER REFERENCES test_lookup_users(id) ON DELETE CASCADE,
            views INTEGER DEFAULT 0
        )
    """)


@test("DB: seed test data")
async def test_seed_data():
    db = get_db()
    await db.execute(
        "INSERT INTO test_lookup_users (name, email, age, score, bio, is_active, created_at) VALUES "
        "($1, $2, $3, $4, $5, $6, $7)",
        "Alice",
        "alice@example.com",
        30,
        95.5,
        "Developer from NYC",
        True,
        "2024-03-15 14:30:00+00",
    )
    await db.execute(
        "INSERT INTO test_lookup_users (name, email, age, score, bio, is_active, created_at) VALUES "
        "($1, $2, $3, $4, $5, $6, $7)",
        "Bob",
        "bob@test.org",
        25,
        82.0,
        None,
        True,
        "2023-06-20 09:00:00+00",
    )
    await db.execute(
        "INSERT INTO test_lookup_users (name, email, age, score, bio, is_active, created_at) VALUES "
        "($1, $2, $3, $4, $5, $6, $7)",
        "Charlie",
        "charlie@example.com",
        45,
        77.3,
        "Senior engineer",
        False,
        "2024-01-10 22:15:00+00",
    )
    await db.execute(
        "INSERT INTO test_lookup_users (name, email, age, score, bio, is_active, created_at) VALUES "
        "($1, $2, $3, $4, $5, $6, $7)",
        "Diana",
        "diana@test.org",
        35,
        91.0,
        "Team lead",
        True,
        "2025-11-01 08:00:00+00",
    )
    # Posts
    await db.execute(
        "INSERT INTO test_lookup_posts (title, author_id, views) VALUES ($1, $2, $3)",
        "Hello World",
        1,
        100,
    )
    await db.execute(
        "INSERT INTO test_lookup_posts (title, author_id, views) VALUES ($1, $2, $3)",
        "Python Tips",
        1,
        250,
    )
    await db.execute(
        "INSERT INTO test_lookup_posts (title, author_id, views) VALUES ($1, $2, $3)",
        "Zig Performance",
        2,
        500,
    )


# --- Exact ---


@test("DB: exact filter")
async def test_db_exact():
    users = await LookupUser.objects.filter(name="Alice").all()
    assert len(users) == 1
    assert users[0].name == "Alice"


@test("DB: exact with None -> IS NULL")
async def test_db_exact_none():
    users = await LookupUser.objects.filter(bio=None).all()
    assert len(users) == 1
    assert users[0].name == "Bob"


# --- IExact ---


@test("DB: iexact filter")
async def test_db_iexact():
    users = await LookupUser.objects.filter(name__iexact="alice").all()
    assert len(users) == 1
    assert users[0].name == "Alice"


# --- Contains ---


@test("DB: contains filter")
async def test_db_contains():
    users = await LookupUser.objects.filter(name__contains="li").all()
    assert len(users) == 2  # Alice, Charlie
    names = {u.name for u in users}
    assert "Alice" in names
    assert "Charlie" in names


@test("DB: icontains filter")
async def test_db_icontains():
    users = await LookupUser.objects.filter(name__icontains="ALI").all()
    assert len(users) == 1  # Alice (only "Alice" contains "ali")
    assert users[0].name == "Alice"


# --- StartsWith / EndsWith ---


@test("DB: startswith filter")
async def test_db_startswith():
    users = await LookupUser.objects.filter(name__startswith="Al").all()
    assert len(users) == 1
    assert users[0].name == "Alice"


@test("DB: endswith filter")
async def test_db_endswith():
    users = await LookupUser.objects.filter(name__endswith="ob").all()
    assert len(users) == 1
    assert users[0].name == "Bob"


@test("DB: istartswith filter")
async def test_db_istartswith():
    users = await LookupUser.objects.filter(name__istartswith="al").all()
    assert len(users) == 1
    assert users[0].name == "Alice"


@test("DB: iendswith filter")
async def test_db_iendswith():
    users = await LookupUser.objects.filter(name__iendswith="OB").all()
    assert len(users) == 1
    assert users[0].name == "Bob"


# --- Comparison ---


@test("DB: gt filter")
async def test_db_gt():
    users = await LookupUser.objects.filter(age__gt=30).all()
    assert len(users) == 2  # Charlie(45), Diana(35)
    names = {u.name for u in users}
    assert "Charlie" in names
    assert "Diana" in names


@test("DB: gte filter")
async def test_db_gte():
    users = await LookupUser.objects.filter(age__gte=35).all()
    assert len(users) == 2  # Charlie(45), Diana(35)


@test("DB: lt filter")
async def test_db_lt():
    users = await LookupUser.objects.filter(age__lt=30).all()
    assert len(users) == 1
    assert users[0].name == "Bob"


@test("DB: lte filter")
async def test_db_lte():
    users = await LookupUser.objects.filter(age__lte=30).all()
    assert len(users) == 2  # Alice(30), Bob(25)


# --- IN ---


@test("DB: in filter")
async def test_db_in():
    users = await LookupUser.objects.filter(name__in=["Alice", "Bob"]).all()
    assert len(users) == 2
    names = {u.name for u in users}
    assert names == {"Alice", "Bob"}


# --- Range ---


@test("DB: range filter")
async def test_db_range():
    users = await LookupUser.objects.filter(age__range=(25, 35)).all()
    assert len(users) == 3  # Alice(30), Bob(25), Diana(35)
    names = {u.name for u in users}
    assert "Charlie" not in names


# --- IsNull ---


@test("DB: isnull=True filter")
async def test_db_isnull_true():
    users = await LookupUser.objects.filter(bio__isnull=True).all()
    assert len(users) == 1
    assert users[0].name == "Bob"


@test("DB: isnull=False filter")
async def test_db_isnull_false():
    users = await LookupUser.objects.filter(bio__isnull=False).all()
    assert len(users) == 3


# --- Regex ---


@test("DB: regex filter")
async def test_db_regex():
    users = await LookupUser.objects.filter(name__regex=r"^[A-B]").all()
    assert len(users) == 2  # Alice, Bob


@test("DB: iregex filter")
async def test_db_iregex():
    users = await LookupUser.objects.filter(name__iregex=r"^(alice|bob)$").all()
    assert len(users) == 2


# --- Transforms ---


@test("DB: year transform")
async def test_db_year():
    users = await LookupUser.objects.filter(created_at__year=2024).all()
    assert len(users) == 2  # Alice (2024-03), Charlie (2024-01)
    names = {u.name for u in users}
    assert "Alice" in names
    assert "Charlie" in names


@test("DB: year__gte transform + lookup")
async def test_db_year_gte():
    users = await LookupUser.objects.filter(created_at__year__gte=2024).all()
    assert len(users) == 3  # Alice, Charlie, Diana (2024+2025)


@test("DB: month transform")
async def test_db_month():
    users = await LookupUser.objects.filter(created_at__month=3).all()
    assert len(users) == 1
    assert users[0].name == "Alice"


# --- Exclude ---


@test("DB: exclude exact")
async def test_db_exclude():
    users = await LookupUser.objects.exclude(name="Alice").all()
    assert len(users) == 3
    names = {u.name for u in users}
    assert "Alice" not in names


@test("DB: exclude icontains")
async def test_db_exclude_icontains():
    users = await LookupUser.objects.exclude(name__icontains="li").all()
    assert len(users) == 2  # Bob, Diana


@test("DB: exclude isnull")
async def test_db_exclude_isnull():
    users = await LookupUser.objects.exclude(bio__isnull=True).all()
    assert len(users) == 3


# --- Chained filters ---


@test("DB: chained filters with multiple lookups")
async def test_db_chained():
    users = (
        await LookupUser.objects.filter(age__gte=25, name__icontains="a")
        .filter(is_active=True)
        .all()
    )
    # age>=25 AND name ILIKE '%a%' AND is_active=true
    # Alice(30, active), Diana(35, active) — Charlie matches age+name but is_active=false
    assert len(users) == 2
    names = {u.name for u in users}
    assert names == {"Alice", "Diana"}


@test("DB: filter + exclude combined")
async def test_db_filter_exclude():
    users = await LookupUser.objects.filter(age__gte=25).exclude(name="Bob").all()
    assert len(users) == 3  # Alice, Charlie, Diana
    names = {u.name for u in users}
    assert "Bob" not in names


# --- Complex queries ---


@test("DB: multiple lookups in single filter()")
async def test_db_multiple_lookups():
    users = await LookupUser.objects.filter(
        age__gte=25,
        age__lte=40,
        is_active=True,
    ).all()
    # Alice(30,active), Bob(25,active), Diana(35,active) — Charlie(45) outside range
    assert len(users) == 3, f"Expected 3 users, got {len(users)}"
    names = {u.name for u in users}
    assert names == {"Alice", "Bob", "Diana"}


@test("DB: email domain filter with contains")
async def test_db_email_domain():
    users = await LookupUser.objects.filter(email__endswith="@example.com").all()
    assert len(users) == 2  # Alice, Charlie


@test("DB: score range filter")
async def test_db_score_range():
    users = (
        await LookupUser.objects.filter(score__range=(80.0, 95.0))
        .order_by("name")
        .all()
    )
    assert len(users) == 2  # Bob(82), Diana(91)


# --- Cleanup ---


@test("DB: cleanup test tables")
async def test_db_cleanup():
    db = get_db()
    await db.execute("DROP TABLE IF EXISTS test_lookup_posts CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_lookup_users CASCADE")


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    # Collect all test functions
    tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            tests.append(obj)

    # Separate sync (unit) and async (integration) tests
    unit_tests = []
    db_tests = []
    for t in tests:
        if "DB:" in t.__name__:
            db_tests.append(t)
        else:
            unit_tests.append(t)

    # Run unit tests (no DB needed)
    print("\n═══ Unit Tests: Lookup SQL Generation ═══")
    for t in unit_tests:
        await t()

    # Run integration tests (need DB)
    print("\n═══ Integration Tests: Live PostgreSQL ═══")
    try:
        db = Database(DB_URL)
        set_db(db)
        await db.connect()
        for t in db_tests:
            await t()
    except Exception as e:
        print(f"\n  ⚠ Database connection failed ({e}), skipping integration tests")

    # Summary
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
