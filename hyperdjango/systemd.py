"""
systemd service management for HyperDjango.

Generates, installs, and manages systemd unit files for production deployment.

Usage:
    hyper systemd install --app app:app --port 8000
    hyper systemd install --app app:app --port 8000 --enable
    hyper systemd uninstall
    hyper systemd status
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hyperdjango.conf import get_setting, resolve_database_url
from hyperdjango.logging import logger

# Secrets (SECRET_KEY, DATABASE_URL) live in a separate EnvironmentFile written
# with 0600 perms — NOT inline in the unit, which lands under
# /etc/systemd/system with world-readable (0644) perms. The app reads
# HYPER_SECRET_KEY (see hyperdjango.conf get_setting → HYPER_<NAME>); a bare
# SECRET_KEY is never consulted, so emitting it would leave the deployed
# service on the per-process random default — silently invalidating every
# session/CSRF token on each restart.
_UNIT_TEMPLATE = """\
[Unit]
Description=HyperDjango — {title}
After=network.target postgresql.service
Wants=postgresql.service
Documentation=https://hyperdjango.dev

[Service]
Type=exec
User={user}
Group={group}
WorkingDirectory={workdir}
EnvironmentFile={env_file}
Environment="HYPER_THREAD_POOL_SIZE={thread_pool_size}"
Environment="PYTHONPATH={workdir}"
ExecStart={python} -m hyperdjango.cli run --host {host} --port {port} --app {app} --prod
ExecStop=/bin/kill -TERM $MAINPID
TimeoutStopSec=30
KillMode=mixed
KillSignal=SIGTERM
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier={service_name}

# Resource limits
LimitNOFILE=65536
LimitNPROC=4096

# Security hardening
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={workdir}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def _service_name(args: object) -> str:
    # dynamic-attr: args is an argparse.Namespace — "name" is only set when the subparser defines --name
    name = getattr(args, "name", None)
    if name:
        return f"hyperdjango-{name}"
    return f"hyperdjango-{Path.cwd().name}"


def _unit_path(args: object) -> Path:
    return Path(f"/etc/systemd/system/{_service_name(args)}.service")


def _env_file_path(args: object) -> Path:
    """Path to the 0600 EnvironmentFile that holds the service secrets."""
    return Path(f"/etc/hyperdjango/{_service_name(args)}.env")


def _systemctl_available() -> bool:
    """systemd only exists on Linux; guard so macOS/BSD get a clear message
    instead of a FileNotFoundError traceback from subprocess."""
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def _require_systemctl(action: str) -> bool:
    if _systemctl_available():
        return True
    logger.error(
        "systemd is not available on this platform ({platform}) — cannot {action}.",
        platform=sys.platform,
        action=action,
    )
    logger.info("systemd unit management only works on Linux hosts with systemctl.")
    return False


def _render_env_file(args: object) -> str:
    """Secrets file contents. The app reads HYPER_SECRET_KEY (NOT bare
    SECRET_KEY). The database URL is emitted as DATABASE_URL (12-factor); the
    framework equally honors HYPER_DATABASE_URL or the libpq PG* set and
    resolves whichever is set to the same connection for server, CLI, and
    driver."""
    secret_key = str(get_setting("SECRET_KEY") or "") or "CHANGE-ME-IN-PRODUCTION"
    database_url = resolve_database_url() or "postgres://localhost/mydb"
    return f"HYPER_SECRET_KEY={secret_key}\nDATABASE_URL={database_url}\n"


