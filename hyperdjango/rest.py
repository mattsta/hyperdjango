"""
REST API framework — ViewSets, ModelSerializer, APIRouter, filtering, pagination.

DRF-equivalent REST layer natively integrated with HyperDjango's Model, QuerySet,
Serializer, Router, and auth systems. Zero Django dependency.

Usage:
    from hyperdjango.rest import (
        ModelSerializer, ModelViewSet, APIRouter,
        PageNumberPagination, FieldFilter, SearchFilter, OrderingFilter,
        IsAuthenticated, action,
    )

    class PostSerializer(ModelSerializer):
        class Meta:
            model = Post
            fields = "__all__"
            read_only_fields = ["id"]

    class PostViewSet(ModelViewSet):
        serializer_class = PostSerializer
        model = Post
        permission_classes = [IsAuthenticated]
        pagination_class = PageNumberPagination
        filter_backends = [FieldFilter, SearchFilter, OrderingFilter]
        filterset_fields = ["status"]
        search_fields = ["title", "content"]
        ordering_fields = ["id", "title"]
        ordering = ["-id"]

        @action(methods=["POST"], detail=True, url_path="publish")
        async def publish(self, request, **kwargs):
            post = await self.get_object()
            post.status = "published"
            await post.save()
            return Response.json(self.get_serializer(obj=post).data)

    router = APIRouter(prefix="/api/v1")
    router.register("posts", PostViewSet)
    router.mount(app.router, namespace="api")
"""

import base64
import contextlib
import csv
import datetime
import decimal
import hashlib
import inspect
import io
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from hyperdjango.conf import (
    CONTENT_TYPE_FORM,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MULTIPART,
    DEFAULT_MAX_PAGE_SIZE,
    DEFAULT_PAGE_SIZE,
    HEADER_CONTENT_TYPE,
    MAX_REGEX_LENGTH,
    MAX_SEARCH_LENGTH,
    get_setting,
    parse_bool,
)
from hyperdjango.database import get_db
from hyperdjango.exceptions import HTTPException, exception_to_response
from hyperdjango.lookups import _escape_like
from hyperdjango.models import Model, _field_to_sql_type, _singularize
from hyperdjango.native import fast_json_dumps
from hyperdjango.native._crypto import (
    hmac_sha256_hex_truncated,
    hmac_sha256_verify_truncated,
)
from hyperdjango.openapi import serializer_to_schema
from hyperdjango.paginator import EmptyPage, Paginator
from hyperdjango.postgres import (
    _TSQUERY_FUNC_MAP,
    SearchMatch,
    SearchQuery,
    SearchRank,
    SearchVector,
)
from hyperdjango.public_id import (
    IDMode,
    IDStrategy,
    PublicIDMixin,
)
from hyperdjango.query import _get_model_by_table
from hyperdjango.ratelimit import (
    DatabaseRateLimitBackend,
    InMemoryRateLimitBackend,
)
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.router import Router
from hyperdjango.serializers import (
    SerializedData,
    Serializer,
    SerializerFieldInfo,
    SerializerMeta,
)
from hyperdjango.sqlident import validate_column_path
from hyperdjango.validation.core.fields import _MISSING, FieldInfo
from hyperdjango.views import View

_logger = logging.getLogger("hyperdjango.rest")

# Frozenset constants for ID mode/strategy membership checks
_SIGNED_OR_ENCODED_MODES = frozenset({IDMode.SIGNED, IDMode.ENCODED})
_SIGNED_ENCODED_RAW_MODES = frozenset({IDMode.SIGNED, IDMode.ENCODED, IDMode.RAW})
_RANDOM_STRATEGIES = frozenset({IDStrategy.RANDOM, IDStrategy.UUID7})
_ENCODE_PK_STRATEGIES = frozenset(
    {IDMode.SIGNED, IDMode.ENCODED, IDStrategy.ENCODED_PK}
)

__all__ = [
    # Exceptions
    "APIException",
    "ValidationError",
    "AuthenticationFailed",
    "PermissionDenied",
    "NotFound",
    "MethodNotAllowed",
    "Throttled",
    "Conflict",
    "handle_api_exception",
    # Permissions
    "BasePermission",
    "AllowAny",
    "IsAuthenticated",
    "IsAdminUser",
    "IsAuthenticatedOrReadOnly",
    "ModelPermission",
    "ObjectPermission",
    # Filter backends
    "FilterBackend",
    "FieldFilter",
    "SearchFilter",
    "FullTextSearchFilter",
    "SearchRankOrderingFilter",
    "OrderingFilter",
    # Pagination
    "PaginatedResponse",
    "APIPagination",
    "PageNumberPagination",
    "LimitOffsetPagination",
    "CursorPagination",
    "ServerCursorPagination",
    "cleanup_expired_server_cursors",
    # Relational fields
    "PrimaryKeyRelatedField",
    "SlugRelatedField",
    # Serializer
    "ModelSerializerMeta",
    "ModelSerializer",
    # Versioning
    "APIVersioning",
    "URLPathVersioning",
    "HeaderVersioning",
    "QueryParamVersioning",
    # Throttling
    "BaseThrottle",
    "SimpleRateThrottle",
    "AnonRateThrottle",
    "UserRateThrottle",
    "ScopedRateThrottle",
    "DatabaseThrottle",
    "DatabaseAnonThrottle",
    "DatabaseUserThrottle",
    "DatabaseScopedThrottle",
    # Authentication
    "AuthResult",
    "BaseAuthentication",
    "SessionAuthentication",
    "APIKeyAuthentication",
    "TokenAuthentication",
    # Parsers
    "BaseParser",
    "JSONParser",
    "FormParser",
    "MultiPartParser",
    # Renderers
    "BaseRenderer",
    "JSONRenderer",
    "CSVRenderer",
    # Mixins
    "CacheableMixin",
    "MeteringMixin",
    "NestedViewSetMixin",
    # Typed fields
    "SerializerMethodField",
    "TypedField",
    "DateTimeField",
    "DateField",
    "TimeField",
    "ChoiceField",
    "MultipleChoiceField",
    "UUIDField",
    "DecimalField",
    "EmailField",
    "URLField",
    "IPAddressField",
    "ReadOnlyField",
    "HiddenField",
    "FileUploadField",
    "ImageUploadField",
    # Defaults and metadata
    "CurrentUserDefault",
    "SimpleMetadata",
    # Action decorator
    "action",
    "ActionMeta",
    # Views and ViewSets
    "ViewSet",
    "GenericAPIView",
    # CRUD mixins
    "ListMixin",
    "CreateMixin",
    "RetrieveMixin",
    "UpdateMixin",
    "DestroyMixin",
    # Bulk mixins
    "BulkCreateMixin",
    "BulkUpdateMixin",
    "BulkDestroyMixin",
    # Composite ViewSets
    "ModelViewSet",
    "ReadOnlyModelViewSet",
    "BulkModelViewSet",
    # Shortcut views
    "CreateAPIView",
    "ListAPIView",
    "RetrieveAPIView",
    "DestroyAPIView",
    "UpdateAPIView",
    "ListCreateAPIView",
    "RetrieveUpdateAPIView",
    "RetrieveUpdateDestroyAPIView",
    "RetrieveDestroyAPIView",
    # Router
    "APIRouter",
    "ViewSetRegistration",
    "NestedRouter",
]

# ── Constants ─────────────────────────────────────────────────────────────────

CURSOR_DIRECTIONS = frozenset({"next", "prev"})

# ── Exceptions ────────────────────────────────────────────────────────────────


class APIException(HTTPException):
    """Base exception for REST API errors.

    Subclasses the framework's single ``HTTPException`` base so a REST error
    raised anywhere — inside a viewset OR from a plain handler, middleware, or
    serializer — is mapped identically by ``exception_to_response``. The
    ``(detail, status_code, errors)`` constructor signature is preserved; the
    per-subclass ``default_status_code`` supplies the status when omitted.
    """

    default_status_code: int = 400

    def __init__(
        self,
        detail: str,
        status_code: int | None = None,
        errors: dict[str, list[str]] | None = None,
        headers: dict[str, str] | None = None,
    ):
        code = status_code if status_code is not None else self.default_status_code
        super().__init__(code, detail, headers=headers, errors=errors)

    def __str__(self) -> str:
        return self.detail


class ValidationError(APIException):
    """Input validation failed (400)."""

    default_status_code: int = 400


class AuthenticationFailed(APIException):
    """Authentication credentials missing or invalid (401)."""

    default_status_code: int = 401


class PermissionDenied(APIException):
    """User lacks required permissions (403)."""

    default_status_code: int = 403


class NotFound(APIException):
    """Requested resource does not exist (404)."""

    default_status_code: int = 404


class MethodNotAllowed(APIException):
    """HTTP method not allowed on this endpoint (405)."""

    default_status_code: int = 405


class NotAcceptable(APIException):
    """No response renderer can satisfy the request's Accept header (406)."""

    default_status_code: int = 406


class Throttled(APIException):
    """Rate limit exceeded (429)."""

    default_status_code: int = 429


class Conflict(APIException):
    """Request conflicts with the current state of the resource (409).

    Used when a server-side cursor is already being replayed by another
    concurrent request: a second FETCH on the same pinned connection would
    interleave the PostgreSQL wire protocol, so we reject rather than corrupt.
    """

    default_status_code: int = 409


def handle_api_exception(exc: APIException) -> Response:
    """Convert an APIException to a structured JSON response.

    Thin delegate to the single framework-wide mapper so viewset error bodies
    are byte-for-byte identical to those emitted on plain-handler and middleware
    paths (and so ``exc.headers`` — e.g. a Throttled ``Retry-After`` — survive).
    """
    return exception_to_response(exc)


# ── Permissions ───────────────────────────────────────────────────────────────


class BasePermission:
    """Base class for permission checks. Override has_permission/has_object_permission.

    Supports composition via operators:
        IsAuthenticated & IsAdminUser     # AND: both must pass
        IsAuthenticated | IsReadOnly      # OR: either can pass
        ~IsBlacklisted                    # NOT: inverts the check
    """

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        return True

    async def has_object_permission(
        self, request: Request, view: ViewSet, obj: Any
    ) -> bool:
        return True

    def __and__(self, other: BasePermission) -> _ANDPermission:
        return _ANDPermission(self, other)

    def __or__(self, other: BasePermission) -> _ORPermission:
        return _ORPermission(self, other)

    def __invert__(self) -> _NOTPermission:
        return _NOTPermission(self)


class _ANDPermission(BasePermission):
    """Composite permission: both operands must pass."""

    def __init__(self, left: BasePermission, right: BasePermission):
        self._left = left
        self._right = right

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        return await self._left.has_permission(
            request, view
        ) and await self._right.has_permission(request, view)

    async def has_object_permission(
        self, request: Request, view: ViewSet, obj: Any
    ) -> bool:
        return await self._left.has_object_permission(
            request, view, obj
        ) and await self._right.has_object_permission(request, view, obj)


class _ORPermission(BasePermission):
    """Composite permission: either operand must pass."""

    def __init__(self, left: BasePermission, right: BasePermission):
        self._left = left
        self._right = right

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        return await self._left.has_permission(
            request, view
        ) or await self._right.has_permission(request, view)

    async def has_object_permission(
        self, request: Request, view: ViewSet, obj: Any
    ) -> bool:
        return await self._left.has_object_permission(
            request, view, obj
        ) or await self._right.has_object_permission(request, view, obj)


class _NOTPermission(BasePermission):
    """Composite permission: inverts the wrapped permission."""

    def __init__(self, wrapped: BasePermission):
        self._wrapped = wrapped

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        return not await self._wrapped.has_permission(request, view)

    async def has_object_permission(
        self, request: Request, view: ViewSet, obj: Any
    ) -> bool:
        return not await self._wrapped.has_object_permission(request, view, obj)


class AllowAny(BasePermission):
    """Allow unrestricted access."""


class IsAuthenticated(BasePermission):
    """Only allow authenticated users."""

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        # SessionAuth sets request.user = AnonymousUser() (NOT None) for anon
        # requests, and AnonymousUser.is_authenticated is False. Testing only
        # ``is not None`` would let anonymous users pass — must also require an
        # authenticated identity.
        user = request.user
        return user is not None and user.is_authenticated


class IsAdminUser(BasePermission):
    """Only allow staff/admin users.

    Checks RBAC groups first (preferred), falls back to the standard Django
    ``is_staff`` / ``is_superuser`` flags.
    """

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        user = request.user
        if user is None or not user.is_authenticated:
            return False
        # The is_staff/is_superuser flags are the documented fallback; RBAC
        # groups ("staff"/"admin") are the preferred signal. All user types
        # (User, SessionUser, AnonymousUser) expose these uniformly, so no
        # branch on user type and no AttributeError on a plain User model.
        # `is True` (not truthiness): a real bool column / SessionUser property
        # satisfies it, but a partially-hydrated User whose is_staff attribute
        # falls back to the class-level FieldInfo descriptor (truthy!) must NOT
        # silently pass this admin gate.
        if user.is_staff is True or user.is_superuser is True:
            return True
        return user.in_group("staff") or user.in_group("admin")


class IsAuthenticatedOrReadOnly(BasePermission):
    """Allow read-only access for unauthenticated, full access for authenticated."""

    _SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        if request.method in self._SAFE_METHODS:
            return True
        # Write branch: an anonymous user (AnonymousUser, not None) must not be
        # able to write/delete — require an authenticated identity.
        user = request.user
        return user is not None and user.is_authenticated


# ── Filter Backends ───────────────────────────────────────────────────────────


class FilterBackend:
    """Base class for queryset filtering. Override filter_queryset."""

    def filter_queryset(self, request: Request, queryset: Any, view: ViewSet) -> Any:
        return queryset


# A single query-parameter segment (field name or lookup) must be a plain SQL
# identifier. Anything else (quotes, operators, whitespace, dots) is rejected
# before it can be rebuilt into an ORM filter key.
_FILTER_SEGMENT_RE = re.compile(r"^[A-Za-z_]\w*$")

# Cap the number of elements a single ``__in`` query param may expand into, so
# one request cannot build an unbounded ``ANY($n)`` array (DoS).
MAX_IN_CLAUSE_ITEMS = 1000


class FieldFilter(FilterBackend):
    """Filter by exact values and lookups from query parameters.

    Maps query params to QuerySet.filter() calls:
        ?status=active       → qs.filter(status="active")
        ?price__gte=10       → qs.filter(price__gte=10)

    Only allows fields listed in view.filterset_fields.
    Supports lookups: exact, __gt, __gte, __lt, __lte, __contains,
    __icontains, __startswith, __endswith, __in, __isnull.
    """

    _ALLOWED_LOOKUPS = frozenset(
        {
            "",
            "gt",
            "gte",
            "lt",
            "lte",
            "contains",
            "icontains",
            "startswith",
            "istartswith",
            "endswith",
            "iendswith",
            "in",
            "isnull",
            "exact",
            "iexact",
            "range",
        }
    )

    def filter_queryset(self, request: Request, queryset: Any, view: ViewSet) -> Any:
        filterset_fields = view.filterset_fields
        if not filterset_fields:
            return queryset

        allowed = set(filterset_fields)
        query_params = request.GET

        filters: dict[str, Any] = {}
        for param, value in query_params.items():
            # Split param into field and lookup: "price__gte" → ("price", "gte")
            parts = param.split("__")
            # A valid param is ``field`` or ``field__lookup`` — at most two
            # segments. The previous code validated only parts[0]/parts[1] but
            # then forwarded the FULL original param string as the filter key,
            # so a trailing segment (e.g. ``status__exact__x'``) flowed into SQL
            # as an unquoted identifier — an injection / schema-oracle / 500
            # vector. Reject anything with extra segments.
            if len(parts) > 2:
                continue
            field_name = parts[0]
            lookup = parts[1] if len(parts) > 1 else ""

            if field_name not in allowed:
                continue
            if lookup not in self._ALLOWED_LOOKUPS:
                continue
            # Defensive per-segment identifier check before the key is rebuilt.
            if not _FILTER_SEGMENT_RE.match(field_name):
                continue
            if lookup and not _FILTER_SEGMENT_RE.match(lookup):
                continue

            # Type coercion for special lookups
            if lookup == "in":
                value = value.split(",")
                # One param must not expand into an unbounded ANY($n) array.
                if len(value) > MAX_IN_CLAUSE_ITEMS:
                    raise ValidationError(
                        f"'{field_name}__in' accepts at most "
                        f"{MAX_IN_CLAUSE_ITEMS} values"
                    )
            elif lookup == "isnull":
                value = parse_bool(value)

            # Rebuild the key from ONLY the validated segments — never forward
            # the raw param string.
            filter_key = f"{field_name}__{lookup}" if lookup else field_name
            filters[filter_key] = value

        if filters:
            queryset = queryset.filter(**filters)
        return queryset


class SearchFilter(FilterBackend):
    """Multi-field text search from ?search= query parameter.

    Supports prefix operators on search_fields:
        "title"     → ILIKE (default, contains)
        "^title"    → ILIKE startswith
        "=title"    → exact match (case-insensitive)
        "$title"    → regex match

    Multiple search terms are AND-ed (each term OR-ed across fields):
        ?search=django rest → (title ILIKE '%django%' OR ...) AND (title ILIKE '%rest%' OR ...)

    Quoted phrases are treated as a single term:
        ?search="django rest" → (title ILIKE '%django rest%' OR ...)
    """

    search_param: str = "search"

    # Prefix → SQL operator mapping
    _PREFIX_OPS: dict[str, str] = {
        "^": "ILIKE",  # startswith pattern: 'term%'
        "=": "ILIKE",  # exact: 'term' (no wildcards, case-insensitive)
        "$": "~*",  # regex (case-insensitive)
    }

    def filter_queryset(self, request: Request, queryset: Any, view: ViewSet) -> Any:
        search_fields = view.search_fields
        if not search_fields:
            return queryset

        raw_search = request.GET.get(self.search_param, "").strip()
        if not raw_search:
            return queryset

        # Cap length to prevent expensive patterns
        if len(raw_search) > MAX_SEARCH_LENGTH:
            raw_search = raw_search[:MAX_SEARCH_LENGTH]

        # Smart split: respect quoted phrases, split on whitespace
        terms = _smart_split(raw_search)
        if not terms:
            return queryset

        # For each term, build OR across fields, then AND the terms together
        for term in terms:
            or_parts: list[str] = []
            params: list[str] = []
            for search_field in search_fields:
                prefix = ""
                field_name = search_field
                if search_field.startswith(("^", "=", "@", "$")):
                    prefix = search_field[0]
                    field_name = search_field[1:]
                # Validate the search field (from search_fields config) before it
                # is interpolated into the ILIKE fragment — never build SQL from
                # an unchecked identifier.

                validate_column_path(field_name, source="SearchFilter")
                col = field_name.replace("__", ".")
                escaped_term = _escape_like(term)

                if prefix == "^":
                    # startswith
                    or_parts.append(f"{col} ILIKE {{idx}}")
                    params.append(f"{escaped_term}%")
                elif prefix == "=":
                    # exact (case-insensitive)
                    or_parts.append(f"{col} ILIKE {{idx}}")
                    params.append(escaped_term)
                elif prefix == "$":
                    # regex — escape user input to prevent catastrophic backtracking
                    # Only allow alphanumeric, spaces, and basic regex chars .|*+?[]()
                    # Strip quantifier stacking (a++, a**, a{1000}) that cause ReDoS
                    safe_regex = _sanitize_regex(term)
                    or_parts.append(f"{col} ~* {{idx}}")
                    params.append(safe_regex)
                else:
                    # default: contains
                    or_parts.append(f"{col} ILIKE {{idx}}")
                    params.append(f"%{escaped_term}%")

            if or_parts:
                sql_template = " OR ".join(or_parts)
                queryset = queryset.where_raw(f"({sql_template})", *params)

        return queryset


def _smart_split(search_string: str) -> list[str]:
    """Split search string into terms, respecting quoted phrases.

    "hello world" → ["hello", "world"]
    '"exact phrase" other' → ["exact phrase", "other"]
    """
    terms: list[str] = []
    current = ""
    in_quote = False
    quote_char = ""

    for char in search_string:
        if char in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = char
        elif char == quote_char and in_quote:
            in_quote = False
            if current.strip():
                terms.append(current.strip())
            current = ""
            quote_char = ""
        elif char == " " and not in_quote:
            if current.strip():
                terms.append(current.strip())
            current = ""
        else:
            current += char

    if current.strip():
        terms.append(current.strip())

    return terms


# Patterns that indicate potentially dangerous regex constructs which can drive
# catastrophic backtracking (ReDoS) in PostgreSQL's ~* operator. Covers:
#   \+\+ \*\*         stacked quantifiers (a++, a**)
#   \{\d{3,}\}        very large bounded repetition ({100+})
#   \.{5,}            excessive wildcards (5+ dots)
#   \)[*+?]           a GROUP followed by a quantifier: (a+)+, (.*)*, (a|a)+
#   \)\{              a GROUP followed by bounded repetition: (a?){20}
# The last two are the important additions: nested/grouped quantifiers such as
# `(a+)+$` are the classic exponential-backtracking payloads that the previous
# guard (which only saw literal `++`/`**`) let through.
_DANGEROUS_REGEX = re.compile(r"(\+\+|\*\*|\{\d{3,}\}|\.{5,}|\)[*+?]|\)\{)")


def _sanitize_regex(pattern: str) -> str:
    """Sanitize a user-provided regex to prevent catastrophic backtracking (ReDoS).

    Strips dangerous quantifier patterns:
    - Nested/grouped quantifiers: a++, a**, (a+)+, (.*)*, (a|a)+, (a?){20}
      (stacking causes exponential backtracking)
    - Very large repetition: a{1000} (causes excessive memory/CPU)
    - Excessive wildcards: ..... (5+ dots)

    If the pattern is dangerous, falls back to literal string matching (the term
    is re.escape'd so it can only match itself), preserving the feature without
    ever handing a backtracking-prone pattern to the database.
    """
    if _DANGEROUS_REGEX.search(pattern):
        # Fall back to escaped literal match
        return re.escape(pattern)
    # Cap regex length (tighter than ILIKE search cap)
    if len(pattern) > MAX_REGEX_LENGTH:
        pattern = pattern[:MAX_REGEX_LENGTH]
    return pattern


class OrderingFilter(FilterBackend):
    """Dynamic ordering from ?ordering= query parameter.

    Validates against view.ordering_fields whitelist:
        ?ordering=-created_at,title → qs.order_by("-created_at", "title")
    """

    ordering_param: str = "ordering"

    def filter_queryset(self, request: Request, queryset: Any, view: ViewSet) -> Any:
        ordering_param = request.GET.get(self.ordering_param, "").strip()

        if ordering_param:
            fields = [f.strip() for f in ordering_param.split(",") if f.strip()]
            valid_fields = self._validate_fields(fields, view)
            if valid_fields:
                queryset = queryset.order_by(*valid_fields)
                return queryset

        # Fall back to default ordering from view
        if view.ordering:
            queryset = queryset.order_by(*view.ordering)

        return queryset

    def _validate_fields(self, fields: list[str], view: ViewSet) -> list[str]:
        """Validate ordering fields against whitelist."""
        allowed = set(view.ordering_fields)
        if not allowed:
            return []

        valid: list[str] = []
        for f in fields:
            bare = f.lstrip("-")
            if bare in allowed:
                valid.append(f)
        return valid


class FullTextSearchFilter(FilterBackend):
    """PostgreSQL full-text search filter using tsvector/tsquery.

    Uses the @@ match operator with to_tsvector and tsquery functions for
    high-quality relevance-based text search.

    Usage:
        class PostViewSet(ModelViewSet):
            filter_backends = [FullTextSearchFilter]
            search_fields = ["title", "content"]
            search_config = "english"  # PostgreSQL text search config
            search_type = "websearch"  # plain, phrase, raw, websearch
    """

    search_param: str = "search"

    def filter_queryset(self, request: Request, queryset: Any, view: ViewSet) -> Any:
        search_term = request.GET.get(self.search_param, "").strip()
        if not search_term:
            return queryset

        search_fields = view.search_fields
        if not search_fields:
            return queryset

        config = view.search_config
        search_type = view.search_type

        # Build FTS match using Expression classes (single source of truth in postgres.py)
        vector = SearchVector(fields=list(search_fields), config=config)
        query = SearchQuery(query=search_term, config=config, search_type=search_type)
        match = SearchMatch(vector=vector, query=query)

        # Convert Expression SQL to where_raw format ({idx} placeholders)
        vector_sql, _ = vector.as_sql()
        escaped_config = config.replace("'", "''")
        query_func = _TSQUERY_FUNC_MAP.get(search_type, "websearch_to_tsquery")
        where_sql = f"({vector_sql}) @@ {query_func}('{escaped_config}', {{idx}})"
        return queryset.where_raw(where_sql, search_term)


