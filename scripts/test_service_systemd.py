"""Tests for registry-driven systemd unit generation (``hyper service install``).

Pins the contract that makes an installed unit trustworthy:

  1. a plain database-backed service renders the exact ExecStart, EnvironmentFile,
     SyslogIdentifier, PostgreSQL readiness gate and backoff directives — and no
     ``ExecReload``, because the server has no SIGHUP reload path;
  2. a ``needs_database=False`` service gets no PostgreSQL dependency at all —
     no ``After=``, no ``Wants=``, no ``ExecStartPre``, no ``DATABASE_URL``;
  3. a companion pair gets the dependency directives in BOTH directions:
     ``Requires=``/``After=`` on the parent, ``PartOf=`` on the companion, and
     the companion is planned first;
  4. PostgreSQL unit detection picks the right unit from synthetic listings, and
     never a template unit;
  5. ``--dry-run`` writes nothing, anywhere;
  6. install writes the files, uninstall removes exactly those files, and a
     shared companion survives uninstalling one of its parents;
  7. an unknown service name fails loudly, listing every valid name.

No systemctl is invoked anywhere in this file: every install/uninstall test
stages into a temporary prefix and exercises the filesystem layer directly.
"""

# hyper-test: unit

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hyperdjango.conf import AUTO_RANDOM_SECRET_SETTINGS
from hyperdjango.services_registry import get_service
from hyperdjango.services_systemd import (
    SECRET_KEY_VAR,
    START_LIMIT_BURST,
    START_LIMIT_INTERVAL_SEC,
    TARGET_NAME,
    PostgresGate,
    SystemdScope,
    build_install_plan,
    detect_postgres_unit,
    host_is_local,
    installed_unit_files,
    no_database_gate,
    parents_of,
    parse_unit_names,
    plan_uninstall,
    postgres_gate,
    redact,
    remove_unit_files,
    render_env_file,
    render_target,
    secret_env_keys,
    stable_secret_vars,
    unit_name,
    write_plan,
)
from hyperdjango.testkit import check, finish, run_main

REPO_ROOT = Path(__file__).resolve().parent.parent

# A host listing that looks like Ubuntu with a meta-unit AND a template, which
# is exactly the shape that makes naive detection pick something unstartable.
UBUNTU_LISTING = """\
postgresql.service                           enabled         enabled
postgresql@.service                          indirect        enabled
ssh.service                                  enabled         enabled
"""

# A host with only per-cluster instances — no meta-unit to fall back on.
INSTANCE_LISTING = """\
postgresql@16-main.service                   enabled         enabled
postgresql@18-main.service                   enabled         enabled
"""

NO_PG_LISTING = """\
ssh.service                                  enabled         enabled
nginx.service                                enabled         enabled
"""


def _plan(name: str, prefix: Path, **overrides):
    """An install plan staged under ``prefix``, with no host systemd consulted."""
    scope = SystemdScope.resolve(user_mode=False, prefix=prefix)
    kwargs = {
        "scope": scope,
        "port": None,
        "host": "127.0.0.1",
        "run_as": "svcuser",
        "enable": True,
        "start": True,
        "persist": False,
        "workdir": Path("/srv/hyperdjango"),
        "python": "/srv/hyperdjango/.venv/bin/python3",
        "unit_names_on_host": parse_unit_names(UBUNTU_LISTING),
    }
    kwargs.update(overrides)
    return build_install_plan(name, **kwargs)


def _directives(content: str) -> list[str]:
    return [line.strip() for line in content.splitlines() if line.strip()]


# ── 1. plain database-backed service ─────────────────────────────────────────


