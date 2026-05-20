"""Tests for the bundled-service registry and the ``hyper service`` CLI verb.

Pins the invariants that make ``hyper service run <name>`` trustworthy:

  1. every registered service points at a real directory and a module that
     really defines the declared ``HyperApp`` attribute (verified by parsing,
     not importing — this test must never boot an app or touch a database)
  2. seed paths name a real module and a real function
  3. ports are unique inside the registry, inside the documented block, and
     disjoint from the e2e suite's reserved ``TEST_PORTS``
  4. companion and companion-token/url references resolve to real entries, and
     the launch order puts dependencies first
  5. generated secrets are stable across calls, long enough for the
     ``require_setting`` gates, written 0600, and gitignored
  6. ``hyper service list`` / ``info`` exit 0 and name a known service; an
     unknown name exits non-zero and lists the valid names

No server is started anywhere in this file.
"""

# hyper-test: unit

import ast
import os
import socket
import subprocess
import sys
from pathlib import Path

from hyperdjango.services_registry import (
    SERVICE_PORT_BLOCK,
    SERVICES,
    SERVICES_ROOT,
    Service,
    UnknownServiceError,
    audit_registry,
    get_service,
    launch_order,
    service_names,
)
from hyperdjango.services_runner import (
    DEMO_CREDENTIAL_VARS,
    ServiceError,
    check_port_free,
    ensure_native_extension,
    read_env_file,
    read_run_state,
    resolve_secrets,
    service_database_url,
    write_env_file,
    write_run_state,
)
from hyperdjango.testkit import check, finish, run_main

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from e2e_helper import TEST_PORTS, service_app, service_seed  # noqa: E402


def _module_file(dotted: str) -> Path:
    """Map ``services.hypernews.app`` to its file without importing it."""
    return REPO_ROOT / Path(*dotted.split(".")).with_suffix(".py")


