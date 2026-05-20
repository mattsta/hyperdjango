"""
HyperSecret provisioning CLI — the trusted operator path.

Runs on an operator/CI host that holds namespace KEKs. Seals secrets locally
(plaintext never leaves this host), manages namespaces/identities/grants via
the admin API, and performs KEK rotation (rewrap).

    export HYPERSECRET_URL=http://127.0.0.1:8960
    export HYPERSECRET_TOKEN=hsk_...   # identity with admin/write scopes

    P=services.hypersecret.provision
    uv run python -m $P keygen --out prod-api.kek --kek-id prod-api-v1
    uv run python -m $P namespace create prod/api --kek-id prod-api-v1
    uv run python -m $P identity create service:prod-api --scopes read
    uv run python -m $P grant service:prod-api prod/api --read
    echo -n "sk_live_..." | uv run python -m $P put prod/api stripe_key --kek-file prod-api.kek
    uv run python -m $P get prod/api stripe_key --kek-file prod-api.kek
    uv run python -m $P rewrap prod/api --old-kek-file prod-api.kek \\
        --new-kek-file prod-api-v2.kek
    uv run python -m $P audit --namespace prod/api
"""

import argparse
import json
import os
import sys
from pathlib import Path

from hyperdjango.mtls import create_ca, issue_cert, write_pem

from .client import SecretsClient, SecretsError
from .envelope import (
    EnvelopeError,
    SealedEnvelope,
    generate_kek,
    kek_fingerprint,
    load_kek_file,
    rewrap_dek,
    write_kek_file,
)


def _client(args, namespace: str = "") -> SecretsClient:
    url = args.url or os.environ.get("HYPERSECRET_URL", "")
    token = args.token or os.environ.get("HYPERSECRET_TOKEN", "")
    if not url or not token:
        raise SecretsError(
            "Set HYPERSECRET_URL and HYPERSECRET_TOKEN (or --url/--token)"
        )
    kek, kek_id = None, ""
    kek_file = args.__dict__.get("kek_file")
    if kek_file:
        kek_id, kek = load_kek_file(kek_file)
    return SecretsClient(
        url, token=token, namespace=namespace, kek=kek, kek_id=kek_id, cache_ttl=0
    )


def _print(data) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


# -- commands ---------------------------------------------------------------


def cmd_keygen(args) -> int:
    kek = generate_kek()
    write_kek_file(args.out, args.kek_id, kek)
    print(f"Wrote KEK {args.kek_id} to {args.out} (mode 0600)")
    print(f"Fingerprint: {kek_fingerprint(kek)}")
    print("Distribute only to services granted this namespace. Never commit it.")
    return 0


def cmd_namespace_create(args) -> int:
    client = _client(args)
    _print(
        client.create_namespace(
            args.name,
            args.kek_id,
            description=args.description,
            owner=args.owner,
        )
    )
    return 0


def cmd_namespace_set_kek(args) -> int:
    client = _client(args)
    _print(client.set_namespace_kek(args.name, args.kek_id))
    return 0


def cmd_identity_create(args) -> int:
    client = _client(args)
    payload = client.create_identity(args.name, args.scopes)
    print(f"Identity: {payload['name']}  scopes: {payload['scopes']}")
    print(f"Token (shown once, store it now): {payload['token']}")
    return 0


def cmd_identity_revoke(args) -> int:
    client = _client(args)
    _print(client.revoke_identity(args.name))
    return 0


def cmd_grant(args) -> int:
    client = _client(args)
    _print(
        client.put_grant(
            args.identity, args.namespace, read=args.read, write=args.write
        )
    )
    return 0


def cmd_grants(args) -> int:
    client = _client(args)
    _print(client.review_grants(namespace=args.namespace))
    return 0


def cmd_put(args) -> int:
    if args.value is not None:
        plaintext = args.value.encode()
    elif args.file:
        plaintext = Path(args.file).read_bytes()
    else:
        plaintext = sys.stdin.buffer.read()
    if not plaintext:
        raise SecretsError("Refusing to store an empty secret")

    client = _client(args, namespace=args.namespace)
    metadata = json.loads(args.metadata) if args.metadata else None
    version = client.put_secret(args.key, plaintext, metadata=metadata)
    print(f"{args.namespace}/{args.key} → version {version} (kek {client.kek_id})")
    return 0


def cmd_get(args) -> int:
    client = _client(args, namespace=args.namespace)
    value = client.get_secret(args.key, version=args.version)
    sys.stdout.write(value)
    if sys.stdout.isatty():
        sys.stdout.write("\n")
    return 0


def cmd_versions(args) -> int:
    client = _client(args, namespace=args.namespace)
    _print(client.versions(args.key))
    return 0


def cmd_list(args) -> int:
    client = _client(args, namespace=args.namespace)
    _print(client.list_keys())
    return 0


def cmd_delete(args) -> int:
    client = _client(args, namespace=args.namespace)
    client.delete_secret(args.key, purge=args.purge)
    print(f"{'Purged' if args.purge else 'Soft-deleted'} {args.namespace}/{args.key}")
    return 0


