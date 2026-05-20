"""
HyperSecret app configuration.

One frozen config object loaded once at import through the framework's
site-config authority. Every field is overridable via ``HYPERSECRET_<FIELD>``
environment variables or an optional ``site.toml`` next to this file
(env beats TOML beats defaults).
"""

from dataclasses import dataclass
from pathlib import Path

from hyperdjango.site_config import SiteConfig, load_site_config


@dataclass(frozen=True)
class HyperSecretConfig(SiteConfig):
    name: str = "HyperSecret"
    tagline: str = "Minimal self-hosted secret manager"

    # Authorization: seconds a cached identity→grants snapshot is served.
    # Bounded staleness for revocations of *grants* (token revocation is
    # checked per-request and takes effect immediately).
    grant_cache_ttl: float = 15.0

    # Audit writer: rows batch in memory and flush when the batch size or
    # the interval since the last flush is reached (amortized inline flush).
    audit_flush_interval: float = 0.25
    audit_flush_batch: int = 500

    # Days a soft-deleted secret is retained before the sweep purges it.
    retention_days: int = 30

    # Days an audit row is retained before the audit sweep trims it, and the
    # seconds between sweeps — keeps the append-only access log bounded.
    audit_retention_days: int = 90
    audit_sweep_interval: float = 3600.0

    # Seconds between retention sweeps (purges soft-deleted secrets past the
    # window). Hourly by default, like the sibling sweep-interval knobs.
    retention_sweep_interval: float = 3600.0

    # Seconds between rotation-due sweeps (publishes one `expired` change
    # event per secret whose declared rotation_due date has passed).
    rotation_sweep_interval: float = 300.0

    # Seconds between transactional-outbox drains (posts pending change events
    # to the hub; a short interval keeps live convergence prompt).
    outbox_drain_interval: float = 1.0

    # Ceiling on keys per batch-fetch request.
    batch_max_keys: int = 100

    # Cap on a secret's serialized-JSON metadata (annotation, not payload) so a
    # caller cannot smuggle bulk data through the metadata column. Measured with
    # the framework serializer the app uses everywhere (fast_json_dumps).
    max_metadata_bytes: int = 8192

    # App-wide request rate limit (framework RateLimitMiddleware, keyed per
    # client IP). Deliberately generous: a secrets API is polled steadily by
    # many services, so the cap exists to bound anonymous cardinality/audit
    # growth from unauthenticated grammar-valid spam, not to shape real traffic.
    rate_limit_requests: int = 20000
    rate_limit_window: int = 60

    # HyperManager hub for live change notifications. Empty = disabled.
    # The token belongs to an identity with a publish grant on "secrets/".
    manager_url: str = ""
    manager_token: str = ""

    # In-process mTLS terminator: set listen port + cert/key/CA to enable.
    # Verifies client certificates on the network socket and forwards to the
    # app's actual bound plaintext port (the terminator takes it from the
    # running server, so it can never desync) with attested identity headers.
    # Bind the plain port to loopback in this topology.
    mtls_listen_port: int = 0
    mtls_cert_file: str = ""
    mtls_key_file: str = ""
    mtls_ca_file: str = ""
    # Terminator backpressure: cap on concurrent client connections, and the
    # idle timeout (seconds) that reaps a peer stalling mid-request.
    mtls_max_connections: int = 512
    mtls_idle_timeout: float = 60.0

    # Where seed writes demo credentials (tokens + KEK files). Demo-only.
    demo_dir: str = str(Path(__file__).parent / ".demo")


def load_hypersecret_config(config_path: Path | None = None) -> HyperSecretConfig:
    """Load config from env vars + optional site.toml + defaults."""
    if config_path is None:
        default_path = Path(__file__).parent / "site.toml"
        if default_path.exists():
            config_path = default_path
    return load_site_config(HyperSecretConfig, "HYPERSECRET", config_path)
