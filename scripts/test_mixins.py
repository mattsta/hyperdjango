"""
Tests for production data pattern mixins.

Tests TimestampMixin, SoftDeleteMixin, OwnershipMixin, VersionedMixin.

Usage:
    uv run hyper-test mixins
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from unittest.mock import patch

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.mixins import (
    OwnershipMixin,
    SoftDeleteMixin,
    SoftDeleteQuerySet,
    TimestampMixin,
    VersionedMixin,
    VersionedQuerySet,
)
from hyperdjango.models import Field, Model, _resolve_instance_db
from hyperdjango.multi_db import ConnectionManager, get_connections, set_connections
from hyperdjango.public_id import IDStrategy, PublicIDMixin
from hyperdjango.signing import SignedSessionMixin, SigningKey

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


def wait_for_clock_past(stamp: datetime, *, timeout: float = 5.0) -> None:
    """Block until ``datetime.now(UTC)`` is strictly past ``stamp``.

    ``TimestampMixin.save`` stamps from ``datetime.now(UTC)``, so two saves
    inside one clock tick produce an EQUAL ``updated_at``. The condition the
    test needs is "the clock has moved", and that is what this waits for — a
    fixed sleep only guesses at how long a tick takes on this machine, and the
    guess is what makes the following assertion machine-dependent.
    """
    deadline = time.monotonic() + timeout
    while datetime.now(UTC) <= stamp:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"UTC clock did not advance past {stamp} within {timeout}s"
            )
        time.sleep(0.001)


# ---------------------------------------------------------------------------
# Test models (module-level)
# ---------------------------------------------------------------------------


class TimestampArticle(TimestampMixin, Model):
    class Meta:
        table = "test_ts_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)


class SoftArticle(SoftDeleteMixin, Model):
    class Meta:
        table = "test_sd_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    is_deleted: bool = Field(default=False)
    deleted_at: datetime | None = Field(default=None)


class OwnedArticle(OwnershipMixin, Model):
    class Meta:
        table = "test_own_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    created_by: int | None = Field(default=None)
    updated_by: int | None = Field(default=None)


class VersionedArticle(VersionedMixin, Model):
    class Meta:
        table = "test_ver_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    version: int = Field(default=1)
    is_current: bool = Field(default=True)
    entity_id: int | None = Field(default=None)


class PublicArticle(PublicIDMixin, Model):
    class Meta:
        table = "test_pub_articles"

    class PublicIDConfig:
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        strategy = IDStrategy.RANDOM
        entropy_bytes = 8

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    # Re-declared on the concrete model so ModelMeta registers it as a real
    # column (PublicIDMixin is a plain mixin, not a Model base, so its field
    # annotation isn't merged into a Model-and-mixin composition).
    public_id: str | None = Field(default=None, unique=True, index=True, max_length=64)


class SignedSession(SignedSessionMixin, TimestampMixin):
    class Meta:
        table = "test_signed_sessions"

    class TokenConfig:
        keys = [SigningKey(secret="test-mixins-session-key-2026", version=1)]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(default=0)


class FullArticle(TimestampMixin, SoftDeleteMixin, OwnershipMixin, Model):
    class Meta:
        table = "test_full_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)
    is_deleted: bool = Field(default=False)
    deleted_at: datetime | None = Field(default=None)
    created_by: int | None = Field(default=None)
    updated_by: int | None = Field(default=None)


# ---------------------------------------------------------------------------
# Routing regression fixtures (Meta.database binding)
# ---------------------------------------------------------------------------
#
# These models bind Meta.database to a second connection ("secondary"). The
# mixin write paths must route to THAT connection — not the global default —
# exactly as base Model.save does via _resolve_instance_db(for_write=True).


class RoutedSoftArticle(SoftDeleteMixin, Model):
    class Meta:
        table = "test_routed_sd"
        database = "secondary"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    is_deleted: bool = Field(default=False)
    deleted_at: datetime | None = Field(default=None)


class RoutedVersionedArticle(VersionedMixin, Model):
    class Meta:
        table = "test_routed_ver"
        database = "secondary"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    version: int = Field(default=1)
    is_current: bool = Field(default=True)
    entity_id: int | None = Field(default=None)


class RoutedPublicArticle(PublicIDMixin, Model):
    class Meta:
        table = "test_routed_pub"
        database = "secondary"

    class PublicIDConfig:
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        strategy = IDStrategy.ENCODED_PK

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    public_id: str | None = Field(default=None, unique=True, index=True, max_length=64)


class _RecordingDB:
    """A stand-in Database that records the SQL it is handed, so a test can
    prove a mixin routed a write to THIS connection (the model's bound
    Meta.database) rather than the global default. It fakes just enough of the
    query surface for the mixin write paths; no real table is ever touched."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._fake_pk = 100

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql))
        return None

    async def query(self, sql, *args):
        self.calls.append(("query", sql))
        return []

    async def query_val(self, sql, *args):
        self.calls.append(("query_val", sql))
        # MAX(version) reads want a version int; INSERT ... RETURNING wants a pk.
        if "MAX(" in sql:
            return 1
        return self._fake_pk

    async def query_one(self, sql, *args):
        self.calls.append(("query_one", sql))
        return None

    def in_transaction(self):
        return False

    @property
    def sql(self) -> str:
        return " | ".join(s for _op, s in self.calls)