def test_plain_db_unit(prefix: Path) -> None:
    plan = _plan("bookstore_api", prefix)
    check("db: one unit (no companions)", len(plan.units) == 1, str(plan.unit_names))
    unit = plan.units[0]
    lines = _directives(unit.content)
    service = get_service("bookstore_api")

    check(
        "db: unit name from the registry name",
        unit.unit_name == "hyperdjango-bookstore_api.service",
        unit.unit_name,
    )
    check(
        "db: port from the registry",
        unit.port == service.port and f"--port {service.port}" in unit.content,
        str(unit.port),
    )
    check(
        "db: ExecStart is the real foreground invocation",
        "ExecStart=/srv/hyperdjango/.venv/bin/python3 -m hyperdjango.cli run "
        "--host 127.0.0.1 --port 8603 --app services.bookstore_api.app:app --prod"
        in lines,
        [ln for ln in lines if ln.startswith("ExecStart=")],
    )
    check(
        "db: EnvironmentFile is the per-service 0600 path",
        f"EnvironmentFile={prefix}/etc/hyperdjango/hyperdjango-bookstore_api.env"
        in lines,
        [ln for ln in lines if ln.startswith("EnvironmentFile=")],
    )
    check(
        "db: WorkingDirectory + PYTHONPATH point at the checkout",
        "WorkingDirectory=/srv/hyperdjango" in lines
        and 'Environment="PYTHONPATH=/srv/hyperdjango"' in lines,
        [ln for ln in lines if "PYTHONPATH" in ln],
    )
    check(
        "db: per-service SyslogIdentifier for journald",
        "SyslogIdentifier=hyperdjango-bookstore_api" in lines,
        [ln for ln in lines if ln.startswith("SyslogIdentifier")],
    )
    check(
        "db: ordered after the detected PostgreSQL unit",
        "After=postgresql.service" in lines and "Wants=postgresql.service" in lines,
        [ln for ln in lines if "postgresql" in ln],
    )
    gate = [ln for ln in lines if ln.startswith("ExecStartPre=")]
    check(
        "db: ExecStartPre runs pg_isready against the resolved host/port",
        len(gate) == 1
        and "pg_isready" in gate[0]
        and "--port=5432" in gate[0]
        and gate[0].split("=", 1)[1].startswith("/"),
        gate,
    )
    check(
        "db: boot-race backoff directives present",
        f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}" in lines
        and f"StartLimitBurst={START_LIMIT_BURST}" in lines
        and "RestartSec=5" in lines
        and "Restart=on-failure" in lines,
        [ln for ln in lines if "Limit" in ln or "Restart" in ln],
    )
    check(
        "db: NO ExecReload — the server has no SIGHUP reload path",
        not any(ln.startswith("ExecReload") for ln in lines),
        [ln for ln in lines if "Reload" in ln],
    )
    check(
        "db: graceful stop wired to SIGTERM (which the server DOES handle)",
        "KillSignal=SIGTERM" in lines and "ExecStop=/bin/kill -TERM $MAINPID" in lines,
        [ln for ln in lines if "Kill" in ln or ln.startswith("ExecStop")],
    )
    check(
        "db: runs as the requested account",
        "User=svcuser" in lines,
        [ln for ln in lines if ln.startswith("User=")],
    )
    check(
        "db: security hardening kept from the original unit template",
        {
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "NoNewPrivileges=true",
            "ReadWritePaths=/srv/hyperdjango",
        }
        <= set(lines),
        [ln for ln in lines if "Protect" in ln or "Private" in ln],
    )
    check(
        "db: grouped into the target and enabled for boot",
        f"PartOf={TARGET_NAME}" in lines
        and f"WantedBy=multi-user.target {TARGET_NAME}" in lines,
        [ln for ln in lines if "target" in ln],
    )
    check(
        "db: DATABASE_URL is the service's own database",
        unit.env_values["DATABASE_URL"].endswith("/hyper_service_bookstore_api"),
        unit.env_values["DATABASE_URL"],
    )
    check(
        "db: env carries the port the unit binds",
        unit.env_values["HYPER_PORT"] == str(service.port),
        unit.env_values["HYPER_PORT"],
    )
    check(
        "db: a signing key is always part of the environment",
        SECRET_KEY_VAR in unit.env_values,
        sorted(unit.env_values),
    )
    check(
        "db: every per-process-random secret is pinned in the env file",
        set(stable_secret_vars()) <= set(unit.env_values),
        str(sorted(set(stable_secret_vars()) - set(unit.env_values))),
    )
    check(
        "db: the pinned set is exactly conf's auto-random secret settings",
        set(stable_secret_vars())
        == {f"HYPER_{n}" for n in AUTO_RANDOM_SECRET_SETTINGS},
        str(stable_secret_vars()),
    )


