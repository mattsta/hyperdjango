"""Tests for multi-tenant / hierarchical ownership system.

Tests TenantRef, ContextVar lifecycle, TenantQuerySet auto-filtering,
TenantMixin, QuerySet composition with SoftDeleteMixin, TenantMiddleware
with all resolvers, admin injection, signal auto-populate, and that
non-tenant models are completely unaffected.
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import threading

from hyperdjango.database import Database, set_db
from hyperdjango.exceptions import HTTPException
from hyperdjango.mixins import SoftDeleteMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import (
    TenantMiddleware,
    TenantMixin,
    TenantRef,
    TenantResolutionError,
    clear_tenant,
    get_tenant,
    get_tenant_hierarchy,
    inject_tenant_condition,
    make_subdomain_resolver,
    resolve_from_header,
    resolve_from_url,
    resolve_from_user,
    set_tenant,
    tenant_context,
)

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")


def run_async(coro):
    return asyncio.run(coro)


# ── Test models ───────────────────────────────────────────────────────────


class TenantPost(TenantMixin, Model):
    class Meta:
        table = "test_tenant_posts"

    id: int = Field(primary_key=True, auto=True)
    tenant_id: int = Field(index=True)
    title: str = Field(max_length=200)
    status: str = Field(max_length=20, default="draft")


class NonTenantPost(Model):
    class Meta:
        table = "test_nontenant_posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)


class TenantSoftPost(TenantMixin, SoftDeleteMixin, Model):
    class Meta:
        table = "test_tenant_soft_posts"

    id: int = Field(primary_key=True, auto=True)
    tenant_id: int = Field(index=True)
    title: str = Field(max_length=200)
    is_deleted: bool = Field(default=False)
    deleted_at: str | None = Field(default=None)


# ── TenantRef + Context tests ────────────────────────────────────────────


def test_tenant_ref_creation():
    """TenantRef stores tenant_id, hierarchy, and type."""
    ref = TenantRef(tenant_id=42)
    assert ref.tenant_id == 42
    assert ref.hierarchy == ()
    assert ref.tenant_type == ""

    ref2 = TenantRef(tenant_id=1, hierarchy=(1, 2, 3), tenant_type="org")
    assert ref2.hierarchy == (1, 2, 3)
    assert ref2.tenant_type == "org"
    print("  PASS: TenantRef creation")


def test_context_set_get_clear():
    """set_tenant/get_tenant/clear_tenant lifecycle."""
    assert get_tenant() is None  # Initial state

    set_tenant(42)
    t = get_tenant()
    assert t is not None
    assert t.tenant_id == 42

    clear_tenant()
    assert get_tenant() is None
    print("  PASS: Context set/get/clear lifecycle")


def test_tenant_context_manager():
    """tenant_context() context manager scopes and restores."""
    assert get_tenant() is None

    with tenant_context(tenant_id=10, hierarchy=[10, 1]):
        t = get_tenant()
        assert t.tenant_id == 10
        assert t.hierarchy == (10, 1)

    assert get_tenant() is None  # Restored after exit
    print("  PASS: tenant_context manager")


def test_tenant_context_nesting():
    """Nested tenant contexts restore correctly."""
    with tenant_context(tenant_id=1):
        assert get_tenant().tenant_id == 1
        with tenant_context(tenant_id=2):
            assert get_tenant().tenant_id == 2
        assert get_tenant().tenant_id == 1

    assert get_tenant() is None
    print("  PASS: Nested tenant contexts")


def test_tenant_context_thread_isolation():
    """Each thread has its own tenant context."""
    results = {}

    def worker(name, tid):
        with tenant_context(tenant_id=tid):
            results[name] = get_tenant().tenant_id

    t1 = threading.Thread(target=worker, args=("t1", 100))
    t2 = threading.Thread(target=worker, args=("t2", 200))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["t1"] == 100
    assert results["t2"] == 200
    assert get_tenant() is None  # Main thread unaffected
    print("  PASS: Thread isolation")


# ── TenantQuerySet tests ─────────────────────────────────────────────────


def _where_clause(sql: str) -> str:
    """Extract the WHERE clause from SQL for assertion checks."""
    idx = sql.find("WHERE")
    return sql[idx:] if idx >= 0 else ""


def test_queryset_no_context_fails_closed():
    """TENANT_STRICT (default): no tenant context → never-match, ZERO rows.

    SECURITY: this is the guarantee that an anonymous / no-tenant request can't
    read every tenant's data. The query must not run globally.
    """
    clear_tenant()
    qs = TenantPost.objects.filter(status="draft")
    sql, params = qs._build_select()
    where = _where_clause(sql)
    assert "tenant_id = " not in where  # not scoped to a real tenant
    assert "1 = 0" in where  # fail-closed never-match injected
    print("  PASS: QuerySet without context — fails closed (0 rows)")


def test_queryset_no_context_nonstrict_opt_out():
    """TENANT_STRICT=False restores the legacy 'no context → global' behaviour."""
    from unittest.mock import patch

    clear_tenant()
    with patch("hyperdjango.tenancy.get_setting", return_value=False):
        qs = TenantPost.objects.filter(status="draft")
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "tenant_id = " not in where
        assert "1 = 0" not in where  # no fail-closed filter when opted out
    print("  PASS: TENANT_STRICT=False — legacy global behaviour")


def test_create_forces_context_tenant_over_forged_id():
    """SECURITY: a create inside tenant 42's context with a FORGED tenant_id=999
    (e.g. mass-assigned from a request body) is forced back to 42 — no
    cross-tenant write."""
    from hyperdjango.tenancy import _auto_set_tenant

    with tenant_context(tenant_id=42):
        post = TenantPost(title="x", tenant_id=999)  # attacker-supplied 999
        run_async(_auto_set_tenant(TenantPost, post, created=True))
        assert post.tenant_id == 42, (
            f"forged tenant_id not overridden: {post.tenant_id}"
        )
    print("  PASS: forged tenant_id on create overridden to context tenant")


def test_update_forces_context_tenant():
    """SECURITY: an update inside tenant 42's context can't move the row to
    another tenant by setting tenant_id."""
    from hyperdjango.tenancy import _auto_set_tenant

    with tenant_context(tenant_id=42):
        post = TenantPost(title="x", tenant_id=999)
        run_async(_auto_set_tenant(TenantPost, post, created=False))
        assert post.tenant_id == 42
    print("  PASS: update can't reassign tenant_id away from context")


def test_create_no_context_strict_raises():
    """SECURITY: creating a tenant row with no context and no explicit tenant_id
    is refused (strict) — no unowned/NULL-tenant rows."""
    from hyperdjango.tenancy import TenantResolutionError, _auto_set_tenant

    clear_tenant()
    post = TenantPost(title="x")  # no tenant_id, no context
    raised = False
    try:
        run_async(_auto_set_tenant(TenantPost, post, created=True))
    except TenantResolutionError:
        raised = True
    assert raised, "strict mode must refuse a no-context create with unset tenant_id"
    print("  PASS: no-context create with unset tenant_id refused (strict)")


def test_create_no_context_explicit_id_allowed():
    """CLI/admin: no context but an EXPLICIT tenant_id is allowed (cross-tenant
    seeding), and is not overridden."""
    from hyperdjango.tenancy import _auto_set_tenant

    clear_tenant()
    post = TenantPost(title="x", tenant_id=7)
    run_async(_auto_set_tenant(TenantPost, post, created=True))
    assert post.tenant_id == 7
    print("  PASS: no-context create with explicit tenant_id allowed")


def test_cache_key_isolated_per_tenant():
    """SECURITY: the compiled-SQL / result cache key must differ per tenant (and
    per scope mode) so a cached tenant-A query never serves tenant-B — and an
    unscoped/no-context template is never reused for a scoped query (which would
    drop the tenant filter)."""
    with tenant_context(tenant_id=1):
        k1 = TenantPost.objects.filter(status="x")._fast_where_key()
    with tenant_context(tenant_id=2):
        k2 = TenantPost.objects.filter(status="x")._fast_where_key()
    k_unscoped = TenantPost.objects.unscoped().filter(status="x")._fast_where_key()
    clear_tenant()
    k_empty = TenantPost.objects.filter(status="x")._fast_where_key()
    keys = {str(k1), str(k2), str(k_unscoped), str(k_empty)}
    assert len(keys) == 4, f"cache keys must all differ, got {len(keys)}: {keys}"
    print("  PASS: cache key isolated per tenant / scope mode")


def test_queryset_with_context():
    """TenantQuerySet with context auto-injects tenant_id filter."""
    with tenant_context(tenant_id=42):
        qs = TenantPost.objects.filter(status="draft")
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "tenant_id = " in where
        assert 42 in params
    print("  PASS: QuerySet with context — auto-filter")


def test_queryset_unscoped():
    """Unscoped QuerySet bypasses tenant filter even with context."""
    with tenant_context(tenant_id=42):
        qs = TenantPost.objects.unscoped().filter(status="draft")
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "tenant_id = " not in where
        assert 42 not in params
    print("  PASS: QuerySet unscoped bypasses filter")


def test_non_tenant_model_unaffected():
    """Non-tenant model is completely unaffected by tenant context."""
    with tenant_context(tenant_id=42):
        qs = NonTenantPost.objects.filter(title="hello")
        sql, params = qs._build_select()
        assert "tenant_id" not in sql
    print("  PASS: Non-tenant model unaffected")


# ── QuerySet Composition tests ────────────────────────────────────────────


def test_tenant_plus_softdelete_composition():
    """TenantMixin + SoftDeleteMixin compose correctly."""
    qs = TenantSoftPost.objects.filter(title="test")

    # Without tenant context — only soft delete filter
    clear_tenant()
    sql, params = qs._build_select()
    where = _where_clause(sql)
    assert "is_deleted" in where
    assert "tenant_id = " not in where  # No tenant WHERE condition

    # With tenant context — both filters
    with tenant_context(tenant_id=7):
        sql2, params2 = qs._build_select()
        where2 = _where_clause(sql2)
        assert "is_deleted" in where2
        assert "tenant_id = " in where2
        assert 7 in params2

    print("  PASS: TenantMixin + SoftDeleteMixin compose")


def test_composed_unscoped():
    """Unscoped on composed QuerySet bypasses tenant but keeps soft delete."""
    with tenant_context(tenant_id=7):
        qs = TenantSoftPost.objects.unscoped().filter(title="test")
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "is_deleted" in where  # Soft delete still active
        assert "tenant_id = " not in where  # Tenant bypassed
    print("  PASS: Composed unscoped keeps soft delete")


def test_composed_with_deleted():
    """with_deleted on composed QuerySet keeps tenant but removes soft delete."""
    with tenant_context(tenant_id=7):
        qs = TenantSoftPost.objects.with_deleted().filter(title="test")
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "is_deleted" not in where  # Soft delete removed
        assert "tenant_id = " in where  # Tenant still active
    print("  PASS: Composed with_deleted keeps tenant")


# ── Resolver tests ────────────────────────────────────────────────────────


class FakeRequest:
    def __init__(self, user=None, headers=None, path="/"):
        self.user = user
        self.headers = headers or {}
        self.path = path


class FakeUser:
    def __init__(self, tenant_id=None):
        self.tenant_id = tenant_id


def test_resolve_from_user():
    """resolve_from_user extracts tenant_id from request.user."""
    req = FakeRequest(user=FakeUser(tenant_id=42))
    ref = resolve_from_user(req)
    assert ref is not None
    assert ref.tenant_id == 42

    # No user
    req2 = FakeRequest(user=None)
    assert resolve_from_user(req2) is None

    # User without tenant_id
    class PlainUser:
        pass

    req3 = FakeRequest(user=PlainUser())
    assert resolve_from_user(req3) is None

    # Real AnonymousUser (falsy __bool__, no tenant_id) → public (None), not a
    # crash and not a fabricated tenant. Locks the anonymous sentinel contract.
    from hyperdjango.auth.user import AnonymousUser

    req4 = FakeRequest(user=AnonymousUser())
    assert resolve_from_user(req4) is None
    print("  PASS: resolve_from_user")


def test_resolve_from_header():
    """resolve_from_header reads X-Tenant-ID."""
    req = FakeRequest(headers={"x-tenant-id": "42"})
    ref = resolve_from_header(req)
    assert ref is not None
    assert ref.tenant_id == 42

    # Missing header
    req2 = FakeRequest(headers={})
    assert resolve_from_header(req2) is None

    # Non-integer header IS present but malformed → fail CLOSED (raise),
    # never silently return None (which would run the request unfiltered).
    req3 = FakeRequest(headers={"x-tenant-id": "abc"})
    try:
        resolve_from_header(req3)
        assert False, "malformed X-Tenant-ID must raise TenantResolutionError"
    except TenantResolutionError:
        pass
    print("  PASS: resolve_from_header")


def test_make_subdomain_resolver():
    """make_subdomain_resolver creates a resolver that extracts from Host header."""
    # Create a resolver with a mock lookup
    lookup_map = {"acme": 42, "beta": 99}
    resolver = make_subdomain_resolver(lambda slug: lookup_map.get(slug))

    req = FakeRequest(headers={"host": "acme.app.com"})
    ref = run_async(resolver(req))
    assert ref is not None
    assert ref.tenant_id == 42
    assert ref.tenant_type == "acme"

    # Unknown slug
    req2 = FakeRequest(headers={"host": "unknown.app.com"})
    assert run_async(resolver(req2)) is None

    # No subdomain
    req3 = FakeRequest(headers={"host": "app.com"})
    assert run_async(resolver(req3)) is None

    # Reserved subdomain
    req4 = FakeRequest(headers={"host": "www.app.com"})
    assert run_async(resolver(req4)) is None
    print("  PASS: make_subdomain_resolver")


def test_resolve_from_url():
    """resolve_from_url extracts from /t/{id}/... path."""
    req = FakeRequest(path="/t/42/dashboard")
    ref = resolve_from_url(req)
    assert ref is not None
    assert ref.tenant_id == 42

    # No tenant prefix
    req2 = FakeRequest(path="/dashboard")
    assert resolve_from_url(req2) is None

    # Non-integer id in a /t/ prefix → fail CLOSED (raise), not silent None.
    req3 = FakeRequest(path="/t/abc/dashboard")
    try:
        resolve_from_url(req3)
        assert False, "malformed tenant URL must raise TenantResolutionError"
    except TenantResolutionError:
        pass
    print("  PASS: resolve_from_url")


# ── inject_tenant_condition tests ─────────────────────────────────────────


def test_inject_tenant_condition_with_tenant():
    """inject_tenant_condition adds condition when tenant is active."""
    with tenant_context(tenant_id=42):
        conditions: list[str] = []
        params: list[object] = []
        inject_tenant_condition(TenantPost, conditions, params)
        assert len(conditions) == 1
        assert "tenant_id" in conditions[0]
        assert 42 in params
    print("  PASS: inject_tenant_condition with context")


def test_inject_tenant_condition_no_tenant():
    """inject_tenant_condition is no-op without context."""
    clear_tenant()
    conditions: list[str] = []
    params: list[object] = []
    inject_tenant_condition(TenantPost, conditions, params)
    assert len(conditions) == 0
    assert len(params) == 0
    print("  PASS: inject_tenant_condition without context")


def test_inject_tenant_condition_non_tenant_model():
    """inject_tenant_condition is no-op for non-tenant models."""
    with tenant_context(tenant_id=42):
        conditions: list[str] = []
        params: list[object] = []
        inject_tenant_condition(NonTenantPost, conditions, params)
        assert len(conditions) == 0
    print("  PASS: inject_tenant_condition non-tenant model")


# ── Live DB tests ─────────────────────────────────────────────────────────


def test_live_tenant_filtering():
    """End-to-end: insert rows for two tenants, query only sees current tenant's."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        set_db(db)

        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS test_tenant_posts (
                    id SERIAL PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    title VARCHAR(200) NOT NULL,
                    status VARCHAR(20) DEFAULT 'draft'
                )
            """)
            await db.execute("DELETE FROM test_tenant_posts")

            # Insert posts for two tenants
            await db.execute(
                "INSERT INTO test_tenant_posts (tenant_id, title, status) VALUES ($1, $2, $3)",
                1,
                "Tenant 1 Post A",
                "published",
            )
            await db.execute(
                "INSERT INTO test_tenant_posts (tenant_id, title, status) VALUES ($1, $2, $3)",
                1,
                "Tenant 1 Post B",
                "draft",
            )
            await db.execute(
                "INSERT INTO test_tenant_posts (tenant_id, title, status) VALUES ($1, $2, $3)",
                2,
                "Tenant 2 Post C",
                "published",
            )

            # Verify SQL generation with context
            clear_tenant()
            qs_all = TenantPost.objects.filter()
            sql_all, _ = qs_all._build_select()
            where_all = _where_clause(sql_all)
            assert "tenant_id = " not in where_all  # No filter

            with tenant_context(tenant_id=1):
                qs_t1 = TenantPost.objects.filter()
                sql_t1, params_t1 = qs_t1._build_select()
                where_t1 = _where_clause(sql_t1)
                assert "tenant_id = " in where_t1
                assert 1 in params_t1

            # Execute queries via raw SQL to verify filtering works
            clear_tenant()
            all_rows = await db.query("SELECT * FROM test_tenant_posts")
            assert len(all_rows) == 3

            t1_rows = await db.query(
                "SELECT * FROM test_tenant_posts WHERE tenant_id = $1", 1
            )
            assert len(t1_rows) == 2

            t2_rows = await db.query(
                "SELECT * FROM test_tenant_posts WHERE tenant_id = $1", 2
            )
            assert len(t2_rows) == 1

            print("  PASS: Live tenant filtering")
        finally:
            await db.execute("DROP TABLE IF EXISTS test_tenant_posts CASCADE")
            await db.disconnect()

    run_async(run())


def test_live_tenant_hierarchy():
    """End-to-end: hierarchical tenant CTE query."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        set_db(db)

        try:
            from hyperdjango.tenancy import CREATE_TENANTS_TABLE_SQL

            await db.execute(CREATE_TENANTS_TABLE_SQL)
            await db.execute("DELETE FROM hyper_tenants")

            # Create hierarchy: Org(1) → Team(2) → Project(3)
            await db.execute(
                "INSERT INTO hyper_tenants (id, name, slug) VALUES ($1, $2, $3)",
                1,
                "Org",
                "org",
            )
            await db.execute(
                "INSERT INTO hyper_tenants (id, name, slug, parent_id) VALUES ($1, $2, $3, $4)",
                2,
                "Team",
                "team",
                1,
            )
            await db.execute(
                "INSERT INTO hyper_tenants (id, name, slug, parent_id) VALUES ($1, $2, $3, $4)",
                3,
                "Project",
                "project",
                2,
            )

            # Get hierarchy from project level — ordering must be deterministic
            # leaf→root (3 = leaf/self, then 2, then 1 = root).
            hierarchy = await get_tenant_hierarchy(db, 3)
            assert hierarchy == [3, 2, 1], f"leaf→root order expected, got {hierarchy}"

            # Order must be stable across repeated calls (no reliance on
            # database-chosen row order).
            for _ in range(5):
                assert await get_tenant_hierarchy(db, 3) == [3, 2, 1]

            # Get hierarchy from team level
            hierarchy2 = await get_tenant_hierarchy(db, 2)
            assert hierarchy2 == [2, 1], f"leaf→root order expected, got {hierarchy2}"

            # Root has only itself
            hierarchy3 = await get_tenant_hierarchy(db, 1)
            assert hierarchy3 == [1]

            print("  PASS: Tenant hierarchy CTE")
        finally:
            await db.execute("DROP TABLE IF EXISTS hyper_tenants CASCADE")
            await db.disconnect()

    run_async(run())