class _RoutingHarness:
    """Registers a ConnectionManager whose 'secondary' alias is a _RecordingDB
    and 'default' is the real connected db, then restores the prior manager."""

    def __enter__(self) -> _RecordingDB:
        self.rec = _RecordingDB()
        self._saved = get_connections()
        mgr = ConnectionManager()
        mgr._databases["default"] = get_db()
        mgr._databases["secondary"] = self.rec
        set_connections(mgr)
        return self.rec

    def __exit__(self, *exc):
        set_connections(self._saved)
        return False


def _tx_counter(db):
    """Return (factory, enters): a drop-in replacement for db.transaction that
    counts how many transactions are entered, delegating to the real context
    manager so the wrapped section still runs for real."""
    orig = db.transaction
    enters = {"n": 0}

    def factory(*args, **kwargs):
        cm = orig(*args, **kwargs)

        class _Wrap:
            async def __aenter__(self):
                enters["n"] += 1
                return await cm.__aenter__()

            async def __aexit__(self, *exc):
                return await cm.__aexit__(*exc)

        return _Wrap()

    return factory, enters


# ---------------------------------------------------------------------------
# DB setup / teardown
# ---------------------------------------------------------------------------

CREATE_TABLES = [
    "DROP TABLE IF EXISTS test_ts_articles CASCADE",
    "DROP TABLE IF EXISTS test_sd_articles CASCADE",
    "DROP TABLE IF EXISTS test_own_articles CASCADE",
    "DROP TABLE IF EXISTS test_ver_articles CASCADE",
    "DROP TABLE IF EXISTS test_pub_articles CASCADE",
    "DROP TABLE IF EXISTS test_signed_sessions CASCADE",
    "DROP TABLE IF EXISTS test_full_articles CASCADE",
    """CREATE TABLE test_ts_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ
    )""",
    """CREATE TABLE test_sd_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        is_deleted BOOLEAN DEFAULT FALSE,
        deleted_at TIMESTAMPTZ
    )""",
    """CREATE TABLE test_own_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        created_by INTEGER,
        updated_by INTEGER
    )""",
    """CREATE TABLE test_ver_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        version INTEGER DEFAULT 1,
        is_current BOOLEAN DEFAULT TRUE,
        entity_id INTEGER
    )""",
    """CREATE TABLE test_pub_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        public_id VARCHAR(64) UNIQUE
    )""",
    """CREATE TABLE test_signed_sessions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER DEFAULT 0,
        token VARCHAR(128) UNIQUE,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ
    )""",
    """CREATE TABLE test_full_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ,
        is_deleted BOOLEAN DEFAULT FALSE,
        deleted_at TIMESTAMPTZ,
        created_by INTEGER,
        updated_by INTEGER
    )""",
]


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in CREATE_TABLES:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for table in [
        "test_full_articles",
        "test_signed_sessions",
        "test_pub_articles",
        "test_ver_articles",
        "test_own_articles",
        "test_sd_articles",
        "test_ts_articles",
    ]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await db.disconnect()