def _module_defines(path: Path, name: str) -> bool:
    """True when ``path`` binds ``name`` at module level.

    Parses instead of importing: importing a service builds a HyperApp,
    registers models and may call ``require_setting``, none of which belongs in
    a unit test.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
    return False


# ── 1-2. Structure: directories, app paths, seed paths ───────────────────────


def test_registry_audit() -> None:
    audit = audit_registry()
    check("registry: self-audit clean", audit.ok, str(audit))
    check("registry: no duplicate ports", not audit.duplicate_ports)
    check("registry: no out-of-block ports", not audit.out_of_block_ports)
    check("registry: every directory exists", not audit.missing_directories)
    check("registry: every companion resolves", not audit.unknown_companions)


def test_registry_nonempty() -> None:
    check("registry: has entries", len(SERVICES) > 0, f"{len(SERVICES)} entries")
    check("registry: SERVICES_ROOT exists", SERVICES_ROOT.is_dir(), str(SERVICES_ROOT))
    check(
        "registry: every entry is a frozen slotted Service",
        all(isinstance(s, Service) for s in SERVICES.values()),
    )
    check(
        "registry: names match dict keys",
        all(name == service.name for name, service in SERVICES.items()),
    )


def test_app_paths_resolve() -> None:
    for name in service_names():
        service = get_service(name)
        check(f"{name}: directory exists", service.directory.is_dir())
        path = _module_file(service.module)
        if not check(f"{name}: app module file exists", path.is_file(), str(path)):
            continue
        check(
            f"{name}: {service.module} defines {service.attribute!r}",
            _module_defines(path, service.attribute),
        )


def test_seed_paths_resolve() -> None:
    for name in service_names():
        service = get_service(name)
        if service.seed_path is None:
            continue
        module, _, function = service.seed_path.partition(":")
        path = _module_file(module)
        if not check(f"{name}: seed module exists", path.is_file(), str(path)):
            continue
        check(
            f"{name}: {module} defines {function!r}",
            _module_defines(path, function),
        )


def test_launchers_resolve() -> None:
    for name in service_names():
        service = get_service(name)
        if service.launcher is None:
            continue
        path = _module_file(service.launcher)
        check(f"{name}: launcher {service.launcher} exists", path.is_file(), str(path))


# ── 3. Ports ─────────────────────────────────────────────────────────────────


def test_ports_unique_and_in_block() -> None:
    ports = [get_service(n).port for n in service_names()]
    check("ports: unique inside the registry", len(ports) == len(set(ports)))
    outside = [p for p in ports if p not in SERVICE_PORT_BLOCK]
    check(
        "ports: all inside the documented block",
        not outside,
        f"outside {SERVICE_PORT_BLOCK}: {outside}",
    )


def test_ports_disjoint_from_test_ports() -> None:
    reserved = set(TEST_PORTS.values())
    registry_ports = set()
    for name in service_names():
        service = get_service(name)
        registry_ports.add(service.port)
        for entry in service.extra_env:
            if entry.name.endswith("_PORT") and entry.value.isdigit():
                registry_ports.add(int(entry.value))
    overlap = sorted(registry_ports & reserved)
    check(
        "ports: no overlap with e2e TEST_PORTS",
        not overlap,
        f"collisions: {overlap}",
    )
    check(
        "ports: e2e block (18100-19260) is disjoint from the service block",
        min(reserved) > SERVICE_PORT_BLOCK.stop,
        f"min TEST_PORT={min(reserved)} block_end={SERVICE_PORT_BLOCK.stop}",
    )


def test_ports_clear_of_ephemeral_range() -> None:
    # `hyper doctor`'s ephemeral_port_overlap check guards the server port
    # against the Linux default 32768-60999; the whole block must clear it.
    check(
        "ports: block is below the Linux ephemeral range",
        SERVICE_PORT_BLOCK.stop <= 32768,
        f"block ends at {SERVICE_PORT_BLOCK.stop}",
    )


# ── 4. Companions ────────────────────────────────────────────────────────────


def test_companions_resolve() -> None:
    for name in service_names():
        service = get_service(name)
        for companion in service.companions:
            check(
                f"{name}: companion {companion!r} is registered",
                companion in SERVICES,
            )
        for binding in service.companion_tokens:
            check(
                f"{name}: token binding -> {binding.companion!r} is registered",
                binding.companion in SERVICES,
            )
            check(
                f"{name}: token binding names a companion of {name}",
                binding.companion in service.companions,
            )
        for url_binding in service.companion_urls:
            check(
                f"{name}: url binding -> {url_binding.companion!r} is registered",
                url_binding.companion in SERVICES,
            )
            check(
                f"{name}: url binding names a companion of {name}",
                url_binding.companion in service.companions,
            )


def test_launch_order_puts_dependencies_first() -> None:
    order = [s.name for s in launch_order("hypersecret")]
    check(
        "launch_order(hypersecret): companion precedes the service",
        order.index("hypermanager") < order.index("hypersecret"),
        str(order),
    )
    mesh = [s.name for s in launch_order("live_config")]
    check(
        "launch_order(live_config): all three services, target last",
        mesh[-1] == "live_config" and len(mesh) == 3,
        str(mesh),
    )
    for name in service_names():
        order = [s.name for s in launch_order(name)]
        check(f"launch_order({name}): no duplicates", len(order) == len(set(order)))
        check(f"launch_order({name}): ends with the target", order[-1] == name)


def test_companion_token_paths_point_at_demo_dirs() -> None:
    # The seeds write tokens.json into their DEMO_DIR, which is a SUBDIRECTORY
    # of the runtime dir — a bare runtime_dir/tokens.json would look in the
    # wrong place and the companion handoff would silently fail.
    for name in service_names():
        service = get_service(name)
        if not service.companion_tokens:
            continue
        for binding in service.companion_tokens:
            companion = get_service(binding.companion)
            demo_dirs = [
                entry.value
                for entry in companion.resolved_env()
                if entry.name.endswith("_DEMO_DIR")
            ]
            if not demo_dirs:
                continue
            check(
                f"{name}: {companion.name} tokens path is inside its demo dir",
                str(companion.tokens_path).startswith(demo_dirs[0]),
                f"{companion.tokens_path} vs {demo_dirs[0]}",
            )


# ── 5. Secrets ───────────────────────────────────────────────────────────────


def test_secret_generation_is_stable(tmp_root: Path) -> None:
    service = get_service("hypersecret")
    env_file = service.env_file
    backup = env_file.read_text(encoding="utf-8") if env_file.is_file() else None
    # Ambient values would short-circuit generation and make this test a no-op.
    saved = {
        var: os.environ.pop(var, None)
        for var in (
            *(s.env_var for s in service.secrets),
            *DEMO_CREDENTIAL_VARS,
        )
    }
    try:
        env_file.unlink(missing_ok=True)
        first = resolve_secrets(service)
        second = resolve_secrets(service)
        check(
            "secrets: file created on first resolve",
            env_file.is_file(),
            str(env_file),
        )
        check(
            "secrets: something was generated on the first call",
            bool(first.generated),
            str(first.generated),
        )
        check(
            "secrets: second call generates nothing new",
            not second.generated,
            str(second.generated),
        )
        check(
            "secrets: values identical across calls",
            first.values == second.values,
        )
        for requirement in service.generated_secrets:
            value = first.values.get(requirement.env_var, "")
            check(
                f"secrets: {requirement.env_var} meets min_length "
                f"{requirement.min_length}",
                len(value) >= requirement.min_length,
                f"len={len(value)}",
            )
        for var in DEMO_CREDENTIAL_VARS:
            check(f"secrets: {var} generated for a seeded service", var in first.values)
        mode = env_file.stat().st_mode & 0o777
        check("secrets: file mode is 0600", mode == 0o600, oct(mode))
        check("secrets: no missing supplied secrets", not first.missing)
    finally:
        env_file.unlink(missing_ok=True)
        if backup is not None:
            write_env_file(env_file, read_env_file_from_text(backup))
        for var, value in saved.items():
            if value is not None:
                os.environ[var] = value


def read_env_file_from_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_env_file_roundtrip(tmp_root: Path) -> None:
    path = tmp_root / "env_roundtrip" / ".env.local"
    payload = {"HYPER_SECRET_KEY": "x" * 40, "HYPER_ADMIN_SECRET": "y" * 40}
    write_env_file(path, payload)
    check("env file: roundtrips", read_env_file(path) == payload)
    check(
        "env file: absent file reads as empty", read_env_file(path.parent / "no") == {}
    )


def test_secret_files_are_gitignored() -> None:
    targets = []
    for name in service_names():
        service = get_service(name)
        targets.append(str(service.env_file.relative_to(REPO_ROOT)))
        targets.append(
            str((service.runtime_dir / "tokens.json").relative_to(REPO_ROOT))
        )
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="\n".join(targets),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    ignored = set(result.stdout.split())
    missing = [t for t in targets if t not in ignored]
    check(
        "secrets: every .env.local and .runtime/tokens.json is gitignored",
        not missing,
        f"not ignored: {missing[:5]}",
    )


def test_supplied_secrets_are_not_invented() -> None:
    service = get_service("semantic_search")
    check(
        "semantic_search: EMBEDDINGS_API_KEY is marked non-generated",
        [s.env_var for s in service.supplied_secrets] == ["EMBEDDINGS_API_KEY"],
        str(service.secrets),
    )


# ── 6. Database URL derivation ───────────────────────────────────────────────


def test_database_urls_are_per_service() -> None:
    urls = {n: service_database_url(get_service(n)) for n in service_names()}
    check("db urls: one per service", len(set(urls.values())) == len(urls))
    for name, url in urls.items():
        check(f"db url: {name} ends with its own database", url.endswith(name))


# ── 6b. Port preflight ───────────────────────────────────────────────────────


def test_port_preflight() -> None:
    """An occupied port must fail loudly, never be silently adopted.

    Regression guard: without this check the readiness poll can be satisfied by
    a STALE server already listening on the port, so the launch reports success
    while the process actually started dies with EADDRINUSE.
    """
    service = get_service("hello")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy_port = listener.getsockname()[1]
    try:
        raised = ""
        try:
            check_port_free(service, busy_port)
        except ServiceError as exc:
            raised = str(exc)
        check("port preflight: occupied port raises", bool(raised))
        check(
            "port preflight: message names the remediation",
            "hyper service stop" in raised and "--port" in raised,
            raised[:200],
        )
    finally:
        listener.close()

    # A freed port must pass. Bind/close to get one nothing else is using.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    ok = True
    try:
        check_port_free(service, free_port)
    except ServiceError:
        ok = False
    check("port preflight: free port passes", ok)


def test_run_state_roundtrip() -> None:
    """`stop` must find a service started on an overridden port.

    Regression guard: keying the PID file on the REGISTRY port alone meant
    `hyper service run <name> --port N` produced a service that
    `hyper service stop <name>` reported as "nothing running", leaving an
    orphaned server holding the port.
    """
    service = get_service("notes_api")
    backup = read_run_state(service)
    try:
        write_run_state(service, {"notes_api": 8680})
        check(
            "run state: roundtrips the actual port",
            read_run_state(service) == {"notes_api": 8680},
            str(read_run_state(service)),
        )
        check(
            "run state: overridden port differs from the registry default",
            service.port != 8680,
        )
        (service.runtime_dir / "running.json").unlink(missing_ok=True)
        check("run state: absent file reads as empty", read_run_state(service) == {})
    finally:
        (service.runtime_dir / "running.json").unlink(missing_ok=True)
        if backup:
            write_run_state(service, backup)


def test_native_extension_gate() -> None:
    ok = True
    try:
        ensure_native_extension()
    except ServiceError:
        ok = False
    check("native gate: passes when the extension is built", ok)


# ── 7. e2e_helper consumes the registry ──────────────────────────────────────


def test_e2e_helper_sources_paths_from_registry() -> None:
    check(
        "e2e_helper: service_app matches the registry",
        service_app("bookstore_api") == get_service("bookstore_api").app_path,
    )
    check(
        "e2e_helper: service_seed matches the registry",
        service_seed("bookstore_api") == get_service("bookstore_api").seed_path,
    )
    check(
        "e2e_helper: unknown name raises UnknownServiceError",
        _raises_unknown(lambda: service_app("nope")),
    )


def _raises_unknown(fn) -> bool:
    try:
        fn()
    except UnknownServiceError:
        return True
    return False


# ── 8. CLI surface ───────────────────────────────────────────────────────────


def _hyper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hyperdjango.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_list() -> None:
    result = _hyper("service", "list")
    check("cli: `service list` exits 0", result.returncode == 0, result.stderr[-400:])
    check("cli: `service list` names hypernews", "hypernews" in result.stdout)
    check("cli: `service list` shows the port block", "8600-8699" in result.stdout)
    for name in service_names():
        check(f"cli: `service list` includes {name}", name in result.stdout)


def test_cli_info() -> None:
    result = _hyper("service", "info", "hypersecret")
    check("cli: `service info` exits 0", result.returncode == 0, result.stderr[-400:])
    out = result.stdout
    check("cli: info names the app path", "services.hypersecret.app:app" in out)
    check("cli: info names the seed path", "services.hypersecret.seed:run" in out)
    check("cli: info names the companion", "hypermanager" in out)
    check("cli: info lists the required secrets", "HYPER_SECRET_KEY" in out)
    check("cli: info shows manual commands", "uv run hyper setup" in out)
    check("cli: info shows the port", "8613" in out)


def test_cli_info_no_db_service() -> None:
    result = _hyper("service", "info", "hello")
    check("cli: info(hello) exits 0", result.returncode == 0)
    check("cli: info(hello) reports no seed", "(none)" in result.stdout)


def test_cli_unknown_name_fails_loudly() -> None:
    result = _hyper("service", "info", "definitely_not_a_service")
    combined = result.stdout + result.stderr
    check(
        "cli: unknown name exits non-zero",
        result.returncode != 0,
        str(result.returncode),
    )
    check("cli: unknown name says 'unknown service'", "unknown service" in combined)
    check("cli: unknown name lists valid names", "hypernews" in combined)
    check("cli: unknown name lists every valid name", "websocket_chat" in combined)


def test_cli_run_rejects_unknown_name() -> None:
    result = _hyper("service", "run", "definitely_not_a_service")
    combined = result.stdout + result.stderr
    check("cli: `service run` unknown exits non-zero", result.returncode != 0)
    check("cli: `service run` unknown lists valid names", "bookstore_api" in combined)


def test_cli_stop_unknown_name() -> None:
    result = _hyper("service", "stop", "definitely_not_a_service")
    check("cli: `service stop` unknown exits non-zero", result.returncode != 0)


def main() -> bool:
    tmp_root = REPO_ROOT / ".test_scratch" / "services_registry"
    tmp_root.mkdir(parents=True, exist_ok=True)

    print("\n== registry structure ==")
    test_registry_audit()
    test_registry_nonempty()
    test_app_paths_resolve()
    test_seed_paths_resolve()
    test_launchers_resolve()

    print("\n== ports ==")
    test_ports_unique_and_in_block()
    test_ports_disjoint_from_test_ports()
    test_ports_clear_of_ephemeral_range()

    print("\n== companions ==")
    test_companions_resolve()
    test_launch_order_puts_dependencies_first()
    test_companion_token_paths_point_at_demo_dirs()

    print("\n== secrets ==")
    test_secret_generation_is_stable(tmp_root)
    test_env_file_roundtrip(tmp_root)
    test_secret_files_are_gitignored()
    test_supplied_secrets_are_not_invented()

    print("\n== database urls ==")
    test_database_urls_are_per_service()

    print("\n== port preflight + native gate ==")
    test_port_preflight()
    test_run_state_roundtrip()
    test_native_extension_gate()

    print("\n== e2e_helper integration ==")
    test_e2e_helper_sources_paths_from_registry()

    print("\n== cli ==")
    test_cli_list()
    test_cli_info()
    test_cli_info_no_db_service()
    test_cli_unknown_name_fails_loudly()
    test_cli_run_rejects_unknown_name()
    test_cli_stop_unknown_name()

    return finish()


if __name__ == "__main__":
    run_main(main)