class SearchRankOrderingFilter(FilterBackend):
    """Order results by full-text search relevance using ts_rank.

    Adds a WHERE clause with the @@ match operator and appends a ts_rank-based
    annotation to the queryset using SearchRank Expression.

    Note: When used alongside FullTextSearchFilter, this filter only adds the
    rank ordering (the match condition is already applied). When used alone,
    it adds both the match condition and the rank ordering.

    Usage:
        class PostViewSet(ModelViewSet):
            filter_backends = [FullTextSearchFilter, SearchRankOrderingFilter]
            search_fields = ["title", "content"]
            search_config = "english"
            search_type = "websearch"
    """

    search_param: str = "search"

    def filter_queryset(self, request: Request, queryset: Any, view: ViewSet) -> Any:
        search_term = request.GET.get(self.search_param, "").strip()
        if not search_term:
            return queryset

        search_fields = view.search_fields
        if not search_fields:
            return queryset

        config = view.search_config
        search_type = view.search_type

        # Build FTS expressions using Expression classes
        vector = SearchVector(fields=list(search_fields), config=config)
        query = SearchQuery(query=search_term, config=config, search_type=search_type)
        rank = SearchRank(vector=vector, query=query)

        # Add match condition + rank annotation
        vector_sql, _ = vector.as_sql()
        escaped_config = config.replace("'", "''")
        query_func = _TSQUERY_FUNC_MAP.get(search_type, "websearch_to_tsquery")
        where_sql = f"({vector_sql}) @@ {query_func}('{escaped_config}', {{idx}})"
        queryset = queryset.where_raw(where_sql, search_term)

        # Store rank expression for downstream ordering
        rank_vector_sql, _ = vector.as_sql()
        rank_sql = f"ts_rank(({rank_vector_sql}), {query_func}('{escaped_config}', {{idx}})) DESC"
        queryset._rank_expression = rank_sql
        queryset._rank_search_term = search_term
        return queryset


# ── API Pagination ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PaginatedResponse:
    """Structured pagination metadata."""

    count: int
    next: str | None
    previous: str | None
    results: list[dict[str, object]]


class APIPagination:
    """Base class for API pagination. Override paginate_queryset and get_paginated_response."""

    _count: int = 0

    # Native JSON fast path: when True the paginator implements paginate_native(),
    # letting identity-serializer list endpoints splice raw query_json bytes into
    # the pagination envelope without hydrating model instances.
    native_supported: bool = False

    async def paginate_queryset(self, queryset: Any, request: Request) -> list[object]:
        raise NotImplementedError("Subclass must implement paginate_queryset()")

    def get_paginated_response(self, data: list[dict[str, object]]) -> Response:
        raise NotImplementedError("Subclass must implement get_paginated_response()")

    @staticmethod
    def _splice_results(envelope: dict[str, object], results_json: bytes) -> Response:
        """Build a paginated JSON Response with pre-serialized ``results`` bytes.

        ``envelope`` holds every key EXCEPT ``results`` (count/next/previous, in
        output order). Its JSON is emitted with the same native encoder as
        Response.json, then the raw ``results_json`` array is spliced in as the
        final key — byte-identical to encoding the whole dict with a Python
        results list, but without ever materializing that list.
        """
        prefix = fast_json_dumps(envelope)  # b'{...}' with no results key
        body = prefix[:-1] + b',"results":' + results_json + b"}"
        return Response(body=body, status=200, content_type="application/json")

    def _build_url(self, request: Request, **params: str) -> str:
        """Build a URL with updated query params."""
        base = request.path
        existing = dict(request.GET)
        existing.update(params)
        qs_parts = [f"{k}={v}" for k, v in existing.items() if v is not None]
        if qs_parts:
            return f"{base}?{'&'.join(qs_parts)}"
        return base


class PageNumberPagination(APIPagination):
    """Standard page-number pagination.

    Query params: ?page=2&page_size=50
    Response: {"count": 100, "next": "?page=3", "previous": "?page=1", "results": [...]}
    """

    page_size: int = DEFAULT_PAGE_SIZE
    page_query_param: str = "page"
    page_size_query_param: str = "page_size"
    max_page_size: int = DEFAULT_MAX_PAGE_SIZE
    native_supported: bool = True
    _request: Request | None = None
    _page_number: int = 1
    _num_pages: int = 1

    def _resolve_page_params(self, request: Request) -> tuple[int, int]:
        """Parse (page_size, page_number) from the request query params.

        Shared by paginate_queryset() and paginate_native() so both paths
        clamp identically.
        """
        size = self.page_size
        page_size_str = request.GET.get(self.page_size_query_param)
        if page_size_str is not None and len(page_size_str) <= 10:
            with contextlib.suppress(ValueError, TypeError):
                size = min(int(page_size_str), self.max_page_size)
        if size < 1:
            size = self.page_size

        page_str = request.GET.get(self.page_query_param, "1")
        if len(page_str) > 10:
            page_num = 1
        else:
            try:
                page_num = int(page_str)
            except ValueError, TypeError:
                page_num = 1
        if page_num < 1:
            page_num = 1
        return size, page_num

    async def paginate_queryset(self, queryset: Any, request: Request) -> list[object]:
        self._request = request
        size, page_num = self._resolve_page_params(request)

        paginator = Paginator(queryset, per_page=size)
        page = await paginator.page(page_num)

        self._count = page.count
        self._page_number = page.number
        self._num_pages = page.num_pages
        return page.items

    async def paginate_native(
        self, queryset: Any, request: Request, columns_sql: str, db: Any
    ) -> Response:
        """Native JSON page: reuse Paginator's count/bounds, fetch rows as JSON.

        Mirrors paginate_queryset()+Paginator.page() exactly (same count,
        num_pages, offset/limit, and out-of-range EmptyPage behaviour) but
        emits the row window straight from db.query_json instead of hydrating
        instances, then splices the bytes into the pagination envelope.
        """
        self._request = request
        size, page_num = self._resolve_page_params(request)

        paginator = Paginator(queryset, per_page=size)
        number = paginator._validate_number(page_num)
        count = await paginator.get_count()
        num_pages = paginator._compute_num_pages(count)

        if number > num_pages:
            if number == 1 and paginator.allow_empty_first_page:
                self._count = count
                self._page_number = 1
                self._num_pages = num_pages
                return self._native_paginated_response(b"[]")
            raise EmptyPage(f"Page {number} contains no results (total: {count})")

        offset = (number - 1) * size
        limit = count - offset if number == num_pages else size

        self._count = count
        self._page_number = number
        self._num_pages = num_pages

        page_qs = queryset.offset(offset).limit(limit)
        sql, params = page_qs._build_select(columns_override=columns_sql)
        json_bytes = await db.query_json(sql, *params)
        return self._native_paginated_response(json_bytes)

    def _native_paginated_response(self, results_json: bytes) -> Response:
        next_url = None
        prev_url = None
        if self._request is not None:
            if self._page_number < self._num_pages:
                next_url = self._build_url(
                    self._request,
                    **{self.page_query_param: str(self._page_number + 1)},
                )
            if self._page_number > 1:
                prev_url = self._build_url(
                    self._request,
                    **{self.page_query_param: str(self._page_number - 1)},
                )
        return self._splice_results(
            {"count": self._count, "next": next_url, "previous": prev_url},
            results_json,
        )

    def get_paginated_response(self, data: list[dict[str, object]]) -> Response:
        next_url = None
        prev_url = None
        if self._request is not None:
            if self._page_number < self._num_pages:
                next_url = self._build_url(
                    self._request,
                    **{self.page_query_param: str(self._page_number + 1)},
                )
            if self._page_number > 1:
                prev_url = self._build_url(
                    self._request,
                    **{self.page_query_param: str(self._page_number - 1)},
                )
        body = {
            "count": self._count,
            "next": next_url,
            "previous": prev_url,
            "results": data,
        }
        return Response.json(body)


class LimitOffsetPagination(APIPagination):
    """Limit/offset pagination.

    Query params: ?limit=50&offset=100
    Response: {"count": 500, "next": "?limit=50&offset=150", "previous": ..., "results": [...]}
    """

    default_limit: int = DEFAULT_PAGE_SIZE
    limit_query_param: str = "limit"
    offset_query_param: str = "offset"
    max_limit: int = DEFAULT_MAX_PAGE_SIZE
    native_supported: bool = True
    _request: Request | None = None
    _limit: int = 25
    _offset: int = 0

    def _resolve_limit_offset(self, request: Request) -> None:
        """Parse limit/offset from query params into self._limit/_offset."""
        limit_str = request.GET.get(self.limit_query_param)
        if limit_str is not None and len(limit_str) <= 10:
            try:
                self._limit = min(int(limit_str), self.max_limit)
            except ValueError, TypeError:
                self._limit = self.default_limit
        else:
            self._limit = self.default_limit
        if self._limit < 1:
            self._limit = self.default_limit

        offset_str = request.GET.get(self.offset_query_param)
        if offset_str is not None and len(offset_str) <= 10:
            try:
                self._offset = max(int(offset_str), 0)
            except ValueError, TypeError:
                self._offset = 0
        else:
            self._offset = 0

    async def paginate_queryset(self, queryset: Any, request: Request) -> list[object]:
        self._request = request
        self._resolve_limit_offset(request)
        self._count = await queryset.count()
        items = await queryset.offset(self._offset).limit(self._limit).all()
        return items

    async def paginate_native(
        self, queryset: Any, request: Request, columns_sql: str, db: Any
    ) -> Response:
        """Native JSON page — same limit/offset + count as paginate_queryset(),
        but the row window comes straight from db.query_json."""
        self._request = request
        self._resolve_limit_offset(request)
        self._count = await queryset.count()
        page_qs = queryset.offset(self._offset).limit(self._limit)
        sql, params = page_qs._build_select(columns_override=columns_sql)
        json_bytes = await db.query_json(sql, *params)
        return self._native_paginated_response(json_bytes)

    def _native_paginated_response(self, results_json: bytes) -> Response:
        next_url = None
        prev_url = None
        if self._request is not None:
            next_offset = self._offset + self._limit
            if next_offset < self._count:
                next_url = self._build_url(
                    self._request,
                    **{
                        self.limit_query_param: str(self._limit),
                        self.offset_query_param: str(next_offset),
                    },
                )
            if self._offset > 0:
                prev_offset = max(self._offset - self._limit, 0)
                prev_url = self._build_url(
                    self._request,
                    **{
                        self.limit_query_param: str(self._limit),
                        self.offset_query_param: str(prev_offset),
                    },
                )
        return self._splice_results(
            {"count": self._count, "next": next_url, "previous": prev_url},
            results_json,
        )

    def get_paginated_response(self, data: list[dict[str, object]]) -> Response:
        next_url = None
        prev_url = None
        if self._request is not None:
            next_offset = self._offset + self._limit
            if next_offset < self._count:
                next_url = self._build_url(
                    self._request,
                    **{
                        self.limit_query_param: str(self._limit),
                        self.offset_query_param: str(next_offset),
                    },
                )
            if self._offset > 0:
                prev_offset = max(self._offset - self._limit, 0)
                prev_url = self._build_url(
                    self._request,
                    **{
                        self.limit_query_param: str(self._limit),
                        self.offset_query_param: str(prev_offset),
                    },
                )
        body = {
            "count": self._count,
            "next": next_url,
            "previous": prev_url,
            "results": data,
        }
        return Response.json(body)


class CursorPagination(APIPagination):
    """Cursor-based (keyset) pagination for large datasets.

    Uses a base64-encoded cursor with type-tagged values for proper round-tripping
    of datetime, integer, string, and decimal ordering fields. More efficient than
    OFFSET for large tables — no COUNT query, no OFFSET scanning.

    Query params: ?cursor=base64token
    Response: {"next": "?cursor=...", "previous": "?cursor=...", "results": [...]}
    """

    page_size: int = DEFAULT_PAGE_SIZE
    cursor_query_param: str = "cursor"
    ordering: str = "-id"
    _request: Request | None = None
    _has_next: bool = False
    _has_previous: bool = False
    _next_cursor: str | None = None
    _prev_cursor: str | None = None

    @staticmethod
    def _reverse_ordering(ordering: str) -> str:
        """Flip an ordering token's direction ('-id' <-> 'id')."""
        return ordering[1:] if ordering.startswith("-") else f"-{ordering}"

    async def paginate_queryset(self, queryset: Any, request: Request) -> list[object]:
        self._request = request

        # Determine ordering field and direction
        order_field = self.ordering
        descending = order_field.startswith("-")
        if descending:
            order_field = order_field[1:]

        cursor_str = request.GET.get(self.cursor_query_param)
        direction = "next"  # default: forward
        cursor_value = None

        if cursor_str:
            result = _decode_cursor(cursor_str)
            if result is not None:
                direction, cursor_value = result
            else:
                # Invalid or tampered cursor — ignore and start from beginning
                cursor_value = None

            if cursor_value is not None:
                if direction == "next":
                    if descending:
                        queryset = queryset.filter(
                            **{f"{order_field}__lt": cursor_value}
                        )
                    else:
                        queryset = queryset.filter(
                            **{f"{order_field}__gt": cursor_value}
                        )
                else:
                    if descending:
                        queryset = queryset.filter(
                            **{f"{order_field}__gt": cursor_value}
                        )
                    else:
                        queryset = queryset.filter(
                            **{f"{order_field}__lt": cursor_value}
                        )

        # Backward ("prev") paging is keyset-symmetric: a forward-ordered scan
        # after the reversed filter would return the FIRST matching region (i.e.
        # the first page) instead of the page immediately preceding the cursor.
        # So for prev we scan in the REVERSED ordering (rows nearest the cursor
        # come first), take page_size(+1), then flip back to display order.
        is_prev = cursor_value is not None and direction == "prev"

        # Apply ordering
        queryset = queryset.order_by(
            self._reverse_ordering(self.ordering) if is_prev else self.ordering
        )

        # Fetch one extra to detect a further page in the scan direction.
        items = await queryset.limit(self.page_size + 1).all()
        has_more = len(items) > self.page_size
        if has_more:
            items = items[: self.page_size]

        if is_prev:
            # Rows came back nearest-cursor-first in reversed order; flip to the
            # normal display order. The extra row (has_more) means a page exists
            # BEFORE this window; a next page always exists (we came from one).
            items = list(reversed(items))
            self._has_previous = has_more
            self._has_next = True
        else:
            self._has_next = has_more
            self._has_previous = cursor_str is not None

        # Build cursors from boundary items
        if items:
            last_item = items[-1]
            last_value = (
                last_item.get(order_field)
                if isinstance(last_item, dict)
                # dynamic-attr: order_field is a runtime-configured model attribute name for cursor pagination
                else getattr(last_item, order_field, None)
            )
            if last_value is not None and self._has_next:
                self._next_cursor = _encode_cursor("next", last_value)

            first_item = items[0]
            first_value = (
                first_item.get(order_field)
                if isinstance(first_item, dict)
                # dynamic-attr: order_field is a runtime-configured model attribute name for cursor pagination
                else getattr(first_item, order_field, None)
            )
            if first_value is not None and self._has_previous:
                self._prev_cursor = _encode_cursor("prev", first_value)

        return items

    def get_paginated_response(self, data: list[dict[str, object]]) -> Response:
        next_url = None
        prev_url = None
        if self._request is not None:
            if self._next_cursor is not None:
                next_url = self._build_url(
                    self._request,
                    **{self.cursor_query_param: self._next_cursor},
                )
            if self._prev_cursor is not None:
                prev_url = self._build_url(
                    self._request,
                    **{self.cursor_query_param: self._prev_cursor},
                )
        body = {
            "next": next_url,
            "previous": prev_url,
            "results": data,
        }
        return Response.json(body)


_cached_cursor_secret: str | None = None
_cursor_secret_lock = threading.Lock()


def _get_cursor_secret() -> str:
    """Get the secret key for HMAC-signing cursor tokens. Resolved once, then cached.

    Uses HYPER_CURSOR_SECRET env var, or falls back to HYPER_SECRET_KEY.

    Thread-safe via double-checked locking: the resolution runs exactly once even
    under concurrent first-callers on a free-threaded build. A non-atomic
    check-then-init would let two threads each mint a *different* ephemeral secret
    — one wins the cache, and tokens signed with the loser's secret then fail HMAC
    verification ("Tampered cursor token"). The generated key is never written back
    into os.environ: the process environment (a shared global) is not mutated at
    request time.
    """
    global _cached_cursor_secret
    # Fast path: already resolved. Reference load is atomic; no lock needed.
    cached = _cached_cursor_secret
    if cached is not None:
        return cached

    with _cursor_secret_lock:
        # Re-check under the lock — another thread may have resolved it while we
        # were blocked on acquire().
        if _cached_cursor_secret is not None:
            return _cached_cursor_secret

        secret = get_setting("CURSOR_SECRET") or get_setting("SECRET_KEY")
        if not secret or len(secret.strip()) < 16:
            # Generate an ephemeral key — valid only for this process lifetime.
            # Do NOT write it back to os.environ: that mutates shared global state
            # from a request thread and races other readers/writers of the env.
            secret = os.urandom(32).hex()
            _logger.warning(
                "HYPER_CURSOR_SECRET not set (or too short) — using ephemeral key. "
                "Cursors will NOT survive server restarts or work across cluster nodes. "
                "Set HYPER_SECRET_KEY (16+ chars) for production."
            )
        _cached_cursor_secret = secret
        return secret


def _encode_cursor(direction: str, value: Any) -> str:
    """Encode and HMAC-sign a cursor value for tamper-proof pagination.

    Format: base64("direction:type_tag:serialized_value:hmac_signature")

    The HMAC prevents users from forging cursor values or injecting
    unexpected types/values. The cursor is stateless and works across
    servers in a cluster as long as HYPER_SECRET_KEY is the same.

    Supports: int, float, str, datetime, date, Decimal, UUID.
    """
    if isinstance(value, int):
        type_tag = "int"
        serialized = str(value)
    elif isinstance(value, float):
        type_tag = "float"
        serialized = repr(value)
    elif isinstance(value, datetime.datetime):
        type_tag = "datetime"
        serialized = value.isoformat()
    elif isinstance(value, datetime.date):
        type_tag = "date"
        serialized = value.isoformat()
    elif isinstance(value, decimal.Decimal):
        type_tag = "decimal"
        serialized = str(value)
    elif isinstance(value, uuid.UUID):
        type_tag = "uuid"
        serialized = str(value)
    else:
        type_tag = "str"
        serialized = str(value)

    payload = f"{direction}:{type_tag}:{serialized}"
    secret = _get_cursor_secret()
    signature = hmac_sha256_hex_truncated(secret.encode(), payload.encode(), 32)
    raw = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor_str: str) -> tuple[str, Any] | None:
    """Decode and verify an HMAC-signed cursor token.

    Returns (direction, coerced_value) on success, or None if:
    - Base64 decoding fails
    - HMAC signature is invalid (tampered)
    - Type coercion fails (malformed value)

    This is safe against:
    - Cursor forgery (HMAC prevents arbitrary value injection)
    - SQL injection (values are parameterized by the ORM, AND HMAC prevents type spoofing)
    - Cross-server usage (stateless, works as long as secret is shared)
    """
    try:
        decoded = base64.urlsafe_b64decode(cursor_str).decode("utf-8")
    except ValueError, UnicodeDecodeError:
        # binascii.Error (bad base64) and UnicodeDecodeError both subclass
        # ValueError; a malformed/forged cursor is simply rejected as absent.
        return None

    # Format: "direction:type_tag:serialized_value:signature"
    parts = decoded.rsplit(":", 1)
    if len(parts) != 2:
        return None

    payload, provided_sig = parts

    # Verify HMAC via unified constant-time helper
    secret = _get_cursor_secret()
    if not hmac_sha256_verify_truncated(
        secret.encode(), payload.encode(), provided_sig, 32
    ):
        return None  # Tampered cursor — reject silently

    # Parse payload: "direction:type_tag:serialized_value"
    payload_parts = payload.split(":", 2)
    if len(payload_parts) != 3:
        return None

    direction, type_tag, raw_value = payload_parts

    if direction not in CURSOR_DIRECTIONS:
        return None

    # Coerce value — wrapped in try/except for malformed data
    try:
        value = _coerce_cursor_value(type_tag, raw_value)
    except ValueError, ArithmeticError:
        # int/float/uuid/fromisoformat raise ValueError; Decimal raises
        # decimal.InvalidOperation (ArithmeticError) on malformed input.
        return None

    return direction, value


def _coerce_cursor_value(type_tag: str, raw_value: str) -> Any:
    """Coerce a decoded cursor value back to its original Python type.

    Only called AFTER HMAC verification, so the type_tag and raw_value
    are trusted (server-generated, not user-forged).
    """
    if type_tag == "int":
        return int(raw_value)
    if type_tag == "float":
        return float(raw_value)
    if type_tag == "datetime":
        return datetime.datetime.fromisoformat(raw_value)
    if type_tag == "date":
        return datetime.date.fromisoformat(raw_value)
    if type_tag == "decimal":
        return decimal.Decimal(raw_value)
    if type_tag == "uuid":
        return uuid.UUID(raw_value)
    return raw_value


# ── Server-Side Cursor Pagination ─────────────────────────────────────────────

# Thread-safe registry of active REAL PostgreSQL server-side cursors (per-process).
# Each entry holds a DatabaseServerCursor (DECLARE CURSOR / FETCH) with a pinned
# pool connection, plus metadata for user binding, access control, and expiry.
_cursor_registry_lock = threading.Lock()
# cursor_id → {user_id, created_at, last_accessed, total_fetched, db_cursor: DatabaseServerCursor}
_active_server_cursors: dict[str, dict[str, Any]] = {}
# Per-user cursor count index — O(1) count check
_user_cursor_counts: dict[str, int] = {}

_SERVER_CURSOR_MAX_IDLE = 300  # 5 minutes idle timeout
_SERVER_CURSOR_MAX_LIFETIME = 1800  # 30 minutes absolute lifetime
_SERVER_CURSOR_MAX_PER_USER = 5  # Max concurrent cursors per user


