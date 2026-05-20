"""Orchestration behind the ``hyper service`` CLI verb.

Takes one name from :mod:`hyperdjango.services_registry` and drives it from a
clean checkout to a serving process: native-extension gate, database resolution,
stable secret generation, ``hyper setup`` (+ seed), companion services first,
then the app itself — with every URL worth opening printed at the end and a
clean teardown on Ctrl-C.

Kept out of :mod:`hyperdjango.cli` so the common, DB-free ``hyper`` commands do
not pay for importing subprocess supervision machinery they never use; ``cli``
imports this lazily from ``cmd_service`` exactly like it does for the other
heavy subsystems.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from hyperdjango.conf import AUTO_RANDOM_SECRET_SETTINGS, resolve_database_url
from hyperdjango.logging import logger
from hyperdjango.services_registry import (
    SERVICE_PORT_BLOCK,
    Service,
    UnknownServiceError,
    get_service,
    launch_order,
    service_names,
)

# Same PID-file convention as ``hyper start`` / ``hyper stop`` / ``hyper status``,
# so a service launched here is visible to (and stoppable by) those commands too.
PID_FILE_TEMPLATE = ".hyper.{port}.pid"

# Demo credentials. Every seed routes user passwords through ``seed_password()``
# and the admin user through ``ensure_admin_user()``, both of which resolve these
# settings before falling back to a random value printed into the seed log.
# Pinning them makes the printed credentials stable across runs.
DEMO_CREDENTIAL_VARS = ("HYPER_SEED_PASSWORD", "HYPER_ADMIN_PASSWORD")

# Paths probed once a service is up; whatever answers below 400 gets printed.
# Cheaper and more honest than declaring mounts in the registry, where they
# would silently drift from the app code.
CANDIDATE_PATHS = (
    "/",
    "/health",
    "/ready",
    "/docs",
    "/openapi.json",
    "/admin/",
    "/version",
)

# Fallback when nothing configured a database at all. Matches the prefix
# ``services/live_config/run_mesh.py`` already assumes for its throwaway DBs.
DEFAULT_DB_PREFIX = "postgres://localhost/"

_READINESS_PATH = "/_ready"
_STARTUP_TIMEOUT_S = 90.0
_SETUP_TIMEOUT_S = 300.0
_STOP_GRACE_S = 8.0


class ServiceError(RuntimeError):
    """A failure with an actionable remediation already attached to the message."""


@dataclass(frozen=True, slots=True)
class ProbedUrl:
    """One reachable URL discovered after a service came up."""

    path: str
    status: int
    label: str


@dataclass(slots=True)
class RunningService:
    """A live service subprocess and the facts needed to talk to it."""

    service: Service
    process: subprocess.Popen
    port: int
    log_path: Path

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(slots=True)
class SecretResolution:
    """Outcome of resolving one service's secrets."""

    values: dict[str, str] = field(default_factory=dict)
    generated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    env_file: Path | None = None


# ── Preflight ────────────────────────────────────────────────────────────────


def ensure_native_extension() -> None:
    """Fail with the exact build command when the Zig extension is absent.

    Deliberately does NOT build: a multi-minute compile is not something a
    ``run`` verb should start behind the user's back.
    """
    try:
        # In-function import by necessity: this IS the probe. A module-level
        # import would raise at import time, before there is any handler to
        # turn ImportError into the actionable build instruction below — the
        # whole point of this function.
        import hyperdjango._hyperdjango_native  # noqa: F401
    except ImportError as exc:
        raise ServiceError(
            "native extension is not built — HyperDjango has no fallback path.\n"
            "    Build it:   uv run hyper-build --release\n"
            f"    Diagnose:   uv run hyper doctor --category build\n"
            f"    (import error: {exc})"
        ) from None


def service_database_url(service: Service) -> str:
    """Per-service database URL derived from the ambient one.

    Every service gets its own database so running one never drops another's
    tables. The database itself is NOT created here — ``hyper setup`` owns that
    DDL and already provisions a missing target via ``ensure_database_exists``.
    """
    base = resolve_database_url()
    if not base:
        return f"{DEFAULT_DB_PREFIX}{service.database_name}"
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=f"/{service.database_name}"))