def systemd_install(args: object) -> None:
    """Generate and install a systemd unit file + a 0600 secrets EnvironmentFile."""
    svc = _service_name(args)
    unit_path = _unit_path(args)
    env_file = _env_file_path(args)

    # The systemd `User=` directive is an OS service-account concept, resolved
    # from the CLI flag then the OS user; it is not a framework setting.
    # dynamic-attr: args is an argparse.Namespace — optional install-subparser flag
    user = getattr(
        args, "user", None
    ) or os.environ.get(  # env-boundary: OS service user, not a config read
        "USER", "root"
    )
    host = getattr(
        args, "host", "0.0.0.0"
    )  # dynamic-attr: argparse.Namespace optional flag
    port = getattr(args, "port", 8000)  # dynamic-attr: argparse.Namespace optional flag
    app = getattr(
        args, "app", "app:app"
    )  # dynamic-attr: argparse.Namespace optional flag
    enable = getattr(
        args, "enable", False
    )  # dynamic-attr: argparse.Namespace optional flag

    content = _UNIT_TEMPLATE.format(
        title=Path.cwd().name,
        user=user,
        group=user,
        workdir=Path.cwd(),
        env_file=env_file,
        thread_pool_size=get_setting("THREAD_POOL_SIZE", 24),
        python=sys.executable,
        host=host,
        port=port,
        app=app,
        service_name=svc,
    )
    env_content = _render_env_file(args)

    logger.info("Service: {svc}", svc=svc)
    logger.info("Unit file: {unit_path}", unit_path=unit_path)
    logger.info("Secrets file: {env_file} (0600)", env_file=env_file)
    logger.info("Working directory: {workdir}", workdir=Path.cwd())
    logger.info("User: {user}", user=user)
    logger.info("Bind: {host}:{port}", host=host, port=port)

    # Platform guard — installing a systemd unit only makes sense on Linux.
    if not _require_systemctl("install a service"):
        # Still write the files locally for inspection so the command is useful
        # on a dev box, but never touch systemctl.
        tmp_unit = Path(f"{svc}.service")
        tmp_env = Path(f"{svc}.env")
        tmp_unit.write_text(content)
        _write_secret_file(tmp_env, env_content)
        logger.info("Unit written for inspection: {p}", p=tmp_unit)
        logger.info("Secrets written for inspection: {p} (0600)", p=tmp_env)
        return

    if os.geteuid() != 0:
        logger.warning("Root required. Run with sudo:")
        logger.info(
            "  sudo hyper systemd install --app {app} --port {port}", app=app, port=port
        )
        # Write to local files for a manual install.
        tmp_unit = Path(f"{svc}.service")
        tmp_env = Path(f"{svc}.env")
        tmp_unit.write_text(content)
        _write_secret_file(tmp_env, env_content)
        logger.info("Unit file written to: {tmp}", tmp=tmp_unit)
        logger.info("Secrets file written to: {tmp} (0600)", tmp=tmp_env)
        logger.info(
            "Manual install: sudo install -m600 -D {tmp_env} {env_file} && "
            "sudo cp {tmp_unit} {unit_path} && sudo systemctl daemon-reload",
            tmp_env=tmp_env,
            env_file=env_file,
            tmp_unit=tmp_unit,
            unit_path=unit_path,
        )
        return

    # Secrets first, with restrictive perms, then the (world-readable) unit.
    env_file.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_file(env_file, env_content)
    logger.success("Installed {env_file} (0600)", env_file=env_file)

    unit_path.write_text(content)
    logger.success("Installed {unit_path}", unit_path=unit_path)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    logger.success("Reloaded systemd")

    if enable:
        subprocess.run(["systemctl", "enable", "--now", svc], check=True)
        logger.success("Enabled and started {svc}", svc=svc)
    else:
        logger.info("Start with: sudo systemctl start {svc}", svc=svc)
        logger.info("Enable on boot: sudo systemctl enable {svc}", svc=svc)


def _write_secret_file(path: Path, content: str) -> None:
    """Write a file containing secrets with 0600 perms (owner read/write only),
    creating it restrictively so the secret is never briefly world-readable."""
    # Open with O_CREAT|O_WRONLY|O_TRUNC and mode 0600 so the file is created
    # with the right perms from the start (write_text would create 0644).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    # Enforce perms even if the file pre-existed with looser perms.
    path.chmod(0o600)


def systemd_uninstall(args: object) -> None:
    """Stop and remove the systemd unit file and its secrets EnvironmentFile."""
    svc = _service_name(args)
    unit_path = _unit_path(args)
    env_file = _env_file_path(args)

    if not _require_systemctl("uninstall a service"):
        return

    if os.geteuid() != 0:
        logger.error("Root required: sudo hyper systemd uninstall")
        sys.exit(1)

    if unit_path.exists():
        subprocess.run(["systemctl", "stop", svc], capture_output=True)
        subprocess.run(["systemctl", "disable", svc], capture_output=True)
        unit_path.unlink()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        logger.success("Removed {unit_path}", unit_path=unit_path)
    else:
        logger.warning("No unit file found at {unit_path}", unit_path=unit_path)

    if env_file.exists():
        env_file.unlink()
        logger.success("Removed {env_file}", env_file=env_file)


def systemd_status(args: object) -> None:
    """Show systemd service status."""
    svc = _service_name(args)
    if not _require_systemctl("query service status"):
        return
    result = subprocess.run(
        ["systemctl", "status", svc],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        for line in result.stdout.rstrip("\n").split("\n"):
            logger.opt(raw=True).info(f"{line}\n")
    else:
        logger.warning("Service {svc} not found or not installed.", svc=svc)
        logger.info("Install with: hyper systemd install --app app:app")
