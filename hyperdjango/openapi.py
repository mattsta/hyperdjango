"""
OpenAPI 3.1 schema generation with Serializer integration.

Auto-generates OpenAPI 3.1 spec from HyperApp routes, Serializer definitions,
and Model metadata. Serves Swagger UI for interactive API documentation.

Usage:
    from hyperdjango.openapi import mount_docs

    mount_docs(app)
    # GET /docs      → Swagger UI
    # GET /openapi.json → OpenAPI 3.1 spec

    # Or generate spec programmatically:
    from hyperdjango.openapi import generate_openapi
    spec = generate_openapi(app)
"""

import html as _html
import inspect
import re
from dataclasses import dataclass
from typing import get_type_hints

from hyperdjango.conf import WRITE_METHODS
from hyperdjango.native import fast_json_dumps
from hyperdjango.response import Response
from hyperdjango.serializers import Serializer, SerializerFieldInfo


@dataclass(slots=True)
class OpenAPISpecCache:
    """Pre-serialized OpenAPI spec bytes for one `mount_docs` instance.

    The spec is static per-process (routes + serializers + type hints
    never change at runtime), so `mount_docs` builds and stores the
    final JSON bytes on first request. The cached_bytes field is None
    until the first `GET /openapi.json` call, after which subsequent
    requests return the cached payload directly via Response(body=...).

    Owned by HyperApp._openapi_caches (one entry per mount_docs call).
    """

    title: str | None
    version: str
    description: str
    cached_bytes: bytes | None = None

    def invalidate(self) -> None:
        """Drop the cached payload so the next request rebuilds it."""
        self.cached_bytes = None


def invalidate_openapi_cache(app) -> None:
    """Reset all mount_docs() JSON caches for the given app.

    Useful in tests that `mount_docs()` before adding more routes and
    want to verify the added routes show up in the spec. Also useful
    if you reload routes via `hot_reload.py` at runtime.
    """
    for cache in app._openapi_caches:
        cache.invalidate()


# ── Membership check constants ────────────────────────────────────────────
_SKIP_PARAMS = frozenset({"request", "return"})

# ── Python type → OpenAPI type mapping ──────────────────────────────────────

_TYPE_MAP: dict[type, dict[str, str]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number", "format": "double"},
    bool: {"type": "boolean"},
}

# Additional type mappings for common Python types
_TYPE_NAME_MAP: dict[str, dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number", "format": "double"},
    "bool": {"type": "boolean"},
    "datetime": {"type": "string", "format": "date-time"},
    "date": {"type": "string", "format": "date"},
    "time": {"type": "string", "format": "time"},
    "Decimal": {"type": "string", "format": "decimal"},
    "UUID": {"type": "string", "format": "uuid"},
    "bytes": {"type": "string", "format": "binary"},
    "dict": {"type": "object"},
    "list": {"type": "array"},
}


def _python_type_to_schema(field_type: type) -> dict[str, str]:
    """Convert a Python type to an OpenAPI schema dict."""
    if field_type in _TYPE_MAP:
        return dict(_TYPE_MAP[field_type])
    type_name = (
        field_type.__name__ if hasattr(field_type, "__name__") else str(field_type)
    )
    if type_name in _TYPE_NAME_MAP:
        return dict(_TYPE_NAME_MAP[type_name])
    return {"type": "string"}


# ── Serializer → Schema conversion ─────────────────────────────────────────