class ServerCursorPagination(APIPagination):
    """Real PostgreSQL DECLARE CURSOR / FETCH pagination for premium API consumers.

    Uses Database.server_cursor() to create a REAL server-side cursor that pins
    a pool connection and uses FETCH N to retrieve pages. Each FETCH is O(1) —
    no re-execution, no OFFSET scanning, no re-query. The database maintains
    the cursor position.

    Each active cursor pins one pool connection for its lifetime. This is the
    trade-off: O(1) page fetches at the cost of one pool slot per cursor.

    Security:
    - Cursor ID is HMAC-signed and bound to user_id (cannot be shared/stolen)
    - Anonymous users identified by IP (prevent namespace collision)
    - Per-user cursor limit (default 5) prevents pool exhaustion
    - Idle timeout (5 min) and max lifetime (30 min) auto-close cursors
    - Tampered tokens rejected via HMAC verification

    Distributed systems:
    - Server affinity REQUIRED: cursor state + pinned DB connection are in-process
    - Load balancer should route by server_cursor query param to same backend
    - Use ConsistentHashRing.get_node(cursor_id) for deterministic routing
    - Wrong server → 404 "Cursor not found" → client restarts query (graceful)

    Read-replica support:
    - ViewSet.get_queryset() returns Model.objects.using("replica")
    - The SQL built from that queryset executes against the replica pool
    - The DECLARE CURSOR pins a connection from the REPLICA pool, not primary
    - Write connections stay free for mutations

    Usage:
        class LargeExportViewSet(ModelViewSet):
            pagination_class = ServerCursorPagination
            def get_queryset(self):
                return MyModel.objects.using("replica")

        # GET /api/exports → {"cursor_id": "...", "results": [...], "exhausted": false}
        # GET /api/exports?server_cursor=<cursor_id> → FETCH next page
    """

    page_size: int = 100
    cursor_query_param: str = "server_cursor"
    max_idle_seconds: int = _SERVER_CURSOR_MAX_IDLE
    max_lifetime_seconds: int = _SERVER_CURSOR_MAX_LIFETIME
    max_per_user: int = _SERVER_CURSOR_MAX_PER_USER
    _request: Request | None = None
    _cursor_id: str | None = None
    _is_exhausted: bool = False

    async def paginate_queryset(self, queryset: Any, request: Request) -> list[object]:
        self._request = request
        cursor_token = request.GET.get(self.cursor_query_param)
        if cursor_token is not None:
            return await self._fetch_existing_cursor(cursor_token, request)
        return await self._create_new_cursor(queryset, request)

    async def _create_new_cursor(self, queryset: Any, request: Request) -> list[object]:
        """DECLARE CURSOR FOR the queryset's SQL via Database.server_cursor(), FETCH first page."""
        user_id = self._get_user_id(request)

        # Generate HMAC-signed cursor ID bound to user
        now = time.time()
        raw_id = f"{user_id}:{uuid.uuid4().hex}:{now}"
        secret = _get_cursor_secret()
        sig = hmac_sha256_hex_truncated(secret.encode(), raw_id.encode(), 32)
        cursor_id = f"{raw_id}:{sig}"

        # Build the SQL from the queryset (the queryset builds parameterized SQL)
        # and resolve the database connection (respects .using("replica"))
        db = queryset._get_db()

        # Build SELECT SQL from the queryset. _build_select() returns a
        # (sql, params) TUPLE — its only positional arg is ``columns_override``
        # (an exists()-style probe), NOT an out-param for bind values. A prior
        # version passed an empty list as columns_override and treated the tuple
        # return as raw SQL text, which broke the DECLARE CURSOR path entirely.
        sql, cursor_params = queryset._build_select()

        # Create REAL PostgreSQL server-side cursor: DECLARE CURSOR FOR <sql>
        # This pins a pool connection and sends BEGIN + DECLARE
        db_cursor = await db.server_cursor(sql, cursor_params, page_size=self.page_size)

        # FETCH first page. server_cursor() pinned a pool connection (BEGIN +
        # DECLARE), but PostgreSQL defers execution to the first FETCH — so a
        # query error / statement timeout first surfaces HERE, before the cursor
        # is registered in _active_server_cursors (and thus before idle cleanup
        # or __del__ could ever reclaim it). Without this guard a failed
        # first-fetch would leak the pinned connection for the process lifetime.
        # Close the cursor (suppressing secondary errors) before re-raising.
        try:
            rows = await db_cursor.fetch_page()
            self._is_exhausted = db_cursor.is_exhausted

            if self._is_exhausted and not rows:
                await db_cursor.close()
                self._cursor_id = None
                return []

            # If exhausted on first page, close cursor immediately
            if self._is_exhausted:
                await db_cursor.close()
                self._cursor_id = None
                return rows

            # Atomic check-and-register under single lock hold. If we're over the
            # per-user limit we must NOT close the DB cursor while holding the
            # lock: close() does a network round-trip (CLOSE + COMMIT) and, on a
            # multiplexing loop, awaiting under a threading.Lock deadlocks the
            # loop thread. Collect the decision under the lock, act after release.
            over_limit = False
            with _cursor_registry_lock:
                count = _user_cursor_counts.get(user_id, 0)
                if count >= self.max_per_user:
                    over_limit = True
                else:
                    _active_server_cursors[cursor_id] = {
                        "user_id": user_id,
                        "created_at": now,
                        "last_accessed": now,
                        "total_fetched": len(rows),
                        "db_cursor": db_cursor,
                        # Claimed for exclusive FETCH access; see _fetch_existing_cursor.
                        "in_use": False,
                    }
                    _user_cursor_counts[user_id] = count + 1

            if over_limit:
                # Over limit — close the DB cursor we just opened (outside the
                # lock). Mark it released so the except guard below does not
                # redundantly re-close it on the Throttled propagation.
                await db_cursor.close()
                db_cursor = None
                raise Throttled(
                    f"Maximum {self.max_per_user} concurrent server cursors per user. "
                    "Close existing cursors before opening new ones."
                )

            self._cursor_id = base64.urlsafe_b64encode(cursor_id.encode()).decode()
            return rows
        except BaseException:
            # Failure before the cursor is safely owned by the registry: release
            # the pinned pool connection now. Once registered above, the cursor
            # is reachable by idle cleanup and must NOT be closed here — but the
            # only post-registration raise (over-limit) already nulled db_cursor.
            if db_cursor is not None:
                with contextlib.suppress(Exception):
                    await db_cursor.close()
            raise

    async def _fetch_existing_cursor(
        self, cursor_token: str, request: Request
    ) -> list[object]:
        """FETCH next page from an existing DECLARE CURSOR via the pinned connection."""
        # Decode and verify cursor token
        try:
            cursor_id = base64.urlsafe_b64decode(cursor_token).decode()
        except Exception:
            raise ValidationError("Invalid cursor token")

        # Verify HMAC signature
        parts = cursor_id.rsplit(":", 1)
        if len(parts) != 2:
            raise ValidationError("Malformed cursor token")
        payload, provided_sig = parts
        secret = _get_cursor_secret()
        if not hmac_sha256_verify_truncated(
            secret.encode(), payload.encode(), provided_sig, 32
        ):
            raise PermissionDenied("Tampered cursor token")

        # Verify user binding. The cursor payload is "<user_id>:<uuid>:<ts>",
        # but user_id itself can contain ':' (anon users are "anon:<ip>"), so a
        # split(':')[0] prefix compared only "anon" and rejected every anon
        # resume. Require the payload to begin with the FULL user-id token
        # followed by the ':' delimiter — binds the cursor to its creator while
        # letting the same (anon or authenticated) user page.
        user_id = self._get_user_id(request)
        if not payload.startswith(f"{user_id}:"):
            raise PermissionDenied("Cursor belongs to a different user")

        # Look up cursor state and either (a) mark it expired for close-after-
        # release, or (b) claim it for exclusive FETCH access. Everything that
        # touches the shared registry happens under the lock; the actual DB I/O
        # (close / fetch_page — network round-trips) happens AFTER release. On a
        # multiplexing loop, awaiting under a threading.Lock deadlocks the loop
        # thread, so we NEVER await while holding _cursor_registry_lock.
        expired_cursor = None
        expired_reason = ""
        with _cursor_registry_lock:
            state = _active_server_cursors.get(cursor_id)
            if state is None:
                raise NotFound("Cursor expired or not found. Start a new query.")

            now = time.time()
            if now - state["created_at"] > self.max_lifetime_seconds:
                expired_cursor = state["db_cursor"]
                expired_reason = "Cursor expired (max lifetime exceeded)"
                self._remove_cursor(cursor_id, state["user_id"])
            elif now - state["last_accessed"] > self.max_idle_seconds:
                expired_cursor = state["db_cursor"]
                expired_reason = "Cursor expired (idle timeout)"
                self._remove_cursor(cursor_id, state["user_id"])
            elif state.get("in_use"):
                # Another request is already replaying this exact cursor token.
                # Two concurrent FETCHes on the same pinned connection would
                # interleave the PostgreSQL wire protocol (corrupted/duplicated
                # pages). Reject rather than corrupt.
                raise Conflict(
                    "Cursor is already in use by another request. Retry shortly."
                )
            else:
                # Claim exclusive access. While in_use is set, the cleanup task
                # will not close this cursor out from under us.
                state["in_use"] = True
                db_cursor = state["db_cursor"]

        # Close an expired cursor OUTSIDE the lock, then report it.
        if expired_cursor is not None:
            await expired_cursor.close()
            raise NotFound(expired_reason)

        # FETCH next page from the REAL PostgreSQL cursor (O(1), no re-query).
        # Exclusive access is guaranteed by the in_use claim above. The finally
        # block always releases the claim (or is a no-op once the cursor has
        # been removed from the registry).
        try:
            rows = await db_cursor.fetch_page()
            self._is_exhausted = db_cursor.is_exhausted

            if not self._is_exhausted:
                # Update access metadata (thread-safe)
                with _cursor_registry_lock:
                    if cursor_id in _active_server_cursors:
                        state["last_accessed"] = time.time()
                        state["total_fetched"] += len(rows)
                self._cursor_id = cursor_token
            else:
                # Cursor exhausted — remove from registry, then close the DB
                # cursor and release the pool connection (close outside lock).
                with _cursor_registry_lock:
                    self._remove_cursor(cursor_id, user_id)
                await db_cursor.close()
                self._cursor_id = None

            return rows
        except BaseException:
            # A fetch failure usually leaves the pinned connection desynced —
            # drop the cursor promptly (remove + close) so its pool connection
            # is freed now, rather than lingering until idle cleanup reclaims it.
            with _cursor_registry_lock:
                self._remove_cursor(cursor_id, user_id)
            with contextlib.suppress(Exception):
                await db_cursor.close()
            self._cursor_id = None
            raise
        finally:
            # Release the exclusive claim. If the cursor was exhausted (and thus
            # removed) this get() returns None and the clear is a no-op.
            with _cursor_registry_lock:
                st = _active_server_cursors.get(cursor_id)
                if st is not None:
                    st["in_use"] = False

    def _remove_cursor(self, cursor_id: str, user_id: str) -> None:
        """Remove cursor from registry. Caller MUST hold _cursor_registry_lock.

        Decrements the per-user count ONLY when this call actually removed an
        entry. Otherwise the exhausted-path / cleanup-task race (both trying to
        remove the same cursor) double-decrements, eroding the max_per_user
        pool-exhaustion guard until it wraps toward zero and lets a user open
        more cursors than allowed.
        """
        removed = _active_server_cursors.pop(cursor_id, None)
        if removed is not None:
            _user_cursor_counts[user_id] = max(
                0, _user_cursor_counts.get(user_id, 0) - 1
            )

    def _get_user_id(self, request: Request) -> str:
        """Extract user ID for cursor binding. Anon users identified by IP."""
        if request.user is None:
            return f"anon:{request.client_ip}"
        user_id = request.user.id
        if user_id is None:
            return f"anon:{request.client_ip}"
        return str(user_id)

    def get_paginated_response(self, data: list[dict[str, object]]) -> Response:
        body: dict[str, object] = {
            "results": data,
            "exhausted": self._is_exhausted,
        }
        body["cursor_id"] = self._cursor_id
        return Response.json(body)


async def cleanup_expired_server_cursors() -> int:
    """Close expired server-side cursors and release their pinned pool connections.

    Thread-safe. Async because closing DB cursors requires CLOSE + COMMIT SQL.
    Call periodically (e.g., every minute via background task).
    Returns the number of cursors cleaned up.
    """
    now = time.time()
    to_close: list[tuple[str, str, Any]] = []  # (cursor_id, user_id, db_cursor)

    with _cursor_registry_lock:
        for cursor_id, state in list(_active_server_cursors.items()):
            if not isinstance(state, dict):
                _active_server_cursors.pop(cursor_id, None)
                continue
            expired = False
            if (
                now - state["created_at"] > _SERVER_CURSOR_MAX_LIFETIME
                or now - state["last_accessed"] > _SERVER_CURSOR_MAX_IDLE
            ):
                expired = True
            # Never yank a cursor that a request is actively FETCHing on — closing
            # its pinned connection mid-fetch corrupts the PG wire protocol. The
            # in-flight request will observe expiry on its next call instead.
            if expired and state.get("in_use"):
                continue
            if expired:
                to_close.append((cursor_id, state["user_id"], state["db_cursor"]))
                _active_server_cursors.pop(cursor_id, None)
                uid = state["user_id"]
                _user_cursor_counts[uid] = max(0, _user_cursor_counts.get(uid, 0) - 1)

    # Close DB cursors OUTSIDE the lock (involves I/O)
    for _, _, db_cursor in to_close:
        try:
            await db_cursor.close()
        # blind-except: best-effort teardown of an already-expired cursor; a close failure must not abort sweeping the remaining cursors.
        except Exception:
            _logger.debug("Failed to close expired server cursor", exc_info=True)

    return len(to_close)


# ── Relational Fields ─────────────────────────────────────────────────────────


class RelatedFieldMixin:
    """Mixin for serializer fields that resolve FK/M2M references.

    Subclasses set lookup_field and override to_internal_value/to_representation.
    """

    def __init__(self, queryset: Any = None, many: bool = False):
        self.queryset = queryset
        self.many = many
        # Marker read by SerializerMeta to register this as a relational field
        # so it participates in async existence validation (avalidate_relations).
        self._is_related_field = True
        # When True, the serialize plan wraps the read getter so the field's
        # synchronous read representation runs (SlugRelatedField emits its slug,
        # not the raw related object). Left False for PrimaryKeyRelatedField,
        # whose raw PK passthrough is already the correct representation.
        self._wire_read_representation = False


class PrimaryKeyRelatedField(RelatedFieldMixin):
    """Serializes FK as integer PK. Validates PK exists on write.

    Read: instance.author_id → 42
    Write: {"author_id": 42} → validates User with pk=42 exists

    Usage:
        class PostSerializer(ModelSerializer):
            author_id: int = PrimaryKeyRelatedField(queryset=User.objects)
    """

    async def to_representation(self, value: Any) -> int | list[int] | None:
        """Serialize: return the PK value directly."""
        if value is None:
            return None
        if self.many and isinstance(value, list):
            return [self._get_pk(item) for item in value]
        return self._get_pk(value)

    def _get_pk(self, value: Any) -> int:
        if isinstance(value, (int, str)):
            return int(value)
        if isinstance(value, dict):
            return value.get("id", value.get("pk"))
        # Model instance — pk is the primary key attribute
        return value.pk

    async def to_internal_value(self, data: Any) -> Any:
        """Deserialize: validate PK exists and return validated value."""
        if self.queryset is None:
            return data
        if self.many:
            if not isinstance(data, list):
                raise ValidationError(
                    f"Expected a list of primary keys, got {type(data).__name__}"
                )
            validated: list[int] = []
            for pk in data:
                await self._validate_pk(pk)
                validated.append(int(pk))
            return validated
        await self._validate_pk(data)
        return int(data)

    async def _validate_pk(self, pk: Any) -> None:
        """Validate a single PK exists in the queryset."""
        try:
            pk_int = int(pk)
        except ValueError, TypeError:
            raise ValidationError(f"Invalid primary key: {pk}")
        try:
            await self.queryset.get(id=pk_int)
        except Model.DoesNotExist:
            raise ValidationError(f"Object with pk={pk_int} does not exist")
        except Exception:
            # Not a "does not exist" — a DB/ORM failure must not be masked as a
            # user validation error. Log with context and propagate (→ 500).
            _logger.exception("PrimaryKeyRelatedField: error validating pk=%r", pk_int)
            raise


class SlugRelatedField(RelatedFieldMixin):
    """Serializes FK as a slug field value. Resolves by slug on write.

    Read: instance.author → "alice" (author.username)
    Write: {"author": "alice"} → resolves User with username="alice"

    Usage:
        class PostSerializer(ModelSerializer):
            author: str = SlugRelatedField(queryset=User.objects, slug_field="username")
    """

    def __init__(
        self, queryset: Any = None, slug_field: str = "slug", many: bool = False
    ):
        super().__init__(queryset=queryset, many=many)
        self.slug_field = slug_field
        # Slug is a computed read representation — wire it into the serialize plan
        # so reads emit the slug string instead of the raw related object.
        self._wire_read_representation = True

    def represent_read(self, value: Any) -> str | list[str] | None:
        """Synchronous read-path representation (the slug value).

        ``to_representation`` is async for symmetry with ``to_internal_value``,
        but the slug read path does no awaiting. The serialize plan is
        synchronous, so it calls this directly (see ``_make_related_repr_getter``
        in serializers.py).
        """
        if value is None:
            return None
        if self.many and isinstance(value, list):
            return [self._get_slug(item) for item in value]
        return self._get_slug(value)

    async def to_representation(self, value: Any) -> str | list[str] | None:
        """Serialize: return the slug field value."""
        return self.represent_read(value)

    def _get_slug(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get(self.slug_field, ""))
        # dynamic-attr: slug_field is a runtime-configured model attribute name
        return str(getattr(value, self.slug_field, ""))

    async def to_internal_value(self, data: Any) -> Any:
        """Deserialize: validate slug exists and return the resolved PK."""
        if self.queryset is None:
            return data
        if self.many:
            if not isinstance(data, list):
                raise ValidationError(f"Expected a list of {self.slug_field} values")
            validated: list[int] = []
            for slug in data:
                pk = await self._resolve_slug(slug)
                validated.append(pk)
            return validated
        return await self._resolve_slug(data)

    async def _resolve_slug(self, slug_value: Any) -> int:
        """Resolve a slug value to a PK."""
        try:
            obj = await self.queryset.get(**{self.slug_field: slug_value})
        except Model.DoesNotExist:
            raise ValidationError(
                f"Object with {self.slug_field}={slug_value!r} does not exist"
            )
        except Exception:
            # Not a "does not exist" — a DB/ORM failure must not be masked as a
            # user validation error. Log with context and propagate (→ 500).
            _logger.exception(
                "SlugRelatedField: error resolving %s=%r",
                self.slug_field,
                slug_value,
            )
            raise
        if isinstance(obj, dict):
            return obj.get("id", obj.get("pk"))
        return obj.pk


# ── ModelSerializer ───────────────────────────────────────────────────────────


# PostgreSQL column types whose binary wire encoding db.query_json renders
# to JSON byte-identically to the Python serialize path (model instance →
# fast_json_dumps). integer/float/bool and the text/char families always
# matched; NUMERIC, TIMESTAMP(TZ), DATE, TIME, UUID, and JSON(B) are rendered
# by zig/src/pg_render.zig with formats matched byte-for-byte to
# fast_json_dumps (see tests/test_db/test_native_json_render.py). BYTEA,
# INTERVAL, VECTOR, arrays, etc. remain Python-path only.
_NATIVE_SAFE_SQL_TYPES = frozenset(
    {
        "SMALLINT",
        "INTEGER",
        "INT",
        "BIGINT",
        "INT2",
        "INT4",
        "INT8",
        "SERIAL",
        "BIGSERIAL",
        "SMALLSERIAL",
        "SERIAL2",
        "SERIAL4",
        "SERIAL8",
        "REAL",
        "DOUBLE PRECISION",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "BOOLEAN",
        "BOOL",
        "TEXT",
        "VARCHAR",
        "CHARACTER VARYING",
        "CHAR",
        "CHARACTER",
        "BPCHAR",
        "TIMESTAMP",
        "TIMESTAMPTZ",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP WITHOUT TIME ZONE",
        "DATE",
        "TIME",
        "NUMERIC",
        "DECIMAL",
        "UUID",
        "JSONB",
        "JSON",
    }
)


def _native_safe_sql_type(sql_type: str) -> bool:
    """True when db.query_json renders this SQL type byte-identically.

    Matches the base type only (length/precision modifiers stripped) against
    an allow-list, so e.g. VARCHAR(200) is safe while INTERVAL is not — exact
    membership avoids the ``startswith("INT")`` trap that INTERVAL would hit.
    """
    base = sql_type.upper().split("(", 1)[0].strip()
    return base in _NATIVE_SAFE_SQL_TYPES


class ModelSerializerMeta(SerializerMeta):
    """Metaclass that auto-generates serializer fields from a Model class.

    After SerializerMeta collects explicitly declared fields, this metaclass
    fills in missing fields by introspecting Model._meta.fields and __annotations__.

    Supports:
    - Meta.depth: auto-generate nested serializers for FK fields (0=flat PKs, 1+=nested)
    - Explicit nested serializer fields: preserved when declared on the class
    - Writable nested: create/update handles nested dicts in validated_data
    """

    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        if name == "ModelSerializer":
            return cls

        # dynamic-attr: optional user-declared inner Meta class, possibly inherited; may be absent
        meta = namespace.get("Meta") or getattr(cls, "Meta", None)
        if meta is None:
            return cls

        model = meta.model if hasattr(meta, "model") else None
        if model is None:
            return cls

        fields_spec = meta.fields if hasattr(meta, "fields") else None
        if fields_spec is None:
            return cls

        read_only_fields = (
            meta.read_only_fields if hasattr(meta, "read_only_fields") else []
        )
        extra_kwargs = meta.extra_kwargs if hasattr(meta, "extra_kwargs") else {}
        depth = meta.depth if hasattr(meta, "depth") else 0

        # Determine which fields to include
        model_fields = model._meta.fields  # dict[str, FieldMeta]
        model_annotations = model.__annotations__  # dict[str, type]

        if fields_spec == "__all__":
            target_fields = list(model_fields.keys())
        else:
            target_fields = list(fields_spec)

        # Get existing explicitly declared fields
        existing_fields = dict(cls._serializer_fields)

        # Detect explicitly declared nested serializer instances on the class.
        # These are Serializer subclass *instances* set as class attributes
        # (e.g., author = UserSerializer(read_only=True)).
        explicit_nested: dict[str, Serializer] = {}
        for field_name in target_fields:
            class_val = namespace.get(field_name)
            if class_val is not None and isinstance(class_val, Serializer):
                explicit_nested[field_name] = class_val

        # Auto-generate nested serializers for FK fields when depth > 0
        nested_serializers: dict[str, type] = {}
        if depth > 0:
            for field_name in target_fields:
                # Don't override explicitly declared nested serializers
                if field_name in explicit_nested:
                    continue
                field_meta = model_fields.get(field_name)
                if field_meta is None or field_meta.foreign_key is None:
                    continue
                # Look up the target model from the registry
                target_model = _get_model_by_table(field_meta.foreign_key)
                if target_model is None:
                    continue
                # Build an auto-generated nested ModelSerializer for this FK
                nested_meta_attrs = {
                    "model": target_model,
                    "fields": "__all__",
                    "read_only_fields": ["id"],
                    "depth": depth - 1,
                }
                nested_meta = type("Meta", (), nested_meta_attrs)
                nested_cls_name = f"_Auto{target_model.__name__}Serializer_d{depth}"
                # Import ModelSerializer reference for the nested class (it's the
                # class being constructed by this metaclass, so use the base)
                ms_base = None
                for base in cls.__mro__:
                    if base.__name__ == "ModelSerializer":
                        ms_base = base
                        break
                if ms_base is None:
                    ms_base = cls
                nested_cls = ModelSerializerMeta(
                    nested_cls_name, (ms_base,), {"Meta": nested_meta}
                )
                nested_serializers[field_name] = nested_cls

        # Store nested serializer classes (explicit instances + auto-generated)
        cls._nested_serializer_classes = nested_serializers
        cls._explicit_nested_instances = explicit_nested

        # Field(exclude=True) is a model-author's HARD "never serialize" marker
        # (password_hash, secret columns). The model's own to_dict() already
        # omits these via _meta.excluded_fields; the ModelSerializer MUST too, or
        # fields="__all__" (or an explicit listing) would leak them straight into
        # the API response as a read-only field. This exclusion wins even over an
        # explicit fields=[...] entry — a security marker, not a default.
        # A model with no Field(exclude=True) never sets _meta.excluded_fields;
        # a default-empty read is fail-safe (absent → nothing extra excluded).
        # dynamic-attr: optional _meta attribute, default-read is correct + safe
        excluded_fields = getattr(model._meta, "excluded_fields", frozenset())

        for field_name in target_fields:
            # Skip if already explicitly declared on the serializer
            if field_name in existing_fields:
                continue

            if field_name in excluded_fields:
                continue

            field_meta = model_fields.get(field_name)
            if field_meta is None:
                continue

            # Get Python type from model annotations
            field_type = model_annotations.get(field_name, str)

            # If this field has an auto-generated nested serializer, use it as field_type
            if field_name in nested_serializers:
                field_type = nested_serializers[field_name]

            # If this field has an explicit nested serializer instance, use its class
            if field_name in explicit_nested:
                field_type = type(explicit_nested[field_name])

            # Determine if read-only. Field(editable=False) is ALWAYS read-only
            # (even under fields="__all__"): the mass-assignment guard for
            # security-sensitive columns (is_staff, is_superuser, password_hash).
            is_read_only = (
                field_meta.auto
                or not field_meta.editable
                or field_name in read_only_fields
            )

            # Get FieldInfo from model class for constraint mapping
            model_field_info = model.__dict__.get(field_name)
            constraints = _extract_constraints(model_field_info)

            # Apply extra_kwargs overrides
            if field_name in extra_kwargs:
                constraints.update(extra_kwargs[field_name])

            # Determine if required
            has_default = False
            if isinstance(model_field_info, FieldInfo):
                has_default = (
                    model_field_info.default is not _MISSING
                    or model_field_info.default_factory is not None
                )

            info = SerializerFieldInfo(
                field_name=field_name,
                field_type=field_type,
                read_only=constraints.pop("read_only", is_read_only),
                write_only=constraints.pop("write_only", False),
                required=constraints.pop(
                    "required", not has_default and not is_read_only
                ),
                default=constraints.pop("default", None),
                source=constraints.pop("source", None),
                min_length=constraints.pop("min_length", None),
                max_length=constraints.pop("max_length", None),
                min_value=constraints.pop("min_value", None),
                max_value=constraints.pop("max_value", None),
                choices=constraints.pop("choices", None),
                label=constraints.pop("label", None),
                help_text=constraints.pop("help_text", None),
            )
            existing_fields[field_name] = info

        cls._serializer_fields = existing_fields

        # Refresh the serialize plan now that the model fields are merged
        # in. SerializerMeta built an initial plan from the explicit
        # declarations only — the auto-injected model fields would be
        # missing without this rebuild.
        from hyperdjango.serializers import build_flat_encoder, build_serialize_plan

        cls._serialize_plan = build_serialize_plan(cls)
        cls._flat_encoder = build_flat_encoder(cls)

        # Auto-detect FK fields → PrimaryKeyRelatedField for write validation
        # (only for fields NOT handled by nested serializers). Seed with any
        # relational fields the base SerializerMeta already registered from
        # explicit class declarations so we merge rather than clobber them.
        relational: dict[str, RelatedFieldMixin] = dict(cls._relational_fields)
        for field_name in target_fields:
            if field_name in relational:
                continue
            # Skip FK fields handled by nested serializers
            if field_name in nested_serializers or field_name in explicit_nested:
                continue
            field_meta = model_fields.get(field_name)
            if field_meta is None or field_meta.foreign_key is None:
                continue
            # Look up the target model from the registry
            target_model = _get_model_by_table(field_meta.foreign_key)
            if target_model is not None:
                target_qs = target_model.objects
                relational[field_name] = PrimaryKeyRelatedField(queryset=target_qs)
        cls._relational_fields = relational

        # ── Field resolution caching ──────────────────────────────────
        # Pre-compute cached field sets for O(1) lookups during serialization.

        # Cache read_only field set (auto fields + explicit read_only)
        cached_read_only: set[str] = set()
        # Cache write field set (writable: not read_only, not computed)
        cached_write: set[str] = set()
        # Cache column-to-field mapping (DB column name → serializer field name)
        cached_column_map: dict[str, str] = {}
        # Track whether all fields map 1:1 to DB columns (no method/nested/computed)
        is_identity = True

        for field_name, field_info in existing_fields.items():
            # Check for SerializerMethodField in class dict
            class_val = namespace.get(field_name)
            has_method_field = (
                class_val is not None
                # dynamic-attr: duck-typed marker probe on an arbitrary value from the user's class namespace
                and getattr(class_val, "_is_serializer_method_field", False)
            )

            # Check for TypedField in class dict
            has_typed_field = (
                class_val is not None
                # dynamic-attr: duck-typed marker probe on an arbitrary value from the user's class namespace
                and getattr(class_val, "_is_typed_field", False)
            )

            # Check for nested serializer (field_type is a Serializer subclass)
            ft = field_info.field_type
            has_nested = (
                isinstance(ft, type)
                and ft is not Serializer
                and issubclass(ft, Serializer)
            )

            # Check for computed field (source points to a method)
            source = field_info.source or field_name
            has_compute = False
            if source != field_name:
                # source= remapping — check if it targets a method
                method_candidate = namespace.get(source)
                if method_candidate is not None and callable(method_candidate):
                    has_compute = True

            is_computed = (
                has_method_field or has_typed_field or has_nested or has_compute
            )

            if is_computed:
                is_identity = False

            if field_info.read_only:
                cached_read_only.add(field_name)
            elif not is_computed:
                cached_write.add(field_name)

            # Column-to-field mapping: use source if set, else field_name
            db_column = field_info.source or field_name
            if not is_computed and db_column != field_name:
                cached_column_map[db_column] = field_name
            elif not is_computed:
                cached_column_map[field_name] = field_name

        # Relational fields (PrimaryKeyRelatedField/SlugRelatedField) require
        # validation lookup during writes, so they break identity serialization.
        if relational:
            is_identity = False

        cls._is_identity_serializer = is_identity
        cls._column_field_map = cached_column_map
        cls._read_only_fields = frozenset(cached_read_only)
        cls._write_fields = frozenset(cached_write)

        # ── Native JSON SELECT plan ───────────────────────────────────
        # For identity serializers, precompute the ordered list of
        # (db_column, output_key) pairs that _serialize_one would emit,
        # in the exact same order (read fields, skipping write_only).
        # The list drives a columns_override SELECT fed to db.query_json,
        # so the native PG-wire→JSON output is byte-identical to
        # serializer.data. Non-identity serializers get None (the REST
        # views then fall back to the Python hydrate+serialize path).
        native_cols: list[tuple[str, str]] | None = None
        if is_identity:
            native_cols = []
            for field_name, field_info in existing_fields.items():
                if field_info.write_only:
                    continue
                column = field_info.source or field_name
                # Only route through query_json when the underlying PG column
                # type renders identically to the Python path. Any unsupported
                # type (NUMERIC/DATE/TIMESTAMP/UUID/JSONB/…) or a column that
                # doesn't resolve disqualifies the whole serializer.
                try:
                    sql_type = _field_to_sql_type(model, column)
                # blind-except: native-select is a pure optimization; a column that can't resolve to a SQL type disqualifies it and the serializer safely falls back to the Python render path.
                except Exception:
                    native_cols = None
                    break
                if not _native_safe_sql_type(sql_type):
                    native_cols = None
                    break
                native_cols.append((column, field_name))
        cls._native_select_columns = native_cols

        return cls