async def clean_data():
    db = get_db()
    for table in [
        "test_full_articles",
        "test_signed_sessions",
        "test_pub_articles",
        "test_ver_articles",
        "test_own_articles",
        "test_sd_articles",
        "test_ts_articles",
    ]:
        await db.execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------------------
# Unit Tests: Mixin field presence
# ---------------------------------------------------------------------------


@test("TimestampMixin: has created_at/updated_at fields")
def test_timestamp_fields():
    assert "created_at" in TimestampArticle._meta.fields
    assert "updated_at" in TimestampArticle._meta.fields


@test("SoftDeleteMixin: has is_deleted/deleted_at fields")
def test_softdelete_fields():
    assert "is_deleted" in SoftArticle._meta.fields
    assert "deleted_at" in SoftArticle._meta.fields


@test("SoftDeleteMixin: objects is SoftDeleteQuerySet")
def test_softdelete_queryset_type():
    assert isinstance(SoftArticle.objects, SoftDeleteQuerySet)


@test("OwnershipMixin: has created_by/updated_by fields")
def test_ownership_fields():
    assert "created_by" in OwnedArticle._meta.fields
    assert "updated_by" in OwnedArticle._meta.fields


@test("VersionedMixin: has version/is_current/entity_id fields")
def test_versioned_fields():
    assert "version" in VersionedArticle._meta.fields
    assert "is_current" in VersionedArticle._meta.fields
    assert "entity_id" in VersionedArticle._meta.fields


@test("VersionedMixin: objects is VersionedQuerySet")
def test_versioned_queryset_type():
    assert isinstance(VersionedArticle.objects, VersionedQuerySet)


@test("Composed: TimestampMixin + SoftDeleteMixin + OwnershipMixin")
def test_composed_fields():
    fields = FullArticle._meta.fields
    assert "created_at" in fields
    assert "updated_at" in fields
    assert "is_deleted" in fields
    assert "deleted_at" in fields
    assert "created_by" in fields
    assert "updated_by" in fields
    assert isinstance(FullArticle.objects, SoftDeleteQuerySet)


# ---------------------------------------------------------------------------
# DB Tests: TimestampMixin
# ---------------------------------------------------------------------------


@test("DB: TimestampMixin sets created_at on insert")
async def test_timestamp_created_at():
    await clean_data()
    article = TimestampArticle(title="Hello")
    await article.save()
    assert isinstance(article.created_at, datetime), (
        f"expected datetime, got {type(article.created_at)}"
    )
    assert isinstance(article.updated_at, datetime)


@test("DB: TimestampMixin preserves created_at on update")
async def test_timestamp_preserves_created_at():
    await clean_data()
    article = TimestampArticle(title="Hello")
    await article.save()
    original_created = article.created_at

    # Simulate update — wait for the clock to actually leave the insert's tick
    # so `updated_at > created_at` is provable rather than probable.
    article.title = "Updated"
    wait_for_clock_past(original_created)
    await article.save()

    assert article.created_at == original_created  # Unchanged
    assert article.updated_at is not None
    assert article.updated_at > original_created


@test("DB: TimestampMixin updated_at changes on each save")
async def test_timestamp_updated_at_changes():
    await clean_data()
    article = TimestampArticle(title="v1")
    await article.save()
    first_updated = article.updated_at

    wait_for_clock_past(first_updated)
    article.title = "v2"
    await article.save()

    assert article.updated_at > first_updated


@test("DB: TimestampMixin get_or_create forwards _using (regression)")
async def test_timestamp_get_or_create():
    # get_or_create/create call instance.save(_using=self._using); a
    # TimestampMixin.save that rejected _using raised TypeError on the create
    # leg, breaking get_or_create for every timestamped model. Prove both legs:
    # a fresh key creates (created=True) with timestamps stamped, and a repeat
    # returns the existing row (created=False, same pk) as success.
    await clean_data()
    obj, created = await TimestampArticle.objects.get_or_create(title="unique-key")
    assert created is True
    assert obj.pk is not None
    assert isinstance(obj.created_at, datetime)
    assert isinstance(obj.updated_at, datetime)

    again, created2 = await TimestampArticle.objects.get_or_create(title="unique-key")
    assert created2 is False
    assert again.pk == obj.pk


