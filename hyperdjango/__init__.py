"""
hyperdjango — Django extended with native Zig performance.

Self-contained validation engine, pg.zig database, turboAPI HTTP server.
Zero external Python dependencies for core functionality.

Usage:
    from hyperdjango import HyperApp, Model, Field, Request, Response

    app = HyperApp(title="My App")

    @app.get("/")
    async def index(request):
        return Response.json({"hello": "world"})
"""

__version__ = "0.18.0"

# The native Zig extension is NOT imported at module-init time, so build tools
# (hyper-build, hyper-test) still work when the .so is missing or broken after a
# failed build. Public names are loaded lazily on first access via __getattr__,
# driven by the single mapping below — `name -> "module:attribute"`. To export a
# new name, add ONE line here (it flows into __all__ and __dir__ automatically).

_EXPORTS: dict[str, str] = {
    # Core
    "HyperApp": "hyperdjango.app:HyperApp",
    "HTTPException": "hyperdjango.exceptions:HTTPException",
    "Request": "hyperdjango.request:Request",
    "Response": "hyperdjango.response:Response",
    # ORM
    "Model": "hyperdjango.models:Model",
    "Field": "hyperdjango.models:Field",
    "DatabaseDefault": "hyperdjango.models:DatabaseDefault",
    "ManyToManyField": "hyperdjango.models:ManyToManyField",
    "VectorField": "hyperdjango.models:VectorField",
    "BaseModel": "hyperdjango.validation.core:BaseModel",
    # Middleware
    "CORSMiddleware": "hyperdjango.standalone_middleware:CORSMiddleware",
    "SecurityHeadersMiddleware": "hyperdjango.standalone_middleware:SecurityHeadersMiddleware",
    "RateLimitMiddleware": "hyperdjango.ratelimit:RateLimitMiddleware",
    "TimingMiddleware": "hyperdjango.standalone_middleware:TimingMiddleware",
    "StaticFilesMiddleware": "hyperdjango.staticfiles:StaticFilesMiddleware",
    # Auth / security
    "SessionAuth": "hyperdjango.auth.sessions:SessionAuth",
    "APIKeyAuth": "hyperdjango.auth.api_keys:APIKeyAuth",
    "require_auth": "hyperdjango.auth.decorators:require_auth",
    "guard": "hyperdjango.guard:guard",
    "Require": "hyperdjango.guard:Require",
    "TokenEngine": "hyperdjango.signing:TokenEngine",
    "SigningKey": "hyperdjango.signing:SigningKey",
    "SignedSessionMixin": "hyperdjango.signing:SignedSessionMixin",
    "SignedAPIKeyMixin": "hyperdjango.signing:SignedAPIKeyMixin",
    # Misc
    "mount_docs": "hyperdjango.openapi:mount_docs",
    "TestClient": "hyperdjango.testing:TestClient",
    "StatusTimelineMixin": "hyperdjango.timeline:StatusTimelineMixin",
    "StatusRecord": "hyperdjango.timeline:StatusRecord",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, _, attr = target.partition(":")
    import importlib

    # dynamic-attr: resolving a public export by its (data-driven) name from the _EXPORTS mapping
    return getattr(importlib.import_module(module_name), attr)


def __dir__() -> list[str]:
    # So IDEs / repl autocomplete / `dir(hyperdjango)` see the full public API,
    # not just what has already been lazily imported.
    return [*globals(), *__all__]
