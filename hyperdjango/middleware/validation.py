"""
Pre-view validation middleware.

Validates incoming request data (JSON body, query params) using dhi
before the view is called. Invalid requests get a 422 response
without touching the view layer.

Usage:
    # settings.py
    MIDDLEWARE = [
        'hyperdjango.middleware.validation.PreValidationMiddleware',
        ...
    ]
    HYPERDJANGO_PRE_VALIDATION = True
"""

from django.http import JsonResponse

from hyperdjango.conf import WRITE_METHODS, get_setting
from hyperdjango.native import fast_json_loads
from hyperdjango.validation.core.validator import (
    ValidationError as DhiValidationError,
)
from hyperdjango.validation.core.validator import (
    ValidationErrors as DhiValidationErrors,
)


class PreValidationMiddleware:
    """Validates JSON request bodies using dhi before view dispatch.

    This is a lighter version of turboAPI's pre-GIL validation concept.
    For JSON API endpoints, this middleware can reject malformed requests
    early, reducing load on the view layer.

    Views can opt-in by setting a `hyper_schema` attribute:

        from hyperdjango.validation.core import BaseModel, Field

        class CreateUserSchema(BaseModel):
            name: str = Field(min_length=1, max_length=100)
            email: EmailStr

        def create_user(request):
            ...
        create_user.hyper_schema = CreateUserSchema
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not get_setting("PRE_VALIDATION"):
            return self.get_response(request)

        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Validate request body against the view's hyper_schema if present."""
        # dynamic-attr: optional marker attribute users attach to their view function; absent on unvalidated views
        schema = getattr(view_func, "hyper_schema", None)
        if schema is None:
            return None

        # Only validate POST/PUT/PATCH with JSON body
        if request.method not in WRITE_METHODS:
            return None

        content_type = request.content_type or ""
        if "json" not in content_type:
            return None

        try:
            body = fast_json_loads(request.body)
        except ValueError, RuntimeError:
            return JsonResponse(
                {"detail": "Invalid JSON body", "status": 400},
                status=400,
            )

        try:
            validated = schema.model_validate(body)
            # Attach validated data to request for the view to use
            request.hyper_validated = validated
            request.hyper_data = validated.model_dump()
        except (DhiValidationError, DhiValidationErrors) as e:
            errors = self._format_errors(e)
            return JsonResponse(
                {"detail": "Validation error", "status": 422, "errors": errors},
                status=422,
            )

        return None

    def _format_errors(self, exc):
        """Format dhi validation errors for JSON response."""
        if hasattr(exc, "errors") and callable(exc.errors):
            return [
                {
                    "field": error.get("loc", [None])[0] if error.get("loc") else None,
                    "message": error.get("msg", str(error)),
                    "type": error.get("type", "validation_error"),
                }
                for error in exc.errors()
            ]
        return [{"field": None, "message": str(exc), "type": "validation_error"}]
