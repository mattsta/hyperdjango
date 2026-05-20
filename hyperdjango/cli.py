"""
HyperDjango CLI — the `hyper` command.

Usage:
    hyper new myapp          # Scaffold a new project
    hyper run                # Start dev server
    hyper run --prod         # Production mode
    hyper routes             # List all routes
    hyper check              # Check feature availability
"""

import argparse
import asyncio
import code
import contextlib
import getpass
import importlib
import inspect
import json
import math
import os
import re
import readline  # noqa: F401 — enables arrow keys/history in REPL
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from urllib.parse import urlparse

import hyperdjango
from hyperdjango.conf import get_setting, resolve_database_url
from hyperdjango.logging import logger

# NOTE: heavy subsystem imports (auth, database, fixtures, migrations,
# staticfiles) are intentionally NOT imported at module level. Importing
# hyperdjango.auth alone costs ~77ms and the whole set ~105ms — paid on EVERY
# `hyper version` / `hyper --help` / `hyper doctor` invocation, none of which
# touch a database. They are imported lazily inside the subcommands that use
# them so the common, DB-free commands start fast. Verify with:
#   python -X importtime -c "import hyperdjango.cli"

_VARCHAR_TYPE_NAMES = frozenset({"varchar", "bpchar"})


def main():
    parser = argparse.ArgumentParser(
        prog="hyper",
        description="HyperDjango — hypermodern web framework",
    )
    subparsers = parser.add_subparsers(dest="command")

    # hyper new
    new_parser = subparsers.add_parser("new", help="Create a new project")
    new_parser.add_argument("name", help="Project name")
    new_parser.add_argument(
        "--with-db", action="store_true", help="Include database setup"
    )
    new_parser.add_argument(
        "--with-auth", action="store_true", help="Include auth (sessions, login)"
    )
    new_parser.add_argument(
        "--with-admin", action="store_true", help="Include admin interface"
    )
    new_parser.add_argument(
        "--full", action="store_true", help="Include everything (db + auth + admin)"
    )

    # hyper run
    run_parser = subparsers.add_parser("run", help="Start the dev server")
    # default=None so an unspecified flag falls through to the HOST/PORT setting
    # (HYPER_HOST/HYPER_PORT env or Django settings); app.run() applies the
    # 127.0.0.1:8000 literal fallback. An explicit flag still wins.
    run_parser.add_argument(
        "--host", default=None, help="Host to bind (default: HOST setting or 127.0.0.1)"
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind (default: PORT setting or 8000)",
    )
    run_parser.add_argument("--prod", action="store_true", help="Production mode")
    run_parser.add_argument(
        "--app", default="app:app", help="App import path (module:variable)"
    )

    # hyper release
    release_parser = subparsers.add_parser(
        "release",
        help="Mint the next release stamp (UTC-ms version, forward-only)",
    )
    release_parser.add_argument(
        "--apply",
        action="store_true",
        help="write the minted stamp into pyproject.toml [project] version",
    )
    release_parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="path to the pyproject.toml to read the previous version from "
        "(and rewrite with --apply)",
    )

    # hyper routes
    routes_parser = subparsers.add_parser("routes", help="List all routes")
    routes_parser.add_argument(
        "--app", default="app:app", help="App import path (module:variable)"
    )
    routes_parser.add_argument(
        "--format",
        choices=("tabular", "stacked", "json"),
        default="tabular",
        help="Output format (default: tabular)",
    )
    routes_parser.add_argument(
        "--prefix", default=None, help="Only show routes whose pattern starts with this"
    )
    routes_parser.add_argument(
        "--unsorted",
        action="store_true",
        help="Preserve registration order (default: sorted by pattern)",
    )

    # hyper check
    subparsers.add_parser("check", help="Check feature availability")

    # hyper doctor
    doctor_parser = subparsers.add_parser("doctor", help="Diagnose platform health")
    doctor_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    doctor_parser.add_argument(
        "--no-db", action="store_true", help="Skip database checks"
    )
    doctor_parser.add_argument("--json", action="store_true", help="JSON output")
    doctor_parser.add_argument("--ci", action="store_true", help="CI-friendly output")
    doctor_parser.add_argument(
        "--verbose", action="store_true", help="Show all details"
    )
    doctor_parser.add_argument("--category", default=None, help="Run only one category")

    # hyper createsuperuser
    su_parser = subparsers.add_parser("createsuperuser", help="Create a superuser")
    su_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    su_parser.add_argument("--username", default=None, help="Username (skip prompt)")
    su_parser.add_argument("--email", default=None, help="Email (skip prompt)")
    su_parser.add_argument(
        "--noinput",
        action="store_true",
        help="Non-interactive mode (requires --username and password via env)",
    )

    # hyper makemigrations
    mm_parser = subparsers.add_parser(
        "makemigrations", help="Generate migration from model changes"
    )
    mm_parser.add_argument("--name", default="auto", help="Migration name")
    mm_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    mm_parser.add_argument(
        "--app", default=None, help="App import path to load models (module:variable)"
    )
    mm_parser.add_argument(
        "--dry-run", action="store_true", help="Show SQL without writing file"
    )
    mm_parser.add_argument("--dir", default="migrations", help="Migrations directory")

    # hyper migrate
    mig_parser = subparsers.add_parser("migrate", help="Apply pending migrations")
    mig_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    mig_parser.add_argument(
        "--app", default=None, help="App import path to load models"
    )
    mig_parser.add_argument(
        "--fake", default=None, help="Mark migration as applied without executing"
    )
    mig_parser.add_argument(
        "--dry-run", action="store_true", help="Show SQL without applying"
    )
    mig_parser.add_argument("--dir", default="migrations", help="Migrations directory")

    # hyper showmigrations
    sm_parser = subparsers.add_parser(
        "showmigrations", help="List migrations with applied status"
    )
    sm_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    sm_parser.add_argument("--dir", default="migrations", help="Migrations directory")

    # hyper rollback
    rb_parser = subparsers.add_parser("rollback", help="Rollback most recent migration")
    rb_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    rb_parser.add_argument(
        "--target", default=None, help="Rollback to this migration (exclusive)"
    )
    rb_parser.add_argument("--dir", default="migrations", help="Migrations directory")

    # hyper db (subcommands: verify, snapshot, drift)
    db_parser = subparsers.add_parser("db", help="Database management commands")
    db_subs = db_parser.add_subparsers(dest="db_command")

    db_verify = db_subs.add_parser("verify", help="Verify models match live DB schema")
    db_verify.add_argument("--database", default=None, help="Database URL")
    db_verify.add_argument("--app", default=None, help="App import path")

    db_snapshot = db_subs.add_parser("snapshot", help="Save current schema snapshot")
    db_snapshot.add_argument("--database", default=None, help="Database URL")
    db_snapshot.add_argument("--dir", default="migrations", help="Migrations directory")

    # hyper db doctor — layered PostgreSQL environment diagnosis
    db_doctor_parser = db_subs.add_parser(
        "doctor",
        help="Diagnose the PostgreSQL environment (connectivity, auth, "
        "privileges, extensions, capacity) with exact remediation",
    )
    db_doctor_parser.add_argument("--database", default=None, help="Database URL")

    # hyper db extensions { list | ensure }
    db_ext = db_subs.add_parser(
        "extensions",
        help="Manage required PostgreSQL extensions (pgvector, pg_trgm, ...)",
    )
    db_ext_subs = db_ext.add_subparsers(dest="ext_command")
    db_ext_subs.add_parser("list", help="Print the declared extension registry")
    db_ext_ensure = db_ext_subs.add_parser(
        "ensure", help="CREATE EXTENSION IF NOT EXISTS for each declared extension"
    )
    db_ext_ensure.add_argument("--database", default=None, help="Database URL")
    db_ext_ensure.add_argument(
        "--only", nargs="*", default=None, help="Limit to specific extension names"
    )

    # hyper collectstatic
    cs_parser = subparsers.add_parser(
        "collectstatic", help="Collect static files with content hashes"
    )
    cs_parser.add_argument(
        "--static-dirs",
        nargs="+",
        default=["static"],
        help="Source directories for static files",
    )
    cs_parser.add_argument(
        "--static-root", default="staticfiles", help="Destination directory"
    )
    cs_parser.add_argument(
        "--clear", action="store_true", help="Clear static root before collecting"
    )
    cs_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be collected without writing",
    )
    cs_parser.add_argument(
        "--no-post-process", action="store_true", help="Skip CSS/JS URL rewriting"
    )
    cs_parser.add_argument(
        "--app", default=None, help="App import path to load (for static_dirs config)"
    )

    # hyper shell
    shell_parser = subparsers.add_parser(
        "shell", help="Interactive Python shell with auto-imports"
    )
    shell_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    shell_parser.add_argument(
        "--app", default=None, help="App import path to load models"
    )
    shell_parser.add_argument(
        "-c", "--command", default=None, help="Execute a command and exit"
    )

    # hyper dbshell
    dbshell_parser = subparsers.add_parser(
        "dbshell", help="Open psql connected to the database"
    )
    dbshell_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )

    # hyper inspectdb
    idb_parser = subparsers.add_parser(
        "inspectdb", help="Generate Model classes from existing DB tables"
    )
    idb_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    idb_parser.add_argument(
        "--table",
        nargs="*",
        default=None,
        help="Specific tables to inspect (default: all)",
    )
    idb_parser.add_argument(
        "--schema", default="public", help="Database schema (default: public)"
    )
    idb_parser.add_argument(
        "--include-views",
        action="store_true",
        help="Include views and materialized views",
    )
    idb_parser.add_argument(
        "--output", default=None, help="Write output to file instead of stdout"
    )

    # hyper dumpdata
    dump_parser = subparsers.add_parser(
        "dumpdata", help="Dump model data to JSON fixtures"
    )
    dump_parser.add_argument(
        "models", nargs="*", help="Model table names to dump (all if omitted)"
    )
    dump_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    dump_parser.add_argument(
        "--output", "-o", default=None, help="Write to file instead of stdout"
    )
    dump_parser.add_argument(
        "--indent", type=int, default=2, help="JSON indentation (default: 2)"
    )
    dump_parser.add_argument(
        "--natural-key",
        nargs="*",
        default=None,
        help="Use natural key fields instead of PKs (only with single model)",
    )

    # hyper loaddata
    load_parser = subparsers.add_parser(
        "loaddata", help="Load JSON fixtures into database"
    )
    load_parser.add_argument("fixture", help="Path to fixture JSON file")
    load_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )

    # hyper setup
    setup_parser = subparsers.add_parser(
        "setup", help="Create tables from models and optionally seed data"
    )
    setup_parser.add_argument(
        "--app", default="app:app", help="App import path (module:variable)"
    )
    setup_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    setup_parser.add_argument(
        "--seed",
        default=None,
        help="Seed module to run after table creation (e.g., seed:run)",
    )
    setup_parser.add_argument(
        "--drop", action="store_true", help="Drop and recreate tables (DESTRUCTIVE)"
    )

    # hyper start (daemonized)
    start_parser = subparsers.add_parser("start", help="Start server in background")
    start_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    start_parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    start_parser.add_argument("--app", default="app:app", help="App import path")
    start_parser.add_argument("--prod", action="store_true", help="Production mode")

    # hyper stop
    stop_parser = subparsers.add_parser("stop", help="Stop running server gracefully")
    stop_parser.add_argument(
        "--port", type=int, default=8000, help="Port of server to stop"
    )
    stop_parser.add_argument(
        "--timeout", type=int, default=30, help="Seconds to wait before SIGKILL"
    )

    # hyper restart
    restart_parser = subparsers.add_parser(
        "restart", help="Restart server (stop + start)"
    )
    restart_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    restart_parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    restart_parser.add_argument("--app", default="app:app", help="App import path")
    restart_parser.add_argument("--prod", action="store_true", help="Production mode")
    restart_parser.add_argument("--timeout", type=int, default=30, help="Stop timeout")

    # hyper status
    status_parser = subparsers.add_parser("status", help="Check if server is running")
    status_parser.add_argument("--port", type=int, default=8000, help="Port to check")

    # hyper systemd
    systemd_parser = subparsers.add_parser("systemd", help="Manage systemd service")
    systemd_sub = systemd_parser.add_subparsers(dest="systemd_command")
    sd_install = systemd_sub.add_parser(
        "install", help="Generate and install systemd unit"
    )
    sd_install.add_argument("--host", default="0.0.0.0", help="Host to bind")
    sd_install.add_argument("--port", type=int, default=8000, help="Port to bind")
    sd_install.add_argument("--app", default="app:app", help="App import path")
    sd_install.add_argument(
        "--name", default=None, help="Service name (default: directory name)"
    )
    sd_install.add_argument("--user", default=None, help="Run as user")
    sd_install.add_argument(
        "--enable", action="store_true", help="Enable and start immediately"
    )
    systemd_sub.add_parser("uninstall", help="Remove systemd unit")
    systemd_sub.add_parser("status", help="Show service status")

    # hyper benchmark
    bench_parser = subparsers.add_parser(
        "benchmark", help="Run EXPLAIN ANALYZE performance benchmarks"
    )
    bench_parser.add_argument(
        "--database", default=None, help="Database URL (or set DATABASE_URL)"
    )
    bench_parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Save results as baseline for regression detection",
    )
    bench_parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )
    bench_parser.add_argument(
        "--threshold",
        type=float,
        default=2.0,
        help="Regression threshold multiplier (default: 2.0x)",
    )
    bench_parser.add_argument(
        "--posts",
        type=int,
        default=50000,
        help="Number of posts to seed (default: 50000)",
    )

    # hyper service
    service_parser = subparsers.add_parser(
        "service", help="List, inspect and run the bundled services"
    )
    service_sub = service_parser.add_subparsers(dest="service_command")
    service_sub.add_parser("list", help="Table of every bundled service")
    svc_info = service_sub.add_parser(
        "info", help="Everything needed to run one service, manual commands included"
    )
    svc_info.add_argument("name", help="Service name (see `hyper service list`)")
    svc_run = service_sub.add_parser(
        "run", help="Set up, seed and serve a service (and its companions)"
    )
    svc_run.add_argument("name", help="Service name (see `hyper service list`)")
    svc_run.add_argument(
        "--port", type=int, default=None, help="Override the service's default port"
    )
    svc_run.add_argument(
        "--no-seed", action="store_true", help="Create tables but skip the seed data"
    )
    svc_run.add_argument(
        "--fresh",
        action="store_true",
        help="Drop and recreate tables before seeding (DESTRUCTIVE)",
    )
    svc_stop = service_sub.add_parser("stop", help="Stop a service and its companions")
    svc_stop.add_argument("name", help="Service name (see `hyper service list`)")
    svc_install = service_sub.add_parser(
        "install",
        help="Install systemd units for a service (and its companions)",
    )
    svc_install.add_argument("name", help="Service name (see `hyper service list`)")
    svc_install.add_argument(
        "--port", type=int, default=None, help="Override the service's default port"
    )
    svc_install.add_argument(
        "--host", default="127.0.0.1", help="Bind address for the unit (ExecStart)"
    )
    svc_install.add_argument(
        "--run-as", default=None, help="System account to run as (default: your user)"
    )
    svc_install.add_argument(
        "--user",
        action="store_true",
        help="Install a `systemctl --user` unit instead of a system unit (no root)",
    )
    svc_install.add_argument(
        "--enable", action="store_true", help="Enable the units to start at boot"
    )
    svc_install.add_argument(
        "--start", action="store_true", help="Start the units immediately"
    )
    svc_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the unit files and systemctl commands; change nothing",
    )
    svc_install.add_argument(
        "--no-setup",
        action="store_true",
        help="Skip `hyper setup` (tables + seed); assume the database is ready",
    )
    svc_uninstall = service_sub.add_parser(
        "uninstall", help="Remove the systemd units `service install` created"
    )
    svc_uninstall.add_argument("name", help="Service name (see `hyper service list`)")
    svc_uninstall.add_argument(
        "--user", action="store_true", help="Operate on `systemctl --user` units"
    )
    svc_uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed; change nothing",
    )

    # hyper version
    subparsers.add_parser("version", help="Show version")

    parser.epilog = (
        "Custom commands: functions registered with @hyperdjango.commands.command "
        "are runnable as `hyper <name>`. Point HYPER_COMMANDS at a comma-separated "
        "list of modules to import (or keep them in a top-level `commands` module) "
        "and run `hyper <name> --help`."
    )

    # Custom management commands (hyperdjango.commands.@command) are not argparse
    # subcommands — argparse would reject them with "invalid choice". Intercept a
    # leading token that is not a built-in subcommand (and not a flag) and route
    # it through the command registry instead. `--help`/no-args still fall through
    # to argparse so built-in help is unaffected.
    builtin_commands = set(subparsers.choices.keys())
    raw_argv = sys.argv[1:]
    if (
        raw_argv
        and not raw_argv[0].startswith("-")
        and raw_argv[0] not in builtin_commands
    ):
        _dispatch_custom_command(raw_argv[0], raw_argv[1:])
        return

    args = parser.parse_args()

    if args.command == "new":
        with_db = args.with_db or args.full
        with_auth = args.with_auth or args.full
        with_admin = args.with_admin or args.full
        cmd_new(args.name, with_db=with_db, with_auth=with_auth, with_admin=with_admin)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "release":
        return cmd_release(args)
    elif args.command == "routes":
        cmd_routes(args)
    elif args.command == "check":
        cmd_check()
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "createsuperuser":
        cmd_createsuperuser(args)
    elif args.command == "makemigrations":
        cmd_makemigrations(args)
    elif args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "showmigrations":
        cmd_showmigrations(args)
    elif args.command == "rollback":
        cmd_rollback(args)
    elif args.command == "db":
        cmd_db(args)
    elif args.command == "collectstatic":
        cmd_collectstatic(args)
    elif args.command == "shell":
        cmd_shell(args)
    elif args.command == "dbshell":
        cmd_dbshell(args)
    elif args.command == "inspectdb":
        cmd_inspectdb(args)
    elif args.command == "dumpdata":
        cmd_dumpdata(args)
    elif args.command == "loaddata":
        cmd_loaddata(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "restart":
        cmd_restart(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "systemd":
        cmd_systemd(args)
    elif args.command == "service":
        return cmd_service(args)
    elif args.command == "benchmark":
        from hyperdjango.benchmark import run_benchmark

        run_benchmark(args)
    elif args.command == "version":
        cmd_version()
    else:
        parser.print_help()


def cmd_new(name, *, with_db=False, with_auth=False, with_admin=False):
    """Scaffold a new project.

    Presets:
        --with-db:    DATABASE_URL, models.py, migrations setup
        --with-auth:  User model, login/logout, session middleware (implies --with-db)
        --with-admin: HyperAdmin registration, admin routes (implies --with-auth)
        --full:       All of the above
    """
    # Implication chain: admin → auth → db
    if with_admin:
        with_auth = True
    if with_auth:
        with_db = True

    project_dir = Path(name)
    if project_dir.exists():
        logger.error("Directory '{name}' already exists", name=name)
        sys.exit(1)

    project_dir.mkdir()
    (project_dir / "views").mkdir()
    (project_dir / "templates").mkdir()
    (project_dir / "static").mkdir()

    # ── app.py ──
    app_imports = ["from hyperdjango import HyperApp, Response"]
    app_init_args = [
        f'    title="{name}",',
        '    views="views",',
        '    templates="templates",',
        '    static="static",',
        "    debug=True,",
    ]
    app_setup_lines = []
    app_routes = []

    if with_db:
        app_imports.append("from hyperdjango.database import Database")
        # No explicit database= — the framework resolves the connection URL from
        # DATABASE_URL / HYPER_DATABASE_URL / the libpq PG* set (see .env.example),
        # so the server, CLI, and driver all agree from one variable.

    if with_auth:
        app_imports.append("from hyperdjango.auth import SessionAuth")
        app_imports.append("from hyperdjango.conf import get_setting")
        app_imports.append(
            "from hyperdjango.auth.db_sessions import DatabaseSessionStore"
        )
        app_setup_lines.extend(
            [
                "",
                "# Auth setup — db= installs the RBAC checker so @require_permission works",
                "session_store = DatabaseSessionStore(app.db)",
                'auth = SessionAuth(secret=get_setting("SESSION_SECRET"), store=session_store, db=app.db)',
                "app.use(auth)",
            ]
        )
        app_routes.extend(
            [
                "",
                '@app.get("/login")',
                "async def login_page(request):",
                "    return Response.html(\"<h1>Login</h1><form method='post'><input name='username'><input name='password' type='password'><button>Login</button></form>\")",
            ]
        )

    if with_admin:
        app_imports.append("from hyperdjango.admin import HyperAdmin")
        app_setup_lines.extend(
            [
                "",
                "# Admin setup",
                'admin = HyperAdmin(app, prefix="/admin")',
                "admin.register_auth_models()",
            ]
        )

    # Health checks always included
    app_setup_lines.extend(
        [
            "",
            "# Health checks",
            "app.mount_health()",
        ]
    )

    app_content = "\n".join(app_imports) + "\n\n"
    app_content += "app = HyperApp(\n" + "\n".join(app_init_args) + "\n)\n"
    app_content += "\n".join(app_setup_lines) + "\n"
    app_content += "\n\n"
    app_content += f'@app.get("/")\nasync def index(request):\n    return Response.json({{"message": "Welcome to {name}!"}})\n'
    app_content += "\n".join(app_routes) + "\n"
    app_content += '\n\nif __name__ == "__main__":\n    app.run()\n'

    (project_dir / "app.py").write_text(app_content)

    # ── views/index.py ──
    (project_dir / "views" / "index.py").write_text("""from hyperdjango import Response

async def get(request):
    return Response.html("<h1>Hello from HyperDjango!</h1>")
""")

    # ── models.py (if --with-db) ──
    if with_db:
        models_content = """from hyperdjango.models import Model, Field

# Define your models here. Example:
# class Product(Model):
#     class Meta:
#         table = "products"
#     id: int = Field(primary_key=True, auto=True)
#     name: str = Field(max_length=200)
#     price: float = Field(default=0.0)
#     is_active: bool = Field(default=True)
"""
        (project_dir / "models.py").write_text(models_content)

    # ── .env.example (if --with-db) ──
    if with_db:
        env_name = name.lower().replace("-", "_")
        (project_dir / ".env.example").write_text(
            "# Database — set ANY ONE of these; the framework resolves them in\n"
            "# order (HYPER_DATABASE_URL, then DATABASE_URL, then the libpq PG*\n"
            "# set: PGDATABASE/PGHOST/PGUSER/...) and every component agrees.\n"
            f"DATABASE_URL=postgres://localhost/{env_name}\n"
            "HYPER_DEBUG=true\n"
        )

    # ── pyproject.toml ──
    (project_dir / "pyproject.toml").write_text(f'''[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "hyperdjango",
]

[dependency-groups]
dev = ["pytest"]
''')

    # ── .python-version ──
    (project_dir / ".python-version").write_text("3.14t\n")

    # ── Makefile ──
    makefile_lines = [
        ".PHONY: run test build\n",
        "run:",
        "\tuv run hyper run\n",
        "test:",
        "\tuv run pytest\n",
        "build:",
        "\tuv run hyper-build --install --release\n",
    ]
    if with_db:
        makefile_lines.extend(
            [
                "migrate:",
                "\tuv run hyper migrate\n",
                "makemigrations:",
                "\tuv run hyper makemigrations\n",
            ]
        )
    if with_auth:
        makefile_lines.extend(
            [
                "createsuperuser:",
                "\tuv run hyper createsuperuser\n",
            ]
        )
    (project_dir / "Makefile").write_text("\n".join(makefile_lines))

    # ── .gitignore ──
    (project_dir / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.env\n*.so\n.venv/\nstaticfiles/\n*.egg-info/\ndist/\nbuild/\n"
    )

    # ── README.md ──
    readme_lines = [
        f"# {name}\n",
        "Built with [HyperDjango](https://github.com/hyperdjango/hyperdjango).\n",
        "## Quick Start\n",
        "```bash",
        "uv sync",
    ]
    if with_db:
        readme_lines.extend(
            ["cp .env.example .env  # configure DATABASE_URL", "uv run hyper migrate"]
        )
    if with_auth:
        readme_lines.append("uv run hyper createsuperuser")
    readme_lines.extend(["uv run hyper run", "```\n"])
    if with_admin:
        readme_lines.append("Admin UI at http://localhost:8000/admin/\n")
    (project_dir / "README.md").write_text("\n".join(readme_lines))

    # ── templates/login.html (if --with-auth) ──
    if with_auth:
        (project_dir / "templates" / "login.html").write_text(
            "<!DOCTYPE html>\n<html>\n<head><title>Login</title></head>\n"
            '<body style="font-family:system-ui;max-width:400px;margin:4em auto;">\n'
            "<h1>Login</h1>\n"
            '<form method="post">\n'
            '  <div style="margin-bottom:1em;">\n'
            "    <label>Username</label><br>\n"
            '    <input name="username" required style="width:100%;padding:8px;">\n'
            "  </div>\n"
            '  <div style="margin-bottom:1em;">\n'
            "    <label>Password</label><br>\n"
            '    <input name="password" type="password" required style="width:100%;padding:8px;">\n'
            "  </div>\n"
            '  <button type="submit" style="padding:8px 24px;">Login</button>\n'
            "</form>\n"
            "</body>\n</html>\n"
        )

    # Print summary
    features = []
    if with_db:
        features.append("database")
    if with_auth:
        features.append("auth")
    if with_admin:
        features.append("admin")
    feature_str = f" ({', '.join(features)})" if features else ""

    logger.success(
        "Created project '{name}'{feature_str}", name=name, feature_str=feature_str
    )
    logger.opt(raw=True).info(f"  cd {name}\n")
    logger.opt(raw=True).info("  uv sync\n")
    if with_db:
        logger.opt(raw=True).info("  cp .env.example .env  # configure DATABASE_URL\n")
        logger.opt(raw=True).info("  uv run hyper migrate\n")
    if with_auth:
        logger.opt(raw=True).info("  uv run hyper createsuperuser\n")
    logger.opt(raw=True).info("  uv run hyper run\n")
    if with_admin:
        logger.opt(raw=True).info("  # Admin at http://localhost:8000/admin/\n")


def cmd_run(args):
    """Start the development server."""
    app = _load_app(args.app)
    app.run(host=args.host, port=args.port, prod=args.prod)


# ── Server lifecycle commands ─────────────────────────────────────────────


def _pid_file(port: int) -> Path:
    return Path(f".hyper.{port}.pid")


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid(port: int) -> int | None:
    pf = _pid_file(port)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        return pid if _is_process_alive(pid) else None
    except ValueError, OSError:
        return None


def cmd_start(args):
    """Start the server in the background (daemonized)."""
    existing = _read_pid(args.port)
    if existing:
        logger.error(
            "Server already running on port {port} (PID {pid})",
            port=args.port,
            pid=existing,
        )
        sys.exit(1)

    cmd = [
        sys.executable,
        "-m",
        "hyperdjango.cli",
        "run",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--app",
        args.app,
    ]
    if args.prod:
        cmd.append("--prod")

    log_path = Path(f".hyper.{args.port}.log")
    log_file = log_path.open("a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Write PID file
    _pid_file(args.port).write_text(str(proc.pid))
    logger.success(
        "Started HyperDjango on {host}:{port} (PID {pid})",
        host=args.host,
        port=args.port,
        pid=proc.pid,
    )
    logger.info("Log: {log_path}", log_path=log_path.resolve())


def cmd_stop(args):
    """Stop a running server gracefully via SIGTERM."""
    import signal as _signal

    pf = _pid_file(args.port)
    pid = _read_pid(args.port)
    if not pid:
        logger.error("No server running on port {port}", port=args.port)
        sys.exit(1)

    logger.info("Stopping server (PID {pid})...", pid=pid)
    os.kill(pid, _signal.SIGTERM)

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            pf.unlink(missing_ok=True)
            logger.success("Server stopped gracefully.")
            return
        time.sleep(0.2)

    logger.warning("Timeout ({timeout}s) — sending SIGKILL", timeout=args.timeout)
    os.kill(pid, _signal.SIGKILL)
    time.sleep(0.5)
    pf.unlink(missing_ok=True)
    logger.info("Server killed.")


def cmd_restart(args):
    """Stop and start the server."""
    pid = _read_pid(args.port)
    if pid:
        args_stop = type("Args", (), {"port": args.port, "timeout": args.timeout})()
        cmd_stop(args_stop)
        time.sleep(0.5)
    cmd_start(args)


def cmd_status(args):
    """Check if a server is running."""
    pid = _read_pid(args.port)
    if pid:
        logger.info(
            "Server running on port {port} (PID {pid})", port=args.port, pid=pid
        )
    else:
        pf = _pid_file(args.port)
        if pf.exists():
            logger.warning("Stale PID file (server not running)")
            pf.unlink(missing_ok=True)
        else:
            logger.info("No server running on port {port}", port=args.port)
        sys.exit(1)


def cmd_systemd(args):
    """Manage systemd service integration."""
    from hyperdjango.systemd import (
        systemd_install,
        systemd_status,
        systemd_uninstall,
    )

    if args.systemd_command == "install":
        systemd_install(args)
    elif args.systemd_command == "uninstall":
        systemd_uninstall(args)
    elif args.systemd_command == "status":
        systemd_status(args)
    else:
        logger.error("Usage: hyper systemd {{install|uninstall|status}}")
        sys.exit(1)


@dataclass(slots=True)
class RouteInfo:
    """Resolved metadata for a single registered route (CLI / JSON output)."""

    method: str
    pattern: str
    name: str
    view: str

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain dict for JSON output."""
        return {
            "method": self.method,
            "pattern": self.pattern,
            "name": self.name,
            "view": self.view,
        }


def collect_route_info(
    app, *, prefix: str | None = None, unsorted: bool = False
) -> list[RouteInfo]:
    """Build the resolved route list for an app.

    Resolves each route's view path, filters by pattern prefix, and orders the
    result (sorted by pattern unless ``unsorted`` preserves registration order).
    """
    if app.views_dir:
        app.discover_routes()

    routes = app.router.routes()
    if not unsorted:
        routes = sorted(routes, key=lambda r: r.pattern)

    infos: list[RouteInfo] = []
    for route in routes:
        if prefix is not None and not route.pattern.startswith(prefix):
            continue
        infos.append(
            RouteInfo(
                method=route.method,
                pattern=route.pattern,
                name=route.name or "",
                view=route.view_path,
            )
        )
    return infos


def cmd_routes(args):
    """List all registered routes."""
    app = _load_app(args.app)
    infos = collect_route_info(app, prefix=args.prefix, unsorted=args.unsorted)

    if args.format == "json":
        payload = [info.to_dict() for info in infos]
        logger.opt(raw=True).info(json.dumps(payload, indent=2) + "\n")
        return

    if not infos:
        logger.info("No routes registered.")
        return

    if args.format == "stacked":
        for info in infos:
            logger.opt(raw=True).info(f"{info.method} {info.pattern}\n")
            logger.opt(raw=True).info(f"    name: {info.name or '-'}\n")
            logger.opt(raw=True).info(f"    view: {info.view}\n")
        return

    # tabular (default)
    logger.opt(raw=True).info(f"{'METHOD':<8} {'PATTERN':<40} {'NAME':<20} {'VIEW'}\n")
    logger.opt(raw=True).info(f"{'-' * 90}\n")
    for info in infos:
        logger.opt(raw=True).info(
            f"{info.method:<8} {info.pattern:<40} {info.name:<20} {info.view}\n"
        )


def cmd_doctor(args):
    """Run platform health diagnostics."""
    from hyperdjango.doctor import run_doctor
    from hyperdjango.doctor._registry import CheckStatus

    db_url = args.database or resolve_database_url()
    output_format = "json" if args.json else ("ci" if args.ci else "terminal")

    report = run_doctor(
        database_url=db_url,
        verbose=args.verbose,
        skip_db=args.no_db,
        output_format=output_format,
        category_filter=args.category or "",
    )

    if args.ci:
        # A security misconfiguration is only ever a WARN (weak/absent secret,
        # insecure cookies, missing CSRF), so gating CI on total_failed alone
        # let insecure deployments pass green. Treat any non-PASS security check
        # as a hard CI failure so misconfig actually blocks the pipeline.
        security_issues = [
            chk
            for cat in report.categories
            if cat.name == "security"
            for chk in cat.checks
            if chk.status in (CheckStatus.WARN, CheckStatus.FAIL)
        ]
        if report.total_failed > 0 or security_issues:
            # Say WHY the gate tripped — a summary line reading "0 failed"
            # followed by a silent exit 1 is indistinguishable from a crash.
            if security_issues:
                names = ", ".join(chk.name for chk in security_issues)
                logger.error(
                    f"CI gate: {len(security_issues)} security check(s) below "
                    f"PASS block the pipeline: {names}. Security warnings are "
                    "hard failures under --ci; fix the config (see hints "
                    "above) to proceed."
                )
            if report.total_failed > 0:
                logger.error(f"CI gate: {report.total_failed} check(s) FAILED.")
            sys.exit(1)


def cmd_check():
    """Check feature availability."""
    logger.opt(raw=True).info("HyperDjango Feature Check\n")
    logger.opt(raw=True).info(f"{'=' * 40}\n")

    # Native extension
    logger.opt(raw=True).info("\n  Native Extension (_hyperdjango_native):\n")
    logger.opt(raw=True).info(
        "  [OK] Compiled — pg.zig + turbonet + SIMD JSON active\n"
    )

    # Core components
    logger.opt(raw=True).info("\n  Components:\n")
    for name in [
        "validation engine (self-contained)",
        "pg.zig database (native Postgres)",
        "turbonet HTTP server (Zig)",
        "SIMD JSON serialization",
        "native template engine",
    ]:
        logger.opt(raw=True).info(f"  [OK] {name}\n")

    # Optional
    logger.opt(raw=True).info("\n  Optional:\n")
    for pkg, role in [("argon2", "password hashing"), ("jinja2", "templates")]:
        try:
            __import__(pkg)
            logger.opt(raw=True).info(f"  [OK] {pkg} ({role})\n")
        except ImportError:
            logger.opt(raw=True).info(f"  [--] {pkg} ({role})\n")

    logger.opt(raw=True).info(f"\n  hyperdjango {hyperdjango.__version__}\n")


def cmd_createsuperuser(args):
    """Create a superuser interactively or via args."""
    from hyperdjango.auth.passwords import hash_password
    from hyperdjango.auth.user import ensure_rbac_tables
    from hyperdjango.auth.validators import validate_password
    from hyperdjango.database import Database, set_db

    db_url = args.database or resolve_database_url()
    if not db_url:
        logger.error("No database URL. Pass --database or set DATABASE_URL.")
        sys.exit(1)

    async def _create():
        db = Database(db_url)
        await db.connect()
        set_db(db)

        # Ensure auth tables exist (ORM-based DDL from Model definitions)
        await ensure_rbac_tables(db=db)

        # Gather input
        username = args.username
        email = args.email

        if not args.noinput:
            if not username:
                username = input("Username: ").strip()
                if not username:
                    logger.error("Username cannot be blank.")
                    sys.exit(1)

            if not email:
                email = input("Email address: ").strip()

            # Check if username already exists
            existing = await db.query_one(
                "SELECT id FROM hyper_users WHERE username = $1", username
            )
            if existing:
                logger.error("User '{username}' already exists.", username=username)
                sys.exit(1)

            # Password prompt with validation
            while True:
                password = getpass.getpass("Password: ")
                password2 = getpass.getpass("Password (again): ")
                if password != password2:
                    logger.error("Passwords don't match. Try again.")
                    continue

                errors = validate_password(password)
                if errors:
                    logger.error("Password validation errors:")
                    for e in errors:
                        logger.opt(raw=True).info(f"  - {e}\n")
                    bypass = input("Bypass password validation? [y/N] ").strip().lower()
                    if bypass == "y":
                        break
                    continue
                break
        else:
            # Non-interactive mode
            if not username:
                logger.error("--username required with --noinput")
                sys.exit(1)
            email = email or ""
            password = str(get_setting("SUPERUSER_PASSWORD") or "")
            if not password:
                logger.error("Set HYPER_SUPERUSER_PASSWORD env var with --noinput")
                sys.exit(1)

        # Create the user via ORM
        from hyperdjango.auth.permissions import PermissionChecker
        from hyperdjango.auth.user import User

        user = User(
            username=username,
            email=email or "",
            password_hash=hash_password(password),
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        await user.save()

        # Assign to RBAC groups (authoritative for access control)
        checker = PermissionChecker(db)
        staff_group = await checker.ensure_group("staff")
        su_group = await checker.ensure_group("superuser")
        await checker.add_user_to_group(user.id, staff_group.id)
        await checker.add_user_to_group(user.id, su_group.id)

        logger.success(
            "Superuser '{username}' created successfully.", username=username
        )
        await db.disconnect()

    asyncio.run(_create())


def _get_db_url(args) -> str:
    """Get database URL from args or environment."""
    # dynamic-attr: args is an argparse.Namespace shared across subcommands — "database" is only set by subparsers that define --database
    db_url = getattr(args, "database", None) or resolve_database_url()
    if not db_url:
        logger.error("No database URL. Pass --database or set DATABASE_URL.")
        sys.exit(1)
    return db_url


def cmd_makemigrations(args):
    """Generate migration from model changes vs live DB."""
    from hyperdjango.database import Database, set_db
    from hyperdjango.migrations import MigrationEngine

    db_url = _get_db_url(args)

    # Load app to register models
    if args.app:
        _load_app(args.app)

    async def _run():
        db = Database(db_url)
        await db.connect()
        set_db(db)

        engine = MigrationEngine(args.dir)
        result = await engine.makemigrations(db, name=args.name, dry_run=args.dry_run)

        if not result["operations"]:
            logger.info(
                "{message}", message=result.get("message", "No changes detected.")
            )
        else:
            # Show operations
            logger.info("Operations ({count}):", count=len(result["operations"]))
            for op in result["operations"]:
                logger.opt(raw=True).info(f"  - {op.description()}\n")

            # Show safety warnings
            for report in result.get("safety", []):
                logger.warning("  {operation}", operation=report["operation"])
                for w in report["warnings"]:
                    logger.opt(raw=True).info(f"    {w}\n")

            # Show SQL preview
            if args.dry_run:
                logger.opt(raw=True).info("\n-- SQL Preview (dry run) --\n")
                for sql in result["sql"]:
                    logger.opt(raw=True).info(f"  {sql}\n")
            else:
                logger.success("{message}", message=result["message"])

        await db.disconnect()

    asyncio.run(_run())


def cmd_migrate(args):
    """Apply pending migrations."""
    from hyperdjango.database import Database, set_db
    from hyperdjango.migrations import MigrationEngine, MigrationStateManager

    db_url = _get_db_url(args)

    if args.app:
        _load_app(args.app)

    async def _run():
        db = Database(db_url)
        await db.connect()
        set_db(db)

        engine = MigrationEngine(args.dir)

        if args.fake:
            # Mark a specific migration as applied

            await MigrationStateManager.ensure_table(db)
            await MigrationStateManager.record_applied(db, args.fake)
            logger.info("Marked {migration} as applied (fake).", migration=args.fake)
        else:
            applied = await engine.migrate(db, dry_run=args.dry_run)
            if applied:
                for name in applied:
                    prefix = "[DRY RUN] " if args.dry_run else ""
                    logger.opt(raw=True).info(f"  {prefix}Applied: {name}\n")
                logger.success("{count} migration(s) applied.", count=len(applied))
            else:
                logger.info("No pending migrations.")

        await db.disconnect()

    asyncio.run(_run())


def cmd_showmigrations(args):
    """List all migrations with applied status."""
    from hyperdjango.database import Database
    from hyperdjango.migrations import MigrationEngine

    db_url = _get_db_url(args)

    async def _run():
        db = Database(db_url)
        await db.connect()

        engine = MigrationEngine(args.dir)
        migrations = await engine.showmigrations(db)

        if not migrations:
            logger.info("No migrations found.")
        else:
            for m in migrations:
                mark = "[X]" if m["applied"] else "[ ]"
                logger.opt(raw=True).info(f"  {mark} {m['name']}\n")

        await db.disconnect()

    asyncio.run(_run())


def cmd_rollback(args):
    """Rollback most recent migration."""
    from hyperdjango.database import Database, set_db
    from hyperdjango.migrations import MigrationEngine

    db_url = _get_db_url(args)

    async def _run():
        db = Database(db_url)
        await db.connect()
        set_db(db)

        engine = MigrationEngine(args.dir)
        rolled_back = await engine.rollback(db, target=args.target)

        if rolled_back:
            for name in rolled_back:
                logger.opt(raw=True).info(f"  Rolled back: {name}\n")
            logger.success("{count} migration(s) rolled back.", count=len(rolled_back))
        else:
            logger.info("Nothing to rollback.")

        await db.disconnect()

    asyncio.run(_run())


def cmd_db(args):
    """Database management subcommands."""
    from hyperdjango.database import Database, set_db
    from hyperdjango.migrations import MigrationEngine

    if args.db_command == "verify":
        db_url = _get_db_url(args)
        if args.app:
            _load_app(args.app)

        async def _run():
            db = Database(db_url)
            await db.connect()
            set_db(db)

            engine = MigrationEngine()
            result = await engine.verify(db)

            if result["matches"]:
                logger.success("Schema matches models. No drift detected.")
            else:
                logger.warning(
                    "Schema drift detected ({count} issues):",
                    count=len(result["drift"]),
                )
                for issue in result["drift"]:
                    logger.opt(raw=True).info(f"  - {issue}\n")

            await db.disconnect()

        asyncio.run(_run())

    elif args.db_command == "snapshot":
        db_url = _get_db_url(args)

        async def _run():
            db = Database(db_url)
            await db.connect()

            engine = MigrationEngine(args.dir)
            filepath = await engine.snapshot(db)
            logger.success("Snapshot saved: {filepath}", filepath=filepath)

            await db.disconnect()

        asyncio.run(_run())

    elif args.db_command == "extensions":
        from hyperdjango import db_extensions

        # dynamic-attr: args is an argparse.Namespace — "ext_command" is only set when the extensions subparser ran
        if getattr(args, "ext_command", None) == "list":
            sys.exit(db_extensions.cli_list())
        # dynamic-attr: args is an argparse.Namespace — "ext_command" is only set when the extensions subparser ran
        elif getattr(args, "ext_command", None) == "ensure":
            db_url = _get_db_url(args)
            only = tuple(args.only) if args.only else None
            sys.exit(db_extensions.cli_ensure(db_url, only=only))
        else:
            logger.error("Usage: hyper db extensions {{list|ensure}}")
            sys.exit(1)

    elif args.db_command == "doctor":
        from hyperdjango import db_doctor

        sys.exit(db_doctor.main(args.database))

    else:
        logger.error("Usage: hyper db {{verify|snapshot|extensions|doctor}}")
        sys.exit(1)


def cmd_collectstatic(args):
    """Collect static files with content-hash filenames."""
    from hyperdjango.staticfiles import (
        ManifestStaticFilesStorage,
        set_manifest_storage,
    )

    # If app specified, load it to get static_dirs config
    static_dirs = args.static_dirs
    if args.app:
        app = _load_app(args.app)
        if hasattr(app, "static_dir") and app.static_dir:
            static_dirs = [app.static_dir]

    storage = ManifestStaticFilesStorage(
        static_dirs=static_dirs,
        static_root=args.static_root,
        max_post_process_passes=0 if args.no_post_process else 5,
    )

    logger.info("Collecting static files from: {dirs}", dirs=", ".join(static_dirs))
    logger.info("Destination: {dest}", dest=str(Path(args.static_root).resolve()))
    if args.dry_run:
        logger.info("(dry run — no files will be written)")
    elif args.clear:
        logger.info("(clearing destination first)")

    result = storage.collectstatic(clear=args.clear, dry_run=args.dry_run)

    logger.opt(raw=True).info(f"  Copied:         {result['copied']} files\n")
    logger.opt(raw=True).info(f"  Post-processed: {result['post_processed']} passes\n")

    if result["errors"]:
        logger.error("  Errors:         {count}", count=len(result["errors"]))
        for err in result["errors"]:
            logger.opt(raw=True).info(f"    - {err}\n")

    if not args.dry_run:
        manifest_path = str(Path(args.static_root).resolve() / "staticfiles.json")
        logger.opt(raw=True).info(f"\n  Manifest: {manifest_path}\n")
        manifest = storage.load_manifest()
        logger.opt(raw=True).info(f"  Entries:  {len(manifest)}\n")

    set_manifest_storage(storage)
    logger.success("Done.")


def cmd_shell(args):
    """Start an interactive Python shell with auto-imported HyperDjango modules."""
    from hyperdjango.database import Database, set_db

    db_url = args.database or resolve_database_url()

    # Build namespace with auto-imports
    namespace = {}

    # Core framework
    namespace["HyperApp"] = hyperdjango.HyperApp
    namespace["Request"] = hyperdjango.Request
    namespace["Response"] = hyperdjango.Response

    # Models and ORM
    try:
        from hyperdjango.models import Field, ManyToManyField, Model

        namespace["Model"] = Model
        namespace["Field"] = Field
        namespace["ManyToManyField"] = ManyToManyField
    except ImportError:
        pass

    # Database
    namespace["Database"] = Database
    namespace["set_db"] = set_db

    # Expressions
    try:
        from hyperdjango.expressions import (
            Avg,
            Case,
            Count,
            F,
            Max,
            Min,
            Sum,
            Value,
            When,
        )

        namespace.update(
            {
                "F": F,
                "Value": Value,
                "Count": Count,
                "Sum": Sum,
                "Avg": Avg,
                "Max": Max,
                "Min": Min,
                "Case": Case,
                "When": When,
            }
        )
    except ImportError:
        pass

    # Forms
    try:
        from hyperdjango.forms import Form, ModelForm

        namespace["Form"] = Form
        namespace["ModelForm"] = ModelForm
    except ImportError:
        pass

    # Auth
    try:
        from hyperdjango.auth.user import User

        namespace["User"] = User
    except ImportError:
        pass

    # Cache
    try:
        from hyperdjango.cache import DatabaseCache, LocMemCache

        namespace["LocMemCache"] = LocMemCache
        namespace["DatabaseCache"] = DatabaseCache
    except ImportError:
        pass

    # Signals
    try:
        from hyperdjango.signals import (
            Signal,
            post_delete,
            post_save,
            pre_delete,
            pre_save,
        )

        namespace.update(
            {
                "Signal": Signal,
                "pre_save": pre_save,
                "post_save": post_save,
                "pre_delete": pre_delete,
                "post_delete": post_delete,
            }
        )
    except ImportError:
        pass

    # Paginator
    try:
        from hyperdjango.paginator import Paginator

        namespace["Paginator"] = Paginator
    except ImportError:
        pass

    # Logger
    namespace["logger"] = logger

    # Load app if specified (registers models)
    if args.app:
        app = _load_app(args.app)
        namespace["app"] = app

    # Connect database if URL available
    db = None
    if db_url:
        try:
            db = Database(db_url)
            asyncio.run(db.connect())
            set_db(db)
            namespace["db"] = db
            logger.info(
                "Database connected: {db_display}",
                db_display=db_url.split("@")[-1] if "@" in db_url else db_url,
            )
        # blind-except: interactive shell must still open when the database is unreachable; the failure is logged and the shell continues without db
        except Exception as e:
            logger.warning("Database connection failed: {error}", error=e)
            logger.info("Shell opened without database connection.")

    # Execute command if -c flag
    if args.command:
        exec(args.command, namespace)  # noqa: S102
        if db:
            asyncio.run(db.disconnect())
        return

    banner = (
        f"HyperDjango {hyperdjango.__version__} interactive shell\n"
        f"Auto-imported: {', '.join(sorted(namespace.keys()))}\n"
        f"Use asyncio.run() for async operations."
    )

    namespace["asyncio"] = asyncio

    try:
        from IPython import start_ipython

        logger.opt(raw=True).info(f"{banner}\n")
        start_ipython(argv=[], user_ns=namespace, display_banner=False)
    except ImportError:
        code.interact(banner=banner, local=namespace)

    if db:
        asyncio.run(db.disconnect())


def cmd_dbshell(args):
    """Open psql connected to the database."""
    db_url = args.database or resolve_database_url()
    if not db_url:
        logger.error("No database URL. Pass --database or set DATABASE_URL.")
        sys.exit(1)

    # Check psql is available
    psql = shutil.which("psql")
    if not psql:
        logger.error("psql not found. Install PostgreSQL client tools.")
        sys.exit(1)

    # Parse URL into psql args
    parsed = urlparse(db_url)

    cmd = [psql]
    if parsed.hostname:
        cmd.extend(["-h", parsed.hostname])
    if parsed.port:
        cmd.extend(["-p", str(parsed.port)])
    if parsed.username:
        cmd.extend(["-U", parsed.username])
    if parsed.path and parsed.path != "/":
        cmd.append(parsed.path.lstrip("/"))

    # Inherit the current environment to hand down to the spawned `psql` process.
    # env-boundary: subprocess-env propagation (+PGPASSWORD below), not a config read.
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/") if parsed.path else ""
    logger.info(
        "Connecting to {host}:{port}/{dbname}", host=host, port=port, dbname=dbname
    )
    try:
        result = subprocess.run(cmd, env=env)
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        pass


def _topological_sort_tables(tables: dict) -> list[str]:
    """Sort table names by FK dependencies (referenced tables first).

    Uses Python's graphlib.TopologicalSorter for correct ordering.
    Ensures generated model file has referenced models defined before
    models that reference them via foreign keys.
    """
    # Build dependency graph: each table depends on its FK targets
    graph = {}
    for name, table in tables.items():
        deps = set()
        for con in table.constraints:
            if con.type == "f" and con.fk_table and con.fk_table in tables:
                deps.add(con.fk_table)
        graph[name] = deps

    try:
        ts = TopologicalSorter(graph)
        return list(ts.static_order())
    except CycleError:
        # Circular FK dependencies — fall back to alphabetical
        return sorted(tables.keys())


def cmd_inspectdb(args):
    """Generate HyperApp Model classes from existing database tables."""
    from hyperdjango.database import Database
    from hyperdjango.migrations import DatabaseIntrospector

    db_url = _get_db_url(args)

    async def _run():
        db = Database(db_url)
        await db.connect()

        # dynamic-attr: args is an argparse.Namespace — "include_views" is only set by subparsers that define the flag
        include_views = getattr(args, "include_views", False)
        snapshot = await DatabaseIntrospector.introspect(
            db,
            schema=args.schema,
            include_views=include_views,
        )

        # Filter tables if specified
        tables = snapshot.tables
        if args.table:
            tables = {name: t for name, t in tables.items() if name in args.table}

        if not tables:
            logger.info("No tables found.")
            await db.disconnect()
            return

        # Determine which types are used for dynamic imports
        used_types = set()
        for table in tables.values():
            for col in table.columns.values():
                py_type = _pg_to_python_type(col.type_name)
                used_types.add(py_type)

        # Topological sort by FK dependencies (referenced tables first)
        sorted_names = _topological_sort_tables(tables)

        # Redirect output to file if --output specified
        # dynamic-attr: args is an argparse.Namespace — "output" is only set by subparsers that define --output
        output_file = getattr(args, "output", None)
        original_stdout = sys.stdout
        if output_file:
            sys.stdout = Path(output_file).open("w", encoding="utf-8")  # noqa: SIM115 — intentionally bare; replaces sys.stdout, closed in finally block

        try:
            # Generate model code (writes to stdout, which may be redirected to --output file)
            sys.stdout.write('"""\n')
            sys.stdout.write("# Auto-generated by `hyper inspectdb`\n")
            sys.stdout.write(f"# Schema: {args.schema}\n")
            sys.stdout.write(f"# Tables: {len(tables)}\n")
            if include_views:
                sys.stdout.write("# Includes views and materialized views\n")
            sys.stdout.write('"""\n')
            sys.stdout.write("\n")
            sys.stdout.write("from hyperdjango.models import Model, Field\n")
            if "datetime" in used_types or "date" in used_types or "time" in used_types:
                dt_imports = []
                if "datetime" in used_types:
                    dt_imports.append("datetime")
                if "date" in used_types:
                    dt_imports.append("date")
                if "time" in used_types:
                    dt_imports.append("time")
                sys.stdout.write(f"from datetime import {', '.join(dt_imports)}\n")
            if "Decimal" in used_types:
                sys.stdout.write("from decimal import Decimal\n")
            sys.stdout.write("\n")

            for table_name in sorted_names:
                if table_name in tables:
                    _generate_model(table_name, tables[table_name])
        finally:
            if output_file:
                sys.stdout.close()
                sys.stdout = original_stdout
                logger.success("Written to {output_file}", output_file=output_file)

        await db.disconnect()

    asyncio.run(_run())


# Python reserved words — column names matching these get a trailing underscore
_PYTHON_RESERVED = frozenset(
    {
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "False",
        "None",
        "True",
        # Soft keywords (3.10+) and problematic builtins
        "match",
        "case",
        "type",
    }
)

# PostgreSQL type → Python type mapping for inspectdb
_PG_TYPE_MAP = {
    # Integer types
    "int2": "int",
    "int4": "int",
    "int8": "int",
    "smallint": "int",
    "integer": "int",
    "bigint": "int",
    "serial": "int",
    "bigserial": "int",
    # Float types
    "float4": "float",
    "float8": "float",
    "real": "float",
    # Precision types (Decimal for accuracy)
    "numeric": "Decimal",
    "decimal": "Decimal",
    "money": "Decimal",
    # Boolean
    "bool": "bool",
    "boolean": "bool",
    # String types
    "text": "str",
    "varchar": "str",
    "bpchar": "str",
    "char": "str",
    "name": "str",
    # Structured string types
    "uuid": "str",
    "json": "str",
    "jsonb": "str",
    "xml": "str",
    "inet": "str",
    "cidr": "str",
    "macaddr": "str",
    # Date/time types
    "timestamptz": "datetime",
    "timestamp": "datetime",
    "date": "date",
    "time": "time",
    "timetz": "time",
    "interval": "str",
    # Binary
    "bytea": "bytes",
    # Array types (PostgreSQL reports these with _ prefix)
    "_int2": "list",
    "_int4": "list",
    "_int8": "list",
    "_float4": "list",
    "_float8": "list",
    "_bool": "list",
    "_text": "list",
    "_varchar": "list",
    "_name": "list",
    "_uuid": "list",
    "_jsonb": "list",
    # Full-text search
    "tsvector": "str",
    "tsquery": "str",
    # Range types
    "int4range": "str",
    "int8range": "str",
    "numrange": "str",
    "tsrange": "str",
    "tstzrange": "str",
    "daterange": "str",
    # Bit types
    "bit": "str",
    "varbit": "str",
    # Geometry
    "point": "str",
    "line": "str",
    "lseg": "str",
    "box": "str",
    "path": "str",
    "polygon": "str",
    "circle": "str",
    # System
    "pg_lsn": "str",
    "oid": "int",
}


def _generate_model(table_name: str, table):
    """Generate a HyperApp Model class from a DbTable."""
    class_name = _table_to_class_name(table_name)

    pk_columns = table.get_pk_columns()
    fk_map = {}
    unique_columns = set()

    # Build FK and unique maps from constraints
    for con in table.constraints:
        if con.type == "f" and len(con.columns) == 1 and con.fk_table:
            fk_map[con.columns[0]] = con.fk_table
        if con.type == "u" and len(con.columns) == 1:
            unique_columns.add(con.columns[0])

    # Composite PK: all columns in the composite key get primary_key=True
    # The ORM uses pk_fields (derived from fields with primary_key=True)
    # to build multi-field WHERE clauses for save/update/delete.
    if len(pk_columns) > 1:
        sys.stdout.write(f"# Composite primary key: ({', '.join(pk_columns)})\n")

    sys.stdout.write(f"class {class_name}(Model):\n")
    sys.stdout.write("    class Meta:\n")
    sys.stdout.write(f'        table = "{table_name}"\n')
    sys.stdout.write("\n")

    for col_name, col in table.columns.items():
        field_args = []
        python_type = _pg_to_python_type(col.type_name)

        # Escape Python reserved words
        safe_name = col_name
        rename_comment = ""
        if col_name in _PYTHON_RESERVED:
            safe_name = f"{col_name}_"
            rename_comment = f"  # Column '{col_name}' renamed — Python keyword"

        # Primary key
        if col_name in pk_columns:
            field_args.append("primary_key=True")
            if col.is_serial:
                field_args.append("auto=True")

        # Foreign key
        if col_name in fk_map:
            fk_table = fk_map[col_name]
            field_args.append(f'foreign_key="{fk_table}"')

        # Preserve max_length from existing schema VARCHAR(N) columns
        if col.char_max_length and col.type_name in _VARCHAR_TYPE_NAMES:
            field_args.append(f"max_length={col.char_max_length}")

        # Unique
        if col_name in unique_columns and col_name not in pk_columns:
            field_args.append("unique=True")

        # Nullable
        if col.nullable:
            python_type = f"{python_type} | None"
            if not col.has_default:
                field_args.append("default=None")

        # Default value (non-serial, non-null)
        comment = rename_comment
        if col.has_default and not col.is_serial and not col.nullable:
            default_expr = col.default_expr or ""
            if default_expr == "true":
                field_args.append("default=True")
            elif default_expr == "false":
                field_args.append("default=False")
            elif default_expr.startswith("'") and default_expr.endswith("'"):
                inner = default_expr[1:-1].replace("''", "'")
                field_args.append(f'default="{inner}"')
            elif _is_numeric_literal(default_expr):
                field_args.append(f"default={default_expr}")
            else:
                comment = f"  # DB default: {default_expr}"
                if rename_comment:
                    comment = rename_comment + comment
        elif rename_comment:
            comment = rename_comment

        args_str = ", ".join(field_args)
        sys.stdout.write(
            f"    {safe_name}: {python_type} = Field({args_str}){comment}\n"
        )

    sys.stdout.write("\n")
    sys.stdout.write("\n")


def _is_numeric_literal(s: str) -> bool:
    """Check if a string is a valid finite numeric literal (int or float, possibly negative).

    Rejects NaN, inf, -inf which are valid for float() but not valid Python defaults.
    """
    try:
        val = float(s)
        return math.isfinite(val)
    except ValueError, TypeError:
        return False


def _table_to_class_name(table_name: str) -> str:
    """Convert table_name to PascalCase class name.

    Handles leading digits by prefixing with 'Table'.
    """
    parts = table_name.replace("-", "_").split("_")
    name = "".join(p.capitalize() for p in parts if p)
    # Ensure valid Python identifier (no leading digit)
    if name and name[0].isdigit():
        name = f"Table{name}"
    return name or "UnnamedTable"


def _pg_to_python_type(type_name: str) -> str:
    """Map PostgreSQL type name to Python type annotation."""
    return _PG_TYPE_MAP.get(type_name, "str")


_PYPROJECT_VERSION_RE = re.compile(
    r'^(?P<prefix>version\s*=\s*")(?P<version>[^"]*)(?P<suffix>")\s*$',
    re.MULTILINE,
)


def cmd_release(args) -> int:
    """Mint the next canonical release stamp (YYYYMMDDHHMMSSmmm, UTC).

    Reads the previous version from pyproject.toml so the forward-only guard
    has its floor; prints the stamp, its human rendering, and the current git
    commit (for APP_BUILD_COMMIT in the deploy environment). With --apply the
    pyproject version line is rewritten in place.
    """
    from hyperdjango.versioning import mint_release_stamp, release_stamp_display

    pyproject = Path(args.pyproject)
    try:
        content = pyproject.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read {path}: {err}", path=pyproject, err=exc)
        return 1
    m = _PYPROJECT_VERSION_RE.search(content)
    if m is None:
        logger.error('No `version = "..."` line found in {path}', path=pyproject)
        return 1
    previous = m.group("version")

    stamp = mint_release_stamp(last=previous)

    commit = ""
    git = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=pyproject.resolve().parent,
    )
    if git.returncode == 0:
        commit = git.stdout.strip()

    print(f"release stamp: {stamp}")
    print(f"released at:   {release_stamp_display(stamp)}")
    print(f"previous:      {previous}")
    if commit:
        print(f"commit:        {commit}  (export HYPER_APP_BUILD_COMMIT={commit})")

    if args.apply:
        updated = (
            content[: m.start()]
            + m.group("prefix")
            + stamp
            + m.group("suffix")
            + content[m.end() :]
        )
        pyproject.write_text(updated, encoding="utf-8")
        print(f"applied:       {pyproject} version {previous} -> {stamp}")
    else:
        print("(dry run — pass --apply to write pyproject.toml)")
    return 0


def cmd_setup(args):
    """Create tables from app models and optionally seed data.

    Introspects all Model subclasses loaded by the app, generates
    CREATE TABLE IF NOT EXISTS SQL for each, and executes it.

    Usage:
        cd services/hypernews
        hyper setup --app app:app
        hyper setup --app app:app --seed seed:run
        hyper setup --app app:app --drop  # DESTRUCTIVE: drops and recreates
    """
    from hyperdjango.database import Database, ensure_database_exists, set_db
    from hyperdjango.fixtures import _sort_by_dependencies
    from hyperdjango.query import _model_registry

    db_url = args.database or resolve_database_url()

    # Load the app module — this registers all Model subclasses
    app_obj = _load_app(args.app)

    if not db_url:
        db_url = app_obj.database_url if hasattr(app_obj, "database_url") else ""
    if not db_url:
        logger.error("No database URL. Set DATABASE_URL or use --database.")
        sys.exit(1)

    async def _run():
        db = Database(db_url)
        try:
            await db.connect()
        except RuntimeError as connect_exc:
            # setup is the DDL authority — a missing target database is DDL
            # it owns. Provision via the maintenance DB and retry; when the
            # database already existed the failure was something else
            # (auth, server down), so surface the ORIGINAL error.
            try:
                created = await ensure_database_exists(db_url)
            except Exception:
                raise connect_exc from None
            if not created:
                raise
            logger.info("Created missing database for {url}", url=db_url)
            db = Database(db_url)
            await db.connect()
        set_db(db)

        models = list(_model_registry.values())
        if not models:
            logger.warning("No models found. Make sure your app imports its models.")
            await db.disconnect()
            return

        logger.info(
            "Found {count} model(s): {names}",
            count=len(models),
            names=", ".join(m.__name__ for m in models),
        )

        # Topological sort — FK targets created before dependents (Kahn's algorithm)
        sorted_models = _sort_by_dependencies(models)

        # A failed DB op now raises the typed hierarchy (DatabaseError base), not
        # a bare RuntimeError; catch both so best-effort DDL stays best-effort.
        from hyperdjango.db.pgzig_connection import DatabaseError

        # Auto-detect required PostgreSQL extensions from models + Meta.indexes
        has_vector = any(
            field_obj.vector_dimensions is not None
            for m in sorted_models
            for fname in m._meta.fields
            for field_obj in [m.__dict__.get(fname)]
            if field_obj is not None and hasattr(field_obj, "vector_dimensions")
        ) or any(
            idx.using in ("hnsw", "ivfflat")
            or any("vector" in op for op in idx.opclasses)
            for m in sorted_models
            for idx in m._meta.indexes
        )
        if has_vector:
            with contextlib.suppress(DatabaseError, RuntimeError):
                await db.execute("CREATE EXTENSION IF NOT EXISTS vector")

        has_trgm = any(
            "trgm" in op
            for m in sorted_models
            for idx in m._meta.indexes
            for op in idx.opclasses
        )
        if has_trgm:
            with contextlib.suppress(DatabaseError, RuntimeError):
                await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

        from hyperdjango.models import create_table_for_model

        for model_cls in sorted_models:
            table = model_cls._meta.table
            if not table:
                continue

            if args.drop:
                logger.info("  Dropping {table}...", table=table)

            try:
                await create_table_for_model(model_cls, db=db, drop=args.drop)
                logger.success("  {table} created", table=table)
            except (DatabaseError, RuntimeError) as e:
                logger.error("  {table} failed ({error})", table=table, error=e)

        logger.success("{count} table(s) created.", count=len(models))

        # Ensure default RBAC groups exist if auth tables are present
        rbac_tables = {m._meta.table for m in sorted_models}
        if "hyper_groups" in rbac_tables:
            from hyperdjango.auth.permissions import PermissionChecker

            checker = PermissionChecker(db)
            await checker.ensure_group("staff")
            await checker.ensure_group("superuser")
            logger.success("  RBAC groups ensured: staff, superuser")

        # Run seed function if specified
        if args.seed:
            logger.info("Running seed...")
            if ":" in args.seed:
                mod_name, func_name = args.seed.split(":", 1)
            else:
                mod_name = args.seed
                func_name = "run"

            seed_mod = importlib.import_module(mod_name)
            # dynamic-attr: func_name is a runtime string parsed from the user's --seed argument; the module attribute is not statically knowable
            seed_func = getattr(seed_mod, func_name)
            if inspect.iscoroutinefunction(seed_func):
                await seed_func(db)
            else:
                seed_func(db)
            logger.success("Seed complete.")

        await db.disconnect()
        logger.success("Setup complete!")

    asyncio.run(_run())


def cmd_service(args) -> int:
    """Run `hyper service <verb>`.

    The orchestration (subprocess supervision, secret persistence, companion
    wiring) lives in :mod:`hyperdjango.services_runner`, and the systemd verbs
    in :mod:`hyperdjango.services_systemd`, both imported here lazily, matching
    this module's rule that DB-free commands never pay for subsystems they do
    not touch.

    Routing the systemd verbs from HERE rather than from the runner's own
    dispatch is what keeps the dependency acyclic: ``services_systemd`` builds
    on ``services_runner`` (it reuses its secret resolution and its setup
    driver), so the runner must not import back.
    """
    if args.service_command in ("install", "uninstall"):
        from hyperdjango.services_systemd import dispatch as dispatch_systemd

        return dispatch_systemd(args)

    from hyperdjango.services_runner import dispatch

    return dispatch(args)


def cmd_version():
    logger.info("hyperdjango {version}", version=hyperdjango.__version__)


def _discover_custom_commands() -> None:
    """Import modules that may register @command handlers.

    Sources, in order:
      1. HYPER_COMMANDS env — comma-separated module paths (explicit, documented).
      2. A top-level ``commands`` module/package in the project root (convention).

    Missing modules are ignored; a module that exists but fails to import surfaces
    a warning so real errors aren't silently swallowed.
    """
    from hyperdjango.commands import discover_commands

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    module_paths: list[str] = []
    env_mods = str(get_setting("COMMANDS") or "")
    for mod in env_mods.split(","):
        mod = mod.strip()
        if mod and mod not in module_paths:
            module_paths.append(mod)
    if "commands" not in module_paths:
        module_paths.append("commands")

    for path in module_paths:
        try:
            discover_commands([path])
        except ModuleNotFoundError:
            continue
        # blind-except: command auto-discovery — a broken but present module is logged as a warning but must not abort CLI startup (absent modules handled above)
        except Exception as e:  # a real error in a present module — surface it
            logger.warning(
                "Failed importing command module '{path}': {err}", path=path, err=e
            )


def _dispatch_custom_command(name: str, rest: list[str]) -> None:
    """Look up and run a custom @command, exiting with its status code."""
    from hyperdjango.commands import get_command, list_commands, run_command

    _discover_custom_commands()

    if get_command(name) is None:
        logger.error("Unknown command: {name}", name=name)
        available = sorted(c.name for c in list_commands())
        if available:
            logger.info(
                "Available custom commands: {names}", names=", ".join(available)
            )
        else:
            logger.info(
                "No custom commands found. Register with @hyperdjango.commands.command "
                "and set HYPER_COMMANDS or add a top-level 'commands' module."
            )
        logger.info("Run 'hyper --help' for built-in commands.")
        sys.exit(2)

    exit_code = asyncio.run(run_command(name, rest))
    sys.exit(exit_code)


def _load_app(import_path):
    """Load a HyperApp from an import path like 'app:app' or 'services.rest_api.app:app'."""
    # Add cwd to path
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    if ":" in import_path:
        module_name, var_name = import_path.split(":", 1)
    else:
        module_name = import_path
        var_name = "app"

    try:
        module = importlib.import_module(module_name)
        # dynamic-attr: var_name is a runtime string parsed from the import path (e.g. "module:app"); the module attribute is not statically knowable
        return getattr(module, var_name)
    except (ImportError, AttributeError) as e:
        logger.error(
            "Error loading app '{import_path}': {error}",
            import_path=import_path,
            error=e,
        )
        sys.exit(1)


def cmd_dumpdata(args):
    """Dump model data to JSON fixtures."""
    from hyperdjango.database import Database, set_db
    from hyperdjango.fixtures import dumpdata, dumpdata_natural
    from hyperdjango.query import _model_registry

    # Require an explicit target. Defaulting to postgres://localhost/hyperdjango
    # silently pointed dumps at the WRONG database.
    # dynamic-attr: args is an argparse.Namespace shared across subcommands — "database" is only set by subparsers that define --database
    db_url = getattr(args, "database", None) or resolve_database_url()
    if not db_url:
        logger.error("No database URL. Pass --database or set DATABASE_URL.")
        sys.exit(1)
    db = Database(db_url)

    async def _run():
        await db.connect()
        set_db(db)
        try:
            if args.models:
                model_classes = []
                for name in args.models:
                    model_cls = _model_registry.get(name)
                    if model_cls is None:
                        logger.error("Unknown model table '{name}'", name=name)
                        logger.info(
                            "Available: {available}",
                            available=", ".join(sorted(_model_registry.keys())),
                        )
                        sys.exit(1)
                    model_classes.append(model_cls)
            else:
                model_classes = list(_model_registry.values())

            if args.natural_key is not None:
                if len(model_classes) != 1:
                    logger.error("--natural-key requires exactly one model")
                    sys.exit(1)
                result = await dumpdata_natural(
                    model_classes[0], args.natural_key, indent=args.indent
                )
            else:
                result = await dumpdata(
                    model_classes, output_path=args.output, indent=args.indent
                )

            if args.output:
                logger.success("Dumped to {output}", output=args.output)
            else:
                sys.stdout.write(f"{result}\n")
        finally:
            await db.disconnect()

    asyncio.run(_run())


def cmd_loaddata(args):
    """Load JSON fixtures into database."""
    from hyperdjango.database import Database, set_db
    from hyperdjango.fixtures import loaddata

    fixture_path = args.fixture
    if not Path(fixture_path).is_file():
        logger.error("Fixture file not found: {path}", path=fixture_path)
        sys.exit(1)

    # Require an explicit target. Defaulting to postgres://localhost/hyperdjango
    # silently loaded fixtures into the WRONG database.
    # dynamic-attr: args is an argparse.Namespace shared across subcommands — "database" is only set by subparsers that define --database
    db_url = getattr(args, "database", None) or resolve_database_url()
    if not db_url:
        logger.error("No database URL. Pass --database or set DATABASE_URL.")
        sys.exit(1)
    db = Database(db_url)

    async def _run():
        await db.connect()
        set_db(db)
        try:
            result = await loaddata(fixture_path, db=db)
            logger.info("Created: {created}", created=result.created)
            logger.info("Updated: {updated}", updated=result.updated)
            if result.skipped:
                logger.info("Skipped: {skipped}", skipped=result.skipped)
            if result.errors:
                logger.error("Errors ({count}):", count=len(result.errors))
                for err in result.errors:
                    logger.opt(raw=True).info(f"  - {err}\n")
            if not result.errors:
                logger.success("Fixtures loaded successfully.")
            sys.exit(1 if result.errors else 0)
        finally:
            await db.disconnect()

    asyncio.run(_run())


if __name__ == "__main__":
    # Propagate the exit code: subcommands that `return` a status (release,
    # service) are also invoked via `python -m hyperdjango.cli`, and dropping
    # the return value there made a failure look like a success.
    sys.exit(main())
