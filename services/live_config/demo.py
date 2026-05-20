"""
Live-config walkthrough — the money shot: a rotation propagates with no restart.

Run it against a mesh already up (``python -m services.live_config.run_mesh`` in
another terminal):

    uv run python -m services.live_config.demo

It reads the storefront's current config (noting the publishable Stripe key's
version), rotates that key in HyperSecret through the operator CLI, then polls
the storefront until the version increments — proving the change propagated
through the HyperManager feed into the running consumer, live. It prints a
before/after and confirms the secret-classified key is never exposed.
"""

import http.client
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

from .run_mesh import HS_URL, KEK_FILE, PROJECT_ROOT, SECRET_DEMO, SF_URL, SIGNING_ENV


def _get_json(url: str) -> dict:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.request("GET", parsed.path or "/")
        resp = conn.getresponse()
        return json.loads(resp.read().decode())
    finally:
        conn.close()


def _key(config: dict, name: str) -> dict:
    return config.get("keys", {}).get(name, {})


def _rotate(operator_token: str, key: str, value: str) -> None:
    env = os.environ.copy()
    env.update(SIGNING_ENV)
    env["HYPERSECRET_URL"] = HS_URL
    env["HYPERSECRET_TOKEN"] = operator_token
    result = subprocess.run(
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
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise RuntimeError("rotation failed — is the mesh up?")


def main() -> int:
    tokens_path = SECRET_DEMO / "tokens.json"
    if not tokens_path.exists():
        print("No runtime credentials — start the mesh first:")
        print("    uv run python -m services.live_config.run_mesh")
        return 1
    operator = json.loads(tokens_path.read_text())["operator:admin"]

    key = "stripe_pk_live"
    before = _get_json(f"{SF_URL}/config")
    before_key = _key(before, key)
    print("BEFORE")
    print(
        f"  {key}: version {before_key.get('version')}  value {before_key.get('value')!r}"
    )
    print(f"  feed_connected: {before.get('feed_connected')}")

    secret_view = _key(before, "webhook_secret")
    assert secret_view.get("value") is None, "secret plaintext must never appear!"
    print(
        f"  webhook_secret (secret): masked as {secret_view.get('masked')!r} "
        f"— plaintext never exposed"
    )

    new_value = f"pk_live_ROTATED_{int(time.time())}"
    print(f"\nROTATING {key} → {new_value!r} via the HyperSecret operator CLI…")
    _rotate(operator, key, new_value)

    target = (before_key.get("version") or 0) + 1
    deadline = time.monotonic() + 20.0
    after_key: dict = {}
    while time.monotonic() < deadline:
        after_key = _key(_get_json(f"{SF_URL}/config"), key)
        if (after_key.get("version") or 0) >= target and after_key.get(
            "value"
        ) == new_value:
            break
        time.sleep(0.25)

    print("\nAFTER (storefront converged live — no restart)")
    print(
        f"  {key}: version {after_key.get('version')}  value {after_key.get('value')!r}"
    )

    ok = after_key.get("version") == target and after_key.get("value") == new_value
    if ok:
        print(
            f"\n✓ Live propagation confirmed: v{before_key.get('version')} → "
            f"v{after_key.get('version')} with no restart."
        )
        return 0
    print("\n✗ Storefront did not converge within the deadline.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
