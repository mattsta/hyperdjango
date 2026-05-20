"""
Django Admin with built-in performance monitoring and query acceleration.

Drop-in replacements for Django's AdminSite and ModelAdmin that provide:
- Auto-prefetch ALL relational fields across list_display, search_fields,
  list_filter, raw_id_fields, autocomplete_fields — not just list_display
- Nested FK traversal detection (e.g., 'author__profile__avatar' in search_fields)
- Query count + total time on every admin page
- Slow query highlighting
- N+1 pattern detection with fix suggestions
- Override get_list_select_related for targeted, computed select_related

Usage:
    # admin.py
    from hyperdjango.serving.admin import HyperAdminSite, HyperModelAdmin

    admin_site = HyperAdminSite(name='hyper_admin')

    class ArticleAdmin(HyperModelAdmin):
        list_display = ['title', 'author', 'category', 'created_at']
        search_fields = ['title', 'author__name', 'category__name']
        list_filter = ['status', 'category', 'author__is_active']
        # ALL relational paths auto-detected and prefetched — zero N+1!

    admin_site.register(Article, ArticleAdmin)

    # urls.py
    urlpatterns = [path('admin/', admin_site.urls)]

    # Or: monkey-patch the default admin site
    from hyperdjango.serving.admin import install_hyper_admin
    install_hyper_admin()  # replaces django.contrib.admin.site
"""

import re
from collections import Counter

from django.conf import settings
from django.contrib.admin import AdminSite, ModelAdmin
from django.core.exceptions import FieldDoesNotExist
from django.db.models.fields.related import (
    ForeignKey,
    ManyToManyField,
    ManyToManyRel,
    OneToOneField,
)


class HyperAdminSite(AdminSite):
    """AdminSite with built-in performance monitoring.

    Injects query stats into every admin page's template context.
    Works with HyperPerformanceMiddleware to track queries per request.
    """

    site_header = "HyperDjango Admin"

    def each_context(self, request):
        ctx = super().each_context(request)
        # Performance stats injected by HyperPerformanceMiddleware
        # dynamic-attr: optional attr injected onto Django's HttpRequest by HyperPerformanceMiddleware only under DEBUG; absent otherwise
        perf = getattr(request, "_hyper_perf_stats", None)
        if perf:
            ctx["hyper_perf"] = perf
        return ctx


def _resolve_field_relations(model, field_path):
    """Resolve a possibly nested field path and classify as select or prefetch.

    Given a model and a field path like 'author__profile__avatar', walks the
    model._meta chain and returns:
        ('select', 'author__profile') — for FK/OneToOne chains
        ('prefetch', 'tags')          — for M2M/reverse FK
        None                          — for non-relational or invalid paths

    The returned path is the deepest FK chain that should be select_related.
    """
    parts = field_path.split("__")
    current_model = model
    select_path_parts = []

    for i, part in enumerate(parts):
        try:
            field = current_model._meta.get_field(part)
        except FieldDoesNotExist:
            # Not a model field — could be a lookup (icontains, exact, etc.)
            # or a callable. Stop here with whatever we've accumulated.
            break

        if isinstance(field, (ForeignKey, OneToOneField)):
            select_path_parts.append(part)
            current_model = field.related_model
        elif isinstance(field, (ManyToManyField, ManyToManyRel)):
            # M2M can only be the terminal field, needs prefetch
            return (
                "prefetch",
                "__".join(select_path_parts + [part])
                if not select_path_parts
                else part,
            )
        elif hasattr(field, "related_model") and field.related_model is not None:
            # Reverse FK (one_to_many) — needs prefetch
            if hasattr(field, "get_accessor_name"):
                return ("prefetch", field.get_accessor_name())
            return ("prefetch", part)
        else:
            # Scalar field — stop. The FK chain up to here is what we want.
            break

    if select_path_parts:
        return ("select", "__".join(select_path_parts))
    return None


