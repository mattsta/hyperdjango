"""
Generic class-based views for common patterns.

Provides a View base class with HTTP method dispatch and specialized views
for list, detail, create, update, and delete operations.

Usage:
    from hyperdjango.views import ListView, DetailView, CreateView

    class UserList(ListView):
        model = User
        per_page = DEFAULT_PAGE_SIZE

    class UserDetail(DetailView):
        model = User

    class UserCreate(CreateView):
        model = User
        fields = ["name", "email"]
        success_url = "/users"

    # Register with app
    app.route("/users", methods=["GET"])(UserList.as_view())
    app.route("/users/{id:int}", methods=["GET"])(UserDetail.as_view())
    app.route("/users/new", methods=["GET", "POST"])(UserCreate.as_view())
"""

import inspect

from hyperdjango.conf import DEFAULT_PAGE_SIZE, get_setting
from hyperdjango.logging import logger
from hyperdjango.paginator import InvalidPage, Paginator
from hyperdjango.response import Response


class View:
    """Base class for class-based views.

    Dispatches to get(), post(), put(), patch(), delete() based on request method.
    Subclass and override the method handlers you need.
    """

    http_method_names = ["get", "head", "post", "put", "patch", "delete", "options"]

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            # dynamic-attr: Django-style CBV init — assigns arbitrary caller-supplied initkwargs as instance attributes
            setattr(self, key, value)

    @classmethod
    def as_view(cls, **initkwargs):
        """Return an async handler function for this view class.

        Usage:
            app.route("/path")(MyView.as_view())
        """

        async def view(request, **kwargs):
            self = cls(**initkwargs)
            self.request = request
            self.kwargs = kwargs
            return await self.dispatch(request, **kwargs)

        view.__name__ = cls.__name__
        view.__qualname__ = cls.__qualname__
        view.view_class = cls
        return view

    async def dispatch(self, request, **kwargs):
        """Route to the right method handler based on request.method."""
        method = request.method.lower()
        if method not in self.http_method_names:
            return self.http_method_not_allowed(request)

        # dynamic-attr: dispatch to a handler named by the runtime HTTP method; the subclass may not define it
        handler = getattr(self, method, None)
        if handler is None:
            return self.http_method_not_allowed(request)

        if inspect.iscoroutinefunction(handler):
            return await handler(request, **kwargs)
        return handler(request, **kwargs)

    def http_method_not_allowed(self, request):
        """Return 405 Method Not Allowed."""
        allowed = [m.upper() for m in self.http_method_names if hasattr(self, m)]
        # Unified error contract: {"detail", "status"} (+ Allow header), routed
        # through the same mapper every other boundary uses.
        return Response.error(
            405, "Method not allowed", headers={"Allow": ", ".join(allowed)}
        )

    def serialize_object(self, obj) -> dict[str, object]:
        """Convert a Model instance to a dict via to_dict().

        Respects Field(exclude=True) — sensitive fields like password_hash
        are automatically omitted. Override in subclasses for custom output.
        """
        return obj.to_dict()

    async def head(self, request, **kwargs):
        """Default HEAD implementation: call GET, return without body."""
        return await self.get(request, **kwargs)