def _extract_constraints(field_info: Any) -> dict[str, Any]:
    """Extract validation constraints from a model's FieldInfo into serializer kwargs."""
    if not isinstance(field_info, FieldInfo):
        return {}

    constraints: dict[str, Any] = {}
    if field_info.ge is not None:
        constraints["min_value"] = field_info.ge
    if field_info.gt is not None:
        constraints["min_value"] = field_info.gt
    if field_info.le is not None:
        constraints["max_value"] = field_info.le
    if field_info.lt is not None:
        constraints["max_value"] = field_info.lt
    if field_info.min_length is not None:
        constraints["min_length"] = field_info.min_length
    if field_info.max_length is not None:
        constraints["max_length"] = field_info.max_length
    if field_info.title is not None:
        constraints["label"] = field_info.title
    if field_info.description is not None:
        constraints["help_text"] = field_info.description
    return constraints


class ModelSerializer(Serializer, metaclass=ModelSerializerMeta):
    """Serializer that auto-generates fields from a HyperDjango Model.

    Supports Meta.depth for auto-nested serializers on FK fields,
    explicit nested serializer fields, and writable nested create/update.

    Usage:
        class UserSerializer(ModelSerializer):
            class Meta:
                model = User
                fields = "__all__"
                read_only_fields = ["id"]

        # Nested: depth=1 auto-generates nested serializers for FK fields
        class PostSerializer(ModelSerializer):
            class Meta:
                model = Post
                fields = "__all__"
                read_only_fields = ["id"]
                depth = 1

        # Explicit nested (preserved, not overridden by depth)
        class PostSerializer(ModelSerializer):
            author = UserSerializer(read_only=True)
            class Meta:
                model = Post
                fields = "__all__"

        # Writable nested create
        serializer = PostSerializer(input_data={"title": "X", "author": {"name": "A"}})
        if serializer.is_valid():
            post = await serializer.create(serializer.validated_data)
    """

    class Meta:
        model: type | None = None
        fields: list[str] | str = "__all__"
        read_only_fields: list[str] = []
        extra_kwargs: dict[str, dict[str, Any]] = {}
        depth: int = 0

    # Cached field metadata (set by ModelSerializerMeta)
    _is_identity_serializer: bool = False
    _column_field_map: dict[str, str] = {}
    _read_only_fields: frozenset[str] = frozenset()
    _write_fields: frozenset[str] = frozenset()
    _nested_serializer_classes: dict[str, type] = {}
    _explicit_nested_instances: dict[str, Serializer] = {}
    # Ordered (db_column, output_key) pairs for the native JSON fast path,
    # or None when the serializer is not a pure model-field passthrough.
    _native_select_columns: list[tuple[str, str]] | None = None

    @classmethod
    def get_field_names(cls) -> list[str]:
        """Return cached list of all serializer field names."""
        return list(cls._serializer_fields.keys())

    def _get_nested_field_info(self) -> dict[str, type]:
        """Return mapping of field_name to nested serializer class for all nested fields."""
        result: dict[str, type] = {}
        for field_name, nested_cls in self._nested_serializer_classes.items():
            result[field_name] = nested_cls
        for field_name, nested_inst in self._explicit_nested_instances.items():
            result[field_name] = type(nested_inst)
        return result

    def _is_nested_read_only(self, field_name: str) -> bool:
        """Check if a nested field is read-only."""
        field_info = self._serializer_fields.get(field_name)
        return bool(field_info is not None and field_info.read_only)

    async def create(self, validated_data: dict[str, object]) -> object:
        """Create and return a new model instance from validated data.

        Handles writable nested: dicts for FK fields create related objects first,
        lists of dicts for many fields create after parent.
        """
        nested_info = self._get_nested_field_info()
        flat_data: dict[str, object] = {}
        nested_many_data: dict[str, list[dict[str, object]]] = {}

        for field_name, value in validated_data.items():
            if field_name in nested_info and isinstance(value, dict):
                if self._is_nested_read_only(field_name):
                    continue
                nested_cls = nested_info[field_name]
                related_model = nested_cls.Meta.model
                if related_model is not None:
                    related_obj = await related_model.objects.create(**value)
                    if isinstance(related_obj, dict):
                        flat_data[field_name] = related_obj.get("id")
                    else:
                        flat_data[field_name] = related_obj.pk
                else:
                    flat_data[field_name] = value
            elif field_name in nested_info and isinstance(value, list):
                if not self._is_nested_read_only(field_name):
                    nested_many_data[field_name] = value
            else:
                flat_data[field_name] = value

        instance = await self.Meta.model.objects.create(**flat_data)

        for field_name, items in nested_many_data.items():
            nested_cls = nested_info[field_name]
            related_model = nested_cls.Meta.model
            if related_model is not None:
                for item_data in items:
                    await related_model.objects.create(**item_data)

        return instance

    async def update(
        self, instance: object, validated_data: dict[str, object]
    ) -> object:
        """Update and return an existing model instance.

        Handles writable nested: dicts for FK fields update related objects,
        lists of dicts create each related object.
        """
        nested_info = self._get_nested_field_info()

        for field_name, value in validated_data.items():
            if field_name in nested_info and isinstance(value, dict):
                if self._is_nested_read_only(field_name):
                    continue
                if isinstance(instance, dict):
                    related = instance.get(field_name)
                else:
                    # dynamic-attr: field_name is a runtime relation name from validated request data
                    related = getattr(instance, field_name, None)
                if related is not None and not isinstance(related, (int, str)):
                    if isinstance(related, dict):
                        related.update(value)
                    else:
                        for k, v in value.items():
                            # dynamic-attr: assigning related-model fields named by validated request keys
                            setattr(related, k, v)
                        await related.save()
                else:
                    nested_cls = nested_info[field_name]
                    related_model = nested_cls.Meta.model
                    if related_model is not None:
                        related_obj = await related_model.objects.create(**value)
                        if isinstance(related_obj, dict):
                            pk_val = related_obj.get("id")
                        else:
                            pk_val = related_obj.pk
                        if isinstance(instance, dict):
                            instance[field_name] = pk_val
                        else:
                            # dynamic-attr: assigning a model field named by validated request key
                            setattr(instance, field_name, pk_val)
            elif field_name in nested_info and isinstance(value, list):
                if self._is_nested_read_only(field_name):
                    continue
                nested_cls = nested_info[field_name]
                related_model = nested_cls.Meta.model
                if related_model is not None:
                    for item_data in value:
                        await related_model.objects.create(**item_data)
            else:
                if isinstance(instance, dict):
                    instance[field_name] = value
                else:
                    # dynamic-attr: assigning a model field named by validated request key
                    setattr(instance, field_name, value)

        if not isinstance(instance, dict):
            await instance.save()
        return instance


# ── Versioning ────────────────────────────────────────────────────────────────


class APIVersioning:
    """Base class for API versioning strategies."""

    default_version: str = "1.0"
    allowed_versions: tuple[str, ...] = ()

    def determine_version(self, request: Request) -> str:
        return self.default_version

    def _validate_version(self, version: str) -> str:
        if self.allowed_versions and version not in self.allowed_versions:
            return self.default_version
        return version


class URLPathVersioning(APIVersioning):
    """Extract version from URL path: /api/v1/users → "1.0"."""

    version_param: str = "version"

    def determine_version(self, request: Request) -> str:
        # Check path params first (set by router)
        version = request.path_params.get(self.version_param)
        if version is not None:
            # Strip 'v' prefix: "v1" → "1", "v2.1" → "2.1"
            version = version.lstrip("v")
            return self._validate_version(version)
        return self.default_version


class HeaderVersioning(APIVersioning):
    """Extract version from Accept header: Accept: application/json; version=1.0."""

    def determine_version(self, request: Request) -> str:
        accept = request.headers.get("accept", "")
        # Parse "application/json; version=1.0"
        for part in accept.split(";"):
            part = part.strip()
            if part.startswith("version="):
                version = part[8:].strip()
                return self._validate_version(version)
        return self.default_version


class QueryParamVersioning(APIVersioning):
    """Extract version from query parameter: ?version=1.0."""

    query_param: str = "version"

    def determine_version(self, request: Request) -> str:
        version = request.GET.get(self.query_param)
        if version is not None:
            return self._validate_version(version)
        return self.default_version


# ── Throttling ────────────────────────────────────────────────────────────────


class BaseThrottle:
    """Base class for per-view rate limiting.

    Subclass and override allow_request() to implement rate limiting logic.
    """

    async def allow_request(self, request: Request, view: ViewSet) -> bool:
        """Return True if the request should be allowed, False if throttled."""
        return True

    def get_wait(self) -> int | None:
        """Return seconds to wait before next request (for Retry-After header)."""
        return None


class SimpleRateThrottle(BaseThrottle):
    """Rate throttle using the existing InMemoryRateLimitBackend.

    Subclass and set `rate` (e.g., "100/hour") and override get_cache_key().
    """

    rate: str = "100/hour"
    _backend: Any = None
    _backend_lock: threading.Lock = threading.Lock()
    _wait: int | None = None

    def __init__(self):
        # Double-checked locking: the shared class-level backend is created
        # exactly once even when many throttles are constructed concurrently on
        # a free-threaded build. The prior check-then-init could build (and
        # discard) several backends, and briefly hand out a not-yet-initialized
        # one to a racing thread.
        if SimpleRateThrottle._backend is None:
            with SimpleRateThrottle._backend_lock:
                if SimpleRateThrottle._backend is None:
                    SimpleRateThrottle._backend = InMemoryRateLimitBackend()
        self._max_requests, self._window = self._parse_rate(self.rate)

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        """Return a unique key for this request. None to skip throttling."""
        return None

    async def allow_request(self, request: Request, view: ViewSet) -> bool:
        key = self.get_cache_key(request, view)
        if key is None:
            return True

        allowed, remaining, reset = self._backend.check_and_increment(
            key, self._max_requests, self._window
        )
        if not allowed:
            self._wait = reset
            return False
        return True

    def get_wait(self) -> int | None:
        return self._wait

    @staticmethod
    def _parse_rate(rate: str) -> tuple[int, int]:
        """Parse rate string like '100/hour' → (100, 3600)."""
        num, period = rate.split("/")
        num_requests = int(num)
        durations = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
        # Support abbreviated: "s", "m", "h", "d"
        abbrev = {"s": "second", "m": "minute", "h": "hour", "d": "day"}
        period = period.strip().lower()
        period = abbrev.get(period, period)
        window = durations.get(period, 3600)
        return num_requests, window


class AnonRateThrottle(SimpleRateThrottle):
    """Throttle unauthenticated users by IP address."""

    rate: str = "100/hour"

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        if request.user is not None:
            return None  # Skip for authenticated users
        return f"throttle:anon:{request.client_ip}"


class UserRateThrottle(SimpleRateThrottle):
    """Throttle authenticated users by user ID."""

    rate: str = "1000/hour"

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        if request.user is None:
            return f"throttle:anon:{request.client_ip}"
        user_id = request.user.id or "unknown"
        return f"throttle:user:{user_id}"


class ScopedRateThrottle(SimpleRateThrottle):
    """Throttle by a view-defined scope. Set throttle_scope on the ViewSet."""

    rate: str = "100/hour"

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        scope = view.__class__.__dict__.get("throttle_scope", "default")
        if request.user is None:
            ident = f"anon:{request.client_ip}"
        else:
            ident = f"user:{request.user.id or 'unknown'}"
        return f"throttle:{scope}:{ident}"


class DatabaseThrottle(SimpleRateThrottle):
    """Rate throttle backed by PostgreSQL UNLOGGED table.

    Persists rate limit counters across restarts and processes.
    Uses the same UNLOGGED table infrastructure as cache and sessions.

    Requires a DatabaseRateLimitBackend instance. Call set_backend() once
    at startup with your Database connection:

        from hyperdjango.ratelimit import DatabaseRateLimitBackend
        backend = DatabaseRateLimitBackend(db)
        await backend.ensure_table()
        DatabaseThrottle.set_backend(backend)

    Usage:
        class MyViewSet(ModelViewSet):
            throttle_classes = [DatabaseAnonThrottle]
    """

    rate: str = "100/hour"
    _db_backend: DatabaseRateLimitBackend | None = None

    def __init__(self):
        self._max_requests, self._window = self._parse_rate(self.rate)
        self._wait: int | None = None

    @classmethod
    def set_backend(cls, backend: DatabaseRateLimitBackend) -> None:
        """Set the shared database backend for all DatabaseThrottle subclasses."""
        cls._db_backend = backend

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        """Return a unique key for this request. None to skip throttling."""
        return None

    async def allow_request(self, request: Request, view: ViewSet) -> bool:
        key = self.get_cache_key(request, view)
        if key is None:
            return True

        if self._db_backend is None:
            # No database backend configured — allow request but log warning
            logging.getLogger("hyperdjango.rest").warning(
                "DatabaseThrottle used without backend; call DatabaseThrottle.set_backend() at startup"
            )
            return True

        try:
            allowed, remaining, reset = await self._db_backend.check_and_increment(
                key, self._max_requests, self._window
            )
        # blind-except: throttle backend down must not block all traffic; fail open and log the full error so the outage is visible server-side.
        except Exception:
            # Database unavailable — fail open to avoid blocking all requests
            logging.getLogger("hyperdjango.rest").exception(
                "DatabaseThrottle backend error; allowing request"
            )
            return True

        if not allowed:
            self._wait = reset
            return False
        return True

    def get_wait(self) -> int | None:
        return self._wait


class DatabaseAnonThrottle(DatabaseThrottle):
    """Database-backed anonymous rate throttle (by IP).

    Only throttles unauthenticated requests. Authenticated users are skipped.
    """

    rate: str = "100/hour"

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        if request.user is not None:
            return None
        return f"throttle:anon:{request.client_ip}"


class DatabaseUserThrottle(DatabaseThrottle):
    """Database-backed user rate throttle.

    Throttles by user ID for authenticated users, by IP for anonymous.
    """

    rate: str = "1000/hour"

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        if request.user is None:
            return f"throttle:anon:{request.client_ip}"
        user_id = request.user.id or "unknown"
        return f"throttle:user:{user_id}"


class DatabaseScopedThrottle(DatabaseThrottle):
    """Database-backed scoped rate throttle.

    Throttles by a view-defined scope. Set throttle_scope on the ViewSet.
    """

    rate: str = "100/hour"

    def get_cache_key(self, request: Request, view: ViewSet) -> str | None:
        scope = view.__class__.__dict__.get("throttle_scope", "default")
        if request.user is None:
            ident = f"anon:{request.client_ip}"
        else:
            ident = f"user:{request.user.id or 'unknown'}"
        return f"throttle:{scope}:{ident}"


# ── Authentication ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AuthResult:
    """Result of successful authentication."""

    user: Any
    auth_info: Any = None


class BaseAuthentication:
    """Base class for per-view authentication.

    Subclass and override authenticate(). Return AuthResult on success, None to skip.
    """

    async def authenticate(self, request: Request) -> AuthResult | None:
        """Try to authenticate the request. Return AuthResult or None."""
        return None


class SessionAuthentication(BaseAuthentication):
    """Authenticate via session cookie (wraps existing SessionAuth logic)."""

    async def authenticate(self, request: Request) -> AuthResult | None:
        # Session auth is typically handled by middleware that sets request.user
        # This authenticator checks if middleware already populated it with an
        # authenticated identity. SessionAuth sets an AnonymousUser (not None)
        # for anon requests, so ``is not None`` alone would treat anon as
        # authenticated — require ``is_authenticated`` too.
        user = request.user
        if user is not None and user.is_authenticated:
            return AuthResult(user=user)
        return None


class APIKeyAuthentication(BaseAuthentication):
    """Authenticate via API key header."""

    header_name: str = "x-api-key"

    async def authenticate(self, request: Request) -> AuthResult | None:
        key = request.headers.get(self.header_name)
        if key is None:
            return None
        if request.api_key_valid:
            return AuthResult(user=request.user, auth_info=key)
        return None


class TokenAuthentication(BaseAuthentication):
    """Authenticate via Authorization: Token <key> header.

    Override get_user_for_token() to implement token lookup.
    """

    keyword: str = "Token"

    async def authenticate(self, request: Request) -> AuthResult | None:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith(f"{self.keyword} "):
            return None
        token = auth_header[len(self.keyword) + 1 :].strip()
        if not token:
            return None
        user = await self.get_user_for_token(token)
        if user is None:
            raise AuthenticationFailed("Invalid token")
        return AuthResult(user=user, auth_info=token)

    async def get_user_for_token(self, token: str) -> Any:
        """Override to look up user by token. Return user or None."""
        return None


# ── Content Negotiation / Parsers ─────────────────────────────────────────────


class BaseParser:
    """Base class for request body parsers."""

    media_type: str = "*/*"

    async def parse(self, request: Request) -> Any:
        """Parse the request body and return structured data."""
        raise NotImplementedError("Subclass must implement parse()")


class JSONParser(BaseParser):
    """Parse JSON request body using SIMD-accelerated parser."""

    media_type: str = CONTENT_TYPE_JSON

    async def parse(self, request: Request) -> Any:
        return await request.json()


class FormParser(BaseParser):
    """Parse URL-encoded form data."""

    media_type: str = CONTENT_TYPE_FORM

    async def parse(self, request: Request) -> Any:
        return await request.form()


class MultiPartParser(BaseParser):
    """Parse multipart form data (files + fields)."""

    media_type: str = CONTENT_TYPE_MULTIPART

    async def parse(self, request: Request) -> Any:
        form_data = await request.form()
        files = await request.files()
        return {"fields": form_data, "files": files}


_PARSER_MAP: dict[str, type[BaseParser]] = {
    CONTENT_TYPE_JSON: JSONParser,
    CONTENT_TYPE_FORM: FormParser,
    CONTENT_TYPE_MULTIPART: MultiPartParser,
}


async def parse_request_body(
    request: Request, parser_classes: tuple[type[BaseParser], ...]
) -> Any:
    """Select the appropriate parser based on Content-Type and parse the body."""
    content_type = request.headers.get(HEADER_CONTENT_TYPE, CONTENT_TYPE_JSON)
    # Strip charset and boundary params: "multipart/form-data; boundary=..." → CONTENT_TYPE_MULTIPART
    base_type = content_type.split(";")[0].strip().lower()

    for parser_cls in parser_classes:
        if parser_cls.media_type == "*/*" or base_type.startswith(
            parser_cls.media_type
        ):
            parser = parser_cls()
            return await parser.parse(request)

    # No matching parser
    raise APIException(
        f"Unsupported media type: {base_type}",
        status_code=415,
    )


# ── Response Renderers ───────────────────────────────────────────────────────


class BaseRenderer:
    """Base response renderer."""

    media_type: str = "application/json"
    format_suffix: str = "json"

    def render(self, data: object, media_type: str | None = None) -> bytes:
        """Render the response data into bytes."""
        raise NotImplementedError("Subclass must implement render()")


class JSONRenderer(BaseRenderer):
    """Renders response data as JSON using SIMD-accelerated serialization."""

    media_type: str = "application/json"
    format_suffix: str = "json"

    def render(self, data: object, media_type: str | None = None) -> bytes:
        return fast_json_dumps(data)


_CSV_INJECTION_CHARS = frozenset({"=", "+", "-", "@", "\t", "\r"})


def _sanitize_csv_cell(value: str) -> str:
    """Prevent CSV formula injection by prefixing dangerous chars."""
    if value and value[0] in _CSV_INJECTION_CHARS:
        return "'" + value
    return value


class CSVRenderer(BaseRenderer):
    """Renders list-of-dicts response data as CSV."""

    media_type: str = "text/csv"
    format_suffix: str = "csv"

    def render(self, data: object, media_type: str | None = None) -> bytes:
        # For paginated responses, extract results list
        if isinstance(data, dict) and "results" in data:
            rows = data["results"]
        elif isinstance(data, list):
            rows = data
        else:
            return b""

        if not rows:
            return b""

        # Use first row's keys as CSV headers
        if not isinstance(rows[0], dict):
            return b""

        headers = list(rows[0].keys())
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            flat: dict[str, object] = {}
            for k, v in row.items():
                if isinstance(v, str):
                    flat[k] = _sanitize_csv_cell(v)
                elif isinstance(v, (int, float, type(None))):
                    flat[k] = v
                else:
                    flat[k] = _sanitize_csv_cell(str(v))
            writer.writerow(flat)
        return output.getvalue().encode("utf-8")


# ── RBAC Permission Classes ───────────────────────────────────────────────────