def serializer_to_schema(
    serializer_class,
    mode: str = "output",
    schemas: dict[str, dict[str, object]] | None = None,
    _referenced: set[str] | None = None,
) -> dict[str, object]:
    """Convert a Serializer class to an OpenAPI schema.

    Args:
        serializer_class: A Serializer subclass with _serializer_fields.
        mode: "output" (response, excludes write_only) or "input" (request, excludes read_only).
        schemas: Component schemas dict for $ref deduplication.
        _referenced: Internal accumulator of component names emitted as a
            `$ref` target during this recursion subtree. Used to detect
            self-referential / cyclic serializers — callers never pass it.

    Returns:
        OpenAPI schema dict. For a non-recursive serializer this is the
        inline `{"type": "object", "properties": {...}}` schema. For a
        self-referential / cyclic serializer the real schema is stored in
        `schemas[<Name>]` and a `{"$ref": "#/components/schemas/<Name>"}`
        is returned so the reference resolves to a stable component.
    """
    if not hasattr(serializer_class, "_serializer_fields"):
        return {"type": "object"}

    # Recursion guard for recursive / self-referential serializers.
    # A self-referential serializer (e.g. NodeSerializer with a `child:
    # NodeSerializer` field) or a cycle (A→B→A) would recurse forever
    # without a placeholder pre-inserted into `schemas`. We register THIS
    # serializer's own name in `schemas` up front so that any nested field
    # pointing back at it resolves to `#/components/schemas/<Name>` instead
    # of recursing. If our own name ends up referenced as a $ref target
    # anywhere in the subtree we return a root $ref so the real schema
    # lives at a stable component address (never an empty placeholder).
    if _referenced is None:
        _referenced = set()
    # Component names are mode-scoped ("{Name}Input" / "{Name}Output"). An
    # input-mode schema and an output-mode schema of the same serializer are
    # DIFFERENT shapes (read_only vs write_only fields, required lists), so
    # nested $refs must resolve to a mode-matching component. Keying nested
    # components by bare class name let an input schema $ref an output-shaped
    # component (read-only fields shown writable, write-only omitted).
    mode_suffix = "Input" if mode == "input" else "Output"
    self_name = f"{serializer_class.__name__}{mode_suffix}"
    self_registered = False
    if schemas is not None and self_name not in schemas:
        schemas[self_name] = {}  # placeholder reserves the slot
        self_registered = True

    properties: dict[str, dict[str, object]] = {}
    required: list[str] = []

    for field_name, field_info in serializer_class._serializer_fields.items():
        if not isinstance(field_info, SerializerFieldInfo):
            continue

        # Skip based on mode
        if mode == "output" and field_info.write_only:
            continue
        if mode == "input" and field_info.read_only:
            continue

        # Check for nested serializer
        ft = field_info.field_type
        if isinstance(ft, type) and issubclass(ft, Serializer) and ft is not Serializer:
            # Nested serializer → mode-scoped $ref (see mode_suffix above).
            nested_name = f"{ft.__name__}{mode_suffix}"
            if schemas is not None and nested_name not in schemas:
                # Pre-insert a placeholder BEFORE recursing so a cycle
                # (A→B→A) or self-reference terminates: the recursive call
                # sees `nested_name` already present and emits a $ref.
                schemas[nested_name] = {}
                schemas[nested_name] = serializer_to_schema(
                    ft, mode=mode, schemas=schemas, _referenced=_referenced
                )
            # Record that `nested_name` is reachable via a $ref so the
            # call that registered it knows to keep a real schema there.
            _referenced.add(nested_name)
            prop = {"$ref": f"#/components/schemas/{nested_name}"}
        else:
            prop = _python_type_to_schema(ft)

        # Apply constraints from SerializerFieldInfo
        if field_info.min_length is not None:
            prop["minLength"] = field_info.min_length
        if field_info.max_length is not None:
            prop["maxLength"] = field_info.max_length
        if field_info.min_value is not None:
            prop["minimum"] = field_info.min_value
        if field_info.max_value is not None:
            prop["maximum"] = field_info.max_value
        if field_info.choices is not None:
            prop["enum"] = field_info.choices
        if field_info.label:
            prop["title"] = field_info.label
        if field_info.help_text:
            prop["description"] = field_info.help_text
        if field_info.default is not None and mode == "input":
            prop["default"] = field_info.default

        properties[field_name] = prop

        # Required fields (input mode only)
        if mode == "input" and field_info.required and field_info.default is None:
            required.append(field_name)

    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    if self_registered:
        if self_name in _referenced:
            # This serializer is self-referential / part of a cycle: some
            # nested $ref points back at `self_name`. Store the real schema
            # at the reserved slot and return a root $ref so the caller's
            # stored value (and every nested $ref) resolves to it.
            schemas[self_name] = schema
            return {"$ref": f"#/components/schemas/{self_name}"}
        # Non-recursive: nothing referenced our reserved slot. Drop the
        # stale placeholder and return the inline schema unchanged so
        # existing (non-recursive) callers see identical output.
        del schemas[self_name]

    return schema


# ── Spec generation ─────────────────────────────────────────────────────────


