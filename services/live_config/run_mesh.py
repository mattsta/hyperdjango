"""
Live-config mesh launcher — one command to stand up the whole demo.

Brings up three isolated services concurrently and wires their credentials the
way a real deployment pipeline would (out-of-band token + KEK distribution),
without forking any of them: HyperSecret and HyperManager are launched through
their own existing CLIs, and the storefront is pointed at them purely over their
HTTP + change-feed APIs.

    HyperSecret   :8960   the secret store (ciphertext only)
    HyperManager  :8970   the change-notification hub (default live tier)
    Storefront    :8980   the consumer (this example)

Run:
    uv run python -m services.live_config.run_mesh

Then, in another terminal, prove live propagation:
    uv run python -m services.live_config.demo

Everything is isolated: two throwaway databases and a gitignored
``.runtime/`` directory hold all state. Ctrl-C tears the mesh down cleanly.

Ports and the database prefix are overridable via env:
    LIVE_CONFIG_HS_PORT / _HM_PORT / _SF_PORT     (defaults 8960/8970/8980)
    LIVE_CONFIG_DB_PREFIX                          (default postgres://localhost/)
"""

import contextlib
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(__file__).resolve().parent / ".runtime"
SECRET_DEMO = RUNTIME / "secret_demo"
MANAGER_DEMO = RUNTIME / "manager_demo"
LOGS = RUNTIME / "logs"

HS_PORT = int(os.environ.get("LIVE_CONFIG_HS_PORT", "8960"))
HM_PORT = int(os.environ.get("LIVE_CONFIG_HM_PORT", "8970"))
SF_PORT = int(os.environ.get("LIVE_CONFIG_SF_PORT", "8980"))
DB_PREFIX = os.environ.get("LIVE_CONFIG_DB_PREFIX", "postgres://localhost/")
HS_DB = "live_config_hypersecret"
HM_DB = "live_config_hypermanager"

HS_URL = f"http://127.0.0.1:{HS_PORT}"
HM_URL = f"http://127.0.0.1:{HM_PORT}"
SF_URL = f"http://127.0.0.1:{SF_PORT}"


def _db_env(db_name: str) -> dict:
    """Point a subprocess at one database with ONE variable. The framework
    resolves ``DATABASE_URL`` to the same connection for both the CLI (setup /
    seed) and the running server, so they agree from a single source — no need
    to also set ``HYPER_DATABASE_URL`` and ``PGDATABASE`` to the same value."""
    return {"DATABASE_URL": DB_PREFIX + db_name}


# Signing secrets, stable across every setup + server process so seed-minted
# identity tokens verify. Demo-only fixed values (>=32 chars for the app's
# require_setting length gate); a real deployment injects real secrets.
SIGNING_ENV = {
    "HYPER_SESSION_SIGNING_KEY": "live-config-demo-session-signing-key-0123456789",
    "HYPER_SECRET_KEY": "live-config-demo-secret-key-0123456789abcdef",
    "HYPER_ADMIN_SECRET": "live-config-demo-admin-secret-0123456789abcdef",
    "HYPER_DEBUG": "1",
}

# The storefront's keys, provisioned into prod/api AFTER seeding (through the
# operator identity + the namespace KEK), so the scenario has a publishable
# Stripe key and a couple of public service keys alongside one genuine secret —
# without modifying HyperSecret's seed. (name, value, is_secret)
DEMO_KEYS = [
    ("stripe_pk_live", "pk_live_demo_51StorefrontPUBLISHABLEkey00", False),
    ("maps_api_key", "AIzaDemo_MapsBrowserKey_public_0000", False),
    ("analytics_key", "UA-DEMO-ANALYTICS-000-public", False),
    ("webhook_secret", "whsec_demo_signing_secret_never_displayed", True),
]

KEK_FILE = SECRET_DEMO / "prod-api.kek"


# ---------------------------------------------------------------------------
# Small HTTP helper (keeps this launcher independent of the test harness)
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = 3.0) -> int:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        conn.request("GET", parsed.path or "/")
        return conn.getresponse().status
    finally:
        conn.close()