# ── TenantRef immutability ─────────────────────────────────────────────


def test_tenant_ref_frozen():
    """TenantRef is frozen — cannot be mutated."""
    ref = TenantRef(tenant_id=42)
    try:
        ref.tenant_id = 99
        assert False, "Should raise FrozenInstanceError"
    except AttributeError:
        pass
    print("  PASS: TenantRef is frozen")


# ── Middleware tests ──────────────────────────────────────────────────────


def test_middleware_sets_context():
    """TenantMiddleware sets tenant context and resets on exit."""
    from hyperdjango.response import Response

    async def handler(request):
        t = get_tenant()
        return Response.json({"tenant_id": t.tenant_id if t else None})

    mw = TenantMiddleware(resolve_tenant=lambda r: TenantRef(tenant_id=42))

    async def run():
        async def call_next(req):
            return await handler(req)

        resp = await mw(FakeRequest(), call_next)
        # After middleware, context should be reset
        assert get_tenant() is None
        return resp

    resp = run_async(run())
    assert resp.status == 200
    print("  PASS: Middleware sets and resets context")


def test_middleware_none_resolver():
    """Middleware with None resolver result sets no tenant."""
    mw = TenantMiddleware(resolve_tenant=lambda r: None)

    async def run():
        async def handler(req):
            assert get_tenant() is None
            return FakeRequest()  # dummy response

        return await mw(FakeRequest(), handler)

    run_async(run())
    print("  PASS: Middleware None resolver")