def generate_openapi(
    app,
    title: str | None = None,
    version: str = "1.0.0",
    description: str = "",
) -> dict[str, object]:
    """Generate an OpenAPI 3.1 spec from a HyperApp.

    Inspects all registered routes and their handlers for:
    - Path parameters (from URL patterns)
    - Request body schemas (from Serializer type hints or annotations)
    - Response schemas (from return type hints or Serializer usage)
    - Docstrings for summaries and descriptions

    Args:
        app: HyperApp instance with registered routes.
        title: API title (defaults to app.title).
        version: API version string.
        description: API description.

    Returns:
        OpenAPI 3.1 spec as a dict (JSON-serializable).
    """
    schemas: dict[str, dict[str, object]] = {}

    spec: dict[str, object] = {
        "openapi": "3.1.0",
        "info": {
            "title": title or app.title,
            "version": version,
            "description": description,
        },
        "paths": {},
        "components": {"schemas": schemas},
    }

    # Group routes by path
    path_map: dict[str, dict[str, object]] = {}
    for route in app.router.routes():
        openapi_path = _convert_path(route.pattern)
        if openapi_path not in path_map:
            path_map[openapi_path] = {}

        method = route.method.lower()
        operation = _build_operation(route, schemas)
        path_map[openapi_path][method] = operation

    spec["paths"] = path_map

    # Add security schemes if auth middleware is configured
    security_schemes = _detect_security_schemes(app)
    if security_schemes:
        spec["components"]["securitySchemes"] = security_schemes
        # Emit a top-level `security` requirement referencing the declared
        # schemes. Without this, OpenAPI treats securitySchemes as merely
        # *available* and marks EVERY operation public — generated docs and
        # clients would then omit auth on all endpoints. Routes carry no
        # per-operation auth metadata here, so we apply a sensible global
        # default: each scheme is a separate requirement object, i.e. any one
        # of the detected schemes (session OR API key) satisfies the
        # requirement (OpenAPI 3.x OR-of-requirements semantics).
        spec["security"] = [{name: []} for name in security_schemes]

    return spec


def mount_docs(
    app,
    path: str = "/docs",
    openapi_path: str = "/openapi.json",
    title: str | None = None,
    version: str = "1.0.0",
    description: str = "",
) -> None:
    """Mount OpenAPI docs endpoints on the app.

    Adds:
    - GET {openapi_path} → JSON OpenAPI 3.1 spec (cached — see below)
    - GET {path} → Swagger UI HTML page

    Performance: the OpenAPI spec is static per-process (determined by
    routes, serializers, and type hints — none of which change at
    runtime). The first request to {openapi_path} builds and caches
    the spec as pre-serialized JSON bytes; subsequent requests return
    the cached bytes directly. Measured:

    - `generate_openapi(bookstore_api_app)` direct: ~1.83 ms
      (`typing.get_type_hints` alone is 18 % of that).
    - `GET /openapi.json` cold: ~2.37 ms (build + json.dumps).
    - `GET /openapi.json` warm (this cache): ~50 μs (cached bytes
      passed straight to Response).

    This is a ~40-50× throughput improvement on the cached path with
    zero API change.

    Cache invalidation: lazy rebuild on first request. If routes are
    added AFTER mount_docs is called (the common case — `mount_docs`
    typically runs early in app setup before most `@app.get` routes
    are defined), the cache is populated on the very first request
    where all routes have been registered. To force a rebuild at
    runtime (e.g. in tests), call `invalidate_openapi_cache(app)`.

    Args:
        app: HyperApp instance.
        path: URL path for the Swagger UI page.
        openapi_path: URL path for the JSON spec endpoint.
        title: API title override.
        version: API version.
        description: API description.
    """
    # Typed spec cache owned by the app. Appended to the app's
    # _openapi_caches list so invalidate_openapi_cache(app) can reset
    # every mount_docs cache for the app in a single pass.
    cache = OpenAPISpecCache(title=title, version=version, description=description)
    app._openapi_caches.append(cache)

    @app.get(openapi_path, name="openapi_spec")
    async def openapi_json(request):
        # Bind the bytes to a local ONCE: a concurrent invalidate_openapi_cache
        # can reset cache.cached_bytes to None between the guard and the Response,
        # which would otherwise send body=None.
        body = cache.cached_bytes
        if body is None:
            spec = generate_openapi(
                app,
                title=cache.title,
                version=cache.version,
                description=cache.description,
            )
            body = fast_json_dumps(spec)
            cache.cached_bytes = body
        return Response(
            body=body,
            status=200,
            content_type="application/json",
        )

    @app.get(path, name="swagger_ui")
    async def docs_page(request):
        # NOTE: the Swagger UI CSS/JS below are loaded from the jsDelivr CDN.
        # This requires outbound internet access at doc-view time and pins a
        # third-party origin. For air-gapped/CSP-strict deployments, vendor the
        # swagger-ui-dist assets locally and serve them via StaticFilesMiddleware,
        # then point these <link>/<script> tags at the local prefix. Left as CDN
        # for now (low priority — the docs page is a developer convenience).
        doc_title = _html.escape(title or app.title)
        html = f"""<!DOCTYPE html>
<html><head>
<title>{doc_title} - API Docs</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
<script>
SwaggerUIBundle({{
    url: "{openapi_path}",
    dom_id: "#swagger-ui",
    deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    plugins: [SwaggerUIBundle.plugins.DownloadUrl],
    layout: "StandaloneLayout",
    defaultModelsExpandDepth: 2,
    defaultModelExpandDepth: 2,
}})
</script>
</body></html>"""
        return Response.html(html)


