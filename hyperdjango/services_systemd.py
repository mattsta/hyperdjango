"""Registry-driven systemd units for the bundled services.

``hyper service install <name>`` turns one registry entry into a real,
administered system service: a unit file whose every field — description, port,
``ExecStart``, working directory, environment — is derived from
:mod:`hyperdjango.services_registry`, and an ``EnvironmentFile`` holding exactly
the environment :func:`hyperdjango.services_runner.cmd_run` would have composed,
built from the SAME persisted per-service secrets. Nothing is retyped by hand,
so a unit can never drift from the registry, and a service installed here signs
with the same keys its seed used.

Four things this module is deliberate about
-------------------------------------------

**Companions are real dependencies, not a launch order.** A service with
companions gets one unit per companion. The parent carries
``Requires=``/``After=`` on each companion so starting the parent starts them
first; the companion carries ``PartOf=<parent>`` so stopping or restarting the
parent stops or restarts it too. Both directions are needed: ``Requires=``
alone propagates stop only from companion to parent, ``PartOf=`` alone gives no
start ordering.

**The PostgreSQL dependency is honest.** ``After=postgresql.service`` is not a
readiness guarantee. It orders start *jobs*, and on Debian/Ubuntu that unit is
literally ``Type=oneshot`` with ``ExecStart=/bin/true`` — a meta unit that pulls
in the per-cluster ``postgresql@<version>-main.service`` instances, which are
themselves ``Type=forking`` (started == the parent forked, not == accepting
connections). A remote database has no local unit at all. So the ordering is
derived from what is actually installed on the host
(:func:`detect_postgres_unit`), and the real gate is an ``ExecStartPre`` running
``pg_isready`` against the host and port resolved from the service's own
``DATABASE_URL`` — paired with ``RestartSec``/``StartLimitIntervalSec``/
``StartLimitBurst`` so a boot-time race retries into success instead of
tripping systemd's start limit.

**These units are restart-only.** The native server installs handlers for
SIGTERM and SIGINT (``install_signal_handlers`` in ``zig/src/server.zig``) and
nothing else; there is no SIGHUP handler and no configuration-reload path
anywhere in the framework. Emitting an ``ExecReload`` that sent SIGHUP would
either do nothing or kill the process on the default disposition, so no
``ExecReload`` is emitted at all: ``systemctl reload`` fails loudly with
"Operation refused", which is the truth, and ``systemctl restart`` is the
supported way to pick up new configuration.

**Grouping is explicit.** Every unit is ``PartOf=hyperdjango.target`` and
``WantedBy=hyperdjango.target multi-user.target``, so one target starts, stops
and restarts every installed service while each is still independently enabled
for boot.
"""

import getpass
import grp
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from hyperdjango.conf import AUTO_RANDOM_SECRET_SETTINGS, get_setting
from hyperdjango.logging import logger
from hyperdjango.services_registry import (
    SERVICES,
    Service,
    UnknownServiceError,
    get_service,
    launch_order,
)
from hyperdjango.services_runner import (
    DEMO_CREDENTIAL_VARS,
    ServiceError,
    check_postgres_reachable,
    companion_token_env,
    compose_env,
    mark_setup_complete,
    needs_initial_setup,
    read_env_file,
    resolve_secrets,
    run_setup,
    service_database_url,
)

# One unit per service, one target for the set. Spelled once so the installer,
# the uninstaller and the tests agree.
UNIT_PREFIX = "hyperdjango-"
TARGET_NAME = "hyperdjango.target"

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
SYSTEM_ENV_DIR = Path("/etc/hyperdjango")

# `hyper run --prod` refuses to boot without a signing key (fail-closed), so an
# installed unit always carries one. It, and every other per-process-random
# secret, is minted and persisted by resolve_secrets — see stable_secret_vars().
SECRET_KEY_VAR = "HYPER_SECRET_KEY"

# Boot-race budget: 30 attempts, 5 s apart, inside a 10-minute window. A
# PostgreSQL that is slow to accept connections at boot is retried into success;
# a genuinely broken service still gives up instead of restarting forever.
RESTART_SEC = 5
START_LIMIT_INTERVAL_SEC = 600
START_LIMIT_BURST = 30

# Hostnames that mean "the database is on this machine", and therefore that a
# local PostgreSQL unit is a legitimate ordering dependency.
LOCAL_DB_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "ip6-localhost"})

_REPO_ROOT = Path(__file__).resolve().parent.parent


class SystemdError(ServiceError):
    """A systemd install/uninstall failure with the remediation attached."""


# ── Scope: system units vs. `systemctl --user` units ─────────────────────────


