"""Declarative registry of the bundled services — ONE source of truth.

Every machine-readable fact about a bundled service lives here: its app import
path, its seed function, the port ``hyper service run`` binds it on, the secrets
it refuses to boot without, the companion services it needs, and the extra
environment it expects. Prose in ``services/<name>/README.md`` explains a
service; this module *defines* it.

Why it lives in ``hyperdjango/`` and not in ``services/`` or ``scripts/``:

* the ``hyper service`` CLI verb lives in :mod:`hyperdjango.cli`, so the registry
  must be importable without putting the repo root on ``sys.path`` (``services``
  is a repo-local package, not a package the framework can rely on importing);
* ``scripts/e2e_helper.py`` also consumes it, and ``scripts/`` is not importable
  from ``hyperdjango`` — the dependency only points one way if the registry sits
  on the framework side;
* it imports nothing but the standard library, so ``hyper service list`` pays
  none of the ~105 ms subsystem-import cost the CLI deliberately defers.

Ports
-----
``hyper service run`` binds inside :data:`SERVICE_PORT_BLOCK` (8600-8699). That
block is deliberately disjoint from:

* ``scripts/e2e_helper.TEST_PORTS`` (18100-19260) — the e2e suite's reserved
  ports, so a developer running a service never collides with a test run;
* ``services/live_config/run_mesh.py`` (8960/8970/8980) — the pre-existing
  standalone mesh launcher;
* the framework's default dev port (8000) and the Linux ephemeral range
  (32768-60999), which ``hyper doctor``'s ``ephemeral_port_overlap`` check
  guards against.

Runtime state
-------------
Services that keep on-disk demo state (minted tokens, KEK files) get a
per-service ``services/<name>/.runtime/`` directory, referenced from
:class:`EnvVar` values via the ``{runtime}`` placeholder. Generated signing
secrets are persisted to ``services/<name>/.env.local``. Both paths are
gitignored.
"""

from dataclasses import dataclass, field
from pathlib import Path

# Repo-local services tree. ``hyperdjango/`` and ``services/`` are siblings under
# the repository root, which is how the editable install is laid out.
SERVICES_ROOT = Path(__file__).resolve().parent.parent / "services"

# The contiguous port block owned by ``hyper service run``. See module docstring
# for why this block and not another.
SERVICE_PORT_BLOCK = range(8600, 8700)

# Placeholder substituted with a service's ``.runtime/`` directory in EnvVar
# values. Spelled once so the CLI and the tests agree on it.
RUNTIME_PLACEHOLDER = "{runtime}"


class UnknownServiceError(LookupError):
    """Raised for a name that is not a registered service."""


@dataclass(frozen=True, slots=True)
class SecretRequirement:
    """A signing secret a service refuses to boot without.

    ``generated=True`` means ``hyper service run`` can mint a stable random
    value and persist it. ``generated=False`` means only a human can supply it
    (an external service credential); the CLI reports it and stops rather than
    inventing a value that cannot possibly work.
    """

    env_var: str
    min_length: int
    purpose: str
    generated: bool = True