def test_middleware_bad_return_type():
    """Middleware FAILS CLOSED on a bad resolver return type.

    A resolver that returns a non-TenantRef, non-None value is a bug/ambiguity.
    The middleware must deny the request (403) rather than silently coerce to
    None and run the handler with NO tenant filter (cross-tenant exposure).
    """
    mw = TenantMiddleware(resolve_tenant=lambda r: 42)  # Returns int, not TenantRef

    async def run():
        async def handler(req):
            assert False, "handler must not run when resolution is ambiguous"

        return await mw(FakeRequest(), handler)

    try:
        run_async(run())
        assert False, "bad resolver return type must deny the request"
    except HTTPException as exc:
        assert exc.status_code == 403
    # Context must be clean afterwards.
    assert get_tenant() is None
    print("  PASS: Middleware fails closed on bad return type")


def test_middleware_resolution_error_denied():
    """A resolver raising TenantResolutionError → request denied (403), NOT unfiltered.

    Regression for the fail-OPEN bug: a bad X-Tenant-ID header used to coerce
    to None and expose data across tenants. It must now deny.
    """

    def bad_resolver(req):
        # Simulate resolve_from_header on a malformed X-Tenant-ID.
        return resolve_from_header(req)

    mw = TenantMiddleware(resolve_tenant=bad_resolver)

    async def run():
        async def handler(req):
            assert False, "handler must not run on resolution failure"

        return await mw(FakeRequest(headers={"x-tenant-id": "not-an-int"}), handler)

    try:
        run_async(run())
        assert False, "malformed tenant claim must deny the request"
    except HTTPException as exc:
        assert exc.status_code == 403
    assert get_tenant() is None
    print("  PASS: Middleware denies on resolution failure")


