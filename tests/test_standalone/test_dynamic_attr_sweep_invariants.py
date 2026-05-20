"""Invariants that back the getattr/setattr enforcement sweep (INFRA group).

The sweep replaced defensive ``getattr(obj, "attr", default)`` calls with direct
attribute access wherever the attribute is a *guaranteed* field/property of the
known type. These tests lock in the presence/type facts those removals rely on,
so a future refactor that renames or drops one of these attributes fails loudly
here instead of silently resurrecting the slop the sweep removed.

They also document the flip side: a handful of getattr sites were *kept* (and
justified) because the attribute is genuinely optional/duck-typed. The relevant
"absence is real" facts are asserted too.
"""

from dataclasses import fields as dataclass_fields

from hyperdjango.models import Field, Model, TableMeta
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.validation.core.fields import FieldInfo

# --- Request: direct-access removals (logging/profiling/ratelimit/cache/tenancy) ---


def test_request_has_guaranteed_direct_access_attributes():
    """method/path/query_string/user/session are always present on a Request.

    The sweep turned getattr(request, "method"/"path"/...) into direct access
    in AccessLogMiddleware, profile_handler, ip_key, the rate-limit middleware,
    CacheMiddleware._make_key and the tenancy middleware. That is only safe
    because these are declared dataclass fields with defaults.
    """
    req = Request()
    assert req.method == "GET"  # field default, __post_init__ upper()s it
    assert req.path == "/"
    assert req.query_string == ""
    assert req.user is None  # user: Any = None
    assert req.session is None  # session: Any = None


def test_request_client_ip_is_always_available_property():
    """client_ip is a property (never absent), so `request.client_ip` is safe.

    Backs the ip_key() and AccessLogMiddleware removals of
    getattr(request, "client_ip", ...).
    """
    assert isinstance(type(Request).__dict__.get("client_ip"), property) or hasattr(
        Request, "client_ip"
    )
    # Falls back to 127.0.0.1 with no headers/scope — always returns a str.
    assert Request().client_ip == "127.0.0.1"


def test_request_has_request_id_field():
    """request_id IS now a declared core field (default None).

    It is minted at the dispatch boundary (honoring an inbound X-Request-ID /
    traceparent) and echoed as the X-Request-ID response header, so it must be a
    real field — never a setattr — and AccessLogMiddleware reads it via direct
    ``request.request_id`` access (no getattr). This flipped a prior invariant:
    request_id used to be optional/injected; it is now always present.
    """
    field_names = {f.name for f in dataclass_fields(Request)}
    assert "request_id" in field_names
    assert Request().request_id is None  # default until the dispatch boundary mints it


# --- Response: the kept status_code getattr guard ---


def test_response_uses_status_not_status_code():
    """hyperdjango Response exposes `.status`, never `.status_code`.

    AccessLogMiddleware / CacheMiddleware read the status via a *duck-typed*
    getattr chain because the middleware-chain return value may be a hyperdjango
    Response (.status) or a foreign response object (.status_code). This asserts
    the hyperdjango side of that contract so the justification stays accurate.
    """
    resp = Response.text("hi")
    assert resp.status == 200
    field_names = {f.name for f in dataclass_fields(Response)}
    assert "status" in field_names
    assert "status_code" not in field_names
    assert not hasattr(resp, "status_code")


# --- migrations: model._meta.pk_field removal ---


def test_tablemeta_pk_field_always_present():
    """TableMeta.pk_field is a declared str field — backs the migrations removal
    of getattr(model._meta, "pk_field", None)."""
    tm_fields = {f.name for f in dataclass_fields(TableMeta)}
    assert "pk_field" in tm_fields

    class _SweepModel(Model):
        class Meta:
            table = "sweep_invariants_model"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()

    assert isinstance(_SweepModel._meta.pk_field, str)
    assert _SweepModel._meta.pk_field == "id"


# --- migrations: the kept field_info getattr guards ---


def test_fieldinfo_declares_db_type_and_big_but_dict_may_hold_plain_defaults():
    """FieldInfo declares db_type/big/vector_dimensions/max_length, yet a model's
    __dict__ entry for an annotated attribute can be a *plain default* (not a
    FieldInfo) — which is why migrations._get_type keeps
    getattr(field_info, "db_type"/"big", ...).
    """
    fi_fields = {f.name for f in dataclass_fields(FieldInfo)}
    assert {"db_type", "big", "vector_dimensions", "max_length"} <= fi_fields

    class _SweepModel2(Model):
        class Meta:
            table = "sweep_invariants_model2"

        id: int = Field(primary_key=True, auto=True)
        tagged: str = Field(db_type="citext")
        plain: str = "just-a-default"  # no Field() wrapper

    # Field()-wrapped attribute stores a FieldInfo with the declared fields.
    tagged = _SweepModel2.__dict__.get("tagged")
    assert isinstance(tagged, FieldInfo)
    assert tagged.db_type == "citext"

    # A plain default stores the raw value — NOT a FieldInfo. getattr(..., None)
    # is therefore load-bearing (direct .db_type would raise AttributeError).
    plain = _SweepModel2.__dict__.get("plain")
    assert not isinstance(plain, FieldInfo)
    assert getattr(plain, "db_type", None) is None