def test_port_override(prefix: Path) -> None:
    plan = _plan("bookstore_api", prefix, port=8699)
    unit = plan.units[0]
    check(
        "port override reaches ExecStart and the env file",
        "--port 8699" in unit.content and unit.env_values["HYPER_PORT"] == "8699",
        unit.env_values["HYPER_PORT"],
    )


def test_user_scope_omits_user_directive(prefix: Path) -> None:
    scope = SystemdScope.resolve(user_mode=True, prefix=prefix)
    plan = _plan("bookstore_api", prefix, scope=scope)
    lines = _directives(plan.units[0].content)
    check(
        "user mode: no User=/Group= (a --user manager rejects them)",
        not any(ln.startswith(("User=", "Group=")) for ln in lines),
        [ln for ln in lines if ln.startswith(("User=", "Group="))],
    )
    check(
        "user mode: boot target is default.target, not multi-user.target",
        f"WantedBy=default.target {TARGET_NAME}" in lines,
        [ln for ln in lines if ln.startswith("WantedBy")],
    )
    check(
        "user mode: systemctl is invoked with --user",
        scope.systemctl == ("systemctl", "--user"),
        str(scope.systemctl),
    )


# ── 2. service that needs no database ────────────────────────────────────────


def test_no_db_unit(prefix: Path) -> None:
    plan = _plan("hello", prefix)
    unit = plan.units[0]
    lines = _directives(unit.content)
    check(
        "no-db: registry says needs_database=False",
        not get_service("hello").needs_database,
        "",
    )
    check(
        "no-db: no PostgreSQL ordering at all",
        not any("postgres" in ln.lower() for ln in lines),
        [ln for ln in lines if "postgres" in ln.lower()],
    )
    check(
        "no-db: no ExecStartPre readiness gate",
        not any(ln.startswith("ExecStartPre") for ln in lines),
        [ln for ln in lines if ln.startswith("ExecStartPre")],
    )
    check(
        "no-db: no DATABASE_URL in the environment file",
        "DATABASE_URL" not in unit.env_values,
        sorted(unit.env_values),
    )
    check(
        "no-db: gate reason explains itself",
        "needs_database=False" in no_database_gate().reason,
        no_database_gate().reason,
    )


# ── 3. companion pair ────────────────────────────────────────────────────────