def check_postgres_reachable(db_url: str) -> None:
    """TCP-preflight the database server so the failure names the fix.

    A refused connection here is the single most common first-run failure and
    the message ``hyper setup`` produces for it is about DDL, not about the
    server being down.
    """
    parsed = urlparse(db_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=3.0):
            return
    except OSError as exc:
        raise ServiceError(
            f"PostgreSQL is not accepting connections at {host}:{port} ({exc}).\n"
            "    Provision it:  make bootstrap-db\n"
            "    Or start it:   pg_ctl start   /   brew services start postgresql\n"
            "    Then verify:   uv run hyper doctor --category database"
        ) from None


# ── Secrets ──────────────────────────────────────────────────────────────────


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` file. Absent file means an empty mapping."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Persist secrets 0600, sorted, with a header explaining the file."""
    body = [
        "# Generated by `hyper service run` — gitignored, safe to delete.",
        "# Regenerating produces NEW secrets, which invalidates any data seeded",
        "# with the old ones; re-run with --fresh after deleting this file.",
    ]
    body.extend(f"{key}={values[key]}" for key in sorted(values))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the start — a chmod after the
    # write leaves a window where the secrets are world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(body) + "\n")


def resolve_secrets(service: Service) -> SecretResolution:
    """Load, generate and persist a service's secrets — stable across runs.

    Values already present in the process environment win (an operator override
    is authoritative). Everything else is read from, or minted into, the
    service's gitignored ``.env.local`` so ``hyper setup`` and the server that
    follows it sign with identical keys.

    Beyond the registry's declared secrets, every setting in
    :data:`hyperdjango.conf.AUTO_RANDOM_SECRET_SETTINGS` is minted here too.
    Those settings default to a FRESH RANDOM VALUE PER PROCESS, so without this
    each restart — a ``hyper service run`` re-run, or a systemd restart of an
    installed unit — would silently invalidate every session cookie, CSRF token
    and signed value the previous process handed out.
    """
    resolution = SecretResolution()
    path = service.env_file
    resolution.env_file = path
    stored = read_env_file(path)
    dirty = False

    wanted: list[tuple[str, int]] = [
        (s.env_var, s.min_length) for s in service.generated_secrets
    ]
    declared = {s.env_var for s in service.secrets}
    wanted.extend(
        (f"HYPER_{name}", 32)
        for name in sorted(AUTO_RANDOM_SECRET_SETTINGS)
        if f"HYPER_{name}" not in declared
    )
    if service.seed_path is not None:
        wanted.extend((name, 16) for name in DEMO_CREDENTIAL_VARS)

    for env_var, min_length in wanted:
        # Asks "did the OPERATOR export this?" — a different question from
        # get_setting()'s "what value applies?", which would hand back an
        # auto-generated per-process secret and always answer yes.
        # env-boundary: operator-supplied detection, not a config read.
        ambient = os.environ.get(env_var, "")
        if len(ambient) >= min_length:
            resolution.values[env_var] = ambient
            continue
        existing = stored.get(env_var, "")
        if len(existing) >= min_length:
            resolution.values[env_var] = existing
            continue
        # token_urlsafe(n) yields ~1.3n characters, so 32 bytes clears every
        # min_length in the registry with room to spare.
        minted = secrets.token_urlsafe(max(32, min_length))
        stored[env_var] = minted
        resolution.values[env_var] = minted
        resolution.generated.append(env_var)
        dirty = True

    for requirement in service.supplied_secrets:
        # Same operator-supplied-vs-resolved distinction as above: an
        # operator export must outrank the persisted file.
        # env-boundary: operator-supplied detection, not a config read.
        value = os.environ.get(requirement.env_var, "") or stored.get(
            requirement.env_var, ""
        )
        if len(value) >= requirement.min_length:
            resolution.values[requirement.env_var] = value
        else:
            resolution.missing.append(requirement.env_var)

    if dirty:
        write_env_file(path, stored)

    return resolution


# ── Environment composition ──────────────────────────────────────────────────


def compose_env(
    service: Service,
    *,
    port: int,
    secrets_values: dict[str, str],
    companion_tokens: dict[str, str],
) -> dict[str, str]:
    """Build the child environment for one service's setup and server processes.

    Both processes get the SAME mapping — that is the whole point: a seed that
    signs tokens with one key and a server that verifies with another is the
    single most confusing failure mode this command exists to remove.
    """
    # Builds the child process's environment block: inherit, then override.
    # env-boundary: subprocess plumbing, not a configuration read.
    env = os.environ.copy()
    env["DATABASE_URL"] = service_database_url(service)
    # HYPER_DATABASE_URL outranks DATABASE_URL in resolve_database_url(); clear
    # any ambient one so it cannot override the per-service database.
    env.pop("HYPER_DATABASE_URL", None)
    env["HYPER_PORT"] = str(port)
    env.update(secrets_values)
    env.update(companion_tokens)
    for entry in service.resolved_env():
        env.setdefault(entry.name, entry.value)
    return env


def companion_token_env(service: Service) -> dict[str, str]:
    """Read companion-minted tokens out of the seeded ``tokens.json`` files."""
    resolved: dict[str, str] = {}
    for binding in service.companion_tokens:
        companion = get_service(binding.companion)
        tokens_path = companion.tokens_path
        if not tokens_path.is_file():
            raise ServiceError(
                f"{service.name} needs the {binding.token_key!r} token minted by "
                f"{companion.name}'s seed, but {tokens_path} does not exist.\n"
                f"    Re-seed the companion:  uv run hyper service run "
                f"{service.name} --fresh"
            )
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        token = tokens.get(binding.token_key, "")
        if not token:
            raise ServiceError(
                f"{companion.name} minted no {binding.token_key!r} identity "
                f"(looked in {tokens_path})."
            )
        resolved[binding.env_var] = token
    return resolved


def companion_url_env(
    service: Service, running: dict[str, RunningService]
) -> dict[str, str]:
    """Fill each declared :class:`CompanionUrl` from the port that companion bound.

    Resolved from the live process rather than from the registry's declared port
    so a ``--port`` override or a launcher-chosen port still produces a correct
    URL.
    """
    resolved: dict[str, str] = {}
    for binding in service.companion_urls:
        live = running.get(binding.companion)
        if live is not None:
            resolved[binding.env_var] = live.base_url
    return resolved


# ── Process lifecycle ────────────────────────────────────────────────────────


def _pid_path(port: int) -> Path:
    return Path(PID_FILE_TEMPLATE.format(port=port))


def _log_path(service: Service, port: int) -> Path:
    return Path(f".hyper.service.{service.name}.{port}.log")


def run_setup(service: Service, env: dict[str, str], *, fresh: bool, seed: bool):
    """Create tables (and optionally seed) via the platform's own DDL authority.

    Shells out to ``hyper setup`` rather than calling ``cmd_setup`` in-process so
    the child gets the composed environment cleanly and the user sees exactly the
    command they could have typed themselves.
    """
    cmd = [sys.executable, "-m", "hyperdjango.cli", "setup", "--app", service.app_path]
    if fresh:
        cmd.append("--drop")
    if seed and service.seed_path:
        cmd.extend(["--seed", service.seed_path])
    logger.info("  $ {cmd}", cmd=" ".join(cmd[2:]))
    result = subprocess.run(
        cmd,
        env=env,
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=_SETUP_TIMEOUT_S,
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-25:])
        raise ServiceError(
            f"`hyper setup` failed for {service.name} (exit {result.returncode}).\n"
            f"{tail}\n"
            "    If the database server is down:  make bootstrap-db\n"
            "    For a broader diagnosis:         uv run hyper doctor"
        )
    return result.stdout + result.stderr


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def check_port_free(service: Service, port: int) -> None:
    """Refuse to start when something already holds the port.

    Without this the readiness poll can be satisfied by a STRANGER — a stale
    server from a previous run answers ``/_ready`` on the same port, the launch
    is reported as successful, and the process actually started dies unnoticed
    with EADDRINUSE. Detect the occupied port up front and say who to stop.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
    except OSError:
        return
    raise ServiceError(
        f"port {port} is already in use, so {service.name} cannot bind it.\n"
        f"    Stop a service left running here:  uv run hyper service stop "
        f"{service.name}\n"
        f"    Or stop any HyperDjango server:     uv run hyper stop --port {port}\n"
        f"    Or pick another port:               uv run hyper service run "
        f"{service.name} --port <N>"
    )


def start_service(service: Service, env: dict[str, str], port: int) -> RunningService:
    """Launch one service server and block until it reports ready."""
    check_port_free(service, port)
    script = (
        f"import {service.module} as _m; "
        f"_m.{service.attribute}.run(host='127.0.0.1', port={port})"
    )
    log_path = _log_path(service, port)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        cwd=_repo_root(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _pid_path(port).write_text(str(process.pid), encoding="utf-8")
    running = RunningService(service, process, port, log_path)
    try:
        _await_ready(running)
    except ServiceError:
        stop_service(running)
        raise
    return running


def _await_ready(running: RunningService) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    url = running.base_url + _READINESS_PATH
    while time.monotonic() < deadline:
        if running.process.poll() is not None:
            tail = _tail(running.log_path)
            raise ServiceError(
                f"{running.service.name} exited during startup "
                f"(code {running.process.returncode}).\n{tail}\n"
                f"    Full log: {running.log_path.resolve()}"
            )
        status = _probe(url)
        if status is not None and status < 400:
            return
        time.sleep(0.2)
    tail = _tail(running.log_path)
    raise ServiceError(
        f"{running.service.name} did not become ready within "
        f"{_STARTUP_TIMEOUT_S:.0f}s on port {running.port}.\n{tail}\n"
        f"    Full log: {running.log_path.resolve()}"
    )


def _tail(path: Path, lines: int = 25) -> str:
    if not path.is_file():
        return "    (no log output)"
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return "\n".join(f"    {line}" for line in tail)


def _probe(url: str, timeout: float = 2.0) -> int | None:
    """HTTP status for ``url``, or ``None`` when the connection failed.

    An HTTP error response is still an answer — a 401 on ``/admin/`` proves the
    route exists — so ``HTTPError`` contributes its code rather than being
    treated as unreachable.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError, OSError, ValueError:
        return None


def probe_urls(running: RunningService) -> tuple[ProbedUrl, ...]:
    """Discover which of the well-known paths this service actually serves."""
    found: list[ProbedUrl] = []
    labels = {
        "/": "app root",
        "/health": "liveness probe",
        "/ready": "readiness probe",
        "/docs": "Swagger UI",
        "/openapi.json": "OpenAPI spec",
        "/admin/": "HyperAdmin panel",
        "/version": "version endpoint",
    }
    for path in CANDIDATE_PATHS:
        status = _probe(running.base_url + path)
        if status is not None and status < 400:
            found.append(ProbedUrl(path, status, labels[path]))
    return tuple(found)


def stop_service(running: RunningService) -> None:
    """SIGTERM the process group, escalate to SIGKILL, then clear the PID file."""
    process = running.process
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError, PermissionError, OSError:
            process.terminate()
        try:
            process.wait(timeout=_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError, PermissionError, OSError:
                process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "{name}: process {pid} did not exit after SIGKILL",
                    name=running.service.name,
                    pid=process.pid,
                )
    _pid_path(running.port).unlink(missing_ok=True)


# ── Seed credential reporting ────────────────────────────────────────────────


def extract_credentials(setup_output: str) -> tuple[str, ...]:
    """Pull the credential lines a seed printed out of its output.

    Seeds surface generated passwords through ``seed_password()`` /
    ``ensure_admin_user()`` log lines; echoing them back means the user never has
    to go digging through scrollback for the login they need.
    """
    interesting = ("password", "token", "api key", "credential")
    lines = []
    for raw in setup_output.splitlines():
        lowered = raw.lower()
        if any(word in lowered for word in interesting):
            lines.append(raw.strip())
    return tuple(dict.fromkeys(lines))


# ── Commands ─────────────────────────────────────────────────────────────────


def _out(message: str = "") -> None:
    """Plain stdout — tables and URL lists are data, not log records.

    Flushed unconditionally: ``hyper service run`` is routinely redirected to a
    file or a CI log, where block-buffered stdout would hold the URL report
    hostage until the process exits — which, for a foreground server, is never.
    """
    print(message, flush=True)


def cmd_list() -> int:
    """Tabulate every registered service."""
    names = service_names()
    width = max(len(n) for n in names)
    _out(
        f"{len(names)} bundled services (ports {SERVICE_PORT_BLOCK.start}-"
        f"{SERVICE_PORT_BLOCK.stop - 1}):"
    )
    _out()
    needs_width = 20
    _out(f"  {'NAME'.ljust(width)}  PORT  {'NEEDS'.ljust(needs_width)}  DEMONSTRATES")
    _out(f"  {'-' * width}  ----  {'-' * needs_width}  ------------")
    for name in names:
        service = get_service(name)
        needs = []
        if service.needs_database:
            needs.append("db")
        if service.secrets:
            needs.append("secrets")
        if service.companions:
            needs.append("companion")
        _out(
            f"  {name.ljust(width)}  {service.port}  "
            f"{(','.join(needs) or '-').ljust(needs_width)}  {service.description}"
        )
    _out()
    _out("  uv run hyper service info <name>   full detail + manual commands")
    _out("  uv run hyper service run <name>    setup, seed and serve")
    return 0


def cmd_info(name: str) -> int:
    """Print everything needed to run one service, manual commands included."""
    service = get_service(name)
    order = launch_order(name)

    _out(f"{service.name} — {service.description}")
    _out()
    _out(f"  directory     {service.directory}")
    _out(f"  app           {service.app_path}")
    _out(f"  seed          {service.seed_path or '(none)'}")
    _out(f"  port          {service.port}")
    _out(
        f"  database      {service.database_name if service.needs_database else '(none)'}"
    )
    _out(f"  tags          {', '.join(service.tags)}")
    if service.launcher:
        _out(f"  launcher      {service.launcher} (owns the whole mesh)")
    if service.companions:
        _out(f"  companions    {', '.join(service.companions)}")
    if service.secrets:
        _out("  secrets")
        for requirement in service.secrets:
            origin = (
                "generated + persisted" if requirement.generated else "YOU MUST SET"
            )
            _out(
                f"      {requirement.env_var} (>={requirement.min_length} chars, "
                f"{origin})"
            )
            _out(f"          {requirement.purpose}")
    if service.extra_env:
        _out("  extra env")
        for entry in service.resolved_env():
            _out(f"      {entry.name}={entry.value}")
    if service.notes:
        _out()
        _out(f"  note: {service.notes}")

    _out()
    _out("Equivalent manual commands:")
    _out()
    if service.launcher:
        _out(f"  uv run python -m {service.launcher}")
    else:
        for step in order:
            db = f"DATABASE_URL={service_database_url(step)}"
            # Registry-declared environment belongs on BOTH commands: it is what
            # redirects a seed's minted tokens to the path the wiring below reads
            # them from. Omit it and the seed writes somewhere else entirely.
            extra = [f"{entry.name}={entry.value}" for entry in step.resolved_env()]
            if step.secrets or step.seed_path:
                # Sibling services reuse the same variable NAMES with different
                # values, so each one's secrets must be loaded in its own shell.
                _out(f"  # {step.name}: load its own secrets first (subshell!) —")
                _out(f"  #   ( set -a; . {step.env_file}; set +a")
            if step.needs_database:
                seed = f" --seed {step.seed_path}" if step.seed_path else ""
                setup_prefix = " ".join([db, *extra])
                _out(
                    f"  {setup_prefix} uv run hyper setup --app {step.app_path} "
                    f"--drop{seed}"
                )
            wiring = []
            for url_binding in step.companion_urls:
                companion = get_service(url_binding.companion)
                wiring.append(
                    f"{url_binding.env_var}=http://127.0.0.1:{companion.port}"
                )
            for token_binding in step.companion_tokens:
                companion = get_service(token_binding.companion)
                wiring.append(
                    f"{token_binding.env_var}="
                    f"$(jq -r '.\"{token_binding.token_key}\"' "
                    f"{companion.tokens_path})"
                )
            prefix = " ".join([db, *extra, *wiring])
            _out(
                f"  {prefix} uv run hyper start --app {step.app_path} "
                f"--port {step.port}"
            )
            if step.secrets or step.seed_path:
                _out("  #   )")
    _out()
    _out(f"  ...or just:  uv run hyper service run {service.name}")
    return 0


def _state_path(service: Service) -> Path:
    """Where a run records the ports it ACTUALLY bound (gitignored)."""
    return service.runtime_dir / "running.json"


def write_run_state(service: Service, ports: dict[str, int]) -> None:
    """Record the ports a run bound so ``stop`` can find them again.

    The registry port is only a DEFAULT — ``--port`` overrides it — so a stop
    that looked only at the registry would silently fail to find a service
    started on an overridden port.
    """
    service.runtime_dir.mkdir(parents=True, exist_ok=True)
    _state_path(service).write_text(json.dumps(ports, indent=2), encoding="utf-8")


def read_run_state(service: Service) -> dict[str, int]:
    """Ports recorded by the last run, or ``{}`` when there is no record."""
    path = _state_path(service)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError, OSError:
        return {}
    return {str(k): int(v) for k, v in loaded.items()}


def cmd_stop(name: str) -> int:
    """Stop a service and its companions.

    Ports come from the last run's recorded state when available, falling back
    to the registry defaults — so ``--port`` overrides and plain runs both stop.
    """
    order = launch_order(name)
    recorded = read_run_state(get_service(name))
    stopped = 0
    for service in order:
        port = recorded.get(service.name, service.port)
        path = _pid_path(port)
        if not path.is_file():
            continue
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            path.unlink(missing_ok=True)
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            logger.success(
                "Stopped {name} (PID {pid}, port {port})",
                name=service.name,
                pid=pid,
                port=port,
            )
            stopped += 1
        except ProcessLookupError, PermissionError, OSError:
            logger.info(
                "{name}: no live process for PID {pid} — clearing stale PID file",
                name=service.name,
                pid=pid,
            )
        path.unlink(missing_ok=True)
    _state_path(get_service(name)).unlink(missing_ok=True)
    if stopped == 0:
        logger.info("Nothing running for {name}", name=name)
        return 1
    return 0


def _run_launcher(service: Service, secrets_values: dict[str, str]) -> int:
    """Hand a mesh service to the launcher that already owns its wiring."""
    env = compose_env(
        service, port=service.port, secrets_values=secrets_values, companion_tokens={}
    )
    logger.info(
        "{name} is a {n}-service mesh — delegating to {launcher}",
        name=service.name,
        n=len(service.companions) + 1,
        launcher=service.launcher,
    )
    _out(f"  $ uv run python -m {service.launcher}")
    result = subprocess.run(
        [sys.executable, "-m", service.launcher], env=env, cwd=_repo_root()
    )
    return result.returncode


def cmd_run(name: str, *, port: int | None, no_seed: bool, fresh: bool) -> int:
    """From nothing to serving — the one command."""
    service = get_service(name)

    logger.info("Checking native extension...")
    ensure_native_extension()
    logger.success("  native extension present")

    order = launch_order(name)

    # 1. Secrets for every service in the launch order, before anything starts.
    resolutions: dict[str, SecretResolution] = {}
    for step in order:
        resolution = resolve_secrets(step)
        resolutions[step.name] = resolution
        if resolution.missing:
            raise ServiceError(
                f"{step.name} needs {', '.join(resolution.missing)} and no random "
                "value can substitute for it (external service credential).\n"
                f"    Export it, or add it to {step.env_file}, then re-run.\n"
                f"    See {step.directory / 'README.md'} for what it is."
            )
        if resolution.generated:
            logger.success(
                "  {name}: generated {vars} -> {path}",
                name=step.name,
                vars=", ".join(resolution.generated),
                path=resolution.env_file,
            )
        elif resolution.env_file is not None:
            logger.info(
                "  {name}: reusing secrets from {path}",
                name=step.name,
                path=resolution.env_file,
            )

    if service.launcher:
        return _run_launcher(service, resolutions[service.name].values)

    # 2. Database preflight once, against the service's own resolved URL.
    if any(step.needs_database for step in order):
        check_postgres_reachable(service_database_url(service))
        logger.success("  PostgreSQL reachable")

    ports = {step.name: step.port for step in order}
    if port is not None:
        ports[service.name] = port
    for step in order:
        step.runtime_dir.mkdir(parents=True, exist_ok=True)

    running: dict[str, RunningService] = {}
    credentials: list[str] = []
    try:
        for step in order:
            logger.info("── {name} ─────────────────", name=step.name)
            # Companions are launched first, so by the time a dependent is
            # reached its companion's seed has already written tokens.json and
            # its server is already bound — both resolve here, once, and the
            # SAME env goes to `hyper setup` and to the server process.
            token_env = companion_token_env(step)
            token_env.update(companion_url_env(step, running))
            env = compose_env(
                step,
                port=ports[step.name],
                secrets_values=resolutions[step.name].values,
                companion_tokens=token_env,
            )
            if step.needs_database:
                logger.info(
                    "  creating tables{seed}...",
                    seed="" if no_seed or not step.seed_path else " + seeding",
                )
                output = run_setup(
                    step,
                    env,
                    fresh=fresh or needs_initial_setup(step),
                    seed=not no_seed,
                )
                credentials.extend(extract_credentials(output))
                logger.success("  database ready ({db})", db=step.database_name)
                # The seed may have just minted companion tokens; re-resolve so
                # a dependent started later reads the fresh values.
                token_env = companion_token_env(step) if running else {}
                token_env.update(companion_url_env(step, running))
                env = compose_env(
                    step,
                    port=ports[step.name],
                    secrets_values=resolutions[step.name].values,
                    companion_tokens=token_env,
                )
            logger.info("  starting on port {port}...", port=ports[step.name])
            running[step.name] = start_service(step, env, ports[step.name])
            logger.success("  {name} is serving", name=step.name)

        write_run_state(service, {n: live.port for n, live in running.items()})
        _report(service, running, credentials, resolutions)
        supervise(running)
    finally:
        for step in reversed(order):
            live = running.get(step.name)
            if live is not None:
                logger.info("Stopping {name}...", name=step.name)
                stop_service(live)
        _state_path(service).unlink(missing_ok=True)
        logger.success("All processes stopped.")
    return 0


def needs_initial_setup(service: Service) -> bool:
    """True when this service has never been set up in this checkout.

    A first run drops so the tables match the current models; later runs keep
    the data unless the user asks for ``--fresh``.
    """
    return not service.runtime_dir.joinpath(".setup-done").exists()


def mark_setup_complete(service: Service) -> None:
    service.runtime_dir.mkdir(parents=True, exist_ok=True)
    service.runtime_dir.joinpath(".setup-done").write_text("", encoding="utf-8")


def _report(
    service: Service,
    running: dict[str, RunningService],
    credentials: list[str],
    resolutions: dict[str, SecretResolution],
) -> None:
    """Print every URL worth opening and every credential the seed produced."""
    _out()
    _out("=" * 66)
    _out(f"  {service.name} is running")
    _out("=" * 66)
    for name, live in running.items():
        mark_setup_complete(live.service)
        role = "main app" if name == service.name else "companion"
        _out()
        _out(f"  {name}  ({role})  {live.base_url}")
        for url in probe_urls(live):
            _out(f"      {(live.base_url + url.path).ljust(38)} {url.label}")
        _out(f"      log: {live.log_path.resolve()}")
    # Pinned demo credentials are reported directly rather than scraped: when
    # HYPER_SEED_PASSWORD is set (which this command always does), the seed
    # takes it silently and prints nothing, so scraping alone would leave the
    # user with a login they cannot guess.
    demo: dict[str, str] = {}
    for name in running:
        for var in DEMO_CREDENTIAL_VARS:
            value = resolutions[name].values.get(var, "")
            if value:
                demo[var] = value
    if demo:
        _out()
        _out("  Demo credentials (same for every seeded user):")
        for var, value in sorted(demo.items()):
            _out(f"      {var.removeprefix('HYPER_').ljust(16)} {value}")
        _out("      admin panel: user 'admin' + HYPER_ADMIN_PASSWORD above")
    if credentials:
        _out()
        _out("  Additional credentials printed by the seed:")
        for line in credentials:
            _out(f"      {line}")
    files = [str(r.env_file) for r in resolutions.values() if r.env_file is not None]
    if files:
        _out()
        _out("  Generated secrets persisted to:")
        for path in files:
            _out(f"      {path}")
    _out()
    _out("  Press Ctrl-C to stop everything.")
    _out("=" * 66)
    _out()


def supervise(running: dict[str, RunningService]) -> None:
    """Block until a shutdown signal **or until any supervised process exits**.

    Watching the children (rather than sleeping on Ctrl-C alone) is what makes
    this a supervisor instead of a timer. It covers both directions of the same
    problem: a ``hyper service stop`` issued from another terminal, and a
    service that dies on its own at runtime. Either way the remaining processes
    are torn down by the caller's ``finally`` instead of being left orphaned
    behind a parent that is asleep with nothing left to supervise.

    SIGINT and SIGTERM are handled EXPLICITLY rather than relying on Python's
    default ``KeyboardInterrupt``: this process has imported the native
    extension (the build gate does), which installs its own signal handlers, so
    the default disposition is not ours to assume. Registering our own also
    means ``hyper service run`` answers SIGTERM — what systemd, ``docker stop``
    and CI timeouts actually send — and not only Ctrl-C.
    """
    stop = threading.Event()
    received: list[int] = []

    def _handle(signum: int, _frame) -> None:
        received.append(signum)
        stop.set()

    previous = {
        sig: signal.signal(sig, _handle) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while not stop.is_set():
            for name, live in running.items():
                if live.process.poll() is None:
                    continue
                _out()
                logger.warning(
                    "{name} exited (code {code}) — shutting the rest down.",
                    name=name,
                    code=live.process.returncode,
                )
                logger.info("  log: {path}", path=live.log_path.resolve())
                return
            # Short wait, not sleep: a signal ends it immediately instead of
            # after the remainder of a poll interval.
            stop.wait(0.5)
        _out()
        logger.info(
            "Received {sig} — shutting down.",
            sig=signal.Signals(received[0]).name if received else "shutdown",
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def dispatch(args) -> int:
    """Route ``hyper service <verb>``. Unknown names fail loudly with the list."""
    verb = args.service_command
    try:
        if verb == "list":
            return cmd_list()
        if verb == "info":
            return cmd_info(args.name)
        if verb == "stop":
            return cmd_stop(args.name)
        if verb == "run":
            return cmd_run(
                args.name, port=args.port, no_seed=args.no_seed, fresh=args.fresh
            )
    except UnknownServiceError as exc:
        logger.error(str(exc))
        return 2
    except ServiceError as exc:
        logger.error(str(exc))
        return 1
    logger.error("Usage: hyper service {list|info|run|stop|install|uninstall} [name]")
    return 2


__all__ = [
    "ServiceError",
    "ProbedUrl",
    "RunningService",
    "SecretResolution",
    "cmd_info",
    "cmd_list",
    "cmd_run",
    "cmd_stop",
    "compose_env",
    "dispatch",
    "ensure_native_extension",
    "mark_setup_complete",
    "needs_initial_setup",
    "service_database_url",
    "extract_credentials",
    "read_env_file",
    "read_run_state",
    "resolve_secrets",
    "supervise",
    "write_env_file",
    "write_run_state",
]
