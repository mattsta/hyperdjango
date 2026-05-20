"""
Storefront (live_config) configuration.

One frozen config object loaded once at import through the framework's
site-config authority. Every field is overridable via ``LIVE_CONFIG_<FIELD>``
environment variables or an optional ``site.toml`` next to this file
(env beats TOML beats defaults).

This service is a *consumer*: it holds only the coordinates and credentials
needed to read its namespace from HyperSecret and subscribe to HyperManager's
change feed. It never holds a write or publish credential, and it never
persists a secret — least privilege, in memory only.
"""

from dataclasses import dataclass
from pathlib import Path

from hyperdjango.site_config import SiteConfig, load_site_config


@dataclass(frozen=True)
class StorefrontConfig(SiteConfig):
    name: str = "Storefront"
    tagline: str = "Live-config consumer of HyperSecret + HyperManager"

    # --- HyperSecret (the secret store this service reads) ----------------
    # base_url + a READ-only identity token for the namespace, plus the
    # namespace master key (KEK) file this service was handed out of band.
    # The KEK never touches the secret server — decryption is client-side.
    hypersecret_url: str = "http://127.0.0.1:8960"
    hypersecret_token: str = ""
    namespace: str = "prod/api"
    kek_file: str = ""

    # --- HyperManager (the change-notification hub this service watches) --
    # base_url + a SUBSCRIBE-only identity token whose grant covers this
    # namespace's subject prefix (``secrets/prod/``). No publish grant.
    manager_url: str = "http://127.0.0.1:8970"
    manager_token: str = ""

    # The keys this service loads, as ``name:classification`` pairs
    # (classification is ``public`` or ``secret``). A ``public`` value is safe
    # to display (e.g. a publishable API key); a ``secret`` value is used but
    # never exposed on any endpoint or log — only a fingerprint is shown.
    keys: str = (
        "stripe_pk_live:public,"
        "maps_api_key:public,"
        "analytics_key:public,"
        "webhook_secret:secret"
    )

    # Client cache: seconds an envelope is served without revalidation. Long by
    # design — the change feed, not the TTL, is what drives convergence, so a
    # rotation lands via a feed nudge long before the TTL would expire.
    cache_ttl: float = 300.0
    # Extra seconds a *stale* cached envelope may be served when the secret
    # store is briefly unreachable (opt-in bounded degradation). 0 = fail
    # closed. A small tolerance keeps the storefront serving its last-known
    # config across a momentary HyperSecret blip instead of hard-failing.
    stale_max: float = 30.0
    # Per-request timeout for calls to HyperSecret.
    fetch_timeout: float = 5.0
    # Seconds startup waits for the change feed's FIRST connect before serving.
    # Bounded, never required: a hub that is slow or down logs a warning and the
    # storefront comes up anyway (it serves its warmed cache), reporting the
    # feed as disconnected until it lands. Waiting at all means the reported
    # feed state is accurate the moment the service is ready, instead of
    # advertising a feed that is still coming up.
    feed_connect_timeout: float = 15.0

    # Port the storefront binds (overridable; the mesh wires 8980 by default).
    port: int = 8980

    def key_specs(self) -> list[tuple[str, bool]]:
        """Parse ``keys`` into ``[(name, is_secret), ...]``.

        A malformed entry (missing/unknown classification) is treated as
        ``public`` only when explicitly ``public``; anything else is treated as
        ``secret`` so an unclassified value fails *closed* to masked handling
        rather than accidentally displaying a secret in full.
        """
        specs: list[tuple[str, bool]] = []
        for raw in self.keys.split(","):
            entry = raw.strip()
            if not entry:
                continue
            name, _, classification = entry.partition(":")
            name = name.strip()
            if not name:
                continue
            is_secret = classification.strip().lower() != "public"
            specs.append((name, is_secret))
        return specs


def load_storefront_config(config_path: Path | None = None) -> StorefrontConfig:
    """Load config from env vars + optional site.toml + defaults."""
    if config_path is None:
        default_path = Path(__file__).parent / "site.toml"
        if default_path.exists():
            config_path = default_path
    return load_site_config(StorefrontConfig, "LIVE_CONFIG", config_path)