def _collect_admin_relations(model, admin_instance, request):
    """Collect all relational field paths from ALL admin configuration surfaces.

    Inspects:
    - list_display: direct FK/M2M columns shown in changelist
    - search_fields: FK traversals like 'author__name' that cause JOINs
    - list_filter: FK fields that Django resolves for filter dropdowns
    - raw_id_fields: FK fields (edit form, but useful to prefetch for display)
    - autocomplete_fields: FK fields (same as raw_id_fields)
    - list_select_related: explicit user overrides (respected as-is)
    - inlines: FK from inline model back to parent (reverse prefetch)

    Returns (select_fields, prefetch_fields) — deduplicated, sorted for stability.
    """
    select_fields = set()
    prefetch_fields = set()

    # If user explicitly set list_select_related, respect it
    # Django's ModelAdmin declares list_select_related = False as a class default.
    explicit_select = admin_instance.list_select_related
    if explicit_select is True:
        # User wants greedy select_related — we still add prefetch for M2M
        select_fields.add("__all__")
    elif explicit_select and isinstance(explicit_select, (list, tuple)):
        select_fields.update(explicit_select)

    # Collect field names from all admin config surfaces
    field_sources = set()

    # 1. list_display
    for name in admin_instance.get_list_display(request):
        field_sources.add(name)

    # 2. search_fields (may contain __ traversals like 'author__name')
    for name in admin_instance.get_search_fields(request):
        # Strip lookup suffixes (icontains, exact, etc.) but keep FK path
        clean = name.lstrip("^=@")  # Django search prefix characters
        field_sources.add(clean)

    # 3. list_filter (can be strings or tuples or filter classes)
    for f in admin_instance.get_list_filter(request):
        if isinstance(f, str):
            field_sources.add(f)
        elif isinstance(f, (list, tuple)) and len(f) >= 1:
            field_sources.add(f[0])

    # 4. raw_id_fields + autocomplete_fields
    # Django's ModelAdmin declares raw_id_fields/autocomplete_fields = () defaults.
    for name in admin_instance.raw_id_fields:
        field_sources.add(name)
    for name in admin_instance.autocomplete_fields:
        field_sources.add(name)

    # 5. Inline models — prefetch the reverse relation
    # ModelAdmin.inlines defaults to (); InlineModelAdmin.model defaults to None.
    for inline_class in admin_instance.inlines:
        inline_model = inline_class.model
        if inline_model is None:
            continue
        # Find the FK from inline model pointing to our model
        for field in inline_model._meta.get_fields():
            if isinstance(field, ForeignKey) and field.related_model is model:
                # The reverse accessor on our model
                accessor = field.remote_field.get_accessor_name()
                prefetch_fields.add(accessor)
                break

    # Resolve each field source to select/prefetch classification
    for field_path in field_sources:
        result = _resolve_field_relations(model, field_path)
        if result is None:
            continue
        kind, path = result
        if kind == "select":
            select_fields.add(path)
        else:
            prefetch_fields.add(path)

    # Remove the __all__ sentinel — handled by caller
    has_greedy_select = "__all__" in select_fields
    select_fields.discard("__all__")

    return select_fields, prefetch_fields, has_greedy_select


class HyperModelAdmin(ModelAdmin):
    """ModelAdmin with comprehensive auto-prefetch across ALL admin surfaces.

    Analyzes list_display, search_fields, list_filter, raw_id_fields,
    autocomplete_fields, and inlines to auto-detect ALL relational fields
    and add select_related/prefetch_related. Handles nested FK traversals
    (e.g., 'author__profile' from search_fields=['author__profile__bio']).

    Eliminates N+1 patterns in:
    - Changelist FK columns
    - Search across FK paths
    - Filter dropdowns on FK fields
    - Inline reverse relations
    """

    # Cache computed relations per model class to avoid re-introspection.
    # Class-level so the result is shared across instances and requests; the
    # admin config surfaces (list_display/search_fields/…) that drive the
    # computation are fixed per ModelAdmin class, so the relation set is stable.
    _relation_cache = {}

    def _cached_relations(self, request):
        """Memoized (select, prefetch, greedy) relations for this model.

        A changelist introspects relations in both get_queryset and
        get_list_select_related; this cache, keyed by model class, computes
        them once and shares the result across both calls.
        """
        key = self.model
        cached = HyperModelAdmin._relation_cache.get(key)
        if cached is None:
            cached = _collect_admin_relations(self.model, self, request)
            HyperModelAdmin._relation_cache[key] = cached
        return cached

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        select_fields, prefetch_fields, greedy_select = self._cached_relations(request)

        if greedy_select:
            qs = qs.select_related()
        elif select_fields:
            qs = qs.select_related(*sorted(select_fields))

        if prefetch_fields:
            qs = qs.prefetch_related(*sorted(prefetch_fields))

        return qs

    def get_list_select_related(self, request):
        """Override Django's list_select_related with computed fields.

        Django's ChangeList.apply_select_related() checks this. We return our
        computed list so Django doesn't do its own (potentially unbounded)
        select_related() on top.
        """
        # Django's ModelAdmin declares list_select_related = False as a class default.
        explicit = self.list_select_related
        if explicit is not None and explicit is not False:
            return explicit

        select_fields, _, _ = self._cached_relations(request)
        if select_fields:
            return sorted(select_fields)
        return False


