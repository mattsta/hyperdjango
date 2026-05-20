"""
E2E: live-config mesh — HyperSecret × HyperManager × Storefront.

# hyper-test: e2e

Boots all three services and proves the central story end to end: a consumer
loads its config from HyperSecret, subscribes to HyperManager's change feed, and
converges on a rotated key LIVE — no restart — while the secret-classified key is
never exposed in plaintext on any endpoint.

  - the storefront's /config loads the provisioned keys at their initial versions
  - rotating a key in HyperSecret propagates through the feed: /config reflects
    the new version within a bounded wait, with no storefront restart
  - the secret-classified key is masked everywhere (never plaintext)

Wires the three services exactly as run_mesh.py does (out-of-band token + KEK
distribution over their HTTP/feed APIs; no fork of either upstream app).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e_helper import TEST_PORTS, AppRunner, http_get  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


SECRET_PORT = TEST_PORTS["live_config_secret"]
MANAGER_PORT = TEST_PORTS["live_config_manager"]
SF_PORT = TEST_PORTS["live_config"]
SECRET_BASE = f"http://127.0.0.1:{SECRET_PORT}"
MANAGER_BASE = f"http://127.0.0.1:{MANAGER_PORT}"
SF_BASE = f"http://127.0.0.1:{SF_PORT}"

SCRATCH = PROJECT_ROOT / ".test_scratch" / "live_config"
SECRET_DEMO = SCRATCH / "secret_demo"
MANAGER_DEMO = SCRATCH / "manager_demo"
KEK_FILE = SECRET_DEMO / "prod-api.kek"

# Stable signing secrets across seed + server subprocesses (>=32 chars for the
# apps' require_setting gate). hyper-test injects some; pin the rest.
os.environ.setdefault(
    "HYPER_SESSION_SIGNING_KEY", "test-session-signing-key-for-tests-only"
)
os.environ.setdefault(
    "HYPER_SECRET_KEY", "test-secret-key-for-live-config-tests-only-0123456789"
)
if len(os.environ.get("HYPER_ADMIN_SECRET", "")) < 32:
    os.environ["HYPER_ADMIN_SECRET"] = (
        "test-admin-secret-for-live-config-tests-only-0123456789"
    )

# The storefront's keys, provisioned into prod/api after seeding. The secret one
# must never surface in plaintext. (name, value, is_secret)
DEMO_KEYS = [
    ("stripe_pk_live", "pk_live_demo_51StorefrontPUBLISHABLEkey00", False),
    ("maps_api_key", "AIzaDemo_MapsBrowserKey_public_0000", False),
    ("analytics_key", "UA-DEMO-ANALYTICS-000-public", False),
    ("webhook_secret", "whsec_demo_signing_secret_never_displayed", True),
]
SECRET_PLAINTEXT = "whsec_demo_signing_secret_never_displayed"

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")
    return cond


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_cli(cmd: list[str], env=None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(f"command failed: {' '.join(cmd[:6])}")
    return result


def setup() -> tuple[dict, dict]:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    for d in (SECRET_DEMO, MANAGER_DEMO):
        d.mkdir(parents=True)

    env = dict(os.environ)
    env["HYPERMANAGER_DEMO_DIR"] = str(MANAGER_DEMO)
    run_cli(
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
        env,
    )
    env = dict(os.environ)
    env["HYPERSECRET_DEMO_DIR"] = str(SECRET_DEMO)
    run_cli(
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
        env,
    )

    secret_tokens = json.loads((SECRET_DEMO / "tokens.json").read_text())
    manager_tokens = json.loads((MANAGER_DEMO / "tokens.json").read_text())
    return secret_tokens, manager_tokens


def provision_keys(operator_token: str) -> None:
    env = dict(os.environ)
    env["HYPERSECRET_URL"] = SECRET_BASE
    env["HYPERSECRET_TOKEN"] = operator_token
    for name, value, _is_secret in DEMO_KEYS:
        run_cli(
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
        )


def rotate(operator_token: str, key: str, value: str) -> None:
    env = dict(os.environ)
    env["HYPERSECRET_URL"] = SECRET_BASE
    env["HYPERSECRET_TOKEN"] = operator_token
    run_cli(
        [
            "uv",
            "run",
            "python",
            "-m",
            "services.hypersecret.provision",
            "put",
            "prod/api",
            key,
            "--kek-file",
            str(KEK_FILE),
            "--value",
            value,
        ],
        env,
    )


def secret_env(manager_tokens: dict) -> dict:
    return {
        "HYPERSECRET_DEMO_DIR": str(SECRET_DEMO),
        "HYPERSECRET_MANAGER_URL": MANAGER_BASE,
        "HYPERSECRET_MANAGER_TOKEN": manager_tokens["producer:hypersecret"],
    }


def storefront_env(secret_tokens: dict, manager_tokens: dict) -> dict:
    return {
        "LIVE_CONFIG_HYPERSECRET_URL": SECRET_BASE,
        "LIVE_CONFIG_HYPERSECRET_TOKEN": secret_tokens["service:prod-api"],
        "LIVE_CONFIG_NAMESPACE": "prod/api",
        "LIVE_CONFIG_KEK_FILE": str(KEK_FILE),
        "LIVE_CONFIG_MANAGER_URL": MANAGER_BASE,
        "LIVE_CONFIG_MANAGER_TOKEN": manager_tokens["service:platform-api"],
        "LIVE_CONFIG_PORT": str(SF_PORT),
    }


def config_json() -> dict:
    return http_get(f"{SF_BASE}/config").json


def key_view(cfg: dict, name: str) -> dict:
    return cfg.get("keys", {}).get(name, {})


# ---------------------------------------------------------------------------


def test_initial_load(secret_tokens):
    print("\n== storefront loads provisioned config ==")
    cfg = config_json()
    check("feed reported connected", cfg.get("feed_connected") is True, str(cfg))
    for name, value, is_secret in DEMO_KEYS:
        view = key_view(cfg, name)
        check(f"{name} loaded at version 1", view.get("version") == 1, str(view))
        check(f"{name} available", view.get("available") is True, str(view))
        if is_secret:
            check(
                f"{name} value masked (no plaintext)",
                view.get("value") is None
                and view.get("masked", "").startswith("sha256:"),
                str(view),
            )
        else:
            check(
                f"{name} public value shown in full",
                view.get("value") == value,
                str(view),
            )


def test_secret_never_exposed():
    print("\n== secret-classified key is never exposed in plaintext ==")
    cfg_body = http_get(f"{SF_BASE}/config").body
    page_body = http_get(f"{SF_BASE}/").body
    events_body = _sse_snapshot()
    check("secret plaintext absent from /config", SECRET_PLAINTEXT not in cfg_body)
    check(
        "secret plaintext absent from / (status page)",
        SECRET_PLAINTEXT not in page_body,
    )
    check(
        "secret plaintext absent from /events snapshot",
        SECRET_PLAINTEXT not in events_body,
    )


def _sse_snapshot() -> str:
    """Read the initial SSE snapshot frame from /events over a raw socket."""
    import socket

    with socket.create_connection(("127.0.0.1", SF_PORT), timeout=5) as sock:
        sock.sendall(
            f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{SF_PORT}\r\n"
            f"Accept: text/event-stream\r\nConnection: close\r\n\r\n".encode()
        )
        sock.settimeout(3)
        buf = b""
        try:
            while b"snapshot" not in buf or b"\n\n" not in buf.split(b"snapshot", 1)[1]:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
        except TimeoutError:
            pass
    return buf.decode("utf-8", errors="replace")


def test_live_rotation(secret_tokens):
    print("\n== live rotation converges without restart ==")
    key = "stripe_pk_live"
    before = key_view(config_json(), key)
    before_version = before.get("version")
    new_value = "pk_live_ROTATED_v2_9999"
    rotate(secret_tokens["operator:admin"], key, new_value)

    converged = wait_for(
        lambda: (
            key_view(config_json(), key).get("version") == (before_version or 0) + 1
            and key_view(config_json(), key).get("value") == new_value
        ),
        timeout=20,
    )
    after = key_view(config_json(), key)
    check(
        "storefront converged to the rotated value live (no restart)",
        converged,
        f"before v{before_version}, after {after}",
    )
    check(
        "version incremented by exactly one",
        after.get("version") == (before_version or 0) + 1,
        str(after),
    )


def test_feed_state_is_honest(manager_runner):
    """The storefront reports the change feed's REAL state.

    ``feed_connected`` is read from the watcher's socket state, so a hub that
    goes away is reported as disconnected — the window where config keeps being
    served but stops being invalidated, and can age up to ``cache_ttl``. A
    locally-tracked flag (set optimistically when ``watch()`` returned) would
    keep claiming "connected" straight through the outage, which is precisely
    the state an operator must not be lied to about. Every step waits on the
    observed transition, never on elapsed time."""
    print("\n== feed state is honest: hub down → disconnected, back → connected ==")
    check(
        "feed reported connected before the hub bounce",
        config_json().get("feed_connected") is True,
        str(config_json()),
    )
    drops_before = config_json().get("feed_drops", 0)

    manager_runner.stop()
    check(
        "hub loss is reported as disconnected",
        wait_for(lambda: config_json().get("feed_connected") is False, timeout=30),
        str(config_json()),
    )
    check(
        "the lost feed session is counted",
        config_json().get("feed_drops", 0) > drops_before,
        f"before={drops_before} now={config_json().get('feed_drops')}",
    )
    view = key_view(config_json(), "maps_api_key")
    check(
        "config is still served from cache while the feed is down",
        view.get("available") is True and view.get("value") == DEMO_KEYS[1][1],
        str(view),
    )

    manager_runner.start()
    check(
        "the reconnect is reported as connected again",
        wait_for(lambda: config_json().get("feed_connected") is True, timeout=30),
        str(config_json()),
    )


# ---------------------------------------------------------------------------


def main() -> bool:
    print(
        f"Live-config E2E — secret {SECRET_PORT} / manager {MANAGER_PORT} "
        f"/ storefront {SF_PORT}"
    )
    secret_tokens, manager_tokens = setup()

    manager_runner = AppRunner(
        "services.hypermanager.app:app",
        host="127.0.0.1",
        port=MANAGER_PORT,
        readiness_path="/ready",
        env={"HYPERMANAGER_DEMO_DIR": str(MANAGER_DEMO)},
    )
    with (
        manager_runner,
        AppRunner(
            "services.hypersecret.app:app",
            host="127.0.0.1",
            port=SECRET_PORT,
            readiness_path="/ready",
            env=secret_env(manager_tokens),
        ),
    ):
        # Provision the storefront's keys BEFORE it starts, so its startup warm
        # fetch finds them.
        provision_keys(secret_tokens["operator:admin"])
        with AppRunner(
            "services.live_config.app:app",
            host="127.0.0.1",
            port=SF_PORT,
            readiness_path="/ready",
            env=storefront_env(secret_tokens, manager_tokens),
        ):
            test_initial_load(secret_tokens)
            test_secret_never_exposed()
            test_live_rotation(secret_tokens)
            test_feed_state_is_honest(manager_runner)

    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    if ERRORS:
        print("Failures:")
        for err in ERRORS:
            print(f"  - {err}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