def cmd_rewrap(args) -> int:
    """KEK rotation: rewrap every version of every key, then declare the new
    KEK generation on the namespace.

    Resume-safe: a version already carrying ``new_kek_id`` is skipped, so a
    rerun after a mid-rotation crash does not try to unwrap it with the old KEK
    (which fails — the wrap-layer AAD binds kek_id). Write-safe: the rewrap pass
    repeats to a fixpoint (until a full pass finds nothing still on the old KEK)
    BEFORE declaring the new generation, so a put that lands under the old KEK
    while an earlier pass is running is picked up by a later pass instead of
    being stranded undecryptable. A tiny window remains between the final pass
    and the set-kek call (documented in the README rotation section)."""
    _old_kek_id, old_kek = load_kek_file(args.old_kek_file)
    new_kek_id, new_kek = load_kek_file(args.new_kek_file)
    client = _client(args, namespace=args.namespace)

    def _rewrap_pass() -> int:
        """One full scan: rewrap every version still sealed under the old KEK.

        include_deleted: rewrap soft-deleted-but-retained secrets too, else a
        revived secret's earlier versions would be undecryptable once the old
        KEK is retired. include_deleted on the per-version fetch lets us read
        their envelopes (admin only).
        """
        done = 0
        for info in client.list_keys(include_deleted=True):
            key = info["key"]
            for v in client.versions(key)["versions"]:
                # Already on the new KEK (a prior interrupted run rewrapped it):
                # skip — unwrapping it with old_kek would fail the AAD check.
                if v["kek_id"] == new_kek_id:
                    continue
                payload = client.fetch_envelope_raw(
                    key, version=v["version"], include_deleted=True
                )
                env = SealedEnvelope.from_dict(payload)
                new_dek = rewrap_dek(
                    env,
                    old_kek=old_kek,
                    new_kek=new_kek,
                    new_kek_id=new_kek_id,
                    namespace=args.namespace,
                    key=key,
                    version=v["version"],
                )
                client.rewrap_version(key, v["version"], new_dek, new_kek_id)
                done += 1
        return done

    rewrapped = 0
    # Bounded pre-repoint passes: writes landing during rotation still seal
    # under the OLD kek (the namespace declares it until set-kek below), so a
    # busy namespace could keep producing old-KEK versions indefinitely. The
    # bound is safe because completion does not depend on reaching a fixpoint
    # here: after set-kek the server rejects old-KEK writes, so the single
    # post-repoint pass below drains the finite remainder deterministically.
    for _ in range(20):
        pass_rewraps = _rewrap_pass()
        rewrapped += pass_rewraps
        if pass_rewraps == 0:
            break
    client.set_namespace_kek(args.namespace, new_kek_id)
    # The namespace now declares the new KEK, so no further old-KEK write can
    # be accepted; whatever raced the repoint is a finite set — drain it.
    rewrapped += _rewrap_pass()
    print(
        f"Rewrapped {rewrapped} versions in {args.namespace}; "
        f"namespace now declares {new_kek_id}"
    )
    print(f"New KEK fingerprint: {kek_fingerprint(new_kek)}")
    print("Distribute the new KEK to granted services, then retire the old file.")
    return 0


def cmd_ca_init(args) -> int:
    """Create the private CA that anchors mTLS client authentication."""
    ca_dir = Path(args.dir)
    ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path, cert_path = ca_dir / "ca.key", ca_dir / "ca.crt"
    if key_path.exists():
        raise SecretsError(f"{key_path} already exists — refusing to overwrite a CA")
    key_pem, cert_pem = create_ca(args.name, days=args.days)
    write_pem(str(key_path), key_pem, private=True)
    write_pem(str(cert_path), cert_pem)
    print(f"CA created: {cert_path} (key {key_path}, mode 0600)")
    print("Distribute ca.crt to servers and clients; guard ca.key like a KEK.")
    return 0


def cmd_cert_issue(args) -> int:
    """Issue a certificate: client certs carry the identity name as CN."""
    ca_dir = Path(args.ca_dir)
    key_path = Path(args.out_prefix + ".key")
    cert_path = Path(args.out_prefix + ".crt")
    if not args.force and (key_path.exists() or cert_path.exists()):
        raise SecretsError(
            f"{args.out_prefix}.crt/.key already exists — pass --force to "
            f"reissue (rotating a cert invalidates the old one)"
        )
    if args.force:
        key_path.unlink(missing_ok=True)
        cert_path.unlink(missing_ok=True)
    ca_key = (ca_dir / "ca.key").read_bytes()
    ca_cert = (ca_dir / "ca.crt").read_bytes()
    key_pem, cert_pem = issue_cert(
        ca_key,
        ca_cert,
        args.cn,
        days=args.days,
        server=args.server,
        san_dns=args.dns.split(",") if args.dns else None,
    )
    write_pem(args.out_prefix + ".key", key_pem, private=True)
    write_pem(args.out_prefix + ".crt", cert_pem)
    kind = "server" if args.server else "client"
    print(f"Issued {kind} certificate CN={args.cn}: {args.out_prefix}.crt/.key")
    if not args.server:
        print(
            "The CN must match an existing identity name — "
            "create it with `identity create` and grant its namespaces."
        )
    return 0


