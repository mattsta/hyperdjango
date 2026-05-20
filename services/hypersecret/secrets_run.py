"""
secrets-run — application-transparent secret injection.

Fetches + decrypts a service's secrets and either ``exec``s the real program
with them in its environment (systemd ``ExecStart=``, Docker ``ENTRYPOINT``)
or writes a 0600 env file for systemd ``EnvironmentFile=`` (oneshot pattern —
see deploy/secrets-fetch@.service). The application itself just reads
ordinary environment variables.

Configuration comes from the environment (set in the unit file):

    HYPERSECRET_URL         server base URL
    HYPERSECRET_TOKEN       service identity token (hsk_...)
    HYPERSECRET_NAMESPACE   env/service namespace
    HYPERSECRET_KEK_FILE    path to the base64 namespace master key (0600)

Usage:

    # exec mode — wrap the real program
    python -m services.hypersecret.secrets_run -- /usr/local/bin/myapp --port 9000

    # explicit mapping (ENV_VAR=secret_key lines; '#' comments)
    python -m services.hypersecret.secrets_run --map /etc/myapp/secrets.map -- myapp

    # env-file mode — for systemd EnvironmentFile=
    python -m services.hypersecret.secrets_run --output /run/secrets/myapp.env

Strict by default: any missing or undecryptable secret aborts the launch
(exit 1) instead of starting the app half-configured. Secret names never
appear in argv of the child, and values never touch disk in exec mode.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from .client import SecretsClient, SecretsError

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _env_name_for(key: str) -> str:
    """Default mapping: secret key → env var (stripe_key → STRIPE_KEY).

    Key names may begin with a digit; env var names may not — prefix those.
    """
    name = re.sub(r"[^A-Z0-9_]", "_", key.upper())
    return name if name[0].isalpha() or name[0] == "_" else "_" + name


def parse_map_file(path: str) -> dict[str, str]:
    """Parse ``ENV_VAR=secret_key`` lines into {env_var: secret_key}."""
    mapping: dict[str, str] = {}
    with Path(path).open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"{path}:{lineno}: expected ENV_VAR=secret_key")
            env_var, _, secret_key = line.partition("=")
            env_var = env_var.strip()
            if not _ENV_NAME_RE.match(env_var):
                raise ValueError(f"{path}:{lineno}: bad env var name {env_var!r}")
            mapping[env_var] = secret_key.strip()
    return mapping


def _mapping_from_keys(keys: list[str]) -> dict[str, str]:
    """Build {env_var: secret_key} from secret keys, refusing collisions.

    ``_env_name_for`` is many-to-one (``db-password`` and ``db_password`` both
    fold to ``DB_PASSWORD``), so a bare dict comprehension would silently drop
    one secret and inject the other under it. Fail loudly instead."""
    mapping: dict[str, str] = {}
    for key in keys:
        env_var = _env_name_for(key)
        if env_var in mapping:
            raise SecretsError(
                f"Env var name collision: {mapping[env_var]!r} and {key!r} both "
                f"map to {env_var}. Use an explicit --map file to disambiguate."
            )
        mapping[env_var] = key
    return mapping


def build_mapping(args, client: SecretsClient) -> dict[str, str]:
    if args.map:
        return parse_map_file(args.map)
    if args.keys:
        return _mapping_from_keys([k for k in args.keys.split(",") if k])
    if args.all:
        # Opt-in "everything in the namespace". Explicit because injecting
        # every secret a service can read — including ones it doesn't need —
        # is the wrong default for a secrets tool (over-provisioning widens
        # blast radius, and adding a key silently changes a running service).
        return _mapping_from_keys([k["key"] for k in client.list_keys()])
    raise SecretsError(
        "specify which secrets to inject: --map <file>, --keys a,b,c, or "
        "--all (inject every key in the namespace, opt-in)"
    )


def fetch_all(client: SecretsClient, mapping: dict[str, str]) -> dict[str, str]:
    secret_keys = sorted(set(mapping.values()))
    values = client.get_secrets(secret_keys)  # strict: raises on any miss
    return {env_var: values[secret_key] for env_var, secret_key in mapping.items()}


# A value is safe to emit bare only if it has no whitespace, quotes, or
# backslash — otherwise systemd's EnvironmentFile parser would strip surrounding
# whitespace or mis-handle the quote/escape, so we double-quote it instead.
_BARE_ENV_VALUE_RE = re.compile(r"^[^\s\"'\\]*$")


def _format_env_value(name: str, value: str) -> str:
    """Render a value for a systemd EnvironmentFile so it round-trips exactly.

    Newlines and carriage returns cannot be represented on a single env-file
    line (systemd would truncate at the break), so they are refused. Everything
    else round-trips: a plain value is emitted bare; anything with whitespace,
    quotes, or a backslash is double-quoted with ``\\`` and ``"`` escaped, so
    leading/trailing spaces and embedded quotes survive the parse."""
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"{name}: values containing newlines or carriage returns cannot be "
            f"written to an env file — use exec mode or the SDK directly"
        )
    if _BARE_ENV_VALUE_RE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env_file(path: str, env: dict[str, str]) -> None:
    """Write a systemd EnvironmentFile, created 0600 before any content."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for name, value in sorted(env.items()):
            f.write(f"{name}={_format_env_value(name, value)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="secrets-run", description="Inject HyperSecret secrets and exec."
    )
    parser.add_argument("--map", help="ENV_VAR=secret_key mapping file")
    parser.add_argument("--keys", help="comma-separated secret keys to inject")
    parser.add_argument(
        "--all",
        action="store_true",
        help="inject EVERY key in the namespace (opt-in; over-broad by design)",
    )
    parser.add_argument("--output", help="write env file here instead of exec-ing")
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- command [args...] to exec"
    )
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not args.output and not command:
        parser.error("either a command (exec mode) or --output (env-file mode)")
    if args.output and command:
        parser.error("--output and a command are mutually exclusive")

    try:
        client = SecretsClient.from_env()
        mapping = build_mapping(args, client)
        if not mapping:
            raise SecretsError(f"No secrets found in {client.namespace}")
        env_updates = fetch_all(client, mapping)
    except (SecretsError, OSError, ValueError) as exc:
        print(f"secrets-run: {exc}", file=sys.stderr)
        return 1

    if args.output:
        write_env_file(args.output, env_updates)
        print(f"secrets-run: wrote {len(env_updates)} secrets to {args.output}")
        return 0

    os.environ.update(env_updates)
    # Replace this process — the app sees a normal environment.
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"secrets-run: exec {command[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