@dataclass(frozen=True, slots=True)
class EnvVar:
    """A static environment entry. ``value`` may embed :data:`RUNTIME_PLACEHOLDER`."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class CompanionToken:
    """An env var whose value is a token minted by a companion service's seed.

    The companion's seed writes ``tokens.json`` into its runtime directory;
    ``token_key`` selects one identity from it. This is the out-of-band
    credential handoff a real deployment pipeline would perform.
    """

    env_var: str
    companion: str
    token_key: str


@dataclass(frozen=True, slots=True)
class CompanionUrl:
    """An env var that must carry a companion's base URL at launch time.

    Resolved from the port the companion actually bound rather than declared as
    a literal, so the URL and the running service can never desync.
    """

    env_var: str
    companion: str


@dataclass(frozen=True, slots=True)
class Service:
    """Everything needed to take one bundled service from nothing to serving."""

    name: str
    app_path: str
    description: str
    port: int
    tags: tuple[str, ...]
    seed_path: str | None = None
    needs_database: bool = True
    secrets: tuple[SecretRequirement, ...] = ()
    extra_env: tuple[EnvVar, ...] = ()
    companions: tuple[str, ...] = ()
    companion_tokens: tuple[CompanionToken, ...] = ()
    companion_urls: tuple[CompanionUrl, ...] = ()
    # Where this service's seed writes its minted identity tokens, relative to
    # ``runtime_dir``. Declared rather than guessed: the seed writes into the
    # service's DEMO_DIR, which is a SUBDIRECTORY of the runtime dir, so a
    # naive ``runtime_dir / "tokens.json"`` looks in the wrong place.
    tokens_file: str = "tokens.json"
    launcher: str | None = None
    notes: str = ""

    @property
    def directory(self) -> Path:
        """The service's source directory."""
        return SERVICES_ROOT / self.name

    @property
    def module(self) -> str:
        """The importable module half of ``app_path``."""
        return self.app_path.split(":", 1)[0]

    @property
    def attribute(self) -> str:
        """The ``HyperApp`` attribute half of ``app_path``."""
        return self.app_path.split(":", 1)[1]

    @property
    def runtime_dir(self) -> Path:
        """Gitignored per-service scratch for demo state (tokens, KEKs)."""
        return self.directory / ".runtime"

    @property
    def tokens_path(self) -> Path:
        """Absolute path to the ``tokens.json`` this service's seed writes."""
        return self.runtime_dir / self.tokens_file

    @property
    def env_file(self) -> Path:
        """Gitignored per-service file holding generated secrets."""
        return self.directory / ".env.local"

    @property
    def database_name(self) -> str:
        """Dedicated database so two services never share a schema."""
        return f"hyper_service_{self.name}"

    @property
    def generated_secrets(self) -> tuple[SecretRequirement, ...]:
        return tuple(s for s in self.secrets if s.generated)

    @property
    def supplied_secrets(self) -> tuple[SecretRequirement, ...]:
        """Secrets only a human can provide (external service credentials)."""
        return tuple(s for s in self.secrets if not s.generated)

    def resolved_env(self) -> tuple[EnvVar, ...]:
        """``extra_env`` with :data:`RUNTIME_PLACEHOLDER` expanded."""
        runtime = str(self.runtime_dir)
        return tuple(
            EnvVar(v.name, v.value.replace(RUNTIME_PLACEHOLDER, runtime))
            for v in self.extra_env
        )


# ── Shared secret requirements ───────────────────────────────────────────────
# HyperSecret and HyperManager both call ``require_setting(..., min_length=32)``
# at import time and refuse to boot without these. The same values MUST reach
# the `hyper setup` seed process and the server process — seed-minted identity
# tokens are signed with SESSION_SIGNING_KEY and will not verify otherwise.

_SESSION_SIGNING_KEY = SecretRequirement(
    env_var="HYPER_SESSION_SIGNING_KEY",
    min_length=32,
    purpose="signs seeded identity tokens — setup and server must share it",
)
_SECRET_KEY = SecretRequirement(
    env_var="HYPER_SECRET_KEY",
    min_length=32,
    purpose="app session signing (require_setting, fail-closed at boot)",
)
_ADMIN_SECRET = SecretRequirement(
    env_var="HYPER_ADMIN_SECRET",
    min_length=32,
    purpose="HyperAdmin panel signing (require_setting, fail-closed at boot)",
)

_VAULT_SECRETS = (_SESSION_SIGNING_KEY, _SECRET_KEY, _ADMIN_SECRET)