# ---------------------------------------------------------------------------
# DB Tests: SoftDeleteMixin
# ---------------------------------------------------------------------------


@test("DB: SoftDeleteMixin .delete() soft deletes")
async def test_softdelete_delete():
    await clean_data()
    article = SoftArticle(title="Hello")
    await article.save()

    await article.delete()
    assert article.is_deleted is True
    assert isinstance(article.deleted_at, datetime)


@test("DB: SoftDeleteMixin auto-filters deleted rows")
async def test_softdelete_auto_filter():
    await clean_data()
    a1 = SoftArticle(title="Active")
    await a1.save()
    a2 = SoftArticle(title="Deleted")
    await a2.save()
    await a2.delete()

    # Default query excludes deleted
    articles = await SoftArticle.objects.all()
    assert len(articles) == 1
    assert articles[0].title == "Active"


@test("DB: SoftDeleteMixin .with_deleted() includes all")
async def test_softdelete_with_deleted():
    await clean_data()
    a1 = SoftArticle(title="Active")
    await a1.save()
    a2 = SoftArticle(title="Deleted")
    await a2.save()
    await a2.delete()

    articles = await SoftArticle.objects.with_deleted().all()
    assert len(articles) == 2


@test("DB: SoftDeleteMixin .only_deleted() returns just deleted")
async def test_softdelete_only_deleted():
    await clean_data()
    a1 = SoftArticle(title="Active")
    await a1.save()
    a2 = SoftArticle(title="Deleted")
    await a2.save()
    await a2.delete()

    deleted = await SoftArticle.objects.only_deleted().all()
    assert len(deleted) == 1
    assert deleted[0].title == "Deleted"


@test("DB: SoftDeleteMixin .hard_delete() removes row")
async def test_softdelete_hard_delete():
    await clean_data()
    article = SoftArticle(title="Gone")
    await article.save()

    await article.hard_delete()

    # Even with_deleted won't find it
    articles = await SoftArticle.objects.with_deleted().all()
    assert len(articles) == 0


@test("DB: SoftDeleteMixin .restore() undoes soft delete")
async def test_softdelete_restore():
    await clean_data()
    article = SoftArticle(title="Restored")
    await article.save()
    await article.delete()

    assert article.is_deleted is True

    await article.restore()
    assert article.is_deleted is False
    assert article.deleted_at is None

    articles = await SoftArticle.objects.all()
    assert len(articles) == 1


@test("DB: SoftDeleteMixin filter chains work with auto-filter")
async def test_softdelete_filter_chain():
    await clean_data()
    a1 = SoftArticle(title="Alpha")
    await a1.save()
    a2 = SoftArticle(title="Beta")
    await a2.save()
    a3 = SoftArticle(title="Gamma")
    await a3.save()
    await a3.delete()

    # Filter on non-deleted only
    results = await SoftArticle.objects.filter(title="Alpha").all()
    assert len(results) == 1

    # Count excludes deleted
    count = await SoftArticle.objects.count()
    assert count == 2


# ---------------------------------------------------------------------------
# DB Tests: OwnershipMixin
# ---------------------------------------------------------------------------


@test("DB: OwnershipMixin save_as sets created_by")
async def test_ownership_save_as():
    await clean_data()
    article = OwnedArticle(title="Hello")
    await article.save_as(42)

    assert article.created_by == 42
    assert article.updated_by == 42


@test("DB: OwnershipMixin save_as preserves created_by on update")
async def test_ownership_preserves_created_by():
    await clean_data()
    article = OwnedArticle(title="Hello")
    await article.save_as(42)

    article.title = "Updated"
    await article.save_as(99)

    assert article.created_by == 42  # Original creator
    assert article.updated_by == 99  # New updater


@test("DB: OwnershipMixin accepts user object")
async def test_ownership_user_object():
    await clean_data()

    class FakeUser:
        pk = 7

    article = OwnedArticle(title="Hello")
    await article.save_as(FakeUser())

    assert article.created_by == 7
    assert article.updated_by == 7


