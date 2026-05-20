"""
HyperManager audit writer.

Every gated action — publish, replay, feed connect, admin, and every denial —
appends an AccessLog row. The batching, never-drop, and in-transaction-safe
flushing all live in the framework's ``BatchWriter``; this module only maps an
action into an ``AccessLog`` row and persists a batch with ``bulk_create``.

``flush_pending()`` drains before an audit read; ``install(app)`` self-registers
the periodic flush and the shutdown drain.
"""

from datetime import UTC, datetime

from hyperdjango.batchwriter import BatchWriter

from .models import AccessLog


async def _persist(rows: list[AccessLog]) -> None:
    await AccessLog.objects.bulk_create(rows)


class AuditWriter:
    """Maps gated actions to AccessLog rows on top of a ``BatchWriter``."""

    def __init__(self, *, flush_interval: float, flush_batch: int):
        self._writer: BatchWriter[AccessLog] = BatchWriter(
            _persist,
            flush_batch=flush_batch,
            flush_interval=flush_interval,
            name="hypermanager_audit",
        )

    async def record(
        self,
        *,
        identity: str,
        action: str,
        outcome: str,
        subject: str = "",
        client_ip: str = "",
        auth_method: str = "",
        fingerprint: str = "",
    ) -> None:
        await self._writer.record(
            AccessLog(
                identity=identity,
                action=action,
                outcome=outcome,
                subject=subject,
                client_ip=client_ip,
                auth_method=auth_method,
                fingerprint=fingerprint,
                created_at=datetime.now(UTC),
            )
        )

    async def flush_pending(self) -> None:
        """Drain everything buffered — called before audit reads."""
        await self._writer.flush_pending()

    def install(self, app) -> None:
        """Register the periodic flush and shutdown drain on the app lifecycle."""
        self._writer.install(app)