# ── Count/Update/Delete with tenant ──────────────────────────────────────


def test_count_with_tenant():
    """_build_count includes tenant_id filter."""
    with tenant_context(tenant_id=5):
        qs = TenantPost.objects.filter(status="draft")
        sql, params = qs._build_count()
        where = _where_clause(sql)
        assert "tenant_id = " in where
        assert 5 in params
    print("  PASS: COUNT with tenant filter")


def test_update_with_tenant():
    """_build_update includes tenant_id filter."""
    with tenant_context(tenant_id=5):
        qs = TenantPost.objects.filter(status="draft")
        sql, params = qs._build_update({"status": "published"})
        where = _where_clause(sql)
        assert "tenant_id = " in where
        assert 5 in params
    print("  PASS: UPDATE with tenant filter")


def test_delete_with_tenant():
    """_build_delete includes tenant_id filter."""
    with tenant_context(tenant_id=5):
        qs = TenantPost.objects.filter(status="draft")
        sql, params = qs._build_delete()
        where = _where_clause(sql)
        assert "tenant_id = " in where
        assert 5 in params
    print("  PASS: DELETE with tenant filter")


def test_select_for_update_with_tenant():
    """select_for_update + tenant filter both present."""
    with tenant_context(tenant_id=5):
        qs = TenantPost.objects.select_for_update().filter(status="draft")
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "tenant_id = " in where
        assert "FOR UPDATE" in sql
    print("  PASS: SELECT FOR UPDATE with tenant")