class ModelPermission(BasePermission):
    """Maps HTTP methods to model-level RBAC permissions.

    Checks add/change/delete/view permissions via the PermissionChecker from
    hyperdjango/auth/permissions.py's hierarchical RBAC system.

    Requires request.user to have a 'permissions' attribute or be a dict with 'id'.
    """

    # HTTP method → required model action
    _METHOD_MAP: dict[str, str] = {
        "GET": "view",
        "HEAD": "view",
        "OPTIONS": "view",
        "POST": "add",
        "PUT": "change",
        "PATCH": "change",
        "DELETE": "delete",
    }

    async def has_permission(self, request: Request, view: ViewSet) -> bool:
        if request.user is None:
            return False
        model = view.model
        if model is None:
            return True
        action = self._METHOD_MAP.get(request.method, "view")
        perm_name = f"{action}_{model._meta.table}"
        # Check via user's permission set (compatible with RBAC checker)
        perms = request.user.get("permissions", set())
        return perm_name in perms


class ObjectPermission(BasePermission):
    """Per-object permission check using RBAC object-level access control.

    Checks whether the user has permission on the specific object instance.
    Uses the owner pattern: object must have an owner_id or author_id field
    matching the current user.
    """

    owner_field: str = "owner_id"

    async def has_object_permission(
        self, request: Request, view: ViewSet, obj: Any
    ) -> bool:
        if request.user is None:
            return False
        user_id = request.user.id
        obj_owner = (
            obj.get(self.owner_field)
            if isinstance(obj, dict)
            # dynamic-attr: owner_field is a runtime-configured model attribute name
            else getattr(obj, self.owner_field, None)
        )
        if obj_owner is None and not isinstance(obj, dict):
            _logger.warning(
                "ObjectPermission: owner_field '%s' not found on %s",
                self.owner_field,
                type(obj).__name__,
            )
        # A null/missing owner must NEVER match: for AnonymousUser user_id is
        # None, so a bare ``obj_owner == user_id`` would grant access to every
        # ownerless row (None == None). Require BOTH sides to be a real identity
        # (mirrors auth/permissions._eval_is_owner).
        return obj_owner is not None and user_id is not None and obj_owner == user_id


# ── Metering Mixin ───────────────────────────────────────────────────────────


class MeteringMixin:
    """Mixin that auto-records API usage events via the MeterEngine.

    Add to your ViewSet to automatically track request count, response size,
    and latency per action. Integrates with quota enforcement.

    Usage:
        class PostViewSet(MeteringMixin, ModelViewSet):
            metering_meter_name = "api_usage"
    """

    metering_meter_name: str = "api_usage"
    metering_enabled: bool = True

    async def _record_metering_event(
        self, request: Request, response: Response, duration_ms: float
    ) -> None:
        """Record a metering event after the response is generated."""
        if not self.metering_enabled:
            return

        try:
            from hyperdjango.metering import get_meter_engine

            engine = get_meter_engine()
            if engine is None:
                return

            # Resolve account_id from request user
            account_id = "anonymous"
            if request.user is not None:
                uid = request.user.id
                if uid is not None:
                    account_id = str(uid)

            dimensions = {
                "requests": 1,
                "response_bytes": len(response.body),
                "duration_ms": duration_ms,
            }

            await engine.record(
                self.metering_meter_name,
                account_id,
                dimensions,
            )
        # blind-except: usage metering is telemetry and must never break the response it is measuring; the failure is logged for later reconciliation.
        except Exception:
            # Metering failure should not break the request
            _logger.debug("Metering event recording failed", exc_info=True)


# ── HTTP Caching Mixin ──────────────────────────────────────────────────────


class CacheableMixin:
    """ViewSet mixin for HTTP conditional responses (ETag, Last-Modified, Cache-Control).

    Usage:
        class PostViewSet(CacheableMixin, ModelViewSet):
            cache_max_age = 60  # Cache-Control: max-age=60
            cache_private = True  # Cache-Control: private
            cache_no_cache = False  # If True, Cache-Control: no-cache
    """

    cache_max_age: int = 0  # seconds; 0 = no max-age directive
    cache_private: bool = True  # private vs public
    cache_no_cache: bool = False  # no-cache forces revalidation

    def _compute_etag(self, content: bytes) -> str:
        """Compute weak ETag from response content hash."""
        digest = hashlib.sha256(content).hexdigest()[:32]
        return f'W/"{digest}"'

    def _build_cache_control(self) -> str:
        """Build Cache-Control header value."""
        parts: list[str] = []
        if self.cache_no_cache:
            parts.append("no-cache")
        if self.cache_private:
            parts.append("private")
        else:
            parts.append("public")
        if self.cache_max_age > 0:
            parts.append(f"max-age={self.cache_max_age}")
        return ", ".join(parts)

    def _check_conditional(self, request: Request, etag: str) -> bool:
        """Check If-None-Match header. Returns True if 304 should be returned."""
        if_none_match = request.headers.get("if-none-match", "")
        if not if_none_match:
            return False
        server_value = self._extract_etag_value(etag)
        for raw_tag in if_none_match.split(","):
            if self._extract_etag_value(raw_tag.strip()) == server_value:
                return True
        return False

    @staticmethod
    def _extract_etag_value(tag: str) -> str:
        """Extract the opaque value from an ETag string, stripping W/ prefix and quotes."""
        tag = tag.strip()
        if tag.startswith('W/"') and tag.endswith('"'):
            return tag[3:-1]
        if tag.startswith('"') and tag.endswith('"'):
            return tag[1:-1]
        return tag

    def _apply_cache_headers(
        self, response: Response, request: Request, content_bytes: bytes | None = None
    ) -> Response:
        """Apply ETag and Cache-Control headers to response.

        Returns 304 Response if conditional check passes, otherwise modifies
        response in-place and returns it.
        """
        cache_control = self._build_cache_control()
        if cache_control:
            response.headers["Cache-Control"] = cache_control

        if content_bytes is not None:
            etag = self._compute_etag(content_bytes)
            response.headers["ETag"] = etag
            if request.method in ("GET", "HEAD") and self._check_conditional(
                request, etag
            ):
                return Response(
                    status=304,
                    body=b"",
                    headers={"ETag": etag, "Cache-Control": cache_control},
                )
        return response


# ── Source Traversal + SerializerMethodField ──────────────────────────────────


def _resolve_dotted_source(obj: Any, source: str) -> Any:
    """Resolve a dotted source path like 'author.name' on an object.

    Traverses through dicts (via .get()) and objects (via getattr)
    following the dot-separated path.
    """
    current = obj
    for part in source.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            # dynamic-attr: part is a runtime segment of a user-configured dotted source path
            current = getattr(current, part, None)
    return current


class SerializerMethodField:
    """A read-only field that calls a method on the serializer.

    Convention: field named 'full_name' calls self.get_full_name(obj).
    Or specify method_name explicitly.

    Usage:
        class UserSerializer(Serializer):
            full_name: str = SerializerMethodField()
            custom: str = SerializerMethodField(method_name="compute_custom")

            def get_full_name(self, obj):
                return f"{obj['first_name']} {obj['last_name']}"

            def compute_custom(self, obj):
                return "custom_value"
    """

    def __init__(self, method_name: str | None = None):
        self.method_name = method_name
        # These are set by the metaclass
        self._field_name: str = ""
        self._is_serializer_method_field = True


# ── Typed Serializer Fields ──────────────────────────────────────────────────


class TypedField:
    """Base class for typed serializer fields with to_representation/to_internal_value."""

    def __init__(
        self, read_only: bool = False, required: bool = True, default: Any = None
    ):
        self.read_only = read_only
        self.required = required
        self.default = default
        self._field_name: str = ""
        self._is_typed_field = True
        # Declared on the base (default False) so every TypedField instance has
        # the attribute — HiddenField sets it True. Lets is_valid / the serialize
        # plan test it with a plain attribute read (no getattr on a known type).
        self._is_hidden = False

    def to_representation(self, value: Any) -> Any:
        return value

    def to_internal_value(self, data: Any) -> Any:
        return data