def _wait_ready(url: str, label: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _http_get(url) == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"{label} did not become ready at {url} within {timeout}s")


# ---------------------------------------------------------------------------
# Managed service subprocess
# ---------------------------------------------------------------------------


class Service:
    """A HyperApp server subprocess (``app.run`` in its own process group).

    Portable: no shell, no daemonization — the parent owns the child directly,
    so Ctrl-C tears the whole mesh down deterministically."""

    def __init__(self, label: str, module_app: str, port: int, env: dict):
        self.label = label
        self.module_app = module_app
        self.port = port
        self.env = env
        self._proc: subprocess.Popen | None = None
        self._log_stack = contextlib.ExitStack()

    def start(self) -> None:
        module, attr = self.module_app.split(":")
        script = (
            f"import {module} as _m; _m.{attr}.run(host='127.0.0.1', port={self.port})"
        )
        proc_env = os.environ.copy()
        proc_env.update(self.env)
        LOGS.mkdir(parents=True, exist_ok=True)
        log = self._log_stack.enter_context((LOGS / f"{self.label}.log").open("w"))
        self._log = log
        self._proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            env=proc_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError, OSError:
            pass
        finally:
            self._proc = None
            self._log_stack.close()


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------


def _run(cmd: list[str], env: dict, label: str) -> None:
    result = subprocess.run(
        cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"{label} failed: {' '.join(cmd[:6])}")


def _createdb(name: str) -> None:
    # createdb is idempotent for the demo: an "already exists" is fine — the
    # per-app `hyper setup --drop` recreates the tables either way.
    subprocess.run(["createdb", name], capture_output=True, text=True, timeout=30)


def _setup_and_seed() -> tuple[dict, dict]:
    """Create the databases, seed both services, and return their token maps."""
    for d in (SECRET_DEMO, MANAGER_DEMO, LOGS):
        d.mkdir(parents=True, exist_ok=True)

    print("• Creating isolated databases…")
    _createdb(HS_DB)
    _createdb(HM_DB)

    base = os.environ.copy()
    base.update(SIGNING_ENV)

    print("• Seeding HyperManager…")
    hm_env = {**base, **_db_env(HM_DB)}
    hm_env["HYPERMANAGER_DEMO_DIR"] = str(MANAGER_DEMO)
    _run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypermanager.app:app",
            "--drop",
            "--seed",
            "services.hypermanager.seed:run",
        ],
        hm_env,
        "hypermanager setup",
    )

    print("• Seeding HyperSecret…")
    hs_env = {**base, **_db_env(HS_DB)}
    hs_env["HYPERSECRET_DEMO_DIR"] = str(SECRET_DEMO)
    _run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypersecret.app:app",
            "--drop",
            "--seed",
            "services.hypersecret.seed:run",
        ],
        hs_env,
        "hypersecret setup",
    )

    secret_tokens = json.loads((SECRET_DEMO / "tokens.json").read_text())
    manager_tokens = json.loads((MANAGER_DEMO / "tokens.json").read_text())
    return secret_tokens, manager_tokens


def _provision_keys(operator_token: str) -> None:
    """Provision the storefront's keys into prod/api over the admin API, using
    the operator identity and the namespace KEK. This is the demo stand-in for a
    platform team publishing service config — it never modifies HyperSecret."""
    print("• Provisioning demo keys into prod/api…")
    env = os.environ.copy()
    env.update(SIGNING_ENV)
    env["HYPERSECRET_URL"] = HS_URL
    env["HYPERSECRET_TOKEN"] = operator_token
    for name, value, _is_secret in DEMO_KEYS:
        _run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "services.hypersecret.provision",
                "put",
                "prod/api",
                name,
                "--kek-file",
                str(KEK_FILE),
                "--value",
                value,
            ],
            env,
            f"provision {name}",
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _connection_map() -> str:
    return f"""
┌─────────────────────────────────────────────────────────────────────────┐
│  LIVE-CONFIG MESH                                                          │
├───────────────┬────────┬──────────────────────────────────────────────────
│  Service      │  Port  │  Role
├───────────────┼────────┼──────────────────────────────────────────────────
│  HyperSecret  │  {HS_PORT:<5} │  secret store (ciphertext only; producer of nudges)
│  HyperManager │  {HM_PORT:<5} │  change-notification hub (default live tier)
│  Storefront   │  {SF_PORT:<5} │  consumer: reads prod/api, watches the feed
└───────────────┴────────┴──────────────────────────────────────────────────

Credentials (isolated, gitignored under services/live_config/.runtime/):
  • Storefront reads with  service:prod-api    (READ on prod/api)
  • Storefront subscribes   service:platform-api (SUBSCRIBE on secrets/prod/)
  • HyperSecret publishes   producer:hypersecret (PUBLISH on secrets/)
"""