@dataclass(frozen=True, slots=True)
class SystemdScope:
    """Where units live and how systemctl is invoked for them.

    ``prefix`` is a staging root in the ``DESTDIR`` sense: the unit and
    environment directories are rooted under it instead of at ``/``. Nothing on
    the CLI sets it — it exists so a caller assembling a container image (and
    the test suite) can render a complete install to a directory without root
    and without a running systemd.
    """

    user_mode: bool
    unit_dir: Path
    env_dir: Path
    prefix: Path | None = None

    @classmethod
    def resolve(cls, *, user_mode: bool, prefix: Path | None = None) -> SystemdScope:
        if user_mode:
            unit_dir = Path.home() / ".config" / "systemd" / "user"
            env_dir = Path.home() / ".config" / "hyperdjango"
        else:
            unit_dir = SYSTEM_UNIT_DIR
            env_dir = SYSTEM_ENV_DIR
        if prefix is not None:
            unit_dir = prefix / unit_dir.relative_to(unit_dir.anchor)
            env_dir = prefix / env_dir.relative_to(env_dir.anchor)
        return cls(
            user_mode=user_mode, unit_dir=unit_dir, env_dir=env_dir, prefix=prefix
        )

    @property
    def systemctl(self) -> tuple[str, ...]:
        return ("systemctl", "--user") if self.user_mode else ("systemctl",)

    @property
    def journalctl(self) -> tuple[str, ...]:
        return ("journalctl", "--user") if self.user_mode else ("journalctl",)

    @property
    def label(self) -> str:
        return "user" if self.user_mode else "system"

    @property
    def boot_target(self) -> str:
        """The target that pulls units in at boot for this scope.

        A ``--user`` manager has no ``multi-user.target``; its equivalent is
        ``default.target``, and it only runs at all once the user has a session
        (or lingering is enabled — see the docs).
        """
        return "default.target" if self.user_mode else "multi-user.target"


# ── PostgreSQL dependency detection ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PostgresGate:
    """How one unit depends on PostgreSQL.

    ``unit`` is the local systemd unit to order after (``None`` for a remote
    database, or a host with no PostgreSQL unit installed). ``pg_isready`` is
    the absolute path to the readiness binary, or ``None`` when it is not
    installed — in which case no gate is emitted and ``reason`` says so.
    """

    host: str
    port: int
    unit: str | None
    pg_isready: str | None
    reason: str

    @property
    def exec_start_pre(self) -> str | None:
        """The ``ExecStartPre`` line, or ``None`` when no gate is possible."""
        if self.pg_isready is None:
            return None
        return (
            f"{self.pg_isready} --host={self.host} --port={self.port} "
            f"--timeout={RESTART_SEC}"
        )


def parse_unit_names(listing: str) -> tuple[str, ...]:
    """Unit names out of ``systemctl list-unit-files``/``list-units`` output.

    Both commands put the unit name in the first whitespace-separated column
    (with ``--no-legend``); ``list-units`` may prefix a status bullet. Takes the
    raw text so the detection below is testable with synthetic input instead of
    a live systemctl.
    """
    names: list[str] = []
    for raw in listing.splitlines():
        line = raw.strip()
        if not line:
            continue
        # `systemctl list-units` marks failed/not-found units with a leading
        # bullet ("● name.service loaded ..."); drop it before the split.
        if line[0] in "●*✔✗x":
            line = line[1:].strip()
        name = line.split()[0] if line.split() else ""
        if name.endswith(".service"):
            names.append(name)
    return tuple(dict.fromkeys(names))


def detect_postgres_unit(names: tuple[str, ...]) -> str | None:
    """Pick the PostgreSQL unit to order after, or ``None`` when there is none.

    Preference order, and why:

    1. ``postgresql.service`` — on Debian/Ubuntu this is the meta-unit that
       pulls in every configured cluster, so it is the right thing to depend on
       when it exists;
    2. the highest-numbered ``postgresql@<version>-<cluster>.service`` instance
       — what you get on a host where only the per-cluster unit is present;
    3. any other ``postgres*`` service.

    Template units (``postgresql@.service``) are never selected: a template has
    no instance to start, so a dependency on one is a unit that fails to load.
    """
    concrete = tuple(n for n in names if not n.endswith("@.service"))
    if "postgresql.service" in concrete:
        return "postgresql.service"
    instances = sorted(
        (n for n in concrete if n.startswith("postgresql@")),
        key=_instance_sort_key,
        reverse=True,
    )
    if instances:
        return instances[0]
    others = sorted(n for n in concrete if n.startswith("postgres"))
    return others[0] if others else None


def _instance_sort_key(unit: str) -> tuple[int, str]:
    """Sort ``postgresql@18-main.service`` above ``postgresql@16-main.service``."""
    instance = unit.removeprefix("postgresql@")
    version = instance.split("-", 1)[0]
    return (int(version) if version.isdigit() else 0, unit)


def host_is_local(host: str) -> bool:
    """True when the database lives on this machine.

    A remote database has no local unit to order after — depending on one would
    either fail to load or silently order against an unrelated server.
    """
    return host.lower() in LOCAL_DB_HOSTS or host.startswith("/")


def postgres_gate(database_url: str, *, unit_names: tuple[str, ...]) -> PostgresGate:
    """Resolve the PostgreSQL dependency for one service's database URL."""
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    ready = shutil.which("pg_isready")
    if not host_is_local(host):
        return PostgresGate(
            host=host,
            port=port,
            unit=None,
            pg_isready=ready,
            reason=(
                f"{host} is not this machine — no local unit to order after; "
                "the ExecStartPre readiness gate is the whole dependency"
            ),
        )
    unit = detect_postgres_unit(unit_names)
    if unit is None:
        return PostgresGate(
            host=host,
            port=port,
            unit=None,
            pg_isready=ready,
            reason=(
                "no PostgreSQL unit is installed on this host — ordering is "
                "skipped and the readiness gate carries the dependency"
            ),
        )
    return PostgresGate(
        host=host,
        port=port,
        unit=unit,
        pg_isready=ready,
        reason=f"detected {unit} on this host",
    )