def test_companion_pair(prefix: Path) -> None:
    plan = _plan("hypersecret", prefix)
    names = list(plan.unit_names)
    check(
        "companion: two units, companion planned first",
        names
        == [
            "hyperdjango-hypermanager.service",
            "hyperdjango-hypersecret.service",
        ],
        str(names),
    )
    companion, parent = plan.units
    parent_lines = _directives(parent.content)
    companion_lines = _directives(companion.content)

    check(
        "companion: parent Requires= the companion (start + stop propagation)",
        "Requires=hyperdjango-hypermanager.service" in parent_lines,
        [ln for ln in parent_lines if ln.startswith("Requires")],
    )
    check(
        "companion: parent After= the companion (ordering)",
        "After=hyperdjango-hypermanager.service" in parent_lines,
        [ln for ln in parent_lines if ln.startswith("After")],
    )
    check(
        "companion: companion PartOf= the parent (stop/restart propagation)",
        "PartOf=hyperdjango-hypersecret.service" in companion_lines,
        [ln for ln in companion_lines if ln.startswith("PartOf")],
    )
    check(
        "companion: the companion does NOT Require the parent (no cycle)",
        not any(
            ln.startswith("Requires=hyperdjango-hypersecret") for ln in companion_lines
        ),
        [ln for ln in companion_lines if ln.startswith("Requires")],
    )
    check(
        "companion: both units are PartOf the grouping target",
        f"PartOf={TARGET_NAME}" in parent_lines
        and f"PartOf={TARGET_NAME}" in companion_lines,
        "",
    )
    check(
        "companion: distinct ports, distinct identifiers",
        parent.port != companion.port
        and "SyslogIdentifier=hyperdjango-hypersecret" in parent_lines
        and "SyslogIdentifier=hyperdjango-hypermanager" in companion_lines,
        f"{parent.port} vs {companion.port}",
    )
    check(
        "companion: parent's env points at the companion's actual port",
        parent.env_values["HYPERSECRET_MANAGER_URL"]
        == f"http://127.0.0.1:{companion.port}",
        parent.env_values["HYPERSECRET_MANAGER_URL"],
    )
    check(
        "companion: registry extra_env reaches the env file",
        parent.env_values["HYPERSECRET_ROTATION_SWEEP_INTERVAL"] == "5",
        parent.env_values.get("HYPERSECRET_ROTATION_SWEEP_INTERVAL", "(missing)"),
    )
    check(
        "companion: enable/start commands cover both units and the target",
        plan.commands[1][:3] == ("systemctl", "enable", TARGET_NAME)
        and set(plan.commands[1][3:]) == set(names),
        str(plan.commands),
    )


def test_shared_companion_partof_accumulates(prefix: Path) -> None:
    """A companion serving two parents must be PartOf BOTH, not just the last."""
    scope = SystemdScope.resolve(user_mode=False, prefix=prefix)
    write_plan(_plan("hypersecret", prefix))
    installed = installed_unit_files(scope)
    check(
        "shared: hypersecret's unit is on disk before the second install",
        "hyperdjango-hypersecret.service" in installed,
        sorted(installed),
    )
    parents = parents_of(
        "hypermanager",
        in_plan=frozenset({"live_config", "hypermanager", "hypersecret"}),
        installed=installed,
    )
    check(
        "shared: companion is PartOf both owners",
        parents
        == (
            "hyperdjango-hypersecret.service",
            "hyperdjango-live_config.service",
        ),
        str(parents),
    )
    remove_unit_files(scope, ["hypersecret", "hypermanager"])


# ── 4. PostgreSQL unit detection (synthetic inputs only) ─────────────────────


def test_pg_detection() -> None:
    ubuntu = parse_unit_names(UBUNTU_LISTING)
    check(
        "pg: unit names parsed out of a listing",
        ubuntu == ("postgresql.service", "postgresql@.service", "ssh.service"),
        str(ubuntu),
    )
    check(
        "pg: bullet-prefixed list-units lines parse too",
        parse_unit_names("● postgresql.service loaded failed failed")
        == ("postgresql.service",),
        "",
    )
    check(
        "pg: meta-unit wins when present",
        detect_postgres_unit(ubuntu) == "postgresql.service",
        str(detect_postgres_unit(ubuntu)),
    )
    check(
        "pg: template unit is NEVER selected (nothing to start)",
        detect_postgres_unit(("postgresql@.service",)) is None,
        str(detect_postgres_unit(("postgresql@.service",))),
    )
    instances = parse_unit_names(INSTANCE_LISTING)
    check(
        "pg: highest-numbered cluster instance when there is no meta-unit",
        detect_postgres_unit(instances) == "postgresql@18-main.service",
        str(detect_postgres_unit(instances)),
    )
    check(
        "pg: no PostgreSQL on the host means no unit dependency",
        detect_postgres_unit(parse_unit_names(NO_PG_LISTING)) is None,
        "",
    )