class DateTimeField(TypedField):
    """DateTime serialization with ISO 8601 format."""

    def __init__(self, format_str: str = "iso", **kwargs: Any):
        super().__init__(**kwargs)
        self.format_str = format_str

    def to_representation(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        return str(value)

    def to_internal_value(self, data: Any) -> datetime.datetime:
        if isinstance(data, datetime.datetime):
            return data
        if isinstance(data, str):
            return datetime.datetime.fromisoformat(data)
        raise ValueError(f"Expected datetime string, got {type(data).__name__}")


class DateField(TypedField):
    """Date serialization with ISO format."""

    def to_representation(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime.date):
            return value.isoformat()
        return str(value)

    def to_internal_value(self, data: Any) -> datetime.date:
        if isinstance(data, datetime.date):
            return data
        if isinstance(data, str):
            return datetime.date.fromisoformat(data)
        raise ValueError(f"Expected date string, got {type(data).__name__}")


class TimeField(TypedField):
    """Time serialization with ISO format."""

    def to_representation(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime.time):
            return value.isoformat()
        return str(value)

    def to_internal_value(self, data: Any) -> datetime.time:
        if isinstance(data, datetime.time):
            return data
        if isinstance(data, str):
            return datetime.time.fromisoformat(data)
        raise ValueError(f"Expected time string, got {type(data).__name__}")


class ChoiceField(TypedField):
    """Validates value against a set of allowed choices."""

    def __init__(self, choices: list[str] | list[int], **kwargs: Any):
        super().__init__(**kwargs)
        self.choices = choices

    def to_internal_value(self, data: Any) -> Any:
        if data in self.choices:
            return data
        # Coerce to the declared choice type before rejecting: form/multipart
        # bodies deliver everything as strings, so an int-choice field would
        # wrongly reject "1". Try casting the input to each choice's type and
        # return the canonical (typed) choice value on a match.
        for choice in self.choices:
            try:
                if type(choice)(data) == choice:
                    return choice
            except ValueError, TypeError:
                continue
        raise ValueError(f"Must be one of: {self.choices}")


class MultipleChoiceField(TypedField):
    """Validates a list of values against allowed choices."""

    def __init__(self, choices: list[str] | list[int], **kwargs: Any):
        super().__init__(**kwargs)
        self.choices = set(choices)

    def to_internal_value(self, data: Any) -> list[Any]:
        if not isinstance(data, list):
            raise ValueError("Expected a list")
        # Deduplicate before validation to avoid redundant checks
        seen: set[object] = set()
        unique_values: list[object] = []
        for v in data:
            if v not in seen:
                seen.add(v)
                unique_values.append(v)
        invalid = [v for v in unique_values if v not in self.choices]
        if invalid:
            raise ValueError(f"Invalid choices: {invalid}")
        return data


class UUIDField(TypedField):
    """UUID serialization/deserialization."""

    def to_representation(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def to_internal_value(self, data: Any) -> uuid.UUID:
        if isinstance(data, uuid.UUID):
            return data
        if isinstance(data, str):
            return uuid.UUID(data)
        raise ValueError(f"Expected UUID string, got {type(data).__name__}")


class DecimalField(TypedField):
    """Decimal serialization with precision control."""

    def __init__(
        self,
        max_digits: int | None = None,
        decimal_places: int | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.max_digits = max_digits
        self.decimal_places = decimal_places

    def to_representation(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def to_internal_value(self, data: Any) -> decimal.Decimal:
        if isinstance(data, decimal.Decimal):
            value = data
        else:
            try:
                value = decimal.Decimal(str(data))
            except decimal.InvalidOperation, ValueError:
                raise ValueError(f"Invalid decimal: {data}")
        if not value.is_finite():  # reject NaN / Infinity
            raise ValueError(f"Invalid decimal: {data}")

        # Enforce decimal_places by quantizing (rounding excess scale away)...
        if self.decimal_places is not None:
            quant = decimal.Decimal(1).scaleb(-self.decimal_places)
            try:
                value = value.quantize(quant, rounding=decimal.ROUND_HALF_UP)
            except decimal.InvalidOperation:
                raise ValueError(f"Invalid decimal: {data}")

        # ...then enforce max_digits (total significant digits) with a clean 400.
        if self.max_digits is not None:
            _, digittuple, exponent = value.as_tuple()
            if exponent >= 0:
                total_digits = len(digittuple) + exponent
            else:
                total_digits = max(len(digittuple), -exponent)
            if total_digits > self.max_digits:
                raise ValueError(
                    f"Ensure that there are no more than {self.max_digits} "
                    f"digits in total."
                )
        return value


class EmailField(TypedField):
    """Email validation field."""

    def to_internal_value(self, data: Any) -> str:
        if not isinstance(data, str) or "@" not in data:
            raise ValueError(f"Invalid email: {data}")
        return data


class URLField(TypedField):
    """URL validation field."""

    def to_internal_value(self, data: Any) -> str:
        if not isinstance(data, str) or not data.startswith(("http://", "https://")):
            raise ValueError(f"Invalid URL: {data}")
        return data


class IPAddressField(TypedField):
    """IPv4/IPv6 address validation."""

    def to_internal_value(self, data: Any) -> str:
        if not isinstance(data, str):
            raise ValueError(f"Expected IP address string, got {type(data).__name__}")
        # Basic validation
        parts = data.split(".")
        if len(parts) == 4:
            # IPv4
            for part in parts:
                if not part.isdigit() or not 0 <= int(part) <= 255:
                    raise ValueError(f"Invalid IPv4 address: {data}")
            return data
        if ":" in data:
            return data  # IPv6 — trust format
        raise ValueError(f"Invalid IP address: {data}")


class ReadOnlyField(TypedField):
    """Always read-only field — never accepted in input."""

    def __init__(self, **kwargs: Any):
        super().__init__(read_only=True, required=False, **kwargs)


class HiddenField(TypedField):
    """Excluded from representation, included in validated_data with default."""

    def __init__(self, default: Any = None, **kwargs: Any):
        super().__init__(read_only=False, required=False, default=default, **kwargs)
        self._is_hidden = True


# ── File magic bytes for image validation ────────────────────────────────────

_IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (
        b"RIFF",
    ),  # also requires content[8:12] == b"WEBP" (checked in _check_magic_bytes)
    "bmp": (b"BM",),
    "svg": (b"<?xml", b"<svg"),
}

_DEFAULT_ALLOWED_EXTENSIONS = frozenset(
    {
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "bmp",
        "svg",
        "pdf",
        "txt",
        "csv",
        "json",
        "xml",
        "zip",
    }
)
_DEFAULT_IMAGE_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
)
_DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


@dataclass(slots=True)
class FileUploadField(TypedField):
    """Validates uploaded file — size, extension, presence."""

    max_size: int = _DEFAULT_MAX_FILE_SIZE
    allowed_extensions: frozenset[str] = _DEFAULT_ALLOWED_EXTENSIONS

    def __post_init__(self) -> None:
        super().__init__(required=True)

    def __init__(
        self,
        max_size: int = _DEFAULT_MAX_FILE_SIZE,
        allowed_extensions: frozenset[str] = _DEFAULT_ALLOWED_EXTENSIONS,
        **kwargs: Any,
    ):
        self.max_size = max_size
        self.allowed_extensions = allowed_extensions
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> Any:
        if data is None or data == b"":
            if self.required:
                raise ValidationError("No file uploaded.")
            return None
        # data may be raw bytes (e.g. from tests or direct body)
        if isinstance(data, bytes):
            if len(data) > self.max_size:
                raise ValidationError(
                    f"File exceeds maximum size of {self.max_size} bytes."
                )
            return data
        # File-like object from multipart parsing — we don't control the class,
        # so hasattr is justified for duck-typing external upload objects.
        name: str = (
            data.name if hasattr(data, "name") else ""
        )  # hasattr OK: external upload objects
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if self.allowed_extensions and ext not in self.allowed_extensions:
            raise ValidationError(
                f"File extension '.{ext}' not allowed. Allowed: {sorted(self.allowed_extensions)}"
            )
        # hasattr OK: external file-like objects — we duck-type .read()
        content: bytes = data.read() if hasattr(data, "read") else bytes(data)
        if len(content) > self.max_size:
            raise ValidationError(
                f"File exceeds maximum size of {self.max_size} bytes."
            )
        return content

    def to_representation(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return str(value)


@dataclass(slots=True)
class ImageUploadField(FileUploadField):
    """Validates uploaded image — checks magic bytes + extension + size."""

    allowed_extensions: frozenset[str] = _DEFAULT_IMAGE_EXTENSIONS
    verify_magic_bytes: bool = True

    def __init__(
        self,
        allowed_extensions: frozenset[str] = _DEFAULT_IMAGE_EXTENSIONS,
        verify_magic_bytes: bool = True,
        **kwargs: Any,
    ):
        self.verify_magic_bytes = verify_magic_bytes
        super().__init__(allowed_extensions=allowed_extensions, **kwargs)

    def to_internal_value(self, data: Any) -> Any:
        content = super().to_internal_value(data)
        if content is None:
            return None
        if self.verify_magic_bytes and isinstance(content, bytes) and len(content) >= 8:
            if not self._check_magic_bytes(content):
                raise ValidationError(
                    "File does not appear to be a valid image (invalid header bytes)."
                )
        return content

    def _check_magic_bytes(self, content: bytes) -> bool:
        for fmt, sigs in _IMAGE_SIGNATURES.items():
            for sig in sigs:
                if content[: len(sig)] == sig:
                    # WebP: RIFF....WEBP — many formats use RIFF, so also check bytes 8-12
                    if fmt == "webp":
                        if len(content) >= 12 and content[8:12] == b"WEBP":
                            return True
                        continue
                    return True
        return False


class CurrentUserDefault:
    """Default value that resolves to the current user from request context.

    Usage:
        class PostSerializer(ModelSerializer):
            author_id: int = HiddenField(default=CurrentUserDefault())
    """

    # Signals _resolve_default() to invoke this with the serializer context
    # (rather than as a zero-arg callable) so it can read request.user.
    requires_context: bool = True

    def __call__(self, context: dict[str, Any]) -> Any:
        request = context.get("request")
        if request is None or request.user is None:
            return None
        return request.user.id


# ── SimpleMetadata (OPTIONS endpoint) ─────────────────────────────────────────


class SimpleMetadata:
    """Generates metadata for OPTIONS endpoint — field types, constraints, choices.

    Returns API description with field introspection for POST/PUT actions.
    """

    _TYPE_LABELS: dict[type, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }

    def determine_metadata(self, request: Request, view: ViewSet) -> dict[str, Any]:
        """Return metadata for the view."""
        metadata: dict[str, Any] = {
            "name": type(view).__name__,
            "description": type(view).__doc__ or "",
            "allowed_methods": list(view._action_map.keys()),
        }

        # Add field metadata for write actions
        try:
            serializer_class = view.get_serializer_class()
            if serializer_class is not None:
                fields = serializer_class._serializer_fields
                actions: dict[str, dict[str, Any]] = {}
                field_info: dict[str, dict[str, Any]] = {}
                for name, info in fields.items():
                    field_meta: dict[str, Any] = {
                        "type": self._TYPE_LABELS.get(info.field_type, "string"),
                        "required": info.required,
                        "read_only": info.read_only,
                    }
                    if info.label:
                        field_meta["label"] = info.label
                    if info.help_text:
                        field_meta["help_text"] = info.help_text
                    if info.min_length is not None:
                        field_meta["min_length"] = info.min_length
                    if info.max_length is not None:
                        field_meta["max_length"] = info.max_length
                    if info.min_value is not None:
                        field_meta["min_value"] = info.min_value
                    if info.max_value is not None:
                        field_meta["max_value"] = info.max_value
                    if info.choices is not None:
                        field_meta["choices"] = info.choices
                    field_info[name] = field_meta
                actions["fields"] = field_info
                metadata["actions"] = actions
        except ValueError, AttributeError:
            pass

        return metadata


# ── @action decorator ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class ActionMeta:
    """Metadata stored on @action-decorated methods."""

    methods: list[str]
    detail: bool
    url_path: str
    url_name: str
    input_serializer: type[Serializer] | None = None
    output_serializer: type[Serializer] | None = None


def action(
    methods: list[str],
    detail: bool = False,
    url_path: str | None = None,
    url_name: str | None = None,
    input_serializer: type[Serializer] | None = None,
    output_serializer: type[Serializer] | None = None,
):
    """Mark a ViewSet method as a routable custom action.

    Args:
        methods: HTTP methods this action responds to (["GET"], ["POST"], etc.)
        detail: True for detail routes (/{pk}/action), False for list routes (/action)
        url_path: URL path suffix (defaults to method name)
        url_name: Route name suffix (defaults to method name with _ → -)
        input_serializer: Serializer class for auto-validating request body before
            the action is called. On validation failure, returns 400 with errors.
            Validated data is stored on request._validated_data.
        output_serializer: Serializer class for documentation/OpenAPI schema generation.
            Does not affect runtime behavior.

    Usage:
        class PublishInput(Serializer):
            publish_date = SerializerField(field_type=str, required=False)
            notify_subscribers = SerializerField(field_type=bool, required=False)

        class PostViewSet(ModelViewSet):
            @action(methods=["POST"], detail=True, input_serializer=PublishInput)
            async def publish(self, request, **kwargs):
                data = request._validated_data  # already validated
                ...
    """

    def decorator(func: Any) -> Any:
        func._is_action = True
        func._action_methods = [m.upper() for m in methods]
        func._action_detail = detail
        func._action_url_path = url_path or func.__name__
        func._action_url_name = url_name or func.__name__.replace("_", "-")
        func._action_meta = ActionMeta(
            methods=[m.upper() for m in methods],
            detail=detail,
            url_path=url_path or func.__name__,
            url_name=url_name or func.__name__.replace("_", "-"),
            input_serializer=input_serializer,
            output_serializer=output_serializer,
        )
        return func

    return decorator


# ── ViewSet ───────────────────────────────────────────────────────────────────


class ViewSet(View):
    """Groups related API actions into a single class.

    Actions are bound to HTTP methods via as_view(actions={"get": "list"}).
    Supports permissions, filtering, pagination, and versioning.
    """

    serializer_class: type[Serializer] | None = None
    queryset: Any = None
    model: type | None = None
    lookup_field: str = "id"
    lookup_url_kwarg: str | None = None

    # Pluggable behavior (tuples to prevent accidental shared-state mutation)
    permission_classes: tuple[type[BasePermission], ...] = (AllowAny,)
    authentication_classes: tuple[type[BaseAuthentication], ...] = ()
    throttle_classes: tuple[type[BaseThrottle], ...] = ()
    pagination_class: type[APIPagination] | None = None
    filter_backends: tuple[type[FilterBackend], ...] = ()
    parser_classes: tuple[type[BaseParser], ...] = (
        JSONParser,
        FormParser,
        MultiPartParser,
    )
    versioning_class: type[APIVersioning] | None = None

    # FTS search defaults (used by SearchFilter when filter_backends includes it)
    search_config: str = "english"
    search_type: str = "websearch"
    metadata_class: type[SimpleMetadata] | None = SimpleMetadata
    renderer_classes: tuple[type[BaseRenderer], ...] = (JSONRenderer,)

    # Filter/search/ordering config (tuples for immutable class-level defaults)
    filterset_fields: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    ordering_fields: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()

    # Native JSON fast path — auto-enabled when the serializer is a pure
    # model-field passthrough (identity). list/retrieve then skip Python
    # serialization entirely and use Zig _db_query_json to build JSON directly
    # from the PostgreSQL wire protocol. Set to False to force the Python path.
    use_native_json: bool = True

    # Field-level RBAC: set to the model name (e.g., "book") to enable per-field
    # hidden/readonly/writable filtering via FieldPermission. Uses
    # PermissionChecker.filter_fields() on serialized output.
    field_permissions_model: str = ""

    # Set during dispatch
    action: str = ""
    _action_map: dict[str, str] = {}
    _parent_lookup: str = ""  # Set by NestedRouter — URL param name for parent resource
    _validated_data: dict[str, object] | None = None

    @property
    def validated_data(self) -> dict[str, object]:
        """Access validated data from input_serializer auto-validation.

        Only available on @action methods that declare input_serializer.
        Raises RuntimeError if no input_serializer was used.
        """
        if self._validated_data is None:
            raise RuntimeError(
                "validated_data is only available on @action methods with input_serializer"
            )
        return self._validated_data

    def get_queryset(self) -> Any:
        """Return the queryset for this viewset."""
        if self.queryset is not None:
            return self.queryset
        if self.model is not None:
            return self.model.objects
        raise ValueError("ViewSet requires 'queryset' or 'model'")

    def get_serializer_class(self) -> type[Serializer]:
        """Return the serializer class. Override for per-action serializers."""
        if self.serializer_class is None:
            raise ValueError("ViewSet requires 'serializer_class'")
        return self.serializer_class

    def get_serializer(self, **kwargs: Any) -> Serializer:
        """Instantiate and return a serializer with context."""
        cls = self.get_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return cls(**kwargs)

    def get_serializer_context(self) -> dict[str, object]:
        """Return extra context for serializer."""
        return {"request": self.request, "view": self}

    def _can_use_native_json(self) -> bool:
        """Check if native JSON fast path is available for this ViewSet.

        True only when use_native_json is enabled AND the serializer is a
        ModelSerializer that is a pure model-field passthrough (identity —
        no computed/method/nested/relational fields, exposed via the
        precomputed ``_native_select_columns`` plan), AND no per-instance
        Python post-processing is required:

        - external ID encoding (PublicIDMixin / IDMixin) rewrites the ``id``
          key/value in Python, which the native path cannot reproduce;
        - field-level RBAC (``field_permissions_model``) inspects each
          serialized dict.

        Either of those forces the Python hydrate+serialize path.
        """
        if not self.use_native_json:
            return False
        serializer_cls = self.get_serializer_class()
        if not isinstance(serializer_cls, ModelSerializerMeta):
            return False
        if serializer_cls._native_select_columns is None:
            return False
        if self._get_public_id_strategy() is not None:
            return False
        if self.field_permissions_model:
            return False
        # Object-level permissions (has_object_permission) are enforced ONLY
        # inside get_object() → check_object_permissions(), which the native
        # fast path skips. If any permission class defines its own
        # has_object_permission, taking the native path would leak objects the
        # caller is not allowed to see (IDOR). Force the slow path so the
        # object-permission check runs.
        for perm_cls in self.permission_classes:
            if (
                perm_cls.has_object_permission
                is not BasePermission.has_object_permission
            ):
                return False
        return True

    def _get_id_manager(self):
        """Get IDManager for this ViewSet's model, if it uses IDMixin."""
        model = self.model
        if model is None:
            return None
        # IDMixin sets _id_manager at class definition time
        return model.__dict__.get(
            "_id_manager"
        )  # dict access — not getattr, IDMixin is our class

    def _has_public_id(self) -> bool:
        """Check if this ViewSet's model uses PublicIDMixin or IDMixin."""
        model = self.model
        if model is None:
            return False
        if self._get_id_manager() is not None:
            return True
        return isinstance(model, type) and issubclass(model, PublicIDMixin)

    def _get_public_id_strategy(self) -> str | None:
        """Get the ID strategy for this ViewSet's model, or None.

        Returns IDMixin mode (signed/encoded/raw) or PublicIDMixin strategy.
        """
        mgr = self._get_id_manager()
        if mgr is not None:
            return mgr.config.mode  # "signed", "encoded", "raw", "random"
        if not self._has_public_id():
            return None
        return self.model._public_id_strategy

    def _make_pk_encoder(self, request: object = None):
        """Resolve the PK→external-ID encoder ONCE and return a callable
        ``encode_one(pk)``.

        A list response otherwise re-resolves the ID manager / mode / encoder /
        width (and re-reads ``request.user``) inside ``_encode_pk`` for every
        row; all of those are constant per request. The returned closure
        produces output byte-identical to ``_encode_pk(pk, request=request)``.
        """
        mgr = self._get_id_manager()
        if mgr is not None:
            mode = mgr.config.mode
            if mode in _SIGNED_OR_ENCODED_MODES:
                user_id = None
                if mgr.config.include_user and request is not None:
                    user = request.user
                    if user is not None:
                        user_id = user.id
                return lambda pk: mgr.encode(pk, user_id=user_id)
            return lambda pk: pk  # raw or random mode — passthrough
        strategy = self._get_public_id_strategy()
        if strategy == IDStrategy.ENCODED_PK:
            encoder = self.model._public_id_encoder
            width = self.model._public_id_width
            if width > 0:
                return lambda pk: encoder.encode_padded(pk, width)
            return lambda pk: encoder.encode(pk)
        return lambda pk: pk

    def _encode_pk(self, pk: int, *, request: object = None) -> str | int:
        """Encode a PK to an external ID string.

        For IDMixin signed/encoded: uses IDManager.encode().
        For PublicIDMixin encoded_pk: uses BaseEncoder.
        For random/uuid7/raw: returns pk unchanged.
        """
        return self._make_pk_encoder(request)(pk)

    def _decode_public_id(
        self, external_id: str, *, request: object = None
    ) -> tuple[str, object]:
        """Decode an external ID to a lookup field and value.

        Returns (lookup_field, lookup_value) tuple:
        - IDMixin signed/encoded: ("id", decoded_int_pk)
        - IDMixin raw: ("id", int(external_id))
        - PublicIDMixin encoded_pk: ("id", decoded_int_pk)
        - PublicIDMixin random/uuid7: ("public_id", external_id_string)
        - No ID system: (self.lookup_field, external_id_as_is)
        """
        mgr = self._get_id_manager()
        if mgr is not None:
            mode = mgr.config.mode
            if mode in _SIGNED_ENCODED_RAW_MODES:
                try:
                    user_id = None
                    if mgr.config.include_user and request is not None:
                        user = request.user
                        if user is not None:
                            user_id = user.id
                    pk = mgr.decode(external_id, user_id=user_id)
                except ValueError, KeyError:
                    raise NotFound("Not found.")  # Don't leak signature failure info
                return ("id", pk)
            # IDMixin random mode — lookup by public_id column
            return ("public_id", external_id)
        strategy = self._get_public_id_strategy()
        if strategy == IDStrategy.ENCODED_PK:
            try:
                pk = self.model._public_id_encoder.decode(external_id)
            except ValueError, KeyError:
                raise NotFound("Not found")
            return ("id", pk)
        if strategy in _RANDOM_STRATEGIES:
            return ("public_id", external_id)
        # No ID system — use lookup_field directly
        return (self.lookup_field, external_id)

    def _encode_response_ids(self, data: object, *, request: object = None) -> object:
        """Encode IDs in serialized response data for external output."""
        strategy = self._get_public_id_strategy()
        if strategy is None:
            return data
        if isinstance(data, list):
            # Resolve the encoder ONCE for the whole list, not once per row.
            encode_one = self._make_pk_encoder(request)
            return [
                self._encode_single_item_id(item, strategy, encode_one) for item in data
            ]
        if isinstance(data, dict):
            encode_one = self._make_pk_encoder(request)
            return self._encode_single_item_id(data, strategy, encode_one)
        return data

    def _encode_single_item_id(
        self, item: dict[str, object], strategy: str, encode_one
    ) -> dict[str, object]:
        """Encode the ID in a single serialized item dict.

        Mutates ``item`` in place: the dict is freshly built per request by the
        serializer (``_serialize_one`` / flat encoder), owned by this response,
        and consumed once — so overwriting ``id`` needs no defensive copy.
        """
        if not isinstance(item, dict):
            return item
        if strategy in _ENCODE_PK_STRATEGIES:
            pk = item.get("id")
            if pk is not None and isinstance(pk, int):
                item["id"] = encode_one(pk)
        elif strategy in _RANDOM_STRATEGIES:
            public_id = item.get("public_id")
            if public_id is not None:
                item["id"] = public_id
        return item

    def _negotiate_renderer(self, request: Request) -> BaseRenderer:
        """Select renderer by URL format suffix or Accept header (q-aware).

        Delegates to the shared ``_negotiate_renderer_impl`` so this path and
        the GenericViewSet path stay in lock-step.
        """
        return _negotiate_renderer_impl(self.renderer_classes, request)

    def _render_response(
        self, request: Request, data: object, status: int = 200
    ) -> Response:
        """Render data through the negotiated renderer.

        Returns a Response with the appropriate Content-Type and headers.
        For CSV responses, adds Content-Disposition for download.
        """
        renderer = self._negotiate_renderer(request)
        body = renderer.render(data)
        content_type = renderer.media_type
        headers: dict[str, str] = {}
        if isinstance(renderer, CSVRenderer):
            headers["Content-Disposition"] = 'attachment; filename="export.csv"'
        return Response(
            body=body, status=status, headers=headers, content_type=content_type
        )

    async def get_object(self) -> Any:
        """Retrieve a single object by lookup field. Raises NotFound.

        If the model uses PublicIDMixin, the URL parameter is treated as a
        public ID and decoded appropriately:
        - encoded_pk: decoded to integer PK, looked up by 'id'
        - random/uuid7: looked up by 'public_id' column
        - no mixin: looked up by self.lookup_field (existing behavior)
        """
        qs = self.get_queryset()
        lookup_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs.get(lookup_kwarg)
        if lookup_value is None:
            raise NotFound("No lookup value provided")

        # Decode public ID to internal lookup field + value. Thread `request`
        # so include_user IDMixin binding decodes against the actual user
        # (self.request is set on both ASGI and native viewset dispatch).
        field, value = self._decode_public_id(lookup_value, request=self.request)

        # `.get()` raises the model's OWN DoesNotExist on a missing row (each
        # model — and the test doubles — declares its own). Resolve it from the
        # QUERYSET's model, not self.model, which is None/absent on generic views.
        # A genuine "no such row" is a clean 404; a DB/ORM failure still surfaces
        # as a 500 below.
        # get_queryset() is a user override point — it may return a real QuerySet
        # (always has _model) or an arbitrary/duck-typed object, so _model is
        # genuinely optional here; we fall back to the base exception.
        # dynamic-attr: qs is duck-typed at the get_queryset() override boundary
        qs_model = getattr(qs, "_model", None)
        not_found_exc = (
            qs_model.DoesNotExist if qs_model is not None else Model.DoesNotExist
        )
        try:
            obj = await qs.get(**{field: value})
        except not_found_exc:
            # The row genuinely isn't there — a real 404.
            raise NotFound("Not found")
        except Exception:
            # A DB outage, ORM bug, or MultipleObjectsReturned is NOT a 404.
            # Silently mapping it to NotFound hides infra failures with zero
            # telemetry — log with context and let it surface as a 500.
            _logger.exception(
                "get_object: unexpected error looking up object by %s=%r", field, value
            )
            raise
        await self.check_object_permissions(self.request, obj)
        return obj

    def filter_queryset(self, queryset: Any) -> Any:
        """Apply all filter backends to the queryset."""
        for backend_cls in self.filter_backends:
            backend = backend_cls()
            queryset = backend.filter_queryset(self.request, queryset, self)
        return queryset

    async def perform_authentication(self, request: Request) -> None:
        """Run authentication classes to populate request.user.

        Authenticators are tried in order. First success sets request.user/request.auth.
        If none succeed, request.user remains as-is (may be None from middleware).
        """
        for auth_cls in self.authentication_classes:
            auth = auth_cls()
            result = await auth.authenticate(request)
            if result is not None:
                request.user = result.user
                request.auth = result.auth_info
                return

    async def check_permissions(self, request: Request) -> None:
        """Check all permission classes. Raises on failure."""
        for perm_cls in self.permission_classes:
            perm = perm_cls()
            if not await perm.has_permission(request, self):
                # 401 (not 403) when the failure is a missing/anonymous identity:
                # request.user is None OR an unauthenticated AnonymousUser.
                if isinstance(perm, IsAuthenticated) and (
                    request.user is None or not request.user.is_authenticated
                ):
                    raise AuthenticationFailed("Authentication required")
                raise PermissionDenied("Permission denied")

    async def check_object_permissions(self, request: Request, obj: Any) -> None:
        """Check object-level permissions. Raises on failure."""
        for perm_cls in self.permission_classes:
            perm = perm_cls()
            if not await perm.has_object_permission(request, self, obj):
                raise PermissionDenied("Permission denied")

    async def check_throttles(self, request: Request) -> None:
        """Check all throttle classes. Raises Throttled on limit exceeded."""
        for throttle_cls in self.throttle_classes:
            throttle = throttle_cls()
            if not await throttle.allow_request(request, self):
                wait = throttle.get_wait()
                detail = "Request was throttled"
                headers = None
                if wait is not None:
                    detail = f"Request was throttled. Retry after {wait} seconds"
                    # Retry-After on every 429 producer (F3): machine-readable
                    # back-off matching ratelimit.build_429_response and the guard.
                    headers = {"Retry-After": str(wait)}
                raise Throttled(detail, headers=headers)

    async def get_request_data(self, request: Request) -> Any:
        """Parse request body using the configured parser classes."""
        return await parse_request_body(request, self.parser_classes)

    @classmethod
    def as_view(
        cls,
        actions: dict[str, str] | None = None,
        parent_lookup: str = "",
        **initkwargs: Any,
    ) -> Any:
        """Return an async handler with action mapping.

        Args:
            actions: Maps HTTP method to action name, e.g. {"get": "list", "post": "create"}
            parent_lookup: URL param name for parent resource (set by NestedRouter)
        """
        # Pre-resolve handlers at route registration time (not per-request)
        # Tuple: (action_name, handler, is_async, action_meta_or_None)
        _resolved: dict[str, tuple[str, Any, bool, ActionMeta | None]] = {}
        for method, action_name in (actions or {}).items():
            for klass in cls.__mro__:
                if action_name in klass.__dict__:
                    handler = klass.__dict__[action_name]
                    # Extract _action_meta at registration time so dispatch never
                    # needs hasattr/getattr on arbitrary handler functions
                    meta: ActionMeta | None = (
                        handler._action_meta
                        if "_action_meta" in handler.__dict__
                        else None
                    )  # __dict__ lookup on function object — no getattr; functions store decorator attrs in __dict__
                    _resolved[method] = (
                        action_name,
                        handler,
                        inspect.iscoroutinefunction(handler),
                        meta,
                    )
                    break

        _parent_lookup = parent_lookup

        async def view(request: Request, **kwargs: Any) -> Response:
            self = cls(**initkwargs)
            self._action_map = actions or {}
            self._parent_lookup = _parent_lookup
            self.request = request
            self.kwargs = kwargs

            try:
                # Versioning
                if self.versioning_class is not None:
                    request.version = self.versioning_class().determine_version(request)

                # Authentication (populate request.user before perm checks)
                await self.perform_authentication(request)

                # Permissions
                await self.check_permissions(request)

                # Throttling (after auth so we know the user)
                await self.check_throttles(request)

                # Handle OPTIONS → return metadata
                method = request.method.lower()
                if method == "options" and self.metadata_class is not None:
                    metadata = self.metadata_class().determine_metadata(request, self)
                    return Response.json(metadata)

                # Look up pre-resolved handler
                resolved = _resolved.get(method)
                if resolved is None:
                    raise MethodNotAllowed(f"Method {request.method} not allowed")
                action_name, handler, is_async, action_meta = resolved
                self.action = action_name

                # Auto-validate request body when @action declares input_serializer
                if action_meta is not None and action_meta.input_serializer is not None:
                    body = await self.get_request_data(request)
                    if not isinstance(body, dict):
                        raise ValidationError(
                            f"Expected JSON object, got {type(body).__name__}"
                        )
                    serializer = action_meta.input_serializer(input_data=body)
                    if (
                        not serializer.is_valid()
                        or not await serializer.avalidate_relations()
                    ):
                        raise ValidationError(
                            "Validation failed",
                            errors={
                                k: [v] if isinstance(v, str) else v
                                for k, v in serializer.errors.items()
                            },
                        )
                    request._validated_data = serializer.validated_data
                    self._validated_data = serializer.validated_data

                # Call handler
                if is_async:
                    response = await handler(self, request, **kwargs)
                else:
                    response = handler(self, request, **kwargs)

                return response

            except HTTPException as exc:
                # APIException now subclasses HTTPException, so this one clause
                # covers both a REST APIException and the framework HTTPException
                # (e.g. a guard raising through). The single mapper emits the
                # unified {"detail","status"} body (+ errors/headers) — identical
                # to the plain-handler and middleware paths.
                return exception_to_response(exc)
            # blind-except: last-resort request boundary — turns any unhandled error into a logged 500 via exception_to_response instead of crashing the worker; full traceback captured below.
            except Exception as exc:
                _logger.exception(
                    "Unhandled error in %s.%s",
                    cls.__name__,
                    self.action
                    if hasattr(self, "action")
                    else "unknown",  # action may not be set if error in permissions
                )
                return exception_to_response(exc)

        view.__name__ = cls.__name__
        view.__qualname__ = cls.__qualname__
        view.view_class = cls
        view.actions = actions or {}
        return view


# ── CRUD Mixins ───────────────────────────────────────────────────────────────


async def _apply_field_permissions(
    view: object,
    request: Request,
    data: SerializedData | list[SerializedData],
    mode: str = "read",
) -> SerializedData | list[SerializedData]:
    """Filter serialized data through field-level RBAC permissions.

    Called by CRUD mixins (ListMixin, RetrieveMixin, CreateMixin, UpdateMixin).
    Uses ``field_permissions_model`` attribute from the view if present.
    No-op when the attribute is unset or empty.
    """
    # dynamic-attr: view is a CRUD-mixin instance; field_permissions_model exists only when the mixin is combined with a base (ViewSet/GenericAPIView) that declares it
    model_name = getattr(view, "field_permissions_model", "")
    if not model_name or request.user is None:
        return data
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.database import get_db

    checker = PermissionChecker(get_db())
    if isinstance(data, list):
        return [
            await checker.filter_fields(request.user, model_name, item, mode)
            for item in data
        ]
    return await checker.filter_fields(request.user, model_name, data, mode)


# ── Native JSON fast-path helpers ──────────────────────────────────────────────


# Stateless renderers (pure render() + class-level media_type/format_suffix)
# reused as one shared instance per class instead of a fresh allocation on
# every _negotiate_renderer / _render_response call. Single dict.get / dict[]=
# are atomic on this free-threaded build and the memo is idempotent (a race
# just discards a redundant equivalent instance).
_RENDERER_SINGLETONS: dict[type, BaseRenderer] = {}


def _renderer_instance(renderer_cls: type) -> BaseRenderer:
    """Return a cached shared instance of a stateless renderer class."""
    inst = _RENDERER_SINGLETONS.get(renderer_cls)
    if inst is None:
        inst = renderer_cls()
        _RENDERER_SINGLETONS[renderer_cls] = inst
    return inst


def _parse_accept_header(accept: str) -> list[tuple[str, float]]:
    """Parse an Accept header into (media_range, q) pairs, highest q first.

    Ties preserve the header's original left-to-right order (stable sort).
    Malformed/absent q-values default to 1.0; blank entries are dropped.
    """
    entries: list[tuple[str, float, int]] = []
    for order, part in enumerate(accept.split(",")):
        segments = part.split(";")
        media_range = segments[0].strip().lower()
        if not media_range:
            continue
        q = 1.0
        for seg in segments[1:]:
            seg = seg.strip()
            if seg[:2] == "q=":
                try:
                    q = float(seg[2:])
                except ValueError:
                    q = 1.0
        entries.append((media_range, q, order))
    entries.sort(key=lambda e: (-e[1], e[2]))
    return [(mr, q) for mr, q, _ in entries]


def _media_range_matches(media_range: str, media_type: str) -> bool:
    """True if an Accept media-range matches a renderer's concrete media type."""
    if media_range == "*/*":
        return True
    if "/" not in media_range or "/" not in media_type:
        return media_range == media_type
    r_type, r_sub = media_range.split("/", 1)
    m_type, m_sub = media_type.split("/", 1)
    if r_sub == "*":
        return r_type == m_type
    return r_type == m_type and r_sub == m_sub


def _negotiate_renderer_impl(
    renderer_classes: tuple[type[BaseRenderer], ...], request: Request
) -> BaseRenderer:
    """Shared renderer negotiation: URL suffix → Accept q-order → 406.

    One module-level routine so the two ViewSet paths can't drift. Honors
    Accept q-values and returns the highest-q supported renderer. When the
    client sends an explicit, unsatisfiable Accept (no wildcard, nothing
    supported) it raises 406 rather than silently defaulting. An absent Accept,
    or one preferring ``*/*``, uses the first (default) renderer.
    """
    # 1) URL format suffix (e.g. /api/posts.csv) wins outright.
    path = request.path
    for renderer_cls in renderer_classes:
        renderer = _renderer_instance(renderer_cls)
        if path.endswith(f".{renderer.format_suffix}"):
            return renderer

    default_renderer = _renderer_instance(renderer_classes[0])

    # 2) Accept header, honoring q-values.
    accept = request.headers.get("accept")
    if not accept:
        return default_renderer
    accepted = _parse_accept_header(accept)
    if not accepted:
        return default_renderer

    for media_range, q in accepted:
        if q <= 0:
            continue  # "q=0" explicitly rejects this type
        if media_range == "*/*":
            return default_renderer
        for renderer_cls in renderer_classes:
            renderer = _renderer_instance(renderer_cls)
            if _media_range_matches(media_range, renderer.media_type):
                return renderer

    # 3) Explicit Accept that nothing supports and no wildcard → 406.
    raise NotAcceptable("No acceptable renderer for the requested media type.")


# Memoized native SELECT column strings. The joined string is a pure function
# of (serializer class, resolved table), both static per view class, so it is
# built once instead of on every native-JSON list request. Keyed by table too
# so a view.model override that changes the table never serves a stale string.
_NATIVE_COLUMNS_SQL_CACHE: dict[tuple[type, str], str] = {}


def _native_columns_sql(view: object) -> str:
    """Build the table-qualified SELECT column list for the native JSON path.

    Emits ``table.col`` (or ``table.col AS output_key`` when the serializer
    field renames a column) in the identity serializer's output-field order,
    so ``db.query_json`` produces JSON byte-identical to ``serializer.data``.
    Columns are always table-qualified so the SELECT stays unambiguous even
    when the queryset carries FK-filter joins.
    """
    serializer_cls = view.get_serializer_class()
    model = view.model if view.model is not None else serializer_cls.Meta.model
    table = model._meta.table
    key = (serializer_cls, table)
    cached = _NATIVE_COLUMNS_SQL_CACHE.get(key)
    if cached is not None:
        return cached
    cols = serializer_cls._native_select_columns
    parts: list[str] = []
    for column, output_key in cols:
        if column == output_key:
            parts.append(f"{table}.{column}")
        else:
            parts.append(f"{table}.{column} AS {output_key}")
    result = ", ".join(parts)
    _NATIVE_COLUMNS_SQL_CACHE[key] = result
    return result


def _native_queryset_ok(qs: Any) -> bool:
    """True when the queryset's shape matches the identity serializer output.

    The native path emits exactly the serializer's columns. Any queryset
    feature that changes the column set or row shape (annotations,
    select/prefetch_related, values(), only()/defer(), DISTINCT, GROUP BY,
    join-related aliases) requires Python post-processing, so we fall back.
    Plain WHERE filters, ordering, and LIMIT/OFFSET are fine — they run
    server-side and don't alter the projected columns.
    """
    return not (
        qs._annotations
        or qs._select_related
        or qs._prefetch_related
        or qs._join_related_aliases
        or qs._values_fields
        or qs._only is not None
        or qs._defer is not None
        or qs._distinct
        or qs._group_by
    )


class ListMixin:
    """Adds list() action — paginated, filtered list of objects."""

    async def list(self, request: Request, **kwargs: Any) -> Response:
        qs = self.filter_queryset(self.get_queryset())

        # Native JSON fast path: skip Python serialization entirely for identity
        # serializers. Builds JSON directly in Zig from the PG wire protocol.
        # Only when the negotiated renderer is JSON (CSV/etc. need the dicts)
        # and the queryset shape matches the serializer's projected columns.
        pag_cls = self.pagination_class
        native_ok = (
            self._can_use_native_json()
            and _native_queryset_ok(qs)
            and isinstance(self._negotiate_renderer(request), JSONRenderer)
        )

        if native_ok and pag_cls is None:
            columns_sql = _native_columns_sql(self)
            sql, params = qs._build_select(columns_override=columns_sql)
            db = get_db()
            json_bytes = await db.query_json(sql, *params)
            response = Response(
                body=json_bytes, status=200, content_type="application/json"
            )
        # dynamic-attr: capability probe — a user-supplied pagination_class need not derive from APIPagination, so native_supported may be absent
        elif native_ok and getattr(pag_cls, "native_supported", False):
            # Paginated native path: LIMIT/OFFSET wired straight into the
            # SELECT, results spliced into the pagination envelope as raw JSON.
            columns_sql = _native_columns_sql(self)
            db = get_db()
            paginator = pag_cls()
            response = await paginator.paginate_native(qs, request, columns_sql, db)
        elif pag_cls is not None:
            paginator = self.pagination_class()
            items = await paginator.paginate_queryset(qs, request)
            serializer = self.get_serializer(obj=items, many=True)
            data = self._encode_response_ids(serializer.data, request=request)
            data = await _apply_field_permissions(self, request, data)
            renderer = self._negotiate_renderer(request)
            if not isinstance(renderer, JSONRenderer):
                paginated_data: dict[str, object] = {"results": data}
                response = self._render_response(request, paginated_data)
            else:
                response = paginator.get_paginated_response(data)
        else:
            items = await qs.all()
            serializer = self.get_serializer(obj=items, many=True)
            data = self._encode_response_ids(serializer.data, request=request)
            data = await _apply_field_permissions(self, request, data)
            response = self._render_response(request, data)

        # Apply HTTP cache headers if CacheableMixin is present
        if isinstance(self, CacheableMixin):
            response = self._apply_cache_headers(response, request, response.body)
        return response


def _normalize_field_errors(
    errors: dict[str, Any],
) -> dict[str, list[Any]]:
    """Normalize serializer errors to the uniform field→list shape.

    Single create/update responses and bulk paths both emit the same
    field→list shape, so every error body parses identically on the client.
    """
    return {k: [v] if isinstance(v, str) else v for k, v in errors.items()}


class CreateMixin:
    """Adds create() action — validate input and create object."""

    async def create(self, request: Request, **kwargs: Any) -> Response:
        body = await self.get_request_data(request)
        body = await _apply_field_permissions(self, request, body, mode="write")
        serializer = self.get_serializer(input_data=body)
        # is_valid() is sync; relational (FK) fields need an async DB existence
        # check, so run both phases and fail on either.
        if not serializer.is_valid() or not await serializer.avalidate_relations():
            raise ValidationError(
                "Validation failed",
                errors=_normalize_field_errors(serializer.errors),
            )
        instance = await self.perform_create(serializer)
        output = self.get_serializer(obj=instance)
        data = self._encode_response_ids(output.data, request=request)
        return Response.json(data, status=201)

    async def perform_create(self, serializer: Serializer) -> Any:
        """Override to customize object creation (e.g., serializer.create(owner=request.user))."""
        return await serializer.create(serializer.validated_data)


class RetrieveMixin:
    """Adds retrieve() action — get single object by lookup field."""

    async def retrieve(self, request: Request, **kwargs: Any) -> Response:
        # Native JSON fast path: build JSON directly in Zig for identity
        # serializers, using the serializer's exact column projection so the
        # output matches serializer.data. Falls back to the Python path (which
        # also runs object-permission checks via get_object()) whenever the
        # serializer/queryset/renderer is not native-eligible.
        response = None
        if self._can_use_native_json() and isinstance(
            self._negotiate_renderer(request), JSONRenderer
        ):
            lookup_kwarg = self.lookup_url_kwarg or self.lookup_field
            lookup_value = self.kwargs.get(lookup_kwarg)
            if lookup_value is None:
                raise NotFound("No lookup value provided")
            # _can_use_native_json() already excluded public-ID strategies, so
            # this decode is an identity mapping to (lookup_field, value).
            # Thread `request` for include_user IDMixin parity with get_object.
            field, value = self._decode_public_id(lookup_value, request=self.request)
            qs = self.get_queryset().filter(**{field: value}).limit(1)
            if _native_queryset_ok(qs):
                columns_sql = _native_columns_sql(self)
                sql, params = qs._build_select(columns_override=columns_sql)
                db = get_db()
                json_bytes = await db.query_json(sql, *params)
                # query_json returns b'[]' for no rows — check and raise NotFound
                if json_bytes == b"[]":
                    raise NotFound("Not found")
                # Unwrap from array: b'[{...}]' -> b'{...}'
                response = Response(
                    body=json_bytes[1:-1],
                    status=200,
                    content_type="application/json",
                )

        if response is None:
            instance = await self.get_object()
            serializer = self.get_serializer(obj=instance)
            data = self._encode_response_ids(serializer.data, request=request)
            data = await _apply_field_permissions(self, request, data)
            response = self._render_response(request, data)

        # Apply HTTP cache headers if CacheableMixin is present
        if isinstance(self, CacheableMixin):
            response = self._apply_cache_headers(response, request, response.body)
        return response


class UpdateMixin:
    """Adds update() and partial_update() actions."""

    async def update(self, request: Request, **kwargs: Any) -> Response:
        instance = await self.get_object()
        partial = self.action == "partial_update"
        body = await self.get_request_data(request)
        body = await _apply_field_permissions(self, request, body, mode="write")
        serializer = self.get_serializer(input_data=body, partial=partial)
        # is_valid() is sync; relational (FK) fields need an async DB existence
        # check, so run both phases and fail on either.
        if not serializer.is_valid() or not await serializer.avalidate_relations():
            raise ValidationError(
                "Validation failed",
                errors=_normalize_field_errors(serializer.errors),
            )
        instance = await self.perform_update(serializer, instance)
        output = self.get_serializer(obj=instance)
        return Response.json(output.data)

    async def perform_update(self, serializer: Serializer, instance: Any) -> Any:
        """Override to customize object update."""
        return await serializer.update(instance, serializer.validated_data)

    async def partial_update(self, request: Request, **kwargs: Any) -> Response:
        """PATCH — delegates to update() with partial=True via self.action check."""
        return await self.update(request, **kwargs)


class DestroyMixin:
    """Adds destroy() action — delete object and return 204."""

    async def destroy(self, request: Request, **kwargs: Any) -> Response:
        instance = await self.get_object()
        await self.perform_destroy(instance)
        return Response(status=204)

    async def perform_destroy(self, instance: Any) -> None:
        """Override to customize object deletion (e.g., soft delete)."""
        await instance.delete()


# ── Bulk CRUD Mixins ─────────────────────────────────────────────────────────

_DEFAULT_MAX_BULK_SIZE = 100


class BulkCreateMixin:
    """Mixin for bulk creation via POST with a list body.

    POST /items/bulk with body: [{"name": "a"}, {"name": "b"}]
    Returns 201 with list of created items, or 400 with per-item errors.
    """

    max_bulk_size: int = _DEFAULT_MAX_BULK_SIZE

    async def bulk_create(self, request: Request, **kwargs: Any) -> Response:
        data = await self.get_request_data(request)
        if not isinstance(data, list):
            raise ValidationError("Expected a list of objects for bulk creation.")
        if len(data) > self.max_bulk_size:
            raise ValidationError(
                f"Bulk size {len(data)} exceeds maximum of {self.max_bulk_size}."
            )

        results: list[dict[str, object]] = []
        errors: dict[int, dict[str, list[str]]] = {}

        for idx, item_data in enumerate(data):
            serializer = self.get_serializer(input_data=item_data)
            # Run BOTH validation phases, matching single create() and
            # bulk_update(): is_valid() is sync, but relational (FK) fields need
            # the async avalidate_relations() to confirm the referenced row
            # exists and to resolve SlugRelatedField slugs → PKs. Skipping it
            # let a raw slug reach a PK column (corruption/500) and turned a bad
            # FK into an unhandled 500 instead of a clean 400.
            if serializer.is_valid() and await serializer.avalidate_relations():
                obj = await self.perform_create(serializer)
                out_serializer = self.get_serializer(obj=obj)
                results.append(out_serializer.data)
            else:
                errors[idx] = _normalize_field_errors(serializer.errors)

        if errors:
            status = 200 if results else 400
            detail = "Some operations failed." if results else "All operations failed."
            return Response.json(
                {"detail": detail, "results": results, "errors": errors}, status=status
            )
        return Response.json(results, status=201)


class BulkUpdateMixin:
    """Mixin for bulk update via PATCH with a list of {id, ...fields}.

    PATCH /items/bulk with body: [{"id": 1, "name": "new"}, {"id": 2, "name": "updated"}]
    """

    max_bulk_size: int = _DEFAULT_MAX_BULK_SIZE

    async def bulk_update(self, request: Request, **kwargs: Any) -> Response:
        data = await self.get_request_data(request)
        if not isinstance(data, list):
            raise ValidationError("Expected a list of objects for bulk update.")
        if len(data) > self.max_bulk_size:
            raise ValidationError(
                f"Bulk size {len(data)} exceeds maximum of {self.max_bulk_size}."
            )

        lookup_field = self.lookup_field
        results: list[dict[str, object]] = []
        # Uniform field→list error shape (same as single create/update), so
        # clients parse every bulk error body identically.
        errors: dict[int, dict[str, list[str]]] = {}

        for idx, item_data in enumerate(data):
            if lookup_field not in item_data:
                errors[idx] = {
                    lookup_field: [f"'{lookup_field}' is required for bulk update."]
                }
                continue
            try:
                qs = self.get_queryset()
                instance = await qs.filter(
                    **{lookup_field: item_data[lookup_field]}
                ).first()
                if instance is None:
                    errors[idx] = {
                        lookup_field: [
                            f"Object with {lookup_field}={item_data[lookup_field]} not found."
                        ]
                    }
                    continue
                serializer = self.get_serializer(input_data=item_data, partial=True)
                if serializer.is_valid() and await serializer.avalidate_relations():
                    obj = await self.perform_update(serializer, instance)
                    out_serializer = self.get_serializer(obj=obj)
                    results.append(out_serializer.data)
                else:
                    errors[idx] = _normalize_field_errors(serializer.errors)
            # blind-except: per-item isolation for a bulk op — one row's failure must not abort the batch; the real error is logged with context below and a generic message returned to avoid leaking internals.
            except Exception:
                # Never stringify the raw exception into the response — it can
                # leak internal state (SQL, paths, constraint names) and goes
                # unlogged. Log server-side with context, return a generic error.
                _logger.exception(
                    "bulk_update: item %d (%s=%r) failed",
                    idx,
                    lookup_field,
                    item_data.get(lookup_field),
                )
                errors[idx] = {"detail": ["Update failed."]}

        if errors:
            status = 200 if results else 400
            detail = "Some operations failed." if results else "All operations failed."
            return Response.json(
                {"detail": detail, "results": results, "errors": errors}, status=status
            )
        return Response.json(results)


class BulkDestroyMixin:
    """Mixin for bulk deletion via DELETE with a list of IDs.

    DELETE /items/bulk with body: [1, 2, 3] or [{"id": 1}, {"id": 2}]
    """

    max_bulk_size: int = _DEFAULT_MAX_BULK_SIZE

    async def bulk_destroy(self, request: Request, **kwargs: Any) -> Response:
        data = await self.get_request_data(request)
        if not isinstance(data, list):
            raise ValidationError("Expected a list of IDs for bulk deletion.")
        if len(data) > self.max_bulk_size:
            raise ValidationError(
                f"Bulk size {len(data)} exceeds maximum of {self.max_bulk_size}."
            )

        lookup_field = self.lookup_field
        deleted_ids: list[object] = []
        # Uniform field→list error shape (same as single create/update).
        errors: dict[int, dict[str, list[str]]] = {}

        for idx, item in enumerate(data):
            # Accept both raw IDs and dicts with lookup_field
            pk = item[lookup_field] if isinstance(item, dict) else item
            try:
                qs = self.get_queryset()
                instance = await qs.filter(**{lookup_field: pk}).first()
                if instance is None:
                    errors[idx] = {
                        lookup_field: [f"Object with {lookup_field}={pk} not found."]
                    }
                    continue
                await self.perform_destroy(instance)
                deleted_ids.append(pk)
            # blind-except: per-item isolation for a bulk op — one row's failure must not abort the batch; the real error is logged with context below and a generic message returned to avoid leaking internals.
            except Exception:
                # Never stringify the raw exception into the response (info leak,
                # unlogged). Log server-side with context, return a generic error.
                _logger.exception(
                    "bulk_destroy: item %d (%s=%r) failed", idx, lookup_field, pk
                )
                errors[idx] = {"detail": ["Delete failed."]}

        if errors:
            status = 200 if deleted_ids else 400
            detail = (
                "Some operations failed." if deleted_ids else "All operations failed."
            )
            return Response.json(
                {"detail": detail, "deleted": deleted_ids, "errors": errors},
                status=status,
            )
        return Response.json({"deleted": deleted_ids}, status=200)


class BulkModelViewSet(
    BulkCreateMixin,
    BulkUpdateMixin,
    BulkDestroyMixin,
    ListMixin,
    CreateMixin,
    RetrieveMixin,
    UpdateMixin,
    DestroyMixin,
    ViewSet,
):
    """Full CRUD + bulk operations ViewSet."""


class ModelViewSet(
    ListMixin, CreateMixin, RetrieveMixin, UpdateMixin, DestroyMixin, ViewSet
):
    """Full CRUD ViewSet for a Model.

    Provides list, create, retrieve, update, partial_update, and destroy actions.
    """


class ReadOnlyModelViewSet(ListMixin, RetrieveMixin, ViewSet):
    """Read-only ViewSet — list and retrieve only."""


# ── GenericAPIView + Shortcut Views ──────────────────────────────────────────


class GenericAPIView(View):
    """Simplified API view that dispatches by HTTP method (get/post/put/patch/delete).

    Unlike ViewSet, does NOT require action mapping — just define handler methods
    matching HTTP method names. Includes all ViewSet capabilities: serializer,
    queryset, permissions, authentication, throttling, pagination, filtering.

    Usage:
        class BookList(ListAPIView):
            serializer_class = BookSerializer
            model = Book
            pagination_class = PageNumberPagination
    """

    serializer_class: type[Serializer] | None = None
    queryset: Any = None
    model: type | None = None
    lookup_field: str = "id"
    lookup_url_kwarg: str | None = None

    # Pluggable behavior (tuples to prevent accidental shared-state mutation)
    permission_classes: tuple[type[BasePermission], ...] = (AllowAny,)
    authentication_classes: tuple[type[BaseAuthentication], ...] = ()
    throttle_classes: tuple[type[BaseThrottle], ...] = ()
    pagination_class: type[APIPagination] | None = None
    filter_backends: tuple[type[FilterBackend], ...] = ()
    parser_classes: tuple[type[BaseParser], ...] = (
        JSONParser,
        FormParser,
        MultiPartParser,
    )
    renderer_classes: tuple[type[BaseRenderer], ...] = (JSONRenderer,)
    metadata_class: type[SimpleMetadata] | None = SimpleMetadata

    # Filter/search/ordering config (tuples for immutable class-level defaults)
    filterset_fields: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    ordering_fields: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()

    # Native JSON fast path (same as ViewSet — shared mixins need this).
    # Auto-enabled for identity serializers; set False to force Python path.
    use_native_json: bool = True

    # Field-level RBAC (parity with ViewSet — checked by _can_use_native_json)
    field_permissions_model: str = ""

    # Set during dispatch
    action: str = ""

    def get_queryset(self) -> Any:
        """Return the queryset for this view."""
        if self.queryset is not None:
            return self.queryset
        if self.model is not None:
            return self.model.objects
        raise ValueError("GenericAPIView requires 'queryset' or 'model'")

    def get_serializer_class(self) -> type[Serializer]:
        """Return the serializer class. Override for per-method serializers."""
        if self.serializer_class is None:
            raise ValueError("GenericAPIView requires 'serializer_class'")
        return self.serializer_class

    def get_serializer(self, **kwargs: Any) -> Serializer:
        """Instantiate and return a serializer with context."""
        ser_cls = self.get_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return ser_cls(**kwargs)

    def get_serializer_context(self) -> dict[str, object]:
        """Return extra context for serializer."""
        return {"request": self.request, "view": self}

    def _can_use_native_json(self) -> bool:
        """Check if native JSON fast path is available for this view.

        Mirrors ViewSet._can_use_native_json: identity serializer with a
        native SELECT plan, no external ID encoding, no field-level RBAC.
        """
        if not self.use_native_json:
            return False
        serializer_cls = self.get_serializer_class()
        if not isinstance(serializer_cls, ModelSerializerMeta):
            return False
        if serializer_cls._native_select_columns is None:
            return False
        if self._get_public_id_strategy() is not None:
            return False
        if self.field_permissions_model:
            return False
        # Object-level permissions (has_object_permission) are enforced ONLY
        # inside get_object() → check_object_permissions(), which the native
        # fast path skips. If any permission class defines its own
        # has_object_permission, taking the native path would leak objects the
        # caller is not allowed to see (IDOR). Force the slow path so the
        # object-permission check runs.
        for perm_cls in self.permission_classes:
            if (
                perm_cls.has_object_permission
                is not BasePermission.has_object_permission
            ):
                return False
        return True

    def _get_id_manager(self):
        """Get IDManager for this view's model, if it uses IDMixin."""
        model = self.model
        if model is None:
            return None
        return model.__dict__.get("_id_manager")

    def _has_public_id(self) -> bool:
        """Check if this view's model uses PublicIDMixin or IDMixin."""
        model = self.model
        if model is None:
            return False
        if self._get_id_manager() is not None:
            return True
        return isinstance(model, type) and issubclass(model, PublicIDMixin)

    def _get_public_id_strategy(self) -> str | None:
        """Get the ID strategy for this view's model, or None."""
        mgr = self._get_id_manager()
        if mgr is not None:
            return mgr.config.mode
        if not self._has_public_id():
            return None
        return self.model._public_id_strategy

    def _make_pk_encoder(self, request: object = None):
        """Resolve the PK→external-ID encoder ONCE and return ``encode_one(pk)``.

        Hoists the per-row manager/mode/encoder/width resolution out of the list
        loop; output is byte-identical to ``_encode_pk(pk, request=request)``.
        """
        mgr = self._get_id_manager()
        if mgr is not None:
            mode = mgr.config.mode
            if mode in _SIGNED_OR_ENCODED_MODES:
                user_id = None
                if mgr.config.include_user and request is not None:
                    user = request.user
                    if user is not None:
                        user_id = user.id
                return lambda pk: mgr.encode(pk, user_id=user_id)
            return lambda pk: pk
        strategy = self._get_public_id_strategy()
        if strategy == IDStrategy.ENCODED_PK:
            encoder = self.model._public_id_encoder
            width = self.model._public_id_width
            if width > 0:
                return lambda pk: encoder.encode_padded(pk, width)
            return lambda pk: encoder.encode(pk)
        return lambda pk: pk

    def _encode_pk(self, pk: int, *, request: object = None) -> str | int:
        """Encode a PK to an external ID string."""
        return self._make_pk_encoder(request)(pk)

    def _decode_public_id(
        self, external_id: str, *, request: object = None
    ) -> tuple[str, object]:
        """Decode an external ID to a lookup field and value."""
        mgr = self._get_id_manager()
        if mgr is not None:
            mode = mgr.config.mode
            if mode in _SIGNED_ENCODED_RAW_MODES:
                try:
                    user_id = None
                    if mgr.config.include_user and request is not None:
                        user = request.user
                        if user is not None:
                            user_id = user.id
                    pk = mgr.decode(external_id, user_id=user_id)
                except ValueError, KeyError:
                    raise NotFound("Not found.")
                return ("id", pk)
            return ("public_id", external_id)
        strategy = self._get_public_id_strategy()
        if strategy == IDStrategy.ENCODED_PK:
            try:
                pk = self.model._public_id_encoder.decode(external_id)
            except ValueError, KeyError:
                raise NotFound("Not found")
            return ("id", pk)
        if strategy in _RANDOM_STRATEGIES:
            return ("public_id", external_id)
        return (self.lookup_field, external_id)

    def _encode_response_ids(self, data: object, *, request: object = None) -> object:
        """Encode IDs in serialized response data for external output."""
        strategy = self._get_public_id_strategy()
        if strategy is None:
            return data
        if isinstance(data, list):
            # Resolve the encoder ONCE for the whole list, not once per row.
            encode_one = self._make_pk_encoder(request)
            return [
                self._encode_single_item_id(item, strategy, encode_one) for item in data
            ]
        if isinstance(data, dict):
            encode_one = self._make_pk_encoder(request)
            return self._encode_single_item_id(data, strategy, encode_one)
        return data

    def _encode_single_item_id(
        self, item: dict[str, object], strategy: str, encode_one
    ) -> dict[str, object]:
        """Encode the ID in a single serialized item dict.

        Mutates ``item`` in place: it is a fresh per-request serializer dict,
        owned by this response and consumed once, so no defensive copy is needed.
        """
        if not isinstance(item, dict):
            return item
        if strategy in _ENCODE_PK_STRATEGIES:
            pk = item.get("id")
            if pk is not None and isinstance(pk, int):
                item["id"] = encode_one(pk)
        elif strategy in _RANDOM_STRATEGIES:
            public_id = item.get("public_id")
            if public_id is not None:
                item["id"] = public_id
        return item

    def _negotiate_renderer(self, request: Request) -> BaseRenderer:
        """Select renderer by URL format suffix or Accept header (q-aware).

        Delegates to the shared ``_negotiate_renderer_impl`` (same routine the
        other ViewSet path uses) to prevent the two from drifting.
        """
        return _negotiate_renderer_impl(self.renderer_classes, request)

    def _render_response(
        self, request: Request, data: object, status: int = 200
    ) -> Response:
        """Render data through the negotiated renderer."""
        renderer = self._negotiate_renderer(request)
        body = renderer.render(data)
        content_type = renderer.media_type
        headers: dict[str, str] = {}
        if isinstance(renderer, CSVRenderer):
            headers["Content-Disposition"] = 'attachment; filename="export.csv"'
        return Response(
            body=body, status=status, headers=headers, content_type=content_type
        )

    async def get_object(self) -> Any:
        """Retrieve a single object by lookup field. Raises NotFound.

        If the model uses PublicIDMixin, the URL parameter is treated as a
        public ID and decoded appropriately.
        """
        qs = self.get_queryset()
        lookup_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs.get(lookup_kwarg)
        if lookup_value is None:
            raise NotFound("No lookup value provided")

        # Decode public ID to internal lookup field + value. Thread `request`
        # for include_user IDMixin binding parity (same fix as ViewSet).
        field, value = self._decode_public_id(lookup_value, request=self.request)

        # `.get()` raises the model's OWN DoesNotExist on a missing row (each
        # model — and the test doubles — declares its own). Resolve it from the
        # QUERYSET's model, not self.model, which is None/absent on generic views.
        # A genuine "no such row" is a clean 404; a DB/ORM failure still surfaces
        # as a 500 below.
        # get_queryset() is a user override point — it may return a real QuerySet
        # (always has _model) or an arbitrary/duck-typed object, so _model is
        # genuinely optional here; we fall back to the base exception.
        # dynamic-attr: qs is duck-typed at the get_queryset() override boundary
        qs_model = getattr(qs, "_model", None)
        not_found_exc = (
            qs_model.DoesNotExist if qs_model is not None else Model.DoesNotExist
        )
        try:
            obj = await qs.get(**{field: value})
        except not_found_exc:
            # The row genuinely isn't there — a real 404.
            raise NotFound("Not found")
        except Exception:
            # A DB outage, ORM bug, or MultipleObjectsReturned is NOT a 404.
            # Silently mapping it to NotFound hides infra failures with zero
            # telemetry — log with context and let it surface as a 500.
            _logger.exception(
                "get_object: unexpected error looking up object by %s=%r", field, value
            )
            raise
        await self.check_object_permissions(self.request, obj)
        return obj

    def filter_queryset(self, queryset: Any) -> Any:
        """Apply all filter backends to the queryset."""
        for backend_cls in self.filter_backends:
            backend = backend_cls()
            queryset = backend.filter_queryset(self.request, queryset, self)
        return queryset

    async def perform_authentication(self, request: Request) -> None:
        """Run authentication classes to populate request.user."""
        for auth_cls in self.authentication_classes:
            auth = auth_cls()
            result = await auth.authenticate(request)
            if result is not None:
                request.user = result.user
                request.auth = result.auth_info
                return

    async def check_permissions(self, request: Request) -> None:
        """Check all permission classes. Raises on failure."""
        for perm_cls in self.permission_classes:
            perm = perm_cls()
            if not await perm.has_permission(request, self):
                # 401 (not 403) when the failure is a missing/anonymous identity:
                # request.user is None OR an unauthenticated AnonymousUser.
                if isinstance(perm, IsAuthenticated) and (
                    request.user is None or not request.user.is_authenticated
                ):
                    raise AuthenticationFailed("Authentication required")
                raise PermissionDenied("Permission denied")

    async def check_object_permissions(self, request: Request, obj: Any) -> None:
        """Check object-level permissions. Raises on failure."""
        for perm_cls in self.permission_classes:
            perm = perm_cls()
            if not await perm.has_object_permission(request, self, obj):
                raise PermissionDenied("Permission denied")

    async def check_throttles(self, request: Request) -> None:
        """Check all throttle classes. Raises Throttled on limit exceeded."""
        for throttle_cls in self.throttle_classes:
            throttle = throttle_cls()
            if not await throttle.allow_request(request, self):
                wait = throttle.get_wait()
                detail = "Request was throttled"
                headers = None
                if wait is not None:
                    detail = f"Request was throttled. Retry after {wait} seconds"
                    # Retry-After on every 429 producer (F3): machine-readable
                    # back-off matching ratelimit.build_429_response and the guard.
                    headers = {"Retry-After": str(wait)}
                raise Throttled(detail, headers=headers)

    async def get_request_data(self, request: Request) -> Any:
        """Parse request body using the configured parser classes."""
        return await parse_request_body(request, self.parser_classes)

    @classmethod
    def as_view(cls, **initkwargs: Any) -> Any:
        """Return an async handler that dispatches by HTTP method name.

        No action mapping needed — just define get(), post(), put(), patch(), delete().
        """
        # Pre-resolve which HTTP methods this class supports
        _http_methods = ("get", "post", "put", "patch", "delete", "head", "options")
        _resolved: dict[str, tuple[Any, bool]] = {}
        for method_name in _http_methods:
            for klass in cls.__mro__:
                if method_name in klass.__dict__:
                    handler = klass.__dict__[method_name]
                    _resolved[method_name] = (
                        handler,
                        inspect.iscoroutinefunction(handler),
                    )
                    break

        async def view(request: Request, **kwargs: Any) -> Response:
            self = cls(**initkwargs)
            self.request = request
            self.kwargs = kwargs
            # Set _action_map for SimpleMetadata compatibility
            self._action_map = {m: m for m in _resolved}

            try:
                # Authentication (populate request.user before perm checks)
                await self.perform_authentication(request)

                # Permissions
                await self.check_permissions(request)

                # Throttling (after auth so we know the user)
                await self.check_throttles(request)

                method = request.method.lower()

                # Handle OPTIONS -> return metadata
                if method == "options" and self.metadata_class is not None:
                    metadata = self.metadata_class().determine_metadata(request, self)
                    return Response.json(metadata)

                # HEAD falls back to GET
                if method == "head" and "head" not in _resolved and "get" in _resolved:
                    method = "get"

                resolved = _resolved.get(method)
                if resolved is None:
                    raise MethodNotAllowed(f"Method {request.method} not allowed")
                handler, is_async = resolved
                # Map PATCH to "partial_update" so UpdateMixin detects partial mode
                self.action = "partial_update" if method == "patch" else method

                if is_async:
                    response = await handler(self, request, **kwargs)
                else:
                    response = handler(self, request, **kwargs)

                return response

            except HTTPException as exc:
                # APIException now subclasses HTTPException, so this one clause
                # covers both a REST APIException and the framework HTTPException
                # (e.g. a guard raising through). The single mapper emits the
                # unified {"detail","status"} body (+ errors/headers) — identical
                # to the plain-handler and middleware paths.
                return exception_to_response(exc)
            # blind-except: last-resort request boundary — turns any unhandled error into a logged 500 via exception_to_response instead of crashing the worker; full traceback captured below.
            except Exception as exc:
                _logger.exception(
                    "Unhandled error in %s.%s",
                    cls.__name__,
                    self.action or "unknown",
                )
                return exception_to_response(exc)

        view.__name__ = cls.__name__
        view.__qualname__ = cls.__qualname__
        view.view_class = cls
        return view


class CreateAPIView(CreateMixin, GenericAPIView):
    """Concrete view for create-only endpoints. POST -> create()."""

    async def post(self, request: Request, **kwargs: Any) -> Response:
        return await self.create(request, **kwargs)


class ListAPIView(ListMixin, GenericAPIView):
    """Concrete view for list-only endpoints. GET -> list()."""

    async def get(self, request: Request, **kwargs: Any) -> Response:
        return await self.list(request, **kwargs)


class RetrieveAPIView(RetrieveMixin, GenericAPIView):
    """Concrete view for retrieve-only endpoints. GET -> retrieve()."""

    async def get(self, request: Request, **kwargs: Any) -> Response:
        return await self.retrieve(request, **kwargs)


class DestroyAPIView(DestroyMixin, GenericAPIView):
    """Concrete view for destroy-only endpoints. DELETE -> destroy()."""

    async def delete(self, request: Request, **kwargs: Any) -> Response:
        return await self.destroy(request, **kwargs)


class UpdateAPIView(UpdateMixin, GenericAPIView):
    """Concrete view for update-only endpoints. PUT/PATCH."""

    async def put(self, request: Request, **kwargs: Any) -> Response:
        return await self.update(request, **kwargs)

    async def patch(self, request: Request, **kwargs: Any) -> Response:
        return await self.partial_update(request, **kwargs)


class ListCreateAPIView(ListMixin, CreateMixin, GenericAPIView):
    """Concrete view for list + create endpoints. GET/POST."""

    async def get(self, request: Request, **kwargs: Any) -> Response:
        return await self.list(request, **kwargs)

    async def post(self, request: Request, **kwargs: Any) -> Response:
        return await self.create(request, **kwargs)


class RetrieveUpdateAPIView(RetrieveMixin, UpdateMixin, GenericAPIView):
    """Concrete view for retrieve + update endpoints. GET/PUT/PATCH."""

    async def get(self, request: Request, **kwargs: Any) -> Response:
        return await self.retrieve(request, **kwargs)

    async def put(self, request: Request, **kwargs: Any) -> Response:
        return await self.update(request, **kwargs)

    async def patch(self, request: Request, **kwargs: Any) -> Response:
        return await self.partial_update(request, **kwargs)


class RetrieveUpdateDestroyAPIView(
    RetrieveMixin, UpdateMixin, DestroyMixin, GenericAPIView
):
    """Concrete view for retrieve + update + destroy. GET/PUT/PATCH/DELETE."""

    async def get(self, request: Request, **kwargs: Any) -> Response:
        return await self.retrieve(request, **kwargs)

    async def put(self, request: Request, **kwargs: Any) -> Response:
        return await self.update(request, **kwargs)

    async def patch(self, request: Request, **kwargs: Any) -> Response:
        return await self.partial_update(request, **kwargs)

    async def delete(self, request: Request, **kwargs: Any) -> Response:
        return await self.destroy(request, **kwargs)


class RetrieveDestroyAPIView(RetrieveMixin, DestroyMixin, GenericAPIView):
    """Concrete view for retrieve + destroy endpoints. GET/DELETE."""

    async def get(self, request: Request, **kwargs: Any) -> Response:
        return await self.retrieve(request, **kwargs)

    async def delete(self, request: Request, **kwargs: Any) -> Response:
        return await self.destroy(request, **kwargs)


# ── APIRouter ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ViewSetRegistration:
    """A registered ViewSet with its URL prefix and basename."""

    prefix: str
    viewset_class: type[ViewSet]
    basename: str


class APIRouter:
    """Auto-registers ViewSets into URL patterns.

    Usage:
        router = APIRouter(prefix="/api/v1")
        router.register("posts", PostViewSet)
        router.register("users", UserViewSet, basename="user")
        router.mount(app.router, namespace="api")
    """

    def __init__(self, prefix: str = ""):
        self._prefix = prefix.rstrip("/")
        self._registrations: list[ViewSetRegistration] = []

    def register(
        self,
        prefix: str,
        viewset_class: type[ViewSet],
        basename: str | None = None,
    ) -> None:
        """Register a ViewSet with a URL prefix.

        Args:
            prefix: URL prefix without slashes (e.g., "posts", "users")
            viewset_class: The ViewSet class to register
            basename: Base name for route names (defaults to prefix, singularized)
        """
        if basename is None:
            basename = self._get_default_basename(viewset_class, prefix)
        self._registrations.append(
            ViewSetRegistration(
                prefix=prefix.strip("/"),
                viewset_class=viewset_class,
                basename=basename,
            )
        )

    def get_urls(self) -> list[tuple[str, str, Any, str]]:
        """Generate (method, pattern, handler, name) tuples for all registered ViewSets.

        Includes an API root view at "/" that lists all registered endpoints.
        """
        urls: list[tuple[str, str, Any, str]] = []

        # Add API root view
        root_handler = self._make_root_view()
        urls.append(("GET", "/", root_handler, "api-root"))

        for reg in self._registrations:
            urls.extend(self._get_urls_for_viewset(reg))
        return urls

    def mount(self, router: Router, namespace: str | None = None) -> None:
        """Mount all registered ViewSets onto a Router instance.

        Args:
            router: The Router to register URL patterns on
            namespace: Optional namespace prefix for route names
        """
        urls = self.get_urls()
        router.include(self._prefix, urls, namespace=namespace)

    def _make_root_view(self) -> Any:
        """Create an API root view that lists all registered endpoints."""
        prefix = self._prefix
        registrations = self._registrations

        async def api_root(request: Request, **kwargs: Any) -> Response:
            endpoints: dict[str, str] = {}
            for reg in registrations:
                url = f"{prefix}/{reg.prefix}"
                endpoints[reg.basename] = url
            return Response.json(endpoints)

        api_root.__name__ = "api_root"
        return api_root

    def _get_urls_for_viewset(
        self, reg: ViewSetRegistration
    ) -> list[tuple[str, str, Any, str]]:
        """Generate URL patterns for a single ViewSet registration."""
        vs = reg.viewset_class
        urls: list[tuple[str, str, Any, str]] = []

        # List route: GET → list, POST → create
        list_actions: dict[str, str] = {}
        if _has_method(vs, "list"):
            list_actions["get"] = "list"
        if _has_method(vs, "create"):
            list_actions["post"] = "create"

        if list_actions:
            handler = vs.as_view(actions=list_actions)
            pattern = f"/{reg.prefix}"
            for method in list_actions:
                name = f"{reg.basename}-{list_actions[method]}"
                urls.append((method.upper(), pattern, handler, name))

        # Detail route: GET → retrieve, PUT → update, PATCH → partial_update, DELETE → destroy
        detail_actions: dict[str, str] = {}
        if _has_method(vs, "retrieve"):
            detail_actions["get"] = "retrieve"
        if _has_method(vs, "update"):
            detail_actions["put"] = "update"
        if _has_method(vs, "partial_update"):
            detail_actions["patch"] = "partial_update"
        if _has_method(vs, "destroy"):
            detail_actions["delete"] = "destroy"

        lookup = vs.lookup_field
        lookup_type = _get_lookup_type(vs)

        if detail_actions:
            handler = vs.as_view(actions=detail_actions)
            pattern = f"/{reg.prefix}/{{{lookup}:{lookup_type}}}"
            for method in detail_actions:
                name = f"{reg.basename}-{detail_actions[method]}"
                urls.append((method.upper(), pattern, handler, name))

        # Custom @action routes
        for attr_name in list(vs.__dict__.keys()):
            attr = vs.__dict__[attr_name]
            if not callable(attr):
                continue
            if not _is_action_method(attr):
                continue

            action_detail = attr._action_detail
            action_url_path = attr._action_url_path
            action_url_name = attr._action_url_name
            action_methods = attr._action_methods

            action_map: dict[str, str] = {}
            for method in action_methods:
                action_map[method.lower()] = attr_name

            handler = vs.as_view(actions=action_map)
            if action_detail:
                pattern = f"/{reg.prefix}/{{{lookup}:{lookup_type}}}/{action_url_path}"
            else:
                pattern = f"/{reg.prefix}/{action_url_path}"

            for method in action_methods:
                name = f"{reg.basename}-{action_url_name}"
                urls.append((method, pattern, handler, name))

        # Bulk operation routes
        bulk_actions: dict[str, str] = {}
        if _has_method(vs, "bulk_create"):
            bulk_actions["post"] = "bulk_create"
        if _has_method(vs, "bulk_update"):
            bulk_actions["patch"] = "bulk_update"
        if _has_method(vs, "bulk_destroy"):
            bulk_actions["delete"] = "bulk_destroy"

        if bulk_actions:
            handler = vs.as_view(actions=bulk_actions)
            pattern = f"/{reg.prefix}/bulk"
            for method in bulk_actions:
                name = f"{reg.basename}-{bulk_actions[method]}"
                urls.append((method.upper(), pattern, handler, name))

        return urls

    # ── OpenAPI 3.1 schema generation ─────────────────────────────────────────

    def get_schema(
        self,
        title: str = "API",
        version: str = "1.0.0",
        description: str = "",
    ) -> dict[str, object]:
        """Generate OpenAPI 3.1 schema from all registered ViewSets.

        Inspects each registered ViewSet's serializer, actions, filters,
        search/ordering fields, pagination, and custom @action methods to
        produce a complete OpenAPI 3.1 specification.

        Args:
            title: API title for the info block.
            version: API version string.
            description: API description.

        Returns:
            OpenAPI 3.1 spec as a JSON-serializable dict.
        """
        schemas: dict[str, dict[str, object]] = {}
        spec: dict[str, object] = {
            "openapi": "3.1.0",
            "info": {"title": title, "version": version, "description": description},
            "paths": {},
            "components": {"schemas": schemas},
        }

        for reg in self._registrations:
            self._add_viewset_paths(spec, reg, schemas)

        return spec

    def _add_viewset_paths(
        self,
        spec: dict[str, object],
        reg: ViewSetRegistration,
        schemas: dict[str, dict[str, object]],
    ) -> None:
        """Add paths for a registered ViewSet to the spec."""
        vs_cls = reg.viewset_class
        prefix = f"{self._prefix}/{reg.prefix}".rstrip("/")
        basename = reg.basename

        # Add model schema to components
        serializer_cls = vs_cls.serializer_class
        schema_name = ""
        if serializer_cls is not None:
            schema_name = serializer_cls.__name__.replace("Serializer", "")
            if schema_name not in schemas:
                schemas[schema_name] = serializer_to_schema(
                    serializer_cls, mode="output", schemas=schemas
                )
            # Also generate input schema
            input_name = f"{schema_name}Input"
            if input_name not in schemas:
                schemas[input_name] = serializer_to_schema(
                    serializer_cls, mode="input", schemas=schemas
                )

        paths = spec["paths"]

        # List + Create endpoint
        list_path = f"{prefix}/"
        if list_path not in paths:
            paths[list_path] = {}

        if _has_method(vs_cls, "list"):
            paths[list_path]["get"] = self._make_list_op(vs_cls, basename, schema_name)
        if _has_method(vs_cls, "create"):
            paths[list_path]["post"] = self._make_create_op(
                vs_cls, basename, schema_name
            )

        # Detail endpoint
        lookup = vs_cls.lookup_field
        detail_path = f"{prefix}/{{{lookup}}}"
        if detail_path not in paths:
            paths[detail_path] = {}

        if _has_method(vs_cls, "retrieve"):
            paths[detail_path]["get"] = self._make_retrieve_op(basename, schema_name)
        if _has_method(vs_cls, "update"):
            paths[detail_path]["put"] = self._make_update_op(basename, schema_name)
            paths[detail_path]["patch"] = self._make_partial_update_op(
                basename, schema_name
            )
        if _has_method(vs_cls, "destroy"):
            paths[detail_path]["delete"] = self._make_destroy_op(basename)

        # Custom @action routes
        for attr_name in list(vs_cls.__dict__.keys()):
            attr = vs_cls.__dict__[attr_name]
            if not callable(attr):
                continue
            if not _is_action_method(attr):
                continue

            action_detail = attr._action_detail
            action_url_path = attr._action_url_path
            action_methods = attr._action_methods

            if action_detail:
                full_path = f"{detail_path}/{action_url_path}"
            else:
                full_path = f"{prefix}/{action_url_path}"

            if full_path not in paths:
                paths[full_path] = {}

            for method in action_methods:
                op = self._make_action_op(attr_name, method, action_detail, basename)
                paths[full_path][method.lower()] = op

    def _make_list_op(
        self,
        vs_cls: type[ViewSet],
        basename: str,
        schema_name: str,
    ) -> dict[str, object]:
        """Generate GET list operation."""
        response_schema: dict[str, object] = {"type": "array"}
        if schema_name:
            response_schema["items"] = {"$ref": f"#/components/schemas/{schema_name}"}

        op: dict[str, object] = {
            "operationId": f"{basename}_list",
            "summary": f"List {basename}",
            "tags": [basename],
            "parameters": [],
            "responses": {
                "200": {
                    "description": "Success",
                    "content": {"application/json": {"schema": response_schema}},
                },
            },
        }

        params = op["parameters"]

        # Filter parameters
        if vs_cls.filterset_fields:
            for field_name in vs_cls.filterset_fields:
                params.append(
                    {
                        "name": field_name,
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                )

        # Search parameter
        if vs_cls.search_fields:
            params.append(
                {
                    "name": "search",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                }
            )

        # Ordering parameter
        if vs_cls.ordering_fields:
            params.append(
                {
                    "name": "ordering",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                }
            )

        # Pagination parameters
        if vs_cls.pagination_class is not None:
            params.append(
                {
                    "name": "page",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer"},
                }
            )
            params.append(
                {
                    "name": "page_size",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer"},
                }
            )

        return op

    def _make_create_op(
        self,
        vs_cls: type[ViewSet],
        basename: str,
        schema_name: str,
    ) -> dict[str, object]:
        """Generate POST create operation."""
        op: dict[str, object] = {
            "operationId": f"{basename}_create",
            "summary": f"Create {basename}",
            "tags": [basename],
            "responses": {
                "201": {
                    "description": "Created",
                },
            },
        }

        if schema_name:
            op["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema_name}Input"},
                    },
                },
            }
            op["responses"]["201"]["content"] = {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                },
            }

        return op

    def _make_retrieve_op(
        self,
        basename: str,
        schema_name: str,
    ) -> dict[str, object]:
        """Generate GET retrieve operation."""
        op: dict[str, object] = {
            "operationId": f"{basename}_retrieve",
            "summary": f"Retrieve {basename}",
            "tags": [basename],
            "responses": {
                "200": {
                    "description": "Success",
                },
                "404": {"description": "Not found"},
            },
        }

        if schema_name:
            op["responses"]["200"]["content"] = {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                },
            }

        return op

    def _make_update_op(
        self,
        basename: str,
        schema_name: str,
    ) -> dict[str, object]:
        """Generate PUT update operation."""
        op: dict[str, object] = {
            "operationId": f"{basename}_update",
            "summary": f"Update {basename}",
            "tags": [basename],
            "responses": {
                "200": {"description": "Success"},
                "404": {"description": "Not found"},
            },
        }

        if schema_name:
            op["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema_name}Input"},
                    },
                },
            }
            op["responses"]["200"]["content"] = {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                },
            }

        return op

    def _make_partial_update_op(
        self,
        basename: str,
        schema_name: str,
    ) -> dict[str, object]:
        """Generate PATCH partial_update operation."""
        op: dict[str, object] = {
            "operationId": f"{basename}_partial_update",
            "summary": f"Partial update {basename}",
            "tags": [basename],
            "responses": {
                "200": {"description": "Success"},
                "404": {"description": "Not found"},
            },
        }

        if schema_name:
            op["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema_name}Input"},
                    },
                },
            }
            op["responses"]["200"]["content"] = {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                },
            }

        return op

    def _make_destroy_op(self, basename: str) -> dict[str, object]:
        """Generate DELETE destroy operation."""
        return {
            "operationId": f"{basename}_destroy",
            "summary": f"Delete {basename}",
            "tags": [basename],
            "responses": {
                "204": {"description": "Deleted"},
                "404": {"description": "Not found"},
            },
        }

    def _make_action_op(
        self,
        attr_name: str,
        method: str,
        detail: bool,
        basename: str,
    ) -> dict[str, object]:
        """Generate operation for a custom @action endpoint."""
        op_id = f"{basename}_{attr_name}"
        summary = attr_name.replace("_", " ").title()

        op: dict[str, object] = {
            "operationId": op_id,
            "summary": summary,
            "tags": [basename],
            "responses": {
                "200": {"description": "Success"},
            },
        }

        if method.upper() in {"POST", "PUT", "PATCH"}:
            op["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {"schema": {"type": "object"}},
                },
            }

        return op

    def _get_default_basename(self, viewset_class: type[ViewSet], prefix: str) -> str:
        """Derive a basename from the model or prefix."""
        if viewset_class.model is not None:
            return _singularize(viewset_class.model._meta.table)
        return prefix.strip("/").replace("/", "-")


