"""
HyperManager seed — demo bootstrap.

Creates identities + prefix grants and a few sample change records.
Tokens (shown once) are written 0600 to ``services/hypermanager/.demo/``
(override: ``HYPERMANAGER_DEMO_DIR``) — the demo stand-in for your
deployment pipeline's credential distribution.
"""

import json
import os
from pathlib import Path

from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.logging import logger

from .config import load_hypermanager_config
from .models import ChangeEvent, ManagerIdentity, TopicGrant

IDENTITIES = {
    # name: (scopes, [(prefix, publish, subscribe), ...])
    # Scopes are coarse capabilities: "feed" is required for the whole
    # change-notification API (publish, replay, cursor, live feed); "admin" for
    # provisioning. Prefix grants authorize which subjects on top of the scope.
    "operator:admin": ("admin,feed", [("", False, False)]),
    "producer:hypersecret": ("feed", [("secrets/", True, False)]),
    "service:platform-api": ("feed", [("secrets/prod/", False, True)]),
    "service:staging-worker": ("feed", [("secrets/staging/", False, True)]),
}

SAMPLE_EVENTS = [
    ("producer:hypersecret", "secrets/prod/api/stripe_key", "created", {"version": 1}),
    (
        "producer:hypersecret",
        "secrets/staging/api/db_password",
        "created",
        {"version": 1},
    ),
]


async def run(db=None):
    """Seed HyperManager. Idempotent: skips if identities already exist."""
    existing = await ManagerIdentity.objects.count()
    if existing:
        logger.info(
            "  HyperManager already seeded ({n} identities). Skipping.", n=existing
        )
        return

    demo_dir = Path(load_hypermanager_config().demo_dir)
    demo_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    logger.info("  Seeding HyperManager (demo state → {d})...", d=demo_dir)

    tokens: dict[str, str] = {}
    for name, (scopes, grants) in IDENTITIES.items():
        result = await ManagerIdentity.generate(name=name, scopes=scopes)
        tokens[name] = result.raw_key
        for prefix, can_publish, can_subscribe in grants:
            if not prefix:
                continue
            await TopicGrant(
                identity_id=result.instance.id,
                prefix=prefix,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                granted_by="seed",
            ).save()
    tokens_path = demo_dir / "tokens.json"
    fd = os.open(tokens_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    logger.info(
        "    {n} identities + grants created (tokens → {p})",
        n=len(IDENTITIES),
        p=tokens_path,
    )

    for producer, subject, kind, metadata in SAMPLE_EVENTS:
        await ChangeEvent(
            producer=producer, subject=subject, kind=kind, metadata=metadata
        ).save()
    logger.info("    {n} sample events created", n=len(SAMPLE_EVENTS))

    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.success(
        "  HyperManager seeded: {i} identities, {e} events",
        i=len(IDENTITIES),
        e=len(SAMPLE_EVENTS),
    )
