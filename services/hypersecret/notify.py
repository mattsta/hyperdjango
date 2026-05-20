"""
HyperSecret → HyperManager change notifier (transactional-outbox poster).

Durability is provided by the OutboxEvent table: a change is written there in
the same transaction as the secret state change (see app.py), and a scheduled
drainer posts each pending row to the hub and deletes it on acknowledgement.
This module is the thin publish client that drainer uses. It speaks the hub's
one-POST publish protocol directly through a framework ``ServiceClient`` — it
has NO dependency on the HyperManager app, only on the shared transport.

Each post carries the outbox row's id as the hub ``dedupe_key`` and is marked
idempotent, so a transport retry (or a drainer restart after a crash between
the POST and the local delete) collapses to a single delivered event — the hub
dedupes on that key whatever delivery tier it runs.

``post`` returns a tri-state outcome so the drainer can act correctly on each:

- ``DELIVERED``  — the hub accepted it; the row can be deleted.
- ``RETRYABLE``  — transport exhaustion or a 5xx; leave the row and retry the
  whole backlog next drain.
- ``PERMANENT``  — a 4xx (bad token, malformed subject): re-posting will fail
  identically, so the row is parked rather than blocking the feed forever.

Notifications are wake-ups, not the system of record — HyperSecret's own
version history stays authoritative, and subscribers reconcile through their
normal fetch path.
"""

from typing import NamedTuple

from hyperdjango.logging import logger
from hyperdjango.serviceclient import (
    ServerError,
    ServiceClient,
    ServiceError,
    ServiceUnavailable,
)
from hyperdjango.telemetry.metrics import Counter

NOTIFY_ERRORS = Counter(
    "hypersecret_notify_errors_total",
    "Change notifications that failed transiently (row is retried next drain).",
)
NOTIFY_POSTED = Counter(
    "hypersecret_notify_posted_total",
    "Change notifications successfully posted to the hub and drained.",
)

DELIVERED = "delivered"
RETRYABLE = "retryable"
PERMANENT = "permanent"

# 4xx statuses that mean "try again later" rather than "this row is poison":
# rate limiting and request timeout are transient backpressure during a burst.
_RETRYABLE_STATUSES = frozenset({408, 429})


class PostOutcome(NamedTuple):
    """Result of one publish attempt: a status plus, when it failed, the
    hub's rejection detail (recorded on a parked outbox row)."""

    status: str
    detail: str = ""


class ChangeNotifier:
    """Posts outbox rows to a HyperManager hub. Disabled when ``manager_url``
    is empty — then the drainer is a no-op and nothing is ever posted."""

    def __init__(self, *, manager_url: str, manager_token: str):
        self.enabled = bool(manager_url)
        self._client: ServiceClient | None = None
        if self.enabled:
            self._client = ServiceClient(manager_url, token=manager_token)

    def post(
        self, subject: str, kind: str, metadata: dict, *, dedupe_key: str
    ) -> PostOutcome:
        """Post one change event idempotently. Returns a ``PostOutcome`` the
        drainer maps to delete / retry-later / park."""
        if self._client is None:
            return PostOutcome(RETRYABLE, "notifier disabled")
        try:
            self._client.request(
                "POST",
                "/v1/events",
                json_body={
                    "subject": subject,
                    "kind": kind,
                    "metadata": metadata,
                    "dedupe_key": dedupe_key,
                },
                idempotent=True,
            )
            NOTIFY_POSTED.inc()
            return PostOutcome(DELIVERED)
        except (ServiceUnavailable, ServerError) as exc:
            # Transport exhaustion or a 5xx: the hub may accept it later.
            NOTIFY_ERRORS.inc()
            logger.warning(
                "Change notification post failed transiently ({s} {k}): {err} "
                "— will retry",
                s=subject,
                k=kind,
                err=exc,
            )
            return PostOutcome(RETRYABLE, str(exc))
        except ServiceError as exc:
            # 429 (rate limited) / 408 (request timeout) are transient
            # backpressure, not a poison event: leave the row pending and retry
            # the whole backlog next drain rather than parking it for an admin.
            if exc.status in _RETRYABLE_STATUSES:
                NOTIFY_ERRORS.inc()
                logger.warning(
                    "Change notification throttled ({s} {k}): {err} — will retry",
                    s=subject,
                    k=kind,
                    err=exc,
                )
                return PostOutcome(RETRYABLE, str(exc))
            # A genuinely permanent 4xx (bad token, malformed subject/kind,
            # 400/403/404/409/422). Re-posting is futile; the drainer parks the
            # row instead of blocking the feed behind it.
            logger.error(
                "Change notification permanently rejected ({s} {k}): {err} "
                "— parking outbox row",
                s=subject,
                k=kind,
                err=exc,
            )
            return PostOutcome(PERMANENT, exc.detail or str(exc))