def no_database_gate() -> PostgresGate:
    """The gate for a service the registry marks ``needs_database=False``."""
    return PostgresGate(
        host="",
        port=0,
        unit=None,
        pg_isready=None,
        reason="service declares needs_database=False — no database dependency",
    )


def query_unit_names(scope: SystemdScope) -> tuple[str, ...]:
    """Every service unit systemd knows about, or ``()`` when it cannot be asked."""
    if not systemctl_available():
        return ()
    result = subprocess.run(
        [*scope.systemctl, "list-unit-files", "--type=service", "--no-legend"],
        capture_output=True,
        text=True,
    )
    return parse_unit_names(result.stdout)


# ── Platform + privilege gates ───────────────────────────────────────────────


def systemctl_available() -> bool:
    """systemd exists only on Linux; anything else gets an explanation, not a
    ``FileNotFoundError`` traceback out of subprocess."""
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def require_systemd(action: str) -> None:
    if systemctl_available():
        return
    raise SystemdError(
        f"cannot {action}: systemd is not available on this platform "
        f"({sys.platform}).\n"
        "    systemd units are a Linux concept; there is no macOS/BSD equivalent "
        "this command can install.\n"
        "    Inspect what WOULD be installed anywhere:  "
        "uv run hyper service install <name> --dry-run\n"
        "    Run the service directly instead:          "
        "uv run hyper service run <name>"
    )


def require_privileges(scope: SystemdScope, action: str) -> None:
    """System units need root; ``--user`` units need exactly the opposite."""
    if scope.user_mode:
        return
    if os.geteuid() == 0:
        return
    verb = action.split()[0]
    raise SystemdError(
        f"cannot {action}: writing {SYSTEM_UNIT_DIR} requires root.\n"
        f"    Re-run with sudo:            sudo -E uv run hyper service {verb} "
        "<name>\n"
        "      (if your sudoers refuses -E, pass the database explicitly:\n"
        '       sudo env DATABASE_URL="$DATABASE_URL" uv run hyper service '
        f"{verb} <name>)\n"
        f"    Or install a per-user unit:  uv run hyper service {verb} <name> "
        "--user\n"
        f"    Or just look at the output:  uv run hyper service {verb} <name> "
        "--dry-run"
    )


def default_run_as() -> str:
    """The account a system unit should run as, by default.

    Under ``sudo`` this is the invoking human, not root: the repository, its
    virtualenv and every per-service ``.runtime`` directory already belong to
    them, and a service running as root would leave root-owned files behind in
    a user's checkout.
    """
    # SUDO_USER is set by sudo(8); no setting can carry "who typed sudo".
    # env-boundary: process invocation context, not framework configuration.
    invoker = os.environ.get("SUDO_USER", "")
    return invoker or getpass.getuser()


# ── Unit rendering ───────────────────────────────────────────────────────────


def unit_name(service_name: str) -> str:
    return f"{UNIT_PREFIX}{service_name}.service"


@dataclass(frozen=True, slots=True)
class UnitPlan:
    """One rendered unit and the files it owns."""

    service: Service
    unit_name: str
    unit_path: Path
    env_path: Path
    port: int
    requires: tuple[str, ...]
    part_of: tuple[str, ...]
    postgres: PostgresGate
    content: str
    env_values: dict[str, str]
    env_content: str
    secret_keys: frozenset[str]

    @property
    def env_preview(self) -> str:
        """The EnvironmentFile with secret values redacted, for ``--dry-run``.

        Printing a signing key into a terminal (and from there into shell
        history, CI logs and screenshots) would defeat the 0600 the real file
        is written with. Which keys are secret is not guessed from their names
        — ``HYPERSECRET_DEMO_DIR`` is a path, not a credential — it comes from
        :func:`secret_env_keys`, which reads the registry.
        """
        lines = [
            "# 0600, owned by the account systemd reads it as.",
            "# Secret values are REDACTED for display; the installed file "
            "carries the real ones.",
        ]
        for key in sorted(self.env_values):
            value = self.env_values[key]
            lines.append(f"{key}={redact(key, value, self.secret_keys)}")
        return "\n".join(lines) + "\n"


def secret_env_keys(service: Service) -> frozenset[str]:
    """Env vars whose values are credentials, straight from the registry.

    Name heuristics are wrong here in both directions: ``HYPERSECRET_DEMO_DIR``
    contains "SECRET" and is a path, while a future ``EMBEDDINGS_API_KEY``-style
    variable might not. The registry already declares which variables are
    secrets, so ask it.
    """
    keys = {SECRET_KEY_VAR, *DEMO_CREDENTIAL_VARS}
    keys.update(requirement.env_var for requirement in service.secrets)
    keys.update(binding.env_var for binding in service.companion_tokens)
    return frozenset(keys)


def redact(key: str, value: str, secret_keys: frozenset[str]) -> str:
    """Display form for one env value: secrets by length, URLs sans password."""
    if key in secret_keys:
        return f"<redacted, {len(value)} chars>"
    if key.endswith("DATABASE_URL"):
        parsed = urlparse(value)
        if parsed.password:
            return value.replace(f":{parsed.password}@", ":<redacted>@", 1)
    return value