# ── Clone propagation ────────────────────────────────────────────────────


def test_unscoped_survives_chain():
    """_unscoped survives through filter().order_by().limit() chain."""
    with tenant_context(tenant_id=5):
        qs = TenantPost.objects.unscoped().filter(status="draft").order_by("title")
        qs._limit = 10
        sql, params = qs._build_select()
        where = _where_clause(sql)
        assert "tenant_id = " not in where  # Still unscoped
        assert "ORDER BY" in sql
        assert "LIMIT" in sql
    print("  PASS: Unscoped survives filter chain")


# ── Live DB QuerySet execution ────────────────────────────────────────────


def test_live_queryset_execution():
    """End-to-end: actual QuerySet.all() execution with tenant filtering."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        set_db(db)

        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS test_tenant_posts (
                    id SERIAL PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    title VARCHAR(200) NOT NULL,
                    status VARCHAR(20) DEFAULT 'draft'
                )
            """)
            await db.execute("DELETE FROM test_tenant_posts")

            await db.execute(
                "INSERT INTO test_tenant_posts (tenant_id, title) VALUES ($1, $2)",
                1,
                "T1 Post",
            )
            await db.execute(
                "INSERT INTO test_tenant_posts (tenant_id, title) VALUES ($1, $2)",
                2,
                "T2 Post",
            )

            # Execute via actual QuerySet
            with tenant_context(tenant_id=1):
                posts = await TenantPost.objects.filter().all()
                assert len(posts) == 1
                assert posts[0].title == "T1 Post"
                assert posts[0].tenant_id == 1

            with tenant_context(tenant_id=2):
                posts = await TenantPost.objects.filter().all()
                assert len(posts) == 1
                assert posts[0].title == "T2 Post"

            # Count
            with tenant_context(tenant_id=1):
                count = await TenantPost.objects.filter().count()
                assert count == 1

            print("  PASS: Live QuerySet.all() + count() with tenant")
        finally:
            await db.execute("DROP TABLE IF EXISTS test_tenant_posts CASCADE")
            await db.disconnect()

    run_async(run())