_REGISTRY: tuple[Service, ...] = (
    Service(
        name="benchmark_app",
        app_path="services.benchmark_app.app:app",
        description="Minimal routes for load testing raw Zig HTTP throughput.",
        port=8601,
        tags=("benchmark", "http", "no-db"),
        needs_database=False,
    ),
    Service(
        name="blog_platform",
        app_path="services.blog_platform.app:app",
        seed_path="services.blog_platform.seed:run",
        description="Multi-language blog: XML sitemaps, RSS/Atom feeds, i18n.",
        port=8602,
        tags=("sitemaps", "feeds", "i18n", "rest", "admin"),
    ),
    Service(
        name="bookstore_api",
        app_path="services.bookstore_api.app:app",
        seed_path="services.bookstore_api.seed:run",
        description=(
            "Full REST API: ModelViewSet, serializers, pagination, "
            "filtering, caching, nested routers."
        ),
        port=8603,
        tags=("rest", "openapi", "caching", "admin", "telemetry"),
    ),
    Service(
        name="cms_lite",
        app_path="services.cms_lite.app:app",
        seed_path="services.cms_lite.seed:run",
        description="Lightweight CMS: URL redirects and flat pages.",
        port=8604,
        tags=("cms", "redirects", "flatpages", "admin"),
    ),
    Service(
        name="content_hub",
        app_path="services.content_hub.app:app",
        seed_path="services.content_hub.seed:run",
        description=(
            "CMS with Q objects, OneToOneField, single-table inheritance, "
            "HyperAdmin custom actions."
        ),
        port=8605,
        tags=("orm", "sti", "admin"),
    ),
    Service(
        name="deployment",
        app_path="services.deployment.app:app",
        seed_path="services.deployment.seed:run",
        description=(
            "Production deployment reference: systemd, nginx, health probes, "
            "env-based config."
        ),
        port=8606,
        tags=("deployment", "systemd", "health"),
        notes=(
            "The README's Configuration block names ALLOWED_ORIGINS, but the app "
            "reads CORS_ORIGINS — see services/deployment/env.service for the "
            "variables the code actually honours."
        ),
    ),
    Service(
        name="forms_demo",
        app_path="services.forms_demo.app:app",
        seed_path="services.forms_demo.seed:run",
        description=(
            "Form + ModelForm validation, cross-field clean(), file uploads, "
            "server-rendered HTML."
        ),
        port=8607,
        tags=("forms", "validation", "uploads"),
    ),
    Service(
        name="full_stack",
        app_path="services.full_stack.app:app",
        seed_path="services.full_stack.seed:run",
        description=(
            "Reference scaffold: project/task manager with session auth, "
            "templates, CRUD, JSON API, HyperAdmin."
        ),
        port=8608,
        tags=("full-stack", "auth", "templates", "admin"),
    ),
    Service(
        name="hello",
        app_path="services.hello.app:app",
        description="The simplest app — two routes, no database, no middleware.",
        port=8609,
        tags=("starter", "no-db"),
        needs_database=False,
    ),
    Service(
        name="hyperai",
        app_path="services.hyperai.app:app",
        seed_path="services.hyperai.seed:run",
        description=(
            "AI chat service: SSE streaming, API keys, tiered rate limits, "
            "OpenAI-compatible endpoint."
        ),
        port=8610,
        tags=("sse", "api-keys", "rate-limit", "admin"),
    ),
    Service(
        name="hypermanager",
        app_path="services.hypermanager.app:app",
        seed_path="services.hypermanager.seed:run",
        description=(
            "Change-notification hub: producers publish metadata-only change "
            "records, subscribers watch a live feed."
        ),
        port=8611,
        tags=("change-feed", "websocket", "rbac", "admin", "secrets"),
        secrets=_VAULT_SECRETS,
        extra_env=(
            EnvVar("HYPERMANAGER_DEMO_DIR", f"{RUNTIME_PLACEHOLDER}/manager_demo"),
        ),
        tokens_file="manager_demo/tokens.json",
    ),
    Service(
        name="hypernews",
        app_path="services.hypernews.app:app",
        seed_path="services.hypernews.seed:run",
        description=(
            "Community platform: multi-forum, threaded comments, karma voting, "
            "eigenvector ring detection, HTMX."
        ),
        port=8612,
        tags=("community", "voting", "htmx", "auth", "admin"),
        notes=(
            "Two seeds exist. seed:run (used here) creates trust tiers, users, "
            "forums AND calls ensure_admin_user() so the admin panel works; "
            "setup:seed creates admin + forums only and is what most e2e suites "
            "use. The README's 'setup:run' names a function that does not exist."
        ),
    ),
    Service(
        name="hypersecret",
        app_path="services.hypersecret.app:app",
        seed_path="services.hypersecret.seed:run",
        description=(
            "Self-hosted secret manager: envelope encryption, service identities, "
            "live rotation nudges via HyperManager."
        ),
        port=8613,
        tags=("secrets", "crypto", "change-feed", "admin", "mtls"),
        secrets=_VAULT_SECRETS,
        extra_env=(
            EnvVar("HYPERSECRET_DEMO_DIR", f"{RUNTIME_PLACEHOLDER}/secret_demo"),
            EnvVar("HYPERSECRET_ROTATION_SWEEP_INTERVAL", "5"),
        ),
        companions=("hypermanager",),
        companion_tokens=(
            CompanionToken(
                env_var="HYPERSECRET_MANAGER_TOKEN",
                companion="hypermanager",
                token_key="producer:hypersecret",
            ),
        ),
        companion_urls=(
            CompanionUrl(env_var="HYPERSECRET_MANAGER_URL", companion="hypermanager"),
        ),
        tokens_file="secret_demo/tokens.json",
    ),
    Service(
        name="hyperticket",
        app_path="services.hyperticket.app:app",
        seed_path="services.hyperticket.seed:run",
        description=(
            "Multi-tenant SaaS ticketing: tenancy, guards, dual auth, HTMX, "
            "background tasks, metering, SLA."
        ),
        port=8614,
        tags=("multi-tenant", "guards", "htmx", "tasks", "admin"),
        notes=(
            "README tells users to bind port 18810, which is inside the e2e "
            "suite's reserved TEST_PORTS range (websocket_stress). Use the "
            "registry port instead."
        ),
    ),
    Service(
        name="live_config",
        app_path="services.live_config.app:app",
        description=(
            "Three-service mesh: a storefront converges on a rotated key live, "
            "no restart (HyperSecret -> HyperManager -> Storefront)."
        ),
        port=8615,
        tags=("mesh", "live-config", "change-feed", "meta-service"),
        needs_database=False,
        launcher="services.live_config.run_mesh",
        companions=("hypersecret", "hypermanager"),
        extra_env=(
            EnvVar("LIVE_CONFIG_SF_PORT", "8615"),
            EnvVar("LIVE_CONFIG_HS_PORT", "8623"),
            EnvVar("LIVE_CONFIG_HM_PORT", "8624"),
        ),
        notes=(
            "Owns no tables itself. Its launcher creates and seeds two throwaway "
            "databases for the upstream services and performs the out-of-band "
            "token + KEK distribution, so `hyper service run` delegates to it "
            "rather than reimplementing that wiring."
        ),
    ),
    Service(
        name="metering_api",
        app_path="services.metering_api.app:app",
        seed_path="services.metering_api.seed:run",
        description=(
            "LLM-style API with usage metering, quota enforcement, IETF "
            "RateLimit headers."
        ),
        port=8616,
        tags=("metering", "quotas", "rate-limit", "admin"),
    ),
    Service(
        name="multi_tenant",
        app_path="services.multi_tenant.app:app",
        seed_path="services.multi_tenant.seed:run",
        description=(
            "Project-management SaaS: TenantMixin isolation, header-based tenant "
            "resolution, cross-tenant admin."
        ),
        port=8617,
        tags=("multi-tenant", "rest", "admin"),
        notes="API calls require an X-Tenant-ID header; see the README.",
    ),
    Service(
        name="notes_api",
        app_path="services.notes_api.app:app",
        seed_path="services.notes_api.seed:run",
        description=(
            "Intermediate service (~170 lines): session auth, cursor pagination, "
            "F-expression updates, FTS, HyperAdmin."
        ),
        port=8618,
        tags=("rest", "auth", "pagination", "fts", "admin"),
        notes=(
            "README tells users to bind port 18811, which is inside the e2e "
            "suite's reserved TEST_PORTS range (websocket_shared_loops). Use the "
            "registry port instead."
        ),
    ),
    Service(
        name="rest_api",
        app_path="services.rest_api.app:app",
        seed_path="services.rest_api.seed:run",
        description=(
            "Blog REST API: CRUD, session auth, API-key auth, CORS, OpenAPI docs."
        ),
        port=8619,
        tags=("rest", "openapi", "auth", "cors"),
    ),
    Service(
        name="semantic_search",
        app_path="services.semantic_search.app:app",
        seed_path="services.semantic_search.seed:run",
        description=(
            "pgvector nearest-neighbour search with HNSW cosine indexing over an "
            "OpenAI-compatible embeddings API."
        ),
        port=8620,
        tags=("pgvector", "embeddings", "search", "admin"),
        secrets=(
            SecretRequirement(
                env_var="EMBEDDINGS_API_KEY",
                min_length=1,
                purpose=(
                    "credential for the external embeddings service — no random "
                    "value can work, supply your own"
                ),
                generated=False,
            ),
        ),
        notes=(
            "Needs the pgvector extension; `hyper setup` creates it "
            "automatically. Point EMBEDDINGS_API_URL at a local Ollama/vLLM "
            "server to avoid a paid API."
        ),
    ),
    Service(
        name="task_queue",
        app_path="services.task_queue.app:app",
        seed_path="services.task_queue.seed:run",
        description=(
            "Background tasks: priorities, retry with backoff, dead-letter queue, "
            "TaskGroup, cron."
        ),
        port=8621,
        tags=("tasks", "queue", "cron", "admin"),
    ),
    Service(
        name="websocket_chat",
        app_path="services.websocket_chat.app:app",
        seed_path="services.websocket_chat.seed:run",
        description=(
            "Real-time chat on the native Zig RFC 6455 server: rooms, presence, "
            "channel pub/sub, LiveQuery."
        ),
        port=8622,
        tags=("websocket", "realtime", "presence", "admin"),
    ),
)