def test_pg_gate_shapes() -> None:
    local = postgres_gate(
        "postgres://localhost:5433/hyper_service_x",
        unit_names=parse_unit_names(UBUNTU_LISTING),
    )
    check(
        "gate: local database orders after the detected unit",
        local.unit == "postgresql.service" and local.port == 5433,
        f"{local.unit} {local.port}",
    )
    remote = postgres_gate(
        "postgres://db.internal:5432/hyper_service_x",
        unit_names=parse_unit_names(UBUNTU_LISTING),
    )
    check(
        "gate: remote database gets NO local unit dependency",
        remote.unit is None and remote.host == "db.internal",
        f"{remote.unit} / {remote.reason}",
    )
    check(
        "gate: remote reason explains the omission",
        "not this machine" in remote.reason,
        remote.reason,
    )
    missing = postgres_gate(
        "postgres://localhost/hyper_service_x",
        unit_names=parse_unit_names(NO_PG_LISTING),
    )
    check(
        "gate: no unit on the host still leaves the readiness gate",
        missing.unit is None and "no PostgreSQL unit is installed" in missing.reason,
        missing.reason,
    )
    check(
        "gate: hosts recognised as local",
        all(host_is_local(h) for h in ("localhost", "127.0.0.1", "::1", "/var/run/pg"))
        and not host_is_local("db.internal"),
        "",
    )
    no_binary = PostgresGate(
        host="localhost", port=5432, unit=None, pg_isready=None, reason="x"
    )
    check(
        "gate: no pg_isready binary means no ExecStartPre at all",
        no_binary.exec_start_pre is None,
        str(no_binary.exec_start_pre),
    )


# ── 5. dry run writes nothing ────────────────────────────────────────────────


def _artifact_snapshot(name: str) -> dict[str, str]:
    """Exactly the paths a REAL install writes inside a service's directory.

    Deliberately not a whole-tree walk: the suite runs files in parallel, and
    another test legitimately rewriting a different service's ``.env.local``
    (or the interpreter dropping a ``__pycache__`` entry) would make a tree
    snapshot fail for reasons that have nothing to do with ``--dry-run``.
    """
    service = get_service(name)
    snapshot = {
        "env.local": service.env_file.read_text(encoding="utf-8")
        if service.env_file.is_file()
        else "(absent)"
    }
    snapshot["runtime"] = (
        "\n".join(sorted(str(p) for p in service.runtime_dir.rglob("*")))
        if service.runtime_dir.is_dir()
        else "(absent)"
    )
    return snapshot


# A service no other test file writes secrets or runtime state for, so the
# "writes nothing" assertion below cannot be perturbed by a parallel test.
DRY_RUN_SERVICE = "blog_platform"


def test_dry_run_writes_nothing() -> None:
    """A dry run must not create or modify a single file."""
    before = _artifact_snapshot(DRY_RUN_SERVICE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyperdjango.cli",
            "service",
            "install",
            DRY_RUN_SERVICE,
            "--dry-run",
            "--enable",
            "--start",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        timeout=120,
    )
    after = _artifact_snapshot(DRY_RUN_SERVICE)
    check("dry-run: exits 0", result.returncode == 0, result.stderr[-400:])
    check(
        "dry-run: prints the unit file and the systemctl commands",
        "[Unit]" in result.stdout and "systemctl daemon-reload" in result.stdout,
        result.stdout[:200],
    )
    check(
        "dry-run: says plainly that nothing happened",
        "Nothing below has been written or executed." in result.stdout,
        "",
    )
    check(
        "dry-run: minted no secrets (.env.local untouched)",
        before["env.local"] == after["env.local"],
        f"{before['env.local'][:40]!r} -> {after['env.local'][:40]!r}",
    )
    check(
        "dry-run: created no runtime state",
        before["runtime"] == after["runtime"],
        f"{before['runtime'][:80]!r} -> {after['runtime'][:80]!r}",
    )
    check(
        "dry-run: secret values are redacted in the printed env file",
        "<generated at install>" in result.stdout or "<redacted," in result.stdout,
        [ln for ln in result.stdout.splitlines() if "SECRET_KEY" in ln][:2],
    )
    check(
        "dry-run: no unit file written into /etc",
        not Path(f"/etc/systemd/system/hyperdjango-{DRY_RUN_SERVICE}.service").exists(),
        "",
    )


