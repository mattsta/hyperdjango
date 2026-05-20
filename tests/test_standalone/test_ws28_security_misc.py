"""Regression tests for the ws28 security + DoS-hardening fixes.

Covers, one lock-in test per issue:

1. [mass-assignment]  ModelForm whose Meta sets neither fields nor exclude must
   raise at class creation (else it binds EVERY writable model field, e.g.
   is_staff / is_superuser, straight from the request body).
2. [open-redirect]    Redirect targets must reject `/\\evil.com`, `//evil.com`,
   `https:evil`, embedded credentials, etc. — a legit relative path is allowed.
3. [admin access]     Admin GET handlers must enforce per-model `can_view`, not
   just is_staff.
4. [spoofable rl key] request.client_ip must ignore X-Forwarded-For by default
   and honor it only behind configured trusted proxies.
5. [GET field cap]    DATA_UPLOAD_MAX_NUMBER_FIELDS must also bound the parsed
   query string, not just form bodies.
6. [admin DoS caps]   Admin ?q search length is capped and a huge ?page is
   clamped to the real last page (no runaway OFFSET).
7. [rl bucket growth] The in-memory rate-limit bucket dict is hard-bounded and
   LRU-evicts, so many distinct keys can't OOM the process.

Run: uv run pytest tests/test_standalone/test_ws28_security_misc.py -q
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import hyperdjango.conf as conf
from hyperdjango.admin import HyperAdmin
from hyperdjango.exceptions import HTTPException
from hyperdjango.forms import ModelForm
from hyperdjango.models import Field, Model
from hyperdjango.ratelimit import InMemoryRateLimitBackend
from hyperdjango.redirects import (
    Redirect,
    RedirectRegistry,
    _is_safe_relative_target,
)
from hyperdjango.request import Request

# ── Shared test model ────────────────────────────────────────────────────────


class _MFUser(Model):
    class Meta:
        table = "ws28_mf_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(max_length=100)
    is_staff: bool = Field(default=False)
    is_superuser: bool = Field(default=False)


# ── 1. Mass-assignment: ModelForm without fields/exclude is prohibited ────────


class TestMassAssignmentForm:
    def test_modelform_without_fields_or_exclude_raises(self):
        with pytest.raises(ValueError, match="fields.*exclude|exclude.*fields"):
            # Class creation triggers __init_subclass__, which must reject this.
            type(
                "UnsafeUserForm",
                (ModelForm,),
                {"Meta": type("Meta", (), {"model": _MFUser})},
            )

    def test_modelform_with_explicit_fields_ignores_undeclared_keys(self):
        class SafeUserForm(ModelForm):
            class Meta:
                model = _MFUser
                fields = ["username"]

        # Only the allow-listed field is declared; is_staff/is_superuser are not.
        assert "username" in SafeUserForm._declared_fields
        assert "is_staff" not in SafeUserForm._declared_fields
        assert "is_superuser" not in SafeUserForm._declared_fields

        # A hostile body trying to set is_superuser is dropped, not bound.
        form = SafeUserForm(
            data={"username": "mallory", "is_superuser": "true", "is_staff": "1"}
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data == {"username": "mallory"}
        assert "is_superuser" not in form.cleaned_data
        assert "is_staff" not in form.cleaned_data

    def test_modelform_fields_all_opt_in_still_works(self):
        class AllForm(ModelForm):
            class Meta:
                model = _MFUser
                fields = "__all__"

        # Explicit opt-in binds all writable fields (auto PK excluded).
        assert "username" in AllForm._declared_fields
        assert "is_staff" in AllForm._declared_fields
        assert "id" not in AllForm._declared_fields


# ── 2. Open redirect ─────────────────────────────────────────────────────────


class TestOpenRedirect:
    BAD = [
        "/\\evil.com",  # backslash trick (browsers normalise \ -> /)
        "//evil.com",  # protocol-relative
        "https:evil",  # scheme-only
        "http://evil.com",  # absolute
        "//user:pass@evil.com",  # embedded credentials
        "javascript:alert(1)",  # non-relative scheme
        "",  # empty
    ]

    def test_helper_rejects_unsafe_targets(self):
        for url in self.BAD:
            assert not _is_safe_relative_target(url), url

    def test_helper_allows_legit_relative_path(self):
        for url in ("/new-page/", "/a/b/c?x=1#frag", "/"):
            assert _is_safe_relative_target(url), url

    def test_registry_add_rejects_unsafe_targets(self):
        r = RedirectRegistry()
        for url in self.BAD:
            with pytest.raises(ValueError):
                r.add("/old/", url)

    def test_registry_add_allows_relative(self):
        r = RedirectRegistry()
        r.add("/old/", "/new/")
        assert r.lookup("/old/") == ("/new/", 301)

    def test_registry_add_allows_external_when_opted_in(self):
        r = RedirectRegistry()
        r.add("/out/", "https://good.example.com/", allow_external=True)
        assert r.lookup("/out/") == ("https://good.example.com/", 301)

    async def test_load_all_skips_disguised_protocol_relative(self):
        r = RedirectRegistry()
        loaded = await r.load_all(
            [
                Redirect(old_path="/ok/", new_path="/safe/"),
                Redirect(old_path="/bad1/", new_path="//evil.com"),
                Redirect(old_path="/bad2/", new_path="/\\evil.com"),
                # A genuine external target (real scheme) is left intact.
                Redirect(old_path="/ext/", new_path="https://good.example.com/"),
            ]
        )
        assert loaded == 2  # only /ok/ and /ext/ survive
        assert r.lookup("/ok/") == ("/safe/", 301)
        assert r.lookup("/bad1/") is None
        assert r.lookup("/bad2/") is None
        assert r.lookup("/ext/") == ("https://good.example.com/", 301)


# ── 3. Admin per-model can_view enforcement ──────────────────────────────────


def _bare_admin():
    admin = HyperAdmin.__new__(HyperAdmin)
    admin._require_auth = True
    admin.title = "Admin"
    admin.prefix = "/admin"
    admin._models = {}
    return admin


class TestAdminViewPermission:
    async def test_staff_without_can_view_gets_403(self):
        admin = _bare_admin()
        config = SimpleNamespace(slug="users")
        # Staff, not superuser, permission set loaded WITHOUT view_users.
        user = {
            "id": 7,
            "is_staff": True,
            "is_superuser": False,
            "_permissions": {"change_users"},
        }
        request = SimpleNamespace(_admin_user=user)

        denied = await admin._require_view_or_403(config, request)
        assert denied is not None
        assert denied.status == 403

    async def test_staff_with_can_view_allowed(self):
        admin = _bare_admin()
        config = SimpleNamespace(slug="users")
        user = {
            "id": 7,
            "is_staff": True,
            "is_superuser": False,
            "_permissions": {"view_users"},
        }
        request = SimpleNamespace(_admin_user=user)

        assert await admin._require_view_or_403(config, request) is None

    async def test_superuser_allowed(self):
        admin = _bare_admin()
        config = SimpleNamespace(slug="users")
        user = {"id": 1, "is_staff": True, "is_superuser": True}
        request = SimpleNamespace(_admin_user=user)

        assert await admin._require_view_or_403(config, request) is None


# ── 4. Spoofable rate-limit key: client_ip trusted-proxy gating ──────────────


def _req_with_xff(xff="1.2.3.4", peer="203.0.113.9"):
    return Request(
        headers={"x-forwarded-for": xff},
        scope={"client": (peer, 4444)},
    )


class TestClientIpRequest:
    def test_xff_ignored_by_default(self):
        req = _req_with_xff()
        # Default TRUSTED_PROXY_COUNT=0, TRUSTED_PROXIES=[] -> use socket peer.
        assert req.client_ip == "203.0.113.9"
        assert req.peer_ip == "203.0.113.9"

    def test_xreal_ip_ignored_by_default(self):
        req = Request(
            headers={"x-real-ip": "9.9.9.9"},
            scope={"client": ("203.0.113.9", 4444)},
        )
        assert req.client_ip == "203.0.113.9"

    def test_xff_honored_with_trusted_proxy_count(self):
        with patch.dict(conf.DEFAULTS, {"TRUSTED_PROXY_COUNT": 1}):
            req = _req_with_xff(xff="1.2.3.4")
            assert req.client_ip == "1.2.3.4"

    def test_xff_hop_selection_with_multiple_proxies(self):
        # chain: client, edge, internal ; 2 trusted hops -> client is parts[-2].
        with patch.dict(conf.DEFAULTS, {"TRUSTED_PROXY_COUNT": 2}):
            req = _req_with_xff(xff="1.1.1.1, 2.2.2.2, 3.3.3.3")
            assert req.client_ip == "2.2.2.2"

    def test_xff_honored_when_peer_is_trusted_proxy(self):
        with patch.dict(conf.DEFAULTS, {"TRUSTED_PROXIES": ["203.0.113.9"]}):
            req = _req_with_xff(xff="1.2.3.4, 5.6.7.8")
            # Peer allow-listed, no count -> original client is the left-most.
            assert req.client_ip == "1.2.3.4"

    def test_untrusted_peer_not_in_list_ignores_xff(self):
        with patch.dict(conf.DEFAULTS, {"TRUSTED_PROXIES": ["10.0.0.1"]}):
            req = _req_with_xff(peer="203.0.113.9")
            assert req.client_ip == "203.0.113.9"


# ── 5. Unbounded GET field count ─────────────────────────────────────────────


class TestQueryFieldCapRequest:
    def test_query_string_field_cap_enforced(self):
        qs = "&".join(f"f{i}=1" for i in range(50))
        req = Request(path="/", query_string=qs)
        with (
            patch.dict(conf.DEFAULTS, {"DATA_UPLOAD_MAX_NUMBER_FIELDS": 5}),
            pytest.raises(HTTPException) as ei,
        ):
            _ = req.query_params
        assert ei.value.status_code == 400

    def test_query_string_under_cap_ok(self):
        req = Request(path="/", query_string="a=1&b=2&c=3")
        with patch.dict(conf.DEFAULTS, {"DATA_UPLOAD_MAX_NUMBER_FIELDS": 5}):
            params = req.query_params
        assert params["a"] == ["1"]


# ── 6. Admin DoS caps: search length + page clamp ────────────────────────────


class _Meta:
    pk_field = "id"
    column_names = ["id", "name"]
    table = "ws28_widgets"


class _WidgetModel:
    _meta = _Meta()


class _CaptureDB:
    def __init__(self, count):
        self._count = count
        self.calls = []

    async def query_val(self, sql, *params):
        self.calls.append(("query_val", sql, params))
        return self._count

    async def query(self, sql, *params):
        self.calls.append(("query", sql, params))
        return []


def _list_config():
    return SimpleNamespace(
        model_class=_WidgetModel,
        ordering="id",
        get_queryset=None,
        searchable_fields=["name"],
        list_filter=[],
        date_hierarchy=None,
        show_full_result_count=True,
        per_page=10,
        get_list_display=None,
        display_columns=[],
        _field_by_name={},
        _fk_display_col_cache={},
        list_editable=[],
        list_display_links=None,
        empty_value_display="-",
        actions=[],
        sortable_by=None,
        name="Widget",
        slug="ws28_widgets",
    )


def _list_admin(db):
    admin = HyperAdmin.__new__(HyperAdmin)
    admin._require_auth = True
    admin.title = "Admin"
    admin.prefix = "/admin"
    admin._models = {}
    admin._get_db = lambda: db
    return admin


def _superuser_request(get):
    return SimpleNamespace(GET=get, _admin_user={"is_superuser": True})


class TestAdminSearchAndPage:
    async def test_search_query_length_capped(self):
        db = _CaptureDB(count=0)
        admin = _list_admin(db)
        config = _list_config()
        long_q = "x" * 500
        req = _superuser_request({"q": long_q})

        ctx = await admin._build_list_context(config, req)

        # The rendered/echoed search is capped to 200 chars ...
        assert len(ctx["search_query"]) == 200
        # ... and the ILIKE parameter is likewise bounded ('%' + 200 + '%').
        ilike_params = [
            p
            for _, _sql, params in db.calls
            for p in params
            if isinstance(p, str) and p.startswith("%") and p.endswith("%")
        ]
        assert ilike_params, "expected an ILIKE search parameter"
        assert all(len(p) == 202 for p in ilike_params)

    async def test_huge_page_clamped_to_last_page(self):
        # 5 rows / 10 per page -> exactly 1 page; page=1e9 must clamp to 1.
        db = _CaptureDB(count=5)
        admin = _list_admin(db)
        config = _list_config()
        req = _superuser_request({"page": "1000000000"})

        ctx = await admin._build_list_context(config, req)

        assert ctx["page"] == 1
        data_sql = next(sql for kind, sql, _ in db.calls if kind == "query")
        assert "OFFSET 0" in data_sql


# ── 7. Rate-limit bucket growth is bounded ───────────────────────────────────


class TestRatelimitBucketBound:
    def test_bucket_count_hard_capped_lru(self):
        # Global cap 160 -> per-shard cap max(1, 160//16)=10 across 16 shards.
        backend = InMemoryRateLimitBackend(max_buckets=160)
        for i in range(5000):
            backend.check_and_increment(f"ip:{i}", max_requests=100, window=60)
        total = sum(len(shard) for shard in backend._shards)
        assert total <= 160, f"bucket dict grew unbounded: {total}"
        # Sanity: it actually filled up (eviction happened), not stayed tiny.
        assert total >= 16

    def test_uncapped_backend_still_works(self):
        backend = InMemoryRateLimitBackend(max_buckets=0)  # disabled cap
        allowed, remaining, _ = backend.check_and_increment("ip:x", 2, 60)
        assert allowed and remaining == 1