SERVICES: dict[str, Service] = {e.name: e for e in _REGISTRY}


def service_names() -> tuple[str, ...]:
    """Every registered service name, in registry order."""
    return tuple(SERVICES)


def get_service(name: str) -> Service:
    """Look up one service, or fail naming every valid choice."""
    service = SERVICES.get(name)
    if service is None:
        raise UnknownServiceError(
            f"unknown service {name!r} — valid names: {', '.join(service_names())}"
        )
    return service


def app_path(name: str) -> str:
    """The ``module:attr`` import path for a service's ``HyperApp``."""
    return get_service(name).app_path


def seed_path(name: str) -> str | None:
    """The ``module:function`` seed path for a service, or ``None``."""
    return get_service(name).seed_path


def launch_order(name: str) -> tuple[Service, ...]:
    """The service plus its companions, dependencies first, de-duplicated.

    Companion graphs are shallow by construction, but a depth-first walk with a
    visiting set means a future cycle degrades to "each service once" instead of
    recursing forever.
    """
    ordered: list[Service] = []
    seen: set[str] = set()

    def visit(target: str) -> None:
        if target in seen:
            return
        seen.add(target)
        service = get_service(target)
        for companion in service.companions:
            visit(companion)
        ordered.append(service)

    visit(name)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class PortConflict:
    """One pair of registry entries (or a registry entry and a reserved port)."""

    port: int
    holders: tuple[str, ...]