def _next_steps() -> str:
    return f"""
NEXT STEPS
  1. See the live config:
       curl -s {SF_URL}/config | python -m json.tool
     Or open the status page:  {SF_URL}/

  2. Rotate the publishable Stripe key (new value → new version):
       export HYPERSECRET_URL={HS_URL}
       export HYPERSECRET_TOKEN=$(python -c \
"import json;print(json.load(open('{SECRET_DEMO / "tokens.json"}'))['operator:admin'])")
       uv run python -m services.hypersecret.provision \\
           put prod/api stripe_pk_live --kek-file {KEK_FILE} --value pk_live_ROTATED_v2

  3. Re-curl {SF_URL}/config — stripe_pk_live shows the NEW version and value,
     with no storefront restart (it converged over the feed).

  Or run the scripted proof in another terminal:
       uv run python -m services.live_config.demo

Press Ctrl-C to tear the mesh down.
"""


def main() -> int:
    secret_tokens, manager_tokens = _setup_and_seed()

    base = os.environ.copy()
    base.update(SIGNING_ENV)

    hm = Service(
        "hypermanager",
        "services.hypermanager.app:app",
        HM_PORT,
        {**base, **_db_env(HM_DB), "HYPERMANAGER_DEMO_DIR": str(MANAGER_DEMO)},
    )
    hs = Service(
        "hypersecret",
        "services.hypersecret.app:app",
        HS_PORT,
        {
            **base,
            **_db_env(HS_DB),
            "HYPERSECRET_DEMO_DIR": str(SECRET_DEMO),
            # Producer wiring: HyperSecret pushes metadata-only nudges to the hub
            # through its transactional outbox, authenticated by a PUBLISH-granted
            # hub identity.
            "HYPERSECRET_MANAGER_URL": HM_URL,
            "HYPERSECRET_MANAGER_TOKEN": manager_tokens["producer:hypersecret"],
        },
    )
    sf = Service(
        "storefront",
        "services.live_config.app:app",
        SF_PORT,
        {
            **base,
            "LIVE_CONFIG_HYPERSECRET_URL": HS_URL,
            "LIVE_CONFIG_HYPERSECRET_TOKEN": secret_tokens["service:prod-api"],
            "LIVE_CONFIG_NAMESPACE": "prod/api",
            "LIVE_CONFIG_KEK_FILE": str(KEK_FILE),
            "LIVE_CONFIG_MANAGER_URL": HM_URL,
            "LIVE_CONFIG_MANAGER_TOKEN": manager_tokens["service:platform-api"],
            "LIVE_CONFIG_PORT": str(SF_PORT),
        },
    )

    services = [hm, hs, sf]
    stop = threading.Event()

    def _on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        print("• Starting HyperManager…")
        hm.start()
        _wait_ready(f"{HM_URL}/ready", "HyperManager")

        print("• Starting HyperSecret…")
        hs.start()
        _wait_ready(f"{HS_URL}/ready", "HyperSecret")

        _provision_keys(secret_tokens["operator:admin"])

        print("• Starting Storefront…")
        sf.start()
        _wait_ready(f"{SF_URL}/ready", "Storefront")

        print(_connection_map())
        print(_next_steps())

        while not stop.wait(0.5):
            pass
    finally:
        print("\n• Tearing down…")
        for svc in reversed(services):
            svc.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