class ListView(View):
    """Display a paginated list of model instances.

    Attributes:
        model: The Model class to query
        queryset: Override to provide a custom queryset
        per_page: Items per page (default: 25, 0 = no pagination)
        ordering: Default ordering (e.g., "-created_at")
        template_name: Template to render (if using template response)
        context_object_name: Name for the object list in context (default: "object_list")
    """

    model = None
    queryset = None
    per_page: int = DEFAULT_PAGE_SIZE
    ordering: str | None = None
    template_name: str | None = None
    context_object_name: str = "object_list"

    def get_queryset(self):
        """Return the queryset to paginate."""
        if self.queryset is not None:
            return self.queryset
        if self.model is not None:
            qs = self.model.objects
            if self.ordering:
                qs = qs.order_by(self.ordering)
            return qs
        raise ValueError("ListView requires either 'model' or 'queryset'")

    async def get(self, request, **kwargs):
        """Handle GET: return paginated list."""
        qs = self.get_queryset()

        # Apply request filters
        qs = self.apply_filters(qs, request)

        if self.per_page > 0:
            paginator = Paginator(qs, per_page=self.per_page)
            page_num = request.GET.get("page", "1")
            try:
                page = await paginator.page(page_num)
            except InvalidPage:
                # Only a bad/out-of-range page NUMBER falls back to page 1.
                # A real error (DB failure, etc.) must NOT be swallowed into
                # silently-wrong results — log it and let it propagate.
                page = await paginator.page(1)
            except Exception:
                logger.opt(exception=True).error(
                    "ListView: pagination query failed for page {page_num!r}.",
                    page_num=page_num,
                )
                raise
            items = page.items
            context = self.get_context_data(
                page=page,
                paginator=paginator,
                **kwargs,
            )
        else:
            items = await qs.all()
            context = self.get_context_data(**kwargs)

        context[self.context_object_name] = [
            self.serialize_item(item) for item in items
        ]
        return Response.json(context)

    def apply_filters(self, qs, request):
        """Override to apply request-based filters to the queryset."""
        return qs

    def get_context_data(self, **kwargs):
        """Build the response context dict."""
        context = {}
        if "page" in kwargs:
            page = kwargs["page"]
            context["page"] = page.number
            context["num_pages"] = page.num_pages
            context["count"] = page.count
            context["has_next"] = page.has_next
            context["has_previous"] = page.has_previous
        return context

    def serialize_item(self, item) -> dict[str, object]:
        """Convert a model instance to a dict for JSON response."""
        if hasattr(item, "_meta"):
            # dynamic-attr: reading model-instance fields by runtime column names from its _meta
            return {col: getattr(item, col) for col in item._meta.column_names}
        if hasattr(item, "__dict__"):
            return {k: v for k, v in item.__dict__.items() if not k.startswith("_")}
        return {"value": item}


class DetailView(View):
    """Display a single model instance by primary key.

    Attributes:
        model: The Model class to query
        pk_url_kwarg: URL kwarg name for the PK (default: "id")
        context_object_name: Name for the object in context (default: "object")
    """

    model = None
    pk_url_kwarg: str = "id"
    context_object_name: str = "object"

    async def get_object(self, **kwargs):
        """Fetch the object by PK. Returns None if not found."""
        pk = kwargs.get(self.pk_url_kwarg)
        if pk is None:
            return None
        try:
            return await self.model.objects.get(**{self.model._meta.pk_field: pk})
        except self.model.DoesNotExist:
            return None

    async def get(self, request, **kwargs):
        """Handle GET: return single object."""
        obj = await self.get_object(**kwargs)
        if obj is None:
            return Response.error(404, "Not found")

        data = self.serialize_object(obj)
        return Response.json({self.context_object_name: data})


class CreateView(View):
    """Handle object creation via POST.

    Attributes:
        model: The Model class to create
        fields: List of field names to accept from request body
        success_url: URL to redirect to after creation (or return in JSON)
    """

    model = None
    fields: list[str] | None = None
    success_url: str | None = None

    async def get(self, request, **kwargs):
        """Handle GET: return form schema / field list."""
        field_list = self.get_fields()
        return Response.json({"fields": field_list})

    async def post(self, request, **kwargs):
        """Handle POST: create object from request body."""
        data = request.json or {}

        # Filter to allowed fields
        field_list = self.get_fields()
        create_data = {k: v for k, v in data.items() if k in field_list}

        # Validate
        errors = self.validate(create_data)
        if errors:
            return Response.error(400, "Validation error", errors=errors)

        obj = await self.model.objects.create(**create_data)
        result = self.serialize_object(obj)

        response_data = {"created": result}
        if self.success_url:
            response_data["redirect"] = self.success_url
        return Response.json(response_data, status=201)

    def get_fields(self) -> list[str]:
        """Return the list of allowed fields."""
        if self.fields:
            return list(self.fields)
        if self.model:
            return self.model._meta.writable_columns
        return []

    def validate(self, data: dict[str, object]) -> dict[str, str]:
        """Override to add custom validation. Return dict of field → error message."""
        return {}