def cmd_audit(args) -> int:
    client = _client(args)
    _print(
        client.query_audit(
            namespace=args.namespace,
            key=args.key,
            identity=args.identity,
            action=args.action,
            outcome=args.outcome,
            limit=args.limit,
        )
    )
    return 0


# -- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hypersecret-provision",
        description="Operator CLI for HyperSecret: certificate authority, "
        "namespaces, identities, grants, and sealed secrets.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default="", help="server URL (or HYPERSECRET_URL)")
    parser.add_argument("--token", default="", help="token (or HYPERSECRET_TOKEN)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="generate a namespace master key file")
    p.add_argument("--out", required=True)
    p.add_argument("--kek-id", required=True, help="generation id, e.g. prod-api-v1")
    p.set_defaults(fn=cmd_keygen)

    ns = sub.add_parser("namespace", help="namespace management").add_subparsers(
        dest="nscmd", required=True
    )
    p = ns.add_parser("create")
    p.add_argument("name")
    p.add_argument("--kek-id", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--owner", default="")
    p.set_defaults(fn=cmd_namespace_create)
    p = ns.add_parser("set-kek")
    p.add_argument("name")
    p.add_argument("--kek-id", required=True)
    p.set_defaults(fn=cmd_namespace_set_kek)

    ident = sub.add_parser("identity", help="identity management").add_subparsers(
        dest="idcmd", required=True
    )
    p = ident.add_parser("create")
    p.add_argument("name")
    p.add_argument("--scopes", default="read")
    p.set_defaults(fn=cmd_identity_create)
    p = ident.add_parser("revoke")
    p.add_argument("name")
    p.set_defaults(fn=cmd_identity_revoke)

    p = sub.add_parser("grant", help="grant an identity access to a namespace")
    p.add_argument("identity")
    p.add_argument("namespace")
    p.add_argument("--read", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--write", action=argparse.BooleanOptionalAction, default=False)
    p.set_defaults(fn=cmd_grant)

    p = sub.add_parser("grants", help="review grants")
    p.add_argument("--namespace", default="")
    p.set_defaults(fn=cmd_grants)

    p = sub.add_parser("put", help="seal + store a new version (rotation too)")
    p.add_argument("namespace")
    p.add_argument("key")
    p.add_argument("--kek-file", required=True)
    p.add_argument("--value", default=None, help="literal value (else stdin/--file)")
    p.add_argument("--file", default="", help="read value from file")
    p.add_argument("--metadata", default="", help="JSON object")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("get", help="fetch + decrypt")
    p.add_argument("namespace")
    p.add_argument("key")
    p.add_argument("--kek-file", required=True)
    p.add_argument("--version", type=int, default=None)
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("versions", help="version history + provenance")
    p.add_argument("namespace")
    p.add_argument("key")
    p.set_defaults(fn=cmd_versions)

    p = sub.add_parser("list", help="list keys in a namespace")
    p.add_argument("namespace")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("delete", help="soft delete (--purge for hard)")
    p.add_argument("namespace")
    p.add_argument("key")
    p.add_argument("--purge", action="store_true")
    p.set_defaults(fn=cmd_delete)

    p = sub.add_parser("rewrap", help="KEK rotation for a whole namespace")
    p.add_argument("namespace")
    p.add_argument("--old-kek-file", required=True)
    p.add_argument("--new-kek-file", required=True)
    p.set_defaults(fn=cmd_rewrap)

    ca = sub.add_parser("ca", help="certificate authority").add_subparsers(
        dest="cacmd", required=True
    )
    p = ca.add_parser("init")
    p.add_argument("--dir", required=True)
    p.add_argument("--name", default="hypersecret-ca")
    p.add_argument("--days", type=int, default=3650)
    p.set_defaults(fn=cmd_ca_init)

    cert = sub.add_parser("cert", help="certificate issuance").add_subparsers(
        dest="certcmd", required=True
    )
    p = cert.add_parser("issue")
    p.add_argument("cn", help="identity name (client) or hostname label (server)")
    p.add_argument("--ca-dir", required=True)
    p.add_argument("--out-prefix", required=True, help="writes <prefix>.crt/.key")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--server", action="store_true")
    p.add_argument("--dns", default="", help="comma-separated SANs (server certs)")
    p.add_argument("--force", action="store_true", help="overwrite existing cert/key")
    p.set_defaults(fn=cmd_cert_issue)

    p = sub.add_parser("audit", help="query the access trail")
    p.add_argument("--namespace", default="")
    p.add_argument("--key", default="")
    p.add_argument("--identity", default="")
    p.add_argument("--action", default="")
    p.add_argument("--outcome", default="")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (SecretsError, EnvelopeError) as exc:
        # EnvelopeError covers a crypto/rotation failure (e.g. a partial rewrap
        # rerun hitting a version already on the new KEK, or a bad KEK file):
        # report it cleanly instead of dumping a traceback and leaving the
        # operator unsure whether the rotation is wedged.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