@dataclass(slots=True)
class RegistryAudit:
    """Result of :func:`audit_registry` — empty lists mean the registry is sound."""

    duplicate_ports: list[PortConflict] = field(default_factory=list)
    out_of_block_ports: list[PortConflict] = field(default_factory=list)
    missing_directories: list[str] = field(default_factory=list)
    unknown_companions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.duplicate_ports
            or self.out_of_block_ports
            or self.missing_directories
            or self.unknown_companions
        )


def audit_registry() -> RegistryAudit:
    """Self-check the registry: unique in-block ports, real dirs, real companions.

    Exposed as a function (not an import-time assertion) so a broken entry is
    reported by the test suite with full detail instead of exploding every
    ``hyper`` invocation at import.
    """
    audit = RegistryAudit()

    by_port: dict[int, list[str]] = {}
    for service in _REGISTRY:
        by_port.setdefault(service.port, []).append(service.name)
        # A launcher's extra ports are registry-declared too — audit them. A
        # value equal to the service's own port is the launcher being told
        # where to bind the main service, not a second claim on that port.
        for env in service.extra_env:
            if not (env.name.endswith("_PORT") and env.value.isdigit()):
                continue
            declared = int(env.value)
            if declared == service.port:
                continue
            by_port.setdefault(declared, []).append(f"{service.name}:{env.name}")

    for port, holders in sorted(by_port.items()):
        if len(holders) > 1:
            audit.duplicate_ports.append(PortConflict(port, tuple(holders)))
        if port not in SERVICE_PORT_BLOCK:
            audit.out_of_block_ports.append(PortConflict(port, tuple(holders)))

    for service in _REGISTRY:
        if not service.directory.is_dir():
            audit.missing_directories.append(service.name)
        for companion in service.companions:
            if companion not in SERVICES:
                audit.unknown_companions.append(f"{service.name} -> {companion}")
        for binding in service.companion_tokens:
            if binding.companion not in SERVICES:
                audit.unknown_companions.append(
                    f"{service.name} -> {binding.companion} (token binding)"
                )
        for url_binding in service.companion_urls:
            if url_binding.companion not in SERVICES:
                audit.unknown_companions.append(
                    f"{service.name} -> {url_binding.companion} (url binding)"
                )

    return audit