def test_dry_run_redacts_secrets(prefix: Path) -> None:
    plan = _plan("hypersecret", prefix)
    preview = plan.units[1].env_preview
    check(
        "dry-run: signing keys are redacted in the preview",
        f"{SECRET_KEY_VAR}=<redacted" in preview,
        [ln for ln in preview.splitlines() if SECRET_KEY_VAR in ln],
    )
    check(
        "dry-run: a path that merely CONTAINS 'SECRET' is not redacted",
        "HYPERSECRET_DEMO_DIR=/" in preview,
        [ln for ln in preview.splitlines() if "DEMO_DIR" in ln],
    )
    check(
        "dry-run: secret keys come from the registry, not from name guessing",
        secret_env_keys(get_service("hypersecret"))
        >= {
            "HYPER_SESSION_SIGNING_KEY",
            "HYPER_ADMIN_SECRET",
            "HYPERSECRET_MANAGER_TOKEN",
            SECRET_KEY_VAR,
        },
        str(sorted(secret_env_keys(get_service("hypersecret")))),
    )
    check(
        "dry-run: a database password is masked out of DATABASE_URL",
        redact("DATABASE_URL", "postgres://u:hunter2@h/db", frozenset())
        == "postgres://u:<redacted>@h/db",
        redact("DATABASE_URL", "postgres://u:hunter2@h/db", frozenset()),
    )


# ── 6. install writes / uninstall removes ────────────────────────────────────


def test_install_then_uninstall(prefix: Path) -> None:
    scope = SystemdScope.resolve(user_mode=False, prefix=prefix)
    plan = _plan("hypersecret", prefix)
    written = write_plan(plan)

    check(
        "install: wrote 2 units + 2 env files + the target",
        len(written) == 5 and all(p.exists() for p in written),
        str([p.name for p in written]),
    )
    check(
        "install: env files are 0600, unit files 0644",
        all(
            (u.env_path.stat().st_mode & 0o777) == 0o600
            and (u.unit_path.stat().st_mode & 0o777) == 0o644
            for u in plan.units
        ),
        str([oct(u.env_path.stat().st_mode & 0o777) for u in plan.units]),
    )
    check(
        "install: the target file is the grouping unit",
        plan.target_path.read_text() == render_target()
        and plan.target_path.name == TARGET_NAME,
        plan.target_path.name,
    )
    check(
        "install: installed_unit_files sees exactly the two services",
        installed_unit_files(scope) == set(plan.unit_names),
        str(sorted(installed_unit_files(scope))),
    )

    selection = plan_uninstall("hypersecret", scope=scope)
    check(
        "uninstall: plans to remove both, keep nothing, orphan nothing",
        selection.remove == ("hypermanager", "hypersecret")
        and not selection.kept
        and not selection.orphaned,
        str(selection),
    )
    report = remove_unit_files(scope, list(selection.remove))
    check(
        "uninstall: removed every file install created",
        not any(p.exists() for p in written),
        str([str(p) for p in written if p.exists()]),
    )
    check(
        "uninstall: dropped the target once the last unit went",
        report.removed_target and TARGET_NAME not in str(installed_unit_files(scope)),
        str(report.removed_files),
    )
    check(
        "uninstall: leaves nothing behind in the unit dir",
        not list(scope.unit_dir.glob("hyperdjango*")),
        str(list(scope.unit_dir.glob("*"))),
    )
    check(
        "uninstall: removes the now-empty secrets directory too",
        not scope.env_dir.exists(),
        str(scope.env_dir),
    )