def _has_method(cls: type, name: str) -> bool:
    """Check if a class or its MRO defines a method."""
    for klass in cls.__mro__:
        if name in klass.__dict__:
            return True
    return False


def _is_action_method(func: Any) -> bool:
    """Check if a function was decorated with @action."""
    return callable(func) and func.__dict__.get("_is_action", False) is True


def _get_lookup_type(viewset_class: type[ViewSet]) -> str:
    """Determine the URL param type for the lookup field."""
    if viewset_class.model is not None:
        lookup = viewset_class.lookup_field
        annotations = viewset_class.model.__annotations__
        field_type = annotations.get(lookup, int)
        if field_type is int:
            return "int"
        if field_type is str:
            return "str"
    return "int"


# ── Nested Router ────────────────────────────────────────────────────────────


class NestedViewSetMixin:
    """Mixin that auto-filters queryset by parent resource FK.

    Set parent_lookup_field to the model FK field name.
    The URL param name comes from the NestedRouter's lookup parameter.

    Usage:
        class CommentViewSet(NestedViewSetMixin, ModelViewSet):
            parent_lookup_field = "post_id"
            model = Comment
    """

    parent_lookup_field: str = ""  # e.g., "post_id"

    def get_queryset(self) -> Any:
        qs = super().get_queryset()
        if self.parent_lookup_field and self._parent_lookup:
            parent_value = self.kwargs.get(self._parent_lookup)
            if parent_value is not None:
                qs = qs.filter(**{self.parent_lookup_field: parent_value})
        return qs


