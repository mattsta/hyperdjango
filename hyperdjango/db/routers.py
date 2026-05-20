"""
Database router for read replica routing.

Automatically routes read queries to replica databases and write queries
to the primary. Supports health-check failover.

Usage in Django settings:
    DATABASES = {
        'default': {
            'ENGINE': 'hyperdjango.db',
            'NAME': 'mydb',
            'HOST': 'primary.db.example.com',
        },
        'replica': {
            'ENGINE': 'hyperdjango.db',
            'NAME': 'mydb',
            'HOST': 'replica.db.example.com',
        },
    }
    DATABASE_ROUTERS = ['hyperdjango.db.routers.ReadReplicaRouter']
"""

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Cached tuple of exception types that indicate a *genuine* replica
# connectivity problem (as opposed to a bug in our own routing code). Resolved
# lazily to avoid a hard Django import at module load.
_CONN_ERR_TYPES: tuple[type[BaseException], ...] | None = None


def _connection_error_types() -> tuple[type[BaseException], ...]:
    global _CONN_ERR_TYPES
    if _CONN_ERR_TYPES is None:
        try:
            from django.db.utils import (
                ConnectionDoesNotExist,
                InterfaceError,
                OperationalError,
            )

            _CONN_ERR_TYPES = (
                OperationalError,
                InterfaceError,
                ConnectionDoesNotExist,
            )
        except ImportError:
            _CONN_ERR_TYPES = ()
    return _CONN_ERR_TYPES


@dataclass(slots=True)
class ReadReplicaRouter:
    """Routes reads to replica, writes to primary.

    Implements Django's database router protocol:
    - db_for_read() → 'replica' (or 'default' if replica unavailable)
    - db_for_write() → 'default' (always primary)
    - allow_relation() → True (cross-database relations ok)
    - allow_migrate() → True only for 'default'

    Failover is self-healing: a genuine replica connection failure marks the
    replica unhealthy for ``health_cooldown_seconds`` and logs it. After the
    cooldown a single re-probe decides whether the replica has recovered, so a
    transient blip no longer permanently pins every read to the primary.
    """

    replica_alias: str = "replica"
    primary_alias: str = "default"
    health_cooldown_seconds: float = 30.0
    _replica_healthy: bool = field(init=False, default=True)
    _unhealthy_at: float = field(init=False, default=0.0)

    def _probe_replica(self) -> bool:
        """Return True if the replica connection is currently usable.

        On a genuine connection error the replica is marked unhealthy (with a
        logged warning) and the cooldown timer is (re)started. On any other,
        unexpected error we fall back to the primary for THIS read only and do
        NOT self-disable — so a bug or oddity can't permanently kill replica
        routing the way the old bare ``except Exception`` did.
        """
        try:
            from django.db import connections

            conn = connections[self.replica_alias]
            conn.ensure_connection()
        except _connection_error_types() as e:
            if self._replica_healthy:
                logger.warning(
                    "ReadReplicaRouter: replica %r unhealthy (%s); routing reads "
                    "to primary %r, will re-probe in %.0fs.",
                    self.replica_alias,
                    e,
                    self.primary_alias,
                    self.health_cooldown_seconds,
                )
            self._replica_healthy = False
            self._unhealthy_at = time.monotonic()
            return False
        # blind-except: replica probe must never propagate — an unexpected (non-connection) error logs and falls back to primary for this read only, deliberately NOT self-disabling the replica so a bug can't permanently pin all reads.
        except Exception:
            logger.exception(
                "ReadReplicaRouter: unexpected error probing replica %r; using "
                "primary for this read (replica NOT disabled).",
                self.replica_alias,
            )
            return False
        else:
            if not self._replica_healthy:
                logger.info(
                    "ReadReplicaRouter: replica %r recovered; resuming replica reads.",
                    self.replica_alias,
                )
            self._replica_healthy = True
            self._unhealthy_at = 0.0
            return True

    def db_for_read(self, model, **hints):
        """Route read queries to replica when healthy, else primary."""
        if not self._replica_healthy:
            # Stay on the primary until the cooldown elapses, then re-probe.
            if (time.monotonic() - self._unhealthy_at) < self.health_cooldown_seconds:
                return self.primary_alias
        if self._probe_replica():
            return self.replica_alias
        return self.primary_alias

    def db_for_write(self, model, **hints):
        """Route write queries to primary."""
        return self.primary_alias

    def allow_relation(self, obj1, obj2, **hints):
        """Allow relations between objects in different databases."""
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Only allow migrations on the primary database."""
        return db == self.primary_alias


@dataclass(slots=True)
class PrimaryOnlyRouter:
    """Routes ALL queries to primary. Useful as a fallback."""

    primary_alias: str = "default"

    def db_for_read(self, model, **hints):
        return self.primary_alias

    def db_for_write(self, model, **hints):
        return self.primary_alias

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == self.primary_alias