def test_uninstall_keeps_a_shared_companion(prefix: Path) -> None:
    """Uninstalling one parent must not silently break another that shares a
    companion — and must still remove the service the operator named."""
    scope = SystemdScope.resolve(user_mode=False, prefix=prefix)
    write_plan(_plan("hypersecret", prefix))
    # live_config declares BOTH hypersecret and hypermanager as companions, so
    # staging its unit makes hypermanager a shared companion.
    (scope.unit_dir / unit_name("live_config")).write_text("[Unit]\n")

    selection = plan_uninstall("hypersecret", scope=scope)
    check(
        "shared: the named service is still removed",
        selection.remove == ("hypersecret",),
        str(selection.remove),
    )
    check(
        "shared: the shared companion is kept for the other installed parent",
        selection.kept == ("hypermanager",),
        str(selection.kept),
    )
    check(
        "shared: the parent left needing the removed service is reported",
        selection.orphaned == ("live_config",),
        str(selection.orphaned),
    )
    report = remove_unit_files(scope, list(selection.remove))
    check(
        "shared: the companion's unit survives",
        (scope.unit_dir / unit_name("hypermanager")).exists(),
        "",
    )
    check(
        "shared: the target survives while units remain",
        not report.removed_target and (scope.unit_dir / TARGET_NAME).exists(),
        str(report.removed_target),
    )


def test_env_file_rendering() -> None:
    rendered = render_env_file({"B": "two", "A": "one", "C": "has space"})
    lines = [ln for ln in rendered.splitlines() if not ln.startswith("#")]
    check(
        "env: sorted, KEY=value, quoted only when needed",
        lines == ["A=one", "B=two", 'C="has space"'],
        str(lines),
    )


# ── 7. unknown name fails loudly ─────────────────────────────────────────────


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hyperdjango.cli", "service", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_unknown_name_fails_loudly() -> None:
    for verb in ("install", "uninstall"):
        result = _cli(verb, "not_a_service", "--dry-run")
        combined = result.stdout + result.stderr
        check(
            f"{verb}: unknown name exits non-zero",
            result.returncode != 0,
            str(result.returncode),
        )
        check(
            f"{verb}: the error names the unknown service",
            "not_a_service" in combined,
            combined[-200:],
        )
        check(
            f"{verb}: the error lists valid names",
            "hypersecret" in combined and "bookstore_api" in combined,
            combined[-200:],
        )


def test_install_help_is_discoverable() -> None:
    result = _cli("install", "--help")
    check("install --help exits 0", result.returncode == 0, result.stderr[-200:])
    for flag in ("--enable", "--start", "--user", "--dry-run", "--port", "--run-as"):
        check(f"install exposes {flag}", flag in result.stdout, result.stdout[:300])


def main() -> bool:
    with tempfile.TemporaryDirectory(prefix="hyper-systemd-") as raw:
        prefix = Path(raw)

        print("\n== plain database-backed service ==")
        test_plain_db_unit(prefix / "plain")
        test_port_override(prefix / "port")
        test_user_scope_omits_user_directive(prefix / "usermode")

        print("\n== service with no database ==")
        test_no_db_unit(prefix / "nodb")

        print("\n== companion pair ==")
        test_companion_pair(prefix / "companion")
        test_shared_companion_partof_accumulates(prefix / "shared-partof")

        print("\n== postgresql detection ==")
        test_pg_detection()
        test_pg_gate_shapes()

        print("\n== dry run ==")
        test_dry_run_writes_nothing()
        test_dry_run_redacts_secrets(prefix / "redact")

        print("\n== install / uninstall ==")
        test_install_then_uninstall(prefix / "install")
        test_uninstall_keeps_a_shared_companion(prefix / "shared")
        test_env_file_rendering()

        print("\n== unknown names + cli surface ==")
        test_unknown_name_fails_loudly()
        test_install_help_is_discoverable()

    return finish()


if __name__ == "__main__":
    run_main(main)