@dataclass(slots=True)
class NestedRouter:
    """Router for nested sub-resources.

    Usage:
        router = APIRouter(prefix="/api/v1")
        router.register("posts", PostViewSet)

        comments_router = NestedRouter(router, "posts", lookup="post_id")
        comments_router.register("comments", CommentViewSet)

        # Generates:
        # GET    /api/v1/posts/{post_id}/comments/          -> list
        # POST   /api/v1/posts/{post_id}/comments/          -> create
        # GET    /api/v1/posts/{post_id}/comments/{id:int}   -> retrieve
        # PUT    /api/v1/posts/{post_id}/comments/{id:int}   -> update
        # PATCH  /api/v1/posts/{post_id}/comments/{id:int}   -> partial_update
        # DELETE /api/v1/posts/{post_id}/comments/{id:int}   -> destroy
    """

    parent_router: APIRouter
    parent_prefix: str
    lookup: str  # e.g., "post_id" -- the URL param name for the parent
    lookup_type: str = "int"  # type hint for URL param
    _registrations: list[ViewSetRegistration] = field(default_factory=list)

    def register(
        self,
        prefix: str,
        viewset_class: type[ViewSet],
        basename: str | None = None,
    ) -> None:
        """Register a child ViewSet under the parent prefix."""
        if basename is None:
            basename = self.parent_router._get_default_basename(viewset_class, prefix)
        self._registrations.append(
            ViewSetRegistration(
                prefix=prefix,
                viewset_class=viewset_class,
                basename=basename,
            )
        )

    def get_urls(self) -> list[tuple[str, str, Any, str]]:
        """Generate nested URL patterns.

        Returns list of (method, pattern, handler, name) tuples.
        """
        urls: list[tuple[str, str, Any, str]] = []

        for reg in self._registrations:
            nested_prefix = (
                f"/{self.parent_prefix}"
                f"/{{{self.lookup}:{self.lookup_type}}}"
                f"/{reg.prefix}"
            )
            basename = f"{self.parent_prefix}-{reg.basename}"

            # List + Create (collection)
            vs_cls = reg.viewset_class
            list_actions: dict[str, str] = {}
            if _has_method(vs_cls, "list"):
                list_actions["get"] = "list"
            if _has_method(vs_cls, "create"):
                list_actions["post"] = "create"

            if list_actions:
                handler = vs_cls.as_view(
                    actions=list_actions, parent_lookup=self.lookup
                )
                for method in list_actions:
                    urls.append(
                        (
                            method.upper(),
                            nested_prefix,
                            handler,
                            f"{basename}-list",
                        )
                    )

            # Detail routes (retrieve, update, partial_update, destroy)
            child_lookup = vs_cls.lookup_field or "id"
            child_lookup_type = _get_lookup_type(vs_cls)
            detail_prefix = f"{nested_prefix}/{{{child_lookup}:{child_lookup_type}}}"
            detail_actions: dict[str, str] = {}
            if _has_method(vs_cls, "retrieve"):
                detail_actions["get"] = "retrieve"
            if _has_method(vs_cls, "update"):
                detail_actions["put"] = "update"
            if _has_method(vs_cls, "partial_update"):
                detail_actions["patch"] = "partial_update"
            if _has_method(vs_cls, "destroy"):
                detail_actions["delete"] = "destroy"

            if detail_actions:
                handler = vs_cls.as_view(
                    actions=detail_actions, parent_lookup=self.lookup
                )
                for method in detail_actions:
                    urls.append(
                        (
                            method.upper(),
                            detail_prefix,
                            handler,
                            f"{basename}-detail",
                        )
                    )

            # Custom @action routes
            for attr_name in list(vs_cls.__dict__.keys()):
                attr = vs_cls.__dict__[attr_name]
                if not callable(attr):
                    continue
                if not _is_action_method(attr):
                    continue

                action_detail = attr._action_detail
                action_url_path = attr._action_url_path
                action_url_name = attr._action_url_name
                action_methods = attr._action_methods

                action_map: dict[str, str] = {}
                for method in action_methods:
                    action_map[method.lower()] = attr_name

                action_handler = vs_cls.as_view(
                    actions=action_map, parent_lookup=self.lookup
                )
                if action_detail:
                    action_prefix = f"{detail_prefix}/{action_url_path}"
                else:
                    action_prefix = f"{nested_prefix}/{action_url_path}"

                action_name = f"{basename}-{action_url_name}"
                for method in action_methods:
                    urls.append((method, action_prefix, action_handler, action_name))

        return urls

    def mount(self, router: Router, namespace: str | None = None) -> None:
        """Mount nested routes on a Router instance."""
        parent_prefix = self.parent_router._prefix
        urls = self.get_urls()
        router.include(parent_prefix, urls, namespace=namespace)