def analyze_queries(queries):
    """Analyze Django connection.queries for performance issues.

    Returns dict with:
    - query_count: total queries
    - total_ms: total query time
    - slow_queries: list of {sql, time_ms} for queries over threshold
    - n_plus_one: list of {pattern, count, suggestion} for N+1 patterns
    """
    # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
    slow_threshold = getattr(settings, "HYPERDJANGO_SLOW_QUERY_MS", 100)
    # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
    n_plus_one_threshold = getattr(settings, "HYPERDJANGO_N_PLUS_ONE_THRESHOLD", 5)

    total_ms = 0.0
    slow_queries = []
    sql_patterns = Counter()

    for q in queries:
        time_ms = float(q.get("time", 0)) * 1000
        total_ms += time_ms
        sql = q.get("sql", "")

        # Normalize SQL for pattern detection
        normalized = re.sub(r"'[^']*'", "'?'", sql)
        normalized = re.sub(r"\b\d+\b", "?", normalized)
        sql_patterns[normalized] += 1

        if time_ms > slow_threshold:
            slow_queries.append(
                {
                    "sql": sql[:300],
                    "time_ms": round(time_ms, 2),
                }
            )

    # Detect N+1 patterns
    n_plus_one = []
    for pattern, count in sql_patterns.items():
        if count >= n_plus_one_threshold:
            # Try to extract table/field info for suggestion
            suggestion = _suggest_fix(pattern)
            n_plus_one.append(
                {
                    "pattern": pattern[:200],
                    "count": count,
                    "suggestion": suggestion,
                }
            )

    return {
        "query_count": len(queries),
        "total_ms": round(total_ms, 1),
        "slow_queries": sorted(slow_queries, key=lambda x: -x["time_ms"])[:10],
        "n_plus_one": n_plus_one,
        "has_issues": bool(slow_queries or n_plus_one),
    }


def _suggest_fix(sql_pattern):
    """Generate a fix suggestion from an N+1 SQL pattern."""
    # Try to extract the FK field from WHERE clause
    match = re.search(
        r'FROM\s+"?(\w+)"?\s+WHERE\s+"?(\w+)"?\s*=', sql_pattern, re.IGNORECASE
    )
    if match:
        table = match.group(1)
        field = match.group(2)
        # Convert table_name to model guess
        model_guess = table.replace("_", " ").title().replace(" ", "")
        field_clean = field.replace("_id", "")
        return f'.select_related("{field_clean}") or .prefetch_related("{field_clean}")'
    return "Add select_related() or prefetch_related() for this relation"


def install_hyper_admin():
    """Replace the default Django admin site with HyperAdminSite.

    Call this in your project's urls.py or AppConfig.ready():
        from hyperdjango.serving.admin import install_hyper_admin
        install_hyper_admin()
    """
    from django.contrib import admin

    hyper_site = HyperAdminSite(name="admin")
    # Copy all registered models from default site
    for model, model_admin in admin.site._registry.items():
        hyper_site.register(model, type(model_admin))
    admin.site = hyper_site
