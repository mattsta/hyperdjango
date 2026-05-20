"""
HyperSecret seed — demo bootstrap acting as the trusted operator.

Creates namespaces, identities, grants, and sample sealed secrets. Because
this runs in the ``hyper setup`` process (operator context, direct DB), it
may hold KEKs — the *server request path* still never sees key material.
In production these steps happen through ``provision.py`` over HTTP.

Demo state (tokens shown-once + KEK files) is written 0600 into
``services/hypersecret/.demo/`` (override: ``HYPERSECRET_DEMO_DIR``).
That directory is gitignored; it is the demo stand-in for your deployment
pipeline's secret distribution step.

Layout seeded (a payment key readable only by production API servers, with
frontend and staging fully isolated):

    prod/api      stripe_key, db_password, jwt_secret   ← service:prod-api only
    prod/frontend cdn_purge_token                       ← service:prod-frontend
    staging/api   db_password                           ← service:staging-api
"""

import json
import os
from pathlib import Path

from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.logging import logger

from .config import load_hypersecret_config
from .envelope import generate_kek, load_kek_file, seal, write_kek_file
from .models import (
    Namespace,
    NamespaceGrant,
    Secret,
    SecretVersion,
    ServiceIdentity,
)

NAMESPACES = {
    "prod/api": "prod-api-v1",
    "prod/frontend": "prod-frontend-v1",
    "staging/api": "staging-api-v1",
}

IDENTITIES = {
    # name: (scopes, [(namespace, read, write), ...])
    "operator:admin": (
        "read,write,admin,audit",
        [(ns, True, True) for ns in NAMESPACES],
    ),
    "service:prod-api": ("read", [("prod/api", True, False)]),
    "service:prod-frontend": ("read", [("prod/frontend", True, False)]),
    "service:staging-api": ("read", [("staging/api", True, False)]),
    "ci:deployer": ("read,write", [(ns, True, True) for ns in NAMESPACES]),
}

SECRETS = {
    "prod/api": {
        "stripe_key": ("sk_live_demo_4242424242424242", {"owner": "platform-team"}),
        "db_password": ("prod-api-db-pw-3f9c2e", {"owner": "platform-team"}),
        "jwt_secret": ("jwt-signing-demo-77aa1b", {"owner": "auth-team"}),
    },
    "prod/frontend": {
        "cdn_purge_token": ("cdn-purge-demo-91d0af", {"owner": "frontend-team"}),
    },
    "staging/api": {
        "db_password": ("staging-db-pw-not-prod", {"owner": "platform-team"}),
    },
}


def _demo_dir() -> Path:
    return Path(load_hypersecret_config().demo_dir)


def _kek_filename(namespace: str) -> str:
    return namespace.replace("/", "-") + ".kek"


async def run(db=None):
    """Seed HyperSecret. Idempotent and resumable: every section (namespaces,
    identities, grants, secrets) is get-or-create, so a rerun after a PARTIAL
    failure — some rows written, the process then crashed — completes the
    missing pieces instead of crashing on a unique constraint or skipping the
    work a coarse "already seeded" short-circuit would have hidden. Only newly
    minted tokens are (re)written to tokens.json; an all-present rerun leaves
    the already-emitted, shown-once tokens on disk untouched."""
    demo_dir = _demo_dir()
    demo_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    logger.info("  Seeding HyperSecret (demo state → {d})...", d=demo_dir)

    # Namespaces + KEKs (operator-side key material). Get-or-create: a namespace
    # that already exists keeps its stored KEK (loaded from the demo file) so a
    # rerun neither duplicate-inserts nor re-keys secrets under a fresh KEK.
    keks: dict[str, bytes] = {}
    ns_rows: dict[str, Namespace] = {}
    for name, kek_id in NAMESPACES.items():
        ns = await Namespace.objects.filter(name=name).first()
        kek_path = demo_dir / _kek_filename(name)
        if ns is None:
            kek = generate_kek()
            if kek_path.exists():
                kek_path.unlink()  # stale demo state from a dropped database
            write_kek_file(str(kek_path), kek_id, kek)
            ns = Namespace(name=name, kek_id=kek_id, owner="platform-team")
            await ns.save()
        else:
            _kid, kek = load_kek_file(str(kek_path))
        keks[name] = kek
        ns_rows[name] = ns
    logger.info("    {n} namespaces + KEK files ready", n=len(NAMESPACES))

    # Identities (tokens shown once → demo tokens file) + grants. Each is
    # get-or-create; a freshly-minted token is written to tokens.json (an
    # already-existing identity's token was shown on its original run).
    tokens: dict[str, str] = {}
    for name, (scopes, grants) in IDENTITIES.items():
        identity = await ServiceIdentity.objects.filter(name=name).first()
        if identity is None:
            result = await ServiceIdentity.generate(name=name, scopes=scopes)
            tokens[name] = result.raw_key
            identity = result.instance
        for ns_name, can_read, can_write in grants:
            grant_exists = await NamespaceGrant.objects.filter(
                identity_id=identity.id, namespace_id=ns_rows[ns_name].id
            ).exists()
            if not grant_exists:
                await NamespaceGrant(
                    identity_id=identity.id,
                    namespace_id=ns_rows[ns_name].id,
                    can_read=can_read,
                    can_write=can_write,
                    granted_by="seed",
                ).save()
    # Only rewrite tokens.json when this run actually minted tokens. An
    # all-identities-present rerun mints nothing, and truncating the file to an
    # empty object would destroy the shown-once credentials from the run that
    # created them.
    if tokens:
        tokens_path = demo_dir / "tokens.json"
        fd = os.open(tokens_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        logger.info(
            "    {n} identities minted (tokens → {p})",
            n=len(tokens),
            p=tokens_path,
        )
    else:
        logger.info("    identities + grants already present (tokens unchanged)")

    # Sample secrets — sealed operator-side, stored as ciphertext only.
    # Get-or-create: skip a secret that already exists so a partial rerun does
    # not collide on the unique (namespace_id, key) constraint.
    count = 0
    for ns_name, entries in SECRETS.items():
        ns = ns_rows[ns_name]
        for key, (value, metadata) in entries.items():
            if await Secret.objects.filter(namespace_id=ns.id, key=key).exists():
                continue
            envelope = seal(
                value.encode(),
                kek=keks[ns_name],
                kek_id=ns.kek_id,
                namespace=ns_name,
                key=key,
                version=1,
            )
            secret = Secret(
                namespace_id=ns.id, key=key, current_version=1, metadata=metadata
            )
            await secret.save()
            await SecretVersion(
                secret_id=secret.id,
                version=1,
                alg=envelope.alg,
                kek_id=envelope.kek_id,
                ciphertext=envelope.ciphertext,
                encrypted_dek=envelope.encrypted_dek,
                created_by="seed",
            ).save()
            count += 1
    logger.info("    {n} sealed secrets created", n=count)

    # HyperAdmin panel user (hyper_users table, separate from identities)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.success(
        "  HyperSecret seeded: {ns} namespaces, {ident} identities, {s} secrets",
        ns=len(NAMESPACES),
        ident=len(IDENTITIES),
        s=count,
    )