def _env_line(key: str, value: str) -> str:
    """One ``EnvironmentFile`` line, quoted only when systemd needs it to be.

    systemd's parser splits on the first ``=`` and strips surrounding quotes;
    an unquoted value keeps trailing whitespace, so quote anything that could
    be misread rather than hoping registry values stay simple.
    """
    if value == value.strip() and not any(c in value for c in " \t\"'\\"):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def render_env_file(values: dict[str, str]) -> str:
    body = [
        "# Generated by `hyper service install` — 0600, do not commit.",
        "# Regenerate with `hyper service install <name>`; the signing keys are",
        "# read from the service's .env.local so they match its seeded data.",
    ]
    body.extend(_env_line(key, values[key]) for key in sorted(values))
    return "\n".join(body) + "\n"


def render_unit(
    *,
    service: Service,
    scope: SystemdScope,
    port: int,
    host: str,
    run_as: str,
    workdir: Path,
    env_path: Path,
    python: str,
    requires: tuple[str, ...],
    part_of: tuple[str, ...],
    postgres: PostgresGate,
    thread_pool_size: int,
) -> str:
    """Render one unit file. Pure — every input is an argument."""
    unit: list[str] = [
        "[Unit]",
        f"Description=HyperDjango service — {service.name}: {service.description}",
        f"Documentation=file://{workdir / 'services' / service.name / 'README.md'}",
        "Documentation=https://hyperdjango.dev",
        "Wants=network-online.target",
        "After=network-online.target",
    ]
    if postgres.unit:
        # Wants=, not Requires=: a database that fails to start should leave
        # this service retrying against the readiness gate (which reports the
        # real problem in the journal), not silently cancel its start job.
        unit.append(f"Wants={postgres.unit}")
        unit.append(f"After={postgres.unit}")
    for companion in requires:
        # Requires+After is the start half of the contract: the companion is
        # started first, and if it stops, this unit stops too.
        unit.append(f"Requires={companion}")
        unit.append(f"After={companion}")
    for parent in part_of:
        # PartOf is the stop/restart half, and it belongs on the DEPENDENT unit
        # pointing at what owns it: stopping or restarting `parent` propagates
        # here. Requires= alone does not propagate in this direction.
        unit.append(f"PartOf={parent}")
    unit.append(f"PartOf={TARGET_NAME}")
    unit.append(f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}")
    unit.append(f"StartLimitBurst={START_LIMIT_BURST}")

    service_section: list[str] = ["", "[Service]", "Type=exec"]
    if not scope.user_mode:
        service_section.append(f"User={run_as}")
        service_section.append(f"Group={_primary_group(run_as)}")
    service_section.extend(
        [
            f"WorkingDirectory={workdir}",
            f"EnvironmentFile={env_path}",
            f'Environment="PYTHONPATH={workdir}"',
            f'Environment="HYPER_THREAD_POOL_SIZE={thread_pool_size}"',
        ]
    )
    gate = postgres.exec_start_pre
    if gate:
        service_section.append(f"ExecStartPre={gate}")
    service_section.extend(
        [
            # `hyper run`, not `hyper start`: `start` daemonises and returns, so
            # systemd would reap the launcher and kill the server it spawned.
            # `run` is the foreground process Type=exec needs.
            f"ExecStart={python} -m hyperdjango.cli run "
            f"--host {host} --port {port} --app {service.app_path} --prod",
            "ExecStop=/bin/kill -TERM $MAINPID",
            # No ExecReload: the native server handles SIGTERM/SIGINT only —
            # there is no SIGHUP reload path. `systemctl reload` therefore fails
            # loudly instead of pretending to have reloaded. Use restart.
            "KillSignal=SIGTERM",
            "KillMode=mixed",
            "TimeoutStopSec=30",
            "Restart=on-failure",
            f"RestartSec={RESTART_SEC}",
            "StandardOutput=journal",
            "StandardError=journal",
            f"SyslogIdentifier={UNIT_PREFIX}{service.name}",
            "",
            "# Resource limits",
            "LimitNOFILE=65536",
            "LimitNPROC=4096",
            "",
            "# Security hardening",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            f"ReadWritePaths={workdir}",
            "NoNewPrivileges=true",
            "ProtectKernelTunables=true",
            "ProtectControlGroups=true",
            "RestrictSUIDSGID=true",
        ]
    )

    install = [
        "",
        "[Install]",
        f"WantedBy={scope.boot_target} {TARGET_NAME}",
    ]
    return "\n".join([*unit, *service_section, *install]) + "\n"


def _primary_group(user: str) -> str:
    """The account's primary group, or the account name when it is unknown.

    A ``User=`` that exists with a differently-named primary group (``ubuntu``
    is in ``ubuntu``, but a system account often is not) would otherwise get a
    ``Group=`` that does not resolve and a unit that refuses to start.
    """
    try:
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    except KeyError:
        return user