# ---------------------------------------------------------------------------
# DB Tests: VersionedMixin
# ---------------------------------------------------------------------------


@test("DB: VersionedMixin first save creates version 1")
async def test_versioned_first_save():
    await clean_data()
    article = VersionedArticle(title="v1")
    await article.save()

    assert article.version == 1
    assert article.is_current is True
    assert article.entity_id == article.id


@test("DB: VersionedMixin second save creates version 2")
async def test_versioned_second_save():
    await clean_data()
    article = VersionedArticle(title="v1")
    await article.save()
    entity_id = article.entity_id

    article.title = "v2"
    await article.save()

    assert article.version == 2
    assert article.is_current is True
    assert article.entity_id == entity_id

    # Both versions exist in DB
    all_rows = await VersionedArticle.objects.with_versions().all()
    assert len(all_rows) == 2


@test("DB: VersionedMixin default query returns only current")
async def test_versioned_default_query():
    await clean_data()
    article = VersionedArticle(title="v1")
    await article.save()
    article.title = "v2"
    await article.save()
    article.title = "v3"
    await article.save()

    # Default: only current versions
    current = await VersionedArticle.objects.all()
    assert len(current) == 1
    assert current[0].title == "v3"
    assert current[0].version == 3


@test("DB: VersionedMixin .with_versions() returns all")
async def test_versioned_with_versions():
    await clean_data()
    article = VersionedArticle(title="v1")
    await article.save()
    article.title = "v2"
    await article.save()

    all_versions = await VersionedArticle.objects.with_versions().all()
    assert len(all_versions) == 2


@test("DB: VersionedMixin .get_history() returns ordered versions")
async def test_versioned_get_history():
    await clean_data()
    article = VersionedArticle(title="v1")
    await article.save()
    article.title = "v2"
    await article.save()
    article.title = "v3"
    await article.save()

    history = await article.get_history()
    assert len(history) == 3
    assert history[0].title == "v1"
    assert history[0].version == 1
    assert history[1].title == "v2"
    assert history[1].version == 2
    assert history[2].title == "v3"
    assert history[2].version == 3


@test("DB: VersionedMixin multiple entities independent")
async def test_versioned_multiple_entities():
    await clean_data()
    a1 = VersionedArticle(title="Article1 v1")
    await a1.save()
    a1.title = "Article1 v2"
    await a1.save()

    a2 = VersionedArticle(title="Article2 v1")
    await a2.save()

    current = await VersionedArticle.objects.all()
    assert len(current) == 2

    all_versions = await VersionedArticle.objects.with_versions().all()
    assert len(all_versions) == 3


@test("DB: VersionedMixin objects.create forwards _using (regression)")
async def test_versioned_create():
    # create() calls instance.save(_using=self._using); a VersionedMixin.save
    # that rejected _using raised TypeError here. Prove create succeeds and
    # stamps version 1.
    await clean_data()
    obj = await VersionedArticle.objects.create(title="ver-created")
    assert obj.pk is not None
    assert obj.version == 1
    assert obj.is_current is True


@test("DB: VersionedMixin get_or_create forwards _using (regression)")
async def test_versioned_get_or_create():
    await clean_data()
    obj, created = await VersionedArticle.objects.get_or_create(title="ver-key")
    assert created is True
    assert obj.pk is not None
    assert obj.version == 1

    again, created2 = await VersionedArticle.objects.get_or_create(title="ver-key")
    assert created2 is False
    assert again.pk == obj.pk


# ---------------------------------------------------------------------------
# DB Tests: PublicIDMixin (create/get_or_create _using regression)
# ---------------------------------------------------------------------------


@test("DB: PublicIDMixin objects.create forwards _using (regression)")
async def test_publicid_create():
    # create() forwards _using into PublicIDMixin.save; the old save(db=None)
    # signature raised TypeError. Prove create succeeds and auto-generates a
    # public_id.
    await clean_data()
    obj = await PublicArticle.objects.create(title="pub-created")
    assert obj.pk is not None
    assert isinstance(obj.public_id, str) and obj.public_id != ""


