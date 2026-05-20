"""
HyperManager app configuration.

Loaded once at import through the site-config authority; every field is
overridable via ``HYPERMANAGER_<FIELD>`` env vars or a ``site.toml`` next
to this file (env beats TOML beats defaults).
"""

from dataclasses import dataclass
from pathlib import Path

from hyperdjango.site_config import SiteConfig, load_site_config


@dataclass(frozen=True)
class HyperManagerConfig(SiteConfig):
    name: str = "HyperManager"
    tagline: str = "Infrastructure change-notification hub"

    # Seconds a cached identity→grants snapshot is served (bounded staleness
    # for grant changes; token/cert revocation is checked per request).
    caller_cache_ttl: float = 15.0

    # --- Delivery tier selectors ------------------------------------------
    # The hub runs one of three delivery models, chosen by these two fields.
    #
    #   ledger_mode  ring_size   tier        contract
    #   -----------  ---------   ---------   -------------------------------
    #   False        > 0         catchup     DEFAULT. Live in-memory pub/sub:
    #                                        publish assigns an in-memory seq
    #                                        and pushes the event to connected
    #                                        subscribers; a bounded ring lets a
    #                                        brief reconnect replay exactly the
    #                                        missed events, resyncing on overrun
    #                                        or restart. No Postgres on this path.
    #   False        == 0        ephemeral   Simplest tier: same live push, no
    #                                        ring, every subscriber resyncs on
    #                                        (re)connect.
    #   True         (n/a)       ledger      OPT-IN durable audited log: publish
    #                                        writes the append-only ChangeEvent
    #                                        row, the feed sends wake hints, and
    #                                        subscribers pull ordered replay. The
    #                                        only tier with at-least-once ordered
    #                                        replay + retention + /v1/events.
    #
    # Default off: a hub is a live notifier until an operator opts into the
    # heavier persistent-audit tier.
    ledger_mode: bool = False

    # Bounded in-memory catch-up ring for the default (non-ledger) tiers: the
    # number of recent events retained for reconnect replay. A reconnecting
    # subscriber still inside this window replays exactly what it missed; one
    # that fell further behind (or reconnected after a restart) is told to
    # full-resync. 0 selects the pure-ephemeral tier (no ring at all).
    catch_up_ring_size: int = 1024

    # Audit writer: rows batch in memory and flush when the batch size or the
    # interval since the last flush is reached (amortized inline flush, with a
    # periodic background drain).
    audit_flush_interval: float = 0.25
    audit_flush_batch: int = 200

    # JSON-encoded metadata ceiling per event (metadata-only contract). Applies
    # to every tier — a change record carries a nudge, never a payload.
    metadata_max_bytes: int = 4096

    # --- Ledger-mode knobs (ledger_mode=True only) ------------------------
    # Maximum events returned by one durable replay page.
    replay_limit: int = 500

    # Days of ledger retention; older events are trimmed by the scheduler.
    # Subscribers further behind than this must full-resync their producer.
    retention_days: int = 14

    # Seconds between ledger-retention sweeps (trims events past the window and
    # raises the replay floor). Hourly by default, like the other periodic knobs.
    retention_sweep_interval: float = 3600.0

    # Cross-replica wake-up fan-out via Postgres LISTEN/NOTIFY. Off = the
    # in-memory layer (single process). The ledger is authoritative either way.
    pg_fanout: bool = False

    # In-process mTLS terminator: set listen port + cert/key/CA to enable.
    # The terminator listens with required client certificates and forwards to
    # the app's actual bound plaintext port (the terminator takes it from the
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

    # Where seed writes demo credentials. Demo-only.
    demo_dir: str = str(Path(__file__).parent / ".demo")


def load_hypermanager_config(config_path: Path | None = None) -> HyperManagerConfig:
    if config_path is None:
        default_path = Path(__file__).parent / "site.toml"
        if default_path.exists():
            config_path = default_path
    return load_site_config(HyperManagerConfig, "HYPERMANAGER", config_path)