def render_target() -> str:
    """The grouping target: one handle for every installed service."""
    return (
        "[Unit]\n"
        "Description=HyperDjango services\n"
        "Documentation=https://hyperdjango.dev\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# ── Dependency wiring ────────────────────────────────────────────────────────


def installed_unit_files(scope: SystemdScope) -> frozenset[str]:
    """Unit filenames this installer has already written into ``unit_dir``."""
    if not scope.unit_dir.is_dir():
        return frozenset()
    return frozenset(p.name for p in scope.unit_dir.glob(f"{UNIT_PREFIX}*.service"))


def parents_of(
    companion: str, *, in_plan: frozenset[str], installed: frozenset[str]
) -> tuple[str, ...]:
    """Unit names that own ``companion`` — the ``PartOf=`` list for its unit.

    A companion can be shared (``hypermanager`` serves both ``hypersecret`` and
    ``live_config``). Taking the parents from the registry and then keeping only
    the ones being installed now or already on disk means installing a second
    parent later re-renders the companion with BOTH bindings, instead of
    silently dropping the first one.
    """
    owners = sorted(s.name for s in SERVICES.values() if companion in s.companions)
    return tuple(
        unit_name(owner)
        for owner in owners
        if owner in in_plan or unit_name(owner) in installed
    )


# ── Environment composition ──────────────────────────────────────────────────


def stable_secret_vars() -> tuple[str, ...]:
    """Env vars that MUST be pinned for a service systemd restarts.

    These settings default to a fresh random value per process, so an unpinned
    one silently invalidates every session cookie and CSRF token on each
    restart. :func:`hyperdjango.services_runner.resolve_secrets` mints and
    persists them; this is the same list, used to render placeholders in
    ``--dry-run`` (where nothing may be minted).
    """
    return tuple(sorted(f"HYPER_{name}" for name in AUTO_RANDOM_SECRET_SETTINGS))


def unit_env_values(
    service: Service,
    *,
    port: int,
    host: str,
    ports: dict[str, int],
    persist: bool,
) -> dict[str, str]:
    """The EnvironmentFile mapping for one service.

    Everything comes from the registry and from the runner's own secret
    resolution — never a fresh set. A regenerated signing key would leave every
    token the service's seed minted permanently unverifiable, which is the exact
    failure this reuse exists to prevent.
    """
    values: dict[str, str] = {}
    if service.needs_database:
        values["DATABASE_URL"] = service_database_url(service)
    values["HYPER_HOST"] = host
    values["HYPER_PORT"] = str(port)

    if persist:
        resolution = resolve_secrets(service)
        if resolution.missing:
            raise SystemdError(
                f"{service.name} needs {', '.join(resolution.missing)} and no "
                "random value can substitute for it (external credential).\n"
                f"    Export it, or add it to {service.env_file}, then re-run.\n"
                f"    See {service.directory / 'README.md'} for what it is."
            )
        values.update(resolution.values)
    else:
        # Dry run: read what is already persisted, invent nothing, write nothing.
        stored = read_env_file(service.env_file)
        for var in (
            *(r.env_var for r in service.secrets),
            *stable_secret_vars(),
        ):
            values[var] = stored.get(var, "<generated at install>")

    for entry in service.resolved_env():
        values.setdefault(entry.name, entry.value)

    for url_binding in service.companion_urls:
        companion_port = ports.get(url_binding.companion)
        if companion_port is not None:
            values[url_binding.env_var] = f"http://{host}:{companion_port}"

    if service.companion_tokens:
        if persist:
            values.update(companion_token_env(service))
        else:
            for binding in service.companion_tokens:
                values[binding.env_var] = "<read from the companion's tokens.json>"

    return values


# ── Planning ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Everything ``install`` would write and run, computed before it acts."""

    service: Service
    scope: SystemdScope
    units: tuple[UnitPlan, ...]
    target_path: Path
    target_content: str
    commands: tuple[tuple[str, ...], ...]

    @property
    def unit_names(self) -> tuple[str, ...]:
        return tuple(u.unit_name for u in self.units)


def build_install_plan(
    name: str,
    *,
    scope: SystemdScope,
    port: int | None,
    host: str,
    run_as: str,
    enable: bool,
    start: bool,
    persist: bool,
    workdir: Path | None = None,
    python: str | None = None,
    unit_names_on_host: tuple[str, ...] | None = None,
) -> InstallPlan:
    """Turn one registry name into a complete, inspectable install plan.

    Pure with respect to systemd: it writes nothing and runs no systemctl. The
    only side effects are the ones ``persist=True`` explicitly asks for
    (minting and storing a service's secrets), which ``--dry-run`` turns off.
    """
    service = get_service(name)
    order = launch_order(name)
    workdir = workdir or _REPO_ROOT
    python = python or sys.executable
    host_units = (
        query_unit_names(scope) if unit_names_on_host is None else unit_names_on_host
    )

    ports = {step.name: step.port for step in order}
    if port is not None:
        ports[service.name] = port

    in_plan = frozenset(step.name for step in order)
    installed = installed_unit_files(scope)
    thread_pool_size = int(get_setting("THREAD_POOL_SIZE", 24))

    units: list[UnitPlan] = []
    for step in order:
        gate = (
            postgres_gate(service_database_url(step), unit_names=host_units)
            if step.needs_database
            else no_database_gate()
        )
        requires = tuple(unit_name(c) for c in step.companions)
        part_of = parents_of(step.name, in_plan=in_plan, installed=installed)
        env_path = scope.env_dir / f"{UNIT_PREFIX}{step.name}.env"
        values = unit_env_values(
            step, port=ports[step.name], host=host, ports=ports, persist=persist
        )
        content = render_unit(
            service=step,
            scope=scope,
            port=ports[step.name],
            host=host,
            run_as=run_as,
            workdir=workdir,
            env_path=env_path,
            python=python,
            requires=requires,
            part_of=part_of,
            postgres=gate,
            thread_pool_size=thread_pool_size,
        )
        units.append(
            UnitPlan(
                service=step,
                unit_name=unit_name(step.name),
                unit_path=scope.unit_dir / unit_name(step.name),
                env_path=env_path,
                port=ports[step.name],
                requires=requires,
                part_of=part_of,
                postgres=gate,
                content=content,
                env_values=values,
                env_content=render_env_file(values),
                secret_keys=secret_env_keys(step),
            )
        )

    commands: list[tuple[str, ...]] = [(*scope.systemctl, "daemon-reload")]
    names = [u.unit_name for u in units]
    if enable:
        commands.append((*scope.systemctl, "enable", TARGET_NAME, *names))
    if start:
        # The target is started too, so `systemctl stop hyperdjango.target`
        # later has an active target to propagate from.
        commands.append((*scope.systemctl, "start", *names))
        commands.append((*scope.systemctl, "start", TARGET_NAME))

    return InstallPlan(
        service=service,
        scope=scope,
        units=tuple(units),
        target_path=scope.unit_dir / TARGET_NAME,
        target_content=render_target(),
        commands=tuple(commands),
    )


# ── Filesystem + systemctl execution ─────────────────────────────────────────


@dataclass(slots=True)
class UninstallReport:
    """What an uninstall removed and what it deliberately kept."""

    removed_units: list[str] = field(default_factory=list)
    removed_files: list[Path] = field(default_factory=list)
    kept_units: list[str] = field(default_factory=list)
    removed_target: bool = False


def write_secret_file(path: Path, content: str) -> None:
    """Write 0600 from creation — never briefly world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def _chown_tree(path: Path, user: str) -> None:
    """Hand back artifacts a root-run install created inside a user's checkout.

    ``hyper setup`` runs as this process (root, under sudo), so the per-service
    ``.runtime`` directory and ``.env.local`` it creates would be root-owned —
    and then unwritable by the unaccounted-for ``User=`` the unit runs as, which
    is how a service that installed cleanly fails on its first write.
    """
    if os.geteuid() != 0:
        return
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return
    if entry.pw_uid == 0:
        return
    if not path.exists():
        return
    os.chown(path, entry.pw_uid, entry.pw_gid)
    if path.is_dir():
        for child in path.rglob("*"):
            os.chown(child, entry.pw_uid, entry.pw_gid)


def run_systemctl(command: tuple[str, ...]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemdError(
            f"`{' '.join(command)}` failed (exit {result.returncode}).\n"
            f"{(result.stdout + result.stderr).strip()}"
        )


def write_plan(plan: InstallPlan) -> tuple[Path, ...]:
    """Write every file the plan owns; return them in write order.

    Secrets first and 0600, unit files second and 0644: the unit lands in a
    world-readable directory, so nothing sensitive may ever appear in it.
    Separated from :func:`cmd_install` so the file layout is exercisable
    without root and without a systemd on the machine.
    """
    written: list[Path] = []
    plan.scope.unit_dir.mkdir(parents=True, exist_ok=True)
    for unit in plan.units:
        write_secret_file(unit.env_path, unit.env_content)
        written.append(unit.env_path)
        unit.unit_path.write_text(unit.content, encoding="utf-8")
        unit.unit_path.chmod(0o644)
        written.append(unit.unit_path)
    plan.target_path.write_text(plan.target_content, encoding="utf-8")
    plan.target_path.chmod(0o644)
    written.append(plan.target_path)
    return tuple(written)


def remove_unit_files(
    scope: SystemdScope, service_names_to_remove: list[str]
) -> UninstallReport:
    """Delete the units and secrets for these services; drop the target last.

    The target only goes when the last HyperDjango unit does — removing it
    while other services are still installed would strand them without their
    grouping handle. The secrets directory goes with it once empty: an
    uninstall that leaves ``/etc/hyperdjango`` behind has not uninstalled.
    """
    report = UninstallReport()
    for name in service_names_to_remove:
        for path in (
            scope.unit_dir / unit_name(name),
            scope.env_dir / f"{UNIT_PREFIX}{name}.env",
        ):
            if path.exists():
                path.unlink()
                report.removed_files.append(path)
        report.removed_units.append(unit_name(name))
    target_path = scope.unit_dir / TARGET_NAME
    if not installed_unit_files(scope) and target_path.exists():
        target_path.unlink()
        report.removed_target = True
        report.removed_files.append(target_path)
    if scope.env_dir.is_dir() and not any(scope.env_dir.iterdir()):
        scope.env_dir.rmdir()
        report.removed_files.append(scope.env_dir)
    return report


# ── Setup (tables + seed) ────────────────────────────────────────────────────


def prepare_databases(plan: InstallPlan, *, run_as: str) -> None:
    """Create tables and seed data for every service in the plan.

    An installed unit that boots against a database with no schema is a service
    that restarts forever, so ``install`` owns this the same way ``run`` does —
    through ``hyper setup``, the platform's single DDL authority, with the SAME
    composed environment the unit will later carry. Companions come first, so a
    companion's seed has minted its identity tokens before the dependent's
    environment is composed from them.
    """
    needs_db = [u for u in plan.units if u.service.needs_database]
    if not needs_db:
        return
    check_postgres_reachable(service_database_url(needs_db[0].service))
    for unit in needs_db:
        step = unit.service
        step.runtime_dir.mkdir(parents=True, exist_ok=True)
        env = compose_env(
            step,
            port=unit.port,
            secrets_values=unit.env_values,
            companion_tokens={},
        )
        logger.info("  {name}: creating tables + seeding...", name=step.name)
        run_setup(step, env, fresh=needs_initial_setup(step), seed=True)
        mark_setup_complete(step)
        _chown_tree(step.runtime_dir, run_as)
        _chown_tree(step.env_file, run_as)
        logger.success(
            "  {name}: database ready ({db})", name=step.name, db=step.database_name
        )


# ── Commands ─────────────────────────────────────────────────────────────────


def _out(message: str = "") -> None:
    print(message, flush=True)


def print_dry_run(plan: InstallPlan) -> None:
    """Show every file and command, and touch nothing."""
    _out("=" * 74)
    _out(f"  DRY RUN — {plan.service.name} ({plan.scope.label} scope)")
    _out("  Nothing below has been written or executed.")
    _out("=" * 74)
    for unit in plan.units:
        role = "main service" if unit.service.name == plan.service.name else "companion"
        _out()
        _out(f"── {unit.unit_path}  ({role}, port {unit.port}) " + "─" * 8)
        _out(f"   PostgreSQL: {unit.postgres.reason}")
        _out()
        _out(unit.content)
        _out(f"── {unit.env_path}  (0600) " + "─" * 20)
        _out()
        _out(unit.env_preview)
    _out(f"── {plan.target_path} " + "─" * 30)
    _out()
    _out(plan.target_content)
    _out("── commands " + "─" * 50)
    _out()
    for command in plan.commands:
        _out(f"  $ {' '.join(command)}")
    _out()
    _out(
        f"  Logs once running:  {' '.join(plan.scope.journalctl)} -u "
        f"{plan.unit_names[0]} -f"
    )
    _out()


def cmd_install(
    name: str,
    *,
    port: int | None,
    host: str,
    user_mode: bool,
    run_as: str | None,
    enable: bool,
    start: bool,
    dry_run: bool,
    setup: bool,
) -> int:
    """Install (and optionally enable/start) units for one service + companions."""
    scope = SystemdScope.resolve(user_mode=user_mode)
    account = run_as or default_run_as()

    if dry_run:
        plan = build_install_plan(
            name,
            scope=scope,
            port=port,
            host=host,
            run_as=account,
            enable=enable,
            start=start,
            persist=False,
        )
        print_dry_run(plan)
        return 0

    require_systemd("install a service")
    require_privileges(scope, "install a service")

    plan = build_install_plan(
        name,
        scope=scope,
        port=port,
        host=host,
        run_as=account,
        enable=enable,
        start=start,
        persist=True,
    )

    logger.info(
        "Installing {n} unit(s) for {name} ({scope} scope, running as {who})",
        n=len(plan.units),
        name=name,
        scope=scope.label,
        who="the invoking user" if scope.user_mode else account,
    )

    if setup:
        prepare_databases(plan, run_as=account)

    write_plan(plan)
    for unit in plan.units:
        logger.success("  {p} (0600)", p=unit.env_path)
        logger.success(
            "  {p}  [port {port}; postgres: {why}]",
            p=unit.unit_path,
            port=unit.port,
            why=unit.postgres.reason,
        )
    logger.success("  {p}", p=plan.target_path)

    for command in plan.commands:
        logger.info("  $ {c}", c=" ".join(command))
        run_systemctl(command)

    _out()
    _out(f"  {plan.service.name} installed as {', '.join(plan.unit_names)}")
    for unit in plan.units:
        _out(f"      http://{host}:{unit.port}   {unit.unit_name}")
    _out()
    if not enable:
        _out(
            f"  Enable at boot:  {' '.join(scope.systemctl)} enable "
            f"{TARGET_NAME} {' '.join(plan.unit_names)}"
        )
    if not start:
        _out(
            f"  Start now:       {' '.join(scope.systemctl)} start "
            f"{' '.join(plan.unit_names)}"
        )
    _out(f"  Status:          {' '.join(scope.systemctl)} status {plan.unit_names[0]}")
    _out(f"  Logs:            {' '.join(scope.journalctl)} -u {plan.unit_names[0]} -f")
    _out(f"  Whole set:       {' '.join(scope.systemctl)} restart {TARGET_NAME}")
    _out(
        "  Reload:          NOT supported — these units are restart-only "
        "(no SIGHUP path in the server)."
    )
    _out(f"  Remove:          uv run hyper service uninstall {plan.service.name}")
    _out()
    return 0


@dataclass(frozen=True, slots=True)
class UninstallSelection:
    """Which units an uninstall touches, and what that costs elsewhere."""

    remove: tuple[str, ...]
    kept: tuple[str, ...]
    orphaned: tuple[str, ...]


def plan_uninstall(name: str, *, scope: SystemdScope) -> UninstallSelection:
    """Decide what ``uninstall <name>`` removes, keeps, and breaks.

    The named service always goes — the operator asked for it by name. Its
    COMPANIONS are kept when another installed service still declares them,
    because taking a shared companion away would silently break a service
    nobody mentioned. Anything still depended on by an installed service after
    the removal is reported as ``orphaned`` rather than quietly ignored.
    """
    order = launch_order(name)
    doomed = {step.name for step in order}
    installed = installed_unit_files(scope)

    def other_parents(target: str) -> list[str]:
        return [
            owner.name
            for owner in SERVICES.values()
            if target in owner.companions
            and owner.name not in doomed
            and unit_name(owner.name) in installed
        ]

    remove: list[str] = []
    kept: list[str] = []
    for step in order:
        if step.name != name and other_parents(step.name):
            kept.append(step.name)
        else:
            remove.append(step.name)
    orphaned = sorted({parent for step in remove for parent in other_parents(step)})
    return UninstallSelection(tuple(remove), tuple(kept), tuple(orphaned))


def cmd_uninstall(name: str, *, user_mode: bool, dry_run: bool) -> int:
    """Stop, disable and remove the units (and secrets) install created."""
    scope = SystemdScope.resolve(user_mode=user_mode)
    get_service(name)  # validate the name before touching anything
    selection = plan_uninstall(name, scope=scope)
    units = [unit_name(n) for n in selection.remove]

    if dry_run:
        _out(f"DRY RUN — would remove {len(units)} unit(s) for {name}:")
        for step in selection.remove:
            _out(f"  rm {scope.unit_dir / unit_name(step)}")
            _out(f"  rm {scope.env_dir / f'{UNIT_PREFIX}{step}.env'}")
        for kept in selection.kept:
            _out(f"  keep {unit_name(kept)} (another installed service needs it)")
        for parent in selection.orphaned:
            _out(f"  WARNING {unit_name(parent)} still requires what is removed")
        _out(f"  $ {' '.join(scope.systemctl)} stop {' '.join(units)}")
        _out(f"  $ {' '.join(scope.systemctl)} disable {' '.join(units)}")
        _out(f"  $ {' '.join(scope.systemctl)} daemon-reload")
        return 0

    require_systemd("uninstall a service")
    require_privileges(scope, "uninstall a service")

    # stop/disable are best-effort: a unit that was written but never enabled
    # (or already removed by hand) must not turn uninstall into a failure.
    subprocess.run([*scope.systemctl, "stop", *units], capture_output=True)
    subprocess.run([*scope.systemctl, "disable", *units], capture_output=True)
    if not (installed_unit_files(scope) - set(units)):
        subprocess.run([*scope.systemctl, "stop", TARGET_NAME], capture_output=True)
        subprocess.run([*scope.systemctl, "disable", TARGET_NAME], capture_output=True)

    report = remove_unit_files(scope, list(selection.remove))
    report.kept_units = [unit_name(k) for k in selection.kept]
    run_systemctl((*scope.systemctl, "daemon-reload"))

    for path in report.removed_files:
        logger.success("  removed {p}", p=path)
    for kept in report.kept_units:
        logger.info("  kept {u} — another installed service requires it", u=kept)
    for parent in selection.orphaned:
        logger.warning(
            "{u} is still installed and declares a companion that was just "
            "removed — reinstall it (`hyper service install {n}`) or uninstall it",
            u=unit_name(parent),
            n=parent,
        )
    if report.removed_target:
        logger.success("  removed {t} (no HyperDjango units left)", t=TARGET_NAME)
    if not report.removed_files:
        logger.warning(
            "Nothing to remove for {name} in {dir} — was it installed with a "
            "different scope? (`--user` units live in ~/.config/systemd/user)",
            name=name,
            dir=scope.unit_dir,
        )
        return 1
    return 0


def dispatch(args) -> int:
    """Route ``hyper service {install,uninstall}``.

    Owns its own error handling for the same reason
    :func:`hyperdjango.services_runner.dispatch` does: an unknown name must
    exit non-zero having NAMED every valid one, and a systemd failure must
    print its remediation rather than a traceback.
    """
    verb = args.service_command
    try:
        if verb == "install":
            return cmd_install(
                args.name,
                port=args.port,
                host=args.host,
                user_mode=args.user,
                run_as=args.run_as,
                enable=args.enable,
                start=args.start,
                dry_run=args.dry_run,
                setup=not args.no_setup,
            )
        if verb == "uninstall":
            return cmd_uninstall(args.name, user_mode=args.user, dry_run=args.dry_run)
    except UnknownServiceError as exc:
        logger.error(str(exc))
        return 2
    except ServiceError as exc:
        logger.error(str(exc))
        return 1
    logger.error("Usage: hyper service {install|uninstall} <name>")
    return 2


__all__ = [
    "InstallPlan",
    "PostgresGate",
    "SystemdError",
    "SystemdScope",
    "UnitPlan",
    "UninstallReport",
    "UninstallSelection",
    "build_install_plan",
    "cmd_install",
    "cmd_uninstall",
    "default_run_as",
    "dispatch",
    "detect_postgres_unit",
    "host_is_local",
    "installed_unit_files",
    "no_database_gate",
    "parents_of",
    "parse_unit_names",
    "plan_uninstall",
    "remove_unit_files",
    "postgres_gate",
    "redact",
    "secret_env_keys",
    "stable_secret_vars",
    "render_env_file",
    "render_target",
    "render_unit",
    "unit_env_values",
    "unit_name",
    "write_plan",
]