@test("DB: PublicIDMixin get_or_create forwards _using (regression)")
async def test_publicid_get_or_create():
    await clean_data()
    obj, created = await PublicArticle.objects.get_or_create(title="pub-key")
    assert created is True
    assert isinstance(obj.public_id, str) and obj.public_id != ""

    again, created2 = await PublicArticle.objects.get_or_create(title="pub-key")
    assert created2 is False
    assert again.pk == obj.pk


# ---------------------------------------------------------------------------
# DB Tests: SignedSessionMixin (create/get_or_create _using regression)
# ---------------------------------------------------------------------------


@test("DB: SignedSessionMixin objects.create forwards _using (regression)")
async def test_signed_session_create():
    # create() forwards _using down the SignedSessionMixin → TimestampMixin →
    # Model save chain; the old save(db=None) signature raised TypeError. Prove
    # create succeeds and auto-generates a token.
    await clean_data()
    obj = await SignedSession.objects.create(user_id=1)
    assert obj.pk is not None
    assert isinstance(obj.token, str) and obj.token != ""


@test("DB: SignedSessionMixin get_or_create forwards _using (regression)")
async def test_signed_session_get_or_create():
    await clean_data()
    obj, created = await SignedSession.objects.get_or_create(user_id=7)
    assert created is True
    assert isinstance(obj.token, str) and obj.token != ""

    again, created2 = await SignedSession.objects.get_or_create(user_id=7)
    assert created2 is False
    assert again.pk == obj.pk


# ---------------------------------------------------------------------------
# DB Tests: Composed mixins
# ---------------------------------------------------------------------------


@test("DB: Full composed model — timestamp + softdelete + ownership")
async def test_composed_full():
    await clean_data()
    article = FullArticle(title="Hello")
    await article.save_as(42)

    assert isinstance(article.created_at, datetime)
    assert isinstance(article.updated_at, datetime)
    assert article.created_by == 42

    await article.delete()
    assert article.is_deleted is True
    assert isinstance(article.deleted_at, datetime)

    # Auto-filtered
    articles = await FullArticle.objects.all()
    assert len(articles) == 0

    # With deleted
    articles = await FullArticle.objects.with_deleted().all()
    assert len(articles) == 1


@test("DB: Composed model restore works")
async def test_composed_restore():
    await clean_data()
    article = FullArticle(title="Hello")
    await article.save_as(42)
    await article.delete()

    await article.restore()
    articles = await FullArticle.objects.all()
    assert len(articles) == 1


# ---------------------------------------------------------------------------
# DB Tests: Meta.database routing (regression)
# ---------------------------------------------------------------------------
# A model bound to a non-default DB must write via _resolve_instance_db, not
# get_db(). These prove each fixed mixin path lands its write on the bound
# ("secondary") connection.


@test("DB: routing — _resolve_instance_db honors Meta.database binding")
async def test_routing_resolution_identity():
    with _RoutingHarness() as rec:
        # Base contract the mixins rely on: a Meta.database-bound model resolves
        # its write connection to that alias, by object identity.
        resolved = _resolve_instance_db(RoutedSoftArticle, for_write=True)
        assert resolved is rec, "bound model did not resolve to secondary DB"


@test("DB: routing — SoftDeleteMixin.delete writes to Meta.database, not default")
async def test_routing_softdelete_delete():
    with _RoutingHarness() as rec:
        art = RoutedSoftArticle(id=7, title="x")
        await art.delete()
    assert any(
        op == "execute" and "UPDATE test_routed_sd" in s and "is_deleted" in s
        for op, s in rec.calls
    ), f"soft-delete UPDATE did not route to secondary: {rec.sql}"


@test("DB: routing — SoftDeleteMixin.hard_delete/restore route to Meta.database")
async def test_routing_softdelete_hard_and_restore():
    with _RoutingHarness() as rec:
        art = RoutedSoftArticle(id=9, title="x")
        await art.hard_delete()
        await art.restore()
    assert any(
        op == "execute" and "DELETE FROM test_routed_sd" in s for op, s in rec.calls
    ), f"hard_delete DELETE did not route to secondary: {rec.sql}"
    assert any(
        op == "execute" and "UPDATE test_routed_sd" in s and "FALSE" in s
        for op, s in rec.calls
    ), f"restore UPDATE did not route to secondary: {rec.sql}"


