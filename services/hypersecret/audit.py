"""
HyperSecret audit writer.

Every access — including denials — produces an AccessLog row. The batching,
never-drop, and in-transaction-safe flushing all live in the framework's
``BatchWriter``; this module only maps an access into an ``AccessLog`` row and
persists a batch with ``bulk_create``.

Read-your-writes for auditors: the audit query endpoint calls
``flush_pending()`` before selecting. ``install(app)`` self-registers the
periodic flush and the shutdown drain, so the app never babysits the writer.
"""

from datetime import UTC, datetime

from hyperdjango.batchwriter import BatchWriter

from .models import AccessLog


async def _persist(rows: list[AccessLog]) -> None:
    await AccessLog.objects.bulk_create(rows)


class AuditWriter:
    """Maps access events to AccessLog rows on top of a ``BatchWriter``."""

    def __init__(self, *, flush_interval: float, flush_batch: int):
        self._writer: BatchWriter[AccessLog] = BatchWriter(
            _persist,
            flush_batch=flush_batch,
            flush_interval=flush_interval,
            name="hypersecret_audit",
        )

    async def record(
        self,
        *,
        identity: str,
        namespace: str,
        key: str,
        version: int,
        action: str,
        outcome: str,
        client_ip: str,
        auth_method: str = "",
        fingerprint: str = "",
    ) -> None:
        await self._writer.record(
            AccessLog(
                identity=identity,
                namespace=namespace,
                key=key,
                version=version,
                action=action,
                outcome=outcome,
                client_ip=client_ip,
                auth_method=auth_method,
                fingerprint=fingerprint,
                # Stamped here, not left to the model default: bulk_create
                # inserts exactly the fields present on the instance.
                created_at=datetime.now(UTC),
            )
        )

    async def flush_pending(self) -> None:
        """Drain everything buffered — called before audit reads."""
        await self._writer.flush_pending()

    def install(self, app) -> None:
        """Register the periodic flush and shutdown drain on the app lifecycle."""
        self._writer.install(app)