def main():
    tests = [
        # TenantRef + Context
        test_tenant_ref_creation,
        test_context_set_get_clear,
        test_tenant_context_manager,
        test_tenant_context_nesting,
        test_tenant_context_thread_isolation,
        test_tenant_ref_frozen,
        # TenantQuerySet
        test_queryset_no_context_fails_closed,
        test_queryset_no_context_nonstrict_opt_out,
        test_create_forces_context_tenant_over_forged_id,
        test_update_forces_context_tenant,
        test_create_no_context_strict_raises,
        test_create_no_context_explicit_id_allowed,
        test_cache_key_isolated_per_tenant,
        test_queryset_with_context,
        test_queryset_unscoped,
        test_non_tenant_model_unaffected,
        # Composition
        test_tenant_plus_softdelete_composition,
        test_composed_unscoped,
        test_composed_with_deleted,
        # Resolvers
        test_resolve_from_user,
        test_resolve_from_header,
        test_make_subdomain_resolver,
        test_resolve_from_url,
        # Middleware
        test_middleware_sets_context,
        test_middleware_none_resolver,
        test_middleware_bad_return_type,
        test_middleware_resolution_error_denied,
        # Admin injection
        test_inject_tenant_condition_with_tenant,
        test_inject_tenant_condition_no_tenant,
        test_inject_tenant_condition_non_tenant_model,
        # Count/Update/Delete
        test_count_with_tenant,
        test_update_with_tenant,
        test_delete_with_tenant,
        test_select_for_update_with_tenant,
        # Clone propagation
        test_unscoped_survives_chain,
        # Live DB
        test_live_queryset_execution,
        test_live_tenant_filtering,
        test_live_tenant_hierarchy,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Multi-Tenant System Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