@test("DB: routing — VersionedMixin.save writes version rows to Meta.database")
async def test_routing_versioned_save():
    with _RoutingHarness() as rec:
        art = RoutedVersionedArticle(title="v1")
        await art.save()
    assert any("INSERT INTO test_routed_ver" in s for _op, s in rec.calls), (
        f"versioned INSERT did not route to secondary: {rec.sql}"
    )


@test("DB: routing — VersionedMixin.get_history reads from Meta.database")
async def test_routing_versioned_get_history():
    with _RoutingHarness() as rec:
        art = RoutedVersionedArticle(id=3, title="v1")
        art.entity_id = 3
        await art.get_history()
    assert any(op == "query" and "FROM test_routed_ver" in s for op, s in rec.calls), (
        f"get_history SELECT did not route to secondary: {rec.sql}"
    )


@test("DB: routing — PublicIDMixin encoded_pk UPDATE lands on Meta.database")
async def test_routing_publicid_encoded_pk_update():
    with _RoutingHarness() as rec:
        art = RoutedPublicArticle(title="x")
        await art.save()
    # The encoded_pk follow-up UPDATE (the actual bug) must hit secondary, or
    # public_id would be a silent no-op on the default DB leaving it NULL.
    assert any(
        op == "execute" and "UPDATE test_routed_pub" in s and "public_id" in s
        for op, s in rec.calls
    ), f"encoded_pk public_id UPDATE did not route to secondary: {rec.sql}"
    assert isinstance(art.public_id, str) and art.public_id != "", (
        f"public_id not generated: {art.public_id!r}"
    )


# ---------------------------------------------------------------------------
# DB Tests: VersionedMixin.save atomicity (regression)
# ---------------------------------------------------------------------------


@test("DB: VersionedMixin.save wraps the version bump in a transaction (autocommit)")
async def test_versioned_save_atomic_autocommit():
    await clean_data()
    db = get_db()
    art = VersionedArticle(title="v1")
    await art.save()  # first version — no critical section

    factory, enters = _tx_counter(db)
    art.title = "v2"
    with patch.object(db, "transaction", factory):
        await art.save()  # existing entity → must open exactly one transaction

    assert enters["n"] == 1, (
        f"versioned update should open exactly one transaction, got {enters['n']}"
    )
    assert art.version == 2, f"expected version 2, got {art.version}"
    rows = await VersionedArticle.objects.with_versions().all()
    assert len(rows) == 2, f"both versions should persist, got {len(rows)}"
    currents = [r for r in rows if r.is_current]
    assert len(currents) == 1, f"exactly one is_current row, got {len(currents)}"


@test("DB: VersionedMixin.save reuses an open caller transaction (no nested tx)")
async def test_versioned_save_atomic_in_caller_tx():
    await clean_data()
    db = get_db()
    art = VersionedArticle(title="v1")
    await art.save()

    factory, enters = _tx_counter(db)
    art.title = "v2"
    with patch.object(db, "transaction", factory):
        async with db.transaction():  # caller opens the tx → enters == 1
            before = enters["n"]
            await art.save()  # in_transaction() True → must NOT open another
            after = enters["n"]

    assert before == 1, f"caller should have opened one transaction, got {before}"
    assert after == before, (
        f"versioned save opened a new tx inside the caller tx: {before}->{after}"
    )
    assert art.version == 2, f"expected version 2, got {art.version}"
    rows = await VersionedArticle.objects.with_versions().all()
    assert len(rows) == 2, f"both versions should persist, got {len(rows)}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    all_tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            all_tests.append(obj)

    unit_tests = [t for t in all_tests if not t.__name__.startswith("DB:")]
    db_tests = [t for t in all_tests if t.__name__.startswith("DB:")]

    print("\n═══ Unit Tests ═══")
    for t in unit_tests:
        await t()

    print("\n═══ DB Integration Tests ═══")
    try:
        db = await setup_db()
        try:
            for t in db_tests:
                await t()
        finally:
            await teardown_db(db)
    except Exception as e:
        print(f"\n  ⚠ Database connection failed ({e}), skipping integration tests")

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