class UpdateView(View):
    """Handle object updates via PUT/PATCH.

    Attributes:
        model: The Model class to update
        fields: List of field names to accept
        pk_url_kwarg: URL kwarg name for the PK (default: "id")
        success_url: URL to redirect to after update
    """

    model = None
    fields: list[str] | None = None
    pk_url_kwarg: str = "id"
    success_url: str | None = None

    async def get_object(self, **kwargs):
        pk = kwargs.get(self.pk_url_kwarg)
        if pk is None:
            return None
        try:
            return await self.model.objects.get(**{self.model._meta.pk_field: pk})
        except self.model.DoesNotExist:
            return None

    async def get(self, request, **kwargs):
        """Handle GET: return current object data."""
        obj = await self.get_object(**kwargs)
        if obj is None:
            return Response.error(404, "Not found")
        data = self.serialize_object(obj)
        return Response.json({"object": data, "fields": self.get_fields()})

    async def put(self, request, **kwargs):
        """Handle PUT: full update."""
        return await self._do_update(request, **kwargs)

    async def patch(self, request, **kwargs):
        """Handle PATCH: partial update."""
        return await self._do_update(request, partial=True, **kwargs)

    async def _do_update(self, request, partial=False, **kwargs):
        obj = await self.get_object(**kwargs)
        if obj is None:
            return Response.error(404, "Not found")

        data = request.json or {}
        field_list = self.get_fields()
        update_data = {k: v for k, v in data.items() if k in field_list}

        errors = self.validate(update_data)
        if errors:
            return Response.error(400, "Validation error", errors=errors)

        # Apply updates
        for key, value in update_data.items():
            # dynamic-attr: assigning a model field named by request-supplied key (validated against get_fields)
            setattr(obj, key, value)
        await obj.save()

        result = self.serialize_object(obj)
        response_data = {"updated": result}
        if self.success_url:
            response_data["redirect"] = self.success_url
        return Response.json(response_data)

    def get_fields(self) -> list[str]:
        if self.fields:
            return list(self.fields)
        if self.model:
            return self.model._meta.writable_columns
        return []

    def validate(self, data: dict[str, object]) -> dict[str, str]:
        return {}


class DeleteView(View):
    """Handle object deletion via DELETE.

    Attributes:
        model: The Model class to delete from
        pk_url_kwarg: URL kwarg name for the PK (default: "id")
        success_url: URL to redirect to after deletion
    """

    model = None
    pk_url_kwarg: str = "id"
    success_url: str | None = None

    async def get_object(self, **kwargs):
        pk = kwargs.get(self.pk_url_kwarg)
        if pk is None:
            return None
        try:
            return await self.model.objects.get(**{self.model._meta.pk_field: pk})
        except self.model.DoesNotExist:
            return None

    async def get(self, request, **kwargs):
        """Handle GET: return confirmation data."""
        obj = await self.get_object(**kwargs)
        if obj is None:
            return Response.error(404, "Not found")
        data = self.serialize_object(obj)
        return Response.json({"object": data, "confirm": "DELETE to confirm"})

    async def delete(self, request, **kwargs):
        """Handle DELETE: delete object."""
        obj = await self.get_object(**kwargs)
        if obj is None:
            return Response.error(404, "Not found")

        pk_value = obj.pk
        await obj.delete()

        response_data = {"deleted": pk_value}
        if self.success_url:
            response_data["redirect"] = self.success_url
        return Response.json(response_data)


# ── Mixins ───────────────────────────────────────────────────────────────────


class LoginRequiredMixin:
    """Mixin that requires authentication.

    Add before View in MRO:
        class MyView(LoginRequiredMixin, DetailView):
            ...

    Redirects unauthenticated users to LOGIN_URL from conf settings.
    Override login_url on the class to use a per-view URL instead.
    """

    login_url: str | None = None

    def _get_login_url(self) -> str:
        """Return the login URL — class attribute overrides conf setting."""
        if self.login_url is not None:
            return self.login_url
        return get_setting("LOGIN_URL")

    async def dispatch(self, request, **kwargs):
        user = request.user
        if user is None:
            login_url = self._get_login_url()
            return Response.redirect(login_url, status=302)
        if not user.is_authenticated:
            login_url = self._get_login_url()
            return Response.redirect(login_url, status=302)
        return await super().dispatch(request, **kwargs)


class PermissionRequiredMixin:
    """Mixin that requires specific permission(s).

    Add before View in MRO:
        class MyView(PermissionRequiredMixin, UpdateView):
            permission_required = "edit_user"
    """

    permission_required: str | list[str] = ""

    async def dispatch(self, request, **kwargs):
        user = request.user
        if user is None:
            return Response.error(401, "Authentication required")

        perms = self.permission_required
        if isinstance(perms, str):
            perms = [perms] if perms else []

        for perm in perms:
            if not user.has_perm(perm):
                return Response.error(403, "Permission denied")

        return await super().dispatch(request, **kwargs)