# ── Internal helpers ────────────────────────────────────────────────────────


def _convert_path(pattern: str) -> str:
    """Convert {param:type} to OpenAPI {param} format."""
    return re.sub(r"\{(\w+)(?::\w+)?\}", r"{\1}", pattern)


# The framework's unified error body shape, for every 4xx/5xx response.
_ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "detail": {"type": "string"},
        "status": {"type": "integer"},
        "errors": {"type": "object"},
    },
    "required": ["detail", "status"],
}


def _error_response(description: str) -> dict:
    """An OpenAPI response object for the unified {"detail","status"} error body."""
    return {
        "description": description,
        "content": {"application/json": {"schema": _ERROR_SCHEMA}},
    }


def _build_operation(
    route,
    schemas: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Build an OpenAPI operation from a route."""
    handler = route.handler
    doc = inspect.getdoc(handler) or ""

    operation: dict[str, object] = {
        "operationId": route.name or handler.__name__,
    }

    # Summary and description from docstring
    if doc:
        lines = doc.strip().split("\n")
        operation["summary"] = lines[0]
        if len(lines) > 1:
            operation["description"] = doc
    else:
        operation["summary"] = route.name or handler.__name__

    # Tags from route path prefix
    path_parts = route.pattern.strip("/").split("/")
    if path_parts and path_parts[0]:
        tag = path_parts[0]
        if not tag.startswith("{"):
            operation["tags"] = [tag]

    # Path parameters
    if route.param_names:
        params: list[dict[str, object]] = []
        for i, name in enumerate(route.param_names):
            param_type = "integer" if route.param_converters[i] is int else "string"
            params.append(
                {
                    "name": name,
                    "in": "path",
                    "required": True,
                    "schema": {"type": param_type},
                }
            )
        operation["parameters"] = params

    # Request body (POST/PUT/PATCH)
    if route.method in WRITE_METHODS:
        request_schema = _extract_request_schema(handler, schemas)
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": request_schema,
                },
            },
        }

    # Response schema
    response_schema = _extract_response_schema(handler, schemas)
    responses: dict[str, dict[str, object]] = {}

    if route.method == "POST":
        responses["201"] = {
            "description": "Created",
            "content": {"application/json": {"schema": response_schema}},
        }
    elif route.method == "DELETE":
        responses["204"] = {"description": "Deleted"}
    else:
        responses["200"] = {
            "description": "Successful response",
            "content": {"application/json": {"schema": response_schema}},
        }

    # Standard error responses — the unified {"detail","status"} body.
    if route.method in WRITE_METHODS:
        responses["400"] = _error_response("Validation error")

    responses["404"] = _error_response("Not found")

    operation["responses"] = responses
    return operation


def _register_serializer_component(
    serializer_class,
    mode: str,
    schemas: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Register a top-level serializer as a mode-scoped component and $ref it.

    Component names are ``{Name}Input`` / ``{Name}Output`` — matching the
    mode-scoped naming ``serializer_to_schema`` uses for its own (self_name)
    and nested components. A recursive serializer stores its real schema at
    ``schema_name`` itself and returns a self-``$ref``; in that case we must
    NOT overwrite the stored schema with the returned reference.
    """
    schema_name = (
        f"{serializer_class.__name__}{'Input' if mode == 'input' else 'Output'}"
    )
    ref = {"$ref": f"#/components/schemas/{schema_name}"}
    if schema_name not in schemas:
        built = serializer_to_schema(serializer_class, mode=mode, schemas=schemas)
        # For a recursive serializer, `built` is exactly this component's own
        # $ref and the real schema is already stored at schema_name — leave it.
        if built != ref:
            schemas[schema_name] = built
    return ref


def _extract_request_schema(
    handler,
    schemas: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Extract request body schema from handler's Serializer usage."""

    # Check for serializer in type hints
    try:
        hints = get_type_hints(handler)
    # blind-except: best-effort introspection; a handler with unresolvable annotations yields an empty request schema instead of aborting the OpenAPI document build.
    except Exception:
        hints = {}

    for param_name, param_type in hints.items():
        if param_name in _SKIP_PARAMS:
            continue
        if isinstance(param_type, type) and issubclass(param_type, Serializer):
            return _register_serializer_component(param_type, "input", schemas)

    # Check for __openapi_request__ attribute (decorator-based)
    # dynamic-attr: handler is an arbitrary route function; __openapi_request__ is present only when the optional @api_input decorator was applied
    serializer_class = getattr(
        handler, "__openapi_request__", None
    )  # set by @api_input decorator
    if (
        serializer_class is not None
        and isinstance(serializer_class, type)
        and issubclass(serializer_class, Serializer)
    ):
        return _register_serializer_component(serializer_class, "input", schemas)

    return {"type": "object"}


def _extract_response_schema(
    handler,
    schemas: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Extract response schema from handler's return type or Serializer."""

    # Check __openapi_response__ attribute
    # dynamic-attr: handler is an arbitrary route function; __openapi_response__ is present only when the optional @api_output decorator was applied
    serializer_class = getattr(
        handler, "__openapi_response__", None
    )  # set by @api_output decorator
    if (
        serializer_class is not None
        and isinstance(serializer_class, type)
        and issubclass(serializer_class, Serializer)
    ):
        return _register_serializer_component(serializer_class, "output", schemas)

    # Check return type hint
    try:
        hints = get_type_hints(handler)
    # blind-except: best-effort introspection; a handler with unresolvable annotations yields an empty response schema instead of aborting the OpenAPI document build.
    except Exception:
        hints = {}

    return_type = hints.get("return")
    if return_type is not None:
        if isinstance(return_type, type) and issubclass(return_type, Serializer):
            return _register_serializer_component(return_type, "output", schemas)

        # Direct type mapping
        schema = _TYPE_MAP.get(return_type)
        if schema:
            return dict(schema)

    return {"type": "object"}


def _detect_security_schemes(app) -> dict[str, dict[str, str]]:
    """Detect auth middleware and generate security scheme definitions."""
    schemes: dict[str, dict[str, str]] = {}

    # Check for session auth
    middleware_list = (
        app._middleware._middleware
        if hasattr(app, "_middleware") and app._middleware
        else []
    )
    for mw in middleware_list:
        mw_name = type(mw).__name__ if hasattr(mw, "__class__") else ""
        if "Session" in mw_name:
            schemes["sessionAuth"] = {
                "type": "apiKey",
                "in": "cookie",
                "name": "session",
                "description": "Session-based authentication via signed cookie",
            }
        if "APIKey" in mw_name:
            schemes["apiKeyAuth"] = {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "API key authentication",
            }

    return schemes


# ── Decorators for explicit schema binding ──────────────────────────────────


def api_input(serializer_class):
    """Decorator to bind a Serializer as the request body schema.

    Usage:
        @app.post("/users")
        @api_input(UserCreateSerializer)
        async def create_user(request):
            ...
    """

    def decorator(func):
        func.__openapi_request__ = serializer_class
        return func

    return decorator


def api_output(serializer_class):
    """Decorator to bind a Serializer as the response body schema.

    Usage:
        @app.get("/users/{id}")
        @api_output(UserSerializer)
        async def get_user(request, id: int):
            ...
    """

    def decorator(func):
        func.__openapi_response__ = serializer_class
        return func

    return decorator
